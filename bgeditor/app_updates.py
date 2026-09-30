# Background Editor - portrait-aware background removal
# Copyright (C) 2026 Optimey CommV
# SPDX-License-Identifier: GPL-3.0-or-later
#
# This program is free software: you can redistribute it and/or modify it under the terms
# of the GNU General Public License as published by the Free Software Foundation, either
# version 3 of the License, or (at your option) any later version.
#
# This program is distributed in the hope that it will be useful, but WITHOUT ANY
# WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS FOR A
# PARTICULAR PURPOSE. See the GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License along with this
# program. If not, see <https://www.gnu.org/licenses/>.

"""App updates: notice a newer Background Editor release on GitHub, and install it when
the user clicks Update now. Nothing here imports Qt.

check_for_app_update() asks GitHub for the latest release of UPDATE_REPO (drafts and
pre-releases are never 'latest'), at most once a day unless forced, with a conditional
request (ETag). On HTTP 403 or 429 it stops and waits for the next daily slot or the
server's reset time, whichever is later; it never retries in a loop. No repository or no
release yet (HTTP 404) is not an error: the report is 'skipped' and the app stays silent.

The asset to install depends on how the app runs: BackgroundEditor-Setup-<version>.exe for
an installed copy, BackgroundEditor-<version>-portable.zip for a portable one.

The signed release manifest BackgroundEditor-<version>-manifest.ps1 that build.ps1 makes is
the root of trust, not GitHub: GitHub's 'digest' is computed over whatever was uploaded, so
it only proves the download arrived intact. A newer release is announced only when its
manifest verifies: the check downloads it (a few kB) and checks it before it says anything.
Its Authenticode signature must be intact and made by exactly the certificate whose SHA-256
is bgeditor.SIGNER_CERT_SHA256 (read when it is needed; the CA behind it is private, so an
untrusted or unknown root is accepted, but only together with that pin). The manifest is a
PowerShell file that holds data only; it is never run, only the JSON inside its
single-quoted here-string is read, and any other statement makes it invalid. Its version
must be the release's version and newer than this copy, and GitHub's size (and digest, when
there is one) of this copy's download must be the manifest's. A release that fails any of
this is not an update: its report is 'unverified', which the app never announces at startup
and never links to, because its files cannot be told apart from a tampered release.
download_update() checks the manifest again, and only then downloads the installer or the
portable zip, which must match the manifest's SHA-256 and size byte for byte. A verified
release that this copy cannot install by itself (the manifest's 'min_update_from' is above
this version, or the copy runs from source) points to the release page instead.

A portable zip is unpacked next to the app folder (in a folder only this user may change,
where the file system allows that) and every file is checked against the manifest's list:
the same set of files, each with its SHA-256 and size. apply_update() checks that set again
right before it starts the PowerShell helper; the helper checks it once more after the app
has closed and hashes every file again while it copies it into the app folder, so nothing
can be swapped in between. The helper, its settings and its backup of the program files live
in a second private folder next to the app; the app holds the helper and its settings open
against changes until the helper has read them, and the helper checks the settings' SHA-256.
models\\, data\\, portable.conf, custom model and data folders (also when they are junctions
or links) and any other top-level link are never touched. The installer is hashed again,
while it is held open so that nobody can change it, right before it is started.

For every PE file, check_signature() also refuses a certificate table that holds anything
besides the PKCS#7 signature and its zero padding: Authenticode does not hash that table,
so data hidden there would otherwise pass as signed.

After an update, cleanup_old_downloads() removes the downloads this app made itself and
recorded by name, SHA-256 and size, once this copy is at least that version; nothing else
in the folder is touched.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import hashlib
import json
import os
import re
import secrets
import shutil
import stat
import struct
import subprocess
import sys
import threading
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from . import UPDATE_REPO, __version__, models, paths, updates

# ------------------------------------------------------------------ settings
STATE_NAME = "app-updates.json"
STATE_VERSION = 1
CHECK_INTERVAL = dt.timedelta(hours=24)
MAX_BLOCK = dt.timedelta(days=7)  # longest wait a rate limit (or a wrong clock) can impose

REPO_URL = f"https://github.com/{UPDATE_REPO}"
RELEASES_URL = f"{REPO_URL}/releases"
URL_LATEST = f"https://api.github.com/repos/{UPDATE_REPO}/releases/latest"
DOWNLOAD_PREFIX = f"{REPO_URL}/releases/download/"
EXE_NAME = "BackgroundEditor.exe"
ZIP_ROOT = "BackgroundEditor"  # the portable zip holds everything in this folder
MAX_ASSET_BYTES = 2_000_000_000
HELPER_START_TIMEOUT = 20.0  # seconds the app waits for the portable helper to report in
HELPER_WAIT_SECONDS = 180  # how long the helper waits for the app to exit
DOWNLOADS_NAME = "downloads.json"  # the record of this app's own downloads, in updates_dir()

# The signed release manifest (made by tools/build_tools.py, which reads these two lines).
MANIFEST_FORMAT = 1
MANIFEST_VARIABLE = "BackgroundEditorManifest"
MAX_MANIFEST_BYTES = 4 << 20
MAX_MANIFEST_FILES = 20000

USER_AGENT = f"BackgroundEditor/{__version__} (app update check)"
_API_LIMIT = 4 << 20  # bytes of a release answer
_NOTES_LIMIT = 64 << 10  # characters of release notes kept
_CHUNK = 1 << 20

Progress = Callable[[str, int, int], None]  # stage text, done, total (0: unknown)

_busy = threading.Lock()
_downloads_lock = threading.Lock()

# Windows error codes of WinVerifyTrust (as unsigned 32-bit values).
TRUST_E_NOSIGNATURE = 0x800B0100
TRUST_E_BAD_DIGEST = 0x80096010
TRUST_E_EXPLICIT_DISTRUST = 0x800B0111
TRUST_E_SUBJECT_NOT_TRUSTED = 0x800B0004
TRUST_E_PROVIDER_UNKNOWN = 0x800B0001
TRUST_E_SUBJECT_FORM_UNKNOWN = 0x800B0003
CERT_E_UNTRUSTEDROOT = 0x800B0109
CERT_E_CHAINING = 0x800B010A
CERT_E_REVOKED = 0x800B010C
CERT_E_EXPIRED = 0x800B0101
CERT_E_WRONG_USAGE = 0x800B0110
CRYPT_E_FILE_ERROR = 0x80092003
# Chain problems that only mean "this PC does not know or trust the root". Everything else
# (a bad signature in the chain, revocation, wrong usage, explicit distrust, ...) is fatal.
CERT_TRUST_IS_UNTRUSTED_ROOT = 0x00000020
CERT_TRUST_REVOCATION_STATUS_UNKNOWN = 0x00000040
CERT_TRUST_IS_PARTIAL_CHAIN = 0x00010000
CERT_TRUST_IS_OFFLINE_REVOCATION = 0x01000000
_ROOT_ONLY_CHAIN_ERRORS = (
    CERT_TRUST_IS_UNTRUSTED_ROOT
    | CERT_TRUST_IS_PARTIAL_CHAIN
    | CERT_TRUST_REVOCATION_STATUS_UNKNOWN
    | CERT_TRUST_IS_OFFLINE_REVOCATION
)

_TRUST_REASONS = {
    TRUST_E_NOSIGNATURE: "it is not signed",
    TRUST_E_BAD_DIGEST: "it was changed after it was signed",
    TRUST_E_EXPLICIT_DISTRUST: "its certificate is marked as not trusted on this PC",
    TRUST_E_SUBJECT_NOT_TRUSTED: "Windows does not trust its signature",
    TRUST_E_PROVIDER_UNKNOWN: "Windows cannot check signatures of this kind of file",
    TRUST_E_SUBJECT_FORM_UNKNOWN: "Windows cannot check signatures of this kind of file",
    CERT_E_REVOKED: "its certificate was revoked",
    CERT_E_EXPIRED: "its certificate had expired when it was signed",
    CERT_E_WRONG_USAGE: "its certificate is not meant for code signing",
    CRYPT_E_FILE_ERROR: "the file could not be read",
}


class UpdateError(Exception):
    """The update cannot go ahead; the message says why, in words for the user."""


class IntegrityError(UpdateError):
    """The release, or what was downloaded or unpacked from it, is not exactly what Optimey
    signed. Such a release must never be installed, also not by hand, so the app offers no
    link to it."""


class ManifestError(IntegrityError):
    """The release manifest is missing, not signed by the pinned certificate, or not valid."""


class ManifestFormatError(UpdateError):
    """The release manifest is signed by the pinned certificate, but of a format this version
    does not understand: a genuine release that has to be installed by hand."""


class _NoRelease(Exception):
    """GitHub answered 404: no repository, or no release published yet."""


@dataclass
class AppUpdateReport:
    # "available" is only ever a release whose signed manifest verified. "unverified" is a
    # newer release that failed that: never announced, never linked, never installed.
    status: str  # "up-to-date" | "available" | "unverified" | "failed" | "skipped"
    message: str
    current: str = ""
    latest: str = ""
    notes: str = ""  # the release body (Markdown; not covered by the signature: show it inertly)
    html_url: str = ""  # the release page ('' for an unverified release)
    asset_name: str = ""
    asset_url: str = ""
    asset_size: int = 0
    github_sha256: str = ""  # GitHub's 'digest' of the asset: a second check, not the gate
    manifest_name: str = ""  # the signed release manifest; without it: notify only
    manifest_url: str = ""
    manifest_size: int = 0
    manifest_github_sha256: str = ""
    verified: bool = False  # the signed release manifest passed (always so for "available")
    installable: bool = False  # Update now can download, check and install it here
    portable: bool = False  # the asset is the portable zip
    details: str = ""


@dataclass(frozen=True)
class ManifestFile:
    path: str  # relative to the zip's root folder, with '/'
    sha256: str
    size: int


@dataclass(frozen=True)
class ManifestAsset:
    name: str
    sha256: str
    size: int
    files: tuple[ManifestFile, ...] = ()  # the portable zip only


@dataclass(frozen=True)
class ReleaseManifest:
    version: str
    built: str
    min_update_from: str  # '' = any version may update to this one
    setup: ManifestAsset | None
    portable: ManifestAsset | None
    sha256: str = ""  # of the manifest file itself


@dataclass
class PreparedUpdate:
    """A downloaded update that matched the signed manifest, ready for apply_update()."""

    path: Path  # the installer or the portable zip
    version: str
    sha256: str  # from the signed manifest
    size: int
    portable: bool
    staging: Path | None = None  # portable: the unpacked, checked files
    files: tuple[ManifestFile, ...] = ()


# ------------------------------------------------------------------ versions
_TAG_RE = re.compile(r"[vV]?(\d{1,5}(?:\.\d{1,5}){0,3})")
_OWN_RE = re.compile(r"[vV]?(\d{1,5}(?:\.\d{1,5}){0,3})([-+][0-9A-Za-z.-]*)?")


def tag_version(tag) -> str:
    """'1.2.0' for the tags '1.2.0' and 'v1.2.0'; '' for anything that is not a plain version."""
    m = _TAG_RE.fullmatch(tag.strip()) if isinstance(tag, str) else None
    return m.group(1) if m else ""


def version_key(text) -> tuple | None:
    """A comparable key: numeric parts (1.10.0 > 1.9.0, 1.2 == 1.2.0), and a version with a
    pre-release suffix ('1.2.0-rc1') before the plain one. None when text is no version."""
    m = _OWN_RE.fullmatch(text.strip()) if isinstance(text, str) else None
    if not m:
        return None
    parts = [int(p) for p in m.group(1).split(".")]
    suffix = m.group(2) or ""
    return (tuple(parts + [0] * (4 - len(parts))), 0 if suffix.startswith("-") else 1)


def is_newer(latest: str, current: str) -> bool:
    new, old = version_key(latest), version_key(current)
    if new is None:
        return False
    return old is None or new > old


def current_version() -> str:
    """This copy's version (a build override from BackgroundEditor.spec included)."""
    import bgeditor

    return str(getattr(bgeditor, "__version__", __version__))


def signer_pin() -> str:
    """The SHA-256 of the certificate that must have signed an update (upper-case hex).
    Read from bgeditor.SIGNER_CERT_SHA256 each time it is needed, never bound at import."""
    import bgeditor

    return str(getattr(bgeditor, "SIGNER_CERT_SHA256", "") or "")


def download_user_agent() -> str:
    return f"BackgroundEditor/{current_version()} (app update)"


def installer_asset_name(version: str) -> str:
    return f"BackgroundEditor-Setup-{version}.exe"


def portable_asset_name(version: str) -> str:
    return f"BackgroundEditor-{version}-portable.zip"


def manifest_asset_name(version: str) -> str:
    return f"BackgroundEditor-{version}-manifest.ps1"


_V = r"\d{1,5}(?:\.\d{1,5}){0,3}"
_ASSET_NAME_RE = re.compile(rf"BackgroundEditor-(?:Setup-{_V}\.exe|{_V}-portable\.zip)")
_MANIFEST_NAME_RE = re.compile(rf"BackgroundEditor-{_V}-manifest\.ps1")
_SHA256_RE = re.compile(r"[0-9a-f]{64}")


# ------------------------------------------------------------------ state
def updates_dir() -> Path:
    """Downloads, the portable helper and its log."""
    return paths.data_dir() / "updates"


def state_path() -> Path:
    return paths.data_dir() / STATE_NAME


def staging_dir() -> Path:
    """Where a portable update is unpacked: next to the app folder, on the same drive."""
    app = paths.app_dir()
    return app.parent / f"{app.name}.update-staging"


def helper_dir(app_dir: Path | None = None) -> Path:
    """The portable helper's private folder next to the app folder: apply-update.ps1, its
    settings and, while it runs, the backup of the program files."""
    app = Path(app_dir) if app_dir is not None else paths.app_dir()
    return app.parent / f"{app.name}.update-helper"


def _load_state() -> dict:
    try:
        with open(state_path(), encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        data = {}
    if not isinstance(data, dict) or data.get("version") != STATE_VERSION:
        data = {"version": STATE_VERSION}
    if not isinstance(data.get("release"), dict):
        data.pop("release", None)
        data.pop("etag", None)
    return data


def _write_json(path: Path, data) -> None:
    """Write JSON atomically (a temporary file next to it, then a rename). Raises OSError."""
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(tmp, "w", encoding="utf-8", newline="") as fh:
            json.dump(data, fh, indent=1, ensure_ascii=False)
            fh.write("\n")
        os.replace(tmp, path)
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


def _save_state(state: dict) -> None:
    try:
        _write_json(state_path(), state)
    except OSError:
        pass


def _text(state: dict, key: str) -> str:
    value = state.get(key)
    return value if isinstance(value, str) else ""


def is_check_due() -> bool:
    """True when the daily check is due and no rate limit applies. A time far in the future
    (a clock that was wrong) does not block the check for longer than MAX_BLOCK."""
    state = _load_state()
    now = updates._now()
    blocked = updates._parse_time(state.get("blocked_until"))
    if blocked is not None and now < blocked <= now + MAX_BLOCK:
        return False
    nxt = updates._parse_time(state.get("next_check"))
    return nxt is None or now >= nxt or nxt > now + CHECK_INTERVAL * 2


def last_status() -> dict:
    """For the settings panel: last check, its outcome, the newest version seen, the skipped one."""
    state = _load_state()
    return {
        "last_check": _text(state, "last_check"),
        "message": _text(state, "message"),
        "status": _text(state, "status"),
        "latest": _text(state, "latest"),
        "skipped_version": _text(state, "skipped_version"),
    }


def skip_version(version: str) -> None:
    """'Skip this version': the automatic check stays silent about it (a manual check still shows it)."""
    state = _load_state()
    state["skipped_version"] = str(version)
    _save_state(state)


def _finish(state: dict, report: AppUpdateReport, checked: bool) -> AppUpdateReport:
    if checked:
        now = updates._now()
        state["last_check"] = updates._iso(now)
        state["next_check"] = updates._iso(now + CHECK_INTERVAL)
    state["status"] = report.status
    state["message"] = report.message
    _save_state(state)
    return report


# ------------------------------------------------------------------ HTTP
def _default_transport(url: str, method: str, headers: dict, redirect: bool, limit: int) -> updates.Reply:
    return updates._urllib_transport(url, method, headers, redirect, limit)


# Replaced by the tests with recorded answers: the API (_transport) and small release
# assets such as the manifest (_asset_transport).
_transport: Callable[[str, str, dict, bool, int], updates.Reply] = _default_transport
_asset_transport: Callable[[str, str, dict, bool, int], updates.Reply] = _default_transport


def _fetch_latest(state: dict, cancel: threading.Event | None) -> dict:
    """The latest release (as stored by _release_record); the stored copy on 304 Not Modified."""
    if cancel is not None and cancel.is_set():
        raise models.DownloadCancelled()
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": USER_AGENT,
    }
    etag = _text(state, "etag") if state.get("release") else ""
    if etag:
        headers["If-None-Match"] = etag
    reply = _transport(URL_LATEST, "GET", headers, True, _API_LIMIT)
    if reply.status in (403, 429):
        raise updates.RateLimited("api.github.com", updates._reset_time(reply.headers))
    if reply.status == 404:
        state.pop("release", None)
        state.pop("etag", None)
        raise _NoRelease()
    if reply.status == 304 and etag:
        return state["release"]
    if reply.status != 200:
        raise updates.HttpStatusError(URL_LATEST, reply.status)
    try:
        data = json.loads(reply.body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise ValueError("GitHub sent an answer that is not JSON") from exc
    release = _release_record(data)
    state["release"] = release
    state["etag"] = reply.headers.get("etag", "")
    return release


def _release_record(data) -> dict:
    """The parts of a GitHub release this module uses, type-checked."""
    if not isinstance(data, dict):
        raise ValueError("GitHub sent an unexpected answer about the latest release")
    assets = []
    for a in data.get("assets") or []:
        if not isinstance(a, dict) or not isinstance(a.get("name"), str):
            continue
        size = a.get("size")
        assets.append(
            {
                "name": a["name"],
                "url": str(a.get("browser_download_url") or ""),
                "size": size if isinstance(size, int) and not isinstance(size, bool) else 0,
                "digest": str(a.get("digest") or ""),
            }
        )
    html_url = str(data.get("html_url") or "")
    return {
        "tag": str(data.get("tag_name") or ""),
        "name": str(data.get("name") or "")[:200],
        "body": str(data.get("body") or "")[:_NOTES_LIMIT],
        "html_url": html_url if html_url.startswith(REPO_URL + "/") else RELEASES_URL,
        "published": str(data.get("published_at") or ""),
        "draft": bool(data.get("draft")),
        "prerelease": bool(data.get("prerelease")),
        "assets": assets,
    }


# ------------------------------------------------------------------ the check
def self_update_blocker() -> str:
    """Why Update now cannot install here ('' when it can)."""
    if sys.platform != "win32":
        return "Automatic installation works on Windows only; download it from the release page."
    if not getattr(sys, "frozen", False):
        return "This copy runs from the source code; get the new version from the release page."
    return ""


def check_for_app_update(force: bool = False, cancel: threading.Event | None = None) -> AppUpdateReport:
    """The daily check (force: now, for Check for updates). Never raises."""
    current = current_version()
    if not _busy.acquire(blocking=False):
        return AppUpdateReport("skipped", "A check for a new version is already running.", current=current)
    try:
        state = _load_state()
        now = updates._now()
        blocked = updates._parse_time(state.get("blocked_until"))
        if blocked is not None and now < blocked <= now + MAX_BLOCK:
            return AppUpdateReport(
                "skipped",
                f"GitHub asked to wait (rate limit); the next check for a new version is after {blocked.astimezone():%d %b %Y, %H:%M}.",
                current=current,
            )
        if not force and not is_check_due():
            return AppUpdateReport("skipped", "The next check for a new version is not due yet.", current=current)
        try:
            release = _fetch_latest(state, cancel)
        except models.DownloadCancelled:
            return AppUpdateReport("skipped", "The check for a new version was cancelled.", current=current)
        except updates.RateLimited as exc:
            # As in updates.py: nothing is asked before the server's reset time (a click
            # neither), and the automatic check waits for its next daily slot or that time.
            nxt = now + CHECK_INTERVAL
            until = min(max(exc.until or nxt, now), now + MAX_BLOCK)
            state["blocked_until"] = updates._iso(until)
            state["last_check"] = updates._iso(now)
            state["next_check"] = updates._iso(max(nxt, until))
            report = AppUpdateReport(
                "skipped",
                f"GitHub asked to wait (rate limit); the next check for a new version is after {until.astimezone():%d %b %Y, %H:%M}.",
                current=current,
            )
            return _finish(state, report, checked=False)
        except _NoRelease:
            report = AppUpdateReport(
                "skipped",
                "No release of Background Editor has been published on GitHub yet.",
                current=current,
                html_url=RELEASES_URL,
            )
            return _finish(state, report, checked=True)
        except Exception as exc:  # no network, a proxy, an unexpected answer: try again tomorrow
            report = AppUpdateReport(
                "failed",
                f"The check for a new version did not work: {updates._short(exc)}. It tries again tomorrow.",
                current=current,
                details=f"{type(exc).__name__}: {exc}",
            )
            return _finish(state, report, checked=True)
        return _finish(state, _judge(release, state, current, force, cancel), checked=True)
    finally:
        _busy.release()


def _asset_url_ok(url: str) -> bool:
    return url.startswith(DOWNLOAD_PREFIX) and ".." not in url


def _github_sha256(digest: str) -> str:
    m = re.fullmatch(r"sha256:([0-9a-fA-F]{64})", digest or "")
    return m.group(1).lower() if m else ""


def _is_reserved_name(part: str) -> bool:
    """Windows device names in every spelling Windows accepts (COM or LPT with a superscript digit, 'CON .txt', ...)."""
    check = getattr(os.path, "isreserved", None)
    try:
        return bool(check(part)) if check is not None else False
    except (TypeError, ValueError):
        return True


def printable_tag(tag: str) -> str:
    """A release tag comes from whoever can publish on GitHub: show only safe characters."""
    return re.sub(r"[^0-9A-Za-z._+-]", "?", tag or "")[:40]


def _judge(
    release: dict, state: dict, current: str, force: bool, cancel: threading.Event | None = None
) -> AppUpdateReport:
    """What the latest release means for this copy. A newer release is 'available' only when
    its signed release manifest verifies (see _verified_release); otherwise 'unverified'."""
    tag = release.get("tag", "")
    version = tag_version(tag)
    base = {"current": current, "html_url": release.get("html_url") or RELEASES_URL}
    if release.get("draft") or release.get("prerelease"):
        return AppUpdateReport("skipped", "The latest release on GitHub is marked as a pre-release.", **base)
    if not version:
        return AppUpdateReport(
            "skipped", f"The latest release on GitHub is tagged '{printable_tag(tag)}', which is not a version number.", **base
        )
    if not is_newer(version, current):
        state["latest"] = version
        if version_key(version) == version_key(current):
            text = f"Background Editor {current} is the newest version."
        else:
            text = f"Background Editor {current} is up to date (the newest release is {version})."
        return AppUpdateReport("up-to-date", text, latest=version, **base)
    if not force and _text(state, "skipped_version") == version:
        return AppUpdateReport("skipped", f"Version {version} is available; you chose to skip it.", current=current, latest=version)
    report = _verified_release(release, version, current, cancel, base)
    if report.status == "available":
        state["latest"] = state["last_seen"] = version
    return report


def _sentence(text: str) -> str:
    """'the manifest ...' -> 'The manifest ....'"""
    text = text.strip()
    return (text[:1].upper() + text[1:] + ("" if text.endswith(".") else ".")) if text else ""


def _unverified(version: str, current: str, why: str) -> AppUpdateReport:
    """A newer release that did not pass the signed manifest: no notes, no link."""
    return AppUpdateReport(
        "unverified",
        f"Version {version} on GitHub is not a verified Optimey release: {why}. "
        "It is not offered as an update; do not install it.",
        current=current,
        latest=version,
    )


def _verified_release(
    release: dict, version: str, current: str, cancel: threading.Event | None, base: dict
) -> AppUpdateReport:
    """Download and verify the release's signed manifest, and match this copy's download on
    GitHub against it. Only a verified release becomes 'available' (installable, or with
    the release page when this copy cannot install it by itself)."""
    if sys.platform != "win32":
        return AppUpdateReport(
            "unverified",
            f"Version {version} is on GitHub, but this copy cannot check its Optimey signature "
            "(that works on Windows only), so it is not offered here.",
            current=current,
            latest=version,
        )
    assets = release.get("assets", [])
    manifest_name = manifest_asset_name(version)
    manifest_asset = next((a for a in assets if a.get("name") == manifest_name), None)
    if manifest_asset is None:
        return _unverified(version, current, "its release has no signed release manifest")
    if not _asset_url_ok(manifest_asset.get("url", "")) or not 0 < manifest_asset.get("size", 0) <= MAX_MANIFEST_BYTES:
        return _unverified(version, current, "its release manifest is not where Optimey publishes it, or has an unusual size")
    portable = paths.is_portable()
    report = AppUpdateReport(
        "available", "", latest=version, notes=release.get("body", ""), portable=portable, **base
    )
    report.manifest_name, report.manifest_url = manifest_name, manifest_asset["url"]
    report.manifest_size = manifest_asset["size"]
    report.manifest_github_sha256 = _github_sha256(manifest_asset.get("digest", ""))
    headline = f"Background Editor {version} is available (you have {current})."
    try:
        folder = updates_dir()
        folder.mkdir(parents=True, exist_ok=True)
        manifest = _download_manifest(report, folder, cancel)
    except models.DownloadCancelled:
        return AppUpdateReport("skipped", "The check for a new version was cancelled.", current=current)
    except IntegrityError as exc:
        return _unverified(version, current, str(exc))
    except ManifestFormatError as exc:  # signed by Optimey, but newer than this copy understands
        report.verified = True
        report.message = f"{headline} {_sentence(str(exc))}"
        return report
    except (UpdateError, OSError) as exc:  # no network, a proxy, a full disk: try again tomorrow
        return AppUpdateReport(
            "failed",
            f"The check for a new version did not work: {updates._short(exc)}. It tries again tomorrow.",
            current=current,
            details=f"{type(exc).__name__}: {exc}",
        )
    problem = manifest_problem(manifest, version, current)
    if problem:
        return _unverified(version, current, problem)
    report.verified = True
    wanted = portable_asset_name(version) if portable else installer_asset_name(version)
    entry = manifest.portable if portable else manifest.setup
    if entry is None:
        kind = "a portable zip" if portable else "an installer"
        report.message = f"{headline} It comes without {kind}, so it is not installed automatically; see the release page."
        return report
    asset = next((a for a in assets if a.get("name") == wanted), None)
    if asset is None:
        report.message = f"{headline} Its release on GitHub lacks {wanted}, so it is not installed automatically; see the release page."
        return report
    url, size = asset.get("url", ""), asset.get("size", 0)
    github = _github_sha256(asset.get("digest", ""))
    if not _asset_url_ok(url):
        return _unverified(version, current, f"its {wanted} is not where Optimey publishes it")
    if size != entry.size or (github and github != entry.sha256):
        return _unverified(version, current, f"its {wanted} on GitHub is not the file in the signed release manifest")
    report.asset_name, report.asset_url, report.asset_size, report.github_sha256 = wanted, url, size, github
    minimum = minimum_problem(manifest, current)
    if minimum:
        report.message = f"{headline} {_sentence(minimum)}"
        return report
    blocker = self_update_blocker()
    report.installable = not blocker
    report.message = f"{headline} {blocker}".strip()
    return report


# ------------------------------------------------------------------ the signed manifest
_SIG_BEGIN = "# SIG # Begin signature block"
_SIG_END = "# SIG # End signature block"
_SIG_BLOCK_RE = re.compile(r"# SIG # Begin signature block\r?\n(?:# [A-Za-z0-9+/=]{1,256}\r?\n)+# SIG # End signature block(?:\r?\n)?")
_MANIFEST_BODY_RE = re.compile(
    r"(?P<comments>(?:#(?: [ -~]*)?\r?\n)*)"
    r"\$" + MANIFEST_VARIABLE + r" = @'\r?\n"
    r"(?P<json>(?:[^\r\n]*\r?\n)*?)"
    r"'@\r?\n"
)
_WINDOWS_RESERVED = {"con", "prn", "aux", "nul", "conin$", "conout$"} | {f"{p}{i}" for p in ("com", "lpt") for i in range(10)}


def _bad(text: str) -> ManifestError:
    return ManifestError(f"the release manifest is not valid ({text})")


def _manifest_json(data: bytes, signed: bool) -> str:
    """The JSON text inside the manifest's here-string. The file must be exactly: comment
    lines, the assignment of one single-quoted here-string, and (signed) the signature block."""
    if len(data) > MAX_MANIFEST_BYTES:
        raise _bad("it is too large")
    try:
        text = data.decode("ascii")
    except UnicodeDecodeError:
        raise _bad("it is not plain ASCII text") from None
    begins, ends = text.count(_SIG_BEGIN), text.count(_SIG_END)
    if signed:
        if begins != 1 or ends != 1:
            raise _bad("it does not end with exactly one signature block")
        i = text.index(_SIG_BEGIN)
        if not _SIG_BLOCK_RE.fullmatch(text, i):
            raise _bad("its signature block is not at the end or not in the expected form")
        body = text[:i]
        # Set-AuthenticodeSignature puts one line break before the block.
        if body.endswith("\r\n"):
            body = body[:-2]
        elif body.endswith("\n"):
            body = body[:-1]
        else:
            raise _bad("its signature block does not start on a line of its own")
    else:
        if begins or ends:
            raise _bad("it holds a signature block")
        body = text
    m = _MANIFEST_BODY_RE.fullmatch(body)
    if not m:
        raise _bad(f"it holds more than comments and the assignment of ${MANIFEST_VARIABLE}")
    for line in m.group("comments").splitlines():
        if line[1:].strip().lower().startswith("requires"):
            raise _bad("it holds a #Requires line")
    json_text = m.group("json")
    if any(line.startswith("'@") for line in json_text.splitlines()):
        raise _bad("its data ends early")
    return json_text


def _no_duplicate_keys(pairs):
    keys = [k for k, _ in pairs]
    if len(keys) != len(set(keys)):
        raise ValueError("a key appears twice")
    return dict(pairs)


def _reject_constant(name):
    raise ValueError(f"{name} is not allowed")


def _int_field(value, low: int, high: int) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        return None
    return value


def manifest_path_problem(rel) -> str:
    """Why rel is not a safe relative path for a file of the portable zip ('' when it is)."""
    if not isinstance(rel, str) or not 0 < len(rel) <= 400:
        return "an empty or overlong path"
    if "\\" in rel or rel.startswith("/") or ":" in rel:
        return f"the path '{rel[:120]}' is not relative"
    for part in rel.split("/"):
        if part in ("", ".", ".."):
            return f"the path '{rel[:120]}' has an empty, '.' or '..' part"
        if any(ch in '<>"|?*' or ord(ch) < 32 or ord(ch) == 127 for ch in part):
            return f"the path '{rel[:120]}' holds a character Windows does not allow"
        if "~" in part:
            # 'PORTAB~1.CON' may be the 8.3 short name of another file (portable.conf); the
            # build never makes a name with '~', so none is accepted.
            return f"the path '{rel[:120]}' holds a '~', which could name another file by its short name"
        if part[-1] in ". ":
            return f"the path '{rel[:120]}' has a part that ends with a dot or a space"
        if part.split(".", 1)[0].lower() in _WINDOWS_RESERVED or _is_reserved_name(part):
            return f"the path '{rel[:120]}' uses a reserved Windows name"
    return ""


def _manifest_asset(value, name: str, key: str, portable: bool) -> ManifestAsset | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise _bad(f"'{key}' is not an object")
    if value.get("name") != name:
        raise _bad(f"'{key}' is not {name}")
    sha = value.get("sha256")
    size = _int_field(value.get("size"), 1, MAX_ASSET_BYTES)
    if not isinstance(sha, str) or not _SHA256_RE.fullmatch(sha) or size is None:
        raise _bad(f"'{key}' has no valid SHA-256 and size")
    files: list[ManifestFile] = []
    if portable:
        if value.get("root") != ZIP_ROOT:
            raise _bad(f"the files of '{key}' are not in {ZIP_ROOT}")
        raw = value.get("files")
        if not isinstance(raw, list) or not 0 < len(raw) <= MAX_MANIFEST_FILES:
            raise _bad(f"'{key}' has no valid list of files")
        seen: set[str] = set()
        for item in raw:
            if not isinstance(item, dict):
                raise _bad("a file entry is not an object")
            rel, fsha = item.get("path"), item.get("sha256")
            fsize = _int_field(item.get("size"), 0, MAX_ASSET_BYTES)
            problem = manifest_path_problem(rel)
            if problem:
                raise _bad(problem)
            if not isinstance(fsha, str) or not _SHA256_RE.fullmatch(fsha) or fsize is None:
                raise _bad(f"{rel[:120]} has no valid SHA-256 and size")
            low = rel.lower()
            if low in seen:
                raise _bad(f"{rel[:120]} is listed twice")
            seen.add(low)
            files.append(ManifestFile(rel, fsha, fsize))
        if EXE_NAME.lower() not in seen:
            raise _bad(f"'{key}' lists no {EXE_NAME}")
    return ManifestAsset(name, sha, size, tuple(files))


def parse_manifest(data: bytes, signed: bool = True) -> ReleaseManifest:
    """The release manifest from the bytes of the .ps1 file. Nothing in it is run: only the
    JSON of its here-string is read, and a file that holds anything else is refused.
    `signed`: the file must end with a signature block (check it with verify_manifest)."""
    json_text = _manifest_json(data, signed)
    try:
        doc = json.loads(json_text, object_pairs_hook=_no_duplicate_keys, parse_constant=_reject_constant)
    except ValueError as exc:
        raise _bad(f"its data is not valid JSON: {exc}") from None
    if not isinstance(doc, dict):
        raise _bad("its data is not an object")
    fmt = _int_field(doc.get("format"), 1, 1 << 30)
    if fmt is None:
        raise _bad("it has no format number")
    if fmt != MANIFEST_FORMAT:
        # Only reached for a signed manifest after its signature passed (verify_manifest).
        raise ManifestFormatError(
            f"the release manifest has format {fmt}, which this version does not understand; "
            "install the new version by hand from the release page"
        )
    version, built, minimum = doc.get("version"), doc.get("built", ""), doc.get("min_update_from", "")
    if not isinstance(version, str) or version_key(version) is None:
        raise _bad("it has no valid version")
    if not isinstance(built, str) or len(built) > 64:
        raise _bad("its build time is not valid")
    if not isinstance(minimum, str) or (minimum and version_key(minimum) is None):
        raise _bad("its minimum version is not valid")
    setup = _manifest_asset(doc.get("setup"), installer_asset_name(version), "setup", portable=False)
    portable = _manifest_asset(doc.get("portable"), portable_asset_name(version), "portable", portable=True)
    if setup is None and portable is None:
        raise _bad("it lists no download")
    return ReleaseManifest(version, built, minimum, setup, portable, hashlib.sha256(data).hexdigest())


def verify_manifest(path: Path | str, pinned: str | None = None) -> ReleaseManifest:
    """The release manifest in `path`, after its Authenticode signature passed the pin
    (default: signer_pin(), read now). The file is held open against changes while it is
    read and checked, so the bytes that are parsed are the bytes whose signature was checked.
    Raises ManifestError (or ManifestFormatError for a signed manifest of an unknown format)."""
    path = Path(path)
    pinned = signer_pin() if pinned is None else pinned
    if sys.platform != "win32":
        raise ManifestError("the release manifest can only be checked on Windows")
    try:
        with _hold_readonly(path):
            with open(path, "rb") as fh:
                data = fh.read(MAX_MANIFEST_BYTES + 1)
            checked = _check_held(path, pinned)
    except OSError as exc:
        raise ManifestError(f"the release manifest cannot be read ({updates._short(exc)})") from exc
    if not checked.ok:
        raise ManifestError(f"the release manifest is not signed by Optimey: {checked.reason}")
    return parse_manifest(data, signed=True)


def manifest_problem(manifest: ReleaseManifest, release_version: str, current: str) -> str:
    """Why the signed manifest does not describe this release as an update for this copy
    ('' when it does): another version than the release's (an old manifest replayed in a new
    release), or not newer than this copy. Either means the release is not what Optimey
    published."""
    if version_key(manifest.version) != version_key(release_version):
        return f"the signed release manifest is for version {manifest.version}, not for version {release_version}"
    if not is_newer(manifest.version, current):
        return (
            f"version {manifest.version} is not newer than this copy ({current}); "
            "an older version or the same one is never installed automatically"
        )
    return ""


def minimum_problem(manifest: ReleaseManifest, current: str) -> str:
    """Why this copy is too old to install the (verified) release by itself ('' when it is
    not): the manifest's min_update_from is above this version. The release is genuine; it
    is installed by hand."""
    minimum = manifest.min_update_from
    if minimum:
        own = version_key(current)
        if own is None or own < version_key(minimum):
            return (
                f"version {manifest.version} can only be installed automatically over version {minimum} "
                f"or newer, and this copy is version {current}. Download it from the release page and "
                "install it by hand"
            )
    return ""


# ------------------------------------------------------------------ download
def download_update(
    report: AppUpdateReport,
    cancel: threading.Event | None = None,
    progress: Progress | None = None,
) -> PreparedUpdate:
    """Download and check the update of `report`. Raises IntegrityError when the release or
    a download is not exactly what Optimey signed, UpdateError for anything else (or
    models.DownloadCancelled / ChecksumMismatch).

    First the signed release manifest (see verify_manifest and manifest_problem); only when
    it passes, the installer or the portable zip, which must match the manifest's SHA-256
    and size. GitHub's digest must agree with the manifest when GitHub publishes one. A
    portable zip is then unpacked into staging_dir() and every file checked against the
    manifest. A partial download is resumed; only a download whose checksum does not match
    is deleted (by models.download_model). A file downloaded earlier is reused when it
    still matches.
    """
    say = progress or (lambda _s, _d, _t: None)
    if report.status != "available" or not report.verified:
        raise ManifestError("The release is not a verified Optimey release, so it is not installed.")
    name = report.asset_name
    if not _ASSET_NAME_RE.fullmatch(name or ""):
        raise UpdateError(f"'{name}' is not a Background Editor download.")
    if not report.asset_url or not 0 < report.asset_size <= MAX_ASSET_BYTES:
        raise UpdateError("The size of the download is not known.")
    if not report.manifest_url or report.manifest_name != manifest_asset_name(report.latest):
        raise ManifestError("The release has no signed release manifest, so it is not installed.")
    portable = name.endswith(".zip")
    if portable:
        _check_portable_folder()
    folder = updates_dir()
    try:
        folder.mkdir(parents=True, exist_ok=True)
        free = shutil.disk_usage(folder).free
    except OSError as exc:
        raise UpdateError(f"The folder for the download cannot be used ({updates._short(exc)}): {folder}") from exc

    # 1. The signed manifest, before anything big is downloaded.
    say("Checking the signed release manifest", 0, 0)
    try:
        manifest = _download_manifest(report, folder, cancel)
    except UpdateError as exc:
        raise type(exc)(f"The update was not installed: {exc}.") from exc
    entry = manifest.portable if portable else manifest.setup
    problem = manifest_problem(manifest, report.latest, current_version())
    if not problem and entry is not None:
        if entry.size != report.asset_size:
            problem = (
                f"GitHub offers {name} with {report.asset_size:,} bytes, but the signed release "
                f"manifest says {entry.size:,}"
            )
        elif report.github_sha256 and report.github_sha256 != entry.sha256:
            problem = f"the SHA-256 GitHub publishes for {name} is not the one in the signed release manifest"
    if problem:
        raise IntegrityError(f"The update was not installed: {problem}.")
    if entry is None:
        raise UpdateError(f"The update was not installed: the signed release manifest lists no {name}.")
    minimum = minimum_problem(manifest, current_version())
    if minimum:
        raise UpdateError(f"The update was not installed: {minimum}.")

    # 2. The installer or the zip; its SHA-256 must be the manifest's.
    _record_download(name, entry.sha256, entry.size, manifest.version)
    spec = models.ModelSpec(
        key="app-update",
        title=f"Background Editor {manifest.version}",
        filename=name,
        url=report.asset_url,
        sha256=entry.sha256,
        size=entry.size,
        licence="GPL-3.0-or-later",
    )
    target = folder / name
    title = f"Downloading Background Editor {manifest.version}"
    reuse = False
    try:
        reuse = target.is_file() and target.stat().st_size == entry.size and models.verify_file(target, spec, cancel)
    except OSError:
        reuse = False
    if not reuse:
        if free < 3 * entry.size:
            raise UpdateError(f"There is not enough free disk space for the update ({3 * entry.size / 1e6:.0f} MB needed).")
        say(title, 0, entry.size)
        models.download_model(
            spec,
            progress=lambda done, total: say(title, done, total),
            cancel=cancel,
            folder=folder,
            user_agent=download_user_agent(),
        )
    if cancel is not None and cancel.is_set():
        raise models.DownloadCancelled()

    # 3. Installer: its signature as well. Portable: unpack and check every file.
    if portable:
        staging = _stage_portable(target, entry, cancel, say)
        checked = check_signature(staging / ZIP_ROOT / EXE_NAME)
        if not checked.ok:
            _remove_tree(staging)
            raise IntegrityError(f"The download was not installed: its {EXE_NAME}: {checked.reason}.")
        return PreparedUpdate(target, manifest.version, entry.sha256, entry.size, True, staging, entry.files)
    say("Checking the signature", 0, 0)
    checked = check_signature(target)
    if not checked.ok:
        raise IntegrityError(f"The download was not installed: {checked.reason}.")
    return PreparedUpdate(target, manifest.version, entry.sha256, entry.size, False)


def _download_manifest(report: AppUpdateReport, folder: Path, cancel: threading.Event | None) -> ReleaseManifest:
    """Download the release manifest into folder and check it (verify_manifest). A manifest
    that fails is removed again: it is this app's own download and of no use. The errors are
    phrases ('the release manifest ...') for the caller to put in a sentence: IntegrityError
    (ManifestError) when it is not what Optimey signed, ManifestFormatError when it is signed
    but of an unknown format, UpdateError when it could not be downloaded or saved."""
    if cancel is not None and cancel.is_set():
        raise models.DownloadCancelled()
    url = report.manifest_url
    if not _asset_url_ok(url) or not _MANIFEST_NAME_RE.fullmatch(report.manifest_name):
        raise ManifestError("the release manifest is not where Optimey publishes it")
    headers = {"User-Agent": download_user_agent(), "Accept": "application/octet-stream"}
    try:
        reply = _asset_transport(url, "GET", headers, True, MAX_MANIFEST_BYTES)
    except (OSError, ValueError) as exc:
        raise UpdateError(f"the signed release manifest could not be downloaded ({updates._short(exc)})") from exc
    if reply.status != 200:
        raise UpdateError(f"the signed release manifest could not be downloaded (HTTP {reply.status})")
    data = reply.body
    if report.manifest_github_sha256 and hashlib.sha256(data).hexdigest() != report.manifest_github_sha256:
        raise ManifestError("the release manifest does not match the checksum GitHub publishes for it")
    path = folder / report.manifest_name
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    try:
        with open(tmp, "wb") as fh:
            fh.write(data)
        os.replace(tmp, path)
    except OSError as exc:
        raise UpdateError(f"the signed release manifest could not be saved in {folder} ({updates._short(exc)})") from exc
    finally:
        tmp.unlink(missing_ok=True)
    try:
        manifest = verify_manifest(path)
    except UpdateError:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
        raise
    _record_download(path.name, manifest.sha256, len(data), manifest.version)
    return manifest


def _check_portable_folder() -> None:
    """The portable update writes into the app folder and next to it."""
    app = paths.app_dir()
    for folder in (app, app.parent):
        if not paths.folder_writable(folder):
            raise UpdateError(
                f"Windows does not allow Background Editor to write in {folder}, so it cannot update itself there. "
                "Download the new version from the release page instead."
            )


def zip_problem(zip_path: Path) -> str:
    """Why a portable zip cannot be used ('' when its layout is as the build makes it)."""
    try:
        with zipfile.ZipFile(zip_path) as zf:
            names = zf.namelist()
    except (OSError, zipfile.BadZipFile) as exc:
        return f"the zip cannot be read ({updates._short(exc)})"
    for n in names:
        parts = n.replace("\\", "/").split("/")
        if n.startswith(("/", "\\")) or ":" in n or ".." in parts or parts[0] != ZIP_ROOT:
            return f"the zip holds an unexpected path ({n[:120]})"
    if f"{ZIP_ROOT}/{EXE_NAME}" not in names:
        return f"the zip holds no {ZIP_ROOT}/{EXE_NAME}"
    return ""


def _sha256_path(path: Path, cancel: threading.Event | None = None) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(_CHUNK), b""):
            if cancel is not None and cancel.is_set():
                raise models.DownloadCancelled()
            h.update(chunk)
    return h.hexdigest()


def _stage_portable(zip_path: Path, entry: ManifestAsset, cancel: threading.Event | None, say: Progress) -> Path:
    """Unpack the portable zip into a fresh staging_dir(), checking every file against the
    manifest while it is written. Returns the staging folder; removes it on any problem."""
    problem = zip_problem(zip_path)
    if problem:
        raise IntegrityError(f"The download was not installed: {problem}.")
    expected = {f.path.lower(): f for f in entry.files}
    prefix = ZIP_ROOT + "/"
    staging = staging_dir()
    title = "Unpacking and checking the new version"
    try:
        with zipfile.ZipFile(zip_path) as zf:
            members: dict[str, zipfile.ZipInfo] = {}
            for info in zf.infolist():
                if info.is_dir():
                    continue
                key = info.filename[len(prefix):].lower()
                if key in members:
                    raise IntegrityError(f"The download was not installed: the zip holds {info.filename[:120]} twice.")
                members[key] = info
            missing = sorted(set(expected) - set(members))
            extra = sorted(set(members) - set(expected))
            if missing or extra:
                what = f"{len(missing)} missing, e.g. {missing[0]}" if missing else f"{len(extra)} not listed, e.g. {extra[0]}"
                raise IntegrityError(f"The download was not installed: its files differ from the signed release manifest ({what}).")
            _new_private_dir(staging)
            root = staging / ZIP_ROOT
            total, done = sum(f.size for f in entry.files), 0
            say(title, 0, total)
            for key, f in sorted(expected.items()):
                info = members[key]
                if info.file_size != f.size:
                    raise IntegrityError(f"The download was not installed: {f.path} does not match the signed release manifest.")
                out = root.joinpath(*f.path.split("/"))
                out.parent.mkdir(parents=True, exist_ok=True)
                h = hashlib.sha256()
                with zf.open(info) as src, open(out, "xb") as dst:
                    for chunk in iter(lambda: src.read(_CHUNK), b""):
                        if cancel is not None and cancel.is_set():
                            raise models.DownloadCancelled()
                        h.update(chunk)
                        dst.write(chunk)
                        done += len(chunk)
                        say(title, done, total)
                if h.hexdigest() != f.sha256:
                    raise IntegrityError(f"The download was not installed: {f.path} does not match the signed release manifest.")
                stamp = time.mktime(info.date_time + (0, 0, -1))
                os.utime(out, (stamp, stamp))
    except BaseException:
        _remove_tree(staging)
        raise
    return staging


def staging_problem(staging: Path, files: tuple[ManifestFile, ...] | list[ManifestFile]) -> str:
    """Why the unpacked update in `staging` is not exactly the manifest's files ('' when it
    is): the same set of files (no file missing, none added), each with its SHA-256 and size,
    and no links."""
    root = staging / ZIP_ROOT
    try:
        if staging.is_symlink() or staging.is_junction() or not staging.is_dir():
            return "the unpacked update is missing or is a link"
        found: dict[str, Path] = {}
        for dirpath, dirnames, filenames in os.walk(staging):
            here = Path(dirpath)
            for name in dirnames + filenames:
                p = here / name
                if p.is_symlink() or p.is_junction():
                    return f"the unpacked update holds a link ({p.relative_to(staging).as_posix()[:120]})"
            for name in filenames:
                p = here / name
                try:
                    rel = p.relative_to(root).as_posix()
                except ValueError:
                    return f"the unpacked update holds an unexpected file ({p.relative_to(staging).as_posix()[:120]})"
                found[rel.lower()] = p
        expected = {f.path.lower(): f for f in files}
        missing = sorted(set(expected) - set(found))
        extra = sorted(set(found) - set(expected))
        if missing:
            return f"{len(missing)} file(s) of the new version are missing from the unpacked update, e.g. {missing[0][:120]}"
        if extra:
            return f"the unpacked update holds {len(extra)} file(s) the signed release manifest does not list, e.g. {extra[0][:120]}"
        for key, f in expected.items():
            p = found[key]
            if p.stat().st_size != f.size or _sha256_path(p) != f.sha256:
                return f"the unpacked {f.path} does not match the signed release manifest"
    except OSError as exc:
        return f"the unpacked update cannot be read ({updates._short(exc)})"
    return ""


def _remove_tree(path: Path) -> None:
    """Remove a folder this module made (staging); links inside are removed, never followed."""
    try:
        if path.is_symlink() or path.is_junction():
            os.rmdir(path) if path.is_dir() else os.unlink(path)
            return
        if not path.exists():
            return

        def writable_again(func, p, _exc):
            os.chmod(p, stat.S_IWRITE)
            func(p)

        shutil.rmtree(path, onexc=writable_again)
    except OSError:
        pass


def _new_private_dir(path: Path) -> None:
    """A fresh, empty folder at path that only this user (with SYSTEM and Administrators)
    may change, where the file system supports that; otherwise an ordinary folder (the
    files in it are checked again before they are used)."""
    _remove_tree(path)
    if path.exists() or path.is_symlink():
        raise UpdateError(f"The old folder {path} cannot be removed; close any program that uses it and try again.")
    try:
        if _create_private_dir(path):
            return
    except OSError:
        pass
    try:
        path.mkdir()
    except OSError as exc:
        raise UpdateError(f"The folder for the new version cannot be made ({updates._short(exc)}): {path}") from exc


def _create_private_dir(path: Path) -> bool:
    """CreateDirectoryW with a protected DACL for this user, SYSTEM and Administrators only.
    False when that is not possible here (the caller then makes an ordinary folder)."""
    if sys.platform != "win32":
        return False
    import ctypes
    from ctypes import wintypes

    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p
    kernel32.CreateDirectoryW.argtypes = [wintypes.LPCWSTR, ctypes.c_void_p]
    kernel32.CreateDirectoryW.restype = wintypes.BOOL
    advapi32.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
    advapi32.OpenProcessToken.restype = wintypes.BOOL
    advapi32.GetTokenInformation.argtypes = [
        wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)
    ]
    advapi32.GetTokenInformation.restype = wintypes.BOOL
    advapi32.ConvertSidToStringSidW.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)]
    advapi32.ConvertSidToStringSidW.restype = wintypes.BOOL
    advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p
    ]
    advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW.restype = wintypes.BOOL

    class SECURITY_ATTRIBUTES(ctypes.Structure):
        _fields_ = [("nLength", wintypes.DWORD), ("lpSecurityDescriptor", ctypes.c_void_p), ("bInheritHandle", wintypes.BOOL)]

    token = wintypes.HANDLE()
    if not advapi32.OpenProcessToken(kernel32.GetCurrentProcess(), 0x0008, ctypes.byref(token)):  # TOKEN_QUERY
        return False
    try:
        need = wintypes.DWORD()
        advapi32.GetTokenInformation(token, 1, None, 0, ctypes.byref(need))  # TokenUser
        if not need.value:
            return False
        buf = ctypes.create_string_buffer(need.value)
        if not advapi32.GetTokenInformation(token, 1, buf, need, ctypes.byref(need)):
            return False
    finally:
        kernel32.CloseHandle(token)
    sid = ctypes.cast(buf, ctypes.POINTER(ctypes.c_void_p))[0]  # TOKEN_USER.User.Sid
    text = ctypes.c_void_p()
    if not advapi32.ConvertSidToStringSidW(sid, ctypes.byref(text)):
        return False
    try:
        user = ctypes.wstring_at(text.value)
    finally:
        kernel32.LocalFree(text)
    if not re.fullmatch(r"S-1-[0-9-]+", user):
        return False
    sd = ctypes.c_void_p()
    sddl = f"D:P(A;OICI;FA;;;SY)(A;OICI;FA;;;BA)(A;OICI;FA;;;{user})"
    if not advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW(sddl, 1, ctypes.byref(sd), None):
        return False
    try:
        attrs = SECURITY_ATTRIBUTES(ctypes.sizeof(SECURITY_ATTRIBUTES), sd, False)
        return bool(kernel32.CreateDirectoryW(str(path), ctypes.byref(attrs)))
    finally:
        kernel32.LocalFree(sd)


# ------------------------------------------------------------------ own downloads
def _downloads_path() -> Path:
    return updates_dir() / DOWNLOADS_NAME


def _load_downloads() -> list[dict]:
    try:
        with open(_downloads_path(), encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return []
    items = data.get("downloads") if isinstance(data, dict) else None
    return [d for d in items if isinstance(d, dict)] if isinstance(items, list) else []


def _record_download(name: str, sha256: str, size: int, version: str) -> None:
    """Remember a file this app downloads into updates_dir(), so that it, and only it, can
    be removed after the update (cleanup_old_downloads)."""
    with _downloads_lock:
        items = [d for d in _load_downloads() if str(d.get("name", "")).lower() != name.lower()]
        items.append({"name": name, "sha256": sha256, "size": int(size), "version": version})
        try:
            _write_json(_downloads_path(), {"downloads": items})
        except OSError:
            pass


def cleanup_old_downloads() -> list[str]:
    """Remove this app's own downloads of versions that are not newer than this copy: the
    update to them is done (or a newer version runs). A file is removed only when its name,
    size and SHA-256 are exactly what was recorded when it was downloaded; its unfinished
    '.part' file goes too. Nothing else in the folder is touched. Returns the removed names.
    Never raises."""
    own = version_key(current_version())
    folder = updates_dir()
    removed: list[str] = []
    if own is None:
        return removed
    with _downloads_lock:
        items = _load_downloads()
        keep: list[dict] = []
        for d in items:
            name, sha, size, version = d.get("name"), d.get("sha256"), d.get("size"), d.get("version")
            if not (
                isinstance(name, str)
                and (_ASSET_NAME_RE.fullmatch(name) or _MANIFEST_NAME_RE.fullmatch(name))
                and isinstance(sha, str)
                and _SHA256_RE.fullmatch(sha)
                and _int_field(size, 1, MAX_ASSET_BYTES) is not None
                and version_key(version) is not None
            ):
                continue  # a record that describes nothing of ours
            if version_key(version) > own:
                keep.append(d)  # not installed yet: kept for Update now
                continue
            still_there = False
            for path, whole in ((folder / name, True), (folder / f"{name}.part", False)):
                try:
                    info = path.lstat()
                    if not stat.S_ISREG(info.st_mode):
                        continue
                    if whole:
                        if info.st_size != size or _sha256_path(path) != sha:
                            continue  # not what this app downloaded: left alone
                    elif info.st_size > size:
                        continue
                    path.unlink()
                    removed.append(path.name)
                except FileNotFoundError:
                    continue
                except OSError:
                    still_there = True  # in use: try again next time
            if still_there:
                keep.append(d)
        if keep != items:
            try:
                _write_json(_downloads_path(), {"downloads": keep})
            except OSError:
                pass
    return removed


# ------------------------------------------------------------------ Authenticode
@dataclass
class SignatureCheck:
    ok: bool
    reason: str  # why not, or how it was trusted
    code: int = 0  # WinVerifyTrust's result, as an unsigned 32-bit value
    cert_sha256: str = ""  # SHA-256 of the signer certificate (DER), upper-case hex
    subject: str = ""  # the signer's name
    chain_errors: int = 0  # CERT_TRUST_* error bits of the signer's chain


def signature_verdict(code: int, cert_sha256: str, chain_errors: int, pinned: str | None = None) -> tuple[bool, str]:
    """Decide on WinVerifyTrust's result. The signature must be present and intact, and the
    signer must be the pinned certificate (by the SHA-256 of its DER encoding; default:
    signer_pin(), read now). A root this PC does not know or trust is fine only together with
    that pin; a trusted chain with another signer is refused too."""
    code &= 0xFFFFFFFF
    pinned = (signer_pin() if pinned is None else pinned or "").upper()
    found = (cert_sha256 or "").upper()
    matches = bool(found) and len(pinned) == 64 and found == pinned
    if code == 0:
        if matches:
            return True, "signed by the Optimey code signing certificate"
        return False, f"it is signed by another certificate ({found or 'unknown'}), not by Optimey"
    if code in (CERT_E_UNTRUSTEDROOT, CERT_E_CHAINING):
        # WinVerifyTrust checks the file's hash and the signature before it looks at the
        # chain, so these codes mean the signature itself is intact.
        if not matches:
            return False, f"it is signed by an unknown certificate ({found or 'unknown'})"
        if chain_errors & ~_ROOT_ONLY_CHAIN_ERRORS:
            return False, f"its certificate chain has errors (0x{chain_errors:08X})"
        return True, "signed by the Optimey code signing certificate (its private CA is not trusted on this PC; the certificate is pinned)"
    reason = _TRUST_REASONS.get(code, f"Windows reports signature error 0x{code:08X}")
    return False, reason


# PKCS#7 SignedData (1.2.840.113549.1.7.2) and the start of PyInstaller's archive cookie.
_PKCS7_SIGNED_DATA = bytes.fromhex("06092a864886f70d010702")
_PYI_COOKIE_MAGIC = b"MEI\x0c\x0b\x0a\x0b\x0e"
_MAX_CERT_TABLE = 16 << 20


def _der_length(der: bytes) -> tuple[int, int] | None:
    """(header length, content length) of a DER SEQUENCE at the start of der, or None."""
    if len(der) < 2 or der[0] != 0x30:
        return None
    first = der[1]
    if first < 0x80:
        return 2, first
    count = first & 0x7F
    if not 1 <= count <= 4 or len(der) < 2 + count:
        return None
    return 2 + count, int.from_bytes(der[2 : 2 + count], "big")


def pe_signature_problem(path: Path | str) -> str:
    """Why the certificate table of a signed PE file holds more than it should ('' when it is
    fine, when the file is not a PE file, or when it is not signed; WinVerifyTrust decides
    those). Authenticode does not hash the certificate table, so data placed there after the
    signature - or a second entry - would pass WinVerifyTrust unnoticed. Allowed is exactly
    one WIN_CERTIFICATE (revision 2, PKCS#7) holding one DER SignedData, followed only by
    the zero bytes that pad it to an 8-byte boundary, at the very end of the file."""
    with open(path, "rb") as fh:
        head = fh.read(0x40)
        if len(head) < 0x40 or head[:2] != b"MZ":
            return ""
        pe = struct.unpack_from("<I", head, 0x3C)[0]
        fh.seek(pe)
        hdr = fh.read(24 + 240)
        if len(hdr) < 26 or hdr[:4] != b"PE\0\0":
            return ""
        magic = struct.unpack_from("<H", hdr, 24)[0]
        if magic == 0x10B:
            count_at, dirs_at = 24 + 92, 24 + 96
        elif magic == 0x20B:
            count_at, dirs_at = 24 + 108, 24 + 112
        else:
            return "its PE header is of an unknown kind"
        if len(hdr) < dirs_at + 5 * 8:
            return "its PE header is cut short"
        if struct.unpack_from("<I", hdr, count_at)[0] <= 4:
            return ""  # no security directory: not signed
        offset, size = struct.unpack_from("<II", hdr, dirs_at + 4 * 8)
        if offset == 0 and size == 0:
            return ""
        end = fh.seek(0, os.SEEK_END)
        if size < 8 or offset + size != end:
            return "its signature is not the last part of the file"
        if size > _MAX_CERT_TABLE:
            return "its signature block is unusually large"
        fh.seek(offset)
        table = fh.read(size)
    length, revision, kind = struct.unpack_from("<IHH", table, 0)
    if revision != 0x0200 or kind != 0x0002:
        return "its signature block is of an unknown kind"
    der = table[8:]
    parsed = _der_length(der)
    if parsed is None or der[parsed[0] : parsed[0] + len(_PKCS7_SIGNED_DATA)] != _PKCS7_SIGNED_DATA:
        return "its signature block does not hold a PKCS#7 signature"
    signature_end = 8 + parsed[0] + parsed[1]
    if not signature_end <= length <= size:
        return "the lengths in its signature block do not agree"
    if size - signature_end >= 8 or any(table[signature_end:]):
        return "its signature block holds data after the signature"
    if _PYI_COOKIE_MAGIC in table:
        return "its signature block holds an embedded program archive"
    return ""


@contextlib.contextmanager
def _hold_readonly(path: Path):
    """Keep path open so that nobody can change, replace, rename or delete it while it is
    checked (and, for the installer, started); reading and running it stay possible. Raises
    OSError when the file is missing or another program has it open for writing."""
    if sys.platform != "win32":
        yield
        return
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateFileW.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE
    ]
    kernel32.CreateFileW.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    # GENERIC_READ, FILE_SHARE_READ only, OPEN_EXISTING, FILE_ATTRIBUTE_NORMAL
    handle = kernel32.CreateFileW(str(path), 0x80000000, 0x1, None, 3, 0x80, None)
    if handle is None or handle == wintypes.HANDLE(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        yield
    finally:
        kernel32.CloseHandle(handle)


def check_signature(path: Path | str, pinned: str | None = None) -> SignatureCheck:
    """The Authenticode check of one file (see signature_verdict and, for PE files,
    pe_signature_problem), with the file held open against changes. `pinned` defaults to
    signer_pin(), read now. Never raises."""
    path = Path(path)
    if sys.platform != "win32":
        return SignatureCheck(False, "signatures can only be checked on Windows")
    if not path.is_file():
        return SignatureCheck(False, f"{path.name} is missing")
    try:
        with _hold_readonly(path):
            return _check_held(path, pinned)
    except OSError as exc:
        return SignatureCheck(False, f"the file cannot be opened for the check ({updates._short(exc)})")


def _check_held(path: Path, pinned: str | None) -> SignatureCheck:
    """check_signature for a file the caller holds open with _hold_readonly."""
    try:
        problem = pe_signature_problem(path)
        code, cert_sha256, subject, chain_errors = _win_verify_trust(path)
    except (OSError, AttributeError, ValueError, struct.error) as exc:
        return SignatureCheck(False, f"the signature could not be checked ({updates._short(exc)})")
    ok, reason = signature_verdict(code, cert_sha256, chain_errors, pinned)
    if ok and problem:
        ok, reason = False, problem
    return SignatureCheck(ok, reason, code, cert_sha256, subject, chain_errors)


def _win_verify_trust(path: Path) -> tuple[int, str, str, int]:
    """(result code, SHA-256 of the signer certificate, signer name, signer chain error bits)
    from WinVerifyTrust.

    One call does the whole check on one open of the file: the signer certificate is read
    from the provider data of that same verification, not by opening the file again.
    """
    import ctypes
    from ctypes import wintypes

    class GUID(ctypes.Structure):
        _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD), ("Data3", wintypes.WORD), ("Data4", ctypes.c_ubyte * 8)]

    class WINTRUST_FILE_INFO(ctypes.Structure):
        _fields_ = [
            ("cbStruct", wintypes.DWORD),
            ("pcwszFilePath", wintypes.LPCWSTR),
            ("hFile", wintypes.HANDLE),
            ("pgKnownSubject", ctypes.POINTER(GUID)),
        ]

    class WINTRUST_DATA(ctypes.Structure):
        _fields_ = [
            ("cbStruct", wintypes.DWORD),
            ("pPolicyCallbackData", ctypes.c_void_p),
            ("pSIPClientData", ctypes.c_void_p),
            ("dwUIChoice", wintypes.DWORD),
            ("fdwRevocationChecks", wintypes.DWORD),
            ("dwUnionChoice", wintypes.DWORD),
            ("pFile", ctypes.POINTER(WINTRUST_FILE_INFO)),
            ("dwStateAction", wintypes.DWORD),
            ("hWVTStateData", wintypes.HANDLE),
            ("pwszURLReference", wintypes.LPWSTR),
            ("dwProvFlags", wintypes.DWORD),
            ("dwUIContext", wintypes.DWORD),
            ("pSignatureSettings", ctypes.c_void_p),
        ]

    class CERT_CONTEXT(ctypes.Structure):
        _fields_ = [
            ("dwCertEncodingType", wintypes.DWORD),
            ("pbCertEncoded", ctypes.POINTER(ctypes.c_ubyte)),
            ("cbCertEncoded", wintypes.DWORD),
            ("pCertInfo", ctypes.c_void_p),
            ("hCertStore", ctypes.c_void_p),
        ]

    class CRYPT_PROVIDER_CERT(ctypes.Structure):  # only the leading fields are used
        _fields_ = [("cbStruct", wintypes.DWORD), ("pCert", ctypes.POINTER(CERT_CONTEXT))]

    class FILETIME(ctypes.Structure):
        _fields_ = [("dwLowDateTime", wintypes.DWORD), ("dwHighDateTime", wintypes.DWORD)]

    class CRYPT_PROVIDER_SGNR(ctypes.Structure):
        _fields_ = [
            ("cbStruct", wintypes.DWORD),
            ("sftVerifyAsOf", FILETIME),
            ("csCertChain", wintypes.DWORD),
            ("pasCertChain", ctypes.c_void_p),
            ("dwSignerType", wintypes.DWORD),
            ("psSigner", ctypes.c_void_p),
            ("dwError", wintypes.DWORD),
            ("csCounterSigners", wintypes.DWORD),
            ("pasCounterSigners", ctypes.c_void_p),
            ("pChainContext", ctypes.c_void_p),
        ]

    class CERT_CHAIN_HEAD(ctypes.Structure):  # CERT_CHAIN_CONTEXT up to TrustStatus
        _fields_ = [("cbSize", wintypes.DWORD), ("dwErrorStatus", wintypes.DWORD), ("dwInfoStatus", wintypes.DWORD)]

    wintrust = ctypes.WinDLL("wintrust")
    crypt32 = ctypes.WinDLL("crypt32")
    wintrust.WinVerifyTrust.argtypes = [wintypes.HWND, ctypes.POINTER(GUID), ctypes.c_void_p]
    wintrust.WinVerifyTrust.restype = ctypes.c_long
    wintrust.WTHelperProvDataFromStateData.argtypes = [wintypes.HANDLE]
    wintrust.WTHelperProvDataFromStateData.restype = ctypes.c_void_p
    wintrust.WTHelperGetProvSignerFromChain.argtypes = [ctypes.c_void_p, wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    wintrust.WTHelperGetProvSignerFromChain.restype = ctypes.POINTER(CRYPT_PROVIDER_SGNR)
    wintrust.WTHelperGetProvCertFromChain.argtypes = [ctypes.POINTER(CRYPT_PROVIDER_SGNR), wintypes.DWORD]
    wintrust.WTHelperGetProvCertFromChain.restype = ctypes.POINTER(CRYPT_PROVIDER_CERT)
    crypt32.CertGetNameStringW.argtypes = [
        ctypes.POINTER(CERT_CONTEXT), wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p, wintypes.LPWSTR, wintypes.DWORD
    ]
    crypt32.CertGetNameStringW.restype = wintypes.DWORD

    # WINTRUST_ACTION_GENERIC_VERIFY_V2 {00AAC56B-CD44-11d0-8CC2-00C04FC295EE}
    action = GUID(0x00AAC56B, 0xCD44, 0x11D0, (ctypes.c_ubyte * 8)(0x8C, 0xC2, 0x00, 0xC0, 0x4F, 0xC2, 0x95, 0xEE))
    file_info = WINTRUST_FILE_INFO(ctypes.sizeof(WINTRUST_FILE_INFO), str(path), None, None)
    data = WINTRUST_DATA()
    data.cbStruct = ctypes.sizeof(WINTRUST_DATA)
    data.dwUIChoice = 2  # WTD_UI_NONE
    data.fdwRevocationChecks = 0  # WTD_REVOKE_NONE: a private CA publishes no revocation list
    data.dwUnionChoice = 1  # WTD_CHOICE_FILE
    data.pFile = ctypes.pointer(file_info)
    data.dwStateAction = 1  # WTD_STATEACTION_VERIFY: keep the provider data for the signer
    # WTD_REVOCATION_CHECK_NONE | WTD_CACHE_ONLY_URL_RETRIEVAL: no network access
    data.dwProvFlags = 0x10 | 0x1000
    no_ui = wintypes.HWND(-1)  # INVALID_HANDLE_VALUE: no interactive user
    code = wintrust.WinVerifyTrust(no_ui, ctypes.byref(action), ctypes.byref(data)) & 0xFFFFFFFF
    cert_sha256, subject, chain_errors = "", "", 0
    try:
        prov = wintrust.WTHelperProvDataFromStateData(data.hWVTStateData) if data.hWVTStateData else None
        signer = wintrust.WTHelperGetProvSignerFromChain(prov, 0, False, 0) if prov else None
        if signer:
            if signer.contents.pChainContext:
                chain = ctypes.cast(signer.contents.pChainContext, ctypes.POINTER(CERT_CHAIN_HEAD)).contents
                chain_errors = int(chain.dwErrorStatus)
            cert = wintrust.WTHelperGetProvCertFromChain(signer, 0)
            if cert and cert.contents.pCert:
                ctx = cert.contents.pCert.contents
                encoded = ctypes.string_at(ctx.pbCertEncoded, ctx.cbCertEncoded)
                cert_sha256 = hashlib.sha256(encoded).hexdigest().upper()
                buf = ctypes.create_unicode_buffer(512)
                if crypt32.CertGetNameStringW(cert.contents.pCert, 4, 0, None, buf, len(buf)) > 1:  # SIMPLE_DISPLAY
                    subject = buf.value
    finally:
        data.dwStateAction = 2  # WTD_STATEACTION_CLOSE
        wintrust.WinVerifyTrust(no_ui, ctypes.byref(action), ctypes.byref(data))
    return code, cert_sha256, subject, chain_errors


# ------------------------------------------------------------------ apply
def apply_update(update: PreparedUpdate) -> None:
    """Start the update that download_update() prepared. Installed: the installer, silently;
    it closes the app, installs and starts the app again. Portable: the helper script, which
    waits for this process to exit. The caller quits the app right after this returns."""
    if sys.platform != "win32":
        raise UpdateError("Automatic installation works on Windows only.")
    if not isinstance(update, PreparedUpdate):
        raise UpdateError("This is not an update this app has downloaded and checked.")
    if update.portable:
        _start_portable_helper(update)
    else:
        _start_installer(update)


def child_environment() -> dict[str, str]:
    """The environment for the installer and the helper, and through them for the restarted
    app: without the settings PyInstaller's start-up made for this process only."""
    env = dict(os.environ)
    if getattr(sys, "frozen", False):
        env["PYINSTALLER_RESET_ENVIRONMENT"] = "1"  # the new app is an independent instance
        for key in ("QT_PLUGIN_PATH", "QML2_IMPORT_PATH"):
            env.pop(key, None)
        meipass = os.path.normcase(str(getattr(sys, "_MEIPASS", "")).rstrip("\\/"))
        if meipass and "PATH" in env:
            env["PATH"] = os.pathsep.join(
                p for p in env["PATH"].split(os.pathsep) if os.path.normcase(p.rstrip("\\/")) != meipass
            )
    return env


def _popen_detached(cmd, cwd: Path, extra_flags: int = 0) -> subprocess.Popen:
    """Start a process that outlives this one (also when this one runs in a job object)."""
    flags = subprocess.CREATE_NEW_PROCESS_GROUP | extra_flags
    kwargs = dict(
        cwd=str(cwd),
        env=child_environment(),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
    )
    try:
        return subprocess.Popen(cmd, creationflags=flags | 0x01000000, **kwargs)  # CREATE_BREAKAWAY_FROM_JOB
    except OSError:
        return subprocess.Popen(cmd, creationflags=flags, **kwargs)  # the job does not allow breaking away


def installer_command(path: Path) -> str:
    """The installer's command line: silent with a progress window, no cancel button, closes
    the app if it still runs, and starts it again afterwards (/RESTARTAPP=1, see installer.iss)."""
    log = path.with_suffix(".log")
    return f'"{path}" /SILENT /SP- /NOCANCEL /NORESTART /CLOSEAPPLICATIONS /RESTARTAPP=1 /LOG="{log}"'


def _start_installer(update: PreparedUpdate) -> None:
    """Hash the installer once more and start it, while it is held open so that it cannot be
    changed or replaced between the check and the start."""
    path = Path(update.path)
    if path.suffix.lower() != ".exe" or not _ASSET_NAME_RE.fullmatch(path.name):
        raise UpdateError(f"{path.name} is not an update this app can install.")
    try:
        with _hold_readonly(path):
            if path.stat().st_size != update.size or _sha256_path(path) != update.sha256:
                raise IntegrityError("The update was not started: the downloaded installer changed after it was checked.")
            checked = _check_held(path, None)
            if not checked.ok:
                raise IntegrityError(f"The update was not started: {checked.reason}.")
            _popen_detached(installer_command(path), path.parent)
    except OSError as exc:
        raise UpdateError(f"The installer could not be started: {updates._short(exc)}") from exc


def _kept_names(app_dir: Path) -> list[str]:
    """Top-level names in the app folder the portable update never touches: models, data,
    portable.conf, and the custom model and data folders of portable.conf when they lie
    inside the app folder. A custom folder is found both as configured and resolved, so one
    that is a junction or a link (whose resolved path lies elsewhere) is kept too."""
    names = {"models", "data", paths.PORTABLE_MARKER.lower()}
    app = Path(app_dir)
    bases = [Path(os.path.abspath(app))]
    try:
        bases.append(app.resolve())
    except OSError:
        pass
    for folder in (paths.model_dir(), paths.data_dir()):
        targets = [Path(os.path.abspath(folder))]
        try:
            targets.append(Path(folder).resolve())
        except OSError:
            pass
        for base in bases:
            for target in targets:
                try:
                    rel = target.relative_to(base)  # case-insensitive on Windows
                except ValueError:
                    continue
                if rel.parts:
                    names.add(rel.parts[0].lower())
    return sorted(names)


def powershell_exe() -> str:
    root = os.environ.get("SystemRoot") or r"C:\Windows"
    return str(Path(root) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe")


HELPER_SCRIPT = "apply-update.ps1"
HELPER_CONFIG = "apply-update.json"


def write_portable_helper(
    staging: Path,
    files: tuple[ManifestFile, ...] | list[ManifestFile],
    *,
    version: str,
    app_dir: Path,
    wait_pid: int,
    relaunch: list[str],
    token: str,
) -> list[str]:
    """Write apply-update.ps1 and its settings (with the manifest's file list) into a fresh
    helper_dir(app_dir), a folder only this user, SYSTEM and Administrators may change (as
    the staging folder, where the file system allows that); return the command that runs it.
    The command carries the settings' SHA-256, which the helper checks before it uses them.
    The helper's backup of the program files goes into that folder too."""
    folder = helper_dir(app_dir)
    _new_private_dir(folder)
    log_dir = updates_dir()
    log_dir.mkdir(parents=True, exist_ok=True)
    settings = {
        "token": token,
        "version": version,
        "staging": str(Path(staging)).rstrip("\\/"),
        "zip_root": ZIP_ROOT,
        "app_dir": str(Path(app_dir)).rstrip("\\/"),
        "backup": str(folder / "backup"),
        "keep": _kept_names(Path(app_dir)),
        "wait_pid": int(wait_pid),
        "wait_seconds": HELPER_WAIT_SECONDS,
        "relaunch": [str(a) for a in relaunch],
        "log": str(log_dir / "apply-update.log"),
        "files": [{"path": f.path, "sha256": f.sha256, "size": f.size} for f in files],
    }
    data = (json.dumps(settings, indent=1, ensure_ascii=False) + "\n").encode("utf-8")
    script = folder / HELPER_SCRIPT
    config = folder / HELPER_CONFIG
    try:
        # 'x': the folder is new and private, so neither file can be there already.
        with open(script, "xb") as fh:
            fh.write(PORTABLE_HELPER.encode("ascii"))
        with open(config, "xb") as fh:
            fh.write(data)
    except OSError as exc:
        raise UpdateError(f"The update helper could not be written ({updates._short(exc)}): {folder}") from exc
    return [
        powershell_exe(),
        "-NoProfile",
        "-NonInteractive",
        "-ExecutionPolicy",
        "Bypass",
        "-WindowStyle",
        "Hidden",
        "-File",
        str(script),
        "-Config",
        str(config),
        "-ConfigSha256",
        hashlib.sha256(data).hexdigest(),
    ]


def helper_log() -> Path:
    return updates_dir() / "apply-update.log"


def _log_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def discard_update(update: PreparedUpdate) -> None:
    """Remove what download_update() unpacked for an update that is not going ahead (the
    download itself stays, for the next try)."""
    if update is not None and update.staging is not None and Path(update.staging) == staging_dir():
        _remove_tree(Path(update.staging))


def _start_portable_helper(update: PreparedUpdate) -> None:
    """Start the helper; on any failure before it has taken over, the unpacked files and
    the helper's folder go."""
    try:
        _run_portable_helper(update)
    except BaseException:
        discard_update(update)
        _remove_tree(helper_dir())
        raise


def _run_portable_helper(update: PreparedUpdate) -> None:
    if not getattr(sys, "frozen", False):
        raise UpdateError("A copy that runs from the source code cannot replace itself.")
    _check_portable_folder()
    staging = update.staging
    if staging is None or Path(staging) != staging_dir() or not update.files:
        raise UpdateError("The update was not started: the new version has not been unpacked and checked.")
    # Checked once more right before the helper starts (the helper checks again once the
    # app has closed, and hashes every file while it copies it).
    problem = staging_problem(staging, update.files)
    if problem:
        raise IntegrityError(f"The update was not started: {problem}.")
    app_dir = paths.app_dir()
    token = secrets.token_hex(8)
    cmd = write_portable_helper(
        staging,
        update.files,
        version=update.version,
        app_dir=app_dir,
        wait_pid=os.getpid(),
        relaunch=[str(app_dir / EXE_NAME)],
        token=token,
    )
    folder = helper_dir(app_dir)
    expected = {
        folder / HELPER_SCRIPT: hashlib.sha256(PORTABLE_HELPER.encode("ascii")).hexdigest(),
        folder / HELPER_CONFIG: cmd[cmd.index("-ConfigSha256") + 1],
    }
    log = helper_log()
    with contextlib.ExitStack() as held:
        # The helper and its settings stay open against changes until the helper has read
        # them (it reports in only after that); what is held must be what was written.
        try:
            for path, sha in expected.items():
                held.enter_context(_hold_readonly(path))
                if _sha256_path(path) != sha:
                    raise IntegrityError(f"The update was not started: {path.name} changed after it was written.")
        except OSError as exc:
            raise UpdateError(f"The update helper cannot be used ({updates._short(exc)}): {folder}") from exc
        try:
            proc = _popen_detached(cmd, log.parent, subprocess.CREATE_NO_WINDOW)
        except OSError as exc:
            raise UpdateError(f"The update helper could not be started: {updates._short(exc)}") from exc
        # Quit only once the helper runs and holds on to this process (it waits for it to
        # exit); a helper blocked by policy or security software must not leave the user
        # without the app.
        deadline = time.monotonic() + HELPER_START_TIMEOUT
        while time.monotonic() < deadline:
            if f"started {token}" in _log_text(log):
                return
            code = proc.poll()
            if code is not None:
                raise UpdateError(f"The update helper stopped at once (exit code {code}). See {log}")
            time.sleep(0.1)
        try:
            proc.kill()
            proc.wait(5)
        except (OSError, subprocess.TimeoutExpired):
            pass
    raise UpdateError(f"The update helper did not start within {HELPER_START_TIMEOUT:.0f} s. See {log}")


# The portable helper. Plain ASCII (Windows PowerShell 5.1 reads a script without a BOM in
# the ANSI code page); its settings come from a UTF-8 JSON file, so paths may hold any
# character, and the command line carries that file's SHA-256.
PORTABLE_HELPER = r"""# Background Editor - portable update helper
# Copyright (C) 2026 Optimey CommV
# SPDX-License-Identifier: GPL-3.0-or-later
#
# Written by Background Editor when you click Update now in a portable copy, and run once,
# hidden, from a folder next to the app that only you, SYSTEM and Administrators may change.
# The app has already unpacked the new version next to its folder and checked every file
# against the signed release manifest. This helper uses its settings only when their SHA-256
# is the one the app passed, waits until the app has closed, checks the unpacked files again
# (the same set of files, each with its SHA-256 and size), backs up the current program
# files into its own folder, and copies the new ones over them, hashing every file again
# while it copies it, so what lands in the app folder is exactly what was checked. Then it
# starts the app again and removes its folder. The models and data folders, portable.conf,
# custom model and data folders and any junction or link at the top of the app folder are
# never touched, and nothing is written through a junction or link. When anything fails,
# the backup is put back. Everything is logged in the app's data\updates\apply-update.log.
param(
    [Parameter(Mandatory = $true)][string]$Config,
    [Parameter(Mandatory = $true)][string]$ConfigSha256
)

$ErrorActionPreference = 'Stop'
# The settings are read once, as bytes, and used only when they are what the app wrote.
$raw = [System.IO.File]::ReadAllBytes($Config)
$hasher = [System.Security.Cryptography.SHA256]::Create()
$rawHash = -join ($hasher.ComputeHash($raw) | ForEach-Object { $_.ToString('x2') })
$hasher.Dispose()
if ($rawHash -ne $ConfigSha256.ToLowerInvariant()) { exit 4 }
$cfg = (New-Object System.Text.UTF8Encoding($false, $true)).GetString($raw) | ConvertFrom-Json
$utf8 = New-Object System.Text.UTF8Encoding($false)
$reparse = [System.IO.FileAttributes]::ReparsePoint
$helperDir = Split-Path -Parent $Config

function Write-Log([string]$Text) {
    $line = (Get-Date -Format 'yyyy-MM-dd HH:mm:ss') + ' ' + $Text + "`n"
    try { [System.IO.File]::AppendAllText([string]$cfg.log, $line, $utf8) } catch { }
}

function Format-Arg([string]$Arg) {
    # One command-line argument; a path never contains a double quote.
    if ($Arg -ne '' -and $Arg -notmatch '[\s"]') { return $Arg }
    return '"' + ($Arg -replace '(\\+)$', '$1$1') + '"'
}

function Start-Program([object[]]$Command, [string]$WorkDir) {
    # CreateProcess, not ShellExecute: no security prompt for a program on a network drive.
    $psi = New-Object System.Diagnostics.ProcessStartInfo
    $psi.FileName = [string]$Command[0]
    $psi.Arguments = (@($Command | Select-Object -Skip 1) | ForEach-Object { Format-Arg ([string]$_) }) -join ' '
    $psi.UseShellExecute = $false
    $psi.CreateNoWindow = $true
    if ($WorkDir) { $psi.WorkingDirectory = $WorkDir }
    [void][System.Diagnostics.Process]::Start($psi)
}

function Get-Attr([string]$Path) {
    # The attributes of Path itself (a link is not followed), or $null when there is nothing.
    try { return [System.IO.File]::GetAttributes($Path) } catch { return $null }
}

function Get-Tree([string]$Dir) {
    # Everything below Dir, without going into junctions or symbolic links.
    foreach ($item in @(Get-ChildItem -LiteralPath $Dir -Force)) {
        $item
        if ($item.PSIsContainer -and -not ($item.Attributes -band $reparse)) { Get-Tree $item.FullName }
    }
}

function Remove-Link($Item) {
    # Removes a junction or symbolic link itself, never what it points to.
    if ($Item.PSIsContainer) { [System.IO.Directory]::Delete($Item.FullName) }
    else { [System.IO.File]::Delete($Item.FullName) }
}

function Remove-Path([string]$Path) {
    # Removes a file or a folder with everything in it; links are removed, never followed.
    $attr = Get-Attr $Path
    if ($null -eq $attr) { return }
    $item = Get-Item -LiteralPath $Path -Force
    if ($attr -band $reparse) { Remove-Link $item; return }
    if (-not $item.PSIsContainer) {
        $item.Attributes = 'Normal'
        [System.IO.File]::Delete($item.FullName)
        return
    }
    foreach ($x in @(Get-Tree $item.FullName)) {
        if (-not $x.PSIsContainer -and -not ($x.Attributes -band $reparse)) { $x.Attributes = 'Normal' }
    }
    [System.IO.Directory]::Delete($item.FullName, $true)
}

$robocopy = Join-Path $env:SystemRoot 'System32\robocopy.exe'
function Copy-Tree([string]$From, [string]$To, [string]$Mode) {
    # /E copies a folder, /MIR also removes what the source no longer has. /XJ never copies
    # or enters a junction or symbolic link. Exit codes 0-7 are success.
    $out = & $robocopy $From $To $Mode /XJ /R:5 /W:2 /NP /NJH /NJS /NDL /NFL
    $code = $LASTEXITCODE
    if ($code -ge 8) {
        Write-Log ('robocopy: ' + (($out | Out-String).Trim()))
        throw "copying '$From' to '$To' failed (robocopy exit code $code)"
    }
}

function Copy-Items([string]$From, [string]$To, [string]$Mode) {
    # For the backup (/E) and for putting it back (/MIR). The kept folders and every junction
    # or link at the top (such as a custom models folder that is a junction) are skipped.
    foreach ($item in @(Get-ChildItem -LiteralPath $From -Force)) {
        if ($keep -contains $item.Name.ToLowerInvariant()) { continue }
        if ($item.Attributes -band $reparse) { Write-Log ('Left alone (a link): ' + $item.FullName); continue }
        $dest = Join-Path $To $item.Name
        $destAttr = Get-Attr $dest
        if ($null -ne $destAttr -and ($destAttr -band $reparse)) { Write-Log ('Left alone (a link): ' + $dest); continue }
        if ($item.PSIsContainer) {
            if ($Mode -eq '/MIR' -and $null -ne $destAttr) {
                # robocopy /MIR deletes files inside the target of a link it finds in the
                # destination, even with /XJ: remove such links (only the links) first.
                foreach ($x in @(Get-Tree $dest)) { if ($x.Attributes -band $reparse) { Remove-Link $x } }
            }
            Copy-Tree $item.FullName $dest $Mode
        }
        else { Copy-Item -LiteralPath $item.FullName -Destination $dest -Force }
    }
}

$appDir = [string]$cfg.app_dir
$staging = [string]$cfg.staging
$newRoot = Join-Path $staging ([string]$cfg.zip_root)
$backup = [string]$cfg.backup
$keep = @($cfg.keep | ForEach-Object { ([string]$_).ToLowerInvariant() })
$files = @($cfg.files)

function Test-Staging {
    # The unpacked files must be exactly the files of the signed manifest, unchanged.
    if (-not (Test-Path -LiteralPath $staging -PathType Container)) { throw 'the unpacked update is missing' }
    if ((Get-Item -LiteralPath $staging -Force).Attributes -band $reparse) { throw 'the unpacked update is a link' }
    $expected = @{}
    foreach ($f in $files) { $expected[([string]$f.path).Replace('/', '\').ToLowerInvariant()] = $f }
    $prefix = $newRoot.TrimEnd('\') + '\'
    $found = @{}
    foreach ($item in @(Get-Tree $staging)) {
        if ($item.Attributes -band $reparse) { throw ('the unpacked update holds a link: ' + $item.FullName) }
        if ($item.PSIsContainer) { continue }
        if (-not $item.FullName.StartsWith($prefix, [System.StringComparison]::OrdinalIgnoreCase)) {
            throw ('the unpacked update holds an unexpected file: ' + $item.FullName)
        }
        $key = $item.FullName.Substring($prefix.Length).ToLowerInvariant()
        if (-not $expected.ContainsKey($key)) {
            throw ('the unpacked update holds a file the signed release manifest does not list: ' + $key)
        }
        $found[$key] = $item
    }
    foreach ($key in @($expected.Keys)) {
        $f = $expected[$key]
        if (-not $found.ContainsKey($key)) { throw ('a file of the new version is missing: ' + $f.path) }
        $item = $found[$key]
        $hash = (Get-FileHash -LiteralPath $item.FullName -Algorithm SHA256).Hash
        if ($item.Length -ne [long]$f.size -or $hash -ne [string]$f.sha256) {
            throw ('the unpacked ' + $f.path + ' does not match the signed release manifest')
        }
    }
}

function Test-NoLinks {
    # Nothing is written through a junction or symbolic link: no folder on the way to a
    # program file of the new version, nor the file itself, may be one.
    $seen = @{}
    foreach ($f in $files) {
        $parts = ([string]$f.path).Split('/')
        if ($keep -contains $parts[0].ToLowerInvariant()) { continue }
        $path = $appDir
        foreach ($part in $parts) {
            $path = Join-Path $path $part
            $key = $path.ToLowerInvariant()
            if ($seen.ContainsKey($key)) { continue }
            $seen[$key] = $true
            $attr = Get-Attr $path
            if ($null -ne $attr -and ($attr -band $reparse)) {
                throw ('the program folder holds a junction or link, which the update does not write through: ' + $path)
            }
        }
    }
}

function Copy-Verified($Entry, $Created) {
    # Copy one file into the app folder, hashing exactly the bytes that are written.
    $rel = ([string]$Entry.path).Replace('/', '\')
    $src = Join-Path $newRoot $rel
    $dst = Join-Path $appDir $rel
    $top = Join-Path $appDir ($rel.Split('\')[0])
    if ($null -eq (Get-Attr $top)) { $Created.Add($top) }
    [void][System.IO.Directory]::CreateDirectory((Split-Path -Parent $dst))
    $tmp = $dst + '.update-new'
    $sha = [System.Security.Cryptography.SHA256]::Create()
    try {
        $in = New-Object System.IO.FileStream($src, [System.IO.FileMode]::Open, [System.IO.FileAccess]::Read, [System.IO.FileShare]::Read)
        try {
            $out = New-Object System.IO.FileStream($tmp, [System.IO.FileMode]::Create, [System.IO.FileAccess]::Write, [System.IO.FileShare]::None)
            try {
                [long]$total = 0
                while (($n = $in.Read($buffer, 0, $buffer.Length)) -gt 0) {
                    [void]$sha.TransformBlock($buffer, 0, $n, $null, 0)
                    $out.Write($buffer, 0, $n)
                    $total += $n
                }
                [void]$sha.TransformFinalBlock($buffer, 0, 0)
            } finally { $out.Dispose() }
        } finally { $in.Dispose() }
        $hex = -join ($sha.Hash | ForEach-Object { $_.ToString('x2') })
        if ($total -ne [long]$Entry.size -or $hex -ne [string]$Entry.sha256) {
            throw ('the unpacked ' + $Entry.path + ' changed after it was checked')
        }
        [System.IO.File]::SetLastWriteTimeUtc($tmp, [System.IO.File]::GetLastWriteTimeUtc($src))
        $oldAttr = Get-Attr $dst
        if ($null -ne $oldAttr) {
            if (-not ($oldAttr -band $reparse)) { [System.IO.File]::SetAttributes($dst, [System.IO.FileAttributes]::Normal) }
            [System.IO.File]::Delete($dst)
        }
        [System.IO.File]::Move($tmp, $dst)
    } finally {
        $sha.Dispose()
        if ([System.IO.File]::Exists($tmp)) { try { [System.IO.File]::Delete($tmp) } catch { } }
    }
}

function Remove-Stale {
    # As robocopy /MIR per folder: in each top-level folder the new version has, remove what
    # it no longer has. Top-level files and folders it does not have are left alone. Links
    # inside those folders are removed as links, never followed.
    $wanted = @{}
    $wantedDirs = @{}
    $tops = @{}
    foreach ($f in $files) {
        $rel = ([string]$f.path).Replace('/', '\')
        $wanted[$rel.ToLowerInvariant()] = $true
        $parts = $rel.Split('\')
        if ($parts.Count -gt 1 -and -not ($keep -contains $parts[0].ToLowerInvariant())) { $tops[$parts[0]] = $true }
        $d = Split-Path -Parent $rel
        while ($d) { $wantedDirs[$d.ToLowerInvariant()] = $true; $d = Split-Path -Parent $d }
    }
    $prefix = $appDir.TrimEnd('\') + '\'
    foreach ($top in @($tops.Keys)) {
        $dir = Join-Path $appDir $top
        $attr = Get-Attr $dir
        if ($null -eq $attr -or -not ($attr -band [System.IO.FileAttributes]::Directory) -or ($attr -band $reparse)) { continue }
        $items = @(Get-Tree $dir)
        foreach ($item in $items) {
            $isLink = [bool]($item.Attributes -band $reparse)
            if ($item.PSIsContainer -and -not $isLink) { continue }
            $key = $item.FullName.Substring($prefix.Length).ToLowerInvariant()
            if ($wanted.ContainsKey($key) -and -not $isLink) { continue }
            if ($isLink) { Remove-Link $item }
            else {
                $item.Attributes = 'Normal'
                [System.IO.File]::Delete($item.FullName)
            }
        }
        $dirs = @($items | Where-Object { $_.PSIsContainer -and -not ($_.Attributes -band $reparse) } | Sort-Object { $_.FullName.Length } -Descending)
        foreach ($d in $dirs) {
            $key = $d.FullName.Substring($prefix.Length).ToLowerInvariant()
            if (-not $wantedDirs.ContainsKey($key) -and @(Get-ChildItem -LiteralPath $d.FullName -Force).Count -eq 0) {
                [System.IO.Directory]::Delete($d.FullName)
            }
        }
    }
}

# 1. Hold on to the app's process, report in (the app closes only after this line), wait.
$proc = $null
try { $proc = Get-Process -Id ([int]$cfg.wait_pid) -ErrorAction Stop } catch { }
Write-Log ('started ' + $cfg.token)
Write-Log ('Updating ' + $appDir + ' to version ' + $cfg.version + ' from ' + $staging)
if ($proc -ne $null -and -not $proc.WaitForExit([int]$cfg.wait_seconds * 1000)) {
    Write-Log ('The app did not close within ' + $cfg.wait_seconds + ' s; nothing was changed.')
    exit 3
}
Write-Log 'The app has closed.'

$result = 1
$changed = $false
$keepBackup = $false
$created = New-Object System.Collections.Generic.List[string]
$buffer = New-Object byte[] 1048576
try {
    Start-Sleep -Milliseconds 500

    # 2. The unpacked files once more, now that the app no longer runs, and the program
    #    folders: no junction or link on the way to a file of the new version.
    Test-Staging
    Write-Log ('All ' + $files.Count + ' unpacked files match the signed release manifest.')
    Test-NoLinks

    # 3. Back up the current program files, into this helper's private folder (a new folder
    #    there inherits its access rules: only this user, SYSTEM and Administrators).
    if ($null -ne (Get-Attr $backup)) { Remove-Path $backup }
    New-Item -ItemType Directory -Path $backup | Out-Null
    Copy-Items $appDir $backup '/E'
    Write-Log ('Program files backed up in ' + $backup)

    # 4. The new program files over the old ones, each hashed again while it is copied.
    $changed = $true
    foreach ($f in $files) {
        if ($keep -contains ([string]$f.path).Split('/')[0].ToLowerInvariant()) { continue }
        Copy-Verified $f $created
    }
    Remove-Stale
    Write-Log ('Updated to version ' + $cfg.version + '.')
    $result = 0
} catch {
    Write-Log ('The update failed: ' + $_.Exception.Message)
    if ($changed) {
        try {
            Copy-Items $backup $appDir '/MIR'
            foreach ($path in $created) { Remove-Path $path }
            Write-Log 'The previous version was restored.'
        } catch {
            $keepBackup = $true
            Write-Log ('Restoring the previous version failed: ' + $_.Exception.Message + '. The backup stays in ' + $backup)
        }
    }
}

# 5. Tidy up and start the app again (the new version, or the restored one). This helper's
#    folder goes too (PowerShell has read this script already); only a backup that could
#    not be put back stays.
if (Test-Path -LiteralPath $staging) {
    try { Remove-Path $staging } catch { Write-Log ('Could not remove ' + $staging) }
}
try {
    if ($keepBackup) {
        Remove-Path $Config
        Remove-Path $PSCommandPath
    } else {
        Remove-Path $helperDir
    }
} catch { Write-Log ('Could not remove ' + $helperDir) }
try {
    Start-Program -Command @($cfg.relaunch) -WorkDir $appDir
    Write-Log 'Started the app again.'
} catch {
    Write-Log ('Could not start the app again: ' + $_.Exception.Message)
}
exit $result
"""

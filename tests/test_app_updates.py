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

"""Tests for the app update check, the signed release manifest, the download, the signature
pin, the portable update helper and the inert release notes.

pytest is not needed: run with the project's Python,

    .venv\\Scripts\\python.exe tests\\test_app_updates.py [test names]

The GitHub API and the manifest downloads are replaced by recorded answers, and the big
downloads come from a local HTTP server on 127.0.0.1; nothing here talks to GitHub, except
test_live_github when the environment variable BGEDITOR_LIVE_TESTS=1 is set (it only
expects the answer 'no release yet' while the repository does not exist).

Nothing here signs with the Optimey key. Every release manifest and every installer the
tests sign is signed with a throwaway code signing certificate (and its own throwaway CA)
that exists only in the memory of one PowerShell process for the length of the test run:
ephemeral keys, never written to disk and never added to any certificate store. The tests
pass that certificate's SHA-256 as the pin, which the app reads when it checks (never
bound at import). The real Optimey certificate is only used to VERIFY what build.ps1 signed
in dist\\ (those checks are skipped without a signed build). Release manifests are written
with tools/build_tools.py, exactly as the build writes them. The portable helper runs for
real, in %TEMP%.
"""

from __future__ import annotations

import base64
import contextlib
import copy
import hashlib
import http.server
import inspect
import json
import os
import re
import secrets
import shutil
import struct
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")  # the UI tests draw nothing on screen

import bgeditor  # noqa: E402
import build_tools as bt  # noqa: E402
from bgeditor import SIGNER_CERT_SHA256, UPDATE_REPO, app_updates, models, paths, updates  # noqa: E402
from bgeditor.options import Options, threads_text  # noqa: E402

SIGNED_EXE = ROOT / "dist" / "BackgroundEditor" / "BackgroundEditor.exe"
PYTHON = sys.executable
TAG = "v1.2.0"
VERSION = "1.2.0"
SETUP_NAME = f"BackgroundEditor-Setup-{VERSION}.exe"
ZIP_NAME = f"BackgroundEditor-{VERSION}-portable.zip"
MANIFEST_NAME = f"BackgroundEditor-{VERSION}-manifest.ps1"
OLD_SHA1_THUMBPRINT = "DC5A657A60F166815ACDA04E71581F7313553BEA"


class Skip(Exception):
    """The test cannot run here (the reason is printed)."""


# ------------------------------------------------------------------ the throwaway signer
_SIGNER_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
$X = 'System.Security.Cryptography.X509Certificates'
$sha = [System.Security.Cryptography.HashAlgorithmName]::SHA256
$pad = [System.Security.Cryptography.RSASignaturePadding]::Pkcs1
$now = [DateTimeOffset]::Now
# A throwaway CA and code signing certificate for one test run. Both keys are ephemeral CNG
# keys in this process's memory: nothing is written to disk or to any certificate store, and
# both are gone when this process ends.
$caKey = New-Object System.Security.Cryptography.RSACng 2048
$caReq = New-Object "$X.CertificateRequest" -ArgumentList 'CN=Background Editor tests - throwaway CA', $caKey, $sha, $pad
$caReq.CertificateExtensions.Add((New-Object "$X.X509BasicConstraintsExtension" -ArgumentList $true, $true, 0, $true))
$ca = $caReq.CreateSelfSigned($now.AddHours(-1), $now.AddDays(2))
$usage = New-Object System.Security.Cryptography.OidCollection
[void]$usage.Add((New-Object System.Security.Cryptography.Oid '1.3.6.1.5.5.7.3.3'))
$key = New-Object System.Security.Cryptography.RSACng 2048
$req = New-Object "$X.CertificateRequest" -ArgumentList 'CN=Background Editor tests - throwaway signer', $key, $sha, $pad
$req.CertificateExtensions.Add((New-Object "$X.X509EnhancedKeyUsageExtension" -ArgumentList $usage, $false))
$serial = New-Object byte[] 16
[System.Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($serial)
$cert = [System.Security.Cryptography.X509Certificates.RSACertificateExtensions]::CopyWithPrivateKey(
    $req.Create($ca, $now.AddHours(-1), $now.AddDays(1), $serial), $key)
$pin = -join ([System.Security.Cryptography.SHA256]::Create().ComputeHash($cert.RawData) | ForEach-Object { $_.ToString('X2') })
[Console]::Out.WriteLine('pin=' + $pin)
[Console]::Out.Flush()
while ($true) {
    $line = [Console]::In.ReadLine()
    if ($null -eq $line -or $line -eq 'quit') { break }
    try {
        $files = [System.Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($line)).Split([char]10)
        foreach ($f in $files) {
            $s = Set-AuthenticodeSignature -LiteralPath $f -Certificate $cert -IncludeChain Signer -HashAlgorithm SHA256
            if ($null -eq $s.SignerCertificate -or $s.Status -eq 'NotSigned') { throw ('not signed: ' + $f + ' (' + $s.StatusMessage + ')') }
        }
        [Console]::Out.WriteLine('signed')
    } catch {
        [Console]::Out.WriteLine('failed ' + ($_.Exception.Message -replace '\s+', ' '))
    }
    [Console]::Out.Flush()
}
"""


class ThrowawaySigner:
    """Signs files with the throwaway certificate of _SIGNER_SCRIPT, which lives in one
    PowerShell process started on first use and ended by close()."""

    def __init__(self) -> None:
        self.proc: subprocess.Popen | None = None
        self.pin = ""
        self._dir: tempfile.TemporaryDirectory | None = None
        self._broken = ""

    def _start(self) -> None:
        if sys.platform != "win32":
            raise Skip("Authenticode is Windows-only")
        if self._broken:
            raise Skip(self._broken)
        self._dir = tempfile.TemporaryDirectory(prefix="bge-ps-")
        script = Path(self._dir.name) / "throwaway-signer.ps1"
        script.write_text(_SIGNER_SCRIPT, encoding="ascii")
        self.proc = subprocess.Popen(
            [app_updates.powershell_exe(), "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(script)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, encoding="ascii", errors="replace", creationflags=subprocess.CREATE_NO_WINDOW,
        )
        line = self.proc.stdout.readline().strip()
        if not re.fullmatch(r"pin=[0-9A-F]{64}", line):
            self.close()
            self._broken = f"the throwaway signing certificate could not be made ({line[:200]!r})"
            raise Skip(self._broken)
        self.pin = line[4:]
        assert self.pin != SIGNER_CERT_SHA256.upper()

    def sign(self, *files: Path) -> str:
        """Sign the files; returns the throwaway certificate's SHA-256 (the pin to pass)."""
        if self.proc is None:
            self._start()
        payload = base64.b64encode("\n".join(str(f) for f in files).encode("utf-8")).decode("ascii")
        self.proc.stdin.write(payload + "\n")
        self.proc.stdin.flush()
        answer = self.proc.stdout.readline().strip()
        if answer != "signed":
            raise AssertionError(f"signing with the throwaway certificate failed: {answer[:300]!r}")
        return self.pin

    def close(self) -> None:
        proc, self.proc = self.proc, None
        if proc is not None:
            try:
                proc.stdin.write("quit\n")
                proc.stdin.flush()
                proc.wait(15)
            except (OSError, ValueError, subprocess.TimeoutExpired):
                proc.kill()
        if self._dir is not None:
            self._dir.cleanup()
            self._dir = None


SIGNER = ThrowawaySigner()


def sign_files(*files: Path) -> str:
    """Sign with the throwaway certificate; returns its SHA-256 (the pin for the tests)."""
    return SIGNER.sign(*files)


@contextlib.contextmanager
def pinned_to(pin: str):
    """Make the app trust `pin` (it reads bgeditor.SIGNER_CERT_SHA256 when it checks)."""
    saved = bgeditor.SIGNER_CERT_SHA256
    bgeditor.SIGNER_CERT_SHA256 = pin
    try:
        yield
    finally:
        bgeditor.SIGNER_CERT_SHA256 = saved


# ------------------------------------------------------------------ helpers
@contextlib.contextmanager
def temp_app(tmp: Path, portable: bool = True, pin: str | None = None):
    """An app folder in tmp; data (and, installed, the user profile folder) go there too.
    With pin, the app trusts that certificate instead of Optimey's for the duration."""
    saved = (paths.app_dir, paths.user_data_dir, os.environ.get("BGEDITOR_PORTABLE"), bgeditor.SIGNER_CERT_SHA256)
    saved_updates = (app_updates._transport, app_updates._asset_transport, app_updates.self_update_blocker)
    app = tmp / "BackgroundEditor"
    app.mkdir(parents=True, exist_ok=True)
    os.environ["BGEDITOR_PORTABLE"] = "1" if portable else "0"
    paths.app_dir = lambda: app
    paths.user_data_dir = lambda: tmp / "profile"
    paths.is_portable.cache_clear()
    paths.portable_config.cache_clear()
    if pin is not None:
        bgeditor.SIGNER_CERT_SHA256 = pin
    try:
        yield app
    finally:
        paths.app_dir, paths.user_data_dir = saved[0], saved[1]
        if saved[2] is None:
            os.environ.pop("BGEDITOR_PORTABLE", None)
        else:
            os.environ["BGEDITOR_PORTABLE"] = saved[2]
        bgeditor.SIGNER_CERT_SHA256 = saved[3]
        app_updates._transport, app_updates._asset_transport, app_updates.self_update_blocker = saved_updates
        paths.is_portable.cache_clear()
        paths.portable_config.cache_clear()


@contextlib.contextmanager
def frozen():
    """Pretend to be the packaged app (apply_update refuses to replace a source checkout)."""
    saved = getattr(sys, "frozen", None)
    sys.frozen = True
    try:
        yield
    finally:
        if saved is None:
            del sys.frozen
        else:
            sys.frozen = saved


def release(tag: str = TAG, digest: bool = True, manifest: bool = True, **extra) -> dict:
    """A GitHub 'latest release' answer with the assets build.ps1 makes (made-up sizes and
    digests; signed_release() makes ones that fit a signed manifest)."""
    ver = tag.lstrip("v")
    names = [
        f"BackgroundEditor-Setup-{ver}.exe",
        f"BackgroundEditor-{ver}-portable.zip",
        f"BackgroundEditor-{ver}-src.zip",
        f"BackgroundEditor-{ver}-third-party-sources.zip",
        f"BackgroundEditor-{ver}-SHA256SUMS.txt",
    ]
    if manifest:
        names.append(f"BackgroundEditor-{ver}-manifest.ps1")
    assets = [
        {
            "id": i,
            "name": n,
            "size": 1000 + i,
            "browser_download_url": f"https://github.com/{UPDATE_REPO}/releases/download/{tag}/{n}",
            "digest": ("sha256:" + hashlib.sha256(n.encode()).hexdigest()) if digest else None,
        }
        for i, n in enumerate(names)
    ]
    data = {
        "tag_name": tag,
        "name": f"Background Editor {ver}",
        "body": "## What's new\n\n- Faster\n",
        "html_url": f"https://github.com/{UPDATE_REPO}/releases/tag/{tag}",
        "draft": False,
        "prerelease": False,
        "published_at": "2026-10-01T10:00:00Z",
        "assets": assets,
    }
    data.update(extra)
    return data


class FakeGitHub:
    """Stands in for app_updates._transport: answers in order (the last one repeats)."""

    def __init__(self, *answers) -> None:
        self.answers = list(answers)
        self.calls: list[tuple[str, dict]] = []

    def __call__(self, url, method, headers, redirect, limit):
        self.calls.append((url, dict(headers)))
        status, hdrs, body = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
        if not isinstance(body, (bytes, bytearray)):
            body = json.dumps(body).encode("utf-8")
        return updates.Reply(status, {k.lower(): v for k, v in hdrs.items()}, bytes(body), url)


class FakeAssets:
    """Stands in for app_updates._asset_transport: small release assets by URL."""

    def __init__(self) -> None:
        self.files: dict[str, bytes] = {}
        self.calls: list[tuple[str, dict]] = []

    def __call__(self, url, method, headers, redirect, limit):
        self.calls.append((url, dict(headers)))
        data = self.files.get(url)
        if data is None:
            return updates.Reply(404, {}, b"", url)
        if len(data) > limit:
            raise ValueError("larger than expected")
        return updates.Reply(200, {}, data, url)


class FileServer:
    """A local HTTP server with Range support, for the download tests."""

    def __init__(self, files: dict[str, bytes]) -> None:
        server = self
        self.files = files
        self.requests: list[tuple[str, str | None]] = []
        self.agents: list[str] = []

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                data = server.files.get(self.path)
                rng = self.headers.get("Range")
                server.requests.append((self.path, rng))
                server.agents.append(self.headers.get("User-Agent", ""))
                if data is None:
                    self.send_error(404)
                    return
                start = 0
                if rng:
                    start = int(re.fullmatch(r"bytes=(\d+)-", rng).group(1))
                    if start >= len(data):
                        self.send_response(416)
                        self.send_header("Content-Range", f"bytes */{len(data)}")
                        self.end_headers()
                        return
                    self.send_response(206)
                    self.send_header("Content-Range", f"bytes {start}-{len(data) - 1}/{len(data)}")
                else:
                    self.send_response(200)
                self.send_header("Content-Length", str(len(data) - start))
                self.end_headers()
                self.wfile.write(data[start:])

            def log_message(self, *_args):
                pass

        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


# Downloads from 127.0.0.1 must not go through a proxy configured on this PC.
urllib.request.install_opener(urllib.request.build_opener(urllib.request.ProxyHandler({})))


def need_signed_exe() -> bytes:
    """The exe of the signed build in dist (signed by build.ps1; only ever verified here)."""
    if sys.platform != "win32":
        raise Skip("Authenticode is Windows-only")
    if not SIGNED_EXE.is_file():
        raise Skip(f"{SIGNED_EXE} is missing; run build.ps1 first")
    check = app_updates.check_signature(SIGNED_EXE, pinned=SIGNER_CERT_SHA256)
    if not check.ok:
        raise Skip(f"{SIGNED_EXE} is not signed with the pinned certificate ({check.reason})")
    return SIGNED_EXE.read_bytes()


_THROWAWAY_EXE: bytes | None = None


def throwaway_signed_exe(tmp: Path) -> tuple[bytes, str]:
    """The dist exe with its Optimey signature replaced by the throwaway certificate's, and
    that certificate's SHA-256. Made once per run."""
    global _THROWAWAY_EXE
    exe = need_signed_exe()
    if _THROWAWAY_EXE is None:
        path = tmp / "throwaway-signed.exe"
        path.write_bytes(strip_signature(exe))
        sign_files(path)
        _THROWAWAY_EXE = path.read_bytes()
        path.unlink()
    return _THROWAWAY_EXE, SIGNER.pin


def cert_table(data: bytes) -> tuple[int, int, int]:
    """(offset of the security directory entry, certificate table offset, its size)."""
    pe = struct.unpack_from("<I", data, 0x3C)[0]
    magic = struct.unpack_from("<H", data, pe + 24)[0]
    security = pe + 24 + (112 if magic == 0x20B else 96) + 4 * 8
    offset, size = struct.unpack_from("<II", data, security)
    return security, offset, size


def strip_signature(data: bytes) -> bytes:
    """The PE file without its Authenticode signature (the certificate table at the end)."""
    b = bytearray(data)
    security, offset, size = cert_table(data)
    assert offset and offset + size == len(b), "the signature is not at the end of the file"
    del b[offset:]
    struct.pack_into("<II", b, security, 0, 0)
    return bytes(b)


def hide_in_cert_table(data: bytes, extra: bytes) -> bytes:
    """The reviewer's measured trick: bytes appended inside the certificate table, with
    WIN_CERTIFICATE.dwLength and the security directory size extended to cover them."""
    b = bytearray(data)
    security, offset, size = cert_table(data)
    assert offset + size == len(b)
    extra = extra + bytes(-len(extra) % 8)
    length = struct.unpack_from("<I", b, offset)[0]
    struct.pack_into("<I", b, offset, length + len(extra))
    struct.pack_into("<II", b, security, offset, size + len(extra))
    return bytes(b + extra)


def portable_zip(path: Path, exe: bytes, version: str = VERSION, extra: dict | None = None, drop=(), change=None) -> Path:
    """A portable zip like build_tools.py makes, with marker files for the checks."""
    files = {
        "BackgroundEditor/BackgroundEditor.exe": exe,
        "BackgroundEditor/_internal/version.txt": version.encode(),
        "BackgroundEditor/_internal/new.dll": b"new library",
        "BackgroundEditor/_internal/sub/deep.pyd": b"new module",
        "BackgroundEditor/LICENSE": b"new licence",
        "BackgroundEditor/README-portable.txt": b"new readme",
        "BackgroundEditor/portable.conf": b"# the new version's default marker\n",
        "BackgroundEditor/models/README.txt": b"new models readme",
    }
    files.update(extra or {})
    for name in drop:
        files.pop(name)
    if change:
        files.update(change)
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in files.items():
            zf.writestr(name, data)
    return path


def fake_portable_app(app: Path) -> dict[str, bytes]:
    """An installed-looking portable folder of version 1.1.0; returns the files that must survive."""
    files = {
        "BackgroundEditor.exe": b"old exe",
        "_internal/version.txt": b"1.1.0",
        "_internal/old.dll": b"old library",
        "_internal/gone/old.pyd": b"old module in a folder the new version no longer has",
        "licenses/a.txt": b"old notice",
        "LICENSE": b"old licence",
        "notes.txt": b"the user's own notes",
        "models/model.onnx": b"a downloaded model",
        "models/README.txt": b"old models readme",
        "data/settings.ini": b"[General]\noptions=mine\n",
        "portable.conf": b"# mine\nmodels = models\n",
    }
    for rel, content in files.items():
        p = app / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(content)
    return {k: v for k, v in files.items() if k.split("/")[0] in ("models", "data", "portable.conf", "notes.txt")}


def snapshot(app: Path) -> dict[str, bytes]:
    """Every file below app, without following junctions or links."""
    found = {}
    for dirpath, dirnames, filenames in os.walk(app):
        here = Path(dirpath)
        dirnames[:] = [d for d in dirnames if not (here / d).is_junction() and not (here / d).is_symlink()]
        for name in filenames:
            p = here / name
            found[p.relative_to(app).as_posix()] = p.read_bytes()
    return found


def changed_files(before: dict, after: dict) -> set[str]:
    return {k for k in set(before) | set(after) if before.get(k) != after.get(k) and not k.startswith("data/updates/")}


def manifest_text(zip_path: Path, setup: Path | None = None, version: str = VERSION, min_from: str = "1.1.0") -> str:
    return bt.release_manifest_text(version, min_from, zip_path, setup)


def edit_manifest_json(text: str, change) -> str:
    """The manifest text with its JSON data changed by change(data)."""
    head, rest = text.split("= @'\n", 1)
    body, tail = rest.rsplit("\n'@\n", 1)
    data = json.loads(body)
    change(data)
    return f"{head}= @'\n{json.dumps(data, indent=1)}\n'@\n{tail}"


def powershell(script: str, *args: str, timeout: float = 180) -> subprocess.CompletedProcess:
    with tempfile.TemporaryDirectory(prefix="bge-ps-") as d:
        path = Path(d) / "script.ps1"
        path.write_text(script, encoding="utf-8")
        return subprocess.run(
            [app_updates.powershell_exe(), "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(path), *args],
            capture_output=True, text=True, timeout=timeout, creationflags=subprocess.CREATE_NO_WINDOW,
        )


def write_manifest(folder: Path, text: str, name: str = MANIFEST_NAME) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / name
    path.write_bytes(text.encode("ascii"))
    return path


def manifest_url(version: str = VERSION) -> str:
    return f"{app_updates.DOWNLOAD_PREFIX}v{version}/BackgroundEditor-{version}-manifest.ps1"


def signed_release(
    tmp: Path,
    version: str = VERSION,
    *,
    min_from: str = "1.1.0",
    digest: bool = True,
    change=None,
    installer: bool = True,
    sign: bool = True,
) -> tuple[dict, FakeAssets, Path]:
    """A GitHub release answer whose installer, portable zip and manifest belong together:
    the manifest lists the real SHA-256 and size of both, GitHub's sizes and digests are
    theirs, and the manifest is signed with the throwaway certificate (sign=False: not at
    all) and served by the returned FakeAssets. change(data) edits its JSON before signing."""
    folder = tmp / f"release-{version}-{secrets.token_hex(3)}"
    folder.mkdir(parents=True)
    setup = folder / f"BackgroundEditor-Setup-{version}.exe"
    setup.write_bytes(b"MZ setup of version " + version.encode())
    zip_path = portable_zip(folder / f"BackgroundEditor-{version}-portable.zip", b"MZ fake exe", version=version)
    text = manifest_text(zip_path, setup if installer else None, version=version, min_from=min_from)
    if change is not None:
        text = edit_manifest_json(text, change)
    manifest = write_manifest(folder, text, name=f"BackgroundEditor-{version}-manifest.ps1")
    if sign:
        sign_files(manifest)
    data = release(tag="v" + version, digest=digest)
    files = {p.name: p for p in (setup, zip_path, manifest)}
    for asset in data["assets"]:
        p = files.get(asset["name"])
        if p is not None:
            blob = p.read_bytes()
            asset["size"] = len(blob)
            asset["digest"] = ("sha256:" + hashlib.sha256(blob).hexdigest()) if digest else None
    assets = FakeAssets()
    assets.files[manifest_url(version)] = manifest.read_bytes()
    return data, assets, manifest


def serve_manifest(data: dict, assets: FakeAssets, blob: bytes, version: str = VERSION) -> None:
    """Serve other manifest bytes for the release, with GitHub's size and digest fitting them."""
    assets.files[manifest_url(version)] = blob
    for asset in data["assets"]:
        if asset["name"] == f"BackgroundEditor-{version}-manifest.ps1":
            asset["size"] = len(blob)
            if asset["digest"]:
                asset["digest"] = "sha256:" + hashlib.sha256(blob).hexdigest()


def check_now(data: dict, assets: FakeAssets) -> app_updates.AppUpdateReport:
    """Check for updates (as a click does) against this release answer and these assets."""
    app_updates._transport = FakeGitHub((200, {}, data))
    app_updates._asset_transport = assets
    return app_updates.check_for_app_update(force=True)


def report_for(asset_url: str, data: bytes, name: str, manifest: Path, assets: FakeAssets, github_sha: str | None = None,
               version: str = VERSION) -> app_updates.AppUpdateReport:
    """What the check reports for a release with this asset and manifest; the manifest is served by assets."""
    url = manifest_url(version)
    assets.files[url] = manifest.read_bytes()
    return app_updates.AppUpdateReport(
        "available",
        "test",
        current="1.1.0",
        latest=version,
        asset_name=name,
        asset_url=asset_url,
        asset_size=len(data),
        github_sha256=hashlib.sha256(data).hexdigest() if github_sha is None else github_sha,
        manifest_name=f"BackgroundEditor-{version}-manifest.ps1",
        manifest_url=url,
        manifest_size=len(assets.files[url]),
        verified=True,
        installable=True,
        portable=name.endswith(".zip"),
    )


def expect_error(kind, fn, *words):
    try:
        fn()
    except kind as exc:
        for word in words:
            assert word in str(exc), (word, str(exc))
        return exc
    raise AssertionError(f"no {kind.__name__}")


def staged_update(tmp: Path, exe: bytes) -> app_updates.PreparedUpdate:
    """A checked, unpacked portable update in staging_dir(), as download_update leaves it."""
    zip_path = portable_zip(tmp / "fixtures" / ZIP_NAME, exe)
    manifest = app_updates.parse_manifest(manifest_text(zip_path).encode("ascii"), signed=False)
    staging = app_updates._stage_portable(zip_path, manifest.portable, None, lambda *_a: None)
    entry = manifest.portable
    return app_updates.PreparedUpdate(zip_path, VERSION, entry.sha256, entry.size, True, staging, entry.files)


@contextlib.contextmanager
def deny_write(path: Path):
    """Keep path open so that others may read it but not change, replace or delete it."""
    import ctypes
    from ctypes import wintypes

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
                                wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    k32.CreateFileW.restype = wintypes.HANDLE
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    # GENERIC_READ, FILE_SHARE_READ only, OPEN_EXISTING
    handle = k32.CreateFileW(str(path), 0x80000000, 0x1, None, 3, 0, None)
    if handle in (None, wintypes.HANDLE(-1).value):
        raise OSError(ctypes.get_last_error(), f"cannot open {path}")
    try:
        yield
    finally:
        k32.CloseHandle(handle)


def make_junction(link: Path, target: Path) -> None:
    """A directory junction (no administrator rights needed)."""
    import _winapi

    target.mkdir(parents=True, exist_ok=True)
    _winapi.CreateJunction(str(target), str(link))
    assert link.is_junction(), link


def remove_junctions(*links: Path) -> None:
    for link in links:
        if link.is_junction():
            os.rmdir(link)


def private_sddl(folder: Path) -> tuple[str, str]:
    """(the folder's SDDL, this user's SID)."""
    out = powershell("param([string]$P)\n(Get-Acl -LiteralPath $P).Sddl\n"
                     "[Security.Principal.WindowsIdentity]::GetCurrent().User.Value", "-P", str(folder))
    sddl, user = out.stdout.split()
    return sddl, user


def assert_private(folder: Path) -> None:
    """Only this user, SYSTEM and Administrators may change folder (a protected DACL)."""
    sddl, user = private_sddl(folder)
    aces = re.findall(r"\((A|D);[^;]*;([^;]*);;;([^)]+)\)", sddl.split("D:", 1)[1])
    assert "D:P" in sddl and {sid for _k, _r, sid in aces} <= {"SY", "BA", user}, sddl
    assert all(kind == "A" for kind, _r, _sid in aces) and len(aces) == 3, sddl


def run_helper(cmd: list[str], timeout: float = 240) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd,
        env=app_updates.child_environment(),
        stdin=subprocess.DEVNULL,
        capture_output=True,
        timeout=timeout,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )


def wait_for(predicate, seconds: float) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.1)
    return predicate()


def dummy_process(seconds: float = 2.0) -> subprocess.Popen:
    """A harmless process that exits by itself, standing in for the app."""
    return subprocess.Popen([PYTHON, "-c", f"import time; time.sleep({seconds})"], creationflags=subprocess.CREATE_NO_WINDOW)


def relaunch_command(marker: Path) -> list[str]:
    """A harmless 'app start' for the helper: writes marker."""
    return [PYTHON, "-c", "import pathlib, sys; pathlib.Path(sys.argv[1]).write_text('relaunched')", str(marker)]


VERIFIER = [PYTHON, str(ROOT / "app.py"), "--verify-update"]


# ------------------------------------------------------------------ versions and options
def test_version_comparison():
    assert app_updates.tag_version("v1.10.0") == "1.10.0" and app_updates.tag_version("1.2") == "1.2"
    for bad in ("", "latest", "v1.2.0-rc1", "release-1.2", "1.2.0.0.1", None, 12):
        assert app_updates.tag_version(bad) == "", bad
    assert app_updates.is_newer("1.10.0", "1.9.0") and not app_updates.is_newer("1.9.0", "1.10.0")
    assert app_updates.is_newer("v1.1.1", "1.1.0") and app_updates.is_newer("2.0", "1.99.99")
    assert not app_updates.is_newer("1.1", "1.1.0") and not app_updates.is_newer("1.1.0", "1.1.0")
    assert app_updates.is_newer("1.2.0", "1.2.0-rc1") and not app_updates.is_newer("1.2.0-rc1", "1.2.0")
    assert not app_updates.is_newer("not a version", "1.0.0") and app_updates.is_newer("1.0.1", "dev build")


def test_options_and_thread_label():
    assert Options().app_update_check is True
    assert Options.from_dict({"app_update_check": "false"}).app_update_check is False
    assert Options.from_dict({"app_update_check": True}).app_update_check is True
    assert Options.from_dict({"app_update_check": None}).app_update_check is True
    assert threads_text(0) == "automatic threads" and threads_text(1) == "1 thread" and threads_text(8) == "8 threads"


# ------------------------------------------------------------------ the check
def test_asset_choice_installed_and_portable(tmp: Path):
    """A newer release whose signed manifest verifies: the asset for this kind of copy."""
    data, assets, _manifest = signed_release(tmp)
    for portable, name in ((False, SETUP_NAME), (True, ZIP_NAME)):
        with temp_app(tmp / str(portable), portable=portable, pin=SIGNER.pin):
            app_updates.self_update_blocker = lambda: ""  # as in the packaged app on Windows
            r = check_now(data, assets)
            asset = next(a for a in data["assets"] if a["name"] == name)
            assert r.status == "available" and r.verified and r.installable, r
            assert r.latest == VERSION and r.current == app_updates.current_version(), r
            assert r.asset_name == name and r.portable == portable and r.asset_size == asset["size"], r
            assert r.asset_url.endswith("/" + name) and r.github_sha256 == asset["digest"].split(":")[1]
            assert r.manifest_name == MANIFEST_NAME and r.manifest_url == manifest_url(), r
            assert r.html_url.endswith("/releases/tag/" + TAG) and "Faster" in r.notes
            assert app_updates.last_status()["latest"] == VERSION
            # The check downloaded and verified the manifest, with the app update User-Agent.
            agent = f"BackgroundEditor/{app_updates.current_version()} (app update)"
            assert assets.calls[-1] == (manifest_url(), {"User-Agent": agent, "Accept": "application/octet-stream"})
            assert (app_updates.updates_dir() / MANIFEST_NAME).read_bytes() == assets.files[manifest_url()]
    # From source (not frozen) Update now is not offered; the release page of a verified release is.
    with temp_app(tmp / "source", portable=False, pin=SIGNER.pin):
        r = check_now(data, assets)
        assert r.status == "available" and r.verified and not r.installable and "release page" in r.message, r


def test_up_to_date_and_odd_tags(tmp: Path):
    data, assets, _manifest = signed_release(tmp)
    with temp_app(tmp / "app", pin=SIGNER.pin):
        current = app_updates.current_version()
        app_updates._transport = FakeGitHub((200, {}, release(tag="v" + current)))
        r = app_updates.check_for_app_update(force=True)
        assert r.status == "up-to-date" and "newest version" in r.message, r
        app_updates._transport = FakeGitHub((200, {}, release(tag="1.0.0")))
        assert app_updates.check_for_app_update(force=True).status == "up-to-date"
        app_updates._transport = FakeGitHub((200, {}, release(tag="nightly")))
        r = app_updates.check_for_app_update(force=True)
        assert r.status == "skipped" and "not a version number" in r.message, r
        app_updates._transport = FakeGitHub((200, {}, release(prerelease=True)))
        assert app_updates.check_for_app_update(force=True).status == "skipped"
        # Downloads that are not where Optimey publishes them: not a verified release.
        odd = copy.deepcopy(data)
        for asset in odd["assets"][:2]:  # the installer and the portable zip
            asset["browser_download_url"] = "https://example.com/" + asset["name"]
        r = check_now(odd, assets)
        assert r.status == "unverified" and "not where Optimey publishes it" in r.message, r
        assert not r.installable and not r.asset_url and not r.html_url and not r.notes, r
        # A manifest from somewhere else is no manifest.
        odd = copy.deepcopy(data)
        odd["assets"][-1]["browser_download_url"] = "https://example.com/" + MANIFEST_NAME
        r = check_now(odd, assets)
        assert r.status == "unverified" and not r.installable and not r.html_url, r


def test_no_release_is_skipped_and_silent(tmp: Path):
    with temp_app(tmp):
        gh = FakeGitHub((404, {}, {"message": "Not Found"}))
        app_updates._transport = gh
        r = app_updates.check_for_app_update(force=False)
        assert r.status == "skipped" and "No release" in r.message and "error" not in r.message.lower(), r
        assert len(gh.calls) == 1 and gh.calls[0][0] == app_updates.URL_LATEST
        assert gh.calls[0][1]["Accept"] == "application/vnd.github+json" and "User-Agent" in gh.calls[0][1]
        # Asked today: the next startup does not ask again.
        assert not app_updates.is_check_due()
        r = app_updates.check_for_app_update(force=False)
        assert r.status == "skipped" and len(gh.calls) == 1, r
        # A click on Check for updates does.
        app_updates.check_for_app_update(force=True)
        assert len(gh.calls) == 2


def test_daily_schedule(tmp: Path):
    with temp_app(tmp):
        assert app_updates.is_check_due()  # never checked
        app_updates._transport = FakeGitHub((200, {}, release(tag="v0.1")))
        app_updates.check_for_app_update()
        state = json.loads(app_updates.state_path().read_text(encoding="utf-8"))
        last, nxt = updates._parse_time(state["last_check"]), updates._parse_time(state["next_check"])
        assert nxt - last == app_updates.CHECK_INTERVAL and not app_updates.is_check_due()
        # A next_check far in the future (a wrong clock) does not block for weeks.
        state["next_check"] = updates._iso(updates._now() + app_updates.CHECK_INTERVAL * 30)
        app_updates.state_path().write_text(json.dumps(state), encoding="utf-8")
        assert app_updates.is_check_due()


def test_etag_and_not_modified(tmp: Path):
    data, assets, _manifest = signed_release(tmp)
    with temp_app(tmp / "app", pin=SIGNER.pin):
        app_updates.self_update_blocker = lambda: ""
        gh = FakeGitHub((200, {"ETag": 'W/"abc"'}, data), (304, {}, b""))
        app_updates._transport = gh
        app_updates._asset_transport = assets
        first = app_updates.check_for_app_update(force=True)
        second = app_updates.check_for_app_update(force=True)
        assert "If-None-Match" not in gh.calls[0][1]
        assert gh.calls[1][1].get("If-None-Match") == 'W/"abc"'
        assert first.status == second.status == "available" and second.asset_name == first.asset_name
        assert second.notes == first.notes and second.github_sha256 == first.github_sha256
        assert second.manifest_url == first.manifest_url and second.installable
        assert len(assets.calls) == 2  # the manifest is verified again at every check
        # A 404 later forgets the stored release and its ETag.
        app_updates._transport = FakeGitHub((404, {}, {}))
        app_updates.check_for_app_update(force=True)
        gh = FakeGitHub((200, {}, data))
        app_updates._transport = gh
        app_updates.check_for_app_update(force=True)
        assert "If-None-Match" not in gh.calls[0][1]


def test_rate_limit_never_retries(tmp: Path):
    with temp_app(tmp):
        reset = int(time.time()) + 2 * 86400
        gh = FakeGitHub((403, {"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": str(reset)}, {"message": "rate limit"}))
        app_updates._transport = gh
        r = app_updates.check_for_app_update(force=True)
        assert r.status == "skipped" and "rate limit" in r.message and len(gh.calls) == 1, r
        assert not app_updates.is_check_due()
        # Also a click waits for the reset time: no request at all.
        r = app_updates.check_for_app_update(force=True)
        assert r.status == "skipped" and len(gh.calls) == 1, r
        state = json.loads(app_updates.state_path().read_text(encoding="utf-8"))
        assert abs(updates._parse_time(state["blocked_until"]).timestamp() - reset) < 5
        # 429 without a reset time: blocked until the next daily slot.
        with temp_app(tmp / "b"):
            gh = FakeGitHub((429, {}, b""))
            app_updates._transport = gh
            assert app_updates.check_for_app_update(force=True).status == "skipped"
            assert app_updates.check_for_app_update(force=True).status == "skipped" and len(gh.calls) == 1


def test_network_failure_is_reported_not_raised(tmp: Path):
    with temp_app(tmp):
        def broken(*_args):
            raise OSError("no route to host")

        app_updates._transport = broken
        r = app_updates.check_for_app_update(force=True)
        assert r.status == "failed" and "no route to host" in r.message, r
        app_updates._transport = FakeGitHub((500, {}, b"oops"))
        assert app_updates.check_for_app_update(force=True).status == "failed"
        app_updates._transport = FakeGitHub((200, {}, b"<html>not json"))
        assert app_updates.check_for_app_update(force=True).status == "failed"


def test_unverified_release_is_no_update(tmp: Path):
    """A newer release whose manifest is missing or fails any check is not an update: status
    'unverified', no notes, no link, never installable, and the message says not to install
    it. A signed manifest that asks for more (a newer format, a minimum version) is genuine:
    it points to the release page. A manifest that cannot be downloaded is a failed check."""
    data, assets, manifest = signed_release(tmp)
    pin = SIGNER.pin

    def unverified(r, *words):
        assert r.status == "unverified" and not r.verified and not r.installable, r
        assert not r.html_url and not r.notes and not r.asset_url and r.latest == VERSION, r
        assert "not a verified Optimey release" in r.message and "do not install it" in r.message, r.message
        for word in words:
            assert word in r.message, (word, r.message)

    with temp_app(tmp / "app", portable=False, pin=pin):
        app_updates.self_update_blocker = lambda: ""
        folder = app_updates.updates_dir()
        # No manifest at all.
        r = check_now(release(manifest=False), assets)
        unverified(r, "has no signed release manifest")
        expect_error(app_updates.ManifestError, lambda: app_updates.download_update(r), "not a verified Optimey release")
        # Not signed.
        d2, a2, _m = signed_release(tmp, sign=False)
        unverified(check_now(d2, a2), "not signed by Optimey", "it is not signed")
        assert not (folder / MANIFEST_NAME).exists()  # a manifest that fails is not kept
        # Changed after it was signed (GitHub's size and digest fit the changed file).
        d3 = copy.deepcopy(data)
        a3 = FakeAssets()
        serve_manifest(d3, a3, manifest.read_bytes().replace(b'"min_update_from": "1.1.0"', b'"min_update_from": "0.0.1"'))
        unverified(check_now(d3, a3), "changed after it was signed")
        # GitHub publishes another checksum for the manifest than the bytes it serves.
        d4 = copy.deepcopy(data)
        d4["assets"][-1]["digest"] = "sha256:" + "0" * 64
        unverified(check_now(d4, assets), "checksum GitHub publishes")
        # The signed manifest of another version replayed in this release.
        d121, a121, m121 = signed_release(tmp, "1.2.1")
        d5 = copy.deepcopy(data)
        a5 = FakeAssets()
        serve_manifest(d5, a5, m121.read_bytes())
        unverified(check_now(d5, a5), "is for version 1.2.1, not for version 1.2.0")
        # The installer on GitHub is not the one the manifest lists: another size, or the
        # same size with another digest.
        d6 = copy.deepcopy(data)
        d6["assets"][0]["size"] += 1
        unverified(check_now(d6, assets), f"its {SETUP_NAME} on GitHub is not the file in the signed release manifest")
        d7 = copy.deepcopy(data)
        d7["assets"][0]["digest"] = "sha256:" + "1" * 64
        unverified(check_now(d7, assets), "is not the file in the signed release manifest")
        # The manifest cannot be downloaded: the check failed (silent at startup), nothing more.
        r = check_now(data, FakeAssets())
        assert r.status == "failed" and "HTTP 404" in r.message and "tries again tomorrow" in r.message, r
        assert not r.html_url and not r.installable
        # GitHub publishes no digest at all: the manifest's SHA-256 is the gate anyway.
        d8, a8, _m = signed_release(tmp, digest=False)
        r = check_now(d8, a8)
        assert r.status == "available" and r.verified and r.installable and r.github_sha256 == "", r
        # Genuine, but this copy is below its min_update_from: the release page.
        d9, a9, _m = signed_release(tmp, min_from="1.2.0")
        r = check_now(d9, a9)
        assert r.status == "available" and r.verified and not r.installable, r
        assert "only be installed automatically over version 1.2.0" in r.message and "release page" in r.message, r
        # Genuine, of a format this version does not know: the release page.
        d10, a10, _m = signed_release(tmp, change=lambda d: d.update(format=2))
        r = check_now(d10, a10)
        assert r.status == "available" and r.verified and not r.installable and "format 2" in r.message, r
        assert "release page" in r.message and r.html_url, r
        # Genuine, without an installer (build.ps1 -NoInstaller), for an installed copy.
        d11, a11, _m = signed_release(tmp, installer=False)
        r = check_now(d11, a11)
        assert r.status == "available" and r.verified and not r.installable and "without an installer" in r.message, r
    # Signed, but not by the certificate this copy trusts (here: the real Optimey pin).
    with temp_app(tmp / "other-pin", portable=False):
        unverified(check_now(data, assets), "not signed by Optimey", "unknown certificate")


def test_skip_this_version(tmp: Path):
    data, assets, _manifest = signed_release(tmp)
    newer, newer_assets, _m = signed_release(tmp, "1.3.0")
    with temp_app(tmp / "app", pin=SIGNER.pin):
        app_updates._transport = FakeGitHub((200, {}, data))
        app_updates._asset_transport = assets
        app_updates.skip_version(VERSION)
        state = json.loads(app_updates.state_path().read_text(encoding="utf-8"))
        state["next_check"] = ""
        app_updates.state_path().write_text(json.dumps(state), encoding="utf-8")
        r = app_updates.check_for_app_update(force=False)
        assert r.status == "skipped" and "skip" in r.message and not assets.calls, r  # nothing downloaded
        assert app_updates.check_for_app_update(force=True).status == "available"  # a click still shows it
        assert check_now(newer, newer_assets).status == "available"
        assert app_updates.last_status()["skipped_version"] == VERSION


# ------------------------------------------------------------------ the release manifest
def test_manifest_is_data_only_and_parses(tmp: Path):
    """The manifest build_tools.py writes: comments and one here-string; parsed without running it."""
    exe = b"MZ fake exe"
    zip_path = portable_zip(tmp / ZIP_NAME, exe)
    setup = tmp / SETUP_NAME
    setup.write_bytes(b"setup bytes")
    text = manifest_text(zip_path, setup)
    assert text.isascii() and "\r" not in text and text.startswith("# Background Editor release manifest - DATA ONLY")
    assert f"${app_updates.MANIFEST_VARIABLE} = @'\n" in text and text.endswith("\n'@\n")
    m = app_updates.parse_manifest(text.encode("ascii"), signed=False)
    assert m.version == VERSION and m.min_update_from == "1.1.0" and re.fullmatch(r"\d{4}-\d\d-\d\dT.*Z", m.built)
    assert m.setup == app_updates.ManifestAsset(SETUP_NAME, hashlib.sha256(b"setup bytes").hexdigest(), 11)
    assert m.portable.name == ZIP_NAME and m.portable.sha256 == hashlib.sha256(zip_path.read_bytes()).hexdigest()
    listed = {f.path: (f.sha256, f.size) for f in m.portable.files}
    with zipfile.ZipFile(zip_path) as zf:
        in_zip = {n.split("/", 1)[1]: (hashlib.sha256(zf.read(n)).hexdigest(), len(zf.read(n))) for n in zf.namelist()}
    assert listed == in_zip and "BackgroundEditor.exe" in listed
    # Without the installer (build.ps1 -NoInstaller) the manifest says so.
    assert app_updates.parse_manifest(manifest_text(zip_path).encode("ascii"), signed=False).setup is None
    # An unsigned manifest must not carry a signature block, a signed one must.
    expect_error(app_updates.ManifestError, lambda: app_updates.parse_manifest(text.encode("ascii"), signed=True), "signature block")


def test_manifest_refuses_anything_but_data(tmp: Path):
    """Scripts with extra statements, other kinds of strings or odd data are refused before
    any JSON is read (unsigned here; the signed case is in test_manifest_signature)."""
    zip_path = portable_zip(tmp / ZIP_NAME, b"MZ fake exe")
    good = manifest_text(zip_path)
    var = f"${app_updates.MANIFEST_VARIABLE}"
    bad = {
        "a statement after the data": good + "Start-Process calc.exe\n",
        "a statement before the data": good.replace(var, "Remove-Item x\n" + var, 1),
        "a statement on the same line": good.replace(var, "Start-Process calc; " + var, 1),
        "a double-quoted (expanding) here-string": good.replace("= @'\n", '= @"\n', 1).replace("\n'@\n", '\n"@\n'),
        "a second assignment": good + "$x = 1\n",
        "a #Requires line": "#Requires -RunAsAdministrator\n" + good,
        "a block comment": "<# x #>\n" + good,
        "the here-string ended early": edit_manifest_json(good, lambda d: None).replace(
            ' "app":', "'@\nStart-Process calc\n$y = @'\n \"app\":", 1),
        "a byte order mark": "\ufeff" + good,
        "a non-ASCII character": good.replace("DATA ONLY", "DATA \u00d6NLY", 1),
        "another variable": good.replace(var, "$Manifest", 1),
    }
    for why, text in bad.items():
        data = text.encode("utf-8")
        expect_error(app_updates.ManifestError, lambda: app_updates.parse_manifest(data, signed=False), "not valid")

    def path0(value):
        return lambda t: edit_manifest_json(t, lambda d: d["portable"]["files"][0].update(path=value))

    json_bad = {
        "duplicate key": lambda t: t.replace('"version":', '"version": "9.9.9",\n "version":', 1),
        "NaN": lambda t: t.replace('"format": 1', '"format": NaN', 1),
        "no version": lambda t: edit_manifest_json(t, lambda d: d.pop("version")),
        "wrong zip name": lambda t: edit_manifest_json(t, lambda d: d["portable"].update(name="Other.zip")),
        "bad hash": lambda t: edit_manifest_json(t, lambda d: d["portable"]["files"][0].update(sha256="xyz")),
        "a path with ..": path0("../evil.dll"),
        "an absolute path": path0("C:/evil.dll"),
        "a backslash": path0("_internal\\x.dll"),
        "a reserved name": path0("_internal/con.txt"),
        "an 8.3 short name of portable.conf": path0("PORTAB~1.CON"),
        "an 8.3 short name of a folder": path0("_INTER~1/x.dll"),
        "any '~'": path0("_internal/a~b.dll"),
        "a name twice": lambda t: edit_manifest_json(t, lambda d: d["portable"]["files"].append(
            dict(d["portable"]["files"][0], path=d["portable"]["files"][0]["path"].upper()))),
        "no exe": lambda t: edit_manifest_json(t, lambda d: d["portable"].update(
            files=[f for f in d["portable"]["files"] if f["path"] != "BackgroundEditor.exe"])),
        "a size that is a bool": lambda t: edit_manifest_json(t, lambda d: d["portable"].update(size=True)),
    }
    for why, change in json_bad.items():
        data = change(good).encode("ascii")
        try:
            app_updates.parse_manifest(data, signed=False)
        except app_updates.ManifestError:
            continue
        raise AssertionError(f"accepted a manifest with {why}")
    assert "'~'" in app_updates.manifest_path_problem("PORTAB~1.CON")
    newer = edit_manifest_json(good, lambda d: d.update(format=2)).encode("ascii")
    exc = expect_error(app_updates.ManifestFormatError, lambda: app_updates.parse_manifest(newer, signed=False), "format 2")
    assert not isinstance(exc, app_updates.IntegrityError)  # genuine when signed: not a tampering case


def test_manifest_version_rules():
    """The manifest must be for the release's version and newer than this copy (else the
    release is not what Optimey published); a copy below min_update_from installs by hand."""
    def manifest(version, minimum="1.1.0"):
        asset = app_updates.ManifestAsset(f"BackgroundEditor-Setup-{version}.exe", "0" * 64, 1)
        return app_updates.ReleaseManifest(version, "", minimum, asset, None)

    problem = app_updates.manifest_problem
    assert problem(manifest("1.2.0"), "1.2.0", "1.1.0") == ""
    assert problem(manifest("1.2"), "1.2.0", "1.1.0") == ""
    assert "is for version 1.2.1, not for version 1.2.0" in problem(manifest("1.2.1"), "1.2.0", "1.1.0")
    assert "not newer than this copy" in problem(manifest("1.1.0"), "1.1.0", "1.1.0")  # the same version
    assert "not newer than this copy" in problem(manifest("1.0.0"), "1.0.0", "1.1.0")  # a downgrade
    assert problem(manifest("2.0.0", minimum="1.5.0"), "2.0.0", "1.1.0") == ""  # that is minimum_problem's
    minimum = app_updates.minimum_problem
    text = minimum(manifest("2.0.0", minimum="1.5.0"), "1.1.0")
    assert "only be installed automatically over version 1.5.0" in text and "this copy is version 1.1.0" in text, text
    assert minimum(manifest("2.0.0", minimum="1.1.0"), "1.1.0") == ""
    assert minimum(manifest("2.0.0", minimum=""), "0.9") == ""


def test_manifest_signature(tmp: Path):
    """A signed manifest verifies with the pin; changed after signing it does not; signed but
    with an extra statement it is still refused; another pin refuses it."""
    zip_path = portable_zip(tmp / "in" / ZIP_NAME, b"MZ fake exe")
    good = write_manifest(tmp / "good", manifest_text(zip_path))
    extra = write_manifest(tmp / "extra", manifest_text(zip_path) + "Start-Process calc.exe\n")
    unsigned = write_manifest(tmp / "unsigned", manifest_text(zip_path))
    pin = sign_files(good, extra)
    m = app_updates.verify_manifest(good, pinned=pin)
    assert m.version == VERSION and m.portable.name == ZIP_NAME and m.sha256 == hashlib.sha256(good.read_bytes()).hexdigest()
    assert m.min_update_from == "1.1.0"
    # The default pin is read when the check runs, not when the module was imported.
    with pinned_to(pin):
        assert app_updates.verify_manifest(good).version == VERSION
    # The JSON changed after signing: the signature no longer fits.
    data = good.read_bytes()
    tampered = write_manifest(tmp / "tampered", data.replace(b'"min_update_from": "1.1.0"', b'"min_update_from": "0.0.1"').decode("ascii"))
    assert tampered.read_bytes() != data
    expect_error(app_updates.ManifestError, lambda: app_updates.verify_manifest(tampered, pinned=pin), "changed after it was signed")
    # Something appended after the signature block: Windows no longer sees a signature.
    appended = write_manifest(tmp / "appended", data.decode("ascii") + "Start-Process calc.exe\r\n")
    expect_error(app_updates.ManifestError, lambda: app_updates.verify_manifest(appended, pinned=pin), "not signed by Optimey")
    # Signed with the extra statement: the signature is fine, the content is not.
    assert app_updates.check_signature(extra, pinned=pin).ok
    expect_error(app_updates.ManifestError, lambda: app_updates.verify_manifest(extra, pinned=pin), "more than comments")
    expect_error(app_updates.ManifestError, lambda: app_updates.verify_manifest(unsigned, pinned=pin), "not signed")
    # Other pins: the real Optimey certificate (the default here), or the old SHA-1 thumbprint.
    expect_error(app_updates.ManifestError, lambda: app_updates.verify_manifest(good), "unknown certificate")
    expect_error(app_updates.ManifestError, lambda: app_updates.verify_manifest(good, pinned=OLD_SHA1_THUMBPRINT), "unknown certificate")


# ------------------------------------------------------------------ download
def test_download_resumes_checks_and_records(tmp: Path):
    """Installed copy: manifest first, then the installer, resumed, matched against the
    manifest's SHA-256, its signature checked; the downloads are recorded for the cleanup."""
    exe, pin = throwaway_signed_exe(tmp)
    setup = tmp / "fixtures" / SETUP_NAME
    setup.parent.mkdir(parents=True)
    setup.write_bytes(exe)
    manifest = write_manifest(tmp / "fixtures", manifest_text(portable_zip(tmp / "fixtures" / ZIP_NAME, exe), setup))
    unsigned_setup = tmp / "unsigned" / SETUP_NAME
    unsigned_setup.parent.mkdir()
    unsigned_setup.write_bytes(strip_signature(exe))
    manifest_u = write_manifest(tmp / "unsigned", manifest_text(portable_zip(tmp / "unsigned" / ZIP_NAME, exe), unsigned_setup))
    sign_files(manifest, manifest_u)
    server = FileServer({"/" + SETUP_NAME: exe, "/u.exe": unsigned_setup.read_bytes()})
    try:
        with temp_app(tmp / "app", portable=False, pin=pin):
            assets = FakeAssets()
            app_updates._asset_transport = assets
            report = report_for(f"{server.url}/{SETUP_NAME}", exe, SETUP_NAME, manifest, assets)
            folder = app_updates.updates_dir()
            folder.mkdir(parents=True)
            (folder / (SETUP_NAME + ".part")).write_bytes(exe[: len(exe) // 2])  # an interrupted download
            stages = []
            prepared = app_updates.download_update(report, progress=lambda s, d, t: stages.append(s))
            assert prepared.path == folder / SETUP_NAME and prepared.path.read_bytes() == exe and not prepared.portable
            assert prepared.sha256 == hashlib.sha256(exe).hexdigest() and prepared.version == VERSION
            assert server.requests == [("/" + SETUP_NAME, f"bytes={len(exe) // 2}-")], server.requests
            assert stages[0] == "Checking the signed release manifest" and any("Checking the signature" in s for s in stages)
            # The manifest came first, with the app update User-Agent, as did the download.
            agent = f"BackgroundEditor/{app_updates.current_version()} (app update)"
            assert [c[0] for c in assets.calls] == [report.manifest_url] and assets.calls[0][1]["User-Agent"] == agent
            assert server.agents == [agent], server.agents
            assert (folder / MANIFEST_NAME).read_bytes() == manifest.read_bytes()
            recorded = json.loads((folder / app_updates.DOWNLOADS_NAME).read_text(encoding="utf-8"))["downloads"]
            assert {d["name"]: d["sha256"] for d in recorded} == {
                MANIFEST_NAME: hashlib.sha256(manifest.read_bytes()).hexdigest(),
                SETUP_NAME: hashlib.sha256(exe).hexdigest(),
            }, recorded
            # A file downloaded earlier is reused when it still matches.
            again = app_updates.download_update(report)
            assert again.path == prepared.path and len(server.requests) == 1
            # An unsigned installer that matches its (signed) manifest: downloaded, refused afterwards.
            assets2 = FakeAssets()
            app_updates._asset_transport = assets2
            bad = report_for(f"{server.url}/u.exe", unsigned_setup.read_bytes(), SETUP_NAME, manifest_u, assets2)
            expect_error(app_updates.IntegrityError, lambda: app_updates.download_update(bad), "not signed")
    finally:
        server.close()


def test_whole_file_hash_gate_for_the_installer(tmp: Path):
    """The installer must be exactly the one in the signed manifest; GitHub's digest (which
    is whatever was uploaded) does not make a different file acceptable."""
    exe, pin = throwaway_signed_exe(tmp)
    setup = tmp / "fixtures" / SETUP_NAME
    setup.parent.mkdir(parents=True)
    setup.write_bytes(exe)
    zip_path = portable_zip(tmp / "fixtures" / ZIP_NAME, exe)
    manifest = write_manifest(tmp / "fixtures", manifest_text(zip_path, setup))
    wrong_version = tmp / "v121"
    setup121 = wrong_version / "BackgroundEditor-Setup-1.2.1.exe"
    wrong_version.mkdir()
    setup121.write_bytes(exe)
    manifest121 = write_manifest(
        wrong_version, manifest_text(portable_zip(wrong_version / "BackgroundEditor-1.2.1-portable.zip", exe), setup121, version="1.2.1")
    )
    minimum = write_manifest(tmp / "minimum", manifest_text(zip_path, setup, min_from="1.2.0"))
    unsigned = write_manifest(tmp / "unsigned", manifest_text(zip_path, setup))
    sign_files(manifest, manifest121, minimum)
    other = bytearray(exe)
    other[0x2000] ^= 0x01  # another file of the same size (a changed byte)
    server = FileServer({"/" + SETUP_NAME: bytes(other)})
    try:
        with temp_app(tmp / "app", portable=False, pin=pin):
            assets = FakeAssets()
            app_updates._asset_transport = assets
            folder = app_updates.updates_dir()
            url = f"{server.url}/{SETUP_NAME}"
            # GitHub's digest describes the uploaded (other) bytes, the manifest the real ones:
            # refused before the download starts.
            report = report_for(url, bytes(other), SETUP_NAME, manifest, assets)
            expect_error(app_updates.IntegrityError, lambda: app_updates.download_update(report),
                         "not the one in the signed release manifest")
            assert server.requests == []
            # Without a digest from GitHub the download happens, and the manifest's SHA-256 stops it.
            report = report_for(url, bytes(other), SETUP_NAME, manifest, assets, github_sha="")
            expect_error(models.ChecksumMismatch, lambda: app_updates.download_update(report), "checksum mismatch")
            assert not (folder / SETUP_NAME).exists() and not (folder / (SETUP_NAME + ".part")).exists()
            # GitHub offers another size than the manifest names: refused before the download.
            server.requests.clear()
            report = report_for(url, bytes(other) + b"x", SETUP_NAME, manifest, assets, github_sha="")
            expect_error(app_updates.IntegrityError, lambda: app_updates.download_update(report), "signed release manifest says")
            # A manifest for another version than the release (tampering), or one that needs a
            # newer copy (genuine: installed by hand, so not an integrity failure).
            report = report_for(url, exe, SETUP_NAME, manifest121, assets)
            expect_error(app_updates.IntegrityError, lambda: app_updates.download_update(report),
                         "is for version 1.2.1, not for version 1.2.0")
            report = report_for(url, exe, SETUP_NAME, minimum, assets)
            exc = expect_error(app_updates.UpdateError, lambda: app_updates.download_update(report),
                               "only be installed automatically over version 1.2.0", "release page")
            assert not isinstance(exc, app_updates.IntegrityError)
            assert server.requests == [], server.requests
            # A manifest that fails its check is not left behind; one GitHub describes otherwise neither.
            report = report_for(url, exe, SETUP_NAME, unsigned, assets)
            expect_error(app_updates.ManifestError, lambda: app_updates.download_update(report), "not signed by Optimey")
            assert not (folder / MANIFEST_NAME).exists() and server.requests == []
            report = report_for(url, exe, SETUP_NAME, manifest, assets)
            report.manifest_github_sha256 = "0" * 64
            expect_error(app_updates.ManifestError, lambda: app_updates.download_update(report), "checksum GitHub publishes")
    finally:
        server.close()


def test_installer_is_hashed_again_right_before_it_starts(tmp: Path):
    """Verifies the signed dist exe with the real pin (nothing is signed here)."""
    exe = need_signed_exe()
    with temp_app(tmp, portable=False):
        folder = app_updates.updates_dir()
        folder.mkdir(parents=True)
        setup = folder / SETUP_NAME
        setup.write_bytes(exe)
        prepared = app_updates.PreparedUpdate(setup, VERSION, hashlib.sha256(exe).hexdigest(), len(exe), False)
        cmd = app_updates.installer_command(setup)
        for flag in ("/SILENT", "/SP-", "/NOCANCEL", "/CLOSEAPPLICATIONS", "/NORESTART", "/RESTARTAPP=1"):
            assert f" {flag}" in cmd, (flag, cmd)
        assert cmd.startswith(f'"{setup}"') and f'/LOG="{setup.with_suffix(".log")}"' in cmd
        started, write_blocked = [], []

        def fake_start(cmd, cwd, extra=0):
            # While it is started, the installer is held open: nobody can change or replace it.
            try:
                with open(setup, "r+b"):
                    write_blocked.append(False)
            except PermissionError:
                write_blocked.append(True)
            try:
                os.replace(setup, setup.with_name("moved.exe"))
                write_blocked.append(False)
            except OSError:
                write_blocked.append(True)
            started.append((cmd, cwd))

        saved = app_updates._popen_detached
        app_updates._popen_detached = fake_start
        try:
            app_updates.apply_update(prepared)
            assert started == [(cmd, setup.parent)] and write_blocked == [True, True], (started, write_blocked)
            # Changed after download_update checked it (same size): not started.
            changed = bytearray(exe)
            changed[0x2000] ^= 0x01
            setup.write_bytes(changed)
            expect_error(app_updates.IntegrityError, lambda: app_updates.apply_update(prepared), "changed after it was checked")
            setup.write_bytes(exe + b"x")
            expect_error(app_updates.IntegrityError, lambda: app_updates.apply_update(prepared), "changed after it was checked")
            # Only what download_update prepared is started.
            expect_error(app_updates.UpdateError, lambda: app_updates.apply_update(setup), "not an update this app")
            assert len(started) == 1
        finally:
            app_updates._popen_detached = saved
    iss = (ROOT / "installer.iss").read_text(encoding="utf-8")
    assert "Check: RestartAfterUpdate" in iss and "{param:RESTARTAPP|0}" in iss and "skipifsilent" in iss


def test_portable_download_stages_and_checks_every_file(tmp: Path):
    """Portable: the zip must match the manifest as a whole, then every unpacked file must be
    one of the manifest's files with its SHA-256 and size; the exact set, nothing more."""
    exe, pin = throwaway_signed_exe(tmp)
    good_zip = portable_zip(tmp / "good" / ZIP_NAME, exe)
    good_text = manifest_text(good_zip)
    variants = {
        "extra": portable_zip(tmp / "extra" / ZIP_NAME, exe, extra={"BackgroundEditor/_internal/planted.dll": b"evil"}),
        "missing": portable_zip(tmp / "missing" / ZIP_NAME, exe, drop=("BackgroundEditor/LICENSE",)),
        "modified": portable_zip(tmp / "modified" / ZIP_NAME, exe, change={"BackgroundEditor/_internal/new.dll": b"NEW library"}),
    }
    manifests = {"good": write_manifest(tmp / "good", good_text)}
    for key, zip_path in variants.items():
        # The whole-zip values fit the served zip; the file list is the good build's. Only a
        # broken build could publish this; it shows the per-file check on its own.
        def fit(d, z=zip_path):
            d["portable"].update(sha256=hashlib.sha256(z.read_bytes()).hexdigest(), size=z.stat().st_size)
        manifests[key] = write_manifest(tmp / key, edit_manifest_json(good_text, fit))
    sign_files(*manifests.values())
    served = {"/good": good_zip.read_bytes(), **{f"/{k}": z.read_bytes() for k, z in variants.items()}}
    server = FileServer(served)
    try:
        with temp_app(tmp / "app", pin=pin) as app:
            assets = FakeAssets()
            app_updates._asset_transport = assets
            report = report_for(f"{server.url}/good", served["/good"], ZIP_NAME, manifests["good"], assets)
            prepared = app_updates.download_update(report)
            staging = app_updates.staging_dir()
            assert prepared.portable and prepared.staging == staging == app.parent / "BackgroundEditor.update-staging"
            assert app_updates.staging_problem(staging, prepared.files) == ""
            assert (staging / "BackgroundEditor" / "BackgroundEditor.exe").read_bytes() == exe
            assert len(prepared.files) == 8 and server.agents[-1].endswith("(app update)")
            assert_private(staging)  # only this user, SYSTEM and Administrators may change it
            for key, words in (("extra", "1 not listed"), ("missing", "1 missing"), ("modified", "new.dll does not match")):
                bad = report_for(f"{server.url}/{key}", served[f"/{key}"], ZIP_NAME, manifests[key], assets)
                expect_error(app_updates.IntegrityError, lambda: app_updates.download_update(bad), words)
                assert not staging.exists(), key  # nothing unpacked stays behind
            # Another zip of the same size where the manifest describes the good one (and GitHub
            # publishes no digest): stopped by the manifest's SHA-256 of the whole zip.
            server.files["/same-size"] = bytes(len(served["/good"]))
            bad = report_for(f"{server.url}/same-size", server.files["/same-size"], ZIP_NAME, manifests["good"], assets, github_sha="")
            expect_error(models.ChecksumMismatch, lambda: app_updates.download_update(bad), "checksum mismatch")
            assert not staging.exists()
    finally:
        server.close()


def test_portable_staging_is_checked_before_the_helper_starts(tmp: Path):
    """A file changed, added or removed in the staging folder after download_update checked
    it: apply_update refuses before the helper is started (nothing is signed here)."""
    exe = need_signed_exe()
    started = []
    saved = app_updates._popen_detached
    app_updates._popen_detached = lambda *args, **kwargs: started.append(args)
    try:
        for change in ("modified", "extra", "missing", "outside"):
            with temp_app(tmp / change) as app:
                fake_portable_app(app)
                prepared = staged_update(tmp / change, exe)
                root = prepared.staging / "BackgroundEditor"
                if change == "modified":
                    (root / "_internal" / "new.dll").write_bytes(b"NEW library")  # same size
                elif change == "extra":
                    (root / "_internal" / "planted.dll").write_bytes(b"evil")
                elif change == "missing":
                    (root / "LICENSE").unlink()
                else:
                    (prepared.staging / "next-to-the-root.dll").write_bytes(b"evil")
                with frozen():
                    err = expect_error(app_updates.IntegrityError, lambda: app_updates.apply_update(prepared), "was not started")
                assert not started and not app_updates.helper_log().exists(), (change, err)
                assert not prepared.staging.exists()  # the refused files are removed
                assert not app_updates.helper_dir(app).exists()
    finally:
        app_updates._popen_detached = saved


# ------------------------------------------------------------------ the portable helper
def test_portable_helper_end_to_end(tmp: Path):
    """The helper for real: its folder is private, it checks its settings and the staged
    files again, replaces the program files, keeps models, data, portable.conf and the
    user's own files, removes its folder (with the backup) and staging, and starts the 'app'
    again."""
    exe = need_signed_exe()
    with temp_app(tmp) as app:
        keep = fake_portable_app(app)
        prepared = staged_update(tmp, exe)
        marker = tmp / "relaunched.txt"
        proc = dummy_process(2.0)
        cmd = app_updates.write_portable_helper(
            prepared.staging, prepared.files, version=VERSION, app_dir=app, wait_pid=proc.pid,
            relaunch=relaunch_command(marker), token="t0ken",
        )
        helper = app_updates.helper_dir(app)
        assert helper == tmp / "BackgroundEditor.update-helper" and Path(cmd[cmd.index("-File") + 1]).parent == helper
        assert sorted(p.name for p in helper.iterdir()) == ["apply-update.json", "apply-update.ps1"]
        assert_private(helper)
        settings = (helper / "apply-update.json").read_bytes()
        assert cmd[cmd.index("-ConfigSha256") + 1] == hashlib.sha256(settings).hexdigest()
        assert json.loads(settings)["backup"] == str(helper / "backup")
        t0 = time.monotonic()
        result = run_helper(cmd)
        log = app_updates.helper_log().read_text(encoding="utf-8")
        assert result.returncode == 0, (result.returncode, log, result.stderr[-800:])
        assert proc.poll() is not None and time.monotonic() - t0 >= 1.0  # it waited for the process
        assert "started t0ken" in log and "All 8 unpacked files match" in log and f"Updated to version {VERSION}" in log, log
        assert f"Program files backed up in {helper / 'backup'}" in log, log
        assert (app / "BackgroundEditor.exe").read_bytes() == exe
        assert (app / "_internal" / "version.txt").read_text() == VERSION
        assert (app / "_internal" / "sub" / "deep.pyd").is_file() and (app / "_internal" / "new.dll").is_file()
        assert not (app / "_internal" / "old.dll").exists() and not (app / "_internal" / "gone").exists()  # mirrored
        assert (app / "LICENSE").read_text() == "new licence" and (app / "README-portable.txt").is_file()
        assert (app / "licenses" / "a.txt").is_file()  # a top-level folder the new version does not have
        assert not list(app.rglob("*.update-new"))
        for rel, content in keep.items():
            assert (app / rel).read_bytes() == content, rel  # models, data, portable.conf, own files
        assert not helper.exists() and not prepared.staging.exists()  # its folder, backup included
        assert not (tmp / "BackgroundEditor.update-backup").exists()
        assert wait_for(lambda: marker.is_file(), 30) and marker.read_text() == "relaunched"


def test_portable_helper_refuses_changed_settings(tmp: Path):
    """Settings changed after the app wrote them (their SHA-256 is on the command line): the
    helper stops before it reports in, and changes nothing."""
    exe = need_signed_exe()
    with temp_app(tmp) as app:
        fake_portable_app(app)
        before = snapshot(app)
        prepared = staged_update(tmp, exe)
        marker = tmp / "relaunched.txt"
        cmd = app_updates.write_portable_helper(
            prepared.staging, prepared.files, version=VERSION, app_dir=app, wait_pid=dummy_process(0.5).pid,
            relaunch=relaunch_command(marker), token="t3",
        )
        config = app_updates.helper_dir(app) / "apply-update.json"
        settings = json.loads(config.read_bytes())
        settings["relaunch"] = [PYTHON, "-c", "print('not the app')"]
        config.write_bytes(json.dumps(settings, indent=1).encode("utf-8") + b"\n")
        result = run_helper(cmd)
        assert result.returncode == 4, (result.returncode, result.stderr[-500:])
        assert "started t3" not in app_updates._log_text(app_updates.helper_log())
        assert not changed_files(before, snapshot(app)) and prepared.staging.exists() and not marker.exists()


def test_portable_helper_catches_a_swap_after_the_app_checked(tmp: Path):
    """Files changed in the staging folder after the app checked them (while the helper waits
    for the app to close): the helper checks again and changes nothing."""
    exe = need_signed_exe()
    for change in ("modified", "extra"):
        with temp_app(tmp / change) as app:
            fake_portable_app(app)
            before = snapshot(app)
            prepared = staged_update(tmp / change, exe)
            assert app_updates.staging_problem(prepared.staging, prepared.files) == ""  # what the app saw
            marker = tmp / change / "relaunched.txt"
            proc = dummy_process(1.5)
            cmd = app_updates.write_portable_helper(
                prepared.staging, prepared.files, version=VERSION, app_dir=app, wait_pid=proc.pid,
                relaunch=relaunch_command(marker), token="t1",
            )
            root = prepared.staging / "BackgroundEditor"
            if change == "modified":
                (root / "_internal" / "new.dll").write_bytes(b"NEW library")  # swapped, same size
            else:
                (root / "_internal" / "planted.dll").write_bytes(b"evil")
            result = run_helper(cmd)
            log = app_updates.helper_log().read_text(encoding="utf-8")
            assert result.returncode == 1 and "The update failed" in log, log
            assert ("does not match the signed release manifest" if change == "modified" else "does not list") in log, log
            assert "backed up" not in log and "restored" not in log, log  # stopped before anything changed
            assert not changed_files(before, snapshot(app))
            assert not prepared.staging.exists() and not app_updates.helper_dir(app).exists()
            assert wait_for(lambda: marker.is_file(), 30)  # the old version is started again


def test_portable_helper_restores_the_backup(tmp: Path):
    """A file of the old version that cannot be removed makes the copy fail halfway: the
    previous program files come back and the helper's folder (with the backup) is removed."""
    exe = need_signed_exe()
    with temp_app(tmp) as app:
        keep = fake_portable_app(app)
        before = snapshot(app)
        prepared = staged_update(tmp, exe)
        marker = tmp / "relaunched.txt"
        cmd = app_updates.write_portable_helper(
            prepared.staging, prepared.files, version=VERSION, app_dir=app, wait_pid=dummy_process(0.5).pid,
            relaunch=relaunch_command(marker), token="t2",
        )
        with deny_write(app / "_internal" / "version.txt"):  # in use: it cannot be replaced
            result = run_helper(cmd, timeout=300)
        log = app_updates.helper_log().read_text(encoding="utf-8")
        assert result.returncode == 1 and "The update failed" in log and "previous version was restored" in log, log
        assert not changed_files(before, snapshot(app))
        for rel, content in keep.items():
            assert (app / rel).read_bytes() == content, rel
        assert not app_updates.helper_dir(app).exists() and not prepared.staging.exists()
        assert wait_for(lambda: marker.is_file(), 30)


def test_portable_helper_keeps_linked_folders(tmp: Path):
    """A custom models folder that is a junction (portable.conf: models = mymodels), and any
    other junction at the top of the app folder, are kept and never followed: not by the
    backup, not by the copy, not when the backup is put back. A junction inside a program
    folder is removed as a link (its target stays); one on the way to a new file stops the
    update before anything changes."""
    exe = need_signed_exe()
    for case in ("success", "restore", "blocked"):
        base = tmp / case
        with temp_app(base) as app:
            fake_portable_app(app)
            (app / "portable.conf").write_bytes(b"# mine\nmodels = mymodels\n")
            paths.portable_config.cache_clear()
            store, elsewhere, nested = base / "model-store", base / "elsewhere", base / "nested-target"
            for folder, name in ((store, "big-model.onnx"), (elsewhere, "keep.txt"), (nested, "inside.txt")):
                folder.mkdir(parents=True)
                (folder / name).write_bytes(b"not the app's: " + name.encode())
            links = [app / "mymodels", app / "extra-link", app / "_internal" / "linked"]
            make_junction(links[0], store)
            make_junction(links[1], elsewhere)
            make_junction(links[2], nested)
            if case == "blocked":  # the new version writes _internal/sub/deep.pyd through this link
                links.append(app / "_internal" / "sub")
                make_junction(links[-1], base / "sub-target")
            try:
                assert paths.model_dir() == app / "mymodels"
                kept = app_updates._kept_names(app)
                assert "mymodels" in kept and {"models", "data", "portable.conf"} <= set(kept), kept
                targets = {p: snapshot(p) for p in (store, elsewhere, nested)}
                before = snapshot(app)
                prepared = staged_update(base, exe)
                marker = base / "relaunched.txt"
                cmd = app_updates.write_portable_helper(
                    prepared.staging, prepared.files, version=VERSION, app_dir=app, wait_pid=dummy_process(0.5).pid,
                    relaunch=relaunch_command(marker), token=f"t-{case}",
                )
                if case == "restore":
                    with deny_write(app / "_internal" / "version.txt"):
                        result = run_helper(cmd, timeout=300)
                else:
                    result = run_helper(cmd)
                log = app_updates.helper_log().read_text(encoding="utf-8")
                for target, files in targets.items():
                    assert snapshot(target) == files, (case, target, log)  # nothing followed or deleted
                assert links[0].is_junction() and links[1].is_junction(), (case, log)  # top-level links kept
                if case == "success":
                    assert result.returncode == 0 and f"Updated to version {VERSION}" in log, log
                    assert not links[2].exists()  # a link inside a program folder goes, as a link
                    assert (app / "_internal" / "version.txt").read_text() == VERSION
                elif case == "restore":
                    assert result.returncode == 1 and "previous version was restored" in log, log
                    assert not changed_files(before, snapshot(app)), changed_files(before, snapshot(app))
                else:
                    assert result.returncode == 1 and "does not write through" in log and "backed up" not in log, log
                    assert not changed_files(before, snapshot(app)) and links[2].is_junction()
                assert wait_for(lambda: marker.is_file(), 30)
            finally:
                remove_junctions(*links)


def test_portable_helper_files_are_held_until_it_reports_in(tmp: Path):
    """apply_update keeps apply-update.ps1 and its settings open against changes until the
    helper has read them and reported in, and refuses files that changed before that."""
    exe = need_signed_exe()
    with temp_app(tmp) as app:
        fake_portable_app(app)
        helper = app_updates.helper_dir(app)
        seen = []

        class Started:
            def poll(self):
                return None

        def fake_start(cmd, cwd, extra=0):
            for name in ("apply-update.ps1", "apply-update.json"):
                try:
                    with open(helper / name, "r+b"):
                        seen.append((name, "writable"))
                except PermissionError:
                    seen.append((name, "held"))
            token = json.loads((helper / "apply-update.json").read_bytes())["token"]  # reading is allowed
            app_updates.helper_log().write_text(f"started {token}\n", encoding="utf-8")
            return Started()

        saved = app_updates._popen_detached, app_updates.write_portable_helper
        app_updates._popen_detached = fake_start
        try:
            with frozen():
                app_updates.apply_update(staged_update(tmp, exe))
            assert seen == [("apply-update.ps1", "held"), ("apply-update.json", "held")], seen
            # Changed between writing and holding: refused, and the helper's folder is removed.
            writer = saved[1]

            def tampering_writer(*args, **kwargs):
                cmd = writer(*args, **kwargs)
                (helper / "apply-update.json").write_bytes(b"{}\n")
                return cmd

            app_updates.write_portable_helper = tampering_writer
            seen.clear()
            prepared = staged_update(tmp / "again", exe)
            with frozen():
                expect_error(app_updates.IntegrityError, lambda: app_updates.apply_update(prepared), "changed after it was written")
            assert not seen and not helper.exists() and not prepared.staging.exists()
        finally:
            app_updates._popen_detached, app_updates.write_portable_helper = saved


def test_portable_apply_update_waits_for_the_helper(tmp: Path):
    """apply_update for a portable copy: the helper starts hidden, reports in, and the caller
    may quit; the helper then does the update on its own."""
    exe = need_signed_exe()
    saved_writer = app_updates.write_portable_helper
    with temp_app(tmp) as app:
        fake_portable_app(app)
        prepared = staged_update(tmp, exe)
        marker = tmp / "relaunched.txt"
        proc = dummy_process(8.0)

        def writer(*args, **kwargs):  # the real helper, with a stand-in for the app process
            kwargs.update(wait_pid=proc.pid, relaunch=relaunch_command(marker))
            return saved_writer(*args, **kwargs)

        app_updates.write_portable_helper = writer
        try:
            with frozen():
                t0 = time.monotonic()
                app_updates.apply_update(prepared)
                waited = time.monotonic() - t0
        finally:
            app_updates.write_portable_helper = saved_writer
        assert waited < app_updates.HELPER_START_TIMEOUT and proc.poll() is None  # returned while the 'app' still ran
        assert wait_for(lambda: marker.is_file(), 120), app_updates.helper_log().read_text(encoding="utf-8")
        assert (app / "_internal" / "version.txt").read_text() == VERSION
        assert (app / "portable.conf").read_bytes() == b"# mine\nmodels = models\n"
        assert wait_for(lambda: not app_updates.helper_dir(app).exists(), 30)


# ------------------------------------------------------------------ own downloads
def test_cleanup_removes_only_recorded_downloads(tmp: Path):
    with temp_app(tmp):
        folder = app_updates.updates_dir()
        folder.mkdir(parents=True)
        current = app_updates.current_version()

        def put(name: str, data: bytes) -> bytes:
            (folder / name).write_bytes(data)
            return data

        def record(name: str, data: bytes, version: str, size: int | None = None) -> None:
            app_updates._record_download(name, hashlib.sha256(data).hexdigest(), size or len(data), version)

        installed = put(f"BackgroundEditor-Setup-{current}.exe", b"the installer of this version")
        record(f"BackgroundEditor-Setup-{current}.exe", installed, current)
        manifest = put(f"BackgroundEditor-{current}-manifest.ps1", b"its manifest")
        record(f"BackgroundEditor-{current}-manifest.ps1", manifest, current)
        older = put("BackgroundEditor-1.0.0-portable.zip", b"an older zip")
        record("BackgroundEditor-1.0.0-portable.zip", older, "1.0.0")
        put("BackgroundEditor-Setup-1.0.9.exe.part", b"half")
        record("BackgroundEditor-Setup-1.0.9.exe", b"x" * 100, "1.0.9", size=100)
        put("BackgroundEditor-Setup-1.0.5.exe", b"replaced by something else")  # recorded with other content
        record("BackgroundEditor-Setup-1.0.5.exe", b"what was downloaded", "1.0.5")
        newer = put("BackgroundEditor-Setup-9.9.9.exe", b"not installed yet")
        record("BackgroundEditor-Setup-9.9.9.exe", newer, "9.9.9")
        put("BackgroundEditor-Setup-0.9.0.exe", b"never recorded")
        notes = put("notes.txt", b"not an update")
        record("notes.txt", notes, "1.0.0")  # recorded, but not an update file name: never removed
        put("apply-update.log", b"the helper's log")
        removed = app_updates.cleanup_old_downloads()
        assert sorted(removed) == sorted([
            f"BackgroundEditor-Setup-{current}.exe", f"BackgroundEditor-{current}-manifest.ps1",
            "BackgroundEditor-1.0.0-portable.zip", "BackgroundEditor-Setup-1.0.9.exe.part",
        ]), removed
        left = sorted(p.name for p in folder.iterdir())
        assert left == sorted([
            app_updates.DOWNLOADS_NAME, "BackgroundEditor-Setup-1.0.5.exe", "BackgroundEditor-Setup-9.9.9.exe",
            "BackgroundEditor-Setup-0.9.0.exe", "notes.txt", "apply-update.log",
        ]), left
        records = json.loads((folder / app_updates.DOWNLOADS_NAME).read_text(encoding="utf-8"))["downloads"]
        assert [d["name"] for d in records] == ["BackgroundEditor-Setup-9.9.9.exe"], records
        assert app_updates.cleanup_old_downloads() == []


# ------------------------------------------------------------------ Authenticode
def test_signature_verdict_rules():
    pin = SIGNER_CERT_SHA256
    other = "AB" * 32
    v = app_updates.signature_verdict
    assert v(0, pin, 0)[0] and v(0, pin.lower(), 0)[0]
    assert not v(0, other, 0)[0]  # a trusted chain, but another signer
    assert v(app_updates.CERT_E_UNTRUSTEDROOT, pin, 0x20)[0]  # private CA not trusted here: fine with the pin
    assert v(app_updates.CERT_E_CHAINING, pin, 0x10000)[0]  # private CA unknown here: fine with the pin
    assert not v(app_updates.CERT_E_UNTRUSTEDROOT, other, 0x20)[0]
    assert not v(app_updates.CERT_E_CHAINING, "", 0x10000)[0]
    assert not v(app_updates.CERT_E_UNTRUSTEDROOT, pin, 0x20 | 0x8)[0]  # a bad signature in the chain
    assert not v(app_updates.CERT_E_CHAINING, pin, 0x10000 | 0x4)[0]  # revoked
    for code in (app_updates.TRUST_E_NOSIGNATURE, app_updates.TRUST_E_BAD_DIGEST, app_updates.TRUST_E_EXPLICIT_DISTRUST,
                 app_updates.CERT_E_REVOKED, app_updates.CERT_E_EXPIRED, 0x80004005):
        assert not v(code, pin, 0)[0], hex(code)
    assert "changed after it was signed" in v(app_updates.TRUST_E_BAD_DIGEST, pin, 0)[1]
    # A pin that is not a SHA-256 never matches, not even an empty signer.
    assert not v(0, "", 0, pinned="")[0] and not v(0, OLD_SHA1_THUMBPRINT, 0, pinned=OLD_SHA1_THUMBPRINT)[0]
    # The default pin is read at call time.
    with pinned_to(other):
        assert v(0, other, 0)[0] and not v(0, pin, 0)[0]
    assert v(0, pin, 0)[0]


def test_sha256_pin(tmp: Path):
    """The pin is the SHA-256 of the signing certificate's DER encoding, not the SHA-1
    thumbprint, and no check binds it at import (verifies the dist build; signs nothing)."""
    assert re.fullmatch(r"[0-9A-F]{64}", SIGNER_CERT_SHA256) and not hasattr(bgeditor, "SIGNER_THUMBPRINT")
    assert app_updates.signer_pin() == SIGNER_CERT_SHA256 and not hasattr(app_updates, "SIGNER_CERT_SHA256")
    for fn in (app_updates.verify_manifest, app_updates.check_signature, app_updates.signature_verdict):
        assert inspect.signature(fn).parameters["pinned"].default is None, fn.__name__
    need_signed_exe()
    r = app_updates.check_signature(SIGNED_EXE)
    assert r.ok and r.cert_sha256 == SIGNER_CERT_SHA256 and "Optimey" in r.subject, r
    assert not app_updates.check_signature(SIGNED_EXE, pinned=OLD_SHA1_THUMBPRINT).ok
    out = powershell(
        "$c = Get-ChildItem Cert:\\CurrentUser\\My | Where-Object { $_.Thumbprint -eq '" + OLD_SHA1_THUMBPRINT + "' }\n"
        "if ($c) { -join ([Security.Cryptography.SHA256]::Create().ComputeHash($c.RawData) | ForEach-Object { $_.ToString('X2') }) }"
    )
    if out.stdout.strip():  # the certificate is on this PC: the pin is its SHA-256
        assert out.stdout.strip() == SIGNER_CERT_SHA256, out.stdout
    # build.ps1 signs with the certificate that has this SHA-256, read from bgeditor/__init__.py.
    build = (ROOT / "build.ps1").read_text(encoding="utf-8")
    assert "SIGNER_CERT_SHA256" in build and OLD_SHA1_THUMBPRINT not in build
    # The tests never sign with a certificate from a store: their signers make theirs in memory.
    for script in (_SIGNER_SCRIPT, _MAKE_CERTS):
        assert "Cert:" not in script and "Import-" not in script and "X509Store" not in script


def test_signature_of_the_real_build(tmp: Path):
    exe = need_signed_exe()
    ok = app_updates.check_signature(SIGNED_EXE)
    assert ok.ok and ok.code == 0 and ok.cert_sha256 == SIGNER_CERT_SHA256 and "Optimey" in ok.subject, ok
    unsigned = tmp / "unsigned.exe"
    unsigned.write_bytes(strip_signature(exe))
    r = app_updates.check_signature(unsigned)
    assert not r.ok and r.code == app_updates.TRUST_E_NOSIGNATURE and "not signed" in r.reason, r
    for offset in (0x400, 0x2000, len(exe) // 2):  # code, and the archive PyInstaller appends
        changed = bytearray(exe)
        changed[offset] ^= 0x01
        path = tmp / f"changed-{offset}.exe"
        path.write_bytes(changed)
        r = app_updates.check_signature(path)
        assert not r.ok and r.code == app_updates.TRUST_E_BAD_DIGEST, (offset, r)
    assert not app_updates.check_signature(tmp / "missing.exe").ok


def test_pe_certificate_table_holds_only_the_signature(tmp: Path):
    """Authenticode does not hash the certificate table. The reviewer's measured trick (bytes
    appended inside it, with WIN_CERTIFICATE.dwLength and the security directory size
    extended) still passes WinVerifyTrust, but check_signature refuses it. Shown with the
    throwaway certificate, so no file that looks signed by Optimey is made."""
    real = need_signed_exe()
    assert app_updates.pe_signature_problem(SIGNED_EXE) == ""
    _security, _offset, size = cert_table(real)
    assert size % 8 == 0 and real[-1] == 0  # the build's file: the signature plus zero padding
    exe, pin = throwaway_signed_exe(tmp)
    base = tmp / "throwaway.exe"
    base.write_bytes(exe)
    assert app_updates.pe_signature_problem(base) == "" and app_updates.check_signature(base, pinned=pin).ok
    cookie = b"MEI\x0c\x0b\x0a\x0b\x0e"  # the start of PyInstaller's archive cookie
    for name, extra in (("archive", cookie + os.urandom(24_000)), ("random", os.urandom(24_000)), ("zeros", bytes(64))):
        path = tmp / f"hidden-{name}.exe"
        path.write_bytes(hide_in_cert_table(exe, extra))
        code, cert, _subject, _chain = app_updates._win_verify_trust(path)
        assert code == app_updates.CERT_E_CHAINING and cert == pin, (name, hex(code))  # the signature itself passes
        r = app_updates.check_signature(path, pinned=pin)
        assert not r.ok and "holds data after the signature" in r.reason, (name, r)
    # A second WIN_CERTIFICATE after the real one.
    b = bytearray(exe)
    security, offset, size = cert_table(exe)
    second = struct.pack("<IHH", 16, 0x0200, 0x0002) + b"\x30\x06" + bytes(6)
    struct.pack_into("<II", b, security, offset, size + len(second))
    path = tmp / "second-entry.exe"
    path.write_bytes(bytes(b) + second)
    assert not app_updates.check_signature(path, pinned=pin).ok
    # The installer of the build passes as well, when it is there.
    setups = sorted((ROOT / "dist").glob("BackgroundEditor-Setup-*.exe"))
    for setup in setups[-1:]:
        assert app_updates.pe_signature_problem(setup) == "" and app_updates.check_signature(setup).ok, setup


def test_microsoft_signature_fails_the_pin():
    if sys.platform != "win32":
        raise Skip("Authenticode is Windows-only")
    root = Path(os.environ.get("SystemRoot", r"C:\Windows"))
    candidates = [root / "explorer.exe", root / "System32" / "DirectML.dll", root / "HelpPane.exe", root / "System32" / "WindowsCodecs.dll"]
    candidates += sorted((root / "System32").glob("*.exe"))[:200]
    for path in candidates:
        if not path.is_file():
            continue
        r = app_updates.check_signature(path)
        if r.code == 0 and r.cert_sha256:  # an embedded, trusted signature (most system files use catalogs)
            assert not r.ok and "another certificate" in r.reason and r.cert_sha256 != SIGNER_CERT_SHA256, r
            assert app_updates.pe_signature_problem(path) == ""  # a normal signature passes that check
            print(f"     ({path.name}: signed by {r.subject}, refused by the pin)")
            return
    raise Skip("no file with an embedded Microsoft signature found")


_MAKE_CERTS = r"""
param([string]$Dir)
$ErrorActionPreference = 'Stop'
$X = 'System.Security.Cryptography.X509Certificates'
$sha = [System.Security.Cryptography.HashAlgorithmName]::SHA256
$pad = [System.Security.Cryptography.RSASignaturePadding]::Pkcs1
$now = [DateTimeOffset]::Now
$usage = New-Object System.Security.Cryptography.OidCollection
[void]$usage.Add((New-Object System.Security.Cryptography.Oid '1.3.6.1.5.5.7.3.3'))
function New-Request([string]$Name, $Key) { New-Object "$X.CertificateRequest" -ArgumentList $Name, $Key, $sha, $pad }
function Get-Pin($Cert) { -join ([System.Security.Cryptography.SHA256]::Create().ComputeHash($Cert.RawData) | ForEach-Object { $_.ToString('X2') }) }
# In memory only (ephemeral CNG keys); nothing goes into a certificate store.
$caKey = New-Object System.Security.Cryptography.RSACng 2048
$caReq = New-Request 'CN=Background Editor test CA' $caKey
$caReq.CertificateExtensions.Add((New-Object "$X.X509BasicConstraintsExtension" -ArgumentList $true, $true, 0, $true))
$ca = $caReq.CreateSelfSigned($now.AddDays(-1), $now.AddYears(1))
$leafKey = New-Object System.Security.Cryptography.RSACng 2048
$leafReq = New-Request 'CN=Background Editor test signer' $leafKey
$leafReq.CertificateExtensions.Add((New-Object "$X.X509EnhancedKeyUsageExtension" -ArgumentList $usage, $false))
$leaf = [System.Security.Cryptography.X509Certificates.RSACertificateExtensions]::CopyWithPrivateKey(
    $leafReq.Create($ca, $now.AddDays(-1), $now.AddMonths(6), [byte[]](1..8)), $leafKey)
$selfKey = New-Object System.Security.Cryptography.RSACng 2048
$selfReq = New-Request 'CN=Background Editor test self-signed' $selfKey
$selfReq.CertificateExtensions.Add((New-Object "$X.X509EnhancedKeyUsageExtension" -ArgumentList $usage, $false))
$self = $selfReq.CreateSelfSigned($now.AddDays(-1), $now.AddMonths(6))
[void](Set-AuthenticodeSignature -LiteralPath (Join-Path $Dir 'chain.exe') -Certificate $leaf -IncludeChain Signer -HashAlgorithm SHA256)
[void](Set-AuthenticodeSignature -LiteralPath (Join-Path $Dir 'self.exe') -Certificate $self -IncludeChain Signer -HashAlgorithm SHA256)
[void](Set-AuthenticodeSignature -LiteralPath (Join-Path $Dir 'other-manifest.ps1') -Certificate $self -IncludeChain Signer -HashAlgorithm SHA256)
'leaf=' + (Get-Pin $leaf)
'self=' + (Get-Pin $self)
"""


def test_untrusted_root_only_with_the_pin(tmp: Path):
    """What other PCs see: the signer's CA is unknown (partial chain) or not trusted. Signed
    here with certificates made in memory for the test."""
    exe = need_signed_exe()
    unsigned = strip_signature(exe)
    for name in ("chain.exe", "self.exe"):
        (tmp / name).write_bytes(unsigned)
    other_manifest = tmp / "other-manifest.ps1"
    other_manifest.write_text(manifest_text(portable_zip(tmp / "zip" / ZIP_NAME, b"MZ fake")), encoding="ascii", newline="")
    out = powershell(_MAKE_CERTS, "-Dir", str(tmp), timeout=120)
    prints = dict(line.split("=", 1) for line in out.stdout.split() if "=" in line)
    if out.returncode != 0 or set(prints) != {"leaf", "self"}:
        raise Skip(f"test certificates could not be made: {out.stderr.strip()[:200]}")
    chain = app_updates.check_signature(tmp / "chain.exe", pinned=prints["leaf"])
    assert chain.ok and chain.code == app_updates.CERT_E_CHAINING and chain.chain_errors == 0x10000, chain
    selfsigned = app_updates.check_signature(tmp / "self.exe", pinned=prints["self"])
    assert selfsigned.ok and selfsigned.code == app_updates.CERT_E_UNTRUSTEDROOT, selfsigned
    # The same files against the real pin: refused.
    for name in ("chain.exe", "self.exe"):
        r = app_updates.check_signature(tmp / name)
        assert not r.ok and "unknown certificate" in r.reason, r
    # A manifest signed by someone else: refused; with that certificate pinned it would pass.
    expect_error(app_updates.ManifestError, lambda: app_updates.verify_manifest(other_manifest), "unknown certificate")
    assert app_updates.verify_manifest(other_manifest, pinned=prints["self"]).version == VERSION
    # Changed after signing: the hash error wins over the chain, also with the pin.
    changed = bytearray((tmp / "chain.exe").read_bytes())
    changed[0x2000] ^= 0x01
    (tmp / "changed.exe").write_bytes(changed)
    r = app_updates.check_signature(tmp / "changed.exe", pinned=prints["leaf"])
    assert not r.ok and r.code == app_updates.TRUST_E_BAD_DIGEST, r


def test_verify_update_command_line(tmp: Path):
    """app.py --verify-update, which build.ps1 runs with the packaged app on what it signed:
    the real pin accepts the dist build and refuses anything signed by another certificate."""
    exe = need_signed_exe()
    throwaway, _pin = throwaway_signed_exe(tmp)
    (tmp / "unsigned.exe").write_bytes(strip_signature(exe))
    (tmp / "throwaway.exe").write_bytes(throwaway)
    (tmp / "hidden.exe").write_bytes(hide_in_cert_table(throwaway, os.urandom(4096)))
    manifest = write_manifest(tmp / "m", manifest_text(portable_zip(tmp / "zip" / ZIP_NAME, throwaway)))
    sign_files(manifest)
    cases = [
        (SIGNED_EXE, 0, "ok: signed by the Optimey", True),
        (tmp / "unsigned.exe", 1, "refused: it is not signed", False),
        (tmp / "throwaway.exe", 1, "refused: it is signed by an unknown certificate", False),
        (tmp / "hidden.exe", 1, "refused: it is signed by an unknown certificate", False),
        (manifest, 1, "refused: it is signed by an unknown certificate", False),
    ]
    dist_manifest = ROOT / "dist" / f"BackgroundEditor-{bgeditor.__version__}-manifest.ps1"
    if dist_manifest.is_file():  # the build's own manifest, signed by build.ps1
        cases.append((dist_manifest, 0, f"ok: release manifest for version {bgeditor.__version__}", True))
    for path, code, words, signer in cases:
        report = tmp / "report.txt"
        r = subprocess.run(VERIFIER + [str(path), "--report", str(report)], capture_output=True, timeout=120,
                           creationflags=subprocess.CREATE_NO_WINDOW)
        text = report.read_text(encoding="utf-8")
        assert r.returncode == code, (path, r.returncode, text, r.stderr[-500:])
        assert text.startswith(words) and (SIGNER_CERT_SHA256 in text) == signer, text


# ------------------------------------------------------------------ UI
_qapp = None


def qt_app():
    global _qapp
    from PyQt6.QtWidgets import QApplication

    _qapp = QApplication.instance() or QApplication([])
    return _qapp


class _Done:
    """A finished AppUpdateWorker, as the main window sees it."""

    def __init__(self, report=None, manual=False, error="", integrity=False, update=None):
        self.result, self.error, self.manual, self.combined = report, error, manual, manual
        self.integrity, self.report = integrity, update

    def was_cancelled(self):
        return False

    def deleteLater(self):
        pass


class _BoxCloser:
    """Closes modal boxes as they appear and records what they showed."""

    def __init__(self, app, grab=None):
        from PyQt6.QtCore import QTimer

        self.seen: list[dict] = []
        self.grab = grab
        self.timer = QTimer()
        self.timer.timeout.connect(self._close)
        self.timer.start(150)

    def _close(self):
        from PyQt6.QtWidgets import QApplication, QMessageBox

        modal = QApplication.activeModalWidget()
        if modal is None:
            return
        info = {"title": modal.windowTitle()}
        if isinstance(modal, QMessageBox):
            info.update(text=modal.text(), informative=modal.informativeText(), icon=modal.icon(),
                        buttons=sorted(b.text().replace("&", "") for b in modal.buttons()))
        if self.grab is not None:
            info.update(self.grab(modal) or {})
        self.seen.append(info)
        modal.close()

    def stop(self):
        self.timer.stop()


def _close_window(w, app):
    w._closing = True
    w.close()
    w.deleteLater()
    app.processEvents()


def test_startup_check_stays_silent(tmp: Path):
    """At startup only a verified new version shows something (the bar); after a click every
    outcome ends in one summary box, and an unverified release is a warning without a link."""
    from PyQt6.QtWidgets import QMessageBox

    from bgeditor.ui import update_bar

    app = qt_app()
    opened = []
    saved_open = update_bar._open_url
    update_bar._open_url = lambda url: opened.append(url.toString())
    with temp_app(tmp):
        from bgeditor.ui.main_window import MainWindow

        w = MainWindow()
        closer = _BoxCloser(app)
        try:
            unverified = app_updates._unverified(VERSION, "1.1.0", "the release manifest is not signed by Optimey: it is not signed")
            for report in (app_updates.AppUpdateReport("skipped", "No release of Background Editor has been published on GitHub yet."),
                           app_updates.AppUpdateReport("failed", "The check for a new version did not work: offline."),
                           app_updates.AppUpdateReport("up-to-date", "Background Editor 1.1.0 is the newest version."),
                           unverified):
                w._app_checker = _Done(report)
                w._on_app_checker_finished()
                app.processEvents()
                assert not w.update_bar.isVisibleTo(w) and not closer.seen and w.lbl_update.text() == "", (report.status, closer.seen)
            # An unverified report never opens anything, also if it reached the bar's button.
            w._app_report = unverified
            w._on_bar_update()
            w._app_report = None
            report = app_updates.AppUpdateReport("available", "Background Editor 1.2.0 is available (you have 1.1.0).",
                                                 current="1.1.0", latest="1.2.0", verified=True, installable=True)
            w._app_checker = _Done(report)
            w._on_app_checker_finished()
            assert w.update_bar.isVisibleTo(w) and "1.2.0" in w.update_bar.lbl_text.text() and not closer.seen
            # Skip this version hides the bar and remembers the choice.
            w._on_bar_skip()
            assert not w.update_bar.isVisibleTo(w) and app_updates.last_status()["skipped_version"] == "1.2.0"
            # A manual check: one summary box with the app and the model result.
            for report, icon in ((app_updates.AppUpdateReport("skipped", "No release of Background Editor has been published on GitHub yet."),
                                  QMessageBox.Icon.Information),
                                 (unverified, QMessageBox.Icon.Warning)):
                w._manual_check = {"pending": {"app", "models"}}
                w._app_checker = _Done(report, manual=True)
                w._on_app_checker_finished()
                assert not closer.seen and not w.update_bar.isVisibleTo(w)
                w._manual_part_done("models", "No newer model was found.")
                app.processEvents()
                assert [(b["text"], b["icon"], b["buttons"]) for b in closer.seen] == [(report.message, icon, ["OK"])], closer.seen
                assert w.act_check_updates.isEnabled() and w._manual_check is None
                closer.seen.clear()
            assert "not a verified Optimey release" in unverified.message and "do not install it" in unverified.message
            assert not opened, opened
            # The cleanup of old downloads runs in the background and removes nothing unrecorded.
            folder = app_updates.updates_dir()
            folder.mkdir(parents=True, exist_ok=True)
            (folder / "BackgroundEditor-Setup-1.0.0.exe").write_bytes(b"not recorded")
            w._clean_old_update_downloads()
            time.sleep(0.5)
            assert (folder / "BackgroundEditor-Setup-1.0.0.exe").is_file()
        finally:
            closer.stop()
            update_bar._open_url = saved_open
            _close_window(w, app)


def test_update_failure_offers_no_link(tmp: Path):
    """When Update now fails, the box has no release page button; when the release or the
    download is not what Optimey signed, it says so and warns against other sources."""
    from PyQt6.QtWidgets import QMessageBox

    app = qt_app()
    with temp_app(tmp):
        from bgeditor.ui.main_window import MainWindow

        w = MainWindow()
        closer = _BoxCloser(app)
        report = app_updates.AppUpdateReport("available", "", current="1.1.0", latest="1.2.0", verified=True, installable=True,
                                             html_url=f"https://github.com/{UPDATE_REPO}/releases/tag/v1.2.0")
        try:
            for integrity, error in ((True, "The update was not installed: the release manifest is not signed by Optimey: it is not signed."),
                                     (False, "The signed release manifest could not be downloaded (HTTP 503).")):
                w._app_installer = _Done(error=error, integrity=integrity, update=report)
                w._on_installer_finished()
                app.processEvents()
                assert len(closer.seen) == 1, closer.seen
                box = closer.seen.pop()
                assert box["buttons"] == ["Close"], box
                assert "release page" not in (box["text"] + box["informative"]).lower(), box
                assert error in box["informative"], box
                if integrity:
                    assert box["icon"] == QMessageBox.Icon.Critical and "not exactly what Optimey signed" in box["text"], box
                    assert "Do not install version 1.2.0 from any other source" in box["informative"], box
                else:
                    assert box["icon"] == QMessageBox.Icon.Warning and "Optimey signed" not in box["text"], box
        finally:
            closer.stop()
            _close_window(w, app)


def test_release_notes_are_inert(tmp: Path):
    """Release notes come from GitHub, outside the signed manifest. Their images (local, UNC
    and remote), raw HTML, style sheets and links to file:, UNC, http:, javascript:,
    search-ms: or other hosts load nothing and open nothing; only https links on github.com
    open. A plain QTextBrowser, for comparison, does load a local image."""
    from PyQt6.QtCore import QRectF, QUrl
    from PyQt6.QtGui import QImage, QPainter, QTextDocument
    from PyQt6.QtWidgets import QLabel, QTextBrowser

    from bgeditor.ui import update_bar

    app = qt_app()
    probe = tmp / "probe.png"
    image = QImage(37, 23, QImage.Format.Format_ARGB32)
    image.fill(0xFF336699)
    assert image.save(str(probe))
    local = QUrl.fromLocalFile(str(probe)).toString()
    unreachable = "192.0.2.1"  # TEST-NET-1: never routed, so an SMB attempt would hang for seconds
    github = f"https://github.com/{UPDATE_REPO}/releases/tag/v1.2.0"
    notes = (
        "# What's new\n\n"
        f"![local]({local}) ![unc](file://{unreachable}/share/a.png) ![remote](https://example.com/a.png) "
        f"![two slashes](//{unreachable}/share/b.png)\n\n"
        f'<img src="{local}"> <img src="file://{unreachable}/share/c.png"> '
        f'<link rel="stylesheet" href="file://{unreachable}/share/s.css"> '
        f'<table background="file://{unreachable}/share/d.png"><tr><td>x</td></tr></table>\n\n'
        f"[release]({github}) [file](file://{unreachable}/share/setup.exe) [unc](\\\\\\\\{unreachable}\\\\share) "
        f"[smb](smb://{unreachable}/share) [http](http://github.com/x) [js](javascript:alert(1)) "
        f"[search](search-ms:query=x&crumb=location:\\\\\\\\{unreachable}\\\\share) [user](https://evil@github.com/x) "
        f"[port](https://github.com:8443/x) [other](https://example.com/x) <https://evil.example/y> www.example.com\n\n"
        f"| a | b |\n|---|---|\n| ![t]({local}) | [in a table](file://{unreachable}/t) |\n"
    )
    bad_links = [
        f"file://{unreachable}/share/setup.exe", f"\\\\{unreachable}\\share", f"smb://{unreachable}/share",
        "http://github.com/x", "javascript:alert(1)", f"search-ms:query=x&crumb=location:\\\\{unreachable}\\share",
        "https://evil@github.com/x", "https://github.com:8443/x", "https://example.com/x", local, "",
    ]
    opened = []
    saved_open = update_bar._open_url
    update_bar._open_url = lambda url: opened.append(url.toString())
    try:
        # For comparison: a plain QTextBrowser loads the local image by itself.
        plain = QTextBrowser()
        plain.setMarkdown(f"![x]({local})")
        loaded = plain.document().resource(QTextDocument.ResourceType.ImageResource.value, QUrl(local))
        assert loaded is not None and loaded.width() == 37, loaded
        t0 = time.monotonic()
        view = update_bar.ReleaseNotesView(notes)
        view.resize(640, 480)
        view.show()
        app.processEvents()
        doc = view.document()
        painted = QImage(1280, 960, QImage.Format.Format_ARGB32)
        painted.setDevicePixelRatio(2.0)  # as on a high-DPI screen, where Qt looks for @2x images
        painter = QPainter(painted)
        doc.drawContents(painter, QRectF(0, 0, 640, 480))
        painter.end()
        assert time.monotonic() - t0 < 5, "rendering the notes waited for something"
        assert isinstance(doc, update_bar.InertDocument) and doc.requested == [], doc.requested
        assert update_bar._unsafe_fragments(doc) == []  # no image, no link but the github one
        text = doc.toPlainText()
        assert "[image: local]" in text and "[image: unc]" in text and "<img src=" in text, text  # raw HTML is text
        assert "\ufffc" not in text
        # Whatever is asked for, nothing is loaded.
        placeholder = doc.resource(QTextDocument.ResourceType.ImageResource.value, QUrl(local))
        assert placeholder.width() == 1, placeholder
        for url in bad_links:
            view.anchorClicked.emit(QUrl(url))
            assert not update_bar.open_release_link(url) and not update_bar.is_release_link(url), url
        assert not opened, opened
        view.anchorClicked.emit(QUrl(github))
        assert opened == [github], opened
        # Plain text when the notes cannot be made safe; empty notes say so.
        assert "no notes" in update_bar.release_notes_document("").toPlainText()
        # The main window's What's new dialog uses the inert view, and its own link is filtered too.
        with temp_app(tmp / "app"):
            from bgeditor.ui.main_window import MainWindow

            w = MainWindow()

            def grab(modal):
                views = modal.findChildren(update_bar.ReleaseNotesView)
                labels = [lbl for lbl in modal.findChildren(QLabel) if "Release page on GitHub" in lbl.text()]
                return {"views": len(views), "unsafe": sum(len(update_bar._unsafe_fragments(v.document())) for v in views),
                        "external": [lbl.openExternalLinks() for lbl in labels]}

            closer = _BoxCloser(app, grab)
            try:
                report = app_updates.AppUpdateReport("available", "", current="1.1.0", latest="1.2.0", notes=notes,
                                                     html_url=github, verified=True, installable=True)
                w._show_release_notes(report)
                app.processEvents()
                assert [(s["views"], s["unsafe"], s["external"]) for s in closer.seen] == [(1, 0, [False])], closer.seen
            finally:
                closer.stop()
                _close_window(w, app)
    finally:
        update_bar._open_url = saved_open


def test_toolbar_about_and_settings(tmp: Path):
    app = qt_app()
    with temp_app(tmp):
        from PyQt6.QtWidgets import QAbstractSpinBox, QComboBox, QToolBar

        from bgeditor.ui.main_window import MainWindow

        w = MainWindow()
        bar = w.findChild(QToolBar)
        texts = [a.text() for a in bar.actions() if a.text()]
        assert texts[-2:] == ["Check for updates", "About"], texts
        if app.platformName() == "windows":  # the offscreen platform has no Windows theme icons
            assert not w.act_check_updates.icon().isNull()
        panel = w.panel
        assert panel.chk_app_updates.isChecked()
        panel.chk_app_updates.setChecked(False)
        assert w._options.app_update_check is False and not w._app_update_timer.isActive()
        panel.chk_app_updates.setChecked(True)
        assert w._options.app_update_check is True and w._app_update_timer.isActive()
        assert "This is version" in panel.lbl_app_update_status.text()
        guarded = panel.widget().findChildren(QAbstractSpinBox) + panel.widget().findChildren(QComboBox)
        assert len(guarded) >= 10
        from PyQt6.QtCore import Qt

        assert all(x.focusPolicy() == Qt.FocusPolicy.StrongFocus for x in guarded)
        w._options.cpu_threads = 1
        assert w._predicted_device_text().endswith("CPU (1 thread)")
        _close_window(w, app)


def test_wheel_needs_focus(tmp: Path):
    """A spin box without focus ignores the wheel (synthetic events do not propagate in Qt 6,
    so the scrolling of the panel is checked with real mouse wheel messages in the smoke test)."""
    from PyQt6.QtCore import QPoint, QPointF, Qt
    from PyQt6.QtGui import QWheelEvent
    from PyQt6.QtWidgets import QApplication

    app = qt_app()
    with temp_app(tmp):
        from bgeditor.ui.settings_panel import SettingsPanel

        panel = SettingsPanel(Options(), True, settings=paths.settings())
        panel.resize(380, 500)
        panel.show()
        app.processEvents()
        sp = panel.sp_threads

        def wheel():
            c = QPointF(sp.width() / 2, sp.height() / 2)
            ev = QWheelEvent(c, c, QPoint(0, 0), QPoint(0, 120), Qt.MouseButton.NoButton,
                             Qt.KeyboardModifier.NoModifier, Qt.ScrollPhase.NoScrollPhase, False)
            QApplication.sendEvent(sp, ev)
            app.processEvents()

        wheel()
        assert sp.value() == 0 and panel.options().cpu_threads == 0
        sp.hasFocus = lambda: True  # what the guard asks; an offscreen window cannot take focus
        wheel()
        assert sp.value() == 1
        panel.close()
        panel.deleteLater()
        app.processEvents()


# ------------------------------------------------------------------ live (optional)
def test_release_text_stays_plain():
    """Tags, links and file names that anyone with release rights controls stay harmless."""
    from PyQt6.QtCore import Qt
    from PyQt6.QtWidgets import QLabel, QMessageBox

    from bgeditor.ui import main_window, update_bar

    # A tag is shown only with safe characters, so it can never become rich text.
    tag = "<img src=&#92;&#92;192.0.2.1&#92;s&#92;a.png>"
    shown = app_updates.printable_tag(tag)
    assert "<" not in shown and "&" not in shown and "\\" not in shown, shown
    assert app_updates.printable_tag("v1.2.0") == "v1.2.0"

    # Message boxes are plain text, including their informative text.
    qt_app()  # keeps the QApplication alive for the widgets below
    box = QMessageBox(QMessageBox.Icon.Warning, "t", "<b>x</b>")
    box.setInformativeText("<img src=x.png>")
    main_window._plain_text(box)
    assert box.textFormat() == Qt.TextFormat.PlainText
    assert all(label.textFormat() == Qt.TextFormat.PlainText for label in box.findChildren(QLabel))

    # Release-note links open only inside this repository.
    ok = f"https://github.com/{UPDATE_REPO}/releases/tag/v1.2.0"
    assert update_bar.is_release_link(ok)
    assert update_bar.is_release_link(f"https://GitHub.com/{UPDATE_REPO}")
    for bad in (
        "https://github.com/someone-else/trojan/releases/download/v1/BackgroundEditor-Setup-1.3.0.exe",
        "https://github.com/login/oauth/authorize?client_id=x",
        f"https://github.com/{UPDATE_REPO}-evil/releases",
        f"https://github.com/{UPDATE_REPO}/../../someone-else/x",
        f"https://github.com/{UPDATE_REPO}/%2e%2e/%2e%2e/someone-else/x",
        f"http://github.com/{UPDATE_REPO}/releases",
    ):
        assert not update_bar.is_release_link(bad), bad

    # Windows device names in every spelling are refused as manifest paths.
    for bad in ("_internal/COM¹.dll", "_internal/lpt²", "_internal/CON .txt", "nul.txt"):
        assert app_updates.manifest_path_problem(bad), bad
    assert app_updates.manifest_path_problem("_internal/python314.dll") == ""


def test_live_github():
    if os.environ.get("BGEDITOR_LIVE_TESTS") != "1":
        raise Skip("set BGEDITOR_LIVE_TESTS=1 to ask the real GitHub API once")
    with tempfile.TemporaryDirectory(prefix="bge-live-") as d, temp_app(Path(d)):
        r = app_updates.check_for_app_update(force=True)
        print(f"     (GitHub says: {r.status}: {r.message})")
        assert r.status in ("skipped", "up-to-date", "available", "unverified"), r


# ------------------------------------------------------------------ runner
def main() -> int:
    only = set(sys.argv[1:])
    tests = [(name, fn) for name, fn in globals().items() if name.startswith("test_") and callable(fn)]
    if only:
        tests = [(n, f) for n, f in tests if n in only or n.removeprefix("test_") in only]
    failed = skipped = 0
    try:
        for name, fn in tests:
            t0 = time.perf_counter()
            try:
                if fn.__code__.co_argcount:
                    with tempfile.TemporaryDirectory(prefix="bge-test-", ignore_cleanup_errors=True) as d:
                        fn(Path(d))
                else:
                    fn()
            except Skip as exc:
                skipped += 1
                print(f"skip {name}: {exc}")
            except Exception:
                failed += 1
                print(f"FAIL {name}")
                traceback.print_exc()
            else:
                print(f"ok   {name} ({time.perf_counter() - t0:.1f} s)")
    finally:
        SIGNER.close()  # the throwaway certificate and its key end with this process
    print(f"{len(tests) - failed - skipped} passed, {skipped} skipped, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())

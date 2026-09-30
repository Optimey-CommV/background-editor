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

"""Build helpers for build.ps1. Run them with the project venv's python.

Only the standard library is used, plus pefile and PyInstaller (both build requirements)
for the checks on the packaged app.

    source-zip         zip the exact source tree the build uses (Corresponding Source)
    verify-source-zip  after the build: fail when the tree no longer matches that zip
    third-party        download (once, cached, SHA-256 verified) and zip the third-party
                       sources listed in tools/third_party_sources.json
    qt-notices         the notices of the third-party code inside the Qt libraries, taken
                       from the qt_attribution.json files of those exact Qt sources
    check-bundle      guards on the packaged app folder; also works out the minimum
                       Microsoft Visual C++ runtime version the bundled binaries need
    portable           the portable zip: app folder + portable.conf + models/README.txt
    release-manifest   the release manifest (BackgroundEditor-<ver>-manifest.ps1): the
                       SHA-256 and size of the installer, of the portable zip and of every
                       file in it, as data in a PowerShell file that build.ps1 then signs
    verify-manifest    after signing: check the manifest's signature (with the app's own
                       code and pin) and its contents against the built files

Every command exits with status 1 and a readable message when something is wrong.
"""

from __future__ import annotations

import argparse
import ast
import fnmatch
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
APP_NAME = "BackgroundEditor"

# The tree a build is made from: the files of the repository. Everything else in the
# project folder (.venv, bench, dist, build output, caches) is not part of the source.
SOURCE_FILES = [
    "app.py",
    "BackgroundEditor.spec",
    "build.ps1",
    "installer.iss",
    "requirements.txt",
    "README.md",
    "LICENSE",
    "LICENSES.txt",
    "LICENSES.txt.license",
    ".gitignore",
    ".gitattributes",
    ".gitleaks.toml",
    ".pre-commit-config.yaml",
]
SOURCE_DIRS = ["bgeditor", "assets", "tests", "tools", "docs", "LICENSES", ".github"]
SKIP_DIR_NAMES = {"__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", ".git"}
SKIP_SUFFIXES = {".pyc", ".pyo", ".part"}
SKIP_FILE_NAMES = {"thumbs.db", "desktop.ini", ".ds_store"}

# SHA-256 of the verbatim licence texts (https://www.gnu.org/licenses/gpl-3.0.txt and
# lgpl-3.0.txt), so a build fails when LICENSE or the LGPL copy was edited by accident.
GPL3_SHA256 = "3972dc9744f6499f0f9b2dbf76696f2ae7ad8af9b23dde66d6af86c9dfb36986"
LGPL3_SHA256 = "e3a994d82e644b03a792a930f574002658412f62407f5fee083f2555c5f23118"

USER_AGENT = "BackgroundEditor-build (python-urllib)"
CHUNK = 1 << 20


class BuildError(Exception):
    pass


def log(text: str = "") -> None:
    print(text, flush=True)


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(CHUNK), b""):
            h.update(chunk)
    return h.hexdigest()


def _zip_time(path: Path) -> tuple[int, int, int, int, int, int]:
    t = time.localtime(max(path.stat().st_mtime, 315532800))  # zip cannot store dates before 1980
    return t[:6]


def _add_file(zf: zipfile.ZipFile, path: Path, arcname: str, compress: int) -> None:
    """Add a file with its own time stamp, in chunks (the archives can be large)."""
    info = zipfile.ZipInfo(arcname, date_time=_zip_time(path))
    info.compress_type = compress
    info.external_attr = 0o644 << 16
    with open(path, "rb") as src, zf.open(info, "w", force_zip64=True) as dst:
        for chunk in iter(lambda: src.read(CHUNK), b""):
            dst.write(chunk)


def _add_text(zf: zipfile.ZipFile, arcname: str, text: str) -> None:
    info = zipfile.ZipInfo(arcname, date_time=time.localtime()[:6])
    info.compress_type = zipfile.ZIP_DEFLATED
    info.external_attr = 0o644 << 16
    zf.writestr(info, text.encode("utf-8"))


def _finish(part: Path, out: Path) -> None:
    with zipfile.ZipFile(part) as zf:
        bad = zf.testzip()
    if bad:
        raise BuildError(f"{part.name}: entry {bad} is corrupt")
    os.replace(part, out)


# ------------------------------------------------------------------- source zip
def source_files(root: Path = ROOT) -> list[tuple[str, Path]]:
    """(relative posix path, absolute path) of every file of the source tree, sorted."""
    files: list[tuple[str, Path]] = []
    for name in SOURCE_FILES:
        path = root / name
        if not path.is_file():
            raise BuildError(f"Source file missing: {path}")
        files.append((name, path))
    for top in SOURCE_DIRS:
        base = root / top
        if not base.is_dir():
            raise BuildError(f"Source folder missing: {base}")
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIR_NAMES)
            for fname in sorted(filenames):
                if Path(fname).suffix.lower() in SKIP_SUFFIXES or fname.lower() in SKIP_FILE_NAMES:
                    continue
                path = Path(dirpath) / fname
                files.append((path.relative_to(root).as_posix(), path))
    _check_against_git(root, [rel for rel, _ in files])
    return sorted(files)


def _check_against_git(root: Path, selected: list[str]) -> None:
    """In a git work tree, the source zip must hold exactly the tracked files.

    A file git does not track (untracked, or ignored on purpose such as a *.pfx or .env)
    would otherwise go into the public source zip; a tracked file outside SOURCE_FILES and
    SOURCE_DIRS would be missing from it. Outside a work tree (a build from an unpacked
    source zip) there is nothing to compare with.
    """
    if not (root / ".git").exists():
        return
    try:
        run = subprocess.run(["git", "-C", str(root), "ls-files", "-z", "--cached"], capture_output=True, timeout=60)
    except (OSError, subprocess.SubprocessError) as exc:
        raise BuildError(f"{root} is a git work tree, but 'git ls-files' failed: {exc}") from exc
    if run.returncode != 0:
        detail = run.stderr.decode("utf-8", "replace").strip()
        raise BuildError(f"{root} is a git work tree, but 'git ls-files' failed:\n{detail}")
    tracked = {p for p in run.stdout.decode("utf-8").split("\0") if p}
    stray = sorted(set(selected) - tracked)
    left_out = sorted(p for p in tracked - set(selected) if (root / p).is_file())
    if stray or left_out:
        lines = [f"  not tracked by git: {p}" for p in stray]
        lines += [f"  tracked, but not in SOURCE_FILES or SOURCE_DIRS: {p}" for p in left_out]
        raise BuildError(
            "The source zip must hold exactly the files of the repository. Add new files with "
            "'git add', move files that must stay private out of the source folders, and list "
            "new top-level files in tools/build_tools.py:\n" + "\n".join(lines)
        )


def cmd_source_zip(args: argparse.Namespace) -> None:
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    prefix = f"{APP_NAME}-{args.version}-src/"
    files = source_files()
    part = out.with_name(out.name + ".part")
    total = 0
    with zipfile.ZipFile(part, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        for rel, path in files:
            _add_file(zf, path, prefix + rel, zipfile.ZIP_DEFLATED)
            total += path.stat().st_size
    _finish(part, out)
    log(f"   {out.name}: {len(files)} files, {total / 1e6:.1f} MB unpacked, {out.stat().st_size / 1e6:.1f} MB zipped")
    if total > 50e6:
        log("   WARNING: the source tree is larger than 50 MB; check for stray large files")
    differ = differs_from_head(ROOT, files)
    if differ:
        shown = ", ".join(differ[:5]) + (f" and {len(differ) - 5} more" if len(differ) > 5 else "")
        log(f"   WARNING: {len(differ)} file(s) differ byte for byte from the last commit ({shown}); "
            "build a release from a clean, committed tree")


def differs_from_head(root: Path, files: list[tuple[str, Path]]) -> list[str]:
    """Files whose bytes on disk are not the blob in HEAD (uncommitted changes, other line
    endings); [] outside a git work tree. Only a clean tree makes the source zip the commit."""
    if not (root / ".git").exists():
        return []
    try:
        run = subprocess.run(["git", "-C", str(root), "ls-tree", "-r", "-z", "HEAD"], capture_output=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        return [rel for rel, _ in files]
    head: dict[str, str] = {}
    if run.returncode == 0:
        for entry in run.stdout.decode("utf-8").split("\0"):
            if "\t" in entry:
                meta, rel = entry.split("\t", 1)
                head[rel] = meta.split()[2]
    differ = []
    for rel, path in files:
        blob = head.get(rel)
        data = path.read_bytes()
        algo = hashlib.sha256 if blob and len(blob) == 64 else hashlib.sha1
        if blob != algo(b"blob %d\0" % len(data) + data).hexdigest():
            differ.append(rel)
    return differ


def cmd_verify_source_zip(args: argparse.Namespace) -> None:
    """The zip must hold exactly the files the build used, byte for byte."""
    zpath = Path(args.zip)
    with zipfile.ZipFile(zpath) as zf:
        names = [n for n in zf.namelist() if not n.endswith("/")]
        prefix = names[0].split("/", 1)[0] + "/" if names else ""
        in_zip = {}
        for n in names:
            in_zip[n[len(prefix):]] = hashlib.sha256(zf.read(n)).hexdigest()
    now = {rel: sha256_file(path) for rel, path in source_files()}
    added = sorted(set(now) - set(in_zip))
    removed = sorted(set(in_zip) - set(now))
    changed = sorted(rel for rel in set(now) & set(in_zip) if now[rel] != in_zip[rel])
    if added or removed or changed:
        lines = [f"  added:   {r}" for r in added] + [f"  removed: {r}" for r in removed]
        lines += [f"  changed: {r}" for r in changed]
        raise BuildError(
            "The source tree changed while the app was being built, so the source zip no longer "
            "matches the build. Build again once nobody is editing:\n" + "\n".join(lines)
        )
    log(f"   {zpath.name} matches the source tree ({len(now)} files)")


# ------------------------------------------------------------ third-party sources
def load_manifest(path: Path) -> dict:
    with open(path, encoding="utf-8") as fh:
        manifest = json.load(fh)
    seen = set()
    for entry in manifest["archives"]:
        for key in ("name", "file", "url", "sha256", "size", "licence", "covers"):
            if key not in entry:
                raise BuildError(f"{path.name}: '{entry.get('name', '?')}' has no '{key}'")
        if entry["file"] in seen or "/" in entry["file"] or "\\" in entry["file"]:
            raise BuildError(f"{path.name}: bad or duplicate file name {entry['file']!r}")
        seen.add(entry["file"])
    return manifest


def _download(url: str, part: Path, expected_size: int | None) -> None:
    last_error: Exception | None = None
    for attempt in range(1, 4):
        try:
            request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(request, timeout=60) as response, open(part, "wb") as fh:
                total = int(response.headers.get("Content-Length") or 0) or expected_size or 0
                done, shown = 0, -1
                for chunk in iter(lambda: response.read(CHUNK), b""):
                    fh.write(chunk)
                    done += len(chunk)
                    step = int(done * 10 / total) if total else -1
                    if step != shown and step >= 0:
                        shown = step
                        log(f"      {done / 1e6:8.1f} MB of {total / 1e6:.1f} MB")
            return
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as exc:
            last_error = exc
            log(f"      attempt {attempt} failed: {exc}")
            time.sleep(3 * attempt)
    raise BuildError(f"Download failed: {url}: {last_error}")


def _check_archive(path: Path, entry: dict) -> str | None:
    """None when the file is the pinned archive, otherwise what is wrong with it."""
    size = path.stat().st_size
    if entry["size"] and size != entry["size"]:
        return f"size {size} instead of {entry['size']}"
    digest = sha256_file(path)
    if entry["sha256"] and digest != entry["sha256"]:
        return f"SHA-256 {digest} instead of {entry['sha256']}"
    for algo in ("sha1", "md5"):  # extra checks against checksums published upstream
        if entry.get(algo):
            h = hashlib.new(algo)
            with open(path, "rb") as fh:
                for chunk in iter(lambda: fh.read(CHUNK), b""):
                    h.update(chunk)
            if h.hexdigest() != entry[algo]:
                return f"{algo.upper()} {h.hexdigest()} instead of {entry[algo]}"
    return None


def fetch_archive(entry: dict, cache: Path, allow_unpinned: bool = False) -> Path:
    """The archive from the cache, downloaded first when missing. Never trusts a file that
    does not match the pinned size and SHA-256."""
    if not entry["sha256"] and not allow_unpinned:
        raise BuildError(f"{entry['file']}: no SHA-256 pinned in the manifest")
    cache.mkdir(parents=True, exist_ok=True)
    dest = cache / entry["file"]
    if dest.is_file():
        problem = _check_archive(dest, entry)
        if problem is None:
            return dest
        log(f"   cached {dest.name} is not the pinned archive ({problem}); downloading again")
        dest.unlink()
    log(f"   downloading {entry['file']} from {entry['url']}")
    part = cache / (entry["file"] + ".part")
    _download(entry["url"], part, entry["size"] or None)
    problem = _check_archive(part, entry)
    if problem is not None:
        part.unlink()
        raise BuildError(f"{entry['file']} from {entry['url']} does not match the manifest: {problem}")
    os.replace(part, dest)
    return dest


def _sources_readme(manifest: dict, version: str) -> str:
    lines = [
        f"Background Editor {version} - source code of third-party components",
        "=" * 64,
        "",
        "This archive holds the unmodified upstream source archives of the third-party",
        "components in Background Editor that are licensed under the GNU GPL, the GNU LGPL",
        "or the Mozilla Public License. Together with the Background Editor source zip",
        f"({APP_NAME}-{version}-src.zip) it is the Corresponding Source for this version",
        "(GNU GPL version 3, section 6). The source zip contains tools/third_party_sources.json",
        "with the same list. The archives are stored as downloaded; check them with the",
        "SHA-256 values below.",
        "",
    ]
    for entry in manifest["archives"]:
        lines += [
            f"{entry['name']}",
            f"  File:     {entry['file']} ({entry['size']:,} bytes)",
            f"  SHA-256:  {entry['sha256']}",
            f"  From:     {entry['url']}",
            f"  Licence:  {entry['licence']}",
        ]
        if entry.get("provides"):
            lines.append(f"  Provides: {entry['provides']}")
        if entry.get("notes"):
            lines.append(f"  Notes:    {entry['notes']}")
        lines.append("")
    return "\n".join(lines)


def cmd_third_party(args: argparse.Namespace) -> None:
    manifest_path = Path(args.manifest)
    manifest = load_manifest(manifest_path)
    cache = Path(args.cache)
    paths = []
    for entry in manifest["archives"]:
        path = fetch_archive(entry, cache, allow_unpinned=args.write_pins)
        if args.write_pins and (not entry["sha256"] or not entry["size"]):
            entry["sha256"], entry["size"] = sha256_file(path), path.stat().st_size
            log(f"   pinned {entry['file']}: {entry['size']} bytes, {entry['sha256']}")
        paths.append((entry, path))
    if args.write_pins:
        with open(manifest_path, "w", encoding="utf-8", newline="") as fh:
            json.dump(manifest, fh, indent=2, ensure_ascii=False)
            fh.write("\n")
    if not args.out:
        return
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    prefix = f"{APP_NAME}-{args.version}-third-party-sources/"
    part = out.with_name(out.name + ".part")
    with zipfile.ZipFile(part, "w", allowZip64=True) as zf:
        _add_text(zf, prefix + "SOURCES.txt", _sources_readme(manifest, args.version))
        for entry, path in paths:
            # Stored, not recompressed: the archives are compressed already and stay
            # byte-identical to the upstream files, so their SHA-256 can be checked.
            _add_file(zf, path, prefix + entry["file"], zipfile.ZIP_STORED)
    _finish(part, out)
    log(f"   {out.name}: {len(paths)} archives, {out.stat().st_size / 1e6:.1f} MB")


# ------------------------------------------------------------------ Qt notices
# Parts of the Qt sources that are not in the bundled Windows Qt libraries (other platforms,
# test and build tooling, D-Bus); everything else is listed, which errs on the safe side.
_QT_NOTICE_SKIP = ("wayland", "xcb", "android", "gradle", "wasm", "cocoa", "testlib", "testinternal",
                   "cmake/", "util/", "src/dbus/")
_QT_MODULES = ("qtbase", "qtsvg", "qtimageformats")


def _qt_attributions(archive: Path):
    """(entry, licence texts) for each third-party component in a Qt source archive."""
    import tarfile

    with tarfile.open(archive) as tf:
        members = {m.name: m for m in tf.getmembers() if m.isfile()}
        for name in sorted(members):
            if not name.endswith("/qt_attribution.json"):
                continue
            rel = name.split("/", 1)[1]
            if any(skip in rel for skip in _QT_NOTICE_SKIP):
                continue
            folder = name.rsplit("/", 1)[0]
            data = json.loads(tf.extractfile(members[name]).read().decode("utf-8"), strict=False)
            for entry in data if isinstance(data, list) else [data]:
                texts = []
                files = entry.get("LicenseFiles") or ([entry["LicenseFile"]] if entry.get("LicenseFile") else [])
                if entry.get("CopyrightFile"):
                    files = [entry["CopyrightFile"]] + list(files)
                for fname in files:
                    member = members.get(f"{folder}/{fname}")
                    if member is not None:
                        texts.append((fname, tf.extractfile(member).read().decode("utf-8", "replace")))
                yield rel, entry, texts


def cmd_qt_notices(args: argparse.Namespace) -> None:
    """licenses/Qt6/THIRD-PARTY-NOTICES.txt from the qt_attribution.json files of the exact
    Qt sources: the notices of the libraries built into the bundled Qt DLLs and plugins."""
    manifest = load_manifest(Path(args.manifest))
    cache = Path(args.cache)
    out = Path(args.out)
    lines = [
        "Qt 6 - third-party components",
        "=" * 30,
        "",
        "The Qt libraries and plugins in this program (GNU LGPL version 3) contain the",
        "third-party components below. This list is generated from the qt_attribution.json",
        "files of the Qt source archives in the third-party sources zip; components that are",
        "only used on other platforms are left out.",
        "",
    ]
    count = 0
    for entry in manifest["archives"]:
        module = entry["file"].split("-", 1)[0]
        if module not in _QT_MODULES:
            continue
        archive = fetch_archive(entry, cache)
        for rel, att, texts in _qt_attributions(archive):
            count += 1
            copyright_ = att.get("Copyright", "")
            if isinstance(copyright_, list):
                copyright_ = "\n".join(copyright_)
            lines += ["-" * 78, f"{att.get('Name', att.get('Id', '?'))}" + (f" {att['Version']}" if att.get("Version") else "")]
            lines.append(f"  In:       {module}/{rel.rsplit('/', 1)[0]}")
            if att.get("Homepage"):
                lines.append(f"  Homepage: {att['Homepage']}")
            lines.append(f"  Licence:  {att.get('License', '')} ({att.get('LicenseId', '')})")
            if copyright_:
                lines += ["", copyright_.strip()]
            for fname, text in texts:
                lines += ["", f"[{fname}]", text.strip()]
            lines.append("")
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "w", encoding="utf-8", newline="") as fh:
        fh.write("\n".join(lines) + "\n")
    log(f"   {out.name}: {count} Qt third-party components")


# ------------------------------------------------------------------ bundle checks
_VC_RUNTIME = re.compile(r"^(vcruntime|msvcp|concrt|vccorlib|vcomp|vcamp|msvcr)\d", re.I)
_VC_STANDARD = {
    "vcruntime140.dll",
    "vcruntime140_1.dll",
    "vcruntime140_threads.dll",
    "msvcp140.dll",
    "msvcp140_1.dll",
    "msvcp140_2.dll",
    "msvcp140_atomic_wait.dll",
    "msvcp140_codecvt_ids.dll",
    "concrt140.dll",
    "vccorlib140.dll",
    "vcomp140.dll",
}
_PE_SUFFIXES = (".dll", ".pyd", ".exe")
_IPP_MARKERS = (b"ippicv", b"ippiw", b"Integrated Performance Primitives", b"ippGetLibVersion")
_FORBIDDEN_MODULES = ("cv2", "PIL.AvifImagePlugin", "PIL._avif", "PyQt6.QtPdf", "rembg", "pymatting", "numba")


def _forbidden_reason(rel: str) -> str | None:
    """Why a file must not be in the app folder, or None."""
    low = rel.lower()
    name = low.rsplit("/", 1)[-1]
    if name == "directml.dll":
        return "DirectML redistributable (the app uses Windows' own System32\\DirectML.dll)"
    if _VC_RUNTIME.match(name) and name.endswith(".dll"):
        return "Microsoft Visual C++ runtime (installed by Microsoft's vc_redist, never bundled)"
    if name == "ucrtbase.dll" or name.startswith("api-ms-win-"):
        return "Microsoft Universal C runtime (part of Windows)"
    if name.startswith("d3dcompiler_"):
        return "Microsoft Direct3D compiler (part of Windows)"
    if name.startswith("qt6pdf") or name == "qpdf.dll":
        return "Qt PDF (would need the qtwebengine sources; not used)"
    if "/plugins/tls/" in low or "/plugins/networkinformation/" in low:
        return "Qt TLS / network information plugin (only QLocalSocket is used)"
    if name in ("libssl-3-x64.dll", "libcrypto-3-x64.dll"):
        return "OpenSSL for Qt's TLS plugin (not used)"
    if "/cv2/" in f"/{low}" or name.startswith("cv2") or "opencv" in name:
        return "OpenCV (removed: its wheel links Intel IPP)"
    if name.startswith("ipp") and name.endswith(".dll"):
        return "Intel IPP"
    if fnmatch.fnmatch(low, "*/pil/_avif*"):
        return "Pillow AVIF support (excluded)"
    if name == "portable.conf":
        return "portable.conf belongs only in the portable zip"
    if name.endswith((".onnx", ".onnx_data")):
        return "AI model weights are downloaded, never bundled"
    return None


def _pe_imports(path: Path):
    import pefile

    pe = pefile.PE(str(path), fast_load=True)
    try:
        pe.parse_data_directories(
            directories=[
                pefile.DIRECTORY_ENTRY["IMAGE_DIRECTORY_ENTRY_IMPORT"],
                pefile.DIRECTORY_ENTRY["IMAGE_DIRECTORY_ENTRY_DELAY_IMPORT"],
            ]
        )
        names = [e.dll.decode("ascii", "replace") for e in getattr(pe, "DIRECTORY_ENTRY_IMPORT", [])]
        names += [e.dll.decode("ascii", "replace") for e in getattr(pe, "DIRECTORY_ENTRY_DELAY_IMPORT", [])]
        linker = (pe.OPTIONAL_HEADER.MajorLinkerVersion, pe.OPTIONAL_HEADER.MinorLinkerVersion)
        return names, linker
    finally:
        pe.close()


def _has_ipp(path: Path) -> bytes | None:
    data = path.read_bytes()
    for marker in _IPP_MARKERS:
        if marker in data:
            return marker
    return None


def _pyz_modules(exe: Path) -> list[str]:
    from PyInstaller.archive.readers import CArchiveReader

    reader = CArchiveReader(str(exe))
    return list(reader.open_embedded_archive("PYZ.pyz").toc.keys())


def _match_any(rel: str, patterns) -> bool:
    low = rel.lower()
    return any(fnmatch.fnmatch(low, p.lower()) for p in patterns)


def cmd_check_bundle(args: argparse.Namespace) -> None:
    app = Path(args.app_dir)
    exe = app / f"{APP_NAME}.exe"
    errors: list[str] = []
    files = sorted(p for p in app.rglob("*") if p.is_file())
    rels = {p: p.relative_to(app).as_posix() for p in files}

    # 1. Files that must never ship.
    for path, rel in rels.items():
        reason = _forbidden_reason(rel)
        if reason:
            errors.append(f"forbidden file {rel}: {reason}")

    # 2. Files that must ship.
    required = {
        f"{APP_NAME}.exe": None,
        "LICENSE": GPL3_SHA256,
        "LICENSES.txt": None,
        "licenses/LGPL-3.0.txt": LGPL3_SHA256,
        "licenses/Qt6/THIRD-PARTY-NOTICES.txt": None,
        f"source/{APP_NAME}-{args.version}-src.zip": None,
    }
    for rel, digest in required.items():
        path = app / rel
        if not path.is_file():
            errors.append(f"missing {rel}")
        elif digest and sha256_file(path) != digest:
            errors.append(f"{rel} is not the verbatim licence text (SHA-256 differs)")
    src_zip = app / "source" / f"{APP_NAME}-{args.version}-src.zip"
    if src_zip.is_file():
        with zipfile.ZipFile(src_zip) as zf:
            names = {n.split("/", 1)[-1] for n in zf.namelist()}
        for must in ("app.py", "LICENSE", "BackgroundEditor.spec", "build.ps1", "bgeditor/__init__.py"):
            if must not in names:
                errors.append(f"source zip lacks {must}")
    if len(list((app / "licenses").glob("*"))) < 5:
        errors.append("licenses folder is missing or nearly empty")

    # 3. Modules inside the exe's archive.
    if exe.is_file():
        for mod in _pyz_modules(exe):
            if any(mod == f or mod.startswith(f + ".") for f in _FORBIDDEN_MODULES):
                errors.append(f"forbidden module {mod} in the exe's archive")

    # 4. PE files: imports must resolve, no IPP, and the VC++ runtime they need.
    system32 = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32"
    bundled_names = {p.name.lower() for p in files}
    vc_min = (0, 0)
    vc_min_files: list[str] = []
    vc_imported: set[str] = set()
    binaries = []
    for path, rel in rels.items():
        if not rel.lower().endswith(_PE_SUFFIXES):
            continue
        binaries.append((rel, path.stat().st_size))
        marker = _has_ipp(path)
        if marker:
            errors.append(f"{rel} contains Intel IPP code (found {marker.decode()!r})")
        try:
            imports, linker = _pe_imports(path)
        except Exception as exc:  # noqa: BLE001 - report any unreadable binary
            errors.append(f"{rel}: cannot read the PE headers ({exc})")
            continue
        uses_vc = False
        for name in imports:
            low = name.lower()
            if _VC_RUNTIME.match(low):
                uses_vc = True
                vc_imported.add(low)
                if low not in _VC_STANDARD:
                    errors.append(f"{rel} imports {name}, a renamed VC++ runtime that Windows cannot provide")
                    continue
            if low in bundled_names or low.startswith(("api-ms-win-", "ext-ms-")):
                continue
            if not (system32 / name).is_file():
                errors.append(f"{rel} imports {name}, which is neither bundled nor in System32")
        if uses_vc:
            if linker > vc_min:
                vc_min, vc_min_files = linker, [rel]
            elif linker == vc_min:
                vc_min_files.append(rel)

    # 5. Every GPL/LGPL/MPL binary must be covered by an archive in the sources manifest,
    #    and every archive must still cover something (the list stays exact).
    manifest = load_manifest(Path(args.manifest))
    import importlib.metadata

    for entry in manifest["archives"]:
        wheel = entry.get("wheel")
        if not wheel:
            continue
        try:
            installed = importlib.metadata.version(wheel["distribution"])
        except importlib.metadata.PackageNotFoundError:
            installed = "not installed"
        if installed != wheel["version"]:
            errors.append(
                f"third_party_sources.json: {entry['file']} is the source for {wheel['distribution']} "
                f"{wheel['version']}, but the build venv has {installed}; update the archive list"
            )
    candidates = [rel for rel in rels.values() if _match_any(rel, manifest["copyleft_files"])]
    for rel in candidates:
        if not any(_match_any(rel, e["covers"]) for e in manifest["archives"]):
            errors.append(f"{rel} is GPL/LGPL/MPL code but no archive in third_party_sources.json covers it")
    for entry in manifest["archives"]:
        if not any(_match_any(rel, entry["covers"]) for rel in rels.values()):
            errors.append(f"third_party_sources.json: {entry['file']} covers nothing in this build any more")
    unknown = [
        rel
        for rel, _ in binaries
        if not _match_any(rel, manifest["copyleft_files"]) and not _match_any(rel, manifest["notice_only_files"])
    ]

    # Report.
    total = sum(p.stat().st_size for p in files)
    log(f"   {len(files)} files, {total / 1e6:.1f} MB; {len(binaries)} binaries:")
    for rel, size in sorted(binaries):
        log(f"      {size / 1e6:9.2f} MB  {rel}")
    vc_text = f"{vc_min[0]}.{vc_min[1]}" if vc_min != (0, 0) else ""
    if vc_text:
        log(f"   VC++ runtime needed: {vc_text} or newer (linked by {', '.join(vc_min_files[:4])}"
            f"{' ...' if len(vc_min_files) > 4 else ''})")
        log(f"   VC++ runtime DLLs imported: {', '.join(sorted(vc_imported))}")
    for rel in unknown:
        log(f"   NOTE: {rel} is in neither list of third_party_sources.json; check its licence")
    if args.json_out:
        with open(args.json_out, "w", encoding="utf-8", newline="") as fh:
            json.dump(
                {
                    "vc_runtime_min": vc_text,
                    "vc_runtime_min_files": vc_min_files,
                    "vc_runtime_dlls": sorted(vc_imported),
                    "files": len(files),
                    "bytes": total,
                    "binaries": [{"path": rel, "bytes": size} for rel, size in sorted(binaries)],
                    "unlisted_binaries": unknown,
                },
                fh,
                indent=2,
            )
            fh.write("\n")
    if errors:
        raise BuildError("The app folder failed the bundle checks:\n" + "\n".join("  " + e for e in errors))
    log("   bundle checks passed")


# ---------------------------------------------------------------------- portable
def _constant(path: Path, name: str, kind: type = str):
    """A module-level NAME = <literal> from a source file, without importing it."""
    tree = ast.parse(path.read_text(encoding="utf-8"), str(path))
    for node in tree.body:
        if (
            isinstance(node, ast.Assign)
            and any(isinstance(t, ast.Name) and t.id == name for t in node.targets)
            and isinstance(node.value, ast.Constant)
            and type(node.value.value) is kind
        ):
            return node.value.value
    raise BuildError(f"No {name} = <{kind.__name__}> in {path}")


def _string_constant(path: Path, name: str) -> str:
    return _constant(path, name, str)


MODELS_README = """Background Editor - models folder

Put the AI model files (.onnx) here, or let the app download them: with internet,
open Advanced settings and choose "Download all models for offline use". The app
checks every model file by its SHA-256 before it uses it.

Because portable.conf is present, the app keeps models here and its settings and
logs in the data folder next to it, so the whole folder can be copied to another
PC, including one without internet.
"""


def _portable_readme(version: str, vc_min: str) -> str:
    return f"""Background Editor {version} - portable

Start BackgroundEditor.exe; nothing needs to be installed. Because the file
portable.conf is present, downloaded AI models (models\\), settings and logs
(data\\) stay inside this folder. Delete portable.conf to use the normal per-user
folders instead.

Requirement: Microsoft Visual C++ Redistributable for Visual Studio 2017-2026 (x64),
version {vc_min} or newer. Most PCs already have it. It is not included here, because it
is Microsoft's own software; the installer version of Background Editor offers to
install it. If the app does not start (for example "VCRUNTIME140.dll was not found"
or "Failed to load Python DLL"), install it from Microsoft:
    https://aka.ms/vc14/vc_redist.x64.exe

GPU acceleration uses Windows' own DirectML (Windows 11 24H2 or later recommended);
without it, the app runs on the CPU.

Licence: GNU General Public License version 3 or later; see LICENSE and
LICENSES.txt. The source code of this version is in
source\\{APP_NAME}-{version}-src.zip. The source code of the GPL/LGPL/MPL
third-party components is in {APP_NAME}-{version}-third-party-sources.zip, published
next to this zip. When you give this program to others, give both zips along.
"""


def cmd_portable(args: argparse.Namespace) -> None:
    app = Path(args.app_dir)
    paths_py = ROOT / "bgeditor" / "paths.py"
    marker_name = _string_constant(paths_py, "PORTABLE_MARKER")
    marker_text = _string_constant(paths_py, "PORTABLE_MARKER_TEXT")
    files = sorted(p for p in app.rglob("*") if p.is_file())
    if not files:
        raise BuildError(f"Empty app folder: {app}")
    for path in files:
        if path.name.lower() == marker_name.lower():
            raise BuildError(f"{path} must not be in the app folder")
    out = Path(args.out)
    part = out.with_name(out.name + ".part")
    top = f"{APP_NAME}/"
    with zipfile.ZipFile(part, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        for path in files:
            _add_file(zf, path, top + path.relative_to(app).as_posix(), zipfile.ZIP_DEFLATED)
        _add_text(zf, top + marker_name, marker_text)
        _add_text(zf, top + "models/README.txt", MODELS_README)
        _add_text(zf, top + "README-portable.txt", _portable_readme(args.version, args.vc_min))
    _finish(part, out)
    log(f"   {out.name}: {len(files) + 3} files, {out.stat().st_size / 1e6:.1f} MB")


# ------------------------------------------------------------ release manifest
APP_UPDATES_PY = ROOT / "bgeditor" / "app_updates.py"
MANIFEST_HEADER = """# Background Editor release manifest - DATA ONLY, never run.
#
# Background Editor checks the Authenticode signature of this file and then reads only the
# JSON text between @' and '@ below; it never runs the file. The JSON lists the SHA-256 and
# size of the installer, of the portable zip and of every file inside that zip. An update
# is installed only when the downloaded files match it byte for byte, and only when this
# version is newer than the running one and the running one is at least min_update_from.
"""
PORTABLE_EXTRAS = ("portable.conf", "models/README.txt", "README-portable.txt")  # added by cmd_portable


def zip_file_list(zip_path: Path, root: str = APP_NAME) -> list[dict]:
    """[{path, sha256, size}] of every file in the zip below root/, sorted by path."""
    files = []
    seen: set[str] = set()
    with zipfile.ZipFile(zip_path) as zf:
        for info in zf.infolist():
            if info.is_dir():
                continue
            if not info.filename.startswith(root + "/"):
                raise BuildError(f"{zip_path.name}: {info.filename} is not below {root}/")
            rel = info.filename[len(root) + 1 :]
            if rel.lower() in seen:
                raise BuildError(f"{zip_path.name}: {rel} is in the zip twice")
            seen.add(rel.lower())
            h = hashlib.sha256()
            size = 0
            with zf.open(info) as fh:
                for chunk in iter(lambda: fh.read(CHUNK), b""):
                    h.update(chunk)
                    size += len(chunk)
            files.append({"path": rel, "sha256": h.hexdigest(), "size": size})
    return sorted(files, key=lambda f: f["path"].lower())


def release_manifest_text(version: str, min_update_from: str, portable: Path, setup: Path | None, built: str = "") -> str:
    """The release manifest: comment lines, then one single-quoted here-string with JSON."""
    if not re.fullmatch(r"\d+(\.\d+){0,3}([-+][0-9A-Za-z.-]+)?", version):
        raise BuildError(f"Version '{version}' is not a version")
    if min_update_from and not re.fullmatch(r"\d+(\.\d+){0,3}", min_update_from):
        raise BuildError(f"Minimum version '{min_update_from}' is not a version like 1.1.0")
    if portable.name != f"{APP_NAME}-{version}-portable.zip":
        raise BuildError(f"{portable.name} is not the portable zip of version {version}")
    if setup is not None and setup.name != f"{APP_NAME}-Setup-{version}.exe":
        raise BuildError(f"{setup.name} is not the installer of version {version}")
    files = zip_file_list(portable)
    if not any(f["path"] == f"{APP_NAME}.exe" for f in files):
        raise BuildError(f"{portable.name} holds no {APP_NAME}/{APP_NAME}.exe")
    tilde = [f["path"] for f in files if "~" in f["path"]]
    if tilde:
        # The app refuses a '~' in a manifest path (it could be an 8.3 short name of another file).
        raise BuildError(f"{portable.name} holds a file name with '~', which the app refuses: {tilde[0]}")
    data = {
        "format": _constant(APP_UPDATES_PY, "MANIFEST_FORMAT", int),
        "app": "Background Editor",
        "version": version,
        "built": built or time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "min_update_from": min_update_from,
        "setup": None
        if setup is None
        else {"name": setup.name, "sha256": sha256_file(setup), "size": setup.stat().st_size},
        "portable": {
            "name": portable.name,
            "sha256": sha256_file(portable),
            "size": portable.stat().st_size,
            "root": APP_NAME,
            "files": files,
        },
    }
    # One line per file of the zip, so the manifest stays readable.
    placeholder = "@@files@@"
    data["portable"]["files"] = placeholder
    body = json.dumps(data, indent=1, ensure_ascii=True)
    rows = ",\n".join("   " + json.dumps(f, ensure_ascii=True) for f in files)
    body = body.replace(json.dumps(placeholder), "[\n" + rows + "\n  ]", 1)
    if placeholder in body or any(line.startswith("'@") for line in body.split("\n")):
        raise BuildError("the manifest data would end its here-string early")
    variable = _string_constant(APP_UPDATES_PY, "MANIFEST_VARIABLE")
    text = f"{MANIFEST_HEADER}${variable} = @'\n{body}\n'@\n"
    if not text.isascii() or "\r" in text:
        raise BuildError("the manifest must be plain ASCII with LF line ends")
    return text


def cmd_release_manifest(args: argparse.Namespace) -> None:
    out = Path(args.out)
    setup = Path(args.setup) if args.setup else None
    text = release_manifest_text(args.version, args.min_update_from, Path(args.portable), setup)
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "w", encoding="ascii", newline="") as fh:
        fh.write(text)
    count = text.count('"path"')
    log(f"   {out.name}: version {args.version}, from version {args.min_update_from or 'any'}, "
        f"installer {'listed' if setup else 'not built'}, portable zip with {count} files")


def cmd_verify_manifest(args: argparse.Namespace) -> None:
    """The manifest as the app reads it (its own parser, and for a signed build its own
    WinVerifyTrust check with the pinned certificate), compared with the built files."""
    sys.path.insert(0, str(ROOT))
    from bgeditor import app_updates  # noqa: PLC0415 - the app's own code, on purpose

    path = Path(args.manifest)
    pin = (args.pin or app_updates.signer_pin()).upper()
    if args.unsigned:
        manifest = app_updates.parse_manifest(path.read_bytes(), signed=False)
        signer = "NOT SIGNED (-NoSign build): the app will not install this release"
    else:
        check = app_updates.check_signature(path, pinned=pin)
        if not check.ok:
            raise BuildError(f"{path.name}: the app refuses its signature: {check.reason}")
        try:
            manifest = app_updates.verify_manifest(path, pinned=pin)
        except app_updates.UpdateError as exc:
            raise BuildError(f"{path.name}: {exc}") from exc
        signer = f"signed by {check.subject} ({check.reason}; certificate SHA-256 {check.cert_sha256})"
    errors = []
    if manifest.version != args.version:
        errors.append(f"version {manifest.version} instead of {args.version}")
    if manifest.min_update_from != args.min_update_from:
        errors.append(f"min_update_from {manifest.min_update_from!r} instead of {args.min_update_from!r}")
    for key, given in (("setup", args.setup), ("portable", args.portable)):
        entry = getattr(manifest, key)
        if not given:
            if entry is not None:
                errors.append(f"it lists a {key} file, but none was built")
            continue
        file = Path(given)
        if entry is None:
            errors.append(f"it does not list {file.name}")
        elif (entry.name, entry.size, entry.sha256) != (file.name, file.stat().st_size, sha256_file(file)):
            errors.append(f"its {key} entry does not match {file.name}")
    listed = {f.path: (f.sha256, f.size) for f in (manifest.portable.files if manifest.portable else ())}
    in_zip = {f["path"]: (f["sha256"], f["size"]) for f in zip_file_list(Path(args.portable))}
    if listed != in_zip:
        diff = sorted(set(listed) ^ set(in_zip)) or sorted(k for k in listed if listed[k] != in_zip.get(k))
        errors.append(f"its file list does not match {Path(args.portable).name} (e.g. {diff[:3]})")
    if args.app_dir:
        app = Path(args.app_dir)
        on_disk = {p.relative_to(app).as_posix(): p for p in app.rglob("*") if p.is_file()}
        expected = {k for k in listed if k not in PORTABLE_EXTRAS}
        if set(on_disk) != expected:
            diff = sorted(set(on_disk) ^ expected)
            errors.append(f"its file list does not match the app folder (e.g. {diff[:3]})")
        else:
            for rel, p in on_disk.items():
                if (sha256_file(p), p.stat().st_size) != listed[rel]:
                    errors.append(f"{rel} in the app folder does not match the manifest")
    if errors:
        raise BuildError(f"{path.name} does not match the build:\n" + "\n".join("  " + e for e in errors))
    log(f"   {path.name}: version {manifest.version}, from version {manifest.min_update_from or 'any'}, "
        f"{len(listed)} files of the portable zip"
        f"{', the installer' if manifest.setup else ''} and both whole files match; {signer}")


# -------------------------------------------------------------------------- main
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("source-zip")
    p.add_argument("--version", required=True)
    p.add_argument("--out", required=True)
    p.set_defaults(func=cmd_source_zip)

    p = sub.add_parser("verify-source-zip")
    p.add_argument("--zip", required=True)
    p.set_defaults(func=cmd_verify_source_zip)

    p = sub.add_parser("third-party")
    p.add_argument("--manifest", default=str(ROOT / "tools" / "third_party_sources.json"))
    p.add_argument("--cache", required=True)
    p.add_argument("--version", default="")
    p.add_argument("--out", default="")
    p.add_argument("--write-pins", action="store_true", help="download unpinned archives and pin them")
    p.set_defaults(func=cmd_third_party)

    p = sub.add_parser("qt-notices")
    p.add_argument("--manifest", default=str(ROOT / "tools" / "third_party_sources.json"))
    p.add_argument("--cache", required=True)
    p.add_argument("--out", required=True)
    p.set_defaults(func=cmd_qt_notices)

    p = sub.add_parser("check-bundle")
    p.add_argument("--app-dir", required=True)
    p.add_argument("--version", required=True)
    p.add_argument("--manifest", default=str(ROOT / "tools" / "third_party_sources.json"))
    p.add_argument("--json-out", default="")
    p.set_defaults(func=cmd_check_bundle)

    p = sub.add_parser("portable")
    p.add_argument("--app-dir", required=True)
    p.add_argument("--version", required=True)
    p.add_argument("--vc-min", required=True)
    p.add_argument("--out", required=True)
    p.set_defaults(func=cmd_portable)

    p = sub.add_parser("release-manifest")
    p.add_argument("--version", required=True)
    p.add_argument("--min-update-from", default="")
    p.add_argument("--portable", required=True)
    p.add_argument("--setup", default="")
    p.add_argument("--out", required=True)
    p.set_defaults(func=cmd_release_manifest)

    p = sub.add_parser("verify-manifest")
    p.add_argument("--manifest", required=True)
    p.add_argument("--version", required=True)
    p.add_argument("--min-update-from", default="")
    p.add_argument("--portable", required=True)
    p.add_argument("--setup", default="")
    p.add_argument("--app-dir", default="")
    p.add_argument("--unsigned", action="store_true", help="a -NoSign build: parse only")
    p.add_argument("--pin", default="", help="the certificate SHA-256 to expect (default: the app's pin)")
    p.set_defaults(func=cmd_verify_manifest)

    args = parser.parse_args(argv)
    if getattr(args, "out", "") and args.command in ("third-party", "source-zip", "portable") and not args.version:
        parser.error("--version is needed with --out")
    try:
        args.func(args)
    except BuildError as exc:
        print(f"ERROR: {exc}", file=sys.stderr, flush=True)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

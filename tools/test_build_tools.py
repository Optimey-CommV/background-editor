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

"""Tests for tools/build_tools.py. Run: .venv\\Scripts\\python.exe tools\\test_build_tools.py

Plain asserts with a small runner (pytest is not part of the build environment). The
tests work in a fixed folder under %TEMP% and remove it afterwards.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
import traceback
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import build_tools as bt  # noqa: E402

WORK = Path(os.environ.get("TEMP", str(Path.home()))) / "BackgroundEditor-build-tools-test"


def _remove(path: Path) -> None:
    """Remove a test folder, also the read-only files git writes under .git/objects."""
    def retry_writable(func, target, _exc):
        os.chmod(target, stat.S_IWRITE)
        func(target)

    if path.exists():
        shutil.rmtree(path, onexc=retry_writable)


def _fresh(name: str) -> Path:
    path = WORK / name
    _remove(path)
    path.mkdir(parents=True)
    return path


def test_forbidden_files() -> None:
    bad = {
        "_internal/onnxruntime/capi/DirectML.dll": "DirectML",
        "_internal/vcruntime140.dll": "Visual C++",
        "_internal/PyQt6/Qt6/bin/MSVCP140_1.dll": "Visual C++",
        "_internal/numpy.libs/msvcp140-a4c2229bdc2a2a630acdc095b4d86008.dll": "Visual C++",
        "_internal/PyQt6/Qt6/bin/concrt140.dll": "Visual C++",
        "_internal/ucrtbase.dll": "Universal C runtime",
        "_internal/api-ms-win-crt-runtime-l1-1-0.dll": "Universal C runtime",
        "_internal/PyQt6/Qt6/bin/Qt6Pdf.dll": "Qt PDF",
        "_internal/PyQt6/Qt6/plugins/imageformats/qpdf.dll": "Qt PDF",
        "_internal/PyQt6/Qt6/plugins/tls/qopensslbackend.dll": "TLS",
        "_internal/PyQt6/Qt6/plugins/networkinformation/qnetworklistmanager.dll": "network information",
        "_internal/libssl-3-x64.dll": "OpenSSL for Qt",
        "_internal/cv2/cv2.pyd": "OpenCV",
        "_internal/opencv_videoio_ffmpeg500_64.dll": "OpenCV",
        "_internal/ippicv.dll": "Intel IPP",
        "_internal/PIL/_avif.cp314-win_amd64.pyd": "AVIF",
        "portable.conf": "portable.conf",
        "models/BiRefNet-matting-epoch_100.onnx": "model weights",
    }
    for rel, word in bad.items():
        reason = bt._forbidden_reason(rel)
        assert reason and word.lower() in reason.lower(), (rel, reason)
    good = [
        "BackgroundEditor.exe",
        "_internal/python314.dll",
        "_internal/libssl-3.dll",
        "_internal/libcrypto-3.dll",
        "_internal/PyQt6/Qt6/bin/Qt6Core.dll",
        "_internal/PyQt6/Qt6/plugins/imageformats/qjpeg.dll",
        "_internal/PIL/_imaging.cp314-win_amd64.pyd",
        "_internal/libx265-217-8a7f7f4ebe0ffaa73ce4bb306c2d18d6.dll",
        "_internal/scipy.libs/libscipy_openblas-197ee2fc9b4d071f7e048078cac74115.dll",
        "LICENSE",
        "source/BackgroundEditor-1.1.0-src.zip",
    ]
    for rel in good:
        assert bt._forbidden_reason(rel) is None, (rel, bt._forbidden_reason(rel))


def test_source_tree_selection() -> None:
    files = dict(bt.source_files())
    for must in ("app.py", "LICENSE", "LICENSES.txt", "BackgroundEditor.spec", "build.ps1",
                 "installer.iss", "requirements.txt", "README.md", "bgeditor/__init__.py",
                 "bgeditor/paths.py", "tools/build_tools.py", "tools/third_party_sources.json",
                 "tools/licenses/LGPL-3.0.txt"):
        assert must in files, must
    for rel in files:
        assert "__pycache__" not in rel and not rel.endswith((".pyc", ".part")), rel
        assert not rel.startswith((".venv/", "bench/", "dist/", "build/")), rel


def test_source_tree_matches_git() -> None:
    """In a git work tree the source zip is exactly the tracked files: an untracked or
    ignored file (such as a stray signing key) stops it, and so does a tracked file that
    SOURCE_FILES and SOURCE_DIRS leave out."""
    if shutil.which("git") is None:
        print("      skipped: git not found")
        return
    root = _fresh("git-tree")
    saved = bt.SOURCE_FILES, bt.SOURCE_DIRS
    bt.SOURCE_FILES, bt.SOURCE_DIRS = ["app.py"], ["pkg"]
    try:
        (root / "pkg").mkdir()
        (root / "app.py").write_bytes(b"print('app')\n")
        (root / "pkg" / "mod.py").write_bytes(b"x = 1\n")
        (root / ".gitignore").write_bytes(b"*.pfx\n")
        subprocess.run(["git", "-C", str(root), "init", "-q"], check=True)
        subprocess.run(["git", "-C", str(root), "add", "app.py", "pkg/mod.py"], check=True)
        assert [rel for rel, _ in bt.source_files(root)] == ["app.py", "pkg/mod.py"]

        def refused(text: str) -> bool:
            try:
                bt.source_files(root)
            except bt.BuildError as exc:
                return text in str(exc)
            return False

        (root / "pkg" / "signing.pfx").write_bytes(b"not a real key")  # ignored by .gitignore
        assert refused("not tracked by git: pkg/signing.pfx")
        (root / "pkg" / "signing.pfx").unlink()
        (root / "pkg" / "new.py").write_bytes(b"y = 2\n")  # new, not added yet
        assert refused("not tracked by git: pkg/new.py")
        subprocess.run(["git", "-C", str(root), "add", "pkg/new.py"], check=True)
        assert len(bt.source_files(root)) == 3
        (root / "notes.txt").write_bytes(b"tracked, outside the selection\n")
        subprocess.run(["git", "-C", str(root), "add", "notes.txt"], check=True)
        assert refused("tracked, but not in SOURCE_FILES or SOURCE_DIRS: notes.txt")
        # A tracked file deleted in the work tree (not committed yet) is no reason to stop.
        (root / "notes.txt").unlink()
        files = bt.source_files(root)
        assert len(files) == 3
        # Byte comparison with HEAD: everything differs before the first commit, nothing
        # after it, and a change of line endings alone is caught too.
        assert bt.differs_from_head(root, files) == ["app.py", "pkg/mod.py", "pkg/new.py"]
        subprocess.run(["git", "-C", str(root), "add", "-A"], check=True)
        subprocess.run(["git", "-C", str(root), "-c", "user.name=Test", "-c", "user.email=test@example.com",
                        "commit", "-q", "-m", "test"], check=True)
        assert bt.differs_from_head(root, files) == []
        (root / "pkg" / "mod.py").write_bytes(b"x = 1\r\n")
        assert bt.differs_from_head(root, files) == ["pkg/mod.py"]
    finally:
        bt.SOURCE_FILES, bt.SOURCE_DIRS = saved


def test_source_zip_round_trip() -> None:
    out = _fresh("src") / "BackgroundEditor-9.9.9-src.zip"
    assert bt.main(["source-zip", "--version", "9.9.9", "--out", str(out)]) == 0
    assert out.is_file()
    with zipfile.ZipFile(out) as zf:
        names = zf.namelist()
        assert all(n.startswith("BackgroundEditor-9.9.9-src/") for n in names)
        assert "BackgroundEditor-9.9.9-src/LICENSE" in names
        assert hashlib.sha256(zf.read("BackgroundEditor-9.9.9-src/LICENSE")).hexdigest() == bt.GPL3_SHA256
    assert bt.main(["verify-source-zip", "--zip", str(out)]) == 0
    # A zip with one file changed must fail the check.
    tampered = out.with_name("tampered.zip")
    with zipfile.ZipFile(out) as src, zipfile.ZipFile(tampered, "w") as dst:
        for info in src.infolist():
            data = src.read(info)
            if info.filename.endswith("/README.md"):
                data += b"\nchanged\n"
            dst.writestr(info, data)
    assert bt.main(["verify-source-zip", "--zip", str(tampered)]) == 1


def test_licence_texts_are_verbatim() -> None:
    assert bt.sha256_file(bt.ROOT / "LICENSE") == bt.GPL3_SHA256
    assert bt.sha256_file(bt.ROOT / "tools" / "licenses" / "LGPL-3.0.txt") == bt.LGPL3_SHA256


def test_manifest_is_complete() -> None:
    manifest = bt.load_manifest(bt.ROOT / "tools" / "third_party_sources.json")
    for entry in manifest["archives"]:
        assert len(entry["sha256"]) == 64 and entry["size"] > 0, entry["file"]
        assert entry["url"].startswith("https://"), entry["file"]
        assert entry["covers"], entry["file"]
        assert entry.get("wheel", {}).get("distribution"), entry["file"]
    files = {e["file"] for e in manifest["archives"]}
    for needed in ("pyqt6-6.11.0.tar.gz", "qtbase-everywhere-src-6.11.2.tar.xz", "libheif-1.23.4.tar.gz",
                   "mingw-w64-x265-4.3-1.src.tar.zst", "mingw-w64-gcc-16.2.0-4.src.tar.zst"):
        assert needed in files, needed
    # Covers of every copyleft binary that the known layout has.
    for rel in ("_internal/PyQt6/QtCore.pyd", "_internal/PyQt6/Qt6/bin/Qt6Gui.dll",
                "_internal/PyQt6/Qt6/plugins/imageformats/qwebp.dll", "_internal/PyQt6/Qt6/translations/qtbase_nl.qm",
                "_internal/libde265-0-863aced21c291386b51bbbd6c57331e8.dll",
                "_internal/libstdc++-6-a168feb806be6a6b9920422b91e14e6f.dll"):
        assert bt._match_any(rel, manifest["copyleft_files"]), rel
        assert any(bt._match_any(rel, e["covers"]) for e in manifest["archives"]), rel


def test_fetch_verifies_checksums() -> None:
    folder = _fresh("fetch")
    upstream = folder / "upstream.tar.gz"
    upstream.write_bytes(b"archive bytes" * 1000)
    digest = bt.sha256_file(upstream)
    cache = folder / "cache"
    entry = {"name": "x", "file": "x-1.0.tar.gz", "url": upstream.as_uri(), "sha256": digest,
             "size": upstream.stat().st_size, "licence": "MIT", "covers": ["x"]}
    got = bt.fetch_archive(entry, cache)
    assert got.read_bytes() == upstream.read_bytes()
    assert not list(cache.glob("*.part"))
    # A corrupted cache entry is replaced by a fresh download.
    got.write_bytes(b"corrupt")
    assert bt.fetch_archive(entry, cache).read_bytes() == upstream.read_bytes()
    # A download that does not match the pin is refused and not kept.
    wrong = dict(entry, file="y-1.0.tar.gz", sha256="0" * 64)
    try:
        bt.fetch_archive(wrong, cache)
    except bt.BuildError as exc:
        assert "does not match" in str(exc)
    else:
        raise AssertionError("a wrong SHA-256 was accepted")
    assert not (cache / "y-1.0.tar.gz").exists() and not (cache / "y-1.0.tar.gz.part").exists()
    # Unpinned entries are refused unless pinning was asked for.
    try:
        bt.fetch_archive(dict(entry, sha256=""), cache)
    except bt.BuildError as exc:
        assert "no SHA-256" in str(exc)
    else:
        raise AssertionError("an unpinned archive was accepted")


def test_third_party_zip_stores_archives_unchanged() -> None:
    folder = _fresh("third")
    upstream = folder / "a.tar.xz"
    upstream.write_bytes(os.urandom(50_000))
    manifest = {
        "copyleft_files": [],
        "notice_only_files": [],
        "archives": [{"name": "A 1.0", "file": "a-1.0.tar.xz", "url": upstream.as_uri(),
                      "sha256": bt.sha256_file(upstream), "size": upstream.stat().st_size,
                      "licence": "LGPL-3.0-only", "covers": ["x"]}],
    }
    mpath = folder / "manifest.json"
    mpath.write_text(json.dumps(manifest), encoding="utf-8")
    out = folder / "BackgroundEditor-9.9.9-third-party-sources.zip"
    assert bt.main(["third-party", "--manifest", str(mpath), "--cache", str(folder / "cache"),
                    "--version", "9.9.9", "--out", str(out)]) == 0
    with zipfile.ZipFile(out) as zf:
        info = zf.getinfo("BackgroundEditor-9.9.9-third-party-sources/a-1.0.tar.xz")
        assert info.compress_type == zipfile.ZIP_STORED
        assert zf.read(info) == upstream.read_bytes()
        readme = zf.read("BackgroundEditor-9.9.9-third-party-sources/SOURCES.txt").decode()
        assert bt.sha256_file(upstream) in readme and "LGPL-3.0-only" in readme


def test_portable_zip() -> None:
    folder = _fresh("portable")
    app = folder / "BackgroundEditor"
    (app / "_internal").mkdir(parents=True)
    (app / "BackgroundEditor.exe").write_bytes(b"MZ")
    (app / "_internal" / "python314.dll").write_bytes(b"MZ")
    (app / "LICENSE").write_text("GPL", encoding="utf-8")
    out = folder / "BackgroundEditor-9.9.9-portable.zip"
    assert bt.main(["portable", "--app-dir", str(app), "--version", "9.9.9", "--vc-min", "14.51",
                    "--out", str(out)]) == 0
    marker = bt._string_constant(bt.ROOT / "bgeditor" / "paths.py", "PORTABLE_MARKER_TEXT")
    with zipfile.ZipFile(out) as zf:
        names = set(zf.namelist())
        assert {"BackgroundEditor/BackgroundEditor.exe", "BackgroundEditor/_internal/python314.dll",
                "BackgroundEditor/LICENSE", "BackgroundEditor/portable.conf",
                "BackgroundEditor/models/README.txt", "BackgroundEditor/README-portable.txt"} <= names
        assert zf.read("BackgroundEditor/portable.conf").decode("utf-8") == marker
        readme = zf.read("BackgroundEditor/README-portable.txt").decode("utf-8")
        assert "14.51" in readme and "vc_redist" in readme and "\r" not in readme
    # The app folder itself must never hold portable.conf.
    (app / "portable.conf").write_text("x", encoding="utf-8")
    assert bt.main(["portable", "--app-dir", str(app), "--version", "9.9.9", "--vc-min", "14.51",
                    "--out", str(out)]) == 1


def test_release_manifest() -> None:
    """The release manifest: data only (comments and one single-quoted here-string with
    JSON), ASCII with LF line ends, and what the app's own parser reads back matches the
    installer, the portable zip and every file in it."""
    sys.path.insert(0, str(bt.ROOT))
    from bgeditor import app_updates

    folder = _fresh("manifest")
    app = folder / "BackgroundEditor"
    (app / "_internal" / "sub").mkdir(parents=True)
    (app / "BackgroundEditor.exe").write_bytes(b"MZ exe")
    (app / "_internal" / "python314.dll").write_bytes(b"MZ dll")
    (app / "_internal" / "sub" / "module.pyd").write_bytes(b"MZ pyd")
    (app / "LICENSE").write_text("GPL", encoding="utf-8")
    zip_path = folder / "BackgroundEditor-9.9.9-portable.zip"
    assert bt.main(["portable", "--app-dir", str(app), "--version", "9.9.9", "--vc-min", "14.51",
                    "--out", str(zip_path)]) == 0
    setup = folder / "BackgroundEditor-Setup-9.9.9.exe"
    setup.write_bytes(b"MZ setup")
    out = folder / "BackgroundEditor-9.9.9-manifest.ps1"
    common = ["--version", "9.9.9", "--min-update-from", "1.1.0", "--portable", str(zip_path)]
    assert bt.main(["release-manifest", *common, "--setup", str(setup), "--out", str(out)]) == 0
    data = out.read_bytes()
    text = data.decode("ascii")
    assert "\r" not in text and text.startswith("# Background Editor release manifest - DATA ONLY")
    code = [line for line in text.split("\n") if line and not line.startswith("#")]
    assert code[0] == "$BackgroundEditorManifest = @'" and code[-1] == "'@", code[:1] + code[-1:]
    m = app_updates.parse_manifest(data, signed=False)
    assert m.version == "9.9.9" and m.min_update_from == "1.1.0"
    assert (m.setup.name, m.setup.sha256, m.setup.size) == (setup.name, bt.sha256_file(setup), setup.stat().st_size)
    assert (m.portable.sha256, m.portable.size) == (bt.sha256_file(zip_path), zip_path.stat().st_size)
    listed = {f.path: (f.sha256, f.size) for f in m.portable.files}
    with zipfile.ZipFile(zip_path) as zf:
        in_zip = {n.split("/", 1)[1]: (hashlib.sha256(zf.read(n)).hexdigest(), len(zf.read(n))) for n in zf.namelist()}
    assert listed == in_zip and set(bt.PORTABLE_EXTRAS) <= set(listed)
    # The check after the build compares it with the files (unsigned here: parse only).
    check = ["verify-manifest", "--unsigned", "--manifest", str(out), "--app-dir", str(app), *common, "--setup", str(setup)]
    assert bt.main(check) == 0
    (app / "_internal" / "python314.dll").write_bytes(b"MZ changed")
    assert bt.main(check) == 1
    (app / "_internal" / "python314.dll").write_bytes(b"MZ dll")
    setup.write_bytes(b"MZ other setup")
    assert bt.main(check) == 1
    # Names and versions must fit together.
    assert bt.main(["release-manifest", *common, "--setup", str(folder / "Other.exe"), "--out", str(out)]) == 1
    assert bt.main(["release-manifest", "--version", "9.9.9", "--min-update-from", "one", "--portable", str(zip_path),
                    "--out", str(out)]) == 1
    assert bt.main(["release-manifest", "--version", "9.9.8", "--portable", str(zip_path), "--out", str(out)]) == 1
    # Without an installer (build.ps1 -NoInstaller).
    assert bt.main(["release-manifest", *common, "--out", str(out)]) == 0
    assert app_updates.parse_manifest(out.read_bytes(), signed=False).setup is None
    # A file name with '~' (which the app refuses: it could be an 8.3 short name) stops the build.
    (app / "_internal" / "odd~1.dll").write_bytes(b"MZ odd")
    tilde_zip = folder / "tilde" / "BackgroundEditor-9.9.9-portable.zip"
    tilde_zip.parent.mkdir()
    assert bt.main(["portable", "--app-dir", str(app), "--version", "9.9.9", "--vc-min", "14.51",
                    "--out", str(tilde_zip)]) == 0
    assert bt.main(["release-manifest", "--version", "9.9.9", "--portable", str(tilde_zip), "--out", str(out)]) == 1


def main() -> int:
    tests = [(name, fn) for name, fn in sorted(globals().items()) if name.startswith("test_") and callable(fn)]
    failed = 0
    try:
        for name, fn in tests:
            try:
                fn()
                print(f"ok    {name}")
            except Exception:  # noqa: BLE001 - report every failure
                failed += 1
                print(f"FAIL  {name}")
                traceback.print_exc()
    finally:
        _remove(WORK)
    print(f"{len(tests) - failed} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())

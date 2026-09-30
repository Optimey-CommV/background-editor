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

# PyInstaller spec for Background Editor (one-folder build; the installer wraps it).
# Build with build.ps1, which also runs the bundle checks, signs the result, makes the
# source zips and the portable zip, and runs Inno Setup.

import ast
import csv
import hashlib
import importlib.metadata
import os
import pathlib
import re
import shutil
import sys

import pefile
from PyInstaller.utils.win32.versioninfo import (
    FixedFileInfo,
    StringFileInfo,
    StringStruct,
    StringTable,
    VarFileInfo,
    VarStruct,
    VSVersionInfo,
)


# ------------------------------------------------------------------ version
def _source_version():
    """__version__ from bgeditor/__init__.py, the single source of the version."""
    path = os.path.join(SPECPATH, "bgeditor", "__init__.py")
    with open(path, encoding="utf-8") as fh:
        tree = ast.parse(fh.read(), path)
    for node in tree.body:
        if (
            isinstance(node, ast.Assign)
            and any(isinstance(t, ast.Name) and t.id == "__version__" for t in node.targets)
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
        ):
            return node.value.value
    raise SystemExit(f"ERROR: no __version__ = \"...\" found in {path}")


def _numeric_version(text):
    """'1.2.0-beta.1' -> (1, 2, 0, 0): the leading dotted numbers, for the Windows version resource."""
    m = re.match(r"\d+(?:\.\d+){0,3}", text.strip())
    parts = [min(int(p), 65535) for p in m.group().split(".")] if m else []
    return tuple(parts + [0] * (4 - len(parts)))


SOURCE_VERSION = _source_version()
# build.ps1 -Version passes an override (for example a pre-release) in BGR_VERSION.
VERSION = os.environ.get("BGR_VERSION", "").strip() or SOURCE_VERSION
_v = _numeric_version(VERSION)

RUNTIME_HOOKS = []
if VERSION != SOURCE_VERSION:
    # Let About and the download User-Agent show the overridden version as well.
    _hook = os.path.join(workpath, "pyi_rth_bgr_version.py")
    with open(_hook, "w", encoding="utf-8", newline="") as fh:
        fh.write(f"import bgeditor\n\nbgeditor.__version__ = {VERSION!r}\n")
    RUNTIME_HOOKS.append(_hook)
print(f"Background Editor version {VERSION} (file version {'.'.join(map(str, _v))})")

COMPANY = "Optimey CommV"
# The source is GPL-3.0-or-later; this build includes PyQt6 (GPL-3.0-only), so the program
# as distributed is GPL v3 (LICENSES.txt).
COPYRIGHT = "Copyright (C) 2026 Optimey CommV. GPL-3.0."

version_info = VSVersionInfo(
    ffi=FixedFileInfo(filevers=_v, prodvers=_v, mask=0x3F, flags=0x0, OS=0x40004, fileType=0x1, subtype=0x0),
    kids=[
        StringFileInfo(
            [
                StringTable(
                    "040904B0",
                    [
                        StringStruct("CompanyName", COMPANY),
                        StringStruct("FileDescription", "Background Editor"),
                        StringStruct("FileVersion", VERSION),
                        StringStruct("InternalName", "BackgroundEditor"),
                        StringStruct("LegalCopyright", COPYRIGHT),
                        StringStruct("OriginalFilename", "BackgroundEditor.exe"),
                        StringStruct("ProductName", "Background Editor"),
                        StringStruct("ProductVersion", VERSION),
                    ],
                )
            ]
        ),
        VarFileInfo([VarStruct("Translation", [0x0409, 1200])]),
    ],
)

# Parts of dependencies that the app never imports. PyQt6.QtNetwork must stay: app.py
# uses QLocalServer/QLocalSocket for the single-instance hand-over. The last group must
# never be bundled at all (OpenCV links Intel IPP; AVIF brings aom/dav1d; rembg would drag
# in numba and pymatting); excluding them makes a stray import fail the self-test.
EXCLUDES = [
    "tkinter",
    "sympy",
    "mpmath",
    "onnxruntime.tools",
    "onnxruntime.transformers",
    "onnxruntime.quantization",
    "onnxruntime.training",
    "PyQt6.QtQml",
    "PyQt6.QtQuick",
    "PyQt6.QtPdf",
    "PyQt6.QtPdfWidgets",
    "PyQt6.QtMultimedia",
    "PyQt6.QtOpenGL",
    # unittest and pydoc must stay: SciPy's array API layer imports pydoc and walks numpy's
    # namespace, which imports numpy.testing and with it unittest.
    # SciPy: the app uses scipy.ndimage, which needs scipy._lib, scipy.linalg and
    # scipy.special at run time. The rest is only reachable through imports inside
    # functions the app never calls (measured: the imaging and engine tests pass with
    # these blocked); leaving them out saves about 40 MB.
    "scipy.cluster",
    "scipy.constants",
    "scipy.datasets",
    "scipy.differentiate",
    "scipy.fft",
    "scipy.fftpack",
    "scipy.integrate",
    "scipy.interpolate",
    "scipy.io",
    "scipy.misc",
    "scipy.odr",
    "scipy.optimize",
    "scipy.signal",
    "scipy.sparse",
    "scipy.spatial",
    "scipy.stats",
    "cv2",
    "PIL.AvifImagePlugin",
    "PIL._avif",
    "rembg",
    "pymatting",
    "numba",
]

a = Analysis(
    ["app.py"],
    pathex=[],
    binaries=[],
    datas=[
        ("assets/app.ico", "assets"),
        ("assets/logo_64.png", "assets"),
        ("assets/logo_128.png", "assets"),
        ("assets/logo_256.png", "assets"),
        ("assets/logo_512.png", "assets"),
        # Test portrait and reference mask for the model-update check (CC0, see LICENSES.txt).
        ("assets/selftest_portrait.jpg", "assets"),
        ("assets/selftest_reference.png", "assets"),
    ],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=RUNTIME_HOOKS,
    excludes=EXCLUDES,
    noarchive=False,
    optimize=1,
)


# ------------------------------------------------------ files left out of the bundle
# The distributed program is GPL v3 (PyQt6 is GPL-3.0-only), so everything in it must be
# GPL-compatible or a Windows system component. build.ps1 (tools/build_tools.py
# check-bundle) repeats these checks on the finished folder and fails the build.
_VC_RUNTIME = re.compile(r"^(vcruntime|msvcp|concrt|vccorlib|vcomp|vcamp|msvcr)\d", re.I)


def _exclusion_reason(dest):
    low = dest.replace("\\", "/").lower()
    name = low.rsplit("/", 1)[-1]
    if name == "directml.dll":
        return "DirectML redistributable: Windows' own System32 copy is used"
    if _VC_RUNTIME.match(name) or name == "ucrtbase.dll" or name.startswith("api-ms-win-"):
        return "Microsoft C/C++ runtime: installed by Microsoft's vc_redist / part of Windows"
    if name.startswith("d3dcompiler_"):
        return "Direct3D compiler: part of Windows"
    if name.startswith("qt6pdf") or name == "qpdf.dll":
        return "Qt PDF: not used"
    if "/plugins/tls/" in low or "/plugins/networkinformation/" in low:
        return "Qt TLS and network information plugins: only QLocalSocket is used"
    if name in ("libssl-3-x64.dll", "libcrypto-3-x64.dll"):
        return "OpenSSL for the Qt TLS plugin: not used"
    if low.startswith("cv2/") or name.startswith("cv2") or "opencv" in name:
        return "OpenCV: removed (Intel IPP)"
    if low.startswith("pil/_avif"):
        return "Pillow AVIF (aom, dav1d): not used"
    return None


def _filter(toc, label):
    kept, dropped = [], {}
    for entry in toc:
        reason = _exclusion_reason(entry[0])
        if reason:
            dropped.setdefault(reason, []).append(entry[0])
        else:
            kept.append(entry)
    for reason, names in sorted(dropped.items()):
        print(f"Excluded {label} ({reason}): {', '.join(sorted(names))}")
    return kept


a.binaries = _filter(a.binaries, "binaries")
a.datas = _filter(a.datas, "data files")

# delvewheel gives the VC++ runtime inside a wheel a unique name (numpy.libs\msvcp140-<hash>.dll)
# and points the extension modules at that name. The renamed copy is not shipped (see above),
# so those imports are pointed back at the standard name, which Windows resolves to the
# system-wide runtime. The new name is shorter, so it fits in place.
_MANGLED_VC = re.compile(r"^((?:vcruntime|msvcp|concrt|vccorlib)140(?:_\w+)?)-[0-9a-f]{32}\.dll$", re.I)


def _unmangle_vc_imports(src, dest):
    """Path of a patched copy of src, or None when src imports no renamed VC++ runtime."""
    pe = pefile.PE(src, fast_load=True)
    try:
        pe.parse_data_directories(
            directories=[
                pefile.DIRECTORY_ENTRY["IMAGE_DIRECTORY_ENTRY_IMPORT"],
                pefile.DIRECTORY_ENTRY["IMAGE_DIRECTORY_ENTRY_DELAY_IMPORT"],
            ]
        )
        patches = []
        for imp in getattr(pe, "DIRECTORY_ENTRY_IMPORT", []):
            patches.append((imp.dll, imp.struct.Name))
        for imp in getattr(pe, "DIRECTORY_ENTRY_DELAY_IMPORT", []):
            if imp.struct.grAttrs & 1:  # RVA-based descriptor
                patches.append((imp.dll, imp.struct.szName))
        patches = [(old, rva) for old, rva in patches if _MANGLED_VC.match(old.decode("ascii", "replace"))]
        if not patches:
            return None
        data = bytearray(pe.__data__)
        for old, rva in patches:
            new = _MANGLED_VC.match(old.decode("ascii")).group(1).encode("ascii") + b".dll"
            offset = pe.get_offset_from_rva(rva)
            if bytes(data[offset:offset + len(old)]) != old:
                raise SystemExit(f"ERROR: unexpected import table layout in {src}")
            data[offset:offset + len(old) + 1] = new + b"\0" * (len(old) + 1 - len(new))
            print(f"Patched import {old.decode()} -> {new.decode()} in {dest}")
    finally:
        pe.close()
    patched = pefile.PE(data=bytes(data))
    if patched.OPTIONAL_HEADER.CheckSum:
        patched.OPTIONAL_HEADER.CheckSum = patched.generate_checksum()
    out = os.path.join(workpath, "unmangled", dest)
    os.makedirs(os.path.dirname(out), exist_ok=True)
    patched.write(out)
    patched.close()
    return out


_binaries = []
for _dest, _src, _type in a.binaries:
    if _dest.lower().endswith((".dll", ".pyd")):
        _src = _unmangle_vc_imports(_src, _dest) or _src
    _binaries.append((_dest, _src, _type))
a.binaries = _binaries

# Everything must come from this venv, the Python installation or the patched copies above.
# PyInstaller resolves missing DLLs along PATH, which can silently pull in unrelated copies
# (for example an OpenSSL from another program).
# realpath: on a mapped network drive PyInstaller reports some files by their UNC path.
def _real(path):
    return os.path.normcase(os.path.realpath(path))


_allowed_roots = [_real(p) + os.sep for p in (sys.prefix, sys.base_prefix, workpath, SPECPATH)]
_strays = [
    f"{dest} <- {src}"
    for dest, src, _ in list(a.binaries) + list(a.datas)
    if not any(_real(src).startswith(root) for root in _allowed_roots)
]
if _strays:
    raise SystemExit("ERROR: files from outside the venv and Python would be bundled:\n  " + "\n  ".join(_strays))

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="BackgroundEditor",
    icon="assets/app.ico",
    version=version_info,
    console=False,
    disable_windowed_traceback=False,
    upx=False,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="BackgroundEditor",
)


# ----------------------------------------------------------------- licences
# LICENSE (GPL v3, the licence of the program as a whole) and LICENSES.txt go next to
# BackgroundEditor.exe, where people can find them. licenses\LGPL-3.0.txt is the LGPL v3
# (Qt, libheif, libde265). The licence files of every bundled distribution go to
# licenses\<distribution>\, so the notices for the libraries bundled inside the wheels
# (OpenBLAS in NumPy and SciPy, libjpeg in Pillow, libheif in pillow-heif, ...) ship with
# the exact versions built in. build.ps1 adds licenses\Qt6 (notices of the code inside Qt)
# and source\ (the source zip).

_LICENCE_NAME = re.compile(r"(licen[cs]e|copying|notice|authors|thirdpartynotices|third[-_]party)", re.I)
_NOT_LICENCE = {".py", ".pyc", ".pyi", ".pyd", ".dll", ".so", ".json", ".h", ".c"}


def _bundled_top_names(*tocs):
    """Top-level package or file names of everything in the bundle."""
    names = set()
    for toc in tocs:
        for entry in toc:
            name, typecode = entry[0], entry[2]
            if typecode == "PYMODULE":
                names.add(name.split(".")[0])
            else:
                names.add(name.replace("\\", "/").split("/")[0])
    return names


def _bundled_dirs(*tocs):
    """Every package folder with something in the bundle ('scipy', 'scipy/ndimage', ...)."""
    dirs = set()
    for toc in tocs:
        for entry in toc:
            name, typecode = entry[0], entry[2]
            if typecode == "PYMODULE":
                parts = name.split(".")[:-1] if not entry[1] or not str(entry[1]).endswith("__init__.py") else name.split(".")
            else:
                parts = name.replace("\\", "/").split("/")[:-1]
            for i in range(1, len(parts) + 1):
                dirs.add("/".join(parts[:i]).lower())
    return dirs


def _record_paths(dist):
    """Paths listed in a distribution's RECORD. Unlike dist.files this does not stat every
    file, which takes minutes when the venv is on a network share."""
    for row in csv.reader((dist.read_text("RECORD") or "").splitlines()):
        if row and row[0]:
            yield pathlib.PurePosixPath(row[0])


def _licence_files(dist, bundled_dirs):
    """(relative destination, source path) for each licence file a distribution installed:
    its dist-info licences, those next to a package's __init__, and those in sub-packages
    that are part of the bundle (for example scipy/_lib/_uarray/LICENSE)."""
    found = []
    for f in _record_paths(dist):
        parts = f.parts
        if not parts or parts[0] == "..":
            continue
        if parts[0].endswith(".dist-info") and len(parts) >= 3 and parts[1] == "licenses":
            rel = parts[2:]  # PEP 639 licence folder; keeps its sub-folders
        elif _LICENCE_NAME.match(parts[-1]) and os.path.splitext(parts[-1])[1].lower() not in _NOT_LICENCE:
            if len(parts) == 2:
                # In the dist-info folder, or next to a package's __init__ (onnxruntime).
                rel = parts[1:] if parts[0].endswith(".dist-info") else parts
            elif len(parts) > 2 and not parts[0].endswith(".dist-info") and "/".join(parts[:-1]).lower() in bundled_dirs:
                rel = parts
            else:
                continue
        else:
            continue
        src = str(dist.locate_file(f))
        if os.path.isfile(src):
            found.append((rel, src))
    return found


def _copy_licences(app_dir, tocs):
    dest_root = os.path.join(app_dir, "licenses")
    shutil.rmtree(dest_root, ignore_errors=True)
    top_names = _bundled_top_names(*tocs)
    bundled_dirs = _bundled_dirs(*tocs)

    owners = {}  # top-level name -> distributions that installed it
    for dist in importlib.metadata.distributions():
        for f in _record_paths(dist):
            if f.parts and f.parts[0] != ".." and not f.parts[0].endswith(".dist-info"):
                top = f.parts[0][:-3] if f.parts[0].endswith(".py") else f.parts[0]
                owners.setdefault(top, {})[dist.metadata["Name"]] = dist

    bundled = {}
    for top in top_names:
        bundled.update(owners.get(top, {}))
    # Not found through the module list: the bootloader and runtime hooks, and Python itself.
    for extra in ("pyinstaller",):
        try:
            dist = importlib.metadata.distribution(extra)
            bundled[dist.metadata["Name"]] = dist
        except importlib.metadata.PackageNotFoundError:
            pass
    stray = sorted(n for n in bundled if n.lower().replace("_", "-") in ("opencv-python-headless", "opencv-python", "rembg"))
    if stray:
        raise SystemExit(f"ERROR: {', '.join(stray)} ended up in the bundle")

    copies = [
        ("Python", ("LICENSE.txt",), os.path.join(sys.base_prefix, "LICENSE.txt")),
    ]
    missing = []
    for name, dist in sorted(bundled.items(), key=lambda kv: kv[0].lower()):
        files = _licence_files(dist, bundled_dirs)
        if not files:
            missing.append(name)
        copies += [(name, rel, src) for rel, src in files]

    seen = set()
    for name, rel, src in copies:
        with open(src, "rb") as fh:
            digest = hashlib.sha256(fh.read()).hexdigest()
        if (name, digest) in seen:
            continue  # the same text installed twice (for example in a package and its dist-info)
        seen.add((name, digest))
        dest = os.path.join(dest_root, name, *rel)
        while os.path.exists(dest):
            dest += "_"
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        shutil.copyfile(src, dest)
    print(f"Licence files of {len(bundled) + 1} distributions copied to {dest_root}")
    if missing:
        print("WARNING: no licence file shipped by: " + ", ".join(missing) + " (see LICENSES.txt)")


for _name, _src in (
    ("LICENSE", "LICENSE"),
    ("LICENSES.txt", "LICENSES.txt"),
):
    shutil.copyfile(os.path.join(SPECPATH, _src), os.path.join(coll.name, _name))
_copy_licences(coll.name, (a.pure, a.binaries, a.datas))
shutil.copyfile(
    os.path.join(SPECPATH, "tools", "licenses", "LGPL-3.0.txt"), os.path.join(coll.name, "licenses", "LGPL-3.0.txt")
)

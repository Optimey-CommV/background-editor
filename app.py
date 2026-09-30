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

"""Entry point for Background Editor."""

from __future__ import annotations

import base64
import ctypes
import fnmatch
import getpass
import hashlib
import io
import json
import os
import sys
import threading
import time
import traceback
from ctypes import wintypes

# A later launch (Explorer starts one process per selected photo) keeps trying this long to
# reach the window of the first one, which may still be loading on a cold start.
HANDOVER_WAIT_S = 120.0
ACK = b"ok\n"
MAX_MESSAGE = 4 << 20

_instance_lock = None  # QLockFile held for the whole life of the process that owns the window


def _prepare_windows() -> None:
    # Own taskbar identity, so Windows shows our icon instead of the Python one.
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("BackgroundEditor.App.1")
    except (AttributeError, OSError):
        pass
    # A windowed exe has no console; give print()/tracebacks somewhere harmless to go.
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w", encoding="utf-8")
    if sys.stderr is None:
        sys.stderr = _log_file()


def _data_dir() -> str:
    from bgeditor import paths

    return str(paths.data_dir())


# The app was called Background Remover before version 1.1.
LEGACY_NAME = "BackgroundRemover"


def _migrate_legacy_data() -> None:
    """Move the downloaded models and other data from the old app folder.

    Same volume, so this is a rename, not a copy of gigabytes. When both folders exist
    (the old app ran after the new one), models missing from the new folder are moved.
    """
    from bgeditor import paths

    if paths.is_portable():
        return  # a portable copy keeps its own data next to the exe
    old, new = os.path.join(str(paths.user_data_dir().parent), LEGACY_NAME), str(paths.user_data_dir())
    if not os.path.isdir(old):
        return
    try:
        if not os.path.exists(new):
            os.replace(old, new)
            return
        old_models, new_models = os.path.join(old, "models"), os.path.join(new, "models")
        if os.path.isdir(old_models):
            os.makedirs(new_models, exist_ok=True)
            for name in os.listdir(old_models):
                src, dst = os.path.join(old_models, name), os.path.join(new_models, name)
                if not name.lower().endswith(".onnx"):
                    continue
                if not os.path.exists(dst):
                    os.replace(src, dst)
                elif _same_file_content(src, dst):
                    os.remove(src)  # a verified duplicate of a model we already have
        _remove_leftovers(old)
    except OSError:
        pass  # a file in use (old app still running): try again next start


def _same_file_content(a: str, b: str) -> bool:
    if os.path.getsize(a) != os.path.getsize(b):
        return False
    digests = []
    for path in (a, b):
        h = hashlib.sha256()
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 22), b""):
                h.update(chunk)
        digests.append(h.digest())
    return digests[0] == digests[1]


def _remove_leftovers(old: str) -> None:
    """Remove the old folder only when nothing but our own empty leftovers remain."""
    removable = {"instance.lock", "error.log", "gpu-compat.json"}
    old_models = os.path.join(old, "models")
    if os.path.isdir(old_models) and not os.listdir(old_models):
        os.rmdir(old_models)
    for name in os.listdir(old):
        path = os.path.join(old, name)
        if name not in removable or not os.path.isfile(path):
            return  # something else is there: leave the folder alone
        if name == "error.log" and os.path.getsize(path) > 0:
            return
    for name in os.listdir(old):
        os.remove(os.path.join(old, name))
    os.rmdir(old)


def _migrate_legacy_settings() -> None:
    """Copy the settings of the old app name once, when the new name has none yet."""
    from PyQt6.QtCore import QSettings

    from bgeditor import paths

    if paths.is_portable():
        return
    new = paths.settings()
    if new.allKeys():
        return
    old = QSettings(LEGACY_NAME, LEGACY_NAME)
    for key in old.allKeys():
        new.setValue(key, old.value(key))
    new.sync()


def _log_path() -> str:
    return os.path.join(_data_dir(), "error.log")


def _log_file():
    try:
        os.makedirs(_data_dir(), exist_ok=True)
        # Line-buffered: a crash must not take the last lines with it.
        return open(_log_path(), "a", encoding="utf-8", errors="backslashreplace", buffering=1)
    except OSError:
        return open(os.devnull, "w", encoding="utf-8")


# ------------------------------------------------------------------ errors
_shown_errors: set[str] = set()
_showing_error = False


def _log_error(text: str) -> None:
    block = f"--- {time.strftime('%Y-%m-%d %H:%M:%S')} unexpected error ---\n{text}"
    in_log = False
    try:
        if sys.stderr is not None:
            sys.stderr.write(block)
            sys.stderr.flush()
            in_log = os.path.normcase(getattr(sys.stderr, "name", "") or "") == os.path.normcase(_log_path())
    except (OSError, ValueError):
        pass
    if not in_log:  # running with a console: keep a copy where users can find it
        try:
            os.makedirs(_data_dir(), exist_ok=True)
            with open(_log_path(), "a", encoding="utf-8", errors="backslashreplace") as fh:
                fh.write(block)
        except OSError:
            pass


def _show_error(summary: str, details: str) -> None:
    global _showing_error
    if _showing_error or summary in _shown_errors:
        return
    try:
        from PyQt6.QtWidgets import QApplication, QMessageBox
    except ImportError:
        return
    if not isinstance(QApplication.instance(), QApplication):
        return
    _shown_errors.add(summary)
    _showing_error = True
    try:
        box = QMessageBox(
            QMessageBox.Icon.Warning,
            "Background Editor",
            "Something went wrong inside the app. You can keep working; restart it if something looks wrong.",
        )
        box.setInformativeText(f"{summary}\n\nThe details were saved in {_log_path()}")
        box.setDetailedText(details)
        box.exec()
    finally:
        _showing_error = False


def _excepthook(exc_type, exc, tb) -> None:
    """Log instead of letting PyQt abort the process on an error in a slot."""
    if issubclass(exc_type, KeyboardInterrupt):
        sys.__excepthook__(exc_type, exc, tb)
        return
    text = "".join(traceback.format_exception(exc_type, exc, tb))
    _log_error(text)
    if threading.current_thread() is threading.main_thread():
        _show_error(f"{exc_type.__name__}: {exc}"[:300], text)


def _thread_excepthook(args) -> None:
    if args.exc_type is not None and not issubclass(args.exc_type, SystemExit):
        _excepthook(args.exc_type, args.exc_value, args.exc_traceback)


def _install_excepthooks() -> None:
    sys.excepthook = _excepthook
    threading.excepthook = _thread_excepthook


# --------------------------------------------------------------- hand-over
def _server_name() -> str:
    try:
        user = getpass.getuser()
    except Exception:
        user = "user"
    # One window per data location: a portable copy and the installed app stay apart.
    from bgeditor import paths

    tag = hashlib.sha1(str(paths.data_dir()).lower().encode("utf-8")).hexdigest()[:8]
    return f"BackgroundEditor-{user}-{tag}"


def _allow_foreground() -> None:
    # Explorer let this process bring a window forward; pass that on to the running window,
    # otherwise Windows only flashes its taskbar button.
    try:
        ctypes.windll.user32.AllowSetForegroundWindow(-1)  # ASFW_ANY
    except (AttributeError, OSError):
        pass


def _send(payload: bytes) -> bool:
    """One attempt to give the running window our photos; True once it confirmed receipt."""
    from PyQt6.QtNetwork import QLocalSocket

    sock = QLocalSocket()
    sock.connectToServer(_server_name())
    if not sock.waitForConnected(500):
        sock.abort()
        return False
    sock.write(payload)
    sock.waitForBytesWritten(2000)  # may say False while the other side is busy; the reply decides
    reply = b""
    deadline = time.monotonic() + 3.0
    while not reply.endswith(b"\n"):
        left = int((deadline - time.monotonic()) * 1000)
        if left <= 0 or not sock.waitForReadyRead(left):
            break
        reply += bytes(sock.readAll())
    sock.abort()
    return reply == ACK


def _hand_over(paths: list[str]) -> str:
    """'sent' when a running window took the paths, 'owner' when this process holds the
    instance lock and must open the window, 'gave-up' when neither happened in time.

    Works without a QApplication, so the extra processes Explorer starts stay light.
    """
    global _instance_lock
    from PyQt6.QtCore import QLockFile

    payload = (json.dumps(paths) + "\n").encode("utf-8")
    _allow_foreground()
    if _send(payload):
        return "sent"
    os.makedirs(_data_dir(), exist_ok=True)
    lock = QLockFile(os.path.join(_data_dir(), "instance.lock"))
    # Stale only when the owning process is gone, however long the window has been open.
    lock.setStaleLockTime(0)
    deadline = time.monotonic() + HANDOVER_WAIT_S
    while True:
        if lock.tryLock(0):
            _instance_lock = lock
            return "owner"
        # Another process owns the window; it answers once it is listening (see _HandOverServer).
        if _send(payload):
            return "sent"
        if time.monotonic() > deadline:
            print(
                f"Hand-over: the running window did not answer within {HANDOVER_WAIT_S:.0f} s; "
                f"{len(paths)} photo(s) not added.",
                file=sys.stderr,
            )
            return "gave-up"
        time.sleep(0.15)


def _parse_paths(raw: bytes) -> list[str]:
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return []
    if not isinstance(data, list):
        return []
    return [p for p in data if isinstance(p, str) and p]


class _HandOverServer:
    """Receives photos from later launches.

    It listens as soon as this process owns the instance lock, before the slow imports and
    the window; whatever arrives meanwhile is kept until the window exists.
    """

    def __init__(self, parent) -> None:
        from PyQt6.QtNetwork import QLocalServer

        self._window = None
        self._queued: list[str] = []
        self._wants_front = False
        self._closed = False
        self._server = QLocalServer(parent)
        self._server.setSocketOptions(QLocalServer.SocketOption.UserAccessOption)
        self._server.newConnection.connect(self._on_connection)
        if not self._server.listen(_server_name()):
            print(f"Hand-over: cannot listen: {self._server.errorString()}", file=sys.stderr)

    def attach(self, window) -> None:
        self._window = window
        window.closing.connect(self.close)
        queued, self._queued = self._queued, []
        if queued or self._wants_front:
            window.receive_paths(queued)

    def close(self) -> None:
        # The window is closing; a new launch then waits for the lock and opens its own window.
        self._closed = True
        self._server.close()

    def _on_connection(self) -> None:
        while self._server.hasPendingConnections():
            sock = self._server.nextPendingConnection()
            if sock is None:
                break
            self._serve(sock)

    def _serve(self, sock) -> None:
        buf = bytearray()
        state = {"done": False}

        def on_ready() -> None:
            data = bytes(sock.readAll())
            if state["done"]:
                return
            buf.extend(data)
            if len(buf) > MAX_MESSAGE:
                state["done"] = True
                sock.abort()
                return
            if b"\n" not in buf:
                return
            state["done"] = True
            if self._deliver(_parse_paths(bytes(buf).split(b"\n", 1)[0])):
                sock.write(ACK)
                sock.flush()
            else:
                sock.abort()  # closing: the sender keeps trying and opens the next window

        sock.readyRead.connect(on_ready)
        sock.disconnected.connect(sock.deleteLater)
        on_ready()  # data may have arrived before the connection was picked up

    def _deliver(self, paths: list[str]) -> bool:
        if self._closed:
            return False
        if self._window is not None:
            self._window.receive_paths(paths)
        else:
            self._queued.extend(paths)
            self._wants_front = True
        return True


def _import_main_window():
    """Import the UI module (NumPy, SciPy, ONNX Runtime: seconds on a cold start) on a helper
    thread, while this thread keeps answering the other processes Explorer started."""
    from PyQt6.QtCore import QCoreApplication, QEventLoop

    result: dict = {}

    def work() -> None:
        try:
            from bgeditor.ui.main_window import MainWindow

            result["cls"] = MainWindow
        except BaseException as exc:  # re-raised on the main thread
            result["exc"] = exc

    t = threading.Thread(target=work, name="import-ui", daemon=True)
    t.start()
    while t.is_alive():
        QCoreApplication.processEvents(QEventLoop.ProcessEventsFlag.AllEvents, 50)
        t.join(0.02)
    if "exc" in result:
        raise result["exc"]
    return result["cls"]


# --------------------------------------------------------------- self-test
# An 8x8 red HEIC made with pillow-heif, to prove the bundled libheif can decode.
_TINY_HEIC = (
    "AAAAHGZ0eXBoZWljAAAAAG1pZjFoZWljbWlhZgAAAXxtZXRhAAAAAAAAACFoZGxyAAAAAAAAAABwaWN0"
    "AAAAAAAAAAAAAAAAAAAAACJpbG9jAAAAAERAAAEAAQAAAAABoAABAAAAAAAAACoAAAAjaWluZgAAAAAA"
    "AQAAABVpbmZlAgAAAAABAABodmMxAAAAAA5waXRtAAAAAAABAAAA/GlwcnAAAADcaXBjbwAAAHVodmND"
    "AQNwAAAAAAAAAAAAHvAA/P34+AAADwNgAAEAGEABDAH//wNwAAADAJAAAAMAAAMAHroCQGEAAQApQgEB"
    "A3AAAAMAkAAAAwAAAwAeoCCBBZbqrprm4CGgwIAAAAyAAAADAIRiAAEABkQBwXPBiQAAABNjb2xybmNs"
    "eAABAA0ABoAAAAAUaXNwZQAAAAAAAABAAAAAQAAAAChjbGFwAAAACAAAAAEAAAAIAAAAAf///8gAAAAC"
    "////yAAAAAIAAAAQcGl4aQAAAAADCAgIAAAAGGlwbWEAAAAAAAAAAQABBYECAwWEAAAAMm1kYXQAAAAm"
    "KAGvGSFeQkDvXaW//34+/1mF/iVbfKZKppcE14iZCwOBgI2RnTg="
)

# Never shipped in the app folder: Windows provides DirectML, and the Visual C++ runtime is
# installed by Microsoft's own installer (the setup offers it; the portable folder says so).
NOT_BUNDLED_DIRECTML = ("directml.dll",)
NOT_BUNDLED_VC_RUNTIME = ("vcruntime*.dll", "msvcp*.dll", "concrt*.dll", "vccorlib*.dll")
VC_RUNTIME_KEY = r"SOFTWARE\Microsoft\VisualStudio\14.0\VC\Runtimes\x64"


def _bundled(patterns: tuple[str, ...]) -> list[str]:
    """Files anywhere in the app folder whose name matches one of the (lower-case) patterns."""
    from bgeditor import paths

    root = str(paths.app_dir())
    found = []
    for folder, _dirs, files in os.walk(root):
        for name in files:
            if any(fnmatch.fnmatch(name.lower(), p) for p in patterns):
                found.append(os.path.relpath(os.path.join(folder, name), root))
    return sorted(found)


def _file_version(path: str) -> str:
    """The Windows file version of a DLL, such as '1.15.5.0', or '' when it has none."""
    try:
        ver = ctypes.WinDLL("version")
        ver.GetFileVersionInfoSizeW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(wintypes.DWORD)]
        ver.GetFileVersionInfoSizeW.restype = wintypes.DWORD
        ver.GetFileVersionInfoW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p]
        ver.GetFileVersionInfoW.restype = wintypes.BOOL
        ver.VerQueryValueW.argtypes = [
            ctypes.c_void_p,
            wintypes.LPCWSTR,
            ctypes.POINTER(ctypes.c_void_p),
            ctypes.POINTER(wintypes.UINT),
        ]
        ver.VerQueryValueW.restype = wintypes.BOOL
        size = ver.GetFileVersionInfoSizeW(path, None)
        if not size:
            return ""
        buf = ctypes.create_string_buffer(size)
        if not ver.GetFileVersionInfoW(path, 0, size, buf):
            return ""
        ptr, length = ctypes.c_void_p(), wintypes.UINT()
        if not ver.VerQueryValueW(buf, "\\", ctypes.byref(ptr), ctypes.byref(length)) or not ptr.value:
            return ""
        # VS_FIXEDFILEINFO starts with dwSignature, dwStrucVersion, dwFileVersionMS, dwFileVersionLS.
        fixed = ctypes.cast(ptr, ctypes.POINTER(ctypes.c_uint32 * 4)).contents
        ms, ls = fixed[2], fixed[3]
        return f"{ms >> 16}.{ms & 0xFFFF}.{ls >> 16}.{ls & 0xFFFF}"
    except (AttributeError, OSError):
        return ""


def _loaded_module(name: str) -> str:
    """Full path of a DLL this process has loaded, or ''."""
    try:
        k32 = ctypes.WinDLL("kernel32")
        k32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
        k32.GetModuleHandleW.restype = wintypes.HMODULE
        k32.GetModuleFileNameW.argtypes = [wintypes.HMODULE, wintypes.LPWSTR, wintypes.DWORD]
        k32.GetModuleFileNameW.restype = wintypes.DWORD
        handle = k32.GetModuleHandleW(name)
        if not handle:
            return ""
        buf = ctypes.create_unicode_buffer(32768)
        return buf.value if k32.GetModuleFileNameW(handle, buf, len(buf)) else ""
    except (AttributeError, OSError):
        return ""


def _vc_runtime_version() -> str:
    """Version of the installed Visual C++ 2015-2022 x64 runtime, or '' when it is not installed."""
    try:
        import winreg

        with winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE, VC_RUNTIME_KEY, 0, winreg.KEY_READ | winreg.KEY_WOW64_64KEY
        ) as key:
            installed = winreg.QueryValueEx(key, "Installed")[0]
            version = winreg.QueryValueEx(key, "Version")[0]
        return str(version) if installed else ""
    except OSError:
        return ""


def _self_test(report_path: str, photo: str | None = None, out_dir: str | None = None) -> int:
    """Used by build.ps1 to check a packaged build: everything the app loads at start-up,
    the hand-over, HEIC support, Qt plugins, that DirectML and the Visual C++ runtime come
    from Windows instead of the app folder, that OpenCV is gone, and the licence files. With a
    photo it also runs a real cutout with the default settings (models must be present), end
    to end in the bundle."""
    frozen = bool(getattr(sys, "frozen", False))
    lines: list[str] = []
    ok = True

    def step(label: str, fn) -> None:
        nonlocal ok
        try:
            passed, detail = fn()
        except Exception as exc:  # report everything, the build decides
            passed, detail = False, f"{exc!r}"
            lines.append("".join(traceback.format_exception(exc)).rstrip())
        ok &= bool(passed)
        lines.append(f"{'ok  ' if passed else 'FAIL'} {label}: {detail}")

    state: dict = {}

    def qt():
        from PyQt6.QtWidgets import QApplication, QStyleFactory

        state["app"] = QApplication([])
        styles = QStyleFactory.keys()
        return "windows11" in [s.lower() for s in styles], f"styles {styles} active {state['app'].style().name()}"

    def network():
        from PyQt6.QtNetwork import QLocalServer, QLocalSocket  # single-instance hand-over

        return QLocalServer is not None and QLocalSocket is not None, "QLocalServer, QLocalSocket"

    def image_formats():
        from PyQt6.QtGui import QImageReader

        formats = sorted({bytes(f).decode().lower() for f in QImageReader.supportedImageFormats()})
        return {"png", "jpeg", "webp", "ico"} <= set(formats), " ".join(formats)

    def libraries():
        import numpy
        import scipy
        import scipy.ndimage as ndi
        from PIL import Image

        # Use the compiled parts once: a missing DLL only shows when they run.
        a = numpy.zeros((9, 9), numpy.float32)
        a[4, 4] = 1.0
        blurred = ndi.gaussian_filter(a, 1.0)
        distance = ndi.distance_transform_edt(a == 0)
        _labels, count = ndi.label(a > 0)
        works = abs(float(blurred.sum()) - 1.0) < 1e-3 and count == 1 and float(distance[0, 0]) > 5.0
        return works, f"numpy {numpy.__version__} scipy {scipy.__version__} pillow {Image.__version__}"

    def onnx():
        import onnxruntime as ort

        providers = ort.get_available_providers()
        return "DmlExecutionProvider" in providers, f"onnxruntime {ort.__version__} providers {providers}"

    def directml():
        # ONNX Runtime loads DirectML.dll only when a GPU session starts: first next to
        # onnxruntime.dll, then the normal search, which ends in System32. So nothing named
        # DirectML.dll may be in the app folder, and Windows' own copy is the one used.
        system = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "DirectML.dll")
        parts = []
        if os.path.isfile(system):
            parts.append(f"Windows {system} version {_file_version(system) or 'unknown'}")
        else:
            parts.append("no DirectML.dll in System32 (older Windows): this PC runs the models on the CPU")
        passed = True
        if frozen:
            bundled = _bundled(NOT_BUNDLED_DIRECTML)
            if bundled:
                passed = False
                parts.append("BUNDLED, must not be shipped: " + ", ".join(bundled))
            else:
                parts.append("none in the app folder")
        else:
            import onnxruntime as ort

            wheel = os.path.join(os.path.dirname(ort.__file__), "capi", "DirectML.dll")
            if os.path.isfile(wheel):
                parts.append(f"from source the onnxruntime wheel's copy is used ({wheel}); the build leaves it out")
        return passed, "; ".join(parts)

    def vc_runtime():
        parts = [
            f"vcruntime140.dll loaded from {_loaded_module('vcruntime140.dll') or 'nowhere'}",
            f"Visual C++ 2015-2022 x64 runtime installed: {_vc_runtime_version() or 'no'}",
        ]
        passed = True
        if frozen:
            bundled = _bundled(NOT_BUNDLED_VC_RUNTIME)
            if bundled:
                passed = False
                parts.append("BUNDLED, must not be shipped: " + ", ".join(bundled))
            else:
                parts.append("none in the app folder")
        return passed, "; ".join(parts)

    def no_opencv():
        # OpenCV's wheel links Intel IPP, which the GPL does not allow to be shipped with PyQt6.
        import importlib.util

        import bgeditor.engine  # noqa: F401  (everything the app imports for processing)
        import bgeditor.imageio  # noqa: F401
        import bgeditor.pipeline  # noqa: F401

        loaded = "cv2" in sys.modules
        bundled = frozen and importlib.util.find_spec("cv2") is not None
        detail = []
        if loaded:
            detail.append("cv2 was imported")
        if bundled:
            detail.append("cv2 is in the bundle")
        return not (loaded or bundled), "; ".join(detail) or "not imported" + (", not bundled" if frozen else "")

    def legal():
        # The About dialog opens these, and GPL v3 needs them next to the program.
        from bgeditor import paths

        root = paths.app_dir()
        missing = [n for n in ("LICENSE", "LICENSES.txt") if not (root / n).is_file()]
        source = root / "source"
        zips = sorted(p.name for p in source.glob("*.zip")) if source.is_dir() else []
        detail = "LICENSE and LICENSES.txt " + ("missing: " + ", ".join(missing) if missing else "present")
        if frozen:
            detail += f"; source folder: {', '.join(zips) or 'no zip (yet)'}"
        return not missing, detail

    def gpus():
        from bgeditor.engine import list_gpus

        return True, str([g.name for g in list_gpus()])

    def heic():
        from PIL import Image

        from bgeditor.imageio import INPUT_EXTENSIONS  # registers the HEIF opener

        if ".heic" not in INPUT_EXTENSIONS:
            return False, "pillow_heif is not available"
        with Image.open(io.BytesIO(base64.b64decode(_TINY_HEIC))) as im:
            im.load()
            size = im.size
            r, g, b = im.convert("RGB").getpixel((4, 4))
        return size == (8, 8) and r > 150 and g < 90 and b < 90, f"decoded {size} rgb {r},{g},{b}"

    def assets():
        from bgeditor.ui.resources import asset_dir

        found = sorted(os.listdir(asset_dir()))
        needed = ("app.ico", "logo_256.png", "selftest_portrait.jpg", "selftest_reference.png")
        missing = [name for name in needed if name not in found]
        if not missing:
            # The model-update check reads these two; make sure they also decode.
            from PIL import Image

            for name in ("selftest_portrait.jpg", "selftest_reference.png"):
                with Image.open(os.path.join(asset_dir(), name)) as im:
                    im.load()
        return not missing, str(found) + (f"; missing {missing}" if missing else "")

    def window():
        # Import and build the window once, without showing it: catches missing modules and
        # Qt pieces used at start-up. It is not closed, so no settings are written.
        from bgeditor.ui.main_window import MainWindow

        w = MainWindow()
        title = w.windowTitle()
        w.deleteLater()
        return title == "Background Editor", "built"

    def app_update_check():
        # The update code loads in the bundle, and WinVerifyTrust answers through ctypes. The
        # self-test runs before build.ps1 signs the exe, so the packaged exe is expected to be
        # unsigned here (or signed by the pinned certificate on a rebuild of a signed folder).
        # The manifest parser reads a small unsigned sample (the signature is checked apart).
        from bgeditor import SIGNER_CERT_SHA256, UPDATE_REPO, app_updates

        target = sys.executable if frozen else os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "explorer.exe")
        result = app_updates.check_signature(target)
        expected = (0, app_updates.TRUST_E_NOSIGNATURE) if frozen else (0,)
        works = result.code in expected and (result.ok == (result.cert_sha256 == SIGNER_CERT_SHA256))
        helper_ascii = app_updates.PORTABLE_HELPER.isascii()
        sample = (
            "# Background Editor release manifest (self-test sample)\n"
            f"${app_updates.MANIFEST_VARIABLE} = @'\n"
            '{"format": 1, "version": "9.9.9", "built": "", "min_update_from": "1.1.0", "portable": null,\n'
            ' "setup": {"name": "BackgroundEditor-Setup-9.9.9.exe", "sha256": "' + "0" * 64 + '", "size": 1}}\n'
            "'@\n"
        ).encode("ascii")
        parsed = app_updates.parse_manifest(sample, signed=False)
        manifest_ok = parsed.version == "9.9.9" and parsed.setup is not None and parsed.min_update_from == "1.1.0"
        detail = (
            f"repository {UPDATE_REPO}; {os.path.basename(target)}: code 0x{result.code:08X}, "
            f"{'accepted' if result.ok else 'refused'} ({result.reason}); helper script ASCII: {helper_ascii}; "
            f"manifest parser: {manifest_ok}"
        )
        return works and helper_ascii and manifest_ok and app_updates.tag_version("v1.10.0") == "1.10.0", detail

    def cutout():
        import time

        import numpy as np
        from PIL import Image

        from bgeditor.engine import Engine
        from bgeditor.options import Options
        from bgeditor.pipeline import process_file

        target = out_dir or os.path.join(os.environ.get("TEMP", "."), "BackgroundEditor-selftest-out")
        os.makedirs(target, exist_ok=True)
        opts = Options(output_mode="folder", output_folder=target)
        engine = Engine()
        t0 = time.perf_counter()
        result = process_file(engine, photo, opts)
        seconds = time.perf_counter() - t0
        with Image.open(result.output) as im:
            alpha = np.asarray(im.convert("RGBA"))[..., 3].astype(np.float32) / 255.0
        cover = float(alpha.mean())
        soft = float(((alpha > 0.02) & (alpha < 0.98)).mean())
        detail = (
            f"{os.path.basename(photo)} -> {result.output} in {seconds:.1f} s on {engine.device_label}; "
            f"coverage {cover:.3f}, soft edge {soft:.4f}"
        )
        return 0.05 < cover < 0.95 and soft > 0.0005, detail

    step("Qt", qt)
    step("Qt network", network)
    step("Qt image formats", image_formats)
    step("libraries", libraries)
    step("ONNX Runtime", onnx)
    step("DirectML.dll", directml)
    step("Visual C++ runtime", vc_runtime)
    step("GPUs", gpus)
    step("HEIC", heic)
    step("assets", assets)
    step("licence files", legal)
    step("app update check", app_update_check)
    if "app" in state:
        step("main window", window)
    step("no OpenCV", no_opencv)
    if photo:
        step("cutout", cutout)
    lines.append("SELFTEST " + ("OK" if ok else "FAILED"))
    with open(report_path, "w", encoding="utf-8", newline="") as fh:
        fh.write("\n".join(lines) + "\n")
    return 0 if ok else 1


# ---------------------------------------------------------- update check
def _verify_update(path: str, extra: list[str]) -> int:
    """--verify-update <file> [--report <text file>]: the signature check of app updates, as
    this program does it (WinVerifyTrust, the pinned certificate, the PE certificate-table
    check). A release manifest (.ps1) is also read and checked. build.ps1 runs this on the
    signed installer and manifest it made; it is also useful by hand.

    Exit code 0 when the file is signed by the pinned certificate (and, for a manifest, is
    valid), 1 when not, 2 when the check itself failed. Nothing else runs: no window, no data
    migration, no single-instance hand-over.
    """
    report = extra[extra.index("--report") + 1] if "--report" in extra[:-1] else ""
    try:
        from bgeditor import app_updates

        result = app_updates.check_signature(path)
        signer = f"({result.subject or 'no signer'}, certificate SHA-256 {result.cert_sha256 or '-'})"
        text = f"{'ok' if result.ok else 'refused'}: {result.reason} {signer}"
        code = 0 if result.ok else 1
        if result.ok and path.lower().endswith(".ps1"):
            try:
                manifest = app_updates.verify_manifest(path)
            except app_updates.UpdateError as exc:
                text, code = f"refused: {exc} {signer}", 1
            else:
                files = len(manifest.portable.files) if manifest.portable else 0
                text = (
                    f"ok: release manifest for version {manifest.version} (from version {manifest.min_update_from or 'any'}; "
                    f"installer: {'yes' if manifest.setup else 'no'}; portable zip: {files} files), {result.reason} {signer}"
                )
    except Exception as exc:  # report it
        text, code = f"error: {exc!r}", 2
    if report:
        try:
            with open(report, "w", encoding="utf-8", newline="") as fh:
                fh.write(text + "\n")
        except OSError:
            pass
    return code


# -------------------------------------------------------------------- main
def main() -> int:
    if len(sys.argv) >= 3 and sys.argv[1] == "--verify-update":
        return _verify_update(sys.argv[2], sys.argv[3:])
    if not (len(sys.argv) >= 2 and sys.argv[1] == "--self-test"):
        _migrate_legacy_data()  # before anything creates the new data folder
    _prepare_windows()
    _install_excepthooks()
    if len(sys.argv) >= 3 and sys.argv[1] == "--self-test":
        extra = sys.argv[3:]
        photo = extra[extra.index("--photo") + 1] if "--photo" in extra[:-1] else None
        out_dir = extra[extra.index("--out") + 1] if "--out" in extra[:-1] else None
        return _self_test(sys.argv[2], photo, out_dir)

    # Absolute paths: the running window may have another working directory.
    paths = [os.path.abspath(a) for a in sys.argv[1:] if not a.startswith("-")]
    from PyQt6.QtCore import QCoreApplication, QTimer

    QCoreApplication.setOrganizationName("BackgroundEditor")
    QCoreApplication.setApplicationName("BackgroundEditor")
    _migrate_legacy_settings()
    if _hand_over(paths) != "owner":
        return 0  # handed over, or the running window never answered: never open a second one

    from PyQt6.QtWidgets import QApplication

    app = QApplication(sys.argv)
    app.setApplicationDisplayName("Background Editor")
    server = _HandOverServer(app)  # listening before the slow part below
    MainWindow = _import_main_window()  # noqa: N806
    from bgeditor.ui.main_window import hard_exit
    from bgeditor.ui.resources import app_icon

    app.setWindowIcon(app_icon())
    window = MainWindow(initial_paths=paths)
    window.show()
    QTimer.singleShot(0, lambda: server.attach(window))
    code = app.exec()
    if window.has_running_threads():
        # Destroying the window would destroy a running QThread, which aborts the process.
        hard_exit(code)
    return code


if __name__ == "__main__":
    sys.exit(main())

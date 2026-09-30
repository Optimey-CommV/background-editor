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

"""Background threads: batch processing, model downloads, model and app update checks,
the app update download, and image loading for the preview and the background picker."""

from __future__ import annotations

import threading
import time
import traceback
from dataclasses import dataclass, replace
from pathlib import Path

from PIL import Image, ImageOps, UnidentifiedImageError
from PyQt6.QtCore import QObject, QRunnable, QThread, pyqtSignal
from PyQt6.QtGui import QImage

from .. import app_updates, imageio, models, updates
from ..imageio import load_image
from ..options import Options
from ..pipeline import Cancelled, process_file


def pil_to_qimage(im: Image.Image) -> QImage:
    if im.mode not in ("RGB", "RGBA"):
        im = im.convert("RGBA" if "A" in im.getbands() else "RGB")
    if im.mode == "RGBA":
        fmt, bpp = QImage.Format.Format_RGBA8888, 4
    else:
        fmt, bpp = QImage.Format.Format_RGB888, 3
    data = im.tobytes()
    qimg = QImage(data, im.width, im.height, im.width * bpp, fmt)
    return qimg.copy()  # detach from the Python buffer


def describe_open_error(exc: BaseException) -> str:
    """A short, plain reason why a photo could not be opened."""
    if isinstance(exc, FileNotFoundError):
        return "The file is no longer there. It may have been moved, renamed or deleted."
    if isinstance(exc, PermissionError):
        return "Windows does not allow reading this file."
    if isinstance(exc, UnidentifiedImageError):
        return "The file is damaged, or it is not a photo format this app can read."
    if isinstance(exc, Image.DecompressionBombError):
        return "The photo is too large to open."
    if isinstance(exc, MemoryError):
        return "Not enough memory to show this photo."
    detail = str(exc) or exc.__class__.__name__
    if isinstance(exc, OSError):  # Pillow reports broken or cut-off image data this way
        return f"The file is damaged or incomplete ({detail})."
    return detail


@dataclass
class Job:
    item_id: int
    path: Path
    output: Path | None = None  # planned for the whole batch by pipeline.plan_outputs


class BatchWorker(QThread):
    itemStarted = pyqtSignal(int)
    itemStage = pyqtSignal(int, str)
    itemDone = pyqtSignal(int, str, float)
    itemFailed = pyqtSignal(int, str)
    itemCancelled = pyqtSignal(int)
    deviceChanged = pyqtSignal(str)

    def __init__(self, engine, jobs: list[Job], options: Options, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._engine = engine
        self._jobs = list(jobs)
        self._options = replace(options)
        self._cancel = threading.Event()

    def cancel(self) -> None:
        self._cancel.set()
        self._engine.abort()

    def was_cancelled(self) -> bool:
        return self._cancel.is_set()

    def run(self) -> None:
        for job in self._jobs:
            if self._cancel.is_set():
                self.itemCancelled.emit(job.item_id)
                continue
            self.itemStarted.emit(job.item_id)
            t0 = time.perf_counter()
            try:
                result = process_file(
                    self._engine,
                    job.path,
                    self._options,
                    cancel=self._cancel,
                    report=lambda text, i=job.item_id: self.itemStage.emit(i, text),
                    on_device=self.deviceChanged.emit,
                    output=job.output,
                )
            except Cancelled:
                self.itemCancelled.emit(job.item_id)
                continue
            except MemoryError:
                self.itemFailed.emit(job.item_id, "Not enough memory for this photo.")
                continue
            except Exception as exc:  # report and continue with the next photo
                traceback.print_exc()
                self.itemFailed.emit(job.item_id, str(exc) or exc.__class__.__name__)
                continue
            self.itemDone.emit(job.item_id, str(result.output), time.perf_counter() - t0)


class WarmUpWorker(QThread):
    """Loads the models in the background so the first photo does not wait for it."""

    def __init__(self, engine, options: Options, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._engine = engine
        self._options = replace(options)

    def run(self) -> None:
        try:
            self._engine.warm_up(self._options)
        except Exception:
            traceback.print_exc()  # the real run will report a problem properly


class DownloadWorker(QThread):
    progress = pyqtSignal(str, "qint64", "qint64")  # title, done, total (bytes can exceed 2**31)
    failed = pyqtSignal(str)
    succeeded = pyqtSignal()

    def __init__(self, specs: list[models.ModelSpec], parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._specs = specs
        self._cancel = threading.Event()

    def cancel(self) -> None:
        self._cancel.set()

    def was_cancelled(self) -> bool:
        return self._cancel.is_set()

    def run(self) -> None:
        try:
            for spec in self._specs:
                models.download_model(
                    spec,
                    progress=lambda done, total, t=spec.title: self.progress.emit(t, done, total),
                    cancel=self._cancel,
                )
        except models.DownloadCancelled:
            self.failed.emit("")
            return
        except Exception as exc:
            if self._cancel.is_set():
                self.failed.emit("")  # an error while stopping is not worth a message
                return
            self.failed.emit(str(exc) or exc.__class__.__name__)
            return
        self.succeeded.emit()


class ModelUpdateWorker(QThread):
    """Runs a model update check, or the adoption of a candidate the user agreed to.

    The work itself lives in bgeditor.updates; this thread only keeps it off the GUI thread
    and turns every outcome, including an unexpected error, into an UpdateReport.
    """

    progress = pyqtSignal(str, "qint64", "qint64")  # stage text, done, total (total 0: unknown)
    report = pyqtSignal(object)  # updates.UpdateReport

    def __init__(
        self,
        mode: str,
        force: bool = False,
        candidate: str = "",
        manual: bool = False,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self.mode = mode
        self.force = force
        self.candidate = candidate  # set: adopt this candidate instead of checking
        self.manual = manual  # started by the user (Check now, or agreeing to a candidate)
        self.combined = False  # part of Check for updates: the result goes into one summary
        self.result = None  # the UpdateReport, once run() is done
        self._cancel = threading.Event()

    def cancel(self) -> None:
        self._cancel.set()

    def was_cancelled(self) -> bool:
        return self._cancel.is_set()

    def run(self) -> None:
        def on_progress(text: str, done: int, total: int) -> None:
            self.progress.emit(str(text), int(done), int(total))

        try:
            if self.candidate:
                result = updates.adopt_candidate(self.candidate, self._cancel, on_progress)
            else:
                result = updates.check_for_model_updates(self.mode, self.force, self._cancel, on_progress)
        except Exception as exc:  # the report says what went wrong; the app keeps its model
            traceback.print_exc()
            result = updates.UpdateReport(status="failed", message=str(exc) or exc.__class__.__name__)
        self.result = result  # read by the GUI thread once 'finished' arrives
        self.report.emit(result)


class AppUpdateWorker(QThread):
    """Checks for a new version of the app ("check"), or downloads, checks and starts the
    one in `report` ("install"). The work lives in bgeditor.app_updates; this thread keeps it
    off the GUI thread. Once 'finished' arrives, `result` holds the AppUpdateReport (check)
    or the started update's path (install), and `error` says what went wrong ('' when the
    user cancelled or nothing failed). `integrity` is True when the install was refused
    because the release or the download is not exactly what Optimey signed.
    """

    progress = pyqtSignal(str, "qint64", "qint64")  # stage text, done, total (0: unknown)

    def __init__(
        self,
        action: str,
        report=None,
        force: bool = False,
        manual: bool = False,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self.action = action  # "check" | "install"
        self.report = report
        self.force = force
        self.manual = manual  # started by the user: report every outcome
        self.combined = False  # part of Check for updates (app and models together)
        self.result = None
        self.error = ""
        self.integrity = False
        self._cancel = threading.Event()

    def cancel(self) -> None:
        self._cancel.set()

    def was_cancelled(self) -> bool:
        return self._cancel.is_set()

    def run(self) -> None:
        def on_progress(text: str, done: int, total: int) -> None:
            self.progress.emit(str(text), int(done), int(total))

        try:
            if self.action == "check":
                self.result = app_updates.check_for_app_update(self.force, self._cancel)
                return
            prepared = app_updates.download_update(self.report, self._cancel, on_progress)
            if self._cancel.is_set():
                app_updates.discard_update(prepared)
                raise models.DownloadCancelled()
            on_progress("Starting the update", 0, 0)
            app_updates.apply_update(prepared)
            self.result = prepared.path
        except models.DownloadCancelled:
            self.error = ""
        except models.ChecksumMismatch as exc:
            self.error = f"The download does not match the signed release manifest, so it was not used ({exc})."
            self.integrity = True
        except app_updates.IntegrityError as exc:
            self.error = str(exc)
            self.integrity = True
        except app_updates.UpdateError as exc:
            self.error = str(exc)
        except Exception as exc:  # network trouble and the like: the app stays as it is
            if not isinstance(exc, OSError):
                traceback.print_exc()
            self.error = str(exc) or exc.__class__.__name__


class _LoaderSignals(QObject):
    loaded = pyqtSignal(int, object, object, str)  # token, before QImage | None, after QImage | None, error
    thumb = pyqtSignal(int, object)  # item id, QImage | None
    backdrop = pyqtSignal(int, object, str)  # token, fitted background QImage | None, error
    background_thumb = pyqtSignal(str, object, str)  # path, QImage | None, error ("missing" when gone)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        # The newest preview request; loaders for older ones stop before decoding more.
        self.latest_token = 0
        # The same for the fitted background behind the preview.
        self.latest_backdrop = 0


class PreviewLoader(QRunnable):
    """Loads the original (as the engine sees it: EXIF-rotated) and the saved result."""

    def __init__(self, token: int, source: Path, output: Path | None, signals: _LoaderSignals) -> None:
        super().__init__()
        self._token = token
        self._source = source
        self._output = output
        self._signals = signals

    def _stale(self) -> bool:
        try:
            return self._signals.latest_token != self._token
        except RuntimeError:
            return True  # window closed

    def run(self) -> None:
        if self._stale():
            return
        before = after = None
        error = ""
        try:
            before = pil_to_qimage(load_image(self._source).rgb)
        except Exception as exc:
            error = describe_open_error(exc)
            if not isinstance(exc, (OSError, ValueError, Image.DecompressionBombError)):
                traceback.print_exc()  # unexpected: worth a line in error.log
        if self._stale():
            return
        if self._output is not None and self._output.exists():
            try:
                with Image.open(self._output) as im:
                    im.load()
                    if im.mode not in ("RGB", "RGBA"):
                        im = im.convert("RGBA")
                    after = pil_to_qimage(im)
            except Exception:
                traceback.print_exc()
        if self._stale():
            return
        try:
            self._signals.loaded.emit(self._token, before, after, error)
        except RuntimeError:
            pass  # window closed while loading


class ThumbnailLoader(QRunnable):
    def __init__(self, item_id: int, path: Path, size: int, signals: _LoaderSignals) -> None:
        super().__init__()
        self._item_id = item_id
        self._path = path
        self._size = size
        self._signals = signals

    def run(self) -> None:
        qimg = None
        try:
            with Image.open(self._path) as im:
                im.draft("RGB", (self._size * 2, self._size * 2))  # fast JPEG downscale
                im = ImageOps.exif_transpose(im)
                im.thumbnail((self._size * 2, self._size * 2), Image.Resampling.LANCZOS)
                qimg = pil_to_qimage(im.convert("RGBA") if im.mode in ("RGBA", "LA", "P") else im.convert("RGB"))
        except Exception:
            qimg = None
        try:
            self._signals.thumb.emit(self._item_id, qimg)
        except RuntimeError:
            pass


def describe_background_error(path: str | Path, exc: BaseException) -> str:
    """Why a background image cannot be used, always naming the file."""
    name = Path(path).name
    if isinstance(exc, FileNotFoundError):
        return f"{name} is no longer there. It may have been moved, renamed or deleted."
    text = str(exc)
    if isinstance(exc, (ValueError, OSError)) and text and name in text:
        return text  # imageio's own message already names the file
    return f"{name}: {describe_open_error(exc)}"


class BackdropLoader(QRunnable):
    """Fits the chosen background image to the result's size for the preview backdrop."""

    def __init__(
        self,
        token: int,
        path: str,
        size: tuple[int, int],
        fit: str,
        blur_px: float,
        fill_hex: str,
        signals: _LoaderSignals,
    ) -> None:
        super().__init__()
        self._token = token
        self._path = path
        self._size = size
        self._fit = fit
        self._blur = blur_px
        self._fill = fill_hex
        self._signals = signals

    def _stale(self) -> bool:
        try:
            return self._signals.latest_backdrop != self._token
        except RuntimeError:
            return True  # window closed

    def run(self) -> None:
        if self._stale():
            return
        qimg = None
        error = ""
        try:
            im = imageio.background_preview(self._path, self._size, self._fit, self._blur, self._fill)
            qimg = pil_to_qimage(im)
        except Exception as exc:
            error = describe_background_error(self._path, exc)
            if not isinstance(exc, (OSError, ValueError, Image.DecompressionBombError)):
                traceback.print_exc()
        if self._stale():
            return
        try:
            self._signals.backdrop.emit(self._token, qimg, error)
        except RuntimeError:
            pass


class BackgroundThumbLoader(QRunnable):
    """A small cover-fitted thumbnail of a background image for the recent list.

    The error is "missing" when the file is gone from a folder that is still there, and
    "unavailable" when the folder itself cannot be reached (an unplugged drive, an offline
    network share): only the first is a reason to drop it from the list.
    """

    def __init__(self, path: str, size: tuple[int, int], signals: _LoaderSignals) -> None:
        super().__init__()
        self._path = path
        self._size = size
        self._signals = signals

    def run(self) -> None:
        qimg = None
        error = ""
        try:
            p = imageio.resolve_background(self._path)  # relative: inside the data folder
            if not p.is_file():
                error = "missing" if p.parent.is_dir() else "unavailable"
            else:
                im = imageio.background_preview(self._path, self._size, "cover", 0.0, "#FFFFFF")
                qimg = pil_to_qimage(im)
        except Exception as exc:
            error = describe_background_error(self._path, exc)
        try:
            self._signals.background_thumb.emit(self._path, qimg, error)
        except RuntimeError:
            pass


LoaderSignals = _LoaderSignals

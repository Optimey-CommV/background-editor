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

"""Main window: photo list, before/after preview and settings."""

from __future__ import annotations

import html
import json
import math
import os
import subprocess
import sys
import threading
from collections import OrderedDict
from dataclasses import replace
from pathlib import Path
from typing import Callable

from PyQt6.QtCore import QByteArray, QEvent, QRectF, QSettings, QSize, Qt, QThreadPool, QTimer, QUrl, pyqtSignal
from PyQt6.QtGui import (
    QAction,
    QColor,
    QDesktopServices,
    QDragEnterEvent,
    QDropEvent,
    QFontDatabase,
    QIcon,
    QKeySequence,
    QPainter,
    QPalette,
    QPixmap,
)
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QButtonGroup,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QProgressDialog,
    QPushButton,
    QSizePolicy,
    QSplitter,
    QTabWidget,
    QTextBrowser,
    QToolBar,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from .. import __version__, app_updates, imageio, models, paths, updates
from ..engine import Engine, list_gpus
from ..imageio import INPUT_EXTENSIONS, is_supported
from ..options import Options, clean_suffix, presets, threads_text
from ..pipeline import DEFAULT_SUFFIX, output_folder_for, plan_outputs
from .compare_view import CompareView
from . import taskbar
from .resources import app_icon, is_dark, logo_pixmap, paint_picture, painted_icon, swatch_border
from .settings_panel import SettingsPanel, format_size, model_title
from .update_bar import ReleaseNotesView, UpdateBar, open_release_link
from .workers import (
    AppUpdateWorker,
    BackdropLoader,
    BatchWorker,
    DownloadWorker,
    Job,
    LoaderSignals,
    ModelUpdateWorker,
    PreviewLoader,
    ThumbnailLoader,
    WarmUpWorker,
)

ROLE_ID = Qt.ItemDataRole.UserRole + 1
ROLE_PATH = Qt.ItemDataRole.UserRole + 2
ROLE_STATUS = Qt.ItemDataRole.UserRole + 3
ROLE_OUTPUT = Qt.ItemDataRole.UserRole + 4
ROLE_STAGE = Qt.ItemDataRole.UserRole + 5  # latest progress text while the photo is being worked on
ROLE_THUMB = Qt.ItemDataRole.UserRole + 6  # True once the thumbnail load has run

PENDING, WORKING, DONE, FAILED = "pending", "working", "done", "failed"
THUMB = 52
APP_TITLE = "Background Editor"

PREVIEW_DELAY_MS = 70  # coalesces fast selection changes (holding an arrow key) into one load
WARM_UP_DELAY_MS = 1000  # after a settings change, load the models once the user pauses
CLOSE_TIMEOUT_MS = 10_000  # longest wait for background work after the window is closed

# The background image behind the preview: fitted off the GUI thread, at most this many
# pixels (the preview rarely shows more), after the user pauses on the fit and blur controls.
BACKDROP_MAX_PIXELS = 16_000_000
BACKDROP_DELAY_MS = 150
BACKDROP_CACHE = 3

# Model updates: the automatic check runs this long after start, only while no photos are
# being processed (otherwise it tries again a minute later), and only when it is due.
UPDATE_FIRST_CHECK_MS = 60_000
UPDATE_RETRY_MS = 60_000
UPDATE_MESSAGE_MS = 120_000  # how long a routine update message stays in the status bar
UPDATE_OFFER_RETRY_MS = 5_000  # an offer waits until the window is free for a dialog
# App updates: the daily check for a new version runs this long after start, only while no
# photos are being processed (otherwise a minute later), and only when it is due.
APP_UPDATE_FIRST_CHECK_MS = 30_000
# After an update: the app's own downloads of the installed version are removed this long
# after start (in a background thread; see app_updates.cleanup_old_downloads).
APP_UPDATE_CLEANUP_MS = 20_000
TASKBAR_ICON_REFRESH_MS = 2_000  # after the first show, the window icons are sent once more

COPYRIGHT = "Copyright (C) 2026 Optimey CommV"


class MainWindow(QMainWindow):
    # Emitted when the user closes the window; the process ends once background work has stopped.
    closing = pyqtSignal()

    def __init__(self, initial_paths: list[str] | None = None) -> None:
        super().__init__()
        self.setWindowTitle(APP_TITLE)
        self.setWindowIcon(app_icon())
        self.setAcceptDrops(True)
        self.resize(1360, 860)

        self._settings = paths.settings()
        self._options = self._load_options()
        self._engine = Engine()
        try:
            self._gpus = list_gpus()
        except Exception:
            self._gpus = []
        self._worker: BatchWorker | None = None
        self._downloader: DownloadWorker | None = None
        self._warmer: WarmUpWorker | None = None
        self._updater: ModelUpdateWorker | None = None
        self._warm_again = False
        self._warmed_key: tuple | None = None
        self._download_ok = False
        self._after_download: Callable[[], None] | None = None
        self._closing = False
        self._next_id = 1
        self._items: dict[int, QListWidgetItem] = {}
        self._done_count = 0
        # Own pools: previews must not queue behind hundreds of thumbnails, and both are capped
        # so a held arrow key or a big folder cannot decode a dozen full-size photos at once.
        self._preview_pool = QThreadPool(self)
        self._preview_pool.setMaxThreadCount(2)
        self._thumb_pool = QThreadPool(self)
        self._thumb_pool.setMaxThreadCount(2)
        self._loader_signals = LoaderSignals()
        self._loader_signals.loaded.connect(self._on_preview_loaded)
        self._loader_signals.thumb.connect(self._on_thumb)
        self._loader_signals.backdrop.connect(self._on_backdrop_loaded)
        self._preview_token = 0
        self._preview_timer = QTimer(self)
        self._preview_timer.setSingleShot(True)
        self._preview_timer.setInterval(PREVIEW_DELAY_MS)
        self._preview_timer.timeout.connect(self._load_preview)
        self._warm_timer = QTimer(self)
        self._warm_timer.setSingleShot(True)
        self._warm_timer.setInterval(WARM_UP_DELAY_MS)
        self._warm_timer.timeout.connect(self._warm_up)
        self._batch_total = 0
        self._batch_done = 0
        self._batch_ids: set[int] = set()
        # The fitted background image behind the preview.
        self._backdrop_token = 0
        self._backdrop_loading: tuple | None = None  # request being fitted now
        self._backdrop_shown: tuple | None = None  # request the view shows
        self._backdrop_failed: tuple | None = None  # last request that failed (reported once)
        self._backdrop_cache: OrderedDict[tuple, object] = OrderedDict()
        self._backdrop_timer = QTimer(self)
        self._backdrop_timer.setSingleShot(True)
        self._backdrop_timer.setInterval(BACKDROP_DELAY_MS)
        self._backdrop_timer.timeout.connect(self._request_backdrop)
        # Model updates.
        self._update_offer = None  # an "ask" report waiting until a dialog can be shown
        self._adopt_dialog: QProgressDialog | None = None
        self._preferred_before_update = ""
        self._update_timer = QTimer(self)
        self._update_timer.setSingleShot(True)
        self._update_timer.timeout.connect(self._auto_update_check)
        self._offer_timer = QTimer(self)
        self._offer_timer.setSingleShot(True)
        self._offer_timer.setInterval(UPDATE_OFFER_RETRY_MS)
        self._offer_timer.timeout.connect(self._show_pending_offer)
        self._update_msg_timer = QTimer(self)
        self._update_msg_timer.setSingleShot(True)
        self._update_msg_timer.timeout.connect(lambda: self._set_update_message(""))
        # App updates.
        self._app_checker: AppUpdateWorker | None = None
        self._app_installer: AppUpdateWorker | None = None
        self._install_dialog: QProgressDialog | None = None
        self._app_report = None  # the available version shown in the update bar
        self._pending_update = None  # Update now, waiting until the photos are done
        self._manual_check: dict | None = None  # Check for updates: app and model results
        self._closing_for_update = False
        self._icon_refreshed = False
        self._summary_box = self._notes_dialog = self._about_dialog = self._failure_box = None  # open dialogs, for the smoke tests
        self._app_update_timer = QTimer(self)
        self._app_update_timer.setSingleShot(True)
        self._app_update_timer.timeout.connect(self._auto_app_update_check)

        self._build_ui()
        self._restore_window()
        self._update_actions()
        # The native window exists from here on; before it is shown, give its window class
        # our icon instead of Qt's default (see taskbar.py).
        taskbar.set_class_icons(self)

        if initial_paths:
            QTimer.singleShot(0, lambda: self.add_paths([Path(p) for p in initial_paths]))
        if self._options.model_updates != "off":
            self._update_timer.start(UPDATE_FIRST_CHECK_MS)
        if self._options.app_update_check:
            self._app_update_timer.start(APP_UPDATE_FIRST_CHECK_MS)
        QTimer.singleShot(APP_UPDATE_CLEANUP_MS, self._clean_old_update_downloads)

    # ================================================================ UI
    def _build_ui(self) -> None:
        tb = QToolBar("Main")
        tb.setMovable(False)
        tb.setIconSize(QSize(20, 20))
        tb.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.addToolBar(tb)

        self.act_add = QAction(QIcon.fromTheme(QIcon.ThemeIcon.ListAdd), "Add photos…", self)
        self.act_add.setShortcut(QKeySequence.StandardKey.Open)
        self.act_add.triggered.connect(self._choose_files)
        self.act_add_folder = QAction(QIcon.fromTheme(QIcon.ThemeIcon.FolderOpen), "Add folder…", self)
        self.act_add_folder.triggered.connect(self._choose_folder)
        self.act_remove = QAction(QIcon.fromTheme(QIcon.ThemeIcon.ListRemove), "Remove", self)
        self.act_remove.setShortcut(QKeySequence.StandardKey.Delete)
        self.act_remove.setToolTip("Remove the selected photos from the list (files are not deleted)")
        self.act_remove.triggered.connect(self._remove_selected)
        self.act_clear = QAction(QIcon.fromTheme(QIcon.ThemeIcon.EditClear), "Clear list", self)
        self.act_clear.triggered.connect(self._clear)
        self.act_open_out = QAction(QIcon.fromTheme(QIcon.ThemeIcon.FolderOpen), "Open results folder", self)
        self.act_open_out.triggered.connect(self._open_output_folder)
        self.act_check_updates = QAction(QIcon.fromTheme(QIcon.ThemeIcon.SyncSynchronizing), "Check for updates", self)
        self.act_check_updates.setToolTip("Look for a new version of Background Editor and for newer AI models")
        self.act_check_updates.triggered.connect(self._check_for_updates)
        self.act_about = QAction(QIcon.fromTheme(QIcon.ThemeIcon.HelpAbout), "About", self)
        self.act_about.triggered.connect(self._about)
        for a in (self.act_add, self.act_add_folder):
            tb.addAction(a)
        tb.addSeparator()
        tb.addAction(self.act_remove)
        tb.addAction(self.act_clear)
        tb.addSeparator()
        tb.addAction(self.act_open_out)

        spacer = QWidget()
        spacer.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        tb.addWidget(spacer)

        self.btn_stop = QPushButton(QIcon.fromTheme(QIcon.ThemeIcon.MediaPlaybackStop), "Stop")
        self.btn_stop.clicked.connect(self._stop)
        self.btn_start = QPushButton(QIcon.fromTheme(QIcon.ThemeIcon.MediaPlaybackStart), "Remove backgrounds")
        self.btn_start.setDefault(True)
        self.btn_start.setMinimumHeight(34)
        self.btn_start.setMinimumWidth(190)
        f = self.btn_start.font()
        f.setBold(True)
        self.btn_start.setFont(f)
        self._style_primary_button()
        self.btn_start.clicked.connect(lambda: self._start())
        tb.addWidget(self.btn_stop)
        tb.addWidget(_hspace(6))
        tb.addWidget(self.btn_start)
        tb.addWidget(_hspace(6))
        tb.addAction(self.act_check_updates)
        tb.addAction(self.act_about)

        # --- photo list
        self.list = QListWidget()
        self.list.setIconSize(QSize(THUMB, THUMB))
        self.list.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.list.setUniformItemSizes(True)
        self.list.setSpacing(2)
        self.list.setWordWrap(False)
        self.list.setTextElideMode(Qt.TextElideMode.ElideMiddle)
        self.list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.list.customContextMenuRequested.connect(self._context_menu)
        self.list.currentItemChanged.connect(lambda *_: self._show_current())
        self.list.itemSelectionChanged.connect(self._update_actions)
        self.list.itemDoubleClicked.connect(self._open_item_result)
        list_box = QWidget()
        lv = QVBoxLayout(list_box)
        lv.setContentsMargins(10, 8, 4, 8)
        lv.setSpacing(6)
        self.lbl_list = QLabel("Photos")
        _bold(self.lbl_list)
        lv.addWidget(self.lbl_list)
        lv.addWidget(self.list, 1)

        # --- preview
        self.view = CompareView()
        self.view.set_empty_state(
            logo_pixmap(256), "Drop photos here", "or click Add photos… — portraits give the best results"
        )
        self.view.zoomChanged.connect(self._on_zoom)
        preview_box = QWidget()
        pv = QVBoxLayout(preview_box)
        pv.setContentsMargins(4, 8, 4, 8)
        pv.setSpacing(6)
        self.update_bar = UpdateBar()
        self.update_bar.setVisible(False)
        self.update_bar.updateRequested.connect(self._on_bar_update)
        self.update_bar.notesRequested.connect(lambda: self._show_release_notes(self._app_report))
        self.update_bar.skipRequested.connect(self._on_bar_skip)
        pv.addWidget(self.update_bar)
        pv.addWidget(self.view, 1)
        pv.addWidget(self._build_preview_bar())

        # --- settings
        self.panel = SettingsPanel(
            self._options, self._settings.value("advanced", False, type=bool), settings=self._settings
        )
        self.panel.optionsChanged.connect(self._on_options)
        self.panel.advancedToggled.connect(lambda on: self._settings.setValue("advanced", on))
        self.panel.checkUpdatesRequested.connect(self._check_for_updates)
        self.panel.downloadAllRequested.connect(self._download_all)
        self.panel.switchModelRequested.connect(self._switch_preferred)

        self.splitter = QSplitter(Qt.Orientation.Horizontal)
        self.splitter.addWidget(list_box)
        self.splitter.addWidget(preview_box)
        self.splitter.addWidget(self.panel)
        self.splitter.setStretchFactor(0, 0)
        self.splitter.setStretchFactor(1, 1)
        self.splitter.setStretchFactor(2, 0)
        self.splitter.setSizes([280, 720, 360])
        self.splitter.setChildrenCollapsible(False)
        self.setCentralWidget(self.splitter)

        # --- status bar
        sb = self.statusBar()
        self.lbl_device = QLabel(self._predicted_device_text())
        self.lbl_device.setToolTip(
            "Which hardware runs the AI models. Automatic uses the GPU only for the Fast model\n"
            "on a dedicated graphics card; hair refinement always runs on the CPU."
        )
        self.lbl_status = QLabel("")
        self.lbl_status.setMinimumWidth(40)  # long messages must not push the window wider
        # Model update activity and results, apart from the messages about the photos.
        self.lbl_update = QLabel("")
        self.lbl_update.setForegroundRole(QPalette.ColorRole.PlaceholderText)
        self.lbl_update.setVisible(False)
        self.progress = QProgressBar()
        self.progress.setMaximumWidth(260)
        self.progress.setTextVisible(True)
        self.progress.setVisible(False)
        sb.addWidget(self.lbl_status, 1)
        sb.addPermanentWidget(self.lbl_update)
        sb.addPermanentWidget(self.progress)
        sb.addPermanentWidget(self.lbl_device)

    def _build_preview_bar(self) -> QWidget:
        bar = QWidget()
        h = QHBoxLayout(bar)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(4)
        h.addWidget(QLabel("Backdrop:"))
        self._backdrop_group = QButtonGroup(self)
        self._backdrop_buttons: dict[str, QToolButton] = {}
        current = self._settings.value("backdrop", "checker")
        for key, tip in (
            ("checker", "Transparency grid"),
            ("#FFFFFF", "White"),
            ("#1FA84F", "Green — shows leftover background colour best"),
            ("#303030", "Dark grey"),
            ("output", "Your chosen background colour"),
            ("image", "Your background image, fitted and blurred as chosen under Background"),
        ):
            b = QToolButton()
            b.setCheckable(True)
            b.setAutoRaise(True)
            b.setIconSize(QSize(18, 18))
            b.setToolTip(tip)
            b.setAccessibleName(tip.split(" — ")[0])
            b.setProperty("backdrop", key)
            self._backdrop_group.addButton(b)
            self._backdrop_buttons[key] = b
            h.addWidget(b)
            if key == current:
                b.setChecked(True)
        self._refresh_backdrop_icons()
        if self._backdrop_group.checkedButton() is None:
            self._backdrop_group.buttons()[0].setChecked(True)
        self._sync_image_backdrop_button()
        self._backdrop_group.buttonClicked.connect(lambda _b: self._apply_backdrop())
        h.addSpacing(12)
        self.btn_compare = QToolButton()
        self.btn_compare.setText("Compare")
        self.btn_compare.setCheckable(True)
        self.btn_compare.setChecked(True)
        self.btn_compare.setToolTip("Show the original on the left of the slider")
        self.btn_compare.toggled.connect(self.view.set_compare)
        h.addWidget(self.btn_compare)
        h.addStretch(1)
        self.lbl_zoom = QLabel("")
        self.lbl_zoom.setMinimumWidth(48)
        self.lbl_zoom.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        h.addWidget(self.lbl_zoom)
        for icon, tip, fn in (
            (QIcon.ThemeIcon.ZoomOut, "Zoom out", lambda: self.view.zoom_by(1 / 1.25)),
            (QIcon.ThemeIcon.ZoomIn, "Zoom in", lambda: self.view.zoom_by(1.25)),
            (QIcon.ThemeIcon.ZoomFitBest, "Fit to window", self.view.fit),
        ):
            b = QToolButton()
            b.setIcon(QIcon.fromTheme(icon))
            b.setToolTip(tip)
            b.setAutoRaise(True)
            b.clicked.connect(fn)
            h.addWidget(b)
        b100 = QToolButton()
        b100.setText("100%")
        b100.setToolTip("Actual pixels — best for checking hair")
        b100.setAutoRaise(True)
        b100.clicked.connect(self.view.actual_size)
        h.addWidget(b100)
        self._apply_backdrop()
        return bar

    def _refresh_backdrop_icons(self) -> None:
        border = swatch_border(self.palette())
        for b in self._backdrop_group.buttons():
            b.setIcon(_backdrop_icon(b.property("backdrop"), self._options.background_color, border))

    def _sync_image_backdrop_button(self) -> None:
        """'Your background' needs a chosen image; without one the grid is shown instead."""
        b = self._backdrop_buttons["image"]
        has_image = bool(self._options.background_image)
        b.setEnabled(has_image)
        b.setToolTip(
            "Your background image, fitted and blurred as chosen under Background"
            if has_image
            else "Your background image — choose one under Background › Image first"
        )
        if not has_image and b.isChecked():
            self._backdrop_buttons["checker"].setChecked(True)
            if hasattr(self, "view"):
                self._apply_backdrop()

    # ========================================================= settings io
    def _load_options(self) -> Options:
        raw = self._settings.value("options", "")
        try:
            data = json.loads(raw) if isinstance(raw, str) and raw else {}
        except (TypeError, ValueError):
            data = {}
        # from_dict drops unknown values and clamps numbers, so a stale setting cannot crash us.
        opts = Options.from_dict(data) if isinstance(data, dict) else Options()
        if not isinstance(data, dict) or "model" not in data:
            # First start: follow the Best preset, which uses an adopted newer model if any.
            opts = replace(opts, **presets()["best"])
        return opts

    def _save_options(self) -> None:
        self._settings.setValue("options", json.dumps(self._options.to_dict()))

    def _save_state(self) -> None:
        self._settings.setValue("geometry", self.saveGeometry())
        self._settings.setValue("splitter", self.splitter.saveState())
        self._save_options()
        self._settings.sync()

    def _restore_window(self) -> None:
        geo = self._settings.value("geometry")
        if isinstance(geo, QByteArray):
            self.restoreGeometry(geo)
        state = self._settings.value("splitter")
        if isinstance(state, QByteArray):
            self.splitter.restoreState(state)

    def _on_options(self, opts: Options) -> None:
        old = self._options
        self._options = replace(opts)
        self._save_options()
        border = swatch_border(self.palette())
        self._backdrop_buttons["output"].setIcon(_backdrop_icon("output", opts.background_color, border))
        self._sync_image_backdrop_button()
        if opts.background == "image" and opts.background_image and _image_look(opts) != _image_look(old):
            # The user is working on the image background: show it behind the result.
            self._backdrop_buttons["image"].setChecked(True)
        self._apply_backdrop(delay=True)
        if not self._busy():
            self.lbl_device.setText(self._predicted_device_text())
        self._warm_timer.start()  # load the models for the new settings once the user pauses
        if old.model_updates == "off" and opts.model_updates != "off" and self._updater is None:
            self._update_timer.start(UPDATE_FIRST_CHECK_MS)
        if opts.app_update_check and not old.app_update_check and self._app_checker is None:
            self._app_update_timer.start(APP_UPDATE_FIRST_CHECK_MS)
        elif not opts.app_update_check:
            self._app_update_timer.stop()

    def _apply_backdrop(self, delay: bool = False) -> None:
        b = self._backdrop_group.checkedButton()
        key = b.property("backdrop") if b is not None else "checker"
        self._settings.setValue("backdrop", key)
        if key == "checker":
            self.view.set_backdrop("checker")
        elif key == "output":
            self.view.set_backdrop("color", QColor(self._options.background_color))
        elif key == "image":
            self.view.set_backdrop("image")
            if delay:
                self._backdrop_timer.start()  # the fit and blur controls change in quick steps
            else:
                self._request_backdrop()
        else:
            self.view.set_backdrop("color", QColor(key))

    # ------------------------------------------------ background image backdrop
    def _backdrop_request(self) -> tuple | None:
        """(path, (width, height), fit, blur, fill) to fit for the result shown now, or None."""
        o = self._options
        size = self.view.result_size()
        if self.view.backdrop() != "image" or not o.background_image or size is None or size.isEmpty():
            return None
        w, h = size.width(), size.height()
        scale = min(1.0, math.sqrt(BACKDROP_MAX_PIXELS / (w * h)))
        # Blur is measured in pixels of the result, so it shrinks with the preview.
        return (
            o.background_image,
            (max(1, round(w * scale)), max(1, round(h * scale))),
            o.background_fit,
            round(o.background_blur * scale, 3),
            o.background_color,
        )

    def _request_backdrop(self) -> None:
        if self._closing:
            return
        request = self._backdrop_request()
        if request is None or request == self._backdrop_shown or request == self._backdrop_loading:
            return
        cached = self._backdrop_cache.get(request)
        if cached is not None:
            self._backdrop_cache.move_to_end(request)
            self.view.set_backdrop_image(cached)
            self._backdrop_shown = request
            return
        self._backdrop_token += 1
        self._loader_signals.latest_backdrop = self._backdrop_token  # older fits stop early
        self._backdrop_loading = request
        path, size, fit, blur, fill = request
        self._preview_pool.start(
            BackdropLoader(self._backdrop_token, path, size, fit, blur, fill, self._loader_signals)
        )

    def _on_backdrop_loaded(self, token: int, qimg, error: str) -> None:
        if token != self._backdrop_token:
            return
        request, self._backdrop_loading = self._backdrop_loading, None
        if qimg is None:
            self.view.set_backdrop_image(None)
            self._backdrop_shown = None
            if error and request != self._backdrop_failed:
                self._backdrop_failed = request
                self._status(f"The background image cannot be shown: {error}")
            return
        self._backdrop_cache[request] = qimg
        while len(self._backdrop_cache) > BACKDROP_CACHE:
            self._backdrop_cache.popitem(last=False)
        self.view.set_backdrop_image(qimg)
        self._backdrop_shown = request
        # The settings may have changed while this one was being fitted.
        if self._backdrop_request() != request:
            self._backdrop_timer.start()

    def _predicted_device_text(self) -> str:
        """What the engine will most likely use for the current settings (it reports the real
        placement once a photo has been processed)."""
        o = self._options
        cpu = f"CPU ({threads_text(o.cpu_threads)})"
        if o.device == "cpu" or not self._gpus:
            return f"Processor: {cpu}"
        if o.device == "gpu":
            return f"Processor: GPU — {self._gpus[0].name} (forced)"
        discrete = [g for g in self._gpus if g.discrete]
        if discrete and o.model == "birefnet-lite":
            return f"Processor: GPU — {discrete[0].name}, when it works there"
        return f"Processor: {cpu}"

    # ============================================================== list
    def bring_to_front(self) -> None:
        """Show the window on top, also when it was minimised (a later launch asks for this)."""
        if self._closing:
            return
        if self.isMinimized():
            self.setWindowState((self.windowState() & ~Qt.WindowState.WindowMinimized) | Qt.WindowState.WindowActive)
        self.show()
        self.raise_()
        self.activateWindow()

    def receive_paths(self, paths: list[str]) -> None:
        """A later launch (e.g. Explorer's right-click menu) handed over photos, or none."""
        if self._closing:
            return
        self.bring_to_front()
        if paths:
            self.add_paths([Path(p) for p in paths])  # also while busy: the batch continues with them

    def _warm_key(self, opts: Options) -> tuple:
        return (tuple(s.key for s in self._engine.required_models(opts)), opts.device, opts.cpu_threads)

    def _warm_up(self) -> None:
        """Start loading the models while the user is still looking at the list."""
        if self._closing or self._busy() or not self._items:
            return
        if self._warmer is not None and self._warmer.isRunning():
            self._warm_again = True  # settings changed meanwhile: load those next
            self._engine.cancel_warm_up()
            return
        opts = replace(self._options)
        key = self._warm_key(opts)
        if key == self._warmed_key:
            return
        if any(models.find_model(s) is None for s in self._engine.required_models(opts)):
            return
        self._warm_again = False
        self._warmed_key = key
        self._warmer = WarmUpWorker(self._engine, opts, self)
        self._warmer.finished.connect(self._on_warm_up_finished)
        self._warmer.start()

    def _on_warm_up_finished(self) -> None:
        warmer = self._warmer
        self._warmer = None
        if warmer is not None:
            warmer.deleteLater()
        if self._warm_again and not self._closing:
            self._warm_again = False
            self._warm_up()

    def add_paths(self, paths: list[Path]) -> None:
        files: list[Path] = []  # absolute, resolved paths
        for p in paths:
            try:
                if p.is_dir():
                    # Resolve the folder once; resolving every file costs a millisecond each.
                    folder = p.resolve()
                    with os.scandir(folder) as it:
                        entries = sorted(it, key=lambda e: e.name.lower())
                    for e in entries:
                        q = folder / e.name
                        if is_supported(q) and e.is_file() and not self._looks_like_output(q):
                            files.append(q)
                elif p.is_file() and is_supported(p):
                    files.append(p.resolve())
            except OSError:
                continue
        known = {str(it.data(ROLE_PATH)).lower() for it in self._items.values()}
        placeholder = self._placeholder_icon()
        added = 0
        for f in files:
            key = str(f).lower()
            if key in known:
                continue
            known.add(key)
            self._add_item(f, placeholder)
            added += 1
        self._update_list_title()
        if added and self.list.currentItem() is None:
            self.list.setCurrentRow(0)
        if added:
            self._warm_up()
        skipped = len(files) - added
        if paths and not files:
            self._status("No supported photos found. Supported: " + ", ".join(sorted(INPUT_EXTENSIONS)))
        elif self._busy() and added:
            self._status(f"Added {added} photo(s); they are processed after the current ones.")
        elif skipped:
            self._status(f"Added {added} photo(s); {skipped} already in the list.")
        else:
            self._status(f"Added {added} photo(s).")
        self._update_actions()

    def _looks_like_output(self, p: Path) -> bool:
        stem = p.stem.lower()
        suffix = (clean_suffix(self._options.suffix) or DEFAULT_SUFFIX).lower()
        return stem.endswith(suffix) or stem.endswith("_mask")

    def _add_item(self, path: Path, placeholder: QIcon) -> None:
        item_id = self._next_id
        self._next_id += 1
        item = QListWidgetItem(placeholder, "")
        item.setData(ROLE_ID, item_id)
        item.setData(ROLE_PATH, str(path))
        item.setToolTip(str(path))
        item.setSizeHint(QSize(0, THUMB + 12))
        self._items[item_id] = item
        self.list.addItem(item)
        self._set_status(item_id, PENDING, "Waiting", update_title=False)
        self._thumb_pool.start(ThumbnailLoader(item_id, path, THUMB, self._loader_signals))

    def _placeholder_icon(self) -> QIcon:
        pm = QPixmap(THUMB, THUMB)
        pm.fill(self.palette().color(QPalette.ColorRole.Midlight))
        return QIcon(pm)

    def _on_thumb(self, item_id: int, qimg) -> None:
        item = self._items.get(item_id)
        if item is None:
            return
        item.setData(ROLE_THUMB, True)
        if qimg is None:
            return
        pm = QPixmap.fromImage(qimg).scaled(
            THUMB * 2, THUMB * 2, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation
        )
        item.setIcon(QIcon(pm))

    def _status_colors(self) -> dict[str, QColor]:
        # Contrast of at least 4.5:1 against the list background in either colour scheme.
        # The window's palette, not the list's: on a theme switch the window hears it first.
        if is_dark(self.palette()):
            return {DONE: QColor("#81C995"), FAILED: QColor("#F28B82")}
        return {DONE: QColor("#137333"), FAILED: QColor("#C5221F")}

    def _apply_status_color(self, item: QListWidgetItem, colors: dict[str, QColor] | None = None) -> None:
        color = (colors or self._status_colors()).get(item.data(ROLE_STATUS))
        if color is None:
            item.setData(Qt.ItemDataRole.ForegroundRole, None)
        else:
            item.setForeground(color)

    def _set_status(
        self, item_id: int, status: str, text: str, output: str | None = None, update_title: bool = True
    ) -> None:
        item = self._items.get(item_id)
        if item is None:
            return
        old = item.data(ROLE_STATUS)
        if old != status:
            self._done_count += (status == DONE) - (old == DONE)
        item.setData(ROLE_STATUS, status)
        item.setData(ROLE_STAGE, text if status == WORKING else None)
        if output is not None:
            item.setData(ROLE_OUTPUT, output)
        name = Path(item.data(ROLE_PATH)).name
        mark = {PENDING: "", WORKING: "⏳ ", DONE: "✔ ", FAILED: "⚠ "}[status]
        item.setText(f"{name}\n{mark}{text}")
        self._apply_status_color(item)
        if update_title:
            self._update_list_title()

    def _update_list_title(self) -> None:
        total = len(self._items)
        self.lbl_list.setText(f"Photos  ({self._done_count}/{total} done)" if total else "Photos")

    def _selected_ids(self) -> list[int]:
        return [it.data(ROLE_ID) for it in self.list.selectedItems()]

    def _remove_selected(self) -> None:
        if self._busy():
            return
        self._thumb_pool.clear()
        for item_id in self._selected_ids():
            item = self._items.pop(item_id, None)
            if item is not None:
                if item.data(ROLE_STATUS) == DONE:
                    self._done_count -= 1
                self.list.takeItem(self.list.row(item))
        self._requeue_thumbnails()
        self._update_list_title()
        self._show_current()
        self._update_actions()

    def _requeue_thumbnails(self) -> None:
        """After clearing the thumbnail queue, queue the photos that still show a placeholder."""
        for item_id, item in self._items.items():
            if not item.data(ROLE_THUMB):
                self._thumb_pool.start(ThumbnailLoader(item_id, Path(item.data(ROLE_PATH)), THUMB, self._loader_signals))

    def _clear(self) -> None:
        if self._busy():
            return
        self._thumb_pool.clear()
        self._preview_pool.clear()
        self.list.clear()
        self._items.clear()
        self._done_count = 0
        self._update_list_title()
        self._show_current()
        self._update_actions()

    def _choose_files(self) -> None:
        exts = " ".join(f"*{e}" for e in sorted(INPUT_EXTENSIONS))
        start = self._settings.value("last_dir", str(Path.home() / "Pictures"))
        files, _ = QFileDialog.getOpenFileNames(self, "Add photos", start, f"Photos ({exts});;All files (*)")
        if files:
            self._settings.setValue("last_dir", str(Path(files[0]).parent))
            self.add_paths([Path(f) for f in files])

    def _choose_folder(self) -> None:
        start = self._settings.value("last_dir", str(Path.home() / "Pictures"))
        folder = QFileDialog.getExistingDirectory(self, "Add all photos in a folder", start)
        if folder:
            self._settings.setValue("last_dir", folder)
            self.add_paths([Path(folder)])

    def _context_menu(self, pos) -> None:
        item = self.list.itemAt(pos)
        if item is None:
            return
        menu = QMenu(self)
        out = item.data(ROLE_OUTPUT)
        a_open = menu.addAction("Open result")
        a_show = menu.addAction("Show result in folder")
        a_open.setEnabled(bool(out) and Path(out).exists())
        a_show.setEnabled(bool(out) and Path(out).exists())
        a_src = menu.addAction("Show original in folder")
        menu.addSeparator()
        a_again = menu.addAction("Process again")
        a_again.setEnabled(not self._busy())
        a_rm = menu.addAction("Remove from list")
        a_rm.setEnabled(not self._busy())
        chosen = menu.exec(self.list.viewport().mapToGlobal(pos))
        if chosen is a_open:
            self._open_item_result(item)
        elif chosen is a_show:
            _reveal(Path(out))
        elif chosen is a_src:
            _reveal(Path(item.data(ROLE_PATH)))
        elif chosen is a_again:
            # The statuses change only once the batch really starts (not if a download is refused).
            self._start([it.data(ROLE_ID) for it in (self.list.selectedItems() or [item])])
        elif chosen is a_rm:
            self._remove_selected()

    def _open_item_result(self, item: QListWidgetItem) -> None:
        out = item.data(ROLE_OUTPUT)
        if out and Path(out).exists():
            QDesktopServices.openUrl(QUrl.fromLocalFile(out))

    def _open_output_folder(self) -> None:
        item = self.list.currentItem()
        target: Path | None = None
        if item is not None and item.data(ROLE_OUTPUT):
            target = Path(item.data(ROLE_OUTPUT))
            _reveal(target)
            return
        if item is not None:
            target = output_folder_for(Path(item.data(ROLE_PATH)), self._options)
        elif self._options.output_mode == "folder" and self._options.output_folder:
            target = Path(os.path.expandvars(os.path.expanduser(self._options.output_folder)))
        if target is not None and target.exists():
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(target)))

    # =========================================================== preview
    def _show_current(self) -> None:
        item = self.list.currentItem()
        self._preview_token += 1
        self._loader_signals.latest_token = self._preview_token  # loads for other photos stop early
        if item is None:
            self._preview_timer.stop()
            self.view.set_busy_text("")
            self.view.set_images(None, None)
            self.lbl_zoom.setText("")
            return
        stage = item.data(ROLE_STAGE)
        self.view.set_busy_text(f"{stage}…" if item.data(ROLE_STATUS) == WORKING and stage else "")
        self._preview_timer.start()

    def _load_preview(self) -> None:
        item = self.list.currentItem()
        if item is None or self._closing:
            return
        out = item.data(ROLE_OUTPUT)
        out_path = Path(out) if out and item.data(ROLE_STATUS) == DONE else None
        self._preview_pool.clear()  # queued loads for photos no longer selected
        self._backdrop_loading = None  # a queued background fit may be gone too; asked again once loaded
        self._preview_pool.start(
            PreviewLoader(self._preview_token, Path(item.data(ROLE_PATH)), out_path, self._loader_signals)
        )

    def _on_preview_loaded(self, token: int, before, after, error: str) -> None:
        if token != self._preview_token:
            return
        if before is None and after is None:
            if error:
                self.view.set_error("This photo could not be opened", error)
            else:
                self.view.set_images(None, None)
            self.lbl_zoom.setText("")
            return
        if before is not None and after is not None and before.size() != after.size():
            before = None  # cropped result: the slider would not line up
        self.view.set_images(before, after, keep_view=True)
        self._on_zoom(self.view.zoom_level())
        self._request_backdrop()  # fitted to this result's size

    def _on_zoom(self, zoom: float) -> None:
        self.lbl_zoom.setText(f"{zoom * 100:.0f}%" if self.view.has_image() else "")

    # ======================================================== processing
    def _busy(self) -> bool:
        return (self._worker is not None and self._worker.isRunning()) or (
            self._downloader is not None and self._downloader.isRunning()
        )

    def _running_threads(self) -> list:
        threads = (self._worker, self._downloader, self._warmer, self._updater, self._app_checker, self._app_installer)
        return [t for t in threads if t is not None and t.isRunning()]

    def has_running_threads(self) -> bool:
        return bool(self._running_threads())

    def _update_actions(self) -> None:
        busy = self._busy()
        has_items = bool(self._items)
        self.btn_start.setEnabled(has_items and not busy)
        self.btn_stop.setEnabled(busy)
        self.act_remove.setEnabled(bool(self.list.selectedItems()) and not busy)
        self.act_clear.setEnabled(has_items and not busy)
        self.panel.setEnabled(not busy)

    def _start(self, ids: list[int] | None = None) -> None:
        """Process the waiting and failed photos, or exactly `ids` when given."""
        if self._closing or self._busy() or not self._items:
            return
        opts = replace(self._options)
        if opts.output_mode == "folder" and not opts.output_folder.strip():
            QMessageBox.information(self, "Choose a folder", "Pick the folder to save results in, or choose “Next to each photo”.")
            return
        if opts.background == "image":
            # Checked once here, so a missing background does not fail every photo in turn.
            if not opts.background_image:
                QMessageBox.information(
                    self,
                    "Choose a background image",
                    "Choose the image to put behind the person under Background › Image, "
                    "or pick Transparent or Solid colour.",
                )
                return
            background = imageio.resolve_background(opts.background_image)
            if not background.is_file():
                QMessageBox.warning(
                    self,
                    "Background image not found",
                    f"The background image cannot be found:\n{background}\n\n"
                    "Choose another one under Background › Image.",
                )
                return
        if ids is None:
            batch = [it for it in self._iter_items() if it.data(ROLE_STATUS) in (PENDING, FAILED)]
            if not batch:
                answer = QMessageBox.question(
                    self,
                    "Process again?",
                    "All photos are already done. Process them again with the current settings?",
                )
                if answer != QMessageBox.StandardButton.Yes:
                    return
                batch = list(self._iter_items())
        else:
            batch = [self._items[i] for i in ids if i in self._items]
            if not batch:
                return

        # Ask about missing models before touching any status, so a refusal changes nothing.
        missing = [s for s in self._engine.required_models(opts) if models.find_model(s) is None]
        if missing:
            self._download_then_start(missing, [it.data(ROLE_ID) for it in batch])
            return
        self._run_batch(batch, opts)

    def _run_batch(self, batch: list[QListWidgetItem], opts: Options) -> None:
        # Name every result once for the whole list, so photos with the same name never
        # overwrite each other and the preview always pairs a photo with its own result.
        sources = {it.data(ROLE_ID): Path(it.data(ROLE_PATH)) for it in self._iter_items()}
        batch_ids = {it.data(ROLE_ID) for it in batch}
        # Results of photos outside this batch stay untouched.
        reserved = [
            Path(it.data(ROLE_OUTPUT))
            for it in self._iter_items()
            if it.data(ROLE_ID) not in batch_ids and it.data(ROLE_OUTPUT)
        ]
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            plan = plan_outputs(list(sources.values()), opts, reserved)
        except Exception as exc:
            QApplication.restoreOverrideCursor()
            QMessageBox.warning(self, "Cannot name the results", str(exc) or exc.__class__.__name__)
            return
        QApplication.restoreOverrideCursor()

        jobs: list[Job] = []
        for it in batch:
            item_id = it.data(ROLE_ID)
            src = sources[item_id]
            self._set_status(item_id, PENDING, "Waiting", update_title=False)
            it.setToolTip(str(src))
            jobs.append(Job(item_id, src, plan[src]))
        self._update_list_title()

        self._batch_ids = batch_ids
        self._batch_total = len(jobs)
        self._batch_done = 0
        self.progress.setRange(0, self._batch_total)
        self.progress.setValue(0)
        self.progress.setFormat("%v / %m")
        self.progress.setVisible(True)
        self._warm_timer.stop()
        self._engine.cancel_warm_up()  # the batch loads whatever it still needs itself
        self._warmed_key = self._warm_key(opts)  # the batch loads exactly these models
        if self._updater is not None and not self._updater.manual:
            # Automatic update checks only run while no photos are processed; a download
            # continues where it stopped on the next try.
            self._updater.cancel()
        self._engine.device_label = ""  # report the placement again for these settings
        self._worker = BatchWorker(self._engine, jobs, opts, self)
        self._worker.itemStarted.connect(self._on_item_started)
        self._worker.itemStage.connect(self._on_item_stage)
        self._worker.itemDone.connect(self._on_item_done)
        self._worker.itemFailed.connect(self._on_item_failed)
        self._worker.itemCancelled.connect(self._on_item_cancelled)
        self._worker.deviceChanged.connect(self._on_device)
        self._worker.finished.connect(self._on_batch_finished)
        self._worker.start()
        if self._warmer is not None and self._warmer.isRunning():
            self._status(f"Loading the AI models, then working on {self._batch_total} photo(s)…")
        else:
            self._status(f"Working on {self._batch_total} photo(s)…")
        self._update_actions()
        self._update_title()

    def _iter_items(self):
        for row in range(self.list.count()):
            yield self.list.item(row)

    def _stop(self) -> None:
        self._cancel_download()
        if self._worker is not None and self._worker.isRunning():
            self._worker.cancel()
            self._status("Stopping after the current step…")

    def _on_item_started(self, item_id: int) -> None:
        self._set_status(item_id, WORKING, "Starting")
        item = self._items.get(item_id)
        if item is not None and self.list.currentItem() is item:
            self.view.set_busy_text("Starting…")
        if self._batch_done == 0:
            self._status(f"Working on {self._batch_total} photo(s)…")

    def _on_item_stage(self, item_id: int, text: str) -> None:
        self._set_status(item_id, WORKING, text)
        item = self._items.get(item_id)
        if item is not None and self.list.currentItem() is item:
            self.view.set_busy_text(text + "…")

    def _on_item_done(self, item_id: int, output: str, seconds: float) -> None:
        self._set_status(item_id, DONE, f"Done in {seconds:.1f} s", output)
        self._batch_done += 1
        self.progress.setValue(self._batch_done)
        self._update_title()
        item = self._items.get(item_id)
        if item is not None:
            item.setToolTip(f"{item.data(ROLE_PATH)}\n→ {output}")
            if self.list.currentItem() is item:
                self._show_current()

    def _on_item_failed(self, item_id: int, message: str) -> None:
        self._set_status(item_id, FAILED, "Failed — hover for details")
        item = self._items.get(item_id)
        if item is not None:
            item.setToolTip(f"{item.data(ROLE_PATH)}\n\n{message}")
            if self.list.currentItem() is item:
                self.view.set_busy_text("")
        self._batch_done += 1
        self.progress.setValue(self._batch_done)
        self._update_title()

    def _on_item_cancelled(self, item_id: int) -> None:
        self._set_status(item_id, PENDING, "Stopped")
        item = self._items.get(item_id)
        if item is not None and self.list.currentItem() is item:
            self.view.set_busy_text("")

    def _on_device(self, label: str) -> None:
        self.lbl_device.setText(f"Processor: {label}")

    def _on_batch_finished(self) -> None:
        worker = self._worker
        self._worker = None
        stopped = worker.was_cancelled() if worker is not None else False
        if worker is not None:
            worker.deleteLater()
        batch_ids, self._batch_ids = self._batch_ids, set()
        self.view.set_busy_text("")
        self.progress.setVisible(False)
        self._update_actions()
        self._update_title()
        if self._closing:
            return
        self._schedule_update_retry()
        # Photos added while the batch ran (from Explorer, a drop or Add photos).
        added = [
            it.data(ROLE_ID)
            for it in self._iter_items()
            if it.data(ROLE_STATUS) == PENDING and it.data(ROLE_ID) not in batch_ids
        ]
        if added and not stopped:
            self._status(f"Continuing with {len(added)} photo(s) added during the batch…")
            QTimer.singleShot(0, lambda ids=added: self._start(ids))
            return
        if self._pending_update is not None:
            self._status("Photos done; starting the update…")
            self._resume_pending_update()
            return
        failed = sum(1 for it in self._iter_items() if it.data(ROLE_STATUS) == FAILED)
        msg = f"Finished: {self._done_count} done"
        if failed:
            msg += f", {failed} failed (hover over them for the reason)"
        msg += "."
        if added:
            msg += f" {len(added)} photo(s) added during the batch are waiting — click Remove backgrounds."
        self._status(msg)
        if not self.isActiveWindow():
            QApplication.alert(self, 0)  # flash the taskbar button until the user looks

    def _update_title(self) -> None:
        if self._worker is not None and self._batch_total:
            self.setWindowTitle(f"{self._batch_done}/{self._batch_total} — {APP_TITLE}")
        else:
            self.setWindowTitle(APP_TITLE)

    # ========================================================== download
    def _download_then_start(self, specs: list[models.ModelSpec], ids: list[int]) -> None:
        names = "\n".join(f"  • {s.title} — {format_size(s.size)}" for s in specs)
        answer = QMessageBox.question(
            self,
            "Download AI models",
            "These models are needed and are not on this PC yet:\n\n"
            f"{names}\n\nTotal {format_size(sum(s.size for s in specs))}, downloaded once and checked. "
            "Download now?",
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self._download_models(specs, lambda: self._start(ids))

    def _download_all(self, include_all: bool) -> None:
        """Advanced › Offline use: fetch every model now, so the app also works without internet."""
        if self._closing or self._busy():
            return
        specs = models.offline_models(include_all)
        missing = [s for s in specs if models.find_model(s) is None]
        folder = paths.model_dir()
        portable_note = (
            f"\n\nThis copy is portable: afterwards the whole folder\n{paths.app_dir()}\n"
            "can be copied to a PC without internet."
            if paths.is_portable()
            else ""
        )
        if not missing:
            QMessageBox.information(
                self,
                "Ready for offline use",
                f"All {len(specs)} models are already on this PC.\n\nFolder: {folder}{portable_note}",
            )
            self.panel.refresh_offline_info()
            return
        if not paths.folder_writable(folder):
            QMessageBox.warning(
                self,
                "Cannot save models there",
                f"Windows does not allow saving in:\n{folder}\n\n"
                "Move the Background Editor folder to a place where you can write, such as Documents or a USB drive.",
            )
            return
        names = "\n".join(f"  • {s.title} — {format_size(s.size)}" for s in missing)
        answer = QMessageBox.question(
            self,
            "Download models for offline use",
            f"These models are not on this PC yet:\n\n{names}\n\n"
            f"Total {format_size(sum(s.size for s in missing))}, downloaded once and checked, into:\n{folder}"
            f"{portable_note}\n\nDownload now?",
        )
        if answer != QMessageBox.StandardButton.Yes:
            return

        def done() -> None:
            QMessageBox.information(
                self,
                "Ready for offline use",
                f"All {len(specs)} models are on this PC.\n\nFolder: {folder}{portable_note}",
            )

        self._download_models(missing, done)

    def _download_models(self, specs: list[models.ModelSpec], after: Callable[[], None] | None) -> None:
        """Download with a progress dialog; `after` runs once every file arrived and checked out."""
        dlg = QProgressDialog("Preparing download…", "Cancel", 0, 1000, self)
        dlg.setWindowTitle("Downloading models")
        dlg.setWindowModality(Qt.WindowModality.WindowModal)
        dlg.setMinimumDuration(0)
        dlg.setAutoClose(False)
        dlg.setAutoReset(False)
        self._download_ok = False
        self._after_download = after
        self._downloader = DownloadWorker(specs, self)

        def on_progress(title: str, done: int, total: int) -> None:
            dlg.setLabelText(f"{title}\n{done / 1e6:.0f} of {total / 1e6:.0f} MB")
            dlg.setValue(int(1000 * done / max(total, 1)))

        def close_dialog() -> None:
            # QProgressDialog emits 'canceled' when it closes; that must not cancel a finished download.
            try:
                dlg.canceled.disconnect(cancel_conn)
            except (TypeError, RuntimeError):
                pass
            dlg.close()
            dlg.deleteLater()

        def on_failed(message: str) -> None:
            close_dialog()
            if message and not self._closing:
                QMessageBox.warning(
                    self,
                    "Download failed",
                    f"The model could not be downloaded:\n\n{message}\n\n"
                    f"You can also place the .onnx files by hand in:\n{paths.model_dir()}",
                )

        def on_done() -> None:
            close_dialog()
            # The thread is still finishing here; go on once it has (see _on_download_finished).
            self._download_ok = True

        self._downloader.progress.connect(on_progress)
        self._downloader.failed.connect(on_failed)
        self._downloader.succeeded.connect(on_done)
        self._downloader.finished.connect(self._on_download_finished)
        cancel_conn = dlg.canceled.connect(self._cancel_download)
        self._downloader.start()
        self._status("Downloading the AI models…")
        self._update_actions()

    def _cancel_download(self) -> None:
        d = self._downloader
        if d is None or not d.isRunning() or d.was_cancelled():
            return
        d.cancel()
        # The download stops at its next check; until then show that something is happening.
        self._status("Cancelling the download…")
        self.progress.setRange(0, 0)
        self.progress.setVisible(True)

    def _on_download_finished(self) -> None:
        d = self._downloader
        self._downloader = None
        ok, after = self._download_ok, self._after_download
        self._download_ok = False
        self._after_download = None
        cancelled = d is not None and d.was_cancelled() and not ok
        if d is not None:
            d.deleteLater()
        self.progress.setVisible(False)
        self.progress.setRange(0, 1)
        self._update_actions()
        if self._closing:
            return
        self.panel.refresh_offline_info()
        if cancelled:
            self._status("Download cancelled. Next time it continues where it stopped.")
        elif ok:
            self._status("Models downloaded and checked.")
        else:
            self._status("")
        if ok and after is not None:
            QTimer.singleShot(0, after)
        self._resume_pending_update()  # after `after`: a batch it starts makes the update wait again

    # ===================================================== model updates
    def _auto_update_check(self) -> None:
        """The weekly check, a minute after start; never while photos are being processed."""
        if self._closing or self._options.model_updates == "off" or self._updater is not None:
            return
        if self._busy():
            self._update_timer.start(UPDATE_RETRY_MS)
            return
        try:
            due = updates.is_check_due()
        except Exception as exc:  # never let the update bookkeeping disturb the app
            print(f"Model updates: cannot tell whether a check is due: {exc!r}", file=sys.stderr)
            return
        if due:
            self._start_updater(self._options.model_updates, force=False, manual=False)

    def _schedule_update_retry(self) -> None:
        """After a batch: an automatic check that had to wait (or was stopped) gets its turn."""
        if self._options.model_updates != "off" and self._updater is None and not self._update_timer.isActive():
            self._update_timer.start(UPDATE_RETRY_MS)

    def _check_updates_now(self, combined: bool = False) -> None:
        """The model part of Check for updates (combined: its result goes into the summary)."""
        if self._closing:
            return
        if self._updater is not None:
            self._updater.manual = True  # a running automatic check now reports like a manual one
            self._updater.combined = combined
            return
        # By hand with updates off, still ask before downloading anything.
        mode = self._options.model_updates if self._options.model_updates != "off" else "ask"
        self._start_updater(mode, force=True, manual=True)
        self._updater.combined = combined

    def _start_updater(self, mode: str, force: bool, manual: bool, candidate: str = "") -> None:
        self._preferred_before_update = models.preferred_segmenter()
        worker = ModelUpdateWorker(mode, force=force, candidate=candidate, manual=manual, parent=self)
        worker.progress.connect(self._on_update_progress)
        worker.finished.connect(self._on_updater_finished)
        self._updater = worker
        text = "Getting the newer model…" if candidate else "Looking for a newer model…"
        self.panel.show_update_status(text)
        self._set_update_message(text, persist=True)
        worker.start()

    def _on_update_progress(self, text: str, done: int, total: int) -> None:
        line = _progress_text(text, done, total)
        self.panel.show_update_status(line)
        self._set_update_message(line, persist=True)
        dlg = self._adopt_dialog
        if dlg is not None:
            dlg.setLabelText(line)
            if total > 0:
                dlg.setRange(0, 1000)
                dlg.setValue(int(1000 * min(done, total) / total))
            else:
                dlg.setRange(0, 0)

    def _on_updater_finished(self) -> None:
        worker = self._updater
        self._updater = None
        if worker is None:
            return
        report = worker.result
        cancelled = worker.was_cancelled()  # before the dialog closes: closing it emits 'canceled'
        worker.deleteLater()
        if self._adopt_dialog is not None:
            dlg, self._adopt_dialog = self._adopt_dialog, None
            try:
                dlg.canceled.disconnect()
            except (TypeError, RuntimeError):
                pass
            dlg.close()
            dlg.deleteLater()
        if self._closing:
            if report is not None and report.status == "adopted":
                # Adopted while closing: keep Best/Balanced on the new model for the next start.
                self._follow_presets(self._preferred_before_update)
                self._save_options()
            return
        self.panel.show_update_status()
        if report is None:
            self._set_update_message("")
            if worker.combined:
                self._manual_part_done("models", "The model update check did not report back.", failed=True)
            return
        if cancelled and report.status != "adopted":
            # Stopped for a batch or by the user: nothing to report, try again later.
            text = "Model update check paused." if not worker.manual else "Model update stopped."
            self._set_update_message(text)
            if worker.combined:
                self._manual_part_done("models", text)
            return
        self._handle_update_report(report, worker)

    def _handle_update_report(self, report, worker: ModelUpdateWorker) -> None:
        status = report.status
        message = (report.message or "").strip()
        # Part of Check for updates: one summary box for the app and the models afterwards.
        combined = worker.combined and self._manual_check is not None
        loud = worker.manual and not combined
        if status == "adopted":
            self._after_adoption(report, loud=loud)
            summary = f"Best and Balanced now use the newer model {model_title(models.preferred_segmenter())}."
        elif status == "notified" and report.candidate and not worker.candidate and worker.mode == "ask":
            self._offer_candidate(report)
            summary = "A newer model is available; you are asked separately whether to download it."
        elif status == "notified":
            # The message is a whole sentence with the reason, from bgeditor.updates.
            summary = message or "A newer model was found, but the app did not switch to it."
            self._set_update_message(summary, tooltip=report.details, persist=True)
            if loud:
                _message_box(self, QMessageBox.Icon.Information, "Model updates", summary, report.details)
        elif status == "failed":
            summary = message or "The model update did not work. It tries again later."
            self._set_update_message(summary, tooltip=report.details, persist=not worker.manual)
            if loud:
                _message_box(self, QMessageBox.Icon.Warning, "Model updates", summary, report.details)
        else:  # "up-to-date" or "skipped"
            if status == "up-to-date":
                summary = message or "The newest suitable model is already in use."
            else:
                summary = message or "Model update check skipped."
            if worker.manual or status == "up-to-date":
                self._set_update_message(summary, tooltip=report.details)
            else:
                self._set_update_message("")
        if combined:
            self._manual_part_done("models", summary, failed=status == "failed", details=report.details)

    def _offer_candidate(self, report) -> None:
        """Ask first: offer the newer model in a dialog once the window is free for one."""
        self._update_offer = report
        self._set_update_message("A newer model is available.", tooltip=report.message, persist=True)
        self._show_pending_offer()

    def _show_pending_offer(self) -> None:
        report = self._update_offer
        if report is None or self._closing:
            return
        if self._busy() or self._updater is not None or QApplication.activeModalWidget() is not None or not self.isActiveWindow():
            self._offer_timer.start()  # not while working, or over another dialog
            return
        self._update_offer = None
        title = model_title(report.candidate) if report.candidate in models.MODELS else report.candidate
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Question)
        box.setWindowTitle("Newer model available")
        box.setText(report.message or f"A newer person model is available: {title}.")
        info = []
        if report.licence and report.licence not in (report.message or ""):
            info.append(f"Licence: {report.licence}")
        info.append(
            "It is downloaded, checked on test photos and then used by Best and Balanced. "
            "The current model stays on this PC."
        )
        box.setInformativeText("\n\n".join(info))
        if report.details:
            box.setDetailedText(report.details)
        use = box.addButton("Download and use", QMessageBox.ButtonRole.AcceptRole)
        box.addButton("Not now", QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(use)
        _plain_text(box).exec()
        if box.clickedButton() is not use:
            self._set_update_message("Newer model not downloaded. Check for updates offers it again.")
            return
        self._adopt(report.candidate)

    def _adopt(self, candidate: str) -> None:
        if self._updater is not None or self._closing:
            return
        dlg = QProgressDialog("Preparing…", "Cancel", 0, 0, self)
        dlg.setWindowTitle("Newer model")
        dlg.setWindowModality(Qt.WindowModality.WindowModal)
        dlg.setMinimumDuration(0)
        dlg.setAutoClose(False)
        dlg.setAutoReset(False)
        dlg.setMinimumWidth(420)
        self._adopt_dialog = dlg
        self._start_updater("ask", force=True, manual=True, candidate=candidate)
        worker = self._updater
        dlg.canceled.connect(lambda: worker is not None and worker.cancel())
        dlg.show()

    def _after_adoption(self, report, loud: bool) -> None:
        """A newer model passed every check: Best and Balanced use it from now on."""
        old_key = self._preferred_before_update
        new_key = models.preferred_segmenter()
        self.panel.refresh_models()  # first, so the model list holds a newly adopted model
        self._follow_presets(old_key)
        text = f"Best and Balanced now use the newer model {model_title(new_key)}."
        self._set_update_message(text, tooltip=report.details, persist=True)
        if not self._busy():
            self._status(text)
        if loud:
            info = report.message or ""
            if report.licence and report.licence not in info:
                info = (info + "\n\n" if info else "") + f"Licence: {report.licence}"
            _message_box(self, QMessageBox.Icon.Information, "Model updated", text, report.details, info)

    def _switch_preferred(self, key: str) -> None:
        """Advanced › Model updates: Best and Balanced go back to (or return to) another model."""
        old_key = models.preferred_segmenter()
        if key == old_key:
            return
        try:
            models.set_preferred(key)
        except (OSError, ValueError) as exc:
            QMessageBox.warning(self, "Model updates", f"The model could not be switched:\n{exc}")
            return
        self.panel.refresh_models()
        self._follow_presets(old_key)
        self._status(f"Best and Balanced now use {model_title(models.preferred_segmenter())}.")

    def _follow_presets(self, old_key: str) -> None:
        """Settings that were Best or Balanced with the old model become the same preset with
        the new one; custom settings keep their model."""
        o = self._options
        current = presets()
        for name in ("best", "balanced"):
            values = dict(current.get(name, {}))
            if not values or values.get("model") == old_key:
                continue
            before = dict(values, model=old_key)
            if all(getattr(o, k, None) == v for k, v in before.items()):
                opts = replace(o, **values)
                self.panel.set_options(opts)
                self._on_options(opts)
                return

    def _set_update_message(self, text: str, tooltip: str = "", persist: bool = False) -> None:
        """Model update news in its own corner of the status bar; routine news fades after a while."""
        self._update_msg_timer.stop()
        fm = self.lbl_update.fontMetrics()
        self.lbl_update.setText(fm.elidedText(text, Qt.TextElideMode.ElideRight, 460))
        self.lbl_update.setToolTip("\n\n".join(t for t in (text, tooltip) if t))
        self.lbl_update.setVisible(bool(text))
        if text and not persist:
            self._update_msg_timer.start(UPDATE_MESSAGE_MS)

    # ======================================================= app updates
    def _clean_old_update_downloads(self) -> None:
        """Remove the downloads of an update that is done (only files the app recorded itself)."""
        if self._closing:
            return

        def run() -> None:
            try:
                removed = app_updates.cleanup_old_downloads()
            except Exception as exc:  # never let the bookkeeping disturb the app
                print(f"App updates: cleaning up old downloads failed: {exc!r}", file=sys.stderr)
                return
            if removed:
                print(f"App updates: removed old downloads {', '.join(removed)}", file=sys.stderr)

        threading.Thread(target=run, name="app-update-cleanup", daemon=True).start()

    def _auto_app_update_check(self) -> None:
        """The daily check for a new version, 30 s after start; never while photos are processed."""
        if self._closing or not self._options.app_update_check or self._app_checker is not None:
            return
        if self._busy():
            self._app_update_timer.start(UPDATE_RETRY_MS)
            return
        try:
            due = app_updates.is_check_due()
        except Exception as exc:  # never let the update bookkeeping disturb the app
            print(f"App updates: cannot tell whether a check is due: {exc!r}", file=sys.stderr)
            return
        if due:
            self._start_app_checker(manual=False)

    def _start_app_checker(self, manual: bool) -> None:
        worker = AppUpdateWorker("check", force=manual, manual=manual, parent=self)
        worker.combined = manual
        worker.finished.connect(self._on_app_checker_finished)
        self._app_checker = worker
        worker.start()

    def _on_app_checker_finished(self) -> None:
        worker, self._app_checker = self._app_checker, None
        if worker is None:
            return
        report = worker.result
        worker.deleteLater()
        if self._closing:
            return
        if report is None:
            report = app_updates.AppUpdateReport("failed", worker.error or "The check for a new version did not work.")
        self.panel.show_app_update_status()
        if report.status == "available":  # only a release whose signed manifest verified
            self._show_update_bar(report)
        # At startup only a verified new version is worth a word (the bar); after Check for
        # updates every outcome goes into the summary. A release that failed verification
        # ('unverified') is a warning there, without any link to it.
        if worker.combined:
            text = report.message
            if report.status == "available":
                if report.installable:
                    text += " Update now is in the bar above the preview."
                else:
                    text += " The bar above the preview opens the release page."
            warn = report.status in ("failed", "unverified")
            self._manual_part_done("app", text, failed=warn, details=report.details)

    def _check_for_updates(self) -> None:
        """Check for updates (toolbar, About, Advanced › Updates): a new version of the app,
        always, and newer models in the chosen mode (with model updates off it still looks,
        and asks before downloading). One summary box follows when both are done."""
        if self._closing or self._manual_check is not None:
            return
        self._manual_check = {"pending": {"app", "models"}}
        self.act_check_updates.setEnabled(False)
        self._set_update_message("Checking for updates…", persist=True)
        if self._app_checker is not None:
            # The startup check is running (it only runs when due, so it asks GitHub): use it.
            self._app_checker.manual = self._app_checker.combined = True
        else:
            self._start_app_checker(manual=True)
        self._check_updates_now(combined=True)

    def _manual_part_done(self, part: str, text: str, failed: bool = False, details: str = "") -> None:
        check = self._manual_check
        if check is None:
            return
        check[part] = (text, failed, details)
        check["pending"].discard(part)
        if check["pending"]:
            return
        self._manual_check = None
        self.act_check_updates.setEnabled(True)
        if self.lbl_update.toolTip().startswith("Checking for updates"):
            self._set_update_message("")
        app_text, app_failed, app_details = check.get("app", ("", False, ""))
        model_text, model_failed, model_details = check.get("models", ("", False, ""))
        box = QMessageBox(
            QMessageBox.Icon.Warning if (app_failed or model_failed) else QMessageBox.Icon.Information,
            "Check for updates",
            app_text or "The check for a new version did not report back.",
            QMessageBox.StandardButton.Ok,
            self,
        )
        if model_text:
            box.setInformativeText(f"AI models: {model_text}")
        detail = "\n\n".join(d for d in (app_details, model_details) if d)
        if detail:
            box.setDetailedText(detail)
        self._summary_box = box  # for the smoke tests
        _plain_text(box).exec()
        self._summary_box = None

    def _show_update_bar(self, report) -> None:
        self._app_report = report
        self.update_bar.set_report(report)
        self.update_bar.setVisible(True)

    def _on_bar_update(self) -> None:
        report = self._app_report
        if report is None or report.status != "available":
            return
        if not report.installable:
            # A verified release this copy cannot install by itself; only a github.com page opens.
            open_release_link(QUrl(report.html_url or app_updates.RELEASES_URL))
            return
        self._update_now(report)

    def _on_bar_skip(self) -> None:
        report = self._app_report
        if report is None:
            return
        try:
            app_updates.skip_version(report.latest)
        except Exception as exc:  # the choice is not stored: say so, keep the bar
            QMessageBox.warning(self, "Skip this version", f"The choice could not be saved:\n{exc}")
            return
        self.update_bar.setVisible(False)
        self._app_report = None
        self.panel.show_app_update_status()
        self._status(f"Version {report.latest} is not announced at startup any more; Check for updates still shows it.")

    def _show_release_notes(self, report) -> None:
        if report is None:
            return
        dlg = QDialog(self)
        dlg.setWindowTitle(f"What's new in Background Editor {report.latest}")
        dlg.resize(640, 540)
        v = QVBoxLayout(dlg)
        head = QLabel(
            f"<b>Background Editor {html.escape(report.latest)}</b> — you have {html.escape(report.current)}."
        )
        v.addWidget(head)
        # The notes are not covered by the signed manifest: shown inertly (no image or other
        # resource is loaded; only https links on github.com open).
        view = ReleaseNotesView(report.notes)
        v.addWidget(view, 1)
        url = report.html_url or app_updates.RELEASES_URL
        link = QLabel(f"<a href='{html.escape(url, quote=True)}'>Release page on GitHub</a>")
        link.setOpenExternalLinks(False)
        link.linkActivated.connect(open_release_link)
        link.setToolTip(url)
        v.addWidget(link)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        go = bb.addButton("Update now" if report.installable else "Open release page", QDialogButtonBox.ButtonRole.AcceptRole)
        go.clicked.connect(dlg.accept)
        bb.rejected.connect(dlg.reject)
        v.addWidget(bb)
        self._notes_dialog = dlg  # for the smoke tests
        accepted = dlg.exec() == QDialog.DialogCode.Accepted
        self._notes_dialog = None
        if accepted:
            self._on_bar_update()

    def _update_now(self, report) -> None:
        """Update now: wait for or stop running work, confirm, then download, check and install."""
        if self._closing or self._app_installer is not None or report is None:
            return
        if self._busy():
            processing = self._worker is not None and self._worker.isRunning()
            what = "Photos are still being processed." if processing else "An AI model is still downloading."
            box = QMessageBox(QMessageBox.Icon.Question, "Update Background Editor", what, parent=self)
            box.setInformativeText(
                "Background Editor closes for the update. Update once the work is done, or stop it now?"
            )
            wait = box.addButton("Update when done", QMessageBox.ButtonRole.AcceptRole)
            stop = box.addButton("Stop and update", QMessageBox.ButtonRole.DestructiveRole)
            box.addButton(QMessageBox.StandardButton.Cancel)
            box.setDefaultButton(wait)
            _plain_text(box).exec()
            if box.clickedButton() is wait:
                self._pending_update = report
                self._status(f"Version {report.latest} is installed once the current work is done.")
            elif box.clickedButton() is stop:
                self._pending_update = report
                self._stop()
            return
        if report.portable:
            text = (
                f"Background Editor will close, replace its program files with version {report.latest} "
                "and restart; your models and settings stay."
            )
        else:
            text = f"Background Editor will close to install version {report.latest}."
        box = QMessageBox(QMessageBox.Icon.Question, "Update Background Editor", text, parent=self)
        box.setInformativeText(
            f"The download is {format_size(report.asset_size)}. Every byte is checked against the release's "
            "manifest, signed by Optimey, before anything is installed."
        )
        go = box.addButton("Update now", QMessageBox.ButtonRole.AcceptRole)
        box.addButton(QMessageBox.StandardButton.Cancel)
        box.setDefaultButton(go)
        _plain_text(box).exec()
        if box.clickedButton() is not go:
            return
        self._start_install(report)

    def _resume_pending_update(self) -> None:
        if self._pending_update is not None:
            QTimer.singleShot(0, self._run_pending_update)

    def _run_pending_update(self) -> None:
        report = self._pending_update
        if report is None or self._closing or self._busy():
            return  # still busy: the next finished batch or download tries again
        self._pending_update = None
        self._update_now(report)

    def _start_install(self, report) -> None:
        dlg = QProgressDialog("Preparing…", "Cancel", 0, 1000, self)
        dlg.setWindowTitle("Updating Background Editor")
        dlg.setWindowModality(Qt.WindowModality.WindowModal)
        dlg.setMinimumDuration(0)
        dlg.setAutoClose(False)
        dlg.setAutoReset(False)
        dlg.setMinimumWidth(440)
        worker = AppUpdateWorker("install", report=report, manual=True, parent=self)

        def on_progress(text: str, done: int, total: int) -> None:
            dlg.setLabelText(_progress_text(text, done, total))
            if total > 0:
                dlg.setRange(0, 1000)
                dlg.setValue(int(1000 * min(done, total) / total))
            else:
                dlg.setRange(0, 0)

        worker.progress.connect(on_progress)
        worker.finished.connect(self._on_installer_finished)
        dlg.canceled.connect(worker.cancel)
        self._install_dialog = dlg
        self._app_installer = worker
        worker.start()
        dlg.show()
        self._status(f"Downloading Background Editor {report.latest}…")

    def _on_installer_finished(self) -> None:
        worker, self._app_installer = self._app_installer, None
        dlg, self._install_dialog = self._install_dialog, None
        if dlg is not None:
            try:
                dlg.canceled.disconnect()  # closing the dialog emits 'canceled'
            except (TypeError, RuntimeError):
                pass
            dlg.close()
            dlg.deleteLater()
        if worker is None:
            return
        started, error, cancelled = worker.result is not None, worker.error, worker.was_cancelled()
        report, integrity = worker.report, bool(getattr(worker, "integrity", False))
        worker.deleteLater()
        if self._closing:
            return
        if started:
            # The installer or the portable helper runs and waits for this window to close.
            self._closing_for_update = True
            self._status("Closing for the update…")
            QTimer.singleShot(0, self.close)
            return
        if cancelled and not error:
            self._status("Update cancelled. The download continues where it stopped next time.")
            return
        if integrity:
            # Not what Optimey signed: no link, and no suggestion to get it elsewhere.
            self._status("The update was refused: the download is not what Optimey signed.")
            box = QMessageBox(
                QMessageBox.Icon.Critical,
                "Update Background Editor",
                "The update was refused: it is not exactly what Optimey signed.",
                parent=self,
            )
            what = f"version {report.latest}" if report is not None and report.latest else "this version"
            box.setInformativeText(
                f"{error}\n\nNothing was installed and Background Editor stays as it is. "
                f"Do not install {what} from any other source."
            )
        else:
            self._status("The update did not work.")
            box = QMessageBox(QMessageBox.Icon.Warning, "Update Background Editor", "The update was not installed.", parent=self)
            box.setInformativeText(f"{error}\n\nBackground Editor stays as it is. Check for updates tries again.")
        box.addButton(QMessageBox.StandardButton.Close)
        self._failure_box = box  # for the smoke tests
        _plain_text(box).exec()
        self._failure_box = None

    # ============================================================== misc
    def _status(self, text: str) -> None:
        self.lbl_status.setText(text)

    def _about(self) -> None:
        dlg = QDialog(self)
        dlg.setWindowTitle("About Background Editor")
        dlg.resize(640, 600)
        v = QVBoxLayout(dlg)
        top = QHBoxLayout()
        top.setSpacing(14)
        logo = QLabel()
        logo.setPixmap(logo_pixmap(80))
        top.addWidget(logo, 0, Qt.AlignmentFlag.AlignTop)
        repo = app_updates.REPO_URL
        title = QLabel(
            "<h2 style='margin:0'>Background Editor</h2>"
            f"<div>Version {html.escape(app_updates.current_version())}</div>"
            f"<div>{html.escape(COPYRIGHT)}</div>"
            f"<div><a href='{html.escape(repo, quote=True)}'>{html.escape(repo.removeprefix('https://'))}</a></div>"
        )
        title.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse | Qt.TextInteractionFlag.LinksAccessibleByMouse
        )
        title.setOpenExternalLinks(True)
        top.addWidget(title, 1)
        v.addLayout(top)

        tabs = QTabWidget()
        tabs.addTab(_rich_text_view(_about_html()), "About")
        tabs.addTab(_rich_text_view(_third_party_html()), "Third-party software")
        tabs.addTab(_rich_text_view(_models_html()), "AI models")
        v.addWidget(tabs, 1)

        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        licence = _legal_file("LICENSE")
        btn = bb.addButton("Licence (GPL v3)…", QDialogButtonBox.ButtonRole.HelpRole)
        btn.setToolTip(str(licence) if licence else "The LICENSE file is missing next to the program.")
        btn.clicked.connect(lambda: _show_text_file(dlg, licence, "GNU General Public License v3"))
        notices = _legal_file("LICENSES.txt")
        btn = bb.addButton("Third-party licences…", QDialogButtonBox.ButtonRole.HelpRole)
        btn.setToolTip(str(notices) if notices else "LICENSES.txt is missing next to the program.")
        btn.clicked.connect(lambda: _show_text_file(dlg, notices, "Third-party licences"))
        source = _source_location()[1]
        btn = bb.addButton("Source code…", QDialogButtonBox.ButtonRole.HelpRole)
        btn.setToolTip(str(source) if source else "The source folder is missing next to the program.")
        btn.setEnabled(source is not None)
        btn.clicked.connect(lambda: source is not None and QDesktopServices.openUrl(QUrl.fromLocalFile(str(source))))
        btn = bb.addButton("Check for updates", QDialogButtonBox.ButtonRole.ActionRole)
        btn.setIcon(QIcon.fromTheme(QIcon.ThemeIcon.SyncSynchronizing))
        btn.setToolTip("Look for a new version of Background Editor and for newer AI models")
        btn.clicked.connect(lambda: (dlg.accept(), QTimer.singleShot(0, self._check_for_updates)))
        bb.rejected.connect(dlg.reject)
        v.addWidget(bb)
        self._about_dialog = dlg  # for the smoke tests
        dlg.exec()
        self._about_dialog = None

    # ============================================================ events
    def dragEnterEvent(self, event: QDragEnterEvent) -> None:  # noqa: N802
        # Photos may also be dropped during a batch; they are processed after it.
        if event.mimeData().hasUrls() and not self._closing:
            event.acceptProposedAction()

    def dropEvent(self, event: QDropEvent) -> None:  # noqa: N802
        paths = [Path(u.toLocalFile()) for u in event.mimeData().urls() if u.isLocalFile()]
        if paths:
            self.add_paths(paths)
            event.acceptProposedAction()

    def changeEvent(self, event) -> None:  # noqa: N802
        super().changeEvent(event)
        if event.type() == QEvent.Type.PaletteChange and hasattr(self, "_backdrop_group"):
            # Light/dark switch: keep the status colours and swatch outlines readable.
            colors = self._status_colors()
            for item in self._items.values():
                self._apply_status_color(item, colors)
            self._refresh_backdrop_icons()
            self._style_primary_button()
        elif event.type() == QEvent.Type.ActivationChange and self.isActiveWindow() and hasattr(self, "view"):
            # Back from another program: the background image may have been edited there.
            self._backdrop_cache.clear()
            if self.view.backdrop() == "image" and not self._closing:
                self._backdrop_shown = None  # fitted again; the current one stays until then
                self._backdrop_timer.start()

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        if not self._icon_refreshed:
            # Once, a moment after the first show: makes a stale taskbar icon cache read our icon.
            self._icon_refreshed = True
            QTimer.singleShot(TASKBAR_ICON_REFRESH_MS, lambda: taskbar.resend_window_icons(self))

    def _style_primary_button(self) -> None:
        """Accent-filled main button, following the Windows accent colour and theme."""
        pal = self.palette()
        accent = pal.color(QPalette.ColorRole.Accent)
        if not accent.isValid() or accent.alpha() == 0:
            accent = pal.color(QPalette.ColorRole.Highlight)
        text = QColor("#FFFFFF") if accent.lightness() < 150 else QColor("#000000")
        hover = accent.lighter(112) if accent.lightness() < 150 else accent.darker(108)
        pressed = accent.darker(115)
        off_bg = pal.color(QPalette.ColorRole.Button)
        off_fg = pal.color(QPalette.ColorGroup.Disabled, QPalette.ColorRole.ButtonText)
        self.btn_start.setStyleSheet(
            "QPushButton { background-color: %s; color: %s; border: 1px solid %s; border-radius: 5px;"
            " padding: 4px 16px; }"
            "QPushButton:hover { background-color: %s; }"
            "QPushButton:pressed { background-color: %s; }"
            "QPushButton:disabled { background-color: %s; color: %s; border-color: %s; }"
            % (
                accent.name(), text.name(), accent.darker(110).name(),
                hover.name(), pressed.name(),
                off_bg.name(), off_fg.name(), pal.color(QPalette.ColorRole.Mid).name(),
            )
        )

    def closeEvent(self, event) -> None:  # noqa: N802
        if self._closing:
            event.ignore()  # already shutting down in the background
            return
        if self._busy():
            if self._worker is None or not self._worker.isRunning():
                title = "Download in progress"
                text = "An AI model is still downloading. Stop the download and quit?\n\nNext time it continues where it stopped."
            else:
                title = "Still working"
                text = "Photos are still being processed. Stop and quit?"
            answer = QMessageBox.question(self, title, text)
            if answer != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
        self._save_state()
        self._closing = True
        for timer in (
            self._preview_timer,
            self._warm_timer,
            self._backdrop_timer,
            self._update_timer,
            self._offer_timer,
            self._app_update_timer,
        ):
            timer.stop()
        self._engine.cancel_warm_up()
        self.closing.emit()
        self._stop()
        if self._updater is not None:
            self._updater.cancel()  # a model download continues where it stopped next time
        for worker in (self._app_checker, self._app_installer):
            if worker is not None:
                worker.cancel()  # an app update download continues where it stopped next time
        self._thumb_pool.clear()
        self._preview_pool.clear()
        self._loader_signals.latest_token = -1  # running preview loads stop early
        self._loader_signals.latest_backdrop = -1
        if not self._running_threads():
            super().closeEvent(event)
            return
        # Never block the UI thread here, and never let Qt destroy a running thread (that
        # aborts the process): hide now and quit once the threads have stopped.
        event.ignore()
        QApplication.instance().setQuitOnLastWindowClosed(False)
        self.hide()
        self._close_deadline = CLOSE_TIMEOUT_MS
        self._close_timer = QTimer(self)
        self._close_timer.setInterval(100)
        self._close_timer.timeout.connect(self._finish_close)
        self._close_timer.start()

    def _finish_close(self) -> None:
        self._close_deadline -= self._close_timer.interval()
        running = self._running_threads()
        if not running:
            self._close_timer.stop()
            QApplication.quit()
            return
        if self._close_deadline <= 0:
            # A model load cannot be interrupted. Settings are saved; end the process
            # rather than wait, or let Qt destroy a thread that is still running.
            self._close_timer.stop()
            names = ", ".join(type(t).__name__ for t in running)
            print(f"Closing: {names} did not stop within {CLOSE_TIMEOUT_MS // 1000} s; exiting.", file=sys.stderr)
            hard_exit(0, self._settings)


# ---------------------------------------------------------------- helpers
def _legal_file(name: str) -> Path | None:
    """LICENSE and LICENSES.txt sit next to the exe when packaged, in the project root from source."""
    names = ("LICENSE", "LICENSE.txt", "COPYING") if name == "LICENSE" else (name,)
    for candidate in names:
        path = paths.app_dir() / candidate
        if path.is_file():
            return path
    return None


def _source_location() -> tuple[str, Path | None]:
    """HTML saying where this version's source code is, and the folder to open for it."""
    if getattr(sys, "frozen", False):
        folder = paths.app_dir() / "source"
        zips = sorted(folder.glob("*.zip")) if folder.is_dir() else []
        if zips:
            names = ", ".join(f"<i>{html.escape(z.name)}</i>" for z in zips)
            return (
                f"The complete source code of this version is included with it: {names} in "
                f"{_file_link(folder)}. The source code of the GPL and LGPL libraries it contains is "
                "available as described in LICENSES.txt.",
                folder,
            )
        return (
            f"The source code of this version belongs in {html.escape(str(folder))}, but that folder is "
            "missing or empty. LICENSES.txt says how else to get it.",
            folder if folder.is_dir() else None,
        )
    return (f"Running from the source code in {_file_link(paths.app_dir())}.", paths.app_dir())


def _file_link(path: Path) -> str:
    return f"<a href='{QUrl.fromLocalFile(str(path)).toString()}'>{html.escape(str(path))}</a>"


def _about_html() -> str:
    if paths.is_portable():
        data = (
            "<b>Portable mode.</b> Models, settings and logs are kept next to the program, so the whole "
            "folder can be copied to another PC, also one without internet.<br>"
            f"Models: {_file_link(paths.model_dir())}<br>"
            f"Settings and logs: {_file_link(paths.data_dir())}"
        )
    else:
        data = (
            "<b>Installed mode.</b> Models and logs are kept in your user profile, settings in the "
            "Windows registry.<br>"
            f"Models: {_file_link(paths.model_dir())}<br>"
            f"Logs: {_file_link(paths.data_dir())}"
        )
    return (
        "<p>Removes the background from photos, with extra care for hair, hats and fine edges. "
        "Everything runs on this PC; photos are never uploaded.</p>"
        "<p>This program is free software: you can redistribute it and/or modify it under the terms "
        "of the GNU General Public License as published by the Free Software Foundation, either "
        "version 3 of the License, or (at your option) any later version.</p>"
        "<p>This program is distributed in the hope that it will be useful, but <b>WITHOUT ANY "
        "WARRANTY</b>; without even the implied warranty of MERCHANTABILITY or FITNESS FOR A "
        "PARTICULAR PURPOSE. See the GNU General Public License for more details.</p>"
        "<p>You should have received a copy of the GNU General Public License along with this "
        "program: click <i>Licence (GPL v3)…</i> below. If not, see "
        "<a href='https://www.gnu.org/licenses/'>https://www.gnu.org/licenses/</a>.</p>"
        f"<h4>Source code</h4><p>{_source_location()[0]}</p>"
        "<h4>Updates</h4><p>Background Editor looks for a new version on "
        f"<a href='{app_updates.RELEASES_URL}'>its GitHub releases page</a> at most once a day "
        "(Advanced settings › Updates) and whenever you click <i>Check for updates</i>. A new version "
        "is installed only when you click <i>Update now</i>, and only when every file matches the "
        "release manifest signed with the Optimey code signing certificate.</p>"
        f"<h4>Where your data is kept</h4><p>{data}</p>"
    )


def _third_party_html() -> str:
    return (
        "<p>This program uses Qt 6 (Copyright (C) The Qt Company Ltd. and other contributors) under "
        "the GNU LGPL v3. The texts of the GNU LGPL v3 and the GNU GPL v3 are in LICENSES.txt "
        "(<i>Third-party licences…</i> below). The Qt libraries are separate files in the program "
        "folder and can be replaced with other builds of the same Qt version.</p>"
        "<p><b>Also included</b></p><ul>"
        "<li>PyQt6 — Riverbank Computing Limited, GNU GPL v3</li>"
        "<li>ONNX Runtime — Microsoft Corporation, MIT licence</li>"
        "<li>NumPy and SciPy — BSD licence</li>"
        "<li>Pillow — MIT-CMU licence</li>"
        "<li>pillow-heif (BSD licence) with libheif and libde265 (GNU LGPL v3) and x265 "
        "(GNU GPL v2 or later)</li>"
        "<li>Python — Python Software Foundation License</li>"
        "</ul>"
        "<p>On the GPU the models run through DirectML, the machine-learning component of Windows, "
        "when this PC has it; it is part of Windows, not of this program.</p>"
        "<p>The copyright notices and licences of every part, and how to get the source code of the "
        "GPL and LGPL parts, are in LICENSES.txt and in the <i>licenses</i> folder next to the "
        "program.</p>"
    )


def _models_html() -> str:
    rows = []
    for spec in list(models.MODELS.values()):
        licence = (spec.licence or "").strip()
        note = (spec.licence_note or "").strip()
        if note and (not licence or licence.lower() in note.lower()):
            terms = note  # the note already names the licence
        else:
            terms = ". ".join(t for t in (licence or "Licence unknown", note) if t)
        state = "on this PC" if models.find_model(spec) is not None else "not downloaded"
        extra = ""
        if spec.discovered:
            # Adopted automatically: show what it was checked against.
            evidence = (models.catalog_entry(spec.key) or {}).get("evidence") or {}
            facts = [
                ("Source", evidence.get("repository") or evidence.get("url")),
                ("Revision", evidence.get("revision")),
                ("SHA-256", evidence.get("sha256")),
                ("Licence source", evidence.get("licence_source")),
                ("Check", _check_summary(evidence)),
            ]
            items = "".join(f"<br>{html.escape(k)}: {html.escape(str(v))}" for k, v in facts if v)
            extra = f"<span style='font-size:small'>{items}</span>"
        rows.append(f"<li><b>{html.escape(spec.title)}</b> — {html.escape(terms)} <i>({state})</i>{extra}</li>")
    return (
        "<p>The AI models are downloaded when first needed; they are not part of the program.</p>"
        "<ul>"
        "<li>BiRefNet — Zheng Peng et al., <a href='https://github.com/ZhengPeng7/BiRefNet'>"
        "github.com/ZhengPeng7/BiRefNet</a>, MIT licence</li>"
        "<li>ViTMatte — Jingfeng Yao et al., Hust Vision Lab, <a href='https://github.com/hustvl/ViTMatte'>"
        "github.com/hustvl/ViTMatte</a>, MIT licence</li>"
        "<li>BiRefNet matting is downloaded from the BiRefNet release; the other ONNX files come "
        "from the model release of the rembg project, <a href='https://github.com/danielgatis/rembg'>"
        "github.com/danielgatis/rembg</a></li>"
        "</ul>"
        "<p><b>Please note:</b> the BiRefNet and ViTMatte weights are MIT licensed, but they were "
        "trained on datasets whose terms allow research use only. Check whether that matters for "
        "how you use the results, especially commercially.</p>"
        f"<p><b>Models known to this copy</b></p><ul>{''.join(rows)}</ul>"
        f"<p>Models folder: {_file_link(paths.model_dir())}</p>"
    )


def _check_summary(evidence: dict) -> str:
    parts = []
    if evidence.get("iou") is not None:
        parts.append(f"IoU {float(evidence['iou']):.3f} against the reference")
    if evidence.get("seconds") is not None:
        parts.append(f"{float(evidence['seconds']):.1f} s on the test portrait")
    return ", ".join(parts)


def _rich_text_view(content: str) -> QTextBrowser:
    view = QTextBrowser()
    view.setOpenLinks(False)  # web links in the browser, folders in Explorer
    view.anchorClicked.connect(QDesktopServices.openUrl)
    view.setHtml(content)
    return view


def _show_text_file(parent: QWidget, path: Path | None, title: str) -> None:
    """Show a licence text inside the app: LICENSE has no extension Windows could open it with."""
    if path is None:
        QMessageBox.warning(
            parent,
            title,
            "The file is missing next to the program.\n\n"
            "The GNU General Public License is also at https://www.gnu.org/licenses/gpl-3.0.html",
        )
        return
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        QMessageBox.warning(parent, title, f"{path} cannot be read:\n{exc.strerror or exc}")
        return
    dlg = QDialog(parent)
    dlg.setWindowTitle(f"{title} — {path.name}")
    dlg.resize(760, 640)
    v = QVBoxLayout(dlg)
    edit = QPlainTextEdit()
    edit.setReadOnly(True)
    edit.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
    edit.setFont(QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont))
    edit.setPlainText(text)
    v.addWidget(edit, 1)
    bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
    reveal = bb.addButton("Show in folder", QDialogButtonBox.ButtonRole.ActionRole)
    reveal.clicked.connect(lambda: _reveal(path))
    bb.rejected.connect(dlg.reject)
    v.addWidget(bb)
    dlg.exec()


def _plain_text(box: QMessageBox) -> QMessageBox:
    """Message boxes show text that may come from GitHub (tags, messages): never as rich text."""
    box.setTextFormat(Qt.TextFormat.PlainText)
    for label in box.findChildren(QLabel):
        label.setTextFormat(Qt.TextFormat.PlainText)
    return box


def _message_box(parent: QWidget, icon, title: str, text: str, details: str = "", info: str = "") -> None:
    box = QMessageBox(icon, title, text, QMessageBox.StandardButton.Ok, parent)
    if info:
        box.setInformativeText(info)
    if details:
        box.setDetailedText(details)
    _plain_text(box).exec()


def _progress_text(text: str, done: int, total: int) -> str:
    """Progress of an update step. Downloads report bytes, other steps count items."""
    if total <= 1:
        return text + "…"
    if total >= 1_000_000:
        return f"{text} — {done / 1e6:.0f} of {total / 1e6:.0f} MB"
    return f"{text} — {done} of {total}"


def _image_look(o: Options) -> tuple:
    """Everything that changes how the background image looks behind the person."""
    pad = o.background_color if o.background_fit == "contain" else ""
    return (o.background, o.background_image, o.background_fit, o.background_blur, pad)


def hard_exit(code: int, settings: QSettings | None = None) -> None:
    """End the process at once, after saving what must survive."""
    if settings is not None:
        settings.sync()
    for stream in (sys.stdout, sys.stderr):
        try:
            if stream is not None:
                stream.flush()
        except (OSError, ValueError):
            pass
    os._exit(code)


def _hspace(px: int) -> QWidget:
    w = QWidget()
    w.setFixedWidth(px)
    return w


def _bold(label: QLabel) -> None:
    f = label.font()
    f.setWeight(f.Weight.DemiBold)
    label.setFont(f)


def _backdrop_icon(key: str, output_color: str, border: QColor) -> QIcon:
    def paint(p: QPainter, size: int) -> None:
        p.setPen(border)
        if key == "image":
            paint_picture(p, QRectF(1, 1, size - 2, size - 2), border)
        elif key == "checker":
            p.setBrush(QColor("#FFFFFF"))
            p.drawRoundedRect(1, 1, size - 2, size - 2, 3, 3)
            c = (size - 2) // 2
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor("#C8C8C8"))
            p.drawRect(1, 1, c, c)
            p.drawRect(1 + c, 1 + c, c, c)
            p.setPen(border)
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawRoundedRect(1, 1, size - 2, size - 2, 3, 3)
        else:
            color = output_color if key == "output" else key
            p.setBrush(QColor(color))
            p.drawRoundedRect(1, 1, size - 2, size - 2, 3, 3)
            if key == "output":
                p.setPen(QColor(0, 0, 0, 160) if QColor(color).lightness() > 128 else QColor(255, 255, 255, 200))
                f = p.font()
                f.setPixelSize(10)
                f.setBold(True)
                p.setFont(f)
                p.drawText(0, 0, size, size, Qt.AlignmentFlag.AlignCenter, "✓")

    return painted_icon(18, paint)


def _reveal(path: Path) -> None:
    """Open Explorer with the file selected."""
    if path.exists():
        subprocess.Popen(["explorer", "/select,", str(path)])
    elif path.parent.exists():
        os.startfile(str(path.parent))  # noqa: S606 (opening a folder the user chose)

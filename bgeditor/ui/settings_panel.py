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

"""Right-hand settings panel: a short simple view plus an optional advanced section."""

from __future__ import annotations

import json
import os
from dataclasses import replace
from datetime import datetime
from pathlib import Path

from PyQt6.QtCore import QEvent, QObject, QPoint, QRectF, QRegularExpression, QSettings, QSize, Qt, QThreadPool, pyqtSignal
from PyQt6.QtGui import QColor, QFont, QIcon, QPainter, QPalette, QPixmap, QRegularExpressionValidator
from PyQt6.QtWidgets import (
    QAbstractSpinBox,
    QButtonGroup,
    QCheckBox,
    QColorDialog,
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMenu,
    QPushButton,
    QRadioButton,
    QScrollArea,
    QSpinBox,
    QToolButton,
    QToolTip,
    QVBoxLayout,
    QWidget,
)

from .. import app_updates, imageio, models, paths, updates
from ..imageio import INPUT_EXTENSIONS
from ..options import PRESET_TITLES, REFINE_RESOLUTIONS, Options, clean_suffix, matching_preset, presets
from .resources import paint_picture, painted_icon, swatch_border
from .workers import BackgroundThumbLoader, LoaderSignals

SWATCHES = [
    ("#FFFFFF", "White"),
    ("#F2F2F2", "Light grey"),
    ("#808080", "Mid grey"),
    ("#000000", "Black"),
    ("#DCE8F5", "Pale blue"),
    ("#2F5D8C", "Studio blue"),
]

# Background images the user picked before, most recent first (JSON list in the settings).
RECENT_BACKGROUNDS_KEY = "recent_backgrounds"
LAST_BACKGROUND_DIR_KEY = "last_background_dir"
MAX_RECENT_BACKGROUNDS = 8
RECENT_COLUMNS = 4
BG_THUMB = QSize(56, 40)

FIT_CHOICES = (
    ("cover", "Fill", "Covers the whole photo; the edges of the background image may be cut off."),
    ("contain", "Fit", "Shows the whole background image; the space around it gets the colour chosen below."),
    ("stretch", "Stretch", "Stretches the background image to the photo's shape; it may look distorted."),
)

UPDATE_MODES = (
    (
        "auto",
        "Automatic (recommended)",
        "Checks weekly for a newer person model. Switches to it only when every safety check passes;\n"
        "otherwise the status bar says why it was not used.",
    ),
    ("ask", "Ask first", "Checks weekly and asks before downloading a newer model."),
    ("off", "Off", "Never checks by itself. Check now and Check for updates still look."),
)

MODEL_HINTS = {
    "birefnet-matting": "Trained on soft hair and fur. Best overall in our portrait tests: keeps hats, headscarves and loose strands.",
    "birefnet-portrait": "Trained on portraits. Cleanest gaps (e.g. between arm and body), but can drop blurry loose hair.",
    "birefnet-general": "Trained on all kinds of subjects. Try it for people holding objects.",
    "birefnet-lite": "Small and fast general model; less precise edges.",
}

DEVICE_TIPS = {
    "auto": "Uses the GPU only where it is known to work (the Fast model on a dedicated graphics card), "
    "otherwise the CPU.",
    "gpu": "Runs the person model on the GPU even where it may fail. There is no fallback to the CPU.",
    "cpu": "Runs everything on the processor: slower, but works on every PC.",
}

# Characters Windows does not allow in file names (the same set options.clean_suffix removes).
BAD_NAME_CHARS_TEXT = '\\ / : * ? " < > |'
SUFFIX_PATTERN = r'[^<>:"/\\|?*\x00-\x1f]*'

# Advanced settings that change the result, reset by the note in the simple view.
ADVANCED_RESULT_FIELDS = (
    "refine_band",
    "refine_resolution",
    "extra_strand_pass",
    "fg_threshold",
    "bg_threshold",
    "edge_shift",
    "edge_soften",
    "main_subject_only",
    "fill_holes",
    "crop_to_subject",
    "crop_margin",
    "device",
)


def advanced_changes(o: Options) -> list[str]:
    """Short names of advanced settings that differ from the defaults and affect the result."""
    d = Options()
    found: list[str] = []
    if o.refine_hair:
        if o.refine_band != d.refine_band:
            found.append("edge zone")
        if o.refine_resolution != d.refine_resolution:
            found.append("refinement detail")
        if (o.fg_threshold, o.bg_threshold) != (d.fg_threshold, d.bg_threshold):
            found.append("certainty levels")
        if o.extra_strand_pass:
            found.append("extra strand pass")
    if o.edge_shift != d.edge_shift:
        found.append("edge shift")
    if o.edge_soften != d.edge_soften:
        found.append("edge softening")
    if o.main_subject_only != d.main_subject_only:
        found.append("everyone kept" if not o.main_subject_only else "main person only")
    if o.fill_holes != d.fill_holes:
        found.append("hole filling" if o.fill_holes else "no hole filling")
    if o.crop_to_subject != d.crop_to_subject:
        found.append("crop" if o.crop_to_subject else "no crop")
    if o.device == "gpu":
        found.append("GPU forced")
    return found


def device_hint(o: Options) -> tuple[str, bool]:
    """Hint under Processor for these options, and whether it is a warning."""
    if o.device == "cpu":
        return DEVICE_TIPS["cpu"], False
    if o.device == "gpu":
        if o.model != "birefnet-lite":
            return (
                "Forcing the GPU with the matting, portrait or general model often fails on graphics cards "
                "with 4 GB or less, and then every photo fails. Automatic is safer. "
                "Hair refinement always runs on the CPU.",
                True,
            )
        return "The Fast (lite) model runs on the GPU. If photos fail, choose Automatic. Hair refinement always runs on the CPU.", False
    return (
        "Automatic uses the GPU only for the Fast (lite) model on a dedicated graphics card, "
        "where it is known to work; everything else runs on the CPU.",
        False,
    )


def model_title(key: str) -> str:
    spec = models.MODELS.get(key)
    return spec.title if spec is not None else key


def model_hint(key: str) -> str:
    if key in MODEL_HINTS:
        return MODEL_HINTS[key]
    spec = models.MODELS.get(key)
    if spec is None:
        return ""
    text = "A newer person model, found by the update check and taken into use after its safety checks."
    note = (spec.licence_note or "").strip()
    return f"{text} {note}" if note else text


def quality_hint(preset: str | None) -> str:
    """Hint under Quality; Best and Balanced name the model they use now."""
    if preset is None:
        return "Custom settings from the advanced section."
    if preset == "fast":
        return "Small general model; uses a dedicated GPU when available. Quick previews and large batches; coarser hair."
    title = model_title(presets().get(preset, {}).get("model", ""))
    if preset == "best":
        return f"{title} plus a refinement pass that rebuilds single hair strands. About a minute per photo on a laptop CPU."
    return f"{title} only: soft hair edges, fewer single strands. About half the time of Best."


def format_size(num_bytes: int) -> str:
    return f"{num_bytes / 1e9:.2f} GB" if num_bytes >= 1e9 else f"{num_bytes / 1e6:.0f} MB"


def format_check_time(value: str) -> str:
    """ISO time of the last update check as local time, or the text itself if it is not ISO."""
    if not value:
        return "never"
    try:
        stamp = datetime.fromisoformat(value)
    except ValueError:
        return value
    if stamp.tzinfo is not None:
        stamp = stamp.astimezone()
    return stamp.strftime("%d %b %Y, %H:%M")


def update_status_text(status: dict) -> str:
    """One status line for Model updates: current model, last check, last result."""
    preferred = str(status.get("preferred") or models.preferred_segmenter())
    parts = [f"Best and Balanced use: {model_title(preferred)}.", f"Last check: {format_check_time(str(status.get('last_check') or ''))}."]
    message = str(status.get("message") or "").strip()
    if message:
        parts.append(message if message.endswith((".", "!", "?")) else message + ".")
    return " ".join(parts)


def app_update_status_text(status: dict, version: str) -> str:
    """One status line for app updates: this version, last check, its outcome."""
    parts = [f"This is version {version}.", f"Last check: {format_check_time(str(status.get('last_check') or ''))}."]
    message = str(status.get("message") or "").strip()
    if message:
        parts.append(message if message.endswith((".", "!", "?")) else message + ".")
    skipped = str(status.get("skipped_version") or "")
    if skipped and skipped not in message:
        parts.append(f"Version {skipped} is skipped at startup.")
    return " ".join(parts)


class WheelGuard(QObject):
    """Spin boxes and combo boxes ignore the mouse wheel unless they have keyboard focus.

    Scrolling the panel over 'CPU threads' used to change it from Automatic to 1 unnoticed.
    The ignored wheel event travels on to the parents, so the scroll area scrolls instead.
    """

    def eventFilter(self, obj, event) -> bool:  # noqa: N802
        if event.type() == QEvent.Type.Wheel and isinstance(obj, QWidget) and not obj.hasFocus():
            event.ignore()
            return True
        return super().eventFilter(obj, event)


def guard_wheel(root: QWidget, guard: QObject) -> int:
    """Protect every spin box and combo box under root; returns how many were found."""
    widgets = root.findChildren(QAbstractSpinBox) + root.findChildren(QComboBox)
    for w in widgets:
        w.setFocusPolicy(Qt.FocusPolicy.StrongFocus)  # no focus from the wheel either
        w.installEventFilter(guard)
    return len(widgets)


def _norm(path: str) -> str:
    """One spelling per background file, so the same image is listed once. A relative path
    means a file inside the data folder (see imageio.resolve_background)."""
    try:
        full = str(imageio.resolve_background(path))
    except (ValueError, OSError):
        full = path
    return os.path.normcase(os.path.normpath(full))


def _full_path(path: str) -> str:
    try:
        return str(imageio.resolve_background(path))
    except (ValueError, OSError):
        return path


def stored_background_path(path: str) -> str:
    """How a chosen background is saved: relative when it lies inside the data folder, so a
    portable copy keeps its backgrounds when the folder is moved to another PC."""
    try:
        return str(Path(path).resolve().relative_to(paths.data_dir().resolve()))
    except (ValueError, OSError):
        return path


def _swatch_icon(color: str, size: int, border: QColor) -> QIcon:
    def paint(p: QPainter, s: int) -> None:
        p.setPen(border)
        p.setBrush(QColor(color))
        p.drawRoundedRect(1, 1, s - 2, s - 2, 4, 4)

    return painted_icon(size, paint)


class Section(QWidget):
    """A titled block with a form layout, used instead of heavy group boxes."""

    def __init__(self, title: str, subtitle: str = "", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(6)
        head = QLabel(title)
        f = QFont(head.font())
        f.setPointSizeF(f.pointSizeF() * 1.1)
        f.setWeight(QFont.Weight.DemiBold)
        head.setFont(f)
        outer.addWidget(head)
        if subtitle:
            outer.addWidget(_hint(subtitle))
        self.form = QFormLayout()
        self.form.setContentsMargins(0, 2, 0, 0)
        self.form.setHorizontalSpacing(10)
        self.form.setVerticalSpacing(8)
        self.form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        self.form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.DontWrapRows)
        outer.addLayout(self.form)


def _hint(text: str) -> QLabel:
    lbl = QLabel(text)
    lbl.setWordWrap(True)
    lbl.setForegroundRole(QPalette.ColorRole.PlaceholderText)
    return lbl


def _separator() -> QFrame:
    line = QFrame()
    line.setFrameShape(QFrame.Shape.HLine)
    line.setFrameShadow(QFrame.Shadow.Sunken)
    return line


def _left(widget: QWidget) -> QHBoxLayout:
    row = QHBoxLayout()
    row.setContentsMargins(0, 0, 0, 0)
    row.addWidget(widget)
    row.addStretch(1)
    return row


class SettingsPanel(QScrollArea):
    optionsChanged = pyqtSignal(object)  # Options
    advancedToggled = pyqtSignal(bool)
    checkUpdatesRequested = pyqtSignal()
    downloadAllRequested = pyqtSignal(bool)  # include all models
    switchModelRequested = pyqtSignal(str)  # model key for Best and Balanced

    def __init__(
        self,
        options: Options,
        advanced: bool,
        parent: QWidget | None = None,
        settings: QSettings | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWidgetResizable(True)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setMinimumWidth(330)
        self._settings = settings if settings is not None else paths.settings()
        self._opts = replace(options)
        self._loading = False
        # Background image thumbnails: loaded off the GUI thread, kept per file.
        self._recent_bgs = self._load_recent_backgrounds()
        self._bg_thumbs: dict[str, QPixmap | None] = {}
        self._bg_errors: dict[str, str] = {}
        self._bg_pending: set[str] = set()
        self._bg_pool = QThreadPool(self)
        self._bg_pool.setMaxThreadCount(2)
        self._bg_signals = LoaderSignals()
        self._bg_signals.background_thumb.connect(self._on_background_thumb)

        body = QWidget()
        self.setWidget(body)
        col = QVBoxLayout(body)
        col.setContentsMargins(16, 12, 16, 16)
        col.setSpacing(16)

        col.addWidget(self._build_output())
        col.addWidget(self._build_quality())
        col.addWidget(self._build_destination())
        col.addWidget(_separator())

        self.chk_advanced = QCheckBox("Show advanced settings")
        self.chk_advanced.setChecked(advanced)
        self.chk_advanced.toggled.connect(self._on_advanced_toggled)
        col.addWidget(self.chk_advanced)

        self.advanced = QWidget()
        adv = QVBoxLayout(self.advanced)
        adv.setContentsMargins(0, 0, 0, 0)
        adv.setSpacing(18)
        adv.addWidget(self._build_model())
        adv.addWidget(self._build_edges())
        adv.addWidget(self._build_mask())
        adv.addWidget(self._build_framing())
        adv.addWidget(self._build_files())
        adv.addWidget(self._build_updates())
        adv.addWidget(self._build_offline())
        btn_reset = QPushButton("Reset all settings")
        btn_reset.setIcon(QIcon.fromTheme(QIcon.ThemeIcon.ViewRefresh))
        btn_reset.clicked.connect(self._reset)
        adv.addWidget(btn_reset, 0, Qt.AlignmentFlag.AlignLeft)
        col.addWidget(self.advanced)
        col.addStretch(1)

        self.advanced.setVisible(advanced)
        if self._opts.background_image and _norm(self._opts.background_image) not in {_norm(p) for p in self._recent_bgs}:
            self._add_recent(self._opts.background_image)
        self.set_options(self._opts)
        self._rebuild_recent_backgrounds()
        self.refresh_offline_info()
        self.show_update_status()
        self.show_app_update_status()
        self._wheel_guard = WheelGuard(self)
        guard_wheel(body, self._wheel_guard)

    # ------------------------------------------------------------ sections
    def _build_output(self) -> QWidget:
        sec = Section("Background")
        self._bg_form = sec.form
        self.rb_transparent = QRadioButton("Transparent")
        self.rb_color = QRadioButton("Solid colour")
        self.rb_image = QRadioButton("Image")
        self.rb_image.setToolTip("Put a picture of your own behind the person.")
        self._bg_group = QButtonGroup(self)
        for rb in (self.rb_transparent, self.rb_color, self.rb_image):
            self._bg_group.addButton(rb)
        row = QHBoxLayout()
        row.setSpacing(16)
        row.addWidget(self.rb_transparent)
        row.addWidget(self.rb_color)
        row.addWidget(self.rb_image)
        row.addStretch(1)
        sec.form.addRow(row)
        # Switching between two buttons toggles both; react once, to the one switched on.
        self._bg_group.buttonToggled.connect(lambda _b, on: on and self._on_background_mode())

        # --- image: chooser, recent images, fit and blur
        self.image_box = QWidget()
        iv = QVBoxLayout(self.image_box)
        iv.setContentsMargins(0, 0, 0, 0)
        iv.setSpacing(6)
        top = QHBoxLayout()
        top.setSpacing(8)
        self.btn_choose_bg = QPushButton("Choose image…")
        self.btn_choose_bg.setIcon(QIcon.fromTheme(QIcon.ThemeIcon.FolderOpen))
        self.btn_choose_bg.setToolTip("Pick a picture to put behind the person (JPEG, PNG, WebP, HEIC, …).")
        self.btn_choose_bg.clicked.connect(self._choose_background)
        top.addWidget(self.btn_choose_bg)
        self.lbl_bg_name = QLabel("")
        self.lbl_bg_name.setMinimumWidth(40)
        top.addWidget(self.lbl_bg_name, 1)
        iv.addLayout(top)
        self.recent_box = QWidget()
        self.recent_grid = QGridLayout(self.recent_box)
        self.recent_grid.setContentsMargins(0, 0, 0, 0)
        self.recent_grid.setHorizontalSpacing(4)
        self.recent_grid.setVerticalSpacing(4)
        self.recent_grid.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        iv.addWidget(self.recent_box)
        self.lbl_recent_hint = _hint("Recent images: click one to use it again, right-click to remove it from this list.")
        iv.addWidget(self.lbl_recent_hint)
        self.lbl_bg_problem = QLabel("")
        self.lbl_bg_problem.setWordWrap(True)
        self.lbl_bg_problem.setVisible(False)
        iv.addWidget(self.lbl_bg_problem)
        self._recent_buttons: list[tuple[QToolButton, str]] = []
        sec.form.addRow(self.image_box)

        self.cmb_fit = QComboBox()
        for key, title, tip in FIT_CHOICES:
            self.cmb_fit.addItem(title, key)
            self.cmb_fit.setItemData(self.cmb_fit.count() - 1, tip, Qt.ItemDataRole.ToolTipRole)
        self.cmb_fit.setToolTip("\n".join(f"{title}: {tip}" for _key, title, tip in FIT_CHOICES))
        self.cmb_fit.currentIndexChanged.connect(self._changed)
        sec.form.addRow("Fit", self.cmb_fit)

        self.sp_blur = QDoubleSpinBox()
        self.sp_blur.setRange(0.0, 50.0)
        self.sp_blur.setDecimals(0)
        self.sp_blur.setSingleStep(1.0)
        self.sp_blur.setSuffix(" px")
        self.sp_blur.setSpecialValueText("Off")
        self.sp_blur.setToolTip(
            "Blurs the background image, like a camera focused on the person.\n"
            "Measured in pixels of the result; 0 keeps it sharp."
        )
        self.sp_blur.valueChanged.connect(self._changed)
        sec.form.addRow("Blur", self.sp_blur)

        self.lbl_pad = _hint("Colour around the image (with Fit):")
        sec.form.addRow(self.lbl_pad)

        # --- solid colour (also the padding colour for Fit)
        self.color_row = QWidget()
        crow = QHBoxLayout(self.color_row)
        crow.setContentsMargins(0, 0, 0, 0)
        crow.setSpacing(4)
        self._swatch_buttons: list[tuple[QToolButton, str]] = []
        for hex_color, name in SWATCHES:
            b = QToolButton()
            b.setIconSize(QSize(20, 20))
            b.setToolTip(f"{name} ({hex_color})")
            b.setAccessibleName(f"{name} background")
            b.setAutoRaise(True)
            b.clicked.connect(lambda _=False, c=hex_color: self._set_color(c))
            crow.addWidget(b)
            self._swatch_buttons.append((b, hex_color))
        self.btn_custom_color = QToolButton()
        self.btn_custom_color.setText("Custom…")
        self.btn_custom_color.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.btn_custom_color.setToolTip("Pick any colour")
        self.btn_custom_color.clicked.connect(self._pick_color)
        crow.addWidget(self.btn_custom_color)
        crow.addStretch(1)
        sec.form.addRow(self.color_row)
        self._refresh_swatches()

        self.cmb_format = QComboBox()
        self.cmb_format.addItem("PNG", "png")
        self.cmb_format.addItem("WebP (lossless)", "webp")
        self.cmb_format.addItem("TIFF", "tif")
        self.cmb_format.addItem("JPEG", "jpg")
        self.cmb_format.setToolTip(
            "JPEG cannot store transparency, so it is only offered with a solid colour or an image."
        )
        self.cmb_format.currentIndexChanged.connect(self._changed)
        sec.form.addRow("File type", self.cmb_format)
        return sec

    def _build_quality(self) -> QWidget:
        sec = Section("Quality")
        self.cmb_quality = QComboBox()
        for key, title in PRESET_TITLES.items():
            self.cmb_quality.addItem(title, key)
        self.cmb_quality.currentIndexChanged.connect(self._on_quality)
        sec.form.addRow(self.cmb_quality)
        self.lbl_quality_hint = _hint("")
        sec.form.addRow(self.lbl_quality_hint)
        # Shown in the simple view when hidden advanced settings still change the result.
        self.lbl_advanced_note = QLabel("")
        self.lbl_advanced_note.setWordWrap(True)
        self.lbl_advanced_note.setTextFormat(Qt.TextFormat.RichText)
        self.lbl_advanced_note.setTextInteractionFlags(Qt.TextInteractionFlag.LinksAccessibleByMouse | Qt.TextInteractionFlag.LinksAccessibleByKeyboard)
        self.lbl_advanced_note.linkActivated.connect(self._on_advanced_note_link)
        self.lbl_advanced_note.setVisible(False)
        sec.form.addRow(self.lbl_advanced_note)
        return sec

    def _build_destination(self) -> QWidget:
        sec = Section("Save to")
        self.rb_same = QRadioButton("Next to each photo")
        self.rb_folder = QRadioButton("This folder:")
        self._dest_group = QButtonGroup(self)
        self._dest_group.addButton(self.rb_same)
        self._dest_group.addButton(self.rb_folder)
        sec.form.addRow(self.rb_same)
        sec.form.addRow(self.rb_folder)
        frow = QHBoxLayout()
        frow.setSpacing(6)
        self.ed_folder = QLineEdit()
        self.ed_folder.setPlaceholderText("Choose a folder…")
        self.ed_folder.editingFinished.connect(self._changed)
        btn = QToolButton()
        btn.setIcon(QIcon.fromTheme(QIcon.ThemeIcon.FolderOpen))
        btn.setToolTip("Browse")
        btn.clicked.connect(self._browse_folder)
        frow.addWidget(self.ed_folder, 1)
        frow.addWidget(btn)
        sec.form.addRow(frow)
        self.rb_same.toggled.connect(self._changed)
        return sec

    def _build_model(self) -> QWidget:
        sec = Section("Model and processor")
        self.cmb_model = QComboBox()
        self._fill_model_combo()
        self.cmb_model.currentIndexChanged.connect(self._changed)
        sec.form.addRow("Model", self.cmb_model)
        self.lbl_model_hint = _hint("")
        sec.form.addRow(self.lbl_model_hint)

        self.cmb_device = QComboBox()
        self.cmb_device.addItem("Automatic (recommended)", "auto")
        self.cmb_device.addItem("GPU (DirectML)", "gpu")
        self.cmb_device.addItem("CPU only", "cpu")
        for i in range(self.cmb_device.count()):
            self.cmb_device.setItemData(i, DEVICE_TIPS[self.cmb_device.itemData(i)], Qt.ItemDataRole.ToolTipRole)
        self.cmb_device.setToolTip(
            "Automatic uses the GPU only where it is known to work.\n"
            "Forcing the GPU can fail on graphics cards with little memory."
        )
        self.cmb_device.currentIndexChanged.connect(self._changed)
        sec.form.addRow("Processor", self.cmb_device)
        self.lbl_device_hint = _hint("")
        sec.form.addRow(self.lbl_device_hint)

        self.sp_threads = QSpinBox()
        self.sp_threads.setRange(0, 64)
        self.sp_threads.setSpecialValueText("Automatic")
        self.sp_threads.setToolTip(
            "CPU threads for the models that run on the processor, including the hair refiner,\n"
            "which always runs on the CPU. Automatic lets ONNX Runtime choose."
        )
        self.sp_threads.valueChanged.connect(self._changed)
        sec.form.addRow("CPU threads", self.sp_threads)
        return sec

    def _fill_model_combo(self) -> None:
        """(Re)fill the model list from the registry; an adopted newer model appears here too."""
        self.cmb_model.clear()
        for key in models.model_keys():
            spec = models.MODELS.get(key)
            if spec is None:
                continue
            self.cmb_model.addItem(spec.title, key)
            hint = model_hint(key)
            if hint:
                self.cmb_model.setItemData(self.cmb_model.count() - 1, hint, Qt.ItemDataRole.ToolTipRole)

    def _build_edges(self) -> QWidget:
        sec = Section("Hair and edges")
        self.chk_refine = QCheckBox("Refine hair and soft edges")
        self.chk_refine.setToolTip("Runs a matting network over the edge of the person to recover loose strands.")
        self.chk_refine.toggled.connect(self._changed)
        sec.form.addRow(self.chk_refine)

        self.sp_band = QDoubleSpinBox()
        self.sp_band.setRange(0.2, 8.0)
        self.sp_band.setSingleStep(0.1)
        self.sp_band.setDecimals(1)
        self.sp_band.setSuffix(" %")
        self.sp_band.setToolTip(
            "How far around the edge the refinement may look, as a share of the image size.\n"
            "Wider catches more flyaway hair but can pull in background detail."
        )
        self.sp_band.valueChanged.connect(self._changed)
        sec.form.addRow("Edge zone width", self.sp_band)

        self.cmb_refine_res = QComboBox()
        for px in REFINE_RESOLUTIONS:
            self.cmb_refine_res.addItem(f"{px} px", px)
        self.cmb_refine_res.setToolTip("Higher keeps finer strands on large photos but takes longer.")
        self.cmb_refine_res.currentIndexChanged.connect(self._changed)
        sec.form.addRow("Refinement detail", self.cmb_refine_res)

        self.chk_extra = QCheckBox("Extra pass for long loose strands")
        self.chk_extra.setToolTip(
            "A second, wider refinement pass that finds long flyaway strands (for example against the sky).\n"
            "Only strands that connect to the person are kept. Adds about half a minute per photo."
        )
        self.chk_extra.toggled.connect(self._changed)
        sec.form.addRow(self.chk_extra)

        # The certainty levels only shape the zone that hair refinement works on.
        self.sp_fg = QSpinBox()
        self.sp_fg.setRange(128, 255)
        self.sp_fg.setToolTip(
            "Used by hair refinement: mask values at or above this count as certainly the person.\n"
            "Only the zone between the two levels is refined."
        )
        self.sp_fg.valueChanged.connect(self._changed)
        sec.form.addRow("Certain person above", self.sp_fg)
        self.sp_bg = QSpinBox()
        self.sp_bg.setRange(0, 127)
        self.sp_bg.setToolTip(
            "Used by hair refinement: mask values at or below this count as certainly background.\n"
            "Only the zone between the two levels is refined."
        )
        self.sp_bg.valueChanged.connect(self._changed)
        sec.form.addRow("Certain background below", self.sp_bg)

        self.chk_decontaminate = QCheckBox("Remove background colour from edges")
        self.chk_decontaminate.setToolTip("Stops a coloured halo of the old background showing in the hair.")
        self.chk_decontaminate.toggled.connect(self._changed)
        sec.form.addRow(self.chk_decontaminate)

        self.sp_shift = QSpinBox()
        self.sp_shift.setRange(-30, 30)
        self.sp_shift.setSuffix(" px")
        self.sp_shift.setToolTip("Negative values pull the edge inwards, positive values push it outwards.")
        self.sp_shift.valueChanged.connect(self._changed)
        sec.form.addRow("Shift edge", self.sp_shift)

        self.sp_soften = QDoubleSpinBox()
        self.sp_soften.setRange(0.0, 20.0)
        self.sp_soften.setSingleStep(0.5)
        self.sp_soften.setDecimals(1)
        self.sp_soften.setSuffix(" px")
        self.sp_soften.setSpecialValueText("Off")
        self.sp_soften.setToolTip("Extra softening of the outline.")
        self.sp_soften.valueChanged.connect(self._changed)
        sec.form.addRow("Soften edge", self.sp_soften)
        return sec

    def _build_mask(self) -> QWidget:
        sec = Section("Subject")
        self.chk_main = QCheckBox("Keep only the main person")
        self.chk_main.setToolTip(
            "Removes shapes that are not connected to the main person, such as people further back.\n"
            "Leave it off unless needed: a part of the person that looks separate (a knee behind an arm)\n"
            "can be removed too, and people touching the subject are kept."
        )
        self.chk_main.toggled.connect(self._changed)
        sec.form.addRow(self.chk_main)
        self.chk_holes = QCheckBox("Fill holes inside the person")
        self.chk_holes.setToolTip("Closes gaps that are fully enclosed by the person. Leave off for gaps between arm and body.")
        self.chk_holes.toggled.connect(self._changed)
        sec.form.addRow(self.chk_holes)
        return sec

    def _build_framing(self) -> QWidget:
        sec = Section("Framing")
        self.chk_crop = QCheckBox("Crop to the person")
        self.chk_crop.toggled.connect(self._changed)
        sec.form.addRow(self.chk_crop)
        self.sp_margin = QDoubleSpinBox()
        self.sp_margin.setRange(0.0, 100.0)
        self.sp_margin.setSingleStep(1.0)
        self.sp_margin.setDecimals(0)
        self.sp_margin.setSuffix(" %")
        self.sp_margin.setToolTip("Space kept around the person, relative to the person's size.")
        self.sp_margin.valueChanged.connect(self._changed)
        sec.form.addRow("Margin", self.sp_margin)
        return sec

    def _build_files(self) -> QWidget:
        sec = Section("Files")
        self.ed_suffix = QLineEdit()
        self.ed_suffix.setMaxLength(40)
        self.ed_suffix.setPlaceholderText("_nobg")
        self.ed_suffix.setValidator(QRegularExpressionValidator(QRegularExpression(SUFFIX_PATTERN), self.ed_suffix))
        self.ed_suffix.setToolTip(
            "Added to the original file name, e.g. photo_nobg.png.\n"
            "Existing files that this app did not make are never replaced;\n"
            "the result then gets a free name with a number, such as photo (2)."
        )
        self.ed_suffix.inputRejected.connect(self._suffix_rejected)
        self.ed_suffix.textChanged.connect(self._update_suffix_hint)
        self.ed_suffix.editingFinished.connect(self._changed)
        sec.form.addRow("Name suffix", self.ed_suffix)
        self.lbl_suffix_hint = _hint("")
        sec.form.addRow(self.lbl_suffix_hint)
        self.sp_jpeg = QSpinBox()
        self.sp_jpeg.setRange(50, 100)
        self.sp_jpeg.valueChanged.connect(self._changed)
        sec.form.addRow("JPEG quality", self.sp_jpeg)
        self.chk_mask = QCheckBox("Also save the mask (name_mask.png)")
        self.chk_mask.toggled.connect(self._changed)
        sec.form.addRow(self.chk_mask)
        self.chk_icc = QCheckBox("Keep the photo's colour profile")
        self.chk_icc.toggled.connect(self._changed)
        sec.form.addRow(self.chk_icc)
        return sec

    def _build_updates(self) -> QWidget:
        sec = Section(
            "Updates",
            "New versions of Background Editor come from its GitHub releases and are installed only "
            "when you click Update now. Newer AI models are looked for once a week.",
        )
        self.chk_app_updates = QCheckBox("Check for new versions at startup")
        self.chk_app_updates.setToolTip(
            "At most once a day, shortly after the start, Background Editor asks GitHub whether a new\n"
            "version was released, and shows a bar above the preview when there is one.\n"
            "Nothing is downloaded or installed until you click Update now."
        )
        self.chk_app_updates.toggled.connect(self._changed)
        sec.form.addRow(self.chk_app_updates)
        self.lbl_app_update_status = _hint("")
        self.lbl_app_update_status.setTextFormat(Qt.TextFormat.PlainText)  # shows text that comes from GitHub
        self.lbl_app_update_status.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        sec.form.addRow(self.lbl_app_update_status)

        self.cmb_updates = QComboBox()
        for key, title, tip in UPDATE_MODES:
            self.cmb_updates.addItem(title, key)
            self.cmb_updates.setItemData(self.cmb_updates.count() - 1, tip, Qt.ItemDataRole.ToolTipRole)
        self.cmb_updates.setToolTip("\n\n".join(f"{title}: {tip}" for _key, title, tip in UPDATE_MODES))
        self.cmb_updates.currentIndexChanged.connect(self._changed)
        sec.form.addRow("AI models", self.cmb_updates)
        sec.form.addRow(_hint("The BiRefNet authors announced newer models with a clear licence per file."))
        self.lbl_update_status = _hint("")
        self.lbl_update_status.setTextFormat(Qt.TextFormat.PlainText)  # shows text that comes from GitHub
        self.lbl_update_status.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        sec.form.addRow(self.lbl_update_status)
        self.btn_check_updates = QPushButton("Check now")
        self.btn_check_updates.setIcon(QIcon.fromTheme(QIcon.ThemeIcon.SyncSynchronizing))
        self.btn_check_updates.setToolTip(
            "Look for a new version of Background Editor and for a newer person model now.\n"
            "A new version is only installed when you click Update now; in Ask first or Off,\n"
            "no model is downloaded without asking."
        )
        self.btn_check_updates.clicked.connect(self.checkUpdatesRequested.emit)
        sec.form.addRow(_left(self.btn_check_updates))
        # Back to the model used before the last switch (and, after that, forward again).
        self.btn_switch_model = QPushButton("")
        self.btn_switch_model.setIcon(QIcon.fromTheme(QIcon.ThemeIcon.EditUndo))
        self.btn_switch_model.clicked.connect(
            lambda: self.switchModelRequested.emit(str(self.btn_switch_model.property("model") or ""))
        )
        self._updates_form = sec.form
        self._switch_row = _left(self.btn_switch_model)
        sec.form.addRow(self._switch_row)
        return sec

    def _build_offline(self) -> QWidget:
        sec = Section("Offline use")
        self.lbl_offline = _hint("")
        self.lbl_offline.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        sec.form.addRow(self.lbl_offline)
        self.chk_all_models = QCheckBox("Include all models")
        self.chk_all_models.setToolTip(
            "Also the models that only matter when you pick them under Model (portrait, general),\n"
            "not just those the quality presets use."
        )
        self.chk_all_models.toggled.connect(lambda _on: self.refresh_offline_info())
        sec.form.addRow(self.chk_all_models)
        self.btn_download_all = QPushButton("Download all models for offline use")
        self.btn_download_all.setIcon(QIcon.fromTheme(QIcon.ThemeIcon.GoDown))
        self.btn_download_all.clicked.connect(lambda: self.downloadAllRequested.emit(self.chk_all_models.isChecked()))
        sec.form.addRow(_left(self.btn_download_all))
        return sec

    # --------------------------------------------------------------- state
    def options(self) -> Options:
        return replace(self._opts)

    def set_options(self, opts: Options) -> None:
        self._loading = True
        try:
            self._opts = replace(opts)
            self.rb_transparent.setChecked(opts.background == "transparent")
            self.rb_color.setChecked(opts.background == "color")
            self.rb_image.setChecked(opts.background == "image")
            self._select_data(self.cmb_fit, opts.background_fit)
            self.sp_blur.setValue(opts.background_blur)
            self._select_data(self.cmb_format, opts.output_format)
            self.rb_same.setChecked(opts.output_mode == "same")
            self.rb_folder.setChecked(opts.output_mode == "folder")
            self.ed_folder.setText(opts.output_folder)
            self._select_data(self.cmb_model, opts.model)
            self._select_data(self.cmb_device, opts.device)
            self.sp_threads.setValue(opts.cpu_threads)
            self.chk_refine.setChecked(opts.refine_hair)
            self.chk_extra.setChecked(opts.extra_strand_pass)
            self.sp_band.setValue(opts.refine_band)
            self._select_data(self.cmb_refine_res, opts.refine_resolution)
            self.chk_decontaminate.setChecked(opts.decontaminate)
            self.sp_shift.setValue(opts.edge_shift)
            self.sp_soften.setValue(opts.edge_soften)
            self.chk_main.setChecked(opts.main_subject_only)
            self.chk_holes.setChecked(opts.fill_holes)
            self.sp_fg.setValue(opts.fg_threshold)
            self.sp_bg.setValue(opts.bg_threshold)
            self.chk_crop.setChecked(opts.crop_to_subject)
            self.sp_margin.setValue(opts.crop_margin)
            self._opts.suffix = clean_suffix(opts.suffix)
            self.ed_suffix.setText(self._opts.suffix)
            self.sp_jpeg.setValue(opts.jpeg_quality)
            self.chk_mask.setChecked(opts.save_mask)
            self.chk_icc.setChecked(opts.keep_icc)
            self.chk_app_updates.setChecked(opts.app_update_check)
            self._select_data(self.cmb_updates, opts.model_updates)
            self._sync_quality_combo()
            self._sync_enabled()
            self._sync_background_image()
        finally:
            self._loading = False

    @staticmethod
    def _select_data(combo: QComboBox, value) -> None:
        idx = combo.findData(value)
        if idx >= 0:
            combo.setCurrentIndex(idx)

    def _read_controls(self) -> Options:
        o = replace(self._opts)
        if self.rb_image.isChecked():
            o.background = "image"
        elif self.rb_color.isChecked():
            o.background = "color"
        else:
            o.background = "transparent"
        o.background_fit = self.cmb_fit.currentData() or "cover"
        o.background_blur = round(self.sp_blur.value(), 1)
        o.output_format = self.cmb_format.currentData() or "png"
        o.output_mode = "folder" if self.rb_folder.isChecked() else "same"
        o.output_folder = self.ed_folder.text().strip()
        o.model = self.cmb_model.currentData() or o.model
        o.device = self.cmb_device.currentData() or "auto"
        o.cpu_threads = self.sp_threads.value()
        o.refine_hair = self.chk_refine.isChecked()
        o.extra_strand_pass = self.chk_extra.isChecked()
        o.refine_band = round(self.sp_band.value(), 1)
        o.refine_resolution = int(self.cmb_refine_res.currentData() or 2048)
        o.decontaminate = self.chk_decontaminate.isChecked()
        o.edge_shift = self.sp_shift.value()
        o.edge_soften = round(self.sp_soften.value(), 1)
        o.main_subject_only = self.chk_main.isChecked()
        o.fill_holes = self.chk_holes.isChecked()
        o.fg_threshold = self.sp_fg.value()
        o.bg_threshold = self.sp_bg.value()
        o.crop_to_subject = self.chk_crop.isChecked()
        o.crop_margin = self.sp_margin.value()
        o.suffix = clean_suffix(self.ed_suffix.text())
        o.jpeg_quality = self.sp_jpeg.value()
        o.save_mask = self.chk_mask.isChecked()
        o.keep_icc = self.chk_icc.isChecked()
        o.app_update_check = self.chk_app_updates.isChecked()
        o.model_updates = self.cmb_updates.currentData() or "auto"
        return o

    def _changed(self, *_args) -> None:
        if self._loading:
            return
        self._opts = self._read_controls()
        self._loading = True
        try:
            self._sync_quality_combo()
            self._sync_enabled()
            self._sync_background_image()
        finally:
            self._loading = False
        self.optionsChanged.emit(replace(self._opts))

    def _on_quality(self, _index: int) -> None:
        if self._loading:
            return
        key = self.cmb_quality.currentData()
        values = presets().get(key)
        if values is None:
            return
        opts = replace(self._read_controls(), **values)
        self.set_options(opts)
        self.optionsChanged.emit(replace(self._opts))

    def _sync_quality_combo(self) -> None:
        preset = matching_preset(self._opts)
        custom_idx = self.cmb_quality.findData("custom")
        if preset is None:
            if custom_idx < 0:
                self.cmb_quality.addItem("Custom", "custom")
                custom_idx = self.cmb_quality.count() - 1
            self.cmb_quality.setCurrentIndex(custom_idx)
        else:
            self._select_data(self.cmb_quality, preset)
            if custom_idx >= 0:
                self.cmb_quality.removeItem(custom_idx)
        self.lbl_quality_hint.setText(quality_hint(preset))
        self.lbl_model_hint.setText(model_hint(self._opts.model))
        self._sync_advanced_note()

    def _sync_advanced_note(self) -> None:
        """In the simple view, say when hidden advanced settings still change the result."""
        changes = advanced_changes(self._opts) if not self.chk_advanced.isChecked() else []
        if changes:
            self.lbl_advanced_note.setText(
                f"Advanced settings active: {', '.join(changes)}. "
                "<a href='show'>Show</a> · <a href='reset'>Reset</a>"
            )
            self.lbl_advanced_note.setToolTip("Reset puts these settings back to their defaults.")
        self.lbl_advanced_note.setVisible(bool(changes))

    def _sync_enabled(self) -> None:
        o = self._opts
        is_color = o.background == "color"
        is_image = o.background == "image"
        # With Fit the image does not cover the whole photo; the solid colour fills the rest.
        pads = is_image and o.background_fit == "contain"
        form = self._bg_form
        for w in (self.image_box, self.cmb_fit, self.sp_blur):
            form.setRowVisible(w, is_image)
        form.setRowVisible(self.lbl_pad, pads)
        form.setRowVisible(self.color_row, is_color or pads)
        self.btn_custom_color.setIcon(_swatch_icon(o.background_color, 18, swatch_border(self.palette())))
        opaque = is_color or is_image
        jpeg_idx = self.cmb_format.findData("jpg")
        item = self.cmb_format.model().item(jpeg_idx)
        if item is not None:
            item.setEnabled(opaque)
        if not opaque and self.cmb_format.currentData() == "jpg":
            self._select_data(self.cmb_format, "png")
            self._opts.output_format = "png"
        self.ed_folder.setEnabled(o.output_mode == "folder")
        for w in (self.sp_band, self.cmb_refine_res, self.sp_fg, self.sp_bg, self.chk_extra):
            w.setEnabled(o.refine_hair)
        self.sp_margin.setEnabled(o.crop_to_subject)
        self.sp_jpeg.setEnabled(o.output_format == "jpg" and opaque)
        # Always available: the hair refiner runs on the CPU even when the GPU is forced.
        self.sp_threads.setEnabled(True)
        text, warning = device_hint(o)
        self.lbl_device_hint.setText(("⚠ " if warning else "") + text)
        self.lbl_device_hint.setForegroundRole(
            QPalette.ColorRole.WindowText if warning else QPalette.ColorRole.PlaceholderText
        )
        self._update_suffix_hint()

    def _update_suffix_hint(self, *_args) -> None:
        suffix = clean_suffix(self.ed_suffix.text())
        ext = self._opts.output_extension()
        if suffix:
            text = f"Example: photo{suffix}{ext}"
        elif self.rb_folder.isChecked():
            text = (
                f"Empty: results keep the photo's name (photo{ext}) in the chosen folder; "
                "next to the photo itself _nobg is added."
            )
        else:
            text = f"Empty: next to the photo _nobg is added anyway (photo_nobg{ext}), so no original is replaced."
        self.lbl_suffix_hint.setText(text)

    def _suffix_rejected(self) -> None:
        if len(self.ed_suffix.text()) >= self.ed_suffix.maxLength():
            msg = f"The suffix can be at most {self.ed_suffix.maxLength()} characters."
        else:
            msg = f"Not allowed in file names: {BAD_NAME_CHARS_TEXT}"
        QToolTip.showText(self.ed_suffix.mapToGlobal(QPoint(0, self.ed_suffix.height())), msg, self.ed_suffix)

    def _refresh_swatches(self) -> None:
        border = swatch_border(self.palette())
        for b, hex_color in self._swatch_buttons:
            b.setIcon(_swatch_icon(hex_color, 20, border))
        if hasattr(self, "cmb_format"):  # fully built
            self.btn_custom_color.setIcon(_swatch_icon(self._opts.background_color, 18, border))

    def changeEvent(self, event) -> None:  # noqa: N802
        super().changeEvent(event)
        if event.type() == QEvent.Type.PaletteChange:
            self._refresh_swatches()
            if hasattr(self, "_recent_buttons"):
                self._rebuild_recent_backgrounds()
                self._sync_background_image()

    # ------------------------------------------------ models, updates, offline
    def refresh_models(self) -> None:
        """After a model was adopted: new entries in the model list, and presets that use it."""
        self._loading = True
        try:
            self._fill_model_combo()
            self._select_data(self.cmb_model, self._opts.model)
            self._sync_quality_combo()
        finally:
            self._loading = False
        self.refresh_offline_info()
        self.show_update_status()

    def show_update_status(self, activity: str = "") -> None:
        """The Model updates status line: what runs now, or the outcome of the last check."""
        if activity:
            self.lbl_update_status.setText(activity)
            self.btn_check_updates.setEnabled(False)
            self.btn_switch_model.setEnabled(False)
            return
        try:
            status = updates.last_status()
        except Exception as exc:  # a damaged status file must not break the settings
            status = {"message": f"The update status cannot be read ({exc})."}
        self.lbl_update_status.setText(update_status_text(status if isinstance(status, dict) else {}))
        self.btn_check_updates.setEnabled(True)
        try:
            current, previous = models.preferred_segmenter(), models.previous_segmenter()
        except Exception:  # the catalogue is unreadable: nothing to switch to
            current = previous = ""
        self.btn_switch_model.setProperty("model", previous)
        self.btn_switch_model.setText(f"Use {model_title(previous)} again" if previous else "")
        self.btn_switch_model.setToolTip(
            f"Best and Balanced go back to {model_title(previous)}, the model used before the last switch. "
            f"{model_title(current)} stays on this PC." if previous else ""
        )
        self._updates_form.setRowVisible(self._switch_row, bool(previous) and previous != current)
        self.btn_switch_model.setEnabled(True)

    def show_app_update_status(self) -> None:
        """The app update status line: this version, the last check and its outcome."""
        try:
            status = app_updates.last_status()
        except Exception as exc:  # a damaged status file must not break the settings
            status = {"message": f"The update status cannot be read ({exc})."}
        self.lbl_app_update_status.setText(app_update_status_text(status, app_updates.current_version()))

    def refresh_offline_info(self) -> None:
        include_all = self.chk_all_models.isChecked()
        try:
            specs = models.offline_models(include_all)
        except Exception as exc:
            self.lbl_offline.setText(f"The model list cannot be read ({exc}).")
            self.btn_download_all.setEnabled(False)
            return
        missing = [s for s in specs if models.find_model(s) is None]
        total = sum(s.size for s in specs)
        if missing:
            need = sum(s.size for s in missing)
            text = (
                f"{len(specs)} models, {format_size(total)} in total; {len(missing)} still to download "
                f"({format_size(need)})."
            )
        else:
            text = f"All {len(specs)} models ({format_size(total)}) are on this PC."
        text += f"\nSaved in:\n{paths.model_dir()}"
        if paths.is_portable():
            text += (
                "\nThis copy is portable: once the models are here, the whole Background Editor folder "
                "can be copied to a PC without internet."
            )
        self.lbl_offline.setText(text)
        self.btn_download_all.setEnabled(bool(missing))
        self.btn_download_all.setToolTip(
            "Every model the selection needs is already on this PC." if not missing else
            f"Downloads {len(missing)} model(s), {format_size(sum(s.size for s in missing))}, into {paths.model_dir()}"
        )

    # --------------------------------------------------- background images
    def _load_recent_backgrounds(self) -> list[str]:
        raw = self._settings.value(RECENT_BACKGROUNDS_KEY, "")
        data = raw
        if isinstance(raw, str):
            try:
                data = json.loads(raw) if raw else []
            except ValueError:
                data = []
        if not isinstance(data, list):
            return []
        found: list[str] = []
        seen: set[str] = set()
        for p in data:
            if isinstance(p, str) and p.strip() and _norm(p) not in seen:
                seen.add(_norm(p))
                found.append(p)
        return found[:MAX_RECENT_BACKGROUNDS]

    def _save_recent_backgrounds(self) -> None:
        self._settings.setValue(RECENT_BACKGROUNDS_KEY, json.dumps(self._recent_bgs))

    def recent_backgrounds(self) -> list[str]:
        return list(self._recent_bgs)

    def _add_recent(self, path: str) -> None:
        key = _norm(path)
        self._recent_bgs = [path] + [p for p in self._recent_bgs if _norm(p) != key]
        del self._recent_bgs[MAX_RECENT_BACKGROUNDS:]
        self._save_recent_backgrounds()

    def _remove_recent(self, path: str) -> None:
        key = _norm(path)
        kept = [p for p in self._recent_bgs if _norm(p) != key]
        if len(kept) != len(self._recent_bgs):
            self._recent_bgs = kept
            self._save_recent_backgrounds()
            self._rebuild_recent_backgrounds()

    def _thumb_placeholder(self, broken: bool = False) -> QIcon:
        w, h = BG_THUMB.width(), BG_THUMB.height()
        pal = self.palette()

        def paint(p: QPainter, _s: int) -> None:
            rect = QRectF(0.5, 0.5, w - 1, h - 1)
            if broken:
                p.setPen(swatch_border(pal))
                p.setBrush(pal.color(QPalette.ColorRole.Midlight))
                p.drawRoundedRect(rect, 3, 3)
                p.setPen(pal.color(QPalette.ColorRole.PlaceholderText))
                p.drawText(rect, Qt.AlignmentFlag.AlignCenter, "?")
            else:
                paint_picture(p, rect, swatch_border(pal))

        # painted_icon draws square icons; draw this one at its own shape instead.
        icon = QIcon()
        for scale in (1.0, 1.5, 2.0):
            pm = QPixmap(round(w * scale), round(h * scale))
            pm.setDevicePixelRatio(scale)
            pm.fill(Qt.GlobalColor.transparent)
            p = QPainter(pm)
            p.setRenderHint(QPainter.RenderHint.Antialiasing)
            paint(p, w)
            p.end()
            icon.addPixmap(pm)
        return icon

    def _rebuild_recent_backgrounds(self) -> None:
        for b, _p in self._recent_buttons:
            self.recent_grid.removeWidget(b)
            b.deleteLater()
        self._recent_buttons = []
        current = _norm(self._opts.background_image) if self._opts.background_image else ""
        for i, path in enumerate(self._recent_bgs):
            b = QToolButton()
            b.setIconSize(BG_THUMB)
            b.setAutoRaise(True)
            b.setCheckable(True)
            b.setChecked(_norm(path) == current)
            b.setAccessibleName(f"Background {Path(path).name}")
            b.clicked.connect(lambda _=False, p=path: self._use_background(p))
            b.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
            b.customContextMenuRequested.connect(lambda pos, btn=b, p=path: self._recent_menu(btn, pos, p))
            self.recent_grid.addWidget(b, i // RECENT_COLUMNS, i % RECENT_COLUMNS)
            self._recent_buttons.append((b, path))
            self._apply_thumb(b, path)
            self._request_thumb(path)
        self.recent_box.setVisible(bool(self._recent_bgs))
        self.lbl_recent_hint.setVisible(bool(self._recent_bgs))

    def _apply_thumb(self, b: QToolButton, path: str) -> None:
        key = _norm(path)
        pm = self._bg_thumbs.get(key)
        error = self._bg_errors.get(key, "")
        full = _full_path(path)
        if pm is not None:
            b.setIcon(QIcon(pm))
            b.setToolTip(full)
        elif error:
            b.setIcon(self._thumb_placeholder(broken=True))
            if error == "unavailable":
                b.setToolTip(f"{full}\n\nThe folder cannot be reached right now (an unplugged drive or network share?).")
            else:
                b.setToolTip(f"{full}\n\n{error}")
        else:
            b.setIcon(self._thumb_placeholder())
            b.setToolTip(full)

    def _request_thumb(self, path: str) -> None:
        key = _norm(path)
        if key in self._bg_thumbs or key in self._bg_errors or key in self._bg_pending:
            return
        self._bg_pending.add(key)
        dpr = max(1.0, self.devicePixelRatioF())
        size = (round(BG_THUMB.width() * dpr), round(BG_THUMB.height() * dpr))
        self._bg_pool.start(BackgroundThumbLoader(path, size, self._bg_signals))

    def _on_background_thumb(self, path: str, qimg, error: str) -> None:
        key = _norm(path)
        self._bg_pending.discard(key)
        if qimg is not None:
            pm = QPixmap.fromImage(qimg)
            pm.setDevicePixelRatio(max(1.0, qimg.width() / BG_THUMB.width()))
            self._bg_thumbs[key] = pm
            self._bg_errors.pop(key, None)
        else:
            self._bg_errors[key] = error or "unreadable"
        if error == "missing":
            self._remove_recent(path)  # the file is gone from a folder that is still there
        else:
            for b, p in self._recent_buttons:
                if _norm(p) == key:
                    self._apply_thumb(b, p)
        if self._opts.background_image and _norm(self._opts.background_image) == key:
            self._sync_background_image()

    def _recent_menu(self, button: QToolButton, pos, path: str) -> None:
        menu = QMenu(self)
        act_use = menu.addAction("Use this image")
        act_remove = menu.addAction("Remove from recent images")
        chosen = menu.exec(button.mapToGlobal(pos))
        if chosen is act_use:
            self._use_background(path)
        elif chosen is act_remove:
            self._remove_recent(path)

    def _sync_background_image(self) -> None:
        """Name of the chosen image, which thumbnail is checked, and any problem with the file."""
        path = self._opts.background_image
        key = _norm(path) if path else ""
        for b, p in self._recent_buttons:
            b.setChecked(bool(key) and _norm(p) == key)
        if not path:
            self.lbl_bg_name.setText("No image chosen yet")
            self.lbl_bg_name.setToolTip("")
            self.lbl_bg_name.setForegroundRole(QPalette.ColorRole.PlaceholderText)
        else:
            name = Path(path).name
            fm = self.lbl_bg_name.fontMetrics()
            width = max(60, self.lbl_bg_name.width() - 4)
            self.lbl_bg_name.setText(fm.elidedText(name, Qt.TextElideMode.ElideMiddle, width))
            self.lbl_bg_name.setToolTip(_full_path(path))
            self.lbl_bg_name.setForegroundRole(QPalette.ColorRole.WindowText)
            self._request_thumb(path)
        problem = ""
        if path and self._opts.background == "image":
            error = self._bg_errors.get(key, "")
            if error == "missing":
                problem = f"{Path(path).name} is no longer there. Choose another image."
            elif error == "unavailable":
                problem = f"The folder of {Path(path).name} cannot be reached right now."
            elif error:
                problem = error
        elif self._opts.background == "image":
            problem = "Choose an image to put behind the person."
        self.lbl_bg_problem.setText(("⚠ " + problem) if problem and path else problem)
        self.lbl_bg_problem.setForegroundRole(
            QPalette.ColorRole.WindowText if path else QPalette.ColorRole.PlaceholderText
        )
        self.lbl_bg_problem.setVisible(bool(problem))

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        if hasattr(self, "lbl_bg_name") and self._opts.background_image:
            self._sync_background_image()  # re-elide the file name

    def _on_background_mode(self) -> None:
        if self._loading:
            return
        if self.rb_image.isChecked():
            # A drive or share that was unreachable may be back: look again.
            retry = [k for k, e in self._bg_errors.items() if e == "unavailable"]
            for key in retry:
                del self._bg_errors[key]
            if retry:
                self._rebuild_recent_backgrounds()
        if self.rb_image.isChecked() and not self._opts.background_image:
            # Start with the most recent image that is still usable, if there is one.
            for p in self._recent_bgs:
                if not self._bg_errors.get(_norm(p)):
                    self._opts.background_image = p
                    break
        self._changed()

    def _choose_background(self) -> None:
        exts = " ".join(f"*{e}" for e in sorted(INPUT_EXTENSIONS))
        start = self._settings.value(LAST_BACKGROUND_DIR_KEY, "") or ""
        if not start and self._opts.background_image:
            try:
                start = str(imageio.resolve_background(self._opts.background_image).parent)
            except (ValueError, OSError):
                start = ""
        if not start and paths.is_portable() and (paths.data_dir() / "backgrounds").is_dir():
            start = str(paths.data_dir() / "backgrounds")
        if not start:
            start = str(Path.home() / "Pictures")
        chosen, _ = QFileDialog.getOpenFileName(
            self, "Choose a background image", start, f"Images ({exts});;All files (*)"
        )
        if not chosen:
            return
        chosen = str(Path(chosen))
        self._settings.setValue(LAST_BACKGROUND_DIR_KEY, str(Path(chosen).parent))
        path = stored_background_path(chosen)
        key = _norm(path)
        self._bg_thumbs.pop(key, None)  # the file may have changed since it was last shown
        self._bg_errors.pop(key, None)
        self._add_recent(path)
        self._rebuild_recent_backgrounds()
        self._use_background(path)

    def _use_background(self, path: str) -> None:
        self._opts.background_image = path
        if not self.rb_image.isChecked():
            self.rb_image.setChecked(True)  # triggers _changed through the button group
        else:
            self._changed()

    # --------------------------------------------------------------- slots
    def _set_color(self, hex_color: str) -> None:
        self._opts.background_color = hex_color
        if self.rb_image.isChecked() or self.rb_color.isChecked():
            self._changed()  # as padding colour, an image stays chosen
        else:
            self.rb_color.setChecked(True)  # triggers _changed

    def _pick_color(self) -> None:
        color = QColorDialog.getColor(QColor(self._opts.background_color), self, "Background colour")
        if color.isValid():
            self._set_color(color.name().upper())

    def _browse_folder(self) -> None:
        start = self.ed_folder.text() or ""
        folder = QFileDialog.getExistingDirectory(self, "Save results to", start)
        if folder:
            self.ed_folder.setText(folder)
            self.rb_folder.setChecked(True)
            self._changed()

    def _on_advanced_toggled(self, on: bool) -> None:
        self.advanced.setVisible(on)
        self._sync_advanced_note()
        if on:
            self.refresh_offline_info()  # models may have been downloaded meanwhile
        self.advancedToggled.emit(on)

    def _on_advanced_note_link(self, link: str) -> None:
        if link == "show":
            self.chk_advanced.setChecked(True)
        elif link == "reset":
            self._reset_advanced()

    def _reset_advanced(self) -> None:
        """Put the result-changing advanced settings back to their defaults; keep the rest."""
        d = Options()
        self.set_options(replace(self._opts, **{name: getattr(d, name) for name in ADVANCED_RESULT_FIELDS}))
        self.optionsChanged.emit(replace(self._opts))

    def _reset(self) -> None:
        # The defaults are the Best preset, which follows the preferred (possibly newer) model.
        self.set_options(replace(Options(), **presets().get("best", {})))
        self.optionsChanged.emit(replace(self._opts))

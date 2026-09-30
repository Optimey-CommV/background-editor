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

"""All user-adjustable settings, shared by the UI, the worker and the engine."""

from __future__ import annotations

import math
import re
from dataclasses import asdict, dataclass, fields

# The segmentation models built into the app. Which models the user may pick is decided at
# run time by models.model_keys(), because a newer model can be adopted by the model update
# check; MODEL_KEYS (a module attribute, see __getattr__ below) follows that list too.
BUILTIN_MODEL_KEYS = ("birefnet-matting", "birefnet-portrait", "birefnet-general", "birefnet-lite")
REFINE_RESOLUTIONS = (1024, 1536, 2048, 3072, 4096)

BACKGROUNDS = ("transparent", "color", "image")
BACKGROUND_FITS = ("cover", "contain", "stretch")
MODEL_UPDATE_MODES = ("auto", "ask", "off")
MAX_BACKGROUND_BLUR = 50.0

# Allowed values for the text settings; anything else falls back to the default.
# 'model' is checked against models.model_keys() when the settings are read.
_CHOICES: dict[str, tuple[str, ...]] = {
    "device": ("auto", "gpu", "cpu"),
    "background": BACKGROUNDS,
    "background_fit": BACKGROUND_FITS,
    "output_format": ("png", "webp", "tif", "jpg"),
    "output_mode": ("same", "folder"),
    "model_updates": MODEL_UPDATE_MODES,
}

# Numeric settings are clamped to the ranges the settings panel offers.
_RANGES: dict[str, tuple[float, float]] = {
    "refine_band": (0.2, 8.0),
    "fg_threshold": (128, 255),
    "bg_threshold": (0, 127),
    "edge_shift": (-30, 30),
    "edge_soften": (0.0, 20.0),
    "cpu_threads": (0, 64),
    "jpeg_quality": (50, 100),
    "crop_margin": (0.0, 100.0),
    "background_blur": (0.0, MAX_BACKGROUND_BLUR),
}

_HEX_COLOR = re.compile(r"#?([0-9A-Fa-f]{3}|[0-9A-Fa-f]{6})")
# Characters Windows does not allow in file names, plus control characters.
_BAD_NAME_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def clean_suffix(value: str) -> str:
    """The name suffix without characters that would create folders or break the file name."""
    return _BAD_NAME_CHARS.sub("", value or "")


def threads_text(cpu_threads: int) -> str:
    """'automatic threads', '1 thread' or 'N threads', for the processor label."""
    if cpu_threads <= 0:
        return "automatic threads"
    return "1 thread" if cpu_threads == 1 else f"{cpu_threads} threads"


def clean_color(value: str) -> str | None:
    """'#RRGGBB' in upper case, or None when value is not a 3- or 6-digit hex colour."""
    m = _HEX_COLOR.fullmatch((value or "").strip())
    if not m:
        return None
    digits = m.group(1)
    if len(digits) == 3:
        digits = "".join(c * 2 for c in digits)
    return "#" + digits.upper()


def _model_keys() -> tuple[str, ...]:
    """The segmentation models the user may pick right now (built in plus adopted)."""
    from . import models  # imported late: models reads the model catalogue at import

    return tuple(models.model_keys())


@dataclass
class Options:
    # --- Cutout -------------------------------------------------------------
    model: str = "birefnet-matting"
    refine_hair: bool = True          # learned matting pass over the edge band (ViTMatte)
    refine_band: float = 1.0          # edge band half-width, % of the image's short side
    extra_strand_pass: bool = False   # second, wider refinement pass for loose strands
    refine_resolution: int = 2048     # long side (px) the refinement pass works at
    fg_threshold: int = 242           # mask >= this (0-255) is certainly the person
    bg_threshold: int = 12            # mask <= this (0-255) is certainly background
    decontaminate: bool = True        # remove old background colour from hair edges
    main_subject_only: bool = False   # drop separate shapes such as people further back
    fill_holes: bool = False          # close fully enclosed holes inside the person
    edge_shift: int = 0               # px; negative shrinks the cutout, positive grows it
    edge_soften: float = 0.0          # px; extra blur on the alpha edge

    # --- Processor ----------------------------------------------------------
    device: str = "auto"              # auto | gpu | cpu
    cpu_threads: int = 0              # 0 = automatic

    # --- Output -------------------------------------------------------------
    background: str = "transparent"   # transparent | color | image
    # The background colour. With an image it fills the bars of 'contain' and shows
    # through where the image itself is transparent.
    background_color: str = "#FFFFFF"
    background_image: str = ""        # the image file; a relative path is inside paths.data_dir()
    background_fit: str = "cover"     # cover (fill and crop) | contain (fit and pad) | stretch
    background_blur: float = 0.0      # px at the output size (Gaussian sigma), 0-50
    output_format: str = "png"        # png | webp | tif | jpg (jpg only with a colour or image)
    jpeg_quality: int = 95
    crop_to_subject: bool = False
    crop_margin: float = 8.0          # % of the subject's size, added on every side
    save_mask: bool = False           # also write <name>_mask.png
    keep_icc: bool = True
    output_mode: str = "same"         # same | folder
    output_folder: str = ""
    suffix: str = "_nobg"

    # --- Updates ------------------------------------------------------------
    app_update_check: bool = True     # look for a new app version at startup (at most daily)
    model_updates: str = "auto"       # auto | ask | off

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "Options":
        """Build options from saved settings. Unknown or out-of-range values fall back or are clamped,
        so a stale or hand-edited value can never crash the app or fail every photo."""
        opts = cls()
        model_keys: tuple[str, ...] | None = None
        for f in fields(cls):
            if f.name not in data:
                continue
            default = getattr(opts, f.name)
            value = data[f.name]
            if value is None:
                continue
            try:
                if isinstance(default, bool):
                    value = value if isinstance(value, bool) else str(value).lower() in ("1", "true", "yes")
                elif isinstance(default, int):
                    value = int(value)
                elif isinstance(default, float):
                    value = float(value)
                    if not math.isfinite(value):
                        continue
                else:
                    value = str(value)
            except (TypeError, ValueError, OverflowError):
                continue
            if f.name in _CHOICES and value not in _CHOICES[f.name]:
                continue
            if f.name == "model":
                if model_keys is None:
                    model_keys = _model_keys()
                if value not in model_keys:
                    continue
            if f.name in _RANGES:
                lo, hi = _RANGES[f.name]
                value = type(default)(min(max(value, lo), hi))
            if f.name == "refine_resolution":
                value = min(REFINE_RESOLUTIONS, key=lambda px: abs(px - value))
            elif f.name == "background_color":
                value = clean_color(value)
                if value is None:
                    continue
            elif f.name == "background_image":
                value = value.strip()
                if "\x00" in value:  # no valid path holds one; keep the default instead
                    continue
            elif f.name == "suffix":
                value = clean_suffix(value)
            setattr(opts, f.name, value)
        return opts

    def output_extension(self) -> str:
        if self.output_format == "jpg" and self.background not in ("color", "image"):
            return ".png"  # JPEG cannot hold transparency
        return "." + self.output_format


def presets() -> dict[str, dict]:
    """Quality presets shown in the simple view. They only touch cutout fields.

    'Best' and 'Balanced' use the preferred segmentation model, which is the adopted newer
    model once the model update check has switched to one, so ask again each time.
    """
    from . import models  # imported late: models reads the model catalogue at import

    best = models.preferred_segmenter()
    return {
        "best": {"model": best, "refine_hair": True, "decontaminate": True},
        "balanced": {"model": best, "refine_hair": False, "decontaminate": True},
        "fast": {"model": "birefnet-lite", "refine_hair": False, "decontaminate": True},
    }


PRESET_TITLES = {
    "best": "Best — hair refinement",
    "balanced": "Balanced",
    "fast": "Fast",
}


def matching_preset(opts: Options) -> str | None:
    for name, values in presets().items():
        if all(getattr(opts, k) == v for k, v in values.items()):
            return name
    return None


def __getattr__(name: str):
    """PRESETS and MODEL_KEYS, for callers written before models could be adopted at run time.

    Both are computed on every access, so they follow an adopted model, but a name bound by
    'from .options import PRESETS' keeps the value of that moment: call presets() and
    models.model_keys() instead.
    """
    if name == "PRESETS":
        return presets()
    if name == "MODEL_KEYS":
        return _model_keys()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

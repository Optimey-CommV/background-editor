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

"""Locating bundled assets (logo, icon) both from source and from the packaged exe,
plus small drawing helpers shared by the widgets."""

from __future__ import annotations

import math
import sys
from functools import lru_cache
from pathlib import Path
from typing import Callable

from PyQt6.QtCore import QPointF, QRectF, Qt
from PyQt6.QtGui import QColor, QIcon, QPainter, QPainterPath, QPalette, QPixmap

# Display scales an icon drawn in code is rendered for, so it stays sharp at 125-300 %.
ICON_SCALES = (1.0, 1.25, 1.5, 1.75, 2.0, 2.5, 3.0)


def is_dark(palette: QPalette) -> bool:
    """True when the palette is a dark colour scheme."""
    return palette.color(QPalette.ColorRole.Window).lightness() < 128


def swatch_border(palette: QPalette) -> QColor:
    """Outline for colour swatches that stays visible on light and dark windows."""
    return QColor(255, 255, 255, 110) if is_dark(palette) else QColor(0, 0, 0, 90)


def painted_icon(size: int, paint: Callable[[QPainter, int], None]) -> QIcon:
    """An icon drawn in code at every common display scale.

    `paint` draws in logical coordinates (0..size); each pixmap carries its device
    pixel ratio, so Qt picks the sharp one instead of stretching a 100 % bitmap.
    """
    icon = QIcon()
    for scale in ICON_SCALES:
        px = max(1, math.ceil(size * scale - 1e-6))  # Qt asks for ceil(size * dpr)
        pm = QPixmap(px, px)
        pm.setDevicePixelRatio(px / size)
        pm.fill(Qt.GlobalColor.transparent)
        p = QPainter(pm)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        paint(p, size)
        p.end()
        icon.addPixmap(pm)
    return icon


def paint_picture(p: QPainter, rect: QRectF, border: QColor | None, radius: float = 3.0) -> None:
    """A small landscape pictogram (sky, sun, hills): 'a picture of your own'.

    Fixed colours, framed by `border`, so it reads on light and dark windows alike.
    """
    clip = QPainterPath()
    clip.addRoundedRect(rect, radius, radius)
    p.save()
    p.setClipPath(clip)
    p.fillRect(rect, QColor("#BFDDF5"))
    w, h = rect.width(), rect.height()
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QColor("#F6C343"))
    p.drawEllipse(QPointF(rect.left() + w * 0.72, rect.top() + h * 0.3), w * 0.13, w * 0.13)
    far = QPainterPath()
    far.moveTo(rect.left(), rect.bottom())
    far.lineTo(rect.left(), rect.top() + h * 0.7)
    far.lineTo(rect.left() + w * 0.35, rect.top() + h * 0.42)
    far.lineTo(rect.left() + w * 0.7, rect.top() + h * 0.75)
    far.lineTo(rect.left() + w * 0.7, rect.bottom())
    far.closeSubpath()
    p.setBrush(QColor("#5E9E6E"))
    p.drawPath(far)
    near = QPainterPath()
    near.moveTo(rect.left() + w * 0.3, rect.bottom())
    near.lineTo(rect.left() + w * 0.66, rect.top() + h * 0.56)
    near.lineTo(rect.right(), rect.top() + h * 0.8)
    near.lineTo(rect.right(), rect.bottom())
    near.closeSubpath()
    p.setBrush(QColor("#3C7A4F"))
    p.drawPath(near)
    p.restore()
    if border is not None:
        p.setPen(border)
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawRoundedRect(rect.adjusted(0.5, 0.5, -0.5, -0.5), radius, radius)


def picture_icon(size: int, border: QColor) -> QIcon:
    return painted_icon(size, lambda p, s: paint_picture(p, QRectF(1, 1, s - 2, s - 2), border))


def asset_dir() -> Path:
    base = getattr(sys, "_MEIPASS", None)
    if base:
        return Path(base) / "assets"
    return Path(__file__).resolve().parents[2] / "assets"


def _fallback(size: int) -> QPixmap:
    pm = QPixmap(size, size)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QColor("#4F46E5"))
    p.drawRoundedRect(0, 0, size, size, size * 0.22, size * 0.22)
    p.setBrush(QColor("#FFFFFF"))
    p.drawEllipse(round(size * 0.36), round(size * 0.2), round(size * 0.28), round(size * 0.3))
    p.drawRoundedRect(round(size * 0.22), round(size * 0.56), round(size * 0.56), round(size * 0.44), size * 0.2, size * 0.2)
    p.end()
    return pm


@lru_cache(maxsize=None)
def logo_pixmap(size: int) -> QPixmap:
    for candidate in (512, 256, 128, 64):
        path = asset_dir() / f"logo_{candidate}.png"
        if candidate >= size and path.exists():
            pm = QPixmap(str(path))
            if not pm.isNull():
                return pm.scaled(size, size, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
    return _fallback(size)


@lru_cache(maxsize=None)
def app_icon() -> QIcon:
    ico = asset_dir() / "app.ico"
    if ico.exists():
        icon = QIcon(str(ico))
        if not icon.isNull():
            return icon
    icon = QIcon()
    for s in (16, 24, 32, 48, 64, 128, 256):
        icon.addPixmap(logo_pixmap(s))
    return icon

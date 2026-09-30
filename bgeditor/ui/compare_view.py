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

"""Before/after preview with a draggable split, zoom and pan."""

from __future__ import annotations

from PyQt6.QtCore import QPointF, QRectF, QSize, Qt, pyqtSignal
from PyQt6.QtGui import (
    QBrush,
    QColor,
    QFont,
    QImage,
    QMouseEvent,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
    QWheelEvent,
)
from PyQt6.QtWidgets import QSizePolicy, QStyle, QWidget

MIN_ZOOM = 0.02
MAX_ZOOM = 16.0
HANDLE_GRAB_PX = 10
# A fitted background image is drawn only while its shape matches the result; a stale one
# (still being fitted to a photo of another shape) would show stretched.
BACKDROP_ASPECT_TOLERANCE = 0.01


def _checker_pixmap(cell: int = 10, dpr: float = 1.0) -> QPixmap:
    """Transparency grid tile; cell is in logical pixels, drawn at the screen's pixel ratio."""
    px = max(1, round(cell * dpr))
    pm = QPixmap(px * 2, px * 2)
    pm.fill(QColor("#FFFFFF"))
    p = QPainter(pm)
    p.fillRect(0, 0, px, px, QColor("#D9D9D9"))
    p.fillRect(px, px, px, px, QColor("#D9D9D9"))
    p.end()
    pm.setDevicePixelRatio(dpr)
    return pm


class CompareView(QWidget):
    """Shows the result over a chosen backdrop; the left side of the split shows the original.

    The backdrop is the transparency grid, a colour, or an image already fitted to the
    result's shape (it may have fewer pixels than the result; it is stretched to cover it).

    Zoom is kept in logical pixels internally (Qt's coordinate system); the level reported
    through zoomChanged is in image pixels per screen pixel, so 100 % is the photo's actual
    pixels on any display scale.
    """

    zoomChanged = pyqtSignal(float)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setMinimumSize(QSize(320, 240))
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

        self._before: QPixmap | None = None
        self._after: QPixmap | None = None
        self._backdrop_image: QPixmap | None = None
        # key -> ((width, height) in device pixels, device pixel ratio, pre-scaled copy)
        self._cache: dict[str, tuple[tuple[int, int], float, QPixmap]] = {}
        self._split = 0.5
        self._compare = True
        self._fit = True
        self._scale = 1.0
        self._offset = QPointF(0, 0)
        self._drag_split = False
        self._pan_from: QPointF | None = None
        self._backdrop = "checker"
        self._backdrop_color = QColor("#FFFFFF")
        self._checker_dpr = 1.0
        self._checker = QBrush(_checker_pixmap())
        self._painted_dpr = 0.0
        self._empty_logo: QPixmap | None = None
        self._empty_title = "Drop photos here"
        self._empty_text = "or use Add photos… — portraits work best"
        self._error_title = ""
        self._error_text = ""
        self._busy_text = ""

    # ------------------------------------------------------------------ API
    def set_empty_state(self, logo: QPixmap | None, title: str, text: str) -> None:
        self._empty_logo = logo
        self._empty_title = title
        self._empty_text = text
        self.update()

    def set_busy_text(self, text: str) -> None:
        if text != self._busy_text:
            self._busy_text = text
            self.update()

    def set_error(self, title: str, text: str = "") -> None:
        """Show a message instead of a photo, e.g. when the selected file cannot be opened."""
        self._before = self._after = None
        self._cache.clear()
        self._error_title = title
        self._error_text = text
        self.update()

    def set_images(self, before: QImage | None, after: QImage | None, keep_view: bool = False) -> None:
        old_size = self._before.size() if self._before is not None else None
        self._before = QPixmap.fromImage(before) if before is not None and not before.isNull() else None
        self._after = QPixmap.fromImage(after) if after is not None and not after.isNull() else None
        self._error_title = self._error_text = ""
        self._cache.clear()
        same_size = old_size is not None and self._before is not None and old_size == self._before.size()
        if not (keep_view and same_size):
            self._fit = True
            self._split = 0.5
        if self._fit:
            self._apply_fit()
        else:
            self._clamp_offset()
        self.update()

    def has_image(self) -> bool:
        return self._before is not None or self._after is not None

    def result_size(self) -> QSize | None:
        """Pixel size of the result shown on the right, or None without a result."""
        return self._after.size() if self._after is not None else None

    def zoom_level(self) -> float:
        """Image pixels per screen pixel (1.0 = actual pixels)."""
        return self._scale * self._dpr()

    def set_compare(self, on: bool) -> None:
        self._compare = on
        self.update()

    def set_backdrop(self, mode: str, color: QColor | None = None) -> None:
        """mode: "checker", "color" (with color) or "image" (see set_backdrop_image)."""
        self._backdrop = mode
        if color is not None:
            self._backdrop_color = QColor(color)
        self.update()

    def backdrop(self) -> str:
        return self._backdrop

    def set_backdrop_image(self, image: QImage | QPixmap | None) -> None:
        """The background fitted to the result (same shape), drawn in "image" mode.

        Until one is set, or while it does not match the result's shape, the transparency
        grid is shown instead.
        """
        if isinstance(image, QImage):
            image = QPixmap.fromImage(image) if not image.isNull() else None
        self._backdrop_image = image if image is not None and not image.isNull() else None
        self._cache.pop("backdrop", None)
        if self._backdrop == "image":
            self.update()

    def has_backdrop_image(self) -> bool:
        """True when the image backdrop is set and fits the current result."""
        return self._usable_backdrop_image() is not None

    def fit(self) -> None:
        self._fit = True
        self._apply_fit()
        self.update()

    def actual_size(self) -> None:
        self._zoom_to(1.0 / self._dpr(), QPointF(self.width() / 2, self.height() / 2))

    def zoom_by(self, factor: float) -> None:
        self._zoom_to(self._scale * factor, QPointF(self.width() / 2, self.height() / 2))

    # ------------------------------------------------------------ geometry
    def _dpr(self) -> float:
        return max(1.0, float(self.devicePixelRatioF()))

    def _image_size(self) -> QSize | None:
        pm = self._after or self._before
        return pm.size() if pm is not None else None

    def _apply_fit(self) -> None:
        size = self._image_size()
        if size is None or size.isEmpty():
            return
        margin = 16
        avail_w = max(1, self.width() - 2 * margin)
        avail_h = max(1, self.height() - 2 * margin)
        # Never enlarge beyond the photo's actual pixels.
        self._scale = min(avail_w / size.width(), avail_h / size.height(), 1.0 / self._dpr())
        w = size.width() * self._scale
        h = size.height() * self._scale
        self._offset = QPointF((self.width() - w) / 2, (self.height() - h) / 2)
        self.zoomChanged.emit(self.zoom_level())

    def _zoom_to(self, scale: float, anchor: QPointF) -> None:
        if self._image_size() is None:
            return
        scale = max(MIN_ZOOM, min(MAX_ZOOM, scale))
        img_pt = (anchor - self._offset) / self._scale
        self._scale = scale
        self._offset = anchor - img_pt * scale
        self._fit = False
        self._clamp_offset()
        self.zoomChanged.emit(self.zoom_level())
        self.update()

    def _clamp_offset(self) -> None:
        size = self._image_size()
        if size is None:
            return
        w = size.width() * self._scale
        h = size.height() * self._scale
        x, y = self._offset.x(), self._offset.y()
        # Keep the image centred when it is smaller than the view, otherwise keep it covering the view.
        x = (self.width() - w) / 2 if w <= self.width() else min(0.0, max(self.width() - w, x))
        y = (self.height() - h) / 2 if h <= self.height() else min(0.0, max(self.height() - h, y))
        self._offset = QPointF(x, y)

    def _pannable(self) -> bool:
        """True when the zoomed photo is larger than the view, so dragging can move it."""
        size = self._image_size()
        if size is None:
            return False
        return size.width() * self._scale > self.width() + 0.5 or size.height() * self._scale > self.height() + 0.5

    def _target_rect(self) -> QRectF:
        size = self._image_size()
        if size is None:
            return QRectF()
        return QRectF(self._offset.x(), self._offset.y(), size.width() * self._scale, size.height() * self._scale)

    def _split_x(self) -> float:
        r = self._target_rect()
        return r.left() + r.width() * self._split

    def _showing_split(self) -> bool:
        return self._compare and self._before is not None and self._after is not None

    # ------------------------------------------------------------- drawing
    def _scaled(self, key: str, pm: QPixmap, w: int, h: int, dpr: float) -> QPixmap:
        """Downscaled copy at the screen's pixel size for the current zoom, so painting (and
        split dragging) stays fast and the painter does not stretch it again."""
        cached = self._cache.get(key)
        if cached is not None and cached[0] == (w, h) and cached[1] == dpr:
            return cached[2]
        scaled = pm.scaled(w, h, Qt.AspectRatioMode.IgnoreAspectRatio, Qt.TransformationMode.SmoothTransformation)
        scaled.setDevicePixelRatio(dpr)
        self._cache[key] = ((w, h), dpr, scaled)
        return scaled

    def _draw_image(self, p: QPainter, key: str, pm: QPixmap, target: QRectF, smooth_up: bool = False) -> None:
        """Draw pm stretched over target. Photos are shown crisp from 200 % on, so single
        pixels can be judged; smooth_up keeps a backdrop smooth at any zoom."""
        dpr = self._dpr()
        w = max(1, round(target.width() * dpr))
        h = max(1, round(target.height() * dpr))
        zoom = w / max(1, pm.width())  # screen pixels per pixmap pixel
        if zoom >= 1.0:
            p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, smooth_up or zoom < 2.0)
            p.drawPixmap(target, pm, QRectF(pm.rect()))
        else:
            # Put the pre-scaled copy on whole screen pixels so it is drawn 1:1.
            x = round(target.left() * dpr) / dpr
            y = round(target.top() * dpr) / dpr
            p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, False)
            p.drawPixmap(QPointF(x, y), self._scaled(key, pm, w, h, dpr))

    def _usable_backdrop_image(self) -> QPixmap | None:
        bg = self._backdrop_image
        size = self._image_size()
        if bg is None or size is None or size.isEmpty() or bg.height() == 0:
            return None
        ratio = (bg.width() / bg.height()) / (size.width() / size.height())
        return bg if abs(ratio - 1.0) <= BACKDROP_ASPECT_TOLERANCE else None

    def _fill_backdrop(self, p: QPainter, rect: QRectF, target: QRectF) -> None:
        """Fill rect (the part of the photo that shows the result; the caller clips to it)."""
        if self._backdrop == "color":
            p.fillRect(rect, self._backdrop_color)
            return
        if self._backdrop == "image":
            bg = self._usable_backdrop_image()
            if bg is not None:
                self._draw_image(p, "backdrop", bg, target, smooth_up=True)
                return
        # Anchor the grid to the photo, not to the split, so it does not slide with the handle.
        p.setBrushOrigin(target.topLeft())
        p.fillRect(rect, self._checker)

    def _check_dpr(self) -> None:
        """Follow the window to a screen with another scale factor."""
        dpr = self._dpr()
        if dpr == self._painted_dpr:
            return
        first = self._painted_dpr == 0.0
        self._painted_dpr = dpr
        self._cache.clear()
        if dpr != self._checker_dpr:
            self._checker_dpr = dpr
            self._checker = QBrush(_checker_pixmap(dpr=dpr))
        if self.has_image():
            if self._fit:
                self._apply_fit()
            elif not first:
                self.zoomChanged.emit(self.zoom_level())

    def paintEvent(self, _event) -> None:  # noqa: N802 (Qt naming)
        self._check_dpr()
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        pal = self.palette()
        p.fillRect(self.rect(), pal.color(pal.ColorRole.Base).darker(104))

        if not self.has_image():
            if self._error_title:
                self._paint_error(p)
            else:
                self._paint_empty(p)
            if self._busy_text:
                self._paint_badge(p, self._busy_text, QPointF(self.width() / 2, 28), center=True)
            p.end()
            return

        target = self._target_rect()
        if self._showing_split():
            sx = self._split_x()
            left = QRectF(target.left(), target.top(), max(0.0, sx - target.left()), target.height())
            right = QRectF(sx, target.top(), max(0.0, target.right() - sx), target.height())
            p.save()
            p.setClipRect(left)
            self._draw_image(p, "before", self._before, target)
            p.restore()
            p.save()
            p.setClipRect(right)
            self._fill_backdrop(p, right, target)
            self._draw_image(p, "after", self._after, target)
            p.restore()
            self._paint_handle(p, sx, target)
        elif self._after is not None:
            self._fill_backdrop(p, target, target)
            self._draw_image(p, "after", self._after, target)
        else:
            self._draw_image(p, "before", self._before, target)

        if self._busy_text:
            self._paint_badge(p, self._busy_text, QPointF(self.width() / 2, 28), center=True)
        p.end()

    def _paint_handle(self, p: QPainter, sx: float, target: QRectF) -> None:
        top = max(0.0, target.top())
        bottom = min(float(self.height()), target.bottom())
        p.setPen(QPen(QColor(255, 255, 255, 230), 2))
        p.drawLine(QPointF(sx, top), QPointF(sx, bottom))
        p.setPen(QPen(QColor(0, 0, 0, 60), 1))
        p.drawLine(QPointF(sx + 1.5, top), QPointF(sx + 1.5, bottom))
        cy = (top + bottom) / 2
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(0, 0, 0, 50))
        p.drawEllipse(QPointF(sx, cy + 1.5), 17, 17)
        p.setBrush(QColor("#FFFFFF"))
        p.drawEllipse(QPointF(sx, cy), 16, 16)
        # Two small arrows inside the knob.
        p.setBrush(QColor("#5B5B5B"))
        for d in (-1, 1):
            path = QPainterPath()
            path.moveTo(sx + d * 4, cy - 5)
            path.lineTo(sx + d * 10, cy)
            path.lineTo(sx + d * 4, cy + 5)
            path.closeSubpath()
            p.drawPath(path)
        self._paint_badge(p, "Before", QPointF(max(target.left(), 0) + 12, max(target.top(), 0) + 12))
        after_w = self._badge_width(p, "After")
        self._paint_badge(
            p, "After", QPointF(min(target.right(), self.width()) - 12 - after_w, max(target.top(), 0) + 12)
        )

    def _badge_width(self, p: QPainter, text: str) -> float:
        return p.fontMetrics().horizontalAdvance(text) + 16

    def _paint_badge(self, p: QPainter, text: str, pos: QPointF, center: bool = False) -> None:
        fm = p.fontMetrics()
        w = fm.horizontalAdvance(text) + 16
        h = fm.height() + 8
        x = pos.x() - w / 2 if center else pos.x()
        rect = QRectF(x, pos.y(), w, h)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(0, 0, 0, 150))
        p.drawRoundedRect(rect, h / 2, h / 2)
        p.setPen(QColor("#FFFFFF"))
        p.drawText(rect, Qt.AlignmentFlag.AlignCenter, text)

    def _paint_empty(self, p: QPainter) -> None:
        pal = self.palette()
        cx = self.width() / 2
        cy = self.height() / 2
        y = cy - 90
        if self._empty_logo is not None and not self._empty_logo.isNull():
            logo = self._empty_logo
            size = 112
            p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
            p.drawPixmap(QRectF(cx - size / 2, y - size / 2, size, size), logo, QRectF(logo.rect()))
            y += size / 2 + 20
        else:
            y = cy - 20
        title_font = QFont(self.font())
        title_font.setPointSizeF(self.font().pointSizeF() * 1.6)
        title_font.setWeight(QFont.Weight.DemiBold)
        p.setFont(title_font)
        p.setPen(pal.color(pal.ColorRole.Text))
        p.drawText(QRectF(0, y, self.width(), 34), Qt.AlignmentFlag.AlignCenter, self._empty_title)
        p.setFont(self.font())
        p.setPen(pal.color(pal.ColorRole.PlaceholderText))
        p.drawText(QRectF(0, y + 36, self.width(), 24), Qt.AlignmentFlag.AlignCenter, self._empty_text)
        # Dashed drop zone around the message, derived from the text colour so it shows in
        # light and dark mode alike.
        zone_color = QColor(pal.color(pal.ColorRole.Text))
        zone_color.setAlphaF(0.35)
        pen = QPen(zone_color, 1.5, Qt.PenStyle.DashLine)
        p.setPen(pen)
        p.setBrush(Qt.BrushStyle.NoBrush)
        zone = QRectF(24, 24, self.width() - 48, self.height() - 48)
        p.drawRoundedRect(zone, 14, 14)

    def _paint_error(self, p: QPainter) -> None:
        pal = self.palette()
        cx = self.width() / 2
        cy = self.height() / 2
        icon = self.style().standardIcon(QStyle.StandardPixmap.SP_MessageBoxWarning)
        size = 48
        top = cy - 70
        icon.paint(p, round(cx - size / 2), round(top), size, size)
        y = top + size + 14
        title_font = QFont(self.font())
        title_font.setPointSizeF(self.font().pointSizeF() * 1.3)
        title_font.setWeight(QFont.Weight.DemiBold)
        p.setFont(title_font)
        p.setPen(pal.color(pal.ColorRole.Text))
        p.drawText(QRectF(24, y, self.width() - 48, 30), Qt.AlignmentFlag.AlignCenter, self._error_title)
        if self._error_text:
            p.setFont(self.font())
            p.setPen(pal.color(pal.ColorRole.PlaceholderText))
            flags = Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop | Qt.TextFlag.TextWordWrap
            p.drawText(QRectF(40, y + 34, self.width() - 80, self.height() - y - 44), flags, self._error_text)

    # -------------------------------------------------------------- events
    def resizeEvent(self, _event) -> None:  # noqa: N802
        if self._fit:
            self._apply_fit()
        else:
            self._clamp_offset()

    def wheelEvent(self, event: QWheelEvent) -> None:  # noqa: N802
        if self._image_size() is None:
            return
        steps = event.angleDelta().y() / 120.0
        if steps == 0:
            return
        self._zoom_to(self._scale * (1.15 ** steps), event.position())

    def mousePressEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if self._image_size() is None:
            return
        pos = event.position()
        if event.button() == Qt.MouseButton.LeftButton and self._showing_split() and abs(pos.x() - self._split_x()) <= HANDLE_GRAB_PX:
            self._drag_split = True
        elif event.button() in (Qt.MouseButton.LeftButton, Qt.MouseButton.MiddleButton):
            if self._showing_split() and event.button() == Qt.MouseButton.LeftButton and self._fit:
                # In fit view a click moves the split straight to the cursor.
                self._drag_split = True
                self._move_split(pos.x())
            elif self._pannable():
                self._pan_from = pos
                self.setCursor(Qt.CursorShape.ClosedHandCursor)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        pos = event.position()
        if self._drag_split:
            self._move_split(pos.x())
            return
        if self._pan_from is not None:
            # A photo that fits the view cannot be moved, so a wobbly click keeps 'fit' on.
            if self._pannable():
                self._offset += pos - self._pan_from
                self._fit = False
                self._clamp_offset()
                self.update()
            self._pan_from = pos
            return
        if self._showing_split() and abs(pos.x() - self._split_x()) <= HANDLE_GRAB_PX:
            self.setCursor(Qt.CursorShape.SplitHCursor)
        elif self._pannable():
            self.setCursor(Qt.CursorShape.OpenHandCursor)
        else:
            self.unsetCursor()

    def mouseReleaseEvent(self, _event: QMouseEvent) -> None:  # noqa: N802
        self._drag_split = False
        self._pan_from = None
        self.unsetCursor()

    def mouseDoubleClickEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if self._image_size() is None:
            return
        if self._fit:
            self._zoom_to(1.0 / self._dpr(), event.position())
        else:
            self.fit()

    def _move_split(self, x: float) -> None:
        r = self._target_rect()
        if r.width() <= 0:
            return
        self._split = max(0.0, min(1.0, (x - r.left()) / r.width()))
        self.update()

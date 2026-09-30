#!/usr/bin/env python3
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

"""Render the Background Editor logo SVGs to PNG files and a Windows icon.

Outputs, written next to this script:

    app.ico          16, 20, 24, 32, 40, 48, 64, 96, 128 and 256 px frames,
                     32-bit RGBA (PNG-compressed frames, Windows Vista and later)
    logo_<n>.png     64, 128, 256 and 512 px transparent PNGs for use in the app
    preview.png      review sheet: every icon frame at 1:1 and at 4x
                     nearest-neighbour zoom, on a light (#F3F3F3) and a dark
                     (#202020) background

Sources:

    logo.svg         master artwork, used for 40 px and up
    logo_small.svg   simplified artwork on a 16-unit grid, used up to 32 px

Requires PyQt6 (QtSvg) and Pillow. Run with:  python build_icon.py
"""

from __future__ import annotations

import os
import struct
import sys
from pathlib import Path

# Render without touching the desktop session; must be set before Qt loads.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PIL import Image, ImageDraw, ImageFont  # noqa: E402
from PyQt6.QtCore import QRectF, Qt  # noqa: E402
from PyQt6.QtGui import QGuiApplication, QImage, QPainter  # noqa: E402
from PyQt6.QtSvg import QSvgRenderer  # noqa: E402

HERE = Path(__file__).resolve().parent
MASTER_SVG = HERE / "logo.svg"
SMALL_SVG = HERE / "logo_small.svg"

ICO_SIZES = [16, 20, 24, 32, 40, 48, 64, 96, 128, 256]
PNG_SIZES = [64, 128, 256, 512]
SMALL_MAX = 32  # frames up to this size are drawn from logo_small.svg

# Where the pixel grid of each source starts, in SVG units. The master tile
# and its checker cells start at 16 of 256; the small artwork starts at 0.
# If that origin lands on a half pixel at some size (40 px: 16 * 40 / 256 =
# 2.5), the whole drawing is shifted by that fraction so edges stay sharp.
GRID_ORIGIN = {MASTER_SVG: 16.0, SMALL_SVG: 0.0}

LIGHT_BG = (0xF3, 0xF3, 0xF3, 255)
DARK_BG = (0x20, 0x20, 0x20, 255)


def source_for(size: int) -> Path:
    return SMALL_SVG if size <= SMALL_MAX else MASTER_SVG


def snap_offset(svg: Path, renderer: QSvgRenderer, size: int) -> float:
    """Shift (in px) that puts the artwork's grid origin on a whole pixel."""
    units = renderer.viewBoxF().width()
    pos = GRID_ORIGIN[svg] * size / units
    return round(pos) - pos


def render_svg(svg: Path, size: int) -> Image.Image:
    """Render an SVG into a size x size straight-alpha RGBA Pillow image."""
    renderer = QSvgRenderer(str(svg))
    if not renderer.isValid():
        raise RuntimeError(f"cannot parse {svg}")
    shift = snap_offset(svg, renderer, size)

    image = QImage(size, size, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(Qt.GlobalColor.transparent)
    painter = QPainter(image)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
    renderer.render(painter, QRectF(shift, shift, size, size))
    painter.end()

    # RGBA8888 is non-premultiplied, which is what PNG and ICO expect.
    image = image.convertToFormat(QImage.Format.Format_RGBA8888)
    data = image.constBits().asstring(image.sizeInBytes())
    return Image.frombuffer(
        "RGBA", (size, size), data, "raw", "RGBA", image.bytesPerLine(), 1
    ).copy()


def write_ico(frames: dict[int, Image.Image], path: Path) -> None:
    largest = frames[max(frames)]
    others = [frames[s] for s in sorted(frames) if s != max(frames)]
    largest.save(
        path,
        format="ICO",
        sizes=[(s, s) for s in sorted(frames)],
        append_images=others,
    )


def verify_ico(path: Path, frames: dict[int, Image.Image]) -> list[str]:
    """Check the icon directory and every frame; return a list of problems."""
    problems = []
    raw = path.read_bytes()
    reserved, kind, count = struct.unpack_from("<HHH", raw, 0)
    if (reserved, kind) != (0, 1):
        problems.append("not an icon file")
    if count != len(frames):
        problems.append(f"{count} frames in directory, expected {len(frames)}")
    for i in range(count):
        w, h, _colors, _res, _planes, bits, _size, _offset = struct.unpack_from(
            "<BBBBHHII", raw, 6 + 16 * i
        )
        w, h = w or 256, h or 256
        if bits != 32:
            problems.append(f"{w}x{h} frame declares {bits} bpp, expected 32")

    with Image.open(path) as ico:
        found = sorted(s[0] for s in ico.info["sizes"])
        if found != sorted(frames):
            problems.append(f"sizes {found}, expected {sorted(frames)}")
        for size, expected in frames.items():
            frame = ico.ico.getimage((size, size))
            if frame.size != (size, size):
                problems.append(f"{size} px frame has size {frame.size}")
            if frame.mode != "RGBA":
                problems.append(f"{size} px frame is {frame.mode}, expected RGBA")
            elif frame.tobytes() != expected.tobytes():
                problems.append(f"{size} px frame differs from its render")
    return problems


def load_font(size: int) -> ImageFont.ImageFont:
    for name in ("segoeui.ttf", "arial.ttf", "DejaVuSans.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default()


def build_preview(frames: dict[int, Image.Image], path: Path) -> None:
    """One sheet: a light and a dark column, each with 1:1 and 4x views."""
    zoom = 4
    pad = 24
    gap = 20
    column_width = 1200
    font = load_font(15)
    title_font = load_font(20)

    def layout(width: int) -> tuple[list[tuple[int, int, int]], int]:
        """Place the 4x zooms left to right, wrapping rows; return (x, y, size)."""
        spots, x, y, row_h = [], 0, 0, 0
        for size in sorted(frames):
            w = size * zoom
            if x and x + w > width:
                x, y, row_h = 0, y + row_h + gap + 22, 0
            spots.append((x, y, size))
            x += w + gap
            row_h = max(row_h, w)
        return spots, y + row_h + 22

    zoom_spots, zoom_height = layout(column_width - 2 * pad)
    actual_height = max(frames) + 22
    height = pad + 34 + actual_height + gap + 30 + zoom_height + pad
    sheet = Image.new("RGBA", (2 * column_width, height), LIGHT_BG)

    for col, (bg, label) in enumerate([(LIGHT_BG, "Light #F3F3F3"), (DARK_BG, "Dark #202020")]):
        ox = col * column_width
        panel = Image.new("RGBA", (column_width, height), bg)
        draw = ImageDraw.Draw(panel)
        ink = (32, 32, 32, 255) if bg == LIGHT_BG else (235, 235, 235, 255)
        muted = (110, 110, 110, 255) if bg == LIGHT_BG else (160, 160, 160, 255)

        y = pad
        draw.text((pad, y), f"{label}  -  actual size (1:1)", font=title_font, fill=ink)
        y += 34
        x = pad
        base = y + max(frames)
        for size in sorted(frames):
            panel.alpha_composite(frames[size], (x, base - size))
            draw.text((x, base + 4), str(size), font=font, fill=muted)
            x += size + gap

        y = base + 22 + gap
        draw.text((pad, y), "4x nearest-neighbour zoom", font=title_font, fill=ink)
        y += 30
        for zx, zy, size in zoom_spots:
            big = frames[size].resize((size * zoom, size * zoom), Image.Resampling.NEAREST)
            panel.alpha_composite(big, (pad + zx, y + zy))
            draw.text((pad + zx, y + zy + size * zoom + 2), f"{size} px", font=font, fill=muted)

        sheet.paste(panel, (ox, 0))

    sheet.convert("RGB").save(path, optimize=True)


def main() -> int:
    app = QGuiApplication.instance() or QGuiApplication(sys.argv[:1])  # noqa: F841

    frames = {size: render_svg(source_for(size), size) for size in ICO_SIZES}

    ico_path = HERE / "app.ico"
    write_ico(frames, ico_path)
    problems = verify_ico(ico_path, frames)

    for size in PNG_SIZES:
        render_svg(MASTER_SVG, size).save(HERE / f"logo_{size}.png", optimize=True)

    build_preview(frames, HERE / "preview.png")

    print(f"wrote {ico_path.name}: " + ", ".join(
        f"{s} ({source_for(s).name})" for s in ICO_SIZES))
    print("wrote " + ", ".join(f"logo_{s}.png" for s in PNG_SIZES) + ", preview.png")
    if problems:
        for problem in problems:
            print("ICO CHECK FAILED:", problem, file=sys.stderr)
        return 1
    print(f"ico check passed: {len(frames)} frames, all 32-bit RGBA, identical to the renders")
    return 0


if __name__ == "__main__":
    sys.exit(main())

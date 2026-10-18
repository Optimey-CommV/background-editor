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

"""Render the side image of the NSIS installer's finish page (installer.nsi,
MUI_WELCOMEFINISHPAGE_BITMAP) from logo_512.png, next to this script:

    installer-side.bmp   164 x 314 px, 24-bit: a vertical gradient in the logo's
                         teal and violet with the logo near the top

Run it with the project venv (Pillow) after the logo changes:
    .venv\\Scripts\\python.exe assets\\build_installer_art.py
"""

from pathlib import Path

from PIL import Image

HERE = Path(__file__).resolve().parent
WIDTH, HEIGHT = 164, 314
TOP = (32, 178, 170)      # the teal of the logo's background
BOTTOM = (91, 62, 196)    # the violet of the logo's figure
LOGO_SIZE = 116
LOGO_TOP = 40


def main() -> None:
    side = Image.new("RGB", (WIDTH, HEIGHT))
    for y in range(HEIGHT):
        t = y / (HEIGHT - 1)
        colour = tuple(round(a + (b - a) * t) for a, b in zip(TOP, BOTTOM))
        for x in range(WIDTH):
            side.putpixel((x, y), colour)
    logo = Image.open(HERE / "logo_512.png").convert("RGBA").resize((LOGO_SIZE, LOGO_SIZE), Image.LANCZOS)
    side.paste(logo, ((WIDTH - LOGO_SIZE) // 2, LOGO_TOP), logo)
    out = HERE / "installer-side.bmp"
    side.save(out, format="BMP")
    print(f"wrote {out} ({WIDTH} x {HEIGHT})")


if __name__ == "__main__":
    main()

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

"""A reliable taskbar icon on Windows.

Qt gives its window class the icon of a resource named IDI_ICON1, which a PyInstaller exe
does not have, so the class icon is Windows' default application icon; only the window
icons (WM_SETICON) are ours. The taskbar sometimes showed the class icon (seen when its
icon cache was stale). So the class gets the window's own icons as well, and the window
icons are sent once more after the window is shown, which makes the taskbar read them again.

Everything here is Windows-only and fails silently elsewhere or on any error.
"""

from __future__ import annotations

import sys

WM_GETICON = 0x007F
WM_SETICON = 0x0080
ICON_SMALL = 0
ICON_BIG = 1
GCLP_HICON = -14
GCLP_HICONSM = -34

# Our own copies of the icons. The window class keeps pointing at them, so they are never
# destroyed (Qt destroys its own icon handles when the window icon changes).
_copies: dict[int, tuple[int, int]] = {}  # window handle -> (big, small)


def _user32():
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32")
    user32.SendMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    user32.SendMessageW.restype = ctypes.c_ssize_t
    user32.CopyIcon.argtypes = [wintypes.HICON]
    user32.CopyIcon.restype = wintypes.HICON
    user32.SetClassLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_ssize_t]
    user32.SetClassLongPtrW.restype = ctypes.c_size_t
    user32.GetClassLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.GetClassLongPtrW.restype = ctypes.c_size_t
    return user32


def _icons(user32, hwnd: int) -> tuple[int, int]:
    """Copies of the window's big and small icon (0 where the window has none)."""
    if hwnd in _copies:
        return _copies[hwnd]
    big = user32.SendMessageW(hwnd, WM_GETICON, ICON_BIG, 0)
    small = user32.SendMessageW(hwnd, WM_GETICON, ICON_SMALL, 0)
    pair = ((user32.CopyIcon(big) or 0) if big else 0, (user32.CopyIcon(small) or 0) if small else 0)
    if any(pair):
        _copies[hwnd] = pair
    return pair


def set_class_icons(widget) -> bool:
    """Give the window class of `widget` the window's own icons. Call once the native window
    exists (winId()) and before it is shown. True when the class icons were set."""
    if sys.platform != "win32":
        return False
    try:
        user32 = _user32()
        hwnd = int(widget.winId())
        big, small = _icons(user32, hwnd)
        if big:
            user32.SetClassLongPtrW(hwnd, GCLP_HICON, big)
        if small:
            user32.SetClassLongPtrW(hwnd, GCLP_HICONSM, small)
        return bool(big or small)
    except (AttributeError, OSError, ValueError, TypeError):
        return False


def resend_window_icons(widget) -> bool:
    """Send WM_SETICON (big and small) again, so the taskbar refreshes a stale icon."""
    if sys.platform != "win32":
        return False
    try:
        user32 = _user32()
        hwnd = int(widget.winId())
        big, small = _icons(user32, hwnd)
        if big:
            user32.SendMessageW(hwnd, WM_SETICON, ICON_BIG, big)
        if small:
            user32.SendMessageW(hwnd, WM_SETICON, ICON_SMALL, small)
        return bool(big or small)
    except (AttributeError, OSError, ValueError, TypeError):
        return False


def class_icons(widget) -> tuple[int, int]:
    """(GCLP_HICON, GCLP_HICONSM) of the widget's window class, for tests."""
    if sys.platform != "win32":
        return (0, 0)
    try:
        user32 = _user32()
        hwnd = int(widget.winId())
        return (user32.GetClassLongPtrW(hwnd, GCLP_HICON), user32.GetClassLongPtrW(hwnd, GCLP_HICONSM))
    except (AttributeError, OSError, ValueError, TypeError):
        return (0, 0)

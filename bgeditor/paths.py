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

"""Where the app keeps models, settings and logs.

Installed (no marker file): per user, in %LOCALAPPDATA%\\BackgroundEditor and the registry.
Portable (a 'portable.conf' next to BackgroundEditor.exe): everything inside the app folder,
so the whole folder can be copied to another PC, including one without internet:

    BackgroundEditor\\
        BackgroundEditor.exe
        portable.conf
        models\\      downloaded AI models
        data\\        settings.ini, error.log, GPU compatibility notes, model catalogue
"""

from __future__ import annotations

import os
import sys
import uuid
from functools import lru_cache
from pathlib import Path

APP_NAME = "BackgroundEditor"
PORTABLE_MARKER = "portable.conf"
PORTABLE_MARKER_TEXT = """# Background Editor - portable mode
#
# This file makes Background Editor portable: downloaded AI models, settings, logs and
# other data are kept inside this folder, so the whole folder can be copied to another
# PC, also one without internet. Download the models first on a PC with internet:
# Advanced settings > Download all models for offline use.
#
# Delete this file to keep models and settings in your user profile instead.
#
# Optional: other folders, relative to this folder (or absolute paths).
# models = models
# data = data
"""


def app_dir() -> Path:
    """The folder of BackgroundEditor.exe, or the project folder when run from source."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def user_data_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    return Path(base) / APP_NAME


@lru_cache(maxsize=1)
def is_portable() -> bool:
    if os.environ.get("BGEDITOR_PORTABLE") in ("0", "1"):  # override for tests
        return os.environ["BGEDITOR_PORTABLE"] == "1"
    return (app_dir() / PORTABLE_MARKER).is_file()


@lru_cache(maxsize=1)
def portable_config() -> dict[str, str]:
    """'key = value' lines of portable.conf; '#' starts a comment."""
    values: dict[str, str] = {}
    try:
        text = (app_dir() / PORTABLE_MARKER).read_text(encoding="utf-8-sig", errors="replace")
    except OSError:
        return values
    for line in text.splitlines():
        line = line.split("#", 1)[0].strip()
        if "=" in line:
            key, value = (part.strip() for part in line.split("=", 1))
            if key and value:
                values[key.lower()] = value.strip('"')
    return values


def _portable_folder(key: str, default: str) -> Path:
    folder = Path(os.path.expandvars(portable_config().get(key, default)))
    return folder if folder.is_absolute() else app_dir() / folder


def data_dir() -> Path:
    return _portable_folder("data", "data") if is_portable() else user_data_dir()


def model_dir() -> Path:
    """Where new downloads go."""
    return _portable_folder("models", "models") if is_portable() else user_data_dir() / "models"


def model_search_dirs() -> list[Path]:
    """Where existing models are looked for, first match wins.

    A 'models' folder next to the exe is always searched first, so an installed copy can be
    given its models by hand too; a portable copy never looks in the user profile, so what
    works on this PC also works after copying the folder.
    """
    dirs = [model_dir()] if is_portable() else [app_dir() / "models", user_data_dir() / "models"]
    if is_portable() and app_dir() / "models" not in dirs:
        dirs.append(app_dir() / "models")
    return dirs


def folder_writable(folder: Path) -> bool:
    """Real write test: os.access ignores ACLs on Windows."""
    try:
        folder.mkdir(parents=True, exist_ok=True)
        probe = folder / f".bgeditor-probe.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp"
        with open(probe, "wb"):
            pass
        probe.unlink()
        return True
    except OSError:
        return False


def settings():
    """QSettings for this mode: an INI file in data\\ when portable, the registry otherwise."""
    from PyQt6.QtCore import QSettings

    if is_portable():
        return QSettings(str(data_dir() / "settings.ini"), QSettings.Format.IniFormat)
    return QSettings(APP_NAME, APP_NAME)

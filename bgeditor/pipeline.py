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

"""One photo in, one file out: load, cut out, finish the edge, frame, compose and save."""

from __future__ import annotations

import os
import stat
import threading
from collections import defaultdict
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable, Iterable

import numpy as np
from PIL import Image

from .engine import Cancelled
from .imageio import (
    check_background,
    check_writable,
    compose,
    hex_to_rgb,
    load_image,
    prepare_background,
    restore_colours,
    save_image,
    srgb_color_to_profile,
    to_srgb,
)
from .options import Options, clean_suffix

Report = Callable[[str], None]

DEFAULT_SUFFIX = "_nobg"
MASK_TAG = "_mask"


@dataclass
class FileResult:
    output: Path
    mask: Path | None
    size: tuple[int, int]


# ------------------------------------------------------------------ naming
def _key(path: Path) -> str:
    """Comparison key for paths: absolute and case-insensitive, as Windows treats them."""
    return os.path.normcase(os.path.abspath(path))


def _same_folder(a: Path, b: Path) -> bool:
    if _key(a) == _key(b):
        return True
    try:
        return os.path.samefile(a, b)
    except (OSError, ValueError):
        return False


def output_folder_for(src: Path, opts: Options) -> Path:
    """Where the result of src goes. ~ and %VARS% are expanded; a relative folder is taken
    as a subfolder next to each photo rather than relative to the app's working directory."""
    src = Path(src)
    text = (opts.output_folder or "").strip() if opts.output_mode == "folder" else ""
    if not text:
        return src.parent
    folder = Path(os.path.expandvars(os.path.expanduser(text)))
    if not folder.is_absolute():
        folder = src.parent / folder
        if not folder.is_absolute():  # a drive-relative path such as 'C:out'
            folder = Path(os.path.abspath(folder))
    return folder


def output_suffix(src: Path, opts: Options) -> str:
    """The name suffix actually used for src. Next to the photo it is never empty, so a
    result can never take the name of an original ('photo.jpg' -> 'photo.png')."""
    suffix = clean_suffix(opts.suffix)
    if not suffix and _same_folder(output_folder_for(src, opts), Path(src).parent):
        suffix = DEFAULT_SUFFIX
    return suffix


@dataclass(frozen=True)
class _Name:
    folder: Path
    stem: str  # source stem, plus a tag when it shares its name with another photo
    suffix: str
    ext: str

    def path(self, n: int = 1) -> Path:
        count = f" ({n})" if n > 1 else ""
        # The count goes before the suffix, so the name still ends with it and is known as ours.
        return self.folder / f"{self.stem}{count}{self.suffix}{self.ext}"


def _natural_name(src: Path, opts: Options) -> _Name:
    return _Name(output_folder_for(src, opts), src.stem, output_suffix(src, opts), opts.output_extension())


def output_path_for(src: Path, opts: Options) -> Path:
    """The plain result name for one photo, before collisions are considered (see plan_outputs)."""
    src = Path(src)
    out = _natural_name(src, opts).path()
    try:
        same = out.resolve() == src.resolve()
    except OSError:
        same = False
    if same:  # never overwrite the original photo
        out = out.with_name(f"{src.stem}{DEFAULT_SUFFIX}{out.suffix}")
    return out


def mask_path_for(output: Path) -> Path:
    return output.with_name(output.stem + MASK_TAG + ".png")


def _folder_label(folder: Path) -> str:
    name = folder.name or folder.drive.rstrip(":\\/").replace("\\", "/").split("/")[-1]
    return clean_suffix(name).strip(" .") or "root"


def _tags(paths: list[Path]) -> list[str]:
    """Short, stable tags that tell photos with the same result name apart."""

    def ext(p: Path) -> str:
        return p.suffix.lstrip(".").lower()

    levels = (
        ext,  # IMG_1234.HEIC and IMG_1234.JPG
        lambda p: _folder_label(p.parent),  # Day1\IMG_0001.JPG and Day2\IMG_0001.JPG
        lambda p: f"{_folder_label(p.parent)}_{ext(p)}",
    )
    for level in levels:
        tags = [level(p) for p in paths]
        if all(tags) and len({t.lower() for t in tags}) == len(tags):
            return ["_" + t for t in tags]
    # Same name, type and folder name (e.g. on two drives): number them in path order.
    order = sorted(range(len(paths)), key=lambda i: _key(paths[i]))
    tags = [""] * len(paths)
    for rank, i in enumerate(order, 1):
        tags[i] = f"_{rank}"
    return tags


class _Listing:
    """What already exists in the output folders. For a batch each folder is read once;
    for a single photo a direct look-up is cheaper than listing a large folder."""

    def __init__(self, scan: bool = True) -> None:
        self._scan = scan
        self._folders: dict[str, dict[str, bool]] = {}

    def entry(self, path: Path) -> bool | None:
        """None when nothing is at path, else whether it is a regular file."""
        if not self._scan:
            try:
                return stat.S_ISREG(os.stat(path, follow_symlinks=False).st_mode)
            except FileNotFoundError:
                return None
            except OSError:
                return False  # something is there that cannot be inspected: leave it alone
        key = _key(path.parent)
        names = self._folders.get(key)
        if names is None:
            names = {}
            try:
                with os.scandir(path.parent) as it:
                    for e in it:
                        try:
                            names[e.name.lower()] = e.is_file(follow_symlinks=False)
                        except OSError:
                            names[e.name.lower()] = False
            except OSError:
                pass  # the folder does not exist yet (or cannot be listed): nothing to protect
            self._folders[key] = names
        return names.get(path.name.lower())


def _ours(path: Path, owned_suffix: str, listing: _Listing) -> bool:
    """True when path is free, or holds a file this app named (its name ends with the suffix).
    With an empty suffix nothing existing counts as ours, so it is never replaced."""
    entry = listing.entry(path)
    if entry is None:
        return True
    return bool(owned_suffix) and entry and path.stem.lower().endswith(owned_suffix.lower())


def _acceptable(out: Path, suffix: str, with_mask: bool, taken: set[str], listing: _Listing) -> bool:
    if _key(out) in taken or not _ours(out, suffix, listing):
        return False
    if with_mask:
        mask = mask_path_for(out)
        if _key(mask) in taken or not _ours(mask, suffix + MASK_TAG if suffix else "", listing):
            return False
    return True


def _pick(name: _Name, with_mask: bool, taken: set[str], listing: _Listing) -> Path:
    for n in range(1, 10_000):
        out = name.path(n)
        if _acceptable(out, name.suffix, with_mask, taken, listing):
            return out
    raise RuntimeError(f"No free file name for {name.stem}{name.suffix}{name.ext} in {name.folder}")


def plan_outputs(sources: list[Path], opts: Options, reserved: Iterable[Path] | None = None) -> dict[Path, Path]:
    """Map every source photo to a collision-free output path for this batch.

    - An output is never one of the sources, nor a path in `reserved`.
    - No two sources get the same output (or mask). Only photos whose names clash are told
      apart, by their file type or folder name, so the names stay the same from run to run
      for the same list. Pass the whole list, not just the photos about to be processed.
    - An existing file is replaced only when the app named it: the suffix is not empty and the
      name ends with it (for masks: with suffix + '_mask'). Otherwise a free name such as
      'photo (2).png' is used.

    The returned dict is keyed by the Path objects passed in.
    """
    given = [Path(s) for s in sources]
    by_key: dict[str, Path] = {}
    for s in given:
        by_key.setdefault(_key(s), s)
    taken = set(by_key) | {_key(Path(p)) for p in (reserved or ())}

    names = {k: _natural_name(s, opts) for k, s in by_key.items()}
    groups: dict[str, list[str]] = defaultdict(list)
    for k, name in names.items():
        groups[_key(name.path())].append(k)
    for members in groups.values():
        if len(members) > 1:
            for k, tag in zip(members, _tags([by_key[k] for k in members])):
                names[k] = replace(names[k], stem=names[k].stem + tag)

    listing = _Listing(scan=len(by_key) > 4)
    chosen: dict[str, Path] = {}
    for k in sorted(names):  # a fixed order, whatever the order of the list
        out = _pick(names[k], opts.save_mask, taken, listing)
        taken.add(_key(out))
        if opts.save_mask:
            taken.add(_key(mask_path_for(out)))
        chosen[k] = out
    return {s: chosen[_key(s)] for s in given}


def _still_free(out: Path, src: Path, opts: Options) -> Path:
    """Re-check a planned name just before writing: a file may have appeared since."""
    suffix = output_suffix(src, opts)
    listing = _Listing(scan=False)
    taken = {_key(src)}
    try:
        if out.resolve() == src.resolve():
            taken.add(_key(out))
    except OSError:
        pass
    if _acceptable(out, suffix, opts.save_mask, taken, listing):
        return out
    stem = out.stem
    if suffix and stem.lower().endswith(suffix.lower()):
        stem = stem[: len(stem) - len(suffix)]
    return _pick(_Name(out.parent, stem, suffix, out.suffix), opts.save_mask, taken, listing)


# ------------------------------------------------------------------ processing
def check_cancel(cancel: threading.Event | None) -> None:
    if cancel is not None and cancel.is_set():
        raise Cancelled()


def subject_box(alpha: np.ndarray, margin_pct: float) -> tuple[int, int, int, int] | None:
    """Bounding box (x0, y0, x1, y1) of the visible subject plus a margin, clamped to the image."""
    solid = alpha > 0.03
    rows = np.flatnonzero(solid.any(axis=1))
    cols = np.flatnonzero(solid.any(axis=0))
    if rows.size == 0 or cols.size == 0:
        return None
    y0, y1 = int(rows[0]), int(rows[-1]) + 1
    x0, x1 = int(cols[0]), int(cols[-1]) + 1
    mx = round((x1 - x0) * margin_pct / 100.0)
    my = round((y1 - y0) * margin_pct / 100.0)
    h, w = alpha.shape
    return max(0, x0 - mx), max(0, y0 - my), min(w, x1 + mx), min(h, y1 + my)


def process_file(
    engine,
    path: Path,
    opts: Options,
    cancel: threading.Event | None = None,
    report: Report | None = None,
    on_device: Callable[[str], None] | None = None,
    output: Path | None = None,
) -> FileResult:
    """Cut out one photo. `output` is the name chosen by plan_outputs for the whole batch;
    without it the photo is planned on its own. The result says where it was really saved.

    With a background image the file is checked before inference (BackgroundError, naming
    it, when it is missing or unreadable); after an optional crop to the subject it is
    fitted to the result's size and converted into the profile the result carries.
    """
    say = report or (lambda _t: None)
    path = Path(path)
    out = Path(output) if output is not None else plan_outputs([path], opts)[path]
    say("Loading photo")
    src = load_image(path)
    check_cancel(cancel)
    # Before any inference: an unwritable destination or an unusable background image
    # should cost seconds, not a model run.
    check_writable(out.parent)
    background_file = check_background(opts.background_image) if opts.background == "image" else None

    icc = src.icc_profile
    if not opts.keep_icc and icc:
        converted = to_srgb(src.rgb, icc)
        if converted is not None:  # else keep the profile: dropping it would shift the colours
            src.rgb, icc = converted, None

    foreground, alpha = engine.cutout(src.rgb, opts, cancel=cancel, report=say, on_device=on_device)
    check_cancel(cancel)

    # The engine already applied the edge shift/softening before estimating edge colours.
    if src.alpha is not None:
        # Whatever was transparent in the photo stays transparent.
        alpha = np.multiply(alpha, src.alpha, dtype=np.float32)
        foreground = restore_colours(foreground, src)
    if opts.crop_to_subject:
        box = subject_box(alpha, opts.crop_margin)
        if box is not None:
            x0, y0, x1, y1 = box
            alpha = alpha[y0:y1, x0:x1]
            foreground = foreground[y0:y1, x0:x1]

    background = None
    if background_file is not None:
        say("Adding background")
        # Fitted to the (cropped) result, in the colour profile the file will carry.
        height, width = alpha.shape
        background = prepare_background(
            background_file,
            (width, height),
            opts.background_fit,
            opts.background_blur,
            opts.background_color,
            icc,
        )
        check_cancel(cancel)
    elif opts.background == "color":
        # The colour is picked in sRGB; express it in the profile the file will carry.
        background = srgb_color_to_profile(hex_to_rgb(opts.background_color), icc)
    say("Saving")
    image = compose(foreground, alpha, background)
    del foreground, background
    out = _still_free(out, path, opts)
    save_image(image, out, icc_profile=icc, dpi=src.dpi, jpeg_quality=opts.jpeg_quality)

    mask_file = None
    if opts.save_mask:
        mask_img = Image.fromarray(np.rint(alpha * 255.0).astype(np.uint8), "L")
        mask_file = save_image(mask_img, mask_path_for(out), dpi=src.dpi)
    return FileResult(output=out, mask=mask_file, size=(image.width, image.height))

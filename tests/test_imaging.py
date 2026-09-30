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

"""Tests for output naming, colour profiles, transparency, backgrounds and loading limits.

pytest is not needed: run with the project's Python,

    .venv\\Scripts\\python.exe tests\\test_imaging.py

No model is loaded; process_file runs with a stand-in engine.
"""

from __future__ import annotations

import contextlib
import io
import os
import random
import struct
import sys
import tempfile
import tracemalloc
import traceback
import zlib
from pathlib import Path

import numpy as np
from PIL import Image, ImageCms, JpegImagePlugin

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bgeditor import imageio, models, options  # noqa: E402
from bgeditor.imageio import (  # noqa: E402
    BackgroundError,
    SaveError,
    background_preview,
    check_background,
    compose,
    is_supported,
    load_image,
    prepare_background,
    profile_space,
    save_image,
)
from bgeditor.options import Options, matching_preset, presets  # noqa: E402
from bgeditor.pipeline import (  # noqa: E402
    mask_path_for,
    output_folder_for,
    output_path_for,
    plan_outputs,
    process_file,
)


# ------------------------------------------------------------------ helpers
class FakeEngine:
    """Stands in for the model: returns the photo as foreground and a chosen alpha."""

    def __init__(self, alpha=None) -> None:
        self.calls = 0
        self._alpha = alpha

    def cutout(self, rgb, opts, cancel=None, report=None, on_device=None):
        self.calls += 1
        fg = np.asarray(rgb, dtype=np.float32) / 255.0
        h, w = fg.shape[:2]
        alpha = np.ones((h, w), np.float32) if self._alpha is None else self._alpha(h, w)
        return fg, alpha


def touch(path: Path, data: bytes = b"original") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def names(plan: dict[Path, Path]) -> dict[str, str]:
    return {src.name if src.parent.name == "" else f"{src.parent.name}/{src.name}": out.name for src, out in plan.items()}


def _s15(v: float) -> bytes:
    return struct.pack(">i", round(v * 65536))


def make_icc(kind: str) -> bytes:
    """A minimal ICC v2 display profile: Adobe RGB (1998) primaries, or grey with gamma 2.2."""

    def desc(text: str) -> bytes:
        a = text.encode("ascii") + b"\0"
        return b"desc" + b"\0" * 4 + struct.pack(">I", len(a)) + a + struct.pack(">IIHB", 0, 0, 0, 0) + b"\0" * 67

    def xyz(x: float, y: float, z: float) -> bytes:
        return b"XYZ " + b"\0" * 4 + _s15(x) + _s15(y) + _s15(z)

    def curv(g: float) -> bytes:
        return b"curv" + b"\0" * 4 + struct.pack(">IH", 1, round(g * 256)) + b"\0\0"

    tags = [
        (b"desc", desc("Test Adobe RGB" if kind == "rgb" else "Test Gray 2.2")),
        (b"cprt", b"text" + b"\0" * 4 + b"none\0"),
        (b"wtpt", xyz(0.9642, 1.0, 0.8249)),
    ]
    if kind == "rgb":
        tags += [
            (b"rXYZ", xyz(0.60974, 0.31111, 0.01947)),
            (b"gXYZ", xyz(0.20528, 0.62567, 0.06087)),
            (b"bXYZ", xyz(0.14919, 0.06322, 0.74457)),
        ]
        tags += [(sig, curv(2.19921875)) for sig in (b"rTRC", b"gTRC", b"bTRC")]
        space = b"RGB "
    else:
        tags += [(b"kTRC", curv(2.2))]
        space = b"GRAY"
    start = 128 + 4 + 12 * len(tags)
    table, body = struct.pack(">I", len(tags)), b""
    for sig, data in tags:
        body += b"\0" * (-len(body) % 4)
        table += sig + struct.pack(">II", start + len(body), len(data))
        body += data
    body += b"\0" * (-len(body) % 4)
    size = 128 + len(table) + len(body)
    header = (
        struct.pack(">I", size) + b"lcms" + bytes([2, 0x10, 0, 0]) + b"mntr" + space + b"XYZ "
        + b"\0" * 12 + b"acsp" + b"MSFT" + b"\0" * 20 + struct.pack(">I", 0)
        + _s15(0.9642) + _s15(1.0) + _s15(0.8249) + b"\0" * 48
    )
    assert len(header) == 128
    return header + table + body


def png_header_only(width: int, height: int) -> bytes:
    """A PNG that declares a size but holds no pixels: enough for Image.open."""

    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IEND", b"")


def srgb_to_adobe(rgb: tuple[int, int, int]) -> tuple[int, int, int]:
    prof = ImageCms.ImageCmsProfile(io.BytesIO(make_icc("rgb")))
    return ImageCms.profileToProfile(
        Image.new("RGB", (1, 1), rgb),
        ImageCms.createProfile("sRGB"),
        prof,
        renderingIntent=ImageCms.Intent.RELATIVE_COLORIMETRIC,
        outputMode="RGB",
    ).getpixel((0, 0))


def close(a, b, tol: int = 1) -> bool:
    return all(abs(int(x) - int(y)) <= tol for x, y in zip(a, b))


@contextlib.contextmanager
def patched(owner, name: str, value):
    """Replace owner.name for the duration of a with-block (the attribute may not exist yet)."""
    missing = object()
    old = getattr(owner, name, missing)
    setattr(owner, name, value)
    try:
        yield
    finally:
        if old is missing:
            delattr(owner, name)
        else:
            setattr(owner, name, old)


RED, GREEN, BLUE, MAGENTA = (255, 0, 0), (0, 255, 0), (0, 0, 255), (255, 0, 255)


def bands_image(path: Path, size=(300, 100), colours=(RED, GREEN, BLUE), **save) -> Path:
    """Vertical bands of equal width, left to right."""
    w, h = size
    arr = np.zeros((h, w, 3), np.uint8)
    edges = np.linspace(0, w, len(colours) + 1).round().astype(int)
    for c, x0, x1 in zip(colours, edges[:-1], edges[1:]):
        arr[:, x0:x1] = c
    Image.fromarray(arr, "RGB").save(path, **save)
    return path


def px(bg: np.ndarray, x: int, y: int) -> tuple[int, int, int]:
    """One pixel of a prepared background (float 0-1) as 0-255 integers."""
    return tuple(int(round(float(v) * 255.0)) for v in bg[y, x])


def background_opts(bg: Path, **kw) -> Options:
    return Options(background="image", background_image=str(bg), **kw)


# ------------------------------------------------------------------ naming
def test_empty_suffix_never_replaces_an_original(tmp: Path) -> None:
    # portrait.jpg -> portrait.png used to destroy the other original.
    jpg = touch(tmp / "portrait.jpg")
    png = touch(tmp / "portrait.png")
    opts = Options(suffix="", output_format="png")
    plan = plan_outputs([jpg], opts)
    assert plan[jpg].name == "portrait_nobg.png", plan
    assert output_path_for(jpg, opts).name == "portrait_nobg.png"
    # Both in the batch: never a source, never the same output.
    plan = plan_outputs([jpg, png], opts)
    assert set(plan.values()).isdisjoint({jpg, png})
    assert len(set(plan.values())) == 2


def test_empty_suffix_in_other_folder_picks_free_name(tmp: Path) -> None:
    src = touch(tmp / "in" / "a.jpg")
    out_dir = tmp / "out"
    touch(out_dir / "a.png", b"someone else's file")
    opts = Options(suffix="", output_mode="folder", output_folder=str(out_dir))
    assert plan_outputs([src], opts)[src] == out_dir / "a (2).png"
    # A free name is used as is.
    other = touch(tmp / "in" / "b.jpg")
    assert plan_outputs([other], opts)[other] == out_dir / "b.png"


def test_large_batch_sees_existing_files_too(tmp: Path) -> None:
    # Bigger batches read each output folder once instead of looking up every name.
    srcs = [touch(tmp / "in" / f"p{i}.jpg") for i in range(8)]
    out_dir = tmp / "out"
    touch(out_dir / "P3.PNG", b"not ours")  # other case, still the same file on Windows
    (out_dir / "p5.png").mkdir()  # a folder in the way
    opts = Options(suffix="", output_mode="folder", output_folder=str(out_dir))
    plan = plan_outputs(srcs, opts)
    assert plan[srcs[3]].name == "p3 (2).png" and plan[srcs[5]].name == "p5 (2).png", names(plan)
    assert plan[srcs[0]].name == "p0.png"
    for i in (3, 5):  # the single-photo look-up agrees
        assert plan_outputs([srcs[i]], opts)[srcs[i]] == plan[srcs[i]]


def test_same_stem_different_extension(tmp: Path) -> None:
    heic = touch(tmp / "IMG_1234.HEIC")
    jpg = touch(tmp / "IMG_1234.JPG")
    plan = plan_outputs([heic, jpg], Options())
    assert plan[heic].name == "IMG_1234_heic_nobg.png", names(plan)
    assert plan[jpg].name == "IMG_1234_jpg_nobg.png", names(plan)


def test_two_folders_into_one_output_folder(tmp: Path) -> None:
    a = touch(tmp / "Day1" / "IMG_0001.JPG")
    b = touch(tmp / "Day2" / "IMG_0001.JPG")
    c = touch(tmp / "Day2" / "IMG_0002.JPG")
    opts = Options(output_mode="folder", output_folder=str(tmp / "Cutouts"))
    plan = plan_outputs([a, b, c], opts)
    assert plan[a] == tmp / "Cutouts" / "IMG_0001_Day1_nobg.png", plan
    assert plan[b] == tmp / "Cutouts" / "IMG_0001_Day2_nobg.png", plan
    assert plan[c] == tmp / "Cutouts" / "IMG_0002_nobg.png", plan  # no clash, no tag
    # Same name, type and folder name on different parents: still distinct.
    x = touch(tmp / "X" / "Shoot" / "p.jpg")
    y = touch(tmp / "Y" / "Shoot" / "p.jpg")
    plan = plan_outputs([x, y], opts)
    assert plan[x] != plan[y]
    assert all(out.stem.endswith("_nobg") for out in plan.values())


def test_plan_is_stable_across_runs_and_order(tmp: Path) -> None:
    srcs = [
        touch(tmp / "d1" / "p.jpg"),
        touch(tmp / "d2" / "p.jpg"),
        touch(tmp / "d2" / "p.png"),
        touch(tmp / "d1" / "q.tif"),
    ]
    opts = Options(output_mode="folder", output_folder=str(tmp / "out"), save_mask=True)
    first = plan_outputs(srcs, opts)
    for out in first.values():  # the results of the first run now exist
        touch(out, b"result")
        touch(mask_path_for(out), b"mask")
    shuffled = list(srcs)
    random.Random(4).shuffle(shuffled)
    second = plan_outputs(shuffled, opts)
    assert first == second, (first, second)


def test_own_previous_result_is_replaced(tmp: Path) -> None:
    src = touch(tmp / "a.jpg")
    touch(tmp / "a_nobg.png", b"earlier result")
    assert plan_outputs([src], Options())[src] == tmp / "a_nobg.png"


def test_output_is_never_a_source(tmp: Path) -> None:
    a = touch(tmp / "a.jpg")
    earlier = touch(tmp / "a_nobg.png")  # an earlier result, added on purpose
    plan = plan_outputs([a, earlier], Options())
    assert plan[a].name == "a (2)_nobg.png", names(plan)
    assert plan[earlier].name == "a_nobg_nobg.png", names(plan)


def test_reserved_paths_are_skipped(tmp: Path) -> None:
    a = touch(tmp / "a.jpg")
    plan = plan_outputs([a], Options(), reserved={tmp / "A_NOBG.png"})
    assert plan[a].name == "a (2)_nobg.png", names(plan)


def test_mask_naming(tmp: Path) -> None:
    assert mask_path_for(tmp / "a_nobg.png").name == "a_nobg_mask.png"
    # A mask may never land on another photo of the batch.
    a = touch(tmp / "a.jpg")
    photo = touch(tmp / "a_nobg_mask.png")
    plan = plan_outputs([a, photo], Options(save_mask=True))
    assert mask_path_for(plan[a]) != photo
    assert plan[a].name == "a (2)_nobg.png", names(plan)
    # With an empty suffix nothing existing is ours: an unrelated 'b_mask.png' moves the pair.
    out_dir = tmp / "out"
    touch(out_dir / "b_mask.png", b"not ours")
    b = touch(tmp / "b.jpg")
    opts = Options(suffix="", output_mode="folder", output_folder=str(out_dir), save_mask=True)
    out = plan_outputs([b], opts)[b]
    assert out.name == "b (2).png" and mask_path_for(out).name == "b (2)_mask.png", out
    # Without masks the plain name is fine.
    assert plan_outputs([b], Options(suffix="", output_mode="folder", output_folder=str(out_dir)))[b].name == "b.png"


def test_suffix_cannot_create_folders(tmp: Path) -> None:
    a = touch(tmp / "a.jpg")
    out = plan_outputs([a], Options(suffix="\\sub/x:y*"))[a]
    assert out.parent == tmp and out.name == "asubxy.png", out


def test_relative_and_variable_output_folder(tmp: Path) -> None:
    a = touch(tmp / "shoot" / "a.jpg")
    assert output_folder_for(a, Options(output_mode="folder", output_folder="Results")) == tmp / "shoot" / "Results"
    os.environ["BGR_TEST_DIR"] = str(tmp / "env")
    assert output_folder_for(a, Options(output_mode="folder", output_folder="%BGR_TEST_DIR%\\x")) == tmp / "env" / "x"
    home = output_folder_for(a, Options(output_mode="folder", output_folder="~\\cutouts"))
    assert home == Path.home() / "cutouts", home
    assert output_folder_for(a, Options(output_mode="folder", output_folder="")) == a.parent


# ------------------------------------------------------------------ processing
def test_process_file_default_output_keeps_originals(tmp: Path) -> None:
    Image.new("RGB", (8, 6), (10, 200, 30)).save(tmp / "p.jpg")
    touch(tmp / "p.png", b"original png")
    res = process_file(FakeEngine(), tmp / "p.jpg", Options(suffix=""))
    assert res.output == tmp / "p_nobg.png", res
    assert (tmp / "p.png").read_bytes() == b"original png"
    assert not list(tmp.glob("*.bgr-tmp")) and not list(tmp.glob(".bgeditor-probe*"))


def test_process_file_rechecks_before_saving(tmp: Path) -> None:
    Image.new("RGB", (8, 6), (10, 200, 30)).save(tmp / "p.jpg")
    out_dir = tmp / "out"
    opts = Options(suffix="", output_mode="folder", output_folder=str(out_dir), save_mask=True)
    planned = plan_outputs([tmp / "p.jpg"], opts)[tmp / "p.jpg"]
    assert planned == out_dir / "p.png"
    touch(planned, b"appeared meanwhile")  # someone else's file shows up during the run
    res = process_file(FakeEngine(), tmp / "p.jpg", opts, output=planned)
    assert planned.read_bytes() == b"appeared meanwhile"
    assert res.output == out_dir / "p (2).png" and res.mask == out_dir / "p (2)_mask.png", res


def test_unwritable_destination_fails_before_inference(tmp: Path) -> None:
    Image.new("RGB", (8, 6)).save(tmp / "p.jpg")
    touch(tmp / "blocker", b"a file, not a folder")
    engine = FakeEngine()
    opts = Options(output_mode="folder", output_folder=str(tmp / "blocker" / "sub"))
    try:
        process_file(engine, tmp / "p.jpg", opts)
    except SaveError as exc:
        assert str(tmp / "blocker" / "sub") in str(exc), exc
        assert ".tmp" not in str(exc) and ".bgr-tmp" not in str(exc)
    else:
        raise AssertionError("expected SaveError")
    assert engine.calls == 0, "inference ran before the destination was checked"


def test_temp_file_has_no_image_extension(tmp: Path) -> None:
    seen: list[str] = []
    original = Image.Image.save

    def spy(self, fp, *args, **kwargs):
        seen.append(str(fp))
        return original(self, fp, *args, **kwargs)

    Image.Image.save = spy
    try:
        save_image(Image.new("RGB", (2, 2)), tmp / "r_nobg.png")
    finally:
        Image.Image.save = original
    assert seen and seen[0].endswith(".bgr-tmp") and not is_supported(Path(seen[0])), seen
    assert [p.name for p in tmp.iterdir()] == ["r_nobg.png"]


# ------------------------------------------------------------------ colour profiles
def test_gray_profile_is_never_embedded_in_rgb(tmp: Path) -> None:
    gray = make_icc("gray")
    Image.new("L", (6, 4), 100).save(tmp / "bw.jpg", icc_profile=gray, quality=95)
    src = load_image(tmp / "bw.jpg")
    assert src.rgb.mode == "RGB" and profile_space(src.icc_profile) == "RGB", profile_space(src.icc_profile)
    for fmt in ("png", "jpg", "tif"):
        opts = Options(output_format=fmt, background="color", suffix=f"_{fmt}")
        res = process_file(FakeEngine(), tmp / "bw.jpg", opts)
        with Image.open(res.output) as im:
            assert profile_space(im.info.get("icc_profile")) == "RGB", (fmt, im.info.get("icc_profile"))
    # A grey profile on an RGB file does not describe it: drop it.
    rgb, icc = imageio._to_rgb8(Image.new("RGB", (2, 2)), gray)
    assert icc is None


def test_keep_icc_off_converts_to_srgb(tmp: Path) -> None:
    adobe = make_icc("rgb")
    colour = (100, 150, 200)
    Image.new("RGB", (6, 4), colour).save(tmp / "wide.png", icc_profile=adobe)
    expected = ImageCms.profileToProfile(
        Image.new("RGB", (1, 1), colour),
        ImageCms.ImageCmsProfile(io.BytesIO(adobe)),
        ImageCms.createProfile("sRGB"),
        outputMode="RGB",
    ).getpixel((0, 0))
    assert not close(expected, colour, 3)  # the test profile really is different from sRGB
    res = process_file(FakeEngine(), tmp / "wide.png", Options(keep_icc=False))
    with Image.open(res.output) as im:
        assert "icc_profile" not in im.info or not im.info["icc_profile"]
        assert close(im.getpixel((2, 2))[:3], expected), (im.getpixel((2, 2)), expected)
    # Kept: pixels and profile unchanged.
    res = process_file(FakeEngine(), tmp / "wide.png", Options(keep_icc=True, suffix="_kept"))
    with Image.open(res.output) as im:
        assert im.info.get("icc_profile") == adobe
        assert im.getpixel((2, 2))[:3] == colour


def test_background_colour_is_converted_into_the_photo_profile(tmp: Path) -> None:
    adobe = make_icc("rgb")
    Image.new("RGB", (8, 4), (50, 60, 70)).save(tmp / "wide.png", icc_profile=adobe)

    def left_half_background(h, w):
        a = np.ones((h, w), np.float32)
        a[:, : w // 2] = 0.0
        return a

    opts = Options(background="color", background_color="#00FF00", output_format="png")
    res = process_file(FakeEngine(left_half_background), tmp / "wide.png", opts)
    with Image.open(res.output) as im:
        assert im.info.get("icc_profile") == adobe
        assert close(im.getpixel((0, 0)), srgb_to_adobe((0, 255, 0))), im.getpixel((0, 0))
        assert im.getpixel((7, 0)) == (50, 60, 70)
    # Untagged photo: the colour is used as picked.
    Image.new("RGB", (8, 4), (50, 60, 70)).save(tmp / "plain.png")
    res = process_file(FakeEngine(left_half_background), tmp / "plain.png", opts)
    with Image.open(res.output) as im:
        assert im.getpixel((0, 0)) == (0, 255, 0)


def test_cmyk_without_profile_becomes_untagged_rgb(tmp: Path) -> None:
    Image.new("CMYK", (4, 4), (0, 0, 0, 0)).save(tmp / "c.jpg")
    src = load_image(tmp / "c.jpg")
    assert src.rgb.mode == "RGB" and src.icc_profile is None


# ------------------------------------------------------------------ transparency and bit depth
def test_source_alpha_is_kept(tmp: Path) -> None:
    arr = np.zeros((4, 6, 4), np.uint8)
    arr[:, :2] = (255, 0, 0, 0)  # hidden red under full transparency
    arr[:, 2:4] = (200, 50, 10, 128)  # semi-transparent
    arr[:, 4:] = (20, 120, 220, 255)  # opaque
    Image.fromarray(arr, "RGBA").save(tmp / "cut.png")
    src = load_image(tmp / "cut.png")
    assert src.alpha is not None and src.alpha.dtype == np.float32
    assert np.allclose(src.alpha[0], np.array([0, 0, 128, 128, 255, 255]) / 255.0)
    model_input = np.asarray(src.rgb)
    assert tuple(model_input[0, 0]) == (128, 128, 128), "the model must not see the hidden colour"
    assert tuple(model_input[0, 5]) == (20, 120, 220)
    res = process_file(FakeEngine(), tmp / "cut.png", Options(suffix="_again", save_mask=True))
    with Image.open(res.output) as im:
        out = np.asarray(im)
    assert tuple(out[0, 0]) == (0, 0, 0, 0)
    assert out[0, 2, 3] == 128 and close(out[0, 2, :3], (200, 50, 10), 2), out[0, 2]
    assert tuple(out[0, 5]) == (20, 120, 220, 255)
    with Image.open(res.mask) as m:
        assert list(np.asarray(m)[0]) == [0, 0, 128, 128, 255, 255]


def test_opaque_alpha_is_ignored_and_palette_transparency_is_read(tmp: Path) -> None:
    Image.new("RGBA", (3, 3), (1, 2, 3, 255)).save(tmp / "opaque.png")
    assert load_image(tmp / "opaque.png").alpha is None
    p = Image.new("P", (4, 1))
    p.putpalette([0, 0, 0, 255, 255, 255] + [0] * 762)
    p.putpixel((0, 0), 1)
    p.info["transparency"] = 0
    p.save(tmp / "pal.png", transparency=0)
    src = load_image(tmp / "pal.png")
    assert src.alpha is not None and list(src.alpha[0]) == [1.0, 0.0, 0.0, 0.0]
    la = Image.merge("LA", (Image.new("L", (2, 1), 90), Image.new("L", (2, 1), 0)))
    la.save(tmp / "la.png")
    assert load_image(tmp / "la.png").alpha is not None


def test_16_bit_grey_always_uses_full_range(tmp: Path) -> None:
    Image.fromarray(np.full((4, 4), 200, np.uint16)).save(tmp / "dark16.png")
    src = load_image(tmp / "dark16.png")
    assert src.rgb.getpixel((0, 0)) == (1, 1, 1), src.rgb.getpixel((0, 0))
    Image.fromarray(np.full((2, 2), 65535, np.uint16)).save(tmp / "white16.png")
    assert load_image(tmp / "white16.png").rgb.getpixel((0, 0)) == (255, 255, 255)


def test_too_large_is_refused_before_decoding(tmp: Path) -> None:
    for side in (13_000, 30_000):  # just over the limit, and far over Pillow's own limit
        path = tmp / f"huge{side}.png"
        path.write_bytes(png_header_only(side, side))
        try:
            load_image(path)
        except ValueError as exc:
            assert "too large" in str(exc).lower() and "150 MP" in str(exc), exc
        else:
            raise AssertionError(f"{side}x{side} was accepted")


def test_compose_matches_reference() -> None:
    rng = np.random.default_rng(1)
    fg = rng.uniform(-0.1, 1.1, (5, 7, 3)).astype(np.float32)
    alpha = rng.uniform(-0.1, 1.1, (5, 7)).astype(np.float32)
    alpha[0, 0] = 0.0
    a = np.clip(alpha, 0.0, 1.0)[..., None]
    f = np.clip(fg, 0.0, 1.0)
    ref = np.empty((5, 7, 4), np.uint8)
    ref[..., :3] = np.rint(f * 255.0)
    ref[..., 3] = np.rint(a[..., 0] * 255.0)
    ref[ref[..., 3] == 0, :3] = 0
    assert np.array_equal(np.asarray(compose(fg, alpha, None)), ref)
    bg = np.asarray((30, 144, 255), np.float32) / 255.0
    ref_rgb = np.rint((f * a + bg * (1.0 - a)) * 255.0).astype(np.uint8)
    got = np.asarray(compose(fg, alpha, (30, 144, 255))).astype(int)
    assert np.abs(got - ref_rgb.astype(int)).max() <= 1


# ------------------------------------------------------------------ options
def test_options_from_dict_validates() -> None:
    o = Options.from_dict(
        {
            "model": "birefnet-old",
            "device": "tpu",
            "background": "blur",
            "output_format": "gif",
            "output_mode": "cloud",
            "background_color": "white",
            "edge_shift": 5000,
            "edge_soften": "inf",
            "refine_band": "nan",
            "refine_resolution": 3000,
            "jpeg_quality": 7,
            "fg_threshold": 999,
            "crop_margin": -4,
            "suffix": "a/b",
        }
    )
    d = Options()
    assert (o.model, o.device, o.background, o.output_format, o.output_mode) == (
        d.model, d.device, d.background, d.output_format, d.output_mode
    )
    assert o.background_color == d.background_color
    assert o.edge_shift == 30 and o.edge_soften == d.edge_soften and o.refine_band == d.refine_band
    assert o.refine_resolution == 3072 and o.jpeg_quality == 50 and o.fg_threshold == 255
    assert o.crop_margin == 0.0 and o.suffix == "ab"
    assert Options.from_dict({"background_color": "#1e8"}).background_color == "#11EE88"
    good = Options(model="birefnet-lite", background="color", background_color="#1E88E5", output_format="jpg")
    assert Options.from_dict(good.to_dict()) == good


def test_options_background_fields_round_trip_and_validate() -> None:
    good = Options(
        background="image",
        background_image="D:\\Backgrounds\\beach.jpg",
        background_fit="contain",
        background_blur=12.5,
        model_updates="ask",
        output_format="jpg",
    )
    assert Options.from_dict(good.to_dict()) == good
    d = Options()
    assert (d.background_image, d.background_fit, d.background_blur, d.model_updates) == ("", "cover", 0.0, "auto")
    o = Options.from_dict(
        {
            "background": "video",
            "background_fit": "zoom",
            "background_blur": 99,
            "model_updates": "never",
            "background_image": "  C:\\bg.png  ",
        }
    )
    assert o.background == d.background and o.background_fit == "cover" and o.model_updates == "auto"
    assert o.background_blur == 50.0 and isinstance(o.background_blur, float)
    assert o.background_image == "C:\\bg.png"
    assert Options.from_dict({"background_blur": -3}).background_blur == 0.0
    assert Options.from_dict({"background_blur": "nan"}).background_blur == 0.0
    assert Options.from_dict({"background_blur": "7"}).background_blur == 7.0
    assert Options.from_dict({"background_image": "a\x00b"}).background_image == ""
    for fit in ("cover", "contain", "stretch"):
        assert Options.from_dict({"background_fit": fit}).background_fit == fit
    for mode in ("auto", "ask", "off"):
        assert Options.from_dict({"model_updates": mode}).model_updates == mode
    assert Options.from_dict({"background": "image"}).background == "image"
    # JPEG is fine whenever the result is opaque.
    assert Options(background="image", output_format="jpg").output_extension() == ".jpg"
    assert Options(background="color", output_format="jpg").output_extension() == ".jpg"
    assert Options(background="transparent", output_format="jpg").output_extension() == ".png"
    # The model is checked against the models registered at that moment, not a fixed list.
    with patched(models, "model_keys", lambda: ["birefnet-matting", "birefnet-lite", "birefnet-v2-test"]):
        assert Options.from_dict({"model": "birefnet-v2-test"}).model == "birefnet-v2-test"
        assert Options.from_dict({"model": "birefnet-portrait"}).model == d.model
        assert options.MODEL_KEYS == ("birefnet-matting", "birefnet-lite", "birefnet-v2-test")
    with patched(models, "model_keys", lambda: ["birefnet-matting", "birefnet-lite"]):
        assert Options.from_dict({"model": "birefnet-v2-test"}).model == d.model


def test_presets_follow_preferred_segmenter() -> None:
    with patched(models, "preferred_segmenter", lambda: "birefnet-v2-test"):
        p = presets()
        assert p["best"]["model"] == p["balanced"]["model"] == "birefnet-v2-test", p
        assert p["fast"]["model"] == "birefnet-lite"
        assert p["best"]["refine_hair"] and not p["balanced"]["refine_hair"]
        assert options.PRESETS == p  # the compatibility attribute follows as well
        assert matching_preset(Options(model="birefnet-v2-test", refine_hair=True)) == "best"
        assert matching_preset(Options(model="birefnet-v2-test", refine_hair=False)) == "balanced"
        assert matching_preset(Options(model="birefnet-matting")) is None
    with patched(models, "preferred_segmenter", lambda: "birefnet-matting"):
        assert presets()["best"]["model"] == "birefnet-matting"
        assert matching_preset(Options()) == "best"
        assert matching_preset(Options(model="birefnet-lite", refine_hair=False)) == "fast"


# ------------------------------------------------------------------ background images
def left_half_background(h: int, w: int) -> np.ndarray:
    a = np.ones((h, w), np.float32)
    a[:, : w // 2] = 0.0
    return a


def test_background_cover_geometry(tmp: Path) -> None:
    wide = bands_image(tmp / "wide.png")  # 300 x 100: red | green | blue
    out = prepare_background(wide, (100, 100), "cover", 0, "#FF00FF", None)
    assert out.shape == (100, 100, 3) and out.dtype == np.float32, (out.shape, out.dtype)
    assert 0.0 <= out.min() and out.max() <= 1.0
    # Scaled to fill the height, the centre third is shown: all green, no bars.
    assert np.array_equal(np.rint(out * 255).astype(np.uint8), np.broadcast_to(np.uint8(GREEN), out.shape))
    # Tall into wide: scaled to fill the width, top and bottom are cropped.
    tall = bands_image(tmp / "tall.png", (100, 300))
    out = prepare_background(tall, (200, 100), "cover", 0, "#FF00FF", None)
    assert out.shape == (100, 200, 3)
    assert close(px(out, 10, 0), RED, 2) and close(px(out, 100, 50), GREEN, 2) and close(px(out, 190, 99), BLUE, 2)


def test_background_contain_geometry(tmp: Path) -> None:
    wide = bands_image(tmp / "wide.png")
    out = prepare_background(wide, (100, 100), "contain", 0, "#FF00FF", None)
    assert out.shape == (100, 100, 3)
    # 300 x 100 fits as 100 x 33, centred: rows 33-65 hold the image, the rest is padding.
    for y in (0, 32, 66, 99):
        assert px(out, 50, y) == MAGENTA, (y, px(out, 50, y))
    for y in (33, 49, 65):
        assert close(px(out, 5, y), RED, 2) and close(px(out, 50, y), GREEN, 2) and close(px(out, 95, y), BLUE, 2), y


def test_background_stretch_geometry(tmp: Path) -> None:
    wide = bands_image(tmp / "wide.png")
    out = prepare_background(wide, (90, 120), "stretch", 0, "#FF00FF", None)
    assert out.shape == (120, 90, 3)
    assert close(px(out, 10, 60), RED, 2) and close(px(out, 45, 0), GREEN, 2) and close(px(out, 80, 119), BLUE, 2)
    assert not any(px(out, x, y) == MAGENTA for x in (0, 89) for y in (0, 119))  # no padding


def test_background_blur(tmp: Path) -> None:
    arr = np.zeros((50, 200, 3), np.uint8)
    arr[:, 100:] = 255
    Image.fromarray(arr, "RGB").save(tmp / "step.png")
    sharp = prepare_background(tmp / "step.png", (200, 50), "stretch", 0, "#FFFFFF", None)
    assert np.array_equal(sharp[25, :, 0], arr[25, :, 0] / 255.0)
    soft = prepare_background(tmp / "step.png", (200, 50), "stretch", 5, "#FFFFFF", None)
    row = soft[25, :, 0]
    assert row[70] < 0.01 and row[130] > 0.99, (row[70], row[130])  # far from the edge: unchanged
    width = int(np.count_nonzero((row > 0.16) & (row < 0.84)))  # about 2 sigma for a Gaussian
    assert 7 <= width <= 13, width
    # Out-of-range values are clamped rather than refused.
    huge = prepare_background(tmp / "step.png", (200, 50), "stretch", 1000, "#FFFFFF", None)
    assert np.array_equal(huge, prepare_background(tmp / "step.png", (200, 50), "stretch", 50, "#FFFFFF", None))
    assert np.array_equal(prepare_background(tmp / "step.png", (200, 50), "stretch", float("nan"), "#FFFFFF", None), sharp)
    # The border does not darken: a plain image stays plain.
    Image.new("RGB", (64, 48), (123, 45, 67)).save(tmp / "plain.png")
    flat = prepare_background(tmp / "plain.png", (80, 60), "cover", 20, "#FFFFFF", None)
    assert np.array_equal(np.rint(flat * 255), np.broadcast_to(np.float32((123, 45, 67)), flat.shape))
    # 'cover' blurs with the real neighbours of the shown part, which were cropped away.
    wide = bands_image(tmp / "wide.png")
    out = prepare_background(wide, (100, 100), "cover", 4, "#FF00FF", None)
    assert px(out, 0, 50)[0] > 20 and px(out, 99, 50)[2] > 20, (px(out, 0, 50), px(out, 99, 50))
    assert close(px(out, 50, 50), GREEN, 1)


def test_background_exif_orientation_and_reduced_decoding(tmp: Path) -> None:
    stored = np.zeros((800, 1600, 3), np.uint8)
    stored[:, :800] = RED
    stored[:, 800:] = BLUE
    im = Image.fromarray(stored, "RGB")
    exif = im.getexif()
    exif[0x0112] = 6  # shown turned 90 degrees clockwise: the stored left side is on top
    im.save(tmp / "turned.jpg", exif=exif.tobytes(), quality=95)
    decoded: list[tuple[int, int]] = []
    original = JpegImagePlugin.JpegImageFile.draft

    def spy(self, mode, size):
        result = original(self, mode, size)
        decoded.append(self.size)
        return result

    with patched(JpegImagePlugin.JpegImageFile, "draft", spy):
        out = prepare_background(tmp / "turned.jpg", (100, 200), "cover", 0, "#FFFFFF", None)
    assert out.shape == (200, 100, 3)
    assert close(px(out, 50, 20), RED, 10) and close(px(out, 50, 180), BLUE, 10), (px(out, 50, 20), px(out, 50, 180))
    # Decoded at 1/8 scale: 200 x 100 as stored, exactly the 100 x 200 needed once turned.
    assert decoded == [(200, 100)], decoded
    # The preview turns it the same way.
    prev = background_preview(tmp / "turned.jpg", (30, 60), "stretch", 0, "#FFFFFF")
    assert prev.size == (30, 60) and prev.mode == "RGB"
    assert close(prev.getpixel((15, 5)), RED, 10) and close(prev.getpixel((15, 55)), BLUE, 10)


def test_background_profile_is_converted(tmp: Path) -> None:
    adobe = make_icc("rgb")
    colour = (100, 150, 200)
    Image.new("RGB", (40, 30), colour).save(tmp / "wide_bg.png", icc_profile=adobe)
    Image.new("RGB", (40, 30), colour).save(tmp / "plain_bg.png")
    in_srgb = ImageCms.profileToProfile(
        Image.new("RGB", (1, 1), colour),
        ImageCms.ImageCmsProfile(io.BytesIO(adobe)),
        ImageCms.createProfile("sRGB"),
        outputMode="RGB",
    ).getpixel((0, 0))
    assert not close(in_srgb, colour, 3)  # the test profile really is different from sRGB
    # Adobe RGB background, sRGB (untagged) result.
    out = prepare_background(tmp / "wide_bg.png", (20, 15), "stretch", 0, "#FFFFFF", None)
    assert close(px(out, 10, 7), in_srgb), (px(out, 10, 7), in_srgb)
    # Same profile on both sides: the pixels stay as they are.
    out = prepare_background(tmp / "wide_bg.png", (20, 15), "stretch", 0, "#FFFFFF", adobe)
    assert px(out, 10, 7) == colour
    # Untagged (sRGB) background, Adobe RGB result; the padding colour is converted as well.
    out = prepare_background(tmp / "plain_bg.png", (80, 15), "contain", 0, "#00FF00", adobe)
    assert close(px(out, 40, 7), srgb_to_adobe(colour)) and close(px(out, 2, 7), srgb_to_adobe(GREEN))
    # The preview is always sRGB.
    prev = background_preview(tmp / "wide_bg.png", (20, 15), "stretch", 0, "#FFFFFF")
    assert close(prev.getpixel((10, 7)), in_srgb)
    # End to end: a tagged photo keeps its profile, and the background is expressed in it.
    Image.new("RGB", (8, 4), (50, 60, 70)).save(tmp / "photo.png", icc_profile=adobe)
    res = process_file(FakeEngine(left_half_background), tmp / "photo.png", background_opts(tmp / "plain_bg.png"))
    with Image.open(res.output) as im:
        assert im.info.get("icc_profile") == adobe
        assert close(im.getpixel((0, 0)), srgb_to_adobe(colour)), im.getpixel((0, 0))
        assert im.getpixel((7, 0)) == (50, 60, 70)
    # With keep_icc off the result is sRGB, so an Adobe RGB background is converted to sRGB.
    opts = background_opts(tmp / "wide_bg.png", keep_icc=False, suffix="_srgb")
    res = process_file(FakeEngine(left_half_background), tmp / "photo.png", opts)
    with Image.open(res.output) as im:
        assert not im.info.get("icc_profile")
        assert close(im.getpixel((0, 0)), in_srgb), (im.getpixel((0, 0)), in_srgb)


def test_transparent_background_image_shows_the_colour(tmp: Path) -> None:
    arr = np.zeros((20, 40, 4), np.uint8)
    arr[:, :20] = (255, 0, 0, 0)  # hidden red under full transparency
    arr[:, 20:] = (0, 0, 255, 255)
    Image.fromarray(arr, "RGBA").save(tmp / "cut.png")
    out = prepare_background(tmp / "cut.png", (40, 20), "stretch", 0, "#00FF00", None)
    assert px(out, 5, 10) == GREEN and px(out, 35, 10) == BLUE, (px(out, 5, 10), px(out, 35, 10))


def test_jpeg_output_with_image_background(tmp: Path) -> None:
    bg = bands_image(tmp / "bg.png", (60, 40), (RED, BLUE))
    Image.new("RGB", (60, 40), (10, 200, 30)).save(tmp / "p.png")

    def centre_subject(h, w):
        a = np.zeros((h, w), np.float32)
        a[h // 4 : 3 * h // 4, w // 4 : 3 * w // 4] = 1.0
        return a

    opts = background_opts(bg, output_format="jpg", background_fit="stretch")
    res = process_file(FakeEngine(centre_subject), tmp / "p.png", opts)
    assert res.output.name == "p_nobg.jpg", res.output
    with Image.open(res.output) as im:
        assert im.format == "JPEG" and im.mode == "RGB"
        assert close(im.getpixel((3, 20)), RED, 12) and close(im.getpixel((56, 20)), BLUE, 12)
        assert close(im.getpixel((30, 20)), (10, 200, 30), 12), im.getpixel((30, 20))


def test_background_is_fitted_after_the_crop(tmp: Path) -> None:
    bg = bands_image(tmp / "bg.png")
    Image.new("RGB", (200, 100), (10, 200, 30)).save(tmp / "p.png")

    def small_subject(h, w):
        a = np.zeros((h, w), np.float32)
        a[40:60, 90:110] = 1.0
        return a

    opts = background_opts(bg, background_fit="stretch", crop_to_subject=True, crop_margin=50.0)
    res = process_file(FakeEngine(small_subject), tmp / "p.png", opts)
    assert res.size == (40, 40), res.size  # 20 x 20 subject plus 10 px on every side
    with Image.open(res.output) as im:
        assert im.mode == "RGB" and im.size == (40, 40)
        # The whole background is stretched over the cropped result, not over the photo.
        assert close(im.getpixel((1, 20)), RED, 2) and close(im.getpixel((38, 20)), BLUE, 2)
        assert close(im.getpixel((20, 2)), GREEN, 2) and im.getpixel((20, 20)) == (10, 200, 30)


def test_unusable_background_fails_before_inference(tmp: Path) -> None:
    Image.new("RGB", (8, 6)).save(tmp / "p.jpg")
    (tmp / "junk.jpg").write_bytes(b"not an image at all")
    noise = np.random.default_rng(3).integers(0, 256, (256, 256, 3), dtype=np.uint8)
    buf = io.BytesIO()
    Image.fromarray(noise, "RGB").save(buf, format="JPEG", quality=90)
    (tmp / "cut_off.jpg").write_bytes(buf.getvalue()[: len(buf.getvalue()) // 2])
    (tmp / "a_folder").mkdir()
    cases = {
        tmp / "nowhere" / "bg.jpg": "not found",
        tmp / "junk.jpg": "damaged",
        tmp / "cut_off.jpg": "damaged or incomplete",
        tmp / "a_folder": "folder",
        "": "no background image chosen",
    }
    for bg, words in cases.items():
        engine = FakeEngine()
        try:
            process_file(engine, tmp / "p.jpg", background_opts(bg, suffix="_x"))
        except BackgroundError as exc:
            assert words in str(exc).lower(), (bg, str(exc))
            if bg:
                assert str(bg) in str(exc), (bg, str(exc))
        else:
            raise AssertionError(f"{bg!r} was accepted")
        assert engine.calls == 0, f"inference ran before {bg!r} was checked"
    assert not list(tmp.glob("p_x*")) and not list(tmp.glob("*.bgr-tmp"))
    # A failure is not remembered: once the file is fixed it is accepted.
    bands_image(tmp / "junk.jpg", format="JPEG")
    assert check_background(tmp / "junk.jpg") == tmp / "junk.jpg"


def test_relative_background_is_inside_the_data_folder(tmp: Path) -> None:
    (tmp / "backgrounds").mkdir()
    bands_image(tmp / "backgrounds" / "b.png")
    with patched(imageio.paths, "data_dir", lambda: tmp):
        assert check_background("backgrounds\\b.png") == tmp / "backgrounds" / "b.png"
        assert prepare_background("backgrounds\\b.png", (3, 1), "stretch", 0, "#FFFFFF", None).shape == (1, 3, 3)


def test_compose_with_image_background_matches_reference() -> None:
    rng = np.random.default_rng(2)
    fg = rng.uniform(-0.1, 1.1, (37, 23, 3)).astype(np.float32)
    alpha = rng.uniform(-0.1, 1.1, (37, 23)).astype(np.float32)
    bg = rng.uniform(0.0, 1.0, (37, 23, 3)).astype(np.float32)
    a = np.clip(alpha, 0.0, 1.0)[..., None]
    f = np.clip(fg, 0.0, 1.0)
    ref = np.rint((f * a + bg * (1.0 - a)) * 255.0).astype(int)
    whole = np.asarray(compose(fg, alpha, bg)).astype(int)
    assert np.abs(whole - ref).max() <= 1
    with patched(imageio, "_BAND_PIXELS", 50):  # bands of two rows: the same results
        banded = [np.asarray(compose(fg, alpha, b)) for b in (bg, None, (30, 144, 255))]
    for got, b in zip(banded, (bg, None, (30, 144, 255))):
        assert np.array_equal(got, np.asarray(compose(fg, alpha, b)))
    try:
        compose(fg, alpha, bg[:-1])
    except ValueError:
        pass
    else:
        raise AssertionError("a background of the wrong size was accepted")


def test_compose_needs_no_full_size_float_copy() -> None:
    h, w = 2000, 3000  # 6 MP: one float32 RGB copy alone would be 72 MB
    fg = np.full((h, w, 3), 0.5, np.float32)
    alpha = np.full((h, w), 0.5, np.float32)
    bg = np.zeros((h, w, 3), np.float32)
    for background, limit in ((bg, 45e6), ((10, 20, 30), 45e6), (None, 55e6)):
        tracemalloc.start()
        try:
            compose(fg, alpha, background)
            peak = tracemalloc.get_traced_memory()[1]
        finally:
            tracemalloc.stop()
        assert peak < limit, (type(background).__name__, peak)


# ------------------------------------------------------------------ runner
def main() -> int:
    tests = [(name, fn) for name, fn in sorted(globals().items()) if name.startswith("test_") and callable(fn)]
    failed = 0
    for name, fn in tests:
        try:
            if fn.__code__.co_argcount:
                with tempfile.TemporaryDirectory(prefix="bgr-test-") as d:
                    fn(Path(d))
            else:
                fn()
        except Exception:
            failed += 1
            print(f"FAIL {name}")
            traceback.print_exc()
        else:
            print(f"ok   {name}")
    print(f"{len(tests) - failed} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())

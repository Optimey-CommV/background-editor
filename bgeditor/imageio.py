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

"""Loading and saving images without losing orientation, colour profile or detail."""

from __future__ import annotations

import functools
import io
import math
import os
import re
import uuid
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from PIL import Image, ImageCms, ImageFilter, ImageOps, UnidentifiedImageError

from . import paths
from .options import BACKGROUND_FITS, MAX_BACKGROUND_BLUR, clean_color

# The pipeline holds several float32 copies of the photo (roughly 100 bytes per pixel),
# so refuse anything larger than this up front, before it is decoded.
MAX_PIXELS = 150_000_000
# Pillow only raises above twice this value (it merely warns in between); the explicit
# check in load_image enforces MAX_PIXELS itself with a readable message.
Image.MAX_IMAGE_PIXELS = MAX_PIXELS

INPUT_EXTENSIONS = {".jpg", ".jpeg", ".jfif", ".png", ".webp", ".bmp", ".tif", ".tiff"}

# Transparent parts of an input are shown to the model on this neutral grey (0-255).
BACKDROP = 128

# compose() works through the image in bands of about this many pixels, so its float32
# scratch memory stays around 12 MB whatever the photo's size.
_BAND_PIXELS = 1 << 20


def _register_heif() -> bool:
    try:
        import pillow_heif  # type: ignore[import-not-found]
    except ImportError:
        return False
    pillow_heif.register_heif_opener()
    return True


if _register_heif():
    INPUT_EXTENSIONS |= {".heic", ".heif"}


@dataclass
class SourceImage:
    path: Path
    rgb: Image.Image  # mode RGB, 8 bit, EXIF orientation applied; transparent parts shown over BACKDROP
    icc_profile: bytes | None  # always an RGB profile describing rgb, or None for sRGB
    dpi: tuple[float, float] | None
    alpha: np.ndarray | None = None  # float32 0-1 (HxW) when the file has transparency, else None


class SaveError(OSError):
    """A result could not be written; the message names the folder."""


class BackgroundError(ValueError):
    """The background image cannot be used; the message names the file."""


def is_supported(path: Path) -> bool:
    return path.suffix.lower() in INPUT_EXTENSIONS


@functools.lru_cache(maxsize=1)
def _srgb_profile_bytes() -> bytes:
    return ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()


def _open_profile(icc: bytes | None) -> ImageCms.ImageCmsProfile | None:
    if not icc:
        return None
    try:
        return ImageCms.ImageCmsProfile(io.BytesIO(icc))
    except (ImageCms.PyCMSError, OSError, TypeError, ValueError):
        return None


def profile_space(icc: bytes | None) -> str | None:
    """The ICC colour space ('RGB', 'GRAY', 'CMYK', ...), or None when absent or unreadable."""
    prof = _open_profile(icc)
    if prof is None:
        return None
    try:
        return (prof.profile.xcolor_space or "").strip() or None
    except (AttributeError, ValueError):
        return None


def _gray_to_rgb(gray: Image.Image, icc: bytes | None) -> tuple[Image.Image, bytes | None]:
    """8-bit L to RGB. A grey profile cannot describe RGB pixels, so convert through it to sRGB."""
    prof = _open_profile(icc)
    if prof is not None and profile_space(icc) == "GRAY":
        try:
            out = ImageCms.profileToProfile(
                gray,
                prof,
                ImageCms.createProfile("sRGB"),
                renderingIntent=ImageCms.Intent.PERCEPTUAL,
                outputMode="RGB",
            )
            return out, _srgb_profile_bytes()
        except (ImageCms.PyCMSError, OSError, ValueError):
            pass
    return gray.convert("RGB"), None


def _to_rgb8(im: Image.Image, icc: bytes | None) -> tuple[Image.Image, bytes | None]:
    """Convert any Pillow mode to 8-bit RGB, keeping the colour meaning intact.

    The returned profile always describes RGB pixels: grey and CMYK sources are converted to
    sRGB through their profile, and a profile that does not fit the pixels is dropped rather
    than embedded in an RGB file where colour-managed software would reject it.
    """
    mode = im.mode
    if mode == "CMYK":
        prof = _open_profile(icc)
        if prof is not None and profile_space(icc) == "CMYK":
            try:
                dst = ImageCms.createProfile("sRGB")
                out = ImageCms.profileToProfile(
                    im, prof, dst, renderingIntent=ImageCms.Intent.PERCEPTUAL, outputMode="RGB"
                )
                return out, _srgb_profile_bytes()
            except (ImageCms.PyCMSError, OSError, ValueError):
                pass
        return im.convert("RGB"), None
    if mode in ("I;16", "I;16L", "I;16B", "I;16N"):
        # The bit depth is known: always scale the full 16-bit range, even for a dark frame.
        arr = np.asarray(im).astype(np.uint32)
        arr8 = ((arr * 255 + 32767) // 65535).astype(np.uint8)
        return _gray_to_rgb(Image.fromarray(arr8, "L"), icc)
    if mode == "I":
        # 32-bit integers: the real range is unknown, so judge it from the data.
        arr = np.asarray(im, dtype=np.float64)
        peak = arr.max(initial=0)
        top = 255.0 if peak <= 255 else 65535.0 if peak <= 65535 else float(peak)
        arr8 = np.clip(np.rint(arr * (255.0 / top)), 0, 255).astype(np.uint8)
        return _gray_to_rgb(Image.fromarray(arr8, "L"), icc)
    if mode == "F":
        arr = np.asarray(im, dtype=np.float64)
        scale = 255.0 if arr.max(initial=0) <= 1.0 else 1.0
        arr8 = np.clip(np.rint(arr * scale), 0, 255).astype(np.uint8)
        return _gray_to_rgb(Image.fromarray(arr8, "L"), icc)
    if mode in ("L", "LA", "1"):
        return _gray_to_rgb(im.convert("L"), icc)
    if mode == "La":
        return _gray_to_rgb(im.convert("LA").convert("L"), icc)
    if mode == "RGB":
        rgb = im
    elif mode in ("PA", "RGBa") or (mode == "P" and "transparency" in im.info):
        # Through RGBA: unpremultiplies RGBa and reads palette transparency correctly.
        rgb = im.convert("RGBA").convert("RGB")
    else:
        rgb = im.convert("RGB")
    return rgb, icc if profile_space(icc) == "RGB" else None


def _alpha_of(im: Image.Image) -> np.ndarray | None:
    """The image's own transparency as uint8 (HxW), or None when it is fully opaque."""
    mode = im.mode
    try:
        if mode in ("RGBA", "LA", "PA"):
            band = im.getchannel("A")
        elif mode in ("RGBa", "La"):
            band = im.convert("RGBA" if mode == "RGBa" else "LA").getchannel("A")
        elif "transparency" in im.info and mode in ("P", "L", "RGB", "1"):
            band = im.convert("RGBA").getchannel("A")
        else:
            return None
    except (ValueError, OSError):
        return None
    arr = np.asarray(band, dtype=np.uint8)
    if arr.size == 0 or arr.min() == 255:
        return None
    return arr


def _over_backdrop(rgb: Image.Image, alpha8: np.ndarray) -> Image.Image:
    """Show the transparent parts over a neutral grey, so the model never sees hidden colours."""
    arr = np.array(rgb, dtype=np.uint8)
    see_through = alpha8 < 255
    a = alpha8[see_through].astype(np.uint32)[:, None]
    px = arr[see_through].astype(np.uint32)
    arr[see_through] = ((px * a + BACKDROP * (255 - a) + 127) // 255).astype(np.uint8)
    return Image.fromarray(arr, "RGB")


def restore_colours(foreground: np.ndarray, src: SourceImage) -> np.ndarray:
    """Where the source was partly transparent, use its own colours instead of the grey mix.

    The source already separated subject from background there, so its straight colour is the
    best foreground estimate. It is recovered from the backdrop composite the model saw; the
    rounding error shrinks with alpha, so it stays invisible once the result is composited.
    """
    if src.alpha is None:
        return foreground
    partial = (src.alpha > 0.0) & (src.alpha < 1.0)
    if not partial.any():
        return foreground
    if not foreground.flags.writeable:
        foreground = foreground.copy()
    a = src.alpha[partial][:, None]
    mix = np.asarray(src.rgb, dtype=np.uint8)[partial].astype(np.float32) / 255.0
    straight = (mix - (BACKDROP / 255.0) * (1.0 - a)) / a
    foreground[partial] = np.clip(straight, 0.0, 1.0)
    return foreground


def _too_large(width: int, height: int) -> ValueError:
    return ValueError(
        f"Photo too large: {width} x {height} ({width * height / 1e6:.0f} MP). "
        f"The limit is {MAX_PIXELS / 1e6:.0f} MP."
    )


def load_image(path: Path) -> SourceImage:
    path = Path(path)
    try:
        opened = Image.open(path)
    except Image.DecompressionBombError as exc:
        # Pillow refuses on its own far above MAX_PIXELS; its message holds the pixel count.
        m = re.search(r"\((\d+) pixels\)", str(exc))
        size = f" ({int(m.group(1)) / 1e6:.0f} MP)" if m else ""
        raise ValueError(f"Photo too large{size}. The limit is {MAX_PIXELS / 1e6:.0f} MP.") from None
    with opened as im:
        if im.width * im.height > MAX_PIXELS:
            raise _too_large(im.width, im.height)  # before decoding, so nothing is allocated
        im.load()
        icc = im.info.get("icc_profile") or None
        dpi = im.info.get("dpi")
        oriented = ImageOps.exif_transpose(im)  # a new image, independent of the open file
        if oriented is im:
            oriented = im.copy()
    alpha8 = _alpha_of(oriented)
    rgb, icc = _to_rgb8(oriented, icc)
    del oriented
    alpha = None
    if alpha8 is not None:
        rgb = _over_backdrop(rgb, alpha8)
        alpha = alpha8.astype(np.float32) / 255.0
    if dpi is not None:
        try:
            dpi = (float(dpi[0]), float(dpi[1]))
        except (TypeError, ValueError, IndexError):
            dpi = None
    return SourceImage(path=path, rgb=rgb, icc_profile=icc, dpi=dpi, alpha=alpha)


def to_srgb(rgb: Image.Image, icc: bytes | None) -> Image.Image | None:
    """Convert RGB pixels described by icc to sRGB. Returns None when that is not possible."""
    if not icc:
        return rgb  # untagged pixels are already taken as sRGB
    if icc == _srgb_profile_bytes():
        return rgb
    prof = _open_profile(icc)
    if prof is None or profile_space(icc) != "RGB":
        return None
    try:
        return ImageCms.profileToProfile(
            rgb,
            prof,
            ImageCms.createProfile("sRGB"),
            renderingIntent=ImageCms.Intent.PERCEPTUAL,
            outputMode="RGB",
        )
    except (ImageCms.PyCMSError, OSError, ValueError):
        return None


def srgb_color_to_profile(color: tuple[int, int, int], icc: bytes | None) -> tuple[int, int, int]:
    """Express an sRGB colour (as picked in the app) in the photo's RGB profile."""
    if not icc or icc == _srgb_profile_bytes():
        return color
    prof = _open_profile(icc)
    if prof is None or profile_space(icc) != "RGB":
        return color
    try:
        px = ImageCms.profileToProfile(
            Image.new("RGB", (1, 1), color),
            ImageCms.createProfile("sRGB"),
            prof,
            renderingIntent=ImageCms.Intent.RELATIVE_COLORIMETRIC,
            outputMode="RGB",
        ).getpixel((0, 0))
    except (ImageCms.PyCMSError, OSError, ValueError):
        return color
    return int(px[0]), int(px[1]), int(px[2])


def _band_rows(height: int, width: int) -> int:
    """Rows per band: about _BAND_PIXELS pixels, at least one row."""
    return max(1, min(height, _BAND_PIXELS // max(1, width)))


def _bands(height: int, rows: int):
    """Row ranges (start, stop) of at most rows rows each."""
    for start in range(0, height, rows):
        yield start, min(height, start + rows)


def compose(
    rgb: np.ndarray,
    alpha: np.ndarray,
    background: tuple[int, int, int] | np.ndarray | None,
) -> Image.Image:
    """Build the output image from a foreground (float 0-1, HxWx3) and alpha (float 0-1, HxW).

    background is None (transparent result), an RGB colour (0-255), or an image as a float
    array (0-1, HxWx3, see prepare_background). Colour and image are mixed the same way, in
    float32: fg * a + bg * (1 - a). Colour and image must already be in the colour space
    of rgb. The work is done in bands of rows, so a 24 MP photo needs no full-size float
    copy on top of its inputs.
    """
    h, w = alpha.shape[:2]
    if rgb.shape[:2] != (h, w):
        raise ValueError(f"foreground is {rgb.shape[1]} x {rgb.shape[0]}, alpha is {w} x {h}")
    rows = _band_rows(h, w)
    # One scratch buffer for colour and one for alpha, reused by every band.
    colour_buf = np.empty((rows, w, 3), dtype=np.float32)
    alpha_buf = np.empty((rows, w), dtype=np.float32)
    if background is None:
        out = np.empty((h, w, 4), dtype=np.uint8)
        for r0, r1 in _bands(h, rows):
            tmp = colour_buf[: r1 - r0]
            np.multiply(rgb[r0:r1], 255.0, out=tmp)
            np.clip(tmp, 0.0, 255.0, out=tmp)
            out[r0:r1, :, :3] = np.rint(tmp, out=tmp)
            a = alpha_buf[: r1 - r0]
            np.multiply(alpha[r0:r1], 255.0, out=a)
            np.clip(a, 0.0, 255.0, out=a)
            out[r0:r1, :, 3] = np.rint(a, out=a)
            # Fully transparent pixels carry no colour; zero them so files compress well
            # and no stale background colour survives in tools that ignore alpha.
            band = out[r0:r1]
            band[band[..., 3] == 0, :3] = 0
        return Image.fromarray(out, "RGBA")

    if isinstance(background, np.ndarray):
        if background.shape != (h, w, 3):
            raise ValueError(
                f"background is {background.shape[1]} x {background.shape[0]}, the photo is {w} x {h}"
            )
        image_bg = background
        colour = None
    else:
        image_bg = None
        colour = np.asarray(background, dtype=np.float32) / 255.0
    out = np.empty((h, w, 3), dtype=np.uint8)
    for r0, r1 in _bands(h, rows):
        a = alpha_buf[: r1 - r0]
        np.clip(alpha[r0:r1], 0.0, 1.0, out=a)
        mixed = colour_buf[: r1 - r0]
        np.clip(rgb[r0:r1], 0.0, 1.0, out=mixed)
        bg = colour if image_bg is None else image_bg[r0:r1]
        # fg * a + bg * (1 - a), computed in place as bg + (fg - bg) * a.
        mixed -= bg
        mixed *= a[..., None]
        mixed += bg
        mixed *= 255.0
        np.rint(mixed, out=mixed)
        np.clip(mixed, 0.0, 255.0, out=mixed)
        out[r0:r1] = mixed
    return Image.fromarray(out, "RGB")


# ------------------------------------------------------------------ background images
def hex_to_rgb(value: str, default: tuple[int, int, int] = (255, 255, 255)) -> tuple[int, int, int]:
    """'#RRGGBB' or '#RGB' as an (r, g, b) tuple; default when value is not a colour."""
    text = clean_color(value)
    if text is None:
        return default
    return int(text[1:3], 16), int(text[3:5], 16), int(text[5:7], 16)


def resolve_background(path: str | Path) -> Path:
    """The background file a setting points to. ~ and %VARS% are expanded; a relative path
    is taken inside paths.data_dir(), so a portable copy keeps its own backgrounds."""
    text = os.path.expandvars(os.path.expanduser(str(path or "").strip()))
    if not text:
        raise BackgroundError("No background image chosen. Choose an image file for the background.")
    p = Path(text)
    if not p.is_absolute():
        p = paths.data_dir() / p
    return p


def _background_error(path: Path, exc: BaseException) -> BackgroundError:
    if isinstance(exc, BackgroundError):
        return exc
    if isinstance(exc, FileNotFoundError):
        text = "Background image not found"
    elif isinstance(exc, (IsADirectoryError, PermissionError)) and path.is_dir():
        text = "The background image is a folder, not a file"
    elif isinstance(exc, PermissionError):
        text = "Windows does not allow reading the background image"
    elif isinstance(exc, UnidentifiedImageError):
        text = "The background image is damaged, or not an image format this app can read"
    elif isinstance(exc, Image.DecompressionBombError):
        text = f"The background image is too large (the limit is {MAX_PIXELS / 1e6:.0f} MP)"
    elif isinstance(exc, OSError):
        detail = exc.strerror or str(exc) or exc.__class__.__name__
        text = f"The background image is damaged or incomplete ({detail})"
    else:  # ValueError from a path with a NUL, SyntaxError from a broken header, ...
        text = f"The background image cannot be read ({exc or exc.__class__.__name__})"
    return BackgroundError(f"{text}: {path}")


def _open_background(path: Path) -> Image.Image:
    """Open (not yet decode) the background image, refusing oversized files up front."""
    try:
        if path.is_dir():
            raise IsADirectoryError(path)
        im = Image.open(path)
    except MemoryError:
        raise
    except Exception as exc:  # Pillow raises many kinds for a broken header
        raise _background_error(path, exc) from exc
    if im.width * im.height > MAX_PIXELS:
        im.close()
        raise BackgroundError(
            f"The background image is too large ({im.width} x {im.height}, "
            f"{im.width * im.height / 1e6:.0f} MP; the limit is {MAX_PIXELS / 1e6:.0f} MP): {path}"
        )
    return im


def _orientation(im: Image.Image) -> int:
    try:
        value = int(im.getexif().get(0x0112, 1))
    except Exception:  # a broken EXIF block only loses the rotation
        return 1
    return value if 1 <= value <= 8 else 1


@functools.lru_cache(maxsize=8)
def _checked(key: str, mtime_ns: int, size: int) -> None:
    """Decode the file once per version of it; raising is not cached, so a fixed file is
    seen at once. A JPEG is decoded at 1/8 scale, which still reads all of its data."""
    path = Path(key)
    im = _open_background(path)
    with im:
        try:
            im.draft(None, (max(1, im.width // 8), max(1, im.height // 8)))
            im.load()
        except MemoryError:
            raise
        except Exception as exc:  # truncated data, a broken stream, ...
            raise _background_error(path, exc) from exc


def check_background(path: str | Path) -> Path:
    """Fail fast, before any inference: the background image is there and can be decoded.

    Returns the resolved path; raises BackgroundError, naming the file, when it cannot be
    used. A batch decodes the same unchanged file only once.
    """
    p = resolve_background(path)
    try:
        st = p.stat()
    except (OSError, ValueError) as exc:
        raise _background_error(p, exc) from exc
    _checked(str(p), st.st_mtime_ns, st.st_size)
    return p


def _clean_blur(blur_px: float) -> float:
    try:
        value = float(blur_px)
    except (TypeError, ValueError):
        return 0.0
    if not math.isfinite(value):
        return 0.0
    return min(max(value, 0.0), MAX_BACKGROUND_BLUR)


def _placed_size(src: tuple[int, int], size: tuple[int, int], fit: str) -> tuple[float, float]:
    """The size the whole source image takes once fitted (before any crop or padding)."""
    sw, sh = src
    w, h = size
    if fit == "stretch":
        return float(w), float(h)
    k = max(w / sw, h / sh) if fit == "cover" else min(w / sw, h / sh)
    return sw * k, sh * k


def _resize(im: Image.Image, size: tuple[int, int], box: tuple[float, float, float, float] | None = None) -> Image.Image:
    full = (0.0, 0.0, float(im.width), float(im.height))
    region = box or full
    if size == im.size and region == full:
        return im
    shrinking = size[0] <= region[2] - region[0] and size[1] <= region[3] - region[1]
    if shrinking:
        # Lanczos for detail; reduce() first for large factors, which is much faster and
        # indistinguishable from a full Lanczos pass at a gap of 3.
        return im.resize(size, Image.Resampling.LANCZOS, box=region, reducing_gap=3.0)
    return im.resize(size, Image.Resampling.BICUBIC, box=region)


def _blurred(im: Image.Image, sigma: float) -> Image.Image:
    # Pillow's Gaussian is three extended box filters: constant time per pixel for any
    # radius, and the edge pixels are repeated, so the border does not darken.
    return im.filter(ImageFilter.GaussianBlur(sigma)) if sigma > 0 else im


def _same_space(a: bytes | None, b: bytes | None) -> bool:
    srgb = _srgb_profile_bytes()
    return (a if a and a != srgb else None) == (b if b and b != srgb else None)


def _convert_profile(rgb: Image.Image, src_icc: bytes | None, dst_icc: bytes | None) -> Image.Image:
    """RGB pixels described by src_icc, expressed in dst_icc (None means sRGB for both).

    When the target profile cannot be read the pixels are left as they are: the result will
    carry that profile anyway, so no conversion can be more correct.
    """
    if _same_space(src_icc, dst_icc):
        return rgb
    src = _open_profile(src_icc) if src_icc else None
    dst = _open_profile(dst_icc) if dst_icc else None
    if (src_icc and (src is None or profile_space(src_icc) != "RGB")) or (
        dst_icc and (dst is None or profile_space(dst_icc) != "RGB")
    ):
        return rgb
    try:
        return ImageCms.profileToProfile(
            rgb,
            src or ImageCms.createProfile("sRGB"),
            dst or ImageCms.createProfile("sRGB"),
            renderingIntent=ImageCms.Intent.PERCEPTUAL,
            outputMode="RGB",
        )
    except (ImageCms.PyCMSError, OSError, ValueError):
        return rgb


def _render_background(
    path: str | Path,
    size: tuple[int, int],
    fit: str,
    blur_px: float,
    fill_hex: str,
    target_icc: bytes | None,
) -> Image.Image:
    """The background image fitted to size: 8-bit RGB in target_icc (None: sRGB)."""
    w, h = int(size[0]), int(size[1])
    if w < 1 or h < 1:
        raise ValueError(f"Background size must be at least 1 x 1, not {w} x {h}")
    if fit not in BACKGROUND_FITS:
        raise ValueError(f"Unknown background fit {fit!r}; expected one of {', '.join(BACKGROUND_FITS)}")
    sigma = _clean_blur(blur_px)
    fill = hex_to_rgb(fill_hex)
    p = resolve_background(path)

    im = _open_background(p)
    with im:
        turned = _orientation(im) in (5, 6, 7, 8)
        shown = (im.height, im.width) if turned else im.size
        pw, ph = _placed_size(shown, (w, h), fit)
        need = (max(1, math.ceil(pw)), max(1, math.ceil(ph)))
        try:
            if need[0] < shown[0] and need[1] < shown[1]:
                # A JPEG much larger than needed is decoded at 1/2, 1/4 or 1/8 scale, never
                # below the size it will be shown at: far less memory and time for 24+ MP.
                im.draft(None, (need[1], need[0]) if turned else need)
            im.load()
        except MemoryError:
            raise
        except Exception as exc:
            raise _background_error(p, exc) from exc
        icc = im.info.get("icc_profile") or None
        try:
            oriented = ImageOps.exif_transpose(im)
        except (OSError, ValueError, SyntaxError, KeyError, TypeError):
            oriented = None  # a broken EXIF block only loses the rotation
        if oriented is None or oriented is im:
            oriented = im.copy()

    alpha8 = _alpha_of(oriented)
    rgb, icc = _to_rgb8(oriented, icc)
    del oriented
    if alpha8 is not None:
        # What shows through a transparent background image is the background colour.
        under = Image.new("RGB", rgb.size, srgb_color_to_profile(fill, icc))
        rgb = Image.composite(rgb, under, Image.fromarray(alpha8, "L"))
        del under

    sw, sh = rgb.size
    offset = (0, 0)
    if fit == "cover":
        k = max(w / sw, h / sh)
        cw, ch = w / k, h / k  # the part of the source that is shown
        x0, y0 = (sw - cw) / 2.0, (sh - ch) / 2.0
        # Blur with the real neighbours of the shown part where the source has them.
        margin = math.ceil(3.0 * sigma) + 1 if sigma > 0 else 0
        mx, my = min(margin, int(x0 * k)), min(margin, int(y0 * k))
        box = (
            max(0.0, x0 - mx / k),
            max(0.0, y0 - my / k),
            min(float(sw), x0 + cw + mx / k),
            min(float(sh), y0 + ch + my / k),
        )
        placed = _blurred(_resize(rgb, (w + 2 * mx, h + 2 * my), box), sigma)
        if mx or my:
            placed = placed.crop((mx, my, mx + w, my + h))
    elif fit == "contain":
        pw, ph = _placed_size((sw, sh), (w, h), fit)
        pw, ph = min(w, max(1, round(pw))), min(h, max(1, round(ph)))
        placed = _blurred(_resize(rgb, (pw, ph)), sigma)
        offset = ((w - pw) // 2, (h - ph) // 2)
    else:  # stretch
        placed = _blurred(_resize(rgb, (w, h)), sigma)
    del rgb

    placed = _convert_profile(placed, icc, target_icc)
    if placed.size != (w, h):
        canvas = Image.new("RGB", (w, h), srgb_color_to_profile(fill, target_icc))
        canvas.paste(placed, offset)
        placed = canvas
    return placed


def prepare_background(
    path: str | Path,
    size: tuple[int, int],
    fit: str,
    blur_px: float,
    fill_hex: str,
    target_icc: bytes | None,
) -> np.ndarray:
    """The background image for a result of size (width, height), ready for compose().

    - fit: 'cover' scales to fill and crops the centre, 'contain' scales to fit and pads
      with fill_hex (an sRGB colour, as picked in the app), 'stretch' resizes to the size.
    - blur_px: Gaussian blur (sigma) in pixels of size, 0-50.
    - The EXIF orientation is applied, and the colours are converted from the file's own
      profile (untagged: sRGB) into target_icc, the profile the result will carry (None:
      sRGB). Transparent parts of the image show fill_hex.
    - Returns float32 HxWx3 in 0-1. Raises BackgroundError naming the file when it cannot
      be read.
    """
    img = _render_background(path, size, fit, blur_px, fill_hex, target_icc)
    arr = np.asarray(img, dtype=np.uint8)
    del img
    return np.divide(arr, np.float32(255.0), dtype=np.float32)


def background_preview(
    path: str | Path,
    size: tuple[int, int],
    fit: str,
    blur_px: float,
    fill_hex: str,
) -> Image.Image:
    """The background as it will look, as an 8-bit sRGB image of size (width, height).

    blur_px is in pixels of size: for a preview smaller than the result, scale the blur by
    preview width / result width to show the same softness.
    """
    return _render_background(path, size, fit, blur_px, fill_hex, None)


def check_writable(folder: Path) -> None:
    """Fail fast, with the folder in the message, when results cannot be saved in folder.

    os.access() ignores Windows ACLs, so this creates and removes a tiny probe file instead.
    """
    folder = Path(folder)
    probe = folder / f".bgeditor-probe.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp"
    try:
        folder.mkdir(parents=True, exist_ok=True)
        fd = os.open(probe, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0))
        os.close(fd)
        os.remove(probe)
    except OSError as exc:
        raise SaveError(f"Cannot save in {folder}: {_reason(exc)}.") from exc


def _reason(exc: OSError) -> str:
    if isinstance(exc, PermissionError):
        return "no permission to write there"
    if isinstance(exc, (FileExistsError, NotADirectoryError)):
        return "a file is in the way of the folder"
    return exc.strerror or str(exc) or exc.__class__.__name__


def save_image(
    image: Image.Image,
    dest: Path,
    icc_profile: bytes | None = None,
    dpi: tuple[float, float] | None = None,
    jpeg_quality: int = 95,
) -> Path:
    """Save atomically: write to a temp file in the same folder, then rename over the target."""
    dest = Path(dest)
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise SaveError(f"Cannot save in {dest.parent}: {_reason(exc)}.") from exc
    ext = dest.suffix.lower()
    params: dict = {}
    if icc_profile:
        params["icc_profile"] = icc_profile
    if dpi:
        params["dpi"] = dpi
    if ext in (".jpg", ".jpeg"):
        if image.mode != "RGB":
            raise ValueError("JPEG has no transparency; compose onto a background colour first")
        fmt = "JPEG"
        params.update(quality=jpeg_quality, subsampling=0, optimize=True)
    elif ext == ".png":
        fmt = "PNG"
        params.update(compress_level=6)
    elif ext == ".webp":
        fmt = "WEBP"
        params.update(lossless=True, method=4)
    elif ext in (".tif", ".tiff"):
        fmt = "TIFF"
        params.update(compression="tiff_deflate")
    else:
        raise ValueError(f"Unsupported output format: {ext}")

    # Not tempfile.mkstemp: on Windows it retries up to os.TMP_MAX (2**31 - 1) times
    # when the folder denies writes, because os.access() ignores ACLs and reports the
    # folder as writable. A plain unique name fails fast with PermissionError instead.
    # The format is passed explicitly, so the temp name deliberately has no image
    # extension: a file left behind by a crash is never picked up as a photo.
    tmp = dest.with_name(f".{dest.stem}.{os.getpid()}.{uuid.uuid4().hex[:8]}.bgr-tmp")
    try:
        image.save(tmp, format=fmt, **params)
        os.replace(tmp, dest)
    except BaseException as exc:
        try:
            os.remove(tmp)
        except OSError:
            pass
        if isinstance(exc, OSError) and not isinstance(exc, SaveError):
            raise SaveError(f"Cannot save {dest.name} in {dest.parent}: {_reason(exc)}.") from exc
        raise
    return dest

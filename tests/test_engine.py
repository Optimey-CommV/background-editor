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

"""Tests for the engine helpers (SciPy/NumPy/Pillow), model I/O adaptivity, the model
catalogue and the model update check.

pytest is not needed: run with the project's Python,

    .venv\\Scripts\\python.exe tests\\test_engine.py

Most tests use synthetic data and tiny ONNX models built here. The update check runs on
recorded API responses (tests/fixtures), never on the network. One test runs the real
BiRefNet lite model as an update candidate (about a minute on a CPU); it is skipped when
that model is not downloaded.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import traceback
from dataclasses import replace
from pathlib import Path

import numpy as np
from PIL import Image
from scipy import ndimage as ndi

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = Path(__file__).resolve().parent / "fixtures"
sys.path.insert(0, str(ROOT))

from bgeditor import engine, models, paths, updates  # noqa: E402
from bgeditor.options import Options  # noqa: E402

rng = np.random.default_rng(7)


# ------------------------------------------------------------------ helpers
@contextlib.contextmanager
def temp_app(tmp: Path):
    """A portable app folder in tmp: data and models go there; MODELS is restored afterwards."""
    saved_app_dir = paths.app_dir
    saved_env = os.environ.get("BGEDITOR_PORTABLE")
    saved_models = dict(models.MODELS)
    saved_preferred = models._preferred
    saved_transport = updates._transport
    os.environ["BGEDITOR_PORTABLE"] = "1"
    paths.app_dir = lambda: tmp
    paths.is_portable.cache_clear()
    paths.portable_config.cache_clear()
    try:
        (tmp / "models").mkdir(parents=True, exist_ok=True)
        yield tmp
    finally:
        paths.app_dir = saved_app_dir
        if saved_env is None:
            os.environ.pop("BGEDITOR_PORTABLE", None)
        else:
            os.environ["BGEDITOR_PORTABLE"] = saved_env
        paths.is_portable.cache_clear()
        paths.portable_config.cache_clear()
        models.MODELS.clear()
        models.MODELS.update(saved_models)
        models._preferred = saved_preferred
        updates._transport = saved_transport


def sha256_of(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# --- a minimal ONNX writer (protobuf by hand; the onnx package is not a dependency)
def _varint(n: int) -> bytes:
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        out.append(b | (0x80 if n else 0))
        if not n:
            return bytes(out)


def _f_int(field: int, n: int) -> bytes:
    return _varint(field << 3) + _varint(n)


def _f_bytes(field: int, data: bytes) -> bytes:
    return _varint(field << 3 | 2) + _varint(len(data)) + data


def _f_str(field: int, s: str) -> bytes:
    return _f_bytes(field, s.encode("utf-8"))


def _value_info(name: str, elem: int, dims) -> bytes:
    shape = b"".join(_f_bytes(1, _f_int(1, d) if isinstance(d, int) else _f_str(2, d)) for d in dims)
    return _f_str(1, name) + _f_bytes(2, _f_bytes(1, _f_int(1, elem) + _f_bytes(2, shape)))


def _tensor(name: str, values: np.ndarray) -> bytes:
    return b"".join(_f_int(1, d) for d in values.shape) + _f_int(2, 1) + _f_str(8, name) + _f_bytes(9, values.astype("<f4").tobytes())


def _node(op: str, inputs, outputs, attrs: bytes = b"") -> bytes:
    return b"".join(_f_str(1, i) for i in inputs) + b"".join(_f_str(2, o) for o in outputs) + _f_str(3, op.lower()) + _f_str(4, op) + attrs


def _attr_int(name: str, value: int) -> bytes:
    return _f_bytes(5, _f_str(1, name) + _f_int(3, value) + _f_int(20, 2))  # type INT


def onnx_model(nodes, inits, inputs, outputs, opset: int = 17, ir: int = 8) -> bytes:
    graph = (
        b"".join(_f_bytes(1, n) for n in nodes)
        + _f_str(2, "test")
        + b"".join(_f_bytes(5, t) for t in inits)
        + b"".join(_f_bytes(11, v) for v in inputs)
        + b"".join(_f_bytes(12, v) for v in outputs)
    )
    return _f_int(1, ir) + _f_str(2, "bgeditor-test") + _f_bytes(7, graph) + _f_bytes(8, _f_str(1, "") + _f_int(2, opset))


CONV_W = np.array([0.8, -0.5, 0.3], np.float32).reshape(1, 3, 1, 1)
CONV_B = np.array([0.1], np.float32)


def conv_model(h="h", w="w") -> bytes:
    """[1, 3, h, w] float -> 1x1 Conv -> [1, 1, h, w] logits."""
    return onnx_model(
        [_node("Conv", ["x", "W", "B"], ["y"])],
        [_tensor("W", CONV_W), _tensor("B", CONV_B)],
        [_value_info("x", 1, [1, 3, h, w])],
        [_value_info("y", 1, [1, 1, h, w])],
    )


def fp16_sigmoid_model() -> bytes:
    """[1, 3, h, w] float16 -> Cast -> Conv -> Sigmoid -> [1, 1, h, w] probabilities."""
    return onnx_model(
        [
            _node("Cast", ["x"], ["xf"], _attr_int("to", 1)),
            _node("Conv", ["xf", "W", "B"], ["logit"]),
            _node("Sigmoid", ["logit"], ["y"]),
        ],
        [_tensor("W", CONV_W), _tensor("B", CONV_B)],
        [_value_info("x", 10, ["batch", 3, "h", "w"])],
        [_value_info("y", 1, ["batch", 1, "h", "w"])],
    )


def deform_model() -> bytes:
    """DeformConv (opset 19): ONNX Runtime 1.24 has no kernel for it."""
    return onnx_model(
        [_node("DeformConv", ["x", "W", "offset"], ["y"])],
        [_tensor("W", np.ones((1, 3, 3, 3), np.float32)), _tensor("offset", np.zeros((1, 18, 6, 6), np.float32))],
        [_value_info("x", 1, [1, 3, 8, 8])],
        [_value_info("y", 1, [1, 1, 6, 6])],
        opset=19,
        ir=9,
    )


def conv_reference(x: np.ndarray) -> np.ndarray:
    """What conv_model computes for an NCHW input."""
    return np.tensordot(CONV_W.reshape(3), x[0], axes=1) + CONV_B[0]


def register(tmp: Path, key: str, data: bytes, **kw) -> models.ModelSpec:
    """Put a model file in the temporary models folder and register it."""
    name = kw.pop("filename", f"{key}.onnx")
    (tmp / "models" / name).write_bytes(data)
    spec = models.ModelSpec(key=key, title=key, filename=name, url="https://example.invalid/" + name,
                            sha256=sha256_of(data), size=len(data), licence="MIT", **kw)
    models.MODELS[key] = spec
    return spec


# --- brute-force references
def brute_within(mask: np.ndarray, r: float) -> np.ndarray:
    ys, xs = np.nonzero(mask)
    h, w = mask.shape
    if ys.size == 0:
        return np.zeros_like(mask, dtype=bool)
    yy, xx = np.mgrid[0:h, 0:w]
    d2 = ((yy[..., None] - ys) ** 2 + (xx[..., None] - xs) ** 2).min(axis=-1)
    return d2 <= r * r


def brute_box(a: np.ndarray, k: int) -> np.ndarray:
    before, after = k // 2, k - 1 - k // 2
    p = np.pad(a.astype(np.float64), ((before, after), (before, after)), mode="reflect")
    out = np.zeros(a.shape)
    for dy in range(k):
        for dx in range(k):
            out += p[dy : dy + a.shape[0], dx : dx + a.shape[1]]
    return out / (k * k)


def brute_gaussian(a: np.ndarray, sigma: float) -> np.ndarray:
    radius = (int(round(sigma * 8 + 1)) | 1) // 2
    x = np.arange(-radius, radius + 1)
    k = np.exp(-(x**2) / (2 * sigma * sigma))
    k /= k.sum()
    p = np.pad(a.astype(np.float64), radius, mode="reflect")
    rows = sum(k[i] * p[:, i : i + a.shape[1]] for i in range(k.size))
    return sum(k[i] * rows[i : i + a.shape[0], :] for i in range(k.size))


def brute_morph(a: np.ndarray, r: int, dilate: bool) -> np.ndarray:
    widths = engine.ellipse_half_widths(r)
    h, w = a.shape
    fill = -np.inf if dilate else np.inf
    p = np.full((h + 2 * r, w + 2 * r), fill)
    p[r : r + h, r : r + w] = a
    out = np.full(a.shape, fill)
    comb = np.maximum if dilate else np.minimum
    for dy in range(-r, r + 1):
        for dx in range(-widths[abs(dy)], widths[abs(dy)] + 1):
            out = comb(out, p[r + dy : r + dy + h, r + dx : r + dx + w])
    return out


def soft_disk(h: int, w: int, cy: float, cx: float, radius: float, edge: float = 3.0) -> np.ndarray:
    yy, xx = np.mgrid[0:h, 0:w]
    d = np.sqrt((yy - cy) ** 2 + (xx - cx) ** 2)
    return np.clip((radius - d) / edge + 0.5, 0.0, 1.0).astype(np.float32)


# ------------------------------------------------------------------ image helpers
def test_no_cv2_anywhere():
    for name in ("engine.py", "models.py", "updates.py"):
        text = (ROOT / "bgeditor" / name).read_text(encoding="utf-8")
        assert not re.search(r"^\s*(import|from)\s+cv2", text, re.M), f"{name} imports cv2"
    code = (
        "import sys; import bgeditor.engine, bgeditor.models, bgeditor.updates, bgeditor.pipeline, "
        "bgeditor.options, bgeditor.imageio, bgeditor.ui.main_window, app; print('cv2' in sys.modules)"
    )
    env = dict(os.environ, QT_QPA_PLATFORM="offscreen")
    out = subprocess.run([sys.executable, "-c", code], cwd=str(ROOT), capture_output=True, text=True, timeout=180, env=env)
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "False", "importing the engine loads cv2"


def test_grow_shrink_match_brute_force():
    for seed in range(4):
        g = np.random.default_rng(seed)
        mask = g.random((37, 51)) > 0.93
        for r in (0.0, 1.0, 1.5, 2.0, 3.7, 7.0, 12.5):
            assert np.array_equal(engine.grow(mask, r), brute_within(mask, r)), (seed, r)
            assert np.array_equal(engine.shrink(~mask, r), ~brute_within(mask, r)), (seed, r)
    assert not engine.grow(np.zeros((9, 9), bool), 3).any()
    assert engine.grow(np.ones((9, 9), bool), 3).all()
    assert engine.shrink(np.ones((9, 9), bool), 3).all()  # the image border does not erode
    assert not engine.shrink(np.zeros((9, 9), bool), 3).any()


def test_box_blur_matches_reflect101_reference():
    for shape in ((40, 30), (7, 5), (1, 25), (25, 1), (3, 3)):
        a = rng.random(shape, dtype=np.float32)
        for k in (2, 3, 4, 9, 10, 31):
            got = engine.box_blur(a, k)
            assert got.dtype == np.float32 and got.shape == a.shape
            assert np.abs(got - brute_box(a, k)).max() < 1e-5, (shape, k)
    a = rng.random((600, 700), dtype=np.float32)  # large enough for the worker threads
    assert np.abs(engine.box_blur(a, 90) - ndi.uniform_filter(a, 90, mode="mirror")).max() < 1e-6


def test_gaussian_blur_matches_reference():
    a = soft_disk(60, 80, 30, 35, 18)
    for sigma in (0.5, 1.3, 2.3, 5.1):
        assert np.abs(engine.gaussian_blur(a, sigma) - brute_gaussian(a, sigma)).max() < 1e-5, sigma


def test_ellipse_kernel_and_morphology():
    # Half widths of OpenCV's MORPH_ELLIPSE kernels, recorded from cv2.getStructuringElement.
    expected = {
        1: [1, 0],
        2: [2, 2, 0],
        3: [3, 3, 2, 0],
        5: [5, 5, 5, 4, 3, 0],
        8: [8, 8, 8, 7, 7, 6, 5, 4, 0],
        13: [13, 13, 13, 13, 12, 12, 12, 11, 10, 9, 8, 7, 5, 0],
    }
    for r, widths in expected.items():
        assert engine.ellipse_half_widths(r) == widths, r
    a = soft_disk(70, 90, 30, 50, 20) * rng.random((70, 90)).astype(np.float32)
    for r in (1, 2, 3, 6, 13):
        for dilate in (True, False):
            assert np.array_equal(engine.ellipse_morph(a, r, dilate), brute_morph(a, r, dilate).astype(np.float32)), (r, dilate)
    # finish_edge: positive grows, negative shrinks, softening blurs, all within 0-1.
    alpha = (soft_disk(200, 200, 100, 100, 50, 0.5) > 0.5).astype(np.float32)
    grown = engine.finish_edge(alpha.copy(), Options(edge_shift=5))
    shrunk = engine.finish_edge(alpha.copy(), Options(edge_shift=-5))
    assert grown.sum() > alpha.sum() > shrunk.sum()
    assert grown[100, 154] == 1.0 and alpha[100, 154] == 0.0
    soft = engine.finish_edge(alpha.copy(), Options(edge_soften=3.0))
    assert 0.0 < soft[100, 150] < 1.0 and soft.min() >= 0.0 and soft.max() <= 1.0


def test_resize_modes():
    ramp = np.tile(np.linspace(0, 1, 50, dtype=np.float32), (20, 1))
    up = engine.resize(ramp, (200, 80), "linear")
    assert up.shape == (80, 200) and up.dtype == np.float32 and up.flags.writeable
    inner = up[:, 10:-10]
    assert np.abs(np.diff(inner, axis=1) - np.diff(inner, axis=1).mean()).max() < 1e-5  # stays linear
    tri = np.array([[0, 128, 255], [255, 128, 0]], np.uint8)
    near = engine.resize(tri, (9, 6), "nearest")
    assert set(np.unique(near)) <= {0, 128, 255}
    img = rng.integers(0, 256, (30, 45, 3), dtype=np.uint8)
    area = engine.resize(img, (15, 10), "area")  # exact factor 3: block means
    blocks = img.reshape(10, 3, 15, 3, 3).astype(np.float64).mean(axis=(1, 3))
    assert np.abs(area.astype(int) - np.rint(blocks)).max() <= 1
    # Non-integer factor: each output pixel is the mean of the area it covers.
    a = rng.random((7, 10), dtype=np.float32)
    got = engine.resize(a, (4, 3), "area")

    def weights(n_in, n_out):
        m = np.zeros((n_out, n_in))
        s = n_in / n_out
        for j in range(n_out):
            for i in range(n_in):
                m[j, i] = max(0.0, min(i + 1, (j + 1) * s) - max(i, j * s)) / s
        return m

    want = weights(7, 3) @ a.astype(np.float64) @ weights(10, 4).T
    assert np.abs(got - want).max() < 1e-5
    same = engine.resize(a, (10, 7), "linear")
    assert np.array_equal(same, a) and same is not a


def test_components_and_mask_clean_up():
    diag = np.eye(6, dtype=bool)
    assert engine._components(diag, 8)[1] == 1 and engine._components(diag, 4)[1] == 6
    # fill_enclosed_holes: a hole inside the body closes, a notch open to the border stays.
    m = soft_disk(120, 120, 60, 60, 45, 0.5)
    m[55:65, 55:65] = 0.0  # enclosed
    m[0:40, 58:62] = 0.0  # reaches the border
    filled = engine.fill_enclosed_holes(m)
    assert filled[60, 60] == 1.0 and filled[5, 60] == 0.0
    # keep_main_subject: a separate person far away is dropped, a touching hand is kept.
    m = np.maximum(soft_disk(200, 300, 100, 100, 60, 0.5), soft_disk(200, 300, 100, 255, 30, 0.5))
    m = np.maximum(m, soft_disk(200, 300, 100, 168, 10, 0.5))
    kept, dropped = engine.keep_main_subject(m)
    assert kept[100, 100] == 1.0 and kept[100, 168] > 0.9 and kept[100, 255] == 0.0
    assert dropped is not None and dropped[100, 255] == 1 and dropped[100, 100] == 0
    # merge_loose_strands: a gain connected to the body is taken, a loose speck is not.
    base = soft_disk(100, 100, 50, 50, 20, 0.5)
    extra = base.copy()
    extra[50, 70:90] = 0.6  # a strand touching the body
    extra[5, 5] = 0.9  # a loose speck
    merged = engine.merge_loose_strands(base, extra)
    assert merged[50, 85] == np.float32(0.6) and merged[5, 5] == 0.0


def test_make_trimap_band_widths():
    mask = (soft_disk(400, 400, 200, 200, 120, 0.5) > 0.5).astype(np.float32)
    tri = engine.make_trimap(mask, Options(refine_band=2.0))  # band = 8 px
    assert set(np.unique(tri)) == {0, 128, 255}
    row = tri[200]
    assert row[200] == 255 and row[5] == 0
    unknown = np.flatnonzero(row == 128)
    left = unknown[unknown < 200]
    assert 15 <= left.size <= 18, left.size  # about 8 px on each side of the edge


def test_estimate_foreground_removes_background_colour():
    # A red subject on blue, with a 4 px soft edge: the edge shows purple (0.5, 0.1, 0.5).
    h, w = 60, 200
    alpha = np.tile(np.clip((100 - np.arange(w)) / 4.0 + 0.5, 0.0, 1.0), (h, 1)).astype(np.float32)
    red, blue = np.array([0.9, 0.1, 0.1], np.float32), np.array([0.1, 0.1, 0.9], np.float32)
    image = alpha[..., None] * red + (1 - alpha[..., None]) * blue
    fg = engine.estimate_foreground(image.copy(), alpha)
    soft = (alpha > 0.3) & (alpha < 0.7)
    assert fg[soft][:, 0].mean() > 0.75 and fg[soft][:, 2].mean() < 0.25, fg[soft].mean(axis=0)
    assert np.allclose(fg[alpha == 1.0], red, atol=1e-6)


def test_threaded_filters_equal_single_call():
    a = rng.random((900, 700), dtype=np.float32)
    for fn, kw in ((ndi.uniform_filter1d, {"size": 33, "mode": "mirror"}), (ndi.maximum_filter1d, {"size": 21, "mode": "nearest"}),
                   (ndi.gaussian_filter1d, {"sigma": 4.0, "mode": "mirror"})):
        for axis in (0, 1):
            assert np.array_equal(engine.filter_axis(fn, a, axis, **kw), fn(a, axis=axis, **kw)), (fn.__name__, axis)


# ------------------------------------------------------------------ model I/O
class _Arg:
    def __init__(self, name, shape, type_="tensor(float)"):
        self.name, self.shape, self.type = name, shape, type_


class _FakeSession:
    def __init__(self, inputs, outputs):
        self._inputs, self._outputs = inputs, outputs

    def get_inputs(self):
        return self._inputs

    def get_outputs(self):
        return self._outputs


def test_model_io_reads_the_session():
    io = engine.model_io(_FakeSession([_Arg("input_image", [1, 3, 1024, 1024])], [_Arg("output_image", [1, 1, 1024, 1024])]), 3)
    assert (io.input_name, io.height, io.width, io.output_name, io.dtype) == ("input_image", 1024, 1024, "output_image", np.float32)
    io = engine.model_io(
        _FakeSession([_Arg("IMAGE", ["batch_size", 3, None, "w"], "tensor(float16)")],
                     [_Arg("aux", [1, 64]), _Arg("ALPHA", ["batch_size", 1, "SigmoidALPHA_dim_2", "SigmoidALPHA_dim_3"], "tensor(float16)")]),
        3,
    )
    assert io.input_name == "IMAGE" and io.output_name == "ALPHA" and io.dtype == np.float16 and io.height is None
    spec = replace(models.MODELS["birefnet-lite"], input_size=None)
    assert io.input_size(spec) == (1024, 1024)
    assert io.input_size(replace(spec, input_size=768)) == (768, 768)
    for inputs, outputs in (
        ([_Arg("x", [1, 4, 64, 64])], [_Arg("y", [1, 1, 64, 64])]),  # wrong channels
        ([_Arg("x", [1, 3, 64, 64]), _Arg("z", [1])], [_Arg("y", [1, 1, 64, 64])]),  # an extra input
        ([_Arg("x", [1, 3, 64, 64])], [_Arg("y", [1, 2, 64, 64])]),  # no 1-channel output
        ([_Arg("x", [1, 3, 64, 64], "tensor(uint8)")], [_Arg("y", [1, 1, 64, 64])]),  # not float
    ):
        try:
            engine.model_io(_FakeSession(inputs, outputs), 3)
        except engine.ModelIOError:
            pass
        else:
            raise AssertionError(f"accepted {inputs} -> {outputs}")


def test_to_alpha_logits_and_probabilities():
    raw = np.array([[[[-4.0, 0.0, 4.0]]]], np.float32)
    lite = models.MODELS["birefnet-lite"]
    assert np.allclose(engine.to_alpha(raw, lite), 1 / (1 + np.exp(-raw[0, 0])))
    assert np.allclose(engine.to_alpha(raw, replace(lite, output="probs")), [[0.0, 0.0, 1.0]])


def test_engine_feeds_what_the_model_declares(tmp):
    photo = Image.fromarray(rng.integers(0, 256, (150, 200, 3), dtype=np.uint8))
    with temp_app(tmp):
        dyn = register(tmp, "test-dynamic", conv_model(), input_size=64, family="general")
        fixed = register(tmp, "test-fixed", conv_model(48, 32), input_size=1024, family="general")
        half = register(tmp, "test-fp16", fp16_sigmoid_model(), input_size=40, output="probs", family="general")
        eng = engine.Engine()
        opts = Options(device="cpu", refine_hair=False, decontaminate=False)
        m = eng.segment(photo, replace(opts, model=dyn.key))
        assert m.shape == (64, 64)  # dynamic size: the spec's input_size
        x = engine.seg_input(photo, engine.model_io(engine.open_cpu_session(tmp / "models" / dyn.filename), 3), dyn)
        assert np.abs(m - 1 / (1 + np.exp(-conv_reference(x)))).max() < 1e-5
        assert eng.segment(photo, replace(opts, model=fixed.key)).shape == (48, 32)  # the model's own size wins
        p = eng.segment(photo, replace(opts, model=half.key))
        assert p.shape == (40, 40)
        x = engine.seg_input(photo, engine.model_io(engine.open_cpu_session(tmp / "models" / half.filename), 3), half)
        assert x.dtype == np.float16
        want = 1 / (1 + np.exp(-conv_reference(x.astype(np.float32))))
        assert np.abs(p - want).max() < 2e-3  # probabilities are used as they are, not squeezed again
        fg, alpha = eng.cutout(photo, replace(opts, model=dyn.key, decontaminate=True, edge_shift=2))
        assert fg.shape == (150, 200, 3) and alpha.shape == (150, 200)


# ------------------------------------------------------------------ catalogue
def test_catalogue_round_trip(tmp):
    with temp_app(tmp):
        data = conv_model()
        name = "BiRefNet_v2-matting-test-0123abcd.onnx"
        (tmp / "models" / name).write_bytes(data)
        spec = models.ModelSpec(
            key="birefnet-v2-matting", title="BiRefNet v2 matting", filename=name,
            url="https://github.com/ZhengPeng7/BiRefNet/releases/download/v2/x.onnx", sha256=sha256_of(data),
            size=len(data), licence="mit", input_size=None, output="probs", family="matting",
            licence_note="MIT licence, read from the release notes", discovered=True,
        )
        assert models.preferred_segmenter() == "birefnet-matting"
        models.adopt_model(spec, {"repository": "ZhengPeng7/BiRefNet"})
        assert models.catalog_path() == tmp / "data" / "model-catalog.json" and models.catalog_path().is_file()
        assert models.preferred_segmenter() == spec.key
        assert [s.key for s in models.offline_models()] == [spec.key, "birefnet-lite", "vitmatte-small"]
        assert spec.key in models.model_keys() and "vitmatte-small" not in models.model_keys()
        assert models.MODELS[spec.key] == spec
        # A fresh start reads it back from the file.
        del models.MODELS[spec.key]
        models._preferred = ""
        models.reload_catalog()
        assert models.MODELS[spec.key] == spec and models.preferred_segmenter() == spec.key
        assert models.catalog_entry(spec.key)["evidence"] == {"repository": "ZhengPeng7/BiRefNet"}
        from bgeditor.options import presets

        assert presets()["best"]["model"] == spec.key and presets()["fast"]["model"] == "birefnet-lite"
        assert Options.from_dict({"model": spec.key}).model == spec.key
        # Back to the built-in model; the adopted one stays registered and on disk.
        models.set_preferred("birefnet-matting")
        assert models.preferred_segmenter() == "birefnet-matting" and models.previous_segmenter() == spec.key
        assert (tmp / "models" / name).is_file() and spec.key in models.MODELS
        # Without its file the entry is ignored.
        (tmp / "models" / name).unlink()
        models.reload_catalog()
        assert spec.key not in models.MODELS and models.preferred_segmenter() == "birefnet-matting"
        assert Options.from_dict({"model": spec.key}).model == "birefnet-matting"


def test_catalogue_ignores_malformed_entries(tmp):
    with temp_app(tmp):
        data = conv_model()
        (tmp / "models" / "good.onnx").write_bytes(data)
        good = {
            "key": "birefnet-v2-general", "title": "BiRefNet v2 general", "filename": "good.onnx",
            "url": "https://huggingface.co/x/y/resolve/abc/good.onnx", "sha256": sha256_of(data), "size": len(data),
            "licence": "mit", "licence_note": "", "input_size": 1024, "output": "logits", "family": "general",
            "sha256_verified": True,
        }
        bad = [
            {**good, "key": "bad-unverified", "sha256_verified": False},
            {**good, "key": "birefnet-matting"},  # would replace a built-in model
            {**good, "key": "bad-traversal", "filename": "..\\..\\evil.onnx"},
            {**good, "key": "bad-builtin-file", "filename": "BiRefNet-matting-epoch_100.onnx"},
            {**good, "key": "bad-http", "url": "http://example.com/good.onnx"},
            {**good, "key": "bad-sha", "sha256": "abc"},
            {**good, "key": "bad-size", "size": "12"},
            {**good, "key": "bad-output", "output": "sigmoid"},
            {**good, "key": "bad-family", "family": "refiner"},
            {**good, "key": "bad-input", "input_size": 5},
            {**good, "key": "bad-missing-file", "filename": "missing.onnx"},
            {**good, "key": "Bad Key"},
            "not a dict",
        ]
        path = tmp / "data" / "model-catalog.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"version": 1, "preferred": "bad-http", "models": bad + [good]}), encoding="utf-8")
        models.reload_catalog()
        discovered = [k for k, s in models.MODELS.items() if s.discovered]
        assert discovered == ["birefnet-v2-general"], discovered
        assert models.preferred_segmenter() == "birefnet-matting"  # the preferred entry was invalid
        path.write_text("{ not json", encoding="utf-8")
        models.reload_catalog()
        assert not [k for k, s in models.MODELS.items() if s.discovered]


# ------------------------------------------------------------------ licence gate
def test_licence_gate():
    ok = updates.licence_verdict
    assert ok("mit", {"model card": "---\nlicense: mit\n---\nBiRefNet"}, ["ZhengPeng7/BiRefNet_v2-matting"]) == ""
    assert ok("Apache-2.0", {}, ["x"]) == "" and ok("bsd-3-clause", {}, ["x"]) == ""
    assert ok("MIT", {"release notes": "Commercial use allowed; the MIT license applies."}, ["a.onnx"]) == ""
    cases = {
        "cc-by-nc-4.0": ok("cc-by-nc-4.0", {}, ["x"]),
        "card CC BY-NC": ok("mit", {"model card": "Weights: CC BY-NC 4.0"}, ["x"]),
        "research only": ok("mit", {"model card": "For research purposes only."}, ["x"]),
        "research-only": ok("mit", {"README section": "These weights are research-only."}, ["x"]),
        "non-commercial": ok("mit", {"release notes": "Non-commercial use"}, ["x"]),
        "academic": ok("mit", {"release notes": "academic version, trained on DIS5K"}, ["x"]),
        "DINOv3 text": ok("mit", {"model card": "Backbone: DINOv3 ViT-L/16 (frozen)."}, ["x"]),
        "DINOv3 licence id": ok("other (dinov3-license)", {}, ["x"]),
        "other": ok("other", {}, ["x"]),
        "no licence": ok("", {}, ["x"]),
        "gated": ok("mit", {}, ["x"], gated=True),
        "NC token": ok("mit", {}, ["BiRefNet_v2-matting-NC.onnx"]),
        "conversion differs": ok("mit", {}, ["x"], base_licence="cc-by-nc-4.0"),
        "gpl": ok("gpl-3.0", {}, ["x"]),
    }
    for label, reason in cases.items():
        assert reason, f"{label}: accepted"
    assert "DINOv3" in cases["DINOv3 text"] and "gated" in cases["gated"]
    assert updates.licence_in_text("BiRefNet_v2-matting.onnx - MIT License") == "mit"
    assert updates.licence_in_text("weights under Apache License 2.0") == "apache-2.0"
    assert updates.licence_in_text("BSD 3-Clause") == "bsd-3-clause"
    assert updates.licence_in_text("submitted") == ""


def test_names_and_markers():
    assert updates.has_v2_marker("BiRefNet_v2-matting-epoch_100.onnx") and updates.has_v2_marker("BiRefNet v2.0")
    assert not updates.has_v2_marker("Gayrat1968/BiRefNetV2") and not updates.has_v2_marker("BiRefNet-general-epoch_244")
    assert updates.is_v2_tag("v2") and updates.is_v2_tag("v2.1.3") and not updates.is_v2_tag("v1") and not updates.is_v2_tag("v20")
    assert updates.family_of("BiRefNet_v2-matting-epoch_9.onnx") == "matting"
    assert updates.family_of("ZhengPeng7/BiRefNet_v2-portrait", "onnx/model.onnx") == "portrait"
    assert updates.family_of("BiRefNet_v2-epoch_9.onnx") == "general"
    assert updates.family_of("BiRefNet_lite-v2-epoch_1.onnx") == "excluded"
    assert updates.family_of("BiRefNet_v2-COD-epoch_1.onnx") == "excluded"
    assert updates.family_of("BiRefNet_v2_lite-matting-epoch_110.onnx") == "excluded"
    assert updates.family_of("onnx-community/BiRefNet_lite-ONNX", "onnx/model.onnx") == "excluded"


def test_onnx_probe_on_tiny_models(tmp):
    p = tmp / "conv.onnx"
    p.write_bytes(conv_model())
    sig = updates.probe_model(updates._ModelBytes(path=p))
    assert sig["ir_version"] == 8 and sig["opsets"] == {"ai.onnx": 17}
    assert sig["inputs"] == [("x", "float", [1, 3, "h", "w"])] and sig["outputs"] == [("y", "float", [1, 1, "h", "w"])]
    assert sig["producers"] == {"y": "Conv"} and updates.probe_reason(sig) == ""
    p.write_bytes(fp16_sigmoid_model())
    sig = updates.probe_model(updates._ModelBytes(path=p))
    assert sig["inputs"][0][1] == "float16" and sig["producers"] == {"y": "Sigmoid"} and updates.probe_reason(sig) == ""
    p.write_bytes(deform_model())
    assert "DeformConv" in updates.probe_reason(updates.probe_model(updates._ModelBytes(path=p)))


def test_validation_rejects_with_clear_reasons(tmp):
    spec = replace(models.MODELS["birefnet-lite"], key="test", discovered=True)
    p = tmp / "deform.onnx"
    p.write_bytes(deform_model())
    v = updates.validate_model(p, spec, baseline_seconds=1.0)
    assert not v.ok and "ONNX Runtime cannot load it" in v.reason and "DeformConv" in v.reason, v.reason
    p = tmp / "conv.onnx"
    p.write_bytes(conv_model())
    v = updates.validate_model(p, spec, baseline_seconds=1.0)
    assert not v.ok and "differs too much" in v.reason and v.output == "logits" and v.producer == "Conv", v
    p = tmp / "fp16.onnx"
    p.write_bytes(fp16_sigmoid_model())
    v = updates.validate_model(p, spec, baseline_seconds=1.0)
    assert not v.ok and v.output == "probs" and v.producer == "Sigmoid" and v.input_size is None, v
    p = tmp / "two-inputs.onnx"
    p.write_bytes(onnx_model([_node("Add", ["a", "b"], ["y"])], [], [_value_info("a", 1, [1, 3, 8, 8]), _value_info("b", 1, [1, 3, 8, 8])],
                             [_value_info("y", 1, [1, 3, 8, 8])]))
    v = updates.validate_model(p, spec, baseline_seconds=1.0)
    assert not v.ok and "inputs or outputs do not fit" in v.reason, v.reason


# ------------------------------------------------------------------ update check (recorded responses)
def fixture(name: str):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


class FakeWeb:
    """Serves recorded API responses and model bytes (with Range); counts every request."""

    def __init__(self):
        meta = fixture("responses_meta.json")["responses"]
        self.routes: dict[str, tuple[int, dict, bytes]] = {}
        self.files: dict[str, bytes] = {}
        self.heads: dict[str, dict] = {}
        self.calls: list[tuple[str, str, dict]] = []
        self.json(updates.URL_GH_RELEASES, fixture("gh_releases.json"), meta["gh_releases"]["headers"]["ETag"])
        self.json(updates.URL_GH_REPOS, fixture("gh_repos.json"), meta["gh_repos"]["headers"]["ETag"])
        self.json(updates.URL_HF_AUTHOR, fixture("hf_author.json"), meta["hf_author"]["headers"]["ETag"])
        self.json(updates.URL_HF_CONVERTED, fixture("hf_converted.json"), meta["hf_onnx"]["headers"]["ETag"])

    def json(self, url, data, etag=None, status=200, headers=None):
        h = {"etag": etag} if etag else {}
        h.update({k.lower(): v for k, v in (headers or {}).items()})
        self.routes[url] = (status, h, json.dumps(data).encode("utf-8"))

    def text(self, url, text):
        self.routes[url] = (200, {}, text.encode("utf-8"))

    def __call__(self, url, method, headers, redirect, limit):
        self.calls.append((method, url, dict(headers)))
        if method == "HEAD" and url in self.heads:
            return updates.Reply(302, self.heads[url], b"", url)
        if url in self.files:
            data = self.files[url]
            m = re.fullmatch(r"bytes=(\d+)-(\d+)", headers.get("Range", ""))
            if m:
                return updates.Reply(206, {}, data[int(m.group(1)) : int(m.group(2)) + 1], url)
            return updates.Reply(200, {"content-length": str(len(data))}, b"" if method == "HEAD" else data, url)
        if url not in self.routes:
            return updates.Reply(404, {}, b"", url)
        status, h, body = self.routes[url]
        if h.get("etag") and headers.get("If-None-Match") == h["etag"]:
            return updates.Reply(304, h, b"", url)
        return updates.Reply(status, h, body, url)


def v2_release(asset_name: str, data: bytes, body: str, label=None, digest=True, extra_assets=()) -> dict:
    return {
        "id": 900001, "html_url": "https://github.com/ZhengPeng7/BiRefNet/releases/tag/v2", "tag_name": "v2",
        "name": "BiRefNet-v2", "draft": False, "prerelease": False, "created_at": "2026-10-01T08:00:00Z",
        "published_at": "2026-10-01T08:00:00Z", "updated_at": "2026-10-01T08:00:00Z", "body": body,
        "assets": [
            {"id": 800001, "name": asset_name, "label": label, "content_type": "application/octet-stream", "state": "uploaded",
             "size": len(data), "digest": ("sha256:" + sha256_of(data)) if digest else None,
             "created_at": "2026-10-01T08:00:00Z", "updated_at": "2026-10-01T08:00:00Z",
             "browser_download_url": f"https://github.com/ZhengPeng7/BiRefNet/releases/download/v2/{asset_name}"},
            *extra_assets,
        ],
    }


def with_release(web: FakeWeb, release: dict) -> None:
    web.json(updates.URL_GH_RELEASES, [release] + fixture("gh_releases.json"), 'W/"v2-release"')
    for a in release["assets"]:
        web.files.setdefault(a["browser_download_url"], b"")
    web.text("https://raw.githubusercontent.com/ZhengPeng7/BiRefNet/HEAD/README.md",
             "# BiRefNet\n\n## Model zoo\n\n| weights | data |\n|---|---|\n| BiRefNet_v2-matting-epoch_100 | P3M, AM-2k |\n\n"
             "## Acknowledgement\n\nSome third-party weights are for non-commercial use only.\n")


def test_update_check_on_recorded_responses(tmp):
    with temp_app(tmp):
        web = FakeWeb()
        updates._transport = web
        assert updates.is_check_due()
        report = updates.check_for_model_updates("auto", force=True)
        assert report.status == "up-to-date", report
        assert len(web.calls) == 4 and all(m == "GET" for m, _, _ in web.calls)  # one per listing, nothing else
        state = json.loads((tmp / "data" / "model-updates.json").read_text(encoding="utf-8"))
        assert len(state["known"]["gh_assets"]) == 28 and len(state["known"]["hf"]) == 26
        assert set(state["etags"]) == {updates.URL_GH_RELEASES, updates.URL_GH_REPOS, updates.URL_HF_AUTHOR, updates.URL_HF_CONVERTED}
        assert not state["candidates"] and state["baselined"] is True
        assert not updates.is_check_due() and updates.last_status()["last_check"] == state["last_check"]
        assert updates.last_status()["preferred"] == "birefnet-matting"
        # The same listings again: every request is conditional and answered 304.
        web.calls.clear()
        report = updates.check_for_model_updates("auto", force=True)
        assert report.status == "up-to-date", report
        assert len(web.calls) == 4 and all(h.get("If-None-Match") for _, _, h in web.calls)
        assert updates.check_for_model_updates("auto").status == "skipped"  # not due for a week
        assert updates.check_for_model_updates("off").status == "skipped"
        # Parsing the real listings finds nothing that is v2.
        found = updates._Findings()
        st = {"known": {}, "candidates": {}}
        updates._scan_github_releases(updates.MAIN_REPO, fixture("gh_releases.json"), None, st, found, baseline=True)
        assert not found.candidates and not found.notices and len(st["known"]["gh_assets"]) == 28
        names = [a["name"] for r in fixture("gh_releases.json") for a in r["assets"]]
        pattern = re.compile(r"^BiRefNet(?P<variant>_[A-Za-z]+)?-(?P<rest>.+?)-epoch_(?P<epoch>[0-9]+)[.](?P<ext>onnx|pth)$")
        assert all(pattern.match(n) for n in names) and not any(updates.has_v2_marker(n) for n in names)


def test_update_check_offers_a_v2_github_file(tmp):
    data = conv_model()
    with temp_app(tmp):
        web = FakeWeb()
        updates._transport = web
        assert updates.check_for_model_updates("ask", force=True).status == "up-to-date"
        name = "BiRefNet_v2-matting-epoch_100.onnx"
        with_release(web, v2_release(name, data, f"{name}: MIT license. Commercial use allowed."))
        web.files[f"https://github.com/ZhengPeng7/BiRefNet/releases/download/v2/{name}"] = data
        web.calls.clear()
        report = updates.check_for_model_updates("ask", force=True)
        assert report.status == "notified" and report.candidate.startswith("gh:ZhengPeng7/BiRefNet:800001:"), report
        assert report.licence == "mit" and "BiRefNet v2 matting" in report.message
        ranged = [h for m, u, h in web.calls if u.endswith(name)]
        assert ranged and all("Range" in h for h in ranged)  # probed from a distance, not downloaded
        state = json.loads((tmp / "data" / "model-updates.json").read_text(encoding="utf-8"))
        assert state["candidates"][report.candidate]["status"] == "notified"
        assert state["candidates"][report.candidate]["licence_source"] == "the release notes"
        # In ask mode it is offered again at the next check; nothing was adopted.
        assert updates.check_for_model_updates("ask", force=True).candidate == report.candidate
        assert models.preferred_segmenter() == "birefnet-matting"


def test_update_check_reasons_for_not_switching(tmp):
    name = "BiRefNet_v2-matting-epoch_100.onnx"
    url = f"https://github.com/ZhengPeng7/BiRefNet/releases/download/v2/{name}"
    scenarios = [
        ("DeformConv", deform_model(), f"{name}: MIT license.", None, True),
        ("CC-BY-NC", conv_model(), "All v2 weights are licensed CC-BY-NC-4.0.", "MIT", True),
        ("no SHA-256", conv_model(), f"{name}: MIT license.", None, False),
        ("no machine-readable licence", conv_model(), "The v2 weights.", None, True),
        ("DINOv3", conv_model(), f"{name}: MIT license. Built on the DINOv3 backbone.", None, True),
    ]
    for i, (expect, data, body, label, digest) in enumerate(scenarios):
        sub = tmp / f"s{i}"
        sub.mkdir()
        with temp_app(sub):
            web = FakeWeb()
            updates._transport = web
            updates.check_for_model_updates("auto", force=True)
            with_release(web, v2_release(name, data, body, label=label, digest=digest))
            web.files[url] = data
            report = updates.check_for_model_updates("auto", force=True)
            assert report.status == "notified" and not report.candidate, (expect, report)
            assert expect in report.message, (expect, report.message)
            assert not any(u == url and "Range" not in h for _, u, h in web.calls), "a full download happened"
            assert models.preferred_segmenter() == "birefnet-matting"
            # Reported once: the next scheduled check stays quiet about the same file.
            state_file = sub / "data" / "model-updates.json"
            state = json.loads(state_file.read_text(encoding="utf-8"))
            state["next_check"] = ""
            state_file.write_text(json.dumps(state), encoding="utf-8")
            again = updates.check_for_model_updates("auto")
            assert again.status == "up-to-date", (expect, again)
    with temp_app(tmp / "pth"):
        web = FakeWeb()
        updates._transport = web
        updates.check_for_model_updates("auto", force=True)
        rel = v2_release("BiRefNet_v2-matting-epoch_100.pth", b"x" * 10, "PyTorch weights")
        with_release(web, rel)
        report = updates.check_for_model_updates("auto", force=True)
        assert report.status == "notified" and "without an ONNX file" in report.message, report


def test_rate_limit_waits_for_the_next_slot(tmp):
    with temp_app(tmp):
        web = FakeWeb()
        updates._transport = web
        reset = int(time.time()) + 3600
        web.json(updates.URL_GH_RELEASES, {"message": "API rate limit exceeded"}, status=403,
                 headers={"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": str(reset)})
        report = updates.check_for_model_updates("auto", force=True)
        assert report.status == "skipped" and "rate limit" in report.message, report
        assert len(web.calls) == 1  # no retry
        state = json.loads((tmp / "data" / "model-updates.json").read_text(encoding="utf-8"))
        assert updates._parse_time(state["blocked_until"]).timestamp() >= reset - 1
        assert updates._parse_time(state["next_check"]) - updates._now() > updates.CHECK_INTERVAL * 0.9
        web.calls.clear()
        assert updates.check_for_model_updates("auto", force=True).status == "skipped"  # even when forced
        assert not web.calls and not updates.is_check_due()
        # A 429 from Hugging Face with a RateLimit header is handled the same way.
        state["blocked_until"] = ""
        (tmp / "data" / "model-updates.json").write_text(json.dumps(state), encoding="utf-8")
        web.json(updates.URL_GH_RELEASES, fixture("gh_releases.json"), 'W/"x"')
        web.json(updates.URL_HF_AUTHOR, {}, status=429, headers={"RateLimit": '"api";r=0;t=120'})
        report = updates.check_for_model_updates("auto", force=True)
        assert report.status == "skipped" and "huggingface.co" in report.message, report


def test_hugging_face_conversion_gates(tmp):
    """A v2 conversion by onnx-community of a v2 author repository, on recorded response shapes."""
    data = conv_model()
    base_id, conv_id = "ZhengPeng7/BiRefNet_v2-matting", "onnx-community/BiRefNet_v2-matting-ONNX"
    base_sha, conv_sha = "a" * 40, "b" * 40
    blobs = fixture("hf_blobs_portrait_onnx.json")
    card = (FIXTURES / "hf_card_portrait_onnx.md").read_text(encoding="utf-8").replace("BiRefNet-portrait", "BiRefNet_v2-matting")

    def web_for(base_card: str) -> FakeWeb:
        web = FakeWeb()
        author = fixture("hf_author.json") + [{
            "id": base_id, "author": "ZhengPeng7", "sha": base_sha, "createdAt": "2026-10-01T00:00:00.000Z",
            "lastModified": "2026-10-01T00:00:00.000Z", "gated": False, "private": False, "tags": ["license:mit"],
            "cardData": {"license": "mit"}, "siblings": [{"rfilename": "README.md"}, {"rfilename": "model.safetensors"}],
        }]
        converted = fixture("hf_converted.json") + [{
            "id": conv_id, "author": "onnx-community", "sha": conv_sha, "createdAt": "2026-10-02T00:00:00.000Z",
            "lastModified": "2026-10-02T00:00:00.000Z", "gated": False, "private": False, "tags": ["onnx", "license:mit"],
            "cardData": {"license": "mit", "base_model": [base_id]},
            "siblings": [{"rfilename": "README.md"}, {"rfilename": "onnx/model.onnx"}, {"rfilename": "onnx/model_fp16.onnx"}],
        }]
        web.json(updates.URL_HF_AUTHOR, author, 'W/"a2"')
        web.json(updates.URL_HF_CONVERTED, converted, 'W/"c2"')
        web.json(f"{updates.HF}/api/models/{base_id}?blobs=true", {
            "id": base_id, "sha": base_sha, "gated": False, "cardData": {"license": "mit"}, "createdAt": "2026-10-01T00:00:00.000Z",
            "siblings": [{"rfilename": "README.md", "size": 10}, {"rfilename": "model.safetensors", "size": 5, "lfs": {"sha256": "c" * 64, "size": 5}}],
        })
        web.text(f"{updates.HF}/{base_id}/raw/{base_sha}/README.md", base_card)
        web.text(f"{updates.HF}/{base_id}/raw/main/README.md", base_card)
        info = dict(blobs, id=conv_id, sha=conv_sha, createdAt="2026-10-02T00:00:00.000Z", cardData={"license": "mit", "base_model": [base_id]})
        info["siblings"] = [s for s in blobs["siblings"] if s["rfilename"] != "onnx/model.onnx"] + [
            {"rfilename": "onnx/model.onnx", "size": len(data), "lfs": {"sha256": sha256_of(data), "size": len(data), "pointerSize": 134}}]
        web.json(f"{updates.HF}/api/models/{conv_id}?blobs=true", info)
        web.text(f"{updates.HF}/{conv_id}/raw/{conv_sha}/README.md", card)
        url = f"{updates.HF}/{conv_id}/resolve/{conv_sha}/onnx/model.onnx"
        web.heads[url] = {"x-linked-etag": f'"{sha256_of(data)}"', "x-linked-size": str(len(data)), "x-repo-commit": conv_sha}
        web.files[url] = data
        return web

    with temp_app(tmp / "dino"):
        web = web_for("---\nlicense: mit\n---\n# BiRefNet v2\nBackbone: DINOv3 ViT-L/16, frozen.\n")
        updates._transport = web
        report = updates.check_for_model_updates("auto", force=True)
        assert report.status == "notified" and "DINOv3" in report.message and not report.candidate, report
        assert not any(m == "HEAD" or "Range" in h for m, _, h in web.calls), "probed a file that fails the licence gate"

    with temp_app(tmp / "clean"):
        web = web_for("---\nlicense: mit\n---\n# BiRefNet v2 matting\nMIT licensed weights, commercial use allowed.\n")
        updates._transport = web
        downloads = []

        def fake_download(spec, progress=None, cancel=None):  # the real downloader is covered elsewhere
            downloads.append(spec.url)
            target = models.user_model_dir() / spec.filename
            target.write_bytes(web.files[spec.url])
            return target

        saved = models.download_model
        models.download_model = fake_download
        try:
            report = updates.check_for_model_updates("auto", force=True)
        finally:
            models.download_model = saved
        # Every licence gate passes; the tiny stand-in model then fails the mask check.
        assert downloads == [f"{updates.HF}/{conv_id}/resolve/{conv_sha}/onnx/model.onnx"], downloads
        assert report.status == "notified" and "differs too much from the reference" in report.message, report
        assert (models.user_model_dir() / f"BiRefNet_v2-matting-ONNX-model-{sha256_of(data)[:8]}.onnx").is_file()  # never deleted
        assert models.preferred_segmenter() == "birefnet-matting"
        state = json.loads((tmp / "clean" / "data" / "model-updates.json").read_text(encoding="utf-8"))
        rec = [c for c in state["candidates"].values() if c["filename"] == "onnx/model.onnx"][0]
        assert rec["status"] == "rejected" and rec["base_licence"] == "mit" and rec["original"] is False
        # A rejected file is not downloaded again at the next check.
        downloads.clear()
        models.download_model = fake_download
        try:
            state["next_check"] = ""
            (tmp / "clean" / "data" / "model-updates.json").write_text(json.dumps(state), encoding="utf-8")
            again = updates.check_for_model_updates("auto")
        finally:
            models.download_model = saved
        assert not downloads and again.status in ("up-to-date", "notified"), again


def test_local_lite_model_as_candidate(tmp):
    """The real BiRefNet lite model, treated as a downloaded v2 candidate, passes the technical
    gates and is adopted into a temporary catalogue."""
    lite = models.MODELS["birefnet-lite"]
    source = models.find_model(lite)
    if source is None:
        print("     (skipped: BiRefNet lite is not downloaded)")
        return
    with temp_app(tmp):
        c = updates.Candidate(
            id="gh:ZhengPeng7/BiRefNet:1:test", source="github", repo="ZhengPeng7/BiRefNet",
            filename="BiRefNet_v2-general-epoch_232.onnx",
            url="https://github.com/ZhengPeng7/BiRefNet/releases/download/v2/BiRefNet_v2-general-epoch_232.onnx",
            size=lite.size, sha256=lite.sha256, licence="mit", licence_source="the release notes", family="general",
            published="2026-10-01T00:00:00Z", v2=True, texts={"release notes": "BiRefNet_v2-general-epoch_232.onnx: MIT license"},
            names=["BiRefNet_v2-general-epoch_232.onnx"], evidence=["https://github.com/ZhengPeng7/BiRefNet/releases/tag/v2"], revision="v2",
        )
        assert updates.pre_download_reason(c) == ""
        target = models.user_model_dir() / updates._local_filename(c)
        try:
            os.link(source, target)  # the 'download': same bytes, no copy
        except OSError:
            import shutil

            shutil.copyfile(source, target)
        saved_probe, saved_baseline = updates._probe_remote, updates._baseline_seconds
        updates._probe_remote = lambda cand, http: ""  # no network
        updates._baseline_seconds = lambda photo, cancel: 3600.0  # the speed rule is tested below
        try:
            t0 = time.perf_counter()
            outcome, reason, spec, validation = updates._download_and_validate(c, updates._Session({}, None), None, lambda *a: None)
            seconds = time.perf_counter() - t0
        finally:
            updates._probe_remote, updates._baseline_seconds = saved_probe, saved_baseline
        assert outcome == "ok", (outcome, reason, validation)
        assert validation.iou >= updates.MIN_IOU and validation.mae <= updates.MAX_MAE and validation.output == "logits"
        assert spec.input_size == 1024 and spec.family == "general" and spec.discovered
        print(f"     (lite as candidate: IoU {validation.iou:.3f}, mean difference {validation.mae:.4f}, "
              f"run {validation.seconds:.1f} s, whole check {seconds:.0f} s)")
        updates._adopt(c, spec, validation)
        assert models.preferred_segmenter() == spec.key == "birefnet-v2-general"
        assert target.is_file() and source.is_file()
        entry = models.catalog_entry(spec.key)
        assert entry["evidence"]["licence"] == "mit" and entry["evidence"]["output_producer"] == "Conv"
        # The engine runs the adopted model like any other.
        m = engine.Engine().segment(Image.open(updates.asset_dir() / "selftest_portrait.jpg").convert("RGB"),
                                    Options(model=spec.key, device="cpu", refine_hair=False))
        assert m.shape == (1024, 1024) and 0.2 < float(m.mean()) < 0.9


def test_speed_gate():
    assert updates.speed_reason(10.0, 10.0) == "" and updates.speed_reason(40.0, 10.0) == ""
    assert "too slow" in updates.speed_reason(40.1, 10.0)
    assert updates.speed_reason(5.0, 0.0) == ""  # no baseline measured: validate_model refuses earlier


# ------------------------------------------------------------------ runner
def main() -> int:
    tests = [(name, fn) for name, fn in globals().items() if name.startswith("test_") and callable(fn)]
    failed = 0
    for name, fn in tests:
        t0 = time.perf_counter()
        try:
            if fn.__code__.co_argcount:
                with tempfile.TemporaryDirectory(prefix="bge-test-") as d:
                    fn(Path(d))
            else:
                fn()
        except Exception:
            failed += 1
            print(f"FAIL {name}")
            traceback.print_exc()
        else:
            print(f"ok   {name} ({time.perf_counter() - t0:.1f} s)")
    print(f"{len(tests) - failed} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())

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

"""Cutout engine: person segmentation, hair refinement and edge colour clean-up.

Pipeline for one photo:
1. A BiRefNet-style model predicts a soft person mask at its input size (1024x1024 for the
   built-in models; the size, input type and output kind are read from the model).
2. Optional: keep only the main subject, fill enclosed holes (on the coarse mask).
3. Optional: ViTMatte rebuilds the alpha inside an edge band (trimap), tiled at a
   working resolution so fine strands survive on large photos.
4. The alpha is brought to full resolution; blur-fusion foreground estimation
   removes the old background colour that is mixed into soft edge pixels.

Image processing uses SciPy (scipy.ndimage), NumPy and Pillow only. The filters work in
strips on a few worker threads (NumPy and SciPy release the GIL), which gives the same
result as one call over the whole image.
"""

from __future__ import annotations

import functools
import json
import math
import os
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Callable

import numpy as np
import onnxruntime as ort
from PIL import Image

from . import models, paths
from .options import Options, threads_text

# Side used for a model whose input size is dynamic, unless its ModelSpec gives one.
DEFAULT_INPUT_SIZE = 1024
SEG_SIZE = DEFAULT_INPUT_SIZE  # the built-in segmentation models' fixed size
MATTE_TILE = 1024  # the built-in refiner's fixed size; the real size is read from the model
MATTE_OVERLAP = 256  # at MATTE_TILE; scaled with the refiner's real input size
REFINER = "vitmatte-small"
IMAGENET_MEAN = np.array([0.485, 0.456, 0.406], dtype=np.float32)
IMAGENET_STD = np.array([0.229, 0.224, 0.225], dtype=np.float32)

# GPU use, measured on a Quadro P1000 (4 GB) with onnxruntime-directml 1.24.4:
# - BiRefNet lite runs correctly with DirectML graph fusion disabled (5-6.6 s vs ~11 s CPU).
# - Swin-L BiRefNets (portrait, general) run out of memory, or return all-zero logits and
#   then hang the device. They stay on the CPU unless the user forces the GPU.
# - ViTMatte cannot be created on DirectML at all (E_INVALIDARG), so it always runs on the CPU.
# - Integrated GPUs took ~2 minutes to create a session and then failed; automatic mode
#   only uses discrete GPUs.
GPU_AUTO_MODELS = {"birefnet-lite"}
# The optional second refinement pass looks this far (% of the short side) into the background.
EXTRA_BAND_OUT = 3.0
CPU_ONLY_MODELS = {REFINER}

ort.set_default_logger_severity(3)

Report = Callable[[str], None]


class Cancelled(Exception):
    pass


class ModelIOError(ValueError):
    """The model's inputs or outputs are not of a kind this engine can feed or read."""


@dataclass(frozen=True)
class GpuInfo:
    device_id: int
    name: str
    vram_mb: int
    discrete: bool


@functools.lru_cache(maxsize=1)
def system_directml_usable() -> bool:
    """Windows' own DirectML.dll is present and new enough for ONNX Runtime.

    The app does not ship DirectML (it is not free software); ONNX Runtime delay-loads the
    System32 copy and needs DMLCreateDevice1 (export ordinal 2), which DirectML 1.0 on
    Windows 10 1809-1909 lacks. Without it the PC is treated as having no usable GPU.
    """
    if sys.platform != "win32":
        return False
    import ctypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.LoadLibraryExW.restype = ctypes.c_void_p
    kernel32.LoadLibraryExW.argtypes = [ctypes.c_wchar_p, ctypes.c_void_p, ctypes.c_uint32]
    kernel32.GetProcAddress.restype = ctypes.c_void_p
    kernel32.GetProcAddress.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    kernel32.FreeLibrary.argtypes = [ctypes.c_void_p]
    LOAD_LIBRARY_SEARCH_SYSTEM32 = 0x00000800
    handle = kernel32.LoadLibraryExW("DirectML.dll", None, LOAD_LIBRARY_SEARCH_SYSTEM32)
    if not handle:
        return False
    try:
        return bool(kernel32.GetProcAddress(handle, ctypes.c_void_p(2)))
    finally:
        kernel32.FreeLibrary(handle)


def list_gpus() -> list[GpuInfo]:
    """DirectML adapters as ONNX Runtime sees them, best first (none without usable DirectML)."""
    found: list[GpuInfo] = []
    if not system_directml_usable():
        return found
    try:
        devices = ort.get_ep_devices()
    except AttributeError:
        return found
    for ep in devices:
        if ep.ep_name != "DmlExecutionProvider":
            continue
        meta = dict(ep.device.metadata)
        opts = dict(ep.ep_options)
        try:
            dev_id = int(opts.get("device_id", meta.get("DxgiAdapterNumber", 0)))
        except ValueError:
            continue
        vram = 0
        try:
            vram = int(str(meta.get("DxgiVideoMemory", "0")).split()[0])
        except (ValueError, IndexError):
            pass
        found.append(GpuInfo(dev_id, meta.get("Description", f"GPU {dev_id}"), vram, meta.get("Discrete") == "1"))
    found.sort(key=lambda g: (not g.discrete, -g.vram_mb))
    return found


def _plausible(out: np.ndarray) -> bool:
    """Catch the silent failure mode seen on DirectML: a constant (e.g. all-zero) output."""
    return bool(np.isfinite(out).all()) and float(np.ptp(out)) > 1e-4


def _sigmoid(x: np.ndarray) -> np.ndarray:
    # Clipped so very confident logits do not overflow exp(); the result is the same in float32.
    return 1.0 / (1.0 + np.exp(-np.clip(x, -80.0, 80.0)))


def _check(cancel: threading.Event | None) -> None:
    if cancel is not None and cancel.is_set():
        raise Cancelled()


# ------------------------------------------------------------ model I/O
_FLOAT_TYPES = ("tensor(float)", "tensor(float16)")


@dataclass(frozen=True)
class ModelIO:
    """What a model takes and returns, read from its ONNX session."""

    input_name: str
    input_type: str  # 'tensor(float)' or 'tensor(float16)'
    channels: int
    height: int | None  # None when the model accepts any height
    width: int | None
    output_name: str
    output_type: str

    @property
    def dtype(self) -> type:
        return np.float16 if self.input_type == "tensor(float16)" else np.float32

    def input_size(self, spec: models.ModelSpec) -> tuple[int, int]:
        """(height, width) to feed: the model's own size, or the spec's size where it is dynamic."""
        side = spec.input_size or DEFAULT_INPUT_SIZE
        return (self.height or side, self.width or side)


def _dim(value) -> int | None:
    """A fixed dimension, or None for a symbolic or unknown one."""
    return value if isinstance(value, int) and value > 0 else None


def _fits(shape, channels: int) -> bool:
    shape = list(shape or [])
    return len(shape) == 4 and _dim(shape[0]) in (1, None) and _dim(shape[1]) in (channels, None)


def _describe(args) -> str:
    return ", ".join(f"{a.name} {list(a.shape or [])} {a.type}" for a in args) or "none"


def model_io(sess: ort.InferenceSession, channels: int) -> ModelIO:
    """Read one [1, channels, H, W] float input and one [1, 1, H, W] float output from a session.

    H and W may be fixed or dynamic; the batch may be 1 or dynamic. Extra outputs are allowed
    (only the [1, 1, H, W] one is fetched); extra inputs are not, since they cannot be fed.
    """
    inputs = sess.get_inputs()
    if len(inputs) != 1 or not _fits(inputs[0].shape, channels):
        raise ModelIOError(f"expected one input of shape [1, {channels}, H, W], found: {_describe(inputs)}")
    inp = inputs[0]
    if inp.type not in _FLOAT_TYPES:
        raise ModelIOError(f"the input {inp.name} is {inp.type}; only float32 and float16 are supported")
    outputs = [o for o in sess.get_outputs() if _fits(o.shape, 1)]
    if len(outputs) != 1:
        raise ModelIOError(f"expected one output of shape [1, 1, H, W], found: {_describe(sess.get_outputs())}")
    out = outputs[0]
    if out.type not in _FLOAT_TYPES:
        raise ModelIOError(f"the output {out.name} is {out.type}; only float32 and float16 are supported")
    return ModelIO(
        input_name=inp.name,
        input_type=inp.type,
        channels=channels,
        height=_dim(inp.shape[2]),
        width=_dim(inp.shape[3]),
        output_name=out.name,
        output_type=out.type,
    )


def seg_input(rgb: Image.Image, io: ModelIO, spec: models.ModelSpec) -> np.ndarray:
    """The segmentation model's input for an RGB photo: resized, ImageNet-normalised, NCHW."""
    h, w = io.input_size(spec)
    small = rgb.resize((w, h), Image.Resampling.BILINEAR)
    x = np.asarray(small, dtype=np.float32) / 255.0
    x = (x - IMAGENET_MEAN) / IMAGENET_STD
    return np.ascontiguousarray(x.transpose(2, 0, 1)[None], dtype=io.dtype)


def to_alpha(out: np.ndarray, spec: models.ModelSpec) -> np.ndarray:
    """A [1, 1, H, W] model output as a float32 HxW map in 0-1.

    The sigmoid is applied only to a model that returns logits; applying it to probabilities
    would squeeze the mask into 0.5-0.73.
    """
    m = np.asarray(out)[0, 0].astype(np.float32)
    if spec.output == "logits":
        return _sigmoid(m).astype(np.float32, copy=False)
    return np.clip(m, 0.0, 1.0)


def session_options(gpu: bool, threads: int = 0) -> ort.SessionOptions:
    so = ort.SessionOptions()
    so.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
    if gpu:
        # DirectML requires these two settings; fusion off keeps peak VRAM within 4 GB.
        so.enable_mem_pattern = False
        so.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
        so.add_session_config_entry("ep.dml.disable_graph_fusion", "1")
    else:
        so.intra_op_num_threads = max(0, int(threads))
        # Lean: no arena and no memory pattern. Measured on the 1 GB BiRefNets: peak
        # working set 6 GB instead of 12 GB, same speed and output; with the arena a
        # second large model could fail with 'bad allocation' at the commit limit.
        so.enable_cpu_mem_arena = False
        so.enable_mem_pattern = False
    return so


def open_cpu_session(path: str | os.PathLike, threads: int = 0) -> ort.InferenceSession:
    """A CPU session with the engine's own settings (used to validate a new model)."""
    return ort.InferenceSession(str(path), sess_options=session_options(False, threads), providers=["CPUExecutionProvider"])


class Engine:
    """Holds ONNX sessions between photos.

    Each model is placed on its own: on a small GPU the large segmentation model may
    not fit while another model does. A model that fails on the GPU in automatic mode
    moves to the CPU for the rest of the session; failures that will happen every time
    (an empty result, an operator the driver rejects, a hung device) are also remembered
    on disk for this GPU, so the next start does not waste time on them.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()  # warm-up and processing may come from different threads
        self._run_options = ort.RunOptions()
        self._aborted = False
        self._sessions: dict[tuple, ort.InferenceSession] = {}
        self._io_known: dict[str, tuple[str, ModelIO]] = {}  # key -> (file name, I/O)
        self._placement: dict[str, str] = {}
        self._gpu_blocked = _load_gpu_blocklist()  # persisted, deterministic failures
        self._gpu_demoted: set[str] = set()  # this session only
        self._warm_generation = 0
        self.device_label = ""

    # ----------------------------------------------------------- models
    @staticmethod
    def _spec(key: str) -> models.ModelSpec:
        spec = models.MODELS.get(key)
        if spec is None:
            raise ValueError(f"The model '{key}' is not available. Choose another model in the settings.")
        return spec

    @staticmethod
    def required_models(opts: Options) -> list[models.ModelSpec]:
        needed = [Engine._spec(opts.model)]
        if opts.refine_hair:
            needed.append(Engine._spec(REFINER))
        return needed

    def reset_gpu_compat(self) -> None:
        """Forget every recorded GPU failure (after a driver update, for example)."""
        with self._lock:
            self._gpu_blocked.clear()
            self._gpu_demoted.clear()
            _save_gpu_blocklist(self._gpu_blocked)

    def _gpu_choice(self, key: str, opts: Options) -> GpuInfo | None:
        if opts.device == "cpu" or key in CPU_ONLY_MODELS:
            return None
        gpus = list_gpus()
        if opts.device == "gpu":
            if not gpus:
                raise RuntimeError("No DirectML-capable GPU was found. Choose CPU or Automatic.")
            return gpus[0]
        # Automatic: only discrete GPUs, only models known to give correct results there.
        gpus = [g for g in gpus if g.discrete]
        if not gpus or key not in GPU_AUTO_MODELS:
            return None
        gpu = gpus[0]
        gk = _gpu_key(key, gpu)
        if gk in self._gpu_blocked or gk in self._gpu_demoted:
            return None
        return gpu

    def _demote(self, key: str, gpu: GpuInfo, exc: BaseException) -> None:
        """Move a model off this GPU; persist only failures that would repeat every time."""
        gk = _gpu_key(key, gpu)
        self._gpu_demoted.add(gk)
        if _is_deterministic_gpu_failure(exc):
            self._gpu_blocked.add(gk)
            _save_gpu_blocklist(self._gpu_blocked)
        for k in [k for k in self._sessions if k[0] == key and k[1] != "cpu"]:
            del self._sessions[k]

    def _session(self, key: str, opts: Options, gpu: GpuInfo | None) -> ort.InferenceSession:
        spec = self._spec(key)
        path = models.find_model(spec)
        if path is None:
            raise FileNotFoundError(f"The model '{spec.title}' is missing. Start again to download it.")
        threads = opts.cpu_threads if opts.cpu_threads > 0 else 0
        cache_key = (key, gpu.device_id if gpu else "cpu", threads, spec.filename)
        sess = self._sessions.get(cache_key)
        if sess is not None:
            return sess
        # Keep memory in check: models are up to 1 GB each, so drop sessions no longer needed.
        wanted = {m.key for m in self.required_models(opts)}
        for k in [k for k in self._sessions if k[0] not in wanted or k[0] == key]:
            del self._sessions[k]

        so = session_options(gpu is not None, threads)
        if gpu is not None:
            providers = [("DmlExecutionProvider", {"device_id": gpu.device_id}), "CPUExecutionProvider"]
        else:
            providers = ["CPUExecutionProvider"]
        sess = ort.InferenceSession(str(path), sess_options=so, providers=providers)
        self._sessions[cache_key] = sess
        return sess

    def _ready_session(self, key: str, opts: Options) -> ort.InferenceSession:
        """The session for this model, created on the GPU or CPU as _gpu_choice decides."""
        gpu = self._gpu_choice(key, opts)
        try:
            return self._session(key, opts, gpu)
        except (Cancelled, FileNotFoundError):
            raise
        except Exception as exc:
            if gpu is None or opts.device == "gpu":
                raise
            self._demote(key, gpu, exc)
            return self._session(key, opts, None)

    def _io(self, key: str, opts: Options, channels: int) -> ModelIO:
        """The model's input and output names, types and sizes (a session is created if needed)."""
        spec = self._spec(key)
        known = self._io_known.get(key)
        if known is not None and known[0] == spec.filename:
            return known[1]
        with self._lock:
            if self._aborted:
                raise Cancelled()
            io = model_io(self._ready_session(key, opts), channels)
            self._io_known[key] = (spec.filename, io)
            return io

    def abort(self) -> None:
        """Stop the model run in progress (safe to call from another thread)."""
        self._aborted = True
        self._run_options.terminate = True

    def _reset_abort(self) -> None:
        self._aborted = False
        self._run_options = ort.RunOptions()

    def cancel_warm_up(self) -> None:
        """Make a running warm_up stop before its next model (safe from any thread)."""
        self._warm_generation += 1

    def warm_up(self, opts: Options) -> None:
        """Load the sessions for these options ahead of time (model loading takes seconds).

        A single model load cannot be interrupted; cancel_warm_up() stops before the next one.
        """
        generation = self._warm_generation
        _ndimage()  # imported here, on the worker thread, rather than during the first photo
        with self._lock:
            for spec in self.required_models(opts):
                if generation != self._warm_generation or models.find_model(spec) is None:
                    return
                self._ready_session(spec.key, opts)

    def _run(
        self,
        key: str,
        feeds: dict[str, np.ndarray],
        opts: Options,
        on_device: Callable[[str], None] | None,
        output: str,
    ) -> np.ndarray:
        with self._lock:
            return self._run_locked(key, feeds, opts, on_device, output)

    def _run_once(self, sess: ort.InferenceSession, feeds: dict[str, np.ndarray], output: str) -> np.ndarray:
        try:
            return sess.run([output], feeds, self._run_options)[0]
        except Exception:
            if self._aborted:  # ORT reports RunOptions.terminate as an ordinary error
                raise Cancelled() from None
            raise

    def _run_locked(self, key, feeds, opts, on_device, output) -> np.ndarray:
        if self._aborted:
            raise Cancelled()
        gpu = self._gpu_choice(key, opts)
        try:
            sess = self._session(key, opts, gpu)
            out = self._run_once(sess, feeds, output)
            if gpu is not None and not _plausible(out):
                raise _ImplausibleOutput(f"{gpu.name} returned an empty result for {self._spec(key).title}")
        except (Cancelled, FileNotFoundError):
            raise
        except Exception as exc:
            if gpu is None or opts.device == "gpu":
                raise
            # Automatic mode: this model does not run on this GPU; continue on the CPU.
            self._demote(key, gpu, exc)
            gpu = None
            if self._aborted:
                raise Cancelled() from None
            sess = self._session(key, opts, None)
            out = self._run_once(sess, feeds, output)
        else:
            if gpu is not None and opts.device == "gpu":
                # A forced run that works clears an old record for this model and GPU.
                gk = _gpu_key(key, gpu)
                if gk in self._gpu_blocked:
                    self._gpu_blocked.discard(gk)
                    _save_gpu_blocklist(self._gpu_blocked)
        self._placement[key] = gpu.name if gpu is not None else "CPU"
        self._announce(opts, on_device)
        return out

    def _announce(self, opts: Options, on_device: Callable[[str], None] | None) -> None:
        used = [self._placement[m.key] for m in self.required_models(opts) if m.key in self._placement]
        places = list(dict.fromkeys(used))
        if not places:
            return
        threads = threads_text(opts.cpu_threads)
        if places == ["CPU"]:
            label = f"CPU ({threads})"
        elif len(places) == 1:
            label = f"GPU — {places[0]}"
        else:
            parts = []
            for m in self.required_models(opts):
                where = self._placement.get(m.key)
                if where:
                    role = "hair" if m.family == "refiner" else "person"
                    parts.append(f"{role}: {'CPU' if where == 'CPU' else 'GPU'}")
            gpu_name = next(p for p in places if p != "CPU")
            label = f"{gpu_name} + CPU ({', '.join(parts)})"
        if label != self.device_label:
            self.device_label = label
            if on_device is not None:
                on_device(label)

    # ------------------------------------------------------------ steps
    def segment(self, rgb: Image.Image, opts: Options, on_device=None) -> np.ndarray:
        """Soft person mask (float32 0-1) at the model's output size (1024x1024 for the built-ins)."""
        spec = self._spec(opts.model)
        io = self._io(opts.model, opts, 3)
        out = self._run(opts.model, {io.input_name: seg_input(rgb, io, spec)}, opts, on_device, io.output_name)
        return to_alpha(out, spec)

    def refine(
        self,
        image: np.ndarray,
        trimap: np.ndarray,
        opts: Options,
        cancel: threading.Event | None,
        report: Report,
        on_device=None,
        label: str = "Refining hair",
    ) -> np.ndarray:
        """ViTMatte over the unknown band. image: HxWx3 uint8, trimap: HxW uint8 (0/128/255)."""
        h, w = trimap.shape
        unknown = trimap == 128
        alpha = (trimap == 255).astype(np.float32)
        if not unknown.any():
            return alpha
        io = self._io(REFINER, opts, 4)
        th, tw = io.input_size(self._spec(REFINER))

        if h <= th * 1.25 and w <= tw * 1.25:
            # Small enough for one pass at the model's size.
            report(label)
            pred = self._matte_tile(image, trimap, True, io, opts, on_device)
            alpha[unknown] = pred[unknown]
            return alpha

        pad_h = max(0, th - h)
        pad_w = max(0, tw - w)
        if pad_h or pad_w:
            # Pad with the ImageNet mean colour (normalises to 0, like the letterbox path)
            # and mark the padding as background.
            image = _pad(image, pad_h, pad_w, MEAN_RGB_U8)
            trimap = _pad(trimap, pad_h, pad_w, 0)
        ph, pw = trimap.shape
        over_y = max(1, MATTE_OVERLAP * th // MATTE_TILE)
        over_x = max(1, MATTE_OVERLAP * tw // MATTE_TILE)
        ys = list(range(0, max(1, ph - th) + 1, th - over_y))
        xs = list(range(0, max(1, pw - tw) + 1, tw - over_x))
        if ys[-1] + th < ph:
            ys.append(ph - th)
        if xs[-1] + tw < pw:
            xs.append(pw - tw)
        unknown_p = trimap == 128
        tiles = [(y, x) for y in ys for x in xs if unknown_p[y : y + th, x : x + tw].any()]

        def ramp(n: int, overlap: int) -> np.ndarray:
            r = np.minimum(np.arange(n) + 1, n - np.arange(n)).astype(np.float32)
            return np.clip(r / overlap, 1e-3, 1.0)

        weight = np.outer(ramp(th, over_y), ramp(tw, over_x))
        acc = np.zeros((ph, pw), dtype=np.float32)
        wsum = np.zeros((ph, pw), dtype=np.float32)
        for i, (y, x) in enumerate(tiles, 1):
            _check(cancel)
            report(f"{label} ({i}/{len(tiles)})")
            tile_img = image[y : y + th, x : x + tw]
            tile_tri = trimap[y : y + th, x : x + tw]
            pred = self._matte_tile(tile_img, tile_tri, False, io, opts, on_device)
            acc[y : y + th, x : x + tw] += pred * weight
            wsum[y : y + th, x : x + tw] += weight
        blended = np.divide(acc, wsum, out=np.zeros_like(acc), where=wsum > 0)[:h, :w]
        alpha[unknown] = blended[unknown]
        return alpha

    def _matte_tile(self, image, trimap, fit_to, io: ModelIO, opts, on_device) -> np.ndarray:
        """Run the refiner on one input of its own size.

        With fit_to set, the image is scaled to fit (aspect ratio kept) and zero-padded,
        as the original ViTMatte inference pads instead of stretching.
        """
        spec = self._spec(REFINER)
        th, tw = io.input_size(spec)
        h, w = trimap.shape
        if fit_to:
            scale = min(th / h, tw / w)
            sw, sh = min(tw, max(1, round(w * scale))), min(th, max(1, round(h * scale)))
            img_in = resize(image, (sw, sh), "area" if scale < 1 else "linear")
            tri_in = resize(trimap, (sw, sh), "nearest")
        else:
            img_in, tri_in = image, trimap
            sh, sw = h, w
        x = np.zeros((1, 4, th, tw), dtype=np.float32)
        rgb = (img_in.astype(np.float32) / 255.0 - IMAGENET_MEAN) / IMAGENET_STD
        x[0, :3, :sh, :sw] = rgb.transpose(2, 0, 1)
        x[0, 3, :sh, :sw] = tri_in.astype(np.float32) / 255.0
        out = self._run(REFINER, {io.input_name: x.astype(io.dtype, copy=False)}, opts, on_device, io.output_name)
        pred = to_alpha(out, spec)[:sh, :sw]
        if (sh, sw) != (h, w):
            pred = resize(np.ascontiguousarray(pred), (w, h), "linear")
        return pred

    # ------------------------------------------------------- full photo
    def cutout(
        self,
        rgb: Image.Image,
        opts: Options,
        cancel: threading.Event | None = None,
        report: Report | None = None,
        on_device: Callable[[str], None] | None = None,
    ) -> tuple[np.ndarray, np.ndarray]:
        """Return (foreground float32 HxWx3, alpha float32 HxW), both 0-1, at the photo's size.

        The alpha already includes the user's edge shift and softening, and the
        foreground colours are estimated for that final alpha.
        """
        say = report or (lambda _t: None)
        with self._lock:
            self._reset_abort()
        _check(cancel)
        W, H = rgb.size
        image = np.asarray(rgb, dtype=np.uint8)

        say("Finding the person")
        coarse = self.segment(rgb, opts, on_device)  # the model's output size
        _check(cancel)
        say("Preparing the edge")
        # Back to the photo's aspect ratio at a modest size for the mask clean-up steps.
        scale = min(1.0, 2048 / max(W, H))
        mw, mh = max(1, round(W * scale)), max(1, round(H * scale))
        mask = resize(coarse, (mw, mh), "linear")
        dropped = None
        if opts.main_subject_only:
            mask, dropped = keep_main_subject(mask)
        if opts.fill_holes:
            mask = fill_enclosed_holes(mask)
        _check(cancel)

        if opts.refine_hair:
            work_scale = min(1.0, opts.refine_resolution / max(W, H))
            ww, wh = max(1, round(W * work_scale)), max(1, round(H * work_scale))
            work_img = image if work_scale == 1.0 else resize(image, (ww, wh), "area")
            work_mask = resize(mask, (ww, wh), "linear")
            dropped_work = None
            if dropped is not None:
                # Removed people stay removed: the refiner must not rebuild them from the image.
                dropped_work = resize(dropped, (ww, wh), "nearest") > 0
            trimap = make_trimap(work_mask, opts)
            if dropped_work is not None:
                trimap[dropped_work] = 0
            _check(cancel)
            alpha_work = self.refine(work_img, trimap, opts, cancel, say, on_device)
            if opts.extra_strand_pass:
                # A wider band finds long loose strands the narrow band misses; only what
                # connects to the person is kept (bokeh and texture give loose scribbles).
                wide = make_trimap(work_mask, opts, band_out=max(opts.refine_band, EXTRA_BAND_OUT))
                if dropped_work is not None:
                    wide[dropped_work] = 0
                _check(cancel)
                extra = self.refine(work_img, wide, opts, cancel, say, on_device, label="Looking for loose strands")
                alpha_work = merge_loose_strands(alpha_work, extra)
        else:
            alpha_work = mask
        _check(cancel)

        alpha = resize(alpha_work, (W, H), "linear")
        np.clip(alpha, 0.0, 1.0, out=alpha)
        # Snap near-certain values; removes faint haze in the background and pinholes in the face.
        alpha[alpha < 0.004] = 0.0
        alpha[alpha > 0.996] = 1.0
        alpha = finish_edge(alpha, opts)

        fg = image.astype(np.float32)
        fg *= 1.0 / 255.0
        if opts.decontaminate:
            _check(cancel)
            say("Cleaning edge colours")
            fg = estimate_foreground(fg, alpha)
        return fg, alpha


class _ImplausibleOutput(RuntimeError):
    pass


# --------------------------------------------------------------- helpers
MEAN_RGB_U8 = tuple(int(round(v * 255)) for v in (0.485, 0.456, 0.406))

# Filters split their work over this many threads; small arrays stay on the calling thread.
_WORKERS = max(1, min(8, os.cpu_count() or 1))
_PARALLEL_MIN = 1 << 18  # elements
_pool_lock = threading.Lock()
_pool: ThreadPoolExecutor | None = None


def _executor() -> ThreadPoolExecutor:
    global _pool
    with _pool_lock:
        if _pool is None:
            _pool = ThreadPoolExecutor(max_workers=_WORKERS, thread_name_prefix="bgeditor-filter")
        return _pool


def _in_strips(fn: Callable[[int, int], None], n: int, elements: int) -> None:
    """Call fn(start, stop) over 0..n in contiguous strips, on worker threads when it pays off.

    fn must only write its own strip. Never call this from inside fn (the pool is shared).
    """
    parts = min(n, _WORKERS) if elements >= _PARALLEL_MIN else 1
    if parts <= 1:
        fn(0, n)
        return
    step = -(-n // parts)
    futures = [_executor().submit(fn, s, min(n, s + step)) for s in range(0, n, step)]
    for f in futures:
        f.result()  # re-raises an error from the strip


def filter_axis(fn, a: np.ndarray, axis: int, **kwargs) -> np.ndarray:
    """Apply a scipy.ndimage 1-D filter along axis 0 or 1 of a 2-D array.

    Rows (axis 1) are filtered in row strips; columns (axis 0) in column strips, each
    transposed so the filter runs over contiguous memory, which is several times faster.
    The result equals fn(a, axis=axis, **kwargs).
    """
    a = np.ascontiguousarray(a)
    h, w = a.shape
    out = np.empty_like(a)
    if axis == 1:

        def rows(y0: int, y1: int) -> None:
            fn(a[y0:y1], axis=1, output=out[y0:y1], **kwargs)

        _in_strips(rows, h, a.size)
    else:

        def cols(x0: int, x1: int) -> None:
            t = np.ascontiguousarray(a[:, x0:x1].T)
            out[:, x0:x1] = fn(t, axis=1, **kwargs).T

        _in_strips(cols, w, a.size)
    return out


def resize(a: np.ndarray, size: tuple[int, int], method: str = "linear") -> np.ndarray:
    """Resize a 2-D float32 or uint8 array, or an HxWx3 uint8 image, to size = (width, height).

    'linear' is Pillow's bilinear filter (when shrinking, Pillow widens the filter, so the
    result is smoothed rather than point-sampled), 'nearest' keeps label and trimap values
    intact, and 'area' gives each output pixel the exact mean of the input area it covers
    when shrinking (bilinear otherwise).
    """
    w, h = int(size[0]), int(size[1])
    if a.dtype == np.float32 and a.ndim == 2:
        pass
    elif a.dtype == np.uint8 and (a.ndim == 2 or (a.ndim == 3 and a.shape[2] == 3)):
        pass
    elif a.dtype == bool and a.ndim == 2:
        a = a.astype(np.uint8)
    else:
        raise TypeError(f"resize: unsupported array {a.dtype} {a.shape}")
    if a.shape[0] == h and a.shape[1] == w:
        return a.copy()
    if method == "area" and w <= a.shape[1] and h <= a.shape[0]:
        return _resize_area(a, w, h)
    resample = Image.Resampling.NEAREST if method == "nearest" else Image.Resampling.BILINEAR
    img = Image.fromarray(np.ascontiguousarray(a))  # mode F, L or RGB
    return np.array(img.resize((w, h), resample))  # a writable copy


def _area_taps(n_in: int, n_out: int) -> tuple[np.ndarray, np.ndarray]:
    """Input indices and weights (n_out x taps) of area averaging from n_in to n_out <= n_in.

    Output sample j covers the input interval [j * s, (j + 1) * s) with s = n_in / n_out;
    each input sample weighs the part of it that lies inside, divided by s.
    """
    scale = n_in / n_out
    start = np.arange(n_out, dtype=np.float64) * scale
    idx = np.floor(start).astype(np.int64)[:, None] + np.arange(int(math.ceil(scale)) + 1)[None, :]
    cover = np.minimum(idx + 1, start[:, None] + scale) - np.maximum(idx, start[:, None])
    weight = (np.clip(cover, 0.0, None) / scale).astype(np.float32)
    return np.minimum(idx, n_in - 1), weight  # taps past the end have weight 0


def _resize_area(a: np.ndarray, w: int, h: int) -> np.ndarray:
    """Shrink by exact area averaging (separable), in output row strips on worker threads."""
    iy, wy = _area_taps(a.shape[0], h)
    ix, wx = _area_taps(a.shape[1], w)
    tail = (1,) * (a.ndim - 2)
    out = np.empty((h, w) + a.shape[2:], dtype=np.float32)

    def rows(j0: int, j1: int) -> None:
        acc = None
        for k in range(iy.shape[1]):
            part = a[iy[j0:j1, k]].astype(np.float32)
            part *= wy[j0:j1, k].reshape((-1, 1) + tail)
            acc = part if acc is None else np.add(acc, part, out=acc)
        res = None
        for k in range(ix.shape[1]):
            part = acc[:, ix[:, k]]
            part *= wx[:, k].reshape((1, -1) + tail)
            res = part if res is None else np.add(res, part, out=res)
        out[j0:j1] = res

    _in_strips(rows, h, a.size)
    if a.dtype == np.uint8:
        return np.clip(np.rint(out), 0, 255).astype(np.uint8)
    return out


def _pad(a: np.ndarray, pad_h: int, pad_w: int, value) -> np.ndarray:
    """Pad at the bottom and right with a constant (one value per channel for colour images)."""
    h, w = a.shape[:2]
    out = np.empty((h + pad_h, w + pad_w) + a.shape[2:], dtype=a.dtype)
    out[...] = value
    out[:h, :w] = a
    return out


def _column_gap(mask: np.ndarray, cap: int) -> np.ndarray:
    """Per pixel, the vertical distance to the nearest True pixel in the same column (int32).

    Columns without a True pixel get a distance larger than cap.
    """
    h, w = mask.shape
    far = np.int32(h + cap + 1)
    rows = np.arange(h, dtype=np.int32)[:, None]
    gap = np.empty((h, w), dtype=np.int32)

    def cols(x0: int, x1: int) -> None:
        m = mask[:, x0:x1]
        above = np.where(m, rows, -far)
        np.maximum.accumulate(above, axis=0, out=above)
        below = np.where(m, rows, h - 1 + far)
        below = np.minimum.accumulate(below[::-1], axis=0)[::-1]
        np.minimum(rows - above, below - rows, out=gap[:, x0:x1])

    _in_strips(cols, w, mask.size)
    return gap


def _within(mask: np.ndarray, r: float) -> np.ndarray:
    """Pixels whose Euclidean distance to the nearest True pixel is at most r (exact).

    The same as thresholding an exact Euclidean distance transform, in two separable steps:
    the vertical distance g to the nearest True pixel of each column, then for every
    horizontal offset dx the pixels with dx^2 + g^2 <= r^2. The work grows with r (at most a
    few hundred pixels here) and is split over rows.
    """
    h, w = mask.shape
    if r < 0 or not mask.any():
        return np.zeros((h, w), dtype=bool)
    reach = int(math.floor(r))
    gap = _column_gap(mask, reach)
    r2 = r * r
    heights = [math.isqrt(int(math.floor(r2 - dx * dx))) for dx in range(min(reach, w - 1) + 1)]
    out = np.zeros((h, w), dtype=bool)

    def rows(y0: int, y1: int) -> None:
        g = gap[y0:y1]
        o = out[y0:y1]
        cond = None
        last = None
        for dx, height in enumerate(heights):
            if height != last:
                cond = g <= height
                last = height
            if dx == 0:
                o |= cond
            else:
                o[:, dx:] |= cond[:, :-dx]
                o[:, :-dx] |= cond[:, dx:]

    _in_strips(rows, h, mask.size)
    return out


def grow(mask: np.ndarray, r: float) -> np.ndarray:
    """Binary dilation by a Euclidean radius: every pixel at most r from the mask."""
    m = np.asarray(mask, dtype=bool)
    if m.all():
        return np.ones(m.shape, dtype=bool)
    return _within(m, r)


def shrink(mask: np.ndarray, r: float) -> np.ndarray:
    """Binary erosion by a Euclidean radius; the image border does not erode."""
    m = np.asarray(mask, dtype=bool)
    if m.all():
        return np.ones(m.shape, dtype=bool)
    return ~_within(~m, r)


_CONNECT8 = np.ones((3, 3), dtype=bool)


def _ndimage():
    """scipy.ndimage, imported on first use: the import takes a noticeable moment, which
    should not delay the app's start (warm_up imports it on its worker thread)."""
    from scipy import ndimage

    return ndimage


def _components(binary: np.ndarray, connectivity: int = 8) -> tuple[np.ndarray, int]:
    """Label the connected True areas 1..n in raster order (0 = False); returns (labels, n)."""
    labels, n = _ndimage().label(binary, structure=_CONNECT8 if connectivity == 8 else None)
    return labels, int(n)


def gaussian_blur(a: np.ndarray, sigma: float) -> np.ndarray:
    """Gaussian blur of a 2-D float32 array with mirrored (reflect-101) borders.

    The kernel reaches as far as a sigma-only Gaussian blur's automatic kernel size for
    float images (about 4 sigma).
    """
    radius = max(1, (int(round(sigma * 8 + 1)) | 1) // 2)
    kw = {"sigma": float(sigma), "mode": "mirror", "radius": radius}
    fn = _ndimage().gaussian_filter1d
    return filter_axis(fn, filter_axis(fn, a, 1, **kw), 0, **kw)


def box_blur(a: np.ndarray, k: int) -> np.ndarray:
    """Normalised k x k box filter of a 2-D float32 array with mirrored (reflect-101) borders.

    The window starts k // 2 pixels before the pixel, also for an even k.
    """
    if k <= 1:
        return np.array(a, dtype=np.float32)
    kw = {"size": int(k), "mode": "mirror"}
    fn = _ndimage().uniform_filter1d
    return filter_axis(fn, filter_axis(fn, a, 1, **kw), 0, **kw)


def ellipse_half_widths(r: int) -> list[int]:
    """Half widths, for row offsets 0..r, of the usual elliptical (2r+1)x(2r+1) structuring element."""
    if r <= 0:
        return [0]
    inv = 1.0 / (r * r)
    return [int(np.rint(r * math.sqrt((r * r - dy * dy) * inv))) for dy in range(r + 1)]


def _combine_shifted(out: np.ndarray, src: np.ndarray, shift: int, combine) -> None:
    """out[y] = combine(out[y], src[y + shift]) for every row y where y + shift is inside."""
    h = out.shape[0]
    if shift >= 0:
        d0, d1, s0 = 0, h - shift, shift
    else:
        d0, d1, s0 = -shift, h, 0
    n = d1 - d0
    if n <= 0:
        return

    def rows(i0: int, i1: int) -> None:
        dst = out[d0 + i0 : d0 + i1]
        combine(dst, src[s0 + i0 : s0 + i1], out=dst)

    _in_strips(rows, n, n * out.shape[1])


def ellipse_morph(a: np.ndarray, r: int, dilate: bool) -> np.ndarray:
    """Grey dilation (maximum) or erosion (minimum) over an elliptical kernel of radius r.

    The kernel is split into its rows: each row width is a 1-D maximum/minimum filter along
    the image rows, and the kernel rows are combined by shifting those results up and down.
    Pixels outside the image do not count, so the border neither grows nor erodes.
    """
    a = np.ascontiguousarray(a, dtype=np.float32)
    r = int(r)
    if r <= 0:
        return a.copy()
    widths = ellipse_half_widths(r)
    ndi = _ndimage()
    filt = ndi.maximum_filter1d if dilate else ndi.minimum_filter1d
    combine = np.maximum if dilate else np.minimum
    out = None
    for d in sorted(set(widths), reverse=True):  # widths[0] == r, the widest row, comes first
        rowwise = filter_axis(filt, a, 1, size=2 * d + 1, mode="nearest") if d > 0 else a
        for dy in range(r + 1):
            if widths[dy] != d:
                continue
            if out is None:
                out = rowwise.copy()  # dy == 0
                continue
            _combine_shifted(out, rowwise, dy, combine)
            _combine_shifted(out, rowwise, -dy, combine)
    return out


def make_trimap(mask: np.ndarray, opts: Options, band_out: float | None = None) -> np.ndarray:
    """0 = background, 128 = unknown edge band, 255 = person.

    The band reaches opts.refine_band % of the short side into the person and band_out %
    (default the same) into the background. Keep the inward band narrow: a solid part
    thinner than twice the band (a braid, a hat brim) would lose its certain core, and
    the refiner may then call it background (measured: a 3 % band deleted a braid).
    """
    h, w = mask.shape
    band = max(1.0, min(h, w) * opts.refine_band / 100.0)
    band_bg = band if band_out is None else max(1.0, min(h, w) * band_out / 100.0)
    fg = shrink(mask >= opts.fg_threshold / 255.0, band)
    bg = shrink(mask <= opts.bg_threshold / 255.0, band_bg)
    trimap = np.full((h, w), 128, dtype=np.uint8)
    trimap[fg] = 255
    trimap[bg] = 0
    return trimap


def merge_loose_strands(base: np.ndarray, extra: np.ndarray, low: float = 0.1) -> np.ndarray:
    """Add what the wide pass found, but only where it connects to the person (base > 0.5)."""
    gain = extra > base
    if not gain.any():
        return base
    labels, _ = _components(np.maximum(base, extra) > low, 8)
    body = np.unique(labels[base > 0.5])
    linked = np.isin(labels, body[body != 0])
    return np.where(linked & gain, extra, base).astype(np.float32)


def keep_main_subject(mask: np.ndarray, reach: float = 0.03) -> tuple[np.ndarray, np.ndarray | None]:
    """Keep the largest shape and every shape that touches it (a hand, loose hair).

    Returns the cleaned mask and a mask of the removed shapes (plus a small halo), so
    later steps can keep them removed. Faint shapes far from the subject fade out.
    """
    solid = mask > 0.5
    labels, count = _components(solid, 8)
    if count < 1:
        return mask, None
    h, w = mask.shape
    r = max(2.0, min(h, w) * reach)
    areas = np.bincount(labels.ravel(), minlength=count + 1)
    main = 1 + int(np.argmax(areas[1:]))
    near_main = grow(labels == main, r)
    touching = np.unique(labels[near_main & solid])
    keep = np.isin(labels, touching[touching != 0])
    # Faint detail (thin strands below 0.5) belongs to the subject when a chain of faint
    # pixels connects it to the kept shapes; unconnected faint shapes (ghosts) fade out.
    # Removed people are handled by the halo below, not by this fade.
    low_labels, _ = _components(mask > 0.08, 8)
    linked = np.unique(low_labels[keep])
    linked_zone = np.isin(low_labels, linked[linked != 0]).astype(np.float32)
    near = gaussian_blur(grow(keep, r).astype(np.float32), r / 4.0)
    factor = np.maximum(linked_zone, np.clip(near * 1.5, 0.0, 1.0))
    out = mask * factor
    dropped_solid = solid & ~keep
    if not dropped_solid.any():
        return out, None
    edge = max(2.0, r / 4.0)
    halo = grow(dropped_solid, edge) & ~grow(keep, edge)
    out[halo] = 0.0
    return out, halo.astype(np.uint8)


def fill_enclosed_holes(mask: np.ndarray) -> np.ndarray:
    """Close gaps fully enclosed by the person, including their soft rim."""
    solid = mask > 0.5
    labels, _ = _components(~solid, 4)
    border = np.unique(np.concatenate([labels[0], labels[-1], labels[:, 0], labels[:, -1]]))
    holes = (labels != 0) & ~np.isin(labels, border)
    if not holes.any():
        return mask
    # Grow over the transition ring so the trimap does not reopen the hole as 'unknown'.
    holes = grow(holes, max(2.0, min(mask.shape) * 0.004))
    out = mask.copy()
    out[holes] = 1.0
    return out


def finish_edge(alpha: np.ndarray, opts: Options) -> np.ndarray:
    """Apply the user's edge shift and softening to a float32 alpha in 0-1."""
    if opts.edge_shift:
        alpha = ellipse_morph(alpha, abs(int(opts.edge_shift)), dilate=opts.edge_shift > 0)
    if opts.edge_soften > 0:
        alpha = gaussian_blur(alpha, float(opts.edge_soften))
    return np.clip(alpha, 0.0, 1.0, out=alpha)


def estimate_foreground(image: np.ndarray, alpha: np.ndarray) -> np.ndarray:
    """Blur-fusion foreground colour estimation (Forte & Pitie, ICIP 2021), two passes.

    Only the soft edge needs it, so the work is limited to the bounding box of
    pixels with 0 < alpha < 1 (plus the blur radius). `image` is updated in place.
    """
    soft = (alpha > 0.0) & (alpha < 1.0)
    rows = np.flatnonzero(soft.any(axis=1))
    cols = np.flatnonzero(soft.any(axis=0))
    if rows.size == 0:
        return image
    h, w = alpha.shape
    r1 = max(9, round(90 * max(h, w) / 2048))
    r2 = max(3, round(6 * max(h, w) / 2048))
    y0, y1 = max(0, rows[0] - r1), min(h, rows[-1] + 1 + r1)
    x0, x1 = max(0, cols[0] - r1), min(w, cols[-1] + 1 + r1)
    # Colour planes (3 x h x w): every filter then runs over contiguous memory.
    img = np.ascontiguousarray(image[y0:y1, x0:x1].transpose(2, 0, 1))
    a = np.ascontiguousarray(alpha[y0:y1, x0:x1])
    f, b = _blur_fusion(img, img, img, a, r1)
    f, _ = _blur_fusion(img, f, b, a, r2)
    image[y0:y1, x0:x1] = f.transpose(1, 2, 0)
    return image


def _box_planes(planes: np.ndarray, k: int) -> np.ndarray:
    out = np.empty_like(planes)
    for i in range(planes.shape[0]):
        out[i] = box_blur(planes[i], k)
    return out


def _blur_fusion(image, fg, bg, alpha, r):
    """One blur-fusion step on 3xHxW colour planes, with reused buffers."""
    a = alpha[None]
    one_minus_a = 1.0 - a
    blurred_a = box_blur(alpha, r)[None]
    buf = np.multiply(fg, a)
    blurred_f = _box_planes(buf, r)
    blurred_f /= blurred_a + 1e-5
    np.multiply(bg, one_minus_a, out=buf)
    blurred_b = _box_planes(buf, r)
    blurred_b /= (1.0 - blurred_a) + 1e-5
    # f = Fb + a * (I - a*Fb - (1-a)*Bb)
    np.multiply(blurred_f, a, out=buf)
    np.subtract(image, buf, out=buf)
    buf -= blurred_b * one_minus_a
    buf *= a
    buf += blurred_f
    np.clip(buf, 0.0, 1.0, out=buf)
    return buf, blurred_b


# ------------------------------------------------------ GPU compatibility
def _is_deterministic_gpu_failure(exc: BaseException) -> bool:
    """Failures that repeat on every attempt: worth remembering across app starts."""
    if isinstance(exc, _ImplausibleOutput):
        return True
    text = str(exc)
    # E_INVALIDARG (operator rejected), DXGI device hung / driver internal error.
    # E_INVALIDARG, DXGI_ERROR_UNSUPPORTED, device hung, driver internal error.
    return any(code in text for code in ("80070057", "887A0004", "887A0006", "887A0020"))


def _gpu_key(model_key: str, gpu: GpuInfo) -> str:
    return f"{model_key}|{gpu.name}|{gpu.vram_mb}|{ort.__version__}"


def _blocklist_path() -> str:
    return str(paths.data_dir() / "gpu-compat.json")


def _load_gpu_blocklist() -> set[str]:
    try:
        with open(_blocklist_path(), encoding="utf-8") as fh:
            data = json.load(fh)
        return {str(x) for x in data.get("cpu_only", [])}
    except (OSError, ValueError, AttributeError):
        return set()


def _save_gpu_blocklist(keys: set[str]) -> None:
    try:
        os.makedirs(os.path.dirname(_blocklist_path()), exist_ok=True)
        with open(_blocklist_path(), "w", encoding="utf-8", newline="") as fh:
            json.dump({"cpu_only": sorted(keys)}, fh, indent=1)
    except OSError:
        pass

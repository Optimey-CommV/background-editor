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

"""Model registry, lookup and verified download.

MODELS holds the built-in models plus the models the model update check adopted. Adopted
models are listed in paths.data_dir()/model-catalog.json; an entry is used only when it is
well formed, its checksum was verified when it was adopted, and its file is present.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import http.client
import json
import os
import re
import ssl
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from . import __version__, paths

USER_AGENT = f"BackgroundEditor/{__version__} (model download)"

RELEASE = "https://github.com/danielgatis/rembg/releases/download/v0.0.0/"

# The segmentation model used when no newer model has been adopted.
DEFAULT_SEGMENTER = "birefnet-matting"
FAST_SEGMENTER = "birefnet-lite"
REFINER = "vitmatte-small"

OUTPUT_KINDS = ("logits", "probs")
FAMILIES = ("matting", "portrait", "general", "lite", "refiner")
# Families an adopted segmentation model may have (a refiner is never adopted).
ADOPTABLE_FAMILIES = ("matting", "portrait", "general", "lite")
MAX_MODEL_BYTES = 2_500_000_000  # larger ONNX files need external data, which is not supported

CATALOG_NAME = "model-catalog.json"
CATALOG_VERSION = 1

# Download tuning. Reads return as soon as any data has arrived and the socket timeout is
# short, so Cancel takes effect within seconds even on a stalled connection. A stalled,
# reset or cut-off connection is resumed with a range request, so the short timeout costs
# no downloaded data.
_READ_SIZE = 64 * 1024
_SOCKET_TIMEOUT = 5.0  # seconds, per connect and per read
_RETRIES = 6  # consecutive attempts without new data before the download gives up
_RETRY_PAUSE = 2.0  # seconds between attempts
_PROGRESS_INTERVAL = 0.1  # seconds between progress reports


@dataclass(frozen=True)
class ModelSpec:
    key: str
    title: str
    filename: str
    url: str
    sha256: str
    size: int
    licence: str
    input_size: int | None = 1024  # None = dynamic: the engine then feeds 1024 x 1024
    output: str = "logits"  # "logits" (the engine applies a sigmoid) or "probs"
    family: str = ""  # "matting" | "portrait" | "general" | "lite" | "refiner"
    licence_note: str = ""  # where the licence was found, for the About box and notices
    discovered: bool = False  # adopted by the model update check, not built in


_BUILTIN_SPECS = (
    ModelSpec(
        key="birefnet-matting",
        title="BiRefNet matting",
        filename="BiRefNet-matting-epoch_100.onnx",
        url="https://github.com/ZhengPeng7/BiRefNet/releases/download/v1/BiRefNet-matting-epoch_100.onnx",
        sha256="6065d27c615ea27308f5b88598dd8db116eb07436c7a323ca40d13b2866c309e",
        size=972_667_742,
        licence="MIT",
        family="matting",
        licence_note="BiRefNet v1 release (github.com/ZhengPeng7/BiRefNet), MIT licence",
    ),
    ModelSpec(
        key="birefnet-portrait",
        title="BiRefNet portrait",
        filename="BiRefNet-portrait-epoch_150.onnx",
        url=RELEASE + "BiRefNet-portrait-epoch_150.onnx",
        sha256="1ba1c8ff5a7bbfadc8d8d13fb11d7be793f91f23d9d466549e37a854f6668f99",
        size=972_666_916,
        licence="MIT",
        family="portrait",
        licence_note="BiRefNet by ZhengPeng7, MIT licence; ONNX file from the rembg model release",
    ),
    ModelSpec(
        key="birefnet-general",
        title="BiRefNet general",
        filename="BiRefNet-general-epoch_244.onnx",
        url=RELEASE + "BiRefNet-general-epoch_244.onnx",
        sha256="58f621f00f5d756097615970a88a791584600dcf7c45b18a0a6267535a1ebd3c",
        size=972_666_916,
        licence="MIT",
        family="general",
        licence_note="BiRefNet by ZhengPeng7, MIT licence; ONNX file from the rembg model release",
    ),
    ModelSpec(
        key="birefnet-lite",
        title="BiRefNet lite",
        filename="BiRefNet-general-bb_swin_v1_tiny-epoch_232.onnx",
        url=RELEASE + "BiRefNet-general-bb_swin_v1_tiny-epoch_232.onnx",
        sha256="5600024376f572a557870a5eb0afb1e5961636bef4e1e22132025467d0f03333",
        size=224_005_088,
        licence="MIT",
        family="lite",
        licence_note="BiRefNet by ZhengPeng7, MIT licence; ONNX file from the rembg model release",
    ),
    ModelSpec(
        key="vitmatte-small",
        title="ViTMatte small (hair refinement)",
        filename="vitmatte-small-distinctions-646.onnx",
        url=RELEASE + "vitmatte-small-distinctions-646.onnx",
        sha256="d232841ac9d9657df3e62f6c92ed425ee25df18d337245cd8b903fc1ba183631",
        size=114_423_212,
        licence="MIT",
        output="probs",  # the graph ends in a sigmoid
        family="refiner",
        licence_note="ViTMatte by hustvl, MIT licence; ONNX export from the rembg model release",
    ),
)
BUILTIN_KEYS = tuple(s.key for s in _BUILTIN_SPECS)

# Built-in models first, then adopted ones. Updated in place (never rebound), so a module
# that imported the name sees adopted models too.
MODELS: dict[str, ModelSpec] = {spec.key: spec for spec in _BUILTIN_SPECS}

_catalog_lock = threading.RLock()
_preferred = ""  # the adopted model to use for 'Best' and 'Balanced'; "" = the built-in default


class DownloadCancelled(Exception):
    pass


class ChecksumMismatch(Exception):
    pass


def user_model_dir() -> Path:
    """Where downloads go: next to the exe when portable, in the user profile otherwise."""
    return paths.model_dir()


def find_model(spec: ModelSpec) -> Path | None:
    """Return a present model file whose size matches; the hash is checked at download time."""
    for folder in paths.model_search_dirs():
        candidate = folder / spec.filename
        try:
            if candidate.is_file() and candidate.stat().st_size == spec.size:
                return candidate
        except OSError:
            continue
    return None


def verify_file(path: Path, spec: ModelSpec, cancel: threading.Event | None = None) -> bool:
    return _hash_file(path, cancel)[0].hexdigest() == spec.sha256


# ------------------------------------------------------------ choosing models
def preferred_segmenter() -> str:
    """The segmentation model for 'Best' and 'Balanced': the adopted newer model when there
    is one and its file is present, otherwise the built-in BiRefNet matting model."""
    with _catalog_lock:
        key = _preferred
    spec = MODELS.get(key)
    if spec is not None and spec.discovered and find_model(spec) is not None:
        return key
    return DEFAULT_SEGMENTER


def offline_models(include_all: bool = False) -> list[ModelSpec]:
    """The models to download for offline use: what the presets use (the preferred
    segmentation model, the lite model and the hair refiner), or every registered model."""
    if include_all:
        return list(MODELS.values())  # a snapshot: the update thread may add a model
    keys = dict.fromkeys((preferred_segmenter(), FAST_SEGMENTER, REFINER))
    return [MODELS[k] for k in keys if k in MODELS]


def model_keys() -> list[str]:
    """The segmentation models the user may pick: every registered model except refiners."""
    return [key for key, spec in list(MODELS.items()) if spec.family != "refiner"]


# ------------------------------------------------------------ model catalogue
_KEY_RE = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}")
_FILENAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,150}\.onnx")
_SHA256_RE = re.compile(r"[0-9a-f]{64}")


def catalog_path() -> Path:
    return paths.data_dir() / CATALOG_NAME


def _read_catalog() -> dict:
    """The catalogue file as a dict; {} when it is missing or not a JSON object."""
    try:
        with open(catalog_path(), encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _text(entry: dict, name: str, limit: int, required: bool = True) -> str | None:
    value = entry.get(name, "")
    if not isinstance(value, str) or len(value) > limit or any(ord(c) < 32 and c not in "\t\n" for c in value):
        return None
    if required and not value.strip():
        return None
    return value


def spec_from_entry(entry) -> ModelSpec | None:
    """A ModelSpec for a catalogue entry, or None when anything about it is malformed.

    Only entries whose checksum was verified at adoption are accepted, and an entry may
    neither replace a built-in model nor point at a built-in model's file.
    """
    if not isinstance(entry, dict) or entry.get("sha256_verified") is not True:
        return None
    key = _text(entry, "key", 64)
    title = _text(entry, "title", 100)
    filename = _text(entry, "filename", 160)
    url = _text(entry, "url", 2048)
    sha256 = _text(entry, "sha256", 64)
    licence = _text(entry, "licence", 100)
    note = _text(entry, "licence_note", 4000, required=False)
    if None in (key, title, filename, url, sha256, licence, note):
        return None
    if not _KEY_RE.fullmatch(key) or key in BUILTIN_KEYS:
        return None
    if not _FILENAME_RE.fullmatch(filename) or ".." in filename:
        return None
    if filename.lower() in {s.filename.lower() for s in _BUILTIN_SPECS}:
        return None
    if not url.startswith("https://") or not _SHA256_RE.fullmatch(sha256):
        return None
    size = entry.get("size")
    if not isinstance(size, int) or isinstance(size, bool) or not 0 < size <= MAX_MODEL_BYTES:
        return None
    input_size = entry.get("input_size")
    if input_size is not None and (
        not isinstance(input_size, int) or isinstance(input_size, bool) or not 64 <= input_size <= 4096
    ):
        return None
    output = entry.get("output")
    family = entry.get("family")
    if output not in OUTPUT_KINDS or family not in ADOPTABLE_FAMILIES:
        return None
    return ModelSpec(
        key=key,
        title=title,
        filename=filename,
        url=url,
        sha256=sha256,
        size=size,
        licence=licence,
        input_size=input_size,
        output=output,
        family=family,
        licence_note=note,
        discovered=True,
    )


def entry_from_spec(spec: ModelSpec, evidence: dict | None = None) -> dict:
    """The catalogue entry for a model whose downloaded file matched spec.sha256."""
    return {
        "key": spec.key,
        "title": spec.title,
        "filename": spec.filename,
        "url": spec.url,
        "sha256": spec.sha256,
        "size": spec.size,
        "licence": spec.licence,
        "licence_note": spec.licence_note,
        "input_size": spec.input_size,
        "output": spec.output,
        "family": spec.family,
        "sha256_verified": True,
        "adopted_at": _now_text(),
        "evidence": evidence or {},
    }


def _now_text() -> str:
    return _dt.datetime.now(_dt.timezone.utc).replace(microsecond=0).isoformat()


def _catalog_models() -> tuple[list[ModelSpec], str]:
    """The usable adopted models (valid entry, file present) and the preferred key."""
    data = _read_catalog()
    specs: list[ModelSpec] = []
    seen_keys: set[str] = set()
    seen_files: set[str] = set()
    entries = data.get("models")
    for entry in entries if isinstance(entries, list) else []:
        spec = spec_from_entry(entry)
        if spec is None or spec.key in seen_keys or spec.filename.lower() in seen_files:
            continue
        if find_model(spec) is None:
            continue
        seen_keys.add(spec.key)
        seen_files.add(spec.filename.lower())
        specs.append(spec)
    preferred = data.get("preferred")
    return specs, preferred if isinstance(preferred, str) else ""


def reload_catalog() -> None:
    """Bring MODELS and the preferred model in line with the catalogue file.

    MODELS is changed in place, one key at a time; call this from the thread that shows
    model lists (or refresh those lists afterwards).
    """
    global _preferred
    specs, preferred = _catalog_models()
    with _catalog_lock:
        wanted = {s.key: s for s in specs}
        for key in [k for k, s in MODELS.items() if s.discovered and k not in wanted]:
            del MODELS[key]
        MODELS.update(wanted)
        _preferred = preferred if preferred in wanted else ""


def _write_catalog(data: dict) -> None:
    """Write the catalogue atomically (a temporary file next to it, then a rename)."""
    path = catalog_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    try:
        with open(tmp, "w", encoding="utf-8", newline="") as fh:
            json.dump(data, fh, indent=1, ensure_ascii=False)
            fh.write("\n")
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def adopt_model(spec: ModelSpec, evidence: dict | None = None, make_preferred: bool = True) -> None:
    """Record a downloaded, checksum-verified and validated model in the catalogue.

    The model becomes the preferred segmentation model when make_preferred is set. The
    previous preference is remembered, and no model file is ever deleted.
    """
    global _preferred
    entry = entry_from_spec(spec, evidence)
    checked = spec_from_entry(entry)
    if checked is None:
        raise ValueError(f"The model entry for '{spec.key}' is not valid.")
    if find_model(checked) is None:
        raise FileNotFoundError(f"The file {spec.filename} is not in the models folder.")
    with _catalog_lock:
        data = _read_catalog()
        entries = data.get("models") if isinstance(data.get("models"), list) else []
        data["version"] = CATALOG_VERSION
        data["models"] = [e for e in entries if not (isinstance(e, dict) and e.get("key") == spec.key)] + [entry]
        if make_preferred:
            data["previous"] = preferred_segmenter()
            data["preferred"] = spec.key
        _write_catalog(data)
        MODELS[checked.key] = checked
        if make_preferred:
            _preferred = checked.key


def set_preferred(key: str) -> None:
    """Use another model for 'Best' and 'Balanced': an adopted model's key, or a built-in
    key (which means: no adopted model). The catalogue remembers the previous choice."""
    global _preferred
    spec = MODELS.get(key)
    if spec is None or spec.family == "refiner":
        raise ValueError(f"'{key}' is not a segmentation model.")
    with _catalog_lock:
        data = _read_catalog()
        data["version"] = CATALOG_VERSION
        data["previous"] = preferred_segmenter()
        data["preferred"] = key if spec.discovered else ""
        _write_catalog(data)
        _preferred = key if spec.discovered else ""


def previous_segmenter() -> str:
    """The preferred model before the last switch (for a 'use the previous model' action)."""
    previous = _read_catalog().get("previous")
    if isinstance(previous, str) and previous in MODELS and MODELS[previous].family != "refiner":
        return previous
    return DEFAULT_SEGMENTER


def catalog_entry(key: str) -> dict | None:
    """The raw catalogue entry of an adopted model (licence evidence for the About box)."""
    entries = _read_catalog().get("models")
    for entry in entries if isinstance(entries, list) else []:
        if isinstance(entry, dict) and entry.get("key") == key and spec_from_entry(entry) is not None:
            return dict(entry)
    return None


ProgressFn = Callable[[int, int], None]


class _Restart(Exception):
    """The partial file cannot be resumed: delete it and download from the beginning."""


class _ClosedEarly(ConnectionError):
    """The server ended the response before the whole file had arrived."""


@dataclass
class _Partial:
    """What the .part file holds: the SHA-256 of its bytes so far, and its size."""

    sha: hashlib._Hash = field(default_factory=hashlib.sha256)
    size: int = 0
    received: int = 0  # bytes received over all attempts, to tell a stall from slow progress


class _Progress:
    """Reports progress at most every _PROGRESS_INTERVAL seconds, and always at the end."""

    def __init__(self, fn: ProgressFn | None, total: int) -> None:
        self._fn = fn
        self._total = total
        self._last = 0.0

    def __call__(self, done: int, force: bool = False) -> None:
        if self._fn is None:
            return
        now = time.monotonic()
        if force or done >= self._total or now - self._last >= _PROGRESS_INTERVAL:
            self._last = now
            self._fn(done, self._total)


def _check_cancel(cancel: threading.Event | None) -> None:
    if cancel is not None and cancel.is_set():
        raise DownloadCancelled()


def _hash_file(
    path: Path, cancel: threading.Event | None = None, report: _Progress | None = None
) -> tuple[hashlib._Hash, int]:
    h = hashlib.sha256()
    size = 0
    with open(path, "rb") as fh:
        while chunk := fh.read(1 << 22):
            _check_cancel(cancel)
            h.update(chunk)
            size += len(chunk)
            if report is not None:
                report(size)
    return h, size


def download_model(
    spec: ModelSpec,
    progress: ProgressFn | None = None,
    cancel: threading.Event | None = None,
    folder: Path | None = None,
    user_agent: str | None = None,
) -> Path:
    """Download into the per-user model folder (or `folder`), resuming a partial file when
    possible. The app update download (bgeditor.app_updates) uses this too, with its own
    folder and User-Agent.

    A stalled, reset or cut-off connection keeps the .part file and resumes it: a few times
    automatically, and otherwise on the next call. Only a complete file whose checksum does
    not match is deleted.
    """
    folder = user_model_dir() if folder is None else Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / spec.filename
    part = folder / (spec.filename + ".part")
    report = _Progress(progress, spec.size)

    state = _load_partial(part, spec, cancel, report)
    if state.size == spec.size:
        # Already complete (for example stopped just before the rename): no request is
        # needed, and a range request for it would only get HTTP 416.
        if state.sha.hexdigest() == spec.sha256:
            os.replace(part, target)
            return target
        part.unlink(missing_ok=True)
        state = _Partial()
        report(0, force=True)

    failures = 0
    while state.size < spec.size:
        _check_cancel(cancel)
        before = state.received
        try:
            _fetch(spec, part, state, cancel, report, user_agent or USER_AGENT)
        except _Restart:
            part.unlink(missing_ok=True)
            state = _Partial()
            report(0, force=True)
            continue
        except Exception as exc:
            if not _is_transient(exc):
                raise
            failures = 0 if state.received > before else failures + 1
            if failures >= _RETRIES:
                raise ConnectionError(
                    f"{spec.filename}: the download stopped at {state.size / 1e6:.0f} of "
                    f"{spec.size / 1e6:.0f} MB ({_reason(exc)}). Try again to continue where it stopped."
                ) from exc
            if cancel is not None:
                if cancel.wait(_RETRY_PAUSE):
                    raise DownloadCancelled() from None
            else:
                time.sleep(_RETRY_PAUSE)
            try:
                on_disk = part.stat().st_size
            except FileNotFoundError:
                on_disk = 0
            if on_disk != state.size:  # not expected; rebuild the state from the file
                received = state.received
                state = _load_partial(part, spec, cancel, report)
                state.received = received

    if state.sha.hexdigest() != spec.sha256:
        part.unlink(missing_ok=True)
        raise ChecksumMismatch(f"{spec.filename}: download is corrupt (checksum mismatch)")
    os.replace(part, target)
    return target


def _load_partial(part: Path, spec: ModelSpec, cancel: threading.Event | None, report: _Progress) -> _Partial:
    try:
        size = part.stat().st_size
    except FileNotFoundError:
        return _Partial()
    if size == 0 or size > spec.size:
        part.unlink(missing_ok=True)
        return _Partial()
    sha, size = _hash_file(part, cancel, report)
    return _Partial(sha, size)


def _fetch(
    spec: ModelSpec,
    part: Path,
    state: _Partial,
    cancel: threading.Event | None,
    report: _Progress,
    user_agent: str = USER_AGENT,
) -> None:
    """One request: append to the .part file until it is complete or the connection fails."""
    headers = {"User-Agent": user_agent}
    if state.size:
        headers["Range"] = f"bytes={state.size}-"
    request = urllib.request.Request(spec.url, headers=headers)
    try:
        response = urllib.request.urlopen(request, timeout=_SOCKET_TIMEOUT)
    except urllib.error.HTTPError as exc:
        if exc.code == 416 and state.size:
            # "Range not satisfiable": the server's file is not longer than the partial one.
            exc.close()
            raise _Restart() from exc
        raise
    too_long = False
    with response:
        if state.size and response.status == 206:
            start, total = _content_range(response.headers.get("Content-Range"))
            if start != state.size:
                raise _Restart()
            if total is not None and total != spec.size:
                raise _wrong_size(spec, total)
            mode = "ab"
        else:
            # A first request, or the server ignored the range: start from the beginning.
            length = (response.headers.get("Content-Length") or "").strip()
            if length.isdigit() and int(length) != spec.size:
                raise _wrong_size(spec, int(length))
            state.sha = hashlib.sha256()
            state.size = 0
            report(0, force=True)
            mode = "wb"
        with open(part, mode) as out:
            while state.size < spec.size:
                _check_cancel(cancel)
                chunk = response.read1(_READ_SIZE)
                if not chunk:
                    # http.client returns b"" instead of raising when the server closes the
                    # connection early; what arrived so far stays and is resumed.
                    raise _ClosedEarly("the server closed the connection")
                if state.size + len(chunk) > spec.size:
                    too_long = True
                    break
                out.write(chunk)
                state.sha.update(chunk)
                state.size += len(chunk)
                state.received += len(chunk)
                report(state.size)
    if too_long:
        part.unlink(missing_ok=True)
        state.sha = hashlib.sha256()
        state.size = 0
        raise ChecksumMismatch(f"{spec.filename}: the server sent more data than expected")


def _content_range(value: str | None) -> tuple[int | None, int | None]:
    """Parse 'bytes <start>-<end>/<total>'; the total may be '*' (unknown)."""
    m = re.fullmatch(r"\s*bytes\s+(\d+)-\d+/(\d+|\*)\s*", value or "")
    if not m:
        return None, None
    return int(m.group(1)), (None if m.group(2) == "*" else int(m.group(2)))


def _wrong_size(spec: ModelSpec, size: int) -> ChecksumMismatch:
    return ChecksumMismatch(
        f"{spec.filename}: the server offers a file of {size:,} bytes instead of {spec.size:,}; "
        "nothing was downloaded"
    )


def _is_transient(exc: BaseException) -> bool:
    """True for errors that a new (range) request can get past: stalls, resets, cut-offs, 5xx."""
    if isinstance(exc, urllib.error.HTTPError):
        return exc.code in (408, 429) or exc.code >= 500
    if isinstance(exc, urllib.error.URLError):
        if not isinstance(exc.reason, BaseException):
            return False
        exc = exc.reason
    return isinstance(
        exc,
        (TimeoutError, ConnectionError, http.client.IncompleteRead, ssl.SSLEOFError, ssl.SSLZeroReturnError),
    )


def _reason(exc: BaseException) -> str:
    if isinstance(exc, urllib.error.HTTPError):
        return f"HTTP {exc.code} {exc.reason}"
    if isinstance(exc, urllib.error.URLError) and isinstance(exc.reason, BaseException):
        exc = exc.reason
    if isinstance(exc, TimeoutError):
        return "no data arrived for a while"
    if isinstance(exc, http.client.IncompleteRead):
        return "the connection was cut off"
    return str(exc) or exc.__class__.__name__


# Adopted models are part of MODELS from the start.
reload_catalog()

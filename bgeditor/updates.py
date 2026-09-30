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

"""Model update check: notice a newer BiRefNet (the announced v2) and switch to it only when
that is safe.

The check runs at most once a week (plus a random delay of up to a day), on a worker
thread; nothing here imports Qt. It reads a handful of public API listings from GitHub and
Hugging Face, restricted to the BiRefNet author (ZhengPeng7) and to ONNX conversions of the
author's models (onnx-community). A file is only a candidate when it is marked as v2.

An automatic switch needs every gate to pass for one specific ONNX file:
  a. it is an ONNX file (PyTorch-only releases are reported, never used);
  b. its machine-readable licence is MIT, Apache-2.0 or BSD, the repository is not gated,
     and neither the model card, the README section, the release notes, a LICENSE or
     manifest file nor the file name mentions non-commercial, research-only or academic
     use, CC-BY-NC or DINOv3 (a DINOv3-based model may carry Meta's DINOv3 licence);
  c. the server publishes a SHA-256 for the file and the download matches it;
  d. ONNX Runtime can create a CPU session for it (DeformConv, for example, cannot);
  e. it takes one [1, 3, H, W] input and returns one [1, 1, H, W] output;
  f. on the bundled test portrait its mask matches the reference mask (IoU >= 0.90 after
     thresholding, mean absolute difference <= 0.08); whether it returns logits or
     probabilities is decided here from its output and graph, and stored;
  g. it runs at most 4x as long as the current model on that photo.
Anything missing or conflicting leads to a notification with the reason instead. In 'ask'
mode nothing is downloaded until the user agrees (adopt_candidate). No model file is ever
deleted; the previous model stays available.

On HTTP 403 or 429 the check stops and waits for the next weekly slot (or the server's
reset time, when that is later); it never retries in a loop.
"""

from __future__ import annotations

import datetime as dt
import email.utils
import json
import os
import random
import re
import shutil
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from dataclasses import asdict, dataclass, field, fields, replace
from http.client import HTTPException
from pathlib import Path
from typing import Callable

import numpy as np

from . import __version__, models, paths

# ------------------------------------------------------------------ settings
STATE_NAME = "model-updates.json"
STATE_VERSION = 1
CHECK_INTERVAL = dt.timedelta(days=7)
CHECK_JITTER = dt.timedelta(hours=24)

AUTHOR = "ZhengPeng7"
CONVERTER = "onnx-community"
MAIN_REPO = f"{AUTHOR}/BiRefNet"
GH_API = "https://api.github.com"
HF = "https://huggingface.co"
URL_GH_RELEASES = f"{GH_API}/repos/{MAIN_REPO}/releases?per_page=100"
URL_GH_REPOS = f"{GH_API}/users/{AUTHOR}/repos?sort=created&direction=desc&per_page=20"
URL_HF_AUTHOR = f"{HF}/api/models?author={AUTHOR}&full=true&cardData=true&limit=200"
URL_HF_CONVERTED = f"{HF}/api/models?author={CONVERTER}&search=BiRefNet&full=true&cardData=true&limit=200"

# The author promised per-file licences for v2 on this day (issue #306); older files are not v2.
V2_NOT_BEFORE = "2026-06-06"
PERMISSIVE_LICENCES = {"mit", "apache-2.0", "bsd", "bsd-2-clause", "bsd-3-clause", "bsd-3-clause-clear"}
TASK_ORDER = ("matting", "portrait", "general")  # for portraits: matting first
EXCLUDED_VARIANTS = {"lite", "tiny", "anime", "cod", "dis", "dis5k", "hrsod", "massive", "toonout"}
QUANTISED_TOKENS = {"quantized", "quantised", "int8", "uint8", "int4", "q4", "q4f16", "q8", "bnb4"}
WEIGHT_SUFFIXES = (".onnx", ".pth", ".pt", ".safetensors", ".ckpt", ".bin")
BANNED_OPS = {"DeformConv": "DeformConv, which ONNX Runtime 1.24 cannot run on the CPU or with DirectML"}

MIN_IOU = 0.90
MAX_MAE = 0.08
MAX_SLOWDOWN = 4.0
MAX_DOWNLOADS_PER_CHECK = 2

_API_LIMIT = 8 << 20  # bytes per API response
_TEXT_LIMIT = 1 << 20  # bytes per card, README or manifest
_STORED_TEXT = 64 << 10  # characters kept per text in the state file
_TIMEOUT = 20.0
_PROBE_BUDGET = 48 << 20  # bytes a pre-download probe may read over HTTP
USER_AGENT = f"BackgroundEditor/{__version__} (model update check)"

Progress = Callable[[str, int, int], None]

_busy = threading.Lock()


@dataclass
class UpdateReport:
    status: str  # "up-to-date" | "adopted" | "notified" | "skipped" | "failed"
    message: str
    candidate: str = ""  # set when the UI may offer adopt_candidate(candidate)
    adopted_key: str = ""
    licence: str = ""
    details: str = ""


# ------------------------------------------------------------------ helpers
def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0)


def _iso(t: dt.datetime | None) -> str:
    return t.isoformat() if t is not None else ""


def _parse_time(text) -> dt.datetime | None:
    if not isinstance(text, str) or not text:
        return None
    try:
        t = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return t if t.tzinfo else t.replace(tzinfo=dt.timezone.utc)


def _tokens(*texts: str) -> set[str]:
    """Lower-case words of names such as 'BiRefNet_v2-matting.onnx' -> {birefnet, v2, matting, onnx}."""
    out: set[str] = set()
    for text in texts:
        out.update(t for t in re.split(r"[^a-z0-9]+", (text or "").lower()) if t)
    return out


def has_v2_marker(*names: str) -> bool:
    """'v2' as a word of its own (v2, v2.0, BiRefNet_v2-matting), not inside 'BiRefNetV2'."""
    return any(re.search(r"(?<![a-z0-9])v2(?![a-z0-9])", (n or "").lower()) for n in names)


def is_v2_tag(tag: str) -> bool:
    return bool(re.fullmatch(r"v2(\.[0-9]+)*", (tag or "").strip().lower()))


def family_of(*names: str) -> str:
    """matting / portrait / general from the names; 'excluded' for lite, anime, COD, DIS and
    similar variants (also 'lite-matting'), which are not used for portraits."""
    tokens = _tokens(*names)
    if tokens & EXCLUDED_VARIANTS:
        return "excluded"
    for task in TASK_ORDER:
        if task in tokens:
            return task
    return "general"


def _short(exc: BaseException, limit: int = 300) -> str:
    text = " ".join(str(exc).split()) or exc.__class__.__name__
    return text if len(text) <= limit else text[: limit - 3] + "..."


# ------------------------------------------------------------------ licence gate
_RESTRICTIVE_TERMS = (
    (re.compile(r"non[\s_-]?commercial", re.I), "non-commercial"),
    (re.compile(r"research[\s_-]+(?:use[\s_-]+|purposes?[\s_-]+)?only|research[\s_-]only|only[\s_-]+for[\s_-]+research", re.I), "research only"),
    (re.compile(r"(?<![a-z])academic(?![a-z])", re.I), "academic"),
    (re.compile(r"cc[\s_-]?by[\s_-]?(?:\d(?:\.\d)?[\s_-]?)?(?:sa[\s_-]?)?nc", re.I), "CC-BY-NC"),
    (re.compile(r"creative\s+commons\s+attribution[\s-]+non", re.I), "CC-BY-NC"),
    (re.compile(r"dino[\s_-]?v3|dinov3", re.I), "DINOv3"),
    (re.compile(r"dino\s+materials", re.I), "DINOv3"),
    (re.compile(r"not\s+for\s+commercial|no\s+commercial\s+use|commercial\s+use\s+(?:is\s+)?(?:not|prohibited|forbidden)", re.I), "no commercial use"),
)


def restrictive_term(text: str) -> str:
    """The first licence restriction mentioned in text, or ''."""
    for pattern, label in _RESTRICTIVE_TERMS:
        if pattern.search(text or ""):
            return label
    return ""


def normalise_licence(value) -> str:
    text = (value or "").strip().lower() if isinstance(value, str) else ""
    text = text.replace("_", "-").replace(" ", "-")
    aliases = {"mit-license": "mit", "apache-2": "apache-2.0", "apache2": "apache-2.0", "apache-license-2.0": "apache-2.0"}
    return aliases.get(text, text)


def licence_verdict(
    licence: str,
    texts: dict[str, str],
    names: list[str],
    gated: bool = False,
    base_licence: str | None = None,
) -> str:
    """'' when the licence allows an automatic switch, otherwise the reason (for the notice).

    licence is the machine-readable licence of the file; texts are the card, README section,
    release notes, LICENSE and manifest text; names are the file name, repository id, tags
    and labels. base_licence is the original model's licence for a converted model.
    """
    if gated:
        return "the repository is gated: it can only be downloaded after requesting access"
    lic = normalise_licence(licence)
    if not lic:
        return "no machine-readable licence was found for this file"
    if lic in ("other", "unknown") or "dinov3" in lic:
        return f"its licence is '{licence}', not a plain permissive licence"
    if lic not in PERMISSIVE_LICENCES:
        return f"its licence '{licence}' is not MIT, Apache-2.0 or BSD"
    if base_licence is not None and normalise_licence(base_licence) != lic:
        return f"the conversion says '{licence}' but the original model says '{base_licence or 'nothing'}'"
    for where, text in texts.items():
        term = restrictive_term(text)
        if term:
            return f"the {where} mentions '{term}'"
    for name in names:
        term = restrictive_term(name)
        if term:
            return f"the name '{name}' indicates '{term}'"
        if "nc" in _tokens(name):
            return f"the name '{name}' contains 'NC' (non-commercial)"
    return ""


_LICENCE_IN_TEXT = re.compile(
    r"\b(MIT|Apache[\s-]?(?:License[\s,]*)?(?:Version\s*)?2(?:\.0)?|BSD[\s-]?[23][\s-]?Clause(?:[\s-]Clear)?|"
    r"CC[\s-]BY(?:[\s-](?:NC|SA|ND))*(?:[\s-]\d\.\d)?|DINOv3[\s-]License)\b",
    re.I,
)


def licence_in_text(text: str) -> str:
    """The licence named in one line of text, normalised ('mit', 'apache-2.0', ...), or ''."""
    m = _LICENCE_IN_TEXT.search(text or "")
    if not m:
        return ""
    found = m.group(1).lower()
    if found == "mit":
        return "mit"
    if found.startswith("apache"):
        return "apache-2.0"
    if found.startswith("bsd"):
        digits = re.search(r"[23]", found).group(0)
        return f"bsd-{digits}-clause" + ("-clear" if "clear" in found else "")
    return normalise_licence(found)


def readme_sections(markdown: str, needles: list[str]) -> str:
    """The Markdown sections (heading to next heading) that mention any needle."""
    needles = [n.lower() for n in needles if n]
    sections: list[list[str]] = [[]]
    for line in (markdown or "").splitlines():
        if re.match(r"\s{0,3}#{1,6}\s", line):
            sections.append([])
        sections[-1].append(line)
    picked = ["\n".join(s) for s in sections if any(n in "\n".join(s).lower() for n in needles)]
    return "\n\n".join(picked)


# ------------------------------------------------------------------ candidates
@dataclass
class Candidate:
    """One ONNX file that may be a newer model, with everything the gates need."""

    id: str
    source: str  # "github" | "huggingface"
    repo: str  # owner/name
    filename: str  # the file's name on the server (asset name or repository path)
    url: str  # download URL pinned to a tag or commit
    size: int
    sha256: str  # published by the server, or ""
    licence: str  # machine-readable licence of this file, or ""
    licence_source: str  # where that licence was read
    family: str  # matting / portrait / general / excluded
    published: str  # ISO time
    v2: bool
    gated: bool = False
    prerelease: bool = False
    external_data: bool = False
    original: bool = True  # the author's own upload (False: a conversion)
    precision: str = "fp32"
    base_licence: str | None = None  # the original's licence, for a conversion
    texts: dict[str, str] = field(default_factory=dict)
    names: list[str] = field(default_factory=list)
    evidence: list[str] = field(default_factory=list)
    revision: str = ""  # tag or commit the URL is pinned to

    def to_dict(self) -> dict:
        data = asdict(self)
        data["texts"] = {k: v[:_STORED_TEXT] for k, v in self.texts.items()}
        return data

    @classmethod
    def from_dict(cls, data: dict) -> "Candidate | None":
        if not isinstance(data, dict):
            return None
        known = {f.name for f in fields(cls)}
        try:
            c = cls(**{k: v for k, v in data.items() if k in known})
        except TypeError:
            return None
        text_fields = (c.id, c.source, c.repo, c.filename, c.url, c.sha256, c.licence, c.licence_source, c.family, c.published, c.precision, c.revision)
        if not all(isinstance(v, str) for v in text_fields) or not c.url.startswith("https://"):
            return None
        if not isinstance(c.size, int) or isinstance(c.size, bool) or not isinstance(c.texts, dict):
            return None
        if not all(isinstance(k, str) and isinstance(v, str) for k, v in c.texts.items()):
            return None
        if not all(isinstance(v, list) and all(isinstance(x, str) for x in v) for v in (c.names, c.evidence)):
            return None
        if not (c.base_licence is None or isinstance(c.base_licence, str)):
            return None
        return c

    @property
    def title(self) -> str:
        variant = " (float16)" if self.precision == "fp16" else ""
        return f"BiRefNet v2 {self.family}{variant}"


def pre_download_reason(c: Candidate) -> str:
    """Why this candidate cannot be switched to automatically, judged before any download."""
    if not c.v2:
        return "it is not marked as BiRefNet v2"
    if not c.filename.lower().endswith(".onnx"):
        return "it is not an ONNX file"
    if c.published[:10] < V2_NOT_BEFORE:
        return f"it was published before {V2_NOT_BEFORE}, when per-file licences were announced"
    if c.family not in TASK_ORDER:
        return "it is not a matting, portrait or general model (lite, anime and dataset-specific variants are skipped)"
    if c.prerelease:
        return "it is marked as a pre-release"
    reason = licence_verdict(c.licence, c.texts, c.names, c.gated, c.base_licence)
    if reason:
        return reason
    if not re.fullmatch(r"[0-9a-f]{64}", c.sha256 or ""):
        return "the server publishes no SHA-256 checksum for this file"
    if c.external_data:
        return "its weights are split into separate .onnx_data files, which are not supported"
    if not 0 < c.size <= models.MAX_MODEL_BYTES:
        return f"its size ({c.size / 1e9:.1f} GB) is outside what the app loads (at most 2.5 GB)"
    return ""


def _rank(c: Candidate) -> tuple:
    """Sort key: matting > portrait > general, float32 before float16, the author's own
    upload before a conversion, newest first."""
    task = TASK_ORDER.index(c.family) if c.family in TASK_ORDER else len(TASK_ORDER)
    published = _parse_time(c.published)
    return (task, c.precision != "fp32", not c.original, -published.timestamp() if published else 0.0)


# ------------------------------------------------------------------ HTTP
class RateLimited(Exception):
    def __init__(self, host: str, until: dt.datetime | None) -> None:
        super().__init__(f"{host} rate limit reached")
        self.host = host
        self.until = until


class HttpStatusError(Exception):
    def __init__(self, url: str, status: int) -> None:
        super().__init__(f"HTTP {status} for {url}")
        self.url = url
        self.status = status


@dataclass
class Reply:
    status: int
    headers: dict[str, str]  # lower-case names
    body: bytes
    url: str


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def _urllib_transport(url: str, method: str, headers: dict[str, str], redirect: bool, limit: int) -> Reply:
    """One HTTP request; every status comes back as a Reply (errors raise OSError)."""
    req = urllib.request.Request(url, method=method, headers={"User-Agent": USER_AGENT, **headers})
    opener = urllib.request.build_opener() if redirect else urllib.request.build_opener(_NoRedirect)
    try:
        resp = opener.open(req, timeout=_TIMEOUT)
    except urllib.error.HTTPError as exc:
        with exc:
            body = exc.read(min(limit, 1 << 16)) if method != "HEAD" else b""
            return Reply(exc.code, {k.lower(): v for k, v in exc.headers.items()}, body, url)
    with resp:
        body = b"" if method == "HEAD" else resp.read(limit + 1)
        if len(body) > limit:
            raise ValueError(f"the response from {urllib.parse.urlsplit(url).netloc} is larger than expected")
        return Reply(resp.status, {k.lower(): v for k, v in resp.headers.items()}, body, url)


# Replaced by the tests with recorded responses.
_transport: Callable[[str, str, dict, bool, int], Reply] = _urllib_transport


def _reset_time(headers: dict[str, str]) -> dt.datetime | None:
    """When a rate-limited server says it may be asked again (Retry-After, X-RateLimit-Reset, RateLimit t=)."""
    now = _now()
    times: list[dt.datetime] = []
    retry = headers.get("retry-after", "").strip()
    if retry.isdigit():
        times.append(now + dt.timedelta(seconds=int(retry)))
    elif retry:
        try:
            times.append(email.utils.parsedate_to_datetime(retry).astimezone(dt.timezone.utc))
        except (TypeError, ValueError):
            pass
    reset = headers.get("x-ratelimit-reset", "").strip()
    if reset.isdigit():
        times.append(dt.datetime.fromtimestamp(int(reset), dt.timezone.utc))
    m = re.search(r"\bt=(\d+)", headers.get("ratelimit", ""))
    if m:
        times.append(now + dt.timedelta(seconds=int(m.group(1))))
    return max(times) if times else None


class _Session:
    """HTTP for one check: conditional requests, a request count, and cancellation."""

    def __init__(self, state: dict, cancel: threading.Event | None) -> None:
        self.etags: dict[str, str] = state.setdefault("etags", {})
        self.cancel = cancel
        self.requests = 0
        self.pending_etags: dict[str, str] = {}

    def check_cancel(self) -> None:
        if self.cancel is not None and self.cancel.is_set():
            raise models.DownloadCancelled()

    def request(self, url: str, method: str = "GET", headers: dict | None = None, redirect: bool = True, limit: int = _API_LIMIT) -> Reply:
        self.check_cancel()
        self.requests += 1
        reply = _transport(url, method, dict(headers or {}), redirect, limit)
        if reply.status in (403, 429):
            raise RateLimited(urllib.parse.urlsplit(url).netloc, _reset_time(reply.headers))
        return reply

    def get(self, url: str, conditional: bool = False, limit: int = _API_LIMIT) -> bytes | None:
        """The body, or None for 304 Not Modified (conditional requests only)."""
        headers = {"Accept": "application/json"} if "/api" in url or "api.github.com" in url else {}
        if conditional and self.etags.get(url):
            headers["If-None-Match"] = self.etags[url]
        reply = self.request(url, headers=headers, limit=limit)
        if reply.status == 304 and conditional:
            return None
        if reply.status != 200:
            raise HttpStatusError(url, reply.status)
        if conditional and reply.headers.get("etag"):
            self.pending_etags[url] = reply.headers["etag"]  # stored once the body was used
        return reply.body

    def get_json(self, url: str, conditional: bool = False):
        body = self.get(url, conditional)
        if body is None:
            return None
        return json.loads(body.decode("utf-8"))

    def get_text(self, url: str) -> str:
        return self.get(url, limit=_TEXT_LIMIT).decode("utf-8", "replace")

    def commit_etags(self) -> None:
        self.etags.update(self.pending_etags)
        self.pending_etags.clear()


# ------------------------------------------------------------------ state
def state_path() -> Path:
    return paths.data_dir() / STATE_NAME


def _load_state() -> dict:
    try:
        with open(state_path(), encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        data = {}
    if not isinstance(data, dict) or data.get("version") != STATE_VERSION:
        data = {"version": STATE_VERSION}
    for key, kind in (("etags", dict), ("known", dict), ("candidates", dict)):
        if not isinstance(data.get(key), kind):
            data[key] = kind()
    return data


def _save_state(state: dict) -> None:
    path = state_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
        with open(tmp, "w", encoding="utf-8", newline="") as fh:
            json.dump(state, fh, indent=1, ensure_ascii=False)
            fh.write("\n")
        os.replace(tmp, path)
    except OSError:
        pass
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except (OSError, NameError):
            pass


def _next_slot(now: dt.datetime) -> dt.datetime:
    """A week from now plus a random delay of up to a day, so installs do not all ask at once."""
    return now + CHECK_INTERVAL + dt.timedelta(seconds=random.randrange(int(CHECK_JITTER.total_seconds())))


def is_check_due() -> bool:
    """True when the weekly check (plus its random delay) is due and no rate limit applies."""
    state = _load_state()
    now = _now()
    blocked = _parse_time(state.get("blocked_until"))
    if blocked is not None and now < blocked:
        return False
    nxt = _parse_time(state.get("next_check"))
    return nxt is None or now >= nxt


def last_status() -> dict:
    state = _load_state()
    last = state.get("last_check")
    message = state.get("message")
    return {
        "last_check": last if isinstance(last, str) else "",
        "message": message if isinstance(message, str) else "",
        "preferred": models.preferred_segmenter(),
    }


def _finish(state: dict, report: UpdateReport, checked: bool) -> UpdateReport:
    now = _now()
    if checked:
        state["last_check"] = _iso(now)
        state["next_check"] = _iso(_next_slot(now))
    state["status"] = report.status
    state["message"] = report.message
    _save_state(state)
    return report


# ------------------------------------------------------------------ discovery
@dataclass
class _Findings:
    candidates: list[Candidate] = field(default_factory=list)
    notices: list[str] = field(default_factory=list)  # v2 without a usable file, and similar
    news: list[str] = field(default_factory=list)  # new files from the author, not marked v2
    fresh: set[tuple[str, str]] = field(default_factory=set)  # (source, repo) read in full this time


def _known(state: dict) -> dict:
    known = state["known"]
    for key, kind in (("gh_assets", list), ("gh_repos", dict), ("hf", dict)):
        if not isinstance(known.get(key), kind):
            known[key] = kind()
    return known


def _scan_github_releases(repo: str, releases, http: _Session, state: dict, found: _Findings, baseline: bool) -> None:
    if not isinstance(releases, list):
        raise ValueError(f"unexpected release list from GitHub for {repo}")
    known_assets = set(_known(state)["gh_assets"])
    readme: str | None = None
    for rel in releases:
        if not isinstance(rel, dict) or rel.get("draft"):
            continue
        tag = str(rel.get("tag_name") or "")
        rel_name = str(rel.get("name") or "")
        body = str(rel.get("body") or "")
        published = str(rel.get("published_at") or rel.get("created_at") or "")
        release_v2 = is_v2_tag(tag) or has_v2_marker(rel_name, repo.split("/")[-1])
        assets = [a for a in rel.get("assets") or [] if isinstance(a, dict) and isinstance(a.get("name"), str)]
        names = [a["name"] for a in assets]
        onnx_assets = [a for a in assets if a["name"].lower().endswith(".onnx")]
        # A release can gain assets long after it was published (v1 did for 11 months).
        for a in assets:
            aid = str(a.get("id"))
            if aid not in known_assets:
                known_assets.add(aid)
                if not baseline and a["name"].lower().endswith(WEIGHT_SUFFIXES):
                    found.news.append(f"{a['name']} (GitHub {repo}, release {tag})")
        v2_weights = [a for a in assets if a["name"].lower().endswith(WEIGHT_SUFFIXES) and (release_v2 or has_v2_marker(a["name"]))]
        if v2_weights and not any(a in onnx_assets for a in v2_weights):
            found.notices.append(f"BiRefNet v2 files were published on GitHub ({repo}, {tag}) without an ONNX file (PyTorch weights only)")
        for a in onnx_assets:
            if not (release_v2 or has_v2_marker(a["name"])):
                continue
            if readme is None:
                try:
                    readme = http.get_text(f"https://raw.githubusercontent.com/{repo}/HEAD/README.md")
                except HttpStatusError:
                    readme = ""
            label = str(a.get("label") or "")
            stem = a["name"].rsplit(".", 1)[0]
            digest = str(a.get("digest") or "")
            sha = digest[7:].lower() if re.fullmatch(r"sha256:[0-9a-fA-F]{64}", digest) else ""
            lic, where = "", ""
            if licence_in_text(label):
                lic, where = licence_in_text(label), "the asset label"
            else:
                for line in body.splitlines():
                    if stem.lower() in line.lower() and licence_in_text(line):
                        lic, where = licence_in_text(line), "the release notes"
                        break
            section = readme_sections(readme, [stem, tag] if tag else [stem])
            if not lic:
                for line in section.splitlines():
                    if stem.lower() in line.lower() and licence_in_text(line):
                        lic, where = licence_in_text(line), "the README"
                        break
            base = a["name"][: -len(".onnx")]
            found.candidates.append(
                Candidate(
                    id=f"gh:{repo}:{a.get('id')}:{sha or a.get('updated_at', '')}",
                    source="github",
                    repo=repo,
                    filename=a["name"],
                    url=str(a.get("browser_download_url") or ""),
                    size=int(a.get("size") or 0),
                    sha256=sha,
                    licence=lic,
                    licence_source=where,
                    family=family_of(a["name"]),
                    published=str(a.get("created_at") or published),
                    v2=True,
                    prerelease=bool(rel.get("prerelease")),
                    external_data=any(n != a["name"] and n.lower().startswith(base.lower()) and n.lower().endswith(("_data", ".data")) for n in names),
                    precision="fp16" if "fp16" in _tokens(a["name"]) else "fp32",
                    texts={"release notes": body, "asset label": label, "README section": section},
                    names=[a["name"], label, tag, rel_name, repo],
                    evidence=[str(rel.get("html_url") or ""), f"https://github.com/{repo}/blob/HEAD/README.md"],
                    revision=tag,
                )
            )
    _known(state)["gh_assets"] = sorted(known_assets)


def _base_models(card: dict) -> list[str]:
    base = card.get("base_model") if isinstance(card, dict) else None
    if isinstance(base, str):
        return [base]
    if isinstance(base, list):
        return [b for b in base if isinstance(b, str)]
    return []


def _card_licence(card: dict) -> str:
    if not isinstance(card, dict):
        return ""
    lic = card.get("license")
    if isinstance(lic, list):
        lic = lic[0] if len(lic) == 1 else "other"
    lic = lic if isinstance(lic, str) else ""
    if normalise_licence(lic) == "other" and isinstance(card.get("license_name"), str):
        return f"other ({card['license_name']})"
    return lic


def _weight_stem(path: str) -> str:
    name = path.rsplit("/", 1)[-1].lower()
    name = re.sub(r"\.(onnx|pth|pt|safetensors|ckpt|bin)$", "", name)
    words = [t for t in re.split(r"[^a-z0-9]+", name) if t and t not in {"fp16", "fp32", "float16", "half"} | QUANTISED_TOKENS]
    return "-".join(words)


def _hf_candidates(repo_id: str, listing: dict, http: _Session, author_cards: dict[str, dict], found: _Findings) -> None:
    """Fetch one v2 repository's file list, card and licence files; add its ONNX files."""
    q = urllib.parse.quote
    info = http.get_json(f"{HF}/api/models/{q(repo_id, safe='/')}?blobs=true")
    if not isinstance(info, dict):
        raise ValueError(f"unexpected model info from Hugging Face for {repo_id}")
    sha = str(info.get("sha") or listing.get("sha") or "")
    if not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise ValueError(f"no commit id for {repo_id}")
    card_data = info.get("cardData") if isinstance(info.get("cardData"), dict) else {}
    gated = bool(info.get("gated"))
    siblings = [s for s in info.get("siblings") or [] if isinstance(s, dict) and isinstance(s.get("rfilename"), str)]
    files = {s["rfilename"]: s for s in siblings}

    def raw(path: str) -> str:
        try:
            return http.get_text(f"{HF}/{q(repo_id, safe='/')}/raw/{sha}/{q(path)}")
        except HttpStatusError:
            return ""

    texts: dict[str, str] = {}
    if "README.md" in files:
        texts["model card"] = raw("README.md")
    for name in files:
        if name.lower() in ("license", "license.md", "license.txt", "licence", "licence.md", "licence.txt"):
            texts["LICENSE file"] = raw(name)
        elif name.lower() == "manifest.json":
            texts["manifest"] = raw(name)
    licence = _card_licence(card_data)
    manifest_licence = ""
    if texts.get("manifest"):
        try:
            manifest = json.loads(texts["manifest"])
            if isinstance(manifest, dict) and isinstance(manifest.get("license"), str):
                manifest_licence = manifest["license"]
        except ValueError:
            pass
    if manifest_licence and licence and normalise_licence(manifest_licence) != normalise_licence(licence):
        licence = f"conflicting ({licence} / {manifest_licence})"

    original = repo_id.split("/")[0] == AUTHOR
    base_licence: str | None = None
    names = [repo_id] + [t for t in info.get("tags") or [] if isinstance(t, str)]
    evidence = [f"{HF}/{repo_id}/tree/{sha}"]
    if not original:
        bases = [b for b in _base_models(card_data) if b.split("/")[0] == AUTHOR]
        if len(bases) != 1:
            return  # not a conversion of exactly one of the author's models
        base = bases[0]
        names.append(base)
        base_card = author_cards.get(base)
        if base_card is None:
            base_info = http.get_json(f"{HF}/api/models/{q(base, safe='/')}")
            base_card = base_info.get("cardData") if isinstance(base_info, dict) and isinstance(base_info.get("cardData"), dict) else {}
            gated = gated or bool(isinstance(base_info, dict) and base_info.get("gated"))
        base_licence = _card_licence(base_card)
        try:
            texts["original model card"] = http.get_text(f"{HF}/{q(base, safe='/')}/raw/main/README.md")
        except HttpStatusError:
            texts["original model card"] = ""
        evidence.append(f"{HF}/{base}")

    weights = [n for n in files if n.lower().endswith(WEIGHT_SUFFIXES)]
    onnx = [n for n in weights if n.lower().endswith(".onnx") and not (_tokens(n) & QUANTISED_TOKENS)]
    if not onnx:
        found.notices.append(f"BiRefNet v2 was published on Hugging Face ({repo_id}) without an ONNX file (PyTorch weights only)")
        return
    several_models = len({_weight_stem(n) for n in weights}) > 1
    published = str(info.get("createdAt") or listing.get("createdAt") or "")
    for name in onnx:
        entry = files[name]
        lfs = entry.get("lfs") if isinstance(entry.get("lfs"), dict) else {}
        sha256 = str(lfs.get("sha256") or "").lower()
        size = lfs.get("size") if isinstance(lfs.get("size"), int) else entry.get("size")
        c = Candidate(
            id=f"hf:{repo_id}@{sha}:{name}",
            source="huggingface",
            repo=repo_id,
            filename=name,
            url=f"{HF}/{q(repo_id, safe='/')}/resolve/{sha}/{q(name)}",
            size=int(size) if isinstance(size, int) else 0,
            sha256=sha256 if re.fullmatch(r"[0-9a-f]{64}", sha256) else "",
            licence=licence,
            licence_source="the model card metadata (license)",
            family=family_of(repo_id, name),
            published=published,
            v2=True,
            gated=gated,
            external_data=any(o != name and o.startswith(name) for o in files),  # model.onnx_data, model.onnx.data
            original=original,
            precision="fp16" if "fp16" in _tokens(name) else "fp32",
            base_licence=base_licence,
            texts=dict(texts),
            names=names + [name],
            evidence=list(evidence),
            revision=sha,
        )
        if several_models:
            c.licence = f"per repository ({licence}), but the repository holds several models"
        found.candidates.append(c)


def _scan_hf(listing, http: _Session, state: dict, found: _Findings, baseline: bool, author_cards: dict[str, dict], converted: bool) -> None:
    if not isinstance(listing, list):
        raise ValueError("unexpected model list from Hugging Face")
    known = _known(state)["hf"]
    for m in listing:
        if not isinstance(m, dict) or not isinstance(m.get("id"), str):
            continue
        repo_id = m["id"]
        owner = repo_id.split("/")[0]
        card = m.get("cardData") if isinstance(m.get("cardData"), dict) else {}
        if converted:
            bases = [b for b in _base_models(card) if b.split("/")[0] == AUTHOR]
            if owner != CONVERTER or not bases:
                continue
        elif owner != AUTHOR:
            continue
        sha = str(m.get("sha") or "")
        weights = sorted(
            str(s["rfilename"])
            for s in m.get("siblings") or []
            if isinstance(s, dict) and str(s.get("rfilename", "")).lower().endswith(WEIGHT_SUFFIXES)
        )
        before = known.get(repo_id) if isinstance(known.get(repo_id), dict) else {}
        new_files = sorted(set(weights) - set(before.get("weights", [])))
        if not converted and not baseline and (not before or new_files):
            found.news.append(f"{repo_id} on Hugging Face ({', '.join(new_files) or 'new repository'})")
        record = {"sha": sha, "weights": weights}
        v2 = has_v2_marker(repo_id, *weights) or (converted and any(has_v2_marker(b) for b in _base_models(card)))
        if v2:
            if before.get("fetched") == sha:
                # Already read at this commit: reuse what was found then.
                _stored_candidates(state, found, source="huggingface", repo=repo_id, revision=sha)
            else:
                _hf_candidates(repo_id, m, http, author_cards, found)
                found.fresh.add(("huggingface", repo_id))
            record["fetched"] = sha
        known[repo_id] = record


def _stored_hf(state: dict, found: _Findings, owner: str) -> None:
    """For an unchanged listing: the candidates read at each repository's current commit."""
    for repo_id, record in _known(state)["hf"].items():
        if repo_id.split("/")[0] == owner and isinstance(record, dict) and record.get("fetched"):
            _stored_candidates(state, found, source="huggingface", repo=repo_id, revision=str(record["fetched"]))


def _discover(state: dict, http: _Session, say: Callable[[str, int, int], None]) -> _Findings:
    """Read the listings and collect candidates. Only fully processed sources keep their ETag."""
    found = _Findings()
    known = _known(state)
    baseline = not state.get("baselined")
    steps = 4
    say("Checking for model updates", 0, steps)

    releases = http.get_json(URL_GH_RELEASES, conditional=True)
    if releases is not None:
        _scan_github_releases(MAIN_REPO, releases, http, state, found, baseline)
        found.fresh.add(("github", MAIN_REPO))
    else:
        _stored_candidates(state, found, source="github", repo=MAIN_REPO)
    say("Checking for model updates", 1, steps)

    repos = http.get_json(URL_GH_REPOS, conditional=True)
    if repos is not None:
        if not isinstance(repos, list):
            raise ValueError("unexpected repository list from GitHub")
        for r in repos:
            if not isinstance(r, dict) or not isinstance(r.get("full_name"), str):
                continue
            full = r["full_name"]
            if "birefnet" not in full.lower() or full == MAIN_REPO or r.get("fork"):
                continue
            pushed = str(r.get("pushed_at") or "")
            if full not in known["gh_repos"] and not baseline:
                found.news.append(f"a new GitHub repository {full}")
            if known["gh_repos"].get(full) != pushed:
                _scan_github_releases(full, http.get_json(f"{GH_API}/repos/{full}/releases?per_page=100"), http, state, found, baseline)
                found.fresh.add(("github", full))
            known["gh_repos"][full] = pushed
    for full in known["gh_repos"]:
        if ("github", full) not in found.fresh:
            _stored_candidates(state, found, source="github", repo=full)
    say("Checking for model updates", 2, steps)

    author = http.get_json(URL_HF_AUTHOR, conditional=True)
    author_cards: dict[str, dict] = {}
    if author is not None:
        if isinstance(author, list):
            author_cards = {m["id"]: m.get("cardData") or {} for m in author if isinstance(m, dict) and isinstance(m.get("id"), str)}
        _scan_hf(author, http, state, found, baseline, author_cards, converted=False)
    else:
        _stored_hf(state, found, AUTHOR)
    say("Checking for model updates", 3, steps)

    converted = http.get_json(URL_HF_CONVERTED, conditional=True)
    if converted is not None:
        _scan_hf(converted, http, state, found, baseline, author_cards, converted=True)
    else:
        _stored_hf(state, found, CONVERTER)
    say("Checking for model updates", 4, steps)

    state["baselined"] = True
    # One record per candidate; a fresh parse (added first) wins over a stored one.
    unique: dict[str, Candidate] = {}
    for c in found.candidates:
        unique.setdefault(c.id, c)
    found.candidates = list(unique.values())
    # Forget candidates that a full read of their source no longer lists (an adopted one stays).
    records = state["candidates"]
    for cid in list(records):
        rec = records[cid]
        if not isinstance(rec, dict) or (
            (rec.get("source"), rec.get("repo")) in found.fresh and cid not in unique and rec.get("status") != "adopted"
        ):
            del records[cid]
    return found


def _stored_candidates(state: dict, found: _Findings, source: str, repo: str = "", owner: str = "", revision: str = "") -> None:
    """Candidates found by an earlier check, for a source that did not change since."""
    for data in state["candidates"].values():
        c = Candidate.from_dict(data)
        if c is None or c.source != source or (revision and c.revision != revision):
            continue
        if (repo and c.repo == repo) or (owner and c.repo.split("/")[0] == owner):
            found.candidates.append(c)


# ------------------------------------------------------------------ ONNX probing
_ELEM_TYPES = {1: "float", 10: "float16", 11: "double", 16: "bfloat16", 7: "int64", 6: "int32", 2: "uint8"}


class ProbeError(Exception):
    """The file could not be read as expected; the probe then decides nothing."""


class _ModelBytes:
    """Random access to an ONNX file, local or over HTTP Range requests, in cached blocks."""

    BLOCK = 1 << 20

    def __init__(self, path: Path | None = None, url: str = "", size: int = 0, http: _Session | None = None) -> None:
        self.path = path
        self.url = url
        self.http = http
        self.size = path.stat().st_size if path is not None else size
        self.cache: dict[int, bytes] = {}
        self.fetched = 0

    def _block(self, index: int) -> bytes:
        if index in self.cache:
            return self.cache[index]
        start = index * self.BLOCK
        end = min(self.size, start + self.BLOCK)
        if start >= end:
            return b""
        if self.path is not None:
            with open(self.path, "rb") as fh:
                fh.seek(start)
                data = fh.read(end - start)
        else:
            if self.fetched + (end - start) > _PROBE_BUDGET:
                raise ProbeError("the model's graph is larger than the probe reads")
            reply = self.http.request(self.url, headers={"Range": f"bytes={start}-{end - 1}"}, limit=self.BLOCK)
            if reply.status != 206:
                raise ProbeError(f"the server did not honour a range request (HTTP {reply.status})")
            data = reply.body
            self.fetched += len(data)
        self.cache[index] = data
        return data

    def read(self, offset: int, n: int) -> bytes:
        out = bytearray()
        while n > 0 and offset < self.size:
            index, skip = divmod(offset, self.BLOCK)
            chunk = self._block(index)[skip : skip + n]
            if not chunk:
                break
            out += chunk
            offset += len(chunk)
            n -= len(chunk)
        return bytes(out)


def _varint(b: bytes, i: int) -> tuple[int, int]:
    value = shift = 0
    while True:
        if i >= len(b):
            raise ProbeError("truncated varint")
        c = b[i]
        i += 1
        value |= (c & 0x7F) << shift
        shift += 7
        if not c & 0x80:
            return value, i
        if shift > 63:
            raise ProbeError("varint too long")


def _pb_fields(b: bytes, i: int = 0, end: int | None = None):
    """Yield (field number, wire type, value, next offset) of a flat protobuf buffer."""
    end = len(b) if end is None else end
    while i < end:
        tag, i = _varint(b, i)
        number, wire = tag >> 3, tag & 7
        if number == 0:
            raise ProbeError("field 0")
        if wire == 0:
            value, i = _varint(b, i)
        elif wire == 2:
            n, i = _varint(b, i)
            if i + n > end:
                raise ProbeError("length past the end")
            value = b[i : i + n]
            i += n
        elif wire == 1:
            value, i = b[i : i + 8], i + 8
        elif wire == 5:
            value, i = b[i : i + 4], i + 4
        else:
            raise ProbeError(f"wire type {wire}")
        yield number, wire, value, i


def _value_info(b: bytes) -> tuple[str, str, list]:
    """(name, element type, dims) of an ONNX ValueInfoProto; a dim is an int, a str or None."""
    name, elem, dims = "", "?", []
    for number, wire, value, _ in _pb_fields(b):
        if number == 1 and wire == 2:
            name = value.decode("utf-8")
        elif number == 2 and wire == 2:
            for tn, tw, tv, _ in _pb_fields(value):
                if tn == 1 and tw == 2:  # tensor_type
                    for sn, sw, sv, _ in _pb_fields(tv):
                        if sn == 1 and sw == 0:
                            elem = _ELEM_TYPES.get(sv, str(sv))
                        elif sn == 2 and sw == 2:
                            for dn, dw, dv, _ in _pb_fields(sv):
                                if dn == 1 and dw == 2:
                                    dim = None
                                    for xn, xw, xv, _ in _pb_fields(dv):
                                        if xn == 1 and xw == 0:
                                            dim = xv
                                        elif xn == 2 and xw == 2:
                                            dim = xv.decode("utf-8", "replace")
                                    dims.append(dim)
    return name, elem, dims


def _header(src: _ModelBytes, offset: int) -> tuple[int, int, int, int]:
    """(field number, wire type, body offset, next offset) of the field starting at offset."""
    b = src.read(offset, 20)
    tag, i = _varint(b, 0)
    number, wire = tag >> 3, tag & 7
    if wire == 0:
        _, j = _varint(b, i)
        return number, wire, offset + j, offset + j
    if wire == 2:
        n, j = _varint(b, i)
        return number, wire, offset + j, offset + j + n
    if wire == 1:
        return number, wire, offset + i, offset + i + 8
    if wire == 5:
        return number, wire, offset + i, offset + i + 4
    raise ProbeError(f"wire type {wire}")


def onnx_signature(src: _ModelBytes) -> dict:
    """IR version, opsets, graph inputs/outputs and the graph's byte range, from the file's
    head and tail only (protobuf writes fields in order: graph = 7, then opset_import = 8;
    inside the graph node = 1 and initializer = 5 come before input = 11 and output = 12)."""
    info: dict = {"ir_version": 0, "opsets": {}}
    offset = 0
    graph = None
    while offset < src.size:
        number, wire, body, nxt = _header(src, offset)
        if number == 1 and wire == 0:  # ir_version
            b = src.read(offset, 20)
            _, i = _varint(b, 0)
            info["ir_version"], _ = _varint(b, i)
        if number == 7 and wire == 2:
            graph = (body, nxt)
            break
        offset = nxt
        if offset > 1 << 20:
            raise ProbeError("no graph near the start of the file")
    if graph is None or graph[1] > src.size:
        raise ProbeError("no graph found")
    g0, g1 = graph
    tail_n = 1 << 16
    best = None
    while True:
        t0 = max(g0, src.size - tail_n)
        tail = src.read(t0, src.size - t0)
        g_end = g1 - t0
        opsets: dict[str, int] = {}
        for number, wire, value, _ in _pb_fields(tail, g_end):
            if number == 8 and wire == 2:
                domain, version = "", 0
                for on, ow, ov, _ in _pb_fields(value):
                    if on == 1 and ow == 2:
                        domain = ov.decode("utf-8", "replace")
                    elif on == 2 and ow == 0:
                        version = ov
                opsets[domain or "ai.onnx"] = version
        for start in range(0, g_end):
            if tail[start] != 0x5A:  # field 11 (input), wire type 2
                continue
            try:
                seq = [(n, v) for n, w, v, _ in _pb_fields(tail, start, g_end)]
                if not all(n in (2, 10, 11, 12, 13, 14, 15, 16) for n, _ in seq) or not any(n == 12 for n, _ in seq):
                    continue
                parsed = [(n, _value_info(v)) for n, v in seq if n in (11, 12)]
                if all(vi[0] for _, vi in parsed):
                    best = parsed
                    break
            except (ProbeError, UnicodeDecodeError, IndexError):
                continue
        if best is not None or t0 == g0 or tail_n >= 1 << 22:
            break
        tail_n *= 4
    if best is None:
        raise ProbeError("the graph inputs were not found")
    info["opsets"] = opsets
    info["inputs"] = [vi for n, vi in best if n == 11]
    info["outputs"] = [vi for n, vi in best if n == 12]
    info["graph"] = graph
    return info


def onnx_nodes(src: _ModelBytes, graph: tuple[int, int], outputs: list[str]) -> tuple[Counter, dict[str, str]]:
    """Operator counts of the graph's node list, and the operator producing each named output."""
    ops: Counter = Counter()
    producers: dict[str, str] = {}
    offset, end = graph
    wanted = set(outputs)
    while offset < end:
        number, wire, body, nxt = _header(src, offset)
        if number != 1 or wire != 2:
            break  # the node list is over (initializers follow)
        node = src.read(body, min(nxt - body, 1 << 16 if nxt - body <= 1 << 16 else 4096))
        op, produced = "", []
        try:
            for n, w, v, _ in _pb_fields(node):
                if n == 2 and w == 2:
                    produced.append(v.decode("utf-8", "replace"))
                elif n == 4 and w == 2:
                    op = v.decode("utf-8", "replace")
                    break
        except ProbeError:
            pass  # a large node read only in part: what was read is enough
        ops[op or "?"] += 1
        for o in produced:
            if o in wanted:
                producers[o] = op
        offset = nxt
    return ops, producers


def probe_model(src: _ModelBytes) -> dict:
    """Signature, operators and output producers; raises ProbeError when unreadable."""
    sig = onnx_signature(src)
    ops, producers = onnx_nodes(src, sig["graph"], [o[0] for o in sig["outputs"]])
    sig["ops"] = ops
    sig["producers"] = producers
    return sig


def probe_reason(sig: dict) -> str:
    """Why a probed model cannot work here ('' when nothing speaks against it)."""
    inputs, outputs = sig["inputs"], sig["outputs"]

    def fits(dims: list, channels: int) -> bool:
        """[1 or symbolic, channels or symbolic, H, W]."""
        if len(dims) != 4:
            return False
        batch, chans = dims[0], dims[1]
        return (batch == 1 or not isinstance(batch, int) or batch <= 0) and (
            chans == channels or not isinstance(chans, int) or chans <= 0
        )

    if len(inputs) != 1 or not fits(inputs[0][2], 3) or inputs[0][1] not in ("float", "float16"):
        return f"it expects {', '.join(f'{n} {d} {t}' for n, t, d in inputs) or 'no input'} instead of one [1, 3, H, W] image"
    if len(outputs) != 1 or not fits(outputs[0][2], 1) or outputs[0][1] not in ("float", "float16"):
        return f"it returns {', '.join(f'{n} {d} {t}' for n, t, d in outputs) or 'nothing'} instead of one [1, 1, H, W] mask"
    for op, why in BANNED_OPS.items():
        if sig["ops"].get(op):
            return f"it uses {why}"
    return ""


# ------------------------------------------------------------------ validation
def asset_dir() -> Path:
    """The bundled assets (the test portrait and its reference mask)."""
    base = getattr(sys, "_MEIPASS", None)
    return Path(base) / "assets" if base else Path(__file__).resolve().parents[1] / "assets"


def speed_reason(seconds: float, baseline: float) -> str:
    """Gate g: '' when the new model takes at most MAX_SLOWDOWN times the current one."""
    if baseline > 0 and seconds > MAX_SLOWDOWN * baseline:
        return f"it is too slow: {seconds:.1f} s on the test portrait against {baseline:.1f} s for the current model"
    return ""


@dataclass
class Validation:
    ok: bool
    reason: str = ""
    output: str = "logits"
    input_size: int | None = 1024
    iou: float = 0.0
    mae: float = 0.0
    seconds: float = 0.0
    baseline_seconds: float = 0.0
    producer: str = ""  # the operator that produces the output (Sigmoid means probabilities)
    environment: bool = False  # failed because of this PC, not the model: try again later


def test_assets_present() -> str:
    """'' when the test portrait and its reference can be read, else the reason."""
    for name in ("selftest_portrait.jpg", "selftest_reference.png"):
        if not (asset_dir() / name).is_file():
            return f"the test portrait for the check is missing ({name})"
    return ""


def _run_cancellable(sess, output_names, feeds, cancel: threading.Event | None):
    """sess.run that stops within a fraction of a second when `cancel` is set."""
    import onnxruntime as ort

    run_options = ort.RunOptions()
    finished = threading.Event()

    def watch() -> None:
        while not finished.wait(0.2):
            if cancel is not None and cancel.is_set():
                run_options.terminate = True
                return

    if cancel is not None:
        threading.Thread(target=watch, name="model-check-cancel", daemon=True).start()
    try:
        return sess.run(output_names, feeds, run_options)
    except Exception:
        if cancel is not None and cancel.is_set():
            raise models.DownloadCancelled() from None
        raise
    finally:
        finished.set()


def _time_run(sess, spec: models.ModelSpec, photo, cancel: threading.Event | None = None) -> tuple[np.ndarray, float]:
    from . import engine

    io = engine.model_io(sess, 3)
    x = engine.seg_input(photo, io, spec)
    t0 = time.perf_counter()
    raw = _run_cancellable(sess, [io.output_name], {io.input_name: x}, cancel)[0]
    return np.asarray(raw, dtype=np.float32), time.perf_counter() - t0


def _baseline_seconds(photo, cancel: threading.Event | None) -> float:
    """How long the current model takes on the test photo (CPU, same settings)."""
    from . import engine

    for key in dict.fromkeys((models.preferred_segmenter(), models.DEFAULT_SEGMENTER, "birefnet-portrait", "birefnet-general")):
        spec = models.MODELS.get(key)
        path = models.find_model(spec) if spec is not None else None
        if path is None:
            continue
        if cancel is not None and cancel.is_set():
            raise models.DownloadCancelled()
        sess = engine.open_cpu_session(path)
        try:
            return _time_run(sess, spec, photo, cancel)[1]
        finally:
            del sess
    raise FileNotFoundError("the current model is not downloaded, so the speed cannot be compared")


def validate_model(
    path: Path,
    spec: models.ModelSpec,
    cancel: threading.Event | None = None,
    baseline_seconds: float | None = None,
) -> Validation:
    """Gates d-g on a downloaded file: loads on the CPU, has the right inputs and outputs,
    gives the reference mask on the test portrait, and is not much slower than the current model."""
    from PIL import Image

    from . import engine

    def cancelled() -> None:
        if cancel is not None and cancel.is_set():
            raise models.DownloadCancelled()

    photo_path = asset_dir() / "selftest_portrait.jpg"
    ref_path = asset_dir() / "selftest_reference.png"
    try:
        with Image.open(photo_path) as im:
            photo = im.convert("RGB")
        with Image.open(ref_path) as im:
            ref = np.asarray(im.convert("L"), dtype=np.float32) / 255.0
    except OSError as exc:
        return Validation(False, f"the test portrait for the check is missing ({_short(exc)})", environment=True)

    cancelled()
    try:
        sess = engine.open_cpu_session(path)
    except Exception as exc:  # ONNX Runtime raises its own exception types
        text = _short(exc)
        hint = " (it uses DeformConv)" if "DeformConv" in text else ""
        return Validation(False, f"ONNX Runtime cannot load it on the CPU{hint}: {text}")
    try:
        try:
            io = engine.model_io(sess, 3)
        except engine.ModelIOError as exc:
            return Validation(False, f"its inputs or outputs do not fit: {exc}")
        outputs = sess.get_outputs()
        if len(outputs) != 1:
            return Validation(False, f"it has {len(outputs)} outputs instead of one [1, 1, H, W] mask")
        # A square fixed size is recorded; a dynamic size is recorded as None (the engine then
        # feeds its default), and a non-square fixed size is read from the model at run time.
        input_size = io.height if io.height is not None and io.height == io.width else None
        probe_spec = replace(spec, input_size=input_size or engine.DEFAULT_INPUT_SIZE)
        cancelled()
        raw, seconds = _time_run(sess, probe_spec, photo, cancel)
    except models.DownloadCancelled:
        raise
    except Exception as exc:
        return Validation(False, f"it failed on the test portrait: {_short(exc)}")
    finally:
        del sess
    if raw.ndim != 4 or not np.isfinite(raw).all() or float(np.ptp(raw)) < 1e-4:
        return Validation(False, "it returns an empty or invalid mask on the test portrait")

    # Logits or probabilities: values outside 0-1 can only be logits. Values inside 0-1 are
    # probabilities (a sigmoid ends the graph, or another op keeps the range); a wrong guess
    # would fail the mask comparison below. The graph's last operator is noted as evidence.
    lo, hi = float(raw.min()), float(raw.max())
    output = "logits" if lo < -1e-3 or hi > 1.0 + 1e-3 else "probs"
    producer = ""
    try:
        producer = probe_model(_ModelBytes(path=Path(path)))["producers"].get(io.output_name, "")
    except (ProbeError, OSError, UnicodeDecodeError, IndexError, KeyError):
        pass
    mask = engine.to_alpha(raw, replace(probe_spec, output=output))
    mask = engine.resize(np.ascontiguousarray(mask, dtype=np.float32), (ref.shape[1], ref.shape[0]), "linear")
    a, b = mask > 0.5, ref > 0.5
    union = int(np.count_nonzero(a | b))
    iou = int(np.count_nonzero(a & b)) / union if union else 0.0
    mae = float(np.abs(mask - ref).mean())
    result = Validation(True, output=output, input_size=input_size, iou=iou, mae=mae, seconds=seconds, producer=producer)
    if iou < MIN_IOU:
        return replace(result, ok=False, reason=f"its mask of the test portrait differs too much from the reference (IoU {iou:.3f}, needs {MIN_IOU})")
    if mae > MAX_MAE:
        return replace(result, ok=False, reason=f"its mask of the test portrait differs too much from the reference (mean difference {mae:.3f}, allowed {MAX_MAE})")

    cancelled()
    try:
        base = baseline_seconds if baseline_seconds is not None else _baseline_seconds(photo, cancel)
    except models.DownloadCancelled:
        raise
    except FileNotFoundError as exc:  # the current model is missing here: not the candidate's fault
        return replace(result, ok=False, reason=_short(exc), environment=True)
    except Exception as exc:
        return replace(result, ok=False, reason=_short(exc))
    result = replace(result, baseline_seconds=base)
    reason = speed_reason(seconds, base)
    return replace(result, ok=False, reason=reason) if reason else result


# ------------------------------------------------------------------ adoption
def _local_filename(c: Candidate) -> str:
    stem = re.sub(r"[^A-Za-z0-9._-]+", "-", c.filename.rsplit("/", 1)[-1].rsplit(".", 1)[0]).strip("-.") or "model"
    if c.source == "huggingface":
        stem = re.sub(r"[^A-Za-z0-9._-]+", "-", c.repo.split("/")[-1]) + "-" + stem
    return f"{stem[:100]}-{c.sha256[:8]}.onnx"


def _new_key(c: Candidate) -> str:
    base = f"birefnet-v2-{c.family}" + ("-fp16" if c.precision == "fp16" else "")
    key, n = base, 2
    while key in models.MODELS and models.MODELS[key].sha256 != c.sha256:
        key = f"{base}-{n}"
        n += 1
    return key


def _spec_for(c: Candidate, validation: Validation | None = None) -> models.ModelSpec:
    source = c.evidence[0] if c.evidence else c.url
    note = f"{c.licence} licence, read from {c.licence_source} ({source}); checked {_now().date().isoformat()}; SHA-256 {c.sha256}"
    return models.ModelSpec(
        key=_new_key(c),
        title=c.title,
        filename=_local_filename(c),
        url=c.url,
        sha256=c.sha256,
        size=c.size,
        licence=c.licence,
        input_size=validation.input_size if validation is not None else 1024,
        output=validation.output if validation is not None else "logits",
        family=c.family,
        licence_note=note,
        discovered=True,
    )


def _probe_remote(c: Candidate, http: _Session) -> str:
    """HEAD check (gated, checksum) and the range-read probe; '' when nothing speaks against it."""
    if c.source == "huggingface":
        reply = http.request(c.url, method="HEAD", redirect=False)
        if reply.status == 401:
            return "the repository is gated: it can only be downloaded after requesting access"
        if reply.status not in (200, 302, 307):
            return f"the file cannot be downloaded (HTTP {reply.status})"
        linked = reply.headers.get("x-linked-etag", "").strip().strip('"').lower()
        if linked and linked != c.sha256:
            return "the download server's checksum differs from the one in the file list"
        size = reply.headers.get("x-linked-size", "")
        if size.isdigit() and int(size) != c.size:
            return "the download server's size differs from the one in the file list"
    try:
        sig = probe_model(_ModelBytes(url=c.url, size=c.size, http=http))
    except ProbeError:
        return ""  # unreadable at a distance: the checks after the download decide
    return probe_reason(sig)


def _download_and_validate(c: Candidate, http: _Session, cancel, say: Progress) -> tuple[str, str, models.ModelSpec | None, Validation | None]:
    """('ok' | 'rejected' | 'retry', reason, spec, validation) for one candidate."""
    missing = test_assets_present()
    if missing:  # check before downloading a gigabyte that could not be checked anyway
        return "retry", missing, None, None
    reason = _probe_remote(c, http)
    if reason:
        return "rejected", reason, None, None
    spec = _spec_for(c)
    folder = models.user_model_dir()
    try:
        folder.mkdir(parents=True, exist_ok=True)
        free = shutil.disk_usage(folder).free
    except OSError as exc:
        return "retry", f"the models folder cannot be used: {_short(exc)}", None, None
    target = folder / spec.filename
    have = models.find_model(spec)
    if have is None and free < 2 * c.size:
        return "retry", f"there is not enough free disk space ({c.size * 2 / 1e9:.1f} GB needed)", None, None
    if have is None or not models.verify_file(have, spec, cancel):
        say(f"Downloading {c.title}", 0, c.size)
        try:
            models.download_model(spec, progress=lambda done, total: say(f"Downloading {c.title}", done, total), cancel=cancel)
        except models.ChecksumMismatch as exc:
            return "rejected", f"the download does not match the published checksum ({_short(exc)})", None, None
        except models.DownloadCancelled:
            raise
        except OSError as exc:
            return "retry", f"the download failed ({_short(exc)})", None, None
        have = target
    say(f"Checking {c.title}", 0, 1)
    validation = validate_model(have, spec, cancel)
    say(f"Checking {c.title}", 1, 1)
    if not validation.ok:
        return ("retry" if validation.environment else "rejected"), validation.reason, None, validation
    return "ok", "", _spec_for(c, validation), validation


def _adopt(c: Candidate, spec: models.ModelSpec, validation: Validation) -> None:
    evidence = {
        "candidate": c.id,
        "source": c.source,
        "repository": c.repo,
        "file": c.filename,
        "revision": c.revision,
        "url": c.url,
        "sha256": c.sha256,
        "licence": c.licence,
        "licence_source": c.licence_source,
        "evidence_urls": [u for u in c.evidence if u],
        "output": validation.output,
        "output_producer": validation.producer,
        "iou": round(validation.iou, 4),
        "mean_difference": round(validation.mae, 4),
        "seconds": round(validation.seconds, 2),
        "baseline_seconds": round(validation.baseline_seconds, 2),
    }
    models.adopt_model(spec, evidence, make_preferred=True)


# ------------------------------------------------------------------ public API
def _record(state: dict, c: Candidate, status: str, reason: str = "") -> None:
    data = c.to_dict()
    data["status"] = status
    data["reason"] = reason
    data["updated"] = _iso(_now())
    state["candidates"][c.id] = data


def _details(c: Candidate, reason: str = "") -> str:
    lines = [f"{c.title}: {c.repo} / {c.filename} ({c.size / 1e6:.0f} MB)", f"Licence: {c.licence or 'none found'} ({c.licence_source or '-'})"]
    if reason:
        lines.append(f"Not adopted because {reason}.")
    lines += [u for u in c.evidence if u]
    return "\n".join(lines)


def check_for_model_updates(
    mode: str,
    force: bool = False,
    cancel: threading.Event | None = None,
    progress: Progress | None = None,
) -> UpdateReport:
    """The weekly check. mode is 'auto', 'ask' or 'off'; force checks now (for 'Check now',
    also in 'off' mode, which then behaves like 'ask'). 'ask' never downloads a model."""
    say = progress or (lambda _s, _d, _t: None)
    if mode not in ("auto", "ask", "off"):
        mode = "ask"
    if mode == "off" and not force:
        return UpdateReport("skipped", "Model updates are turned off.")
    if not _busy.acquire(blocking=False):
        return UpdateReport("skipped", "A model update check is already running.")
    try:
        state = _load_state()
        now = _now()
        blocked = _parse_time(state.get("blocked_until"))
        if blocked is not None and now < blocked:
            return UpdateReport("skipped", f"The update servers asked to wait; the next check is after {blocked:%Y-%m-%d %H:%M} UTC.")
        if not force and not is_check_due():
            return UpdateReport("skipped", "The next model update check is not due yet.")
        http = _Session(state, cancel)
        try:
            found = _discover(state, http, say)
        except models.DownloadCancelled:
            return UpdateReport("skipped", "The model update check was cancelled.")
        except RateLimited as exc:
            nxt = _next_slot(now)
            until = exc.until or nxt
            state["blocked_until"] = _iso(max(until, now))
            state["last_check"] = _iso(now)
            state["next_check"] = _iso(max(nxt, until))
            report = UpdateReport("skipped", f"{exc.host} asked to wait (rate limit); the next check is after {max(nxt, until):%Y-%m-%d}.")
            state["status"], state["message"] = report.status, report.message
            _save_state(state)
            return report
        except Exception as exc:  # network trouble or an unexpected answer: try again next week
            return _finish(state, _failed(exc), True)
        http.commit_etags()
        try:
            return _decide(state, found, "ask" if mode == "off" else mode, http, cancel, say, force)
        except models.DownloadCancelled:
            return _finish(state, UpdateReport("skipped", "The model update was cancelled; it continues at the next check."), True)
        except Exception as exc:  # a background check must never take the app down
            return _finish(state, _failed(exc), True)
    finally:
        _busy.release()


def _failed(exc: BaseException) -> UpdateReport:
    return UpdateReport(
        "failed",
        f"The model update check failed: {_short(exc)}. It will try again next week.",
        details=f"{type(exc).__name__}: {exc}",
    )


def _decide(state: dict, found: _Findings, mode: str, http: _Session, cancel, say: Progress, force: bool) -> UpdateReport:
    records = state["candidates"]
    active = models.MODELS.get(models.preferred_segmenter())
    active_entry = models.catalog_entry(active.key) if active is not None and active.discovered else None
    active_repo = (active_entry or {}).get("evidence", {}).get("repository", "") if active_entry else ""

    usable: list[Candidate] = []
    rejected: list[tuple[Candidate, str, bool]] = []  # (candidate, reason, first time)
    licence_change = ""
    for c in sorted(found.candidates, key=_rank):
        before = records.get(c.id) if isinstance(records.get(c.id), dict) else {}
        if before.get("status") == "adopted" or (active is not None and active.sha256 == c.sha256):
            if active_repo and c.repo == active_repo:
                reason = licence_verdict(c.licence, c.texts, c.names, c.gated, c.base_licence)
                if reason:
                    licence_change = reason
            continue
        if before.get("status") == "rejected":
            if force:
                rejected.append((c, str(before.get("reason", "")), False))
            continue
        reason = pre_download_reason(c)
        if reason:
            rejected.append((c, reason, before.get("status") != "notified-reason"))
            _record(state, c, "notified-reason", reason)
            continue
        usable.append(c)

    if licence_change:
        return _finish(
            state,
            UpdateReport(
                "notified",
                f"The licence information of the model in use changed: {licence_change}. It stays in use; you can switch back to the built-in model in the settings.",
                licence=active.licence if active else "",
            ),
            True,
        )

    if usable and mode == "auto":
        attempts = 0
        for c in usable:
            if attempts >= MAX_DOWNLOADS_PER_CHECK:
                break
            attempts += 1
            try:
                outcome, reason, spec, validation = _download_and_validate(c, http, cancel, say)
            except models.DownloadCancelled:
                return _finish(state, UpdateReport("skipped", "The model update was cancelled; it continues at the next check."), True)
            except RateLimited as exc:
                state["blocked_until"] = _iso(exc.until or _next_slot(_now()))
                return _finish(state, UpdateReport("skipped", f"{exc.host} asked to wait (rate limit); the model update continues later."), True)
            except (OSError, ValueError, HttpStatusError, HTTPException) as exc:
                outcome, reason, spec, validation = "retry", _short(exc), None, None
            if outcome == "ok":
                try:
                    _adopt(c, spec, validation)
                except Exception as exc:  # the catalogue could not be written
                    _record(state, c, "retry", _short(exc))
                    return _finish(state, UpdateReport("failed", f"The new model could not be recorded: {_short(exc)}.", details=_details(c)), True)
                _record(state, c, "adopted")
                return _finish(
                    state,
                    UpdateReport(
                        "adopted",
                        f"Switched to {spec.title}, a newer model that passed every check (licence {c.licence}). The previous model is kept.",
                        adopted_key=spec.key,
                        licence=c.licence,
                        details=_details(c) + f"\nMask check: IoU {validation.iou:.3f}, mean difference {validation.mae:.3f}; {validation.seconds:.1f} s against {validation.baseline_seconds:.1f} s.",
                    ),
                    True,
                )
            if outcome == "retry":
                _record(state, c, "retry", reason)
                return _finish(
                    state,
                    UpdateReport("failed", f"A newer model ({c.title}) was found but could not be checked: {reason}. It will try again next week.", details=_details(c, reason)),
                    True,
                )
            _record(state, c, "rejected", reason)
            rejected.append((c, reason, True))
        usable = []

    # Ask mode (or 'off' with a forced check): offer the best candidate whose inputs, outputs
    # and operators look usable from a distance; nothing is downloaded.
    for c in usable[:MAX_DOWNLOADS_PER_CHECK]:
        try:
            reason = _probe_remote(c, http)
        except RateLimited as exc:
            state["blocked_until"] = _iso(exc.until or _next_slot(_now()))
            return _finish(state, UpdateReport("skipped", f"{exc.host} asked to wait (rate limit); the check continues later."), True)
        except (OSError, ValueError, HttpStatusError, ProbeError, HTTPException):
            reason = ""  # not readable from a distance: the checks after the download decide
        if reason:
            _record(state, c, "rejected", reason)
            rejected.append((c, reason, True))
            continue
        _record(state, c, "notified")
        return _finish(
            state,
            UpdateReport(
                "notified",
                f"A newer model is available: {c.title} ({c.size / 1e6:.0f} MB, licence {c.licence}). Download it and check it?",
                candidate=c.id,
                licence=c.licence,
                details=_details(c),
            ),
            True,
        )

    fresh = [(c, r) for c, r, first in rejected if first or force]
    if fresh:
        c, reason = fresh[0]
        return _finish(
            state,
            UpdateReport(
                "notified",
                f"A newer BiRefNet model was found ({c.repo}: {c.filename}), but the app did not switch to it: {reason}.",
                licence=c.licence,
                details="\n\n".join(_details(x, r) for x, r in fresh),
            ),
            True,
        )
    notices = list(dict.fromkeys(found.notices))
    shown = state.setdefault("notices_shown", [])
    new_notices = [n for n in notices if n not in shown or force]
    if new_notices:
        state["notices_shown"] = sorted(set(shown) | set(new_notices))
        return _finish(state, UpdateReport("notified", new_notices[0] + "; nothing was changed.", details="\n".join(new_notices)), True)
    if found.news:
        return _finish(
            state,
            UpdateReport(
                "notified",
                "New BiRefNet files were published by its author. They are not marked as v2, so nothing was changed.",
                details="\n".join(found.news),
            ),
            True,
        )
    return _finish(state, UpdateReport("up-to-date", "No newer model was found."), True)


def adopt_candidate(
    candidate_id: str,
    cancel: threading.Event | None = None,
    progress: Progress | None = None,
) -> UpdateReport:
    """After the user agreed to an offered candidate (ask mode): download, check and switch.

    Every gate still applies; the user's agreement only replaces the automatic mode.
    """
    say = progress or (lambda _s, _d, _t: None)
    if not _busy.acquire(blocking=False):
        return UpdateReport("skipped", "A model update check is already running.")
    try:
        state = _load_state()
        c = Candidate.from_dict(state["candidates"].get(candidate_id))
        if c is None:
            return UpdateReport("failed", "That model is no longer offered. Check for updates again.")
        reason = pre_download_reason(c)
        if reason:
            _record(state, c, "rejected", reason)
            return _finish(state, UpdateReport("notified", f"{c.title} cannot be used: {reason}.", details=_details(c, reason)), False)
        http = _Session(state, cancel)
        try:
            outcome, reason, spec, validation = _download_and_validate(c, http, cancel, say)
        except models.DownloadCancelled:
            return UpdateReport("skipped", "The download was cancelled; it can continue later.", candidate=c.id)
        except RateLimited as exc:
            return UpdateReport("failed", f"{exc.host} asked to wait (rate limit). Try again later.", candidate=c.id)
        except Exception as exc:  # network trouble or an unexpected answer: the user can try again
            outcome, reason, spec, validation = "retry", _short(exc), None, None
        if outcome == "ok":
            try:
                _adopt(c, spec, validation)
            except Exception as exc:  # the catalogue could not be written
                return UpdateReport("failed", f"The new model could not be recorded: {_short(exc)}.", candidate=c.id)
            _record(state, c, "adopted")
            return _finish(
                state,
                UpdateReport(
                    "adopted",
                    f"Switched to {spec.title} (licence {c.licence}). The previous model is kept.",
                    adopted_key=spec.key,
                    licence=c.licence,
                    details=_details(c),
                ),
                False,
            )
        if outcome == "retry":
            _record(state, c, "notified", reason)
            return _finish(state, UpdateReport("failed", f"{c.title} could not be checked: {reason}. Try again later.", candidate=c.id, details=_details(c, reason)), False)
        _record(state, c, "rejected", reason)
        return _finish(state, UpdateReport("notified", f"{c.title} was not adopted: {reason}.", details=_details(c, reason)), False)
    finally:
        _busy.release()

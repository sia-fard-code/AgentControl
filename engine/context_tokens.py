"""Heuristic active-input token estimator and context-only usage helper.

Every count here is an ESTIMATE. The character-feature prior is a documented
heuristic, not a tokenizer and not proof of any provider limit or envelope.
No filesystem, network, vocabulary loading or model switching happens here.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections import OrderedDict, deque
from dataclasses import dataclass

ERROR_CODES = frozenset({
    "invalid_policy", "invalid_config", "malformed_packet", "cyclic_packet",
    "unsupported_type", "too_deep", "too_large", "counter_failed", "counter_invalid",
    "image_counter_failed", "image_counter_invalid", "invalid_estimate",
    "identity_mismatch", "invalid_usage",
})

MAX_DEPTH = 128
MAX_NODES = 1_000_000
MAX_COUNT = 10 ** 12
MAX_BUCKETS = 32
MAX_RATIOS = 8
MAX_MATCHED = 32
INITIAL_MARGIN = 1.2
CALIBRATED_MARGIN = 1.1
HISTORY_MODES = frozenset({"stateless", "server", "conversation", "unknown"})
SERVER_STATE_KEYS = ("previous_response_id", "conversation")
CONVERSATION_KEYS = frozenset({"messages", "input", "system"})
IMAGE_BLOCK_TYPES = frozenset({"image_url", "input_image", "image"})
# Media prior (heuristic, not a claimed model formula): fit within 2048px,
# then 85 base + 170 per 512px tile; "low" detail = base only.
MEDIA_BASE, MEDIA_TILE, MEDIA_TILE_PX, MEDIA_FIT_PX = 85, 170, 512, 2048
MEDIA_UNKNOWN_DIMS = MEDIA_BASE + MEDIA_TILE * 16


class ContextEstimateError(ValueError):
    """Fixed-code error; the message is the code only, never payload text."""

    def __init__(self, code: str):
        self.code = code if code in ERROR_CODES else "invalid_config"
        super().__init__(self.code)


@dataclass(frozen=True)
class Estimate:
    tokens: int                      # base_tokens with margin/calibration applied
    base_tokens: int                 # raw heuristic or injected-counter count
    packet_sha256: str
    method: str                      # "heuristic" | "injected_counter"
    calibration_samples: int
    observed_input_tokens: int | None  # historical observation of same packet digest, not a forecast
    flags: tuple[str, ...]
    bucket: str                      # digest of calibration bucket key
    identity: str                    # digest of route/model/revision/history
    media_items: int


_RUNS = re.compile(
    r"[A-Za-z0-9]+|\s+|[\x00-\x7f]|[\U0001F000-\U0001FFFF\u2600-\u27bf]|.", re.DOTALL)


def text_tokens(text: str) -> tuple[int, int]:
    """Return (heuristic tokens, non-ASCII codepoints) for one string."""
    tokens = non_ascii = 0
    for match in _RUNS.finditer(text):
        run = match.group()
        first = run[0]
        if first.isascii() and first.isalnum():
            tokens += math.ceil(len(run) / 4)
        elif first.isspace() and (first.isascii() or len(run) > 1):
            tokens += math.ceil(len(run) / 8)
            non_ascii += sum(not c.isascii() for c in run)  # Unicode whitespace stays out of ascii bucket
        elif first.isascii():
            tokens += 1
        else:
            non_ascii += len(run)
            o = ord(first)
            tokens += 3 if 0x1F000 <= o <= 0x1FFFF or 0x2600 <= o <= 0x27BF else 2
    return tokens, non_ascii


def _media_prior(width, height, detail) -> tuple[int, bool]:
    """Return (prior tokens, dims_known)."""
    if detail == "low":
        return MEDIA_BASE, True
    if not (type(width) is int and type(height) is int and width > 0 and height > 0):
        return MEDIA_UNKNOWN_DIMS, False
    scale = min(1.0, MEDIA_FIT_PX / max(width, height))
    w, h = math.ceil(width * scale), math.ceil(height * scale)
    tiles = math.ceil(w / MEDIA_TILE_PX) * math.ceil(h / MEDIA_TILE_PX)
    return MEDIA_BASE + MEDIA_TILE * tiles, True


class _Walk:
    """One pass: heuristic count, canonical sha256, features. Keeps no bodies."""

    def __init__(self, image_type, image_counter):
        self.image_type, self.image_counter = image_type, image_counter
        self.digest, self.schema, self.media = hashlib.sha256(), hashlib.sha256(), hashlib.sha256()
        self.tokens = self.chars = self.non_ascii = self.nodes = self.media_items = 0
        self.has_schema = self.media_prior = self.dims_unknown = False
        self._active: set[int] = set()
        self._skip_holder, self._skip_key = None, None

    def _feed(self, tag: bytes, data: bytes, schema: bool) -> None:
        part = tag + len(data).to_bytes(8, "big") + data
        self.digest.update(part)
        if schema:
            self.schema.update(part)

    def _text(self, text: str, schema: bool, count: bool = True, tag: bytes = b"s") -> None:
        self._feed(tag, text.encode("utf-8", "surrogatepass"), schema)
        if count:
            tokens, non_ascii = text_tokens(text)
            self.tokens += tokens
            self.chars += len(text)
            self.non_ascii += non_ascii

    def walk(self, value, depth=0, schema=False, content=False, key=None) -> None:
        self.nodes += 1
        if depth > MAX_DEPTH:
            raise ContextEstimateError("too_deep")
        if self.nodes > MAX_NODES:
            raise ContextEstimateError("too_large")
        if value is None or type(value) is bool:
            self._feed(b"k", repr(value).encode(), schema)
            self.tokens += 1
        elif type(value) is str:
            self._text(value, schema)
        elif type(value) in (int, float):
            if type(value) is float and not math.isfinite(value):
                raise ContextEstimateError("malformed_packet")
            self._text(repr(value), schema, tag=b"i" if type(value) is int else b"f")
        elif self.image_type is not None and isinstance(value, self.image_type):
            self._asset(value, None, schema)
        elif isinstance(value, (dict, list, tuple)):
            if id(value) in self._active:
                raise ContextEstimateError("cyclic_packet")
            self._active.add(id(value))
            try:
                self._container(value, depth, schema, content, key)
            finally:
                self._active.discard(id(value))
        else:
            raise ContextEstimateError("unsupported_type")

    def _container(self, value, depth, schema, content, key) -> None:
        if isinstance(value, dict):
            kind = value.get("type")
            if content and kind is not None and type(kind) is not str:
                raise ContextEstimateError("malformed_packet")
            if content and kind in IMAGE_BLOCK_TYPES:
                self._image_block(value, schema)
                content = False   # remaining fields walk as plain data
            self._feed(b"{", b"", schema)
            self.tokens += 2 if key in ("messages", "input") else 1  # message/object framing
            for k, v in value.items():
                if type(k) is not str:
                    raise ContextEstimateError("malformed_packet")
                if value is self._skip_holder and k == self._skip_key:
                    self._text(k, schema)
                    self._feed(b"b", b"", schema)  # media body: already hashed by _media
                    continue
                self._text(k, schema or k in ("tools", "functions", "response_format"))
                self.tokens += 1
                in_schema = schema or k in ("tools", "functions", "response_format")
                self.has_schema |= in_schema
                self.walk(v, depth + 1, in_schema, k == "content" and not schema, k)
            self._feed(b"}", b"", schema)
        else:
            self._feed(b"[", b"", schema)
            self.tokens += 1
            for item in value:
                self.walk(item, depth + 1, schema, content, key)
            self._feed(b"]", b"", schema)

    def _image_block(self, block: dict, schema: bool) -> None:
        detail, holder, mime = block.get("detail"), block, None
        key = next((k for k in ("image_url", "source", "image", "file_id") if k in block), None)
        source = block.get(key)
        if isinstance(source, dict):
            holder, detail, mime = source, source.get("detail", detail), source.get("media_type")
            key = next((k for k in ("url", "data", "file_id") if k in source), None)
            source = source.get(key)
        if detail is not None and type(detail) is not str:
            raise ContextEstimateError("malformed_packet")
        self._skip_holder, self._skip_key = holder, key   # all other fields walk normally
        if self.image_type is not None and isinstance(source, self.image_type):
            return self._asset(source, detail, schema)
        if type(source) is not str:
            raise ContextEstimateError("malformed_packet")
        # URL/base64/file_id is hashed (never counted as text, fetched or decoded); dims unknown.
        body = hashlib.sha256(source.encode("utf-8", "surrogatepass")).hexdigest()
        self._media(body, None, None, mime if type(mime) is str else None, detail, schema)

    def _asset(self, asset, detail, schema: bool) -> None:
        data = getattr(asset, "data", None)  # never asset.data_url(): it expands base64
        if type(data) is not bytes:
            raise ContextEstimateError("malformed_packet")
        mime = getattr(asset, "mime", None)
        self._media(hashlib.sha256(data).hexdigest(), getattr(asset, "width", None),
                    getattr(asset, "height", None), mime if type(mime) is str else None,
                    detail, schema)

    def _media(self, body_sha, width, height, mime, detail, schema: bool) -> None:
        shape = json.dumps([body_sha, width, height, mime, detail], default=repr).encode()
        self._feed(b"m", shape, schema)
        self.media.update(len(shape).to_bytes(8, "big") + shape)  # body+MIME: no cross-image calibration
        self.media_items += 1
        if self.image_counter is not None:
            try:
                count = self.image_counter(sha256=body_sha, width=width, height=height,
                                           mime=mime, detail=detail)
            except Exception:
                raise ContextEstimateError("image_counter_failed") from None
            if type(count) is not int or not 0 <= count <= MAX_COUNT:
                raise ContextEstimateError("image_counter_invalid")
            self.tokens += count
            return
        prior, known = _media_prior(width, height, detail)
        self.tokens += prior
        self.media_prior = True
        self.dims_unknown |= not known


def _sha(*parts) -> str:
    return hashlib.sha256(json.dumps(list(parts)).encode()).hexdigest()  # typed, unambiguous framing


_HEX64 = re.compile(r"[0-9a-f]{64}")
# Closed flag set, in the order estimate() generates them.
FLAG_ORDER = ("estimated", "media_prior", "media_dims_unknown", "history_unobservable",
              "calibrated", "uncalibrated", "historical_match")


def _count_ok(v, hi=MAX_COUNT) -> bool:
    return type(v) is int and 0 <= v <= hi


def _valid_estimate(e) -> bool:
    """Shape check of a (possibly replace()d) Estimate; proves no provider count."""
    if type(e) is not Estimate:
        return False
    if not (_count_ok(e.tokens) and _count_ok(e.base_tokens)
            and _count_ok(e.calibration_samples, MAX_RATIOS) and _count_ok(e.media_items, MAX_NODES)
            and (e.observed_input_tokens is None or _count_ok(e.observed_input_tokens))):
        return False
    if not all(type(d) is str and _HEX64.fullmatch(d) for d in (e.packet_sha256, e.bucket, e.identity)):
        return False
    if type(e.method) is not str or e.method not in ("heuristic", "injected_counter"):
        return False
    f = e.flags
    if type(f) is not tuple or not all(type(x) is str for x in f):
        return False
    return (f == tuple(x for x in FLAG_ORDER if x in f) and f[:1] == ("estimated",)
            and ("calibrated" in f) != ("uncalibrated" in f))


class ContextMeter:
    """Estimate active retained INPUT tokens for one route/model/history identity."""

    def __init__(self, policy, *, route, model, model_revision=None, history_mode="stateless",
                 tokenizer=None, image_type=None, image_counter=None):
        for name in ("window_tokens", "reply_tokens", "safety_tokens", "input_limit_tokens"):
            v = getattr(policy, name, None)
            if type(v) is not int or v < 0:
                raise ContextEstimateError("invalid_policy")
        if type(route) is not str or type(model) is not str or not route or not model:
            raise ContextEstimateError("invalid_config")
        if model_revision is not None and type(model_revision) is not str:
            raise ContextEstimateError("invalid_config")
        if type(history_mode) is not str or history_mode not in HISTORY_MODES:
            raise ContextEstimateError("invalid_config")
        if (tokenizer is not None and not callable(tokenizer)) or (
                image_counter is not None and not callable(image_counter)) or (
                image_type is not None and not isinstance(image_type, type)):
            raise ContextEstimateError("invalid_config")
        self._policy = policy
        self._history_mode = history_mode
        self._tokenizer, self._image_type, self._image_counter = tokenizer, image_type, image_counter
        self._identity = _sha("id", route, model, model_revision, history_mode)
        self._buckets: OrderedDict[str, deque] = OrderedDict()   # bucket -> positive ratios
        self._matched: OrderedDict[str, int] = OrderedDict()     # packet sha -> last observed
        self._estimates = self._observations = self._unknown_usage = 0

    def __repr__(self) -> str:
        return f"ContextMeter(buckets={len(self._buckets)}, samples={self._samples()})"

    @property
    def policy(self):
        """The policy object this meter was built with (read-only)."""
        return self._policy

    def _samples(self) -> int:
        return sum(len(r) for r in self._buckets.values())

    def estimate(self, payload) -> Estimate:
        if not isinstance(payload, dict):
            raise ContextEstimateError("malformed_packet")
        walk = _Walk(self._image_type, self._image_counter)
        walk.walk(payload)
        flags = ["estimated"]
        base, method = walk.tokens + 3, "heuristic"   # +3 request envelope prior
        if self._tokenizer is not None:
            try:
                count = self._tokenizer(payload)
            except Exception:
                raise ContextEstimateError("counter_failed") from None
            if type(count) is not int or not 0 <= count <= MAX_COUNT:
                raise ContextEstimateError("counter_invalid")
            base, method = count, "injected_counter"
        if walk.media_prior:
            flags.append("media_prior")
        if walk.dims_unknown:
            flags.append("media_dims_unknown")
        if self._history_mode != "stateless" or any(payload.get(k) is not None for k in SERVER_STATE_KEYS):
            flags.append("history_unobservable")
        lang = "ascii" if walk.non_ascii == 0 else ("mixed" if walk.non_ascii * 4 < walk.chars else "unicode")
        size = max(0, base).bit_length()
        bucket = _sha("b", self._identity, method, lang, size, walk.schema.hexdigest() if walk.has_schema else "-",
                      walk.media.hexdigest() if walk.media_items else "-", walk.media_items)
        ratios = self._buckets.get(bucket)
        samples = len(ratios) if ratios else 0
        if samples:
            tokens = math.ceil(base * max(ratios) * CALIBRATED_MARGIN)
            flags.append("calibrated")
        else:
            tokens = math.ceil(base * INITIAL_MARGIN)
            flags.append("uncalibrated")
        if tokens > MAX_COUNT:
            raise ContextEstimateError("too_large")  # never clamp the margin away silently
        packet_sha = walk.digest.hexdigest()
        observed = self._matched.get(packet_sha)
        if observed is not None:
            flags.append("historical_match")  # past observation only, never a future-exact claim
        self._estimates += 1
        return Estimate(tokens=tokens, base_tokens=base, packet_sha256=packet_sha,
                        method=method, calibration_samples=samples, observed_input_tokens=observed,
                        flags=tuple(flags), bucket=bucket, identity=self._identity,
                        media_items=walk.media_items)

    def observe(self, estimate, *, input_tokens) -> None:
        # Estimate is public and dataclasses.replace() bypasses estimate(): validate every
        # consumed field and the prospective ratio before touching any counter or map.
        if not _valid_estimate(estimate):
            raise ContextEstimateError("invalid_estimate")
        if estimate.identity != self._identity:
            raise ContextEstimateError("identity_mismatch")
        if input_tokens is None:
            self._unknown_usage += 1   # uncertainty preserved; history untouched
            return
        if type(input_tokens) is not int or not 0 <= input_tokens <= MAX_COUNT:
            raise ContextEstimateError("invalid_usage")
        ratio = None
        # Zero, server-history and zero-base observations carry no usable ratio.
        if input_tokens and estimate.base_tokens and "history_unobservable" not in estimate.flags:
            ratio = input_tokens / estimate.base_tokens
            if not (math.isfinite(ratio) and ratio > 0):
                raise ContextEstimateError("invalid_estimate")
        self._observations += 1
        self._matched[estimate.packet_sha256] = input_tokens
        self._matched.move_to_end(estimate.packet_sha256)
        while len(self._matched) > MAX_MATCHED:
            self._matched.popitem(last=False)
        if ratio is None:
            return
        ratios = self._buckets.setdefault(estimate.bucket, deque(maxlen=MAX_RATIOS))
        ratios.append(ratio)
        self._buckets.move_to_end(estimate.bucket)
        while len(self._buckets) > MAX_BUCKETS:
            self._buckets.popitem(last=False)

    def metadata(self) -> dict:
        p = self._policy
        return {"identity_sha256": self._identity, "history_mode": self._history_mode,
                "method": "injected_counter" if self._tokenizer else "heuristic",
                "window_tokens": p.window_tokens, "input_limit_tokens": p.input_limit_tokens,
                "buckets": len(self._buckets), "calibration_samples": self._samples(),
                "estimates": self._estimates, "observations": self._observations,
                "unknown_usage": self._unknown_usage}


def _count(raw: dict, key: str):
    """Return int, None (absent/null) or raise for malformed counters."""
    v = raw.get(key)
    if v is None:
        return None
    if type(v) is not int or v < 0:
        raise ContextEstimateError("invalid_usage")
    return v


def input_tokens_from_usage(raw, *, route=None) -> int | None:
    """Active input tokens from one response's usage (context only, not billing).

    route is the engine-owned call route ("chat"/"responses"/None), never model input.
    Chat prompt_tokens and, on route "responses" only, a bare Responses input_tokens
    already include nested cached tokens: counted once. Native Anthropic input_tokens,
    cache_read_input_tokens and cache_creation_input_tokens are disjoint: summed, and
    any absent/null component leaves the total unknown. Key absence never selects a
    dialect. Unknown stays None; a complete all-zero input stays 0.
    """
    if route not in (None, "chat", "responses"):
        raise ValueError("invalid_route")
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ContextEstimateError("invalid_usage")
    native_keys = ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")
    prompt = _count(raw, "prompt_tokens")
    native = [_count(raw, k) for k in native_keys]   # validate every counter first
    if prompt is not None:
        return prompt
    if route == "responses" and not any(k in raw for k in native_keys[1:]):
        return native[0]   # trusted inclusive Responses total (or None when missing)
    if any(n is None for n in native):
        return None   # partial/ambiguous disjoint usage: an unknown component is not 0
    return sum(native)

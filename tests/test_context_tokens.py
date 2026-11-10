"""Tests for engine/context_tokens.py (heuristic active-input estimator). Synthetic text only."""

from __future__ import annotations

import dataclasses
import importlib.util
import math
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

MODULE_PATH = Path(__file__).resolve().parents[1] / "engine" / "context_tokens.py"


def _load():
    # Missing module must fail loudly, never skip.
    assert MODULE_PATH.is_file(), "engine/context_tokens.py is missing"
    spec = importlib.util.spec_from_file_location("agentcontrol_context_tokens", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses need the module registered before exec
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def ct():
    return _load()


POLICY = SimpleNamespace(
    window_tokens=1_000_000,
    reply_tokens=32_000,
    safety_tokens=16_000,
    input_limit_tokens=952_000,
    auto_compact=False,
    compact_trigger_tokens=None,
)


def _meter(ct, **kw):
    kw.setdefault("route", "r1")
    kw.setdefault("model", "model-v1")
    return ct.ContextMeter(POLICY, **kw)


def _packet(text, **extra):
    packet = {"model": "model-v1", "messages": [{"role": "user", "content": text}]}
    packet.update(extra)
    return packet


def test_large_ascii_packet_estimates_tokens_not_bytes(ct):
    text = ("alpha beta gamma delta " * 15_000)  # ~345 KB of ASCII
    assert len(text.encode()) > 256 * 1024
    est = _meter(ct).estimate(_packet(text))
    words = 60_000
    # ~1 token per short word + whitespace; far below one token per byte.
    assert words <= est.base_tokens < len(text) // 2
    assert est.tokens >= est.base_tokens * 1.2  # initial 20% margin
    assert est.method == "heuristic"
    assert est.calibration_samples == 0
    assert est.observed_input_tokens is None
    assert "estimated" in est.flags


def test_text_features_unicode_emoji_whitespace_and_escaped_literal(ct):
    assert ct.text_tokens("abcdefgh") == (2, 0)
    assert ct.text_tokens(" " * 16) == (2, 0)
    assert ct.text_tokens("!?,") == (3, 0)
    assert ct.text_tokens("\u4e00\u4e01") == (4, 2)
    assert ct.text_tokens("\U0001F600") == (3, 1)
    # A literal backslash-u escape is ASCII text, not the CJK character.
    assert ct.text_tokens("\\u4e00") == (3, 0)
    assert ct.text_tokens("\ud800x")[0] >= 2  # lone surrogate is safe


def test_lone_surrogate_packet_estimates_without_raw_error(ct):
    est = _meter(ct).estimate(_packet("bad \ud800 text"))
    assert est.base_tokens > 0 and len(est.packet_sha256) == 64


def test_schema_tool_and_envelope_growth(ct):
    m = _meter(ct)
    plain = m.estimate(_packet("hello"))
    tool = {"type": "function", "function": {"name": "lookup", "parameters": {
        "type": "object", "properties": {"q": {"type": "string"}}, "required": ["q"]}}}
    tools = m.estimate(_packet("hello", tools=[tool]))
    more = m.estimate(_packet("hello", tools=[tool, tool]))
    scalar = m.estimate(_packet("hello", temperature=0.5))
    assert plain.base_tokens < tools.base_tokens < more.base_tokens
    assert scalar.base_tokens > plain.base_tokens
    assert tools.bucket != plain.bucket


def test_injected_counter_used_and_errors_fixed(ct):
    seen = []
    m = _meter(ct, tokenizer=lambda p: seen.append(p) or 1000)
    est = m.estimate(_packet("x"))
    assert (est.base_tokens, est.method, est.tokens) == (1000, "injected_counter", 1200)
    assert "estimated" in est.flags
    # Non-monotone counters are passed through untouched for a future Guard.
    counts = iter([500, 100])
    nm = _meter(ct, tokenizer=lambda p: next(counts))
    assert nm.estimate(_packet("a")).base_tokens == 500
    assert nm.estimate(_packet("a b c")).base_tokens == 100

    def boom(p):
        raise RuntimeError("SENTINEL_PROMPT_9 leaked")
    for counter, code in ((boom, "counter_failed"), (lambda p: -1, "counter_invalid"),
                          (lambda p: 1.5, "counter_invalid"), (lambda p: True, "counter_invalid")):
        with pytest.raises(ct.ContextEstimateError) as err:
            _meter(ct, tokenizer=counter).estimate(_packet("SENTINEL_PROMPT_9"))
        assert err.value.code == code and str(err.value) == code
        assert err.value.__cause__ is None


def test_usage_helper_three_forms_and_unknown_vs_zero(ct):
    f = ct.input_tokens_from_usage
    chat = {"prompt_tokens": 1000, "completion_tokens": 9,
            "prompt_tokens_details": {"cached_tokens": 800}}
    responses = {"input_tokens": 1000, "output_tokens": 9,
                 "input_tokens_details": {"cached_tokens": 800}}
    anthropic = {"input_tokens": 10, "cache_read_input_tokens": 800,
                 "cache_creation_input_tokens": 190, "output_tokens": 9}
    assert (f(chat), f(responses, route="responses"), f(anthropic)) == (1000, 1000, 1000)
    assert f({"prompt_tokens": 7, "input_tokens": 99}) == 7  # prompt_tokens precedence
    assert f(None) is None and f({}) is None and f({"output_tokens": 5}) is None
    assert f({"input_tokens": 10, "cache_read_input_tokens": None}) is None
    assert f({"input_tokens": 0, "cache_read_input_tokens": 0,
              "cache_creation_input_tokens": 0}) == 0
    assert f({"prompt_tokens": 0}) == 0
    for bad in ({"prompt_tokens": True}, {"prompt_tokens": -1}, {"input_tokens": 1.5},
                {"input_tokens": "10"}, ["prompt_tokens"]):
        with pytest.raises(ct.ContextEstimateError) as err:
            f(bad)
        assert err.value.code == "invalid_usage"


def test_calibration_comparable_bucket_only(ct):
    m = _meter(ct)
    first = m.estimate(_packet("alpha beta gamma " * 50))
    observed = first.base_tokens // 2  # measured downward drift; actual integer, not an assumed 0.5
    m.observe(first, input_tokens=observed)
    assert m.metadata()["calibration_samples"] == 1 and m.metadata()["observations"] == 1
    same = m.estimate(_packet("delta omega sigma " * 50))
    assert same.bucket == first.bucket and same.calibration_samples == 1 and "calibrated" in same.flags
    assert same.tokens == math.ceil(same.base_tokens * (observed / first.base_tokens) * ct.CALIBRATED_MARGIN)
    assert same.tokens < same.base_tokens
    # Unicode, new schema, larger size and media do not inherit English evidence.
    for other in (_packet("\u4e00\u4e8c\u4e09 " * 50),
                  _packet("alpha beta gamma " * 50, tools=[{"name": "t"}]),
                  _packet("alpha beta gamma " * 5000),
                  _packet([{"type": "text", "text": "alpha beta gamma " * 50},
                           {"type": "image_url", "image_url": {"url": "https://h.example/i.png"}}])):
        est = m.estimate(other)
        assert est.calibration_samples == 0 and est.tokens >= est.base_tokens * 1.2
    # Upward drift: the largest recent ratio wins.
    m.observe(m.estimate(_packet("alpha beta gamma " * 50)), input_tokens=first.base_tokens * 3)
    up = m.estimate(_packet("alpha beta gamma " * 50))
    assert up.tokens >= up.base_tokens * 3 * 1.1 - 1


def test_zero_none_invalid_measurements(ct):
    m = _meter(ct)
    est = m.estimate(_packet("alpha beta"))
    m.observe(est, input_tokens=est.base_tokens * 2)
    before = m.estimate(_packet("alpha beta"))
    m.observe(est, input_tokens=None)
    m.observe(est, input_tokens=0)
    after = m.estimate(_packet("alpha beta"))
    assert after.tokens == before.tokens > 0  # neither None nor 0 erases or collapses history
    assert after.observed_input_tokens == 0    # measured 0 preserved as 0
    assert "historical_match" in after.flags
    assert m.metadata()["unknown_usage"] == 1
    for bad in (True, -1, 1.5, "10"):
        with pytest.raises(ct.ContextEstimateError) as err:
            m.observe(est, input_tokens=bad)
        assert err.value.code == "invalid_usage"
    assert m.estimate(_packet("alpha beta")) == after


def test_identity_mismatch_and_frozen_estimate(ct):
    a, b = _meter(ct), _meter(ct, model_revision="rev2")
    est = a.estimate(_packet("x"))
    with pytest.raises(ct.ContextEstimateError) as err:
        b.observe(est, input_tokens=10)
    assert err.value.code == "identity_mismatch" and b.metadata()["calibration_samples"] == 0
    with pytest.raises(ct.ContextEstimateError):
        b.observe(object(), input_tokens=10)
    with pytest.raises(Exception):
        est.tokens = 1


def test_failed_estimate_keeps_state_and_payload_unchanged(ct):
    m = _meter(ct)
    good = m.estimate(_packet("a b"))
    m.observe(good, input_tokens=good.base_tokens)
    meta = m.metadata()
    cyclic = _packet("a")
    cyclic["messages"].append(cyclic)
    for bad, code in ((cyclic, "cyclic_packet"), (_packet(object()), "unsupported_type"),
                      (_packet(float("nan")), "malformed_packet"), ({1: "x"}, "malformed_packet"),
                      ("not a dict", "malformed_packet")):
        with pytest.raises(ct.ContextEstimateError) as err:
            m.estimate(bad)
        assert err.value.code == code
    deep = []
    for _ in range(200):
        deep = [deep]
    with pytest.raises(ct.ContextEstimateError):
        m.estimate({"messages": deep})
    assert m.metadata() == meta
    payload = _packet("stable", tools=[{"name": "t"}])
    snapshot = repr(payload)
    m.estimate(payload)
    assert repr(payload) == snapshot


def test_bounded_buckets_and_ratio_windows(ct):
    m = _meter(ct)
    for i in range(40):
        est = m.estimate(_packet("w " * (2 ** (i % 20)), tools=[{"name": f"t{i}"}]))
        m.observe(est, input_tokens=est.base_tokens + 1)
    est = m.estimate(_packet("same text"))
    for _ in range(20):
        m.observe(est, input_tokens=est.base_tokens)
    meta = m.metadata()
    assert meta["buckets"] <= 32 and meta["calibration_samples"] <= 32 * 8
    assert m.estimate(_packet("same text")).calibration_samples == 8


class FakeImage:
    __slots__ = ("path", "mime", "data", "width", "height", "sha256", "source")

    def __init__(self, data, width=1024, height=1024):
        self.path, self.mime, self.data = "C:/secret/SENTINEL_PATH.png", "image/png", data
        self.width, self.height, self.sha256, self.source = width, height, "x", "SENTINEL_SRC"

    def data_url(self):
        raise AssertionError("counter must not expand base64")


def _img_packet(asset, detail=None):
    block = {"type": "image_url", "image_url": {"url": asset}}
    if detail:
        block["image_url"]["detail"] = detail
    return {"messages": [{"role": "user", "content": [block]}]}


def test_media_prior_hashing_and_privacy(ct):
    m = _meter(ct, image_type=FakeImage)
    a = m.estimate(_img_packet(FakeImage(b"\x89PNG-one")))
    b = m.estimate(_img_packet(FakeImage(b"\x89PNG-two")))
    low = m.estimate(_img_packet(FakeImage(b"\x89PNG-one"), detail="low"))
    assert a.packet_sha256 != b.packet_sha256 and a.base_tokens == b.base_tokens
    assert low.base_tokens < a.base_tokens and low.bucket != a.bucket
    assert "media_prior" in a.flags and a.media_items == 1
    # 1024x1024 -> 4 tiles: 85 + 4*170 prior (documented heuristic).
    assert a.base_tokens >= 85 + 4 * 170
    b64 = "data:image/png;base64," + "QUFB" * 50_000
    url = m.estimate(_img_packet(b64))
    # Unknown dims use the 2048px worst-case prior (85 + 16*170), never base64-as-text (~50k).
    assert 2805 <= url.base_tokens < 2900 and "media_dims_unknown" in url.flags
    # Image-shaped dicts inside tool schemas or plain strings are text, not media.
    schema = m.estimate(_packet("x", tools=[{"type": "image_url", "image_url": {"url": "u"}}]))
    assert schema.media_items == 0
    counted = _meter(ct, image_type=FakeImage, image_counter=lambda **k: 7)
    assert "media_prior" not in counted.estimate(_img_packet(FakeImage(b"z"))).flags
    with pytest.raises(ct.ContextEstimateError) as err:
        _meter(ct, image_counter=lambda **k: -3).estimate(_img_packet(b64))
    assert err.value.code == "image_counter_invalid"
    text = repr(a) + repr(m) + repr(m.metadata()) + repr(url)
    for leak in ("SENTINEL", "QUFB", "secret", "https", "base64"):
        assert leak not in text


def test_server_history_flagged_unobservable(ct):
    m = _meter(ct)
    est = m.estimate(_packet("next turn", previous_response_id="resp_1"))
    assert "history_unobservable" in est.flags
    m.observe(est, input_tokens=50_000)
    assert m.metadata()["calibration_samples"] == 0  # no ratio from invisible history
    server = _meter(ct, history_mode="server")
    assert "history_unobservable" in server.estimate(_packet("x")).flags
    assert server.metadata()["identity_sha256"] != m.metadata()["identity_sha256"]


def test_sentinel_absent_from_estimate_and_metadata(ct):
    m = _meter(ct)
    est = m.estimate(_packet("SENTINEL_PROMPT_42 https://example.test/x C:/tmp/f"))
    m.observe(est, input_tokens=99)
    blob = repr(est) + repr(m) + repr(m.metadata())
    for leak in ("SENTINEL_PROMPT_42", "example.test", "C:/tmp"):
        assert leak not in blob


# --- Repair regressions (integrity / packet hashing) ---

def test_r1_margin_over_max_count_is_too_large_not_clamped(ct):
    m = _meter(ct, tokenizer=lambda p: ct.MAX_COUNT)
    meta = m.metadata()
    with pytest.raises(ct.ContextEstimateError) as err:
        m.estimate(_packet("x"))
    assert err.value.code == "too_large" and m.metadata() == meta
    assert _meter(ct, tokenizer=lambda p: ct.MAX_COUNT // 2).estimate(_packet("x")).tokens > ct.MAX_COUNT // 2


def _img(data, mime="image/png"):
    asset = FakeImage(data)
    asset.mime = mime
    return _img_packet(asset)


def test_r2_media_bucket_includes_body_and_mime(ct):
    m = _meter(ct, image_type=FakeImage)
    a = m.estimate(_img(b"A-body"))
    m.observe(a, input_tokens=a.base_tokens // 2)
    assert m.estimate(_img(b"B-body")).calibration_samples == 0
    assert m.estimate(_img(b"A-body", "image/jpeg")).calibration_samples == 0
    assert m.estimate(_img(b"A-body")).calibration_samples == 1


def test_r3_typed_scalar_framing(ct):
    m = _meter(ct)
    for left, right in ((1, "1"), (1.5, "1.5"), (True, "True"), (None, "None"), (0, False)):
        a, b = m.estimate(_packet("x", meta={"x": left})), m.estimate(_packet("x", meta={"x": right}))
        assert a.packet_sha256 != b.packet_sha256, (left, right)
    a, b = m.estimate(_packet("x", meta={"x": 1})), m.estimate(_packet("x", meta={"x": "1"}))
    assert a.base_tokens == b.base_tokens  # heuristic count unchanged
    s1 = m.estimate(_packet("x", tools=[{"name": "t", "enum": [1]}]))
    s2 = m.estimate(_packet("x", tools=[{"name": "t", "enum": ["1"]}]))
    assert s1.packet_sha256 != s2.packet_sha256 and s1.bucket != s2.bucket


def _anth(media_type="image/png", **extra):
    block = {"type": "image", "source": {"type": "base64", "media_type": media_type,
                                         "data": "QUFB" * 20_000}}
    block.update(extra)
    return {"messages": [{"role": "user", "content": [block]}]}


def test_r4_image_block_other_fields_hashed_and_validated(ct):
    m = _meter(ct)
    base = m.estimate(_anth())
    assert base.media_items == 1 and base.base_tokens < 3000  # body hashed, not counted as text
    assert m.estimate(_anth("image/jpeg")).packet_sha256 != base.packet_sha256
    cached = m.estimate(_anth(cache_control={"type": "ephemeral"}))
    assert cached.packet_sha256 != base.packet_sha256
    with pytest.raises(ct.ContextEstimateError) as err:
        m.estimate(_anth(extra=object()))
    assert err.value.code == "unsupported_type"
    loop = {}
    loop["self"] = loop
    with pytest.raises(ct.ContextEstimateError) as err:
        m.estimate(_anth(extra=loop))
    assert err.value.code == "cyclic_packet"


def test_r5_identity_hash_is_unambiguous(ct):
    a = _meter(ct, route="a\x1fb", model="c")
    b = _meter(ct, route="a", model="b\x1fc")
    assert a.metadata()["identity_sha256"] != b.metadata()["identity_sha256"]
    with pytest.raises(ct.ContextEstimateError) as err:
        b.observe(a.estimate(_packet("x")), input_tokens=5)
    assert err.value.code == "identity_mismatch"
    n, s = _meter(ct, model_revision=None), _meter(ct, model_revision="None")
    assert n.metadata()["identity_sha256"] != s.metadata()["identity_sha256"]
    with pytest.raises(ct.ContextEstimateError):
        s.observe(n.estimate(_packet("x")), input_tokens=5)


def test_r6_unhashable_type_and_mode_and_input_image_file_id(ct):
    with pytest.raises(ct.ContextEstimateError) as err:
        _meter(ct).estimate(_packet([{"type": []}]))
    assert err.value.code == "malformed_packet"
    with pytest.raises(ct.ContextEstimateError) as err:
        _meter(ct, history_mode=[])
    assert err.value.code == "invalid_config"
    m = _meter(ct)
    a = m.estimate(_packet([{"type": "input_image", "file_id": "file-a"}]))
    b = m.estimate(_packet([{"type": "input_image", "file_id": "file-b"}]))
    assert a.media_items == 1 and a.packet_sha256 != b.packet_sha256
    assert "media_prior" in a.flags and "media_dims_unknown" in a.flags


def test_r7_non_ascii_whitespace_counts_codepoints_and_bucket(ct):
    assert ct.text_tokens("\u3000\u3000") == (1, 2)
    m = _meter(ct)
    ascii_est = m.estimate(_packet("alpha  beta  " * 50))
    m.observe(ascii_est, input_tokens=ascii_est.base_tokens // 2)
    uni = m.estimate(_packet("alpha\u3000\u3000beta\u3000\u3000" * 50))
    assert uni.base_tokens == ascii_est.base_tokens and uni.calibration_samples == 0


def test_r8_policy_property_read_only(ct):
    m = _meter(ct)
    assert m.policy is POLICY
    with pytest.raises(AttributeError):
        m.policy = None


NAN, INF = float("nan"), float("inf")
_BAD_COUNTS = [True, False, -1, 1.5, "10", "x", NAN, INF, None, 10 ** 12 + 1]
_BAD_DIGESTS = ["x", "", "A" * 64, "g" * 64, "a" * 63, "a" * 65, 1, None, b"a" * 64, ["a" * 64]]
_BAD_FIELDS = (
    [("tokens", v) for v in _BAD_COUNTS] + [("base_tokens", v) for v in _BAD_COUNTS]
    + [("packet_sha256", v) for v in _BAD_DIGESTS] + [("bucket", v) for v in _BAD_DIGESTS]
    + [("identity", v) for v in _BAD_DIGESTS]
    + [("method", v) for v in ("other", "", None, 1, "Heuristic")]
    + [("calibration_samples", v) for v in (True, -1, 9, 1.0, None, "1", NAN)]
    + [("media_items", v) for v in (True, -1, 10 ** 6 + 1, 1.0, None, "0", INF)]
    + [("observed_input_tokens", v) for v in (True, -1, 1.5, "0", NAN, INF, 10 ** 12 + 1)]
    + [("flags", v) for v in (["estimated"], "estimated", ("estimated", "RAW-SENTINEL"),
                              ("estimated", 1), None, ("uncalibrated",), frozenset({"estimated"}))]
)


def _primed(ct):
    m = _meter(ct)
    first = m.estimate(_packet("alpha beta gamma " * 50))
    m.observe(first, input_tokens=first.base_tokens * 2)
    valid = m.estimate(_packet("alpha beta gamma " * 50))
    return m, valid


@pytest.mark.parametrize("field,value", _BAD_FIELDS)
@pytest.mark.parametrize("usage", [7, 0, None])
def test_invalid_estimate_rejected_without_mutation(ct, field, value, usage):
    m, valid = _primed(ct)
    bad = dataclasses.replace(valid, **{field: value})
    snap = m.metadata()
    with pytest.raises(ct.ContextEstimateError) as err:
        m.observe(bad, input_tokens=usage)
    assert err.value.code == "invalid_estimate" and str(err.value) == "invalid_estimate"
    assert m.metadata() == snap  # nothing counted, matched or calibrated
    assert m.estimate(_packet("alpha beta gamma " * 50)) == valid  # forecast/calibration unchanged


def test_foreign_identity_still_identity_mismatch_without_mutation(ct):
    m, valid = _primed(ct)
    foreign = _meter(ct, model_revision="rev2").estimate(_packet("alpha beta gamma " * 50))
    snap = m.metadata()
    for usage in (5, None):
        with pytest.raises(ct.ContextEstimateError) as err:
            m.observe(foreign, input_tokens=usage)
        assert err.value.code == "identity_mismatch"
    assert m.metadata() == snap and m.estimate(_packet("alpha beta gamma " * 50)) == valid


def test_malformed_usage_rejected_without_mutation(ct):
    m, valid = _primed(ct)
    snap = m.metadata()
    for usage in (True, -1, 1.5, "10", NAN, INF, 10 ** 12 + 1, [1]):
        with pytest.raises(ct.ContextEstimateError) as err:
            m.observe(valid, input_tokens=usage)
        assert err.value.code == "invalid_usage"
    assert m.metadata() == snap and m.estimate(_packet("alpha beta gamma " * 50)) == valid


def test_valid_paths_zero_unknown_and_old_estimate_accepted(ct):
    m, valid = _primed(ct)
    old = valid  # legit earlier Estimate: no history measurement required
    m.observe(old, input_tokens=None)
    assert m.metadata()["unknown_usage"] == 1 and m.metadata()["observations"] == 1
    m.observe(old, input_tokens=0)
    after = m.estimate(_packet("alpha beta gamma " * 50))
    assert after.observed_input_tokens == 0 and after.tokens == valid.tokens  # 0 kept, history kept
    assert m.metadata()["calibration_samples"] == 1
    m.observe(dataclasses.replace(old, observed_input_tokens=None), input_tokens=old.base_tokens)
    assert m.metadata()["calibration_samples"] == 2


def test_real_frozen_policy_integration(ct):
    path = MODULE_PATH.parent / "context_policy.py"
    spec = importlib.util.spec_from_file_location("agentcontrol_context_policy", path)
    cp = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = cp
    spec.loader.exec_module(cp)
    policy = cp.Policy(window_tokens=200_000, reply_tokens=32_000, safety_tokens=8_000)
    m = ct.ContextMeter(policy, route="r1", model="model-v1")
    assert m.policy is policy and isinstance(m.policy, cp.Policy)
    md = m.metadata()
    assert md["window_tokens"] == 200_000 and md["input_limit_tokens"] == policy.input_limit_tokens == 160_000
    est = m.estimate(_packet("alpha beta"))
    assert "estimated" in est.flags and est.tokens < policy.input_limit_tokens
    with pytest.raises(dataclasses.FrozenInstanceError):
        policy.window_tokens = 1


def test_usage_absent_null_complete_zero_distinct(ct):
    f = ct.input_tokens_from_usage
    assert f({"input_tokens": 5, "cache_read_input_tokens": 3}) is None  # creation absent
    assert f({"input_tokens": 5, "cache_read_input_tokens": 3, "cache_creation_input_tokens": None}) is None
    assert f({"input_tokens": 0, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}) == 0


def test_usage_dialect_needs_trusted_route_not_key_absence(ct):
    f = ct.input_tokens_from_usage
    native = {"input_tokens": 10, "cache_read_input_tokens": 80, "cache_creation_input_tokens": 10}
    chat = {"prompt_tokens": 100, "prompt_tokens_details": {"cached_tokens": 80}}
    resp = {"input_tokens": 100, "input_tokens_details": {"cached_tokens": 80}}
    assert f(chat, route="chat") == 100 and f(resp, route="responses") == 100
    for route in (None, "chat", "responses"):
        assert f(native, route=route) == 100
        assert f({"prompt_tokens": 0}, route=route) == 0
        assert f({"input_tokens": 0, "cache_read_input_tokens": 0,
                  "cache_creation_input_tokens": 0}, route=route) == 0
        assert f({"output_tokens": 5}, route=route) is None
        assert f({"input_tokens": 100, "cache_creation_input_tokens": 0}, route=route) is None
        assert f({"input_tokens": 100, "cache_read_input_tokens": None,
                  "cache_creation_input_tokens": 0}, route=route) is None
    # Absence of both native components cannot establish an inclusive dialect.
    assert f({"input_tokens": 100}) is None
    assert f({"input_tokens": 100}, route="chat") is None
    assert f(resp) is None and f(resp, route="chat") is None
    assert f({"input_tokens": 0}, route="responses") == 0
    for bad in ({"input_tokens": "1"}, {"cache_read_input_tokens": -1}):
        raw = dict(bad)
        with pytest.raises(ct.ContextEstimateError) as err:
            f(raw, route="responses")
        assert err.value.code == "invalid_usage" and raw == bad
    with pytest.raises(ValueError):
        f(chat, route="anthropic")

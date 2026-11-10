import argparse
import dataclasses
import importlib.util
import math
import sys
from pathlib import Path

import pytest

PATH = Path(__file__).resolve().parents[1] / "engine" / "context_policy.py"


def _load():
    if not PATH.exists():
        pytest.fail("engine/context_policy.py missing")
    spec = importlib.util.spec_from_file_location("_agentcontrol_context_policy", PATH)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod  # dataclasses resolve string annotations via sys.modules
    spec.loader.exec_module(mod)
    return mod


cp = _load() if PATH.exists() else None


def ns(**kw):
    base = dict(context_policy=None, context_window_tokens=None, context_reply_tokens=None,
                context_safety_tokens=None, auto_compact=None, compact_fraction=None)
    base.update(kw)
    return argparse.Namespace(**base)


def test_default_1m_model_policy():
    assert cp is not None, "engine/context_policy.py missing"
    sel = cp.resolve_policy(ns())
    p = sel.policy
    assert sel.provenance == "default" and sel.explicit_fields == ()
    assert (p.window_tokens, p.reply_tokens, p.safety_tokens) == (1_000_000, 32768, 50000)
    assert p.input_limit_tokens == 1_000_000 - 32768 - 50000
    assert p.auto_compact is False and p.compact_fraction == 0.70
    assert p.compact_trigger_tokens == 700_000 and p.compact_target_tokens == 560_000
    assert p.verified_output_limit is None


def err(code, fn, *a, **kw):
    with pytest.raises(cp.ContextPolicyError) as exc:
        fn(*a, **kw)
    assert exc.value.code == code and str(exc.value) == code  # fixed code, no argument bodies


@pytest.mark.parametrize("window,reply,safety", [(160, 10, 8), (16, 1, 1), (3, 1, 1)])
def test_small_injected_window_defaults(window, reply, safety):
    p = cp.resolve_policy({}, model_window=window).policy
    assert (p.reply_tokens, p.safety_tokens, p.input_limit_tokens) == (reply, safety, window - reply - safety)


def test_explicit_window_and_reserves_settings_free(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    sel = cp.resolve_policy({"context_policy": "model-v1", "context_window_tokens": 200_000,
                             "context_reply_tokens": 8000, "context_safety_tokens": 2000})
    assert sel.provenance == "explicit" and sel.policy.input_limit_tokens == 190_000
    assert sel.explicit_fields == ("context_policy", "context_reply_tokens",
                                   "context_safety_tokens", "context_window_tokens")
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("field,value", [("context_policy", "legacy"), ("context_policy", "model-v1"),
                                         ("auto_compact", False), ("compact_fraction", 0.5)])
def test_frozen_rejects_any_override_without_mutation(field, value):
    args = ns(**{field: value})
    before = dict(vars(args))
    err("context_overrides_frozen", cp.resolve_policy, args, frozen=True, model_window=0)
    assert vars(args) == before


def test_frozen_without_override_ignores_unused_route_data():
    sel = cp.resolve_policy(ns(), frozen=True, model_window=-5, route_output_limit="bad")
    assert (sel.policy, sel.provenance, sel.explicit_fields) == (None, "frozen", ())


def test_explicit_legacy_and_legacy_with_options():
    sel = cp.resolve_policy(ns(context_policy="legacy"), model_window=0, route_output_limit=-1)
    assert (sel.policy, sel.provenance, sel.explicit_fields) == (None, "explicit", ("context_policy",))
    assert sel.metadata() == {"context_policy": "legacy", "provenance": "explicit",
                              "explicit_fields": ["context_policy"]}
    err("context_options_require_model_v1", cp.resolve_policy,
        ns(context_policy="legacy", auto_compact=False))


def test_oneshot_defaults_model_v1_but_rejects_compaction():
    assert cp.resolve_policy(ns(), oneshot=True).policy.auto_compact is False
    err("compaction_unavailable_in_oneshot", cp.resolve_policy, ns(auto_compact=True), oneshot=True)


def test_false_flag_tracked_not_enabled():
    sel = cp.resolve_policy(ns(auto_compact=False))
    assert sel.explicit_fields == ("auto_compact",) and sel.policy.auto_compact is False
    assert sel.provenance == "default"


BAD_INTS = [True, 0, -1, 1.5, "10", math.nan, math.inf]


@pytest.mark.parametrize("value", BAD_INTS)
@pytest.mark.parametrize("field,code", [("context_window_tokens", "invalid_window_tokens"),
                                        ("context_reply_tokens", "invalid_reply_tokens"),
                                        ("context_safety_tokens", "invalid_safety_tokens")])
def test_bad_numeric_fields(field, code, value):
    err(code, cp.resolve_policy, ns(**{field: value}))


@pytest.mark.parametrize("value", BAD_INTS)
def test_bad_model_window_and_route_limit(value):
    err("invalid_window_tokens", cp.resolve_policy, ns(), model_window=value)
    err("invalid_route_output_limit", cp.resolve_policy, ns(), route_output_limit=value)


@pytest.mark.parametrize("value", [True, 0, 1, -0.1, 1.5, math.nan, math.inf, "0.5"])
def test_bad_fraction(value):
    err("invalid_compact_fraction", cp.resolve_policy, ns(compact_fraction=value))


@pytest.mark.parametrize("field,value,code", [("auto_compact", 1, "invalid_auto_compact"),
                                              ("auto_compact", "true", "invalid_auto_compact"),
                                              ("context_policy", True, "invalid_context_policy"),
                                              ("context_policy", "Legacy", "invalid_context_policy")])
def test_bad_flags(field, value, code):
    err(code, cp.resolve_policy, ns(**{field: value}))


def test_impossible_reserves():
    err("input_allowance_impossible", cp.resolve_policy, {}, model_window=1)
    err("input_allowance_impossible", cp.resolve_policy,
        {"context_window_tokens": 1000, "context_reply_tokens": 900, "context_safety_tokens": 100})


def test_route_output_limit_known_versus_unknown():
    assert cp.resolve_policy({}).policy.verified_output_limit is None
    p = cp.resolve_policy({}, route_output_limit=64000).policy
    assert (p.reply_tokens, p.verified_output_limit) == (64000, 64000)
    assert cp.resolve_policy({}, route_output_limit=1000).policy.reply_tokens == 32768
    err("reply_below_route_output_limit", cp.resolve_policy,
        {"context_reply_tokens": 999}, route_output_limit=1000)


@pytest.mark.parametrize("fraction,window,trigger,target",
                         [(0.70, 1_000_000, 700_000, 560_000), (0.5, 1001, 500, 400), (0.3, 10_000, 3000, 2400)])
def test_fraction_target_math(fraction, window, trigger, target):
    p = cp.resolve_policy({"auto_compact": True, "compact_fraction": fraction, "context_window_tokens": window})
    assert (p.policy.compact_trigger_tokens, p.policy.compact_target_tokens) == (trigger, target)
    assert type(p.policy.compact_fraction) is float


def test_auto_trigger_must_sit_below_input_limit():
    err("compaction_trigger_invalid", cp.resolve_policy, {"auto_compact": True, "compact_fraction": 0.99})
    assert cp.resolve_policy({"compact_fraction": 0.99}).policy.auto_compact is False  # inactive trigger ok
    err("compaction_trigger_invalid", cp.Policy, 100, 1, 1, True, 0.01)  # target 0


def test_records_immutable():
    sel = cp.resolve_policy(ns(auto_compact=False))
    with pytest.raises(dataclasses.FrozenInstanceError):
        sel.policy.window_tokens = 5
    with pytest.raises(dataclasses.FrozenInstanceError):
        sel.explicit_fields = ()
    assert isinstance(cp.Selection(None, "explicit", ["context_policy"]).explicit_fields, tuple)
    err("invalid_selection", cp.Selection, None, "guess", ())


def test_args_unmodified_and_metadata_closed():
    sentinel = "SYNTHETIC-SENTINEL-7f3a"
    args = ns(compact_fraction=0.6)
    args.prompt = sentinel
    args.messages = [sentinel]
    before = dict(vars(args))
    meta = cp.resolve_policy(args).metadata()
    assert vars(args) == before
    assert sentinel not in repr(meta)
    assert set(meta) == {"context_policy", "provenance", "explicit_fields", "window_tokens",
                         "reply_tokens", "safety_tokens", "input_limit_tokens", "auto_compact",
                         "compact_fraction", "compact_trigger_tokens", "compact_target_tokens",
                         "verified_output_limit"}
    assert all(isinstance(v, (str, int, float, bool, list, type(None))) for v in meta.values())


@pytest.mark.parametrize("auto", [False, True])
def test_huge_window_exact_trigger_no_overflow(auto):
    huge = 10 ** 400  # no arbitrary model-window cap; trigger math must stay exact
    sel = cp.resolve_policy({"context_window_tokens": huge, "auto_compact": auto})
    p = sel.policy
    assert (p.compact_trigger_tokens, p.compact_target_tokens) == (7 * 10 ** 399, 56 * 10 ** 398)
    meta = sel.metadata()
    assert meta["compact_trigger_tokens"] == 7 * 10 ** 399 and meta["auto_compact"] is auto


@pytest.mark.parametrize("value", [10 ** 400, -(10 ** 400)])
def test_huge_int_fraction_is_coded_error(value):
    err("invalid_compact_fraction", cp.Policy, 100, 1, 1, compact_fraction=value)
    err("invalid_compact_fraction", cp.resolve_policy, {"compact_fraction": value})


P = None if cp is None else cp.Policy(1000, 10, 10)


@pytest.mark.parametrize("policy,prov,fields", [
    (P, "frozen", ()), (None, "frozen", ("auto_compact",)), (None, "frozen", ("context_policy",)),
    (None, "default", ()), (None, "explicit", ()), (None, "explicit", ("auto_compact",)),
    (None, "explicit", ("auto_compact", "context_policy")), (None, "default", ("context_policy",)),
    (P, "default", ("context_policy",)), (P, "explicit", ()), (P, "explicit", ("auto_compact",)),
    (P, "explicit", ("context_policy", "auto_compact")), (P, "explicit", ("context_policy", "context_policy")),
])
def test_selection_rejects_impossible_provenance(policy, prov, fields):
    err("invalid_selection", cp.Selection, policy, prov, fields)


@pytest.mark.parametrize("policy,prov,fields", [
    (None, "frozen", ()), (None, "explicit", ("context_policy",)), (None, "explicit", ["context_policy"]),
    (P, "default", ()), (P, "default", ("auto_compact",)), (P, "explicit", ("context_policy",)),
    (P, "explicit", ["auto_compact", "context_policy", "context_window_tokens"]),
])
def test_selection_valid_counterexamples(policy, prov, fields):
    sel = cp.Selection(policy, prov, fields)
    assert sel.explicit_fields == tuple(fields) and isinstance(sel.explicit_fields, tuple)


@pytest.mark.parametrize("fields", [None, 5, True, "context_policy", {"context_policy": 1},
                                    {"context_policy"}, [["x"]], [{}], [None]])
def test_selection_bad_field_containers_are_coded(fields):
    err("invalid_selection", cp.Selection, None, "explicit", fields)

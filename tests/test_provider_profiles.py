"""Measured operating policy, not advertised provider maxima; all CLI runs stay cold."""
import argparse
import dataclasses
import json
import sys
from pathlib import Path

import pytest

from test_engine_reasoning import engine, harness, _finish_reply  # noqa: F401

GPT = "gpt-6.1-sol"
CLAUDE = "anthropic_tool_call.claude-opus-5-5-think"
PROFILE_KEYS = {
    "profile_id", "route", "effective_effort", "configured_operating_window",
    "observed_retained_input_lower_bound", "declared_max", "evidence_effort",
    "evidence_source", "evidence_match", "capacity_evidence_state",
    "cap_support", "cap_evidence_state", "window_provenance",
}


@pytest.fixture(autouse=True)
def isolated(engine, tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(Path(engine.__file__).parent))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("CLAUDE_AGENT_RUN_INDEX", str(tmp_path / "index.jsonl"))
    monkeypatch.delenv("_HARNESS_APPROVED_WORKER", raising=False)
    for name in ("load_cfg", "refresh_models", "preload_skills"):
        monkeypatch.setattr(engine, name, lambda *a, **k: pytest.fail("crossed cold boundary"))


def select(engine, model=GPT, route="chat", effort="high", supplier=None,
           model_window=1_000_000, frozen=False, oneshot=False, **options):
    ce = engine.capability_module("context_engine")
    cp = engine.capability_module("context_policy")
    args = argparse.Namespace(**options)
    return ce.select(args, cp, frozen=frozen, oneshot=oneshot, model_window=model_window,
                     profile_supplier=supplier or (lambda: engine.provider_profile(model, route, effort)))


@pytest.mark.parametrize("route,lower", [("chat", 513757), ("responses", 513746)])
def test_gpt_measured_operating_default(engine, route, lower):
    sel = select(engine, route=route)
    p = sel.policy
    assert (p.window_tokens, p.reply_tokens, p.safety_tokens, p.input_limit_tokens) == (
        512000, 32000, 25600, 454400)
    assert (p.auto_compact, p.compact_fraction, p.compact_trigger_tokens,
            p.compact_target_tokens, p.verified_output_limit) == (False, .70, 358400, 286720, None)
    meta = sel.metadata()["provider_profile"]
    assert meta["configured_operating_window"] == 512000
    assert meta["observed_retained_input_lower_bound"] == lower
    assert meta["declared_max"] is None
    assert meta["evidence_effort"] == "high" and meta["evidence_match"] is True
    assert meta["window_provenance"] == "operating_profile"
    assert meta["cap_support"] == "observed" and meta["cap_evidence_state"] == "numeric_only"


@pytest.mark.parametrize("effort", ["none", "low", "medium", "xhigh", "max"])
def test_other_gpt_efforts_configured_not_silently_tested(engine, effort):
    meta = select(engine, effort=effort).metadata()
    assert meta["window_tokens"] == 512000
    profile = meta["provider_profile"]
    assert profile["effective_effort"] == effort
    assert profile["evidence_effort"] == "high" and profile["evidence_match"] is False
    assert profile["capacity_evidence_state"] == "scope_mismatch"
    assert meta["verified_output_limit"] is None


def test_claude_capacity_and_unsupported_output_contract(engine):
    sel = select(engine, model=CLAUDE, effort="medium")
    meta = sel.metadata()["provider_profile"]
    assert sel.policy.window_tokens == 1_000_000
    assert meta["observed_retained_input_lower_bound"] == 994948
    assert meta["declared_max"] == 1_000_000
    assert meta["evidence_effort"] == "medium" and meta["evidence_match"] is True
    assert (meta["cap_support"], meta["cap_evidence_state"]) == ("unsupported", "ignored")
    assert sel.policy.verified_output_limit is None
    assert select(engine, model=CLAUDE).metadata()["provider_profile"]["evidence_match"] is False


@pytest.mark.parametrize("model,route", [("SYNTHETIC-SECRET", "chat"), (GPT, "unknown"),
                                        ("gpt-6-sol", "responses")])
def test_unknown_identity_uses_only_existing_unverified_configuration(engine, model, route):
    meta = select(engine, model=model, route=route, model_window=700000).metadata()
    profile = meta["provider_profile"]
    assert meta["window_tokens"] == profile["configured_operating_window"] == 700000
    assert profile["profile_id"] == "unprofiled"
    assert profile["capacity_evidence_state"] == "unverified"
    assert profile["window_provenance"] == "existing_configuration"
    assert profile["observed_retained_input_lower_bound"] is None
    assert profile["declared_max"] is None and profile["evidence_match"] is False
    assert profile["cap_support"] == profile["cap_evidence_state"] == "unverified"
    assert "SYNTHETIC-SECRET" not in json.dumps(meta)


def test_explicit_window_and_reserves_are_operator_not_provider_evidence(engine):
    sel = select(engine, context_window_tokens="600000", context_reply_tokens="40000",
                 context_safety_tokens="20000", auto_compact=True, compact_fraction="0.6")
    p = sel.policy
    assert (p.window_tokens, p.reply_tokens, p.safety_tokens) == (600000, 40000, 20000)
    meta = sel.metadata()
    assert meta["provider_profile"]["configured_operating_window"] == 512000
    assert meta["provider_profile"]["window_provenance"] == "operator_override"
    assert "context_window_tokens" in meta["explicit_fields"]
    assert p.verified_output_limit is None and p.compact_trigger_tokens == 360000


@pytest.mark.parametrize("options,code", [
    ({"context_window_tokens": "SYNTHETIC-SECRET"}, "invalid_window_tokens"),
    ({"context_reply_tokens": "0"}, "invalid_reply_tokens"),
    ({"context_safety_tokens": "-1"}, "invalid_safety_tokens"),
    ({"compact_fraction": "nan"}, "invalid_compact_fraction"),
    ({"context_policy": "bad"}, "invalid_context_policy"),
    ({"auto_compact": True, "compact_fraction": "0.99"}, "compaction_trigger_invalid"),
])
def test_invalid_options_do_not_resolve_profile(engine, options, code):
    ce = engine.capability_module("context_engine")
    with pytest.raises(ce.Refused, match="^" + code + "$"):
        select(engine, supplier=lambda: pytest.fail("profile resolved for invalid CLI"), **options)


def test_frozen_and_legacy_are_profile_free_and_metadata_exact(engine):
    never = lambda: pytest.fail("legacy/frozen profile lookup")
    frozen = select(engine, frozen=True, supplier=never)
    legacy = select(engine, context_policy="legacy", supplier=never)
    assert frozen.metadata() == {"context_policy": "legacy", "provenance": "frozen", "explicit_fields": []}
    assert legacy.metadata() == {"context_policy": "legacy", "provenance": "explicit",
                                 "explicit_fields": ["context_policy"]}
    ce = engine.capability_module("context_engine")
    with pytest.raises(ce.Refused, match="context_overrides_frozen"):
        select(engine, frozen=True, supplier=never, context_window_tokens="512000")


def test_no_configured_or_explicit_window_fails_cold(engine):
    ce = engine.capability_module("context_engine")
    with pytest.raises(ce.Refused, match="invalid_window_tokens"):
        select(engine, model_window=None, supplier=lambda: pytest.fail("invalid configuration"))
    assert select(engine, model="unknown", model_window=None,
                  context_window_tokens="200000").policy.window_tokens == 200000


def test_metadata_is_closed_copied_and_selection_immutable(engine):
    sel = select(engine)
    meta = sel.metadata()
    assert set(meta["provider_profile"]) == PROFILE_KEYS
    assert "SYNTHETIC-SECRET" not in json.dumps(meta)
    meta["provider_profile"]["evidence_source"] = "SYNTHETIC-SECRET"
    assert "SYNTHETIC-SECRET" not in json.dumps(sel.metadata())
    with pytest.raises(dataclasses.FrozenInstanceError):
        sel.provider_profile = ()
    cp = engine.capability_module("context_policy")
    with pytest.raises(cp.ContextPolicyError, match="invalid_selection"):
        cp.Selection(sel.policy, sel.provenance, sel.explicit_fields,
                     provider_profile=(("evidence_source", "SYNTHETIC-SECRET"),))


@pytest.mark.parametrize("model,extra,expected_route,expected_effort,window", [
    (GPT, [], "responses", "high", 512000),
    (GPT, ["--no-stream", "-e", "none"], "chat", "low", 512000),
    (GPT, ["--tool-mode", "prompt", "--allow-prompt-tools"], "chat", "high", 512000),
    (CLAUDE, ["-e", "medium"], "chat", "medium", 1000000),
    ("SYNTHETIC-SECRET", [], "chat", "high", 1000000),
])
def test_source_cli_selects_actual_identity_before_skills_credentials(
        engine, monkeypatch, tmp_path, model, extra, expected_route, expected_effort, window):
    ce = engine.capability_module("context_engine")
    real = ce.select
    captured = []

    class Selected(Exception):
        pass

    def capture(*args, **kwargs):
        result = real(*args, **kwargs)
        captured.append(result)
        raise Selected

    monkeypatch.setattr(ce, "select", capture)
    monkeypatch.setattr(sys, "argv", ["delegate.py", "-t", "synthetic", "-C", str(tmp_path),
                                     "--profile", "legacy", "-m", model, *extra])
    with pytest.raises(Selected):
        engine.main()
    meta = captured[0].metadata()
    assert meta["window_tokens"] == window
    assert meta["provider_profile"]["route"] == expected_route
    assert meta["provider_profile"]["effective_effort"] == expected_effort


@pytest.mark.parametrize("oneshot", [False, True])
def test_runtime_capsule_and_completion_metadata_use_effective_policy(
        engine, harness, monkeypatch, oneshot):
    captured = []
    runtime = engine.capability_module("context_runtime").ContextRuntime
    original = runtime.__init__

    def initialize(self, *args, **kwargs):
        original(self, *args, **kwargs)
        captured.append(self)

    monkeypatch.setattr(runtime, "__init__", initialize)
    monkeypatch.setattr(engine, "preload_skills", lambda *a: {})
    reply = {"choices": [{"message": {"content": "synthetic done"}, "finish_reason": "stop"}]}
    flags = ["--oneshot"] if oneshot else []
    harness.run("-m", GPT, "--profile", "legacy", "-t", "SYNTHETIC-SECRET", *flags,
                replies=[reply if oneshot else _finish_reply("native")])
    completion = harness.transcript_path.with_suffix(".artifacts") / "completion.json"
    meta = json.loads(completion.read_text(encoding="utf-8"))["context_policy"]
    assert (meta["window_tokens"], meta["reply_tokens"], meta["input_limit_tokens"]) == (512000, 32000, 454400)
    assert meta["provider_profile"]["window_provenance"] == "operating_profile"
    assert "SYNTHETIC-SECRET" not in json.dumps(meta)
    if not oneshot:
        assert len(captured) == 1
        assert captured[0].snapshot_data()["capsule"]["external"]["context_policy"] == meta


def test_pure_policy_defaults_and_source_dependencies_unchanged(engine):
    cp = engine.capability_module("context_policy")
    assert cp.resolve_policy({}).policy.window_tokens == 1000000
    assert "provider_profile" not in cp.resolve_policy({}).metadata()
    import ast
    from pathlib import Path
    tree = ast.parse(Path(cp.__file__).read_text(encoding="utf-8"))
    imports = {node.module.split(".")[0] for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)}
    imports.update(alias.name.split(".")[0] for node in ast.walk(tree)
                   if isinstance(node, ast.Import) for alias in node.names)
    assert imports <= {"__future__", "dataclasses", "fractions"}

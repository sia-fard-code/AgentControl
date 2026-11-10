"""Offline source-engine model context integration; synthetic data and tmp HOME only."""
import json
from pathlib import Path

import pytest

from test_engine_reasoning import engine, harness  # noqa: F401
from test_engine_turn_budget import GPT, _call, _end, _prose, _run


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch, engine):
    monkeypatch.syspath_prepend(str(Path(engine.__file__).parent))
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.delenv("_HARNESS_APPROVED_WORKER", raising=False)


@pytest.mark.parametrize("flags,code", [
    (["--context-window-tokens", "0"], "invalid_window_tokens"),
    (["--context-reply-tokens", "secret"], "invalid_reply_tokens"),
    (["--context-safety-tokens", "-1"], "invalid_safety_tokens"),
    (["--compact-fraction", "nan"], "invalid_compact_fraction"),
    (["--context-policy", "legacy", "--no-auto-compact"], "context_options_require_model_v1"),
    (["--oneshot", "--auto-compact"], "compaction_unavailable_in_oneshot"),
])
def test_context_flags_fail_cold(harness, engine, tmp_path, monkeypatch, capsys, flags, code):
    def forbidden():
        pytest.fail("cold validation must precede config")
    monkeypatch.setattr(engine, "load_cfg", forbidden)
    assert _run(harness, *flags, replies=[]) == 2
    err = capsys.readouterr().err
    assert f"error: context policy refused: {code}" in err
    assert "secret" not in err
    assert not (tmp_path / "index.jsonl").exists()
    assert not harness.payloads


@pytest.mark.parametrize("frozen", ["manifest", "approved", "recovery"])
def test_frozen_context_flags_rejected_before_authority_load(
        harness, engine, monkeypatch, capsys, frozen):
    flags = {"manifest": ["--supervisor-manifest", "missing"],
             "approved": [], "recovery": ["--recovery-contract", "missing"]}[frozen]
    if frozen == "approved":
        monkeypatch.setenv("_HARNESS_APPROVED_WORKER", "1")
    monkeypatch.setattr(engine, "load_cfg", lambda: pytest.fail("config read"))
    assert _run(harness, *flags, "--context-policy", "model-v1", replies=[]) == 2
    assert "context_overrides_frozen" in capsys.readouterr().err


@pytest.mark.parametrize("oneshot", [False, True])
@pytest.mark.parametrize("profile", ["legacy", "resilient-v1"])
def test_large_plain_input_default_model_policy(harness, tmp_path, oneshot, profile):
    flags = ["--oneshot"] if oneshot else []
    task = "synthetic " * 80000
    reply = _prose("done") if oneshot else _call("finish", {"summary": "done"})
    code = _run(harness, "-m", GPT, "--profile", profile, *flags,
                "-t", task, replies=[reply])
    assert code == 0
    assert len(harness.payloads) == 1
    assert len(json.dumps(harness.payloads[0])) > 600000


def test_oneshot_scope_cleared_after_success_second_run_and_failure(harness, engine):
    current = lambda: engine._CAP_MODULES["context_engine"].current()  # noqa: E731
    for _ in range(2):
        assert _run(harness, "-m", GPT, "--oneshot", "-t", "synthetic " * 80000,
                    replies=[_prose("done")]) == 0
        assert current() is None
    with pytest.raises(IndexError):  # fake gateway has no reply: exception exits the scope
        _run(harness, "-m", GPT, "--oneshot", "-t", "x", replies=[])
    assert current() is None
    assert len(harness.payloads) >= 2


def test_explicit_legacy_keeps_byte_guard(harness, tmp_path):
    assert _run(harness, "--context-policy", "legacy", "-m", GPT,
                "-t", "x" * 300000, replies=[]) == 3
    assert not harness.payloads
    assert _end(tmp_path)["stop_reason"] == "context_budget"


def test_new_mode_projection_fault_is_not_context_overflow(harness, engine, monkeypatch, tmp_path):
    runtime = engine.capability_module("context_runtime").ContextRuntime
    original = runtime.admit_result

    def admit(self, *args, **kwargs):
        result = original(self, *args, **kwargs)
        self.guard.stop("projection_error")
        return result

    monkeypatch.setattr(runtime, "admit_result", admit)
    assert _run(harness, "-m", GPT, replies=[_call("list_dir", {"path": "."}),
                                            _prose("synthetic partial")]) == 3
    assert _end(tmp_path)["stop_reason"] == "context_admission"


def test_new_mode_http_413_is_transport(engine, monkeypatch):
    import io
    import urllib.error

    ce = engine.capability_module("context_engine")
    cp = engine.capability_module("context_policy")
    ct = engine.capability_module("context_tokens")
    policy = cp.Policy(10000, 100, 100)
    ctx = ce.Scope(policy, ct.ContextMeter(policy, model=GPT, route="chat"))

    def reject(*args, **kwargs):
        raise urllib.error.HTTPError("http://synthetic", 413, "synthetic", {},
                                     io.BytesIO(b'{"detail":"synthetic private body"}'))

    monkeypatch.setattr(engine.urllib.request, "urlopen", reject)
    with ce.scope(ctx):
        reply = engine.chat("http://synthetic", "fake", {"model": GPT, "messages": []})
    assert reply == {"_transient": "HTTP 413"}
    assert engine.classify_failure(reply, "native", 0) == "transport"
    assert ce.current() is None

"""Synthetic request/accounting boundary regressions; no providers or live state."""
import json
from types import SimpleNamespace

import pytest

from test_engine_reasoning import engine, harness  # noqa: F401

KNOWN = {"prompt_tokens": 7, "completion_tokens": 3}
ZERO = {"prompt_tokens": 0, "completion_tokens": 0}
RAW = "SYNTHETIC_MALFORMED_BODY"


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.delenv("_HARNESS_APPROVED_WORKER", raising=False)


def answer(usage=ZERO):
    return {"choices": [{"message": {"content": "synthetic report"}}], "usage": usage}


def boundary(engine, monkeypatch, *, attempts=1, phase=None, failures=()):
    original, dispatched = engine.call_model, []
    pending = iter(failures)

    def stream(*args, **kwargs):
        dispatched.append(1)
        result = next(pending)
        if isinstance(result, BaseException):
            raise result
        return result

    def call(*args, **kwargs):
        args = list(args)
        route = SimpleNamespace(route="chat") if args[3] == phase else None
        if len(args) > 7:
            args[7] = route
        else:
            kwargs["streaming"] = route
        kwargs["max_attempts"] = attempts
        return original(*args, **kwargs)

    monkeypatch.setattr(engine, "stream_turn", stream)
    monkeypatch.setattr(engine, "call_model", call)
    return dispatched


def run_incomplete(harness, replies):
    with pytest.raises(SystemExit) as exc:
        harness.run("--context-policy", "model-v1", "-m", "gpt-6-sol",
                    "--max-iters", "2", replies=replies)
    assert exc.value.code == 3


def check(harness, tmp_path, dispatched, expected, *, reason="max_iters"):
    path = harness.transcript_path.with_suffix(".usage.jsonl")
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    assert len(harness.payloads) + len(dispatched) == len(rows) == len(expected)
    assert [(r["phase"], r["sent"], r["recv"]) for r in rows] == expected
    index = (tmp_path / "index.jsonl").read_text()
    end = [r for r in map(json.loads, index.splitlines()) if r.get("event") == "end"][-1]
    for name, pos in (("sent", 1), ("recv", 2)):
        values = [r[pos] for r in expected]
        assert end[name] == (None if None in values else sum(values))
    if reason == "transport":
        # "transport" is not a STOP_REASONS code, so the end row omits the field by contract;
        # run_incomplete already proved exit code 3 (the _transport_rejected path).
        assert end["status"] == "failed" and "stop_reason" not in end
    else:
        assert end["status"] in ("failed", "incomplete") and end["stop_reason"] == reason
    assert end["tool_calls"] == 0
    assert RAW not in index
    assert RAW not in path.read_text()
    anomalies = harness.transcript_path.with_suffix(".anomalies.jsonl")
    if anomalies.exists():
        assert RAW not in anomalies.read_text()


@pytest.mark.parametrize("choices", [
    [None], [RAW], [{"message": None}], [{"message": {"content": 7}}],
], ids=["null-choice", "string-choice", "null-message", "numeric-content"])
def test_returned_malformed_main(harness, engine, monkeypatch, tmp_path, choices):
    dispatched = boundary(engine, monkeypatch)
    run_incomplete(harness, [{"choices": choices, "usage": KNOWN}, answer()])
    check(harness, tmp_path, dispatched, [("loop", 7, 3), ("wrapup", 0, 0)])


@pytest.mark.parametrize("phase", ["native", "oneshot"], ids=["main", "wrapup"])
@pytest.mark.parametrize("usage", [KNOWN, None, ZERO], ids=["known", "unknown", "zero"])
def test_terminal_stream_is_current_attempt(harness, engine, monkeypatch, tmp_path,
                                            phase, usage):
    dispatched = boundary(engine, monkeypatch, phase=phase, failures=[
        engine.StreamError("transport", "synthetic disconnect", 1, usage)])
    run_incomplete(harness, [answer()])
    measured = (None, None) if usage is None else tuple(usage.values())
    expected = [("loop", 0, 0), ("wrapup", *measured)] if phase == "oneshot" else [
        ("loop", *measured), ("wrapup", 0, 0)]
    check(harness, tmp_path, dispatched, expected)


@pytest.mark.parametrize("prior", [KNOWN, None, ZERO], ids=["known", "unknown", "zero"])
@pytest.mark.parametrize("reject_usage", [None, KNOWN], ids=["unmeasured-reject", "known-reject"])
def test_context_rejection_preserves_dispatched_attempts(
        harness, engine, monkeypatch, tmp_path, prior, reject_usage):
    dispatched = boundary(engine, monkeypatch, attempts=2, phase="native", failures=[
        engine.StreamError("idle", "synthetic idle", 1, prior),
        engine.StreamError("http", "HTTP 400 context_length_exceeded", 0, reject_usage)])
    run_incomplete(harness, [])
    first = (None, None) if prior is None else tuple(prior.values())
    last = (None, None) if reject_usage is None else tuple(reject_usage.values())
    check(harness, tmp_path, dispatched, [("loop", *first), ("loop", *last)],
          reason="context_budget")
    assert harness.payloads == []


@pytest.mark.parametrize("prior", [KNOWN, None, ZERO], ids=["known", "unknown", "zero"])
@pytest.mark.parametrize("ending", ["success", "fatal", "fallback"])
def test_retry_boundary_counts_each_request(harness, engine, monkeypatch, tmp_path,
                                            prior, ending):
    terminal = (engine.StreamError("http", "HTTP 413", 0) if ending == "fatal" else
                engine.StreamError("http", "HTTP 400 synthetic rejection", 0)
                if ending == "fallback" else answer())
    dispatched = boundary(engine, monkeypatch, attempts=2, phase="native", failures=[
        engine.StreamError("idle", "synthetic idle", 1, prior), terminal])
    run_incomplete(harness, [answer(), answer()] if ending == "fallback" else [answer()])
    measured = (None, None) if prior is None else tuple(prior.values())
    expected = [("loop", *measured)]
    if ending == "fatal":
        expected += [("loop", None, None)]
    else:
        if ending == "fallback":
            expected += [("loop", None, None)]
        expected += [("loop", 0, 0), ("wrapup", 0, 0)]
    check(harness, tmp_path, dispatched, expected,
          reason="transport" if ending == "fatal" else "max_iters")


@pytest.mark.parametrize("refuse_at", [1, 2], ids=["initial", "after-retry"])
def test_predispatch_refusal_has_no_phantom_row(harness, engine, monkeypatch, tmp_path,
                                               refuse_at):
    dispatched = boundary(engine, monkeypatch, attempts=2, phase="native", failures=[
        engine.StreamError("idle", "synthetic idle", 1, KNOWN)])
    original, checks = engine.context_preflight, []

    def preflight(payload):
        checks.append(1)
        if len(checks) == refuse_at:
            raise engine.ContextBudgetError("synthetic predispatch refusal")
        return original(payload)

    monkeypatch.setattr(engine, "context_preflight", preflight)
    run_incomplete(harness, [answer()])
    expected = ([("loop", 7, 3)] if refuse_at == 2 else []) + [("wrapup", 0, 0)]
    # An unmeasured refusal does not establish overflow, so the engine reports admission.
    check(harness, tmp_path, dispatched, expected, reason="context_admission")
    assert len(dispatched) == refuse_at - 1

"""Responses terminal semantics and paid usage (design 8.5, plan Task 8A).
Synthetic text only; the harness fakes the stream, the gateway and the clock."""
from __future__ import annotations

import json

import pytest

from test_engine_reasoning import engine, harness  # noqa: F401

USAGE = {"input_tokens": 100, "output_tokens": 20, "input_tokens_details": {"cached_tokens": 0}}
TEXT = "synthetic partial answer"


def _incomplete(usage=USAGE, reason="max_output_tokens"):
    return {"type": "response.incomplete",
            "response": {"status": "incomplete", "incomplete_details": {"reason": reason},
                         "usage": usage}}


def _records(path):
    return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]


def _run_failed(harness, scripts, effort="high"):
    with pytest.raises(SystemExit) as ei:
        harness.run("--oneshot", "-m", "gpt-6.1-sol", "-e", effort, replies=[],
                    stream_replies=scripts)
    return ei.value.code


def _assert_partial(harness, tmp_path, capsys, *, sent=100, recv=20, rows=1, text=TEXT):
    end = _records(tmp_path / "index.jsonl")[-1]
    assert end["status"] == "failed" and end.get("stop_reason") == "response_incomplete"
    assert (end["sent"], end["recv"]) == (sent, recv)
    usage_rows = _records(harness.transcript_path.with_suffix(".usage.jsonl"))
    assert len(usage_rows) == rows and usage_rows[-1]["sent"] == 100 \
        and usage_rows[-1]["recv"] == 20
    assert harness.payloads == []          # no Chat fallback
    out, err = capsys.readouterr()
    assert "response_incomplete" in err and "max_output_tokens" in err
    transcript = harness.transcript_path.read_text(encoding="utf-8")
    if text:
        assert transcript.count(text) == 1 and out.count(text) == 1
    # The eagerly streamed heading is historical data; terminal metadata plus an
    # appended partial marker, not rewriting past bytes, determines completeness.
    assert "[PARTIAL - response_incomplete]" in transcript


def test_incomplete_with_text_is_partial_and_charged_once(harness, tmp_path, capsys):
    script = [{"type": "response.created"},
              {"type": "response.output_text.delta", "delta": TEXT}, _incomplete()]
    assert _run_failed(harness, [script]) == 1
    assert len(harness.stream_calls) == 1
    _assert_partial(harness, tmp_path, capsys)


def test_reasoning_only_incomplete_still_charges_and_never_retries(harness, tmp_path, capsys):
    script = [{"type": "response.created"},
              {"type": "response.output_item.done", "item": {"type": "reasoning", "summary": []}},
              _incomplete()]
    assert _run_failed(harness, [script, script]) == 1
    assert len(harness.stream_calls) == 1
    _assert_partial(harness, tmp_path, capsys, text=None)


def test_incomplete_as_first_event_is_not_a_transport_cut(harness, tmp_path, capsys):
    assert _run_failed(harness, [[_incomplete()], [_incomplete()]], effort="max") == 1
    assert len(harness.stream_calls) == 1
    _assert_partial(harness, tmp_path, capsys, text=None)


def test_repeated_terminal_snapshots_charge_one_generation(harness, tmp_path, capsys):
    script = [{"type": "response.output_text.delta", "delta": TEXT}, _incomplete(), _incomplete()]
    assert _run_failed(harness, [script]) == 1
    _assert_partial(harness, tmp_path, capsys)


def test_earlier_attempt_charge_kept_without_reset(engine, harness, tmp_path, capsys):
    idle = engine.StreamError("idle", "no event for 300s", 1)
    idle.usage = {"input_tokens": 100, "output_tokens": 20}
    second = [{"type": "response.output_text.delta", "delta": TEXT}, _incomplete()]
    assert _run_failed(harness, [[idle], second], effort="max") == 1
    assert len(harness.stream_calls) == 2
    _assert_partial(harness, tmp_path, capsys, sent=200, recv=40, rows=2)


@pytest.mark.parametrize("kind", ["response.failed", "error"])
@pytest.mark.parametrize("raw_usage", [False, True])
def test_failed_terminal_usage_is_charged(harness, tmp_path, kind, raw_usage):
    ev = {"type": kind, "response": {"status": "failed", "usage": USAGE}}
    if raw_usage:
        ev["usage"] = ev["response"].pop("usage")
    assert _run_failed(harness, [[{"type": "response.created"}, ev]]) == 1
    assert len(harness.stream_calls) == 1 and harness.payloads == []
    end = _records(tmp_path / "index.jsonl")[-1]
    assert end["status"] == "failed" and (end["sent"], end["recv"]) == (100, 20)
    assert "stop_reason" not in end
    assert len(_records(harness.transcript_path.with_suffix(".usage.jsonl"))) == 1


@pytest.mark.parametrize("prior_idle, first_event", [(False, False), (True, False),
                                                        (False, True)])
def test_native_incomplete_never_dispatches_complete_arguments(
        engine, harness, tmp_path, monkeypatch, capsys, prior_idle, first_event):
    def forbidden_dispatch(*args, **kwargs):
        pytest.fail("an incomplete response must not dispatch tools")

    monkeypatch.setattr(engine.Runner, "dispatch", forbidden_dispatch)
    script = [{"type": "response.output_item.done", "item": {
        "type": "function_call", "call_id": "synthetic-call", "name": "write_file",
        "arguments": json.dumps({"path": "must-not-exist.txt", "content": "synthetic"})}},
        _incomplete()]
    if first_event:
        script = [_incomplete()]
    idle = engine.StreamError("idle", "no event for 300s", 1, usage=USAGE)
    scripts = [[idle], script] if prior_idle else [script]
    with pytest.raises(SystemExit) as caught:
        harness.run("-m", "gpt-6.1-sol", "--tool-mode", "native", replies=[],
                    stream_replies=scripts)
    assert caught.value.code == 1
    attempts = len(scripts)
    assert len(harness.stream_calls) == attempts
    assert not (tmp_path / "must-not-exist.txt").exists()
    _assert_partial(harness, tmp_path, capsys, text=None, rows=attempts,
                    sent=100 * attempts, recv=20 * attempts)


def test_native_partial_saves_only_previously_accepted_edit(harness, tmp_path, capsys):
    def write(path):
        return {"type": "response.output_item.done", "item": {
            "type": "function_call", "call_id": path, "name": "write_file",
            "arguments": json.dumps({"path": path, "content": "synthetic edit"})}}
    patch = tmp_path / "accepted.patch"
    scripts = [[write("early.txt"), {"type": "response.completed", "response": {"usage": USAGE}}],
               [write("late.txt"), _incomplete()]]
    with pytest.raises(SystemExit) as caught:
        harness.run("-m", "gpt-6.1-sol", "--tool-mode", "native", "--patch", str(patch),
                    replies=[], stream_replies=scripts)
    assert caught.value.code == 1
    assert len(harness.stream_calls) == 2 and harness.payloads == []
    assert patch.exists()
    assert "early.txt" in patch.read_text() and "late.txt" not in patch.read_text()
    assert not (tmp_path / "early.txt").exists() and not (tmp_path / "late.txt").exists()
    completion = json.loads((harness.transcript_path.with_suffix(".artifacts") / "completion.json").read_text())
    assert completion["patch_binding"] is not None
    _assert_partial(harness, tmp_path, capsys, text=None, rows=2, sent=200, recv=40)


@pytest.mark.parametrize("oneshot", [False, True])
def test_context_terminal_preserves_paid_attempts(engine, harness, tmp_path, oneshot):
    paid = dict(USAGE, note="synthetic private usage")
    idle = engine.StreamError("idle", "no event for 300s", 1, usage=paid)
    terminal = {"type": "response.failed", "response": {
        "error": {"code": "context_length_exceeded"}, "usage": paid}}
    with pytest.raises(SystemExit) as caught:
        harness.run(*(["--oneshot"] if oneshot else []), "-m", "gpt-6.1-sol", "-e", "max",
                    replies=[], stream_replies=[[idle], [terminal, terminal]])
    assert caught.value.code in (1, 3)
    assert len(harness.stream_calls) == 2 and harness.payloads == []
    end = _records(tmp_path / "index.jsonl")[-1]
    assert end["status"] == "failed" and end["stop_reason"] == "context_budget"
    assert (end["sent"], end["recv"]) == (200, 40)
    rows = _records(harness.transcript_path.with_suffix(".usage.jsonl"))
    assert len(rows) == 2
    assert [(row["sent"], row["recv"]) for row in rows] == [(100, 20), (100, 20)]
    assert "context_length_exceeded" not in json.dumps(rows)
    assert "synthetic private usage" not in json.dumps(rows)
    anomaly = harness.transcript_path.with_suffix(".anomalies.jsonl")
    assert "synthetic private usage" not in anomaly.read_text(encoding="utf-8")


@pytest.mark.parametrize("oneshot", [False, True])
@pytest.mark.parametrize("response", ["synthetic malformed", ["synthetic malformed"], 7,
    *[{"incomplete_details": detail} for detail in (["synthetic malformed"], "synthetic malformed", 7)]])
@pytest.mark.parametrize("top_usage", [False, True])
def test_malformed_terminal_is_known_partial(engine, harness, tmp_path, capsys, monkeypatch,
                                             response, oneshot, top_usage):
    monkeypatch.setattr(engine.Runner, "dispatch", lambda *a, **k: pytest.fail("partial dispatch"))
    terminal = {"type": "response.incomplete", "response": response}
    if top_usage or not isinstance(response, dict):
        terminal["usage"] = USAGE
    else:
        terminal["response"] = dict(response, usage=USAGE)
    script = [{"type": "response.output_text.delta", "delta": TEXT}, terminal]
    with pytest.raises(SystemExit) as caught:
        harness.run(*(["--oneshot"] if oneshot else []), "-m", "gpt-6.1-sol", replies=[],
                    stream_replies=[script])
    assert caught.value.code == 1 and len(harness.stream_calls) == 1
    out, err = capsys.readouterr()
    transcript = harness.transcript_path.read_text(encoding="utf-8")
    assert transcript.count(TEXT) == 1 and out.count(TEXT) == int(oneshot)
    assert "response_incomplete (unknown)" in err and "synthetic malformed" not in err + transcript
    assert "[PARTIAL - response_incomplete]" in transcript
    end = _records(tmp_path / "index.jsonl")[-1]
    assert end["stop_reason"] == "response_incomplete" and end["status"] == "failed"
    assert (end["sent"], end["recv"]) == (100, 20)
    assert len(_records(harness.transcript_path.with_suffix(".usage.jsonl"))) == 1
    assert harness.payloads == []


def test_missing_terminal_stays_stream_incomplete(harness, tmp_path, capsys):
    script = [{"type": "response.output_text.delta", "delta": TEXT}]
    assert _run_failed(harness, [script]) == 1
    end = _records(tmp_path / "index.jsonl")[-1]
    assert end["status"] == "failed" and "stop_reason" not in end
    assert end["sent"] is None and end["recv"] is None
    assert "response_incomplete" not in capsys.readouterr().err


def test_partial_never_seeks_to_rewrite_streamed_history(engine, harness, tmp_path, monkeypatch, capsys):
    original = engine.Transcript
    class AppendOnlyFile:
        def __init__(self, fh): self._fh = fh
        def __getattr__(self, key): return getattr(self._fh, key)
        def seek(self, *args, **kwargs): pytest.fail("original transcript cannot be rewritten")
    class GuardedTranscript(original):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.fh = AppendOnlyFile(self.fh)
    monkeypatch.setattr(engine, "Transcript", GuardedTranscript)
    assert _run_failed(harness, [[{"type": "response.output_text.delta", "delta": TEXT}, _incomplete()]]) == 1
    _assert_partial(harness, tmp_path, capsys)


@pytest.mark.parametrize("provider", [False, True])
def test_nonstream_oneshot_context_rejection_preserves_prior_paid_reply(
        engine, harness, tmp_path, monkeypatch, capsys, provider):
    paid = dict(USAGE, note="synthetic private usage")
    empty = {"choices": [{"message": {"role": "assistant", "content": ""}, "finish_reason": "stop"}],
             "usage": paid}
    rejection = engine.ContextBudgetError("provider context" if provider else "request", provider=provider)
    scripted = iter([empty, rejection])
    def chat_attempt(url, key, payload, turn):
        harness.payloads.append(json.loads(json.dumps(payload)))
        item = next(scripted)
        if isinstance(item, BaseException): raise item
        return item
    monkeypatch.setattr(engine, "chat_with_model_retry", chat_attempt)
    with pytest.raises(SystemExit) as caught:
        harness.run("--oneshot", "--no-stream", "-m", "gpt-6.1-sol", "-e", "high",
                    replies=[], stream_replies=[])
    assert caught.value.code == 3 and len(harness.payloads) == 2 and harness.stream_calls == []
    end = _records(tmp_path / "index.jsonl")[-1]
    assert end["status"] == "failed" and end["stop_reason"] == "context_budget"
    assert (end["sent"], end["recv"]) == (100, 20)
    usage = _records(harness.transcript_path.with_suffix(".usage.jsonl"))
    assert len(usage) == 1 and (usage[0]["sent"], usage[0]["recv"]) == (100, 20)
    assert "synthetic private usage" not in json.dumps(usage)
    if not provider:
        assert capsys.readouterr().out.startswith("[PARTIAL - context_budget]")


def test_anomaly_boundary_never_copies_request_reply_or_unknown_usage(engine, tmp_path):
    path = tmp_path / "metadata-only.anomaly.jsonl"
    payload = {"model": "synthetic", "messages": [{"role": "user", "content": "SYNTHETIC-REQUEST"}]}
    response = {"id": "chatcmpl-synthetic", "choices": [{"finish_reason": "stop", "message": {
        "content": "SYNTHETIC-RESPONSE", "tool_calls": [{"id": "synthetic", "function": {
            "name": "write_file", "arguments": "SYNTHETIC-ARGUMENTS"}}]}}],
        "usage": dict(USAGE, note="SYNTHETIC-USAGE-SECRET")}
    engine.log_anomaly(str(path), 1, 1, "native", payload, response, cause="prose_only", elapsed=1)
    text = path.read_text(encoding="utf-8")
    assert all(marker not in text for marker in
               ("SYNTHETIC-REQUEST", "SYNTHETIC-RESPONSE", "SYNTHETIC-ARGUMENTS", "SYNTHETIC-USAGE-SECRET"))
    record = json.loads(text)
    assert record["turn"] == 1 and record["cause"] == "prose_only" and record["request_bytes"] > 0


@pytest.mark.parametrize("route,retry", [("chat", "cut"), ("chat", "idle"), ("responses", "idle")])
@pytest.mark.parametrize("known", [False, True])
def test_stream_retry_preflight_preserves_only_prior_attempt_usage(
        engine, harness, tmp_path, capsys, route, retry, known):
    model = "anthropic_tool_call.claude-x" if route == "chat" else "gpt-6-sol"
    effort = "medium" if retry == "cut" else "max"
    base = {"model": model, "stream": True}
    base.update(messages=[{"role": "user", "content": ""}] if route == "chat" else [],
                **({"input": ""} if route == "responses" else {}))
    if route == "responses":
        base.pop("messages")
    base = engine.apply_effort(base, model, effort)
    if route == "responses":
        base["reasoning"]["summary"] = "auto"
    guard = engine.capability_module("context_guard")
    task = "x" * (guard.REQUEST_LIMIT - engine._payload_bytes(base) - 1)
    paid = USAGE if known else None
    if retry == "cut":
        script = [121, {"choices": [{"delta": {"content": "synthetic cut"}}], "usage": paid}, {"_done": True}]
    else:
        script = [engine.StreamError("idle", "synthetic idle", 1, usage=paid)]
    with pytest.raises(SystemExit) as caught:
        harness.run("--oneshot", "--context-policy", "legacy", "--text-only", "-m", model,
                    "-e", effort, "-t", task, replies=[], stream_replies=[script])
    assert caught.value.code == 3 and len(harness.stream_calls) == 1 and harness.payloads == []
    rows = _records(harness.transcript_path.with_suffix(".usage.jsonl"))
    assert len(rows) == 1
    expected = (100, 20) if known else (None, None)
    assert (rows[0]["sent"], rows[0]["recv"]) == expected
    end = _records(tmp_path / "index.jsonl")[-1]
    assert end["status"] == "failed" and end["stop_reason"] == "context_budget"
    assert (end["sent"], end["recv"]) == expected
    assert capsys.readouterr().out.startswith("[PARTIAL - context_budget]")


@pytest.mark.parametrize("route", ["chat", "responses"])
@pytest.mark.parametrize("provider", [False, True])
@pytest.mark.parametrize("current_usage", [False, True])
def test_stream_context_conversion_preserves_history_without_phantom_usage(
        engine, harness, tmp_path, route, provider, current_usage):
    model = "anthropic_tool_call.claude-x" if route == "chat" else "gpt-6-sol"
    idle = engine.StreamError("idle", "synthetic paid idle", 1, usage=USAGE)
    rejection = engine.ContextBudgetError("provider context" if provider else "request", provider=provider)
    if current_usage:
        rejection.usage = USAGE
    with pytest.raises(SystemExit) as caught:
        harness.run("--oneshot", "-m", model, "-e", "high", replies=[],
                    stream_replies=[[idle], [rejection]])
    assert caught.value.code == 3 and len(harness.stream_calls) == 2 and harness.payloads == []
    rows = _records(harness.transcript_path.with_suffix(".usage.jsonl"))
    count = 2 if current_usage else 1
    assert len(rows) == count and [(row["sent"], row["recv"]) for row in rows] == [(100, 20)] * count
    end = _records(tmp_path / "index.jsonl")[-1]
    assert end["stop_reason"] == "context_budget" and (end["sent"], end["recv"]) == (100 * count, 20 * count)

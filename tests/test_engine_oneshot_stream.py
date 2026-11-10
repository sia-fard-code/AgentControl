"""Streaming one-shots (live-thinking design). Synthetic text only; the harness
fakes the stream, the gateway and the clock."""
from __future__ import annotations

import io
import json
import socket
import urllib.error

import pytest

from test_engine_reasoning import RESPONSES_USAGE, engine, harness  # noqa: F401


def _gpt_events(answer, *, summary=None, usage=RESPONSES_USAGE, summary_in_item=None,
                completed=True):
    evs = [{"type": "response.created"}, {"type": "response.in_progress"}]
    if summary is not None:
        evs.append({"type": "response.reasoning_summary_part.added"})
        evs += [{"type": "response.reasoning_summary_text.delta", "delta": p} for p in summary]
        evs.append({"type": "response.reasoning_summary_part.done"})
    item = {"type": "reasoning", "summary": []}
    if summary_in_item is not None:
        item["summary"] = [{"type": "summary_text", "text": summary_in_item}]
    evs.append({"type": "response.output_item.done", "item": item})
    evs += [{"type": "response.output_text.delta", "delta": p} for p in answer]
    if completed:
        evs.append({"type": "response.completed", "response": {"usage": usage}})
    return evs


def _answer(content):
    return {"choices": [{"message": {"role": "assistant", "content": content},
                         "finish_reason": "stop"}]}


def _records(path):
    return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]


# GPT's 512k operating window gives 32000 reply tokens; legacy stays byte-exact.
@pytest.mark.parametrize("policy, cap", [
    ((), {"max_output_tokens": 32000}),
    (("--context-policy", "legacy"), {}),
], ids=["default", "legacy"])
def test_gpt_oneshot_streams_through_responses(harness, capsys, policy, cap):
    harness.run("--oneshot", *policy, "-m", "gpt-6.1-sol", "-e", "max", replies=[],
                stream_replies=[_gpt_events(["Hello", " world"])])
    assert harness.payloads == []
    path, payload = harness.stream_calls[0]
    assert path == "/openai/responses"
    assert payload == {"model": "gpt-6.1-sol", "input": "do the thing", "stream": True,
                       "reasoning": {"effort": "max", "summary": "auto"}, **cap}
    out, err = capsys.readouterr()
    assert out == "Hello world"
    assert err.endswith("\n[sent 100 tok to gpt-6.1-sol; returned 20 tok]\n")


def test_reasoning_reaches_the_transcript_before_the_answer(engine, harness, monkeypatch):
    from agent_control.narration import parse_narration

    monkeypatch.setattr(engine, "LIVE_FLUSH_SECONDS", 0.0)
    seen = []
    script = _gpt_events(["Answer."], summary=["**Plan**\n\nFirst ", "check X."])
    first_answer = next(i for i, e in enumerate(script)
                        if e["type"] == "response.output_text.delta")
    script.insert(first_answer,
                  lambda: seen.append(harness.transcript_path.read_text(encoding="utf-8")))
    text = harness.run("--oneshot", "-m", "gpt-6.1-sol", replies=[], stream_replies=[script])
    assert "<think>\n**Plan**\n\nFirst check X." in seen[0]
    assert "## Final report" not in seen[0]
    events = parse_narration(text).events
    assert [e.kind for e in events if e.kind in ("narration", "final")] == ["narration", "final"]
    assert next(e for e in events if e.kind == "final").data["text"] == "Answer."


def test_summary_from_the_reasoning_item_when_no_deltas(harness):
    text = harness.run("--oneshot", "-m", "gpt-6-sol", replies=[],
                       stream_replies=[_gpt_events(["ok"], summary_in_item="Checked the cases.")])
    assert "<think>\nChecked the cases.\n</think>" in text


def test_no_reasoning_section_without_summary(harness):
    text = harness.run("--oneshot", "-m", "gpt-6-sol", replies=[],
                       stream_replies=[_gpt_events(["ok"])])
    assert "### [1] thinking" not in text
    assert "## Final report\n\nok\n" in text


def test_http_error_before_first_event_falls_back_to_non_streaming(engine, harness, capsys):
    err = engine.StreamError("http", "HTTP 400: unsupported", 0)
    harness.run("--oneshot", "-m", "gpt-6-sol", replies=[_answer("fallback answer")],
                stream_replies=[err])
    assert len(harness.payloads) == 1 and harness.payloads[0]["stream"] is False
    assert capsys.readouterr().out == "fallback answer"


def test_idle_at_max_retries_once_at_xhigh(engine, harness, capsys):
    idle = engine.StreamError("idle", "no event for 300s", 0)
    text = harness.run("--oneshot", "-m", "gpt-6.1-sol", "-e", "max", replies=[],
                       stream_replies=[[{"type": "response.created"}, idle],
                                       _gpt_events(["late answer"])])
    assert [p["reasoning"]["effort"] for _, p in harness.stream_calls] == ["max", "xhigh"]
    assert "_[ 1] no reply within 300 s at max; retrying at xhigh_" in text
    assert capsys.readouterr().out == "late answer"


def test_second_idle_fails_the_run(engine, harness, tmp_path):
    idle = engine.StreamError("idle", "no event for 300s", 1)
    with pytest.raises(SystemExit) as ei:
        harness.run("--oneshot", "-m", "gpt-6-sol", "-e", "high", replies=[],
                    stream_replies=[[idle], [idle]])
    assert ei.value.code == 1
    assert len(harness.stream_calls) == 2
    assert _records(tmp_path / "index.jsonl")[-1]["status"] == "failed"


def test_stream_without_completed_event_fails_and_keeps_the_text(harness, tmp_path):
    # Review focus 5: a dropped connection must not pass as a complete answer.
    with pytest.raises(SystemExit) as ei:
        harness.run("--oneshot", "-m", "gpt-6-sol", replies=[],
                    stream_replies=[_gpt_events(["half an ans"], completed=False)])
    assert ei.value.code == 1
    assert "half an ans" in harness.transcript_path.read_text(encoding="utf-8")
    assert _records(tmp_path / "index.jsonl")[-1]["status"] == "failed"


class _FakeStream:
    def __init__(self, lines):
        self.lines = lines

    def __iter__(self):
        return iter(self.lines)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_stream_events_skips_bad_lines(engine, monkeypatch):
    # Review focus 4: keep-alives, event lines, broken JSON and undecodable bytes.
    lines = [b": keep-alive\n", b"event: response.created\n", b'data: {"type": "a"}\n',
             b"data: {not json\n", b"data: \xff\xfe\n", b"\n", b'data: {"type": "b"}\n',
             b"data: [DONE]\n", b'data: {"type": "after-done"}\n']
    seen = {}

    def fake_urlopen(req, timeout=None):
        seen["timeout"] = timeout
        seen["auth"] = req.get_header("Authorization")
        return _FakeStream(lines)

    monkeypatch.setattr(engine.urllib.request, "urlopen", fake_urlopen)
    got = list(engine.stream_events("http://gw", "k", "/openai/responses", {"stream": True}))
    assert got == [{"type": "a"}, {"type": "b"}, engine.STREAM_DONE]
    assert seen == {"timeout": engine.STREAM_IDLE_TIMEOUT, "auth": "Bearer k"}


def test_stream_events_http_error_is_a_stream_error_without_the_key(engine, monkeypatch):
    def boom(req, timeout=None):
        raise urllib.error.HTTPError("http://gw/x", 400, "bad", None,
                                     io.BytesIO(b'{"detail": "bad key sekret-key"}'))

    monkeypatch.setattr(engine.urllib.request, "urlopen", boom)
    with pytest.raises(engine.StreamError) as ei:
        list(engine.stream_events("http://gw", "sekret-key", "/openai/responses", {}))
    assert ei.value.kind == "http" and ei.value.events_seen == 0
    assert "sekret-key" not in str(ei.value)


def test_stream_events_read_timeout_is_idle(engine, monkeypatch):
    class Slow(_FakeStream):
        def __iter__(self):
            yield b'data: {"type": "a"}\n'
            raise socket.timeout("timed out")

    monkeypatch.setattr(engine.urllib.request, "urlopen", lambda req, timeout=None: Slow([]))
    gen = engine.stream_events("http://gw", "k", "/p", {})
    assert next(gen) == {"type": "a"}
    with pytest.raises(engine.StreamError) as ei:
        next(gen)
    assert ei.value.kind == "idle" and ei.value.events_seen == 1


@pytest.mark.parametrize("status", [401, 403])
def test_stream_auth_failure_is_terminal_and_sanitized(engine, monkeypatch, status):
    def boom(req, timeout=None):
        raise urllib.error.HTTPError("http://gw/x", status, "bad", None,
                                     io.BytesIO(b'{"detail": "bad key sekret-key", '
                                                b'"body": "synthetic response body"}'))

    monkeypatch.setattr(engine.urllib.request, "urlopen", boom)
    with pytest.raises(SystemExit) as ei:
        list(engine.stream_events("http://gw", "sekret-key", "/p", {}))
    assert f"HTTP {status}" in str(ei.value)
    assert "[redacted]" in str(ei.value)
    assert "sekret-key" not in str(ei.value)
    assert "synthetic response body" not in str(ei.value)


def test_stream_transport_failure_does_not_echo_the_reason(engine, monkeypatch):
    def boom(req, timeout=None):
        raise urllib.error.URLError("synthetic reason with sekret-key")

    monkeypatch.setattr(engine.urllib.request, "urlopen", boom)
    with pytest.raises(engine.StreamError) as ei:
        list(engine.stream_events("http://gw", "sekret-key", "/p", {}))
    assert ei.value.kind == "transport" and ei.value.events_seen == 0
    assert "sekret-key" not in str(ei.value)
    assert "synthetic reason" not in str(ei.value)


CLAUDE = "anthropic_tool_call.claude-x"


def _chat_events(pieces, *, usage=None, done=True, clock=None):
    evs = [{"object": "chat.completion.chunk",
            "choices": [{"index": 0, "delta": {"content": p}}]} for p in pieces]
    last = {"object": "chat.completion.chunk",
            "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}
    if usage is not None:
        last["usage"] = usage
    evs.append(last)
    if clock is not None:
        evs.append(clock)                 # a number advances the fake clock
    if done:
        evs.append({"_done": True})
    return evs


@pytest.mark.parametrize("policy, cap", [
    ((), {"max_completion_tokens": 32768}),
    (("--context-policy", "legacy"), {}),
], ids=["default", "legacy"])
def test_claude_oneshot_streams_through_chat(harness, capsys, policy, cap):
    harness.run("--oneshot", *policy, "-m", CLAUDE, "-e", "high", replies=[],
                stream_replies=[_chat_events(["The ", "answer."], clock=8)])
    path, payload = harness.stream_calls[0]
    assert path == "/api/chat/completions"
    assert payload == {"model": CLAUDE, "stream": True, "reasoning_effort": "high",
                       "messages": [{"role": "user", "content": "do the thing"}], **cap}
    assert capsys.readouterr().out == "The answer."


def test_claude_stream_cut_retries_once_with_shorter_answer(engine, harness, capsys):
    text = harness.run("--oneshot", "-m", CLAUDE, replies=[],
                       stream_replies=[_chat_events(["long ans"], clock=121),
                                       _chat_events(["short answer"], clock=20)])
    assert len(harness.stream_calls) == 2
    assert harness.stream_calls[1][1]["messages"][0]["content"] == (
        "do the thing" + engine.ONESHOT_SHORTER)
    assert ("_[ 1] reply cut by the gateway's 2-minute limit after 121 s; "
            "asking for a shorter answer_") in text
    assert "long ans\n\n" + engine.ONESHOT_CUT_MARK in text
    assert capsys.readouterr().out == "short answer"


def test_second_cut_keeps_the_answer_marked(engine, harness, capsys, tmp_path):
    harness.run("--oneshot", "-m", CLAUDE, replies=[],
                stream_replies=[_chat_events(["first"], clock=121),
                                _chat_events(["second"], clock=121)])
    assert capsys.readouterr().out == "second\n\n" + engine.ONESHOT_CUT_MARK
    assert _records(tmp_path / "index.jsonl")[-1]["status"] == "complete"


def test_claude_stream_without_done_fails(harness, tmp_path):
    with pytest.raises(SystemExit) as ei:
        harness.run("--oneshot", "-m", CLAUDE, replies=[],
                    stream_replies=[_chat_events(["partial"], done=False)])
    assert ei.value.code == 1
    assert _records(tmp_path / "index.jsonl")[-1]["status"] == "failed"


def test_claude_http_error_falls_back_to_non_streaming(engine, harness, capsys):
    harness.run("--oneshot", "-m", CLAUDE, replies=[_answer("plain")],
                stream_replies=[engine.StreamError("http", "HTTP 400", 0)])
    assert harness.payloads[0]["stream"] is False
    assert capsys.readouterr().out == "plain"


def test_claude_think_tags_become_reasoning(harness):
    text = harness.run("--oneshot", "-m", CLAUDE, replies=[],
                       stream_replies=[_chat_events(["<think>pla", "n</think>The answer."])])
    assert "<think>\nplan\n</think>" in text
    assert "## Final report\n\nThe answer.\n" in text


def _merged(pieces):
    out = []
    for kind, text in pieces:
        if out and out[-1][0] == kind:
            out[-1] = (kind, out[-1][1] + text)
        else:
            out.append((kind, text))
    return out


@pytest.mark.parametrize("chunks, expected", [
    (["plain answer"], [("answer", "plain answer")]),
    (["<think>plan</think>answer"], [("reasoning", "plan"), ("answer", "answer")]),
    # Review focus 3: tags split across chunks never leak into the answer.
    (["<thi", "nk>plan</th", "ink>answer"], [("reasoning", "plan"), ("answer", "answer")]),
    (["a <", "b"], [("answer", "a <b")]),
    (["<think>still thinking"], [("reasoning", "still thinking")]),
])
def test_think_splitter(engine, chunks, expected):
    splitter = engine.ThinkSplitter()
    pieces = []
    for chunk in chunks:
        pieces += splitter.feed(chunk)
    pieces += splitter.flush()
    assert _merged(pieces) == expected


def test_gpt_stream_with_no_events_falls_back(harness, capsys):
    harness.run("--oneshot", "-m", "gpt-6-sol", replies=[_answer("fallback answer")],
                stream_replies=[[]])
    assert len(harness.payloads) == 1 and harness.payloads[0]["stream"] is False
    assert capsys.readouterr().out == "fallback answer"


def test_claude_stream_with_no_events_falls_back(harness, capsys):
    harness.run("--oneshot", "-m", CLAUDE, replies=[_answer("fallback answer")],
                stream_replies=[[]])
    assert harness.payloads[0]["stream"] is False
    assert capsys.readouterr().out == "fallback answer"


def test_claude_quick_bare_error_stream_falls_back(harness, capsys):
    harness.run("--oneshot", "-m", CLAUDE, replies=[_answer("real answer")],
                stream_replies=[_chat_events(["Error: "], clock=5)])
    assert harness.payloads[0]["stream"] is False
    assert capsys.readouterr().out == "real answer"


def test_gpt_stream_with_bare_error_answer_falls_back(harness, capsys):
    harness.run("--oneshot", "-m", "gpt-6-sol", replies=[_answer("real answer")],
                stream_replies=[_gpt_events(["Error:"])])
    assert harness.payloads[0]["stream"] is False
    assert capsys.readouterr().out == "real answer"


def test_two_empty_cut_streams_fail_the_run(harness, tmp_path):
    with pytest.raises(SystemExit) as ei:
        harness.run("--oneshot", "-m", CLAUDE, replies=[],
                    stream_replies=[_chat_events([], clock=121), _chat_events([], clock=121)])
    assert ei.value.code == 1
    assert _records(tmp_path / "index.jsonl")[-1]["status"] == "failed"


def test_stream_events_keepalives_do_not_reset_the_idle_limit(engine, monkeypatch):
    t = {"now": 0.0}

    def tick():
        t["now"] += 100.0
        return t["now"]

    monkeypatch.setattr(engine, "_now", tick)
    lines = [b": keep-alive\n"] * 10
    monkeypatch.setattr(engine.urllib.request, "urlopen",
                        lambda req, timeout=None: _FakeStream(lines))
    with pytest.raises(engine.StreamError) as ei:
        list(engine.stream_events("http://gw", "k", "/p", {}))
    assert ei.value.kind == "idle" and ei.value.events_seen == 0


def test_streamed_text_is_visible_without_waiting_for_the_next_fragment(harness):
    seen = []
    script = _gpt_events(["first part", " second part"])
    second = [i for i, e in enumerate(script)
              if e["type"] == "response.output_text.delta"][1]
    script.insert(second,
                  lambda: seen.append(harness.transcript_path.read_text(encoding="utf-8")))
    harness.run("--oneshot", "-m", "gpt-6-sol", replies=[], stream_replies=[script])
    assert "## Final report\n\nfirst part" in seen[0]


def test_claude_idle_at_max_retries_once_at_xhigh(engine, harness, capsys):
    idle = engine.StreamError("idle", "no event for 300s", 0)
    text = harness.run("--oneshot", "-m", CLAUDE, "-e", "max", replies=[],
                       stream_replies=[[idle], _chat_events(["late answer"], clock=5)])
    assert [p.get("reasoning_effort") for _, p in harness.stream_calls] == ["max", "xhigh"]
    assert "_[ 1] no reply within 300 s at max; retrying at xhigh_" in text
    assert capsys.readouterr().out == "late answer"


def test_claude_second_idle_fails_the_run(engine, harness, tmp_path):
    idle = engine.StreamError("idle", "no event for 300s", 1)
    with pytest.raises(SystemExit) as ei:
        harness.run("--oneshot", "-m", CLAUDE, "-e", "high", replies=[],
                    stream_replies=[[idle], [idle]])
    assert ei.value.code == 1
    assert len(harness.stream_calls) == 2


def test_cut_attempt_usage_is_journalled_and_counted(harness, tmp_path, capsys):
    usage = {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120}
    harness.run("--oneshot", "-m", CLAUDE, replies=[],
                stream_replies=[_chat_events(["long"], usage=usage, clock=121),
                                _chat_events(["short"], usage=usage, clock=5)])
    rows = _records(harness.transcript_path.with_suffix(".usage.jsonl"))
    assert len(rows) == 2 and all(r["sent"] == 100 and r["recv"] == 20 for r in rows)
    end = _records(tmp_path / "index.jsonl")[-1]
    assert end["sent"] == 200 and end["recv"] == 40
    assert "[sent 200 tok to anthropic_tool_call.claude-x; returned 40 tok]" in capsys.readouterr().err


def test_stream_events_skips_a_line_with_invalid_utf8_inside_json(engine, monkeypatch):
    lines = [b'data: {"type": "a", "delta": "x\xff"}\n', b'data: {"type": "b"}\n']
    monkeypatch.setattr(engine.urllib.request, "urlopen",
                        lambda req, timeout=None: _FakeStream(lines))
    got = list(engine.stream_events("http://gw", "k", "/p", {}))
    assert got == [{"type": "b"}]

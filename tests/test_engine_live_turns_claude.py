"""Live agent turns: Claude through chat streaming (LA §3.1-§3.3)."""
from __future__ import annotations

import json

import pytest

from test_engine_reasoning import engine, harness  # noqa: F401

CLAUDE = "anthropic_tool_call.claude-x"
USAGE = {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120}


def chunk(content=None, tool_calls=None, finish=None):
    delta = {}
    if content is not None:
        delta["content"] = content
    if tool_calls is not None:
        delta["tool_calls"] = tool_calls
    return {"choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}


def claude_turn(name, args, note="I will look.", call_id="toolu_1", secs=0, index=True,
                raw=None):
    raw = json.dumps(args) if raw is None else raw
    first = {"id": call_id, "type": "function", "function": {"name": name, "arguments": ""}}
    if index:
        first["index"] = 0
    events = [chunk("<think>"), chunk("</think>"), chunk("\n\n"), chunk(note),
              chunk(tool_calls=[first])]
    for k in range(0, len(raw), 7):
        piece = {"function": {"arguments": raw[k:k + 7]}}
        if index:
            piece["index"] = 0
        events.append(chunk(tool_calls=[piece]))
    if secs:
        events.append(secs)
    events += [chunk(finish="stop"), {"choices": [], "usage": USAGE}, engine_done()]
    return events


def engine_done():
    return {"_done": True}


def finish_turn():
    return claude_turn("finish", {"summary": "all done"}, note="")


def test_note_streams_and_no_empty_thinking_section(harness):
    seen = []
    turn = claude_turn("list_dir", {"path": "."})
    turn.insert(4, lambda: seen.append(harness.transcript_path.read_text(encoding="utf-8")))
    text = harness.run("-m", CLAUDE, replies=[], stream_replies=[turn, finish_turn()])
    assert "### [1] thinking\n\nI will look." in seen[0]
    assert text.count("I will look.") == 1
    assert "<think>" not in text
    assert "### [2] thinking" not in text          # the finish turn had no note
    assert harness.stream_calls[0][0] == "/api/chat/completions"
    assert harness.stream_calls[0][1]["stream"] is True


def test_tool_calls_are_assembled_from_pieces(harness):
    text = harness.run("-m", CLAUDE, replies=[],
                       stream_replies=[claude_turn("list_dir", {"path": "."}), finish_turn()])
    assert '### [1] list_dir({"path": "."})' in text
    second = harness.stream_calls[1][1]["messages"]
    assert any(m.get("tool_call_id") == "toolu_1" for m in second)


def test_missing_index_is_treated_as_zero(harness):
    text = harness.run("-m", CLAUDE, replies=[],
                       stream_replies=[claude_turn("list_dir", {"path": "."}, index=False),
                                       finish_turn()])
    assert '### [1] list_dir({"path": "."})' in text


def test_cut_stream_gets_the_smaller_step_correction(harness, engine):
    cut = claude_turn("write_file", {"path": "a.py", "content": "x" * 200}, secs=121,
                      raw='{"path": "a.py", "content": "xxxxxxxx')
    text = harness.run("-m", CLAUDE, "--write", replies=[], stream_replies=[cut, finish_turn()])
    assert "reply cut by the gateway's 2-minute limit after 121 s; asking for a smaller step" in text
    assert harness.stream_calls[1][1]["messages"][-1]["content"] == engine.CUT_CORRECTION
    assert "_[ 1] composing write_file a.py ..._\n> xxxxxxxx" in text


def test_slow_but_complete_reply_is_a_normal_step(harness):
    slow = claude_turn("list_dir", {"path": "."}, secs=121)
    text = harness.run("-m", CLAUDE, replies=[], stream_replies=[slow, finish_turn()])
    assert "reply cut" not in text and '### [1] list_dir({"path": "."})' in text


def test_quick_complete_reply_is_a_normal_step(harness):
    quick = claude_turn("list_dir", {"path": "."}, secs=60)
    text = harness.run("-m", CLAUDE, replies=[], stream_replies=[quick, finish_turn()])
    assert "reply cut" not in text


def prompt_finish_turn():
    return [chunk('```tool\n{"name": "finish", "args": {"summary": "ok"}}\n```'),
            chunk(finish="stop"), engine_done()]


@pytest.mark.parametrize("flags, fin", [
    (["--tool-mode", "native"], finish_turn),
    (["--tool-mode", "prompt", "--allow-prompt-tools"], prompt_finish_turn),
])
def test_prose_only_is_nudged_without_writing_the_note_twice(harness, flags, fin):
    prose = [chunk("I think the answer is 4."), chunk(finish="stop"), engine_done()]
    text = harness.run("-m", CLAUDE, *flags, replies=[], stream_replies=[prose, fin()])
    assert "reply had no tool call; asking for one" in text
    assert text.count("I think the answer is 4.") == 1
    assert len(harness.stream_calls) == 2
    msgs = harness.stream_calls[1][1]["messages"]
    assert msgs[-2] == {"role": "assistant", "content": "I think the answer is 4."}
    assert msgs[-1]["role"] == "user"
    assert msgs[-1]["content"].startswith("You called no tool")
    assert "call finish" in msgs[-1]["content"]


def test_stream_without_done_is_retried(harness):
    broken = claude_turn("list_dir", {"path": "."})[:-1]          # no [DONE]
    harness.run("-m", CLAUDE, replies=[], stream_replies=[broken, finish_turn()])
    assert len(harness.stream_calls) == 2


def test_prompt_mode_streams_the_note_and_holds_the_tool_block(harness):
    block = '```tool\n{"name": "list_dir", "args": {"path": "."}}\n```'
    turn = [chunk("Listing it.\n"), chunk(block[:5]), chunk(block[5:]),
            chunk(finish="stop"), engine_done()]
    fin = [chunk('```tool\n{"name": "finish", "args": {"summary": "ok"}}\n```'),
           chunk(finish="stop"), engine_done()]
    text = harness.run("-m", CLAUDE, "--tool-mode", "prompt", "--allow-prompt-tools", replies=[],
                       stream_replies=[turn, fin])
    assert "### [1] thinking\n\nListing it." in text
    assert '"name": "list_dir"' not in text.split('### [1] list_dir(')[0].split("## Task")[-1]
    assert text.count("Listing it.") == 1


def test_complete_tool_action(engine):
    ok = {"choices": [{"message": {"tool_calls": [
        {"function": {"name": "x", "arguments": '{"a": 1}'}}]}}]}
    bad = {"choices": [{"message": {"tool_calls": [
        {"function": {"name": "x", "arguments": '{"a": '}}]}}]}
    none = {"choices": [{"message": {"content": "hi"}}]}
    assert engine.complete_tool_action(ok, "native")
    assert not engine.complete_tool_action(bad, "native")
    assert not engine.complete_tool_action(none, "native")
    blk = {"choices": [{"message": {"content": '```tool\n{"name": "x", "args": {}}\n```'}}]}
    assert engine.complete_tool_action(blk, "prompt")
    assert not engine.complete_tool_action(none, "prompt")


def test_claude_routes(engine):
    assert engine.agent_stream_route(CLAUDE, "native", False) == "chat"
    assert engine.agent_stream_route(CLAUDE, "prompt", False) == "chat"
    assert engine.agent_stream_route(CLAUDE, "native", True) is None

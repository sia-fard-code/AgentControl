import re

from test_engine_reasoning import RESPONSES_USAGE, engine, harness
from test_engine_live_turns_gpt import gpt_turn, finish_turn as gpt_finish
from test_engine_live_turns_claude import (
    chunk, engine_done, finish_turn as claude_finish, CLAUDE, USAGE,
)


def test_gpt_tool_calls_follow_output_index(harness):
    items = [
        {"type": "function_call", "id": "fc_a", "call_id": "call_1",
         "name": "list_dir", "arguments": '{"path": "."}'},
        {"type": "function_call", "id": "fc_b", "call_id": "call_2",
         "name": "list_dir", "arguments": '{"path": ".claude"}'},
    ]
    script = [
        {"type": "response.output_item.added", "output_index": i, "item": item}
        for i, item in enumerate(items)
    ]
    script += [
        {"type": "response.output_item.done", "output_index": i, "item": items[i]}
        for i in (1, 0)
    ]
    script += [{"type": "response.completed", "response": {
        "output": items, "usage": RESPONSES_USAGE,
    }}, {"_done": True}]
    harness.run("-m", "gpt-6-sol", replies=[],
                stream_replies=[script, gpt_finish()])
    outputs = [item["call_id"] for item in harness.stream_calls[1][1]["input"]
               if item.get("type") == "function_call_output"]
    assert outputs == ["call_1", "call_2"]


def tool_chunk(index, *, call_id=None, name=None, arguments=None):
    function = {}
    if name is not None:
        function["name"] = name
    if arguments is not None:
        function["arguments"] = arguments
    call = {"index": index, "function": function}
    if call_id is not None:
        call["id"] = call_id
    return {"choices": [{"delta": {"tool_calls": [call]}}]}


def native_end():
    return [chunk(finish="stop"), {"choices": [], "usage": USAGE}, {"_done": True}]


def test_claude_interleaved_previews_keep_call_ownership(harness):
    script = [
        tool_chunk(0, call_id="t0", name="write_file", arguments=""),
        tool_chunk(1, call_id="t1", name="write_file", arguments=""),
        tool_chunk(0, arguments='{"path": "a.py", "content": "AA'),
        tool_chunk(1, arguments='{"path": "b.py", "content": "BB'),
        tool_chunk(0, arguments='CC"}'),
        tool_chunk(1, arguments='DD"}'),
    ] + native_end()
    transcript = harness.run("--model", CLAUDE, "--write",
                             replies=[], stream_replies=[script, claude_finish()])
    previews = {"a.py": [], "b.py": []}
    active = None
    for line in transcript.splitlines():
        match = re.fullmatch(r"_\[ 1\] composing write_file (.+) \.\.\._", line)
        if match:
            active = match.group(1)
        elif line.startswith("> ") and active is not None:
            previews[active].append(line[2:])
        else:
            active = None
    assert "".join(previews["a.py"]) == "AACC"
    assert "".join(previews["b.py"]) == "BBDD"


def test_claude_split_function_names(harness):
    script = [
        tool_chunk(0, call_id="t0", name="list_", arguments=""),
        tool_chunk(0, name="dir"),
        tool_chunk(0, arguments='{"path": '),
        tool_chunk(0, arguments='"."}'),
    ] + native_end()
    transcript = harness.run("--model", CLAUDE, replies=[],
                             stream_replies=[script, claude_finish()])
    assert '### [1] list_dir({"path": "."})' in transcript


def test_claude_split_write_name_registers_preview_lazily(harness):
    script = [
        tool_chunk(0, call_id="t0", name="write_", arguments=""),
        tool_chunk(0, name="file"),
        tool_chunk(0, arguments='{"path": "n.md", "content": "hi"}'),
    ] + native_end()
    transcript = harness.run("--model", CLAUDE, "--write",
                             replies=[], stream_replies=[script, claude_finish()])
    assert "_[ 1] composing write_file n.md ..._" in transcript
    assert "> hi" in transcript


def test_claude_broken_stream_flushes_held_text(harness, engine):
    script = [chunk("I am checking ``"),
              engine.StreamError("transport", "connection reset", 1)]
    finish = [chunk('```tool\n{"name": "finish", "args": {"summary": "ok"}}\n```'),
              chunk(finish="stop"), {"_done": True}]
    transcript = harness.run("--model", CLAUDE, "--tool-mode", "prompt", "--allow-prompt-tools",
                             replies=[], stream_replies=[script, finish])
    assert "I am checking ``" in transcript

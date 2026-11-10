"""Live agent turns: pure building blocks (LA §3.1, §3.2)."""
from __future__ import annotations

import json

import pytest

from test_engine_reasoning import engine  # noqa: F401


def _tr(engine, tmp_path):
    return engine.Transcript(str(tmp_path / "t.md"), "# t")


def _text(tmp_path):
    return (tmp_path / "t.md").read_text(encoding="utf-8")


def test_responses_input_converts_the_whole_history(engine):
    msgs = [
        {"role": "system", "content": "S"},
        {"role": "user", "content": "U"},
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "call_1", "type": "function",
             "function": {"name": "list_dir", "arguments": '{"path": "."}'}}]},
        {"role": "tool", "tool_call_id": "call_1", "content": "a.py"},
        {"role": "assistant", "content": engine.CUT_PLACEHOLDER},
        {"role": "user", "content": engine.CUT_CORRECTION},
    ]
    assert engine.responses_input(msgs) == [
        {"role": "system", "content": "S"},
        {"role": "user", "content": "U"},
        {"type": "function_call", "call_id": "call_1", "name": "list_dir",
         "arguments": '{"path": "."}'},
        {"type": "function_call_output", "call_id": "call_1", "output": "a.py"},
        {"role": "assistant", "content": engine.CUT_PLACEHOLDER},
        {"role": "user", "content": engine.CUT_CORRECTION},
    ]


def test_responses_tools_flattens_function_tools(engine):
    out = engine.responses_tools(engine.native_tools())
    names = [t["name"] for t in out]
    assert "write_file" in names and "finish" in names
    assert all(t["type"] == "function" and "parameters" in t and "function" not in t
               for t in out)


def _feed_all(fs, raw, step):
    return "".join(fs.feed(raw[k:k + step]) for k in range(0, len(raw), step))


@pytest.mark.parametrize("step", [1, 2, 3, 5, 64])
def test_arg_field_stream_decodes_split_escapes(engine, step):
    value = 'line "one"\n\\path\ttab é 😀 end'
    raw = json.dumps({"path": "src/a.py", "content": value})
    fs = engine.ArgFieldStream("content")
    assert _feed_all(fs, raw, step) == value
    assert fs.done and fs.started and fs.path == "src/a.py"


def test_arg_field_stream_path_after_the_field_is_found_late(engine):
    fs = engine.ArgFieldStream("content")
    first = fs.feed('{"content": "abc')
    assert first == "abc" and fs.started and fs.path is None
    fs.feed('", "path": "x.py"}')
    assert fs.done and fs.path == "x.py"


def test_arg_field_stream_lone_surrogate_does_not_break_writing(engine, tmp_path):
    fs = engine.ArgFieldStream("content")
    text = fs.feed('{"content": "a\\ud83db"}')
    tr = _tr(engine, tmp_path)
    live = engine.LiveTurn(tr, 1)
    live.preview_start("write_file", None)
    live.preview_text(text)          # must not raise UnicodeEncodeError
    live.close()
    assert "> a" in _text(tmp_path)


@pytest.mark.parametrize("step", [1, 64])
def test_arg_field_stream_lone_surrogate_at_field_end(engine, tmp_path, step):
    fs = engine.ArgFieldStream("content")
    # The string closes before the rest of the JSON object arrives.
    text = _feed_all(fs, '{"content": "a\\ud83d"', step)
    assert fs.started and fs.done
    assert text.startswith("a") and text.encode("utf-8")
    assert fs.feed(", \"path\": \"x.py\"}") == "" and fs.path == "x.py"
    tr = _tr(engine, tmp_path)
    live = engine.LiveTurn(tr, 1)
    live.preview_start("write_file", None)
    live.preview_text(text)
    live.close()
    assert "> a" in _text(tmp_path)


def test_fence_hold_stops_at_the_tool_fence_even_when_split(engine):
    h = engine.FenceHold()
    out = h.feed("I will list it.\n``") + h.feed("`to") + h.feed('ol\n{"name": "x"}')
    assert out == "I will list it.\n" and h.flush() == ""


def test_live_turn_reasoning_then_note(engine, tmp_path):
    tr = _tr(engine, tmp_path)
    live = engine.LiveTurn(tr, 3)
    live.reasoning("Checking ")
    live.reasoning("the file.")
    assert "### [3] thinking\n\n<think>\nChecking the file." in _text(tmp_path)
    live.note("\n\n")                 # whitespace alone opens nothing
    live.note("I will read it.")
    live.close()
    assert _text(tmp_path).endswith(
        "### [3] thinking\n\n<think>\nChecking the file.\n</think>\n\nI will read it.\n")
    assert live.wrote_note


def test_live_turn_whitespace_only_writes_nothing(engine, tmp_path):
    tr = _tr(engine, tmp_path)
    live = engine.LiveTurn(tr, 2)
    live.note("\n \n")
    live.close()
    assert "thinking" not in _text(tmp_path) and not live.wrote_note


def test_live_turn_preview_prefixes_every_line(engine, tmp_path):
    tr = _tr(engine, tmp_path)
    live = engine.LiveTurn(tr, 3)
    live.note("Writing it.")
    live.preview_start("write_file", "src/app.py")
    live.preview_text("### [9] thinking\n\n---\r\n## Fin")
    live.preview_text("al report\n[m: 3 tool calls]")
    live.close()
    tail = _text(tmp_path).split("Writing it.\n", 1)[1]
    assert tail == ("\n_[ 3] composing write_file src/app.py ..._\n"
                    "> ### [9] thinking\n>\n> ---\n> ## Final report\n> [m: 3 tool calls]\n")
    raw = (tmp_path / "t.md").read_bytes().replace(b"\r\n", b"\n")
    assert b"\r" not in raw  # native line endings must not hide a stray CR


def test_live_turn_preview_without_path_and_newlines_in_path(engine, tmp_path):
    tr = _tr(engine, tmp_path)
    live = engine.LiveTurn(tr, 12)
    live.preview_start("finish", None)
    live.preview_text("done")
    live.close()
    live2 = engine.LiveTurn(tr, 13)
    live2.preview_start("write_file", "a\nb.py")
    live2.close()
    text = _text(tmp_path)
    assert "_[12] composing finish ..._\n> done\n" in text
    assert "_[13] composing write_file a b.py ..._" in text


def test_live_turn_note_after_preview_opens_a_new_section(engine, tmp_path):
    tr = _tr(engine, tmp_path)
    live = engine.LiveTurn(tr, 4)
    live.preview_start("write_file", "a.py")
    live.preview_text("x")
    live.note("and more")
    live.close()
    assert "> x\n\n### [4] thinking\n\nand more\n" in _text(tmp_path)

"""Live agent turns: GPT through /openai/responses (LA §3.1, §3.3)."""
from __future__ import annotations

import json

import pytest

from test_engine_reasoning import RESPONSES_USAGE, engine, harness  # noqa: F401


def gpt_turn(name, args, call_id="call_1", summary=("Checking ", "the file."), note="",
             usage=RESPONSES_USAGE, extra_calls=()):
    events = [{"type": "response.created"}]
    for d in summary:
        events.append({"type": "response.reasoning_summary_text.delta", "delta": d})
    if summary:
        events.append({"type": "response.reasoning_summary_part.done"})
    if note:
        events.append({"type": "response.output_text.delta", "delta": note})
    for nm, ar, cid in ((name, args, call_id), *extra_calls):
        raw = json.dumps(ar)
        item = {"type": "function_call", "id": "fc_" + cid, "call_id": cid,
                "name": nm, "arguments": ""}
        events.append({"type": "response.output_item.added", "item": item})
        for k in range(0, len(raw), 7):
            events.append({"type": "response.function_call_arguments.delta",
                           "item_id": item["id"], "delta": raw[k:k + 7]})
        events.append({"type": "response.output_item.done",
                       "item": {**item, "arguments": raw}})
    events.append({"type": "response.completed", "response": {"usage": usage}})
    return events


def finish_turn(call_id="call_9"):
    return gpt_turn("finish", {"summary": "all done"}, call_id=call_id, summary=())


def _records(path):
    return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]


def test_reasoning_is_in_the_transcript_before_the_turn_ends(harness):
    seen = []
    turn = gpt_turn("list_dir", {"path": "."})
    turn.insert(4, lambda: seen.append(harness.transcript_path.read_text(encoding="utf-8")))
    text = harness.run("-m", "gpt-6-sol", "-e", "high", replies=[],
                       stream_replies=[turn, finish_turn()])
    assert "### [1] thinking\n\n<think>\nChecking the file." in seen[0]
    assert "### [1] list_dir" not in seen[0]
    assert "<think>\nChecking the file.\n\n\n</think>" in text or \
           "<think>\nChecking the file.\n\n</think>" in text
    assert "### [1] list_dir(" in text


def test_request_shape_and_history_round_trip(harness):
    harness.run("-m", "gpt-6-sol", "-e", "high", replies=[],
                stream_replies=[gpt_turn("list_dir", {"path": "."}), finish_turn()])
    path, first = harness.stream_calls[0]
    assert path == "/openai/responses"
    assert first["stream"] is True and first["tool_choice"] == "auto"
    assert first["reasoning"] == {"effort": "high", "summary": "auto"}
    assert first["input"][0]["role"] == "system"
    assert all(t["type"] == "function" and "name" in t for t in first["tools"])
    assert "messages" not in first
    second = harness.stream_calls[1][1]["input"]
    assert {"type": "function_call", "call_id": "call_1", "name": "list_dir",
            "arguments": json.dumps({"path": "."})} in second
    outputs = [i for i in second if i.get("type") == "function_call_output"]
    assert outputs and outputs[0]["call_id"] == "call_1"


def test_usage_row_comes_from_response_completed(harness):
    harness.run("-m", "gpt-6-sol", replies=[],
                stream_replies=[gpt_turn("list_dir", {"path": "."}), finish_turn()])
    rows = _records(harness.transcript_path.with_suffix(".usage.jsonl"))
    assert rows[0]["sent"] == 100 and rows[0]["recv"] == 20


def test_note_is_streamed_once(harness):
    text = harness.run("-m", "gpt-6-sol", replies=[],
                       stream_replies=[gpt_turn("list_dir", {"path": "."}, summary=(),
                                                note="Listing the root."),
                                       finish_turn()])
    assert text.count("Listing the root.") == 1


def test_two_function_calls_in_one_turn(harness):
    turn = gpt_turn("list_dir", {"path": "."},
                    extra_calls=(("list_dir", {"path": ".claude"}, "call_2"),))
    text = harness.run("-m", "gpt-6-sol", replies=[], stream_replies=[turn, finish_turn()])
    second = harness.stream_calls[1][1]["input"]
    ids = [i["call_id"] for i in second if i.get("type") == "function_call_output"]
    assert ids == ["call_1", "call_2"]
    assert text.count("### [1] list_dir(") == 2


def test_write_file_text_is_previewed_while_it_streams(harness, tmp_path):
    seen = []
    turn = gpt_turn("write_file", {"path": "notes.md", "content": "# Title\n\nbody"}, summary=())
    turn.insert(-2, lambda: seen.append(harness.transcript_path.read_text(encoding="utf-8")))
    harness.run("-m", "gpt-6-sol", "--write", replies=[], stream_replies=[turn, finish_turn()])
    assert "_[ 1] composing write_file notes.md ..._\n> # Title\n>\n> body" in seen[0]


def test_finish_summary_is_previewed(harness):
    text = harness.run("-m", "gpt-6-sol", replies=[], stream_replies=[finish_turn()])
    assert "_[ 1] composing finish ..._\n> all done" in text


def test_rejected_stream_falls_back_for_the_rest_of_the_run(harness, engine, capsys):
    err = engine.StreamError("http", "HTTP 400: unsupported parameter", 0)
    text = harness.run("-m", "gpt-6-sol",
                       replies=[{"choices": [{"message": {"content": "", "tool_calls": [
                           {"id": "c1", "type": "function", "function": {
                               "name": "list_dir", "arguments": '{"path": "."}'}}]}}]},
                                {"choices": [{"message": {"content": "", "tool_calls": [
                                    {"id": "c2", "type": "function", "function": {
                                        "name": "finish", "arguments": '{"summary": "ok"}'}}]}}]}],
                       stream_replies=[err])
    assert text.count("**streaming unavailable (HTTP 400); continuing without live view**") == 1
    assert len(harness.stream_calls) == 1 and len(harness.payloads) == 2


def test_model_not_found_is_refreshed_and_retried_streaming(harness, engine):
    stale = engine.StreamError("http", "HTTP 400: Model not found", 0)
    harness.run("-m", "gpt-6-sol", replies=[], stream_replies=[stale, finish_turn()])
    assert len(harness.stream_calls) == 2 and harness.payloads == []
    assert "streaming unavailable" not in harness.transcript_path.read_text(encoding="utf-8")


@pytest.mark.parametrize("err", [
    ("http", "HTTP 502: bad gateway", 0),
    ("transport", "cannot reach gateway: ConnectionResetError", 0),
    ("transport", "the stream ended before the response completed", 3),
])
def test_transport_failures_retry_streaming(harness, engine, err):
    harness.run("-m", "gpt-6-sol", replies=[],
                stream_replies=[engine.StreamError(*err), finish_turn()])
    assert len(harness.stream_calls) == 2 and harness.payloads == []


def test_mid_stream_failure_closes_open_reasoning(harness, engine):
    broken = gpt_turn("list_dir", {"path": "."})[:3] + [
        engine.StreamError("transport", "connection reset", 3)]
    text = harness.run("-m", "gpt-6-sol", replies=[], stream_replies=[broken, finish_turn()])
    first = text.split("attempt 2/4", 1)[0]
    assert first.count("<think>") == first.count("</think>") == 1


def test_idle_stream_takes_the_timeout_path_at_xhigh_after_max(harness, engine):
    idle = engine.StreamError("idle", "no event for 600s", 0)
    harness.run("-m", "gpt-6.1-sol", "-e", "max", replies=[],
                stream_replies=[idle, finish_turn()])
    assert harness.stream_calls[1][1]["reasoning"]["effort"] == "xhigh"


def test_no_stream_flag_makes_no_streaming_request(harness):
    from test_engine_reasoning import _finish_reply
    harness.run("-m", "gpt-6-sol", replies=[_finish_reply("native")])   # adds --no-stream
    assert harness.stream_calls == []


def test_no_stream_also_turns_off_oneshot_streaming(harness):
    harness.run("--oneshot", "-m", "gpt-6-sol",
                replies=[{"choices": [{"message": {"content": "an answer"}}]}])
    assert harness.stream_calls == []


def test_other_models_and_prompt_mode_gpt_stay_non_streamed(engine):
    assert engine.agent_stream_route("gpt-6-sol", "native", False) == "responses"
    assert engine.agent_stream_route("gpt-6-sol", "prompt", False) is None
    assert engine.agent_stream_route("qwen3.6:latest", "prompt", False) is None
    assert engine.agent_stream_route("gpt-6-sol", "native", True) is None

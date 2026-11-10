from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from agent_control import reader_claude_code as rcc

SENTINEL = "SENTINEL-TEXT-DO-NOT-STORE"
NOW = datetime(2026, 10, 2, tzinfo=timezone.utc)
SESSION = "5f0c0000-aaaa-bbbb-cccc-000000000001"
SLUG = "D--Work-Tools-AgentControl"


def usage(**overrides):
    value = {
        "input_tokens": 2,
        "cache_creation_input_tokens": 702,
        "cache_read_input_tokens": 173955,
        "output_tokens": 1930,
        "output_tokens_details": {"thinking_tokens": 1200},
        "server_tool_use": {"web_search_requests": 0, "web_fetch_requests": 0},
        "service_tier": "standard",
        "cache_creation": {"ephemeral_1h_input_tokens": 690, "ephemeral_5m_input_tokens": 12},
        "inference_geo": "not_available",
        "iterations": [
            {"input_tokens": 99, "output_tokens": 99,
             "cache_read_input_tokens": 99, "cache_creation_input_tokens": 99}
        ],
        "speed": "standard",
    }
    value.update(overrides)
    return value


def assistant(msg_id="msg_01abc", request_id="req_011abc", uuid="u1", u=None, **overrides):
    line = {
        "type": "assistant",
        "uuid": uuid,
        "parentUuid": "u0",
        "sessionId": SESSION,
        "timestamp": "2026-10-01T19:39:58.648Z",
        "cwd": "D:\\repos\\AgentControl",
        "isSidechain": False,
        "requestId": request_id,
        "version": "2.1.281",
        "entrypoint": "claude-vscode",
        "message": {
            "id": msg_id,
            "type": "message",
            "role": "assistant",
            "model": "claude-opus-5-5",
            "content": [
                {"type": "thinking", "thinking": SENTINEL},
                {"type": "text", "text": SENTINEL},
                {"type": "tool_use", "id": "toolu_1", "name": "Bash",
                 "input": {"command": SENTINEL}},
            ],
            "stop_reason": "end_turn",
            "usage": usage() if u is None else u,
        },
    }
    if request_id is None:
        del line["requestId"]
    if msg_id is None:
        del line["message"]["id"]
    if uuid is None:
        del line["uuid"]
    line.update(overrides)
    return line


def user_line():
    return {
        "type": "user", "uuid": "u9", "sessionId": SESSION,
        "timestamp": "2026-10-01T19:39:50.000Z",
        "message": {"role": "user", "content": [
            {"type": "text", "text": SENTINEL},
            {"type": "tool_result", "tool_use_id": "toolu_1", "content": SENTINEL},
        ]},
    }


def noise_lines():
    return [
        user_line(),
        {"type": "ai-title", "sessionId": SESSION, "aiTitle": SENTINEL},
        {"type": "last-prompt", "sessionId": SESSION, "lastPrompt": SENTINEL},
        {"type": "attachment", "sessionId": SESSION, "attachment": {"content": SENTINEL}},
        {"type": "queue-operation", "sessionId": SESSION, "content": SENTINEL},
        {"type": "system", "sessionId": SESSION, "content": SENTINEL},
        {"type": "mode", "sessionId": SESSION, "mode": SENTINEL},
        {"type": "file-history-snapshot", "snapshot": {"text": SENTINEL}},
    ]


def dump(lines):
    return "".join(json.dumps(line) + "\n" for line in lines)


def main_file(source: Path, session=SESSION) -> Path:
    path = source / SLUG / f"{session}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def write(path: Path, lines) -> None:
    path.write_text(dump(lines), encoding="utf-8", newline="\n")


def append(path: Path, text: str) -> None:
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(text)


def assert_clean(*objects) -> None:
    for obj in objects:
        assert SENTINEL not in repr(obj)
        assert SENTINEL not in json.dumps(obj, default=repr)


def test_1_single_line_maps_all_fields(tmp_path):
    path = main_file(tmp_path)
    write(path, [assistant()])
    calls, checkpoint, diags = rcc.read(tmp_path, None, now=NOW)
    assert diags == []
    assert calls == [{
        "tool": "claude_code",
        "key": "msg_01abc:req_011abc",
        "session": SESSION,
        "root_session": SESSION,
        "at": "2026-10-01T19:39:58.648000+00:00",
        "model": "claude-opus-5-5",
        "cwd": "D:\\repos\\AgentControl",
        "tokens_sent": 2,
        "tokens_returned": 1930,
        "cache_read": 173955,
        "cache_write": 702,
        "cache_write_5m": 12,
        "reasoning": 1200,
        "sent_includes_cache_read": False,
        "speed": "standard",
    }]
    assert rcc.TOOL == "claude_code"
    assert list(checkpoint["files"]) == [f"{SLUG}/{SESSION}.jsonl"]
    json.dumps(checkpoint)


def test_2_multi_line_call_keeps_last_numbers(tmp_path):
    path = main_file(tmp_path)
    write(path, [
        assistant(u=usage(output_tokens=10)),
        assistant(uuid="u2", u=usage(output_tokens=20)),
        assistant(uuid="u3", u=usage(output_tokens=30)),
    ])
    calls, _, diags = rcc.read(tmp_path, None, now=NOW)
    assert diags == []
    assert len(calls) == 1
    assert calls[0]["tokens_returned"] == 30


def test_3_second_read_reemits_newer_numbers(tmp_path):
    path = main_file(tmp_path)
    write(path, [assistant(u=usage(output_tokens=10))])
    calls1, cp1, _ = rcc.read(tmp_path, None, now=NOW)
    append(path, dump([assistant(uuid="u2", u=usage(output_tokens=50))]))
    calls2, _, diags = rcc.read(tmp_path, cp1, now=NOW)
    assert diags == []
    assert [c["key"] for c in calls2] == [calls1[0]["key"]]
    assert calls2[0]["tokens_returned"] == 50


def test_4_non_call_lines_give_nothing(tmp_path):
    path = main_file(tmp_path)
    no_usage = assistant(msg_id="msg_nousage")
    del no_usage["message"]["usage"]
    synthetic = assistant(msg_id="msg_syn")
    synthetic["message"]["model"] = "<synthetic>"
    write(path, [
        synthetic,
        assistant(msg_id="msg_err", isApiErrorMessage=True),
        assistant(msg_id="msg_user", type="user"),
        no_usage,
        assistant(msg_id="msg_badusage", u="not-an-object"),
        *noise_lines(),
    ])
    calls, checkpoint, diags = rcc.read(tmp_path, None, now=NOW)
    assert calls == []
    assert diags == []
    assert_clean(checkpoint)


def test_5_key_fallbacks(tmp_path):
    path = main_file(tmp_path)
    write(path, [
        assistant(msg_id="msg_a", request_id="req_a", uuid="ua"),
        assistant(msg_id="msg_b", request_id=None, uuid="ub"),
        assistant(msg_id=None, request_id=None, uuid="uc"),
        assistant(msg_id=None, request_id=None, uuid=None),
    ])
    calls, _, diags = rcc.read(tmp_path, None, now=NOW)
    assert [c["key"] for c in calls] == ["msg_a:req_a", "msg_b", "uc"]
    assert len(diags) == 1
    diag = diags[0]
    assert diag.code == "claude_code_skipped_line"
    assert diag.line == 4
    assert diag.source is not None and diag.source.endswith(f"{SESSION}.jsonl")
    assert_clean(diag.message, diag.source)


def test_6_subagent_session_and_root(tmp_path):
    main = main_file(tmp_path)
    write(main, [assistant(msg_id="msg_main", request_id="req_main")])
    sub = tmp_path / SLUG / SESSION / "subagents" / "agent-a1b2.jsonl"
    sub.parent.mkdir(parents=True)
    write(sub, [assistant(msg_id="msg_sub", request_id="req_sub",
                          agentId="a1b2", isSidechain=True)])
    calls, checkpoint, diags = rcc.read(tmp_path, None, now=NOW)
    assert diags == []
    by_key = {c["key"]: c for c in calls}
    assert by_key["msg_sub:req_sub"]["session"] == f"{SESSION}:a1b2"
    assert by_key["msg_sub:req_sub"]["root_session"] == SESSION
    assert by_key["msg_main:req_main"]["session"] == SESSION
    assert sorted(checkpoint["files"]) == [
        f"{SLUG}/{SESSION}.jsonl",
        f"{SLUG}/{SESSION}/subagents/agent-a1b2.jsonl",
    ]


def test_6b_missing_session_id_uses_file_stem(tmp_path):
    path = main_file(tmp_path, session="stem-session")
    line = assistant()
    del line["sessionId"]
    write(path, [line])
    calls, _, _ = rcc.read(tmp_path, None, now=NOW)
    assert calls[0]["session"] == "stem-session"
    assert calls[0]["root_session"] == "stem-session"


def test_7_incremental_reads(tmp_path):
    path = main_file(tmp_path)
    write(path, [assistant(msg_id="msg_1", request_id="req_1")])
    calls, cp, _ = rcc.read(tmp_path, None, now=NOW)
    assert [c["key"] for c in calls] == ["msg_1:req_1"]

    # Unchanged: nothing.
    calls, cp, diags = rcc.read(tmp_path, cp, now=NOW)
    assert calls == [] and diags == []

    # Append yields only the new call.
    append(path, dump([assistant(msg_id="msg_2", request_id="req_2")]))
    calls, cp, _ = rcc.read(tmp_path, cp, now=NOW)
    assert [c["key"] for c in calls] == ["msg_2:req_2"]

    # A partial line waits.
    text = json.dumps(assistant(msg_id="msg_3", request_id="req_3"))
    append(path, text[:40])
    calls, cp, diags = rcc.read(tmp_path, cp, now=NOW)
    assert calls == [] and diags == []
    append(path, text[40:] + "\n")
    calls, cp, _ = rcc.read(tmp_path, cp, now=NOW)
    assert [c["key"] for c in calls] == ["msg_3:req_3"]

    # A replaced file is read again in full with the same keys.
    write(path, [
        assistant(msg_id="msg_1", request_id="req_1", uuid="x"),
        assistant(msg_id="msg_2", request_id="req_2"),
    ])
    calls, cp, _ = rcc.read(tmp_path, cp, now=NOW)
    assert [c["key"] for c in calls] == ["msg_1:req_1", "msg_2:req_2"]

    # A lost checkpoint costs time, not accuracy.
    calls, _, _ = rcc.read(tmp_path, None, now=NOW)
    assert [c["key"] for c in calls] == ["msg_1:req_1", "msg_2:req_2"]


def test_8_null_versus_zero(tmp_path):
    path = main_file(tmp_path)
    u = usage(cache_read_input_tokens=0)
    del u["output_tokens_details"]
    del u["cache_creation"]
    del u["speed"]
    write(path, [assistant(u=u)])
    calls, _, _ = rcc.read(tmp_path, None, now=NOW)
    call = calls[0]
    assert call["cache_read"] == 0
    assert call["reasoning"] is None
    assert call["cache_write_5m"] is None
    assert call["speed"] is None


def test_9_sentinel_never_leaks(tmp_path):
    path = main_file(tmp_path)
    bad_ts = assistant(msg_id="msg_badts", timestamp=SENTINEL)
    bad_tokens = assistant(msg_id="msg_badtok", u=usage(output_tokens=SENTINEL))
    no_ids = assistant(msg_id=None, request_id=None, uuid=None)
    write(path, [*noise_lines(), assistant(), bad_ts, bad_tokens, no_ids])
    append(path, SENTINEL + " not json\n")
    calls, checkpoint, diags = rcc.read(tmp_path, None, now=NOW)
    assert calls  # the good line still came through
    assert diags  # malformed lines were reported
    for diag in diags:
        assert diag.code.startswith("claude_code_")
        assert_clean(diag.code, diag.message, diag.source)
    assert_clean(calls, checkpoint)


def test_10_missing_source(tmp_path):
    prior = {"files": {"a.jsonl": {"offset": 1}}}
    calls, checkpoint, diags = rcc.read(tmp_path / "nope", prior, now=NOW)
    assert calls == []
    assert checkpoint == prior
    assert [d.code for d in diags] == ["claude_code_source_missing"]

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from agent_control import reader_codex

SENT = "SENTINEL-TEXT-DO-NOT-STORE"
NOW = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)
CWD = "D:\\repos\\Projectb"
CWD2 = "D:\\Work\\Other"
THREAD = "11111111-1111-1111-1111-111111111111"
ROOT = "22222222-2222-2222-2222-222222222222"
KEYS = {
    "tool", "key", "session", "root_session", "at", "model", "cwd",
    "tokens_sent", "tokens_returned", "cache_read", "cache_write",
    "cache_write_5m", "reasoning", "sent_includes_cache_read", "speed",
}


def ts(second: int) -> str:
    return f"2026-10-01T10:00:{second:02d}Z"


def iso(second: int) -> str:
    return f"2026-10-01T10:00:{second:02d}+00:00"


def usage(inp, cached, cw, out, reas, total=None):
    return {
        "input_tokens": inp,
        "cached_input_tokens": cached,
        "cache_write_input_tokens": cw,
        "output_tokens": out,
        "reasoning_output_tokens": reas,
        "total_tokens": total if total is not None else inp + out,
    }


def line(kind, payload, second=0):
    return {"timestamp": ts(second), "type": kind, "payload": payload}


def meta(thread=THREAD, session_id=None, parent=None, cwd=CWD, source="vscode"):
    return line("session_meta", {
        "id": thread,
        "session_id": session_id if session_id is not None else thread,
        "parent_thread_id": parent,
        "cwd": cwd,
        "originator": "codex_vscode",
        "source": source,
        "model_provider": "openai",
        "cli_version": "0.1",
        "base_instructions": {"text": SENT},
    })


def turn(turn_id, model="gpt-5-codex", cwd=CWD, second=1):
    return line("turn_context", {
        "turn_id": turn_id, "model": model, "cwd": cwd, "effort": "high",
        "approval_policy": "on-request", "summary": "auto",
    }, second)


def record(turn_id, resp, use, thread=THREAD, second=2):
    return line("token_usage_record", {
        "thread_id": thread, "turn_id": turn_id, "session_id": ROOT,
        "root_turn_id": turn_id, "response_id": resp, "usage": use,
        "turn_token_usage": use, "thread_token_usage": use,
    }, second)


def tc(total, last, second=3):
    return line("event_msg", {
        "type": "token_count",
        "info": {
            "total_token_usage": total,
            "last_token_usage": last,
            "model_context_window": 272000,
        },
        "rate_limits": {"primary": None},
    }, second)


def tc_null(second=3):
    return line("event_msg", {"type": "token_count", "info": None,
                              "rate_limits": {}}, second)


def noise(second=4):
    return [
        line("response_item", {"type": "message", "role": "user",
                               "content": [{"type": "input_text", "text": SENT}]}, second),
        line("event_msg", {"type": "user_message", "message": SENT}, second),
        line("event_msg", {"type": "agent_message", "message": SENT}, second),
        line("compacted", {"message": SENT}, second),
        line("world_state", {"text": SENT}, second),
    ]


def write(path: Path, rows, mode="w"):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open(mode, encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row) + "\n")


def rollout(source: Path, name="rollout-2026-10-01T10-00-00-a.jsonl", archived=False):
    base = "archived_sessions" if archived else "sessions/2026/10/01"
    return source / base / name


def by_key(calls):
    return {call["key"]: call for call in calls}


def test_records_give_one_call_each_and_token_count_ignored(tmp_path):
    path = rollout(tmp_path)
    u1 = usage(32593, 20480, 0, 165, 0)
    u2 = usage(1000, 500, 7, 300, 120)
    write(path, [
        meta(),
        turn("t1", model="gpt-5-codex", cwd=CWD, second=1),
        record("t1", "resp_a", u1, second=2),
        tc(usage(32593, 20480, 0, 165, 0), u1, second=3),
        turn("t2", model="gpt-5", cwd=CWD2, second=5),
        record("t2", "resp_b", u2, second=6),
        tc(usage(33593, 20980, 7, 465, 120), u2, second=7),
    ])
    calls, checkpoint, diags = reader_codex.read(tmp_path, None, now=NOW)
    assert diags == []
    assert len(calls) == 2
    calls = by_key(calls)
    first = calls[f"{THREAD}:resp_a"]
    assert set(first) == KEYS
    assert first == {
        "tool": "codex", "key": f"{THREAD}:resp_a", "session": THREAD,
        "root_session": THREAD, "at": iso(2), "model": "gpt-5-codex", "cwd": CWD,
        "tokens_sent": 32593, "tokens_returned": 165, "cache_read": 20480,
        "cache_write": 0, "cache_write_5m": None, "reasoning": 0,
        "sent_includes_cache_read": True, "speed": None,
    }
    second = calls[f"{THREAD}:resp_b"]
    assert second["model"] == "gpt-5"
    assert second["cwd"] == CWD2
    assert (second["tokens_sent"], second["tokens_returned"], second["cache_read"],
            second["cache_write"], second["reasoning"]) == (1000, 300, 500, 7, 120)
    state = next(iter(checkpoint["files"].values()))
    assert state["has_records"] is True
    json.dumps(checkpoint)


def test_token_count_only_file(tmp_path):
    path = rollout(tmp_path)
    l1 = usage(100, 40, 0, 10, 2)
    l2 = usage(200, 80, 0, 20, 4)
    write(path, [
        meta(),
        turn("t1", model="gpt-5-codex", second=1),
        tc_null(second=2),
        tc(usage(100, 40, 0, 10, 2, total=110), l1, second=3),
        tc(usage(100, 40, 0, 10, 2, total=110), l1, second=4),
        turn("t2", model="gpt-5", cwd=CWD2, second=5),
        tc(usage(300, 120, 0, 30, 6, total=330), l2, second=6),
        tc_null(second=7),
    ])
    calls, checkpoint, diags = reader_codex.read(tmp_path, None, now=NOW)
    assert diags == []
    assert [c["key"] for c in calls] == [f"{THREAD}:tc:110", f"{THREAD}:tc:330"]
    first, second = calls
    assert first["at"] == iso(3)
    assert first["model"] == "gpt-5-codex"
    assert (first["tokens_sent"], first["cache_read"], first["tokens_returned"],
            first["reasoning"]) == (100, 40, 10, 2)
    assert second["model"] == "gpt-5"
    assert second["cwd"] == CWD2
    assert second["tokens_sent"] == 200
    state = next(iter(checkpoint["files"].values()))
    assert state["has_records"] is False
    assert state["last_total"] == 330


def test_switch_to_records_mid_file(tmp_path):
    path = rollout(tmp_path)
    l1 = usage(100, 0, 0, 10, 0)
    write(path, [
        meta(),
        turn("t1", second=1),
        tc(usage(100, 0, 0, 10, 0, total=110), l1, second=2),
        tc(usage(200, 0, 0, 20, 0, total=220), l1, second=3),
    ])
    calls, checkpoint, _ = reader_codex.read(tmp_path, None, now=NOW)
    assert len(calls) == 2
    u = usage(50, 10, 0, 5, 1)
    write(path, [
        turn("t2", model="gpt-5", second=10),
        record("t2", "resp_x", u, second=11),
        tc(usage(250, 10, 0, 25, 1, total=275), u, second=12),
    ], mode="a")
    calls, checkpoint, diags = reader_codex.read(tmp_path, checkpoint, now=NOW)
    assert diags == []
    assert [c["key"] for c in calls] == [f"{THREAD}:resp_x"]
    assert calls[0]["model"] == "gpt-5"
    state = next(iter(checkpoint["files"].values()))
    assert state["has_records"] is True
    write(path, [tc(usage(300, 10, 0, 30, 1, total=330), u, second=13)], mode="a")
    calls, checkpoint, _ = reader_codex.read(tmp_path, checkpoint, now=NOW)
    assert calls == []
    assert next(iter(checkpoint["files"].values()))["has_records"] is True


def test_child_session_root(tmp_path):
    child = "33333333-3333-3333-3333-333333333333"
    path = rollout(tmp_path)
    write(path, [
        meta(thread=child, session_id=ROOT, parent=THREAD,
             source={"subagent": {"other": "guardian"}}),
        turn("t1", second=1),
        record("t1", "resp_c", usage(10, 0, 0, 1, 0), thread=child, second=2),
    ])
    calls, _, _ = reader_codex.read(tmp_path, None, now=NOW)
    assert len(calls) == 1
    assert calls[0]["session"] == child
    assert calls[0]["root_session"] == ROOT
    assert calls[0]["key"] == f"{child}:resp_c"


def test_root_falls_back_to_parent_then_id(tmp_path):
    child = "44444444-4444-4444-4444-444444444444"
    row = meta(thread=child, parent=THREAD)
    row["payload"]["session_id"] = None
    write(rollout(tmp_path), [row, turn("t1"), record("t1", "r", usage(1, 0, 0, 1, 0), thread=child)])
    alone = meta(thread=ROOT)
    del alone["payload"]["session_id"]
    write(rollout(tmp_path, name="rollout-b.jsonl"),
          [alone, turn("t1"), record("t1", "r", usage(1, 0, 0, 1, 0), thread=ROOT)])
    calls = by_key(reader_codex.read(tmp_path, None, now=NOW)[0])
    assert calls[f"{child}:r"]["root_session"] == THREAD
    assert calls[f"{ROOT}:r"]["root_session"] == ROOT


def test_turn_from_earlier_read_uses_checkpoint(tmp_path):
    path = rollout(tmp_path)
    write(path, [meta(), turn("t1", model="gpt-5-old", cwd=CWD2, second=1),
                 record("t1", "resp_0", usage(1, 0, 0, 1, 0), second=2),
                 turn("t2", model="gpt-5-new", cwd=CWD, second=3)])
    _, checkpoint, _ = reader_codex.read(tmp_path, None, now=NOW)
    write(path, [record("t1", "resp_1", usage(5, 0, 0, 1, 0), second=4)], mode="a")
    calls, _, _ = reader_codex.read(tmp_path, checkpoint, now=NOW)
    assert len(calls) == 1
    assert calls[0]["model"] == "gpt-5-old"
    assert calls[0]["cwd"] == CWD2


def test_unknown_turn_falls_back_to_latest(tmp_path):
    write(rollout(tmp_path), [meta(), turn("t1", model="gpt-5-latest", cwd=CWD2),
                              record("missing", "r", usage(1, 0, 0, 1, 0))])
    calls, _, _ = reader_codex.read(tmp_path, None, now=NOW)
    assert calls[0]["model"] == "gpt-5-latest"
    assert calls[0]["cwd"] == CWD2


def test_turns_keep_at_most_64(tmp_path):
    assert reader_codex._TURN_MEMORY == 64
    rows = [meta()] + [turn(f"t{i}", model=f"m{i}") for i in range(100)]
    write(rollout(tmp_path), rows)
    _, checkpoint, _ = reader_codex.read(tmp_path, None, now=NOW)
    state = next(iter(checkpoint["files"].values()))
    assert len(state["turns"]) == 64
    assert "t99" in state["turns"] and "t36" in state["turns"]
    assert "t35" not in state["turns"]
    assert state["model"] == "m99"


def test_archived_sessions_are_read(tmp_path):
    write(rollout(tmp_path, archived=True), [
        meta(), turn("t1"), record("t1", "resp_arch", usage(1, 0, 0, 1, 0))])
    write(tmp_path / "sessions" / "other.jsonl", [
        meta(), turn("t1"), record("t1", "resp_ignored", usage(1, 0, 0, 1, 0))])
    calls, checkpoint, _ = reader_codex.read(tmp_path, None, now=NOW)
    assert [c["key"] for c in calls] == [f"{THREAD}:resp_arch"]
    assert list(checkpoint["files"]) == ["archived_sessions/rollout-2026-10-01T10-00-00-a.jsonl"]


def test_token_mapping(tmp_path):
    write(rollout(tmp_path), [meta(), turn("t1"),
                              record("t1", "r", usage(900, 300, 11, 77, 33))])
    call = reader_codex.read(tmp_path, None, now=NOW)[0][0]
    assert call["sent_includes_cache_read"] is True
    assert call["tokens_sent"] == 900
    assert call["cache_read"] == 300
    assert call["cache_write"] == 11
    assert call["tokens_returned"] == 77
    assert call["reasoning"] == 33
    assert call["cache_write_5m"] is None
    assert call["speed"] is None


def test_missing_counters_become_none(tmp_path):
    write(rollout(tmp_path), [meta(), turn("t1"),
                              record("t1", "r", {"input_tokens": 5, "output_tokens": True})])
    call = reader_codex.read(tmp_path, None, now=NOW)[0][0]
    assert call["tokens_sent"] == 5
    assert call["tokens_returned"] is None
    assert call["cache_read"] is None


def test_sentinel_never_stored(tmp_path):
    path = rollout(tmp_path)
    write(path, [meta(), turn("t1")] + noise() + [
        record("t1", "r1", usage(1, 0, 0, 1, 0)),
        tc(usage(1, 0, 0, 1, 0), usage(1, 0, 0, 1, 0)),
    ])
    with path.open("a", encoding="utf-8") as handle:
        handle.write("{not json " + SENT + "\n")
        handle.write(json.dumps(line("token_usage_record", {"thread_id": SENT})) + "\n")
        handle.write(json.dumps({"timestamp": SENT, "type": "token_usage_record",
                                 "payload": {"thread_id": THREAD, "response_id": "r2",
                                             "usage": usage(1, 0, 0, 1, 0)}}) + "\n")
    calls, checkpoint, diags = reader_codex.read(tmp_path, None, now=NOW)
    assert diags, "malformed lines should produce diagnostics"
    blob = json.dumps(calls) + json.dumps(checkpoint) + repr(diags)
    assert SENT not in blob
    for diag in diags:
        assert diag.code.startswith("codex_")


def test_bad_line_diagnostic_has_line_number(tmp_path):
    path = rollout(tmp_path)
    write(path, [meta()])
    with path.open("a", encoding="utf-8") as handle:
        handle.write("garbage " + SENT + "\n")
    _, _, diags = reader_codex.read(tmp_path, None, now=NOW)
    assert [(d.code, d.line) for d in diags] == [("codex_bad_line", 2)]
    assert diags[0].source and SENT not in diags[0].message


def test_replaced_file_resets_state(tmp_path):
    path = rollout(tmp_path)
    write(path, [meta(), turn("t1", model="old-model"),
                 tc(usage(1, 0, 0, 1, 0, total=500), usage(1, 0, 0, 1, 0)),
                 record("t1", "r1", usage(1, 0, 0, 1, 0))])
    _, checkpoint, _ = reader_codex.read(tmp_path, None, now=NOW)
    assert next(iter(checkpoint["files"].values()))["has_records"] is True
    other = "55555555-5555-5555-5555-555555555555"
    write(path, [meta(thread=other, cwd=CWD2),
                 tc(usage(9, 0, 0, 1, 0, total=500), usage(9, 0, 0, 1, 0)),
                 line("event_msg", {"type": "user_message", "message": SENT}),
                 line("event_msg", {"type": "user_message", "message": SENT})])
    calls, checkpoint, _ = reader_codex.read(tmp_path, checkpoint, now=NOW)
    assert [c["key"] for c in calls] == [f"{other}:tc:500"]
    assert calls[0]["model"] is None
    assert calls[0]["cwd"] == CWD2
    state = next(iter(checkpoint["files"].values()))
    assert state["has_records"] is False
    assert state["turns"] == {}
    assert state["session"] == other


def test_reread_is_stable_and_unchanged_file_skipped(tmp_path):
    write(rollout(tmp_path), [meta(), turn("t1"),
                              record("t1", "r1", usage(1, 0, 0, 1, 0))])
    first, checkpoint, _ = reader_codex.read(tmp_path, None, now=NOW)
    again, _, _ = reader_codex.read(tmp_path, None, now=NOW)
    assert first == again
    calls, checkpoint2, _ = reader_codex.read(tmp_path, checkpoint, now=NOW)
    assert calls == []
    assert checkpoint2 == checkpoint


def test_duplicate_key_keeps_last(tmp_path):
    write(rollout(tmp_path), [meta(), turn("t1"),
                              record("t1", "r1", usage(1, 0, 0, 1, 0)),
                              record("t1", "r1", usage(7, 0, 0, 1, 0))])
    calls, _, _ = reader_codex.read(tmp_path, None, now=NOW)
    assert len(calls) == 1
    assert calls[0]["tokens_sent"] == 7


def test_missing_source(tmp_path):
    checkpoint = {"files": {"x": {"has_records": True}}}
    calls, new, diags = reader_codex.read(tmp_path / "nope", checkpoint, now=NOW)
    assert calls == []
    assert new == checkpoint
    assert [d.code for d in diags] == ["codex_source_missing"]


def test_garbage_checkpoint_is_tolerated(tmp_path):
    write(rollout(tmp_path), [meta(), turn("t1"),
                              record("t1", "r1", usage(1, 0, 0, 1, 0))])
    for bad in ({"files": "nope"}, {"files": {"sessions/2026/10/01/rollout-2026-10-01T10-00-00-a.jsonl": 5}}, {}):
        calls, _, _ = reader_codex.read(tmp_path, bad, now=NOW)
        assert len(calls) == 1

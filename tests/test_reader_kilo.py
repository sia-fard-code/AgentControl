from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from agent_control import reader_kilo

SENTINEL = "SENTINEL-TEXT-DO-NOT-STORE"
NOW = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)
READER_KEYS = ("tool", "key", "session", "root_session", "at", "model", "cwd",
               "tokens_sent", "tokens_returned", "cache_read", "cache_write",
               "cache_write_5m", "reasoning", "sent_includes_cache_read", "speed")


def asst(**over):
    data = {
        "role": "assistant",
        "modelID": "anthropic_tool_call.claude-opus-4-8-think",
        "providerID": "openwebui",
        "cost": 0,
        "tokens": {"input": 1200, "output": 300, "reasoning": 50,
                   "cache": {"read": 90000, "write": 7}, "total": 91557},
        "path": {"cwd": "D:/repos/iptv", "root": "D:/repos/iptv"},
        "time": {"created": 1759300000000, "completed": 1759300004000},
        "variant": "high", "agent": "code", "mode": "code",
        "finish": "tool-calls", "parentID": "msg_x",
        "summary": {"title": SENTINEL}, "text": SENTINEL,
    }
    data.update(over)
    return data


def user():
    return {"role": "user", "text": SENTINEL, "summary": {"title": SENTINEL}}


def make_db(path: Path, sessions, messages, *, wal: bool = False) -> Path:
    con = sqlite3.connect(path)
    if wal:
        con.execute("pragma journal_mode=wal")
    con.execute("create table session (id text primary key, parent_id text,"
                " directory text, title text)")
    con.execute("create table message (id text primary key, session_id text,"
                " time_created integer, time_updated integer, data text)")
    con.execute("create table part (id text primary key, message_id text, data text)")
    for sid, parent, directory in sessions:
        con.execute("insert into session values (?, ?, ?, ?)",
                    (sid, parent, directory, SENTINEL))
    for row in messages:
        insert_message(con, *row)
    con.commit()
    con.close()
    return path


def insert_message(con, mid, sid, tc, tu, data):
    text = data if isinstance(data, str) else json.dumps(data)
    con.execute("insert or replace into message values (?, ?, ?, ?, ?)",
                (mid, sid, tc, tu, text))
    con.execute("insert or replace into part values (?, ?, ?)",
                ("prt_" + mid, mid, json.dumps({"type": "text", "text": SENTINEL})))


def add_rows(path: Path, messages) -> None:
    con = sqlite3.connect(path)
    for row in messages:
        insert_message(con, *row)
    con.commit()
    con.close()


def read(path, cp=None):
    return reader_kilo.read(path, cp, now=NOW)


SESSIONS = [("ses_a", None, "D:/Work/root-dir")]


# 1
def test_assistant_message_maps_every_field(tmp_path):
    db = make_db(tmp_path / "kilo.db", SESSIONS,
                 [("msg_1", "ses_a", 1759300000000, 1759300004500, asst())])
    calls, cp, diags = read(db)
    assert diags == []
    assert calls == [{
        "tool": "kilo", "key": "msg_1", "session": "ses_a", "root_session": "ses_a",
        "at": "2025-10-01T06:26:44+00:00",
        "model": "anthropic_tool_call.claude-opus-4-8-think",
        "cwd": "D:/repos/iptv",
        "tokens_sent": 1200, "tokens_returned": 350, "cache_read": 90000,
        "cache_write": 7, "cache_write_5m": None, "reasoning": 50,
        "sent_includes_cache_read": False, "speed": None,
    }]
    assert tuple(calls[0]) == READER_KEYS
    assert cp == {"updated_after": 1759300004500, "ids_at": ["msg_1"]}
    assert reader_kilo.TOOL == "kilo"


# 2
def test_tokens_returned_rules(tmp_path):
    rows = [
        ("m_both", "ses_a", 1, 10, asst(tokens={"input": 1, "output": 10, "reasoning": 5})),
        ("m_no_reason", "ses_a", 1, 11, asst(tokens={"input": 1, "output": 10})),
        ("m_no_output", "ses_a", 1, 12, asst(tokens={"input": 1, "reasoning": 5})),
    ]
    calls, _, diags = read(make_db(tmp_path / "kilo.db", SESSIONS, rows))
    assert diags == []
    by_key = {c["key"]: c for c in calls}
    assert (by_key["m_both"]["tokens_returned"], by_key["m_both"]["reasoning"]) == (15, 5)
    assert by_key["m_no_reason"]["tokens_returned"] == 10
    assert by_key["m_no_reason"]["reasoning"] is None
    assert by_key["m_no_output"]["tokens_returned"] is None
    assert by_key["m_no_output"]["reasoning"] == 5
    assert by_key["m_no_reason"]["cache_read"] is None


# 3
def test_user_and_tokenless_assistant_messages_skipped(tmp_path):
    no_tokens = asst()
    del no_tokens["tokens"]
    rows = [
        ("m_user", "ses_a", 1, 10, user()),
        ("m_none", "ses_a", 1, 11, no_tokens),
        ("m_str", "ses_a", 1, 12, asst(tokens="lots")),
        ("m_ok", "ses_a", 1, 13, asst()),
    ]
    calls, cp, diags = read(make_db(tmp_path / "kilo.db", SESSIONS, rows))
    assert [c["key"] for c in calls] == ["m_ok"]
    assert diags == []
    assert cp == {"updated_after": 13, "ids_at": ["m_ok"]}


def test_unparseable_data_gives_bad_line_with_id_only(tmp_path):
    rows = [
        ("m_bad", "ses_a", 1, 10, SENTINEL + " {not json"),
        ("m_list", "ses_a", 1, 11, "[1, 2]"),
        ("m_ok", "ses_a", 1, 12, asst()),
    ]
    calls, _, diags = read(make_db(tmp_path / "kilo.db", SESSIONS, rows))
    assert [c["key"] for c in calls] == ["m_ok"]
    assert [d.code for d in diags] == ["kilo_bad_line", "kilo_bad_line"]
    assert "m_bad" in diags[0].message and "m_list" in diags[1].message
    assert all(SENTINEL not in repr(d) for d in diags)


# 4
def test_root_walks_up_three_levels_and_cycle_terminates(tmp_path):
    sessions = [
        ("ses_top", None, "D:/top"), ("ses_1", "ses_top", "D:/1"),
        ("ses_2", "ses_1", "D:/2"), ("ses_3", "ses_2", "D:/3"),
        ("ses_cA", "ses_cB", "D:/a"), ("ses_cB", "ses_cA", "D:/b"),
        ("ses_orphan", "ses_gone", "D:/o"),
    ]
    rows = [
        ("m_deep", "ses_3", 1, 10, asst()),
        ("m_cycle", "ses_cA", 1, 11, asst()),
        ("m_orphan", "ses_orphan", 1, 12, asst()),
        ("m_nosession", "ses_missing", 1, 13, asst()),
    ]
    calls, _, diags = read(make_db(tmp_path / "kilo.db", sessions, rows))
    assert diags == []
    roots = {c["key"]: c["root_session"] for c in calls}
    assert roots["m_deep"] == "ses_top"
    assert roots["m_cycle"] in ("ses_cA", "ses_cB")
    assert roots["m_orphan"] == "ses_orphan"
    assert roots["m_nosession"] == "ses_missing"


# 5
def test_cwd_and_at_fallbacks(tmp_path):
    no_path = asst(time={"created": 1759300000000})
    del no_path["path"]
    rows = [
        ("m_fb", "ses_a", 1, 10, no_path),
        ("m_nocwd", "ses_a", 1, 11, asst(path={"root": "D:/r"})),
    ]
    calls, _, _ = read(make_db(tmp_path / "kilo.db", SESSIONS, rows))
    by_key = {c["key"]: c for c in calls}
    assert by_key["m_fb"]["cwd"] == "D:/Work/root-dir"
    assert by_key["m_fb"]["at"] == "2025-10-01T06:26:40+00:00"
    assert by_key["m_nocwd"]["cwd"] == "D:/Work/root-dir"
    assert by_key["m_nocwd"]["at"] == "2025-10-01T06:26:44+00:00"


# 6
def test_boundary_rows_not_lost_or_repeated(tmp_path):
    T = 1759300009000
    db = make_db(tmp_path / "kilo.db", SESSIONS,
                 [("m_1", "ses_a", 1, T, asst()), ("m_2", "ses_a", 1, T, asst())])
    calls, cp, _ = read(db)
    assert sorted(c["key"] for c in calls) == ["m_1", "m_2"]
    assert cp == {"updated_after": T, "ids_at": ["m_1", "m_2"]}
    add_rows(db, [("m_3", "ses_a", 1, T, asst())])
    calls, cp2, _ = read(db, cp)
    assert [c["key"] for c in calls] == ["m_3"]
    assert cp2 == {"updated_after": T, "ids_at": ["m_1", "m_2", "m_3"]}
    calls, cp3, diags = read(db, cp2)
    assert calls == [] and diags == []
    assert cp3 == cp2
    json.dumps(cp3)


# 7
def test_updated_message_is_reemitted(tmp_path):
    db = make_db(tmp_path / "kilo.db", SESSIONS, [("m_1", "ses_a", 1, 100, asst())])
    calls, cp, _ = read(db)
    assert calls[0]["tokens_sent"] == 1200
    newer = asst(tokens={"input": 5000, "output": 1, "cache": {"read": 2, "write": 3}})
    add_rows(db, [("m_1", "ses_a", 1, 200, newer)])
    calls, cp2, _ = read(db, cp)
    assert [(c["key"], c["tokens_sent"]) for c in calls] == [("m_1", 5000)]
    assert cp2 == {"updated_after": 200, "ids_at": ["m_1"]}


# 8
def test_garbage_file_gives_db_unavailable_and_keeps_checkpoint(tmp_path):
    db = tmp_path / "kilo.db"
    db.write_bytes(b"this is not a sqlite database at all " * 200)
    cp = {"updated_after": 42, "ids_at": ["m_x"]}
    calls, cp2, diags = read(db, cp)
    assert calls == []
    assert cp2 == cp
    assert [d.code for d in diags] == ["kilo_db_unavailable"]
    calls, cp3, diags = read(db, None)
    assert calls == [] and [d.code for d in diags] == ["kilo_db_unavailable"]


# 9
def test_db_bytes_unchanged_by_read(tmp_path):
    db = make_db(tmp_path / "kilo.db", SESSIONS,
                 [("m_1", "ses_a", 1, 10, asst()), ("m_2", "ses_a", 1, 11, user())],
                 wal=True)
    before = {p.name: p.read_bytes() for p in tmp_path.iterdir()}
    calls, _, _ = read(db)
    assert len(calls) == 1
    after = {p.name: p.read_bytes() for p in tmp_path.iterdir() if p.name in before}
    assert after == before


# 10
def test_sentinel_never_leaks(tmp_path):
    rows = [
        ("m_1", "ses_a", 1, 10, asst()),
        ("m_2", "ses_a", 1, 11, user()),
        ("m_3", "ses_a", 1, 12, SENTINEL),
    ]
    db = make_db(tmp_path / "kilo.db", SESSIONS, rows)
    calls, cp, diags = read(db)
    assert len(calls) == 1
    blob = json.dumps(calls) + json.dumps(cp) + repr(diags)
    assert SENTINEL not in blob


# 11
def test_missing_source(tmp_path):
    cp = {"updated_after": 5, "ids_at": []}
    calls, cp2, diags = read(tmp_path / "nope" / "kilo.db", cp)
    assert calls == [] and cp2 == cp
    assert [d.code for d in diags] == ["kilo_source_missing"]
    assert not (tmp_path / "nope").exists()

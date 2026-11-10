from __future__ import annotations

import pytest

from agent_control import operator_log
from agent_control.storage import StateLocks

KNOWN_KEYS = {
    "schema",
    "ts",
    "action",
    "run_id",
    "outcome",
    "kind",
    "chars",
    "engine_pid",
    "children",
    "survivors",
}


def test_append_and_read_back(tmp_path):
    lock = StateLocks().write
    operator_log.append_action(
        tmp_path, lock, ts="2025-01-01T00:00:00+00:00", action="kill",
        run_id="r1", outcome="terminated", engine_pid=5, children=0, survivors=0,
    )
    operator_log.append_action(
        tmp_path, lock, ts="2025-01-01T00:00:01+00:00", action="steer",
        run_id="r2", outcome="ok", kind="steer", chars=3,
    )
    records = operator_log.read_actions(tmp_path)
    assert len(records) == 2
    for record in records:
        assert set(record.keys()) == KNOWN_KEYS


def test_record_has_no_text_fields(tmp_path):
    lock = StateLocks().write
    record = operator_log.append_action(
        tmp_path, lock, ts="2025-01-01T00:00:00+00:00", action="steer",
        run_id="r1", outcome="ok", kind="steer", chars=12,
    )
    for banned in ("text", "message", "content"):
        assert banned not in record


def test_malformed_lines_ignored(tmp_path):
    lock = StateLocks().write
    operator_log.append_action(
        tmp_path, lock, ts="2025-01-01T00:00:00+00:00", action="kill",
        run_id="r1", outcome="terminated",
    )
    path = operator_log.log_path(tmp_path)
    with path.open("a", encoding="utf-8") as handle:
        handle.write("not json\n")
        handle.write('{"schema": 2, "action": "kill", "run_id": "r2",'
                     ' "ts": "t", "outcome": "terminated"}\n')
        handle.write('{"schema": 1, "action": "dispatch", "run_id": "r3",'
                     ' "ts": "t", "outcome": "ok"}\n')
    records = operator_log.read_actions(tmp_path)
    assert [r["run_id"] for r in records] == ["r1"]


def test_read_is_memoised(tmp_path, monkeypatch):
    lock = StateLocks().write
    operator_log.append_action(
        tmp_path, lock, ts="2025-01-01T00:00:00+00:00", action="kill",
        run_id="r1", outcome="terminated",
    )
    calls = {"n": 0}
    original = operator_log.read_jsonl

    def counting(*args, **kwargs):
        calls["n"] += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(operator_log, "read_jsonl", counting)
    operator_log.read_actions(tmp_path)
    operator_log.read_actions(tmp_path)
    assert calls["n"] == 1
    operator_log.append_action(
        tmp_path, lock, ts="2025-01-01T00:00:02+00:00", action="kill",
        run_id="r2", outcome="terminated",
    )
    operator_log.read_actions(tmp_path)
    assert calls["n"] == 2


def test_killed_run_ids(tmp_path):
    lock = StateLocks().write
    data = [
        ("kill", "r1", "terminated"),
        ("kill", "r2", "partial"),
        ("kill", "r3", "already_gone"),
        ("kill", "r4", "not_verified"),
        ("steer", "r5", "ok"),
    ]
    for i, (action, run_id, outcome) in enumerate(data):
        operator_log.append_action(
            tmp_path, lock, ts=f"2025-01-01T00:00:0{i}+00:00", action=action,
            run_id=run_id, outcome=outcome,
        )
    actions = operator_log.read_actions(tmp_path)
    assert operator_log.killed_run_ids(actions) == frozenset({"r1", "r2"})


def test_append_rejects_unknown_action(tmp_path):
    lock = StateLocks().write
    with pytest.raises(ValueError):
        operator_log.append_action(
            tmp_path, lock, ts="t", action="dispatch",
            run_id="r1", outcome="ok",
        )

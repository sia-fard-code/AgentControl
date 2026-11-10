from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent_control.models import IndexCheckpoint
from agent_control.run_index import (
    EndEvent,
    StartEvent,
    parse_index_record,
    read_index_batch,
    reduce_index_events,
    validate_artifact_paths,
)


def start_json(
    *,
    run_id: str = "run-1",
    schema: int | None = 2,
    root: str = r"Q:\agentcontrol-test\Projectb",
    cwd: str | None = None,
    label: str = "demo",
    engine_pid: int | None = 4321,
    max_iters: int | None = None,
    ts: str = "2026-09-17T12:00:00+00:00",
    transcript: str = r"Q:\agentcontrol-test\Projectb\.claude\agent-runs\a.md",
    messages: str = r"Q:\agentcontrol-test\Projectb\.claude\agent-runs\a.messages.jsonl",
    usage_journal: str = r"Q:\agentcontrol-test\Projectb\.claude\agent-runs\a.usage.jsonl",
) -> dict[str, object]:
    record: dict[str, object] = {
        "run_id": run_id,
        "event": "start",
        "ts": ts,
        "harness_version": 6,
        "root": root,
        "cwd": cwd if cwd is not None else root,
        "label": label,
        "model": "model",
        "effort": None,
        "tool_mode": None,
        "write_mode": "stage",
        "engine_pid": engine_pid,
        "transcript": transcript,
        "messages": messages,
        "usage_journal": usage_journal,
    }
    if max_iters is not None:
        record["max_iters"] = max_iters
    if schema is not None:
        record["schema"] = schema
    return record


def end_json(
    *,
    run_id: str = "run-1",
    schema: int | None = 2,
    status: str = "complete",
) -> dict[str, object]:
    record: dict[str, object] = {
        "run_id": run_id,
        "event": "end",
        "ts": "2026-09-17T12:05:00+00:00",
        "status": status,
        "tool_calls": 10,
        "refused": 0,
        "sent": 1234,
        "recv": 567,
    }
    if schema is not None:
        record["schema"] = schema
    return record


def start_event(**kwargs: object) -> StartEvent:
    return parse_index_record(start_json(**kwargs), 1)  # type: ignore[arg-type,return-value]


def end_event(**kwargs: object) -> EndEvent:
    return parse_index_record(end_json(**kwargs), 1)  # type: ignore[arg-type,return-value]


def _write(path: Path, records: list[dict[str, object]]) -> None:
    path.write_text(
        "".join(json.dumps(r, separators=(",", ":")) + "\n" for r in records),
        encoding="utf-8",
    )


def test_schema1_missing_status_maps_ok_to_complete() -> None:
    event = parse_index_record(
        {"run_id": "r", "event": "end", "ts": "2026-09-17T12:00:00+00:00", "status": "ok"},
        1,
    )
    assert isinstance(event, EndEvent)
    assert event.schema == 1
    assert event.status == "complete"


def test_schema1_start_has_no_engine_pid() -> None:
    record = start_json(schema=None, engine_pid=None)
    del record["engine_pid"]
    event = parse_index_record(record, 1)
    assert isinstance(event, StartEvent)
    assert event.schema == 1
    assert event.engine_pid is None


def test_start_parses_max_iters_when_present() -> None:
    event = parse_index_record(start_json(max_iters=25), 1)
    assert isinstance(event, StartEvent)
    assert event.max_iters == 25


def test_start_max_iters_is_none_when_absent() -> None:
    record = start_json()
    assert "max_iters" not in record
    event = parse_index_record(record, 1)
    assert isinstance(event, StartEvent)
    assert event.max_iters is None


def test_schema2_accepts_terminal_statuses() -> None:
    for status in ("complete", "failed", "interrupted"):
        event = parse_index_record(end_json(status=status), 1)
        assert isinstance(event, EndEvent)
        assert event.status == status


def test_unsupported_record_raises() -> None:
    with pytest.raises(ValueError):
        parse_index_record({"event": "sneeze"}, 1)


def test_start_and_end_never_join_by_label_or_filename() -> None:
    pairs, diagnostics = reduce_index_events(
        [
            start_event(run_id="start-id", label="same"),
            end_event(run_id="end-id"),
        ]
    )
    assert pairs["start-id"][1] is None
    assert pairs["end-id"][0] is None


def test_exact_duplicate_events_are_idempotent() -> None:
    pairs, diagnostics = reduce_index_events(
        [start_event(run_id="r"), start_event(run_id="r")]
    )
    assert pairs["r"][0] is not None
    assert diagnostics == ()


def test_conflicting_duplicate_emits_diagnostic() -> None:
    pairs, diagnostics = reduce_index_events(
        [end_event(run_id="r", status="complete"), end_event(run_id="r", status="failed")]
    )
    assert pairs["r"][1].status == "complete"
    assert any(d.code == "conflicting_duplicate_event" for d in diagnostics)


def test_worktree_cwd_is_preserved_but_root_identifies_project() -> None:
    event = start_event(root=r"D:\repos\Projectb", cwd=r"D:\repos\Projectb\.worktrees\wt1")
    assert event.root == r"D:\repos\Projectb"
    assert event.cwd == r"D:\repos\Projectb\.worktrees\wt1"


def test_validate_artifact_paths_flags_missing_sibling(tmp_path: Path) -> None:
    transcript = tmp_path / "a.md"
    transcript.write_text("x", encoding="utf-8")
    event = start_event(
        transcript=str(transcript),
        messages=str(tmp_path / "a.messages.jsonl"),
        usage_journal=str(tmp_path / "missing.usage.jsonl"),
    )
    checked, diagnostics = validate_artifact_paths(event)
    assert checked.transcript == str(transcript)
    assert any(d.code == "inaccessible_artifact" for d in diagnostics)


def test_read_batch_incremental_offset(tmp_path: Path) -> None:
    path = tmp_path / "index.jsonl"
    _write(path, [start_json(run_id="a")])
    first = read_index_batch(path, IndexCheckpoint(offset=0, identity=None))
    assert len(first.events) == 1
    assert first.full_rescan is False
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(end_json(run_id="a"), separators=(",", ":")) + "\n")
    second = read_index_batch(path, first.checkpoint)
    assert len(second.events) == 1
    assert isinstance(second.events[0], EndEvent)


def test_partial_trailing_line_is_not_consumed(tmp_path: Path) -> None:
    path = tmp_path / "index.jsonl"
    complete = json.dumps(start_json(run_id="a"), separators=(",", ":")) + "\n"
    path.write_bytes((complete + '{"run_id":"b","eve').encode("utf-8"))
    batch = read_index_batch(path, IndexCheckpoint(offset=0, identity=None))
    assert len(batch.events) == 1
    assert batch.checkpoint.offset == len(complete.encode("utf-8"))


def test_truncation_forces_safe_full_rescan(tmp_path: Path) -> None:
    path = tmp_path / "index.jsonl"
    _write(path, [start_json(run_id="original"), end_json(run_id="original")])
    first = read_index_batch(path, IndexCheckpoint(offset=0, identity=None))
    _write(path, [start_json(run_id="replacement")])
    batch = read_index_batch(path, first.checkpoint)
    assert batch.full_rescan is True
    assert batch.events[0].run_id == "replacement"


def test_malformed_line_does_not_stop_batch(tmp_path: Path) -> None:
    path = tmp_path / "index.jsonl"
    good = json.dumps(start_json(run_id="a"), separators=(",", ":")) + "\n"
    path.write_text(good + "{malformed\n" + json.dumps(end_json(run_id="a")) + "\n", encoding="utf-8")
    batch = read_index_batch(path, IndexCheckpoint(offset=0, identity=None))
    run_ids = {event.run_id for event in batch.events}
    assert run_ids == {"a"}
    assert any(d.code == "malformed_json" for d in batch.diagnostics)

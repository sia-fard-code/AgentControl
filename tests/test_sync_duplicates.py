from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from agent_control.discovery import repo_index_path
from agent_control.ledger import highest_valid_revisions, load_ledger
from agent_control.models import ProcessSnapshot
from agent_control.paths import AppPaths
from agent_control.storage import StateLocks
from agent_control.sync import Synchronizer


class FakeProbe:
    def inspect(self, pid: int) -> ProcessSnapshot | None:
        # The pid is reported dead: no live process is ever found.
        return None


def _synchronizer(paths: AppPaths) -> Synchronizer:
    return Synchronizer(
        paths,
        StateLocks(),
        lambda: datetime(2026, 9, 17, 13, 0, tzinfo=timezone.utc),
        FakeProbe(),
    )


def _make_artifacts(repo: Path, run_id: str) -> dict[str, str]:
    runs = repo / ".claude" / "agent-runs"
    runs.mkdir(parents=True, exist_ok=True)
    transcript = runs / f"{run_id}.md"
    messages = runs / f"{run_id}.messages.jsonl"
    usage = runs / f"{run_id}.usage.jsonl"
    transcript.write_text("# transcript\n", encoding="utf-8")
    messages.write_text("{}\n", encoding="utf-8")
    usage.write_text("{}\n", encoding="utf-8")
    return {
        "transcript": str(transcript),
        "messages": str(messages),
        "usage_journal": str(usage),
    }


def _start_record(
    repo: Path, run_id: str, engine_pid: int | None = None
) -> dict[str, object]:
    art = _make_artifacts(repo, run_id)
    return {
        "schema": 2,
        "run_id": run_id,
        "event": "start",
        "ts": "2026-09-17T12:00:00+00:00",
        "harness_version": 6,
        "root": str(repo),
        "cwd": str(repo),
        "label": "demo",
        "model": "model",
        "effort": None,
        "tool_mode": None,
        "write_mode": "disk",
        "engine_pid": engine_pid,
        "transcript": art["transcript"],
        "messages": art["messages"],
        "usage_journal": art["usage_journal"],
    }


def _end_record(run_id: str, status: str = "complete") -> dict[str, object]:
    return {
        "schema": 2,
        "run_id": run_id,
        "event": "end",
        "ts": "2026-09-17T12:05:00+00:00",
        "status": status,
        "tool_calls": 10,
        "refused": 0,
        "sent": 1234,
        "recv": 567,
    }


def _append(path: Path, records: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        for record in records:
            handle.write(
                json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n"
            )


def _read_state(paths: AppPaths) -> dict:
    return json.loads(paths.sync_state.read_text(encoding="utf-8"))


def _write_scan_roots(paths: AppPaths, roots: list[str]) -> None:
    paths.scan_roots_file.parent.mkdir(parents=True, exist_ok=True)
    paths.scan_roots_file.write_text(json.dumps(roots), encoding="utf-8")


def test_late_duplicate_end_dropped(tmp_path: Path) -> None:
    paths = AppPaths.for_test(tmp_path)
    scan_root = tmp_path / "work"
    repo = scan_root / "Repo"
    repo.mkdir(parents=True)
    # Make the repo index discoverable from sync 1 so its start-only line is
    # read then, and only the late end is new in sync 2.
    _write_scan_roots(paths, [str(scan_root)])

    start = _start_record(repo, "e1")
    end = _end_record("e1")
    # Machine index has the full pair; the repo index initially only the start.
    _append(paths.machine_index, [start, end])
    idx = repo_index_path(repo)
    _append(idx, [start])

    sync = _synchronizer(paths)
    sync.run()

    records = highest_valid_revisions(load_ledger(paths.ledger)[0])
    assert records["e1"].status == "complete"

    # The identical end reaches the repo index a moment later.
    _append(idx, [end])
    sync.run()

    state = _read_state(paths)
    assert state["unmatched_ends"] == {}
    records = highest_valid_revisions(load_ledger(paths.ledger)[0])
    assert records["e1"].status == "complete"


def test_late_duplicate_start_ignored(tmp_path: Path) -> None:
    paths = AppPaths.for_test(tmp_path)
    repo = tmp_path / "Repo"
    repo.mkdir(parents=True)

    start = _start_record(repo, "s1", engine_pid=4242)
    end = _end_record("s1")
    # Only the machine index sees the pair; the ledger closes the run.
    _append(paths.machine_index, [start, end])

    sync = _synchronizer(paths)
    sync.run()
    records = highest_valid_revisions(load_ledger(paths.ledger)[0])
    assert records["s1"].status == "complete"

    # The identical start lands in the repo index a moment later, alone.
    _append(repo_index_path(repo), [start])
    report = sync.run()

    assert report.appended_revisions == 0
    records = highest_valid_revisions(load_ledger(paths.ledger)[0])
    assert records["s1"].status == "complete"

    state = _read_state(paths)
    assert "s1" not in state["open_starts"]
    assert "s1" not in sync.open_runs


def test_genuine_unmatched_end_still_carried(tmp_path: Path) -> None:
    paths = AppPaths.for_test(tmp_path)

    # An end for a run the ledger has never seen: no start anywhere.
    _append(paths.machine_index, [_end_record("u1")])

    sync = _synchronizer(paths)
    sync.run()

    state = _read_state(paths)
    assert "u1" in state["unmatched_ends"]

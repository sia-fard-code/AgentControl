from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

import pytest

from agent_control.discovery import repo_index_path
from agent_control.ledger import highest_valid_revisions, load_ledger
from agent_control.models import ProcessSnapshot
from agent_control.paths import AppPaths
from agent_control.projects import load_projects
from agent_control.storage import StateLocks
from agent_control.sync import Synchronizer


class FakeProbe:
    def inspect(self, pid: int) -> ProcessSnapshot | None:
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


def _start_record(repo: Path, run_id: str) -> dict[str, object]:
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
        "engine_pid": None,
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
            handle.write(json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n")


def _write_scan_roots(paths: AppPaths, roots: list[str]) -> None:
    paths.scan_roots_file.parent.mkdir(parents=True, exist_ok=True)
    paths.scan_roots_file.write_text(json.dumps(roots), encoding="utf-8")


def test_unknown_repo_under_scan_root(tmp_path: Path) -> None:
    paths = AppPaths.for_test(tmp_path)
    scan_root = tmp_path / "work"
    repo = scan_root / "Khattati"
    repo.mkdir(parents=True)
    _write_scan_roots(paths, [str(scan_root)])
    idx = repo_index_path(repo)
    _append(idx, [_start_record(repo, "r1"), _end_record("r1")])

    sync = _synchronizer(paths)
    sync.run()

    projects, _ = load_projects(paths.projects)
    roots = {p.root for p in projects.values()}
    assert any(os.path.normcase(r) == os.path.normcase(str(repo)) for r in roots)
    project = next(p for p in projects.values() if os.path.normcase(p.root) == os.path.normcase(str(repo)))
    assert project.state == "discovered"

    records = highest_valid_revisions(load_ledger(paths.ledger)[0])
    assert records["r1"].status == "complete"


def test_sibling_of_known_project(tmp_path: Path) -> None:
    paths = AppPaths.for_test(tmp_path)
    work = tmp_path / "work"
    known = work / "Known"
    sibling = work / "Sibling"
    known.mkdir(parents=True)
    sibling.mkdir(parents=True)

    # Register the known project via its machine-index run.
    _append(paths.machine_index, [_start_record(known, "k1"), _end_record("k1")])
    _synchronizer(paths).run()

    # Now the sibling gets only a repo index.
    _append(repo_index_path(sibling), [_start_record(sibling, "s1"), _end_record("s1")])
    _synchronizer(paths).run()

    projects, _ = load_projects(paths.projects)
    roots = {os.path.normcase(p.root) for p in projects.values()}
    assert os.path.normcase(str(sibling)) in roots
    records = highest_valid_revisions(load_ledger(paths.ledger)[0])
    assert records["s1"].status == "complete"


def test_identical_records_in_both_indexes(tmp_path: Path) -> None:
    paths = AppPaths.for_test(tmp_path)
    scan_root = tmp_path / "work"
    repo = scan_root / "Repo"
    repo.mkdir(parents=True)
    _write_scan_roots(paths, [str(scan_root)])

    start = _start_record(repo, "d1")
    end = _end_record("d1")
    _append(paths.machine_index, [start, end])
    _append(repo_index_path(repo), [start, end])

    sync = _synchronizer(paths)
    report = sync.run()

    records = highest_valid_revisions(load_ledger(paths.ledger)[0])
    assert records["d1"].status == "complete"
    assert not any(
        d.code == "conflicting_duplicate_event" for d in report.diagnostics
    )


def test_incremental_reading(tmp_path: Path) -> None:
    paths = AppPaths.for_test(tmp_path)
    scan_root = tmp_path / "work"
    repo = scan_root / "Repo"
    repo.mkdir(parents=True)
    _write_scan_roots(paths, [str(scan_root)])
    idx = repo_index_path(repo)
    _append(idx, [_start_record(repo, "r1")])

    sync = _synchronizer(paths)
    first = sync.run()
    assert first.scanned_records == 1

    second = sync.run()
    assert second.scanned_records == 0

    _append(idx, [_end_record("r1")])
    third = sync.run()
    assert third.scanned_records == 1

    state = json.loads(paths.sync_state.read_text(encoding="utf-8"))
    key = os.path.normcase(os.path.abspath(str(idx)))
    assert key in state["repo_checkpoints"]


def test_rewrite_triggers_full_rescan_keeps_other_open(tmp_path: Path) -> None:
    paths = AppPaths.for_test(tmp_path)
    scan_root = tmp_path / "work"
    repo_a = scan_root / "RepoA"
    repo_b = scan_root / "RepoB"
    repo_a.mkdir(parents=True)
    repo_b.mkdir(parents=True)
    _write_scan_roots(paths, [str(scan_root)])
    idx_a = repo_index_path(repo_a)
    idx_b = repo_index_path(repo_b)

    _append(idx_a, [_start_record(repo_a, "a1")])
    _append(idx_b, [_start_record(repo_b, "b1")])

    sync = _synchronizer(paths)
    sync.run()
    assert "a1" in sync.open_runs
    assert "b1" in sync.open_runs

    # Rewrite idx_a wholesale with different content.
    new_start = _start_record(repo_a, "a2")
    idx_a.write_text(
        json.dumps(new_start, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )

    report = sync.run()
    assert report.full_rescan is True
    # The open run in the other repo index must survive.
    assert "b1" in sync.open_runs


def test_malformed_scan_roots_diagnostic(tmp_path: Path) -> None:
    paths = AppPaths.for_test(tmp_path)
    paths.scan_roots_file.parent.mkdir(parents=True, exist_ok=True)
    paths.scan_roots_file.write_text("{bad json", encoding="utf-8")

    sync = _synchronizer(paths)
    report = sync.run()
    assert any(d.code == "invalid_scan_roots" for d in report.diagnostics)

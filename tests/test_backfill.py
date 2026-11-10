from __future__ import annotations

import os
import time
from pathlib import Path

import json

import pytest

from agent_control.backfill import apply_backfill, plan_backfill
from agent_control.cli import main
from agent_control.discovery import repo_index_path
from agent_control.run_index import parse_index_record


COMPLETE_FOOTER = (
    "[anthropic_tool_call.claude-opus-4-8-think e=medium tools=native: "
    "19 tool calls, 239,539 tok sent / 6,898 tok returned]"
)


def _write_transcript(
    repo: Path,
    run_id: str,
    *,
    writes: str = "disk",
    root: str | None = None,
    final: bool = True,
    footer: bool = True,
    body: str | None = None,
    age_seconds: float = 10_000.0,
) -> Path:
    runs = repo / ".claude" / "agent-runs"
    runs.mkdir(parents=True, exist_ok=True)
    label = run_id.split("-", 2)[2]
    root_value = root if root is not None else str(os.path.realpath(repo))
    lines = [
        f"# {label}",
        "- model: `gpt-5.6-sol`  effort: `high`  tools: `prompt`",
        f"- writes: `{writes}`  root: `{root_value}`",
        "- policy: whatever",
        "- steer: none",
        "## Task",
        "do the thing",
        "",
    ]
    if body is not None:
        lines.append(body)
    if final:
        lines += ["", "---", "", "## Final report", "all done", ""]
    if footer:
        lines.append(COMPLETE_FOOTER)
    transcript = runs / f"{run_id}.md"
    transcript.write_text("\n".join(lines) + "\n", encoding="utf-8")
    old = time.time() - age_seconds
    os.utime(transcript, (old, old))
    return transcript


def _mtime(path: Path) -> float:
    return os.path.getmtime(path)


def test_complete_run(tmp_path: Path) -> None:
    repo = tmp_path / "Repo"
    t = _write_transcript(repo, "20260924-171330-runtime")
    plan = plan_backfill(
        repo, machine_index=tmp_path / "machine.jsonl", now=time.time()
    )
    assert plan.skipped == ()
    assert len(plan.records) == 2
    start, end = plan.records
    assert start["event"] == "start"
    assert start["run_id"] == "20260924-171330-runtime"
    assert start["label"] == "runtime"
    assert start["model"] == "gpt-5.6-sol"
    assert start["effort"] == "high"
    assert start["tool_mode"] == "prompt"
    assert start["write_mode"] == "disk"
    assert start["backfilled"] is True
    assert start["harness_version"] is None
    assert start["engine_pid"] is None
    assert start["max_iters"] is None
    assert start["root"] == os.path.realpath(repo)
    assert start["cwd"] == os.path.realpath(repo)
    assert Path(start["transcript"]).is_absolute()
    assert end["event"] == "end"
    assert end["status"] == "complete"
    assert end["tool_calls"] == 19
    assert end["sent"] == 239539
    assert end["recv"] == 6898
    assert end["refused"] is None
    assert end["backfilled"] is True
    for rec in plan.records:
        parse_index_record(rec, 1)


def test_failed_run(tmp_path: Path) -> None:
    repo = tmp_path / "Repo"
    _write_transcript(repo, "20260924-171330-runtime", final=False, footer=True)
    plan = plan_backfill(
        repo, machine_index=tmp_path / "machine.jsonl", now=time.time()
    )
    assert len(plan.records) == 2
    end = plan.records[1]
    assert end["status"] == "failed"
    assert end["tool_calls"] == 19
    for rec in plan.records:
        parse_index_record(rec, 1)


def test_interrupted_run(tmp_path: Path) -> None:
    repo = tmp_path / "Repo"
    _write_transcript(repo, "20260924-171330-runtime", final=False, footer=False)
    plan = plan_backfill(
        repo, machine_index=tmp_path / "machine.jsonl", now=time.time()
    )
    assert len(plan.records) == 2
    end = plan.records[1]
    assert end["status"] == "interrupted"
    assert end["tool_calls"] is None
    assert end["sent"] is None
    assert end["recv"] is None
    for rec in plan.records:
        parse_index_record(rec, 1)


def test_fanout_file_skipped(tmp_path: Path) -> None:
    repo = tmp_path / "Repo"
    runs = repo / ".claude" / "agent-runs"
    runs.mkdir(parents=True)
    (runs / "20260924-171330-fanout.md").write_text(
        "synthesis without header\nsome text\n", encoding="utf-8"
    )
    plan = plan_backfill(
        repo, machine_index=tmp_path / "machine.jsonl", now=time.time()
    )
    assert plan.records == ()
    assert plan.skipped == (
        ("20260924-171330-fanout", "not an engine transcript"),
    )


def test_skip_when_in_machine_index(tmp_path: Path) -> None:
    repo = tmp_path / "Repo"
    _write_transcript(repo, "20260924-171330-runtime")
    machine = tmp_path / "machine.jsonl"
    machine.write_text(
        json.dumps({"run_id": "20260924-171330-runtime", "event": "start"}) + "\n",
        encoding="utf-8",
    )
    plan = plan_backfill(repo, machine_index=machine, now=time.time())
    assert plan.records == ()
    assert plan.skipped == (("20260924-171330-runtime", "already indexed"),)


def test_skip_when_in_repo_index(tmp_path: Path) -> None:
    repo = tmp_path / "Repo"
    _write_transcript(repo, "20260924-171330-runtime")
    repo_index_path(repo).write_text(
        json.dumps({"run_id": "20260924-171330-runtime", "event": "start"}) + "\n",
        encoding="utf-8",
    )
    plan = plan_backfill(
        repo, machine_index=tmp_path / "machine.jsonl", now=time.time()
    )
    assert plan.records == ()
    assert plan.skipped == (("20260924-171330-runtime", "already indexed"),)


def test_idempotent(tmp_path: Path) -> None:
    repo = tmp_path / "Repo"
    _write_transcript(repo, "20260924-171330-runtime")
    machine = tmp_path / "machine.jsonl"
    plan = plan_backfill(repo, machine_index=machine, now=time.time())
    assert len(plan.records) == 2
    assert apply_backfill(repo, plan) == 2

    plan2 = plan_backfill(repo, machine_index=machine, now=time.time())
    assert plan2.records == ()


def test_recently_modified_skipped(tmp_path: Path) -> None:
    repo = tmp_path / "Repo"
    _write_transcript(repo, "20260924-171330-runtime", age_seconds=1.0)
    machine = tmp_path / "machine.jsonl"
    plan = plan_backfill(repo, machine_index=machine, now=time.time())
    assert plan.records == ()
    assert plan.skipped == (("20260924-171330-runtime", "recently modified"),)

    plan2 = plan_backfill(
        repo, machine_index=machine, now=time.time(), min_age_seconds=0
    )
    assert len(plan2.records) == 2


def test_dry_run_cli_writes_nothing(tmp_path: Path) -> None:
    repo = tmp_path / "Repo"
    _write_transcript(repo, "20260924-171330-runtime")
    state_dir = tmp_path / "state"
    machine = tmp_path / "machine.jsonl"
    exit_code = main(
        [
            "--state-dir",
            str(state_dir),
            "--machine-index",
            str(machine),
            "backfill-index",
            "--repo",
            str(repo),
            "--min-age-minutes",
            "0",
        ]
    )
    assert exit_code == 0
    assert not repo_index_path(repo).exists()


def test_apply_cli_writes(tmp_path: Path) -> None:
    repo = tmp_path / "Repo"
    _write_transcript(repo, "20260924-171330-runtime")
    state_dir = tmp_path / "state"
    machine = tmp_path / "machine.jsonl"
    exit_code = main(
        [
            "--state-dir",
            str(state_dir),
            "--machine-index",
            str(machine),
            "backfill-index",
            "--repo",
            str(repo),
            "--apply",
            "--min-age-minutes",
            "0",
        ]
    )
    assert exit_code == 0
    lines = repo_index_path(repo).read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    for line in lines:
        parse_index_record(json.loads(line), 1)


def test_end_to_end_sync_discovers(tmp_path: Path) -> None:
    from datetime import datetime, timezone

    from agent_control.ledger import highest_valid_revisions, load_ledger
    from agent_control.models import ProcessSnapshot
    from agent_control.paths import AppPaths
    from agent_control.projects import load_projects
    from agent_control.storage import StateLocks
    from agent_control.sync import Synchronizer

    class FakeProbe:
        def inspect(self, pid: int) -> ProcessSnapshot | None:
            return None

    paths = AppPaths.for_test(tmp_path)
    scan_root = tmp_path / "work"
    repo = scan_root / "Khattati"
    repo.mkdir(parents=True)
    _write_transcript(repo, "20260924-171330-runtime")
    paths.scan_roots_file.parent.mkdir(parents=True, exist_ok=True)
    paths.scan_roots_file.write_text(json.dumps([str(scan_root)]), encoding="utf-8")

    plan = plan_backfill(repo, machine_index=paths.machine_index, now=time.time())
    assert apply_backfill(repo, plan) == 2

    sync = Synchronizer(
        paths,
        StateLocks(),
        lambda: datetime(2026, 9, 30, 13, 0, tzinfo=timezone.utc),
        FakeProbe(),
    )
    sync.run()

    projects, _ = load_projects(paths.projects)
    project = next(
        p
        for p in projects.values()
        if os.path.normcase(p.root) == os.path.normcase(os.path.realpath(repo))
    )
    assert project.state == "discovered"

    records = highest_valid_revisions(load_ledger(paths.ledger)[0])
    assert records["20260924-171330-runtime"].status == "complete"

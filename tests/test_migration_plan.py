from __future__ import annotations

import json
import shutil
from datetime import datetime, timezone
from pathlib import Path

import pytest

from agent_control.migration import (
    MigrationSource,
    plan_migration,
    write_migration_report,
)
from agent_control.paths import AppPaths

FIXTURES = Path(__file__).parent / "fixtures" / "migration"


def fixed_clock() -> datetime:
    return datetime(2026, 9, 17, 13, 0, tzinfo=timezone.utc)


def _prepare(tmp_path: Path, *, projectb_pricing: dict | None = None) -> tuple[AppPaths, tuple[MigrationSource, ...]]:
    projecta_root = tmp_path / "Projecta"
    projectb_root = tmp_path / "Projectb"
    shutil.copytree(FIXTURES / "projecta", projecta_root)
    shutil.copytree(FIXTURES / "projectb", projectb_root)
    if projectb_pricing is not None:
        (projectb_root / "costs").mkdir(parents=True, exist_ok=True)
        (projectb_root / "costs" / "pricing.json").write_text(
            json.dumps(projectb_pricing), encoding="utf-8"
        )

    index = tmp_path / "machine-index.jsonl"
    template = (FIXTURES / "machine-index.jsonl").read_text(encoding="utf-8")
    index.write_text(
        template.replace("__PROJECTA_ROOT__", str(projecta_root).replace("\\", "\\\\")),
        encoding="utf-8",
    )

    paths = AppPaths.for_test(tmp_path)
    paths = AppPaths(
        repo_root=paths.repo_root,
        state_dir=paths.state_dir,
        machine_index=index,
        baseline_pricing=paths.baseline_pricing,
        web_dir=paths.web_dir,
    )
    sources = (
        MigrationSource(
            name="projecta",
            root=projecta_root,
            session_dir=projecta_root / ".claude" / "agent-runs",
        ),
        MigrationSource(
            name="projectb",
            root=projectb_root,
            session_dir=projectb_root / ".claude" / "agent-runs",
        ),
    )
    return paths, sources


def test_dry_run_report_does_not_create_runtime_accounting(tmp_path: Path) -> None:
    paths, sources = _prepare(tmp_path)
    report = plan_migration(paths, sources, clock=fixed_clock)
    paths.migration_reports.mkdir(parents=True, exist_ok=True)
    write_migration_report(paths.migration_reports / "review.json", report)
    assert not paths.projects.exists()
    assert not paths.ledger.exists()
    assert not paths.pricing_history.exists()


def test_report_counts_logical_runs_and_pricing_initialize(tmp_path: Path) -> None:
    paths, sources = _prepare(tmp_path)
    report = plan_migration(paths, sources, clock=fixed_clock)
    assert report.logical_runs >= 2
    assert report.machine_ids >= 1
    assert report.legacy_ids >= 1
    assert report.pricing_decision == "initialize"
    assert report.applicable is True


def test_pricing_conflict_makes_report_inapplicable(tmp_path: Path) -> None:
    conflicting = {
        "sample-model": {
            "input": 9.0,
            "output": 9.0,
            "cache_read": 9.0,
            "cache_write": 9.0,
        }
    }
    paths, sources = _prepare(tmp_path, projectb_pricing=conflicting)
    report = plan_migration(paths, sources, clock=fixed_clock)
    assert report.pricing_decision == "conflict"
    assert report.applicable is False


def test_matching_projectb_baseline_is_not_a_conflict(tmp_path: Path) -> None:
    matching = {
        "sample-model": {
            "input": 5.0,
            "output": 15.0,
            "cache_read": 0.5,
            "cache_write": 6.0,
        }
    }
    paths, sources = _prepare(tmp_path, projectb_pricing=matching)
    report = plan_migration(paths, sources, clock=fixed_clock)
    assert report.pricing_decision == "initialize"
    assert report.applicable is True


def test_report_id_is_stable_across_regeneration(tmp_path: Path) -> None:
    paths, sources = _prepare(tmp_path)
    first = plan_migration(paths, sources, clock=fixed_clock)
    second = plan_migration(paths, sources, clock=fixed_clock)
    assert first.report_id == second.report_id


def test_write_report_returns_content_hash(tmp_path: Path) -> None:
    paths, sources = _prepare(tmp_path)
    report = plan_migration(paths, sources, clock=fixed_clock)
    paths.migration_reports.mkdir(parents=True, exist_ok=True)
    target = paths.migration_reports / "review.json"
    digest = write_migration_report(target, report)
    import hashlib

    assert digest == hashlib.sha256(target.read_bytes()).hexdigest()

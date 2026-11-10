from __future__ import annotations

import hashlib
import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import pytest

from agent_control.ledger import highest_valid_revisions, load_ledger
from agent_control.migration import (
    MigrationSource,
    apply_migration,
    plan_migration,
    write_migration_report,
)
from agent_control.paths import AppPaths
from agent_control.pricing import load_baseline
from agent_control.projects import load_projects
from agent_control.storage import StateLocks

FIXTURES = Path(__file__).parent / "fixtures" / "migration"


def fixed_clock() -> datetime:
    return datetime(2026, 9, 17, 13, 0, tzinfo=timezone.utc)


@dataclass
class MigrationCase:
    paths: AppPaths
    sources: tuple[MigrationSource, ...]
    report_path: Path

    def apply(self):
        report = plan_migration(self.paths, self.sources, clock=fixed_clock)
        digest = write_migration_report(self.report_path, report)
        return apply_migration(
            self.paths,
            self.report_path,
            self.sources,
            confirmation_sha256=digest,
            locks=StateLocks(),
            clock=fixed_clock,
        )


@pytest.fixture
def migration_case(tmp_path: Path) -> MigrationCase:
    projecta = tmp_path / "Projecta"
    projectb = tmp_path / "Projectb"
    shutil.copytree(FIXTURES / "projecta", projecta)
    shutil.copytree(FIXTURES / "projectb", projectb)
    index = tmp_path / "machine-index.jsonl"
    template = (FIXTURES / "machine-index.jsonl").read_text(encoding="utf-8")
    index.write_text(
        template.replace("__PROJECTA_ROOT__", str(projecta).replace("\\", "\\\\")),
        encoding="utf-8",
    )
    base = AppPaths.for_test(tmp_path)
    paths = AppPaths(
        repo_root=base.repo_root,
        state_dir=base.state_dir,
        machine_index=index,
        baseline_pricing=base.state_dir / "baseline.json",
        web_dir=base.web_dir,
    )
    reports = paths.migration_reports
    reports.mkdir(parents=True, exist_ok=True)
    sources = (
        MigrationSource(
            name="projecta", root=projecta, session_dir=projecta / ".claude" / "agent-runs"
        ),
        MigrationSource(
            name="projectb", root=projectb, session_dir=projectb / ".claude" / "agent-runs"
        ),
    )
    return MigrationCase(paths=paths, sources=sources, report_path=reports / "review.json")


def test_apply_creates_projects_ledger_and_baseline(migration_case: MigrationCase) -> None:
    result = migration_case.apply()
    assert result.appended_revisions > 0
    assert result.project_events >= 2
    assert result.baseline_action == "initialized"
    projects, _ = load_projects(migration_case.paths.projects)
    assert len(projects) >= 2
    ledger = highest_valid_revisions(load_ledger(migration_case.paths.ledger)[0])
    assert "machine-a-run" in ledger
    baseline = load_baseline(migration_case.paths.baseline_pricing)
    assert "sample-model" in baseline


def test_second_apply_is_noop_and_never_creates_pricing_history(
    migration_case: MigrationCase,
) -> None:
    first = migration_case.apply()
    second = migration_case.apply()
    assert first.appended_revisions > 0
    assert second.appended_revisions == 0
    assert second.baseline_action == "unchanged"
    assert not migration_case.paths.pricing_history.exists()


def test_wrong_confirmation_hash_is_rejected(migration_case: MigrationCase) -> None:
    report = plan_migration(
        migration_case.paths, migration_case.sources, clock=fixed_clock
    )
    write_migration_report(migration_case.report_path, report)
    with pytest.raises(ValueError, match="confirmation"):
        apply_migration(
            migration_case.paths,
            migration_case.report_path,
            migration_case.sources,
            confirmation_sha256="0" * 64,
            locks=StateLocks(),
            clock=fixed_clock,
        )
    assert not migration_case.paths.ledger.exists()


def test_changed_source_after_report_refuses_apply(migration_case: MigrationCase) -> None:
    report = plan_migration(
        migration_case.paths, migration_case.sources, clock=fixed_clock
    )
    digest = write_migration_report(migration_case.report_path, report)
    # Mutate a source after the report was produced.
    ledger = migration_case.sources[0].root / "costs" / "ledger.jsonl"
    with ledger.open("a", encoding="utf-8") as handle:
        handle.write(
            '{"run_id":"new","status":"complete","model":"sample-model",'
            '"timestamp":"2026-09-16T15:00:00+00:00","label":"x","tokens_sent":1,'
            '"tokens_returned":1,"cache_read":0,"cache_write":0,"source":"delegated",'
            '"filename":"20260916-150000-x.md"}\n'
        )
    with pytest.raises(ValueError, match="changed"):
        apply_migration(
            migration_case.paths,
            migration_case.report_path,
            migration_case.sources,
            confirmation_sha256=digest,
            locks=StateLocks(),
            clock=fixed_clock,
        )


def test_apply_does_not_modify_source_artifacts(migration_case: MigrationCase) -> None:
    ledger = migration_case.sources[0].root / "costs" / "ledger.jsonl"
    before = hashlib.sha256(ledger.read_bytes()).hexdigest()
    migration_case.apply()
    after = hashlib.sha256(ledger.read_bytes()).hexdigest()
    assert before == after

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path

import pytest

from agent_control.migration import (
    MigrationEvidence,
    MigrationSource,
    _dedupe_by_run_id,
    build_source_manifest,
    collect_migration_evidence,
    legacy_run_id,
)
from agent_control.models import RunCandidate, UsageEvidence, UsageTotals

FIXTURES = Path(__file__).parent / "fixtures" / "migration"
_FORBIDDEN_KEYS = (
    "prompt",
    "assistant",
    "tool_result",
    "content",
    "credential",
    "authorization",
    "response_body",
    "api_key",
)


@dataclass
class MigrationFixture:
    sources: tuple[MigrationSource, ...]
    index: Path
    projecta: MigrationSource
    projectb: MigrationSource


def _stable_project_id(name: str) -> str:
    return f"project-{name}"


@pytest.fixture
def migration_fixture(tmp_path: Path) -> MigrationFixture:
    projecta_root = tmp_path / "Projecta"
    projectb_root = tmp_path / "Projectb"
    shutil.copytree(FIXTURES / "projecta", projecta_root)
    shutil.copytree(FIXTURES / "projectb", projectb_root)

    index = tmp_path / "machine-index.jsonl"
    template = (FIXTURES / "machine-index.jsonl").read_text(encoding="utf-8")
    index.write_text(
        template.replace("__PROJECTA_ROOT__", str(projecta_root).replace("\\", "\\\\")),
        encoding="utf-8",
    )

    projecta = MigrationSource(
        name="projecta",
        root=projecta_root,
        session_dir=projecta_root / ".claude" / "agent-runs",
    )
    projecta = MigrationSource(
        name="projecta", root=projecta_root, session_dir=projecta.session_dir
    )
    projectb = MigrationSource(
        name="projectb",
        root=projectb_root,
        session_dir=projectb_root / ".claude" / "agent-runs",
    )
    return MigrationFixture(
        sources=(projecta, projectb), index=index, projecta=projecta, projectb=projectb
    )


def test_same_relative_filename_in_two_projects_has_distinct_legacy_ids(
    migration_fixture: MigrationFixture,
) -> None:
    relative = Path(".claude/agent-runs/20260916-120000-sample.md")
    projecta = migration_fixture.projecta
    projectb = migration_fixture.projectb
    projecta_id = legacy_run_id(
        _stable_project_id("projecta"), projecta.root, projecta.root / relative
    )
    projectb_id = legacy_run_id(_stable_project_id("projectb"), projectb.root, projectb.root / relative)
    assert projecta_id != projectb_id
    assert projecta_id.startswith("legacy:")


def test_legacy_id_is_case_and_separator_insensitive(
    migration_fixture: MigrationFixture,
) -> None:
    projecta = migration_fixture.projecta
    backslash = projecta.root / ".claude" / "agent-runs" / "20260916-120000-sample.md"
    forward = Path(str(backslash).replace("\\", "/"))
    assert legacy_run_id("p", projecta.root, backslash) == legacy_run_id(
        "p", projecta.root, forward
    )


def test_machine_id_is_joined_only_by_canonical_artifact_path(
    migration_fixture: MigrationFixture,
) -> None:
    evidence, diagnostics = collect_migration_evidence(
        migration_fixture.sources, migration_fixture.index
    )
    linked = [item for item in evidence if item.artifact_match]
    assert linked
    assert linked[0].run_id == "machine-a-run"


def test_legacy_run_without_machine_id_uses_deterministic_id(
    migration_fixture: MigrationFixture,
) -> None:
    evidence, _ = collect_migration_evidence(
        migration_fixture.sources, migration_fixture.index
    )
    legacy = [item for item in evidence if item.run_id.startswith("legacy:")]
    assert legacy


def test_manifest_records_every_input(migration_fixture: MigrationFixture) -> None:
    manifest = build_source_manifest(
        migration_fixture.sources, migration_fixture.index
    )
    assert manifest
    for entry in manifest:
        assert entry.size >= 0
        assert len(entry.sha256) == 64
        assert entry.mtime_ns > 0


def test_no_forbidden_keys_appear_in_evidence(
    migration_fixture: MigrationFixture,
) -> None:
    evidence, _ = collect_migration_evidence(
        migration_fixture.sources, migration_fixture.index
    )
    for item in evidence:
        serialized = repr(item.candidate).casefold()
        for key in _FORBIDDEN_KEYS:
            assert f'"{key}"' not in serialized


def _evidence(run_id: str, *, rank: int, tokens_sent: int | None) -> MigrationEvidence:
    totals = UsageTotals(
        tokens_sent=tokens_sent,
        tokens_returned=tokens_sent,
        cache_read=0,
        cache_write=0,
        sent_includes_cache_read=None,
        response_count=1,
        tokens_sent_unknown_responses=0 if tokens_sent is not None else 1,
        tokens_returned_unknown_responses=0 if tokens_sent is not None else 1,
        cache_read_unknown_responses=0,
        cache_write_unknown_responses=0,
    )
    source = "usage_journal" if tokens_sent is not None else "missing"
    candidate = RunCandidate(
        run_id=run_id,
        project_id="p",
        timestamp="2026-09-16T12:00:00+00:00",
        ended_at=None,
        status="complete",
        label="demo",
        model="model",
        effort=None,
        tool_mode=None,
        write_mode=None,
        tool_calls=None,
        refused=None,
        usage=UsageEvidence(totals=totals, source=source, artifact=None, diagnostics=()),
        transcript_artifact=None,
        provenance=("machine_index",),
        lifecycle_inferred=False,
    )
    return MigrationEvidence(
        run_id=run_id,
        project_id="p",
        artifact_relative_path="a.md",
        candidate=candidate,
        source_rank=rank,
        artifact_match=rank == 0,
    )


def test_dedupe_prefers_candidate_with_usage_over_bare_lifecycle() -> None:
    # A machine-index run with no readable artifacts (better rank, no usage) must
    # not shadow the repo ledger row that actually carries token counts.
    machine = _evidence("run", rank=0, tokens_sent=None)
    ledger = _evidence("run", rank=1, tokens_sent=1234)
    deduped, duplicates = _dedupe_by_run_id([machine, ledger])
    assert duplicates == 1
    assert len(deduped) == 1
    assert deduped[0].candidate.usage.totals.tokens_sent == 1234


def test_dedupe_breaks_ties_by_rank_when_usage_equal() -> None:
    high = _evidence("run", rank=0, tokens_sent=1000)
    low = _evidence("run", rank=1, tokens_sent=1000)
    deduped, _ = _dedupe_by_run_id([low, high])
    assert deduped[0].source_rank == 0

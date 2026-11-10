from __future__ import annotations

import hashlib
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Literal

from .ledger import validate_run_record
from .models import (
    CostClassification,
    Diagnostic,
    RunCandidate,
    UsageEvidence,
    UsageSource,
    UsageTotals,
)
from .paths import AppPaths
from .pricing import calculate_cost, validate_rate_set
from .projects import canonical_root
from .run_index import StartEvent, parse_index_record, reduce_index_events
from .storage import (
    canonical_json_hash,
    normalize_optional_utc,
    normalize_utc,
    read_jsonl,
    strict_json_loads,
)
from .usage import select_usage_evidence, validate_counter

_AGENT_RUNS = Path(".claude") / "agent-runs"


@dataclass(frozen=True)
class MigrationSource:
    name: str
    root: Path
    session_dir: Path | None


@dataclass(frozen=True)
class SourceManifestEntry:
    path: str
    size: int
    mtime_ns: int
    sha256: str


@dataclass(frozen=True)
class MigrationEvidence:
    run_id: str
    project_id: str
    artifact_relative_path: str
    candidate: RunCandidate
    source_rank: int
    artifact_match: bool


def _canonical_relative(root: Path, artifact: Path) -> str:
    root_resolved = Path(canonical_root(root)[0])
    artifact_resolved = Path(canonical_root(artifact)[0])
    relative = artifact_resolved.relative_to(root_resolved)
    return str(relative).replace("/", "\\").casefold()


def legacy_run_id(project_id: str, project_root: Path, artifact: Path) -> str:
    canonical = _canonical_relative(Path(project_root), Path(artifact))
    digest = hashlib.sha256(
        f"{project_id}\x1f{canonical}".encode("utf-8")
    ).hexdigest()
    return "legacy:" + digest


def _provisional_project_id(source: MigrationSource) -> str:
    root_key = canonical_root(source.root)[1]
    digest = hashlib.sha256(root_key.encode("utf-8")).hexdigest()[:16]
    return f"migration:{digest}"


def _sha256_file(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def _manifest_entry(path: Path) -> SourceManifestEntry:
    stat = path.stat()
    return SourceManifestEntry(
        path=str(path),
        size=stat.st_size,
        mtime_ns=stat.st_mtime_ns,
        sha256=_sha256_file(path),
    )


def _iter_source_files(source: MigrationSource) -> list[Path]:
    files: list[Path] = []
    for relative in (
        Path("costs") / "ledger.jsonl",
        Path("costs") / "pricing.json",
        Path("runs") / "agent-registry.jsonl",
    ):
        candidate = source.root / relative
        if candidate.is_file():
            files.append(candidate)
    if source.session_dir is not None and source.session_dir.is_dir():
        for child in sorted(source.session_dir.iterdir()):
            if child.is_file():
                files.append(child)
    return files


def build_source_manifest(
    sources: Sequence[MigrationSource], index: Path
) -> tuple[SourceManifestEntry, ...]:
    entries: list[SourceManifestEntry] = []
    if index.is_file():
        entries.append(_manifest_entry(index))
    for source in sources:
        for path in _iter_source_files(source):
            entries.append(_manifest_entry(path))
    return tuple(entries)


def _machine_starts_by_artifact(
    index: Path, sources: Sequence[MigrationSource]
) -> tuple[dict[tuple[str, str], StartEvent], dict[str, object]]:
    rows, _ = read_jsonl(index, source=str(index))
    events = []
    for line, row in enumerate(rows, start=1):
        try:
            events.append(parse_index_record(row, line))
        except ValueError:
            continue
    pairs, _ = reduce_index_events(events)
    by_artifact: dict[tuple[str, str], StartEvent] = {}
    for run_id, (start, _end) in pairs.items():
        if start is None:
            continue
        artifact = Path(start.transcript)
        for source in sources:
            try:
                relative = _canonical_relative(source.root, artifact)
            except ValueError:
                continue
            by_artifact[(source.name, relative)] = start
            break
    return by_artifact, pairs


def _ledger_rows(source: MigrationSource) -> list[Mapping[str, object]]:
    ledger = source.root / "costs" / "ledger.jsonl"
    rows, _ = read_jsonl(ledger, source=str(ledger))
    return rows


def _usage_from_ledger(row: Mapping[str, object]) -> UsageEvidence:
    tokens_sent = validate_counter(row.get("tokens_sent"), "tokens_sent")
    tokens_returned = validate_counter(row.get("tokens_returned"), "tokens_returned")
    cache_read = validate_counter(row.get("cache_read"), "cache_read")
    cache_write = validate_counter(row.get("cache_write"), "cache_write")
    totals = UsageTotals(
        tokens_sent=tokens_sent,
        tokens_returned=tokens_returned,
        cache_read=cache_read,
        cache_write=cache_write,
        sent_includes_cache_read=None,
        response_count=1,
        tokens_sent_unknown_responses=0 if tokens_sent is not None else 1,
        tokens_returned_unknown_responses=0 if tokens_returned is not None else 1,
        cache_read_unknown_responses=0 if cache_read is not None else 1,
        cache_write_unknown_responses=0 if cache_write is not None else 1,
    )
    return UsageEvidence(totals=totals, source="messages", artifact=None, diagnostics=())


def _artifact_siblings(session_dir: Path | None, stem: str) -> tuple[Path | None, Path | None, Path | None]:
    if session_dir is None:
        return None, None, None
    transcript = session_dir / f"{stem}.md"
    messages = session_dir / f"{stem}.messages.jsonl"
    journal = session_dir / f"{stem}.usage.jsonl"
    return (
        transcript if transcript.is_file() else None,
        messages if messages.is_file() else None,
        journal if journal.is_file() else None,
    )


def _candidate_from_row(
    row: Mapping[str, object],
    source: MigrationSource,
    project_id: str,
    run_id: str,
    provenance: tuple[str, ...],
) -> tuple[RunCandidate, str]:
    filename = str(row.get("filename") or "")
    stem = Path(filename).stem
    transcript, messages, journal = _artifact_siblings(source.session_dir, stem)
    usage = select_usage_evidence(transcript, messages, journal)
    if usage.source == "missing":
        usage = _usage_from_ledger(row)
    artifact = source.root / _AGENT_RUNS / filename if filename else source.root
    relative = _canonical_relative(source.root, artifact) if filename else ""
    status = str(row.get("status") or "complete")
    candidate = RunCandidate(
        run_id=run_id,
        project_id=project_id,
        timestamp=normalize_utc(str(row.get("timestamp"))),
        ended_at=normalize_optional_utc(row.get("ended_at") if isinstance(row.get("ended_at"), str) else None),
        status=status if status in {"complete", "failed", "interrupted", "running"} else "complete",  # type: ignore[arg-type]
        label=str(row.get("label") or "unknown"),
        model=str(row.get("model") or "unknown"),
        effort=row.get("effort") if isinstance(row.get("effort"), str) else None,
        tool_mode=row.get("tool_mode") if isinstance(row.get("tool_mode"), str) else None,
        write_mode=row.get("write_mode") if isinstance(row.get("write_mode"), str) else None,
        tool_calls=validate_counter(row.get("tool_calls"), "tool_calls"),
        refused=validate_counter(row.get("refused"), "refused"),
        usage=usage,
        transcript_artifact=str(transcript) if transcript is not None else None,
        provenance=provenance,
        lifecycle_inferred=False,
    )
    return candidate, relative


def collect_migration_evidence(
    sources: Sequence[MigrationSource], index: Path
) -> tuple[list[MigrationEvidence], tuple[Diagnostic, ...]]:
    by_artifact, _pairs = _machine_starts_by_artifact(index, sources)
    evidence: list[MigrationEvidence] = []
    diagnostics: list[Diagnostic] = []
    seen: set[tuple[str, str]] = set()

    for source in sources:
        project_id = _provisional_project_id(source)
        for row in _ledger_rows(source):
            filename = str(row.get("filename") or "")
            if not filename:
                diagnostics.append(
                    Diagnostic(
                        "ledger_row_missing_filename",
                        "repo ledger row lacks an artifact filename",
                        source=source.name,
                    )
                )
                continue
            artifact = source.root / _AGENT_RUNS / filename
            relative = _canonical_relative(source.root, artifact)
            machine_start = by_artifact.get((source.name, relative))
            if machine_start is not None:
                run_id = machine_start.run_id
                provenance = ("machine_index", f"{source.name}_ledger")
                artifact_match = True
            else:
                run_id = legacy_run_id(project_id, source.root, artifact)
                provenance = (f"{source.name}_ledger",)
                artifact_match = False
            candidate, rel = _candidate_from_row(
                row, source, project_id, run_id, provenance
            )
            seen.add((source.name, relative))
            evidence.append(
                MigrationEvidence(
                    run_id=run_id,
                    project_id=project_id,
                    artifact_relative_path=rel,
                    candidate=candidate,
                    source_rank=0 if artifact_match else 1,
                    artifact_match=artifact_match,
                )
            )

        # Transcripts with no ledger row (e.g. Projectb) become legacy evidence.
        if source.session_dir is not None and source.session_dir.is_dir():
            for transcript in sorted(source.session_dir.glob("*.md")):
                relative = _canonical_relative(source.root, transcript)
                if (source.name, relative) in seen:
                    continue
                machine_start = by_artifact.get((source.name, relative))
                run_id = (
                    machine_start.run_id
                    if machine_start is not None
                    else legacy_run_id(project_id, source.root, transcript)
                )
                usage = select_usage_evidence(
                    transcript,
                    transcript.with_suffix(".messages.jsonl"),
                    transcript.with_suffix(".usage.jsonl"),
                )
                # When the machine index already identified this run, carry its
                # real identity forward. A transcript scan cannot see the model,
                # so fabricating "unknown" here would clobber good sync data.
                if machine_start is not None:
                    model = machine_start.model
                    label = machine_start.label
                    effort = machine_start.effort
                    tool_mode = machine_start.tool_mode
                    write_mode = machine_start.write_mode
                    timestamp = machine_start.ts
                    provenance = (f"{source.name}_transcript", "machine_index")
                else:
                    model = "unknown"
                    label = transcript.stem
                    effort = None
                    tool_mode = None
                    write_mode = None
                    timestamp = normalize_utc("2026-01-01T00:00:00+00:00")
                    provenance = (f"{source.name}_transcript",)
                candidate = RunCandidate(
                    run_id=run_id,
                    project_id=project_id,
                    timestamp=timestamp,
                    ended_at=None,
                    status="complete",
                    label=label,
                    model=model,
                    effort=effort,
                    tool_mode=tool_mode,
                    write_mode=write_mode,
                    tool_calls=None,
                    refused=None,
                    usage=usage,
                    transcript_artifact=str(transcript),
                    provenance=provenance,
                    lifecycle_inferred=True,
                )
                evidence.append(
                    MigrationEvidence(
                        run_id=run_id,
                        project_id=project_id,
                        artifact_relative_path=relative,
                        candidate=candidate,
                        source_rank=2,
                        artifact_match=machine_start is not None,
                    )
                )
    return evidence, tuple(diagnostics)


@dataclass(frozen=True)
class MigrationReport:
    schema: int
    report_id: str
    generated_at: str
    manifest: tuple[SourceManifestEntry, ...]
    input_counts: Mapping[str, int]
    logical_runs: int
    machine_ids: int
    legacy_ids: int
    duplicates: int
    conflicts: int
    inaccessible_artifacts: int
    malformed_records: int
    unsupported_records: int
    usage_sources: Mapping[str, int]
    coverage: Mapping[str, int]
    project_roots: tuple[str, ...]
    planned_candidates: tuple[RunCandidate, ...]
    pricing_decision: Literal["initialize", "unchanged", "conflict"]
    baseline_catalog: Mapping[str, object] | None
    projected_normal_total: float
    projected_excluded_total: float
    diagnostics: tuple[Diagnostic, ...]
    applicable: bool


def _read_repo_pricing(source: MigrationSource) -> dict[str, object]:
    path = source.root / "costs" / "pricing.json"
    if not path.is_file():
        return {}
    value = strict_json_loads(path.read_bytes())
    if not isinstance(value, Mapping):
        return {}
    models: dict[str, object] = {}
    for model, rates in value.items():
        if not isinstance(model, str) or model.startswith("_") or model == "<synthetic>":
            continue
        if not isinstance(rates, Mapping):
            continue
        if any(rates.get(key) is None for key in ("input", "output", "cache_read", "cache_write")):
            continue
        try:
            validate_rate_set(rates, f"{source.name}.{model}")
        except ValueError:
            continue
        models[model] = {
            "input": float(rates["input"]),
            "output": float(rates["output"]),
            "cache_read": float(rates["cache_read"]),
            "cache_write": float(rates["cache_write"]),
        }
    return models


def _reconcile_pricing(
    sources: Sequence[MigrationSource],
) -> tuple[Literal["initialize", "unchanged", "conflict"], dict[str, object], tuple[Diagnostic, ...]]:
    combined: dict[str, object] = {}
    diagnostics: list[Diagnostic] = []
    for source in sources:
        for model, rates in _read_repo_pricing(source).items():
            if model in combined and combined[model] != rates:
                diagnostics.append(
                    Diagnostic(
                        "pricing_baseline_conflict",
                        f"conflicting baseline rates for {model}",
                        source=source.name,
                    )
                )
                return "conflict", combined, tuple(diagnostics)
            combined[model] = rates
    if not combined:
        return "unchanged", combined, tuple(diagnostics)
    return "initialize", combined, tuple(diagnostics)


def _has_usage(item: MigrationEvidence) -> bool:
    totals = item.candidate.usage.totals
    return totals.tokens_sent is not None or totals.tokens_returned is not None


def _dedupe_priority(item: MigrationEvidence) -> tuple[int, int]:
    # Prefer a candidate that actually carries usage, then the strongest source
    # rank (lower is better). A bare machine-index lifecycle record with no
    # readable artifacts must never shadow a repo ledger row that has tokens.
    return (0 if _has_usage(item) else 1, item.source_rank)


def _dedupe_by_run_id(
    evidence: Sequence[MigrationEvidence],
) -> tuple[list[MigrationEvidence], int]:
    best: dict[str, MigrationEvidence] = {}
    duplicates = 0
    for item in evidence:
        existing = best.get(item.run_id)
        if existing is None:
            best[item.run_id] = item
        else:
            duplicates += 1
            if _dedupe_priority(item) < _dedupe_priority(existing):
                best[item.run_id] = item
    return list(best.values()), duplicates


def _candidate_to_record(candidate: RunCandidate) -> object:
    totals = candidate.usage.totals
    return validate_run_record(
        {
            "schema": 1,
            "run_id": candidate.run_id,
            "revision": 1,
            "project_id": candidate.project_id,
            "timestamp": candidate.timestamp,
            "ended_at": candidate.ended_at,
            "status": candidate.status,
            "label": candidate.label,
            "model": candidate.model,
            "effort": candidate.effort,
            "tool_mode": candidate.tool_mode,
            "write_mode": candidate.write_mode,
            "tool_calls": candidate.tool_calls,
            "refused": candidate.refused,
            "tokens_sent": totals.tokens_sent,
            "tokens_returned": totals.tokens_returned,
            "cache_read": totals.cache_read,
            "cache_write": totals.cache_write,
            "sent_includes_cache_read": totals.sent_includes_cache_read,
            "tokens_sent_unknown_responses": totals.tokens_sent_unknown_responses,
            "tokens_returned_unknown_responses": totals.tokens_returned_unknown_responses,
            "cache_read_unknown_responses": totals.cache_read_unknown_responses,
            "cache_write_unknown_responses": totals.cache_write_unknown_responses,
            "usage_source": candidate.usage.source,
            "usage_artifact": candidate.usage.artifact,
            "transcript_artifact": candidate.transcript_artifact,
            "provenance": list(candidate.provenance),
            "lifecycle_inferred": candidate.lifecycle_inferred,
            "observed_at": candidate.timestamp,
        },
        "migration-candidate",
    )


def plan_migration(
    paths: AppPaths,
    sources: Sequence[MigrationSource],
    *,
    clock: Callable[[], datetime],
) -> MigrationReport:
    manifest = build_source_manifest(sources, paths.machine_index)
    evidence, evidence_diagnostics = collect_migration_evidence(
        sources, paths.machine_index
    )
    deduped, duplicates = _dedupe_by_run_id(evidence)

    machine_ids = sum(1 for item in deduped if not item.run_id.startswith("legacy:"))
    legacy_ids = sum(1 for item in deduped if item.run_id.startswith("legacy:"))

    usage_sources: dict[str, int] = {}
    coverage: dict[str, int] = {}
    baseline_map: dict[str, object]
    pricing_decision, baseline_map, pricing_diagnostics = _reconcile_pricing(sources)

    baseline_rates = {
        model: validate_rate_set(rates, model) for model, rates in baseline_map.items()
    }

    candidates = tuple(item.candidate for item in deduped)
    records = [_candidate_to_record(candidate) for candidate in candidates]
    normal_total = 0.0
    for record in records:
        source_name = record.usage_source  # type: ignore[attr-defined]
        usage_sources[source_name] = usage_sources.get(source_name, 0) + 1
        cost = calculate_cost(record, baseline_rates, [])  # type: ignore[arg-type]
        coverage[cost.classification] = coverage.get(cost.classification, 0) + 1
        if cost.amount is not None:
            normal_total += cost.amount

    diagnostics = tuple([*evidence_diagnostics, *pricing_diagnostics])
    inaccessible = sum(1 for d in diagnostics if d.code == "inaccessible_artifact")
    conflicts = sum(1 for d in diagnostics if d.code.endswith("_conflict"))

    project_roots = tuple(
        sorted({canonical_root(source.root)[0] for source in sources})
    )
    input_counts = {
        "sources": len(sources),
        "manifest_entries": len(manifest),
        "evidence_rows": len(evidence),
    }

    applicable = pricing_decision != "conflict" and conflicts == 0

    unsigned = {
        "schema": 1,
        "manifest": [asdict(entry) for entry in manifest],
        "logical_runs": len(deduped),
        "machine_ids": machine_ids,
        "legacy_ids": legacy_ids,
        "pricing_decision": pricing_decision,
        "baseline_catalog": baseline_map or None,
        "project_roots": list(project_roots),
        "planned_run_ids": sorted(item.run_id for item in deduped),
    }
    report_id = canonical_json_hash(unsigned)

    return MigrationReport(
        schema=1,
        report_id=report_id,
        generated_at=normalize_utc(clock().isoformat()),
        manifest=manifest,
        input_counts=input_counts,
        logical_runs=len(deduped),
        machine_ids=machine_ids,
        legacy_ids=legacy_ids,
        duplicates=duplicates,
        conflicts=conflicts,
        inaccessible_artifacts=inaccessible,
        malformed_records=0,
        unsupported_records=0,
        usage_sources=usage_sources,
        coverage=coverage,
        project_roots=project_roots,
        planned_candidates=candidates,
        pricing_decision=pricing_decision,
        baseline_catalog=baseline_map or None,
        projected_normal_total=normal_total,
        projected_excluded_total=0.0,
        diagnostics=diagnostics,
        applicable=applicable,
    )


def write_migration_report(path: Path, report: MigrationReport) -> str:
    import json

    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(
        asdict(report), sort_keys=True, indent=2, allow_nan=False
    ) + "\n"
    path.write_text(payload, encoding="utf-8")
    return hashlib.sha256(path.read_bytes()).hexdigest()


@dataclass(frozen=True)
class MigrationApplyResult:
    report_id: str
    project_events: int
    appended_revisions: int
    unchanged_runs: int
    baseline_action: Literal["initialized", "unchanged"]
    diagnostics: tuple[Diagnostic, ...]


def load_migration_report(path: Path) -> MigrationReport:
    value = strict_json_loads(path.read_bytes())
    if not isinstance(value, Mapping):
        raise ValueError("migration report must be an object")
    return MigrationReport(
        schema=int(value["schema"]),
        report_id=str(value["report_id"]),
        generated_at=str(value["generated_at"]),
        manifest=tuple(
            SourceManifestEntry(**entry) for entry in value.get("manifest", [])
        ),
        input_counts=dict(value.get("input_counts", {})),
        logical_runs=int(value["logical_runs"]),
        machine_ids=int(value["machine_ids"]),
        legacy_ids=int(value["legacy_ids"]),
        duplicates=int(value.get("duplicates", 0)),
        conflicts=int(value.get("conflicts", 0)),
        inaccessible_artifacts=int(value.get("inaccessible_artifacts", 0)),
        malformed_records=int(value.get("malformed_records", 0)),
        unsupported_records=int(value.get("unsupported_records", 0)),
        usage_sources=dict(value.get("usage_sources", {})),
        coverage=dict(value.get("coverage", {})),
        project_roots=tuple(value.get("project_roots", [])),
        planned_candidates=(),
        pricing_decision=value["pricing_decision"],
        baseline_catalog=value.get("baseline_catalog"),
        projected_normal_total=float(value.get("projected_normal_total", 0.0)),
        projected_excluded_total=float(value.get("projected_excluded_total", 0.0)),
        diagnostics=(),
        applicable=bool(value["applicable"]),
    )


def verify_source_manifest(
    report: MigrationReport,
    sources: Sequence[MigrationSource],
    index: Path,
) -> None:
    current = {entry.path: entry for entry in build_source_manifest(sources, index)}
    recorded = {entry.path: entry for entry in report.manifest}
    if current != recorded:
        raise ValueError("migration inputs changed since the report was produced")


def _initialize_baseline(
    path: Path, baseline_catalog: Mapping[str, object], locks
) -> Literal["initialized", "unchanged"]:
    from .storage import atomic_write_json

    if path.exists():
        existing = strict_json_loads(path.read_bytes())
        if isinstance(existing, Mapping) and existing.get("initialized"):
            return "unchanged"
    if not baseline_catalog:
        return "unchanged"
    atomic_write_json(
        path,
        {
            "schema": 1,
            "currency": "USD",
            "unit_tokens": 1_000_000,
            "initialized": True,
            "models": dict(baseline_catalog),
        },
        locks.write,
    )
    return "initialized"


def apply_migration(
    paths: AppPaths,
    report_path: Path,
    sources: Sequence[MigrationSource],
    *,
    confirmation_sha256: str,
    locks,
    clock: Callable[[], datetime],
) -> MigrationApplyResult:
    from .ledger import sync_candidate
    from .projects import ProjectRegistry

    with locks.write:
        stored = load_migration_report(report_path)
        actual = hashlib.sha256(report_path.read_bytes()).hexdigest()
        if actual != confirmation_sha256:
            raise ValueError("migration report confirmation mismatch")
        verify_source_manifest(stored, sources, paths.machine_index)
        fresh = plan_migration(paths, sources, clock=clock)
        if fresh.report_id != stored.report_id:
            raise ValueError("migration inputs or plan changed")
        if not fresh.applicable:
            raise ValueError("migration report is not applicable")

        baseline_action = _initialize_baseline(
            paths.baseline_pricing, fresh.baseline_catalog or {}, locks
        )

        registry = ProjectRegistry(paths.projects, locks, clock)
        project_ids: dict[str, str] = {}
        project_events = 0
        for source in sources:
            project, created = registry.discover(source.root)
            project_ids[_provisional_project_id(source)] = project.project_id
            if created:
                project_events += 1

        observed_at = normalize_utc(clock().isoformat())
        appended = 0
        unchanged = 0
        for candidate in fresh.planned_candidates:
            mapped_project = project_ids.get(
                candidate.project_id, candidate.project_id
            )
            resolved = _remap_project(candidate, mapped_project)
            result = sync_candidate(
                paths.ledger, resolved, observed_at=observed_at, locks=locks
            )
            if result.appended:
                appended += 1
            else:
                unchanged += 1

        return MigrationApplyResult(
            report_id=fresh.report_id,
            project_events=project_events,
            appended_revisions=appended,
            unchanged_runs=unchanged,
            baseline_action=baseline_action,
            diagnostics=fresh.diagnostics,
        )


def _remap_project(candidate: RunCandidate, project_id: str) -> RunCandidate:
    from dataclasses import replace

    return replace(candidate, project_id=project_id)


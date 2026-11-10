from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from .ledger import (
    _TERMINAL_STATUSES,
    highest_valid_revisions,
    load_ledger,
    sync_candidate,
)
from .liveness import classify_liveness
from .models import (
    Diagnostic,
    IndexCheckpoint,
    Liveness,
    ProcessProbe,
    RunCandidate,
    safe_stop_reason,
    SyncReport,
    UsageEvidence,
    UsageTotals,
)
from .discovery import load_scan_roots, repo_index_files
from .paths import AppPaths
from .projects import ProjectRegistry, load_projects
from .run_index import (
    EndEvent,
    StartEvent,
    parse_index_record,
    read_index_batch,
    reduce_index_events,
    validate_artifact_paths,
)
from .storage import (
    StateLocks,
    atomic_write_json,
    normalize_utc,
    strict_json_loads,
)
from .usage import select_usage_evidence

if TYPE_CHECKING:
    from .tool_import import ImportReport


@dataclass
class SyncState:
    checkpoint: IndexCheckpoint = field(
        default_factory=lambda: IndexCheckpoint(0, None)
    )
    open_starts: dict[str, StartEvent] = field(default_factory=dict)
    unmatched_ends: dict[str, EndEvent] = field(default_factory=dict)
    retry_run_ids: list[str] = field(default_factory=list)
    # Per repo index file, keyed by normcase'd absolute path string.
    repo_checkpoints: dict[str, IndexCheckpoint] = field(default_factory=dict)


def _checkpoint_to_json(checkpoint: IndexCheckpoint) -> dict[str, object]:
    identity = checkpoint.identity
    return {
        "offset": checkpoint.offset,
        "identity": (
            None
            if identity is None
            else {
                "device": identity.device,
                "inode": identity.inode,
                "prefix_length": identity.prefix_length,
                "prefix_sha256": identity.prefix_sha256,
            }
        ),
    }


def _checkpoint_from_json(value: object) -> IndexCheckpoint:
    from .models import FileIdentity

    if not isinstance(value, dict):
        return IndexCheckpoint(0, None)
    identity_value = value.get("identity")
    identity = None
    if isinstance(identity_value, dict):
        identity = FileIdentity(
            device=int(identity_value["device"]),
            inode=int(identity_value["inode"]),
            prefix_length=int(identity_value["prefix_length"]),
            prefix_sha256=str(identity_value["prefix_sha256"]),
        )
    return IndexCheckpoint(offset=int(value.get("offset", 0)), identity=identity)


def load_sync_state(path: Path) -> SyncState:
    if not path.exists():
        return SyncState()
    value = strict_json_loads(path.read_bytes())
    if not isinstance(value, dict):
        return SyncState()
    state = SyncState(checkpoint=_checkpoint_from_json(value.get("checkpoint")))
    for run_id, raw in (value.get("open_starts") or {}).items():
        event = parse_index_record(raw, 0)
        if isinstance(event, StartEvent):
            state.open_starts[str(run_id)] = event
    for run_id, raw in (value.get("unmatched_ends") or {}).items():
        event = parse_index_record(raw, 0)
        if isinstance(event, EndEvent):
            state.unmatched_ends[str(run_id)] = event
    retry = value.get("retry_run_ids") or []
    state.retry_run_ids = [str(item) for item in retry]
    repo_checkpoints = value.get("repo_checkpoints")
    if isinstance(repo_checkpoints, dict):
        for key, raw in repo_checkpoints.items():
            state.repo_checkpoints[str(key)] = _checkpoint_from_json(raw)
    return state


def _start_to_json(event: StartEvent) -> dict[str, object]:
    return {
        "schema": event.schema,
        "run_id": event.run_id,
        "event": "start",
        "ts": event.ts,
        "harness_version": event.harness_version,
        "root": event.root,
        "cwd": event.cwd,
        "label": event.label,
        "model": event.model,
        "effort": event.effort,
        "tool_mode": event.tool_mode,
        "write_mode": event.write_mode,
        "engine_pid": event.engine_pid,
        "transcript": event.transcript,
        "messages": event.messages,
        "usage_journal": event.usage_journal,
        "max_iters": event.max_iters,
    }


def _end_to_json(event: EndEvent) -> dict[str, object]:
    return {
        "schema": event.schema,
        "run_id": event.run_id,
        "event": "end",
        "ts": event.ts,
        "status": event.status,
        "tool_calls": event.tool_calls,
        "refused": event.refused,
        "sent": event.sent,
        "recv": event.recv,
        **({"stop_reason": event.stop_reason} if event.stop_reason else {}),
    }


def _latest_artifact_mtime(start: StartEvent) -> float | None:
    latest: float | None = None
    for raw in (start.transcript, start.messages, start.usage_journal):
        path = Path(raw)
        try:
            mtime = path.stat().st_mtime
        except OSError:
            continue
        if latest is None or mtime > latest:
            latest = mtime
    return latest


def _index_fallback_usage(end: EndEvent | None) -> tuple[UsageEvidence, tuple[Diagnostic, ...]]:
    if end is None or (end.sent is None and end.recv is None):
        return (
            UsageEvidence(
                totals=UsageTotals(
                    tokens_sent=None,
                    tokens_returned=None,
                    cache_read=None,
                    cache_write=None,
                    sent_includes_cache_read=None,
                    response_count=0,
                    tokens_sent_unknown_responses=0,
                    tokens_returned_unknown_responses=0,
                    cache_read_unknown_responses=0,
                    cache_write_unknown_responses=0,
                ),
                source="missing",
                artifact=None,
                diagnostics=(),
            ),
            (),
        )
    totals = UsageTotals(
        tokens_sent=end.sent,
        tokens_returned=end.recv,
        cache_read=None,
        cache_write=None,
        sent_includes_cache_read=None,
        response_count=1,
        tokens_sent_unknown_responses=0 if end.sent is not None else 1,
        tokens_returned_unknown_responses=0 if end.recv is not None else 1,
        cache_read_unknown_responses=1,
        cache_write_unknown_responses=1,
    )
    diagnostic = Diagnostic(
        "index_end_usage_fallback",
        "primary counters filled from index end event",
        run_id=end.run_id,
    )
    return (
        UsageEvidence(totals=totals, source="missing", artifact=None, diagnostics=(diagnostic,)),
        (diagnostic,),
    )


@dataclass(frozen=True)
class OpenRunObservation:
    """The latest liveness verdict for a run whose index has no end record."""

    run_id: str
    liveness: Liveness
    engine_pid: int | None
    max_iters: int | None
    transcript: str | None
    messages: str | None
    usage_journal: str | None
    observed_at: str  # normalized ISO UTC time the verdict was computed
    start: StartEvent | None = None


class Synchronizer:
    def __init__(
        self,
        paths: AppPaths,
        locks: StateLocks,
        clock: Callable[[], datetime],
        process_probe: ProcessProbe,
        *,
        min_interval_seconds: float = 0.0,
        tool_importer: Callable[[], ImportReport] | None = None,
    ) -> None:
        self.paths = paths
        self.tool_importer = tool_importer
        self.locks = locks
        self.clock = clock
        self.process_probe = process_probe
        self.registry = ProjectRegistry(paths.projects, locks, clock)
        self.open_runs: dict[str, OpenRunObservation] = {}
        self.min_interval_seconds = min_interval_seconds
        self._archive_sources: dict[str, tuple] = {}
        self._last_run_at: datetime | None = None
        self._last_report: SyncReport | None = None

    def run(self) -> SyncReport:
        with self.locks.write:
            try:
                poll = getattr(self.process_probe, "poll_observers", None)
                if poll is not None:
                    poll()
            except Exception:
                pass  # read-only evidence is independent of accounting classification
            if (
                self.min_interval_seconds > 0
                and self._last_run_at is not None
                and self._last_report is not None
                and (self.clock() - self._last_run_at).total_seconds()
                < self.min_interval_seconds
            ):
                return self._last_report
            report = self._run_locked()
            self._last_run_at = self.clock()
            self._last_report = report
            return report

    def _run_locked(self) -> SyncReport:
        now = self.clock()
        observed_at = normalize_utc(now.isoformat())
        state = load_sync_state(self.paths.sync_state)

        # Discover repo run index files: per-repo copies of the same engine
        # register, written where a sandboxed launcher can reach them.
        projects, _ = load_projects(self.paths.projects)
        scan_roots, scan_diagnostics = load_scan_roots(self.paths.scan_roots_file)
        files = repo_index_files(
            [p.root for p in projects.values()], scan_roots
        )

        def _repo_key(path: Path) -> str:
            return os.path.normcase(os.path.abspath(str(path)))

        batch = read_index_batch(self.paths.machine_index, state.checkpoint)
        repo_batches: list[tuple[Path, str, object]] = []
        for repo_file in files:
            key = _repo_key(repo_file)
            cp = state.repo_checkpoints.get(key, IndexCheckpoint(0, None))
            repo_batches.append((repo_file, key, read_index_batch(repo_file, cp)))

        full_rescan = batch.full_rescan or any(
            rb.full_rescan for (_, _, rb) in repo_batches
        )
        if full_rescan:
            state.open_starts.clear()
            state.unmatched_ends.clear()
            # Re-read every source from the start so no source's earlier open
            # starts are lost.
            batch = read_index_batch(
                self.paths.machine_index, IndexCheckpoint(0, None)
            )
            repo_batches = []
            for repo_file in files:
                key = _repo_key(repo_file)
                repo_batches.append(
                    (
                        repo_file,
                        key,
                        read_index_batch(repo_file, IndexCheckpoint(0, None)),
                    )
                )

        repo_events: list[StartEvent | EndEvent] = []
        for _, _, rb in repo_batches:
            repo_events.extend(rb.events)

        # The machine index comes before repo files, so on a conflict its copy
        # wins.
        events: list[StartEvent | EndEvent] = [
            *state.open_starts.values(),
            *state.unmatched_ends.values(),
            *batch.events,
            *repo_events,
        ]
        pairs, reduce_diagnostics = reduce_index_events(events)

        current_by_id = highest_valid_revisions(load_ledger(self.paths.ledger)[0])

        scanned_records = len(batch.events) + len(repo_events)
        diagnostics: list[Diagnostic] = [
            *scan_diagnostics,
            *batch.diagnostics,
        ]
        for _, _, rb in repo_batches:
            diagnostics.extend(rb.diagnostics)
        diagnostics.extend(reduce_diagnostics)
        appended = 0
        unchanged = 0
        discovered = 0
        retry_run_ids: list[str] = []
        next_open: dict[str, StartEvent] = {}
        next_open_runs: dict[str, OpenRunObservation] = {}
        next_unmatched: dict[str, EndEvent] = {}

        for run_id, (start, end) in pairs.items():
            if start is None:
                if end is not None:
                    current = current_by_id.get(run_id)
                    # Carry only a genuinely unmatched end; drop a late
                    # duplicate of an end already applied to the ledger.
                    if current is None or current.ended_at is None:
                        next_unmatched[run_id] = end
                continue

            # run_ids are unique, so a start for a run the ledger already
            # closed with a real end record is a late duplicate: skip it.
            current = current_by_id.get(run_id)
            if end is None and current is not None and current.ended_at is not None:
                continue

            project, created = self.registry.discover(start.root)
            if created:
                discovered += 1

            checked, path_diagnostics = validate_artifact_paths(start)
            diagnostics.extend(path_diagnostics)

            liveness = classify_liveness(
                start,
                end,
                now=now,
                latest_artifact_mtime=_latest_artifact_mtime(start),
                probe=self.process_probe,
            )

            current = current_by_id.get(run_id)

            # Skip work that cannot change anything: a run still open in the
            # index, already terminal in the ledger, whose fresh liveness agrees
            # it is terminal with the same status, has nothing left to persist.
            if (
                end is None
                and current is not None
                and current.status in _TERMINAL_STATUSES
                and liveness.terminal
                and liveness.persist_status == current.status
            ):
                unchanged += 1
                self._archive_activity(run_id, start.transcript)
                next_open[run_id] = start
                next_open_runs[run_id] = OpenRunObservation(
                    run_id=run_id,
                    liveness=liveness,
                    engine_pid=start.engine_pid,
                    max_iters=start.max_iters,
                    transcript=start.transcript,
                    messages=start.messages,
                    usage_journal=start.usage_journal,
                    observed_at=observed_at,
                    start=start,
                )
                if any(
                    diag.code == "inaccessible_artifact" and diag.run_id == run_id
                    for diag in path_diagnostics
                ):
                    retry_run_ids.append(run_id)
                continue

            usage = self._resolve_usage(start, end, diagnostics)

            candidate = RunCandidate(
                run_id=run_id,
                project_id=project.project_id,
                timestamp=start.ts,
                ended_at=end.ts if end is not None else None,
                # Persist only the durable lifecycle fact (open vs terminal).
                # Ephemeral display states (stalled/abandoned) are computed live
                # at snapshot time and never written here.
                status=liveness.persist_status,
                label=start.label,
                model=start.model,
                effort=start.effort,
                tool_mode=start.tool_mode,
                write_mode=start.write_mode,
                tool_calls=end.tool_calls if end is not None else None,
                refused=end.refused if end is not None else None,
                stop_reason=safe_stop_reason(
                    end.stop_reason if end is not None else None, liveness.persist_status
                ),
                usage=usage,
                transcript_artifact=start.transcript,
                provenance=("machine_index",),
                lifecycle_inferred=liveness.inferred,
            )

            result = sync_candidate(
                self.paths.ledger,
                candidate,
                observed_at=observed_at,
                locks=self.locks,
                current=current,
            )
            current_by_id[run_id] = result.record
            if result.appended:
                appended += 1
            else:
                unchanged += 1

            # Capture a sanitized activity snapshot while the transcript is still
            # readable, so per-step detail survives cleanup of an ephemeral
            # workspace. Only structural metadata + counters are stored.
            self._archive_activity(run_id, start.transcript)

            if end is None:
                next_open[run_id] = start
                next_open_runs[run_id] = OpenRunObservation(
                    run_id=run_id,
                    liveness=liveness,
                    engine_pid=start.engine_pid,
                    max_iters=start.max_iters,
                    transcript=start.transcript,
                    messages=start.messages,
                    usage_journal=start.usage_journal,
                    observed_at=observed_at,
                    start=start,
                )
                if any(
                    diag.code == "inaccessible_artifact" and diag.run_id == run_id
                    for diag in path_diagnostics
                ):
                    retry_run_ids.append(run_id)

        next_repo_checkpoints = {
            key: rb.checkpoint for (_, key, rb) in repo_batches
        }
        next_state = SyncState(
            checkpoint=batch.checkpoint,
            open_starts=next_open,
            unmatched_ends=next_unmatched,
            retry_run_ids=retry_run_ids,
            repo_checkpoints=next_repo_checkpoints,
        )
        self._persist_state(next_state, diagnostics)
        self.open_runs = next_open_runs

        report = SyncReport(
            scanned_records=scanned_records,
            discovered_projects=discovered,
            appended_revisions=appended,
            unchanged_runs=unchanged,
            retry_runs=len(retry_run_ids),
            full_rescan=full_rescan,
            diagnostics=tuple(diagnostics),
        )
        return self._import_tool_usage(report)

    def _import_tool_usage(self, report: SyncReport) -> SyncReport:
        """Run the tool-usage importer after the run-index work (spec §6.5)."""
        if self.tool_importer is None:
            return report
        try:
            extra = tuple(self.tool_importer().diagnostics)
        except Exception as exc:  # a failing import must not fail the sync
            extra = (Diagnostic("tool_import_failed", type(exc).__name__),)
        return replace(report, diagnostics=report.diagnostics + extra)

    def _archive_activity(self, run_id: str, transcript: str | None) -> None:
        if not transcript:
            return
        path = Path(transcript)
        if not path.is_file():
            return
        signature = self._archive_signature(path)
        if signature == self._archive_sources.get(run_id):
            return
        from . import activity

        snapshot = activity.build_activity_snapshot(transcript)
        # Only persist when there is genuine content to keep; do not overwrite a
        # richer archive with an empty snapshot if the transcript momentarily
        # became unreadable.
        if snapshot["step_count"] == 0:
            existing = self.paths.activity_archive(run_id)
            if existing.is_file():
                self._archive_sources[run_id] = signature
                return
        activity.write_activity_archive(
            self.paths.activity_archive(run_id), snapshot
        )
        self._archive_sources[run_id] = signature

    @staticmethod
    def _archive_signature(transcript: Path) -> tuple:
        def stat_sig(p: Path) -> tuple[int, int] | None:
            try:
                st = p.stat()
            except OSError:
                return None
            return (st.st_size, st.st_mtime_ns)

        journal = transcript.with_suffix(".usage.jsonl")
        return (stat_sig(transcript), stat_sig(journal))

    def _resolve_usage(
        self,
        start: StartEvent,
        end: EndEvent | None,
        diagnostics: list[Diagnostic],
    ) -> UsageEvidence:
        evidence = select_usage_evidence(
            Path(start.transcript),
            Path(start.messages),
            Path(start.usage_journal),
        )
        diagnostics.extend(evidence.diagnostics)
        if evidence.source != "missing":
            return evidence
        fallback, fallback_diagnostics = _index_fallback_usage(end)
        diagnostics.extend(fallback_diagnostics)
        return fallback

    def _persist_state(
        self, state: SyncState, diagnostics: list[Diagnostic]
    ) -> None:
        payload = {
            "schema": 1,
            "checkpoint": _checkpoint_to_json(state.checkpoint),
            "open_starts": {
                run_id: _start_to_json(event)
                for run_id, event in state.open_starts.items()
            },
            "unmatched_ends": {
                run_id: _end_to_json(event)
                for run_id, event in state.unmatched_ends.items()
            },
            "retry_run_ids": list(state.retry_run_ids),
            "repo_checkpoints": {
                key: _checkpoint_to_json(cp)
                for key, cp in state.repo_checkpoints.items()
            },
            "diagnostics": [
                {
                    "code": diag.code,
                    "message": diag.message,
                    "source": diag.source,
                    "line": diag.line,
                    "run_id": diag.run_id,
                    "project_id": diag.project_id,
                }
                for diag in diagnostics
            ],
        }
        atomic_write_json(self.paths.sync_state, payload, self.locks.write)

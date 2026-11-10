from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol

ProjectState = Literal["discovered", "approved", "rejected", "disabled"]
RunStatus = Literal["running", "complete", "failed", "interrupted"]
# Ephemeral display state for an open (not-yet-terminal) run, computed live at
# snapshot time and never persisted. "stalled" and "abandoned" are NOT terminal.
LivenessDisplay = Literal[
    "running", "stalled", "abandoned", "unverified", "complete", "failed", "interrupted"
]
UsageSource = Literal["usage_journal", "messages", "footer", "missing"]
CostClassification = Literal["exact", "upper_bound", "missing_rate", "missing_usage"]
# Why an engine run ended incomplete (persisted status stays "failed").
StopReason = Literal["max_iters", "repair_limit", "no_action_limit", "mutation_outcome_unknown",
                     "context_budget", "context_admission", "recovery_state", "response_incomplete"]
STOP_REASONS = frozenset(
    {"max_iters", "repair_limit", "no_action_limit", "mutation_outcome_unknown", "context_budget",
     "context_admission", "recovery_state", "response_incomplete"}
)


def safe_stop_reason(value: object, status: object) -> StopReason | None:
    """An allowlisted reason on a failed run, else None (unknown text never passes)."""
    if status == "failed" and isinstance(value, str) and value in STOP_REASONS:
        return value  # type: ignore[return-value]
    return None


@dataclass(frozen=True)
class Diagnostic:
    code: str
    message: str
    source: str | None = None
    line: int | None = None
    run_id: str | None = None
    project_id: str | None = None


@dataclass(frozen=True)
class UsageTotals:
    tokens_sent: int | None
    tokens_returned: int | None
    cache_read: int | None
    cache_write: int | None
    sent_includes_cache_read: bool | None
    response_count: int
    tokens_sent_unknown_responses: int
    tokens_returned_unknown_responses: int
    cache_read_unknown_responses: int
    cache_write_unknown_responses: int


@dataclass(frozen=True)
class UsageEvidence:
    totals: UsageTotals
    source: UsageSource
    artifact: str | None
    diagnostics: tuple[Diagnostic, ...]


@dataclass(frozen=True)
class RunCandidate:
    run_id: str
    project_id: str
    timestamp: str
    ended_at: str | None
    status: RunStatus
    label: str
    model: str
    effort: str | None
    tool_mode: str | None
    write_mode: str | None
    tool_calls: int | None
    refused: int | None
    usage: UsageEvidence
    transcript_artifact: str | None
    provenance: tuple[str, ...]
    lifecycle_inferred: bool
    stop_reason: StopReason | None = None


@dataclass(frozen=True)
class RunRecord:
    schema: int
    run_id: str
    revision: int
    project_id: str
    timestamp: str
    ended_at: str | None
    status: RunStatus
    label: str
    model: str
    effort: str | None
    tool_mode: str | None
    write_mode: str | None
    tool_calls: int | None
    refused: int | None
    tokens_sent: int | None
    tokens_returned: int | None
    cache_read: int | None
    cache_write: int | None
    sent_includes_cache_read: bool | None
    tokens_sent_unknown_responses: int
    tokens_returned_unknown_responses: int
    cache_read_unknown_responses: int
    cache_write_unknown_responses: int
    usage_source: UsageSource
    usage_artifact: str | None
    transcript_artifact: str | None
    provenance: tuple[str, ...]
    lifecycle_inferred: bool
    observed_at: str
    stop_reason: StopReason | None = None


@dataclass(frozen=True)
class AppendResult:
    appended: bool
    record: RunRecord


@dataclass(frozen=True)
class RateSet:
    input: float
    output: float
    cache_read: float
    cache_write: float


@dataclass(frozen=True)
class PricingVersion:
    schema: int
    version_id: str
    model: str
    effective_at: str
    created_at: str
    rates: RateSet
    note: str


@dataclass(frozen=True)
class CostResult:
    amount: float | None
    classification: CostClassification
    rate_version_id: str | None


@dataclass(frozen=True)
class PricingPreview:
    model: str
    effective_at: str
    matching_runs: int
    affected_runs: int
    newly_priced_runs: int
    superseded_runs: int
    old_known_total: float
    projected_known_total: float
    delta: float
    warnings: tuple[str, ...]


@dataclass(frozen=True)
class Project:
    project_id: str
    root: str
    root_key: str
    display_name: str
    state: ProjectState
    observed_at: str
    note: str | None


@dataclass(frozen=True)
class ProjectEvent:
    schema: int
    event_id: str
    project_id: str
    event: Literal["discovered", "approved", "rejected", "disabled", "renamed"]
    root: str
    root_key: str
    display_name: str
    observed_at: str
    note: str | None


@dataclass(frozen=True)
class FileIdentity:
    device: int
    inode: int
    prefix_length: int
    prefix_sha256: str


@dataclass(frozen=True)
class IndexCheckpoint:
    offset: int
    identity: FileIdentity | None


@dataclass(frozen=True)
class ProcessSnapshot:
    pid: int
    command_line: tuple[str, ...]
    create_time: float
    running: bool


@dataclass(frozen=True)
class ProbeUnknown:
    """The probe could not determine whether the process is alive."""

    reason: str  # "access_denied" | "psutil_unavailable" | "probe_error"


class ProcessProbe(Protocol):
    def inspect(self, pid: int) -> ProcessSnapshot | ProbeUnknown | None: ...


@dataclass(frozen=True)
class Liveness:
    """A liveness verdict for a not-yet-ended run.

    ``display`` is the ephemeral state to show (running/stalled/abandoned or a
    terminal status). ``persist_status`` is what the ledger should store: a
    terminal status only when there is real evidence, otherwise "running" (the
    run stays open). ``terminal`` marks that the persisted status is final and
    must never be revised back to running. ``silence_seconds`` is the time since
    the last observed activity signal, for display. ``code`` explains the call.
    """

    display: LivenessDisplay
    persist_status: RunStatus
    terminal: bool
    inferred: bool
    code: str
    silence_seconds: float | None = None
    last_activity_at: str | None = None


@dataclass(frozen=True)
class SyncReport:
    scanned_records: int
    discovered_projects: int
    appended_revisions: int
    unchanged_runs: int
    retry_runs: int
    full_rescan: bool
    diagnostics: tuple[Diagnostic, ...]


TaskState = Literal[
    "continuing", "awaiting-verification", "reconciliation", "blocked", "verified-complete"
]
TASK_STATES: tuple[TaskState, ...] = (
    "continuing", "awaiting-verification", "reconciliation", "blocked", "verified-complete"
)
TaskReason = Literal[
    "cancelled", "revoked", "deadline", "no_progress", "replan_limit", "attempt_limit",
    "usage_limit", "invalid_evidence", "launch_failed", "operator_required", "planner_limit",
    "worker_active", "planning", "next_slice_ready", "verification_due", "effect_uncertain",
    "ownership_unavailable", "initial_snapshot", "state_inconsistent", "verified",
]


@dataclass(frozen=True)
class TaskStatusSource:
    """Startup-only pins, not ACL/provenance proof.

    Caller must keep this fixed parent-owned task-status.json outside worker write
    authority. Neither project approval nor model role registers a source.
    """

    project_id: str
    project_root: str
    envelope_sha256: str
    path: str | Path


@dataclass(frozen=True)
class TaskUsage:
    charged: int
    reserved: int
    unknown_calls: int
    total_usage: int | None


@dataclass(frozen=True)
class TaskLimits:
    max_replans: int | None
    max_total_attempts: int | None
    max_concurrent_workers: int | None
    max_planner_calls: int | None
    max_final_report_calls: int | None
    stop_grace_seconds: int | None
    deadline: float | int | None
    max_usage: int | None
    worker_turn_limit: int | None


@dataclass(frozen=True)
class TaskStatusObservation:
    """Structural last parent observation; not acceptance or current-lease proof."""

    project_id: str
    schema_version: int
    orchestration_id: str
    envelope_sha256: str
    owner_epoch: int
    state: TaskState
    reason: TaskReason
    candidate_sha256: str | None
    seed_receipt_sha256: str | None
    plan_revision: int | None
    active_workers: int
    unresolved_workers: int
    pending_evaluations: int
    verifier_owner: Literal["worker", "coordinator", "none"]
    rerun_owed: bool
    satisfied_criteria: tuple[str, ...]
    required_criteria: tuple[str, ...]
    usage: TaskUsage
    limits: TaskLimits
    observation: Literal["last-parent-observation"] = "last-parent-observation"

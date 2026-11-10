from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timezone, tzinfo
from pathlib import Path
from typing import TYPE_CHECKING

from .models import (
    CostClassification,
    CostResult,
    Project,
    PricingVersion,
    RateSet,
    RunRecord,
    TaskStatusObservation,
    safe_stop_reason,
)
from .pricing import calculate_call_cost, calculate_cost
from .progress import RunProgress, read_progress
from .tool_calls import TOOLS, ToolCall
from .tool_sessions import TOOL_LIVE_SECONDS, group_sessions, known_sum, session_row

if TYPE_CHECKING:
    from .sync import OpenRunObservation

_APPROVED = "approved"
_COUNTER_FIELDS = ("tokens_sent", "tokens_returned", "cache_read", "cache_write")
# The engine bounds each model request at 600 s and each command at 600 s, and
# writes the transcript before every model attempt and after every tool. So 12
# minutes without any artifact write means the engine is not advancing. This
# matches agent_await.py's default stall threshold.
QUIET_AFTER_SECONDS = 720
RETRY_ATTENTION_ATTEMPT = 3
NUDGE_ATTENTION = 2
CUT_ATTENTION = 2
RECENT_WINDOW_SECONDS = 900
_TERMINAL_STATUSES = ("complete", "failed", "interrupted")


def _default_activity_mtime(record: RunRecord) -> float | None:
    if not record.transcript_artifact:
        return None
    try:
        return Path(record.transcript_artifact).stat().st_mtime
    except OSError:
        return None


def _default_progress_of(record: RunRecord) -> RunProgress | None:
    if not record.transcript_artifact:
        return None
    return read_progress(record.transcript_artifact)


def live_state(
    record: RunRecord,
    observation: "OpenRunObservation | None",
    progress: RunProgress | None,
    now: datetime,
    activity_mtime: float | None,
) -> dict[str, object]:
    """Compute the live display verdict for an open (or dismissed) run."""
    verified = (
        observation is not None
        and observation.liveness.code == "process_identity_valid"
    )
    if observation is not None and observation.liveness.silence_seconds is not None:
        silence = observation.liveness.silence_seconds
        last_activity_at = observation.liveness.last_activity_at
    else:
        signal = datetime.fromisoformat(record.timestamp).timestamp()
        if activity_mtime is not None and activity_mtime > signal:
            signal = activity_mtime
        silence = max(now.timestamp() - signal, 0.0)
        last_activity_at = datetime.fromtimestamp(signal, tz=timezone.utc).isoformat()

    state, reason, attention = _classify(
        observation, progress, verified, silence
    )
    dismiss_allowed = state == "quiet" and not verified
    return {
        "state": state,
        "reason": reason,
        "attention": attention,
        "dismiss_allowed": dismiss_allowed,
        "verified": verified,
        "silence_seconds": silence,
        "last_activity_at": last_activity_at,
    }


def _classify(
    observation: "OpenRunObservation | None",
    progress: RunProgress | None,
    verified: bool,
    silence: float | None,
) -> tuple[str, str, bool]:
    if (
        progress is not None
        and progress.phase == "model_call"
        and progress.attempt is not None
        and progress.attempt >= 2
    ):
        return (
            "retrying",
            f"model call attempt {progress.attempt}/{progress.max_attempts}",
            progress.attempt >= RETRY_ATTENTION_ATTEMPT,
        )
    if progress is not None and progress.phase == "model_call" and progress.cuts >= 1:
        n = progress.cuts
        reason = ("reply cut at the gateway's 2-minute limit" if n == 1
                  else f"{n} replies cut at the gateway's 2-minute limit")
        return "retrying", reason, n >= CUT_ATTENTION
    if progress is not None and progress.nudges >= 1:
        n = progress.nudges
        reason = "1 reply with no tool call" if n == 1 else f"{n} replies with no tool call"
        return "retrying", reason, n >= NUDGE_ATTENTION
    if silence is not None and silence > QUIET_AFTER_SECONDS:
        tail = "engine process verified alive" if verified else "engine process not verified"
        return "quiet", f"no artifact write for {int(silence // 60)} min; " + tail, True
    if not verified:
        return "unverified", _unverified_reason(observation), False
    return "running", "engine process verified alive", False


def _unverified_reason(observation: "OpenRunObservation | None") -> str:
    if observation is None or observation.engine_pid is None:
        return "no process identity (schema-1 run)"
    code = observation.liveness.code
    if code.startswith("probe_unknown:"):
        return f"process probe: {code.split(':', 1)[1]}"
    return f"liveness: {code}"


@dataclass(frozen=True)
class SnapshotQuery:
    project_ids: tuple[str, ...] = ()
    models: tuple[str, ...] = ()
    tasks: tuple[str, ...] = ()
    states: tuple[str, ...] = ()
    date_from: date | None = None
    date_to: date | None = None
    include_excluded: bool = False
    tools: tuple[str, ...] = ()


_ENGINE = "engine"


def _run_date(record: RunRecord, tz: tzinfo) -> date:
    return datetime.fromisoformat(record.timestamp).astimezone(tz).date()


def _call_date(call: ToolCall, tz: tzinfo) -> date:
    return datetime.fromisoformat(call.at).astimezone(tz).date()


def _counter_summary(
    records: Sequence[RunRecord], field_name: str, calls: Sequence[ToolCall] = ()
) -> dict[str, object]:
    values = [getattr(record, field_name) for record in records]
    values += [getattr(call, field_name) for call in calls]
    known = [value for value in values if value is not None]
    return {
        "known_total": sum(known) if known else None,
        "unknown_rows": len(values) - len(known),
        "partial": bool(known) and len(known) != len(values),
    }


# Each tool call's CostResult, keyed by id(call). build_snapshot prices every
# call once and hands this map to the aggregations, which never re-price.
CallCosts = Mapping[int, CostResult]


def _price_calls(
    calls: Sequence[ToolCall],
    baseline: Mapping[str, RateSet | None],
    history: Sequence[PricingVersion],
) -> dict[int, CostResult]:
    costs: dict[int, CostResult] = {}
    for call in calls:
        if id(call) not in costs:
            costs[id(call)] = calculate_call_cost(call, baseline, history)
    return costs


def _known_cost(
    records: Sequence[RunRecord],
    calls: Sequence[ToolCall],
    baseline: Mapping[str, RateSet | None],
    history: Sequence[PricingVersion],
    costs: CallCosts,
) -> float:
    total = 0.0
    for record in records:
        amount = calculate_cost(record, baseline, history).amount
        if amount is not None:
            total += amount
    for call in calls:
        amount = costs[id(call)].amount
        if amount is not None:
            total += amount
    return total


def _by_tool(
    records: Sequence[RunRecord],
    calls: Sequence[ToolCall],
    baseline: Mapping[str, RateSet | None],
    history: Sequence[PricingVersion],
    costs: CallCosts,
) -> dict[str, dict[str, object]]:
    result: dict[str, dict[str, object]] = {}
    if records:
        result[_ENGINE] = {
            "count": len(records),
            "sessions": None,
            "known_cost": _known_cost(records, (), baseline, history, costs),
            **{name: known_sum(records, name) for name in _COUNTER_FIELDS},
        }
    for tool in TOOLS:
        members = [call for call in calls if call.tool == tool]
        if not members:
            continue
        result[tool] = {
            "count": len(members),
            "sessions": len({call.root_session for call in members}),
            "known_cost": _known_cost((), members, baseline, history, costs),
            **{name: known_sum(members, name) for name in _COUNTER_FIELDS},
        }
    return result


def _period_totals(
    records: Sequence[RunRecord],
    baseline: Mapping[str, RateSet | None],
    history: Sequence[PricingVersion],
    calls: Sequence[ToolCall],
    costs: CallCosts,
) -> dict[str, object]:
    summary: dict[str, object] = {
        "runs": len(records),
        "known_cost": _known_cost(records, calls, baseline, history, costs),
    }
    for field_name in _COUNTER_FIELDS:
        summary[field_name] = _counter_summary(records, field_name, calls)
    summary["calls"] = len(calls)
    summary["by_tool"] = _by_tool(records, calls, baseline, history, costs)
    return summary


def _matches_filters(record: RunRecord, query: SnapshotQuery, tz: tzinfo) -> bool:
    if query.project_ids and record.project_id not in query.project_ids:
        return False
    if query.models and record.model not in query.models:
        return False
    if query.tasks and record.label not in query.tasks:
        return False
    if query.states and record.status not in query.states:
        return False
    run_day = _run_date(record, tz)
    if query.date_from is not None and run_day < query.date_from:
        return False
    if query.date_to is not None and run_day > query.date_to:
        return False
    if query.tools and _ENGINE not in query.tools:
        return False
    return True


def _call_matches_filters(
    call: ToolCall, query: SnapshotQuery, day_of: Callable[[ToolCall], date]
) -> bool:
    if query.tools and call.tool not in query.tools:
        return False
    # Task and state filters describe runs only, so they exclude every call.
    if query.tasks or query.states:
        return False
    if query.project_ids and call.project_id not in query.project_ids:
        return False
    if query.models and call.model not in query.models:
        return False
    call_day = day_of(call)
    if query.date_from is not None and call_day < query.date_from:
        return False
    if query.date_to is not None and call_day > query.date_to:
        return False
    return True


def _bucket_spend(
    records: Sequence[RunRecord],
    calls: Sequence[ToolCall],
    baseline: Mapping[str, RateSet | None],
    history: Sequence[PricingVersion],
    run_key: Callable[[RunRecord], object],
    call_key: Callable[[ToolCall], object],
    costs: CallCosts,
) -> tuple[dict[object, float], dict[object, int], dict[object, int]]:
    buckets: dict[object, float] = {}
    runs: dict[object, int] = {}
    call_counts: dict[object, int] = {}
    for record in records:
        group_key = run_key(record)
        amount = calculate_cost(record, baseline, history).amount
        buckets[group_key] = buckets.get(group_key, 0.0) + (amount or 0.0)
        runs[group_key] = runs.get(group_key, 0) + 1
    for call in calls:
        group_key = call_key(call)
        amount = costs[id(call)].amount
        buckets[group_key] = buckets.get(group_key, 0.0) + (amount or 0.0)
        call_counts[group_key] = call_counts.get(group_key, 0) + 1
    return buckets, runs, call_counts


def _grouped_spend(
    records: Sequence[RunRecord],
    baseline: Mapping[str, RateSet | None],
    history: Sequence[PricingVersion],
    key: str,
    calls: Sequence[ToolCall],
    costs: CallCosts,
    call_key: Callable[[ToolCall], object] | None = None,
) -> list[dict[str, object]]:
    buckets, runs, call_counts = _bucket_spend(
        records, calls, baseline, history,
        lambda record: getattr(record, key),
        call_key or (lambda call: getattr(call, key)),
        costs,
    )
    ordered = sorted(buckets.items(), key=lambda item: (-item[1], str(item[0])))
    return [
        {
            "key": key_value,
            "known_cost": amount,
            "runs": runs.get(key_value, 0),
            "calls": call_counts.get(key_value, 0),
        }
        for key_value, amount in ordered
    ]


def _spend_by_tool(
    records: Sequence[RunRecord],
    calls: Sequence[ToolCall],
    baseline: Mapping[str, RateSet | None],
    history: Sequence[PricingVersion],
    costs: CallCosts,
) -> list[dict[str, object]]:
    """Spend per tool, with engine runs under "engine"; same rows as by_model."""
    buckets, runs, call_counts = _bucket_spend(
        records, calls, baseline, history,
        lambda record: _ENGINE,
        lambda call: call.tool,
        costs,
    )
    ordered = sorted(buckets.items(), key=lambda item: (-item[1], str(item[0])))
    return [
        {
            "key": key_value,
            "known_cost": amount,
            "runs": runs.get(key_value, 0),
            "calls": call_counts.get(key_value, 0),
        }
        for key_value, amount in ordered
    ]


def _spend_by_day(
    records: Sequence[RunRecord],
    baseline: Mapping[str, RateSet | None],
    history: Sequence[PricingVersion],
    tz: tzinfo,
    calls: Sequence[ToolCall],
    costs: CallCosts,
    day_of: Callable[[ToolCall], date],
) -> list[dict[str, object]]:
    buckets, runs, call_counts = _bucket_spend(
        records, calls, baseline, history,
        lambda record: _run_date(record, tz).isoformat(),
        lambda call: day_of(call).isoformat(),
        costs,
    )
    ordered = sorted(buckets.items(), key=lambda item: str(item[0]), reverse=True)
    return [
        {
            "day": day,
            "known_cost": amount,
            "runs": runs.get(day, 0),
            "calls": call_counts.get(day, 0),
        }
        for day, amount in ordered
    ]


def _run_view(
    record: RunRecord,
    baseline: Mapping[str, RateSet | None],
    history: Sequence[PricingVersion],
    included: bool,
) -> dict[str, object]:
    cost = calculate_cost(record, baseline, history)
    return {
        "run_id": record.run_id,
        "project_id": record.project_id,
        "timestamp": record.timestamp,
        "ended_at": record.ended_at,
        # Persisted lifecycle fact (open vs terminal).
        "status": record.status,
        # History rows do not compute live state; that lives in "live".
        "display_state": record.status,
        # Allowlisted incomplete cause on failed rows only; null otherwise.
        "stop_reason": safe_stop_reason(record.stop_reason, record.status),
        "silence_seconds": None,
        "last_activity_at": None,
        "label": record.label,
        "model": record.model,
        "effort": record.effort,
        "tool_calls": record.tool_calls,
        "tokens_sent": record.tokens_sent,
        "tokens_returned": record.tokens_returned,
        "cache_read": record.cache_read,
        "cache_write": record.cache_write,
        "usage_source": record.usage_source,
        "cost": cost.amount,
        "cost_classification": cost.classification,
        "lifecycle_inferred": record.lifecycle_inferred,
        "included_in_totals": included,
    }


def _live_members(
    runs: Sequence[RunRecord],
    obs_map: "Mapping[str, OpenRunObservation]",
) -> list[tuple[RunRecord, bool]]:
    """Return (record, dismissed) for every row that belongs in "live"."""
    members: list[tuple[RunRecord, bool]] = []
    for record in runs:
        if record.status == "running":
            members.append((record, False))
            continue
        if "operator_dismiss" in record.provenance:
            obs = obs_map.get(record.run_id)
            if obs is not None and obs.liveness.code == "process_identity_valid":
                members.append((record, True))
    return members


def _build_live(
    runs: Sequence[RunRecord],
    obs_map: "Mapping[str, OpenRunObservation]",
    projects: Mapping[str, Project],
    baseline: Mapping[str, RateSet | None],
    history: Sequence[PricingVersion],
    is_approved: Callable[[RunRecord], bool],
    now: datetime,
    resolve_mtime: Callable[[RunRecord], float | None],
    resolve_progress: Callable[[RunRecord], RunProgress | None],
) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for record, dismissed in _live_members(runs, obs_map):
        obs = obs_map.get(record.run_id)
        progress = resolve_progress(record)
        state = live_state(record, obs, progress, now, resolve_mtime(record))
        reason = state["reason"]
        attention = state["attention"]
        dismiss_allowed = state["dismiss_allowed"]
        if dismissed:
            reason = "dismissed by operator; " + str(reason)
            attention = False
            dismiss_allowed = False
        verified = bool(state["verified"])
        can_kill = verified and obs is not None and obs.engine_pid is not None
        can_steer = verified and not dismissed and bool(record.transcript_artifact)
        if can_kill and can_steer:
            control_reason: str | None = None
        elif obs is None or obs.engine_pid is None:
            control_reason = "no process identity (schema-1 run)"
        elif not verified:
            control_reason = "engine process not verified alive"
        elif not record.transcript_artifact:
            control_reason = "no transcript recorded"
        elif dismissed:
            control_reason = "dismissed by operator"
        else:
            control_reason = None
        project = projects.get(record.project_id)
        row = _run_view(record, baseline, history, is_approved(record))
        row.update(
            {
                "can_kill": can_kill,
                "can_steer": can_steer,
                "control_reason": control_reason,
                "display_state": state["state"],
                "state_reason": reason,
                "attention": attention,
                "dismiss_allowed": dismiss_allowed,
                "verified": state["verified"],
                "dismissed": dismissed,
                "silence_seconds": state["silence_seconds"],
                "last_activity_at": state["last_activity_at"],
                "project_state": project.state if project is not None else None,
                "elapsed_seconds": max(
                    0, int(now.timestamp() - datetime.fromisoformat(record.timestamp).timestamp())
                ),
                "max_iters": obs.max_iters if obs is not None else None,
                "phase": progress.phase if progress is not None else None,
                "iteration": progress.iteration if progress is not None else None,
                "attempt": progress.attempt if progress is not None else None,
                "max_attempts": progress.max_attempts if progress is not None else None,
                "nudges": progress.nudges if progress is not None else None,
                "last_tool": progress.last_tool if progress is not None else None,
                "last_path": progress.last_path if progress is not None else None,
            }
        )
        rows.append(row)
    rows.sort(
        key=lambda r: (not r["attention"], _neg_ts(r["timestamp"]), r["run_id"])
    )
    return rows


def _neg_ts(timestamp: str) -> float:
    return -datetime.fromisoformat(timestamp).timestamp()


def _build_recently_ended(
    runs: Sequence[RunRecord],
    live_ids: set[str],
    projects: Mapping[str, Project],
    baseline: Mapping[str, RateSet | None],
    history: Sequence[PricingVersion],
    is_approved: Callable[[RunRecord], bool],
    now: datetime,
    obs_map: "Mapping[str, OpenRunObservation]",
    killed: set[str],
) -> list[dict[str, object]]:
    rows: list[tuple[float, dict[str, object]]] = []
    for record in runs:
        if record.status not in _TERMINAL_STATUSES:
            continue
        if record.run_id in live_ids:
            continue
        if record.ended_at is not None:
            end_iso = record.ended_at
            end_source = "end_record"
        else:
            obs = obs_map.get(record.run_id)
            last_activity = (
                obs.liveness.last_activity_at if obs is not None else None
            )
            if last_activity is not None:
                end_iso = last_activity
                end_source = "last_activity"
            else:
                end_iso = record.observed_at
                end_source = "observed"
        end_dt = datetime.fromisoformat(end_iso)
        end_ts = end_dt.timestamp()
        if now.timestamp() - end_ts > RECENT_WINDOW_SECONDS:
            continue
        dismissed = "operator_dismiss" in record.provenance
        attention = record.status in ("failed", "interrupted") and not dismissed
        if record.run_id in killed:
            reason = "killed by operator"
        elif dismissed:
            reason = "dismissed by operator"
        elif record.status == "complete":
            reason = "completed"
        else:
            reason = record.status
        if record.run_id not in killed and record.lifecycle_inferred:
            reason += " (inferred)"
        started = datetime.fromisoformat(record.timestamp).timestamp()
        project = projects.get(record.project_id)
        row = _run_view(record, baseline, history, is_approved(record))
        row.update(
            {
                "project_state": project.state if project is not None else None,
                "ended_at_effective": end_iso,
                "elapsed_seconds": max(0, int(end_ts - started)),
                "dismissed": dismissed,
                "attention": attention,
                "end_reason": reason,
                "end_time_source": end_source,
            }
        )
        rows.append((end_ts, row))
    rows.sort(key=lambda item: (-item[0], item[1]["run_id"]))
    return [row for _, row in rows[:10]]


def _live_totals(
    rows: Sequence[Mapping[str, object]],
    tool_rows: Sequence[Mapping[str, object]] = (),
) -> dict[str, object]:
    tool_burn: float | None = None
    for tool_row in tool_rows:
        burn_cost = tool_row["burn_per_hour"]["cost"]  # type: ignore[index]
        if burn_cost is not None:
            tool_burn = (tool_burn or 0.0) + burn_cost
    return {
        **_run_live_totals(rows),
        "tool_sessions": len(tool_rows),
        "tool_burn_cost_per_hour": tool_burn,
    }


def _tool_session_rows(
    calls: Sequence[ToolCall],
    projects: Mapping[str, Project],
    baseline: Mapping[str, RateSet | None],
    history: Sequence[PricingVersion],
    is_call_approved: Callable[[ToolCall], bool],
    now: datetime,
) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    live: list[tuple[datetime, dict[str, object]]] = []
    recent: list[tuple[datetime, dict[str, object]]] = []
    for session in group_sessions(calls):
        last = datetime.fromisoformat(session.last_at)
        age = (now - last).total_seconds()
        if age > TOOL_LIVE_SECONDS + RECENT_WINDOW_SECONDS:
            continue
        latest = session.calls[-1]
        row = session_row(
            session, projects, baseline, history, is_call_approved(latest), now
        )
        (live if age <= TOOL_LIVE_SECONDS else recent).append((last, row))
    live.sort(key=lambda item: item[0], reverse=True)
    recent.sort(key=lambda item: item[0], reverse=True)
    return [row for _, row in live], [row for _, row in recent]


def _run_live_totals(rows: Sequence[Mapping[str, object]]) -> dict[str, object]:
    known_cost = 0.0
    cost_unknown_runs = 0
    sent_known: list[int] = []
    returned_known: list[int] = []
    burn = 0.0
    burn_rows = 0
    for row in rows:
        cost = row["cost"]
        if cost is None:
            cost_unknown_runs += 1
        else:
            known_cost += cost
            elapsed = row["elapsed_seconds"]
            if elapsed >= 60:
                burn += cost / (elapsed / 3600)
                burn_rows += 1
        if row["tokens_sent"] is not None:
            sent_known.append(row["tokens_sent"])
        if row["tokens_returned"] is not None:
            returned_known.append(row["tokens_returned"])
    return {
        "runs": len(rows),
        "known_cost": known_cost,
        "cost_unknown_runs": cost_unknown_runs,
        "tokens_sent": sum(sent_known) if sent_known else None,
        "tokens_returned": sum(returned_known) if returned_known else None,
        "burn_per_hour": burn if burn_rows else None,
    }


def build_snapshot(
    runs: Sequence[RunRecord],
    projects: Mapping[str, Project],
    baseline: Mapping[str, RateSet | None],
    history: Sequence[PricingVersion],
    query: SnapshotQuery,
    *,
    now: datetime,
    tz: tzinfo | None = None,
    activity_mtime_of: Callable[[RunRecord], float | None] | None = None,
    observations: "Mapping[str, OpenRunObservation] | None" = None,
    progress_of: Callable[[RunRecord], RunProgress | None] | None = None,
    operator_actions: "Sequence[Mapping[str, object]] | None" = None,
    tool_calls: Sequence[ToolCall] = (),
    task_statuses: Sequence[TaskStatusObservation] | None = None,
) -> dict[str, object]:
    if (
        query.date_from is not None
        and query.date_to is not None
        and query.date_from > query.date_to
    ):
        raise ValueError("date_from must not be after date_to")

    resolve_mtime = activity_mtime_of or _default_activity_mtime
    resolve_progress = progress_of or _default_progress_of
    obs_map = observations or {}
    killed = {
        a["run_id"]
        for a in operator_actions or ()
        if a.get("action") == "kill"
        and a.get("outcome") in ("terminated", "partial")
        and isinstance(a.get("run_id"), str)
    }
    if tz is None:
        tz = datetime.now().astimezone().tzinfo
    today = now.astimezone(tz).date()

    def is_approved(record: RunRecord) -> bool:
        project = projects.get(record.project_id)
        return project is not None and project.state == _APPROVED

    def is_call_approved(call: ToolCall) -> bool:
        if call.project_id is None:
            return False
        project = projects.get(call.project_id)
        return project is not None and project.state == _APPROVED

    filtered = [record for record in runs if _matches_filters(record, query, tz)]
    approved = [record for record in filtered if is_approved(record)]
    visible = filtered if query.include_excluded else approved
    # Each call's local date is computed at most once per snapshot.
    call_days: dict[int, date] = {}

    def day_of(call: ToolCall) -> date:
        day = call_days.get(id(call))
        if day is None:
            day = call_days[id(call)] = _call_date(call, tz)
        return day

    filtered_calls = [
        call for call in tool_calls if _call_matches_filters(call, query, day_of)
    ]
    approved_calls = [call for call in filtered_calls if is_call_approved(call)]
    # Price each call once; every aggregation below reads this map.
    costs = _price_calls(filtered_calls, baseline, history)

    def in_month(day: date) -> bool:
        return day.year == today.year and day.month == today.month

    normal_all = approved
    normal_today = [record for record in approved if _run_date(record, tz) == today]
    normal_month = [record for record in approved if in_month(_run_date(record, tz))]
    calls_today = [call for call in approved_calls if day_of(call) == today]
    calls_month = [call for call in approved_calls if in_month(day_of(call))]

    project_totals = {}
    for project_id, project in projects.items():
        if project.state != _APPROVED:
            continue
        project_runs = [record for record in approved if record.project_id == project_id]
        project_calls = [call for call in approved_calls if call.project_id == project_id]
        project_totals[project_id] = {
            "display_name": project.display_name,
            "state": project.state,
            **_period_totals(project_runs, baseline, history, project_calls, costs),
        }

    coverage: dict[str, int] = {}
    for record in approved:
        classification: CostClassification = calculate_cost(
            record, baseline, history
        ).classification
        coverage[classification] = coverage.get(classification, 0) + 1
    for call in approved_calls:
        classification = costs[id(call)].classification
        coverage[classification] = coverage.get(classification, 0) + 1

    tool_live, tool_recent = _tool_session_rows(
        filtered_calls, projects, baseline, history, is_call_approved, now
    )

    live = _build_live(
        runs, obs_map, projects, baseline, history, is_approved, now,
        resolve_mtime, resolve_progress,
    )
    live_ids = {row["run_id"] for row in live}
    recently_ended = _build_recently_ended(
        runs, live_ids, projects, baseline, history, is_approved, now, obs_map,
        killed,
    )
    offset = now.astimezone(tz).strftime("%z")
    timezone_label = f"{offset[:3]}:{offset[3:]}" if offset else "+00:00"

    # Tasks have no model/tool/run date; do not invent matches for those filters.
    goal_tasks = None
    if task_statuses is not None:
        goal_tasks = [
            asdict(observed)
            for observed in task_statuses
            if isinstance(observed, TaskStatusObservation)
            and (project := projects.get(observed.project_id)) is not None
            and (query.include_excluded or project.state == _APPROVED)
            and (not query.project_ids or observed.project_id in query.project_ids)
            and (not query.states or observed.state in query.states)
            and (not query.tasks or observed.orchestration_id in query.tasks)
            and not (query.models or query.tools or query.date_from or query.date_to)
        ]

    return {
        **({"goal_tasks": goal_tasks} if goal_tasks is not None else {}),
        "generated_at": now.isoformat(),
        "timezone": timezone_label,
        "normal_totals": {
            "today": _period_totals(
                normal_today, baseline, history, calls_today, costs
            ),
            "month": _period_totals(
                normal_month, baseline, history, calls_month, costs
            ),
            "all_time": _period_totals(
                normal_all, baseline, history, approved_calls, costs
            ),
        },
        "project_totals": project_totals,
        "runs": [
            {
                **_run_view(record, baseline, history, is_approved(record)),
                "killed_by_operator": record.run_id in killed,
            }
            for record in sorted(visible, key=lambda r: r.timestamp, reverse=True)
        ],
        "live": live,
        "live_totals": _live_totals(live, tool_live),
        "recently_ended": recently_ended,
        "tool_sessions_live": tool_live,
        "tool_sessions_recent": tool_recent,
        "spend": {
            "by_day": _spend_by_day(
                approved, baseline, history, tz, approved_calls, costs, day_of
            ),
            "by_project": _grouped_spend(
                approved, baseline, history, "project_id", approved_calls, costs
            ),
            "by_model": _grouped_spend(
                approved, baseline, history, "model", approved_calls, costs
            ),
            "by_task": _grouped_spend(
                approved, baseline, history, "label", approved_calls, costs,
                lambda call: f"tool:{call.tool}",
            ),
            "by_tool": _spend_by_tool(
                approved, approved_calls, baseline, history, costs
            ),
        },
        "coverage": coverage,
    }

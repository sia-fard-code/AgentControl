"""Tool-usage sessions (spec §8): grouping calls and building session rows.

Rows carry ids, model ids, timestamps and counters only, never the call's
working directory or any other free text.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime

from .models import CostClassification, PricingVersion, Project, RateSet
from .pricing import calculate_call_cost
from .tool_calls import ToolCall

TOOL_LIVE_SECONDS = 600
TOOL_LABELS = {"engine": "Harness agents", "claude_code": "Claude Code", "codex": "Codex",
               "cline": "Cline", "kilo": "Kilo"}
BURN_WINDOW_SECONDS = 3600
_SEVERITY: dict[str, int] = {
    "exact": 0, "upper_bound": 1, "missing_rate": 2, "missing_usage": 3,
}
_SUM_FIELDS = ("tokens_sent", "tokens_returned", "cache_read", "cache_write", "reasoning")


@dataclass(frozen=True)
class ToolSession:
    tool: str
    root_session: str
    calls: tuple[ToolCall, ...]   # sorted by at
    first_at: str
    last_at: str
    project_id: str | None        # the latest call's
    sessions: frozenset[str]


def _instant(value: str) -> datetime:
    return datetime.fromisoformat(value)


def group_sessions(calls: Iterable[ToolCall]) -> list[ToolSession]:
    """Group calls by (tool, root_session); each session's calls sorted by time."""
    groups: dict[tuple[str, str], list[ToolCall]] = {}
    for call in calls:
        groups.setdefault((call.tool, call.root_session), []).append(call)
    result: list[ToolSession] = []
    for (tool, root), members in groups.items():
        ordered = tuple(sorted(members, key=lambda c: (_instant(c.at), c.key)))
        result.append(
            ToolSession(
                tool=tool,
                root_session=root,
                calls=ordered,
                first_at=ordered[0].at,
                last_at=ordered[-1].at,
                project_id=ordered[-1].project_id,
                sessions=frozenset(c.session for c in ordered),
            )
        )
    return result


def known_sum(rows: Sequence[object], field_name: str) -> int | None:
    """Sum the known values of a counter, or None when none is known."""
    known = [getattr(r, field_name) for r in rows if getattr(r, field_name) is not None]
    return sum(known) if known else None


def tokens_in(calls: Iterable[ToolCall]) -> int | None:
    """Total input tokens including cache, or None when no tokens_sent is known.

    OpenAI-shaped calls already count cache reads in tokens_sent; for
    Anthropic-shaped calls the cache read and write are added (missing as 0).
    """
    total: int | None = None
    for call in calls:
        if call.tokens_sent is None:
            continue
        amount = call.tokens_sent
        if not call.sent_includes_cache_read:
            amount += (call.cache_read or 0) + (call.cache_write or 0)
        total = (total or 0) + amount
    return total


def worst_classification(
    classifications: Iterable[CostClassification],
) -> CostClassification:
    worst: CostClassification = "exact"
    for item in classifications:
        if _SEVERITY[item] > _SEVERITY[worst]:
            worst = item
    return worst


def session_row(
    session: ToolSession,
    projects: Mapping[str, Project],
    baseline: Mapping[str, RateSet | None],
    history: Sequence[PricingVersion],
    included: bool,
    now: datetime,
) -> dict[str, object]:
    costs = [calculate_call_cost(c, baseline, history) for c in session.calls]
    model_counts: dict[str, int] = {}
    for call in session.calls:
        if call.model is not None:
            model_counts[call.model] = model_counts.get(call.model, 0) + 1
    models = sorted(model_counts, key=lambda m: (-model_counts[m], m))
    known_costs = [cost.amount for cost in costs if cost.amount is not None]

    burn_cost: float | None = None
    burn_returned = 0
    for call, cost in zip(session.calls, costs):
        age = (now - _instant(call.at)).total_seconds()
        if age > BURN_WINDOW_SECONDS:
            continue
        if cost.amount is not None:
            burn_cost = (burn_cost or 0.0) + cost.amount
        if call.tokens_returned is not None:
            burn_returned += call.tokens_returned

    project = projects.get(session.project_id) if session.project_id else None
    row: dict[str, object] = {
        "tool": session.tool,
        "session": session.root_session,
        "project_id": session.project_id,
        "project_name": project.display_name if project is not None else None,
        "included_in_totals": included,
        "models": models,
        "first_at": session.first_at,
        "last_at": session.last_at,
        "calls": len(session.calls),
        "subagents": len(session.sessions - {session.root_session}),
    }
    for field_name in _SUM_FIELDS:
        row[field_name] = known_sum(session.calls, field_name)
    row["tokens_in"] = tokens_in(session.calls)
    row["cost"] = sum(known_costs) if known_costs else None
    row["cost_classification"] = worst_classification(c.classification for c in costs)
    row["burn_per_hour"] = {"cost": burn_cost, "tokens_returned": burn_returned}
    return row

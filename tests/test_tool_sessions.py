from __future__ import annotations

from datetime import datetime, timezone

import pytest

from agent_control.models import Project, RateSet
from agent_control.tool_calls import ToolCall
from agent_control.tool_sessions import (
    TOOL_LABELS,
    TOOL_LIVE_SECONDS,
    group_sessions,
    session_row,
)

NOW = datetime(2026, 9, 17, 12, 0, tzinfo=timezone.utc)
SENTINEL = "SENTINEL-TEXT-DO-NOT-STORE"
BASELINE = {"alpha": RateSet(1, 10, 0.1, 2), "beta": RateSet(2, 20, 0.2, 4)}


def call(key: str, **overrides: object) -> ToolCall:
    values: dict[str, object] = {
        "schema": 1,
        "tool": "claude_code",
        "key": key,
        "revision": 1,
        "session": "root",
        "root_session": "root",
        "at": "2026-09-17T11:50:00+00:00",
        "model": "alpha",
        "cwd": rf"D:\Work\{SENTINEL}",
        "project_id": "P1",
        "tokens_sent": 1_000_000,
        "tokens_returned": 0,
        "cache_read": 0,
        "cache_write": 0,
        "cache_write_5m": None,
        "reasoning": None,
        "sent_includes_cache_read": False,
        "speed": None,
        "recorded_at": "2026-09-17T11:59:00+00:00",
    }
    values.update(overrides)
    return ToolCall(**values)  # type: ignore[arg-type]


def project(project_id: str, state: str = "approved") -> Project:
    return Project(
        project_id=project_id,
        root=rf"D:\Work\{project_id}",
        root_key=rf"d:\work\{project_id}".casefold(),
        display_name=f"Project {project_id}",
        state=state,  # type: ignore[arg-type]
        observed_at="2026-09-01T00:00:00+00:00",
        note=None,
    )


def test_constants() -> None:
    assert TOOL_LIVE_SECONDS == 600
    assert TOOL_LABELS["engine"] == "Harness agents"
    assert TOOL_LABELS["claude_code"] == "Claude Code"


def test_grouping_by_tool_and_root_session() -> None:
    calls = [
        call("a2", at="2026-09-17T11:40:00+00:00"),
        call("a1", at="2026-09-17T11:30:00+00:00"),
        call("a3", session="sub1", at="2026-09-17T11:35:00+00:00"),
        call("a4", session="sub2", at="2026-09-17T11:36:00+00:00"),
        call("b1", tool="codex", root_session="root", session="root"),
        call("c1", root_session="other", session="other"),
    ]
    sessions = group_sessions(calls)
    by_key = {(s.tool, s.root_session): s for s in sessions}
    assert set(by_key) == {("claude_code", "root"), ("codex", "root"),
                           ("claude_code", "other")}
    main = by_key[("claude_code", "root")]
    assert [c.key for c in main.calls] == ["a1", "a3", "a4", "a2"]
    assert main.sessions == frozenset({"root", "sub1", "sub2"})
    assert main.first_at == "2026-09-17T11:30:00+00:00"
    assert main.last_at == "2026-09-17T11:40:00+00:00"
    assert len(by_key[("codex", "root")].calls) == 1


def test_session_row_fields() -> None:
    calls = [
        # Two hours ago, beta, other project: outside the burn window.
        call("k1", at="2026-09-17T10:00:00+00:00", model="beta", project_id="P2"),
        call("k2", at="2026-09-17T11:10:00+00:00", session="sub1",
             tokens_returned=1000, reasoning=5),
        call("k3", at="2026-09-17T11:20:00+00:00", session="sub2",
             cache_write_5m=10, tokens_returned=500),
        call("k4", at="2026-09-17T11:50:00+00:00", cache_read=None),
    ]
    session = group_sessions(calls)[0]
    projects = {"P1": project("P1"), "P2": project("P2")}
    row = session_row(session, projects, BASELINE, [], True, NOW)
    assert row["tool"] == "claude_code"
    assert row["session"] == "root"
    assert row["project_id"] == "P1"
    assert row["project_name"] == "Project P1"
    assert row["included_in_totals"] is True
    assert row["models"] == ["alpha", "beta"]
    assert row["first_at"] == "2026-09-17T10:00:00+00:00"
    assert row["last_at"] == "2026-09-17T11:50:00+00:00"
    assert row["calls"] == 4
    assert row["subagents"] == 2
    assert row["tokens_sent"] == 4_000_000
    assert row["tokens_returned"] == 1500
    assert row["cache_read"] == 0
    assert row["cache_write"] == 0
    assert row["reasoning"] == 5
    # beta 2.0 + alpha 3 x 1.0, plus output on alpha calls.
    assert row["cost"] == pytest.approx(2.0 + 3.0 + 1500 * 10 / 1_000_000)
    assert row["cost_classification"] == "upper_bound"
    burn = row["burn_per_hour"]
    assert burn["cost"] == pytest.approx(3.0 + 1500 * 10 / 1_000_000)
    assert burn["tokens_returned"] == 1500
    assert SENTINEL not in repr(row)


def test_session_row_worst_classification_and_null_sums() -> None:
    calls = [
        call("k1", tokens_returned=None, at="2026-09-17T09:00:00+00:00"),
        call("k2", model=None, at="2026-09-17T09:01:00+00:00"),
    ]
    session = group_sessions(calls)[0]
    row = session_row(session, {}, BASELINE, [], False, NOW)
    assert row["cost_classification"] == "missing_usage"
    assert row["cost"] is None
    assert row["reasoning"] is None
    assert row["project_name"] is None
    assert row["included_in_totals"] is False
    assert row["models"] == ["alpha"]
    assert row["subagents"] == 0
    assert row["burn_per_hour"] == {"cost": None, "tokens_returned": 0}

    rate_only = group_sessions([call("k3", model="gamma"), call("k4")])[0]
    assert session_row(rate_only, {}, BASELINE, [], False, NOW)[
        "cost_classification"] == "missing_rate"


def _tokens_in(*calls: ToolCall) -> object:
    session = group_sessions(calls)[0]
    return session_row(session, {}, BASELINE, [], True, NOW)["tokens_in"]


ANTHROPIC = {"tokens_sent": 10, "cache_read": 1000, "cache_write": 50,
             "sent_includes_cache_read": False}
OPENAI = {"tokens_sent": 1000, "cache_read": 900, "cache_write": None,
          "sent_includes_cache_read": True}


def test_tokens_in_anthropic_shape_adds_cache() -> None:
    assert _tokens_in(call("a", **ANTHROPIC)) == 1060


def test_tokens_in_anthropic_shape_missing_cache_counts_zero() -> None:
    assert _tokens_in(call("a", tokens_sent=10, cache_read=None,
                           cache_write=None)) == 10


def test_tokens_in_openai_shape_already_includes_cache() -> None:
    assert _tokens_in(call("o", **OPENAI)) == 1000


def test_tokens_in_mixed_session_sums_both() -> None:
    assert _tokens_in(
        call("a", at="2026-09-17T11:40:00+00:00", **ANTHROPIC),
        call("o", at="2026-09-17T11:41:00+00:00", **OPENAI),
        call("n", at="2026-09-17T11:42:00+00:00", tokens_sent=None,
             cache_read=5000),
    ) == 2060


def test_tokens_in_none_when_no_sent_known() -> None:
    assert _tokens_in(
        call("a", at="2026-09-17T11:40:00+00:00", tokens_sent=None, cache_read=7),
        call("b", at="2026-09-17T11:41:00+00:00", tokens_sent=None),
    ) is None


def test_session_row_keeps_tokens_sent_alongside_tokens_in() -> None:
    session = group_sessions([call("a", **ANTHROPIC)])[0]
    row = session_row(session, {}, BASELINE, [], True, NOW)
    assert row["tokens_sent"] == 10
    assert row["cache_read"] == 1000
    assert row["cache_write"] == 50

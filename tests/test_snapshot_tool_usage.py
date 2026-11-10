from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from agent_control.models import Project, RateSet, RunRecord
from agent_control.snapshot import SnapshotQuery, build_snapshot
from agent_control.tool_calls import ToolCall

NOW = datetime(2026, 9, 17, 12, 0, tzinfo=timezone.utc)
SENTINEL = "SENTINEL-TEXT-DO-NOT-STORE"
BASELINE = {"alpha": RateSet(1, 10, 0.1, 2), "beta": RateSet(2, 20, 0.2, 4)}
UTC = timezone.utc


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


PROJECTS = {"P1": project("P1"), "P2": project("P2"), "PP": project("PP", "pending")}


def run(run_id: str = "r1", *, project_id: str = "P1", model: str = "alpha",
        label: str = "task", timestamp: str = "2026-09-17T09:00:00+00:00",
        tokens_sent: int | None = 1_000_000) -> RunRecord:
    return RunRecord(
        schema=1, run_id=run_id, revision=1, project_id=project_id,
        timestamp=timestamp, ended_at=None, status="complete",  # type: ignore[arg-type]
        label=label, model=model, effort=None, tool_mode=None, write_mode=None,
        tool_calls=1, refused=0, tokens_sent=tokens_sent, tokens_returned=0,
        cache_read=0, cache_write=0, sent_includes_cache_read=False,
        tokens_sent_unknown_responses=0, tokens_returned_unknown_responses=0,
        cache_read_unknown_responses=0, cache_write_unknown_responses=0,
        usage_source="usage_journal", usage_artifact="a.usage.jsonl",
        transcript_artifact=None, provenance=("machine_index",),
        lifecycle_inferred=False, observed_at="2026-09-17T09:05:00+00:00",
    )


def call(key: str, **overrides: object) -> ToolCall:
    values: dict[str, object] = {
        "schema": 1, "tool": "claude_code", "key": key, "revision": 1,
        "session": "s1", "root_session": "s1",
        "at": "2026-09-17T09:00:00+00:00", "model": "alpha",
        "cwd": rf"D:\Work\{SENTINEL}", "project_id": "P1",
        "tokens_sent": 1_000_000, "tokens_returned": 0, "cache_read": 0,
        "cache_write": 0, "cache_write_5m": None, "reasoning": None,
        "sent_includes_cache_read": False, "speed": None,
        "recorded_at": "2026-09-17T11:59:00+00:00",
    }
    values.update(overrides)
    return ToolCall(**values)  # type: ignore[arg-type]


def snap(runs=(), calls=(), query: SnapshotQuery | None = None, tz=UTC, now=NOW):
    return build_snapshot(
        list(runs), PROJECTS, BASELINE, [], query or SnapshotQuery(), now=now,
        tz=tz, activity_mtime_of=lambda r: None, progress_of=lambda r: None,
        tool_calls=list(calls),
    )


def test_totals_include_approved_calls_only() -> None:
    calls = [
        call("a", project_id="P1"),
        call("b", project_id="PP"),
        call("c", project_id=None),
    ]
    result = snap(calls=calls)
    all_time = result["normal_totals"]["all_time"]
    assert all_time["calls"] == 1
    assert all_time["runs"] == 0
    assert all_time["known_cost"] == pytest.approx(1.0)
    assert all_time["tokens_sent"]["known_total"] == 1_000_000
    assert result["project_totals"]["P1"]["calls"] == 1
    assert result["project_totals"]["P1"]["known_cost"] == pytest.approx(1.0)
    assert "PP" not in result["project_totals"]
    assert SENTINEL not in repr(result)


def test_by_tool_keys_counts_and_sessions() -> None:
    runs = [run("r1")]
    calls = [
        call("a", session="s1", root_session="s1"),
        call("b", session="sub", root_session="s1", tokens_returned=100),
        call("c", session="s2", root_session="s2", cache_read=None),
        call("d", tool="codex", session="x", root_session="x", model="beta"),
    ]
    result = snap(runs, calls)
    by_tool = result["normal_totals"]["all_time"]["by_tool"]
    assert set(by_tool) == {"engine", "claude_code", "codex"}
    assert by_tool["engine"]["count"] == 1
    assert by_tool["engine"]["sessions"] is None
    assert by_tool["engine"]["known_cost"] == pytest.approx(1.0)
    claude = by_tool["claude_code"]
    assert claude["count"] == 3
    assert claude["sessions"] == 2
    assert claude["tokens_sent"] == 3_000_000
    assert claude["tokens_returned"] == 100
    assert claude["cache_read"] == 0
    assert claude["cache_write"] == 0
    assert claude["known_cost"] == pytest.approx(3.0 + 100 * 10 / 1_000_000)
    assert by_tool["codex"]["count"] == 1
    assert by_tool["codex"]["sessions"] == 1
    assert by_tool["codex"]["known_cost"] == pytest.approx(2.0)
    assert result["project_totals"]["P1"]["by_tool"]["codex"]["count"] == 1
    all_time = result["normal_totals"]["all_time"]
    assert all_time["runs"] == 1
    assert all_time["calls"] == 4
    assert all_time["known_cost"] == pytest.approx(6.0 + 100 * 10 / 1_000_000)


def test_by_tool_null_token_sums() -> None:
    result = snap(calls=[call("a", cache_write=None)])
    claude = result["normal_totals"]["all_time"]["by_tool"]["claude_code"]
    assert claude["cache_write"] is None
def test_day_boundaries_follow_local_timezone() -> None:
    tz = timezone(timedelta(hours=2))
    calls = [
        call("late", at="2026-09-16T21:30:00+00:00"),   # 23:30 local, 16th
        call("early", at="2026-09-16T22:30:00+00:00"),  # 00:30 local, 17th
    ]
    result = snap(calls=calls, tz=tz)
    by_day = {row["day"]: row for row in result["spend"]["by_day"]}
    assert set(by_day) == {"2026-09-16", "2026-09-17"}
    assert by_day["2026-09-17"]["calls"] == 1
    assert by_day["2026-09-17"]["runs"] == 0
    assert by_day["2026-09-16"]["calls"] == 1
    assert result["normal_totals"]["today"]["calls"] == 1
    assert result["normal_totals"]["month"]["calls"] == 2


def _table_sum(rows) -> float:
    return sum(row["known_cost"] for row in rows)


def test_spend_tables_include_calls_and_sum_to_all_time() -> None:
    runs = [
        run("r1", label="build", model="alpha"),
        run("r2", label="lint", model="beta", project_id="P2",
            timestamp="2026-09-15T09:00:00+00:00"),
    ]
    calls = [
        call("a", model="beta"),
        call("b", tool="codex", project_id="P2", at="2026-09-14T09:00:00+00:00"),
        call("c", tool="kilo", model=None),
        call("d", project_id="PP"),
    ]
    result = snap(runs, calls)
    spend = result["spend"]
    total = result["normal_totals"]["all_time"]["known_cost"]
    assert total == pytest.approx(1.0 + 2.0 + 2.0 + 1.0)
    for name in ("by_day", "by_project", "by_model", "by_task", "by_tool"):
        assert _table_sum(spend[name]) == pytest.approx(total), name
    tasks = {row["key"]: row for row in spend["by_task"]}
    assert tasks["tool:claude_code"]["calls"] == 1
    assert tasks["tool:claude_code"]["runs"] == 0
    assert tasks["build"]["runs"] == 1
    assert tasks["build"]["calls"] == 0
    tools = {row["key"]: row for row in spend["by_tool"]}
    assert set(tools) == {"engine", "claude_code", "codex", "kilo"}
    assert tools["engine"]["runs"] == 2
    assert tools["engine"]["known_cost"] == pytest.approx(3.0)
    assert tools["kilo"]["calls"] == 1
    assert tools["kilo"]["known_cost"] == 0.0
    models = {row["key"]: row for row in spend["by_model"]}
    assert models["beta"]["runs"] == 1
    assert models["beta"]["calls"] == 1
    projects = {row["key"]: row for row in spend["by_project"]}
    assert projects["P2"]["calls"] == 1
    assert projects["P2"]["runs"] == 1


def test_coverage_counts_calls() -> None:
    calls = [call("a"), call("b", model=None), call("c", cache_write_5m=5),
             call("d", project_id="PP", tokens_sent=None)]
    result = snap([run("r1")], calls)
    assert result["coverage"] == {"exact": 2, "missing_rate": 1, "upper_bound": 1}


def test_tool_filters() -> None:
    runs = [run("r1")]
    calls = [call("a", at="2026-09-17T11:55:00+00:00"),
             call("k", tool="kilo", root_session="k", session="k",
                  at="2026-09-17T11:55:00+00:00")]
    engine = snap(runs, calls, SnapshotQuery(tools=("engine",)))
    totals = engine["normal_totals"]["all_time"]
    assert totals["calls"] == 0 and totals["runs"] == 1
    assert engine["tool_sessions_live"] == []
    kilo = snap(runs, calls, SnapshotQuery(tools=("kilo",)))
    totals = kilo["normal_totals"]["all_time"]
    assert totals["runs"] == 0 and totals["calls"] == 1
    assert set(totals["by_tool"]) == {"kilo"}
    assert [row["tool"] for row in kilo["tool_sessions_live"]] == ["kilo"]
    assert kilo["runs"] == []


def test_project_model_date_and_task_filters_apply_to_calls() -> None:
    calls = [
        call("a", project_id="P1", model="alpha", at="2026-09-17T09:00:00+00:00"),
        call("b", project_id="P2", model="beta", at="2026-09-10T09:00:00+00:00"),
    ]
    by_project = snap(calls=calls, query=SnapshotQuery(project_ids=("P2",)))
    assert by_project["normal_totals"]["all_time"]["calls"] == 1
    assert by_project["spend"]["by_project"][0]["key"] == "P2"
    by_model = snap(calls=calls, query=SnapshotQuery(models=("alpha",)))
    assert by_model["spend"]["by_model"][0]["key"] == "alpha"
    assert by_model["normal_totals"]["all_time"]["calls"] == 1
    by_date = snap(calls=calls, query=SnapshotQuery(date_from=date(2026, 9, 1),
                                                    date_to=date(2026, 9, 12)))
    assert by_date["normal_totals"]["all_time"]["calls"] == 1
    assert by_date["spend"]["by_day"][0]["day"] == "2026-09-10"
    by_task = snap([run("r1")], calls, SnapshotQuery(tasks=("task",)))
    assert by_task["normal_totals"]["all_time"]["calls"] == 0
    assert by_task["normal_totals"]["all_time"]["runs"] == 1
    by_state = snap(calls=calls, query=SnapshotQuery(states=("complete",)))
    assert by_state["normal_totals"]["all_time"]["calls"] == 0
def _ago(seconds: int) -> str:
    return (NOW - timedelta(seconds=seconds)).isoformat()


def test_live_and_recent_windows() -> None:
    calls = [
        call("a0", root_session="live", session="live", at=_ago(900)),
        call("a1", root_session="live", session="live", at=_ago(599)),
        call("l2", root_session="live2", session="live2", at=_ago(10)),
        call("b1", root_session="recent", session="recent", at=_ago(601)),
        call("b2", root_session="recent2", session="recent2", at=_ago(1400)),
        call("c1", root_session="old", session="old", at=_ago(1501)),
    ]
    result = snap(calls=calls)
    live = [row["session"] for row in result["tool_sessions_live"]]
    recent = [row["session"] for row in result["tool_sessions_recent"]]
    assert live == ["live2", "live"]
    assert recent == ["recent", "recent2"]
    assert result["tool_sessions_live"][1]["calls"] == 2
    assert result["tool_sessions_live"][0]["included_in_totals"] is True


def test_live_totals_gain_tool_fields() -> None:
    calls = [
        call("a", root_session="x", session="x", at=_ago(30)),
        call("b", root_session="y", session="y", at=_ago(60), model="beta"),
        call("c", root_session="z", session="z", at=_ago(1000)),
    ]
    totals = snap(calls=calls)["live_totals"]
    assert totals["tool_sessions"] == 2
    assert totals["tool_burn_cost_per_hour"] == pytest.approx(3.0)
    assert totals["runs"] == 0
    unknown = snap(calls=[call("a", at=_ago(30), model=None)])["live_totals"]
    assert unknown["tool_sessions"] == 1
    assert unknown["tool_burn_cost_per_hour"] is None


def test_without_tool_calls_only_new_keys_are_added() -> None:
    result = build_snapshot(
        [run("r1")], PROJECTS, BASELINE, [], SnapshotQuery(), now=NOW, tz=UTC,
        activity_mtime_of=lambda r: None, progress_of=lambda r: None,
    )
    engine_row = {"count": 1, "sessions": None, "known_cost": pytest.approx(1.0),
                  "tokens_sent": 1_000_000, "tokens_returned": 0,
                  "cache_read": 0, "cache_write": 0}
    for period in ("today", "month", "all_time"):
        totals = result["normal_totals"][period]
        assert totals["runs"] == 1
        assert totals["calls"] == 0
        assert totals["by_tool"] == {"engine": engine_row}
    assert result["project_totals"]["P1"]["calls"] == 0
    assert result["project_totals"]["P2"]["by_tool"] == {}
    assert result["tool_sessions_live"] == []
    assert result["tool_sessions_recent"] == []
    assert result["live_totals"]["tool_sessions"] == 0
    assert result["live_totals"]["tool_burn_cost_per_hour"] is None
    spend = result["spend"]
    assert spend["by_model"] == [
        {"key": "alpha", "known_cost": pytest.approx(1.0), "runs": 1, "calls": 0}
    ]
    assert spend["by_day"] == [
        {"day": "2026-09-17", "known_cost": pytest.approx(1.0), "runs": 1, "calls": 0}
    ]
    assert spend["by_tool"] == [
        {"key": "engine", "known_cost": pytest.approx(1.0), "runs": 1, "calls": 0}
    ]
    assert result["coverage"] == {"exact": 1}
    empty = build_snapshot([], PROJECTS, BASELINE, [], SnapshotQuery(), now=NOW, tz=UTC)
    assert empty["normal_totals"]["all_time"]["by_tool"] == {}
    assert empty["spend"]["by_tool"] == []


def test_pending_project_session_is_live_but_not_counted() -> None:
    calls = [call("p", project_id="PP", at=_ago(30))]
    result = snap(calls=calls)
    rows = result["tool_sessions_live"]
    assert len(rows) == 1
    assert rows[0]["included_in_totals"] is False
    assert rows[0]["project_id"] == "PP"
    assert rows[0]["project_name"] == "Project PP"
    assert result["normal_totals"]["all_time"]["calls"] == 0
    assert result["normal_totals"]["all_time"]["known_cost"] == 0.0
    assert result["live_totals"]["tool_sessions"] == 1


def test_each_call_is_priced_at_most_once(monkeypatch) -> None:
    from agent_control import snapshot as snapshot_module

    tools = ("claude_code", "codex", "cline", "kilo")
    calls = [
        call(
            f"k{i}", tool=tools[i % 4], session=f"s{i % 3}",
            root_session=f"s{i % 3}",
            project_id=("P1", "P2", "PP", None)[i % 4],
            model=("alpha", "beta", None)[i % 3],
            at=_ago(60 * i) if i % 2 else f"2026-09-{1 + i % 17:02d}T23:30:00+00:00",
        )
        for i in range(24)
    ]
    runs = [run("r1"), run("r2", project_id="P2", model="beta")]
    tz = timezone(timedelta(hours=2))
    expected = snap(runs=runs, calls=calls, tz=tz)

    counts: dict[int, int] = {}
    original = snapshot_module.calculate_call_cost

    def counting(item, baseline, history):
        counts[id(item)] = counts.get(id(item), 0) + 1
        return original(item, baseline, history)

    monkeypatch.setattr(snapshot_module, "calculate_call_cost", counting)
    result = snap(runs=runs, calls=calls, tz=tz)

    assert result == expected
    assert counts, "calls were never priced"
    assert max(counts.values()) == 1
    assert sum(counts.values()) <= len(calls)


def test_TEMP_timing_40k() -> None:  # THROWAWAY: removed before finishing
    import time
    tools = ("claude_code", "codex", "cline", "kilo")
    calls = [
        call(f"k{i}", tool=tools[i % 4], session=f"s{i % 500}",
             root_session=f"s{i % 500}", project_id=("P1", "P2", "PP")[i % 3],
             model=("alpha", "beta")[i % 2], at=_ago(i * 130))
        for i in range(40_000)
    ]
    runs = [run(f"r{i}", project_id=("P1", "P2")[i % 2]) for i in range(871)]
    times = []
    for _ in range(3):
        start = time.perf_counter()
        snap(runs=runs, calls=calls, tz=timezone(timedelta(hours=2)))
        times.append(time.perf_counter() - start)
    print("\nTIMING build_snapshot 40k calls:", [round(t, 3) for t in times])

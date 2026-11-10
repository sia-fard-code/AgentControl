from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timedelta, timezone

import pytest

from agent_control.models import Liveness, Project, RateSet, RunRecord
from agent_control.progress import RunProgress
from agent_control.snapshot import SnapshotQuery, build_snapshot, live_state
from agent_control.sync import OpenRunObservation

NOW = datetime(2026, 9, 17, 12, 0, tzinfo=timezone.utc)


def liveness(code: str, silence: float | None, last_activity: str | None = None) -> Liveness:
    return Liveness(
        display="running",  # type: ignore[arg-type]
        persist_status="running",  # type: ignore[arg-type]
        terminal=False,
        inferred=False,
        code=code,
        silence_seconds=silence,
        last_activity_at=last_activity,
    )


def observation(
    run_id: str = "a",
    *,
    code: str = "process_identity_valid",
    silence: float | None = None,
    engine_pid: int | None = 4321,
    max_iters: int | None = 40,
    last_activity: str | None = None,
) -> OpenRunObservation:
    return OpenRunObservation(
        run_id=run_id,
        liveness=liveness(code, silence, last_activity),
        engine_pid=engine_pid,
        max_iters=max_iters,
        transcript="a.md",
        messages=None,
        usage_journal=None,
        observed_at="2026-09-17T11:59:00+00:00",
    )


def progress(
    *,
    phase: str = "tools",
    iteration: int | None = 3,
    attempt: int | None = None,
    max_attempts: int | None = None,
    nudges: int = 0,
    last_tool: str | None = None,
    last_path: str | None = None,
) -> RunProgress:
    return RunProgress(
        iteration=iteration,
        phase=phase,  # type: ignore[arg-type]
        attempt=attempt,
        max_attempts=max_attempts,
        nudges=nudges,
        last_tool=last_tool,
        last_path=last_path,
    )


def project(project_id: str, state: str = "approved") -> Project:
    return Project(
        project_id=project_id,
        root=rf"D:\repos\{project_id}",
        root_key=rf"d:\repos\{project_id}".casefold(),
        display_name=project_id,
        state=state,  # type: ignore[arg-type]
        observed_at="2026-09-01T00:00:00+00:00",
        note=None,
    )


def run(
    *,
    run_id: str = "r",
    project_id: str = "P1",
    model: str = "model",
    label: str = "task",
    status: str = "complete",
    timestamp: str = "2026-09-17T09:00:00+00:00",
    tokens_sent: int | None = 100,
    tokens_returned: int | None = 20,
    cache_read: int | None = 0,
    cache_write: int | None = 0,
) -> RunRecord:
    return RunRecord(
        schema=1,
        run_id=run_id,
        revision=1,
        project_id=project_id,
        timestamp=timestamp,
        ended_at=None,
        status=status,  # type: ignore[arg-type]
        label=label,
        model=model,
        effort=None,
        tool_mode=None,
        write_mode=None,
        tool_calls=1,
        refused=0,
        tokens_sent=tokens_sent,
        tokens_returned=tokens_returned,
        cache_read=cache_read,
        cache_write=cache_write,
        sent_includes_cache_read=False,
        tokens_sent_unknown_responses=0 if tokens_sent is not None else 1,
        tokens_returned_unknown_responses=0 if tokens_returned is not None else 1,
        cache_read_unknown_responses=0 if cache_read is not None else 1,
        cache_write_unknown_responses=0 if cache_write is not None else 1,
        usage_source="usage_journal",
        usage_artifact="a.usage.jsonl",
        transcript_artifact="a.md",
        provenance=("machine_index",),
        lifecycle_inferred=False,
        observed_at="2026-09-17T09:05:00+00:00",
    )


def _rec(**kw) -> RunRecord:
    kw.setdefault("run_id", "a")
    kw.setdefault("status", "running")
    kw.setdefault("timestamp", "2026-09-17T11:59:00+00:00")
    return run(**kw)


def test_live_state_verified_long_model_call_is_running() -> None:
    rec = _rec()
    obs = observation(silence=480.0)
    state = live_state(
        rec, obs, progress(phase="model_call", attempt=1, max_attempts=4), NOW, None
    )
    assert state["state"] == "running"
    assert state["attention"] is False
    assert state["dismiss_allowed"] is False


def test_live_state_verified_but_quiet() -> None:
    rec = _rec()
    obs = observation(silence=1200.0)
    state = live_state(rec, obs, None, NOW, None)
    assert state["state"] == "quiet"
    assert state["attention"] is True
    assert state["dismiss_allowed"] is False
    assert "verified alive" in state["reason"]


def test_live_state_quiet_without_observation_allows_dismiss() -> None:
    rec = _rec(timestamp="2026-09-17T11:40:00+00:00")
    state = live_state(rec, None, None, NOW, None)
    assert state["state"] == "quiet"
    assert state["dismiss_allowed"] is True


def test_live_state_schema1_unverified() -> None:
    rec = _rec()
    obs = observation(code="artifact_activity_recent", engine_pid=None, silence=60.0)
    state = live_state(rec, obs, None, NOW, None)
    assert state["state"] == "unverified"
    assert state["attention"] is False
    assert state["reason"] == "no process identity (schema-1 run)"


def test_live_state_probe_unknown_reason() -> None:
    rec = _rec()
    obs = observation(code="probe_unknown:access_denied", silence=60.0)
    state = live_state(rec, obs, None, NOW, None)
    assert state["state"] == "unverified"
    assert state["reason"] == "process probe: access_denied"


def test_live_state_retrying_attempts() -> None:
    rec = _rec()
    obs = observation(silence=60.0)
    two = live_state(rec, obs, progress(phase="model_call", attempt=2, max_attempts=4), NOW, None)
    assert two["state"] == "retrying"
    assert two["attention"] is False
    three = live_state(rec, obs, progress(phase="model_call", attempt=3, max_attempts=4), NOW, None)
    assert three["state"] == "retrying"
    assert three["attention"] is True


def test_live_state_nudges() -> None:
    rec = _rec()
    obs = observation(silence=60.0)
    one = live_state(rec, obs, progress(nudges=1), NOW, None)
    assert one["state"] == "retrying"
    assert one["attention"] is False
    two = live_state(rec, obs, progress(nudges=2), NOW, None)
    assert two["attention"] is True


def test_open_run_silent_hours_is_never_stalled_or_abandoned() -> None:
    rec = _rec(timestamp="2026-09-17T07:00:00+00:00")
    state = live_state(rec, None, None, NOW, None)
    assert state["state"] not in ("stalled", "abandoned")
    assert state["state"] == "quiet"


def test_live_membership_ignores_filters() -> None:
    projects = {"P1": project("P1")}
    open_run = run(run_id="a", status="running", timestamp="2026-09-17T11:59:00+00:00")
    obs = {"a": observation(silence=60.0)}
    for query in (SnapshotQuery(states=("complete",)), SnapshotQuery(project_ids=("OTHER",))):
        snap = build_snapshot(
            [open_run], projects, {}, [], query, now=NOW,
            observations=obs, progress_of=lambda r: None,
        )
        assert [row["run_id"] for row in snap["live"]] == ["a"]


def test_live_row_carries_project_state_and_totals_flag() -> None:
    projects = {"P1": project("P1", state="discovered")}
    open_run = run(run_id="a", status="running", timestamp="2026-09-17T11:59:00+00:00")
    snap = build_snapshot(
        [open_run], projects, {}, [], SnapshotQuery(), now=NOW,
        observations={"a": observation(silence=60.0)}, progress_of=lambda r: None,
    )
    row = snap["live"][0]
    assert row["project_state"] == "discovered"
    assert row["included_in_totals"] is False


def test_dismissed_run_in_live_when_engine_alive() -> None:
    projects = {"P1": project("P1")}
    rec = run(run_id="d", status="interrupted", timestamp="2026-09-17T11:59:00+00:00")
    rec = replace(rec, provenance=("machine_index", "operator_dismiss"))
    obs = {"d": observation("d", silence=60.0)}
    snap = build_snapshot(
        [rec], projects, {}, [], SnapshotQuery(), now=NOW,
        observations=obs, progress_of=lambda r: None,
    )
    row = snap["live"][0]
    assert row["dismissed"] is True
    assert row["dismiss_allowed"] is False
    assert row["state_reason"].startswith("dismissed by operator; ")


def test_dismissed_run_without_observation_not_in_live() -> None:
    projects = {"P1": project("P1")}
    rec = run(run_id="d", status="interrupted", timestamp="2026-09-17T11:59:00+00:00")
    rec = replace(rec, provenance=("machine_index", "operator_dismiss"))
    snap = build_snapshot(
        [rec], projects, {}, [], SnapshotQuery(), now=NOW,
        observations={}, progress_of=lambda r: None,
    )
    assert snap["live"] == []


def test_live_sort_attention_first_then_newest() -> None:
    projects = {"P1": project("P1")}
    quiet = run(run_id="quiet", status="running", timestamp="2026-09-17T11:40:00+00:00")
    fresh = run(run_id="fresh", status="running", timestamp="2026-09-17T11:59:00+00:00")
    obs = {
        "quiet": observation("quiet", silence=1200.0),
        "fresh": observation("fresh", silence=30.0),
    }
    snap = build_snapshot(
        [fresh, quiet], projects, {}, [], SnapshotQuery(), now=NOW,
        observations=obs, progress_of=lambda r: None,
    )
    assert [row["run_id"] for row in snap["live"]] == ["quiet", "fresh"]


def test_live_row_has_elapsed_and_max_iters_and_null_progress() -> None:
    projects = {"P1": project("P1")}
    open_run = run(run_id="a", status="running", timestamp="2026-09-17T11:59:00+00:00")
    snap = build_snapshot(
        [open_run], projects, {}, [], SnapshotQuery(), now=NOW,
        observations={"a": observation(silence=60.0, max_iters=40)},
        progress_of=lambda r: None,
    )
    row = snap["live"][0]
    assert row["elapsed_seconds"] == 60
    assert row["max_iters"] == 40
    for field_name in ("phase", "iteration", "attempt", "max_attempts", "nudges", "last_tool", "last_path"):
        assert row[field_name] is None


def test_history_rows_do_not_compute_live_state() -> None:
    projects = {"P1": project("P1")}
    open_run = run(run_id="a", status="running", timestamp="2026-09-17T11:59:00+00:00")
    snap = build_snapshot(
        [open_run], projects, {}, [], SnapshotQuery(), now=NOW,
        observations={"a": observation(silence=60.0)}, progress_of=lambda r: None,
    )
    row = snap["runs"][0]
    assert row["display_state"] == "running"
    assert row["silence_seconds"] is None
    assert row["last_activity_at"] is None


def test_terminal_run_keeps_its_status_as_display() -> None:
    projects = {"P1": project("P1")}
    done = run(run_id="a", status="complete", timestamp="2026-09-17T09:00:00+00:00")
    snap = build_snapshot(
        [done], projects, {}, [], SnapshotQuery(), now=NOW, activity_mtime_of=lambda r: None
    )
    row = snap["runs"][0]
    assert row["status"] == "complete"
    assert row["display_state"] == "complete"
    assert row["silence_seconds"] is None


def test_pending_is_diagnostic_but_excluded_until_approval() -> None:
    runs = [run(run_id="r1", project_id="P1")]
    pending = {"P1": project("P1", state="discovered")}
    before = build_snapshot(runs, pending, {}, [], SnapshotQuery(), now=NOW)
    diagnostic = build_snapshot(
        runs, pending, {}, [], SnapshotQuery(include_excluded=True), now=NOW
    )
    assert before["normal_totals"]["all_time"]["runs"] == 0
    assert diagnostic["runs"][0]["included_in_totals"] is False

    approved = {"P1": replace(pending["P1"], state="approved")}
    after = build_snapshot(runs, approved, {}, [], SnapshotQuery(), now=NOW)
    assert after["normal_totals"]["all_time"]["runs"] == 1


def test_rejected_and_disabled_are_excluded() -> None:
    runs = [run(run_id="r1", project_id="P1"), run(run_id="r2", project_id="P2")]
    projects = {"P1": project("P1", "rejected"), "P2": project("P2", "disabled")}
    snapshot = build_snapshot(runs, projects, {}, [], SnapshotQuery(), now=NOW)
    assert snapshot["normal_totals"]["all_time"]["runs"] == 0


def test_filters_by_model_and_projectand_state() -> None:
    runs = [
        run(run_id="r1", project_id="P1", model="alpha"),
        run(run_id="r2", project_id="P1", model="beta"),
        run(run_id="r3", project_id="P2", model="alpha", status="failed"),
    ]
    projects = {"P1": project("P1"), "P2": project("P2")}
    only_alpha = build_snapshot(
        runs, projects, {}, [], SnapshotQuery(models=("alpha",)), now=NOW
    )
    assert {row["run_id"] for row in only_alpha["runs"]} == {"r1", "r3"}
    only_p1 = build_snapshot(
        runs, projects, {}, [], SnapshotQuery(project_ids=("P1",)), now=NOW
    )
    assert {row["run_id"] for row in only_p1["runs"]} == {"r1", "r2"}
    only_failed = build_snapshot(
        runs, projects, {}, [], SnapshotQuery(states=("failed",)), now=NOW
    )
    assert {row["run_id"] for row in only_failed["runs"]} == {"r3"}


def test_inclusive_date_filter_and_invalid_range() -> None:
    runs = [
        run(run_id="r1", timestamp="2026-09-16T23:00:00+00:00"),
        run(run_id="r2", timestamp="2026-09-17T09:00:00+00:00"),
    ]
    projects = {"P1": project("P1")}
    inclusive = build_snapshot(
        runs,
        projects,
        {},
        [],
        SnapshotQuery(date_from=date(2026, 9, 17), date_to=date(2026, 9, 17)),
        now=NOW,
        tz=timezone.utc,
    )
    assert {row["run_id"] for row in inclusive["runs"]} == {"r2"}
    with pytest.raises(ValueError):
        build_snapshot(
            runs,
            projects,
            {},
            [],
            SnapshotQuery(date_from=date(2026, 9, 18), date_to=date(2026, 9, 17)),
            now=NOW,
        )


def test_today_month_all_time_periods() -> None:
    runs = [
        run(run_id="today", timestamp="2026-09-17T09:00:00+00:00"),
        run(run_id="month", timestamp="2026-09-02T09:00:00+00:00"),
        run(run_id="old", timestamp="2026-07-01T09:00:00+00:00"),
    ]
    projects = {"P1": project("P1")}
    snapshot = build_snapshot(
        runs, projects, {}, [], SnapshotQuery(), now=NOW, tz=timezone.utc
    )
    totals = snapshot["normal_totals"]
    assert totals["today"]["runs"] == 1
    assert totals["month"]["runs"] == 2
    assert totals["all_time"]["runs"] == 3


def test_counter_shape_distinguishes_unknown_and_partial() -> None:
    runs = [
        run(run_id="known", tokens_sent=100),
        run(run_id="unknown", tokens_sent=None),
    ]
    projects = {"P1": project("P1")}
    snapshot = build_snapshot(runs, projects, {}, [], SnapshotQuery(), now=NOW)
    tokens_sent = snapshot["normal_totals"]["all_time"]["tokens_sent"]
    assert tokens_sent["known_total"] == 100
    assert tokens_sent["unknown_rows"] == 1
    assert tokens_sent["partial"] is True


def test_spend_groups_rank_by_known_cost_descending() -> None:
    baseline = {"alpha": RateSet(5, 15, 0.5, 6), "beta": RateSet(1, 1, 0, 0)}
    runs = [
        run(run_id="r1", model="alpha", tokens_sent=1000, tokens_returned=0),
        run(run_id="r2", model="beta", tokens_sent=1000, tokens_returned=0),
    ]
    projects = {"P1": project("P1")}
    snapshot = build_snapshot(runs, projects, baseline, [], SnapshotQuery(), now=NOW)
    models = snapshot["spend"]["by_model"]
    assert models[0]["key"] == "alpha"
    assert models[0]["known_cost"] >= models[1]["known_cost"]


def test_days_are_newest_first() -> None:
    runs = [
        run(run_id="older", timestamp="2026-09-15T09:00:00+00:00"),
        run(run_id="newer", timestamp="2026-09-17T09:00:00+00:00"),
    ]
    projects = {"P1": project("P1")}
    snapshot = build_snapshot(
        runs, projects, {}, [], SnapshotQuery(), now=NOW, tz=timezone.utc
    )
    days = [entry["day"] for entry in snapshot["spend"]["by_day"]]
    assert days == ["2026-09-17", "2026-09-15"]


def test_coverage_counts_classifications() -> None:
    runs = [
        run(run_id="exact", tokens_sent=10, cache_read=1, cache_write=1),
        run(run_id="missing", tokens_sent=None),
    ]
    projects = {"P1": project("P1")}
    baseline = {"model": RateSet(5, 15, 0.5, 6)}
    snapshot = build_snapshot(runs, projects, baseline, [], SnapshotQuery(), now=NOW)
    coverage = snapshot["coverage"]
    assert coverage["missing_usage"] == 1
    assert coverage["exact"] == 1


def test_runs_hide_absolute_artifact_paths() -> None:
    runs = [run(run_id="r1")]
    projects = {"P1": project("P1")}
    snapshot = build_snapshot(runs, projects, {}, [], SnapshotQuery(), now=NOW)
    row = snapshot["runs"][0]
    assert "usage_artifact" not in row
    assert "transcript_artifact" not in row


def test_recently_ended_includes_recent_complete_run() -> None:
    projects = {"P1": project("P1")}
    rec = replace(
        run(run_id="a", status="complete"),
        ended_at="2026-09-17T11:55:00+00:00",
    )
    snap = build_snapshot(
        [rec], projects, {}, [], SnapshotQuery(), now=NOW, tz=timezone.utc
    )
    rows = snap["recently_ended"]
    assert [r["run_id"] for r in rows] == ["a"]
    row = rows[0]
    assert row["end_reason"] == "completed"
    assert row["attention"] is False
    assert row["dismissed"] is False
    assert row["ended_at_effective"] == "2026-09-17T11:55:00+00:00"
    assert row["project_state"] == "approved"


def test_recently_ended_interrupted_has_attention() -> None:
    projects = {"P1": project("P1")}
    rec = replace(
        run(run_id="a", status="interrupted"),
        ended_at="2026-09-17T11:55:00+00:00",
    )
    snap = build_snapshot(
        [rec], projects, {}, [], SnapshotQuery(), now=NOW, tz=timezone.utc
    )
    row = snap["recently_ended"][0]
    assert row["attention"] is True
    assert row["end_reason"] == "interrupted"


def test_recently_ended_dismissed_reason() -> None:
    projects = {"P1": project("P1")}
    rec = replace(
        run(run_id="a", status="interrupted"),
        ended_at="2026-09-17T11:55:00+00:00",
        provenance=("machine_index", "operator_dismiss"),
    )
    snap = build_snapshot(
        [rec], projects, {}, [], SnapshotQuery(), now=NOW, tz=timezone.utc,
        observations={}, progress_of=lambda r: None,
    )
    row = snap["recently_ended"][0]
    assert row["dismissed"] is True
    assert row["end_reason"] == "dismissed by operator"
    assert row["attention"] is False


def test_recently_ended_excludes_old_run() -> None:
    projects = {"P1": project("P1")}
    rec = replace(
        run(run_id="a", status="complete"),
        ended_at="2026-09-17T11:40:00+00:00",
    )
    snap = build_snapshot(
        [rec], projects, {}, [], SnapshotQuery(), now=NOW, tz=timezone.utc
    )
    assert snap["recently_ended"] == []


def test_recently_ended_uses_observed_at_when_ended_at_none() -> None:
    projects = {"P1": project("P1")}
    rec = replace(
        run(run_id="a", status="complete"),
        ended_at=None,
        observed_at="2026-09-17T11:55:00+00:00",
        lifecycle_inferred=True,
    )
    snap = build_snapshot(
        [rec], projects, {}, [], SnapshotQuery(), now=NOW, tz=timezone.utc
    )
    row = snap["recently_ended"][0]
    assert row["ended_at_effective"] == "2026-09-17T11:55:00+00:00"
    assert row["end_reason"] == "completed (inferred)"


def test_recently_ended_end_record_source() -> None:
    projects = {"P1": project("P1")}
    rec = replace(
        run(run_id="a", status="complete"),
        ended_at="2026-09-17T11:55:00+00:00",
    )
    obs = {"a": observation("a", last_activity="2026-09-17T11:50:00+00:00")}
    snap = build_snapshot(
        [rec], projects, {}, [], SnapshotQuery(), now=NOW, tz=timezone.utc,
        observations=obs, progress_of=lambda r: None,
    )
    row = snap["recently_ended"][0]
    assert row["ended_at_effective"] == "2026-09-17T11:55:00+00:00"
    assert row["end_time_source"] == "end_record"


def test_recently_ended_uses_last_activity_for_process_gone() -> None:
    projects = {"P1": project("P1")}
    last_activity = "2026-09-17T11:54:00+00:00"
    rec = replace(
        run(run_id="a", status="failed"),
        ended_at=None,
        observed_at="2026-09-17T12:00:00+00:00",
        lifecycle_inferred=True,
    )
    obs = {"a": observation("a", code="process_gone", last_activity=last_activity)}
    snap = build_snapshot(
        [rec], projects, {}, [], SnapshotQuery(), now=NOW, tz=timezone.utc,
        observations=obs, progress_of=lambda r: None,
    )
    row = snap["recently_ended"][0]
    assert row["ended_at_effective"] == last_activity
    assert row["end_time_source"] == "last_activity"


def test_recently_ended_reaped_run_excluded_by_last_activity() -> None:
    projects = {"P1": project("P1")}
    rec = replace(
        run(run_id="a", status="failed"),
        ended_at=None,
        observed_at="2026-09-17T12:00:00+00:00",
        lifecycle_inferred=True,
    )
    obs = {
        "a": observation(
            "a", code="process_gone", last_activity="2026-09-17T09:00:00+00:00"
        )
    }
    snap = build_snapshot(
        [rec], projects, {}, [], SnapshotQuery(), now=NOW, tz=timezone.utc,
        observations=obs, progress_of=lambda r: None,
    )
    assert snap["recently_ended"] == []


def test_recently_ended_observed_source_without_observation() -> None:
    projects = {"P1": project("P1")}
    rec = replace(
        run(run_id="a", status="complete"),
        ended_at=None,
        observed_at="2026-09-17T11:55:00+00:00",
        lifecycle_inferred=True,
    )
    snap = build_snapshot(
        [rec], projects, {}, [], SnapshotQuery(), now=NOW, tz=timezone.utc
    )
    row = snap["recently_ended"][0]
    assert row["ended_at_effective"] == "2026-09-17T11:55:00+00:00"
    assert row["end_time_source"] == "observed"


def test_recently_ended_newest_first_capped_at_ten() -> None:
    projects = {"P1": project("P1")}
    runs = [
        replace(
            run(run_id=f"r{i}", status="complete"),
            ended_at=f"2026-09-17T11:55:{i:02d}+00:00",
        )
        for i in range(12)
    ]
    snap = build_snapshot(
        runs, projects, {}, [], SnapshotQuery(), now=NOW, tz=timezone.utc
    )
    rows = snap["recently_ended"]
    assert len(rows) == 10
    ends = [r["ended_at_effective"] for r in rows]
    assert ends == sorted(ends, reverse=True)


def test_recently_ended_ignores_query_filters() -> None:
    projects = {"P1": project("P1")}
    rec = replace(
        run(run_id="a", status="complete"),
        ended_at="2026-09-17T11:55:00+00:00",
    )
    snap = build_snapshot(
        [rec], projects, {}, [],
        SnapshotQuery(states=("failed",), project_ids=("OTHER",)),
        now=NOW, tz=timezone.utc,
    )
    assert [r["run_id"] for r in snap["recently_ended"]] == ["a"]


def test_recently_ended_excludes_live_rows() -> None:
    projects = {"P1": project("P1")}
    rec = replace(
        run(run_id="d", status="interrupted", timestamp="2026-09-17T11:59:00+00:00"),
        ended_at="2026-09-17T11:55:00+00:00",
        provenance=("machine_index", "operator_dismiss"),
    )
    obs = {"d": observation("d", silence=60.0)}
    snap = build_snapshot(
        [rec], projects, {}, [], SnapshotQuery(), now=NOW, tz=timezone.utc,
        observations=obs, progress_of=lambda r: None,
    )
    assert [r["run_id"] for r in snap["live"]] == ["d"]
    assert snap["recently_ended"] == []


def test_local_dates_shift_today_bucket() -> None:
    projects = {"P1": project("P1")}
    rec = run(run_id="a", status="complete", timestamp="2026-09-16T23:30:00+00:00")
    tz = timezone(timedelta(hours=2))
    local = build_snapshot([rec], projects, {}, [], SnapshotQuery(), now=NOW, tz=tz)
    assert local["normal_totals"]["today"]["runs"] == 1
    assert local["timezone"] == "+02:00"
    utc = build_snapshot(
        [rec], projects, {}, [], SnapshotQuery(), now=NOW, tz=timezone.utc
    )
    assert utc["normal_totals"]["today"]["runs"] == 0


def test_live_totals_sums_and_burn() -> None:
    projects = {"P1": project("P1")}
    baseline = {"alpha": RateSet(5, 15, 0.5, 6)}
    a = run(
        run_id="a", model="alpha", status="running",
        timestamp="2026-09-17T11:00:00+00:00",
        tokens_sent=1_000_000, tokens_returned=0,
    )
    b = run(
        run_id="b", model="alpha", status="running",
        timestamp="2026-09-17T11:30:00+00:00",
        tokens_sent=1_000_000, tokens_returned=0,
    )
    obs = {"a": observation("a", silence=60.0), "b": observation("b", silence=60.0)}
    snap = build_snapshot(
        [a, b], projects, baseline, [], SnapshotQuery(), now=NOW, tz=timezone.utc,
        observations=obs, progress_of=lambda r: None,
    )
    totals = snap["live_totals"]
    assert totals["runs"] == 2
    assert totals["known_cost"] == pytest.approx(10.0)
    assert totals["cost_unknown_runs"] == 0
    assert totals["tokens_sent"] == 2_000_000
    assert totals["tokens_returned"] == 0
    # a: $5 over 3600s -> 5/hr; b: $5 over 1800s -> 10/hr
    assert totals["burn_per_hour"] == pytest.approx(15.0)


def test_live_totals_unknown_cost_and_empty() -> None:
    projects = {"P1": project("P1")}
    rec = run(
        run_id="a", status="running", timestamp="2026-09-17T11:00:00+00:00",
        tokens_sent=None, tokens_returned=None,
    )
    obs = {"a": observation("a", silence=60.0)}
    snap = build_snapshot(
        [rec], projects, {}, [], SnapshotQuery(), now=NOW, tz=timezone.utc,
        observations=obs, progress_of=lambda r: None,
    )
    totals = snap["live_totals"]
    assert totals["cost_unknown_runs"] == 1
    assert totals["tokens_sent"] is None
    assert totals["burn_per_hour"] is None

    empty = build_snapshot(
        [], projects, {}, [], SnapshotQuery(), now=NOW, tz=timezone.utc
    )
    assert empty["live_totals"]["runs"] == 0
    assert empty["live_totals"]["known_cost"] == 0.0
    assert empty["live_totals"]["burn_per_hour"] is None


def test_verified_live_row_can_steer_and_kill() -> None:
    projects = {"P1": project("P1")}
    rec = run(run_id="a", status="running", timestamp="2026-09-17T11:59:00+00:00")
    obs = {"a": observation("a", silence=60.0)}
    snap = build_snapshot(
        [rec], projects, {}, [], SnapshotQuery(), now=NOW,
        observations=obs, progress_of=lambda r: None,
    )
    row = snap["live"][0]
    assert row["can_steer"] is True
    assert row["can_kill"] is True
    assert row["control_reason"] is None


def test_schema1_row_reason() -> None:
    projects = {"P1": project("P1")}
    rec = run(run_id="a", status="running", timestamp="2026-09-17T11:59:00+00:00")
    obs = {"a": observation("a", code="probe_unknown:no_pid", silence=60.0, engine_pid=None)}
    snap = build_snapshot(
        [rec], projects, {}, [], SnapshotQuery(), now=NOW,
        observations=obs, progress_of=lambda r: None,
    )
    row = snap["live"][0]
    assert row["can_steer"] is False
    assert row["can_kill"] is False
    assert row["control_reason"] == "no process identity (schema-1 run)"


def test_unverified_row_reason() -> None:
    projects = {"P1": project("P1")}
    rec = run(run_id="a", status="running", timestamp="2026-09-17T11:59:00+00:00")
    obs = {"a": observation("a", code="probe_unknown:access_denied", silence=60.0)}
    snap = build_snapshot(
        [rec], projects, {}, [], SnapshotQuery(), now=NOW,
        observations=obs, progress_of=lambda r: None,
    )
    row = snap["live"][0]
    assert row["can_steer"] is False
    assert row["can_kill"] is False
    assert row["control_reason"] == "engine process not verified alive"


def test_no_transcript_blocks_steer_only() -> None:
    projects = {"P1": project("P1")}
    rec = run(run_id="a", status="running", timestamp="2026-09-17T11:59:00+00:00")
    rec = replace(rec, transcript_artifact="")
    obs = {"a": observation("a", silence=60.0)}
    snap = build_snapshot(
        [rec], projects, {}, [], SnapshotQuery(), now=NOW,
        observations=obs, progress_of=lambda r: None,
    )
    row = snap["live"][0]
    assert row["can_kill"] is True
    assert row["can_steer"] is False
    assert row["control_reason"] == "no transcript recorded"


def test_dismissed_verified_row() -> None:
    projects = {"P1": project("P1")}
    rec = run(run_id="d", status="interrupted", timestamp="2026-09-17T11:59:00+00:00")
    rec = replace(rec, provenance=("machine_index", "operator_dismiss"))
    obs = {"d": observation("d", silence=60.0)}
    snap = build_snapshot(
        [rec], projects, {}, [], SnapshotQuery(), now=NOW,
        observations=obs, progress_of=lambda r: None,
    )
    row = snap["live"][0]
    assert row["can_steer"] is False
    assert row["can_kill"] is True
    assert row["control_reason"] == "dismissed by operator"


def test_recently_ended_killed_reason() -> None:
    projects = {"P1": project("P1")}
    rec = run(
        run_id="k", status="interrupted",
        timestamp="2026-09-17T11:58:00+00:00",
    )
    rec = replace(rec, ended_at="2026-09-17T11:59:00+00:00")
    actions = [{
        "schema": 1, "ts": "2026-09-17T11:59:00+00:00",
        "action": "kill", "run_id": "k", "outcome": "terminated",
    }]
    snap = build_snapshot(
        [rec], projects, {}, [], SnapshotQuery(), now=NOW,
        observations={}, progress_of=lambda r: None,
        operator_actions=actions,
    )
    row = snap["recently_ended"][0]
    assert row["end_reason"] == "killed by operator"


def test_kill_already_gone_is_not_a_kill() -> None:
    projects = {"P1": project("P1")}
    rec = run(
        run_id="k", status="interrupted",
        timestamp="2026-09-17T11:58:00+00:00",
    )
    rec = replace(rec, ended_at="2026-09-17T11:59:00+00:00")
    actions = [{
        "schema": 1, "ts": "2026-09-17T11:59:00+00:00",
        "action": "kill", "run_id": "k", "outcome": "already_gone",
    }]
    snap = build_snapshot(
        [rec], projects, {}, [], SnapshotQuery(), now=NOW,
        observations={}, progress_of=lambda r: None,
        operator_actions=actions,
    )
    row = snap["recently_ended"][0]
    assert row["end_reason"] != "killed by operator"


def test_history_row_flag() -> None:
    projects = {"P1": project("P1")}
    killed = run(run_id="k", status="interrupted", timestamp="2026-09-17T09:00:00+00:00")
    other = run(run_id="o", status="complete", timestamp="2026-09-17T09:00:00+00:00")
    actions = [{
        "schema": 1, "ts": "2026-09-17T11:59:00+00:00",
        "action": "kill", "run_id": "k", "outcome": "terminated",
    }]
    snap = build_snapshot(
        [killed, other], projects, {}, [], SnapshotQuery(), now=NOW,
        observations={}, progress_of=lambda r: None,
        operator_actions=actions,
    )
    for row in snap["runs"]:
        if row["run_id"] == "k":
            assert row["killed_by_operator"] is True
        else:
            assert row["killed_by_operator"] is False


def test_no_actions_keeps_reasons() -> None:
    projects = {"P1": project("P1")}
    rec = run(
        run_id="k", status="interrupted",
        timestamp="2026-09-17T11:58:00+00:00",
    )
    rec = replace(rec, ended_at="2026-09-17T11:59:00+00:00")
    snap = build_snapshot(
        [rec], projects, {}, [], SnapshotQuery(), now=NOW,
        observations={}, progress_of=lambda r: None,
        operator_actions=None,
    )
    row = snap["recently_ended"][0]
    assert row["end_reason"] == "interrupted"


def test_cut_progress_is_retrying_with_attention_from_the_second():
    from agent_control.progress import RunProgress
    from agent_control.snapshot import _classify

    def progress(cuts):
        return RunProgress(iteration=4, phase="model_call", attempt=1, max_attempts=4,
                           nudges=0, last_tool=None, last_path=None, cuts=cuts)

    state, reason, attention = _classify(None, progress(1), False, 5.0)
    assert (state, reason, attention) == (
        "retrying", "reply cut at the gateway's 2-minute limit", False)
    state, reason, attention = _classify(None, progress(2), False, 5.0)
    assert (state, reason, attention) == (
        "retrying", "2 replies cut at the gateway's 2-minute limit", True)

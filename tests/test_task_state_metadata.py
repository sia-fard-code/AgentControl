"""Phase6 metadata-only logical task state: trusted opt-in source tests (synthetic tmp_path only)."""
from __future__ import annotations

import json
import os
from dataclasses import asdict, replace
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from agent_control.dashboard import DashboardApp
from agent_control.snapshot import SnapshotQuery, build_snapshot
from agent_control import models
from agent_control.task_state import parse_task_status, TaskStatusError

SENTINEL = "SENTINEL_PAYLOAD_DO_NOT_LEAK"
ROOT_SHA = "a" * 64


@pytest.fixture
def source(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    parent_state = tmp_path / "parent-state"
    parent_state.mkdir()
    return models.TaskStatusSource(
        project_id="p1", project_root=str(root), envelope_sha256=ROOT_SHA,
        path=parent_state / "task-status.json",
    )


def metadata(source, state="continuing"):
    reason, owner = {
        "continuing": ("planning", "none"),
        "awaiting-verification": ("verification_due", "coordinator"),
        "reconciliation": ("effect_uncertain", "none"),
        "blocked": ("operator_required", "none"),
        "verified-complete": ("verified", "none"),
    }[state]
    return {
        "schema_version": 1, "orchestration_id": "goal-1",
        "envelope_sha256": ROOT_SHA, "project_root": source.project_root,
        "owner_epoch": 1, "state": state, "reason": reason,
        "candidate_sha256": "b" * 64, "seed_receipt_sha256": None,
        "plan_revision": None, "active_workers": 0, "unresolved_workers": 0,
        "pending_evaluations": 0, "verifier_owner": owner,
        "rerun_owed": state == "awaiting-verification",
        "satisfied_criteria": ["criterion-1"] if state == "verified-complete" else [],
        "required_criteria": ["criterion-1"],
        "usage": {"charged": 0, "reserved": 0, "unknown_calls": 0, "total_usage": None},
        "limits": {
            "max_replans": None, "max_total_attempts": None,
            "max_concurrent_workers": 8, "max_planner_calls": None,
            "max_final_report_calls": 1, "stop_grace_seconds": 60,
            "deadline": None, "max_usage": None, "worker_turn_limit": 0,
        },
    }


@pytest.mark.parametrize("state", [
    "continuing", "awaiting-verification", "reconciliation", "blocked", "verified-complete",
])
def test_all_states_are_frozen_safe_typed_observations(source, state):
    raw = metadata(source, state)
    observed = parse_task_status(json.dumps(raw).encode(), source)
    assert observed.project_id == "p1"
    assert observed.state == state
    assert observed.reason == raw["reason"]
    assert observed.observation == "last-parent-observation"
    assert observed.usage.total_usage is None
    assert observed.usage.charged == 0
    assert observed.limits.max_usage is None
    assert observed.limits.worker_turn_limit == 0
    assert "project_root" not in asdict(observed)
    assert "path" not in asdict(observed)
    with pytest.raises(Exception):
        observed.state = "blocked"
    raw["usage"]["charged"] = 12
    assert observed.usage.charged == 0


def test_null_and_zero_are_distinct(source):
    raw = metadata(source)
    raw["usage"]["total_usage"] = 0
    raw["limits"].update(max_usage=0, deadline=0, max_replans=0)
    observed = parse_task_status(raw, source)
    assert observed.usage.total_usage == 0
    assert observed.limits.max_usage == observed.limits.deadline == observed.limits.max_replans == 0
    assert observed.plan_revision is None


@pytest.mark.parametrize("field,value", [
    ("schema_version", 2), ("schema_version", True),
    ("envelope_sha256", "c" * 64), ("project_root", "/unapproved-root"),
    ("active_workers", True), ("owner_epoch", -1), ("plan_revision", 10**15 + 1),
    ("reason", SENTINEL), ("state", SENTINEL), ("orchestration_id", SENTINEL + "\n"),
    ("candidate_sha256", SENTINEL), ("verifier_owner", SENTINEL),
    ("rerun_owed", 0), ("required_criteria", [SENTINEL + "\n"]),
    ("satisfied_criteria", ["unknown"]), ("required_criteria", ["x", "x"]),
    ("required_criteria", ["x"] * 33),
    ("usage", {"charged": 0}), ("limits", {"max_usage": 0}),
    ("payload", SENTINEL),
])
def test_closed_schema_binding_and_safe_errors(source, field, value):
    raw = metadata(source)
    raw[field] = value
    with pytest.raises(TaskStatusError) as error:
        parse_task_status(raw, source)
    assert SENTINEL not in str(error.value)
    assert source.project_root not in str(error.value)


@pytest.mark.parametrize("section,key,value", [
    ("usage", "charged", True), ("usage", "reserved", None),
    ("usage", "unknown_calls", -1), ("usage", "total_usage", float("inf")),
    ("usage", "payload", SENTINEL), ("limits", "max_usage", True),
    ("limits", "deadline", float("nan")), ("limits", "payload", SENTINEL),
])
def test_nested_validation(source, section, key, value):
    raw = metadata(source)
    raw[section][key] = value
    with pytest.raises(TaskStatusError):
        parse_task_status(raw, source)


@pytest.mark.parametrize("field,value", [
    ("reason", "planning"), ("satisfied_criteria", []), ("candidate_sha256", None),
    ("verifier_owner", "coordinator"), ("rerun_owed", True),
    ("active_workers", 1), ("unresolved_workers", 1), ("pending_evaluations", 1),
])
def test_verified_complete_is_not_inferred_from_label(source, field, value):
    raw = metadata(source, "verified-complete")
    raw[field] = value
    with pytest.raises(TaskStatusError):
        parse_task_status(raw, source)


@pytest.mark.parametrize("raw", [
    b'{"schema_version":1,"schema_version":1}', b'{"x":NaN}', b'\xff', b'{}' * 9000,
])
def test_strict_bounded_json(source, raw):
    with pytest.raises(TaskStatusError):
        parse_task_status(raw, source)


def test_binding_is_lexical_without_raw_path_probes(source, monkeypatch):
    raw = metadata(source)
    raw["project_root"] = os.path.join(source.project_root, ".")
    def forbidden(*args, **kwargs):
        raise AssertionError("raw path filesystem probe")
    monkeypatch.setattr(Path, "stat", forbidden)
    monkeypatch.setattr(Path, "resolve", forbidden)
    assert parse_task_status(raw, source).project_id == "p1"


@pytest.fixture
def project(source):
    return models.Project("p1", source.project_root, source.project_root, "Synthetic", "approved", "2026-01-01T00:00:00Z", None)


NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def publish(path, text):
    path = Path(path)
    path.write_text(text, encoding="utf-8")
    os.utime(path, (NOW.timestamp(), NOW.timestamp()))


def snapshot(project, **kwargs):
    return build_snapshot([], {project.project_id: project}, {}, [], SnapshotQuery(), now=NOW, **kwargs)


def test_default_snapshot_unchanged_and_explicit_empty(project):
    legacy = snapshot(project)
    assert "goal_tasks" not in legacy
    assert snapshot(project, task_statuses=None) == legacy
    configured = snapshot(project, task_statuses=[])
    assert configured.pop("goal_tasks") == []
    assert configured == legacy


def test_rows_safe_and_project_visibility(source, project):
    observed = parse_task_status(metadata(source, "verified-complete"), source)
    rows = snapshot(project, task_statuses=[observed])["goal_tasks"]
    assert rows == [asdict(observed)]
    encoded = json.dumps(rows)
    assert source.project_root not in encoded
    assert str(source.path) not in encoded
    assert SENTINEL not in encoded
    assert snapshot(replace(project, state="disabled"), task_statuses=[observed])["goal_tasks"] == []
    assert build_snapshot([], {}, {}, [], SnapshotQuery(), now=NOW, task_statuses=[observed])["goal_tasks"] == []


@pytest.fixture
def app_factory(tmp_path, monkeypatch, project):
    from agent_control import dashboard
    from agent_control.storage import StateLocks
    paths = SimpleNamespace(state_dir=tmp_path / "dashboard-state")
    paths.state_dir.mkdir()
    for key in ("projects", "ledger", "baseline_pricing", "pricing_history", "tool_usage"):
        setattr(paths, key, paths.state_dir / (key + ".jsonl"))
    monkeypatch.setattr(dashboard, "load_projects", lambda path: ({project.project_id: project}, []))
    sync = Mock(open_runs={})
    def create(**kwargs):
        return DashboardApp(paths, StateLocks(), sync, lambda: NOW, **kwargs)
    return create, sync


def test_actual_dashboard_opt_in_and_unregistered_forgery(source, app_factory):
    create, sync = app_factory
    data = Path(source.project_root) / "DATA"
    data.mkdir()
    publish(data / "task-status.json", json.dumps(metadata(source, "verified-complete")))
    publish(source.path, json.dumps(metadata(source, "blocked")))
    assert "goal_tasks" not in create().snapshot(SnapshotQuery())
    assert create(task_status_sources=[]).snapshot(SnapshotQuery())["goal_tasks"] == []
    app = create(task_status_sources=[source])
    rows = app.snapshot(SnapshotQuery())["goal_tasks"]
    assert rows
    assert rows[0]["state"] == "blocked"
    Path(source.path).unlink()
    assert app.snapshot(SnapshotQuery())["goal_tasks"] == []
    sync.run.assert_called()


@pytest.mark.parametrize("change", ["project_id", "project_root"])
def test_unregistered_or_mismatched_source_not_read(source, app_factory, monkeypatch, change):
    create, _ = app_factory
    wrong = replace(source, **{change: "unregistered" if change == "project_id" else str(Path(source.project_root) / "other")})
    app = create(task_status_sources=[wrong])
    monkeypatch.setattr("agent_control.dashboard.read_task_status", lambda *args: pytest.fail("unregistered source read"))
    assert app.snapshot(SnapshotQuery())["goal_tasks"] == []


@pytest.mark.parametrize("attack", ["symlink", "hardlink", "parent-symlink", "alias", "data", "oversize", "invalid", "stale", "reparse"])
def test_unsafe_or_unavailable_sources_omitted(source, app_factory, tmp_path, monkeypatch, attack):
    from agent_control import task_state
    create, _ = app_factory
    path = Path(source.path)
    raw = json.dumps(metadata(source, "verified-complete"))
    publish(path, raw)
    if attack == "symlink":
        target = tmp_path / "other.json"
        publish(target, raw)
        path.unlink()
        try:
            path.symlink_to(target)
        except OSError:
            pytest.skip("symlink creation unavailable")
    elif attack == "hardlink":
        os.link(path, tmp_path / "alias.json")
    elif attack == "parent-symlink":
        alias = tmp_path / "linked-parent"
        try:
            alias.symlink_to(path.parent, target_is_directory=True)
        except OSError:
            pytest.skip("symlink creation unavailable")
        source = replace(source, path=alias / path.name)
    elif attack == "alias":
        source = replace(source, path=path.parent / ".." / path.parent.name / path.name)
    elif attack == "data":
        data = Path(source.project_root) / "DATA"
        data.mkdir()
        path = data / "task-status.json"
        publish(path, raw)
        source = replace(source, path=path)
    elif attack == "oversize":
        publish(path, " " * (16 * 1024 + 1))
    elif attack == "invalid":
        publish(path, json.dumps({"payload": SENTINEL}))
    elif attack == "stale":
        os.utime(path, (NOW.timestamp() - 86400, NOW.timestamp() - 86400))
    else:
        original = task_state.os.lstat
        def reparse(candidate, *args, **kwargs):
            info = original(candidate, *args, **kwargs)
            return SimpleNamespace(**({key: getattr(info, key) for key in dir(info) if key.startswith("st_")} | {"st_file_attributes": 0x400}))
        monkeypatch.setattr(task_state.os, "lstat", reparse)
    app = create(task_status_sources=[source])
    assert app.snapshot(SnapshotQuery())["goal_tasks"] == []

@pytest.mark.parametrize("changes,visible", [
    ({"project_ids": ("p1",)}, True), ({"project_ids": ("other",)}, False),
    ({"states": ("continuing",)}, True), ({"states": ("blocked",)}, False),
    ({"tasks": ("goal-1",)}, True), ({"tasks": ("other",)}, False),
    ({"models": ("synthetic-model",)}, False), ({"tools": ("engine",)}, False),
    ({"date_from": NOW.date()}, False), ({"date_to": NOW.date()}, False),
])
def test_task_filters(source, project, changes, visible):
    observed = parse_task_status(metadata(source), source)
    result = build_snapshot([], {"p1": project}, {}, [], SnapshotQuery(**changes), now=NOW, task_statuses=[observed])
    assert bool(result["goal_tasks"]) is visible


def test_include_excluded_is_visibility_not_source_registration(source, project):
    observed = parse_task_status(metadata(source), source)
    excluded = replace(project, state="disabled")
    result = build_snapshot([], {"p1": excluded}, {}, [], SnapshotQuery(include_excluded=True), now=NOW, task_statuses=[observed])
    assert result["goal_tasks"] == [asdict(observed)]


@pytest.mark.parametrize("state,reason", [
    *[("blocked", reason) for reason in (
        "cancelled", "revoked", "deadline", "no_progress", "replan_limit", "attempt_limit",
        "usage_limit", "invalid_evidence", "launch_failed", "operator_required", "planner_limit", "effect_uncertain",
    )],
    ("continuing", "worker_active"), ("continuing", "next_slice_ready"),
    *[("reconciliation", reason) for reason in (
        "ownership_unavailable", "initial_snapshot", "state_inconsistent",
    )],
])
def test_producer_reason_allowlist(source, state, reason):
    raw = metadata(source, state)
    raw["reason"] = reason
    if reason == "worker_active":
        raw["verifier_owner"] = "worker"
        raw["active_workers"] = 1
    assert parse_task_status(raw, source).reason == reason


@pytest.mark.parametrize("state,reason,owner", [
    ("continuing", "verification_due", "none"), ("continuing", "planning", "worker"),
    ("continuing", "worker_active", "none"), ("awaiting-verification", "verification_due", "none"),
    ("blocked", "verified", "none"), ("reconciliation", "operator_required", "none"),
])
def test_other_state_consistency(source, state, reason, owner):
    raw = metadata(source, state)
    raw.update(reason=reason, verifier_owner=owner)
    with pytest.raises(TaskStatusError):
        parse_task_status(raw, source)


def test_duplicate_key_in_otherwise_valid_json(source):
    raw = json.dumps(metadata(source))
    raw = raw[:-1] + ', "owner_epoch": 2}'
    with pytest.raises(TaskStatusError):
        parse_task_status(raw, source)


def test_future_file_omitted(source, app_factory):
    create, _ = app_factory
    Path(source.path).write_text(json.dumps(metadata(source, "verified-complete")))
    future = datetime.now(timezone.utc).timestamp() + 86400
    os.utime(source.path, (future, future))
    assert create(task_status_sources=[source]).snapshot(SnapshotQuery())["goal_tasks"] == []


def test_changed_file_omitted(source, monkeypatch):
    from agent_control.task_state import read_task_status
    path = Path(source.path)
    publish(path, json.dumps(metadata(source, "verified-complete")))
    real_fstat = os.fstat
    def changed(fd):
        result = real_fstat(fd)
        return SimpleNamespace(**({key: getattr(result, key) for key in dir(result) if key.startswith("st_")} | {"st_size": result.st_size + 1}))
    monkeypatch.setattr(os, "fstat", changed)
    assert read_task_status(source, now=NOW) is None


@pytest.mark.parametrize("changes_during_read", [False, True])
def test_handle_ctime_is_compared_with_the_same_stat_api(source, monkeypatch,
                                                       changes_during_read):
    from agent_control.task_state import read_task_status
    publish(Path(source.path), json.dumps(metadata(source, "blocked")))
    real_fstat, calls = os.fstat, []

    def handle_stat(fd):
        result = real_fstat(fd)
        calls.append(None)
        fields = {key: getattr(result, key) for key in dir(result) if key.startswith("st_")}
        fields["st_ctime_ns"] = result.st_ctime_ns + 1000000000 + (
            len(calls) if changes_during_read else 0)
        return SimpleNamespace(**fields)

    monkeypatch.setattr(os, "fstat", handle_stat)
    observed = read_task_status(source, now=NOW)
    assert (observed is None) is changes_during_read


def test_invalid_dashboard_metadata_has_no_payload_or_paths(source, app_factory):
    create, _ = app_factory
    raw = metadata(source, "verified-complete")
    raw["payload"] = SENTINEL
    publish(source.path, json.dumps(raw))
    result = create(task_status_sources=[source]).snapshot(SnapshotQuery())
    assert result["goal_tasks"] == []
    body = json.dumps(result)
    assert SENTINEL not in body
    assert str(source.path) not in body
    assert source.project_root not in body


def test_models_export_trusted_source_and_states():
    assert set(models.TASK_STATES) == {
        "continuing",
        "awaiting-verification",
        "reconciliation",
        "blocked",
        "verified-complete",
    }
    src = models.TaskStatusSource(
        project_id="p1", project_root="X:/proj", envelope_sha256=ROOT_SHA, path="X:/state/task-status.json"
    )
    with pytest.raises(Exception):
        src.project_id = "other"  # frozen

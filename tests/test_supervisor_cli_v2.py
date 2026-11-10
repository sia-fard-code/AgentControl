"""Policy-v2 CLI preparation is explicit authority, not live execution."""
import json

import pytest

from supervisor_driver_helpers import (CAPS, GOAL, FakeAdapter, correcting_planner, setup,
                                      task, two_worker_planner)
from supervisor_helpers import load

supervise = load("supervise")


def _inputs(tmp_path):
    fx = setup(tmp_path)
    policy = fx.project / ".claude" / "agent-policy.json"
    policy.parent.mkdir()
    policy.write_text('{"allowed_cmds":[["git","status"]]}')
    goal, registry, caps, out = [tmp_path / n for n in
                                ("goal.txt", "registry.json", "caps.json", "approval.json")]
    goal.write_text(GOAL)
    registry.write_bytes(fx.registry)
    caps.write_text(json.dumps(CAPS))
    args = ["prepare", "--project", str(fx.project), "--goal", str(goal), "--registry", str(registry),
            "--capabilities", str(caps), "--out", str(out)]
    return fx, out, args


def _prepared(tmp_path, capsys, *flags):
    fx, out, args = _inputs(tmp_path)
    code = supervise.main(args + list(flags))
    data = json.loads(capsys.readouterr().out)
    assert code == 0, data
    env = supervise.contract.load_envelope(out.read_bytes(), data["sha256"], fx.project)
    assert not data["approved"] and not fx.state.exists() and not fx.artifacts.exists()
    return env


def test_new_prepare_is_v2_eight_workers_and_no_implicit_lifetime_or_time_budget(tmp_path, capsys):
    env = _prepared(tmp_path, capsys)
    assert env.schema_version == 2
    assert env.limits["max_concurrent_workers"] == 8
    for key in ("max_total_attempts", "max_replans", "max_planner_calls"):
        assert env.limits[key] is None
    assert env.deadline is None and env.max_usage is None
    assert env.limits["stop_grace_seconds"] == 60 and env.limits["max_final_report_calls"] == 1


def test_explicit_prepare_limits_and_higher_capacity_are_approved_not_runtime_overrides(tmp_path, capsys):
    env = _prepared(tmp_path, capsys, "--max-total-attempts", "30", "--max-replans", "4",
                     "--max-planner-calls", "5", "--max-concurrent-workers", "16",
                     "--deadline-seconds", "600", "--max-usage", "5000000")
    assert (env.limits["max_total_attempts"], env.limits["max_replans"],
            env.limits["max_planner_calls"], env.limits["max_concurrent_workers"]) == (30, 4, 5, 16)
    assert env.deadline is not None and env.max_usage == 5000000


def test_explicit_zero_replans_is_supported_while_default_is_unbounded(tmp_path, capsys):
    env = _prepared(tmp_path, capsys, "--max-replans", "0")
    assert env.limits["max_replans"] == 0 and env.limits["max_planner_calls"] is None


@pytest.mark.parametrize("flag,value", [("--max-total-attempts", "0"), ("--max-concurrent-workers", "0"),
                                        ("--deadline-seconds", "0"), ("--max-usage", "0")])
def test_invalid_explicit_budget_never_writes_an_approval(tmp_path, capsys, flag, value):
    fx, out, args = _inputs(tmp_path)
    assert supervise.main(args + [flag, value]) == 2
    assert json.loads(capsys.readouterr().out).get("error")
    assert not out.exists() and not fx.state.exists() and not fx.artifacts.exists()


def test_run_goal_without_deadline_or_usage_ceiling_reaches_real_protected_acceptance(tmp_path):
    fx = setup(tmp_path)
    data = supervise.contract.strict_json(fx.raw)
    data.update(schema_version=2, limits=dict(supervise.contract.DEFAULT_LIMITS_V2),
                 deadline=None, max_usage=None)
    raw = supervise.contract.canonical(data)
    fx.envelope = supervise.contract.load_envelope(raw, supervise._sha(raw), fx.project)
    planner = lambda brief, rev: json.dumps({"plan_revision": rev, "tasks": [
        task("W1", "write src/a.txt A", ["C1"]), task("W2", "write src/b.txt B", ["C2", "CI"])]})
    class Guarded(FakeAdapter):
        observations = 0
        def observe(self, identity):
            self.observations += 1
            if self.observations > 100:
                pytest.fail("simple synthetic goal did not converge: " + self.briefs[-3:][0][1][-600:])
            return super().observe(identity)
    ad = Guarded(fx.envelope, fx.artifacts, planner)
    result = supervise.run_goal(fx.envelope, goal_text=GOAL, registry_raw=fx.registry,
        capabilities=CAPS, state_dir=fx.state, artifact_root=fx.artifacts, adapter=ad, wait=lambda s: None)
    assert result["outcome"] == "SUCCEEDED" and result["counters"]["attempts"] == 2, result
    assert result["usage"]["charged"] == 30 and len(ad.launches) == 3


def _upgrade(fx, **overrides):
    data = supervise.contract.strict_json(fx.raw)
    data.update(schema_version=2, limits=dict(supervise.contract.DEFAULT_LIMITS_V2),
                 deadline=None, max_usage=None)
    data.update(overrides)
    raw = supervise.contract.canonical(data)
    fx.envelope = supervise.contract.load_envelope(raw, supervise._sha(raw), fx.project)
    return fx


def test_invalid_intermediate_plan_keeps_the_latest_committed_candidate_and_useful_patch(tmp_path):
    fx = _upgrade(setup(tmp_path, ci_wants="B2"))

    def planner(brief, revision):
        if revision > 3:
            pytest.fail("committed candidate was lost across the invalid revision")
        if revision == 0:
            return two_worker_planner(brief, revision)
        if revision == 1:
            return "{not valid json"
        assert "chk-ci" in brief and "b_stale" in brief
        return correcting_planner(brief, revision)

    ad = FakeAdapter(fx.envelope, fx.artifacts, planner)
    out = supervise.run_goal(fx.envelope, goal_text=GOAL, registry_raw=fx.registry, capabilities=CAPS,
        state_dir=fx.state, artifact_root=fx.artifacts, adapter=ad, wait=lambda _: None)
    assert out["outcome"] == "SUCCEEDED" and out["counters"]["planner_calls"] == 3, out
    assert out["counters"]["attempts"] == 3 and out["counters"]["replans"] == 2


@pytest.mark.parametrize("limit,value,reason", [("max_total_attempts", 2, "attempt_limit"),
    ("max_replans", 0, "replan_limit"), ("max_planner_calls", 1, "planner_limit")])
def test_explicit_v2_limit_still_stops_the_real_goal(tmp_path, limit, value, reason):
    limits = dict(supervise.contract.DEFAULT_LIMITS_V2)
    limits[limit] = value
    fx = _upgrade(setup(tmp_path, ci_wants="B2"), limits=limits)
    ad = FakeAdapter(fx.envelope, fx.artifacts, two_worker_planner)
    out = supervise.run_goal(fx.envelope, goal_text=GOAL, registry_raw=fx.registry, capabilities=CAPS,
        state_dir=fx.state, artifact_root=fx.artifacts, adapter=ad, wait=lambda _: None)
    assert (out["outcome"], out["reason"]) == ("PARTIAL", reason), out
    assert out["counters"]["planner_calls"] == 1 and out["counters"]["attempts"] == 2


def test_explicit_v2_usage_budget_is_not_disabled_by_null_deadline(tmp_path):
    fx = _upgrade(setup(tmp_path), max_usage=1000)
    ad = FakeAdapter(fx.envelope, fx.artifacts, two_worker_planner)
    out = supervise.run_goal(fx.envelope, goal_text=GOAL, registry_raw=fx.registry, capabilities=CAPS,
        state_dir=fx.state, artifact_root=fx.artifacts, adapter=ad, wait=lambda _: None, reserve=1000)
    assert (out["outcome"], out["reason"]) == ("PARTIAL", "usage_limit")
    assert len(ad.launches) == 1 and out["counters"]["attempts"] == 0


def test_unknown_mutation_never_uses_the_safe_context_continuation_path(tmp_path):
    fx = _upgrade(setup(tmp_path))
    clock = type("Clock", (), {"t": 1000.0, "__call__": lambda self: self.t})()
    waits = []

    class Unknown(FakeAdapter):
        def observe(self, identity):
            if "attempt_id" in identity:
                return {"state": "UNKNOWN", "outcome": None, "quiescent": False,
                        "usage": None, "reason": "mutation_outcome_unknown"}
            return super().observe(identity)

    def wait(seconds):
        waits.append(seconds)
        if len(waits) == 6:
            raise KeyboardInterrupt
        clock.t += seconds

    ad = Unknown(fx.envelope, fx.artifacts, two_worker_planner)
    out = supervise.run_goal(fx.envelope, goal_text=GOAL, registry_raw=fx.registry, capabilities=CAPS,
        state_dir=fx.state, artifact_root=fx.artifacts, adapter=ad, clock=clock, wait=wait)
    assert out["outcome"] == "BLOCKED" and out["reason"] == "cancel_incomplete", out
    assert out["counters"]["attempts"] == 2 and out["counters"]["replans"] == 0
    assert len(ad.launches) == 3 and out["unresolved"] == 2

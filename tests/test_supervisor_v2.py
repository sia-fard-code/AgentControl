"""End-to-end v2 policy oracles: real run_goal/core/journal/temp Git/protected validators.
Only provider transport is faked (barrier/scripted planner+worker functions). No model/network."""
import json

import pytest

from supervisor_helpers import load
from supervisor_evidence_helpers import git
from supervisor_driver_helpers import CAPS
from supervisor_v2_driver_helpers import (BarrierAdapter, advancing_planner, advancing_want,
                                          fanout_planner, journal_records, setup_n,
                                          stalled_brief, stalled_then_changed_planner)

supervise = load("supervise")


def _run(fx, adapter, **kw):
    return supervise.run_goal(fx.envelope, goal_text=fx.goal, registry_raw=fx.registry,
                              capabilities=CAPS, state_dir=fx.state, artifact_root=fx.artifacts,
                              adapter=adapter, wait=lambda s: None, **kw)


def _main_untouched(fx):
    assert git(fx.project, "status", "--porcelain") == ""
    assert git(fx.project, "rev-parse", "HEAD").strip() == fx.baseline
    assert git(fx.project, "worktree", "list", "--porcelain").count("worktree ") == 1


# Explicit absolute deadline + usage ceiling isolates scheduling from deadline/max_usage=None handling.
EXPLICIT = {"deadline": 2_000_000_000.0, "max_usage": 10_000_000,
            "limits": {"max_total_attempts": 64, "max_planner_calls": 32, "max_replans": 31}}
NULLS = {"deadline": None, "max_usage": None, "limits": {}}
authority = pytest.mark.parametrize("auth", [NULLS, EXPLICIT], ids=["null_defaults", "explicit"])


def _fanout(tmp_path, n, cap, barrier, auth=NULLS):
    auth = dict(auth)
    limits = dict(auth.pop("limits"), max_concurrent_workers=cap)
    fx = setup_n(tmp_path, n, limits=limits, **auth)
    assert fx.envelope.schema_version == 2
    adapter = BarrierAdapter(fx.envelope, fx.artifacts, fanout_planner(n), barrier)
    return fx, adapter, _run(fx, adapter)


@authority
def test_eight_independent_tasks_run_at_once_and_pass_protected_check(tmp_path, auth):
    fx, ad, out = _fanout(tmp_path, 8, 8, barrier=8, auth=auth)
    assert ad.peak == 8 and ad.open, (ad.trace, json.dumps(out))  # all 8 live at once, barrier opened
    assert (out["outcome"], out["reason"]) == ("SUCCEEDED", "succeeded"), out
    assert out["counters"]["attempts"] == 8 and ad.worker_launches == 8
    assert out["counters"]["planner_calls"] == 1 and out["counters"]["replans"] == 0
    assert set(out["satisfied_criteria"]) == {f"C{i}" for i in range(8)} | {"CI"}
    _main_untouched(fx)


@authority
def test_configured_sixteen_capacity_runs_sixteen_at_once(tmp_path, auth):
    fx, ad, out = _fanout(tmp_path, 16, 16, barrier=16, auth=auth)
    assert ad.peak == 16 and ad.open, ad.trace
    assert (out["outcome"], out["reason"]) == ("SUCCEEDED", "succeeded"), out
    assert out["counters"]["attempts"] == 16 and ad.worker_launches == 16
    _main_untouched(fx)


@authority
def test_ten_workers_four_replans_end_in_protected_verified_success(tmp_path, auth):
    auth = dict(auth)
    fx = setup_n(tmp_path, 1, want=advancing_want(4), limits=auth.pop("limits"), **auth)
    ad = BarrierAdapter(fx.envelope, fx.artifacts, advancing_planner(4), barrier=1)
    out = _run(fx, ad)
    assert (out["outcome"], out["reason"]) == ("SUCCEEDED", "succeeded"), json.dumps(out)
    kinds = [k for k, _ in ad.launches]
    assert kinds.count("call_planner") == 5 and kinds.count("launch_worker") == 10
    c = out["counters"]
    assert (c["attempts"], c["planner_calls"], c["replans"]) == (10, 5, 4)
    # real Git selection: replaced f0 is R4, every revision's retained side file survives
    assert ad.peak <= 8 and len(out["candidate_sha256"]) == 64
    for rev in range(1, 5):
        brief = [b for k, b in ad.briefs if k == "call_planner"][rev]
        assert "chk-all" in brief  # each replan consumed the trusted protected failure
    _main_untouched(fx)


def test_no_novelty_round_is_a_counted_diagnostic_replan_then_useful_correction(tmp_path):
    fx = setup_n(tmp_path, 1, want={"f0.txt": "V0"})
    ad = BarrierAdapter(fx.envelope, fx.artifacts, stalled_then_changed_planner(), barrier=1)
    out = _run(fx, ad)
    assert out["reason"] != "no_progress", json.dumps(out)  # v2 does not stop on a stall
    assert (out["outcome"], out["reason"]) == ("SUCCEEDED", "succeeded"), json.dumps(out)
    calls = [r["data"] for r in journal_records(fx.state, "call") if r["data"]["role"] == "planner"]
    reasons = [c.get("admission_reason") for c in calls]
    assert reasons[0] == "initial" and "diagnostic" in reasons, reasons
    assert len(calls) == out["counters"]["planner_calls"] >= 3  # diagnostic admissions are counted
    diag = reasons.index("diagnostic")
    # watermark consumed monotonically; evidence count equals real journaled evidence (none forged)
    marks = [c["watermark"] for c in calls]
    assert marks == sorted(marks) and marks[-1] <= out["counters"]["evidence"]
    assert out["counters"]["evidence"] == len(journal_records(fx.state, "evidence"))
    briefs = [b for k, b in ad.briefs if k == "call_planner"]
    assert stalled_brief(briefs[diag]), briefs[diag][-800:]  # stall acknowledged honestly
    assert "new evidence" not in briefs[diag].lower().replace("no new evidence", "")
    _main_untouched(fx)


def test_no_deadline_cancellation_keeps_fixed_60s_grace_and_serializes_finitely(tmp_path):
    fx = setup_n(tmp_path, 1)
    assert fx.envelope.deadline is None and fx.envelope.max_usage is None
    ad = BarrierAdapter(fx.envelope, fx.artifacts, fanout_planner(1), barrier=1)
    real = ad.launch

    def interrupt_on_worker(effect, brief, **kw):
        if effect.kind == "launch_worker":
            raise KeyboardInterrupt  # operator cancel before the worker launch
        return real(effect, brief, **kw)
    ad.launch = interrupt_on_worker
    clock = type("Clock", (), {"t": 1000.0, "__call__": lambda self: self.t})()
    out = supervise.run_goal(fx.envelope, goal_text=fx.goal, registry_raw=fx.registry,
        capabilities=CAPS, state_dir=fx.state, artifact_root=fx.artifacts, adapter=ad, clock=clock,
        wait=lambda seconds: setattr(clock, "t", clock.t + seconds))
    # The intent was admitted before the interrupt: absence of a receipt is not proof of no effect.
    assert out["outcome"] == "BLOCKED" and out["counters"]["attempts"] == 1, json.dumps(out)
    assert not any(kind == "launch_worker" for kind, _ in ad.launches)
    stops = [r["data"] for r in journal_records(fx.state, "stop")]
    assert stops and stops[0]["reason"] == "cancelled"
    assert stops[0]["deadline"] - stops[0]["start"] == 60  # grace, not infinity/TypeError
    raw = b"".join(p.read_bytes() for p in fx.state.rglob("journal*") if p.is_file())
    assert b"Infinity" not in raw and b"NaN" not in raw
    _main_untouched(fx)


def test_context_limited_collected_worker_continues_with_fresh_identity_and_passes(tmp_path):
    from supervisor_v2_driver_helpers import ContextLimitedAdapter, continuation_planner
    fx = setup_n(tmp_path, 1, want={"f0.txt": "V0"})
    ad = ContextLimitedAdapter(fx.envelope, fx.artifacts, continuation_planner)
    out = _run(fx, ad)
    assert (out["outcome"], out["reason"]) == ("SUCCEEDED", "succeeded"), json.dumps(out)
    workers = [r["data"] for r in journal_records(fx.state, "worker")]
    assert len(workers) >= 2 and len({w["launch_id"] for w in workers}) == len(workers)
    assert len({w["run_id"] for w in workers}) == len(workers)  # fresh worker identity/context
    assert out["counters"]["replans"] >= 1
    _main_untouched(fx)


@authority
def test_queue_of_ten_under_eight_never_exceeds_capacity_and_drains(tmp_path, auth):
    fx, ad, out = _fanout(tmp_path, 10, 8, barrier=8, auth=auth)
    assert ad.peak == 8, ad.trace
    assert max(n for kind, n in ad.trace if kind == "launch") <= 8
    assert ad.worker_launches == 10 and out["counters"]["attempts"] == 10
    assert (out["outcome"], out["reason"]) == ("SUCCEEDED", "succeeded"), out
    _main_untouched(fx)

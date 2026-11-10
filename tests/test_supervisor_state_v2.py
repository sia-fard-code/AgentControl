"""Version-2 core policy over the real journal; synthetic tmp_path inputs only."""
import uuid

import pytest

from supervisor_helpers import canonical, digest, envelope_bytes, load
from supervisor_v2_helpers import envelope_bytes_v2

contract = load("supervisor_contract")
ss = load("supervisor_state")


class Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


def _open(tmp_path, clock=None, limits=None, **kw):
    raw, approved = envelope_bytes_v2(tmp_path, limits, **kw)
    env = contract.load_envelope(raw, approved, tmp_path)
    return ss.Supervisor(tmp_path / "state", env, clock or Clock())


def _plan(tasks, revision):
    return canonical({"schema_version": 1, "plan_revision": revision, "tasks": [
        {"task_id": t, "depends_on": [], "objective_digest": digest(t), "criteria": ["C1"],
         "hypothesis_ids": []} for t in tasks]})


def _planned(sup, tasks, n):
    effect = sup.admit_planner(f"p{n}", f"run-p{n}", 10)
    sup.record_plan(f"p{n}", _plan(tasks, n), usage=3)
    return effect


def _launch(sup, task, n):
    return sup.admit_worker(task, f"a{n}", str(uuid.uuid4()), f"run-a{n}", f"wt-a{n}", 100)


def _finish(sup, n, outcome="failed"):
    sup.observe_attempt(f"a{n}", "TERMINAL", outcome, True, 5)
    sup.observe_attempt(f"a{n}", "COLLECTED", outcome, True, 5)


def _fail_evidence(sup, n):
    return sup.register_evidence("check_failure", "C1", digest(f"cand-{n}"),
                                 check_id="chk-unit", failure_code="assert_failed")


def _accept_all(sup, n):
    sup.register_evidence("candidate", "C1", digest(f"cand-{n}"))
    sup.record_acceptance(digest(f"cand-{n}"), ["C1", "C2", "CI"])


def test_v2_more_than_six_admissions_and_eight_task_metadata(tmp_path):
    tasks = [f"W{i}" for i in range(1, 9)]
    with _open(tmp_path) as sup:
        _planned(sup, tasks, 0)
        for i, t in enumerate(tasks, 1):
            _launch(sup, t, i)
        assert sup.counters()["attempts"] == 8
    with _open(tmp_path) as sup:  # replay re-validates the 8-task plan binding
        assert sup.counters()["attempts"] == 8 and len(sup.plan_view()["tasks"]) == 8


def test_v2_capacity_eight_refuses_ninth_until_release(tmp_path):
    tasks = [f"W{i}" for i in range(1, 10)]
    with _open(tmp_path) as sup:
        _planned(sup, tasks, 0)
        for i, t in enumerate(tasks[:8], 1):
            _launch(sup, t, i)
        with pytest.raises(ss.StateError):
            _launch(sup, "W9", 9)
        _finish(sup, 1)
        _launch(sup, "W9", 9)
        assert sup.counters()["occupied"] == 8


def test_v2_capacity_sixteen_refuses_seventeenth(tmp_path):
    tasks = [f"W{i}" for i in range(1, 18)]
    with _open(tmp_path, limits={"max_concurrent_workers": 16}) as sup:
        _planned(sup, tasks, 0)
        for i, t in enumerate(tasks[:16], 1):
            _launch(sup, t, i)
        with pytest.raises(ss.StateError):
            _launch(sup, "W17", 17)
        _finish(sup, 3)
        _launch(sup, "W17", 17)


def test_v2_many_replans_end_accepted_with_reasons(tmp_path):
    clock = Clock()
    with _open(tmp_path, clock) as sup:
        reasons = []
        for n in range(5):  # 5 planner calls, 4 replans, eval-free progress via failures
            reasons.append(_planned(sup, [f"T{n}"], n).data["admission_reason"])
            _launch(sup, f"T{n}", n)
            _finish(sup, n)
            _fail_evidence(sup, n)
            if n < 4:
                assert sup.decide().kind == "replan"
        assert reasons == ["initial"] + ["progress"] * 4
        _accept_all(sup, 9)
        assert sup.decide().outcome == "SUCCEEDED"
    with _open(tmp_path, clock) as sup:
        assert sup.counters()["replans"] == 4 and sup.final["outcome"] == "SUCCEEDED"


def test_v2_no_novelty_is_diagnostic_replan_not_fake_evidence(tmp_path):
    with _open(tmp_path) as sup:
        _planned(sup, ["T0"], 0)
        _launch(sup, "T0", 0)
        _finish(sup, 0)
        _fail_evidence(sup, 0)
        assert sup.decide().kind == "replan"
        _planned(sup, ["T1"], 1)
        _launch(sup, "T1", 1)
        _finish(sup, 1)  # quiescent, collected, no new evidence
        before = sup.counters()
        decision = sup.decide()
        assert (decision.kind, decision.reason) == ("replan", "diagnostic")
        effect = sup.admit_planner("p2", "run-p2", 10)
        assert effect.data["admission_reason"] == "diagnostic"
        after = sup.counters()
        assert after["evidence"] == before["evidence"] == after["watermark"]
        assert after["planner_calls"] == before["planner_calls"] + 1
        sup.record_plan("p2", _plan(["T2"], 2), usage=1)
    with _open(tmp_path) as sup:
        assert sup.counters()["planner_calls"] == 3 and sup.stop is None


def test_v2_forged_admission_reason_fails_replay(tmp_path):
    with _open(tmp_path) as sup:
        data = dict(sup.admit_planner("p0", "run-p0", 10).data)
        sup.planner_failed("p0", None)
        data.update(call_id="p1", run_id="run-p1", launch_id=str(uuid.uuid4()),
                    revision=1, admission_reason="progress")  # no new evidence: forged
        data.update(sup._identity_fields("run-p1", data["launch_id"]))
        sup._journal.transact("call", data)
    with pytest.raises(ss.StateError):
        with _open(tmp_path):
            pass


def test_v2_null_deadline_and_budget(tmp_path):
    clock = Clock(5_000_000_000.0)
    with _open(tmp_path, clock) as sup:
        assert sup.decide().kind == "plan"
        sup.admit_planner("p0", "run-p0", 10**9)  # no ceiling, still accounted
        assert sup.totals()["reserved"] == 10**9
        sup.request_stop("cancelled")
        assert sup.stop["start"] == clock.t and sup.stop["deadline"] == clock.t + 60


def test_v2_explicit_deadline_and_budget_stop(tmp_path):
    clock = Clock()
    with _open(tmp_path, clock, deadline=1500.0) as sup:
        clock.t = 1600.0
        with pytest.raises(ss.StateError):
            sup.admit_planner("p0", "run-p0", 10)
        assert sup.decide().reason == "deadline" and sup.stop["start"] == 1500.0
    with _open(tmp_path / "b", Clock(), max_usage=50) as sup:
        with pytest.raises(ss.StateError):
            sup.admit_planner("p0", "run-p0", 60)
        assert sup.decide(reserve=60).reason == "usage_limit"


def test_v2_explicit_caps_and_zero_replans(tmp_path):
    with _open(tmp_path / "a", limits={"max_total_attempts": 7}) as sup:
        _planned(sup, [f"W{i}" for i in range(7)], 0)  # proposal bound = explicit cap 7
        for i in range(7):
            _launch(sup, f"W{i}", i)
        _finish(sup, 0, "rejected")
        sup.record_retry_proof("a0", "rejected_before_effects", digest("proof"))
        with pytest.raises(ss.StateError):  # 8th admission refused by the explicit cap
            _launch(sup, "W0", 7)
    with _open(tmp_path / "b", limits={"max_replans": 0}) as sup:
        _planned(sup, ["T0"], 0)
        _launch(sup, "T0", 0)
        _finish(sup, 0)
        _fail_evidence(sup, 0)
        with pytest.raises(ss.StateError):
            sup.admit_planner("p1", "run-p1", 10)
        assert sup.decide().reason == "replan_limit"
    with _open(tmp_path / "c", limits={"max_planner_calls": 1}) as sup:
        _planned(sup, ["T0"], 0)
        _launch(sup, "T0", 0)
        _finish(sup, 0)
        assert sup.decide().reason == "planner_limit"


def test_v2_eval_groups_across_four_revisions_and_closeout(tmp_path):
    with _open(tmp_path) as sup:
        for n in range(4):
            _planned(sup, [f"T{n}"], n)
            _launch(sup, f"T{n}", n)
            _finish(sup, n)
            sup.begin_evaluation(f"e{n}", None, f"evals/e{n}")
            sup.finish_evaluation(f"e{n}", digest(f"facts{n}"), quiescent=True)
            _fail_evidence(sup, n)
        _accept_all(sup, 9)
        assert sup.admit_report("r0", "run-r0", 5).data["admission_reason"] == "closeout"
    with _open(tmp_path) as sup:
        assert len(sup.evaluations) == 4 and sup.counters()["reports"] == 1


def test_v2_unknown_restored_intent_holds_and_changed_approval_refused(tmp_path):
    with _open(tmp_path) as sup:
        _planned(sup, ["T0"], 0)
        _launch(sup, "T0", 0)  # intent durable, no observation before "crash"
    with _open(tmp_path) as sup:
        assert sup.attempts["a0"]["lifecycle"] == "UNKNOWN"
        assert sup.decide().kind == "reconcile"
        with pytest.raises(ss.StateError):
            sup.admit_planner("p1", "run-p1", 10)
    with pytest.raises(Exception):  # changed approval cannot reuse the state directory
        with _open(tmp_path, limits={"max_concurrent_workers": 9}):
            pass


def test_v1_envelope_keeps_legacy_schema_and_no_progress(tmp_path):
    raw, approved = envelope_bytes(tmp_path)
    env = contract.load_envelope(raw, approved, tmp_path)
    with ss.Supervisor(tmp_path / "state", env, Clock()) as sup:
        effect = sup.admit_planner("p0", "run-p0", 10)
        assert "admission_reason" not in effect.data
        sup.record_plan("p0", _plan(["T0"], 0), usage=3)
        _launch(sup, "T0", 0)
        _finish(sup, 0)
        assert sup.decide().reason == "no_progress"
    assert "admission_reason" not in ss.SCHEMAS["call"]

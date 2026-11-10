import uuid

import pytest

from supervisor_helpers import canonical, digest, envelope_bytes, load

contract = load("supervisor_contract")
sj = load("supervisor_journal")
ss = load("supervisor_state")


class Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


def _env(root, **kw):
    raw, approved = envelope_bytes(root, **kw)
    return contract.load_envelope(raw, approved, root)


def _open(tmp_path, clock, **kw):
    return ss.Supervisor(tmp_path / "state", _env(tmp_path, **kw), clock)


def _plan(tasks, revision):
    return canonical({"schema_version": 1, "plan_revision": revision, "tasks": [
        {"task_id": t, "depends_on": [], "objective_digest": digest(t), "criteria": ["C1"],
         "hypothesis_ids": []} for t in tasks]})


def _planned(sup, tasks=("W1", "W2"), n=0):
    sup.admit_planner(f"p{n}", f"run-p{n}", 10)
    return sup.record_plan(f"p{n}", _plan(tasks, n), usage=3)


def _launch(sup, task, n):
    return sup.admit_worker(task, f"a{n}", str(uuid.uuid4()), f"run-a{n}", f"wt-a{n}", 100)


def _finish(sup, n, usage=7, outcome="succeeded"):
    sup.observe_attempt(f"a{n}", "TERMINAL", outcome, True, usage)
    sup.observe_attempt(f"a{n}", "COLLECTED", outcome, True, usage)


def _prove(sup, n, reason="rejected_before_effects"):
    """Synthetic trusted safe-to-retry proof bound to attempt and an evidence digest."""
    return sup.record_retry_proof(f"a{n}", reason, digest(f"proof-{n}"))


def test_admission_is_durable_before_effect_and_survives_restart(tmp_path):
    clock = Clock()
    with _open(tmp_path, clock) as sup:
        _planned(sup)
        effect = _launch(sup, "W1", 1)
        assert effect.kind == "launch_worker" and effect.data["attempt_id"] == "a1"
    with _open(tmp_path, clock) as sup:
        assert sup.counters()["attempts"] == 1
        assert sup.counters()["planner_calls"] == 1
        with pytest.raises(ss.StateError):   # same identities are never reused
            sup.admit_worker("W2", "a1", str(uuid.uuid4()), "run-x", "wt-x", 1)


def test_third_occupied_worker_refused_and_unknown_retains_slot(tmp_path):
    with _open(tmp_path, Clock()) as sup:
        _planned(sup, ("W1", "W2", "W3"))
        _launch(sup, "W1", 1)
        _launch(sup, "W2", 2)
        with pytest.raises(ss.StateError):
            _launch(sup, "W3", 3)
        sup.observe_attempt("a1", "TERMINAL", "failed", False, None)   # engine exit, survivor
        with pytest.raises(ss.StateError):
            _launch(sup, "W3", 3)
        sup.observe_attempt("a2", "UNKNOWN", None, False, None)
        with pytest.raises(ss.StateError):                              # unknown blocks dispatch
            _launch(sup, "W3", 3)
        assert sup.counters()["occupied"] == 2
        assert sup.decide().kind == "reconcile"


def test_seventh_admission_refused_and_rejected_attempts_count(tmp_path):
    with _open(tmp_path, Clock()) as sup:
        _planned(sup, ("W1",))
        for n in range(1, 7):
            _launch(sup, "W1", n)
            _finish(sup, n, usage=0, outcome="rejected")
            _prove(sup, n)
        assert sup.counters()["attempts"] == 6
        with pytest.raises(ss.StateError):
            _launch(sup, "W1", 7)


@pytest.mark.parametrize("outcome", ["rejected", "failed"])
def test_replacement_requires_durable_retry_proof(tmp_path, outcome):
    clock = Clock()
    with _open(tmp_path, clock) as sup:
        _planned(sup, ("W1",))
        _launch(sup, "W1", 1)
        _finish(sup, 1, usage=0, outcome=outcome)
        with pytest.raises(ss.StateError):          # terminal failure is not proof of no effects
            _launch(sup, "W1", 2)
        with pytest.raises(ss.StateError):          # fixed reason enum only
            sup.record_retry_proof("a1", "looks fine", digest("p"))
        with pytest.raises(ss.StateError):          # unknown attempt cannot be proven
            sup.record_retry_proof("a9", "rejected_before_effects", digest("p"))
        _prove(sup, 1, "effects_reconciled")
    with _open(tmp_path, clock) as sup:             # proof survives restart
        assert _launch(sup, "W1", 2).data["attempt_id"] == "a2"


def test_retry_proof_refused_for_unknown_or_uncollected_attempt(tmp_path):
    with _open(tmp_path, Clock()) as sup:
        _planned(sup, ("W1", "W2"))
        _launch(sup, "W1", 1)
        _launch(sup, "W2", 2)
        sup.observe_attempt("a1", "TERMINAL", "rejected", True, 0)
        sup.observe_attempt("a2", "UNKNOWN", None, False, None)
        for n in (1, 2):
            with pytest.raises(ss.StateError):
                _prove(sup, n)


@pytest.mark.parametrize("field", ["launch", "run", "worktree"])
def test_unique_identities_required(tmp_path, field):
    with _open(tmp_path, Clock()) as sup:
        _planned(sup)
        launch = str(uuid.uuid4())
        sup.admit_worker("W1", "a1", launch, "run-a1", "wt-a1", 1)
        ids = {"launch": (launch, "run-b", "wt-b"), "run": (str(uuid.uuid4()), "run-a1", "wt-b"),
               "worktree": (str(uuid.uuid4()), "run-b", "wt-a1")}[field]
        with pytest.raises(ss.StateError):
            sup.admit_worker("W2", "a2", *ids, 1)
        with pytest.raises(ss.StateError):   # planner transcripts share the run-id namespace
            sup.admit_worker("W2", "a3", str(uuid.uuid4()), "run-p0", "wt-c", 1)


def test_null_usage_keeps_reservation_and_rollup_is_not_double_charged(tmp_path):
    with _open(tmp_path, Clock()) as sup:
        _planned(sup)
        _launch(sup, "W1", 1)
        _launch(sup, "W2", 2)
        _finish(sup, 1, usage=None)
        _finish(sup, 2, usage=40)
        sup.observe_attempt("a2", "COLLECTED", "succeeded", True, 40)   # idempotent re-read
        assert sup.totals() == {"charged": 43, "reserved": 100, "unknown_calls": 1,
                                "total_usage": None}
        with pytest.raises(ss.StateError):                                 # measured usage is fixed
            sup.observe_attempt("a2", "COLLECTED", "succeeded", True, 41)


def _fail(sup, n, check="chk-unit", code="assert_failed", cand=None):
    return sup.register_evidence("check_failure", "CI", candidate=cand or digest(f"K{n}"),
                                 check_id=check, failure_code=code)


def _batch_done(sup, first, count=2):
    for n in range(first, first + count):
        _finish(sup, n)


def test_wait_while_batch_member_active_then_no_progress(tmp_path):
    with _open(tmp_path, Clock()) as sup:
        _planned(sup)
        _launch(sup, "W1", 1)
        _launch(sup, "W2", 2)
        _finish(sup, 1)
        sup.observe_attempt("a2", "TERMINAL", "succeeded", False, 5)   # descendant still alive
        assert sup.decide().kind == "wait"
        sup.observe_attempt("a2", "COLLECTED", "succeeded", True, 5)
        decision = sup.decide()
        assert (decision.kind, decision.reason) == ("stopping", "no_progress")
        assert sup.decide().outcome == "PARTIAL"
        assert sup.counters()["planner_calls"] == 1


def test_replan_consumes_whole_snapshot_and_lost_response_stays_consumed(tmp_path):
    clock = Clock()
    with _open(tmp_path, clock) as sup:
        _planned(sup)
        _launch(sup, "W1", 1)
        _launch(sup, "W2", 2)
        _batch_done(sup, 1)
        assert _fail(sup, 1) is not None
        assert _fail(sup, 1) is None                 # duplicate evidence registers nothing
        _fail(sup, 1, code="timeout")                # E2 in the same snapshot
        assert sup.decide().kind == "replan"
        sup.admit_planner("p1", "run-p1", 10)
    with _open(tmp_path, clock) as sup:             # response lost across a restart
        assert sup.counters()["planner_calls"] == 2
        assert sup.decide().kind == "wait"           # outstanding call is never retried
        sup.planner_failed("p1", None)
        assert _fail(sup, 1, code="timeout") is None  # re-presented E2 is not new
        with pytest.raises(ss.StateError):
            sup.admit_planner("p2", "run-p2", 10)
        assert sup.decide().reason == "no_progress"
        assert sup.totals()["reserved"] == 10        # lost call keeps its reservation


def test_later_evidence_after_failed_replan_permits_second_replan(tmp_path):
    with _open(tmp_path, Clock()) as sup:
        _planned(sup)
        _launch(sup, "W1", 1)
        _launch(sup, "W2", 2)
        _batch_done(sup, 1)
        _fail(sup, 1)
        sup.admit_planner("p1", "run-p1", 10)
        sup.planner_failed("p1", 4)
        _fail(sup, 1, code="timeout")
        assert sup.decide().kind == "replan"
        sup.admit_planner("p2", "run-p2", 10)
        assert sup.counters()["replans"] == 2


def test_stale_planner_result_rejected(tmp_path):
    with _open(tmp_path, Clock()) as sup:
        _planned(sup)
        with pytest.raises(ss.StateError):
            sup.record_plan("p0", _plan(("W1",), 0), usage=1)    # already answered
        with pytest.raises(ss.StateError):
            sup.admit_planner("p1", "run-p1", 10)                # no barrier yet


def test_invalid_plan_consumes_call(tmp_path):
    with _open(tmp_path, Clock()) as sup:
        sup.admit_planner("p0", "run-p0", 10)
        with pytest.raises(contract.ContractError):
            sup.record_plan("p0", _plan(("W1",), 1), usage=None)
        assert sup.counters()["planner_calls"] == 1
        with pytest.raises(ss.StateError):
            sup.admit_planner("p0b", "run-p0b", 10)
        assert sup.decide().reason == "no_progress"


def _accept(sup, cand):
    sup.register_evidence("candidate", "CI", candidate=cand)
    sup.record_acceptance(cand, ["C1", "C2", "CI"])


def _exhaust_replans(sup):
    """P0 -> A1,A2 -> E1 -> R1 -> A3,A4 -> E2 -> R2 -> A5,A6 (T04)."""
    _planned(sup)
    for rev in (1, 2):
        n = 2 * rev - 1
        _launch(sup, "W1", n)
        _launch(sup, "W2", n + 1)
        _batch_done(sup, n)
        _fail(sup, n)
        assert sup.decide().kind == "replan"
        sup.admit_planner(f"p{rev}", f"run-p{rev}", 10)
        sup.record_plan(f"p{rev}", _plan(("W1", "W2"), rev), usage=2)
    _launch(sup, "W1", 5)
    _launch(sup, "W2", 6)
    _batch_done(sup, 5)


def test_replan_limit_precedes_attempt_limit_with_fresh_evidence(tmp_path):
    with _open(tmp_path, Clock()) as sup:
        _exhaust_replans(sup)
        _fail(sup, 6)                                   # E3 arrives
        decision = sup.decide()
        assert (decision.kind, decision.reason) == ("stopping", "replan_limit")
        with pytest.raises(ss.StateError):
            sup.admit_planner("p3", "run-p3", 10)
        with pytest.raises(ss.StateError):
            _launch(sup, "W1", 7)


def test_sixth_attempt_acceptance_beats_ceiling(tmp_path):
    with _open(tmp_path, Clock()) as sup:
        _exhaust_replans(sup)
        _accept(sup, digest("K6"))
        assert sup.decide().outcome == "SUCCEEDED"


def test_attempt_limit_with_one_replan_left(tmp_path):
    with _open(tmp_path, Clock()) as sup:
        _planned(sup, ("W1", "W2", "W3", "W4"))
        for n in range(1, 5, 2):
            _launch(sup, f"W{n}", n)
            _launch(sup, f"W{n + 1}", n + 1)
            _batch_done(sup, n)
        _fail(sup, 4)
        sup.admit_planner("p1", "run-p1", 10)
        sup.record_plan("p1", _plan(("W5", "W6"), 1), usage=2)
        _launch(sup, "W5", 5)
        _launch(sup, "W6", 6)
        _batch_done(sup, 5)
        _fail(sup, 6)
        decision = sup.decide()
        assert (decision.kind, decision.reason) == ("stopping", "attempt_limit")


def test_acceptance_needs_bound_registered_candidate(tmp_path):
    with _open(tmp_path, Clock()) as sup:
        _planned(sup)
        with pytest.raises(ss.StateError):
            sup.record_acceptance(digest("unbound"), ["C1", "C2", "CI"])
        sup.register_evidence("candidate", "CI", candidate=digest("K"))
        with pytest.raises(ss.StateError):
            sup.record_acceptance(digest("K"), ["C1", "C9"])
        sup.record_acceptance(digest("K"), ["C1", "C2"])        # partial criteria
        _launch(sup, "W1", 1)
        _launch(sup, "W2", 2)
        assert sup.decide().kind == "wait"                      # never early success
        _batch_done(sup, 1)
        assert sup.decide().reason != "succeeded"


def test_cancel_stop_deadline_survives_restart_at_t50(tmp_path):
    clock = Clock(1000.0)
    with _open(tmp_path, clock) as sup:
        _planned(sup)
        _launch(sup, "W1", 1)
        sup.request_stop("cancelled")
        with pytest.raises(ss.StateError):
            _launch(sup, "W2", 2)
        with pytest.raises(ss.StateError):
            sup.admit_report("r1", "run-r1", 5)
    clock.t = 1050.0
    with _open(tmp_path, clock) as sup:
        assert sup.stop_deadline() == 1060.0
        sup.request_stop("cancelled")                 # idempotent; never extends
        assert sup.stop_deadline() == 1060.0
        assert sup.decide().kind == "wait"
        with pytest.raises(ss.StateError):
            sup.register_evidence("candidate", "CI", candidate=digest("K"))
        clock.t = 1060.0
        final = sup.decide()
        assert (final.outcome, final.reason) == ("BLOCKED", "cancel_incomplete")
        assert final.data["unresolved"] == 1


def test_stale_supervisor_handle_cannot_admit(tmp_path):
    clock = Clock()
    old = _open(tmp_path, clock).__enter__()
    _planned(old)
    old.close()
    with _open(tmp_path, clock) as new:
        with pytest.raises((ss.StateError, sj.JournalError)):
            _launch(old, "W1", 1)
        assert new.counters()["attempts"] == 0


def test_quiescent_cancel_is_cancelled(tmp_path):
    with _open(tmp_path, Clock()) as sup:
        _planned(sup)
        _launch(sup, "W1", 1)
        _finish(sup, 1)
        sup.request_stop("cancelled")
        assert sup.decide().outcome == "CANCELLED"


@pytest.mark.parametrize("unknown,expected", [(True, ("BLOCKED", "unresolved_effects")),
                                              (False, ("PARTIAL", "deadline"))])
def test_deadline_stops_without_success(tmp_path, unknown, expected):
    clock = Clock(1000.0)
    with _open(tmp_path, clock, deadline=2000.0) as sup:
        _planned(sup)
        _launch(sup, "W1", 1)
        sup.register_evidence("candidate", "CI", candidate=digest("K"))
        sup.record_acceptance(digest("K"), ["C1", "C2", "CI"])
        if unknown:
            sup.observe_attempt("a1", "UNKNOWN", None, False, None)
        else:
            _finish(sup, 1)
        clock.t = 2000.0
        assert sup.decide().kind == "stopping"
        with pytest.raises(ss.StateError):
            sup.admit_planner("p1", "run-p1", 10)
        clock.t = 2060.0
        final = sup.decide()
        assert (final.outcome, final.reason) == expected


def test_single_report_slot_consumed_even_if_lost(tmp_path):
    clock = Clock()
    with _open(tmp_path, clock) as sup:
        _planned(sup, ("W1",))
        _launch(sup, "W1", 1)
        _finish(sup, 1)
        _accept(sup, digest("K"))
        assert sup.admit_report("r1", "run-r1", 5).kind == "call_report"
    with _open(tmp_path, clock) as sup:
        with pytest.raises(ss.StateError):
            sup.admit_report("r2", "run-r2", 5)
        assert sup.totals()["reserved"] == 5
        assert sup.decide().kind == "wait"
        sup.report_done("r1", None, False)
        assert sup.decide().outcome == "SUCCEEDED"


def _ready_closeout(sup):
    _planned(sup, ("W1",))
    _launch(sup, "W1", 1)
    _finish(sup, 1)
    _accept(sup, digest("K"))


def _succeeded(sup):
    _ready_closeout(sup)
    assert sup.decide().outcome == "SUCCEEDED"


def test_terminal_success_freezes_even_optional_report_admission(tmp_path):
    with _open(tmp_path, Clock()) as sup:
        _succeeded(sup)
        with pytest.raises(ss.StateError):
            sup.admit_report("r1", "run-r1", 5)


def test_unresolved_prefinal_report_cannot_coexist_with_success_after_restart(tmp_path):
    clock = Clock()
    with _open(tmp_path, clock) as sup:
        _planned(sup, ("W1",))
        _launch(sup, "W1", 1)
        _finish(sup, 1)
        _accept(sup, digest("K"))
        sup.admit_report("r1", "run-r1", 5)
        assert sup.decide().kind == "wait"
    with _open(tmp_path, clock) as sup:
        assert sup.decide().kind == "wait"
        sup.request_stop("deadline")
        clock.t = sup.stop_deadline()
        outcome = sup.decide()
        assert outcome.outcome == "BLOCKED" and outcome.data["unresolved"] == 1


def _refused(sup, admit, match="usage"):
    n = len(sup._journal.records)
    with pytest.raises(ss.StateError, match=match):
        admit()
    assert len(sup._journal.records) == n


@pytest.mark.parametrize("bad", [0, -1, True, 1.5, "5", None])
def test_every_admission_rejects_malformed_reserve_and_appends_nothing(tmp_path, bad):
    with _open(tmp_path, Clock()) as sup:
        _refused(sup, lambda: sup.admit_planner("p0", "run-p0", bad))
        _planned(sup, ("W1",))
        _refused(sup, lambda: sup.admit_worker("W1", "a1", str(uuid.uuid4()), "run-a1",
                                               "wt-a1", bad))
        _launch(sup, "W1", 1)
        _finish(sup, 1)
        _accept(sup, digest("K"))
        assert sup.decide().outcome == "SUCCEEDED"
        _refused(sup, lambda: sup.admit_report("r1", "run-r1", bad))


def test_admission_refused_when_known_plus_unresolved_plus_reserve_exceeds_max(tmp_path):
    clock = Clock()
    with _open(tmp_path, clock, max_usage=202) as sup:
        _planned(sup)                                   # planner measured 3
        _launch(sup, "W1", 1)                           # 3 known + 100 unresolved
        _refused(sup, lambda: _launch(sup, "W2", 2))    # 3 + 100 + 100 = 203 > 202
        _finish(sup, 1, usage=None)                     # unknown keeps its full reservation
    with _open(tmp_path, clock, max_usage=202) as sup:
        assert sup.totals()["reserved"] == 100
        _refused(sup, lambda: _launch(sup, "W2", 2))
        assert sup.admit_worker("W2", "a2", str(uuid.uuid4()), "run-a2", "wt-a2",
                                99).kind == "launch_worker"    # exactly at the limit is allowed


def test_measured_overrun_blocks_every_future_admission(tmp_path):
    clock = Clock()
    with _open(tmp_path, clock, max_usage=150) as sup:
        _planned(sup, ("W1", "W2"))
        _launch(sup, "W1", 1)
        _finish(sup, 1, usage=500, outcome="failed")    # overran its estimate
        _refused(sup, lambda: _launch(sup, "W2", 2))
        _refused(sup, lambda: sup.admit_planner("p1", "run-p1", 1))
        # The driver is told to stop, not to launch into a refusal loop.
        assert sup.decide(reserve=100).reason == "usage_limit"
        assert sup.decide().kind == "final" and sup.decide().reason == "usage_limit"


def test_usage_limit_waits_for_in_flight_work_before_stopping(tmp_path):
    with _open(tmp_path, Clock(), max_usage=113) as sup:
        _planned(sup, ("W1", "W2"))                     # 3 measured
        _launch(sup, "W1", 1)                           # +100 reserved
        assert sup.decide(reserve=100) == ss.Decision("wait", "usage_limit")
        _finish(sup, 1, usage=50, outcome="failed")
        d = sup.decide(reserve=100)
        assert (d.kind, d.reason) == ("stopping", "usage_limit")
        assert sup.decide().outcome == "PARTIAL"


@pytest.mark.parametrize("usages,known,total,unknown", [
    ((0, 0), 0, 0, 0), ((None, None), 0, None, 3), ((0, None), 0, None, 2)])
def test_totals_separate_known_subtotal_from_unknown(tmp_path, usages, known, total, unknown):
    with _open(tmp_path, Clock()) as sup:
        sup.admit_planner("p0", "run-p0", 10)
        sup.record_plan("p0", _plan(("W1", "W2"), 0), usage=usages[1])
        _launch(sup, "W1", 1)
        _launch(sup, "W2", 2)
        _finish(sup, 1, usage=usages[0])
        _finish(sup, 2, usage=usages[1])
        t = sup.totals()
        assert (t["charged"], t["total_usage"], t["unknown_calls"]) == (known, total, unknown)


def test_report_done_settles_measured_usage_once(tmp_path):
    clock = Clock()
    with _open(tmp_path, clock) as sup:
        _ready_closeout(sup)
        sup.admit_report("r1", "run-r1", 5)
        assert sup.totals()["reserved"] == 5
        sup.report_done("r1", 0, True)                  # measured zero is a real observation
    with _open(tmp_path, clock) as sup:
        t = sup.totals()
        assert (t["reserved"], t["unknown_calls"], t["total_usage"]) == (0, 0, t["charged"])
        n = len(sup._journal.records)
        assert sup.report_done("r1", 0, True) is None   # identical re-observation is idempotent
        for args in (("r1", 1, True), ("r1", 0, False), ("p0", 0, True), ("rX", 0, True)):
            with pytest.raises(ss.StateError):
                sup.report_done(*args)
        assert len(sup._journal.records) == n
        with pytest.raises(ss.StateError):
            sup.admit_report("r2", "run-r2", 5)          # no slot refund


def test_lost_report_stays_unknown_across_restart(tmp_path):
    clock = Clock()
    with _open(tmp_path, clock) as sup:
        _ready_closeout(sup)
        sup.admit_report("r1", "run-r1", 5)
        sup.report_done("r1", None, False)
    with _open(tmp_path, clock) as sup:
        t = sup.totals()
        assert (t["reserved"], t["unknown_calls"], t["total_usage"]) == (5, 1, None)
        for bad in ((-1, True), (True, True), (1.0, True), (None, "yes")):
            with pytest.raises(ss.StateError):
                sup.report_done("r1", *bad)


def _rich_plan(revision=0):
    return canonical({"schema_version": 1, "plan_revision": revision, "tasks": [
        {"task_id": "W1", "depends_on": [], "objective_digest": digest("obj-1"),
         "criteria": ["C1", "C2"], "hypothesis_ids": ["H1"]},
        {"task_id": "W2", "depends_on": ["W1"], "objective_digest": digest("obj-2"),
         "criteria": ["CI"], "hypothesis_ids": []}]})


def test_restart_reconstructs_full_approved_task_bindings(tmp_path):
    clock, raw = Clock(), _rich_plan()
    with _open(tmp_path, clock) as sup:
        sup.admit_planner("p0", "run-p0", 10)
        plan = sup.record_plan("p0", raw, usage=3)
        before = sup.plan_view()
    assert before["tasks"] == plan.tasks and before["revision"] == 0
    import hashlib
    canon = contract.canonical(contract.strict_json(raw))
    assert before["proposal_digest"] == hashlib.sha256(canon).hexdigest()
    with _open(tmp_path, clock) as sup:
        assert sup.plan_view() == before
    log = (tmp_path / "state" / sj.LOG_NAME).read_bytes()
    assert b"obj-1" not in log and digest("obj-1").encode() in log   # digest only, no prose


def test_effect_identity_is_derived_from_unique_ids_and_layout_is_bound_once(tmp_path):
    clock, art = Clock(), tmp_path / "artifacts"
    sup = ss.Supervisor(tmp_path / "state", _env(tmp_path), clock, artifact_dir=art)
    with sup:
        p = sup.admit_planner("p0", "run-p0", 10)
        sup.record_plan("p0", _plan(("W1",), 0), usage=3)
        w = _launch(sup, "W1", 1).data
        epoch = sup._journal.epoch
    assert uuid.UUID(p.data["launch_id"]) and p.data["epoch"] == w["epoch"] == epoch
    assert w["transcript_ref"] == "runs/run-a1/run-a1.md"          # unique basename = run_id
    assert w["receipt_ref"] == f"runs/run-a1/{w['launch_id']}.receipt.json"
    assert (w["artifact_ref"], w["worktree_ref"]) == ("runs/run-a1/artifacts", "worktrees/a1")
    assert w["job_name"] == f"AgentControl-{w['launch_id']}" and w["layout"] is not None
    for other in (tmp_path / "elsewhere", None):                   # different layout holds
        with pytest.raises(ss.StateError):
            with ss.Supervisor(tmp_path / "state", _env(tmp_path), clock, artifact_dir=other):
                pass
    with ss.Supervisor(tmp_path / "state", _env(tmp_path), clock, artifact_dir=art) as sup:
        assert sup.effect_identity("a1") == w
        assert sup.decide().kind == "reconcile"   # admitted, never proven spawned: UNKNOWN
        with pytest.raises(ss.StateError):
            sup.record_process_identity("a1", w["launch_id"], 4242, "birth-1")  # stale epoch


def test_process_identity_is_bound_to_effect_and_immutable(tmp_path):
    with _open(tmp_path, Clock()) as sup:
        p = sup.admit_planner("p0", "run-p0", 10).data
        sup.record_process_identity("p0", p["launch_id"], 11, "birth-p")
        sup.record_plan("p0", _plan(("W1",), 0), usage=3)
        w = _launch(sup, "W1", 1).data
        rec = sup.record_process_identity("a1", w["launch_id"], 4242, "birth-1")
        n = len(sup._journal.records)
        assert sup.record_process_identity("a1", w["launch_id"], 4242, "birth-1") is None
        assert len(sup._journal.records) == n                       # idempotent re-read
        for bad in (("a1", w["launch_id"], 4243, "birth-1"), ("a1", w["launch_id"], 4242, "b2"),
                    ("a1", str(uuid.uuid4()), 4242, "birth-1"), ("a9", w["launch_id"], 1, "b"),
                    ("a1", w["launch_id"], -1, "birth-1")):
            with pytest.raises(ss.StateError):
                sup.record_process_identity(*bad)
        assert rec["pid"] == 4242 and sup.effect_identity("a1")["process"] == {
            "pid": 4242, "birth": "birth-1"}
        assert sup.effect_identity("p0")["process"] == {"pid": 11, "birth": "birth-p"}


def test_reselecting_old_candidate_clears_acceptance_without_new_progress(tmp_path):
    a, b = digest("candidate-a"), digest("candidate-b")
    with _open(tmp_path, Clock()) as sup:
        sup.register_evidence("candidate", "C1", a)
        sup.register_evidence("candidate", "C1", b)
        sup.record_acceptance(b, ["C1"])
        count = len(sup.evidence)
        assert sup.register_evidence("candidate", "C1", a) is None
        assert sup.candidate == a and sup.accepted == set()
        assert len(sup.evidence) == count
    with _open(tmp_path, Clock()) as sup:
        assert sup.candidate == a and sup.accepted == set()
        assert len(sup.evidence) == count


def test_empty_acceptance_replaces_an_older_partial_result(tmp_path):
    with _open(tmp_path, Clock()) as sup:
        candidate = digest("candidate")
        sup.register_evidence("candidate", "C1", candidate)
        sup.record_acceptance(candidate, ["C1"])
        sup.record_acceptance(candidate, [])
        assert sup.accepted == set()


def test_unclassified_model_token_is_not_trusted_failure_evidence(tmp_path):
    with _open(tmp_path, Clock()) as sup:
        with pytest.raises(ss.StateError):
            sup.register_evidence("check_failure", "C1", digest("candidate"),
                                  check_id=sup.envelope.check_ids[0],
                                  failure_code="arbitrary_model_token")


def test_stopping_counts_unresolved_planner_process_as_a_survivor(tmp_path):
    clock = Clock()
    with _open(tmp_path, clock) as sup:
        sup.admit_planner("p0", "run-p0", 10)
        sup.request_stop("deadline")
        assert sup.decide().kind == "wait"
        clock.t += 60
        final = sup.decide()
        assert (final.outcome, final.reason) == ("BLOCKED", "unresolved_effects")
        assert final.data["unresolved"] == 1


def test_partial_reported_usage_blocks_admission_without_faking_a_complete_total(tmp_path):
    raw, approved = envelope_bytes(tmp_path, max_usage=100)
    env = contract.load_envelope(raw, approved, tmp_path)
    with ss.Supervisor(tmp_path / "s", env, Clock()) as sup:
        sup.admit_planner("p0", "run-p0", 10)
        sup.observe_usage_floor("p0", 95)
        assert sup.totals()["charged"] == 95 and sup.totals()["reserved"] == 10
        assert sup.totals()["total_usage"] is None
        sup.planner_failed("p0", None)
        with pytest.raises(ss.StateError, match="usage"):
            sup.admit_planner("p1", "run-p1", 10)
    with ss.Supervisor(tmp_path / "s", env, Clock()) as sup:
        assert sup.totals()["charged"] == 95 and sup.totals()["total_usage"] is None


def test_usage_floor_settles_once_and_can_never_refund_reported_work(tmp_path):
    with _open(tmp_path, Clock()) as sup:
        sup.admit_planner("p0", "run-p0", 10)
        sup.observe_usage_floor("p0", 5)
        assert sup.observe_usage_floor("p0", 5) is None
        with pytest.raises(ss.StateError):
            sup.observe_usage_floor("p0", 4)
        with pytest.raises(ss.StateError):
            sup.planner_failed("p0", 4)
        sup.planner_failed("p0", 7)
        assert sup.totals()["charged"] == 7 and sup.totals()["reserved"] == 0
        assert sup.totals()["total_usage"] == 7


def test_unfinished_evaluation_blocks_replan_success_and_false_quiet_stop(tmp_path):
    clock = Clock()
    with _open(tmp_path, clock) as sup:
        _planned(sup, ("W1",))
        _launch(sup, "W1", 1)
        _finish(sup, 1)
        candidate = digest("integrated")
        sup.register_evidence("candidate", "C1", candidate)
        sup.begin_evaluation("e0", candidate, "evaluations/rev-0")
        assert sup.decide().kind == "reconcile"
        with pytest.raises(ss.StateError):
            sup.record_acceptance(candidate, list(sup.envelope.criteria))
    with _open(tmp_path, clock) as sup:
        assert sup.decide().kind == "reconcile"
        sup.request_stop("deadline")
        clock.t += 60
        final = sup.decide()
        assert final.outcome == "BLOCKED" and final.data["unresolved"] == 1


def test_bound_quiescent_evaluation_can_finish_once_but_not_run_twice_in_a_batch(tmp_path):
    with _open(tmp_path, Clock()) as sup:
        _planned(sup, ("W1",))
        _launch(sup, "W1", 1)
        _finish(sup, 1)
        candidate = digest("integrated")
        sup.register_evidence("candidate", "C1", candidate)
        sup.begin_evaluation("e0", candidate, "evaluations/rev-0")
        with pytest.raises(ss.StateError):
            sup.finish_evaluation("e0", digest("facts"), quiescent=False)
        sup.finish_evaluation("e0", digest("facts"), quiescent=True)
        assert sup.finish_evaluation("e0", digest("facts"), quiescent=True) is None
        with pytest.raises(ss.StateError):
            sup.begin_evaluation("e1", candidate, "evaluations/rev-0-again")
        sup.record_acceptance(candidate, list(sup.envelope.criteria))
        assert sup.decide().outcome == "SUCCEEDED"


def test_materialization_is_held_before_candidate_identity_can_be_known(tmp_path):
    with _open(tmp_path, Clock()) as sup:
        _planned(sup, ("W1",))
        _launch(sup, "W1", 1)
        _finish(sup, 1)
        sup.begin_evaluation("e0", None, "batches/rev-0")
        assert sup.decide().kind == "reconcile"
        candidate = digest("new materialized content")
        sup.bind_evaluation_candidate("e0", candidate)
        assert sup.candidate == candidate and sup.accepted == set()
        with pytest.raises(ss.StateError):
            sup.bind_evaluation_candidate("e0", digest("different content"))
        sup.finish_evaluation("e0", digest("facts"), quiescent=True)
        sup.register_evidence("candidate", "C1", candidate)
        sup.record_acceptance(candidate, list(sup.envelope.criteria))
        assert sup.decide().outcome == "SUCCEEDED"


@pytest.mark.parametrize("reason", ["deadline", "cancelled", "no_progress"])
def test_late_deadline_observation_cannot_extend_cleanup_time(tmp_path, reason):
    clock = Clock()
    with _open(tmp_path, clock) as sup:
        sup.admit_planner("p0", "run-p0", 10)
        clock.t = sup.envelope.deadline + 500
        sup.request_stop(reason)
        assert sup.stop_deadline() == sup.envelope.deadline + 60
        assert sup.decide().outcome == "BLOCKED"


def _rewrite(tmp_path, mutate):
    """Forge a semantically wrong journal whose hash chain is recomputed and valid."""
    path = tmp_path / "state" / sj.LOG_NAME
    prev, out = "0" * 64, []
    for line in path.read_bytes().splitlines():
        rec = contract.strict_json(line)
        body = {k: rec[k] for k in ("v", "seq", "epoch", "t", "kind", "data")}
        mutate(body)
        prev = sj._hash(prev, body)
        out.append(canonical({**body, "h": prev}))
    path.write_bytes(b"\n".join(out) + b"\n")


@pytest.mark.parametrize("forge", ["revision", "dangling_edge", "dup_task", "unknown_criterion",
                                   "acceptance_wrong_candidate", "duplicate_call"])
def test_replay_rejects_semantically_forged_but_rehashed_records(tmp_path, forge):
    clock = Clock()
    with _open(tmp_path, clock) as sup:
        sup.admit_planner("p0", "run-p0", 10)
        sup.record_plan("p0", _rich_plan(), usage=3)
        sup.register_evidence("candidate", "C1", digest("cand-A"))
        sup.record_acceptance(digest("cand-A"), ["C1"])

    def mutate(body):
        d = body["data"]
        if body["kind"] == "plan":
            t = d["tasks"]
            if forge == "revision":
                d["revision"] = 1
            elif forge == "dangling_edge":
                t[1]["depends_on"] = ["W9"]
            elif forge == "dup_task":
                t[1]["task_id"] = "W1"
            elif forge == "unknown_criterion":
                t[0]["criteria"] = ["C9"]
        elif body["kind"] == "acceptance" and forge == "acceptance_wrong_candidate":
            d["candidate"] = digest("cand-B")
        elif body["kind"] == "evidence" and forge == "duplicate_call":
            body["kind"], body["data"] = "call", {
                "call_id": "p0", "run_id": "run-p9", "role": "planner", "reserve": 1,
                "revision": 0, "watermark": 0}
    _rewrite(tmp_path, mutate)
    with pytest.raises((ss.StateError, sj.JournalError)):
        with _open(tmp_path, clock):
            pass

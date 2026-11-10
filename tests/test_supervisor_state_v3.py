import hashlib
import uuid

import pytest

from supervisor_helpers import canonical, digest, load
from supervisor_v2_helpers import envelope_dict_v2

state = load("supervisor_state")
codec = load("supervisor_contract_v3")
journal = load("supervisor_journal")
REJECT = (state.StateError, journal.JournalError)
INITIAL = digest("initial")
INITIAL_RECEIPT = digest("initial-receipt")


def envelope(root, kind="artifact-manifest", limits=None):
    data = envelope_dict_v2(root, limits, criteria=["C1", "C2"])
    commit = data.pop("baseline_commit")
    data.update(schema_version=3, continuation=dict(codec.CONTINUATION), candidate={
        "kind": kind, "initial_sha256": INITIAL, "inputs": ["src"],
        "bounds": {"max_entries": 100, "max_file_bytes": 1000,
                   "max_total_bytes": 10000, "max_manifest_bytes": 1000,
                   "max_path_bytes": 512, "max_seconds": 10},
    })
    if kind == "git":
        data["candidate"]["baseline_commit"] = commit
    return codec.load_envelope3(data, root)


def supervisor(root, env):
    return state.Supervisor(root / "state", env, lambda: 100.0)


def plan(s, revision=0, tasks=None):
    s.admit_planner(f"p{revision}", f"planner{revision}", 2)
    tasks = tasks if tasks is not None else [("T", [], ["C1", "C2"])]
    s.record_plan(f"p{revision}", canonical({
        "schema_version": 1, "plan_revision": revision,
        "tasks": [{"task_id": t, "depends_on": deps, "objective_digest": digest(t),
                   "criteria": criteria, "hypothesis_ids": []}
                  for t, deps, criteria in tasks],
    }), None)


def initial(s):
    return s.register_snapshot3(INITIAL, "snapshots/initial", INITIAL_RECEIPT,
                                digest("scope"))


def launch(s, name="a", task="T"):
    return s.admit_worker(task, name, str(uuid.uuid4()), f"run-{name}",
                          f"tree-{name}", 3)


def collect(s, name="a", outcome="yielded"):
    s.observe_attempt(name, "COLLECTED", outcome, True, None)


def capture(s, label="next", candidate=None, attempt="a", **overrides):
    args = dict(seed=INITIAL_RECEIPT, source_attempts=(attempt,), kind="worker")
    args.update(overrides)
    return s.register_snapshot3(candidate or digest(label), f"snapshots/{label}",
                                digest(f"receipt-{label}"), digest("scope"), **args)


@pytest.mark.parametrize("kind", ["git", "artifact-manifest"])
def test_v3_common_rules_and_replay(tmp_path, kind):
    env = envelope(tmp_path, kind)
    if kind == "artifact-manifest":
        with pytest.raises(codec.ContractError):
            _ = env.baseline_commit
    with supervisor(tmp_path, env) as s:
        plan(s, tasks=[(f"T{i}", [], ["C1"]) for i in range(64)])
        assert s.calls["p0"]["admission_reason"] == "initial"
        assert len(s.plan_view()["tasks"]) == 64
        assert s.envelope.limits["max_total_attempts"] is None
    with supervisor(tmp_path, env) as s:
        assert len(s.plan_view()["tasks"]) == 64
        assert s.counters()["planner_calls"] == 1
        assert s.totals()["total_usage"] is None


def test_registered_seed_atomic_worker_and_restart(tmp_path):
    env = envelope(tmp_path)
    with supervisor(tmp_path, env) as s:
        plan(s)
        with pytest.raises(REJECT):
            launch(s)
        initial(s)
        effect = launch(s)
        assert effect.data["seed_candidate"] == INITIAL
        assert effect.data["seed_ref"] == "snapshots/initial"
        assert effect.data["seed_receipt"] == INITIAL_RECEIPT
        assert effect.data["predecessor"] is None
        assert effect.data["attempt_number"] == 1
        assert effect.data["planner_calls"] == 1
        durable = next(r.data for r in s._journal.records if r.kind == "worker")
        assert durable == effect.data
    with supervisor(tmp_path, env) as s:
        assert s.attempts["a"]["seed_receipt"] == INITIAL_RECEIPT
        assert s.attempts["a"]["lifecycle"] == "UNKNOWN"
        assert s.counters()["attempts"] == 1
        assert s.totals()["reserved"] == 5
        with pytest.raises(REJECT):
            launch(s, "b")


def test_yield_selects_current_context_and_keeps_receipt_bindings(tmp_path):
    with supervisor(tmp_path, envelope(tmp_path)) as s:
        initial(s)
        plan(s)
        first = launch(s)
        collect(s)
        a = capture(s)
        count = len(s._journal.records)
        assert capture(s) == a
        assert len(s._journal.records) == count
        b = capture(s, "second-receipt", candidate=a["candidate"])
        assert a["receipt_digest"] != b["receipt_digest"]
        assert s.candidate == INITIAL
        s.record_yield3("a", a["receipt_digest"], digest("handoff"))
        s.select_candidate(b["candidate"], receipt_digest=b["receipt_digest"])
        second = launch(s, "b")
        assert second.data["seed_candidate"] == b["candidate"] != INITIAL
        assert second.data["seed_receipt"] == b["receipt_digest"]
        assert second.data["predecessor"] == "a"
        assert second.data["continuation_key"] != first.data["continuation_key"]
        with pytest.raises(REJECT):
            s._write("worker", dict(second.data))
        assert s.counters()["attempts"] == 2


def test_collection_rejects_unknown_unquiet_stale_and_unowned(tmp_path):
    with supervisor(tmp_path, envelope(tmp_path)) as s:
        initial(s)
        plan(s)
        launch(s)
        s.observe_attempt("a", "TERMINAL", "failed", False, None)
        with pytest.raises(REJECT):
            capture(s)
        s.observe_attempt("a", "UNKNOWN", None, False, None)
        with pytest.raises(REJECT):
            capture(s)
        collect(s, outcome="failed")
        with pytest.raises(REJECT):
            s.record_retry_proof("a", "effects_reconciled", digest("unanchored"))
        with pytest.raises(REJECT):
            capture(s, effects_known=False)
        with pytest.raises(REJECT):
            s.register_snapshot3(digest("x"), "../escape", digest("r"), digest("scope"),
                                 seed=INITIAL_RECEIPT, source_attempts=("a",), kind="worker")
        receipt = capture(s)
        s.select_candidate(receipt["candidate"])
        with pytest.raises(REJECT):
            capture(s, "stale")
        with pytest.raises(REJECT):
            s.record_yield3("missing", receipt["receipt_digest"], digest("handoff"))
        plan(s, 1)
        with pytest.raises(REJECT):
            capture(s, "old-revision", seed=receipt["receipt_digest"])


@pytest.mark.parametrize("cap", [None, 1])
def test_noop_requests_diagnostic_not_hidden_quota(tmp_path, cap):
    with supervisor(tmp_path, envelope(tmp_path, limits={"max_total_attempts": cap})) as s:
        initial(s)
        plan(s)
        launch(s)
        collect(s)
        receipt = capture(s, candidate=INITIAL)
        s.record_yield3("a", receipt["receipt_digest"], digest("handoff"))
        s.select_candidate(INITIAL, receipt_digest=receipt["receipt_digest"])
        decision = s.decide()
        if cap is not None:
            assert (decision.kind, decision.reason) == ("stopping", "attempt_limit")
            return
        assert (decision.kind, decision.reason) == ("replan", "diagnostic")
        assert s.final is None and s.stop is None
        plan(s, 1, [("D", [], [])])
        assert s.calls["p1"]["admission_reason"] == "diagnostic"
        launch(s, "diagnostic", "D")
        collect(s, "diagnostic", "succeeded")
        assert not s.accepted
        assert s.decide().kind == "replan"
        assert s.counters()["attempts"] == 2
        assert s.envelope.limits["max_replans"] is None


def test_acceptance_requires_exact_protected_facts_and_not_worker_completion(tmp_path):
    with supervisor(tmp_path, envelope(tmp_path)) as s:
        initial(s)
        plan(s, tasks=[("T", [], ["C1"]), ("U", ["T"], ["C2"])])
        launch(s)
        collect(s, outcome="succeeded")
        assert "U" not in s._launchable()
        assert not s.accepted
        receipt = capture(s)
        s.select_candidate(receipt["candidate"])
        facts = {"criteria": ["C1"]}
        with pytest.raises(REJECT):
            s.record_acceptance(s.candidate, facts, evaluation_id="missing")
        s.begin_evaluation("e", s.candidate, "evaluations/e")
        with pytest.raises(REJECT):
            s.record_acceptance(s.candidate, facts, evaluation_id="e")
        s.finish_evaluation("e", hashlib.sha256(canonical(facts)).hexdigest(), quiescent=True)
        with pytest.raises(REJECT):
            s.record_acceptance(INITIAL, facts, evaluation_id="e")
        with pytest.raises(REJECT):
            s.record_acceptance(s.candidate, {"criteria": ["C1", "C2"]}, evaluation_id="e")
        s.record_acceptance(s.candidate, facts, evaluation_id="e")
        assert s.decide().kind != "final"
        assert "U" in s._launchable()
        launch(s, "dependent", "U")
        with pytest.raises(REJECT):
            s.record_acceptance(s.candidate, facts, evaluation_id="e")


@pytest.mark.parametrize("mismatch", ["epoch", "revision", "candidate"])
def test_completed_evaluation_cannot_cross_context(tmp_path, mismatch):
    env = envelope(tmp_path)
    facts = {"criteria": ["C1", "C2"]}
    with supervisor(tmp_path, env) as s:
        initial(s)
        plan(s)
        launch(s)
        collect(s, outcome="succeeded")
        a, b = capture(s), capture(s, "other")
        s.select_candidate(a["candidate"])
        s.begin_evaluation("e", s.candidate, "evaluations/e")
        s.finish_evaluation("e", hashlib.sha256(canonical(facts)).hexdigest(), quiescent=True)
        if mismatch == "revision":
            plan(s, 1)
        elif mismatch == "candidate":
            s.select_candidate(b["candidate"])
        if mismatch != "epoch":
            with pytest.raises(REJECT):
                s.record_acceptance(s.candidate, facts, evaluation_id="e")
    if mismatch == "epoch":
        with supervisor(tmp_path, env) as s:
            with pytest.raises(REJECT):
                s.record_acceptance(s.candidate, facts, evaluation_id="e")

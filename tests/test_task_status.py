import hashlib
import json
import math
from pathlib import Path

import pytest

from supervisor_helpers import canonical, digest, load
from test_supervisor_state_v3 import (INITIAL, capture, collect, envelope, initial, launch,
                                      plan, supervisor)

ts = load("task_status")
FACTS = {"criteria": ["C1", "C2"]}


def _disk(s):
    root = Path(s.envelope.project_root) / "state"
    return len(s._journal.records), sorted((str(p), p.stat().st_size)
                                           for p in root.rglob("*") if p.is_file())


def project(s):
    before = _disk(s)
    out = ts.project_task_status(s)
    after = _disk(s)
    assert before == after  # projection is pure: no decide()/STOP write
    assert set(out) == set(ts.FIELDS)
    return out


def evaluated(s, facts=FACTS):
    initial(s)
    plan(s)
    launch(s)
    collect(s, outcome="succeeded")
    s.select_candidate(capture(s)["candidate"])
    s.begin_evaluation("e", s.candidate, "evaluations/e")
    s.finish_evaluation("e", hashlib.sha256(canonical(facts)).hexdigest(), quiescent=True)


def test_initial_planning_then_missing_initial_snapshot(tmp_path):
    with supervisor(tmp_path, envelope(tmp_path)) as s:
        out = project(s)
        assert (out["state"], out["reason"]) == ("continuing", "planning")
        assert out["usage"]["total_usage"] == 0 and out["owner_epoch"] >= 1
        assert out["limits"]["max_total_attempts"] is None
        plan(s)
        assert project(s)["usage"]["total_usage"] is None  # unmeasured call: unknown, not 0
        assert (project(s)["state"], project(s)["reason"]) == ("reconciliation",
                                                              "initial_snapshot")


def test_active_slice_unknown_and_lifecycle_alone(tmp_path):
    with supervisor(tmp_path, envelope(tmp_path)) as s:
        initial(s)
        plan(s)
        out = project(s)
        assert (out["state"], out["reason"]) == ("continuing", "next_slice_ready")
        launch(s)
        s.observe_attempt("a", "RUNNING", None, False, None)
        out = project(s)
        assert (out["state"], out["reason"], out["active_workers"]) == (
            "continuing", "worker_active", 1)
        assert out["verifier_owner"] == "worker"
        s.observe_attempt("a", "UNKNOWN", None, False, None)
        out = project(s)
        assert (out["state"], out["reason"]) == ("reconciliation", "effect_uncertain")
        assert out["unresolved_workers"] == 1
        collect(s, outcome="succeeded")
        out = project(s)
        assert out["state"] != "verified-complete" and out["satisfied_criteria"] == []


def test_selected_candidate_awaits_coordinator_verification(tmp_path):
    with supervisor(tmp_path, envelope(tmp_path)) as s:
        initial(s)
        plan(s)
        launch(s)
        collect(s, outcome="succeeded")
        s.select_candidate(capture(s)["candidate"])
        out = project(s)
        assert (out["state"], out["reason"], out["verifier_owner"], out["rerun_owed"]) == (
            "awaiting-verification", "verification_due", "coordinator", True)
        s.begin_evaluation("e", s.candidate, "evaluations/e")
        out = project(s)
        assert (out["state"], out["pending_evaluations"]) == ("awaiting-verification", 1)
        assert out["candidate_sha256"] == s.candidate


def test_failed_criteria_with_next_slice_is_continuing_not_blocked(tmp_path):
    with supervisor(tmp_path, envelope(tmp_path)) as s:
        evaluated(s, {"criteria": ["C1"]})
        s.record_acceptance(s.candidate, {"criteria": ["C1"]}, evaluation_id="e")
        plan(s, 1, [("U", [], ["C2"])])
        out = project(s)
        assert (out["state"], out["reason"]) == ("continuing", "next_slice_ready")
        assert out["satisfied_criteria"] == ["C1"] and out["required_criteria"] == ["C1", "C2"]


def test_verified_complete_only_from_published_final(tmp_path):
    with supervisor(tmp_path, envelope(tmp_path)) as s:
        evaluated(s)
        s.record_acceptance(s.candidate, FACTS, evaluation_id="e")
        assert project(s)["state"] != "verified-complete"  # accepted but final unpublished
        assert s.decide().outcome == "SUCCEEDED"
        out = project(s)
        assert (out["state"], out["reason"], out["verifier_owner"], out["rerun_owed"]) == (
            "verified-complete", "verified", "none", False)
        doc = ts.validate_task_status(json.dumps(out), expected_envelope_sha256=out[
            "envelope_sha256"], expected_project_root=out["project_root"])
        assert doc == out


def test_inconsistent_final_and_false_owner_reconcile(tmp_path):
    with supervisor(tmp_path, envelope(tmp_path)) as s:
        evaluated(s)
        s.record_acceptance(s.candidate, FACTS, evaluation_id="e")
        s.decide()
        s.accepted = set()  # in-memory inconsistency: published success without acceptance
        assert (project(s)["state"], project(s)["reason"]) == ("reconciliation",
                                                              "state_inconsistent")
    with supervisor(tmp_path, envelope(tmp_path)) as s:
        s.owner_current = lambda epoch=None: False  # simulated lost ownership
        assert (project(s)["state"], project(s)["reason"]) == ("reconciliation",
                                                              "ownership_unavailable")


@pytest.mark.parametrize("cap", [1])
def test_published_stop_is_blocked_with_fixed_reason(tmp_path, cap):
    with supervisor(tmp_path, envelope(tmp_path, limits={"max_total_attempts": cap})) as s:
        initial(s)
        plan(s)
        launch(s)
        collect(s)
        receipt = capture(s, candidate=INITIAL)
        s.record_yield3("a", receipt["receipt_digest"], digest("handoff"))
        s.select_candidate(INITIAL, receipt_digest=receipt["receipt_digest"])
        assert s.decide().reason == "attempt_limit"
        out = project(s)
        assert (out["state"], out["reason"]) == ("blocked", "attempt_limit")
        assert out["limits"]["max_total_attempts"] == 1


def _valid(tmp_path):
    with supervisor(tmp_path, envelope(tmp_path)) as s:
        initial(s)
        plan(s)
        return project(s)


def _check(doc, env, root):
    return ts.validate_task_status(doc, expected_envelope_sha256=env,
                                   expected_project_root=root)


SECRET = "SECRET-BODY-VALUE"


@pytest.mark.parametrize("mutate", [
    lambda d: d.update(body=SECRET),
    lambda d: d.update(active_workers=True),
    lambda d: d.update(active_workers=1.0),
    lambda d: d.update(active_workers=-1),
    lambda d: d["usage"].update(total_usage=math.nan),
    lambda d: d["usage"].update(secret=SECRET),
    lambda d: d["limits"].update(max_total_attempts=1.5),
    lambda d: d.update(state=SECRET),
    lambda d: d.update(reason=SECRET),
    lambda d: d.update(verifier_owner=SECRET),
    lambda d: d.update(schema_version=2),
    lambda d: d.update(satisfied_criteria=["C9"]),
    lambda d: d.update(satisfied_criteria=["C2", "C1"]),
    lambda d: d.update(candidate_sha256=SECRET),
    lambda d: d.update(state="verified-complete", reason="verified"),
    lambda d: d.pop("usage"),
])
def test_validate_rejects_without_echo(tmp_path, mutate):
    good = _valid(tmp_path)
    assert _check(dict(good), good["envelope_sha256"], good["project_root"]) == good
    bad = json.loads(json.dumps(good))
    mutate(bad)
    with pytest.raises(ts.TaskStatusError) as err:
        _check(bad, good["envelope_sha256"], good["project_root"])
    assert SECRET not in str(err.value)


def test_validate_binds_expected_envelope_and_root_lexically(tmp_path):
    good = _valid(tmp_path)
    foreign = tmp_path / "does-not-exist"
    with pytest.raises(ts.TaskStatusError):
        _check(good, digest("other-approval"), good["project_root"])
    with pytest.raises(ts.TaskStatusError):
        _check(good, good["envelope_sha256"], str(foreign))
    assert not foreign.exists()
    with pytest.raises(ts.TaskStatusError):
        _check(b"\xff not json", good["envelope_sha256"], good["project_root"])
    with pytest.raises(ts.TaskStatusError):
        _check('{"a": 1, "a": 2}', good["envelope_sha256"], good["project_root"])


def test_module_documents_trust_limit():
    doc = ts.__doc__.lower()
    assert "not authenticated" in doc and "trusted registry" in doc

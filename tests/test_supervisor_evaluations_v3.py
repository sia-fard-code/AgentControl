import hashlib

import pytest

from supervisor_helpers import canonical
from test_supervisor_state_v3 import (REJECT, capture, collect, envelope, initial, launch,
                                      plan, supervisor)

FACTS = {"criteria": ["C1", "C2"]}
FACTS_DIGEST = hashlib.sha256(canonical(FACTS)).hexdigest()


def selected(s):
    initial(s)
    plan(s)
    launch(s)
    collect(s, outcome="succeeded")
    s.select_candidate(capture(s)["candidate"])


def test_second_same_revision_evaluation_at_planner_ceiling(tmp_path):
    env = envelope(tmp_path, limits={"max_planner_calls": 1})
    with supervisor(tmp_path, env) as s:
        selected(s)
        s.begin_evaluation("e1", s.candidate, "evaluations/e1")
        s.finish_evaluation("e1", FACTS_DIGEST, quiescent=True)
        s.begin_evaluation("e2", s.candidate, "evaluations/e2")
        assert s.counters()["planner_calls"] == 1
        s.finish_evaluation("e2", FACTS_DIGEST, quiescent=True)
        s.record_acceptance(s.candidate, FACTS, evaluation_id="e2")
    with supervisor(tmp_path, env) as s:  # journal replay of two completed evaluations
        assert all(e["done"] for e in s.evaluations.values()) and len(s.evaluations) == 2
        with pytest.raises(REJECT):
            s.admit_planner("p9", "planner9", 2)
        with pytest.raises(REJECT):  # accepted facts cannot be reused
            s.record_acceptance(s.candidate, FACTS, evaluation_id="e2")


def begin_data(s, eid, **overrides):
    data = {"evaluation_id": eid, "candidate": s.candidate, "artifact_ref": f"evaluations/{eid}",
            "revision": s.plan["revision"], "epoch": s._journal.epoch,
            "seed_receipt": s.seed_receipt3}
    data.update(overrides)
    return data


@pytest.mark.parametrize("stale", ["pending", "duplicate", "candidate", "revision",
                                   "epoch", "seed_receipt"])
def test_second_evaluation_rejects_pending_duplicate_and_stale(tmp_path, stale):
    with supervisor(tmp_path, envelope(tmp_path)) as s:
        selected(s)
        s.begin_evaluation("e1", s.candidate, "evaluations/e1")
        if stale != "pending":
            s.finish_evaluation("e1", FACTS_DIGEST, quiescent=True)
        overrides = {"pending": {}, "duplicate": {"evaluation_id": "e1"},
                     "candidate": {"candidate": "0" * 64}, "revision": {"revision": 7},
                     "epoch": {"epoch": s._journal.epoch + 1},
                     "seed_receipt": {"seed_receipt": "1" * 64}}[stale]
        count = len(s._journal.records)
        with pytest.raises(REJECT):
            s._write3("evaluation_begin", begin_data(s, "e2", **overrides))
        assert len(s._journal.records) == count


def test_replay_rejects_second_begin_while_first_pending(tmp_path):
    env = envelope(tmp_path)
    with supervisor(tmp_path, env) as s:
        selected(s)
        s.begin_evaluation("e1", s.candidate, "evaluations/e1")
        s._journal.transact("evaluation_begin", begin_data(s, "e2"))  # bypasses live check
    with pytest.raises(REJECT):
        with supervisor(tmp_path, env):
            pass

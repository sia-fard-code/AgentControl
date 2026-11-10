"""Concrete implementation-review counterexamples; real state and owned evidence, no models."""
import pytest

from supervisor_driver_helpers import CAPS, GOAL, FakeAdapter, setup, two_worker_planner
from supervisor_helpers import canonical, digest, load
import test_supervisor_state as st

supervise = load("supervise")
core = load("supervisor_state")
evidence = load("supervisor_evidence")


def _v2(fx):
    data = supervise.contract.strict_json(fx.raw)
    data.update(schema_version=2, limits=dict(supervise.contract.DEFAULT_LIMITS_V2),
                 deadline=None, max_usage=None)
    raw = canonical(data)
    fx.envelope = supervise.contract.load_envelope(raw, supervise._sha(raw), fx.project)
    return fx


@pytest.mark.parametrize("same_candidate", [False, True])
def test_uncommitted_frame_cannot_supply_its_own_quiescence_and_acceptance(tmp_path, same_candidate):
    fx = _v2(setup(tmp_path))
    fx.artifacts.mkdir()
    (fx.artifacts / "batches").mkdir()
    ad = FakeAdapter(fx.envelope, fx.artifacts, two_worker_planner)
    with core.Supervisor(fx.state, fx.envelope, lambda: 1000.0, artifact_dir=fx.artifacts) as sup:
        st._planned(sup, ("W1",))
        st._launch(sup, "W1", 1)
        st._finish(sup, 1)
        candidate = evidence.integrate(fx.project, fx.baseline, [], fx.artifacts / "batches" / "candidate-0")
        bound = candidate.candidate_sha256 if same_candidate else digest("a different candidate")
        sup.select_candidate(bound)
        sup.begin_evaluation("eval-0", bound, "batches/rev-0.json")
        frame = {"v": 1, "envelope": fx.envelope.digest, "revision": 0, "evaluation_id": "eval-0",
                 "candidate": candidate.candidate_sha256, "candidate_ref": "batches/candidate-0",
                 "manifest_sha": candidate.manifest_sha256, "registry": fx.envelope.validator_digest,
                 "selection": [], "satisfied": list(fx.envelope.criteria), "checks": [],
                 "hypotheses": [], "invalid": None, "quiescent": True}
        path = fx.artifacts / "batches" / "rev-0.json"
        path.write_bytes(canonical(frame))
        driver = supervise._Driver(sup, fx.envelope, ad, GOAL, fx.registry, {}, str(fx.artifacts),
                                    lambda: 1000.0, lambda s: None, 1, None)
        before = len(sup._journal.records)
        with pytest.raises(supervise.SuperviseError) as exc:
            driver.evaluate_batch()
        assert exc.value.code == "batch_uncertain"
        assert len(sup._journal.records) == before and not sup.evaluations["eval-0"]["done"]
        assert sup.accepted == set() and sup.decide().kind == "reconcile"


def test_persistent_inconsistent_observation_still_cancels_waits_and_finishes_blocked(tmp_path):
    fx = _v2(setup(tmp_path))
    clock = type("Clock", (), {"t": 1000.0, "__call__": lambda self: self.t})()
    waits = []

    class Inconsistent(FakeAdapter):
        cancellations = []
        observations = 0

        def observe(self, identity):
            self.observations += 1
            if self.observations > 500:
                pytest.fail("persistent observation prevented STOPPING cleanup/finalization")
            if "attempt_id" in identity:
                return {"state": "TERMINAL", "outcome": "succeeded", "quiescent": True,
                        "usage": 1, "reported_usage": 2, "reason": None}
            return super().observe(identity)

        def cancel(self, identity, owner_guard, *, timeout=5):
            assert owner_guard()
            self.cancellations.append(identity.get("attempt_id") or identity["call_id"])
            return {"quiescent": False}

    def wait(seconds):
        waits.append(seconds)
        clock.t += seconds

    ad = Inconsistent(fx.envelope, fx.artifacts, two_worker_planner)
    result = supervise.run_goal(fx.envelope, goal_text=GOAL, registry_raw=fx.registry,
        capabilities=CAPS, state_dir=fx.state, artifact_root=fx.artifacts, adapter=ad,
        clock=clock, wait=wait)
    assert result["outcome"] == "BLOCKED" and result["reason"] == "unresolved_effects"
    assert ad.cancellations and sum(waits) >= 60 and clock.t <= 1062
    assert result["counters"]["attempts"] == 2 and result["counters"]["planner_calls"] == 1

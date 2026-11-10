"""Independent review regressions using real Core3 and snapshot transport."""
from pathlib import Path

import pytest

from supervisor_evidence_helpers import make_registry
from supervisor_helpers import digest
from supervisor_v2_helpers import envelope_dict_v2
from test_supervisor_continuation_v3 import (
    codec, core, driver, first_slice, fx, launch, opened, plan, supervise,
)


def test_data_selection_reopens_skipped_task_before_protected_evaluation(fx):
    with opened(fx) as sup:
        d = driver(fx, sup)
        plan(sup)
        launch(sup)
        fx.adapter.changes["a"] = first_slice
        d.reconcile()
        d.evaluate_batch()
        assert sup.accepted == {"C1"}

        # An equal-DATA continuation supplies the diagnostic replan admission.
        launch(sup, "noop")
        d.reconcile()
        d.evaluate_batch()
        assert sup.decide().reason == "diagnostic"
        plan(sup, 1, [("A", [], ["C1"]), ("B", [], ["C2"])])
        assert sup._launchable() == ["B"]
        launch(sup, "b", "B")

        def change_b(tree):
            (tree / "src" / "binary").write_bytes(b"needs repair")
            (tree / "src" / "done").write_text("done", encoding="utf-8")

        fx.adapter.changes["b"] = change_b
        d.reconcile()
        assert sup._barrier()
        previous_candidate = sup.candidate
        previous_evaluations = len(fx.evaluations)
        d.evaluate_batch()
        assert sup.stop is None
        assert sup.candidate != previous_candidate
        assert not sup.accepted
        assert not sup._barrier()
        assert len(fx.evaluations) == previous_evaluations

        d.step()
        reopened = next(effect for effect in fx.adapter.launches
                        if effect.data["task_id"] == "A")
        fx.adapter.changes[reopened.data["attempt_id"]] = lambda tree: (
            tree / "src" / "binary").write_bytes(b"\x00new\xfe")
        d.reconcile()
        assert not sup.accepted
        d.evaluate_batch()
        assert fx.evaluations[-1].candidate_sha256 == sup.candidate
        assert len(fx.evaluations) == previous_evaluations + 1
        assert sup.accepted == {"C1", "C2"}
        assert sup.stop is None


def test_prior_owner_pending_evaluation_blocks_after_durable_grace(fx, monkeypatch):
    registry = make_registry(fx.root / "validators", [
        ("chk-unit", ["C1", "C2"], "raise AssertionError('must not run')\n", [], None, 1),
    ])
    data = envelope_dict_v2(Path(fx.env.project_root), criteria=["C1", "C2"])
    data.pop("baseline_commit")
    data.update(schema_version=3, continuation=dict(codec.CONTINUATION), candidate={
        "kind": "artifact-manifest", "initial_sha256": fx.env.candidate.initial_sha256,
        "inputs": list(fx.env.candidate.inputs), "bounds": dict(fx.env.candidate.bounds),
    }, validator_digest=supervise._sha(registry), goal_digest=digest("goal"))
    data["acceptance_digest"] = supervise.acceptance_binding(
        data["criteria"], data["check_ids"], data["hypothesis_ids"], data["validator_digest"])
    fx.env = codec.load_envelope3(data, Path(fx.env.project_root))
    fx.adapter.env = fx.env
    assert fx.env.deadline is None and fx.env.max_usage is None
    now = [100.0]
    clock = lambda: now[0]
    state_dir = fx.root / "state"
    with core.Supervisor(state_dir, fx.env, clock, artifact_dir=fx.art) as sup:
        d = driver(fx, sup)
        d.clock = clock
        plan(sup)
        launch(sup)
        d.reconcile()
        mkdir = Path.mkdir

        def crash_before_directory(path, *args, **kwargs):
            if path.parent.name == "evaluations3":
                raise OSError("crash before validator directory")
            return mkdir(path, *args, **kwargs)

        with monkeypatch.context() as patch:
            patch.setattr(Path, "mkdir", crash_before_directory)
            with pytest.raises(OSError, match="before validator directory"):
                d.evaluate_batch()
        evaluation_id, pending = next(iter(sup.evaluations.items()))
        old_epoch = sup._journal.epoch
        assert not pending["done"]
        assert not (fx.art / "evaluations3" / evaluation_id).exists()
        d.evaluate_batch()
        assert sup.stop is None  # Same-owner unknown work must not be replayed.
        assert not fx.evaluations
        admissions = sup.counters()

    monkeypatch.setattr(fx.adapter, "preflight", lambda: None, raising=False)
    monkeypatch.setattr(fx.adapter, "close", lambda: None, raising=False)
    waits = []

    def advance(seconds):
        waits.append(seconds)
        assert len(waits) <= 61, "prior-owner evaluation never reached bounded stop"
        now[0] += seconds

    out = supervise.run_goal(
        fx.env, goal_text="goal", registry_raw=registry, capabilities=fx.env.capabilities,
        state_dir=state_dir, artifact_root=fx.art, adapter=fx.adapter,
        clock=clock, wait=advance, reserve=1)
    assert (out["outcome"], out["reason"], out["unresolved"]) == (
        "BLOCKED", "unresolved_effects", 1)
    assert now[0] == 160.0 and len(waits) == 60
    assert out["counters"] == admissions
    assert not fx.evaluations
    assert not fx.adapter.launches

    with core.Supervisor(state_dir, fx.env, clock, artifact_dir=fx.art) as sup:
        assert sup._journal.epoch != old_epoch
        assert sup.decide().outcome == "BLOCKED"
        assert sup.stop == {"reason": "invalid_evidence", "start": 100.0, "deadline": 160.0}
        assert sup.evaluations[evaluation_id] == pending
        assert not sup.evaluations[evaluation_id]["done"]
        assert sup.counters() == admissions
        assert sum(not e["done"] for e in sup.evaluations.values()) == 1
        assert not sup._occupied() and all(c["done"] for c in sup.calls.values())

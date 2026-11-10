"""Source-only continuation checks: real Core3 and snapshot transport; synthetic workers."""
import dataclasses
from pathlib import Path
from types import SimpleNamespace

import pytest

from supervisor_helpers import canonical, digest, load
from supervisor_v2_helpers import envelope_dict_v2
from test_supervisor_state_v3 import launch, plan

candidate = load("candidate")  # Fixture correction: the engine module is candidate.py.
codec = load("supervisor_contract_v3")
core = load("supervisor_state")
supervise = load("supervise")
status = load("task_status")
continuation = load("supervisor_continuation_v3")


@dataclasses.dataclass(frozen=True)
class Collected:
    attempt_id: str
    run_id: str
    seed_candidate: str
    seed_receipt: str
    snapshot: object
    receipt_ref: str
    completion_sha256: str
    handoff_sha256: str
    findings: str = "unverified"
    worker_status: str = "unverified"
    stop_reason: str = "artifact_cap"


class Transport:
    def __init__(self, env, art, initial):
        self.env, self.art, self.initial = env, art, initial
        self.bounds = candidate.SnapshotBounds(**dict(env.candidate.bounds))
        self.observations, self.results, self.changes = {}, {}, {}
        self.launches = []
        self.bad_context = False

    def set_snapshot_record_resolver(self, callback):
        self.resolve = callback

    def initial_snapshot(self):
        return self.initial

    def observe(self, identity):
        return self.observations.get(identity["attempt_id"], {
            "state": "TERMINAL", "outcome": "failed", "quiescent": True, "usage": None})

    def read_worker(self, identity, *, owner_guard):
        assert owner_guard()
        aid = identity["attempt_id"]
        if aid not in self.results:
            record = self.resolve(identity["seed_receipt"])
            seed_record = self.resolve(record["seed"]) if record["seed"] else None
            seed = candidate.open_snapshot(
                self.art / record["snapshot_ref"], bounds=self.bounds,
                manifest_sha256=record["candidate"],
                receipt_sha256=record["receipt_digest"], scope_sha256=record["scope_digest"],
                seed_sha256=None if seed_record is None else seed_record["candidate"],
                context_sha256=None if record["kind"] == "initial" else codec.snapshot_context3(
                    self.env.digest, seed_receipt=record["seed"],
                    source_attempts=record["source_attempts"], kind=record["kind"]))
            output = self.art.parent / ("output-" + aid)
            candidate.materialize(seed, output, bounds=self.bounds, owner_guard=owner_guard)
            self.changes.get(aid, lambda tree: None)(output)
            snapshot = candidate.capture(
                output, self.art / "snapshots" / ("worker-" + aid), bounds=self.bounds,
                inputs=self.env.candidate.inputs, owner_guard=owner_guard,
                seed_sha256=identity["seed_candidate"],
                context_sha256=codec.snapshot_context3(
                    self.env.digest, seed_receipt=identity["seed_receipt"],
                    source_attempts=[aid], kind="worker"))
            self.results[aid] = Collected(
                aid, identity["run_id"], identity["seed_candidate"], identity["seed_receipt"],
                snapshot, f"snapshots/worker-{aid}", digest("completion-" + aid),
                digest("handoff-" + aid))
        result = self.results[aid]
        if self.bad_context:
            result = dataclasses.replace(
                result, snapshot=dataclasses.replace(result.snapshot, context_sha256=digest("wrong")))
        return result

    def objective(self, identity, task):
        return "Produce the next bounded slice."

    def launch(self, effect, brief, *, on_process, owner_guard):
        assert owner_guard()
        self.launches.append(effect)
        on_process(effect.data["attempt_id"], effect.data["launch_id"],
                   100 + len(self.launches), "synthetic-birth")


@pytest.fixture
def fx(tmp_path, monkeypatch):
    project, art = tmp_path / "project", tmp_path / "artifacts"
    (project / "src").mkdir(parents=True)
    (project / "src" / "binary").write_bytes(b"\x00old\xff")
    (project / "src" / "deleted").write_bytes(b"remove me")
    bounds = dict(max_entries=100, max_file_bytes=10000, max_total_bytes=100000,
                  max_manifest_bytes=100000, max_path_bytes=512, max_seconds=10)
    (art / "snapshots").mkdir(parents=True)
    initial = candidate.capture(
        project, art / "snapshots" / "initial", bounds=candidate.SnapshotBounds(**bounds),
        inputs=("src",), owner_guard=lambda: True)
    data = envelope_dict_v2(project, criteria=["C1", "C2"])
    data.pop("baseline_commit")
    data.update(schema_version=3, deadline=None, max_usage=None,
                continuation=dict(codec.CONTINUATION), candidate={
                    "kind": "artifact-manifest", "initial_sha256": initial.manifest_sha256,
                    "inputs": ["src"], "bounds": bounds})
    env = codec.load_envelope3(data, project)
    adapter = Transport(env, art, initial)
    evaluations = []

    def protected_local_validator(snapshot, registry, envelope, output, manual, **kw):
        assert kw["owner_guard"]()
        assert not hasattr(snapshot, "baseline_commit")
        assert kw["identity_provider"](snapshot.tree) == snapshot.candidate_sha256
        evaluations.append(snapshot)
        tree = Path(snapshot.tree)
        satisfied = []
        if (tree / "src" / "binary").read_bytes() == b"\x00new\xfe" \
                and not (tree / "src" / "deleted").exists():
            satisfied.append("C1")
        if (tree / "src" / "done").exists():
            satisfied.append("C2")
        return SimpleNamespace(satisfied_criteria=tuple(satisfied), checks=(),
                               hypothesis_transitions=(), invalid_reason=None)

    monkeypatch.setattr(supervise.evidence, "evaluate_acceptance", protected_local_validator)
    return SimpleNamespace(root=tmp_path, env=env, art=art, adapter=adapter,
                           evaluations=evaluations)


def opened(fx):
    return core.Supervisor(fx.root / "state", fx.env, lambda: 100.0, artifact_dir=fx.art)


def driver(fx, sup):
    return supervise._Driver(
        sup, fx.env, fx.adapter, "synthetic goal",
        canonical({"checks": [], "manual_criteria": []}), {}, fx.art,
        lambda: 100.0, lambda seconds: None, 1, None,
        status_path=fx.root / "state" / "task-status.json")


def first_slice(tree):
    (tree / "src" / "binary").write_bytes(b"\x00new\xfe")
    (tree / "src" / "deleted").unlink()


def test_artifact_cap_yield_preserves_binary_deletion_and_current_seed(fx):
    with opened(fx) as sup:
        d = driver(fx, sup)
        plan(sup)
        first = launch(sup)
        fx.adapter.changes["a"] = first_slice
        d.reconcile()
        assert sup.attempts["a"]["outcome"] == "failed"
        assert sup.attempts["a"]["usage"] is None
        assert not sup.accepted
        d.evaluate_batch()
        assert sup.accepted == {"C1"}
        current = sup.seed_receipt3
        d._workers(sup.decide())
        second = fx.adapter.launches[-1]
        assert second.data["seed_receipt"] == current != first.data["seed_receipt"]
        assert second.data["predecessor"] == "a"
        fx.adapter.changes[second.data["attempt_id"]] = (
            lambda tree: (tree / "src" / "done").write_bytes(b"done"))
        d.reconcile()
        d.evaluate_batch()
        assert sup.decide().outcome == "SUCCEEDED"
        assert sup.counters()["planner_calls"] == 1
        assert sup.totals()["total_usage"] is None
        assert fx.env.limits["max_total_attempts"] is None


def test_same_revision_acceptance_launches_dependency_without_planner(fx):
    with opened(fx) as sup:
        d = driver(fx, sup)
        plan(sup, tasks=[("T", [], ["C1"]), ("U", ["T"], ["C2"])])
        launch(sup)
        fx.adapter.changes["a"] = first_slice
        d.reconcile()
        assert "U" not in sup._launchable3()
        d.evaluate_batch()
        assert "U" in sup._launchable3()
        d._workers(sup.decide())
        assert fx.adapter.launches[0].data["task_id"] == "U"
        assert sup.counters()["planner_calls"] == 1


def test_equal_data_siblings_have_distinct_receipts_and_cannot_cross_seed(fx):
    with opened(fx) as sup:
        d = driver(fx, sup)
        plan(sup, tasks=[("T", [], ["C1"]), ("U", [], ["C2"])])
        launch(sup)
        launch(sup, "b", "U")
        d.reconcile()
        receipts = [sup.yields3[aid]["receipt_digest"] for aid in ("a", "b")]
        assert receipts[0] != receipts[1]
        assert {sup.snapshots3[r]["candidate"] for r in receipts} == {sup.candidate}
        d.evaluate_batch()
        merged = sup.snapshots3[sup.seed_receipt3]
        assert merged["kind"] == "merge"
        assert merged["receipt_digest"] not in receipts
        with pytest.raises(ValueError):
            d.continuation._inputs(sup.seed_receipt3, ["a", "b"])
        assert sup.decide().reason == "diagnostic"
        assert not sup.accepted


def test_completed_evaluation_is_cached_by_full_context_not_replayed(fx):
    with opened(fx) as sup:
        d = driver(fx, sup)
        plan(sup)
        launch(sup)
        d.reconcile()
        d.evaluate_batch()
        count = len(sup._journal.records)
        d.evaluate_batch()
        assert len(fx.evaluations) == 1
        assert len(sup._journal.records) == count


def test_registered_merge_recovers_original_reference_without_merging_again(fx, monkeypatch):
    merge = candidate.merge
    calls = []

    def tracked(*args, **kw):
        calls.append(args[2])
        return merge(*args, **kw)

    monkeypatch.setattr(candidate, "merge", tracked)
    with opened(fx) as sup:
        d = driver(fx, sup)
        plan(sup)
        launch(sup)
        d.reconcile()
        with monkeypatch.context() as patch:
            patch.setattr(sup, "select_candidate",
                          lambda *a, **kw: (_ for _ in ()).throw(OSError("crash")))
            with pytest.raises(OSError):
                d.evaluate_batch()
        registered = next(r for r in sup.snapshots3.values() if r["kind"] == "merge")
    with opened(fx) as sup:
        d = driver(fx, sup)
        d.evaluate_batch()
        assert sup.seed_receipt3 == registered["receipt_digest"]
        assert sup.snapshots3[sup.seed_receipt3]["snapshot_ref"] == registered["snapshot_ref"]
        assert len(calls) == 1
        assert fx.adapter.launches == []


def test_pending_unknown_verifier_is_never_replayed(fx, monkeypatch):
    with opened(fx) as sup:
        d = driver(fx, sup)
        plan(sup)
        launch(sup)
        d.reconcile()
        monkeypatch.setattr(supervise.evidence, "evaluate_acceptance",
                            lambda *a, **kw: (_ for _ in ()).throw(OSError("crash")))
        with pytest.raises(OSError):
            d.evaluate_batch()
    with opened(fx) as sup:
        d = driver(fx, sup)
        d.evaluate_batch()
        assert any(not e["done"] for e in sup.evaluations.values())
        assert not sup.accepted
        assert fx.adapter.launches == []


@pytest.mark.parametrize("failure", ["active_child", "unknown", "context", "receipt"])
def test_collection_requires_quiescence_known_ownership_and_pins(fx, failure):
    with opened(fx) as sup:
        d = driver(fx, sup)
        plan(sup)
        effect = launch(sup)
        if failure in ("active_child", "unknown"):
            fx.adapter.observations["a"] = {
                "state": "UNKNOWN" if failure == "unknown" else "TERMINAL",
                "outcome": None if failure == "unknown" else "failed",
                "quiescent": False, "usage": None}
        elif failure == "context":
            fx.adapter.bad_context = True
        else:
            result = fx.adapter.read_worker(effect.data, owner_guard=sup.owner_current)
            # Snapshot has no directory attribute; use the capture destination.
            (fx.art / result.receipt_ref / "receipt.json").write_bytes(b"{}")
        d.reconcile()
        assert "a" not in sup.yields3
        assert not sup.accepted
        assert sup.attempts["a"]["lifecycle"] != "COLLECTED"


@pytest.mark.parametrize("failure", ["late_change", "owner_loss"])
def test_protected_evaluation_cannot_finish_after_binding_changes(fx, monkeypatch, failure):
    evaluate = supervise.evidence.evaluate_acceptance
    with opened(fx) as sup:
        d = driver(fx, sup)
        plan(sup)
        launch(sup)
        d.reconcile()

        def changes(snapshot, *args, **kw):
            facts = evaluate(snapshot, *args, **kw)
            if failure == "late_change":
                (Path(snapshot.tree) / "src" / "binary").write_bytes(b"changed late")
            else:
                monkeypatch.setattr(sup, "owner_current", lambda epoch=None: False)
            return facts

        monkeypatch.setattr(supervise.evidence, "evaluate_acceptance", changes)
        with pytest.raises(ValueError):
            d.evaluate_batch()
        assert not sup.accepted
        assert any(not e["done"] for e in sup.evaluations.values())


def test_status_publication_is_closed_atomic_metadata_and_failure_is_non_authoritative(fx, monkeypatch):
    with opened(fx) as sup:
        d = driver(fx, sup)
        path = fx.root / "state" / "task-status.json"
        d.continuation.publish()
        raw = path.read_bytes()
        data = status.validate_task_status(
            raw, expected_envelope_sha256=fx.env.digest,
            expected_project_root=fx.env.project_root)
        assert data["limits"]["max_total_attempts"] is None
        assert b"unverified" not in raw and b"remove me" not in raw
        with pytest.raises(status.TaskStatusError):
            status.publish_task_status(sup, fx.art / "task-status.json",
                                       owner_guard=sup.owner_current)
        with pytest.raises(status.TaskStatusError):
            status.publish_task_status(sup, path, owner_guard=lambda: False)
        accepted = set(sup.accepted)
        monkeypatch.setattr(status, "publish_task_status",
                            lambda *a, **kw: (_ for _ in ()).throw(OSError("unavailable")))
        d.continuation.publish()
        assert sup.accepted == accepted and path.read_bytes() == raw


def test_schema_selection_does_not_change_legacy_adapter_route(monkeypatch):
    calls = []
    legacy = lambda *a, **kw: calls.append(("legacy", a, kw))
    resilient = lambda *a, **kw: calls.append(("v3", a, kw))
    harness3 = load("supervisor_harness_v3")
    monkeypatch.setattr(supervise.harness, "HarnessAdapter", legacy)
    monkeypatch.setattr(harness3, "ResilientHarnessAdapter", resilient)
    for version in (1, 2, 3):
        supervise._new_adapter(SimpleNamespace(schema_version=version), "synthetic", {}, lambda: 0)
    assert [c[0] for c in calls] == ["legacy", "legacy", "v3"]

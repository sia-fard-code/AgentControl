"""Source-only schema-3 real contracts: actual protected validator and real Git worktree."""
import hashlib
from pathlib import Path
import sys

import pytest

from supervisor_helpers import load
from supervisor_evidence_helpers import make_registry
from supervisor_v2_helpers import envelope_dict_v2

ev = load("supervisor_evidence")
codec = load("supervisor_contract_v3")
candidate = load("candidate")
REAL_EVALUATE = ev.evaluate_acceptance  # captured before any fixture monkeypatches it

BOUNDS = dict(max_entries=100, max_file_bytes=10000, max_total_bytes=100000,
              max_manifest_bytes=100000, max_path_bytes=512, max_seconds=10)
SCRIPT = ("import pathlib, sys\n"
          "t = pathlib.Path(sys.argv[1]) / 'src'\n"
          "assert (t / 'binary').read_bytes() == b'\\x00new\\xfe'\n"
          "assert not (t / 'deleted').exists()\n")


def real_job_or_skip():
    if sys.platform != "win32":
        pytest.skip("real Windows Job containment needs win32; no OS containment claim")


def registry_env(tmp_path, project, initial_sha):
    registry = make_registry(tmp_path / "validators",
                             [("chk-current", ["C1"], SCRIPT, [], None, 30)])
    data = envelope_dict_v2(project, criteria=["C1"])
    data.pop("baseline_commit")
    data.update(schema_version=3, continuation=dict(codec.CONTINUATION),
                validator_digest=hashlib.sha256(registry).hexdigest(),
                check_ids=["chk-current"], hypothesis_ids=[], candidate={
                    "kind": "artifact-manifest", "initial_sha256": initial_sha,
                    "inputs": ["src"], "bounds": BOUNDS})
    return registry, codec.load_envelope3(data, project)


def protected_tree(tmp_path, name, mutate):
    """Write a src tree into <name>/tree and return (snapshot3, identity callback)."""
    directory = tmp_path / name
    tree = directory / "tree"
    (tree / "src").mkdir(parents=True)
    (tree / "src" / "binary").write_bytes(b"\x00old\xff")
    (tree / "src" / "deleted").write_bytes(b"remove me")
    mutate(tree)
    bounds = candidate.SnapshotBounds(**BOUNDS)
    calls = []

    def identity(root):
        assert Path(root) == tree
        calls.append(root)
        return candidate.capture(
            root, tmp_path / f"id-{name}-{len(calls)}", bounds=bounds, inputs=("src",),
            owner_guard=lambda: True, strict_layout=True).manifest_sha256

    pin = identity(tree)
    snap = ev.EvaluationSnapshot3(candidate_dir=str(directory), tree=str(tree),
                                  candidate_sha256=pin, artifact_ref="snapshots/" + name)
    return snap, identity, calls


def current_slice(tree):
    (tree / "src" / "binary").write_bytes(b"\x00new\xfe")
    (tree / "src" / "deleted").unlink()


def test_real_validator_accepts_current_binary_and_deletion_only(tmp_path):
    real_job_or_skip()
    project = tmp_path / "project"
    project.mkdir()
    before = sorted(p.name for p in project.iterdir())
    snap, identity, calls = protected_tree(tmp_path, "current", current_slice)
    registry, env = registry_env(tmp_path, project, snap.candidate_sha256)
    result = REAL_EVALUATE(snap, registry, env, tmp_path / "out-current",
                           identity_provider=identity, owner_guard=lambda: True)
    assert result.invalid_reason is None
    assert tuple(result.satisfied_criteria) == ("C1",)
    assert len(calls) >= 2  # pin plus at least one re-identification by the evaluator

    stale, stale_identity, _ = protected_tree(tmp_path, "stale", lambda tree: None)
    result = REAL_EVALUATE(stale, registry, env, tmp_path / "out-stale",
                           identity_provider=stale_identity, owner_guard=lambda: True)
    assert "C1" not in tuple(result.satisfied_criteria)
    assert sorted(p.name for p in project.iterdir()) == before


def test_real_validator_accepts_path_typed_descriptor_like_continuation(tmp_path):
    """Continuation3 builds EvaluationSnapshot3 with pathlib.Path tree/candidate_dir."""
    import dataclasses
    real_job_or_skip()
    project = tmp_path / "project"
    project.mkdir()
    snap, identity, _ = protected_tree(tmp_path, "pathy", current_slice)
    snap = dataclasses.replace(snap, tree=Path(snap.tree), candidate_dir=Path(snap.candidate_dir))
    registry, env = registry_env(tmp_path, project, snap.candidate_sha256)
    result = REAL_EVALUATE(snap, registry, env, tmp_path / "out-pathy",
                           identity_provider=identity, owner_guard=lambda: True)
    assert (result.invalid_reason, tuple(result.satisfied_criteria)) == (None, ("C1",)), \
        result.checks


def test_core_driver_runs_actual_protected_validator_on_current_data(tmp_path):
    real_job_or_skip()
    from supervisor_helpers import canonical
    from test_supervisor_continuation_v3 import Transport, first_slice
    from test_supervisor_state_v3 import launch, plan
    core, supervise = load("supervisor_state"), load("supervise")
    assert supervise.evidence.evaluate_acceptance is REAL_EVALUATE  # no fake evaluator
    project, art = tmp_path / "project", tmp_path / "artifacts"
    (project / "src").mkdir(parents=True)
    (project / "src" / "binary").write_bytes(b"\x00old\xff")
    (project / "src" / "deleted").write_bytes(b"remove me")
    parent_bytes = {p.name: p.read_bytes() for p in (project / "src").iterdir()}
    (art / "snapshots").mkdir(parents=True)
    initial = candidate.capture(project, art / "snapshots" / "initial",
                                bounds=candidate.SnapshotBounds(**BOUNDS), inputs=("src",),
                                owner_guard=lambda: True)
    registry, env = registry_env(tmp_path, project, initial.manifest_sha256)
    adapter = Transport(env, art, initial)
    with core.Supervisor(tmp_path / "state", env, lambda: 100.0, artifact_dir=art) as sup:
        d = supervise._Driver(sup, env, adapter, "synthetic goal", registry, {}, art,
                              lambda: 100.0, lambda seconds: None, 1, None,
                              status_path=tmp_path / "state" / "task-status.json")
        plan(sup, tasks=[("T", [], ["C1"])])
        first = launch(sup)
        adapter.changes["a"] = first_slice
        d.reconcile()
        assert not sup.accepted
        epoch = sup._journal.epoch
        d.evaluate_batch()
        assert sup.accepted == {"C1"}, {
            p.name: p.read_bytes()[:300] for p in (art / "evaluations3").rglob("*")
            if p.is_file() and "tree" not in p.parts}
        done = [e for e in sup.evaluations.values() if e["done"]]
        assert len(done) == 1 and done[0]["candidate"] == sup.candidate
        assert sup.seed_receipt3 != first.data["seed_receipt"]
        eid = next(k for k, e in sup.evaluations.items() if e["done"])
        key = {"revision": sup.plan["revision"], "candidate": sup.candidate,
               "seed_receipt": sup.seed_receipt3, "epoch": epoch}
        assert eid == "eval-" + hashlib.sha256(canonical(key)).hexdigest()
        tree = art / "evaluations3" / eid / "tree" / "src"
        assert (tree / "binary").read_bytes() == b"\x00new\xfe"
        assert not (tree / "deleted").exists()
        assert (art / "evaluations3" / eid / "criteria.json").read_bytes() == \
            canonical({"criteria": ["C1"]})
        assert sup.decide().outcome == "SUCCEEDED"
    assert {p.name: p.read_bytes() for p in (project / "src").iterdir()} == parent_bytes

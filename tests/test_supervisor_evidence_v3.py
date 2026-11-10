from dataclasses import FrozenInstanceError
import hashlib
from pathlib import Path
import subprocess
from types import SimpleNamespace

import pytest

from supervisor_helpers import load
from supervisor_evidence_helpers import make_registry
from supervisor_v2_helpers import envelope_dict_v2

ev = load("supervisor_evidence")
codec = load("supervisor_contract_v3")
candidate = load("candidate")


@pytest.fixture
def protected(tmp_path, monkeypatch):
    project = tmp_path / "project"
    project.mkdir()
    directory = tmp_path / "protected"
    tree = directory / "tree"
    (tree / "src").mkdir(parents=True)
    (tree / "src" / "value.txt").write_bytes(b"synthetic\n")
    identities = tmp_path / "identities"
    identities.mkdir()
    bounds_data = dict(max_entries=100, max_file_bytes=1000, max_total_bytes=10000,
                       max_manifest_bytes=10000, max_path_bytes=512, max_seconds=10)
    bounds = candidate.SnapshotBounds(**bounds_data)
    state = dict(owned=True, quiet=True, launches=0, calls=0, after_wait=lambda: None)
    guard = lambda: state["owned"]

    def identity(root):
        assert Path(root) == tree
        assert guard() is True
        state["calls"] += 1
        return candidate.capture(
            root, identities / str(state["calls"]), bounds=bounds, inputs=("src",),
            owner_guard=guard, strict_layout=True).manifest_sha256

    pin = identity(tree)
    script = ("import pathlib, sys\n"
              "assert (pathlib.Path(sys.argv[1]) / 'src/value.txt').read_bytes()"
              " == b'synthetic\\n'\n")
    registry = make_registry(
        tmp_path / "validators", [("chk-data", ["C1"], script, [], None, 30)])
    data = envelope_dict_v2(project, criteria=["C1"])
    data.pop("baseline_commit")
    data.update(schema_version=3, continuation=dict(codec.CONTINUATION),
                validator_digest=hashlib.sha256(registry).hexdigest(),
                check_ids=["chk-data"], hypothesis_ids=[], candidate={
                    "kind": "artifact-manifest", "initial_sha256": pin,
                    "inputs": ["src"], "bounds": bounds_data,
                })
    envelope = codec.load_envelope3(data, project)
    snapshot = ev.EvaluationSnapshot3(
        candidate_dir=str(directory), tree=str(tree), candidate_sha256=pin,
        artifact_ref="snapshots/protected")

    class Child:
        pid, birth = 123, 456

        def __init__(self, argv, options):
            self.argv, self.options, self.rc = argv, options, None

        def resume(self):
            state["launches"] += 1
            with open(self.options["stdout_path"], "wb") as out, open(
                    self.options["stderr_path"], "wb") as err:
                self.rc = subprocess.run(
                    self.argv, cwd=self.options["cwd"], env=self.options["env"],
                    stdin=subprocess.DEVNULL, stdout=out, stderr=err,
                    timeout=10, check=False).returncode

        def wait(self, timeout):
            state["after_wait"]()
            return self.rc

        def close(self):
            pass

    class Job:
        @classmethod
        def create(cls, name):
            return cls()

        def create_suspended(self, argv, **options):
            return Child(argv, options)

        def wait_empty(self, timeout):
            return state["quiet"]

        def is_empty(self):
            return state["quiet"]

        def terminate(self, owner_guard, timeout):
            assert owner_guard()
            return {"outcome": "unknown"}

        def close(self):
            pass

    monkeypatch.setattr(ev, "WindowsJob", Job)
    return SimpleNamespace(snapshot=snapshot, registry=registry, envelope=envelope,
                           identity=identity, guard=guard, state=state, tree=tree,
                           out=tmp_path / "out")


def _evaluate(p, **overrides):
    options = dict(identity_provider=p.identity, owner_guard=p.guard)
    options.update(overrides)
    return ev.evaluate_acceptance(
        p.snapshot, p.registry, p.envelope, p.out, **options)


def test_protected_evaluation_uses_data_not_flat_identity(protected, monkeypatch):
    p = protected
    assert p.snapshot.candidate_sha256 != ev.candidate_identity(p.tree)
    assert not hasattr(p.snapshot, "baseline_commit")
    with pytest.raises(FrozenInstanceError):
        p.snapshot.tree = "other"
    monkeypatch.setattr(
        ev, "candidate_identity",
        lambda *a, **k: pytest.fail("schema 3 used the legacy flat identity"))
    facts = _evaluate(p)
    assert facts.accepted and facts.satisfied_criteria == ("C1",)
    assert facts.candidate_sha256 == p.snapshot.candidate_sha256
    assert facts.checks == ({"check_id": "chk-data", "status": "pass",
                             "failure_code": None},)
    assert p.state["calls"] >= 4 and p.state["launches"] == 1


@pytest.mark.parametrize("provider", [None, "worker-supplied"])
def test_schema3_requires_explicit_callable(protected, provider):
    with pytest.raises(ev.EvidenceError) as err:
        _evaluate(protected, identity_provider=provider)
    assert err.value.code == "identity_provider_required"
    assert protected.state["launches"] == 0 and not protected.out.exists()


@pytest.mark.parametrize("version", [1, 2])
def test_legacy_callback_is_refused_before_use(version, tmp_path):
    with pytest.raises(ev.EvidenceError) as err:
        ev.evaluate_acceptance(
            None, b"", SimpleNamespace(schema_version=version), tmp_path / "out",
            identity_provider=lambda root: pytest.fail("legacy callback used"))
    assert err.value.code == "identity_provider_forbidden"
    assert not (tmp_path / "out").exists()


@pytest.mark.parametrize("when", ["before", "identity", "command"])
def test_owner_loss_cannot_accept(protected, when):
    p = protected
    provider = p.identity
    if when == "before":
        p.state["owned"] = False
    elif when == "identity":
        def provider(root):
            digest = p.identity(root)
            p.state["owned"] = False
            return digest
    else:
        p.state["after_wait"] = lambda: p.state.update(owned=False)
    facts = _evaluate(p, identity_provider=provider)
    assert not facts.accepted and facts.invalid_reason == "owner_lost"
    if when != "command":
        assert p.state["launches"] == 0


@pytest.mark.parametrize("when", ["before", "command", "late"])
def test_data_change_cannot_accept(protected, monkeypatch, when):
    p = protected

    def mutate():
        (p.tree / "src" / "late.txt").write_bytes(b"late\n")

    if when == "before":
        mutate()
    elif when == "command":
        p.state["after_wait"] = mutate
    else:
        original = ev._script_digests
        calls = []

        def digests(*args):
            result = original(*args)
            calls.append(None)
            if len(calls) == 2:
                mutate()
            return result

        monkeypatch.setattr(ev, "_script_digests", digests)
    facts = _evaluate(p)
    assert not facts.accepted and facts.invalid_reason == "candidate_mutated"
    assert facts.satisfied_criteria == ()
    if when == "before":
        assert p.state["launches"] == 0


def test_unconfirmed_descendants_cannot_accept(protected):
    protected.state["quiet"] = False
    facts = _evaluate(protected)
    assert not facts.accepted and facts.invalid_reason == "survivors"

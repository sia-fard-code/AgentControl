"""Protocol-2 parent transport, real codecs/snapshots, fake contained processes."""
import dataclasses
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
from types import SimpleNamespace
import uuid

import pytest

ENGINE = Path(__file__).resolve().parents[1] / "engine"
sys.path.insert(0, str(ENGINE))

import candidate
import finish_gate as fg
import recovery_handoff as rh
import recovery_runtime as rr
import supervisor_contract as contract
import supervisor_contract_v3 as codec
import supervisor_harness_v3 as adapter_module
import supervisor_manifest_v2 as manifest
import supervisor_process as process
import supervisor_state as state
from supervisor_harness import HarnessError, PLANNER_MODEL, WORKER_MODEL, command_id
from supervisor_v2_helpers import envelope_dict_v2


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


class FakeChild:
    pid = 431
    birth = 133801234567890123

    def __init__(self, job):
        self.job = job

    def resume(self):
        self.job.events.append("resume")
        self.job.resumed = True

    def close(self):
        self.job.events.append("child-close")


class FakeJob:
    supported = True
    jobs = {}

    def __init__(self, name):
        self.name = name
        self._created_here = True
        self.events = []
        self.count = 1
        self.resumed = False
        self.leader = True

    @classmethod
    def atomic_supported(cls):
        return cls.supported

    @classmethod
    def create(cls, name):
        if name in cls.jobs:
            raise process.ProcessError("job_exists")
        job = cls(name)
        cls.jobs[name] = job
        return job

    @classmethod
    def open_existing(cls, name):
        if name not in cls.jobs:
            raise process.ProcessError("job_missing")
        return cls.jobs[name]

    def create_suspended_atomic(self, argv, **kw):
        self.argv = argv
        self.cwd = kw["cwd"]
        self.events.append("suspended")
        Path(kw["stdout_path"]).write_bytes(b"")
        Path(kw["stderr_path"]).write_bytes(b"")
        return FakeChild(self)

    def contains(self, pid, birth):
        return self.leader and pid == FakeChild.pid and birth == FakeChild.birth

    def active_count(self):
        return self.count

    def is_empty(self):
        return self.active_count() == 0

    def terminate(self, guard, *, timeout):
        assert guard() is True
        self.events.append("terminate")
        self.count = 0
        return {"outcome": "exited", "active_processes": 0}

    def close(self):
        self.events.append("job-close")


@pytest.fixture
def world(tmp_path, monkeypatch):
    FakeJob.jobs = {}
    FakeJob.supported = True
    root = tmp_path / "policy"
    (root / ".claude").mkdir(parents=True)
    policy = contract.canonical({"allowed_cmds": [["python", "-m", "pytest"]]})
    (root / ".claude" / "agent-policy.json").write_bytes(policy)
    artifacts = tmp_path / "artifacts"
    (artifacts / "snapshots").mkdir(parents=True)
    data = tmp_path / "data"
    (data / "src").mkdir(parents=True)
    (data / "src" / "old.bin").write_bytes(b"\x00initial\xff")
    (data / "a.txt").write_bytes(b"file scope")
    bounds = candidate.SnapshotBounds(max_entries=100, max_file_bytes=10000,
                                     max_total_bytes=100000, max_manifest_bytes=65536,
                                     max_path_bytes=512, max_seconds=10)
    owner = lambda: True
    initial = candidate.capture(data, artifacts / "snapshots" / "initial",
                                bounds=bounds, inputs=(".",), owner_guard=owner,
                                strict_layout=True)
    body = envelope_dict_v2(root)
    body.pop("baseline_commit")
    body.update(schema_version=3, models=[PLANNER_MODEL, WORKER_MODEL],
                policy_digest=sha(policy), cancel_authority=True,
                capabilities={"write": ["src/", "a.txt"],
                              "commands": [command_id(["python", "-m", "pytest"])],
                              "mcp": []},
                continuation=dict(codec.CONTINUATION),
                candidate={"kind": "artifact-manifest",
                           "initial_sha256": initial.manifest_sha256, "inputs": ["."],
                           "bounds": dataclasses.asdict(bounds)})
    env = codec.load_envelope3(body, root)
    installed = tmp_path / "installed"
    installed.mkdir()
    for name in adapter_module.REQUIRED_BUNDLE:
        shutil.copyfile(ENGINE / name, installed / name)
    engine = installed / "delegate.py"
    engine.write_bytes(b"# Never executed by these tests.\n")
    ad = adapter_module.ResilientHarnessAdapter(
        env, artifacts, capabilities={"write_paths": ["src/", "a.txt"],
                                     "commands": [["python", "-m", "pytest"]], "mcp": "none"},
        engine_path=engine, clock=lambda: 100.0, job_factory=FakeJob)

    def probe(flag):
        assert flag == "--supervisor-capabilities-v2"
        caps = dict(manifest.capabilities2())
        caps["features"] = dict(caps["features"])
        caps["roles"] = dict(caps["roles"])
        return contract.canonical(caps)

    monkeypatch.setattr(ad, "_probe", probe)
    monkeypatch.setattr(adapter_module, "_terminate_suspended",
                        lambda child: child.job.events.append("abort"))
    ad.preflight()
    with state.Supervisor(tmp_path / "state", env, lambda: 100.0,
                          artifact_dir=artifacts) as sup:
        sup.register_snapshot3(initial.manifest_sha256, "snapshots/initial",
                               initial.receipt_sha256, initial.scope_sha256)
        sup.admit_planner("p0", "planner0", 2)
        sup.record_plan("p0", contract.canonical({
            "schema_version": 1, "plan_revision": 0,
            "tasks": [{"task_id": "T", "depends_on": [], "objective_digest": sha(b"T"),
                       "criteria": [env.criteria[0]], "hypothesis_ids": []}]}), None)
        yield SimpleNamespace(ad=ad, sup=sup, initial=initial, bounds=bounds,
                              engine=engine, data=data, artifacts=artifacts)
    ad.close()


def admit(w, name="a"):
    return w.sup.admit_worker("T", name, str(uuid.uuid4()), "run-" + name,
                              "tree-" + name, 3)


def launch(w, effect=None, callback=None, guard=None):
    effect = effect or admit(w)
    w.ad.launch(effect, "task\r\nbrief", on_process=callback or w.sup.record_process_identity,
                owner_guard=guard or w.sup.owner_current)
    return w.sup.effect_identity(effect.data["attempt_id"])


def finish(w, identity, *, unavailable=False, inflight=False, uncertain=False,
           stop="max_iters"):
    paths = w.ad._paths(identity)
    m = manifest.load_manifest2(str(paths["manifest"]), sha(paths["manifest"].read_bytes()))
    boundary = manifest.Boundary2(m.write_paths, m.commands,
                                  candidate_root=m.candidate_root, manifest=m)
    from supervisor_launch_v2 import identity_provider2
    runtime = rr.RecoveryRuntime.prepare(
        str(paths["config"]), run_id=m.run_id, task_text="task\nbrief",
        policy_root=m.root, candidate_root=m.candidate_root, launch_id=m.launch_id,
        artifact_dir=paths["engine_artifacts"], plan=fg.VerificationPlan(()),
        boundary=boundary, identity_provider=identity_provider2(
            m, owner_guard=w.sup.owner_current))
    snap = fg.EvidenceSnapshot("disk", m.candidate_root, 0, uncertain, (), (), unavailable)
    evidence = SimpleNamespace(plan=fg.VerificationPlan(()), snapshot=lambda: snap)
    if unavailable or inflight or uncertain:
        pending = rh.begin(runtime.owned_dir, binding=runtime.binding, spec=runtime.spec,
                           evidence=evidence, before_sha256=None, schema_version=2)
        if not inflight:
            runtime.handoff_sha256 = rh.settle(runtime.owned_dir, pending,
                                               evidence=evidence, after_sha256=None)
    else:
        runtime.checkpoint(evidence)
    completion = dict(runtime.metadata(), verification_status="unverified")
    (paths["engine_artifacts"] / "completion.json").write_bytes(contract.canonical(completion))
    root = str(paths["candidate"])
    ready = {"schema": 1, "launch_id": m.launch_id, "engine_pid": FakeChild.pid,
             "state": "ready", "run_id": m.run_id, "root": root,
             "transcript": str(paths["transcript"]), "index_paths": [str(paths["index"])]}
    paths["receipt"].write_bytes(contract.canonical(ready) + b"\n")
    rows = [{"schema": 2, "run_id": m.run_id, "event": "start",
             "root": root, "engine_pid": FakeChild.pid},
            {"schema": 2, "run_id": m.run_id, "event": "end",
             "status": "incomplete", "stop_reason": stop, "sent": None, "recv": None}]
    paths["index"].write_bytes(b"\n".join(contract.canonical(row) for row in rows) + b"\n")
    FakeJob.jobs[m.launch_id if m.launch_id in FakeJob.jobs
                 else identity["job_name"]].count = 0
    return paths


def test_artifact_preflight_never_queries_git_and_initial_is_real(world, monkeypatch):
    monkeypatch.setattr(adapter_module.subprocess, "run",
                        lambda *a, **k: pytest.fail("artifact preflight queried Git"))
    assert world.ad.preflight()["version"] == 2
    assert world.ad.initial_snapshot() == world.initial


@pytest.mark.parametrize("fault", ["atomic", "protocol", "policy"])
def test_preflight_refuses_before_launch(world, monkeypatch, fault):
    if fault == "atomic":
        FakeJob.supported = False
    elif fault == "protocol":
        monkeypatch.setattr(world.ad, "_probe", lambda flag: b'{"version":1}')
    else:
        (Path(world.ad.envelope.project_root) / ".claude" / "agent-policy.json").write_bytes(b"{}")
    with pytest.raises(HarnessError):
        world.ad.preflight()
    assert not FakeJob.jobs


def test_callback_is_durable_before_resume_and_current_seed_is_materialized(world):
    first = admit(world)
    world.sup.observe_attempt("a", "COLLECTED", "failed", True, None)
    (world.data / "src" / "old.bin").unlink()
    (world.data / "src" / "new.bin").write_bytes(b"\x00CURRENT\xfe")
    current = candidate.capture(
        world.data, world.artifacts / "snapshots" / "current", bounds=world.bounds,
        inputs=(".",), owner_guard=world.sup.owner_current, strict_layout=True,
        seed_sha256=world.initial.manifest_sha256,
        context_sha256=codec.snapshot_context3(
            world.sup.envelope.digest, seed_receipt=world.initial.receipt_sha256,
            source_attempts=["a"], kind="worker"))
    receipt = world.sup.register_snapshot3(
        current.manifest_sha256, "snapshots/current", current.receipt_sha256,
        current.scope_sha256, seed=world.initial.receipt_sha256,
        source_attempts=("a",), kind="worker")
    # A capture alone does not authorize continuation; register the synthetic trusted yield.
    world.sup.record_yield3("a", receipt["receipt_digest"], sha(b"synthetic trusted handoff"))
    world.sup.select_candidate(current.manifest_sha256, receipt_digest=receipt["receipt_digest"])
    world.ad.set_snapshot_record_resolver(world.sup.snapshots3.get)
    second = admit(world, "b")

    def pin(*args):
        world.sup.record_process_identity(*args)
        job = FakeJob.jobs[second.data["job_name"]]
        assert not job.resumed
        assert world.sup.effect_identity("b")["process"]["birth"] == "winft:" + str(FakeChild.birth)
        job.events.append("journal")

    identity = launch(world, second, pin)
    paths = world.ad._paths(identity)
    assert not (paths["candidate"] / "src" / "old.bin").exists()
    assert (paths["candidate"] / "src" / "new.bin").read_bytes() == b"\x00CURRENT\xfe"
    job = FakeJob.jobs[identity["job_name"]]
    assert job.events == ["suspended", "journal", "resume"]
    assert "--write" in job.argv and "--worktree" not in job.argv and "--patch" not in job.argv
    assert job.argv[job.argv.index("--profile") + 1] == "resilient-v1"
    assert second.data["seed_candidate"] != first.data["seed_candidate"]
    assert json.loads(paths["config"].read_bytes())["binding"]["seed_sha256"] == current.manifest_sha256


def test_owner_loss_after_callback_never_resumes_or_cancels_an_existing_group(world):
    live = [True]
    effect = admit(world)

    def pin(*args):
        world.sup.record_process_identity(*args)
        live[0] = False

    with pytest.raises(HarnessError, match="owner_lost"):
        launch(world, effect, pin, lambda: live[0])
    assert FakeJob.jobs[effect.data["job_name"]].events == ["suspended", "abort"]


@pytest.mark.parametrize("fault", ["mutation_outcome_unknown", "recovery_state", "inflight",
                                 "uncertain", "pid", "config", "manifest", "contract"])
def test_unknown_or_unpinned_results_are_never_collected(world, fault):
    identity = launch(world)
    paths = finish(world, identity, inflight=fault == "inflight", uncertain=fault == "uncertain",
                   stop=fault if fault in ("mutation_outcome_unknown", "recovery_state")
                   else "max_iters")
    if fault == "pid":
        row = json.loads(paths["receipt"].read_bytes())
        row["engine_pid"] += 1
        paths["receipt"].write_bytes(contract.canonical(row) + b"\n")
    elif fault in ("config", "manifest"):
        paths[fault].write_bytes(paths[fault].read_bytes() + b" ")
    elif fault == "contract":
        path = paths["engine_artifacts"] / "completion.json"
        body = json.loads(path.read_bytes())
        body["recovery_contract_sha256"] = "0" * 64
        path.write_bytes(contract.canonical(body))
    with pytest.raises(HarnessError):
        world.ad.read_worker(identity)
    assert not paths["capture"].exists()


def test_unverified_null_after_is_a_new_parent_observation_and_idempotent(world):
    identity = launch(world)
    root = world.ad._paths(identity)["candidate"]
    (root / "src" / "old.bin").unlink()
    (root / "src" / "result.bin").write_bytes(b"\x00parent observation\xff")
    finish(world, identity, unavailable=True)
    result = world.ad.read_worker(identity)
    assert result.worker_status == "unverified"
    assert result.snapshot.seed_sha256 == world.initial.manifest_sha256
    assert result.snapshot.manifest_sha256 != world.initial.manifest_sha256
    assert not hasattr(result, "accepted") and not hasattr(result, "verified")
    assert world.ad.read_worker(identity) == result
    assert not world.sup.accepted
    (root / "src" / "result.bin").write_bytes(b"changed after collection")
    with pytest.raises(HarnessError):
        world.ad.read_worker(identity)


@pytest.mark.parametrize("path,allowed", [("a.txt", True), ("src/new.bin", True),
                                        ("a.txt/child", False), ("outside.bin", False),
                                        (".git", False)])
def test_exact_file_and_directory_scope(world, path, allowed):
    identity = launch(world)
    root = world.ad._paths(identity)["candidate"]
    target = root / path
    if path == "a.txt/child":
        (root / "a.txt").unlink()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"new")
    if path == ".git":
        with pytest.raises(Exception):
            finish(world, identity)
        return
    finish(world, identity)
    if allowed:
        assert world.ad.read_worker(identity).snapshot
    else:
        with pytest.raises(HarnessError):
            world.ad.read_worker(identity)


def test_retained_group_not_leader_controls_quiescence_and_usage_stays_unknown(world):
    identity = launch(world)
    finish(world, identity)
    job = FakeJob.jobs[identity["job_name"]]
    job.leader = False
    job.count = 1
    assert world.ad.observe(identity)["state"] == "RUNNING"
    job.count = 0
    observation = world.ad.observe(identity)
    assert observation["state"] == "TERMINAL" and observation["quiescent"]
    assert observation["usage"] is None and observation["reported_usage"] is None
    wrong = dict(identity, process=dict(identity["process"], birth="winft:1"))
    assert world.ad.observe(wrong)["state"] == "UNKNOWN"
    with pytest.raises(HarnessError):
        world.ad.cancel(wrong, world.sup.owner_current)
    assert "terminate" not in job.events


def test_recovery_requires_trusted_guard_and_live_exact_birth_or_remains_unknown(world):
    identity = launch(world)
    finish(world, identity)
    old = world.ad
    recovered = adapter_module.ResilientHarnessAdapter(
        old.envelope, old.artifact_root,
        capabilities={"write_paths": ["src/", "a.txt"],
                      "commands": [["python", "-m", "pytest"]], "mcp": "none"},
        engine_path=old.engine_path, clock=old.clock, job_factory=FakeJob)
    recovered._probe = old._probe
    recovered.preflight()
    try:
        with pytest.raises(HarnessError):
            recovered.read_worker(identity)
        FakeJob.jobs[identity["job_name"]].leader = False
        with pytest.raises(HarnessError):
            recovered.read_worker(identity, owner_guard=world.sup.owner_current)
        FakeJob.jobs.pop(identity["job_name"])
        assert recovered.observe(identity)["state"] == "UNKNOWN"
    finally:
        recovered.close()


@pytest.mark.parametrize("file", ["delegate.py", "supervisor_launch_v2.py"])
def test_engine_and_identity_bundle_drift_blocks_collection(world, file):
    identity = launch(world)
    paths = finish(world, identity)
    path = world.engine.parent / file
    path.write_bytes(path.read_bytes() + b"\n# drift\n")
    with pytest.raises(HarnessError, match="engine_changed"):
        world.ad.read_worker(identity)
    assert not paths["capture"].exists()


@pytest.mark.parametrize("fault", ["receipt", "escape", "scope"])
def test_seed_pins_are_not_worker_self_hash_authority(world, fault):
    effect = admit(world)
    data = dict(effect.data)
    if fault == "receipt":
        data["seed_receipt"] = "0" * 64
    elif fault == "escape":
        data["seed_ref"] = "../outside"
    else:
        data["seed_candidate"] = "0" * 64
    with pytest.raises(HarnessError):
        world.ad.launch(SimpleNamespace(data=data), "task\nbrief",
                        on_process=world.sup.record_process_identity,
                        owner_guard=world.sup.owner_current)
    assert not FakeJob.jobs

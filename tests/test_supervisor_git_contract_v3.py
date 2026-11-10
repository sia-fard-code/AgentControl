"""Real Git materialization and CURRENT lineage; no worker or validator execution."""
import dataclasses
import os
from pathlib import Path
import shutil
from types import SimpleNamespace

import test_supervisor_harness_v3_integration as fixtures
from test_supervisor_identity_v2 import git

candidate = fixtures.candidate
codec = fixtures.codec
contract = fixtures.contract


def test_git_worktrees_materialize_approved_baseline_then_current(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(home / "gitconfig"))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setattr(fixtures.FakeJob, "jobs", {})
    monkeypatch.setattr(fixtures.FakeJob, "supported", True)

    project = tmp_path / "project"
    (project / ".claude").mkdir(parents=True)
    policy = contract.canonical({"allowed_cmds": [["python", "-m", "pytest"]]})
    (project / ".claude" / "agent-policy.json").write_bytes(policy)
    (project / "DATA").mkdir()
    baseline_files = {"binary.bin": b"\x00baseline\xff", "old.txt": b"baseline old"}
    for name, raw in baseline_files.items():
        (project / "DATA" / name).write_bytes(raw)
    git(project, "init")
    git(project, "add", ".claude/agent-policy.json", "DATA")
    git(project, "commit", "-m", "synthetic approved baseline")
    baseline = git(project, "rev-parse", "HEAD")
    assert len(baseline) == 40 and all(c in "0123456789abcdef" for c in baseline)
    original_status = git(project, "status", "--porcelain")

    artifacts = tmp_path / "artifacts"
    (artifacts / "snapshots").mkdir(parents=True)
    prepared = tmp_path / "prepared"
    shutil.copytree(project / "DATA", prepared / "DATA")
    bounds = candidate.SnapshotBounds(
        max_entries=100, max_file_bytes=10000, max_total_bytes=100000,
        max_manifest_bytes=65536, max_path_bytes=512, max_seconds=10)
    inputs = ("DATA",)
    initial = candidate.capture(
        prepared, artifacts / "snapshots" / "initial", bounds=bounds,
        inputs=inputs, owner_guard=lambda: True, strict_layout=True)
    body = fixtures.envelope_dict_v2(project)
    body.pop("baseline_commit")
    body.update(
        schema_version=3, models=[fixtures.PLANNER_MODEL, fixtures.WORKER_MODEL],
        policy_digest=fixtures.sha(policy), cancel_authority=True,
        capabilities={"write": ["DATA/"],
                      "commands": [fixtures.command_id(["python", "-m", "pytest"])],
                      "mcp": []},
        continuation=dict(codec.CONTINUATION),
        candidate={"kind": "git", "baseline_commit": baseline,
                   "initial_sha256": initial.manifest_sha256,
                   "inputs": list(inputs), "bounds": dataclasses.asdict(bounds)})
    env = codec.load_envelope3(body, project)
    installed = tmp_path / "installed"
    installed.mkdir()
    for name in fixtures.adapter_module.REQUIRED_BUNDLE:
        shutil.copyfile(fixtures.ENGINE / name, installed / name)
    engine = installed / "delegate.py"
    engine.write_bytes(b"# Never executed by this test.\n")
    ad = fixtures.adapter_module.ResilientHarnessAdapter(
        env, artifacts,
        capabilities={"write_paths": ["DATA/"],
                      "commands": [["python", "-m", "pytest"]], "mcp": "none"},
        engine_path=engine, clock=lambda: 100.0, job_factory=fixtures.FakeJob)

    def probe(flag):
        assert flag == "--supervisor-capabilities-v2"
        caps = dict(fixtures.manifest.capabilities2())
        caps["features"] = dict(caps["features"])
        caps["roles"] = dict(caps["roles"])
        return contract.canonical(caps)

    monkeypatch.setattr(ad, "_probe", probe)
    monkeypatch.setattr(fixtures.adapter_module, "_terminate_suspended",
                        lambda child: child.job.events.append("abort"))
    ad.preflight()
    try:
        with fixtures.state.Supervisor(tmp_path / "state", env, lambda: 100.0,
                                       artifact_dir=artifacts) as sup:
            sup.register_snapshot3(initial.manifest_sha256, "snapshots/initial",
                                   initial.receipt_sha256, initial.scope_sha256)
            sup.admit_planner("p0", "planner0", 2)
            sup.record_plan("p0", contract.canonical({
                "schema_version": 1, "plan_revision": 0,
                "tasks": [{"task_id": "T", "depends_on": [],
                           "objective_digest": fixtures.sha(b"T"),
                           "criteria": [env.criteria[0]], "hypothesis_ids": []}]}), None)
            ad.set_snapshot_record_resolver(sup.snapshots3.get)
            world = SimpleNamespace(ad=ad, sup=sup)

            def checked_root(identity):
                paths = ad._paths(identity)
                root = paths["candidate"]
                assert root != project and root.is_dir()
                assert (root / ".git").is_file()
                assert git(root, "rev-parse", "HEAD") == baseline
                # Exercise the same strict administration/identity checks as launch.
                m = fixtures.manifest.load_manifest2(
                    os.fspath(paths["manifest"]),
                    fixtures.sha(paths["manifest"].read_bytes()))
                ad._administration(m, sup.owner_current)
                digest = ad._identity_provider(m, sup.owner_current)(os.fspath(root))
                assert digest == identity["seed_candidate"]
                return root

            try:
                first = fixtures.launch(world)
            except fixtures.HarnessError as exc:
                if exc.__context__ is not None:
                    raise exc.__context__ from exc
                raise
            first_root = checked_root(first)
            assert {p.name: p.read_bytes() for p in (first_root / "DATA").iterdir()} == baseline_files
            first_job = fixtures.FakeJob.jobs[first["job_name"]]
            assert first_job.events == ["suspended", "resume"]
            # FakeJob has no worker: explicitly establish known quiescence/yield.
            first_job.count = 0
            sup.observe_attempt("a", "COLLECTED", "failed", True, None)
            changed = b"\x00CURRENT\xfe\x00"
            (first_root / "DATA" / "binary.bin").write_bytes(changed)
            (first_root / "DATA" / "old.txt").unlink()
            (first_root / "DATA" / "new.bin").write_bytes(b"\xffnew\x00")
            packet, paths = ad._packet(first)
            admitted = fixtures.manifest.load_manifest2(
                os.fspath(paths["manifest"]), packet["manifest_sha256"])
            observed = ad._identity_provider(admitted, sup.owner_current)(os.fspath(first_root))
            admin = ad._capture_admin(admitted, packet, sup.owner_current, observed)
            current = candidate.capture(
                first_root, artifacts / "snapshots" / "current", bounds=bounds,
                inputs=inputs, owner_guard=sup.owner_current, strict_layout=True,
                seed_sha256=initial.manifest_sha256,
                git_admin=admin, context_sha256=codec.snapshot_context3(
                    env.digest, seed_receipt=initial.receipt_sha256,
                    source_attempts=["a"], kind="worker"))
            receipt = sup.register_snapshot3(
                current.manifest_sha256, "snapshots/current", current.receipt_sha256,
                current.scope_sha256, seed=initial.receipt_sha256,
                source_attempts=("a",), kind="worker")
            sup.record_yield3("a", receipt["receipt_digest"],
                              fixtures.sha(b"synthetic trusted handoff"))
            sup.select_candidate(current.manifest_sha256,
                                 receipt_digest=receipt["receipt_digest"])
            second = fixtures.admit(world, "b")
            assert second.data["seed_candidate"] == current.manifest_sha256
            assert second.data["seed_receipt"] == receipt["receipt_digest"]

            def pin(*args):
                sup.record_process_identity(*args)
                job = fixtures.FakeJob.jobs[second.data["job_name"]]
                assert not job.resumed
                assert sup.effect_identity("b")["process"]["birth"] == (
                    "winft:" + str(fixtures.FakeChild.birth))
                job.events.append("journal")

            successor = fixtures.launch(world, second, pin)
            second_root = checked_root(successor)
            assert second_root != first_root
            assert {p.name: p.read_bytes() for p in (second_root / "DATA").iterdir()} == {
                "binary.bin": changed, "new.bin": b"\xffnew\x00"}
            assert not (second_root / "DATA" / "old.txt").exists()
            assert fixtures.FakeJob.jobs[successor["job_name"]].events == [
                "suspended", "journal", "resume"]
            assert {p.name: p.read_bytes() for p in (project / "DATA").iterdir()} == baseline_files
            assert (project / ".claude" / "agent-policy.json").read_bytes() == policy
            assert git(project, "rev-parse", "HEAD") == baseline
            assert git(project, "status", "--porcelain") == original_status
    finally:
        ad.close()

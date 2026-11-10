"""Child routing requires exclusive candidate ownership and immutable administration.

These are process-ownership preconditions, not an OS filesystem sandbox.
"""
from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest


@pytest.fixture
def child(monkeypatch, tmp_path):
    root = Path(__file__).resolve().parents[1]
    monkeypatch.syspath_prepend(str(root / "engine"))
    monkeypatch.syspath_prepend(str(root))
    monkeypatch.setenv("TEMP_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "home"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    return importlib.import_module("delegate")


def test_unknown_pinned_schema_refused_before_config(child, monkeypatch, tmp_path, capsys):
    """A future schema must not fall through to the legacy profile gate."""
    import hashlib
    import json

    manifest = tmp_path / "manifest.json"
    raw = json.dumps({"schema": 999}).encode()
    manifest.write_bytes(raw)
    monkeypatch.setattr(sys, "argv", [
        "delegate.py", "--root", str(tmp_path),
        "--supervisor-manifest", str(manifest),
        "--supervisor-manifest-sha256", hashlib.sha256(raw).hexdigest(),
        "--profile", "resilient-v1", "--oneshot", "--task", "test",
    ])
    monkeypatch.setattr(child, "load_cfg", lambda *a, **k: pytest.fail("config reached"))
    with pytest.raises(SystemExit):
        child.main()
    output = capsys.readouterr()
    assert "schema" in (output.out + output.err).lower()
    assert "legacy profile" not in (output.out + output.err).lower()


def canon(path):
    import os
    return os.path.normcase(os.path.realpath(path))


def sha(raw):
    import hashlib
    return hashlib.sha256(raw).hexdigest()


def reply(*calls):
    import json
    return {"choices": [{"finish_reason": "stop", "message": {
        "content": "readonly report", "tool_calls": [
            {"id": f"call{i}", "type": "function", "function": {
                "name": name, "arguments": json.dumps(arguments)}}
            for i, (name, arguments) in enumerate(calls)]}}]}


@pytest.fixture
def world(child, monkeypatch, tmp_path):
    import json
    import os
    import uuid
    from types import SimpleNamespace
    import candidate
    import supervisor_contract as contract
    import supervisor_manifest_v2 as mv2

    policy, run = tmp_path / "policy", tmp_path / "run"
    policy.mkdir()
    run.mkdir()
    root = run / "candidate"
    root.mkdir()
    (policy / "a.txt").write_text("canonical\n")
    (policy / child.POLICY_FILE).parent.mkdir(parents=True, exist_ok=True)
    (policy / child.POLICY_FILE).write_text("{}")
    (root / "a.txt").write_text("seed\n")
    monkeypatch.chdir(policy)
    monkeypatch.setenv("CLAUDE_AGENT_RUN_INDEX", str(run / "index.jsonl"))
    monkeypatch.setattr(child, "run_index_path", lambda: str(run / "index.jsonl"))
    monkeypatch.setattr(child, "repo_run_index_path", lambda p: str(run / "repo-index.jsonl"))
    monkeypatch.setattr(child.atexit, "register", lambda *a, **k: None)
    monkeypatch.setattr(child.signal, "signal", lambda *a, **k: None)
    monkeypatch.setattr(child.time, "sleep", lambda *a: None)
    monkeypatch.setattr(child, "make_worktree", lambda *a, **k: pytest.fail("make_worktree"))
    native = child.capability_module("supervisor_launch_v2")
    monkeypatch.setattr(native, "_git", lambda *a: pytest.fail("artifact used Git"))
    runtime = child.capability_module("recovery_runtime")
    plan = child.capability_module("finish_gate").VerificationPlan()
    bounds = dict(max_entries=20, max_file_bytes=1000, max_total_bytes=10000,
                  max_manifest_bytes=10000, max_path_bytes=512, max_seconds=10)
    owner_pid = os.getpid()
    # Synthetic parent-owned tree: authority is current native process ownership,
    # never a role string, worker-reported digest, or bool supplied by JSON.
    def parent_owner():
        return os.getpid() == owner_pid and root.parent == run and run.is_dir()
    seed = candidate.identity(str(root), bounds=candidate.SnapshotBounds(**bounds),
                              inputs=("a.txt",), owner_guard=parent_owner, strict_layout=True)
    lid = str(uuid.uuid4())
    transcript = run / "r.md"
    data = {
        "schema_version": 2, "profile": "resilient-v1", "launch_id": lid,
        "binding": {"run_id": child.run_id_for(str(transcript)),
                    "task_sha256": sha(b"tiny task"), "root_identity": canon(policy),
                    "envelope_sha256": "e" * 64,
                    "origin": {"kind": "artifact-manifest", "initial_sha256": seed},
                    "seed_sha256": seed,
                    "scope_sha256": sha(contract.canonical(["a.txt"]))},
        "spec": {"definition_sha256": runtime.rh.definition_sha256(plan),
                 "config_sha256": "c" * 64, "verifier_owner": "coordinator"},
        "candidate_root_identity": canon(root), "owned_dir": canon(run / "r.artifacts"),
    }
    cp = run / "contract.json"
    cp.write_bytes(contract.canonical(data))
    manifest = {
        "schema": mv2.SCHEMA, "root": str(policy),
        "policy_sha256": sha(b"{}"), "orchestration_id": "o", "attempt_id": "a1",
        "launch_id": lid, "run_id": child.run_id_for(str(transcript)), "role": "worker",
        "write_paths": ["a.txt"], "commands": [], "mcp": "none", "max_iters": 3,
        "envelope_sha256": "e" * 64,
        "candidate": {"kind": "artifact-manifest", "initial_sha256": seed,
                      "inputs": ["a.txt"], "bounds": bounds},
        "seed_candidate": seed, "seed_ref": "seed.json", "seed_receipt": "f" * 64,
        "candidate_root": str(root), "run_dir": str(run), "profile": "resilient-v1",
        "recovery_contract_ref": "contract.json", "recovery_contract_sha256": sha(cp.read_bytes()),
    }
    gateway, payloads, prepared, runners, parsed = [], [], [], [], []
    monkeypatch.setattr(child, "load_cfg", lambda: (gateway.append("cfg") or ("http://fake.invalid", "fake")))
    monkeypatch.setattr(child, "refresh_models", lambda *a: gateway.append("models"))
    original_prepare = runtime.RecoveryRuntime.prepare
    def prepare(path, **kwargs):
        result = original_prepare(path, **kwargs)
        prepared.append((result, kwargs))
        assert not gateway and not (run / "index.jsonl").exists()
        return result
    monkeypatch.setattr(runtime.RecoveryRuntime, "prepare", prepare)
    original_read = runtime.read_contract
    def read(path):
        result = original_read(path)
        parsed.append(result)
        return result
    monkeypatch.setattr(runtime, "read_contract", read)
    original_init = child.Runner.__init__
    def init(self, *args, **kwargs):
        original_init(self, *args, **kwargs)
        runners.append((self, kwargs))
    monkeypatch.setattr(child.Runner, "__init__", init)
    state = SimpleNamespace(policy=policy, run=run, root=root, manifest=manifest,
                            contract=data, cp=cp, gateway=gateway, payloads=payloads,
                            prepared=prepared, runners=runners, parsed=parsed, native=native)
    def invoke(extra=(), planner=False, raw=None, digest=None, contract_pin=True):
        if contract_pin:
            manifest["recovery_contract_sha256"] = sha(cp.read_bytes())
        if planner:
            manifest.update(role="planner", write_paths=[], commands=[], max_iters=1,
                            candidate_root=None, seed_candidate=None, seed_ref=None,
                            seed_receipt=None, recovery_contract_ref=None,
                            recovery_contract_sha256=None)
        mp = run / "manifest.json"
        mp.write_bytes(json.dumps(manifest).encode() if raw is None else raw)
        argv = ["delegate.py", "--task", "tiny task", "--root", str(policy),
                "--profile", "resilient-v1", "--no-stream", "--model", "gpt-6-sol",
                "--transcript", str(transcript), "--label", "worker-a1",
                "--launch-id", lid, "--launch-receipt", str(run / "launch.jsonl"),
                "--supervisor-manifest", str(mp), "--supervisor-manifest-sha256",
                sha(mp.read_bytes()) if digest is None else digest]
        argv += (["--oneshot"] if planner else ["--write", "--tool-mode", "native",
                 "--max-iters", "3", "--recovery-contract", str(cp)])
        script = iter([reply(("write_file", {"path": "a.txt", "content": "changed\n"}),
                             ("finish", {"summary": "observations only"})), reply()])
        def chat(url, key, payload):
            payloads.append(payload)
            assert planner or prepared
            return reply() if planner else next(script)
        monkeypatch.setattr(child, "chat", chat)
        monkeypatch.setattr(sys, "argv", argv + list(extra))
        try:
            child.main()
            return 0
        except SystemExit as exc:
            return exc.code
    state.invoke = invoke
    return state


def test_worker_routes_owned_artifact_and_binds_contract2(world):
    import json
    assert world.invoke() == 0
    assert (world.root / "a.txt").read_text() == "changed\n"
    assert (world.policy / "a.txt").read_text() == "canonical\n"
    assert {p.name for p in world.policy.iterdir()} == {"a.txt", ".claude"}
    assert len(world.parsed) == len(world.prepared) == 1
    runtime, facts = world.prepared[0]
    assert runtime.schema_version == 2 and facts["baseline_commit"] is None
    assert facts["parsed"] is world.parsed[0]
    assert facts["boundary"].candidate_root == str(world.root)
    assert callable(facts["identity_provider"])
    # main() has relinquished the exact native launch receipt by this point.
    # A callback tied to that launch must not retain observation authority.
    with pytest.raises(world.native.ManifestError):
        facts["identity_provider"](str(world.root))
    runner, options = world.runners[0]
    assert canon(runner.root) == canon(world.root) and options["mcp_policy"] is None
    meta = json.loads((world.run / "r.artifacts" / "completion.json").read_text())
    assert meta["recovery_handoff_status"] == "bound"
    assert meta["run_binding"] is None and meta["patch_binding"] is None
    rows = [json.loads(line) for line in (world.run / "index.jsonl").read_text().splitlines()]
    assert canon(rows[0]["root"]) == canon(world.policy)


@pytest.mark.parametrize("bad", ["profile", "hash", "schema", "seed", "contract", "pin",
                                  "alias", "worktree", "no-write", "unbound", "schema1"])
def test_preflight_denies_before_gateway_model_index(world, bad):
    import json
    extra, raw, digest, contract_pin = (), None, None, True
    if bad == "profile":
        extra = ("--profile", "legacy")
    elif bad == "hash":
        raw, digest = b"not JSON; pin must fail first", "0" * 64
    elif bad == "schema":
        world.manifest["schema"] = "agentcontrol.supervisor-manifest/v99"
    elif bad == "seed":
        (world.root / "a.txt").write_text("tampered\n")
    elif bad == "contract":
        world.contract["binding"]["task_sha256"] = "0" * 64
        world.cp.write_text(json.dumps(world.contract))
    elif bad == "pin":
        world.cp.write_text("{}")
        contract_pin = False
    elif bad == "alias":
        extra = ("--root", str(world.policy / ".." / "run"))
    elif bad == "worktree":
        extra = ("--worktree",)
    elif bad == "no-write":
        world.manifest["role"] = "planner"  # malformed worker fields cannot confer rights
    elif bad == "unbound":
        extra = ("--recovery-contract", str(world.run / "missing"))
    else:
        world.contract["schema_version"] = 1
        world.cp.write_text(json.dumps(world.contract))
    assert world.invoke(extra, raw=raw, digest=digest, contract_pin=contract_pin) == 2
    assert world.gateway == world.payloads == []
    assert not (world.run / "index.jsonl").exists()
    assert (world.policy / "a.txt").read_text() == "canonical\n"


def test_planner_is_toolless_without_runtime_or_identity(world, monkeypatch):
    monkeypatch.setattr(world.native, "identity_provider2", lambda *a, **k: pytest.fail("planner provider"))
    monkeypatch.setattr(world.native, "verify_seed2", lambda *a, **k: pytest.fail("planner seed"))
    assert world.invoke(planner=True) == 0
    assert world.prepared == world.parsed == []
    assert len(world.payloads) == 1 and not world.payloads[0].get("tools")
    assert (world.root / "a.txt").read_text() == "seed\n"

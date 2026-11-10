"""Tests for engine/recovery_runtime.py (bound resilient-v1 runtime activation, Task 3.4)."""
import hashlib
import importlib.util
import json
import os
import sys
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
CMD = ("python", "-m", "pytest", "--token=SECRET-7731")
TASK = "fix the thing"


@pytest.fixture
def rr(monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / "engine"))
    spec = importlib.util.spec_from_file_location(
        "recovery_runtime_under_test", ROOT / "engine" / "recovery_runtime.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _canon(p):
    return os.path.normcase(os.path.realpath(p))


def env(rr, tmp_path, owner="worker"):
    fg = sys.modules["finish_gate"]
    (tmp_path / "policy").mkdir()
    wt, owned = tmp_path / "wt", tmp_path / "art"
    wt.mkdir(), owned.mkdir()
    plan = fg.VerificationPlan((CMD,))
    lid = str(uuid.uuid4())
    contract = {"schema_version": 1, "profile": "resilient-v1", "launch_id": lid,
                "binding": {"run_id": "run-1", "task_sha256": hashlib.sha256(
                    TASK.encode()).hexdigest(), "root_identity": _canon(tmp_path / "policy"),
                    "baseline_commit": "b" * 40, "patch_ref": "patch.diff",
                    "findings_ref": None},
                "spec": {"definition_sha256": rr.rh.definition_sha256(plan),
                         "config_sha256": "c" * 64, "verifier_owner": owner},
                "candidate_root_identity": _canon(wt), "owned_dir": _canon(owned)}
    cpath = tmp_path / "contract.json"
    cpath.write_text(json.dumps(contract))
    actual = dict(run_id="run-1", task_text=TASK, policy_root=str(tmp_path / "policy"),
                  baseline_commit="b" * 40, candidate_root=str(wt), launch_id=lid,
                  artifact_dir=str(owned), plan=plan)
    return contract, cpath, actual, fg


def code(exc_info):
    return exc_info.value.code


def test_prepare_valid_contract_records_raw_digest_only(rr, tmp_path):
    contract, cpath, actual, _ = env(rr, tmp_path)
    rt = rr.RecoveryRuntime.prepare(str(cpath), **actual)
    meta = rt.metadata()
    assert meta["recovery_profile"] == "resilient-v1"
    assert meta["recovery_contract_sha256"] == hashlib.sha256(cpath.read_bytes()).hexdigest()
    assert meta["recovery_handoff_status"] == "bound" and meta["recovery_handoff_sha256"] is None
    assert "SECRET" not in json.dumps(meta)


@pytest.mark.parametrize("mutate,want", [
    (lambda c: c.update(extra=1), "invalid_contract"),
    (lambda c: c.update(schema_version=2), "invalid_contract"),
    (lambda c: c.update(profile="legacy"), "invalid_contract"),
    (lambda c: c.update(launch_id="not-a-uuid"), "invalid_contract"),
    (lambda c: c["binding"].update(task_sha256="f" * 64), "task_mismatch"),
    (lambda c: c["binding"].update(run_id="run-2"), "run_mismatch"),
    (lambda c: c["binding"].update(baseline_commit="c" * 40), "baseline_mismatch"),
    (lambda c: c["binding"].update(root_identity=_canon(c["owned_dir"])), "root_mismatch"),
    (lambda c: c.update(launch_id=str(uuid.uuid4())), "launch_mismatch"),
    (lambda c: c["spec"].update(definition_sha256="d" * 64), "spec_mismatch"),
    (lambda c: c.update(candidate_root_identity=c["owned_dir"]), "candidate_mismatch"),
    (lambda c: c["spec"].update(verifier_owner="root"), "invalid_contract"),
    (lambda c: c["binding"].update(budget=9), "invalid_contract"),
], ids=["extra", "schema", "profile", "uuid", "task", "run", "base", "root", "launch",
        "spec", "cand", "owner", "widen"])
def test_contract_mismatch_fixed_codes(rr, tmp_path, mutate, want):
    contract, cpath, actual, _ = env(rr, tmp_path)
    mutate(contract)
    cpath.write_text(json.dumps(contract))
    with pytest.raises(rr.RecoveryRuntimeError) as ei:
        rr.RecoveryRuntime.prepare(str(cpath), **actual)
    assert code(ei) == want and "SECRET" not in str(ei.value)


def test_bad_json_and_secret_never_echoed(rr, tmp_path):
    _, cpath, actual, _ = env(rr, tmp_path)
    cpath.write_text('{"SECRET-7731": ')
    with pytest.raises(rr.RecoveryRuntimeError) as ei:
        rr.read_contract(str(cpath))
    assert code(ei) == "invalid_contract" and "SECRET" not in str(ei.value)


def test_artifacts_inside_candidate_refused(rr, tmp_path):
    contract, cpath, actual, _ = env(rr, tmp_path)
    inner = tmp_path / "wt" / "art"
    inner.mkdir()
    contract["owned_dir"] = _canon(inner)
    actual["artifact_dir"] = str(inner)
    cpath.write_text(json.dumps(contract))
    with pytest.raises(rr.RecoveryRuntimeError) as ei:
        rr.RecoveryRuntime.prepare(str(cpath), **actual)
    assert code(ei) == "artifact_inside_candidate"


def test_candidate_equal_policy_root_refused(rr, tmp_path):
    contract, cpath, actual, _ = env(rr, tmp_path)
    contract["candidate_root_identity"] = contract["binding"]["root_identity"]
    actual["candidate_root"] = actual["policy_root"]
    cpath.write_text(json.dumps(contract))
    with pytest.raises(rr.RecoveryRuntimeError) as ei:
        rr.RecoveryRuntime.prepare(str(cpath), **actual)
    assert code(ei) == "isolation_required"


def bound(rr, tmp_path, owner="worker"):
    contract, cpath, actual, fg = env(rr, tmp_path, owner)
    rt = rr.RecoveryRuntime.prepare(str(cpath), **actual)
    ev = fg.RunEvidence("disk", actual["candidate_root"], actual["plan"])
    return rt, ev, contract


def no_hash(rr, monkeypatch):
    def boom(root):
        raise AssertionError("RootHasher must not run for ordinary calls")
    monkeypatch.setattr(rr.evidence_mod, "candidate_identity", boom)


def test_intent_durable_before_write_real_barrier(rr, tmp_path, monkeypatch):
    rt, ev, c = bound(rr, tmp_path)
    no_hash(rr, monkeypatch)
    assert rt.before_call("read_file", ev) is None  # literal read-only: no intent
    pending = rt.before_call("write_file", ev)
    assert rr.rh.read_handoff(c["owned_dir"])["phase"] == "inflight"
    ev.record_mutation("a.txt", "x", "y")
    rt.after_call(pending, ev)
    rec = rr.rh.read_handoff(c["owned_dir"])
    assert rec["phase"] == "settled" and rec["after"]["candidate_sha256"] is None
    assert rec["rerun_owed"] is True and not rt.blocked


def test_begin_failure_blocks_without_action(rr, tmp_path, monkeypatch):
    rt, ev, c = bound(rr, tmp_path)
    def nobar(path):
        raise OSError("unsupported")
    monkeypatch.setattr(rr.rh, "_reused_dirbarrier", nobar)
    with pytest.raises(rr.RecoveryRuntimeError) as ei:
        rt.before_call("run_command", ev)
    assert code(ei) == "durability_unsupported" and rt.blocked
    with pytest.raises(rr.RecoveryRuntimeError) as ei:
        rt.before_call("mcp__x__y", ev)
    assert code(ei) == "recovery_blocked"
    meta = rt.metadata()
    assert meta["recovery_handoff_status"] == "error"
    assert meta["recovery_error"] == "durability_unsupported"


def test_settle_failure_keeps_effect_and_blocks_replay(rr, tmp_path, monkeypatch):
    rt, ev, c = bound(rr, tmp_path)
    pending = rt.before_call("replace_in_file", ev)
    def bad(*a, **k):
        raise rr.rh.HandoffError("publish_failed")
    monkeypatch.setattr(rr.rh, "settle", bad)
    ev.record_mutation("a.txt", "x", "y")
    assert rt.after_call(pending, ev) is None  # never raises: action already ran
    assert rt.blocked and rt.metadata()["recovery_error"] == "publish_failed"
    with pytest.raises(rr.RecoveryRuntimeError):
        rt.before_call("write_file", ev)


def test_verification_call_bounded_hash_failure_is_unknown(rr, tmp_path, monkeypatch):
    rt, ev, c = bound(rr, tmp_path)
    calls = []
    def fail(root):
        calls.append(root)
        raise rr.evidence_mod.EvidenceError("x")
    monkeypatch.setattr(rr.evidence_mod, "candidate_identity", fail)
    pending = rt.before_call("run_command", ev, verification=True)
    ev.record_command(CMD, turn=1, outcome="exited", exit_code=3)
    rt.after_call(pending, ev, verification=True)
    assert len(calls) == 2 and not rt.blocked
    rec = rr.rh.read_handoff(c["owned_dir"])
    assert rec["evidence"]["checks"][0]["exit_code"] == 3 and rec["rerun_owed"] is True


def _hash(rr, monkeypatch, value):
    monkeypatch.setattr(rr.evidence_mod, "candidate_identity", lambda root: value[0])


def test_checkpoint_preserves_current_check(rr, tmp_path, monkeypatch):
    rt, ev, c = bound(rr, tmp_path)
    box = ["a" * 64]
    _hash(rr, monkeypatch, box)
    p = rt.before_call("write_file", ev)
    ev.record_mutation("a.txt", "x", "y")
    rt.after_call(p, ev)
    p = rt.before_call("run_command", ev, verification=True)
    ev.record_command(CMD, turn=2, outcome="exited", exit_code=0)
    rt.after_call(p, ev, verification=True)
    sha = rt.checkpoint(ev)
    rec = rr.rh.read_handoff(c["owned_dir"])
    assert rec["rerun_owed"] is False and rt.metadata()["recovery_handoff_sha256"] == sha
    box[0] = None  # history remains, but unknown CURRENT identity cannot certify it
    def fail(root):
        raise rr.evidence_mod.EvidenceError("x")
    monkeypatch.setattr(rr.evidence_mod, "candidate_identity", fail)
    assert rt.checkpoint(ev) != sha
    rec = rr.rh.read_handoff(c["owned_dir"])
    assert rec["after"]["candidate_sha256"] is None and rec["rerun_owed"]
    assert rec["evidence"]["checks"] and rec["evidence"]["candidate_unavailable"]


def test_external_delta_and_late_edit_owed(rr, tmp_path, monkeypatch):
    rt, ev, c = bound(rr, tmp_path)
    box = ["a" * 64]
    _hash(rr, monkeypatch, box)
    p = rt.before_call("run_command", ev, verification=True)
    ev.record_command(CMD, turn=1, outcome="exited", exit_code=0)
    rt.after_call(p, ev, verification=True)
    box[0] = "e" * 64  # changed with no recorded mutation
    rt.checkpoint(ev)
    assert rr.rh.read_handoff(c["owned_dir"])["rerun_owed"] is True
    p = rt.before_call("write_file", ev)
    ev.record_mutation("a.txt", "x", "z")
    rt.after_call(p, ev)
    rt.checkpoint(ev)
    assert rr.rh.read_handoff(c["owned_dir"])["rerun_owed"] is True


def test_coordinator_owner_always_owed(rr, tmp_path, monkeypatch):
    rt, ev, c = bound(rr, tmp_path, owner="coordinator")
    _hash(rr, monkeypatch, ["a" * 64])
    rt.checkpoint(ev)
    assert rr.rh.read_handoff(c["owned_dir"])["rerun_owed"] is True


def test_unbound_metadata_no_authority(rr):
    meta = rr.unbound_metadata()
    assert meta == {"recovery_profile": "resilient-v1", "recovery_contract_sha256": None,
                    "recovery_handoff_status": "unbound", "recovery_handoff_sha256": None,
                    "recovery_error": "missing_contract"}


@pytest.fixture
def dg(monkeypatch, rr):
    spec = importlib.util.spec_from_file_location(
        "delegate_recovery_under_test", ROOT / "engine" / "delegate.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_delegate_capability_loader_and_stop_reason(dg):
    assert dg.capability_module("recovery_runtime").RecoveryRuntime
    assert "recovery_state" in dg.STOP_REASONS


def test_contract_inside_candidate_refused(rr, tmp_path):
    contract, cpath, actual, _ = env(rr, tmp_path)
    inside = tmp_path / "wt" / "c.json"
    inside.write_text(json.dumps(contract))
    with pytest.raises(rr.RecoveryRuntimeError) as ei:
        rr.RecoveryRuntime.prepare(str(inside), **actual)
    assert code(ei) == "contract_inside_candidate"


def test_prepare_uses_frozen_parse_without_aliases(rr, tmp_path):
    data, path, actual, _ = env(rr, tmp_path)
    parsed = rr.read_contract(path)
    path.write_text('{"SECRET": "changed"}')
    rt = rr.RecoveryRuntime.prepare(path, parsed=parsed, **actual)
    parsed[0]["binding"]["run_id"] = "other"
    assert rt.binding["run_id"] == "run-1"
    assert rt.contract_sha256 == parsed[1]


def _git(root, *args):
    import subprocess
    return subprocess.run(["git", "-c", "user.name=Temp", "-c",
                           "user.email=temp@example.invalid", *args], cwd=root,
                          check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def cli(dg, rr, tmp_path, monkeypatch):
    repo = tmp_path / "p"
    repo.mkdir()
    _git(repo, "init")
    (repo / "base.txt").write_text("base\n")
    _git(repo, "add", "base.txt")
    _git(repo, "commit", "-m", "base")
    external = tmp_path / "out"
    external.mkdir()
    transcript = external / "r.md"
    owned = external / "r.artifacts"
    wt = repo / ".claude" / "worktrees" / "w"
    lid = str(uuid.uuid4())
    plan = sys.modules["finish_gate"].VerificationPlan()
    data = {"schema_version": 1, "profile": "resilient-v1", "launch_id": lid,
            "binding": {"run_id": dg.run_id_for(str(transcript)),
                        "task_sha256": hashlib.sha256(TASK.encode()).hexdigest(),
                        "root_identity": _canon(repo),
                        "baseline_commit": _git(repo, "rev-parse", "HEAD"),
                        "patch_ref": "patch.diff", "findings_ref": None},
            "spec": {"definition_sha256": rr.rh.definition_sha256(plan),
                     "config_sha256": "c" * 64, "verifier_owner": "worker"},
            "candidate_root_identity": _canon(wt), "owned_dir": _canon(owned)}
    path = external / "c.json"
    path.write_text(json.dumps(data))
    monkeypatch.chdir(repo)
    monkeypatch.setenv("CLAUDE_AGENT_RUN_INDEX", str(external / "index.jsonl"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "home"))
    monkeypatch.setattr(dg.atexit, "register", lambda *a, **k: None)
    monkeypatch.setattr(dg.signal, "signal", lambda *a, **k: None)
    monkeypatch.setattr(dg.time, "sleep", lambda s: None)
    gateway = []
    monkeypatch.setattr(dg, "load_cfg", lambda: (gateway.append("cfg") or ("http://fake", "x")))
    monkeypatch.setattr(dg, "refresh_models", lambda *a: gateway.append("models"))
    payloads = []
    class CLI:
        def run(self, replies=(), extra=(), active=True, mode="native", cap=3):
            script = iter(replies)
            def chat(url, key, payload):
                payloads.append(payload)
                reply = next(script)
                if isinstance(reply, Exception):
                    raise reply
                return reply
            monkeypatch.setattr(dg, "chat", chat)
            argv = ["delegate.py", "-t", TASK, "-C", str(repo), "--no-stream",
                    "--model", "gpt-6-sol", "--tool-mode", mode,
                    "--max-iters", str(cap), "--profile", "resilient-v1",
                    "--transcript", str(transcript), "--label", "w",
                    "--launch-id", lid, "--launch-receipt", str(external / "receipt")]
            if mode == "prompt":
                argv += ["--allow-prompt-tools"]
            if active:
                argv += ["--worktree", "--recovery-contract", str(path)]
            monkeypatch.setattr(sys, "argv", argv + list(extra))
            try:
                dg.main()
                return 0
            except SystemExit as exc:
                return exc.code
    result = CLI()
    result.repo, result.wt, result.owned, result.data, result.path = repo, wt, owned, data, path
    result.transcript, result.external = transcript, external
    result.gateway, result.payloads = gateway, payloads
    return result


def reply(*calls):
    return {"choices": [{"finish_reason": "stop", "message": {
        "content": "", "tool_calls": [
            {"id": f"c{i}", "type": "function", "function": {
                "name": name, "arguments": json.dumps(args)}}
            for i, (name, args) in enumerate(calls)]}}]}


def write(name="one"):
    return "write_file", {"path": name, "content": "actual effect\n"}


def finish():
    return "finish", {"summary": "observed; acceptance unverified"}


@pytest.mark.parametrize("field", ["task", "root", "launch", "role", "schema"])
def test_cli_contract_refused_before_gateway_index(cli, field, capsys):
    c = cli.data
    if field == "task":
        c["binding"]["task_sha256"] = "d" * 64
    elif field == "root":
        c["binding"]["root_identity"] = _canon(cli.external)
    elif field == "launch":
        c["launch_id"] = str(uuid.uuid4())
    elif field == "role":
        c["spec"]["verifier_owner"] = "SECRET"
    else:
        c["schema_version"] = 9
    cli.path.write_text(json.dumps(c))
    assert cli.run() == 2
    assert cli.gateway == [] and cli.payloads == []
    assert not (cli.external / "index.jsonl").exists()
    assert "SECRET" not in capsys.readouterr().err


@pytest.mark.parametrize("extra", [("--profile", "legacy"), ("--oneshot",)])
def test_cli_contract_rejects_legacy_and_oneshot(cli, extra):
    assert cli.run(extra=extra) == 2
    assert cli.gateway == []


def test_cli_unbound_completion(cli):
    assert cli.run([reply(finish())], active=False) == 0
    meta = json.loads((cli.owned / "completion.json").read_text())
    assert meta["recovery_handoff_status"] == "unbound"
    assert meta["recovery_handoff_sha256"] is None
    assert meta["recovery_contract_sha256"] is None
    # Recovery authority is absent, but genuine legacy transport evidence is retained.
    if meta["candidate_unavailable"] is not None:
        assert meta["run_binding"] is None and meta["patch_binding"] is None
    elif meta["run_binding"] is not None:
        assert meta["run_binding"]["root_identity"] == _canon(cli.repo)
    assert "task_success" not in meta


@pytest.mark.parametrize("failure", [None, "before", "after"])
def test_cli_durable_hooks_block_following_actions(cli, dg, rr, monkeypatch, failure):
    runtime = dg.capability_module("recovery_runtime")
    seen = []
    dispatch = dg.Runner.dispatch
    def observed(self, name, args):
        if name == "write_file":
            rec = runtime.rh.read_handoff(str(cli.owned))
            assert rec["phase"] == "inflight"
            assert not (cli.wt / args["path"]).exists()
            seen.append(name)
        return dispatch(self, name, args)
    monkeypatch.setattr(dg.Runner, "dispatch", observed)
    if failure == "before":
        def fail(*a, **k):
            raise runtime.rh.HandoffError("publish_failed")
        monkeypatch.setattr(runtime.rh, "begin", fail)
    if failure == "after":
        def fail(*a, **k):
            raise runtime.rh.HandoffError("publish_failed")
        monkeypatch.setattr(runtime.rh, "settle", fail)
    status = cli.run([reply(write(), write("two"), finish())])
    assert status == (3 if failure else 0)
    assert (cli.wt / "one").exists() is (failure != "before")
    assert (cli.wt / "two").exists() is (failure is None)
    assert len(cli.payloads) == 1
    meta = json.loads((cli.owned / "completion.json").read_text())
    assert meta["recovery_handoff_status"] == ("error" if failure else "bound")
    if failure:
        assert "recovery_state" in cli.transcript.read_text()
        assert "not re-run" in cli.transcript.read_text()
    else:
        rec = runtime.rh.read_handoff(str(cli.owned))
        assert rec["phase"] == "settled" and rec["rerun_owed"]
        assert meta["recovery_handoff_sha256"] == hashlib.sha256(
            (cli.owned / runtime.rh.HANDOFF_NAME).read_bytes()).hexdigest()


def test_cli_bound_cap_uses_local_report(cli):
    assert cli.run([reply(write())], cap=2) == 3
    assert len(cli.payloads) == 1
    assert "LOCAL" in cli.transcript.read_text()
    assert (cli.owned / "recovery_handoff.json").exists()


def test_cli_prompt_dispatch_has_intent(cli, dg, monkeypatch):
    runtime = dg.capability_module("recovery_runtime")
    dispatch = dg.Runner.dispatch
    def observed(self, name, args):
        assert runtime.rh.read_handoff(str(cli.owned))["phase"] == "inflight"
        return dispatch(self, name, args)
    monkeypatch.setattr(dg.Runner, "dispatch", observed)
    prompt = {"choices": [{"message": {"content":
        '```tool {"name":"write_file","args":{"path":"one","content":"x"}}```'},
        "finish_reason": "stop"}]}
    assert cli.run([prompt], mode="prompt", cap=2) == 3
    assert (cli.wt / "one").read_text() == "x"


@pytest.mark.parametrize("kind", ["path", "branch", "sanitized", "empty"])
def test_modern_worktree_never_deletes_existing(cli, dg, kind):
    label = "w"
    if kind in ("path", "sanitized"):
        cli.wt.mkdir(parents=True)
        (cli.wt / "keep").write_text("keep")
        if kind == "sanitized":
            label = "w/"
    elif kind == "branch":
        _git(cli.repo, "branch", "agent/w")
    else:
        label = "///"
    with pytest.raises((SystemExit, ValueError)):
        dg.make_worktree(str(cli.repo), label, destructive=False)
    if kind in ("path", "sanitized"):
        assert (cli.wt / "keep").read_text() == "keep"
    if kind == "branch":
        assert _git(cli.repo, "rev-parse", "agent/w") == cli.data["binding"]["baseline_commit"]

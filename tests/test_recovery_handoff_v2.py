"""Negotiated protocol-2 observations; only temporary synthetic files and callbacks."""
import copy
import hashlib
import importlib.util
import json
import os
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
D, E = "d" * 64, "e" * 64
COMMIT = "b" * 40
TASK = "tiny task"
CMD = ("check", "a.txt")
LAUNCH = "12345678-1234-4234-8234-123456789abc"


def canon(path):
    return os.path.normcase(os.path.realpath(path))


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


@pytest.fixture
def rr(monkeypatch, tmp_path):
    monkeypatch.syspath_prepend(str(ROOT / "engine"))
    spec = importlib.util.spec_from_file_location(
        "recovery_runtime_v2_test", ROOT / "engine" / "recovery_runtime.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module.rh, "_reused_dirbarrier", lambda path: None)

    def forbidden(*args, **kwargs):
        raise AssertionError("no subprocess in recovery extension tests")

    monkeypatch.setattr(subprocess, "run", forbidden)
    return module


def environment(rr, tmp_path, kind="artifact-manifest"):
    import supervisor_manifest_v2 as manifest_mod

    policy, run = tmp_path / "p", tmp_path / "r"
    policy.mkdir()
    run.mkdir()
    candidate = run / "candidate"
    candidate.mkdir()
    (candidate / "a.txt").write_bytes(b"x\n")
    owned, path = run / "r.artifacts", run / "c.json"
    fg = sys.modules["finish_gate"]
    plan = fg.VerificationPlan((CMD,))
    origin = ({"kind": "git", "baseline_commit": COMMIT} if kind == "git" else
              {"kind": "artifact-manifest", "initial_sha256": D})
    binding = {
        "run_id": "r", "task_sha256": sha(TASK.encode()), "root_identity": canon(policy),
        "envelope_sha256": E, "origin": origin, "seed_sha256": D,
        "scope_sha256": sha(rr.contract.canonical(["a.txt"])),
    }
    data = {
        "schema_version": 2, "profile": rr.PROFILE, "launch_id": LAUNCH,
        "binding": binding,
        "spec": {"definition_sha256": rr.rh.definition_sha256(plan),
                 "config_sha256": "c" * 64, "verifier_owner": "coordinator"},
        "candidate_root_identity": canon(candidate), "owned_dir": canon(owned),
    }
    path.write_bytes(rr.contract.canonical(data))
    descriptor = dict(origin, initial_sha256=D, inputs=["a.txt"], bounds={
        "max_entries": 100, "max_file_bytes": 1000, "max_total_bytes": 10000,
        "max_manifest_bytes": 10000, "max_path_bytes": 512, "max_seconds": 10})
    manifest = manifest_mod._shape({
        "schema": manifest_mod.SCHEMA, "root": canon(policy), "policy_sha256": D,
        "orchestration_id": "o", "attempt_id": "a", "launch_id": LAUNCH, "run_id": "r",
        "role": "worker", "write_paths": ["a.txt"], "commands": [list(CMD)],
        "mcp": "none", "max_iters": 2, "envelope_sha256": E, "candidate": descriptor,
        "seed_candidate": D, "seed_ref": "seed.json", "seed_receipt": E,
        "candidate_root": canon(candidate), "run_dir": canon(run),
        "profile": rr.PROFILE, "recovery_contract_ref": "c.json",
        "recovery_contract_sha256": sha(path.read_bytes()),
    })
    args = SimpleNamespace(
        allow_prompt_tools=False, verify_command=[], transcript=str(run / "r.md"),
        oneshot=False, write=True, worktree=False, patch=None, max_iters=2,
        label="worker-a", recovery_contract=str(path), profile="resilient-v1")
    actual = dict(
        run_id="r", task_text=TASK, policy_root=str(policy), baseline_commit=COMMIT,
        candidate_root=str(candidate), launch_id=LAUNCH, artifact_dir=str(owned), plan=plan)
    return SimpleNamespace(rr=rr, rh=rr.rh, mm=manifest_mod, manifest=manifest, args=args,
                           actual=actual, data=data, path=path, policy=policy, run=run,
                           candidate=candidate, owned=owned, plan=plan, fg=fg)


def boundary(s, monkeypatch):
    monkeypatch.setattr(s.mm, "policy_sha256", lambda *args: D)
    return s.mm.validate_launch2(
        s.manifest, s.args, manifest_path=str(s.run / "m.json"), main_root=str(s.policy),
        policy_file=None, launch_id=LAUNCH, run_id="r", mode="native", allowed_cmds=(CMD,),
        permitted=lambda rule, argv: tuple(argv) == rule,
        verify_seed=lambda manifest, root: canon(root) == canon(s.candidate))


def prepare(s, monkeypatch, provider=None, **kwargs):
    return s.rr.RecoveryRuntime.prepare(
        s.path, boundary=boundary(s, monkeypatch),
        identity_provider=(lambda root: D) if provider is None else provider,
        **dict(s.actual, **kwargs))


def rewrite(s, *, pin=True):
    s.path.write_bytes(s.rr.contract.canonical(s.data))
    if pin:
        s.manifest = replace(s.manifest, recovery_contract_sha256=sha(s.path.read_bytes()))


def evidence(s):
    return s.fg.RunEvidence("disk", canon(s.candidate), s.plan)


def completion(s, rt):
    raw = s.rr.contract.canonical({"recovery_profile": s.rr.PROFILE,
                                  "recovery_contract_sha256": rt.contract_sha256,
                                  "recovery_handoff_sha256": rt.handoff_sha256})
    (s.owned / s.rh.COMPLETION_NAME).write_bytes(raw)
    return SimpleNamespace(completion_sha256=sha(raw))


def collect(s, rt, result, provider, **kwargs):
    return s.rh.collect_handoff(
        s.owned, result, expected=s.data["binding"], approved=s.data["spec"], plan=s.plan,
        candidate_root_identity=canon(s.candidate), identity_provider=provider,
        contract_sha256=rt.contract_sha256, **kwargs)


@pytest.mark.parametrize("kind", ["git", "artifact-manifest"], ids=["git", "artifact"])
def test_v2_roundtrip_generations_and_observation_only(rr, tmp_path, monkeypatch, kind):
    s = environment(rr, tmp_path, kind)
    barriers = []
    monkeypatch.setattr(s.rh, "_reused_dirbarrier", barriers.append)
    rt, ev = prepare(s, monkeypatch), evidence(s)
    for generation in (1, 2):
        pending = rt.before_call("run_command", ev, verification=True)
        assert pending["schema_version"] == 2 and pending["generation"] == generation
        assert pending["binding"] == s.data["binding"] and pending["phase"] == "inflight"
        ev.record_command(CMD, turn=generation, outcome="exited", exit_code=0)
        rt.after_call(pending, ev, verification=True)
        record = s.rh.read_handoff(s.owned)
        assert record["phase"] == "settled" and record["rerun_owed"] is True
        assert set(record) == s.rh.TOP_KEYS
        assert "task_success" not in record and "epoch" not in record and "caps" not in record
    assert len(barriers) >= 4
    record, digest = collect(s, rt, completion(s, rt), lambda root: D)
    assert digest == rt.handoff_sha256 and record["rerun_owed"]


def test_artifact_never_reads_baseline_or_legacy_identity(rr, tmp_path, monkeypatch):
    s = environment(rr, tmp_path)

    class ForbiddenBaseline:
        def __eq__(self, other):
            raise AssertionError("artifact must not compare a Git baseline")

    def forbidden(root):
        raise AssertionError("v2 must not use the legacy flat-tree identity")

    monkeypatch.setattr(rr.evidence_mod, "candidate_identity", forbidden)
    calls = []

    def identity(root):
        assert root == canon(s.candidate)
        calls.append(root)
        return D

    rt = prepare(s, monkeypatch, provider=identity, baseline_commit=ForbiddenBaseline())
    ev = evidence(s)
    assert rt.before_call("read_file", ev) is None
    pending = rt.before_call("write_file", ev)
    ev.record_mutation("a.txt", "x", "y")
    rt.after_call(pending, ev)
    assert calls == []
    pending = rt.before_call("run_command", ev, verification=True)
    ev.record_command(CMD, turn=2, outcome="exited", exit_code=7)
    rt.after_call(pending, ev, verification=True)
    assert len(calls) == 2
    rt.checkpoint(ev)
    collect(s, rt, completion(s, rt), identity)
    assert len(calls) == 4
    assert s.rh.read_handoff(s.owned)["evidence"]["checks"][0]["exit_code"] == 7


def test_v1_golden_shapes_bytes_and_digests(rr, tmp_path):
    s = environment(rr, tmp_path)
    data = copy.deepcopy(s.data)
    data["schema_version"] = 1
    data["binding"] = {"run_id": "r", "task_sha256": sha(TASK.encode()),
                       "root_identity": canon(s.policy), "baseline_commit": COMMIT,
                       "patch_ref": "patch.diff", "findings_ref": None}
    data["spec"]["verifier_owner"] = "worker"
    raw = json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    s.path.write_bytes(raw)
    parsed, digest = rr.read_contract(s.path)
    assert parsed == data and digest == sha(raw)
    rt, ev = rr.RecoveryRuntime.prepare(s.path, **s.actual), evidence(s)
    pending = rt.before_call("write_file", ev)
    expected = {"schema_version": 1, "binding": data["binding"], "generation": 1,
                "spec": data["spec"], "phase": "inflight",
                "before": {"candidate_sha256": None, "mutation_seq": 0}, "after": None,
                "evidence": {"write_mode": "disk", "root_identity": canon(s.candidate),
                             "mutation_seq": 0, "uncertain_mutation": False, "staged_paths": [],
                             "checks": [], "candidate_unavailable": False}, "rerun_owed": True}
    assert pending == expected
    assert (s.owned / s.rh.HANDOFF_NAME).read_bytes() \
        == json.dumps(expected, sort_keys=True, separators=(",", ":")).encode()
    rt.after_call(pending, ev)
    assert rt.metadata()["recovery_contract_sha256"] == sha(raw)


def test_v1_is_not_implicitly_upgraded(rr, tmp_path):
    s = environment(rr, tmp_path)
    s.owned.mkdir()
    ev = evidence(s)
    v1 = {"run_id": "r", "task_sha256": sha(TASK.encode()), "root_identity": canon(s.policy),
          "baseline_commit": COMMIT, "patch_ref": "patch.diff", "findings_ref": None}
    pending = s.rh.begin(s.owned, binding=v1, spec=s.data["spec"], evidence=ev, before_sha256=D)
    s.rh.settle(s.owned, pending, evidence=ev, after_sha256=D)
    before = (s.owned / s.rh.HANDOFF_NAME).read_bytes()
    with pytest.raises(s.rh.HandoffError, match="invalid_binding"):
        s.rh.begin(s.owned, binding=s.data["binding"], spec=s.data["spec"], evidence=ev,
                   before_sha256=D)
    with pytest.raises(s.rh.HandoffError, match="version_mismatch"):
        s.rh.begin(s.owned, binding=s.data["binding"], spec=s.data["spec"], evidence=ev,
                   before_sha256=D, schema_version=2)
    assert (s.owned / s.rh.HANDOFF_NAME).read_bytes() == before


@pytest.mark.parametrize("mutate", [
    lambda b: b.update(extra=1), lambda b: b.update(task_sha256=None),
    lambda b: b.update(seed_sha256=True), lambda b: b.update(scope_sha256="bad"),
    lambda b: b.update(root_identity="relative"), lambda b: b.update(root_identity="/bad\0root"),
    lambda b: b.update(origin={"kind": "other", "initial_sha256": D}),
    lambda b: b.update(origin={"kind": "git", "baseline_commit": "0" * 40}),
    lambda b: b.update(origin={"kind": "git", "baseline_commit": None}),
    lambda b: b.update(origin={"kind": "artifact-manifest", "initial_sha256": None}),
    lambda b: b["origin"].update(baseline_commit=None),
], ids=["extra", "null", "bool", "sha", "relative", "nul", "kind", "zero", "commit",
        "initial", "origin-extra"])
def test_bad_v2_bindings_rejected_before_publish(rr, tmp_path, mutate):
    s = environment(rr, tmp_path)
    s.owned.mkdir()
    mutate(s.data["binding"])
    with pytest.raises(s.rh.HandoffError, match="invalid_binding"):
        s.rh.begin(s.owned, binding=s.data["binding"], spec=s.data["spec"], evidence=evidence(s),
                   before_sha256=D, schema_version=2)
    assert not (s.owned / s.rh.HANDOFF_NAME).exists()


@pytest.mark.parametrize("text", [
    lambda t: t.replace('"schema_version":2', '"schema_version":true'),
    lambda t: t.replace('"schema_version":2', '"schema_version":3'),
    lambda t: t.replace('"schema_version":2', '"schema_version":2,"schema_version":2'),
    lambda t: t.replace('"schema_version":2', '"schema_version":NaN'),
    lambda t: t.replace('"schema_version":2', '"schema_version":1e400'),
    lambda t: t.replace('"schema_version":2', '"extra":false,"schema_version":2'),
], ids=["bool", "unknown", "duplicate", "nan", "overflow", "extra"])
def test_strict_v2_config_and_record_json(rr, tmp_path, monkeypatch, text):
    s = environment(rr, tmp_path)
    rt = prepare(s, monkeypatch)
    rt.checkpoint(evidence(s))
    path = s.owned / s.rh.HANDOFF_NAME
    path.write_text(text(path.read_text()))
    with pytest.raises(s.rh.HandoffError, match="invalid_record"):
        s.rh.read_handoff(s.owned)
    s.path.write_text(text(s.path.read_text()))
    with pytest.raises(rr.RecoveryRuntimeError, match="invalid_contract"):
        rr.read_contract(s.path)


@pytest.mark.parametrize("field", ["root", "envelope", "seed", "scope", "task", "run",
                                   "launch", "pin", "owner", "candidate", "boundary-root", "origin"])
def test_v2_pins_and_actual_facts_fail_before_directory_writes(rr, tmp_path, monkeypatch, field):
    s = environment(rr, tmp_path)
    if field in ("root", "envelope", "seed", "scope", "task", "run", "origin"):
        key = {"root": "root_identity", "envelope": "envelope_sha256", "seed": "seed_sha256",
               "scope": "scope_sha256", "task": "task_sha256", "run": "run_id", "origin": "origin"}[field]
        s.data["binding"][key] = {"root": canon(s.run), "run": "other", "envelope": D, "origin": {
            "kind": "artifact-manifest", "initial_sha256": E}}.get(field, E)
    elif field == "launch": s.data["launch_id"] = "22345678-1234-4234-8234-123456789abc"
    elif field == "owner": s.data["spec"]["verifier_owner"] = "worker"
    elif field == "candidate": s.data["candidate_root_identity"] = canon(s.run / "other")
    elif field == "pin": s.data["spec"]["config_sha256"] = E
    rewrite(s, pin=field != "pin")
    b = boundary(s, monkeypatch) if field != "pin" else s.mm.Boundary2(
        s.manifest.write_paths, s.manifest.commands, candidate_root=canon(s.candidate),
        manifest=s.manifest)
    if field == "boundary-root": b = replace(b, candidate_root=canon(s.run / "other"))
    with pytest.raises(rr.RecoveryRuntimeError):
        rr.RecoveryRuntime.prepare(s.path, boundary=b, identity_provider=lambda root: D, **s.actual)
    assert not s.owned.exists()


def test_v2_requires_internal_boundary_and_callback(rr, tmp_path, monkeypatch):
    s = environment(rr, tmp_path)
    b = boundary(s, monkeypatch)
    with pytest.raises(rr.RecoveryRuntimeError, match="identity_provider_required"):
        rr.RecoveryRuntime.prepare(s.path, boundary=b, **s.actual)
    for invalid in (s.manifest, {"manifest": s.manifest}):
        with pytest.raises(rr.RecoveryRuntimeError, match="invalid_boundary"):
            rr.RecoveryRuntime.prepare(s.path, boundary=invalid,
                                       identity_provider=lambda root: D, **s.actual)
    assert not s.owned.exists()


def test_boundary_protocol_is_loader_alias_safe(rr, tmp_path, monkeypatch):
    s = environment(rr, tmp_path)
    b = boundary(s, monkeypatch)

    class LoaderAliasBoundary:
        manifest, candidate_root = b.manifest, b.candidate_root
        write_paths, commands = b.write_paths, b.commands
        check_write, check_command = b.check_write, b.check_command

    rt = rr.RecoveryRuntime.prepare(s.path, boundary=LoaderAliasBoundary(),
                                    identity_provider=lambda root: D, **s.actual)
    assert rt.contract_sha256 == sha(s.path.read_bytes())


def test_v2_cached_parse_is_frozen_and_pins_original_bytes(rr, tmp_path, monkeypatch):
    s = environment(rr, tmp_path)
    b = boundary(s, monkeypatch)
    parsed = rr.read_contract(s.path)
    with pytest.raises(TypeError):
        parsed[0]["binding"]["origin"]["initial_sha256"] = E
    s.path.write_bytes(b'{"changed":true}')
    rt = rr.RecoveryRuntime.prepare(s.path, parsed=parsed, boundary=b,
                                    identity_provider=lambda root: D, **s.actual)
    assert rt.contract_sha256 == parsed[1] and rt.binding == s.data["binding"]


def test_binding_roots_are_checked_lexically(rr, tmp_path, monkeypatch):
    s = environment(rr, tmp_path)
    foreign, actual = canon(tmp_path / "foreign"), os.path.realpath

    def guarded(path, *args, **kwargs):
        assert os.fspath(path) != foreign
        return actual(path, *args, **kwargs)

    monkeypatch.setattr(os.path, "realpath", guarded)
    claim = dict(s.data["binding"], root_identity=foreign)
    assert s.rh._binding(claim, None, schema_version=2) == claim


@pytest.mark.parametrize("variant", ["late", "none", "raise", "bad", "uncertain"])
def test_identity_failure_and_late_edit_preserve_debt_and_exit(rr, tmp_path, monkeypatch, variant):
    s = environment(rr, tmp_path)
    box = [D]

    def provider(root):
        assert root == canon(s.candidate)
        if isinstance(box[0], Exception): raise box[0]
        return box[0]

    rt, ev = prepare(s, monkeypatch, provider=provider), evidence(s)
    pending = rt.before_call("run_command", ev, verification=True)
    ev.record_command(CMD, turn=1, outcome="exited", exit_code=9)
    rt.after_call(pending, ev, verification=True)
    old = rt.handoff_sha256
    if variant == "late":
        (s.candidate / "a.txt").write_bytes(b"late")
        box[0] = E
    elif variant == "none": box[0] = None
    elif variant == "raise": box[0] = RuntimeError("private callback details")
    elif variant == "bad": box[0] = True
    else: ev.record_unknown_mutation()
    assert rt.checkpoint(ev) != old
    record = s.rh.read_handoff(s.owned)
    assert record["rerun_owed"] and not rt.blocked
    assert record["evidence"]["checks"][0]["exit_code"] == 9
    if variant in ("none", "raise", "bad"):
        assert record["after"]["candidate_sha256"] is None
        assert record["evidence"]["candidate_unavailable"]
    elif variant == "uncertain": assert record["evidence"]["uncertain_mutation"]


@pytest.mark.parametrize("flag", ["uncertain", "unavailable"])
def test_v2_sticky_flags_cannot_be_cleared(rr, tmp_path, monkeypatch, flag):
    s = environment(rr, tmp_path)
    rt, ev = prepare(s, monkeypatch), evidence(s)
    if flag == "uncertain": ev.record_unknown_mutation()
    else: ev.record_candidate_unavailable()
    pending = rt.before_call("run_command", ev, verification=True)
    fresh = evidence(s)
    fresh.record_mutation("a.txt", "x", "y")
    fresh.record_command(CMD, turn=2, outcome="exited", exit_code=0)
    rt.after_call(pending, fresh, verification=True)
    assert rt.blocked and rt.error == "evidence_regressed"
    assert s.rh.read_handoff(s.owned)["phase"] == "inflight"


@pytest.mark.parametrize("variant", ["late", "completion", "handoff", "contract", "missing", "unavailable"])
def test_v2_collector_rejects_unconfirmed_observations(rr, tmp_path, monkeypatch, variant):
    s = environment(rr, tmp_path)
    rt = prepare(s, monkeypatch)
    rt.checkpoint(evidence(s))
    result, provider = completion(s, rt), lambda root: D
    if variant == "late": provider = lambda root: E
    elif variant == "completion":
        path = s.owned / s.rh.COMPLETION_NAME
        path.write_bytes(path.read_bytes() + b" ")
    elif variant == "handoff":
        path = s.owned / s.rh.HANDOFF_NAME
        path.write_bytes(path.read_bytes().replace(b'"generation":1', b'"generation":2'))
    elif variant == "contract":
        path = s.owned / s.rh.COMPLETION_NAME
        data = json.loads(path.read_bytes())
        data["recovery_contract_sha256"] = E
        path.write_bytes(rr.contract.canonical(data))
        result.completion_sha256 = sha(path.read_bytes())
    elif variant == "missing": provider = None
    else: provider = lambda root: None
    with pytest.raises(s.rh.HandoffError): collect(s, rt, result, provider)


def test_v2_generations_retain_binding_and_spec(rr, tmp_path, monkeypatch):
    s = environment(rr, tmp_path)
    rt, ev = prepare(s, monkeypatch), evidence(s)
    rt.checkpoint(ev)
    original = (s.owned / s.rh.HANDOFF_NAME).read_bytes()
    for key, value, code in (
            ("binding", dict(rt.binding, seed_sha256=E), "binding_mismatch"),
            ("spec", dict(rt.spec, config_sha256=E), "spec_mismatch")):
        args = dict(binding=rt.binding, spec=rt.spec)
        args[key] = value
        with pytest.raises(s.rh.HandoffError, match=code):
            s.rh.begin(s.owned, evidence=ev, before_sha256=D, schema_version=2, **args)
        assert (s.owned / s.rh.HANDOFF_NAME).read_bytes() == original


def test_v2_durability_failure_does_not_acknowledge_dispatch(rr, tmp_path, monkeypatch):
    s = environment(rr, tmp_path)
    rt = prepare(s, monkeypatch)

    def unavailable(path): raise OSError("synthetic barrier failure")

    monkeypatch.setattr(s.rh, "_reused_dirbarrier", unavailable)
    with pytest.raises(rr.RecoveryRuntimeError, match="durability_unsupported"):
        rt.before_call("write_file", evidence(s))
    assert rt.blocked
    record = s.rh.read_handoff(s.owned)
    assert record["schema_version"] == 2 and record["phase"] == "inflight" and record["rerun_owed"]

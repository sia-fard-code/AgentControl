"""Synthetic tests for engine/recovery_handoff.py (metadata-only handoff primitives)."""
import hashlib
import importlib.util
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
CMD = ("python", "-m", "pytest", "--token=SECRET-7731")
D, E = "d" * 64, "e" * 64


@pytest.fixture
def rh(monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / "engine"))
    spec = importlib.util.spec_from_file_location(
        "recovery_handoff_core_under_test", ROOT / "engine" / "recovery_handoff.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    # Synthetic state-machine tests only; the real barrier is covered by its own test.
    monkeypatch.setattr(mod, "_reused_dirbarrier", lambda path: None)
    return mod


def _canon(p):
    return os.path.normcase(os.path.realpath(p))


def make(rh, tmp_path, owner="worker", mode="disk"):
    fg = sys.modules["finish_gate"]
    wt = tmp_path / "wt"
    wt.mkdir()
    (wt / "a.txt").write_text("x")
    owned = tmp_path / "owned"
    owned.mkdir()
    plan = fg.VerificationPlan((CMD,))
    binding = {"run_id": "run-1", "task_sha256": "a" * 64,
               "root_identity": _canon(tmp_path / "policy"), "baseline_commit": "b" * 40,
               "patch_ref": "patch.diff", "findings_ref": None}
    spec = {"definition_sha256": rh.definition_sha256(plan) if owner == "worker" else E,
            "config_sha256": "c" * 64, "verifier_owner": owner}
    return SimpleNamespace(fg=fg, wt=wt, owned=owned, plan=plan, binding=binding, spec=spec,
                           ev=fg.RunEvidence(mode, str(wt), plan))


def clean_cycle(rh, s, before=D, after=D):
    s.ev.record_mutation("a.txt", "x", "y")
    pending = rh.begin(s.owned, binding=s.binding, spec=s.spec, evidence=s.ev,
                       before_sha256=before)
    s.ev.record_command(CMD, turn=2, outcome="exited", exit_code=0)
    return pending, rh.settle(s.owned, pending, evidence=s.ev, after_sha256=after)


def code(exc_info):
    return exc_info.value.code


def test_worker_receipt_after_seq_and_definition(rh, tmp_path):
    s = make(rh, tmp_path)
    pending, digest = clean_cycle(rh, s)
    assert pending["phase"] == "inflight" and pending["rerun_owed"] is True
    assert pending["after"] is None
    raw = (s.owned / rh.HANDOFF_NAME).read_bytes()
    assert hashlib.sha256(raw).hexdigest() == digest
    rec = rh.read_handoff(s.owned)
    assert set(rec) == {"schema_version", "binding", "generation", "spec", "phase", "before",
                        "after", "evidence", "rerun_owed"}
    assert rec["phase"] == "settled" and rec["rerun_owed"] is False
    assert b"SECRET" not in raw and b"argv" not in raw and b"trusted" not in raw
    assert rec["after"] == {"candidate_sha256": D, "mutation_seq": 1}


@pytest.mark.parametrize("bad_paths", [[{}], ["a", 1]], ids=["object", "mixed"])
def test_malformed_staged_paths_raise_fixed_error(rh, tmp_path, bad_paths):
    s = make(rh, tmp_path)
    clean_cycle(rh, s)
    path = s.owned / rh.HANDOFF_NAME
    record = json.loads(path.read_text())
    record["evidence"]["staged_paths"] = bad_paths
    path.write_text(json.dumps(record))
    with pytest.raises(rh.HandoffError) as exc:
        rh.read_handoff(s.owned)
    assert exc.value.code == "invalid_record"


def test_settled_point_sequence_must_match_evidence(rh, tmp_path):
    s = make(rh, tmp_path)
    clean_cycle(rh, s)
    path = s.owned / rh.HANDOFF_NAME
    record = json.loads(path.read_text())
    record["after"]["mutation_seq"] += 1
    path.write_text(json.dumps(record))
    with pytest.raises(rh.HandoffError, match="invalid_record"):
        rh.read_handoff(s.owned)


@pytest.mark.parametrize("kind", ["binding", "spec"])
def test_new_generation_cannot_change_existing_authority(rh, tmp_path, kind):
    s = make(rh, tmp_path)
    clean_cycle(rh, s)
    before = (s.owned / rh.HANDOFF_NAME).read_bytes()
    if kind == "binding":
        s.binding["task_sha256"] = E
    else:
        s.spec["config_sha256"] = E
    with pytest.raises(rh.HandoffError, match=kind + "_mismatch"):
        rh.begin(s.owned, binding=s.binding, spec=s.spec, evidence=s.ev, before_sha256=D)
    assert (s.owned / rh.HANDOFF_NAME).read_bytes() == before


def test_partial_os_write_is_completed_before_acknowledgement(rh, tmp_path, monkeypatch):
    s = make(rh, tmp_path)
    actual_write = rh.os.write
    monkeypatch.setattr(rh.os, "write", lambda fd, data: actual_write(fd, data[:7]))
    pending = rh.begin(s.owned, binding=s.binding, spec=s.spec, evidence=s.ev,
                       before_sha256=D)
    assert rh.read_handoff(s.owned) == pending


def test_zero_progress_write_never_acknowledges_intent(rh, tmp_path, monkeypatch):
    s = make(rh, tmp_path)
    monkeypatch.setattr(rh.os, "write", lambda fd, data: 0)
    with pytest.raises(rh.HandoffError, match="publish_failed"):
        rh.begin(s.owned, binding=s.binding, spec=s.spec, evidence=s.ev, before_sha256=D)
    assert not (s.owned / rh.HANDOFF_NAME).exists()


def test_native_directory_barrier_is_acknowledged_or_fail_closed(rh, tmp_path, monkeypatch):
    s = make(rh, tmp_path)
    monkeypatch.setattr(rh, "_reused_dirbarrier", rh.journal._fsync_dir)
    try:
        pending = rh.begin(s.owned, binding=s.binding, spec=s.spec, evidence=s.ev,
                           before_sha256=D)
    except rh.HandoffError as exc:
        assert exc.code == "durability_unsupported"
        assert rh.read_handoff(s.owned)["phase"] == "inflight"
        print("NATIVE_HANDOFF_DURABILITY=unsupported-fail-closed")
    else:
        assert pending == rh.read_handoff(s.owned)
        print("NATIVE_HANDOFF_DURABILITY=acknowledged")


def test_binding_validation_does_not_probe_an_untrusted_root(rh, tmp_path, monkeypatch):
    s = make(rh, tmp_path)
    foreign = _canon(tmp_path / "unapproved-root")
    actual_realpath = rh.os.path.realpath

    def reject_probe(value, *args, **kwargs):
        assert os.fspath(value) != foreign, "untrusted root must not be probed"
        return actual_realpath(value, *args, **kwargs)

    monkeypatch.setattr(rh.os.path, "realpath", reject_probe)
    claim = dict(s.binding, root_identity=foreign)
    assert rh._binding(claim, s.owned) == claim  # structural observation, never authority


def test_collection_compares_evidence_root_using_platform_identity(rh, tmp_path):
    s = make(rh, tmp_path)
    root = _canon(s.wt)
    candidate = rh.evidence_mod.candidate_identity(root)
    _, digest = clean_cycle(rh, s, before=candidate, after=candidate)
    completion = rh.contract.canonical({"recovery_profile": rh.PROFILE,
                                        "recovery_handoff_sha256": digest})
    (s.owned / rh.COMPLETION_NAME).write_bytes(completion)
    result = SimpleNamespace(completion_sha256=hashlib.sha256(completion).hexdigest())
    record, got = rh.collect_handoff(s.owned, result, expected=s.binding, approved=s.spec,
                                     plan=s.plan, candidate_root_identity=root)
    assert got == digest and record["after"]["candidate_sha256"] == candidate


@pytest.mark.parametrize("variant", ["late_edit", "failed", "staged", "hash_changed",
                                     "unknown_after"])
def test_debt_is_owed(rh, tmp_path, variant):
    s = make(rh, tmp_path, mode="stage" if variant == "staged" else "disk")
    s.ev.record_mutation("a.txt", "x", "y")
    pending = rh.begin(s.owned, binding=s.binding, spec=s.spec, evidence=s.ev, before_sha256=D)
    s.ev.record_command(CMD, turn=2, outcome="exited", exit_code=1 if variant == "failed" else 0)
    if variant == "late_edit":
        s.ev.record_mutation("a.txt", "y", "z")
    after = {"hash_changed": E, "unknown_after": None}.get(variant, D)
    rh.settle(s.owned, pending, evidence=s.ev, after_sha256=after)
    assert rh.read_handoff(s.owned)["rerun_owed"] is True


def test_coordinator_debt_always_owed(rh, tmp_path):
    s = make(rh, tmp_path, owner="coordinator")
    clean_cycle(rh, s)
    assert rh.read_handoff(s.owned)["rerun_owed"] is True


@pytest.mark.parametrize("field,value,want", [
    ("definition_sha256", E, "spec_mismatch"), ("verifier_owner", "admin", "invalid_spec"),
    ("config_sha256", "bad", "invalid_spec"), ("verifier_owner", True, "invalid_spec")])
def test_wrong_spec_rejected(rh, tmp_path, field, value, want):
    s = make(rh, tmp_path)
    s.spec[field] = value
    with pytest.raises(rh.HandoffError) as ei:
        rh.begin(s.owned, binding=s.binding, spec=s.spec, evidence=s.ev, before_sha256=D)
    assert code(ei) == want


@pytest.mark.parametrize("field,value", [
    ("patch_ref", "../x"), ("findings_ref", "a\\b"), ("baseline_commit", "b" * 39),
    ("task_sha256", True), ("run_id", 7), ("extra", False), ("root_identity", "rel")])
def test_bad_binding_rejected(rh, tmp_path, field, value):
    s = make(rh, tmp_path)
    s.binding[field] = value
    with pytest.raises(rh.HandoffError) as ei:
        rh.begin(s.owned, binding=s.binding, spec=s.spec, evidence=s.ev, before_sha256=D)
    assert code(ei) == "invalid_binding"
    assert not (s.owned / rh.HANDOFF_NAME).exists()


@pytest.mark.parametrize("mutate", [
    lambda t: t.replace('"generation":1', '"generation":1e400'),
    lambda t: t.replace('"generation":1', '"generation":NaN'),
    lambda t: t.replace('"generation":1', '"generation":true'),
    lambda t: t.replace('"phase"', '"trusted":true,"phase"'),
    lambda t: t + " " * (65 * 1024)])
def test_bad_record_rejected(rh, tmp_path, mutate):
    s = make(rh, tmp_path)
    clean_cycle(rh, s)
    path = s.owned / rh.HANDOFF_NAME
    text = path.read_text()
    assert '"generation":1' in text
    path.write_text(mutate(text))
    with pytest.raises(rh.HandoffError) as ei:
        rh.read_handoff(s.owned)
    assert code(ei) == "invalid_record"


def test_inflight_and_stale_generation(rh, tmp_path):
    s = make(rh, tmp_path)
    pending = rh.begin(s.owned, binding=s.binding, spec=s.spec, evidence=s.ev, before_sha256=D)
    with pytest.raises(rh.HandoffError) as ei:  # crash/restart: inflight blocks a fresh begin
        rh.begin(s.owned, binding=s.binding, spec=s.spec, evidence=s.ev, before_sha256=D)
    assert code(ei) == "inflight_exists"
    rh.settle(s.owned, pending, evidence=s.ev, after_sha256=D)
    with pytest.raises(rh.HandoffError) as ei:
        rh.settle(s.owned, pending, evidence=s.ev, after_sha256=D)
    assert code(ei) == "stale_pending"
    nxt = rh.begin(s.owned, binding=s.binding, spec=s.spec, evidence=s.ev, before_sha256=D)
    assert nxt["generation"] == 2  # checkpoint replaced, generation never reset
    forged = dict(nxt, generation=3)
    with pytest.raises(rh.HandoffError) as ei:
        rh.settle(s.owned, forged, evidence=s.ev, after_sha256=D)
    assert code(ei) == "stale_pending"
    assert rh.read_handoff(s.owned)["phase"] == "inflight"


@pytest.mark.parametrize("flag", ["uncertain", "unavailable"])
def test_sticky_flags_not_cleared(rh, tmp_path, flag):
    s = make(rh, tmp_path)
    if flag == "uncertain":
        s.ev.record_unknown_mutation()
    else:
        s.ev.record_candidate_unavailable()
    pending = rh.begin(s.owned, binding=s.binding, spec=s.spec, evidence=s.ev, before_sha256=D)
    fresh = s.fg.RunEvidence("disk", str(s.wt), s.plan)
    fresh.record_mutation("a.txt", "x", "y")
    fresh.record_command(CMD, turn=3, outcome="exited", exit_code=0)
    with pytest.raises(rh.HandoffError) as ei:
        rh.settle(s.owned, pending, evidence=fresh, after_sha256=D)
    assert code(ei) == "evidence_regressed"
    assert rh.read_handoff(s.owned)["phase"] == "inflight"


def test_barrier_failure_is_fixed_error(rh, tmp_path, monkeypatch):
    s = make(rh, tmp_path)
    clean_cycle(rh, s)
    before = (s.owned / rh.HANDOFF_NAME).read_bytes()

    def boom(path):
        raise OSError("platform refused " + str(path))
    monkeypatch.setattr(rh, "_reused_dirbarrier", boom)
    with pytest.raises(rh.HandoffError) as ei:
        rh.begin(s.owned, binding=s.binding, spec=s.spec, evidence=s.ev, before_sha256=D)
    assert code(ei) == "durability_unsupported" and str(ei.value) == "durability_unsupported"
    assert (s.owned / rh.HANDOFF_NAME).exists() and before  # existing checkpoint not deleted
    assert sorted(p.name for p in s.owned.iterdir()) == sorted([rh.HANDOFF_NAME, rh.LOCK_NAME])


def test_real_platform_barrier(rh, tmp_path, monkeypatch):
    monkeypatch.setattr(rh, "_reused_dirbarrier", sys.modules["supervisor_journal"]._fsync_dir)
    s = make(rh, tmp_path)
    clean_cycle(rh, s)
    assert rh.read_handoff(s.owned)["phase"] == "settled"


def test_lock_contention_fails(rh, tmp_path):
    s = make(rh, tmp_path)
    journal = sys.modules["supervisor_journal"]
    fd = os.open(s.owned / rh.LOCK_NAME, os.O_RDWR | os.O_CREAT)
    try:
        journal._lock(fd)
        with pytest.raises(rh.HandoffError) as ei:
            rh.begin(s.owned, binding=s.binding, spec=s.spec, evidence=s.ev, before_sha256=D)
        assert code(ei) == "locked"
        journal._unlock(fd)
    finally:
        os.close(fd)
    assert not (s.owned / rh.HANDOFF_NAME).exists()


def test_artifacts_inside_known_candidate_forbidden(rh, tmp_path):
    s = make(rh, tmp_path)
    inner = s.wt / ".handoff"
    inner.mkdir()
    with pytest.raises(rh.HandoffError) as ei:
        rh.begin(inner, binding=s.binding, spec=s.spec, evidence=s.ev, before_sha256=D)
    assert code(ei) == "artifact_inside_candidate"
    rh.begin(inner, binding=s.binding, spec=s.spec, evidence=s.ev, before_sha256=None)


def collected(rh, s, after="real"):
    ident = sys.modules["supervisor_evidence"].candidate_identity
    x = ident(s.wt) if after == "real" else after
    _, hdigest = clean_cycle(rh, s, before=x, after=x)
    comp = json.dumps({"recovery_profile": "resilient-v1",
                       "recovery_handoff_sha256": hdigest}).encode()
    (s.owned / "completion.json").write_bytes(comp)
    return SimpleNamespace(completion_sha256=hashlib.sha256(comp).hexdigest())


def collect(rh, s, result, **kw):
    args = dict(expected=s.binding, approved=s.spec, plan=s.plan,
                candidate_root_identity=_canon(s.wt))
    args.update(kw)
    return rh.collect_handoff(s.owned, result, **args)


def test_collect_policy_root_differs_from_worktree(rh, tmp_path):
    s = make(rh, tmp_path)
    result = collected(rh, s)
    rec, digest = collect(rh, s, result)
    assert rec["rerun_owed"] is False and rec["binding"]["root_identity"] != _canon(s.wt)
    assert digest == hashlib.sha256((s.owned / rh.HANDOFF_NAME).read_bytes()).hexdigest()
    assert "argv" not in json.dumps(rec)  # metadata only; argv not returned either
    with pytest.raises(rh.HandoffError) as ei:  # policy root is not the candidate root
        collect(rh, s, result, candidate_root_identity=s.binding["root_identity"])
    assert code(ei) == "root_mismatch"


def test_collect_x_vs_y_rejected(rh, tmp_path):
    s = make(rh, tmp_path)
    result = collected(rh, s)
    (s.wt / "a.txt").write_text("changed after settle")
    with pytest.raises(rh.HandoffError) as ei:
        collect(rh, s, result)
    assert code(ei) == "stale_candidate"


def test_collect_unknown_candidate_never_green(rh, tmp_path):
    s = make(rh, tmp_path)
    result = collected(rh, s, after=None)
    with pytest.raises(rh.HandoffError) as ei:
        collect(rh, s, result)
    assert code(ei) == "candidate_unknown"


@pytest.mark.parametrize("tamper,want", [
    ("completion", "completion_mismatch"), ("handoff", "handoff_mismatch"),
    ("profile", "completion_mismatch"), ("expected", "binding_mismatch"),
    ("approved", "spec_mismatch")])
def test_collect_tampering_refused(rh, tmp_path, tamper, want):
    s = make(rh, tmp_path)
    result = collected(rh, s)
    kw = {}
    comp = s.owned / "completion.json"
    if tamper == "completion":
        comp.write_bytes(comp.read_bytes() + b" ")
    elif tamper == "handoff":
        h = s.owned / rh.HANDOFF_NAME
        h.write_bytes(h.read_bytes().replace(b'"generation":1', b'"generation":2'))
    elif tamper == "profile":
        data = json.loads(comp.read_bytes())
        data["recovery_profile"] = "legacy"
        comp.write_bytes(json.dumps(data).encode())
        result.completion_sha256 = hashlib.sha256(comp.read_bytes()).hexdigest()
    elif tamper == "expected":
        kw["expected"] = dict(s.binding, run_id="run-2")
    else:
        kw["approved"] = dict(s.spec, verifier_owner="coordinator")
    with pytest.raises(rh.HandoffError) as ei:
        collect(rh, s, result, **kw)
    assert code(ei) == want

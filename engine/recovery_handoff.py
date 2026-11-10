"""Durable metadata-only recovery handoff record (resilient-v1, spec section 3).

Records observations bound to a run binding, an approved check spec and candidate digests.
It is NOT an acceptance engine: no field here grants trust, permission or task success.
Raw argv, file content and model text are never persisted; argv is rebuilt from the plan.
"""
import hashlib
import os
import stat
import uuid

import finish_gate as fg
import supervisor_contract as contract
import supervisor_evidence as evidence_mod
import supervisor_journal as journal

HANDOFF_NAME = "recovery_handoff.json"
LOCK_NAME = "recovery_handoff.lock"
COMPLETION_NAME = "completion.json"
PROFILE = "resilient-v1"
MAX_BYTES = 64 * 1024
MAX_COMPLETION_BYTES = contract.MAX_RECORD_BYTES_V2
MAX_INT = contract.MAX_COUNT_V2
TOP_KEYS = frozenset({"schema_version", "binding", "generation", "spec", "phase", "before",
                      "after", "evidence", "rerun_owed"})
BINDING_KEYS = frozenset({"run_id", "task_sha256", "root_identity", "baseline_commit",
                          "patch_ref", "findings_ref"})
BINDING2_KEYS = frozenset({"run_id", "task_sha256", "root_identity", "envelope_sha256",
                           "origin", "seed_sha256", "scope_sha256"})
SPEC_KEYS = frozenset({"definition_sha256", "config_sha256", "verifier_owner"})
POINT_KEYS = frozenset({"candidate_sha256", "mutation_seq"})
EVIDENCE_KEYS = frozenset({"write_mode", "root_identity", "mutation_seq", "uncertain_mutation",
                           "staged_paths", "checks", "candidate_unavailable"})
CHECK_KEYS = frozenset({"check_id", "turn", "mutation_seq", "root_identity", "outcome",
                        "exit_code", "mutated_candidate"})
OUTCOMES = ("exited", "refused", "timed_out", "outcome_unknown")
# Same-module lookup so synthetic tests can stub it; production reuses the journal barrier.
_reused_dirbarrier = journal._fsync_dir


class HandoffError(Exception):
    """Fixed, central-safe reason code only; never user text, paths, argv or OS messages."""

    def __init__(self, code):
        super().__init__(code)
        self.code = code


def _int(value, code, lo=0, hi=MAX_INT):
    if type(value) is not int or not lo <= value <= hi:
        raise HandoffError(code)
    return value


def _bool(value, code):
    if type(value) is not bool:
        raise HandoffError(code)
    return value


def _sha(value, code, nullable=False):
    if value is None and nullable:
        return None
    if type(value) is not str or not contract.SHA256.fullmatch(value):
        raise HandoffError(code)
    return value


def _abs(value, code):
    if type(value) is not str or not 0 < len(value) <= 4096 or not os.path.isabs(value):
        raise HandoffError(code)
    return value


def _closed(value, keys, code):
    if type(value) is not dict or set(value) != keys:
        raise HandoffError(code)
    return value


def _version(value, code):
    return _int(value, code, 1, 2)


def _lexical_root(value, code):
    """Validate spelling without resolving a root supplied by untrusted metadata."""
    root = _abs(value, code)
    if "\0" in root or os.path.normcase(os.path.normpath(root)) != root:
        raise HandoffError(code)
    return root


def _origin(value):
    if type(value) is not dict or type(value.get("kind")) is not str:
        raise HandoffError("invalid_binding")
    if value["kind"] == "git":
        _closed(value, {"kind", "baseline_commit"}, "invalid_binding")
        commit = value["baseline_commit"]
        if type(commit) is not str or not contract.COMMIT.fullmatch(commit) \
                or commit == "0" * 40:
            raise HandoffError("invalid_binding")
    elif value["kind"] == "artifact-manifest":
        _closed(value, {"kind", "initial_sha256"}, "invalid_binding")
        _sha(value["initial_sha256"], "invalid_binding")
    else:
        raise HandoffError("invalid_binding")
    return dict(value)


def _binding(value, owned_dir, schema_version=1):
    _version(schema_version, "invalid_binding")
    if schema_version == 2:
        b = _closed(value, BINDING2_KEYS, "invalid_binding")
        if type(b["run_id"]) is not str or not contract.ID.fullmatch(b["run_id"]):
            raise HandoffError("invalid_binding")
        _lexical_root(b["root_identity"], "invalid_binding")
        for key in ("task_sha256", "envelope_sha256", "seed_sha256", "scope_sha256"):
            _sha(b[key], "invalid_binding")
        return dict(b, origin=_origin(b["origin"]))
    b = _closed(value, BINDING_KEYS, "invalid_binding")
    if type(b["run_id"]) is not str or not contract.ID.fullmatch(b["run_id"]) \
            or type(b["baseline_commit"]) is not str \
            or not contract.COMMIT.fullmatch(b["baseline_commit"]):
        raise HandoffError("invalid_binding")
    _sha(b["task_sha256"], "invalid_binding")
    root = _abs(b["root_identity"], "invalid_binding")
    if os.path.normcase(os.path.normpath(root)) != root:
        raise HandoffError("invalid_binding")
    for key in ("patch_ref", "findings_ref"):
        if b[key] is None and key == "findings_ref":
            continue
        try:  # syntax/confinement of a run-owned reference; grants nothing
            contract.validate_owned_reference(b[key])
        except contract.ContractError:
            raise HandoffError("invalid_binding") from None
    return dict(b)


def _spec(value):
    s = _closed(value, SPEC_KEYS, "invalid_spec")
    _sha(s["definition_sha256"], "invalid_spec")
    _sha(s["config_sha256"], "invalid_spec")
    if type(s["verifier_owner"]) is not str or s["verifier_owner"] not in ("worker", "coordinator"):
        raise HandoffError("invalid_spec")
    return dict(s)


def _point(value, code="invalid_record"):
    p = _closed(value, POINT_KEYS, code)
    _sha(p["candidate_sha256"], code, nullable=True)
    _int(p["mutation_seq"], code)
    return dict(p)


def _commands(plan):
    cmds = getattr(plan, "commands", None)
    if type(cmds) is not tuple or len(cmds) > 64 or not all(
            type(c) is tuple and 0 < len(c) <= 256 and all(type(a) is str for a in c) for c in cmds):
        raise HandoffError("invalid_plan")
    return cmds


def definition_sha256(plan):
    """Digest of the ordered approved check argv; the argv itself is never persisted."""
    return hashlib.sha256(contract.canonical([list(c) for c in _commands(plan)])).hexdigest()


def _evidence(value, code):
    e = _closed(value, EVIDENCE_KEYS, code)
    if e["write_mode"] not in ("disk", "stage"):
        raise HandoffError(code)
    _abs(e["root_identity"], code)
    _int(e["mutation_seq"], code)
    _bool(e["uncertain_mutation"], code)
    _bool(e["candidate_unavailable"], code)
    paths, checks = e["staged_paths"], e["checks"]
    if type(paths) is not list or len(paths) > 256 or not all(
            type(p) is str and 0 < len(p) <= 512 for p in paths) \
            or paths != sorted(set(paths)):
        raise HandoffError(code)
    if type(checks) is not list or len(checks) > 256:
        raise HandoffError(code)
    for c in checks:
        _closed(c, CHECK_KEYS, code)
        _int(c["check_id"], code, hi=63)
        _int(c["turn"], code)
        _int(c["mutation_seq"], code)
        _abs(c["root_identity"], code)
        _bool(c["mutated_candidate"], code)
        if c["outcome"] not in OUTCOMES or (c["exit_code"] is not None and (
                c["outcome"] != "exited" or _int(c["exit_code"], code, -2**31, 2**32) is None)):
            raise HandoffError(code)
    return e


def _meta(evidence):
    """Typed snapshot metadata of a RunEvidence (any module alias); argv is dropped."""
    try:
        snap = evidence.snapshot()
        meta = {k: getattr(snap, k) for k in EVIDENCE_KEYS - {"checks", "staged_paths"}}
        meta["staged_paths"] = list(snap.staged_paths)
        meta["checks"] = [{k: getattr(c, k) for k in CHECK_KEYS} for c in snap.checks]
    except (AttributeError, TypeError, ValueError):
        raise HandoffError("invalid_evidence") from None
    return _evidence(meta, "invalid_evidence")


def _record(data, owned_dir):
    r = _closed(data, TOP_KEYS, "invalid_record")
    version = _version(r["schema_version"], "invalid_record")
    try:
        _binding(r["binding"], owned_dir, schema_version=version)
        _spec(r["spec"])
    except HandoffError:
        raise HandoffError("invalid_record") from None
    _int(r["generation"], "invalid_record", 1)
    before = _point(r["before"])
    ev = _evidence(r["evidence"], "invalid_record")
    _bool(r["rerun_owed"], "invalid_record")
    if r["phase"] == "inflight":
        if r["after"] is not None or r["rerun_owed"] is not True \
                or before["mutation_seq"] != ev["mutation_seq"]:
            raise HandoffError("invalid_record")
    elif r["phase"] == "settled":
        after = _point(r["after"])
        if after["mutation_seq"] != ev["mutation_seq"] \
                or before["mutation_seq"] > after["mutation_seq"]:
            raise HandoffError("invalid_record")
    else:
        raise HandoffError("invalid_record")
    return r


def _owed(rec, plan):
    """Rerun debt from the real finish gate plus candidate facts; never cleared by a role claim."""
    cmds = _commands(plan)
    ev = rec["evidence"]
    before, after = rec["before"]["candidate_sha256"], (rec["after"] or {}).get("candidate_sha256")
    if rec["phase"] != "settled" or rec["spec"]["verifier_owner"] != "worker" or not cmds \
            or before is None or after != before or ev["uncertain_mutation"] \
            or ev["candidate_unavailable"] or ev["staged_paths"]:
        return True
    if any(c["check_id"] >= len(cmds) for c in ev["checks"]):
        raise HandoffError("invalid_record")
    snap = fg.EvidenceSnapshot(
        ev["write_mode"], ev["root_identity"], ev["mutation_seq"], ev["uncertain_mutation"],
        tuple(ev["staged_paths"]),
        tuple(fg.CheckEvidence(argv=cmds[c["check_id"]], **c) for c in ev["checks"]),
        ev["candidate_unavailable"])
    gate = fg.evaluate_finish(snap, fg.VerificationPlan(cmds))
    return gate.verification_status not in ("verified_by_command", "no_mutations") \
        or bool(gate.reason_codes or gate.missing_check_ids)


def _unsafe(st):
    return not stat.S_ISREG(st.st_mode) or getattr(st, "st_file_attributes", 0) & 0x400


def _owned(owned_dir):
    path = os.fspath(owned_dir)
    try:
        st = os.lstat(path)
    except OSError:
        raise HandoffError("owned_dir_unsafe") from None
    if not stat.S_ISDIR(st.st_mode) or getattr(st, "st_file_attributes", 0) & 0x400:
        raise HandoffError("owned_dir_unsafe")
    return os.path.realpath(path)


def _read_bytes(path, max_bytes, missing_ok=False):
    try:
        if _unsafe(os.lstat(path)):
            raise HandoffError("unsafe_file")
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_BINARY", 0))
    except FileNotFoundError:
        if missing_ok:
            return None
        raise HandoffError("missing_file") from None
    except OSError:
        raise HandoffError("unreadable_file") from None
    try:
        if _unsafe(os.fstat(fd)):
            raise HandoffError("unsafe_file")
        raw = os.read(fd, max_bytes + 1)
    except OSError:
        raise HandoffError("unreadable_file") from None
    finally:
        os.close(fd)
    return raw


def _load(base, missing_ok=False):
    raw = _read_bytes(os.path.join(base, HANDOFF_NAME), MAX_BYTES, missing_ok)
    if raw is None:
        return None, None
    try:
        data = contract.strict_json(raw, max_bytes=MAX_BYTES)
    except contract.ContractError:
        raise HandoffError("invalid_record") from None
    return _record(data, base), raw


class _Locked:
    """OS-held transaction lock (released on process death); reuses the journal lock."""

    def __init__(self, base):
        self.path = os.path.join(base, LOCK_NAME)

    def __enter__(self):
        try:
            if os.path.lexists(self.path) and _unsafe(os.lstat(self.path)):
                raise HandoffError("unsafe_file")
            self.fd = os.open(self.path, os.O_RDWR | os.O_CREAT | getattr(os, "O_BINARY", 0))
        except OSError:
            raise HandoffError("locked") from None
        try:
            journal._lock(self.fd)
        except (journal.JournalError, OSError):
            os.close(self.fd)
            raise HandoffError("locked") from None
        return self

    def __exit__(self, *exc):
        try:
            journal._unlock(self.fd)
        finally:
            os.close(self.fd)


def _publish(base, record):
    _record(record, base)
    raw = contract.canonical(record)
    if len(raw) > MAX_BYTES:
        raise HandoffError("record_too_large")
    tmp = os.path.join(base, f".{HANDOFF_NAME}.{uuid.uuid4().hex}.tmp")
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o600)
        try:
            remaining = memoryview(raw)
            while remaining:
                written = os.write(fd, remaining)
                if type(written) is not int or not 0 < written <= len(remaining):
                    raise OSError("handoff write made no valid progress")
                remaining = remaining[written:]
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(tmp, os.path.join(base, HANDOFF_NAME))
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise HandoffError("publish_failed") from None
    try:  # published but not durable: caller must not dispatch; never rolled back
        _reused_dirbarrier(base)
    except OSError:
        raise HandoffError("durability_unsupported") from None
    return hashlib.sha256(raw).hexdigest()


def _inside(root, path):
    root, path = os.path.normcase(root), os.path.normcase(path)
    try:
        return os.path.commonpath([root, path]) == root
    except ValueError:  # different drives
        return False


def _check_spec_plan(spec, plan):
    if spec["verifier_owner"] == "worker" and spec["definition_sha256"] != definition_sha256(plan):
        raise HandoffError("spec_mismatch")


def _no_artifacts_in_candidate(base, meta, *digests):
    # A known candidate digest cannot cover its own changing handoff artifacts.
    if any(d is not None for d in digests) and _inside(meta["root_identity"], base):
        raise HandoffError("artifact_inside_candidate")


def begin(owned_dir, *, binding, spec, evidence, before_sha256, schema_version=1):
    """Publish durable inflight intent; returns the record. Caller must not dispatch on error."""
    _version(schema_version, "invalid_record")
    base = _owned(owned_dir)
    binding, spec = _binding(binding, base, schema_version=schema_version), _spec(spec)
    _sha(before_sha256, "invalid_candidate", nullable=True)
    meta = _meta(evidence)
    _check_spec_plan(spec, getattr(evidence, "plan", None))
    _no_artifacts_in_candidate(base, meta, before_sha256)
    with _Locked(base):
        current, _ = _load(base, missing_ok=True)
        if current is not None and current["phase"] != "settled":
            raise HandoffError("inflight_exists")
        if current is not None and current["schema_version"] != schema_version:
            raise HandoffError("version_mismatch")
        if current is not None and current["binding"] != binding:
            raise HandoffError("binding_mismatch")
        if current is not None and current["spec"] != spec:
            raise HandoffError("spec_mismatch")
        record = {"schema_version": schema_version, "binding": binding,
                  "generation": 1 if current is None else current["generation"] + 1,
                  "spec": spec, "phase": "inflight",
                  "before": {"candidate_sha256": before_sha256,
                             "mutation_seq": meta["mutation_seq"]},
                  "after": None, "evidence": meta, "rerun_owed": True}
        _publish(base, record)
    return record


def settle(owned_dir, pending, *, evidence, after_sha256):
    """Settle the current inflight record; returns the SHA256 of the persisted bytes."""
    base = _owned(owned_dir)
    _sha(after_sha256, "invalid_candidate", nullable=True)
    meta = _meta(evidence)
    with _Locked(base):
        current, _ = _load(base, missing_ok=True)
        if current is None or current["phase"] != "inflight" or type(pending) is not dict \
                or contract.canonical(current) != contract.canonical(pending):
            raise HandoffError("stale_pending")
        old = current["evidence"]
        if meta["root_identity"] != old["root_identity"] \
                or meta["mutation_seq"] < old["mutation_seq"] \
                or (old["uncertain_mutation"] and not meta["uncertain_mutation"]) \
                or (old["candidate_unavailable"] and not meta["candidate_unavailable"]):
            raise HandoffError("evidence_regressed")
        plan = getattr(evidence, "plan", None)
        _check_spec_plan(current["spec"], plan)
        _no_artifacts_in_candidate(base, meta, current["before"]["candidate_sha256"],
                                   after_sha256)
        record = dict(current, phase="settled", evidence=meta,
                      after={"candidate_sha256": after_sha256,
                             "mutation_seq": meta["mutation_seq"]})
        record["rerun_owed"] = _owed(record, plan)
        return _publish(base, record)


def read_handoff(owned_dir):
    """Strict bounded read of the current checkpoint (atomically replaced, so no lock)."""
    record, _ = _load(_owned(owned_dir))
    return record


def collect_handoff(owned_dir, result, *, expected, approved, plan, candidate_root_identity,
                    identity_provider=None, contract_sha256=None):
    """Bind a collected completion to the handoff and re-check the live candidate digest.

    Returns (record, handoff_sha256) as an OBSERVATION only. The protected parent still owns
    ownership/quiescence guards and every acceptance predicate; this grants none of them.
    """
    base = _owned(owned_dir)
    approved = _spec(approved)
    root = _abs(candidate_root_identity, "invalid_candidate_root")
    if os.path.normcase(os.path.realpath(root)) != root:
        raise HandoffError("invalid_candidate_root")
    want = _sha(getattr(result, "completion_sha256", None), "completion_mismatch")
    raw = _read_bytes(os.path.join(base, COMPLETION_NAME), MAX_COMPLETION_BYTES)
    if len(raw) > MAX_COMPLETION_BYTES or hashlib.sha256(raw).hexdigest() != want:
        raise HandoffError("completion_mismatch")
    try:
        completion = contract.strict_json(raw, max_bytes=MAX_COMPLETION_BYTES)
    except contract.ContractError:
        raise HandoffError("completion_mismatch") from None
    if type(completion) is not dict or completion.get("recovery_profile") != PROFILE:
        raise HandoffError("completion_mismatch")
    if contract_sha256 is not None:
        _sha(contract_sha256, "completion_mismatch")
        if completion.get("recovery_contract_sha256") != contract_sha256:
            raise HandoffError("completion_mismatch")
    record, hraw = _load(base)
    version = record["schema_version"]
    expected = _binding(expected, base, schema_version=version)
    if version == 2:
        if not callable(identity_provider):
            raise HandoffError("identity_provider_required")
        if contract_sha256 is None:
            raise HandoffError("contract_pin_required")
    digest = hashlib.sha256(hraw).hexdigest()
    if completion.get("recovery_handoff_sha256") != digest:
        raise HandoffError("handoff_mismatch")
    if contract.canonical(record["binding"]) != contract.canonical(expected):
        raise HandoffError("binding_mismatch")
    if contract.canonical(record["spec"]) != contract.canonical(approved):
        raise HandoffError("spec_mismatch")
    _check_spec_plan(approved, plan)
    if record["phase"] != "settled":
        raise HandoffError("not_settled")
    ev = record["evidence"]
    if ev["root_identity"] != root or any(c["root_identity"] != root for c in ev["checks"]):
        raise HandoffError("root_mismatch")
    if record["rerun_owed"] != _owed(record, plan):
        raise HandoffError("debt_mismatch")
    after = record["after"]["candidate_sha256"]
    if after is None:
        raise HandoffError("candidate_unknown")  # unknown identity is never certified
    _no_artifacts_in_candidate(base, ev, after)
    if version == 2:
        try:
            live = _sha(identity_provider(root), "candidate_unverifiable")
        except Exception:
            raise HandoffError("candidate_unverifiable") from None
    else:
        try:
            live = evidence_mod.candidate_identity(root)
        except evidence_mod.EvidenceError:
            raise HandoffError("candidate_unverifiable") from None
    if live != after:
        raise HandoffError("stale_candidate")
    return record, digest

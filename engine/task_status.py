"""Read-only task-status projection (schema 1) over trusted v3 supervisor state.

Validation is STRUCTURAL ONLY: it is not authenticated publication and grants no trust by
role, hash, file or state label. Input must come from an operator-approved, parent-owned
source outside worker write authority; the caller owns that trusted registry and source ACL
(later integration). Projection never calls decide()/admission/cancel and never touches files.
"""
import math
import os
import stat
import tempfile
from pathlib import Path

import supervisor_contract as contract
from supervisor_state import STOP_REASONS


class TaskStatusError(ValueError):
    """Fixed-message rejection; untrusted values are never echoed."""


STATES = ("continuing", "awaiting-verification", "reconciliation", "blocked",
          "verified-complete")
REASONS = tuple(dict.fromkeys(STOP_REASONS + (
    "planner_limit", "worker_active", "planning", "next_slice_ready", "verification_due",
    "effect_uncertain", "ownership_unavailable", "initial_snapshot", "state_inconsistent",
    "verified")))
OWNERS = ("worker", "coordinator", "none")
FIELDS = ("schema_version", "orchestration_id", "envelope_sha256", "project_root",
          "owner_epoch", "state", "reason", "candidate_sha256", "seed_receipt_sha256",
          "plan_revision", "active_workers", "unresolved_workers", "pending_evaluations",
          "verifier_owner", "rerun_owed", "satisfied_criteria", "required_criteria",
          "usage", "limits")
USAGE_KEYS = ("charged", "reserved", "unknown_calls", "total_usage")
LIMIT_KEYS = tuple(contract.DEFAULT_LIMITS_V2) + ("deadline", "max_usage", "worker_turn_limit")
_MAX = 10**15


def _fail(what):
    raise TaskStatusError(f"task status {what} is invalid")


def _count(v, what, null=False):
    if (v is None and null) or (type(v) is int and 0 <= v <= _MAX):
        return v
    _fail(what)


def _sha(v, what, null=False):
    if (v is None and null) or (isinstance(v, str) and contract.SHA256.fullmatch(v)):
        return v
    _fail(what)


def _ids(v, what):
    try:
        return list(contract._ids(v, what, full=True))
    except contract.ContractError:
        _fail(what)


def _lexical(path):
    return os.path.normcase(os.path.normpath(path))


def validate_task_status(raw_or_dict, *, expected_envelope_sha256, expected_project_root):
    """Strict closed-schema check bound to the caller's trusted approval digest and root."""
    d = raw_or_dict
    if isinstance(d, str):
        d = d.encode("utf-8")
    if isinstance(d, (bytes, bytearray)):
        try:
            d = contract.strict_json(d)
        except contract.ContractError:
            _fail("encoding")
    if not isinstance(d, dict) or set(d) != set(FIELDS):
        _fail("field set")
    if type(d["schema_version"]) is not int or d["schema_version"] != 1:
        _fail("schema_version")
    if not isinstance(d["orchestration_id"], str) \
            or not contract.ID.fullmatch(d["orchestration_id"]):
        _fail("orchestration_id")
    if _sha(d["envelope_sha256"], "envelope_sha256") != expected_envelope_sha256:
        _fail("approval binding")
    root = d["project_root"]  # lexical identity only: untrusted metadata is never probed
    if not isinstance(root, str) or not isinstance(expected_project_root, str) \
            or not os.path.isabs(root) or _lexical(root) != _lexical(expected_project_root):
        _fail("project_root binding")
    _count(d["owner_epoch"], "owner_epoch")
    for key, allowed in (("state", STATES), ("reason", REASONS), ("verifier_owner", OWNERS)):
        if not isinstance(d[key], str) or d[key] not in allowed:
            _fail(key)
    _sha(d["candidate_sha256"], "candidate_sha256", null=True)
    _sha(d["seed_receipt_sha256"], "seed_receipt_sha256", null=True)
    _count(d["plan_revision"], "plan_revision", null=True)
    for key in ("active_workers", "unresolved_workers", "pending_evaluations"):
        _count(d[key], key)
    if type(d["rerun_owed"]) is not bool:
        _fail("rerun_owed")
    required = _ids(d["required_criteria"], "required_criteria")
    satisfied = _ids(d["satisfied_criteria"], "satisfied_criteria")
    if satisfied != [c for c in required if c in satisfied]:
        _fail("satisfied_criteria")
    _check_numbers(d["usage"], d["limits"])
    _check_consistency(d, satisfied == required)
    return d


_CONTINUING = ("worker_active", "planning", "next_slice_ready")
_RECONCILE = ("effect_uncertain", "ownership_unavailable", "initial_snapshot",
              "state_inconsistent")


def _check_numbers(usage, limits):
    if not isinstance(usage, dict) or set(usage) != set(USAGE_KEYS):
        _fail("usage")
    for key in USAGE_KEYS:
        _count(usage[key], "usage", null=key == "total_usage")
    if not isinstance(limits, dict) or set(limits) != set(LIMIT_KEYS):
        _fail("limits")
    for key in LIMIT_KEYS:
        v = limits[key]
        if key == "deadline":
            if not (v is None or (type(v) in (int, float) and math.isfinite(v))):
                _fail("limits")
        else:
            _count(v, "limits", null=True)


def _check_consistency(d, all_satisfied):
    state, reason = d["state"], d["reason"]
    if state == "verified-complete":
        ok = (reason == "verified" and all_satisfied and d["candidate_sha256"] is not None
              and d["verifier_owner"] == "none" and not d["rerun_owed"]
              and d["active_workers"] == d["unresolved_workers"] == d["pending_evaluations"] == 0)
    elif state == "awaiting-verification":
        ok = reason == "verification_due" and d["verifier_owner"] == "coordinator"
    elif state == "continuing":
        ok = reason in _CONTINUING
    elif state == "reconciliation":
        ok = reason in _RECONCILE
    else:  # blocked: fixed stop codes, or unresolved effects left by a published stop
        ok = reason in STOP_REASONS + ("planner_limit", "effect_uncertain")
    if not ok or (d["verifier_owner"] == "worker") != (reason == "worker_active"):
        _fail("state consistency")


def _derive(sup, accepted, required):
    """(state, reason) from durable facts only; never decide(), so nothing is written."""
    attempts = list(sup.attempts.values())
    unknown = any(a["lifecycle"] == "UNKNOWN" for a in attempts)
    pending = any(not e["done"] for e in sup.evaluations.values())
    quiet = not (unknown or pending or sup._occupied() or sup._outstanding_call()
                 or any(a["lifecycle"] != "COLLECTED" for a in attempts))
    if not sup.owner_current():
        return "reconciliation", "ownership_unavailable"
    if sup.final is not None:
        f = sup.final
        if f["outcome"] == "SUCCEEDED":
            if sup.candidate is not None and set(required) <= accepted and quiet:
                return "verified-complete", "verified"
            return "reconciliation", "state_inconsistent"
        if f["reason"] in STOP_REASONS + ("planner_limit",):
            return "blocked", f["reason"]
        if f["reason"] in ("unresolved_effects", "cancel_incomplete"):
            return "blocked", "effect_uncertain"
        return "reconciliation", "state_inconsistent"
    if sup.stop is not None:
        return "blocked", sup.stop["reason"]
    if unknown:
        return "reconciliation", "effect_uncertain"
    if pending:
        return "awaiting-verification", "verification_due"
    if sup.plan is None and not sup.calls:
        return "continuing", "planning"
    if sup.initial_receipt3 is None:
        return "reconciliation", "initial_snapshot"
    if sup._outstanding_call():
        return "continuing", "planning"
    if sup._occupied():
        return "continuing", "worker_active"
    if sup._launchable3():
        return "continuing", "next_slice_ready"
    initial = sup.snapshots3[sup.initial_receipt3]["candidate"]
    verified = any(e["done"] and e.get("accepted3") and e["candidate"] == sup.candidate
                   for e in sup.evaluations.values())
    if quiet and sup.candidate not in (None, initial) and not verified:
        return "awaiting-verification", "verification_due"
    return "continuing", "planning"  # replan / closeout / no-op diagnostic: not a new quota


def project_task_status(sup):
    """Closed schema-1 metadata from a trusted, opened schema-3 Supervisor; pure read."""
    if not getattr(sup, "_v3", False):
        _fail("source schema")
    env = sup.envelope
    required = list(env.criteria)
    accepted = set(sup.accepted)
    state, reason = _derive(sup, accepted, required)
    occupied = sup._occupied()
    t = sup.totals()
    limits = {k: env.limits.get(k) for k in contract.DEFAULT_LIMITS_V2}
    limits.update(deadline=env.deadline, max_usage=env.max_usage,
                  worker_turn_limit=env.worker_turn_limit)
    out = {
        "schema_version": 1, "orchestration_id": env.orchestration_id,
        "envelope_sha256": env.digest, "project_root": env.project_root,
        "owner_epoch": sup._journal.epoch, "state": state, "reason": reason,
        "candidate_sha256": sup.candidate, "seed_receipt_sha256": sup.seed_receipt3,
        "plan_revision": sup.plan["revision"] if sup.plan else None,
        "active_workers": sum(a["lifecycle"] != "UNKNOWN" for a in occupied),
        "unresolved_workers": sum(a["lifecycle"] == "UNKNOWN" for a in occupied),
        "pending_evaluations": sum(not e["done"] for e in sup.evaluations.values()),
        "verifier_owner": {"worker_active": "worker",
                           "verification_due": "coordinator"}.get(reason, "none"),
        "rerun_owed": reason == "verification_due",
        "satisfied_criteria": [c for c in required if c in accepted],
        "required_criteria": required,
        "usage": {k: t[k] for k in USAGE_KEYS}, "limits": limits,
    }
    return validate_task_status(out, expected_envelope_sha256=env.digest,
                                expected_project_root=env.project_root)


def encode_task_status(data):
    """Encode only validated, closed metadata; no worker text or exception content."""
    data = validate_task_status(
        data, expected_envelope_sha256=data["envelope_sha256"],
        expected_project_root=data["project_root"])
    raw = contract.canonical(data)
    if len(raw) > contract.MAX_ENVELOPE_BYTES:
        _fail("publication size")
    return raw


def publish_task_status(sup, path, *, owner_guard):
    """Publish a last parent observation at the trusted startup state-directory path."""
    def owned():
        if not owner_guard() or not sup.owner_current():
            _fail("publication owner")

    owned()
    path = Path(path)
    state_dir = getattr(getattr(sup, "_journal", None), "state_dir", None)
    if state_dir is None:
        _fail("publication path")
    expected = Path(state_dir).resolve() / "task-status.json"

    def safe_path():
        import ntpath
        if not path.is_absolute() or path != expected or path.absolute() != path.resolve() \
                or not path.parent.is_dir():
            _fail("publication path")
        parent = ntpath.normcase(str(path.parent.resolve()))
        roots = [sup.envelope.project_root]
        artifact = getattr(sup, "artifact_dir", None)
        if artifact is not None:
            roots.append(artifact)
        for root in roots:
            root = ntpath.normcase(str(Path(root).resolve()))
            try:
                overlap = ntpath.commonpath((parent, root)) in (parent, root)
            except ValueError:
                overlap = False
            if overlap:
                _fail("publication path")
        if path.exists() or path.is_symlink():
            info = path.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                _fail("publication file")

    safe_path()
    raw = encode_task_status(project_task_status(sup))
    owned()
    fd, temporary = tempfile.mkstemp(prefix=".task-status-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            owned()
            safe_path()
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        owned()
        safe_path()
        if os.name == "nt":
            import ctypes
            from ctypes import wintypes
            move = ctypes.WinDLL("kernel32", use_last_error=True).MoveFileExW
            move.argtypes = (wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD)
            move.restype = wintypes.BOOL
            if not move(temporary, str(path), 0x1 | 0x8):  # replace + write-through
                raise ctypes.WinError(ctypes.get_last_error())
        else:
            os.replace(temporary, path)
            directory = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        owned()
        safe_path()
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass

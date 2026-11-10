"""Opt-in schema-1 task metadata; structural observation, never an acceptance gate.

No engine imports, discovery, mutations or probes of paths supplied by raw JSON.
Trusted startup callers own source provenance and ACLs outside worker write authority.
"""
from __future__ import annotations

import json
import math
import os
import re
import stat
from datetime import datetime
from pathlib import Path
from typing import cast

from .models import (
    TASK_STATES, TaskLimits, TaskReason, TaskState, TaskStatusObservation,
    TaskStatusSource, TaskUsage,
)

MAX_METADATA_BYTES = 16 * 1024
# shortcut: metadata has no publication timestamp; omit files older than five minutes
# rather than imply current ownership, until the producer supplies a freshness field.
MAX_SOURCE_AGE_SECONDS = 300
_MAX = 10**15
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}")
_SHA = re.compile(r"[0-9a-f]{64}")
_STOP = (
    "cancelled", "revoked", "deadline", "no_progress", "replan_limit", "attempt_limit",
    "usage_limit", "invalid_evidence", "launch_failed", "operator_required",
)
_CONTINUING = ("worker_active", "planning", "next_slice_ready")
_RECONCILE = ("effect_uncertain", "ownership_unavailable", "initial_snapshot", "state_inconsistent")
_REASONS = _STOP + ("planner_limit",) + _CONTINUING + ("verification_due",) + _RECONCILE + ("verified",)
_OWNERS = ("worker", "coordinator", "none")
_USAGE = ("charged", "reserved", "unknown_calls", "total_usage")
_LIMITS = (
    "max_replans", "max_total_attempts", "max_concurrent_workers", "max_planner_calls",
    "max_final_report_calls", "stop_grace_seconds", "deadline", "max_usage", "worker_turn_limit",
)
_FIELDS = (
    "schema_version", "orchestration_id", "envelope_sha256", "project_root", "owner_epoch",
    "state", "reason", "candidate_sha256", "seed_receipt_sha256", "plan_revision",
    "active_workers", "unresolved_workers", "pending_evaluations", "verifier_owner",
    "rerun_owed", "satisfied_criteria", "required_criteria", "usage", "limits",
)


class TaskStatusError(ValueError):
    """Fixed-message rejection; never disclose untrusted content or source paths."""


def _fail():
    raise TaskStatusError("task status metadata is invalid")


def _count(value, *, nullable=False):
    if not ((value is None and nullable) or (type(value) is int and 0 <= value <= _MAX)):
        _fail()


def _match(pattern, value, *, nullable=False):
    if not ((value is None and nullable) or (isinstance(value, str) and pattern.fullmatch(value))):
        _fail()


def _ids(value):
    if (not isinstance(value, list) or len(value) > 32
            or any(not isinstance(v, str) or not _ID.fullmatch(v) for v in value)
            or len(set(value)) != len(value)):
        _fail()
    return tuple(value)


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            _fail()
        result[key] = value
    return result


def _constant(_value):
    _fail()


def lexical_root(root: str) -> str:
    """Identity only; never resolve or stat a root taken from metadata."""
    return os.path.normcase(os.path.normpath(root))


def parse_task_status(raw, source: TaskStatusSource) -> TaskStatusObservation:
    """Closed producer schema 1, bound to explicitly trusted root and approval pins.

    A valid verified-complete observation is not independent acceptance evidence or
    proof that its publisher still holds the current lease.
    """
    if not isinstance(source, TaskStatusSource):
        _fail()
    _match(_ID, source.project_id)
    _match(_SHA, source.envelope_sha256)
    if not isinstance(source.project_root, str) or not os.path.isabs(source.project_root):
        _fail()
    try:
        if isinstance(raw, str):
            raw = raw.encode("utf-8")
        if isinstance(raw, (bytes, bytearray)):
            if len(raw) > MAX_METADATA_BYTES:
                _fail()
            raw = json.loads(bytes(raw).decode("utf-8"), object_pairs_hook=_pairs,
                             parse_constant=_constant)
    except (UnicodeError, ValueError, RecursionError):
        raise TaskStatusError("task status metadata is invalid") from None
    d = raw
    if not isinstance(d, dict) or set(d) != set(_FIELDS):
        _fail()
    if type(d["schema_version"]) is not int or d["schema_version"] != 1:
        _fail()
    _match(_ID, d["orchestration_id"])
    _match(_SHA, d["envelope_sha256"])
    if d["envelope_sha256"] != source.envelope_sha256:
        _fail()
    root = d["project_root"]
    if (not isinstance(root, str) or not os.path.isabs(root)
            or lexical_root(root) != lexical_root(source.project_root)):
        _fail()
    _count(d["owner_epoch"])
    for key, allowed in (("state", TASK_STATES), ("reason", _REASONS), ("verifier_owner", _OWNERS)):
        if not isinstance(d[key], str) or d[key] not in allowed:
            _fail()
    for key in ("candidate_sha256", "seed_receipt_sha256"):
        _match(_SHA, d[key], nullable=True)
    _count(d["plan_revision"], nullable=True)
    for key in ("active_workers", "unresolved_workers", "pending_evaluations"):
        _count(d[key])
    if type(d["rerun_owed"]) is not bool:
        _fail()
    required, satisfied = _ids(d["required_criteria"]), _ids(d["satisfied_criteria"])
    if satisfied != tuple(c for c in required if c in satisfied):
        _fail()
    usage, limits = d["usage"], d["limits"]
    if not isinstance(usage, dict) or set(usage) != set(_USAGE):
        _fail()
    for key in _USAGE:
        _count(usage[key], nullable=key == "total_usage")
    if not isinstance(limits, dict) or set(limits) != set(_LIMITS):
        _fail()
    for key in _LIMITS:
        value = limits[key]
        if key == "deadline":
            if value is not None and not (type(value) in (int, float) and math.isfinite(value)):
                _fail()
        else:
            _count(value, nullable=True)
    state, reason = d["state"], d["reason"]
    if state == "verified-complete":
        consistent = (reason == "verified" and satisfied == required
                      and d["candidate_sha256"] is not None and d["verifier_owner"] == "none"
                      and not d["rerun_owed"]
                      and d["active_workers"] == d["unresolved_workers"] == d["pending_evaluations"] == 0)
    elif state == "awaiting-verification":
        consistent = reason == "verification_due" and d["verifier_owner"] == "coordinator"
    elif state == "continuing":
        consistent = reason in _CONTINUING
    elif state == "reconciliation":
        consistent = reason in _RECONCILE
    else:
        consistent = reason in _STOP + ("planner_limit", "effect_uncertain")
    if not consistent or (d["verifier_owner"] == "worker") != (reason == "worker_active"):
        _fail()
    return TaskStatusObservation(
        project_id=source.project_id, schema_version=1,
        orchestration_id=d["orchestration_id"], envelope_sha256=d["envelope_sha256"],
        owner_epoch=d["owner_epoch"], state=cast(TaskState, state), reason=cast(TaskReason, reason),
        candidate_sha256=d["candidate_sha256"], seed_receipt_sha256=d["seed_receipt_sha256"],
        plan_revision=d["plan_revision"], active_workers=d["active_workers"],
        unresolved_workers=d["unresolved_workers"], pending_evaluations=d["pending_evaluations"],
        verifier_owner=d["verifier_owner"], rerun_owed=d["rerun_owed"],
        satisfied_criteria=satisfied, required_criteria=required,
        usage=TaskUsage(**usage), limits=TaskLimits(**limits),
    )


def _plain(info, *, directory=False):
    return (not getattr(info, "st_file_attributes", 0) & 0x400
            and (stat.S_ISDIR(info.st_mode) if directory else
                 stat.S_ISREG(info.st_mode) and info.st_nlink == 1))


def _identity(info):
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns,
            info.st_mode, info.st_nlink)


def read_task_status(source: TaskStatusSource, *, now: datetime) -> TaskStatusObservation | None:
    """Read only the registered absolute fixed file; fail closed without raw errors.

    Reject linked/reparse components, aliases, in-project paths and changed files.
    Structural checks do not establish ownership/ACLs: the startup caller must.
    """
    try:
        path = Path(source.path)
        if (not path.is_absolute() or path.name != "task-status.json" or ".." in path.parts
                or not os.path.isabs(source.project_root)):
            return None
        # Check the trusted path only. No raw metadata path ever reaches filesystem APIs.
        if os.path.normcase(os.path.realpath(path)) != os.path.normcase(str(path)):
            return None
        if path.is_relative_to(Path(source.project_root)):
            return None
        for parent in reversed(path.parents):
            if not _plain(os.lstat(parent), directory=True):
                return None
        before = os.lstat(path)
        if (not _plain(before) or before.st_size > MAX_METADATA_BYTES
                or not 0 <= now.timestamp() - before.st_mtime <= MAX_SOURCE_AGE_SECONDS):
            return None
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(path, flags)
        with os.fdopen(fd, "rb") as stream:
            opened = os.fstat(stream.fileno())
            path_stamp, handle_stamp = _identity(before), _identity(opened)
            # Windows path-stat and handle-stat expose different ctime values.
            # Compare shared identity across APIs and retain both full stamps
            # for before/after comparison within each API.
            if (not _plain(opened)
                    or path_stamp[:4] + path_stamp[5:] != handle_stamp[:4] + handle_stamp[5:]):
                return None
            raw = stream.read(MAX_METADATA_BYTES + 1)
            after = os.fstat(stream.fileno())
        final_path = os.lstat(path)
        if (len(raw) > MAX_METADATA_BYTES or not _plain(after) or not _plain(final_path)
                or handle_stamp != _identity(after) or path_stamp != _identity(final_path)):
            return None
        return parse_task_status(raw, source)
    except (OSError, ValueError, TypeError, OverflowError, RecursionError):
        return None

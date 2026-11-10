"""Schema-3 approval codec: v2 common authority fields plus a tagged candidate descriptor.

Independent of supervisor_contract.load_envelope, which keeps rejecting schema 3.
Only typed metadata and digests; filesystem capture/overlap checks belong to the runtime.
"""
import hashlib
import json
import math
import os
import re
from dataclasses import dataclass
from types import MappingProxyType

from supervisor_contract import (COMMIT, ENVELOPE_KEYS, ID, MAX_COUNT_V2, MAX_ENVELOPE_BYTES,
                                 MAX_USAGE_CEILING, SHA256, ContractError, _capabilities, _ids,
                                 _int, _limits_v2, _match, canonical, canonical_root,
                                 validate_owned_reference)

ENVELOPE_KEYS_V3 = (ENVELOPE_KEYS - {"baseline_commit"}) | {"candidate", "continuation"}
GIT_KEYS = frozenset({"kind", "baseline_commit", "initial_sha256", "inputs", "bounds"})
ARTIFACT_KEYS = GIT_KEYS - {"baseline_commit"}
BOUNDS_KEYS = ("max_entries", "max_file_bytes", "max_total_bytes", "max_manifest_bytes",
               "max_path_bytes", "max_seconds")
CONTINUATION = MappingProxyType({"profile": "resilient-v1", "admission": "approved-task"})
MAX_BYTES_V3 = 2**53 - 1  # exact JSON/journal integer range; larger fails closed
MAX_PATH_BYTES_V3 = 32767  # Windows long-path ceiling
MAX_SECONDS_V3 = 86400.0
# Approval-frame bytes and the explicit traversal bound limit work, not an invented quota.
MAX_INPUTS_V3 = MAX_ENVELOPE_BYTES
RESERVED = re.compile(r"^(CON|PRN|AUX|NUL|COM[0-9]|LPT[0-9])$", re.IGNORECASE)


@dataclass(frozen=True)
class GitCandidate:
    kind: str
    baseline_commit: str
    initial_sha256: str
    inputs: tuple
    bounds: MappingProxyType


@dataclass(frozen=True)
class ArtifactCandidate:
    kind: str
    initial_sha256: str
    inputs: tuple
    bounds: MappingProxyType

    @property
    def baseline_commit(self):
        raise ContractError("artifact-manifest candidate has no Git baseline")


@dataclass(frozen=True)
class Continuation:
    profile: str
    admission: str


@dataclass(frozen=True)
class GoalEnvelope3:
    digest: str
    orchestration_id: str
    project_root: str
    goal_digest: str
    acceptance_digest: str
    policy_digest: str
    validator_digest: str
    models: tuple
    capabilities: MappingProxyType
    cancel_authority: bool
    limits: MappingProxyType
    deadline: float | None
    worker_turn_limit: int
    verification_timeout_seconds: int
    criteria: tuple
    check_ids: tuple
    hypothesis_ids: tuple
    max_usage: int | None
    candidate: GitCandidate | ArtifactCandidate
    continuation: Continuation
    raw: bytes  # validated canonical approval bytes; digest = sha256(raw)
    schema_version: int = 3

    @property
    def baseline_commit(self):
        """Git-only; artifact candidates fail closed so legacy Git paths cannot proceed."""
        return self.candidate.baseline_commit


def _inputs(value):
    """Sorted unique normalized relative data paths; '.' (whole scope) only when alone."""
    if not isinstance(value, list) or not 1 <= len(value) <= MAX_INPUTS_V3 \
            or not all(isinstance(v, str) for v in value) or value != sorted(set(value)):
        raise ContractError("candidate.inputs must be a sorted unique non-empty list")
    if value == ["."]:
        return (".",)
    for item in value:
        validate_owned_reference(item)
        for part in item.split("/"):
            if RESERVED.match(part.split(".")[0]) or part[-1] in ". " \
                    or any(ord(c) < 32 or c in '<>"|?*' for c in part):
                raise ContractError("candidate.inputs contains a reserved or malformed name")
    return tuple(value)


def _bounds(value):
    if not isinstance(value, dict) or set(value) != set(BOUNDS_KEYS):
        raise ContractError("candidate.bounds keys must match the allowlisted schema exactly")
    out = {"max_entries": _int(value["max_entries"], 1, MAX_COUNT_V2, "bounds.max_entries"),
           "max_path_bytes": _int(value["max_path_bytes"], 1, MAX_PATH_BYTES_V3,
                                  "bounds.max_path_bytes")}
    for key in ("max_file_bytes", "max_total_bytes", "max_manifest_bytes"):
        out[key] = _int(value[key], 1, MAX_BYTES_V3, f"bounds.{key}")
    if out["max_file_bytes"] > out["max_total_bytes"]:
        raise ContractError("file bound must not exceed max_total_bytes")
    secs = value["max_seconds"]
    if type(secs) not in (int, float) or not math.isfinite(secs) \
            or not 0 < secs <= MAX_SECONDS_V3:
        raise ContractError("bounds.max_seconds must be finite and positive")
    out["max_seconds"] = secs
    return MappingProxyType({k: out[k] for k in BOUNDS_KEYS})


def _candidate(value):
    kind = value.get("kind") if isinstance(value, dict) else None
    keys = {"git": GIT_KEYS, "artifact-manifest": ARTIFACT_KEYS}.get(kind) \
        if isinstance(kind, str) else None
    if keys is None or set(value) != keys:
        raise ContractError("candidate must be an exact git or artifact-manifest descriptor")
    bounds = _bounds(value["bounds"])
    inputs = _inputs(value["inputs"])
    if len(inputs) > bounds["max_entries"]:
        raise ContractError("candidate inputs exceed declared traversal work")
    common = dict(kind=kind,
                   initial_sha256=_match(SHA256, value["initial_sha256"], "initial_sha256", True),
                   inputs=inputs, bounds=bounds)
    if kind == "artifact-manifest":
        return ArtifactCandidate(**common)
    commit = _match(COMMIT, value["baseline_commit"], "baseline_commit", True)
    if commit == "0" * 40:
        raise ContractError("baseline_commit must be a real commit")
    return GitCandidate(baseline_commit=commit, **common)


def load_envelope3(data, project_root=None):
    """Validate schema-3 approval data; optional trusted root must match project_root."""
    try:
        raw = canonical(data)
    except (TypeError, ValueError) as exc:
        raise ContractError("envelope must be finite JSON data") from exc
    if len(raw) > MAX_ENVELOPE_BYTES:
        raise ContractError("envelope exceeds the size bound")
    data = json.loads(raw)  # private deep copy: later caller mutation cannot reach us
    if not isinstance(data, dict) or set(data) != ENVELOPE_KEYS_V3:
        raise ContractError("envelope keys must match the allowlisted schema exactly")
    _int(data["schema_version"], 3, 3, "schema_version")
    root = data["project_root"]
    if not isinstance(root, str) or not os.path.isabs(root) or (
            project_root is not None and canonical_root(root) != canonical_root(project_root)):
        raise ContractError("project_root must be the absolute policy-owning root")
    deadline = data["deadline"]
    if deadline is not None and (type(deadline) not in (int, float)
                                 or not math.isfinite(deadline) or deadline <= 0):
        raise ContractError("deadline must be finite")
    max_usage = data["max_usage"]
    if max_usage is not None:
        _int(max_usage, 1, MAX_USAGE_CEILING, "max_usage")
    if type(data["cancel_authority"]) is not bool:
        raise ContractError("cancel_authority must be boolean")
    if data["continuation"] != dict(CONTINUATION):
        raise ContractError("continuation must be the approved resilient-v1 profile")
    return GoalEnvelope3(
        digest=hashlib.sha256(raw).hexdigest(),
        orchestration_id=_match(ID, data["orchestration_id"], "orchestration_id", True),
        project_root=canonical_root(root),
        goal_digest=_match(SHA256, data["goal_digest"], "goal_digest", True),
        acceptance_digest=_match(SHA256, data["acceptance_digest"], "acceptance_digest", True),
        policy_digest=_match(SHA256, data["policy_digest"], "policy_digest", True),
        validator_digest=_match(SHA256, data["validator_digest"], "validator_digest", True),
        models=_ids(data["models"], "models", 8, nonempty=True, full=True),
        capabilities=_capabilities(data["capabilities"], full=True),
        cancel_authority=data["cancel_authority"],
        limits=_limits_v2(data["limits"]),
        deadline=None if deadline is None else float(deadline),
        worker_turn_limit=_int(data["worker_turn_limit"], 1, 1000, "worker_turn_limit"),
        verification_timeout_seconds=_int(data["verification_timeout_seconds"], 1, 3600,
                                          "verification_timeout_seconds"),
        criteria=_ids(data["criteria"], "criteria", nonempty=True, full=True),
        check_ids=_ids(data["check_ids"], "check_ids", nonempty=True, full=True),
        hypothesis_ids=_ids(data["hypothesis_ids"], "hypothesis_ids", full=True),
        max_usage=max_usage,
        candidate=_candidate(data["candidate"]),
        continuation=Continuation(**CONTINUATION),
        raw=raw,
    )


def dump_envelope3(envelope):
    """Return a fresh copy of the validated canonical data (no defaults added)."""
    return json.loads(envelope.raw)


def snapshot_context3(envelope_sha256, *, seed_receipt, source_attempts, kind):
    """Bind DATA transport to approved, ordered producing lineage, not acceptance."""
    envelope_sha256 = _match(SHA256, envelope_sha256, "envelope_sha256", True)
    if not isinstance(source_attempts, (list, tuple)):
        raise ContractError("source_attempts must be an ordered list of unique IDs")
    sources = _ids(list(source_attempts), "source_attempts", len(source_attempts), full=True)
    if seed_receipt is not None:
        seed_receipt = _match(SHA256, seed_receipt, "seed_receipt", True)
    if kind not in ("initial", "worker", "merge") or (
            kind == "initial" and (seed_receipt is not None or sources)) or (
            kind != "initial" and (seed_receipt is None or not sources)) or (
            kind == "worker" and len(sources) != 1):
        raise ContractError("snapshot kind, seed receipt and sources must agree")
    return hashlib.sha256(canonical({
        "envelope_sha256": envelope_sha256, "seed_receipt_sha256": seed_receipt,
        "source_attempts": list(sources), "kind": kind,
    })).hexdigest()

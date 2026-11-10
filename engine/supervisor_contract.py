"""Strict goal-supervisor contract: approved envelope and planner proposal schemas.

Only allowlisted metadata is accepted; content-bearing prose stays in run-owned artifacts.
"""
import hashlib
import json
import math
import os
import re
from dataclasses import dataclass
from types import MappingProxyType

LIMITS = MappingProxyType({
    "max_replans": 2,
    "max_total_attempts": 6,
    "max_concurrent_workers": 2,
    "max_planner_calls": 3,
    "max_final_report_calls": 1,
    "stop_grace_seconds": 60,
})
SHA256 = re.compile(r"^[0-9a-f]{64}$")
COMMIT = re.compile(r"^[0-9a-f]{40}$")
ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
MAX_ENVELOPE_BYTES = 64 * 1024
# Version 2: null means no lifetime quota; counts are still recorded by consumers.
DEFAULT_LIMITS_V2 = MappingProxyType({
    "max_replans": None,
    "max_total_attempts": None,
    "max_concurrent_workers": 8,
    "max_planner_calls": None,
    "max_final_report_calls": 1,
    "stop_grace_seconds": 60,
})
# Per-request/frame technical bounds, not lifetime goal quotas.
MAX_PLAN_TASKS_V2 = 64
MAX_PROPOSAL_BYTES_V2 = 128 * 1024
MAX_RECORD_BYTES_V2 = 256 * 1024
MAX_COUNT_V2 = 10**9  # technical integer bound for explicit v2 caps and revisions
MAX_WORKERS_V2 = 1024  # technical bound on configurable concurrency


class ContractError(ValueError):
    """Input violates the approved supervisor contract."""


def _reject_constant(name):
    raise ContractError(f"non-finite JSON constant: {name}")


def _no_duplicates(pairs):
    out = {}
    for key, value in pairs:
        if key in out:
            raise ContractError("duplicate JSON key")  # never echo untrusted key text
        out[key] = value
    return out


def strict_json(raw, *, max_bytes=MAX_ENVELOPE_BYTES):
    """Parse bytes as JSON rejecting duplicate keys and NaN/Infinity."""
    if type(max_bytes) is not int or max_bytes <= 0:
        raise ContractError("max_bytes must be a positive integer")
    if not isinstance(raw, (bytes, bytearray)) or len(raw) > max_bytes:
        raise ContractError("expected bounded JSON bytes")
    try:
        return json.loads(bytes(raw).decode("utf-8"), object_pairs_hook=_no_duplicates,
                          parse_constant=_reject_constant)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ContractError("malformed JSON") from exc


def canonical(data):
    return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
                      allow_nan=False).encode()


def _int(value, lo, hi, name):
    if type(value) is not int or not lo <= value <= hi:
        raise ContractError(f"{name} must be an integer in [{lo}, {hi}]")
    return value


def _ids(value, name, limit=32, nonempty=False, full=False):
    # Item types are checked before set() so unhashable JSON items raise ContractError.
    if not isinstance(value, list) or not (1 if nonempty else 0) <= len(value) <= limit \
            or not all(isinstance(v, str) and (ID.fullmatch if full else ID.match)(v)
                       for v in value) \
            or len(set(value)) != len(value):
        raise ContractError(f"{name} must be a list of unique bounded IDs")
    return tuple(value)


def _match(pattern, value, name, full=False):
    # v2 uses fullmatch ("$" alone admits a trailing newline); v1 keeps its original matching.
    if not isinstance(value, str) or not (pattern.fullmatch if full else pattern.match)(value):
        raise ContractError(f"{name} is malformed")
    return value


ENVELOPE_KEYS = frozenset({
    "schema_version", "orchestration_id", "project_root", "goal_digest", "acceptance_digest",
    "policy_digest", "validator_digest", "baseline_commit", "models", "capabilities",
    "cancel_authority", "limits", "deadline", "worker_turn_limit",
    "verification_timeout_seconds", "criteria", "check_ids", "hypothesis_ids", "max_usage",
})
MAX_USAGE_CEILING = 10**12  # operator token-usage ceiling bound; an estimate, not provider spend
CAPABILITY_KEYS = frozenset({"write", "commands", "mcp"})
CAP_ITEM = re.compile(r"^[A-Za-z0-9_./:-]{1,128}$")


@dataclass(frozen=True)
class GoalEnvelope:
    digest: str
    orchestration_id: str
    project_root: str
    goal_digest: str
    acceptance_digest: str
    policy_digest: str
    validator_digest: str
    baseline_commit: str
    models: tuple
    capabilities: MappingProxyType
    cancel_authority: bool
    limits: MappingProxyType
    deadline: float | None  # None only in schema 2: no goal deadline chosen
    worker_turn_limit: int
    verification_timeout_seconds: int
    criteria: tuple
    check_ids: tuple
    hypothesis_ids: tuple
    max_usage: int | None  # None only in schema 2: no aggregate usage ceiling chosen
    schema_version: int = 1  # appended last so direct legacy construction is unchanged


def canonical_root(path):
    return os.path.normcase(os.path.realpath(os.fspath(path)))


def _capabilities(value, full=False):
    if not isinstance(value, dict) or set(value) != CAPABILITY_KEYS:
        raise ContractError("capabilities must name exactly write/commands/mcp")
    match = CAP_ITEM.fullmatch if full else CAP_ITEM.match
    out = {}
    for key in sorted(CAPABILITY_KEYS):
        items = value[key]
        if not isinstance(items, list) or len(items) > 64 \
                or not all(isinstance(i, str) and match(i) for i in items) \
                or len(set(items)) != len(items):
            raise ContractError(f"capabilities.{key} malformed")
        if key == "write" and any(i.startswith(("/", "\\")) or ":" in i or ".." in i.split("/")
                                  for i in items):
            raise ContractError("write capability must be a relative in-root path")
        out[key] = tuple(items)
    return MappingProxyType(out)


def load_envelope(raw, approved_digest, project_root):
    """Validate operator-approved envelope bytes against the trusted digest and root."""
    _match(SHA256, approved_digest, "approval digest")
    if not isinstance(raw, (bytes, bytearray)) or hashlib.sha256(raw).hexdigest() != approved_digest:
        raise ContractError("envelope bytes do not match the operator approval digest")
    data = strict_json(raw)
    if not isinstance(data, dict) or set(data) != ENVELOPE_KEYS:
        raise ContractError("envelope keys must match the allowlisted schema exactly")
    version = _int(data["schema_version"], 1, 2, "schema_version")
    v2 = version == 2
    if v2:
        limits = _limits_v2(data["limits"])
    else:
        limits = data["limits"]
        if not isinstance(limits, dict) or set(limits) != set(LIMITS) or any(
                type(limits[k]) is not int or limits[k] != v for k, v in LIMITS.items()):
            raise ContractError("limits must equal the fixed approved ceilings")
        limits = LIMITS
    root = data["project_root"]
    if not isinstance(root, str) or not os.path.isabs(root) \
            or canonical_root(root) != canonical_root(project_root):
        raise ContractError("project_root does not match the canonical policy-owning root")
    deadline = data["deadline"]
    if not (v2 and deadline is None) and (type(deadline) not in (int, float)
                                          or not math.isfinite(deadline) or deadline <= 0):
        raise ContractError("deadline must be finite")
    max_usage = data["max_usage"]
    if not (v2 and max_usage is None):
        max_usage = _int(max_usage, 1, MAX_USAGE_CEILING, "max_usage")
    if type(data["cancel_authority"]) is not bool:
        raise ContractError("cancel_authority must be boolean")
    return GoalEnvelope(
        digest=approved_digest,
        orchestration_id=_match(ID, data["orchestration_id"], "orchestration_id", v2),
        project_root=canonical_root(root),
        goal_digest=_match(SHA256, data["goal_digest"], "goal_digest", v2),
        acceptance_digest=_match(SHA256, data["acceptance_digest"], "acceptance_digest", v2),
        policy_digest=_match(SHA256, data["policy_digest"], "policy_digest", v2),
        validator_digest=_match(SHA256, data["validator_digest"], "validator_digest", v2),
        baseline_commit=_match(COMMIT, data["baseline_commit"], "baseline_commit", v2),
        models=_ids(data["models"], "models", 8, nonempty=True, full=v2),
        capabilities=_capabilities(data["capabilities"], full=v2),
        cancel_authority=data["cancel_authority"],
        limits=limits,
        deadline=None if deadline is None else float(deadline),
        worker_turn_limit=_int(data["worker_turn_limit"], 1, 1000, "worker_turn_limit"),
        verification_timeout_seconds=_int(data["verification_timeout_seconds"], 1, 3600,
                                          "verification_timeout_seconds"),
        criteria=_ids(data["criteria"], "criteria", nonempty=True, full=v2),
        check_ids=_ids(data["check_ids"], "check_ids", nonempty=True, full=v2),
        hypothesis_ids=_ids(data["hypothesis_ids"], "hypothesis_ids", full=v2),
        max_usage=max_usage,
        schema_version=version,
    )


def _limits_v2(value):
    """Validate an explicit approved v2 limits dict; nothing is injected or defaulted here."""
    if not isinstance(value, dict) or set(value) != set(DEFAULT_LIMITS_V2):
        raise ContractError("limits keys must match the allowlisted schema exactly")
    for key, lo in (("max_replans", 0), ("max_total_attempts", 1), ("max_planner_calls", 1)):
        if value[key] is not None:
            _int(value[key], lo, MAX_COUNT_V2, f"limits.{key}")
    _int(value["max_concurrent_workers"], 1, MAX_WORKERS_V2, "limits.max_concurrent_workers")
    for key in ("max_final_report_calls", "stop_grace_seconds"):
        if type(value[key]) is not int or value[key] != DEFAULT_LIMITS_V2[key]:
            raise ContractError("limits must equal the fixed approved ceilings")
    return MappingProxyType({k: value[k] for k in DEFAULT_LIMITS_V2})


PROPOSAL_KEYS = frozenset({"schema_version", "plan_revision", "tasks"})
TASK_KEYS = frozenset({"task_id", "depends_on", "objective_digest", "criteria", "hypothesis_ids"})


@dataclass(frozen=True)
class Task:
    task_id: str
    depends_on: tuple
    objective_digest: str  # objective prose lives in a run-owned artifact
    criteria: tuple
    hypothesis_ids: tuple


@dataclass(frozen=True)
class Plan:
    revision: int
    tasks: tuple


def _acyclic(tasks):
    deps = {t.task_id: t.depends_on for t in tasks}
    done, visiting = set(), set()

    def visit(node):
        if node in done:
            return
        if node in visiting:
            raise ContractError("task dependencies are cyclic")
        visiting.add(node)
        for dep in deps[node]:
            visit(dep)
        visiting.discard(node)
        done.add(node)

    for node in deps:
        visit(node)


def validate_proposal(raw, envelope, expected_revision):
    """Validate untrusted planner output: task graph only, never authority fields."""
    v2 = envelope.schema_version in (2, 3)
    data = strict_json(raw, max_bytes=MAX_PROPOSAL_BYTES_V2) if v2 else strict_json(raw)
    if not isinstance(data, dict) or set(data) != PROPOSAL_KEYS:
        raise ContractError("proposal keys must match the allowlisted schema exactly")
    _int(data["schema_version"], 1, 1, "schema_version")
    max_rev = envelope.limits["max_replans"]
    if _int(data["plan_revision"], 0, MAX_COUNT_V2 if max_rev is None else max_rev,
            "plan_revision") != expected_revision:
        raise ContractError("stale or future plan revision")
    raw_tasks = data["tasks"]
    if not isinstance(raw_tasks, list) or not 1 <= len(raw_tasks) <= proposal_task_limit(envelope):
        raise ContractError("tasks must be a bounded non-empty list")
    tasks = []
    for item in raw_tasks:
        if not isinstance(item, dict) or set(item) != TASK_KEYS:
            raise ContractError("task keys must match the allowlisted schema exactly")
        criteria = _ids(item["criteria"], "criteria", full=v2)
        hyps = _ids(item["hypothesis_ids"], "hypothesis_ids", full=v2)
        if (not criteria and envelope.schema_version != 3) \
                or not set(criteria) <= set(envelope.criteria):
            raise ContractError("task criteria must be approved envelope criteria")
        if not set(hyps) <= set(envelope.hypothesis_ids):
            raise ContractError("hypotheses must be operator-registered IDs")
        tasks.append(Task(_match(ID, item["task_id"], "task_id", v2),
                          _ids(item["depends_on"], "depends_on", full=v2),
                          _match(SHA256, item["objective_digest"], "objective_digest", v2),
                          criteria, hyps))
    ids = [t.task_id for t in tasks]
    if len(set(ids)) != len(ids):
        raise ContractError("duplicate task IDs")
    if any(not set(t.depends_on) <= set(ids) for t in tasks):
        raise ContractError("unknown task dependency")
    _acyclic(tasks)
    return Plan(expected_revision, tuple(tasks))


def proposal_task_limit(envelope):
    """Max tasks per proposal: v1 keeps 6; v2 is 64, or a smaller explicit attempt ceiling."""
    if envelope.schema_version not in (2, 3):
        return envelope.limits["max_total_attempts"]
    cap = envelope.limits["max_total_attempts"]
    return MAX_PLAN_TASKS_V2 if cap is None else min(cap, MAX_PLAN_TASKS_V2)


def limit_reached(envelope, key, used):
    """True once `used` meets a finite cap; a null (v2) cap is never reached."""
    if key not in envelope.limits:
        raise ContractError("unknown limit key")
    _int(used, 0, MAX_COUNT_V2, "used")
    cap = envelope.limits[key]
    return cap is not None and used >= cap


def deadline_passed(envelope, now):
    """True at/after a chosen deadline; a null (v2) deadline never passes."""
    if type(now) not in (int, float) or not math.isfinite(now):
        raise ContractError("now must be finite")
    return envelope.deadline is not None and now >= envelope.deadline


def usage_available(envelope, charged, reserved, reserve):
    """Can `reserve` more be admitted? Reserve must be positive; null max_usage has no ceiling."""
    _int(reserve, 1, MAX_USAGE_CEILING, "reserve")
    _int(charged, 0, 10**18, "charged")
    _int(reserved, 0, 10**18, "reserved")
    return envelope.max_usage is None or charged + reserved + reserve <= envelope.max_usage


def validate_owned_reference(relative):
    """Validate reference syntax without probing any untrusted filesystem path."""
    if not isinstance(relative, str) or not relative or len(relative) > 512 \
            or "\\" in relative or ":" in relative or relative.startswith("/") \
            or any(part in ("", ".", "..") for part in relative.split("/")):
        raise ContractError("owned reference must be a normalized relative path")
    return relative


def resolve_owned(owned_dir, relative):
    """Resolve a reference strictly inside an owned directory, following no escaping links."""
    validate_owned_reference(relative)
    base = os.path.realpath(os.fspath(owned_dir))
    target = os.path.realpath(os.path.join(base, *relative.split("/")))
    if os.path.commonpath([os.path.normcase(base), os.path.normcase(target)]) != os.path.normcase(base):
        raise ContractError("owned reference escapes its directory")
    from pathlib import Path
    return Path(target)

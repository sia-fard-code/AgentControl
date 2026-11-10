"""Deterministic goal-supervisor state over the owner-checked journal.

The supervisor performs no subprocess or model effects: every admission is durably journaled
first and only then returned as Effect metadata for a driver to execute.
"""
import hashlib
import os
import uuid
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

import supervisor_contract as contract
import supervisor_journal as sj
from supervisor_journal import (boolean, digest, integer, number, one_of, optional_int,
                                optional_token, token, tokens)

LIFECYCLES = ("SPAWN_PENDING", "RUNNING", "TERMINAL", "COLLECTED", "UNKNOWN")
OUTCOMES = ("succeeded", "failed", "rejected", "cancelled")
STOP_REASONS = ("cancelled", "revoked", "deadline", "no_progress", "replan_limit", "attempt_limit",
                "usage_limit", "invalid_evidence", "launch_failed", "operator_required")
EVIDENCE_KINDS = ("candidate", "check_failure", "hypothesis_falsified")
FAILURE_CODES = ("assert_failed", "timeout", "launch_failed", "unclassified_failure",
                 "conflict", "validator_changed", "candidate_mutated", "output_limit",
                 "containment_unknown")
GRACE = contract.LIMITS["stop_grace_seconds"]


def _opt(check):
    return lambda v: v if v is None else check(v)


_TASK_FIELDS = {"task_id": token, "depends_on": tokens, "objective_digest": digest,
                "criteria": tokens, "hypothesis_ids": tokens}


def _task_list(v):
    """Strict nested allowlist for durable task bindings; semantics are checked on apply."""
    if not isinstance(v, list) or not 1 <= len(v) <= contract.LIMITS["max_total_attempts"]:
        raise sj.JournalError("plan tasks are not a bounded list")
    for item in v:
        if not isinstance(item, dict) or set(item) != set(_TASK_FIELDS):
            raise sj.JournalError("plan task does not match the allowlisted schema")
        for key, check in _TASK_FIELDS.items():
            check(item[key])
    return v


def _proposal(revision, tasks):
    """Canonical proposal bytes rebuilt from durable task bindings (digest and re-validation)."""
    return contract.canonical({"schema_version": 1, "plan_revision": revision, "tasks": tasks})


def _reference(value):
    if not isinstance(value, str) or not value or len(value) > 512 or "\\" in value \
            or ":" in value or PurePosixPath(value).is_absolute() \
            or any(p in ("", ".", "..") for p in value.split("/")):
        raise sj.JournalError("effect reference is not an owned relative reference")
    return value


_IDENTITY_FIELDS = {
    "launch_id": token, "epoch": integer, "layout": _opt(digest),
    "transcript_ref": _reference, "receipt_ref": _reference,
    "artifact_ref": _reference, "job_name": token,
}


SCHEMAS = {
    "layout": {"digest": _opt(digest)},
    "call": {"call_id": token, "run_id": token, "role": one_of("planner", "report"),
             "reserve": integer, "revision": integer, "watermark": integer,
             **_IDENTITY_FIELDS},
    "call_done": {"call_id": token, "status": one_of("invalid", "failed", "succeeded"),
                  "usage": optional_int},
    # Approved task bindings (IDs/digests only; objective prose stays run-owned). Semantics are
    # re-validated against the envelope on every replay; old edge-only records fail closed.
    "plan": {"call_id": token, "revision": integer, "proposal_digest": digest,
             "tasks": lambda v: _task_list(v), "usage": optional_int},
    "worker": {"task_id": token, "attempt_id": token, "launch_id": token, "run_id": token,
               "worktree_id": token, "worktree_ref": _reference,
               "reserve": integer, "revision": integer, **_IDENTITY_FIELDS},
    "process": {"effect_id": token, "launch_id": token, "pid": integer, "birth": token},
    "usage_floor": {"effect_id": token, "known_usage": integer},
    "evaluation_begin": {"evaluation_id": token, "candidate": _opt(digest),
                         "artifact_ref": _reference, "revision": integer},
    "evaluation_binding": {"evaluation_id": token, "candidate": digest},
    "evaluation_done": {"evaluation_id": token, "facts_digest": digest},
    "attempt": {"attempt_id": token, "lifecycle": one_of(*LIFECYCLES),
                "outcome": _opt(one_of(*OUTCOMES)), "quiescent": boolean, "usage": optional_int},
    # Explicit trusted safe-to-retry proof; a terminal failure/rejection alone never qualifies.
    "retry_proof": {"attempt_id": token,
                    "reason": one_of("rejected_before_effects", "not_spawned", "effects_reconciled"),
                    "evidence": digest},
    "evidence": {"key": digest, "kind": one_of(*EVIDENCE_KINDS), "criterion": token,
                 "candidate": digest, "check_id": optional_token,
                 "failure_code": _opt(one_of(*FAILURE_CODES)),
                  "hypothesis_id": optional_token},
    "selection": {"candidate": digest},
    "acceptance": {"candidate": digest, "criteria": tokens},
    "stop": {"reason": one_of(*STOP_REASONS), "start": number, "deadline": number},
    "final": {"outcome": one_of("SUCCEEDED", "PARTIAL", "BLOCKED", "CANCELLED"), "reason": token,
              "unresolved": integer},
}

ADMISSION_REASONS = ("initial", "progress", "diagnostic", "closeout")


def _task_list_v2(v):
    """V2 request bound (64 tasks); semantics and any smaller explicit cap re-validate on apply."""
    if not isinstance(v, list) or not 1 <= len(v) <= contract.MAX_PLAN_TASKS_V2:
        raise sj.JournalError("plan tasks are not a bounded list")
    for item in v:
        if not isinstance(item, dict) or set(item) != set(_TASK_FIELDS):
            raise sj.JournalError("plan task does not match the allowlisted schema")
        for key, check in _TASK_FIELDS.items():
            check(item[key])
    return v


def _sources3(value):
    if not isinstance(value, list):
        raise sj.JournalError("snapshot sources must be an ID list")
    for item in value:
        token(item)
    if len(set(value)) != len(value):
        raise sj.JournalError("duplicate snapshot source")
    return value


def schemas_for(envelope):
    """Version-selected record schemas; v1 returns the unchanged module-global SCHEMAS."""
    version = getattr(envelope, "schema_version", 1)
    if version not in (2, 3):
        return SCHEMAS
    schemas = {kind: dict(fields) for kind, fields in SCHEMAS.items()}
    schemas["call"]["admission_reason"] = one_of(*ADMISSION_REASONS)
    schemas["plan"]["tasks"] = _task_list_v2
    schemas["stop"]["reason"] = one_of(*STOP_REASONS, "planner_limit")
    if version == 3:
        schemas["worker"].update({
            "seed_candidate": digest, "seed_ref": _reference, "seed_receipt": digest,
            "seed_selection": integer, "predecessor": optional_token,
            "continuation_key": digest, "attempt_number": integer,
            "planner_calls": integer, "watermark": integer,
        })
        schemas["attempt"]["outcome"] = _opt(one_of(*OUTCOMES, "yielded"))
        schemas["snapshot3"] = {
            "candidate": digest, "snapshot_ref": _reference, "receipt_digest": digest,
            "scope_digest": digest, "seed": _opt(digest), "source_attempts": _sources3,
            "kind": one_of("initial", "worker", "merge"), "effects_known": boolean,
            "revision": integer, "epoch": integer,
        }
        schemas["yield3"] = {
            "attempt_id": token, "receipt_digest": digest, "handoff_digest": digest,
            "seed_receipt": digest, "revision": integer, "epoch": integer,
        }
        schemas["selection"]["receipt_digest"] = digest
        schemas["evaluation_begin"].update({"epoch": integer, "seed_receipt": digest})
        schemas["acceptance"].update({"evaluation_id": token, "facts_digest": digest})
    return schemas


class StateError(RuntimeError):
    """A requested admission/observation is not permitted; the caller must not act."""


@dataclass(frozen=True)
class Effect:
    kind: str   # launch_worker | call_planner | call_report
    data: dict


@dataclass(frozen=True)
class Decision:
    kind: str   # plan | launch | wait | reconcile | replan | stopping | final
    reason: str = None
    outcome: str = None
    data: dict = field(default_factory=dict)


class Supervisor:
    def __init__(self, state_dir, envelope, clock, *, artifact_dir=None):
        self.envelope = envelope
        self.clock = clock
        self.artifact_dir = None if artifact_dir is None else Path(artifact_dir).resolve()
        self._requested_layout = None if self.artifact_dir is None else hashlib.sha256(
            contract.canonical({"artifact_root": os.path.normcase(str(self.artifact_dir))})
        ).hexdigest()
        self._v3 = getattr(envelope, "schema_version", 1) == 3
        self._v2 = getattr(envelope, "schema_version", 1) in (2, 3)
        self.schemas = schemas_for(envelope)
        self._journal = sj.Journal(state_dir, envelope, self.schemas, clock)

    def __enter__(self):
        self._journal.__enter__()
        try:
            self._reset()
            for rec in self._journal.records:
                if rec.kind in self.schemas:  # a valid hash chain is not semantic consistency
                    self._check(rec.kind, rec.data, epoch=rec.epoch)
                    self._apply(rec)
            if self._layout_seen:
                if self.layout != self._requested_layout:
                    raise StateError("artifact layout changed; reconciliation required")
            else:
                self._write("layout", {"digest": self._requested_layout})
            for attempt_id, attempt in list(self.attempts.items()):
                if attempt["lifecycle"] in ("ADMITTED", "SPAWN_PENDING"):
                    self.observe_attempt(attempt_id, "UNKNOWN", None, False, None)
        except BaseException:
            self._journal.close()
            raise
        return self

    def __exit__(self, *exc):
        self.close()

    def close(self):
        self._journal.close()

    def owner_current(self, epoch=None):
        return self._journal.owner_current(epoch)

    def _reset(self):
        self.calls = {}      # call_id -> call metadata, usage, done
        self.identities = {}
        self.processes = {}
        self.usage_floors = {}
        self.evaluations = {}
        self.layout, self._layout_seen = None, False
        self.attempts = {}   # attempt_id -> attempt metadata, lifecycle, usage
        self.run_ids, self.launch_ids, self.worktree_ids = set(), set(), set()
        self.plan = None     # {"revision", "tasks", "deps"}
        self.evidence = []   # canonical keys in durable order; watermark indexes this list
        self.candidate = None
        self.accepted = set()
        self.watermark = 0
        self.stop = None
        self.final = None
        self.snapshots3 = {}
        self.initial_receipt3 = None
        self.seed_receipt3 = None
        self.selection3 = 0
        self.yields3 = {}
        self.continuation_keys3 = set()

    def _write(self, kind, data):
        if kind in ("call", "worker"):  # every admission: usage guard precedes the transaction
            self._admit_usage(data["reserve"])
        self._check(kind, data)
        self._apply(self._journal.transact(kind, data))

    def _check(self, kind, d, *, epoch=None):
        """Semantic consistency of one record against reconstructed state (live and replay)."""
        limits, bad = self.envelope.limits, None
        if kind in ("plan", "call_done", "attempt") and d.get("usage") is not None:
            eid = d.get("call_id", d.get("attempt_id"))
            if d["usage"] < self.usage_floors.get(eid, 0):
                raise StateError("settled usage is below its reported lower bound")
        if kind in ("call", "worker"):
            if d["epoch"] != (epoch or self._journal.epoch) or d["layout"] != self.layout:
                raise StateError("effect owner or artifact layout does not bind")
            expected = self._identity_fields(d["run_id"], d["launch_id"], d["epoch"])
            if any(d[k] != v for k, v in expected.items()) or d["launch_id"] in self.launch_ids:
                raise StateError("effect identity is inconsistent or reused")
        if kind == "usage_floor":
            item = self.calls.get(d["effect_id"], self.attempts.get(d["effect_id"]))
            if item is None or d["known_usage"] < self.usage_floors.get(d["effect_id"], 0) \
                    or (item["usage"] is not None and d["known_usage"] > item["usage"]):
                bad = "reported usage lower bound is inconsistent"
        elif kind == "evaluation_begin":
            if self.plan is None or (d["candidate"] is not None and d["candidate"] != self.candidate) \
                    or not self._barrier() \
                    or d["evaluation_id"] in self.evaluations \
                    or d["revision"] != self.plan["revision"] \
                    or (any(not e["done"] for e in self.evaluations.values()) if self._v3 else
                        any(e["revision"] == d["revision"] for e in self.evaluations.values())
                        or self._limit("max_planner_calls", len(self.evaluations))):
                bad = "evaluation is not a fresh quiescent plan batch"
        elif kind == "evaluation_binding":
            e = self.evaluations.get(d["evaluation_id"])
            if e is None or e["done"] or e["candidate"] not in (None, d["candidate"]):
                bad = "materialized candidate does not bind the pending evaluation"
        elif kind == "evaluation_done":
            if d["evaluation_id"] not in self.evaluations \
                    or self.evaluations[d["evaluation_id"]]["done"]:
                bad = "evaluation completion is not bound to pending work"
        elif kind == "layout":
            if self._layout_seen:
                bad = "duplicate layout binding"
        elif kind == "process":
            identity = self.identities.get(d["effect_id"])
            if identity is None or identity["launch_id"] != d["launch_id"] \
                    or d["effect_id"] in self.processes or d["pid"] <= 0:
                bad = "process identity does not bind an unsettled effect"
        elif kind == "call":
            planners = len(self._planner_calls())
            if d["call_id"] in self.calls or d["call_id"] in self.attempts \
                    or d["run_id"] in self.run_ids:
                bad = "duplicate call identity"
            elif d["role"] == "planner" and (d["revision"] != planners
                                             or self._limit("max_planner_calls", planners)
                                             or (self._v2 and planners
                                                 and self._limit("max_replans", planners - 1))
                                             or not self.watermark <= d["watermark"]
                                             <= len(self.evidence)):
                bad = "planner call exceeds counters or watermark"
            elif self._v2 and d["admission_reason"] != self._admission_reason(d["role"]):
                bad = "call admission reason does not bind current facts"
            elif d["role"] == "report" and (self.final is not None or self.stop is not None
                                            or not self._barrier()
                                            or not set(self.envelope.criteria) <= self.accepted
                                            or self.counters()["reports"] >= limits["max_final_report_calls"]):
                bad = "report call not permitted"
        elif kind in ("call_done", "plan"):
            call = self.calls.get(d["call_id"])
            if call is None or call["done"] or (kind == "plan" and call["role"] != "planner"):
                bad = "result for unknown or settled call"
            elif kind == "plan":
                raw = _proposal(d["revision"], d["tasks"])
                try:
                    contract.validate_proposal(raw, self.envelope, call["revision"])
                except contract.ContractError:
                    bad = "plan record does not re-validate"
                if bad is None and hashlib.sha256(raw).hexdigest() != d["proposal_digest"]:
                    bad = "plan digest mismatch"
        elif kind == "worker":
            ids = (d["attempt_id"] in self.attempts or d["run_id"] in self.run_ids
                   or d["attempt_id"] in self.calls
                   or d["launch_id"] in self.launch_ids or d["worktree_id"] in self.worktree_ids)
            if ids or self.plan is None or d["revision"] != self.plan["revision"] \
                    or d["task_id"] not in self.plan["tasks"] \
                    or self._limit("max_total_attempts", len(self.attempts)):
                bad = "worker admission inconsistent with plan, identities or counters"
        elif kind in ("attempt", "retry_proof") and d["attempt_id"] not in self.attempts:
            bad = "observation for unknown attempt"
        elif kind == "evidence" and any(k == d["key"] for k, _ in self.evidence):
            bad = "duplicate evidence key"
        elif kind == "acceptance" and (d["candidate"] != self.candidate
                                       or not set(d["criteria"]) <= set(self.envelope.criteria)):
            bad = "acceptance does not bind the current candidate"
        elif kind in ("stop", "final") and getattr(self, kind) is not None:
            bad = f"duplicate {kind} record"
        if bad:
            raise StateError(f"{bad}; reconciliation required")
        if self._v3:
            self._check3(kind, d, epoch=epoch)

    def _apply(self, rec):
        d = rec.data
        if rec.kind == "layout":
            self.layout, self._layout_seen = d["digest"], True
        elif rec.kind == "process":
            self.processes[d["effect_id"]] = {"pid": d["pid"], "birth": d["birth"]}
        elif rec.kind == "usage_floor":
            self.usage_floors[d["effect_id"]] = d["known_usage"]
        elif rec.kind == "evaluation_begin":
            self.evaluations[d["evaluation_id"]] = dict(d, done=False, facts_digest=None)
        elif rec.kind == "evaluation_done":
            self.evaluations[d["evaluation_id"]].update(done=True, facts_digest=d["facts_digest"])
        elif rec.kind == "evaluation_binding":
            self.evaluations[d["evaluation_id"]]["candidate"] = d["candidate"]
            self.candidate, self.accepted = d["candidate"], set()
        elif rec.kind == "call":
            self.identities[d["call_id"]] = dict(d)
            self.calls[d["call_id"]] = dict(d, usage=None, done=False)
            self.run_ids.add(d["run_id"])
            self.launch_ids.add(d["launch_id"])
            if d["role"] == "planner":
                self.watermark = d["watermark"]  # whole supplied snapshot is consumed
        elif rec.kind in ("call_done", "plan"):
            self.calls[d["call_id"]].update(usage=d["usage"], done=True, status=d.get("status"))
            if rec.kind == "plan":
                bindings = tuple(contract.Task(t["task_id"], tuple(t["depends_on"]),
                                               t["objective_digest"], tuple(t["criteria"]),
                                               tuple(t["hypothesis_ids"])) for t in d["tasks"])
                self.plan = {"revision": d["revision"], "bindings": bindings,
                             "proposal_digest": d["proposal_digest"],
                             "tasks": [t.task_id for t in bindings],
                             "deps": {t.task_id: list(t.depends_on) for t in bindings}}
        elif rec.kind == "worker":
            self.identities[d["attempt_id"]] = dict(d)
            self.attempts[d["attempt_id"]] = dict(d, lifecycle="ADMITTED", outcome=None,
                                                  quiescent=False, usage=None, retry_proof=None)
            self.run_ids.add(d["run_id"])
            self.launch_ids.add(d["launch_id"])
            self.worktree_ids.add(d["worktree_id"])
        elif rec.kind == "retry_proof":
            self.attempts[d["attempt_id"]]["retry_proof"] = d["reason"]
        elif rec.kind == "attempt":
            self.attempts[d["attempt_id"]].update(
                {k: d[k] for k in ("lifecycle", "outcome", "quiescent", "usage")})
        elif rec.kind == "evidence":
            self.evidence.append((d["key"], d["criterion"]))
        elif rec.kind == "selection":
            self.candidate, self.accepted = d["candidate"], set()
        elif rec.kind == "acceptance":
            self.accepted = set(d["criteria"])
        elif rec.kind == "stop":
            self.stop = d
        elif rec.kind == "final":
            self.final = d
        if self._v3:
            self._apply3(rec)

    # ---- schema-3 protected metadata bindings ----------------------------
    def _require3(self):
        if not self._v3 or not self.owner_current():
            raise StateError("schema-3 metadata requires the current journal owner")

    def _write3(self, kind, data):
        try:
            fields = self.schemas[kind]
            if set(data) != set(fields):
                raise sj.JournalError("metadata fields do not match the schema")
            for key, check in fields.items():
                check(data[key])
        except sj.JournalError:
            raise StateError("metadata does not match the fixed schema") from None
        self._write(kind, data)

    def _revision3(self):
        return self.plan["revision"] if self.plan else 0

    def _context3(self, task_id):
        snapshot = self.snapshots3.get(self.seed_receipt3)
        if self.initial_receipt3 is None or snapshot is None:
            raise StateError("a registered initial/current snapshot is required")
        previous = [aid for aid, a in self.attempts.items() if a["task_id"] == task_id]
        context = {
            "seed_candidate": snapshot["candidate"], "seed_ref": snapshot["snapshot_ref"],
            "seed_receipt": snapshot["receipt_digest"], "seed_selection": self.selection3,
            "predecessor": previous[-1] if previous else None,
        }
        context["continuation_key"] = hashlib.sha256(contract.canonical({
            "task_id": task_id, "revision": self._revision3(), **context,
        })).hexdigest()
        context.update(attempt_number=len(self.attempts) + 1,
                       planner_calls=len(self._planner_calls()), watermark=self.watermark)
        return context

    def _receipt_for_attempt3(self, receipt, attempt_id):
        snapshot = self.snapshots3.get(receipt)
        attempt = self.attempts.get(attempt_id)
        return snapshot is not None and attempt is not None \
            and snapshot["effects_known"] \
            and attempt_id in snapshot["source_attempts"] \
            and snapshot["seed"] == attempt["seed_receipt"] \
            and snapshot["revision"] == attempt["revision"] == self._revision3()

    def _evaluation_context3(self, evaluation, epoch=None):
        return evaluation is not None and self.plan is not None \
            and evaluation["epoch"] == (self._journal.epoch if epoch is None else epoch) \
            and evaluation["revision"] == self.plan["revision"] \
            and evaluation["candidate"] == self.candidate \
            and evaluation["seed_receipt"] == self.seed_receipt3 \
            and self.seed_receipt3 in self.snapshots3

    def _check3(self, kind, d, *, epoch=None):
        owner_epoch = self._journal.epoch if epoch is None else epoch
        if kind in ("call", "worker"):
            if self.stop or self.final or not self._usage_room(d["reserve"]):
                raise StateError("admission exceeds the durable open state or usage ceiling")
        if kind in ("snapshot3", "yield3", "evaluation_begin") \
                and d["epoch"] != owner_epoch:
            raise StateError("metadata owner epoch does not bind")
        if kind == "snapshot3":
            sources = d["source_attempts"]
            if d["receipt_digest"] in self.snapshots3 or not d["effects_known"] \
                    or d["revision"] != self._revision3():
                raise StateError("snapshot receipt, effects or revision does not bind")
            if any(r["snapshot_ref"] == d["snapshot_ref"]
                   and (r["candidate"], r["scope_digest"]) !=
                   (d["candidate"], d["scope_digest"]) for r in self.snapshots3.values()):
                raise StateError("snapshot reference is already bound to different data")
            if d["kind"] == "initial":
                if self.initial_receipt3 is not None or self.attempts or sources \
                        or d["seed"] is not None \
                        or d["candidate"] != self.envelope.candidate.initial_sha256:
                    raise StateError("initial snapshot must bind the exact approved data once")
            else:
                if not sources or (d["kind"] == "worker" and len(sources) != 1) \
                        or d["seed"] not in self.snapshots3 \
                        or d["seed"] != self.seed_receipt3 \
                        or any(a["lifecycle"] == "UNKNOWN" for a in self.attempts.values()):
                    raise StateError("snapshot needs known sources and the current registered seed")
                for aid in sources:
                    a = self.attempts.get(aid)
                    if a is None or a["lifecycle"] != "COLLECTED" or not a["quiescent"] \
                            or a["outcome"] is None or a["seed_receipt"] != d["seed"] \
                            or a["revision"] != d["revision"]:
                        raise StateError("snapshot source is stale, unknown or not quiescent")
        elif kind == "yield3":
            a = self.attempts.get(d["attempt_id"])
            if a is None or a["lifecycle"] != "COLLECTED" or not a["quiescent"] \
                    or a["outcome"] is None or d["attempt_id"] in self.yields3 \
                    or d["seed_receipt"] != a["seed_receipt"] \
                    or d["revision"] != a["revision"] \
                    or not self._receipt_for_attempt3(d["receipt_digest"], d["attempt_id"]) \
                    or any(a["lifecycle"] == "UNKNOWN" for a in self.attempts.values()):
                raise StateError("yield needs a pinned, collected and reconciled source receipt")
        elif kind == "retry_proof":
            a = self.attempts[d["attempt_id"]]
            if a["lifecycle"] != "COLLECTED" or not a["quiescent"] \
                    or a["outcome"] not in ("failed", "rejected", "cancelled") \
                    or (d["reason"] == "effects_reconciled"
                        and not self._receipt_for_attempt3(d["evidence"], d["attempt_id"])):
                raise StateError("retry proof lacks collected quiescence or its receipt anchor")
        elif kind == "worker":
            expected = self._context3(d["task_id"])
            if any(d[k] != v for k, v in expected.items()) \
                    or d["continuation_key"] in self.continuation_keys3 \
                    or d["task_id"] not in self._launchable() \
                    or len(self._occupied()) >= self.envelope.limits["max_concurrent_workers"] \
                    or any(not e["done"] for e in self.evaluations.values()) \
                    or any(a["lifecycle"] == "UNKNOWN" for a in self.attempts.values()):
                raise StateError("worker continuation context or consumption does not bind")
        elif kind == "attempt":
            a = self.attempts[d["attempt_id"]]
            if (a["lifecycle"] == "COLLECTED"
                and any(a[k] != d[k] for k in ("lifecycle", "outcome", "quiescent", "usage"))) \
                    or (a["usage"] is not None and a["usage"] != d["usage"]) \
                    or (d["lifecycle"] in ("TERMINAL", "COLLECTED") and d["outcome"] is None) \
                    or (d["lifecycle"] not in ("TERMINAL", "COLLECTED")
                        and d["outcome"] is not None):
                raise StateError("attempt lifecycle or measured usage is inconsistent")
        elif kind == "selection":
            snapshot = self.snapshots3.get(d["receipt_digest"])
            if snapshot is None or snapshot["candidate"] != d["candidate"] \
                    or snapshot["revision"] != self._revision3() or not self._barrier() \
                    or any(not e["done"] for e in self.evaluations.values()):
                raise StateError("selection needs a registered, current, quiescent snapshot")
        elif kind == "evaluation_begin":
            if d["candidate"] is None or d["candidate"] != self.candidate \
                    or d["seed_receipt"] != self.seed_receipt3 \
                    or self.seed_receipt3 not in self.snapshots3:
                raise StateError("evaluation must pin the selected registered snapshot")
        elif kind in ("evaluation_binding", "evaluation_done"):
            e = self.evaluations.get(d["evaluation_id"])
            if not self._evaluation_context3(e, owner_epoch) or not self._barrier() \
                    or (kind == "evaluation_binding" and d["candidate"] != self.candidate):
                raise StateError("evaluation has a stale candidate, revision or owner")
        elif kind == "acceptance":
            e = self.evaluations.get(d["evaluation_id"])
            if not self._evaluation_context3(e, owner_epoch) or not e["done"] \
                    or e.get("accepted3", False) or e["facts_digest"] != d["facts_digest"] \
                    or not self._barrier() \
                    or any(not item["done"] for item in self.evaluations.values()):
                raise StateError("acceptance needs unused completed protected facts and quiescence")

    def _apply3(self, rec):
        d = rec.data
        if rec.kind == "snapshot3":
            self.snapshots3[d["receipt_digest"]] = dict(d)
            if d["kind"] == "initial":
                self.initial_receipt3 = self.seed_receipt3 = d["receipt_digest"]
                self.candidate, self.accepted = d["candidate"], set()
                self.selection3 += 1
        elif rec.kind == "selection":
            self.seed_receipt3 = d["receipt_digest"]
            self.selection3 += 1
        elif rec.kind == "worker":
            self.continuation_keys3.add(d["continuation_key"])
        elif rec.kind == "yield3":
            self.yields3[d["attempt_id"]] = dict(d)
            self.attempts[d["attempt_id"]]["retry_proof"] = "effects_reconciled"
        elif rec.kind == "acceptance":
            self.evaluations[d["evaluation_id"]]["accepted3"] = True

    def register_snapshot3(self, candidate, snapshot_ref, receipt_digest, scope_digest, *,
                           seed=None, source_attempts=(), kind="initial", effects_known=True):
        """Privileged capture registration; the driver proves capture, scope and quiescence."""
        self._require3()
        try:
            sources = list(source_attempts)
        except TypeError:
            raise StateError("snapshot sources must be an ID sequence") from None
        data = {
            "candidate": candidate, "snapshot_ref": snapshot_ref,
            "receipt_digest": receipt_digest, "scope_digest": scope_digest, "seed": seed,
            "source_attempts": sources, "kind": kind, "effects_known": effects_known,
            "revision": self._revision3(), "epoch": self._journal.epoch,
        }
        try:
            for key, check in self.schemas["snapshot3"].items():
                check(data[key])
        except sj.JournalError:
            raise StateError("snapshot metadata does not match the fixed schema") from None
        previous = self.snapshots3.get(receipt_digest)
        if previous is not None:
            if all(previous[k] == v for k, v in data.items() if k not in ("epoch", "revision")):
                return dict(previous)
            raise StateError("capture receipt bindings are immutable")
        self._write3("snapshot3", data)
        return dict(self.snapshots3[receipt_digest])

    def record_yield3(self, attempt_id, receipt_digest, handoff_digest):
        """Register a trusted handoff without reclassifying any raw worker result."""
        self._require3()
        a = self.attempts.get(attempt_id)
        if a is None:
            raise StateError("unknown yield source")
        data = {
            "attempt_id": attempt_id, "receipt_digest": receipt_digest,
            "handoff_digest": handoff_digest, "seed_receipt": a["seed_receipt"],
            "revision": a["revision"], "epoch": self._journal.epoch,
        }
        previous = self.yields3.get(attempt_id)
        if previous is not None:
            if all(previous[k] == v for k, v in data.items() if k != "epoch"):
                return dict(previous)
            raise StateError("yield receipt bindings are immutable")
        self._write3("yield3", data)
        return dict(self.yields3[attempt_id])

    def _launchable3(self, continuations=True):
        if self.plan is None or self._limit("max_total_attempts", len(self.attempts)):
            return []
        here = [a for a in self.attempts.values() if a["revision"] == self.plan["revision"]]
        bindings = {t.task_id: t for t in self.plan["bindings"]}

        def settled(task_id):
            criteria = bindings[task_id].criteria
            if criteria:
                return set(criteria) <= self.accepted
            return any(a["task_id"] == task_id and a["lifecycle"] == "COLLECTED"
                       and a["quiescent"] for a in here)

        ready = []
        for task_id in self.plan["tasks"]:
            if settled(task_id) or not all(settled(dep) for dep in self.plan["deps"][task_id]):
                continue
            prior = [a for a in here if a["task_id"] == task_id]
            if prior:
                a = prior[-1]
                if not continuations or a["retry_proof"] is None \
                        or a["lifecycle"] != "COLLECTED" or not a["quiescent"]:
                    continue
                if a["retry_proof"] == "effects_reconciled":
                    current = self.snapshots3.get(self.seed_receipt3)
                    if current is None or self.selection3 <= a["seed_selection"] \
                            or current["candidate"] == a["seed_candidate"]:
                        continue
            ready.append(task_id)
        return ready

    # ---- read-only views -------------------------------------------------
    def _limit(self, key, used):
        """Finite caps are reached at `used`; a null v2 cap never is (v1 caps are always finite)."""
        return contract.limit_reached(self.envelope, key, used)

    def _deadline_passed(self):
        return contract.deadline_passed(self.envelope, self.clock())

    def _admission_reason(self, role):
        """V2 call reason derived only from durable facts, never supplied by a caller."""
        if role == "report":
            return "closeout"
        if not self._planner_calls():
            return "initial"
        return "progress" if self._new_relevant_evidence() else "diagnostic"

    def _planner_calls(self):
        return [c for c in self.calls.values() if c["role"] == "planner"]

    def _occupied(self):
        return [a for a in self.attempts.values()
                if not (a["lifecycle"] in ("TERMINAL", "COLLECTED") and a["quiescent"])]

    def counters(self):
        planner = len(self._planner_calls())
        return {"attempts": len(self.attempts), "planner_calls": planner,
                "replans": max(planner - 1, 0), "occupied": len(self._occupied()),
                "reports": sum(c["role"] == "report" for c in self.calls.values()),
                "evidence": len(self.evidence), "watermark": self.watermark}

    def totals(self):
        """Measured usage charged once per call; unknown usage keeps its reservation."""
        items = list(self.calls.items()) + list(self.attempts.items())
        unknown = [i for _, i in items if i["usage"] is None]
        charged = sum(i["usage"] if i["usage"] is not None else self.usage_floors.get(eid, 0)
                      for eid, i in items)
        # total_usage is exact only when every call was measured; null never becomes 0.
        return {"charged": charged, "reserved": sum(i["reserve"] for i in unknown),
                "unknown_calls": len(unknown), "total_usage": None if unknown else charged}

    def _admit_usage(self, reserve):
        """Shared guard before any admission write; max_usage is a token-unit estimate."""
        if type(reserve) is not int or not 1 <= reserve <= contract.MAX_USAGE_CEILING:
            raise StateError("usage reserve must be a positive integer estimate")
        if not self._usage_room(reserve):
            raise StateError("usage ceiling reached; admission refused")

    def _usage_room(self, reserve):
        t = self.totals()  # accounting is kept even when max_usage is null (no ceiling)
        return contract.usage_available(self.envelope, t["charged"], t["reserved"], reserve)

    def report_done(self, call_id, usage, success):
        """Settle an admitted report once; usage None keeps its reservation. No slot refund."""
        call = self.calls.get(call_id)
        if call is None or call["role"] != "report":
            raise StateError("stale or unknown call")
        if type(success) is not bool or not (usage is None or (type(usage) is int and usage >= 0)):
            raise StateError("report result does not match the fixed schema")
        status = "succeeded" if success else "failed"
        if call["done"]:
            if (call["usage"], call.get("status")) == (usage, status):
                return None
            raise StateError("report result already observed")
        self._write("call_done", {"call_id": call_id, "status": status, "usage": usage})
        return self.calls[call_id]

    def stop_deadline(self):
        return self.stop["deadline"] if self.stop else None

    def _outstanding_call(self):
        return any(not c["done"] for c in self.calls.values())

    def _new_relevant_evidence(self):
        return any(c not in self.accepted for _, c in self.evidence[self.watermark:])

    def _launchable(self):
        if self._v3:
            return self._launchable3()
        if self.plan is None or self._limit("max_total_attempts", len(self.attempts)):
            return []
        rev = self.plan["revision"]
        here = [a for a in self.attempts.values() if a["revision"] == rev]
        collected = {a["task_id"] for a in here if a["lifecycle"] == "COLLECTED"}
        # Only an attempt with a durable safe-to-retry proof may be replaced (fresh slot and IDs).
        launched = {a["task_id"] for a in here if a["retry_proof"] is None}
        return [t for t in self.plan["tasks"]
                if t not in launched and set(self.plan["deps"][t]) <= collected]

    def _barrier(self):
        # Every admitted attempt must be COLLECTED and quiescent; terminal alone is not enough.
        uncollected = any(a["lifecycle"] != "COLLECTED" for a in self.attempts.values())
        return not (uncollected or self._occupied() or self._outstanding_call()
                    or (self._launchable3(False) if self._v3 else self._launchable()))

    def record_retry_proof(self, attempt_id, reason, evidence):
        """Durably record trusted proof that a collected, quiescent attempt is safe to replace."""
        a = self.attempts.get(attempt_id)
        if a is None or a["lifecycle"] != "COLLECTED" or not a["quiescent"] \
                or a["outcome"] not in ("failed", "rejected", "cancelled"):
            raise StateError("retry proof needs a collected, quiescent, unsuccessful attempt")
        try:
            self._write("retry_proof", {"attempt_id": attempt_id, "reason": reason,
                                        "evidence": evidence})
        except sj.JournalError:
            raise StateError("retry proof does not match the fixed schema") from None

    def _require_open(self, model_call=False):
        if self.final or self.stop:
            raise StateError("goal is stopping or final; admissions are frozen")
        if self._deadline_passed():
            raise StateError("goal deadline passed")
        if any(a["lifecycle"] == "UNKNOWN" for a in self.attempts.values()):
            raise StateError("unknown attempt requires reconciliation")
        if any(not e["done"] for e in self.evaluations.values()):
            raise StateError("unknown verification effects require reconciliation")

    def _fresh_ids(self, run_id, *others):
        token(run_id)
        if run_id in self.run_ids:
            raise StateError("run identity already used")
        for value, used in others:
            if value in used:
                raise StateError("identity already used")

    def _identity_fields(self, run_id, launch_id, epoch=None):
        token(run_id)
        if any(c in run_id for c in "\\/:") or run_id.endswith(".") \
                or run_id.split(".")[0].upper() in {
                    "CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)),
                    *(f"LPT{i}" for i in range(1, 10))}:
            raise StateError("run identity is not a portable artifact basename")
        try:
            if str(uuid.UUID(launch_id)) != launch_id:
                raise ValueError
        except (ValueError, TypeError, AttributeError):
            raise StateError("launch identity is not a canonical UUID") from None
        prefix = f"runs/{run_id}"
        return {"launch_id": launch_id, "epoch": epoch or self._journal.epoch,
                "layout": self.layout, "transcript_ref": f"{prefix}/{run_id}.md",
                "receipt_ref": f"{prefix}/{launch_id}.receipt.json",
                "artifact_ref": f"{prefix}/artifacts", "job_name": f"AgentControl-{launch_id}"}

    def effect_identity(self, effect_id):
        identity = self.identities.get(effect_id)
        if identity is None:
            raise StateError("unknown effect identity")
        result = dict(identity)
        if effect_id in self.processes:
            result["process"] = dict(self.processes[effect_id])
        return result

    def record_process_identity(self, effect_id, launch_id, pid, birth):
        """Pin the exact suspended process before resume; old owners cannot stamp new facts."""
        identity = self.identities.get(effect_id)
        if identity is None or identity["launch_id"] != launch_id \
                or identity["epoch"] != self._journal.epoch:
            raise StateError("stale or unknown effect process identity")
        try:
            integer(pid)
            token(birth)
            if pid <= 0:
                raise sj.JournalError("invalid process identity")
        except sj.JournalError:
            raise StateError("invalid process identity") from None
        observed = {"pid": pid, "birth": birth}
        if effect_id in self.processes:
            if self.processes[effect_id] == observed:
                return None
            raise StateError("process identity is immutable")
        self._write("process", {"effect_id": effect_id, "launch_id": launch_id,
                                "pid": pid, "birth": birth})
        return dict(observed)

    def observe_usage_floor(self, effect_id, known_usage):
        """Retain reported work without asserting that missing provider usage is measured zero."""
        try:
            integer(known_usage)
        except sj.JournalError:
            raise StateError("invalid reported usage lower bound") from None
        if effect_id in self.usage_floors and self.usage_floors[effect_id] == known_usage:
            return None
        self._write("usage_floor", {"effect_id": effect_id, "known_usage": known_usage})
        return known_usage

    # ---- admissions: durable before the returned effect ------------------
    def admit_planner(self, call_id, run_id, reserve):
        self._admit_usage(reserve)  # usage refusal precedes every other admission check
        self._require_open(model_call=True)
        calls = self._planner_calls()
        if self._limit("max_planner_calls", len(calls)) or self._outstanding_call() \
                or (self._v2 and calls and self._limit("max_replans", len(calls) - 1)):
            raise StateError("planner call not permitted")
        if calls:  # adaptive replan: barrier plus new relevant evidence after the watermark
            # v2: a quiescent no-novelty batch is a counted diagnostic replan, not fake evidence
            diagnostic = self._v2 and not set(self.envelope.criteria) <= self.accepted
            if not self._barrier() or not (self._new_relevant_evidence() or diagnostic):
                raise StateError("replan requires a quiescent barrier and new evidence")
        self._fresh_ids(run_id, (call_id, self.calls))
        data = {"call_id": call_id, "run_id": run_id, "role": "planner", "reserve": reserve,
                "revision": len(calls), "watermark": len(self.evidence),
                **self._identity_fields(run_id, str(uuid.uuid4()))}
        if self._v2:
            data["admission_reason"] = self._admission_reason("planner")
        self._write("call", data)
        return Effect("call_planner", data)

    def _open_call(self, call_id, role):
        call = self.calls.get(call_id)
        if call is None or call["role"] != role or call["done"]:
            raise StateError("stale or unknown call")
        return call

    def record_plan(self, call_id, raw, usage):
        """Accept a validated planner proposal; an invalid one still consumes the call."""
        call = self._open_call(call_id, "planner")
        try:
            plan = contract.validate_proposal(raw, self.envelope, call["revision"])
        except contract.ContractError:
            self._write("call_done", {"call_id": call_id, "status": "invalid", "usage": usage})
            raise
        if self.stop or self.final or self._deadline_passed():
            self._write("call_done", {"call_id": call_id, "status": "failed", "usage": usage})
            raise StateError("late planner response cannot alter the goal")
        tasks = [{"task_id": t.task_id, "depends_on": list(t.depends_on),
                  "objective_digest": t.objective_digest, "criteria": list(t.criteria),
                  "hypothesis_ids": list(t.hypothesis_ids)} for t in plan.tasks]
        self._write("plan", {"call_id": call_id, "revision": plan.revision, "tasks": tasks,
                             "proposal_digest": hashlib.sha256(
                                 _proposal(plan.revision, tasks)).hexdigest(), "usage": usage})
        return plan

    def plan_view(self):
        """Stable view of the current approved plan for the driver: Task bindings + digest."""
        if self.plan is None:
            return None
        return {"revision": self.plan["revision"], "tasks": self.plan["bindings"],
                "proposal_digest": self.plan["proposal_digest"]}

    def planner_failed(self, call_id, usage):
        """Record a failed or lost planner response; the call stays consumed."""
        self._open_call(call_id, "planner")
        self._write("call_done", {"call_id": call_id, "status": "failed", "usage": usage})

    def admit_worker(self, task_id, attempt_id, launch_id, run_id, worktree_id, reserve):
        self._admit_usage(reserve)
        self._require_open()
        limits = self.envelope.limits
        if self._limit("max_total_attempts", len(self.attempts)):
            raise StateError("attempt ceiling reached")
        if len(self._occupied()) >= limits["max_concurrent_workers"]:
            raise StateError("concurrency ceiling reached")
        if task_id not in self._launchable():
            raise StateError("task is not launchable under the current plan")
        try:
            if str(uuid.UUID(launch_id)) != launch_id:
                raise ValueError
        except (ValueError, TypeError, AttributeError):
            raise StateError("launch_id must be a canonical UUID") from None
        self._fresh_ids(run_id, (attempt_id, self.attempts), (launch_id, self.launch_ids),
                        (worktree_id, self.worktree_ids))
        data = {"task_id": task_id, "attempt_id": attempt_id, "launch_id": launch_id,
                 "run_id": run_id, "worktree_id": worktree_id, "reserve": reserve,
                "revision": self.plan["revision"], "worktree_ref": f"worktrees/{attempt_id}",
                **self._identity_fields(run_id, launch_id)}
        if self._v3:
            data.update(self._context3(task_id))
        self._write("worker", data)
        return Effect("launch_worker", data)

    def admit_report(self, call_id, run_id, reserve):
        """Optional pre-final closeout after acceptance; success waits for its quiescent result."""
        self._admit_usage(reserve)
        self._require_open(model_call=True)
        if not self._barrier() or not set(self.envelope.criteria) <= self.accepted:
            raise StateError("report call not permitted")
        if self.counters()["reports"] >= self.envelope.limits["max_final_report_calls"]:
            raise StateError("report slot already consumed")
        self._fresh_ids(run_id, (call_id, self.calls))
        data = {"call_id": call_id, "run_id": run_id, "role": "report", "reserve": reserve,
                "revision": 0, "watermark": 0,
                **self._identity_fields(run_id, str(uuid.uuid4()))}
        if self._v2:
            data["admission_reason"] = "closeout"
        self._write("call", data)
        return Effect("call_report", data)

    # ---- trusted observations (from adapters, never planner output) ------
    def observe_attempt(self, attempt_id, lifecycle, outcome, quiescent, usage):
        """Record a trusted lifecycle fact. Idempotent re-reads write nothing."""
        a = self.attempts.get(attempt_id)
        if a is None:
            raise StateError("unknown attempt")
        new = {"lifecycle": lifecycle, "outcome": outcome, "quiescent": quiescent, "usage": usage}
        if all(a[k] == v for k, v in new.items()):
            return None
        if a["lifecycle"] == "COLLECTED":
            raise StateError("collected attempt facts are immutable")
        if a["usage"] is not None and usage != a["usage"]:
            raise StateError("measured usage cannot change")
        if lifecycle in ("TERMINAL", "COLLECTED") and outcome is None:
            raise StateError("terminal facts need an outcome")
        self._write("attempt", dict(new, attempt_id=attempt_id))
        return self.attempts[attempt_id]

    def register_evidence(self, kind, criterion, candidate, check_id=None, failure_code=None,
                          hypothesis_id=None):
        """Register bounded trusted evidence; returns its key, or None if not new."""
        self._require_open()
        env = self.envelope
        shape_ok = {
            "candidate": check_id is None and failure_code is None and hypothesis_id is None,
            "check_failure": check_id in env.check_ids and failure_code in FAILURE_CODES
                             and hypothesis_id is None,
            "hypothesis_falsified": hypothesis_id in env.hypothesis_ids and check_id in env.check_ids
                                    and failure_code is None,
        }.get(kind, False)
        if not shape_ok or criterion not in env.criteria:
            raise StateError("evidence does not match the approved trusted schema")
        if kind == "candidate":
            self.select_candidate(candidate)
        data = {"kind": kind, "criterion": criterion, "candidate": candidate, "check_id": check_id,
                "failure_code": failure_code, "hypothesis_id": hypothesis_id}
        # Identity excludes labels/timestamps/output; a return to an earlier candidate is not new.
        key = hashlib.sha256(contract.canonical(
            {k: v for k, v in data.items() if not (kind == "candidate" and k == "criterion")}
        )).hexdigest()
        if any(k == key for k, _ in self.evidence):
            return None
        self._write("evidence", dict(data, key=key))
        return key

    def select_candidate(self, candidate, *, receipt_digest=None):
        """Current content binding is independent from novelty; old content grants no new turn."""
        self._require_open()
        if self._v3:
            self._require3()
            if receipt_digest is None:
                if candidate == self.candidate and self.seed_receipt3 is not None:
                    return None
                matches = [r for r in self.snapshots3.values() if r["candidate"] == candidate]
                if len(matches) != 1:
                    raise StateError("selection requires an unambiguous registered receipt")
                receipt_digest = matches[0]["receipt_digest"]
            snapshot = self.snapshots3.get(receipt_digest)
            if snapshot is None or snapshot["candidate"] != candidate:
                raise StateError("selection does not bind a registered snapshot")
            if receipt_digest == self.seed_receipt3:
                return None
            self._write3("selection", {"candidate": candidate, "receipt_digest": receipt_digest})
            return candidate
        if receipt_digest is not None:
            raise StateError("capture receipt selection requires schema 3")
        if candidate == self.candidate:
            return None
        self._write("selection", {"candidate": candidate})
        return candidate

    def begin_evaluation(self, evaluation_id, candidate, artifact_ref):
        """Record the command group before validation can create any external process."""
        self._require_open()
        data = {"evaluation_id": evaluation_id, "candidate": candidate,
                "artifact_ref": artifact_ref,
                "revision": self.plan["revision"] if self.plan else 0}
        if self._v3:
            self._require3()
            data.update(epoch=self._journal.epoch, seed_receipt=self.seed_receipt3)
            self._write3("evaluation_begin", data)
        else:
            self._write("evaluation_begin", data)
        return dict(self.evaluations[evaluation_id])

    def finish_evaluation(self, evaluation_id, facts_digest, *, quiescent):
        if quiescent is not True:
            raise StateError("verification quiescence is unconfirmed")
        previous = self.evaluations.get(evaluation_id)
        if self._v3:
            self._require3()
            if not self._evaluation_context3(previous) or not self._barrier():
                raise StateError("verification completion has stale or nonquiescent context")
        if previous is not None and previous["done"]:
            if previous["facts_digest"] == facts_digest:
                return None
            raise StateError("verification facts are immutable")
        self._write("evaluation_done", {"evaluation_id": evaluation_id, "facts_digest": facts_digest})
        return dict(self.evaluations[evaluation_id])

    def bind_evaluation_candidate(self, evaluation_id, candidate):
        self._write("evaluation_binding", {"evaluation_id": evaluation_id, "candidate": candidate})
        return candidate

    def record_acceptance(self, candidate, criteria, *, evaluation_id=None):
        """Record protected-validator acceptance for the current integrated candidate."""
        self._require_open()
        if self._v3:
            self._require3()
            facts = criteria
            if not isinstance(facts, dict) or set(facts) != {"criteria"}:
                raise StateError("protected facts must name their approved criterion IDs")
            try:
                tokens(facts["criteria"])
                facts_digest = hashlib.sha256(contract.canonical(facts)).hexdigest()
            except (sj.JournalError, TypeError, ValueError):
                raise StateError("protected facts do not match the fixed schema") from None
            self._write3("acceptance", {
                "candidate": candidate, "criteria": sorted(facts["criteria"]),
                "evaluation_id": evaluation_id, "facts_digest": facts_digest,
            })
            return
        if evaluation_id is not None:
            raise StateError("evaluation-bound acceptance requires schema 3")
        if candidate != self.candidate:
            raise StateError("acceptance must bind the current registered candidate")
        if not set(criteria) <= set(self.envelope.criteria):
            raise StateError("acceptance names unapproved criteria")
        self._write("acceptance", {"candidate": candidate, "criteria": sorted(set(criteria))})

    def request_stop(self, reason):
        """Durably enter STOPPING once; the original t+60 deadline is never extended."""
        if self.final or self.stop:
            return Decision("stopping", self.stop["reason"] if self.stop else None)
        now = self.clock()
        start = now if self.envelope.deadline is None else min(now, self.envelope.deadline)
        self._write("stop", {"reason": reason, "start": start, "deadline": start + GRACE})
        return Decision("stopping", reason)

    def _finalize(self, outcome, reason, unresolved=0):
        self._write("final", {"outcome": outcome, "reason": reason, "unresolved": unresolved})
        return self.decide()

    def decide(self, reserve=1):
        """Pure-metadata next step; may durably record STOPPING or the final outcome.

        reserve is the driver's next admission estimate: a step no admission could pass under
        the usage ceiling becomes a fixed usage_limit stop instead of a launch/refuse loop."""
        if self.final:
            f = self.final
            return Decision("final", f["reason"], f["outcome"], {"unresolved": f["unresolved"]})
        if self.stop:
            unresolved = (len(self._occupied()) + sum(not c["done"] for c in self.calls.values())
                          + sum(not e["done"] for e in self.evaluations.values()))
            cancel = self.stop["reason"] in ("cancelled", "revoked")
            if unresolved and self.clock() < self.stop["deadline"]:
                return Decision("wait", self.stop["reason"])
            if unresolved:
                return self._finalize("BLOCKED", "cancel_incomplete" if cancel
                                      else "unresolved_effects", unresolved)
            if self.stop["reason"] == "operator_required":
                return self._finalize("BLOCKED", "operator_required")
            return self._finalize("CANCELLED" if cancel else "PARTIAL", self.stop["reason"])
        if self._deadline_passed():
            return self.request_stop("deadline")
        if any(a["lifecycle"] == "UNKNOWN" for a in self.attempts.values()):
            return Decision("reconcile")
        if any(not e["done"] for e in self.evaluations.values()):
            return Decision("reconcile")
        room = self._usage_room(reserve)
        if self.plan is None and not self.calls:
            return Decision("plan") if room else self.request_stop("usage_limit")
        if self._v3 and self.initial_receipt3 is None:
            return Decision("reconcile", "initial_snapshot")
        launchable = self._launchable()
        if launchable and not room:  # no admission would pass: drain in-flight work, then stop
            if self._occupied() or self._outstanding_call():
                return Decision("wait", "usage_limit")
            return self.request_stop("usage_limit")
        if launchable and len(self._occupied()) < self.envelope.limits["max_concurrent_workers"]:
            return Decision("launch", data={"tasks": launchable})
        if not self._barrier():
            return Decision("wait")
        if set(self.envelope.criteria) <= self.accepted:
            return self._finalize("SUCCEEDED", "succeeded")
        c = self.counters()
        if self._v2 and self._limit("max_planner_calls", c["planner_calls"]):
            return self.request_stop("planner_limit")
        if self._limit("max_replans", c["replans"]):
            return self.request_stop("replan_limit")
        if self._limit("max_total_attempts", c["attempts"]):
            return self.request_stop("attempt_limit")
        reason = None
        if not self._new_relevant_evidence():
            if not self._v2:
                return self.request_stop("no_progress")
            reason = "diagnostic"  # v2: counted fresh investigation, not fake evidence
        return Decision("replan", reason) if room else self.request_stop("usage_limit")

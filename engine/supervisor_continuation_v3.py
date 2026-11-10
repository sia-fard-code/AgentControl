"""Schema-3 composition for the existing driver; the Core remains the only state machine."""
import hashlib
import os
from pathlib import Path
import tempfile

import candidate
import supervisor_contract as contract
import supervisor_contract_v3 as codec
import supervisor_evidence as evidence
import task_status


def _sha(raw):
    return hashlib.sha256(raw).hexdigest()


def _bounded(path, maximum):
    with Path(path).open("rb") as stream:
        raw = stream.read(maximum + 1)
    if len(raw) > maximum:
        raise ValueError("snapshot metadata exceeds its bound")
    return raw


def _snapshot_identity(snapshot, parent, bounds, inputs, owner_guard):
    # Transport contains blobs, not a tree; validate DATA only after protected materialization.
    with tempfile.TemporaryDirectory(prefix=".verify-", dir=parent) as temporary:
        tree = Path(temporary) / "tree"
        candidate.materialize(snapshot, tree, bounds=bounds, owner_guard=owner_guard)
        return candidate.identity(tree, bounds=bounds, inputs=inputs,
                                  owner_guard=owner_guard, strict_layout=False)


def validate_initial(env, art, snapshot, owner_guard):
    """Read only the operator-prepared v1 initial snapshot, never the live project."""
    expected = Path(art).resolve() / "snapshots" / "initial"
    if Path(snapshot.directory).absolute() != expected \
            or expected.resolve() != expected or snapshot.context_sha256 is not None \
            or snapshot.seed_sha256 is not None \
            or snapshot.manifest_sha256 != env.candidate.initial_sha256:
        raise ValueError("initial snapshot does not bind approval")
    if not owner_guard():
        raise ValueError("snapshot owner unavailable")
    bounds = candidate.SnapshotBounds(**dict(env.candidate.bounds))
    opened = candidate.open_snapshot(
        expected, bounds=bounds, manifest_sha256=snapshot.manifest_sha256,
        receipt_sha256=snapshot.receipt_sha256, scope_sha256=snapshot.scope_sha256,
        seed_sha256=None, context_sha256=None)
    actual = _snapshot_identity(opened, expected.parent, bounds, env.candidate.inputs, owner_guard)
    if not owner_guard() or actual != env.candidate.initial_sha256:
        raise ValueError("initial snapshot identity changed")
    return opened


class Continuation3:
    def __init__(self, driver):
        self.d, self.sup, self.env = driver, driver.sup, driver.env
        self.art = Path(driver.art).resolve()
        self.bounds = candidate.SnapshotBounds(**dict(self.env.candidate.bounds))
        initial = validate_initial(self.env, self.art, driver.adapter.initial_snapshot(),
                                   self.sup.owner_current)
        self.scope = initial.scope_sha256
        self._guard()
        self.sup.register_snapshot3(
            initial.manifest_sha256, "snapshots/initial", initial.receipt_sha256,
            initial.scope_sha256, kind="initial")
        self.open_record(self.sup.snapshots3[self.sup.initial_receipt3])
        driver.adapter.set_snapshot_record_resolver(self.resolve)
        self.publish()

    def _guard(self):
        if not self.sup.owner_current():
            raise ValueError("snapshot owner unavailable")

    def _context(self, seed, sources, kind):
        return codec.snapshot_context3(self.env.digest, seed_receipt=seed,
                                       source_attempts=list(sources), kind=kind)

    def resolve(self, receipt):
        self._guard()
        return dict(self.sup.snapshots3[receipt])

    def identity(self, tree):
        self._guard()
        result = candidate.identity(
            tree, bounds=self.bounds, inputs=self.env.candidate.inputs,
            owner_guard=self.sup.owner_current, strict_layout=False)
        self._guard()
        return result

    def _reference(self, directory):
        directory = Path(directory).absolute()
        ref = directory.relative_to(self.art).as_posix()
        if contract.resolve_owned(self.art, ref) != directory or directory.resolve() != directory \
                or Path(ref).parts[0] != "snapshots":
            raise ValueError("snapshot is not in protected storage")
        return ref

    def _open(self, directory, manifest, receipt, scope, seed, context):
        self._guard()
        self._reference(directory)
        if scope != self.scope:
            raise ValueError("snapshot scope changed")
        snapshot = candidate.open_snapshot(
            directory, bounds=self.bounds, manifest_sha256=manifest,
            receipt_sha256=receipt, scope_sha256=scope, seed_sha256=seed,
            context_sha256=context)
        if _snapshot_identity(snapshot, self.art / "snapshots", self.bounds,
                              self.env.candidate.inputs, self.sup.owner_current) != manifest:
            raise ValueError("snapshot data changed")
        self._guard()
        return snapshot

    def open_record(self, record):
        seed = self.sup.snapshots3[record["seed"]] if record["seed"] is not None else None
        context = None if record["kind"] == "initial" else self._context(
            record["seed"], record["source_attempts"], record["kind"])
        return self._open(
            contract.resolve_owned(self.art, record["snapshot_ref"]), record["candidate"],
            record["receipt_digest"], record["scope_digest"],
            None if seed is None else seed["candidate"], context)

    def collect(self, aid):
        self._guard()
        attempt = self.sup.attempts[aid]
        result = self.d.adapter.read_worker(
            self.sup.effect_identity(aid), owner_guard=self.sup.owner_current)
        snapshot = result.snapshot
        context = self._context(attempt["seed_receipt"], [aid], "worker")
        if (result.attempt_id, result.run_id, result.seed_candidate, result.seed_receipt) != (
                aid, attempt["run_id"], attempt["seed_candidate"], attempt["seed_receipt"]) \
                or snapshot.seed_sha256 != attempt["seed_candidate"] \
                or snapshot.context_sha256 != context:
            raise ValueError("worker snapshot identity changed")
        for pin in (result.completion_sha256, result.handoff_sha256):
            if not isinstance(pin, str) or not contract.SHA256.fullmatch(pin):
                raise ValueError("worker completion pin is invalid")
        ref = self._reference(snapshot.directory)
        if result.receipt_ref not in (ref, ref + "/receipt.json"):
            raise ValueError("worker snapshot reference changed")
        try:
            self._open(snapshot.directory, snapshot.manifest_sha256, snapshot.receipt_sha256,
                       snapshot.scope_sha256, attempt["seed_candidate"], context)
        except candidate.CandidateError:
            raise ValueError("worker snapshot verification failed") from None
        return result

    def reconcile(self):
        sup = self.sup
        self.d._reconcile_calls()
        ready = []
        # Observe every admitted group before reading any worker output.
        for aid, attempt in list(sup.attempts.items()):
            if attempt["lifecycle"] == "COLLECTED":
                continue
            obs = self.d._observe(aid)
            if obs is None or obs["state"] == "UNKNOWN":
                sup.observe_attempt(aid, "UNKNOWN", None, False, attempt["usage"])
            elif obs["state"] == "RUNNING":
                sup.observe_attempt(aid, "RUNNING", None, False, obs["usage"])
            else:
                sup.observe_attempt(aid, "TERMINAL", obs["outcome"] or "failed",
                                    obs["quiescent"], obs["usage"])
                if obs["quiescent"] and obs["outcome"] is not None:
                    ready.append(aid)
        if any(a["lifecycle"] not in ("TERMINAL", "COLLECTED") or not a["quiescent"]
               for a in sup.attempts.values()) or sup._outstanding_call():
            return
        ready += [aid for aid, a in sup.attempts.items()
                  if a["lifecycle"] == "COLLECTED" and aid not in sup.yields3]
        collected = {}
        for aid in ready:
            result = self.d._collect(aid)
            if result is None:
                attempt = sup.attempts[aid]
                if attempt["lifecycle"] != "COLLECTED":
                    sup.observe_attempt(aid, "UNKNOWN", None, False, attempt["usage"])
                else:
                    raise ValueError("collected snapshot is no longer readable")
            else:
                collected[aid] = result
        if any(a["lifecycle"] == "UNKNOWN" for a in sup.attempts.values()):
            return
        for aid, result in collected.items():
            attempt, snapshot = sup.attempts[aid], result.snapshot
            self._guard()
            sup.observe_attempt(aid, "COLLECTED", attempt["outcome"], True, attempt["usage"])
            sup.register_snapshot3(
                snapshot.manifest_sha256, self._reference(snapshot.directory),
                snapshot.receipt_sha256, snapshot.scope_sha256,
                seed=attempt["seed_receipt"], source_attempts=[aid], kind="worker")
            sup.record_yield3(aid, snapshot.receipt_sha256, result.handoff_sha256)

    def _inputs(self, seed_receipt, sources):
        results = []
        for aid in sources:
            attempt = self.sup.attempts[aid]
            handoff = self.sup.yields3[aid]
            record = self.sup.snapshots3[handoff["receipt_digest"]]
            if attempt["seed_receipt"] != seed_receipt \
                    or handoff["seed_receipt"] != seed_receipt \
                    or record["seed"] != seed_receipt \
                    or record["source_attempts"] != [aid] or record["kind"] != "worker" \
                    or attempt["revision"] != self.sup.plan["revision"] \
                    or record["revision"] != attempt["revision"] \
                    or attempt["lifecycle"] != "COLLECTED" or not attempt["quiescent"]:
                raise ValueError("merge input has a stale producing receipt")
            results.append(self.open_record(record))
        return results

    def _select_batch(self):
        sup = self.sup
        seed_receipt = sup.seed_receipt3
        seed = self.open_record(sup.snapshots3[seed_receipt])
        sources = sorted(aid for aid, a in sup.attempts.items()
                         if a["revision"] == sup.plan["revision"]
                         and a["seed_receipt"] == seed_receipt)
        if not sources:
            return False, False
        results = self._inputs(seed_receipt, sources)
        context = self._context(seed_receipt, sources, "merge")
        ref = "snapshots/merge-" + context
        existing = [r for r in sup.snapshots3.values() if r["snapshot_ref"] == ref]
        if existing:
            record = existing[0]
            if record["seed"] != seed_receipt or record["source_attempts"] != sources \
                    or record["kind"] != "merge":
                raise ValueError("merge producing metadata changed")
            merged = self.open_record(record)
        else:
            destination = self.art / ref
            if destination.exists():
                # Capture completed before registration: pin raw protected bytes before parsing.
                manifest = _bounded(destination / "manifest.json", self.bounds.max_manifest_bytes)
                receipt = _bounded(destination / "receipt.json", contract.MAX_ENVELOPE_BYTES)
                merged = self._open(destination, _sha(manifest), _sha(receipt), self.scope,
                                    seed.manifest_sha256, context)
            else:
                self._guard()
                try:
                    merged = candidate.merge(
                        seed, results, destination, bounds=self.bounds,
                        # The transport accepts normalized relative scope roots;
                        # per-worker file/directory policy was checked at collection.
                        allowed_write_paths=tuple(p.removesuffix("/")
                                                  for p in self.env.capabilities["write"]),
                        owner_guard=sup.owner_current, context_sha256=context)
                except candidate.CandidateError as exc:
                    if exc.code != "merge_conflict":
                        raise
                    return False, True
                merged = self._open(
                    merged.directory, merged.manifest_sha256, merged.receipt_sha256,
                    merged.scope_sha256, seed.manifest_sha256, context)
            sup.register_snapshot3(
                merged.manifest_sha256, ref, merged.receipt_sha256, merged.scope_sha256,
                seed=seed_receipt, source_attempts=sources, kind="merge")
        self._guard()
        sup.select_candidate(merged.manifest_sha256, receipt_digest=merged.receipt_sha256)
        return merged.manifest_sha256 != seed.manifest_sha256, False

    def _facts(self, evaluation):
        self._guard()
        path = contract.resolve_owned(self.art, evaluation["artifact_ref"])
        raw = _bounded(path, contract.MAX_ENVELOPE_BYTES)
        facts = contract.strict_json(raw)
        if _sha(raw) != evaluation["facts_digest"] or not isinstance(facts, dict) \
                or set(facts) != {"criteria"} or contract.canonical(facts) != raw:
            raise ValueError("protected evaluation facts changed")
        self._guard()
        return facts

    def prior_batch(self, revision):
        evaluations = [e for e in self.sup.evaluations.values()
                       if e["done"] and e["revision"] < revision]
        if not evaluations:
            return None
        evaluation = evaluations[-1]
        facts = self._facts(evaluation)
        return {"candidate": evaluation["candidate"], "satisfied": facts["criteria"],
                "checks": [], "hypotheses": [], "invalid": None, "selection": []}

    def evaluate_batch(self):
        sup = self.sup
        self._guard()
        if any(not e["done"] for e in sup.evaluations.values()):
            if any(not e["done"] and e["epoch"] != sup._journal.epoch
                   for e in sup.evaluations.values()):
                sup.request_stop("invalid_evidence")
            return  # Unknown command groups are owed reconciliation, never replay.
        if not sup._barrier():
            return
        if contract.deadline_passed(self.env, self.d.clock()):
            sup.request_stop("deadline")
            return
        improved, conflict = self._select_batch()
        if not sup._barrier():
            return  # DATA changes clear acceptance and can reopen previously skipped work.
        epoch = sup._journal.epoch
        key = {"revision": sup.plan["revision"], "candidate": sup.candidate,
               "seed_receipt": sup.seed_receipt3, "epoch": epoch}
        eid = "eval-" + _sha(contract.canonical(key))
        previous = sup.evaluations.get(eid)
        if previous is not None:
            facts = self._facts(previous)
            if not previous.get("accepted3"):
                sup.record_acceptance(sup.candidate, facts, evaluation_id=eid)
            return
        selected = self.open_record(sup.snapshots3[sup.seed_receipt3])
        ref = f"evaluations3/{eid}/criteria.json"
        sup.begin_evaluation(eid, sup.candidate, ref)
        self.publish()
        directory = self.art / "evaluations3" / eid
        directory.mkdir(parents=True, exist_ok=False)
        tree = directory / "tree"
        self._guard()
        candidate.materialize(selected, tree, bounds=self.bounds, owner_guard=sup.owner_current)
        if self.identity(tree) != key["candidate"]:
            raise ValueError("validation materialization changed")
        # DATA is not a fabricated Git baseline; the protected evaluator owns validator policy.
        descriptor = evidence.EvaluationSnapshot3(
            candidate_dir=directory, tree=tree, candidate_sha256=key["candidate"], artifact_ref=ref,
            conflict_run_id=eid if conflict else None)
        facts = evidence.evaluate_acceptance(
            descriptor, self.d.registry_raw, self.env, directory / "checks", self.d.manual,
            deadline=self.env.deadline, clock=self.d.clock, owner_guard=sup.owner_current,
            on_deadline=lambda: sup.request_stop("deadline"), identity_provider=self.identity)
        self._guard()
        if self.identity(tree) != key["candidate"] or sup.seed_receipt3 != key["seed_receipt"] \
                or sup.candidate != key["candidate"] or not sup.owner_current(epoch) \
                or not sup._barrier():
            raise ValueError("protected evaluation context changed")
        self.open_record(sup.snapshots3[key["seed_receipt"]])
        if facts.invalid_reason not in (None, "conflict"):
            sup.request_stop("invalid_evidence")
            return
        criteria = sorted(facts.satisfied_criteria) if facts.invalid_reason is None and not conflict else []
        protected = {"criteria": criteria}
        raw = contract.canonical(protected)
        with (directory / "criteria.json").open("xb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        if os.name != "nt":
            fd = os.open(directory, os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        self._guard()
        sup.finish_evaluation(eid, _sha(raw), quiescent=True)
        if sup.stop:
            return
        if improved:
            sup.register_evidence("candidate", self.env.criteria[0], sup.candidate)
        sup.record_acceptance(sup.candidate, protected, evaluation_id=eid)
        registry = contract.strict_json(self.d.registry_raw)
        unmet = set(self.env.criteria) - set(criteria)
        manual = set(registry["manual_criteria"])
        if unmet and unmet <= manual and all(
                check["status"] == "pass" for check in facts.checks
                if set(self.d.checks[check["check_id"]]) & unmet):
            sup.request_stop("operator_required")

    def workers(self, decision):
        self._guard()
        if any(not e["done"] for e in self.sup.evaluations.values()) \
                or any(a["lifecycle"] == "UNKNOWN" for a in self.sup.attempts.values()):
            return
        self.open_record(self.sup.snapshots3[self.sup.seed_receipt3])
        self.d._launch_workers(decision)

    def publish(self):
        if self.d.status_path is None:
            return
        try:
            task_status.publish_task_status(
                self.sup, self.d.status_path, owner_guard=self.sup.owner_current)
        except (OSError, ValueError, TypeError, KeyError):
            pass  # A failed UI observation neither grants nor revokes acceptance.

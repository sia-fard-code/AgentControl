"""Runtime adapter: one run's active view, phase meters, receipts and batch admission.

No filesystem or network here: journal/publisher/reader/external_bindings are the
run's own trusted callbacks. Metadata/repr carry counts, digests and codes only.
Context readiness is not permission, verification or execution proof.
"""
from __future__ import annotations

import copy
import hashlib
import json
import threading
from collections import OrderedDict

ROUTES = ("chat", "responses")
COMPONENTS = ("context_tokens", "context_receipts", "context_guard")
PHASES = ("worker", "helper", "advisor", "reviewer", "report", "compaction")
MAX_METERS = 8
AUTO_PIN_ROLES = ("system", "developer")
MEDIA_TYPES = ("image_url", "input_image", "image")


def _opt_callable(v):
    return v is None or callable(v)


class ContextRuntime:
    def __init__(self, policy, *, load_component, model, route, effort=None, image_type=None,
                 journal=None, publisher=None, reader=None, external_bindings=None):
        limit = getattr(policy, "input_limit_tokens", None)
        if (type(limit) is not int or limit <= 0 or not callable(load_component)
                or type(model) is not str or not model or route not in ROUTES
                or not (effort is None or type(effort) is str)
                or not (image_type is None or isinstance(image_type, type))
                or not all(_opt_callable(c) for c in (journal, publisher, reader, external_bindings))):
            raise ValueError("invalid_config")   # validated before any load or effect
        self.policy, self.model, self.route, self.effort = policy, model, route, effort
        self._image_type, self._journal, self._external = image_type, journal, external_bindings
        self._tokens, receipts, self._guard_mod = (load_component(n) for n in COMPONENTS)
        self._store = receipts.ReceiptStore(publisher=publisher, reader=reader)
        self._meters = OrderedDict()   # (phase, model, route) -> ContextMeter, bounded LRU
        self._rows = []                # [{"id", "msg", "pinned"}] active view, engine-owned ids
        self._groups = []              # complete batches: {"kind", "ids", "refs"}
        self._next_id = 0
        self._epoch = 0
        self._batch = None
        self._guard = None
        self._observed = {"produced_bytes": 0, "read_bytes": 0, "results": 0}   # run-wide
        self._compactions = 0
        # guards every active-view mutation and coherent read; never held across Guard,
        # journal, publisher or external callbacks, so it never nests (_project included)
        self._lock = threading.Lock()

    def __repr__(self):
        return f"ContextRuntime(messages={len(self._rows)}, epoch={self._epoch})"

    # -- active view --------------------------------------------------------------
    def _row(self, msg, pinned):
        self._next_id += 1
        return {"id": self._next_id, "msg": copy.deepcopy(msg), "pinned": pinned}

    def _auto_pin(self, msg):
        c = msg.get("content")
        media = isinstance(c, list) and any(
            (isinstance(b, dict) and b.get("type") in MEDIA_TYPES)
            or (self._image_type is not None and isinstance(b, self._image_type)) for b in c)
        return msg.get("role") in AUTO_PIN_ROLES or media

    def _emit(self, msg):
        if self._journal is not None:   # canonical record of actual messages only
            self._journal(copy.deepcopy(msg))

    def append(self, message, *, pinned=False):
        if not isinstance(message, dict) or type(message.get("role")) is not str:
            raise ValueError("invalid_message")
        if self._batch is not None:
            raise ValueError("batch_open")   # projected rows would be duplicated; use journal_only
        self._emit(message)   # caller-owned callback: outside the lock
        with self._lock:   # serialized with commit_view so a concurrent CAS cannot drop this row
            # shortcut: a begin_batch racing this call raises after the journal record (journal-only
            # semantics); callers own batch sequencing, upgrade to a reservation if that changes.
            if self._batch is not None:
                raise ValueError("batch_open")
            self._rows.append(self._row(message, bool(pinned) or self._auto_pin(message)))
            self._epoch += 1

    def journal_only(self, message):
        """Record an actual message canonically without touching the active view."""
        if not isinstance(message, dict) or type(message.get("role")) is not str:
            raise ValueError("invalid_message")
        self._emit(message)

    def payload_messages(self):
        return [copy.deepcopy(r["msg"]) for r in self._rows]

    # -- meters -------------------------------------------------------------------
    def meter_for(self, phase="worker", model=None, route=None):
        key = (phase, model or self.model, route or self.route)
        if phase not in PHASES or key[2] not in ROUTES or type(key[1]) is not str:
            raise ValueError("invalid_meter")
        meter = self._meters.pop(key, None) or self._tokens.ContextMeter(
            self.policy, route=key[2], model=key[1], image_type=self._image_type)
        self._meters[key] = meter
        primary = ("worker", self.model, self.route)   # worker R never evicted by other phases
        while len(self._meters) > MAX_METERS:
            self._meters.pop(next(k for k in self._meters if k != primary))
        return meter

    def prepare_request(self, payload, *, phase="worker", model=None, route=None):
        return self.meter_for(phase, model, route).estimate(payload)

    def observe_response(self, estimate, raw_usage, *, phase="worker", model=None, route=None):
        """Context calibration only (cached input counted once); billing stays with the caller."""
        tokens = self._tokens.input_tokens_from_usage(raw_usage, route=route or self.route)
        self.meter_for(phase, model, route).observe(estimate, input_tokens=tokens)
        return tokens

    # -- receipts -----------------------------------------------------------------
    def _reserve(self, slot_ids):
        res = self._store.reserve_batch(slot_ids)
        if self._batch is not None:
            self._batch["refs"] = [r.ref for r in res]   # own-run refs only
        return res

    def read_result(self, ref, offset=0, limit=4096):
        """Private pager for a future read_context_result tool; not authority of any kind."""
        return self._store.read(ref, offset, limit)

    # -- batch --------------------------------------------------------------------
    def begin_batch(self, payload, assistant, result_envelopes, slot_ids, *, kind="native"):
        if self._batch is not None or kind not in ("native", "prompt"):
            raise ValueError("invalid_state")
        if (not isinstance(payload, dict)
                or not isinstance(assistant, dict) or assistant.get("role") != "assistant"):
            raise ValueError("projection_error")
        ids, envs = list(slot_ids), [copy.deepcopy(e) for e in result_envelopes]
        if len(ids) != len(envs):
            raise ValueError("projection_error")
        with self._lock:   # payload check and base capture see one coherent active view
            if self._batch is not None:
                raise ValueError("invalid_state")
            if self._digest(payload.get("messages")) != self._digest([r["msg"] for r in self._rows]):
                raise ValueError("projection_error")   # payload must be exactly the active view
            group = [self._row(assistant, False)] + [self._row(e, False) for e in envs]
            self._batch = {"kind": kind, "base": list(self._rows), "group": group, "adds": [],
                           "slots": dict(zip(ids, group[1:])), "refs": [], "provisional": set(),
                           "done": set()}
        tap = type("_Tap", (), {})()
        tap.reserve_batch, tap.record = self._reserve, self._store.record
        self._guard = self._guard_mod.Guard(
            image_type=self._image_type, context=self.meter_for("worker"), receipts=tap,
            publish=self._project, reader_registered=True)
        try:
            ok = self._guard.start_batch([payload, assistant], len(ids),
                                         result_envelopes=envs, slot_ids=ids)
        except Exception:
            ok = False
        if not ok:   # nothing dispatched; active view and journal untouched
            with self._lock:
                self._rows, self._batch = self._batch["base"], None
            return False
        self._emit(assistant)   # canonical assistant once, only after a valid preflight
        return True

    def _slot(self, slot_id):
        if self._batch is None or slot_id not in self._batch["slots"]:
            raise ValueError("invalid_state")
        return self._batch["slots"][slot_id]["msg"]

    def admit_result(self, name, result, *, slot_id, outcome="returned", exit_code=None,
                     provisional=False, additions=(), compact=None):
        env = {k: v for k, v in self._slot(slot_id).items() if k != "content"}
        if not isinstance(result, str):
            raise ValueError("projection_error")
        if not provisional:   # actual FULL result, before and independent of the preview
            self._emit({**env, "content": str.__str__(result)})
        for a in additions:
            self._emit(a)
        if provisional:
            self._batch["provisional"].add(slot_id)
        self._batch["done"].add(slot_id)
        return self._guard.admit(name, result, envelope=env, additions=tuple(additions),
                                 compact=compact, slot_id=slot_id, outcome=outcome,
                                 exit_code=exit_code, provisional=provisional)

    def replace_result(self, name, previous, result, *, slot_id, outcome="returned",
                       exit_code=None, compact=None):
        env = {k: v for k, v in self._slot(slot_id).items() if k != "content"}
        if slot_id not in self._batch["provisional"] or not isinstance(result, str):
            raise ValueError("invalid_state")
        self._emit({**env, "content": str.__str__(result)})   # the final actual result
        shown = self._guard.replace_result(name, previous, result, envelope=env, slot_id=slot_id,
                                           compact=compact, outcome=outcome, exit_code=exit_code)
        self._batch["provisional"].discard(slot_id)
        return shown

    def _control_text(self, reason_code):
        if type(reason_code) is not str or not reason_code.isidentifier():
            raise ValueError("invalid_reason")   # fixed codes only; never echoed content
        return f"[not run] {reason_code}"

    def skip_result(self, slot_id, reason_code="not_run"):
        """Known not executed: keeps its receipt fallback; nothing runs here."""
        env = {k: v for k, v in self._slot(slot_id).items() if k != "content"}
        if slot_id in self._batch["done"]:
            raise ValueError("invalid_state")   # recorded outcomes are immutable
        self._emit({**env, "content": self._control_text(reason_code)})   # canonical control once
        self._batch["done"].add(slot_id)
        return self._guard.skip(slot_id=slot_id, reason_code=reason_code)

    def cancel_remaining(self, reason_code="cancelled"):
        """Close every open slot as known not executed, in API order; never runs an action.
        A provisional finish whose function never ran is finalized via its own slot."""
        b = self._batch
        for s in list(b["slots"]) if b else ():
            if s in b["provisional"]:
                self.replace_result("finish", "", self._control_text(reason_code), slot_id=s,
                                    outcome="skipped", exit_code=None)
            elif s not in b["done"]:
                self.skip_result(s, reason_code)

    # -- read-only status / system revision (caller-owned authority stays outside) ----
    @property
    def guard(self):
        return self._guard

    @property
    def batch_open(self):
        return self._batch is not None

    @property
    def stopped(self):
        return bool(self._guard is not None and self._guard.exhausted)

    @property
    def reason(self):
        return self._guard.reason if self._guard is not None else None

    def replace_system(self, message):
        """Revise ONLY the active first system row; canonical journal appends the revision."""
        if (not isinstance(message, dict) or message.get("role") != "system"
                or not self._rows or self._rows[0]["msg"].get("role") != "system"):
            raise ValueError("invalid_message")
        if self._batch is not None:
            raise ValueError("batch_open")
        self._emit(message)   # caller-owned callback: outside the lock
        with self._lock:   # serialized with commit_view; re-check the state the CAS may change
            if (self._batch is not None or not self._rows
                    or self._rows[0]["msg"].get("role") != "system"):
                raise ValueError("invalid_state")
            self._rows[0] = self._row(message, True)
            self._epoch += 1
    def before_action(self, slot_id):
        """Context readiness ONLY; permissions and unknown effects stay with the caller."""
        self._slot(slot_id)
        return self._guard.before_action(slot_id)

    def end_batch(self):
        b = self._batch
        if b is None or b["provisional"] or self._guard.pending:
            raise ValueError("invalid_state")
        self._fold()   # Guard read stays outside the lock
        with self._lock:   # group publication and batch close are one step for CAS/snapshots
            self._groups.append({"kind": b["kind"], "ids": [r["id"] for r in b["group"]],
                                 "refs": list(b["refs"])})
            self._batch = None

    def _fold(self):
        """Run-wide observed stats survive fresh Guards per batch and compaction."""
        obs = self._guard.metadata().get("observed", {})
        for k in self._observed:
            self._observed[k] += obs.get(k, 0)

    # -- snapshot / compaction CAS ------------------------------------------------
    def _canon(self, v, path=()):
        """JSON-safe canonical value; images by pixel SHA+mime/dims, never path/repr."""
        if v is None or type(v) in (bool, int, float, str):
            return v
        if id(v) in path:
            raise ValueError("unsupported_value")   # cycle
        path += (id(v),)
        if type(v) is dict and all(type(k) is str for k in v):
            return {k: self._canon(x, path) for k, x in v.items()}
        if type(v) in (list, tuple):
            return [self._canon(x, path) for x in v]
        if self._image_type is not None and type(v) is self._image_type:
            return {"$image": hashlib.sha256(bytes(v.data)).hexdigest(), "mime": v.mime,
                    "width": v.width, "height": v.height}
        raise ValueError("unsupported_value")   # fixed code; no repr of user objects

    def _digest(self, value):
        return hashlib.sha256(json.dumps(self._canon(value), sort_keys=True).encode()).hexdigest()

    def _read_external(self):   # caller-owned callback: never invoked while holding the lock
        return self._external() if self._external is not None else None

    def _bindings(self, ext):
        """Engine-semantic state only; no billing, timestamps or compaction artifacts."""
        return {"epoch": self._epoch, "messages_sha256": self._digest([r["msg"] for r in self._rows]),
                "policy_sha256": self._digest(repr(self.policy)), "route": self.route,
                "model": self.model, "effort": self.effort, "history_mode": "stateless",
                "batch_open": self._batch is not None, "external": ext,
                # engine-owned frozen prefix handle; set last so no caller value can override it
                "reference_index_ref": self._store.index_ref()}

    def _retained(self, g):
        """Groups whose receipts lack complete capture must stay active (retention, not authority)."""
        return not all(self._store.capture_complete(r) for r in g["refs"])

    def bindings(self):
        ext = self._read_external()   # outside the lock
        with self._lock:
            return self._bindings(ext)

    def snapshot_data(self):
        if self._batch is not None:
            raise ValueError("batch_open")   # compact only between complete batches
        ext = self._read_external()   # outside the lock
        with self._lock:   # messages, pins, groups and bindings describe one active view
            if self._batch is not None:
                raise ValueError("batch_open")
            pos = {r["id"]: i for i, r in enumerate(self._rows)}
            keep = {i for g in self._groups if self._retained(g) for i in g["ids"]}
            active = [g for g in self._groups if all(i in pos for i in g["ids"])]
            snap = {"messages": [copy.deepcopy(r["msg"]) for r in self._rows],
                    "pinned": [i for i, r in enumerate(self._rows)
                               if r["pinned"] or r["id"] in keep],
                    "groups": [tuple(pos[i] for i in g["ids"]) for g in active],
                    "group_kinds": [g["kind"] for g in active],   # same filtered order
                    "bindings": self._bindings(ext)}
        # engine-owned semantic state (never a model summary): counts/codes only
        snap["capsule"] = {"external": copy.deepcopy(ext), "receipts": self._store.metadata()}
        return snap

    def group_refs(self):
        """Own-run receipt refs per active group, aligned with snapshot_data()['groups']."""
        ids = {r["id"] for r in self._rows}
        return [list(g["refs"]) for g in self._groups if all(i in ids for i in g["ids"])]

    def commit_view(self, expected_bindings, candidate, artifact_ref=None, *, lineage):
        """Atomic CAS of ONLY the active view. lineage maps each candidate row to its source
        snapshot position (None = the one replacement memory row); identity is positional."""
        if self._batch is not None or not isinstance(candidate, list):
            return False
        ext = self._read_external()   # once, outside the lock
        with self._lock:   # internal re-check catches reentrant changes made by the callback
            if self._bindings(ext) != expected_bindings:
                return False
            if not all(isinstance(m, dict) and type(m.get("role")) is str for m in candidate):
                return False
            src, n = self._rows, len(self._rows)
            if (type(lineage) is not tuple or len(lineage) != len(candidate)
                    or lineage.count(None) != 1):
                return False
            kept = [i for i in lineage if i is not None]
            if not all(type(i) is int and 0 <= i < n for i in kept):
                return False
            omitted = set(range(n)) - set(kept)
            if not omitted:
                return False
            first = min(omitted)   # memory sits where the first omitted source row was
            if lineage != tuple([i for i in range(first) if i not in omitted] + [None]
                                + [i for i in range(first, n) if i not in omitted]):
                return False   # also rejects duplicates, reordering and misplaced None
            try:   # retained rows equal their indicated source rows (canonical value)
                if any(i is not None and self._digest(m) != self._digest(src[i]["msg"])
                       for m, i in zip(candidate, lineage)):
                    return False
            except ValueError:
                return False
            memory = candidate[lineage.index(None)]
            if (set(memory) != {"role", "content"} or memory["role"] != "assistant"
                    or type(memory["content"]) is not str or not memory["content"].strip()):
                return False
            at = {r["id"]: i for i, r in enumerate(src)}
            gone = [g for g in self._groups if any(at[i] in omitted for i in g["ids"])]
            if (sum(len(g["ids"]) for g in gone) != len(omitted)
                    or any(at[i] not in omitted for g in gone for i in g["ids"])):
                return False   # only whole existing groups; no ungrouped source omissions
            real = [g for g in self._groups if g["kind"] in ("native", "prompt")]
            if (any(src[i]["pinned"] for i in omitted) or any(self._retained(g) for g in gone)
                    or (real and real[-1] in gone)):
                return False   # pins, incomplete capture and newest real batch stay active
            new_id = self._next_id + 1   # one memory ID, allocated only on publication
            rows = [{"id": new_id, "msg": copy.deepcopy(memory), "pinned": False} if i is None
                    else src[i] for i in lineage]
            pos = {r["id"]: i for i, r in enumerate(rows)}
            groups = [g for g in self._groups if g not in gone]
            if any([pos[i] for i in g["ids"]] != list(range(pos[g["ids"][0]],
                                                          pos[g["ids"][0]] + len(g["ids"])))
                   for g in groups):
                return False   # surviving groups stay complete, contiguous and ordered
            was = {i for g in self._groups for i in g["ids"]}
            grouped = {i for g in groups for i in g["ids"]}
            for r in rows:   # replacement memory by engine row identity, never by its label
                m = r["msg"]
                if r["id"] in grouped or r["pinned"]:
                    continue
                if r["id"] in was:   # a kept row lost its group: identity swap would detach it
                    return False
                if m["role"] == "assistant" and not m.get("tool_calls") and type(m.get("content")) is str:
                    groups.append({"kind": "memory", "ids": [r["id"]], "refs": []})
            groups.sort(key=lambda g: pos[g["ids"][0]])   # newest real batch stays last
            self._groups, self._rows, self._next_id = groups, rows, new_id
            self._epoch += 1
            self._compactions += 1
            return True

    def metadata(self):
        return {"messages": len(self._rows), "pinned": sum(r["pinned"] for r in self._rows),
                "groups": len(self._groups), "epoch": self._epoch, "compactions": self._compactions,
                "observed": dict(self._observed), "meters": len(self._meters),
                "receipts": self._store.metadata(),
                "admission": self._guard.metadata() if self._guard is not None else None}

    def _project(self, packet):
        """Admission publisher: update ONLY the active view, atomically, keeping base/pins."""
        b = self._batch
        msgs = packet.get("messages") if isinstance(packet, dict) else None
        n_base, n_group = len(b["base"]), len(b["group"])
        if not isinstance(msgs, list) or len(msgs) < n_base + n_group:
            raise ValueError("projection_error")
        adds = msgs[n_base + n_group:]
        if len(adds) < len(b["adds"]):
            raise ValueError("projection_error")   # mandatory additions never disappear
        with self._lock:   # invoked only by Guard calls made outside the lock: never nested
            group = [{**r, "msg": copy.deepcopy(m)} for r, m in zip(b["group"], msgs[n_base:])]
            new_adds = b["adds"] + [self._row(m, True) for m in adds[len(b["adds"]):]]
            b["group"], b["adds"] = group, new_adds
            b["slots"] = {s: group[i + 1] for i, s in enumerate(b["slots"])}
            self._rows = b["base"] + group + new_adds
            self._epoch += 1

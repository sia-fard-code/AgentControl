"""Model-mode (token) admission for one tool batch: whole-packet measurement only.

Pure apart from trusted callbacks (counter, receipts, publisher); no filesystem.
Estimates are heuristic: a fit is not proof of actual model capacity.
"""
import copy
import itertools

MAX_BATCH = 128
CLIP_STEPS = (16384, 8192, 4096, 1024, 256)   # finite predetermined clip sizes (bytes)
COMPACT_MAX = 512
WHOLE_ONLY = ("read_command_output", "read_context_result")   # JSON: never prefix-sliced
# Capture may be partial (publisher fault/inline cap): promise only what was captured.
CLIP_NOTICE = ("\n[context] output clipped to fit the model context; the captured result and its"
               " content_complete flag are in this run's receipt; do not rerun to recover it.")
REASONS = ("model_window_full", "history_unobservable", "counter_error", "projection_error",
           "resource_error", "publisher_failed", "invalid_state")
SKIP_CODES = ("not_run", "admission_closing", "denied", "cancelled", "batch_aborted")   # closed, no content


def _utf8(text):
    """Same policy as ReceiptStore._encode, so lone surrogates never raise after an action."""
    try:
        return str.encode(text, "utf-8", "surrogateescape")
    except UnicodeEncodeError:
        return str.encode(text, "utf-8", "surrogatepass")


class AdmissionError(Exception):
    """Fixed reason code only; never packet text, refs or call ids."""

    def __init__(self, reason):
        self.reason = reason if reason in REASONS else "invalid_state"
        super().__init__(self.reason)


class ModelAdmission:
    def __init__(self, context, *, receipts, publish, reader_registered, preview_bytes=16384):
        limit = getattr(getattr(context, "policy", None), "input_limit_tokens", None)
        if not callable(getattr(context, "estimate", None)) or type(limit) is not int or limit <= 0:
            raise AdmissionError("counter_error")
        if not (callable(getattr(receipts, "reserve_batch", None))
                and callable(getattr(receipts, "record", None)) and callable(publish)):
            raise AdmissionError("invalid_state")
        if reader_registered is not True:
            raise AdmissionError("invalid_state")   # no fallback may point at an unwired reader
        if type(preview_bytes) is not int or preview_bytes <= 0:
            raise AdmissionError("invalid_state")
        self._ctx, self._receipts, self._publish = context, receipts, publish
        self._preview = preview_bytes
        self._base = None          # frozen request (earlier batches included), own deep copy
        self._slots = {}           # slot_id -> {"env", "ref", "fallback", "content", "state"}
        self._order = []
        self._additions = []       # mandatory additions, after all paired result slots
        self._safe = None          # selection map of R (all fallbacks)
        self._sel = None           # current selection map P (slot_id -> content)
        self._checkpoint = None    # unpublished R + mandatory additions when unadmissible
        self._active = None        # last successfully published packet
        self._epoch = None         # counting-epoch key that measured the active packet
        self._view_bytes = 0       # selected result bytes of the PUBLISHED packet
        self.reason = None
        self.observed = {"produced_bytes": 0, "read_bytes": 0, "results": 0}
        self.current_tokens = None

    def __repr__(self):
        return f"ModelAdmission(slots={len(self._order)}, reason={self.reason})"

    # -- packet helpers -------------------------------------------------------------
    def _packet(self, selection, additions):
        msgs = list(self._base["messages"])
        msgs += [{**self._slots[s]["env"], "content": selection[s]} for s in self._order]
        msgs += additions
        return copy.deepcopy({**self._base, "messages": msgs})

    def _fits(self, packet):
        try:
            est = self._ctx.estimate(packet)
            tokens, flags = est.tokens, est.flags
        except Exception:
            raise AdmissionError("counter_error") from None
        if type(tokens) is not int or tokens < 0 or not isinstance(flags, tuple):
            raise AdmissionError("counter_error")
        if "history_unobservable" in flags:
            raise AdmissionError("history_unobservable")   # unsafe, not "window full"
        limit = self._ctx.policy.input_limit_tokens
        return tokens <= limit, tokens

    def _stop(self, reason):
        self.reason = self.reason or reason
        return False

    def stop(self, reason):
        """Public close (e.g. provider rejection by the wrapper); an earlier reason is kept."""
        return self._stop(reason)

    def _commit(self, selection, additions):
        """Publish a whole measured packet; advance the active view only on success."""
        packet = self._packet(selection, additions)
        try:
            self._publish(copy.deepcopy(packet))
        except Exception:
            self._stop("publisher_failed")
            raise AdmissionError("publisher_failed") from None
        self._active, self._sel, self._additions = packet, dict(selection), list(additions)
        self._view_bytes = sum(len(_utf8(v)) for v in selection.values())   # published view

    # -- batch ----------------------------------------------------------------------
    def start_batch(self, request_obj, n_calls, *, result_envelopes, slot_ids):
        """Reserve receipts and measure the all-fallback packet R before any action."""
        if any(self._slots[s]["state"] in ("pending", "provisional") for s in self._order):
            raise AdmissionError("invalid_state")   # previous batch unfinished: change nothing
        if isinstance(request_obj, list):   # [wire payload, native assistant envelope] only
            if (len(request_obj) != 2 or not isinstance(request_obj[0], dict)
                    or not isinstance(request_obj[1], dict) or request_obj[1].get("role") != "assistant"):
                raise AdmissionError("projection_error")
            payload, assistant = request_obj
            extra = [assistant]
        elif isinstance(request_obj, dict):
            payload, extra = request_obj, []
        else:
            raise AdmissionError("projection_error")
        if not isinstance(payload.get("messages"), list):
            raise AdmissionError("projection_error")
        if type(n_calls) is not int or not 0 <= n_calls <= MAX_BATCH:
            raise AdmissionError("projection_error")   # before touching either iterable
        try:   # at most n_calls + 1 items: endless/oversized iterables stay within MAX_BATCH memory
            slot_ids = list(itertools.islice(slot_ids, n_calls + 1))
            envs = list(itertools.islice(result_envelopes, n_calls + 1))
        except Exception:
            raise AdmissionError("projection_error") from None
        if (len(slot_ids) != n_calls
                or len(envs) != n_calls or not all(isinstance(s, str) and s for s in slot_ids)
                or len(set(slot_ids)) != n_calls
                or not all(isinstance(e, dict) and e.get("content", "") == "" for e in envs)):
            raise AdmissionError("projection_error")
        envs = [{k: v for k, v in e.items() if k != "content"} for e in envs]   # empty preflight content
        if self.reason is not None:
            return False   # stopped earlier: reserve nothing, dispatch nothing
        # own snapshot: later live appends to the caller's lists are never counted twice
        base = copy.deepcopy({**payload, "messages": list(payload["messages"]) + extra})
        try:
            res = list(self._receipts.reserve_batch(tuple(slot_ids))) if n_calls else []
            ok = (len(res) == n_calls
                  and all(r.slot_id == s and isinstance(r.ref, str) and isinstance(r.fallback_text, str)
                          and r.fallback_text and len(r.fallback_text.encode("utf-8")) <= COMPACT_MAX
                          for r, s in zip(res, slot_ids)))
        except Exception:
            ok = False
        if not ok:
            return self._stop("resource_error")
        self._base, self._order, self._additions = base, slot_ids, []
        self._slots = {s: {"env": copy.deepcopy(e), "ref": r.ref, "fallback": r.fallback_text,
                           "state": "pending"} for s, e, r in zip(slot_ids, envs, res)}
        self._safe = {s: self._slots[s]["fallback"] for s in slot_ids}
        self._sel = dict(self._safe)
        try:
            self._epoch = self._key()
            fit, tokens = self._fits(self._packet(self._safe, []))
        except AdmissionError as exc:   # counter/history faults labelled apart from window full
            return self._stop(exc.reason)
        if not fit:
            return self._stop("model_window_full")   # nothing dispatched or published
        try:
            self._commit(self._safe, [])
        except AdmissionError:
            return False   # publisher failed: no action may run against an unpublished R
        self.current_tokens = tokens
        return True

    # -- results --------------------------------------------------------------------
    def _resolve(self, slot_id, envelope, states):
        if slot_id is None:   # implicit: next owed slot in API order
            slot_id = next((s for s in self._order if self._slots[s]["state"] == "pending"), None)
        slot = self._slots.get(slot_id) if isinstance(slot_id, str) else None
        if slot is None or slot["state"] not in states:
            raise AdmissionError("invalid_state")   # unknown, duplicate or finished slot
        if envelope is not None and (not isinstance(envelope, dict) or envelope.get("content", "") != ""
                                     or {k: v for k, v in envelope.items() if k != "content"} != slot["env"]):
            raise AdmissionError("projection_error")
        return slot_id, slot

    def _candidates(self, name, result, compact, fallback):
        raw = _utf8(result)
        if len(raw) <= self._preview:   # declared per-result inline bound
            yield result
        if callable(compact):
            compact = compact()   # lazy: built only when the full result did not fit
        if isinstance(compact, str) and compact and len(_utf8(compact)) <= COMPACT_MAX:
            yield compact
        if name not in WHOLE_ONLY:
            notice = len(CLIP_NOTICE.encode("utf-8"))
            for k in (s for s in CLIP_STEPS if notice < s <= self._preview):
                if len(raw) > k:   # clip + notice stays within k bytes
                    yield raw[:k - notice].decode("utf-8", "ignore") + CLIP_NOTICE
        yield fallback   # fixed local fallback; whole-R restore only if even this fails

    def _select(self, slot_id, name, result, compact, additions):
        """First candidate whose WHOLE packet fits; else whole R; else stop unpublished."""
        adds = self._additions + [copy.deepcopy(a) for a in additions]
        fallback = self._slots[slot_id]["fallback"]
        try:
            if self.reason is None:
                for cand in self._candidates(name, result, compact, fallback):
                    sel = {**self._sel, slot_id: cand}
                    fit, tokens = self._fits(self._packet(sel, adds))
                    if fit:
                        self._commit(sel, adds)
                        self.current_tokens = tokens
                        return cand
                # no local candidate fits: non-monotone counts may need ALL slots back at R
                fit, tokens = self._fits(self._packet(self._safe, adds))
                if fit:
                    self._commit(self._safe, adds)
                    self.current_tokens = tokens
                    return fallback
                self._checkpoint = self._packet(self._safe, adds)   # never published
                self._stop("model_window_full")
            elif additions:
                self._checkpoint = self._packet(self._safe, adds)
        except AdmissionError as exc:
            self._stop(exc.reason)
        except Exception:
            self._stop("projection_error")
        return self._sel.get(slot_id, fallback)   # what the active (published) packet holds

    def _record(self, slot, name, result, outcome, exit_code, final):
        # seal first: the action already ran, so nothing below may leave the slot runnable
        slot["state"] = "executed" if final else "provisional"   # never back to pending
        slot["outcome"] = outcome
        try:   # store requires base str; the caller's subclass object stays untouched for selection
            self._receipts.record(slot["ref"], str.__str__(result), outcome=outcome,
                                  exit_code=exit_code, final=final)
        except Exception:
            self._stop("resource_error")   # explicit capture fault; effects stand, no replay
        try:
            raw = len(_utf8(result))
            self.observed["produced_bytes"] += raw
            self.observed["read_bytes"] += raw if name in ("read_file", "load_skill") + WHOLE_ONLY else 0
            self.observed["results"] += 1
        except Exception:
            self._stop("resource_error")

    def admit(self, name, result, *, slot_id=None, envelope=None, additions=(), compact=None,
              outcome="returned", exit_code=None, provisional=False):
        if not isinstance(result, str):
            raise AdmissionError("projection_error")
        slot_id, slot = self._resolve(slot_id, envelope, ("pending",))
        self._record(slot, name, result, None if provisional else outcome, exit_code, not provisional)
        return self._select(slot_id, name, result, compact, list(additions))

    def replace_result(self, name, previous, result, *, slot_id, envelope=None, compact=None,
                       outcome="returned", exit_code=None):
        """Finalize a provisional slot by identity (never text search); no re-execution."""
        if not isinstance(result, str) or not isinstance(previous, str):
            raise AdmissionError("projection_error")
        slot_id, slot = self._resolve(slot_id, envelope, ("provisional",))
        self._record(slot, name, result, outcome, exit_code, True)
        return self._select(slot_id, name, result, compact, [])

    def skip(self, slot_id, reason_code):
        """Known not-executed: finalize its receipt; the slot keeps its R fallback."""
        if not isinstance(slot_id, str):
            raise AdmissionError("invalid_state")   # explicit identity only; never "next owed"
        _, slot = self._resolve(slot_id, None, ("pending",))
        code = reason_code if isinstance(reason_code, str) and reason_code in SKIP_CODES else "not_run"
        slot["state"], slot["outcome"] = "skipped", "skipped"   # known not executed
        try:
            self._receipts.record(slot["ref"], f"[not run: {code}]", outcome="skipped", final=True)
        except Exception:
            self._stop("resource_error")
        return slot["fallback"]

    def _key(self):
        """Counting epoch: explicit int epoch/revision, else meter identity/observations/calibration."""
        ctx = self._ctx
        limit = ctx.policy.input_limit_tokens
        for attr in ("epoch", "revision"):
            if type(getattr(ctx, attr, None)) is int:
                return (id(ctx), limit, attr, getattr(ctx, attr))
        meta = getattr(ctx, "metadata", None)
        if meta is None:
            return (id(ctx), limit)   # pure fake: stable identity
        try:
            meta = meta() if callable(meta) else meta
            # "estimates" is ignored: counting itself increments it
            return (id(ctx), limit, repr([meta.get(k) for k in
                                          ("identity_sha256", "observations", "calibration_samples")]))
        except Exception:
            raise AdmissionError("counter_error") from None

    def _remeasure(self):
        """Epoch changed: keep P if it still fits, else publish whole R, else stop unpublished."""
        adds = self._additions
        try:
            key = self._key()
            fit, tokens = self._fits(self._packet(self._sel, adds))
            if not fit:
                fit, tokens = self._fits(self._packet(self._safe, adds))
                if not fit:
                    self._checkpoint = self._packet(self._safe, adds)   # never published
                    return self._stop("model_window_full")
                self._commit(self._safe, adds)
        except AdmissionError as exc:
            return self._stop(exc.reason)
        self.current_tokens, self._epoch = tokens, key
        return True

    def before_action(self, slot_id):
        """CONTEXT readiness only (not permission or verification) before running one slot."""
        if not isinstance(slot_id, str):
            raise AdmissionError("invalid_state")
        self._resolve(slot_id, None, ("pending",))
        if self.reason is not None:
            return False
        try:
            same = self._key() == self._epoch
        except AdmissionError as exc:
            return self._stop(exc.reason)
        return True if same else self._remeasure()

    def rebind(self, context):
        """New counting epoch: an old measurement proves nothing; re-measure now."""
        limit = getattr(getattr(context, "policy", None), "input_limit_tokens", None)
        if not callable(getattr(context, "estimate", None)) or type(limit) is not int or limit <= 0:
            raise AdmissionError("counter_error")
        self._ctx = context
        if self.reason is not None:
            return False   # stopped reason is never silently cleared
        if self._base is None:
            return True
        return self._remeasure()

    # -- views ----------------------------------------------------------------------
    def closing(self):
        return self.reason is not None

    @property
    def exhausted(self):
        return self.reason is not None

    @property
    def pending(self):
        return sum(1 for s in self._order if self._slots[s]["state"] == "pending")

    @property
    def packet(self):
        return copy.deepcopy(self._active)

    def checkpoint_payload(self):
        """Unpublished R + mandatory additions, for later maintenance/local report only."""
        return copy.deepcopy(self._checkpoint)

    def metadata(self):
        """Counts and codes only: no content, refs or call ids."""
        states = [self._slots[s]["state"] for s in self._order]
        return {"mode": "model", "reason": self.reason, "estimated": True,
                "current_tokens": self.current_tokens,
                "input_limit_tokens": self._ctx.policy.input_limit_tokens,
                "observed": dict(self.observed),
                "producer_bounds": {"batch_slots": MAX_BATCH, "preview_bytes": self._preview,
                                    "compact_bytes": COMPACT_MAX},
                "slots": {k: states.count(k) for k in ("pending", "provisional", "executed", "skipped")},
                "selected_active_bytes": self._view_bytes,
                "additions": len(self._additions)}

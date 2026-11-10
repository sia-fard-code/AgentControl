"""Deterministic context admission for one delegated run (stdlib only).

Byte counts are conservative guardrails, NOT measured provider token capacity:
UTF-8 bytes of the JSON request, with each inline image charged a fixed estimate
instead of its base64 wire size.
"""
from __future__ import annotations

import json
import re

REQUEST_LIMIT = 256 * 1024   # whole-request accounting ceiling
CLOSEOUT_RESERVE = 64 * 1024  # kept free for the final no-tool report
READ_BUDGET = 128 * 1024     # cumulative read_file/load_skill text
OUTPUT_BUDGET = 192 * 1024   # cumulative tool output of every kind
BATCH_BUDGET = 64 * 1024     # tool output within one assistant batch
COMPACT_RESULT = 512         # reserved per call before dispatch
IMAGE_COST = 8 * 1024        # per inline image, an estimate of its context use
READ_TOOLS = ("read_file", "load_skill")
PROFILES = ("legacy", "resilient-v1")
PAGED_TOOLS = ("read_file", "list_dir", "grep")  # text only; never commands/images/skills
CLIP_NOTICE = ("\n[context budget] output clipped to this run's local allowance; "
               "narrow the request (line range, path or pattern) to see more.")


def _is_read(name):
    # read_command_output pages stored bytes: charged as READ like read_file
    return name in READ_TOOLS or name in ("read_command_output", "read_context_result", "read_files")


class ContextBudgetError(Exception):
    """Safe control-flow signal; never contains request or provider text."""

    def __init__(self, reason="request", *, provider=False):
        super().__init__("context budget exhausted")
        self.reason = reason
        self.provider = provider


def request_bytes(obj, *, image_type=None):
    """Conservative JSON/UTF-8 estimate, with fixed estimated image cost.

    The caller supplies its supported asset class; arbitrary objects must fail
    rather than masquerade as images. No image bytes or metadata are serialized.
    """
    images = 0
    def default(value):
        nonlocal images
        if image_type is not None and isinstance(value, image_type):
            images += 1
            return ""
        raise TypeError("unsupported context accounting input")
    text = obj if isinstance(obj, str) else json.dumps(obj, default=default)
    return len(text.encode("utf-8")) + images * IMAGE_COST


def _model_fits(obj, context):
    """Whole-object token estimate vs policy.input_limit_tokens; faults never fall back to bytes."""
    limit = getattr(getattr(context, "policy", None), "input_limit_tokens", None)
    try:
        est = context.estimate(obj)
        tokens, flags = est.tokens, est.flags
    except Exception:
        raise ContextBudgetError("counter_error") from None
    if (type(limit) is not int or limit <= 0 or type(tokens) is not int or tokens < 0
            or not isinstance(flags, tuple)):
        raise ContextBudgetError("counter_error")
    if "history_unobservable" in flags:
        raise ContextBudgetError("history_unobservable")   # unsafe, not "window full"
    return tokens <= limit


def fits(obj, limit, *, image_type=None, context=None):
    if context is not None:   # model mode: the legacy byte limit is not model capacity
        return _model_fits(obj, context)
    return request_bytes(obj, image_type=image_type) <= limit


_ADMISSION = None
_MODEL_HOOKS = ("start_batch", "admit", "replace_result", "skip", "before_action", "stop",
                "closing", "metadata")


def _admission_module():
    """Sibling context_admission.py beside this file, never an ambient installed module."""
    global _ADMISSION
    if _ADMISSION is None:
        import importlib.util
        import os
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "context_admission.py")
        try:
            spec = importlib.util.spec_from_file_location("_agentcontrol_context_admission", path)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
        except Exception:
            raise ContextBudgetError("invalid_state") from None
        cls = getattr(mod, "ModelAdmission", None)
        if not callable(cls) or not all(callable(getattr(cls, h, None)) for h in _MODEL_HOOKS):
            raise ContextBudgetError("invalid_state")
        _ADMISSION = mod
    return _ADMISSION


class Guard:
    """Cumulative and per-batch output budgets plus request headroom."""

    def __init__(self, *, image_type=None, profile="legacy", context=None, receipts=None,
                 publish=None, reader_registered=False):
        if profile not in PROFILES:
            raise ValueError("unknown execution profile")
        self.profile = profile
        self.image_type = image_type
        self.read = self.output = self.refusals = 0
        self.batch = self.base = self.rejected = self.pending = 0
        self.exhausted = self.provider_rejected = False
        self.reason = None
        self.report_source = "local"
        self._m = None   # model mode: composed ModelAdmission; validated before any action
        if context is not None:
            self._m = _admission_module().ModelAdmission(
                context, receipts=receipts, publish=publish, reader_registered=reader_registered)
            self._sync()

    def _sync(self, value=None):
        """Model mode units: read/output = observed bytes, base = estimated tokens (None = unknown)."""
        m = self._m
        self.read, self.output = m.observed["read_bytes"], m.observed["produced_bytes"]
        self.base, self.batch = m.current_tokens, None   # no per-batch byte budget in model mode
        self.pending, self.reason, self.exhausted = m.pending, m.reason, m.exhausted
        return value

    def before_action(self, slot_id):
        """Context readiness only, never permission or verification."""
        if self._m is None:
            return not self.exhausted
        return self._sync(self._m.before_action(slot_id))

    def stop(self, reason, *, provider=False):
        if self._m is not None:
            self._m.stop(reason)
            self.provider_rejected |= provider
            return self._sync()
        self.exhausted = True
        self.reason = self.reason or reason
        self.provider_rejected |= provider

    def start_batch(self, request_obj, n_calls, *, result_envelopes=(), slot_ids=None):
        """Reserve a compact, paired result for every admitted native call."""
        if self._m is not None:   # explicit stable slot ids required (none needed for n=0)
            if slot_ids is None and n_calls == 0:
                slot_ids = ()
            return self._sync(self._m.start_batch(request_obj, n_calls,
                                                  result_envelopes=result_envelopes, slot_ids=slot_ids))
        self.batch = 0
        self.pending = n_calls
        if self.profile != "legacy":  # every paired compact slot must fit before any action
            if type(n_calls) is not int or n_calls < 0:
                raise ValueError("invalid call count")
            if self.output + n_calls * COMPACT_RESULT > OUTPUT_BUDGET:
                self.stop("cumulative tool output")
            elif n_calls * COMPACT_RESULT > BATCH_BUDGET:
                self.stop("per-batch output")
        self.base = (request_bytes(request_obj, image_type=self.image_type)
                     + n_calls * COMPACT_RESULT
                     + sum(request_bytes(e) for e in result_envelopes))
        if self.base > REQUEST_LIMIT - CLOSEOUT_RESERVE:
            self.stop("request context")
        return not self.exhausted

    def _charge(self, name, result, envelope):
        size = len(result.encode("utf-8"))
        if _is_read(name):
            self.read += size
        self.output += size
        self.batch += size
        self.pending = max(self.pending - 1, 0)
        self.base += request_bytes(envelope, image_type=self.image_type) - COMPACT_RESULT
        return result

    def skip(self, envelope=None, text=None, *, slot_id=None, reason_code="not_run"):
        if self._m is not None:   # explicit protected slot; it keeps its receipt fallback
            return self._sync(self._m.skip(slot_id, reason_code))
        result = text or "[context budget] not run: an earlier budget was exhausted."
        if self.profile != "legacy":
            return self._control(result, envelope)
        return self._charge("", result, {**(envelope or {}), "content": result})

    def _control(self, text, envelope):
        """Resilient control text: longest prefix within the reserved slot and residuals.

        Charged as control output, never as source READ bytes; may be empty when
        nothing is left (closing metadata then carries the truth)."""
        later = max(self.pending - 1, 0) * COMPACT_RESULT
        cap = min(COMPACT_RESULT, OUTPUT_BUDGET - self.output - later,
                  BATCH_BUDGET - self.batch - later)
        room = REQUEST_LIMIT - CLOSEOUT_RESERVE - self.base + COMPACT_RESULT

        def ok(k):
            t = text[:k]
            return len(t.encode("utf-8")) <= cap and request_bytes(
                {**(envelope or {}), "content": t}, image_type=self.image_type) <= room

        lo, hi = 0, len(text)
        while lo < hi:
            mid = (lo + hi + 1) // 2
            lo, hi = (mid, hi) if ok(mid) else (lo, mid - 1)
        text = text[:lo]
        return self._charge("", text, {**(envelope or {}), "content": text})

    def _clip(self, name, result, envelope, extra):
        """Longest text prefix + notice fitting every residual budget, else None."""
        later = max(self.pending - 1, 0) * COMPACT_RESULT  # paired results still owed
        cap = min(BATCH_BUDGET // 4, BATCH_BUDGET - later - self.batch,
                  OUTPUT_BUDGET - self.output - later)
        if _is_read(name):
            cap = min(cap, READ_BUDGET - self.read)
        room = REQUEST_LIMIT - CLOSEOUT_RESERVE - self.base - extra + COMPACT_RESULT

        def ok(k):
            text = result[:k] + CLIP_NOTICE
            return (len(text.encode("utf-8")) <= cap and request_bytes(
                {**(envelope or {}), "content": text}, image_type=self.image_type) <= room)

        lo, hi = 0, len(result)
        while lo < hi:  # largest k with ok(k); sizes grow monotonically with k
            mid = (lo + hi + 1) // 2
            lo, hi = (mid, hi) if ok(mid) else (lo, mid - 1)
        return result[:lo] + CLIP_NOTICE if lo and ok(lo) else None  # all text gone = refuse

    def _compact_fits(self, name, compact, envelope, extra):
        """Whole compact recovery text (never prefix-cut) within every residual."""
        size = len(compact.encode("utf-8"))
        later = max(self.pending - 1, 0) * COMPACT_RESULT
        room = REQUEST_LIMIT - CLOSEOUT_RESERVE - self.base - extra + COMPACT_RESULT
        return (size <= COMPACT_RESULT
                and self.output + size + later <= OUTPUT_BUDGET
                and self.batch + size + later <= BATCH_BUDGET
                and not (_is_read(name) and self.read + size > READ_BUDGET)
                and request_bytes({**(envelope or {}), "content": compact},
                                  image_type=self.image_type) <= room)

    def admit(self, name, result, *, envelope=None, additions=(), compact=None, slot_id=None,
              outcome="returned", exit_code=None, provisional=False):
        """Charge delivered envelopes, including refusals; never echo refused text.

        compact: resilient-only whole fallback (<= COMPACT_RESULT), or a zero-arg
        callable returning it, used instead of refusing when the full result is
        over a residual budget. Never prefix-cut."""
        if self._m is not None:
            return self._sync(self._m.admit(name, result, slot_id=slot_id, envelope=envelope,
                                            additions=additions, compact=compact, outcome=outcome,
                                            exit_code=exit_code, provisional=provisional))
        size = len(result.encode("utf-8"))
        env = {**(envelope or {}), "content": result}
        cost = request_bytes(env, image_type=self.image_type)
        extra = request_bytes(additions, image_type=self.image_type) if additions else 0
        over = self.reason if self.exhausted else None
        # resilient: later paired compact results keep their reserved slots
        later = 0 if self.profile == "legacy" else max(self.pending - 1, 0) * COMPACT_RESULT
        if not over:
            if self.base + cost + extra - COMPACT_RESULT > REQUEST_LIMIT - CLOSEOUT_RESERVE:
                over = "request context"
            elif _is_read(name) and self.read + size > READ_BUDGET:
                over = "cumulative read"
            elif self.output + size + later > OUTPUT_BUDGET:
                over = "cumulative tool output"
            elif self.batch + size + later > BATCH_BUDGET:
                over = "per-batch output"
        if over and not self.exhausted and self.profile != "legacy" and name in PAGED_TOOLS:
            clipped = self._clip(name, result, envelope, extra)
            if clipped is not None:
                over, result = None, clipped
                env = {**(envelope or {}), "content": result}
        if over and not self.exhausted and self.profile != "legacy" and callable(compact):
            compact = compact()   # lazy: built (and published) only when needed
        if (over and not self.exhausted and self.profile != "legacy"
                and isinstance(compact, str) and compact
                and self._compact_fits(name, compact, envelope, extra)):
            # whole refs/rc/cursor survive; previews dropped, nothing latched
            over, result = None, compact
            env = {**(envelope or {}), "content": result}
        if over:
            self.stop(over)
            self.refusals += 1
            self.rejected += size + extra
            head = result.split("\n", 1)[0]
            head = head + "\n" if name == "run_command" and re.fullmatch(r"exit=-?\d+", head) else ""
            result = head + f"[context budget] output refused: {over} budget exhausted; action not re-run."
            env = {**(envelope or {}), "content": result}
            if self.profile != "legacy":
                return self._control(result, envelope)
        else:
            self.base += extra
        return self._charge(name, result, env)

    def replace_result(self, name, previous, result, *, envelope=None, slot_id=None, compact=None,
                       outcome="returned", exit_code=None):
        """Re-admit a deferred finish decision instead of charging its old 'ok'."""
        if self._m is not None:   # finalize the provisional slot by identity; no re-execution
            return self._sync(self._m.replace_result(name, previous, result, slot_id=slot_id,
                                                     envelope=envelope, compact=compact,
                                                     outcome=outcome, exit_code=exit_code))
        size = len(previous.encode("utf-8"))
        if _is_read(name):
            self.read -= size
        self.output -= size
        self.batch -= size
        self.base -= request_bytes({**(envelope or {}), "content": previous},
                                   image_type=self.image_type) - COMPACT_RESULT
        self.pending += 1  # the deferred call's slot is re-used, not consumed twice
        return self.admit(name, result, envelope=envelope)

    def closing(self):
        if self._m is not None:
            return self._sync(self._m.closing())
        return self.exhausted

    def metadata(self):
        if self._m is not None:   # counts and codes only: no contents, refs or slot ids
            self._sync()
            return {**self._m.metadata(), "context_policy": "model-v1",
                    "units": {"current_tokens": "estimated_tokens", "input_limit_tokens": "tokens",
                              "observed": "bytes", "producer_bounds": "slots/bytes"},
                    "provider_rejected": self.provider_rejected, "report_source": self.report_source}
        return {"reason": self.reason, "usage": {"request": self.base, "read": self.read,
                "output": self.output, "batch": self.batch, "rejected": self.rejected,
                "refusals": self.refusals}, "limits": {"request": REQUEST_LIMIT,
                "reserve": CLOSEOUT_RESERVE, "read": READ_BUDGET,
                "output": OUTPUT_BUDGET, "batch": BATCH_BUDGET},
                "report_source": self.report_source}

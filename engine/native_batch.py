"""Pure structural validation of native multi-tool batches plus protocol-health tracking.

No dispatch, I/O or authority: callers still apply per-call policy at dispatch time.
Reasons are a fixed safe enum; IDs, names and arguments never appear in them.
"""
import json
import math
from dataclasses import dataclass


@dataclass(frozen=True)
class NativeCall:
    id: str
    name: str
    args: dict


@dataclass(frozen=True)
class BatchVerdict:
    ok: bool
    reason: str
    calls: tuple = ()


def _no_dup_pairs(pairs):
    out = {}
    for k, v in pairs:
        if k in out:
            raise ValueError("duplicate key")
        out[k] = v
    return out


def _reject_constant(_):
    raise ValueError("non-finite constant")


def _finite_float(raw):
    value = float(raw)
    if not math.isfinite(value):
        raise ValueError("non-finite number")
    return value


def _parse_args(raw):
    if not isinstance(raw, str):
        return None
    try:
        val = json.loads(raw, object_pairs_hook=_no_dup_pairs,
                         parse_constant=_reject_constant, parse_float=_finite_float)
    except (ValueError, RecursionError):
        return None
    return val if isinstance(val, dict) else None


def _fail(reason):
    return BatchVerdict(False, reason, ())


def validate_batch(calls, *, batch_cap, check_arguments=None):
    """Validate the whole batch before anything runs; any bad entry rejects all of it."""
    if type(batch_cap) is not int or batch_cap < 1:
        return _fail("invalid_cap")
    if not isinstance(calls, list):
        return _fail("not_a_list")
    if not calls:
        return _fail("empty_batch")
    if len(calls) > batch_cap:
        return _fail("over_cap")
    parsed, seen = [], set()
    for c in calls:
        if not isinstance(c, dict):
            return _fail("bad_shape")
        cid = c.get("id")
        if not isinstance(cid, str) or not cid:
            return _fail("missing_id")
        if cid in seen:
            return _fail("duplicate_id")
        seen.add(cid)
        fn = c.get("function")
        if c.get("type") != "function" or not isinstance(fn, dict):
            return _fail("bad_shape")
        name = fn.get("name")
        if not isinstance(name, str) or not name:
            return _fail("bad_name")
        args = _parse_args(fn.get("arguments"))
        if args is None:
            return _fail("malformed_arguments")
        parsed.append(NativeCall(cid, name, args))
    if check_arguments is not None:
        for nc in parsed:
            try:
                check_arguments(nc.name, nc.args)
            except Exception:  # callback text may echo user data; keep the reason bounded
                return _fail("invalid_arguments")
    return BatchVerdict(True, "ok", tuple(parsed))


REASONS = frozenset(("reply_cut", "prose_only", "no_tool_block", "invalid_arguments",
                     "multiple_tool_calls", "no_action", "protocol_error"))


class RepairHealth:
    """Local wire health vs task progress. Advisory only: grants no retries, caps or work."""

    def __init__(self, limit=2):
        if type(limit) is not int or limit < 0:
            raise ValueError("limit must be a non-negative int")
        self.limit = limit
        self.consecutive = 0
        self.total = 0
        self.reasons = {}
        self.progress_seq = 0
        self._since_progress = set()  # incident reasons seen since the last real progress
        self._recurring = False

    def incident(self, reason):
        """Record a genuine protocol incident; True if another LOCAL repair is allowed."""
        key = reason if reason in REASONS else "other"
        self.consecutive += 1
        self.total += 1
        self.reasons[key] = self.reasons.get(key, 0) + 1
        if key in self._since_progress:
            self._recurring = True
        self._since_progress.add(key)
        return self.consecutive <= self.limit

    def action(self, *, valid=True, known=True, task_changed=False, evidence_changed=False):
        if not (valid and known):
            return  # unknown or invalid effect proves nothing about the wire or the task
        self.consecutive = 0
        if task_changed or evidence_changed:
            self.progress_seq += 1
            self._since_progress.clear()
            self._recurring = False

    def metadata(self):
        return {
            "limit": self.limit,
            "consecutive": self.consecutive,
            "total_incidents": self.total,
            "reasons": dict(self.reasons),
            "progress_seq": self.progress_seq,
            "diagnostic_needed": self.consecutive > self.limit or self._recurring,
        }

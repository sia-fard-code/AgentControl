"""Immutable fallback envelopes and bounded receipts for tool results.

Descriptive data only: never approval, verification, or execution proof.
No IO here; publisher/reader are trusted runtime adapters.
"""
from __future__ import annotations

import base64
import hashlib
import inspect
import itertools
import json
import re
import secrets
from dataclasses import dataclass, field

_CODES = frozenset({
    "invalid_config", "invalid_batch", "duplicate_slot", "capacity",
    "invalid_ref", "invalid_argument", "invalid_transition", "read_failed",
})
_SLOT_RE = re.compile(r"[A-Za-z0-9._:-]{1,128}")
_INDEX_HW_RE = re.compile(r"0|[1-9][0-9]{0,5}")  # canonical ASCII; bounded before int()
_BLOCK = 4096
_MAX_BATCH = 128
_MAX_INLINE_PER_RECEIPT = 16384
_MAX_INLINE_TOTAL = 64 * 1024 * 1024
_MAX_HASH_TOTAL = 64 * 1024 * 1024
_MAX_RECEIPTS = 65536
_FINAL_OUTCOMES = ("returned", "skipped", "unknown")


class ReceiptError(ValueError):
    """Fixed closed code only; never echoes bodies, refs or backend errors."""

    def __init__(self, code: str):
        super().__init__(code if code in _CODES else "invalid_argument")
        self.code = self.args[0]


@dataclass(frozen=True)
class Reservation:
    slot_id: str = field(repr=False)
    ref: str = field(repr=False)
    fallback_text: str = field(repr=False)


def _strict_int(value, low, high):
    return type(value) is int and low <= value <= high


class _Receipt:
    __slots__ = ("seq", "state", "outcome", "exit_code", "inline", "blob_key",
                 "hashes", "total_bytes", "stored_bytes", "complete")

    def __init__(self, seq):
        self.seq = seq
        self.state = "pending"
        self.outcome = self.exit_code = self.inline = self.blob_key = None
        self.hashes = self.total_bytes = self.stored_bytes = None
        self.complete = False


class ReceiptStore:
    def __init__(self, *, publisher=None, reader=None, max_receipts=4096,
                 inline_bytes=16384, max_body_bytes=1048576):
        if (publisher is None) != (reader is None):
            raise ReceiptError("invalid_config")
        if publisher is not None and not (callable(publisher) and callable(reader)):
            raise ReceiptError("invalid_config")
        if publisher is not None and (inspect.iscoroutinefunction(publisher)
                                      or inspect.iscoroutinefunction(reader)):
            raise ReceiptError("invalid_config")
        if not (_strict_int(max_receipts, 1, _MAX_RECEIPTS)
                and _strict_int(inline_bytes, 1, _MAX_INLINE_PER_RECEIPT)
                and _strict_int(max_body_bytes, inline_bytes, _MAX_INLINE_TOTAL)):
            raise ReceiptError("invalid_config")
        blocks = -(-max_body_bytes // _BLOCK)
        if (max_receipts * inline_bytes > _MAX_INLINE_TOTAL
                or max_receipts * blocks * 32 > _MAX_HASH_TOTAL):
            raise ReceiptError("invalid_config")
        self._publisher, self._reader = publisher, reader
        self._max_receipts = max_receipts
        self._inline_bytes = inline_bytes
        self._max_body = max_body_bytes
        self._scope = secrets.token_hex(16)
        self._index_prefix = f"result://{self._scope}/index/"
        # Constant line width: the longest ordinary ref this store can issue, plus LF.
        self._index_line = len(f"result://{self._scope}/{max_receipts}") + 1
        self._receipts: dict[str, _Receipt] = {}
        self._slots: set[str] = set()
        # Refs inside a callback-bearing record/read; blocks same-ref reentry
        # from callbacks. shortcut: single-thread reentry only, not a lock.
        self._busy: set[str] = set()

    def __repr__(self):
        return f"ReceiptStore(receipts={len(self._receipts)})"

    def _fallback(self, ref):
        return json.dumps({
            "result_ref": ref,
            "reader_tool": "read_context_result",
            "note": "Read the recorded result with read_context_result."
                    " Do not rerun the action.",
        }, separators=(",", ":"))

    def reserve_batch(self, slot_ids):
        if isinstance(slot_ids, (str, bytes)):
            raise ReceiptError("invalid_batch")
        try:  # islice ignores __length_hint__ and stops at cap+1
            batch = list(itertools.islice(iter(slot_ids), _MAX_BATCH + 1))
        except Exception:
            raise ReceiptError("invalid_batch") from None
        if not 1 <= len(batch) <= _MAX_BATCH:
            raise ReceiptError("invalid_batch")
        for slot in batch:
            if type(slot) is not str or not _SLOT_RE.fullmatch(slot):
                raise ReceiptError("invalid_batch")
        if len(set(batch)) != len(batch) or not self._slots.isdisjoint(batch):
            raise ReceiptError("duplicate_slot")
        if len(self._receipts) + len(batch) > self._max_receipts:
            raise ReceiptError("capacity")
        out = []
        for slot in batch:
            seq = len(self._receipts) + len(out) + 1
            ref = f"result://{self._scope}/{seq}"
            out.append((ref, _Receipt(seq), Reservation(slot, ref, self._fallback(ref))))
        for ref, rec, _ in out:
            self._receipts[ref] = rec
        self._slots.update(batch)
        return tuple(r for _, _, r in out)

    def _lookup(self, ref):
        rec = self._receipts.get(ref) if type(ref) is str else None
        if rec is None:
            raise ReceiptError("invalid_ref")
        return rec

    def index_ref(self):
        """Handle for the frozen, immutable reservation prefix 1..H (not a live list)."""
        return f"{self._index_prefix}{len(self._receipts)}"

    def capture_complete(self, ref):
        """True only for a final recorded receipt with complete capture; no backend IO."""
        rec = self._receipts.get(ref) if type(ref) is str else None
        return rec is not None and rec.state == "recorded" and rec.complete

    def _index_high(self, ref):
        if type(ref) is not str or not ref.startswith(self._index_prefix):
            return None
        tail = ref[len(self._index_prefix):]
        if not _INDEX_HW_RE.fullmatch(tail) or int(tail) > len(self._receipts):
            return None
        return int(tail)

    def _read_index(self, ref, high, offset, limit):
        # Lines are generated from sequence numbers for this page only; no manifest.
        if not (type(offset) is int and type(limit) is int and 1 <= limit <= _BLOCK):
            raise ReceiptError("invalid_argument")
        width, total = self._index_line, high * self._index_line
        if not 0 <= offset <= total:
            raise ReceiptError("invalid_argument")
        end = min(offset + limit, total)
        data = ""
        if end > offset:
            first, last = offset // width, (end - 1) // width
            lines = "".join(f"result://{self._scope}/{seq}".ljust(width - 1) + "\n"
                            for seq in range(first + 1, last + 2))
            data = lines[offset - first * width:end - first * width]
        return {"ref": ref, "state": "recorded", "outcome": None, "exit_code": None,
                "offset": offset, "next_offset": end, "eof": end == total,
                "content_complete": True, "total_bytes": total, "stored_bytes": total,
                "encoding": "utf-8", "data": data, "kind": "reference_index",
                "high_water": high}

    def _encode(self, text):
        # Slice by chars first so the full UTF-8 copy is bounded (<=4x max_body).
        head = text[:self._max_body]
        try:
            raw = head.encode("utf-8", "surrogateescape")
        except UnicodeEncodeError:
            raw = head.encode("utf-8", "surrogatepass")
        measured = len(head) == len(text)
        if measured and len(raw) <= self._max_body:
            return raw, len(raw), False
        return raw[:self._max_body], (len(raw) if measured else None), True

    def record(self, ref, text, *, outcome, exit_code=None, final=True):
        """Record a result once. A publisher fault is a documented capture fallback:
        keep an explicitly incomplete inline prefix with the real outcome/exit and seal
        the slot (no re-record or replay). It is not by itself a new stop condition."""
        rec = self._lookup(ref)
        if ref in self._busy:
            raise ReceiptError("invalid_transition")
        if type(text) is not str or type(final) is not bool:
            raise ReceiptError("invalid_argument")
        if final:
            if type(outcome) is not str or outcome not in _FINAL_OUTCOMES:
                raise ReceiptError("invalid_argument")
            if exit_code is not None and not (
                    outcome == "returned" and _strict_int(exit_code, -2**31, 2**32 - 1)):
                raise ReceiptError("invalid_argument")
            if rec.state == "recorded":
                raise ReceiptError("invalid_transition")
        else:
            if outcome is not None or exit_code is not None:
                raise ReceiptError("invalid_argument")
            if rec.state != "pending":
                raise ReceiptError("invalid_transition")
        data, total, truncated = self._encode(text)
        inline = blob_key = hashes = None
        if len(data) <= self._inline_bytes:
            inline = data
        elif self._publisher is not None:
            blob_key = f"{self._scope}-{rec.seq}-r{1 if rec.state == 'pending' else 2}"
            hashes = b"".join(hashlib.sha256(data[i:i + _BLOCK]).digest()
                              for i in range(0, len(data), _BLOCK))
            self._busy.add(ref)
            try:
                ack = self._publisher(blob_key, data)
            except Exception:
                ack = False
            finally:
                self._busy.discard(ref)
            if ack is not None:  # only a synchronous None ack counts as published
                if inspect.iscoroutine(ack):
                    ack.close()  # never awaited; close to avoid the warning
                blob_key = hashes = None
                inline = data[:self._inline_bytes]
                truncated = True
        else:
            inline = data[:self._inline_bytes]
            truncated = True
        stored = len(inline) if inline is not None else len(data)
        rec.state = "recorded" if final else "provisional"
        rec.outcome, rec.exit_code = outcome, exit_code
        rec.inline, rec.blob_key, rec.hashes = inline, blob_key, hashes
        rec.total_bytes, rec.stored_bytes = total, stored
        rec.complete = not truncated and total == stored

    def _blocks(self, rec, first, last):
        start, end = first * _BLOCK, min((last + 1) * _BLOCK, rec.stored_bytes)
        try:
            got = self._reader(rec.blob_key, start, end - start)
        except Exception:
            raise ReceiptError("read_failed") from None
        if type(got) is not bytes or len(got) != end - start:
            raise ReceiptError("read_failed")
        for i, blk in enumerate(range(first, last + 1)):
            part = got[i * _BLOCK:(i + 1) * _BLOCK]
            if hashlib.sha256(part).digest() != rec.hashes[blk * 32:(blk + 1) * 32]:
                raise ReceiptError("read_failed")
        return got, start

    def read(self, ref, offset=0, limit=4096):
        high = self._index_high(ref)
        if high is not None:
            return self._read_index(ref, high, offset, limit)
        rec = self._lookup(ref)
        if ref in self._busy:
            raise ReceiptError("read_failed")
        if not (type(offset) is int and type(limit) is int and 1 <= limit <= _BLOCK):
            raise ReceiptError("invalid_argument")
        page = {"ref": ref, "state": rec.state, "outcome": rec.outcome,
                "exit_code": rec.exit_code, "offset": offset, "next_offset": None,
                "eof": False, "content_complete": False, "total_bytes": None,
                "stored_bytes": None, "encoding": None, "data": ""}
        if rec.state == "pending":
            if offset != 0:
                raise ReceiptError("invalid_argument")
            return page
        if not 0 <= offset <= rec.stored_bytes:
            raise ReceiptError("invalid_argument")
        end = min(offset + limit, rec.stored_bytes)
        if rec.inline is not None:
            chunk = rec.inline[offset:end]
        elif end == offset:
            chunk = b""
        else:
            self._busy.add(ref)
            try:
                got, base = self._blocks(rec, offset // _BLOCK, (end - 1) // _BLOCK)
            finally:
                self._busy.discard(ref)
            chunk = got[offset - base:end - base]
        try:
            data, encoding = chunk.decode("utf-8"), "utf-8"
        except UnicodeDecodeError:
            data, encoding = base64.b64encode(chunk).decode("ascii"), "base64"
        page.update(next_offset=end, eof=end == rec.stored_bytes,
                    content_complete=rec.complete, total_bytes=rec.total_bytes,
                    stored_bytes=rec.stored_bytes, encoding=encoding, data=data)
        return page

    def metadata(self):
        recs = self._receipts.values()
        return {
            "receipts": len(self._receipts),
            "states": {s: sum(r.state == s for r in recs)
                       for s in ("pending", "provisional", "recorded")},
            "outcomes": {o: sum(r.outcome == o for r in recs) for o in _FINAL_OUTCOMES},
            "content_incomplete": sum(r.state != "pending" and not r.complete
                                      for r in recs),
            "inline_bytes_used": sum(len(r.inline or b"") for r in recs),
            "limits": {"max_receipts": self._max_receipts,
                       "inline_bytes": self._inline_bytes,
                       "max_body_bytes": self._max_body,
                       "max_batch": _MAX_BATCH, "page_bytes": _BLOCK},
        }

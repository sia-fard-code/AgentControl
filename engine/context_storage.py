"""Run-owned context body storage on Capabilities artifacts (ReceiptStore backend).

Keys are private engine blob keys, never paths. Bodies go through
caps.save_artifact (exclusive plain file, shared quota); reads go through the
command-output read_page identity/single-link/reparse/per-block digest checks.
"""
from __future__ import annotations

import base64
import hashlib
import os

_CODES = frozenset({"invalid_config", "invalid_argument", "duplicate_key",
                    "publish_failed", "read_failed"})
_MAX_BODY = 1048576
_MAX_READ = 8192
_MAX_KEYS = 65536  # attempted keys per run; equals context_receipts._MAX_RECEIPTS hard cap


class ContextStorageError(ValueError):
    """Fixed closed code only; never echoes bodies, keys, paths or backend errors."""

    def __init__(self, code):
        super().__init__(code if code in _CODES else "invalid_argument")
        self.code = self.args[0]


class OwnedContextStorage:
    def __init__(self, caps, read_page_fn, block_bytes=65536):
        if not callable(read_page_fn) or type(block_bytes) is not int \
                or block_bytes != 65536:  # must equal read_page's verification block
            raise ContextStorageError("invalid_config")
        self._caps, self._read_page, self._block = caps, read_page_fn, block_bytes
        self._entries = {}   # key -> (ref, entry); never exposed
        self._used = set()   # keys ever attempted; immutable, no republish
        self._failed = 0

    def __repr__(self):
        return f"OwnedContextStorage(blobs={len(self._entries)})"

    def _path(self, ref):
        root = self._caps._artifact_root()
        full = (self._caps.resolve_read(ref) if ref.startswith("artifact://")
                else os.path.join(self._caps.root, ref))
        if os.path.dirname(os.path.realpath(full)) != root or os.path.islink(full):
            raise ValueError("outside artifact root")
        return full

    def _page(self, ref, entry, offset, length):
        page = self._read_page(ref, entry, offset, length)
        raw = page["data"].encode("utf-8") if page["encoding"] == "utf-8" \
            else base64.b64decode(page["data"], validate=True)
        if page["offset"] != offset or len(raw) != length:
            raise ValueError("short or misplaced page")  # no optimistic short read
        return raw

    def publish(self, key, data):
        if type(key) is not str or not 1 <= len(key) <= 128 \
                or type(data) is not bytes or not 1 <= len(data) <= _MAX_BODY:
            raise ContextStorageError("invalid_argument")
        if key in self._used:  # checked before any write; keys are immutable
            raise ContextStorageError("duplicate_key")
        if len(self._used) >= _MAX_KEYS:  # finite attempt ledger; refuse before backend/reserve
            raise ContextStorageError("publish_failed")
        self._used.add(key)
        try:
            ref = self._caps.save_artifact(data, ".ctx")
            path = self._path(ref)
            st = os.lstat(path)
            b = self._block
            entry = {"path": path, "size": len(data),
                     "id": (st.st_dev, st.st_ino, st.st_mtime_ns),
                     "sha256": hashlib.sha256(data).hexdigest(),
                     "blocks": [hashlib.sha256(data[i:i + b]).digest()
                                for i in range(0, len(data), b)],
                     "complete": True, "discarded": 0}
            for off in range(0, len(data), b):  # whole body verified before the ack
                if self._page(ref, entry, off, min(b, len(data) - off)) != data[off:off + b]:
                    raise ValueError("readback mismatch")
        except Exception:  # noqa: BLE001 - fixed code only, no backend detail
            self._failed += 1
            raise ContextStorageError("publish_failed") from None
        self._entries[key] = (ref, entry)
        return None

    def read(self, key, offset, length):
        got = self._entries.get(key) if type(key) is str else None
        if got is None:
            raise ContextStorageError("read_failed")
        ref, entry = got
        if not (type(offset) is int and type(length) is int and offset >= 0
                and 1 <= length <= _MAX_READ and offset + length <= entry["size"]):
            raise ContextStorageError("invalid_argument")
        try:
            if self._path(ref) != entry["path"]:
                raise ValueError("moved")
            data = self._page(ref, entry, offset, length)
            self._caps._artifact_root()  # root must still be the snapshotted one
        except Exception:  # noqa: BLE001
            raise ContextStorageError("read_failed") from None
        return data

    def metadata(self):
        return {"blobs": len(self._entries), "failed_publishes": self._failed,
                "stored_bytes": sum(e["size"] for _, e in self._entries.values())}

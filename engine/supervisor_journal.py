"""Exclusive-owner, append-only, hash-chained, fsynced supervisor metadata journal.

One line is one atomic transaction. Only allowlisted primitive metadata is persisted.
"""
import hashlib
import json
import math
import os
import re
import sys
import uuid
from collections import namedtuple

import supervisor_contract as contract

LOG_NAME = "journal.log"
LOCK_NAME = "owner.lock"
VERSION = 1
MAX_RECORD_BYTES = 16 * 1024
MAX_LOG_BYTES = 16 * 1024 * 1024
# Envelope version 2: bounded frames and segments, but no segment-count or total-history cap.
# Retained records still consume real RAM/disk/replay time; exhaustion fails closed.
MAX_V2_RECORD_BYTES = 256 * 1024
MAX_V2_SEGMENT_BYTES = 16 * 1024 * 1024
_SEGMENT = re.compile(r"^journal\.([1-9][0-9]*)\.log$")
_TOKEN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")

Record = namedtuple("Record", "seq epoch t kind data")


class JournalError(RuntimeError):
    """Journal ownership, integrity or schema failure; callers must hold, never guess."""


# Field validators: return the value unchanged or raise JournalError.
def token(v):
    if not isinstance(v, str) or not _TOKEN.fullmatch(v):
        raise JournalError("field is not a bounded token")
    return v


def optional_token(v):
    return v if v is None else token(v)


def integer(v):
    if type(v) is not int or not 0 <= v <= 2**53:
        raise JournalError("field is not a non-negative integer")
    return v


def optional_int(v):
    return v if v is None else integer(v)


def number(v):
    if type(v) not in (int, float) or not math.isfinite(v):
        raise JournalError("field is not a finite number")
    return v


def boolean(v):
    if type(v) is not bool:
        raise JournalError("field is not boolean")
    return v


def tokens(v):
    if not isinstance(v, list) or len(v) > 64:
        raise JournalError("field is not a bounded token list")
    return [token(i) for i in v]


def digest(v):
    if not isinstance(v, str) or not contract.SHA256.fullmatch(v):
        raise JournalError("field is not a sha256 digest")
    return v


def optional_digest(v):
    return v if v is None else digest(v)


def one_of(*allowed):
    def check(v):
        if v not in allowed or not isinstance(v, str):
            raise JournalError("field value not allowlisted")
        return v
    return check


INTERNAL = {
    "genesis": {"envelope_digest": digest, "orchestration_id": token, "version": integer},
    "owner": {"pid": integer, "birth": token, "nonce": token},
}


def _birth_identity():
    """Best-effort process creation identity; None when the platform cannot supply it."""
    try:
        if sys.platform == "win32":
            import ctypes
            from ctypes import wintypes
            k32 = ctypes.WinDLL("kernel32", use_last_error=True)
            k32.GetCurrentProcess.restype = wintypes.HANDLE  # pseudo-handle must not truncate
            k32.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
            k32.GetProcessTimes.restype = wintypes.BOOL
            times = [wintypes.FILETIME() for _ in range(4)]
            handle = k32.GetCurrentProcess()
            if k32.GetProcessTimes(handle, *[ctypes.byref(t) for t in times]):
                return f"win:{(times[0].dwHighDateTime << 32) | times[0].dwLowDateTime}"
        else:
            with open(f"/proc/{os.getpid()}/stat", "rb") as fh:
                return "linux:" + fh.read().rsplit(b")", 1)[1].split()[19].decode()
    except (OSError, IndexError, AttributeError, ValueError):
        pass
    return None


def _lock(fd):
    """Take a non-blocking OS-held exclusive lock; released by the OS on process death."""
    try:
        if sys.platform == "win32":
            import msvcrt
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        raise JournalError("orchestration is owned by another live supervisor") from exc


def _unlock(fd):
    if sys.platform == "win32":
        import msvcrt
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
    else:
        import fcntl
        fcntl.flock(fd, fcntl.LOCK_UN)


def _hash(prev, body):
    return hashlib.sha256(prev.encode() + contract.canonical(body)).hexdigest()


def _segment_name(i):
    return LOG_NAME if i == 0 else f"journal.{i}.log"


def _fsync_dir(path):
    """Durably publish directory entries; raise OSError when the platform refuses."""
    if sys.platform != "win32":
        fd = os.open(path, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
        return
    import ctypes
    from ctypes import wintypes
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
                                wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    k32.CreateFileW.restype = wintypes.HANDLE
    k32.FlushFileBuffers.argtypes = [wintypes.HANDLE]
    k32.FlushFileBuffers.restype = wintypes.BOOL
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    # GENERIC_WRITE, share all, OPEN_EXISTING, FILE_FLAG_BACKUP_SEMANTICS (directory handle)
    h = k32.CreateFileW(path, 0x40000000, 0x7, None, 3, 0x02000000, None)
    if h is None or h == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        if not k32.FlushFileBuffers(h):
            raise ctypes.WinError(ctypes.get_last_error())
    finally:
        k32.CloseHandle(h)


HEAD_NAME = "commit.head"  # v2 commit witness: last acknowledged record; not history
_HEAD_KEYS = {"v", "seq", "h", "seg", "off"}
_MAX_HEAD_BYTES = 1024


def _read_head(state_dir):
    """Strict bounded witness read; None only when absent. Anything else invalid holds."""
    try:
        with open(os.path.join(state_dir, HEAD_NAME), "rb") as fh:
            raw = fh.read(_MAX_HEAD_BYTES + 1)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise JournalError("commit witness unreadable; holding") from exc
    try:
        head = contract.strict_json(raw) if len(raw) <= _MAX_HEAD_BYTES else None
        ok = isinstance(head, dict) and set(head) == _HEAD_KEYS and head["v"] == 2 \
            and all(type(head[k]) is int and head[k] >= 0 for k in ("v", "seq", "seg", "off"))
        ok = ok and contract.canonical(head) == raw
        digest(head["h"]) if ok else None
    except (contract.ContractError, JournalError, TypeError, KeyError):
        ok = False
    if not ok:
        raise JournalError("commit witness corrupt or unsupported; holding")
    return head


def _publish_head(state_dir, head):
    """Atomically replace the witness via an exclusive own temp file, fsync and directory flush."""
    tmp = os.path.join(state_dir, f"{HEAD_NAME}.{uuid.uuid4().hex}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o600)
    try:
        raw = contract.canonical(head)
        if os.write(fd, raw) != len(raw):
            raise OSError("short commit witness write")
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp, os.path.join(state_dir, HEAD_NAME))
    _fsync_dir(state_dir)


class Journal:
    """Context manager owning one orchestration's state directory."""

    def __init__(self, state_dir, envelope, schemas, clock):
        overlap = set(schemas) & set(INTERNAL)
        if overlap:
            raise JournalError("schema kinds collide with internal records")
        self.state_dir = os.fspath(state_dir)
        self.envelope = envelope
        self.schemas = {**INTERNAL, **schemas}
        self.clock = clock
        self.records = []
        self.epoch = None
        self._fd = self._log = None
        self._prev = "0" * 64
        self._size = 0
        self._seg = 0  # index of the current tail segment; always 0 for version 1
        self._ack_head = None
        version = getattr(envelope, "schema_version", 1)
        if type(version) is not int or version not in (1, 2, 3):
            raise JournalError("unsupported envelope schema version")
        self._v2 = version in (2, 3)  # envelope 3 reuses the v2 storage frame format

    def _limits(self):
        """(record frame bound, segment bound) read at use so tests can inject thresholds."""
        if self._v2:
            return MAX_V2_RECORD_BYTES, MAX_V2_SEGMENT_BYTES
        return MAX_RECORD_BYTES, MAX_LOG_BYTES

    def __enter__(self):
        created = not os.path.isdir(self.state_dir)
        os.makedirs(self.state_dir, exist_ok=True)
        fd = os.open(os.path.join(self.state_dir, LOCK_NAME), os.O_RDWR | os.O_CREAT, 0o600)
        try:
            _lock(fd)
        except BaseException:
            os.close(fd)
            raise
        self._fd = fd
        try:
            self._replay()
            self._log = os.open(os.path.join(self.state_dir, _segment_name(self._seg)),
                                os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_BINARY", 0), 0o600)
            if os.fstat(self._log).st_size != self._size:
                raise JournalError("journal changed outside this owner; holding")
            if not self.records:  # durably publish the new log (and new state dir) before any owner
                try:
                    os.fsync(self._log)
                    _fsync_dir(self.state_dir)
                    if created:
                        _fsync_dir(os.path.dirname(os.path.abspath(self.state_dir)))
                except OSError as exc:
                    raise JournalError("initial journal publication not durable; holding") from exc
                self._append("genesis", {"envelope_digest": self.envelope.digest,
                                         "orchestration_id": self.envelope.orchestration_id,
                                         "version": VERSION}, epoch=0)
            genesis = self.records[0]
            if genesis.kind != "genesis" or genesis.data["envelope_digest"] != self.envelope.digest:
                raise JournalError("journal belongs to a different approved envelope")
            birth = _birth_identity()
            if birth is None:  # OS lock is primary, but unverifiable birth identity fails closed
                raise JournalError("process birth identity unavailable; holding")
            self.epoch = self.records[-1].epoch + 1
            self.transact("owner", {"pid": os.getpid(), "birth": birth,
                                    "nonce": uuid.uuid4().hex})
        except BaseException:
            self.close()
            raise
        return self

    def __exit__(self, *exc):
        self.close()

    def close(self):
        self.epoch = None
        for name in ("_log", "_fd"):
            fd = getattr(self, name)
            if fd is not None:
                if name == "_fd":
                    _unlock(fd)
                os.close(fd)
                setattr(self, name, None)

    def _segments(self):
        """Ordered existing segment names; v2 requires strict contiguity and no foreign names."""
        names = os.listdir(self.state_dir)
        found = {0} if LOG_NAME in names else set()
        for n in names:
            m = _SEGMENT.fullmatch(n)
            if m:
                found.add(int(m.group(1)))
            elif self._v2 and n.startswith("journal") and n != LOG_NAME:
                raise JournalError("unexpected journal file; holding")
        if not self._v2:
            if found - {0}:
                raise JournalError("segmented journal state is incompatible with version 1")
            return [LOG_NAME] if found else []
        if found != set(range(len(found))):
            raise JournalError("journal segments missing or non-contiguous; holding")
        return [_segment_name(i) for i in range(len(found))]

    def _replay(self):
        frame, seg_max = self._limits()
        read_max = frame if self._v2 else contract.MAX_ENVELOPE_BYTES  # v1: strict_json's bound
        names = self._segments()
        head = _read_head(self.state_dir) if self._v2 else None
        if self._v2 and (head is None) != (not names):
            # No silent reconstruction: un-witnessed v2 data, interrupted creation or lost data hold.
            raise JournalError("commit witness missing or journal data lost; holding")
        witnessed = head is None
        epoch = 0
        for i, name in enumerate(names):
            path = os.path.join(self.state_dir, name)
            size = os.path.getsize(path)
            if size > seg_max:
                raise JournalError("journal exceeds bounded size")
            if size == 0 and i < len(names) - 1:  # only the newest may be empty (rotation crash)
                raise JournalError("empty interior journal segment; holding")
            with open(path, "rb") as fh:
                while True:
                    line = fh.readline(read_max + 1)  # bounded: never one buffer of whole history
                    if not line:
                        break
                    if not line.endswith(b"\n"):
                        raise JournalError("torn journal tail; reconciliation required")
                    epoch = self._replay_line(line[:-1], epoch)
                    if head is not None and len(self.records) - 1 == head["seq"]:
                        witnessed = (i, fh.tell(), self._prev) == (head["seg"], head["off"], head["h"])
                self._seg, self._size = i, fh.tell()
        if not witnessed:  # acknowledged record/segment/suffix missing; later extras stay consumed
            raise JournalError("journal does not reach its commit witness; holding")
        self._ack_head = head

    def _replay_line(self, line, epoch):
        seq = len(self.records)
        try:
            if len(line) <= contract.MAX_ENVELOPE_BYTES:
                rec = contract.strict_json(line)
            else:  # only v2 frames reach here; needs the contract's bounded keyword
                rec = contract.strict_json(line, max_bytes=MAX_V2_RECORD_BYTES)
        except (contract.ContractError, TypeError) as exc:
            raise JournalError(f"corrupt journal record {seq}") from exc
        if not isinstance(rec, dict) or set(rec) != {"v", "seq", "epoch", "t", "kind", "data", "h"} \
                or rec["v"] != VERSION or rec["seq"] != seq:
            raise JournalError(f"malformed journal record {seq}")
        body = {k: rec[k] for k in ("v", "seq", "epoch", "t", "kind", "data")}
        if rec["h"] != _hash(self._prev, body):
            raise JournalError(f"journal hash chain broken at record {seq}")
        self._validate(rec["kind"], rec["data"])
        number(rec["t"])
        if type(rec["epoch"]) is not int or rec["epoch"] < epoch \
                or (self.records and rec["t"] < self.records[-1].t):
            raise JournalError(f"journal epoch/clock regression at record {seq}")
        self._prev = rec["h"]
        self.records.append(Record(seq, rec["epoch"], rec["t"], rec["kind"], rec["data"]))
        return rec["epoch"]

    def _validate(self, kind, data):
        schema = self.schemas.get(kind)
        if schema is None or not isinstance(data, dict) or set(data) != set(schema):
            raise JournalError("record does not match allowlisted schema")  # no untrusted echo
        for key, check in schema.items():
            check(data[key])

    def owner_current(self, epoch=None):
        """Effect guard: retained owner handles still name these exact local files."""
        if self.epoch is None or (epoch is not None and epoch != self.epoch):
            return False
        try:
            for fd, name in ((self._fd, LOCK_NAME), (self._log, _segment_name(self._seg))):
                if fd is None:
                    return False
                held, named = os.fstat(fd), os.stat(os.path.join(self.state_dir, name),
                                                    follow_symlinks=False)
                if not held.st_ino or (held.st_dev, held.st_ino) != (named.st_dev, named.st_ino):
                    return False
            # In-place truncation/extension keeps the inode; only our exact published size is ours.
            if held.st_size != self._size or named.st_size != self._size:
                return False
            # A tail this owner did not publish means another writer: no effect authority.
            if self._v2 and self._segments() != [_segment_name(i) for i in range(self._seg + 1)]:
                return False
            if self._v2 and _read_head(self.state_dir) != self._ack_head:
                return False
            return True
        except (OSError, JournalError):
            return False

    def transact(self, kind, data):
        """Durably append one atomic metadata transaction; return its Record only after fsync."""
        if not self.owner_current():
            raise JournalError("journal handle is not the current owner")
        if kind in ("genesis",):
            raise JournalError("internal record kind")
        return self._append(kind, data, epoch=self.epoch)

    def _append(self, kind, data, epoch):
        self._validate(kind, data)
        now = number(self.clock())
        if self.records and now < self.records[-1].t:
            raise JournalError("clock moved backwards; holding")
        if os.fstat(self._log).st_size != self._size:
            raise JournalError("journal changed outside this owner; holding")
        body = {"v": VERSION, "seq": len(self.records), "epoch": epoch, "t": now,
                "kind": kind, "data": dict(data)}
        h = _hash(self._prev, body)
        line = contract.canonical({**body, "h": h}) + b"\n"
        frame, seg_max = self._limits()
        if len(line) > frame or (self._v2 and len(line) > seg_max):
            raise JournalError("record exceeds bounded size")
        if len(line) - 1 > contract.MAX_ENVELOPE_BYTES:  # never write a frame replay cannot parse
            try:
                contract.strict_json(line[:-1], max_bytes=MAX_V2_RECORD_BYTES)
            except (contract.ContractError, TypeError) as exc:
                raise JournalError("record not replayable under bounded parsing") from exc
        if self._size + len(line) > seg_max:  # refuse before writing; replay would reject it
            if not self._v2:
                raise JournalError("journal would exceed bounded size; holding")
            if self._size:  # an empty newest segment (rotation crash) is reused, not skipped
                self._rotate()
        try:
            written = os.write(self._log, line)
            os.fsync(self._log)
            if written != len(line):
                raise OSError("short journal write")
        except OSError as exc:
            self.close()  # a possibly torn tail must be reconciled by a fresh owner
            raise JournalError("journal append failed; holding") from exc
        try:
            self._size += len(line)
            self._prev = h
            record = Record(body["seq"], epoch, now, kind, body["data"])
            self.records.append(record)
            if self._v2:  # witness before acknowledgment; on failure replay still consumes the row
                head = {"v": 2, "seq": record.seq, "h": h, "seg": self._seg, "off": self._size}
                _publish_head(self.state_dir, head)
                self._ack_head = head
        except OSError as exc:
            self.close()
            raise JournalError("commit witness publication failed; holding") from exc
        except BaseException:
            self.close()  # committed on disk; replay recovers it, uncertain memory must not act
            raise
        return record

    def _rotate(self):
        """Exclusively create and durably publish the next segment under the held owner lock."""
        nxt = self._seg + 1
        path = os.path.join(self.state_dir, _segment_name(nxt))
        try:
            fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_EXCL
                         | getattr(os, "O_BINARY", 0), 0o600)
        except OSError as exc:
            self.close()  # a foreign or concurrent tail exists: no effect authority remains
            raise JournalError("journal segment publication failed; holding") from exc
        try:
            os.fsync(fd)
            _fsync_dir(self.state_dir)
        except OSError as exc:
            os.close(fd)
            self.close()  # publication uncertain; a fresh owner replays the empty newest segment
            raise JournalError("journal segment publication not durable; holding") from exc
        os.close(self._log)
        self._log, self._seg, self._size = fd, nxt, 0

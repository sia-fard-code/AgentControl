"""Recoverable bounded command output for resilient-v1 (standard library only).

capture_command() runs one already-validated argv with stdout/stderr drained
concurrently in bounded chunks. Each stream keeps a small inline prefix in
memory; past it, bytes spill lazily into one run-local artifact obtained from
open_sink(name). Quota/I/O failure never kills the child: the drain continues
and counts discarded bytes, so capture completeness is always explicit.
"""
from __future__ import annotations

import os
import subprocess
import threading

CHUNK = 64 * 1024
BLOCK = 64 * 1024         # artifact verification block (per-block sha256)
INLINE_BYTES = 4 * 1024
JOIN_GRACE = 5.0          # finite wait for pipes a grandchild may still hold
ERRORS = ("no_artifact_dir", "quota", "io_error", "pipe_held_open", "read_error",
          "capture_pending")


class StreamCapture:
    """One stream: inline prefix, optional spilled artifact, honest accounting."""

    def __init__(self, name, open_sink):
        self.name = name
        self._open_sink = open_sink
        self.prefix = bytearray()
        self.sink = None
        self.total = 0          # bytes the child wrote that we read
        self.stored = 0         # bytes durably in the artifact (if any)
        self.discarded = 0      # bytes read but neither inline-only nor stored
        self.error = None       # first bounded error code from ERRORS
        self.ref = None
        self._spilling = False
        self._detached = False

    def detach(self):
        """Stop new feeds without waiting for an in-flight filesystem operation."""
        self._detached = True

    def feed(self, data):
        if not self._detached:
            self._feed(data)

    def unfinished(self):
        """Conservative snapshot; the drain retains ownership of any open sink."""
        self.detach()
        self._fail("capture_pending")
        snapshot = StreamCapture(self.name, self._open_sink)
        snapshot.prefix = self.prefix.copy()
        snapshot.total = max(self.total, len(snapshot.prefix))
        snapshot.discarded = snapshot.total - len(snapshot.prefix)
        snapshot.error = "capture_pending"
        snapshot.detach()
        return snapshot

    def _feed(self, data):
        self.total += len(data)
        if not self._spilling:
            room = INLINE_BYTES - len(self.prefix)
            if len(data) <= room:
                self.prefix += data
                return
            self.prefix += data[:room]
            data = data[room:]
            self._spilling = True
            self._start_sink()
            self._store(bytes(self.prefix))
        self._store(data)

    def _start_sink(self):
        try:
            self.sink = self._open_sink(self.name)
        except Exception as e:  # noqa: BLE001 - classified, never raised
            self._fail(_code(e))

    def _store(self, data):
        if self._detached:
            self.discarded += len(data)
            return
        if self.sink is None:
            self.discarded += len(data)
            return
        try:
            n = self.sink.write(data)
        except Exception as e:  # noqa: BLE001
            n = 0
            self._fail(_code(e))
            self._abandon()
        self.stored += n
        self.discarded += len(data) - n
        if n < len(data) and self.sink is not None:
            self._fail(getattr(self.sink, "error", None) or "quota")
            self._abandon()

    def _abandon(self):
        sink, self.sink = self.sink, None
        if sink is not None:
            try:
                self.ref = sink.close()   # keep the stored part reachable
            except Exception:  # noqa: BLE001
                self.ref = None
                self.stored = 0

    def _fail(self, code):
        if self.error is None:
            self.error = code

    def finish(self):
        self.detach()
        self._abandon()
        # Spilled stream with no surviving artifact: inline prefix is all we have.
        if self._spilling and self.ref is None:
            self.discarded = self.total - len(self.prefix)
            self.stored = 0
        return self

    @property
    def complete(self):
        return self.error is None and self.discarded == 0

    def record(self):
        """Metadata only: never the raw bytes beyond the bounded inline prefix."""
        return {"ref": self.ref, "total_bytes": self.total,
                "stored_bytes": self.stored, "discarded_bytes": self.discarded,
                "capture_complete": self.complete, "error": self.error}


def _code(exc):
    if isinstance(exc, ValueError) and "artifact directory" in str(exc) \
            and "no artifact" in str(exc):
        return "no_artifact_dir"
    if isinstance(exc, ValueError):
        return "quota"
    return "io_error"


POLL = 0.02  # drain never blocks in read: it polls, so cancellation is always finite


def _available(fd):
    """Bytes readable now without blocking (>0), 0 if none yet, -1 at EOF."""
    if os.name == "nt":
        import ctypes
        import msvcrt
        from ctypes import wintypes
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        avail = wintypes.DWORD()
        if k32.PeekNamedPipe(wintypes.HANDLE(msvcrt.get_osfhandle(fd)), None, 0, None,
                             ctypes.byref(avail), None):
            return avail.value
        if ctypes.get_last_error() == 109:   # ERROR_BROKEN_PIPE: every writer closed
            return -1
        raise OSError("pipe peek failed")
    import select
    return CHUNK if select.select([fd], [], [], POLL)[0] else 0


def _drain(fd, cap, stop):
    try:
        while not stop.is_set():
            n = _available(fd)
            if n < 0:
                return
            if n == 0:
                if os.name == "nt":
                    stop.wait(POLL)
                continue
            data = os.read(fd, min(n, CHUNK))
            if not data:
                return
            cap.feed(data)
        cap._fail("pipe_held_open")   # cancelled before EOF: output may be missing
    except (OSError, ValueError):
        cap._fail("read_error")
    finally:
        # Only this drain closes its sink, including after delayed storage I/O.
        cap.finish()


class Result:
    def __init__(self, returncode, stdout, stderr):
        self.returncode, self.stdout, self.stderr = returncode, stdout, stderr


def capture_command(argv, *, cwd, timeout, open_sink, on_start=None, on_exit=None):
    """Run argv once; return Result with StreamCapture stdout/stderr.

    Raises subprocess.TimeoutExpired / OSError exactly like subprocess.run so
    the caller keeps its effect-unknown handling. Cleanup is finite.
    """
    # stdin inherited, as the legacy subprocess.run path does
    proc = subprocess.Popen(argv, cwd=cwd, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, shell=False)
    if on_start is not None:
        try:
            on_start(proc)
        except Exception:
            pass  # Optional evidence cannot change the command's actual outcome.
    caps = [StreamCapture("stdout", open_sink), StreamCapture("stderr", open_sink)]
    stop = threading.Event()
    threads = []
    ok = False
    try:
        for p, c in zip((proc.stdout, proc.stderr), caps):
            t = threading.Thread(target=_drain, args=(p.fileno(), c, stop), daemon=True)
            t.start()
            threads.append(t)
        rc = proc.wait(timeout=timeout)
        ok = True
    except (OSError, subprocess.TimeoutExpired):
        raise
    except Exception as exc:
        # The child may already have acted before drain startup/communication failed.
        raise OSError("command communication failed") from exc
    finally:
        if not ok:  # timeout, thread start failure, interrupted wait: never leak the child
            proc.kill()
            try:
                proc.wait(timeout=JOIN_GRACE)
            except subprocess.TimeoutExpired:
                pass
        if on_exit is not None:
            try:
                on_exit(proc.returncode)
            except Exception:
                pass
        _close(proc, threads, caps, stop)
    return Result(rc, caps[0], caps[1])


READ_LIMIT = 4096
COMPACT_HINT = "budget: previews dropped; page refs with read_command_output, never rerun"


class CompactText(str):
    """Tool result text plus an engine-owned compact fallback.

    The fallback comes from structured state the engine built, never from parsing
    the (child-controlled) text. compact() is lazy: it is computed (and may publish
    inline bytes as artifacts) only when the context guard actually needs it."""

    def __new__(cls, text, make_compact):
        self = super().__new__(cls, text)
        self._make, self._done = make_compact, None
        return self

    def compact(self):
        if self._done is None:
            try:
                self._done = self._make() or ""
            except Exception:  # noqa: BLE001 - no fallback is safe; guard then refuses
                self._done = ""
        return self._done or None


def compact_result(name, text):
    """Trusted compact callable for engine-built results, else None (never parses text)."""
    return text.compact if isinstance(text, CompactText) else None


def command_compact(rc, streams):
    """<=COMPACT bytes: rc + refs/counts as engine-escaped JSON; no output bytes."""
    import json
    keep = ("ref", "stored_bytes", "discarded_bytes", "capture_complete")
    body = {s: {k: rec[k] for k in keep} for s, rec in streams.items()}
    return f"exit={rc}\n{json.dumps(body, sort_keys=True, ensure_ascii=True)}\n[{COMPACT_HINT}]"


def page_compact(page):
    """Retry cursor at the SAME offset: dropped data is never skipped."""
    import json
    page = dict(page, data="", encoding="utf-8", next_offset=page["offset"],
                eof=page["offset"] >= page["stored_bytes"],
                budget="page dropped by context budget; retry with a smaller limit")
    return json.dumps(page, sort_keys=True, ensure_ascii=True)


def legacy_text(data):
    """Decode like subprocess.run(text=True, errors='replace'): locale encoding,
    universal newlines. Artifacts keep the raw bytes; this is display only."""
    import locale
    text = data.decode(locale.getpreferredencoding(False), "replace")
    return text.replace("\r\n", "\n").replace("\r", "\n")


def read_page(ref, entry, offset, limit):
    """One bounded page of a pinned command artifact as a JSON-able dict.

    Raises ValueError on any identity/digest mismatch; the cursor counts bytes.
    """
    import base64
    import hashlib
    import os
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    def same(st):
        reparse = getattr(st, "st_file_attributes", 0) & 0x400  # REPARSE_POINT
        return ((st.st_dev, st.st_ino, st.st_mtime_ns) == entry["id"]
                and st.st_size == entry["size"] and st.st_nlink == 1 and not reparse)

    if not same(os.lstat(entry["path"])):
        raise ValueError("command output artifact was replaced or modified")
    fd = os.open(entry["path"], flags)
    try:
        if not same(os.fstat(fd)):
            raise ValueError("command output artifact was replaced or modified")
        # Every page re-verifies the whole 64KiB blocks it touches (bounded memory),
        # so a byte flip with restored mtime is caught on any later read.
        first, end = offset // BLOCK, min(offset + limit, entry["size"])
        os.lseek(fd, first * BLOCK, os.SEEK_SET)
        span = b""
        for i in range(first, (end - 1) // BLOCK + 1 if end > offset else first):
            want = min(BLOCK, entry["size"] - i * BLOCK)
            block = b""
            while len(block) < want:
                got = os.read(fd, want - len(block))
                if not got:
                    break
                block += got
            if hashlib.sha256(block).digest() != entry["blocks"][i]:
                raise ValueError("command output artifact digest mismatch")
            span += block
        if not same(os.fstat(fd)) or not same(os.lstat(entry["path"])):
            raise ValueError("command output artifact was replaced or modified")
    finally:
        os.close(fd)
    data = span[offset - first * BLOCK:end - first * BLOCK]
    try:
        text, encoding = data.decode("utf-8"), "utf-8"
    except UnicodeDecodeError:
        text, encoding = base64.b64encode(data).decode("ascii"), "base64"
    nxt = offset + len(data)
    return {"ref": ref, "offset": offset, "next_offset": nxt,
            "eof": nxt >= entry["size"], "capture_complete": entry["complete"],
            "stored_bytes": entry["size"], "discarded_bytes": entry["discarded"],
            "encoding": encoding, "data": text}


def _close(proc, threads, caps, stop):
    """Finite: a grandchild may hold the pipes open, so drains are cancelled
    after JOIN_GRACE (they poll, never block in read) and pipes then closed.
    The grandchild itself is NOT killed or contained; only our handles end."""
    import time
    deadline = time.monotonic() + JOIN_GRACE
    for t in threads:
        t.join(max(0.0, deadline - time.monotonic()))
    stop.set()
    for t in threads:
        t.join(1.0)   # a poll cycle, or one in-flight sink write
    for i, (t, c) in enumerate(zip(threads, caps)):
        if t.is_alive():
            caps[i] = c.unfinished()
    for p in (proc.stdout, proc.stderr):
        try:
            p.close()
        except OSError:
            pass
    for c in caps:
        c.finish()

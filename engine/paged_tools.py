"""Bounded I/O producers for the opt-in resilient-v1 Runner profile."""
from __future__ import annotations

import codecs
import contextlib
import fnmatch
import heapq
import json
import os
import re
import stat
import subprocess
import sys
import threading

MAX_OFFSET = 10000
MAX_LIMIT = 1000
MAX_SCAN_ENTRIES = 50000
MAX_SCAN_BYTES = 8 * 1024 * 1024
MAX_DEPTH = 64
LINE_BYTES = 8192
SEARCH_TIMEOUT = 30
MAX_REQUEST = 64 * 1024
# A JSON-escaped line can grow up to 6x (control chars), plus path and fields.
FRAME_BYTES = 8 * LINE_BYTES
CHILD_FLAG = "--search-child"
CHILD_REASONS = {"directory depth limit reached", "file scan limit reached",
                 "search byte scan limit reached", "search scan/line limit reached",
                 "oversized line searched only in bounded prefix",
                 "non-UTF-8 or unreadable file skipped", "reparse/junction directory skipped",
                 "path outside root skipped", "bad search request"}


def _paging(offset, limit):
    if isinstance(offset, bool) or not isinstance(offset, int) or not 0 <= offset <= MAX_OFFSET:
        raise ValueError(f"offset must be an integer from 0 to {MAX_OFFSET}")
    if limit is not None and (isinstance(limit, bool) or not isinstance(limit, int)
                              or not 1 <= limit <= MAX_LIMIT):
        raise ValueError(f"limit must be an integer from 1 to {MAX_LIMIT}")
    return offset, limit or MAX_LIMIT


def _render(run, records, offset, limit, state, empty):
    output = run._output
    # Reserve space for producer diagnostics in addition to the page's own notice.
    page = output.bounded_lines(records, output.PAGE_BYTES - 256, start=offset, limit=limit)
    if page.next_offset is not None and page.next_offset > MAX_OFFSET:
        page = output.Page(page.text, True, None,
                           "[incomplete: pagination offset limit reached; narrow the search/path]")
    text = output.render(page)
    if state.get("reason"):
        text += f"\n[incomplete: {state['reason']}; narrow the search/path]"
    return text or empty


def list_dir(run, path, skip_dirs, *, offset=0, limit=None):
    offset, limit = _paging(offset, limit)
    full = run._resolve(path)
    state = {}

    def entries():
        with os.scandir(full) as scan:
            for n, entry in enumerate(scan):
                if n >= MAX_SCAN_ENTRIES:
                    state["reason"] = "directory scan limit reached; alphabetical page is partial"
                    break
                if entry.name in skip_dirs:
                    continue
                try:
                    if entry.is_symlink():
                        label = f"{entry.name}  (symlink)"
                    elif entry.is_dir(follow_symlinks=False):
                        label = entry.name + "/"
                    else:
                        info = entry.stat(follow_symlinks=False)
                        label = (f"{entry.name}  ({info.st_size:,} b)" if stat.S_ISREG(info.st_mode)
                                 else f"{entry.name}  (nonregular)")
                    yield entry.name, label
                except OSError:
                    state["reason"] = "directory changed during scan"

    # nsmallest uses a bounded heap, not a materialized/sorted directory listing.
    selected = heapq.nsmallest(offset + limit + 1, entries())
    return _render(run, (label for _, label in selected), offset, limit, state, "(empty)")


def _binary_lines(stream, state):
    """Yield bounded logical lines, discarding oversized tails within a scan cap."""
    while state.get("bytes", 0) < MAX_SCAN_BYTES and not state.get("timeout"):
        raw = stream.readline(min(LINE_BYTES + 1, MAX_SCAN_BYTES - state.get("bytes", 0)))
        if not raw:
            return
        state["bytes"] = state.get("bytes", 0) + len(raw)
        first = raw
        clipped = len(raw) > LINE_BYTES
        while not raw.endswith(b"\n") and len(raw) >= LINE_BYTES + 1:
            if state["bytes"] >= MAX_SCAN_BYTES or state.get("timeout"):
                state["reason"] = "search scan/line limit reached"
                break
            raw = stream.readline(min(LINE_BYTES + 1, MAX_SCAN_BYTES - state["bytes"]))
            state["bytes"] += len(raw)
            clipped = True
            if not raw:
                break
        yield first[:LINE_BYTES], clipped
    state["reason"] = "search timed out" if state.get("timeout") else "search byte scan limit reached"


def _rg_records(run, pattern, glob, skip_dirs, state):
    argv = ["rg", "-n", "--no-heading", "--color", "never", "--no-follow", "-S", "-e", pattern]
    if glob:
        argv += ["-g", glob]
    for directory in sorted(skip_dirs):
        argv += ["-g", f"!{directory}/"]
    proc = subprocess.Popen(argv, cwd=run.root, stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL, shell=False)
    with _owned(proc, state):
        for raw, clipped in _binary_lines(proc.stdout, state):
            yield raw.decode("utf-8", "replace").rstrip() + (
                " [line clipped; narrow the search]" if clipped else "")
        if state.get("reason") and proc.poll() is None:
            proc.kill()
        code = proc.wait()
        if code not in (0, 1) and not state.get("reason"):
            state["reason"] = f"rg exited with status {code}"


@contextlib.contextmanager
def _owned(proc, state):
    """Own a started child: deadline kill, then kill/reap/close on every exit path."""
    timer = None
    try:
        def timeout():
            state["timeout"] = True
            state["reason"] = "search timed out"
            if proc.poll() is None:
                proc.kill()

        timer = threading.Timer(SEARCH_TIMEOUT, timeout)
        timer.daemon = True
        timer.start()
        yield
    finally:
        if timer is not None:
            timer.cancel()
        if proc.poll() is None:
            proc.kill()
        proc.wait()
        for pipe in (getattr(proc, "stdin", None), proc.stdout):
            if pipe is not None:
                pipe.close()


def _compile(pattern):
    # Match ripgrep's smart-case behavior instead of changing case at pagination.
    return re.compile(pattern, 0 if any(c.isupper() for c in pattern) else re.IGNORECASE)


def _inside(root, path):
    try:
        real = os.path.normcase(os.path.realpath(path))
        return os.path.commonpath([root, real]) == root
    except (OSError, ValueError):
        return False


def _reparse(entry):
    """Junctions/reparse points can report is_symlink False and is_dir True."""
    if getattr(os.path, "isjunction", None) and os.path.isjunction(entry.path):
        return True
    attrs = getattr(entry.stat(follow_symlinks=False), "st_file_attributes", 0)
    return bool(attrs & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))


def _walk(root, pattern, glob, skip_dirs, state):
    """Inner fallback search generator; runs only inside the owned search child."""
    rx = _compile(pattern)
    base = os.path.realpath(root)
    root = os.path.normcase(base)

    def walk(directory, depth):
        if depth > MAX_DEPTH:
            state["reason"] = "directory depth limit reached"
            return
        if not _inside(root, directory):
            state["reason"] = "path outside root skipped"
            return
        with os.scandir(directory) as scan:
            for entry in scan:
                state["entries"] = state.get("entries", 0) + 1
                if state["entries"] > MAX_SCAN_ENTRIES:
                    state["reason"] = "file scan limit reached"
                    return
                if state.get("bytes", 0) >= MAX_SCAN_BYTES:
                    state["reason"] = "search byte scan limit reached"
                    return
                try:
                    if entry.is_symlink() or entry.name in skip_dirs and entry.is_dir(follow_symlinks=False):
                        continue
                    if entry.is_dir(follow_symlinks=False):
                        if _reparse(entry):
                            state["reason"] = "reparse/junction directory skipped"
                        else:
                            yield from walk(entry.path, depth + 1)
                        continue
                    if not entry.is_file(follow_symlinks=False):
                        continue
                    rel = os.path.relpath(entry.path, base).replace(os.sep, "/")
                    if glob and not fnmatch.fnmatch(rel, glob) and not fnmatch.fnmatch(entry.name, glob):
                        continue
                    if not _inside(root, entry.path):
                        state["reason"] = "path outside root skipped"
                        continue
                    with open(entry.path, "rb") as fh:
                        for n, (raw, clipped) in enumerate(_binary_lines(fh, state), 1):
                            try:
                                # Clipped prefixes may end mid-character; keep only whole chars.
                                line = codecs.getincrementaldecoder("utf-8")().decode(raw, final=not clipped)
                            except UnicodeDecodeError:
                                state["reason"] = "non-UTF-8 or unreadable file skipped"
                                continue
                            if "\x00" in line:
                                break
                            if rx.search(line):
                                yield rel, n, line.rstrip(), clipped
                            elif clipped:
                                state["reason"] = "oversized line searched only in bounded prefix"
                except OSError:
                    state["reason"] = "non-UTF-8 or unreadable file skipped"
                    continue
    yield from walk(base, 0)


def _py_records(run, pattern, glob, skip_dirs, state):
    """Regex fallback in an owned child so a stuck regex is killable at the deadline."""
    _compile(pattern)
    request = json.dumps({"root": run.root, "pattern": pattern, "glob": glob,
                          "skip": sorted(skip_dirs)}).encode("utf-8")
    if len(request) > MAX_REQUEST:
        raise ValueError("search request too large")
    # Fixed trusted interpreter and this file only; -I ignores env/script-dir imports.
    proc = subprocess.Popen([sys.executable, "-I", os.path.abspath(__file__), CHILD_FLAG],
                            cwd=run.root, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL, shell=False)
    with _owned(proc, state):
        proc.stdin.write(request)
        proc.stdin.close()
        done, used = False, 0
        while not done and used < MAX_SCAN_BYTES:
            raw = proc.stdout.readline(FRAME_BYTES + 1)
            if not raw:
                break
            used += len(raw)
            try:
                if len(raw) > FRAME_BYTES or not raw.endswith(b"\n"):
                    raise ValueError
                frame = json.loads(raw)
                if frame == {"done": True}:
                    done = True
                elif set(frame) == {"r"} and frame["r"] in CHILD_REASONS:
                    state["reason"] = frame["r"]
                else:
                    rel, n, text, clipped = frame["m"]
                    if not (isinstance(rel, str) and isinstance(text, str) and len(text) <= LINE_BYTES
                            and type(n) is int and n > 0 and isinstance(clipped, bool)):
                        raise ValueError
                    try:
                        run._resolve(rel)  # parent recheck of root/blocked-path policy
                    except (ValueError, OSError):
                        state["reason"] = "path outside root skipped"
                        continue
                    yield f"{rel}:{n}:{text}" + (" [line clipped; narrow the search]" if clipped else "")
            except (ValueError, TypeError, KeyError):
                break
        if not done and not state.get("timeout"):
            state["reason"] = "search child protocol error or limit; results incomplete"


def _child_main():
    state, out = {}, sys.stdout.buffer
    try:
        req = json.loads(sys.stdin.buffer.read(MAX_REQUEST + 1)[:MAX_REQUEST])
        root, pattern, glob, skip = req["root"], req["pattern"], req["glob"], req["skip"]
        if not (isinstance(root, str) and isinstance(pattern, str) and isinstance(skip, list)
                and (glob is None or isinstance(glob, str)) and all(isinstance(s, str) for s in skip)):
            raise TypeError
        for rel, n, text, clipped in _walk(root, pattern, glob, set(skip), state):
            frame = json.dumps({"m": [rel, n, text, clipped]}).encode("ascii") + b"\n"
            if len(frame) > FRAME_BYTES:
                state["reason"] = "search scan/line limit reached"
                continue
            out.write(frame)
    except (ValueError, TypeError, KeyError, re.error, OSError):
        state["reason"] = "bad search request"
    if state.get("reason"):
        out.write(json.dumps({"r": state["reason"]}).encode("ascii") + b"\n")
    out.write(b'{"done": true}\n')
    out.flush()


def grep(run, pattern, glob, skip_dirs, *, offset=0, limit=None, use_rg=True):
    offset, limit = _paging(offset, limit)
    state = {}
    records = _rg_records(run, pattern, glob, skip_dirs, state) if use_rg else None
    try:
        if records is not None:
            try:
                return _render(run, records, offset, limit, state, "(no matches)")
            except FileNotFoundError:
                pass
            finally:
                records.close()
        records = _py_records(run, pattern, glob, skip_dirs, state)
        try:
            return _render(run, records, offset, limit, state, "(no matches)")
        finally:
            records.close()
    except re.error as exc:
        return f"error: bad regex: {exc}"


def _pending_chunks(text, size):
    pos = 0
    while pos < len(text):
        end = min(len(text), pos + size)
        newline = text.find("\n", pos, end)
        if newline >= 0:
            end = newline + 1
        yield text[pos:end]
        pos = end


def _read_source(run, path):
    uri = isinstance(path, str) and path.startswith(("skill://", "artifact://"))
    full = run.caps.resolve_read(path) if uri else run._resolve(path)
    pending = not uri and path in run.pending
    return full, pending, run.pending[path] if pending else None, uri


@contextlib.contextmanager
def _read_chunks(full, pending, text, size):
    if pending:
        yield _pending_chunks(text, size)
    else:
        with open(full, encoding="utf-8", errors="replace") as fh:
            yield iter(lambda: fh.readline(size), "")


def read_file(run, path, start_line, end_line, max_read):
    full, pending, text, uri = _read_source(run, path)
    ranged = start_line is not None or end_line is not None
    if not ranged:
        if not pending and not uri and path in run.read_whole:
            return (f"You already read all of {path} earlier in this run; the content "
                    "is above. Re-read a specific range with start_line/end_line if you "
                    "need to look again.")
        if pending:
            body = text[:max_read + 1]
        else:
            with open(full, encoding="utf-8", errors="replace") as fh:
                body = fh.read(max_read + 1)
        if len(body) > max_read:
            return (f"refused: {path} is over the {max_read:,}-char whole-read limit; "
                    "use grep, then read_file with start_line/end_line.")
        if not pending and not uri:
            run.read_whole.add(path)
        return ("(pending, not yet on disk)\n" if pending else "") + body

    a = max(1, int(start_line or 1))
    b = int(end_line or a + 200)
    if b < a:
        raise ValueError("end_line must be >= start_line")
    with _read_chunks(full, pending, text, max_read + 1) as chunks:
        out, used, scanned, line, last, cut = [], 0, 0, 1, a - 1, False
        for chunk in chunks:
            scanned += len(chunk)
            if scanned > MAX_SCAN_BYTES:
                cut = True
                break
            if a <= line <= b:
                room = max_read - used
                out.append(chunk[:room])
                used += min(room, len(chunk))
                last = line
                if len(chunk) > room:
                    cut = True
                    break
            if chunk.endswith("\n"):
                line += 1
            if line > b:
                break
        head = f"({path} lines {a}-{last}" + ("; pending, not yet on disk" if pending else "") + ")\n"
        return head + "".join(out) + ("\n...[range too large or scan incomplete; narrow it]" if cut else "")


READ_FILES_SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["requests"],
    "properties": {"requests": {
        "type": "array", "minItems": 1, "maxItems": 8,
        "items": {"type": "object", "additionalProperties": False, "required": ["path"],
                  "properties": {"path": {"type": "string", "minLength": 1},
                                 "start_line": {"type": "integer", "minimum": 1},
                                 "end_line": {"type": "integer", "minimum": 1}}}}}}


def validate_read_files(requests: object) -> tuple[dict, ...]:
    if not isinstance(requests, list) or not 1 <= len(requests) <= 8:
        raise ValueError("requests must contain 1 to 8 entries")
    entries = []
    for entry in requests:
        if (not isinstance(entry, dict) or set(entry) - {"path", "start_line", "end_line"}
                or not isinstance(entry.get("path"), str) or not entry["path"].strip()):
            raise ValueError("each request requires a nonempty path and optional line bounds")
        for key in ("start_line", "end_line"):
            if key in entry and (type(entry[key]) is not int or entry[key] < 1):
                raise ValueError("line bounds must be positive integers")
        if entry.get("end_line", entry.get("start_line", 1)) < entry.get("start_line", 1):
            raise ValueError("end_line must be >= start_line")
        entries.append(dict(entry))
    return tuple(entries)


def _json(value):
    return json.dumps(value, ensure_ascii=True, separators=(",", ":"))


def _bounded_error(text, make, cap):
    # Bound the field before rendering, including JSON escaping and all other statuses.
    text = str(text)[:300]
    while len(_json(make(text))) > cap:
        if not text:
            raise ValueError("read_files budget cannot hold an error envelope")
        text = text[:-1]
    return _json(make(text))


def _batch_page(run, request, index, fits):
    full, pending, text, _ = _read_source(run, request["path"])
    start, end = request.get("start_line", 1), request.get("end_line")
    content, next_line = [], start

    def row(complete, body=None, next_start=None):
        result = {"index": index, "status": "served",
                  "content": "".join(content) if body is None else body, "complete": complete}
        if not complete:
            result["next_range"] = {"start_line": next_line if next_start is None else next_start}
            if end is not None:
                result["next_range"]["end_line"] = end
        return result

    def lines(chunks):
        scanned, n, line = 0, 1, ""
        for chunk in chunks:
            scanned += len(chunk.encode("utf-8"))
            if scanned > MAX_SCAN_BYTES:
                raise ValueError(f"read scan limit reached at line {n}; narrow the range")
            if n >= start:
                line += chunk
                # Even the smallest final envelope must fit before assembly continues.
                if not fits(row(True, "".join(content) + line)):
                    raise ValueError(f"line {n} cannot fit the read_files result budget; narrow the range")
            if chunk.endswith("\n"):
                if n >= start:
                    yield n, line
                    line = ""
                n += 1
        if line:
            yield n, line

    with _read_chunks(full, pending, text, min(LINE_BYTES + 1, MAX_SCAN_BYTES + 1)) as chunks:
        source = lines(chunks)
        while True:
            try:
                n, line = next(source)
            except StopIteration:
                if not content and start > 1:
                    raise ValueError("start_line is past the end of the file")
                return row(True)
            except ValueError:
                if content:
                    return row(False)
                raise
            body = "".join(content) + line
            complete = end is not None and n >= end
            candidate = row(complete, body, n + 1)
            if fits(candidate):
                content.append(line)
                next_line = n + 1
                if complete:
                    return candidate
                continue
            # A final line may fit without continuation metadata. Look ahead once,
            # through the same bounded scan, never through an unbounded read.
            if fits(row(True, body)):
                try:
                    next(source)
                except StopIteration:
                    return row(True, body)
                except ValueError:
                    pass
            if content:
                return row(False)
            raise ValueError(f"line {n} cannot fit the read_files result budget; narrow the range")


def read_files(run, requests, max_read: int) -> str:
    if type(max_read) is not int or max_read < len(_json({"error": ""})):
        raise ValueError("read_files budget cannot hold an error envelope")
    try:
        entries = validate_read_files(requests)
    except ValueError as exc:
        return _bounded_error(exc, lambda text: {"error": text}, max_read)
    rows = [{"index": i, "status": "unserved", "reason": "budget"}
            for i in range(len(entries))]

    def packet(index, row):
        return {"results": rows[:index] + [row] + rows[index + 1:]}

    # Reserve every status and enough room to start an entry before any effects.
    if (len(_json({"results": rows})) > max_read or any(
            len(_json(packet(i, {"index": i, "status": "served", "content": "",
                                 "complete": True}))) > max_read for i in range(len(rows)))):
        return _bounded_error("read_files status budget exceeded", lambda text: {"error": text}, max_read)
    for i, request in enumerate(entries):
        fits = lambda row: len(_json(packet(i, row))) <= max_read
        if not fits({"index": i, "status": "served", "content": "", "complete": True}):
            break
        rows[i] = {"index": i, "status": "error", "error": ""}
        try:
            rows[i] = _batch_page(run, request, i, fits)
        except (OSError, ValueError) as exc:
            rendered = _bounded_error(exc, lambda text: packet(
                i, {"index": i, "status": "error", "error": text}), max_read)
            rows[i] = json.loads(rendered)["results"][i]
        if rows[i]["status"] == "served" and not rows[i]["complete"]:
            break
    return _json({"results": rows})


if __name__ == "__main__" and sys.argv[1:] == [CHILD_FLAG]:
    _child_main()

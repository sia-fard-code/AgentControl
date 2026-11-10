#!/usr/bin/env python3
"""Explicit paid read_files canary with metadata-only reporting; import/help are offline."""
import argparse
import contextlib
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import secrets
import sys
import time
import urllib.parse
import urllib.request
import uuid

_SPEC = importlib.util.spec_from_file_location(
    "_rf_context_live_canary", Path(__file__).resolve().parent / "context_live_canary.py")
base = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(base)
ROOT, MODELS, CanaryFailure = base.ROOT, base.MODELS, base.CanaryFailure
MAX_POSTS, MAX_INPUT = 30, 650000   # approved cumulative trial cap (spec section 10)
FRAMING = 64        # conservative per-message token framing added to UTF-8 byte count
LOCK_WAIT = 2.0     # bounded wait for a live holder; a stale lock is never stolen
READ_CONTRACT = "full-file-v2"


def _count(value):
    return value if type(value) is int and value >= 0 else None


def _full_read_form(request):
    """Accept native syntax only; coverage is checked against the current pinned body."""
    if (not isinstance(request, dict) or not isinstance(request.get("path"), str)
            or set(request) - {"path", "start_line", "end_line"}):
        return None
    for key in ("start_line", "end_line"):
        if key in request and (type(request[key]) is not int or request[key] < 1):
            return None
    start = request.get("start_line", 1)
    if start != 1:
        return None
    ranged = "start_line" in request or "end_line" in request
    # Verified existing read_file/read_files native default when a range is requested.
    end = request.get("end_line", start + 200) if ranged else None
    return ranged, end


def _native_line_count(text):
    # The native text reader uses newline boundaries, not splitlines' Unicode separators.
    return text.count("\n") + int(bool(text) and not text.endswith("\n"))


def _unique_object(pairs):
    obj = {}
    for key, value in pairs:
        if key in obj:
            raise ValueError
        obj[key] = value
    return obj


def projection(data, estimate, messages):
    """Named conservative PROJECTION, not a provider bound: max(bytes+framing, 2*estimate)."""
    if _count(estimate) is None:
        raise CanaryFailure("estimate_missing")
    return max(len(data) + FRAMING * max(int(messages), 1), 2 * estimate)


class BudgetLedger:
    """Append-only JSONL: header, reserve rows, monotonic usage rows. Fails closed."""

    def __init__(self, path):
        self.path = Path(path)
        self.lock = Path(str(self.path) + ".lock")

    @staticmethod
    def init(path, max_posts=MAX_POSTS, max_input=MAX_INPUT):
        if not (0 <= max_posts <= MAX_POSTS and 0 <= max_input <= MAX_INPUT):
            raise CanaryFailure("invalid_budget")
        header = {"kind": "budget", "budget_id": str(uuid.uuid4()), "root": str(ROOT),
                  "max_posts": max_posts, "max_input": max_input}
        try:
            with open(path, "x", encoding="utf-8", newline="\n") as f:
                f.write(json.dumps(header, sort_keys=True) + "\n")
                f.flush()
                os.fsync(f.fileno())
        except FileExistsError:
            raise CanaryFailure("budget_exists") from None
        return header

    def _load(self):
        try:
            data = self.path.read_bytes()
        except FileNotFoundError:
            raise CanaryFailure("budget_missing") from None
        except OSError:
            raise CanaryFailure("budget_corrupt") from None
        try:
            if not data.endswith(b"\n"):
                raise ValueError
            rows = [json.loads(line, object_pairs_hook=_unique_object)
                    for line in data.decode("utf-8").splitlines()]
            head = rows[0]
            if (set(head) != {"kind", "budget_id", "root", "max_posts", "max_input"} or
                    head["kind"] != "budget" or head["root"] != str(ROOT) or
                    not isinstance(head["budget_id"], str) or
                    str(uuid.UUID(head["budget_id"])) != head["budget_id"] or
                    _count(head["max_posts"]) is None or head["max_posts"] > MAX_POSTS or
                    _count(head["max_input"]) is None or head["max_input"] > MAX_INPUT):
                raise ValueError
            proj, used = [], {}
            for row in rows[1:]:
                kind, rid = row.get("kind"), row.get("id")
                if kind == "reserve" and set(row) == {"kind", "id", "projection"} and \
                        type(rid) is int and rid == len(proj) + 1 and _count(row["projection"]) is not None:
                    proj.append(row["projection"])
                elif kind == "usage" and set(row) == {"kind", "id", "input"} and \
                        type(rid) is int and 1 <= rid <= len(proj) and \
                        _count(row["input"]) is not None and row["input"] > used.get(rid, -1):
                    used[rid] = row["input"]
                else:
                    raise ValueError
        except (ValueError, TypeError, AttributeError, IndexError, UnicodeError):
            raise CanaryFailure("budget_corrupt") from None
        return head, proj, used

    def state(self):
        head, proj, used = self._load()
        charged = sum(max(p, used.get(i + 1, 0)) for i, p in enumerate(proj))
        return {"posts": len(proj), "projected": sum(proj), "excess": charged - sum(proj),
                "charged": charged, "reported_known": len(used),
                "reported_missing": len(proj) - len(used)}

    def overrun(self):
        head, _, _ = self._load()
        return self.state()["charged"] > head["max_input"]

    def _locked_append(self, decide):
        deadline = time.monotonic() + LOCK_WAIT
        while True:
            try:
                fd = os.open(self.lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                break
            except FileExistsError:
                if time.monotonic() > deadline:
                    raise CanaryFailure("budget_locked") from None
                time.sleep(0.005)
            except OSError:
                raise CanaryFailure("budget_locked") from None
        try:
            row, value = decide(*self._load())
            if row is not None:
                with open(self.path, "a", encoding="utf-8", newline="\n") as f:
                    f.write(json.dumps(row, sort_keys=True) + "\n")
                    f.flush()
                    os.fsync(f.fileno())
            return value
        finally:
            os.close(fd)
            os.unlink(self.lock)

    def reserve(self, amount):
        """Durably reserve one POST + projected input BEFORE any network; no refunds."""
        def decide(head, proj, used):
            charged = sum(max(p, used.get(i + 1, 0)) for i, p in enumerate(proj))
            if (_count(amount) is None or len(proj) + 1 > head["max_posts"] or
                    charged + amount > head["max_input"]):
                raise CanaryFailure("budget_exhausted")
            return {"kind": "reserve", "id": len(proj) + 1, "projection": amount}, len(proj) + 1
        return self._locked_append(decide)

    def reconcile(self, rid, reported):
        """Record known gross input monotonically; duplicates/lower snapshots add nothing."""
        def decide(head, proj, used):
            if type(rid) is not int or not 1 <= rid <= len(proj) or _count(reported) is None:
                raise CanaryFailure("budget_corrupt")
            if reported <= used.get(rid, -1):
                return None, None
            return {"kind": "usage", "id": rid, "input": reported}, None
        return self._locked_append(decide)


def parse(argv=None):
    p = base.Parser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--run-live", action="store_true", help="required for any paid generation")
    p.add_argument("--init-budget", action="store_true",
                   help="create a FRESH Temp ledger (--budget) with the approved caps; no network")
    p.add_argument("--case", help="claude-chat | gpt-responses")
    p.add_argument("--mode", help="baseline (claude-chat only) | batch")
    p.add_argument("--engine", help="source | installed")
    p.add_argument("--fixture-count", default="2", help="2..8 random-sentinel fixtures")
    for name in ("out", "launch-id", "launch-receipt", "budget"):
        p.add_argument("--" + name)
    return p.parse_args(argv)


def _existing_temp_file(value):
    p = Path(value) if value else None
    temp = Path(base.tempfile.gettempdir()).resolve()
    if p is None or not p.is_absolute() or p != p.resolve() or not base._inside(p, temp):
        return None
    return p if p.is_file() and not p.is_symlink() else None


def validate(ns):
    if ns.init_budget:
        return None if not ns.run_live and base._fresh_temp(ns.budget) else "invalid_budget"
    error = base.validate(ns)   # run_live, case, engine, launch id, fresh out/receipt
    if error == "run_live_required":
        return error
    if error == "invalid_case" or (not error and ns.case not in ("claude-chat", "gpt-responses")):
        return "invalid_case"
    if ns.mode not in ("baseline", "batch") or (ns.mode == "baseline" and ns.case != "claude-chat"):
        return "invalid_mode"
    if error:
        return error
    if ns.fixture_count not in [str(n) for n in range(2, 9)]:
        return "invalid_fixture_count"
    budget = _existing_temp_file(ns.budget)
    if budget is None or budget in (Path(ns.out), Path(ns.launch_receipt)):
        return "invalid_budget"
    return None


def _fixtures(launch_id, count):
    """Ignored ROOT/.claude/agent-runs fixtures; random sentinels never enter the prompt."""
    directory = ROOT / ".claude" / "agent-runs" / "read-files-fixtures" / launch_id
    directory.mkdir(parents=True, exist_ok=False)
    files = {}
    for i in range(count):
        path = directory / ("fixture-%02d.txt" % (i + 1))
        text = "synthetic read_files fixture %d\nsentinel %s\n" % (i + 1, secrets.token_hex(16))
        with path.open("x", encoding="utf-8", newline="\n") as f:
            f.write(text)
        files[path] = text
    return files


def _brief(agents, files, mode):
    tool = ("ONE native read_files call listing every fixture path in order"
            if mode == "batch" else "one native read_file call per fixture, one per reply, in order")
    return (READ_CONTRACT + ': Read AGENTS.md first; prefer read_file({"path":"AGENTS.md"}). '
            "Native ranges are allowed only with start_line=1 and an explicit or native-default "
            "end covering the complete file. This same full-coverage rule applies to every fixture. "
            "Then read the listed synthetic fixtures with " +
            tool + ", complete contents. Use no other tools, commands, MCP, "
            "writes, nested dispatch or other reads. Then call finish with a summary quoting "
            "each fixture's sentinel value exactly. All data is synthetic. Dispatch anomalies "
            "remain active scope; preserve bounded evidence and report blockers.\n"
            "Fixture paths (in order, relative to the root):\n" +
            "\n".join(p.relative_to(ROOT).as_posix() if ROOT in p.parents else p.as_posix()
                      for p in files) +
            "\nThe complete CURRENT AGENTS.md is pinned here independently of tool reads:\n" + agents)


METRICS = {"case_elapsed_s": "monotonic seconds around the engine main() call",
           "headers_s": "monotonic seconds from urlopen call to its return (response headers)",
           "first_event_s": "urlopen call to first complete consumed SSE data JSON event; not TTFT",
           "first_text_s": "urlopen call to first consumed nonempty assistant-text delta; null if none"}


def _gross_input(usage, route):
    """Mirror context_tokens.input_tokens_from_usage: inclusive prompt/Responses, disjoint native."""
    if not isinstance(usage, dict):
        return None
    if _count(usage.get("prompt_tokens")) is not None:
        return usage["prompt_tokens"]
    native = [_count(usage.get(k)) for k in
              ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")]
    if route == "responses" and not any(k in usage for k in
                                        ("cache_read_input_tokens", "cache_creation_input_tokens")):
        return native[0]
    return None if None in native else sum(native)


def _attempt_error(post, exc):
    name = type(exc).__name__
    post["error_class"] = name if name in (
        "HTTPError", "URLError", "TimeoutError", "ConnectionResetError", "OSError") else "other"
    status = getattr(exc, "code", None)
    post["http_status"] = status if type(status) is int and 100 <= status <= 599 else None
    post["outcome"] = "network_error"


class Timed:
    """Pass-through response proxy: same bytes/order/exceptions; bounded transient parsing."""

    def __init__(self, inner, observer, post, started):
        self.inner, self.observer, self.post, self.started = inner, observer, post, started
        self.usage, self.output = None, None
        self.buffer, self.format, self.invalid, self.eof = b"", None, False, False

    def __getattr__(self, name):
        return getattr(self.inner, name)

    def __enter__(self):
        self.inner.__enter__()
        return self

    def __exit__(self, *exc):
        try:
            return self.inner.__exit__(*exc)
        except Exception as error:
            _attempt_error(self.post, error)
            raise
        finally:
            if exc[1] is None:
                self._feed(b"", eof=True)
            else:
                _attempt_error(self.post, exc[1])
            self._settle()

    def _bad(self):
        self.invalid, self.buffer = True, b""
        self.observer.error = self.observer.error or "observation_parse"
        if self.post["outcome"] != "network_error":
            self.post["outcome"] = "parse_error"

    def _object(self, data, event=False):
        if data.strip() == b"[DONE]" and event:
            return
        try:
            obj = json.loads(data)
            if not isinstance(obj, dict):
                raise ValueError
            choices = obj.get("choices", [])
            if not isinstance(choices, list) or any(not isinstance(c, dict) for c in choices):
                raise ValueError
            if event:
                now = round(time.monotonic() - self.started, 6)
                if self.post["first_event_s"] is None:
                    self.post["first_event_s"] = now
                text = any(isinstance(c.get("delta"), dict) and
                           isinstance(c["delta"].get("content"), str) and c["delta"]["content"]
                           for c in choices) or (obj.get("type") == "response.output_text.delta"
                           and isinstance(obj.get("delta"), str) and obj["delta"])
                if text and self.post["first_text_s"] is None:
                    self.post["first_text_s"] = now
            usage = obj.get("usage")
            if usage is None and isinstance(obj.get("response"), dict):
                usage = obj["response"].get("usage")
            value = _gross_input(usage, self.post["route"])
            if value is not None:
                self.usage = max(self.usage or 0, value)
            if isinstance(usage, dict):
                out = _count(usage.get("completion_tokens", usage.get("output_tokens")))
                self.output = max(self.output or 0, out) if out is not None else self.output
        except (ValueError, TypeError, UnicodeError, RecursionError):
            self._bad()

    def _feed(self, data, eof=False):
        if self.invalid or self.eof:
            return
        self.buffer += data
        if self.format is None and self.buffer.strip():
            self.format = "json" if self.buffer.lstrip().startswith(b"{") else "sse"
        if self.format == "json":
            if len(self.buffer) > 1 << 24:
                self._bad()
            elif eof:
                self._object(self.buffer)
                self.buffer = b""
        else:
            while b"\n" in self.buffer and not self.invalid:
                line, self.buffer = self.buffer.split(b"\n", 1)
                self._line(line)
            if len(self.buffer) > 1 << 20:
                self._bad()
            elif eof and self.buffer:
                self._line(self.buffer)
                self.buffer = b""
        if eof:
            self.eof = True
            if self.post["outcome"] != "network_error" and not self.invalid:
                self.post["outcome"] = "complete" if self.usage is not None else "usage_unknown"
        self._settle()

    def _line(self, line):
        if len(line) > 1 << 20:
            self._bad()
        elif line.startswith(b"data:"):
            self._object(line[5:], event=True)
        elif line.strip() and not line.startswith((b":", b"event:", b"id:", b"retry:")):
            self._bad()

    def __iter__(self):
        try:
            for data in self.inner:
                self._feed(data)
                yield data
        except Exception as exc:
            _attempt_error(self.post, exc)
            raise
        self._feed(b"", eof=True)

    def read(self, *args):
        try:
            data = self.inner.read(*args)
        except Exception as exc:
            _attempt_error(self.post, exc)
            raise
        self._feed(data, eof=(not args or args[0] is None or args[0] < 0 or
                              (not data and args[0] != 0)))
        return data

    def readline(self, *args):
        try:
            data = self.inner.readline(*args)
        except Exception as exc:
            _attempt_error(self.post, exc)
            raise
        self._feed(data, eof=not data and (not args or args[0] != 0))
        return data

    def _settle(self):
        self.post["usage_output"] = self.output
        previous = self.post["usage_input"]
        if self.usage is None or (previous is not None and self.usage <= previous):
            return
        self.post["usage_input"], self.post["usage_output"] = self.usage, self.output
        try:
            self.observer.book.reconcile(self.post["reservation"], self.usage)
            if self.observer.book.overrun():
                self.observer.error = self.observer.error or "budget_overrun"
        except CanaryFailure as exc:
            self.observer.error = self.observer.error or exc.code
        except OSError:
            self.observer.error = self.observer.error or "budget_io"
        except Exception:
            self.observer.error = self.observer.error or "budget_observation"


class Observer:
    def __init__(self, ns, engine, ce, files, book):
        self.ns, self.engine, self.ce, self.book = ns, engine, ce, book
        self.expected = {ROOT / "AGENTS.md": None, **files}
        self.reads, self.read_proofs, self.delivered, self.posts = [], [], set(), []
        self.error = None
        self.journal_path, self.journal = None, None

    def mark_journal(self):
        if self.journal_path is not None:
            try:
                self.journal = base.journal_mark(self.journal_path, self.journal)
            except CanaryFailure as exc:
                self.fail(exc.code)

    def fail(self, code):
        self.error = self.error or code
        raise CanaryFailure(code)

    def _path(self, raw):
        path = Path(raw)
        return (ROOT / path).resolve() if not path.is_absolute() else path.resolve()

    def _record(self, path, text):
        data = text.encode("utf-8")
        self.reads.append(str(path))
        self.expected[path] = text
        self.read_proofs.append({"complete": True, "bytes": len(data),
                                 "sha256": hashlib.sha256(data).hexdigest()})

    def dispatch(self, original, runner, name, argv):
        """Deny everything but the exact planned reads BEFORE any effect."""
        order = list(self.expected)
        if not isinstance(argv, dict):
            self.fail("tool_forbidden")
        if self.reads and name in ("read_file", "read_files") and str(order[0]) not in self.delivered:
            self.fail("agents_not_delivered")
        if name == "read_file":
            form = _full_read_form(argv)
            if form is None:
                self.fail("read_forbidden")
            n = len(self.reads)
            if (n >= len(order) or self._path(argv["path"]) != order[n] or
                    (n > 0 and self.ns.mode == "batch")):
                self.fail("read_sequence")
            path = order[n]
            expected = path.read_text(encoding="utf-8")
            ranged, end = form
            count = _native_line_count(expected)
            if ranged and end < count:
                self.fail("read_forbidden")
            value = original(runner, name, argv)
            rendered = ("(%s lines 1-%d)\n" % (argv["path"], count) + expected
                        if ranged else expected)
            if value != rendered:
                self.fail("read_incomplete")
            self._record(path, expected)
            return value
        if name != "read_files" or self.ns.mode != "batch":
            self.fail("tool_forbidden")
        reqs = argv.get("requests")
        if (set(argv) != {"requests"} or not isinstance(reqs, list) or
                any(_full_read_form(r) is None for r in reqs)):
            self.fail("read_forbidden")
        if len(self.reads) != 1 or [self._path(r["path"]) for r in reqs] != order[1:]:
            self.fail("read_sequence")
        bodies = [p.read_text(encoding="utf-8") for p in order[1:]]
        if any(_full_read_form(r)[0] and _full_read_form(r)[1] < _native_line_count(text)
               for r, text in zip(reqs, bodies)):
            self.fail("read_forbidden")
        value = original(runner, name, argv)
        try:
            rows = json.loads(value)["results"]
            ok = [r == {"index": i, "status": "served", "content": bodies[i], "complete": True}
                  for i, r in enumerate(rows)] == [True] * len(bodies) and len(rows) == len(bodies)
        except (ValueError, TypeError, KeyError, IndexError):
            ok = False
        if not ok:
            self.fail("read_incomplete")
        for path, text in zip(order[1:], bodies):
            self._record(path, text)
        return value

    def observe_delivery(self, body):
        """Delivered = exact read text inside an OUTGOING tool-result field (decoded JSON too)."""
        probe = base.Observer.__new__(base.Observer)
        probe.expected = {str(p): t for p, t in self.expected.items() if str(p) in self.reads}
        probe.delivered = self.delivered
        base.Observer.observe_delivery(probe, body)

    def urlopen(self, original, request, *args, **kwargs):
        data = getattr(request, "data", None)
        if data is None:
            return original(request, *args, **kwargs)
        if self.error:
            self.fail(self.error)
        url = getattr(request, "full_url", None)
        try:
            path = urllib.parse.urlsplit(url).path if isinstance(url, str) else None
        except ValueError:
            path = None
        self.mark_journal()
        meta = base.wire_metadata(path, data)
        scope = self.ce.current()
        route = "responses" if self.ns.case == "gpt-responses" else "chat"
        cap = "max_output_tokens" if route == "responses" else "max_completion_tokens"
        phase = getattr(scope, "phase", None)
        limit = getattr(getattr(scope, "policy", None), "reply_tokens", None)
        if (not meta.get("valid") or meta.get("model") != MODELS[self.ns.case] or
                meta.get("route") != route or meta.get("paired") is not True or
                meta.get("effort_form") != ("nested" if route == "responses" else "flat") or
                meta.get("effort") != base._effort(self.ns.case) or
                meta.get("cap_field") != cap or _count(limit) is None or
                meta.get("cap_value") != limit or phase not in ("worker", "report", "compaction")):
            self.fail("wire_invalid")
        estimate = getattr(getattr(scope, "last_estimate", None), "tokens", None)
        rows = meta["message_count"] + meta["input_count"]
        try:
            amount = projection(data, estimate, rows)
            reservation = self.book.reserve(amount)   # durable fsync append before network
        except CanaryFailure as exc:
            self.fail(exc.code)
        except OSError:
            self.fail("budget_io")
        self.observe_delivery(json.loads(data))
        post = {"reservation": reservation, "projection": amount, "estimate": estimate,
                "request_bytes": len(data), "route": meta["route"],
                "effort_form": meta["effort_form"], "effort": meta["effort"],
                "cap_field": meta["cap_field"], "cap_value": meta["cap_value"],
                "phase": getattr(scope, "phase", None) if getattr(scope, "phase", None) in
                ("worker", "report", "compaction") else None,
                "headers_s": None, "first_event_s": None, "first_text_s": None,
                "usage_input": None, "usage_output": None, "outcome": "pending",
                "error_class": None, "http_status": None}
        self.posts.append(post)
        started = time.monotonic()
        try:
            response = original(request, *args, **kwargs)
        except Exception as exc:
            _attempt_error(post, exc)
            raise
        post["headers_s"] = round(time.monotonic() - started, 6)
        status = getattr(response, "status", None)
        post["http_status"] = status if type(status) is int and 100 <= status <= 599 else None
        post["outcome"] = "headers"
        return Timed(response, self, post, started)


def _engine_argv(ns, ids, brief):
    """Normal ordinary run: no context window/reply/safety, stream or compaction overrides."""
    argv = ["delegate.py", "-C", str(ROOT), "--profile", "resilient-v1",
            "--context-policy", "model-v1", "--skill", "ponytail", "--label", ids["run_id"],
            "--max-iters", "8" if ns.mode == "batch" else "14", "--launch-id", ns.launch_id,
            "--launch-receipt", ns.launch_receipt, "--transcript", str(ids["transcript"]),
            "--model", MODELS[ns.case], "--effort", base._effort(ns.case)]
    return argv + (["--read-files"] if ns.mode == "batch" else []) + ["-t", brief]


def _execute_loaded(ns, ids, files, brief, engine, book):
    observer = Observer(ns, engine, engine.capability_module("context_engine"), files, book)
    observer.journal_path = ids["messages"]
    old_urlopen, old_dispatch, old_argv = urllib.request.urlopen, engine.Runner.dispatch, sys.argv
    started = time.monotonic()
    try:
        urllib.request.urlopen = lambda req, *a, **kw: observer.urlopen(old_urlopen, req, *a, **kw)
        engine.Runner.dispatch = lambda runner, name, argv: observer.dispatch(old_dispatch, runner, name, argv)
        sys.argv = _engine_argv(ns, ids, brief)
        try:
            code = engine.main()
            code = 0 if code is None else code if type(code) is int else 1
        except SystemExit as exc:
            code = 0 if exc.code is None else exc.code if type(exc.code) is int else 1
        except Exception:
            code = None   # engine outcome unknown; metadata is still collected below
    finally:
        urllib.request.urlopen, engine.Runner.dispatch, sys.argv = old_urlopen, old_dispatch, old_argv
    elapsed = round(time.monotonic() - started, 6)
    return _collect(ns, ids, observer, files, code, elapsed)


def _finish_proof(rows, files):
    """Finish must follow every read result and quote each fixture's random sentinel."""
    finishes = [i for i, r in enumerate(rows) if base._finish_ok(r) is True]
    results = [i for i, r in enumerate(rows) if r.get("role") == "tool"]
    if not finishes or len(results) < 2 or finishes[-1] < results[-2]:
        return False
    summary = json.loads(rows[finishes[-1]]["tool_calls"][0]["function"]["arguments"])["summary"]
    return all(text.split()[-1] in summary for text in files.values())


def _usage_coverage(rows, posts):
    """Compare phase totals, not row counts: recovery may merge per-loop usage."""
    if not rows or not posts:
        return "usage_unknown"
    observed, journal = {}, {}
    for post in posts:
        phase = post["phase"]
        if post["usage_input"] is None or post["usage_output"] is None:
            return "usage_unknown"
        totals = observed.setdefault(phase, [0, 0])
        totals[0] += post["usage_input"]
        totals[1] += post["usage_output"]
    for row in rows:
        phase = {"loop": "worker", "wrapup": "report"}.get(row.get("phase"), row.get("phase"))
        raw = row.get("raw")
        # Native input is disjoint from cache; Chat/Responses prompt input is inclusive.
        route = posts[0]["route"]
        gross = _gross_input(raw, route) if isinstance(raw, dict) else _count(row.get("sent"))
        if isinstance(raw, dict):
            sent = _count(raw.get("prompt_tokens", raw.get("input_tokens")))
            if sent != _count(row.get("sent")):
                return "usage_mismatch"
        output = _count(row.get("recv"))
        if phase not in ("worker", "report", "compaction") or gross is None or output is None:
            return "usage_unknown"
        totals = journal.setdefault(phase, [0, 0])
        totals[0] += gross
        totals[1] += output
    return None if observed == journal else "usage_mismatch"


def _collect(ns, ids, observer, files, code, elapsed):
    """Whitelisted metadata only: fixed codes, counts, hashes, timings and artifact PATHS."""
    state = None
    try:
        state = observer.book.state()
    except CanaryFailure as exc:
        observer.error = observer.error or exc.code
    except OSError:
        observer.error = observer.error or "budget_io"
    result = {"ok": False, "error": None, "observer_error": observer.error,
              "case": ns.case, "mode": ns.mode, "engine": ns.engine,
              "read_contract": READ_CONTRACT,
              "root": str(ROOT), "launch_id": ns.launch_id, "run_id": ids["run_id"],
              "engine_pid": os.getpid(), "engine_exit": code, "normal_finish": False,
              "paired": False, "finish_proof": False, "read_count": len(observer.reads),
              "delivered_count": len(set(observer.reads) & observer.delivered),
              "read_proofs": observer.read_proofs, "case_elapsed_s": elapsed,
              "metrics": METRICS, "posts": observer.posts, "usage": None, "budget": state,
              "claude_cap_enforcement": None,
              "artifacts": {**{k: str(ids[k]) for k in ("transcript", "index", "messages", "usage")},
                            "receipt": ns.launch_receipt, "budget": str(observer.book.path),
                            "fixtures": [str(p) for p in files]}}
    try:
        end = base.identity_check(ns, ids, os.getpid(), base._jsonl(Path(ns.launch_receipt)),
                                  base._jsonl(ids["index"]))
        observer.mark_journal()
        usage_rows = base._jsonl(ids["usage"])
        result["usage"] = base.usage_metadata(usage_rows)
        coverage = _usage_coverage(usage_rows, observer.posts)
        rows = base._jsonl(ids["messages"])
        journal = base.journal_metadata(rows)
        result["paired"], result["normal_finish"] = journal["paired"], journal["normal_finish"]
        result["finish_proof"] = _finish_proof(rows, files)
        error = (observer.error or
                 ("network_anomaly" if any(p["outcome"] == "network_error" for p in observer.posts) else None) or
                 coverage or
                 ("terminal_failed" if end.get("status") != "complete" or end.get("refused") != 0 or end.get("stop_reason") else None) or
                 (None if result["normal_finish"] else "finish_missing") or
                 (None if len(observer.reads) == len(files) + 1 else "read_sequence") or
                 (None if result["delivered_count"] == len(files) + 1 else "read_not_delivered") or
                 (None if result["finish_proof"] else "finish_proof"))
    except CanaryFailure as exc:
        error = observer.error or exc.code
    except (OSError, ValueError, TypeError, KeyError, UnicodeError):
        error = "artifact_invalid"
    result["observer_error"] = observer.error
    if any(p["outcome"] == "network_error" for p in observer.posts):
        error = observer.error or "network_anomaly"
    if code != 0:
        error = error or "engine_failed"
    result["ok"], result["error"] = error is None, error
    return result, code if type(code) is int and code != 0 else 1 if error else 0


def _execute(ns, out):
    engine_path = (ROOT / "engine" / "delegate.py" if ns.engine == "source"
                   else Path.home() / ".claude" / "bin" / "delegate.py")
    ids = base._run_paths(ns, out)
    files = _fixtures(ns.launch_id, int(ns.fixture_count))
    agents = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
    with base._loaded_engine(engine_path) as engine:
        return _execute_loaded(ns, ids, files, _brief(agents, files, ns.mode), engine,
                               BudgetLedger(ns.budget))


def main(argv=None):
    out, owns_out, ns = None, False, None
    try:
        ns = parse(argv)
        error = validate(ns)
        if error:
            raise CanaryFailure(error)
        if ns.init_budget:
            print(json.dumps({"ok": True, **BudgetLedger.init(ns.budget)}, separators=(",", ":")))
            return 0
        out = Path(ns.out)
        out.mkdir(exist_ok=False)
        owns_out = True
        old_index = os.environ.get("CLAUDE_AGENT_RUN_INDEX")
        try:
            os.environ["CLAUDE_AGENT_RUN_INDEX"] = str(out / "index.jsonl")
            with open(os.devnull, "w", encoding="utf-8") as discard:
                with contextlib.redirect_stdout(discard), contextlib.redirect_stderr(discard):
                    result, code = _execute(ns, out)
        finally:
            if old_index is None:
                os.environ.pop("CLAUDE_AGENT_RUN_INDEX", None)
            else:
                os.environ["CLAUDE_AGENT_RUN_INDEX"] = old_index
    except CanaryFailure as exc:
        result, code = {"ok": False, "error": exc.code}, 1
    except Exception:
        result, code = {"ok": False, "error": "canary_internal"}, 1
    if owns_out:
        try:
            with (out / "result.json").open("x", encoding="utf-8") as f:
                json.dump(result, f, separators=(",", ":"))
        except Exception:
            result, code = dict(result, ok=False, error="result_write_failed"), code or 1
    print(json.dumps(result, separators=(",", ":")))
    return code


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""Run a task on a remote model, executing its tool calls locally.

The remote model drives a loop: it asks to read files, grep, or run commands;
this runner executes them here and feeds results back; it iterates until it
calls finish(). Only the final report returns to the caller, so an agent can
burn a hundred thousand tokens exploring a repo without any of it entering the
coordinator's context.

USAGE
  delegate.py -t "fix the failing test in foo.py" -C /path/to/repo
  delegate.py -f brief.md -C /path/to/repo   # read the task from a file;
      # prefer this in PowerShell, where "$(cat brief.md)" mangles the brief.

TWO TOOL PROTOCOLS
  native  - the API's `tools` parameter (with tool_choice "auto").
  prompt  - tools described in the system prompt; the model replies with a
            ```tool {...}``` block that we parse. This is what Cline calls
            "disable native tools".
  auto    - see auto_tool_mode(): native for anthropic_tool_call.* and gpt-*,
            prompt for everything else. Prompt mode stays available for any
             model with --tool-mode prompt. Native-capable models also require
             --allow-prompt-tools for an operator-approved compatibility test.
            Measured 2026-10-02 on the upgraded gateway: a 6-step tool task at
            -e high finished in 13-19 s with 0 retries in native mode for
            gpt-6-sol, gpt-6.1-sol, gpt-6-luna and gpt-5.6-sol; prompt mode
            took 20-292 s, and gpt-6-luna needed 7 nudges and 10 retries.

  How effort is sent (measured on Open WebUI 0.11.4):
  * GPT models (gpt-5.6-sol, gpt-6-sol, gpt-6-luna, gpt-6.1-sol) answer 400 to
    ANY request carrying the flat top-level `reasoning_effort`, with or without
    tools ("this parameter has moved to 'reasoning.effort'"). They get the
    nested `"reasoning": {"effort": E}` instead, and with it native tools work
    too: tool_calls come back and a tool-result turn completes. Native mode no
    longer has to drop the effort.
  * Accepted values: gpt-5.6-sol, gpt-6-sol, gpt-6-luna take none, low, medium,
    high, xhigh, max. gpt-6.1-sol rejects "none" (see _EFFORT_SUBSTITUTES). No
    model accepts "minimal".
  * anthropic_tool_call.* still takes the flat `reasoning_effort` (the gateway
    ignores it), so it keeps getting it.

VISIBILITY
  Every step is appended live to a transcript (--transcript, default under
  .claude/agent-runs/) and echoed to stderr: the model's own narration, the
  tool call, and a preview of the result. Tail it to watch the agent think:
      tail -f .claude/agent-runs/<name>.md

STEERING
  Before each turn the runner reads <transcript>.steer. Anything you put there
  is injected as a user message and the file is cleared, so you can redirect a
  run in flight without killing it:
      echo "stop reading main.ts, focus on src/library" > <transcript>.steer

SAFETY (deliberately conservative -- widen only on request)
  * paths confined to the repo root; .. and absolute escapes refused
  * commands matched against an argv allowlist; shell=False throughout, so no
    pipes, globbing, chaining or substitution is possible
  * that allowlist is per-repo: a repo may ship .claude/agent-policy.json to
    REPLACE the built-in one, plus the directories grep/list_dir skip (see
    load_policy). One shared script serving many repos, each with its own
    boundary -- as opposed to a forked copy per repo, whose security rules then
    drift apart silently.
  * write_file never touches disk; writes are emitted as a patch for review
  * hard iteration cap
  * transport timeouts (client-side read timeout) and non-advancing replies
    (a 200 that carried no tool action) are both treated as the same
    retryable condition and retried with jittered backoff, never crashing
    the run; genuinely terminal HTTP auth failures (401/403) still exit
"""
import argparse
import atexit
import base64
import contextlib
import copy
import datetime
import difflib
import fnmatch
import hashlib
import http.client
import io
import json
import os
import random
import re
import shutil
import signal
import socket
import stat
import struct
import subprocess
import sys
import time
import uuid
import warnings
import urllib.error
import urllib.request

# THIS DIRECTORY IS THE CANONICAL COPY of the harness. Every repo on this machine
# runs this file; no repo should vendor its own. If you find a copy of delegate.py
# inside a repo, treat THIS one as newer unless its HARNESS_VERSION says otherwise
# -- overwriting this file from an older repo copy silently removes features that
# other repos depend on (notably per-repo policy loading, added in 2). A repo
# pins what it needs by asserting HARNESS_VERSION; see the harness README.
HARNESS_VERSION = 6

# Machine-index contract version. Schema 2 adds process identity (engine_pid) to
# start records and uses explicit terminal statuses (complete|failed|interrupted)
# on end records so AgentControl can validate liveness without heuristics.
INDEX_SCHEMA = 2

DIAGNOSTICS = None
_DIAGNOSTIC_REQUEST = 0


def diagnostic(event, **fields):
    # Fail open: a broken emitter must never mask the observed operation's outcome.
    if DIAGNOSTICS is not None:
        try:
            return DIAGNOSTICS.emit(event, **fields)
        except Exception:
            return False
    return False


DIAG_TOOL_NAMES = {"read_file", "read_files", "view_image", "list_dir", "grep", "run_command", "write_file",
                   "replace_in_file", "read_command_output", "read_context_result",
                   "list_mcp_tools", "load_skill", "finish"}


def diag_tool_name(name):
    """Fixed enum only: never echo a model-chosen tool name into diagnostics."""
    if isinstance(name, str) and name.startswith(MCP_PREFIX):
        return "mcp"
    return name if isinstance(name, str) and name in DIAG_TOOL_NAMES else "other"


def _safe_exception_fields(exc):
    """Bounded class + engine basename/line only; never the message or traceback."""
    try:
        return capability_module("harness_diagnostics").exception_fields(exc)
    except Exception:
        return {}


def request_evidence(route, timeout):
    global _DIAGNOSTIC_REQUEST
    _DIAGNOSTIC_REQUEST += 1
    diagnostic("request_start", attempt=_DIAGNOSTIC_REQUEST, route=route,
               timeout_s=timeout, phase="request")
    return time.monotonic()


def request_error(reason, started, **fields):
    diagnostic("request_error", reason=reason, elapsed_s=time.monotonic() - started,
               attempt=_DIAGNOSTIC_REQUEST, **fields)


def normalize_usage(raw):
    """Return canonical usage counters without collapsing unknowns to zero."""
    result = {"sent": None, "recv": None, "cache_read": None,
              "cache_write": None}
    if not isinstance(raw, dict):
        return result

    def value(obj, key):
        if not isinstance(obj, dict):
            return None
        try:
            candidate = obj.get(key)
        except Exception:
            return None
        # Objects are used only to descend into detail mappings. Scalar usage
        # values must be measured non-negative integer counters, never content.
        if isinstance(candidate, dict):
            return candidate
        return candidate if isinstance(candidate, int) and not isinstance(candidate, bool) and candidate >= 0 else None

    for field, keys in (("sent", ("prompt_tokens", "input_tokens")),
                        ("recv", ("completion_tokens", "output_tokens"))):
        for key in keys:
            candidate = value(raw, key)
            if candidate is not None:
                result[field] = candidate
                break

    for key in ("cache_read_input_tokens",):
        candidate = value(raw, key)
        if candidate is not None:
            result["cache_read"] = candidate
            break
    else:
        details = value(raw, "prompt_tokens_details")
        candidate = value(details, "cached_tokens")
        if candidate is None:
            # Responses-API usage (GPT on the upgraded gateway).
            candidate = value(value(raw, "input_tokens_details"), "cached_tokens")
        if candidate is None:
            candidate = value(raw, "cached_tokens")
        if candidate is None:
            candidate = value(raw, "cache_read_tokens")
        result["cache_read"] = candidate

    candidate = value(raw, "cache_creation_input_tokens")
    if candidate is not None:
        result["cache_write"] = candidate
    else:
        creation = value(raw, "cache_creation")
        buckets = [value(creation, key) for key in
                   ("ephemeral_5m_input_tokens", "ephemeral_1h_input_tokens")]
        buckets = [bucket for bucket in buckets if bucket is not None]
        if buckets:
            try:
                result["cache_write"] = sum(buckets)
            except Exception:
                result["cache_write"] = None
        else:
            details = value(raw, "prompt_tokens_details")
            candidate = value(details, "cache_write_tokens")
            if candidate is None:
                candidate = value(value(raw, "input_tokens_details"),
                                  "cache_write_tokens")
            if candidate is None:
                candidate = value(raw, "cache_write_tokens")
            result["cache_write"] = candidate
    return {key: val if isinstance(val, int) and not isinstance(val, bool) and val >= 0
            else None for key, val in result.items()}


# Only these usage keys and nested prompt-detail keys may leave the engine in a
# sanitized raw mapping. Everything else (prompts, tool results, response bodies)
# is dropped so the machine-wide accounting state never stores conversation data.
_USAGE_ALLOWLIST = (
    "prompt_tokens", "completion_tokens", "total_tokens",
    "input_tokens", "output_tokens",
    "cache_read_input_tokens", "cache_creation_input_tokens", "cached_tokens",
    "cache_read_tokens", "cache_write_tokens",
)
_USAGE_NESTED_COMMON_KEYS = (
    "cached_tokens", "cache_write_tokens", "cache_read_tokens",
    "ephemeral_5m_input_tokens", "ephemeral_1h_input_tokens",
)
# Each nested detail object carries its own key allowlist. The Responses-API
# objects (input_/output_tokens_details) are kept narrow on purpose.
_USAGE_NESTED_KEYS = {
    "prompt_tokens_details": _USAGE_NESTED_COMMON_KEYS + ("image_tokens",),
    "cache_creation": _USAGE_NESTED_COMMON_KEYS,
    "input_tokens_details": ("cached_tokens", "cache_write_tokens", "image_tokens"),
    "output_tokens_details": ("reasoning_tokens",),
}
_USAGE_NESTED = tuple(_USAGE_NESTED_KEYS)


def sanitize_usage(raw):
    """Return an allowlisted copy of a gateway usage mapping, or None.

    Preserves only recognized integer counters and recognized nested prompt
    detail objects. Unknown keys are dropped entirely.
    """
    if not isinstance(raw, dict):
        return None
    cleaned = {}
    for key in _USAGE_ALLOWLIST:
        if key in raw and isinstance(raw[key], (int, type(None))) and not isinstance(raw[key], bool):
            cleaned[key] = raw[key]
    for nested in _USAGE_NESTED:
        value = raw.get(nested)
        if isinstance(value, dict):
            inner = {
                k: v
                for k, v in value.items()
                if k in _USAGE_NESTED_KEYS[nested]
                and isinstance(v, (int, type(None)))
                and not isinstance(v, bool)
            }
            if inner:
                cleaned[nested] = inner
    return cleaned or None


def usage_record(iter_no, phase, model, resp, ts=None):
    """Build one usage sidecar record for a gateway response."""
    raw = resp.get("usage") if isinstance(resp, dict) else None
    normalized = normalize_usage(raw)
    if ts is None:
        ts = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {"iter": iter_no, "phase": phase, "model": model, "ts": ts,
            "sent": normalized["sent"], "recv": normalized["recv"],
            "cache_read": normalized["cache_read"],
            "cache_write": normalized["cache_write"],
            "raw": sanitize_usage(raw)}


def append_usage_record(path, *, iteration, phase, model, timestamp, raw_usage,
                        started=None):
    """Durably append one sanitized usage row. Nonblocking on OSError."""
    record = {
        "iter": iteration,
        "phase": phase,
        "model": model,
        "ts": timestamp,
    }
    if started is not None:
        record["started"] = started
    normalized = normalize_usage(raw_usage)
    record.update(
        sent=normalized["sent"],
        recv=normalized["recv"],
        cache_read=normalized["cache_read"],
        cache_write=normalized["cache_write"],
        raw=sanitize_usage(raw_usage),
    )
    try:
        with open(path, "a", encoding="utf-8", newline="\n") as fh:
            fh.write(json.dumps(record, sort_keys=True, separators=(",", ":"),
                                allow_nan=False) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        return True
    except OSError as exc:
        print(f"WARNING: usage journal append failed: {type(exc).__name__}",
              file=sys.stderr)
        return False


# The machine-wide run index. The engine is the only component that sees EVERY
# run regardless of where --transcript / -C send its files, so it keeps this
# register: one JSONL, two lines per run (start, end). A repo-level ledger that
# only scans <root>/.claude/agent-runs/ misses any run whose transcript landed
# elsewhere; this index does not. Each record is also mirrored to a repo run
# index beside the transcript, so a sandboxed launcher that cannot write this
# machine-wide file still leaves a discoverable register entry.
DEFAULT_RUN_INDEX = os.path.join(
    os.path.expanduser("~"), ".claude", "agent-runs-index.jsonl")


def run_index_path():
    """Resolve the index path, honouring CLAUDE_AGENT_RUN_INDEX for testability."""
    return os.environ.get("CLAUDE_AGENT_RUN_INDEX") or DEFAULT_RUN_INDEX


# Sandboxed launchers (Codex's Windows sandbox, for one) may write the workspace
# but not ~/.claude, so the machine-wide append fails silently and the run is
# invisible. The transcript's own folder is always writable (the transcript is
# written there), so mirror each record there too; AgentControl reads both.
REPO_RUN_INDEX_NAME = "agent-runs-index.jsonl"


def repo_run_index_path(transcript):
    """The repo run index sits beside the transcript, in its own folder."""
    return os.path.join(os.path.dirname(os.path.abspath(transcript)),
                        REPO_RUN_INDEX_NAME)


def run_id_for(transcript):
    """Run id is the transcript basename with a trailing .md stripped."""
    base = os.path.basename(transcript)
    return base[:-3] if base.endswith(".md") else base


def index_record(event, **fields):
    """Build and order one run-index record. Pure: no I/O, no gateway needed.

    A "start" record names where the run's files land, written BEFORE the first
    gateway call so a run killed mid-flight still leaves a register entry. An
    "end" record carries the final tallies.
    """
    ts = fields.pop("ts", None) or \
        datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    rec = {"schema": INDEX_SCHEMA, "run_id": fields.pop("run_id", None),
           "event": event, "ts": ts}
    if event == "start":
        for key in ("harness_version", "label", "model", "effort", "tool_mode",
                    "root", "cwd", "engine_pid", "transcript", "messages",
                    "usage_journal", "write_mode", "max_iters"):
            rec[key] = fields.get(key)
        if rec.get("engine_pid") is None:
            rec["engine_pid"] = os.getpid()
    elif event == "end":
        for key in ("tool_calls", "refused", "sent", "recv", "status"):
            rec[key] = fields.get(key)
        # Omitted unless set, so legacy end records keep their exact shape.
        if fields.get("stop_reason") in STOP_REASONS:
            rec["stop_reason"] = fields["stop_reason"]
    return rec


# Why an agent run ended incomplete; other components consume these exact codes.
STOP_REASONS = ("max_iters", "repair_limit", "no_action_limit", "mutation_outcome_unknown",
                "context_budget", "context_admission", "recovery_state", "response_incomplete")
STOP_REASON_TEXT = {
    "response_incomplete": "the provider ended the response incomplete; partial output is not a completed action",
    "context_admission": "context projection, storage or measurement failed; capacity overflow is not established",
    "context_budget": ("the context-admission budget (an estimate, not exact provider "
                       "capacity) was exhausted; work stopped before the request overflowed"),
    "max_iters": "the available turn budget was exhausted without a usable finish",
    "recovery_state": "durable recovery state is unavailable; work stopped without replay",
    "repair_limit": "replies kept failing the tool protocol after the correction allowance",
    "no_action_limit": "the model returned no tool action three times in a row",
    "mutation_outcome_unknown": ("an action timed out or lost its outcome and may have "
                                 "acted; check its effect before any rerun, never replay it"),
}
REPAIR_LIMIT = 2    # correction requests per run, inside the --max-iters ceiling


def turn_budget(max_iters):
    """(work, closeout) turns under a hard max_iters total.

    One tool-less report turn is always reserved; from 3 turns up, up to two
    tool-enabled closeout turns, leaving at least one work turn when max >= 2.
    """
    closeout = min(2, max_iters - 2) if max_iters >= 3 else 0
    return max_iters - closeout - 1, closeout


def _check_utf8_text(label, value):
    """File text must be a str that encodes as UTF-8; check before any mutation."""
    if not isinstance(value, str):
        raise ValueError(f"{label} must be a string")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        raise ValueError(f"{label} is not valid UTF-8 text (lone surrogate)") from None


def checked_tool_arguments(name, args):
    if not isinstance(args, dict):
        raise ValueError("tool arguments must be a JSON object")
    if name == "finish" and not isinstance(args.get("summary", ""), str):
        raise ValueError("finish summary must be a string")
    for key in {"write_file": ("content",),
                "replace_in_file": ("old_string", "new_string")}.get(name, ()):
        _check_utf8_text(f"{name} {key}", args.get(key))
    if name in ("read_command_output", "read_context_result"):
        if not isinstance(args.get("ref"), str) or set(args) - {"ref", "offset", "limit"}:
            raise ValueError("read_command_output takes ref (string), optional offset, limit")
        for k, lo, hi in (("offset", 0, COMMAND_PAGE_MAX_OFFSET),
                          ("limit", 1, COMMAND_PAGE_BYTES)):
            v = args.get(k, lo if k == "offset" else hi)
            if type(v) is not int or not lo <= v <= hi:
                raise ValueError(f"read_command_output {k} must be an integer {lo}..{hi}")
    return args


def normalize_end_status(raw):
    """Map internal end states onto the schema-2 terminal vocabulary.

    A blank/error report is a failed run, an explicit interrupt is interrupted,
    and any successful completion is complete.
    """
    if raw in ("complete", "failed", "interrupted"):
        return raw
    if raw == "ok":
        return "complete"
    if raw == "interrupted":
        return "interrupted"
    return "failed"


def append_run_event(path, record):
    """Append one JSON line durably to the run index.

    Flushes and fsyncs so a crash right after a run cannot lose its register
    entry. Swallows OSError with a clear warning: accounting must never kill a
    run, and it must never depend on a live AgentControl HTTP service.
    """
    try:
        with open(path, "a", encoding="utf-8", newline="\n") as fh:
            fh.write(json.dumps(record, sort_keys=True, separators=(",", ":"),
                                allow_nan=False) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        return True
    except OSError as exc:
        print(f"WARNING: run index append failed: {type(exc).__name__}",
              file=sys.stderr)
        return False


def write_index_record(rec, path=None):
    """Append one JSON line to the run index. Swallows OSError: accounting must
    never kill a run."""
    if path is None:
        path = run_index_path()
    append_run_event(path, rec)


def write_index_records(rec, paths):
    """Append the same record to each distinct path, in order.

    Two paths that resolve to the same file (e.g. CLAUDE_AGENT_RUN_INDEX aimed
    at the repo index) are written once, not twice. Returns one boolean per
    distinct path. Each append swallows OSError: accounting never kills a run.
    """
    results = []
    seen = set()
    for path in paths:
        key = os.path.normcase(os.path.realpath(path))
        if key in seen:
            continue
        seen.add(key)
        results.append(append_run_event(path, rec))
    return results


_UNSET = object()


class RunLifecycle:
    """Write at most one terminal record on normal or catchable interpreter exit.

    Hard OS/tree kills bypass Python handlers. Diagnostic observers retain only
    evidence flushed before such a kill; they cannot invent its initiator.
    """

    def __init__(self, run_id, index_path, model, repo_index_path=None):
        self.run_id = run_id
        self.index_path = index_path
        self.index_paths = [index_path]
        if repo_index_path is not None:
            self.index_paths.append(repo_index_path)
        self.model = model
        self._ended = False
        self._tool_calls = None
        self._refused = None
        self._sent = None
        self._recv = None
        self._registered = False
        self._finishing = False
        self._terminal = None
        self._pending = []
        self._terminal_offsets = {}
        self._index_total = 0
        self.diagnostics = DIAGNOSTICS

    def _diagnostic(self, event, **fields):
        # Fail open: a broken diagnostic helper must never mask the run outcome.
        if self.diagnostics is not None:
            try:
                self.diagnostics.emit(event, **fields)
            except Exception:
                pass

    def begin(self, **start_fields):
        results = write_index_records(
            index_record("start", run_id=self.run_id, model=self.model, **start_fields),
            self.index_paths,
        )
        if results and not results[0] and any(results[1:]):
            print(
                "note: run registered only in the repo run index "
                f"{self.index_paths[1]}; the machine-wide index was not writable",
                file=sys.stderr,
            )
        atexit.register(self._safety_end)
        for sig in (getattr(signal, "SIGINT", None), getattr(signal, "SIGTERM", None),
                    getattr(signal, "SIGBREAK", None)):
            if sig is not None:
                try:
                    signal.signal(sig, self._on_signal)
                except (ValueError, OSError):
                    pass  # not on main thread / unsupported: atexit still covers it
        self._registered = True
        self._diagnostic("registered", phase="startup")
        return any(results)

    def update(self, *, tool_calls=None, refused=None, sent=_UNSET, recv=_UNSET):
        """Record the latest progress so a safety end has accurate counters.

        sent/recv None is an explicit unknown total and replaces an earlier number.
        """
        if tool_calls is not None:
            self._tool_calls = tool_calls
        if refused is not None:
            self._refused = refused
        if sent is not _UNSET:
            self._sent = sent
        if recv is not _UNSET:
            self._recv = recv

    def finish(self, status, *, tool_calls=None, refused=None, sent=_UNSET, recv=_UNSET,
               stop_reason=None):
        """Write the authoritative terminal end record exactly once."""
        # _ended only after every distinct index holds the record; a later
        # finish retries just the missing paths with the ORIGINAL payload.
        if self._ended or self._finishing:
            return
        self._finishing = True
        try:
            if self._terminal is None:
                status = normalize_end_status(status)
                self._diagnostic("terminal", status=status, phase="finish")
                self.update(tool_calls=tool_calls, refused=refused, sent=sent, recv=recv)
                self._terminal = index_record(
                    "end", run_id=self.run_id, status=status,
                    tool_calls=self._tool_calls, refused=self._refused,
                    sent=self._sent, recv=self._recv, stop_reason=stop_reason,
                )
                seen = set()
                for path in self.index_paths:
                    key = os.path.normcase(os.path.realpath(path))
                    if key not in seen:
                        seen.add(key)
                        self._pending.append(path)
                self._index_total = len(self._pending)
            self._diagnostic("terminal_requested", status=self._terminal["status"], phase="finish",
                             indexes_expected=self._index_total)
            missing = []
            written = self._index_total - len(self._pending)
            for path in self._pending:
                try:
                    key = os.path.normcase(os.path.realpath(path))
                    if key in self._terminal_offsets:
                        present = self._terminal_present(path, self._terminal_offsets[key])
                        ok = present is True or (present is False and append_run_event(path, self._terminal))
                    else:
                        try:
                            offset = os.path.getsize(path)
                        except FileNotFoundError:
                            offset = 0
                        except OSError:
                            offset = None
                        self._terminal_offsets[key] = offset
                        ok = append_run_event(path, self._terminal)
                except Exception:
                    ok = False
                if not ok:
                    missing.append(path)
                else:
                    written += 1
                self._diagnostic("index_append_result", phase="finish",
                                 outcome="ok" if ok else "failed",
                                 indexes_expected=self._index_total,
                                 indexes_attempted=len(self._terminal_offsets), indexes_written=written)
            self._pending = missing
            if not missing:
                self._ended = True
                self._diagnostic("terminal_persisted", phase="finish",
                                 indexes_expected=self._index_total,
                                 indexes_attempted=self._index_total,
                                 indexes_written=self._index_total)
        finally:
            self._finishing = False

    def _terminal_present(self, path, offset):
        """Confirm an ambiguous append before retry; an unread gap stays unknown."""
        if offset is None:
            return None
        try:
            with open(path, "rb") as stream:
                size = stream.seek(0, os.SEEK_END)
                if size < offset:
                    return None
                stream.seek(offset)
                data = stream.read(64 * 1024)
            for line in data.splitlines(keepends=True):
                if not line.endswith(b"\n"):
                    continue
                try:
                    matches = json.loads(line) == self._terminal
                except (ValueError, UnicodeError):
                    continue
                if matches:
                    with open(path, "a", encoding="utf-8") as stream:
                        stream.flush()
                        os.fsync(stream.fileno())
                    return True
            return False if offset + len(data) == size and (not data or data.endswith(b"\n")) else None
        except FileNotFoundError:
            return False if offset == 0 else None
        except (OSError, ValueError):
            return None

    def _safety_end(self):
        # Runs at interpreter exit if finish() was never reached: the run died
        # without reporting, so it is interrupted, not complete.
        if self._ended or not self._registered:
            return
        self._diagnostic("process_exit", reason="exit_without_finish")
        self.finish("interrupted")

    def _on_signal(self, signum, frame):
        self._diagnostic("signal", signal=int(signum), reason="signal")
        self._safety_end()
        # Re-raising the default signal bypasses finally/atexit on POSIX.
        # Close only our owned MCP processes before handing termination back.
        close_open_runners()
        # Restore default handling and re-raise so the process exits normally.
        try:
            signal.signal(signum, signal.SIG_DFL)
        except (ValueError, OSError):
            pass
        os.kill(os.getpid(), signum)


CONF = os.path.join(os.path.expanduser("~"), ".claude", ".openwebui.env")
DEFAULT_MODEL = "anthropic_tool_call.claude-opus-5-5-think"
MAX_RESULT_CHARS = 100_000
CMD_TIMEOUT = 600
COMMAND_PREVIEW_BYTES = 2048   # per-stream inline preview when output is paged
COMMAND_PAGE_BYTES = 4096      # read_command_output limit ceiling (bytes per page)
COMMAND_PAGE_MAX_OFFSET = 256 * 1024 * 1024  # == MCP_MAX_ARTIFACT_BYTES shared quota

# Per-request read timeout, seconds. Tradeoff: reasoning models at xhigh effort
# legitimately take minutes to reply, so this must stay generous or we would
# abort perfectly healthy calls. But an unbounded wait is worse -- a single
# wedged upstream connection would hang the whole run forever with no recovery,
# turning one slow call into a dead run. A bounded-but-generous timeout lets the
# retry path (see call_model) recover instead.
REQUEST_TIMEOUT = 600

# Streaming one-shots (live-thinking design): a stream with no line for this long
# counts as a timeout, and live transcript text is fsynced at most this often.
STREAM_IDLE_TIMEOUT = 300
LIVE_FLUSH_SECONDS = 1.0

# A bare "Error:" reply that took this long is the gateway's cut of a Claude
# reply at its ~121 s limit (measured 2026-10-02: 401 logged cuts, all >= 116 s;
# no successful Claude reply over 122 s). A quick bare "Error:" is some other
# gateway fault and keeps the ordinary retry.
REPLY_CUT_SECONDS = 100
CUT_CORRECTIONS_PER_TURN = 2
CUT_PLACEHOLDER = "[reply discarded by the gateway's 2-minute limit]"
CUT_CORRECTION = (
    "Your last reply took longer than the gateway's 2-minute limit, so it was "
    "discarded and nothing ran. Do the same work in a smaller step: call one "
    "tool, and keep any write_file or replace_in_file under about 120 lines. "
    "Write a big file in parts.")
BARE_ERROR = re.compile(r"\s*Error:?\s*")


def usable_answer(text):
    """A streamed reply worth returning: not empty and not the gateway's bare "Error:"."""
    return bool(text.strip()) and not BARE_ERROR.fullmatch(text)


def _now():
    """Monotonic clock for attempt timing; tests replace it."""
    return time.monotonic()


# Argv prefix rules. Each element is an fnmatch pattern, so a rule can name a
# path shape. Matched element-wise over the first len(rule) argv entries, never
# through a shell.
#
# These are patterns, not literals, because the literal form was silently broken:
# ["node", "scripts"] required argv[1] to be exactly "scripts", so every real
# invocation (node scripts/check-css-motion.mjs) was refused by a rule that
# looked like it permitted them.
DEFAULT_ALLOWED_CMDS = [
    ["npm", "test"], ["npm", "run", "build"], ["npm", "run", "build:*"],
    ["npx", "vitest", "run"], ["npx", "vitest", "run", "*"], ["npx", "tsc"],
    ["git", "status"], ["git", "diff"], ["git", "log"],
    ["node", "scripts/*"],
    ["python", "-m", "py_compile", "*"],
    ["python", "tools/agent-harness/*"],   # symmetric with node scripts/*: run a self-test
]

# Directories grep and list_dir never descend into: build output, vendored
# dependencies, and other bulk that would drown a search in noise. NOT a
# security control -- read_file still reaches inside, deliberately, so an agent
# can open one known file in there without a repo-wide walk being ruined by it.
DEFAULT_SKIP_DIRS = {".git", "node_modules", "dist", "packages", "webos-app",
                     ".kilo", ".gstack"}

DEFAULT_MAX_READ_CHARS = 30_000     # ~7.5k tokens; above this, force grep or a line range

# Candidate snapshot around a designated --verify-command in disk mode ONLY.
# Default commands never scan: an unbounded walk once read 134-208 GB per run.
# Over any budget the snapshot is unavailable (never "unchanged"). The deadline
# is cooperative: one blocked OS read/stat call cannot be preempted.
SNAPSHOT_MAX_BYTES = 16 * 1024 * 1024
SNAPSHOT_MAX_ENTRIES = 2048           # file/dir visits across scan AND validation
SNAPSHOT_MAX_SECONDS = 1.0
SNAPSHOT_CHUNK = 64 * 1024
# VCS metadata and the engine's own run state (transcripts, .steer) only. Policy
# SKIP_DIRS are NOT excluded: they can hold candidate files.
SNAPSHOT_SKIP = frozenset({".git", ".claude"})
_REPARSE_POINT = 0x400                # FILE_ATTRIBUTE_REPARSE_POINT (junctions)


def _candidate_snapshot(root):
    """Digest of every file/dir under root within budget, else None."""
    deadline = time.monotonic() + SNAPSHOT_MAX_SECONDS

    def identity(st):
        if (getattr(st, "st_file_attributes", 0) & _REPARSE_POINT
                or not (stat.S_ISREG(st.st_mode) or stat.S_ISDIR(st.st_mode))):
            return None
        return (st.st_dev, st.st_ino, st.st_mode, st.st_size,
                st.st_mtime_ns, st.st_ctime_ns)

    records, observed, entries, budget = [], [], 0, SNAPSHOT_MAX_BYTES
    members = {}                        # rel_dir -> exact child names seen by the scan
    try:
        root_st = os.lstat(root)
        root_id = identity(root_st)
        if root_id is None or not stat.S_ISDIR(root_st.st_mode):
            return None
        stack = [("", root_id)]
        observed.append((root, root_id))
        while stack:
            rel_dir, dir_id = stack.pop()
            directory = os.path.join(root, rel_dir) if rel_dir else root
            if time.monotonic() > deadline or identity(os.lstat(directory)) != dir_id:
                return None
            names = members[rel_dir] = set()
            with os.scandir(directory) as it:
                for entry in it:
                    entries += 1
                    if entries > SNAPSHOT_MAX_ENTRIES or time.monotonic() > deadline:
                        return None
                    if not rel_dir and entry.name in SNAPSHOT_SKIP:
                        continue
                    names.add(entry.name)
                    rel = f"{rel_dir}/{entry.name}" if rel_dir else entry.name
                    entry_id = identity(entry.stat(follow_symlinks=False))
                    st = os.lstat(entry.path)
                    file_id = identity(st)
                    # DirEntry metadata is cached; use fresh path/handle identities.
                    if (entry_id is None or file_id is None
                            or stat.S_IFMT(entry_id[2]) != stat.S_IFMT(file_id[2])):
                        return None             # links/junctions or changed paths
                    observed.append((entry.path, file_id))
                    if stat.S_ISDIR(st.st_mode):
                        records.append(f"d\0{rel}")
                        stack.append((rel, file_id))
                        continue
                    if st.st_size > budget:
                        return None             # over the byte budget
                    budget -= st.st_size
                    h, got = hashlib.sha256(), 0
                    # These checks detect races, not a portable link/open sandbox.
                    with open(entry.path, "rb") as fh:
                        handle_id = identity(os.fstat(fh.fileno()))
                        # Windows path/handle ctime can use different semantics;
                        # compare each ctime only against the same source.
                        if (handle_id is None or handle_id[:-1] != file_id[:-1]
                                or identity(os.lstat(entry.path)) != file_id):
                            return None
                        while True:
                            if time.monotonic() > deadline:
                                return None
                            chunk = fh.read(min(SNAPSHOT_CHUNK, st.st_size + 1 - got))
                            if not chunk:
                                break
                            got += len(chunk)
                            if got > st.st_size:
                                return None     # grew while reading
                            h.update(chunk)
                        if identity(os.fstat(fh.fileno())) != handle_id:
                            return None
                    if got != st.st_size or identity(os.lstat(entry.path)) != file_id:
                        return None             # shrank, edited, or replaced
                    records.append(f"f\0{rel}\0{got}\0{h.hexdigest()}")
            if identity(os.lstat(directory)) != dir_id:
                return None
        # Windows directory metadata can lag child additions; re-enumerate within
        # the same entry/time budgets instead of trusting metadata alone.
        for rel_dir, names in members.items():
            directory = os.path.join(root, rel_dir) if rel_dir else root
            with os.scandir(directory) as it:
                for entry in it:
                    entries += 1
                    if entries > SNAPSHOT_MAX_ENTRIES or time.monotonic() > deadline:
                        return None
                    if not rel_dir and entry.name in SNAPSHOT_SKIP:
                        continue
                    if entry.name not in names:
                        return None     # added (or listed twice) since the scan
                    names.discard(entry.name)
            if names or time.monotonic() > deadline:
                return None             # removed since the scan, or out of time
        # Recheck already scanned objects; a sibling scan may have changed them.
        for path, expected in reversed(observed):
            if time.monotonic() > deadline or identity(os.lstat(path)) != expected:
                return None
    except OSError:
        return None                             # unreadable, vanished, or racing
    records.sort()                              # bounded by SNAPSHOT_MAX_ENTRIES
    digest = hashlib.sha256("\n".join(records).encode("utf-8", "surrogatepass")).hexdigest()
    return digest if time.monotonic() <= deadline else None

# The live policy. load_policy() replaces these from the repo's
# .claude/agent-policy.json; everything downstream reads these names, so a
# per-repo boundary needs no other change.
ALLOWED_CMDS = [list(rule) for rule in DEFAULT_ALLOWED_CMDS]
SKIP_DIRS = set(DEFAULT_SKIP_DIRS)
MAX_READ_CHARS = DEFAULT_MAX_READ_CHARS


CAPABILITY_DOCS = [
    ("list_mcp_tools", {"server": "optional MCP server name"},
     "List configured MCP servers (no server: cheap manifest, nothing started), or "
     "start one server and discover its tools. Discovered tools (named "
     "mcp__<server>__<tool>) become callable on your NEXT reply."),
    ("load_skill", {"name": "skill name from the catalog"},
     "Load a skill's full instructions. Its resources are then readable with "
     "read_file('skill://<name>/<file>'). Skills never widen your permissions."),
]
CAPABILITY_SCHEMAS = {
    "list_mcp_tools": {"type": "object", "properties": {"server": {"type": "string"}},
                       "required": []},
    "load_skill": {"type": "object", "properties": {"name": {"type": "string"}},
                   "required": ["name"]},
}


def mcp_tool_docs(runner):
    """(name, schema, description) for discovered MCP tools, runtime schemas intact.

    Also records exactly what was advertised: only those names are callable.
    """
    if runner is None:
        return []
    out = []
    for t in runner.caps.mcp_native():
        fn = t["function"]
        out.append((fn["name"], fn.get("parameters") or {"type": "object"},
                    str(fn.get("description") or "")))
    runner.caps.advertised = {n for n, _, _ in out}
    return out


def tool_docs(runner=None):
    docs = _builtin_tool_docs()
    if runner is not None and runner.profile == "resilient-v1":
        docs = [(name, dict(args, offset=0, limit=100) if name in ("list_dir", "grep")
                 else args, desc + " Optional offset/limit select a bounded page; follow offset=N."
                 if name in ("list_dir", "grep") else desc)
                for name, args, desc in docs]
    if runner is not None and (runner.profile == "resilient-v1"
                               or runner.context_runtime is not None):
        docs.insert(5, ("read_command_output", {"ref": "cmd://...", "offset": 0, "limit": 4096},
                        "Page stored output of an earlier run_command in this run by its "
                        "cmd:// ref. offset counts bytes; follow next_offset until eof. "
                        "Never reruns the command."))
    if runner is not None and runner.context_runtime is not None:
        docs.insert(6, ("read_context_result", {"ref": "receipt reference", "offset": 0, "limit": 4096},
                        "Read this run's recorded result; never executes an action. "
                        "Body availability is reported explicitly; do not rerun an action to recover output."))
    if runner is not None and runner.read_files_enabled:
        docs.insert(1, ("read_files", {"requests": [{"path": "repo-relative path"}]},
                        "Read 1–8 independent UTF-8 files or line ranges in order. "
                        "Returns bounded JSON statuses and exact continuation ranges; "
                        "unserved entries were not executed."))
    return docs[:-1] + CAPABILITY_DOCS + docs[-1:]


def _builtin_tool_docs():
    """Built on demand, not at import, so it reflects the ACTIVE policy.

    The run_command description enumerates the allowlist and read_file quotes the
    read limit. Freezing those at import time would advertise the built-in rules
    to a model actually governed by a repo policy -- so it would confidently run
    refused commands and blame the harness.
    """
    return [
    ("read_file", {"path": "repo-relative path", "start_line": 1, "end_line": 200},
     "Read a UTF-8 text file, or just a line range. start_line/end_line are optional. "
     f"Files larger than {MAX_READ_CHARS:,} chars CANNOT be read whole -- grep to find "
     "the lines you need, then read that range. Re-reading a file you already read "
     "returns a pointer."),
    ("view_image", {"path": "repo-relative path"},
     "Look at a PNG, JPEG or WebP image in the repo. The pixels arrive in the "
     "next message after the tool results. Do not call finish in the same turn."),
    ("list_dir", {"path": "repo-relative path"},
     "List files and directories."),
    ("grep", {"pattern": "regex", "glob": "optional, e.g. src/**/*.ts"},
     "Search file contents. Prefer this over reading large files whole."),
    ("run_command", {"argv": ALLOWED_CMDS[0] if ALLOWED_CMDS else ["git", "status"]},
     "Run an allowlisted command. No shell: pipes and globs will not work. "
     "Allowed: " + "; ".join(" ".join(a) for a in ALLOWED_CMDS)),
    ("write_file", {"path": "...", "content": "full new contents"},
     "Write a whole file. Requires the COMPLETE new contents, so use it only for "
     "small or brand-new files; for an existing file prefer replace_in_file."),
    ("replace_in_file", {"path": "...", "old_string": "exact text to find",
                         "new_string": "replacement text"},
     "Edit part of a file. old_string must match byte-for-byte and appear EXACTLY "
     "once; include surrounding lines to make it unique. This is the correct way to "
     "edit a large file you cannot read whole."),
    ("finish", {"summary": "your final report"},
     "Call when done."),
    ]


POLICY_FILE = os.path.join(".claude", "agent-policy.json")
POLICY_KEYS = {"allowed_cmds", "skip_dirs", "max_read_chars", "comment",
               "mcp", "skills_dirs"}

# Capability policy (MCP servers, extra skill directories). Reset by every
# load_policy() call so one repo's servers can never leak into another run.
MCP_POLICY = None
SKILLS_DIRS = []

# Runtime modules live beside this file and are loaded by absolute path
# (no sys.path mutation). MCP/skills remain on demand; tests may inject
# stand-ins into _CAP_MODULES.
ENGINE_DIR = os.path.dirname(os.path.abspath(__file__))
_CAP_MODULES = {}


class CapabilityUnavailable(RuntimeError):
    """A requested runtime module is not installed."""


def capability_module(name):
    if name in _CAP_MODULES:
        return _CAP_MODULES[name]
    if name not in (
        "harness_mcp", "harness_skills", "harness_diagnostics",
        "finish_gate", "run_review", "trajectory", "context_guard",
        "supervisor_policy", "tool_output", "paged_tools", "native_batch",
        "recovery_runtime", "supervisor_manifest_v2", "supervisor_launch_v2",
        "command_output", "context_policy", "context_tokens", "context_engine",
        "context_runtime", "context_receipts", "context_storage", "context_compaction",
    ):
        raise CapabilityUnavailable(f"unknown capability module {name}")
    path = os.path.join(ENGINE_DIR, name + ".py")
    if not os.path.isfile(path):
        raise CapabilityUnavailable(f"{name}.py is not installed beside {ENGINE_DIR}")
    import importlib.util
    spec = importlib.util.spec_from_file_location(f"_agentcontrol_{name}", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    try:
        spec.loader.exec_module(mod)
    except BaseException:
        sys.modules.pop(spec.name, None)
        raise
    _CAP_MODULES[name] = mod
    return mod


def _optional_module(name):
    try:
        return capability_module(name)
    except CapabilityUnavailable:
        return None


def _validate_capability_policy(path, pol):
    """Validate nested capability keys with their owning modules; exit closed."""
    global MCP_POLICY, SKILLS_DIRS
    if "mcp" in pol:
        try:
            MCP_POLICY = capability_module("harness_mcp").validate_mcp_policy(pol["mcp"])
        except Exception as e:
            MCP_POLICY = None
            sys.exit(f"error: {path}: invalid mcp policy: {type(e).__name__}: {e}")
    if "skills_dirs" in pol:
        try:
            SKILLS_DIRS = list(capability_module("harness_skills")
                               .validate_skills_dirs(pol["skills_dirs"]))
        except Exception as e:
            SKILLS_DIRS = []
            sys.exit(f"error: {path}: invalid skills_dirs: {type(e).__name__}: {e}")


def load_policy(root):
    """Replace the built-in boundary with the repo's own, if it ships one.

    Returns a one-line description of what is in force, for the transcript header
    and stderr -- the effective security boundary must be visible in the log of
    every run, not implied by the absence of a message.

    Fails CLOSED on a malformed or unrecognised policy: silently falling back to
    the built-in rules would swap one repo's boundary for another's (the defaults
    here are a Node project's) while looking like it worked. A typo'd key is the
    same class of error -- the author believes they set a rule that is not in
    force -- so unknown keys are refused too rather than ignored.
    """
    global ALLOWED_CMDS, SKIP_DIRS, MAX_READ_CHARS, MCP_POLICY, SKILLS_DIRS
    # Start from the built-ins every time. This function establishes the whole
    # boundary rather than amending whatever was in force, so a second call for a
    # repo that ships no policy cannot inherit the previous repo's rules -- the
    # kind of leak that makes a security boundary depend on call order.
    ALLOWED_CMDS = [list(rule) for rule in DEFAULT_ALLOWED_CMDS]
    SKIP_DIRS = set(DEFAULT_SKIP_DIRS)
    MAX_READ_CHARS = DEFAULT_MAX_READ_CHARS
    MCP_POLICY = None
    SKILLS_DIRS = []

    path = os.path.join(root, POLICY_FILE)
    if not os.path.exists(path):
        return f"policy: built-in defaults ({len(ALLOWED_CMDS)} command rules; no {POLICY_FILE})"
    try:
        with open(path, encoding="utf-8") as fh:
            pol = json.load(fh)
    except (OSError, json.JSONDecodeError) as e:
        sys.exit(f"error: cannot read {path}: {e}")
    if not isinstance(pol, dict):
        sys.exit(f"error: {path} must contain a JSON object")
    unknown = set(pol) - POLICY_KEYS
    if unknown:
        sys.exit(f"error: {path} has unrecognised key(s): {', '.join(sorted(unknown))}. "
                 f"Valid keys: {', '.join(sorted(POLICY_KEYS))}")

    if "allowed_cmds" in pol:
        rules = pol["allowed_cmds"]
        if not isinstance(rules, list) or not rules:
            sys.exit(f"error: {path}: allowed_cmds must be a non-empty list of argv "
                     'rules, e.g. [["git", "status"], ["python", "scripts/*"]]')
        for rule in rules:
            if (not isinstance(rule, list) or not rule
                    or not all(isinstance(a, str) and a for a in rule)):
                sys.exit(f"error: {path}: each allowed_cmds rule must be a non-empty "
                         f"list of non-empty strings; got {rule!r}")
        ALLOWED_CMDS = [list(rule) for rule in rules]

    if "skip_dirs" in pol:
        dirs = pol["skip_dirs"]
        if not isinstance(dirs, list) or not all(isinstance(d, str) and d for d in dirs):
            sys.exit(f"error: {path}: skip_dirs must be a list of non-empty strings")
        SKIP_DIRS = set(dirs)

    if "max_read_chars" in pol:
        n = pol["max_read_chars"]
        if not isinstance(n, int) or isinstance(n, bool) or n < 1_000:
            sys.exit(f"error: {path}: max_read_chars must be an integer >= 1000")
        MAX_READ_CHARS = n

    _validate_capability_policy(path, pol)
    caps = ""
    if MCP_POLICY:
        caps += f", {len(MCP_POLICY.get('servers') or {})} MCP server(s)"
    if SKILLS_DIRS:
        caps += f", {len(SKILLS_DIRS)} skills dir(s)"
    return (f"policy: {path} ({len(ALLOWED_CMDS)} command rules, "
            f"{len(SKIP_DIRS)} skipped dirs, read limit {MAX_READ_CHARS:,} chars{caps})")


def load_cfg():
    cfg = {}
    if os.path.exists(CONF):
        for line in open(CONF, encoding="utf-8"):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                cfg[k.strip()] = v.strip().strip('"').strip("'")
    url = cfg.get("OPENWEBUI_URL") or os.environ.get("OPENWEBUI_URL")
    key = cfg.get("OPENWEBUI_API_KEY") or os.environ.get("OPENWEBUI_API_KEY")
    if not url or not key:
        sys.exit(f"error: set OPENWEBUI_URL and OPENWEBUI_API_KEY in {CONF}")
    url = url.rstrip("/")
    return (url[:-4] if url.endswith("/api") else url), key


def link_node_modules(root, wt):
    """Point the worktree's node_modules at the real one.

    `git worktree add` gives a clean checkout with no node_modules, so npx/vitest
    would fail there and the agent could not verify anything. A junction (Windows,
    no admin needed) or symlink costs nothing and is gitignored already.
    """
    src, dst = os.path.join(root, "node_modules"), os.path.join(wt, "node_modules")
    if not os.path.isdir(src) or os.path.exists(dst):
        return
    try:
        if os.name == "nt":
            subprocess.run(["cmd", "/c", "mklink", "/J", dst, src],
                           capture_output=True, timeout=60)
        else:
            os.symlink(src, dst)
    except (OSError, subprocess.SubprocessError) as e:
        print(f"warning: could not link node_modules into the worktree: {e}",
              file=sys.stderr)


def _recovery_worktree_label(label):
    label = re.sub(r"[^A-Za-z0-9_-]", "", label)
    if not label:
        raise ValueError("resilient-v1 requires a nonempty worktree label")
    return label


def make_worktree(root, label, *, destructive=True):
    """Disposable worktree on its own branch, so real writes stay off the main tree."""
    if not destructive:
        label = _recovery_worktree_label(label)
    wt = os.path.join(root, ".claude", "worktrees", label)
    branch = "agent/" + label
    if destructive:
        subprocess.run(["git", "worktree", "remove", "--force", wt],
                       cwd=root, capture_output=True)
        subprocess.run(["git", "branch", "-D", branch], cwd=root, capture_output=True)
    else:
        if os.path.lexists(wt):
            sys.exit("error: resilient-v1 refuses an existing worktree path")
        valid = subprocess.run(["git", "check-ref-format", "--branch", branch],
                               cwd=root, capture_output=True)
        exists = subprocess.run(
            ["git", "show-ref", "--verify", "--quiet", "refs/heads/" + branch],
            cwd=root, capture_output=True)
        if valid.returncode != 0 or exists.returncode != 1:
            sys.exit("error: resilient-v1 refuses an existing or invalid worktree branch")
    os.makedirs(os.path.dirname(wt), exist_ok=True)
    r = subprocess.run(["git", "worktree", "add", "-b", branch, wt, "HEAD"],
                       cwd=root, capture_output=True, text=True)
    if r.returncode != 0:
        sys.exit(f"error: could not create worktree: {r.stderr.strip()}")
    link_node_modules(root, wt)
    print(f"worktree:   {wt}  (branch {branch})", file=sys.stderr, flush=True)
    return wt, branch


def worktree_diff(wt):
    subprocess.run(["git", "add", "-A"], cwd=wt, capture_output=True)
    r = subprocess.run(["git", "diff", "--cached"], cwd=wt,
                       capture_output=True, text=True)
    return r.stdout


# ---- image input -----------------------------------------------------------
# Images travel as in-memory ImageAsset objects inside message content parts and
# become base64 data URLs only when a request is serialized for the network
# (_wire_default). Every artifact (messages journal, transcript, anomalies)
# gets metadata only. These limits are engine constants, not policy keys.
IMAGE_MAX_SOURCE_BYTES = 32 * 1024 * 1024    # file size read from disk
IMAGE_MAX_PIXELS = 40_000_000                # decoded width x height
IMAGE_MAX_SIDE = 2048                        # longest transmitted side
IMAGE_FALLBACK_SIDES = (1536, 1024)          # tried when 2048 is still too big
IMAGE_MAX_BYTES = 4 * 1024 * 1024            # one transmitted image
IMAGE_MAX_PARTS = 8                          # image parts retained per request
IMAGE_MAX_TOTAL_BYTES = 16 * 1024 * 1024     # all image parts in one request
IMAGE_JPEG_QUALITY = 85


class ImageError(ValueError):
    """An image that cannot be sent; the message is safe to show."""


class ImageAsset:
    """One prepared image: the bytes to send plus metadata. repr is metadata only."""

    __slots__ = ("path", "mime", "data", "width", "height", "sha256", "source")

    def __init__(self, path, mime, data, width, height, source=None):
        self.path, self.mime, self.data = path, mime, bytes(data)
        self.width, self.height = width, height
        self.sha256 = hashlib.sha256(self.data).hexdigest()
        self.source = source     # None, or metadata of the untransformed file

    @property
    def size(self):
        return len(self.data)

    def meta(self):
        out = {"path": self.path, "mime": self.mime, "sha256": self.sha256,
               "width": self.width, "height": self.height, "bytes": self.size}
        if self.source:
            out["source"] = dict(self.source)
        return out

    def describe(self):
        text = (f"{self.path} ({self.mime}, {self.width}x{self.height}, "
                f"{self.size:,} bytes, sha256 {self.sha256[:16]})")
        if self.source:
            text += f" [{self.source.get('note')}]"
        return text

    def data_url(self):
        return f"data:{self.mime};base64," + base64.b64encode(self.data).decode("ascii")

    def __repr__(self):
        return f"ImageAsset({self.describe()})"

    __str__ = __repr__


def _wire_default(obj):
    """json.dumps default for network serialization only."""
    if isinstance(obj, ImageAsset):
        return obj.data_url()
    raise TypeError(f"{type(obj).__name__} is not JSON serializable")


class _FallbackContextBudgetError(Exception):
    def __init__(self, reason="request", *, provider=False):
        super().__init__("context budget exhausted")
        self.reason = reason
        self.provider = provider


_cg = _optional_module("context_guard")
ContextBudgetError = _cg.ContextBudgetError if _cg is not None else _FallbackContextBudgetError


def requested_output_field(payload):
    if "input" in payload:
        return "max_output_tokens"
    return "max_completion_tokens"


def context_preflight(payload):
    check_payload_images(payload)
    cg = capability_module("context_guard")
    scope = _CAP_MODULES["context_engine"].current() if "context_engine" in _CAP_MODULES else None
    if scope is not None:  # model-v1: whole packet in estimated tokens, no legacy byte cap
        # Request (not provider-verified) an output bound equal to the reserved reply headroom;
        # set on the dict that is measured and then serialized to the wire.
        field = requested_output_field(payload)
        if any(name in payload and name != field for name in
               ("max_tokens", "max_completion_tokens", "max_output_tokens")):
            raise ContextBudgetError("invalid_output_bound")
        bound, reply = payload.get(field), scope.policy.reply_tokens
        if bound is None:
            payload[field] = reply
        elif type(bound) is not int or not 0 < bound <= reply:
            raise ContextBudgetError("invalid_output_bound")
        if not cg.fits(payload, scope.policy.input_limit_tokens, context=scope):
            raise ContextBudgetError()
    elif not cg.fits(payload, cg.REQUEST_LIMIT, image_type=ImageAsset):
        raise ContextBudgetError()


def check_context_rejection(detail):
    ce = _CAP_MODULES.get("context_engine")
    if ce is not None and ce.current() is not None and re.search(r"HTTP 413\b", str(detail)):
        return  # transport size is not evidence of a model-token window limit
    if re.search(r"HTTP 413\b|context[_ -]?(?:length|window|limit)|maximum context|too many tokens|prompt (?:is )?too long|input.{0,20}token.{0,20}(?:exceed|limit)",
                 str(detail), re.IGNORECASE):
        raise ContextBudgetError("provider context", provider=True)


def _payload_bytes(payload):
    return len(json.dumps(payload, default=_wire_default).encode())


def _png_info(data, display):
    if len(data) < 33 or data[12:16] != b"IHDR":
        raise ImageError(f"{display}: truncated or invalid PNG")
    w, h = struct.unpack(">II", data[16:24])
    pos, seen_end = 8, False
    while pos + 12 <= len(data):
        (length,) = struct.unpack(">I", data[pos:pos + 4])
        kind = data[pos + 4:pos + 8]
        if pos + 12 + length > len(data):
            break
        if kind == b"acTL":
            raise ImageError(f"{display}: animated PNG is not supported; send a static image")
        pos += 12 + length
        if kind == b"IEND":
            seen_end = True
            break
    if not seen_end:
        raise ImageError(f"{display}: truncated or corrupted PNG (no IEND chunk)")
    return w, h


def _jpeg_info(data, display):
    if not data.rstrip(b"\x00").endswith(b"\xff\xd9"):
        raise ImageError(f"{display}: truncated or corrupted JPEG (no end marker)")
    pos = 2
    while pos + 4 <= len(data):
        if data[pos] != 0xFF:
            break
        marker = data[pos + 1]
        if marker == 0xFF:
            pos += 1
            continue
        if marker in (0x01, 0xD8) or 0xD0 <= marker <= 0xD7:
            pos += 2
            continue
        (length,) = struct.unpack(">H", data[pos + 2:pos + 4])
        if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
            if pos + 9 > len(data):
                break
            h, w = struct.unpack(">HH", data[pos + 5:pos + 9])
            if w and h:
                return w, h
            break
        pos += 2 + length
    raise ImageError(f"{display}: corrupted JPEG (no frame header with dimensions)")


def _webp_info(data, display):
    if len(data) < 30:
        raise ImageError(f"{display}: truncated or invalid WebP")
    (riff_size,) = struct.unpack("<I", data[4:8])
    if riff_size + 8 > len(data):
        raise ImageError(f"{display}: truncated WebP")
    kind = data[12:16]
    if kind == b"VP8X":
        if data[20] & 0x02:
            raise ImageError(f"{display}: animated WebP is not supported; send a static image")
        w = int.from_bytes(data[24:27], "little") + 1
        h = int.from_bytes(data[27:30], "little") + 1
        return w, h
    if kind == b"VP8 " and data[23:26] == b"\x9d\x01\x2a":
        w, h = struct.unpack("<HH", data[26:30])
        return w & 0x3FFF, h & 0x3FFF
    if kind == b"VP8L" and data[20] == 0x2F:
        (bits,) = struct.unpack("<I", data[21:25])
        return (bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1
    raise ImageError(f"{display}: corrupted or unsupported WebP")


def _sniff_image(data, display):
    """(mime, pillow format, width, height) from signature and header only."""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return ("image/png", "PNG") + _png_info(data, display)
    if data.startswith(b"\xff\xd8\xff"):
        return ("image/jpeg", "JPEG") + _jpeg_info(data, display)
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ("image/webp", "WEBP") + _webp_info(data, display)
    raise ImageError(f"{display}: not a PNG, JPEG or WebP image")


def _load_pillow():
    """(Image, ImageOps) when Pillow is installed, else None. Optional."""
    try:
        from PIL import Image, ImageOps
    except ImportError:
        return None
    return Image, ImageOps


def _fit(w, h, side):
    scale = min(1.0, side / max(w, h))
    return max(1, round(w * scale)), max(1, round(h * scale))


def _pillow_prepare(pil, data, fmt, display):
    """Decode under the pixel limit; returns (bytes, mime, w, h, note) or None
    when the original bytes can be sent unchanged."""
    Image, ImageOps = pil
    old = Image.MAX_IMAGE_PIXELS
    try:
        Image.MAX_IMAGE_PIXELS = IMAGE_MAX_PIXELS
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            im = Image.open(io.BytesIO(data), formats=[fmt])
            if getattr(im, "is_animated", False) or getattr(im, "n_frames", 1) > 1:
                raise ImageError(f"{display}: animated or multi-frame images are not "
                                 "supported; send a static image")
            im.load()
            orientation = im.getexif().get(0x0112, 1)
    except ImageError:
        raise
    except (Image.DecompressionBombError, Image.DecompressionBombWarning):
        raise ImageError(f"{display}: over the {IMAGE_MAX_PIXELS:,}-pixel limit "
                         "(possible decompression bomb)")
    except Exception as exc:
        raise ImageError(f"{display}: corrupted or truncated image "
                         f"({type(exc).__name__})")
    finally:
        Image.MAX_IMAGE_PIXELS = old
    w, h = im.size
    if orientation in (1, None) and max(w, h) <= IMAGE_MAX_SIDE \
            and len(data) <= IMAGE_MAX_BYTES:
        return None
    im = ImageOps.exif_transpose(im)
    alpha = im.mode in ("RGBA", "LA", "PA") or (im.mode == "P" and "transparency" in im.info)
    for side in (IMAGE_MAX_SIDE,) + IMAGE_FALLBACK_SIDES:
        tw, th = _fit(*im.size, side)
        out = im if (tw, th) == im.size else im.resize((tw, th), Image.LANCZOS)
        buf = io.BytesIO()
        if alpha:
            out.convert("RGBA").save(buf, "PNG", optimize=True)
            mime, label = "image/png", "PNG"
        else:
            out.convert("RGB").save(buf, "JPEG", quality=IMAGE_JPEG_QUALITY)
            mime, label = "image/jpeg", f"JPEG q{IMAGE_JPEG_QUALITY}"
        if buf.tell() <= IMAGE_MAX_BYTES:
            note = (f"transformed {w}x{h} -> {tw}x{th}"
                    f"{', EXIF-transposed' if orientation not in (1, None) else ''}"
                    f", re-encoded as {label}")
            return buf.getvalue(), mime, tw, th, note
    raise ImageError(f"{display}: still over {IMAGE_MAX_BYTES:,} bytes after resizing "
                     f"to {IMAGE_FALLBACK_SIDES[-1]} px; send a smaller image")


def prepare_image(path, display_path):
    """Validate one image file and return the ImageAsset to send.

    Static PNG/JPEG/WebP only, identified by signature. Raises ImageError with
    a message naming display_path; nothing is sent or logged on failure.
    """
    try:
        st = os.stat(path)
    except OSError:
        raise ImageError(f"cannot read image {display_path}")
    if not os.path.isfile(path):
        raise ImageError(f"{display_path}: not a regular file")
    if st.st_size > IMAGE_MAX_SOURCE_BYTES:
        raise ImageError(f"{display_path}: {st.st_size:,} bytes is over the "
                         f"{IMAGE_MAX_SOURCE_BYTES:,}-byte source limit")
    try:
        with open(path, "rb") as fh:
            data = fh.read(IMAGE_MAX_SOURCE_BYTES + 1)
    except OSError:
        raise ImageError(f"cannot read image {display_path}")
    if len(data) > IMAGE_MAX_SOURCE_BYTES:
        raise ImageError(f"{display_path}: over the {IMAGE_MAX_SOURCE_BYTES:,}-byte source limit")
    mime, fmt, w, h = _sniff_image(data, display_path)
    if w <= 0 or h <= 0:
        raise ImageError(f"{display_path}: invalid dimensions {w}x{h}")
    if w * h > IMAGE_MAX_PIXELS:
        raise ImageError(f"{display_path}: {w}x{h} is over the {IMAGE_MAX_PIXELS:,}-pixel "
                         "limit (possible decompression bomb)")
    pil = _load_pillow()
    if pil is None:
        if max(w, h) > IMAGE_MAX_SIDE or len(data) > IMAGE_MAX_BYTES:
            raise ImageError(
                f"{display_path}: {w}x{h}, {len(data):,} bytes needs resizing to fit "
                f"{IMAGE_MAX_SIDE} px / {IMAGE_MAX_BYTES:,} bytes, which requires Pillow "
                "(pip install Pillow); or send a smaller image")
        return ImageAsset(display_path, mime, data, w, h)
    done = _pillow_prepare(pil, data, fmt, display_path)
    if done is None:
        return ImageAsset(display_path, mime, data, w, h)
    out, out_mime, tw, th, note = done
    source = {"sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data),
              "width": w, "height": h, "mime": mime, "note": note}
    return ImageAsset(display_path, out_mime, out, tw, th, source=source)


def image_part(asset):
    """A chat-completions image content part; the url is made at serialization."""
    return {"type": "image_url", "image_url": {"url": asset}}


def user_content(text, images):
    """The original string when there are no images, else multipart content."""
    if not images:
        return text
    return [{"type": "text", "text": text}] + [image_part(a) for a in images]


def append_content_text(content, suffix):
    """content + suffix; multipart content is copied with the suffix on its
    first text part, keeping every image part (shorter-answer retries)."""
    if isinstance(content, str):
        return content + suffix
    parts = [dict(p) for p in content]
    for p in parts:
        if p.get("type") == "text":
            p["text"] = (p.get("text") or "") + suffix
            return parts
    return [{"type": "text", "text": suffix}] + parts


def content_images(content):
    """The ImageAsset objects in one message content, in order."""
    if not isinstance(content, list):
        return []
    out = []
    for p in content:
        if isinstance(p, dict) and p.get("type") == "image_url":
            url = (p.get("image_url") or {}).get("url")
            if isinstance(url, ImageAsset):
                out.append(url)
    return out


def history_images(msgs):
    return [a for m in msgs for a in content_images(m.get("content"))]


def image_cap_error(images, adding=()):
    """None when images + adding fit the per-request caps, else the reason."""
    allimgs = list(images) + list(adding)
    total = sum(a.size for a in allimgs)
    if len(allimgs) > IMAGE_MAX_PARTS:
        return (f"{len(allimgs)} images would exceed the {IMAGE_MAX_PARTS}-image "
                "limit per request")
    if total > IMAGE_MAX_TOTAL_BYTES:
        return (f"{total:,} image bytes would exceed the {IMAGE_MAX_TOTAL_BYTES:,}-byte "
                "limit per request")
    return None


IMAGE_MAX_TEXT_CHARS = 600_000   # message text of one image-bearing request


def _parts_text_chars(content):
    """Chars of text in one content value: a string, or a list of parts whose
    text/input_text/output_text entries count. Image parts never count, and
    an ImageAsset is never serialized here."""
    if isinstance(content, str):
        return len(content)
    n = 0
    if isinstance(content, list):
        for p in content:
            if isinstance(p, dict) and isinstance(p.get("text"), str):
                n += len(p["text"])
    return n


def _responses_images(items):
    out = []
    for it in items if isinstance(items, list) else []:
        for p in (it.get("content") if isinstance(it, dict) else None) or []:
            if isinstance(p, dict) and p.get("type") == "input_image" \
                    and isinstance(p.get("image_url"), ImageAsset):
                out.append(p["image_url"])
    return out


def payload_text_chars(payload):
    """Message text of a Chat (messages) or Responses (input) payload: content,
    tool results and tool-call arguments, excluding any image encoding."""
    n = 0
    for m in payload.get("messages") or []:
        n += _parts_text_chars(m.get("content"))
        for c in m.get("tool_calls") or []:
            args = (c.get("function") or {}).get("arguments")
            n += len(args) if isinstance(args, str) else 0
    items = payload.get("input")
    if isinstance(items, str):
        return n + len(items)
    for it in items or []:
        if not isinstance(it, dict):
            continue
        n += _parts_text_chars(it.get("content"))
        for field in ("arguments", "output"):
            if isinstance(it.get(field), str):
                n += len(it[field])
    return n


def image_text_cap_error(chars):
    if chars > IMAGE_MAX_TEXT_CHARS:
        return (f"{chars:,} chars of text would exceed the {IMAGE_MAX_TEXT_CHARS:,}-char "
                "limit for a request carrying images")
    return None


def check_payload_images(payload):
    """Exit with a clear error rather than send a request over the image caps.
    Text-only payloads are not checked here (their limits are unchanged)."""
    msgs = payload.get("messages") or []
    images = history_images(msgs) + _responses_images(payload.get("input"))
    if not images:
        return
    err = image_cap_error(images) or image_text_cap_error(payload_text_chars(payload))
    if err:
        sys.exit(f"error: not sending to {payload.get('model')}: {err}")


def sanitize_content(content):
    """Message content with every image replaced by its metadata."""
    if not isinstance(content, list):
        return content
    out = []
    for p in content:
        if isinstance(p, dict) and p.get("type") == "image_url":
            url = (p.get("image_url") or {}).get("url")
            meta = url.meta() if isinstance(url, ImageAsset) else {"omitted": True}
            out.append({"type": "image", "image": meta})
        else:
            out.append(p)
    return out


def sanitize_message(m):
    if not isinstance(m, dict) or not isinstance(m.get("content"), list):
        return m
    return {**m, "content": sanitize_content(m["content"])}


FINISH_DEFERRED = ("refused: finish was called in the same turn as a successful "
                   "view_image, before you have seen the image. Nothing was finished; "
                   "look at the image in the next message, then call finish.")


def admit_image(msgs, viewed, call_id, asset):
    """Queue a view_image result for delivery, or refuse it visibly when the
    request caps (whole history included) would be exceeded. Assets already
    in the history are never evicted. Returns the text tool result."""
    err = image_cap_error(history_images(msgs) + [a for _, a in viewed], [asset])
    if err:
        return (f"refused: {asset.path} was not attached: {err} (images already in "
                "this conversation count). Describe what you need from the images "
                "you have already seen instead.")
    viewed.append((call_id, asset))
    return (f"image loaded: {asset.describe()}. The pixels follow in the next "
            "message, after all tool results.")


def viewed_images_content(viewed):
    """One user message with every image, captioned with its path and available call ID."""
    parts = []
    for call_id, asset in viewed:
        origin = f" call {call_id}" if call_id is not None else ""
        parts.append({"type": "text",
                      "text": f"Image from view_image{origin}: {asset.path}"})
        parts.append(image_part(asset))
    return parts


_DATA_URL = re.compile(r"data:image/[A-Za-z0-9.+-]*;base64,[A-Za-z0-9+/=_\-\\]*")


def scrub_data_urls(text):
    if not isinstance(text, str):
        return text
    return _DATA_URL.sub("[image data omitted]", text)


_DATA_HEAD = re.compile(r"data:image/[A-Za-z0-9.+-]*;base64,")
_DATA_PARTIAL = re.compile(r"data:image/[A-Za-z0-9.+-]{0,128}(;(b(a(s(e(6(4)?)?)?)?)?)?)?")
_DATA_BODY = re.compile(r"[A-Za-z0-9+/=_\-\\]*")
_DATA_START = "data:image/"
_DATA_MARK = "[image data omitted]"
_DATA_CARRY_MAX = len(_DATA_START) + 128 + len(";base64")


def _partial_data_head(s):
    return _DATA_START.startswith(s) or bool(_DATA_PARTIAL.fullmatch(s))


class DataUrlRedactor:
    """Streaming counterpart of scrub_data_urls for text arriving in deltas.

    Holds back at most _DATA_CARRY_MAX chars that could be the start of a
    data:image/...;base64, header and drops the encoded body until its first
    non-base64 char, so no split of a data URL across deltas reaches the
    output. Ordinary text is passed through at once.
    """

    def __init__(self):
        self.carry = ""
        self.in_body = False

    def feed(self, text):
        buf, self.carry = self.carry + (text or ""), ""
        out, i = [], 0
        while True:
            if self.in_body:
                i = _DATA_BODY.match(buf, i).end()
                if i == len(buf):
                    break
                self.in_body = False
            m = _DATA_HEAD.search(buf, i)
            if m:
                out += [buf[i:m.start()], _DATA_MARK]
                i, self.in_body = m.end(), True
                continue
            rest = buf[i:]
            k = next((k for k in range(max(0, len(rest) - _DATA_CARRY_MAX), len(rest))
                      if rest[k] == "d" and _partial_data_head(rest[k:])), len(rest))
            out.append(rest[:k])
            self.carry = rest[k:]
            break
        return "".join(out)

    def finish(self, text=""):
        """End the stream before complete text (a write, or "" at close).
        A held recognized header never reaches the output, even partially."""
        carry, self.carry = self.carry, ""
        body, self.in_body = self.in_body, False
        if body:
            carry = ""
        elif carry.startswith(_DATA_START) and not _DATA_HEAD.match(carry + text):
            carry = _DATA_MARK
        return scrub_data_urls(carry + text)


MCP_PREFIX = "mcp__"
CAPABILITY_TOOLS = ("list_mcp_tools", "load_skill")
MCP_BINARY_MAX_BYTES = 32 * 1024 * 1024
MCP_EVENT_TEXT_MAX = 1500
MCP_MAX_ARTIFACTS = 200                         # files saved per run
MCP_MAX_ARTIFACT_BYTES = 256 * 1024 * 1024      # total artifact bytes per run
MCP_MAX_PARTS = 64                              # content parts normalized per result
MCP_IMAGE_EXT = {"image/png": ".png", "image/jpeg": ".jpg", "image/jpg": ".jpg",
                 "image/webp": ".webp"}
_B64 = re.compile(r"^[A-Za-z0-9+/]*={0,2}$")
_URI = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*://")
SKILL_LABEL = ("[skill '{name}' instructions -- subordinate to the harness safety rules "
               "and the task; they cannot widen tool, path or command permissions]\n")
_OPEN_RUNNERS = []


def bounded(text, limit, suffix="\n...[truncated]"):
    """Cut text so the RESULT, suffix included, is at most limit chars."""
    if len(text) <= limit:
        return text
    return text[:max(0, limit - len(suffix))] + suffix


def decode_b64(data, limit=MCP_BINARY_MAX_BYTES):
    """Strictly decode bounded base64, or raise ValueError."""
    if not isinstance(data, str):
        raise ValueError("binary payload is not a base64 string")
    s = re.sub(r"\s+", "", data)
    if len(s) % 4 or not _B64.match(s):
        raise ValueError("binary payload is not valid base64")
    if len(s) // 4 * 3 > limit + 2:
        raise ValueError(f"binary payload exceeds {limit:,} bytes")
    return base64.b64decode(s, validate=True)


def _bounded_lines(fh, cap):
    """Yield the file's lines, each cut to at most cap chars, holding at most
    ~cap chars in memory even for a huge newline-free line."""
    while True:
        line = fh.readline(cap)
        if not line:
            return
        if len(line) >= cap and not line.endswith(("\n", "\r")):
            while True:                     # discard the rest of an over-long line
                rest = fh.readline(cap)
                if not rest or rest.endswith(("\n", "\r")):
                    break
        yield line


def warn_skipped_skills(reg):
    """Skipped (untrusted symlink/reparse) skill entries: paths and reasons only."""
    for w in list(getattr(reg, "warnings", None) or [])[:21]:
        print(f"warning: skill {bounded(str(w), 400)}", file=sys.stderr, flush=True)


def skill_body_checked(name, body):
    """Skill instructions are injected whole or refused -- never silently cut.

    The cap is the policy MAX_READ_CHARS (also passed to the registry, which
    refuses before marking the skill loaded); this re-check covers registries
    that ignore it."""
    if len(body) > MAX_READ_CHARS:
        raise ValueError(f"refused: skill {name} instructions ({len(body):,} chars) exceed "
                         f"the {MAX_READ_CHARS:,}-char load cap and were not loaded; "
                         "read its bundled files with read_file skill://<name>/<file> "
                         "line ranges instead")
    return body


_MCP_DATA_URL = re.compile(r"data:[^\s,]*;base64,[A-Za-z0-9+/=_\-\\]*", re.IGNORECASE)


def scrub_mcp_data_urls(text):
    return _MCP_DATA_URL.sub("[binary data omitted]", text)


def strip_binary(obj):
    """Deep copy with base64 fields replaced by a size note (for logs)."""
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if k in ("data", "blob") and isinstance(v, str):
                out[k] = f"[{len(v):,} chars base64 omitted]"
            else:
                out[k] = strip_binary(v)
        return out
    if isinstance(obj, list):
        return [strip_binary(v) for v in obj]
    if isinstance(obj, str):
        return scrub_mcp_data_urls(obj)
    return obj


_ART_LOCK = __import__("threading").Lock()  # guards Capabilities._art_bytes across drains


class _CommandSink:
    """Streaming writer for one command-output artifact; raw bytes only on disk."""

    def __init__(self, caps, name, full, fd):
        self.caps, self.name, self.full, self.fd = caps, name, full, fd
        self.size, self.sha, self.error = 0, hashlib.sha256(), None
        # per-block digests let every page be verified without rehashing the file;
        # bounded: 256MiB quota / 64KiB = 4096 x 32B digests at most
        self.blocks, self._blk, self._hashed = [], hashlib.sha256(), 0

    def write(self, data):
        """Bytes durably written; fewer than len(data) means quota/disk stop
        (self.error then says which). Never raises for I/O."""
        with _ART_LOCK:  # stdout/stderr drains share one per-run quota: reserve first
            used = getattr(self.caps, "_art_bytes", 0)
            take = max(0, min(len(data), MCP_MAX_ARTIFACT_BYTES - used))
            self.caps._art_bytes = used + take
        done = 0
        try:
            while done < take:
                n = os.write(self.fd, data[done:take])
                if n <= 0:
                    break
                self._hash(data[done:done + n])
                done += n
        except OSError:
            self.error = "io_error"
        finally:
            with _ART_LOCK:
                self.caps._art_bytes -= take - done   # refund only the unwritten part
            self.size += done
        if done < len(data) and self.error is None:
            self.error = "quota"
        return done

    def _hash(self, data):
        B = capability_module("command_output").BLOCK
        self.sha.update(data)
        while data:
            take = data[:B - self._hashed % B]
            self._blk.update(take)
            self._hashed += len(take)
            data = data[len(take):]
            if self._hashed % B == 0:
                self.blocks.append(self._blk.digest())
                self._blk = hashlib.sha256()

    def close(self):
        """Register and return cmd:// ref (identity + digest pinned), or None."""
        fd, self.fd = self.fd, None
        if fd is None:
            return None
        try:
            ident = os.fstat(fd)
        finally:
            os.close(fd)
        self.caps._artifact_root()
        st = os.lstat(self.full)                 # post-close mtime is the stable one
        reparse = getattr(st, "st_file_attributes", 0) & getattr(
            stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
        if ((st.st_dev, st.st_ino) != (ident.st_dev, ident.st_ino) or st.st_size != self.size
                or not stat.S_ISREG(st.st_mode) or st.st_nlink != 1 or reparse):
            raise OSError("command artifact changed or aliased while being published")
        ref = f"cmd://{self.name}"
        if not hasattr(self.caps, "command_outputs"):
            self.caps.command_outputs = {}
        if self._hashed % capability_module("command_output").BLOCK:
            self.blocks.append(self._blk.digest())   # final partial block
        self.caps.command_outputs[ref] = {
            "path": self.full, "size": self.size, "sha256": self.sha.hexdigest(),
            "blocks": tuple(self.blocks),
            "id": (st.st_dev, st.st_ino, st.st_mtime_ns),
            "complete": False, "discarded": 0}
        return ref


class Capabilities:
    """Per-run MCP + skills registry. Owns capability dispatch and cleanup."""

    def __init__(self, root, *, mcp_policy=None, skills_dirs=(), allow_mcp_mutation=False,
                 artifact_dir=None, event=None, skills_home=None, skills_root=None):
        self.root = os.path.realpath(root)
        self.skills_root = os.path.realpath(skills_root or root)
        self.mcp_policy = mcp_policy if (mcp_policy and mcp_policy.get("servers")) else None
        self.allow_mcp_mutation = bool(allow_mcp_mutation)
        self.skills_dirs = list(skills_dirs or ())
        self.skills_home = skills_home
        self.artifact_dir = artifact_dir
        self.event = event              # MCP event sink; attach once transcript exists
        self.on_skill_load = None       # called with a skill name on runtime load
        self.advertised = set()         # MCP tool names actually shown to the model
        self.artifacts = {}             # artifact id -> absolute path
        self._mcp = self._skills = None
        self._seq = 0
        self.closed = False
        self.outcome_unknown = False    # a tools/call was sent and its outcome lost

    # -- lazily created managers ------------------------------------------------
    def mcp(self):
        if self.mcp_policy is None or self.closed:
            return None
        if self._mcp is None:
            mod = capability_module("harness_mcp")
            self._mcp = mod.MCPManager(self.mcp_policy, self.root,
                                       allow_mutation=self.allow_mcp_mutation,
                                       event=self._on_event)
        return self._mcp

    def skills(self):
        if self._skills is None:
            mod = capability_module("harness_skills")
            self._skills = mod.SkillRegistry(self.skills_root, skills_dirs=self.skills_dirs,
                                             home=self.skills_home, strict_duplicates=False)
            warn_skipped_skills(self._skills)
        return self._skills

    def _on_event(self, ev):
        # harness_mcp's own end status, not tool text: timeout/error mean the call
        # was sent and its outcome lost; rpc_error/tool_error are definite answers.
        if (isinstance(ev, dict) and ev.get("phase") == "end"
                and ev.get("tool") != "initialize+tools/list"
                and ev.get("status") in ("timeout", "error")):
            self.outcome_unknown = True
        sink = self.event
        if sink is not None:
            try:
                sink(ev)
            except Exception as e:      # observability must never break a call
                print(f"warning: MCP event sink failed: {type(e).__name__}",
                      file=sys.stderr)

    # -- catalog ----------------------------------------------------------------
    def skill_catalog(self):
        """Metadata only. A broken skills dir must not kill an unrelated run:
        the catalog is then empty (warned once); explicit loads still fail closed."""
        if getattr(self, "_catalog_error", None):
            return []
        try:
            return list(self.skills().catalog())
        except Exception as e:
            self._catalog_error = f"{type(e).__name__}: {e}"
            if not isinstance(e, CapabilityUnavailable):
                print(f"warning: skill catalog unavailable: {self._catalog_error}",
                      file=sys.stderr, flush=True)
            return []

    def loaded_skills(self):
        return list(getattr(self._skills, "loaded", []) or []) if self._skills else []

    def mcp_servers(self):
        return sorted((self.mcp_policy or {}).get("servers") or {})

    def mcp_native(self):
        mgr = self._mcp
        if mgr is None:
            return []
        return [t for t in (mgr.native_tools() or [])
                if isinstance(t, dict) and str(((t.get("function") or {}).get("name"))
                                               or "").startswith(MCP_PREFIX)]

    # -- model-facing tools -----------------------------------------------------
    def list_mcp_tools(self, server=None):
        if self.mcp_policy is None:
            return "no MCP servers are configured for this repo (policy has no mcp.servers)"
        if server is not None and not isinstance(server, str):
            raise ValueError("server must be a string")
        out = self.mcp().list_tools(server)
        if server is not None:
            out = {"server": server, "tools": out,
                   "note": "discovered tools are callable from your NEXT reply"}
        return bounded(json.dumps(strip_binary(out), indent=1, default=str), MAX_READ_CHARS)

    def load_skill(self, name):
        if not isinstance(name, str) or not name:
            raise ValueError("name must be a non-empty skill name")
        before = self.loaded_skills()
        body = skill_body_checked(name, self.skills().load(name, max_chars=MAX_READ_CHARS))
        if name not in before and self.on_skill_load:
            self.on_skill_load(name)
        return SKILL_LABEL.format(name=name) + body

    def call_mcp(self, name, args):
        mgr = self.mcp()
        if mgr is None or not mgr.owns(name):
            raise ValueError(f"no such MCP tool {name}")
        if name not in self.advertised:
            raise ValueError(f"{name} has not been discovered yet; call list_mcp_tools "
                             "with its server first, then call it on the next reply")
        return self.normalize_result(name, mgr.call(name, args))

    # -- results and artifacts --------------------------------------------------
    @staticmethod
    def _plain_dir_or_absent(p):
        """None if p is absent; else its lstat, refusing links/reparse points/non-dirs."""
        try:
            st = os.lstat(p)
        except FileNotFoundError:
            return None
        if (os.path.islink(p) or getattr(st, "st_file_attributes", 0) & 0x400
                or not os.path.isdir(p)):
            raise ValueError(f"artifact path component {os.path.basename(p)!r} is a "
                             "symlink/reparse point or not a plain directory")
        return st

    def _artifact_chain(self, d):
        """Components that must be plain dirs: below the run root when the artifact
        dir lives inside it (any link there is refused), else just the dir itself
        (an outside --transcript location is the caller's explicit choice)."""
        anchors = {os.path.normcase(self.root), os.path.normcase(self.skills_root)}
        chain, cur = [d], os.path.dirname(d)
        while cur and os.path.dirname(cur) != cur:
            if os.path.normcase(os.path.realpath(cur)) in anchors:
                return list(reversed(chain))
            chain.append(cur)
            cur = os.path.dirname(cur)
        return [d]

    def _artifact_root(self):
        """Canonical artifact dir, re-verified against the first-use snapshot on
        every call: same canonical path, same directory identity, no symlink or
        reparse substitution anywhere below the run root."""
        if not self.artifact_dir:
            raise ValueError("no artifact directory for this run")
        d = os.path.abspath(self.artifact_dir)
        snap = getattr(self, "_art_snap", None)
        chain = self._artifact_chain(d)
        if snap is None:
            os.makedirs(os.path.dirname(chain[0]), exist_ok=True)
            for comp in chain:                  # top-down; each checked before use
                if self._plain_dir_or_absent(comp) is None:
                    try:
                        os.mkdir(comp, 0o700)
                    except FileExistsError:
                        pass                    # re-checked by the identity pass below
        ids = []
        for comp in chain:
            st = self._plain_dir_or_absent(comp)
            if st is None:
                raise ValueError("artifact directory vanished during the run")
            ids.append((st.st_dev, st.st_ino))
        real = os.path.realpath(d)
        if snap is None:
            if os.path.dirname(real) != os.path.realpath(os.path.dirname(d)) or \
                    os.path.normcase(os.path.basename(real)) != \
                    os.path.normcase(os.path.basename(d)):
                raise ValueError("artifact directory resolves through a symlink")
            self._art_snap = (real, ids)
            return real
        if real != snap[0] or ids != snap[1]:
            raise ValueError("artifact directory changed during the run (substitution)")
        return real

    def save_artifact(self, data, ext):
        if self._seq >= MCP_MAX_ARTIFACTS:
            raise ValueError(f"too many artifacts this run (max {MCP_MAX_ARTIFACTS})")
        d = self._artifact_root()
        with _ART_LOCK:
            total = getattr(self, "_art_bytes", 0) + len(data)
            if total > MCP_MAX_ARTIFACT_BYTES:
                raise ValueError(f"artifact byte budget exhausted ({MCP_MAX_ARTIFACT_BYTES:,} "
                                 "bytes per run)")
            self._art_bytes = total
        self._seq += 1
        name = f"mcp-{self._seq:04d}-{hashlib.sha256(data).hexdigest()[:12]}{ext}"
        full = os.path.join(d, name)
        flags = (os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
                 | getattr(os, "O_NOFOLLOW", 0))
        fd = os.open(full, flags, 0o600)          # exclusive: never reuses/follows a file
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        self._artifact_root()                      # dir must still be the snapshotted one
        if full == self.root or full.startswith(self.root + os.sep):
            return os.path.relpath(full, self.root).replace(os.sep, "/")
        ref = f"artifact://{name}"
        self.artifacts[name] = full
        return ref

    def open_command_sink(self, stream):
        """Exclusive run-local command-output file under the shared artifact quota."""
        d = self._artifact_root()
        with _ART_LOCK:  # both drains may open sinks concurrently
            if self._seq >= MCP_MAX_ARTIFACTS:
                raise ValueError(f"too many artifacts this run (max {MCP_MAX_ARTIFACTS})")
            self._seq += 1
            seq = self._seq
        name = f"cmd-{seq:04d}-{stream}.out"
        full = os.path.join(d, name)
        flags = (os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
                 | getattr(os, "O_NOFOLLOW", 0))
        return _CommandSink(self, name, full, os.open(full, flags, 0o600))

    def resolve_command_output(self, ref):
        """(path, size, digest, identity) for THIS run's command artifact, else ValueError."""
        entry = getattr(self, "command_outputs", {}).get(ref) if isinstance(ref, str) else None
        if entry is None:
            raise ValueError("not a command-output reference from this run")
        self._artifact_root()
        return entry

    def _save_b64(self, kind, mime, payload, saved, lines):
        """Decode one base64 payload to an artifact file; never keeps the base64."""
        try:
            data = decode_b64(payload)
            ext = MCP_IMAGE_EXT.get(mime.lower(), ".bin")
            ref = self.save_artifact(data, ext)
        except (ValueError, OSError) as e:
            lines.append(f"[{kind} part refused: {e}]")
            return
        saved.append(ref)
        hint = " (view with view_image next turn)" if ext != ".bin" else ""
        lines.append(f"[{kind} {mime or '?'} {len(data):,} bytes saved to {ref}{hint}]")

    def normalize_result(self, name, raw):
        raw = raw if isinstance(raw, dict) else {"content": [{"type": "text",
                                                               "text": str(raw)}]}
        lines, texts, saved = [], [], []
        if raw.get("isError"):
            lines.append("isError: true")
        if raw.get("error") is not None:
            lines.append("error: " + json.dumps(strip_binary(raw["error"]), default=str)[:2000])
        rpc = raw.get("rpcError")
        if isinstance(rpc, dict):
            lines.append(f"rpcError code={json.dumps(rpc.get('code'), default=str)[:40]}: "
                         + bounded(str(rpc.get("message", "")), 2000))
            if rpc.get("data") is not None:
                lines.append("rpcError data: " + bounded(json.dumps(
                    strip_binary(rpc["data"]), default=str), 1000))
        elif rpc is not None:
            lines.append("rpcError: " + bounded(str(rpc), 2000))
        parts = raw.get("content") if isinstance(raw.get("content"), list) else []
        if len(parts) > MCP_MAX_PARTS:
            lines.append(f"[{len(parts) - MCP_MAX_PARTS} further content parts omitted; "
                         f"max {MCP_MAX_PARTS}]")
        for part in parts[:MCP_MAX_PARTS]:
            if not isinstance(part, dict):
                continue
            kind = str(part.get("type") or "binary")[:40]
            res = part.get("resource")
            if kind == "text":
                texts.append(str(part.get("text", "")))
            elif kind == "resource" and isinstance(res, dict):
                # Embedded resource: inline payload only; the uri is never fetched.
                uri = bounded(scrub_mcp_data_urls(str(res.get("uri", "?"))), 300)
                mime = str(res.get("mimeType") or "")[:100]
                if isinstance(res.get("blob"), str):
                    self._save_b64(f"resource {uri}", mime, res["blob"], saved, lines)
                elif res.get("text") is not None:
                    texts.append(f"[resource {uri} {mime or 'text'}]\n{res['text']}")
                else:
                    lines.append("[resource] " + json.dumps(strip_binary(res),
                                                            default=str)[:1000])
            elif kind in ("image", "audio") or "data" in part or "blob" in part:
                mime = str(part.get("mimeType") or "")[:100]
                self._save_b64(kind, mime, part.get("data", part.get("blob")), saved, lines)
            else:
                lines.append("[content part] " + json.dumps(strip_binary(part),
                                                            default=str)[:1000])
        if raw.get("structuredContent") is not None and not texts:
            texts.append(json.dumps(strip_binary(raw["structuredContent"]), default=str))
        text = "\n".join(texts)
        head = "\n".join(lines)
        room = MAX_READ_CHARS - len(head) - 1
        if len(text) > room:
            clean = strip_binary(raw)
            clean["_saved_artifacts"] = saved     # file refs replace the base64 payloads
            full = json.dumps(clean, indent=1, default=str).encode("utf-8")
            ref = self.save_artifact(full, ".json")
            suffix = f"\n...[truncated; full result ({len(full):,} bytes) saved to {ref}]"
            text = bounded(text, max(0, room), suffix)
        return bounded(scrub_mcp_data_urls((head + "\n" + text).strip("\n")) or "(empty MCP result)",
                       MAX_READ_CHARS)

    def resolve_read(self, path):
        """Read-only resolution of skill:// and artifact:// references."""
        if path.startswith("skill://"):
            return self.skills().resolve_read(path)
        if path.startswith("artifact://"):
            name = path[len("artifact://"):]
            full = self.artifacts.get(name)
            if not full or "/" in name or "\\" in name:
                raise ValueError(f"unknown artifact {path}")
            d = self._artifact_root()
            if os.path.dirname(os.path.realpath(full)) != d or os.path.islink(full) \
                    or not os.path.isfile(full):
                raise ValueError(f"artifact {path} escapes the artifact directory")
            return full
        raise ValueError(f"unsupported URI {path}")

    def close(self):
        if self.closed:
            return
        self.closed = True
        mgr, self._mcp = self._mcp, None
        if mgr is not None:
            mgr.close()


def close_open_runners():
    failures = 0
    for r in list(_OPEN_RUNNERS):
        try:
            r.close()
        except Exception as exc:
            failures += 1
            diagnostic("cleanup_error", phase="cleanup", outcome="raised",
                       **_safe_exception_fields(exc))
    return failures


atexit.register(close_open_runners)


class Runner:
    # SKIP_DIRS / MAX_READ_CHARS are module-level and policy-driven (see
    # load_policy), not class attributes, so a repo's own boundary is in force
    # here without the caller having to thread it through.

    def __init__(self, root, write_mode="stage", *, mcp_policy=None, skills_dirs=(),
                 allow_mcp_mutation=False, artifact_dir=None, event=None,
                 skills_home=None, skills_root=None, verification_plan=None,
                 text_only=False, boundary=None, profile="legacy", read_files_enabled=False):
        """write_mode 'disk' writes for real; 'stage' accumulates a review patch.

        Capability keywords are optional: Runner(root, write_mode) behaves as
        before, with only the always-present list_mcp_tools/load_skill added.

        Staging is safe but makes an agent unable to verify itself: run_command
        executes against the on-disk tree, so a staged edit is invisible to the
        tests that are supposed to prove it. Worktree runs use 'disk' precisely so
        that implement-then-verify is coherent; the isolation comes from the
        worktree, not from withholding the writes.
        """
        if profile not in ("legacy", "resilient-v1"):
            raise ValueError(f"unknown execution profile: {profile}")
        if type(read_files_enabled) is not bool or (read_files_enabled and profile != "resilient-v1"):
            raise ValueError("read_files requires the resilient-v1 profile")
        self.read_files_enabled = read_files_enabled
        if read_files_enabled:
            self.BUILTIN_TOOLS = self.BUILTIN_TOOLS + ("read_files",)
        self.profile = profile
        self.context_runtime = None
        self._paged = capability_module("paged_tools") if profile == "resilient-v1" else None
        self._output = capability_module("tool_output") if self._paged is not None else None
        self._commands = capability_module("command_output") if self._paged is not None else None
        self.root = os.path.realpath(root)
        self.boundary = boundary
        self.text_only = text_only
        self.write_mode = write_mode
        self.pending = {}
        self.read_whole = set()
        self.calls = 0
        self.refused = 0
        self.command_timed_out = False  # a command ran past CMD_TIMEOUT and may have acted
        fg = _optional_module("finish_gate")
        traj = _optional_module("trajectory")
        self.base_tracker = fg.StagedBaseTracker() if fg is not None else None
        self.evidence = (
            fg.RunEvidence(
                write_mode, self.root, plan=verification_plan or fg.VerificationPlan()
            )
            if fg is not None
            else None
        )
        self.trajectory = traj.TrajectoryDetector() if traj is not None else None
        self._turn_suspicions = []
        self.current_turn = 0
        self.candidate_seq = 0
        self._last_mutation_changed = False
        self._last_cmd_info = ("ok", False, None, False)
        self.caps = Capabilities(self.root, mcp_policy=mcp_policy, skills_dirs=skills_dirs,
                                 allow_mcp_mutation=allow_mcp_mutation,
                                 artifact_dir=artifact_dir, event=event,
                                 skills_home=skills_home, skills_root=skills_root)
        _OPEN_RUNNERS.append(self)

    def drain_turn_suspicions(self):
        out = tuple(self._turn_suspicions)
        self._turn_suspicions.clear()
        return out

    @property
    def outcome_unknown(self):
        """True once a dispatched action may have acted with its outcome lost."""
        return self.command_timed_out or self.caps.outcome_unknown

    def close(self):
        """Idempotent; stops any MCP servers this run started."""
        try:
            self.caps.close()
        finally:
            if self in _OPEN_RUNNERS:
                _OPEN_RUNNERS.remove(self)

    def list_mcp_tools(self, server=None):
        return self.caps.list_mcp_tools(server)

    def load_skill(self, name):
        return self.caps.load_skill(name)

    def _read_uri(self, path, start_line=None, end_line=None):
        """Read-only skill:// / artifact:// read. Line ranges stream the file, so
        any line of a large sidefile is reachable without loading the whole file;
        returned text is bounded by MAX_READ_CHARS either way."""
        full = self.caps.resolve_read(path)
        if start_line is None and end_line is None:
            with open(full, "r", encoding="utf-8", errors="replace", newline="") as fh:
                text = fh.read(MAX_READ_CHARS + 1)
            if len(text) > MAX_READ_CHARS:
                return (f"refused: {path} is over the {MAX_READ_CHARS:,}-char whole-read "
                        "limit; read it with start_line/end_line.")
            return text
        a = max(1, int(start_line or 1))
        b = int(end_line or a + 200)
        if b < a:
            raise ValueError("end_line must be >= start_line")
        out, size, last, cut = [], 0, a - 1, False
        with open(full, "r", encoding="utf-8", errors="replace", newline="") as fh:
            for i, line in enumerate(_bounded_lines(fh, MAX_READ_CHARS + 1), 1):
                if i < a:
                    continue
                if i > b:
                    break
                if size + len(line) > MAX_READ_CHARS:
                    out.append(line[:max(0, MAX_READ_CHARS - size)])
                    cut = True
                    last = i
                    break
                out.append(line)
                size += len(line)
                last = i
        if last < a:
            return f"({path} has no line {a}: start_line is past the end of the file)"
        body = "".join(out)
        if cut:
            body += "\n...[range too large; narrow it]"
        return f"({path} lines {a}-{last})\n" + body

    def _resolve(self, path):
        if isinstance(path, str) and _URI.match(path):
            raise ValueError(f"URIs are not repo paths ({path}); only read_file accepts "
                             "skill:// and artifact:// references")
        full = os.path.realpath(os.path.join(self.root, path))
        if full != self.root and not full.startswith(self.root + os.sep):
            raise ValueError(f"path escapes the repo root: {path}")
        return full

    def view_image(self, path):
        """Load a repo image for the model to see (PNG/JPEG/WebP).

        Stricter than _resolve's lexical rules: only plain repo-relative paths.
        SKIP_DIRS is search hygiene, not access control, so it is not checked.
        """
        if self.text_only:
            return "refused: text-only image policy; no image was loaded"
        if not isinstance(path, str) or not path.strip():
            raise ValueError("path must be a non-empty repo-relative string")
        if path.startswith("artifact://"):
            # Only images this run saved itself (registered, confined, re-verified);
            # skill:// and every other URI stay refused by _resolve below.
            if os.path.splitext(path)[1].lower() not in set(MCP_IMAGE_EXT.values()):
                raise ValueError(f"{path} is not a saved image artifact")
            return prepare_image(self.caps.resolve_read(path), path)
        norm = path.replace("\\", "/")
        if (norm.startswith("/") or os.path.isabs(path)
                or re.match(r"^[A-Za-z]:", path) or os.path.splitdrive(path)[0]):
            raise ValueError(f"absolute, rooted, drive or UNC paths are refused: {path}")
        if ".." in norm.split("/"):
            raise ValueError(f"'..' path components are refused: {path}")
        full = self._resolve(path)
        return prepare_image(full, norm)

    def read_files(self, requests):
        if not self.read_files_enabled:
            return "error: no such tool read_files"
        return self._paged.read_files(self, requests, MAX_READ_CHARS)

    def read_file(self, path, start_line=None, end_line=None):
        """Read a file, or a line range of one.

        Whole-file reads of large files are the main way an agent run dies: one
        86 KB stylesheet read twice cost ~45k tokens and blew the context. So
        oversized reads are refused with directions rather than served, and a
        repeated whole-file read returns a pointer instead of the content again.
        """
        if self.profile == "resilient-v1":
            return self._paged.read_file(self, path, start_line, end_line, MAX_READ_CHARS)
        if isinstance(path, str) and path.startswith(("skill://", "artifact://")):
            return self._read_uri(path, start_line, end_line)
        if path in self.pending:
            return f"(pending, not yet on disk)\n{self.pending[path]}"
        text = open(self._resolve(path), encoding="utf-8", errors="replace").read()

        if start_line is not None or end_line is not None:
            lines = text.splitlines(True)
            a = max(1, int(start_line or 1))
            b = min(len(lines), int(end_line or a + 200))
            body = "".join(lines[a - 1:b])
            head = f"({path} lines {a}-{b} of {len(lines)})\n"
            if len(body) > MAX_READ_CHARS:
                body = body[:MAX_READ_CHARS] + "\n...[range too large; narrow it]"
            return head + body

        if path in self.read_whole:
            return (f"You already read all of {path} earlier in this run; the content "
                    "is above. Re-read a specific range with start_line/end_line if you "
                    "need to look again.")

        if len(text) > MAX_READ_CHARS:
            n = text.count("\n") + 1
            return (f"refused: {path} is {len(text):,} chars ({n:,} lines), over the "
                    f"{MAX_READ_CHARS:,}-char whole-read limit. Use grep to locate "
                    "what you need, then read_file with start_line/end_line. Reading a "
                    "file this size whole will exhaust the context and kill this run.")

        self.read_whole.add(path)
        return text

    def list_dir(self, path=".", **paging):
        if self.profile == "resilient-v1":
            return self._paged.list_dir(self, path, SKIP_DIRS, **paging)
        if paging:
            raise TypeError("legacy list_dir does not accept paging arguments")
        full = self._resolve(path)
        out = []
        for name in sorted(os.listdir(full)):
            if name in SKIP_DIRS:
                continue
            p = os.path.join(full, name)
            out.append(f"{name}/" if os.path.isdir(p) else
                       f"{name}  ({os.path.getsize(p):,} b)")
        return "\n".join(out) or "(empty)"

    def _grep_page(self, pattern, glob, offset, limit, use_rg=False):
        return self._paged.grep(self, pattern, glob, SKIP_DIRS, offset=offset,
                                limit=limit, use_rg=use_rg)

    def grep(self, pattern, glob=None, **paging):
        if self.profile == "resilient-v1":
            return self._paged.grep(self, pattern, glob, SKIP_DIRS, **paging)
        if paging:
            raise TypeError("legacy grep does not accept paging arguments")
        argv = ["rg", "-n", "--no-heading", "-S", pattern]
        if glob:
            argv += ["-g", glob]
        # Exclusions LAST: ripgrep resolves competing globs last-match-wins, and the
        # skipped dirs must beat a caller glob that would otherwise reach into them.
        # Without this, a repo policy meant something different on a machine with
        # ripgrep installed than on one falling back to _grep_py, which prunes these
        # dirs before any glob is considered.
        for d in sorted(SKIP_DIRS):
            argv += ["-g", f"!{d}/"]
        try:
            r = subprocess.run(argv, cwd=self.root, capture_output=True,
                               text=True, timeout=120, shell=False)
            return r.stdout or "(no matches)"
        except subprocess.TimeoutExpired:
            return "error: search timed out"
        except FileNotFoundError:
            pass                       # ripgrep absent on this machine; fall through
        return self._grep_py(pattern, glob)

    def _grep_py(self, pattern, glob=None):
        try:
            rx = re.compile(pattern, re.IGNORECASE)
        except re.error as e:
            return f"error: bad regex: {e}"
        hits, truncated = [], False
        for dirpath, dirnames, filenames in os.walk(self.root):
            dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
            for fn in filenames:
                full = os.path.join(dirpath, fn)
                rel = os.path.relpath(full, self.root).replace(os.sep, "/")
                if glob and not fnmatch.fnmatch(rel, glob) and not fnmatch.fnmatch(fn, glob):
                    continue
                try:
                    full = self._resolve(rel)   # skip symlinked files escaping the root
                except ValueError:
                    continue
                try:
                    with open(full, encoding="utf-8", errors="strict") as fh:
                        for n, line in enumerate(fh, 1):
                            if rx.search(line):
                                hits.append(f"{rel}:{n}:{line.rstrip()[:300]}")
                                if len(hits) >= 2000:
                                    truncated = True
                                    break
                except (OSError, UnicodeDecodeError):
                    continue
                if truncated:
                    break
            if truncated:
                break
        if not hits:
            return "(no matches)"
        return "\n".join(hits) + ("\n...[stopped at 2000 matches]" if truncated else "")

    def run_command(self, argv):
        if not isinstance(argv, list) or not all(isinstance(a, str) for a in argv):
            self._last_cmd_info = ("invalid_args", False, None, False)
            return "error: argv must be a list of strings"

        # Path-shaped arguments must stay inside the repo. Without this a pattern
        # rule is not the boundary it appears to be: fnmatch's * spans separators,
        # so ["python", "scripts/*"] also matches "scripts/../../evil.py" and would
        # happily run code from anywhere on disk -- the one thing _resolve exists
        # to prevent for the file tools. Only arguments that LOOK like paths are
        # checked, because ".." is legitimate elsewhere: a git revision range
        # (git log HEAD..HEAD~2) contains it and must keep working.
        #
        # Do not reduce the root test to os.path.isabs. Python 3.13 changed it on
        # Windows so a path rooted with a single (back)slash is no longer absolute,
        # and "/tmp/evil.py" started passing this guard. It went unnoticed because
        # the allowlist refused such a path anyway -- until a rule shorter than argv
        # left a trailing argument unconstrained, e.g. ["python","-m","pytest"] plus
        # "/tmp/evil_test.py", which pytest then collects and EXECUTES.
        for a in argv:
            if "/" not in a and "\\" not in a:
                continue
            norm = a.replace("\\", "/")
            if norm.startswith("/") or os.path.isabs(a) \
                    or re.match(r"^[A-Za-z]:", a) \
                    or ".." in norm.split("/"):
                self.refused += 1
                if self.evidence is not None:
                    self.evidence.record_command(
                        argv, turn=self.current_turn, outcome="refused",
                        exit_code=None, root_identity=self.root)
                self._last_cmd_info = ("refused", False, None, False)
                return (f"refused: the argument {a!r} is an absolute path, or escapes "
                        "the repo with '..'. Arguments that name a path must be "
                        "repo-relative and stay inside the repo. Do not retry it.")

        def permitted(rule):
            return len(argv) >= len(rule) and all(
                fnmatch.fnmatch(argv[i], rule[i]) for i in range(len(rule)))

        if not any(permitted(rule) for rule in ALLOWED_CMDS):
            self.refused += 1
            if self.evidence is not None:
                self.evidence.record_command(
                    argv, turn=self.current_turn, outcome="refused",
                    exit_code=None, root_identity=self.root)
            self._last_cmd_info = ("refused", False, None, False)
            return (f"refused: {' '.join(argv)} is not allowlisted.\n"
                    "Allowed patterns: " + "; ".join(" ".join(a) for a in ALLOWED_CMDS)
                    + "\nDo not retry the same command; it will be refused again.")
        if self.boundary is not None and not self.boundary.check_command(argv):
            self.refused += 1
            self._last_cmd_info = ("refused", False, None, False)
            return ("refused: supervisor boundary allows only its exact selected argv. "
                    "Do not retry it.")
        # On Windows npm/npx/node are .cmd shims that shell=False cannot resolve by
        # bare name. Resolve the real executable rather than reaching for a shell.
        exe = shutil.which(argv[0])
        if exe is None:
            for ext in (".cmd", ".exe", ".bat"):
                exe = shutil.which(argv[0] + ext)
                if exe:
                    break
        if exe is None:
            if self.evidence is not None:
                self.evidence.record_command(
                    argv, turn=self.current_turn, outcome="exited",
                    exit_code=127, root_identity=self.root)
            self._last_cmd_info = ("not_found", False, 127, False)
            return f"error: {argv[0]} not found on PATH"
        # Stage mode: commands cannot touch the staged candidate (staged-base
        # validation guards the disk base). Disk mode scans only when a run
        # designated verification commands; otherwise effects stay unseen.
        disk = self.write_mode == "disk"
        plan = self.evidence.plan.commands if self.evidence is not None else ()
        before_tree = _candidate_snapshot(self.root) if disk and plan else None
        # Legacy subprocess.run hides identity; the streaming collector observes Popen.
        cmd_fields = dict(phase="command", call_index=getattr(self, "_diagnostic_call_index", None),
                          turn=self.current_turn if type(self.current_turn) is int else None)
        observed = {}
        def spawned(proc):
            observed["child_pid"] = proc.pid
            diagnostic("command_start", child_pid=proc.pid, _child=proc, **cmd_fields)
        if self.profile != "resilient-v1" and self.context_runtime is None:
            diagnostic("command_start", **cmd_fields)
        try:
            if self.profile == "resilient-v1" or self.context_runtime is not None:
                r = self._commands.capture_command(
                    [exe] + argv[1:], cwd=self.root, timeout=CMD_TIMEOUT,
                    open_sink=self._command_sink, on_start=spawned,
                    on_exit=lambda code: observed.update(exit_code=code))
            else:
                r = subprocess.run([exe] + argv[1:], cwd=self.root, capture_output=True,
                                   text=True, errors="replace", timeout=CMD_TIMEOUT,
                                   shell=False)
        except subprocess.TimeoutExpired:
            # Null exit_code: the child's status is unknown, not 0.
            diagnostic("command_exit", outcome="failed", exit_code=observed.get("exit_code"),
                       child_pid=observed.get("child_pid"),
                       exception_class="TimeoutExpired", **cmd_fields)
            self.command_timed_out = True
            if self.evidence is not None:
                self.evidence.record_command(
                    argv, turn=self.current_turn, outcome="timed_out",
                    exit_code=None, root_identity=self.root)
            self._last_cmd_info = ("timed_out", True, None, False)
            return f"error: timed out after {CMD_TIMEOUT}s"
        except (OSError, UnicodeError) as e:
            diagnostic("command_exit", outcome="failed", exit_code=observed.get("exit_code"),
                       child_pid=observed.get("child_pid"), **cmd_fields,
                       **_safe_exception_fields(e))
            # run() may fail during communication after the child has acted.
            self.command_timed_out = True
            if self.evidence is not None:
                self.evidence.record_command(
                    argv, turn=self.current_turn, outcome="outcome_unknown",
                    exit_code=None, root_identity=self.root)
            self._last_cmd_info = ("outcome_unknown", True, None, False)
            return f"error: command outcome unavailable ({type(e).__name__})"
        diagnostic("command_exit", outcome="returned", exit_code=r.returncode,
                   child_pid=observed.get("child_pid"), **cmd_fields)
        # Snapshot trouble never changes the command's own exit result.
        after_tree = _candidate_snapshot(self.root) if before_tree is not None else None
        mutated_candidate = bool(after_tree is not None and after_tree != before_tree)
        if disk and after_tree is None:
            self.candidate_seq += 1  # unseen effects: invalidate candidate/review
            if self.evidence is not None:
                self.evidence.record_candidate_unavailable()
        elif mutated_candidate:
            self.candidate_seq += 1
        if self.evidence is not None:
            self.evidence.record_command(
                argv, turn=self.current_turn, outcome="exited", exit_code=r.returncode,
                root_identity=self.root, mutated_candidate=mutated_candidate)
        self._last_cmd_info = ("ok" if r.returncode == 0 else "nonzero",
                               True, r.returncode, mutated_candidate)
        if self.profile == "resilient-v1" or self.context_runtime is not None:
            return self._format_captured(r)
        return f"exit={r.returncode}\n--- stdout ---\n{r.stdout}\n--- stderr ---\n{r.stderr}"

    def read_command_output(self, ref, offset=0, limit=4096):
        """Page a THIS-run command-output artifact; never executes anything."""
        if self._commands is None:
            return "error: read_command_output is unavailable in this profile"
        try:
            entry = self.caps.resolve_command_output(ref)
        except (ValueError, OSError) as e:
            return f"error: {e}"
        for name, v in (("offset", offset), ("limit", limit)):
            if type(v) is not int:
                return f"error: {name} must be an integer"
        if not 0 <= offset <= entry["size"]:
            return f"error: offset must be within 0..{entry['size']}"
        if not 1 <= limit <= 4096:
            return "error: limit must be within 1..4096"
        co = self._commands
        try:
            page = co.read_page(ref, entry, offset, limit)
        except (ValueError, OSError) as e:
            return f"error: {e}"
        return co.CompactText(json.dumps(page, ensure_ascii=True, sort_keys=True),
                              lambda: co.page_compact(page))

    def read_context_result(self, ref, offset=0, limit=4096):
        """Page a THIS-run context result; never opens caller-supplied paths."""
        if self.context_runtime is None:
            return "error: read_context_result is unavailable in this profile"
        try:
            args = checked_tool_arguments(
                "read_context_result", {"ref": ref, "offset": offset, "limit": limit})
            page = self.context_runtime.read_result(**args)
        except ValueError:
            return "error: invalid context result request"
        if not isinstance(page, dict):
            return "error: context result unavailable"
        co = self._commands
        return co.CompactText(json.dumps(page, ensure_ascii=True, sort_keys=True),
                              lambda: co.page_compact(page))

    def compact_result(self, name, result):
        if self._commands is None or not isinstance(result, str):
            return None
        return self._commands.compact_result(name, result)

    def _command_sink(self, stream):
        if not self.caps.artifact_dir:
            raise ValueError("no artifact directory for this run")
        return self.caps.open_command_sink(stream)

    def _format_captured(self, r):
        """Small complete output keeps the legacy shape; else preview + refs."""
        co = self._commands
        out, err = r.stdout, r.stderr
        recs = {"stdout": out.record(), "stderr": err.record()}
        for c in (out, err):
            entry = getattr(self.caps, "command_outputs", {}).get(c.ref)
            if entry is not None:
                entry["complete"], entry["discarded"] = c.complete, c.discarded

        def compact():
            # Lazy: inline-only streams are published only when the guard needs refs.
            for name, c in (("stdout", out), ("stderr", err)):
                rec = recs[name]
                if rec["ref"] is None and c.prefix and rec["total_bytes"] == len(c.prefix):
                    sink, ref, n = None, None, 0
                    try:
                        sink = self._command_sink(name)
                        n = sink.write(bytes(c.prefix))
                    except (ValueError, OSError):
                        pass
                    finally:
                        if sink is not None:
                            try:
                                ref = sink.close()
                            except (ValueError, OSError):
                                pass
                    if ref is not None:
                        rec.update(ref=ref, stored_bytes=n)
                # Compaction removes the inline prefix. Count only bytes reachable
                # through a published artifact, including any partial publication.
                stored = rec["stored_bytes"] if rec["ref"] is not None else 0
                rec.update(stored_bytes=stored,
                           discarded_bytes=max(0, rec["total_bytes"] - stored),
                           capture_complete=rec["capture_complete"]
                           and stored == rec["total_bytes"])
                entry = getattr(self.caps, "command_outputs", {}).get(rec["ref"])
                if entry is not None:
                    entry.update(complete=rec["capture_complete"],
                                 discarded=rec["discarded_bytes"])
            return co.command_compact(r.returncode, recs)

        if out.ref is None and err.ref is None and out.complete and err.complete:
            return co.CompactText(f"exit={r.returncode}\n--- stdout ---\n"
                                  f"{co.legacy_text(out.prefix)}\n--- stderr ---\n"
                                  f"{co.legacy_text(err.prefix)}", compact)
        lines = [f"exit={r.returncode}"]
        for name, c in (("stdout", out), ("stderr", err)):
            lines.append(f"--- {name} {json.dumps(recs[name], sort_keys=True)} ---")
            lines.append(co.legacy_text(bytes(c.prefix[:COMMAND_PREVIEW_BYTES])))
        lines.append("[output exceeds inline limit; page it with read_command_output"
                     "(ref, offset, limit) - do not rerun the command]")
        return co.CompactText("\n".join(lines), compact)

    def _current(self, path):
        """Contents an edit should apply to: staged version if any, else disk."""
        if self.write_mode == "stage" and path in self.pending:
            return self.pending[path]
        return open(self._resolve(path), encoding="utf-8", errors="replace").read()

    def _commit(self, path, content):
        _check_utf8_text("file content", content)
        full = self._resolve(path)
        if self.boundary is not None:
            self.boundary.check_write(self.root, full)
        if self.write_mode != "disk" and self.base_tracker is not None:
            self.base_tracker.record_if_first(path, full)
        try:
            before = self._current(path)
        except OSError:
            before = None
        self.read_whole.discard(path)          # contents changed; allow a re-read
        if self.write_mode == "disk":
            os.makedirs(os.path.dirname(full), exist_ok=True)
            tmp = f"{full}.{uuid.uuid4().hex}.tmp"
            try:
                # Write a sibling then replace: a failed write never truncates the old bytes.
                fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL
                             | getattr(os, "O_BINARY", 0), 0o666)
                with os.fdopen(fd, "wb") as fh:
                    fh.write(content.encode("utf-8"))
                    fh.flush()
                    os.fsync(fh.fileno())
                if os.path.exists(full):
                    shutil.copymode(full, tmp)
                os.replace(tmp, full)
            except BaseException:
                with contextlib.suppress(OSError):
                    os.remove(tmp)
                if self.evidence is not None:
                    self.evidence.record_unknown_mutation()
                self.candidate_seq += 1
                if self.trajectory is not None:
                    self.trajectory.advance_epoch()
                raise
            wrote = True
        else:
            self.pending[path] = content
            wrote = False
        if self.evidence is not None:
            changed = self.evidence.record_mutation(
                path, before, content, staged=(self.write_mode != "disk"))
        else:
            changed = (before is None or before != content)
        if changed:
            self.candidate_seq += 1
        self._last_mutation_changed = changed
        return wrote

    def write_file(self, path, content):
        self._resolve(path)
        wrote = self._commit(path, content)
        return (f"wrote {path} ({len(content):,} chars)." if wrote else
                f"staged {path} ({len(content):,} chars). Not written to disk.")

    def replace_in_file(self, path, old_string, new_string):
        """Exact-match single replacement.

        The only way to edit a file too large to read whole -- requiring full
        contents for a write made an 82 KB stylesheet uneditable. Refuses on zero
        or multiple matches rather than guessing which occurrence was meant.
        """
        full = self._resolve(path)
        if self.boundary is not None:
            self.boundary.check_write(self.root, full)
        try:
            text = self._current(path)
        except OSError as e:
            return f"error: cannot read {path}: {e}"
        n = text.count(old_string)
        if n == 0:
            return (f"error: old_string not found in {path}. It must match the file "
                    "byte-for-byte including indentation. grep for a nearby unique "
                    "line and copy it exactly.")
        if n > 1:
            return (f"error: old_string appears {n} times in {path}; it must be unique. "
                    "Include surrounding lines to disambiguate.")
        wrote = self._commit(path, text.replace(old_string, new_string))
        return (f"replaced 1 occurrence in {path}"
                + ("." if wrote else " (staged, not on disk)."))

    def patch(self):
        chunks = []
        for path, new in sorted(self.pending.items()):
            if self.base_tracker is not None:
                old = self.base_tracker.validate_and_get_base(path, self._resolve(path))
            else:
                try:
                    old = open(self._resolve(path), encoding="utf-8", errors="replace").read()
                except OSError:
                    old = ""
            chunks.extend(difflib.unified_diff(
                old.splitlines(True), new.splitlines(True),
                fromfile="a/" + path, tofile="b/" + path))
        return "".join(chunks)

    BUILTIN_TOOLS = ("read_file", "view_image", "list_dir", "grep", "run_command",
                     "write_file", "replace_in_file", "read_command_output",
                     "read_context_result") + CAPABILITY_TOOLS

    def dispatch(self, name, args):
        # "returned" vs "raised" is transport-level; business success lives in the text.
        if getattr(self, "_diagnostic_turn", None) != self.current_turn:
            self._diagnostic_turn, self._diagnostic_call_index = self.current_turn, 0
        requested = getattr(self, "_diagnostic_next_index", None)
        self._diagnostic_call_index = (requested if type(requested) is int and requested > 0
                                      else getattr(self, "_diagnostic_call_index", 0) + 1)
        self._diagnostic_next_index = None
        fields = dict(phase="tool", tool_name=diag_tool_name(name), call_index=self._diagnostic_call_index,
                      turn=self.current_turn if type(self.current_turn) is int else None)
        self._diagnostic_fields = fields
        diagnostic("tool_start", **fields)
        try:
            out = self._dispatch(name, args)
        except BaseException as exc:
            diagnostic("tool_error", outcome="raised", **fields,
                       **_safe_exception_fields(exc))
            raise
        diagnostic("tool_end", outcome="returned", **fields)
        return out

    def _dispatch(self, name, args):
        self.calls += 1
        if not isinstance(args, dict):
            return "error: tool arguments must be a JSON object"
        if self.trajectory is not None:
            ToolObservation = capability_module("trajectory").ToolObservation
        if isinstance(name, str) and name.startswith(MCP_PREFIX):
            try:
                out = self.caps.call_mcp(name, args)
            except Exception as e:
                diagnostic("tool_error", outcome="error", **self._diagnostic_fields,
                           **_safe_exception_fields(e))
                out = f"error: {type(e).__name__}: {e}"
            if self.caps.outcome_unknown:
                if self.evidence is not None:
                    self.evidence.record_unknown_mutation()
            elif self.caps.allow_mcp_mutation:
                if self.evidence is not None:
                    self.evidence.record_unknown_mutation()
                self.candidate_seq += 1
            if self.trajectory is not None:
                if self.caps.outcome_unknown:
                    obs = ToolObservation(tool=name, turn=self.current_turn,
                                          outcome="outcome_unknown", uncertain_effect=True)
                elif self.caps.allow_mcp_mutation:
                    obs = ToolObservation(tool=name, turn=self.current_turn,
                                          outcome="ok", state_changed=True)
                else:
                    obs = ToolObservation(
                        tool=name, turn=self.current_turn, outcome="ok",
                        args_fingerprint=self.trajectory.fingerprint(args),
                        result_fingerprint=self.trajectory.fingerprint(out))
                self._turn_suspicions.extend(self.trajectory.observe(obs))
            return bounded(out, MAX_RESULT_CHARS)
        # Explicit registry: only names this harness defines AND advertises.
        if name not in self.BUILTIN_TOOLS or name not in {d[0] for d in tool_docs(self)}:
            return f"error: no such tool {name}"
        fn = getattr(self, name)
        self._last_mutation_changed = False
        try:
            out = fn(**args)
        except Exception as e:
            diagnostic("tool_error", outcome="error", **self._diagnostic_fields,
                       **_safe_exception_fields(e))
            out = f"error: {type(e).__name__}: {e}"
            if self.trajectory is not None:
                self._turn_suspicions.extend(self.trajectory.observe(ToolObservation(
                    tool=name, turn=self.current_turn, outcome="error",
                    args_fingerprint=self.trajectory.fingerprint(args),
                    result_fingerprint=self.trajectory.fingerprint(out))))
            return out
        if self.trajectory is not None:
            canon_path = None
            if (isinstance(args.get("path"), str)
                    and not args["path"].startswith(("skill://", "artifact://"))):
                try:
                    canon_path = self._resolve(args["path"])
                except ValueError:
                    pass
            args_fp = self.trajectory.fingerprint(args)
            if name == "run_command":
                cmd_outcome, cmd_started, _, cmd_mutated = self._last_cmd_info
                obs = ToolObservation(
                    tool=name, turn=self.current_turn, outcome=cmd_outcome,
                    command_started=cmd_started, state_changed=cmd_mutated,
                    uncertain_effect=cmd_outcome in ("timed_out", "outcome_unknown"),
                    args_fingerprint=args_fp, result_fingerprint=self.trajectory.fingerprint(out))
            elif name == "replace_in_file":
                failed = isinstance(out, str) and out.startswith("error:")
                obs = ToolObservation(
                    tool=name, turn=self.current_turn, canonical_path=canon_path,
                    outcome="replace_failed" if failed else "ok",
                    state_changed=(not failed and self._last_mutation_changed),
                    args_fingerprint=args_fp, result_fingerprint=self.trajectory.fingerprint(out))
            elif name == "write_file":
                obs = ToolObservation(
                    tool=name, turn=self.current_turn, canonical_path=canon_path, outcome="ok",
                    state_changed=self._last_mutation_changed, args_fingerprint=args_fp,
                    result_fingerprint=self.trajectory.fingerprint(out))
            elif name == "read_file":
                refused = isinstance(out, str) and out.startswith("refused:")
                res_fp = self.trajectory.fingerprint(out)
                if (canon_path is not None and args.get("start_line") is None
                        and args.get("end_line") is None and not refused):
                    res_fp = self.trajectory.fingerprint(self._current(args["path"]))
                obs = ToolObservation(
                    tool=name, turn=self.current_turn, canonical_path=canon_path,
                    outcome="refused" if refused else "ok",
                    args_fingerprint=args_fp, result_fingerprint=res_fp)
            elif name == "grep":
                err = isinstance(out, str) and out.startswith("error:")
                covered_paths = []
                if not err:
                    for line in out.splitlines():
                        if ":" not in line:
                            continue
                        try:
                            covered_paths.append(self._resolve(line.split(":", 1)[0]))
                        except ValueError:
                            pass
                obs = ToolObservation(
                    tool=name, turn=self.current_turn, outcome="error" if err else "ok",
                    covered_paths=tuple(covered_paths), grep_all_paths=(not err and not args.get("glob")),
                    args_fingerprint=args_fp, result_fingerprint=self.trajectory.fingerprint(out))
            else:
                res_str = out.describe() if isinstance(out, ImageAsset) else str(out)
                obs = ToolObservation(
                    tool=name, turn=self.current_turn, canonical_path=canon_path, outcome="ok",
                    args_fingerprint=args_fp, result_fingerprint=self.trajectory.fingerprint(res_str))
            self._turn_suspicions.extend(self.trajectory.observe(obs))
        if isinstance(out, ImageAsset):
            return out      # never truncated; the loop delivers the pixels
        return out if len(out) <= MAX_RESULT_CHARS else \
            out[:MAX_RESULT_CHARS] + f"\n...[truncated at {MAX_RESULT_CHARS:,} chars]"


class Transcript:
    """Live, flushed-on-write log so a human can tail the run as it happens."""

    def __init__(self, path, header):
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self.path = path
        self.fh = open(path, "w", encoding="utf-8")
        self._last_flush = float("-inf")
        # Set once the run carries images: a model or gateway echo of a data
        # URL must never reach the transcript. Text-only runs are untouched.
        self.scrub_images = False
        # Streamed deltas can split a data URL anywhere; this holds a possible
        # header prefix and drops an encoded body across append() calls.
        self.redactor = DataUrlRedactor()
        self.write(header)

    def write(self, text):
        if self.scrub_images:
            text = self.redactor.finish(text)
        self.fh.write(text + "\n")
        self.flush()

    def append(self, text):
        """Write streamed text with no newline. It reaches the OS at once, so a
        reader such as the dashboard sees it; fsync runs at most every
        LIVE_FLUSH_SECONDS so a fast stream does not fsync per token."""
        if self.scrub_images:
            text = self.redactor.feed(text)
        self.fh.write(text)
        self.fh.flush()
        if _now() - self._last_flush >= LIVE_FLUSH_SECONDS:
            self.flush()

    def flush(self):
        self.fh.flush()
        os.fsync(self.fh.fileno())
        self._last_flush = _now()

    def start_step(self, i, narration, tool, args):
        if narration:
            self.write(f"\n### [{i}] thinking\n\n{narration.strip()}")
        if tool:
            brief = json.dumps(args)[:300]
            self.write(f"\n### [{i}] {tool}({brief})\n")
        print(f"  [{i:>2}] {tool or 'reply'}"
              f"{'  ' + json.dumps(args)[:70] if args else ''}", file=sys.stderr, flush=True)

    def step_result(self, result):
        self.write("```\n" + (result or "")[:1500] +
                   ("\n...[truncated in transcript]" if len(result or "") > 1500 else "") +
                   "\n```")

    def step(self, i, narration, tool, args, result):
        self.start_step(i, narration, tool, args)
        if tool:
            self.step_result(result)

    def close(self):
        if self.fh.closed:
            return
        if self.scrub_images:
            self.fh.write(self.redactor.finish(""))
        self.fh.close()


class LiveAnswer:
    """Writes a streamed one-shot into the transcript as it arrives.

    Reasoning goes into a "### [1] thinking" section inside <think>...</think>,
    which the narration view shows as reasoning; the answer goes under
    "## Final report". Each stays open while text arrives, so the dashboard
    shows it live; close() ends them and returns the answer.
    """

    def __init__(self, tr):
        self.tr = tr
        self.state = "idle"          # idle -> reasoning -> answer -> closed
        self.parts = []

    def reasoning(self, text):
        if not text or self.state not in ("idle", "reasoning"):
            return
        if self.state == "idle":
            self.tr.write("\n### [1] thinking\n\n<think>")
            self.state = "reasoning"
        self.tr.append(text)

    def answer_text(self, text):
        if not text or self.state == "closed":
            return
        if self.state == "reasoning":
            self.tr.write("\n</think>")
        if self.state != "answer":
            self.tr.write("\n---\n\n## Final report\n")
            self.state = "answer"
        self.tr.append(text)
        self.parts.append(text)

    def close(self):
        if self.state == "reasoning":
            self.tr.write("\n</think>")
        elif self.state == "answer":
            self.tr.write("")        # end the answer's last line
        self.tr.flush()
        self.state = "closed"
        return "".join(self.parts)


def responses_input(msgs):
    """The engine's chat-format history as Responses API input items (LA §3.1)."""
    items = []
    for m in msgs:
        role, content = m.get("role"), m.get("content") or ""
        if role == "tool":
            items.append({"type": "function_call_output", "call_id": m.get("tool_call_id"),
                          "output": content})
        elif role == "assistant":
            if content:
                items.append({"role": "assistant", "content": content})
            for c in m.get("tool_calls") or []:
                fn = c.get("function") or {}
                items.append({"type": "function_call", "call_id": c.get("id"),
                              "name": fn.get("name"),
                              "arguments": fn.get("arguments") or "{}"})
        elif isinstance(content, list):
            parts = []
            for p in content:
                if p.get("type") == "text":
                    parts.append({"type": "input_text", "text": p.get("text") or ""})
                elif p.get("type") == "image_url":
                    parts.append({"type": "input_image",
                                  "image_url": (p.get("image_url") or {}).get("url")})
            items.append({"role": role, "content": parts})
        else:
            items.append({"role": role, "content": content})
    return items


def responses_tools(tools):
    """native_tools() entries in the Responses API's flat function form."""
    return [{"type": "function", "name": t["function"]["name"],
             "description": t["function"]["description"],
             "parameters": t["function"]["parameters"]} for t in tools]


def _printable(text):
    # A lone surrogate from a broken \uXXXX escape cannot be written as UTF-8.
    return text.encode("utf-8", "replace").decode("utf-8")


class ArgFieldStream:
    """Decode one string field of a tool call's JSON arguments as they stream.

    feed(piece) returns the field's newly decoded text. An escape split across
    pieces (a lone backslash, a partial \\uXXXX, or a surrogate pair's first
    half) is held until complete. path holds the "path" argument once its
    string has closed. The key search is textual: the first "<field>": "
    in the arguments, which is the argument key in practice.
    """

    def __init__(self, field):
        self.key = re.compile(r'"%s"\s*:\s*"' % re.escape(field))
        self.buf = ""
        self.pos = None
        self.started = False
        self.done = False
        self.path = None

    def feed(self, piece):
        self.buf += piece or ""
        if self.path is None:
            m = _PATH_ARG.search(self.buf)
            if m:
                try:
                    self.path = json.loads('"' + m.group(1) + '"')
                except ValueError:
                    self.path = m.group(1)
        if self.done:
            return ""
        if self.pos is None:
            m = self.key.search(self.buf)
            if not m:
                return ""
            self.pos, self.started = m.end(), True
        buf, i, n = self.buf, self.pos, len(self.buf)
        end = None
        while i < n:
            ch = buf[i]
            if ch == '"':
                end = i
                break
            if ch != "\\":
                i += 1
                continue
            if i + 1 >= n:
                break
            if buf[i + 1] != "u":
                i += 2
                continue
            if i + 6 > n:
                break
            code = buf[i + 2:i + 6]
            if all(c in _HEX for c in code) and 0xD800 <= int(code, 16) <= 0xDBFF:
                if i + 6 >= n or (buf[i + 6] == "\\" and i + 7 >= n):
                    break                  # wait to see whether a pair follows
                if buf[i + 6:i + 8] == "\\u":
                    if i + 12 > n:
                        break
                    i += 12
                    continue
            i += 6
        segment = buf[self.pos:i]
        if end is not None:
            self.done, self.pos = True, end + 1
        else:
            self.pos = i
        try:
            text = json.loads('"' + segment + '"')
        except ValueError:
            text = segment
        return _printable(text)


class FenceHold:
    """Pass prompt-mode text through until the ```tool fence; hold the rest."""

    FENCE = "```tool"

    def __init__(self):
        self.carry = ""
        self.closed = False

    def feed(self, text):
        if self.closed:
            return ""
        buf = self.carry + (text or "")
        i = buf.find(self.FENCE)
        if i != -1:
            self.closed, self.carry = True, ""
            return buf[:i]
        keep = max((k for k in range(1, len(self.FENCE)) if buf.endswith(self.FENCE[:k])),
                   default=0)
        self.carry = buf[-keep:] if keep else ""
        return buf[:-keep] if keep else buf

    def flush(self):
        rest = "" if self.closed else self.carry
        self.carry = ""
        return rest


class LiveTurn:
    """Writes one streamed agent turn into the transcript as it arrives (LA §3.2).

    Reasoning goes inside <think>...</think> under "### [i] thinking", the note
    follows as plain text, and previewed tool text follows a composing line with
    each line prefixed "> " (a bare ">" when empty), so no line of a file can
    start a step, a final report, a status line or the footer. wrote_note tells
    the loop not to write the note again.
    """

    def __init__(self, tr, turn):
        self.tr = tr
        self.turn = turn
        self.state = "idle"        # idle | reasoning | note | preview | closed
        self.section = False       # a "### [i] thinking" section is open
        self.wrote_note = False
        self.pending = ""          # whitespace held until the note has real text
        self.line_open = False     # the current preview line has text
        self.preview_key = None    # the call that owns the open preview

    def _end(self):
        if self.state == "reasoning":
            self.tr.write("\n</think>")
        elif self.state == "note" or (self.state == "preview" and self.line_open):
            self.tr.write("")
        self.line_open = False
        self.preview_key = None
        self.state = "idle"

    def _open(self, first):
        if self.section:
            self.tr.write(first)
        else:
            self.tr.write(f"\n### [{self.turn}] thinking\n" + first)
            self.section = True

    def reasoning(self, text):
        if not text or self.state == "closed":
            return
        if self.state != "reasoning":
            self._end()
            self._open("\n<think>")
            self.state = "reasoning"
        self.tr.append(_printable(text))

    def note(self, text):
        if not text or self.state == "closed":
            return
        if self.state == "note":
            self.tr.append(_printable(text))
            return
        self.pending += text
        if not self.pending.strip():
            return
        text, self.pending = self.pending.lstrip(), ""
        self._end()
        self._open("")
        self.state = "note"
        self.wrote_note = True
        self.tr.append(_printable(text))

    def preview_start(self, tool, path):
        if self.state == "closed":
            return
        self._end()
        self.pending = ""
        path = _printable(" ".join(str(path).split())) if path else None
        tr, turn = self.tr, self.turn
        tr.write(f"\n_[{turn:>2}] composing {tool}{' ' + path if path else ''} ..._")
        self.section = False
        self.state = "preview"

    def preview_text(self, text):
        if self.state != "preview" or not text:
            return
        out = []
        for k, part in enumerate(_printable(text).replace("\r", "").split("\n")):
            if k:
                out.append("\n" if self.line_open else ">\n")
                self.line_open = False
            if part:
                if not self.line_open:
                    out.append("> ")
                    self.line_open = True
                out.append(part)
        self.tr.append("".join(out))

    def close(self):
        if self.state == "closed":
            return
        self._end()
        self.pending = ""
        self.tr.flush()
        self.state = "closed"


BASE_RULES_TEMPLATE = """You are a delegated engineering agent working in a repository.

Gather evidence with the tools before concluding anything; never guess file
contents or command output.

Budget your context deliberately. Files over {limit} characters cannot be read
whole and the attempt is refused: grep for what you need, then read_file with
start_line/end_line. Never re-read a file you have already read -- scroll back
instead. Running out of context kills the run and wastes all the work.
Use text tools first for code and repository evidence. Load images only when
visual evidence is needed; never use images as a substitute for reading code.

Edit with replace_in_file, not write_file, whenever the file already exists:
write_file needs the complete contents, which is impossible for a large file.

You cannot run arbitrary shell commands, only the allowlisted ones. A refused
command will be refused every time -- do not retry it, find another way.

Narrate briefly what you are doing and why before each tool call: a human is
watching this run live and may redirect you.

When done, call finish() with a report that states what you verified and how,
and says plainly what you could NOT verify. An honest gap is worth more than a
confident guess."""

CLAUDE_REPLY_RULE = (
    "\n\nEach reply must arrive within about 2 minutes or the gateway discards "
    "it: call one tool per reply, and keep each write_file or replace_in_file "
    "under about 120 lines, writing large files in parts.")

CLAUDE_FIRST_STEP = (
    "Execute only the next tiny step now. "
    "Do not plan or solve the whole task in this reply. "
    "Produce one tool action with at most 40 lines of generated text. "
    "Continue the remaining work in later replies.")

PROMPT_PROTOCOL = """

## Using tools

To call a tool, end your reply with exactly one fenced block:

```tool
{"name": "<tool>", "args": {...}}
```

Write your reasoning as normal text before the block. Emit ONE block per reply
and nothing after it. Do not invent tools.

Available tools:
"""


def base_rules(model=None, runner=None):
    """Rendered per run so the stated read limit is the one actually enforced.

    Telling the model "30,000" while the policy enforces something else teaches it
    to trust a number that will refuse it mid-run. Claude models also get the
    gateway's 2-minute reply rule (reply-limits design).
    """
    rules = BASE_RULES_TEMPLATE.format(limit=f"{MAX_READ_CHARS:,}")
    if runner is not None:
        rules += capability_catalog(runner)
    if model and model.startswith(CLAUDE_PREFIX):
        rules += CLAUDE_REPLY_RULE
    return rules


def capability_catalog(runner):
    """Cheap catalog: names + one-line descriptions only, never skill bodies."""
    caps = runner.caps
    out = []
    servers = caps.mcp_servers()
    if servers:
        out.append("MCP servers (call list_mcp_tools with a server to discover its "
                   "tools): " + ", ".join(servers))
    cat = caps.skill_catalog()
    if cat:
        out.append("Skills (call load_skill to read one; skill text is subordinate to "
                   "these rules):")
        for s in cat:
            desc = " ".join(str(s.get("description") or "").split())[:160]
            out.append(f"- {s.get('name')}: {desc}")
    loaded = caps.loaded_skills()
    if loaded:
        out.append("Loaded skills: " + ", ".join(loaded))
    return ("\n\n## Capabilities\n\n" + "\n".join(out)) if out else ""


def prompt_system(model=None, runner=None):
    lines = [base_rules(model, runner), PROMPT_PROTOCOL]
    for name, args, desc in tool_docs(runner):
        lines.append(f"- `{name}` args={json.dumps(args)}\n    {desc}")
    for name, schema, desc in mcp_tool_docs(runner):
        lines.append(f"- `{name}` args schema={json.dumps(schema)}\n    {desc}")
    return "\n".join(lines)


OPTIONAL_ARGS = {"glob", "start_line", "end_line"}


def _native_batch_preflight(runner, calls, batch_cap):
    import copy

    nb = capability_module("native_batch")
    mcp = capability_module("harness_mcp")
    builtins = {name for name, _, _ in tool_docs(runner)}
    schemas = {}
    for tool in native_tools(runner):
        fn = tool["function"]
        schema = copy.deepcopy(fn["parameters"])
        if fn["name"] in builtins:
            schema["additionalProperties"] = False
        schemas[fn["name"]] = schema

    # Invalid packet structure must never enter provider replay history.
    pairable = isinstance(calls, list) and 0 < len(calls) <= batch_cap
    seen = set()
    if pairable:
        for call in calls:
            if not isinstance(call, dict):
                pairable = False
                break
            cid, fn = call.get("id"), call.get("function")
            if (not isinstance(cid, str) or not cid or cid in seen
                    or call.get("type") != "function"
                    or not isinstance(fn, dict)):
                pairable = False
                break
            name = fn.get("name")
            if (not isinstance(name, str) or name not in schemas
                    or not isinstance(fn.get("arguments"), str)):
                pairable = False
                break
            seen.add(cid)

    def check_arguments(name, arguments):
        if name not in schemas:
            raise ValueError("unknown tool")
        schema = schemas[name]
        mcp.check_schema(schema)
        mcp.validate_args(arguments, schema)
        checked_tool_arguments(name, arguments)
        if name == "read_files":
            runner._paged.validate_read_files(arguments.get("requests"))
        if runner._paged is not None and name in ("list_dir", "grep"):
            runner._paged._paging(arguments.get("offset", 0), arguments.get("limit", 1))

    verdict = nb.validate_batch(
        calls, batch_cap=batch_cap, check_arguments=check_arguments)
    return verdict, pairable


def native_tools(runner=None):
    out = []
    for name, args, desc in tool_docs(runner):
        if name == "read_files":
            out.append({"type": "function", "function": {
                "name": name, "description": desc,
                "parameters": runner._paged.READ_FILES_SCHEMA}})
            continue
        if name in CAPABILITY_SCHEMAS:
            out.append({"type": "function", "function": {
                "name": name, "description": desc,
                "parameters": CAPABILITY_SCHEMAS[name]}})
            continue
        props, required = {}, []
        for k, v in args.items():
            if isinstance(v, list):
                props[k] = {"type": "array", "items": {"type": "string"}}
            elif isinstance(v, bool) or isinstance(v, int):
                props[k] = {"type": "integer"}
            else:
                props[k] = {"type": "string"}
            if name in ("read_command_output", "read_context_result") and k in ("offset", "limit"):
                props[k].update(minimum=0 if k == "offset" else 1,
                                maximum=COMMAND_PAGE_MAX_OFFSET if k == "offset"
                                else COMMAND_PAGE_BYTES)
            elif (runner is not None and runner.profile == "resilient-v1"
                    and name in ("list_dir", "grep") and k in ("offset", "limit")):
                props[k].update(minimum=0 if k == "offset" else 1,
                                maximum=runner._paged.MAX_OFFSET if k == "offset"
                                else runner._paged.MAX_LIMIT)
            elif k not in OPTIONAL_ARGS:
                required.append(k)
        out.append({"type": "function", "function": {
            "name": name, "description": desc,
            "parameters": {"type": "object", "properties": props, "required": required}}})
    for name, schema, desc in mcp_tool_docs(runner):
        out.append({"type": "function", "function": {
            "name": name, "description": desc, "parameters": schema}})
    return out


TOOL_BLOCK = re.compile(r"```tool\s*(\{.*?\})\s*```", re.S)
THINK_BLOCK = re.compile(r"<think>.*?</think>", re.S)

# Tool-call fields whose text is previewed while it streams (live agent turns, LA §3.2).
PREVIEW_FIELDS = {"write_file": "content", "replace_in_file": "new_string",
                  "finish": "summary"}
_PATH_ARG = re.compile(r'"path"\s*:\s*"((?:[^"\\]|\\.)*)"')
_HEX = set("0123456789abcdefABCDEF")


ERROR_DETAIL_MAX = 300


def error_detail(body, key=None):
    """Extract a short, safe error description from a gateway error body.

    Only the body's `detail` string or `error.message` string is used -- never
    the raw body, which a gateway may echo request content into. The text is
    collapsed to one line, the API key is redacted should it appear, and the
    result is capped at ERROR_DETAIL_MAX characters.
    """
    text = None
    try:
        data = json.loads(body)
    except (ValueError, TypeError):
        data = None
    if isinstance(data, dict):
        if isinstance(data.get("detail"), str):
            text = data["detail"]
        elif isinstance(data.get("error"), dict) and \
                isinstance(data["error"].get("message"), str):
            text = data["error"]["message"]
    if not text:
        return "(no detail or error.message in the error body)"
    text = scrub_data_urls(" ".join(text.split()))
    if key:
        text = text.replace(key, "[redacted]")
    return text[:ERROR_DETAIL_MAX]


def chat(url, key, payload):
    context_preflight(payload)
    req = urllib.request.Request(url + "/api/chat/completions",
                                 data=json.dumps(payload, default=_wire_default).encode(),
                                 method="POST")
    req.add_header("Authorization", "Bearer " + key)
    req.add_header("Content-Type", "application/json")
    started = request_evidence("chat", REQUEST_TIMEOUT)
    try:
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as r:
            result = json.loads(r.read())
            diagnostic("request_end", elapsed_s=time.monotonic() - started)
            return result
    except urllib.error.HTTPError as e:
        request_error("http_error", started, http_status=e.code)
        # Auth failures are terminal: retrying a bad/expired key is pointless,
        # so exit rather than burn the retry budget.
        if e.code in (401, 403):
            body = e.read()[:8000].decode(errors="replace")
            sys.exit(f"error: HTTP {e.code}: {error_detail(body, key)}")
        if e.code == 413:
            ce = _CAP_MODULES.get("context_engine")
            if ce is not None and ce.current() is not None:
                return {"_transient": "HTTP 413"}
            raise ContextBudgetError("provider context", provider=True)
        # A 400 "Model not found" means this request hit a gateway replica whose
        # model cache is stale. Flag it so chat_with_model_retry can refresh and
        # re-roll the replica instead of treating it as an ordinary transient.
        if e.code == 400:
            body = e.read()[:8000].decode(errors="replace")
            check_context_rejection(body)
            if "Model not found" in body:
                return {"_transient": "HTTP 400 model not found",
                        "_model_not_found": True}
            # Any other 400 is the gateway rejecting the request itself (an
            # unsupported parameter or value). Replaying the identical payload
            # can never succeed, so flag it fatal; chat_with_model_retry stops
            # the run with this short, sanitized detail. "_transient" stays for
            # callers that only test presence.
            return {"_transient": "HTTP 400", "_fatal": True, "_status": 400,
                    "_error_detail": error_detail(body, key)}
        check_context_rejection(e.read()[:8000].decode(errors="replace"))
        # Other HTTP errors (e.g. 5xx from a struggling gateway) are transient:
        # hand a sentinel back so call_model retries instead of dying. The error
        # arrived immediately, so do NOT claim it took REQUEST_TIMEOUT seconds.
        return {"_transient": f"HTTP {e.code}"}
    except (TimeoutError, socket.timeout):
        request_error("client_timeout", started)
        # Client-side read timeout. TimeoutError is NOT a subclass of URLError,
        # so it would otherwise propagate and kill the whole run. Note that the
        # same slow upstream can instead surface as a 200 whose content is a
        # bare "Error:" (str(TimeoutError()) is ""); advances() rejects that,
        # so both symptoms funnel into the identical retry path.
        return {"_transient": f"read timeout after {REQUEST_TIMEOUT}s"}
    except urllib.error.URLError as e:
        request_error("client_timeout" if isinstance(e.reason, TimeoutError) else
                      "transport_error", started)
        # Connection-level failure (DNS, refused, reset). Transient: retry.
        return {"_transient": f"cannot reach gateway: {e.reason}"}
    except (OSError, http.client.HTTPException) as e:
        request_error("disconnect" if isinstance(e, (ConnectionError, http.client.HTTPException))
                      else "transport_error", started)
        raise


STREAM_DONE = {"_done": True}


class StreamError(Exception):
    """A streaming request that failed.

    kind is "http" (the gateway answered with an HTTP error), "idle" (no line
    for STREAM_IDLE_TIMEOUT seconds) or "transport" (any other failure).
    events_seen counts the events received before it, so a caller can tell a
    stream that never started (and may fall back) from one that broke midway.
    usage carries any counters received before the stream broke.
    """

    def __init__(self, kind, detail, events_seen=0, usage=None, reason=None):
        super().__init__(f"{kind}: {detail}")
        self.kind = kind
        self.detail = detail
        self.events_seen = events_seen
        self.usage = usage
        self.reason = reason


def stream_events(url, key, path, payload, idle_timeout=None):
    """POST payload to url + path and yield each Server-Sent Event's JSON data.

    Lines other than "data: ..." are skipped, as are data lines that are not a
    JSON object. "data: [DONE]" yields STREAM_DONE and ends the stream. Raises
    StreamError; an auth failure exits like chat(). The key goes only into the
    Authorization header and is masked out of any error detail.
    """
    context_preflight(payload)
    timeout = STREAM_IDLE_TIMEOUT if idle_timeout is None else idle_timeout
    req = urllib.request.Request(url + path,
                                 data=json.dumps(payload, default=_wire_default).encode(),
                                 method="POST")
    req.add_header("Authorization", "Bearer " + key)
    req.add_header("Content-Type", "application/json")
    req.add_header("Accept", "text/event-stream")
    seen = 0
    last_event = _now()
    started = request_evidence("responses" if path == "/openai/responses" else "chat", timeout)
    last_report, last_activity = started, None
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            diagnostic("stream_open", phase="stream")
            for raw in r:
                if _now() - last_event > timeout:
                    request_error("stream_idle", started, events_seen=seen)
                    raise StreamError("idle", f"no event for {timeout}s", seen)
                try:
                    line = raw.decode("utf-8").strip()
                except UnicodeDecodeError:
                    continue
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    diagnostic("request_end", events_seen=seen, elapsed_s=time.monotonic() - started)
                    yield STREAM_DONE
                    return
                try:
                    obj = json.loads(data)
                except ValueError:
                    continue
                if isinstance(obj, dict):
                    seen += 1
                    last_event = _now()
                    kind = obj.get("type", "")
                    activity = ("reasoning" if isinstance(kind, str) and "reasoning" in kind else
                                "answer" if kind == "response.output_text.delta" else
                                "tool" if kind == "response.function_call_arguments.delta" else
                                "completed" if kind == "response.completed" else "other")
                    choices = obj.get("choices")
                    if isinstance(choices, list):
                        for choice in choices:
                            delta = choice.get("delta") if isinstance(choice, dict) else None
                            if isinstance(delta, dict):
                                if delta.get("reasoning_content") or delta.get("reasoning"):
                                    activity = "reasoning"
                                elif delta.get("tool_calls"):
                                    activity = "tool"
                                elif delta.get("content"):
                                    activity = "answer"
                    now = time.monotonic()
                    if activity != last_activity or now - last_report >= 10:
                        diagnostic("stream_progress", activity=activity, events_seen=seen,
                                   elapsed_s=now - started, phase="stream")
                        last_report, last_activity = now, activity
                    yield obj
            diagnostic("request_end", events_seen=seen, elapsed_s=time.monotonic() - started)
    except urllib.error.HTTPError as e:
        request_error("http_error", started, http_status=e.code, events_seen=seen)
        body = e.read()[:8000].decode(errors="replace")
        detail = error_detail(body, key)
        check_context_rejection(detail)
        if e.code in (401, 403):
            sys.exit(f"error: HTTP {e.code}: {detail}")
        raise StreamError("http", f"HTTP {e.code}: {detail}", seen)
    except (TimeoutError, socket.timeout):
        request_error("stream_idle", started, events_seen=seen)
        raise StreamError("idle", f"no event for {timeout}s", seen)
    except urllib.error.URLError as e:
        request_error("stream_idle" if isinstance(e.reason, TimeoutError) else
                      "transport_error", started, events_seen=seen)
        raise StreamError("transport", f"cannot reach gateway: {type(e.reason).__name__}", seen)
    except (OSError, http.client.HTTPException) as e:
        request_error("disconnect" if isinstance(e, (ConnectionError, http.client.HTTPException))
                      else "transport_error", started, events_seen=seen)
        raise StreamError("transport", type(e).__name__, seen)


def refresh_models(url, key):
    # GET /api/models?refresh=true forces the gateway replica to rebuild its
    # model cache. Measured: after a refresh the new models show up 10/10 times.
    # Returns the set of model ids, or None on ANY failure -- it never raises,
    # because it runs on the recovery path and must not add a new way to crash.
    req = urllib.request.Request(url + "/api/models?refresh=true", method="GET")
    req.add_header("Authorization", "Bearer " + key)
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            data = json.loads(r.read())
    except Exception:
        return None
    try:
        items = data.get("data") if isinstance(data, dict) else data
        return {m["id"] for m in items if isinstance(m, dict) and "id" in m}
    except Exception:
        return None


def chat_with_model_retry(url, key, payload, turn, tries=6, sleep=time.sleep):
    # The gateway load-balances across replicas with no affinity; some hold a
    # stale model cache and answer 400 "Model not found". A fresh ?refresh=true
    # plus a quick retry re-rolls onto a good replica. Loop until a non-404-ish
    # reply or the retry budget is spent.
    def stop_if_fatal(resp):
        # A request the gateway rejects outright (HTTP 400 other than the stale
        # replica case) fails identically on every retry: stop now, saying why.
        if isinstance(resp, dict) and (resp.get("_fatal") or resp.get("_transient")):
            check_context_rejection(resp.get("_error_detail") or resp.get("_transient"))
        if isinstance(resp, dict) and resp.get("_fatal"):
            n_img = len(history_images(payload.get("messages") or []))
            img_note = (f" The request carried {n_img} image(s); {payload.get('model')} "
                        "may not accept image input. Not retrying without them."
                        if n_img else "")
            sys.exit(f"error: gateway rejected the request for "
                     f"{payload.get('model')} with HTTP {resp.get('_status', 400)}: "
                     f"{resp.get('_error_detail') or '(no detail)'}\n"
                     f"Not retrying: the same request would fail the same way.{img_note}")

    context_preflight(payload)
    resp = chat(url, key, payload)
    stop_if_fatal(resp)
    k = 0
    while isinstance(resp, dict) and resp.get("_model_not_found") and k < tries:
        k += 1
        refresh_models(url, key)
        print(f"  [{turn:>2}] model not found on this gateway replica "
              f"(stale model cache); refreshed, retrying {k}/{tries}",
              file=sys.stderr, flush=True)
        sleep(random.uniform(0.5, 2.0))
        context_preflight(payload)
        resp = chat(url, key, payload)
        stop_if_fatal(resp)
    if isinstance(resp, dict) and resp.get("_model_not_found"):
        model = payload.get("model")
        sys.exit(f"error: gateway kept answering 'Model not found' for {model} "
                 f"after {tries} retries; its replicas have stale model caches "
                 f"(try again, or pass another -m)")
    return resp


def first_message(resp):
    """(choice, message) of a well-formed Chat envelope, else None. Never indexes blindly."""
    choices = resp.get("choices") if isinstance(resp, dict) else None
    choice = choices[0] if isinstance(choices, list) and choices else None
    msg = choice.get("message") if isinstance(choice, dict) else None
    return (choice, msg) if isinstance(msg, dict) else None


def classify_failure(resp, mode, elapsed):
    """Name why a reply did not advance the loop (see the reply-limits design).

    reply_cut      a bare "Error:" that took REPLY_CUT_SECONDS or longer: the
                   gateway cut a Claude reply at its ~121 s limit
    prose_only     native mode: text but no tool call
    no_tool_block  prompt mode: text but no tool block
    timeout        the client read timeout (REQUEST_TIMEOUT)
    transport      any other transport failure
    empty          anything else: empty content, or a quick bare "Error:"
    """
    if isinstance(resp, dict) and "_transient" in resp:
        detail = str(resp.get("_transient") or "")
        return "timeout" if detail.startswith("read timeout") else "transport"
    msg = (first_message(resp) or (None, {}))[1]
    content = msg.get("content") if isinstance(msg.get("content"), str) else ""
    if BARE_ERROR.fullmatch(content):
        return "reply_cut" if elapsed >= REPLY_CUT_SECONDS else "empty"
    if content.strip() and not msg.get("tool_calls"):
        if mode == "native":
            return "prose_only"
        if mode == "prompt":
            return "no_tool_block"
    return "empty"


def advances(resp, mode):
    # True when the reply gives the loop something to act on. WHY this exists:
    # "no tool calls" must never again be conflated with "the model is done".
    # A reply with no tool_calls (native) or no tool block (prompt) used to be
    # accepted as the final answer, so a gateway hiccup that returned a bare
    # "Error:" with no tool_calls got reported as a run's result. finish is
    # itself a tool, so a genuine completion still advances the loop.
    # A transient transport failure (see chat) is signalled by the sentinel
    # dict, which has no "choices"; it never advances, so call_model retries.
    # Test key PRESENCE, not truthiness: an empty message ({"_transient": ""})
    # is still a transport failure, and a truthiness test would fall through to
    # resp["choices"] and reintroduce the KeyError this sentinel exists to
    # prevent. The isinstance guard keeps a None or list from raising here.
    if isinstance(resp, dict) and "_transient" in resp:
        return False
    found = first_message(resp)
    if found is None:
        return False                   # malformed envelope: never act on its tools
    msg = found[1]
    if mode == "oneshot":
        # One request, no tools: the answer itself is the result. An empty or
        # bare "Error:" reply is the same gateway hiccup as above, so retry it.
        content = msg.get("content") if isinstance(msg.get("content"), str) else ""
        return bool(content.strip()) and not BARE_ERROR.fullmatch(content)
    if mode == "native":
        return bool(msg.get("tool_calls"))
    return TOOL_BLOCK.search(msg.get("content") or "") is not None


def _log_image_anomaly(anomaly_path, turn, attempt, mode, payload, resp, cause,
                       elapsed, request_bytes, images):
    """Anomaly record for a request carrying images: allowlisted diagnostic
    fields only -- no response body, request echo or content."""
    is_transient = isinstance(resp, dict) and "_transient" in resp
    rec = {
        "turn": turn, "attempt": attempt, "mode": mode, "model": payload.get("model"),
        "cause": cause or ("transport" if is_transient else "no_advance"),
        "transient": scrub_data_urls(resp.get("_transient")) if is_transient else None,
        "elapsed_s": round(elapsed, 1) if elapsed is not None else None,
        "messages_in_request": len(payload.get("messages") or payload.get("input") or []),
        "request_bytes": request_bytes,
        "images": [a.meta() for a in images],
    }
    if isinstance(resp, dict) and not is_transient:
        choice, msg = first_message(resp) or ({}, {})
        rec["response_id"] = resp.get("id") if isinstance(resp.get("id"), str) else None
        rec["finish_reason"] = choice.get("finish_reason")
        content = msg.get("content")
        rec["content_chars"] = len(content) if isinstance(content, str) else None
        rec["tool_calls"] = len(msg.get("tool_calls") or [])
    with open(anomaly_path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec) + "\n")


def log_anomaly(anomaly_path, turn, attempt, mode, payload, resp, cause=None, elapsed=None):
    # One JSON object per line, appended. NOTE: the gateway returns usage as
    # null, so we deliberately do NOT rely on token counts to detect anomalies;
    # the signal is purely "the reply did not advance the loop".
    request_bytes = _payload_bytes(payload)
    images = history_images(payload.get("messages") or [])
    if not images and isinstance(payload.get("input"), list):
        images = [p.get("image_url") for it in payload["input"] if isinstance(it, dict)
                  for p in (it.get("content") if isinstance(it.get("content"), list) else [])
                  if isinstance(p, dict) and isinstance(p.get("image_url"), ImageAsset)]
    if images:
        _log_image_anomaly(anomaly_path, turn, attempt, mode, payload, resp, cause,
                           elapsed, request_bytes, images)
        return
    # Classify by key PRESENCE, not truthiness: a falsey sentinel such as
    # {"_transient": ""} is still a transport failure and has no "choices".
    # A truthiness test would mislabel it "no_advance" and then index it as a
    # normal response, reintroducing the KeyError the sentinel exists to prevent.
    is_transient = isinstance(resp, dict) and "_transient" in resp
    rec = {
        "turn": turn,
        "attempt": attempt,
        "mode": mode,
        "model": payload.get("model"),
        # "cause" names the classify_failure() causes: reply_cut, prose_only,
        # no_tool_block, timeout, transport, or empty. Direct callers that omit
        # it keep the legacy transport/no_advance classification.
        "cause": cause or ("transport" if is_transient else "no_advance"),
        "transient": "transport" if is_transient else None,
        "elapsed_s": round(elapsed, 1) if elapsed is not None else None,
        "messages_in_request": len(payload.get("messages") or []),
        "request_bytes": request_bytes,
        # An anomaly is metadata, not a second copy of a provider response.
        "response": None,
        "usage": sanitize_usage(resp.get("usage") if isinstance(resp, dict) else None),
    }
    found = None if is_transient else first_message(resp)
    if not is_transient and found is None:
        # Malformed envelope: allowlisted shape only, never the raw body.
        rec.update(cause=cause or "malformed", response=None,
                   response_type=type(resp).__name__)
    elif found is not None:
        choice, msg = found
        # top-level "id" is the gateway's correlation id -- the only handle for
        # reporting this upstream.
        response_id = resp.get("id")
        rec["response_id"] = (response_id if isinstance(response_id, str)
                              and re.fullmatch(r"[A-Za-z0-9_-]{1,128}", response_id) else None)
        finish_reason = choice.get("finish_reason")
        rec["finish_reason"] = (finish_reason if finish_reason in
                                ("stop", "length", "tool_calls", "content_filter", "max_tokens",
                                 "max_output_tokens") else "unknown")
        rec["content"] = None
        calls = msg.get("tool_calls")
        rec["tool_calls"] = len(calls) if isinstance(calls, list) else None
    with open(anomaly_path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec) + "\n")


def call_model(url, key, payload, mode, tr, anomaly_path, turn, streaming=None, max_attempts=4,
               *, _maintenance=False):
    ce = _CAP_MODULES.get("context_engine")
    if ce is None or ce.current_runtime() is None:
        return _call_model_impl(
            url, key, payload, mode, tr, anomaly_path, turn, streaming, max_attempts)
    phase = "compaction" if _maintenance else "report" if mode == "oneshot" else "worker"
    with ce.request_scope(phase) as ctx:
        if _maintenance:
            # Maintenance is one same-wire RPC, never a protocol/effort/route repair.
            context_preflight(payload)
            if streaming is not None and streaming.route:
                live = LiveTurn(ce.DiscardTranscript(), turn)
                try:
                    resp = stream_turn(url, key, payload, streaming.route, live, mode, turn, tries=0)
                finally:
                    live.close()
            else:
                resp = chat(url, key, payload)
            result = resp, advances(resp, "oneshot"), None, 0
        else:
            result = _call_model_impl(
                url, key, copy.deepcopy(payload), mode, tr, anomaly_path,
                turn, streaming, max_attempts)
        resp = result[0]
        ctx.observe(resp.get("usage") if isinstance(resp, dict) else None)
        return result


def _call_model_impl(url, key, payload, mode, tr, anomaly_path, turn, streaming=None, max_attempts=4):
    """Send one turn's request, retrying only where a retry can help.

    Returns (resp, ok, cause, elapsed): cause is None when ok, else the
    classify_failure() name of the last failed attempt; elapsed is that
    attempt's wall time in seconds. "reply_cut", "prose_only" and
    "no_tool_block" return after ONE attempt: resending the identical request
    repeats them, so the caller responds to the cause instead (reply-limits design).
    """
    # Jitter matters because several agents run in parallel via fanout.py; a
    # fixed backoff would make them all retry in lockstep and re-hammer a
    # struggling gateway at the same instants.
    backoffs = [2, 8, 20]      # seconds before attempts 2, 3, 4
    resp, cause, elapsed = None, None, 0.0
    timeouts = 0
    earlier_usage = []

    def chat_attempt():
        try:
            return chat_with_model_retry(url, key, payload, turn)
        except BaseException as exc:
            exc._earlier_usage = list(earlier_usage)
            raise

    for attempt in range(1, max_attempts + 1):
        # Pre-call heartbeat: the transcript is otherwise only written AFTER a
        # reply arrives, so a legitimately slow xhigh call is indistinguishable
        # from a wedged one. Emit one short line to transcript AND stderr the
        # instant a call (or retry) starts, so a working-but-slow agent stays
        # visible. This fires per attempt, so retries are seen as they happen.
        try:
            context_preflight(payload)
        except ContextBudgetError as exc:
            exc._earlier_usage = list(earlier_usage)
            raise
        req_bytes = _payload_bytes(payload)
        n_msgs = len(payload.get("messages") or [])
        hb = (f"[{turn:>2}] calling {payload.get('model')} "
              f"(attempt {attempt}/{max_attempts}, {n_msgs} msgs, ~{req_bytes} bytes) ...")
        if tr is not None:
            tr.write(f"\n_{hb}_")
        print("  " + hb, file=sys.stderr, flush=True)
        started = _now()
        route = streaming.route if streaming is not None else None
        if route:
            live = LiveTurn(tr, turn)
            try:
                resp = stream_turn(url, key, payload, route, live, mode, turn)
            except ContextBudgetError as exc:
                exc._earlier_usage = [*earlier_usage, getattr(exc, "usage", None)]
                raise
            except StreamError as exc:
                try:
                    check_context_rejection(exc.detail)
                except ContextBudgetError as rejected:
                    rejected._earlier_usage = [*earlier_usage, exc.usage]
                    raise
                if exc.reason:
                    diagnostic("request_error", reason=exc.reason, events_seen=exc.events_seen)
                status = _http_status(exc) if exc.kind == "http" else None
                if exc.reason == "response_incomplete":
                    log_stream_failure(anomaly_path, attempt, payload, exc, _now() - started)
                    fatal = SystemExit(1)
                    fatal._stream_error = exc
                    fatal._earlier_usage = [*earlier_usage, exc.usage]
                    fatal._usage_dialect = "responses"
                    raise fatal from exc
                if status == 413:
                    log_stream_failure(anomaly_path, attempt, payload, exc, _now() - started)
                    fatal = SystemExit(3)
                    fatal._transport_rejected = True
                    fatal._earlier_usage = [*earlier_usage, exc.usage]
                    raise fatal from exc  # transport rejection cannot benefit from replay
                if (exc.events_seen == 0 and status is not None and 400 <= status < 500
                        and status not in (408, 429)):
                    # The rejected stream is prior only when dispatching the fallback.
                    earlier_usage.append(exc.usage)
                    streaming.route = None
                    route = None
                    note = (f"**streaming unavailable (HTTP {status}); "
                            "continuing without live view**")
                    tr.write("\n" + note)
                    print(f"  [{turn:>2}] {note.strip('*')}", file=sys.stderr, flush=True)
                    resp = chat_attempt()
                elif exc.kind == "idle":
                    resp = {"_transient": f"read timeout after {REQUEST_TIMEOUT}s (stream idle)",
                            "usage": exc.usage}
                else:
                    resp = {"_transient": f"stream {exc.kind}: {exc.detail}", "usage": exc.usage}
                    if status in (408, 429) and exc.events_seen == 0 and exc.usage is None:
                        # Gateway refused before any event or usage: no generation to record.
                        resp["_no_generation"] = True
            finally:
                live.close()
        else:
            resp = chat_attempt()
        if isinstance(resp, dict) and resp.get("_transient") == "HTTP 413":
            # chat() returns this only under model-v1; replay cannot shrink the payload.
            log_anomaly(anomaly_path, turn, attempt, mode, payload, resp,
                        cause="transport", elapsed=_now() - started)
            fatal = SystemExit(3)
            fatal._transport_rejected = True
            fatal._earlier_usage = [*earlier_usage, None]
            raise fatal
        if earlier_usage and isinstance(resp, dict):
            resp["_earlier_usage"] = list(earlier_usage)
        elapsed = _now() - started
        if (route == "chat" and elapsed >= REPLY_CUT_SECONDS and isinstance(resp, dict)
                and "_transient" not in resp and not complete_tool_action(resp, mode)):
            # A streamed Claude reply ends like a complete one when the gateway
            # cuts it at ~121 s; only the unfinished tool action shows it (LA §3.3).
            cause = "reply_cut"
            log_anomaly(anomaly_path, turn, attempt, mode, payload, resp,
                        cause=cause, elapsed=elapsed)
            print(f"  [{turn:>2}] anomaly: reply carried no tool action "
                  f"(attempt {attempt}/{max_attempts})", file=sys.stderr, flush=True)
            return resp, False, cause, elapsed
        if advances(resp, mode):
            return resp, True, None, elapsed
        cause = classify_failure(resp, mode, elapsed)
        log_anomaly(anomaly_path, turn, attempt, mode, payload, resp,
                    cause=cause, elapsed=elapsed)
        # Presence, not truthiness: a falsey sentinel ({"_transient": ""}) is
        # still a transport failure, so classify it as one for the log message.
        is_transient = isinstance(resp, dict) and "_transient" in resp
        transient = resp.get("_transient") if isinstance(resp, dict) else None
        reason = (f"transport failure ({transient})" if is_transient
                  else "reply carried no tool action")
        print(f"  [{turn:>2}] anomaly: {reason} "
              f"(attempt {attempt}/{max_attempts})", file=sys.stderr, flush=True)
        if cause in ("reply_cut", "prose_only", "no_tool_block"):
            return resp, False, cause, elapsed
        if cause == "timeout" and attempt < max_attempts:
            timeouts += 1
            if timeouts >= 2:
                return resp, False, cause, elapsed
            lowered = lower_effort_after_timeout(payload)
            if lowered is not None:
                payload = lowered
                note = (f"[{turn:>2}] no reply within {REQUEST_TIMEOUT} s at max; "
                        "retrying at xhigh")
                if tr is not None:
                    tr.write(f"\n_{note}_")
                print("  " + note, file=sys.stderr, flush=True)
        if attempt < max_attempts:
            # Only a request followed by another attempt belongs in prior history.
            usage = resp.get("usage") if isinstance(resp, dict) else None
            if not (isinstance(resp, dict) and resp.get("_no_generation")):
                earlier_usage.append(usage if isinstance(usage, dict) else None)
            delay = backoffs[attempt - 1] * random.uniform(0.75, 1.25)
            time.sleep(delay)
    if earlier_usage and not isinstance(resp, dict):
        # A malformed terminal body cannot carry the earlier attempts; keep metadata only.
        resp = {"usage": None, "_earlier_usage": list(earlier_usage)}
    return resp, False, cause, elapsed


def read_task_file(path, stdin=None):
    """Return the task text from PATH, decoding by BOM and normalizing newlines.

    PATH "-" reads bytes from STDIN (default sys.stdin.buffer). A UTF-8 BOM
    selects utf-8-sig, a UTF-16 BOM selects utf-16, anything else is strict
    utf-8. OSError from a real file propagates; a decode failure or an
    empty/whitespace-only result raises ValueError.
    """
    if path == "-":
        if stdin is None:
            stdin = sys.stdin.buffer
        raw = stdin.read()
    else:
        with open(path, "rb") as fh:
            raw = fh.read()
    if raw.startswith(b"\xef\xbb\xbf"):
        encoding = "utf-8-sig"
    elif raw.startswith(b"\xff\xfe") or raw.startswith(b"\xfe\xff"):
        encoding = "utf-16"
    else:
        encoding = "utf-8"
    try:
        text = raw.decode(encoding)
    except UnicodeDecodeError:
        raise ValueError("task file is not UTF-8 or UTF-16 text")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    if not text.strip():
        raise ValueError("task file is empty")
    return text


CLAUDE_PREFIX = "anthropic_tool_call."

# Per-model effort values the gateway rejects, mapped to the nearest accepted
# one. Measured: gpt-6.1-sol answers 400 to "none" (it supports low..max only).
_EFFORT_SUBSTITUTES = {"gpt-6.1-sol": {"none": "low"}}


def auto_tool_mode(model):
    """The tool protocol --tool-mode auto picks: native for Claude and gpt-*,
    else prompt."""
    if model.startswith(CLAUDE_PREFIX) or model.startswith("gpt-"):
        return "native"
    return "prompt"


def effective_effort(model, effort):
    """Return (effort to send, substituted?) after _EFFORT_SUBSTITUTES."""
    sub = _EFFORT_SUBSTITUTES.get(model, {}).get(effort)
    return (sub, True) if sub is not None else (effort, False)


def apply_effort(payload, model, effort):
    """Put the effort into a request payload in the form the model accepts.

    anthropic_tool_call.* keeps the flat `reasoning_effort` (omitted for none,
    as before). Every other model gets the nested `reasoning.effort`: the
    gateway now rejects the flat parameter for them outright. "none" is sent
    explicitly there, since those models accept it as a value.
    """
    if model.startswith(CLAUDE_PREFIX):
        if effort != "none":
            payload["reasoning_effort"] = effort
    else:
        payload["reasoning"] = {"effort": effort}
    return payload


def lower_effort_after_timeout(payload):
    """A copy of payload at xhigh when it asked for max, else None.

    On 2026-10-02 gpt-6.1-sol at max went silent past the 600 s client timeout
    on open-ended questions, while xhigh answered in 4 minutes.
    """
    reasoning = payload.get("reasoning")
    if isinstance(reasoning, dict) and reasoning.get("effort") == "max":
        return {**payload, "reasoning": {**reasoning, "effort": "xhigh"}}
    if payload.get("reasoning_effort") == "max":
        return {**payload, "reasoning_effort": "xhigh"}
    return None


ONESHOT_MAX_CHARS = 600_000
ONESHOT_SHORTER = ("\n\nAnswer in under about 1,500 words; the gateway discards "
                   "replies that take longer than 2 minutes.")


def oneshot_stream_route(model):
    """The streaming route a one-shot takes, or None for the non-streaming path."""
    if model.startswith("gpt-"):
        return "responses"
    if model.startswith(CLAUDE_PREFIX):
        return "chat"
    return None


class TurnStreaming:
    """Per-run streaming state for agent turns. route is None once the gateway
    rejected the streamed form (LA §3.3), or when streaming is off.
    """

    def __init__(self, route):
        self.route = route


def agent_stream_route(model, mode, no_stream):
    """The streaming route an agent turn takes, or None for today's request."""
    if no_stream:
        return None
    if model.startswith("gpt-") and mode == "native":
        return "responses"
    if model.startswith(CLAUDE_PREFIX) and mode in ("native", "prompt"):
        return "chat"
    return None


def provider_profile(model, route, effort):
    """Closed local evidence for the requested identity; observations are not maxima."""
    gpt = model == "gpt-6.1-sol" and route in ("chat", "responses")
    claude = model == "anthropic_tool_call.claude-opus-5-5-think" and route == "chat"
    evidence_effort = "high" if gpt else "medium" if claude else None
    match = evidence_effort is not None and effort == evidence_effort
    return (
        ("profile_id", "gpt_6_1_sol" if gpt else "claude_opus_5_5_think" if claude else "unprofiled"),
        ("route", route if route in ("chat", "responses") else "unknown"),
        ("effective_effort", effort if effort in ("none", "low", "medium", "high", "xhigh", "max") else "unknown"),
        ("configured_operating_window", 512000 if gpt else 1000000 if claude else None),
        ("observed_retained_input_lower_bound", (513757 if route == "chat" else 513746)
         if gpt else 994948 if claude else None),
        ("declared_max", 1000000 if claude else None),
        ("evidence_effort", evidence_effort),
        ("evidence_source", "gpt_retention_20261102" if gpt else "claude_retention_20261102" if claude else None),
        ("evidence_match", match),
        ("capacity_evidence_state", "observed" if match else "scope_mismatch" if gpt or claude else "unverified"),
        ("cap_support", "observed" if gpt else "unsupported" if claude else "unverified"),
        ("cap_evidence_state", "numeric_only" if gpt else "ignored" if claude else "unverified"),
    )


def responses_payload(payload):
    """A chat-completions agent payload as a streamed Responses request."""
    body = {"model": payload["model"], "input": responses_input(payload.get("messages") or []),
            "stream": True,
            "reasoning": {**(payload.get("reasoning") or {}), "summary": "auto"}}
    if payload.get("tools"):
        body["tools"] = responses_tools(payload["tools"])
        body["tool_choice"] = "auto"
    if payload.get("temperature") is not None:
        body["temperature"] = payload["temperature"]
    if payload.get("max_completion_tokens") is not None:
        body["max_output_tokens"] = payload["max_completion_tokens"]
    return body


def _http_status(exc):
    m = re.match(r"HTTP (\d{3})", getattr(exc, "detail", "") or "")
    return int(m.group(1)) if m else None


def _preview_feed(live, call_key, entry, piece):
    tool, fs = entry
    was = fs.started
    text = fs.feed(piece)
    if fs.started and (not was or (text and (live.state != "preview" or
                                           live.preview_key != call_key))):
        live.preview_start(tool, fs.path)
        live.preview_key = call_key
    if text:
        live.preview_text(text)


def _keep_usage(events, got):
    """Yield events; a StreamError raised mid-stream carries usage already put in got["usage"]."""
    try:
        yield from events
    except StreamError as exc:
        if exc.usage is None and isinstance(got.get("usage"), dict):
            exc.usage = got["usage"]
        raise


def _gpt_terminal_event(ev, got, seen):
    kind = ev.get("type")
    if kind not in ("response.completed", "response.incomplete", "response.failed", "error"):
        return
    response = ev.get("response")
    response = response if isinstance(response, dict) else {}
    usage = response.get("usage")
    if not isinstance(usage, dict):
        usage = ev.get("usage")
    if isinstance(usage, dict):
        got["usage"] = usage
    if kind == "response.incomplete":
        details = response.get("incomplete_details")
        reason = details.get("reason") if isinstance(details, dict) else None
        if reason not in ("max_output_tokens", "content_filter"):
            reason = "unknown"
        raise StreamError("transport", f"response_incomplete ({reason})", seen,
                          usage=got.get("usage"), reason="response_incomplete")
    if kind in ("response.failed", "error"):
        try:
            check_context_rejection(ev)
        except ContextBudgetError as exc:
            exc.usage = got.get("usage")
            raise
        raise StreamError("transport", "the gateway reported a failed response", seen,
                          usage=got.get("usage"), reason="gateway_failed")


def stream_gpt_turn(url, key, payload, live):
    """One GPT agent turn through /openai/responses, written live (LA §3.1).

    Returns a chat-shaped reply: content is the output text, tool_calls come
    from function_call items in order, usage from response.completed. Raises
    StreamError when the gateway reports a failure or the stream ends before
    response.completed.
    """
    context_preflight(payload)
    text, calls, previews, output_indexes = [], [], {}, {}
    usage, completed, summary_deltas, seen = None, False, False, 0
    got = {}
    for ev in _keep_usage(stream_events(url, key, "/openai/responses", payload,
                                        idle_timeout=REQUEST_TIMEOUT), got):
        if ev.get("_done"):
            continue
        seen += 1
        _gpt_terminal_event(ev, got, seen)
        kind = ev.get("type") or ""
        if kind == "response.reasoning_summary_text.delta":
            summary_deltas = True
            live.reasoning(ev.get("delta") or "")
        elif kind == "response.reasoning_summary_part.done" and summary_deltas:
            live.reasoning("\n\n")
        elif kind == "response.output_text.delta":
            piece = ev.get("delta") or ""
            text.append(piece)
            live.note(piece)
        elif kind == "response.output_item.added":
            item = ev.get("item") or {}
            if item.get("id") and isinstance(ev.get("output_index"), int):
                output_indexes[item["id"]] = ev["output_index"]
            if item.get("type") == "function_call" and item.get("name") in PREVIEW_FIELDS:
                previews[item.get("id")] = (item["name"],
                                            ArgFieldStream(PREVIEW_FIELDS[item["name"]]))
        elif kind == "response.function_call_arguments.delta":
            item_id = ev.get("item_id")
            entry = previews.get(item_id)
            if entry:
                _preview_feed(live, item_id, entry, ev.get("delta") or "")
        elif kind == "response.output_item.done":
            item = ev.get("item") or {}
            if item.get("type") == "function_call":
                index = ev.get("output_index", output_indexes.get(item.get("id")))
                arrival = len(calls)
                calls.append((index if isinstance(index, int) else arrival, arrival, item))
            elif item.get("type") == "reasoning" and not summary_deltas:
                live.reasoning("\n\n".join(s.get("text") for s in item.get("summary") or []
                                           if isinstance(s, dict) and s.get("text")))
        elif kind == "response.completed":
            completed = True
            usage = got.get("usage")
    if not completed:
        raise StreamError("transport", "the stream ended before the response completed", seen,
                          reason="stream_incomplete")
    msg = {"role": "assistant", "content": "".join(text)}
    if calls:
        msg["tool_calls"] = [{"id": it.get("call_id"), "type": "function",
                              "function": {"name": it.get("name"),
                                           "arguments": it.get("arguments") or "{}"}}
                             for _, _, it in sorted(calls, key=lambda c: c[:2])]
    return {"choices": [{"message": msg, "finish_reason": "stop"}], "usage": usage}


def stream_claude_turn(url, key, payload, live, mode):
    """One Claude agent turn through chat streaming, written live (LA §3.1).

    Content goes through ThinkSplitter (Claude's <think></think> arrives
    empty) and, in prompt mode, through FenceHold so the tool block is not
    written live. Tool calls are assembled by index (missing index = 0).
    finish_reason is "stop" even for a complete tool call, so the caller
    judges a cut by complete_tool_action and elapsed time.
    """
    context_preflight(payload)
    split, hold = ThinkSplitter(), (FenceHold() if mode == "prompt" else None)
    content, slots, previews = [], {}, {}
    usage, done, seen, finish = None, False, 0, None

    def show(pieces):
        for kind, text in pieces:
            if kind == "reasoning":
                live.reasoning(text)
            else:
                live.note(hold.feed(text) if hold else text)

    try:
        for ev in stream_events(url, key, "/api/chat/completions", payload,
                                idle_timeout=REQUEST_TIMEOUT):
            if ev.get("_done"):
                done = True
                continue
            seen += 1
            if isinstance(ev.get("usage"), dict):
                usage = ev["usage"]
            for ch in (ev.get("choices") or [])[:1]:
                finish = ch.get("finish_reason") or finish
                delta = ch.get("delta") or {}
                piece = delta.get("content")
                if isinstance(piece, str) and piece:
                    content.append(piece)
                    show(split.feed(piece))
                for tc in delta.get("tool_calls") or []:
                    idx = tc.get("index") if isinstance(tc.get("index"), int) else 0
                    slot = slots.setdefault(idx, {"id": None, "name": None, "args": []})
                    fn = tc.get("function") or {}
                    if tc.get("id"):
                        slot["id"] = tc["id"]
                    if fn.get("name"):
                        slot["name"] = (slot["name"] or "") + fn["name"]
                    args = fn.get("arguments")
                    if args:
                        slot["args"].append(args)
                        name = slot["name"]
                        if idx not in previews and name in PREVIEW_FIELDS:
                            previews[idx] = (name, ArgFieldStream(PREVIEW_FIELDS[name]))
                        if idx in previews:
                            _preview_feed(live, idx, previews[idx], args)
    except StreamError as exc:
        if isinstance(usage, dict):
            exc.usage = usage
        raise
    finally:
        show(split.flush())
        if hold:
            live.note(hold.flush())
    if not done:
        raise StreamError("transport", "the stream ended before [DONE]", seen, usage=usage)
    msg = {"role": "assistant", "content": "".join(content)}
    if slots:
        msg["tool_calls"] = [{"id": s["id"] or f"call_{k}", "type": "function",
                              "function": {"name": s["name"], "arguments": "".join(s["args"])}}
                             for k, s in sorted(slots.items())]
    return {"choices": [{"message": msg, "finish_reason": finish or "stop"}], "usage": usage}


def complete_tool_action(resp, mode):
    """True when a reply carries a whole tool action: native tool calls whose
    arguments parse to objects, or a prompt-mode tool block that parses."""
    choices = resp.get("choices") if isinstance(resp, dict) else None
    msg = ((choices[0] or {}).get("message") or {}) if choices else {}
    if mode == "native":
        calls = msg.get("tool_calls") or []
        if not calls:
            return False
        for c in calls:
            try:
                if not isinstance(json.loads((c.get("function") or {}).get("arguments") or ""),
                                  dict):
                    return False
            except ValueError:
                return False
        return True
    m = TOOL_BLOCK.search(msg.get("content") or "")
    if not m:
        return False
    try:
        return isinstance(json.loads(m.group(1)), dict)
    except ValueError:
        return False


def stream_turn(url, key, payload, route, live, mode, turn, tries=6):
    """Send one agent turn as a stream; returns a chat-shaped reply marked _live.

    A stale replica's 400 "Model not found" before the first event is
    refreshed and retried, as chat_with_model_retry does. Any other
    StreamError propagates to call_model.
    """
    for k in range(tries + 1):
        try:
            if route == "responses":
                resp = stream_gpt_turn(url, key, responses_payload(payload), live)
            elif route == "chat":
                resp = stream_claude_turn(url, key, {**payload, "stream": True}, live, mode)
            else:
                raise StreamError("transport", f"unknown stream route {route!r}", 0)
        except StreamError as exc:
            check_context_rejection(exc.detail)
            if (exc.kind == "http" and exc.events_seen == 0 and _http_status(exc) == 400
                    and "model not found" in (exc.detail or "").lower() and k < tries):
                refresh_models(url, key)
                print(f"  [{turn:>2}] model not found on this gateway replica "
                      f"(stale model cache); refreshed, retrying {k + 1}/{tries}",
                      file=sys.stderr, flush=True)
                time.sleep(random.uniform(0.5, 2.0))
                continue
            raise
        resp["_live"] = True
        return resp


def log_stream_failure(anomaly_path, attempt, payload, err, elapsed):
    diagnostic("request_error", attempt=attempt, elapsed_s=elapsed,
               events_seen=err.events_seen, reason=err.reason or
               {"idle": "stream_idle", "http": "http_error"}.get(err.kind, "transport_error"))
    cause = "timeout" if err.kind == "idle" else "transport"
    log_anomaly(anomaly_path, 1, attempt, "oneshot", payload,
                {"_transient": f"stream {err.kind}: {err.detail}"},
                cause=cause, elapsed=elapsed)


def gpt_stream_event(ev, live, state):
    """Apply one Responses API stream event to the live transcript."""
    _gpt_terminal_event(ev, state, 1)
    kind = ev.get("type") or ""
    if kind == "response.reasoning_summary_text.delta":
        state["summary_deltas"] = True
        live.reasoning(ev.get("delta") or "")
    elif kind == "response.reasoning_summary_part.done" and state["summary_deltas"]:
        live.reasoning("\n\n")
    elif kind == "response.output_item.done":
        item = ev.get("item") or {}
        if item.get("type") == "reasoning" and not state["summary_deltas"]:
            texts = [s.get("text") for s in item.get("summary") or []
                     if isinstance(s, dict) and s.get("text")]
            live.reasoning("\n\n".join(texts))
    elif kind == "response.output_text.delta":
        live.answer_text(ev.get("delta") or "")
    elif kind == "response.completed":
        state["completed"] = True


def _carry_oneshot_context_usage(exc, earlier, *current_snapshots):
    exc._earlier_usage = list(earlier)
    # A refused next request is not an unmeasured generation. Only append an
    # actual current snapshot; prior unknown paid attempts remain None in earlier.
    for usage in current_snapshots:
        if isinstance(usage, dict):
            exc._earlier_usage.append(usage)
            break


def _oneshot_preflight(payload, earlier):
    try:
        context_preflight(payload)
    except ContextBudgetError as exc:
        _carry_oneshot_context_usage(exc, earlier)
        raise


def stream_gpt_oneshot(args, url, key, prompt, tr, anomaly_path, images=()):
    """Stream a GPT one-shot through /openai/responses into the transcript.

    Returns (answer, usage_resp), or (None, None) when the stream failed before
    its first event, so the caller falls back to the non-streaming path. A
    stream silent for STREAM_IDLE_TIMEOUT gets one more attempt, at xhigh when
    it was max (reply-limits design). Raises StreamError otherwise.
    """
    effort = args.effort
    # Text-only one-shots keep the plain string input; images need input parts.
    gpt_input = prompt if not images else responses_input(
        [{"role": "user", "content": user_content(prompt, images)}])
    supervised = bool(getattr(args, "supervisor_manifest", None))
    attempts = (1,) if supervised else (1, 2)
    earlier = []    # one usage (or None if unknown) per retried attempt
    for attempt in attempts:
        payload = apply_effort({"model": args.model, "input": gpt_input, "stream": True},
                               args.model, effort)
        payload.setdefault("reasoning", {})["summary"] = "auto"
        if args.temperature is not None:
            payload["temperature"] = args.temperature
        _oneshot_preflight(payload, earlier)
        hb = (f"[ 1] calling {args.model} (attempt {attempt}/{len(attempts)}, 1 msgs, "
              f"~{_payload_bytes(payload)} bytes) ...")
        tr.write(f"\n_{hb}_")
        print("  " + hb, file=sys.stderr, flush=True)
        live = LiveAnswer(tr)
        state = {"summary_deltas": False, "completed": False, "usage": None}
        received = 0
        started = _now()
        try:
            for ev in stream_events(url, key, "/openai/responses", payload):
                if not ev.get("_done"):
                    received += 1
                gpt_stream_event(ev, live, state)
            if not state["completed"]:
                raise StreamError("transport",
                                  "the stream ended before the response completed", received,
                                  reason="stream_incomplete")
        except ContextBudgetError as exc:
            live.close()
            _carry_oneshot_context_usage(exc, earlier, getattr(exc, "usage", None), state["usage"])
            raise
        except StreamError as exc:
            answer = live.close()
            if exc.reason == "response_incomplete":
                exc._partial_answer = answer
            if exc.usage is None and isinstance(state["usage"], dict):
                exc.usage = state["usage"]     # counters received before the break
            exc._earlier_usage = list(earlier)
            try:
                check_context_rejection(exc.detail)
            except ContextBudgetError as rejected:
                _carry_oneshot_context_usage(rejected, earlier, exc.usage)
                raise
            log_stream_failure(anomaly_path, attempt, payload, exc, _now() - started)
            if (exc.reason == "response_incomplete" or supervised
                    or (exc.kind == "http" and _http_status(exc) == 413)):
                raise  # no replay of a semantic terminal, rejected body or unadmitted generation
            if exc.events_seen == 0 and exc.kind in ("http", "transport"):
                # This attempt generated nothing; earlier retried attempts still cost.
                return None, ({"usage": earlier.pop(), "_earlier_usage": earlier}
                              if earlier else None)
            if exc.kind == "idle" and attempt == 1:
                earlier.append(exc.usage if isinstance(exc.usage, dict) else None)
                if effort == "max":
                    effort = "xhigh"
                    note = (f"[ 1] no reply within {STREAM_IDLE_TIMEOUT} s at max; "
                            "retrying at xhigh")
                    tr.write(f"\n_{note}_")
                    print("  " + note, file=sys.stderr, flush=True)
                continue
            raise
        answer = live.close()
        if not usable_answer(answer):
            exc = StreamError("transport", "the stream ended without a usable answer", received)
            log_stream_failure(anomaly_path, attempt, payload, exc, _now() - started)
            if supervised:
                exc.usage = state["usage"]
                exc._earlier_usage = list(earlier)
                raise exc
            return None, {"usage": state["usage"], "_earlier_usage": earlier}
        return answer, {"usage": state["usage"], "_earlier_usage": earlier}
    raise StreamError("idle", f"no event for {STREAM_IDLE_TIMEOUT}s", 1)


ONESHOT_CUT_MARK = "[cut off by the gateway's 2-minute limit]"


class ThinkSplitter:
    """Split streamed text into reasoning (inside <think>...</think>) and answer.

    A tag can be split across chunks, so a possible partial tag at the end of
    a chunk is held back until the next one decides it.
    """

    OPEN, CLOSE = "<think>", "</think>"

    def __init__(self):
        self.inside = False
        self.carry = ""

    def _kind(self):
        return "reasoning" if self.inside else "answer"

    def feed(self, text):
        buf, out = self.carry + text, []
        self.carry = ""
        while buf:
            tag = self.CLOSE if self.inside else self.OPEN
            i = buf.find(tag)
            if i == -1:
                keep = max((k for k in range(1, len(tag)) if buf.endswith(tag[:k])),
                           default=0)
                piece = buf[:-keep] if keep else buf
                self.carry = buf[-keep:] if keep else ""
                if piece:
                    out.append((self._kind(), piece))
                break
            if i:
                out.append((self._kind(), buf[:i]))
            buf = buf[i + len(tag):]
            self.inside = not self.inside
        return out

    def flush(self):
        rest, self.carry = self.carry, ""
        return [(self._kind(), rest)] if rest else []


def _feed_live(live, pieces):
    for kind, text in pieces:
        if kind == "reasoning":
            live.reasoning(text)
        else:
            live.answer_text(text)


def stream_chat_oneshot(args, url, key, prompt, tr, anomaly_path, images=()):
    """Stream a Claude one-shot through chat completions into the transcript.

    Returns (answer, usage_resp), or (None, None) when the stream failed before
    its first event, so the caller falls back to the non-streaming path. The
    gateway ends a cut stream normally (2026-10-02 probes), so elapsed time is
    the only signal: a stream that ran REPLY_CUT_SECONDS or longer is retried
    once with ONESHOT_SHORTER, and a second cut keeps that answer marked with
    ONESHOT_CUT_MARK. A stream silent for STREAM_IDLE_TIMEOUT gets one more
    attempt, at xhigh when it was max. Raises StreamError otherwise.
    """
    text = user_content(prompt, images)
    effort = args.effort
    earlier = []
    supervised = bool(getattr(args, "supervisor_manifest", None))
    attempts = (1,) if supervised else (1, 2)
    for attempt in attempts:
        payload = apply_effort({"model": args.model,
                                "messages": [{"role": "user", "content": text}],
                                "stream": True}, args.model, effort)
        if args.temperature is not None:
            payload["temperature"] = args.temperature
        _oneshot_preflight(payload, earlier)
        hb = (f"[ 1] calling {args.model} (attempt {attempt}/{len(attempts)}, 1 msgs, "
              f"~{_payload_bytes(payload)} bytes) ...")
        tr.write(f"\n_{hb}_")
        print("  " + hb, file=sys.stderr, flush=True)
        live, split = LiveAnswer(tr), ThinkSplitter()
        usage, done = None, False
        received = 0
        started = _now()
        try:
            for ev in stream_events(url, key, "/api/chat/completions", payload):
                if ev.get("_done"):
                    done = True
                    continue
                received += 1
                for choice in (ev.get("choices") or [])[:1]:
                    piece = (choice.get("delta") or {}).get("content")
                    if isinstance(piece, str):
                        _feed_live(live, split.feed(piece))
                if isinstance(ev.get("usage"), dict):
                    usage = ev["usage"]
            _feed_live(live, split.flush())
            if not done:
                raise StreamError("transport", "the stream ended before [DONE]", received)
        except ContextBudgetError as exc:
            live.close()
            _carry_oneshot_context_usage(exc, earlier, getattr(exc, "usage", None), usage)
            raise
        except StreamError as exc:
            live.close()
            if exc.usage is None and isinstance(usage, dict):
                exc.usage = usage              # counters received before the break
            exc._earlier_usage = list(earlier)
            try:
                check_context_rejection(exc.detail)
            except ContextBudgetError as rejected:
                _carry_oneshot_context_usage(rejected, earlier, exc.usage)
                raise
            log_stream_failure(anomaly_path, attempt, payload, exc, _now() - started)
            if exc.kind == "http" and _http_status(exc) == 413:
                raise
            if exc.events_seen == 0 and exc.kind in ("http", "transport"):
                if isinstance(exc.usage, dict):
                    earlier.append(exc.usage)
                # Only attempts that generated or reported usage cost anything.
                return None, ({"usage": earlier.pop(), "earlier_usage": earlier}
                              if earlier else None)
            if exc.kind == "idle" and attempt == 1:
                earlier.append(exc.usage if isinstance(exc.usage, dict) else None)
                if effort == "max":
                    effort = "xhigh"
                    note = (f"[ 1] no reply within {STREAM_IDLE_TIMEOUT} s at max; "
                            "retrying at xhigh")
                    tr.write(f"\n_{note}_")
                    print("  " + note, file=sys.stderr, flush=True)
                continue
            raise
        elapsed = _now() - started
        answer = live.close()
        if elapsed < REPLY_CUT_SECONDS:
            if not usable_answer(answer):
                exc = StreamError("transport", "the stream ended without a usable answer", received)
                log_stream_failure(anomaly_path, attempt, payload, exc, elapsed)
                return None, {"usage": usage, "earlier_usage": earlier}
            return answer, {"usage": usage, "earlier_usage": earlier}
        log_anomaly(anomaly_path, 1, attempt, "oneshot", payload,
                    {"choices": [{"message": {"content": ""}, "finish_reason": "stop"}]},
                    cause="reply_cut", elapsed=elapsed)
        tr.write("\n" + ONESHOT_CUT_MARK)
        if attempt == 1:
            earlier.append(usage if isinstance(usage, dict) else None)
        if attempt == 2:
            if not usable_answer(answer):
                exc = StreamError("transport", "the reply was cut twice without a usable answer",
                                  received, usage=usage)
                exc._earlier_usage = list(earlier)
                raise exc
            return answer + "\n\n" + ONESHOT_CUT_MARK, {"usage": usage, "earlier_usage": earlier}
        note = (f"[ 1] reply cut by the gateway's 2-minute limit after {elapsed:.0f} s; "
                "asking for a shorter answer")
        tr.write(f"\n_{note}_")
        print("  " + note, file=sys.stderr, flush=True)
        text = append_content_text(user_content(prompt, images), ONESHOT_SHORTER)
    raise StreamError("transport", "the stream ended without an answer", 1)


def run_oneshot(args, url, key, prompt, tr, tpath, anomaly_path, lifecycle,
                journal, journal_usage, images=()):
    """Run the one-shot under the selected model-v1 context Scope, if any."""
    call = (args, url, key, prompt, tr, tpath, anomaly_path, lifecycle,
            journal, journal_usage, images)
    sel = getattr(args, "context_selection", None)
    if sel is None or sel.policy is None:
        return _run_oneshot_impl(*call)
    route = None if getattr(args, "no_stream", False) else oneshot_stream_route(args.model)
    meter = capability_module("context_tokens").ContextMeter(
        sel.policy, route=route or "chat", model=args.model, image_type=ImageAsset)
    cengine = capability_module("context_engine")
    try:
        with cengine.scope(cengine.Scope(sel.policy, meter)):
            return _run_oneshot_impl(*call)
    finally:
        artifact_dir = (tpath[:-3] if tpath.endswith(".md") else tpath) + ".artifacts"
        try:
            os.makedirs(artifact_dir, exist_ok=True)
            with open(os.path.join(artifact_dir, "completion.json"), "w", encoding="utf-8") as fh:
                json.dump({"context_policy": sel.metadata()}, fh, indent=2, sort_keys=True)
        except OSError:
            pass  # metadata failure must not mask the original outcome


def _run_oneshot_impl(args, url, key, prompt, tr, tpath, anomaly_path, lifecycle,
                      journal, journal_usage, images=()):
    """Send one tool-less request and record it like a run.

    GPT and Claude one-shots stream, so the transcript
    and the dashboard's narration show reasoning and answer text as they arrive;
    a stream that fails before its first event falls back to the non-streaming
    path, which other models use directly. Registration (start record,
    transcript header) is done by main(); this writes the answer, the usage
    row, the footer and the end record.
    """
    supervised = bool(getattr(args, "supervisor_manifest", None))
    images = list(images or [])
    if images:
        tr.scrub_images = True
    msg = {"role": "user", "content": user_content(prompt, images)}
    journal(msg)
    for a in images:
        tr.write(f"\n_image attached: {a.describe()}_")

    usages = []

    def known_total(name):
        # Complete total or None: one unmeasured attempt makes the sum unknown, not partial.
        values = [normalize_usage(u)[name] for u in usages]
        if not values or not all(type(v) is int and v >= 0 for v in values):
            return None
        return sum(values)

    def record_response(resp):
        for u in (resp.get("_earlier_usage") or resp.get("earlier_usage") or []):
            journal_usage(1, "loop", {"usage": u}, started=call_started)
            usages.append(u)
        journal_usage(1, "loop", resp, started=call_started)
        usages.append(resp.get("usage"))

    def close_failed(reason, exit_code=1, stop_reason=None):
        tail = f"[{args.model} e={args.effort} tools=oneshot: 0 tool calls]"
        tr.write("\n" + tail)
        tr.close()
        lifecycle.finish("failed", tool_calls=0, refused=0,
                         sent=known_total("sent"), recv=known_total("recv"),
                         stop_reason=stop_reason)
        if images:
            reason = scrub_data_urls(reason) + (
                f" -- {args.model} with {len(images)} image(s); not retried "
                "without the images or with another model")
        print(f"error: {reason}. Transcript: {tpath}", file=sys.stderr)
        sys.exit(exit_code)

    def close_context_failed(exc):
        for usage in getattr(exc, "_earlier_usage", []):
            record_response({"usage": usage})
        if not getattr(exc, "provider", False):
            report = ("[PARTIAL - context_budget]\nLOCAL report: request admission stopped "
                      "this one-shot. No tools ran and no edits were made. The requested "
                      "answer is unverified; shorten the input before retrying.")
            print(report)
            tr.write("\n" + report)
        close_failed("provider context limit stopped this request" if getattr(exc, "provider", False)
                     else "local request admission stopped this request", 3, "context_budget")

    call_started = datetime.datetime.now(datetime.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ")
    content = usage_resp = None
    route = None if getattr(args, "no_stream", False) else oneshot_stream_route(args.model)
    if route is not None:
        try:
            if route == "responses":
                content, usage_resp = stream_gpt_oneshot(args, url, key, prompt, tr,
                                                         anomaly_path, images)
            elif route == "chat":
                content, usage_resp = stream_chat_oneshot(args, url, key, prompt, tr,
                                                          anomaly_path, images)
        except ContextBudgetError as exc:
            close_context_failed(exc)
        except StreamError as exc:
            record_response({"usage": exc.usage if isinstance(exc.usage, dict) else None,
                             "_earlier_usage": getattr(exc, "_earlier_usage", [])})
            if exc.reason == "response_incomplete":
                sys.stdout.write(getattr(exc, "_partial_answer", ""))
                sys.stdout.flush()
                tr.write("\n[PARTIAL - response_incomplete]")
                close_failed(exc.detail, stop_reason="response_incomplete")
            close_failed(f"streaming request failed ({exc.kind}: {exc.detail})",
                         3 if exc.kind == "http" and _http_status(exc) == 413 else 1)
        except SystemExit as exc:
            if exc.code not in (None, 0, 1):
                print(exc.code, file=sys.stderr)
            close_failed("gateway request failed")
        if content is None and usage_resp is not None:
            record_response(usage_resp)    # streamed attempts that fell back still cost

    if content is None:
        if supervised and route is not None:
            close_failed("supervised one-shot yielded no complete response")
        payload = apply_effort({"model": args.model, "messages": [msg], "stream": False},
                               args.model, args.effort)
        if args.temperature is not None:
            payload["temperature"] = args.temperature
        try:
            call_options = {"max_attempts": 1} if supervised else {}
            resp, ok, cause, elapsed = call_model(url, key, payload, "oneshot", tr,
                                                  anomaly_path, 1, **call_options)
            record_response(resp)
            if not ok and cause == "reply_cut" and not supervised:
                note = (f"[ 1] reply cut by the gateway's 2-minute limit after "
                        f"{elapsed:.0f} s; asking for a shorter answer")
                tr.write(f"\n_{note}_")
                print("  " + note, file=sys.stderr, flush=True)
                shorter = {"role": "user",
                           "content": append_content_text(msg["content"], ONESHOT_SHORTER)}
                journal(shorter)
                payload = {**payload, "messages": [shorter]}
                resp, ok, cause, elapsed = call_model(url, key, payload, "oneshot", tr,
                                                      anomaly_path, 1)
                record_response(resp)
        except ContextBudgetError as exc:
            close_context_failed(exc)
        except SystemExit as exc:
            # chat_with_model_retry / chat stop with a message on a fatal 400, an
            # auth error or exhausted stale-replica retries: record the run failed.
            if exc.code not in (None, 0, 1):
                print(exc.code, file=sys.stderr)
            for u in getattr(exc, "_earlier_usage", []):
                record_response({"usage": u})
            close_failed("gateway request failed",
                         3 if getattr(exc, "_transport_rejected", False) else 1)
        if not ok:
            close_failed("model returned no usable answer -- likely a context-limit "
                         "or gateway failure")
        content = resp["choices"][0]["message"].get("content") or ""
        usage_resp = resp
        tr.write("\n---\n\n## Final report\n\n" + content)
    else:
        record_response(usage_resp)

    journal({"role": "assistant", "content": content})
    sent, recv = known_total("sent"), known_total("recv")
    tail = (f"[{args.model} e={args.effort} tools=oneshot: 0 tool calls"
            f"{f', {sent or 0:,} tok sent / {recv or 0:,} tok returned' if sent or recv else ''}]")
    tr.write("\n" + tail)
    tr.close()
    lifecycle.finish("complete", tool_calls=0, refused=0, sent=sent, recv=recv)

    sys.stdout.write(content)
    sys.stdout.flush()
    shown = ["?" if v is None else v for v in (sent, recv)]
    img = reported_image_tokens(usages)
    img_note = ""
    if img is not None:
        img_note = f", incl. {img[0]} image-input tok{' (partial)' if img[1] else ''}"
    print(f"\n[sent {shown[0]} tok to {args.model}{img_note}; returned {shown[1]} tok]",
          file=sys.stderr, flush=True)


def reported_image_tokens(usages):
    """(subtotal, partial) of explicitly reported image-input tokens, or None.

    Only prompt_tokens_details.image_tokens / input_tokens_details.image_tokens
    count; never estimated. It is already inside the sent total, so it is
    shown as an 'incl.' note and never added. partial is True when some usage
    in the set lacked the counter.
    """
    values, missing = [], False
    for u in usages:
        v = None
        if isinstance(u, dict):
            for nested in ("prompt_tokens_details", "input_tokens_details"):
                d = u.get(nested)
                if isinstance(d, dict) and isinstance(d.get("image_tokens"), int) \
                        and not isinstance(d.get("image_tokens"), bool):
                    v = d["image_tokens"]
                    break
        if v is None:
            missing = True
        else:
            values.append(v)
    if not values:
        return None
    return sum(values), missing


def build_oneshot_prompt(task, attachments, max_chars=ONESHOT_MAX_CHARS):
    """The task text, then each attached file under a FILE separator, in order.

    Exits 2 (before any request) on an unreadable file or when the running
    total passes max_chars (None: model-v1, the token window decides).
    """
    prompt = task
    for path in attachments:
        try:
            with open(path, encoding="utf-8", errors="replace") as fh:
                text = fh.read()
        except OSError:
            print(f"error: cannot read {path}", file=sys.stderr)
            sys.exit(2)
        prompt += f"\n\n===== FILE: {path} =====\n{text}"
        if max_chars is not None and len(prompt) > max_chars:
            print(f"error: input exceeds 600,000 chars at {path}. Pass fewer "
                  f"files, or split the request.", file=sys.stderr)
            sys.exit(2)
    return prompt


def prepare_oneshot_images(paths):
    """Every --attach-image as an ImageAsset, checked against the request caps.

    Exits 2 (before config, index or network) on any refused image.
    """
    images = []
    for path in paths:
        try:
            images.append(prepare_image(path, path))
        except ImageError as exc:
            print(f"error: {exc}", file=sys.stderr)
            sys.exit(2)
        err = image_cap_error(images)
        if err:
            print(f"error: {err} at {path}. Pass fewer or smaller images.",
                  file=sys.stderr)
            sys.exit(2)
    return images


CLAUDE_MULTI_REFUSED = ("refused: this reply contained several tool calls and none "
                        "were run. Call exactly ONE tool per reply.")
OUTCOME_UNKNOWN_SKIPPED = ("not run: an earlier action's outcome is unknown (it may have "
                           "acted), so no further tools run in this session.")


def preload_skills(root, names):
    """Validate and read --skill bodies before any network or worktree."""
    if not names:
        return []
    try:
        reg = capability_module("harness_skills").SkillRegistry(
            root, skills_dirs=SKILLS_DIRS, strict_duplicates=False)
        warn_skipped_skills(reg)
        out = []
        for name in names:
            out.append((name, skill_body_checked(
                name, reg.load(name, max_chars=MAX_READ_CHARS))))
        return out
    except Exception as e:
        print(f"error: --skill: {type(e).__name__}: {e}", file=sys.stderr)
        sys.exit(2)


def skill_prompt(bodies):
    return "".join(SKILL_LABEL.format(name=n) + b + "\n\n" for n, b in bodies)


def mcp_event_line(ev):
    """Stable heartbeat marker for monitors: metadata only, never args/results."""
    rec = {"phase": ev.get("phase"), "server": ev.get("server"), "tool": ev.get("tool"),
           "timeout_s": ev.get("timeout_s"), "duration_s": ev.get("duration_s")}
    if ev.get("phase") == "end":
        rec["status"] = ev.get("status")
    return "_MCP_CALL " + json.dumps(rec, separators=(",", ":"), default=str) + "_"


def attach_capability_observers(run, tr, usage_path, model, tpath):
    """Wire MCP events and skill loads to the transcript and usage journal."""
    def on_event(ev):
        if not isinstance(ev, dict):
            return
        tr.write(mcp_event_line(ev))
        phase = ev.get("phase")
        if phase == "start" and ev.get("args") is not None:
            tr.write("```\nMCP args: " + bounded(json.dumps(
                strip_binary(ev["args"]), default=str), MCP_EVENT_TEXT_MAX) + "\n```")
        elif phase == "end":
            body = ev.get("error") if ev.get("error") is not None else ev.get("result")
            if body is not None:
                tr.write("```\nMCP " + ("error" if ev.get("error") is not None else "result")
                         + ": " + bounded(json.dumps(strip_binary(body), default=str),
                                          MCP_EVENT_TEXT_MAX) + "\n```")
        if phase in ("start", "end"):
            row = {"phase": "mcp_" + phase, "model": model, "server": ev.get("server"),
                   "tool": ev.get("tool"), "duration_s": ev.get("duration_s"),
                   "status": ev.get("status") if phase == "end" else None,
                   "transcript": os.path.abspath(tpath),
                   "ts": datetime.datetime.now(datetime.timezone.utc)
                   .strftime("%Y-%m-%dT%H:%M:%SZ"),
                   "sent": None, "recv": None, "cache_read": None, "cache_write": None}
            try:
                with open(usage_path, "a", encoding="utf-8", newline="\n") as fh:
                    fh.write(json.dumps(row, sort_keys=True, separators=(",", ":"),
                                        default=str) + "\n")
            except OSError as exc:
                print(f"WARNING: usage journal append failed: {type(exc).__name__}",
                      file=sys.stderr)

    run.caps.event = on_event
    run.caps.on_skill_load = lambda name: tr.write(f"_SKILL_LOADED {name}_")


class LaunchReceipt:
    """Append-only startup receipt: launch metadata only, never task/argv/usage."""

    def __init__(self, path, launch_id):
        self.path, self.launch_id = path, launch_id
        self.done = False
        self.reason = None
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_APPEND, 0o600)
            self.handle = os.fdopen(fd, "a", encoding="utf-8", newline="\n")
        except FileExistsError:
            print(f"error: launch receipt already exists: {path}", file=sys.stderr)
            sys.exit(2)
        except OSError as exc:
            print(f"error: cannot create launch receipt {path}: {type(exc).__name__}",
                  file=sys.stderr)
            sys.exit(2)
        try:
            self.write("starting")
        except BaseException:
            self.handle.close()
            raise

    def write(self, state, run_id=None, **fields):
        rec = {"schema": 1, "launch_id": self.launch_id, "engine_pid": os.getpid(),
               "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
               "state": state, "run_id": run_id, **fields}
        self.handle.write(json.dumps(rec, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n")
        self.handle.flush()
        os.fsync(self.handle.fileno())

    def close(self):
        self.handle.close()


_LAUNCH = None


def _bootstrap_launch_receipt(argv):
    """Take the explicit --launch-receipt/--launch-id pair before full argparse,
    so a parse failure is still recorded. Returns (path, id) or (None, None)."""
    found = {}
    for i, arg in enumerate(argv):
        for flag in ("--launch-receipt", "--launch-id"):
            if arg == flag and i + 1 < len(argv):
                if flag in found:
                    print("error: launch identity options must not be repeated", file=sys.stderr)
                    sys.exit(2)
                found[flag] = argv[i + 1]
            elif arg.startswith(flag + "="):
                if flag in found:
                    print("error: launch identity options must not be repeated", file=sys.stderr)
                    sys.exit(2)
                found[flag] = arg[len(flag) + 1:]
    path, launch_id = found.get("--launch-receipt"), found.get("--launch-id")
    if path is None and launch_id is None:
        return None, None
    if path is None or launch_id is None:
        print("error: --launch-receipt and --launch-id must be given together",
              file=sys.stderr)
        sys.exit(2)
    if not os.path.isabs(path):
        print("error: --launch-receipt must be an absolute path", file=sys.stderr)
        sys.exit(2)
    try:
        launch_id = str(uuid.UUID(launch_id))
    except ValueError:
        print("error: --launch-id must be a UUID", file=sys.stderr)
        sys.exit(2)
    return path, launch_id


def _launch_abort(code):
    """Record a pre-ready exit. Never masks the original error."""
    rec = _LAUNCH
    if rec is None:
        if code not in (None, 0):
            number = code if isinstance(code, int) else 1
            line = (f"LAUNCH_FAILED engine_pid={os.getpid()} reason_code=preflight_error "
                    f"exit_code={number}")
            print(line, flush=True)
            print(line, file=sys.stderr, flush=True)
        return
    if rec.done:
        return
    rec.done = True
    state = "rejected" if code == 2 else "failed"
    marker = "LAUNCH_REJECTED" if state == "rejected" else "LAUNCH_FAILED"
    reason = rec.reason or ("usage_error" if state == "rejected" else "startup_error")
    exit_code = code if isinstance(code, int) else (0 if code is None else 1)
    try:
        rec.write(state, reason_code=reason, exit_code=exit_code)
    except OSError as exc:
        print(f"WARNING: launch receipt write failed: {type(exc).__name__}", file=sys.stderr)
    line = f"{marker} launch_id={rec.launch_id} reason_code={reason} receipt={rec.path}"
    print(line, flush=True)
    print(line, file=sys.stderr, flush=True)


def diagnostic_journal(path, **identity):
    try:
        hd = capability_module("harness_diagnostics")
        journal = hd.Journal(path, "engine", bundle_id=hd.bundle_id(ENGINE_DIR), **identity)
        journal.emit("engine_build", phase="startup", execution_profile=identity.get("execution_profile"),
                     runtime_version=".".join(str(part) for part in sys.version_info[:3]),
                     architecture="64bit" if sys.maxsize > 2**32 else "32bit")
        return journal
    except Exception:
        try:
            print("WARNING: lifecycle diagnostics unavailable", file=sys.stderr)
        except Exception:
            pass
        return None


def _startup_diagnostics():
    global DIAGNOSTICS
    DIAGNOSTICS = diagnostic_journal(_LAUNCH.path + ".engine-startup.diagnostics.jsonl",
                                     launch_id=_LAUNCH.launch_id, engine_pid=os.getpid())
    diagnostic("observer_started", phase="startup")


def _execution_profile_module():
    """Load only the pinned sibling, including embedded callers without ENGINE_DIR on sys.path."""
    if "execution_profile" not in _CAP_MODULES:
        import importlib.util
        name = "_agentcontrol_execution_profile"
        path = os.path.join(ENGINE_DIR, "execution_profile.py")
        try:
            spec = importlib.util.spec_from_file_location(name, path)
            module = importlib.util.module_from_spec(spec)
            sys.modules[name] = module
            spec.loader.exec_module(module)
        except (OSError, ImportError, AttributeError):
            sys.modules.pop(name, None)
            raise ValueError("invalid_settings") from None
        _CAP_MODULES["execution_profile"] = module
    return _CAP_MODULES["execution_profile"]


def _read_engine_settings():
    """Read bounded local metadata; protected operator storage is a precondition.

    Trusted programs and home/.claude protection must be established by the
    operator. This is not an ACL sandbox against workers running as the same SID.
    """
    from pathlib import Path
    profile = _execution_profile_module()
    MAX_SETTINGS_BYTES, parse_settings = profile.MAX_SETTINGS_BYTES, profile.parse_settings

    try:
        path = Path.home() / ".claude" / "engine-settings.json"
        if not path.is_absolute() or str(path).startswith(("\\\\", "//")):
            raise ValueError("invalid_settings")
        if os.name == "nt":
            import ctypes
            drive_type = ctypes.windll.kernel32.GetDriveTypeW
            drive_type.argtypes = [ctypes.c_wchar_p]
            drive_type.restype = ctypes.c_uint
            if drive_type(path.anchor) not in (2, 3, 6):
                raise ValueError("invalid_settings")
        for candidate in (*reversed(path.parents), path):
            try:
                info = os.lstat(candidate)
            except FileNotFoundError:
                return None
            if (stat.S_ISLNK(info.st_mode)
                    or getattr(info, "st_file_attributes", 0) & 0x400):
                raise ValueError("invalid_settings")
            if candidate != path and not stat.S_ISDIR(info.st_mode):
                raise ValueError("invalid_settings")
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                or info.st_size > MAX_SETTINGS_BYTES):
            raise ValueError("invalid_settings")
        flags = (os.O_RDONLY | getattr(os, "O_BINARY", 0)
                 | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
        with os.fdopen(os.open(path, flags), "rb") as stream:
            opened = os.fstat(stream.fileno())
            if not os.path.samestat(info, opened):
                raise ValueError("invalid_settings")
            raw = stream.read(MAX_SETTINGS_BYTES + 1)
            final = os.fstat(stream.fileno())
            if (not stat.S_ISREG(final.st_mode) or final.st_nlink != 1
                    or final.st_size > MAX_SETTINGS_BYTES
                    or final.st_size != opened.st_size
                    or final.st_mtime_ns != opened.st_mtime_ns):
                raise ValueError("invalid_settings")
        return parse_settings(raw)
    except (OSError, ValueError, RuntimeError):
        raise ValueError("invalid_settings") from None


def main():
    global _LAUNCH
    _LAUNCH = None
    if "--show-profile" in sys.argv[1:]:
        return _main()  # A cold probe has no launch lifecycle to journal.
    primary = cleanup_error = None
    try:
        _main()
    except SystemExit as exc:
        primary = exc
        _launch_abort(exc.code)
        raise
    except BaseException as exc:
        primary = exc
        _launch_abort(1)  # any non-exit exception before ready is a startup failure
        raise
    finally:
        try:
            diagnostic("cleanup_start", phase="cleanup")
            try:
                failures = close_open_runners()
            except BaseException as exc:
                cleanup_error = exc
                # Sanitized evidence; never replace the run's original error/outcome.
                diagnostic("cleanup_end", phase="cleanup", outcome="failed",
                           **_safe_exception_fields(exc))
                if primary is None:
                    raise
            else:
                diagnostic("cleanup_end", phase="cleanup",
                           outcome="failed" if failures else "returned")
        finally:
            try:
                if _LAUNCH is not None:
                    _LAUNCH.close()
            except Exception as exc:
                diagnostic("cleanup_error", phase="cleanup", outcome="raised",
                           **_safe_exception_fields(exc))
                if primary is None and cleanup_error is None and not isinstance(exc, OSError):
                    cleanup_error = exc
                    raise
                try:
                    print("WARNING: launch receipt close failed", file=sys.stderr)
                except Exception:
                    pass
            finally:
                _LAUNCH = None
                # Keep the primary failure paired and last, even if teardown failed.
                error = primary if primary is not None else cleanup_error
                if isinstance(error, SystemExit):
                    code = error.code if isinstance(error.code, int) else (0 if error.code is None else 1)
                    diagnostic("process_exit", reason="system_exit", exit_code=code)
                elif error is not None:
                    diagnostic("exception", reason="broken_output" if isinstance(error, BrokenPipeError)
                               else "unhandled_exception", **_safe_exception_fields(error))


def _main():
    with contextlib.ExitStack() as context_scopes:
        return _main_impl(context_scopes)


def _main_impl(context_scopes):
    global DIAGNOSTICS, _DIAGNOSTIC_REQUEST
    DIAGNOSTICS, _DIAGNOSTIC_REQUEST = None, 0
    ap = argparse.ArgumentParser()
    if "--read-files" in sys.argv[1:] and any(flag in sys.argv[1:] for flag in (
            "--version", "--supervisor-capabilities", "--supervisor-capabilities-v2")):
        ap.error("read_files requires an ordinary direct native agent run")
    # Checked before -t is required, so a version probe needs no dummy task.
    if "--version" in sys.argv[1:]:
        print(f"delegate.py harness version {HARNESS_VERSION} "
              f"(policy file: {POLICY_FILE})")
        return
    # Pure probe: no config, policy, receipt or index side effects.
    if "--supervisor-capabilities" in sys.argv[1:]:
        print(json.dumps(capability_module("supervisor_policy").capabilities()))
        return
    if "--supervisor-capabilities-v2" in sys.argv[1:]:
        from collections.abc import Mapping

        def plain(value):
            if isinstance(value, Mapping):
                return {key: plain(item) for key, item in value.items()}
            if isinstance(value, (tuple, list)):
                return [plain(item) for item in value]
            return value

        print(json.dumps(plain(capability_module("supervisor_manifest_v2").capabilities2())))
        return
    global _LAUNCH
    show_profile = "--show-profile" in sys.argv[1:]
    if not show_profile:
        receipt_path, launch_id = _bootstrap_launch_receipt(sys.argv[1:])
        if receipt_path is not None:
            _LAUNCH = LaunchReceipt(receipt_path, launch_id)
            _startup_diagnostics()
    ap.add_argument("--launch-receipt", metavar="ABSOLUTE_PATH",
                    help="append-only startup receipt (exclusive create); paired "
                         "with --launch-id")
    ap.add_argument("--launch-id", metavar="UUID",
                    help="launch identity recorded in --launch-receipt")
    ap.add_argument("--version", action="store_true",
                    help="print the harness version and exit; a repo can assert a "
                         "minimum before trusting its policy file is honoured")
    grp = ap.add_mutually_exclusive_group(required=not show_profile)
    grp.add_argument("-t", "--task", help="the task text")
    grp.add_argument("-f", "--task-file", dest="task_file", metavar="PATH",
                     help="read the task from a UTF-8 or UTF-16 text file; '-' "
                          "reads standard input. Prefer this to -t in PowerShell, "
                          "where \"$(cat file)\" joins the lines with spaces.")
    ap.add_argument("-m", "--model", default=DEFAULT_MODEL)
    ap.add_argument("-e", "--effort", default="high",
                    choices=["none", "low", "medium", "high", "xhigh", "max"])
    ap.add_argument("--tool-mode", default="auto", choices=["auto", "native", "prompt"])
    ap.add_argument("--profile", default=None, choices=["legacy", "resilient-v1"])
    ap.add_argument("--read-files", action="store_true",
                    help="opt in to bounded multi-file reads for direct resilient/model-v1 runs")
    ap.add_argument("--show-profile", action="store_true",
                    help="print execution profile and provenance as JSON, then exit")
    # strings on purpose: values are converted with fixed error codes, never echoed
    ap.add_argument("--context-policy", default=None, metavar="legacy|model-v1")
    ap.add_argument("--context-window-tokens", default=None, metavar="N")
    ap.add_argument("--context-reply-tokens", default=None, metavar="N")
    ap.add_argument("--context-safety-tokens", default=None, metavar="N")
    ap.add_argument("--auto-compact", default=None, action=argparse.BooleanOptionalAction)
    ap.add_argument("--compact-fraction", default=None, metavar="F")
    ap.add_argument("--recovery-contract", dest="recovery_contract", default=None,
                    metavar="PATH",
                    help="operator-prepared closed recovery contract (resilient-v1 agent "
                         "mode only); records observations, grants no authority")
    ap.add_argument("--allow-prompt-tools", action="store_true",
                    help="allow prompt tools for a native-capable model in an "
                         "operator-approved compatibility test")
    ap.add_argument("-C", "--root", default=".")
    # None sentinels (defaults 30 / agent.patch are filled in after parsing) so
    # --oneshot can tell an explicitly passed value from the default.
    ap.add_argument("--max-iters", type=int, default=None,
                    help="hard total of model turns, including the final report")
    ap.add_argument("--soft-iters", type=int, default=None,
                    help="checkpoint turn; preauthorizes one continuation up to the "
                         "work turns left under --max-iters")
    ap.add_argument("--patch", default=None)
    ap.add_argument("--transcript")
    ap.add_argument("--label", default="agent")
    ap.add_argument("--write", action="store_true",
                    help="real writes in --root without creating a worktree. Only safe "
                         "when --root is already a disposable worktree; otherwise the "
                         "agent edits your actual tree.")
    ap.add_argument("--worktree", action="store_true",
                    help="run in a disposable git worktree with REAL writes, so the "
                         "agent can run tests against its own edits")
    ap.add_argument("--verify-command", dest="verify_command", action="append",
                    default=[], metavar="JSON_ARGV",
                    help="repeatable JSON argv verification command required before finish")
    ap.add_argument("--review-finish", dest="review_finish", action="store_true",
                    help="run at most one read-only patch review at finish")
    ap.add_argument("--supervise-stuck", dest="supervise_stuck", action="store_true",
                    help="opt-in read-only advisor consultation on trajectory suspicion")
    ap.add_argument("--oneshot", action="store_true",
                    help="send exactly one request with no tools and print the "
                         "reply; still registered, journalled and indexed like a run")
    ap.add_argument("--attach", action="append", default=[], metavar="FILE",
                    help="--oneshot only: append FILE's text to the prompt "
                         "(repeatable, in order)")
    ap.add_argument("--text-only", action="store_true",
                    help="refuse image attachments and view_image before loading assets")
    ap.add_argument("--attach-image", dest="attach_image", action="append", default=[],
                    metavar="FILE",
                    help="--oneshot only: send a PNG/JPEG/WebP image with the prompt "
                         "(repeatable, in order)")
    ap.add_argument("--temperature", type=float, default=None,
                    help="--oneshot only: sampling temperature")
    ap.add_argument("--no-stream", action="store_true",
                    help="send agent turns and one-shots without streaming (no live view)")
    ap.add_argument("--skill", action="append", default=[], metavar="NAME",
                    help="preload a skill's instructions (repeatable); agents can "
                         "also load skills on demand with load_skill")
    ap.add_argument("--supervisor-manifest", metavar="ABSOLUTE_PATH",
                    help="opt-in supervisor protocol v1 manifest (outside the repo); "
                         "paired with --supervisor-manifest-sha256")
    ap.add_argument("--supervisor-manifest-sha256", metavar="HEX64",
                    help="pinned sha256 of --supervisor-manifest")
    ap.add_argument("--supervisor-capabilities", action="store_true",
                    help="print supervisor protocol capabilities as JSON and exit")
    args = ap.parse_args()
    has_manifest = (args.supervisor_manifest is not None
                    or args.supervisor_manifest_sha256 is not None)
    approved_worker = os.environ.get("_HARNESS_APPROVED_WORKER") == "1"
    if args.read_files and (
            args.profile != "resilient-v1" or args.context_policy != "model-v1"
            or args.oneshot or has_manifest or approved_worker or args.recovery_contract is not None
            or args.show_profile or args.supervisor_capabilities or args.allow_prompt_tools
            or (auto_tool_mode(args.model) if args.tool_mode == "auto" else args.tool_mode) != "native"):
        print("error: read_files requires explicit resilient-v1/model-v1 direct native agent mode",
              file=sys.stderr)
        sys.exit(2)
    try:
        resolve_profile = _execution_profile_module().resolve_profile
        settings = None
        if (args.profile is None and not args.oneshot
                and not has_manifest and not approved_worker):
            settings = _read_engine_settings()
        selection = resolve_profile(args.profile, oneshot=args.oneshot,
                                    supervisor_manifest=has_manifest,
                                    approved_worker=approved_worker, settings=settings)
    except ValueError:
        print("error: execution profile refused: invalid_settings", file=sys.stderr)
        sys.exit(2)
    args.profile = selection["profile"]
    if args.show_profile:
        print(json.dumps(selection))
        return
    try:  # cold: before manifest authority, config and index
        cengine = capability_module("context_engine")
        capability_module("context_tokens")
        cpolicy = capability_module("context_policy")
        def requested_profile():
            mode = auto_tool_mode(args.model) if args.tool_mode == "auto" else args.tool_mode
            route = ((None if args.no_stream else oneshot_stream_route(args.model))
                     if args.oneshot else agent_stream_route(args.model, mode, args.no_stream))
            effort, _ = effective_effort(args.model, args.effort)
            return provider_profile(args.model, route or "chat", effort)

        args.context_selection = cengine.select(
            args, cpolicy, oneshot=args.oneshot,
            frozen=has_manifest or approved_worker or args.recovery_contract is not None,
            profile_supplier=requested_profile)
        if not args.oneshot and args.context_selection.policy is not None:
            capability_module("context_runtime")
            capability_module("context_receipts")
            capability_module("command_output")
            capability_module("context_storage")
            if args.context_selection.policy.auto_compact:
                capability_module("context_compaction")
    except CapabilityUnavailable:
        print("error: context policy refused: module_unavailable", file=sys.stderr)
        sys.exit(2)
    except cengine.Refused as e:
        print(f"error: context policy refused: {e.code}", file=sys.stderr)
        sys.exit(2)
    manifest = None
    manifest_v2 = False
    if args.supervisor_manifest is not None or args.supervisor_manifest_sha256 is not None:
        sp = capability_module("supervisor_policy")
        sm2 = capability_module("supervisor_manifest_v2")
        try:
            if args.supervisor_manifest is None or args.supervisor_manifest_sha256 is None:
                raise sp.ManifestError("supervisor manifest and sha256 must be given together")
            # Pin bounded RAW bytes before even selecting the schema; never downgrade.
            raw = sm2.read_pinned(args.supervisor_manifest, args.supervisor_manifest_sha256,
                                  sm2.MAX_MANIFEST_BYTES, "supervisor manifest")
            header = json.loads(raw.decode("utf-8"))
            schema = header.get("schema") if isinstance(header, dict) else None
            if schema == sp.SCHEMA:
                if args.profile != "legacy":
                    ap.error("supervisor manifest-v1 does not authorize resilient-v1")
                manifest = sp.load_manifest(args.supervisor_manifest,
                                            args.supervisor_manifest_sha256)
            elif schema == sm2.SCHEMA:
                if args.profile != "resilient-v1":
                    ap.error("supervisor manifest-v2 requires resilient-v1")
                manifest = sm2.load_manifest2(args.supervisor_manifest,
                                             args.supervisor_manifest_sha256)
                manifest_v2 = True
            else:
                raise sp.ManifestError("unsupported manifest schema")
        except (sp.ManifestError, sm2.ManifestError) as exc:
            if _LAUNCH is not None:
                _LAUNCH.reason = "policy_error"
            # Preserve the original v1 loader's fixed diagnostic vocabulary.
            print(f"error: supervisor refused: {exc}", file=sys.stderr)
            sys.exit(2)
        except (UnicodeError, ValueError, TypeError):
            if _LAUNCH is not None:
                _LAUNCH.reason = "policy_error"
            print("error: supervisor refused: invalid_manifest", file=sys.stderr)
            sys.exit(2)
    if args.profile != "legacy":
        capability_module("paged_tools")
        capability_module("tool_output")
        capability_module("command_output")
    recovery_contract = None
    recovery_runtime = None
    supervisor_identity = None
    if args.profile == "resilient-v1" and args.worktree:
        try:
            args.label = _recovery_worktree_label(args.label)
        except ValueError:
            ap.error("resilient-v1 requires a nonempty worktree label")
        if os.path.lexists(os.path.join(
                os.path.realpath(args.root), ".claude", "worktrees", args.label)):
            ap.error("resilient-v1 refuses to recreate an existing worktree label")
    supervisor_boundary = None
    supervisor_worktree = None
    if args.supervisor_manifest is not None or args.supervisor_manifest_sha256 is not None:
        sp = capability_module("supervisor_policy")
        sl2 = capability_module("supervisor_launch_v2") if manifest_v2 else sp
        try:
            if args.supervisor_manifest is None or args.supervisor_manifest_sha256 is None:
                raise sp.ManifestError("supervisor manifest and sha256 must be given together")
            supervisor_root = os.path.realpath(args.root)
            policy_pin = manifest.policy_sha256 if manifest_v2 else manifest["policy_sha256"]
            if sp.policy_sha256(supervisor_root, POLICY_FILE) != policy_pin:
                raise sp.ManifestError("repository policy digest does not match the manifest")
            load_policy(supervisor_root)
            import fnmatch

            def inherited_permitted(rule, argv):
                return len(argv) >= len(rule) and all(
                    fnmatch.fnmatch(argv[i], pattern) for i, pattern in enumerate(rule))

            supervisor_mode = ("oneshot" if args.oneshot else
                               auto_tool_mode(args.model) if args.tool_mode == "auto"
                               else args.tool_mode)
            if manifest_v2:
                owner_pid, owner_launch = os.getpid(), _LAUNCH

                # Requires exclusive parent-owned candidate/admin; not an OS sandbox.
                def trusted_internal_guard():
                    return os.getpid() == owner_pid and _LAUNCH is owner_launch

                supervisor_boundary = sm2.validate_launch2(
                    manifest, args, manifest_path=args.supervisor_manifest,
                    main_root=supervisor_root, policy_file=POLICY_FILE,
                    launch_id=args.launch_id,
                    run_id=run_id_for(args.transcript) if args.transcript else "",
                    mode=supervisor_mode, allowed_cmds=ALLOWED_CMDS,
                    permitted=inherited_permitted,
                    verify_seed=lambda m, root: sl2.verify_seed2(
                        m, root, owner_guard=trusted_internal_guard))
                if manifest.role == "worker":
                    supervisor_identity = sl2.identity_provider2(
                        manifest, owner_guard=trusted_internal_guard)
            else:
                supervisor_boundary = sp.validate_launch(
                    manifest, args, manifest_path=args.supervisor_manifest,
                    main_root=supervisor_root, policy_file=POLICY_FILE,
                    launch_id=args.launch_id,
                    run_id=run_id_for(args.transcript) if args.transcript else "",
                    mode=supervisor_mode, allowed_cmds=ALLOWED_CMDS,
                    permitted=inherited_permitted)
                if args.worktree:
                    supervisor_worktree = sp.create_owned_worktree(
                        supervisor_root, args.label, manifest["baseline_commit"])
        except (sp.ManifestError, sm2.ManifestError, sl2.ManifestError) as exc:
            if _LAUNCH is not None:
                _LAUNCH.reason = "policy_error"
            print(f"error: supervisor refused: {exc}", file=sys.stderr)
            sys.exit(2)
    if args.recovery_contract is not None:
        recovery_mod = capability_module("recovery_runtime")
        try:
            if (args.profile != "resilient-v1" or args.oneshot
                    or not args.launch_id or not args.launch_receipt or not args.transcript):
                raise recovery_mod.RecoveryRuntimeError("invalid_contract")
            recovery_contract = recovery_mod.read_contract(args.recovery_contract)
            if recovery_contract[0]["schema_version"] == 1:
                if not args.worktree or manifest_v2:
                    raise recovery_mod.RecoveryRuntimeError("invalid_contract")
            elif (not manifest_v2 or supervisor_boundary is None
                    or supervisor_boundary.manifest is not manifest
                    or manifest.role != "worker" or not args.write or args.worktree
                    or args.profile != manifest.profile):
                raise recovery_mod.RecoveryRuntimeError("invalid_contract")
        except recovery_mod.RecoveryRuntimeError as exc:
            if _LAUNCH is not None:
                _LAUNCH.reason = "policy_error"
            print(f"error: recovery contract refused: {exc.code}", file=sys.stderr)
            sys.exit(2)
    if _LAUNCH is None:
        runs_dir = os.path.join(os.path.realpath(args.root), ".claude", "agent-runs")
        try:
            os.makedirs(runs_dir, exist_ok=True)
        except OSError as exc:
            print(f"error: cannot create launch receipt directory {runs_dir}: "
                  f"{type(exc).__name__}", file=sys.stderr)
            sys.exit(2)
        auto_id = str(uuid.uuid4())
        stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d-%H%M%S")
        _LAUNCH = LaunchReceipt(
            os.path.join(runs_dir, f"{stamp}-{auto_id}.launch.jsonl"), auto_id)
        _startup_diagnostics()
        print(f"launch receipt: {_LAUNCH.path} launch_id={auto_id}",
              file=sys.stderr, flush=True)

    if args.oneshot:
        if (args.write or args.worktree or args.patch is not None
                or args.max_iters is not None or args.soft_iters is not None
                or args.tool_mode != "auto" or args.allow_prompt_tools
                or args.verify_command or args.review_finish or args.supervise_stuck):
            print("error: --oneshot sends one request without tools; drop "
                  "--write/--worktree/--patch/--max-iters/--tool-mode/--allow-prompt-tools/"
                  "--soft-iters/--verify-command/--review-finish/--supervise-stuck",
                  file=sys.stderr)
            sys.exit(2)
        args.max_iters = 1
        args.patch = "agent.patch"
        verification_plan = capability_module("finish_gate").VerificationPlan()
    else:
        try:
            verification_plan = capability_module("finish_gate").VerificationPlan.from_cli(
                args.verify_command)
        except ValueError as exc:
            ap.error(str(exc))
        if args.allow_prompt_tools and args.tool_mode != "prompt":
            ap.error("--allow-prompt-tools needs --tool-mode prompt")
        if (args.tool_mode == "prompt" and auto_tool_mode(args.model) == "native"
                and not args.allow_prompt_tools):
            _LAUNCH.reason = "native_tools_required"
            ap.error(
                f"{args.model} supports native tools. Remove --tool-mode prompt "
                "or use --tool-mode native. Native tool calling is not a built-in "
                "subagent. A gateway timeout is not evidence of missing tool support. "
                "For an operator-approved compatibility test, add --allow-prompt-tools. "
                "Do not add this flag automatically to bypass this check. "
                "Read ~/.claude/DELEGATION.md.")
        if args.attach_image:
            print("error: --attach-image needs --oneshot; agents use the view_image "
                  "tool instead", file=sys.stderr)
            sys.exit(2)
        if args.max_iters is None:
            args.max_iters = 30
        if args.max_iters < 1:
            ap.error("--max-iters must be a positive integer")
        work = turn_budget(args.max_iters)[0]
        if args.soft_iters is not None and not 1 <= args.soft_iters <= work:
            ap.error(f"--soft-iters must be between 1 and {work} (--max-iters "
                     f"{args.max_iters} minus the closeout and report turns)")
        if args.patch is None:
            args.patch = "agent.patch"

    if args.task_file is not None:
        try:
            args.task = read_task_file(args.task_file)
        except OSError as exc:
            ap.error(f"cannot read --task-file {args.task_file}: {exc.strerror or exc}")
        except ValueError as exc:
            ap.error(f"--task-file {args.task_file}: {exc}")

    if args.text_only and args.attach_image:
        ap.error("--text-only refuses --attach-image; no image was loaded")

    if args.oneshot:
        # Built before config, gateway or index are touched: a bad attachment
        # exits 2 with nothing sent and nothing registered.
        if args.context_selection.policy is not None:  # model-v1: token meter, no byte cap
            oneshot_prompt = build_oneshot_prompt(args.task, args.attach, max_chars=None)
        else:
            oneshot_prompt = build_oneshot_prompt(args.task, args.attach)
        oneshot_images = prepare_oneshot_images(args.attach_image)
        if oneshot_images and image_text_cap_error(len(oneshot_prompt)):
            print(f"error: {image_text_cap_error(len(oneshot_prompt))}. Shorten the "
                  "task or attachments, or split the request.", file=sys.stderr)
            sys.exit(2)
        mode = "oneshot"
    else:
        mode = args.tool_mode
    if mode == "auto":
        mode = auto_tool_mode(args.model)
    # Native tools now work with effort for every model (nested reasoning.effort
    # for GPT), so the effort is no longer forced to none in native mode. Only a
    # value this model rejects is swapped, and args.effort becomes the effective
    # value so the transcript header, index and footer record what was sent.
    effort, substituted = effective_effort(args.model, args.effort)
    if substituted:
        print(f"note: {args.model} does not accept effort '{args.effort}'; "
              f"using '{effort}'", file=sys.stderr)
        args.effort = effort

    main_root = os.path.realpath(args.root)
    # Policy comes from the MAIN repo, before anything else reads the boundary: a
    # worktree is a throwaway checkout of this repo and inherits its rules, so
    # loading from the worktree would just find the same file one copy removed --
    # and would silently fall back to the built-ins if the policy were untracked.
    # It is validated (MCP/skills included) before the gateway is contacted or a
    # worktree exists, so a bad policy costs nothing and leaves nothing behind.
    policy_note = load_policy(main_root)
    print(policy_note, file=sys.stderr, flush=True)
    skill_bodies = preload_skills(main_root, args.skill)
    if args.oneshot and skill_bodies:
        oneshot_prompt = skill_prompt(skill_bodies) + oneshot_prompt

    def initialize_gateway():
        url, key = load_cfg()
        ids = refresh_models(url, key)
        if isinstance(ids, set) and args.model not in ids:
            print(f"warning: {args.model} is not in the gateway's model list; "
                  f"continuing (its replicas may be out of sync)", file=sys.stderr)
        return url, key

    if recovery_contract is None:
        url, key = initialize_gateway()
    wt_path = None
    wt_baseline_head = None
    if args.worktree:
        wt_path, wt_branch = (
            supervisor_worktree if supervisor_worktree is not None
            else make_worktree(main_root, args.label, destructive=False)
            if args.profile == "resilient-v1"
            else make_worktree(main_root, args.label))
        wt_baseline_head = capability_module("run_review").worktree_head(wt_path)
    tpath = args.transcript or os.path.join(
        args.root, ".claude", "agent-runs",
        f"{datetime.datetime.now():%Y%m%d-%H%M%S}-{args.label}.md")
    # MCP mutation needs an explicit --write; a worktree alone never grants it,
    # because MCP servers act outside the disposable checkout.
    run = Runner((supervisor_boundary.candidate_root if manifest_v2 else None)
                 or wt_path or args.root, "disk" if (wt_path or args.write) else "stage",
                 mcp_policy=None if args.oneshot or supervisor_boundary is not None else MCP_POLICY,
                 skills_dirs=SKILLS_DIRS, allow_mcp_mutation=bool(args.write),
                 artifact_dir=(tpath[:-3] if tpath.endswith(".md") else tpath)
                 + ".artifacts", skills_root=main_root, verification_plan=verification_plan,
                 text_only=args.text_only, boundary=supervisor_boundary,
                 profile=args.profile, read_files_enabled=args.read_files)
    if recovery_contract is not None:
        recovery_mod = capability_module("recovery_runtime")
        try:
            if manifest_v2:
                # main() may bootstrap the native receipt after boundary preflight.
                owner_launch = _LAUNCH
            recovery_runtime = recovery_mod.RecoveryRuntime.prepare(
                args.recovery_contract, parsed=recovery_contract,
                run_id=run_id_for(tpath), task_text=args.task,
                policy_root=main_root,
                baseline_commit=(manifest.candidate.baseline_commit
                                 if manifest_v2 and manifest.candidate.kind == "git"
                                 else wt_baseline_head),
                candidate_root=run.root, launch_id=args.launch_id,
                artifact_dir=run.caps.artifact_dir, plan=verification_plan,
                **({"boundary": supervisor_boundary,
                    "identity_provider": supervisor_identity} if manifest_v2 else {}))
        except recovery_mod.RecoveryRuntimeError as exc:
            if _LAUNCH is not None:
                _LAUNCH.reason = "policy_error"
            print(f"error: recovery contract refused: {exc.code}", file=sys.stderr)
            sys.exit(2)
        url, key = initialize_gateway()
    for name in args.skill:
        run.caps.skills().load(name, max_chars=MAX_READ_CHARS)
    loaded_line = "- loaded skills: " + (", ".join(args.skill) or "(none)")
    steer_path = tpath + ".steer"
    if args.oneshot:
        writes_label = "none"
        # The brief is the task text; attachments are named, never inlined.
        brief = args.task + ("\n\nAttached files:\n" + "\n".join(
            f"- {p}" for p in args.attach) if args.attach else "")
        if oneshot_images:
            brief += "\n\nAttached images:\n" + "\n".join(
                f"- {a.describe()}" for a in oneshot_images)
    else:
        writes_label = "disk" if (wt_path or args.write) else "staged (patch only)"
        brief = args.task
    tr = Transcript(tpath, "\n".join([
        f"# {args.label}", "",
        f"- model: `{args.model}`  effort: `{args.effort}`  tools: `{mode}`",
        f"- writes: `{writes_label}`"
        f"  root: `{os.path.relpath(wt_path, main_root) if wt_path else main_root}`",
        f"- {policy_note}",
        loaded_line,
        f"- steer: write guidance into `{os.path.basename(steer_path)}` to redirect mid-run",
        "", "## Task", "", brief, "", "---"]))
    print(f"transcript: {tpath}", file=sys.stderr, flush=True)

    # Sibling files next to the transcript.
    anomaly_path = tpath[:-3] + ".anomalies.jsonl" if tpath.endswith(".md") \
        else tpath + ".anomalies.jsonl"
    messages_path = tpath[:-3] + ".messages.jsonl" if tpath.endswith(".md") \
        else tpath + ".messages.jsonl"
    usage_path = tpath[:-3] + ".usage.jsonl" if tpath.endswith(".md") \
        else tpath + ".usage.jsonl"

    # Machine-wide run index. A repo-level cost ledger discovers runs by scanning
    # <root>/.claude/agent-runs/; a transcript redirected outside that directory
    # (via --transcript, or -C pointing outside a repo) is invisible to it. Warn
    # loudly so it can never happen silently, then register the run here where it
    # will be found regardless.
    index_dir = os.path.realpath(os.path.join(main_root, ".claude", "agent-runs"))
    idx_path = run_index_path()
    if os.path.realpath(os.path.dirname(tpath)) != index_dir:
        print("warning: transcript " + os.path.abspath(tpath) + " is not inside "
              + index_dir + "; a repo-level cost ledger scanning .claude/agent-runs/ "
              "will NOT discover this run. It is registered in the machine-wide "
              "index at " + idx_path + ".", file=sys.stderr, flush=True)
    run_id = run_id_for(tpath)
    DIAGNOSTICS = diagnostic_journal(tpath + ".engine.diagnostics.jsonl", run_id=run_id,
        launch_id=_LAUNCH.launch_id, engine_pid=os.getpid(), execution_profile=args.profile)
    if args.oneshot:
        write_mode = "none"
    else:
        write_mode = "disk" if (wt_path or args.write) else "staged"
    lifecycle = RunLifecycle(run_id, idx_path, args.model,
                             repo_index_path=repo_run_index_path(tpath))
    registered = lifecycle.begin(
        harness_version=HARNESS_VERSION, label=args.label, effort=args.effort,
        tool_mode=mode, root=main_root, cwd=os.path.realpath(os.getcwd()),
        transcript=os.path.abspath(tpath), messages=os.path.abspath(messages_path),
        usage_journal=os.path.abspath(usage_path), write_mode=write_mode,
        max_iters=args.max_iters)
    # Receipt is terminal for startup either way; accounting failure must not kill the run.
    if registered:
        _LAUNCH.write("ready", run_id=run_id, root=main_root, label=args.label,
                      transcript=os.path.realpath(tpath),
                      index_paths=[os.path.realpath(p) for p in lifecycle.index_paths])
        _LAUNCH.done = True
        print(f"LAUNCH_READY launch_id={_LAUNCH.launch_id} run_id={run_id} "
              f"receipt={_LAUNCH.path}", file=sys.stderr, flush=True)
    else:
        _LAUNCH.write("unregistered", reason_code="index_unwritable")
        _LAUNCH.done = True
        print(f"LAUNCH_UNREGISTERED launch_id={_LAUNCH.launch_id} "
              f"receipt={_LAUNCH.path}", file=sys.stderr, flush=True)

    def journal_usage(iter_no, phase, resp, started=None):
        # One durable, sanitized row per response or usage-bearing failed stream.
        # Loop and wrap-up share this single point so each is journalled exactly
        # once; failed attempts without usage add no extra row.
        raw_usage = resp.get("usage") if isinstance(resp, dict) else None
        append_usage_record(
            usage_path,
            iteration=iter_no,
            phase=phase,
            model=args.model,
            timestamp=datetime.datetime.now(datetime.timezone.utc)
            .strftime("%Y-%m-%dT%H:%M:%SZ"),
            raw_usage=raw_usage,
            started=started,
        )

    # Message journalling: every message appended to msgs is written here as one
    # JSON line, write-only, for post-mortem analysis of a dead run.
    #
    # A --resume flag is deliberately NOT implemented: replaying a journalled turn
    # would re-execute tools that already wrote to disk (write_file, replace_in_file,
    # run_command), which needs idempotency keys before it is safe.
    def journal(m):
        fields = dict(phase="checkpoint", turn=run.current_turn,
                      call_index=getattr(run, "_diagnostic_call_index", None) or None)
        diagnostic("checkpoint_start", **fields)
        try:
            with open(messages_path, "a", encoding="utf-8") as fh:
                line = json.dumps(sanitize_message(m))
                if tr.scrub_images:
                    line = scrub_data_urls(line)
                fh.write(line + "\n")
        except BaseException as exc:
            diagnostic("checkpoint_end", outcome="raised", **fields, **_safe_exception_fields(exc))
            raise
        diagnostic("checkpoint_end", outcome="returned", **fields)

    rt = None
    context_skills = {}
    if not args.oneshot and args.context_selection.policy is not None:
        storage = capability_module("context_storage").OwnedContextStorage(
            run.caps, capability_module("command_output").read_page, block_bytes=65536)
        rt = capability_module("context_runtime").ContextRuntime(
            args.context_selection.policy, load_component=capability_module,
            model=args.model,
            route=agent_stream_route(args.model, mode, args.no_stream) or "chat",
            effort=args.effort, image_type=ImageAsset, journal=journal,
            publisher=storage.publish, reader=storage.read,
            external_bindings=lambda: {
                **cengine.semantic_bindings(
                    run, task=args.task, steers=operator_guidance_list,
                    steer_path=steer_path, policy=(ALLOWED_CMDS, sorted(SKIP_DIRS), MAX_READ_CHARS,
                                                   MCP_POLICY, SKILLS_DIRS),
                    protocol=protocol_health, review=review_result,
                    correction=correction_attempted, review_attempted=review_attempted),
                "context_policy": args.context_selection.metadata()})
        run.context_runtime = rt
        run._commands = capability_module("command_output")
        context_scopes.enter_context(cengine.runtime_scope(rt))
    guard = None

    def request_fits(payload, phase="worker"):
        if rt is None:
            return cg_mod.fits(payload, cg_mod.REQUEST_LIMIT - cg_mod.CLOSEOUT_RESERVE,
                               image_type=ImageAsset)
        try:
            return cg_mod.fits(payload, None, image_type=ImageAsset, context=rt.meter_for(phase))
        except ContextBudgetError as exc:
            guard.stop(exc.reason, provider=exc.provider)
            return False

    def add_msg(m, *, pinned=False):
        # Engine notices and repairs need admission too. Native tool results are
        # already reserved by Guard and must always pair their admitted call IDs.
        if guard is not None and m.get("role") != "tool":
            prospective = {"model": args.model, "messages": [*msgs, m], "stream": False}
            if mode == "native":
                prospective["tools"] = native_tools(run)
                prospective["tool_choice"] = "auto"
            if rt is not None:
                prospective = apply_effort(prospective, args.model, args.effort)
            if not request_fits(prospective):
                guard.stop("request context")
                if rt is None:
                    guard.rejected += cg_mod.request_bytes(m, image_type=ImageAsset)
                return False
        if rt is None:
            msgs.append(m)
            journal(m)
        else:
            rt.append(m, pinned=pinned)
            msgs[:] = rt.payload_messages()
        return True

    if args.oneshot:
        try:
            run_oneshot(args, url, key, oneshot_prompt, tr, tpath, anomaly_path,
                        lifecycle, journal, journal_usage, oneshot_images)
        except ContextBudgetError:
            report = ("[PARTIAL - context_budget]\nLOCAL report: request admission or provider "
                      "context limit stopped this one-shot. No tools ran and no edits were made. "
                      "The requested answer is unverified; shorten the input before retrying.")
            print(report)
            tr.write("\n" + report)
            tr.close()
            lifecycle.finish("failed", tool_calls=0, refused=0,
                             stop_reason="context_budget")
            sys.exit(3)
        return

    attach_capability_observers(run, tr, usage_path, args.model, tpath)
    msgs = []
    add_msg({"role": "system", "content": prompt_system(args.model, run)
             if mode == "prompt" else base_rules(args.model, run)})
    if skill_bodies:
        add_msg({"role": "user", "content": skill_prompt(skill_bodies).rstrip()}, pinned=True)
    add_msg({"role": "user", "content": args.task}, pinned=True)
    if args.model.startswith(CLAUDE_PREFIX):
        # A system-only bound still timed out; keep this beside the task, before the first cut.
        add_msg({"role": "user", "content": CLAUDE_FIRST_STEP})
    sent = recv = 0

    def token_note():
        # Footer keeps the old text; an unknown total is shown as unknown, never as 0.
        if sent is None or recv is None:
            return ""          # unknown total: show no numbers (index end record holds null)
        return f", {sent:,} tok sent / {recv:,} tok returned" if sent or recv else ""

    def fail_image_agent(exc):
        if not history_images(msgs):
            raise exc
        detail = scrub_data_urls(str(exc.code))
        error = (f"error: {args.model} image-bearing request failed; "
                 f"images were not dropped.\n{detail}")
        print(error, file=sys.stderr, flush=True)
        tr.write("\n**Model request failed**\n\n" + error)
        tail = (f"[{args.model} e={args.effort} tools={mode}: {run.calls} tool calls"
                f"{f', {run.refused} refused' if run.refused else ''}{token_note()}]")
        tr.write("\n" + tail)
        tr.close()
        print(tail, file=sys.stderr)
        lifecycle.finish(
            "failed", tool_calls=run.calls, refused=run.refused, sent=sent, recv=recv
        )
        sys.exit(1)

    def add_tokens(resp):
        # Footer/index totals from the normalized counters, so both usage
        # shapes count: Chat Completions (prompt_/completion_tokens, Claude) and
        # Responses (input_/output_tokens, GPT on the upgraded gateway).
        # One unmeasured attempt makes the complete total unknown (None), never a subtotal.
        nonlocal sent, recv
        n = normalize_usage(resp.get("usage") if isinstance(resp, dict) else None)
        known = {k: type(n[k]) is int and n[k] >= 0 for k in ("sent", "recv")}
        sent = sent + n["sent"] if known["sent"] and sent is not None else None
        recv = recv + n["recv"] if known["recv"] and recv is not None else None

    final = None
    nudges = 0        # consecutive replies that carried no tool action
    cuts = 0          # consecutive cut replies, each answered with a correction
    repairs = 0       # correction requests this run; never reset (REPAIR_LIMIT)
    stop_reason = None  # set when the run must end incomplete (STOP_REASONS)
    recovery_last_exit_code = None

    def recovery_dispatch(name, cargs):
        nonlocal stop_reason, recovery_last_exit_code
        if recovery_runtime is None:
            result = run.dispatch(name, cargs)
            if (rt is not None and rt.policy.auto_compact and name == "load_skill"
                    and cargs.get("name") in run.caps.loaded_skills()
                    and cargs["name"] not in context_skills):
                context_skills[cargs["name"]] = str(result)
            return result
        if run.outcome_unknown:
            return OUTCOME_UNKNOWN_SKIPPED
        argv = cargs.get("argv")
        verification = (
            name == "run_command"
            and isinstance(argv, (list, tuple))
            and all(isinstance(arg, str) for arg in argv)
            and tuple(argv) in verification_plan.commands)
        try:
            pending = recovery_runtime.before_call(
                name, run.evidence, verification=verification)
        except capability_module("recovery_runtime").RecoveryRuntimeError:
            stop_reason = "recovery_state"
            return "NOT RUN: recovery_state; no action taken; not re-run."
        if name == "run_command":
            recovery_last_exit_code = None
        try:
            result = run.dispatch(name, cargs)
            if name == "run_command" and isinstance(result, str):
                match = re.match(r"exit=(-?\d{1,12})(?:\r?\n|$)", result)
                if match:
                    recovery_last_exit_code = int(match.group(1))
            return result
        finally:
            recovery_runtime.after_call(
                pending, run.evidence, verification=verification)
            if recovery_runtime.blocked and not run.outcome_unknown:
                stop_reason = "recovery_state"

    cg_mod = capability_module("context_guard")
    guard = cg_mod.Guard(image_type=ImageAsset, profile=args.profile)
    work, closeout = turn_budget(args.max_iters)
    soft = args.soft_iters if args.soft_iters is not None else work
    turn = 0          # worker turn ordinal; reviewer calls share the total budget
    fg_mod = capability_module("finish_gate")
    rr_mod = capability_module("run_review")
    traj_mod = _optional_module("trajectory")
    protocol_health = (capability_module("native_batch").RepairHealth(REPAIR_LIMIT)
                       if args.profile == "resilient-v1" else None)
    protocol_health_saved = None
    repair_turn, repair_allowed = None, True

    def persist_protocol_health(force=False):
        nonlocal protocol_health_saved
        if protocol_health is None:
            return
        metadata = protocol_health.metadata()
        if not force and metadata == protocol_health_saved:
            return
        import tempfile
        path = os.path.splitext(os.fspath(tpath))[0] + ".protocol-health.json"
        temporary = None
        try:
            fd, temporary = tempfile.mkstemp(
                prefix=".protocol-health-", dir=os.path.dirname(path) or ".")
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(metadata, fh, indent=2, sort_keys=True)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(temporary, path)
            temporary = None
        except OSError:
            tr.write("\n_[protocol health] sidecar persistence failed_")
            return
        finally:
            if temporary is not None:
                try:
                    os.unlink(temporary)
                except OSError:
                    pass
        tr.write("\n_[protocol health] " +
                 json.dumps(metadata, sort_keys=True) + "_")
        protocol_health_saved = metadata

    def protocol_observation():
        evidence = run.evidence
        try:
            gate = fg_mod.evaluate_finish(evidence.snapshot(), evidence.plan)
            return evidence.mutation_seq, gate.to_metadata()
        except Exception:
            # Failed advisory observation is not evidence of progress or acceptance.
            return evidence.mutation_seq, None

    persist_protocol_health()
    advisories = []
    last_advisor_turn = -1000
    operator_guidance_list = []
    model_calls_used = 0
    correction_attempted = False
    review_attempted = False
    review_result = None
    last_candidate_input = None

    def _capture_candidate(ver_status):
        nonlocal last_candidate_input
        cand = rr_mod.capture_review_input(
            run_id=run_id, task_text=args.task, transcript_path=tpath,
            root=main_root, write_mode=write_mode,
            candidate_seq=run.candidate_seq, mutation_seq=run.evidence.mutation_seq,
            verification_status=ver_status, runner=run, wt_path=wt_path,
            expected_wt_path=wt_path, baseline_head=wt_baseline_head,
            worktree_diff_fn=worktree_diff, artifact_dir=run.caps.artifact_dir,
            operator_guidance=operator_guidance_list)
        if isinstance(cand, rr_mod.ReviewInput) and run.caps.artifact_dir:
            try:
                cand = rr_mod.save_candidate_artifact(run.caps.artifact_dir, cand)
            except OSError:
                pass
        last_candidate_input = cand
        return cand

    def _run_readonly_review(candidate, iter_no):
        nonlocal model_calls_used

        def _generate_readonly(req):
            nonlocal model_calls_used
            model_calls_used += 1
            rev_payload = apply_effort(
                {"model": args.model, "messages": req["messages"], "stream": False},
                args.model, args.effort)
            # Never attach tools or tool_choice to reviewer payload.
            rev_started = datetime.datetime.now(datetime.timezone.utc).strftime(
                "%Y-%m-%dT%H:%M:%SZ")
            try:
                with cengine.request_scope("reviewer") as ctx:
                    resp = chat_with_model_retry(url, key, rev_payload, iter_no)
                    if ctx is not None:
                        ctx.observe(resp.get("usage"))
            except ContextBudgetError as exc:
                guard.stop(exc.reason, provider=exc.provider)
                raise
            for usage in resp.get("_earlier_usage") or []:
                er = {"usage": usage}
                journal_usage(iter_no, "review", er, started=rev_started)
                add_tokens(er)
            journal_usage(iter_no, "review", resp, started=rev_started)
            add_tokens(resp)
            lifecycle.update(tool_calls=run.calls, refused=run.refused, sent=sent, recv=recv)
            return resp

        return rr_mod.review_run(candidate, generate_readonly=_generate_readonly)

    def _run_readonly_advisor(adv_input, iter_no):
        nonlocal model_calls_used

        def _generate_readonly(req):
            nonlocal model_calls_used
            model_calls_used += 1
            adv_payload = apply_effort(
                {"model": args.model, "messages": req["messages"], "stream": False},
                args.model, args.effort)
            # Never attach tools or tool_choice to advisor payload.
            adv_started = datetime.datetime.now(datetime.timezone.utc).strftime(
                "%Y-%m-%dT%H:%M:%SZ")
            try:
                with cengine.request_scope("advisor") as ctx:
                    resp = chat_with_model_retry(url, key, adv_payload, iter_no)
                    if ctx is not None:
                        ctx.observe(resp.get("usage"))
            except ContextBudgetError as exc:
                guard.stop(exc.reason, provider=exc.provider)
                raise
            for usage in resp.get("_earlier_usage") or []:
                er = {"usage": usage}
                journal_usage(iter_no, "advisor", er, started=adv_started)
                add_tokens(er)
            journal_usage(iter_no, "advisor", resp, started=adv_started)
            add_tokens(resp)
            lifecycle.update(tool_calls=run.calls, refused=run.refused, sent=sent, recv=recv)
            return resp

        return traj_mod.consult_advisor(adv_input, generate_readonly=_generate_readonly)

    def _maybe_supervise_stuck(iter_no):
        nonlocal last_advisor_turn
        new_suspicions = run.drain_turn_suspicions()
        if guard.closing() or run.outcome_unknown or not args.supervise_stuck or not new_suspicions or traj_mod is None or run.trajectory is None:
            return
        fresh = [s for s in new_suspicions if s.state_epoch == run.trajectory.state_epoch]
        if not fresh:
            return
        if len(advisories) >= traj_mod.ADVISOR_MAX_INTERVENTIONS:
            return
        if (iter_no - last_advisor_turn) < traj_mod.ADVISOR_COOLDOWN_TURNS:
            return
        if model_calls_used >= work + closeout:
            return
        s = fresh[-1]
        snap = run.evidence.snapshot(tuple(run.pending))
        gate = fg_mod.evaluate_finish(snap, verification_plan)
        cand = _capture_candidate(gate.verification_status)
        patch_text = cand.patch_text if isinstance(cand, rr_mod.ReviewInput) else ""
        patch_sha = cand.binding.patch_sha256 if isinstance(cand, rr_mod.ReviewInput) else ""
        adv_input = traj_mod.AdvisorInput(
            run_id=run_id, turn=iter_no, suspicion=s,
            state_epoch=run.trajectory.state_epoch,
            task_text=args.task, patch_text=patch_text, write_mode=write_mode,
            candidate_seq=run.candidate_seq, mutation_seq=run.evidence.mutation_seq,
            patch_sha256=patch_sha)
        last_advisor_turn = iter_no
        adv_result = _run_readonly_advisor(adv_input, iter_no)
        advisories.append(adv_result)
        if guard.closing():
            return
        if (adv_result.status == "advise"
                and adv_result.state_epoch == run.trajectory.state_epoch):
            notice = traj_mod.format_advisor_notice(adv_result)
            add_msg({"role": "user", "content": notice})
            tr.write(f"\n_[advisor suggestion] {adv_result.action_code} ({s.code})_")

    def evaluate_finish_proposal(iter_no, summary):
        nonlocal correction_attempted, review_attempted, review_result
        snap = run.evidence.snapshot(tuple(run.pending))
        gate = fg_mod.evaluate_finish(snap, verification_plan)
        if (gate.correction_useful and not correction_attempted
                and model_calls_used < work + closeout):
            correction_attempted = True
            missing_cmds = [
                " ".join(verification_plan.commands[cid])
                for cid in gate.missing_check_ids
                if 0 <= cid < len(verification_plan.commands)]
            reasons_str = ", ".join(gate.reason_codes) or gate.verification_status
            cmds_str = "; ".join(missing_cmds)
            msg_text = (
                f"not finished: factual verification incomplete ({reasons_str})"
                + (f" for required command(s): {cmds_str}" if cmds_str else "")
                + ". Run the required verification command(s) against the current edits, "
                "then call finish again.")
            return False, msg_text
        if args.review_finish and not review_attempted:
            review_attempted = True
            candidate = _capture_candidate(gate.verification_status)
            if isinstance(candidate, rr_mod.ReviewUnavailable):
                review_result = rr_mod.review_run(candidate, generate_readonly=lambda _req: {})
            elif model_calls_used >= work + closeout:
                review_result = rr_mod.ReviewResult(
                    "unavailable", (), candidate.binding, "unavailable", "budget_exhausted")
            else:
                review_result = _run_readonly_review(candidate, iter_no)
            if (review_result.verdict == "concerns" and not correction_attempted
                    and model_calls_used < work + closeout):
                correction_attempted = True
                return False, rr_mod.format_concerns_notice(review_result)
        if guard.closing() or run.outcome_unknown:
            return False, "not finished: context budget or uncertain mutation stopped the run"
        return True, "ok"

    def budget_notice(kind, text):
        tr.write(f"\n_[turn budget] {kind}_")
        add_msg({"role": "user", "content": f"[turn budget] {kind}: {text}"})

    def take_repair(i, cause):
        # Legacy keeps its run-wide allowance; resilient wire health is local.
        nonlocal repairs, stop_reason, repair_turn, repair_allowed
        if protocol_health is not None and repair_turn != i:
            repair_turn = i
            repair_allowed = protocol_health.incident(cause)
            persist_protocol_health()
        if i >= work:
            stop_reason = "max_iters"  # repairs must not consume closeout reserves
            return False
        if protocol_health is not None:
            if repair_allowed:
                return True
        elif repairs < REPAIR_LIMIT:
            repairs += 1
            return True
        stop_reason = "repair_limit"
        note = f"[{i:>2}] {cause}: correction allowance ({REPAIR_LIMIT}) used up; stopping"
        tr.write(f"\n**{note}**")
        print("  " + note, file=sys.stderr, flush=True)
        return False

    streaming = TurnStreaming(agent_stream_route(args.model, mode, args.no_stream))

    def sync_steers():
        if os.path.exists(steer_path):
            with open(steer_path, encoding="utf-8") as source:
                guidance = source.read().strip()
            if guidance:
                operator_guidance_list.append(guidance)
                run.candidate_seq += 1
                if run.trajectory is not None:
                    run.trajectory.advance_epoch()
                # Authority pins must survive even when the old view no longer fits.
                message = {"role": "user", "content": f"[operator guidance] {guidance}"}
                if rt is None:
                    add_msg(message)
                else:
                    rt.append(message, pinned=True)
                    msgs[:] = rt.payload_messages()
                tr.write(f"\n### [{run.current_turn}] OPERATOR GUIDANCE\n\n> {guidance}")
            with open(steer_path, "w"):
                pass

    def render_worker(messages):
        packet = apply_effort({"model": args.model, "messages": messages, "stream": False},
                              args.model, args.effort)
        if mode == "native":
            packet.update(tools=native_tools(run), tool_choice="auto")
        return prepared_packet(packet)

    def prepared_packet(packet):
        if streaming.route == "responses":
            return responses_payload(packet)
        return {**packet, "stream": bool(streaming.route)}

    def prepare_summary(messages, reply_tokens):
        packet = apply_effort({"model": args.model, "messages": list(messages), "stream": False,
                               "max_completion_tokens": reply_tokens}, args.model, args.effort)
        return prepared_packet(packet)

    def call_summary(request):
        nonlocal model_calls_used
        model_calls_used += 1
        packet = apply_effort({"model": request.model, "messages": list(request.messages),
                               "stream": False, "max_completion_tokens": request.reply_tokens},
                              request.model, request.effort)
        return call_model(url, key, packet, "oneshot", None, None, run.current_turn,
                          TurnStreaming(request.route) if streaming.route else None,
                          max_attempts=1, _maintenance=True)[0]

    def charge_summary(response, error):
        for usage in getattr(error, "_earlier_usage", []):
            journal_usage(run.current_turn, "compaction", {"usage": usage})
            add_tokens({"usage": usage})
        if error is not None and isinstance(getattr(error, "usage", None), dict):
            response = {"usage": error.usage}
        journal_usage(run.current_turn, "compaction", response)
        add_tokens(response)
        lifecycle.update(tool_calls=run.calls, refused=run.refused, sent=sent, recv=recv)

    auto = None
    if rt is not None and rt.policy.auto_compact:
        auto = cengine.AutomaticCompaction(
            rt, capability_module("context_compaction"), prepare=prepare_summary,
            render=render_worker, call=call_summary,
            persist=lambda record: run.caps.save_artifact(
                json.dumps({**record, "trust": "UNTRUSTED"}).encode(), ".ctxjson"),
            # Decline before the RPC once a streamed-form rejection moved the run off its
            # bound route: maintenance must never be certified under an identity it did not use.
            charge=charge_summary, can_call=lambda: model_calls_used < work + closeout
            and (streaming.route or "chat") == rt.route,
            sync_steers=sync_steers, image_type=ImageAsset)

    for i in range(1, work + closeout + 1):
        if guard.closing():
            stop_reason = "context_budget"
            break
        if model_calls_used >= work + closeout:
            stop_reason = "max_iters"
            break
        turn = i
        run.current_turn = i
        run._diagnostic_call_index = 0
        diagnostic("turn_started", phase="turn", turn=i)
        if i == soft + 1 and soft < work:
            budget_notice("checkpoint", f"you have used {soft} work turns. A continuation "
                          f"of up to {work - soft} more work turns is preauthorized, then "
                          f"{closeout} closeout turns and one final report with no tools. "
                          "Keep to the task's scope.")
        if work - i + 1 == 3 or (i == 1 and work < 3):
            budget_notice("warning", f"{work - i + 1} work turn(s) left before closeout "
                          f"({closeout} closeout turns, then a final report with no tools). "
                          "Prioritize what is needed to finish and verify.")
        if i == work + 1:
            budget_notice("closeout", f"work turns are used up. You have {closeout} "
                          "closeout turn(s) with tools, then a final report with no tools. "
                          "Start no new scope: run any needed verification, fix and "
                          "recheck only what it shows, then call finish.")
        sync_steers()

        if guard.closing():
            stop_reason = "context_budget"
            break

        # Rebuilt each turn: MCP tools discovered or skills loaded last turn are
        # advertised (and therefore callable) from this turn on.
        system = dict(msgs[0], content=prompt_system(args.model, run)
                      if mode == "prompt" else base_rules(args.model, run))
        if rt is None:
            msgs[0] = system
        else:
            if auto is not None:
                system["content"] += "".join("\n\n" + body for body in context_skills.values())
                system["content"] += "\n\n[ENGINE STATE: authoritative metadata, not summary proof]\n" + json.dumps(
                    rt.bindings()["external"], sort_keys=True)
            if system != msgs[0]:
                rt.replace_system(system)
            msgs[:] = rt.payload_messages()
        payload = apply_effort({"model": args.model,
                               "messages": msgs if rt is None else rt.payload_messages(),
                               "stream": False}, args.model, args.effort)
        if mode == "native":
            payload["tools"] = native_tools(run)
            payload["tool_choice"] = "auto"
        if rt is not None:
            payload = copy.deepcopy(payload)
        if auto is not None:
            auto.attempt()
            msgs[:] = rt.payload_messages()
            payload["messages"] = rt.payload_messages()
            if model_calls_used >= work + closeout:
                stop_reason = "max_iters"
                break
        if not request_fits(prepared_packet(payload) if rt is not None else payload):
            # Refuse the request rather than send one that may overflow; history is kept.
            guard.stop("request context")
            if rt is None:
                print(f"  [{i:>2}] context budget: request ~{cg_mod.request_bytes(payload, image_type=ImageAsset)} "
                      "bytes exceeds the admission limit; closing out", file=sys.stderr, flush=True)
            stop_reason = "context_budget"
            break

        call_started = datetime.datetime.now(datetime.timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ")
        model_calls_used += 1
        try:
            resp, ok, cause, elapsed = call_model(url, key, payload, mode, tr, anomaly_path, i,
                                                 streaming)
        except ContextBudgetError as exc:
            for usage in getattr(exc, "_earlier_usage", []):
                journal_usage(i, "loop", {"usage": usage}, started=call_started)
                add_tokens({"usage": usage})
            guard.stop(exc.reason, provider=exc.provider)
            stop_reason = "context_budget"
            break
        except SystemExit as exc:
            for usage in getattr(exc, "_earlier_usage", []):
                journal_usage(i, "loop", {"usage": usage}, started=call_started)
                add_tokens({"usage": usage})
            lifecycle.update(tool_calls=run.calls, refused=run.refused, sent=sent, recv=recv)
            stream_error = getattr(exc, "_stream_error", None)
            if stream_error is not None and stream_error.reason == "response_incomplete":
                tr.write("\n[PARTIAL - response_incomplete]")
                print(f"error: {stream_error.detail}. Transcript: {tpath}", file=sys.stderr)
                stop_reason = "response_incomplete"
                break
            if getattr(exc, "_transport_rejected", False):
                tr.close()
                lifecycle.finish("failed", tool_calls=run.calls, refused=run.refused,
                                 sent=sent, recv=recv, stop_reason="transport")
                raise
            if not history_images(msgs):
                raise
            fail_image_agent(exc)
        if not isinstance(resp, dict):
            resp = {"usage": None}    # malformed final response: one attempt, usage unknown
        for usage in resp.get("_earlier_usage") or []:
            earlier_resp = {"usage": usage}
            journal_usage(i, "loop", earlier_resp, started=call_started)
            add_tokens(earlier_resp)
        journal_usage(i, "loop", resp, started=call_started)
        add_tokens(resp)
        # Keep the lifecycle guard's counters current so a crash mid-run still
        # records accurate tokens in its safety end record.
        lifecycle.update(tool_calls=run.calls, refused=run.refused, sent=sent, recv=recv)
        # resp may be the transient sentinel (no "choices") when ok is False, so
        # extract defensively rather than indexing -- otherwise the very timeout
        # we are hardening against would crash here instead of being retried.
        msg = (first_message(resp) or (None, {}))[1]
        content = msg.get("content") if isinstance(msg.get("content"), str) else ""
        # A streamed turn already wrote its note live (LA §3.2): don't repeat it.
        streamed = isinstance(resp, dict) and bool(resp.get("_live"))
        shown = None if streamed else content

        calls = msg.get("tool_calls") or []
        diagnostic("response_accepted", phase="response", turn=i)
        native_verdict = None
        preflight_refused = False
        preflight_repair_allowed = True
        if (protocol_health is not None or rt is not None) and mode == "native":
            raw_calls = msg.get("tool_calls")
            if raw_calls is not None and (
                    raw_calls or not isinstance(raw_calls, list)):
                native_verdict, pairable = _native_batch_preflight(
                    run, raw_calls,
                    cg_mod.BATCH_BUDGET // cg_mod.COMPACT_RESULT)
                if not native_verdict.ok:
                    preflight_refused = True
                    preflight_repair_allowed = take_repair(
                        i, "invalid_arguments" if pairable else "protocol_error")
                    if not pairable:
                        tr.write("\n_[native protocol] unpairable packet rejected_")
                        if not preflight_repair_allowed:
                            break
                        if not add_msg({
                                "role": "user",
                                "content": (
                                    "[native protocol] Invalid tool-call packet. "
                                    "Return a corrected batch with unique nonempty "
                                    "IDs, known tool names, function objects and "
                                    "string JSON arguments. No calls from the "
                                    "rejected packet were executed.")}):
                            stop_reason = "context_budget"
                            break
                        continue
                calls = raw_calls
        replay = THINK_BLOCK.sub("", content).strip() if mode == "native" else content
        envelope = {"role": "assistant", "content": replay}
        if calls:
            envelope["tool_calls"] = calls
        result_envelopes = [{"role": "tool", "tool_call_id": c["id"], "content": ""}
                            for c in calls] if mode == "native" else []
        if rt is None and not guard.start_batch(
                [payload, envelope], len(calls) if mode == "native" else 1,
                result_envelopes=result_envelopes):
            stop_reason = "context_budget"
            break
        diagnostic("batch_validated", phase="batch", turn=i,
                   batch_size=len(calls) if isinstance(calls, list) else 0,
                   outcome="error" if preflight_refused or not ok else "returned")

        if not ok and i >= work:
            stop_reason = "max_iters"
            break
        if not ok and cause in ("reply_cut", "prose_only", "no_tool_block"):
            if not take_repair(i, cause):
                break
        if not ok and cause == "reply_cut":
            cuts += 1
            if cuts <= CUT_CORRECTIONS_PER_TURN:
                note = (f"[{i:>2}] reply cut by the gateway's 2-minute limit after "
                        f"{elapsed:.0f} s; asking for a smaller step")
                tr.write(f"\n_{note}_")
                print("  " + note, file=sys.stderr, flush=True)
                add_msg({"role": "assistant", "content": CUT_PLACEHOLDER})
                add_msg({"role": "user", "content": CUT_CORRECTION})
                continue
        if not ok and cause in ("prose_only", "no_tool_block"):
            note = f"[{i:>2}] reply had no tool call; asking for one"
            tr.write(f"\n_{note}_")
            print("  " + note, file=sys.stderr, flush=True)

        if not ok:
            # The reply failed after retries, or needs a tool-call nudge. Do NOT end the run on
            # gateway garbage. Nudge: tell the model it called no tool so nothing
            # ran, and it must call exactly one tool or finish. Allow at most 2
            # such nudges; on the third give-up, stop the loop and fall through to
            # the iteration-cap wrap-up so a partial report is still produced.
            nudges += 1
            if nudges >= 3:
                tr.write("\n**model returned no tool action 3 times; giving up "
                         "and requesting a wrap-up**")
                print(f"  [{i:>2}] no tool action after retries x3; wrapping up",
                      file=sys.stderr, flush=True)
                stop_reason = "no_action_limit"
                break
            tr.step(i, shown, "NO_TOOL_ACTION", {}, "nudging for a tool call")
            add_msg({"role": "assistant", "content": content})
            add_msg({"role": "user", "content":
                     "You called no tool, so nothing ran. Call exactly one tool "
                     "now to make progress, or call finish if you are genuinely "
                     "done."})
            continue
        nudges = 0
        cuts = 0

        if mode == "native":
            calls = msg.get("tool_calls")
            # ok is True, so calls is non-empty here (advances() guarantees it).
            # Reasoning models surface <think> blocks in content. Replaying those
            # verbatim into the next request feeds the upstream API a thinking block
            # it did not author and cannot verify, which is a plausible cause of the
            # gateway returning a bare "Error:" a few turns in. Send the prose only.
            replay = THINK_BLOCK.sub("", content).strip()
            envelope = {"role": "assistant", "content": replay, "tool_calls": calls}
            slot_ids = [f"turn_{i}_slot_{j}" for j in range(len(calls))]
            if rt is not None:
                if not rt.begin_batch(payload, envelope, result_envelopes, slot_ids):
                    stop_reason = "context_budget"
                    break
                guard = rt.guard
            elif not add_msg(envelope) and protocol_health is not None:
                stop_reason = "context_budget"
                break
            stop = False
            executed, viewed, finish_at = [], [], None
            deferred_finishes = set()
            bad_args = preflight_refused
            health_before = (protocol_observation()
                             if protocol_health is not None and not preflight_refused
                             else None)
            health_acted = False
            multi_refused = (protocol_health is None
                             and args.model.startswith(CLAUDE_PREFIX) and len(calls) > 1)
            for call_index, c in enumerate(calls):
                run._diagnostic_next_index = call_index + 1
                name = c["function"]["name"]
                result_envelope = {"role": "tool", "tool_call_id": c["id"]}
                if (run.outcome_unknown
                        or (recovery_runtime is not None and recovery_runtime.blocked)
                        or guard.closing()
                        or multi_refused or preflight_refused):
                    if rt is not None:
                        if run.outcome_unknown:
                            code = "outcome_unknown"
                        elif recovery_runtime is not None and recovery_runtime.blocked:
                            code = "recovery_state"
                        elif guard.closing():
                            code = "context_budget"
                        elif preflight_refused:
                            run.refused += 1
                            code = "invalid_arguments"
                        else:
                            run.refused += 1
                            code = "multiple_tool_calls"
                        result = rt.skip_result(slot_ids[call_index], code)
                    elif run.outcome_unknown:
                        result = guard.skip(result_envelope, OUTCOME_UNKNOWN_SKIPPED)
                    elif recovery_runtime is not None and recovery_runtime.blocked:
                        result = guard.skip(
                            result_envelope,
                            "NOT RUN: recovery_state; action deferred; not re-run.")
                    elif guard.closing():
                        result = guard.skip(result_envelope)
                    elif preflight_refused:
                        run.refused += 1
                        result = guard.skip(
                            result_envelope, "error: native batch failed preflight; "
                            "no calls in this batch were executed")
                    else:
                        run.refused += 1
                        result = guard.admit(name, CLAUDE_MULTI_REFUSED,
                                             envelope=result_envelope)
                    executed.append((c, name, {}, result))
                    tr.step(i, shown, name, {}, result)
                    shown = None
                    continue
                additions = ()
                try:
                    if protocol_health is not None or rt is not None:
                        cargs = native_verdict.calls[call_index].args
                    else:
                        cargs = checked_tool_arguments(
                            name, json.loads(c["function"]["arguments"] or "{}"))
                except (ValueError, TypeError) as e:
                    result = f"error: unparseable arguments: {e}"
                    cargs = {}
                    bad_args = True
                    tr.start_step(i, shown, name, cargs)
                    shown = None
                else:
                    if name == "finish":
                        finish_at = len(executed)
                        deferred_finishes.add(finish_at)
                        result = "ok"
                        if rt is not None:
                            result = rt.admit_result(
                                "finish", "ok", slot_id=slot_ids[call_index],
                                provisional=True, outcome=None, exit_code=None)
                        if protocol_health is not None or rt is not None:
                            # Preserve the result reservation until all actions settle.
                            executed.append((c, name, cargs, result))
                            continue
                    else:
                        if rt is not None and not rt.before_action(slot_ids[call_index]):
                            result = rt.skip_result(slot_ids[call_index], "context_budget")
                            executed.append((c, name, cargs, result))
                            tr.step(i, shown, name, cargs, result)
                            shown = None
                            continue
                        tr.start_step(i, shown, name, cargs)
                        shown = None
                        result = recovery_dispatch(name, cargs)
                        if protocol_health is not None and not run.outcome_unknown:
                            health_acted = True
                        additions = ()
                        if isinstance(result, ImageAsset):
                            previous = len(viewed)
                            result = admit_image(msgs, viewed, c["id"], result)
                            additions = (({"role": "user", "content": viewed_images_content(viewed[previous:])},)
                                         if len(viewed) > previous else ())
                            if rt is None:
                                result = guard.admit(name, result, envelope=result_envelope,
                                                     additions=additions)
                                if guard.closing():
                                    del viewed[previous:]
                                tr.step_result(result)
                                executed.append((c, name, cargs, result))
                                continue
                if rt is not None:
                    outcome, exit_code = "returned", None
                    if name == "run_command":
                        info = run._last_cmd_info
                        if info[0] in ("timed_out", "outcome_unknown"):
                            outcome = "unknown"
                        elif info[1] is False:
                            outcome = "skipped"
                        else:
                            exit_code = info[2]
                    producer_result = result
                    result = rt.admit_result(
                        name, str(result), slot_id=slot_ids[call_index], outcome=outcome,
                        exit_code=exit_code, additions=additions,
                        compact=run.compact_result(name, producer_result))
                else:
                    result = guard.admit(name, result, envelope=result_envelope,
                                         compact=run.compact_result(name, result))
                if len(executed) not in deferred_finishes:
                    tr.step_result(result)
                executed.append((c, name, cargs, result))
            if finish_at is not None:
                c, name, cargs, previous = executed[finish_at]
                if (protocol_health is not None
                        and not guard.closing() and not run.outcome_unknown
                        and not (recovery_runtime is not None and recovery_runtime.blocked)):
                    health_acted = True
                if (guard.closing() or run.outcome_unknown
                        or (recovery_runtime is not None and recovery_runtime.blocked)):
                    executed[finish_at] = (c, name, cargs,
                        "not finished: recovery_state; not re-run."
                        if (recovery_runtime is not None and recovery_runtime.blocked
                            and not run.outcome_unknown)
                        else "not finished: a budget was exhausted or a mutation outcome is unknown")
                elif viewed:
                    # The pixels have not been seen yet: finishing now would
                    # report on an image the model never looked at.
                    executed[finish_at] = (c, name, cargs, FINISH_DEFERRED)
                elif bad_args:
                    executed[finish_at] = (c, name, cargs,
                        "not finished: this batch contained invalid tool arguments; "
                        "correct them before reporting success")
                else:
                    accepted, fin_msg = evaluate_finish_proposal(i, cargs.get("summary", ""))
                    if accepted:
                        final = cargs.get("summary", "")
                        stop = True
                    else:
                        executed[finish_at] = (c, name, cargs, fin_msg)
                c, name, cargs, result = executed[finish_at]
                if rt is not None:
                    for index in sorted(deferred_finishes):
                        fc, fname, fargs, prior = executed[index]
                        admitted = rt.replace_result(
                            fname, prior, result, slot_id=slot_ids[index],
                            outcome="returned" if stop else "skipped", exit_code=None)
                        executed[index] = (fc, fname, fargs, admitted)
                elif protocol_health is not None:
                    # One factual last-proposal decision; pair every deferred call.
                    for index in sorted(deferred_finishes):
                        fc, fname, fargs, _ = executed[index]
                        admitted = guard.admit(
                            fname, result,
                            envelope={"role": "tool", "tool_call_id": fc["id"]})
                        executed[index] = (fc, fname, fargs, admitted)
                else:
                    result = guard.replace_result(name, previous, result,
                        envelope={"role": "tool", "tool_call_id": c["id"]})
                    executed[finish_at] = (c, name, cargs, result)
            for index, (c, name, cargs, result) in enumerate(executed):
                run._diagnostic_call_index = index + 1
                if index in deferred_finishes:
                    tr.step(i, shown, name, cargs, result)
                    shown = None
                recorded = (True if rt is not None else add_msg(
                    {"role": "tool", "tool_call_id": c["id"], "content": result}))
                diagnostic("result_recorded", phase="result", turn=i, call_index=index + 1,
                           tool_name=diag_tool_name(name),
                           outcome="ok" if recorded is not False else "error")
            if viewed:
                tr.scrub_images = True
                if rt is None:
                    add_msg({"role": "user", "content": viewed_images_content(viewed)})
            if rt is not None:
                rt.cancel_remaining()
                rt.end_batch()
                msgs[:] = rt.payload_messages()
            if health_before is not None and health_acted and not run.outcome_unknown:
                health_after = protocol_observation()
                protocol_health.action(
                    task_changed=(health_before[0] != health_after[0]),
                    evidence_changed=(health_before[1] is not None
                                      and health_after[1] is not None
                                      and health_after[1] != health_before[1]))
                persist_protocol_health()
            if run.outcome_unknown:
                final, stop_reason = None, "mutation_outcome_unknown"
                break
            if recovery_runtime is not None and recovery_runtime.blocked:
                final, stop_reason = None, "recovery_state"
                break
            if guard.closing():
                # finish in an exhausted batch must not turn it into success.
                final, stop_reason = None, "context_budget"
                break
            if multi_refused and not take_repair(i, "multiple_tool_calls"):
                break
            if stop:
                break
            if bad_args and not (
                    preflight_repair_allowed if preflight_refused
                    else take_repair(i, "invalid_arguments")):
                break
            _maybe_supervise_stuck(i)
        else:
            # ok is True, so a tool block is present here (advances() guarantees it).
            m = TOOL_BLOCK.search(content)
            narration = content[:m.start()].strip()
            try:
                call = json.loads(m.group(1))
                name = call["name"]
                cargs = checked_tool_arguments(name, call.get("args", {}))
            except Exception as e:
                add_msg({"role": "assistant", "content": content})
                add_msg({"role": "user",
                         "content": f"That tool block was unparseable ({e}). "
                                    "Re-emit exactly one valid ```tool block."})
                tr.step(i, None if streamed else narration, "PARSE_ERROR", {}, str(e))
                if not take_repair(i, "invalid_arguments"):
                    break
                continue
            slot_id = f"turn_{i}_slot_0"
            if rt is not None:
                if not rt.begin_batch(payload, {"role": "assistant", "content": content},
                                      [{"role": "user", "content": ""}], [slot_id],
                                      kind="prompt"):
                    stop_reason = "context_budget"
                    break
                guard = rt.guard
            if name == "finish":
                if rt is not None:
                    prior = rt.admit_result("finish", "ok", slot_id=slot_id,
                                            provisional=True, outcome=None)
                accepted, fin_msg = evaluate_finish_proposal(i, cargs.get("summary", ""))
                if rt is not None:
                    fin_msg = rt.replace_result(
                        "finish", prior, f"Result of finish:\n\n{fin_msg}", slot_id=slot_id,
                        outcome="returned" if accepted else "skipped")
                    rt.end_batch()
                    msgs[:] = rt.payload_messages()
                else:
                    fin_msg = guard.admit("finish", fin_msg, envelope={"role": "user"},
                                          additions="Result of finish:\n\n")
                if guard.closing():
                    stop_reason = "context_budget"
                    break
                tr.step(i, None if streamed else narration, "finish", {}, fin_msg)
                if accepted:
                    final = cargs.get("summary", "")
                    break
                if rt is None:
                    add_msg({"role": "assistant", "content": content})
                    add_msg({"role": "user", "content": f"Result of finish:\n\n{fin_msg}"})
                continue
            if rt is not None and not rt.before_action(slot_id):
                rt.skip_result(slot_id, "context_budget")
                rt.end_batch()
                msgs[:] = rt.payload_messages()
                stop_reason = "context_budget"
                break
            tr.start_step(i, None if streamed else narration, name, cargs)
            result = recovery_dispatch(name, cargs)
            if recovery_runtime is not None and (
                    run.outcome_unknown or recovery_runtime.blocked):
                tr.step_result(result if isinstance(result, str)
                               else "action executed; recovery state requires reconciliation")
                final, stop_reason = None, (
                    "mutation_outcome_unknown" if run.outcome_unknown else "recovery_state")
                break
            viewed = []
            result_envelope = {"role": "user"}
            if rt is not None:
                producer_result = result
                additions = ()
                if isinstance(result, ImageAsset):
                    result = admit_image(msgs, viewed, None, result)
                    if viewed:
                        additions = ({"role": "user", "content": viewed_images_content(viewed)},)
                        tr.scrub_images = True
                outcome, exit_code = "returned", None
                if name == "run_command":
                    info = run._last_cmd_info
                    outcome = "unknown" if info[0] in ("timed_out", "outcome_unknown") else (
                        "skipped" if info[1] is False else "returned")
                    exit_code = info[2] if outcome == "returned" else None
                result = rt.admit_result(
                    name, f"Result of {name}:\n\n{result}", slot_id=slot_id,
                    outcome=outcome, exit_code=exit_code, additions=additions,
                    compact=run.compact_result(name, producer_result))
                rt.end_batch()
                msgs[:] = rt.payload_messages()
            elif isinstance(result, ImageAsset):
                result = admit_image(msgs, viewed, None, result)
                addition = ({"role": "user", "content": viewed_images_content(viewed)}
                            if viewed else ())
                result = guard.admit(name, result, envelope=result_envelope, additions=addition)
                if guard.closing():
                    viewed.clear()
                if viewed:
                    tr.scrub_images = True
            else:
                result = guard.admit(name, result, envelope=result_envelope,
                                     additions=f"Result of {name}:\n\n",
                                     compact=run.compact_result(name, result))
            tr.step_result(result)
            recorded = True
            if rt is None:
                add_msg({"role": "assistant", "content": content})
                recorded = add_msg({"role": "user",
                                    "content": user_content(f"Result of {name}:\n\n{result}",
                                                            [a for _, a in viewed])})
            diagnostic("result_recorded", phase="result", turn=i, call_index=1,
                       tool_name=diag_tool_name(name),
                       outcome="ok" if recorded is not False else "error")
            if run.outcome_unknown:
                stop_reason = "mutation_outcome_unknown"
                break
            if guard.closing():
                stop_reason = "context_budget"
                break
            _maybe_supervise_stuck(i)
    else:
        stop_reason = "max_iters"   # work and closeout turns used without a finish()
    if guard.closing() and stop_reason != "mutation_outcome_unknown":
        final, stop_reason = None, "context_budget"

    if recovery_runtime is not None:
        diagnostic("checkpoint_start", phase="checkpoint", turn=run.current_turn)
        try:
            recovery_runtime.checkpoint(run.evidence)
        except BaseException as exc:
            diagnostic("checkpoint_end", phase="checkpoint", outcome="raised",
                       turn=run.current_turn, **_safe_exception_fields(exc))
            raise
        diagnostic("checkpoint_end", phase="checkpoint", outcome="returned", turn=run.current_turn)
        if run.outcome_unknown:
            final, stop_reason = None, "mutation_outcome_unknown"
        elif recovery_runtime.blocked:
            final, stop_reason = None, "recovery_state"

    if (rt is not None and stop_reason == "context_budget"
            and (rt.reason or guard.reason) not in (None, "model_window_full", "request context",
                                                    "provider context", "request")):
        stop_reason = "context_admission"

    def partial_report():
        # The reserved final turn, inside --max-iters: tools are not offered and
        # any tool call in the reply is ignored. No model request follows it.
        report_turn = max(turn, model_calls_used) + 1
        why = STOP_REASON_TEXT[stop_reason]
        tr.write(f"\n**run incomplete ({stop_reason}): {why}; requesting the final report**")
        print(f"  [{report_turn:>2}] {stop_reason}; asking for a partial report",
              file=sys.stderr, flush=True)
        budget_notice("final report", f"the run stopped incomplete: {why}. No tools "
                      "remain. Write your final report now in plain prose from what you "
                      "established. Say which parts are incomplete or unverified, and "
                      "what the next safe step is.")
        report_messages = list(msgs)
        report_messages[0] = dict(msgs[0], content=msgs[0]["content"] +
            "\n\nFINAL REPORT MODE: For this last request only, tools are disabled. "
            "The previous tool-only response format does not apply: "
            "do not call finish or emit a tool block. Write a concise plain-prose "
            "partial report distinguishing observed facts from unverified work.")
        wrap = apply_effort({"model": args.model, "messages": report_messages, "stream": False},
                            args.model, args.effort)
        call_started = datetime.datetime.now(datetime.timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ")
        resp, report, failed_usage = {}, "", []
        try:
            if guard.provider_rejected or not request_fits(wrap, "report"):
                raise ContextBudgetError("report reserve")
            resp = call_model(url, key, wrap, "oneshot", tr, anomaly_path, report_turn)[0]
            if not isinstance(resp, dict) or not resp:
                resp = {"usage": None}    # a returned attempt with unknown usage still counts
        except SystemExit as exc:
            failed_usage = getattr(exc, "_earlier_usage", [])
            detail = scrub_data_urls(str(exc.code))
            what = ("image-bearing report request failed; images were not dropped"
                    if history_images(msgs) else "report request failed")
            print(f"error: {args.model} {what}.\n{detail}", file=sys.stderr, flush=True)
            tr.write(f"\n**Model request failed**\n\nerror: {args.model} {what}.\n{detail}")
        except Exception as exc:
            failed_usage = getattr(exc, "_earlier_usage", [])
            # A report failure must not erase the primary incomplete outcome.
            note = f"final report unavailable ({type(exc).__name__}); using local fallback"
            print(note, file=sys.stderr, flush=True)
            tr.write(f"\n**{note}**")
        for usage in failed_usage + (resp.get("_earlier_usage") or []):
            journal_usage(report_turn, "wrapup", {"usage": usage}, started=call_started)
            add_tokens({"usage": usage})
        if resp:
            journal_usage(report_turn, "wrapup", resp, started=call_started)
            add_tokens(resp)
        shaped = first_message(resp)
        content = shaped[1].get("content") if shaped else None
        report = content.strip() if isinstance(content, str) else ""
        report = TOOL_BLOCK.sub("", report).strip()    # strip a stray block if emitted
        if not report or BARE_ERROR.fullmatch(report):
            report = (f"LOCAL factual report: {run.calls} tool call(s) ran. {patch_note} "
                      "Completion and any unlisted checks are unverified. "
                      f"Read the transcript ({tpath}) for the recorded actions and results.")
        else:
            guard.report_source = "remote"
        return (f"[PARTIAL - {stop_reason}: {why}]\n"
                 "Next safe action: review the transcript and any patch or worktree "
                 "changes before you rerun or continue; do not assume unlisted work "
                 "was verified.\n\n" + report)

    patch_saved = False
    patch_note = "No staged patch was pending; any direct disk edits remain in the checkout."

    def save_patch():
        nonlocal patch_saved, patch_note
        if patch_saved:
            return
        if wt_path:
            diff = worktree_diff(wt_path)
            patch_abs = os.path.join(main_root, args.patch)
            with open(patch_abs, "w", encoding="utf-8", newline="") as fh:
                fh.write(diff)
            files = diff.count("\n+++ ")
            note = (f"worktree {wt_path} on branch {wt_branch}: {files} file(s) changed "
                    f"-> {args.patch}")
            tr.write(f"\n**{note}**")
            print(f"\n[{note}]", file=sys.stderr)
            if not diff.strip():
                print("warning: the agent changed nothing in the worktree", file=sys.stderr)
        elif run.pending:
            with open(args.patch, "w", encoding="utf-8", newline="") as fh:
                fh.write(run.patch())
            note = f"staged {len(run.pending)} file(s) -> {args.patch}"
            tr.write(f"\n**{note}** (review: `git apply --check {args.patch}`)")
            print(f"\n[{note}; review with: git apply --check {args.patch}]", file=sys.stderr)
        if wt_path or run.pending:
            patch_note = f"Patch evidence saved to {args.patch}; review it before applying or continuing."
        patch_saved = True

    def save_results():
        nonlocal review_result
        save_patch()
        if final:
            print(final)
            tr.write("\n---\n\n## Final report\n\n" + final)

        snap = run.evidence.snapshot(tuple(run.pending))
        gate_result = fg_mod.evaluate_finish(snap, verification_plan)
        final_candidate = _capture_candidate(gate_result.verification_status)
        if review_result is not None:
            current_binding = (final_candidate.binding
                               if isinstance(final_candidate, rr_mod.ReviewInput) else None)
            review_result = review_result.with_current_binding(current_binding)
        ver_block = fg_mod.render_verification_block(gate_result, verification_plan, snap)
        rev_block = (
            rr_mod.render_review_block(review_result, current_candidate_seq=run.candidate_seq)
            if (args.review_finish and review_result is not None) else None)
        emit_status_blocks = bool(
            verification_plan.commands or args.review_finish or snap.mutation_seq > 0
            or snap.staged_paths or snap.uncertain_mutation)
        usable_final = bool((final or "").strip()) and not BARE_ERROR.fullmatch(final or "")
        if emit_status_blocks:
            tr.write("\n" + ver_block)
            if usable_final:
                print("\n" + ver_block)
        if rev_block is not None:
            tr.write("\n" + rev_block)
            if usable_final:
                print(rev_block)
        if run.caps.artifact_dir:
            try:
                os.makedirs(run.caps.artifact_dir, exist_ok=True)
                comp_record = {
                    "run_id": run_id,
                    "context_budget": guard.metadata(),
                    **({"context_runtime": rt.metadata(),
                        "context_policy": args.context_selection.metadata()} if rt is not None else {}),
                    "verification_status": gate_result.verification_status,
                    "gate": gate_result.to_metadata(),
                    "run_binding": (
                        final_candidate.binding.run.to_metadata()
                        if isinstance(final_candidate, rr_mod.ReviewInput) else None),
                    "patch_binding": (
                        final_candidate.binding.to_metadata()
                        if isinstance(final_candidate, rr_mod.ReviewInput) else None),
                    "candidate_unavailable": (
                        final_candidate.to_metadata()
                        if isinstance(final_candidate, rr_mod.ReviewUnavailable) else None),
                    "review": review_result.to_metadata() if review_result is not None else None,
                    "correction_attempted": correction_attempted,
                    "review_attempted": review_attempted,
                    "suspicions": run.trajectory.to_metadata() if run.trajectory is not None else [],
                    "advisories": [
                        a.with_current_epoch(run.trajectory.state_epoch if run.trajectory is not None else 0).to_metadata()
                        for a in advisories
                    ],
                }
                if args.profile == "resilient-v1":
                    persist_protocol_health(force=True)
                    comp_record["protocol_health"] = protocol_health.metadata()
                    comp_record.update(
                        recovery_runtime.metadata() if recovery_runtime is not None
                        else capability_module("recovery_runtime").unbound_metadata())
                comp_path = os.path.join(run.caps.artifact_dir, "completion.json")
                with open(comp_path, "w", encoding="utf-8") as cfh:
                    json.dump(comp_record, cfh, indent=2, sort_keys=True)
            except OSError:
                pass

        tail = (f"[{args.model} e={args.effort} tools={mode}: {run.calls} tool calls"
                f"{f', {run.refused} refused' if run.refused else ''}{token_note()}]")
        tr.write("\n" + tail)
        tr.close()
        print(tail, file=sys.stderr)

    try:
        save_patch()
        if stop_reason:
            if stop_reason == "response_incomplete":
                final = f"LOCAL partial report: {patch_note} Completion remains unverified."
            elif recovery_runtime is None:
                final = partial_report()
            else:
                final = (
                    f"LOCAL partial report\nStop: {stop_reason}\n"
                    f"Recovery: {recovery_runtime.error or 'bound'}\n"
                    f"Handoff SHA256: {recovery_runtime.handoff_sha256 or 'unavailable'}\n"
                    "Recorded effects and exits are not re-run. Task acceptance is "
                    "unverified; coordinator review is required.")
                if recovery_last_exit_code is not None:
                    final += f"\nLast observed command exit: {recovery_last_exit_code}"
        save_results()
    except Exception as exc:
        if not stop_reason:
            raise
        try:
            print(f"error: incomplete run output failed ({type(exc).__name__}); "
                  "the report or patch may not have been saved completely. "
                  f"Inspect the transcript and worktree: {tpath}", file=sys.stderr)
        except OSError:
            pass
        sys.exit(1 if stop_reason == "response_incomplete" else 3)
    finally:
        if stop_reason:
            lifecycle.finish("failed", tool_calls=run.calls, refused=run.refused,
                             sent=sent, recv=recv, stop_reason=stop_reason)
            try:
                tr.close()
            except Exception:
                pass

    unusable = not (final or "").strip() or BARE_ERROR.fullmatch(final)
    status = "failed" if (stop_reason or unusable) else "complete"
    lifecycle.finish(
        status, tool_calls=run.calls, refused=run.refused, sent=sent, recv=recv,
        stop_reason=stop_reason,
    )
    if stop_reason:
        # Semantic partials retain their provider-failure exit; local limits use 3.
        print(f"error: run incomplete ({stop_reason}). Transcript: {tpath}",
              file=sys.stderr)
        sys.exit(1 if stop_reason == "response_incomplete" else 3)

    # A blank or bare-"Error:" report means the gateway or context limit killed the
    # run. Exiting 0 there would let fanout count it as a success and hide the loss.
    if unusable:
        print("error: model returned no usable report -- likely a context-limit or "
              f"gateway failure. Transcript: {tpath}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()

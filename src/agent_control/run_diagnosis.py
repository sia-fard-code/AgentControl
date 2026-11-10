"""Bounded metadata evidence, not a replacement for recorded run status."""
from __future__ import annotations

from collections import Counter, deque
from datetime import datetime, timedelta, timezone
import json
import math
import os
from pathlib import Path
import re
import stat
from typing import get_args

from .models import RunRecord, StopReason

MAX_DIAGNOSTIC_BYTES = 256 * 1024
MAX_DIAGNOSTIC_ROWS = 2048
_STOP_REASONS = frozenset(get_args(StopReason))
_TOOLS = frozenset({"read_file", "write_file", "replace_in_file", "list_dir", "grep",
                    "run_command", "finish", "view_image", "load_skill", "list_skills",
                    "list_mcp_tools", "read_command_output", "read_agent_transcript", "mcp"})
_SOURCES = frozenset({"delegate.py", "harness_diagnostics.py", "context_guard.py",
    "command_output.py", "harness_mcp.py", "harness_skills.py", "native_batch.py",
    "resilient_tools.py", "execution_profile.py", "recovery_runtime.py", "paged_tools.py",
    "tool_output.py"})
_SOURCES |= frozenset({"agent_await.py", "ask-model.py", "fanout.py", "process_control.py",
    "harness_process_identity.py", "harness_process_control.py", "artifact_reader.py",
    "engine_steer.py", "model_registry.py", "patch_safety.py", "protocol_health.py",
    "recovery_artifacts.py", "recovery_candidate.py", "recovery_evidence.py", "recovery_handoff.py",
    "recovery_verification.py", "runtime_git_observation.py", "runtime_observation.py",
    "runtime_progress.py", "skill_usage.py", "trajectory.py", "verification.py",
    "candidate.py", "finish_gate.py", "harness_setup.py", "init-agent-harness.py",
    "run_review.py", "supervise.py", "task_status.py", "supervisor_continuation_v3.py",
    "supervisor_contract.py", "supervisor_contract_v3.py", "supervisor_evidence.py",
    "supervisor_harness.py", "supervisor_harness_v3.py", "supervisor_journal.py",
    "supervisor_launch_v2.py", "supervisor_manifest_v2.py", "supervisor_policy.py",
    "supervisor_process.py", "supervisor_state.py"})
# Mirrors engine/harness_diagnostics.py BUNDLE_SOURCES context modules (pinned by test).
_SOURCES |= frozenset({"context_policy.py", "context_tokens.py", "context_receipts.py",
    "context_admission.py", "context_runtime.py", "context_compaction.py", "context_engine.py",
    "context_storage.py"})
_CLASSES = frozenset({"Exception", "RuntimeError", "ValueError", "TypeError", "KeyError",
    "IndexError", "AttributeError", "AssertionError", "OSError", "IOError", "EOFError",
    "BrokenPipeError", "ConnectionError", "ConnectionResetError", "PermissionError",
    "FileNotFoundError", "TimeoutError", "TimeoutExpired", "MemoryError", "RecursionError",
    "UnicodeError", "UnicodeEncodeError", "UnicodeDecodeError", "JSONDecodeError",
    "SystemExit", "KeyboardInterrupt", "MCPError", "StreamError", "BudgetExceeded"})
_STARTS = {"request_start": "request", "turn_started": "response", "tool_start": "tool",
           "command_start": "command", "checkpoint_start": "checkpoint",
           "terminal_requested": "terminal", "cleanup_start": "cleanup"}
_ENDS = {"request_end": "request", "response_accepted": "response", "tool_end": "tool",
         "command_exit": "command", "checkpoint_end": "checkpoint",
         "terminal_persisted": "terminal", "cleanup_end": "cleanup"}
_EVENTS = frozenset(_STARTS) | frozenset(_ENDS) | {
    "registered", "engine_build", "batch_validated", "tool_error", "result_recorded",
    "index_append_result", "exception", "process_exit", "engine_exit_observed",
    "stream_progress", "terminal", "child_exit", "observer_started", "observer_attached", "observer_exit", "cleanup_error", "signal"}


def _integer(value, *, minimum=0):
    return value if type(value) is int and minimum <= value <= 2**64 - 1 else None


def _exit_code(value):
    return value if type(value) is int and -(2**31) <= value <= 2**32 - 1 else None


def _member(value, choices):
    return value if isinstance(value, str) and value in choices else None


def _unique_pairs(pairs):
    out = {}
    for key, value in pairs:
        if key in out:
            raise ValueError("duplicate diagnostic key")
        out[key] = value
    return out


def _finite_number(raw):
    value = float(raw)
    if not math.isfinite(value):
        raise ValueError("nonfinite diagnostic number")
    return value


def _identity(row):
    pid, birth = _integer(row.get("engine_pid"), minimum=1), row.get("engine_birth")
    if pid is None or not isinstance(birth, str) or not re.fullmatch(
            r"(?:winft:[0-9]{1,20}|linux:[0-9a-f-]{36}:[0-9]{1,20})", birth):
        return None
    return pid, birth, row.get("launch_id")


def _read_rows(path: Path, run_id: str):
    """Keep a startup anchor and recent tail. An unread middle is always explicit."""
    info = {"state": "missing", "partial": False, "invalid_rows": 0, "bytes_read": 0}
    rows = []
    prefix_count = max(1, MAX_DIAGNOSTIC_ROWS // 4)
    recent = deque(maxlen=max(1, MAX_DIAGNOSTIC_ROWS - prefix_count))
    accepted_count = 0
    fd = None
    try:
        if os.path.normcase(str(path.resolve())) != os.path.normcase(os.path.abspath(path)):
            info["state"] = "unavailable"
            return rows, info
        if not stat.S_ISREG(path.lstat().st_mode):
            info["state"] = "unavailable"
            return rows, info
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(path, flags | getattr(os, "O_NONBLOCK", 0))
        with os.fdopen(fd, "rb") as stream:
            fd = None
            st = os.fstat(stream.fileno())
            if not stat.S_ISREG(st.st_mode):
                info["state"] = "unavailable"
                return rows, info
            if st.st_size <= MAX_DIAGNOSTIC_BYTES:
                segments = [(stream.read(MAX_DIAGNOSTIC_BYTES), False)]
            else:
                head = MAX_DIAGNOSTIC_BYTES // 4
                segments = [(stream.read(head), False)]
                stream.seek(st.st_size - (MAX_DIAGNOSTIC_BYTES - head))
                segments.append((stream.read(MAX_DIAGNOSTIC_BYTES - head), True))
                info["partial"] = True
        info["state"] = "recorded"
        for data, cut_start in segments:
            info["bytes_read"] += len(data)
            if cut_start:
                data = data.partition(b"\n")[2]
            if data and not data.endswith(b"\n"):
                info["partial"] = True
                data = data.rpartition(b"\n")[0] + b"\n"
            for raw in data.splitlines():
                if not raw or len(raw) > 8192:
                    info["invalid_rows"] += 1
                    continue
                try:
                    row = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_pairs,
                                     parse_float=_finite_number, parse_constant=_finite_number)
                    if not isinstance(row, dict) or type(row.get("schema")) is not int:
                        raise ValueError
                    if row["schema"] != 1 or row.get("run_id") != run_id:
                        continue
                    if not isinstance(row.get("event"), str) or row["event"] not in _EVENTS:
                        continue
                    accepted_count += 1
                    if len(rows) < prefix_count:
                        rows.append(row)
                    else:
                        recent.append(row)
                    if accepted_count > MAX_DIAGNOSTIC_ROWS:
                        info["partial"] = True
                except (ValueError, UnicodeError, RecursionError):
                    info["invalid_rows"] += 1
        return rows + list(recent), info
    except FileNotFoundError:
        return rows, info
    except (OSError, ValueError, RuntimeError):
        info["state"] = "unavailable"
        return [], info
    finally:
        if fd is not None:
            os.close(fd)


def _boundary(stage, row):
    out = {"stage": stage}
    for key in ("turn", "call_index", "seq", "monotonic_ns"):
        value = _integer(row.get(key))
        if value is not None:
            out[key] = value
    if _member(row.get("tool_name"), _TOOLS):
        out["tool_name"] = row["tool_name"]
    return out


def _same_operation(a, b):
    return a["stage"] == b["stage"] and all(a.get(k) == b.get(k) for k in ("turn", "call_index"))


def _exception_summary(row):
    result = {"class": _member(row.get("exception_class"), _CLASSES) or "OtherError"}
    if _member(row.get("source"), _SOURCES):
        result["source"] = row["source"]
        line = _integer(row.get("line"), minimum=1)
        if line is not None:
            result["line"] = line
    return result


def diagnose_run(record: RunRecord) -> dict:
    """Read only the two known diagnostic siblings, never transcript or messages."""
    missing = {"state": "missing", "partial": False, "invalid_rows": 0, "bytes_read": 0}
    if record.transcript_artifact:
        base = record.transcript_artifact
        engine, ei = _read_rows(Path(base + ".engine.diagnostics.jsonl"), record.run_id)
        observer, oi = _read_rows(Path(base + ".observer.diagnostics.jsonl"), record.run_id)
    else:
        engine, observer, ei, oi = [], [], dict(missing), dict(missing)
    anchor = next((_identity(row) for row in engine if _identity(row) is not None), None)
    mismatch = False
    accepted = []
    for row in engine:
        if anchor is None or _identity(row) != anchor:
            mismatch = True
            continue
        accepted.append(row)
    pending = []
    completed = None
    exception = cleanup_exception = None
    fatal_reason = None
    mechanism = None
    append_failed = False
    engine_exit, command_exit, bundle = None, None, None
    profile, runtime, architecture = None, None, None
    terminal = {"requested": None, "persisted": None, "indexes_attempted": None,
                "indexes_written": None, "indexes_expected": None}
    for row in accepted:
        name = row["event"]
        if name == "engine_build" and isinstance(row.get("bundle_id"), str):
            if re.fullmatch(r"sha256:[0-9a-f]{64}", row["bundle_id"]):
                bundle = row["bundle_id"]
            profile = _member(row.get("execution_profile"), {"legacy", "resilient-v1"})
            architecture = _member(row.get("architecture"), {"32bit", "64bit"})
            if isinstance(row.get("runtime_version"), str) and re.fullmatch(r"[0-9]{1,4}\.[0-9]{1,4}\.[0-9]{1,4}", row["runtime_version"]):
                runtime = row["runtime_version"]
        if name in _STARTS:
            pending.append(_boundary(_STARTS[name], row))
        if name in _ENDS or name == "result_recorded":
            stage = "result" if name == "result_recorded" else _ENDS[name]
            boundary = _boundary(stage, row)
            if not _member(row.get("outcome"), {"failed", "error", "raised"}):
                pending = [item for item in pending if not _same_operation(item, boundary)]
                completed = boundary
                if name == "tool_end":
                    pending.append(_boundary("result", row))
        if name == "batch_validated":
            completed = _boundary("batch", row)
        if name == "command_exit":
            command_exit = _exit_code(row.get("exit_code"))
        if name == "process_exit":
            engine_exit = _exit_code(row.get("exit_code"))
            if mechanism is None and row.get("reason") == "system_exit":
                mechanism = "system_exit"
        if name == "signal" and mechanism is None and row.get("reason") == "signal":
            mechanism = "signal"
        if name == "terminal_requested":
            terminal["requested"] = True
            expected = _integer(row.get("indexes_expected"))
            if expected is not None:
                terminal["indexes_expected"] = expected
        if name in {"index_append_result", "terminal_persisted"}:
            for key in ("indexes_expected", "indexes_attempted", "indexes_written"):
                value = _integer(row.get(key))
                if value is not None:
                    terminal[key] = value
            if name == "terminal_persisted":
                terminal["persisted"] = True
            else:
                expected = (terminal["indexes_expected"] if terminal["indexes_expected"] is not None
                            else terminal["indexes_attempted"])
                if expected is not None and expected > 0 and terminal["indexes_written"] is not None:
                    terminal["persisted"] = terminal["indexes_written"] == expected
            if name == "index_append_result":
                attempted, written = terminal["indexes_attempted"], terminal["indexes_written"]
                append_failed |= bool(_member(row.get("outcome"), {"failed", "error", "raised"}))
                append_failed |= attempted is not None and written is not None and attempted > written
        if name == "exception":
            candidate = _exception_summary(row)
            if fatal_reason is None and _member(row.get("reason"), {
                    "unhandled_exception", "broken_output", "system_exit", "signal"}):
                fatal_reason = row["reason"]
                exception = candidate
            elif exception is None:
                exception = candidate
        if cleanup_exception is None and (name == "cleanup_error" or (
                name == "cleanup_end" and _member(row.get("outcome"), {"failed", "raised", "error"})
                and "exception_class" in row)):
            cleanup_exception = _exception_summary(row)
    reported_exit = engine_exit
    observed_exit, observed_conflict, exit_conflict = None, False, False
    for row in observer:
        # pid/birth describe the monitor itself. Only engine_* bind its target.
        observed_identity = (_integer(row.get("engine_pid"), minimum=1), row.get("engine_birth"))
        launch = row.get("launch_id")
        if anchor is None or observed_identity != anchor[:2] or (
                launch is not None and anchor[2] is not None and launch != anchor[2]):
            mismatch = True
            continue
        if row["event"] in {"engine_exit_observed", "observer_exit"}:
            value = _exit_code(row.get("exit_code"))
            if value is not None:
                exit_conflict |= reported_exit is not None and reported_exit != value
                if observed_exit is not None and observed_exit != value:
                    observed_conflict = exit_conflict = True
                if not observed_conflict:
                    observed_exit = value
    if observed_conflict:
        engine_exit, exit_source = None, None
    elif observed_exit is not None:
        engine_exit, exit_source = observed_exit, "observer"
    else:
        engine_exit, exit_source = reported_exit, "engine" if reported_exit is not None else None
    if record.status == "complete" and fatal_reason and not (
            fatal_reason == "system_exit" and engine_exit == 0):
        category = "post_completion_exception"
    elif record.status == "complete":
        category = "cleanup_failed" if cleanup_exception is not None else "complete"
    elif record.status == "running":
        category = "in_progress"
    elif fatal_reason in {"unhandled_exception", "broken_output"}:
        category = {"unhandled_exception": "engine_exception", "broken_output": "broken_output",
                    "system_exit": "system_exit", "signal": "signal_exit"}[fatal_reason]
    elif terminal["persisted"] is False:
        category = "terminal_write_failed" if append_failed else "terminal_incomplete"
    elif record.stop_reason in {"mutation_outcome_unknown", "recovery_state", "response_incomplete", "context_admission"}:
        category = record.stop_reason
    elif record.stop_reason:
        category = "limit_stop"
    elif fatal_reason is not None:
        category = {"system_exit": "system_exit", "signal": "signal_exit"}[fatal_reason]
    elif mechanism is not None:
        category = "signal_exit" if mechanism == "signal" else "system_exit"
    elif record.status == "failed" and record.ended_at:
        category = "recorded_failure"
    else:
        category = "process_exit_unknown"
    recorded = fatal_reason is not None or mechanism is not None or terminal["requested"] or record.ended_at
    return {"run_id": record.run_id, "status": record.status, "category": category,
            "stop_reason": _member(record.stop_reason, _STOP_REASONS),
            "evidence": "recorded" if recorded else "observed" if engine_exit is not None else "unknown",
            "bundle_id": bundle, "execution_profile": profile, "runtime_version": runtime,
            "architecture": architecture, "last_completed": completed,
            "unfinished": pending[-1] if pending else None, "exception": exception,
            "cleanup_exception": cleanup_exception,
            "engine_exit_code": engine_exit, "last_command_exit_code": command_exit,
            "reported_engine_exit_code": reported_exit, "engine_exit_source": exit_source,
            "exit_mechanism": mechanism,
            "terminal": terminal, "coverage": {"engine": ei["state"], "observer": oi["state"],
                "partial": ei["partial"] or oi["partial"],
                "invalid_rows": ei["invalid_rows"] + oi["invalid_rows"],
                "bytes_read": ei["bytes_read"] + oi["bytes_read"], "identity_mismatch": mismatch,
                "exit_code_conflict": exit_conflict}}


def build_incident_report(records, *, now: datetime, days=7, max_runs=200) -> dict:
    """A start-time cohort; sampling limits never change the population denominator."""
    if type(days) is not int or not 1 <= days <= 3650:
        raise ValueError("days must be between 1 and 3650")
    if type(max_runs) is not int or not 1 <= max_runs <= 1000:
        raise ValueError("max_runs must be between 1 and 1000")
    now = now.astimezone(timezone.utc)
    since = now - timedelta(days=days)
    latest = {}
    for record in records:
        if record.run_id not in latest or record.revision > latest[record.run_id].revision:
            latest[record.run_id] = record
    eligible = []
    for record in latest.values():
        try:
            stamp = datetime.fromisoformat(record.timestamp.replace("Z", "+00:00"))
            if stamp.tzinfo is not None and since <= stamp <= now:
                eligible.append((stamp, record))
        except (TypeError, ValueError):
            continue
    eligible.sort(key=lambda item: (item[0], item[1].run_id), reverse=True)
    eligible = [record for _stamp, record in eligible]
    statuses = Counter(row.status for row in eligible)
    groups = Counter()
    for record in eligible[:max_runs]:
        diagnosis = diagnose_run(record)
        stage = (diagnosis["unfinished"] or diagnosis["last_completed"] or {}).get("stage")
        groups[(diagnosis["bundle_id"], record.model, record.tool_mode, diagnosis["execution_profile"], diagnosis["stop_reason"],
                diagnosis["category"], stage)] += 1
    terminal = sum(statuses[key] for key in ("complete", "failed", "interrupted"))
    return {"since": since.isoformat(), "until": now.isoformat(), "eligible_runs": len(eligible),
            "terminal_runs": terminal, "interrupted_runs": statuses["interrupted"],
            "interruption_rate": statuses["interrupted"] / terminal if terminal else None,
            "scanned_runs": min(max_runs, len(eligible)), "unscanned_runs": max(0, len(eligible)-max_runs),
            "statuses": dict(statuses), "groups": [
                {"bundle_id": key[0], "model": key[1], "tool_mode": key[2],
                 "execution_profile": key[3], "stop_reason": key[4], "category": key[5], "last_stage": key[6], "count": count}
                for key, count in groups.most_common()]}

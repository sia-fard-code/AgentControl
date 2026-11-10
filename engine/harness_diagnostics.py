"""Durable lifecycle evidence, never conversation content or exception messages."""
import json
import os
import re
import sys
import time
import hashlib
import threading
import runpy
from pathlib import Path
from collections import deque
from datetime import datetime, timezone

MAX_TAIL_BYTES = 256 * 1024
MAX_ROWS = 128
OBSERVERS = {"engine", "ask", "fanout", "control", "watcher", "kilo", "kilo_owner", "monitor"}
EVENTS = {"registered", "request_start", "stream_open", "stream_progress", "request_end",
          "request_error", "exception", "signal", "terminal", "process_exit", "child_start",
          "child_exit", "child_state_unknown", "stop_requested", "stop_result", "observer_started", "background_requested",
          "background_observed", "instance_disposed", "session_deleted", "session_error", "session_idle"}
EVENTS.update({"plugin_started", "plugin_heartbeat", "plugin_disposed", "background_deleted",
               "owner_started", "owner_heartbeat", "owner_disposed", "owner_process_started",
               "owner_process_adopted", "owner_child_exit", "owner_disappearance", "owner_boundary",
               "kill_issued", "kill_result", "observer_attached", "observer_exit", "observer_unknown",
               "observer_detached"})
REASONS = {"client_timeout", "stream_idle", "http_error", "disconnect", "transport_error",
           "gateway_failed", "stream_incomplete", "unhandled_exception", "broken_output",
           "system_exit", "signal", "exit_without_finish", "agent_timeout", "fanout_deadline",
           "operator_kill", "unexpected_exit", "normal_exit", "wrapper_exception", "synthesis_timeout",
           "explicit_stop", "explicit_restart"}
REASONS.update({"session_stop", "session_removed", "task_finished", "sandbox_enabled",
                "instance_dispose", "runtime_shutdown", "launch_rollback", "api_session_stop", "unknown"})
SOURCES = {"delegate.py", "ask-model.py", "fanout.py", "agent_await.py",
           "process_control.py", "harness_diagnostics.py"}
ENUMS = {"reason": REASONS, "source": SOURCES,
         "platform": {"nt", "posix"},
         "activity": {"reasoning", "answer", "tool", "other", "completed"},
         "action": {"start", "monitor", "stop", "restart", "status", "logs", "list"},
         "lifetime": {"session", "persistent", "parent"},
         "reported_reason": {"exit", "stop", "restart", "timeout", "shutdown", "session_disposed", "group_switch"},
         "phase": {"request", "stream", "startup", "finish"},
         "status": {"complete", "failed", "interrupted", "terminated", "partial", "already_gone",
                    "starting", "running", "ready", "stopping", "stopped", "exited", "denied"},
         "route": {"responses", "chat"}}
# Engine boundary events (parent consumer contract).
EVENTS.update({"engine_build", "turn_started", "response_accepted", "batch_validated", "tool_start",
               "tool_end", "tool_error", "command_start", "command_exit", "result_recorded",
               "checkpoint_start", "checkpoint_end", "terminal_requested", "index_append_result",
               "terminal_persisted", "cleanup_start", "cleanup_end", "cleanup_error"})
ENUMS["phase"] = ENUMS["phase"] | {"turn", "response", "batch", "tool", "command", "result",
                                   "checkpoint", "terminal", "cleanup"}
ENUMS["tool_name"] = {"read_file", "view_image", "list_dir", "grep", "run_command", "write_file",
                      "replace_in_file", "read_command_output", "list_mcp_tools", "load_skill", "finish", "mcp", "other"}
ENUMS["execution_profile"] = {"legacy", "resilient-v1"}
ENUMS["architecture"] = {"32bit", "64bit"}
ENUMS.update({"method": {"taskkill", "control_file", "sigterm_group", "sigkill_group", "native_job", "process_terminate"},
              "outcome": {"returned", "failed", "raised", "ok", "error"},
              "exit_signal": {"SIGTERM", "SIGKILL", "SIGINT", "SIGBREAK", "SIGHUP"}})
PATTERNS = {"run_id": r"\w[\w.:-]{0,199}",
            "launch_id": r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}",
            "exception_class": r"[A-Za-z_][A-Za-z0-9_.]{0,79}"}
PATTERNS.update({k: r"[A-Za-z0-9_.:-]{1,128}" for k in ("instanceID", "sessionID", "callID", "processID")})
PATTERNS.update({k: r"[A-Za-z0-9_.:-]{1,128}" for k in ("owner_id", "operation_id", "scope_id")})
BIRTHS = {"birth", "parent_birth", "engine_birth", "child_birth", "target_birth", "owner_birth"}
PATTERNS.update({k: r"(winft:[0-9]{1,20}|linux:[0-9a-f-]{36}:[0-9]{1,20})" for k in BIRTHS})
PATTERNS.update(runtime_version=r"[0-9]+\.[0-9]+\.[0-9]+", exit_ticks=r"[0-9]{1,20}")
PATTERNS["patch_id"] = r"sha256:[0-9a-f]{64}"
BOOLS = {"pre_kill", "rollback_hook", "actual_exit_hook", "runner_hook", "birth_available"}
INTS = {"engine_pid": (1, 0xffffffff), "pid": (1, 0xffffffff), "ppid": (0, 0xffffffff),
        "target_pid": (1, 0xffffffff), "child_pid": (1, 0xffffffff), "attempt": (0, 1_000_000),
        "events_seen": (0, 2**63 - 1), "http_status": (100, 599),
        "exit_code": (-2**31, 0xffffffff), "windows_status": (0, 0xffffffff),
        "signal": (1, 255), "line": (1, 10_000_000)}
INTS.update(owner_pid=(1, 0xffffffff), diagnostics_version=(1, 1_000_000), plugin_version=(1, 1_000_000),
            started_ms=(0, 2**53 - 1), api_exit_code=(-2**31, 0xffffffff))
INTS.update({k: (0, 10_000_000) for k in ("turn", "call_index", "batch_size",
                                          "indexes_expected", "indexes_attempted", "indexes_written")})
INTS.update(seq=(0, 2**63 - 1), monotonic_ns=(0, 2**63 - 1))
PATTERNS["bundle_id"] = r"sha256:[0-9a-f]{64}"
_identity_functions = None


def _identity(name, pid):
    global _identity_functions
    try:
        if _identity_functions is None:
            path = Path(__file__).with_name("harness_process_identity.py")
            if not path.is_file():
                path = Path(__file__).resolve().parents[1] / "src/harness_process_identity.py"
            _identity_functions = runpy.run_path(str(path))
        return _identity_functions[name](pid)
    except Exception:
        return None


def process_birth(pid):
    return _identity("process_birth", pid)


def live_process_birth(pid):
    return _identity("live_process_birth", pid)


def clean(values):
    """Reused at the write and read trust boundaries. Unknown keys are discarded."""
    out = {}
    for key, value in values.items():
        if key in ENUMS:
            valid = isinstance(value, str) and value in ENUMS[key]
        elif key in PATTERNS:
            valid = (value is None and key in BIRTHS) or (isinstance(value, str) and re.fullmatch(PATTERNS[key], value))
        elif key in BOOLS:
            valid = type(value) is bool
        elif key in INTS:
            lo, hi = INTS[key]
            valid = type(value) is int and lo <= value <= hi
            valid = valid or (key in {"exit_code", "windows_status", "api_exit_code"} and value is None)
        elif key in {"elapsed_s", "timeout_s"}:
            valid = type(value) in (int, float) and 0 <= value <= 1e9
        else:
            valid = False
        if valid:
            out[key] = value
    return out


class Journal:
    def __init__(self, path, observer, **identity):
        self.path = os.fspath(path)
        self.observer = observer
        self.warned = False
        self.birth = process_birth(os.getpid())
        self.parent_birth = process_birth(os.getppid())
        if self.birth and self.parent_birth and self.birth.startswith("winft:") and self.parent_birth.startswith("winft:"):
            if int(self.parent_birth[6:]) >= int(self.birth[6:]):
                self.parent_birth = None  # the original parent PID may have been recycled
        self.child_identity = {}
        context = {"processID": os.environ.get("KILO_BACKGROUND_PROCESS_ID"),
                   "owner_id": os.environ.get("KILO_LIFECYCLE_OWNER_ID"),
                   "patch_id": os.environ.get("KILO_LIFECYCLE_PATCH_ID"),
                   "owner_birth": os.environ.get("KILO_LIFECYCLE_OWNER_BIRTH")}
        value = os.environ.get("KILO_LIFECYCLE_OWNER_PID", "")
        if value.isascii() and value.isdigit() and len(value) <= 10:
            context["owner_pid"] = int(value)
        context = {k: v for k, v in context.items() if v is not None} if observer in {"engine", "ask", "fanout"} else {}
        if context.get("owner_pid") and not context.get("owner_birth"):
            context["owner_birth"] = live_process_birth(context["owner_pid"])
        context.update(identity)
        if observer == "engine" and context.get("engine_pid") == os.getpid():
            context["engine_birth"] = self.birth
        self.identity = {k: v for k, v in clean(context).items() if k in {
            "run_id", "launch_id", "engine_pid", "engine_birth", "processID", "owner_id", "owner_pid", "owner_birth", "patch_id",
            "bundle_id"}}
        self.seq = 0
        self._lock = threading.RLock()

    def emit(self, event, **fields):
        # Fail open: diagnostics must never raise into the operation they observe.
        try:
            with self._lock:
                return self._emit(event, fields)
        except Exception:
            return False

    def _emit(self, event, fields):
        if not isinstance(event, str) or event not in EVENTS or self.observer not in OBSERVERS:
            return False
        self.seq += 1
        row = clean(fields)
        row.update(seq=self.seq, monotonic_ns=time.monotonic_ns())
        if event in {"child_start", "command_start"} and row.get("child_pid"):
            self.child_identity[row["child_pid"]] = _identity("birth_for_child", fields.get("_child"))
        if row.get("child_pid") in self.child_identity:
            row["child_birth"] = self.child_identity[row["child_pid"]]
        row.update(clean(self.identity))
        row.update(schema=1, ts=datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
                   observer=self.observer, event=event, pid=os.getpid(), ppid=os.getppid(),
                   platform=os.name, birth=self.birth, parent_birth=self.parent_birth)
        if "exit_code" in row:
            code = row["exit_code"]
            row["windows_status"] = None if code is None else code & 0xffffffff
        else:
            row.pop("windows_status", None)
        try:
            with open(self.path, "a", encoding="utf-8", newline="\n") as handle:
                handle.write(json.dumps(row, separators=(",", ":"), allow_nan=False) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            return True
        except (OSError, ValueError):
            if not self.warned:
                self.warned = True
                try:
                    sys.stderr.write("WARNING: lifecycle diagnostics unavailable\n")
                    sys.stderr.flush()
                except (OSError, ValueError):
                    pass
            return False


BUNDLE_SOURCES = (
    "delegate.py", "harness_diagnostics.py", "harness_process_identity.py",
    "harness_process_control.py", "harness_mcp.py", "harness_skills.py",
    "artifact_reader.py", "context_guard.py", "command_output.py", "engine_steer.py",
    "execution_profile.py", "fanout.py", "model_registry.py", "native_batch.py",
    "paged_tools.py", "patch_safety.py", "protocol_health.py", "resilient_tools.py",
    "recovery_artifacts.py", "recovery_candidate.py", "recovery_evidence.py",
    "recovery_handoff.py", "recovery_runtime.py", "recovery_verification.py",
    "runtime_git_observation.py", "runtime_observation.py", "runtime_progress.py",
    "skill_usage.py", "tool_output.py", "trajectory.py", "verification.py",
    "agent_await.py", "ask-model.py", "candidate.py", "finish_gate.py", "harness_setup.py",
    "init-agent-harness.py", "run_review.py", "supervise.py", "task_status.py",
    "supervisor_continuation_v3.py", "supervisor_contract.py", "supervisor_contract_v3.py",
    "supervisor_evidence.py", "supervisor_harness.py", "supervisor_harness_v3.py",
    "supervisor_journal.py", "supervisor_launch_v2.py", "supervisor_manifest_v2.py",
    "supervisor_policy.py", "supervisor_process.py", "supervisor_state.py",
    "context_policy.py", "context_tokens.py", "context_receipts.py", "context_admission.py",
    "context_runtime.py", "context_compaction.py", "context_engine.py", "context_storage.py",
)
SOURCES.update(BUNDLE_SOURCES)
BUNDLE_FALLBACKS = {
    "harness_process_identity.py": "src/agent_control/process_identity.py",
    "harness_process_control.py": "src/agent_control/process_control.py",
    "artifact_reader.py": "src/agent_control/artifact_reader.py",
}
_bundle_cache = {}


def bundle_id(directory=None):
    """sha256 over an explicit, bounded engine source manifest; cached per process."""
    directory = Path(directory or Path(__file__).parent)
    if directory not in _bundle_cache:
        digest = hashlib.sha256()
        try:
            total = 0
            for name in BUNDLE_SOURCES:
                path = directory / name
                digest.update(name.encode() + b"\0")
                if not path.exists() and name in BUNDLE_FALLBACKS:
                    path = directory.parent / BUNDLE_FALLBACKS[name]
                if not path.exists():
                    digest.update(b"absent\0")
                    continue
                part, count = hashlib.sha256(), 0
                with path.open("rb") as stream:
                    for block in iter(lambda: stream.read(64 * 1024), b""):
                        count += len(block)
                        total += len(block)
                        if count > 4 * 1024 * 1024 or total > 16 * 1024 * 1024:
                            raise ValueError("incomplete source fingerprint")
                        part.update(block)
                digest.update(b"present\0" + part.digest())
            _bundle_cache[directory] = "sha256:" + digest.hexdigest()
        except (OSError, ValueError):
            _bundle_cache[directory] = None
    return _bundle_cache[directory]


def exception_fields(exc):
    fields = {"exception_class": type(exc).__name__}
    tb = exc.__traceback__
    while tb is not None:
        source = os.path.basename(tb.tb_frame.f_code.co_filename)
        if source in SOURCES:
            fields.update(source=source, line=tb.tb_lineno)
        tb = tb.tb_next
    return clean(fields)


def _unique(pairs):
    out = {}
    for key, value in pairs:
        if key in out:
            raise ValueError("duplicate diagnostic field")
        out[key] = value
    return out


def read_events(path, **expected_identity):
    expected = {k: v for k, v in expected_identity.items() if v is not None}
    try:
        with open(path, "rb") as handle:
            start = max(0, handle.seek(0, os.SEEK_END) - MAX_TAIL_BYTES)
            handle.seek(start)
            data = handle.read(MAX_TAIL_BYTES)
    except (OSError, ValueError, TypeError):
        return []
    lines = data.split(b"\n")[:-1]
    if start and lines:
        lines.pop(0)
    found = deque(maxlen=MAX_ROWS)
    for raw in lines:
        try:
            row = json.loads(raw, object_pairs_hook=_unique)
        except (ValueError, UnicodeError, RecursionError):
            continue
        if not isinstance(row, dict) or type(row.get("schema")) is not int or row["schema"] != 1:
            continue
        observer, event = row.get("observer"), row.get("event")
        if not isinstance(observer, str) or observer not in OBSERVERS:
            continue
        if not isinstance(event, str) or event not in EVENTS:
            continue
        safe = clean(row)
        safe.update(schema=1, observer=observer, event=event)
        stamp = row.get("ts")
        if isinstance(stamp, str) and re.fullmatch(r"\d{4}-\d\d-\d\dT[\d:.]{8,15}(Z|\+00:00)", stamp):
            safe["ts"] = stamp
        if all(k in safe and type(safe[k]) is type(v) and safe[k] == v for k, v in expected.items()):
            found.append(safe)
    return list(found)


def read_coverage(directory, owner_pid=None, owner_id=None, process_id=None, probe_birth=None, now=None):
    """Only a fresh marker from the live, birth-matched runtime is activation."""
    result = {"owner": "unavailable", "plugin": "unavailable", "pre_kill": False}
    if type(owner_pid) is not int or not 0 < owner_pid <= 0xffffffff:
        return result
    try:
        paths = sorted(Path(directory).glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)[:32]
        records = [row for path in paths for row in read_events(path) if row.get("pid") == owner_pid]
        actual_birth = (probe_birth or live_process_birth)(owner_pid)
    except Exception:
        return result
    now = datetime.now(timezone.utc).timestamp() if now is None else now

    def stamp(row):
        try:
            return datetime.fromisoformat(row["ts"].replace("Z", "+00:00")).timestamp()
        except (KeyError, ValueError, TypeError, OverflowError, OSError):
            return float("-inf")

    plugin_instances = {row.get("instanceID") for row in records
                        if row["observer"] == "kilo" and row.get("processID") == process_id}
    for role, observer, prefix, identity in (("owner", "kilo_owner", "owner_", "owner_id"),
                                              ("plugin", "kilo", "plugin_", "instanceID")):
        profiles = [row for row in records if row["observer"] == observer
                    and row["event"] in {prefix + "started", prefix + "heartbeat", prefix + "disposed"}]
        if role == "owner" and owner_id:
            profiles = [row for row in profiles if row.get("owner_id") == owner_id]
        if role == "plugin" and process_id:
            profiles = [row for row in profiles if row.get("instanceID") in plugin_instances]
        latest = {}
        for row in sorted(profiles, key=stamp):
            latest[row.get(identity)] = row
        states = []
        for row in latest.values():
            state = "active"
            if not row.get(identity) or actual_birth is None or row.get("birth") != actual_birth:
                state = "unverified"
            elif role == "owner" and (row.get("owner_pid") != owner_pid or row.get("owner_birth") != actual_birth):
                state = "unverified"
            elif row["event"].endswith("disposed"):
                state = "inactive"
            elif stamp(row) > now + 5 or stamp(row) < now - 45:
                state = "stale"
            elif row.get("diagnostics_version") != 2:
                state = "unsupported"
            elif role == "owner" and (not known_patch_id() or row.get("patch_id") != known_patch_id()
                    or row.get("runtime_version") != "7.8.18" or not all(
                    row.get(k) is True for k in ("pre_kill", "rollback_hook", "actual_exit_hook"))):
                state = "unsupported"
            states.append(state)
        if states:
            result[role] = "active" if "active" in states else states[-1]
    result["pre_kill"] = result["owner"] == "active"
    return result


def diagnostic_directory():
    return Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local/share") / "kilo/lifecycle"


def known_patch_id():
    try:
        path = Path(__file__).with_name("harness_owner_patch.json")
        if not path.is_file():
            path = Path(__file__).resolve().parents[1] / "tools/kilo/owner-patchkit/manifest.json"
        value = json.loads(path.read_text(encoding="utf-8")).get("patch_id")
        return value if isinstance(value, str) and re.fullmatch(PATTERNS["patch_id"], value) else None
    except (OSError, ValueError, TypeError, AttributeError):
        return None

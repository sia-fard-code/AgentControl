"""Verified termination of a delegated run's engine process tree.

A PID is only ever terminated after its identity has been confirmed against
the run's start event, so a recycled PID belonging to something else is never
touched.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re

from .liveness import _process_identity_valid
from .models import ProcessSnapshot
from .run_index import StartEvent


class NotVerified(Exception):
    """The PID is not this run's engine, or it cannot be checked."""


class KillDenied(Exception):
    """The OS refused access (psutil.AccessDenied)."""


def _control_evidence(start, event, status=None):
    try:
        _write_control_evidence(start, event, status)
    except Exception:
        pass  # even malformed metadata must not change a verified kill/result


def _write_control_evidence(start, event, status):
    """Fixed-schema intent/outcome, separate from authoritative engine accounting."""
    path = Path(start.transcript)
    if not path.is_absolute() or not re.fullmatch(r"\w[\w.:-]{0,199}", start.run_id):
        return
    row = {"schema": 1, "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
           "observer": "control", "event": event, "reason": "operator_kill",
           "run_id": start.run_id, "engine_pid": start.engine_pid,
           "pid": os.getpid(), "ppid": os.getppid()}
    if status in {"terminated", "partial", "already_gone", "denied"}:
        row["status"] = status
    try:
        with open(str(path) + ".control.diagnostics.jsonl", "a", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(row, separators=(",", ":")) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
    except OSError:
        pass  # diagnostics must not alter verified process-control behavior


ACCESS_DENIED_MESSAGE = (
    "access denied: the engine process belongs to another Windows user or runs "
    "elevated (for example inside the Codex sandbox); stop it from the tool that "
    "launched it"
)


@dataclass(frozen=True)
class KillResult:
    outcome: str  # "terminated" | "partial" | "already_gone"
    engine_pid: int
    children: int  # descendants found before termination
    survivors: int

    def as_dict(self) -> dict[str, object]:
        return {
            "outcome": self.outcome,
            "engine_pid": self.engine_pid,
            "children": self.children,
            "survivors": self.survivors,
        }


def terminate_engine(
    start: StartEvent,
    *,
    psutil_module=None,
    grace_seconds: float = 5.0,
) -> KillResult:
    pid = start.engine_pid
    if pid is None:
        raise NotVerified("no_process_identity")

    ps = psutil_module
    if ps is None:
        try:  # pragma: no cover - exercised via injected fake in tests
            import psutil as ps  # type: ignore[no-redef]
        except Exception:
            raise NotVerified("psutil_unavailable")

    try:
        return _terminate(ps, start, pid, grace_seconds)
    except ps.AccessDenied:
        raise KillDenied(ACCESS_DENIED_MESSAGE)


def _terminate(ps, start: StartEvent, pid: int, grace_seconds: float) -> KillResult:
    try:
        proc = ps.Process(pid)
    except ps.NoSuchProcess:
        return KillResult("already_gone", pid, 0, 0)

    try:
        snapshot = ProcessSnapshot(
            pid=pid,
            command_line=tuple(proc.cmdline()),
            create_time=proc.create_time(),
            running=proc.is_running() and proc.status() != ps.STATUS_ZOMBIE,
        )
    except ps.NoSuchProcess:
        return KillResult("already_gone", pid, 0, 0)

    if not _process_identity_valid(start, snapshot):
        raise NotVerified("identity_mismatch")

    try:
        children = proc.children(recursive=True)
    except ps.NoSuchProcess:
        return KillResult("already_gone", pid, 0, 0)

    _control_evidence(start, "stop_requested")
    try:
        proc.terminate()
    except ps.NoSuchProcess:
        _control_evidence(start, "stop_result", "already_gone")
        return KillResult("already_gone", pid, len(children), 0)
    except ps.AccessDenied:
        _control_evidence(start, "stop_result", "denied")
        raise
    for child in children:
        try:
            child.terminate()
        except ps.NoSuchProcess:
            pass

    _gone, alive = ps.wait_procs([proc, *children], timeout=grace_seconds)
    for survivor in alive:
        try:
            survivor.kill()
        except ps.NoSuchProcess:
            pass
    _, alive = ps.wait_procs(alive, timeout=2)

    outcome = "partial" if alive else "terminated"
    _control_evidence(start, "stop_result", outcome)
    return KillResult(outcome, pid, len(children), len(alive))

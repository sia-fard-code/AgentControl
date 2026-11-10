from __future__ import annotations

import os
import re
import sys
from datetime import datetime, timezone

from .models import Liveness, ProbeUnknown, ProcessProbe, ProcessSnapshot
from .run_index import EndEvent, StartEvent

# The engine may be launched directly (.claude\bin\delegate.py) or loaded by a
# wrapper script that calls main() in the same process, so the script path in
# the command line is not reliable. Identity is instead proven by three facts
# that a recycled pid cannot fake: the process is live and holds the recorded
# pid, it was created inside the start-time window (a pid cannot be reused while
# its owner is alive), and it is a Python interpreter.
_PYTHON_EXE = re.compile(r"^pythonw?[\d.]*(\.exe)?$")
_CREATE_BEFORE = 300  # seconds a process may predate the start timestamp
_CREATE_AFTER = 5  # seconds a process may postdate the start timestamp
_STALL_WINDOW = 300  # seconds of silence after which an open run is "stalled"
# A schema-1 run (no process identity) silent this long is treated as
# definitively dead and reaped to a terminal state. Deliberately long (hours,
# not minutes) so it never causes the running<->interrupted flapping that a
# short threshold did, while still clearing runs that linger for days.
_REAP_WINDOW = 2 * 60 * 60  # 2 hours


try:  # pragma: no cover - psutil is an optional runtime probe
    import psutil
except Exception:  # pragma: no cover
    psutil = None  # type: ignore[assignment]

PSUTIL_AVAILABLE = psutil is not None


class PsutilProcessProbe:
    def __init__(self):
        self.observer = None
        try:
            from .exit_observer import ExitObserver
            self.observer = ExitObserver()
        except Exception:
            try:
                print("WARNING: independent exit observer unavailable", file=sys.stderr)
            except Exception:
                pass

    def observe(self, start, snapshot):
        return self.observer.attach(start, snapshot) if self.observer is not None else False

    def close(self):
        if self.observer is not None:
            self.observer.close()

    def poll_observers(self):
        if self.observer is not None:
            self.observer.poll()

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass

    def inspect(self, pid: int) -> ProcessSnapshot | ProbeUnknown | None:
        try:
            self.observer.poll(pid)
        except Exception:
            pass
        if psutil is None:
            return ProbeUnknown("psutil_unavailable")
        try:
            process = psutil.Process(pid)
            with process.oneshot():
                return ProcessSnapshot(
                    pid=pid,
                    command_line=tuple(process.cmdline()),
                    create_time=process.create_time(),
                    running=process.is_running()
                    and process.status() != psutil.STATUS_ZOMBIE,
                )
        except psutil.NoSuchProcess:
            # ZombieProcess is a subclass; a zombie is definitively gone too.
            return None
        except psutil.AccessDenied:
            return ProbeUnknown("access_denied")
        except Exception:
            return ProbeUnknown("probe_error")


def _start_epoch(start: StartEvent) -> float:
    return datetime.fromisoformat(start.ts).timestamp()


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat()


def _silence(now: datetime, start: StartEvent, latest_artifact_mtime: float | None):
    """Seconds since the most recent activity signal, and its timestamp.

    The signal is the newest of the start time and the latest artifact mtime, so
    a run with no artifact activity yet is measured from when it started.
    """
    signal = _start_epoch(start)
    if latest_artifact_mtime is not None and latest_artifact_mtime > signal:
        signal = latest_artifact_mtime
    seconds = now.timestamp() - signal
    if seconds < 0:
        seconds = 0.0
    return seconds, _iso(signal)


def _process_identity_valid(start: StartEvent, snapshot: ProcessSnapshot | None) -> bool:
    if snapshot is None or not snapshot.running:
        return False
    if not snapshot.command_line:
        return False
    executable = os.path.basename(snapshot.command_line[0]).casefold()
    if not _PYTHON_EXE.match(executable):
        return False
    start_epoch = _start_epoch(start)
    return (
        start_epoch - _CREATE_BEFORE
        <= snapshot.create_time
        <= start_epoch + _CREATE_AFTER
    )


def classify_liveness(
    start: StartEvent,
    end: EndEvent | None,
    *,
    now: datetime,
    latest_artifact_mtime: float | None,
    probe: ProcessProbe,
) -> Liveness:
    # 1. A real end record is authoritative and terminal.
    if end is not None:
        return Liveness(
            display=end.status,
            persist_status=end.status,
            terminal=True,
            inferred=False,
            code="end_event",
        )

    silence_seconds, last_activity_at = _silence(now, start, latest_artifact_mtime)

    # 2. Schema-2 runs carry process identity. If the identified engine process
    #    is definitively gone, the run ended without an end record: terminal
    #    (abandoned). If it is present and valid, the run is genuinely running.
    if start.engine_pid is not None:
        snapshot = probe.inspect(start.engine_pid)
        if isinstance(snapshot, ProbeUnknown):
            # The probe could not tell whether the process is alive. Ignorance
            # is never proof of death: keep the run open (unverified) until the
            # long reap window makes prolonged silence itself the evidence.
            if silence_seconds > _REAP_WINDOW:
                return Liveness(
                    display="abandoned",
                    persist_status="interrupted",
                    terminal=True,
                    inferred=True,
                    code="reaped_after_long_silence",
                    silence_seconds=silence_seconds,
                    last_activity_at=last_activity_at,
                )
            return Liveness(
                display="unverified",
                persist_status="running",
                terminal=False,
                inferred=True,
                code=f"probe_unknown:{snapshot.reason}",
                silence_seconds=silence_seconds,
                last_activity_at=last_activity_at,
            )
        if _process_identity_valid(start, snapshot):
            try:
                observe = getattr(probe, "observe", None)
                if observe is not None:
                    observe(start, snapshot)
            except Exception:
                pass  # evidence collection never changes the existing liveness decision
            return Liveness(
                display="running",
                persist_status="running",
                terminal=False,
                inferred=False,
                code="process_identity_valid",
                silence_seconds=silence_seconds,
                last_activity_at=last_activity_at,
            )
        return Liveness(
            display="abandoned",
            persist_status="interrupted",
            terminal=True,
            inferred=False,
            code="process_gone",
            silence_seconds=silence_seconds,
            last_activity_at=last_activity_at,
        )

    # 3. Schema-1 runs have no process identity. A short silence is NOT proof of
    #    death, so it stays open and is merely DISPLAYED as stalled. But once a
    #    run has been silent far past any plausible pause (the long reap window),
    #    it is treated as definitively abandoned and persisted terminal ONCE.
    #    Because terminal is absorbing and the window is long, this can never
    #    flap back to running.
    if silence_seconds > _REAP_WINDOW:
        return Liveness(
            display="abandoned",
            persist_status="interrupted",
            terminal=True,
            inferred=True,
            code="reaped_after_long_silence",
            silence_seconds=silence_seconds,
            last_activity_at=last_activity_at,
        )
    display = "stalled" if silence_seconds > _STALL_WINDOW else "running"
    return Liveness(
        display=display,
        persist_status="running",
        terminal=False,
        inferred=True,
        code="artifact_activity_recent" if display == "running" else "artifact_activity_stale",
        silence_seconds=silence_seconds,
        last_activity_at=last_activity_at,
    )

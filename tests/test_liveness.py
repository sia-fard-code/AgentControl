from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from agent_control.liveness import PsutilProcessProbe, classify_liveness
from agent_control.models import ProbeUnknown, ProcessSnapshot
from tests.test_run_index import end_event, start_event

ENGINE = r"C:\Users\operator\.claude\bin\delegate.py"


def utc(value: str) -> datetime:
    return datetime.fromisoformat(value)


@dataclass
class FakeProbe:
    result: ProcessSnapshot | ProbeUnknown | None = None

    def inspect(self, pid: int) -> ProcessSnapshot | ProbeUnknown | None:
        return self.result


# --- terminal from real evidence -------------------------------------------
def test_end_event_is_terminal_and_persisted() -> None:
    v = classify_liveness(
        start_event(run_id="r"),
        end_event(run_id="r", status="failed"),
        now=utc("2026-09-17T13:00:00+00:00"),
        latest_artifact_mtime=None,
        probe=FakeProbe(),
    )
    assert v.display == "failed"
    assert v.persist_status == "failed"
    assert v.terminal is True


def test_schema2_process_gone_is_terminal_abandoned() -> None:
    v = classify_liveness(
        start_event(engine_pid=42, ts="2026-09-17T12:00:00+00:00"),
        None,
        now=utc("2026-09-17T12:01:00+00:00"),
        latest_artifact_mtime=None,
        probe=FakeProbe(result=None),
    )
    assert v.display == "abandoned"
    assert v.persist_status == "interrupted"
    assert v.terminal is True


def test_schema2_valid_process_is_running_open() -> None:
    probe = FakeProbe(
        ProcessSnapshot(42, ("python", ENGINE), utc("2026-09-17T12:00:00+00:00").timestamp(), True)
    )
    v = classify_liveness(
        start_event(engine_pid=42, ts="2026-09-17T12:00:00+00:00"),
        None,
        now=utc("2026-09-17T12:01:00+00:00"),
        latest_artifact_mtime=None,
        probe=probe,
    )
    assert v.display == "running"
    assert v.persist_status == "running"
    assert v.terminal is False


def test_observer_hook_runs_only_after_identity_verification(tmp_path):
    calls = []
    class ObservingProbe(FakeProbe):
        def observe(self, start, snapshot):
            calls.append((start, snapshot))
    start = start_event(engine_pid=42, ts="2026-09-17T12:00:00+00:00",
                        transcript=str(tmp_path / "synthetic.md"))
    valid = ProcessSnapshot(42, ("python", ENGINE), utc(start.ts).timestamp(), True)
    probe = ObservingProbe(valid)
    classify_liveness(start, None, now=utc("2026-09-17T12:01:00+00:00"), latest_artifact_mtime=None, probe=probe)
    assert calls == [(start, valid)]
    calls.clear()
    probe.result = ProcessSnapshot(42, ("not-python",), valid.create_time, True)
    classify_liveness(start, None, now=utc("2026-09-17T12:01:00+00:00"), latest_artifact_mtime=None, probe=probe)
    assert calls == []


def test_observer_failure_cannot_change_liveness(tmp_path):
    class ObservingProbe(FakeProbe):
        def observe(self, start, snapshot):
            raise RuntimeError("SYNTHETIC_PRIVATE")
    start = start_event(engine_pid=42, ts="2026-09-17T12:00:00+00:00")
    valid = ProcessSnapshot(42, ("python", ENGINE), utc(start.ts).timestamp(), True)
    value = classify_liveness(start, None, now=utc("2026-09-17T12:01:00+00:00"),
                              latest_artifact_mtime=None, probe=ObservingProbe(valid))
    assert value.code == "process_identity_valid" and value.display == "running"


def test_optional_observer_initialization_failure_is_fail_open(monkeypatch, capsys):
    import agent_control.exit_observer as observer
    def fail():
        raise RuntimeError("SYNTHETIC_PRIVATE")
    monkeypatch.setattr(observer, "ExitObserver", fail)
    probe = PsutilProcessProbe()
    probe.poll_observers()
    probe.close()
    assert "SYNTHETIC_PRIVATE" not in capsys.readouterr().err


def test_schema2_wrapper_launcher_is_running() -> None:
    # The engine may be loaded by a wrapper script whose path is in the command
    # line instead of .claude\bin\delegate.py. Identity is proven by the live
    # pid + create-time window + a python interpreter, not any script path.
    cmd = (
        "C:\\Python313\\python.exe",
        "-u",
        "D:\\r\\.claude\\run-delegate.py",
        "--brief",
        "b.md",
    )
    probe = FakeProbe(
        ProcessSnapshot(42, cmd, utc("2026-09-17T12:00:00+00:00").timestamp(), True)
    )
    v = classify_liveness(
        start_event(engine_pid=42, ts="2026-09-17T12:00:00+00:00"),
        None,
        now=utc("2026-09-17T12:01:00+00:00"),
        latest_artifact_mtime=None,
        probe=probe,
    )
    assert v.display == "running"
    assert v.code == "process_identity_valid"


def test_schema2_non_python_process_is_gone() -> None:
    probe = FakeProbe(
        ProcessSnapshot(
            42,
            ("C:\\Windows\\notepad.exe",),
            utc("2026-09-17T12:00:00+00:00").timestamp(),
            True,
        )
    )
    v = classify_liveness(
        start_event(engine_pid=42, ts="2026-09-17T12:00:00+00:00"),
        None,
        now=utc("2026-09-17T12:01:00+00:00"),
        latest_artifact_mtime=None,
        probe=probe,
    )
    assert v.display == "abandoned"
    assert v.code == "process_gone"


# --- schema-1 (no process identity): silence is NON-terminal ---------------
def test_schema1_recent_activity_is_running_open() -> None:
    start = start_event(schema=None, engine_pid=None, ts="2026-09-17T12:00:00+00:00")
    v = classify_liveness(
        start,
        None,
        now=utc("2026-09-17T12:02:00+00:00"),
        latest_artifact_mtime=utc("2026-09-17T12:01:00+00:00").timestamp(),
        probe=FakeProbe(),
    )
    assert v.display == "running"
    assert v.persist_status == "running"
    assert v.terminal is False
    assert v.silence_seconds == 60.0


def test_schema1_silence_is_stalled_not_terminal() -> None:
    start = start_event(schema=None, engine_pid=None, ts="2026-09-17T12:00:00+00:00")
    v = classify_liveness(
        start,
        None,
        now=utc("2026-09-17T13:00:00+00:00"),
        latest_artifact_mtime=utc("2026-09-17T12:01:00+00:00").timestamp(),
        probe=FakeProbe(),
    )
    # long silent but no proof of death -> stalled, still OPEN (persist running)
    assert v.display == "stalled"
    assert v.persist_status == "running"
    assert v.terminal is False
    assert v.silence_seconds == 3540.0


def test_schema1_missing_activity_is_stalled_open_not_interrupted() -> None:
    # A schema-1 run whose transcript is not (yet) readable must NOT be stamped
    # terminal from that alone - that caused the interrupted<->running flapping.
    start = start_event(schema=None, engine_pid=None, ts="2026-09-17T12:00:00+00:00")
    v = classify_liveness(
        start,
        None,
        now=utc("2026-09-17T13:00:00+00:00"),
        latest_artifact_mtime=None,
        probe=FakeProbe(),
    )
    assert v.display == "stalled"
    assert v.persist_status == "running"
    assert v.terminal is False


def test_schema1_reaped_terminal_after_long_silence() -> None:
    # A schema-1 run silent for many hours is definitively dead and must be
    # persisted terminal (once), so it stops lingering in the live list.
    start = start_event(schema=None, engine_pid=None, ts="2026-09-17T12:00:00+00:00")
    v = classify_liveness(
        start,
        None,
        now=utc("2026-09-18T12:00:00+00:00"),  # 24h later
        latest_artifact_mtime=None,
        probe=FakeProbe(),
    )
    assert v.display == "abandoned"
    assert v.persist_status == "interrupted"
    assert v.terminal is True
    assert v.code == "reaped_after_long_silence"


def test_schema1_stalled_stays_open_before_reap_window() -> None:
    # Just past the stall window but well before the reap window: stalled, open.
    start = start_event(schema=None, engine_pid=None, ts="2026-09-17T12:00:00+00:00")
    v = classify_liveness(
        start,
        None,
        now=utc("2026-09-17T12:30:00+00:00"),  # 30 min
        latest_artifact_mtime=None,
        probe=FakeProbe(),
    )
    assert v.display == "stalled"
    assert v.persist_status == "running"
    assert v.terminal is False


def test_silence_uses_max_of_start_and_activity() -> None:
    # With no artifact activity at all, silence is measured from the start time.
    start = start_event(schema=None, engine_pid=None, ts="2026-09-17T12:00:00+00:00")
    v = classify_liveness(
        start,
        None,
        now=utc("2026-09-17T12:00:30+00:00"),
        latest_artifact_mtime=None,
        probe=FakeProbe(),
    )
    assert v.silence_seconds == 30.0
    assert v.display == "running"  # only 30s since start -> still fresh


# --- schema-2 probe that cannot tell: never terminal on ignorance ----------
def test_schema2_probe_access_denied_stays_open_unverified() -> None:
    v = classify_liveness(
        start_event(engine_pid=42, ts="2026-09-17T12:00:00+00:00"),
        None,
        now=utc("2026-09-17T12:01:00+00:00"),
        latest_artifact_mtime=None,
        probe=FakeProbe(result=ProbeUnknown("access_denied")),
    )
    assert v.display == "unverified"
    assert v.persist_status == "running"
    assert v.terminal is False
    assert v.inferred is True
    assert v.code == "probe_unknown:access_denied"


def test_schema2_probe_psutil_unavailable_stays_open_unverified() -> None:
    v = classify_liveness(
        start_event(engine_pid=42, ts="2026-09-17T12:00:00+00:00"),
        None,
        now=utc("2026-09-17T12:01:00+00:00"),
        latest_artifact_mtime=None,
        probe=FakeProbe(result=ProbeUnknown("psutil_unavailable")),
    )
    assert v.display == "unverified"
    assert v.persist_status == "running"
    assert v.terminal is False
    assert v.code == "probe_unknown:psutil_unavailable"


def test_schema2_probe_unknown_reaps_after_long_silence() -> None:
    v = classify_liveness(
        start_event(engine_pid=42, ts="2026-09-17T12:00:00+00:00"),
        None,
        now=utc("2026-09-18T12:00:00+00:00"),  # 24h later
        latest_artifact_mtime=None,
        probe=FakeProbe(result=ProbeUnknown("access_denied")),
    )
    assert v.display == "abandoned"
    assert v.persist_status == "interrupted"
    assert v.terminal is True
    assert v.inferred is True
    assert v.code == "reaped_after_long_silence"


def test_psutil_probe_classifies_errors(monkeypatch) -> None:
    class FakePsutil:
        class Error(Exception):
            pass

        class NoSuchProcess(Error):
            pass

        class AccessDenied(Error):
            pass

        STATUS_ZOMBIE = "zombie"

        _raise: Exception

        def Process(self, pid):  # noqa: N802 - mimic psutil API
            raise self._raise

    fake = FakePsutil()

    fake._raise = FakePsutil.NoSuchProcess()
    monkeypatch.setattr("agent_control.liveness.psutil", fake)
    assert PsutilProcessProbe().inspect(1) is None

    fake._raise = FakePsutil.AccessDenied()
    result = PsutilProcessProbe().inspect(1)
    assert isinstance(result, ProbeUnknown)
    assert result.reason == "access_denied"

    fake._raise = RuntimeError("boom")
    result = PsutilProcessProbe().inspect(1)
    assert isinstance(result, ProbeUnknown)
    assert result.reason == "probe_error"

    monkeypatch.setattr("agent_control.liveness.psutil", None)
    result = PsutilProcessProbe().inspect(1)
    assert isinstance(result, ProbeUnknown)
    assert result.reason == "psutil_unavailable"

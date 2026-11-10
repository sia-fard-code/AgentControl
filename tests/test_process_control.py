from __future__ import annotations

import subprocess
import sys
import time
from datetime import datetime, timezone

import pytest

from agent_control.process_control import (
    KillDenied,
    NotVerified,
    terminate_engine,
)
from agent_control.run_index import StartEvent

from tests.fake_psutil import FakeProcess, FakePsutil

MARKER_CMDLINE = ["python", "C:\\Users\\x\\.claude\\bin\\delegate.py"]

WRAPPER_CMDLINE = [
    "C:\\Python313\\python.exe",
    "-u",
    "D:\\r\\.claude\\run-delegate.py",
    "--brief",
    "b.md",
]


def _start(pid, ts=None):
    if ts is None:
        ts = datetime(2025, 1, 1, 12, 0, 0, tzinfo=timezone.utc).isoformat()
    return StartEvent(
        schema=2,
        run_id="r1",
        ts=ts,
        harness_version=6,
        root="R",
        cwd="R",
        label="t",
        model="m",
        effort=None,
        tool_mode=None,
        write_mode="disk",
        engine_pid=pid,
        transcript="T.md",
        messages="T.messages.jsonl",
        usage_journal="T.usage.jsonl",
    )


def _epoch(start):
    return datetime.fromisoformat(start.ts).timestamp()


def test_identity_mismatch_touches_nothing():
    start = _start(100)
    # A non-Python process holding the recorded pid is not our engine.
    proc = FakeProcess(100, ["C:\\Windows\\notepad.exe"], _epoch(start))
    ps = FakePsutil([proc])
    with pytest.raises(NotVerified):
        terminate_engine(start, psutil_module=ps)
    assert not any(
        c.startswith("terminate") or c.startswith("kill") for c in ps.calls
    )


def test_operator_kill_records_intent_before_termination(tmp_path):
    from dataclasses import replace
    import json
    start = replace(_start(100), transcript=str(tmp_path / "run.md"))
    path = tmp_path / "run.md.control.diagnostics.jsonl"
    class ObservedProcess(FakeProcess):
        def terminate(self):
            assert path.exists(), "termination intent was not flushed"
            record = json.loads(path.read_text(encoding="utf-8").splitlines()[-1])
            assert record["event"] == "stop_requested"
            assert record["reason"] == "operator_kill" and record["engine_pid"] == 100
            super().terminate()
    proc = ObservedProcess(100, MARKER_CMDLINE, _epoch(start))
    result = terminate_engine(start, psutil_module=FakePsutil([proc]))
    assert result.outcome == "terminated"
    records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert records[-1]["event"] == "stop_result" and records[-1]["status"] == "terminated"


@pytest.mark.parametrize("transcript", [None, "bad\x00path"])
def test_bad_diagnostic_path_cannot_prevent_verified_kill(tmp_path, transcript):
    from dataclasses import replace
    start = replace(_start(100), transcript=transcript)
    proc = FakeProcess(100, MARKER_CMDLINE, _epoch(start))
    assert terminate_engine(start, psutil_module=FakePsutil([proc])).outcome == "terminated"


def test_create_time_outside_window():
    start = _start(100)
    proc = FakeProcess(100, MARKER_CMDLINE, _epoch(start) - 400)
    ps = FakePsutil([proc])
    with pytest.raises(NotVerified):
        terminate_engine(start, psutil_module=ps)


def test_wrapper_launcher_is_terminated():
    # A wrapper command line (python.exe running a run-delegate.py wrapper, no
    # .claude\bin\delegate.py marker) with a matching create time is our engine.
    start = _start(100)
    proc = FakeProcess(100, WRAPPER_CMDLINE, _epoch(start))
    ps = FakePsutil([proc])
    result = terminate_engine(start, psutil_module=ps)
    assert result.outcome == "terminated"


def test_non_python_matching_window_raises_identity_mismatch():
    start = _start(100)
    proc = FakeProcess(100, ["C:\\Windows\\notepad.exe"], _epoch(start))
    ps = FakePsutil([proc])
    with pytest.raises(NotVerified) as exc_info:
        terminate_engine(start, psutil_module=ps)
    assert "identity_mismatch" in str(exc_info.value)


def test_children_listed_before_parent_terminated():
    start = _start(100)
    engine = FakeProcess(100, MARKER_CMDLINE, _epoch(start), child_pids=(101, 102))
    c1 = FakeProcess(101, ["c1"], _epoch(start))
    c2 = FakeProcess(102, ["c2"], _epoch(start))
    ps = FakePsutil([engine, c1, c2])
    result = terminate_engine(start, psutil_module=ps)
    assert ps.calls[0] == "children:100"
    term = ps.calls.index("terminate:100")
    assert term < ps.calls.index("terminate:101")
    assert term < ps.calls.index("terminate:102")
    assert result.outcome == "terminated"
    assert result.children == 2
    assert result.survivors == 0


def test_survivor_is_killed():
    start = _start(100)
    engine = FakeProcess(100, MARKER_CMDLINE, _epoch(start), child_pids=(101,))
    c1 = FakeProcess(101, ["c1"], _epoch(start), survive_terminate=True)
    ps = FakePsutil([engine, c1])
    result = terminate_engine(start, psutil_module=ps, grace_seconds=0)
    assert "kill:101" in ps.calls
    assert result.outcome == "terminated"


def test_unkillable_is_partial():
    start = _start(100)
    engine = FakeProcess(100, MARKER_CMDLINE, _epoch(start), child_pids=(101,))
    c1 = FakeProcess(
        101, ["c1"], _epoch(start), survive_terminate=True, survive_kill=True
    )
    ps = FakePsutil([engine, c1])
    result = terminate_engine(start, psutil_module=ps, grace_seconds=0)
    assert result.outcome == "partial"
    assert result.survivors == 1


def test_already_gone():
    start = _start(999)
    ps = FakePsutil([])
    result = terminate_engine(start, psutil_module=ps)
    assert result.outcome == "already_gone"
    assert result.children == 0


def test_access_denied():
    start = _start(100)
    engine = FakeProcess(100, MARKER_CMDLINE, _epoch(start), deny=True)
    ps = FakePsutil([engine])
    with pytest.raises(KillDenied):
        terminate_engine(start, psutil_module=ps)


def test_access_denied_on_cmdline():
    start = _start(100)
    engine = FakeProcess(100, MARKER_CMDLINE, _epoch(start), deny_cmdline=True)
    ps = FakePsutil([engine])
    with pytest.raises(KillDenied) as exc_info:
        terminate_engine(start, psutil_module=ps)
    assert "another Windows user" in str(exc_info.value)


def test_access_denied_on_terminate_after_verified():
    start = _start(100)
    engine = FakeProcess(100, MARKER_CMDLINE, _epoch(start), deny=True)
    ps = FakePsutil([engine])
    with pytest.raises(KillDenied) as exc_info:
        terminate_engine(start, psutil_module=ps)
    assert "another Windows user" in str(exc_info.value)
    assert not any(c.startswith("kill") for c in ps.calls)


def test_access_denied_on_children():
    start = _start(100)
    engine = FakeProcess(100, MARKER_CMDLINE, _epoch(start), deny_children=True)
    ps = FakePsutil([engine])
    with pytest.raises(KillDenied) as exc_info:
        terminate_engine(start, psutil_module=ps)
    assert "another Windows user" in str(exc_info.value)


def test_no_pid():
    start = _start(None)
    ps = FakePsutil([])
    with pytest.raises(NotVerified):
        terminate_engine(start, psutil_module=ps)


def test_real_process_tree_is_killed(tmp_path):
    psutil = pytest.importorskip("psutil")
    bin_dir = tmp_path / ".claude" / "bin"
    bin_dir.mkdir(parents=True)
    script = bin_dir / "delegate.py"
    script.write_text(
        "import subprocess, sys, time\n"
        "child = subprocess.Popen([sys.executable, '-c', "
        "'import time; time.sleep(120)'])\n"
        "print(child.pid, flush=True)\n"
        "time.sleep(120)\n"
    )
    proc = subprocess.Popen(
        [sys.executable, str(script)], stdout=subprocess.PIPE, text=True
    )
    child_pid = None
    try:
        child_pid = int(proc.stdout.readline().strip())
        start = _start(proc.pid, ts=datetime.now(timezone.utc).isoformat())
        result = terminate_engine(start)
        assert result.outcome == "terminated"
        assert result.children >= 1
        deadline = time.time() + 5
        while time.time() < deadline and (
            psutil.pid_exists(proc.pid) or psutil.pid_exists(child_pid)
        ):
            time.sleep(0.1)
        assert not psutil.pid_exists(proc.pid)
        assert not psutil.pid_exists(child_pid)
    finally:
        for pid in (proc.pid, child_pid):
            if pid is not None and psutil.pid_exists(pid):
                try:
                    psutil.Process(pid).kill()
                except Exception:
                    pass

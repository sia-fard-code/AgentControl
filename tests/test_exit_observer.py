import importlib.util
import json
import os
from dataclasses import replace
from pathlib import Path
import subprocess
import sys

import pytest

from agent_control.models import ProcessSnapshot
from test_process_control import _start
from test_process_identity import identity_module

MODULE = Path(__file__).resolve().parents[1] / "src/agent_control/exit_observer.py"


def observer_module():
    assert MODULE.exists(), "independent exit observation is missing"
    spec = importlib.util.spec_from_file_location("synthetic_observer", MODULE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def write_identity(start, birth):
    Path(start.transcript + ".engine.diagnostics.jsonl").write_text(json.dumps({
        "schema": 1, "observer": "engine", "event": "registered", "run_id": start.run_id,
        "engine_pid": start.engine_pid, "engine_birth": birth,
    }) + "\n", encoding="utf-8")


@pytest.mark.skipif(os.name != "nt", reason="Windows independent exit observation")
def test_independent_observer_retains_exit_code_without_changing_engine_history(tmp_path):
    module = observer_module()
    identity = identity_module()
    child = subprocess.Popen([sys.executable, "-c", "print('ready',flush=True); input()"],
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    observer = module.ExitObserver()
    try:
        assert child.stdout.readline().strip() == "ready"
        with identity.ProcessHandle.open(child.pid) as handle:
            start = replace(_start(child.pid), transcript=str(tmp_path / "synthetic.md"))
            snapshot = ProcessSnapshot(child.pid, (sys.executable, "SYNTHETIC_PRIVATE"),
                                       handle.created_epoch, True)
            write_identity(start, handle.birth)
        assert observer.attach(start, snapshot)
        child.kill()
        child.communicate(timeout=10)
        observer.poll(child.pid)
        rows = [json.loads(line) for line in Path(start.transcript + ".observer.diagnostics.jsonl").read_text().splitlines()]
        assert rows[0]["event"] == "observer_attached"
        assert rows[-1]["event"] == "observer_exit"
        assert rows[-1]["exit_code"] == child.returncode
        assert rows[-1]["exit_ticks"] and "reason" not in rows[-1]
        assert "SYNTHETIC_PRIVATE" not in json.dumps(rows)
    finally:
        observer.close()
        if child.poll() is None:
            child.kill()
        child.communicate(timeout=10)


def test_missing_or_mismatched_engine_birth_cannot_be_observed(tmp_path):
    module = observer_module()
    observer = module.ExitObserver()
    start = replace(_start(os.getpid()), transcript=str(tmp_path / "synthetic.md"))
    snapshot = ProcessSnapshot(os.getpid(), (sys.executable,), 0, True)
    try:
        assert observer.attach(start, snapshot) is False
        write_identity(start, "winft:1")
        assert observer.attach(start, snapshot) is False
        assert not Path(start.transcript + ".observer.diagnostics.jsonl").exists()
    finally:
        observer.close()


def test_unknown_observation_retains_handle_without_inventing_exit(tmp_path):
    module = observer_module()
    class Handle:
        birth, closed = "winft:123", False
        def poll(self):
            return {"state": "unknown", "exit_code": None, "exit_ticks": None}
        def close(self):
            self.closed = True
    handle = Handle()
    observer = module.ExitObserver(open_handle=lambda *a, **k: handle)
    start = replace(_start(123), transcript=str(tmp_path / "synthetic.md"))
    write_identity(start, handle.birth)
    snapshot = ProcessSnapshot(123, (sys.executable,), 1, True)
    assert observer.attach(start, snapshot)
    observer.poll(123)
    rows = [json.loads(line) for line in Path(start.transcript + ".observer.diagnostics.jsonl").read_text().splitlines()]
    assert rows[-1]["event"] == "observer_unknown" and rows[-1]["exit_code"] is None
    assert not any(row["event"] == "observer_exit" for row in rows)
    assert not handle.closed and observer.handles
    observer.close()
    assert handle.closed


def test_observer_storage_failure_warns_once_and_does_not_raise(tmp_path, capsys):
    module = observer_module()
    class Handle:
        birth, closed = "winft:123", False
        def poll(self):
            return {"state": "unknown", "exit_code": None, "exit_ticks": None}
        def close(self):
            self.closed = True
    handle = Handle()
    observer = module.ExitObserver(open_handle=lambda *a, **k: handle)
    start = replace(_start(123), transcript=str(tmp_path / "synthetic.md"))
    write_identity(start, handle.birth)
    Path(start.transcript + ".observer.diagnostics.jsonl").mkdir()
    assert observer.attach(start, ProcessSnapshot(123, (sys.executable,), 1, True))
    observer.poll(123)
    observer.close()
    assert handle.closed
    assert capsys.readouterr().err.count("independent exit diagnostics unavailable") == 1


def test_transient_unknown_does_not_lose_later_real_exit(tmp_path):
    module = observer_module()
    class Handle:
        birth, closed, calls = "winft:123", False, 0
        def poll(self):
            self.calls += 1
            return {"state": "unknown", "exit_code": None} if self.calls < 3 else {
                "state": "exited", "exit_code": 0, "exit_ticks": "123"}
        def close(self):
            self.closed = True
    handle = Handle()
    observer = module.ExitObserver(open_handle=lambda *a, **k: handle)
    start = replace(_start(123), transcript=str(tmp_path / "synthetic.md"))
    write_identity(start, handle.birth)
    assert observer.attach(start, ProcessSnapshot(123, (sys.executable,), 1, True))
    observer.poll(123)
    observer.poll(123)
    observer.poll(123)
    records = [json.loads(line) for line in Path(start.transcript + ".observer.diagnostics.jsonl").read_text().splitlines()]
    assert sum(row["event"] == "observer_unknown" for row in records) == 1
    assert records[-1]["event"] == "observer_exit" and records[-1]["exit_code"] == 0
    assert handle.closed and not observer.handles

import os
from pathlib import Path

import pytest

from test_ask_model_wrapper import _load
from test_lifecycle_diagnostics import rows
from test_fanout_synthesis import fanout  # noqa: F401


@pytest.mark.parametrize("code", [0, 7, 3221225786])
def test_wrapper_records_real_child_identity_and_observed_exit(tmp_path, monkeypatch, code):
    ask = _load()
    receipt = tmp_path / "launch.jsonl"
    class Child:
        pid = 4321
        returncode = code
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def wait(self):
            return code
    monkeypatch.setattr(ask.subprocess, "Popen", lambda *a, **kw: Child())
    # Old implementation may call run, but must never dispatch a real model.
    monkeypatch.setattr(ask.subprocess, "run", lambda *a, **kw: type("Result", (), {"returncode": code})())
    assert ask.main(["-C", str(tmp_path), "-m", "synthetic", "-p", "SYNTHETIC_PRIVATE",
                     "--launch-receipt", str(receipt), "--launch-id",
                     "c356dcf3-66ce-4f96-8952-89584b92db99"]) == code
    path = Path(str(receipt) + ".ask.diagnostics.jsonl")
    assert path.exists(), "wrapper exit evidence is missing"
    events = rows(path)
    assert [e["event"] for e in events] == ["observer_started", "child_start", "child_exit"]
    assert events[-1]["child_pid"] == 4321 and events[-1]["exit_code"] == code
    assert "engine_pid" not in events[-1], "a launcher PID is not necessarily the engine PID"
    assert events[-1]["windows_status"] == code & 0xffffffff
    assert "SYNTHETIC_PRIVATE" not in path.read_text(encoding="utf-8")


def test_optional_diagnostics_load_failure_does_not_block_wrapper(tmp_path, monkeypatch):
    ask = _load()
    class Child:
        pid, returncode = 4321, 0
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def wait(self):
            return 0
    def fail(*args, **kwargs):
        raise RuntimeError("SYNTHETIC_PRIVATE")
    monkeypatch.setattr(ask, "load_engine", fail)
    if hasattr(ask, "runpy"):
        monkeypatch.setattr(ask.runpy, "run_path", fail)
    monkeypatch.setattr(ask.subprocess, "Popen", lambda *a, **k: Child())
    assert ask.main(["-C", str(tmp_path), "-m", "synthetic", "-p", "SYNTHETIC_PRIVATE"]) == 0


def test_fanout_timeout_records_intent_before_kill(fanout, tmp_path, monkeypatch):
    path = tmp_path / ".claude/agent-runs/s-agent1-lens.md.fanout.diagnostics.jsonl"
    class Child:
        pid = 4321
        returncode = None
        calls = 0
        def communicate(self, timeout=None):
            self.calls += 1
            if self.calls == 1:
                raise fanout.subprocess.TimeoutExpired("SYNTHETIC_PRIVATE", timeout)
            return "", "SYNTHETIC_PRIVATE"
        def kill(self):
            assert path.exists(), "kill has no durable owner-side evidence"
            assert rows(path)[-1]["reason"] == "agent_timeout"
            assert rows(path)[-1]["event"] == "stop_requested"
            self.returncode = -9
    monkeypatch.setattr(fanout.subprocess, "Popen", lambda *a, **kw: Child())
    result = fanout.run_agent(1, "synthetic", "high", "lens", "SYNTHETIC_PRIVATE",
                              str(tmp_path), 3, "s", 1)
    assert result[3] is None
    assert rows(path)[-1]["event"] == "child_exit"
    assert rows(path)[-1]["exit_code"] == -9
    assert "SYNTHETIC_PRIVATE" not in path.read_text(encoding="utf-8")


def test_synthesis_timeout_has_pre_kill_evidence(fanout, tmp_path, monkeypatch):
    class Child:
        pid = 4321
        returncode = None
        calls = 0
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def communicate(self, timeout=None):
            self.calls += 1
            if self.calls == 1:
                raise fanout.subprocess.TimeoutExpired("SYNTHETIC_PRIVATE", timeout)
            return "", "SYNTHETIC_PRIVATE"
        def kill(self):
            paths = list(tmp_path.rglob("*.fanout.diagnostics.jsonl"))
            assert paths and rows(paths[0])[-1]["reason"] == "synthesis_timeout"
            self.returncode = -9
    monkeypatch.setattr(fanout.subprocess, "Popen", lambda *a, **kw: Child())
    monkeypatch.setattr(fanout.subprocess, "run", lambda *a, **kw: type(
        "Result", (), {"returncode": 0, "stdout": "synthetic", "stderr": ""})())
    fanout.synthesize("SYNTHETIC_PRIVATE", [(1, "synthetic", "lens", "SYNTHETIC_PRIVATE")],
                      "synthetic", str(tmp_path))
    paths = list(tmp_path.rglob("*.fanout.diagnostics.jsonl"))
    assert paths, "synthesis has no durable exit evidence"
    observed = rows(paths[0])
    assert any(row["event"] == "child_exit" and row["exit_code"] == -9 for row in observed)
    assert "SYNTHETIC_PRIVATE" not in paths[0].read_text(encoding="utf-8")

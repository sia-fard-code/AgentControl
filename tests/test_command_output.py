"""Unit tests for engine/command_output.py capture bounds (synthetic, tmp_path)."""
from __future__ import annotations

import subprocess
import sys
import time

import pytest

from test_engine_reasoning import engine  # noqa: F401


@pytest.fixture
def co(engine):
    return engine.capability_module("command_output")


def _py(code):
    return [sys.executable, "-c", code]


def test_small_output_stays_inline_without_sink(co, tmp_path):
    def no_sink(name):
        raise AssertionError("small output must not spill")
    r = co.capture_command(_py("print('hi'); import sys; sys.stderr.write('e')"),
                           cwd=str(tmp_path), timeout=30, open_sink=no_sink)
    assert r.returncode == 0 and r.stdout.prefix.strip() == b"hi"
    assert r.stdout.complete and r.stderr.prefix == b"e" and r.stdout.ref is None


def test_missing_artifact_dir_is_explicitly_incomplete(co, tmp_path):
    def no_dir(name):
        raise ValueError("no artifact directory for this run")
    r = co.capture_command(_py("import sys; sys.stdout.write('x' * 300000); sys.exit(4)"),
                           cwd=str(tmp_path), timeout=30, open_sink=no_dir)
    rec = r.stdout.record()
    assert r.returncode == 4  # real rc survives missing storage
    assert rec["ref"] is None and rec["capture_complete"] is False
    assert rec["error"] == "no_artifact_dir" and rec["total_bytes"] == 300000
    assert rec["discarded_bytes"] == 300000 - co.INLINE_BYTES
    assert len(r.stdout.prefix) == co.INLINE_BYTES


class _Cap:
    """Sink that accepts at most `room` bytes total, then short-writes."""
    def __init__(self, room):
        self.room, self.data, self.closed = room, bytearray(), False

    def write(self, b):
        n = min(len(b), self.room - len(self.data))
        self.data += b[:n]
        return n

    def close(self):
        self.closed = True
        return "cmd://synthetic"


def test_quota_short_write_keeps_draining_and_counts(co, tmp_path):
    sink = _Cap(10000)
    r = co.capture_command(_py("import sys; sys.stdout.write('y' * 500000)"),
                           cwd=str(tmp_path), timeout=30, open_sink=lambda n: sink)
    rec = r.stdout.record()
    assert r.returncode == 0 and sink.closed  # child not killed by volume
    assert rec["stored_bytes"] == 10000 and rec["error"] == "quota"
    assert rec["discarded_bytes"] == 490000 and rec["capture_complete"] is False
    assert rec["ref"] == "cmd://synthetic"


def test_timeout_with_inherited_pipe_is_finite(co, tmp_path, monkeypatch):
    monkeypatch.setattr(co, "JOIN_GRACE", 1.0)
    # grandchild inherits stdout and outlives the killed parent
    code = ("import subprocess, sys, time; subprocess.Popen([sys.executable, '-c', "
            "'import time; time.sleep(30)']); time.sleep(30)")
    t0 = time.monotonic()
    with pytest.raises(subprocess.TimeoutExpired):
        co.capture_command(_py(code), cwd=str(tmp_path), timeout=1,
                           open_sink=lambda n: None)
    assert time.monotonic() - t0 < 15

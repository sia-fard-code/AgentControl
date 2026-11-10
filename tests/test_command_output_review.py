"""Independent-review counterexamples; synthetic commands and temporary storage."""
import json
import sys
import threading

import pytest

from test_engine_reasoning import engine  # noqa: F401
from test_command_output_fixes import _recording_popen, _runner


@pytest.mark.parametrize("failure", ["missing", "count", "bytes", "close"])
def test_compact_storage_failure_counts_all_inaccessible_bytes(
        engine, tmp_path, monkeypatch, failure):
    runner = _runner(engine, tmp_path, monkeypatch,
                     "sys.stdout.write('S' * 3000); sys.exit(6)\n")
    try:
        out = runner.run_command(["python", "emit.py"])
        if failure == "missing":
            runner.caps.artifact_dir = None
        elif failure == "count":
            monkeypatch.setattr(engine, "MCP_MAX_ARTIFACTS", 0)
        elif failure == "bytes":
            monkeypatch.setattr(engine, "MCP_MAX_ARTIFACT_BYTES", 128)
        else:
            close = engine._CommandSink.close

            def fail_close(sink):
                close(sink)
                raise OSError("synthetic publication failure")
            monkeypatch.setattr(engine._CommandSink, "close", fail_close)
        compact = runner.compact_result("run_command", out)()
        assert compact.startswith("exit=6\n") and len(compact.encode()) <= 512
        rec = json.loads(compact.splitlines()[1])["stdout"]
        assert rec["capture_complete"] is False
        assert rec["discarded_bytes"] == 3000 - rec["stored_bytes"]
        if failure == "bytes":
            assert rec["ref"] and rec["stored_bytes"] == 128
            page = json.loads(runner.read_command_output(rec["ref"]))
            assert page["data"] == "S" * 128 and page["capture_complete"] is False
            assert page["discarded_bytes"] == 2872
        else:
            assert rec["ref"] is None and rec["stored_bytes"] == 0
    finally:
        runner.close()


def test_compact_missing_spilled_artifact_also_drops_prefix(engine, tmp_path, monkeypatch):
    runner = _runner(engine, tmp_path, monkeypatch, "sys.stdout.write('X' * 20000)\n")
    runner.caps.artifact_dir = None
    try:
        out = runner.run_command(["python", "emit.py"])
        compact = runner.compact_result("run_command", out)()
        rec = json.loads(compact.splitlines()[1])["stdout"]
        assert rec["ref"] is None and rec["capture_complete"] is False
        assert rec["stored_bytes"] == 0 and rec["discarded_bytes"] == 20000
    finally:
        runner.close()


def test_shutdown_does_not_wait_for_inflight_storage_lock(engine, tmp_path, monkeypatch):
    co = engine.capability_module("command_output")
    monkeypatch.setattr(co, "JOIN_GRACE", 0.05)
    started, release, closed = threading.Event(), threading.Event(), threading.Event()
    result, errors = [], []
    baseline = set(threading.enumerate())

    class Sink:
        def write(self, data):
            started.set()
            release.wait(10)
            return len(data)

        def close(self):
            assert release.is_set(), "must not close a sink while its writer is active"
            closed.set()
            return "cmd://synthetic-stalled"

    def run():
        try:
            result.append(co.capture_command(
                [sys.executable, "-c", "import sys; sys.stdout.write('X' * 5000)"],
                cwd=str(tmp_path), timeout=10, open_sink=lambda name: Sink()))
        except BaseException as exc:
            errors.append(exc)

    caller = threading.Thread(target=run)
    caller.start()
    try:
        assert started.wait(5)
        caller.join(2)
        assert not caller.is_alive(), "cleanup reacquired the lock held by storage I/O"
        assert not errors and result[0].returncode == 0
        assert result[0].stdout.complete is False and result[0].stdout.ref is None
        assert not closed.is_set()
    finally:
        release.set()
        caller.join(10)
        for thread in set(threading.enumerate()) - baseline:
            thread.join(5)
    assert closed.is_set() and not errors


def test_thread_start_failure_keeps_effect_unknown_and_reaps_child(
        engine, tmp_path, monkeypatch):
    runner = _runner(engine, tmp_path, monkeypatch, "print('synthetic')\n")
    co = engine.capability_module("command_output")
    procs = _recording_popen(co, monkeypatch)

    def fail_start(thread):
        raise RuntimeError("synthetic thread resource failure")

    monkeypatch.setattr(co.threading.Thread, "start", fail_start)
    try:
        try:
            out = runner.run_command(["python", "emit.py"])
        except RuntimeError:
            out = "communication failure escaped the outcome-unknown boundary"
        assert out.startswith("error: command outcome unavailable")
        assert runner.command_timed_out
        assert runner._last_cmd_info == ("outcome_unknown", True, None, False)
        assert len(procs) == 1 and procs[0].returncode is not None
        assert procs[0].stdout.closed and procs[0].stderr.closed
    finally:
        runner.close()

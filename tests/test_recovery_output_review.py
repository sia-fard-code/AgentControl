"""Independent review counterexamples; synthetic data and temporary roots only."""
import io

import pytest

from test_engine_reasoning import engine  # noqa: F401


def _runner(engine, root):
    return engine.Runner(str(root), profile="resilient-v1", skills_home=str(root),
                         skills_root=str(root))


def test_normal_read_preserves_later_result_reservation(engine, tmp_path):
    cg = engine.capability_module("context_guard")
    guard = cg.Guard(profile="resilient-v1")
    assert guard.start_batch({}, 2)
    guard.admit("read_file", "x" * cg.BATCH_BUDGET)
    guard.admit("read_file", "x")
    assert guard.batch <= cg.BATCH_BUDGET
    assert guard.output <= cg.OUTPUT_BUDGET
    assert guard.read <= cg.READ_BUDGET


def test_clipped_utf8_prefix_does_not_hide_later_matches(engine, tmp_path):
    (tmp_path / "a.txt").write_bytes(b"a" * 8191 + "é\nneedle\n".encode("utf-8"))
    runner = _runner(engine, tmp_path)
    result = runner._grep_page("needle", "*.txt", 0, 10, use_rg=False)
    assert "a.txt:2:needle" in result


def test_timer_start_failure_reaps_owned_search(engine, tmp_path, monkeypatch):
    runner = _runner(engine, tmp_path)
    paged = runner._paged

    class Child:
        def __init__(self):
            self.stdout = io.BytesIO()
            self.killed = self.waited = False

        def poll(self):
            return -9 if self.killed else None

        def kill(self):
            self.killed = True

        def wait(self, timeout=None):
            self.waited = True
            return 0

    class Timer:
        def __init__(self, *args, **kwargs):
            pass

        def start(self):
            raise RuntimeError("synthetic timer startup failure")

        def cancel(self):
            pass

    child = Child()
    monkeypatch.setattr(paged.subprocess, "Popen", lambda *a, **kw: child)
    monkeypatch.setattr(paged.threading, "Timer", Timer)
    with pytest.raises(RuntimeError, match="synthetic timer startup failure"):
        runner._grep_page("needle", "*.txt", 0, 10, use_rg=True)
    assert child.killed and child.waited and child.stdout.closed

from contextlib import nullcontext
from datetime import datetime, timezone
from types import SimpleNamespace

from agent_control.sync import Synchronizer


def test_retained_observers_are_polled_even_when_no_open_run_needs_reclassification():
    calls = []
    sync = Synchronizer.__new__(Synchronizer)
    sync.locks = SimpleNamespace(write=nullcontext())
    sync.process_probe = SimpleNamespace(poll_observers=lambda: calls.append("observed"))
    sync.clock = lambda: datetime(2026, 10, 28, tzinfo=timezone.utc)
    sync.min_interval_seconds = 2
    sync._last_run_at = sync.clock()
    sync._last_report = object()
    sync._run_locked = lambda: (_ for _ in ()).throw(AssertionError("accounting should remain cached"))
    assert sync.run() is sync._last_report
    assert calls == ["observed"]


def test_observer_poll_error_does_not_change_cached_accounting():
    sync = Synchronizer.__new__(Synchronizer)
    sync.locks = SimpleNamespace(write=nullcontext())
    def fail():
        raise RuntimeError("SYNTHETIC_PRIVATE")
    sync.process_probe = SimpleNamespace(poll_observers=fail)
    sync.clock = lambda: datetime(2026, 10, 28, tzinfo=timezone.utc)
    sync.min_interval_seconds = 2
    sync._last_run_at = sync.clock()
    sync._last_report = object()
    assert sync.run() is sync._last_report

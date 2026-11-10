from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from agent_control.dashboard import DashboardApp, create_server
from agent_control.instance_lock import (
    InstanceLockError,
    acquire_instance_lock,
)
from agent_control.models import SyncReport
from agent_control.paths import AppPaths
from agent_control.storage import StateLocks


def _clock() -> datetime:
    return datetime(2026, 9, 17, 12, 0, tzinfo=timezone.utc)


def _make_app(tmp_path: Path) -> DashboardApp:
    paths = AppPaths.for_test(tmp_path)
    synchronizer = MagicMock()
    synchronizer.run.return_value = SyncReport(0, 0, 0, 0, 0, False, ())
    return DashboardApp(paths, StateLocks(), synchronizer, _clock)


@pytest.mark.skipif(
    os.name != "nt", reason="SO_EXCLUSIVEADDRUSE only refuses reuse on Windows"
)
def test_second_server_on_same_port_fails(tmp_path: Path) -> None:
    app = _make_app(tmp_path)
    first = create_server(app, port=0)
    try:
        port = first.server_address[1]
        with pytest.raises(OSError):
            second = create_server(app, port=port)
            second.server_close()
    finally:
        first.server_close()


def test_second_instance_lock_fails(tmp_path: Path) -> None:
    lock = acquire_instance_lock(tmp_path)
    try:
        with pytest.raises(InstanceLockError):
            acquire_instance_lock(tmp_path)
    finally:
        lock.release()


def test_lock_reacquirable_after_release(tmp_path: Path) -> None:
    lock = acquire_instance_lock(tmp_path)
    lock.release()
    lock2 = acquire_instance_lock(tmp_path)
    lock2.release()


def test_lock_file_contains_pid(tmp_path: Path) -> None:
    # On Windows the byte-0 lock blocks reads while held, so read after
    # release; the pid the process wrote persists in the file.
    lock = acquire_instance_lock(tmp_path)
    lock.release()
    text = (tmp_path / "serve.lock").read_text()
    assert text.strip() == str(os.getpid())


def test_lock_is_context_manager(tmp_path: Path) -> None:
    with acquire_instance_lock(tmp_path):
        with pytest.raises(InstanceLockError):
            acquire_instance_lock(tmp_path)

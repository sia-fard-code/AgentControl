from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path

import pytest

from agent_control.paths import AppPaths


@pytest.fixture(autouse=True)
def isolated_operator_home(tmp_path: Path, monkeypatch) -> None:
    """Keep operator settings and subprocess home lookups inside each test."""
    # Missing settings are valid legacy behavior. Do not create entries inside
    # tests' candidate roots, or collide with their own deliberately named homes.
    home = tmp_path / ".operator-home-isolation"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    # Cost estimation is off by default in the product. The existing suite keeps
    # exercising it explicitly; test_cost_feature_flag.py covers the default.
    monkeypatch.setenv("AGENTCONTROL_ENABLE_COST", "1")


@pytest.fixture
def temp_paths(tmp_path: Path) -> AppPaths:
    return AppPaths.for_test(tmp_path)


@pytest.fixture
def fixed_now():
    return lambda: datetime(2026, 9, 17, 12, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def guard_repo_index_files(tmp_path: Path, monkeypatch) -> None:
    """Fail any test whose sync reads a repo run index outside tmp_path.

    AGENTS.md requires tests to use temporary paths only. Sync discovers repo
    run index files from real project roots; if a test leaks a real root, the
    deployed engine's live run indexes under those roots would be scanned. This
    autouse guard wraps the real ``repo_index_files`` and fails the test if any
    returned path is not under the test's ``tmp_path``. Tests that never call
    sync are unaffected because the wrapper is never invoked.
    """
    import agent_control.sync as sync_module

    real = sync_module.repo_index_files
    allowed_prefix = os.path.normcase(os.path.abspath(str(tmp_path)))

    def guarded(*args, **kwargs):
        result = real(*args, **kwargs)
        for path in result:
            normalized = os.path.normcase(os.path.abspath(str(path)))
            if not normalized.startswith(allowed_prefix):
                pytest.fail(
                    "sync read a repo run index outside tmp_path: "
                    f"{path!r} (allowed prefix: {allowed_prefix!r})"
                )
        return result

    monkeypatch.setattr(sync_module, "repo_index_files", guarded)


@pytest.fixture(autouse=True)
def guard_tool_readers(tmp_path: Path, monkeypatch) -> None:
    """Fail any test whose tool import reads a source outside tmp_path.

    AGENTS.md requires tests to use temporary paths only. The tool-usage
    readers default to live sources such as ``%USERPROFILE%\\.claude``,
    ``.codex``, ``%APPDATA%`` and ``.local\\share\\kilo``. This autouse guard
    wraps the real ``tool_import._load_reader`` so that every reader it
    returns fails the test, before reading, when its ``source`` is not under
    the test's ``tmp_path``. Tests that never load a reader are unaffected
    because the wrapper is never invoked.
    """
    import agent_control.tool_import as tool_import_module

    real = tool_import_module._load_reader
    allowed_prefix = os.path.normcase(os.path.abspath(str(tmp_path)))

    def guarded_load(*args, **kwargs):
        read = real(*args, **kwargs)

        def guarded_read(source, *rest, **options):
            normalized = os.path.normcase(os.path.abspath(str(source)))
            if not (normalized == allowed_prefix
                    or normalized.startswith(allowed_prefix + os.sep)):
                pytest.fail(
                    "a tool reader was given a source outside tmp_path: "
                    f"{source!r} (allowed prefix: {allowed_prefix!r})"
                )
            return read(source, *rest, **options)

        return guarded_read

    monkeypatch.setattr(tool_import_module, "_load_reader", guarded_load)

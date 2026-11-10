from __future__ import annotations

import json
import os
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from agent_control.models import TaskStatusSource
from agent_control.task_sources import TaskSourcesError, load_task_sources

PIN = "a" * 64
PIN2 = "b" * 64


def _entry(tmp_path: Path, **over) -> dict:
    root = tmp_path / "proj"
    status = tmp_path / "parent" / "task-status.json"
    e = {"project_id": "proj-1", "project_root": str(root),
         "envelope_sha256": PIN, "path": str(status)}
    e.update(over)
    return e


def _write(tmp_path: Path, payload, raw: str | None = None) -> Path:
    reg = tmp_path / "operator" / "task-sources.json"
    reg.parent.mkdir(parents=True, exist_ok=True)
    reg.write_text(raw if raw is not None else json.dumps(payload), encoding="utf-8")
    return reg


def _code(reg: Path) -> str:
    with pytest.raises(TaskSourcesError) as info:
        load_task_sources(reg)
    return info.value.code


def test_valid_registry_returns_frozen_known_sources(tmp_path):
    e = _entry(tmp_path)
    got = load_task_sources(_write(tmp_path, {"schema": 1, "sources": [e]}))
    assert isinstance(got, tuple) and len(got) == 1
    src = got[0]
    assert type(src) is TaskStatusSource
    assert (src.project_id, src.project_root, src.envelope_sha256) == (
        "proj-1", e["project_root"], PIN)
    assert str(src.path) == e["path"]
    with pytest.raises(FrozenInstanceError):
        src.project_id = "x"  # type: ignore[misc]


def test_empty_sources_is_empty_tuple(tmp_path):
    assert load_task_sources(_write(tmp_path, {"schema": 1, "sources": []})) == ()


@pytest.mark.parametrize("raw", [
    "", "null", "[]", '{"schema":1}', '{"sources":[]}',
    '{"schema":true,"sources":[]}', '{"schema":1.0,"sources":[]}',
    '{"schema":2,"sources":[]}', '{"schema":"1","sources":[]}',
    '{"schema":1,"sources":null}', '{"schema":1,"sources":[],"x":1}',
    '{"schema":1,"schema":1,"sources":[]}', '{"schema":NaN,"sources":[]}',
    '{"schema":Infinity,"sources":[]}', "\ufeff{}", "{not json",
])
def test_closed_top_level_rejects(tmp_path, raw):
    _code(_write(tmp_path, None, raw=raw))


def _bad_entries(tmp_path: Path) -> list:
    root = tmp_path / "proj"
    return [
        None, [], "x",
        {k: v for k, v in _entry(tmp_path).items() if k != "path"},
        {**_entry(tmp_path), "extra": 1},
        _entry(tmp_path, project_id=None), _entry(tmp_path, project_id=True),
        _entry(tmp_path, project_id=1), _entry(tmp_path, project_id=""),
        _entry(tmp_path, project_id="bad id/../x"),
        _entry(tmp_path, envelope_sha256="A" * 64),
        _entry(tmp_path, envelope_sha256="a" * 63),
        _entry(tmp_path, envelope_sha256=None),
        _entry(tmp_path, path=None), _entry(tmp_path, path=False),
        _entry(tmp_path, path="relative/task-status.json"),
        _entry(tmp_path, project_root="relative"),
        _entry(tmp_path, path=r"\\server\share\task-status.json"),
        _entry(tmp_path, path="//server/share/task-status.json"),
        _entry(tmp_path, path=str(tmp_path / "a\x00b.json")),
        _entry(tmp_path, path=str(tmp_path / "parent" / ".." / "parent" / "t.json")),
        _entry(tmp_path, path=str(tmp_path / "parent") + os.sep + "." + os.sep + "t.json"),
        _entry(tmp_path, path=str(root / "task-status.json")),
        _entry(tmp_path, path=str(root)),
        _entry(tmp_path, project_root=str(tmp_path / "parent")),
    ]


def test_entry_rejects(tmp_path):
    for bad in _bad_entries(tmp_path):
        reg = _write(tmp_path, {"schema": 1, "sources": [bad]})
        with pytest.raises(TaskSourcesError):
            load_task_sources(reg)
            pytest.fail(f"accepted {bad!r}")


def test_duplicates(tmp_path):
    e = _entry(tmp_path)
    for dup in (
        e,  # identical triple
        _entry(tmp_path, envelope_sha256=PIN2),  # same path, other pin
        _entry(tmp_path, path=str(tmp_path / "p2" / "t.json")),  # same project+pin
        _entry(tmp_path, project_id="proj-2", project_root=str(tmp_path / "q")),
    ):
        _code(_write(tmp_path, {"schema": 1, "sources": [e, dup]}))
    if os.name == "nt":  # case aliases of one path
        alias = _entry(tmp_path, envelope_sha256=PIN2, path=e["path"].upper())
        _code(_write(tmp_path, {"schema": 1, "sources": [e, alias]}))
    ok = _entry(tmp_path, envelope_sha256=PIN2, path=str(tmp_path / "p2" / "t.json"))
    assert len(load_task_sources(_write(tmp_path, {"schema": 1, "sources": [e, ok]}))) == 2


def _many(tmp_path: Path, n: int) -> list:
    return [_entry(tmp_path, envelope_sha256=f"{i:064x}",
                   path=str(tmp_path / f"s{i}" / "t.json")) for i in range(n)]


def test_entry_count_limit(tmp_path):
    assert len(load_task_sources(_write(tmp_path, {"schema": 1, "sources": _many(tmp_path, 64)}))) == 64
    _code(_write(tmp_path, {"schema": 1, "sources": _many(tmp_path, 65)}))


def test_raw_size_limit(tmp_path):
    raw = json.dumps({"schema": 1, "sources": []})
    _code(_write(tmp_path, None, raw=raw + " " * (64 * 1024 - len(raw) + 1)))
    assert load_task_sources(_write(tmp_path, None, raw=raw + " " * (64 * 1024 - len(raw)))) == ()


def test_missing_is_error_not_empty(tmp_path):
    assert _code(tmp_path / "nope.json") == "task_sources_missing"
    assert _code(tmp_path) != ""  # directory


def test_links_rejected(tmp_path):
    real = _write(tmp_path, {"schema": 1, "sources": []})
    hard = tmp_path / "hard.json"
    os.link(real, hard)
    _code(hard)
    _code(real)  # nlink now 2
    os.unlink(hard)
    sym = tmp_path / "sym.json"
    try:
        sym.symlink_to(real)
    except OSError:
        pytest.skip("symlinks unavailable")
    _code(sym)
    linkdir = tmp_path / "linkdir"
    linkdir.symlink_to(real.parent, target_is_directory=True)
    _code(linkdir / real.name)


def test_error_text_has_no_content_or_path(tmp_path):
    sentinel = "SENTINEL_9f3c"
    reg = _write(tmp_path, None, raw='{"schema":1,"sources":[{"%s":1}]}' % sentinel)
    with pytest.raises(TaskSourcesError) as info:
        load_task_sources(reg)
    text = str(info.value) + repr(info.value)
    assert sentinel not in text and str(tmp_path) not in text
    assert info.value.__cause__ is None and info.value.__suppress_context__


class _Stop(Exception):
    pass


def _serve(tmp_path, monkeypatch, extra: list[str]):
    from agent_control import cli, dashboard, instance_lock, liveness, sync
    calls: dict = {}

    def lock(*a, **k):
        calls["lock"] = True
        raise AssertionError("lock acquired")

    class Sync:
        def __init__(self, *a, **k):
            calls["sync"] = True

    class App:
        def __init__(self, *a, **k):
            calls["app_kwargs"] = k

    def server(*a, **k):
        raise _Stop

    monkeypatch.setattr(instance_lock, "acquire_instance_lock", lock)
    monkeypatch.setattr(sync, "Synchronizer", Sync)
    monkeypatch.setattr(dashboard, "DashboardApp", App)
    monkeypatch.setattr(dashboard, "create_server", server)
    monkeypatch.setattr(liveness, "PsutilProcessProbe", lambda: object())
    monkeypatch.setattr(cli, "_tool_importer_for", lambda *a, **k: None)
    args = cli.build_parser().parse_args(
        ["--state-dir", str(tmp_path / "state"), "serve", *extra])
    return cli, args, calls


@pytest.mark.parametrize("make", ["missing", "corrupt"])
def test_cli_refuses_bad_registry_before_locks(tmp_path, monkeypatch, capsys, make):
    reg = tmp_path / "reg.json"
    if make == "corrupt":
        reg.write_text('{"schema":1,"sources":[{"SENTINEL_77":1}]}', encoding="utf-8")
    cli, args, calls = _serve(tmp_path, monkeypatch, ["--task-sources", str(reg)])
    assert cli._run_serve(args) == 2
    assert calls == {}
    err = capsys.readouterr().err
    assert "task_sources_" in err
    assert "SENTINEL_77" not in err and str(tmp_path) not in err


def _serve_ok(tmp_path, monkeypatch, extra):
    cli, args, calls = _serve(tmp_path, monkeypatch, extra)
    from agent_control import instance_lock

    class Lock:
        def release(self):
            pass

        def close(self):
            pass

    monkeypatch.setattr(instance_lock, "acquire_instance_lock", lambda *a, **k: Lock())
    with pytest.raises(_Stop):
        cli._run_serve(args)
    return calls["app_kwargs"]


def test_cli_flag_absent_passes_none(tmp_path, monkeypatch):
    kw = _serve_ok(tmp_path, monkeypatch, [])
    assert kw.get("task_status_sources") is None


def test_cli_empty_registry_passes_empty_tuple(tmp_path, monkeypatch):
    reg = _write(tmp_path, {"schema": 1, "sources": []})
    kw = _serve_ok(tmp_path, monkeypatch, ["--task-sources", str(reg)])
    assert kw["task_status_sources"] == ()


def test_cli_registry_passes_typed_sources(tmp_path, monkeypatch):
    reg = _write(tmp_path, {"schema": 1, "sources": [_entry(tmp_path)]})
    kw = _serve_ok(tmp_path, monkeypatch, ["--task-sources", str(reg)])
    (src,) = kw["task_status_sources"]
    assert type(src) is TaskStatusSource


@pytest.mark.parametrize("configured", [False, True])
def test_real_app_snapshot_distinguishes_absent_from_empty_registry(tmp_path, configured):
    from datetime import datetime, timezone
    from unittest.mock import MagicMock
    from agent_control.dashboard import DashboardApp
    from agent_control.paths import AppPaths
    from agent_control.snapshot import SnapshotQuery
    from agent_control.storage import StateLocks

    sources = load_task_sources(_write(tmp_path, {"schema": 1, "sources": []})) if configured else None
    paths = AppPaths.for_test(tmp_path / "app")
    app = DashboardApp(paths, StateLocks(), MagicMock(),
                       lambda: datetime(2026, 11, 1, tzinfo=timezone.utc),
                       task_status_sources=sources)
    snapshot = app.snapshot(SnapshotQuery())
    assert ("goal_tasks" in snapshot) is configured
    if configured:
        assert snapshot["goal_tasks"] == []
    assert snapshot["runs"] == []

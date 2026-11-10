from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from agent_control import cli
from agent_control.models import Diagnostic
from agent_control.paths import AppPaths
from agent_control.storage import StateLocks
from agent_control.sync import Synchronizer
from agent_control.tool_import import ImportReport, SourceSetting, write_usage_sources


class FakeProbe:
    def inspect(self, pid: int):
        return None


def _now() -> datetime:
    return datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)


def _empty_report(*diagnostics: Diagnostic) -> ImportReport:
    return ImportReport(applied=True, tools=(), new_project_roots=(),
                        diagnostics=tuple(diagnostics))


def test_sync_calls_importer_once_per_run(temp_paths: AppPaths) -> None:
    calls: list[int] = []

    def importer() -> ImportReport:
        calls.append(1)
        return _empty_report(Diagnostic("kilo_bad_line", "kilo: bad line",
                                        source="kilo.db", line=3))

    sync = Synchronizer(temp_paths, StateLocks(), _now, FakeProbe(),
                        tool_importer=importer)
    report = sync.run()
    assert calls == [1]
    assert [d.code for d in report.diagnostics].count("kilo_bad_line") == 1
    sync.run()
    assert calls == [1, 1]


def test_importer_failure_becomes_diagnostic(temp_paths: AppPaths) -> None:
    def importer() -> ImportReport:
        raise RuntimeError("SENTINEL-TEXT-DO-NOT-STORE")

    sync = Synchronizer(temp_paths, StateLocks(), _now, FakeProbe(),
                        tool_importer=importer)
    report = sync.run()
    failed = [d for d in report.diagnostics if d.code == "tool_import_failed"]
    assert len(failed) == 1
    assert failed[0].message == "RuntimeError"
    assert "SENTINEL-TEXT-DO-NOT-STORE" not in repr(report)
    assert report.scanned_records == 0


def test_sync_without_importer_has_no_tool_diagnostics(temp_paths: AppPaths) -> None:
    report = Synchronizer(temp_paths, StateLocks(), _now, FakeProbe()).run()
    assert not any(d.code.startswith("tool_") for d in report.diagnostics)
    assert not temp_paths.tool_usage.exists()


def test_tool_importer_for_none_when_nothing_enabled(tmp_path: Path,
                                                     temp_paths: AppPaths) -> None:
    home = tmp_path / "home"
    locks = StateLocks()
    assert cli._tool_importer_for(temp_paths, locks, _now, home=home, env={}) is None
    db = tmp_path / "kilo.db"
    db.write_bytes(b"")
    write_usage_sources(temp_paths.usage_sources,
                        {"kilo": SourceSetting(enabled=False, path=str(db))},
                        locks.write)
    assert cli._tool_importer_for(temp_paths, locks, _now, home=home, env={}) is None


def test_tool_importer_for_skips_enabled_tool_with_missing_source(
        tmp_path: Path, temp_paths: AppPaths) -> None:
    locks = StateLocks()
    write_usage_sources(temp_paths.usage_sources,
                        {"kilo": SourceSetting(enabled=True,
                                               path=str(tmp_path / "absent.db"))},
                        locks.write)
    assert cli._tool_importer_for(temp_paths, locks, _now,
                                  home=tmp_path / "home", env={}) is None


def test_tool_importer_for_returns_callable_for_enabled_kilo(
        tmp_path: Path, temp_paths: AppPaths, monkeypatch) -> None:
    import agent_control.tool_import as tool_import

    home = tmp_path / "home"
    locks = StateLocks()
    db = tmp_path / "kilo.db"
    db.write_bytes(b"")
    write_usage_sources(temp_paths.usage_sources,
                        {"kilo": SourceSetting(enabled=True, path=str(db)),
                         "codex": SourceSetting(enabled=False, path=None)},
                        locks.write)
    seen: list[dict] = []

    def fake_import(paths, locks_arg, **kwargs):
        seen.append({"paths": paths, "locks": locks_arg, **kwargs})
        return _empty_report()

    monkeypatch.setattr(tool_import, "import_tool_usage", fake_import)
    importer = cli._tool_importer_for(temp_paths, locks, _now, home=home, env={})
    assert callable(importer)
    assert seen == []
    report = importer()
    assert report.diagnostics == ()
    assert len(seen) == 1
    call = seen[0]
    assert call["paths"] is temp_paths and call["locks"] is locks
    assert list(call["tools"]) == ["kilo"]
    assert call["sources"] == {"kilo": db}
    assert call["apply"] is True
    assert call["now"] == _now()
    assert call["home"] == home

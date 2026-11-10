import json
from pathlib import Path

import pytest

from agent_control.cli import main


def test_help_is_available_without_creating_runtime_state(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    state_dir = tmp_path / "state"

    with pytest.raises(SystemExit) as exit_info:
        main(["--state-dir", str(state_dir), "--help"])

    assert exit_info.value.code == 0
    assert "sync" in capsys.readouterr().out
    assert not state_dir.exists()


def test_sync_command_emits_strict_json_report(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    state_dir = tmp_path / "state"
    machine_index = tmp_path / "machine-index.jsonl"
    machine_index.write_text("", encoding="utf-8")

    exit_code = main(
        [
            "--state-dir",
            str(state_dir),
            "--machine-index",
            str(machine_index),
            "sync",
        ]
    )

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["appended_revisions"] == 0
    assert payload["scanned_records"] == 0


def test_migrate_dry_run_reports_without_writing_state(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    import shutil

    fixtures = Path(__file__).parent / "fixtures" / "migration"
    projecta = tmp_path / "Projecta"
    projectb = tmp_path / "Projectb"
    shutil.copytree(fixtures / "projecta", projecta)
    shutil.copytree(fixtures / "projectb", projectb)
    index = tmp_path / "machine-index.jsonl"
    template = (fixtures / "machine-index.jsonl").read_text(encoding="utf-8")
    index.write_text(
        template.replace("__PROJECTA_ROOT__", str(projecta).replace("\\", "\\\\")),
        encoding="utf-8",
    )
    state_dir = tmp_path / "state"
    report = tmp_path / "report.json"

    exit_code = main(
        [
            "--state-dir",
            str(state_dir),
            "--machine-index",
            str(index),
            "migrate",
            "--dry-run",
            "--projecta",
            str(projecta),
            "--projectb",
            str(projectb),
            "--report",
            str(report),
        ]
    )

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["applicable"] is True
    assert payload["logical_runs"] >= 2
    assert report.exists()
    assert not (state_dir / "ledger.jsonl").exists()


def test_projects_list_is_empty_strict_json(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    state_dir = tmp_path / "state"
    exit_code = main(["--state-dir", str(state_dir), "projects", "list"])
    assert exit_code == 0
    assert json.loads(capsys.readouterr().out) == []


def test_projects_approve_maps_verb_to_state(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from datetime import datetime, timezone

    from agent_control.projects import ProjectRegistry
    from agent_control.storage import StateLocks

    state_dir = tmp_path / "state"
    registry = ProjectRegistry(
        state_dir / "projects.jsonl",
        StateLocks(),
        lambda: datetime(2026, 9, 17, 12, 0, tzinfo=timezone.utc),
    )
    project, _ = registry.discover(r"D:\repos\Projectb")

    exit_code = main(
        ["--state-dir", str(state_dir), "projects", "approve", project.project_id]
    )
    assert exit_code == 0
    assert json.loads(capsys.readouterr().out)["state"] == "approved"


def test_pricing_show_reports_uninitialized_baseline(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    state_dir = tmp_path / "state"
    pricing = tmp_path / "pricing.json"
    pricing.write_text(
        json.dumps(
            {
                "schema": 1,
                "currency": "USD",
                "unit_tokens": 1000000,
                "initialized": False,
                "models": {},
            }
        ),
        encoding="utf-8",
    )
    exit_code = main(
        ["--state-dir", str(state_dir), "--pricing", str(pricing), "pricing", "show"]
    )
    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["baseline"] == {}
    assert payload["history"] == []
    assert not (state_dir / "pricing_history.jsonl").exists()


SENTINEL = "SENTINEL-TEXT-DO-NOT-STORE"


def _reader_call(tool: str, key: str, cwd: str, at: str, sent: int) -> dict:
    return {"tool": tool, "key": key, "session": "s-1", "root_session": "s-1",
            "at": at, "model": "model-1", "cwd": cwd, "tokens_sent": sent,
            "tokens_returned": 20, "cache_read": 5, "cache_write": 7,
            "cache_write_5m": None, "reasoning": None,
            "sent_includes_cache_read": False, "speed": None}


def _fake_tools(tmp_path: Path, monkeypatch, *, with_sources: bool = True) -> dict:
    """Fake readers and default sources, all under tmp_path."""
    import os

    import agent_control.tool_import as tool_import
    from agent_control.models import Diagnostic

    project = tmp_path / "proj"
    (project / ".git").mkdir(parents=True)
    sources_root = tmp_path / "sources"
    if with_sources:
        for tool in ("kilo", "codex"):
            (sources_root / tool).mkdir(parents=True)
    read_log: list[str] = []
    prefix = os.path.normcase(str(tmp_path))

    def make_reader(tool: str):
        def read(source, checkpoint, *, now):
            assert os.path.normcase(str(source)).startswith(prefix)
            read_log.append(tool)
            calls = [_reader_call(tool, f"{tool}-1", str(project),
                                  "2026-10-01T09:00:00+00:00", 100),
                     _reader_call(tool, f"{tool}-2", str(project),
                                  "2026-10-01T10:00:00+00:00", 50)]
            diags = [Diagnostic(f"{tool}_bad_line", SENTINEL, source=str(source),
                                line=2)]
            return calls, {"offset": 2}, diags
        return read

    def fake_default(tool, *, home, env):
        return sources_root / tool

    monkeypatch.setattr(tool_import, "_load_reader", make_reader)
    monkeypatch.setattr(tool_import, "default_source", fake_default)
    return {"read_log": read_log, "sources_root": sources_root}


def _state_files(state_dir: Path) -> list[str]:
    if not state_dir.exists():
        return []
    return sorted(str(p.relative_to(state_dir)) for p in state_dir.rglob("*"))


def test_import_usage_dry_run_prints_report_and_writes_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch
) -> None:
    fakes = _fake_tools(tmp_path, monkeypatch)
    state_dir = tmp_path / "state"
    before = _state_files(state_dir)
    code = main(["--state-dir", str(state_dir), "import-usage",
                 "--tool", "kilo", "--tool", "codex"])
    out = capsys.readouterr().out
    assert code == 0
    assert fakes["read_log"] == ["kilo", "codex"]
    lines = out.splitlines()
    kilo = next(line for line in lines if line.startswith("kilo: "))
    assert kilo.startswith("kilo: 2 calls (2 new, 0 revised), 1 sessions, ")
    assert "2026-10-01T09:00:00" in kilo and " to 2026-10-01T10:00:00" in kilo
    assert kilo.endswith(", sent 150, returned 40, cache read 10, cache write 14")
    assert any(line.startswith("codex: 2 calls (2 new, 0 revised)") for line in lines)
    assert "New projects pending approval: 1" in lines
    assert f"  {tmp_path / 'proj'}" in lines
    assert "2 diagnostics:" in lines
    assert any("kilo_bad_line" in line for line in lines)
    assert SENTINEL not in out
    assert lines[-1] == "Dry run: nothing written."
    assert _state_files(state_dir) == before


def test_import_usage_apply_writes_usage_and_enables_tools(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch
) -> None:
    from agent_control.storage import StateLocks
    from agent_control.tool_import import (
        SourceSetting, load_usage_sources, write_usage_sources)

    _fake_tools(tmp_path, monkeypatch)
    state_dir = tmp_path / "state"
    custom = tmp_path / "custom-kilo"
    custom.mkdir()
    write_usage_sources(state_dir / "usage_sources.json",
                        {"kilo": SourceSetting(enabled=False, path=str(custom))},
                        StateLocks().write)
    code = main(["--state-dir", str(state_dir), "import-usage", "--apply",
                 "--tool", "kilo", "--tool", "codex"])
    out = capsys.readouterr().out
    assert code == 0
    assert out.splitlines()[-1] == "Imported. Enabled: kilo, codex."
    usage = (state_dir / "tool_usage.jsonl").read_text(encoding="utf-8")
    assert len(usage.splitlines()) == 4
    assert SENTINEL not in usage
    settings, diags = load_usage_sources(state_dir / "usage_sources.json")
    assert diags == []
    assert settings["kilo"] == SourceSetting(enabled=True, path=str(custom))
    assert settings["codex"] == SourceSetting(enabled=True, path=None)


def test_import_usage_apply_refuses_while_dashboard_runs(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch
) -> None:
    from agent_control.instance_lock import acquire_instance_lock

    fakes = _fake_tools(tmp_path, monkeypatch)
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    lock = acquire_instance_lock(state_dir)
    try:
        code = main(["--state-dir", str(state_dir), "import-usage", "--apply",
                     "--tool", "kilo"])
    finally:
        lock.release()
    captured = capsys.readouterr()
    assert code == 2
    assert ("The dashboard is running. Stop it, run import-usage --apply, "
            "then start it again.") in captured.out + captured.err
    assert fakes["read_log"] == []
    assert not (state_dir / "tool_usage.jsonl").exists()


def test_import_usage_single_tool_imports_only_that_tool(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch
) -> None:
    import json as _json

    fakes = _fake_tools(tmp_path, monkeypatch)
    state_dir = tmp_path / "state"
    code = main(["--state-dir", str(state_dir), "import-usage", "--apply",
                 "--tool", "kilo"])
    out = capsys.readouterr().out
    assert code == 0
    assert fakes["read_log"] == ["kilo"]
    assert not any(line.startswith("codex:") for line in out.splitlines())
    records = [_json.loads(line) for line in
               (state_dir / "tool_usage.jsonl").read_text(encoding="utf-8").splitlines()]
    assert {r["tool"] for r in records} == {"kilo"}
    sources = _json.loads((state_dir / "usage_sources.json").read_text(encoding="utf-8"))
    assert set(sources["tools"]) == {"kilo"}
    assert out.splitlines()[-1] == "Imported. Enabled: kilo."


def test_import_usage_default_selects_existing_sources(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch
) -> None:
    fakes = _fake_tools(tmp_path, monkeypatch)
    code = main(["--state-dir", str(tmp_path / "state"), "import-usage"])
    assert code == 0
    assert fakes["read_log"] == ["codex", "kilo"]
    capsys.readouterr()


def test_import_usage_without_sources_exits_1(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch
) -> None:
    fakes = _fake_tools(tmp_path, monkeypatch, with_sources=False)
    state_dir = tmp_path / "state"
    code = main(["--state-dir", str(state_dir), "import-usage"])
    captured = capsys.readouterr()
    assert code == 1
    assert "No tool sources found." in captured.out + captured.err
    assert fakes["read_log"] == []
    assert _state_files(state_dir) == []


def test_tool_importer_reuses_one_cache_across_calls(tmp_path: Path, monkeypatch) -> None:
    from datetime import datetime, timezone

    from agent_control import cli, tool_calls, tool_import
    from agent_control.paths import AppPaths
    from agent_control.storage import StateLocks

    paths = AppPaths.for_test(tmp_path)
    paths.state_dir.mkdir(parents=True)
    source = tmp_path / "cline-source"
    source.mkdir()
    tool_import.write_usage_sources(
        paths.usage_sources,
        {"cline": tool_import.SourceSetting(enabled=True, path=str(source))},
        StateLocks().write,
    )
    seen: list[dict] = []

    def fake_import(*args, **kwargs):
        seen.append(kwargs)
        return "report"

    monkeypatch.setattr(tool_import, "import_tool_usage", fake_import)
    importer = cli._tool_importer_for(
        paths, StateLocks(),
        lambda: datetime(2026, 10, 1, tzinfo=timezone.utc),
        home=tmp_path, env={},
    )
    assert importer is not None
    assert importer() == "report"
    assert importer() == "report"
    assert len(seen) == 2
    store = seen[0]["store"]
    assert isinstance(store, tool_calls.ToolCallCache)
    assert seen[1]["store"] is store
    assert store.path == paths.tool_usage
    assert seen[0]["tools"] == ("cline",)
    assert seen[0]["apply"] is True
from __future__ import annotations

import hashlib
import json
import sqlite3
import sys
import types
from datetime import datetime, timezone
from pathlib import Path

import pytest

from agent_control import projects, tool_import
from agent_control.storage import StateLocks
from agent_control.tool_calls import load_tool_calls

SENTINEL = "SENTINEL-TEXT-DO-NOT-STORE"
NOW = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)


def make_call(key, *, tool="cline", session="s1", root_session=None,
              at="2026-09-30T10:00:00+00:00", cwd=None, sent=10, returned=5,
              cache_read=None, cache_write=None, **extra):
    call = {
        "tool": tool, "key": key, "session": session,
        "root_session": root_session or session, "at": at,
        "model": "gpt-5.6-terra", "cwd": cwd,
        "tokens_sent": sent, "tokens_returned": returned,
        "cache_read": cache_read, "cache_write": cache_write,
        "cache_write_5m": None, "reasoning": None,
        "sent_includes_cache_read": False, "speed": None,
    }
    call.update(extra)
    return call


def fake_reader(calls, checkpoint=None, diagnostics=()):
    seen = []

    def read(source, cp, *, now):
        seen.append({"source": source, "checkpoint": cp, "now": now})
        return [dict(c) for c in calls], dict(checkpoint or {"n": len(calls)}), list(diagnostics)

    read.seen = seen
    return read


def raising_reader(source, cp, *, now):
    raise RuntimeError(SENTINEL)


def run(paths, *, readers, apply, home, tools=None):
    tools = tools or list(readers)
    return tool_import.import_tool_usage(
        paths, StateLocks(), tools=tools,
        sources={t: home / f"src-{t}" for t in tools},
        apply=apply, now=NOW, home=home, readers=readers)


def snapshot(root: Path) -> dict[str, str]:
    if not root.exists():
        return {}
    return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(root.rglob("*")) if p.is_file()}


def all_state_text(root: Path) -> str:
    return "".join(p.read_text(encoding="utf-8", errors="replace")
                   for p in root.rglob("*") if p.is_file())


def make_repo(path: Path) -> Path:
    (path / ".git").mkdir(parents=True)
    return path


def registry(paths):
    return projects.ProjectRegistry(paths.projects, StateLocks(), clock=lambda: NOW)


def lines(path: Path) -> list[str]:
    if not path.exists():
        return []
    return [line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


# 1
def test_dry_run_writes_nothing_and_counts(tmp_path, temp_paths):
    repo = make_repo(tmp_path / "repo")
    registry(temp_paths).discover(tmp_path / "other")
    temp_paths.tool_usage_state.write_text(
        json.dumps({"schema": 1, "tools": {"cline": {"old": 1}}}), encoding="utf-8")
    before = snapshot(temp_paths.state_dir)
    calls = [
        make_call("k1", session="a", at="2026-09-30T10:00:00+00:00", cwd=str(repo / "x"),
                  sent=10, returned=5, cache_read=3),
        make_call("k2", session="a", at="2026-09-30T12:00:00+00:00", cwd=str(repo),
                  sent=None, returned=7, cache_write=2),
        make_call("k3", session="b", at="2026-09-29T08:00:00+00:00", cwd=None,
                  sent=1, returned=1),
    ]
    reader = fake_reader(calls)
    report = run(temp_paths, readers={"cline": reader}, apply=False, home=tmp_path)
    assert snapshot(temp_paths.state_dir) == before
    assert reader.seen[0]["checkpoint"] == {"old": 1}
    assert reader.seen[0]["now"] == NOW
    assert report.applied is False
    (summary,) = report.tools
    assert summary.tool == "cline"
    assert (summary.calls_seen, summary.new_calls, summary.revised_calls) == (3, 3, 0)
    assert summary.sessions == 2
    assert summary.first_at == "2026-09-29T08:00:00+00:00"
    assert summary.last_at == "2026-09-30T12:00:00+00:00"
    assert (summary.tokens_sent, summary.tokens_returned) == (11, 13)
    assert (summary.cache_read, summary.cache_write) == (3, 2)
    assert summary.diagnostics == 0
    assert report.new_project_roots == (projects.canonical_root(repo)[0],)


# 2
def test_apply_writes_and_second_apply_is_idempotent(tmp_path, temp_paths):
    repo = make_repo(tmp_path / "repo")
    calls = [make_call("k1", cwd=str(repo / "sub")), make_call("k2", cwd=str(repo))]
    reader = fake_reader(calls, checkpoint={"pos": 2})
    report = run(temp_paths, readers={"cline": reader}, apply=True, home=tmp_path)
    assert report.applied is True
    assert report.tools[0].new_calls == 2
    stored, _ = load_tool_calls(temp_paths.tool_usage)
    assert set(stored) == {("cline", "k1"), ("cline", "k2")}
    project_ids = {c.project_id for c in stored.values()}
    assert len(project_ids) == 1 and None not in project_ids
    state = json.loads(temp_paths.tool_usage_state.read_text(encoding="utf-8"))
    assert state == {"schema": 1, "tools": {"cline": {"pos": 2}}}
    loaded, _ = projects.load_projects(temp_paths.projects)
    assert [p.state for p in loaded.values()] == ["discovered"]
    assert report.new_project_roots == (next(iter(loaded.values())).root,)
    usage_lines = lines(temp_paths.tool_usage)
    project_lines = lines(temp_paths.projects)

    again = run(temp_paths, readers={"cline": reader}, apply=True, home=tmp_path)
    assert lines(temp_paths.tool_usage) == usage_lines
    assert lines(temp_paths.projects) == project_lines
    assert (again.tools[0].new_calls, again.tools[0].revised_calls) == (0, 0)
    assert again.tools[0].calls_seen == 2
    assert again.new_project_roots == ()
    assert reader.seen[1]["checkpoint"] == {"pos": 2}


# 3
def test_reemitted_call_with_larger_counters_is_revised(tmp_path, temp_paths):
    run(temp_paths, readers={"cline": fake_reader([make_call("k1", sent=10)])},
        apply=True, home=tmp_path)
    report = run(temp_paths, readers={"cline": fake_reader([make_call("k1", sent=25)])},
                 apply=True, home=tmp_path)
    assert (report.tools[0].new_calls, report.tools[0].revised_calls) == (0, 1)
    stored, _ = load_tool_calls(temp_paths.tool_usage)
    assert stored[("cline", "k1")].revision == 2
    assert stored[("cline", "k1")].tokens_sent == 25
    assert len(lines(temp_paths.tool_usage)) == 2


# 4
def test_failing_reader_keeps_checkpoint_and_others_run(tmp_path, temp_paths):
    temp_paths.state_dir.mkdir(parents=True, exist_ok=True)
    temp_paths.tool_usage_state.write_text(
        json.dumps({"schema": 1, "tools": {"codex": {"old": 1}}}), encoding="utf-8")
    report = run(temp_paths, readers={"codex": raising_reader,
                                      "cline": fake_reader([make_call("k1")], {"c": 1})},
                 apply=True, home=tmp_path, tools=["codex", "cline"])
    state = json.loads(temp_paths.tool_usage_state.read_text(encoding="utf-8"))
    assert state["tools"] == {"codex": {"old": 1}, "cline": {"c": 1}}
    failed = [d for d in report.diagnostics if d.code == "tool_reader_failed"]
    assert [d.message for d in failed] == ["codex: RuntimeError"]
    assert SENTINEL not in repr(report)
    assert SENTINEL not in all_state_text(temp_paths.state_dir)
    assert [s.tool for s in report.tools] == ["codex", "cline"]
    assert report.tools[0].diagnostics == 1
    assert report.tools[1].new_calls == 1


# 5
def test_invalid_call_is_diagnosed_and_others_imported(tmp_path, temp_paths):
    calls = [make_call("k1"), make_call("k2", title=SENTINEL), make_call("k3")]
    report = run(temp_paths, readers={"cline": fake_reader(calls)}, apply=True, home=tmp_path)
    invalid = [d for d in report.diagnostics if d.code == "tool_call_invalid"]
    assert len(invalid) == 1
    assert invalid[0].message.startswith("cline: ")
    assert "record" in invalid[0].message
    assert SENTINEL not in repr(report)
    stored, _ = load_tool_calls(temp_paths.tool_usage)
    assert set(stored) == {("cline", "k1"), ("cline", "k3")}
    assert report.tools[0].new_calls == 2
    assert report.tools[0].diagnostics == 1


# 6
def test_nested_folder_resolves_to_repository_root(tmp_path):
    repo = make_repo(tmp_path / "repo")
    nested = repo / "src" / "pkg"
    nested.mkdir(parents=True)
    home = tmp_path / "home"
    assert tool_import.resolve_project_root(str(nested), home=home) == str(repo)
    assert tool_import.resolve_project_root(str(repo), home=home) == str(repo)


# 7
def test_worktree_resolves_to_same_projectas_repository(tmp_path, temp_paths):
    repo = make_repo(tmp_path / "repo")
    worktree = repo / ".claude" / "worktrees" / "x"
    worktree.mkdir(parents=True)
    (worktree / ".git").write_text("gitdir: elsewhere\n", encoding="utf-8")
    (worktree / "sub").mkdir()
    assert tool_import.resolve_project_root(str(worktree / "sub"), home=tmp_path) == str(worktree)
    calls = [make_call("k1", cwd=str(repo)), make_call("k2", cwd=str(worktree / "sub"))]
    report = run(temp_paths, readers={"cline": fake_reader(calls)}, apply=True, home=tmp_path)
    stored, _ = load_tool_calls(temp_paths.tool_usage)
    assert stored[("cline", "k1")].project_id == stored[("cline", "k2")].project_id
    assert stored[("cline", "k1")].project_id is not None
    loaded, _ = projects.load_projects(temp_paths.projects)
    assert len(loaded) == 1
    assert len(report.new_project_roots) == 1


# 8
def test_folder_without_repository_resolves_to_itself(tmp_path):
    folder = tmp_path / "plain" / "dir"
    folder.mkdir(parents=True)
    home = tmp_path / "home"
    assert tool_import.resolve_project_root(str(folder), home=home) == str(folder)


def test_relative_cwd_is_returned_unchanged(tmp_path):
    assert tool_import.resolve_project_root("some/relative", home=tmp_path) == "some/relative"


# 9
def test_home_repo_does_not_capture_subfolders(tmp_path):
    home = make_repo(tmp_path / "home")
    sub = home / "a" / "b"
    sub.mkdir(parents=True)
    assert tool_import.resolve_project_root(str(sub), home=home) == str(sub)
    assert tool_import.resolve_project_root(str(home / "gone"), home=home) == str(home / "gone")


# 10
def test_deleted_subfolder_resolves_to_repository(tmp_path):
    repo = make_repo(tmp_path / "repo")
    gone = repo / "deleted" / "deeper"
    assert not gone.exists()
    assert tool_import.resolve_project_root(str(gone), home=tmp_path / "home") == str(repo)


# 11
def test_approved_project_receives_its_id(tmp_path, temp_paths):
    repo = make_repo(tmp_path / "repo")
    reg = registry(temp_paths)
    project, created = reg.discover(repo)
    assert created
    reg.transition(project.project_id, "approved")
    project_lines = lines(temp_paths.projects)
    calls = [make_call("k1", cwd=str(repo / "src"))]
    dry = run(temp_paths, readers={"cline": fake_reader(calls)}, apply=False, home=tmp_path)
    assert dry.new_project_roots == ()
    report = run(temp_paths, readers={"cline": fake_reader(calls)}, apply=True, home=tmp_path)
    assert report.new_project_roots == ()
    stored, _ = load_tool_calls(temp_paths.tool_usage)
    assert stored[("cline", "k1")].project_id == project.project_id
    assert lines(temp_paths.projects) == project_lines


# 12
def test_dry_run_lists_new_root_once(tmp_path, temp_paths):
    repo = make_repo(tmp_path / "repo")
    (repo / "a").mkdir()
    calls = [make_call("k1", cwd=str(repo)), make_call("k2", cwd=str(repo / "a")),
             make_call("k3", cwd=str(repo / "b"), tool="kilo")]
    readers = {"cline": fake_reader(calls[:2]), "kilo": fake_reader(calls[2:])}
    report = run(temp_paths, readers=readers, apply=False, home=tmp_path)
    assert report.new_project_roots == (projects.canonical_root(repo)[0],)
    assert not temp_paths.projects.exists()
    assert snapshot(temp_paths.state_dir) == {}


# 13
def test_cwd_none_gives_no_project(tmp_path, temp_paths):
    report = run(temp_paths, readers={"cline": fake_reader([make_call("k1", cwd=None)])},
                 apply=True, home=tmp_path)
    stored, _ = load_tool_calls(temp_paths.tool_usage)
    assert stored[("cline", "k1")].project_id is None
    assert report.new_project_roots == ()
    assert not temp_paths.projects.exists()


def test_relative_cwd_is_not_attributed(tmp_path, temp_paths):
    calls = [make_call("k1", cwd="relative/dir")]
    report = run(temp_paths, readers={"cline": fake_reader(calls)}, apply=True, home=tmp_path)
    stored, _ = load_tool_calls(temp_paths.tool_usage)
    assert stored[("cline", "k1")].project_id is None
    assert stored[("cline", "k1")].cwd == "relative/dir"
    assert report.new_project_roots == ()
    assert not temp_paths.projects.exists()


# 14
def test_usage_sources_round_trip(tmp_path, temp_paths):
    path = temp_paths.usage_sources
    assert tool_import.load_usage_sources(path) == ({}, [])
    path.parent.mkdir(parents=True, exist_ok=True)
    for bad in ("{not json " + SENTINEL, json.dumps([SENTINEL]),
                json.dumps({"schema": 1, "tools": {"cline": {"enabled": "yes", "path": None}}}),
                json.dumps({"schema": 2, "tools": {}}),
                json.dumps({"schema": 1, "tools": {"nope": {"enabled": True, "path": None}}})):
        path.write_text(bad, encoding="utf-8")
        settings, diags = tool_import.load_usage_sources(path)
        assert settings == {}
        assert [d.code for d in diags] == ["usage_sources_invalid"]
        assert SENTINEL not in repr(diags)
    override = tmp_path / "fork-storage"
    written = {"cline": tool_import.SourceSetting(enabled=True, path=str(override)),
               "kilo": tool_import.SourceSetting(enabled=False, path=None)}
    tool_import.write_usage_sources(path, written, StateLocks().write)
    raw = json.loads(path.read_text(encoding="utf-8"))
    assert raw["schema"] == 1
    settings, diags = tool_import.load_usage_sources(path)
    assert diags == []
    assert settings == written
    assert "codex" not in settings
    home = tmp_path / "home"
    env = {"APPDATA": str(tmp_path / "appdata")}
    assert tool_import.resolve_source("cline", settings["cline"], home=home, env=env) == override
    assert tool_import.resolve_source("kilo", settings["kilo"], home=home, env=env) == (
        home / ".local" / "share" / "kilo" / "kilo.db")
    assert tool_import.resolve_source("codex", None, home=home, env={}) == home / ".codex"


# 15
def test_default_source_table(tmp_path):
    home = tmp_path / "home"
    appdata = tmp_path / "appdata"
    env = {"APPDATA": str(appdata)}
    assert tool_import.default_source("claude_code", home=home, env=env) == (
        home / ".claude" / "projects")
    assert tool_import.default_source("codex", home=home, env=env) == home / ".codex"
    codex_home = tmp_path / "codex-home"
    assert tool_import.default_source(
        "codex", home=home, env={"CODEX_HOME": str(codex_home)}) == codex_home
    assert tool_import.default_source("cline", home=home, env=env) == (
        appdata / "Code" / "User" / "globalStorage" / "saoudrizwan.claude-dev")
    assert tool_import.default_source("cline", home=home, env={}) is None
    assert tool_import.default_source("kilo", home=home, env=env) == (
        home / ".local" / "share" / "kilo" / "kilo.db")


def _write_cline_source(source: Path, cwd: Path) -> None:
    task = "1758000000001"
    tdir = source / "tasks" / task
    tdir.mkdir(parents=True)
    ts = 1758000000000
    payload = {"request": SENTINEL, "tokensIn": 100, "tokensOut": 20,
               "cacheWrites": 5, "cacheReads": 70, "cost": 0.01}
    entries = [
        {"ts": ts - 5, "type": "say", "say": "text", "text": SENTINEL},
        {"ts": ts, "type": "say", "say": "api_req_started", "text": json.dumps(payload),
         "modelInfo": {"providerId": "openai", "modelId": "gpt-5.6-terra", "mode": "act"}},
    ]
    (tdir / "ui_messages.json").write_text(json.dumps(entries), encoding="utf-8")
    (tdir / "api_conversation_history.json").write_text(
        json.dumps([{"role": "user", "content": [{"type": "text", "text": SENTINEL}]}]),
        encoding="utf-8")
    (source / "state").mkdir()
    (source / "state" / "taskHistory.json").write_text(json.dumps([
        {"id": task, "ts": ts, "task": SENTINEL, "tokensIn": 1, "tokensOut": 1,
         "cacheWrites": 0, "cacheReads": 0, "totalCost": 0.0, "modelId": "gpt-5.6-terra",
         "cwdOnTaskInitialization": str(cwd)}]), encoding="utf-8")


def _write_kilo_source(db: Path, cwd: Path) -> None:
    con = sqlite3.connect(db)
    con.execute("create table session (id text primary key, parent_id text,"
                " directory text, title text)")
    con.execute("create table message (id text primary key, session_id text,"
                " time_created integer, time_updated integer, data text)")
    con.execute("create table part (id text primary key, message_id text, data text)")
    con.execute("insert into session values (?, ?, ?, ?)", ("ses_a", None, str(cwd), SENTINEL))
    data = {"role": "assistant", "modelID": "claude-opus-4-8", "providerID": "openwebui",
            "tokens": {"input": 1200, "output": 300, "reasoning": 50,
                       "cache": {"read": 900, "write": 7}},
            "path": {"cwd": str(cwd), "root": str(cwd)},
            "time": {"created": 1759300000000, "completed": 1759300004000},
            "summary": {"title": SENTINEL}, "text": SENTINEL}
    con.execute("insert into message values (?, ?, ?, ?, ?)",
                ("msg_1", "ses_a", 1759300000000, 1759300004500, json.dumps(data)))
    con.execute("insert into part values (?, ?, ?)",
                ("prt_1", "msg_1", json.dumps({"type": "text", "text": SENTINEL})))
    con.commit()
    con.close()


# 16
def test_end_to_end_with_real_cline_and_kilo_readers(tmp_path, temp_paths):
    repo = make_repo(tmp_path / "repo")
    (repo / "kilo-sub").mkdir()
    cline_src = tmp_path / "cline-src"
    _write_cline_source(cline_src, repo)
    kilo_db = tmp_path / "kilo.db"
    _write_kilo_source(kilo_db, repo / "kilo-sub")
    report = tool_import.import_tool_usage(
        temp_paths, StateLocks(), tools=["cline", "kilo"],
        sources={"cline": cline_src, "kilo": kilo_db},
        apply=True, now=NOW, home=tmp_path)
    assert [(s.tool, s.new_calls) for s in report.tools] == [("cline", 1), ("kilo", 1)]
    assert report.diagnostics == ()
    stored, _ = load_tool_calls(temp_paths.tool_usage)
    assert {key[0] for key in stored} == {"cline", "kilo"}
    assert len({c.project_id for c in stored.values()}) == 1
    state = json.loads(temp_paths.tool_usage_state.read_text(encoding="utf-8"))
    assert set(state["tools"]) == {"cline", "kilo"}
    assert SENTINEL not in all_state_text(temp_paths.state_dir)
    assert SENTINEL not in repr(report)


# 17
def test_guard_fails_reader_called_outside_tmp_path(tmp_path, monkeypatch):
    calls = []
    module = types.ModuleType("agent_control_test_fake_reader")

    def read(source, checkpoint, *, now):
        calls.append(source)
        return [], {}, []

    module.read = read
    monkeypatch.setitem(sys.modules, module.__name__, module)
    monkeypatch.setitem(tool_import.READER_MODULES, "cline", module.__name__)
    guarded = tool_import._load_reader("cline")
    assert guarded(tmp_path / "inside", None, now=NOW) == ([], {}, [])
    with pytest.raises(pytest.fail.Exception):
        guarded(tmp_path.parent / "outside-tmp-path", None, now=NOW)
    assert calls == [tmp_path / "inside"]


def _forbid_full_load(monkeypatch):
    from agent_control import tool_calls

    def refuse(path):
        raise AssertionError("tool_usage.jsonl must not be loaded whole")

    monkeypatch.setattr(tool_calls, "load_tool_calls", refuse)


def _run_with(paths, *, readers, apply, home, store=None):
    tools = list(readers)
    return tool_import.import_tool_usage(
        paths, StateLocks(), tools=tools,
        sources={t: home / f"src-{t}" for t in tools},
        apply=apply, now=NOW, home=home, readers=readers, store=store)


# 18
def test_import_with_no_calls_never_loads_the_store(tmp_path, temp_paths, monkeypatch):
    run(temp_paths, readers={"cline": fake_reader([make_call("k1")])},
        apply=True, home=tmp_path)
    usage_before = lines(temp_paths.tool_usage)
    _forbid_full_load(monkeypatch)
    readers = {"cline": fake_reader([], checkpoint={"pos": 9}),
               "codex": fake_reader([], checkpoint={"off": 3})}
    report = run(temp_paths, readers=readers, apply=True, home=tmp_path)
    assert [(s.tool, s.calls_seen, s.new_calls) for s in report.tools] == [
        ("cline", 0, 0), ("codex", 0, 0)]
    state = json.loads(temp_paths.tool_usage_state.read_text(encoding="utf-8"))
    assert state == {"schema": 1, "tools": {"cline": {"pos": 9}, "codex": {"off": 3}}}
    assert lines(temp_paths.tool_usage) == usage_before


# 19
def test_store_gives_same_report_and_writes(tmp_path, monkeypatch):
    from agent_control import tool_calls
    from agent_control.paths import AppPaths

    repo = make_repo(tmp_path / "repo")
    first = [make_call("k1", cwd=str(repo), sent=10), make_call("k2", sent=4)]
    second = [make_call("k1", cwd=str(repo), sent=30), make_call("k2", sent=4),
              make_call("k3", tool="codex", cwd=str(repo / "sub"))]
    plain = AppPaths.for_test(tmp_path / "plain")
    cached = AppPaths.for_test(tmp_path / "cached")
    for paths in (plain, cached):
        run(paths, readers={"cline": fake_reader(first)}, apply=True, home=tmp_path)

    def readers():
        return {"cline": fake_reader(second[:2], checkpoint={"pos": 2}),
                "codex": fake_reader(second[2:], checkpoint={"pos": 1})}

    expected = _run_with(plain, readers=readers(), apply=True, home=tmp_path)

    store = tool_calls.ToolCallCache(cached.tool_usage)
    loads = []
    real_load = store.load

    def counted_load():
        loads.append(1)
        return real_load()

    store.load = counted_load
    _forbid_full_load(monkeypatch)
    report = _run_with(cached, readers=readers(), apply=True, home=tmp_path,
                       store=store)

    assert report == expected
    assert (report.tools[0].new_calls, report.tools[0].revised_calls) == (0, 1)
    assert report.tools[1].new_calls == 1
    assert loads == [1]

    def records(paths):
        # Each state dir's registry mints its own project UUIDs; compare roots.
        roots = {p.project_id: p.root
                 for p in projects.load_projects(paths.projects)[0].values()}
        out = []
        for line in lines(paths.tool_usage):
            record = json.loads(line)
            record["project_id"] = roots.get(record["project_id"])
            out.append(record)
        return out

    assert records(cached) == records(plain)
    assert len(records(cached)) == 4
    assert (cached.tool_usage_state.read_text(encoding="utf-8")
            == plain.tool_usage_state.read_text(encoding="utf-8"))


# 20
def test_dry_run_with_store_leaves_cached_map_unchanged(tmp_path, temp_paths):
    from agent_control import tool_calls

    run(temp_paths, readers={"cline": fake_reader([make_call("k1", sent=10)])},
        apply=True, home=tmp_path)
    store = tool_calls.ToolCallCache(temp_paths.tool_usage)
    cached_before, _ = store.load()
    internal_before = dict(store._calls)
    before = snapshot(temp_paths.state_dir)
    calls = [make_call("k1", sent=50), make_call("k2")]
    report = _run_with(temp_paths, readers={"cline": fake_reader(calls)},
                       apply=False, home=tmp_path, store=store)
    assert (report.tools[0].new_calls, report.tools[0].revised_calls) == (1, 1)
    assert store._calls == internal_before
    assert store.load()[0] == cached_before
    assert set(cached_before) == {("cline", "k1")}
    assert snapshot(temp_paths.state_dir) == before

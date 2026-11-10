from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path

import pytest

ENGINE_PATH = Path(__file__).resolve().parents[1] / "engine" / "delegate.py"


def _load_engine():
    spec = importlib.util.spec_from_file_location("agentcontrol_engine", ENGINE_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def engine():
    return _load_engine()


def _read_events(index: Path) -> list[dict]:
    return [
        json.loads(line)
        for line in index.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _begin(lc):
    lc.begin(harness_version=6, label="x", effort="high", tool_mode="prompt",
             root="R", cwd="R", transcript="t.md", messages="t.messages.jsonl",
             usage_journal="t.usage.jsonl", write_mode="staged")


def test_repo_run_index_path_sits_beside_transcript(engine, tmp_path: Path) -> None:
    transcript = tmp_path / "sub" / "20260924-171330-runtime.md"
    got = engine.repo_run_index_path(str(transcript))
    assert got == os.path.join(
        os.path.dirname(os.path.abspath(str(transcript))),
        engine.REPO_RUN_INDEX_NAME,
    )
    assert os.path.basename(got) == "agent-runs-index.jsonl"


def test_lifecycle_writes_identical_records_to_both_indexes(engine, tmp_path: Path) -> None:
    machine = tmp_path / "machine.jsonl"
    repo = tmp_path / "repo.jsonl"
    lc = engine.RunLifecycle("run-1", str(machine), "m", repo_index_path=str(repo))
    _begin(lc)
    lc.finish("ok", tool_calls=5, refused=0, sent=100, recv=20)
    assert machine.read_bytes() == repo.read_bytes()
    lines = machine.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2


def test_machine_index_unwritable_still_records_in_repo_index(
        engine, tmp_path: Path, capsys) -> None:
    machine = tmp_path / "machine_dir"
    machine.mkdir()  # a directory cannot be opened for append -> OSError
    repo = tmp_path / "repo.jsonl"
    lc = engine.RunLifecycle("run-2", str(machine), "m", repo_index_path=str(repo))
    _begin(lc)
    lc.finish("ok", tool_calls=1, refused=0, sent=10, recv=2)
    events = _read_events(repo)
    assert [e["event"] for e in events] == ["start", "end"]
    err = capsys.readouterr().err
    assert "WARNING: run index append failed" in err
    assert "only in the repo run index" in err
    assert "the machine-wide index was not writable" in err


def test_repo_index_unwritable_still_records_in_machine_index(
        engine, tmp_path: Path) -> None:
    machine = tmp_path / "machine.jsonl"
    repo = tmp_path / "repo_dir"
    repo.mkdir()  # directory -> OSError on append, but must not raise
    lc = engine.RunLifecycle("run-3", str(machine), "m", repo_index_path=str(repo))
    _begin(lc)
    lc.finish("ok", tool_calls=1, refused=0, sent=10, recv=2)
    events = _read_events(machine)
    assert [e["event"] for e in events] == ["start", "end"]


def test_same_path_is_written_once(engine, tmp_path: Path) -> None:
    shared = tmp_path / "shared.jsonl"
    lc = engine.RunLifecycle("run-4", str(shared), "m", repo_index_path=str(shared))
    _begin(lc)
    lc.finish("ok", tool_calls=1, refused=0, sent=10, recv=2)
    lines = shared.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2


def test_safety_end_goes_to_both_indexes(engine, tmp_path: Path) -> None:
    machine = tmp_path / "machine.jsonl"
    repo = tmp_path / "repo.jsonl"
    lc = engine.RunLifecycle("run-5", str(machine), "m", repo_index_path=str(repo))
    _begin(lc)
    lc._safety_end()
    for index in (machine, repo):
        events = _read_events(index)
        assert events[-1]["event"] == "end"
        assert events[-1]["status"] == "interrupted"


def test_repo_index_lines_parse_for_agentcontrol(engine, tmp_path: Path) -> None:
    from agent_control.run_index import EndEvent, StartEvent, parse_index_record

    machine = tmp_path / "machine.jsonl"
    repo = tmp_path / "repo.jsonl"
    lc = engine.RunLifecycle("run-6", str(machine), "m", repo_index_path=str(repo))
    lc.begin(harness_version=6, label="x", effort="high", tool_mode="prompt",
             root=r"D:\repos\Projectb", cwd=r"D:\repos\Projectb",
             transcript=r"D:\repos\Projectb\.claude\agent-runs\a.md",
             messages=r"D:\repos\Projectb\.claude\agent-runs\a.messages.jsonl",
             usage_journal=r"D:\repos\Projectb\.claude\agent-runs\a.usage.jsonl",
             write_mode="staged")
    lc.finish("interrupted", tool_calls=2, refused=0, sent=5, recv=1)
    events = _read_events(repo)
    parsed = [parse_index_record(e, i) for i, e in enumerate(events, 1)]
    assert isinstance(parsed[0], StartEvent)
    assert isinstance(parsed[1], EndEvent)

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import sys
from pathlib import Path

import pytest

ENGINE_PATH = Path(__file__).resolve().parents[1] / "engine" / "delegate.py"
INSTALLED_ENGINE = Path(r"C:\Users\operator\.claude\bin\delegate.py")
_INSTALLED_AT_IMPORT = (
    hashlib.sha256(INSTALLED_ENGINE.read_bytes()).hexdigest()
    if INSTALLED_ENGINE.is_file()
    else None
)


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


def test_harness_and_schema_versions(engine) -> None:
    assert engine.HARNESS_VERSION == 6
    assert engine.INDEX_SCHEMA == 2


def test_start_record_has_process_identity_and_schema(engine) -> None:
    record = engine.index_record(
        "start",
        run_id="r",
        harness_version=6,
        label="demo",
        model="model",
        effort=None,
        tool_mode=None,
        root=r"D:\repos\Projectb",
        cwd=r"D:\repos\Projectb\.worktrees\wt1",
        transcript=r"D:\repos\Projectb\.claude\agent-runs\a.md",
        messages=r"D:\repos\Projectb\.claude\agent-runs\a.messages.jsonl",
        usage_journal=r"D:\repos\Projectb\.claude\agent-runs\a.usage.jsonl",
        write_mode="stage",
    )
    assert record["schema"] == 2
    assert record["run_id"] == "r"
    assert record["root"] == r"D:\repos\Projectb"
    assert record["cwd"] == r"D:\repos\Projectb\.worktrees\wt1"
    assert record["engine_pid"] == os.getpid()


def test_end_record_uses_terminal_status(engine) -> None:
    record = engine.index_record(
        "end", run_id="r", tool_calls=3, refused=0, sent=10, recv=2, status="complete"
    )
    assert record["schema"] == 2
    assert record["status"] == "complete"


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("ok", "complete"),
        ("complete", "complete"),
        ("failed", "failed"),
        ("interrupted", "interrupted"),
        ("no_report", "failed"),
    ],
)
def test_normalize_end_status(engine, raw: str, expected: str) -> None:
    assert engine.normalize_end_status(raw) == expected


def _read_events(index: Path) -> list[dict]:
    return [
        json.loads(line)
        for line in index.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def test_lifecycle_finish_writes_exactly_one_end(engine, tmp_path: Path) -> None:
    index = tmp_path / "index.jsonl"
    lc = engine.RunLifecycle("run-1", str(index), "gpt-5.6-sol")
    lc.begin(harness_version=6, label="x", effort="high", tool_mode="prompt",
             root="R", cwd="R", transcript="t.md", messages="t.messages.jsonl",
             usage_journal="t.usage.jsonl", write_mode="staged")
    lc.finish("complete", tool_calls=5, refused=0, sent=100, recv=20)
    lc.finish("failed")  # second call must be a no-op
    events = _read_events(index)
    starts = [e for e in events if e["event"] == "start"]
    ends = [e for e in events if e["event"] == "end"]
    assert len(starts) == 1
    assert len(ends) == 1
    assert ends[0]["status"] == "complete"
    assert ends[0]["tool_calls"] == 5


def test_lifecycle_safety_end_marks_interrupted(engine, tmp_path: Path) -> None:
    # Simulate the process dying before finish(): the atexit hook must still
    # write a terminal 'interrupted' end record so the run never lingers open.
    index = tmp_path / "index.jsonl"
    lc = engine.RunLifecycle("run-2", str(index), "gpt-5.6-sol")
    lc.begin(harness_version=6, label="x", effort="high", tool_mode="prompt",
             root="R", cwd="R", transcript="t.md", messages="t.messages.jsonl",
             usage_journal="t.usage.jsonl", write_mode="staged")
    lc.update(tool_calls=3, sent=50, recv=8)
    lc._safety_end()  # what atexit/signal would invoke
    events = _read_events(index)
    ends = [e for e in events if e["event"] == "end"]
    assert len(ends) == 1
    assert ends[0]["status"] == "interrupted"
    assert ends[0]["tool_calls"] == 3
    assert ends[0]["sent"] == 50


def test_lifecycle_safety_end_is_noop_after_finish(engine, tmp_path: Path) -> None:
    index = tmp_path / "index.jsonl"
    lc = engine.RunLifecycle("run-3", str(index), "gpt-5.6-sol")
    lc.begin(harness_version=6, label="x", effort="high", tool_mode="prompt",
             root="R", cwd="R", transcript="t.md", messages="t.messages.jsonl",
             usage_journal="t.usage.jsonl", write_mode="staged")
    lc.finish("complete", tool_calls=1, refused=0, sent=10, recv=2)
    lc._safety_end()  # must not append a second end
    ends = [e for e in _read_events(index) if e["event"] == "end"]
    assert len(ends) == 1
    assert ends[0]["status"] == "complete"


def test_lifecycle_end_is_agentcontrol_compatible(engine, tmp_path: Path) -> None:
    from agent_control.run_index import EndEvent, StartEvent, parse_index_record

    index = tmp_path / "index.jsonl"
    lc = engine.RunLifecycle("run-4", str(index), "gpt-5.6-sol")
    lc.begin(harness_version=6, label="x", effort="high", tool_mode="prompt",
             root=r"D:\repos\Projectb", cwd=r"D:\repos\Projectb",
             transcript=r"D:\repos\Projectb\.claude\agent-runs\a.md",
             messages=r"D:\repos\Projectb\.claude\agent-runs\a.messages.jsonl",
             usage_journal=r"D:\repos\Projectb\.claude\agent-runs\a.usage.jsonl",
             write_mode="staged")
    lc.finish("interrupted", tool_calls=2, refused=0, sent=5, recv=1)
    events = _read_events(index)
    parsed = [parse_index_record(e, i) for i, e in enumerate(events, 1)]
    assert isinstance(parsed[0], StartEvent)
    assert isinstance(parsed[1], EndEvent)
    assert parsed[1].status == "interrupted"


def test_append_run_event_is_durable(engine, tmp_path: Path) -> None:
    index = tmp_path / "index.jsonl"
    ok = engine.append_run_event(str(index), {"run_id": "r", "event": "start"})
    assert ok is True
    lines = index.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0])["run_id"] == "r"


def test_append_run_event_nonblocking_on_error(engine, tmp_path: Path, capsys) -> None:
    # A directory path cannot be opened for append; the engine must warn, not raise.
    directory = tmp_path / "dir"
    directory.mkdir()
    ok = engine.append_run_event(str(directory), {"run_id": "r"})
    assert ok is False
    assert "run index append failed" in capsys.readouterr().err


def test_engine_start_records_are_valid_for_agentcontrol(engine, tmp_path: Path) -> None:
    from agent_control.run_index import StartEvent, parse_index_record

    record = engine.index_record(
        "start",
        run_id="r",
        harness_version=6,
        label="demo",
        model="model",
        effort=None,
        tool_mode=None,
        root=r"D:\repos\Projectb",
        cwd=r"D:\repos\Projectb",
        transcript=r"D:\repos\Projectb\.claude\agent-runs\a.md",
        messages=r"D:\repos\Projectb\.claude\agent-runs\a.messages.jsonl",
        usage_journal=r"D:\repos\Projectb\.claude\agent-runs\a.usage.jsonl",
        write_mode="stage",
    )
    record["ts"] = "2026-09-17T12:00:00+00:00"
    parsed = parse_index_record(record, 1)
    assert isinstance(parsed, StartEvent)
    assert parsed.engine_pid == os.getpid()
    assert parsed.schema == 2


def test_sanitize_usage_drops_forbidden_keys(engine) -> None:
    raw = {
        "prompt_tokens": 100,
        "completion_tokens": 20,
        "prompt_tokens_details": {"cached_tokens": 60, "secret": "leak"},
        "content": "do not store this",
        "assistant": "nor this",
        "tool_result": "nor this",
    }
    cleaned = engine.sanitize_usage(raw)
    assert cleaned["prompt_tokens"] == 100
    assert cleaned["prompt_tokens_details"] == {"cached_tokens": 60}
    assert "content" not in cleaned
    assert "assistant" not in cleaned
    assert "tool_result" not in cleaned


def test_sanitize_usage_rejects_non_usage(engine) -> None:
    assert engine.sanitize_usage(None) is None
    assert engine.sanitize_usage({"content": "x"}) is None


def test_usage_record_only_contains_sanitized_raw(engine) -> None:
    record = engine.usage_record(
        1,
        "loop",
        "model",
        {"usage": {"input_tokens": 10, "output_tokens": 2, "content": "leak"}},
        ts="2026-09-17T12:00:00+00:00",
    )
    assert record["raw"] == {"input_tokens": 10, "output_tokens": 2}
    assert "leak" not in json.dumps(record)


def test_append_usage_record_is_exactly_one_row(engine, tmp_path: Path) -> None:
    journal = tmp_path / "a.usage.jsonl"
    ok = engine.append_usage_record(
        str(journal),
        iteration=1,
        phase="loop",
        model="model",
        timestamp="2026-09-17T12:00:00+00:00",
        raw_usage={"input_tokens": 10, "output_tokens": 2},
    )
    assert ok is True
    lines = journal.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    row = json.loads(lines[0])
    assert row["phase"] == "loop"
    assert row["sent"] == 10


def test_engine_usage_row_is_agentcontrol_compatible(engine, tmp_path: Path) -> None:
    from agent_control.usage import read_usage_journal

    journal = tmp_path / "a.usage.jsonl"
    engine.append_usage_record(
        str(journal),
        iteration=1,
        phase="loop",
        model="model",
        timestamp="2026-09-17T12:00:00+00:00",
        raw_usage={"input_tokens": 10, "output_tokens": 2, "cache_read_input_tokens": 5},
    )
    evidence = read_usage_journal(journal)
    assert evidence.source == "usage_journal"
    assert evidence.totals.tokens_sent == 10
    assert evidence.totals.cache_read == 5


def test_engine_source_has_no_agentcontrol_http(engine) -> None:
    text = ENGINE_PATH.read_text(encoding="utf-8").casefold()
    assert "127.0.0.1:8787" not in text
    assert "import agent_control" not in text
    assert "from agent_control" not in text


@pytest.mark.parametrize(
    "bad",
    [
        ["python", "-m", "pytest", "/tmp/evil_test.py"],
        ["python", "-m", "pytest", "\\tmp\\evil_test.py"],
        ["python", "-m", "pytest", "\\\\server\\share\\evil_test.py"],
        ["python", "-m", "pytest", "C:/x/evil_test.py"],
        ["python", "-m", "pytest", "tests/../../evil_test.py"],
    ],
)
def test_run_command_refuses_escaping_path_args(
    engine, tmp_path: Path, monkeypatch, bad
) -> None:
    calls = []

    def stub_run(*args, **kwargs):
        calls.append((args, kwargs))
        raise AssertionError("subprocess.run must not be reached for a refused arg")

    monkeypatch.setattr(engine, "ALLOWED_CMDS", [["python", "-m", "pytest"]])
    monkeypatch.setattr(engine.subprocess, "run", stub_run)
    runner = engine.Runner(str(tmp_path), write_mode="stage")
    result = runner.run_command(bad)
    assert isinstance(result, str)
    assert result.startswith("refused:")
    assert calls == []


def test_run_command_allows_repo_relative_path_arg(
    engine, tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(engine, "ALLOWED_CMDS", [["python", "-m", "pytest"]])

    class _Completed:
        returncode = 0
        stdout = ""
        stderr = ""

    def stub_run(*args, **kwargs):
        return _Completed()

    monkeypatch.setattr(engine.subprocess, "run", stub_run)
    runner = engine.Runner(str(tmp_path), write_mode="stage")
    result = runner.run_command(["python", "-m", "pytest", "tests/test_x.py"])
    assert not result.startswith("refused:")


def test_start_record_carries_max_iters_when_given(engine) -> None:
    with_iters = engine.index_record(
        "start", run_id="r", harness_version=6, max_iters=40
    )
    assert with_iters["max_iters"] == 40
    without = engine.index_record("start", run_id="r", harness_version=6)
    assert without["max_iters"] is None


def test_start_record_with_max_iters_parses_for_agentcontrol(engine) -> None:
    from agent_control.run_index import StartEvent, parse_index_record

    record = engine.index_record(
        "start",
        run_id="r",
        harness_version=6,
        label="demo",
        model="model",
        effort=None,
        tool_mode=None,
        root=r"D:\repos\Projectb",
        cwd=r"D:\repos\Projectb",
        transcript=r"D:\repos\Projectb\.claude\agent-runs\a.md",
        messages=r"D:\repos\Projectb\.claude\agent-runs\a.messages.jsonl",
        usage_journal=r"D:\repos\Projectb\.claude\agent-runs\a.usage.jsonl",
        write_mode="stage",
        max_iters=40,
    )
    record["ts"] = "2026-09-17T12:00:00+00:00"
    parsed = parse_index_record(record, 1)
    assert isinstance(parsed, StartEvent)
    assert parsed.schema == 2


def test_append_usage_record_started_is_optional(engine, tmp_path: Path) -> None:
    journal = tmp_path / "a.usage.jsonl"
    engine.append_usage_record(
        str(journal),
        iteration=1,
        phase="loop",
        model="model",
        timestamp="2026-09-17T12:00:00+00:00",
        raw_usage={"input_tokens": 10, "output_tokens": 2},
        started="2026-09-24T10:00:00Z",
    )
    engine.append_usage_record(
        str(journal),
        iteration=2,
        phase="loop",
        model="model",
        timestamp="2026-09-17T12:00:01+00:00",
        raw_usage={"input_tokens": 10, "output_tokens": 2},
    )
    rows = [json.loads(line) for line in journal.read_text(encoding="utf-8").splitlines()]
    assert rows[0]["started"] == "2026-09-24T10:00:00Z"
    assert "started" not in rows[1]


def test_usage_journal_with_started_parses_for_agentcontrol(
    engine, tmp_path: Path
) -> None:
    from agent_control.activity import iteration_metrics

    journal = tmp_path / "a.usage.jsonl"
    engine.append_usage_record(
        str(journal),
        iteration=1,
        phase="loop",
        model="model",
        timestamp="2026-09-17T12:00:00+00:00",
        raw_usage={"input_tokens": 10, "output_tokens": 2},
        started="2026-09-17T11:59:59Z",
    )
    metrics = iteration_metrics(journal)
    assert 1 in metrics
    assert metrics[1]["tokens_sent"] == 10


def test_contract_suite_never_writes_installed_engine() -> None:
    # After an approved deploy the checkout and installed copies are identical,
    # so the old "they differ" assertion is obsolete. What must still hold is
    # that running this contract suite never mutates the installed engine.
    if _INSTALLED_AT_IMPORT is None:
        pytest.skip("installed engine not present")
    current = hashlib.sha256(INSTALLED_ENGINE.read_bytes()).hexdigest()
    assert current == _INSTALLED_AT_IMPORT

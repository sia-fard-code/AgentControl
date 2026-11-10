import importlib.util, json, sys
from pathlib import Path
import pytest
import threading

ENGINE_DIR = Path(__file__).resolve().parents[1] / "engine"


@pytest.fixture
def eng():
    sys.path.insert(0, str(ENGINE_DIR))
    spec = importlib.util.spec_from_file_location("delegate_boundaries", ENGINE_DIR / "delegate.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    yield mod
    sys.path.remove(str(ENGINE_DIR))


def _ends(path):
    if not path.exists():
        return []
    return [r for r in map(json.loads, path.read_text().splitlines()) if r.get("event") == "end"]


class BoomDiag:
    def emit(self, event, **fields):
        raise RuntimeError("diag broken")


def test_finish_retries_only_missing_index_path(eng, tmp_path, monkeypatch):
    machine, repo = tmp_path / "machine.jsonl", tmp_path / "repo.jsonl"
    lc = eng.RunLifecycle("r1", str(machine), "m", repo_index_path=str(repo))
    lc.diagnostics = None
    real = eng.append_run_event
    fail = {"on": True}
    def flaky(path, rec):
        if fail["on"] and Path(path) == machine:
            return False
        return real(path, rec)
    monkeypatch.setattr(eng, "append_run_event", flaky)
    lc.finish("complete", tool_calls=3)
    assert len(_ends(repo)) == 1 and _ends(machine) == []
    assert lc._ended is False
    fail["on"] = False
    lc.finish("interrupted")  # later safe retry keeps the original payload
    assert [r["status"] for r in _ends(machine)] == [_ends(repo)[0]["status"]]
    assert len(_ends(repo)) == 1 and _ends(machine)[0]["tool_calls"] == 3
    assert lc._ended is True
    lc.finish("complete")
    assert len(_ends(machine)) == 1 and len(_ends(repo)) == 1


def test_broken_diagnostics_do_not_block_terminal(eng, tmp_path):
    idx = tmp_path / "i.jsonl"
    lc = eng.RunLifecycle("r2", str(idx), "m")
    lc.diagnostics = BoomDiag()
    lc.finish("complete")
    assert len(_ends(idx)) == 1 and lc._ended is True


class RecordingDiag:
    def __init__(self):
        self.rows = []

    def emit(self, event, **fields):
        self.rows.append({"event": event, **fields})
        return True


def test_retry_counts_describe_all_distinct_indexes(eng, tmp_path, monkeypatch):
    machine, repo = tmp_path / "machine.jsonl", tmp_path / "repo.jsonl"
    lifecycle = eng.RunLifecycle("r", str(machine), "m", repo_index_path=str(repo))
    diary = lifecycle.diagnostics = RecordingDiag()
    original = eng.append_run_event
    failures = {str(machine)}
    monkeypatch.setattr(eng, "append_run_event", lambda path, row:
                        False if str(path) in failures else original(path, row))
    lifecycle.finish("failed")
    failures.clear()
    lifecycle.finish("interrupted")
    persisted = [row for row in diary.rows if row["event"] == "terminal_persisted"][-1]
    assert persisted["indexes_attempted"] == persisted["indexes_written"] == 2


def test_intermediate_terminal_counts_do_not_confirm_unattempted_indexes(eng, tmp_path):
    lifecycle = eng.RunLifecycle("r", str(tmp_path / "machine.jsonl"), "m",
                                repo_index_path=str(tmp_path / "repo.jsonl"))
    diary = lifecycle.diagnostics = RecordingDiag()
    lifecycle.finish("complete")
    results = [row for row in diary.rows if row["event"] == "index_append_result"]
    assert [(row["indexes_attempted"], row["indexes_written"]) for row in results] == [(1, 1), (2, 2)]
    assert all(row["indexes_expected"] == 2 for row in results)
    assert [row["outcome"] for row in results] == ["ok", "ok"]
    assert eng.capability_module("harness_diagnostics").clean({"indexes_expected": 2}) == {"indexes_expected": 2}


def test_ambiguous_append_acknowledgement_does_not_duplicate_a_terminal(eng, tmp_path, monkeypatch):
    index = tmp_path / "index.jsonl"
    lifecycle = eng.RunLifecycle("r", str(index), "m")
    lifecycle.diagnostics = None
    original = eng.append_run_event
    calls = []
    def ambiguous(path, row):
        calls.append(1)
        result = original(path, row)
        if len(calls) == 1:
            raise OSError("synthetic lost acknowledgement")
        return result
    monkeypatch.setattr(eng, "append_run_event", ambiguous)
    lifecycle.finish("complete")
    assert len(_ends(index)) == 1 and not lifecycle._ended
    lifecycle.finish("interrupted")
    assert len(_ends(index)) == 1 and lifecycle._ended


def test_tool_ordinals_reset_per_turn_without_changing_tool_counts(eng, tmp_path, monkeypatch):
    diary = RecordingDiag()
    monkeypatch.setattr(eng, "DIAGNOSTICS", diary)
    run = eng.Runner(str(tmp_path))
    run.calls = 37
    run.current_turn = 2
    run.dispatch("list_dir", {})
    run.current_turn = 3
    run.dispatch("list_dir", {})
    starts = [row for row in diary.rows if row["event"] == "tool_start"]
    assert [(row["turn"], row["call_index"]) for row in starts] == [(2, 1), (3, 1)]
    assert run.calls == 39


def test_caught_tool_exception_gets_safe_evidence_but_remains_a_result(eng, tmp_path, monkeypatch):
    diary = RecordingDiag()
    monkeypatch.setattr(eng, "DIAGNOSTICS", diary)
    result = eng.Runner(str(tmp_path)).dispatch("read_file", {"path": "SYNTHETIC_PRIVATE"})
    assert result.startswith("error:")
    errors = [row for row in diary.rows if row["event"] == "tool_error"]
    assert errors and errors[-1]["exception_class"] == "FileNotFoundError"
    assert "SYNTHETIC_PRIVATE" not in json.dumps(diary.rows)


def test_nonstring_tool_name_cannot_crash_diagnostics(eng, tmp_path, monkeypatch):
    diary = RecordingDiag()
    monkeypatch.setattr(eng, "DIAGNOSTICS", diary)
    assert eng.Runner(str(tmp_path)).dispatch({}, {}).startswith("error:")
    assert diary.rows[0]["tool_name"] == "other"


def test_cleanup_keeps_primary_exception_and_records_both_boundaries(eng, monkeypatch, tmp_path):
    diary = RecordingDiag()
    primary = ValueError("synthetic primary")
    monkeypatch.setattr(eng, "DIAGNOSTICS", diary)
    monkeypatch.setattr(eng, "_main", lambda: (_ for _ in ()).throw(primary))
    monkeypatch.setattr(eng, "close_open_runners", lambda: (_ for _ in ()).throw(OSError("synthetic secondary")))
    with pytest.raises(ValueError) as error:
        eng.main()
    assert error.value is primary
    assert any(row["event"] == "cleanup_start" for row in diary.rows)
    assert any(row["event"] == "cleanup_end" and row.get("exception_class") == "OSError" for row in diary.rows)
    assert diary.rows[-1]["event"] == "exception"
    assert diary.rows[-1]["exception_class"] == "ValueError"


def test_post_completion_cleanup_error_records_fatal_evidence(eng, monkeypatch, tmp_path):
    diary = RecordingDiag()
    monkeypatch.setattr(eng, "DIAGNOSTICS", diary)
    monkeypatch.setattr(eng, "_main", lambda: None)
    monkeypatch.setattr(eng, "close_open_runners", lambda: (_ for _ in ()).throw(OSError("synthetic cleanup")))
    with pytest.raises(OSError):
        eng.main()
    assert diary.rows[-1]["event"] == "exception"
    assert diary.rows[-1]["exception_class"] == "OSError"


def test_runner_cleanup_failure_is_visible_and_does_not_skip_other_runners(eng, monkeypatch, tmp_path):
    diary, closed = RecordingDiag(), []
    class Fails:
        def close(self):
            raise OSError("SYNTHETIC_PRIVATE")
    class Healthy:
        def close(self):
            closed.append(True)
    monkeypatch.setattr(eng, "DIAGNOSTICS", diary)
    monkeypatch.setattr(eng, "_OPEN_RUNNERS", [Fails(), Healthy()])
    eng.close_open_runners()
    assert closed == [True]
    errors = [row for row in diary.rows if row["event"] == "cleanup_error"]
    assert errors and errors[0]["exception_class"] == "OSError"
    assert "SYNTHETIC_PRIVATE" not in json.dumps(diary.rows)


def test_build_fingerprint_covers_guard_changes(eng, tmp_path):
    hd = eng.capability_module("harness_diagnostics")
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir(); b.mkdir()
    for folder, content in ((a, "synthetic-a"), (b, "synthetic-b")):
        (folder / "delegate.py").write_text("synthetic", encoding="utf-8")
        (folder / "context_guard.py").write_text(content, encoding="utf-8")
    assert hd.bundle_id(a) != hd.bundle_id(b)


def test_build_fingerprint_refuses_incomplete_source_hash(eng, tmp_path):
    hd = eng.capability_module("harness_diagnostics")
    (tmp_path / "delegate.py").write_bytes(b"x" * (4 * 1024 * 1024 + 1))
    assert hd.bundle_id(tmp_path) is None


def test_fingerprint_manifest_covers_all_engine_source_modules(eng, tmp_path):
    hd = eng.capability_module("harness_diagnostics")
    assert {path.name for path in ENGINE_DIR.glob("*.py")} <= set(hd.BUNDLE_SOURCES)


def test_output_capture_exposes_actual_child_and_observers_fail_open(eng, tmp_path):
    co = eng.capability_module("command_output")
    script = tmp_path / "child.py"
    script.write_text("from pathlib import Path\nPath('once').write_text('x')\n", encoding="utf-8")
    children, exits = [], []
    def observed(proc):
        children.append(proc)
        raise RuntimeError("synthetic observer failure")
    result = co.capture_command([sys.executable, str(script)], cwd=tmp_path, timeout=20,
        open_sink=lambda _: None, on_start=observed, on_exit=exits.append)
    assert result.returncode == 0
    assert len(children) == 1 and children[0].pid > 0
    assert exits == [0]
    assert (tmp_path / "once").read_text() == "x"


def test_journal_sequence_order_matches_durable_write_order(eng, tmp_path, monkeypatch):
    hd = eng.capability_module("harness_diagnostics")
    path = tmp_path / "events.jsonl"
    journal = hd.Journal(path, "engine", engine_pid=123)
    entered, second_done = threading.Event(), threading.Event()
    original = hd.Journal._emit.__globals__["clean"]
    def interleave(values):
        if threading.current_thread().name == "first" and "engine_pid" in values:
            entered.set()
            second_done.wait(0.15)
        return original(values)
    monkeypatch.setitem(hd.Journal._emit.__globals__, "clean", interleave)
    first = threading.Thread(name="first", target=lambda: journal.emit("command_start"))
    def emit_second():
        journal.emit("command_exit", exit_code=0)
        second_done.set()
    second = threading.Thread(name="second", target=emit_second)
    first.start()
    assert entered.wait(2)
    second.start()
    first.join(2); second.join(2)
    assert not first.is_alive() and not second.is_alive()
    assert [json.loads(line)["seq"] for line in path.read_text().splitlines()] == [1, 2]

"""Turn-budget consumers: wrappers, watcher, ledger and UI see incomplete runs."""
from __future__ import annotations

import os

import pytest

from tests.test_ask_model_wrapper import FakeRun, _load

BASE = ["-m", "m", "-p", "x", "--tools"]


@pytest.fixture
def ask(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    module = _load()
    monkeypatch.setattr(module.tempfile, "tempdir", str(tmp_path))
    return module


@pytest.mark.parametrize("extra,soft", [(["--max-iters", "40", "--soft-iters", "27"], "27"),
                                        (["--soft-iters", "27"], "27"), ([], None)])
def test_ask_forwards_soft_iters_only_with_tools(ask, extra, soft):
    argv = ask.build_argv(ask.parse_args(BASE + extra), "p.txt")
    if soft is None:
        assert "--soft-iters" not in argv
    else:
        assert argv[argv.index("--soft-iters") + 1] == soft


@pytest.mark.parametrize("extra", [["--soft-iters", "3"],  # no --tools
                                   ["--tools", "--soft-iters", "0"], ["--tools", "--max-iters", "0"],
                                   ["--tools", "--max-iters", "40", "--soft-iters", "38"],
                                   ["--tools", "--soft-iters", "28"], ["--tools", "--max-iters", "1", "--soft-iters", "1"]])
def test_ask_rejects_bad_soft_iters(ask, extra):
    with pytest.raises(SystemExit) as err:
        ask.parse_args(["-m", "m", "-p", "x"] + extra)
    assert err.value.code == 2


@pytest.fixture
def fan():
    import importlib.util
    from pathlib import Path
    path = Path(__file__).resolve().parents[1] / "engine" / "fanout.py"
    spec = importlib.util.spec_from_file_location("fanout_budget", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def fake_child(code, out, seen):
    class Child:
        returncode, pid = code, 77

        def __init__(self, argv, **kwargs):
            seen.append(argv)

        def communicate(self, timeout=None):
            return out, "[step 9] synthetic\n"
    return Child


def test_fanout_forwards_soft_iters_and_keeps_old_signature(fan, monkeypatch, tmp_path):
    seen = []
    monkeypatch.setattr(fan.subprocess, "Popen", fake_child(0, "synthetic report", seen))
    fan.run_agent(1, "m", "high", "lens", "t", str(tmp_path), 40, "s", 5)
    assert "--soft-iters" not in seen[-1]
    fan.run_agent(1, "m", "high", "lens", "t", str(tmp_path), 40, "s", 5, soft_iters=27)
    assert seen[-1][seen[-1].index("--soft-iters") + 1] == "27"


def test_text_only_forwarding(ask, fan, monkeypatch, tmp_path):
    argv = ask.build_argv(ask.parse_args(BASE + ["--text-only", "--skill", "ponytail"]), "p.txt")
    assert "--text-only" in argv and argv[argv.index("--skill") + 1] == "ponytail"
    seen = []
    monkeypatch.setattr(fan.subprocess, "Popen", fake_child(3, "partial", seen))
    result = fan.run_agent(1, "m", "high", "l", "t", str(tmp_path), 4, "s", 5,
                           text_only=True, skills=["ponytail"])
    assert "--text-only" in seen[0] and "--skill" in seen[0]
    assert result[4].startswith("incomplete") and len(seen) == 1


def test_fanout_exit3_keeps_partial_evidence_as_not_success(fan, monkeypatch, tmp_path):
    seen = []
    monkeypatch.setattr(fan.subprocess, "Popen", fake_child(3, "synthetic partial\n", seen))
    idx, _, _, rep, err, tpath, _ = fan.run_agent(1, "m", "high", "l", "t", str(tmp_path), 4, "s", 5)
    assert rep == "synthetic partial" and err.startswith("incomplete") and tpath
    assert len(seen) == 1  # never relaunched
    results = [(1, "m", "a", "synthetic good", None, "t1", 1.0), (2, "m", "b", rep, err, tpath, 1.0)]
    prompts = []

    def read_prompt(argv, **kwargs):
        prompts.append(open(argv[argv.index("-f") + 1], encoding="utf-8").read())
        raise OSError("synthetic stop")
    monkeypatch.setattr(fan.subprocess, "Popen", read_prompt)
    fan.synthesize("t", results, "m", str(tmp_path))
    assert "synthetic good" in prompts[0] and "synthetic partial" not in prompts[0]


@pytest.mark.parametrize("codes,expected", [((0, 0), 0), ((0, 3), 3), ((3, 1), 1)])
def test_fanout_main_marks_incomplete_and_exits_nonzero(fan, monkeypatch, tmp_path, capsys,
                                                         codes, expected):
    seen, queue = [], list(codes)

    def child(argv, **kwargs):
        seen.append(argv)
        return fake_child(queue.pop(0), "synthetic text", [])(argv)
    monkeypatch.setattr(fan.subprocess, "Popen", child)
    monkeypatch.setattr(fan.sys, "argv", ["fanout", "-t", "t", "-C", str(tmp_path),
                                          "--lens", "a", "--lens", "b", "--max-parallel", "1",
                                          "--soft-iters", "5", "--max-iters", "10"])
    assert fan.main() == expected
    out = capsys.readouterr().out
    assert len(seen) == 2 and all("--soft-iters" in a for a in seen)  # no retry
    assert ("[INCOMPLETE" in out) == (3 in codes)


def test_fanout_rejects_soft_iters_beyond_work_capacity(fan, monkeypatch, tmp_path):
    monkeypatch.setattr(fan.sys, "argv", ["fanout", "-t", "t", "-C", str(tmp_path), "--lens", "a",
                                          "--max-iters", "10", "--soft-iters", "8"])
    with pytest.raises(SystemExit) as err:
        fan.main()
    assert err.value.code == 2


from tests.test_agent_await_launch import attempt, watch, watcher  # noqa: E402,F401


def set_end(index, **fields):
    import json
    records = [json.loads(line) for line in index.read_text(encoding="utf-8").splitlines()]
    records[-1].update(fields)
    index.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")


@pytest.mark.parametrize("reason", ["max_iters", "repair_limit", "no_action_limit",
                                    "mutation_outcome_unknown", "context_budget", "recovery_state"])
def test_watcher_reports_incomplete_with_known_reason(watcher, monkeypatch, tmp_path, attempt,
                                                      capsys, reason):
    set_end(attempt[1], status="failed", stop_reason=reason)
    assert watch(watcher, monkeypatch, tmp_path, attempt[0]) == 3
    out = capsys.readouterr().out
    assert f"INCOMPLETE - registered run stopped at {reason}" in out and "FINISHED" not in out


@pytest.mark.parametrize("fields", [{"status": "failed", "stop_reason": "SYNTHETIC<b>"},
                                    {"status": "failed", "stop_reason": ["max_iters"]},
                                    {"status": "failed", "stop_reason": {"code": "max_iters"}},
                                    {"status": "failed"}, {"status": "interrupted"}])
def test_watcher_unknown_or_legacy_terminal_stays_failed(watcher, monkeypatch, tmp_path, attempt,
                                                         capsys, fields):
    set_end(attempt[1], **fields)
    assert watch(watcher, monkeypatch, tmp_path, attempt[0]) == 7
    out = capsys.readouterr().out
    assert "FAILED -" in out and "SYNTHETIC<b>" not in out and "INCOMPLETE" not in out


def test_watcher_legacy_partial_footer_is_incomplete_not_success(watcher, monkeypatch, tmp_path,
                                                                 capsys):
    runs = tmp_path / ".claude/agent-runs"
    runs.mkdir(parents=True)
    (runs / "20000101-000000-synthetic-worker.md").write_text(
        "## Final report\n\n[PARTIAL - stopped at the 3-iteration cap]\n\nsynthetic\n"
        "[synthetic-model: 3 tool calls]\n", encoding="utf-8")
    assert watch(watcher, monkeypatch, tmp_path) == 6
    out = capsys.readouterr().out
    assert "INCOMPLETE" in out and "FINISHED" not in out


from tests.test_run_index import end_json, start_json  # noqa: E402

REASONS = ["max_iters", "repair_limit", "no_action_limit", "mutation_outcome_unknown",
           "context_budget", "recovery_state"]


@pytest.mark.parametrize("reason", REASONS)
def test_end_event_parses_known_stop_reason(reason):
    from agent_control.run_index import parse_index_record
    event = parse_index_record({**end_json(status="failed"), "stop_reason": reason}, 1)
    assert event.status == "failed" and event.stop_reason == reason


@pytest.mark.parametrize("record", [end_json(), end_json(status="failed"), end_json(schema=None),
                                    {**end_json(status="failed"), "stop_reason": "SYNTHETIC<b>"},
                                    {**end_json(status="failed"), "stop_reason": 7},
                                    {**end_json(), "stop_reason": "max_iters"},
                                    {**end_json(), "stop_reason": "context_budget"},
                                    {**end_json(), "stop_reason": "recovery_state"}])
def test_end_event_legacy_or_invalid_reason_is_none_and_terminal_kept(record):
    from agent_control.run_index import parse_index_record
    event = parse_index_record(record, 1)
    assert event.stop_reason is None and event.status in ("complete", "failed")


from tests.test_sync import append_index, synchronizer  # noqa: E402,F401


def current_row(paths, run_id="r"):
    from agent_control.ledger import highest_valid_revisions, load_ledger
    return highest_valid_revisions(load_ledger(paths.ledger)[0])[run_id]


@pytest.mark.parametrize("reason", ["max_iters", "context_budget", "recovery_state"])
def test_sync_ledgers_stop_reason_and_is_idempotent(temp_paths, synchronizer, reason):
    from agent_control.ledger import load_ledger
    append_index(temp_paths.machine_index, [start_json(run_id="r"), {
        **end_json(run_id="r", status="failed"), "stop_reason": reason}])
    assert synchronizer.run().appended_revisions == 1
    assert synchronizer.run().appended_revisions == 0
    row = current_row(temp_paths)
    assert (row.status, row.stop_reason, row.tokens_sent) == ("failed", reason, 1234)
    assert [r.stop_reason for r in load_ledger(temp_paths.ledger)[0]] == [reason]


def test_sync_drops_context_budget_on_complete_record(temp_paths, synchronizer):
    append_index(temp_paths.machine_index, [start_json(run_id="r"), {
        **end_json(run_id="r"), "stop_reason": "context_budget"}])
    synchronizer.run()
    row = current_row(temp_paths)
    assert (row.status, row.stop_reason) == ("complete", None)


def test_unmatched_end_keeps_reason_across_checkpoint(temp_paths, synchronizer):
    append_index(temp_paths.machine_index, [{**end_json(run_id="r", status="failed"),
                                             "stop_reason": "repair_limit"}])
    synchronizer.run()
    append_index(temp_paths.machine_index, [start_json(run_id="r")])
    synchronizer.run()
    assert current_row(temp_paths).stop_reason == "repair_limit"


def test_legacy_ledger_row_without_reason_loads_and_bad_reason_is_dropped(temp_paths, synchronizer):
    import json
    from agent_control.ledger import load_ledger
    append_index(temp_paths.machine_index, [start_json(run_id="r"), end_json(run_id="r")])
    synchronizer.run()
    row = json.loads(temp_paths.ledger.read_text(encoding="utf-8"))
    assert "stop_reason" in row and row["stop_reason"] is None
    del row["stop_reason"]
    bad = {**row, "run_id": "bad", "status": "failed", "stop_reason": "SYNTHETIC<b>"}
    temp_paths.ledger.write_text(json.dumps(row) + "\n" + json.dumps(bad) + "\n", encoding="utf-8")
    records, _ = load_ledger(temp_paths.ledger)
    assert [r.stop_reason for r in records] == [None, None]


@pytest.mark.parametrize("reason", ["max_iters", "context_budget", "recovery_state"])
def test_snapshot_rows_carry_stop_reason_and_failed_filter_still_matches(reason):
    from dataclasses import replace
    from agent_control.snapshot import SnapshotQuery, build_snapshot
    from tests.test_snapshot import NOW, project, run
    runs = [replace(run(run_id="inc", status="failed"), stop_reason=reason),
            run(run_id="old", status="failed")]
    snap = build_snapshot(runs, {"P1": project("P1")}, {}, [],
                          SnapshotQuery(states=("failed",)), now=NOW)
    rows = {row["run_id"]: row for row in snap["runs"]}
    assert rows["inc"]["status"] == "failed" and rows["inc"]["stop_reason"] == reason
    assert rows["old"]["stop_reason"] is None


def test_ask_propagates_incomplete_exit3_and_cleans_prompt(ask, monkeypatch):
    run = FakeRun(returncode=3)
    monkeypatch.setattr(ask.subprocess, "Popen", run)
    assert ask.main(BASE + ["--max-iters", "2"]) == 3
    assert not os.path.exists(run.prompt_path)

"""Whole recovery metadata must fit budgets without losing cursors or pairing."""
import json

import pytest

from test_engine_reasoning import engine  # noqa: F401


def test_compact_command_keeps_later_result_slot(engine):
    cg = engine.capability_module("context_guard")
    co = engine.capability_module("command_output")
    guard = cg.Guard(profile="resilient-v1")
    assert guard.start_batch({}, 2)
    guard.batch = guard.output = cg.BATCH_BUDGET - 2 * cg.COMPACT_RESULT
    fallback = "exit=7\ncommand output saved; do not rerun"
    result = co.CompactText("exit=7\n" + "x" * 3000, lambda: fallback)
    assert guard.admit("run_command", result, compact=result.compact) == fallback
    assert guard.admit("read_file", "later result") == "later result"
    assert not guard.closing() and guard.pending == 0
    assert guard.batch <= cg.BATCH_BUDGET and guard.output <= cg.OUTPUT_BUDGET


def test_dropped_read_page_returns_same_cursor_and_charges_read(engine):
    cg = engine.capability_module("context_guard")
    co = engine.capability_module("command_output")
    page = {"ref": "cmd://synthetic.out", "offset": 500, "next_offset": 4596,
            "eof": False, "capture_complete": True, "stored_bytes": 10000,
            "discarded_bytes": 0, "encoding": "utf-8", "data": "x" * 4096}
    result = co.CompactText(json.dumps(page), lambda: co.page_compact(page))
    guard = cg.Guard(profile="resilient-v1")
    assert guard.start_batch({}, 1)
    guard.read = cg.READ_BUDGET - cg.COMPACT_RESULT
    before = guard.read
    admitted = guard.admit("read_command_output", result, compact=result.compact)
    got = json.loads(admitted)
    assert got["next_offset"] == got["offset"] == 500
    assert got["data"] == "" and got["eof"] is False
    assert guard.read == before + len(admitted.encode("utf-8"))
    assert not guard.closing()


@pytest.mark.parametrize("resource,tool", [("read", "read_command_output"),
                                           ("output", "run_command")])
def test_real_cumulative_exhaustion_still_closes(engine, resource, tool):
    cg = engine.capability_module("context_guard")
    guard = cg.Guard(profile="resilient-v1")
    assert guard.start_batch({}, 1)
    setattr(guard, resource, cg.READ_BUDGET if resource == "read" else cg.OUTPUT_BUDGET)
    result = guard.admit(tool, "private output", compact=lambda: "compact")
    assert guard.closing()
    assert guard.reason == ("cumulative read" if resource == "read" else "cumulative tool output")
    assert "private output" not in result


def test_legacy_does_not_evaluate_recovery_callback(engine):
    cg = engine.capability_module("context_guard")
    guard = cg.Guard(profile="legacy")
    assert guard.start_batch({}, 1)
    result = guard.admit("run_command", "exit=0\n" + "x" * (cg.BATCH_BUDGET + 1),
                         compact=lambda: pytest.fail("legacy invoked recovery callback"))
    assert guard.closing() and result.startswith("exit=0\n")

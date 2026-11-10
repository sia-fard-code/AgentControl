"""Resilient Guard reservation edges; synthetic strings only, no external actions."""
import pytest

from test_engine_reasoning import engine  # noqa: F401


def _g(engine, n):
    cg = engine.capability_module("context_guard")
    g = cg.Guard(profile="resilient-v1")
    return cg, g, g.start_batch({}, n)


def _within(cg, g):
    assert g.batch <= cg.BATCH_BUDGET and g.output <= cg.OUTPUT_BUDGET
    assert g.read <= cg.READ_BUDGET
    assert g.base <= cg.REQUEST_LIMIT - cg.CLOSEOUT_RESERVE


def test_two_call_counterexample_fits(engine):
    cg, g, ok = _g(engine, 2)
    assert ok
    g.admit("read_file", "x" * cg.BATCH_BUDGET)
    g.admit("read_file", "x")
    _within(cg, g)


def test_output_511_rejects_batch_before_action(engine):
    cg = engine.capability_module("context_guard")
    g = cg.Guard(profile="resilient-v1")
    g.output = cg.OUTPUT_BUDGET - 511
    assert not g.start_batch({}, 1)
    assert g.exhausted and g.reason


def test_read_spent_refusal_leaves_read_unchanged(engine):
    cg, g, _ = _g(engine, 1)
    g.read = cg.READ_BUDGET
    out = g.admit("read_file", "abc")
    assert "refused" in out and g.read == cg.READ_BUDGET
    assert len(out.encode()) <= cg.COMPACT_RESULT and g.output == len(out.encode())
    _within(cg, g)


def test_fitting_read_reserves_later_compact_reply(engine):
    cg, g, _ = _g(engine, 2)
    out = g.admit("read_file", "x" * (cg.BATCH_BUDGET - 100))
    assert "[context budget]" in out
    assert g.batch <= cg.BATCH_BUDGET - cg.COMPACT_RESULT


def test_skip_oversized_text_bounded(engine):
    cg, g, _ = _g(engine, 1)
    out = g.skip(text="y" * 100000)
    assert len(out.encode()) <= cg.COMPACT_RESULT
    _within(cg, g)


def test_command_overage_keeps_exit_latch_no_replay(engine):
    cg, g, _ = _g(engine, 1)
    g.output = cg.OUTPUT_BUDGET - 600
    out = g.admit("run_command", "exit=3\n" + "z" * 5000)
    assert out.startswith("exit=3\n") and "not re-run" in out and g.exhausted
    _within(cg, g)


def test_envelope_and_utf8_escape_cost_fit(engine):
    cg, g, _ = _g(engine, 1)
    g.base = cg.REQUEST_LIMIT - cg.CLOSEOUT_RESERVE - 1000
    g.admit("read_file", "\x01é" * 900, envelope={"role": "tool", "tool_call_id": "c1"})
    _within(cg, g)


def test_three_pending_calls_safe(engine):
    cg, g, _ = _g(engine, 3)
    g.admit("read_file", "x" * cg.BATCH_BUDGET)
    g.admit("grep", "x" * cg.BATCH_BUDGET)
    g.skip()
    _within(cg, g)
    assert g.pending == 0


def test_replace_result_not_double_consumed(engine):
    cg, g, _ = _g(engine, 2)
    g.admit("finish", "ok")
    g.replace_result("finish", "ok", "done")
    assert g.pending == 1
    g.admit("read_file", "x" * cg.BATCH_BUDGET)
    _within(cg, g)
    assert g.pending == 0


def test_legacy_start_batch_unchanged(engine):
    cg = engine.capability_module("context_guard")
    g = cg.Guard()
    g.output = cg.OUTPUT_BUDGET - 511
    assert g.start_batch({}, 1)


def test_clipped_read_preserves_cumulative_output_reservation(engine, tmp_path):
    cg = engine.capability_module("context_guard")
    g = cg.Guard(profile="resilient-v1")
    g.output = cg.OUTPUT_BUDGET - 1200
    assert g.start_batch({}, 2)
    g.admit("read_file", "x" * 2000)
    assert g.output <= cg.OUTPUT_BUDGET - cg.COMPACT_RESULT
    g.admit("read_file", "x")
    _within(cg, g)


@pytest.mark.parametrize("count", [True, False, -1, 1.5, "2"],
                         ids=["true", "false", "negative", "float", "text"])
def test_resilient_call_count_is_strict_integer(engine, tmp_path, count):
    cg = engine.capability_module("context_guard")
    with pytest.raises(ValueError, match="call count"):
        cg.Guard(profile="resilient-v1").start_batch({}, count)

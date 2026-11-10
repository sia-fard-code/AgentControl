"""resilient-v1 Guard edges: slot accounting and no-noise for fitting results."""
from __future__ import annotations

import pytest

from test_engine_reasoning import engine, harness  # noqa: F401

RES = "resilient-v1"


@pytest.fixture()
def cg(engine):
    return engine.capability_module("context_guard")


def test_replace_result_does_not_consume_two_slots(cg) -> None:
    g = cg.Guard(profile=RES)
    g.start_batch({"m": "x"}, 3)
    g.admit("run_command", "ok", envelope={"role": "tool"})
    assert g.pending == 2
    g.replace_result("run_command", "ok", "done", envelope={"role": "tool"})
    assert g.pending == 2


def test_fitting_result_gets_no_notice(cg) -> None:
    g = cg.Guard(profile=RES)
    g.start_batch({"m": "x"}, 1)
    assert g.admit("grep", "a:1:hit", envelope={"role": "tool"}) == "a:1:hit"

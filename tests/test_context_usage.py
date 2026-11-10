"""Compaction-phase usage rows through the EXISTING journal writer and reader.

Expected to pass without production changes: append_usage_record takes any phase
and read_usage_journal skips only MCP phases. These tests pin that behaviour.
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

from agent_control.usage import read_usage_journal

ENGINE = Path(__file__).resolve().parents[1] / "engine" / "delegate.py"
SECRET = "SYNTHETIC-NOTE-7f3a"
RAW = {"input_tokens": 10, "output_tokens": 2, "cache_read_input_tokens": 5,
       "history": SECRET, "notes": [SECRET], "body": {"text": SECRET}}


@pytest.fixture(scope="module")
def engine():
    spec = importlib.util.spec_from_file_location("ctx_usage_engine", ENGINE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _append(engine, path, phase, raw=RAW):
    assert engine.append_usage_record(str(path), iteration=1, phase=phase, model="m",
                                      timestamp="2026-01-01T00:00:00Z", raw_usage=raw)


@pytest.mark.parametrize("phase", ["compaction", "compaction_synthetic_unknown"])
def test_compaction_phase_counts_like_loop_and_drops_content(engine, tmp_path, phase):
    loop, comp = tmp_path / "loop.usage.jsonl", tmp_path / "comp.usage.jsonl"
    _append(engine, loop, "loop")
    _append(engine, comp, phase)
    text = comp.read_text(encoding="utf-8")
    assert SECRET not in text and json.loads(text)["phase"] == phase
    evidence = read_usage_journal(comp)
    assert evidence.totals == read_usage_journal(loop).totals  # same billed/cached semantics
    assert evidence.totals.tokens_sent == 10 and evidence.totals.cache_read == 5
    assert SECRET not in repr(evidence)


def test_null_counters_stay_unknown_and_zero_stays_known(engine, tmp_path):
    zero, null = tmp_path / "zero.usage.jsonl", tmp_path / "null.usage.jsonl"
    _append(engine, zero, "compaction", {"input_tokens": 0, "output_tokens": 0})
    _append(engine, null, "compaction", None)
    assert read_usage_journal(zero).totals.tokens_sent == 0
    assert read_usage_journal(null).totals.tokens_sent is None


def test_append_only_and_rereading_does_not_double_count(engine, tmp_path):
    path = tmp_path / "a.usage.jsonl"
    _append(engine, path, "loop")
    _append(engine, path, "compaction")
    first = path.read_text(encoding="utf-8")
    assert len(first.splitlines()) == 2
    assert read_usage_journal(path).totals == read_usage_journal(path).totals
    assert read_usage_journal(path).totals.tokens_sent == 20
    _append(engine, path, "compaction")
    assert path.read_text(encoding="utf-8").startswith(first)

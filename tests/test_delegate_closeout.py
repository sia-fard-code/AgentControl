"""Bounded, tool-less closeout review; all model responses are synthetic."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from test_engine_reasoning import RESPONSES_USAGE, engine, harness  # noqa: F401
from test_engine_turn_budget import GPT, _call, _list_reply, _prose, _run, _usage
from test_delegate_verification import (  # noqa: F401
    CHECK, FINISH, VERIFY, _batch, _completion, _isolated_skills, _passing_command)


def _review(verdict, codes=()):
    return _prose(json.dumps({"verdict": verdict, "finding_codes": list(codes)}),
                  RESPONSES_USAGE)


def test_review_finish_pass_completes_without_extra_worker_turn_and_never_upgrades_verification(
        harness, engine, tmp_path, capsys):
    assert _run(harness, "-m", GPT, "--review-finish", replies=[_batch(
        ("write_file", {"path": "x.txt", "content": "synthetic candidate\n"}), FINISH),
        _review("pass")]) == 0
    assert len(harness.payloads) == 2
    reviewer = harness.payloads[1]
    assert "tools" not in reviewer and "tool_choice" not in reviewer
    review_data = json.loads(reviewer["messages"][1]["content"])
    assert review_data["verification_status"] == "staged_unverified_on_disk"
    record = _completion(harness)
    assert record["verification_status"] == "staged_unverified_on_disk"
    assert record["review"]["verdict"] == "pass"
    assert record["review"]["applicability"] == "current"
    assert record["review_attempted"] is True and record["correction_attempted"] is False
    assert "[reviewer opinion] pass" in capsys.readouterr().out
    transcript = harness.transcript_path.read_text(encoding="utf-8")
    assert "[reviewer opinion] pass" in transcript
    assert transcript.splitlines()[-1].startswith(f"[{GPT} ")
    assert [(row["phase"], row["sent"]) for row in _usage(harness)] == [
        ("loop", None), ("review", 100)]
    assert "synthetic candidate" not in json.dumps(record)
    rr = engine.capability_module("run_review")
    loaded = rr.load_recorded_review_input(
        record, artifact_dir=str(harness.transcript_path.with_suffix(".artifacts")),
        task_text="do the thing", transcript_path=str(harness.transcript_path))
    assert isinstance(loaded, rr.ReviewInput)
    assert loaded.binding.patch_sha256 == record["patch_binding"]["patch_sha256"]
    assert not (tmp_path / "x.txt").exists()


def test_review_finish_concerns_triggers_at_most_one_correction_and_marks_superseded_on_edit(
        harness, capsys):
    assert _run(harness, "-m", GPT, "--review-finish", replies=[_batch(
        ("write_file", {"path": "x.txt", "content": "synthetic first\n"}), FINISH),
        _review("concerns", ["task_coverage"]), _batch(
        ("write_file", {"path": "x.txt", "content": "synthetic second\n"}), FINISH)]) == 0
    assert len(harness.payloads) == 3
    record = _completion(harness)
    assert record["review_attempted"] is True and record["correction_attempted"] is True
    assert record["review"]["verdict"] == "concerns"
    assert record["review"]["applicability"] == "superseded"
    assert record["review"]["reviewed_binding"]["candidate_seq"] == 1
    assert record["patch_binding"]["candidate_seq"] == 2
    messages = harness.payloads[2]["messages"]
    assert any("[reviewer opinion — non-authoritative]" in str(m.get("content"))
               for m in messages)
    assert not any(str(m.get("content")).startswith("[operator guidance]") for m in messages)
    assert not Path(str(harness.transcript_path) + ".steer").exists()
    marker = "[reviewer opinion] concerns — superseded; applies to candidate 1, not candidate 2"
    assert marker in harness.transcript_path.read_text(encoding="utf-8")
    assert marker in capsys.readouterr().out
    assert [r["phase"] for r in _usage(harness)] == ["loop", "review", "loop"]


@pytest.mark.parametrize("tool_mode", ["native", "prompt"])
def test_review_unexpected_tool_call_is_never_executed(harness, tmp_path, tool_mode):
    args = {"path": "reviewer.txt", "content": "synthetic forbidden write"}
    unexpected = (_call("write_file", args) if tool_mode == "native" else
                  _prose("```tool " + json.dumps({"name": "write_file", "args": args}) + "```"))
    assert _run(harness, "-m", GPT, "--review-finish", replies=[_batch(
        ("write_file", {"path": "x.txt", "content": "synthetic candidate\n"}), FINISH),
        unexpected]) == 0
    record = _completion(harness)
    assert record["review"]["verdict"] == "abstain"
    assert record["review"]["unavailable_reason"] == "unexpected_tool_call"
    assert not (tmp_path / "reviewer.txt").exists()
    assert "reviewer.txt" not in (tmp_path / "agent.patch").read_text(encoding="utf-8")
    assert len(harness.payloads) == 2 and record["correction_attempted"] is False


def test_verification_correction_takes_priority_over_review_when_one_turn_remains_and_respects_max_iters(
        harness, engine, monkeypatch):
    commands = _passing_command(engine, monkeypatch)
    assert _run(harness, "-m", GPT, "--write", *VERIFY, "--review-finish", "--max-iters", "3",
                replies=[_batch(
                    ("write_file", {"path": "x.py", "content": "value = 1\n"}), FINISH),
                    _batch(("run_command", {"argv": CHECK}), FINISH)]) == 0
    assert len(harness.payloads) == 2 <= 3
    assert all("tools" in payload for payload in harness.payloads)
    assert len(commands) == 1
    assert "factual verification incomplete" in json.dumps(harness.payloads[1]["messages"])
    record = _completion(harness)
    assert record["verification_status"] == "verified_by_command"
    assert record["correction_attempted"] is True and record["review_attempted"] is True
    assert record["review"]["unavailable_reason"] == "unbounded_disk_write"


def test_review_consumes_shared_budget_and_reserved_report_keeps_correct_turn(harness):
    assert _run(harness, "-m", GPT, "--review-finish", "--max-iters", "5", replies=[
        _batch(("write_file", {"path": "x.txt", "content": "synthetic candidate\n"}), FINISH),
        _review("concerns", ["verification_gap"]),
        _list_reply("native"), _list_reply("native"), _prose("synthetic partial report")]) == 3
    assert len(harness.payloads) == 5
    assert "tools" not in harness.payloads[-1]
    assert [(row["iter"], row["phase"]) for row in _usage(harness)] == [
        (1, "loop"), (1, "review"), (2, "loop"), (3, "loop"), (5, "wrapup")]
    assert _completion(harness)["review_attempted"] is True
    tail = harness.transcript_path.read_text(encoding="utf-8").splitlines()[-1]
    assert tail.startswith(f"[{GPT} ") and "tool calls" in tail

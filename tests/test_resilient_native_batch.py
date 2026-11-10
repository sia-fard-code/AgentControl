"""Phase 2 main-loop checks: synthetic chat and temporary files only."""
from __future__ import annotations

import json

import pytest

from test_engine_reasoning import engine, harness, _finish_reply  # noqa: F401
from test_engine_turn_budget import _call, _prose, _run as _run_agent, _end, GPT

CLAUDE = "anthropic_tool_call.claude-x"


def _run(harness, *args, replies):
    # Disk assertions must exercise actual temporary-tree mutations, not staged edits.
    return _run_agent(harness, *args, "--write", replies=replies)


def _batch(*replies):
    calls = []
    for n, reply in enumerate(replies):
        call = reply["choices"][0]["message"]["tool_calls"][0]
        call["id"] = f"c{n}"
        calls.append(call)
    return {"choices": [{"message": {"content": "", "tool_calls": calls},
                         "finish_reason": "stop"}]}


def _write(text="ok", path="out.txt"):
    return _call("write_file", {"path": path, "content": text})


def _health(harness):
    path = harness.transcript_path.with_name("run.protocol-health.json")
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.mark.parametrize("profile", ["resilient-v1", "legacy"], ids=["new", "old"])
def test_three_claude_batches(harness, tmp_path, profile):
    replies = [_batch(_write(str(n)), _call("list_dir", {"path": "."}))
               for n in range(3)]
    replies += [_finish_reply("native") if profile == "resilient-v1" else _prose()]
    code = _run(harness, "-m", CLAUDE, "--profile", profile, replies=replies)
    if profile == "legacy":
        assert code == 3 and _end(tmp_path)["stop_reason"] == "repair_limit"
        assert not (tmp_path / "out.txt").exists()
        assert not harness.transcript_path.with_name("run.protocol-health.json").exists()
    else:
        assert code == 0 and (tmp_path / "out.txt").read_text() == "2"
        assert _health(harness)["total_incidents"] == 0
        assert "multiple_tool_calls" not in harness.transcript_path.read_text()


@pytest.mark.parametrize("kind", ["json", "schema", "dupkey", "float", "paging"],
                          ids=["json", "schema", "dupkey", "float", "paging"])
@pytest.mark.parametrize("model", [CLAUDE, GPT], ids=["claude", "gpt"])
def test_invalid_tail_prevents_earlier_write(harness, tmp_path, kind, model):
    tails = {
        "json": _call("write_file", {}, raw="{SECRET"),
        "schema": _call("write_file", {"path": "x", "content": "x", "SECRET": 1}),
        "dupkey": _call("write_file", {}, raw='{"path":"a","path":"b","content":"x"}'),
        "float": _call("write_file", {}, raw='{"path":"a","content":1e400}'),
        "paging": _call("read_file", {"path": "a", "start_line": True}),
    }
    bad = _batch(_write(), tails[kind])
    assert _run(harness, "-m", model, "--profile", "resilient-v1",
                replies=[bad, _finish_reply("native")]) == 0
    assert not (tmp_path / "out.txt").exists()
    messages = harness.payloads[1]["messages"]
    assert [m["tool_call_id"] for m in messages if m["role"] == "tool"] == ["c0", "c1"]
    health = _health(harness)
    assert health["total_incidents"] == 1
    assert "SECRET" not in json.dumps(health)


@pytest.mark.parametrize("kind", ["dup", "missing", "shape"], ids=["dup", "missing", "shape"])
def test_unpairable_packet_not_replayed(harness, tmp_path, kind):
    bad = _batch(_write(), _write("tail"))
    calls = bad["choices"][0]["message"]["tool_calls"]
    if kind == "dup":
        calls[1]["id"] = calls[0]["id"]
    elif kind == "missing":
        del calls[1]["id"]
    else:
        calls[1]["function"] = "SECRET"
    assert _run(harness, "-m", CLAUDE, "--profile", "resilient-v1",
                replies=[bad, _finish_reply("native")]) == 0
    assert not (tmp_path / "out.txt").exists()
    assert not any("tool_calls" in m or m["role"] == "tool"
                   for m in harness.payloads[1]["messages"])
    assert _health(harness)["total_incidents"] == 1


@pytest.mark.parametrize("spaced", [True, False], ids=["spaced", "consecutive"])
def test_local_repair_health(harness, tmp_path, spaced):
    replies = []
    for _ in range(3):
        replies.append(_call("write_file", {}, raw="{SECRET"))
        if spaced:
            replies.append(_call("list_dir", {"path": "."}))
    replies.append(_finish_reply("native") if spaced else _prose())
    assert _run(harness, "-m", CLAUDE, "--profile", "resilient-v1",
                replies=replies) == (0 if spaced else 3)
    health = _health(harness)
    assert health["total_incidents"] == 3 and health["progress_seq"] == 0
    assert health["diagnostic_needed"] is True
    assert "SECRET" not in json.dumps(health)
    if not spaced:
        assert _end(tmp_path)["stop_reason"] == "repair_limit"


@pytest.mark.parametrize("finish_first", [False, True], ids=["finish-last", "finish-first"])
def test_uncertain_command_blocks_later_actions_and_pairs_calls(
        harness, tmp_path, engine, monkeypatch, finish_first):
    calls = []

    def failed_command(*args, **kwargs):
        calls.append(args)
        raise OSError("synthetic communication failure")

    monkeypatch.setattr(engine.capability_module("command_output"),
                        "capture_command", failed_command)
    monkeypatch.setattr(engine.shutil, "which", lambda name: name)
    actions = [
        _call("run_command", {"argv": ["python", "-m", "py_compile", "x.py"]}),
        _call("write_file", {"path": "must-not-exist.txt", "content": "synthetic"}),
    ]
    finish = _finish_reply("native")
    actions = [finish, *actions] if finish_first else [*actions, finish]
    assert _run(harness, "-m", CLAUDE, "--profile", "resilient-v1",
                replies=[_batch(*actions), _prose("synthetic partial")]) == 3
    assert len(calls) == 1 and not (tmp_path / "must-not-exist.txt").exists()
    assert _end(tmp_path)["stop_reason"] == "mutation_outcome_unknown"
    assert len(harness.payloads) == 2
    results = [m["tool_call_id"] for m in harness.payloads[1]["messages"]
               if m.get("role") == "tool"]
    assert sorted(results) == ["c0", "c1", "c2"]


def test_finish_before_write_uses_final_mutation_evidence(
        harness, tmp_path, engine, monkeypatch):
    import inspect

    fg = engine.capability_module("finish_gate")
    original = fg.evaluate_finish
    finish_sequences = []

    def observed(snapshot, plan):
        if inspect.currentframe().f_back.f_code.co_name == "evaluate_finish_proposal":
            finish_sequences.append(snapshot.mutation_seq)
        return original(snapshot, plan)

    monkeypatch.setattr(fg, "evaluate_finish", observed)
    reply = _batch(_finish_reply("native"),
                   _call("write_file", {"path": "after.txt", "content": "synthetic"}))
    assert _run(harness, "-m", CLAUDE, "--profile", "resilient-v1", replies=[reply]) == 0
    assert (tmp_path / "after.txt").read_text() == "synthetic"
    assert finish_sequences == [1]
    completion = json.loads((harness.transcript_path.with_suffix(".artifacts")
                             / "completion.json").read_text())
    assert completion["verification_status"] == "unverified_post_edit_mutation"

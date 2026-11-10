"""Live tool progress with synthetic replies and tmp_path transcripts only."""
from __future__ import annotations

import json
import re

import pytest

from agent_control.progress import parse_progress
from test_engine_reasoning import engine, harness  # noqa: F401
from test_engine_turn_budget import GPT, _call, _finish_reply, _prose, _run
from test_delegate_verification import _batch


@pytest.fixture(autouse=True)
def _isolated_skills(tmp_path, monkeypatch):
    home = str(tmp_path / "home")
    monkeypatch.setenv("HOME", home)
    monkeypatch.setenv("USERPROFILE", home)


def _stale_retries(engine, monkeypatch):
    write = engine.Transcript.write

    def with_retries(self, text):
        if text.lstrip().startswith("_[") and "] calling " in text:
            write(self, "### [1] NO_TOOL_ACTION({})\n"
                  "_[1] reply cut by the gateway's 2-minute limit after 121 s; "
                  "asking for a smaller step_")
        return write(self, text)

    monkeypatch.setattr(engine.Transcript, "write", with_retries)


def _assert_running(harness, name):
    text = harness.transcript_path.read_text(encoding="utf-8")
    progress = parse_progress(text)
    assert progress.phase == "tools"
    assert progress.last_tool == name
    assert progress.attempt is None and progress.max_attempts is None
    assert progress.nudges == progress.cuts == 0
    assert set(progress.as_dict()) == {
        "iteration", "phase", "attempt", "max_attempts", "nudges",
        "last_tool", "last_path", "cuts",
    }
    return text


@pytest.mark.parametrize("mode", ["native", "prompt"])
@pytest.mark.parametrize("failing", [False, True])
def test_tool_phase_visible_inside_dispatch(harness, engine, monkeypatch, mode, failing):
    _stale_retries(engine, monkeypatch)
    name = "read_file" if failing else "list_dir"
    args = {"path": "missing.txt" if failing else "."}
    dispatch = engine.Runner.dispatch
    results = []

    def observed(self, tool, tool_args):
        _assert_running(harness, tool)
        result = dispatch(self, tool, tool_args)
        results.append(result)
        return result

    monkeypatch.setattr(engine.Runner, "dispatch", observed)
    narration = "synthetic tool narration"
    if mode == "native":
        reply = _call(name, args)
        reply["choices"][0]["message"]["content"] = narration
    else:
        reply = _prose(narration + '\n```tool ' + json.dumps({
            "name": name, "args": args}) + '```')
    flags = ["--tool-mode", mode]
    if mode == "prompt":
        flags.append("--allow-prompt-tools")  # fixture only; no policy change
    assert _run(harness, "-m", GPT, "-e", "high", *flags,
                replies=[reply, _finish_reply(mode)]) == 0
    text = harness.transcript_path.read_text(encoding="utf-8")
    assert len(results) == 1
    assert results[0].startswith("error:") == failing
    assert text.count(f"### [1] {name}(") == 1
    # model-v1 prompt mode renders the canonical admitted result the model receives.
    shown = f"Result of {name}:\n\n" + results[0] if mode == "prompt" else results[0]
    assert text.count("```\n" + shown + "\n```") == 1
    assert text.count(narration) == 1
    assert parse_progress(text).phase == "finished"


@pytest.mark.parametrize("finish_first", [False, True])
@pytest.mark.parametrize("finish_args", [{}, {"summary": "synthetic batch done"}])
def test_native_batch_renders_each_result_before_next_dispatch(
        harness, engine, monkeypatch, finish_first, finish_args):
    _stale_retries(engine, monkeypatch)
    actions = [("list_dir", {"path": "."}),
               ("read_file", {"path": "missing.txt"})]
    finish = ("finish", finish_args)
    actions.insert(0 if finish_first else len(actions), finish)
    reply = _batch(*actions)
    reply["choices"][0]["message"]["content"] = "synthetic batch narration"
    dispatch = engine.Runner.dispatch
    results = []

    def observed(self, name, args):
        text = _assert_running(harness, name)
        for _, result in results:
            assert "```\n" + result + "\n```" in text
        result = dispatch(self, name, args)
        results.append((name, result))
        return result

    monkeypatch.setattr(engine.Runner, "dispatch", observed)
    # Empty summaries remain an unusable report, but must still render the call.
    # Strict model-v1 preflight refuses finish({}) (summary is required) before any
    # dispatch; the unvalidated empty-finish batch is a legacy-only wire contract.
    policy = [] if finish_args else ["--context-policy", "legacy"]
    assert _run(harness, "-m", GPT, "-e", "high", *policy,
                replies=[reply]) == (0 if finish_args else 1)
    text = harness.transcript_path.read_text(encoding="utf-8")
    assert len(results) == 2
    for name, result in results:
        assert text.count(f"### [1] {name}(") == 1
        assert text.count("```\n" + result + "\n```") == 1
    assert text.count("### [1] finish(") == 1
    assert text.count("synthetic batch narration") == 1
    blocks = re.findall(r"### \[1\] (\w+)\([^\n]*\)\n\n```\n(.*?)\n```", text, re.S)
    assert dict(blocks) == {**dict(results), "finish": "ok"}
    tool_messages = [m for m in harness.payloads[0]["messages"] if m["role"] == "tool"]
    assert tool_messages == []  # the initial request has no fabricated tool results
    assert parse_progress(text).phase == "finished"


@pytest.mark.parametrize("policy", ["legacy", "model-v1"])
def test_empty_finish_after_bad_arguments_has_one_paired_result(harness, policy):
    reply = _batch(("read_file", []), ("finish", {}))
    assert _run(harness, "-m", GPT, "-e", "high", "--context-policy", policy,
                replies=[reply, _finish_reply("native")]) == 0
    text = harness.transcript_path.read_text(encoding="utf-8")
    blocks = re.findall(r"### \[1\] (\w+)\([^\n]*\)\n\n```\n(.*?)\n```", text, re.S)
    assert len(blocks) == 2
    assert blocks[0][0] == "read_file"
    assert blocks[1][0] == "finish"
    assert text.count("### [1] finish(") == 1
    paired = [m["tool_call_id"] for m in harness.payloads[1]["messages"] if m["role"] == "tool"]
    assert paired == ["c0", "c1"]
    if policy == "legacy":  # historical wire text; model-v1 preflight refuses the batch whole
        assert "unparseable arguments" in blocks[0][1]
        assert "this batch contained invalid tool arguments" in blocks[1][1]

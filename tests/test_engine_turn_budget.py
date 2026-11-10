"""Bounded turn scheduling and honest incomplete outcomes (turn-budget design).

Synthetic replies and temporary paths only; no network.
"""
from __future__ import annotations

import json
import subprocess

import pytest

from test_engine_reasoning import (  # noqa: F401
    RESPONSES_USAGE, _finish_reply, _list_reply, engine, harness)

GPT = "gpt-6-sol"


def _call(name, args, cid="c1", raw=None):
    return {"choices": [{"message": {"content": "", "tool_calls": [{
        "id": cid, "type": "function", "function": {
            "name": name, "arguments": raw if raw is not None else json.dumps(args)}}]},
        "finish_reason": "stop"}]}


def _prose(text="synthetic partial report", usage=None):
    resp = {"choices": [{"message": {"content": text}, "finish_reason": "stop"}]}
    if usage is not None:
        resp["usage"] = usage
    return resp


def _run(harness, *argv, replies, durations=None):
    try:
        harness.run(*argv, replies=replies, durations=durations)
    except SystemExit as exc:
        return exc.code
    return 0


def _end(tmp_path):
    rows = [json.loads(x) for x in (tmp_path / "index.jsonl").read_text().splitlines()]
    return [r for r in rows if r["event"] == "end"][-1]


def _usage(harness):
    path = harness.transcript_path.with_name("run.usage.jsonl")
    return [json.loads(x) for x in path.read_text().splitlines()]


def _notices(payload, kind):
    return sum(1 for m in payload["messages"] if m["role"] == "user"
               and isinstance(m["content"], str)
               and m["content"].startswith(f"[turn budget] {kind}"))


@pytest.mark.parametrize("max_iters", [1, 2, 3, 4, 5])
def test_total_model_turns_never_exceed_max(harness, tmp_path, capsys, max_iters):
    replies = [_list_reply("native")] * (max_iters - 1) + [_prose()]
    code = _run(harness, "-m", GPT, "--max-iters", str(max_iters), replies=replies)
    assert code == 3
    assert len(harness.payloads) == max_iters
    assert all("tools" in p for p in harness.payloads[:-1])
    assert "tools" not in harness.payloads[-1]
    end = _end(tmp_path)
    assert end["status"] == "failed" and end["stop_reason"] == "max_iters"
    out = capsys.readouterr().out
    assert out.startswith("[PARTIAL") and "max_iters" in out
    assert "synthetic partial report" in out


def test_default_schedule_27_work_2_closeout_1_report(harness, tmp_path):
    replies = [_list_reply("native")] * 29 + [_prose()]
    assert _run(harness, "-m", GPT, replies=replies) == 3
    p = harness.payloads
    assert len(p) == 30 and "tools" in p[28] and "tools" not in p[29]
    assert _notices(p[23], "warning") == 0 and _notices(p[24], "warning") == 1
    assert _notices(p[26], "closeout") == 0 and _notices(p[27], "closeout") == 1
    for kind in ("warning", "closeout", "final report"):
        assert _notices(p[29], kind) == 1
    assert _notices(p[29], "checkpoint") == 0


def test_explicit_checkpoint_preauthorizes_one_continuation(harness, tmp_path):
    replies = [_list_reply("native")] * 39 + [_prose()]
    code = _run(harness, "-m", GPT, "--max-iters", "40", "--soft-iters", "27",
                replies=replies)
    assert code == 3
    p = harness.payloads
    assert len(p) == 40 and "tools" in p[38] and "tools" not in p[39]
    assert _notices(p[26], "checkpoint") == 0 and _notices(p[27], "checkpoint") == 1
    assert _notices(p[36], "closeout") == 0 and _notices(p[37], "closeout") == 1
    assert _notices(p[39], "checkpoint") == 1 and _notices(p[39], "warning") == 1
    assert _end(tmp_path)["stop_reason"] == "max_iters"


def test_early_finish_skips_reserves_and_completes(harness, tmp_path):
    code = _run(harness, "-m", GPT, replies=[_list_reply("native"), _finish_reply("native")])
    assert code == 0 and len(harness.payloads) == 2
    end = _end(tmp_path)
    assert end["status"] == "complete" and "stop_reason" not in end
    assert [(r["iter"], r["phase"]) for r in _usage(harness)] == [(1, "loop"), (2, "loop")]


@pytest.mark.parametrize("argv", [
    ["--max-iters", "0"], ["--max-iters", "-2"], ["--soft-iters", "0"],
    ["--soft-iters", "28"], ["--max-iters", "1", "--soft-iters", "1"],
    ["--max-iters", "40", "--soft-iters", "38"], ["--oneshot", "--soft-iters", "3"],
])
def test_invalid_budget_flags_rejected_before_config(harness, engine, tmp_path,
                                                     monkeypatch, argv):
    def boom():
        raise AssertionError("config must not be read")
    monkeypatch.setattr(engine, "load_cfg", boom)
    assert _run(harness, "-m", GPT, *argv, replies=[]) == 2
    assert not (tmp_path / "index.jsonl").exists() and harness.payloads == []


def test_soft_iters_accepted_at_available_work_limit(harness, tmp_path):
    code = _run(harness, "-m", GPT, "--soft-iters", "27", replies=[_finish_reply("native")])
    assert code == 0 and _end(tmp_path)["status"] == "complete"


def _cut():
    return _prose("Error: ")


def test_repair_allowance_is_two_per_run_and_report_survives(harness, tmp_path, capsys):
    # Prose-only replies: two get a correction; the third failure stops repair.
    replies = [_prose("talk"), _list_reply("native"), _prose("talk"),
               _list_reply("native"), _prose("talk"), _prose("synthetic report")]
    assert _run(harness, "-m", GPT, replies=replies) == 3
    assert len(harness.payloads) == 6 and "tools" not in harness.payloads[-1]
    end = _end(tmp_path)
    assert end["status"] == "failed" and end["stop_reason"] == "repair_limit"
    out = capsys.readouterr().out
    assert out.startswith("[PARTIAL") and "repair_limit" in out


def test_cut_corrections_share_the_run_allowance(harness, tmp_path):
    replies = [_cut(), _cut(), _list_reply("native"), _cut(), _prose("synthetic report")]
    assert _run(harness, "-m", GPT, replies=replies, durations=[121, 121, 1, 121, 1]) == 3
    assert len(harness.payloads) == 5
    assert _end(tmp_path)["stop_reason"] == "repair_limit"


def test_invalid_native_arguments_count_as_protocol_failure(harness, tmp_path):
    bad = _call("write_file", None, raw="{not json")
    replies = [bad, bad, bad, _prose("synthetic report")]
    assert _run(harness, "-m", GPT, replies=replies) == 3
    assert len(harness.payloads) == 4
    assert _end(tmp_path)["stop_reason"] == "repair_limit"
    assert not (tmp_path / "agent.patch").exists()


def test_malformed_prompt_block_counts_as_protocol_failure(harness, tmp_path):
    bad2 = _prose('```tool {not json} ```')
    replies = [bad2, bad2, bad2, _prose("synthetic report")]
    code = _run(harness, "-m", "anthropic_tool_call.claude-x", "--tool-mode", "prompt",
                "--allow-prompt-tools", replies=replies)
    assert code == 3 and len(harness.payloads) == 4
    assert _end(tmp_path)["stop_reason"] == "repair_limit"


def test_no_action_limit_when_repairs_remain_unused(harness, tmp_path):
    # Transport failures are retried inside call_model, not repaired: three
    # consecutive turns without a tool action end with no_action_limit.
    dead = {"_transient": "synthetic transport failure"}
    replies = [dead] * 12 + [_prose("synthetic report")]
    assert _run(harness, "-m", GPT, replies=replies) == 3
    end = _end(tmp_path)
    assert end["status"] == "failed" and end["stop_reason"] == "no_action_limit"


def test_blank_report_still_records_reason_and_local_fallback(harness, tmp_path, capsys):
    replies = [_list_reply("native"), _prose("Error: ")] + [_prose("")] * 3
    assert _run(harness, "-m", GPT, "--max-iters", "2", replies=replies) == 3
    assert len(harness.payloads) <= 2 + 3  # call_model's internal retries only
    end = _end(tmp_path)
    assert end["status"] == "failed" and end["stop_reason"] == "max_iters"
    out = capsys.readouterr().out
    assert out.startswith("[PARTIAL") and "max_iters" in out
    tail = harness.transcript_path.read_text(encoding="utf-8").splitlines()[-1]
    assert tail.startswith(f"[{GPT} ") and "tool calls" in tail


def test_rejected_report_still_records_reason(harness, tmp_path):
    fatal = {"_fatal": True, "_status": 400, "_error_detail": "synthetic rejection"}
    assert _run(harness, "-m", GPT, "--max-iters", "2",
                replies=[_list_reply("native"), fatal]) == 3
    assert _end(tmp_path)["stop_reason"] == "max_iters"


def test_partial_usage_journal_once_per_response_inside_cap(harness, tmp_path, capsys):
    replies = [_list_reply("native", RESPONSES_USAGE), _prose("rep", RESPONSES_USAGE)]
    assert _run(harness, "-m", GPT, "--max-iters", "2", replies=replies) == 3
    assert [(r["iter"], r["phase"]) for r in _usage(harness)] == [
        (1, "loop"), (2, "wrapup")]  # design schema: phase is loop|wrapup
    assert "200 tok sent / 40 tok returned" in capsys.readouterr().err
    assert _end(tmp_path)["sent"] == 200


@pytest.mark.parametrize("discarded", ["", "Error:"])
def test_discarded_report_response_usage_is_journalled_once(harness, tmp_path, discarded):
    replies = [_list_reply("native", RESPONSES_USAGE), _prose(discarded, RESPONSES_USAGE),
               _prose("synthetic partial", RESPONSES_USAGE)]
    assert _run(harness, "-m", GPT, "--max-iters", "2", replies=replies) == 3
    assert [(r["iter"], r["phase"]) for r in _usage(harness)] == [
        (1, "loop"), (2, "wrapup"), (2, "wrapup")]
    assert _end(tmp_path)["sent"] == 300
    assert _end(tmp_path)["recv"] == 60


def test_usage_survives_fatal_retry_in_reserved_report(harness, tmp_path):
    fatal = {"_fatal": True, "_status": 400, "_error_detail": "synthetic rejection"}
    replies = [_list_reply("native", RESPONSES_USAGE), _prose("", RESPONSES_USAGE), fatal]
    assert _run(harness, "-m", GPT, "--max-iters", "2", replies=replies) == 3
    assert _end(tmp_path)["stop_reason"] == "max_iters"
    assert _end(tmp_path)["sent"] == 200
    assert len(_usage(harness)) == 2


def test_usage_survives_fatal_retry_during_ordinary_work(harness, tmp_path, engine, monkeypatch):
    callbacks = []
    monkeypatch.setattr(engine.atexit, "register", callbacks.append)
    fatal = {"_fatal": True, "_status": 400, "_error_detail": "synthetic rejection"}
    with pytest.raises(SystemExit):
        harness.run("-m", GPT, replies=[_prose("Error:", RESPONSES_USAGE), fatal])
    # Preserve the existing fatal-work path's interpreter-exit lifecycle guard.
    for callback in callbacks:
        callback()
    assert _end(tmp_path)["status"] == "interrupted"
    assert "stop_reason" not in _end(tmp_path)
    assert _end(tmp_path)["sent"] == 100
    assert len(_usage(harness)) == 1


@pytest.mark.parametrize("fatal", [False, True])
def test_nonstream_oneshot_retains_retry_usage_on_success_and_failure(harness, tmp_path, fatal):
    last = ({"_fatal": True, "_status": 400, "_error_detail": "synthetic rejection"}
            if fatal else _prose("synthetic answer", RESPONSES_USAGE))
    assert _run(harness, "-m", GPT, "--oneshot", "--no-stream",
                replies=[_prose("", RESPONSES_USAGE), last]) == (1 if fatal else 0)
    assert _end(tmp_path)["sent"] == (100 if fatal else 200)
    assert len(_usage(harness)) == (1 if fatal else 2)


def test_report_turn_cannot_execute_tools(harness, tmp_path, engine, monkeypatch):
    ran = []
    monkeypatch.setattr(engine.Runner, "dispatch", lambda self, n, a: ran.append(n) or "x")
    report = _prose("synthetic report")
    report["choices"][0]["message"]["tool_calls"] = _call("write_file", {
        "path": "x.txt", "content": "y"})["choices"][0]["message"]["tool_calls"]
    assert _run(harness, "-m", GPT, "--max-iters", "1", replies=[report]) == 3
    assert ran == [] and "tools" not in harness.payloads[0]
    assert not (tmp_path / "x.txt").exists()


def _timeout_run(monkeypatch, engine):
    def fake_run(*a, **k):
        raise subprocess.TimeoutExpired(cmd="synthetic", timeout=1)
    monkeypatch.setattr(engine.subprocess, "run", fake_run)
    # model-v1 context (the default) runs commands through bounded capture.
    monkeypatch.setattr(engine.capability_module("command_output"), "capture_command", fake_run)
    monkeypatch.setattr(engine.shutil, "which", lambda n: n)


def test_uncertain_command_stops_dispatch_without_replay(harness, tmp_path, engine,
                                                         monkeypatch, capsys):
    _timeout_run(monkeypatch, engine)
    ran = []
    real = engine.Runner.dispatch
    monkeypatch.setattr(engine.Runner, "dispatch",
                        lambda self, n, a: ran.append(n) or real(self, n, a))
    batch = _call("run_command", {"argv": ["python", "-m", "py_compile", "x.py"]})
    batch["choices"][0]["message"]["tool_calls"].append(
        _call("list_dir", {"path": "."}, cid="c2")["choices"][0]["message"]["tool_calls"][0])
    replies = [batch, _prose("synthetic report")]
    code = _run(harness, "-m", GPT, replies=replies)
    results = [m["content"] for m in harness.payloads[-1]["messages"] if m["role"] == "tool"]
    assert results[0].startswith("error: timed out after"), results
    assert code == 3
    assert ran == ["run_command"]
    assert len(harness.payloads) == 2 and "tools" not in harness.payloads[-1]
    end = _end(tmp_path)
    assert end["status"] == "failed" and end["stop_reason"] == "mutation_outcome_unknown"
    assert "mutation_outcome_unknown" in capsys.readouterr().out


def test_tool_error_text_is_not_uncertainty(harness, tmp_path, engine, monkeypatch):
    # Arbitrary tool output that mentions timeouts is not a control signal.
    monkeypatch.setattr(engine.Runner, "dispatch",
                        lambda self, n, a: "error: timed out after 600s; outcome unknown")
    replies = [_list_reply("native"), _finish_reply("native")]
    assert _run(harness, "-m", GPT, replies=replies) == 0
    assert _end(tmp_path)["status"] == "complete"


@pytest.mark.parametrize("status,uncertain", [
    ("timeout", True), ("error", True), ("rpc_error", False), ("tool_error", False)])
def test_mcp_end_event_latches_only_lost_outcomes(engine, tmp_path, status, uncertain):
    caps = engine.Capabilities(str(tmp_path))
    caps._on_event({"phase": "end", "status": status, "server": "s", "tool": "t"})
    assert caps.outcome_unknown is uncertain


def test_blank_finish_is_not_complete(harness, tmp_path):
    blank = _call("finish", {"summary": "  "})
    assert _run(harness, "-m", GPT, replies=[blank]) != 0
    assert _end(tmp_path)["status"] == "failed"


@pytest.mark.parametrize("failure_turn", [2, 3])
def test_protocol_repairs_cannot_consume_closeout_reserves(harness, tmp_path, failure_turn):
    # Five total turns provide two work turns, two closeout turns, one report.
    replies = [_list_reply("native")] * (failure_turn - 1)
    replies += [_prose("synthetic no action"), _prose("synthetic partial")]
    assert _run(harness, "-m", GPT, "--max-iters", "5", replies=replies) == 3
    assert len(harness.payloads) == failure_turn + 1
    assert "tools" not in harness.payloads[-1]
    assert _end(tmp_path)["stop_reason"] == "max_iters"


@pytest.mark.parametrize("mode,invalid_args,name", [
    ("native", [], "list_dir"), ("native", None, "list_dir"),
    ("native", {"summary": 7}, "finish"), ("prompt", [], "list_dir"),
    ("prompt", {"summary": 7}, "finish"),
])
def test_invalid_argument_shapes_use_bounded_protocol_repair(harness, tmp_path,
                                                             mode, invalid_args, name):
    bad = (_call(name, invalid_args) if mode == "native" else
           _prose('```tool ' + json.dumps({"name": name, "args": invalid_args}) + ' ```'))
    args = ["--tool-mode", mode] + (["--allow-prompt-tools"] if mode == "prompt" else [])
    model = GPT if mode == "native" else "anthropic_tool_call.claude-x"
    assert _run(harness, "-m", model, *args,
                replies=[bad, bad, bad, _prose("synthetic partial")]) == 3
    assert len(harness.payloads) == 4
    assert _end(tmp_path)["stop_reason"] == "repair_limit"


def test_report_exception_keeps_primary_reason_and_partial_exit(harness, tmp_path,
                                                               engine, monkeypatch):
    real = engine.call_model

    def model(url, key, payload, mode, *args, **kwargs):
        if mode == "oneshot":
            raise RuntimeError("SYNTHETIC_PRIVATE_EXCEPTION")
        return real(url, key, payload, mode, *args, **kwargs)

    monkeypatch.setattr(engine, "call_model", model)
    assert _run(harness, "-m", GPT, "--max-iters", "2",
                replies=[_list_reply("native")]) == 3
    assert _end(tmp_path)["stop_reason"] == "max_iters"
    assert "SYNTHETIC_PRIVATE_EXCEPTION" not in harness.transcript_path.read_text()


@pytest.mark.parametrize("mode", ["native", "prompt"])
def test_reserved_report_overrides_tool_only_system_instructions(harness, tmp_path, mode):
    flags = ["--tool-mode", mode] + (["--allow-prompt-tools"] if mode == "prompt" else [])
    model = GPT if mode == "native" else "anthropic_tool_call.claude-x"
    assert _run(harness, "-m", model, *flags, "--max-iters", "1",
                replies=[_prose("synthetic partial")]) == 3
    system = harness.payloads[-1]["messages"][0]["content"]
    assert "FINAL REPORT MODE" in system
    assert "do not call finish or emit a tool block" in system


def test_mcp_discovery_error_is_not_an_uncertain_mutation(engine, tmp_path):
    caps = engine.Capabilities(str(tmp_path))
    caps._on_event({"phase": "end", "status": "timeout", "server": "s",
                    "tool": "initialize+tools/list"})
    assert caps.outcome_unknown is False


def test_partial_patch_write_failure_keeps_terminal_reason(harness, tmp_path, capsys):
    patch = tmp_path / "missing-parent" / "partial.patch"
    replies = [_call("write_file", {"path": "x.txt", "content": "synthetic"}),
               _prose("synthetic partial")]
    assert _run(harness, "-m", GPT, "--max-iters", "2", "--patch", str(patch),
                replies=replies) == 3
    assert _end(tmp_path)["status"] == "failed"
    assert _end(tmp_path)["stop_reason"] == "max_iters"
    assert not patch.exists()
    assert "not have been saved" in capsys.readouterr().err


def test_partial_broken_stdout_keeps_terminal_reason(harness, tmp_path, engine, monkeypatch):
    import builtins
    real_print = builtins.print

    def broken_stdout(*args, **kwargs):
        if not kwargs.get("file"):
            raise BrokenPipeError("synthetic closed pipe")
        return real_print(*args, **kwargs)

    monkeypatch.setattr(engine, "print", broken_stdout, raising=False)
    assert _run(harness, "-m", GPT, "--max-iters", "1",
                replies=[_prose("synthetic partial")]) == 3
    assert _end(tmp_path)["status"] == "failed"
    assert _end(tmp_path)["stop_reason"] == "max_iters"


@pytest.mark.parametrize("marker", ["**run incomplete (", "_[turn budget] final report_"])
def test_early_partial_transcript_failure_preserves_reason(
        harness, tmp_path, engine, monkeypatch, marker):
    real_write = engine.Transcript.write

    def failed_write(self, text):
        if marker in text:
            raise OSError("synthetic disk failure")
        return real_write(self, text)

    monkeypatch.setattr(engine.Transcript, "write", failed_write)
    assert _run(harness, "-m", GPT, "--max-iters", "1",
                replies=[_prose("synthetic partial")]) == 3
    assert _end(tmp_path)["status"] == "failed"
    assert _end(tmp_path)["stop_reason"] == "max_iters"


def test_malformed_native_batch_cannot_complete_via_finish(harness, tmp_path):
    batch = _call("write_file", {}, raw="{")
    batch["choices"][0]["message"]["tool_calls"] += _finish_reply("native")["choices"][0]["message"]["tool_calls"]
    assert _run(harness, "-m", GPT,
                replies=[batch, batch, batch, _prose("synthetic partial")]) == 3
    assert _end(tmp_path)["stop_reason"] == "repair_limit"
    assert len(harness.payloads) == 4


def test_claude_whole_batch_refusal_uses_shared_repair_allowance(harness, tmp_path):
    batch = _list_reply("native")
    batch["choices"][0]["message"]["tool_calls"] += _call("list_dir", {}, cid="c2")["choices"][0]["message"]["tool_calls"]
    assert _run(harness, "-m", "anthropic_tool_call.claude-x",
                replies=[batch, batch, batch, _prose("synthetic partial")]) == 3
    assert _end(tmp_path)["stop_reason"] == "repair_limit"
    assert len(harness.payloads) == 4


@pytest.mark.parametrize("error", [
    UnicodeDecodeError("utf-8", b"\xff", 0, 1, "synthetic output"),
    OSError("synthetic communication failure"),
])
def test_command_communication_failure_blocks_later_native_actions(
        harness, tmp_path, engine, monkeypatch, error):
    calls = []

    def failed_command(*args, **kwargs):
        calls.append(args)
        raise error

    monkeypatch.setattr(engine.subprocess, "run", failed_command)
    # model-v1 context (the default) runs commands through bounded capture.
    monkeypatch.setattr(engine.capability_module("command_output"),
                        "capture_command", failed_command)
    monkeypatch.setattr(engine.shutil, "which", lambda name: name)
    command = _call("run_command", {"argv": ["python", "-m", "py_compile", "x.py"]})
    finish = _finish_reply("native")["choices"][0]["message"]["tool_calls"][0]
    command["choices"][0]["message"]["tool_calls"].append(dict(finish, id="c2"))
    assert _run(harness, "-m", GPT,
                replies=[command, _prose("synthetic partial")]) == 3
    assert len(calls) == 1
    assert _end(tmp_path)["stop_reason"] == "mutation_outcome_unknown"


def test_lost_mcp_outcome_stops_native_batch_and_finishes_incomplete(harness, tmp_path,
                                                                   engine, monkeypatch):
    calls = []

    def lose_outcome(self, name, args):
        calls.append(name)
        self._on_event({"phase": "end", "status": "timeout", "server": "s", "tool": "change"})
        raise TimeoutError("synthetic timeout")

    monkeypatch.setattr(engine.Capabilities, "call_mcp", lose_outcome)
    # Advertise the schema so strict native preflight admits the call.
    monkeypatch.setattr(engine.Capabilities, "mcp_native", lambda self: [{
        "type": "function", "function": {
            "name": "mcp__s__change", "description": "synthetic",
            "parameters": {"type": "object", "properties": {}}}}])
    batch = _call("mcp__s__change", {})
    batch["choices"][0]["message"]["tool_calls"] += (
        _call("mcp__s__change", {}, cid="c2")["choices"][0]["message"]["tool_calls"])
    assert _run(harness, "-m", GPT, replies=[batch, _prose("synthetic partial")]) == 3
    assert calls == ["mcp__s__change"]
    assert len(harness.payloads) == 2
    assert _end(tmp_path)["stop_reason"] == "mutation_outcome_unknown"

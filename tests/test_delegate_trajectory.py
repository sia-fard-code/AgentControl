"""Trajectory detection and opt-in advising; synthetic responses and temporary state only."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from test_engine_reasoning import RESPONSES_USAGE, engine, harness  # noqa: F401
from test_engine_turn_budget import GPT, _call, _finish_reply, _prose, _run, _usage
from test_delegate_verification import _batch, _completion, _isolated_skills  # noqa: F401


def test_repeated_read_file_records_suspicion_in_completion_json_without_steering(harness, tmp_path):
    (tmp_path / "x.txt").write_text("synthetic unchanged contents\n", encoding="utf-8")
    assert _run(harness, "-m", GPT, replies=[
        _call("read_file", {"path": "x.txt"}) for _ in range(3)
    ] + [_finish_reply("native")]) == 0
    suspicions = _completion(harness)["suspicions"]
    assert suspicions == [{"code": "repeated_action_observation", "count": 3,
                           "first_action_ordinal": 1, "last_action_ordinal": 3,
                           "state_epoch": 0}]
    assert len(harness.payloads) == 4
    for i, payload in enumerate(harness.payloads):
        assert len(payload["messages"]) == 2 + 2 * i
        assert [m["content"] for m in payload["messages"] if m["role"] == "user"] == [
            "do the thing"]
    assert not Path(str(harness.transcript_path) + ".steer").exists()


@pytest.mark.parametrize("reset_after_read", [False, True])
def test_repeated_replace_failures_reset_after_read_and_emit_without_read(
        harness, tmp_path, reset_after_read):
    (tmp_path / "x.txt").write_text("synthetic base\n", encoding="utf-8")
    failures = [_call("replace_in_file", {"path": "x.txt", "old_string": f"missing {i}",
                                        "new_string": "synthetic replacement"})
                for i in range(5)]
    replies = (failures[:2] + [_call("read_file", {"path": "x.txt"})] + failures[2:]
               if reset_after_read else failures[:3])
    assert _run(harness, "-m", GPT, replies=replies + [_finish_reply("native")]) == 0
    assert _completion(harness)["suspicions"] == [{
        "code": "replace_failures_without_read", "count": 3,
        "first_action_ordinal": 4 if reset_after_read else 1,
        "last_action_ordinal": 6 if reset_after_read else 3, "state_epoch": 0}]
    assert not Path(str(harness.transcript_path) + ".steer").exists()


@pytest.mark.parametrize("scenario", ["refused", "NO_TOOL_ACTION", "PARSE_ERROR"])
def test_repeated_command_refusal_and_protocol_failure_exclusion(harness, scenario):
    if scenario == "refused":
        flags = []
        replies = [_call("run_command", {"argv": ["synthetic-disallowed-command"]})] * 3
        replies += [_finish_reply("native")]
        code = 0
    else:
        flags = (["--tool-mode", "prompt", "--allow-prompt-tools"]
                 if scenario == "PARSE_ERROR" else [])
        text = "```tool {not json} ```" if scenario == "PARSE_ERROR" else "synthetic narration"
        replies = [_prose(text)] * 3 + [_prose("synthetic partial report")]
        code = 3
    assert _run(harness, "-m", GPT, *flags, replies=replies) == code
    suspicions = _completion(harness)["suspicions"]
    if scenario == "refused":
        assert "repeated_command_refusal" in [s["code"] for s in suspicions]
    else:
        assert suspicions == []
        assert scenario in harness.transcript_path.read_text(encoding="utf-8")
    assert not Path(str(harness.transcript_path) + ".steer").exists()


def _advisor_reply(usage=None):
    return _prose(json.dumps({"status": "advise", "action_code": "inspect_before_repeating"}),
                  usage=usage)


def _advisor_payloads(harness):
    return [p for p in harness.payloads
            if "trajectory advisor" in p["messages"][0]["content"]]


@pytest.mark.parametrize("mode", ["native", "prompt"])
def test_supervise_stuck_consults_without_tools_and_injects_only_advisor_notice(
        harness, tmp_path, engine, monkeypatch, mode):
    (tmp_path / "x.txt").write_text("synthetic unchanged contents\n", encoding="utf-8")
    real_open = open

    def guarded_open(path, mode="r", *args, **kwargs):
        if str(path).endswith(".steer"):
            assert not any(c in mode for c in "wax+"), "advisor must not write steer files"
        return real_open(path, mode, *args, **kwargs)

    monkeypatch.setattr(engine, "open", guarded_open, raising=False)
    flags = [] if mode == "native" else ["--tool-mode", "prompt", "--allow-prompt-tools"]
    read = (_call("read_file", {"path": "x.txt"}) if mode == "native" else
            _prose('```tool {"name": "read_file", "args": {"path": "x.txt"}}```'))
    assert _run(harness, "-m", GPT, "--supervise-stuck", *flags, replies=[read] * 3 + [
        _advisor_reply(RESPONSES_USAGE), _finish_reply(mode)]) == 0
    advisor, = _advisor_payloads(harness)
    assert len(harness.payloads) == 5 and advisor == harness.payloads[3]
    assert "tools" not in advisor and "tool_choice" not in advisor
    assert advisor["stream"] is False and advisor["reasoning"] == {"effort": "high"}
    data = json.loads(advisor["messages"][1]["content"])
    record = _completion(harness)
    assert data["run_id"] == record["run_id"]
    assert data["turn"] == 3 and data["suspicion"] == record["suspicions"][0]
    assert data["state_epoch"] == data["candidate_seq"] == data["mutation_seq"] == 0
    assert data["patch_sha256"] == record["patch_binding"]["patch_sha256"]
    assert record["advisories"] == [{
        "status": "advise", "action_code": "inspect_before_repeating",
        "suspicion_code": "repeated_action_observation", "turn": 3, "state_epoch": 0,
        "applicability": "current", "unavailable_reason": None}]
    notices = [m["content"] for m in harness.payloads[-1]["messages"]
               if m["role"] == "user"
               and m["content"].startswith("[advisor suggestion — non-authoritative]")]
    assert len(notices) == 1 and "not proof of test results" in notices[0]
    assert "[operator guidance]" not in json.dumps(harness.payloads)
    transcript = harness.transcript_path.read_text(encoding="utf-8")
    assert "[advisor suggestion] inspect_before_repeating" in transcript
    assert "[operator guidance]" not in transcript
    assert list(tmp_path.rglob("*.steer")) == []
    assert record["verification_status"] == "no_mutations"
    usage = _usage(harness)
    assert [r["phase"] for r in usage] == ["loop", "loop", "loop", "advisor", "loop"]
    assert usage[3]["iter"] == 3 and usage[3]["sent"] == 100 and usage[3]["raw"]["input_tokens"] == 100


def test_supervise_stuck_cooldown_and_cap(harness, tmp_path, engine):
    traj = engine.capability_module("trajectory")
    assert traj.ADVISOR_COOLDOWN_TURNS == 3 and traj.ADVISOR_MAX_INTERVENTIONS == 2
    paths = ["first.txt", "cooldown.txt", "second.txt", "also-cooldown.txt", "capped.txt"]
    for path in paths:
        (tmp_path / path).write_text("synthetic unchanged contents\n", encoding="utf-8")

    def repeat(path):
        return _batch(*[("read_file", {"path": path})] * 3)

    assert _run(harness, "-m", GPT, "--supervise-stuck", replies=[
        repeat(paths[0]), _advisor_reply(), repeat(paths[1]), _call("list_dir", {"path": "."}),
        repeat(paths[2]), _advisor_reply(), repeat(paths[3]), _call("list_dir", {"path": "."}),
        repeat(paths[4]), _finish_reply("native")]) == 0
    advisors = _advisor_payloads(harness)
    assert [json.loads(p["messages"][1]["content"])["turn"] for p in advisors] == [1, 4]
    record = _completion(harness)
    assert len(record["suspicions"]) == 5
    assert [a["turn"] for a in record["advisories"]] == [1, 4]
    assert len(harness.payloads) == 10


def test_supervise_stuck_ignores_suspicion_invalidated_in_same_native_batch(harness, tmp_path):
    (tmp_path / "x.txt").write_text("synthetic unchanged contents\n", encoding="utf-8")
    assert _run(harness, "-m", GPT, "--supervise-stuck", replies=[_batch(
        *[("read_file", {"path": "x.txt"})] * 3,
        ("write_file", {"path": "new.txt", "content": "synthetic changed state\n"})),
        _finish_reply("native")]) == 0
    record = _completion(harness)
    assert record["suspicions"] == [{
        "code": "repeated_action_observation", "count": 3,
        "first_action_ordinal": 1, "last_action_ordinal": 3, "state_epoch": 0}]
    assert record["patch_binding"]["mutation_seq"] == 1
    assert record["advisories"] == [] and _advisor_payloads(harness) == []
    assert len(harness.payloads) == 2


@pytest.mark.parametrize("kind", ["tool_calls", "function_call", "tool_block"])
def test_supervise_stuck_never_executes_unexpected_advisor_tools(
        harness, tmp_path, engine, monkeypatch, kind):
    (tmp_path / "x.txt").write_text("synthetic unchanged contents\n", encoding="utf-8")
    args = {"path": "advisor.txt", "content": "synthetic forbidden write"}
    if kind == "tool_calls":
        reply = _call("write_file", args)
    elif kind == "function_call":
        reply = _advisor_reply()
        reply["choices"][0]["message"]["function_call"] = {
            "name": "write_file", "arguments": json.dumps(args)}
    else:
        reply = _prose('```tool ' + json.dumps({"name": "write_file", "args": args}) + '```')
    dispatched = []
    dispatch = engine.Runner.dispatch

    def observe_dispatch(self, name, args):
        dispatched.append(name)
        return dispatch(self, name, args)

    monkeypatch.setattr(engine.Runner, "dispatch", observe_dispatch)
    assert _run(harness, "-m", GPT, "--supervise-stuck", replies=[
        _call("read_file", {"path": "x.txt"})] * 3 + [reply, _finish_reply("native")]) == 0
    assert dispatched == ["read_file"] * 3
    assert not (tmp_path / "advisor.txt").exists()
    assert not (tmp_path / "agent.patch").exists()
    result, = _completion(harness)["advisories"]
    assert result["status"] == "abstain" and result["action_code"] is None
    assert result["unavailable_reason"] == "unexpected_tool_call"
    assert result["applicability"] == "current"
    assert "[advisor suggestion — non-authoritative]" not in json.dumps(
        harness.payloads[-1]["messages"])


@pytest.mark.parametrize(("max_iters", "advisor_calls"), [(4, 0), (5, 1), (6, 1)])
def test_supervise_stuck_shares_budget_and_preserves_reserved_report(
        harness, tmp_path, max_iters, advisor_calls):
    (tmp_path / "x.txt").write_text("synthetic unchanged contents\n", encoding="utf-8")
    replies = [_call("read_file", {"path": "x.txt"})] * 3
    if advisor_calls:
        replies.append(_advisor_reply())
    if max_iters == 6:
        replies.append(_call("list_dir", {"path": "."}))
    replies.append(_prose("synthetic partial report"))
    assert _run(harness, "-m", GPT, "--supervise-stuck", "--max-iters", str(max_iters),
                replies=replies) == 3
    assert len(harness.payloads) == max_iters
    assert len(_advisor_payloads(harness)) == advisor_calls
    assert "tools" not in harness.payloads[-1] and "tool_choice" not in harness.payloads[-1]
    assert len(_completion(harness)["advisories"]) == advisor_calls
    assert _usage(harness)[-1]["phase"] == "wrapup"
    assert _usage(harness)[-1]["iter"] == max_iters


def test_supervise_stuck_records_advice_as_stale_after_mutation(harness, tmp_path):
    (tmp_path / "x.txt").write_text("synthetic unchanged contents\n", encoding="utf-8")
    assert _run(harness, "-m", GPT, "--supervise-stuck", replies=[
        _batch(*[("read_file", {"path": "x.txt"})] * 3), _advisor_reply(), _batch(
            ("write_file", {"path": "new.txt", "content": "synthetic change\n"}),
            ("finish", {"summary": "synthetic finished"}))]) == 0
    record = _completion(harness)
    assert record["advisories"][0]["applicability"] == "stale"
    assert record["advisories"][0]["state_epoch"] == 0
    assert record["verification_status"] == "staged_unverified_on_disk"


def test_supervise_stuck_rejects_oneshot_before_config(
        harness, tmp_path, engine, monkeypatch, capsys):
    monkeypatch.setattr(engine, "load_cfg", lambda: pytest.fail("config must not be read"))
    assert _run(harness, "-m", GPT, "--oneshot", "--supervise-stuck", replies=[]) == 2
    assert "--supervise-stuck" in capsys.readouterr().err
    assert not (tmp_path / "index.jsonl").exists() and harness.payloads == []

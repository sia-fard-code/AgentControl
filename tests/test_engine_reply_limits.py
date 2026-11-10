"""Reply-limit handling: cut replies and prose-only replies (reply-limits design).

Synthetic replies only; the harness fakes the gateway and the clock.
"""
from __future__ import annotations

import json

import pytest

from test_engine_reasoning import _finish_reply, _list_reply, engine, harness  # noqa: F401

CLAUDE = "anthropic_tool_call.claude-x"


def _content_reply(content):
    return {"choices": [{"message": {"role": "assistant", "content": content},
                         "finish_reason": "stop"}]}


def _anomalies(harness):
    path = harness.transcript_path.with_suffix(".anomalies.jsonl")
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()]


@pytest.mark.parametrize("resp, mode, elapsed, cause", [
    (_content_reply("Error: "), "native", 121.0, "reply_cut"),
    (_content_reply("Error:"), "oneshot", 100.0, "reply_cut"),
    (_content_reply("Error: "), "native", 5.0, "empty"),
    (_content_reply(""), "native", 130.0, "empty"),
    (_content_reply("I will read the file."), "native", 20.0, "prose_only"),
    (_content_reply("I will read the file."), "prompt", 20.0, "no_tool_block"),
    ({"_transient": "read timeout after 600s"}, "native", 600.0, "timeout"),
    ({"_transient": "HTTP 502"}, "native", 1.0, "transport"),
    ({"_transient": ""}, "native", 1.0, "transport"),
])
def test_classify_failure(engine, resp, mode, elapsed, cause):
    assert engine.classify_failure(resp, mode, elapsed) == cause


def test_cut_reply_gets_one_correction_not_a_resend(engine, harness):
    text = harness.run("-m", CLAUDE,
                       replies=[_content_reply("Error: "), _finish_reply("native")],
                       durations=[121, 3])
    assert len(harness.payloads) == 2
    msgs = harness.payloads[1]["messages"]
    assert msgs[-2] == {"role": "assistant", "content": engine.CUT_PLACEHOLDER}
    assert msgs[-1] == {"role": "user", "content": engine.CUT_CORRECTION}
    assert ("_[ 1] reply cut by the gateway's 2-minute limit after 121 s; "
            "asking for a smaller step_") in text


def test_quick_bare_error_is_resent_unchanged(harness):
    harness.run("-m", CLAUDE,
                replies=[_content_reply("Error: "), _finish_reply("native")],
                durations=[5, 3])
    assert len(harness.payloads) == 2
    assert harness.payloads[0] == harness.payloads[1]


def test_third_consecutive_cut_exhausts_the_run_repair_allowance(harness):
    # Turn-budget design: two corrections per run, then repair_limit and the
    # reserved tool-less report turn (no further correction or nudge).
    with pytest.raises(SystemExit) as caught:
        harness.run("-m", CLAUDE,
                    replies=[_content_reply("Error: ")] * 3 + [_content_reply("report")],
                    durations=[121, 121, 121, 3])
    assert caught.value.code == 3
    assert len(harness.payloads) == 4 and "tools" not in harness.payloads[-1]
    text = harness.transcript_path.read_text(encoding="utf-8")
    assert text.count("reply cut by the gateway's 2-minute limit") == 2
    assert "NO_TOOL_ACTION" not in text and "repair_limit" in text


def test_cut_allowance_does_not_reset_after_progress(harness):
    with pytest.raises(SystemExit) as caught:
        harness.run("-m", CLAUDE,
                    replies=[_content_reply("Error: "), _content_reply("Error: "),
                             _list_reply("native"), _content_reply("Error: "),
                             _content_reply("report")],
                    durations=[121, 121, 3, 121, 3])
    assert caught.value.code == 3
    assert len(harness.payloads) == 5
    text = harness.transcript_path.read_text(encoding="utf-8")
    assert text.count("reply cut by the gateway's 2-minute limit") == 2
    assert "repair_limit" in text


@pytest.mark.parametrize("mode, flags", [
    ("native", ["--tool-mode", "native"]),
    ("prompt", ["--tool-mode", "prompt", "--allow-prompt-tools"]),
])
def test_prose_reply_is_kept_and_nudged_without_resend(harness, mode, flags):
    prose = "I will now look at the file."
    text = harness.run("-m", CLAUDE, *flags,
                       replies=[_content_reply(prose), _finish_reply(mode)],
                       durations=[20, 3])
    assert len(harness.payloads) == 2
    msgs = harness.payloads[1]["messages"]
    assert msgs[-2] == {"role": "assistant", "content": prose}
    assert msgs[-1]["role"] == "user"
    assert msgs[-1]["content"].startswith("You called no tool")
    assert "_[ 1] reply had no tool call; asking for one_" in text


def test_anomaly_records_cause_and_elapsed(harness):
    harness.run("-m", CLAUDE,
                replies=[_content_reply("Error: "), _finish_reply("native")],
                durations=[121.04, 3])
    rec = _anomalies(harness)[0]
    assert rec["cause"] == "reply_cut"
    assert rec["elapsed_s"] == 121.0
    # Anomaly artifacts contain cause/counters, never another provider-body copy.
    assert rec["content"] is None and rec["response"] is None
    assert rec["finish_reason"] == "stop"


TIMEOUT = {"_transient": "read timeout after 600s"}


def test_timeout_at_max_retries_once_at_xhigh(harness):
    text = harness.run("-m", "gpt-6.1-sol", "-e", "max",
                       replies=[TIMEOUT, _finish_reply("native")], durations=[600, 5])
    assert len(harness.payloads) == 2
    assert harness.payloads[0]["reasoning"] == {"effort": "max"}
    assert harness.payloads[1]["reasoning"] == {"effort": "xhigh"}
    assert "_[ 1] no reply within 600 s at max; retrying at xhigh_" in text


def test_timeout_at_high_retries_once_unchanged_then_fails_the_turn(harness):
    text = harness.run("-m", "gpt-6-sol", "-e", "high",
                       replies=[TIMEOUT, TIMEOUT, _finish_reply("native")],
                       durations=[600, 600, 5])
    assert len(harness.payloads) == 3
    assert harness.payloads[0] == harness.payloads[1]
    assert "NO_TOOL_ACTION" in text
    assert "retrying at xhigh" not in text


def test_claude_system_prompt_states_the_two_minute_rule(engine, harness):
    harness.run("-m", CLAUDE, replies=[_finish_reply("native")])
    system = harness.payloads[0]["messages"][0]["content"]
    assert system.endswith(engine.CLAUDE_REPLY_RULE)


def test_gpt_system_prompt_has_no_two_minute_rule(engine, harness):
    harness.run("-m", "gpt-6-sol", replies=[_finish_reply("native")])
    system = harness.payloads[0]["messages"][0]["content"]
    assert engine.CLAUDE_REPLY_RULE not in system


@pytest.mark.parametrize("mode", ["native", "prompt"])
def test_claude_first_request_ends_with_small_step_guidance(harness, mode):
    compatibility = ["--allow-prompt-tools"] if mode == "prompt" else []
    harness.run("-m", CLAUDE, "--tool-mode", mode, *compatibility,
                replies=[_finish_reply(mode)])
    messages = harness.payloads[0]["messages"]
    assert messages[-2] == {"role": "user", "content": "do the thing"}
    assert messages[-1] == {
        "role": "user",
        "content": (
            "Execute only the next tiny step now. "
            "Do not plan or solve the whole task in this reply. "
            "Produce one tool action with at most 40 lines of generated text. "
            "Continue the remaining work in later replies."
        ),
    }


@pytest.mark.parametrize("model", ["gpt-6.1-sol", "qwen3.6:latest"])
def test_other_models_do_not_receive_claude_small_step_guidance(harness, model):
    harness.run("-m", model, "--tool-mode", "native",
                replies=[_finish_reply("native")])
    messages = harness.payloads[0]["messages"]
    assert len(messages) == 2
    assert messages[-1] == {"role": "user", "content": "do the thing"}


def test_claude_small_step_guidance_is_not_repeated_on_later_turns(harness):
    harness.run("-m", CLAUDE,
                replies=[_list_reply("native"), _finish_reply("native")])
    first = harness.payloads[0]["messages"]
    second = harness.payloads[1]["messages"]
    assert len(first) == 3
    assert second[:len(first)] == first
    assert [message["role"] for message in second[len(first):]] == ["assistant", "tool"]


def test_claude_oneshot_does_not_receive_agent_step_guidance(harness):
    harness.run("--oneshot", "-m", CLAUDE, replies=[_content_reply("done")])
    assert harness.payloads[0]["messages"] == [{"role": "user", "content": "do the thing"}]


def test_oneshot_cut_retries_once_with_shorter_answer_line(engine, harness, capsys):
    # qwen3.6:latest takes the non-streaming one-shot path before and after
    # Tasks 3-4, so this test keeps covering that path.
    text = harness.run("--oneshot", "-m", "qwen3.6:latest",
                       replies=[_content_reply("Error: "), _content_reply("short answer")],
                       durations=[121, 30])
    assert len(harness.payloads) == 2
    assert harness.payloads[1]["messages"][0]["content"] == (
        "do the thing" + engine.ONESHOT_SHORTER)
    assert capsys.readouterr().out == "short answer"
    assert ("_[ 1] reply cut by the gateway's 2-minute limit after 121 s; "
            "asking for a shorter answer_") in text

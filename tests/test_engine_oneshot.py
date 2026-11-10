"""Offline tests for delegate.py --oneshot (one request, no tools, registered)."""
from __future__ import annotations

import json

import pytest

from test_engine_reasoning import RESPONSES_USAGE, engine, harness  # noqa: F401


def _answer(content, usage=None):
    resp = {"choices": [{"message": {"role": "assistant", "content": content},
                         "finish_reason": "stop"}]}
    if usage is not None:
        resp["usage"] = usage
    return resp


def _stream(answer, usage=None):
    evs = [{"type": "response.output_text.delta", "delta": answer}]
    evs.append({"type": "response.completed", "response": {"usage": usage}})
    return [evs]


def _records(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()]


def _oneshot(harness, *argv, replies, stream_replies=None):
    return harness.run("--oneshot", *argv, replies=replies, stream_replies=stream_replies)


# ---- 1. payload ------------------------------------------------------------

@pytest.mark.parametrize("context_policy", [None, "legacy"], ids=["default-model-v1", "legacy"])
def test_non_streaming_payload_has_no_tools_and_nested_effort(harness, context_policy) -> None:
    policy_args = [] if context_policy is None else ["--context-policy", context_policy]
    _oneshot(harness, *policy_args, "-m", "qwen3.6:latest", "-e", "max",
             replies=[_answer("hi")])
    assert len(harness.payloads) == 1
    p = harness.payloads[0]
    expected = {"model": "qwen3.6:latest",
                "messages": [{"role": "user", "content": "do the thing"}],
                "stream": False, "reasoning": {"effort": "max"}}
    if context_policy is None:
        expected["max_completion_tokens"] = 32768
    assert p == expected


def test_claude_payload_flat_effort_and_temperature(harness) -> None:
    stream = [[{"choices": [{"delta": {"content": "hi"}}]}, {"_done": True}]]
    _oneshot(harness, "-m", "anthropic_tool_call.claude-x", "-e", "high",
             "--temperature", "0.3", replies=[], stream_replies=stream)
    path, p = harness.stream_calls[0]
    assert path == "/api/chat/completions"
    assert p["reasoning_effort"] == "high"
    assert "reasoning" not in p
    assert p["temperature"] == 0.3 and p["stream"] is True
    assert "tools" not in p and "tool_choice" not in p
    assert [m["role"] for m in p["messages"]] == ["user"]


def test_effort_substitution_applies(harness, capsys) -> None:
    _oneshot(harness, "-m", "gpt-6.1-sol", "-e", "none", replies=[],
             stream_replies=_stream("hi"))
    assert harness.stream_calls[0][1]["reasoning"] == {"effort": "low", "summary": "auto"}
    assert "temperature" not in harness.stream_calls[0][1]


# ---- 2. attachments ----------------------------------------------------------

def test_attachments_appended_in_order(harness, tmp_path) -> None:
    (tmp_path / "a.txt").write_text("alpha", encoding="utf-8")
    (tmp_path / "b.txt").write_bytes(b"be\xfft")
    _oneshot(harness, "-m", "gpt-6.1-sol", "--attach", "b.txt", "--attach", "a.txt",
             replies=[], stream_replies=_stream("ok"))
    content = harness.stream_calls[0][1]["input"]
    b_text = b"be\xfft".decode("utf-8", errors="replace")
    assert content == ("do the thing"
                       f"\n\n===== FILE: b.txt =====\n{b_text}"
                       "\n\n===== FILE: a.txt =====\nalpha")


def test_attachment_cap_exits_2_without_request(harness, tmp_path, capsys) -> None:
    (tmp_path / "small.txt").write_text("x" * 10, encoding="utf-8")
    (tmp_path / "big.txt").write_text("y" * 600_000, encoding="utf-8")
    with pytest.raises(SystemExit) as ei:
        _oneshot(harness, "--context-policy", "legacy", "--attach", "small.txt",
                 "--attach", "big.txt", replies=[_answer("never")])
    assert ei.value.code == 2
    assert harness.payloads == []
    assert ("error: input exceeds 600,000 chars at big.txt. Pass fewer files, "
            "or split the request.") in capsys.readouterr().err
    assert not (tmp_path / "index.jsonl").exists()


def test_missing_attachment_exits_2(harness, tmp_path, capsys) -> None:
    with pytest.raises(SystemExit) as ei:
        _oneshot(harness, "--attach", "nope.txt", replies=[_answer("never")])
    assert ei.value.code == 2
    assert harness.payloads == []
    assert "error: cannot read nope.txt" in capsys.readouterr().err


# ---- 3. output -----------------------------------------------------------------

def test_stdout_is_exact_content_and_token_line(harness, capsys) -> None:
    answer = "  line one\n\nline two  "
    _oneshot(harness, "-m", "gpt-6.1-sol", replies=[],
             stream_replies=_stream(answer, RESPONSES_USAGE))
    out, err = capsys.readouterr()
    assert out == answer
    assert err.endswith("\n[sent 100 tok to gpt-6.1-sol; returned 20 tok]\n")


def test_token_line_unknown_usage(harness, capsys) -> None:
    _oneshot(harness, "-m", "gpt-6.1-sol", replies=[], stream_replies=_stream("x"))
    assert "[sent ? tok to gpt-6.1-sol; returned ? tok]" in capsys.readouterr().err


# ---- 4. registration -------------------------------------------------------------

def test_registered_like_a_normal_run(harness, engine, tmp_path, capsys) -> None:
    from agent_control.narration import parse_narration

    (tmp_path / "a.txt").write_text("SECRET-ATTACHED-CONTENT", encoding="utf-8")
    answer = "The answer.\n\nSecond paragraph."
    text = _oneshot(harness, "-m", "gpt-6.1-sol", "-e", "max", "--label", "ask",
                    "--attach", "a.txt", replies=[],
                    stream_replies=_stream(answer, RESPONSES_USAGE))
    for idx in (tmp_path / "index.jsonl",
                tmp_path / ".claude" / "agent-runs" / "agent-runs-index.jsonl"):
        recs = _records(idx)
        assert [r["event"] for r in recs] == ["start", "end"]
        start, end = recs
        assert start["tool_mode"] == "oneshot"
        assert start["write_mode"] == "none"
        assert start["max_iters"] == 1
        assert start["label"] == "ask" and start["effort"] == "max"
        assert start["harness_version"] == engine.HARNESS_VERSION
        assert end["status"] == "complete"
        assert end["tool_calls"] == 0 and end["refused"] == 0
        assert end["sent"] == 100 and end["recv"] == 20
    usage = _records(harness.transcript_path.with_suffix(".usage.jsonl"))
    assert len(usage) == 1 and usage[0]["sent"] == 100 and usage[0]["recv"] == 20
    assert "SECRET-ATTACHED-CONTENT" not in text
    assert "a.txt" in text
    narration = parse_narration(text)
    finals = [e for e in narration.events if e.kind == "final"]
    assert len(finals) == 1 and finals[0].data["text"] == answer
    assert narration.brief.startswith("do the thing")
    assert narration.header.tool_mode == "oneshot"
    assert text.rstrip().endswith("100 tok sent / 20 tok returned]")


# ---- 5. gateway failure -------------------------------------------------------------

def test_gateway_400_fails_and_records_end(harness, tmp_path, capsys) -> None:
    fatal = {"_transient": "HTTP 400", "_fatal": True, "_status": 400,
             "_error_detail": "Unsupported parameter"}
    with pytest.raises(SystemExit) as ei:
        _oneshot(harness, "-m", "qwen3.6:latest", replies=[fatal])
    assert ei.value.code == 1
    assert len(harness.payloads) == 1
    assert "Unsupported parameter" in capsys.readouterr().err
    ends = [r for r in _records(tmp_path / "index.jsonl") if r["event"] == "end"]
    assert len(ends) == 1 and ends[0]["status"] == "failed"
    repo = tmp_path / ".claude" / "agent-runs" / "agent-runs-index.jsonl"
    assert [r["status"] for r in _records(repo) if r["event"] == "end"] == ["failed"]


def test_transient_error_is_retried(harness, capsys) -> None:
    _oneshot(harness, "-m", "qwen3.6:latest",
             replies=[{"_transient": "HTTP 502"}, _answer("Error:"), _answer("fine")])
    assert len(harness.payloads) == 3
    assert capsys.readouterr().out == "fine"


def test_model_not_found_is_retried(harness, capsys) -> None:
    _oneshot(harness, "-m", "qwen3.6:latest",
             replies=[{"_transient": "HTTP 400 model not found",
                       "_model_not_found": True}, _answer("fine")])
    assert len(harness.payloads) == 2
    assert capsys.readouterr().out == "fine"


# ---- 6. incompatible options ----------------------------------------------------------

@pytest.mark.parametrize("extra", [
    ["--write"], ["--worktree"], ["--patch", "x.patch"], ["--max-iters", "30"],
    ["--tool-mode", "native"], ["--tool-mode", "prompt"],
])
def test_incompatible_options_rejected(harness, capsys, extra) -> None:
    with pytest.raises(SystemExit) as ei:
        _oneshot(harness, *extra, replies=[_answer("never")])
    assert ei.value.code == 2
    assert harness.payloads == []
    assert ("error: --oneshot sends one request without tools; drop "
            "--write/--worktree/--patch/--max-iters/--tool-mode") in capsys.readouterr().err


def test_tool_mode_auto_is_accepted(harness, capsys) -> None:
    _oneshot(harness, "--tool-mode", "auto", "-m", "qwen3.6:latest", replies=[_answer("ok")])
    assert capsys.readouterr().out == "ok"


def test_normal_run_defaults_unchanged(harness, tmp_path) -> None:
    from test_engine_reasoning import _finish_reply

    harness.run("-m", "gpt-6-sol", replies=[_finish_reply("native")])
    start = _records(tmp_path / "index.jsonl")[0]
    assert start["max_iters"] == 30 and start["tool_mode"] == "native"

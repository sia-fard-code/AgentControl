"""Engine behaviour against the upgraded gateway (Responses-style GPT models).

Everything here uses fakes (chat / urlopen / load_cfg / refresh_models) and
tmp_path; nothing reaches a real gateway.
"""
from __future__ import annotations

import importlib.util
import io
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

import pytest

ENGINE_PATH = Path(__file__).resolve().parents[1] / "engine" / "delegate.py"

RESPONSES_USAGE = {
    "input_tokens": 100,
    "input_tokens_details": {"cached_tokens": 60, "cache_write_tokens": 7, "junk": "x"},
    "output_tokens": 20,
    "output_tokens_details": {"reasoning_tokens": 12, "secret": "leak"},
    "total_tokens": 120,
}


def _load_engine():
    spec = importlib.util.spec_from_file_location("agentcontrol_engine", ENGINE_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def engine():
    return _load_engine()


def _finish_reply(mode, usage=None):
    if mode == "native":
        msg = {"content": "", "tool_calls": [{
            "id": "c1", "type": "function",
            "function": {"name": "finish",
                         "arguments": json.dumps({"summary": "done"})}}]}
    else:
        msg = {"content": '```tool {"name": "finish", "args": {"summary": "done"}}```'}
    resp = {"choices": [{"message": msg, "finish_reason": "stop"}]}
    if usage is not None:
        resp["usage"] = usage
    return resp


def _list_reply(mode, usage=None):
    if mode == "native":
        msg = {"content": "", "tool_calls": [{
            "id": "c1", "type": "function",
            "function": {"name": "list_dir", "arguments": json.dumps({"path": "."})}}]}
    else:
        msg = {"content": '```tool {"name": "list_dir", "args": {"path": "."}}```'}
    resp = {"choices": [{"message": msg, "finish_reason": "stop"}]}
    if usage is not None:
        resp["usage"] = usage
    return resp


@pytest.fixture()
def harness(engine, tmp_path, monkeypatch):
    """Run engine.main() fully offline; returns a runner that records payloads."""
    monkeypatch.setenv("CLAUDE_AGENT_RUN_INDEX", str(tmp_path / "index.jsonl"))
    monkeypatch.setattr(engine, "load_cfg", lambda: ("http://gw", "sekret-key"))
    monkeypatch.setattr(engine, "refresh_models", lambda url, key: None)
    # Keep the lifecycle from installing process-wide hooks inside pytest.
    monkeypatch.setattr(engine.atexit, "register", lambda *a, **k: None)
    monkeypatch.setattr(engine.signal, "signal", lambda *a, **k: None)
    monkeypatch.setattr(engine.time, "sleep", lambda s: None)
    clock = {"t": 1000.0}
    monkeypatch.setattr(engine, "_now", lambda: clock["t"])
    monkeypatch.chdir(tmp_path)
    transcript = tmp_path / ".claude" / "agent-runs" / "run.md"

    class H:
        payloads: list = []
        stream_calls: list = []
        transcript_path = transcript

        def run(self, *argv, replies, durations=None, stream_replies=None):
            replies = list(replies)
            durations = list(durations or [])

            def fake_chat(url, key, payload):
                self.payloads.append(json.loads(json.dumps(payload)))
                if durations:
                    clock["t"] += durations.pop(0)
                return replies.pop(0)

            monkeypatch.setattr(engine, "chat", fake_chat)
            stream_scripts = list(stream_replies or [])

            def fake_stream(url, key, path, payload, idle_timeout=None):
                # Scripts contain events, fake-clock advances, callbacks or errors.
                # A bare exception raises before the first event.
                self.stream_calls.append((path, json.loads(json.dumps(payload))))
                script = stream_scripts.pop(0)
                if isinstance(script, BaseException):
                    raise script
                for item in script:
                    if isinstance(item, BaseException):
                        raise item
                    if callable(item):
                        item()
                    elif isinstance(item, (int, float)) and not isinstance(item, bool):
                        clock["t"] += item
                    else:
                        yield item

            monkeypatch.setattr(engine, "stream_events", fake_stream)
            if stream_replies is None:
                argv = ("--no-stream", *argv)   # tests without scripts keep today's path
            monkeypatch.setattr(sys, "argv", [
                "delegate.py", "-t", "do the thing", "-C", str(tmp_path),
                "--transcript", str(transcript), *argv])
            engine.main()
            return transcript.read_text(encoding="utf-8")

    h = H()
    h.payloads = []
    h.stream_calls = []
    return h


# ---- effort in the request -------------------------------------------------

def test_gpt_payload_uses_nested_reasoning(harness, capsys) -> None:
    harness.run("-m", "gpt-6-sol", "-e", "high", replies=[_finish_reply("native")])
    payload = harness.payloads[0]
    assert payload["reasoning"] == {"effort": "high"}
    assert "reasoning_effort" not in payload


def test_claude_payload_keeps_flat_reasoning_effort(harness) -> None:
    harness.run("-m", "anthropic_tool_call.claude-x", "-e", "high",
                replies=[_finish_reply("native")])
    payload = harness.payloads[0]
    assert payload["reasoning_effort"] == "high"
    assert "reasoning" not in payload
    assert payload["tools"] and payload["tool_choice"] == "auto"


def test_gpt_61_none_is_substituted_with_note(harness, capsys) -> None:
    text = harness.run("-m", "gpt-6.1-sol", "-e", "none",
                       replies=[_finish_reply("native")])
    assert harness.payloads[0]["reasoning"] == {"effort": "low"}
    assert "reasoning_effort" not in harness.payloads[0]
    err = capsys.readouterr().err
    assert "note: gpt-6.1-sol does not accept effort 'none'; using 'low'" in err
    assert "effort: `low`" in text


def test_gpt_none_is_sent_explicitly_for_models_that_accept_it(harness) -> None:
    harness.run("-m", "gpt-6-sol", "-e", "none", replies=[_finish_reply("native")])
    assert harness.payloads[0]["reasoning"] == {"effort": "none"}


def test_native_gpt_keeps_effort(harness, capsys) -> None:
    text = harness.run("-m", "gpt-6-sol", "-e", "high", "--tool-mode", "native",
                       replies=[_finish_reply("native")])
    payload = harness.payloads[0]
    assert payload["reasoning"] == {"effort": "high"}
    assert payload["tools"] and payload["tool_choice"] == "auto"
    err = capsys.readouterr().err
    assert "forcing effort=none" not in err
    assert "effort: `high`" in text


def test_wrapup_payload_uses_nested_reasoning(harness, capsys) -> None:
    # --max-iters is a hard total: 2 = one work turn + the reserved report turn.
    with pytest.raises(SystemExit) as caught:
        harness.run("-m", "gpt-6-sol", "-e", "xhigh", "--max-iters", "2",
                    replies=[_list_reply("native", RESPONSES_USAGE),
                             {"choices": [{"message": {"content": "partial"}}],
                              "usage": RESPONSES_USAGE}])
    assert caught.value.code == 3
    assert len(harness.payloads) == 2
    for payload in harness.payloads:
        assert payload["reasoning"] == {"effort": "xhigh"}
        assert "reasoning_effort" not in payload
    err = capsys.readouterr().err
    assert "200 tok sent / 40 tok returned" in err


@pytest.mark.parametrize("model", [
    "gpt-6-sol", "gpt-6.1-sol", "gpt-5.6-sol",
    "anthropic_tool_call.claude-opus-5-5-think",
])
def test_auto_tool_mode_native(engine, model) -> None:
    assert engine.auto_tool_mode(model) == "native"


@pytest.mark.parametrize("model", [
    "anthropic_pipe.claude-sonnet-4-6", "qwen3.6:latest",
    "gpt54-opine",  # no hyphen after "gpt": not a gpt-* model
])
def test_auto_tool_mode_prompt(engine, model) -> None:
    assert engine.auto_tool_mode(model) == "prompt"


def test_auto_gpt_run_sends_native_tools_with_nested_effort(harness) -> None:
    harness.run("-m", "gpt-6-sol", "-e", "high", replies=[_finish_reply("native")])
    payload = harness.payloads[0]
    assert payload["tools"] and payload["tool_choice"] == "auto"
    assert payload["reasoning"] == {"effort": "high"}
    assert "reasoning_effort" not in payload


@pytest.mark.parametrize("model", [
    "anthropic_tool_call.claude-opus-5-5-think", "gpt-6.1-sol",
])
def test_prompt_override_for_native_model_is_rejected_before_config(
    harness, engine, monkeypatch, capsys, tmp_path, model,
):
    monkeypatch.setattr(engine, "load_cfg", lambda: pytest.fail("gateway config was read"))
    with pytest.raises(SystemExit) as error:
        harness.run("-m", model, "--tool-mode", "prompt",
                    replies=[_finish_reply("prompt")])
    assert error.value.code == 2
    message = capsys.readouterr().err
    assert "supports native tools" in message
    assert "--tool-mode native" in message
    assert "Do not add" in message
    assert not harness.transcript_path.exists()
    assert not (tmp_path / "index.jsonl").exists()


@pytest.mark.parametrize("model", [
    "anthropic_tool_call.claude-opus-5-5-think", "gpt-6.1-sol",
])
def test_explicit_prompt_compatibility_opt_in_is_preserved(harness, model):
    harness.run("-m", model, "--tool-mode", "prompt", "--allow-prompt-tools",
                replies=[_finish_reply("prompt")])
    payload = harness.payloads[0]
    assert "tools" not in payload
    assert "```tool" in payload["messages"][0]["content"]


@pytest.mark.parametrize("mode", ["auto", "prompt"])
def test_models_without_native_default_keep_prompt_tools(harness, mode):
    harness.run("-m", "qwen3.6:latest", "--tool-mode", mode,
                replies=[_finish_reply("prompt")])
    assert "tools" not in harness.payloads[0]


@pytest.mark.parametrize("mode", ["auto", "native"])
def test_prompt_compatibility_opt_in_requires_prompt_mode(harness, capsys, mode):
    with pytest.raises(SystemExit) as error:
        harness.run("-m", "gpt-6.1-sol", "--tool-mode", mode, "--allow-prompt-tools",
                    replies=[])
    assert error.value.code == 2
    assert "needs --tool-mode prompt" in capsys.readouterr().err


def test_oneshot_rejects_prompt_compatibility_opt_in(harness, capsys):
    with pytest.raises(SystemExit) as error:
        harness.run("--oneshot", "--allow-prompt-tools", replies=[])
    assert error.value.code == 2
    assert "one request without tools" in capsys.readouterr().err


# ---- usage -------------------------------------------------------------------

def test_normalize_responses_usage(engine) -> None:
    n = engine.normalize_usage(RESPONSES_USAGE)
    assert n == {"sent": 100, "recv": 20, "cache_read": 60, "cache_write": 7}


def test_normalize_chat_usage_unchanged(engine) -> None:
    n = engine.normalize_usage({"prompt_tokens": 10, "completion_tokens": 2,
                                "prompt_tokens_details": {"cached_tokens": 4}})
    assert n == {"sent": 10, "recv": 2, "cache_read": 4, "cache_write": None}


def test_sanitize_keeps_responses_details(engine) -> None:
    cleaned = engine.sanitize_usage(RESPONSES_USAGE)
    assert cleaned == {
        "input_tokens": 100,
        "input_tokens_details": {"cached_tokens": 60, "cache_write_tokens": 7},
        "output_tokens": 20,
        "output_tokens_details": {"reasoning_tokens": 12},
        "total_tokens": 120,
    }


def test_sanitize_details_use_their_own_allowlists(engine) -> None:
    cleaned = engine.sanitize_usage({
        "input_tokens": 1,
        "input_tokens_details": {"reasoning_tokens": 3},
        "output_tokens_details": {"cached_tokens": 5, "reasoning_tokens": True},
    })
    assert cleaned == {"input_tokens": 1}


def test_footer_totals_on_responses_usage(harness, tmp_path, capsys) -> None:
    text = harness.run("-m", "gpt-6-sol", "-e", "high",
                       replies=[_finish_reply("native", RESPONSES_USAGE)])
    err = capsys.readouterr().err
    assert "100 tok sent / 20 tok returned" in err
    assert "100 tok sent / 20 tok returned" in text
    ends = [json.loads(line) for line in
            (tmp_path / "index.jsonl").read_text(encoding="utf-8").splitlines()
            if json.loads(line).get("event") == "end"]
    assert ends[-1]["sent"] == 100 and ends[-1]["recv"] == 20


def test_footer_totals_on_chat_usage(harness, capsys) -> None:
    harness.run("-m", "anthropic_tool_call.claude-x", "-e", "high",
                replies=[_finish_reply("native", {"prompt_tokens": 9,
                                                  "completion_tokens": 3})])
    assert "9 tok sent / 3 tok returned" in capsys.readouterr().err


def test_responses_usage_row_is_readable_by_agentcontrol(engine, tmp_path) -> None:
    from agent_control.usage import read_usage_journal

    journal = tmp_path / "a.usage.jsonl"
    engine.append_usage_record(str(journal), iteration=1, phase="loop",
                               model="gpt-6-sol",
                               timestamp="2026-09-17T12:00:00+00:00",
                               raw_usage=RESPONSES_USAGE)
    evidence = read_usage_journal(journal)
    assert evidence.totals.tokens_sent == 100, evidence.diagnostics
    assert evidence.totals.tokens_returned == 20


# ---- HTTP 400 handling -------------------------------------------------------

def _http_error(code, body):
    return urllib.error.HTTPError(url="http://gw/api/chat/completions", code=code,
                                  msg="err", hdrs=None, fp=io.BytesIO(body))


def test_400_unsupported_value_stops_run_once(harness, engine, monkeypatch) -> None:
    detail = ("Unsupported value: 'none' is not supported with this model. "
              "Supported values are: 'low', 'medium', 'high', 'xhigh', and 'max'.")
    calls = []

    def fake_urlopen(req, timeout=None):
        calls.append(req)
        raise _http_error(400, json.dumps({"detail": detail}).encode())

    def no_sleep(_):
        raise AssertionError("a fatal 400 must not be retried")

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(engine.time, "sleep", no_sleep)
    monkeypatch.setattr(sys, "argv", [
        "delegate.py", "-t", "TASK-TEXT-MUST-NOT-LEAK", "-C", str(harness.transcript_path.parents[2]),
        "--transcript", str(harness.transcript_path), "--no-stream",
        "-m", "gpt-6.1-sol", "-e", "high"])
    with pytest.raises(SystemExit) as ei:
        engine.main()
    message = str(ei.value.code)
    assert len(calls) == 1
    assert "HTTP 400" in message
    assert detail in message
    assert "TASK-TEXT-MUST-NOT-LEAK" not in message
    assert "sekret-key" not in message


def test_400_error_message_field_is_used_and_truncated(engine, monkeypatch) -> None:
    long_msg = "Unsupported parameter: " + "x" * 1000

    def fake_urlopen(req, timeout=None):
        raise _http_error(400, json.dumps({"error": {"message": long_msg}}).encode())

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    resp = engine.chat("http://gw", "k", {"model": "m", "messages": [{"content": "PROMPT"}]})
    assert resp.get("_fatal") is True
    assert "_model_not_found" not in resp
    assert len(resp["_error_detail"]) <= 300
    assert resp["_error_detail"].startswith("Unsupported parameter: ")
    with pytest.raises(SystemExit) as ei:
        engine.chat_with_model_retry("http://gw", "k", {"model": "m"}, 1,
                                     sleep=lambda _: pytest.fail("no retry"))
    message = str(ei.value.code)
    assert "HTTP 400" in message and "Unsupported parameter: " in message
    assert "PROMPT" not in message


def test_400_detail_redacts_the_key(engine, monkeypatch) -> None:
    def fake_urlopen(req, timeout=None):
        raise _http_error(400, json.dumps({"detail": "bad key sekret-key here"}).encode())

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    resp = engine.chat("http://gw", "sekret-key", {"model": "m"})
    assert "sekret-key" not in resp["_error_detail"]


def test_400_without_json_detail_has_safe_placeholder(engine, monkeypatch) -> None:
    def fake_urlopen(req, timeout=None):
        raise _http_error(400, b"<html>echoed PROMPT body</html>")

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    resp = engine.chat("http://gw", "k", {"model": "m"})
    assert resp.get("_fatal") is True
    assert "PROMPT" not in resp["_error_detail"]


def test_model_not_found_retry_still_works_via_urlopen(engine, monkeypatch) -> None:
    good = {"choices": [{"message": {"content": "ok"}}]}
    seq = [_http_error(400, b'{"detail":"Model not found"}'), None]

    class _Resp:
        def read(self):
            return json.dumps(good).encode()

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_urlopen(req, timeout=None):
        item = seq.pop(0)
        if item is not None:
            raise item
        return _Resp()

    refreshed = []
    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(engine, "refresh_models", lambda u, k: refreshed.append(1))
    out = engine.chat_with_model_retry("http://gw", "k", {"model": "m"}, 1,
                                       sleep=lambda _: None)
    assert out == good
    assert refreshed == [1]


def test_5xx_is_still_transient_not_fatal(engine, monkeypatch) -> None:
    def fake_urlopen(req, timeout=None):
        raise _http_error(503, b'{"detail":"busy"}')

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    resp = engine.chat("http://gw", "k", {"model": "m"})
    assert resp == {"_transient": "HTTP 503"}

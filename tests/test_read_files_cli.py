"""Cold opt-in and native request integration for read_files. Synthetic only."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from test_engine_reasoning import _finish_reply, engine, harness  # noqa: F401
from test_engine_turn_budget import GPT, _call, _run

ON = ("--read-files", "--profile", "resilient-v1", "--context-policy", "model-v1")
CLAUDE = "anthropic_tool_call.claude-x"


@pytest.fixture(autouse=True)
def isolated(engine, monkeypatch, tmp_path):
    monkeypatch.syspath_prepend(str(Path(engine.__file__).parent))
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.delenv("_HARNESS_APPROVED_WORKER", raising=False)


def _trap(engine, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("touched before the read_files guard")
    for name in ("_read_engine_settings", "load_cfg", "refresh_models", "chat"):
        monkeypatch.setattr(engine, name, boom)


REJECT = [
    ("--read-files",),
    ("--read-files", "--profile", "resilient-v1"),
    ("--read-files", "--context-policy", "model-v1"),
    ("--read-files", "--profile", "legacy", "--context-policy", "model-v1"),
    ("--read-files", "--profile", "resilient-v1", "--context-policy", "legacy"),
    (*ON, "--oneshot"),
    (*ON, "--supervisor-manifest", "C:/x.json", "--supervisor-manifest-sha256", "0" * 64),
    (*ON, "--recovery-contract", "C:/r.json"),
    (*ON, "--tool-mode", "prompt"),
    (*ON, "--tool-mode", "prompt", "--allow-prompt-tools"),
    (*ON, "--supervisor-manifest-sha256", "0" * 64),
    (*ON, "--show-profile"),
    (*ON, "--supervisor-capabilities"),
    (*ON, "--supervisor-capabilities-v2"),
    (*ON, "--version"),
]


@pytest.mark.parametrize("argv", REJECT)
def test_cold_rejection_before_any_effect(engine, harness, monkeypatch, tmp_path, capsys,
                                          argv) -> None:
    _trap(engine, monkeypatch)
    assert _run(harness, *argv, "-m", GPT, replies=[]) == 2
    assert "read_files" in capsys.readouterr().err
    assert not (tmp_path / "index.jsonl").exists()
    assert harness.payloads == []


def test_approved_worker_rejected(engine, harness, monkeypatch, tmp_path, capsys) -> None:
    monkeypatch.setenv("_HARNESS_APPROVED_WORKER", "1")
    _trap(engine, monkeypatch)
    assert _run(harness, *ON, "-m", GPT, replies=[]) == 2
    assert not (tmp_path / "index.jsonl").exists()


def _tools(payload):
    return [t["function"]["name"] if "function" in t else t["name"] for t in payload["tools"]]


@pytest.mark.parametrize("model", [GPT, CLAUDE])
def test_opt_in_batch_read_delivers_bodies(harness, tmp_path, model) -> None:
    (tmp_path / "a.txt").write_text("alpha\n", encoding="utf-8")
    (tmp_path / "b.txt").write_text("beta\n", encoding="utf-8")
    replies = [_call("read_files", {"requests": [{"path": "a.txt"}, {"path": "b.txt"}]}),
               _finish_reply("native")]
    assert _run(harness, *ON, "-m", model, replies=replies) == 0
    tools = _tools(harness.payloads[0])
    assert tools.index("read_files") == tools.index("read_file") + 1
    spec = [t for t in harness.payloads[0]["tools"] if t["function"]["name"] == "read_files"][0]
    assert spec["function"]["parameters"]["properties"]["requests"]["maxItems"] == 8
    msg = [m for m in harness.payloads[1]["messages"] if m["role"] == "tool"][-1]
    assert msg["tool_call_id"] == "c1"
    rows = json.loads(msg["content"])["results"]
    assert [(r["status"], r["content"]) for r in rows] == [("served", "alpha\n"), ("served", "beta\n")]


@pytest.mark.parametrize("bad", [{"start_line": True}, {"start_line": 5, "end_line": 2}],
                         ids=["bool", "reversed"])
def test_malformed_tail_blocks_whole_native_batch(harness, tmp_path, bad) -> None:
    two = {"choices": [{"message": {"content": "", "tool_calls": [
        {"id": "c1", "type": "function", "function": {
            "name": "write_file", "arguments": json.dumps({"path": "w.txt", "content": "x"})}},
        {"id": "c2", "type": "function", "function": {
            "name": "read_files", "arguments": json.dumps(
                {"requests": [{"path": "a.txt"}, {"path": "a.txt", **bad}]})}}]},
        "finish_reason": "stop"}]}
    (tmp_path / "a.txt").write_text("alpha\n", encoding="utf-8")
    _run(harness, *ON, "--write", "-m", GPT, replies=[two, _finish_reply("native")])
    assert not (tmp_path / "w.txt").exists()


def test_no_opt_in_request_unchanged(harness) -> None:
    base = ("--profile", "resilient-v1", "--context-policy", "model-v1", "-m", GPT)
    assert _run(harness, *base, replies=[_finish_reply("native")]) == 0
    plain = harness.payloads[0]
    assert "read_files" not in _tools(plain)
    assert "read_files" not in json.dumps(plain)


@pytest.mark.parametrize("model", [GPT, CLAUDE])
@pytest.mark.parametrize("context", ["legacy", "model-v1"])
def test_baseline_full_wire_fingerprint(harness, tmp_path, model, context):
    assert _run(harness, "--profile", "resilient-v1", "--context-policy", context,
                "-m", model, replies=[_finish_reply("native")]) == 0
    wire = json.dumps(harness.payloads[0], sort_keys=True)
    for root in (str(tmp_path), str(tmp_path).replace("\\", "/")):
        wire = wire.replace(json.dumps(root)[1:-1], "<ROOT>")
    digest = hashlib.sha256(wire.encode()).hexdigest()
    baseline = {
        ("legacy", GPT): "c8a38a9483f1423dcab42b08289ac171213b10310fad4df09ce55ba7fbe4d4db",
        ("legacy", CLAUDE): "37284e2a2cb6ced2cdca1bb38d5a981ab4f75b4a52324423369ed6503d248bcf",
        ("model-v1", GPT): "093289272ade7666be7c719e50680b012a0a3d8754cf4eb8f38768ce46a06cd8",
        ("model-v1", CLAUDE): "d6f83851bba8e47eb7543574e77bc9bf6f46945cf4172e451e29af4809084a99",
    }
    assert digest == baseline[context, model]


def test_disabled_registry_is_baseline(engine, tmp_path):
    run = engine.Runner(str(tmp_path), profile="resilient-v1")
    assert "read_files" not in run.BUILTIN_TOOLS
    assert "read_files" not in engine.Runner.BUILTIN_TOOLS


def test_responses_schema_pairing_and_preflight(harness, tmp_path):
    from test_engine_live_turns_gpt import gpt_turn, finish_turn
    (tmp_path / "a").write_text("synthetic\n", encoding="utf-8")
    req = {"requests": [{"path": "a"}]}
    harness.run(*ON, "-m", GPT, replies=[], stream_replies=[
        gpt_turn("read_files", req, summary=()), finish_turn()])
    first = harness.stream_calls[0][1]
    tool = next(t for t in first["tools"] if t["name"] == "read_files")
    assert tool["parameters"]["properties"]["requests"]["items"]["additionalProperties"] is False
    output = [m for m in harness.stream_calls[1][1]["input"]
              if m.get("type") == "function_call_output"]
    assert len(output) == 1 and output[0]["call_id"] == "call_1"
    assert json.loads(output[0]["output"])["results"][0]["content"] == "synthetic\n"
    malformed = gpt_turn("write_file", {"path": "must-not-exist", "content": "x"}, summary=(),
                         extra_calls=(("read_files", {"requests": [
                             {"path": "a"}, {"path": "a", "start_line": 3, "end_line": 1}]}, "tail"),))
    harness.run(*ON, "--write", "-m", GPT, replies=[], stream_replies=[malformed, finish_turn()])
    assert not (tmp_path / "must-not-exist").exists()


def test_opt_in_serialization_is_deterministic(harness) -> None:
    for _ in range(2):
        assert _run(harness, *ON, "-m", GPT, replies=[_finish_reply("native")]) == 0
    a, b = harness.payloads
    assert json.dumps(a["tools"]) == json.dumps(b["tools"])
    assert a["messages"][0] == b["messages"][0]

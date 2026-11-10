"""Offline prompt-mode context ownership; synthetic tasks and temporary HOME only."""
import json
from pathlib import Path

import pytest

from test_engine_reasoning import engine, harness  # noqa: F401
from test_engine_turn_budget import _run

QWEN = "qwen3.6:latest"


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch, engine):
    monkeypatch.syspath_prepend(str(Path(engine.__file__).parent))
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.delenv("_HARNESS_APPROVED_WORKER", raising=False)


def prompt_call(name, args):
    return {"choices": [{"message": {"content": "```tool " + json.dumps(
        {"name": name, "args": args}) + "```"}, "finish_reason": "stop"}]}


def test_large_prompt_default_uses_runtime_batch(harness, engine, monkeypatch):
    runtime = engine.capability_module("context_runtime").ContextRuntime
    original = runtime.begin_batch
    kinds = []

    def begin(self, *args, **kwargs):
        kinds.append(kwargs.get("kind"))
        return original(self, *args, **kwargs)

    monkeypatch.setattr(runtime, "begin_batch", begin)
    assert _run(harness, "-m", QWEN, "-t", "synthetic " * 80000,
                replies=[prompt_call("list_dir", {"path": "."}),
                         prompt_call("finish", {"summary": "done"})]) == 0
    assert kinds == ["prompt", "prompt"]
    assert len(harness.payloads) == 2
    assert len(json.dumps(harness.payloads[0])) > 600000
    rows = harness.payloads[1]["messages"]
    assert rows[-1]["role"] == "user"
    assert not any(m["role"] == "tool" for m in rows)
    assert "tools" not in harness.payloads[0]


def test_prompt_large_read_is_fully_recoverable_and_result_not_pinned(
        harness, engine, monkeypatch, tmp_path):
    text = "synthetic-body\n" * 2000
    (tmp_path / "large.txt").write_text(text, encoding="utf-8")
    runtime = engine.capability_module("context_runtime").ContextRuntime
    original = runtime.end_batch
    captured = []

    def end(self):
        result = original(self)
        captured.append(self)
        return result

    monkeypatch.setattr(runtime, "end_batch", end)
    assert _run(harness, "-m", QWEN, replies=[prompt_call("read_file", {"path": "large.txt"}),
                                            prompt_call("finish", {"summary": "done"})]) == 0
    rt = captured[0]
    snap = rt.snapshot_data()
    assert snap["groups"][0][-1] not in snap["pinned"]
    ref = rt.group_refs()[0][0]
    chunks, offset = [], 0
    while True:
        page = rt.read_result(ref, offset, 4096)
        assert page["content_complete"]
        chunks.append(page["data"])
        if page["eof"]:
            break
        offset = page["next_offset"]
    recovered = "".join(chunks)
    assert text.rstrip() in recovered
    assert len(recovered) > 16000
    journal = harness.transcript_path.with_name("run.messages.jsonl")
    rows = [json.loads(line) for line in journal.read_text().splitlines()]
    assert sum(m.get("content", "").startswith("Result of read_file:") for m in rows) == 1
    assert not any(m["role"] == "tool" for m in rows)


def test_prompt_native_override_still_needs_operator(harness, capsys):
    assert _run(harness, "-m", "gpt-6-sol", "--tool-mode", "prompt", replies=[]) == 2
    assert "--allow-prompt-tools" in capsys.readouterr().err
    assert not harness.payloads

"""Offline engine.main(): big command output -> ref -> read -> finish (synthetic)."""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pytest

from test_engine_reasoning import engine, harness, _finish_reply  # noqa: F401
from test_engine_turn_budget import _call, _run, _end, GPT

EMIT = ["python", "emit.py"]
SENTINEL = "RAWSENTINEL7Q"


def _setup(engine, tmp_path, monkeypatch, rc=0):
    monkeypatch.syspath_prepend(str(Path(engine.__file__).parent))
    (tmp_path / "emit.py").write_text(
        "import sys\nopen('count.txt', 'a').write('x')\n"
        f"sys.stdout.write('{SENTINEL}' * 8000)\nsys.exit({rc})\n", encoding="utf-8")
    policy_dir = tmp_path / ".claude"
    policy_dir.mkdir()
    (policy_dir / "agent-policy.json").write_text(
        json.dumps({"allowed_cmds": [EMIT]}), encoding="utf-8")
    monkeypatch.setattr(engine.shutil, "which", lambda name: sys.executable)


def _tool_results(harness):
    return [m["content"] for m in harness.payloads[-1]["messages"] if m["role"] == "tool"]


class _Reader(dict):
    """Reply that pages the first cmd:// ref seen in the previous request."""
    def __init__(self, harness):
        super().__init__()
        self.harness = harness
        self.response = None

    def _response(self):
        if self.response is None:
            results = [m["content"] for m in self.harness.payloads[-1]["messages"]
                       if m["role"] == "tool"]
            ref = re.findall(r'"ref": "(cmd://[^"]+)"', results[-1])[0]
            self.response = _call("read_command_output", {"ref": ref, "limit": 64}, cid="c2")
        return self.response

    def __getitem__(self, key):
        return self._response()[key]

    def get(self, key, default=None):
        return self._response().get(key, default)


@pytest.mark.parametrize("rc", [0, 3])
def test_main_big_output_ref_read_finish(harness, engine, tmp_path, monkeypatch, rc):
    _setup(engine, tmp_path, monkeypatch, rc)
    replies = [_call("run_command", {"argv": EMIT}), _Reader(harness),
               _finish_reply("native")]
    code = _run(harness, "-m", GPT, "--profile", "resilient-v1", "--write",
                replies=replies)
    results = _tool_results(harness)
    assert (tmp_path / "count.txt").read_text() == "x"      # ran exactly once
    assert results[0].startswith(f"exit={rc}\n") and len(results[0]) < 16 * 1024
    page = json.loads(results[1])
    assert page["data"] == (SENTINEL * 8000)[:64] and page["next_offset"] == 64
    end = _end(tmp_path)
    assert code == 0 and end["status"] == "complete"
    assert end.get("stop_reason") != "context_budget"
    meta = [tmp_path / "index.jsonl",
            harness.transcript_path.with_name("run.usage.jsonl"),
            harness.transcript_path.with_name("run.protocol-health.json")]
    for p in meta:      # accounting and status metadata never contain raw output
        assert SENTINEL not in p.read_text(encoding="utf-8", errors="replace"), p


@pytest.mark.parametrize("context_policy", ["legacy", "model-v1"])
def test_main_large_native_batch_preserves_all_results(
        harness, engine, tmp_path, monkeypatch, context_policy):
    _setup(engine, tmp_path, monkeypatch)
    calls = [_call("run_command", {"argv": EMIT}, cid=f"cmd{i}")["choices"][0]
             ["message"]["tool_calls"][0] for i in range(30)]
    batch = {"choices": [{"message": {"content": "", "tool_calls": calls},
                          "finish_reason": "stop"}]}
    code = _run(harness, "-m", GPT, "--profile", "resilient-v1", "--write",
                "--context-policy", context_policy,
                replies=[batch, _finish_reply("native")])
    assert code == 0 and _end(tmp_path)["status"] == "complete"
    assert (tmp_path / "count.txt").read_text() == "x" * 30
    assert len(harness.payloads) == 2
    paired = [m for m in harness.payloads[1]["messages"] if m["role"] == "tool"]
    assert [m["tool_call_id"] for m in paired] == [f"cmd{i}" for i in range(30)]
    assert all(m["content"].startswith("exit=0\n") for m in paired)
    if context_policy == "legacy":
        assert any("previews dropped" in m["content"] for m in paired)
    else:
        refs = []
        for result in paired:
            content = result["content"]
            assert "previews dropped" not in content
            assert SENTINEL in content
            result_refs = re.findall(r'"ref": "(cmd://[^"]+)"', content)
            assert result_refs
            refs.extend(result_refs)
        assert len(refs) == len(set(refs))

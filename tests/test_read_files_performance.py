"""Equivalent-evidence request-count proof for read_files. Synthetic, scripted only."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from test_engine_reasoning import _finish_reply, engine, harness  # noqa: F401
from test_engine_turn_budget import GPT, _call, _run

ON = ("--read-files", "--profile", "resilient-v1", "--context-policy", "model-v1")
BASE = ("--profile", "resilient-v1", "--context-policy", "model-v1")


@pytest.fixture(autouse=True)
def isolated(engine, monkeypatch, tmp_path):
    monkeypatch.syspath_prepend(str(Path(engine.__file__).parent))
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.delenv("_HARNESS_APPROVED_WORKER", raising=False)


def _files(tmp_path, n):
    names = [f"f{i}.txt" for i in range(n)]
    for i, name in enumerate(names):
        (tmp_path / name).write_text(f"synthetic body {i}\n", encoding="utf-8")
    return names


def _tool_bodies(payload):
    return [m["content"] for m in payload["messages"] if m["role"] == "tool"]


@pytest.mark.parametrize("n", [2, 8])
def test_batch_reduces_requests_with_equal_evidence(harness, tmp_path, n) -> None:
    names = _files(tmp_path, n)
    single = [_call("read_file", {"path": p}, cid=f"r{i}") for i, p in enumerate(names)]
    assert _run(harness, *BASE, "-m", GPT, replies=single + [_finish_reply("native")]) == 0
    baseline = list(harness.payloads)
    assert len(baseline) == n + 1
    singles = _tool_bodies(baseline[-1])
    assert singles == [f"synthetic body {i}\n" for i in range(n)]

    harness.payloads.clear()
    batch = _call("read_files", {"requests": [{"path": p} for p in names]}, cid="b1")
    assert _run(harness, *ON, "-m", GPT, replies=[batch, _finish_reply("native")]) == 0
    assert len(harness.payloads) == 2
    tool_msgs = [m for m in harness.payloads[-1]["messages"] if m["role"] == "tool"]
    assert [m["tool_call_id"] for m in tool_msgs] == ["b1"]
    rows = json.loads(tool_msgs[0]["content"])["results"]
    assert [r["index"] for r in rows] == list(range(n))
    assert [r["content"] for r in rows] == singles
    assert all(r["status"] == "served" and r["complete"] for r in rows)


def test_partial_and_error_rows_are_not_claimed_complete(harness, engine, tmp_path, monkeypatch) -> None:
    text = "synthetic line\n" * 75
    (tmp_path / "a.txt").write_text(text, encoding="utf-8")
    policy = tmp_path / ".claude"
    policy.mkdir(exist_ok=True)
    (policy / "agent-policy.json").write_text(json.dumps({"max_read_chars": 1000}), encoding="utf-8")
    batch = _call("read_files", {"requests": [{"path": "nope.txt"}, {"path": "a.txt"}]})
    continuation = _call("read_files", {"requests": [{"path": "a.txt", "start_line": 1}]}, cid="next")
    real = engine.Runner.read_files
    rows_seen = []
    def read(self, requests):
        out = real(self, requests)
        rows = json.loads(out)["results"]
        rows_seen.append(rows)
        if len(rows_seen) == 1:
            continuation["choices"][0]["message"]["tool_calls"][0]["function"]["arguments"] = json.dumps(
                {"requests": [{"path": "a.txt", **rows[1]["next_range"]}]})
        return out
    monkeypatch.setattr(engine.Runner, "read_files", read)
    assert _run(harness, *ON, "-m", GPT,
                replies=[batch, continuation, _finish_reply("native")]) == 0
    rows = rows_seen[0]
    assert [r["status"] for r in rows] == ["error", "served"]
    assert "content" not in rows[0] and not rows[1]["complete"]
    assert rows_seen[1][0]["complete"]
    assert rows[1]["content"] + rows_seen[1][0]["content"] == text


@pytest.mark.parametrize("cached", [None, 0])
def test_cache_unknown_vs_measured_zero_and_numeric_clock(harness, engine, tmp_path, cached):
    from test_engine_turn_budget import _usage, _end
    names = _files(tmp_path, 2)
    usage = {"prompt_tokens": 10, "completion_tokens": 2}
    if cached is not None:
        usage["prompt_tokens_details"] = {"cached_tokens": cached}
    read = _call("read_files", {"requests": [{"path": p} for p in names]})
    read["usage"] = usage
    assert _run(harness, *ON, "-m", GPT, replies=[read, _finish_reply("native", usage)],
                durations=[2.0, 1.0]) == 0
    assert [row["cache_read"] for row in _usage(harness)] == [cached, cached]
    assert engine._now() == 1003.0
    assert _end(tmp_path)["status"] == "complete"


@pytest.mark.parametrize("withhold", [False, True])
def test_model_admission_receipts_and_partial_closeout(harness, engine, tmp_path, monkeypatch, withhold):
    from test_engine_turn_budget import _end, _prose
    runtime = engine.capability_module("context_runtime").ContextRuntime
    original_admit, original_end = runtime.admit_result, runtime.end_batch
    admitted, closed = [], []
    def admit(self, name, body, **kwargs):
        result = original_admit(self, name, body, **kwargs)
        admitted.append((name, result))
        return result
    def end(self):
        result = original_end(self)
        closed.append(self.metadata())
        return result
    monkeypatch.setattr(runtime, "admit_result", admit)
    monkeypatch.setattr(runtime, "end_batch", end)
    if withhold:
        monkeypatch.setattr(runtime, "before_action", lambda self, slot:
                            self.guard.stop("model_window_full") or False)
    names = _files(tmp_path, 2)
    read = _call("read_files", {"requests": [{"path": p} for p in names]})
    code = _run(harness, *ON, "-m", GPT, replies=[read,
                _prose() if withhold else _finish_reply("native")])
    assert code == (3 if withhold else 0)
    states = closed[0]["receipts"]["states"]
    assert states["pending"] == states["provisional"] == 0
    if withhold:
        assert not admitted and closed[0]["receipts"]["outcomes"]["skipped"] == 1
        assert "tools" not in harness.payloads[-1]
        assert _end(tmp_path)["stop_reason"] == "context_budget"
    else:
        assert admitted[0][0] == "read_files"
        assert json.loads(admitted[0][1])["results"][0]["complete"]

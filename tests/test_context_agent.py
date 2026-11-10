"""Native agent loop under the model-v1 context runtime; fake gateway, tmp HOME, synthetic text."""
import json
import sys
from pathlib import Path

import pytest

from test_engine_reasoning import engine, harness  # noqa: F401
from test_engine_turn_budget import GPT, _run


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch, engine):
    monkeypatch.syspath_prepend(str(Path(engine.__file__).parent))
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.delenv("_HARNESS_APPROVED_WORKER", raising=False)


def _batch(*calls):
    return {"choices": [{"message": {"content": "", "tool_calls": [
        {"id": cid, "type": "function",
         "function": {"name": name, "arguments": json.dumps(args)}}
        for cid, name, args in calls]}, "finish_reason": "stop"}]}


def test_native_large_read_uses_zero_argument_compact_callback(harness, tmp_path):
    (tmp_path / "large.txt").write_text("synthetic-body\n" * 1400, encoding="utf-8")
    first = _batch(("read", "read_file", {"path": "large.txt"}),
                   ("list", "list_dir", {"path": "."}))
    last = _batch(("finish", "finish", {"summary": "done"}))
    code = _run(harness, "-m", GPT, "--profile", "resilient-v1",
                replies=[first, last])
    assert code == 0
    assert len(harness.payloads) == 2
    results = [m for m in harness.payloads[1]["messages"] if m["role"] == "tool"]
    assert len(results) == 2
    assert any("the captured result and its content_complete flag" in m["content"] for m in results)
    assert any("large.txt" in m["content"] for m in results)


@pytest.fixture
def emitter(engine, tmp_path, monkeypatch):
    argv = ["python", "emit.py"]
    (tmp_path / "emit.py").write_text(
        "open('count.txt', 'a').write('x')\n"
        "print('synthetic-body')\nraise SystemExit(3)\n", encoding="utf-8")
    policy_dir = tmp_path / ".claude"
    policy_dir.mkdir(exist_ok=True)
    (policy_dir / "agent-policy.json").write_text(
        json.dumps({"allowed_cmds": [argv]}), encoding="utf-8")
    monkeypatch.setattr(engine.shutil, "which", lambda name: sys.executable)
    return argv


def test_native_command_then_deferred_finish_large_task(harness, tmp_path, emitter):
    argv = emitter
    reply = _batch(("c1", "run_command", {"argv": argv}),
                   ("c2", "finish", {"summary": "done"}))
    code = _run(harness, "-m", GPT, "--profile", "resilient-v1", "--write",
                "-t", "synthetic " * 80000, replies=[reply])
    assert code == 0
    assert len(harness.payloads) == 1
    assert len(json.dumps(harness.payloads[0])) > 600000
    assert (tmp_path / "count.txt").read_text() == "x"


def test_two_requests_reused_ids_command_exit_and_reader(
        harness, engine, tmp_path, monkeypatch, emitter):
    runtime = engine.capability_module("context_runtime").ContextRuntime
    original_admit = runtime.admit_result
    admitted, batches = [], []
    original_begin = runtime.begin_batch
    original_end = runtime.end_batch
    reader_reply = _batch(
        ("c1", "read_context_result", {"ref": "pending"}),
        ("c2", "finish", {"summary": "done"}))

    def begin(self, *args, **kwargs):
        batches.append(self)
        return original_begin(self, *args, **kwargs)

    def admit(self, name, body, **kwargs):
        result = original_admit(self, name, body, **kwargs)
        admitted.append((name, body, kwargs, result))
        return result

    def end(self, *args, **kwargs):
        result = original_end(self, *args, **kwargs)
        if len(batches) == 1:
            ref = self.group_refs()[-1][0]
            reader_reply["choices"][0]["message"]["tool_calls"][0]["function"][
                "arguments"] = json.dumps({"ref": ref, "offset": 0, "limit": 64})
        return result

    monkeypatch.setattr(runtime, "begin_batch", begin)
    monkeypatch.setattr(runtime, "admit_result", admit)
    monkeypatch.setattr(runtime, "end_batch", end)
    argv = emitter
    code = _run(harness, "-m", GPT, "-e", "high", "--profile", "resilient-v1", "--write",
                replies=[_batch(("c1", "run_command", {"argv": argv})), reader_reply])
    assert code == 0
    assert len(harness.payloads) == 2
    assert batches[0] is batches[1]
    for payload in harness.payloads:
        assert "read_context_result" in {
            tool["function"]["name"] for tool in payload["tools"]}
    command = next(row for row in admitted if row[0] == "run_command")
    assert command[2]["outcome"] == "returned"
    assert command[2]["exit_code"] == 3
    assert (tmp_path / "count.txt").read_text() == "x"
    reader = next(row for row in admitted if row[0] == "read_context_result")
    page = json.loads(reader[1])
    assert page["offset"] == 0
    assert "synthetic-body" in json.dumps(page)
    assert command[2]["slot_id"] != reader[2]["slot_id"]
    rows = harness.payloads[1]["messages"]
    assert sum(row.get("tool_call_id") == "c1" for row in rows) == 1

    # A second invocation must not retain the first run's receipt authority.
    prior_runtime = batches[0]
    assert _run(harness, "-m", GPT, "--profile", "legacy",
                replies=[_batch(("c1", "finish", {"summary": "done"}))]) == 0
    assert batches[-1] is not prior_runtime


def test_context_reader_validation_and_fixed_errors(engine):
    class Runtime:
        calls = []

        def read_result(self, **kwargs):
            self.calls.append(kwargs)
            raise ValueError("synthetic-private-backend-detail")

    class Reader:
        context_runtime = None

    reader = Reader()
    method = engine.Runner.read_context_result
    assert "unavailable" in method(reader, "ctx://synthetic")
    reader.context_runtime = Runtime()
    assert method(reader, "ctx://synthetic", offset=True) == (
        "error: invalid context result request")
    assert not reader.context_runtime.calls
    assert method(reader, "ctx://synthetic") == "error: invalid context result request"
    assert len(reader.context_runtime.calls) == 1


@pytest.mark.parametrize("blocked", ["before_action", "unknown"])
def test_native_pending_calls_close_without_replay(
        harness, engine, monkeypatch, emitter, blocked):
    runtime = engine.capability_module("context_runtime").ContextRuntime
    original_end = runtime.end_batch
    closed, dispatched = [], []

    def end(self):
        result = original_end(self)
        closed.append(self.metadata())
        return result

    def dispatch(self, name, args):
        dispatched.append(name)
        assert name == "run_command"
        self._last_cmd_info = ("outcome_unknown", True, None, False)
        self.command_timed_out = True
        return "synthetic unknown command outcome"

    monkeypatch.setattr(runtime, "end_batch", end)
    monkeypatch.setattr(engine.Runner, "dispatch", dispatch)
    if blocked == "before_action":
        # End after the one refused batch without requiring another tool action.
        monkeypatch.setattr(runtime, "before_action", lambda self, slot:
                            self.guard.stop("model_window_full") or False)
    reply = _batch(
        ("f1", "finish", {"summary": "early proposal"}),
        ("c1", "run_command", {"argv": emitter}),
        ("c2", "list_dir", {"path": "."}),
        ("f2", "finish", {"summary": "last proposal"}))
    partial = {"choices": [{"message": {"content": "synthetic partial"},
                            "finish_reason": "stop"}]}
    assert _run(harness, "-m", GPT, "--write", replies=[reply, partial]) == 3
    assert dispatched == ([] if blocked == "before_action" else ["run_command"])
    assert len(closed) == 1
    states = closed[0]["receipts"]["states"]
    assert states["pending"] == states["provisional"] == 0
    outcomes = closed[0]["receipts"]["outcomes"]
    assert outcomes["unknown"] == (blocked == "unknown")
    assert outcomes["skipped"] == (4 if blocked == "before_action" else 3)
    assert len(harness.payloads) == 2
    assert "tools" not in harness.payloads[-1]

"""Actual Python interpreters, local scripted transport, temporary state only."""
import json
import os
import subprocess
import sys
from pathlib import Path
import pytest

ENGINE = Path(__file__).resolve().parents[1] / "engine" / "delegate.py"


def call(name, args, identity):
    return {"id": identity, "type": "function", "function": {
        "name": name, "arguments": json.dumps(args)}}


def run_child(tmp_path, *, fail_after_command=False, fail_checkpoint=False,
              fail_result_checkpoint=False, invalid_tail=False, extra=()):
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    (scripts / "exit_one.py").write_text(
        "from pathlib import Path\nimport sys\np=Path('executions')\np.write_text((p.read_text() if p.exists() else '')+'x')\nsys.exit(1)\n",
        encoding="utf-8")
    (tmp_path / "out.txt").write_text("old", encoding="utf-8")
    policy_dir = tmp_path / ".claude"
    policy_dir.mkdir()
    (policy_dir / "agent-policy.json").write_text(json.dumps({"allowed_cmds": [["python", "scripts/*"]]}), encoding="utf-8")
    actions = [call("replace_in_file", {"path": "out.txt", "old_string": "old", "new_string": "new"}, "c0"),
               call("run_command", {"argv": ["python", "scripts/exit_one.py"]}, "c1")]
    if invalid_tail:
        actions[-1] = call("run_command", {"argv": "synthetic invalid argv"}, "c1")
    replies = [{"choices": [{"message": {"content": "", "tool_calls": actions}, "finish_reason": "stop"}]},
        {"choices": [{"message": {"content": "", "tool_calls": [
            call("finish", {"summary": "synthetic complete"}, "finish")]}, "finish_reason": "stop"}]}]
    driver = tmp_path / "driver.py"
    driver.write_text(f'''
import importlib.util, json, os, sys
from pathlib import Path
sys.path.insert(0, {str(ENGINE.parent)!r})
spec = importlib.util.spec_from_file_location("synthetic_engine", {str(ENGINE)!r})
engine = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = engine
spec.loader.exec_module(engine)
engine.load_cfg = lambda: ("http://synthetic.invalid", "synthetic-key")
engine.refresh_models = lambda *a: None
replies = iter({replies!r})
engine.chat = lambda *a, **k: next(replies)
original = engine.Transcript.step_result
def result(self, value, *args, **kwargs):
    if {fail_after_command!r} and str(value).startswith("exit=1"):
        raise TypeError("synthetic result processing failure")
    return original(self, value, *args, **kwargs)
engine.Transcript.step_result = result
if {fail_checkpoint!r} or {fail_result_checkpoint!r}:
    import builtins
    old_open = builtins.open
    class History:
        def __init__(self, handle): self.handle = handle
        def __enter__(self): return self
        def __exit__(self, *args): return self.handle.__exit__(*args)
        def write(self, value):
            if json.loads(value).get("role") == "tool":
                raise OSError("synthetic result checkpoint failure")
            return self.handle.write(value)
    def fail_history(file, mode="r", *args, **kwargs):
        if str(file).endswith(".messages.jsonl") and "a" in mode:
            if {fail_checkpoint!r}:
                raise OSError("synthetic checkpoint failure")
            return History(old_open(file, mode, *args, **kwargs))
        return old_open(file, mode, *args, **kwargs)
    engine.open = fail_history
sys.argv = ["delegate.py", "-C", {str(tmp_path)!r}, "-m", "anthropic_tool_call.claude-x",
    "--profile", "resilient-v1", "--write", "--no-stream", "--max-iters", "8",
    "--transcript", {str(tmp_path / "run.md")!r}, "-t", "synthetic task", *{list(extra)!r}]
engine.main()
''', encoding="utf-8")
    env = dict(os.environ, CLAUDE_AGENT_RUN_INDEX=str(tmp_path / "machine-index.jsonl"))
    result = subprocess.run([sys.executable, str(driver)], cwd=tmp_path, env=env,
                            capture_output=True, text=True, timeout=60)
    diary = tmp_path / "run.md.engine.diagnostics.jsonl"
    rows = [json.loads(line) for line in diary.read_text().splitlines()] if diary.exists() else []
    index = tmp_path / "machine-index.jsonl"
    ends = [row for row in map(json.loads, index.read_text().splitlines()) if row["event"] == "end"]
    return result, rows, ends


def test_real_edit_then_empty_command_exit_one_does_not_crash_engine(tmp_path):
    result, rows, ends = run_child(tmp_path)
    assert result.returncode == 0, result.stderr
    assert (tmp_path / "out.txt").read_text() == "new"
    assert (tmp_path / "executions").read_text() == "x"
    assert ends[-1]["status"] == "complete"
    assert any(row["event"] == "command_exit" and row.get("exit_code") == 1 for row in rows)
    spawned = [row for row in rows if row["event"] == "command_start"]
    assert len(spawned) == 1 and spawned[0]["child_pid"] > 0
    if os.name == "nt":
        assert spawned[0]["child_birth"].startswith("winft:")
    assert [(row["turn"], row["call_index"]) for row in rows
            if row["event"] == "tool_start"][:2] == [(1, 1), (1, 2)]
    assert sum(row["event"] == "result_recorded" and row.get("turn") == 1 for row in rows) == 2
    assert not any(row["event"] == "exception" for row in rows)
    builds = [row for row in rows if row["event"] == "engine_build"]
    assert builds[-1]["execution_profile"] == "resilient-v1"
    assert builds[-1]["runtime_version"]


def test_result_processing_exception_records_cause_without_replaying_command(tmp_path):
    result, rows, ends = run_child(tmp_path, fail_after_command=True)
    assert result.returncode == 1
    assert (tmp_path / "executions").read_text() == "x"
    fatal = [row for row in rows if row["event"] == "exception"]
    assert fatal[-1]["exception_class"] == "TypeError"
    assert fatal[-1]["source"] == "delegate.py"
    assert ends[-1]["status"] == "interrupted"
    assert not any(row["event"] == "result_recorded" and row.get("turn") == 1 for row in rows)


def test_checkpoint_io_failure_has_a_failed_boundary_and_terminal_fallback(tmp_path):
    result, rows, ends = run_child(tmp_path, fail_checkpoint=True)
    assert result.returncode == 1
    failed = [row for row in rows if row["event"] == "checkpoint_end" and row.get("outcome") == "raised"]
    assert failed and failed[0]["exception_class"] == "OSError"
    assert ends[-1]["status"] == "interrupted"


@pytest.mark.parametrize("policy", ["legacy", "model-v1"])
def test_result_checkpoint_failure_identifies_the_result_not_the_last_executed_tool(tmp_path, policy):
    result, rows, ends = run_child(tmp_path, fail_result_checkpoint=True,
                                   extra=("--context-policy", policy))
    assert result.returncode == 1
    if policy == "legacy":  # legacy journals results only after the whole batch ran
        assert (tmp_path / "executions").read_text() == "x"
    else:  # model-v1 journals each admitted result before the next dispatch
        assert not (tmp_path / "executions").exists()
        assert not any(r["event"] == "tool_start" and r.get("call_index") == 2 for r in rows)
    failed = [row for row in rows if row["event"] == "checkpoint_end" and row.get("outcome") == "raised"]
    assert failed[-1]["turn"] == 1 and failed[-1]["call_index"] == 1
    assert failed[-1]["exception_class"] == "OSError"
    assert ends[-1]["status"] == "interrupted"


def test_rejected_batch_has_explicit_validation_outcome_and_no_action_start(tmp_path):
    result, rows, ends = run_child(tmp_path, invalid_tail=True)
    assert result.returncode == 0, result.stderr
    assert (tmp_path / "out.txt").read_text() == "old"
    assert not (tmp_path / "executions").exists()
    validation = [row for row in rows if row["event"] == "batch_validated" and row.get("turn") == 1]
    assert validation[-1]["outcome"] == "error"
    assert not any(row["event"] == "tool_start" and row.get("turn") == 1 for row in rows)

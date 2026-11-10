"""Finish-gate integration with synthetic replies and temporary run state only."""
from __future__ import annotations

import json
import subprocess

import pytest

from test_engine_reasoning import engine, harness  # noqa: F401
from test_engine_turn_budget import GPT, _call, _finish_reply, _prose, _run

CHECK = ["python", "-m", "py_compile", "x.py"]
VERIFY = ["--verify-command", json.dumps(CHECK)]
FINISH = ("finish", {"summary": "synthetic finished"})


@pytest.fixture(autouse=True)
def _isolated_skills(tmp_path, monkeypatch):
    home = str(tmp_path / "home")
    monkeypatch.setenv("HOME", home)
    monkeypatch.setenv("USERPROFILE", home)


def _batch(*actions):
    calls = [
        _call(name, args, cid=f"c{i}")["choices"][0]["message"]["tool_calls"][0]
        for i, (name, args) in enumerate(actions)
    ]
    return {"choices": [{"message": {"content": "", "tool_calls": calls},
                         "finish_reason": "stop"}]}


def _completion(harness):
    path = harness.transcript_path.with_suffix(".artifacts") / "completion.json"
    return json.loads(path.read_text(encoding="utf-8"))


def _passing_command(engine, monkeypatch):
    calls = []

    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, 0, "synthetic command output", "")

    co = engine.capability_module("command_output")

    def capture(argv, *, cwd, timeout, open_sink, on_start=None, on_exit=None):
        # model-v1 context (the default) runs commands through bounded capture.
        calls.append((argv, {"cwd": cwd, "timeout": timeout}))
        out, err = co.StreamCapture("stdout", open_sink), co.StreamCapture("stderr", open_sink)
        out.feed(b"synthetic command output")
        if on_exit is not None:
            on_exit(0)
        return co.Result(0, out.finish(), err.finish())

    monkeypatch.setattr(engine.shutil, "which", lambda name: name)
    monkeypatch.setattr(engine.subprocess, "run", run)
    monkeypatch.setattr(co, "capture_command", capture)
    return calls


@pytest.mark.parametrize("missing", [
    ("finish_gate",), ("trajectory",), ("finish_gate", "trajectory"),
])
@pytest.mark.parametrize("write_mode", ["stage", "disk"])
def test_runner_without_optional_modules(engine, tmp_path, monkeypatch, missing, write_mode):
    available = engine.capability_module

    def capability_module(name):
        if name in missing:
            raise engine.CapabilityUnavailable(f"{name} is not installed")
        return available(name)

    monkeypatch.setattr(engine, "capability_module", capability_module)
    monkeypatch.setattr(engine, "ALLOWED_CMDS", [CHECK])
    commands = _passing_command(engine, monkeypatch)
    path = tmp_path / "x.py"
    path.write_text("value = 0\n", encoding="utf-8")
    runner = engine.Runner(str(tmp_path), write_mode=write_mode)
    try:
        assert (runner.evidence is None) == ("finish_gate" in missing)
        assert (runner.base_tracker is None) == ("finish_gate" in missing)
        assert (runner.trajectory is None) == ("trajectory" in missing)
        assert runner.dispatch("read_file", {"path": "x.py"}) == "value = 0\n"
        runner.dispatch("write_file", {"path": "x.py", "content": "value = 1\n"})
        assert runner.dispatch("replace_in_file", {
            "path": "x.py", "old_string": "1", "new_string": "2",
        }).startswith("replaced 1 occurrence")
        runner.write_file("x.py", "value = 2\n")
        assert runner.candidate_seq == 2
        runner.write_file("new.txt", "synthetic new file\n")
        assert runner.candidate_seq == 3
        if write_mode == "stage":
            assert path.read_text(encoding="utf-8") == "value = 0\n"
            patch = runner.patch().replace("\r\n", "\n")
            assert "-value = 0\n+value = 2\n" in patch
            assert "+synthetic new file\n" in patch
        else:
            assert path.read_text(encoding="utf-8") == "value = 2\n"
            assert runner.patch() == ""
        assert runner.dispatch("run_command", {"argv": CHECK}).startswith("exit=0")
        assert len(commands) == 1
        assert runner.run_command(["python", "../escape.py"]).startswith("refused:")
        assert runner.run_command(["synthetic-disallowed-command"]).startswith("refused:")
        assert runner.dispatch("read_file", {"path": "../escape.txt"}).startswith("error:")
        assert runner.dispatch("__init__", {}).startswith("error: no such tool")
    finally:
        runner.close()


@pytest.mark.parametrize("flags", [
    ["--verify-command", "not-json"],
    ["--verify-command", "[]"],
    ["--verify-command", '["python", 1]'],
    ["--verify-command", '{"command": "python"}'],
    ["--verify-command", '[""]'],
    ["--oneshot", *VERIFY],
    ["--oneshot", "--review-finish"],
])
def test_invalid_verify_command_rejected_before_config(
        harness, engine, tmp_path, monkeypatch, flags):
    def forbidden(*args):
        raise AssertionError("policy/config must not be read")

    monkeypatch.setattr(engine, "load_policy", forbidden)
    monkeypatch.setattr(engine, "load_cfg", forbidden)
    assert _run(harness, "-m", GPT, *flags, replies=[]) == 2
    assert not (tmp_path / "index.jsonl").exists()
    assert harness.payloads == []


@pytest.mark.parametrize("validation", ["relative", "absolute", "explicit", "runner_patch"])
def test_staged_base_compatibility_rejects_changed_disk_base(engine, tmp_path, monkeypatch, validation):
    monkeypatch.chdir(tmp_path)
    path = tmp_path / "x.txt"
    path.write_text("synthetic base\n", encoding="utf-8")
    runner = engine.Runner(str(tmp_path))
    try:
        runner.write_file("x.txt", "synthetic pending\n")
        validate = {
            "relative": lambda: runner.base_tracker.validate("x.txt"),
            "absolute": lambda: runner.base_tracker.validate(str(path)),
            "explicit": lambda: runner.base_tracker.validate("x.txt", str(path)),
            "runner_patch": runner.patch,
        }[validation]
        assert "synthetic base" in validate()
        path.write_text("synthetic external edit\n", encoding="utf-8")
        with pytest.raises(engine.capability_module("finish_gate").StagedBaseChangedError):
            validate()
    finally:
        runner.close()


def test_disk_mutation_without_verify_command_records_unverified(harness, tmp_path, capsys):
    assert _run(harness, "-m", GPT, "--write", replies=[_batch(
        ("write_file", {"path": "x.py", "content": "value = 1\n"}), FINISH)]) == 0
    record = _completion(harness)
    assert record["verification_status"] == "unverified_post_edit_mutation"
    assert "no_verification_specified" in record["gate"]["reason_codes"]
    assert record["correction_attempted"] is False
    assert record["candidate_unavailable"]["reason"] == "unbounded_disk_write"
    assert (tmp_path / "x.py").read_text() == "value = 1\n"
    marker = "Verification: unverified_post_edit_mutation"
    assert marker in harness.transcript_path.read_text(encoding="utf-8")
    assert marker in capsys.readouterr().out
    metadata = json.dumps(record)
    assert "value = 1" not in metadata and "synthetic finished" not in metadata
    assert "do the thing" not in metadata


def test_staged_edit_even_with_passing_command_is_staged_unverified_on_disk(
        harness, engine, tmp_path, monkeypatch, capsys):
    (tmp_path / "x.py").write_text("value = 0\n", encoding="utf-8")
    calls = _passing_command(engine, monkeypatch)
    assert _run(harness, "-m", GPT, *VERIFY, replies=[_batch(
        ("write_file", {"path": "x.py", "content": "value = 1\n"}),
        ("run_command", {"argv": CHECK}), FINISH)]) == 0
    record = _completion(harness)
    assert record["verification_status"] == "staged_unverified_on_disk"
    assert record["correction_attempted"] is False
    assert record["patch_binding"]["source"] == "pending"
    assert len(calls) == 1 and len(harness.payloads) == 1
    assert (tmp_path / "x.py").read_text() == "value = 0\n"
    assert "Verification: staged_unverified_on_disk" in capsys.readouterr().out
    assert "synthetic command output" not in json.dumps(record)


@pytest.mark.parametrize("mode", ["native", "prompt"])
def test_disk_verify_command_defers_finish_once_then_verifies(
        harness, engine, monkeypatch, mode):
    calls = _passing_command(engine, monkeypatch)
    edit = ("write_file", {"path": "x.py", "content": "value = 1\n"})
    check = ("run_command", {"argv": CHECK})
    if mode == "native":
        flags = []
        replies = [_batch(edit, FINISH), _batch(check, FINISH)]
        correction_index = 1
    else:
        flags = ["--tool-mode", "prompt", "--allow-prompt-tools"]
        replies = [_prose("```tool " + json.dumps({"name": name, "args": args}) + "```")
                   for name, args in (edit, FINISH, check, FINISH)]
        correction_index = 2
    assert _run(harness, "-m", GPT, "--write", *VERIFY, *flags, replies=replies) == 0
    record = _completion(harness)
    assert record["verification_status"] == "verified_by_command"
    assert record["correction_attempted"] is True
    assert len(calls) == 1 and len(harness.payloads) == len(replies)
    assert "factual verification incomplete" in json.dumps(
        harness.payloads[correction_index]["messages"])


def test_post_check_edit_invalidates_prior_passing_check(harness, engine, monkeypatch):
    _passing_command(engine, monkeypatch)
    assert _run(harness, "-m", GPT, "--write", *VERIFY, replies=[_batch(
        ("run_command", {"argv": CHECK}),
        ("write_file", {"path": "x.py", "content": "value = 1\n"}), FINISH),
        _finish_reply("native")]) == 0
    record = _completion(harness)
    assert record["verification_status"] == "unverified_post_edit_mutation"
    assert record["gate"]["reason_codes"] == ["post_check_mutation"]
    assert record["correction_attempted"] is True
    assert "post_check_mutation" in json.dumps(harness.payloads[1]["messages"])
    assert len(harness.payloads) == 2


def test_partial_disk_write_failure_invalidates_prior_passing_check(
        harness, engine, tmp_path, monkeypatch):
    _passing_command(engine, monkeypatch)
    target = tmp_path / "x.py"
    target.write_text("value = 0\n", encoding="utf-8")
    real_replace = engine.os.replace
    replacements = []

    def replace_then_raise(src, dst):
        real_replace(src, dst)
        if engine.os.path.normcase(str(dst)) == engine.os.path.normcase(str(target)):
            replacements.append((src, dst))
            # The disk effect completed, but its acknowledgement was lost.
            raise OSError("synthetic replacement acknowledgement failure")

    monkeypatch.setattr(engine.os, "replace", replace_then_raise)
    assert _run(harness, "-m", GPT, "--write", *VERIFY, replies=[_batch(
        ("write_file", {"path": "ok.py", "content": "ok = 1\n"}),
        ("run_command", {"argv": CHECK}),
        ("write_file", {"path": "x.py", "content": "value = 1\n"}),
        FINISH)]) == 0
    assert target.read_bytes() == b"value = 1\n"
    assert len(replacements) == 1
    record = _completion(harness)
    assert record["verification_status"] == "unverified_post_edit_mutation"
    assert "command_outcome_unknown" in record["gate"]["reason_codes"]


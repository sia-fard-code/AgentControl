"""Opt-in resilient-v1 profile: flag, guard recovery, legacy unchanged.

Synthetic fixtures and fakes only; nothing reaches a real gateway.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from test_engine_reasoning import engine, harness  # noqa: F401
from test_engine_turn_budget import GPT, _call, _run

RES = "resilient-v1"


@pytest.fixture(autouse=True)
def _sibling_imports(engine, monkeypatch):
    # Match launching delegate.py as a script without relying on test order.
    monkeypatch.syspath_prepend(str(Path(engine.__file__).parent))


@pytest.fixture()
def cg(engine):
    return engine.capability_module("context_guard")


def _env():
    return {"role": "tool", "tool_call_id": "c1"}


def test_guard_default_profile_is_legacy(cg) -> None:
    assert cg.Guard().profile == "legacy"
    with pytest.raises(ValueError):
        cg.Guard(profile="bogus")


def test_legacy_guard_still_latches_oversized_read(cg) -> None:
    g = cg.Guard()
    g.start_batch({"m": "x" * 500}, 1)
    out = g.admit("read_file", "x" * 65537, envelope=_env())
    assert g.exhausted and "refused" in out


def test_resilient_recovers_oversized_read_without_latch(cg) -> None:
    g = cg.Guard(profile=RES)
    g.start_batch({"m": "x" * 500}, 1)          # ~514-byte request
    for name in ("read_file", "grep", "list_dir"):
        g.batch = 0
        before = g.output
        out = g.admit(name, "é" * 65537, envelope=_env())
        assert not g.exhausted, name
        assert "[context budget]" in out and "narrow" in out
        delivered = len(out.encode("utf-8"))
        assert g.output - before == delivered
        assert delivered <= cg.BATCH_BUDGET


def test_resilient_cumulative_read_exhaustion_still_latches(cg) -> None:
    g = cg.Guard(profile=RES)
    g.start_batch({"m": "x" * 88}, 1)           # ~102-byte request
    g.read = cg.READ_BUDGET
    out = g.admit("read_file", "x" * 70000, envelope=_env())
    assert g.exhausted and g.reason == "cumulative read" and "refused" in out
    assert "provider" not in out


def test_resilient_respects_actual_request_headroom(cg) -> None:
    g = cg.Guard(profile=RES)
    limit = cg.REQUEST_LIMIT - cg.CLOSEOUT_RESERVE
    g.start_batch({"m": "x" * (limit - 3000)}, 1)
    out = g.admit("grep", "y" * 65537, envelope=_env())
    assert g.base <= limit
    if not g.exhausted:
        assert len(out.encode("utf-8")) < 3000


def test_resilient_keeps_reservations_for_later_calls(cg) -> None:
    g = cg.Guard(profile=RES)
    g.start_batch({"m": "x"}, 3)
    g.admit("read_file", "z" * 200000, envelope=_env())
    assert g.base + 2 * cg.COMPACT_RESULT <= cg.REQUEST_LIMIT - cg.CLOSEOUT_RESERVE
    assert g.batch <= cg.BATCH_BUDGET - 2 * cg.COMPACT_RESULT


def test_resilient_run_command_keeps_exit_line_and_latch(cg) -> None:
    g = cg.Guard(profile=RES)
    g.start_batch({"m": "x"}, 1)
    out = g.admit("run_command", "exit=0\n--- stdout ---\n" + "o" * 70000, envelope=_env())
    assert out.startswith("exit=0\n") and "not re-run" in out and g.exhausted
    assert "applied" not in out


# ---- main loop ---------------------------------------------------------------

def _tool(payload, name):
    for t in payload.get("tools", []):
        fn = t.get("function", t)
        if fn.get("name") == name:
            return fn
    raise AssertionError(f"no tool {name}")


def _finish():
    return _call("finish", {"summary": "done"}, cid="c9")


def test_unknown_profile_fails_before_gateway(engine, harness, monkeypatch) -> None:
    monkeypatch.setattr(engine, "load_cfg", lambda: pytest.fail("gateway touched"))
    assert _run(harness, "-m", GPT, "--profile", "bogus", replies=[]) == 2
    assert harness.payloads == []


def test_legacy_schema_unchanged_and_resilient_adds_paging(engine, harness) -> None:
    _run(harness, "-m", GPT, replies=[_finish()])
    legacy = harness.payloads[-1]
    _run(harness, "-m", GPT, "--profile", "legacy", replies=[_finish()])
    assert harness.payloads[-1]["tools"] == legacy["tools"]
    for name in ("list_dir", "grep"):
        assert "offset" not in json.dumps(_tool(legacy, name))
    _run(harness, "-m", GPT, "--profile", RES, replies=[_finish()])
    res = harness.payloads[-1]
    for name in ("list_dir", "grep"):
        props = _tool(res, name)["parameters"]["properties"]
        assert props["offset"]["minimum"] == 0 and props["limit"]["maximum"] > 0
        assert "offset" not in _tool(res, name)["parameters"]["required"]


def test_resilient_main_loop_pages_big_listing(engine, harness, tmp_path) -> None:
    d = tmp_path / "many"
    d.mkdir()
    for i in range(5000):
        (d / f"{'n' * 60}{i:05d}.txt").write_text("x")
    code = _run(harness, "-m", GPT, "--profile", RES, replies=[
        _call("list_dir", {"path": "many"}), _finish()])
    assert code == 0 and len(harness.payloads) == 2
    sent = json.dumps(harness.payloads[1])
    assert "offset=" in sent and "context budget exhausted" not in sent


def test_run_command_oversized_stdout_runs_once_keeps_exit(engine, harness, tmp_path,
                                                             monkeypatch) -> None:
    argv = ["python", "emit.py"]
    (tmp_path / "emit.py").write_text(
        "import sys\nopen('log.txt', 'a').write('appended\\n')\n"
        "sys.stdout.write('o' * 300000)\n", encoding="utf-8")
    policy_dir = tmp_path / ".claude"
    policy_dir.mkdir()
    (policy_dir / "agent-policy.json").write_text(
        json.dumps({"allowed_cmds": [argv]}), encoding="utf-8")
    monkeypatch.syspath_prepend(str(Path(engine.__file__).parent))
    monkeypatch.setattr(engine.shutil, "which", lambda name: sys.executable)
    monkeypatch.setattr(engine, "MAX_RESULT_CHARS", 70000)
    assert _run(harness, "-m", GPT, "--profile", RES, replies=[
        _call("run_command", {"argv": argv}), _finish()]) == 0
    assert (tmp_path / "log.txt").read_text() == "appended\n"
    sent = json.dumps(harness.payloads[1])
    assert "exit=0" in sent and "read_command_output" in sent
    assert "budget exhausted" not in sent


def test_runner_profile_validation_precedes_capabilities(engine, tmp_path, monkeypatch):
    monkeypatch.setattr(engine, "Capabilities", lambda *a, **kw: pytest.fail("caps built"))
    monkeypatch.setattr(engine, "_optional_module", lambda name: pytest.fail("evidence built"))
    with pytest.raises(ValueError, match="profile"):
        engine.Runner(str(tmp_path), profile="bogus")


def test_missing_paged_module_fails_before_gateway(engine, harness, monkeypatch):
    real_module = engine.capability_module

    def missing(name):
        if name == "paged_tools":
            raise engine.CapabilityUnavailable("paged_tools missing")
        return real_module(name)

    monkeypatch.setattr(engine, "capability_module", missing)
    monkeypatch.setattr(engine, "load_cfg", lambda: pytest.fail("gateway touched"))
    with pytest.raises(engine.CapabilityUnavailable, match="paged_tools"):
        _run(harness, "-m", GPT, "--profile", RES, replies=[])


def test_missing_command_output_module_fails_before_gateway(engine, harness, monkeypatch):
    real_module = engine.capability_module

    def missing(name):
        if name == "command_output":
            raise engine.CapabilityUnavailable("command_output missing")
        return real_module(name)

    monkeypatch.setattr(engine, "capability_module", missing)
    monkeypatch.setattr(engine, "load_cfg", lambda: pytest.fail("gateway touched"))
    # Legacy context: the profile preflight raises the capability error itself.
    with pytest.raises(engine.CapabilityUnavailable, match="command_output"):
        _run(harness, "-m", GPT, "--profile", RES, "--context-policy", "legacy", replies=[])


@pytest.mark.parametrize("context", [[], ["--context-policy", "model-v1"]])
def test_missing_command_output_module_refuses_model_context_before_gateway(
        engine, harness, monkeypatch, capsys, context):
    real_module = engine.capability_module

    def missing(name):
        if name == "command_output":
            raise engine.CapabilityUnavailable("command_output missing")
        return real_module(name)

    monkeypatch.setattr(engine, "capability_module", missing)
    monkeypatch.setattr(engine, "load_cfg", lambda: pytest.fail("gateway touched"))
    assert _run(harness, "-m", GPT, "--profile", RES, *context, replies=[]) == 2
    assert "context policy refused: module_unavailable" in capsys.readouterr().err
    assert harness.payloads == []


def test_missing_command_output_module_fails_before_capabilities(engine, tmp_path, monkeypatch):
    real_module = engine.capability_module

    def missing(name):
        if name == "command_output":
            raise engine.CapabilityUnavailable("command_output missing")
        return real_module(name)

    monkeypatch.setattr(engine, "capability_module", missing)
    monkeypatch.setattr(engine, "Capabilities", lambda *a, **kw: pytest.fail("caps built"))
    with pytest.raises(engine.CapabilityUnavailable, match="command_output"):
        engine.Runner(str(tmp_path), profile=RES)


def test_manifest_v1_cannot_admit_new_profile(engine, harness, monkeypatch):
    monkeypatch.setattr(engine, "load_cfg", lambda: pytest.fail("gateway touched"))
    assert _run(harness, "-m", GPT, "--profile", RES,
                "--supervisor-manifest", "not-opened.json", replies=[]) == 2
    assert harness.payloads == []


def test_legacy_does_not_require_paged_module(engine, tmp_path, monkeypatch):
    real_module = engine.capability_module

    def missing(name):
        assert name != "paged_tools", "legacy loaded resilient producer"
        return real_module(name)

    monkeypatch.setattr(engine, "capability_module", missing)
    assert engine.Runner(str(tmp_path)).profile == "legacy"

"""Installed-harness adapter: synthetic installed engine only, temporary paths only."""
import json
import os
import sys
import time
import uuid

import pytest

sys.path.insert(0, os.path.dirname(__file__))
from supervisor_harness_helpers import (  # noqa: E402
    PYTHON, TEST_ARGV, calls, load, make_engine, make_envelope, new_caps, operator_caps)

sh = load("supervisor_harness")
win = pytest.mark.skipif(sys.platform != "win32", reason="Windows Job Objects")


def adapter(tmp_path, env, engine, **kw):
    art = tmp_path / "artifacts"
    art.mkdir(exist_ok=True)
    kw.setdefault("capabilities", operator_caps())
    return sh.HarnessAdapter(env, art, engine_path=engine, python_executable=PYTHON, **kw)


def code(excinfo):
    return excinfo.value.code


def test_command_id_is_deterministic_and_argv_exact(tmp_path):
    assert sh.command_id(TEST_ARGV) == sh.command_id(list(TEST_ARGV))
    assert sh.command_id(TEST_ARGV).startswith("cmd-")
    assert sh.command_id(TEST_ARGV) != sh.command_id(TEST_ARGV + ["-q"])


@win
def test_new_installed_probe_preflights(tmp_path):
    env, root = make_envelope(tmp_path)
    engine = make_engine(tmp_path)
    meta = adapter(tmp_path, env, engine).preflight()
    assert meta["protocol"] == "agentcontrol.supervisor" and meta["version"] == 1
    assert len(meta["engine_sha256"]) == 64
    assert calls(engine) == []                               # probe only, no model call


@pytest.mark.parametrize("behavior,reason", [
    ({"help_flag": False}, "engine_unsupported"),            # old installed script
    ({"caps": {"protocol": "agentcontrol.supervisor", "version": 0}}, "engine_unsupported"),
    ({"caps": "not-json-object"}, "engine_unsupported"),
])
def test_old_or_malformed_engine_fails_with_zero_effects(tmp_path, behavior, reason):
    env, root = make_envelope(tmp_path)
    engine = make_engine(tmp_path, **behavior)
    ad = adapter(tmp_path, env, engine)
    before = sorted(p.name for p in tmp_path.rglob("*"))
    with pytest.raises(sh.HarnessError) as e:
        ad.preflight()
    assert code(e) in (reason, "containment_unavailable")
    assert calls(engine) == []
    assert sorted(p.name for p in tmp_path.rglob("*")) == before   # no state/index/runtime


def test_missing_containment_feature_fails(tmp_path):
    env, root = make_envelope(tmp_path)
    caps = new_caps()
    caps["features"]["exact_command_argv"] = False
    with pytest.raises(sh.HarnessError) as e:
        adapter(tmp_path, env, make_engine(tmp_path, caps=caps)).preflight()
    assert code(e) in ("engine_unsupported", "containment_unavailable")


def test_wrong_policy_digest_fails(tmp_path):
    env, root = make_envelope(tmp_path, policy_digest="b" * 64)
    with pytest.raises(sh.HarnessError) as e:
        adapter(tmp_path, env, make_engine(tmp_path)).preflight()
    assert code(e) == "policy_mismatch"


def test_missing_model_profile_fails(tmp_path):
    env, root = make_envelope(tmp_path, models=["gpt-6.1-sol"])
    with pytest.raises(sh.HarnessError) as e:
        adapter(tmp_path, env, make_engine(tmp_path)).preflight()
    assert code(e) == "model_not_permitted"


@pytest.mark.parametrize("caps", [
    {"write_paths": ["src/", "docs/"], "commands": [list(TEST_ARGV)], "mcp": "none"},
    {"write_paths": ["src/"], "commands": [["python", "evil.py"]], "mcp": "none"},
    {"write_paths": ["src/"], "commands": [list(TEST_ARGV)], "mcp": "all"},
    {"write_paths": ["src/"], "commands": [list(TEST_ARGV)], "mcp": "none", "extra": 1},
])
def test_capabilities_wider_than_envelope_fail(tmp_path, caps):
    env, root = make_envelope(tmp_path)
    with pytest.raises(sh.HarnessError) as e:
        adapter(tmp_path, env, make_engine(tmp_path), capabilities=caps)
    assert code(e) == "capabilities_mismatch"


@win
def test_envelope_cannot_widen_the_inherited_repository_command_policy(tmp_path):
    argv = ["python", "scripts/not-approved.py"]
    env, root = make_envelope(tmp_path, capabilities={"write": ["src/"],
                             "commands": [sh.command_id(argv)], "mcp": []})
    caps = {"write_paths": ["src/"], "commands": [argv], "mcp": "none"}
    with pytest.raises(sh.HarnessError) as exc:
        adapter(tmp_path, env, make_engine(tmp_path), capabilities=caps).preflight()
    assert exc.value.code == "capabilities_mismatch"


@win
def test_engine_change_after_preflight_refuses_launch(tmp_path):
    env, root = make_envelope(tmp_path)
    engine = make_engine(tmp_path)
    ad = adapter(tmp_path, env, engine)
    ad.preflight()
    engine.write_text(engine.read_text() + "\n# changed\n")
    with pytest.raises(sh.HarnessError) as e:
        ad.check_engine()
    assert code(e) == "engine_changed"


def _ready(tmp_path, **behavior):
    env, root = make_envelope(tmp_path)
    engine = make_engine(tmp_path, **behavior)
    ad = adapter(tmp_path, env, engine, clock=lambda: 100.0)
    ad.preflight()
    state = load("supervisor_state").Supervisor(
        tmp_path / "state", env, lambda: 100.0, artifact_dir=ad.artifact_root)
    return ad, state, engine


def _terminal(ad, identity):
    end = time.monotonic() + 10
    while time.monotonic() < end:
        observation = ad.observe(identity)
        if observation["state"] == "TERMINAL":
            return observation
        time.sleep(0.01)
    pytest.fail("synthetic owned process did not reach bound terminal evidence")


@win
def test_real_job_launch_pins_identity_before_any_child_code(tmp_path):
    ad, state, engine = _ready(tmp_path, plan='{"synthetic":true}')
    with state as sup:
        effect = sup.admit_planner("p0", "run-" + uuid.uuid4().hex, 10)
        ordered = []

        def pin(*args):
            assert calls(engine) == []
            ordered.append("pin")
            return sup.record_process_identity(*args)

        try:
            ad.launch(effect, "synthetic brief", on_process=pin, owner_guard=sup.owner_current)
            identity = sup.effect_identity("p0")
            obs = _terminal(ad, identity)
            assert ordered == ["pin"] and obs["quiescent"] is True
            assert obs["outcome"] == "succeeded" and obs["reported_usage"] == 18
            argv = calls(engine)[0]
            assert os.path.normcase(os.path.realpath(argv[argv.index("-C") + 1])) == \
                os.path.normcase(os.path.realpath(tmp_path / "repo"))
            assert "--oneshot" in argv and "--write" not in argv and "--tool-mode" not in argv
            assert argv[argv.index("--launch-id") + 1] == identity["launch_id"]
            assert argv[argv.index("--skill") + 1] == "ponytail"
            assert "--allow-prompt-tools" not in argv and len(calls(engine)) == 1
            with pytest.raises(sh.HarnessError):
                ad.launch(effect, "synthetic brief", on_process=pin, owner_guard=sup.owner_current)
            assert len(calls(engine)) == 1
        finally:
            ad.close()


@win
def test_failed_durable_callback_never_resumes_the_suspended_process(tmp_path):
    ad, state, engine = _ready(tmp_path)
    with state as sup:
        effect = sup.admit_planner("p0", "run-" + uuid.uuid4().hex, 10)
        try:
            with pytest.raises(sh.HarnessError):
                ad.launch(effect, "synthetic brief", on_process=lambda *a: (_ for _ in ()).throw(
                    RuntimeError("synthetic callback failure")), owner_guard=sup.owner_current)
            assert calls(engine) == []
        finally:
            ad.close()


@win
def test_deadline_crossed_during_durable_pin_cannot_resume_any_engine_code(tmp_path):
    ad, state, engine = _ready(tmp_path)
    clock = type("Clock", (), {"t": 100.0, "__call__": lambda self: self.t})()
    ad.clock = clock
    with state as sup:
        effect = sup.admit_planner("p0", "run-" + uuid.uuid4().hex, 10)

        def pin(*args):
            result = sup.record_process_identity(*args)
            clock.t = sup.envelope.deadline
            return result

        try:
            with pytest.raises(sh.HarnessError):
                ad.launch(effect, "synthetic brief", on_process=pin, owner_guard=sup.owner_current)
            assert calls(engine) == []
        finally:
            try:
                ad.cancel(sup.effect_identity("p0"), sup.owner_current)
            except sh.HarnessError:
                pass
            ad.close()


@win
def test_resume_reopens_exact_live_job_without_another_dispatch(tmp_path):
    ad, state, engine = _ready(tmp_path, sleep=0.8)
    with state as sup:
        effect = sup.admit_planner("p0", "run-" + uuid.uuid4().hex, 10)
        ad.launch(effect, "synthetic brief", on_process=sup.record_process_identity,
                  owner_guard=sup.owner_current)
        identity = sup.effect_identity("p0")
        ad.close()
        resumed = adapter(tmp_path, sup.envelope, engine, clock=lambda: 100.0)
        try:
            resumed.preflight()
            assert resumed.observe(identity)["state"] in ("RUNNING", "UNKNOWN")
            assert _terminal(resumed, identity)["quiescent"] is True
            assert len(calls(engine)) == 1
        finally:
            resumed.close()


@win
def test_plan_codec_computes_owned_objective_digest_without_persisting_prose_centrally(tmp_path):
    body = {"plan_revision": 0, "tasks": [{"task_id": "W1", "depends_on": [],
            "objective": "synthetic-private-canary", "criteria": ["C1"], "hypothesis_ids": []}]}
    ad, state, engine = _ready(tmp_path, plan=json.dumps(body))
    with state as sup:
        effect = sup.admit_planner("p0", "run-" + uuid.uuid4().hex, 10)
        try:
            ad.launch(effect, "synthetic brief", on_process=sup.record_process_identity,
                      owner_guard=sup.owner_current)
            identity = sup.effect_identity("p0")
            _terminal(ad, identity)
            raw, bindings = ad.read_plan(identity, 0)
            plan = sup.record_plan("p0", raw, usage=None)
            assert ad.objective(identity, plan.tasks[0]) == "synthetic-private-canary"
            assert set(bindings) == {"W1"} and b"synthetic-private-canary" not in raw
            assert b"synthetic-private-canary" not in (
                tmp_path / "state" / load("supervisor_journal").LOG_NAME).read_bytes()
            assert ad.read_plan(identity, 0)[0] == raw  # idempotent recovery, no new call
        finally:
            ad.close()


@win
@pytest.mark.parametrize("damage", ["receipt_schema", "receipt_pid", "foreign_index", "foreign_reason", "birth"])
def test_forged_metadata_never_becomes_a_terminal_observation(tmp_path, damage):
    ad, state, engine = _ready(tmp_path)
    with state as sup:
        effect = sup.admit_planner("p0", "run-" + uuid.uuid4().hex, 10)
        try:
            ad.launch(effect, "synthetic brief", on_process=sup.record_process_identity,
                      owner_guard=sup.owner_current)
            identity = sup.effect_identity("p0")
            _terminal(ad, identity)
            paths = ad._paths(identity)
            if damage.startswith("receipt") or damage == "foreign_index":
                rows = [json.loads(line) for line in paths["receipt"].read_text().splitlines()]
                ready = next(r for r in rows if r["state"] == "ready")
                if damage == "receipt_schema":
                    ready["schema"] = True
                elif damage == "receipt_pid":
                    ready["engine_pid"] += 1
                else:
                    ready["index_paths"] = [str(tmp_path / "unrelated-index")]
                paths["receipt"].write_text("\n".join(json.dumps(r) for r in rows) + "\n")
            elif damage == "foreign_reason":
                rows = [json.loads(line) for line in paths["index"].read_text().splitlines()]
                rows[-1]["stop_reason"] = "synthetic-private-canary"
                paths["index"].write_text("\n".join(json.dumps(r) for r in rows) + "\n")
            else:
                identity["process"] = {**identity["process"], "birth": "winft:1"}
            observed = ad.observe(identity)
            assert observed["state"] == "UNKNOWN"
            assert "synthetic-private-canary" not in json.dumps(observed)
        finally:
            ad.close()


@win
def test_worker_argv_and_real_bound_collection_keep_prose_out_of_metadata(tmp_path):
    ad, state, engine = _ready(tmp_path, worker_patch="", findings="synthetic-private-canary")
    with state as sup:
        contract = load("supervisor_contract")
        sup.admit_planner("p0", "plan-" + uuid.uuid4().hex, 10)
        raw = contract.canonical({"schema_version": 1, "plan_revision": 0, "tasks": [{
            "task_id": "W1", "depends_on": [], "objective_digest": "0" * 64,
            "criteria": ["C1"], "hypothesis_ids": []}]})
        sup.record_plan("p0", raw, usage=0)
        attempt = "a-" + uuid.uuid4().hex
        effect = sup.admit_worker("W1", attempt, str(uuid.uuid4()), "run-" + uuid.uuid4().hex,
                                  attempt, 10)
        try:
            ad.launch(effect, "synthetic worker task", on_process=sup.record_process_identity,
                      owner_guard=sup.owner_current)
            identity = sup.effect_identity(attempt)
            assert _terminal(ad, identity)["quiescent"] is True
            result = ad.read_worker(identity)
            assert result.patch_bytes == b"" and result.findings == "synthetic-private-canary"
            assert "synthetic-private-canary" not in json.dumps(result.metadata())
            argv = calls(engine)[0]
            assert "--worktree" in argv and "--write" not in argv and "--oneshot" not in argv
            assert argv[argv.index("--tool-mode") + 1] == "native"
            assert argv[argv.index("--label") + 1] == attempt
            assert os.path.commonpath([str(ad._paths(identity)["run"]),
                                       argv[argv.index("--patch") + 1]]) == str(ad._paths(identity)["run"])
        finally:
            ad.close()

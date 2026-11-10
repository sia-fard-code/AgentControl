"""Hard controller exits at real durable cut points; only synthetic installed children run."""
import json
import hashlib
import subprocess
import sys
import time
from pathlib import Path

import pytest

from supervisor_harness_helpers import calls, make_engine, make_envelope, operator_caps
from supervisor_helpers import canonical, load

ENGINE = Path(__file__).resolve().parents[1] / "engine"
ss = load("supervisor_state")
sh = load("supervisor_harness")
win = pytest.mark.skipif(sys.platform != "win32", reason="Windows Job hard-crash recovery")

HELPER = r'''
import hashlib, json, os, sys, time
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from supervisor_contract import canonical, load_envelope
from supervisor_harness import HarnessAdapter
from supervisor_state import Supervisor
c = json.loads(Path(sys.argv[2]).read_text())
raw = Path(c["envelope"]).read_bytes()
e = load_envelope(raw, hashlib.sha256(raw).hexdigest(), c["root"])
ad = HarnessAdapter(e, c["artifacts"], capabilities=c["caps"], engine_path=c["engine"], clock=lambda:100.)
ad.preflight()
with Supervisor(c["state"], e, lambda:100., artifact_dir=c["artifacts"]) as sup:
    sup.admit_planner("p0", "plan-synthetic", 10)
    proposal = canonical({"schema_version":1,"plan_revision":0,"tasks":[{
        "task_id":"W1","depends_on":[],"objective_digest":"0"*64,
        "criteria":["C1"],"hypothesis_ids":[]}]})
    sup.record_plan("p0", proposal, 0)
    effect = sup.admit_worker("W1", "a1", c["launch"], c["run"], "a1", 10)
    if c["phase"] == "stopping":
        sup.request_stop("cancelled")
    if c["phase"] in ("intent", "stopping"):
        os._exit(17)
    ad.launch(effect, "synthetic worker task", on_process=sup.record_process_identity,
              owner_guard=sup.owner_current)
    identity = sup.effect_identity("a1")
    until = time.monotonic()+10
    while time.monotonic()<until:
        obs = ad.observe(identity)
        if c["phase"]=="ready" and obs["state"]=="RUNNING":
            sup.observe_attempt("a1","RUNNING",None,False,None)
            os._exit(17)
        if c["phase"]=="collected" and obs["state"]=="TERMINAL" and obs["quiescent"]:
            ad.read_worker(identity)
            sup.observe_attempt("a1","COLLECTED","succeeded",True,None)
            os._exit(17)
        time.sleep(.01)
    raise RuntimeError("synthetic hard-crash cut point was not reached")
'''


@win
@pytest.mark.parametrize("phase", ["intent", "ready", "collected", "stopping"])
def test_controller_hard_exit_preserves_exact_effects_and_original_limits(tmp_path, phase):
    import uuid
    env, root = make_envelope(tmp_path)
    engine = make_engine(tmp_path, sleep=3 if phase == "ready" else 0.1, worker_patch="")
    artifacts, state = tmp_path / "artifacts", tmp_path / "state"
    artifacts.mkdir()
    envelope = tmp_path / "envelope.json"
    from supervisor_helpers import envelope_bytes
    raw, _ = envelope_bytes(root, baseline_commit=env.baseline_commit, goal_digest=env.goal_digest,
        acceptance_digest=env.acceptance_digest, policy_digest=env.policy_digest,
        validator_digest=env.validator_digest, models=list(env.models),
        capabilities={k: list(v) for k, v in env.capabilities.items()})
    envelope.write_bytes(raw)
    decoded = load("supervisor_contract").load_envelope(raw, hashlib.sha256(raw).hexdigest(), root)
    assert decoded.digest == env.digest
    env = decoded
    config = {"root": str(root), "envelope": str(envelope), "engine": str(engine),
              "artifacts": str(artifacts), "state": str(state), "caps": operator_caps(),
              "phase": phase, "launch": str(uuid.uuid4()), "run": "crash-" + uuid.uuid4().hex}
    cfg, script = tmp_path / "config.json", tmp_path / "controller.py"
    cfg.write_bytes(canonical(config))
    script.write_text(HELPER)
    proc = subprocess.run([sys.executable, str(script), str(ENGINE), str(cfg)], cwd=tmp_path,
                           capture_output=True, text=True, timeout=20)
    assert proc.returncode == 17, proc.stderr
    clock = type("Clock", (), {"t": 150.0 if phase == "stopping" else 100.0,
                               "__call__": lambda self: self.t})()
    ad = sh.HarnessAdapter(env, artifacts, capabilities=operator_caps(), engine_path=engine, clock=clock)
    try:
        ad.preflight()
        with ss.Supervisor(state, env, clock, artifact_dir=artifacts) as sup:
            assert sup.counters()["attempts"] == 1 and sup.counters()["planner_calls"] == 1
            if phase in ("intent", "stopping"):
                assert calls(engine) == [] and sup.attempts["a1"]["lifecycle"] == "UNKNOWN"
                with pytest.raises(ss.StateError):
                    sup.admit_worker("W1", "a2", str(uuid.uuid4()), "new-run", "a2", 10)
                if phase == "stopping":
                    assert sup.stop_deadline() == 160 and sup.decide().kind == "wait"
                    clock.t = 160
                    assert sup.decide().outcome == "BLOCKED"
            elif phase == "ready":
                until = time.monotonic() + 10
                while time.monotonic() < until:
                    obs = ad.observe(sup.effect_identity("a1"))
                    if obs["state"] == "TERMINAL":
                        break
                    time.sleep(0.01)
                assert obs["state"] == "TERMINAL" and obs["quiescent"]
                ad.read_worker(sup.effect_identity("a1"))
                sup.observe_attempt("a1", "COLLECTED", "succeeded", True, None)
                assert len(calls(engine)) == 1 and sup.counters()["attempts"] == 1
            else:
                assert sup.attempts["a1"]["lifecycle"] == "COLLECTED"
                ad.read_worker(sup.effect_identity("a1"))  # no lost-Job re-open needed for recorded proof
                assert len(calls(engine)) == 1 and sup.counters()["attempts"] == 1
    finally:
        ad.close()

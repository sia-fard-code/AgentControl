"""V2 request/clock consumers, independent of state reducer implementation."""
import hashlib
import json
import os
import sys
import time
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from supervisor_helpers import load
from supervisor_harness_helpers import make_engine, make_envelope, operator_caps
from supervisor_v2_helpers import envelope_bytes_v2

sh = load("supervisor_harness")
contract = load("supervisor_contract")


def _adapter(tmp_path, behavior=None, **kwargs):
    old, root = make_envelope(tmp_path)
    raw, approved = envelope_bytes_v2(root, baseline_commit=old.baseline_commit,
        policy_digest=old.policy_digest, models=list(old.models),
        capabilities={k: list(v) for k, v in old.capabilities.items()})
    env = contract.load_envelope(raw, approved, root)
    ad = sh.HarnessAdapter(env, tmp_path / "artifacts", capabilities=operator_caps(),
                           engine_path=make_engine(tmp_path, **(behavior or {})), clock=lambda: 100.0, **kwargs)
    return ad


@pytest.mark.parametrize("count", [8, 16])
def test_v2_plan_codec_allows_real_eight_and_sixteen_task_metadata(tmp_path, monkeypatch, count):
    ad = _adapter(tmp_path)
    directory = Path(ad.artifact_root) / "synthetic-run"
    directory.mkdir(parents=True)
    stdout = directory / "stdout.json"
    stdout.write_text(json.dumps({"plan_revision": 4, "tasks": [{"task_id": "W" + str(n),
        "depends_on": [], "objective": "synthetic concise objective", "criteria": ["C1"],
        "hypothesis_ids": []} for n in range(count)]}))
    monkeypatch.setattr(ad, "_packet", lambda identity: ({}, {"run": directory, "stdout": stdout}))
    raw, refs = ad.read_plan({}, 4)
    assert len(contract.validate_proposal(raw, ad.envelope, 4).tasks) == count
    assert len(refs) == count and b"synthetic concise objective" not in raw


def test_no_goal_deadline_reaches_the_containment_boundary_not_none_arithmetic(tmp_path):
    entered = []

    class StopBeforeChild:
        @staticmethod
        def create(name):
            entered.append(name)
            raise sh.ProcessError("synthetic no child")

    ad = _adapter(tmp_path, job_factory=StopBeforeChild)
    ad.preflight()
    run, launch = "r-" + uuid.uuid4().hex, str(uuid.uuid4())
    layout = hashlib.sha256(contract.canonical({
        "artifact_root": os.path.normcase(ad.artifact_root)})).hexdigest()
    identity = {"call_id": "p0", "role": "planner", "run_id": run, "launch_id": launch,
                "epoch": 1, "layout": layout, "job_name": "AgentControl-" + launch,
                "transcript_ref": f"runs/{run}/{run}.md", "receipt_ref": f"runs/{run}/{launch}.receipt.json",
                "artifact_ref": f"runs/{run}/artifacts"}
    with pytest.raises(sh.HarnessError) as exc:
        ad.launch(SimpleNamespace(kind="call_planner", data=identity), "synthetic brief",
                  on_process=lambda *a: pytest.fail("there is no child"), owner_guard=lambda: True)
    assert exc.value.code == "launch_failed" and entered == [identity["job_name"]]
    ad.close()


@pytest.mark.skipif(sys.platform != "win32", reason="real Windows Job concurrency proof")
def test_eight_actual_owned_kernel_jobs_are_live_before_release_then_collect(tmp_path):
    release = tmp_path / "release-workers"
    ad = _adapter(tmp_path, behavior={"release_file": str(release), "worker_patch": ""})
    ad.preflight()
    state = load("supervisor_state")
    with state.Supervisor(tmp_path / "state", ad.envelope, lambda: 100.0,
                          artifact_dir=ad.artifact_root) as sup:
        sup.admit_planner("p0", "plan-" + uuid.uuid4().hex, 10)
        proposal = contract.canonical({"schema_version": 1, "plan_revision": 0, "tasks": [{
            "task_id": f"W{i}", "depends_on": [], "objective_digest": "0" * 64,
            "criteria": ["C1"], "hypothesis_ids": []} for i in range(8)]})
        sup.record_plan("p0", proposal, 0)
        ids = []
        try:
            for i in range(8):
                aid = "a-" + uuid.uuid4().hex
                effect = sup.admit_worker(f"W{i}", aid, str(uuid.uuid4()),
                                          "native-" + uuid.uuid4().hex, aid, 10)
                ad.launch(effect, "synthetic worker", on_process=sup.record_process_identity,
                          owner_guard=sup.owner_current)
                ids.append(aid)
            end = time.monotonic() + 10
            while time.monotonic() < end:
                ready = [ad.observe(sup.effect_identity(aid)) for aid in ids]
                if all(r["state"] == "RUNNING" for r in ready):
                    break
                time.sleep(0.01)
            assert len(ready) == 8 and all(r["state"] == "RUNNING" for r in ready)
            # Workers can own launcher/console descendants; capacity counts worker Jobs, not OS PIDs.
            assert len({sup.effect_identity(aid)["process"]["pid"] for aid in ids}) == 8
            assert all(ad._runs[sup.effect_identity(aid)["launch_id"]]["job"].contains(
                sup.effect_identity(aid)["process"]["pid"],
                int(sup.effect_identity(aid)["process"]["birth"][6:])) for aid in ids)
            assert sum(run["job"].active_count() for run in ad._runs.values()) >= 8
            assert sup.counters()["occupied"] == 8
            release.write_text("synthetic release")
            for aid in ids:
                end = time.monotonic() + 10
                while time.monotonic() < end:
                    obs = ad.observe(sup.effect_identity(aid))
                    if obs["state"] == "TERMINAL":
                        break
                    time.sleep(0.01)
                assert obs["state"] == "TERMINAL" and obs["quiescent"]
                ad.read_worker(sup.effect_identity(aid))
                sup.observe_attempt(aid, "COLLECTED", "succeeded", True, None)
            assert sup.counters()["occupied"] == 0 and sup.counters()["attempts"] == 8
        finally:
            release.touch(exist_ok=True)
            for aid in ids:
                try:
                    ad.cancel(sup.effect_identity(aid), sup.owner_current)
                except sh.HarnessError:
                    pass
            ad.close()

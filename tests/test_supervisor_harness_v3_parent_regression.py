"""Focused schema-3 parent regressions using synthetic, contained processes."""

import json

from test_supervisor_harness_v3_integration import adapter_module, finish, launch, rh, world
from supervisor_harness import HarnessError


def test_parent_launch_records_process_and_resumes(world):
    try:
        identity = launch(world)
    except HarnessError as exc:
        if exc.__context__ is not None:
            raise exc.__context__ from None
        raise
    assert identity["process"] == {"pid": 431, "birth": "winft:133801234567890123"}


def test_parent_collects_quiescent_handoff_as_unverified(world):
    identity = launch(world)
    finish(world, identity)
    packet, paths = world.ad._packet(identity)
    config = json.loads(paths["config"].read_bytes())
    record = json.loads((paths["engine_artifacts"] / rh.HANDOFF_NAME).read_bytes())
    assert adapter_module._canonical(record["evidence"]["root_identity"]) == config["candidate_root_identity"]
    world.ad._handoff_observation(identity, packet, paths, world.sup.owner_current)
    observation = world.ad.observe(identity)
    assert observation["state"] == "TERMINAL" and observation["quiescent"] is True
    try:
        result = world.ad.read_worker(identity)
    except HarnessError as exc:
        if exc.__context__ is not None:
            raise exc.__context__ from None
        raise
    assert result.worker_status == "unverified"

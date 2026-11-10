import json

import pytest

from test_supervisor_evidence_v3 import _evaluate, ev, protected  # noqa: F401 (fixture)
from test_supervisor_harness_v3_integration import finish, launch, world  # noqa: F401

_UNKNOWN = object()


@pytest.mark.parametrize("value", [1, "yes", _UNKNOWN, None, [True]])
def test_schema3_owner_guard_requires_literal_true(protected, value):  # noqa: F811
    p = protected
    facts = _evaluate(p, owner_guard=lambda: value)
    assert not facts.accepted
    assert all(c["status"] == "not_run" for c in facts.checks)
    assert p.state["launches"] == 0 and not p.out.exists()


def test_schema3_owner_guard_exception_fails_closed(protected):  # noqa: F811
    def boom():
        raise RuntimeError("synthetic")

    p = protected
    try:
        facts = _evaluate(p, owner_guard=boom)
    except ev.EvidenceError:
        facts = None
    assert facts is None or not facts.accepted
    assert p.state["launches"] == 0 and not p.out.exists()


def test_schema3_contained_runner_rejects_truthy_owner(protected):  # noqa: F811
    p = protected
    calls = []

    def flips():  # literal True for the pre-checks, truthy 1 once the runner asks
        calls.append(1)
        return True if len(calls) <= 6 else 1

    facts = _evaluate(p, owner_guard=flips)
    assert not facts.accepted and p.state["launches"] == 0


def test_schema3_literal_true_still_accepts(protected):  # noqa: F811
    assert _evaluate(protected, owner_guard=lambda: True).accepted


SENTINEL = b"SYNTHETIC_PRIVATE_ASSISTANT_TEXT"


def test_collection_copies_no_worker_stdout(world):  # noqa: F811
    identity = launch(world)
    finish(world, identity)
    _, paths = world.ad._packet(identity)
    paths["stdout"].write_bytes(SENTINEL + b"\n")
    first = world.ad.read_worker(identity)
    assert first.findings is None and first.findings_ref is None
    assert first.worker_status == "unverified"
    assert not (paths["engine_artifacts"] / "supervisor-findings.txt").exists()
    raw = paths["collection"].read_bytes()
    assert SENTINEL not in raw and b"accepted" not in raw
    meta = json.loads(raw)
    assert meta["findings_ref"] is None and meta["findings_sha256"] is None
    assert paths["stdout"].read_bytes() == SENTINEL + b"\n"
    again = world.ad.read_worker(identity)  # cached reopen stays exact
    assert again.findings is None and again.receipt_ref == first.receipt_ref
    assert again.snapshot.receipt_sha256 == first.snapshot.receipt_sha256
    assert paths["collection"].read_bytes() == raw

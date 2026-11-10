import hashlib
from types import SimpleNamespace

import pytest

from supervisor_helpers import load
from supervisor_v2_helpers import envelope_dict_v2

codec = load("supervisor_contract_v3")
contract = load("supervisor_contract")
candidate = load("candidate")
harness = load("supervisor_harness_v3")

BOUNDS = dict(max_entries=100, max_file_bytes=1000, max_total_bytes=10000,
              max_manifest_bytes=10000, max_path_bytes=512, max_seconds=10)
ENV = "a" * 64
SEED = "b" * 64


def test_context_is_closed_canonical_ordered_metadata():
    expected = hashlib.sha256(contract.canonical({
        "envelope_sha256": ENV, "seed_receipt_sha256": SEED,
        "source_attempts": ["attempt-b", "attempt-a"], "kind": "merge",
    })).hexdigest()
    for sources in (["attempt-b", "attempt-a"], ("attempt-b", "attempt-a")):
        assert codec.snapshot_context3(
            ENV, seed_receipt=SEED, source_attempts=sources, kind="merge") == expected
    assert codec.snapshot_context3(
        ENV, seed_receipt=SEED, source_attempts=["attempt-a", "attempt-b"],
        kind="merge") != expected
    assert codec.snapshot_context3(
        ENV, seed_receipt=None, source_attempts=(), kind="initial")
    with pytest.raises(TypeError):
        codec.snapshot_context3(
            ENV, seed_receipt=None, source_attempts=[], kind="initial", nonce="no")


@pytest.mark.parametrize("envelope,seed,sources,kind", [
    ("A" * 64, None, [], "initial"),
    (ENV, "B" * 64, ["a"], "worker"),
    (ENV, None, ["a"], "initial"),
    (ENV, SEED, [], "initial"),
    (ENV, None, ["a"], "worker"),
    (ENV, SEED, [], "merge"),
    (ENV, SEED, ["a", "b"], "worker"),
    (ENV, SEED, ["a", "a"], "merge"),
    (ENV, SEED, ["bad/id"], "worker"),
    (ENV, SEED, ["a\n"], "worker"),
    (ENV, SEED, {"a"}, "worker"),
    (ENV, SEED, ["a"], "unknown"),
])
def test_context_rejects_invalid_lineage(envelope, seed, sources, kind):
    with pytest.raises(codec.ContractError):
        codec.snapshot_context3(
            envelope, seed_receipt=seed, source_attempts=sources, kind=kind)


def _record(snapshot, ref, *, seed=None, sources=(), kind="initial"):
    return dict(candidate=snapshot.manifest_sha256,
                receipt_digest=snapshot.receipt_sha256, snapshot_ref=ref,
                seed=seed, scope_digest=snapshot.scope_sha256,
                source_attempts=list(sources), effects_known=True, kind=kind,
                revision=2, epoch=1)


def test_parent_scope_identity_uses_transport_utf8_canonicalization(tmp_path):
    root = tmp_path / "data"
    (root / "données").mkdir(parents=True)
    (root / "données" / "synthetic.bin").write_bytes(b"\x00synthetic\xff")
    snapshot = candidate.capture(root, tmp_path / "snapshot", bounds=candidate.SnapshotBounds(),
                                 inputs=("données",), owner_guard=lambda: True,
                                 strict_layout=True)
    adapter = object.__new__(harness.ResilientHarnessAdapter)
    adapter.envelope = SimpleNamespace(candidate=SimpleNamespace(inputs=("données",)))
    assert adapter._scope() == snapshot.scope_sha256


def _lineage(tmp_path):
    root = tmp_path / "project"
    (root / "src").mkdir(parents=True)
    (root / "src" / "value.txt").write_bytes(b"synthetic\n")
    store = tmp_path / "owned"
    (store / "snapshots").mkdir(parents=True)
    bounds = candidate.SnapshotBounds(**BOUNDS)
    initial = candidate.capture(
        root, store / "snapshots" / "initial", bounds=bounds, inputs=("src",),
        owner_guard=lambda: True, strict_layout=True)
    data = envelope_dict_v2(root)
    data.pop("baseline_commit")
    data.update(schema_version=3, continuation=dict(codec.CONTINUATION), candidate={
        "kind": "artifact-manifest", "initial_sha256": initial.manifest_sha256,
        "inputs": ["src"], "bounds": BOUNDS,
    })
    adapter = object.__new__(harness.ResilientHarnessAdapter)
    adapter.envelope = codec.load_envelope3(data, root)
    adapter.artifact_root = str(store)
    adapter._snapshot_record_resolver = None
    context = codec.snapshot_context3(
        adapter.envelope.digest, seed_receipt=initial.receipt_sha256,
        source_attempts=["attempt-a"], kind="worker")
    worker = candidate.capture(
        root, store / "snapshots" / "worker", bounds=bounds, inputs=("src",),
        owner_guard=lambda: True, seed_sha256=initial.manifest_sha256,
        strict_layout=True, context_sha256=context)
    records = {
        initial.receipt_sha256: _record(initial, "snapshots/initial"),
        worker.receipt_sha256: _record(
            worker, "snapshots/worker", seed=initial.receipt_sha256,
            sources=["attempt-a"], kind="worker"),
    }
    return adapter, initial, worker, records, root


def _reopen(adapter, worker, guard=lambda: True):
    return adapter._snapshot(
        "snapshots/worker", worker.manifest_sha256, worker.receipt_sha256,
        owner_guard=guard)


def test_equal_data_has_distinct_contexts_and_requires_explicit_context(tmp_path):
    adapter, initial, worker, _, root = _lineage(tmp_path)
    other_context = codec.snapshot_context3(
        adapter.envelope.digest, seed_receipt=initial.receipt_sha256,
        source_attempts=["attempt-b"], kind="worker")
    other = candidate.capture(
        root, tmp_path / "other", bounds=adapter._bounds(), inputs=("src",),
        owner_guard=lambda: True, seed_sha256=initial.manifest_sha256,
        strict_layout=True, context_sha256=other_context)
    assert worker.manifest_sha256 == other.manifest_sha256 == initial.manifest_sha256
    assert worker.context_sha256 != other.context_sha256
    assert worker.receipt_sha256 != other.receipt_sha256
    # Snapshot has no directory attribute; use the fixture's capture destination.
    directory = tmp_path / "owned" / "snapshots" / "worker"
    assert worker.receipt_sha256 == hashlib.sha256(
        (directory / "receipt.json").read_bytes()).hexdigest()
    for context in (None, other_context):
        with pytest.raises(candidate.CandidateError):
            candidate.open_snapshot(
                directory, bounds=adapter._bounds(),
                manifest_sha256=worker.manifest_sha256,
                receipt_sha256=worker.receipt_sha256,
                scope_sha256=worker.scope_sha256,
                seed_sha256=initial.manifest_sha256, context_sha256=context)


def test_reopen_uses_producing_record_not_successor_context(tmp_path):
    adapter, _, worker, records, _ = _lineage(tmp_path)
    adapter.set_snapshot_record_resolver(records.get)
    successor_context = codec.snapshot_context3(
        adapter.envelope.digest, seed_receipt=worker.receipt_sha256,
        source_attempts=["attempt-b"], kind="worker")
    reopened = _reopen(adapter, worker)
    assert reopened.context_sha256 == worker.context_sha256 != successor_context
    records[worker.receipt_sha256]["source_attempts"] = ["attempt-b"]
    with pytest.raises(harness.HarnessError):
        _reopen(adapter, worker)


@pytest.mark.parametrize("field,value", [
    ("receipt_digest", "c" * 64), ("candidate", "c" * 64),
    ("snapshot_ref", "snapshots/foreign"), ("scope_digest", "c" * 64),
    ("effects_known", False), ("seed", "c" * 64),
    ("source_attempts", ["attempt-a", "attempt-a"]),
])
def test_reopen_rejects_mismatched_producing_record(tmp_path, field, value):
    adapter, _, worker, records, _ = _lineage(tmp_path)
    records[worker.receipt_sha256][field] = value
    adapter.set_snapshot_record_resolver(records.get)
    with pytest.raises((harness.HarnessError, codec.ContractError)):
        _reopen(adapter, worker)


def test_reopen_requires_resolver_and_protected_seed_data_pin(tmp_path):
    adapter, initial, worker, records, _ = _lineage(tmp_path)
    with pytest.raises(harness.HarnessError):
        _reopen(adapter, worker)
    adapter.set_snapshot_record_resolver(records.get)
    seed_record = records.pop(initial.receipt_sha256)
    with pytest.raises(harness.HarnessError):
        _reopen(adapter, worker)
    records[initial.receipt_sha256] = dict(seed_record, candidate="c" * 64)
    with pytest.raises(harness.HarnessError):
        _reopen(adapter, worker)


def test_receipt_raw_pin_is_checked_before_parsing(tmp_path, monkeypatch):
    adapter, _, worker, _, _ = _lineage(tmp_path)
    # Snapshot has no directory attribute; use the fixture's capture destination.
    (tmp_path / "owned" / "snapshots" / "worker" / "receipt.json").write_bytes(b"not JSON")
    monkeypatch.setattr(
        harness.contract, "strict_json",
        lambda *a, **k: pytest.fail("parsed an unpinned receipt"))
    with pytest.raises(harness.HarnessError):
        _reopen(adapter, worker)


def test_resolver_is_bracketed_by_owner_checks(tmp_path, monkeypatch):
    adapter, _, worker, records, _ = _lineage(tmp_path)
    owned = [True]

    def resolve(pin):
        assert owned[0]
        owned[0] = False
        return records.get(pin)

    adapter.set_snapshot_record_resolver(resolve)
    monkeypatch.setattr(
        candidate, "open_snapshot",
        lambda *a, **k: pytest.fail("opened after ownership loss"))
    with pytest.raises(harness.HarnessError) as err:
        _reopen(adapter, worker, lambda: owned[0])
    assert str(err.value) == "owner_lost"


def test_v1_fixed_initial_remains_compatible_without_resolver(tmp_path):
    adapter, initial, _, _, _ = _lineage(tmp_path)
    # Snapshot has no directory attribute; use the fixture's capture destination.
    receipt = tmp_path / "owned" / "snapshots" / "initial" / "receipt.json"
    raw = receipt.read_bytes()
    assert adapter.initial_snapshot().receipt_sha256 == initial.receipt_sha256
    reopened = adapter._snapshot(
        "snapshots/initial", initial.manifest_sha256, initial.receipt_sha256,
        owner_guard=lambda: True)
    assert reopened.context_sha256 is None
    assert receipt.read_bytes() == raw


def test_worker_capture_and_cached_reopen_bind_admitted_lineage(tmp_path, monkeypatch):
    adapter, initial, _, _, root = _lineage(tmp_path)
    run = tmp_path / "owned" / "runs" / "synthetic"
    (run / "engine").mkdir(parents=True)
    (run / "stdout").write_bytes(b"")
    paths = dict(run=run, engine_artifacts=run / "engine", stdout=run / "stdout",
                 manifest=run / "manifest", candidate=root, capture=run / "collected3",
                 collection=run / "collection3.json")
    identity = dict(launch_id="launch-a", attempt_id="attempt-a", run_id="run-a",
                    seed_candidate=initial.manifest_sha256,
                    seed_receipt=initial.receipt_sha256, seed_ref="snapshots/initial")
    guard = lambda: True
    adapter._owner = lambda *a, **k: guard
    adapter._controller_pins = {"launch-a": b"controller"}
    adapter.observe = lambda *a, **k: dict(
        state="TERMINAL", quiescent=True, worker_status="complete", reason=None)
    adapter._packet = lambda *a: ({"manifest_sha256": "d" * 64}, paths)
    adapter._job = lambda *a: (SimpleNamespace(is_empty=guard), None)
    adapter._handoff_observation = lambda *a: (
        {"verification_status": "verified_by_command", "context_sha256": "f" * 64},
        b"completion", b"handoff", initial.manifest_sha256)
    m = SimpleNamespace(write_paths=("src",), commands=(), candidate_root=str(root),
                        candidate=SimpleNamespace(kind="artifact-manifest"))
    monkeypatch.setattr(harness, "_role", lambda _: ("worker", "attempt-a"))
    monkeypatch.setattr(harness.manifest, "load_manifest2", lambda *a: m)
    monkeypatch.setattr(harness.manifest, "Boundary2",
                        lambda *a, **k: SimpleNamespace(check_write=lambda root, full: True))
    first = adapter.read_worker(identity)
    second = adapter.read_worker(identity)
    expected = codec.snapshot_context3(
        adapter.envelope.digest, seed_receipt=identity["seed_receipt"],
        source_attempts=[identity["attempt_id"]], kind="worker")
    assert first.snapshot.context_sha256 == second.snapshot.context_sha256 == expected
    assert first.snapshot.receipt_sha256 == second.snapshot.receipt_sha256
    assert set(harness._data(paths["collection"])) == harness.COLLECTION_KEYS

import dataclasses
import hashlib
import json
from pathlib import Path

import pytest

from supervisor_helpers import load

cand = load("candidate")
B = cand.SnapshotBounds(max_entries=30, max_file_bytes=1024, max_total_bytes=4096,
                        max_manifest_bytes=8192, max_path_bytes=120, max_seconds=10)
A, C = "a" * 64, "c" * 64
V1_KEYS = {"schema", "manifest_sha256", "scope_sha256", "seed_sha256",
           "git_pointer_sha256", "file_count", "total_file_bytes", "platform"}


@pytest.fixture(autouse=True)
def fixed_clock(monkeypatch):
    monkeypatch.setattr(cand.time, "monotonic", lambda: 100.0)


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False).encode("utf-8")


def tree(root, *, changed=False):
    root.mkdir()
    (root / "binary").write_bytes(b"\x00\xff\x80\r\n")
    (root / ("created" if changed else "deleted")).write_bytes(b"new" if changed else b"old")
    return root


def cap(root, dest, **kwargs):
    return cand.capture(root, dest, bounds=B, inputs=(".",), owner_guard=lambda: True,
                        **kwargs)


def merge(seed, results, dest, **kwargs):
    return cand.merge(seed, results, dest, bounds=B, allowed_write_paths=(".",),
                      owner_guard=lambda: True, **kwargs)


def opened(snapshot, **kwargs):
    return cand.open_snapshot(snapshot.directory, bounds=B,
                              manifest_sha256=snapshot.manifest_sha256,
                              receipt_sha256=snapshot.receipt_sha256,
                              scope_sha256=snapshot.scope_sha256,
                              seed_sha256=snapshot.seed_sha256, **kwargs)


def receipt_bytes(snapshot):
    return (Path(snapshot.directory) / "receipt.json").read_bytes()


def assert_receipt(snapshot, context):
    raw = receipt_bytes(snapshot)
    expected = {"schema": "candidate-receipt-1" if context is None else "candidate-receipt-2",
                "manifest_sha256": snapshot.manifest_sha256,
                "scope_sha256": snapshot.scope_sha256,
                "seed_sha256": snapshot.seed_sha256,
                "git_pointer_sha256": None, "file_count": snapshot.file_count,
                "total_file_bytes": snapshot.total_file_bytes, "platform": cand.PLATFORM}
    if context is not None:
        expected["context_sha256"] = context
    assert set(json.loads(raw)) == V1_KEYS | ({"context_sha256"} if context is not None else set())
    assert raw == canonical(expected)
    assert snapshot.receipt_sha256 == sha(raw)
    assert snapshot.manifest_sha256 == sha((Path(snapshot.directory) / "manifest.json").read_bytes())
    assert snapshot.context_sha256 == context


def test_v1_exact_golden_omitted_none_and_legacy_constructor(tmp_path):
    root = tmp_path / "empty"
    root.mkdir()
    implicit = cap(root, tmp_path / "implicit")
    explicit = cap(root, tmp_path / "explicit", context_sha256=None)
    manifest = ('{"entries":[],"inputs":["."],"platform":"' + cand.PLATFORM
                + '","schema":"candidate-manifest-1"}').encode()
    golden = ('{"file_count":0,"git_pointer_sha256":null,"manifest_sha256":"'
              + sha(manifest) + '","platform":"' + cand.PLATFORM
              + '","schema":"candidate-receipt-1","scope_sha256":"'
              + sha(b'["."]') + '","seed_sha256":null,"total_file_bytes":0}').encode()
    assert cand.SCHEMA_RECEIPT == "candidate-receipt-1"
    assert receipt_bytes(implicit) == receipt_bytes(explicit) == golden
    assert implicit.receipt_sha256 == explicit.receipt_sha256 == sha(golden)
    legacy = cand.Snapshot(implicit.directory, implicit.manifest_sha256,
                           implicit.receipt_sha256, implicit.scope_sha256,
                           implicit.file_count, implicit.total_file_bytes, implicit.seed_sha256)
    assert legacy.context_sha256 is None
    assert opened(legacy) == legacy
    assert opened(legacy, context_sha256=None) == legacy
    assert_receipt(legacy, None)


def test_capture_sibling_lineages_and_exact_replay(tmp_path):
    seed = cap(tree(tmp_path / "seed-root"), tmp_path / "seed")
    root = tree(tmp_path / "worker-root", changed=True)
    first = cap(root, tmp_path / "first", seed_sha256=seed.manifest_sha256, context_sha256=A)
    sibling = cap(root, tmp_path / "sibling", seed_sha256=seed.manifest_sha256, context_sha256=C)
    replay = cap(root, tmp_path / "replay", seed_sha256=seed.manifest_sha256, context_sha256=A)
    legacy = cap(root, tmp_path / "legacy", seed_sha256=seed.manifest_sha256)
    assert len({s.manifest_sha256 for s in (first, sibling, replay, legacy)}) == 1
    assert len({s.seed_sha256 for s in (first, sibling, replay, legacy)}) == 1
    assert len({s.receipt_sha256 for s in (first, sibling, legacy)}) == 3
    assert receipt_bytes(first) == receipt_bytes(replay)
    for snapshot, context in ((seed, None), (first, A), (sibling, C), (replay, A), (legacy, None)):
        assert_receipt(snapshot, context)


@pytest.mark.parametrize("seed_context,worker_context", [(None, A), (A, None), (A, C)])
def test_singleton_merge_context_and_legacy_default(tmp_path, seed_context, worker_context):
    seed = cap(tree(tmp_path / "seed-root"), tmp_path / "seed", context_sha256=seed_context)
    worker = cap(tree(tmp_path / "worker-root", changed=True), tmp_path / "worker",
                 seed_sha256=seed.manifest_sha256, context_sha256=worker_context)
    merged = merge(seed, [worker], tmp_path / "merged", context_sha256="d" * 64)
    replay = merge(seed, [worker], tmp_path / "replay", context_sha256="d" * 64)
    legacy = merge(seed, [worker], tmp_path / "legacy")
    explicit = merge(seed, [worker], tmp_path / "explicit", context_sha256=None)
    assert merged.manifest_sha256 == worker.manifest_sha256
    assert merged.seed_sha256 == worker.seed_sha256 == seed.manifest_sha256
    assert merged.receipt_sha256 != worker.receipt_sha256
    assert receipt_bytes(merged) == receipt_bytes(replay)
    assert receipt_bytes(legacy) == receipt_bytes(explicit)
    assert_receipt(merged, "d" * 64)
    assert_receipt(legacy, None)
    assert opened(merged, context_sha256="d" * 64) == merged


@pytest.mark.parametrize("context", [None, A])
def test_strict_open_materialize_and_delta(tmp_path, context):
    seed = cap(tree(tmp_path / "seed-root"), tmp_path / "seed", context_sha256=context)
    result = cap(tree(tmp_path / "result-root", changed=True), tmp_path / "result",
                 seed_sha256=seed.manifest_sha256, context_sha256=context)
    assert opened(result, context_sha256=context) == result
    materialized = cand.materialize(result, tmp_path / "out", bounds=B, owner_guard=lambda: True)
    assert materialized.context_sha256 == context
    assert (tmp_path / "out" / "binary").read_bytes() == b"\x00\xff\x80\r\n"
    assert (tmp_path / "out" / "created").read_bytes() == b"new"
    assert not (tmp_path / "out" / "deleted").exists()
    changes = {p: (old, new) for p, old, new in cand.delta(seed, result, bounds=B)}
    assert set(changes) == {"created", "deleted"}
    assert changes["created"][0] is None and changes["deleted"][1] is None
    assert cand.delta(result, result, bounds=B) == ()
    original = receipt_bytes(result)
    wrong_contexts = [None, C] if context is not None else [A]
    for wrong in wrong_contexts:
        with pytest.raises(cand.CandidateError, match="^tampered$"):
            opened(result, context_sha256=wrong)
        if wrong is None:
            with pytest.raises(cand.CandidateError, match="^tampered$"):
                opened(result)
        bad = dataclasses.replace(result, context_sha256=wrong)
        with pytest.raises(cand.CandidateError, match="^tampered$"):
            cand.materialize(bad, tmp_path / "rejected", bounds=B, owner_guard=lambda: True)
        with pytest.raises(cand.CandidateError, match="^tampered$"):
            cand.delta(seed, bad, bounds=B)
        with pytest.raises(cand.CandidateError, match="^tampered$"):
            cand.delta(bad, result, bounds=B)
    assert receipt_bytes(result) == original
    assert not (tmp_path / "rejected").exists()


BAD_CONTEXTS = ["", "A" * 64, "a" * 63, "a" * 65, "g" * 64, "a" * 63 + "\n",
                b"a" * 64, 7, True, [], {}]


@pytest.mark.parametrize("bad", BAD_CONTEXTS)
@pytest.mark.parametrize("producer", ["capture", "merge"])
def test_bad_producer_context_before_staging(tmp_path, monkeypatch, producer, bad):
    root = tree(tmp_path / "root")
    seed = cap(root, tmp_path / "seed")
    before = set(tmp_path.iterdir())

    def no_stage(*args, **kwargs):
        pytest.fail("invalid context reached staging")

    monkeypatch.setattr(cand, "_Stage", no_stage)
    with pytest.raises(cand.CandidateError, match="^invalid_args$"):
        if producer == "capture":
            cap(root, tmp_path / "bad", context_sha256=bad)
        else:
            merge(seed, [], tmp_path / "bad", context_sha256=bad)
    assert set(tmp_path.iterdir()) == before


ATTACKS = ["missing_context", "null_context", "uppercase_context", "short_context",
           "typed_context", "v1_with_context", "unknown_schema", "typed_schema", "extra_key",
           "missing_key", "noncanonical", "duplicate", "receipt_pin", "manifest_pin",
           "scope_pin", "seed_pin", "context_pin", "bad_snapshot_context", "bad_manifest_hash",
           "bad_git_hash", "bad_count"]


@pytest.mark.parametrize("attack", ATTACKS)
def test_malformed_or_mispinned_receipts_fail_closed(tmp_path, attack):
    snapshot = cap(tree(tmp_path / "root"), tmp_path / "snapshot", context_sha256=A)
    original = receipt_bytes(snapshot)
    receipt = json.loads(original)
    if attack == "missing_context":
        receipt.pop("context_sha256")
    elif attack == "null_context":
        receipt["context_sha256"] = None
    elif attack == "uppercase_context":
        receipt["context_sha256"] = "A" * 64
    elif attack == "short_context":
        receipt["context_sha256"] = "a" * 63
    elif attack == "typed_context":
        receipt["context_sha256"] = 1
    elif attack == "v1_with_context":
        receipt["schema"] = "candidate-receipt-1"
    elif attack == "unknown_schema":
        receipt["schema"] = "candidate-receipt-3"
    elif attack == "typed_schema":
        receipt["schema"] = ["candidate-receipt-2"]
    elif attack == "extra_key":
        receipt["authority"] = True
    elif attack == "missing_key":
        receipt.pop("platform")
    elif attack == "bad_manifest_hash":
        receipt["manifest_sha256"] = C
    elif attack == "bad_git_hash":
        receipt["git_pointer_sha256"] = "G" * 64
    elif attack == "bad_count":
        receipt["file_count"] = True
    raw = canonical(receipt)
    if attack == "noncanonical":
        raw += b"\n"
    elif attack == "duplicate":
        raw = raw[:-1] + b',"context_sha256":"' + A.encode() + b'"}'
    (Path(snapshot.directory) / "receipt.json").write_bytes(raw)
    snapshot = dataclasses.replace(snapshot, receipt_sha256=sha(raw))
    pin_fields = {"receipt_pin": ("receipt_sha256", C), "manifest_pin": ("manifest_sha256", C),
                  "scope_pin": ("scope_sha256", C), "seed_pin": ("seed_sha256", C),
                  "context_pin": ("context_sha256", C),
                  "bad_snapshot_context": ("context_sha256", "A" * 64)}
    if attack in pin_fields:
        field, value = pin_fields[attack]
        snapshot = dataclasses.replace(snapshot, **{field: value})
    before = set(tmp_path.iterdir())
    with pytest.raises(cand.CandidateError, match="^tampered$"):
        opened(snapshot, context_sha256=snapshot.context_sha256)
    with pytest.raises(cand.CandidateError, match="^tampered$"):
        cand.materialize(snapshot, tmp_path / "out", bounds=B, owner_guard=lambda: True)
    with pytest.raises(cand.CandidateError, match="^tampered$"):
        cand.delta(snapshot, snapshot, bounds=B)
    with pytest.raises(cand.CandidateError, match="^tampered$"):
        merge(snapshot, [], tmp_path / "merged", context_sha256=C)
    assert set(tmp_path.iterdir()) == before
    assert receipt_bytes(snapshot) == raw

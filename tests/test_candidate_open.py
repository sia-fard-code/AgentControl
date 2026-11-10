import dataclasses

import pytest

from test_candidate_snapshots import B, OK, cand, cap, tree


def test_open_requires_journal_anchored_transport_and_content_pins(tmp_path):
    snapshot = cap(tree(tmp_path / "s", {"a": b"\x00\xff"}), tmp_path / "store")
    reopened = cand.open_snapshot(snapshot.directory, bounds=B,
                                  manifest_sha256=snapshot.manifest_sha256,
                                  receipt_sha256=snapshot.receipt_sha256,
                                  scope_sha256=snapshot.scope_sha256, seed_sha256=None)
    assert reopened == snapshot
    cand.materialize(reopened, tmp_path / "m", bounds=B, owner_guard=OK)
    assert (tmp_path / "m/a").read_bytes() == b"\x00\xff"


@pytest.mark.parametrize("field", ["manifest_sha256", "receipt_sha256", "scope_sha256",
                                   "seed_sha256"])
def test_wrong_protected_snapshot_pin_is_rejected(tmp_path, field):
    snapshot = cap(tree(tmp_path / "s", {"a": b"a"}), tmp_path / "store")
    args = {"manifest_sha256": snapshot.manifest_sha256,
            "receipt_sha256": snapshot.receipt_sha256,
            "scope_sha256": snapshot.scope_sha256, "seed_sha256": None}
    args[field] = "f" * 64
    with pytest.raises(cand.CandidateError):
        cand.open_snapshot(snapshot.directory, bounds=B, **args)

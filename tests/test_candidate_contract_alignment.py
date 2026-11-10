"""Align bounded approval metadata with the actual snapshot port."""
import pytest

from supervisor_helpers import load
from test_supervisor_state_v3 import envelope

cand = load("candidate")
codec = load("supervisor_contract_v3")


def test_metadata_allowance_is_independent_of_file_payload_allowance(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    data = codec.dump_envelope3(envelope(project))
    data["candidate"]["inputs"] = ["."]
    data["candidate"]["bounds"].update(max_file_bytes=4, max_total_bytes=4,
                                        max_manifest_bytes=4096)
    approved = codec.load_envelope3(data, project)
    snapshot = cand.capture(project, tmp_path / "snapshot",
                            bounds=cand.SnapshotBounds(**dict(approved.candidate.bounds)),
                            inputs=approved.candidate.inputs, owner_guard=lambda: True)
    assert snapshot.total_file_bytes == 0 and snapshot.file_count == 0


def test_input_work_uses_declared_bounds_not_an_unmeasured_256_limit(tmp_path):
    data = codec.dump_envelope3(envelope(tmp_path))
    data["candidate"]["inputs"] = [f"p{i:04}" for i in range(300)]
    data["candidate"]["bounds"].update(max_entries=1000, max_manifest_bytes=10000)
    assert len(codec.load_envelope3(data, tmp_path).candidate.inputs) == 300
    data["candidate"]["bounds"]["max_entries"] = 10
    with pytest.raises(codec.ContractError):
        codec.load_envelope3(data, tmp_path)


def test_readonly_candidate_identity_matches_capture_without_writes(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    (root / "a").write_bytes(b"\x00\xff")
    bounds = cand.SnapshotBounds()
    snapshot = cand.capture(root, tmp_path / "s", bounds=bounds, inputs=(".",),
                            owner_guard=lambda: True)
    before = sorted(str(p.relative_to(tmp_path)) for p in tmp_path.rglob("*"))
    identity = cand.identity(root, bounds=bounds, inputs=(".",), owner_guard=lambda: True)
    assert identity == snapshot.manifest_sha256
    assert sorted(str(p.relative_to(tmp_path)) for p in tmp_path.rglob("*")) == before

"""Journal compatibility for envelope schema 3: durability codec only, synthetic tmp_path data."""
import os
from types import SimpleNamespace

import pytest

from supervisor_helpers import digest, envelope_bytes, load
from supervisor_v2_helpers import envelope_bytes_v2, envelope_dict_v2

contract = load("supervisor_contract")
v3 = load("supervisor_contract_v3")
sj = load("supervisor_journal")

SCHEMAS = {"admit": {"attempt_id": sj.token, "slot": sj.integer, "usage": sj.optional_int}}
BOUNDS = {"max_entries": 1000, "max_file_bytes": 1 << 20, "max_total_bytes": 1 << 24,
          "max_manifest_bytes": 1 << 20, "max_path_bytes": 1024, "max_seconds": 60}


def _env3(root, **overrides):
    d = envelope_dict_v2(root)
    del d["baseline_commit"]
    d.update(schema_version=3, continuation={"profile": "resilient-v1", "admission": "approved-task"},
             candidate={"kind": "git", "baseline_commit": "b" * 40, "initial_sha256": digest("snap"),
                        "inputs": ["docs", "src"], "bounds": dict(BOUNDS)})
    d.update(overrides)
    return v3.load_envelope3(d)


def _open(state, env):
    return sj.Journal(state, env, SCHEMAS, clock=lambda: 100.0)


def _admit(j, i):
    return j.transact("admit", {"attempt_id": f"a{i}", "slot": i % 8, "usage": None})


def _admits(j):
    return [r.data["attempt_id"] for r in j.records if r.kind == "admit"]


def test_v3_create_reopen_new_epoch_replays_and_keeps_nulls(tmp_path):
    env, state = _env3(tmp_path), tmp_path / "s"
    with _open(state, env) as j:
        first = j.epoch
        _admit(j, 1)
    with _open(state, env) as j:
        assert j.epoch > first and _admits(j) == ["a1"]
        assert [r for r in j.records if r.kind == "admit"][0].data["usage"] is None
        assert sum(r.kind == "genesis" for r in j.records) == 1
        assert j.records[0].data["version"] == sj.VERSION == 1
        assert j._limits() == (sj.MAX_V2_RECORD_BYTES, sj.MAX_V2_SEGMENT_BYTES)


def test_v3_uses_v2_segments_and_commit_head(tmp_path, monkeypatch):
    monkeypatch.setattr(sj, "MAX_V2_SEGMENT_BYTES", 1024)
    env, state = _env3(tmp_path), tmp_path / "s"
    with _open(state, env) as j:
        for i in range(20):
            _admit(j, i)
    names = os.listdir(state)
    assert "journal.1.log" in names and "commit.head" in names
    with _open(state, env) as j:
        assert _admits(j) == [f"a{i}" for i in range(20)]


def test_v3_fsync_failure_fences_owner(tmp_path, monkeypatch):
    env, state = _env3(tmp_path), tmp_path / "s"
    with _open(state, env) as j:
        _admit(j, 1)
        def boom(fd):
            raise OSError("synthetic fsync failure")
        monkeypatch.setattr(sj.os, "fsync", boom)
        with pytest.raises(sj.JournalError):
            _admit(j, 2)
        monkeypatch.undo()
        assert not j.owner_current()
        with pytest.raises(sj.JournalError):
            _admit(j, 3)


def test_v3_witness_dir_flush_failure_holds(tmp_path, monkeypatch):
    monkeypatch.setattr(sj, "MAX_V2_SEGMENT_BYTES", 1024)
    env, state = _env3(tmp_path), tmp_path / "s"
    with _open(state, env) as j:
        def boom(path):
            raise OSError("synthetic directory flush failure")
        monkeypatch.setattr(sj, "_fsync_dir", boom)
        with pytest.raises(sj.JournalError):
            for i in range(40):
                _admit(j, i)
        assert not j.owner_current()


def test_v3_digest_mismatch_and_stale_owner_reject(tmp_path):
    env, state = _env3(tmp_path), tmp_path / "s"
    with _open(state, env) as j:
        _admit(j, 1)
    with pytest.raises(sj.JournalError):
        with _open(state, _env3(tmp_path, max_usage=5000)):
            pass
    stale = j
    with _open(state, env):
        pass
    with pytest.raises(sj.JournalError):
        _admit(stale, 2)


def test_v1_v2_defaults_unchanged_and_unknown_version_rejected(tmp_path):
    e1 = contract.load_envelope(*envelope_bytes(tmp_path), tmp_path)
    e2 = contract.load_envelope(*envelope_bytes_v2(tmp_path), tmp_path)
    assert _open(tmp_path / "a", e1)._limits() == (sj.MAX_RECORD_BYTES, sj.MAX_LOG_BYTES)
    assert _open(tmp_path / "b", e2)._limits() == (sj.MAX_V2_RECORD_BYTES, sj.MAX_V2_SEGMENT_BYTES)
    for bad in (99, 0, True, "3"):
        with pytest.raises(sj.JournalError):
            _open(tmp_path / "c", SimpleNamespace(schema_version=bad))

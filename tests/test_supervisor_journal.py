import subprocess
import sys

import pytest

from supervisor_helpers import ENGINE, envelope_bytes, load

contract = load("supervisor_contract")
sj = load("supervisor_journal")

SCHEMAS = {"admit": {"attempt_id": sj.token, "slot": sj.integer, "usage": sj.optional_int}}


def _env(root, **overrides):
    raw, approved = envelope_bytes(root, **overrides)
    return contract.load_envelope(raw, approved, root)


def _open(state, env, clock=lambda: 100.0):
    return sj.Journal(state, env, SCHEMAS, clock=clock)


def test_append_replay_survives_reopen_and_bumps_epoch(tmp_path):
    env = _env(tmp_path)
    with _open(tmp_path / "s", env) as j:
        first_epoch = j.epoch
        j.transact("admit", {"attempt_id": "a1", "slot": 1, "usage": None})
    with _open(tmp_path / "s", env) as j:
        admits = [r for r in j.records if r.kind == "admit"]
        assert [(r.data["attempt_id"], r.data["usage"]) for r in admits] == [("a1", None)]
        assert j.epoch == first_epoch + 1


@pytest.mark.parametrize("data", [
    {"attempt_id": "a1", "slot": 1},                                    # missing key
    {"attempt_id": "a1", "slot": 1, "usage": None, "brief": "x"},       # unknown key
    {"attempt_id": "synthetic prose with spaces", "slot": 1, "usage": None},
    {"attempt_id": "a1\n", "slot": 1, "usage": None},
    {"attempt_id": "a1", "slot": 1.5, "usage": None},
    {"attempt_id": "a1", "slot": True, "usage": None},
])
def test_only_allowlisted_metadata_is_persisted(tmp_path, data):
    with _open(tmp_path / "s", _env(tmp_path)) as j:
        with pytest.raises(sj.JournalError):
            j.transact("admit", data)
        with pytest.raises(sj.JournalError):
            j.transact("unknown_kind", {})
        assert not [r for r in j.records if r.kind == "admit"]


def test_digest_validator_rejects_trailing_control_character(tmp_path):
    with pytest.raises(sj.JournalError):
        sj.digest("0" * 64 + "\n")


def test_second_owner_in_process_is_refused(tmp_path):
    env = _env(tmp_path)
    with _open(tmp_path / "s", env):
        with pytest.raises(sj.JournalError):
            _open(tmp_path / "s", env).__enter__()


def test_stale_handle_cannot_append_after_release(tmp_path):
    env = _env(tmp_path)
    old = _open(tmp_path / "s", env).__enter__()
    old.close()
    with _open(tmp_path / "s", env) as new:
        with pytest.raises(sj.JournalError):
            old.transact("admit", {"attempt_id": "a9", "slot": 9, "usage": None})
        assert not [r for r in new.records if r.kind == "admit"]


def test_live_effect_guard_is_epoch_bound_and_false_after_close(tmp_path):
    j = _open(tmp_path / "s", _env(tmp_path)).__enter__()
    epoch = j.epoch
    assert j.owner_current(epoch) is True
    assert j.owner_current(epoch + 1) is False
    j.close()
    assert j.owner_current(epoch) is False


def test_replaced_lock_path_never_retains_effect_authority(tmp_path):
    with _open(tmp_path / "s", _env(tmp_path)) as j:
        lock = tmp_path / "s" / sj.LOCK_NAME
        try:
            lock.rename(lock.with_suffix(".old"))
        except PermissionError:
            # Windows sharing can itself prevent replacement of the held inode.
            assert j.owner_current(j.epoch) is True
            return
        lock.write_bytes(b"new unrelated lock")
        assert j.owner_current(j.epoch) is False
        with pytest.raises(sj.JournalError):
            j.transact("admit", {"attempt_id": "a1", "slot": 1, "usage": None})


def test_other_envelope_digest_is_refused(tmp_path):
    with _open(tmp_path / "s", _env(tmp_path)):
        pass
    with pytest.raises(sj.JournalError):
        _open(tmp_path / "s", _env(tmp_path, orchestration_id="orch-other")).__enter__()


@pytest.mark.parametrize("damage", ["torn", "flip", "garbage"])
def test_corrupt_journal_fails_closed_without_rewrite(tmp_path, damage):
    env = _env(tmp_path)
    with _open(tmp_path / "s", env) as j:
        j.transact("admit", {"attempt_id": "a1", "slot": 1, "usage": None})
    log = tmp_path / "s" / sj.LOG_NAME
    raw = log.read_bytes()
    bad = {"torn": raw[:-7], "flip": raw.replace(b'"a1"', b'"a2"'),
           "garbage": raw + b'{"v":1}\n'}[damage]
    log.write_bytes(bad)
    with pytest.raises(sj.JournalError):
        _open(tmp_path / "s", env).__enter__()
    assert log.read_bytes() == bad


CHILD = r"""
import os, sys
sys.path[:0] = [sys.argv[1], sys.argv[2]]
from supervisor_helpers import envelope_bytes
import supervisor_contract as c, supervisor_journal as sj
root, state, mode = sys.argv[3], sys.argv[4], sys.argv[5]
raw, approved = envelope_bytes(root)
env = c.load_envelope(raw, approved, root)
schemas = {"admit": {"attempt_id": sj.token, "slot": sj.integer, "usage": sj.optional_int}}
try:
    j = sj.Journal(state, env, schemas, clock=lambda: 100.0).__enter__()
except sj.JournalError:
    sys.exit(3)
j.transact("admit", {"attempt_id": "child", "slot": 1, "usage": None})
if mode == "crash":
    os._exit(0)  # no close/unlock: simulates a hard supervisor crash
j.close()
"""


def _child(tmp_path, mode):
    tests_dir = str(ENGINE.parent / "tests")
    return subprocess.run([sys.executable, "-c", CHILD, str(ENGINE), tests_dir, str(tmp_path),
                           str(tmp_path / "s"), mode], capture_output=True, timeout=60)


def test_competing_owner_process_is_refused(tmp_path):
    env = _env(tmp_path)
    with _open(tmp_path / "s", env) as j:
        result = _child(tmp_path, "normal")
        assert result.returncode == 3, result.stderr
        assert not [r for r in j.records if r.kind == "admit"]
    with _open(tmp_path / "s", env) as j:
        assert not [r for r in j.records if r.kind == "admit"]


def test_hard_crash_replay_keeps_records_and_lock_is_released(tmp_path):
    env = _env(tmp_path)
    result = _child(tmp_path, "crash")
    assert result.returncode == 0, result.stderr
    with _open(tmp_path / "s", env) as j:
        assert [r.data["attempt_id"] for r in j.records if r.kind == "admit"] == ["child"]
        j.transact("admit", {"attempt_id": "a2", "slot": 2, "usage": 5})
    with _open(tmp_path / "s", env) as j:
        assert [r.data["slot"] for r in j.records if r.kind == "admit"] == [1, 2]


def test_clock_regression_holds(tmp_path):
    env = _env(tmp_path)
    with _open(tmp_path / "s", env, clock=lambda: 500.0):
        pass
    with pytest.raises(sj.JournalError):
        _open(tmp_path / "s", env, clock=lambda: 400.0).__enter__()


def test_missing_birth_identity_fails_closed(tmp_path, monkeypatch):
    monkeypatch.setattr(sj, "_birth_identity", lambda: None)
    with pytest.raises(sj.JournalError):
        _open(tmp_path / "s", _env(tmp_path)).__enter__()
    monkeypatch.undo()
    with _open(tmp_path / "s", _env(tmp_path)) as j:  # lock released; nothing owner-less persisted
        assert all(r.data["birth"] for r in j.records if r.kind == "owner")


def test_log_size_cap_checked_before_write(tmp_path, monkeypatch):
    env = _env(tmp_path)
    with _open(tmp_path / "s", env) as j:
        monkeypatch.setattr(sj, "MAX_LOG_BYTES", j._size + 10)
        with pytest.raises(sj.JournalError):
            j.transact("admit", {"attempt_id": "a1", "slot": 1, "usage": None})
        monkeypatch.undo()
    with _open(tmp_path / "s", env) as j:
        assert not [r for r in j.records if r.kind == "admit"]


def test_schema_diagnostic_does_not_echo_untrusted_kind(tmp_path):
    with _open(tmp_path / "s", _env(tmp_path)) as j:
        with pytest.raises(sj.JournalError) as exc:
            j.transact("synthetic-untrusted-kind", {})
        assert "synthetic-untrusted-kind" not in str(exc.value)

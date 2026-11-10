import os

import pytest

from supervisor_helpers import envelope_bytes, load
from supervisor_v2_helpers import envelope_bytes_v2

contract = load("supervisor_contract")
sj = load("supervisor_journal")

SCHEMAS = {"admit": {"attempt_id": sj.token, "slot": sj.integer, "usage": sj.optional_int}}


def _env(root, version=2):
    raw, approved = (envelope_bytes_v2(root) if version == 2 else envelope_bytes(root))
    return contract.load_envelope(raw, approved, root)


def _open(state, env, clock=lambda: 100.0):
    return sj.Journal(state, env, SCHEMAS, clock=clock)


def _admit(j, i):
    return j.transact("admit", {"attempt_id": f"a{i}", "slot": i % 8, "usage": None})


def _admits(j):
    return [r.data["attempt_id"] for r in j.records if r.kind == "admit"]


@pytest.fixture
def small_segments(monkeypatch):
    monkeypatch.setattr(sj, "MAX_V2_SEGMENT_BYTES", 1024)


def test_v2_rotates_past_segment_size_and_replays_all(tmp_path, small_segments):
    env, state = _env(tmp_path), tmp_path / "s"
    with _open(state, env) as j:
        for i in range(40):
            _admit(j, i)
        hashes = j._prev
    names = sorted(os.listdir(state))
    assert "journal.log" in names and "journal.1.log" in names and "journal.2.log" in names
    assert all(os.path.getsize(state / n) <= 1024 for n in names if n.startswith("journal"))
    with _open(state, env) as j:
        assert _admits(j) == [f"a{i}" for i in range(40)]
        assert [r.seq for r in j.records] == list(range(len(j.records)))
        assert sum(r.kind == "genesis" for r in j.records) == 1
        assert j.records[42].kind == "owner" and j._prev != hashes
        _admit(j, 40)
    with _open(state, env) as j:
        assert _admits(j)[-1] == "a40"


def _segmented(tmp_path):
    env, state = _env(tmp_path), tmp_path / "s"
    with _open(state, env) as j:
        for i in range(24):
            _admit(j, i)
        tail = j._seg
    assert tail >= 2
    return env, state, tail


def _seg(state, i):
    return state / ("journal.log" if i == 0 else f"journal.{i}.log")


def _damage_missing(state, tail):
    os.remove(_seg(state, 1))


def _damage_tamper(state, tail):
    raw = _seg(state, 1).read_bytes()
    _seg(state, 1).write_bytes(raw.replace(b'"slot":', b'"slot":1', 1))


def _damage_reorder(state, tail):
    a, b = _seg(state, 1).read_bytes(), _seg(state, 2).read_bytes()
    _seg(state, 1).write_bytes(b)
    _seg(state, 2).write_bytes(a)


def _damage_torn(state, tail):
    _seg(state, tail).write_bytes(_seg(state, tail).read_bytes()[:-1])


def _damage_junk_tail(state, tail):
    _seg(state, tail + 1).write_bytes(b'{"v":1}\n')


def _damage_gap_tail(state, tail):
    _seg(state, tail + 2).write_bytes(b"")


def _damage_foreign_name(state, tail):
    (state / "journal.01.log").write_bytes(b"")


def _damage_two_empty(state, tail):
    _seg(state, tail + 1).write_bytes(b"")
    _seg(state, tail + 2).write_bytes(b"")


@pytest.mark.parametrize("damage", [_damage_missing, _damage_tamper, _damage_reorder,
                                    _damage_torn, _damage_junk_tail, _damage_gap_tail,
                                    _damage_foreign_name, _damage_two_empty])
def test_v2_segment_damage_holds_without_rewrite(tmp_path, small_segments, damage):
    env, state, tail = _segmented(tmp_path)
    damage(state, tail)
    snap = lambda: {n: (state / n).read_bytes() for n in os.listdir(state) if n.startswith("journal")}
    before = snap()
    with pytest.raises(sj.JournalError):
        with _open(state, env):
            pass
    assert snap() == before


def test_v2_single_empty_newest_segment_is_reused_not_reset(tmp_path, small_segments):
    env, state, tail = _segmented(tmp_path)
    _seg(state, tail + 1).write_bytes(b"")  # owner died after publishing, before writing
    with _open(state, env) as j:
        assert j._seg == tail + 1 and [r.seq for r in j.records] == list(range(len(j.records)))
        _admit(j, 99)
    with _open(state, env) as j:
        assert _admits(j) == [f"a{i}" for i in range(24)] + ["a99"]
        assert sum(r.kind == "genesis" for r in j.records) == 1


def test_v2_external_next_segment_revokes_authority(tmp_path, small_segments):
    env, state, tail = _segmented(tmp_path)
    with _open(state, env) as j:
        _seg(state, j._seg + 1).write_bytes(b"")
        assert not j.owner_current()
        with pytest.raises(sj.JournalError):
            _admit(j, 50)


def test_v2_replaced_active_tail_revokes_authority(tmp_path, small_segments):
    env, state, tail = _segmented(tmp_path)
    with _open(state, env) as j:
        active = _seg(state, j._seg)
        raw = active.read_bytes()
        try:
            os.replace(active, state / "moved.bin")
        except PermissionError:
            # Windows sharing prevents replacing the held active tail; authority stays sound.
            assert j.owner_current(j.epoch) is True
            return
        active.write_bytes(raw)
        assert not j.owner_current()
        with pytest.raises(sj.JournalError):
            _admit(j, 50)


class _FailingList(list):
    def append(self, item):
        raise MemoryError("synthetic bookkeeping failure")


def test_post_fsync_bookkeeping_failure_fences_and_replay_keeps_admission(tmp_path):
    env, state = _env(tmp_path), tmp_path / "s"
    with _open(state, env) as j:
        _admit(j, 1)
        j.records = _FailingList(j.records)
        with pytest.raises(MemoryError):
            _admit(j, 2)
        assert not j.owner_current()
        with pytest.raises(sj.JournalError):
            _admit(j, 3)
    with _open(state, env) as j:
        assert _admits(j) == ["a1", "a2"]


@pytest.mark.parametrize("fault", ["fsync", "short"])
def test_write_or_fsync_fault_fences_owner(tmp_path, monkeypatch, fault):
    env, state = _env(tmp_path), tmp_path / "s"
    with _open(state, env) as j:
        _admit(j, 1)
        if fault == "fsync":
            def boom(fd):
                raise OSError("synthetic fsync failure")
            monkeypatch.setattr(sj.os, "fsync", boom)
        else:
            real = sj.os.write
            monkeypatch.setattr(sj.os, "write", lambda fd, b: real(fd, b[:5]))
        with pytest.raises(sj.JournalError):
            _admit(j, 2)
        monkeypatch.undo()
        assert not j.owner_current()
    if fault == "fsync":  # frame reached the file: replay consumes the committed admission
        with _open(state, env) as j:
            assert _admits(j) == ["a1", "a2"]
    else:  # torn tail holds for reconciliation rather than dropping or guessing
        with pytest.raises(sj.JournalError):
            with _open(state, env):
                pass


def test_rotation_publication_failure_fences_and_recovers(tmp_path, small_segments, monkeypatch):
    env, state = _env(tmp_path), tmp_path / "s"
    with _open(state, env) as j:
        def boom(path):
            raise OSError("synthetic directory flush failure")
        monkeypatch.setattr(sj, "_fsync_dir", boom)
        with pytest.raises(sj.JournalError):
            for i in range(40):
                _admit(j, i)
        done = len(_admits(j))
        assert not j.owner_current()
    monkeypatch.undo()
    with _open(state, env) as j:  # the published empty newest segment is reused
        assert _admits(j) == [f"a{i}" for i in range(done)]
        _admit(j, 99)


def test_oversized_line_rejected_with_bounded_read(tmp_path, monkeypatch):
    env, state = _env(tmp_path), tmp_path / "s"
    with _open(state, env):
        pass
    monkeypatch.setattr(sj, "MAX_V2_RECORD_BYTES", 4096)
    with open(state / "journal.log", "ab") as fh:
        fh.write(b"x" * (1024 * 1024) + b"\n")
    with pytest.raises(sj.JournalError):
        with _open(state, env):
            pass


CHILD = r"""
import os, sys
sys.path[:0] = [sys.argv[1], sys.argv[2]]
from supervisor_v2_helpers import envelope_bytes_v2
import supervisor_contract as c, supervisor_journal as sj
root, state = sys.argv[3], sys.argv[4]
raw, approved = envelope_bytes_v2(root)
env = c.load_envelope(raw, approved, root)
sj.MAX_V2_SEGMENT_BYTES = 1024
real, rotate = sj._fsync_dir, sj.Journal._rotate
def die(path):
    real(path)  # next segment durably published, frame not yet written
    sys.stdout.write(str(sum(r.kind == "admit" for r in j.records)))
    sys.stdout.flush()
    os._exit(7)
def rotating(self):
    sj._fsync_dir = die  # only the rotation's directory flush dies, not witness publication
    rotate(self)
sj.Journal._rotate = rotating
schemas = {"admit": {"attempt_id": sj.token, "slot": sj.integer, "usage": sj.optional_int}}
j = sj.Journal(state, env, schemas, clock=lambda: 100.0).__enter__()
for i in range(100):
    j.transact("admit", {"attempt_id": f"a{i}", "slot": 1, "usage": None})
"""


def test_hard_exit_during_rotation_recovers_exact_sequence(tmp_path):
    import subprocess
    import sys
    from supervisor_helpers import ENGINE
    env, state = _env(tmp_path), tmp_path / "s"
    result = subprocess.run([sys.executable, "-c", CHILD, str(ENGINE), str(ENGINE.parent / "tests"),
                             str(tmp_path), str(state)], capture_output=True, timeout=60)
    assert result.returncode == 7, result.stderr
    done = int(result.stdout)
    assert done > 0 and (state / "journal.1.log").read_bytes() == b""
    with _open(state, env) as j:
        assert _admits(j) == [f"a{i}" for i in range(done)]
        assert [r.seq for r in j.records] == list(range(len(j.records)))
        _admit(j, 999)
    stale = j
    with pytest.raises(sj.JournalError):
        stale.transact("admit", {"attempt_id": "late", "slot": 1, "usage": None})
    with _open(state, env) as j:
        assert _admits(j) == [f"a{i}" for i in range(done)] + ["a999"]


def test_actual_history_beyond_former_16mib_ceiling(tmp_path):
    env, state = _env(tmp_path), tmp_path / "s"
    schemas = {"bulk": {"ids": sj.tokens}}
    ids = lambda i: [f"t{i}-{k}-" + "x" * 110 for k in range(64)]  # synthetic bounded tokens
    n = 2400  # ~7.9KB frames: ~19MB total, beyond the 16MiB single-segment bound
    with sj.Journal(state, env, schemas, clock=lambda: 100.0) as j:
        for i in range(n):
            j.transact("bulk", {"ids": ids(i)})
        assert j._seg >= 1
    total = sum(os.path.getsize(state / x) for x in os.listdir(state) if x.startswith("journal"))
    assert total > sj.MAX_LOG_BYTES
    assert all(os.path.getsize(state / x) <= sj.MAX_V2_SEGMENT_BYTES for x in os.listdir(state))
    with sj.Journal(state, env, schemas, clock=lambda: 100.0) as j:
        bulk = [r for r in j.records if r.kind == "bulk"]
        assert len(bulk) == n and bulk[-1].data["ids"] == ids(n - 1)
        j.transact("bulk", {"ids": ids(n)})
    with sj.Journal(state, env, schemas, clock=lambda: 100.0) as j:
        assert sum(r.kind == "bulk" for r in j.records) == n + 1


def test_frame_above_64kib_is_replayable_with_the_real_v2_contract(tmp_path):
    env, state = _env(tmp_path), tmp_path / "s"
    schemas = {"bulk": {"ids": sj.tokens}}
    big = {"ids": [f"k{k}-" + "y" * 120 for k in range(64)]}
    wide = {"bulk": {f"f{n}": sj.tokens for n in range(10)}}
    data = {f"f{n}": big["ids"] for n in range(10)}  # ~80KB synthetic frame
    with sj.Journal(state, env, wide, clock=lambda: 100.0) as j:
        j.transact("bulk", data)
    with sj.Journal(state, env, wide, clock=lambda: 100.0) as j:
        assert [r.data for r in j.records if r.kind == "bulk"] == [data]


def test_frame_larger_than_segment_refuses_before_publishing_any_tail(tmp_path, small_segments):
    env, state = _env(tmp_path), tmp_path / "s"
    schema = {"bulk": {"ids": sj.tokens}}
    with sj.Journal(state, env, schema, clock=lambda: 100.0) as j:
        before = {p.name: p.read_bytes() for p in state.glob("journal*")}
        data = {"ids": ["x" * 120 for _ in range(64)]}
        with pytest.raises(sj.JournalError):
            j.transact("bulk", data)
        assert {p.name: p.read_bytes() for p in state.glob("journal*")} == before


def test_nonadjacent_foreign_tail_also_revokes_effect_authority(tmp_path):
    env, state = _env(tmp_path), tmp_path / "s"
    with _open(state, env) as j:
        (state / "journal.2.log").write_bytes(b"")
        assert j.owner_current() is False
        with pytest.raises(sj.JournalError):
            _admit(j, 1)


@pytest.mark.parametrize("damage", ["missing", "rollback"])
def test_changed_acknowledgement_witness_revokes_live_effect_authority(tmp_path, damage):
    env, state = _env(tmp_path), tmp_path / "s"
    with _open(state, env) as j:
        head = state / sj.HEAD_NAME
        previous = head.read_bytes()
        _admit(j, 1)
        if damage == "missing":
            head.unlink()
        else:
            head.write_bytes(previous)
        assert j.owner_current() is False
        with pytest.raises(sj.JournalError):
            _admit(j, 2)


def test_v1_rejects_segmented_state_instead_of_fresh_goal(tmp_path):
    env, state = _env(tmp_path, version=1), tmp_path / "s"
    with _open(state, env):
        pass
    (state / "journal.1.log").write_bytes(b"")
    with pytest.raises(sj.JournalError):
        with _open(state, env):
            pass


def test_inplace_truncation_revokes_effect_authority(tmp_path):
    env, state = _env(tmp_path), tmp_path / "s"
    with _open(state, env) as j:
        size = j._size
        _admit(j, 1)
        os.truncate(state / "journal.log", size)
        assert j.owner_current() is False
        with pytest.raises(sj.JournalError):
            _admit(j, 2)


def _hold_unchanged(state, env):
    snap = lambda: {n: (state / n).read_bytes() for n in os.listdir(state) if n != "owner.lock"}
    before = snap()
    with pytest.raises(sj.JournalError):
        with _open(state, env):
            pass
    assert snap() == before


def test_deleted_highest_segment_holds(tmp_path, small_segments):
    env, state, tail = _segmented(tmp_path)
    os.remove(_seg(state, tail))
    _hold_unchanged(state, env)


def _witness_missing(head):
    head.unlink()


def _witness_corrupt(head):
    head.write_bytes(head.read_bytes()[:-3])


def _witness_future(head):
    head.write_bytes(head.read_bytes().replace(b'"v":2', b'"v":3'))


def _rewrite(head, **changes):  # a well-formed witness naming a different acknowledged tail
    data = contract.strict_json(head.read_bytes())
    head.write_bytes(contract.canonical({**data, **changes}))


def _witness_ahead(head):
    _rewrite(head, seq=contract.strict_json(head.read_bytes())["seq"] + 1)


def _witness_wrong_hash(head):
    _rewrite(head, h="f" * 64)


def _witness_wrong_offset(head):
    _rewrite(head, off=contract.strict_json(head.read_bytes())["off"] - 1)


@pytest.mark.parametrize("damage", [_witness_missing, _witness_corrupt, _witness_future,
                                    _witness_ahead, _witness_wrong_hash, _witness_wrong_offset])
def test_missing_corrupt_future_or_unsatisfied_witness_holds(tmp_path, damage):
    env, state = _env(tmp_path), tmp_path / "s"
    with _open(state, env) as j:
        _admit(j, 1)
    damage(state / "commit.head")
    _hold_unchanged(state, env)


def test_pre_head_failure_fences_and_replay_consumes_unacknowledged_admission(tmp_path, monkeypatch):
    env, state = _env(tmp_path), tmp_path / "s"
    with _open(state, env) as j:
        _admit(j, 1)
        old = (state / "commit.head").read_bytes()
        def boom(path, head):
            raise OSError("synthetic witness failure")
        monkeypatch.setattr(sj, "_publish_head", boom)
        with pytest.raises(sj.JournalError):
            _admit(j, 2)
        assert not j.owner_current()
    monkeypatch.undo()
    assert (state / "commit.head").read_bytes() == old  # older valid witness, durable extra row
    with _open(state, env) as j:
        assert _admits(j) == ["a1", "a2"]  # consumed, never refunded


def test_post_head_directory_flush_failure_fences_and_consumes(tmp_path, monkeypatch):
    env, state = _env(tmp_path), tmp_path / "s"
    with _open(state, env) as j:
        _admit(j, 1)
        def boom(path):
            raise OSError("synthetic flush failure")
        monkeypatch.setattr(sj, "_fsync_dir", boom)
        with pytest.raises(sj.JournalError):
            _admit(j, 2)
        assert not j.owner_current()
    monkeypatch.undo()
    with _open(state, env) as j:
        assert _admits(j) == ["a1", "a2"]


@pytest.mark.parametrize("version", [1, 2])
def test_initial_publication_flush_failure_fences_without_owner(tmp_path, monkeypatch, version):
    env, state = _env(tmp_path, version), tmp_path / "s"
    def boom(path):
        raise OSError("synthetic directory flush failure")
    monkeypatch.setattr(sj, "_fsync_dir", boom)
    j = _open(state, env)
    with pytest.raises(sj.JournalError):
        j.__enter__()
    assert j.epoch is None and j._fd is None and not j.owner_current()
    assert (state / "journal.log").read_bytes() == b""  # no genesis, no owner record
    monkeypatch.undo()
    if version == 2:  # interrupted v2 creation fails closed: no authority from a missing witness
        _hold_unchanged(state, env)
    else:  # legacy single-log semantics unchanged: empty log replays as fresh
        with _open(state, env) as j:
            assert [r.kind for r in j.records] == ["genesis", "owner"]


def test_truncation_to_earlier_complete_record_holds(tmp_path):
    env, state = _env(tmp_path), tmp_path / "s"
    with _open(state, env) as j:
        size = j._size
        _admit(j, 1)
    os.truncate(state / "journal.log", size)
    _hold_unchanged(state, env)

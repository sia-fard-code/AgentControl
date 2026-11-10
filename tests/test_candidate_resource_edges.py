import os
import sys
import types

import pytest

from supervisor_helpers import load

cand = load("candidate")
B = cand.SnapshotBounds(max_entries=1000, max_file_bytes=4096, max_total_bytes=16384,
                        max_manifest_bytes=200000, max_path_bytes=4000, max_seconds=10)
OK = lambda: True  # noqa: E731


def deep(root, depth):
    p = root.joinpath(*(["d"] * depth))
    p.mkdir(parents=True)
    (p / "leaf").write_bytes(b"v1")
    return p / "leaf"


def ident(root, **kw):
    return cand.identity(root, bounds=B, inputs=(".",), owner_guard=OK, **kw)


def _depth():
    f, n = sys._getframe(), 0
    while f:
        f, n = f.f_back, n + 1
    return n


def test_deep_tree_walk_is_not_recursive(tmp_path):
    leaf = deep(tmp_path / "s", 60)  # 60 levels keeps the path under Windows MAX_PATH
    old = sys.getrecursionlimit()
    sys.setrecursionlimit(_depth() + 90)  # recursive walk needs >= 2 frames per level
    try:
        a = ident(tmp_path / "s")
        b = ident(tmp_path / "s", strict_layout=False)
        snap = cand.capture(tmp_path / "s", tmp_path / "x", bounds=B, inputs=(".",),
                            owner_guard=OK)
        cand.materialize(snap, tmp_path / "m", bounds=B, owner_guard=OK)
        leaf.write_bytes(b"v2")
        c = ident(tmp_path / "s")
    finally:
        sys.setrecursionlimit(old)
    assert a == b == snap.manifest_sha256 and c != a
    assert (tmp_path / "m").joinpath(*(["d"] * 60), "leaf").read_bytes() == b"v1"


@pytest.mark.skipif(os.name == "nt", reason="600-deep path needs POSIX path lengths")
def test_600_deep_tree_at_default_recursion_limit(tmp_path):
    leaf = deep(tmp_path / "s", 600)
    a = ident(tmp_path / "s")
    leaf.write_bytes(b"v2")
    assert ident(tmp_path / "s") != a


def _expiring_clock(monkeypatch, data):
    """Clock frozen at 0 until the first os.read of `data`, then far past the deadline."""
    now, fds, reads = [0.0], set(), []
    real_open, real_read = os.open, os.read

    def fake_open(path, *a, **kw):
        fd = real_open(path, *a, **kw)
        if os.path.abspath(os.fspath(path)) == os.path.abspath(data):
            fds.add(fd)
        return fd

    def fake_read(fd, n):
        out = real_read(fd, n)
        if fd in fds:
            reads.append(len(out))
            now[0] = 1e9
        return out

    monkeypatch.setattr(cand, "time", types.SimpleNamespace(monotonic=lambda: now[0]))
    monkeypatch.setattr(os, "open", fake_open)
    monkeypatch.setattr(os, "read", fake_read)
    return reads


BIG = cand.SnapshotBounds(max_entries=10, max_file_bytes=4 * cand.CHUNK,
                          max_total_bytes=4 * cand.CHUNK, max_manifest_bytes=16384,
                          max_path_bytes=120, max_seconds=10)


@pytest.mark.parametrize("mode", ["capture", "identity"])
def test_deadline_checked_between_hash_chunks(tmp_path, monkeypatch, mode):
    src = tmp_path / "s"
    src.mkdir()
    data = src / "DATA"
    data.write_bytes(b"\x5a" * (3 * cand.CHUNK))
    reads = _expiring_clock(monkeypatch, str(data))
    with pytest.raises(cand.CandidateError) as e:
        if mode == "capture":
            cand.capture(src, tmp_path / "x", bounds=BIG, inputs=(".",), owner_guard=OK)
        else:
            cand.identity(src, bounds=BIG, inputs=(".",), owner_guard=OK)
    assert e.value.code == "bound_time"
    assert reads == [cand.CHUNK]  # no second DATA read after the deadline passed
    assert not (tmp_path / "x").exists()

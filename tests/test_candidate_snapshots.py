import os
import sys

import pytest

from supervisor_helpers import load

cand = load("candidate")
B = cand.SnapshotBounds(max_entries=50, max_file_bytes=4096, max_total_bytes=16384,
                        max_manifest_bytes=16384, max_path_bytes=120, max_seconds=10)
OK = lambda: True  # noqa: E731


def tree(root, files, dirs=()):
    for rel, data in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
    for d in dirs:
        (root / d).mkdir(parents=True, exist_ok=True)
    return root


def cap(root, dest, inputs=(".",), **kw):
    kw.setdefault("bounds", B)
    kw.setdefault("owner_guard", OK)
    return cand.capture(root, dest, inputs=inputs, **kw)


def code(fn, *a, **kw):
    with pytest.raises(cand.CandidateError) as e:
        fn(*a, **kw)
    return e.value.code


def test_roundtrip_binary_utf8_emptydir_and_stable_identity(tmp_path):
    src = tree(tmp_path / "s", {"a.bin": bytes(range(256)), "d/ü.txt": "héllo".encode()},
               dirs=["empty"])
    s1 = cap(src, tmp_path / "x1")
    s2 = cap(src, tmp_path / "other-name")
    assert s1.manifest_sha256 == s2.manifest_sha256
    assert s1.file_count == 2 and s1.total_file_bytes == 256 + len("héllo".encode())
    m = cand.materialize(s1, tmp_path / "m", bounds=B, owner_guard=OK)
    assert (tmp_path / "m" / "a.bin").read_bytes() == bytes(range(256))
    assert (tmp_path / "m" / "d" / "ü.txt").read_text(encoding="utf-8") == "héllo"
    assert (tmp_path / "m" / "empty").is_dir()
    assert cap(tmp_path / "m", tmp_path / "x3").manifest_sha256 == s1.manifest_sha256
    assert m.manifest_sha256 == s1.manifest_sha256


def test_seed_only_in_receipt_and_missing_selector_is_absence(tmp_path):
    src = tree(tmp_path / "s", {"a": b"1"})
    s1 = cap(src, tmp_path / "x1", inputs=("a", "gone"))
    s2 = cap(src, tmp_path / "x2", inputs=("gone", "a"), seed_sha256="0" * 64)
    assert s1.manifest_sha256 == s2.manifest_sha256
    assert s1.receipt_sha256 != s2.receipt_sha256 and s2.seed_sha256 == "0" * 64
    cand.materialize(s1, tmp_path / "m", bounds=B, owner_guard=OK)
    assert not (tmp_path / "m" / "gone").exists()


def test_delta_add_delete_modify(tmp_path):
    a = cap(tree(tmp_path / "a", {"k": b"1", "m": b"1", "d": b"1"}), tmp_path / "xa")
    b = cap(tree(tmp_path / "b", {"k": b"1", "m": b"2", "n": b"3"}), tmp_path / "xb")
    ch = {c[0]: c for c in cand.delta(a, b, bounds=B)}
    assert set(ch) == {"m", "n", "d"}
    assert ch["n"][1] is None and ch["d"][2] is None


@pytest.mark.parametrize("field,val", [("max_entries", 2), ("max_file_bytes", 3),
                                       ("max_total_bytes", 5), ("max_manifest_bytes", 40),
                                       ("max_path_bytes", 4)])
def test_bounds_fail_closed(tmp_path, field, val):
    import dataclasses
    src = tree(tmp_path / "s", {"aaaaa": b"1234", "b": b"12", "c": b""})
    bounds = dataclasses.replace(B, **{field: val})
    assert code(cap, src, tmp_path / "x", bounds=bounds).startswith("bound")
    assert not (tmp_path / "x").exists()


def test_time_bound_fail_closed(tmp_path, monkeypatch):
    src = tree(tmp_path / "s", {"a": b"1", "b": b"2"})
    t = iter(range(0, 1000, 100))
    monkeypatch.setattr(cand.time, "monotonic", lambda: next(t))
    assert code(cap, src, tmp_path / "x") == "bound_time"
    assert not (tmp_path / "x").exists()


@pytest.mark.parametrize("bad", [0, -1, True, 1.5, float("inf"), None])
def test_bounds_validation(bad):
    with pytest.raises(cand.CandidateError):
        cand.SnapshotBounds(max_entries=bad, max_file_bytes=1, max_total_bytes=1,
                            max_manifest_bytes=1, max_path_bytes=1)


@pytest.mark.parametrize("sel", ["../x", "/abs", "C:/x", "a/../b", "a:ads", "CON", "nul.txt",
                                 "a\\b", "", "./a", "a/"])
def test_unsafe_selectors_rejected(tmp_path, sel):
    src = tree(tmp_path / "s", {"a": b"1"})
    assert code(cap, src, tmp_path / "x", inputs=(sel,)) == "unsafe_path"


def test_operational_overlap_and_dest_inside_root_rejected(tmp_path):
    src = tree(tmp_path / "s", {"state/j.log": b"1", "src/a": b"1"})
    assert code(cap, src, tmp_path / "x", operational_roots=(src / "state",)) == "operational_overlap"
    assert code(cap, src, tmp_path / "x", inputs=("src",),
                operational_roots=(src,)) == "operational_overlap"
    assert code(cap, src, src / "x", inputs=("src",)) == "destination_unsafe"


def test_owner_guard_false_before_any_write(tmp_path):
    src = tree(tmp_path / "s", {"a": b"1"})
    assert code(cap, src, tmp_path / "x", owner_guard=lambda: False) == "owner_guard"
    assert code(cap, src, tmp_path / "y", owner_guard=lambda: 1) == "owner_guard"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["s"]


def test_existing_destination_not_modified(tmp_path):
    src = tree(tmp_path / "s", {"a": b"1"})
    tree(tmp_path / "x", {"keep": b"k"})
    assert code(cap, src, tmp_path / "x") == "destination_exists"
    assert [p.name for p in (tmp_path / "x").iterdir()] == ["keep"]


def test_symlink_and_hardlink_refused(tmp_path):
    src = tree(tmp_path / "s", {"a": b"1"})
    os.link(src / "a", src / "h")
    assert code(cap, src, tmp_path / "x") == "unsupported_entry"
    src2 = tree(tmp_path / "s2", {"a": b"1"})
    try:
        os.symlink(src2 / "a", src2 / "l")
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation not permitted")
    assert code(cap, src2, tmp_path / "y") == "unsupported_entry"


def test_case_alias_selectors_rejected(tmp_path):
    src = tree(tmp_path / "s", {"a": b"1"})
    assert code(cap, src, tmp_path / "x", inputs=("A", "a")) == "unsafe_path"


def test_modification_during_capture_fails(tmp_path, monkeypatch):
    src = tree(tmp_path / "s", {"a": b"1", "b": b"2"})
    real = cand._hash_file

    def mutate(*a, **kw):
        r = real(*a, **kw)
        (src / "a").write_bytes(b"changed-longer")
        return r
    monkeypatch.setattr(cand, "_hash_file", mutate)
    assert code(cap, src, tmp_path / "x") == "changed_during_capture"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["s"]


def test_short_writes_complete_and_fsync_failure_leaves_nothing(tmp_path, monkeypatch):
    src = tree(tmp_path / "s", {"a": b"hello world" * 20})
    real_write = os.write
    monkeypatch.setattr(cand.os, "write", lambda fd, b: real_write(fd, bytes(b[:3])))
    s = cap(src, tmp_path / "x")
    monkeypatch.setattr(cand.os, "write", real_write)
    cand.materialize(s, tmp_path / "m", bounds=B, owner_guard=OK)
    assert (tmp_path / "m" / "a").read_bytes() == b"hello world" * 20

    def boom(fd):
        raise OSError("disk says no")
    monkeypatch.setattr(cand.os, "fsync", boom)
    assert code(cap, src, tmp_path / "y") == "io_error"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["m", "s", "x"]


@pytest.mark.parametrize("what", ["blob", "manifest", "receipt"])
def test_tampered_snapshot_not_materialized(tmp_path, what):
    s = cap(tree(tmp_path / "s", {"a": b"1"}), tmp_path / "x")
    d = tmp_path / "x"
    target = {"blob": next((d / "blobs").iterdir()), "manifest": d / "manifest.json",
              "receipt": d / "receipt.json"}[what]
    target.write_bytes(target.read_bytes() + b" ")
    assert code(cand.materialize, s, tmp_path / "m", bounds=B, owner_guard=OK) == "tampered"
    assert not (tmp_path / "m").exists()
    assert sorted(p.name for p in tmp_path.iterdir()) == ["s", "x"]


def test_materialization_partial_is_unselectable(tmp_path):
    s = cap(tree(tmp_path / "s", {"a": b"1", "b": b"2"}), tmp_path / "x")
    flips = iter([True])
    guard = lambda: next(flips, False)  # noqa: E731  owner lost mid-materialization
    assert code(cand.materialize, s, tmp_path / "m", bounds=B, owner_guard=guard) == "owner_guard"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["s", "x"]


def _branch(tmp_path, seed, name, files, dirs=()):
    root = tmp_path / ("r" + name)
    cand.materialize(seed, root, bounds=B, owner_guard=OK)
    for rel, data in files.items():
        if data is None:
            (root / rel).rmdir() if (root / rel).is_dir() else (root / rel).unlink()
        else:
            tree(root, {rel: data})
    return cap(root, tmp_path / ("c" + name), seed_sha256=seed.manifest_sha256)


def merge(seed, results, dest, scopes=(".",)):
    return cand.merge(seed, results, dest, bounds=B, allowed_write_paths=scopes, owner_guard=OK)


def test_independent_and_identical_merge(tmp_path):
    seed = cap(tree(tmp_path / "s", {"tools/a": b"1", "docs/b": b"1"}), tmp_path / "seed")
    r1 = _branch(tmp_path, seed, "1", {"tools/a": b"2", "tools/new": b"n"})
    r2 = _branch(tmp_path, seed, "2", {"docs/b": None, "tools/new": b"n"})
    m = merge(seed, (r1, r2), tmp_path / "out")
    cand.materialize(m, tmp_path / "mm", bounds=B, owner_guard=OK)
    got = sorted(p.relative_to(tmp_path / "mm").as_posix() for p in (tmp_path / "mm").rglob("*")
                 if p.is_file())
    assert got == ["tools/a", "tools/new"]
    assert (tmp_path / "mm" / "tools" / "a").read_bytes() == b"2"
    assert not hasattr(m, "accepted")


def test_merge_scope_segment_boundary_and_deletion(tmp_path):
    seed = cap(tree(tmp_path / "s", {"tools/a": b"1", "toolsmith/b": b"1"}), tmp_path / "seed")
    ok = _branch(tmp_path, seed, "1", {"tools/a": b"2"})
    assert merge(seed, (ok,), tmp_path / "o1", scopes=("tools",)).file_count == 2
    bad = _branch(tmp_path, seed, "2", {"toolsmith/b": b"2"})
    assert code(merge, seed, (bad,), tmp_path / "o2", ("tools",)) == "scope_violation"
    dele = _branch(tmp_path, seed, "3", {"toolsmith/b": None})
    assert code(merge, seed, (dele,), tmp_path / "o3", ("tools",)) == "scope_violation"
    assert not (tmp_path / "o2").exists() and not (tmp_path / "o3").exists()


def test_merge_conflicts_and_stale_seed(tmp_path):
    seed = cap(tree(tmp_path / "s", {"d/a": b"1", "x": b"1"}), tmp_path / "seed")
    r1 = _branch(tmp_path, seed, "1", {"x": b"2"})
    r2 = _branch(tmp_path, seed, "2", {"x": b"3"})
    assert code(merge, seed, (r1, r2), tmp_path / "o1") == "merge_conflict"
    r3 = _branch(tmp_path, seed, "3", {"d/a": None})
    r4 = _branch(tmp_path, seed, "4", {"d/new": b"z"})
    assert merge(seed, (r3, r4), tmp_path / "o2").file_count == 2
    r5 = _branch(tmp_path, seed, "5", {"d/a": None, "d": None})  # delete ancestor dir
    assert code(merge, seed, (r5, r4), tmp_path / "o4") == "merge_conflict"
    stale = cap(tmp_path / "r1", tmp_path / "st", seed_sha256="f" * 64)
    assert code(merge, seed, (stale,), tmp_path / "o3") == "stale_seed"


def test_git_pointer_only_with_exact_protected_admin(tmp_path):
    src = tree(tmp_path / "s", {"a": b"1", ".git": b"gitdir: ../g/worktrees/x\n"})
    assert code(cap, src, tmp_path / "x0") == "git_admin_required"
    import hashlib
    sha = hashlib.sha256((src / ".git").read_bytes()).hexdigest()
    calls = []
    admin = cand.GitAdmin(pointer_sha256=sha, guard=lambda: calls.append(1) or True)
    s = cap(src, tmp_path / "x1", git_admin=admin)
    assert len(calls) >= 2 and s.file_count == 1
    assert s.manifest_sha256 == cap(tree(tmp_path / "p", {"a": b"1"}), tmp_path / "x2").manifest_sha256
    wrong = cand.GitAdmin(pointer_sha256="0" * 64, guard=OK)
    assert code(cap, src, tmp_path / "x3", git_admin=wrong) == "git_admin_mismatch"
    nested = tree(tmp_path / "n", {"a": b"1", "sub/.git": b"x"})
    assert code(cap, nested, tmp_path / "x4", git_admin=admin) == "git_admin_required"
    gitdir = tree(tmp_path / "gd", {"a": b"1", ".git/HEAD": b"ref"})
    assert code(cap, gitdir, tmp_path / "x5", git_admin=admin) == "git_admin_mismatch"

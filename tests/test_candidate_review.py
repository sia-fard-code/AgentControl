import dataclasses
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from test_candidate_snapshots import B, OK, cand, cap, code, tree

_MISSING = object()


def forged(snapshot, manifest=_MISSING, receipt=_MISSING, raw=None, **claims):
    """Recompute transport hashes deliberately; these are not authority."""
    d = Path(snapshot.directory)
    if manifest is _MISSING:
        manifest = json.loads((d / "manifest.json").read_bytes())
    if receipt is _MISSING:
        receipt = json.loads((d / "receipt.json").read_bytes())
    # Malformed non-finite values are intentionally encoded, not rejected by the fixture.
    mb = json.dumps(manifest, sort_keys=True, separators=(",", ":"),
                    ensure_ascii=False).encode() if raw is None else raw
    if isinstance(receipt, dict):
        receipt["manifest_sha256"] = cand._sha(mb)
    rb = cand._canon(receipt)
    (d / "manifest.json").write_bytes(mb)
    (d / "receipt.json").write_bytes(rb)
    return dataclasses.replace(snapshot, manifest_sha256=cand._sha(mb),
                               receipt_sha256=cand._sha(rb), **claims)


@pytest.mark.parametrize("directory", [False, True])
def test_owned_layout_rejects_unselected_sibling(tmp_path, directory):
    src = tree(tmp_path / "s", {"data/a": b"\x00\xff"})
    before = cap(src, tmp_path / "before", inputs=("data",))
    if directory:
        (src / "undeclared").mkdir()
    else:
        (src / "undeclared.py").write_bytes(b"not selected")
    initial = cap(src, tmp_path / "initial", inputs=("data",))
    assert initial.manifest_sha256 == before.manifest_sha256
    assert code(cap, src, tmp_path / "owned", inputs=("data",),
                strict_layout=True) == "undeclared_entry"
    assert not (tmp_path / "owned").exists()


def test_owned_layout_structural_parents_and_selected_hidden_data(tmp_path):
    src = tree(tmp_path / "s", {"outer/data/a": b"a", "outer/data/.ignored": b"\x00\xfe",
                              "outer/data/.gitignore": b"*\n", "outer/data/new": b"new",
                              "state/j.log": b"operational"})
    s = cap(src, tmp_path / "x", inputs=("outer/data",),
            operational_roots=(src / "state",), strict_layout=True)
    m = json.loads((tmp_path / "x" / "manifest.json").read_bytes())
    assert {e["path"] for e in m["entries"]} == {
        "outer/data", "outer/data/a", "outer/data/.ignored",
        "outer/data/.gitignore", "outer/data/new"}
    cand.materialize(s, tmp_path / "m", bounds=B, owner_guard=OK)
    assert (tmp_path / "m/outer/data/.ignored").read_bytes() == b"\x00\xfe"
    assert cap(tmp_path / "m", tmp_path / "again", inputs=("outer/data",),
               strict_layout=True).manifest_sha256 == s.manifest_sha256


def directory_link(target, link):
    try:
        os.symlink(target, link, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("native directory symlink unavailable")


@pytest.mark.parametrize("ancestor", [False, True])
def test_operational_alias_is_checked_physically_before_copy(tmp_path, monkeypatch, ancestor):
    src = tree(tmp_path / "s", {"data/a": b"a", "state/j.log": b"private"})
    alias = tmp_path / "accounting"
    directory_link(src if ancestor else src / "state", alias)
    copied = []
    real = cand._Stage.add

    def record(self, *args):
        copied.append(args[0])
        return real(self, *args)

    monkeypatch.setattr(cand._Stage, "add", record)
    inputs = ("data",) if ancestor else (".",)
    assert code(cap, src, tmp_path / "x", inputs=inputs,
                operational_roots=(alias,)) == "operational_overlap"
    assert not copied and not (tmp_path / "x").exists()


def test_root_ancestry_alias_does_not_hide_operational_overlap(tmp_path):
    parent = tmp_path / "real"
    src = tree(parent / "s", {"state/j.log": b"private"})
    alias = tmp_path / "alias"
    directory_link(parent, alias)
    assert code(cap, alias / "s", tmp_path / "x",
                operational_roots=(src / "state",)) == "operational_overlap"


def test_owned_git_exception_requires_exact_authenticated_name(tmp_path):
    src = tree(tmp_path / "s", {"data/a": b"a", ".git": b"gitdir: ../admin\n"})
    admin = cand.GitAdmin(cand._sha((src / ".git").read_bytes()), OK)
    s = cap(src, tmp_path / "x", inputs=("data",), strict_layout=True, git_admin=admin)
    assert s.file_count == 1
    assert code(cap, src, tmp_path / "noauth", inputs=("data",),
                strict_layout=True) == "git_admin_required"
    (src / ".git").rename(src / "pointer")
    (src / "pointer").rename(src / ".GIT")
    assert code(cap, src, tmp_path / "alias", inputs=("data",),
                strict_layout=True, git_admin=admin) == "git_admin_mismatch"


def test_scandir_collection_stops_before_unbounded_allocation(tmp_path, monkeypatch):
    src = tmp_path / "s"
    src.mkdir()
    real = cand.os.scandir
    consumed = []

    class Many:
        def __enter__(self): return self
        def __exit__(self, *args): return False
        def __iter__(self): return self

        def __next__(self):
            consumed.append(1)
            assert len(consumed) <= 4, "enumeration exceeded the existing entry bound"
            return SimpleNamespace(name=f"f{len(consumed):05}")

    monkeypatch.setattr(cand.os, "scandir",
                        lambda p: Many() if os.fspath(p) == str(src) else real(p))
    assert code(cap, src, tmp_path / "x", bounds=dataclasses.replace(B, max_entries=1)) \
        == "bound_entries"
    assert 1 <= len(consumed) <= 4 and not (tmp_path / "x").exists()


def test_selector_work_is_bounded_before_filesystem_or_quadratic_scan(tmp_path, monkeypatch):
    src = tmp_path / "s"
    src.mkdir()
    clock = iter(range(10000))
    monkeypatch.setattr(cand.time, "monotonic", lambda: next(clock))

    def no_stage(*args, **kwargs):
        pytest.fail("selector validation must enforce its deadline before staging")

    monkeypatch.setattr(cand, "_Stage", no_stage)
    bounds = dataclasses.replace(B, max_entries=5000, max_manifest_bytes=100000, max_seconds=2)
    assert code(cap, src, tmp_path / "x", inputs=[f"absent{i:05}" for i in range(2000)],
                bounds=bounds) == "bound_time"


def test_selector_count_bound_and_component_prefix_order(tmp_path):
    src = tree(tmp_path / "s", {}, dirs=(".",))
    assert code(cap, src, tmp_path / "x", inputs=("absent1", "absent2"),
                bounds=dataclasses.replace(B, max_entries=1)) == "bound_entries"
    assert code(cap, src, tmp_path / "nested", inputs=("a", "a-", "a/b")) == "unsafe_path"


def test_bounded_enumeration_keeps_canonical_order(tmp_path):
    src = tree(tmp_path / "s", {"z": b"z", "a": b"a", "middle": b"m"})
    a = cap(src, tmp_path / "x", inputs=("z", "middle", "a"))
    b = cap(src, tmp_path / "y", inputs=("a", "z", "middle"))
    assert a.manifest_sha256 == b.manifest_sha256
    m = json.loads((tmp_path / "x/manifest.json").read_bytes())
    assert [e["path"] for e in m["entries"]] == ["a", "middle", "z"]


@pytest.mark.parametrize("malformation", [
    "manifest_list", "receipt_list", "entries_object", "entry_list", "size_bool",
    "size_float", "size_nan", "size_inf", "size_negative", "duplicate_entry",
    "duplicate_input", "scope_mismatch", "wrong_kind", "mode_integer", "bad_sha",
    "entry_root", "extra_manifest_key", "extra_entry_key", "directory_fields",
    "receipt_bool", "receipt_float", "receipt_extra", "platform", "echoed_scope",
])
def test_self_hashed_malformed_metadata_is_not_authority(tmp_path, malformation):
    s = cap(tree(tmp_path / "s", {"a": b"x"}), tmp_path / "x")
    m = json.loads((tmp_path / "x/manifest.json").read_bytes())
    r = json.loads((tmp_path / "x/receipt.json").read_bytes())
    claims = {}
    if malformation == "manifest_list": m = []
    elif malformation == "receipt_list": r = []
    elif malformation == "entries_object": m["entries"] = {}
    elif malformation == "entry_list": m["entries"] = [[]]
    elif malformation.startswith("size_"):
        m["entries"][0]["size"] = {"size_bool": True, "size_float": 1.0,
            "size_nan": float("nan"), "size_inf": float("inf"), "size_negative": -1}[malformation]
    elif malformation == "duplicate_entry": m["entries"] *= 2
    elif malformation == "duplicate_input": m["inputs"] = ["a", "a"]
    elif malformation == "scope_mismatch": m["inputs"] = ["a"]
    elif malformation == "wrong_kind": m["entries"][0]["type"] = "link"
    elif malformation == "mode_integer": m["entries"][0]["exec"] = 1
    elif malformation == "bad_sha": m["entries"][0]["sha256"] = "not a digest"
    elif malformation == "entry_root": m["entries"][0]["path"] = "."
    elif malformation == "extra_manifest_key": m["root"] = "elsewhere"
    elif malformation == "extra_entry_key": m["entries"][0]["root"] = "elsewhere"
    elif malformation == "directory_fields": m["entries"][0]["type"] = "dir"
    elif malformation == "receipt_bool": r["file_count"] = claims["file_count"] = True
    elif malformation == "receipt_float": r["total_file_bytes"] = claims["total_file_bytes"] = 1.0
    elif malformation == "receipt_extra": r["root"] = "elsewhere"
    elif malformation == "platform": m["platform"] = r["platform"] = "other"
    elif malformation == "echoed_scope": r["scope_sha256"] = claims["scope_sha256"] = "0" * 64
    s = forged(s, m, r, **claims)
    assert code(cand.materialize, s, tmp_path / "m", bounds=B, owner_guard=OK) == "tampered"
    assert not (tmp_path / "m").exists()


def test_duplicate_json_key_with_matching_hash_is_rejected(tmp_path):
    s = cap(tree(tmp_path / "s", {"a": b"x"}), tmp_path / "x")
    raw = (tmp_path / "x/manifest.json").read_bytes()
    assert b'"size":1' in raw
    s = forged(s, raw=raw.replace(b'"size":1', b'"size":1,"size":1'))
    assert code(cand.materialize, s, tmp_path / "m", bounds=B, owner_guard=OK) == "tampered"


def test_snapshot_and_blob_roots_are_not_followed(tmp_path):
    s = cap(tree(tmp_path / "s", {"a": b"x"}), tmp_path / "x")
    alias = tmp_path / "alias"
    directory_link(tmp_path / "x", alias)
    assert code(cand.materialize, dataclasses.replace(s, directory=str(alias)),
                tmp_path / "m1", bounds=B, owner_guard=OK) == "tampered"
    blobs = tmp_path / "x/blobs"
    blobs.rename(tmp_path / "external")
    directory_link(tmp_path / "external", blobs)
    assert code(cand.materialize, s, tmp_path / "m2", bounds=B, owner_guard=OK) == "tampered"


def results_for(tmp_path, left, right):
    seed = cap(tree(tmp_path / "s", {}, dirs=(".",)), tmp_path / "seed")
    a = cap(tree(tmp_path / "a", left), tmp_path / "ca", seed_sha256=seed.manifest_sha256)
    b = cap(tree(tmp_path / "b", right), tmp_path / "cb", seed_sha256=seed.manifest_sha256)
    return seed, (a, b)


@pytest.mark.parametrize("entries,total,expected", [
    (1, 8, "bound_entries"), (10, 4, "bound_total"),
])
def test_merge_union_limits_precede_staging(tmp_path, entries, total, expected):
    seed, results = results_for(tmp_path, {"a": b"1234"}, {"b": b"5678"})
    bounds = dataclasses.replace(B, max_entries=entries, max_file_bytes=4, max_total_bytes=total)
    assert code(cand.merge, seed, results, tmp_path / "out", bounds=bounds,
                allowed_write_paths=(".",), owner_guard=OK) == expected
    assert not (tmp_path / "out").exists() and not list(tmp_path.glob(".out.stage-*"))


@pytest.mark.parametrize("left,right", [
    ({"A": b"a"}, {"a": b"b"}), ({"Dir/a": b"a"}, {"dir/b": b"b"}),
])
def test_cross_result_case_tree_collisions_are_not_published(tmp_path, left, right):
    seed, results = results_for(tmp_path, left, right)
    assert code(cand.merge, seed, results, tmp_path / "out", bounds=B,
                allowed_write_paths=(".",), owner_guard=OK) == "merge_conflict"
    assert not (tmp_path / "out").exists()


@pytest.mark.parametrize("lose_owner", [False, True])
def test_published_merge_is_reverified_before_acknowledgement(tmp_path, monkeypatch, lose_owner):
    seed, results = results_for(tmp_path, {"a": b"\x00\xff"}, {"b": b"\xfe\x00"})
    real = cand._Stage.publish
    published = []

    def publish(self):
        real(self)
        if self.dest == str(tmp_path / "out"):
            published.append(True)
            if not lose_owner:
                next((tmp_path / "out/blobs").iterdir()).write_bytes(b"corrupt")

    monkeypatch.setattr(cand._Stage, "publish", publish)
    guard = lambda: not (lose_owner and published)
    assert code(cand.merge, seed, results, tmp_path / "out", bounds=B,
                allowed_write_paths=(".",), owner_guard=guard) \
        == ("owner_guard" if lose_owner else "tampered")
    assert published and (tmp_path / "out").is_dir()


def test_binary_creation_deletion_and_type_change_roundtrip(tmp_path):
    seed = cap(tree(tmp_path / "s", {"gone": b"\x00\xff", "node": b"old"}), tmp_path / "seed")
    a = cap(tree(tmp_path / "a", {"node/new": bytes(range(256))}), tmp_path / "ca",
            seed_sha256=seed.manifest_sha256)
    b = cap(tree(tmp_path / "b", {"gone": b"\x00\xff", "node": b"old", "add": b"\xff\x00"}),
            tmp_path / "cb", seed_sha256=seed.manifest_sha256)
    merged = cand.merge(seed, (a, a, b), tmp_path / "out", bounds=B,
                        allowed_write_paths=(".",), owner_guard=OK)
    cand.materialize(merged, tmp_path / "m", bounds=B, owner_guard=OK)
    assert not (tmp_path / "m/gone").exists()
    assert (tmp_path / "m/node/new").read_bytes() == bytes(range(256))
    assert (tmp_path / "m/add").read_bytes() == b"\xff\x00"


def test_cross_result_file_directory_change_conflicts(tmp_path):
    seed = cap(tree(tmp_path / "s", {"node": b"old"}), tmp_path / "seed")
    a = cap(tree(tmp_path / "a", {"node/new": b"new"}), tmp_path / "ca",
            seed_sha256=seed.manifest_sha256)
    b = cap(tree(tmp_path / "b", {"node": b"changed"}), tmp_path / "cb",
            seed_sha256=seed.manifest_sha256)
    assert code(cand.merge, seed, (a, b), tmp_path / "out", bounds=B,
                allowed_write_paths=(".",), owner_guard=OK) == "merge_conflict"
    assert not (tmp_path / "out").exists()


@pytest.mark.parametrize("replace_file", [False, True], ids=["new-dir", "same-subtree"])
def test_compatible_directory_changes_merge_without_false_conflict(tmp_path, replace_file):
    seed = cap(tree(tmp_path / "s", {"node": b"old"} if replace_file else {}, dirs=(".",)),
               tmp_path / "seed")
    left = {"node/a": b"a"}
    right = {"node/a": b"a", "extra": b"x"} if replace_file else {"node/b": b"b"}
    a = cap(tree(tmp_path / "a", left), tmp_path / "ca", seed_sha256=seed.manifest_sha256)
    b = cap(tree(tmp_path / "b", right), tmp_path / "cb", seed_sha256=seed.manifest_sha256)
    merged = cand.merge(seed, (a, b), tmp_path / "out", bounds=B,
                        allowed_write_paths=(".",), owner_guard=OK)
    cand.materialize(merged, tmp_path / "m", bounds=B, owner_guard=OK)
    assert (tmp_path / "m/node/a").read_bytes() == b"a"
    assert (tmp_path / ("m/extra" if replace_file else "m/node/b")).read_bytes() \
        == (b"x" if replace_file else b"b")

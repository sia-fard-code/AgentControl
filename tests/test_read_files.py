"""Opt-in bounded multi-file reads (read_files) under resilient-v1.

Synthetic tmp_path fixtures only; no network, no live state.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from test_engine_reasoning import engine, harness  # noqa: F401

RES = "resilient-v1"


@pytest.fixture(autouse=True)
def _sibling_imports(engine, monkeypatch):
    monkeypatch.syspath_prepend(str(Path(engine.__file__).parent))


@pytest.fixture()
def pt(engine):
    return engine.capability_module("paged_tools")


def _runner(engine, root, enabled=True):
    return engine.Runner(str(root), "stage", profile=RES, read_files_enabled=enabled)


def _spy(run, monkeypatch):
    seen = {"n": 0}
    real, real_uri = run._resolve, run.caps.resolve_read

    def resolve(path):
        seen["n"] += 1
        return real(path)

    def resolve_uri(path):
        seen["n"] += 1
        return real_uri(path)
    monkeypatch.setattr(run, "_resolve", resolve)
    monkeypatch.setattr(run.caps, "resolve_read", resolve_uri)
    return seen


def _ok(content, index):
    return {"index": index, "status": "served", "content": content, "complete": True}


def test_two_small_reads_in_order(engine, tmp_path) -> None:
    (tmp_path / "a.txt").write_text("alpha\n", encoding="utf-8")
    (tmp_path / "b.txt").write_text("beta\nmore\n", encoding="utf-8")
    run = _runner(engine, tmp_path)
    out = json.loads(run.read_files([{"path": "b.txt"}, {"path": "a.txt"}]))
    assert out == {"results": [_ok("beta\nmore\n", 0), _ok("alpha\n", 1)]}


def test_ranged_read(engine, tmp_path) -> None:
    (tmp_path / "a.txt").write_text("1\n2\n3\n4\n", encoding="utf-8")
    run = _runner(engine, tmp_path)
    out = json.loads(run.read_files([{"path": "a.txt", "start_line": 2, "end_line": 3}]))
    assert out == {"results": [_ok("2\n3\n", 0)]}


@pytest.mark.parametrize("pending", [False, True], ids=["disk", "staged"])
@pytest.mark.parametrize("case", ["skip_long_prefix", "serve_long_line", "serve_without_newline"])
def test_long_logical_lines_match_existing_ranges(engine, tmp_path, monkeypatch,
                                                  pending, case) -> None:
    if case == "skip_long_prefix":
        text = "x" * 8193 + "\n" + "y" * 16387 + "\nok\n"
        start = end = 3
        expected = "ok\n"
        cap = 1000
    else:
        expected = "x" * 9001 + ("\n" if case == "serve_long_line" else "")
        text = expected
        start = end = 1
        cap = 30000
    monkeypatch.setattr(engine, "MAX_READ_CHARS", cap)
    run = _runner(engine, tmp_path)
    if pending:
        run.pending["long.txt"] = text
    else:
        (tmp_path / "long.txt").write_text(text, encoding="utf-8")
    assert run.read_file("long.txt", start, end).endswith(expected)
    rendered = run.read_files([{"path": "long.txt", "start_line": start, "end_line": end}])
    assert len(rendered) <= cap
    row = json.loads(rendered)["results"][0]
    assert row["status"] == "served", row
    assert row["content"] == expected
    assert row["complete"] is True
    assert "next_range" not in row


BAD = [
    [],
    [{"path": "a.txt"}] * 9,
    "a.txt",
    {"path": "a.txt"},
    None,
    [{"path": "a.txt"}, {"path": "a.txt", "bogus": 1}],
    [{"path": "a.txt"}, {"path": "a.txt", "start_line": True}],
    [{"path": "a.txt"}, {"path": "a.txt", "end_line": None}],
    [{"path": "a.txt"}, {"path": "a.txt", "start_line": 0}],
    [{"path": "a.txt"}, {"path": "a.txt", "start_line": 1.0}],
    [{"path": "a.txt"}, {"path": "a.txt", "start_line": 5, "end_line": 2}],
    [{"path": "a.txt"}, {"path": ""}],
    [{"path": "a.txt"}, {"path": 3}],
    [{"path": "a.txt"}, {"start_line": 1}],
    [{"path": "a.txt"}, "a.txt"],
]


@pytest.mark.parametrize("requests", BAD)
def test_malformed_batch_rejected_before_any_read(engine, pt, tmp_path, monkeypatch,
                                                  requests) -> None:
    (tmp_path / "a.txt").write_text("alpha\n", encoding="utf-8")
    with pytest.raises(ValueError):
        pt.validate_read_files(requests)
    run = _runner(engine, tmp_path)
    seen = _spy(run, monkeypatch)
    out = json.loads(run.read_files(requests))
    assert set(out) == {"error"} and isinstance(out["error"], str)
    assert len(out["error"]) <= 300
    assert seen["n"] == 0


def test_validate_returns_normalized_entries(pt) -> None:
    got = pt.validate_read_files([{"path": "a"}, {"path": "b", "start_line": 2, "end_line": 2}])
    assert got == ({"path": "a"}, {"path": "b", "start_line": 2, "end_line": 2})
    assert isinstance(got, tuple)


def test_schema_is_closed_object_array(pt) -> None:
    s = pt.READ_FILES_SCHEMA
    assert s["additionalProperties"] is False and s["required"] == ["requests"]
    arr = s["properties"]["requests"]
    assert arr["type"] == "array" and arr["minItems"] == 1 and arr["maxItems"] == 8
    item = arr["items"]
    assert item["additionalProperties"] is False and item["required"] == ["path"]
    assert set(item["properties"]) == {"path", "start_line", "end_line"}


def test_pending_disk_and_uri_match_single_reads(engine, tmp_path) -> None:
    (tmp_path / "d.txt").write_text("disk\n", encoding="utf-8")
    run = engine.Runner(str(tmp_path), "stage", profile=RES, read_files_enabled=True,
                        artifact_dir=str(tmp_path / "art"))
    run.write_file("p.txt", "staged\n")
    ref = run.caps.save_artifact(b'{"a": 1}', ".json")
    out = json.loads(run.read_files([{"path": "d.txt"}, {"path": "p.txt"}, {"path": ref}]))
    assert out == {"results": [_ok("disk\n", 0), _ok("staged\n", 1), _ok('{"a": 1}', 2)]}
    assert run.read_whole == set()


def test_denials_and_mixed_errors_stay_in_their_rows(engine, tmp_path) -> None:
    (tmp_path / "a.txt").write_text("alpha\n", encoding="utf-8")
    (tmp_path.parent / "outside.txt").write_text("secret\n", encoding="utf-8")
    run = _runner(engine, tmp_path)
    reqs = [{"path": "../outside.txt"}, {"path": "a.txt"}, {"path": "missing.txt"},
            {"path": "artifact://../x"}, {"path": "https://example.com/x"}]
    rows = json.loads(run.read_files(reqs))["results"]
    assert [r["status"] for r in rows] == ["error", "served", "error", "error", "error"]
    assert [r["index"] for r in rows] == [0, 1, 2, 3, 4]
    assert "escapes" in rows[0]["error"] and "unknown artifact" in rows[3]["error"]
    assert rows[1]["content"] == "alpha\n"
    assert all("content" not in r and "secret" not in r["error"] for r in rows if r["status"] == "error")


def test_symlink_escape_denied(engine, tmp_path) -> None:
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    (out_dir / "s.txt").write_text("secret\n", encoding="utf-8")
    root = tmp_path / "root"
    root.mkdir()
    try:
        os.symlink(out_dir / "s.txt", root / "link.txt")
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable")
    row = json.loads(_runner(engine, root).read_files([{"path": "link.txt"}]))["results"][0]
    assert row["status"] == "error" and "escapes" in row["error"]


def test_duplicates_and_current_edits_are_fresh(engine, tmp_path) -> None:
    f = tmp_path / "a.txt"
    f.write_text("one\n", encoding="utf-8")
    run = _runner(engine, tmp_path)
    out = json.loads(run.read_files([{"path": "a.txt"}, {"path": "a.txt"}]))
    assert out == {"results": [_ok("one\n", 0), _ok("one\n", 1)]}
    f.write_text("two\n", encoding="utf-8")
    assert json.loads(run.read_files([{"path": "a.txt"}]))["results"][0]["content"] == "two\n"
    run.write_file("a.txt", "three\n")
    assert json.loads(run.read_files([{"path": "a.txt"}]))["results"][0]["content"] == "three\n"
    assert run.read_whole == set()
    assert run.read_file("a.txt").endswith("three\n")  # single read is not suppressed


NASTY = "q\"uo\\te\x01\x1f\té ✓ 𝄞 \u2028\n"


@pytest.mark.parametrize("cap", range(1000, 1600, 37))
def test_whole_result_fits_cap_and_continues_exactly(engine, pt, tmp_path, cap) -> None:
    text = "".join(f"{i:03d}{NASTY * (i % 5 + 1)}" for i in range(60))
    (tmp_path / "n.txt").write_text(text, encoding="utf-8")
    run = _runner(engine, tmp_path)
    got, start, calls = "", 1, 0
    while True:
        out = pt.read_files(run, [{"path": "n.txt", "start_line": start, "end_line": 10 ** 6}], cap)
        assert len(out) <= cap
        row = json.loads(out)["results"][0]
        assert row["status"] == "served" and row["content"]
        got += row["content"]
        calls += 1
        if row["complete"]:
            assert "next_range" not in row
            break
        assert row["next_range"]["end_line"] == 10 ** 6
        start = row["next_range"]["start_line"]
    expect = "".join(text.replace("\r", "").splitlines(True))
    assert got == expect and calls > 1


def test_unserved_tail_has_no_content(engine, pt, tmp_path) -> None:
    for i in range(8):
        (tmp_path / f"f{i}.txt").write_text(("x" * 50 + "\n") * 40, encoding="utf-8")
    run = _runner(engine, tmp_path)
    out = pt.read_files(run, [{"path": f"f{i}.txt"} for i in range(8)], 1000)
    assert len(out) <= 1000
    rows = json.loads(out)["results"]
    assert [r["index"] for r in rows] == list(range(8))
    assert rows[0]["status"] == "served" and rows[0]["complete"] is False
    assert rows[0]["next_range"] == {"start_line": rows[0]["content"].count("\n") + 1}
    assert rows[-1]["status"] == "unserved"
    assert all(r["status"] != "unserved" for r in rows[:1])
    for r in rows:
        if r["status"] == "unserved":
            assert set(r) == {"index", "status", "reason"} and len(r["reason"]) <= 300


@pytest.mark.parametrize("line_length", [3000, 16387])
def test_overlong_line_is_reported_not_dropped(engine, pt, tmp_path, line_length) -> None:
    (tmp_path / "w.txt").write_text("short\n" + "y" * line_length + "\nafter\n", encoding="utf-8")
    run = _runner(engine, tmp_path)
    row = json.loads(pt.read_files(run, [{"path": "w.txt"}], 2000))["results"][0]
    assert row == {"index": 0, "status": "served", "content": "short\n", "complete": False,
                   "next_range": {"start_line": 2}}
    row = json.loads(pt.read_files(run, [{"path": "w.txt", "start_line": 2}], 2000))["results"][0]
    assert row["status"] == "error" and "line 2" in row["error"] and "y" * 50 not in row["error"]


def test_start_past_end_is_error(engine, tmp_path) -> None:
    (tmp_path / "a.txt").write_text("1\n2\n", encoding="utf-8")
    row = json.loads(_runner(engine, tmp_path).read_files(
        [{"path": "a.txt", "start_line": 9}]))["results"][0]
    assert row["status"] == "error" and "past the end" in row["error"]


def test_tiny_cap_is_whole_call_error(engine, pt, tmp_path) -> None:
    run = _runner(engine, tmp_path)
    out = pt.read_files(run, [{"path": f"f{i}.txt"} for i in range(8)], 100)
    assert set(json.loads(out)) == {"error"} and len(out) <= 100


def test_guard_counts_read_files_as_read_but_never_clips_it(engine) -> None:
    cg = engine.capability_module("context_guard")
    assert cg._is_read("read_files")
    assert "read_files" not in cg.PAGED_TOOLS


@pytest.mark.parametrize("cap", [1, 12, 13, 20, 64, 100])
@pytest.mark.parametrize("requests", [None, [{"path": "x"}] * 8])
def test_all_preflight_errors_fit_before_io(engine, pt, tmp_path, monkeypatch, cap, requests):
    run = _runner(engine, tmp_path)
    seen = _spy(run, monkeypatch)
    try:
        out = pt.read_files(run, requests, cap)
    except ValueError:
        assert cap < len('{"error":""}')
    else:
        assert len(out) <= cap and set(json.loads(out)) == {"error"}
    assert seen["n"] == 0


def test_executed_unservable_is_error_not_unserved(engine, pt, tmp_path, monkeypatch):
    import builtins
    (tmp_path / "huge.txt").write_text("x" * 4000 + "\n", encoding="utf-8")
    (tmp_path / "later.txt").write_text("later\n", encoding="utf-8")
    run = _runner(engine, tmp_path)
    resolved, opened, reads = [], [], []
    resolve, real_open = run._resolve, builtins.open

    def resolving(path):
        resolved.append(path)
        return resolve(path)

    class Stream:
        def __init__(self, fh):
            self.fh = fh
        def __enter__(self):
            return self
        def __exit__(self, *args):
            self.fh.close()
        def readline(self, size=-1):
            reads.append(size)
            assert 0 < size <= pt.LINE_BYTES + 1
            return self.fh.readline(size)

    def opening(path, *args, **kwargs):
        opened.append(Path(path).name)
        return Stream(real_open(path, *args, **kwargs))

    monkeypatch.setattr(run, "_resolve", resolving)
    monkeypatch.setattr(pt, "open", opening, raising=False)
    out = pt.read_files(run, [{"path": "huge.txt"}, {"path": "later.txt"}], 1000)
    rows = json.loads(out)["results"]
    assert len(out) <= 1000 and rows[0]["status"] == "error"
    assert "line 1" in rows[0]["error"]
    for row, name in zip(rows, ["huge.txt", "later.txt"]):
        if row["status"] == "unserved":
            assert name not in resolved and name not in opened
    assert "huge.txt" in opened and reads and run.read_whole == set()


def test_unserved_really_untouched(engine, pt, tmp_path, monkeypatch):
    import builtins
    for i in range(8):
        (tmp_path / f"f{i}").write_text("small line\n" * 500, encoding="utf-8")
    run = _runner(engine, tmp_path)
    resolved, opened = [], []
    resolve, real_open = run._resolve, builtins.open
    def resolving(path):
        resolved.append(path)
        return resolve(path)
    def opening(path, *args, **kwargs):
        opened.append(Path(path).name)
        return real_open(path, *args, **kwargs)
    monkeypatch.setattr(run, "_resolve", resolving)
    monkeypatch.setattr(pt, "open", opening, raising=False)
    rows = json.loads(pt.read_files(run, [{"path": f"f{i}"} for i in range(8)], 1000))["results"]
    assert any(r["status"] == "unserved" for r in rows)
    for row in rows:
        if row["status"] == "unserved":
            assert f'f{row["index"]}' not in resolved + opened


@pytest.mark.parametrize("cap", [64, 130, 1000])
def test_escaped_error_envelope_fits(engine, pt, tmp_path, monkeypatch, cap):
    run = _runner(engine, tmp_path)
    def denied(path):
        raise ValueError('escapes "\\\\\x01' * 500)
    monkeypatch.setattr(run, "_resolve", denied)
    out = pt.read_files(run, [{"path": "x"}], cap)
    assert len(out) <= cap
    data = json.loads(out)
    assert "error" in data or data["results"][0]["status"] == "error"


@pytest.mark.parametrize("staged", [False, True])
def test_scan_cap_applies_while_serving_and_skipping(engine, pt, tmp_path, monkeypatch, staged):
    text = "12345\n" * 50
    (tmp_path / "a").write_text(text, encoding="utf-8")
    run = _runner(engine, tmp_path)
    if staged:
        run.write_file("a", text)
    monkeypatch.setattr(pt, "MAX_SCAN_BYTES", 20)
    for start in (1, 20):
        row = json.loads(pt.read_files(run, [{"path": "a", "start_line": start}], 100000))["results"][0]
        assert row["status"] == "error" or not row["complete"]
        if row["status"] == "served":
            assert row["content"] == "12345\n" * 3
            assert row["next_range"] == {"start_line": 4}
    assert run.read_whole == set()


@pytest.mark.parametrize("staged", [False, True])
@pytest.mark.parametrize("start", [1, 2, 3])
def test_scan_cap_inside_long_logical_line(engine, pt, tmp_path, monkeypatch, staged, start):
    text = "head\n" + "x" * 16387 + "\nok\n"
    (tmp_path / "a").write_text(text, encoding="utf-8")
    run = _runner(engine, tmp_path)
    if staged:
        run.pending["a"] = text
    monkeypatch.setattr(pt, "MAX_SCAN_BYTES", 9000)
    out = pt.read_files(run, [{"path": "a", "start_line": start}], 30000)
    assert len(out) <= 30000
    row = json.loads(out)["results"][0]
    if start == 1:
        assert row == {"index": 0, "status": "served", "content": "head\n", "complete": False,
                       "next_range": {"start_line": 2}}
    else:
        assert row["status"] == "error" and "scan limit" in row["error"]
    assert run.read_whole == set()


@pytest.mark.parametrize("staged", [False, True])
@pytest.mark.parametrize("newline", ["", "\n"], ids=["eof", "newline"])
@pytest.mark.parametrize("char", ["x", "\x01"])
def test_long_line_exact_serialized_boundary(engine, pt, tmp_path, staged, newline, char):
    text = char * 9001 + newline
    (tmp_path / "a").write_text(text, encoding="utf-8")
    run = _runner(engine, tmp_path)
    if staged:
        run.pending["a"] = text
    expected = {"results": [_ok(text, 0)]}
    cap = len(json.dumps(expected, ensure_ascii=True, separators=(",", ":")))
    out = pt.read_files(run, [{"path": "a"}], cap)
    assert len(out) == cap and json.loads(out) == expected
    smaller = pt.read_files(run, [{"path": "a"}], cap - 1)
    assert len(smaller) <= cap - 1
    assert json.loads(smaller)["results"][0]["status"] == "error"
    assert run.read_whole == set()


@pytest.mark.parametrize("staged", [False, True])
def test_long_line_continuation_uses_logical_boundary(engine, pt, tmp_path, staged):
    first = "x" * 16387 + "\n"
    text = first + "y" * 500 + "\n"
    (tmp_path / "a").write_text(text, encoding="utf-8")
    run = _runner(engine, tmp_path)
    if staged:
        run.pending["a"] = text
    partial = {"index": 0, "status": "served", "content": first, "complete": False,
               "next_range": {"start_line": 2}}
    cap = len(json.dumps({"results": [partial]}, separators=(",", ":")))
    out = pt.read_files(run, [{"path": "a"}], cap)
    assert len(out) == cap and json.loads(out)["results"] == [partial]
    tail = pt.read_files(run, [{"path": "a", **partial["next_range"]}], cap)
    assert len(tail) <= cap and json.loads(tail)["results"] == [_ok("y" * 500 + "\n", 0)]
    assert run.read_whole == set()


def test_large_budget_never_unbounds_line_read(engine, pt, tmp_path, monkeypatch):
    import builtins
    text = "x" * (pt.LINE_BYTES * 3) + "\n"
    (tmp_path / "a").write_text(text, encoding="utf-8")
    run = _runner(engine, tmp_path)
    real_open = builtins.open
    sizes = []
    class Stream:
        def __init__(self, fh):
            self.fh = fh
        def __enter__(self):
            return self
        def __exit__(self, *args):
            self.fh.close()
        def readline(self, size=-1):
            sizes.append(size)
            assert 0 < size <= pt.LINE_BYTES + 1
            return self.fh.readline(size)
    monkeypatch.setattr(pt, "open", lambda *a, **k: Stream(real_open(*a, **k)), raising=False)
    row = json.loads(pt.read_files(run, [{"path": "a"}], 10 ** 8))["results"][0]
    assert row == _ok(text, 0) and len(sizes) > 1
    assert run.read_whole == set()


def test_batch_rechecks_after_single_suppression_and_uri_changes(engine, pt, tmp_path):
    f = tmp_path / "a"
    f.write_text("old\n", encoding="utf-8")
    run = _runner(engine, tmp_path)
    assert run.read_file("a") == "old\n" and run.read_whole == {"a"}
    f.write_text("new\n", encoding="utf-8")
    assert json.loads(run.read_files([{"path": "a"}]))["results"] == [_ok("new\n", 0)]
    repo = tmp_path / "repo"
    repo.mkdir()
    run = engine.Runner(str(repo), profile=RES, read_files_enabled=True,
                        artifact_dir=str(tmp_path / "art"))
    ref = run.caps.save_artifact(b"synthetic\n", ".txt")
    assert ref.startswith("artifact://")
    assert json.loads(run.read_files([{"path": ref}]))["results"][0]["complete"]
    Path(run.caps.resolve_read(ref)).unlink()
    assert json.loads(run.read_files([{"path": ref}]))["results"][0]["status"] == "error"


def test_read_files_diagnostic_name(engine):
    assert engine.diag_tool_name("read_files") == "read_files"


def test_exact_serialized_boundary(engine, pt, tmp_path, monkeypatch):
    (tmp_path / "a").write_text("x\n", encoding="utf-8")
    run = _runner(engine, tmp_path)
    expected = {"results": [_ok("x\n", 0)]}
    cap = len(json.dumps(expected, separators=(",", ":")))
    out = pt.read_files(run, [{"path": "a"}], cap)
    assert json.loads(out) == expected and len(out) == cap
    smaller = pt.read_files(run, [{"path": "a"}], cap - 1)
    assert len(smaller) <= cap - 1 and json.loads(smaller) != expected


def test_skill_grants_denials_and_ranges(engine, pt, tmp_path):
    skills = tmp_path / "skills" / "tiny"
    skills.mkdir(parents=True)
    (skills / "SKILL.md").write_text("---\nname: tiny\ndescription: Synthetic\n---\nSynthetic rules\n", encoding="utf-8")
    (skills / "ref.txt").write_text("one\ntwo\nthree\n", encoding="utf-8")
    run = engine.Runner(str(tmp_path), profile=RES, read_files_enabled=True,
                        skills_dirs=[str(skills.parent)], skills_home=str(tmp_path / "home"))
    req = [{"path": "skill://tiny/ref.txt", "start_line": 2, "end_line": 2}]
    assert json.loads(run.read_files(req))["results"][0]["status"] == "error"
    run.load_skill("tiny")
    assert json.loads(run.read_files(req))["results"] == [_ok("two\n", 0)]
    bad = json.loads(run.read_files([{"path": "skill://tiny/../../outside"}]))["results"][0]
    assert bad["status"] == "error"
    assert run.read_whole == set()


def test_disabled_runner_refuses(engine, tmp_path) -> None:
    (tmp_path / "a.txt").write_text("alpha\n", encoding="utf-8")
    run = engine.Runner(str(tmp_path), "stage", profile=RES)
    assert run.read_files_enabled is False
    assert run.dispatch("read_files", {"requests": [{"path": "a.txt"}]}) == \
        "error: no such tool read_files"
    assert "read_files" not in {d[0] for d in engine.tool_docs(run)}
    with pytest.raises(ValueError):
        engine.Runner(str(tmp_path), "stage", read_files_enabled=True)

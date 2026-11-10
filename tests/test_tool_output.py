"""Bounded, paginated tool output under the opt-in resilient-v1 profile.

Synthetic tmp_path fixtures and fakes only; no network, no live state.
"""
from __future__ import annotations

import io
import json
import os

import pytest

from test_engine_reasoning import engine, harness  # noqa: F401

RES = "resilient-v1"


@pytest.fixture()
def to(engine):
    return engine.capability_module("tool_output")


def _spy(n, text="entry-{}"):
    seen = {"n": 0}

    def gen():
        for i in range(n):
            seen["n"] += 1
            yield text.format(i)
    return gen(), seen


def test_bounded_lines_pages_without_consuming_past_cap(to) -> None:
    it, seen = _spy(5000)
    page = to.bounded_lines(it, 1000)
    out = to.render(page)
    assert len(out.encode("utf-8")) <= 1000
    assert page.truncated and page.next_offset and page.notice
    assert str(page.next_offset) in page.notice
    assert seen["n"] <= page.next_offset + 1          # one lookahead at most
    it2, _ = _spy(5000)
    nxt = to.bounded_lines(it2, 1000, start=page.next_offset)
    assert nxt.text.splitlines()[0] == f"entry-{page.next_offset}"


def test_bounded_lines_complete_page_has_no_notice(to) -> None:
    page = to.bounded_lines(iter(["a", "b"]), 1000)
    assert (page.text, page.truncated, page.next_offset, page.notice) == ("a\nb", False, None, "")
    assert to.render(page) == "a\nb"


@pytest.mark.parametrize("line", ["é" * 10000, "\u0001\u2028" * 5000, "x" * 70000],
                         ids=["unicode", "escaped", "ascii"])
def test_long_single_line_fits_utf8_and_json(to, line) -> None:
    page = to.bounded_lines(iter([line, "next"]), 500)
    out = to.render(page)
    assert len(out.encode("utf-8")) <= 500
    out.encode("utf-8").decode("utf-8")                # no split code point
    assert page.truncated and page.notice


def test_clip_text_respects_json_escaped_bytes(to) -> None:
    text = "\u0001é" * 5000
    clipped = to.clip_text(text, 600, notice="[cut]", measure=lambda s: len(json.dumps(s)))
    assert len(json.dumps(clipped)) <= 600 and clipped.endswith("[cut]")


@pytest.mark.parametrize("kw", [{"max_bytes": 0}, {"max_bytes": True}, {"start": -1},
                                {"start": True}, {"limit": 0}, {"max_bytes": 5}],
                         ids=["cap0", "capbool", "neg", "startbool", "lim0", "tiny"])
def test_bounded_lines_bad_args(to, kw) -> None:
    args = {"max_bytes": 100, **kw}
    with pytest.raises(ValueError):
        to.bounded_lines(iter(["x" * 50]), **args)


def test_bounded_lines_giant_record_advances_and_limit(to) -> None:
    page = to.bounded_lines(iter(["y" * 9999, "z"]), 200)
    assert page.next_offset == 1 and "clipped" in page.notice
    lim = to.bounded_lines(iter("abc"), 1000, limit=2)
    assert (lim.text, lim.next_offset) == ("a\nb", 2) and "offset=2" in lim.notice


def test_clip_text_fits_unchanged_and_too_small(to) -> None:
    assert to.clip_text("short", 100) == "short"
    with pytest.raises(ValueError):
        to.clip_text("x" * 100, 3, notice="[truncated]")


# ---- Runner wiring --------------------------------------------------------

def _runner(engine, root, profile=RES, mode="stage"):
    return engine.Runner(str(root), mode, profile=profile)


def test_list_dir_5000_entries_pages_bounded(engine, to, tmp_path) -> None:
    d = tmp_path / "many"
    d.mkdir()
    for i in range(5000):
        (d / f"f{i:05d}.txt").write_text("x")
    run = _runner(engine, tmp_path)
    first = run.list_dir("many")
    assert len(first.encode("utf-8")) <= to.PAGE_BYTES
    assert "f00000.txt" in first and "offset=" in first
    off = int(first.rsplit("offset=", 1)[1].split()[0].rstrip(".;)]"))
    second = run.list_dir("many", offset=off)
    assert f"f{off:05d}.txt" in second.splitlines()[0]
    small = run.list_dir("many", offset=10, limit=3)
    assert small.splitlines()[:3] == [f"f{i:05d}.txt  (1 b)" for i in (10, 11, 12)]


@pytest.mark.parametrize("kw", [{"offset": -1}, {"offset": True}, {"limit": 0},
                                {"limit": 10**9}, {"offset": "3"}, {"offset": 10**12}])
def test_invalid_paging_args_refused(engine, tmp_path, kw) -> None:
    run = _runner(engine, tmp_path)
    with pytest.raises(ValueError):
        run.list_dir(".", **kw)
    with pytest.raises(ValueError):
        run.grep("x", **kw)


def test_legacy_refuses_paging_and_lists_as_before(engine, tmp_path) -> None:
    (tmp_path / "a.txt").write_text("hi")
    run = _runner(engine, tmp_path, profile="legacy")
    assert run.list_dir(".") == "a.txt  (2 b)"
    with pytest.raises(TypeError):
        run.list_dir(".", offset=0)


def test_list_dir_skips_skip_dirs_and_outside_links(engine, tmp_path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    (root / ".git").mkdir()
    (root / "ok.txt").write_text("1")
    outside = tmp_path / "outside"
    outside.mkdir()
    try:
        os.symlink(outside, root / "link", target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable")
    out = _runner(engine, root).list_dir(".")
    assert ".git" not in out and "ok.txt" in out
    assert "link/" not in out


def test_grep_pages_and_skips_outside_symlinked_files(engine, to, tmp_path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    (root / "big.txt").write_text("".join(f"needle {i} {'é' * 50}\n" for i in range(3000)))
    (tmp_path / "secret.txt").write_text("needle SECRET\n")
    try:
        os.symlink(tmp_path / "secret.txt", root / "leak.txt")
    except (OSError, NotImplementedError):
        pass
    run = _runner(engine, root)
    for out in (run.grep("needle"), run._grep_page("needle", None, 0, None, use_rg=False)):
        assert len(out.encode("utf-8")) <= to.PAGE_BYTES
        assert "SECRET" not in out and "offset=" in out
    nxt = run.grep("needle", offset=5, limit=2)
    assert [ln.split(":", 2)[2].split()[1] for ln in nxt.splitlines()[:2]] == ["5", "6"]


def test_grep_rg_page_full_kills_and_reaps(engine, tmp_path, monkeypatch) -> None:
    procs = []

    class FakeRg:
        def __init__(self, argv, **kw):
            assert kw.get("stdout") is not None and "capture_output" not in kw
            self.argv, self.killed, self.waited, self.read = argv, False, False, 0
            self.stdout = self
            procs.append(self)

        def readline(self, size=-1):
            self.read += 1
            assert self.read < 100000, "unbounded read"
            line = b"a.txt:1:" + b"m" * 5000 + b"\n"
            return line[:size] if size and size > 0 else line

        def kill(self):
            self.killed = True

        def poll(self):
            return 0 if self.killed else None

        def wait(self, timeout=None):
            self.waited = True
            return 0

        def close(self):
            pass

    monkeypatch.setattr(engine.subprocess, "Popen", FakeRg)
    out = _runner(engine, tmp_path).grep("m")
    assert procs and procs[0].killed and procs[0].waited
    assert "offset=" in out


def _body(out):
    return out.split("\n", 1)[1]


def test_pending_read_honors_ranges_like_disk(engine, tmp_path) -> None:
    text = "".join(f"line {i}\n" for i in range(1, 51))
    (tmp_path / "f.txt").write_text(text)
    run = _runner(engine, tmp_path)
    disk = run.read_file("f.txt", start_line=3, end_line=5)
    run.pending["f.txt"] = text
    pend = run.read_file("f.txt", start_line=3, end_line=5)
    assert _body(pend) == _body(disk) == "line 3\nline 4\nline 5\n"
    assert "pending" in pend.split("\n", 1)[0]


def test_pending_range_start_past_eof(engine, tmp_path) -> None:
    run = _runner(engine, tmp_path)
    run.pending["f.txt"] = "a\nb\n"
    out = run.read_file("f.txt", start_line=10, end_line=12)
    assert "a" not in _body(out) and "b" not in _body(out)


def test_pending_max_read_cap(engine, tmp_path) -> None:
    run = _runner(engine, tmp_path)
    run.pending["big.txt"] = "x" * (engine.MAX_READ_CHARS * 3)
    assert run.read_file("big.txt").startswith("refused:")
    rng = run.read_file("big.txt", start_line=1, end_line=1)
    assert len(rng) < engine.MAX_READ_CHARS + 200


def test_pending_whole_read_legacy_unchanged(engine, tmp_path) -> None:
    run = _runner(engine, tmp_path, profile="legacy")
    run.pending["f.txt"] = "x" * (engine.MAX_READ_CHARS * 3)
    assert run.read_file("f.txt") == "(pending, not yet on disk)\n" + "x" * (engine.MAX_READ_CHARS * 3)


def test_resilient_disk_range_does_not_read_whole_file(engine, tmp_path, monkeypatch) -> None:
    (tmp_path / "huge.txt").write_text("short\n" + "y" * 500000 + "\nlast\n")
    run = _runner(engine, tmp_path)
    out = run.read_file("huge.txt", start_line=2, end_line=2)
    assert len(out) <= engine.MAX_READ_CHARS + 300 and "narrow" in out


def test_directory_scan_cap_is_explicit(engine, tmp_path, monkeypatch):
    run = _runner(engine, tmp_path)
    for i in range(5):
        (tmp_path / f"f{i}").write_text("x")
    monkeypatch.setattr(run._paged, "MAX_SCAN_ENTRIES", 3)
    monkeypatch.setattr(run._paged.os, "listdir", lambda *a: pytest.fail("unbounded listing"))
    out = run.list_dir(".")
    assert "incomplete" in out and "narrow" in out


def test_pagination_cap_does_not_offer_invalid_offset(engine, tmp_path, monkeypatch):
    run = _runner(engine, tmp_path)
    for i in range(6):
        (tmp_path / f"f{i}").write_text("x")
    monkeypatch.setattr(run._paged, "MAX_OFFSET", 2)
    out = run.list_dir(".", offset=2, limit=2)
    assert "incomplete" in out and "narrow" in out and "offset=4" not in out


def test_fallback_smart_case_nested_skip_and_giant_line(engine, tmp_path, monkeypatch):
    run = _runner(engine, tmp_path)
    nested = tmp_path / "nested" / ".git"
    nested.mkdir(parents=True)
    (nested / "secret").write_text("Needle SECRET\n")
    (tmp_path / "a.txt").write_text("Needle one\nneedle two\n")
    (tmp_path / "giant.txt").write_text("x" * 100000 + "Needle hidden\n")
    monkeypatch.setattr(run._paged, "MAX_SCAN_BYTES", 20000)
    out = run._grep_page("Needle", "*.txt", 0, None, use_rg=False)
    assert "Needle one" in out and "needle two" not in out and "SECRET" not in out
    assert "incomplete" in out and "narrow" in out


def test_rg_timeout_and_exception_reap(engine, tmp_path, monkeypatch):
    run = _runner(engine, tmp_path)
    procs = []
    timers = []

    class Timer:
        def __init__(self, seconds, callback):
            self.callback = callback
            self.cancelled = False
            timers.append(self)

        def start(self):
            pass

        def cancel(self):
            self.cancelled = True

    class Proc:
        def __init__(self, *a, **kw):
            self.stdout = self
            self.killed = self.waited = self.closed = False
            procs.append(self)

        def readline(self, size):
            if len(procs) == 1:
                timers[-1].callback()
                return b""
            raise OSError("synthetic pipe failure")

        def poll(self):
            return -9 if self.killed else None

        def kill(self):
            self.killed = True

        def wait(self, timeout=None):
            self.waited = True
            return -9

        def close(self):
            self.closed = True

    monkeypatch.setattr(run._paged.threading, "Timer", Timer)
    monkeypatch.setattr(engine.subprocess, "Popen", Proc)
    assert "timed out" in run.grep("x")
    with pytest.raises(OSError, match="pipe failure"):
        run.grep("x")
    assert all(p.killed and p.waited and p.closed for p in procs)
    assert all(t.cancelled for t in timers)


def test_rg_scan_cap_kills_before_wait(engine, tmp_path, monkeypatch):
    run = _runner(engine, tmp_path)
    procs = []

    class Proc:
        def __init__(self, *a, **kw):
            self.stdout = self
            self.killed = self.waited = self.closed = False
            procs.append(self)

        def readline(self, size):
            return b"m" * size

        def poll(self):
            return -9 if self.killed else None

        def kill(self):
            self.killed = True

        def wait(self, timeout=None):
            assert self.killed, "wait could deadlock on a full pipe"
            self.waited = True
            return -9

        def close(self):
            self.closed = True

    monkeypatch.setattr(run._paged, "MAX_SCAN_BYTES", 100)
    monkeypatch.setattr(engine.subprocess, "Popen", Proc)
    out = run.grep("m")
    assert "incomplete" in out and "narrow" in out
    assert procs[0].killed and procs[0].waited and procs[0].closed

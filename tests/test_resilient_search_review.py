"""Bounded-search review counterexamples; temporary roots and owned children only."""
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import psutil

ENGINE = Path(__file__).resolve().parents[1] / "engine" / "delegate.py"
CHILD = textwrap.dedent("""
    import importlib.util, sys, time
    spec = importlib.util.spec_from_file_location("agentcontrol_engine", sys.argv[1])
    eng = importlib.util.module_from_spec(spec); sys.modules[spec.name] = eng
    spec.loader.exec_module(eng)
    r = eng.Runner(sys.argv[2], profile="resilient-v1", skills_home=sys.argv[2], skills_root=sys.argv[2])
    r._paged.SEARCH_TIMEOUT = 0.2
    t = time.monotonic()
    out = r._grep_page("(a+)+$", None, 0, 10, use_rg=False)
    print(round(time.monotonic() - t, 2)); print(out)
""")


def _reap(proc):
    try:
        kids = psutil.Process(proc.pid).children(recursive=True)
    except psutil.NoSuchProcess:
        kids = []
    for p in kids + [proc]:
        try:
            p.kill()
        except (psutil.NoSuchProcess, ProcessLookupError, OSError):
            pass
    psutil.wait_procs([k for k in kids], timeout=5)
    proc.wait(timeout=5)


def test_fallback_regex_deadline(tmp_path):
    (tmp_path / "x.txt").write_text("a" * 128 + "!\n")
    proc = subprocess.Popen([sys.executable, "-c", CHILD, str(ENGINE), str(tmp_path)],
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    try:
        out, _ = proc.communicate(timeout=8)
    except subprocess.TimeoutExpired:
        out = None
    finally:
        _reap(proc)
    assert out is not None, "fallback regex was not interrupted by SEARCH_TIMEOUT"
    elapsed, _, text = out.partition("\n")
    assert float(elapsed) < 2 and "timed out" in text


from test_engine_reasoning import engine  # noqa: E402,F401
import io  # noqa: E402
import os  # noqa: E402
import pytest  # noqa: E402


def _runner(engine, root):
    return engine.Runner(str(root), profile="resilient-v1", skills_home=str(root),
                         skills_root=str(root))


def test_child_paging(engine, tmp_path):
    (tmp_path / "p.txt").write_text("".join(f"hit {i}\n" for i in range(1, 11)))
    out = _runner(engine, tmp_path)._grep_page("hit", "*.txt", 5, 2, use_rg=False)
    assert "p.txt:6:hit 6" in out and "p.txt:7:hit 7" in out and "hit 8" not in out
    assert "hit 5" not in out


def test_bad_utf8_incomplete(engine, tmp_path):
    (tmp_path / "b.txt").write_bytes(b"\xff\xfe bad\nneedle\x1b[31m\r\n")
    out = _runner(engine, tmp_path)._grep_page("needle", None, 0, 10, use_rg=False)
    assert "b.txt:2:needle" in out and "incomplete" in out


class Child:
    def __init__(self, data=b"", err=None):
        self.stdin, self.stdout = io.BytesIO(), io.BytesIO(data)
        if err:
            self.stdout.readline = lambda *a: (_ for _ in ()).throw(err)
        self.killed = self.waited = False

    def poll(self):
        return -9 if self.killed else None

    def kill(self):
        self.killed = True

    def wait(self, timeout=None):
        self.waited = True
        return 0


@pytest.mark.parametrize("rg,data,err", [
    (True, b"x:1:needle\n" * 50, None),          # page full
    (True, b"", OSError("synthetic read")),      # read error
    (False, b"not json\n", None),                # bad frame
    (False, b"", None),                          # child died, no done frame
], ids=["full", "rderr", "frame", "eof"])
def test_owned_cleanup(engine, tmp_path, monkeypatch, rg, data, err):
    runner = _runner(engine, tmp_path)
    child = Child(data, err)
    monkeypatch.setattr(runner._paged.subprocess, "Popen", lambda *a, **kw: child)
    try:
        out = runner._grep_page("needle", None, 0, 1, use_rg=rg)
    except OSError:
        out = "incomplete"
    assert child.killed and child.waited and child.stdout.closed
    assert "incomplete" in out or "next" in out.lower() or "offset" in out


def _junction(link, target):
    winapi = pytest.importorskip("_winapi")
    try:
        winapi.CreateJunction(str(target), str(link))
    except (AttributeError, OSError) as exc:
        pytest.skip(f"cannot create temporary junction: {exc}")


@pytest.mark.parametrize("kind", ["out", "skip"], ids=["out", "skip"])
def test_junction_rejected(engine, tmp_path, kind):
    root, outside = tmp_path / "root", tmp_path / "outside"
    (root / "node_modules").mkdir(parents=True)
    outside.mkdir()
    (outside / "s.txt").write_text("needle outside\n")
    (root / "node_modules" / "s.txt").write_text("needle skipped\n")
    _junction(root / "alias", outside if kind == "out" else root / "node_modules")
    out = _runner(engine, root)._grep_page("needle", None, 0, 10, use_rg=False)
    assert "needle" not in out.replace("(no matches)", "").split("[incomplete")[0]
    assert "junction" in out

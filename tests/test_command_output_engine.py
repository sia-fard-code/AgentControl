"""Resilient-v1 recoverable bounded command output (synthetic, tmp_path only)."""
from __future__ import annotations

import sys

from test_engine_reasoning import engine  # noqa: F401

EMIT = ["python", "emit.py"]
BIG = 200 * 1024  # well above the old 64KiB per-batch failure


def _emitter(tmp_path, body):
    (tmp_path / "emit.py").write_text(
        "import sys\n"
        "open('count.txt', 'a').write('x')\n" + body, encoding="utf-8")


def _runner(engine, tmp_path, monkeypatch, **kw):
    monkeypatch.setattr(engine, "ALLOWED_CMDS", [EMIT])
    monkeypatch.setattr(engine.shutil, "which", lambda name: sys.executable)
    (tmp_path / "art").mkdir()
    return engine.Runner(str(tmp_path), write_mode="disk", profile="resilient-v1",
                         artifact_dir=str(tmp_path / "art"), **kw)


def test_big_stdout_runs_once_and_stays_bounded(engine, tmp_path, monkeypatch):
    _emitter(tmp_path, f"sys.stdout.write('{{\"k\":\"' + 'a' * {BIG} + '\"}}')\n")
    runner = _runner(engine, tmp_path, monkeypatch)
    try:
        out = runner.run_command(EMIT)
        assert (tmp_path / "count.txt").read_text() == "x"  # ran exactly once
        assert out.startswith("exit=0\n")
        assert len(out.encode("utf-8")) <= 16 * 1024  # never the old 64KiB blowup
        assert "read_command_output" in out
    finally:
        runner.close()


def _refs(out):
    import re
    return re.findall(r'"ref": "(cmd://[^"]+)"', out)


def _read_all(runner, ref, limit=4096):
    import base64, json
    data, off = b"", 0
    while True:
        page = json.loads(runner.read_command_output(ref, offset=off, limit=limit))
        assert page["offset"] == off and len(page) == 9
        chunk = (page["data"].encode("utf-8") if page["encoding"] == "utf-8"
                 else base64.b64decode(page["data"]))
        data += chunk
        off = page["next_offset"]
        assert off == len(data)
        if page["eof"]:
            return data, page


def test_both_streams_nonzero_reconstruct_losslessly(engine, tmp_path, monkeypatch):
    _emitter(tmp_path,
             f"sys.stdout.buffer.write(b'\\xff\\x00\\xc3' + 'é☃'.encode() * {BIG // 4})\n"
             f"sys.stderr.buffer.write(b'E' * {BIG} + b'\\x80TAIL')\n"
             "sys.exit(3)\n")
    runner = _runner(engine, tmp_path, monkeypatch)
    try:
        out = runner.run_command(EMIT)
        assert out.startswith("exit=3\n") and len(out.encode()) <= 16 * 1024
        assert (tmp_path / "count.txt").read_text() == "x"
        ref_out, ref_err = _refs(out)
        got, last = _read_all(runner, ref_out, limit=4093)
        assert got == b"\xff\x00\xc3" + "é☃".encode() * (BIG // 4)
        assert last["capture_complete"] is True and last["discarded_bytes"] == 0
        got, _ = _read_all(runner, ref_err)
        assert got.endswith(b"\x80TAIL") and len(got) == BIG + 5
        assert (tmp_path / "count.txt").read_text() == "x"  # recovery never reruns
    finally:
        runner.close()


def test_read_rejects_foreign_refs_and_bad_ranges(engine, tmp_path, monkeypatch):
    _emitter(tmp_path, f"sys.stdout.write('z' * {BIG})\n")
    runner = _runner(engine, tmp_path, monkeypatch)
    try:
        (ref,) = _refs(runner.run_command(EMIT))
        bad = ["emit.py", "../emit.py", "artifact://x", "cmd://../emit.py",
               "https://example.invalid/x", 7, None]
        for r in bad:
            assert runner.read_command_output(r).startswith("error:")
        for off, lim in [(-1, 10), (BIG + 1, 10), (0, 0), (0, 4097),
                         (True, 10), (0, False), ("0", 10), (0, 1.5)]:
            assert runner.read_command_output(ref, offset=off, limit=lim).startswith("error:")
        import json
        page = json.loads(runner.read_command_output(ref, offset=BIG, limit=1))
        assert page["eof"] is True and page["data"] == ""
    finally:
        runner.close()


def test_read_detects_tampered_artifact(engine, tmp_path, monkeypatch):
    _emitter(tmp_path, f"sys.stdout.write('q' * {BIG})\n")
    runner = _runner(engine, tmp_path, monkeypatch)
    try:
        (ref,) = _refs(runner.run_command(EMIT))
        path = runner.caps.command_outputs[ref]["path"]
        with open(path, "r+b") as fh:
            fh.write(b"Q")
        assert runner.read_command_output(ref).startswith("error:")
    finally:
        runner.close()

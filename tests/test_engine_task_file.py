from __future__ import annotations

import importlib.util
import io
import os
import subprocess
import sys
from pathlib import Path

import pytest

ENGINE_PATH = Path(__file__).resolve().parents[1] / "engine" / "delegate.py"


def _load_engine():
    spec = importlib.util.spec_from_file_location("agentcontrol_engine", ENGINE_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def engine():
    return _load_engine()


def test_utf8_text_roundtrip(engine, tmp_path):
    p = tmp_path / "task.md"
    p.write_bytes("hello\nworld".encode("utf-8"))
    assert engine.read_task_file(str(p)) == "hello\nworld"


def test_utf8_bom_removed(engine, tmp_path):
    p = tmp_path / "task.md"
    p.write_bytes(b"\xef\xbb\xbf" + "hi".encode("utf-8"))
    assert engine.read_task_file(str(p)) == "hi"


def test_utf16_le_bom_decodes(engine, tmp_path):
    p = tmp_path / "task.md"
    p.write_bytes("some text".encode("utf-16"))
    assert engine.read_task_file(str(p)) == "some text"


def test_crlf_and_lone_cr_become_lf(engine, tmp_path):
    p = tmp_path / "task.md"
    p.write_bytes(b"a\r\nb\rc")
    assert engine.read_task_file(str(p)) == "a\nb\nc"


def test_empty_file_raises(engine, tmp_path):
    p = tmp_path / "task.md"
    p.write_bytes(b"")
    with pytest.raises(ValueError):
        engine.read_task_file(str(p))


def test_whitespace_only_raises(engine, tmp_path):
    p = tmp_path / "task.md"
    p.write_bytes(b"  \r\n\t ")
    with pytest.raises(ValueError):
        engine.read_task_file(str(p))


def test_invalid_bytes_raise(engine, tmp_path):
    p = tmp_path / "task.md"
    p.write_bytes(b"\x80abc")
    with pytest.raises(ValueError):
        engine.read_task_file(str(p))


def test_stdin_dash(engine):
    result = engine.read_task_file("-", stdin=io.BytesIO(b"from stdin\r\n"))
    assert result == "from stdin\n"


def test_missing_file_raises(engine, tmp_path):
    p = tmp_path / "nope.md"
    with pytest.raises(FileNotFoundError):
        engine.read_task_file(str(p))


def _run_cli(args, tmp_path):
    env = dict(os.environ)
    env["CLAUDE_AGENT_RUN_INDEX"] = str(tmp_path / "index.jsonl")
    return subprocess.run(
        [sys.executable, str(ENGINE_PATH), *args],
        cwd=str(tmp_path),
        capture_output=True,
        text=True,
        timeout=60,
        env=env,
    )


def test_cli_both_t_and_f(tmp_path):
    f = tmp_path / "task.md"
    f.write_text("hello", encoding="utf-8")
    proc = _run_cli(["-t", "x", "-f", str(f)], tmp_path)
    assert proc.returncode == 2
    assert "not allowed with argument" in proc.stderr


def test_cli_neither(tmp_path):
    proc = _run_cli([], tmp_path)
    assert proc.returncode == 2
    assert "one of the arguments -t/--task -f/--task-file is required" in proc.stderr


def test_cli_missing_file(tmp_path):
    proc = _run_cli(["-f", str(tmp_path / "nope.md")], tmp_path)
    assert proc.returncode == 2
    assert "cannot read --task-file" in proc.stderr


def test_cli_empty_file(tmp_path):
    f = tmp_path / "empty.md"
    f.write_bytes(b"")
    proc = _run_cli(["-f", str(f)], tmp_path)
    assert proc.returncode == 2
    assert "task file is empty" in proc.stderr


def test_cli_version(tmp_path):
    proc = _run_cli(["--version"], tmp_path)
    assert proc.returncode == 0
    assert "harness version" in proc.stdout

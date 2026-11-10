"""fanout.py's synthesis step runs through `delegate.py --oneshot`.

Offline: subprocess.Popen is monkeypatched, so no engine or gateway is touched.
"""
from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "engine" / "fanout.py"
DELEGATE = os.path.join(os.path.dirname(os.path.abspath(str(SCRIPT))), "delegate.py")

RESULTS = [
    (1, "gpt-5.6-sol", "memory pressure", "Report one: the heap grows.", None, "t1", 3.0),
    (2, "gpt-5.6-sol", "IDB lifetime", "Report two: transactions close.", None, "t2", 4.0),
    (3, "gpt-5.6-sol", "watchdog", None, "timed out", "t3", 9.0),
]


@pytest.fixture(scope="module")
def fanout():
    spec = importlib.util.spec_from_file_location("fanout_under_test", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeRun:
    def __init__(self, returncode=0, stdout="", stderr="", raises=None):
        self.result = (returncode, stdout, stderr)
        self.raises = raises
        self.calls = []
        self.prompt_path = None
        self.prompt_text = None

    def __call__(self, argv, *args, **kwargs):
        argv = list(argv)
        self.calls.append((argv, kwargs))
        self.prompt_path = argv[argv.index("-f") + 1]
        self.prompt_text = Path(self.prompt_path).read_text(encoding="utf-8")
        if self.raises is not None:
            raise self.raises
        code, out, err = self.result
        self.returncode, self.stdout, self.stderr = code, out, err
        self.pid = 4321
        return self

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def communicate(self, timeout=None):
        return self.stdout, self.stderr


def _patch(monkeypatch, fanout, run):
    monkeypatch.setattr(fanout.subprocess, "Popen", run)
    return run


def test_argv_is_a_oneshot_call(fanout, monkeypatch, tmp_path):
    run = _patch(monkeypatch, fanout, FakeRun(stdout="merged\n"))
    root = str(tmp_path)
    fanout.synthesize("Why?", RESULTS, "gpt-5.6-terra", root, "fanout")
    assert len(run.calls) == 1
    argv, kwargs = run.calls[0]
    index = argv.index("--launch-id")
    assert argv == [sys.executable, "-u", DELEGATE, "--oneshot", "-C", root,
                    "-m", "gpt-5.6-terra", "-e", "high",
                    "--label", "fanout-synthesis"] + argv[index:index + 4] + ["-f", run.prompt_path]
    assert kwargs["stdout"] == subprocess.PIPE and kwargs["stderr"] == subprocess.PIPE


def test_label_follows_the_fanout_label(fanout, monkeypatch, tmp_path):
    run = _patch(monkeypatch, fanout, FakeRun(stdout="ok"))
    fanout.synthesize("Why?", RESULTS, "m", str(tmp_path), "other-run")
    argv = run.calls[0][0]
    assert argv[argv.index("--label") + 1] == "other-run-synthesis"


def test_default_label_is_fanout_synthesis(fanout, monkeypatch, tmp_path):
    run = _patch(monkeypatch, fanout, FakeRun(stdout="ok"))
    fanout.synthesize("Why?", RESULTS, "m", str(tmp_path))
    argv = run.calls[0][0]
    assert argv[argv.index("--label") + 1] == f"{fanout.FANOUT_LABEL}-synthesis"
    assert fanout.FANOUT_LABEL == "fanout"


def test_success_returns_stdout_and_prompt_file_is_removed(fanout, monkeypatch, tmp_path):
    run = _patch(monkeypatch, fanout, FakeRun(
        stdout="They agree on X.\nThey disagree on Y.\n",
        stderr="[sent 900 tok to gpt-5.6-terra; returned 40 tok]\n"))
    out = fanout.synthesize("Why does it crash?", RESULTS, "gpt-5.6-terra",
                            str(tmp_path), "fanout")
    assert out == "They agree on X.\nThey disagree on Y.\n"
    assert "Why does it crash?" in run.prompt_text
    assert "Report one: the heap grows." in run.prompt_text
    assert "Report two: transactions close." in run.prompt_text
    assert "AGENT 3" not in run.prompt_text  # no report, not included
    assert not os.path.exists(run.prompt_path)


@pytest.mark.parametrize("code", [1, 2])
def test_failure_returns_last_stderr_line(fanout, monkeypatch, tmp_path, code):
    run = _patch(monkeypatch, fanout, FakeRun(
        returncode=code, stdout="partial?",
        stderr="note: something\nerror: gateway returned HTTP 502\n\n"))
    out = fanout.synthesize("Why?", RESULTS, "m", str(tmp_path), "fanout")
    assert out == "(synthesis failed: error: gateway returned HTTP 502)"
    assert not os.path.exists(run.prompt_path)


def test_failure_with_empty_stderr_still_reports(fanout, monkeypatch, tmp_path):
    run = _patch(monkeypatch, fanout, FakeRun(returncode=1, stderr=""))
    out = fanout.synthesize("Why?", RESULTS, "m", str(tmp_path), "fanout")
    assert out.startswith("(synthesis failed: ")
    assert out.endswith(")")
    assert not os.path.exists(run.prompt_path)


def test_launch_error_is_reported_and_file_removed(fanout, monkeypatch, tmp_path):
    run = _patch(monkeypatch, fanout, FakeRun(raises=OSError("no python")))
    out = fanout.synthesize("Why?", RESULTS, "m", str(tmp_path), "fanout")
    assert out.startswith("(synthesis failed: ")
    assert "no python" in out
    assert not os.path.exists(run.prompt_path)


def test_fanout_has_no_own_gateway_code():
    text = SCRIPT.read_text(encoding="utf-8")
    for needle in ("urllib", "OPENWEBUI_API_KEY", "/api/chat/completions"):
        assert needle not in text

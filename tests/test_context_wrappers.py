"""Context-option forwarding in ask-model.py and fanout.py; subprocess is always faked."""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

ENGINE = Path(__file__).resolve().parents[1] / "engine"
CTX = ["--context-policy", "model-v1", "--context-window-tokens", "200000",
       "--context-reply-tokens", "8000", "--context-safety-tokens", "5000"]


def _ctx(argv):
    return [a for a in argv if a.startswith(("--context-", "--auto-compact",
                                             "--no-auto-compact", "--compact-"))]


def _load(name, file):
    spec = importlib.util.spec_from_file_location(name, ENGINE / file)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def ask(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.delenv("OPENWEBUI_MODEL", raising=False)
    return _load("ask_ctx", "ask-model.py")


def _ask_argv(ask, extra):
    args = ask.parse_args(["-p", "synthetic", "-m", "m1"] + extra)
    return ask.build_argv(args, "brief.txt")


def test_ask_no_context_flags_injects_nothing(ask):
    assert not _ctx(_ask_argv(ask, ["--tools"]))
    assert not _ctx(_ask_argv(ask, []))


def test_ask_oneshot_forwards_explicit_values_verbatim(ask):
    argv = _ask_argv(ask, CTX + ["--no-auto-compact"])
    assert argv[argv.index("--context-policy"):][:8] == CTX
    assert "--no-auto-compact" in argv and "--auto-compact" not in argv
    assert "--oneshot" in argv and "--allow-prompt-tools" not in argv


def test_ask_tools_forwards_auto_compact_and_fraction(ask):
    argv = _ask_argv(ask, ["--tools", "--auto-compact", "--compact-fraction", "0.6"])
    assert "--oneshot" not in argv and "--auto-compact" in argv
    assert argv[argv.index("--compact-fraction") + 1] == "0.6"
    assert argv.count("-C") == 1 and argv[argv.index("-C") + 1] == str(Path.cwd())


def test_ask_oneshot_auto_compact_rejected_before_model(ask, monkeypatch, capsys):
    monkeypatch.setattr(ask.subprocess, "Popen", lambda *a, **k: pytest.fail("launched"))
    with pytest.raises(SystemExit) as exc:
        ask.main(["-p", "synthetic", "-m", "m1", "--auto-compact", "--compact-fraction", "0.123"])
    assert exc.value.code == 2
    err = capsys.readouterr().err
    assert "compaction_unavailable_in_oneshot" in err and "0.123" not in err


class Cap:
    """Fake Popen: records argv, returns an empty successful child."""
    calls: list = []
    pid, returncode = 1, 0

    def __init__(self, argv, *a, **k):
        Cap.calls.append(list(argv))

    def communicate(self, timeout=None):
        return "synthetic report", ""

    def __enter__(self):
        return self

    def __exit__(self, *a):
        pass


@pytest.fixture
def fan(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    module = _load("fanout_ctx", "fanout.py")
    Cap.calls = []
    monkeypatch.setattr(module.subprocess, "Popen", Cap)
    monkeypatch.setattr(module.tempfile, "tempdir", str(tmp_path))
    return module


def test_fanout_child_gets_explicit_options(fan, tmp_path):
    ctx = {"context_policy": "model-v1", "context_window_tokens": "200000",
           "auto_compact": False, "compact_fraction": "0.5"}
    fan.run_agent(1, "m1", "high", "lens", "task", str(tmp_path), 5, "s", 9, context=ctx)
    argv = Cap.calls[0]
    assert argv[argv.index("--context-policy") + 1] == "model-v1"
    assert argv[argv.index("--context-window-tokens") + 1] == "200000"
    assert "--no-auto-compact" in argv and argv[argv.index("--compact-fraction") + 1] == "0.5"
    assert "--context-reply-tokens" not in argv


def test_fanout_synthesis_gets_window_not_auto_compact(fan, tmp_path):
    ctx = {"context_policy": "model-v1", "context_window_tokens": "200000",
           "context_reply_tokens": "8000", "auto_compact": True, "compact_fraction": "0.5"}
    fan.synthesize("task", [(1, "m1", "lens", "r")], "m2", str(tmp_path), context=ctx)
    argv = Cap.calls[0]
    assert "--oneshot" in argv and argv[argv.index("--context-reply-tokens") + 1] == "8000"
    assert not [a for a in _ctx(argv) if "compact" in a]


def test_fanout_without_context_injects_nothing(fan, tmp_path):
    fan.run_agent(1, "m1", "high", "lens", "task", str(tmp_path), 5, "s", 9)
    assert not _ctx(Cap.calls[0])

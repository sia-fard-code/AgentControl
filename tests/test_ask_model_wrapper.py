"""engine/ask-model.py is a thin wrapper over `delegate.py --oneshot`.

Offline: subprocess.Popen is monkeypatched, so no engine or gateway is touched.
"""
from __future__ import annotations

import importlib.util
import itertools
import os
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "engine" / "ask-model.py"
DELEGATE = os.path.join(os.path.dirname(os.path.abspath(str(SCRIPT))), "delegate.py")


def _load():
    spec = importlib.util.spec_from_file_location("ask_model_wrapper", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeRun:
    """Records each call and snapshots the prompt file while it still exists."""

    def __init__(self, returncode: int = 0, raises: BaseException | None = None):
        self.pid = 4321
        self.returncode = returncode
        self.raises = raises
        self.calls: list[tuple[list, dict]] = []
        self.prompt_path: str | None = None
        self.prompt_text: str | None = None

    def __call__(self, argv, *args, **kwargs):
        argv = list(argv)
        self.calls.append((argv, kwargs))
        self.prompt_path = argv[argv.index("-f") + 1]
        self.prompt_text = Path(self.prompt_path).read_text(encoding="utf-8")
        if self.raises is not None:
            raise self.raises
        return self

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def wait(self):
        return self.returncode


def launch_flags(argv):
    index = argv.index("--launch-receipt")
    assert Path(argv[index + 1]).is_absolute()
    assert str(uuid.UUID(argv[index + 3])) == argv[index + 3]
    return argv[index:index + 4]


@pytest.fixture
def ask(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("OPENWEBUI_MODEL", raising=False)
    module = _load()
    monkeypatch.setattr(module.tempfile, "tempdir", str(tmp_path))
    return module


@pytest.fixture
def fake(ask, monkeypatch):
    run = FakeRun()
    monkeypatch.setattr(ask.subprocess, "Popen", run)
    return run


EFFORTS = [None, "none", "high", "max"]
TEMPS = [None, "0.2"]
FILE_SETS = [[], ["a.txt"], ["a.txt", os.path.join("sub dir", "c.md")]]
LABELS = [None, "my-label"]
ROOTS = [None, "given"]


@pytest.mark.parametrize(
    "effort,temp,files,label,root",
    list(itertools.product(EFFORTS, TEMPS, FILE_SETS, LABELS, ROOTS)),
)
def test_argv_mapping(ask, fake, tmp_path, effort, temp, files, label, root):
    model = "gpt-5.6-sol"
    cli = ["-m", model, "-p", "do the thing"]
    if effort:
        cli += ["-e", effort]
    if temp:
        cli += ["--temperature", temp]
    if label:
        cli += ["--label", label]
    root_value = str(tmp_path / "proj") if root else os.getcwd()
    if root:
        cli += ["-C", root_value]
    cli += files

    assert ask.main(cli) == 0
    assert len(fake.calls) == 1
    argv, kwargs = fake.calls[0]

    expected = [sys.executable, "-u", DELEGATE, "--oneshot", "-C", root_value,
                "-m", model, "--label", label or "ask-gpt-5-6-sol"]
    if effort:
        expected += ["-e", effort]
    if temp:
        expected += ["--temperature", str(float(temp))]
    for f in files:
        expected += ["--attach", f]
    expected += launch_flags(argv) + ["-f", fake.prompt_path]
    assert argv == expected
    # Output is passed straight through, never captured.
    assert not kwargs.get("capture_output")
    assert "stdout" not in kwargs and "stderr" not in kwargs


def test_long_options_are_accepted(ask, fake, tmp_path):
    root = str(tmp_path)
    assert ask.main(["--model", "m1", "--effort", "low", "--prompt", "x",
                     "--root", root, "--label", "L", "f.py"]) == 0
    argv = fake.calls[0][0]
    assert argv[3:] == ["--oneshot", "-C", root, "-m", "m1", "--label", "L",
                         "-e", "low", "--attach", "f.py"] + launch_flags(argv) + ["-f", fake.prompt_path]


@pytest.mark.parametrize("effort", ["none", "low", "medium", "high", "xhigh", "max"])
def test_every_effort_choice_is_forwarded(ask, fake, effort):
    assert ask.main(["-m", "m", "-p", "x", "-e", effort]) == 0
    argv = fake.calls[0][0]
    assert argv[argv.index("-e") + 1] == effort


def test_invalid_effort_is_a_usage_error(ask, fake):
    with pytest.raises(SystemExit) as exc:
        ask.main(["-m", "m", "-p", "x", "-e", "extreme"])
    assert exc.value.code == 2
    assert fake.calls == []


@pytest.mark.parametrize(
    "model,label",
    [
        ("gpt-5.6-sol", "ask-gpt-5-6-sol"),
        ("foo..//bar", "ask-foo-bar"),
        ("anthropic_tool_call.claude-opus-4", "ask-anthropic-tool-call-claude-opus-4"),
        ("anthropic_tool_call.claude-sonnet-4-5-20250929",
         "ask-anthropic-tool-call-claude-sonnet-4-"),
    ],
)
def test_default_label(ask, fake, model, label):
    assert ask.main(["-m", model, "-p", "x"]) == 0
    argv = fake.calls[0][0]
    got = argv[argv.index("--label") + 1]
    assert got == label
    assert len(got) <= 40


def test_model_defaults_from_environment(ask, fake, monkeypatch):
    monkeypatch.setenv("OPENWEBUI_MODEL", "env-model")
    assert ask.main(["-p", "x"]) == 0
    argv = fake.calls[0][0]
    assert argv[argv.index("-m") + 1] == "env-model"
    assert argv[argv.index("--label") + 1] == "ask-env-model"


def test_missing_prompt_is_a_usage_error(ask, fake):
    with pytest.raises(SystemExit) as exc:
        ask.main(["-m", "m", "file.txt"])
    assert exc.value.code == 2
    assert fake.calls == []


def test_missing_model_is_a_usage_error(ask, fake):
    with pytest.raises(SystemExit) as exc:
        ask.main(["-p", "x"])
    assert exc.value.code == 2
    assert fake.calls == []


def test_prompt_file_holds_prompt_and_is_deleted(ask, fake):
    prompt = "Summarise \u2014 caf\u00e9 \u2713\nsecond line"
    assert ask.main(["-m", "m", "-p", prompt]) == 0
    assert fake.prompt_text == prompt
    assert not os.path.exists(fake.prompt_path)


@pytest.mark.parametrize("code", [0, 1, 2, 7])
def test_exit_code_passes_through_and_file_is_deleted(ask, monkeypatch, code):
    run = FakeRun(returncode=code)
    monkeypatch.setattr(ask.subprocess, "Popen", run)
    assert ask.main(["-m", "m", "-p", "x"]) == code
    assert not os.path.exists(run.prompt_path)


def test_prompt_file_deleted_when_launch_raises(ask, monkeypatch):
    run = FakeRun(raises=OSError("cannot exec"))
    monkeypatch.setattr(ask.subprocess, "Popen", run)
    with pytest.raises(OSError):
        ask.main(["-m", "m", "-p", "x"])
    assert run.prompt_text == "x"
    assert not os.path.exists(run.prompt_path)


@pytest.mark.parametrize("tools", [False, True])
def test_launch_receipt_identity_is_forwarded(ask, tmp_path, tools):
    receipt = str(tmp_path / "fresh.launch.jsonl")
    launch_id = "c356dcf3-66ce-4f96-8952-89584b92db99"
    flags = ["--tools"] if tools else []
    args = ask.parse_args(["-p", "synthetic task", "-m", "gpt-6.1-sol",
                           "-C", str(tmp_path), "--launch-receipt", receipt,
                           "--launch-id", launch_id, *flags])
    argv = ask.build_argv(args, str(tmp_path / "brief.txt"))
    assert argv[argv.index("--launch-receipt") + 1] == receipt
    assert argv[argv.index("--launch-id") + 1] == launch_id
    assert not Path(receipt).exists()


@pytest.mark.parametrize("flags", [["--launch-id", "c356dcf3-66ce-4f96-8952-89584b92db99"],
                                   ["--launch-receipt", "relative.jsonl"],
                                   ["--launch-receipt", "relative.jsonl", "--launch-id",
                                    "c356dcf3-66ce-4f96-8952-89584b92db99"]])
def test_invalid_receipt_pair_rejected_before_side_effects(ask, fake, tmp_path, flags):
    with pytest.raises(SystemExit) as caught:
        ask.main(["-m", "synthetic", "-p", "synthetic", *flags])
    assert caught.value.code == 2 and fake.calls == []
    assert not list(tmp_path.glob("*.diagnostics.jsonl"))


class FakeEngine:
    def __init__(self, ids):
        self.ids = ids
        self.refresh_args = None

    def load_cfg(self):
        return "http://gw", "k"

    def refresh_models(self, url, key):
        self.refresh_args = (url, key)
        return self.ids


def _no_subprocess(*args, **kwargs):
    raise AssertionError("subprocess.run must not be called for --list-models")


def test_list_models_prints_sorted_ids(ask, monkeypatch, capsys):
    engine = FakeEngine({"zeta", "alpha", "mid-1"})
    monkeypatch.setattr(ask, "load_engine", lambda: engine)
    monkeypatch.setattr(ask.subprocess, "run", _no_subprocess)
    assert ask.main(["--list-models"]) == 0
    out = capsys.readouterr().out
    assert out.splitlines() == ["alpha", "mid-1", "zeta"]
    assert engine.refresh_args == ("http://gw", "k")


def test_list_models_failure_exits_1(ask, monkeypatch, capsys):
    monkeypatch.setattr(ask, "load_engine", lambda: FakeEngine(None))
    monkeypatch.setattr(ask.subprocess, "run", _no_subprocess)
    assert ask.main(["--list-models"]) == 1
    captured = capsys.readouterr()
    assert "error: could not list models from the gateway" in captured.err
    assert captured.out == ""


def test_missing_prompt_without_list_models_still_errors(ask, fake, capsys):
    with pytest.raises(SystemExit) as exc:
        ask.main(["-m", "m"])
    assert exc.value.code == 2
    assert "-p/--prompt is required" in capsys.readouterr().err
    assert fake.calls == []


def test_load_engine_loads_real_delegate(ask):
    mod = ask.load_engine()
    assert callable(mod.load_cfg) and callable(mod.refresh_models)


def test_docstring_documents_list_models(ask):
    assert "ask-model.py --list-models" in ask.__doc__


def test_wrapper_has_no_http_or_config_code():
    text = SCRIPT.read_text(encoding="utf-8")
    for needle in ("urllib", "OPENWEBUI_API_KEY", ".openwebui.env", "reasoning_effort"):
        assert needle not in text

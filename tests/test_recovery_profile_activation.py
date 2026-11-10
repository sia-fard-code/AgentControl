"""Operator activation is execution policy, never a new approval authority."""
import ast
import importlib
import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

ENGINE = Path(__file__).resolve().parents[1] / "engine"
SETTINGS = {"schema": 1, "default_profile": "resilient-v1"}


@pytest.fixture
def modules(monkeypatch):
    monkeypatch.syspath_prepend(str(ENGINE))
    return importlib.import_module("execution_profile"), importlib.import_module("delegate")


@pytest.fixture
def settings_path():
    path = Path.home() / ".claude" / "engine-settings.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


@pytest.mark.parametrize("options,profile,provenance", [
    ({}, "legacy", "legacy-default"),
    ({"settings": SETTINGS}, "resilient-v1", "operator-settings"),
    ({"profile": "legacy", "settings": SETTINGS}, "legacy", "explicit"),
    ({"profile": "resilient-v1", "oneshot": True}, "resilient-v1", "explicit"),
    ({"oneshot": True, "settings": SETTINGS}, "legacy", "oneshot"),
    ({"supervisor_manifest": True, "settings": SETTINGS}, "legacy", "supervisor-manifest"),
    ({"approved_worker": True, "settings": SETTINGS}, "legacy", "approved-worker"),
])
def test_pure_precedence(modules, options, profile, provenance):
    resolver, _ = modules
    assert resolver.resolve_profile(**options) == {"profile": profile, "provenance": provenance}


def cold(monkeypatch, delegate, argv):
    def forbidden(*args, **kwargs):
        pytest.fail("cold profile inspection invoked execution infrastructure")
    monkeypatch.setattr(delegate, "load_cfg", forbidden)
    monkeypatch.setattr(delegate, "capability_module", forbidden)
    monkeypatch.setattr(delegate, "_bootstrap_launch_receipt", forbidden)
    monkeypatch.setattr(delegate, "_startup_diagnostics", forbidden)
    monkeypatch.setattr(delegate, "request_evidence", forbidden)
    monkeypatch.setattr(sys, "argv", ["delegate.py", *argv])
    delegate.main()


@pytest.mark.parametrize("argv,worker,expected", [
    ([], False, {"profile": "resilient-v1", "provenance": "operator-settings"}),
    (["--profile", "legacy"], False, {"profile": "legacy", "provenance": "explicit"}),
    (["--profile", "resilient-v1"], False, {"profile": "resilient-v1", "provenance": "explicit"}),
    (["--oneshot"], False, {"profile": "legacy", "provenance": "oneshot"}),
    (["--oneshot", "--profile", "resilient-v1"], False,
     {"profile": "resilient-v1", "provenance": "explicit"}),
    ([], True, {"profile": "legacy", "provenance": "approved-worker"}),
    (["--supervisor-manifest", "not-opened"], False,
     {"profile": "legacy", "provenance": "supervisor-manifest"}),
    (["--supervisor-manifest-sha256", "not-opened"], False,
     {"profile": "legacy", "provenance": "supervisor-manifest"}),
])
def test_cold_profile(modules, settings_path, monkeypatch, capsys, argv, worker, expected):
    _, delegate = modules
    settings_path.write_text(json.dumps(SETTINGS), encoding="utf-8")
    monkeypatch.delenv("_HARNESS_APPROVED_WORKER", raising=False)
    if worker:
        monkeypatch.setenv("_HARNESS_APPROVED_WORKER", "1")
    if argv or worker:
        def forbidden():
            pytest.fail("fenced or explicit path read operator settings")
        monkeypatch.setattr(delegate, "_read_engine_settings", forbidden)
    cold(monkeypatch, delegate, ["--show-profile", *argv])
    assert json.loads(capsys.readouterr().out) == expected


def test_missing_and_handle_time_home(modules, tmp_path, monkeypatch, capsys):
    _, delegate = modules
    monkeypatch.delenv("_HARNESS_APPROVED_WORKER", raising=False)
    cold(monkeypatch, delegate, ["--show-profile"])
    assert json.loads(capsys.readouterr().out) == {
        "profile": "legacy", "provenance": "legacy-default"}
    home = tmp_path / "second-home"
    (home / ".claude").mkdir(parents=True)
    (home / ".claude" / "engine-settings.json").write_text(json.dumps(SETTINGS))
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    cold(monkeypatch, delegate, ["--show-profile"])
    assert json.loads(capsys.readouterr().out)["provenance"] == "operator-settings"


@pytest.mark.parametrize("raw", [
    b"", b"[]", b"null", b"{secret-invalid-json", b"x" * 4097,
    b'{"schema":true,"default_profile":"resilient-v1"}',
    b'{"schema":2,"default_profile":"resilient-v1"}',
    b'{"schema":1,"default_profile":"unknown"}',
    b'{"schema":1,"default_profile":"resilient-v1","extra":0}',
    b'{"schema":1,"schema":1,"default_profile":"resilient-v1"}',
    b'{"schema":1,"default_profile":NaN}',
    b'{"schema":1,"default_profile":Infinity}',
    b'{"schema":1.0,"default_profile":"resilient-v1"}',
    b'{"schema":1}', b'\xff',
])
def test_bad_settings_fail_closed(modules, settings_path, monkeypatch, capsys, raw):
    _, delegate = modules
    monkeypatch.delenv("_HARNESS_APPROVED_WORKER", raising=False)
    settings_path.write_bytes(raw)
    with pytest.raises(SystemExit) as exc:
        cold(monkeypatch, delegate, ["--show-profile"])
    assert exc.value.code == 2
    captured = capsys.readouterr()
    assert not captured.out
    assert captured.err.strip() == "error: execution profile refused: invalid_settings"


@pytest.mark.parametrize("kind", ["symlink", "ancestor", "hardlink", "directory"])
def test_nonlocal_or_linked_settings_rejected(modules, settings_path, tmp_path, kind):
    _, delegate = modules
    target = tmp_path / "target.json"
    target.write_text(json.dumps(SETTINGS))
    try:
        if kind == "symlink":
            settings_path.symlink_to(target)
        elif kind == "ancestor":
            settings_path.parent.rmdir()
            target_dir = tmp_path / "target-dir"
            target_dir.mkdir()
            (target_dir / settings_path.name).write_text(json.dumps(SETTINGS))
            settings_path.parent.symlink_to(target_dir, target_is_directory=True)
        elif kind == "hardlink":
            os.link(target, settings_path)
        else:
            settings_path.mkdir()
    except OSError as exc:
        pytest.skip(f"filesystem cannot create test link: {exc}")
    with pytest.raises(ValueError, match="^invalid_settings$"):
        delegate._read_engine_settings()


def test_io_failure_not_absence(modules, settings_path, monkeypatch):
    _, delegate = modules
    settings_path.write_text(json.dumps(SETTINGS))
    def denied(*args, **kwargs):
        raise PermissionError("must not disclose filesystem detail")
    monkeypatch.setattr(os, "open", denied)
    with pytest.raises(ValueError, match="^invalid_settings$"):
        delegate._read_engine_settings()


@pytest.mark.parametrize("argv,worker", [
    (["--profile", "legacy"], "0"),
    (["--profile", "resilient-v1"], "1"),
    (["--oneshot"], "0"),
    (["--supervisor-manifest", "missing"], "0"),
    (["--supervisor-manifest-sha256", "invalid"], "0"),
    ([], "1"),
])
def test_fences_skip_invalid_settings(modules, settings_path, monkeypatch, capsys, argv, worker):
    _, delegate = modules
    settings_path.write_bytes(b"private-invalid-body")
    monkeypatch.setenv("_HARNESS_APPROVED_WORKER", worker)
    cold(monkeypatch, delegate, ["--show-profile", *argv])
    assert set(json.loads(capsys.readouterr().out)) == {"profile", "provenance"}


def test_maximum_bytes_and_operator_legacy(modules, settings_path):
    resolver, delegate = modules
    raw = json.dumps({"schema": 1, "default_profile": "legacy"}).encode()
    settings_path.write_bytes(raw + b" " * (4096 - len(raw)))
    assert resolver.resolve_profile(settings=delegate._read_engine_settings()) == {
        "profile": "legacy", "provenance": "operator-settings"}


def test_reparse_ancestor_rejected(modules, settings_path, monkeypatch):
    _, delegate = modules
    settings_path.write_text(json.dumps(SETTINGS))
    original = os.lstat
    def reparse(path, *args, **kwargs):
        info = original(path, *args, **kwargs)
        if Path(path) == settings_path.parent:
            return SimpleNamespace(st_mode=info.st_mode, st_file_attributes=0x400)
        return info
    monkeypatch.setattr(os, "lstat", reparse)
    with pytest.raises(ValueError, match="^invalid_settings$"):
        delegate._read_engine_settings()


@pytest.mark.parametrize("flag", ["--version", "--supervisor-capabilities",
                                   "--supervisor-capabilities-v2"])
def test_existing_probes_remain_cold(modules, settings_path, monkeypatch, capsys, flag):
    _, delegate = modules
    settings_path.write_bytes(b"invalid-private-body")
    def forbidden(*args, **kwargs):
        pytest.fail("existing cold probe touched startup or operator settings")
    for name in ("load_cfg", "_read_engine_settings", "_bootstrap_launch_receipt",
                 "_startup_diagnostics", "request_evidence"):
        monkeypatch.setattr(delegate, name, forbidden)
    monkeypatch.setattr(sys, "argv", ["delegate.py", flag])
    delegate.main()
    output = capsys.readouterr().out
    if flag == "--version":
        assert output == f"delegate.py harness version 6 (policy file: {delegate.POLICY_FILE})\n"
    else:
        assert isinstance(json.loads(output), dict)


@pytest.mark.parametrize("name,method", [
    ("supervisor_harness.py", "create_suspended"),
    ("supervisor_harness_v3.py", "create_suspended_atomic"),
])
def test_approved_launcher_child_environment(name, method, monkeypatch, tmp_path):
    # Execute the actual launcher's env construction and process call, isolated
    # from unrelated approval/materialization setup covered by the harness suites.
    tree = ast.parse((ENGINE / name).read_text(encoding="utf-8"))
    call = next(n for n in ast.walk(tree) if isinstance(n, ast.Call)
                and isinstance(n.func, ast.Attribute) and n.func.attr == method)
    assignment = next(n for n in ast.walk(tree) if isinstance(n, ast.Assign) and n.value is call)
    parent = next(n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                  and assignment in list(ast.walk(n)))
    env_nodes = [n for n in ast.walk(parent) if isinstance(n, ast.Assign)
                 and n.lineno < assignment.lineno
                 and any(isinstance(t, ast.Name) and t.id == "env" or
                         isinstance(t, ast.Subscript) and isinstance(t.value, ast.Name)
                         and t.value.id == "env" for t in n.targets)]
    monkeypatch.setenv("GIT_DIR", "must-filter")
    monkeypatch.setenv("PROFILE_TEST_INHERITED", "keep")
    monkeypatch.delenv("_HARNESS_APPROVED_WORKER", raising=False)
    before = dict(os.environ)
    argv = ["python", "approved-argv-unchanged"]
    captured = {}
    def create(actual_argv, **kwargs):
        assert actual_argv is argv
        captured.update(kwargs)
    namespace = {"os": os, "argv": argv, "job": SimpleNamespace(**{method: create}),
                 "self": SimpleNamespace(envelope=SimpleNamespace(project_root=str(tmp_path))),
                 "paths": {"stdout": tmp_path / "out", "stderr": tmp_path / "err"}}
    code = ast.fix_missing_locations(ast.Module(body=[*sorted(env_nodes, key=lambda n: n.lineno), assignment],
                                               type_ignores=[]))
    exec(compile(code, name, "exec"), namespace)
    assert captured["env"] == {**{k: v for k, v in before.items()
                                  if not k.upper().startswith("GIT_")},
                               "_HARNESS_APPROVED_WORKER": "1"}
    assert dict(os.environ) == before

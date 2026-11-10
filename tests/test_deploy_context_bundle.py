from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "deploy_engine.py"
CONTEXT = (
    "context_policy.py", "context_tokens.py", "context_receipts.py", "context_admission.py",
    "context_runtime.py", "context_compaction.py", "context_engine.py", "context_storage.py",
)
NEW_SOURCE = ("HARNESS_VERSION = 6\n"
              "def engine():\n    return capability_module('context_engine')\n")


@pytest.fixture(scope="module")
def dm():
    spec = importlib.util.spec_from_file_location("deploy_engine_context", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def dirs(dm, tmp_path):
    src, dst = tmp_path / "engine", tmp_path / "bin"
    src.mkdir()
    dst.mkdir()
    (src / "delegate.py").write_text(NEW_SOURCE, encoding="utf-8")
    (dst / "delegate.py").write_text("HARNESS_VERSION = 5\n", encoding="utf-8")
    for name in (dm._HELPER_NAMES + dm._BUNDLE_NAMES + dm._SUPERVISION_NAMES
                 + dm._GOAL_SUPERVISOR_NAMES + dm._RECOVERY_NAMES):
        (src / name).write_text(f"NAME = {name!r}\n", encoding="utf-8")
    return src, dst


def _add(directory, names, tag="new"):
    for name in names:
        (directory / name).write_text(f"TAG = {tag!r}  # {name}\n", encoding="utf-8")


def _snap(directory):
    return {p.name: p.read_bytes() for p in directory.iterdir() if ".backup-" not in p.name}


def _deploy(dm, src, dst, **kw):
    kw.setdefault("test_command", ["python", "-c", "print('ok')"])
    return dm.deploy(src / "delegate.py", dst / "delegate.py", apply=True, **kw)


def test_family_constants(dm):
    assert dm._CONTEXT_NAMES == CONTEXT
    assert set(CONTEXT) <= set(dm._ALL_NAMES)


def test_diagnostics_fingerprint_covers_context_family():
    spec = importlib.util.spec_from_file_location(
        "hd_context", ROOT / "engine" / "harness_diagnostics.py")
    hd = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(hd)
    assert set(CONTEXT) <= set(hd.BUNDLE_SOURCES)


@pytest.mark.parametrize("module", ["context_policy", "context_engine"])
def test_complete_family_planned_with_exact_bytes(dm, dirs, module):
    src, dst = dirs
    (src / "delegate.py").write_text(
        f"HARNESS_VERSION = 6\nX = capability_module({module!r})\n", encoding="utf-8")
    _add(src, CONTEXT)
    by_name = {p.name: p for p in dm._plan(src / "delegate.py", dst / "delegate.py")}
    for name in CONTEXT:
        assert by_name[name].payload == (src / name).read_bytes()
        assert by_name[name].destination == dst / name


@pytest.mark.parametrize("missing", CONTEXT)
@pytest.mark.parametrize("apply", [True, False])
def test_missing_member_fails_before_gate_backup_or_write(dm, dirs, tmp_path, missing, apply):
    src, dst = dirs
    _add(src, tuple(n for n in CONTEXT if n != missing))
    marker = tmp_path / "gate-ran"
    before = _snap(dst)
    with pytest.raises(dm.DeploymentError, match=missing):
        dm.deploy(src / "delegate.py", dst / "delegate.py", apply=apply,
                  test_command=[sys.executable, "-c", f"open({str(marker)!r}, 'w')"])
    assert not marker.exists()
    assert _snap(dst) == before
    assert not list(dst.glob("*.backup-*"))


def test_out_of_tree_source_cannot_borrow_repo_family(dm, dirs):
    src, dst = dirs  # tmp source; the repo's engine/ copies must not be used
    with pytest.raises(dm.DeploymentError, match="context"):
        dm._plan(src / "delegate.py", dst / "delegate.py")


@pytest.mark.parametrize("extra", [(), CONTEXT[:3], CONTEXT])
def test_old_source_without_dependency_ignores_context_files(dm, dirs, extra):
    src, dst = dirs
    (src / "delegate.py").write_text("HARNESS_VERSION = 6\n", encoding="utf-8")
    _add(src, extra)
    names = [p.name for p in dm._plan(src / "delegate.py", dst / "delegate.py")]
    assert not set(names) & set(CONTEXT)


def test_context_family_requires_complete_recovery_family(dm, dirs):
    src, dst = dirs
    _add(src, CONTEXT)
    (src / dm._RECOVERY_NAMES[0]).unlink()
    with pytest.raises(dm.DeploymentError, match="recovery"):
        dm._plan(src / "delegate.py", dst / "delegate.py")


def test_apply_installs_family_and_backs_up_old_bytes(dm, dirs):
    src, dst = dirs
    _add(src, CONTEXT)
    _add(dst, CONTEXT[:2], tag="old")
    result = _deploy(dm, src, dst)
    for name in CONTEXT:
        assert (dst / name).read_bytes() == (src / name).read_bytes()
    backed = {Path(p).name.split(".backup-")[0] for p in result.backup_paths}
    assert set(CONTEXT[:2]) <= backed


def test_failure_after_copy_restores_old_selected_files(dm, dirs, monkeypatch):
    src, dst = dirs
    _add(src, CONTEXT)
    _add(dst, CONTEXT[:3], tag="old")
    before = _snap(dst)
    real = dm.verify_compiled

    def tamper(path, payload):
        if Path(path).name == "context_engine.py":
            raise dm.DeploymentError("synthetic tamper detected")
        real(path, payload)

    monkeypatch.setattr(dm, "verify_compiled", tamper)
    with pytest.raises(dm.DeploymentError, match="synthetic tamper"):
        _deploy(dm, src, dst)
    assert _snap(dst) == before
    for name in CONTEXT[3:]:
        assert not (dst / name).exists()


def test_uncompilable_context_member_rolls_back(dm, dirs):
    src, dst = dirs
    _add(src, CONTEXT)
    (src / "context_runtime.py").write_text("def broken(:\n", encoding="utf-8")
    before = _snap(dst)
    with pytest.raises(dm.DeploymentError):
        _deploy(dm, src, dst)
    assert _snap(dst) == before


def test_gate_discovers_all_context_tests(dm, tmp_path, monkeypatch):
    tests = tmp_path / "tests"
    tests.mkdir()
    wanted = ["test_context_policy.py", "test_context_guard_model.py",
              "test_context_engine.py", "test_context_integration.py"]
    for name in wanted + ["test_unrelated.py"]:
        (tests / name).write_text("", encoding="utf-8")
    monkeypatch.setattr(dm, "_REPO_ROOT", tmp_path)
    legacy = dm.gate_command(False)
    command = dm.gate_command(False, include_context=True)
    assert command[:len(legacy)] == legacy
    assert {Path(t).name for t in command[len(legacy):]} == set(wanted)

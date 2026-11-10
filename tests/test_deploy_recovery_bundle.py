from __future__ import annotations

import hashlib
import importlib.util
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "deploy_engine.py"
RECOVERY = (
    "candidate.py", "native_batch.py", "paged_tools.py", "recovery_handoff.py",
    "recovery_runtime.py", "supervisor_continuation_v3.py", "supervisor_contract_v3.py",
    "supervisor_harness_v3.py", "supervisor_launch_v2.py", "supervisor_manifest_v2.py",
    "task_status.py", "tool_output.py",
)
PROFILE = "execution_profile.py"
COMMAND_OUTPUT = "command_output.py"
PASS = ["python", "-c", "print('ok')"]


@pytest.fixture(scope="module")
def dm():
    spec = importlib.util.spec_from_file_location("deploy_engine_recovery", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _deps(dm):
    return (dm._HELPER_NAMES + dm._BUNDLE_NAMES + dm._SUPERVISION_NAMES
            + dm._GOAL_SUPERVISOR_NAMES)


@pytest.fixture
def dirs(dm, tmp_path):
    src, dst = tmp_path / "engine", tmp_path / "bin"
    src.mkdir()
    dst.mkdir()
    (src / "delegate.py").write_text("HARNESS_VERSION = 6\n", encoding="utf-8")
    (dst / "delegate.py").write_text("HARNESS_VERSION = 5\n", encoding="utf-8")
    for name in _deps(dm):
        (src / name).write_text(f"NAME = {name!r}\n", encoding="utf-8")
    return src, dst


def _add(src, names, tag="new"):
    for name in names:
        (src / name).write_text(f"TAG = {tag!r}  # {name}\n", encoding="utf-8")


def _names(dm, src, dst):
    return [p.name for p in dm._plan(src / "delegate.py", dst / "delegate.py")]
def test_family_constants_and_rollback_names(dm):
    assert dm._RECOVERY_NAMES == RECOVERY
    assert dm._RECOVERY_OPTIONAL_NAMES == (PROFILE, COMMAND_OUTPUT)
    assert set(RECOVERY + (PROFILE, COMMAND_OUTPUT)) <= set(dm._ALL_NAMES)
    assert dm._EXPECTED_HARNESS == 6


def test_legacy_plan_unchanged_without_recovery(dm, dirs):
    src, dst = dirs
    names = _names(dm, src, dst)
    assert not set(names) & set(RECOVERY + (PROFILE,))
    assert dm.gate_command(True, False, False, True, True) == dm.gate_command(
        True, False, False, True, True, include_recovery=False)


def test_complete_family_planned_with_all_bytes(dm, dirs):
    src, dst = dirs
    _add(src, RECOVERY)
    plan = dm._plan(src / "delegate.py", dst / "delegate.py")
    by_name = {p.name: p for p in plan}
    for name in RECOVERY:
        assert hashlib.sha256(by_name[name].payload).digest() == hashlib.sha256(
            (src / name).read_bytes()).digest()
        assert by_name[name].destination == dst / name
    assert PROFILE not in by_name


def test_optional_profile_deploys_only_with_complete_family(dm, dirs):
    src, dst = dirs
    _add(src, RECOVERY + (PROFILE,))
    assert PROFILE in _names(dm, src, dst)


def test_command_output_source_dependency_planned_from_selected_tree(dm, dirs):
    src, dst = dirs
    _add(src, RECOVERY + (COMMAND_OUTPUT,))
    (src / "delegate.py").write_text(
        "HARNESS_VERSION = 6\ndef capture():\n    return capability_module('command_output')\n",
        encoding="utf-8")
    plan = dm._plan(src / "delegate.py", dst / "delegate.py")
    item = next(p for p in plan if p.name == COMMAND_OUTPUT)
    assert item.payload == (src / COMMAND_OUTPUT).read_bytes()
    assert item.destination == dst / COMMAND_OUTPUT


def test_command_output_missing_fails_before_backup_or_write(dm, dirs):
    src, dst = dirs
    _add(src, RECOVERY)
    (src / "delegate.py").write_text(
        "HARNESS_VERSION = 6\ndef capture():\n    return capability_module('command_output')\n",
        encoding="utf-8")
    before = _snap(dst)
    with pytest.raises(dm.DeploymentError, match="command_output.py"):
        dm.deploy(src / "delegate.py", dst / "delegate.py", apply=True, test_command=PASS)
    assert _snap(dst) == before
    assert not list(dst.glob("*.backup-*"))


def test_old_recovery_source_does_not_require_new_command_module(dm, dirs):
    src, dst = dirs
    _add(src, RECOVERY)
    assert COMMAND_OUTPUT not in _names(dm, src, dst)


@pytest.mark.parametrize("extra", [(), (PROFILE,)])
def test_partial_family_rejected(dm, dirs, extra):
    src, dst = dirs
    _add(src, RECOVERY[:-1] + extra)
    with pytest.raises(dm.DeploymentError, match="tool_output.py"):
        _names(dm, src, dst)


def test_profile_alone_rejected(dm, dirs):
    src, dst = dirs
    _add(src, (PROFILE,))
    with pytest.raises(dm.DeploymentError, match="recovery"):
        _names(dm, src, dst)


@pytest.mark.parametrize("dep_attr", ["_BUNDLE_NAMES", "_SUPERVISION_NAMES",
                                      "_GOAL_SUPERVISOR_NAMES"])
def test_family_requires_dependencies(dm, dirs, dep_attr):
    src, dst = dirs
    _add(src, RECOVERY)
    for name in getattr(dm, dep_attr):
        (src / name).unlink()
    with pytest.raises(dm.DeploymentError, match="recovery"):
        _names(dm, src, dst)
def _snap(directory):
    return {p.name: p.read_bytes() for p in directory.iterdir() if ".backup-" not in p.name}


def test_apply_installs_and_backs_up_family(dm, dirs):
    src, dst = dirs
    _add(src, RECOVERY + (PROFILE,))
    _add(dst, RECOVERY[:3], tag="old")
    result = dm.deploy(src / "delegate.py", dst / "delegate.py", apply=True, test_command=PASS)
    for name in RECOVERY + (PROFILE,):
        assert (dst / name).read_bytes() == (src / name).read_bytes()
    backed = {Path(p).name.split(".backup-")[0] for p in result.backup_paths}
    assert set(RECOVERY[:3]) <= backed


def test_failure_after_new_copy_rolls_back_everything(dm, dirs, monkeypatch):
    src, dst = dirs
    _add(src, RECOVERY + (PROFILE,))
    _add(dst, RECOVERY[:3] + _deps(dm)[:2], tag="old")
    before = _snap(dst)
    real = dm.verify_compiled

    def fail_last(path, payload):
        if Path(path).name == PROFILE:
            raise dm.DeploymentError("synthetic profile failure")
        real(path, payload)

    monkeypatch.setattr(dm, "verify_compiled", fail_last)
    with pytest.raises(dm.DeploymentError, match="synthetic profile"):
        dm.deploy(src / "delegate.py", dst / "delegate.py", apply=True, test_command=PASS)
    assert _snap(dst) == before
    for name in RECOVERY[3:]:
        assert not (dst / name).exists()


def test_uncompilable_family_member_rolls_back(dm, dirs):
    src, dst = dirs
    _add(src, RECOVERY)
    (src / "task_status.py").write_text("def broken(:\n", encoding="utf-8")
    before = _snap(dst)
    with pytest.raises(dm.DeploymentError):
        dm.deploy(src / "delegate.py", dst / "delegate.py", apply=True, test_command=PASS)
    assert _snap(dst) == before


def test_gate_selects_existing_recovery_tests(dm, tmp_path, monkeypatch):
    tests = tmp_path / "tests"
    tests.mkdir()
    wanted = ["test_candidate_a.py", "test_recovery_b.py", "test_resilient_c.py",
              "test_native_batch_core.py", "test_tool_output.py", "test_task_status.py",
              "test_supervisor_contract_v3.py", "test_supervisor_launch_v2.py",
              "test_execution_profile.py", "test_command_output_main.py"]
    for name in wanted + ["test_unrelated.py"]:
        (tests / name).write_text("", encoding="utf-8")
    monkeypatch.setattr(dm, "_REPO_ROOT", tmp_path)
    legacy = dm.gate_command(False)
    command = dm.gate_command(False, include_recovery=True)
    assert command[:len(legacy)] == legacy
    added = {Path(t).name for t in command[len(legacy):]}
    assert added == set(wanted)

from __future__ import annotations

import hashlib
import importlib.util
import sys
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import MagicMock

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "deploy_engine.py"


def _load():
    spec = importlib.util.spec_from_file_location("deploy_engine", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def deploy_module():
    return _load()


def _write_engine(path: Path, harness: int = 6) -> None:
    path.write_text(f"HARNESS_VERSION = {harness}\n", encoding="utf-8")


@dataclass
class DeploymentCase:
    module: object
    source: Path
    destination: Path
    passing_tests: list

    def apply(self):
        return self.module.deploy(
            self.source,
            self.destination,
            apply=True,
            test_command=self.passing_tests,
        )


HELPERS = ("ask-model.py", "fanout.py")
ALL_FILES = ("delegate.py",) + HELPERS


def _write_helpers(directory: Path, tag: str) -> None:
    for name in HELPERS:
        (directory / name).write_text(f"VERSION_TAG = {tag!r}\n", encoding="utf-8")


def _snapshot(directory: Path) -> dict:
    return {
        name: (directory / name).read_bytes()
        for name in ALL_FILES
        if (directory / name).exists()
    }


@pytest.fixture
def case(deploy_module, tmp_path: Path) -> DeploymentCase:
    source = tmp_path / "engine" / "delegate.py"
    source.parent.mkdir(parents=True)
    _write_engine(source)
    _write_helpers(source.parent, "new")
    destination = tmp_path / "bin" / "delegate.py"
    destination.parent.mkdir(parents=True)
    _write_engine(destination, harness=5)
    _write_helpers(destination.parent, "old")
    return DeploymentCase(
        module=deploy_module,
        source=source,
        destination=destination,
        passing_tests=["python", "-c", "print('ok')"],
    )


def test_dry_run_does_not_modify_destination(deploy_module, case: DeploymentCase) -> None:
    original = case.destination.read_bytes()
    result = deploy_module.deploy(
        case.source, case.destination, apply=False, test_command=case.passing_tests
    )
    assert result.applied is False
    assert case.destination.read_bytes() == original


def test_failed_contract_gate_does_not_backup_or_install(
    deploy_module, case: DeploymentCase
) -> None:
    original = case.destination.read_bytes()
    with pytest.raises(deploy_module.DeploymentError):
        deploy_module.deploy(
            case.source,
            case.destination,
            apply=True,
            test_command=["python", "-c", "import sys; sys.exit(1)"],
        )
    assert case.destination.read_bytes() == original
    backups = list(case.destination.parent.glob("delegate.py.backup-*"))
    assert backups == []


def test_apply_backs_up_and_installs(deploy_module, case: DeploymentCase) -> None:
    result = case.apply()
    assert result.applied is True
    assert case.destination.read_bytes() == case.source.read_bytes()
    backup = Path(result.backup_path)
    assert backup.is_file()
    assert "HARNESS_VERSION = 5" in backup.read_text(encoding="utf-8")


def test_optional_watcher_is_installed_and_backed_up(case: DeploymentCase):
    source = case.source.parent / "agent_await.py"
    destination = case.destination.parent / "agent_await.py"
    source.write_text('WATCHER = "synthetic-new"\n', encoding="utf-8")
    destination.write_text('WATCHER = "synthetic-old"\n', encoding="utf-8")
    result = case.apply()
    assert destination.read_bytes() == source.read_bytes()
    backups = [Path(path) for path in result.backup_paths if Path(path).name.startswith("agent_await.py.")]
    assert len(backups) == 1
    assert b"synthetic-old" in backups[0].read_bytes()


@pytest.mark.parametrize("existing", [True, False])
def test_optional_watcher_rolls_back_with_engine(case: DeploymentCase, monkeypatch, existing):
    source = case.source.parent / "agent_await.py"
    destination = case.destination.parent / "agent_await.py"
    source.write_text('WATCHER = "synthetic-new"\n', encoding="utf-8")
    original = b'WATCHER = "synthetic-old"\n'
    if existing:
        destination.write_bytes(original)
    before = _snapshot(case.destination.parent)
    real_verify = case.module.verify_compiled

    def fail_watcher(path, payload):
        if Path(path).name == "agent_await.py":
            raise case.module.DeploymentError("synthetic watcher verification failure")
        real_verify(path, payload)

    monkeypatch.setattr(case.module, "verify_compiled", fail_watcher)
    with pytest.raises(case.module.DeploymentError, match="synthetic watcher"):
        case.apply()
    assert _snapshot(case.destination.parent) == before
    if existing:
        assert destination.read_bytes() == original
    else:
        assert not destination.exists()


def test_watcher_gate_is_optional_and_preserves_legacy_prefix(deploy_module, tmp_path, monkeypatch):
    tests = tmp_path / "tests"
    tests.mkdir()
    for name in ("test_engine_launch_receipts.py", "test_agent_await_launch.py"):
        (tests / name).write_text("# synthetic gate fixture\n", encoding="utf-8")
    monkeypatch.setattr(deploy_module, "_REPO_ROOT", tmp_path)
    legacy = deploy_module.gate_command(False)
    with_watcher = deploy_module.gate_command(False, include_watcher=True)
    assert with_watcher[:len(legacy)] == legacy
    assert "tests/test_engine_launch_receipts.py" in with_watcher
    assert "tests/test_agent_await_launch.py" in with_watcher
    assert "tests/test_agent_await_launch.py" not in legacy


def test_post_replace_verification_failure_restores_original(
    deploy_module, case: DeploymentCase, monkeypatch
) -> None:
    original = case.destination.read_bytes()
    monkeypatch.setattr(
        deploy_module,
        "verify_installation",
        MagicMock(side_effect=deploy_module.DeploymentError("bad checksum")),
    )
    with pytest.raises(deploy_module.DeploymentError):
        case.apply()
    assert case.destination.read_bytes() == original


def test_rollback_restores_backup(deploy_module, case: DeploymentCase) -> None:
    result = case.apply()
    backup = Path(result.backup_path)
    rollback = deploy_module.rollback(case.destination, backup)
    assert rollback.applied is True
    assert "HARNESS_VERSION = 5" in case.destination.read_text(encoding="utf-8")


def test_dry_run_writes_nothing_for_any_file(deploy_module, case: DeploymentCase) -> None:
    before = sorted(p.name for p in case.destination.parent.iterdir())
    originals = _snapshot(case.destination.parent)
    result = deploy_module.deploy(
        case.source, case.destination, apply=False, test_command=case.passing_tests
    )
    assert result.applied is False
    assert result.backup_paths == ()
    assert sorted(p.name for p in case.destination.parent.iterdir()) == before
    assert _snapshot(case.destination.parent) == originals


def test_dry_run_is_the_cli_default(deploy_module, case: DeploymentCase, monkeypatch) -> None:
    gate = MagicMock()
    monkeypatch.setattr(deploy_module, "run_contract_tests", gate)
    originals = _snapshot(case.destination.parent)
    assert deploy_module.main(
        ["--source", str(case.source), "--destination", str(case.destination)]
    ) == 0
    gate.assert_not_called()
    assert _snapshot(case.destination.parent) == originals
    assert not list(case.destination.parent.glob("*.backup-*"))


def test_apply_deploys_and_backs_up_all_three(deploy_module, case: DeploymentCase) -> None:
    originals = _snapshot(case.destination.parent)
    result = case.apply()
    assert result.applied is True
    dest_dir = case.destination.parent
    for name in ALL_FILES:
        assert (dest_dir / name).read_bytes() == (case.source.parent / name).read_bytes()
        backups = list(dest_dir.glob(f"{name}.backup-*"))
        assert len(backups) == 1, name
        assert backups[0].read_bytes() == originals[name]
        digest = hashlib.sha256(originals[name]).hexdigest()[:12]
        assert backups[0].name.startswith(f"{name}.backup-")
        assert backups[0].name.endswith(f"-{digest}")
    assert sorted(Path(p).name.split(".backup-")[0] for p in result.backup_paths) == sorted(
        ALL_FILES
    )
    assert result.backup_path == str(next(dest_dir.glob("delegate.py.backup-*")))


def test_apply_installs_helpers_missing_from_destination(
    deploy_module, case: DeploymentCase
) -> None:
    for name in HELPERS:
        (case.destination.parent / name).unlink()
    result = case.apply()
    for name in HELPERS:
        assert (case.destination.parent / name).read_text(encoding="utf-8") == (
            "VERSION_TAG = 'new'\n"
        )
    assert len(result.backup_paths) == 1


def test_one_failed_verification_restores_all_three(
    deploy_module, case: DeploymentCase, monkeypatch
) -> None:
    originals = _snapshot(case.destination.parent)
    real = deploy_module.verify_compiled

    def fail_for_fanout(destination, payload):
        if Path(destination).name == "fanout.py":
            raise deploy_module.DeploymentError("fanout.py did not compile")
        return real(destination, payload)

    monkeypatch.setattr(deploy_module, "verify_compiled", fail_for_fanout)
    with pytest.raises(deploy_module.DeploymentError):
        case.apply()
    assert _snapshot(case.destination.parent) == originals


def test_syntax_error_in_helper_restores_all_three(
    deploy_module, case: DeploymentCase
) -> None:
    (case.source.parent / "ask-model.py").write_text("def broken(:\n", encoding="utf-8")
    originals = _snapshot(case.destination.parent)
    with pytest.raises(deploy_module.DeploymentError, match="ask-model.py"):
        case.apply()
    assert _snapshot(case.destination.parent) == originals
    assert not list(case.destination.parent.glob("__pycache__"))


def test_failure_removes_helpers_that_did_not_exist_before(
    deploy_module, case: DeploymentCase, monkeypatch
) -> None:
    (case.destination.parent / "fanout.py").unlink()
    originals = _snapshot(case.destination.parent)
    monkeypatch.setattr(
        deploy_module,
        "verify_installation",
        MagicMock(side_effect=deploy_module.DeploymentError("bad harness")),
    )
    with pytest.raises(deploy_module.DeploymentError):
        case.apply()
    assert _snapshot(case.destination.parent) == originals
    assert not (case.destination.parent / "fanout.py").exists()


def test_missing_source_helper_fails_before_any_change(
    deploy_module, case: DeploymentCase
) -> None:
    (case.source.parent / "fanout.py").unlink()
    originals = _snapshot(case.destination.parent)
    with pytest.raises(deploy_module.DeploymentError, match="fanout.py"):
        case.apply()
    assert _snapshot(case.destination.parent) == originals
    assert not list(case.destination.parent.glob("*.backup-*"))


def test_rollback_restores_a_helper_backup(deploy_module, case: DeploymentCase) -> None:
    result = case.apply()
    backup = next(Path(p) for p in result.backup_paths if "fanout.py.backup-" in p)
    deploy_module.rollback(case.destination, backup)
    assert (case.destination.parent / "fanout.py").read_text(encoding="utf-8") == (
        "VERSION_TAG = 'old'\n"
    )
    assert "HARNESS_VERSION = 6" in case.destination.read_text(encoding="utf-8")


def test_rollback_rejects_non_backup_file(deploy_module, case: DeploymentCase, tmp_path: Path) -> None:
    bogus = tmp_path / "not-a-backup.txt"
    bogus.write_text("x", encoding="utf-8")
    with pytest.raises(deploy_module.DeploymentError):
        deploy_module.rollback(case.destination, bogus)


# ------------------------------------------------- capability bundle files --

BUNDLE = ("harness_mcp.py", "harness_skills.py", "harness_setup.py",
          "init-agent-harness.py")
EVERYTHING = ALL_FILES + BUNDLE


def _write_bundle(directory: Path, tag: str, names=BUNDLE) -> None:
    for name in names:
        (directory / name).write_text(f"BUNDLE_TAG = {tag!r}\n", encoding="utf-8")


def _snap_all(directory: Path) -> dict:
    return {n: (directory / n).read_bytes() for n in EVERYTHING
            if (directory / n).exists()}


def test_legacy_three_file_source_does_not_touch_bundle(deploy_module, case):
    _write_bundle(case.destination.parent, "old")
    result = case.apply()
    assert sorted(Path(p).name.split(".backup-")[0] for p in result.backup_paths) \
        == sorted(ALL_FILES)
    for name in BUNDLE:
        assert (case.destination.parent / name).read_text() == "BUNDLE_TAG = 'old'\n"


def test_bundle_deployed_backed_up_and_new_files_created(deploy_module, case):
    _write_bundle(case.source.parent, "new")
    _write_bundle(case.destination.parent, "old", names=BUNDLE[:1])
    result = case.apply()
    dest = case.destination.parent
    for name in EVERYTHING:
        assert (dest / name).read_bytes() == (case.source.parent / name).read_bytes()
    backed = sorted(Path(p).name.split(".backup-")[0] for p in result.backup_paths)
    assert backed == sorted(ALL_FILES + BUNDLE[:1])
    for name in BUNDLE:
        assert name in result.message
    assert not list(dest.glob("__pycache__"))


@pytest.mark.parametrize("missing", BUNDLE)
def test_partial_bundle_refused_before_any_change(deploy_module, case, missing):
    _write_bundle(case.source.parent, "new",
                  names=tuple(n for n in BUNDLE if n != missing))
    originals = _snap_all(case.destination.parent)
    with pytest.raises(deploy_module.DeploymentError, match=missing):
        case.apply()
    assert _snap_all(case.destination.parent) == originals
    assert not list(case.destination.parent.glob("*.backup-*"))


def test_bundle_failure_restores_all_and_deletes_new_files(
    deploy_module, case, monkeypatch
) -> None:
    _write_bundle(case.source.parent, "new")
    _write_bundle(case.destination.parent, "old", names=("harness_mcp.py",))
    originals = _snap_all(case.destination.parent)
    real = deploy_module.verify_compiled

    def fail_last(destination, payload):
        if Path(destination).name == "init-agent-harness.py":
            raise deploy_module.DeploymentError("init-agent-harness.py bad")
        return real(destination, payload)

    monkeypatch.setattr(deploy_module, "verify_compiled", fail_last)
    with pytest.raises(deploy_module.DeploymentError):
        case.apply()
    assert _snap_all(case.destination.parent) == originals
    for name in ("harness_skills.py", "harness_setup.py", "init-agent-harness.py"):
        assert not (case.destination.parent / name).exists()


def test_bundle_syntax_error_restores_all(deploy_module, case) -> None:
    _write_bundle(case.source.parent, "new")
    (case.source.parent / "harness_setup.py").write_text("def x(:\n")
    originals = _snap_all(case.destination.parent)
    with pytest.raises(deploy_module.DeploymentError, match="harness_setup.py"):
        case.apply()
    assert _snap_all(case.destination.parent) == originals


def test_compile_check_does_not_execute(deploy_module, tmp_path) -> None:
    marker = tmp_path / "ran"
    script = tmp_path / "harness_mcp.py"
    payload = f"open({str(marker)!r}, 'w').close()\n".encode()
    script.write_bytes(payload)
    deploy_module.verify_compiled(script, payload)
    assert not marker.exists()


def test_bundle_dry_run_lists_files_and_writes_nothing(deploy_module, case) -> None:
    _write_bundle(case.source.parent, "new")
    before = sorted(p.name for p in case.destination.parent.iterdir())
    result = deploy_module.deploy(case.source, case.destination, apply=False,
                                  test_command=case.passing_tests)
    assert "harness_setup.py" in result.message
    assert sorted(p.name for p in case.destination.parent.iterdir()) == before


def test_rollback_restores_a_bundle_backup(deploy_module, case) -> None:
    _write_bundle(case.source.parent, "new")
    _write_bundle(case.destination.parent, "old")
    result = case.apply()
    backup = next(Path(p) for p in result.backup_paths
                  if "harness_mcp.py.backup-" in p)
    deploy_module.rollback(case.destination, backup)
    assert (case.destination.parent / "harness_mcp.py").read_text() == \
        "BUNDLE_TAG = 'old'\n"


def test_gate_includes_capability_tests_only_with_bundle(deploy_module, case):
    legacy = deploy_module.gate_command(include_bundle=False)
    assert legacy == deploy_module._DEFAULT_TEST
    full = deploy_module.gate_command(include_bundle=True)
    assert full[: len(legacy)] == legacy
    for name in deploy_module._BUNDLE_TESTS:
        if (deploy_module._REPO_ROOT / name).is_file():
            assert name in full
    assert "tests/test_engine_harness_setup.py" in full


def test_main_apply_uses_bundle_gate(deploy_module, case, monkeypatch) -> None:
    _write_bundle(case.source.parent, "new")
    seen = []
    monkeypatch.setattr(deploy_module, "run_contract_tests", seen.append)
    assert deploy_module.main(["--source", str(case.source), "--destination",
                               str(case.destination), "--apply"]) == 0
    assert seen == [deploy_module.gate_command(include_bundle=True)]


def test_deploy_gate_checks_turn_budget_before_install(deploy_module, case, monkeypatch):
    originals = _snapshot(case.destination.parent)
    checked = []

    def reject_budget_regression(command):
        checked.extend(command)
        assert "tests/test_engine_turn_budget.py" in command
        raise deploy_module.DeploymentError("synthetic budget regression")

    monkeypatch.setattr(deploy_module, "run_contract_tests", reject_budget_regression)
    with pytest.raises(deploy_module.DeploymentError, match="budget regression"):
        deploy_module.main(["--source", str(case.source), "--destination",
                            str(case.destination), "--apply"])
    assert checked
    assert _snapshot(case.destination.parent) == originals
    assert not list(case.destination.parent.glob("*.backup-*"))


def test_diagnostic_helper_deployed_and_gated(deploy_module, case):
    source = case.source.parent / "harness_diagnostics.py"
    source.write_text("SCHEMA = 1\n", encoding="utf-8")
    result = deploy_module.deploy(case.source, case.destination, apply=True, test_command=case.passing_tests)
    assert result.applied
    assert (case.destination.parent / source.name).read_bytes() == source.read_bytes()
    gate = deploy_module.gate_command(False, include_diagnostics=True)
    assert "tests/test_lifecycle_diagnostics.py" in gate
    assert "tests/test_engine_diagnostics.py" in gate


def test_canonical_process_identity_module_is_deployed_without_duplicate_source(deploy_module, case):
    source = case.source.parent.parent / "src/harness_process_identity.py"
    source.parent.mkdir(exist_ok=True)
    source.write_text("SYNTHETIC_IDENTITY = True\n", encoding="utf-8")
    result = deploy_module.deploy(case.source, case.destination, apply=True, test_command=case.passing_tests)
    assert result.applied
    assert (case.destination.parent / source.name).read_bytes() == source.read_bytes()


def test_registered_owner_fingerprint_is_copied_as_metadata_not_python(deploy_module, case):
    import json
    source = case.source.parent.parent / "tools/kilo/owner-patchkit/manifest.json"
    source.parent.mkdir(parents=True)
    source.write_text(json.dumps({"schema": 1, "patch_id": "sha256:" + "a" * 64}), encoding="utf-8")
    result = deploy_module.deploy(case.source, case.destination, apply=True, test_command=case.passing_tests)
    assert result.applied
    assert (case.destination.parent / "harness_owner_patch.json").read_bytes() == source.read_bytes()


# ------------------------------------------------ supervision bundle files --

SUPERVISION = ("finish_gate.py", "run_review.py", "trajectory.py", "context_guard.py")


def test_supervision_bundle_deployed_backed_up_and_rolled_back(deploy_module, case):
    _write_bundle(case.source.parent, "new", names=SUPERVISION)
    _write_bundle(case.destination.parent, "old", names=SUPERVISION[:1])
    destination = case.destination.parent
    original = (destination / "finish_gate.py").read_bytes()
    result = case.apply()
    assert result.applied is True
    for name in ALL_FILES + SUPERVISION:
        assert (destination / name).read_bytes() == (case.source.parent / name).read_bytes()
    backed = sorted(Path(p).name.split(".backup-")[0] for p in result.backup_paths)
    assert backed == sorted(ALL_FILES + SUPERVISION[:1])
    backup = next(Path(p) for p in result.backup_paths
                  if Path(p).name.startswith("finish_gate.py.backup-"))
    assert backup.read_bytes() == original
    assert deploy_module.rollback(case.destination, backup).applied is True
    assert (destination / "finish_gate.py").read_bytes() == original
    assert case.destination.read_bytes() == case.source.read_bytes()


@pytest.mark.parametrize("missing", SUPERVISION)
def test_partial_supervision_bundle_refused_before_any_change(deploy_module, case, missing):
    _write_bundle(case.source.parent, "new",
                  names=tuple(n for n in SUPERVISION if n != missing))
    originals = {p.name: p.read_bytes() for p in case.destination.parent.iterdir()}
    with pytest.raises(deploy_module.DeploymentError, match=missing):
        case.apply()
    assert {p.name: p.read_bytes() for p in case.destination.parent.iterdir()} == originals
    assert not list(case.destination.parent.glob("*.backup-*"))


@pytest.mark.parametrize("broken", SUPERVISION)
def test_supervision_syntax_error_restores_all_and_deletes_new_files(
    deploy_module, case, broken
):
    _write_bundle(case.source.parent, "new", names=SUPERVISION)
    _write_bundle(case.destination.parent, "old", names=SUPERVISION[:1])
    (case.source.parent / broken).write_text("def broken(:\n", encoding="utf-8")
    originals = _snapshot(case.destination.parent)
    with pytest.raises(deploy_module.DeploymentError, match=broken):
        case.apply()
    assert _snapshot(case.destination.parent) == originals
    assert (case.destination.parent / "finish_gate.py").read_text(encoding="utf-8") == \
        "BUNDLE_TAG = 'old'\n"
    for name in SUPERVISION[1:]:
        assert not (case.destination.parent / name).exists()
    assert not list(case.destination.parent.glob("__pycache__"))


def test_supervision_gate_is_optional_and_preserves_legacy_prefix(
    deploy_module, tmp_path, monkeypatch
):
    (tmp_path / "tests").mkdir()
    existing_tests = deploy_module._SUPERVISION_TESTS[:-1]
    for name in existing_tests:
        (tmp_path / name).write_text("# synthetic gate fixture\n", encoding="utf-8")
    monkeypatch.setattr(deploy_module, "_REPO_ROOT", tmp_path)
    legacy = deploy_module.gate_command(False)
    assert legacy == deploy_module._DEFAULT_TEST
    with_supervision = deploy_module.gate_command(False, include_supervision=True)
    assert with_supervision == legacy + list(existing_tests)


def test_supervision_gate_includes_command_and_progress_regressions(deploy_module, tmp_path, monkeypatch):
    names = ("tests/test_delegate_command_snapshot.py", "tests/test_delegate_progress.py")
    (tmp_path / "tests").mkdir()
    for name in names:
        (tmp_path / name).write_text("# synthetic gate fixture\n", encoding="utf-8")
        assert name in deploy_module._SUPERVISION_TESTS
    monkeypatch.setattr(deploy_module, "_REPO_ROOT", tmp_path)
    command = deploy_module.gate_command(False, include_supervision=True)
    assert command == deploy_module._DEFAULT_TEST + list(names)


def test_main_apply_with_supervision_bundle_uses_supervision_gate(
    deploy_module, case, monkeypatch
):
    _write_bundle(case.source.parent, "new", names=SUPERVISION)
    gate = MagicMock(return_value=case.passing_tests)
    contract_tests = MagicMock()
    monkeypatch.setattr(deploy_module, "gate_command", gate)
    monkeypatch.setattr(deploy_module, "run_contract_tests", contract_tests)
    assert deploy_module.main(["--source", str(case.source), "--destination",
                               str(case.destination), "--apply"]) == 0
    gate.assert_called_once_with(False, include_watcher=False,
                                 include_diagnostics=False, include_supervision=True)
    contract_tests.assert_called_once_with(case.passing_tests)


GOAL_SUPERVISOR = (
    "supervisor_contract.py", "supervisor_journal.py", "supervisor_state.py",
    "supervisor_evidence.py", "supervisor_process.py", "supervisor_policy.py",
    "supervisor_harness.py", "supervise.py",
)


def test_goal_supervisor_is_atomic_optional_bundle(deploy_module, case):
    _write_bundle(case.source.parent, "new", names=GOAL_SUPERVISOR)
    (case.source.parent / "supervisor_validator.py").write_text("TAG = 'new'\n")
    names = [item.name for item in deploy_module._plan(case.source, case.destination)]
    assert set(GOAL_SUPERVISOR) | {"supervisor_validator.py"} <= set(names)
    before = _snap_all(case.destination.parent)
    result = deploy_module.deploy(case.source, case.destination, apply=False,
                                  test_command=case.passing_tests)
    assert result.applied is False and _snap_all(case.destination.parent) == before
    result = case.apply()
    assert result.applied
    for name in GOAL_SUPERVISOR + ("supervisor_validator.py",):
        assert (case.destination.parent / name).read_bytes() == (case.source.parent / name).read_bytes()


@pytest.mark.parametrize("missing", GOAL_SUPERVISOR)
def test_partial_goal_bundle_refused_without_changes(deploy_module, case, missing):
    _write_bundle(case.source.parent, "new", names=tuple(n for n in GOAL_SUPERVISOR if n != missing))
    before = _snap_all(case.destination.parent)
    with pytest.raises(deploy_module.DeploymentError, match="incomplete goal supervisor"):
        case.apply()
    assert _snap_all(case.destination.parent) == before


def test_goal_supervisor_failure_rolls_back_all_files(deploy_module, case):
    _write_bundle(case.source.parent, "new", names=GOAL_SUPERVISOR)
    (case.source.parent / "supervise.py").write_text("def broken(:\n")
    before = _snap_all(case.destination.parent)
    with pytest.raises(deploy_module.DeploymentError):
        case.apply()
    assert _snapshot(case.destination.parent) == {k: v for k, v in before.items() if k in ALL_FILES}
    assert not any((case.destination.parent / n).exists() for n in GOAL_SUPERVISOR)


def test_goal_supervisor_gate_preserves_legacy_prefix(deploy_module, tmp_path, monkeypatch):
    (tmp_path / "tests").mkdir()
    selected = ("tests/test_supervisor_protocol.py", "tests/test_supervisor_traces.py")
    for name in selected:
        (tmp_path / name).write_text("# synthetic test fixture\n")
    monkeypatch.setattr(deploy_module, "_REPO_ROOT", tmp_path)
    assert deploy_module.gate_command(False) == deploy_module._DEFAULT_TEST
    command = deploy_module.gate_command(False, include_goal_supervisor=True)
    assert command[:len(deploy_module._DEFAULT_TEST)] == deploy_module._DEFAULT_TEST
    assert set(command[len(deploy_module._DEFAULT_TEST):]) == set(selected)

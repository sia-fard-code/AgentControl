"""Atomic, verified, rollback-capable deployment of the shared engine.

The engine is three files deployed together into one directory: ``delegate.py``
and the two one-shot helpers that call it, ``ask-model.py`` and ``fanout.py``.
The helpers are read from the source's directory and installed next to the
destination ``delegate.py``.

Deployment is operator-gated. By default this runs a dry run and never touches
the installed engine. ``--apply`` runs the contract test gate, backs up each
current installed file (``<file>.backup-<stamp>-<digest>``), atomically replaces
it, and verifies it (``delegate.py``: checksum and harness version; the helpers:
checksum and ``py_compile``). If any file fails, all three are restored.

Optional capability bundle: if the source directory also ships
``harness_mcp.py``, ``harness_skills.py``, ``harness_setup.py`` and
``init-agent-harness.py``, all four are deployed with the engine under the same
backup / checksum / ``py_compile`` / restore-everything rules (files that did not
exist before are deleted on failure), and the gate also runs the capability
tests. A source with only some of them is refused before anything changes.

Optional supervision bundle: ``finish_gate.py``, ``run_review.py`` and
``trajectory.py`` deploy together with the same verification and rollback rules,
plus their contract tests. Partial bundles are refused; legacy sources with none
are unchanged.

An optional sibling ``agent_await.py`` is deployed independently of that bundle.
Its launch-identity tests join the gate, and it has the same rollback guarantees.
Legacy sources without the watcher retain their existing deployment set.

The optional goal-supervisor source bundle deploys atomically only when complete;
its protocol, trace and process tests join the gate. This does not activate a goal.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import ast
import py_compile
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
_DEFAULT_SOURCE = _REPO_ROOT / "engine" / "delegate.py"
_DEFAULT_DESTINATION = Path(os.path.expanduser("~")) / ".claude" / "bin" / "delegate.py"
_DEFAULT_TEST = [sys.executable, "-m", "pytest", "tests/test_engine_contract.py",
                 "tests/test_engine_turn_budget.py", "tests/test_engine_turn_budget_images.py",
                 "tests/test_turn_budget_consumers.py", "-q"]
_BACKUP_GLOB = "delegate.py.backup-*"
_HARNESS_RE = re.compile(r"^HARNESS_VERSION\s*=\s*(\d+)", re.MULTILINE)
_EXPECTED_HARNESS = 6
_ENGINE_NAME = "delegate.py"
_HELPER_NAMES = ("ask-model.py", "fanout.py")
_DEPLOYED_NAMES = (_ENGINE_NAME,) + _HELPER_NAMES
# Optional capability bundle. A source that ships ANY of these must ship ALL of
# them (the runtime pair plus the initializer and its logic), so an install can
# never mix a new capability module with a stale/missing sibling. A legacy
# source with none of them deploys exactly the three files above.
_BUNDLE_NAMES = ("harness_mcp.py", "harness_skills.py", "harness_setup.py",
                 "init-agent-harness.py")
_SUPERVISION_NAMES = ("finish_gate.py", "run_review.py", "trajectory.py", "context_guard.py")
_WATCHER_NAME = "agent_await.py"
_DIAGNOSTIC_NAME = "harness_diagnostics.py"
_IDENTITY_NAME = "harness_process_identity.py"
_OWNER_PATCH_NAME = "harness_owner_patch.json"
_GOAL_SUPERVISOR_NAMES = (
    "supervisor_contract.py", "supervisor_journal.py", "supervisor_state.py",
    "supervisor_evidence.py", "supervisor_process.py", "supervisor_policy.py",
    "supervisor_harness.py", "supervise.py",
)
_GOAL_OPTIONAL_NAMES = ("supervisor_validator.py",)
# Recovery family: all-or-nothing, and only on top of complete capability,
# supervision and goal-supervisor bundles. The profile module rides along only
# with the complete family.
_RECOVERY_NAMES = (
    "candidate.py", "native_batch.py", "paged_tools.py", "recovery_handoff.py",
    "recovery_runtime.py", "supervisor_continuation_v3.py", "supervisor_contract_v3.py",
    "supervisor_harness_v3.py", "supervisor_launch_v2.py", "supervisor_manifest_v2.py",
    "task_status.py", "tool_output.py",
)
_RECOVERY_OPTIONAL_NAMES = ("execution_profile.py", "command_output.py")
# Context family: all-or-nothing, selected only when the source statically needs
# it (presence alone never selects it), and only on top of the recovery family.
_CONTEXT_NAMES = (
    "context_policy.py", "context_tokens.py", "context_receipts.py", "context_admission.py",
    "context_runtime.py", "context_compaction.py", "context_engine.py", "context_storage.py",
)
_CONTEXT_TEST_GLOBS = ("test_context*.py",)
_ALL_NAMES = (
    _DEPLOYED_NAMES
    + _BUNDLE_NAMES
    + _SUPERVISION_NAMES
    + (_WATCHER_NAME, _DIAGNOSTIC_NAME, _IDENTITY_NAME, _OWNER_PATCH_NAME)
    + _GOAL_SUPERVISOR_NAMES + _GOAL_OPTIONAL_NAMES
    + _RECOVERY_NAMES + _RECOVERY_OPTIONAL_NAMES
    + _CONTEXT_NAMES
)
# Bounded globs inside tests/ only (no project-wide scan).
_RECOVERY_TEST_GLOBS = (
    "test_candidate*.py", "test_recovery*.py", "test_resilient*.py",
    "test_native_batch_core.py", "test_tool_output.py", "test_task_status.py",
    "test_supervisor*_v3.py", "test_supervisor*identity_v2.py", "test_supervisor*launch_v2.py",
    "test_supervisor*child*routing*v2.py", "test_supervisor*manifest_v2.py",
    "test_supervisor*real*contract*.py", "test_supervisor*boundar*.py",
    "test_supervisor*evaluation*.py", "test_supervisor*continuation*review*.py",
    "test_execution_profile*.py", "test_command_output*.py",
)
# Extra contract tests run by the gate only when the bundle is deployed; a
# listed file that does not exist in the checkout is left out.
_BUNDLE_TESTS = (
    "tests/test_engine_mcp_runtime.py",
    "tests/test_engine_skills_runtime.py",
    "tests/test_engine_capabilities.py",
    "tests/test_engine_harness_setup.py",
)
_SUPERVISION_TESTS = (
    "tests/test_finish_gate.py",
    "tests/test_run_review.py",
    "tests/test_trajectory.py",
    "tests/test_engine_context_guard.py",
    "tests/test_delegate_verification.py",
    "tests/test_delegate_command_snapshot.py",
    "tests/test_delegate_progress.py",
    "tests/test_delegate_closeout.py",
    "tests/test_delegate_trajectory.py",
)
_WATCHER_TESTS = ("tests/test_engine_launch_receipts.py", "tests/test_agent_await_launch.py",
                   "tests/test_launch_visibility_subprocess.py")
_DIAGNOSTIC_TESTS = ("tests/test_lifecycle_diagnostics.py", "tests/test_engine_diagnostics.py",
                     "tests/test_wrapper_diagnostics.py", "tests/test_ask_model_wrapper.py",
                      "tests/test_fanout_synthesis.py", "tests/test_lifecycle_subprocess.py",
                      "tests/test_engine_boundaries.py", "tests/test_engine_failure_reproduction.py",
                      "tests/test_deploy_diagnostic_boundaries.py",
                      "tests/test_process_identity.py", "tests/test_exit_observer.py", "tests/test_kilo_owner_patchkit.py")
_GOAL_SUPERVISOR_TESTS = tuple("tests/test_supervisor_" + name + ".py" for name in (
    "contract", "journal", "state", "evidence", "process", "protocol", "harness",
    "validator", "traces", "crash", "cli", "contract_v2", "journal_v2", "state_v2",
    "harness_v2", "cli_v2", "v2")) + (
        "tests/test_engine_oneshot.py", "tests/test_engine_oneshot_stream.py")
_ROLLBACK_RE = re.compile(
    r"(" + "|".join(re.escape(n) for n in _ALL_NAMES) + r")\.backup-.+"
)


class DeploymentError(Exception):
    pass


@dataclass(frozen=True)
class DeploymentResult:
    applied: bool
    source_sha256: str
    destination_sha256: str | None
    backup_path: str | None
    message: str
    backup_paths: tuple = ()


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _harness_version(text: str) -> int | None:
    match = _HARNESS_RE.search(text)
    return int(match.group(1)) if match else None


def run_contract_tests(test_command) -> None:
    result = subprocess.run(
        list(test_command), cwd=str(_REPO_ROOT), capture_output=True, text=True
    )
    if result.returncode != 0:
        raise DeploymentError(
            f"engine contract tests failed (exit {result.returncode})"
        )


def _atomic_write(destination: Path, payload: bytes) -> None:
    temporary = destination.with_name(f".{destination.name}.{os.getpid()}.tmp")
    with temporary.open("wb") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, destination)


def _backup(destination: Path) -> Path:
    payload = destination.read_bytes()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    digest = _sha256(payload)[:12]
    backup = destination.with_name(f"{destination.name}.backup-{stamp}-{digest}")
    _atomic_write(backup, payload)
    return backup


def verify_installation(destination: Path, payload: bytes) -> None:
    installed = destination.read_bytes()
    if _sha256(installed) != _sha256(payload):
        raise DeploymentError("installed checksum mismatch")
    harness = _harness_version(installed.decode("utf-8", errors="replace"))
    if harness != _EXPECTED_HARNESS:
        raise DeploymentError(
            f"installed harness version {harness!r} != {_EXPECTED_HARNESS}"
        )


def verify_compiled(destination: Path, payload: bytes) -> None:
    """Checksum plus ``py_compile`` for a helper script.

    The bytecode goes to a throwaway directory so verification never leaves a
    ``__pycache__`` beside the installed scripts.
    """
    destination = Path(destination)
    if _sha256(destination.read_bytes()) != _sha256(payload):
        raise DeploymentError(f"{destination.name}: installed checksum mismatch")
    if destination.suffix == ".json":
        try:
            value = json.loads(payload)
            if type(value.get("schema")) is not int or value["schema"] != 1 or not re.fullmatch(r"sha256:[0-9a-f]{64}", value.get("patch_id", "")):
                raise ValueError("invalid owner fingerprint registry")
        except (ValueError, TypeError, AttributeError) as error:
            raise DeploymentError("invalid owner fingerprint registry") from error
        return
    with tempfile.TemporaryDirectory() as scratch:
        try:
            py_compile.compile(
                str(destination),
                cfile=os.path.join(scratch, "check.pyc"),
                doraise=True,
            )
        except py_compile.PyCompileError as exc:
            raise DeploymentError(
                f"{destination.name}: does not compile: {exc.msg.strip()}"
            ) from exc


@dataclass
class _Planned:
    name: str
    destination: Path
    payload: bytes
    backup: Path | None = None
    existed: bool = False


def _bundle_names(source: Path) -> tuple:
    """Bundle files to deploy: none (legacy source) or all; partial is refused."""
    present = [n for n in _BUNDLE_NAMES if (source.parent / n).is_file()]
    if not present:
        return ()
    missing = [n for n in _BUNDLE_NAMES if n not in present]
    if missing:
        raise DeploymentError(
            "incomplete capability bundle in source; missing: " + ", ".join(missing)
        )
    return _BUNDLE_NAMES


def _supervision_names(source: Path) -> tuple:
    """Supervision files to deploy: none (legacy source) or all three; partial is refused."""
    present = [n for n in _SUPERVISION_NAMES if (source.parent / n).is_file()]
    if not present:
        return ()
    missing = [n for n in _SUPERVISION_NAMES if n not in present]
    if missing:
        raise DeploymentError(
            "incomplete supervision bundle in source; missing: " + ", ".join(missing)
        )
    return _SUPERVISION_NAMES


def _goal_supervisor_names(source: Path) -> tuple:
    present = tuple(n for n in _GOAL_SUPERVISOR_NAMES + _GOAL_OPTIONAL_NAMES
                    if (source.parent / n).is_file())
    if not present:
        return ()
    missing = tuple(n for n in _GOAL_SUPERVISOR_NAMES if n not in present)
    if missing:
        raise DeploymentError("incomplete goal supervisor bundle; missing: " + ", ".join(missing))
    return _GOAL_SUPERVISOR_NAMES + tuple(n for n in _GOAL_OPTIONAL_NAMES if n in present)


def _requires_module(source: Path, module: str) -> bool:
    """Inspect the selected source's sibling dependencies without executing it."""
    if not source.is_file():
        return False  # _plan reports the missing source itself
    try:
        tree = ast.parse(source.read_bytes(), filename=str(source))
    except (SyntaxError, ValueError) as exc:
        raise DeploymentError(f"cannot parse engine source {source}: {exc}") from exc
    for node in ast.walk(tree):
        if isinstance(node, ast.Import) and any(
                a.name.split(".")[0] == module for a in node.names):
            return True
        if (isinstance(node, ast.ImportFrom) and not node.level
                and (node.module or "").split(".")[0] == module):
            return True
        if isinstance(node, ast.Constant) and node.value == module + ".py":
            return True  # fixed sibling-loader literal
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id == "capability_module" and node.args
                and isinstance(node.args[0], ast.Constant) and node.args[0].value == module):
            return True
    return False


def _requires_profile(source: Path) -> bool:
    return _requires_module(source, "execution_profile")


def _recovery_names(source: Path) -> tuple:
    if _requires_profile(source) and not (source.parent / "execution_profile.py").is_file():
        raise DeploymentError("selected engine source requires execution_profile.py, "
                              "which is missing; refusing incomplete bundle")
    if (_requires_module(source, "command_output")
            and not (source.parent / "command_output.py").is_file()):
        raise DeploymentError("selected engine source requires command_output.py, "
                              "which is missing; refusing incomplete bundle")
    present = tuple(n for n in _RECOVERY_NAMES + _RECOVERY_OPTIONAL_NAMES
                    if (source.parent / n).is_file())
    if not present:
        return ()
    missing = tuple(n for n in _RECOVERY_NAMES if n not in present)
    if missing:
        raise DeploymentError("incomplete recovery family; missing: " + ", ".join(missing))
    if not (_bundle_names(source) and _supervision_names(source)
            and _goal_supervisor_names(source)):
        raise DeploymentError("recovery family requires complete capability, "
                              "supervision and goal supervisor bundles")
    return _RECOVERY_NAMES + tuple(n for n in _RECOVERY_OPTIONAL_NAMES if n in present)


def _context_names(source: Path) -> tuple:
    # Static dependency, not file presence, decides; members come only from source.parent.
    if not any(_requires_module(source, m) for m in ("context_policy", "context_engine")):
        return ()
    missing = tuple(n for n in _CONTEXT_NAMES if not (source.parent / n).is_file())
    if missing:
        raise DeploymentError("selected engine source requires the context family; "
                              "missing: " + ", ".join(missing))
    if not _recovery_names(source):
        raise DeploymentError("context family requires the complete recovery family")
    return _CONTEXT_NAMES


def gate_command(include_bundle: bool, include_watcher: bool = False,
                  include_diagnostics: bool = False,
                  include_supervision: bool = False,
                  include_goal_supervisor: bool = False,
                  include_recovery: bool = False,
                  include_context: bool = False) -> list:
    """The contract-test gate, extended for the optional files being deployed."""
    command = list(_DEFAULT_TEST)
    if include_bundle:
        extra = [t for t in _BUNDLE_TESTS if (_REPO_ROOT / t).is_file()]
        command += extra   # legacy argv stays an exact prefix
    if include_watcher:
        command += [t for t in _WATCHER_TESTS if (_REPO_ROOT / t).is_file()]
    if include_diagnostics:
        command += [t for t in _DIAGNOSTIC_TESTS if (_REPO_ROOT / t).is_file()]
    if include_supervision:
        command += [t for t in _SUPERVISION_TESTS if (_REPO_ROOT / t).is_file()]
    if include_goal_supervisor:
        command += [t for t in _GOAL_SUPERVISOR_TESTS if (_REPO_ROOT / t).is_file()]
    if include_recovery:
        found = {p.relative_to(_REPO_ROOT).as_posix()
                 for g in _RECOVERY_TEST_GLOBS for p in (_REPO_ROOT / "tests").glob(g)}
        command += [t for t in sorted(found) if t not in command]
    if include_context:
        found = {p.relative_to(_REPO_ROOT).as_posix()
                 for g in _CONTEXT_TEST_GLOBS for p in (_REPO_ROOT / "tests").glob(g)}
        command += [t for t in sorted(found) if t not in command]
    return command


def _plan(source: Path, destination: Path) -> list:
    plan = []
    names = (_DEPLOYED_NAMES + _bundle_names(source) + _supervision_names(source)
             + _goal_supervisor_names(source) + _recovery_names(source)
             + _context_names(source))
    if (source.parent / _WATCHER_NAME).is_file():
        names += (_WATCHER_NAME,)
    if (source.parent / _DIAGNOSTIC_NAME).is_file():
        names += (_DIAGNOSTIC_NAME,)
    identity_source = source.parent / _IDENTITY_NAME
    if not identity_source.is_file():
        identity_source = source.parent.parent / "src" / _IDENTITY_NAME
    if identity_source.is_file():
        names += (_IDENTITY_NAME,)
    owner_source = source.parent / _OWNER_PATCH_NAME
    if not owner_source.is_file():
        owner_source = source.parent.parent / "tools/kilo/owner-patchkit/manifest.json"
    if owner_source.is_file():
        names += (_OWNER_PATCH_NAME,)
    for name in names:
        src = (source if name == _ENGINE_NAME else identity_source if name == _IDENTITY_NAME
               else owner_source if name == _OWNER_PATCH_NAME else source.parent / name)
        dst = destination if name == _ENGINE_NAME else destination.parent / name
        if not src.is_file():
            raise DeploymentError(f"source file missing: {src}")
        plan.append(_Planned(name=name, destination=dst, payload=src.read_bytes()))
    return plan


def _verify(item: _Planned) -> None:
    # Looked up at call time so tests can substitute either verifier.
    if item.name == _ENGINE_NAME:
        verify_installation(item.destination, item.payload)
    else:
        verify_compiled(item.destination, item.payload)


def _restore_all(plan: list) -> None:
    """Put every planned file back as it was before this deployment."""
    problems = []
    for item in plan:
        try:
            if item.backup is not None:
                _atomic_write(item.destination, item.backup.read_bytes())
            elif not item.existed and item.destination.exists():
                item.destination.unlink()
        except OSError as exc:
            problems.append(f"{item.destination}: {exc}")
    if problems:
        raise DeploymentError("restore incomplete: " + "; ".join(problems))


def deploy(
    source: Path,
    destination: Path,
    *,
    apply: bool,
    test_command,
) -> DeploymentResult:
    source = Path(source)
    destination = Path(destination)
    plan = _plan(source, destination)
    engine = plan[0]
    source_hash = _sha256(engine.payload)

    if not apply:
        current = destination.read_bytes() if destination.is_file() else None
        changed = [
            item.name
            for item in plan
            if not item.destination.is_file()
            or item.destination.read_bytes() != item.payload
        ]
        return DeploymentResult(
            applied=False,
            source_sha256=source_hash,
            destination_sha256=_sha256(current) if current is not None else None,
            backup_path=None,
            message="dry run; no changes made"
            + (f" (would update: {', '.join(changed)})" if changed else ""),
        )

    run_contract_tests(test_command)

    # Back up everything before writing anything, so a failure part-way through
    # always has a complete set to restore from.
    for item in plan:
        item.existed = item.destination.is_file()
        if item.existed:
            item.backup = _backup(item.destination)
    try:
        # Compatible helpers must exist before a new entrypoint can use them.
        for item in sorted(plan, key=lambda entry: entry.name == _ENGINE_NAME):
            _atomic_write(item.destination, item.payload)
            _verify(item)
    except Exception as exc:
        try:
            _restore_all(plan)
        except DeploymentError as restore_error:
            raise DeploymentError(
                f"deployment failed ({exc}) and {restore_error}"
            ) from exc
        if isinstance(exc, DeploymentError):
            raise
        raise DeploymentError(f"deployment failed: {exc}") from exc

    backups = tuple(str(item.backup) for item in plan if item.backup is not None)
    return DeploymentResult(
        applied=True,
        source_sha256=source_hash,
        destination_sha256=_sha256(destination.read_bytes()),
        backup_path=str(engine.backup) if engine.backup is not None else None,
        message="engine deployed: " + ", ".join(item.name for item in plan),
        backup_paths=backups,
    )


def rollback(destination: Path, backup: Path) -> DeploymentResult:
    """Restore one file from its backup.

    ``destination`` is the installed ``delegate.py``; a helper backup
    (``ask-model.py.backup-*`` / ``fanout.py.backup-*``) is restored to the
    helper of that name beside it.
    """
    destination = Path(destination)
    backup = Path(backup)
    match = _ROLLBACK_RE.fullmatch(backup.name)
    if not backup.is_file() or not match:
        raise DeploymentError(
            "rollback requires a delegate.py, ask-model.py or fanout.py "
            ".backup-* file"
        )
    if match.group(1) != _ENGINE_NAME:
        destination = destination.parent / match.group(1)
    payload = backup.read_bytes()
    if destination.is_file():
        _backup(destination)
    _atomic_write(destination, payload)
    return DeploymentResult(
        applied=True,
        source_sha256=_sha256(payload),
        destination_sha256=_sha256(destination.read_bytes()),
        backup_path=str(backup),
        message="engine rolled back",
    )


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="deploy_engine")
    parser.add_argument("--source", type=Path, default=_DEFAULT_SOURCE)
    parser.add_argument("--destination", type=Path, default=_DEFAULT_DESTINATION)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--rollback", type=Path)
    args = parser.parse_args(argv)

    if args.rollback is not None:
        result = rollback(args.destination, args.rollback)
    else:
        gate_options = {
            "include_watcher": (Path(args.source).parent / _WATCHER_NAME).is_file(),
            "include_diagnostics": (Path(args.source).parent / _DIAGNOSTIC_NAME).is_file(),
            "include_supervision": bool(_supervision_names(Path(args.source))),
        }
        if _goal_supervisor_names(Path(args.source)):
            gate_options["include_goal_supervisor"] = True
        if _recovery_names(Path(args.source)):
            gate_options["include_recovery"] = True
        if _context_names(Path(args.source)):
            gate_options["include_context"] = True
        result = deploy(
            args.source,
            args.destination,
            apply=args.apply,
            test_command=gate_command(bool(_bundle_names(Path(args.source))), **gate_options),
        )
    print(result.message)
    for backup in result.backup_paths or (
        (result.backup_path,) if result.backup_path else ()
    ):
        print(f"backup: {backup}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

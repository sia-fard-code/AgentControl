"""Actual scoped DATA identity, not metadata self-hash or acceptance authority."""
import dataclasses
import importlib.util
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "engine"))
import candidate
from supervisor_manifest_v2 import Manifest2, PROFILE, SCHEMA
from supervisor_policy import ManifestError, git_environment

SPEC = importlib.util.spec_from_file_location(
    "supervisor_identity_v2_subject", ROOT / "engine" / "supervisor_launch_v2.py")
launch = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(launch)
FAILURE = "candidate identity verification failed"


@dataclasses.dataclass(frozen=True)
class Descriptor:
    bounds: tuple
    inputs: tuple = ("DATA",)
    kind: str = "artifact-manifest"
    initial_sha256: str = "a" * 64

    @property
    def baseline_commit(self):
        raise AssertionError("artifact baseline getter accessed")


@dataclasses.dataclass(frozen=True)
class GitDescriptor:
    bounds: tuple
    baseline_commit: str
    inputs: tuple = ("DATA",)
    kind: str = "git"


def manifest(policy, run, descriptor, seed="b" * 64):
    return Manifest2(schema=SCHEMA, root=str(policy), policy_sha256="c" * 64,
                     orchestration_id="synthetic", attempt_id="attempt", launch_id="launch",
                     run_id="run", role="worker", write_paths=("DATA/",), commands=(),
                     mcp="none", max_iters=1, envelope_sha256="d" * 64, candidate=descriptor,
                     seed_candidate=seed, seed_ref="seed/snapshot", seed_receipt="e" * 64,
                     candidate_root=str(run / "candidate"), run_dir=str(run), profile=PROFILE,
                     recovery_contract_ref="contract.json", recovery_contract_sha256="f" * 64)


@pytest.fixture
def artifact(tmp_path):
    policy, run = tmp_path / "policy", tmp_path / "run"
    root = run / "candidate"
    policy.mkdir()
    (root / "DATA").mkdir(parents=True)
    (root / "DATA/binary.bin").write_bytes(b"\x00synthetic\xff\n")
    desc = Descriptor(tuple(dataclasses.asdict(candidate.SnapshotBounds()).items()))
    seed = candidate.identity(root, bounds=candidate.SnapshotBounds(**dict(desc.bounds)),
                              inputs=desc.inputs, owner_guard=lambda: True, strict_layout=True)
    return manifest(policy, run, desc, seed), root


def denied(provider, root):
    with pytest.raises(ManifestError) as error:
        provider(root)
    assert str(error.value) == FAILURE and error.value.__cause__ is None


def test_artifact_never_uses_git_and_binary_changes_are_observations(artifact, monkeypatch):
    m, root = artifact

    def forbidden(*args, **kwargs):
        raise AssertionError("unexpected Git/process I/O")

    monkeypatch.setattr(subprocess, "run", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    provider = launch.identity_provider2(m, owner_guard=lambda: True)
    assert provider(root) == m.seed_candidate
    assert launch.verify_seed2(m, root, owner_guard=lambda: True)
    (root / "DATA/binary.bin").write_bytes(b"\x00changed\xfe")
    assert provider(root) != m.seed_candidate
    assert not launch.verify_seed2(m, root, owner_guard=lambda: True)


@pytest.mark.parametrize("name", ["receipt.json", ".git", ".GIT", "DATA/.git"])
def test_artifact_has_no_operational_or_metadata_exclusions(artifact, name):
    m, root = artifact
    (root / name).write_bytes(b"synthetic")
    denied(launch.identity_provider2(m, owner_guard=lambda: True), root)


@pytest.mark.parametrize("answer", [False, None, 1, "true"])
def test_owner_must_return_literal_true(artifact, answer):
    m, root = artifact
    denied(launch.identity_provider2(m, owner_guard=lambda: answer), root)


def test_foreign_callback_path_is_rejected_before_probe(artifact, monkeypatch):
    m, root = artifact
    provider = launch.identity_provider2(m, owner_guard=lambda: True)

    def forbidden(*args, **kwargs):
        raise AssertionError("foreign path was probed")

    monkeypatch.setattr(launch.os.path, "realpath", forbidden)
    monkeypatch.setattr(launch.os, "lstat", forbidden)
    denied(provider, root.parent / "foreign")


def git(cwd, *args):
    if shutil.which("git") is None:
        pytest.skip("Git unavailable")
    r = subprocess.run(["git", "-c", "core.hooksPath=", "-c", "core.fsmonitor=false",
                        "-c", "user.name=Synthetic", "-c", "user.email=s@example.invalid",
                        *args], cwd=cwd, env=git_environment(), capture_output=True,
                       timeout=30)
    assert r.returncode == 0
    return r.stdout.decode().strip()


@pytest.fixture
def linked(tmp_path):
    policy, run = tmp_path / "policy", tmp_path / "run"
    policy.mkdir()
    run.mkdir()
    git(policy, "init")
    (policy / "DATA").mkdir()
    (policy / "DATA/binary.bin").write_bytes(b"\x00git\xff")
    git(policy, "add", "DATA")
    git(policy, "commit", "-m", "synthetic")
    baseline = git(policy, "rev-parse", "HEAD")
    root = run / "candidate"
    git(policy, "worktree", "add", "--detach", str(root), baseline)
    admin = Path((root / ".git").read_text()[8:].strip())
    desc = GitDescriptor(tuple(dataclasses.asdict(candidate.SnapshotBounds()).items()), baseline)
    return manifest(policy, run, desc), root, admin


def test_real_git_data_identity_and_admin_redirect_rejection(linked):
    m, root, admin = linked
    launch._administration(launch._canonical(m.root), launch._canonical(root),
                           m.candidate.baseline_commit, lambda: True)
    provider = launch.identity_provider2(m, owner_guard=lambda: True)
    first = provider(root)
    assert len(first) == 64 and provider(root) == first
    (root / "DATA/binary.bin").write_bytes(b"changed")
    assert provider(root) != first
    (admin / "gitdir").write_text(str(root.parent / "foreign/.git") + "\n")
    denied(provider, root)


@pytest.mark.parametrize("replacement", [b"gitdir: nowhere", b"gitdir: nowhere\r\n",
                                        b"gitdir: nowhere\nextra\n", b"gitdir: \xff\n"])
def test_git_pointer_requires_exact_bounded_utf8_line(linked, replacement):
    m, root, _ = linked
    (root / ".git").chmod(stat.S_IWRITE | stat.S_IREAD)
    # Git marks this file hidden on Windows. Opening an existing hidden file
    # with CREATE_ALWAYS ("wb") is denied even after clearing read-only.
    with (root / ".git").open("r+b") as stream:
        stream.write(replacement)
        stream.truncate()
    denied(launch.identity_provider2(m, owner_guard=lambda: True), root)


def test_owner_rechecked_after_real_data_capture(artifact, monkeypatch):
    m, root = artifact
    owner = [True]
    original = candidate._hash_file

    def hash_then_lose(*args, **kwargs):
        result = original(*args, **kwargs)
        owner[0] = False
        return result

    monkeypatch.setattr(candidate, "_hash_file", hash_then_lose)
    denied(launch.identity_provider2(m, owner_guard=lambda: owner[0]), root)

"""Native read-only handles; only self-created synthetic children are terminated."""
import importlib.util
import os
from pathlib import Path
import subprocess
import sys

import pytest

SOURCE = Path(__file__).resolve().parents[1] / "src/harness_process_identity.py"


def identity_module():
    assert SOURCE.exists(), "retained process identity support is missing"
    spec = importlib.util.spec_from_file_location("synthetic_identity", SOURCE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_self_birth_is_stable_and_invalid_pid_is_unknown():
    identity = identity_module()
    first = identity.process_birth(os.getpid())
    assert first and identity.process_birth(os.getpid()) == first
    assert identity.process_birth(-1) is None
    assert identity.process_birth(True) is None
    assert identity.process_birth(4294967294) is None


@pytest.mark.skipif(os.name != "nt", reason="Windows retained-handle exit collection")
@pytest.mark.parametrize("code", [0, 17, 259])
def test_retained_handle_survives_exit_and_preserves_status(code):
    identity = identity_module()
    child = subprocess.Popen([sys.executable, "-c", "import sys; print('ready',flush=True); sys.exit(int(input()))"],
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    handle = None
    try:
        assert child.stdout.readline().strip() == "ready"
        handle = identity.ProcessHandle.open(child.pid)
        assert handle is not None and handle.birth.startswith("winft:")
        assert handle.poll()["state"] == "running"
        child.communicate(f"{code}\n", timeout=10)
        found = handle.poll()
        assert found["state"] == "exited" and found["exit_code"] == code
        assert found["exit_ticks"] and int(found["exit_ticks"]) > 0
        handle.close()
        handle.close()
        assert handle.poll()["state"] == "unknown"
    finally:
        if handle:
            handle.close()
        if child.poll() is None:
            child.kill()
        child.communicate(timeout=10)


@pytest.mark.skipif(os.name != "nt", reason="Windows exact process creation check")
def test_creation_mismatch_is_not_accepted():
    identity = identity_module()
    assert identity.ProcessHandle.open(os.getpid(), expected_creation_time=0) is None


@pytest.mark.skipif(os.name != "nt", reason="Windows retained-handle hard exit collection")
def test_external_hard_exit_has_observed_status_not_an_invented_reason():
    identity = identity_module()
    child = subprocess.Popen([sys.executable, "-c", "import sys; print('ready',flush=True); input()"],
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    handle = None
    try:
        assert child.stdout.readline().strip() == "ready"
        handle = identity.ProcessHandle.open(child.pid)
        assert handle
        child.kill()
        child.communicate(timeout=10)
        result = handle.poll()
        assert result["state"] == "exited" and result["exit_code"] == child.returncode
        assert "reason" not in result
    finally:
        if handle:
            handle.close()
        if child.poll() is None:
            child.kill()
        child.communicate(timeout=10)


@pytest.mark.skipif(os.name != "nt", reason="Windows actual Popen handle identity")
def test_child_identity_comes_from_the_held_spawn_handle():
    identity = identity_module()
    child = subprocess.Popen([sys.executable, "-c", "print('ready',flush=True); input()"],
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        assert child.stdout.readline().strip() == "ready"
        with identity.ProcessHandle.open(child.pid) as handle:
            assert identity.birth_for_child(child) == handle.birth
        child.communicate("\n", timeout=10)
        assert identity.birth_for_child(child)
        assert identity.birth_for_child(type("UnrelatedPID", (), {"pid": os.getpid()})()) is None
    finally:
        if child.poll() is None:
            child.kill()
        child.communicate(timeout=10)

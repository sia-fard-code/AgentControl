"""Child protocol-v2 activation and source CLI routing tests."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
ENGINE = ROOT / "engine"
sys.path.insert(0, str(ENGINE))


def cold_cli(tmp_path, *arguments):
    env = dict(os.environ, HOME=str(tmp_path), USERPROFILE=str(tmp_path))
    return subprocess.run(
        [sys.executable, str(ENGINE / "delegate.py"), *arguments],
        cwd=tmp_path, env=env, text=True, capture_output=True, timeout=30,
    )


def test_capabilities_v2_is_readonly_json(tmp_path):
    from supervisor_manifest_v2 import capabilities2

    result = cold_cli(tmp_path, "--supervisor-capabilities-v2")
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == json.loads(json.dumps(
        capabilities2(), default=lambda value: dict(value)
    ))
    assert list(tmp_path.iterdir()) == []


def test_identity_provider2_is_public():
    from supervisor_launch_v2 import identity_provider2

    assert callable(identity_provider2)

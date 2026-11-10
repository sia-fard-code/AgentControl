from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "deploy_engine.py"
PROFILE = "execution_profile.py"
PASS = [sys.executable, "-c", "print('ok')"]  # gate passes: cannot protect staging
LOADER = 'PROFILE = _load_sibling("execution_profile.py")\nHARNESS_VERSION = 6\n'


@pytest.fixture(scope="module")
def dm():
    spec = importlib.util.spec_from_file_location("deploy_engine_activation", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def dirs(dm, tmp_path, monkeypatch):
    monkeypatch.setattr(dm, "verify_installation", lambda *a: None)
    monkeypatch.setattr(dm, "verify_compiled", lambda *a: None)
    src, dst = tmp_path / "engine", tmp_path / "bin"
    src.mkdir()
    dst.mkdir()
    (dst / "delegate.py").write_text("SENTINEL = 5\n", encoding="utf-8")
    for name in (dm._HELPER_NAMES + dm._BUNDLE_NAMES + dm._SUPERVISION_NAMES
                 + dm._GOAL_SUPERVISOR_NAMES + dm._RECOVERY_NAMES):
        (src / name).write_text(f"NAME = {name!r}\n", encoding="utf-8")
    return src, dst


def _deploy(dm, src, dst, text):
    (src / "delegate.py").write_text(text, encoding="utf-8")
    return dm.deploy(src / "delegate.py", dst / "delegate.py", apply=True, test_command=PASS)


@pytest.mark.parametrize("text", [
    LOADER,
    "import execution_profile\nHARNESS_VERSION = 6\n",
    "from execution_profile import select\nHARNESS_VERSION = 6\n",
])
def test_missing_activation_module_refused_before_writes(dm, dirs, text):
    src, dst = dirs
    before = sorted(p.name for p in dst.iterdir())
    with pytest.raises(dm.DeploymentError, match="requires execution_profile.py"):
        _deploy(dm, src, dst, text)
    assert sorted(p.name for p in dst.iterdir()) == before  # no backups, no writes
    assert (dst / "delegate.py").read_text(encoding="utf-8") == "SENTINEL = 5\n"


def test_complete_bundle_with_activation_module_installs(dm, dirs):
    src, dst = dirs
    (src / PROFILE).write_text("X = 1\n", encoding="utf-8")
    result = _deploy(dm, src, dst, LOADER)
    assert result.applied and (dst / PROFILE).read_text(encoding="utf-8") == "X = 1\n"
    assert (dst / "delegate.py").read_text(encoding="utf-8") == LOADER


def test_legacy_source_without_dependency_still_installs(dm, dirs):
    src, dst = dirs
    text = "# old note: execution_profile.py\nHARNESS_VERSION = 6\n"
    assert _deploy(dm, src, dst, text).applied
    assert not (dst / PROFILE).exists()


def test_unparseable_source_fails_before_writes(dm, dirs):
    src, dst = dirs
    with pytest.raises(dm.DeploymentError, match="cannot parse"):
        _deploy(dm, src, dst, "def broken(:\n")
    assert (dst / "delegate.py").read_text(encoding="utf-8") == "SENTINEL = 5\n"
    assert len(list(dst.iterdir())) == 1

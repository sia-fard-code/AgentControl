"""Selective browser deployment: synthetic files, temporary policy/install only."""
import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def module():
    spec = importlib.util.spec_from_file_location(
        "deploy_browser_under_test", ROOT / "scripts" / "deploy_browser_research.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def fixture(tmp_path):
    source, install = tmp_path / "source", tmp_path / "installed"
    source.mkdir()
    for name in ("server.mjs", "public_proxy.py"):
        (source / name).write_text("synthetic asset " + name, encoding="utf-8")
    policy = tmp_path / "agent-policy.json"
    original = {"allowed_cmds": [["git", "status"]], "max_read_chars": 30000,
                "mcp": {"allowed_commands": [["existing.exe"]], "servers": {
                    "existing": {"command": ["existing.exe"], "enabled": False}}}}
    policy.write_text(json.dumps(original), encoding="utf-8")
    command = [str(tmp_path / "node.exe"), str(install / "server.mjs")]
    return source, install, policy, original, command


def test_preview_does_not_change_policy_or_install(tmp_path):
    source, install, policy, original, command = fixture(tmp_path)
    result = module().deploy(source, install, policy, command, apply=False)
    assert result["applied"] is False
    assert not install.exists()
    assert json.loads(policy.read_text()) == original


def test_deploy_is_selective_and_preserves_permissions_with_backup(tmp_path):
    source, install, policy, original, command = fixture(tmp_path)
    mod = module()
    result = mod.deploy(source, install, policy, command, apply=True)
    actual = json.loads(policy.read_text())
    assert actual["allowed_cmds"] == original["allowed_cmds"]
    assert actual["max_read_chars"] == 30000
    assert actual["mcp"]["servers"]["existing"] == original["mcp"]["servers"]["existing"]
    server = actual["mcp"]["servers"]["research_browser"]
    assert server["command"] == command and server["enabled"] is True
    assert server["allowed_tools"] == server["mutating_tools"] == mod.TOOLS
    assert command in actual["mcp"]["allowed_commands"]
    assert {p.name for p in install.iterdir()} == set(mod.ASSETS)
    assert json.loads((Path(result["backup"]) / "policy.json").read_text()) == original
    assert "allowed_cmds" not in result  # no policy contents in output metadata


def test_policy_write_failure_rolls_assets_back(tmp_path, monkeypatch):
    source, install, policy, original, command = fixture(tmp_path)
    mod = module()
    mod.deploy(source, install, policy, command, apply=True)
    old = (install / "server.mjs").read_bytes()
    before = policy.read_bytes()
    (source / "server.mjs").write_text("synthetic changed asset", encoding="utf-8")
    real_replace = mod.os.replace

    def fail_policy(src, dst):
        if Path(dst) == policy:
            raise OSError("synthetic failure")
        return real_replace(src, dst)

    monkeypatch.setattr(mod.os, "replace", fail_policy)
    with pytest.raises(OSError):
        mod.deploy(source, install, policy, command, apply=True)
    assert (install / "server.mjs").read_bytes() == old
    assert policy.read_bytes() == before


def test_refuse_install_directory_with_unowned_files(tmp_path):
    source, install, policy, original, command = fixture(tmp_path)
    install.mkdir()
    (install / "unrelated.txt").write_text("synthetic unrelated", encoding="utf-8")
    with pytest.raises(ValueError, match="unowned"):
        module().deploy(source, install, policy, command, apply=True)
    assert json.loads(policy.read_text()) == original

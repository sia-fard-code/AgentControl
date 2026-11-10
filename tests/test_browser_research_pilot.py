"""Offline boundary checks for the disabled browser-research MCP fragment. Temp dirs only;
no browser, no network, no command execution."""
import copy
import importlib.util
import json
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from engine import harness_mcp as hm  # noqa: E402

FRAGMENT = ROOT / "config" / "browser-research-pilot.json"
TOOLS = ["browser_navigate", "browser_navigate_back", "browser_close"]
REQUIRED = ["--headless", "--isolated", "--sandbox", "--block-service-workers",
            "--no-webmcp", "--codegen", "none", "--image-responses", "omit",
            "--timeout-action", "5000", "--timeout-navigation", "15000",
            "--timeout-settle", "1000"]
FORBIDDEN = ["--port", "--host", "--extension", "--cdp-endpoint", "--endpoint",
             "--storage-state", "--secrets", "--user-data-dir", "--caps",
             "--no-sandbox", "--save-session", "--save-trace", "--output-dir"]
VERSION = "0.0.83"


def fragment():
    return json.loads(FRAGMENT.read_text(encoding="utf-8"))


def server():
    return fragment()["servers"]["research_browser"]


def test_fragment_shape_is_exact_and_disabled():
    frag = fragment()
    assert set(frag) == {"allowed_commands", "servers"}
    assert frag["allowed_commands"] == []
    assert list(frag["servers"]) == ["research_browser"]
    srv = server()
    assert srv["enabled"] is False
    assert srv["env"] == {}
    assert isinstance(srv["timeout_s"], (int, float)) and 0 < srv["timeout_s"] <= 120
    assert srv["allowed_tools"] == TOOLS
    assert srv["mutating_tools"] == TOOLS


def test_command_is_placeholder_with_safe_args_only():
    cmd = server()["command"]
    assert cmd[0].startswith("<ABSOLUTE-PATH-TO>") and cmd[0].endswith("node.exe")
    assert cmd[1].startswith("<ABSOLUTE-PATH-TO>") and f"@playwright/mcp@{VERSION}" in cmd[1]
    assert not any(t in a.lower() for a in cmd for t in ("npx", ".cmd", ".bat", "*"))
    args = cmd[2:]
    assert args == REQUIRED  # no extra configuration, proxy, or script flags
    for i in range(0, len(REQUIRED)):
        assert REQUIRED[i] in args
    for flag in ("--codegen", "--image-responses", "--timeout-action",
                 "--timeout-navigation", "--timeout-settle"):
        assert args[args.index(flag) + 1] == REQUIRED[REQUIRED.index(flag) + 1]
    assert not any(a.split("=")[0] in FORBIDDEN for a in args)
    assert "browser_snapshot" not in server()["allowed_tools"]


def test_fragment_validates_and_enabling_needs_exact_grant(tmp_path):
    pol = hm.validate_mcp_policy(fragment())
    assert pol["allowed_commands"] == [] and pol["servers"]["research_browser"]["enabled"] is False
    enabled = copy.deepcopy(fragment())
    enabled["servers"]["research_browser"]["enabled"] = True
    with pytest.raises(hm.MCPError):
        hm.validate_mcp_policy(enabled)  # placeholder path and no grant
    real = str(tmp_path / "node.exe")  # absolute, never executed
    enabled["servers"]["research_browser"]["command"][:2] = [real, str(tmp_path / "cli.js")]
    with pytest.raises(hm.MCPError, match="allowed_commands"):
        hm.validate_mcp_policy(enabled)


def test_disabled_server_never_launches(tmp_path):
    mgr = hm.MCPManager(hm.validate_mcp_policy(fragment()), str(tmp_path))
    try:
        with pytest.raises(hm.MCPError, match="disabled"):
            mgr.list_tools("research_browser")
        with pytest.raises(hm.MCPError):
            mgr.call("mcp__research_browser__browser_navigate", {"url": "about:blank"})
        assert not mgr._servers
    finally:
        mgr.close()


def _load_delegate(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "home"))
    spec = importlib.util.spec_from_file_location(
        "delegate_browser_pilot", ROOT / "engine" / "delegate.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_fragment_adds_no_shell_permissions(tmp_path, monkeypatch):
    delegate = _load_delegate(tmp_path, monkeypatch)
    repo = tmp_path / "repo"
    (repo / ".claude").mkdir(parents=True)
    base = {"allowed_cmds": [["git", "status"]]}
    pfile = repo / ".claude" / "agent-policy.json"
    pfile.write_text(json.dumps(base), encoding="utf-8")
    delegate.load_policy(str(repo))
    before = (copy.deepcopy(delegate.ALLOWED_CMDS), delegate.MAX_READ_CHARS)
    assert delegate.MCP_POLICY is None
    pfile.write_text(json.dumps({**base, "mcp": fragment()}), encoding="utf-8")
    delegate.load_policy(str(repo))
    assert (delegate.ALLOWED_CMDS, delegate.MAX_READ_CHARS) == before
    assert before == ([["git", "status"]], 30_000)
    assert delegate.MCP_POLICY["allowed_commands"] == []
    assert delegate.MCP_POLICY["servers"]["research_browser"]["enabled"] is False


def _smoke_module():
    spec = importlib.util.spec_from_file_location(
        "browser_smoke_helpers", ROOT / "tests" / "test_browser_research_smoke.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("content", [None, [], [{"text": "synthetic-private-marker"}]])
def test_smoke_error_reports_no_response_content(content):
    with pytest.raises(AssertionError) as error:
        _smoke_module()._text({"isError": True, "content": content})
    assert "synthetic-private-marker" not in str(error.value)


def test_smoke_missing_paths_fail_instead_of_skip(monkeypatch):
    monkeypatch.setenv("AGENTCONTROL_BROWSER_NODE", "")
    monkeypatch.setenv("AGENTCONTROL_BROWSER_MCP_CLI", "")
    with pytest.raises(AssertionError, match="AGENTCONTROL_BROWSER_NODE"):
        _smoke_module()._paths()


def test_smoke_wrong_package_version_fails(tmp_path, monkeypatch):
    node, cli = tmp_path / "node.exe", tmp_path / "cli.js"
    node.touch()
    cli.touch()
    (tmp_path / "package.json").write_text(
        json.dumps({"name": "@playwright/mcp", "version": "0.0.38"}), encoding="utf-8")
    monkeypatch.setenv("AGENTCONTROL_BROWSER_NODE", str(node))
    monkeypatch.setenv("AGENTCONTROL_BROWSER_MCP_CLI", str(cli))
    with pytest.raises(AssertionError):
        _smoke_module()._paths()

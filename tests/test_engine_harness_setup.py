"""Tests for engine/harness_setup.py and engine/init-agent-harness.py.

Everything runs against a synthetic HOME, config and repo root in tmp_path; no
real ~/.claude, Kilo or Claude config is read, and no gateway is contacted.
"""
from __future__ import annotations

import importlib.util
import io
import json
import os
import py_compile
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO = Path(__file__).resolve().parents[1]
ENGINE = REPO / "engine"
SECRET = "s3cr3t-VALUE-do-not-copy"


def _load():
    spec = importlib.util.spec_from_file_location(
        "harness_setup_under_test", ENGINE / "harness_setup.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def hs():
    return _load()


@pytest.fixture
def env(tmp_path, monkeypatch):
    home = tmp_path / "home"
    root = tmp_path / "repo"
    home.mkdir()
    root.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    return SimpleNamespace(home=home, root=root, tmp=tmp_path)


def run(hs, env, *args, selftest_ok=True):
    calls = []

    def fake_selftest(root):
        calls.append(root)
        return selftest_ok, "selftest_policy: PASS" if selftest_ok else "FAIL"

    out, err = io.StringIO(), io.StringIO()
    code = hs.main(["-C", str(env.root), *args], home=str(env.home),
                   run_selftest=fake_selftest, stdout=out, stderr=err)
    return code, out.getvalue() + err.getvalue(), calls


def policy_of(env):
    return json.loads((env.root / ".claude" / "agent-policy.json").read_text("utf-8"))


def snapshot(root: Path) -> dict:
    return {str(p.relative_to(root)): p.read_bytes()
            for p in sorted(root.rglob("*")) if p.is_file()}


# ----------------------------------------------------------- initial setup --

@pytest.mark.parametrize("marker, expected", [
    ("Cargo.toml", [["cargo", "test"], ["cargo", "check"], ["cargo", "clippy"]]),
    ("go.mod", [["go", "test", "./..."], ["go", "build", "./..."], ["go", "vet"]]),
    ("pom.xml", [["mvn", "-q", "test"]]),
])
def test_stack_defaults_preserve_original_command_boundary(hs, env, marker, expected):
    (env.root / marker).write_text("synthetic", encoding="utf-8")
    _, commands = hs.detect_commands(str(env.root))
    stack = expected[0][0]
    assert [rule for rule in commands if rule[0] == stack] == expected

def test_initial_python_setup_generates_policy_settings_gitignore_selftest(hs, env):
    (env.root / "pyproject.toml").write_text("[project]\nname='x'\n")
    (env.root / "scripts").mkdir()
    (env.root / "tools").mkdir()
    (env.root / "build").mkdir()
    (env.root / "generated_out").mkdir()
    (env.root / ".gitignore").write_text("generated_out/\n")
    code, text, calls = run(hs, env)
    assert code == 0, text
    pol = policy_of(env)
    cmds = pol["allowed_cmds"]
    for rule in (["python", "-m", "pytest"], ["python", "-m", "py_compile", "*"],
                 ["ruff", "check"], ["python", "scripts/*"], ["node", "scripts/*"],
                 ["python", "tools/agent-harness/*"], ["git", "status"],
                 ["git", "diff"], ["git", "log"], ["git", "ls-files"]):
        assert rule in cmds, rule
    assert ["python", "tools/*"] not in cmds        # only the FIRST scripts dir
    for d in (".git", ".claude", "node_modules", "__pycache__", "build",
              "generated_out"):
        assert d in pol["skip_dirs"], d
    assert "dist" not in pol["skip_dirs"]           # absent on disk
    assert "mcp" not in pol and "skills_dirs" not in pol
    assert isinstance(pol["comment"], list)
    settings = json.loads((env.root / ".claude" / "settings.json").read_text())
    assert "Bash(python tools/agent-harness/selftest_policy.py:*)" in \
        settings["permissions"]["allow"]
    gi = (env.root / ".gitignore").read_text()
    for line in (".claude/agent-runs/", ".claude/worktrees/",
                 ".claude/settings.local.json", "*.patch"):
        assert line in gi.splitlines()
    selftest = env.root / "tools" / "agent-harness" / "selftest_policy.py"
    py_compile.compile(str(selftest), cfile=str(env.tmp / "s.pyc"), doraise=True)
    assert calls == [str(env.root)]
    assert "PASS" in text


def test_stack_markers(hs, env):
    (env.root / "package.json").write_text("{}")
    (env.root / "requirements.txt").write_text("")
    (env.root / "Cargo.toml").write_text("")
    (env.root / "go.mod").write_text("")
    (env.root / "pom.xml").write_text("")
    cmds = hs.detect_commands(str(env.root))[1]
    for rule in (["npm", "test"], ["npm", "run", "build"], ["npm", "run", "lint"],
                 ["npx", "vitest", "run"], ["npx", "tsc"],
                 ["python", "-m", "pytest"], ["python", "-m", "py_compile", "*"],
                 ["cargo", "test"], ["go", "test", "./..."], ["mvn", "-q", "test"]):
        assert rule in cmds, rule
    assert ["ruff", "check"] not in cmds            # requirements.txt only
    assert len(cmds) == len({tuple(c) for c in cmds})


def test_settings_merge_preserves_existing_and_gitignore_not_duplicated(hs, env):
    claude = env.root / ".claude"
    claude.mkdir()
    (claude / "settings.json").write_text(json.dumps(
        {"model": "x", "permissions": {"allow": ["Bash(ls:*)"], "deny": ["y"]}}))
    (env.root / ".gitignore").write_text("*.patch\n.claude/worktrees/\n")
    assert run(hs, env)[0] == 0
    settings = json.loads((claude / "settings.json").read_text())
    assert settings["model"] == "x" and settings["permissions"]["deny"] == ["y"]
    allow = settings["permissions"]["allow"]
    assert allow[0] == "Bash(ls:*)" and len(allow) == len(set(allow))
    gi = (env.root / ".gitignore").read_text().splitlines()
    assert gi.count("*.patch") == 1 and gi.count(".claude/worktrees/") == 1


def test_existing_policy_kept_without_force_and_replaced_with_force(hs, env):
    claude = env.root / ".claude"
    claude.mkdir()
    custom = {"allowed_cmds": [["git", "status"]], "skip_dirs": [".git"]}
    (claude / "agent-policy.json").write_text(json.dumps(custom))
    code, text, _ = run(hs, env)
    assert code == 0 and policy_of(env) == custom
    assert "kept" in text.lower()
    (env.root / "pyproject.toml").write_text("")
    assert run(hs, env, "--force")[0] == 0
    assert ["python", "-m", "pytest"] in policy_of(env)["allowed_cmds"]


def test_dry_run_writes_nothing(hs, env):
    (env.root / "pyproject.toml").write_text("")
    before = snapshot(env.root)
    code, text, calls = run(hs, env, "--dry-run")
    assert code == 0
    assert snapshot(env.root) == before and calls == []
    assert "allowed_cmds" in text


def test_selftest_failure_is_reported_as_failure(hs, env):
    code, text, _ = run(hs, env, selftest_ok=False)
    assert code != 0 and "selftest" in text.lower()


# ------------------------------------------------------------- MCP imports --

def write_kilo(env, servers, name="kilo.json"):
    path = env.tmp / name
    path.write_text(json.dumps({"mcp": servers, "provider": {"k": SECRET}}))
    return path


def write_claude(env, servers, projects=None):
    (env.root / ".mcp.json").write_text(json.dumps({"mcpServers": servers}))
    data = {"oauthAccount": {"token": SECRET}, "projects": projects or {}}
    (env.home / ".claude.json").write_text(json.dumps(data))


def base_policy(env, extra=None):
    claude = env.root / ".claude"
    claude.mkdir(exist_ok=True)
    pol = {"comment": ["keep me"], "allowed_cmds": [["git", "status"]],
           "skip_dirs": [".git", "custom"], "max_read_chars": 12345,
           "skills_dirs": ["skills"]}
    pol.update(extra or {})
    (claude / "agent-policy.json").write_text(json.dumps(pol, indent=2))
    return pol


def test_kilo_import_local_only_disabled_env_names_only(hs, env):
    path = write_kilo(env, {
        "fs": {"type": "local", "command": ["fs-server", "--root", "."],
               "environment": {"API_TOKEN": SECRET}, "timeout": 2500},
        "web": {"type": "remote", "url": "https://example.test/mcp"},
    })
    pol = base_policy(env)
    code, text, _ = run(hs, env, "--mcp-from-kilo", str(path))
    assert code == 0, text
    new = policy_of(env)
    for key in ("comment", "allowed_cmds", "skip_dirs", "max_read_chars",
                "skills_dirs"):
        assert new[key] == pol[key], key
    assert new["mcp"]["allowed_commands"] == []
    assert new["mcp"]["servers"] == {"fs": {
        "command": ["fs-server", "--root", "."], "env": {}, "timeout_s": 2.5,
        "allowed_tools": [], "mutating_tools": [], "enabled": False}}
    assert "web" not in new["mcp"]["servers"] and "remote" in text
    assert "API_TOKEN" in text
    assert SECRET not in text
    assert SECRET not in (env.root / ".claude" / "agent-policy.json").read_text()


def test_claude_import_projectand_user_scoped(hs, env):
    root_key = str(env.root)
    write_claude(env, {"a": {"command": "srv-a", "args": ["x"],
                             "env": {"KEY": SECRET}}},
                 projects={root_key: {"mcpServers": {
                     "b": {"type": "stdio", "command": "srv-b"},
                     "h": {"type": "http", "url": "https://x.test"}}},
                     "/other": {"mcpServers": {"z": {"command": "zz"}}}})
    base_policy(env)
    code, text, _ = run(hs, env, "--mcp-from-claude")
    assert code == 0, text
    servers = policy_of(env)["mcp"]["servers"]
    assert sorted(servers) == ["a", "b"]
    assert servers["a"]["command"] == ["srv-a", "x"]
    assert servers["a"]["env"] == {} and servers["a"]["enabled"] is False
    assert "KEY" in text and SECRET not in text


@pytest.mark.parametrize("argv", [
    ["srv", "--api-key", "abc"],
    ["srv", "--token=abc"],
    ["srv", "https://user:pw@host.test/x"],
    ["srv", "https://host.test/x?access_token=abc"],
    ["srv", "--password", "hunter2"],
    ["srv", "ghp_" + "a" * 36],
])
def test_credential_like_args_skip_server_without_echo(hs, env, argv):
    path = write_kilo(env, {"bad": {"type": "local", "command": argv},
                            "ok": {"type": "local", "command": ["fine"]}})
    base_policy(env)
    code, text, _ = run(hs, env, "--mcp-from-kilo", str(path))
    assert code == 0, text
    servers = policy_of(env)["mcp"]["servers"]
    assert "bad" not in servers and "ok" in servers
    assert "bad" in text
    for a in argv[1:]:
        assert a not in text


def test_conflicting_duplicates_across_sources_fail(hs, env):
    path = write_kilo(env, {"dup": {"type": "local", "command": ["one"]}})
    write_claude(env, {"dup": {"command": "two"}})
    pol_before = base_policy(env)
    raw = (env.root / ".claude" / "agent-policy.json").read_bytes()
    code, text, _ = run(hs, env, "--mcp-from-kilo", str(path), "--mcp-from-claude")
    assert code != 0 and "dup" in text
    assert (env.root / ".claude" / "agent-policy.json").read_bytes() == raw
    assert pol_before == policy_of(env)


def test_identical_duplicates_are_merged(hs, env):
    path = write_kilo(env, {"same": {"type": "local", "command": ["srv", "a"]}})
    write_claude(env, {"same": {"command": "srv", "args": ["a"]}})
    base_policy(env)
    code, text, _ = run(hs, env, "--mcp-from-kilo", str(path), "--mcp-from-claude")
    assert code == 0, text
    assert list(policy_of(env)["mcp"]["servers"]) == ["same"]


def test_existing_enabled_server_never_altered(hs, env):
    existing = {"allowed_commands": [["/bin/live"]], "servers": {
        "live": {"command": ["/bin/live"], "env": {"X": "1"}, "timeout_s": 5,
                 "allowed_tools": ["read"], "mutating_tools": [],
                 "enabled": True}}}
    base_policy(env, {"mcp": existing})
    path = write_kilo(env, {"live": {"type": "local", "command": ["other"]},
                            "new": {"type": "local", "command": ["n"]}})
    code, text, _ = run(hs, env, "--mcp-from-kilo", str(path))
    assert code == 0, text
    mcp = policy_of(env)["mcp"]
    assert mcp["servers"]["live"] == existing["servers"]["live"]
    assert mcp["allowed_commands"] == [["/bin/live"]]
    assert mcp["servers"]["new"]["enabled"] is False
    assert "live" in text and "warning" in text.lower()


def test_server_filter_and_unknown_server(hs, env):
    path = write_kilo(env, {"a": {"type": "local", "command": ["a"]},
                            "b": {"type": "local", "command": ["b"]}})
    base_policy(env)
    code, _, _ = run(hs, env, "--mcp-from-kilo", str(path), "--mcp-server", "b")
    assert code == 0 and list(policy_of(env)["mcp"]["servers"]) == ["b"]
    raw = (env.root / ".claude" / "agent-policy.json").read_bytes()
    code, text, _ = run(hs, env, "--mcp-from-kilo", str(path),
                        "--mcp-server", "nope")
    assert code != 0 and "nope" in text
    assert (env.root / ".claude" / "agent-policy.json").read_bytes() == raw


def test_import_dry_run_redacted_and_writes_nothing(hs, env):
    path = write_kilo(env, {"fs": {"type": "local", "command": ["fs"],
                                   "environment": {"TOKEN": SECRET}}})
    base_policy(env)
    before = snapshot(env.root)
    code, text, calls = run(hs, env, "--mcp-from-kilo", str(path), "--dry-run")
    assert code == 0 and snapshot(env.root) == before and calls == []
    assert '"fs"' in text and SECRET not in text


def test_force_with_import_keeps_existing_allowlist(hs, env):
    pol = base_policy(env)
    path = write_kilo(env, {"fs": {"type": "local", "command": ["fs"]}})
    code, _, _ = run(hs, env, "--force", "--mcp-from-kilo", str(path))
    assert code == 0
    assert policy_of(env)["allowed_cmds"] == pol["allowed_cmds"]


@pytest.mark.parametrize("timeout,expected", [
    (2500, 2.5), (0, 120), (-5, 120), (10 ** 12, 3600), ("x", 120), (None, 120),
    (True, 120),
])
def test_timeout_conversion_is_finite_and_bounded(hs, timeout, expected):
    assert hs.kilo_timeout_s(timeout) == expected


def test_imported_policy_passes_runtime_validator(hs, env):
    if not (ENGINE / "harness_mcp.py").is_file():
        pytest.skip("harness_mcp.py not present")
    spec = importlib.util.spec_from_file_location("hm_for_setup",
                                                  ENGINE / "harness_mcp.py")
    hm = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(hm)
    path = write_kilo(env, {"fs": {"type": "local", "command": ["fs", "x"],
                                   "timeout": 1000}})
    write_claude(env, {"c": {"command": "c", "args": ["--stdio"]}})
    base_policy(env)
    assert run(hs, env, "--mcp-from-kilo", str(path), "--mcp-from-claude")[0] == 0
    hm.validate_mcp_policy(policy_of(env)["mcp"])


def test_cli_wrapper_dry_run_subprocess(env):
    (env.root / "pyproject.toml").write_text("")
    child_env = dict(os.environ, HOME=str(env.home), USERPROFILE=str(env.home))
    before = snapshot(env.root)
    r = subprocess.run([sys.executable, str(ENGINE / "init-agent-harness.py"),
                        "-C", str(env.root), "--dry-run"],
                       capture_output=True, text=True, env=child_env, timeout=120)
    assert r.returncode == 0, r.stderr
    assert "allowed_cmds" in r.stdout and snapshot(env.root) == before


def install_engine(home: Path) -> None:
    bin_dir = home / ".claude" / "bin"
    bin_dir.mkdir(parents=True)
    for name in ("delegate.py", "harness_mcp.py", "harness_skills.py", "harness_diagnostics.py",
                 "context_guard.py"):
        if (ENGINE / name).is_file():
            shutil.copy2(ENGINE / name, bin_dir / name)


def test_generated_selftest_passes_against_engine_in_synthetic_home(hs, env):
    install_engine(env.home)
    (env.root / "pyproject.toml").write_text("")
    (env.root / "scripts").mkdir()
    out = io.StringIO()
    code = hs.main(["-C", str(env.root)], home=str(env.home), stdout=out,
                   stderr=out, selftest_env={"HOME": str(env.home),
                                             "USERPROFILE": str(env.home)})
    text = out.getvalue()
    assert code == 0, text
    assert "selftest_policy: PASS" in text
    if (ENGINE / "harness_mcp.py").is_file():
        assert "mcp=checked" in text


def test_generated_selftest_fails_for_overbroad_policy(hs, env):
    install_engine(env.home)
    claude = env.root / ".claude"
    claude.mkdir()
    (claude / "agent-policy.json").write_text(json.dumps(
        {"allowed_cmds": [["python"], ["git", "status"]], "skip_dirs": [".git"]}))
    out = io.StringIO()
    code = hs.main(["-C", str(env.root)], home=str(env.home), stdout=out,
                   stderr=out, selftest_env={"HOME": str(env.home),
                                             "USERPROFILE": str(env.home)})
    text = out.getvalue()
    assert code != 0, text
    assert "should be REFUSED but was permitted: python -c" in text


def test_generated_selftest_fails_when_harness_missing(hs, env):
    (env.root / "pyproject.toml").write_text("")
    out = io.StringIO()
    code = hs.main(["-C", str(env.root)], home=str(env.home), stdout=out,
                   stderr=out, selftest_env={"HOME": str(env.home),
                                             "USERPROFILE": str(env.home)})
    assert code != 0
    assert "harness not installed" in out.getvalue()

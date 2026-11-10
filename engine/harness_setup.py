"""Maintained source for the per-repo delegated-agent harness initializer.

``engine/init-agent-harness.py`` is the CLI wrapper; all logic lives here so it
is importable and testable. It generates a STARTING-POINT security boundary for
a repo (``.claude/agent-policy.json``), merges Claude Code permissions into
``.claude/settings.json``, appends harness scratch paths to ``.gitignore``,
writes ``tools/agent-harness/selftest_policy.py`` and runs that selftest before
reporting success.

Optional MCP import (``--mcp-from-kilo`` / ``--mcp-from-claude``) reads local
stdio server definitions IN MEMORY ONLY and adds them as DISABLED entries under
the policy's ``mcp.servers`` with no tools allowed and no environment. It never:

* copies environment VALUES (only the omitted variable names are reported),
* imports remote/HTTP/SSE servers or connects to anything,
* imports a server whose argv looks credential-bearing (the server id and a
  warning are reported; the offending value never is),
* adds ``mcp.allowed_commands`` launch authorizations -- enabling a server is a
  separate, deliberate operator edit,
* alters ``allowed_cmds``/``skip_dirs``/``max_read_chars``/``comment``/
  ``skills_dirs`` or any existing MCP server/key of an existing policy.
"""
from __future__ import annotations

import argparse
import copy
import fnmatch
import json
import math
import os
import re
import subprocess
import sys
from urllib.parse import parse_qsl, urlsplit

POLICY_REL = os.path.join(".claude", "agent-policy.json")
SETTINGS_REL = os.path.join(".claude", "settings.json")
SELFTEST_REL = os.path.join("tools", "agent-harness", "selftest_policy.py")

COMMON_SKIP = (".git", ".claude", "node_modules", "__pycache__")
OPTIONAL_SKIP = ("dist", "build", "target", ".venv", "venv", ".mypy_cache",
                 ".pytest_cache", ".next", ".turbo", "vendor", "coverage")
SCRIPT_DIRS = ("scripts", "tools", "bin")
GIT_RULES = (["git", "status"], ["git", "diff"], ["git", "log"],
             ["git", "ls-files"])
GITIGNORE_LINES = (".claude/agent-runs/", ".claude/worktrees/",
                   ".claude/settings.local.json", "*.patch")
SETTINGS_ALLOW = (
    "Bash(python $HOME/.claude/bin/ask-model.py:*)",
    "Bash(python $HOME/.claude/bin/delegate.py:*)",
    "Bash(python $HOME/.claude/bin/fanout.py:*)",
    "Bash(python tools/agent-harness/selftest_policy.py:*)",
    "Bash(git status:*)",
    "Bash(git diff:*)",
    "Bash(git log:*)",
    "Bash(git ls-files:*)",
)

DEFAULT_TIMEOUT_S = 120
MAX_TIMEOUT_S = 3600
MIN_TIMEOUT_S = 1


class SetupError(Exception):
    """A clear, user-facing refusal. Never carries a secret value."""


def _add(rules, rule):
    if rule not in rules:
        rules.append(rule)


def detect_commands(root):
    """Return (stack labels, allowed_cmds) from stack marker files."""
    labels, rules = [], []
    has = lambda name: os.path.isfile(os.path.join(root, name))  # noqa: E731
    if has("package.json"):
        labels.append("Node (package.json)")
        for r in (["npm", "test"], ["npm", "run", "build"], ["npm", "run", "lint"],
                  ["npx", "vitest", "run"], ["npx", "tsc"]):
            _add(rules, r)
    if has("pyproject.toml"):
        labels.append("Python (pyproject) (pyproject.toml)")
        for r in (["python", "-m", "pytest"], ["python", "-m", "py_compile", "*"],
                  ["ruff", "check"]):
            _add(rules, r)
    if has("requirements.txt"):
        labels.append("Python (requirements.txt)")
        for r in (["python", "-m", "pytest"], ["python", "-m", "py_compile", "*"]):
            _add(rules, r)
    if has("Cargo.toml"):
        labels.append("Rust (Cargo.toml)")
        for r in (["cargo", "test"], ["cargo", "check"], ["cargo", "clippy"]):
            _add(rules, r)
    if has("go.mod"):
        labels.append("Go (go.mod)")
        for r in (["go", "test", "./..."], ["go", "build", "./..."], ["go", "vet"]):
            _add(rules, r)
    if has("pom.xml"):
        labels.append("Java (pom.xml)")
        for r in (["mvn", "-q", "test"],):
            _add(rules, r)
    for d in SCRIPT_DIRS:
        if os.path.isdir(os.path.join(root, d)):
            labels.append(f"scripts in {d}/")
            _add(rules, ["python", f"{d}/*"])
            _add(rules, ["node", f"{d}/*"])
            break
    _add(rules, ["python", "tools/agent-harness/*"])
    for r in GIT_RULES:
        _add(rules, list(r))
    return labels, rules


def _gitignored_top_dirs(root):
    """Top-level directories matched by a simple, non-negated .gitignore line."""
    path = os.path.join(root, ".gitignore")
    try:
        with open(path, encoding="utf-8") as fh:
            lines = fh.read().splitlines()
    except (OSError, UnicodeDecodeError):
        return []
    try:
        dirs = [e.name for e in os.scandir(root) if e.is_dir()]
    except OSError:
        return []
    out = []
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith(("#", "!")):
            continue
        pat = line.strip("/")
        if not pat or "/" in pat or pat.startswith("**"):
            continue
        out += [d for d in dirs if fnmatch.fnmatchcase(d, pat) and d not in out]
    return out


def detect_skip_dirs(root):
    skip = list(COMMON_SKIP)
    for d in OPTIONAL_SKIP + tuple(_gitignored_top_dirs(root)):
        if d not in skip and os.path.isdir(os.path.join(root, d)):
            skip.append(d)
    return sorted(skip)


def generate_policy(root):
    labels, rules = detect_commands(root)
    detected = ", ".join(labels) or "no known stack markers"
    comment = [
        f"Security boundary for delegated agents. Detected: {detected}.",
        "This file REPLACES the harness built-in allowlist; it does not extend it.",
        "Generated by init-agent-harness.py as a STARTING POINT. Review it.",
        "allowed_cmds: argv prefix rules, fnmatch per element, NO shell. Path-shaped",
        "arguments that are absolute or contain '..' are refused.",
        "NEVER add: python -c / node -e; git push / git commit; any shell (bash, sh,",
        "powershell, cmd); rm / del. Remove rules that reach operator-gated scripts.",
        "skip_dirs: search hygiene for grep/list_dir, NOT access control.",
        "mcp (optional): imported servers are DISABLED with no tools allowed and no",
        "env. Launching one needs an explicit mcp.allowed_commands entry that",
        "exactly matches its command -- a separate, deliberate operator edit.",
    ]
    return {"comment": comment, "allowed_cmds": rules,
            "skip_dirs": detect_skip_dirs(root)}


# ------------------------------------------------------- secret detection --
_SECRET_WORD = re.compile(
    r"(api[-_]?key|apikey|token|secret|passw(or)?d|passwd|pwd|credential|"
    r"auth|bearer|private[-_]?key|client[-_]?secret|access[-_]?key|session)",
    re.IGNORECASE)
_SECRET_SHAPE = re.compile(
    r"^(gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|sk-[A-Za-z0-9_-]{16,}|"
    r"xox[abprs]-[A-Za-z0-9-]{10,}|AKIA[0-9A-Z]{16}|AIza[0-9A-Za-z_-]{30,}|"
    r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\..*|Bearer\s+\S+)$")


def _uri_has_secret(arg):
    if "://" not in arg:
        return False
    try:
        parts = urlsplit(arg)
    except ValueError:
        return True
    if parts.username or parts.password or "@" in parts.netloc:
        return True
    for key, _value in parse_qsl(parts.query, keep_blank_values=True):
        if _SECRET_WORD.search(key):
            return True
    return False


def argv_looks_credential(argv):
    """True if any argument is credential-like. Never returns the value."""
    for i, arg in enumerate(argv):
        if _uri_has_secret(arg) or _SECRET_SHAPE.match(arg.strip()):
            return True
        if arg.startswith("-") or "=" in arg:
            flag = arg.lstrip("-").split("=", 1)[0]
            if _SECRET_WORD.search(flag):
                return True
        elif i > 0 and _SECRET_WORD.search(arg) and "=" in arg:
            return True
    return False


def kilo_timeout_s(value):
    """Kilo timeout (milliseconds) -> finite bounded seconds."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return DEFAULT_TIMEOUT_S
    if not math.isfinite(value) or value <= 0:
        return DEFAULT_TIMEOUT_S
    seconds = value / 1000.0
    seconds = min(max(seconds, MIN_TIMEOUT_S), MAX_TIMEOUT_S)
    return int(seconds) if float(seconds).is_integer() else seconds


# ------------------------------------------------------------- parsers --
# Each candidate is (server_id, source_label, argv | None, env_names, problem).

def _read_json(path, label):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except FileNotFoundError:
        return None
    except (OSError, ValueError, UnicodeDecodeError) as exc:
        raise SetupError(f"cannot parse {label} ({type(exc).__name__})") from None


def _env_names(env):
    return sorted(k for k in env if isinstance(k, str)) if isinstance(env, dict) else []


def parse_kilo(data, label):
    out = []
    mcp = data.get("mcp") if isinstance(data, dict) else None
    if not isinstance(mcp, dict):
        return out
    servers = mcp.get("server") if isinstance(mcp.get("server"), dict) else mcp
    for sid, spec in servers.items():
        if not isinstance(spec, dict):
            continue
        kind = spec.get("type", "local")
        names = _env_names(spec.get("environment", spec.get("env")))
        if kind != "local" or "url" in spec:
            out.append((sid, label, None, names, "remote/non-stdio, not imported"))
            continue
        cmd = spec.get("command")
        if not isinstance(cmd, list) or not cmd or \
                not all(isinstance(a, str) and a for a in cmd):
            out.append((sid, label, None, names, "command is not an argv list"))
            continue
        out.append((sid, label, list(cmd), names,
                    kilo_timeout_s(spec.get("timeout"))))
    return out


def parse_claude_servers(servers, label):
    out = []
    if not isinstance(servers, dict):
        return out
    for sid, spec in servers.items():
        if not isinstance(spec, dict):
            continue
        names = _env_names(spec.get("env"))
        kind = spec.get("type", "stdio")
        if kind != "stdio" or "url" in spec:
            out.append((sid, label, None, names, "remote/non-stdio, not imported"))
            continue
        cmd, args = spec.get("command"), spec.get("args", [])
        if not isinstance(cmd, str) or not cmd or not isinstance(args, list) or \
                not all(isinstance(a, str) and a for a in args):
            out.append((sid, label, None, names, "command/args malformed"))
            continue
        out.append((sid, label, [cmd] + list(args), names, DEFAULT_TIMEOUT_S))
    return out


DEFAULT_SOURCE = "__default__"
NAME_RE = re.compile(r"^[A-Za-z0-9_-]+$")


def _norm(path):
    return os.path.normcase(os.path.normpath(os.path.abspath(path)))


def _claude_file(path, label, root, include_projects):
    data = _read_json(path, label)
    if not isinstance(data, dict):
        return [] if data is None else []
    out = parse_claude_servers(data.get("mcpServers"), label)
    if include_projects and isinstance(data.get("projects"), dict):
        for key, proj in data["projects"].items():
            if isinstance(key, str) and isinstance(proj, dict) and \
                    _norm(key) == _norm(root):
                out += parse_claude_servers(proj.get("mcpServers"),
                                            f"{label} projects[root]")
    return out


def collect_candidates(root, home, kilo=None, claude=None):
    """Parse the selected sources in memory; returns candidate tuples."""
    cands = []
    if kilo is not None:
        path = (os.path.join(home, ".config", "kilo", "kilo.json")
                if kilo == DEFAULT_SOURCE else kilo)
        data = _read_json(path, "kilo config")
        if data is None:
            raise SetupError(f"kilo config not found: {path}")
        cands += parse_kilo(data, "kilo")
    if claude is not None:
        if claude == DEFAULT_SOURCE:
            files = [(os.path.join(root, ".mcp.json"), ".mcp.json", False),
                     (os.path.join(home, ".claude.json"), "~/.claude.json", True)]
        else:
            files = [(claude, "claude config", True)]
        if not any(os.path.isfile(p) for p, _l, _i in files):
            raise SetupError("no Claude MCP config found (.mcp.json / ~/.claude.json)")
        for path, label, projects in files:
            cands += _claude_file(path, label, root, projects)
    return cands


def select_servers(cands, wanted):
    """Apply --mcp-server, refuse credential argv, detect conflicting duplicates.

    Returns (importable {sid: (argv, timeout_s, env_names)}, report lines).
    """
    report, by_id = [], {}
    if wanted:
        known = {c[0] for c in cands}
        missing = [w for w in wanted if w not in known]
        if missing:
            raise SetupError("unknown MCP server(s) selected: " + ", ".join(missing)
                             + "; available: " + (", ".join(sorted(known)) or "none"))
        cands = [c for c in cands if c[0] in wanted]
    for sid, label, argv, names, extra in cands:
        if argv is not None and argv_looks_credential(argv):
            argv, extra = None, "credential-like argument"
        key = (tuple(argv) if argv else None, extra)
        prior = by_id.get(sid)
        if prior and prior[0] != key:
            raise SetupError(f"MCP server {sid!r} is defined differently in "
                             f"{prior[1]} and {label}; refusing to choose one. "
                             "Use --mcp-server to select, or fix a source.")
        by_id.setdefault(sid, (key, label, sorted(set(names))))
        if prior:
            by_id[sid] = (key, prior[1], sorted(set(prior[2]) | set(names)))
    importable = {}
    for sid in sorted(by_id):
        (argv, extra), label, names = by_id[sid]
        if argv is None:
            report.append(f"warning: skipped MCP server {sid!r} from {label}: {extra}"
                          + (" (no value shown)" if "credential" in extra else ""))
            continue
        if not NAME_RE.match(sid) or "__" in sid or len(sid) > 50:
            report.append(f"warning: skipped MCP server {sid!r}: invalid name")
            continue
        importable[sid] = (list(argv), extra, names)
    return importable, report


def merge_mcp(policy, importable):
    """Return (new policy, report). Never alters existing keys or servers."""
    new = copy.deepcopy(policy)
    report = []
    mcp = new.get("mcp")
    if mcp is None:
        mcp = new["mcp"] = {}
    if not isinstance(mcp, dict) or not isinstance(mcp.get("servers", {}), dict):
        raise SetupError("existing policy 'mcp' is malformed; fix it by hand first")
    mcp.setdefault("allowed_commands", [])
    servers = mcp.setdefault("servers", {})
    lowered = {s.lower(): s for s in servers}
    for sid, (argv, timeout_s, names) in importable.items():
        clash = lowered.get(sid.lower())
        if clash is not None:
            state = "enabled" if servers[clash].get("enabled", True) else "disabled"
            report.append(f"warning: MCP server {sid!r} collides with existing "
                          f"{state} server {clash!r}; existing entry retained "
                          "unchanged, import skipped")
            continue
        servers[sid] = {"command": argv, "env": {}, "timeout_s": timeout_s,
                        "allowed_tools": [], "mutating_tools": [], "enabled": False}
        lowered[sid.lower()] = sid
        line = f"imported MCP server {sid!r} (disabled, no tools allowed)"
        if names:
            line += "; env omitted, set by hand if needed: " + ", ".join(names)
        report.append(line)
    return new, report


def redacted(policy):
    shown = copy.deepcopy(policy)
    servers = (shown.get("mcp") or {}).get("servers") if isinstance(
        shown.get("mcp"), dict) else None
    if isinstance(servers, dict):
        for spec in servers.values():
            if isinstance(spec, dict) and isinstance(spec.get("env"), dict):
                spec["env"] = {k: "<redacted>" for k in spec["env"]}
    return shown


_SELFTEST_HEAD = r'''#!/usr/bin/env python3
"""Verify this repo's delegated-agent security boundary.

The boundary is `.claude/agent-policy.json`, interpreted by the shared harness at
~/.claude/bin/delegate.py. That script is machine-wide and serves several repos,
so nothing in this repo pins its behaviour. This test is the pin.

Generated by init-agent-harness.py. Extend ALLOWED and REFUSED as the policy
grows: a rule nobody asserted is a rule nobody will notice losing. It reads the
repo policy only, writes temp scratch only, and executes no command and no MCP
server (subprocess is replaced by recorders and restored afterwards).

    python tools/agent-harness/selftest_policy.py
"""
import importlib.util
import json
import os
import pathlib
import sys
import tempfile

REPO = pathlib.Path(__file__).resolve().parents[2]
HARNESS = pathlib.Path.home() / ".claude" / "bin" / "delegate.py"
MIN_HARNESS_VERSION = 2      # 2 introduced per-repo policy loading

if not HARNESS.exists():
    sys.exit(f"error: harness not installed at {HARNESS}")

spec = importlib.util.spec_from_file_location("delegate_under_test", HARNESS)
delegate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(delegate)

installed = getattr(delegate, "HARNESS_VERSION", 0)
if installed < MIN_HARNESS_VERSION:
    sys.exit(f"error: {HARNESS} is harness version {installed or 'pre-2'}, need "
             f">= {MIN_HARNESS_VERSION}. Below that, .claude/agent-policy.json is "
             "IGNORED. Reinstall the current harness.")

failures = []


def check(label, condition):
    if not condition:
        failures.append(label)


policy = json.loads((REPO / ".claude" / "agent-policy.json").read_text(encoding="utf-8"))
keys = getattr(delegate, "POLICY_KEYS", set())
HAS_MCP = "mcp" in keys and hasattr(delegate, "capability_module")
HAS_SKILLS = "skills_dirs" in keys and hasattr(delegate, "capability_module")
for key, present in (("mcp", HAS_MCP), ("skills_dirs", HAS_SKILLS)):
    check(f"policy uses '{key}' but the installed harness lacks that capability",
          key not in policy or present)

note = delegate.load_policy(str(REPO))
check("policy file not found; the run would fall back to the built-ins",
      ".claude" in note and "built-in" not in note)
check("ALLOWED_CMDS does not match the policy file",
      delegate.ALLOWED_CMDS == [list(r) for r in policy["allowed_cmds"]])
if "skip_dirs" in policy:
    check("SKIP_DIRS does not match the policy file",
          delegate.SKIP_DIRS == set(policy["skip_dirs"]))
if "max_read_chars" in policy:
    check("MAX_READ_CHARS does not match the policy file",
          delegate.MAX_READ_CHARS == policy["max_read_chars"])

docs = {name: desc for name, _a, desc in delegate.tool_docs()}
check("run_command's description does not advertise this repo's allowlist",
      " ".join(policy["allowed_cmds"][0]) in docs["run_command"])
check("the read limit stated to the model is not the one enforced",
      f"{delegate.MAX_READ_CHARS:,}" in docs["read_file"])


class FakeCompleted:
    returncode, stdout, stderr = 0, "", ""


executed = []
_real_run, _real_which = delegate.subprocess.run, delegate.shutil.which
delegate.subprocess.run = lambda argv, **kw: (executed.append(argv), FakeCompleted())[1]
# Allowed tools need not be installed here: permission, not presence, is tested.
delegate.shutil.which = lambda name, *a, **k: name
ALLOWED = [list(r) for r in policy["allowed_cmds"]]
REFUSED = [
    ["npm", "test"] if ["npm", "test"] not in ALLOWED else ["npm", "publish"],
    ["python", "-c", "import shutil"],
    ["python", "-u", "-c", "import shutil"],
    ["node", "-e", "require('fs')"],
    ["bash", "-c", "ls"],
    ["powershell", "-Command", "ls"],
    ["rm", "-rf", "src"],
    ["git", "push"],
    ["git", "commit", "-m", "x"],
    ["python", "scripts/../../evil.py"],
    ["python", "/tmp/evil.py"],
    ["python", "-m", "pytest", "/tmp/evil_test.py"],
    ["python", "-m", "pytest", "\\\\server\\share\\evil_test.py"],
    ["python", "-m", "py_compile", "/tmp/evil.py"],
]
try:
    runner = delegate.Runner(str(REPO), write_mode="stage")
    for argv in ALLOWED:
        concrete = [a.replace("*", "x") for a in argv]
        executed.clear()
        out = runner.run_command(concrete)
        check(f"should be ALLOWED but was not run: {' '.join(concrete)} -> {out[:80]}",
              not out.startswith("refused:") and len(executed) == 1)
    for argv in REFUSED:
        executed.clear()
        out = runner.run_command(argv)
        check(f"should be REFUSED but was permitted: {' '.join(argv)}",
              out.startswith("refused:"))
        check(f"REFUSED command still reached the shell: {' '.join(argv)}",
              not executed)
    if hasattr(runner, "close"):
        runner.close()
finally:
    delegate.subprocess.run, delegate.shutil.which = _real_run, _real_which
'''

_SELFTEST_TAIL = r'''

def policy_exits(text, label):
    """A policy the loader cannot trust must exit, never fall back silently."""
    with tempfile.TemporaryDirectory() as d:
        os.makedirs(os.path.join(d, ".claude"))
        with open(os.path.join(d, ".claude", "agent-policy.json"), "w",
                  encoding="utf-8") as fh:
            fh.write(text)
        try:
            delegate.load_policy(d)
        except SystemExit:
            return
        failures.append(f"a {label} policy did not fail closed")


policy_exits("{ not json", "malformed")
policy_exits('{"allowed_cmd": [["git","status"]]}', "misspelled-key")
policy_exits('{"allowed_cmds": "git status"}', "wrong-type")
policy_exits('{"allowed_cmds": []}', "empty")
policy_exits('{"max_read_chars": "big"}', "wrong-type read limit")
SRV = ["selftest-mcp-server", "--stdio"]
if HAS_MCP:
    policy_exits(json.dumps({"mcp": {"servers": {"x": {
        "command": SRV, "enabled": True}}}}), "enabled-MCP-argv-not-authorized")
    policy_exits('{"mcp": {"bogus": 1}}', "unknown-MCP-key")
    policy_exits('{"mcp": {"servers": []}}', "wrong-type-MCP")
if HAS_SKILLS:
    policy_exits('{"skills_dirs": "skills"}', "wrong-type-skills_dirs")


def mcp_refusals():
    """Refusals of the installed MCP runtime. Popen is a recorder: nothing launches."""
    try:
        hm = delegate.capability_module("harness_mcp")
    except Exception as e:
        failures.append(f"policy capability 'mcp' present but runtime missing: "
                        f"{type(e).__name__}")
        return
    launched = []
    real_popen = hm.subprocess.Popen
    hm.subprocess.Popen = lambda *a, **k: launched.append(a) or (_ for _ in ()).throw(
        OSError("selftest: launch blocked"))
    try:
        pol = hm.validate_mcp_policy({"allowed_commands": [SRV], "servers": {"st": {
            "command": SRV, "enabled": True, "allowed_tools": ["read_*", "write_*"],
            "mutating_tools": ["write_*"]}}})
        with tempfile.TemporaryDirectory() as scratch:
            mgr = hm.MCPManager(pol, scratch, allow_mutation=False)
            try:
                def refused(name, label):
                    try:
                        mgr.call(name, {})
                    except Exception as e:
                        check(f"MCP {label}: unexpected error {type(e).__name__}",
                              str(e).startswith("refused:"))
                        return
                    failures.append(f"MCP {label} was not refused")
                refused("mcp__absent__read_x", "absent server")
                tools = getattr(mgr, "_tools", None)
                if isinstance(tools, dict):
                    for tool in ("other_tool", "write_file"):
                        tools[f"mcp__st__{tool}"] = {
                            "server": "st", "tool": tool, "description": "",
                            "schema": {"type": "object"}, "mutating": False}
                    refused("mcp__st__other_tool", "tool outside allowed_tools")
                    refused("mcp__st__write_file", "mutating tool without --write")
                else:
                    failures.append("MCP runtime layout changed; extend this selftest")
            finally:
                mgr.close()
        try:
            hm.validate_mcp_policy({"allowed_commands": [], "servers": {
                "st": {"command": SRV, "enabled": True}}})
            failures.append("enabled MCP argv outside allowed_commands was accepted")
        except Exception:
            pass
    finally:
        hm.subprocess.Popen = real_popen
    check(f"MCP refusal checks launched a process ({len(launched)})", not launched)


if HAS_MCP:
    mcp_refusals()
delegate.load_policy(str(REPO))

if failures:
    print(f"selftest_policy: FAIL ({len(failures)})", file=sys.stderr)
    for f in failures:
        print(f"  - {f}", file=sys.stderr)
    sys.exit(1)
print(f"selftest_policy: PASS ({len(ALLOWED)} allowed, {len(REFUSED)} refused, "
      f"mcp={'checked' if HAS_MCP else 'absent'}, "
      f"skills={'checked' if HAS_SKILLS else 'absent'})")
'''

SELFTEST_SOURCE = _SELFTEST_HEAD + _SELFTEST_TAIL


def _atomic_write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)
    os.replace(tmp, path)


def _dump(policy):
    return json.dumps(policy, indent=2) + "\n"


def merged_settings(path):
    """Return (new settings text or None if unchanged, added entries)."""
    data = {}
    if os.path.isfile(path):
        data = _read_json(path, ".claude/settings.json")
        if not isinstance(data, dict):
            raise SetupError(".claude/settings.json is not a JSON object")
    perms = data.setdefault("permissions", {})
    if not isinstance(perms, dict) or not isinstance(perms.setdefault("allow", []), list):
        raise SetupError(".claude/settings.json permissions are malformed")
    added = [e for e in SETTINGS_ALLOW if e not in perms["allow"]]
    if not added:
        return None, []
    perms["allow"].extend(added)
    return json.dumps(data, indent=2) + "\n", added


def merged_gitignore(path):
    text = ""
    if os.path.isfile(path):
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
    have = {line.strip() for line in text.splitlines()}
    missing = [line for line in GITIGNORE_LINES if line not in have]
    if not missing:
        return None, []
    if text and not text.endswith("\n"):
        text += "\n"
    text += "# Delegated agent harness scratch (per-run, disposable).\n"
    text += "\n".join(missing) + "\n"
    return text, missing


def default_run_selftest(root, extra_env=None):
    env = dict(os.environ)
    env.update(extra_env or {})
    try:
        r = subprocess.run([sys.executable, os.path.join(root, SELFTEST_REL)],
                           cwd=root, env=env, capture_output=True, text=True,
                           timeout=600)
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"selftest could not run: {type(exc).__name__}"
    return r.returncode == 0, (r.stdout + r.stderr).strip()


def build_parser():
    p = argparse.ArgumentParser(prog="init-agent-harness.py",
                                description=__doc__.splitlines()[0])
    p.add_argument("-C", "--root", default=".")
    p.add_argument("--force", action="store_true",
                   help="regenerate the policy and selftest (never during MCP import)")
    p.add_argument("--dry-run", action="store_true",
                   help="show the redacted proposed policy and write nothing")
    p.add_argument("--mcp-from-kilo", nargs="?", const=DEFAULT_SOURCE, default=None,
                   metavar="PATH", help="import local stdio servers from Kilo config "
                   "(default ~/.config/kilo/kilo.json)")
    p.add_argument("--mcp-from-claude", nargs="?", const=DEFAULT_SOURCE, default=None,
                   metavar="PATH", help="import local stdio servers from Claude "
                   "config (default: project .mcp.json and ~/.claude.json)")
    p.add_argument("--mcp-server", action="append", default=[], metavar="ID",
                   help="only import this server id (repeatable)")
    return p


def main(argv=None, *, home=None, run_selftest=None, stdout=None, stderr=None,
         selftest_env=None):
    out, err = stdout or sys.stdout, stderr or sys.stderr
    args = build_parser().parse_args(argv)
    try:
        return _main(args, home or os.path.expanduser("~"),
                     run_selftest or (lambda r: default_run_selftest(r, selftest_env)),
                     out)
    except SetupError as exc:
        print(f"error: {exc}", file=err)
        return 2


def _main(args, home, run_selftest, out):
    root = os.path.abspath(args.root)
    if not os.path.isdir(root):
        raise SetupError(f"root is not a directory: {root}")
    importing = args.mcp_from_kilo is not None or args.mcp_from_claude is not None
    if args.mcp_server and not importing:
        raise SetupError("--mcp-server needs --mcp-from-kilo or --mcp-from-claude")
    policy_path = os.path.join(root, POLICY_REL)
    exists = os.path.isfile(policy_path)
    report = []

    # Parse everything (and refuse conflicts) BEFORE any write.
    importable = {}
    if importing:
        cands = collect_candidates(root, home, args.mcp_from_kilo,
                                   args.mcp_from_claude)
        importable, rep = select_servers(cands, args.mcp_server)
        report += rep

    if exists:
        current = _read_json(policy_path, POLICY_REL)
        if not isinstance(current, dict):
            raise SetupError(f"{POLICY_REL} is not a JSON object; fix it by hand")
        if args.force and not importing:
            policy = generate_policy(root)
            report.append(f"{POLICY_REL}: regenerated (--force)")
        else:
            policy = current
            if args.force:
                report.append("--force ignored for the policy during MCP import: "
                              "the existing command allowlist is kept")
            report.append(f"{POLICY_REL}: existing policy kept")
    else:
        policy = generate_policy(root)
        report.append(f"{POLICY_REL}: generated")
    if importing:
        policy, rep = merge_mcp(policy, importable)
        report += rep

    settings_text, added = merged_settings(os.path.join(root, SETTINGS_REL))
    gi_text, gi_added = merged_gitignore(os.path.join(root, ".gitignore"))
    selftest_path = os.path.join(root, SELFTEST_REL)
    write_selftest = args.force and not importing or not os.path.isfile(selftest_path)
    policy_changed = not exists or policy != current

    if args.dry_run:
        print("dry run: nothing written. Proposed policy (env values redacted):",
              file=out)
        print(_dump(redacted(policy)), file=out)
        for line in report:
            print(line, file=out)
        if added:
            print(f"would add {len(added)} settings permission(s)", file=out)
        if gi_added:
            print("would append to .gitignore: " + ", ".join(gi_added), file=out)
        if write_selftest:
            print(f"would write {SELFTEST_REL}", file=out)
        return 0

    if policy_changed:
        _atomic_write(policy_path, _dump(policy))
    if settings_text is not None:
        _atomic_write(os.path.join(root, SETTINGS_REL), settings_text)
        report.append(f"{SETTINGS_REL}: added {len(added)} permission(s)")
    if gi_text is not None:
        _atomic_write(os.path.join(root, ".gitignore"), gi_text)
        report.append(".gitignore: appended " + ", ".join(gi_added))
    if write_selftest:
        _atomic_write(selftest_path, SELFTEST_SOURCE)
        report.append(f"{SELFTEST_REL}: written")
    for line in report:
        print(line, file=out)

    ok, output = run_selftest(root)
    print(output, file=out)
    if not ok:
        print("error: selftest failed; the harness boundary is NOT verified",
              file=out)
        return 1
    print("agent harness ready", file=out)
    return 0

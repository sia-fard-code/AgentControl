"""Deploy only browser assets and one explicit MCP grant, with rollback backups."""
import argparse
import copy
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile

ASSETS = ("server.mjs", "public_proxy.py")
TOOLS = ["browser_navigate", "browser_navigate_back", "browser_close"]
ROOT = Path(__file__).resolve().parents[1]


def deploy(source, install, policy_path, command, *, apply=False):
    source, install, policy_path = map(Path, (source, install, policy_path))
    for name in ASSETS:
        if not (source / name).is_file() or (source / name).is_symlink():
            raise ValueError("missing or linked browser asset")
    if install.exists() and (install.is_symlink() or
                            {p.name for p in install.iterdir()} - set(ASSETS)):
        raise ValueError("unowned installation directory")
    original = policy_path.read_bytes()
    policy = json.loads(original)
    candidate = copy.deepcopy(policy)
    mcp = candidate.setdefault("mcp", {"allowed_commands": [], "servers": {}})
    old = mcp["servers"].get("research_browser")
    if old and old["command"] != command:
        used_elsewhere = any(s["command"] == old["command"] for n, s in
                             mcp["servers"].items() if n != "research_browser")
        if not used_elsewhere:
            mcp["allowed_commands"] = [c for c in mcp["allowed_commands"]
                                        if c != old["command"]]
    if command not in mcp["allowed_commands"]:
        mcp["allowed_commands"].append(list(command))
    mcp["servers"]["research_browser"] = {
        "command": list(command), "env": {}, "timeout_s": 90,
        "allowed_tools": list(TOOLS), "mutating_tools": list(TOOLS), "enabled": True}
    hashes = {name: hashlib.sha256((source / name).read_bytes()).hexdigest()
              for name in ASSETS}
    result = {"applied": False, "server": "research_browser", "asset_sha256": hashes,
              "shell_permissions_changed": False}
    if not apply:
        return result

    install.parent.mkdir(parents=True, exist_ok=True)
    backup = Path(tempfile.mkdtemp(prefix="browser-research-backup-", dir=install.parent))
    (backup / "policy.json").write_bytes(original)
    stage = Path(tempfile.mkdtemp(prefix=".browser-stage-", dir=install.parent))
    policy_tmp, old_moved, new_moved = None, False, False
    try:
        for name in ASSETS:
            shutil.copy2(source / name, stage / name)
            if hashlib.sha256((stage / name).read_bytes()).hexdigest() != hashes[name]:
                raise RuntimeError("asset changed during deployment")
        fd, name = tempfile.mkstemp(prefix=".browser-policy-", dir=policy_path.parent)
        policy_tmp = Path(name)
        with os.fdopen(fd, "wb") as stream:
            stream.write((json.dumps(candidate, indent=2) + "\n").encode("utf-8"))
            stream.flush()
            os.fsync(stream.fileno())
        if policy_path.read_bytes() != original:
            raise RuntimeError("policy changed during deployment")
        if install.exists():
            os.replace(install, backup / "assets")
            old_moved = True
        os.replace(stage, install)
        new_moved = True
        if policy_path.read_bytes() != original:
            raise RuntimeError("policy changed during deployment")
        os.replace(policy_tmp, policy_path)
        policy_tmp = None
    except BaseException:
        if new_moved:
            shutil.rmtree(install)
        if old_moved:
            os.replace(backup / "assets", install)
        raise
    finally:
        if stage.exists():
            shutil.rmtree(stage)
        if policy_tmp is not None:
            policy_tmp.unlink(missing_ok=True)
    result.update(applied=True, backup=str(backup))
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-dir", type=Path, default=ROOT / "tools" / "browser-research")
    parser.add_argument("--install-dir", type=Path,
                        default=Path.home() / ".claude" / "bin" / "browser-research")
    parser.add_argument("--policy", type=Path, default=ROOT / ".claude" / "agent-policy.json")
    parser.add_argument("--node", type=Path, required=True)
    parser.add_argument("--python", type=Path, default=Path(sys.executable))
    parser.add_argument("--playwright-root", type=Path, required=True)
    parser.add_argument("--browser", type=Path, required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    for path in (args.node, args.python, args.browser):
        if not path.is_absolute() or not path.is_file():
            parser.error("runtime paths must be absolute existing files")
    if not (args.playwright_root.is_absolute() and
            (args.playwright_root / "playwright-core" / "package.json").is_file()):
        parser.error("playwright-root must be an absolute installed dependency root")
    command = [str(args.node), str(args.install_dir / "server.mjs"),
               "--python", str(args.python), "--playwright-root", str(args.playwright_root),
               "--browser", str(args.browser), "--proxy-script", str(args.install_dir / "public_proxy.py")]
    result = deploy(args.source_dir, args.install_dir, args.policy, command, apply=args.apply)
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Run the pinned build tool with disposable HOME/XDG/cache and no release credentials."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import tempfile

COMMIT = "b81761530650f8ec7b071441f4f54d2514f1a244"


def environment(root, compiler, parent=None):
    parent = os.environ if parent is None else parent
    allowed = {"PATH", "SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT", "PROGRAMFILES", "PROGRAMFILES(X86)",
               "PROCESSOR_ARCHITECTURE", "NUMBER_OF_PROCESSORS"}
    env = {key: value for key, value in parent.items() if key.upper() in allowed}
    home = Path(root) / ".owner-isolated"
    env.update(PATH=str(Path(compiler).parent) + os.pathsep + parent.get("PATH", parent.get("Path", "")),
        HOME=str(home / "home"), USERPROFILE=str(home / "home"),
        XDG_DATA_HOME=str(home / "data"), XDG_CONFIG_HOME=str(home / "config"),
        XDG_CACHE_HOME=str(home / "cache"), XDG_STATE_HOME=str(home / "state"),
        TEMP=str(home / "tmp"), TMP=str(home / "tmp"), BUN_INSTALL=str(home / "bun"),
        BUN_INSTALL_CACHE_DIR=str(home / "bun-cache"), KILO_CHANNEL="owner-attribution",
        KILO_VERSION="7.8.18-owner-attribution", KILO_TELEMETRY_LEVEL="off",
        KILO_DISABLE_DEFAULT_PLUGINS="true", KILO_DISABLE_MODELS_FETCH="1",
        KILO_CONFIG_CONTENT=json.dumps({"autoupdate": False}))
    return env


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--compiler", type=Path, required=True)
    parser.add_argument("--cwd", default=".", help="working directory relative to the isolated checkout")
    parser.add_argument("arguments", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    root, compiler = args.root.resolve(), args.compiler.resolve()
    staging = (Path(tempfile.gettempdir()) / "kilo").resolve()
    cwd = (root / args.cwd).resolve()
    if staging not in root.parents or staging not in compiler.parents or not compiler.is_file():
        parser.error("checkout and compiler must be inside the temporary Kilo staging root")
    if cwd != root and root not in cwd.parents:
        parser.error("working directory must stay inside the isolated checkout")
    commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip()
    if commit != COMMIT:
        parser.error("isolated checkout is not the pinned commit")
    env = environment(root, compiler)
    for name in ("HOME", "TEMP", "XDG_DATA_HOME", "XDG_CONFIG_HOME", "XDG_CACHE_HOME", "XDG_STATE_HOME", "BUN_INSTALL_CACHE_DIR"):
        Path(env[name]).mkdir(parents=True, exist_ok=True)
    arguments = args.arguments[1:] if args.arguments[:1] == ["--"] else args.arguments
    if not arguments:
        parser.error("a build-tool command is required after --")
    return subprocess.call([str(compiler), *arguments], cwd=cwd, env=env)


if __name__ == "__main__":
    raise SystemExit(main())

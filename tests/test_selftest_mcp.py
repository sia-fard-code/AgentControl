"""Run the repository boundary pin using copied engines and a synthetic HOME."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]


def test_repository_selftest_checks_mcp_without_launching(tmp_path):
    home = tmp_path / "home"
    engine = home / ".claude" / "bin"
    engine.mkdir(parents=True)
    for source in (ROOT / "engine").glob("*.py"):
        shutil.copyfile(source, engine / source.name)
    repo = tmp_path / "repo"
    policy = repo / ".claude" / "agent-policy.json"
    policy.parent.mkdir(parents=True)
    policy.write_text(json.dumps({"allowed_cmds": [["git", "status"]],
                                 "skip_dirs": [".git", ".claude"]}), encoding="utf-8")
    script = repo / "tools" / "agent-harness" / "selftest_policy.py"
    script.parent.mkdir(parents=True)
    shutil.copyfile(ROOT / "tools" / "agent-harness" / "selftest_policy.py", script)
    env = dict(os.environ, HOME=str(home), USERPROFILE=str(home),
               CLAUDE_AGENT_RUN_INDEX=str(tmp_path / "index.jsonl"))
    result = subprocess.run([sys.executable, str(script)], cwd=repo, env=env,
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "mcp=checked" in result.stdout
    assert not (tmp_path / "index.jsonl").exists()

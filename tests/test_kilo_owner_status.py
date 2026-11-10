import json
import os
from pathlib import Path
import subprocess
import sys


def test_status_reports_unavailable_instead_of_assuming_installed_means_active(tmp_path):
    script = Path(__file__).resolve().parents[1] / "tools/kilo/owner-patchkit/status.py"
    result = subprocess.run([sys.executable, str(script), "--owner-pid", str(os.getpid()),
                             "--directory", str(tmp_path)], capture_output=True, text=True, timeout=10)
    assert result.returncode == 2
    status = json.loads(result.stdout.splitlines()[0])
    assert status["owner"] == "unavailable" and status["pre_kill"] is False
    assert not list(tmp_path.iterdir())

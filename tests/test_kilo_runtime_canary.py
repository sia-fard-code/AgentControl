"""Opt-in real backend plugin canary: isolated auth, home, database and workspace."""
import base64
import json
import os
from pathlib import Path
import socket
import runpy
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

import pytest

BINARY = os.environ.get("KILO_CANARY_BINARY")
ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.skipif(not BINARY, reason="set KILO_CANARY_BINARY for the isolated installed-runtime canary")
def test_real_runtime_loads_plugin_and_advertises_observation_only(tmp_path):
    psutil = pytest.importorskip("psutil")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    plugin = ROOT / "tools/kilo/lifecycle-diagnostics.js"
    config = tmp_path / "kilo.json"
    config.write_text(json.dumps({"plugin": [plugin.as_uri()]}), encoding="utf-8")
    password = str(uuid.uuid4())
    env = {k: v for k, v in os.environ.items()
           if k.upper() in {"PATH", "SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT", "PROGRAMFILES", "PROGRAMFILES(X86)"}}
    env.update(HOME=str(tmp_path), USERPROFILE=str(tmp_path), TEMP=str(tmp_path), TMP=str(tmp_path),
               XDG_DATA_HOME=str(tmp_path / "data"), XDG_CONFIG_HOME=str(tmp_path / "config"),
               XDG_CACHE_HOME=str(tmp_path / "cache"), XDG_STATE_HOME=str(tmp_path / "state"),
               KILO_CONFIG=str(config), KILO_SERVER_PASSWORD=password, KILO_SERVER_USERNAME="synthetic",
               KILO_DISABLE_DEFAULT_PLUGINS="true", BUN_INSTALL_CACHE_DIR=str(tmp_path / "bun-cache"))
    with socket.socket() as reserved:
        reserved.bind(("127.0.0.1", 0))
        port = reserved.getsockname()[1]
    child = subprocess.Popen([BINARY, "serve", "--hostname", "127.0.0.1", "--port", str(port)],
                             cwd=workspace, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    auth = "Basic " + base64.b64encode(f"synthetic:{password}".encode()).decode()
    base = f"http://127.0.0.1:{port}"

    def request(route, method="GET"):
        req = urllib.request.Request(base + route, method=method, headers={"Authorization": auth})
        with urllib.request.urlopen(req, timeout=15) as response:
            return response.status, response.read(2 * 1024 * 1024)

    try:
        # Never mutate an endpoint until this exact self-created process owns the port.
        deadline = time.monotonic() + 45
        while time.monotonic() < deadline:
            assert child.poll() is None, "isolated Kilo canary exited during startup"
            ports = psutil.Process(child.pid).net_connections(kind="inet")
            if any(item.status == psutil.CONN_LISTEN and item.laddr.port == port for item in ports):
                break
            time.sleep(0.05)
        else:
            pytest.fail("isolated Kilo canary did not bind its test port")
        status, health = request("/global/health")
        assert status == 200 and json.loads(health)["version"] == "7.8.18"
        directory = urllib.parse.quote(str(workspace), safe="")
        status, _ = request("/provider?directory=" + directory)
        assert status == 200
        journal_dir = tmp_path / "data/kilo/lifecycle"
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and not list(journal_dir.glob("*.jsonl")):
            time.sleep(0.05)
        paths = list(journal_dir.glob("*.jsonl"))
        assert paths, "real runtime did not initialize the configured plugin"
        rows = [json.loads(line) for path in paths for line in path.read_text(encoding="utf-8").splitlines()]
        started = [row for row in rows if row["event"] == "plugin_started"]
        assert started and all(row["pid"] == child.pid and row["birth"] for row in started)
        assert all(row["pre_kill"] is False for row in started)
        diagnostics = runpy.run_path(str(ROOT / "engine/harness_diagnostics.py"))
        coverage = diagnostics["read_coverage"](journal_dir, owner_pid=child.pid)
        assert coverage["plugin"] == "active"
        assert coverage["owner"] == "unavailable" and coverage["pre_kill"] is False
        status, _ = request("/instance/dispose?directory=" + directory, "POST")
        assert status == 200
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            rows = [json.loads(line) for path in paths for line in path.read_text(encoding="utf-8").splitlines()]
            if any(row["event"] == "plugin_disposed" for row in rows):
                break
            time.sleep(0.05)
        assert any(row["event"] == "plugin_disposed" for row in rows)
        assert password not in json.dumps(rows)
    finally:
        child.kill()
        child.communicate(timeout=15)

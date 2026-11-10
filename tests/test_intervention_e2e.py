from __future__ import annotations

import json
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from http.client import HTTPConnection
from pathlib import Path

import pytest

from agent_control.dashboard import DashboardApp, create_server
from agent_control.liveness import PsutilProcessProbe
from agent_control.paths import AppPaths
from agent_control.storage import StateLocks
from agent_control.sync import Synchronizer

CSRF = "tok"


def _clock() -> datetime:
    return datetime.now(timezone.utc)


def _get(port: int, path: str, headers: dict | None = None):
    conn = HTTPConnection("127.0.0.1", port)
    hdrs = {"Host": f"127.0.0.1:{port}"}
    if headers:
        hdrs.update(headers)
    conn.request("GET", path, headers=hdrs)
    response = conn.getresponse()
    data = response.read()
    conn.close()
    return response.status, data


def _post(port: int, path: str, body: dict):
    conn = HTTPConnection("127.0.0.1", port)
    headers = {
        "Host": f"127.0.0.1:{port}",
        "Origin": f"http://127.0.0.1:{port}",
        "X-AgentControl-CSRF": CSRF,
        "Content-Type": "application/json",
    }
    conn.request("POST", path, body=json.dumps(body), headers=headers)
    response = conn.getresponse()
    data = response.read()
    conn.close()
    return response.status, data


def _narration(port: int, run_id: str, after: int = 0):
    status, data = _get(
        port,
        f"/api/narration?run_id={run_id}&after={after}",
        {"X-AgentControl-CSRF": CSRF},
    )
    assert status == 200, data
    return json.loads(data)


def test_steer_then_kill_end_to_end(tmp_path: Path) -> None:
    psutil = pytest.importorskip("psutil")

    # Copy the fake engine to a path whose command line satisfies the
    # dashboard's process-identity check.
    engine_path = tmp_path / ".claude" / "bin" / "delegate.py"
    engine_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy(Path(__file__).parent / "fake_engine.py", engine_path)

    proj = tmp_path / "proj"
    transcript = proj / ".claude" / "agent-runs" / "20260924-000000-e2e.md"
    transcript.parent.mkdir(parents=True, exist_ok=True)

    paths = AppPaths.for_test(tmp_path)
    paths.state_dir.mkdir(parents=True, exist_ok=True)
    locks = StateLocks()
    synchronizer = Synchronizer(
        paths, locks, _clock, PsutilProcessProbe(), min_interval_seconds=0.0
    )
    app = DashboardApp(paths, locks, synchronizer, _clock)
    httpd = create_server(app, port=0, csrf_token=CSRF)
    port = httpd.server_address[1]
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()

    child_pid_file = tmp_path / "child.pid"
    engine = subprocess.Popen([
        sys.executable, str(engine_path),
        "--index", str(paths.machine_index),
        "--transcript", str(transcript),
        "--root", str(proj),
        "--label", "e2e",
        "--turns", "400",
        "--turn-seconds", "0.3",
        "--child-pid-file", str(child_pid_file),
    ])

    try:
        # 3. Wait until the run is live and controllable.
        run_id = None
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            status, data = _get(port, "/api/data")
            if status == 200:
                for row in json.loads(data).get("live", []):
                    if row.get("can_steer") and row.get("can_kill"):
                        run_id = row["run_id"]
                        break
            if run_id:
                break
            time.sleep(0.2)
        assert run_id is not None, "no controllable live run appeared"

        # 4. Narration total must grow between two reads ~1s apart.
        first = _narration(port, run_id)
        time.sleep(1.0)
        second = _narration(port, run_id)
        assert second["total"] > first["total"]

        # 5. Steer, then wait for delivery.
        guidance_text = "please focus on file 3"
        status, data = _post(
            port, f"/api/runs/{run_id}/steer", {"text": guidance_text}
        )
        assert status == 200, data
        assert json.loads(data)["state"] == "queued"

        delivered = False
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            payload = _narration(port, run_id)
            steer = payload.get("steer") or []
            if steer and steer[0]["state"] == "delivered":
                assert any(
                    e.get("kind") == "guidance"
                    and guidance_text in json.dumps(e)
                    for e in payload["events"]
                )
                delivered = True
                break
            time.sleep(0.2)
        assert delivered, "steer was never delivered"

        # 6. Kill.
        child_pid = int(child_pid_file.read_text(encoding="utf-8").strip())
        status, data = _post(port, f"/api/runs/{run_id}/kill", {"confirm": True})
        assert status == 200, data
        kill_payload = json.loads(data)
        assert kill_payload["outcome"] == "terminated"
        assert kill_payload["children"] >= 1
        engine_pid = kill_payload["engine_pid"]

        # 7. Wait for the run to appear ended, killed by operator.
        ended = False
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            status, data = _get(port, "/api/data")
            if status == 200:
                for row in json.loads(data).get("recently_ended", []):
                    if (
                        row.get("run_id") == run_id
                        and row.get("end_reason") == "killed by operator"
                    ):
                        ended = True
                        break
            if ended:
                break
            time.sleep(0.2)
        assert ended, "run never showed as killed by operator"

        # 8. Neither process may survive.
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and (
            psutil.pid_exists(engine_pid) or psutil.pid_exists(child_pid)
        ):
            time.sleep(0.2)
        assert not psutil.pid_exists(engine_pid)
        assert not psutil.pid_exists(child_pid)

        # 9. No state file may leak transcript or steer content.
        for path in paths.state_dir.rglob("*"):
            if path.is_file():
                blob = path.read_bytes()
                assert b"MARKER-NARRATION-XYZ" not in blob, path
                assert b"please focus on file 3" not in blob, path

        # 10. Operator log has exactly steer then kill, with no text.
        log = (paths.state_dir / "operator-actions.jsonl").read_text(
            encoding="utf-8"
        )
        lines = [ln for ln in log.splitlines() if ln.strip()]
        assert len(lines) == 2
        records = [json.loads(ln) for ln in lines]
        assert [r["action"] for r in records] == ["steer", "kill"]
        for record in records:
            assert "text" not in record
    finally:
        _cleanup(engine, child_pid_file, psutil)
        httpd.shutdown()


def _cleanup(engine: subprocess.Popen, child_pid_file: Path, psutil) -> None:
    try:
        if engine.poll() is None:
            engine.terminate()
    except Exception:
        pass
    try:
        child_pid = int(child_pid_file.read_text(encoding="utf-8").strip())
        if psutil.pid_exists(child_pid):
            psutil.Process(child_pid).terminate()
    except Exception:
        pass

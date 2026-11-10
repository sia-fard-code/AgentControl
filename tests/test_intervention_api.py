from __future__ import annotations

import json
import threading
from dataclasses import replace
from datetime import datetime, timezone
from http.client import HTTPConnection
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from agent_control.dashboard import DashboardApp, create_server
from agent_control.ledger import sync_candidate
from agent_control.models import Liveness, SyncReport
from agent_control.operator_log import append_action, read_actions
from agent_control.paths import AppPaths
from agent_control.run_index import StartEvent
from agent_control.snapshot import SnapshotQuery
from agent_control.storage import StateLocks
from agent_control.sync import OpenRunObservation
from tests.fake_psutil import FakeProcess, FakePsutil
from tests.test_ledger import run_candidate

CSRF = "test-csrf-token"
START_TS = "2026-09-17T11:59:00+00:00"
ENGINE_CMDLINE = ["python", "C:\\Users\\x\\.claude\\bin\\delegate.py"]


def _clock() -> datetime:
    return datetime(2026, 9, 17, 12, 0, tzinfo=timezone.utc)


def _transcript(task: str = "do the thing") -> str:
    return "\n".join([
        "# lbl",
        "- model: `m`  effort: `medium`  tools: `native`",
        "- writes: `disk`  root: `R`",
        "## Task",
        "",
        task,
        "",
        "---",
        "",
        "_[ 1] calling m (attempt 1/4, 2 msgs, ~9 bytes) ..._",
        "### [1] thinking",
        "reasoning for turn one",
        '### [1] read_file({"path": "a.py"})',
        "```",
        "file output",
        "```",
        "_[ 2] calling m (attempt 1/4, 2 msgs, ~9 bytes) ..._",
        "### [2] thinking",
        "reasoning for turn two",
        '### [2] read_file({"path": "a.py"})',
        "```",
        "second output",
        "```",
    ]) + "\n"


@pytest.fixture
def app(tmp_path: Path) -> DashboardApp:
    paths = AppPaths.for_test(tmp_path)
    sync = MagicMock()
    sync.run.return_value = SyncReport(0, 0, 0, 0, 0, False, ())
    sync.open_runs = {}
    return DashboardApp(paths, StateLocks(), sync, _clock)


def _make_app(tmp_path: Path, psutil_module) -> DashboardApp:
    paths = AppPaths.for_test(tmp_path)
    sync = MagicMock()
    sync.run.return_value = SyncReport(0, 0, 0, 0, 0, False, ())
    sync.open_runs = {}
    return DashboardApp(paths, StateLocks(), sync, _clock, psutil_module=psutil_module)


@pytest.fixture
def server(app: DashboardApp):
    httpd = create_server(app, port=0, csrf_token=CSRF)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield httpd
    httpd.shutdown()


def _serve(app: DashboardApp):
    httpd = create_server(app, port=0, csrf_token=CSRF)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    return httpd


def _port(server) -> int:
    return server.server_address[1]


def _post(server, path: str, body: dict):
    conn = HTTPConnection("127.0.0.1", _port(server))
    headers = {
        "Host": f"127.0.0.1:{_port(server)}",
        "Origin": f"http://127.0.0.1:{_port(server)}",
        "X-AgentControl-CSRF": CSRF,
        "Content-Type": "application/json",
    }
    conn.request("POST", path, body=json.dumps(body), headers=headers)
    response = conn.getresponse()
    data = response.read()
    conn.close()
    return response.status, data


def _seed(app: DashboardApp, run_id: str, transcript, status: str = "running") -> None:
    sync_candidate(
        app.paths.ledger,
        replace(
            run_candidate(run_id=run_id, status=status),
            transcript_artifact=str(transcript),
        ),
        observed_at="2026-09-17T12:00:00+00:00",
        locks=app.locks,
    )


def _start(run_id: str, transcript, pid: int) -> StartEvent:
    return StartEvent(
        schema=2, run_id=run_id, ts=START_TS, harness_version=6, root="R",
        cwd="R", label="lbl", model="m", effort=None, tool_mode="native",
        write_mode="disk", engine_pid=pid, transcript=str(transcript),
        messages="", usage_journal="", max_iters=40,
    )


def _observe(app: DashboardApp, run_id: str, transcript, *,
             code: str = "process_identity_valid", pid: int = 4321) -> None:
    app.synchronizer.open_runs[run_id] = OpenRunObservation(
        run_id=run_id,
        liveness=Liveness(display="running", persist_status="running",
                          terminal=False, inferred=False, code=code),
        engine_pid=pid, max_iters=40, transcript=str(transcript),
        messages=None, usage_journal=None,
        observed_at="2026-09-17T12:00:00+00:00",
        start=_start(run_id, transcript, pid),
    )


def _write(app: DashboardApp, name: str, text: str) -> Path:
    app.paths.state_dir.mkdir(parents=True, exist_ok=True)
    path = app.paths.state_dir / name
    path.write_text(text, encoding="utf-8")
    return path


def test_steer_appends_and_logs_metadata(app: DashboardApp, server) -> None:
    transcript = _write(app, "r.md", _transcript())
    _seed(app, "r", transcript)
    _observe(app, "r", transcript)
    status, data = _post(server, "/api/runs/r/steer", {"text": "please focus"})
    assert status == 200
    steer_text = Path(str(transcript) + ".steer").read_text(encoding="utf-8")
    assert steer_text.rstrip().endswith("please focus")

    actions = read_actions(app.paths.state_dir)
    steers = [a for a in actions if a["action"] == "steer"]
    assert len(steers) == 1
    log_lines = (app.paths.state_dir / "operator-actions.jsonl").read_text(
        encoding="utf-8"
    )
    assert "please focus" not in log_lines


def test_steer_preconditions(app: DashboardApp, server) -> None:
    transcript = _write(app, "r.md", _transcript())
    _seed(app, "r", transcript)

    # not_open: no observation yet
    status, data = _post(server, "/api/runs/r/steer", {"text": "hi"})
    assert status == 409
    assert json.loads(data)["error"]["code"] == "not_open"

    # not_verified
    _observe(app, "r", transcript, code="probe_unknown:x")
    status, data = _post(server, "/api/runs/r/steer", {"text": "hi"})
    assert status == 409
    assert json.loads(data)["error"]["code"] == "not_verified"

    # invalid_text (empty)
    _observe(app, "r", transcript)
    status, data = _post(server, "/api/runs/r/steer", {"text": "   "})
    assert status == 400
    assert json.loads(data)["error"]["code"] == "invalid_text"

    # no_transcript: observation points at a missing file
    missing = app.paths.state_dir / "gone.md"
    _observe(app, "r", missing)
    status, data = _post(server, "/api/runs/r/steer", {"text": "hi"})
    assert status == 409
    assert json.loads(data)["error"]["code"] == "no_transcript"

    # invalid_request: both text and kind
    _observe(app, "r", transcript)
    status, data = _post(
        server, "/api/runs/r/steer", {"text": "hi", "kind": "wrap_up"}
    )
    assert status == 400
    assert json.loads(data)["error"]["code"] == "invalid_request"


def test_wrap_up_uses_canned_text(app: DashboardApp, server) -> None:
    from agent_control import steering

    transcript = _write(app, "r.md", _transcript())
    _seed(app, "r", transcript)
    _observe(app, "r", transcript)
    status, _ = _post(server, "/api/runs/r/steer", {"kind": "wrap_up"})
    assert status == 200
    steer_text = Path(str(transcript) + ".steer").read_text(encoding="utf-8")
    assert steering.WRAP_UP_TEXT in steer_text


def test_steer_delivered_status(app: DashboardApp, server) -> None:
    transcript = _write(app, "r.md", _transcript())
    _seed(app, "r", transcript)
    _observe(app, "r", transcript)
    status, data = _post(server, "/api/runs/r/steer", {"text": "please focus"})
    assert status == 200
    steer_id = json.loads(data)["id"]

    # simulate delivery: guidance appears in transcript, steer file consumed
    with open(transcript, "a", encoding="utf-8") as fh:
        fh.write("\n### [5] OPERATOR GUIDANCE\n\n> please focus\n")
    Path(str(transcript) + ".steer").write_text("", encoding="utf-8")

    conn = HTTPConnection("127.0.0.1", _port(server))
    conn.request("GET", "/api/narration?run_id=r",
                 headers={"Host": f"127.0.0.1:{_port(server)}",
                          "X-AgentControl-CSRF": CSRF})
    resp = conn.getresponse()
    payload = json.loads(resp.read())
    conn.close()
    steer = payload["steer"]
    assert steer[0]["id"] == steer_id
    assert steer[0]["state"] == "delivered"
    assert steer[0]["turn"] == 5


def test_cancel_steer(app: DashboardApp, server) -> None:
    transcript = _write(app, "r.md", _transcript())
    _seed(app, "r", transcript)
    _observe(app, "r", transcript)
    status, data = _post(server, "/api/runs/r/steer", {"text": "please focus"})
    steer_id = json.loads(data)["id"]

    status, data = _post(
        server, "/api/runs/r/steer/cancel", {"id": steer_id}
    )
    assert status == 200
    assert json.loads(data)["state"] == "cancelled"
    steer_text = Path(str(transcript) + ".steer").read_text(encoding="utf-8")
    assert "please focus" not in steer_text

    status, data = _post(
        server, "/api/runs/r/steer/cancel", {"id": steer_id}
    )
    assert status == 409
    assert json.loads(data)["error"]["code"] == "not_pending"


def test_kill_requires_confirm(app: DashboardApp, server) -> None:
    transcript = _write(app, "r.md", _transcript())
    _seed(app, "r", transcript)
    _observe(app, "r", transcript)
    status, data = _post(server, "/api/runs/r/kill", {})
    assert status == 400
    assert json.loads(data)["error"]["code"] == "confirm_required"


def _engine_proc(pid: int, child_pids=()) -> FakeProcess:
    return FakeProcess(
        pid, ENGINE_CMDLINE,
        datetime.fromisoformat(START_TS).timestamp(),
        child_pids=child_pids,
    )


def test_kill_terminates_fake_tree(tmp_path: Path) -> None:
    engine = _engine_proc(4321, child_pids=(9001,))
    child = FakeProcess(9001, ["python", "worker"], 0.0)
    psu = FakePsutil([engine, child])
    app = _make_app(tmp_path, psu)
    transcript = _write(app, "r.md", _transcript())
    _seed(app, "r", transcript)
    _observe(app, "r", transcript)
    server = _serve(app)
    try:
        status, data = _post(server, "/api/runs/r/kill", {"confirm": True})
    finally:
        server.shutdown()
    assert status == 200
    payload = json.loads(data)
    assert payload["outcome"] == "terminated"
    assert payload["children"] == 1
    actions = read_actions(app.paths.state_dir)
    kills = [a for a in actions if a["action"] == "kill"]
    assert kills[0]["engine_pid"] == 4321
    assert kills[0]["children"] == 1


def test_kill_refuses_recycled_pid(tmp_path: Path) -> None:
    # A recycled pid belongs to a process created after the engine died, so its
    # create time falls outside the start-time window.
    engine = FakeProcess(
        4321, ["python", "not-the-engine"],
        datetime.fromisoformat(START_TS).timestamp() + 600,
    )
    psu = FakePsutil([engine])
    app = _make_app(tmp_path, psu)
    transcript = _write(app, "r.md", _transcript())
    _seed(app, "r", transcript)
    _observe(app, "r", transcript)
    server = _serve(app)
    try:
        status, data = _post(server, "/api/runs/r/kill", {"confirm": True})
    finally:
        server.shutdown()
    assert status == 409
    assert json.loads(data)["error"]["code"] == "not_verified"
    assert not any(c.startswith("terminate:") for c in psu.calls)


def test_second_kill_reports_already_gone(tmp_path: Path) -> None:
    engine = _engine_proc(4321)
    psu = FakePsutil([engine])
    app = _make_app(tmp_path, psu)
    transcript = _write(app, "r.md", _transcript())
    _seed(app, "r", transcript)
    _observe(app, "r", transcript)
    server = _serve(app)
    try:
        status, _ = _post(server, "/api/runs/r/kill", {"confirm": True})
        assert status == 200
        status, data = _post(server, "/api/runs/r/kill", {"confirm": True})
    finally:
        server.shutdown()
    assert status == 200
    assert json.loads(data)["outcome"] == "already_gone"


def test_kill_not_open_and_no_identity(tmp_path: Path) -> None:
    psu = FakePsutil([])
    app = _make_app(tmp_path, psu)
    transcript = _write(app, "r.md", _transcript())
    _seed(app, "r", transcript)
    server = _serve(app)
    try:
        status, data = _post(server, "/api/runs/r/kill", {"confirm": True})
        assert status == 409
        assert json.loads(data)["error"]["code"] == "not_open"

        app.synchronizer.open_runs["r"] = OpenRunObservation(
            run_id="r",
            liveness=Liveness(display="running", persist_status="running",
                              terminal=False, inferred=False,
                              code="process_identity_valid"),
            engine_pid=None, max_iters=40, transcript=str(transcript),
            messages=None, usage_journal=None,
            observed_at="2026-09-17T12:00:00+00:00", start=None,
        )
        status, data = _post(server, "/api/runs/r/kill", {"confirm": True})
    finally:
        server.shutdown()
    assert status == 409
    assert json.loads(data)["error"]["code"] == "no_process_identity"


def test_snapshot_passes_operator_actions(app: DashboardApp) -> None:
    transcript = _write(app, "r.md", _transcript())
    _seed(app, "r", transcript, status="interrupted")
    append_action(
        app.paths.state_dir, app.locks.write, ts="2026-09-17T12:00:00+00:00",
        action="kill", run_id="r", outcome="terminated", engine_pid=4321,
        children=1, survivors=0,
    )
    snapshot = app.snapshot(SnapshotQuery())
    ended = snapshot["recently_ended"]
    match = [r for r in ended if r["run_id"] == "r"]
    assert match and match[0]["end_reason"] == "killed by operator"

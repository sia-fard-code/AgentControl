from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from http.client import HTTPConnection
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from agent_control.dashboard import (
    ApiError,
    DashboardApp,
    _query_from_params,
    create_server,
    make_handler,
)
from agent_control.models import SyncReport
from agent_control.paths import AppPaths
from agent_control.projects import ProjectRegistry
from agent_control.snapshot import SnapshotQuery
from agent_control.storage import StateLocks

CSRF = "test-csrf-token"


def _clock() -> datetime:
    return datetime(2026, 9, 17, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def app(tmp_path: Path) -> DashboardApp:
    paths = AppPaths.for_test(tmp_path)
    synchronizer = MagicMock()
    synchronizer.run.return_value = SyncReport(0, 0, 0, 0, 0, False, ())
    return DashboardApp(paths, StateLocks(), synchronizer, _clock)


@pytest.fixture
def server(app: DashboardApp):
    httpd = create_server(app, port=0, csrf_token=CSRF)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield httpd
    httpd.shutdown()


def _host(server) -> str:
    return f"127.0.0.1:{server.server_address[1]}"


def _request(server, method: str, path: str, *, headers=None, body=None):
    conn = HTTPConnection("127.0.0.1", server.server_address[1])
    conn.request(method, path, body=body, headers=headers or {})
    response = conn.getresponse()
    data = response.read()
    conn.close()
    return response.status, data


class _ResponseSocket:
    """Exercise the real HTTP handler with deterministic transport failures."""

    def __init__(self, request: bytes, *, error=None, fail_write=0):
        self.incoming = BytesIO(request)
        self.writes: list[bytes] = []
        self.error = error
        self.fail_write = fail_write

    def makefile(self, *args):
        return self.incoming

    def sendall(self, data):
        self.writes.append(bytes(data))
        if len(self.writes) == self.fail_write:
            raise self.error


@pytest.mark.parametrize("error_type", [
    ConnectionAbortedError, ConnectionResetError, BrokenPipeError,
])
@pytest.mark.parametrize("method,path,fail_write", [
    ("GET", "/api/projects", 1),
    ("GET", "/api/projects", 2),
    ("GET", "/not-found", 1),
    ("GET", "/not-found", 2),
    ("GET", "/static/dashboard.js", 1),
    ("GET", "/static/dashboard.js", 2),
    ("PUT", "/api/projects", 1),
])
def test_client_disconnect_closes_request_without_traceback_or_retry(
    app, monkeypatch, error_type, fail_write, method, path,
):
    web = app.paths.repo_root / "web"
    web.mkdir(parents=True)
    (web / "dashboard.js").write_text("// synthetic fixture\n", encoding="utf-8")
    monkeypatch.setattr(app, "_web_dir", lambda: web)
    request = f"{method} {path} HTTP/1.1\r\nHost: 127.0.0.1:12345\r\n\r\n".encode()
    connection = _ResponseSocket(
        request * 2, error=error_type(10053, "synthetic client disconnect"),
        fail_write=fail_write,
    )
    handler = make_handler(app, CSRF)(
        connection, ("127.0.0.1", 23456),
        SimpleNamespace(server_address=("127.0.0.1", 12345)),
    )
    assert handler.close_connection is True
    assert len(connection.writes) == fail_write


def test_response_write_does_not_hide_unrelated_os_errors(app):
    connection = _ResponseSocket(
        b"GET /api/projects HTTP/1.1\r\nHost: 127.0.0.1:12345\r\n\r\n",
        error=OSError(5, "synthetic I/O failure"), fail_write=2,
    )
    with pytest.raises(OSError, match="synthetic I/O failure"):
        make_handler(app, CSRF)(
            connection, ("127.0.0.1", 23456),
            SimpleNamespace(server_address=("127.0.0.1", 12345)),
        )


def test_aborted_post_response_does_not_repeat_committed_mutation(app):
    project, _ = app.registry.discover(app.paths.repo_root)
    body = b'{"display_name":"Synthetic renamed project"}'
    request = (
        f"POST /api/projects/{project.project_id}/rename HTTP/1.1\r\n"
        "Host: 127.0.0.1:12345\r\nOrigin: http://127.0.0.1:12345\r\n"
        f"X-AgentControl-CSRF: {CSRF}\r\nContent-Type: application/json\r\n"
        f"Content-Length: {len(body)}\r\n\r\n"
    ).encode() + body
    connection = _ResponseSocket(
        request * 2, error=ConnectionAbortedError(10053, "synthetic disconnect"),
        fail_write=2,
    )
    make_handler(app, CSRF)(
        connection, ("127.0.0.1", 23456),
        SimpleNamespace(server_address=("127.0.0.1", 12345)),
    )
    events = [json.loads(line) for line in app.paths.projects.read_text().splitlines()]
    assert [event["event"] for event in events] == ["discovered", "renamed"]


def test_server_binds_only_to_loopback(server) -> None:
    assert server.server_address[0] == "127.0.0.1"


def test_data_endpoint_synchronizes_before_snapshot(app: DashboardApp) -> None:
    payload = app.snapshot(SnapshotQuery())
    app.synchronizer.run.assert_called_once_with()
    assert "normal_totals" in payload


def test_get_data_returns_json(server) -> None:
    status, data = _request(server, "GET", "/api/data")
    assert status == 200
    payload = json.loads(data)
    assert "normal_totals" in payload


def test_activity_requires_run_id(server) -> None:
    status, _ = _request(server, "GET", "/api/activity")
    assert status == 400


def test_activity_unknown_run_is_404(server) -> None:
    status, _ = _request(server, "GET", "/api/activity?run_id=does-not-exist")
    assert status == 404


def test_activity_returns_sanitized_timeline(app: DashboardApp, server) -> None:
    from dataclasses import replace

    from agent_control.ledger import sync_candidate
    from tests.test_ledger import run_candidate

    transcript = app.paths.state_dir / "run.md"
    transcript.parent.mkdir(parents=True, exist_ok=True)
    transcript.write_text(
        "### [1] thinking\nSECRET REASONING\n"
        '### [2] read_file({"path": "AGENTS.md"})\n'
        '### [3] write_file({"path": "x.py", "content": "SECRET BODY"})\n',
        encoding="utf-8",
    )
    candidate = replace(
        run_candidate(run_id="live-1", model="gpt-5.6-sol"),
        transcript_artifact=str(transcript),
    )
    sync_candidate(
        app.paths.ledger, candidate, observed_at="2026-09-17T12:00:00+00:00", locks=app.locks
    )

    status, data = _request(server, "GET", "/api/activity?run_id=live-1")
    assert status == 200
    payload = json.loads(data)
    assert payload["tool_count"] == 2
    tools = [s["tool"] for s in payload["steps"] if s["phase"] == "tool"]
    assert tools == ["read_file", "write_file"]
    body = json.dumps(payload)
    assert "SECRET REASONING" not in body
    assert "SECRET BODY" not in body


def test_activity_exposes_real_step_start_times(app: DashboardApp, server) -> None:
    from dataclasses import replace

    from agent_control.ledger import sync_candidate
    from tests.test_ledger import run_candidate

    transcript = app.paths.state_dir / "timed.md"
    transcript.parent.mkdir(parents=True, exist_ok=True)
    transcript.write_text(
        "### [1] thinking\n"
        '### [1] read_file({"path": "a"})\n'
        '### [2] read_file({"path": "b"})\n',
        encoding="utf-8",
    )
    journal = transcript.with_suffix(".usage.jsonl")
    journal.write_text(
        '{"iter":1,"ts":"2026-09-17T12:00:00Z","sent":100,"recv":10,"cache_read":0,"cache_write":0}\n'
        '{"iter":2,"ts":"2026-09-17T12:00:30Z","sent":200,"recv":20,"cache_read":0,"cache_write":0}\n',
        encoding="utf-8",
    )
    candidate = replace(
        run_candidate(run_id="timed-1", model="gpt-5.6-sol", status="running"),
        transcript_artifact=str(transcript),
    )
    sync_candidate(
        app.paths.ledger, candidate, observed_at="2026-09-17T12:00:00+00:00", locks=app.locks
    )

    status, data = _request(server, "GET", "/api/activity?run_id=timed-1")
    payload = json.loads(data)
    # The server reports its own clock so the client can compute true elapsed.
    assert "server_now" in payload
    # Each iteration's first step carries its real start timestamp.
    starts = {s["iteration"]: s.get("started_at") for s in payload["steps"] if s.get("started_at")}
    assert starts[1] == "2026-09-17T12:00:00+00:00"
    assert starts[2] == "2026-09-17T12:00:30+00:00"
    # The live current step's real start is surfaced for the running timer.
    assert payload["current_started_at"] == "2026-09-17T12:00:30+00:00"


def test_activity_flags_inaccessible_artifact(app: DashboardApp, server) -> None:
    from dataclasses import replace

    from agent_control.ledger import sync_candidate
    from tests.test_ledger import run_candidate

    # Point at a transcript path that does not exist on disk (ephemeral run).
    missing = app.paths.state_dir / "gone" / "run.md"
    candidate = replace(
        run_candidate(run_id="ghost-1", model="gpt-5.6-sol", status="running"),
        transcript_artifact=str(missing),
    )
    sync_candidate(
        app.paths.ledger, candidate, observed_at="2026-09-17T12:00:00+00:00", locks=app.locks
    )

    status, data = _request(server, "GET", "/api/activity?run_id=ghost-1")
    payload = json.loads(data)
    assert status == 200
    assert payload["artifact_path_recorded"] is True
    assert payload["artifact_accessible"] is False
    assert payload["step_count"] == 0


def test_activity_falls_back_to_archive_when_transcript_gone(
    app: DashboardApp, server
) -> None:
    from dataclasses import replace

    from agent_control.activity import build_activity_snapshot, write_activity_archive
    from agent_control.ledger import sync_candidate
    from tests.test_ledger import run_candidate

    transcript = app.paths.state_dir / "arch.md"
    transcript.parent.mkdir(parents=True, exist_ok=True)
    transcript.write_text(
        "### [1] thinking\n"
        '### [1] read_file({"path": "AGENTS.md"})\n'
        '### [2] write_file({"path": "y.py", "content": "SECRET"})\n',
        encoding="utf-8",
    )
    transcript.with_suffix(".usage.jsonl").write_text(
        '{"iter":1,"ts":"2026-09-18T10:00:00Z","sent":100,"recv":10,"cache_read":0,"cache_write":0}\n'
        '{"iter":2,"ts":"2026-09-18T10:00:12Z","sent":300,"recv":20,"cache_read":0,"cache_write":0}\n',
        encoding="utf-8",
    )
    candidate = replace(
        run_candidate(run_id="arch-1", model="gpt-5.6-sol", status="interrupted"),
        transcript_artifact=str(transcript),
    )
    sync_candidate(
        app.paths.ledger, candidate, observed_at="2026-09-18T10:05:00+00:00", locks=app.locks
    )
    # archive it, then delete the live transcript to simulate cleanup
    write_activity_archive(
        app.paths.activity_archive("arch-1"), build_activity_snapshot(transcript)
    )
    transcript.unlink()
    transcript.with_suffix(".usage.jsonl").unlink()

    status, data = _request(server, "GET", "/api/activity?run_id=arch-1")
    payload = json.loads(data)
    assert status == 200
    assert payload["artifact_accessible"] is False
    assert payload["from_archive"] is True
    tools = [s["tool"] for s in payload["steps"] if s["phase"] == "tool"]
    assert tools == ["read_file", "write_file"]
    assert "SECRET" not in json.dumps(payload)


def test_dismiss_run_marks_open_run_interrupted(app: DashboardApp, server) -> None:
    from agent_control.ledger import highest_valid_revisions, load_ledger, sync_candidate
    from tests.test_ledger import run_candidate

    sync_candidate(
        app.paths.ledger,
        run_candidate(run_id="stuck", status="running", tokens_sent=500),
        observed_at="2026-09-17T12:00:00+00:00",
        locks=app.locks,
    )
    host = f"127.0.0.1:{server.server_address[1]}"
    status, data = _request(
        server,
        "POST",
        "/api/runs/stuck/dismiss",
        headers={
            "Host": host,
            "Origin": f"http://{host}",
            "Content-Type": "application/json",
            "X-AgentControl-CSRF": CSRF,
        },
        body="{}",
    )
    assert status == 200
    assert json.loads(data)["status"] == "interrupted"
    current = highest_valid_revisions(load_ledger(app.paths.ledger)[0])["stuck"]
    assert current.status == "interrupted"
    assert current.tokens_sent == 500  # usage preserved


def test_dismiss_run_refuses_verified_alive_engine(
    app: DashboardApp, server
) -> None:
    from agent_control.ledger import highest_valid_revisions, load_ledger, sync_candidate
    from agent_control.models import Liveness
    from agent_control.sync import OpenRunObservation
    from tests.test_ledger import run_candidate

    sync_candidate(
        app.paths.ledger,
        run_candidate(run_id="alive", status="running", tokens_sent=500),
        observed_at="2026-09-17T12:00:00+00:00",
        locks=app.locks,
    )
    app.synchronizer.open_runs = {
        "alive": OpenRunObservation(
            run_id="alive",
            liveness=Liveness(
                display="running",
                persist_status="running",
                terminal=False,
                inferred=False,
                code="process_identity_valid",
                silence_seconds=30.0,
                last_activity_at="2026-09-17T11:59:30+00:00",
            ),
            engine_pid=4321,
            max_iters=40,
            transcript=None,
            messages=None,
            usage_journal=None,
            observed_at="2026-09-17T11:59:30+00:00",
        )
    }
    host = f"127.0.0.1:{server.server_address[1]}"
    status, data = _request(
        server,
        "POST",
        "/api/runs/alive/dismiss",
        headers={
            "Host": host,
            "Origin": f"http://{host}",
            "Content-Type": "application/json",
            "X-AgentControl-CSRF": CSRF,
        },
        body="{}",
    )
    assert status == 409
    assert json.loads(data)["error"]["code"] == "engine_alive"
    current = highest_valid_revisions(load_ledger(app.paths.ledger)[0])["alive"]
    assert current.status == "running"


def test_data_returns_verified_live_run(app: DashboardApp, server) -> None:
    from agent_control.ledger import sync_candidate
    from agent_control.models import Liveness
    from agent_control.sync import OpenRunObservation
    from tests.test_ledger import run_candidate

    sync_candidate(
        app.paths.ledger,
        run_candidate(run_id="alive-1", status="running", tokens_sent=100),
        observed_at="2026-09-17T12:00:00+00:00",
        locks=app.locks,
    )
    app.synchronizer.open_runs = {
        "alive-1": OpenRunObservation(
            run_id="alive-1",
            liveness=Liveness(
                display="running",
                persist_status="running",
                terminal=False,
                inferred=False,
                code="process_identity_valid",
                silence_seconds=30.0,
                last_activity_at="2026-09-17T11:59:30+00:00",
            ),
            engine_pid=4321,
            max_iters=40,
            transcript=None,
            messages=None,
            usage_journal=None,
            observed_at="2026-09-17T11:59:30+00:00",
        )
    }
    status, data = _request(server, "GET", "/api/data")
    assert status == 200
    payload = json.loads(data)
    live = {row["run_id"]: row for row in payload["live"]}
    assert "alive-1" in live
    assert live["alive-1"]["verified"] is True


def test_unknown_path_returns_404(server) -> None:
    status, _ = _request(server, "GET", "/api/nope")
    assert status == 404


def test_invalid_method_returns_405(server) -> None:
    status, _ = _request(server, "DELETE", "/api/data")
    assert status == 405


def test_mutation_requires_matching_host(server) -> None:
    status, _ = _request(
        server,
        "POST",
        "/api/pricing/preview",
        headers={
            "Host": "evil.example",
            "Origin": f"http://{_host(server)}",
            "Content-Type": "application/json",
            "X-AgentControl-CSRF": CSRF,
        },
        body="{}",
    )
    assert status == 400


def test_mutation_requires_same_origin(server) -> None:
    status, _ = _request(
        server,
        "POST",
        "/api/pricing/preview",
        headers={
            "Host": _host(server),
            "Origin": "http://evil.example",
            "Content-Type": "application/json",
            "X-AgentControl-CSRF": CSRF,
        },
        body="{}",
    )
    assert status == 403


def test_mutation_requires_csrf_token(server) -> None:
    status, _ = _request(
        server,
        "POST",
        "/api/pricing/preview",
        headers={
            "Host": _host(server),
            "Origin": f"http://{_host(server)}",
            "Content-Type": "application/json",
        },
        body="{}",
    )
    assert status == 403


def test_mutation_requires_json_content_type(server) -> None:
    status, _ = _request(
        server,
        "POST",
        "/api/pricing/preview",
        headers={
            "Host": _host(server),
            "Origin": f"http://{_host(server)}",
            "Content-Type": "text/plain",
            "X-AgentControl-CSRF": CSRF,
        },
        body="{}",
    )
    assert status == 415


def test_oversized_body_is_rejected(server) -> None:
    big = json.dumps({"x": "y" * 20000})
    status, _ = _request(
        server,
        "POST",
        "/api/pricing/preview",
        headers={
            "Host": _host(server),
            "Origin": f"http://{_host(server)}",
            "Content-Type": "application/json",
            "X-AgentControl-CSRF": CSRF,
        },
        body=big,
    )
    assert status == 413


def test_non_finite_json_is_rejected(server) -> None:
    status, data = _request(
        server,
        "POST",
        "/api/pricing/preview",
        headers={
            "Host": _host(server),
            "Origin": f"http://{_host(server)}",
            "Content-Type": "application/json",
            "X-AgentControl-CSRF": CSRF,
        },
        body='{"n": Infinity}',
    )
    assert status == 400
    assert json.loads(data)["error"]["code"] == "invalid_json"


def test_projectapproval_via_api(server, app: DashboardApp) -> None:
    registry = ProjectRegistry(app.paths.projects, app.locks, _clock)
    project, _ = registry.discover(r"D:\repos\Projectb")
    status, data = _request(
        server,
        "POST",
        f"/api/projects/{project.project_id}/approve",
        headers={
            "Host": _host(server),
            "Origin": f"http://{_host(server)}",
            "Content-Type": "application/json",
            "X-AgentControl-CSRF": CSRF,
        },
        body="{}",
    )
    assert status == 200
    assert json.loads(data)["state"] == "approved"


def test_path_traversal_static_is_blocked(server) -> None:
    status, _ = _request(server, "GET", "/static/..%2f..%2fsecret")
    assert status in (400, 404)


def test_get_rejects_bad_host(server) -> None:
    headers = {"Host": "evil.example:1"}
    status, _ = _request(server, "GET", "/api/data", headers=headers)
    assert status == 400
    status, _ = _request(server, "GET", "/", headers=headers)
    assert status == 400


def test_get_accepts_localhost_host(server) -> None:
    headers = {"Host": f"localhost:{server.server_address[1]}"}
    status, _ = _request(server, "GET", "/api/data", headers=headers)
    assert status == 200


def test_index_has_csp_and_frame_headers(server) -> None:
    conn = HTTPConnection("127.0.0.1", server.server_address[1])
    conn.request("GET", "/", headers={"Host": _host(server)})
    response = conn.getresponse()
    response.read()
    csp = response.getheader("Content-Security-Policy")
    frame = response.getheader("X-Frame-Options")
    conn.close()
    assert "frame-ancestors 'none'" in csp
    assert "script-src 'self'" in csp
    assert frame == "DENY"


def test_panel_assets_served(server) -> None:
    headers = {"Host": _host(server)}
    status, _ = _request(server, "GET", "/static/panel.js", headers=headers)
    assert status == 200
    status, _ = _request(server, "GET", "/static/panel.css", headers=headers)
    assert status == 200


# --- tool usage (Task 9) ---------------------------------------------------

SENTINEL = "SENTINEL-TEXT-DO-NOT-STORE"
JUNK_SENTINEL = "SENTINEL-TEXT-DO-NOT-STORE-JUNK"


def test_tool_query_parses_comma_and_repeated_forms() -> None:
    assert _query_from_params("tool=kilo,codex").tools == ("kilo", "codex")
    assert _query_from_params("tool=kilo&tool=codex").tools == ("kilo", "codex")
    assert _query_from_params("tool=engine,claude_code,cline").tools == (
        "engine", "claude_code", "cline",
    )
    assert _query_from_params("").tools == ()


def test_tool_query_rejects_unknown_id() -> None:
    with pytest.raises(ApiError) as caught:
        _query_from_params("tool=kilo,bogus")
    assert caught.value.status == 400
    assert caught.value.code == "invalid_query"


def test_tool_bogus_returns_400(server) -> None:
    status, data = _request(server, "GET", "/api/data?tool=bogus")
    assert status == 400
    assert json.loads(data)["error"]["code"] == "invalid_query"


def _tool_record(project_id: str, cwd: str, session: str, key: str, at: str) -> dict:
    return {
        "schema": 1, "revision": 1, "project_id": project_id,
        "recorded_at": "2026-09-17T11:59:30+00:00",
        "tool": "codex", "key": key, "session": session, "root_session": "root-1",
        "at": at, "model": "gpt-test", "cwd": cwd,
        "tokens_sent": 100, "tokens_returned": 20, "cache_read": 5,
        "cache_write": None, "cache_write_5m": None, "reasoning": None,
        "sent_includes_cache_read": False, "speed": None,
    }


def _write_tool_usage(app: DashboardApp, tmp_path: Path) -> str:
    root = tmp_path / f"work-{SENTINEL}"
    root.mkdir()
    registry = ProjectRegistry(app.paths.projects, app.locks, _clock)
    project, _ = registry.discover(root, display_name="Demo")
    registry.transition(project.project_id, "approved")
    records = [
        _tool_record(project.project_id, project.root, "root-1", "k1",
                     "2026-09-17T11:58:00+00:00"),
        _tool_record(project.project_id, project.root, "sub-1", "k2",
                     "2026-09-17T11:59:00+00:00"),
    ]
    lines = [json.dumps(r) for r in records]
    lines.append(json.dumps({"junk": JUNK_SENTINEL}))
    app.paths.state_dir.mkdir(parents=True, exist_ok=True)
    app.paths.tool_usage.write_text("\n".join(lines) + "\n", encoding="utf-8")
    (app.paths.state_dir / "junk.txt").write_text(JUNK_SENTINEL, encoding="utf-8")
    return project.root


def test_data_includes_tool_usage(app: DashboardApp, server, tmp_path: Path) -> None:
    _write_tool_usage(app, tmp_path)
    status, data = _request(server, "GET", "/api/data")
    assert status == 200
    payload = json.loads(data)
    live = payload["tool_sessions_live"]
    assert len(live) == 1
    assert live[0]["tool"] == "codex"
    assert live[0]["calls"] == 2
    assert live[0]["subagents"] == 1
    assert live[0]["project_name"] == "Demo"
    assert payload["normal_totals"]["all_time"]["by_tool"]["codex"]["count"] == 2
    assert [row["key"] for row in payload["spend"]["by_tool"]] == ["codex"]
    assert payload["live_totals"]["tool_sessions"] == 1


def test_data_tool_filter_narrows_calls(app: DashboardApp, server, tmp_path: Path) -> None:
    _write_tool_usage(app, tmp_path)
    status, data = _request(server, "GET", "/api/data?tool=kilo")
    assert status == 200
    payload = json.loads(data)
    assert payload["tool_sessions_live"] == []
    assert "codex" not in payload["normal_totals"]["all_time"]["by_tool"]


def test_data_keeps_sentinel_out(app: DashboardApp, server, tmp_path: Path) -> None:
    root = _write_tool_usage(app, tmp_path)
    status, data = _request(server, "GET", "/api/data")
    assert status == 200
    body = data.decode("utf-8")
    escaped_root = json.dumps(root)[1:-1]
    assert body.count(SENTINEL) == body.count(escaped_root)
    assert JUNK_SENTINEL not in body

from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from http.client import HTTPConnection
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from agent_control.cli import build_parser, main
from agent_control.dashboard import DashboardApp, create_server, normalize_public_origin
from agent_control.models import SyncReport
from agent_control.paths import AppPaths
from agent_control.storage import StateLocks

ORIGIN = "https://agents.example.test"
HOST = "agents.example.test"
CSRF = "synthetic-csrf-token"


@pytest.mark.parametrize("value, expected", [
    (ORIGIN, ORIGIN),
    ("HTTPS://AGENTS.EXAMPLE.TEST:443", ORIGIN),
    (ORIGIN + ":8443", ORIGIN + ":8443"),
])
def test_public_origin_normalization(value: str, expected: str, tmp_path: Path) -> None:
    assert normalize_public_origin(value) == expected


@pytest.mark.parametrize("value", [
    "", "http://agents.example.test", "//agents.example.test",
    "https://synthetic-user:synthetic-value@agents.example.test",
    ORIGIN + "/", ORIGIN + "/path", ORIGIN + "?", ORIGIN + "#",
    ORIGIN + ":", ORIGIN + ":0", ORIGIN + ":65536", ORIGIN + ":bad",
    ORIGIN + ",evil.example.test", "https://*.example.test",
    "https://agents..example.test", "https://-agents.example.test",
    "https://agents.example.test.", "https://agents%2eexample.test",
    "https://agents\\example.test", "https://agents.exa\nmple.test",
    " " + ORIGIN, ORIGIN + "\x7f", "https://[::1]",
])
def test_public_origin_rejects_ambiguous_urls(value: str, tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="bare HTTPS origin"):
        normalize_public_origin(value)


@pytest.fixture
def public_app(tmp_path: Path) -> DashboardApp:
    synchronizer = MagicMock()
    synchronizer.run.return_value = SyncReport(0, 0, 0, 0, 0, False, ())
    return DashboardApp(
        AppPaths.for_test(tmp_path), StateLocks(), synchronizer,
        lambda: datetime(2026, 10, 28, tzinfo=timezone.utc), public_origin=ORIGIN,
    )


@pytest.fixture
def public_server(public_app: DashboardApp):
    server = create_server(public_app, port=0, csrf_token=CSRF)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def _request(server, method: str, path: str, headers: list[tuple[str, str]]):
    connection = HTTPConnection("127.0.0.1", server.server_address[1])
    connection.putrequest(method, path, skip_host=True)
    for name, value in headers:
        connection.putheader(name, value)
    body = b"{}" if method == "POST" else None
    if body is not None:
        connection.putheader("Content-Type", "application/json")
        connection.putheader("Content-Length", str(len(body)))
    connection.endheaders(body)
    response = connection.getresponse()
    result = response.status, response.read(), dict(response.getheaders())
    connection.close()
    return result


def test_public_get_preserves_loopback_binding_and_page_security(public_server) -> None:
    assert public_server.server_address[0] == "127.0.0.1"
    for host in (HOST, HOST.upper(), HOST + ":443"):
        status, body, headers = _request(public_server, "GET", "/", [("Host", host)])
        assert status == 200
        assert CSRF.encode() in body
        assert "frame-ancestors 'none'" in headers["Content-Security-Policy"]
        assert headers["X-Frame-Options"] == "DENY"
    for route in ("/api/data", "/static/dashboard.js", "/static/panel.css"):
        assert _request(public_server, "GET", route, [("Host", HOST)])[0] == 200


def test_public_get_rejects_missing_duplicate_and_spoofed_hosts(public_server) -> None:
    for headers in (
        [], [("Host", "127.0.0.1:8787")], [("Host", HOST + ":8443")],
        [("Host", HOST), ("Host", "evil.example.test")],
        [("Host", HOST + "/")], [("Host", HOST + ",evil.example.test")],
        [("Host", "evil.example.test"), ("X-Forwarded-Host", HOST),
         ("Forwarded", "host=" + HOST)],
    ):
        status, _, _ = _request(public_server, "GET", "/", headers)
        assert status == 400


def test_public_mutations_keep_origin_and_csrf_guards(public_server) -> None:
    valid = [("Host", HOST), ("Origin", ORIGIN), ("X-AgentControl-CSRF", CSRF)]
    cases = [
        (valid, 404, "not_found"),
        ([("Host", HOST), ("Origin", ORIGIN + ":443"),
          ("X-AgentControl-CSRF", CSRF)], 404, "not_found"),
        (valid[:1] + valid[2:], 403, "bad_origin"),
        (valid[:2], 403, "bad_csrf"),
        (valid + [("Origin", "https://evil.example.test")], 403, "bad_origin"),
    ]
    for origin in ("null", "http://" + HOST, ORIGIN + ":8443", ORIGIN + "/",
                   ORIGIN + " https://evil.example.test"):
        cases.append(([("Host", HOST), ("Origin", origin),
                       ("X-AgentControl-CSRF", CSRF)], 403, "bad_origin"))
    for headers, expected_status, code in cases:
        status, body, _ = _request(public_server, "POST", "/api/no-such-route", headers)
        assert status == expected_status
        assert json.loads(body)["error"]["code"] == code


def test_public_approval_only_mutates_after_all_guards(public_app, public_server) -> None:
    project, _ = public_app.registry.discover(str(public_app.paths.state_dir / "project"))
    route = f"/api/projects/{project.project_id}/approve"
    headers = [("Host", HOST), ("Origin", "https://evil.example.test"),
               ("X-AgentControl-CSRF", CSRF)]
    assert _request(public_server, "POST", route, headers)[0] == 403
    headers[1] = ("Origin", ORIGIN)
    status, body, _ = _request(public_server, "POST", route, headers)
    assert status == 200
    assert json.loads(body)["state"] == "approved"


def test_public_narration_still_requires_csrf(public_server) -> None:
    route = "/api/narration?run_id=synthetic-missing-run"
    assert _request(public_server, "GET", route, [("Host", HOST)])[0] == 403
    assert _request(public_server, "GET", route, [
        ("Host", HOST), ("X-AgentControl-CSRF", CSRF),
    ])[0] == 404


def test_public_nondefault_port_is_not_the_backend_port(public_app) -> None:
    public_app.public_origin = ORIGIN + ":8443"
    server = create_server(public_app, port=0, csrf_token=CSRF)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        assert _request(server, "GET", "/api/data", [("Host", HOST + ":8443")])[0] == 200
        assert _request(server, "GET", "/api/data", [("Host", HOST)])[0] == 400
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_cli_origin_validation_is_sanitized_before_state_creation(tmp_path, capsys) -> None:
    invalid = "https://synthetic-user:synthetic-value@agents.example.test"
    with pytest.raises(SystemExit) as error:
        main(["--state-dir", str(tmp_path / "state"), "--public-origin", invalid, "serve"])
    assert error.value.code == 2
    assert "synthetic-user" not in capsys.readouterr().err
    assert not (tmp_path / "state").exists()


def test_cli_passes_public_origin_to_app(tmp_path, monkeypatch) -> None:
    factory = MagicMock(return_value=MagicMock())
    monkeypatch.setattr("agent_control.dashboard.create_server", factory)
    assert build_parser().parse_args(["serve"]).public_origin is None
    assert main([
        "--state-dir", str(tmp_path / "state"),
        "--machine-index", str(tmp_path / "missing-index.jsonl"),
        "--public-origin", "HTTPS://AGENTS.EXAMPLE.TEST:443", "serve",
    ]) == 0
    assert factory.call_args.args[0].public_origin == ORIGIN

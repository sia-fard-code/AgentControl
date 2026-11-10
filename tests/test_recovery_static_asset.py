"""The new module must be loadable through the real closed HTTP asset route."""
from datetime import datetime, timezone
from http.client import HTTPConnection
from threading import Thread
from unittest.mock import MagicMock

import pytest

from agent_control.dashboard import DashboardApp, create_server
from agent_control.paths import AppPaths
from agent_control.storage import StateLocks


@pytest.fixture
def asset_server(tmp_path):
    paths = AppPaths.for_test(tmp_path)
    paths.web_dir.mkdir(parents=True)
    (paths.web_dir / "task-state.js").write_text("export const synthetic = true;", encoding="utf-8")
    (paths.web_dir / "private.js").write_text("SYNTHETIC_PRIVATE", encoding="utf-8")
    app = DashboardApp(paths, StateLocks(), MagicMock(),
                       lambda: datetime(2026, 11, 1, tzinfo=timezone.utc))
    server = create_server(app, port=0, csrf_token="synthetic-csrf")
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def fetch(server, path):
    connection = HTTPConnection("127.0.0.1", server.server_address[1], timeout=5)
    try:
        connection.request("GET", path)
        response = connection.getresponse()
        return response.status, dict(response.getheaders()), response.read()
    finally:
        connection.close()


def test_task_state_module_is_served_with_javascript_and_nosniff(asset_server):
    status, headers, body = fetch(asset_server, "/static/task-state.js")
    assert status == 200
    assert headers["Content-Type"] == "text/javascript; charset=utf-8"
    assert headers["X-Content-Type-Options"] == "nosniff"
    assert body == b"export const synthetic = true;"


@pytest.mark.parametrize("path", ["/static/private.js", "/static/../task-state.js",
                                  "/static/sub/task-state.js"])
def test_new_module_does_not_open_other_asset_paths(asset_server, path):
    status, _, body = fetch(asset_server, path)
    assert status in (400, 404)
    assert b"SYNTHETIC_PRIVATE" not in body

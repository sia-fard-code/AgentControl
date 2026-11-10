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
from agent_control.models import SyncReport
from agent_control.paths import AppPaths
from agent_control.storage import StateLocks
from tests.test_ledger import run_candidate

CSRF = "test-csrf-token"


def _clock() -> datetime:
    return datetime(2026, 9, 17, 12, 0, tzinfo=timezone.utc)


def _transcript(task: str = "do the thing", tool_output: str = "file output") -> str:
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
        tool_output,
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


@pytest.fixture
def server(app: DashboardApp):
    httpd = create_server(app, port=0, csrf_token=CSRF)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield httpd
    httpd.shutdown()


def _port(server) -> int:
    return server.server_address[1]


def _get(server, path: str, headers: dict | None = None):
    conn = HTTPConnection("127.0.0.1", _port(server))
    hdrs = {"Host": f"127.0.0.1:{_port(server)}"}
    if headers:
        hdrs.update(headers)
    conn.request("GET", path, headers=hdrs)
    response = conn.getresponse()
    data = response.read()
    conn.close()
    return response.status, data


def _seed(app: DashboardApp, run_id: str, transcript: Path, status: str = "running") -> None:
    sync_candidate(
        app.paths.ledger,
        replace(
            run_candidate(run_id=run_id, status=status),
            transcript_artifact=str(transcript),
        ),
        observed_at="2026-09-17T12:00:00+00:00",
        locks=app.locks,
    )


def _write(app: DashboardApp, name: str, text: str) -> Path:
    app.paths.state_dir.mkdir(parents=True, exist_ok=True)
    path = app.paths.state_dir / name
    path.write_text(text, encoding="utf-8")
    return path


def test_unknown_run_is_found_after_one_sync(app: DashboardApp) -> None:
    transcript = _write(app, "new.md", _transcript())

    def sync_registers_the_run():
        _seed(app, "new-run", transcript)
        return SyncReport(0, 0, 0, 0, 0, False, ())

    app.synchronizer.run.side_effect = sync_registers_the_run
    payload = app.narration("new-run", 0)
    assert payload["available"] is True
    assert app.synchronizer.run.call_count == 1


def test_known_run_does_not_sync(app: DashboardApp) -> None:
    transcript = _write(app, "r.md", _transcript())
    _seed(app, "r", transcript)
    app.narration("r", 0)
    assert app.synchronizer.run.call_count == 0


def test_still_unknown_after_sync_is_404(app: DashboardApp) -> None:
    from agent_control.dashboard import ApiError

    with pytest.raises(ApiError) as ei:
        app.narration("missing", 0)
    assert ei.value.status == 404
    assert app.synchronizer.run.call_count == 1


def test_narration_requires_csrf(app: DashboardApp, server) -> None:
    transcript = _write(app, "r.md", _transcript())
    _seed(app, "r", transcript)
    status, _ = _get(server, "/api/narration?run_id=r")
    assert status == 403


def test_narration_returns_events_and_brief(app: DashboardApp, server) -> None:
    transcript = _write(app, "r.md", _transcript())
    _seed(app, "r", transcript)
    status, data = _get(
        server, "/api/narration?run_id=r&after=0", {"X-AgentControl-CSRF": CSRF}
    )
    assert status == 200
    payload = json.loads(data)
    assert payload["available"] is True
    assert payload["brief"] == "do the thing"
    total = payload["total"]
    assert total == len(payload["events"])
    assert payload["events"][0]["seq"] == 0

    status, data = _get(
        server, "/api/narration?run_id=r&after=2", {"X-AgentControl-CSRF": CSRF}
    )
    payload2 = json.loads(data)
    assert payload2["brief"] is None
    assert payload2["events"][0]["seq"] == 2
    assert payload2["total"] == total


def test_narration_bad_after(app: DashboardApp, server) -> None:
    transcript = _write(app, "r.md", _transcript())
    _seed(app, "r", transcript)
    status, _ = _get(
        server, "/api/narration?run_id=r&after=-1", {"X-AgentControl-CSRF": CSRF}
    )
    assert status == 400
    status, _ = _get(
        server, "/api/narration?run_id=r&after=x", {"X-AgentControl-CSRF": CSRF}
    )
    assert status == 400


def test_narration_rejects_non_ascii_after(app: DashboardApp, server) -> None:
    transcript = _write(app, "r.md", _transcript())
    _seed(app, "r", transcript)
    status, _ = _get(
        server, "/api/narration?run_id=r&after=%C2%B2",
        {"X-AgentControl-CSRF": CSRF},
    )
    assert status == 400


def test_narration_masks_and_counts(app: DashboardApp, server) -> None:
    secret = "sk-" + "a" * 20
    transcript = _write(app, "r.md", _transcript(tool_output=secret))
    _seed(app, "r", transcript)
    status, data = _get(
        server, "/api/narration?run_id=r&after=0", {"X-AgentControl-CSRF": CSRF}
    )
    payload = json.loads(data)
    assert payload["masked"] >= 1
    assert secret not in json.dumps(payload)


def test_masked_counts_whole_transcript(app: DashboardApp, server) -> None:
    secret = "sk-" + "a" * 20
    transcript = _write(app, "r.md", _transcript(tool_output=secret))
    _seed(app, "r", transcript)
    first = app.narration("r", 0)
    total = first["total"]
    last = app.narration("r", total - 1)
    assert first["masked"] >= 1
    assert first["masked"] == last["masked"]


def test_narration_masks_each_parse_once(app: DashboardApp, monkeypatch) -> None:
    import agent_control.dashboard as dashboard_mod

    real = dashboard_mod.masked_dict
    calls = {"n": 0}

    def counting(event):
        calls["n"] += 1
        return real(event)

    monkeypatch.setattr(dashboard_mod, "masked_dict", counting)
    transcript = _write(app, "r.md", _transcript())
    _seed(app, "r", transcript)
    first = app.narration("r", 0)
    total = first["total"]
    app.narration("r", total - 1)
    assert calls["n"] == total


def test_narration_missing_transcript(app: DashboardApp, server) -> None:
    missing = app.paths.state_dir / "gone.md"
    _seed(app, "r", missing)
    status, data = _get(
        server, "/api/narration?run_id=r", {"X-AgentControl-CSRF": CSRF}
    )
    assert status == 200
    payload = json.loads(data)
    assert payload["available"] is False
    assert payload["total"] == 0


def test_narration_unknown_run_404(app: DashboardApp, server) -> None:
    status, _ = _get(
        server, "/api/narration?run_id=nope", {"X-AgentControl-CSRF": CSRF}
    )
    assert status == 404


def test_narration_writes_nothing_to_state(app: DashboardApp, server) -> None:
    transcript = _write(app, "r.md", _transcript())
    _seed(app, "r", transcript)

    def _files() -> set[str]:
        return {
            str(p.relative_to(app.paths.state_dir))
            for p in app.paths.state_dir.rglob("*")
            if p.is_file()
        }

    _get(server, "/api/narration?run_id=r", {"X-AgentControl-CSRF": CSRF})
    before = _files()
    for _ in range(3):
        _get(server, "/api/narration?run_id=r", {"X-AgentControl-CSRF": CSRF})
    assert _files() == before

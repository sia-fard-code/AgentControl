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


def _request(server, method: str, path: str):
    conn = HTTPConnection("127.0.0.1", server.server_address[1])
    conn.request(method, path)
    response = conn.getresponse()
    data = response.read()
    conn.close()
    return response.status, data


def _seed(app: DashboardApp, run_id: str, transcript: Path, *, status: str) -> None:
    candidate = replace(
        run_candidate(run_id=run_id, model="gpt-5.6-sol", status=status),
        transcript_artifact=str(transcript),
    )
    sync_candidate(
        app.paths.ledger, candidate, observed_at="2026-09-17T12:00:00+00:00", locks=app.locks
    )


def _mtime_iso(path: Path) -> str:
    return datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc).isoformat()


def test_activity_reports_model_call_phase(app: DashboardApp, server) -> None:
    transcript = app.paths.state_dir / "calling.md"
    transcript.parent.mkdir(parents=True, exist_ok=True)
    transcript.write_text(
        '### [1] read_file({"path": "a"})\n'
        "_[ 2] calling gpt-5.6-sol (attempt 1/4, foo) ..._\n",
        encoding="utf-8",
    )
    _seed(app, "live-1", transcript, status="running")

    status, data = _request(server, "GET", "/api/activity?run_id=live-1")
    assert status == 200
    payload = json.loads(data)
    assert payload["current_phase"] == "model_call"
    assert payload["current_phase_since"] == _mtime_iso(transcript)
    assert payload["current_started_at"] == payload["current_phase_since"]
    assert payload["progress"] is not None
    assert payload["progress"]["phase"] == "model_call"


def test_activity_progress_none_when_not_running(app: DashboardApp, server) -> None:
    transcript = app.paths.state_dir / "done.md"
    transcript.parent.mkdir(parents=True, exist_ok=True)
    transcript.write_text(
        '### [1] read_file({"path": "a"})\n',
        encoding="utf-8",
    )
    _seed(app, "done-1", transcript, status="complete")

    status, data = _request(server, "GET", "/api/activity?run_id=done-1")
    payload = json.loads(data)
    assert payload["progress"] is None
    assert payload["current_phase"] is None


def test_activity_caches_snapshot_for_unchanged_transcript(
    app: DashboardApp, server, monkeypatch
) -> None:
    import agent_control.activity as activity_mod

    transcript = app.paths.state_dir / "cached.md"
    transcript.parent.mkdir(parents=True, exist_ok=True)
    transcript.write_text(
        '### [1] read_file({"path": "a"})\n',
        encoding="utf-8",
    )
    _seed(app, "cache-1", transcript, status="running")

    calls = {"n": 0}
    real = activity_mod.build_activity_snapshot

    def _counting(path):
        calls["n"] += 1
        return real(path)

    monkeypatch.setattr(activity_mod, "build_activity_snapshot", _counting)

    _request(server, "GET", "/api/activity?run_id=cache-1")
    _request(server, "GET", "/api/activity?run_id=cache-1")
    assert calls["n"] == 1


def test_activity_cache_bounded_to_16(app: DashboardApp) -> None:
    app.paths.state_dir.mkdir(parents=True, exist_ok=True)
    for i in range(20):
        transcript = app.paths.state_dir / f"bound-{i}.md"
        transcript.write_text(
            '### [1] read_file({"path": "a"})\n', encoding="utf-8"
        )
        run_id = f"bound-{i}"
        _seed(app, run_id, transcript, status="running")
        app.activity(run_id)
    assert len(app._activity_cache) == 16


def test_activity_concurrent_calls_no_exception(app: DashboardApp) -> None:
    app.paths.state_dir.mkdir(parents=True, exist_ok=True)
    run_ids = []
    for i in range(3):
        transcript = app.paths.state_dir / f"conc-{i}.md"
        transcript.write_text(
            '### [1] read_file({"path": "a"})\n', encoding="utf-8"
        )
        run_id = f"conc-{i}"
        _seed(app, run_id, transcript, status="running")
        run_ids.append(run_id)

    errors: list[Exception] = []

    def _hammer(run_id: str) -> None:
        try:
            for _ in range(50):
                app.activity(run_id)
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [
        threading.Thread(target=_hammer, args=(run_ids[i % 3],)) for i in range(8)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []

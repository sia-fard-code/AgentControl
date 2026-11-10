import json
from dataclasses import replace
from datetime import datetime, timezone

from agent_control.ledger import sync_candidate
from agent_control.cli import main
from tests.test_dashboard_api import app, server, _request
from tests.test_ledger import run_candidate
from tests.test_run_diagnosis import sample_run, event, write_events, PRIVATE


def test_diagnosis_requires_a_known_run(server):
    assert _request(server, "GET", "/api/diagnosis")[0] == 400
    assert _request(server, "GET", "/api/diagnosis?run_id=unknown")[0] == 404


def test_diagnosis_http_and_activity_return_only_the_same_metadata_summary(app, server, tmp_path):
    record = sample_run(tmp_path)
    candidate = replace(run_candidate(run_id="sample", status="interrupted"),
                        transcript_artifact=record.transcript_artifact,
                        timestamp=record.timestamp, ended_at=None)
    sync_candidate(app.paths.ledger, candidate,
                   observed_at="2026-11-01T11:00:00Z", locks=app.locks)
    write_events(record, [event("registered"), event("exception", reason="broken_output",
                  exception_class="BrokenPipeError", source="delegate.py", line=200,
                  traceback=PRIVATE, stderr=PRIVATE)])
    status, data = _request(server, "GET", "/api/diagnosis?run_id=sample")
    assert status == 200
    payload = json.loads(data)
    assert payload["category"] == "broken_output"
    assert PRIVATE not in data.decode()
    status, data = _request(server, "GET", "/api/activity?run_id=sample")
    assert status == 200
    assert json.loads(data)["diagnosis"] == payload


def test_diagnose_and_incidents_cli_are_read_only(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr("agent_control.cli._utc_now", lambda: datetime(2026, 11, 1, 11, tzinfo=timezone.utc))
    record = sample_run(tmp_path)
    write_events(record, [event("registered"), event("command_exit", exit_code=1)])
    before = {p: p.read_bytes() for p in tmp_path.iterdir() if p.is_file()}
    common = ["--state-dir", str(tmp_path), "--machine-index", str(tmp_path / "index.jsonl")]
    assert main([*common, "diagnose", "--run-id", "sample"]) == 0
    assert json.loads(capsys.readouterr().out)["category"] == "process_exit_unknown"
    assert main([*common, "incidents", "--days", "365", "--limit", "1"]) == 0
    assert json.loads(capsys.readouterr().out)["scanned_runs"] == 1
    assert before == {p: p.read_bytes() for p in tmp_path.iterdir() if p.is_file()}

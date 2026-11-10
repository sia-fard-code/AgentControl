"""Observe transport evidence without copying payloads into diagnostics."""
import io
import json
import socket
import urllib.error

import pytest

from test_engine_contract import engine  # noqa: F401
from test_lifecycle_diagnostics import load_module, rows


@pytest.fixture
def observed(engine, tmp_path, monkeypatch):
    path = tmp_path / "engine.diagnostics.jsonl"
    journal = load_module().Journal(path, "engine", run_id="synthetic", engine_pid=123)
    monkeypatch.setattr(engine, "DIAGNOSTICS", journal, raising=False)
    return path


class Response:
    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def __iter__(self):
        yield b'data: {"type":"response.created"}\n'
        yield b'data: {"type":"response.reasoning_summary_text.delta","delta":"SYNTHETIC_PRIVATE"}\n'
        yield b'data: {"type":"response.completed"}\n'
        yield b'data: [DONE]\n'

    def read(self):
        return b'{"choices": [{"message": {"content": "SYNTHETIC_PRIVATE"}}]}'


def test_stream_records_request_and_last_activity(engine, observed, monkeypatch):
    monkeypatch.setattr(engine.urllib.request, "urlopen", lambda *a, **k: Response())
    list(engine.stream_events("http://synthetic", "SYNTHETIC_PRIVATE", "/openai/responses",
                              {"input": "SYNTHETIC_PRIVATE"}))
    records = rows(observed)
    assert records[0]["event"] == "request_start"
    assert records[0]["timeout_s"] == 300
    assert any(r["event"] == "stream_open" for r in records)
    assert any(r.get("activity") == "reasoning" for r in records)
    assert records[-1]["event"] == "request_end"
    assert "SYNTHETIC_PRIVATE" not in observed.read_text(encoding="utf-8")


@pytest.mark.parametrize("exc,reason,status", [
    (socket.timeout("SYNTHETIC_PRIVATE"), "stream_idle", None),
    (ConnectionResetError("SYNTHETIC_PRIVATE"), "disconnect", None),
    (urllib.error.HTTPError("http://synthetic", 504, "SYNTHETIC_PRIVATE", {},
                            io.BytesIO(b'SYNTHETIC_PRIVATE')), "http_error", 504),
])
def test_transport_error_is_structured(engine, observed, monkeypatch, exc, reason, status):
    def fail(*args, **kwargs):
        raise exc
    monkeypatch.setattr(engine.urllib.request, "urlopen", fail)
    with pytest.raises(engine.StreamError):
        list(engine.stream_events("http://synthetic", "synthetic-key", "/openai/responses", {}))
    last = rows(observed)[-1]
    assert last["event"] == "request_error" and last["reason"] == reason
    assert last.get("http_status") == status
    assert "SYNTHETIC_PRIVATE" not in observed.read_text(encoding="utf-8")


def test_nonstream_request_has_evidence(engine, observed, monkeypatch):
    monkeypatch.setattr(engine.urllib.request, "urlopen", lambda *a, **k: Response())
    engine.chat("http://synthetic", "synthetic-key", {"input": "SYNTHETIC_PRIVATE"})
    assert [r["event"] for r in rows(observed)] == ["request_start", "request_end"]


def test_chat_stream_has_reasoning_and_answer_milestones(engine, observed, monkeypatch):
    class ChatResponse(Response):
        def __iter__(self):
            yield b'data: {"choices":[{"delta":{"reasoning_content":"SYNTHETIC_PRIVATE"}}]}\n'
            yield b'data: {"choices":[{"delta":{"content":"SYNTHETIC_PRIVATE"}}]}\n'
            yield b'data: [DONE]\n'
    monkeypatch.setattr(engine.urllib.request, "urlopen", lambda *a, **k: ChatResponse())
    list(engine.stream_events("http://synthetic", "synthetic-key", "/api/chat/completions", {}))
    assert {"reasoning", "answer"} <= {r.get("activity") for r in rows(observed)}
    assert "SYNTHETIC_PRIVATE" not in observed.read_text(encoding="utf-8")


def test_exception_gets_metadata_without_changing_accounting(engine, observed, tmp_path, monkeypatch):
    lifecycle = engine.RunLifecycle("synthetic", str(tmp_path / "index.jsonl"), "synthetic")
    lifecycle.begin(transcript=str(tmp_path / "run.md"))
    def fail():
        raise RuntimeError("SYNTHETIC_PRIVATE")
    monkeypatch.setattr(engine, "_main", fail, raising=False)
    with pytest.raises(RuntimeError):
        engine.main()
    records = rows(observed)
    assert records[-1]["event"] == "exception"
    assert records[-1]["exception_class"] == "RuntimeError"
    assert rows(tmp_path / "index.jsonl")[-1]["event"] == "start"
    lifecycle._safety_end()
    assert rows(tmp_path / "index.jsonl")[-1]["status"] == "interrupted"
    assert "SYNTHETIC_PRIVATE" not in observed.read_text(encoding="utf-8")


def test_signal_intent_is_written_before_existing_handler(engine, observed, tmp_path, monkeypatch):
    lc = engine.RunLifecycle("synthetic", str(tmp_path / "index.jsonl"), "synthetic")
    lc.begin(transcript=str(tmp_path / "run.md"))
    captured = []
    monkeypatch.setattr(engine.os, "kill", lambda *args: captured.extend(rows(observed)))
    lc._on_signal(engine.signal.SIGTERM, None)
    assert any(r["event"] == "signal" and r["signal"] == engine.signal.SIGTERM for r in captured)
    assert any(r.get("status") == "interrupted" for r in captured)


def test_gateway_failed_is_not_mislabeled_as_transport_failure(engine, observed):
    with pytest.raises(engine.StreamError) as caught:
        engine.gpt_stream_event({"type": "response.failed", "response": {
            "error": {"message": "SYNTHETIC_PRIVATE"}}}, None, {"usage": None})
    assert caught.value.reason == "gateway_failed"


def test_engine_diagnostics_loader_failure_is_fail_open(engine, tmp_path, monkeypatch, capsys):
    def fail(*args):
        raise RuntimeError("SYNTHETIC_PRIVATE")
    monkeypatch.setattr(engine, "capability_module", fail)
    assert engine.diagnostic_journal(str(tmp_path / "synthetic.jsonl")) is None
    assert "SYNTHETIC_PRIVATE" not in capsys.readouterr().err

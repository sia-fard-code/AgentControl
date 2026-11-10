from __future__ import annotations

import importlib.util
import io
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

import pytest

ENGINE_PATH = Path(__file__).resolve().parents[1] / "engine" / "delegate.py"


def _load_engine():
    spec = importlib.util.spec_from_file_location("agentcontrol_engine", ENGINE_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def engine():
    return _load_engine()


class _FakeResp:
    def __init__(self, body: bytes):
        self._body = body

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _http_error(code: int, body: bytes):
    return urllib.error.HTTPError(
        url="http://gw/api/chat/completions",
        code=code,
        msg="err",
        hdrs=None,
        fp=io.BytesIO(body),
    )


def test_chat_400_model_not_found(engine, monkeypatch):
    def fake_urlopen(req, timeout=None):
        raise _http_error(400, b'{"detail":"Model not found"}')

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    out = engine.chat("http://gw", "k", {"model": "m"})
    assert out.get("_model_not_found") is True
    assert out.get("_transient") == "HTTP 400 model not found"


def test_chat_400_other(engine, monkeypatch):
    def fake_urlopen(req, timeout=None):
        raise _http_error(400, b'{"detail":"something else"}')

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    out = engine.chat("http://gw", "k", {"model": "m"})
    assert out.get("_transient") == "HTTP 400"
    assert "_model_not_found" not in out


def test_chat_500(engine, monkeypatch):
    def fake_urlopen(req, timeout=None):
        raise _http_error(500, b"boom")

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    out = engine.chat("http://gw", "k", {"model": "m"})
    assert out.get("_transient") == "HTTP 500"
    assert "_model_not_found" not in out


def test_retry_recovers(engine, monkeypatch):
    results = [
        {"_transient": "HTTP 400 model not found", "_model_not_found": True},
        {"_transient": "HTTP 400 model not found", "_model_not_found": True},
        {"choices": [{"message": {"content": "ok"}}]},
    ]
    calls = {"chat": 0, "refresh": 0, "sleep": 0}

    def fake_chat(url, key, payload):
        r = results[calls["chat"]]
        calls["chat"] += 1
        return r

    def fake_refresh(url, key):
        calls["refresh"] += 1
        return {"m"}

    def fake_sleep(_):
        calls["sleep"] += 1

    monkeypatch.setattr(engine, "chat", fake_chat)
    monkeypatch.setattr(engine, "refresh_models", fake_refresh)
    out = engine.chat_with_model_retry("http://gw", "k", {"model": "m"}, 3,
                                       sleep=fake_sleep)
    assert out == {"choices": [{"message": {"content": "ok"}}]}
    assert calls["refresh"] == 2
    assert calls["sleep"] == 2


def test_retry_always_not_found(engine, monkeypatch):
    def fake_chat(url, key, payload):
        return {"_transient": "HTTP 400 model not found", "_model_not_found": True}

    monkeypatch.setattr(engine, "chat", fake_chat)
    monkeypatch.setattr(engine, "refresh_models", lambda u, k: {"x"})
    with pytest.raises(SystemExit) as ei:
        engine.chat_with_model_retry("http://gw", "k", {"model": "m"}, 1,
                                     tries=3, sleep=lambda _: None)
    assert "stale model caches" in str(ei.value)


def test_retry_first_good(engine, monkeypatch):
    calls = {"refresh": 0, "sleep": 0}

    def fake_chat(url, key, payload):
        return {"choices": [{"message": {"content": "ok"}}]}

    def fake_refresh(url, key):
        calls["refresh"] += 1
        return {"m"}

    def fake_sleep(_):
        calls["sleep"] += 1

    monkeypatch.setattr(engine, "chat", fake_chat)
    monkeypatch.setattr(engine, "refresh_models", fake_refresh)
    out = engine.chat_with_model_retry("http://gw", "k", {"model": "m"}, 1,
                                       sleep=fake_sleep)
    assert out == {"choices": [{"message": {"content": "ok"}}]}
    assert calls["refresh"] == 0
    assert calls["sleep"] == 0


def test_refresh_models_exception(engine, monkeypatch):
    def fake_urlopen(req, timeout=None):
        raise RuntimeError("down")

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    assert engine.refresh_models("http://gw", "k") is None


def test_refresh_models_data_shape(engine, monkeypatch):
    def fake_urlopen(req, timeout=None):
        return _FakeResp(json.dumps({"data": [{"id": "a"}, {"id": "b"}]}).encode())

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    assert engine.refresh_models("http://gw", "k") == {"a", "b"}


def test_refresh_models_list_shape(engine, monkeypatch):
    def fake_urlopen(req, timeout=None):
        return _FakeResp(json.dumps([{"id": "a"}, {"id": "c"}]).encode())

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    assert engine.refresh_models("http://gw", "k") == {"a", "c"}


@pytest.mark.parametrize("status", [401, 403])
def test_chat_auth_failure_is_sanitized(engine, monkeypatch, status):
    def fake_urlopen(req, timeout=None):
        raise _http_error(status, b'{"detail": "bad key sekret-key", "echo": "raw body"}')

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(SystemExit) as ei:
        engine.chat("http://gw", "sekret-key", {"model": "m"})
    msg = str(ei.value)
    assert f"HTTP {status}" in msg and "[redacted]" in msg
    assert "sekret-key" not in msg and "raw body" not in msg

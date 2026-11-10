from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from agent_control import reader_cline

SENTINEL = "SENTINEL-TEXT-DO-NOT-STORE"
READER_KEYS = ("tool", "key", "session", "root_session", "at", "model", "cwd",
               "tokens_sent", "tokens_returned", "cache_read", "cache_write",
               "cache_write_5m", "reasoning", "sent_includes_cache_read", "speed")
NOW = datetime(2026, 10, 1, 12, 0, 0, tzinfo=timezone.utc)
TS = 1758000000000


def _req(ts=TS, *, provider="openai", model="gpt-5.6-terra", with_model=True, **tokens):
    payload = {"request": SENTINEL}
    payload.update(tokens)
    entry = {"ts": ts, "type": "say", "say": "api_req_started", "text": json.dumps(payload)}
    if with_model:
        entry["modelInfo"] = {"providerId": provider, "modelId": model, "mode": "act"}
    return entry


def _full_tokens():
    return {"tokensIn": 100, "tokensOut": 20, "cacheWrites": 5, "cacheReads": 70, "cost": 0.01}


def _write_task(source: Path, task: str, entries, *, raw: str | None = None) -> Path:
    tdir = source / "tasks" / task
    tdir.mkdir(parents=True, exist_ok=True)
    ui = tdir / "ui_messages.json"
    if raw is not None:
        ui.write_text(raw, encoding="utf-8")
    else:
        extra = [
            {"ts": TS - 5, "type": "say", "say": "text", "text": SENTINEL},
            {"ts": TS - 4, "type": "say", "say": "reasoning", "text": SENTINEL},
        ]
        ui.write_text(json.dumps(extra + list(entries)), encoding="utf-8")
    (tdir / "api_conversation_history.json").write_text(
        json.dumps([{"role": "user", "content": [{"type": "text", "text": SENTINEL}]}]),
        encoding="utf-8")
    (tdir / "task_metadata.json").write_text(json.dumps({"files_in_context": []}),
                                             encoding="utf-8")
    return ui


def _write_history(source: Path, items) -> None:
    state = source / "state"
    state.mkdir(parents=True, exist_ok=True)
    full = []
    for item in items:
        row = {"ts": TS, "task": SENTINEL, "tokensIn": 1, "tokensOut": 1, "cacheWrites": 0,
               "cacheReads": 0, "totalCost": 0.0, "modelId": "gpt-5.6-terra"}
        row.update(item)
        full.append(row)
    (state / "taskHistory.json").write_text(json.dumps(full), encoding="utf-8")


def _assert_no_sentinel(calls, checkpoint, diags) -> None:
    assert SENTINEL not in json.dumps(calls)
    assert SENTINEL not in json.dumps(checkpoint)
    for d in diags:
        assert SENTINEL not in repr(d)


def test_completed_request_gives_one_exact_call(tmp_path):
    src = tmp_path / "cline"
    _write_task(src, "1758000000001", [_req(**_full_tokens())])
    _write_history(src, [{"id": "1758000000001", "cwdOnTaskInitialization": "C:\\proj\\a"}])
    calls, cp, diags = reader_cline.read(src, None, now=NOW)
    assert diags == []
    assert len(calls) == 1
    call = calls[0]
    assert set(call) == set(READER_KEYS)
    assert call == {
        "tool": "cline",
        "key": f"1758000000001:{TS}",
        "session": "1758000000001",
        "root_session": "1758000000001",
        "at": datetime.fromtimestamp(TS / 1000, tz=timezone.utc).isoformat(),
        "model": "gpt-5.6-terra",
        "cwd": "C:\\proj\\a",
        "tokens_sent": 100,
        "tokens_returned": 20,
        "cache_read": 70,
        "cache_write": 5,
        "cache_write_5m": None,
        "reasoning": None,
        "sent_includes_cache_read": True,
        "speed": None,
    }
    assert call["at"].endswith("+00:00")
    assert "1758000000001" in cp["files"]
    json.dumps(cp)


def test_streaming_request_counted_once_after_completion(tmp_path):
    src = tmp_path / "cline"
    _write_task(src, "t1", [_req()])
    calls, cp, diags = reader_cline.read(src, None, now=NOW)
    assert calls == [] and diags == []
    _write_task(src, "t1", [_req(**_full_tokens())])
    later = NOW + timedelta(seconds=31)
    calls, cp, diags = reader_cline.read(src, cp, now=later)
    assert [c["key"] for c in calls] == [f"t1:{TS}"]
    calls, cp, diags = reader_cline.read(src, cp, now=later + timedelta(seconds=60))
    assert calls == []


def test_cancelled_request_with_tokens_is_counted(tmp_path):
    src = tmp_path / "cline"
    _write_task(src, "t1", [_req(cancelReason="user_cancelled",
                                 streamingFailedMessage=SENTINEL, tokensIn=7, tokensOut=3)])
    calls, cp, diags = reader_cline.read(src, None, now=NOW)
    assert len(calls) == 1
    assert calls[0]["tokens_sent"] == 7 and calls[0]["tokens_returned"] == 3
    _assert_no_sentinel(calls, cp, diags)


def test_provider_shapes(tmp_path):
    src = tmp_path / "cline"
    _write_task(src, "t1", [
        _req(TS, provider="openai", tokensIn=1, tokensOut=1),
        _req(TS + 1, provider="anthropic", model="claude-x", tokensIn=1, tokensOut=1),
        _req(TS + 2, with_model=False, tokensIn=1, tokensOut=1),
    ])
    calls, _, _ = reader_cline.read(src, None, now=NOW)
    by_key = {c["key"]: c for c in calls}
    assert by_key[f"t1:{TS}"]["sent_includes_cache_read"] is True
    assert by_key[f"t1:{TS + 1}"]["sent_includes_cache_read"] is False
    assert by_key[f"t1:{TS + 2}"]["sent_includes_cache_read"] is True
    assert by_key[f"t1:{TS + 2}"]["model"] is None
def test_cwd_from_task_history_or_none(tmp_path):
    src = tmp_path / "cline"
    _write_task(src, "known", [_req(tokensIn=1, tokensOut=1)])
    _write_task(src, "unknown", [_req(tokensIn=1, tokensOut=1)])
    _write_history(src, [{"id": "known", "cwdOnTaskInitialization": "/work/known"}])
    calls, _, diags = reader_cline.read(src, None, now=NOW)
    assert diags == []
    by_session = {c["session"]: c for c in calls}
    assert by_session["known"]["cwd"] == "/work/known"
    assert by_session["unknown"]["cwd"] is None


def test_sentinel_never_leaves_reader(tmp_path):
    src = tmp_path / "cline"
    _write_task(src, "t1", [_req(**_full_tokens()), _req(TS + 9)])
    _write_task(src, "bad", [], raw="[{" + SENTINEL)
    _write_history(src, [{"id": "t1", "cwdOnTaskInitialization": "/w"}])
    calls, cp, diags = reader_cline.read(src, None, now=NOW)
    assert len(calls) == 1
    assert any(d.code == "cline_bad_file" for d in diags)
    _assert_no_sentinel(calls, cp, diags)


def test_reread_waits_thirty_seconds_after_read_at(tmp_path):
    src = tmp_path / "cline"
    _write_task(src, "t1", [_req(tokensIn=1, tokensOut=1)])
    calls, cp1, _ = reader_cline.read(src, None, now=NOW)
    assert len(calls) == 1
    _write_task(src, "t1", [_req(tokensIn=1, tokensOut=1),
                            _req(TS + 1, tokensIn=22, tokensOut=33)])
    calls, cp2, diags = reader_cline.read(src, cp1, now=NOW + timedelta(seconds=10))
    assert calls == [] and diags == []
    assert cp2["files"]["t1"] == cp1["files"]["t1"]
    calls, cp3, _ = reader_cline.read(src, cp2, now=NOW + timedelta(seconds=31))
    assert sorted(c["key"] for c in calls) == [f"t1:{TS}", f"t1:{TS + 1}"]
    assert cp3["files"]["t1"] != cp1["files"]["t1"]


def test_malformed_file_keeps_previous_checkpoint(tmp_path):
    src = tmp_path / "cline"
    _write_task(src, "t1", [_req(tokensIn=1, tokensOut=1)])
    _, cp1, _ = reader_cline.read(src, None, now=NOW)
    _write_task(src, "t1", [], raw="not json at all {{{")
    calls, cp2, diags = reader_cline.read(src, cp1, now=NOW + timedelta(seconds=31))
    assert calls == []
    assert [d.code for d in diags] == ["cline_bad_file"]
    assert cp2["files"]["t1"] == cp1["files"]["t1"]
    # A non-array JSON document is also a bad file.
    _write_task(src, "t1", [], raw=json.dumps({"x": 1}))
    calls, cp3, diags = reader_cline.read(src, cp2, now=NOW + timedelta(seconds=90))
    assert calls == [] and [d.code for d in diags] == ["cline_bad_file"]
    assert cp3["files"]["t1"] == cp1["files"]["t1"]


def test_malformed_file_first_read_has_no_checkpoint(tmp_path):
    src = tmp_path / "cline"
    _write_task(src, "t1", [], raw="[")
    calls, cp, diags = reader_cline.read(src, None, now=NOW)
    assert calls == [] and [d.code for d in diags] == ["cline_bad_file"]
    assert "t1" not in cp.get("files", {})


def test_null_versus_zero(tmp_path):
    src = tmp_path / "cline"
    _write_task(src, "t1", [_req(tokensIn=5, tokensOut=0, cacheReads=0)])
    calls, _, _ = reader_cline.read(src, None, now=NOW)
    assert calls[0]["tokens_returned"] == 0
    assert calls[0]["cache_read"] == 0
    assert calls[0]["cache_write"] is None


def test_missing_source(tmp_path):
    prior = {"files": {"t1": {"size": 1, "mtime_ns": 2, "read_at": NOW.isoformat()}}}
    calls, cp, diags = reader_cline.read(tmp_path / "nope", prior, now=NOW)
    assert calls == []
    assert cp == prior
    assert [d.code for d in diags] == ["cline_source_missing"]
    calls, cp, diags = reader_cline.read(tmp_path / "nope", None, now=NOW)
    assert calls == [] and [d.code for d in diags] == ["cline_source_missing"]

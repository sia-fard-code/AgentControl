from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

from agent_control import storage, tool_calls
from agent_control.tool_calls import (
    COUNTER_FIELDS,
    READER_KEYS,
    SCHEMA,
    TOOLS,
    ToolCall,
    ToolCallCache,
    append_tool_calls,
    load_tool_calls,
    merge_call,
    validate_reader_call,
    validate_tool_call,
)

SENTINEL = "SENTINEL-TEXT-DO-NOT-STORE"
RECORDED = "2026-10-01T20:00:00+00:00"


def reader_call(**overrides: object) -> dict[str, object]:
    call: dict[str, object] = {
        "tool": "claude_code",
        "key": "msg_01:req_01",
        "session": "sess-1",
        "root_session": "sess-1",
        "at": "2026-10-01T19:39:58.648Z",
        "model": "claude-opus-4-1",
        "cwd": r"C:\work\project",
        "tokens_sent": 100,
        "tokens_returned": 50,
        "cache_read": 10,
        "cache_write": 5,
        "cache_write_5m": 5,
        "reasoning": None,
        "sent_includes_cache_read": False,
        "speed": "standard",
    }
    call.update(overrides)
    return call


def test_contract_constants() -> None:
    assert TOOLS == ("claude_code", "codex", "cline", "kilo")
    assert SCHEMA == 1
    assert set(COUNTER_FIELDS) <= set(READER_KEYS)
    assert len(READER_KEYS) == 15


def test_valid_reader_call_passes_and_at_is_normalized() -> None:
    source = reader_call()
    result = validate_reader_call(source)

    assert result is not source
    assert result["at"] == storage.normalize_utc("2026-10-01T19:39:58.648Z")
    assert result["at"].endswith("+00:00")
    assert source["at"] == "2026-10-01T19:39:58.648Z"
    assert {k: v for k, v in result.items() if k != "at"} == {
        k: v for k, v in source.items() if k != "at"
    }


def _with_extra() -> dict[str, object]:
    call = reader_call()
    call["title"] = "x"
    return call


def _without_model() -> dict[str, object]:
    call = reader_call()
    del call["model"]
    return call


@pytest.mark.parametrize(
    "call",
    [
        _with_extra(),
        _without_model(),
        reader_call(tokens_sent=True),
        reader_call(cache_read=-1),
        reader_call(key="has space"),
        reader_call(model="claude opus"),
        reader_call(cwd="C:\\work\nproject"),
        reader_call(speed="Fast mode!"),
        reader_call(tool="other"),
        reader_call(at="not a time"),
        reader_call(sent_includes_cache_read=1),
        reader_call(session=""),
        reader_call(tokens_returned=1.5),
    ],
    ids=[
        "extra-key", "missing-key", "bool-counter", "negative-counter",
        "key-space", "model-space", "cwd-newline", "speed-text", "tool-other",
        "bad-at", "flag-int", "empty-session", "float-counter",
    ],
)
def test_invalid_reader_calls_are_rejected(call: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        validate_reader_call(call)


@pytest.mark.parametrize(
    "field", ["key", "session", "root_session", "model", "cwd", "speed", "at", "tool"]
)
def test_value_error_never_contains_the_sentinel(field: str) -> None:
    bad = SENTINEL + " with a sentence\n"
    with pytest.raises(ValueError) as info:
        validate_reader_call(reader_call(**{field: bad}))
    assert SENTINEL not in str(info.value)
    assert field in str(info.value)


def test_value_error_for_extra_key_never_contains_the_sentinel() -> None:
    call = reader_call()
    call[SENTINEL] = SENTINEL
    with pytest.raises(ValueError) as info:
        validate_reader_call(call)
    assert SENTINEL not in str(info.value)


def stored(**overrides: object) -> dict[str, object]:
    record = dict(validate_reader_call(reader_call()))
    record.update(
        schema=1, revision=1, project_id="proj-1", recorded_at=RECORDED
    )
    record.update(overrides)
    return record


def test_validate_tool_call_accepts_a_stored_record() -> None:
    call = validate_tool_call(stored(recorded_at="2026-10-01T20:00:00Z"))
    assert isinstance(call, ToolCall)
    assert call.recorded_at == RECORDED
    assert call.as_json()["recorded_at"] == RECORDED
    assert validate_tool_call(call.as_json()) == call


@pytest.mark.parametrize(
    "overrides",
    [
        {"schema": 2},
        {"revision": 0},
        {"revision": True},
        {"project_id": "has space"},
        {"project_id": "a" * 65},
        {"project_id": 7},
        {"recorded_at": "nope"},
    ],
)
def test_validate_tool_call_rejects_bad_stored_fields(overrides: dict) -> None:
    with pytest.raises(ValueError):
        validate_tool_call(stored(**overrides))


def test_validate_tool_call_rejects_extra_and_missing_keys() -> None:
    extra = stored()
    extra["note"] = "x"
    with pytest.raises(ValueError):
        validate_tool_call(extra)
    missing = stored()
    del missing["recorded_at"]
    with pytest.raises(ValueError):
        validate_tool_call(missing)


def test_merge_into_nothing_gives_revision_one() -> None:
    call = merge_call(None, reader_call(), project_id="proj-1", recorded_at=RECORDED)

    assert call is not None
    assert call.revision == 1
    assert call.schema == 1
    assert call.project_id == "proj-1"
    assert call.recorded_at == RECORDED
    assert call.at == storage.normalize_utc("2026-10-01T19:39:58.648Z")
    assert call.tokens_sent == 100


def test_merge_identical_data_gives_none() -> None:
    current = merge_call(None, reader_call(), project_id="proj-1", recorded_at=RECORDED)
    again = merge_call(
        current, reader_call(), project_id="proj-1",
        recorded_at="2026-10-02T00:00:00+00:00",
    )
    assert again is None


def test_merge_counters_take_the_larger_known_value() -> None:
    current = merge_call(None, reader_call(), project_id="proj-1", recorded_at=RECORDED)
    later = "2026-10-02T00:00:00+00:00"

    grown = merge_call(
        current, reader_call(tokens_returned=80), project_id="proj-1",
        recorded_at=later,
    )
    assert grown is not None
    assert grown.revision == 2
    assert grown.tokens_returned == 80
    assert grown.recorded_at == later

    assert merge_call(
        grown, reader_call(tokens_returned=20), project_id="proj-1",
        recorded_at=later,
    ) is None
    assert merge_call(
        grown, reader_call(tokens_returned=None, cache_read=None),
        project_id="proj-1", recorded_at=later,
    ) is None

    filled = merge_call(
        grown, reader_call(reasoning=12, tokens_sent=None), project_id="proj-1",
        recorded_at=later,
    )
    assert filled is not None
    assert filled.revision == 3
    assert filled.reasoning == 12
    assert filled.tokens_sent == 100
    assert filled.tokens_returned == 80


def test_merge_fills_nulls_and_keeps_known_fields() -> None:
    current = merge_call(
        None, reader_call(model=None, cwd=None), project_id=None,
        recorded_at=RECORDED,
    )
    assert current is not None
    assert current.model is None and current.cwd is None
    assert current.project_id is None

    incoming = reader_call(
        model="gpt-5", cwd=r"C:\other", session="sess-2", root_session="sess-0",
        at="2026-10-03T00:00:00Z", speed="fast",
    )
    filled = merge_call(current, incoming, project_id="proj-2", recorded_at=RECORDED)
    assert filled is not None
    assert filled.revision == 2
    assert filled.model == "gpt-5"
    assert filled.cwd == r"C:\other"
    assert filled.project_id == "proj-2"
    assert filled.session == "sess-1"
    assert filled.root_session == "sess-1"
    assert filled.at == current.at
    assert filled.speed == "standard"

    kept = merge_call(
        filled, reader_call(model="other-model", cwd=r"C:\third"),
        project_id="proj-3", recorded_at=RECORDED,
    )
    assert kept is None


def test_merge_rejects_invalid_incoming() -> None:
    with pytest.raises(ValueError):
        merge_call(None, reader_call(key="a b"), project_id=None, recorded_at=RECORDED)


def _jsonl(path: Path, records: list[object]) -> None:
    path.write_text(
        "".join(
            (r if isinstance(r, str) else json.dumps(r)) + "\n" for r in records
        ),
        encoding="utf-8",
    )


def test_load_keeps_highest_revision_and_later_line_on_ties(tmp_path: Path) -> None:
    path = tmp_path / "tool_usage.jsonl"
    _jsonl(
        path,
        [
            stored(revision=2, tokens_returned=70),
            stored(revision=1, tokens_returned=50),
            stored(key="k2", revision=1, tokens_returned=1),
            stored(key="k2", revision=1, tokens_returned=2),
            stored(tool="codex", revision=1, tokens_returned=9),
        ],
    )

    calls, diags = load_tool_calls(path)

    assert diags == []
    assert calls[("claude_code", "msg_01:req_01")].tokens_returned == 70
    assert calls[("claude_code", "msg_01:req_01")].revision == 2
    assert calls[("claude_code", "k2")].tokens_returned == 2
    assert calls[("codex", "msg_01:req_01")].tokens_returned == 9
    assert len(calls) == 3


def test_load_reports_bad_lines_without_content(tmp_path: Path) -> None:
    path = tmp_path / "tool_usage.jsonl"
    _jsonl(
        path,
        [
            stored(),
            "{broken " + SENTINEL,
            stored(key="k2", model=SENTINEL + " text"),
            [SENTINEL],
            stored(key="k3"),
        ],
    )

    calls, diags = load_tool_calls(path)

    assert sorted(k for _, k in calls) == ["k3", "msg_01:req_01"]
    assert [(d.code, d.line) for d in diags] == [
        ("tool_usage_bad_line", 2),
        ("tool_usage_bad_line", 3),
        ("tool_usage_bad_line", 4),
    ]
    for diag in diags:
        assert diag.source == str(path)
        assert SENTINEL not in repr(diag)


def test_load_missing_file(tmp_path: Path) -> None:
    assert load_tool_calls(tmp_path / "absent.jsonl") == ({}, [])


def test_append_then_load_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "state" / "tool_usage.jsonl"
    path.parent.mkdir()
    first = merge_call(None, reader_call(), project_id="proj-1", recorded_at=RECORDED)
    second = merge_call(
        first, reader_call(tokens_returned=99), project_id="proj-1",
        recorded_at=RECORDED,
    )
    other = merge_call(
        None, reader_call(tool="kilo", key="m-2", model=None, cwd=None),
        project_id=None, recorded_at=RECORDED,
    )
    assert first and second and other
    lock = storage.StateLocks().write

    append_tool_calls(path, [first], lock)
    append_tool_calls(path, [second, other], lock)

    calls, diags = load_tool_calls(path)
    assert diags == []
    assert calls == {("claude_code", "msg_01:req_01"): second, ("kilo", "m-2"): other}
    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 3
    assert json.loads(lines[0]) == first.as_json()


def _count_parses(monkeypatch: pytest.MonkeyPatch) -> list[bytes]:
    seen: list[bytes] = []
    real = tool_calls._parse_records

    def counting(data: bytes, **kwargs: object):
        seen.append(bytes(data))
        return real(data, **kwargs)

    monkeypatch.setattr(tool_calls, "_parse_records", counting)
    return seen


def test_cache_skips_an_unchanged_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "tool_usage.jsonl"
    _jsonl(path, [stored(), stored(key="k2")])
    seen = _count_parses(monkeypatch)
    cache = ToolCallCache(path)

    first, _ = cache.load()
    second, _ = cache.load()

    assert len(seen) == 1
    assert first == second
    assert len(first) == 2


def test_cache_parses_only_the_appended_tail(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "tool_usage.jsonl"
    lock = storage.StateLocks().write
    first = validate_tool_call(stored())
    append_tool_calls(path, [first], lock)
    cache = ToolCallCache(path)
    cache.load()
    before = path.stat().st_size
    seen = _count_parses(monkeypatch)

    newer = validate_tool_call(stored(revision=2, tokens_returned=500))
    append_tool_calls(path, [newer], lock)
    calls, diags = cache.load()

    assert diags == []
    assert len(seen) == 1
    assert seen[0] == path.read_bytes()[before:]
    assert calls[("claude_code", "msg_01:req_01")] == newer


def test_cache_reports_tail_bad_lines_with_absolute_numbers(tmp_path: Path) -> None:
    path = tmp_path / "tool_usage.jsonl"
    _jsonl(path, [stored(), stored(key="k2")])
    cache = ToolCallCache(path)
    cache.load()
    with path.open("a", encoding="utf-8") as handle:
        handle.write("{broken " + SENTINEL + "\n")

    _, diags = cache.load()

    assert [(d.code, d.line) for d in diags] == [("tool_usage_bad_line", 3)]
    assert SENTINEL not in repr(diags)


def test_cache_reloads_a_rewritten_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "tool_usage.jsonl"
    _jsonl(path, [stored(key="k1"), stored(key="k2")])
    cache = ToolCallCache(path)
    cache.load()
    seen = _count_parses(monkeypatch)

    _jsonl(path, [stored(key="k9", tokens_sent=1), stored(key="k8"), stored(key="k7")])
    calls, _ = cache.load()

    assert len(seen) == 1
    assert seen[0] == path.read_bytes()
    assert sorted(k for _, k in calls) == ["k7", "k8", "k9"]

    path.unlink()
    assert cache.load() == ({}, [])

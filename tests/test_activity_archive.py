from __future__ import annotations

import json
from pathlib import Path

from agent_control.activity import (
    build_activity_snapshot,
    load_activity_archive,
    write_activity_archive,
)

_FORBIDDEN = ("prompt", "assistant", "tool_result", "content", "reasoning", "SECRET")


def _make_run(tmp_path: Path) -> Path:
    transcript = tmp_path / "run.md"
    transcript.write_text(
        "### [1] thinking\nSECRET private reasoning\n"
        '### [1] read_file({"path": "AGENTS.md"})\n'
        '### [2] run_command({"command": "SECRET pytest"})\n'
        '### [2] write_file({"path": "x.py", "content": "SECRET body"})\n',
        encoding="utf-8",
    )
    (tmp_path / "run.usage.jsonl").write_text(
        '{"iter":1,"ts":"2026-09-18T10:00:00Z","sent":100,"recv":10,"cache_read":0,"cache_write":0}\n'
        '{"iter":2,"ts":"2026-09-18T10:00:12Z","sent":300,"recv":20,"cache_read":50,"cache_write":0}\n',
        encoding="utf-8",
    )
    return transcript


def test_snapshot_is_sanitized(tmp_path: Path) -> None:
    transcript = _make_run(tmp_path)
    snap = build_activity_snapshot(transcript)
    blob = json.dumps(snap)
    for token in _FORBIDDEN:
        assert token not in blob
    tools = [s["tool"] for s in snap["steps"] if s["phase"] == "tool"]
    assert tools == ["read_file", "run_command", "write_file"]
    # run_command must not expose its command string, only that a command ran
    run = next(s for s in snap["steps"] if s["tool"] == "run_command")
    assert run["path"] is None
    assert snap["tool_count"] == 3


def test_write_then_load_roundtrip(tmp_path: Path) -> None:
    transcript = _make_run(tmp_path)
    snap = build_activity_snapshot(transcript)
    archive = tmp_path / "archive" / "run.json"
    write_activity_archive(archive, snap)
    loaded = load_activity_archive(archive)
    assert loaded is not None
    assert loaded["tool_count"] == 3
    assert loaded["steps"][0]["iteration"] == 1


def test_load_missing_archive_is_none(tmp_path: Path) -> None:
    assert load_activity_archive(tmp_path / "nope.json") is None


def test_snapshot_missing_transcript_is_empty(tmp_path: Path) -> None:
    snap = build_activity_snapshot(tmp_path / "gone.md")
    assert snap["steps"] == []
    assert snap["tool_count"] == 0

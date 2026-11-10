from __future__ import annotations

import json
from pathlib import Path

from agent_control.activity import build_activity_snapshot, iteration_metrics


def _write_journal(path: Path, rows: list[dict]) -> None:
    path.write_text(
        "".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8"
    )


def test_iteration_metrics_compute_duration_tokens(tmp_path: Path) -> None:
    journal = tmp_path / "r.usage.jsonl"
    _write_journal(
        journal,
        [
            {"iter": 1, "phase": "loop", "ts": "2026-09-16T19:14:07Z", "sent": 100, "recv": 10, "cache_read": 40, "cache_write": 5},
            {"iter": 2, "phase": "loop", "ts": "2026-09-16T19:14:17Z", "sent": 200, "recv": 20, "cache_read": 60, "cache_write": 0},
        ],
    )
    metrics = iteration_metrics(journal)
    assert metrics[1]["tokens_sent"] == 100
    assert metrics[1]["tokens_returned"] == 10
    # duration of iter 1 = seconds until iter 2 began
    assert metrics[1]["duration_seconds"] == 10.0
    # last iteration has no successor, duration is None
    assert metrics[2]["duration_seconds"] is None


def test_iteration_metrics_missing_journal_is_empty(tmp_path: Path) -> None:
    assert iteration_metrics(tmp_path / "nope.usage.jsonl") == {}


def test_iteration_metrics_tolerate_bad_rows(tmp_path: Path) -> None:
    journal = tmp_path / "r.usage.jsonl"
    journal.write_text(
        '{"iter": 1, "ts": "2026-09-16T19:14:07Z", "sent": 5, "recv": 1, "cache_read": 0, "cache_write": 0}\n'
        "{bad json}\n"
        '{"iter": 2, "ts": "2026-09-16T19:14:09Z", "sent": 7, "recv": 2, "cache_read": 0, "cache_write": 0}\n',
        encoding="utf-8",
    )
    metrics = iteration_metrics(journal)
    assert set(metrics) == {1, 2}
    assert metrics[1]["duration_seconds"] == 2.0


def test_iteration_metrics_split_model_and_tool_time(tmp_path: Path) -> None:
    journal = tmp_path / "r.usage.jsonl"
    _write_journal(
        journal,
        [
            {
                "iter": 1,
                "started": "2026-09-16T19:14:00Z",
                "ts": "2026-09-16T19:14:07Z",
                "sent": 100,
                "recv": 10,
                "cache_read": 0,
                "cache_write": 0,
            },
            {
                "iter": 2,
                "started": "2026-09-16T19:14:10Z",
                "ts": "2026-09-16T19:14:17Z",
                "sent": 200,
                "recv": 20,
                "cache_read": 0,
                "cache_write": 0,
            },
        ],
    )
    metrics = iteration_metrics(journal)
    assert metrics[1]["model_started_at"] == "2026-09-16T19:14:00+00:00"
    assert metrics[1]["replied_at"] == "2026-09-16T19:14:07+00:00"
    # model_seconds = ts - started
    assert metrics[1]["model_seconds"] == 7.0
    # tool_seconds = next.started - this.ts
    assert metrics[1]["tool_seconds"] == 3.0
    # duration_seconds unchanged (next.ts - this.ts)
    assert metrics[1]["duration_seconds"] == 10.0
    # last row has model time but no successor -> tool_seconds None
    assert metrics[2]["model_seconds"] == 7.0
    assert metrics[2]["tool_seconds"] is None


def test_iteration_metrics_without_started_keep_duration(tmp_path: Path) -> None:
    journal = tmp_path / "r.usage.jsonl"
    _write_journal(
        journal,
        [
            {"iter": 1, "ts": "2026-09-16T19:14:07Z", "sent": 100, "recv": 10, "cache_read": 0, "cache_write": 0},
            {"iter": 2, "ts": "2026-09-16T19:14:17Z", "sent": 200, "recv": 20, "cache_read": 0, "cache_write": 0},
        ],
    )
    metrics = iteration_metrics(journal)
    assert metrics[1]["duration_seconds"] == 10.0
    assert metrics[1]["model_started_at"] is None
    assert metrics[1]["model_seconds"] is None
    assert metrics[1]["tool_seconds"] is None


def test_build_activity_snapshot_carries_timing(tmp_path: Path) -> None:
    transcript = tmp_path / "run.md"
    transcript.write_text(
        '### [1] read_file({"path": "a"})\n'
        '### [2] read_file({"path": "b"})\n',
        encoding="utf-8",
    )
    journal = transcript.with_suffix(".usage.jsonl")
    _write_journal(
        journal,
        [
            {
                "iter": 1,
                "started": "2026-09-16T19:14:00Z",
                "ts": "2026-09-16T19:14:07Z",
                "sent": 100,
                "recv": 10,
                "cache_read": 0,
                "cache_write": 0,
            },
            {
                "iter": 2,
                "started": "2026-09-16T19:14:10Z",
                "ts": "2026-09-16T19:14:17Z",
                "sent": 200,
                "recv": 20,
                "cache_read": 0,
                "cache_write": 0,
            },
        ],
    )
    snap = build_activity_snapshot(transcript)
    by_iter = {s["iteration"]: s for s in snap["steps"]}
    assert by_iter[1]["model_seconds"] == 7.0
    assert by_iter[1]["tool_seconds"] == 3.0
    assert by_iter[2]["model_seconds"] == 7.0
    assert by_iter[2]["tool_seconds"] is None
    # totals: model 7+7=14, tool only iter1's 3
    assert snap["model_seconds_total"] == 14.0
    assert snap["tool_seconds_total"] == 3.0

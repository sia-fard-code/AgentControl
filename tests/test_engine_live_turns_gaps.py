import json

import pytest

from test_engine_reasoning import RESPONSES_USAGE, engine, harness  # noqa: F401
from test_engine_live_turns_gpt import finish_turn as gpt_finish
from test_engine_live_turns_claude import (
    chunk,
    finish_turn as claude_finish,
    CLAUDE,
    USAGE,
)


@pytest.mark.parametrize("status", [408, 429])
def test_temporary_http_errors_keep_streaming(engine, harness, monkeypatch, status):
    delays = []
    monkeypatch.setattr(engine.time, "sleep", delays.append)
    transcript = harness.run(
        "-m", "gpt-6-sol",
        replies=[],
        stream_replies=[
            [engine.StreamError("http", f"HTTP {status}: rate limited", 0)],
            gpt_finish(),
        ],
    )
    assert len(harness.stream_calls) == 2
    assert all(path == "/openai/responses" for path, _ in harness.stream_calls)
    assert not harness.payloads
    assert "streaming unavailable" not in transcript.lower()
    assert len(delays) == 1 and 1.5 <= delays[0] <= 2.5
    rows = [
        json.loads(line)
        for line in harness.transcript_path.with_suffix(".usage.jsonl")
        .read_text(encoding="utf-8").splitlines()
    ]
    assert len(rows) == 1
    assert rows[0]["sent"] == RESPONSES_USAGE["input_tokens"]
    assert rows[0]["recv"] == RESPONSES_USAGE["output_tokens"]


@pytest.mark.parametrize("failure", ["eof", "transport", "idle"])
def test_broken_claude_stream_usage_counts_once(engine, harness, tmp_path, failure):
    broken = [chunk("x"), {"choices": [], "usage": USAGE}]
    if failure != "eof":
        broken.append(engine.StreamError(failure, "stream broke", 2))
    harness.run(
        "--model", CLAUDE,
        replies=[],
        stream_replies=[broken, claude_finish()],
    )
    assert len(harness.stream_calls) == 2
    assert not harness.payloads
    rows = [
        json.loads(line)
        for line in harness.transcript_path.with_suffix(".usage.jsonl")
        .read_text(encoding="utf-8").splitlines()
    ]
    assert len(rows) == 2
    assert all(row["phase"] == "loop" for row in rows)
    assert all(row["sent"] == USAGE["prompt_tokens"] for row in rows)
    assert all(row["recv"] == USAGE["completion_tokens"] for row in rows)
    end = json.loads((tmp_path / "index.jsonl").read_text().splitlines()[-1])
    assert end["sent"] == sum(row["sent"] for row in rows)
    assert end["recv"] == sum(row["recv"] for row in rows)


def test_broken_claude_stream_without_usage_adds_unknown_row(harness, tmp_path):
    # The cut stream generated output, so it is a real attempt with unknown usage.
    harness.run(
        "--model", CLAUDE,
        replies=[],
        stream_replies=[[chunk("x")], claude_finish()],
    )
    assert len(harness.stream_calls) == 2
    rows = [
        json.loads(line)
        for line in harness.transcript_path.with_suffix(".usage.jsonl")
        .read_text(encoding="utf-8").splitlines()
    ]
    assert len(rows) == 2
    assert rows[0]["sent"] is None and rows[0]["recv"] is None
    assert rows[1]["sent"] == USAGE["prompt_tokens"]
    assert rows[1]["recv"] == USAGE["completion_tokens"]
    end = json.loads((tmp_path / "index.jsonl").read_text().splitlines()[-1])
    assert end["sent"] is None and end["recv"] is None

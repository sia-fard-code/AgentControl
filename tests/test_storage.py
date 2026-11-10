import json
import threading
from pathlib import Path

import pytest

from agent_control.storage import (
    append_jsonl,
    atomic_write_json,
    read_jsonl,
    strict_json_loads,
)


@pytest.mark.parametrize("raw", ['{"n":NaN}', '{"n":Infinity}', '{"n":-Infinity}'])
def test_strict_json_rejects_non_finite(raw: str) -> None:
    with pytest.raises(ValueError, match="finite"):
        strict_json_loads(raw)


def test_read_jsonl_is_tolerant(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    path.write_text('{"event":"start"}\n{broken\n[]\n', encoding="utf-8")
    rows, diagnostics = read_jsonl(path, source="index")
    assert rows == [{"event": "start"}]
    assert [item.code for item in diagnostics] == [
        "malformed_json",
        "non_object_json",
    ]


def test_read_jsonl_skips_malformed_utf8_without_losing_later_rows(
    tmp_path: Path,
) -> None:
    path = tmp_path / "events.jsonl"
    path.write_bytes(b'{"event":"start"}\n\xff\n{"event":"end"}\n')

    rows, diagnostics = read_jsonl(path, source="index")

    assert rows == [{"event": "start"}, {"event": "end"}]
    assert [item.code for item in diagnostics] == ["malformed_json"]
    assert diagnostics[0].line == 2


def test_append_and_atomic_write_use_fsync(tmp_path: Path, monkeypatch) -> None:
    calls: list[int] = []
    monkeypatch.setattr("agent_control.storage.os.fsync", calls.append)
    lock = threading.RLock()
    rows = tmp_path / "rows.jsonl"
    state = tmp_path / "state.json"

    append_jsonl(rows, [{"run_id": "r"}], lock)
    atomic_write_json(state, {"offset": 1}, lock)

    assert json.loads(rows.read_text()) == {"run_id": "r"}
    assert json.loads(state.read_text()) == {"offset": 1}
    assert len(calls) == 2
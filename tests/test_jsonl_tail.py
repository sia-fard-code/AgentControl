from __future__ import annotations

import json
import os
from pathlib import Path

from agent_control.jsonl_tail import tail_jsonl

SENTINEL = "SENTINEL-TEXT-DO-NOT-STORE"


def _line(n: int, pad: str = "") -> str:
    return json.dumps({"n": n, "pad": pad}) + "\n"


def _write(path: Path, text: str) -> None:
    path.write_bytes(text.encode("utf-8"))


def _append(path: Path, text: str) -> None:
    with path.open("ab") as handle:
        handle.write(text.encode("utf-8"))


def _bump_mtime(path: Path, seconds: int = 5) -> None:
    st = path.stat()
    newer = st.st_mtime_ns + seconds * 1_000_000_000
    os.utime(path, ns=(newer, newer))


def test_first_read_returns_every_row_with_line_numbers(tmp_path: Path) -> None:
    path = tmp_path / "a.jsonl"
    _write(path, _line(1) + _line(2) + _line(3))

    rows, state, diags, restarted = tail_jsonl(path, None)

    assert [(n, row["n"]) for n, row in rows] == [(1, 1), (2, 2), (3, 3)]
    assert diags == []
    assert restarted is False
    assert state["offset"] == path.stat().st_size
    assert state["lines"] == 3
    assert state["size"] == path.stat().st_size
    assert state["head_len"] == path.stat().st_size
    assert isinstance(state["head"], str) and len(state["head"]) == 64
    json.dumps(state)


def test_append_returns_only_new_rows_with_absolute_numbers(tmp_path: Path) -> None:
    path = tmp_path / "a.jsonl"
    _write(path, _line(1) + _line(2))
    _, state, _, _ = tail_jsonl(path, None)

    _append(path, _line(3) + _line(4))
    _bump_mtime(path)
    rows, state2, diags, restarted = tail_jsonl(path, state)

    assert [(n, row["n"]) for n, row in rows] == [(3, 3), (4, 4)]
    assert diags == []
    assert restarted is False
    assert state2["lines"] == 4


def test_partial_last_line_waits_until_complete(tmp_path: Path) -> None:
    path = tmp_path / "a.jsonl"
    full = _line(2)
    _write(path, _line(1) + full[:5])

    rows, state, diags, _ = tail_jsonl(path, None)
    assert [n for n, _ in rows] == [1]
    assert diags == []
    assert state["offset"] == len(_line(1).encode())

    _append(path, full[5:])
    _bump_mtime(path)
    rows, state, diags, restarted = tail_jsonl(path, state)
    assert [(n, row["n"]) for n, row in rows] == [(2, 2)]
    assert diags == []
    assert restarted is False
    assert state["offset"] == path.stat().st_size


def test_unchanged_file_returns_nothing(tmp_path: Path) -> None:
    path = tmp_path / "a.jsonl"
    _write(path, _line(1))
    _, state, _, _ = tail_jsonl(path, None)

    rows, state2, diags, restarted = tail_jsonl(path, state)

    assert rows == []
    assert diags == []
    assert restarted is False
    assert state2 == state


def test_truncated_file_restarts(tmp_path: Path) -> None:
    path = tmp_path / "a.jsonl"
    _write(path, _line(1) + _line(2) + _line(3))
    _, state, _, _ = tail_jsonl(path, None)

    _write(path, _line(7))
    _bump_mtime(path)
    rows, state2, diags, restarted = tail_jsonl(path, state)

    assert restarted is True
    assert [(n, row["n"]) for n, row in rows] == [(1, 7)]
    assert diags == []
    assert state2["lines"] == 1


def test_same_size_with_different_first_bytes_restarts(tmp_path: Path) -> None:
    path = tmp_path / "a.jsonl"
    _write(path, _line(1) + _line(2))
    _, state, _, _ = tail_jsonl(path, None)

    _write(path, _line(8) + _line(9))
    _bump_mtime(path)
    assert path.stat().st_size == state["size"]
    rows, state2, diags, restarted = tail_jsonl(path, state)

    assert restarted is True
    assert [(n, row["n"]) for n, row in rows] == [(1, 8), (2, 9)]
    assert diags == []
    assert state2["lines"] == 2


def test_growth_past_head_window_is_not_a_restart(tmp_path: Path) -> None:
    path = tmp_path / "a.jsonl"
    first = _line(1, "x" * 80)
    _write(path, first)
    assert 90 <= len(first) <= 110
    rows, state, _, restarted = tail_jsonl(path, None)
    assert restarted is False
    assert len(rows) == 1

    n = 1
    seen = [1]
    while path.stat().st_size < 10_000:
        chunk = "".join(_line(n + i, "y" * 300) for i in range(1, 4))
        n += 3
        _append(path, chunk)
        _bump_mtime(path)
        rows, state, diags, restarted = tail_jsonl(path, state)
        assert restarted is False
        assert diags == []
        seen.extend(num for num, _ in rows)

    assert seen == list(range(1, n + 1))
    assert state["head_len"] == 4096
    assert state["lines"] == n


def test_crlf_lines_are_handled(tmp_path: Path) -> None:
    path = tmp_path / "a.jsonl"
    path.write_bytes(b'{"n": 1}\r\n\r\n{"n": 2}\r\n')

    rows, state, diags, _ = tail_jsonl(path, None)

    assert [(n, row["n"]) for n, row in rows] == [(1, 1), (3, 2)]
    assert diags == []
    assert state["offset"] == path.stat().st_size


def test_bad_json_line_gives_diagnostic_without_content(tmp_path: Path) -> None:
    path = tmp_path / "a.jsonl"
    _write(
        path,
        _line(1)
        + "{not json " + SENTINEL + "\n"
        + json.dumps([SENTINEL]) + "\n"
        + _line(4),
    )

    rows, _, diags, _ = tail_jsonl(path, None)

    assert [n for n, _ in rows] == [1, 4]
    assert [(d.code, d.line) for d in diags] == [
        ("jsonl_bad_line", 2),
        ("jsonl_bad_line", 3),
    ]
    for diag in diags:
        assert SENTINEL not in repr(diag)
        assert diag.source == str(path)


def test_vanished_file_gives_missing(tmp_path: Path) -> None:
    path = tmp_path / "a.jsonl"
    _write(path, _line(1))
    _, state, _, _ = tail_jsonl(path, None)
    path.unlink()

    rows, state2, diags, restarted = tail_jsonl(path, state)

    assert rows == []
    assert restarted is False
    assert state2 == state
    assert [d.code for d in diags] == ["jsonl_missing"]

    rows, state3, diags, restarted = tail_jsonl(path, None)
    assert rows == [] and state3 == {} and restarted is False
    assert [d.code for d in diags] == ["jsonl_missing"]

"""Incremental reader for append-only JSONL files.

Only complete lines are consumed. The returned state is JSON-serializable and
lets the next call resume from the byte offset after the last complete line.
Diagnostics carry the path and line number, never file content.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Mapping

from .models import Diagnostic

HEAD_WINDOW = 4096
_STATE_INTS = ("size", "mtime_ns", "offset", "lines", "head_len")


def _valid_state(state: Mapping[str, object] | None) -> dict[str, object] | None:
    if not isinstance(state, Mapping):
        return None
    for name in _STATE_INTS:
        value = state.get(name)
        if type(value) is not int or value < 0:
            return None
    if not isinstance(state.get("head"), str):
        return None
    return dict(state)


def _head_hash(handle, length: int) -> str:
    handle.seek(0)
    return hashlib.sha256(handle.read(length)).hexdigest()


def tail_jsonl(
    path: Path, state: Mapping[str, object] | None
) -> tuple[list[tuple[int, dict[str, object]]], dict[str, object], list[Diagnostic], bool]:
    source = str(path)
    try:
        stat = path.stat()
    except FileNotFoundError:
        missing = Diagnostic("jsonl_missing", "file not found", source=source)
        return [], dict(state or {}), [missing], False

    previous = _valid_state(state)
    if (
        previous is not None
        and previous["size"] == stat.st_size
        and previous["mtime_ns"] == stat.st_mtime_ns
    ):
        return [], previous, [], False

    size = stat.st_size
    rows: list[tuple[int, dict[str, object]]] = []
    diagnostics: list[Diagnostic] = []
    restarted = False
    try:
        with path.open("rb") as handle:
            offset = 0
            lines = 0
            if previous is not None:
                offset = int(previous["offset"])
                lines = int(previous["lines"])
                head_len = int(previous["head_len"])
                if size < offset or (
                    head_len > size or _head_hash(handle, head_len) != previous["head"]
                ):
                    offset, lines, restarted = 0, 0, True
            handle.seek(offset)
            data = handle.read(max(0, size - offset))
            end = data.rfind(b"\n")
            consumed = data[: end + 1] if end >= 0 else b""
            for raw in consumed.split(b"\n")[:-1]:
                lines += 1
                if raw.endswith(b"\r"):
                    raw = raw[:-1]
                text = raw.decode("utf-8", errors="replace")
                if not text.strip():
                    continue
                try:
                    value = json.loads(text)
                except (ValueError, RecursionError):
                    value = None
                if not isinstance(value, dict):
                    diagnostics.append(
                        Diagnostic(
                            "jsonl_bad_line",
                            "line is not a JSON object",
                            source=source,
                            line=lines,
                        )
                    )
                    continue
                rows.append((lines, value))
            offset += len(consumed)
            head_len = min(HEAD_WINDOW, offset)
            head = _head_hash(handle, head_len)
    except FileNotFoundError:
        missing = Diagnostic("jsonl_missing", "file not found", source=source)
        return [], dict(state or {}), [missing], False

    new_state: dict[str, object] = {
        "size": size,
        "mtime_ns": stat.st_mtime_ns,
        "offset": offset,
        "lines": lines,
        "head": head,
        "head_len": head_len,
    }
    return rows, new_state, diagnostics, restarted

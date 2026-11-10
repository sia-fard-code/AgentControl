"""Kilo reader: one call per assistant message row in Kilo's SQLite store.

Only the fields listed in the tool-usage design (section 4.1) are kept. Message
text, titles, summaries and parts are never selected or returned.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path

from .models import Diagnostic

TOOL = "kilo"
_BUSY_TIMEOUT_SECONDS = 5.0
_MAX_DEPTH = 32
# checkpoint: {"updated_after": int_ms, "ids_at": [message ids whose time_updated == updated_after]}

_MESSAGE_QUERY = (
    "select id, session_id, time_created, time_updated, data from message "
    "where time_updated >= ? order by time_updated, id"
)
_SESSION_QUERY = "select id, parent_id, directory from session"


def _int_or_none(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _str_or_none(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _mapping(value: object) -> Mapping[str, object]:
    return value if isinstance(value, Mapping) else {}


def _parse_checkpoint(checkpoint: Mapping[str, object] | None) -> tuple[int | None, set[str]]:
    if not isinstance(checkpoint, Mapping):
        return None, set()
    updated_after = _int_or_none(checkpoint.get("updated_after"))
    if updated_after is None:
        return None, set()
    raw_ids = checkpoint.get("ids_at")
    ids = {i for i in raw_ids if isinstance(i, str)} if isinstance(raw_ids, list) else set()
    return updated_after, ids


def _ms_to_iso(ms: int) -> str | None:
    try:
        return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat()
    except (OverflowError, OSError, ValueError):
        return None


def _root_session(session_id: str, sessions: Mapping[str, tuple[object, object]]) -> str:
    if session_id not in sessions:
        return session_id
    current = session_id
    seen = {current}
    for _ in range(_MAX_DEPTH):
        parent = sessions[current][0]
        if not isinstance(parent, str) or parent not in sessions or parent in seen:
            break
        current = parent
        seen.add(current)
    return current


def _tokens_returned(output: int | None, reasoning: int | None) -> int | None:
    if output is None:
        return None
    if reasoning is None:
        return output
    return output + reasoning


def _call(
    message_id: str,
    session_id: str,
    data: Mapping[str, object],
    sessions: Mapping[str, tuple[object, object]],
) -> dict[str, object] | None:
    if data.get("role") != "assistant":
        return None
    tokens = data.get("tokens")
    if not isinstance(tokens, Mapping):
        return None
    times = _mapping(data.get("time"))
    at_ms = _int_or_none(times.get("completed"))
    if at_ms is None:
        at_ms = _int_or_none(times.get("created"))
    at = _ms_to_iso(at_ms) if at_ms is not None else None
    if at is None:
        return None
    cwd = _str_or_none(_mapping(data.get("path")).get("cwd"))
    if cwd is None and session_id in sessions:
        cwd = _str_or_none(sessions[session_id][1])
    cache = _mapping(tokens.get("cache"))
    output = _int_or_none(tokens.get("output"))
    reasoning = _int_or_none(tokens.get("reasoning"))
    return {
        "tool": TOOL,
        "key": message_id,
        "session": session_id,
        "root_session": _root_session(session_id, sessions),
        "at": at,
        "model": _str_or_none(data.get("modelID")),
        "cwd": cwd,
        "tokens_sent": _int_or_none(tokens.get("input")),
        "tokens_returned": _tokens_returned(output, reasoning),
        "cache_read": _int_or_none(cache.get("read")),
        "cache_write": _int_or_none(cache.get("write")),
        "cache_write_5m": None,
        "reasoning": reasoning,
        "sent_includes_cache_read": False,
        "speed": None,
    }


def read(
    source: Path, checkpoint: Mapping[str, object] | None, *, now: datetime
) -> tuple[list[dict[str, object]], dict[str, object], list[Diagnostic]]:
    """Return assistant-message calls updated since ``checkpoint``.

    ``now`` is part of the shared reader contract; Kilo rows carry their own times.
    """
    unchanged: dict[str, object] = dict(checkpoint) if isinstance(checkpoint, Mapping) else {}
    src = str(source)
    if not source.is_file():
        return [], unchanged, [
            Diagnostic("kilo_source_missing", f"Kilo database not found: {src}", source=src)
        ]
    updated_after, ids_at = _parse_checkpoint(checkpoint)
    try:
        con = sqlite3.connect(
            source.as_uri() + "?mode=ro", uri=True, timeout=_BUSY_TIMEOUT_SECONDS
        )
        try:
            sessions = {
                row[0]: (row[1], row[2])
                for row in con.execute(_SESSION_QUERY)
                if isinstance(row[0], str)
            }
            rows = con.execute(
                _MESSAGE_QUERY, (updated_after if updated_after is not None else -(2**63),)
            ).fetchall()
        finally:
            con.close()
    except sqlite3.DatabaseError as exc:
        return [], unchanged, [
            Diagnostic(
                "kilo_db_unavailable",
                f"Kilo database could not be read ({type(exc).__name__})",
                source=src,
            )
        ]

    calls: dict[str, dict[str, object]] = {}
    diagnostics: list[Diagnostic] = []
    new_max: int | None = None
    new_ids: set[str] = set()
    for message_id, session_id, _created, time_updated, raw in rows:
        stamp = _int_or_none(time_updated)
        if not isinstance(message_id, str) or stamp is None:
            continue
        if stamp == updated_after and message_id in ids_at:
            continue
        if new_max is None or stamp > new_max:
            new_max, new_ids = stamp, set()
        if stamp == new_max:
            new_ids.add(message_id)
        try:
            data = json.loads(raw) if isinstance(raw, (str, bytes)) else None
        except ValueError:
            data = None
        if not isinstance(data, Mapping):
            diagnostics.append(
                Diagnostic("kilo_bad_line", f"Unreadable data in Kilo message {message_id}",
                           source=src)
            )
            continue
        call = _call(message_id, session_id if isinstance(session_id, str) else "",
                     data, sessions)
        if call is not None:
            calls.pop(message_id, None)
            calls[message_id] = call

    if new_max is None:
        return [], unchanged, diagnostics
    if new_max == updated_after:
        new_ids |= ids_at
    return list(calls.values()), {"updated_after": new_max, "ids_at": sorted(new_ids)}, diagnostics

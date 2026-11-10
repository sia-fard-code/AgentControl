"""Append-only, metadata-only log of operator actions.

Each record holds only metadata about a steer, steer-cancel or kill action:
never the steer text, never a hash of it, and never any transcript content.
"""

from __future__ import annotations

import threading
from pathlib import Path

from .storage import read_jsonl

LOG_NAME = "operator-actions.jsonl"
ACTIONS = frozenset({"steer", "steer_cancel", "kill"})

_KNOWN_KEYS = (
    "schema",
    "ts",
    "action",
    "run_id",
    "outcome",
    "kind",
    "chars",
    "engine_pid",
    "children",
    "survivors",
)

_cache: dict[str, tuple[tuple[int, int], list[dict[str, object]]]] = {}
_cache_lock = threading.Lock()


def log_path(state_dir: Path) -> Path:
    return state_dir / LOG_NAME


def append_action(
    state_dir: Path,
    lock,
    *,
    ts: str,
    action: str,
    run_id: str,
    outcome: str,
    kind: str | None = None,
    chars: int | None = None,
    engine_pid: int | None = None,
    children: int | None = None,
    survivors: int | None = None,
) -> dict[str, object]:
    if action not in ACTIONS:
        raise ValueError(f"unknown action: {action!r}")
    if not run_id:
        raise ValueError("run_id must be non-empty")
    if not outcome:
        raise ValueError("outcome must be non-empty")

    from .storage import append_jsonl

    record: dict[str, object] = {
        "schema": 1,
        "ts": ts,
        "action": action,
        "run_id": run_id,
        "outcome": outcome,
        "kind": kind,
        "chars": chars,
        "engine_pid": engine_pid,
        "children": children,
        "survivors": survivors,
    }
    append_jsonl(log_path(state_dir), [record], lock)
    return record


def _is_valid(item: dict[str, object]) -> bool:
    if item.get("schema") != 1:
        return False
    if item.get("action") not in ACTIONS:
        return False
    for field in ("run_id", "ts", "outcome"):
        value = item.get(field)
        if not isinstance(value, str) or not value:
            return False
    return True


def read_actions(state_dir: Path) -> list[dict[str, object]]:
    path = log_path(state_dir)
    key = str(path)
    try:
        stat = path.stat()
        signature = (stat.st_size, stat.st_mtime_ns)
    except OSError:
        with _cache_lock:
            _cache.pop(key, None)
        return []

    with _cache_lock:
        cached = _cache.get(key)
        if cached is not None and cached[0] == signature:
            return cached[1]

    rows, _ = read_jsonl(path, source="operator-actions")
    result: list[dict[str, object]] = []
    for item in rows:
        if not _is_valid(item):
            continue
        result.append({field: item.get(field) for field in _KNOWN_KEYS})

    with _cache_lock:
        _cache[key] = (signature, result)
    return result


def killed_run_ids(actions) -> frozenset[str]:
    return frozenset(
        record["run_id"]
        for record in actions
        if record.get("action") == "kill"
        and record.get("outcome") in {"terminated", "partial"}
    )

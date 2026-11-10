"""Reader for Codex rollout files (spec section 5.3).

Each ``token_usage_record`` is one call. Files written before records existed
fall back to ``event_msg`` ``token_count`` events, one call per change of the
cumulative total. Only ids, timestamps, model names, cwd and token counters
are kept; message text, instructions and other content are never read into
calls, checkpoints or diagnostics.
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Mapping

from .jsonl_tail import _valid_state as _valid_tail
from .jsonl_tail import tail_jsonl
from .models import Diagnostic
from .storage import normalize_utc

TOOL = "codex"
_TURN_MEMORY = 64
_SUBDIRS = ("sessions", "archived_sessions")
_PATTERN = "rollout-*.jsonl"


def _str(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _count(value: object) -> int | None:
    if type(value) is int and value >= 0:
        return value
    return None


def _fresh_state() -> dict[str, object]:
    return {
        "session": None,
        "root": None,
        "cwd": None,
        "model": None,
        "turns": {},
        "has_records": False,
        "last_total": None,
    }


def _load_state(entry: object) -> tuple[dict[str, object], Mapping[str, object] | None]:
    """Return (per-file state, tail state) from a checkpoint entry, defensively."""
    state = _fresh_state()
    if not isinstance(entry, Mapping):
        return state, None
    for name in ("session", "root", "cwd", "model"):
        state[name] = _str(entry.get(name))
    turns: dict[str, list[str | None]] = {}
    raw_turns = entry.get("turns")
    if isinstance(raw_turns, Mapping):
        for turn_id, pair in raw_turns.items():
            if isinstance(turn_id, str) and isinstance(pair, list) and len(pair) == 2:
                turns[turn_id] = [_str(pair[0]), _str(pair[1])]
    while len(turns) > _TURN_MEMORY:
        turns.pop(next(iter(turns)))
    state["turns"] = turns
    state["has_records"] = entry.get("has_records") is True
    last_total = entry.get("last_total")
    state["last_total"] = last_total if type(last_total) is int else None
    tail = entry.get("tail")
    return state, tail if isinstance(tail, Mapping) else None


def _list_files(source: Path) -> list[tuple[str, Path]]:
    found: dict[str, Path] = {}
    for sub in _SUBDIRS:
        base = source / sub
        if not base.is_dir():
            continue
        for path in base.rglob(_PATTERN):
            if path.is_file():
                found[path.relative_to(source).as_posix()] = path
    return sorted(found.items())


def _tokens(use: Mapping[str, object]) -> dict[str, object]:
    return {
        "tokens_sent": _count(use.get("input_tokens")),
        "tokens_returned": _count(use.get("output_tokens")),
        "cache_read": _count(use.get("cached_input_tokens")),
        "cache_write": _count(use.get("cache_write_input_tokens")),
        "cache_write_5m": None,
        "reasoning": _count(use.get("reasoning_output_tokens")),
        "sent_includes_cache_read": True,
        "speed": None,
    }


def _call(key, state, at, model, cwd, use) -> dict[str, object]:
    call: dict[str, object] = {
        "tool": TOOL,
        "key": key,
        "session": state["session"],
        "root_session": state["root"],
        "at": at,
        "model": model,
        "cwd": cwd,
    }
    call.update(_tokens(use))
    return call


def _skipped(source: str, line: int, reason: str) -> Diagnostic:
    return Diagnostic(f"{TOOL}_skipped_line", reason, source=source, line=line)


def _apply_meta(state: dict[str, object], payload: Mapping[str, object]) -> None:
    if state["session"] is not None:
        return  # the first session_meta describes this file's own thread
    session = _str(payload.get("id"))
    if session is None:
        return
    state["session"] = session
    state["root"] = (
        _str(payload.get("session_id")) or _str(payload.get("parent_thread_id")) or session
    )
    cwd = _str(payload.get("cwd"))
    if cwd is not None:
        state["cwd"] = cwd


def _apply_turn(state: dict[str, object], payload: Mapping[str, object]) -> None:
    model = _str(payload.get("model"))
    cwd = _str(payload.get("cwd"))
    if model is not None:
        state["model"] = model
    if cwd is not None:
        state["cwd"] = cwd
    turn_id = _str(payload.get("turn_id"))
    if turn_id is None:
        return
    turns = state["turns"]
    assert isinstance(turns, dict)
    turns.pop(turn_id, None)
    turns[turn_id] = [model, cwd]
    while len(turns) > _TURN_MEMORY:
        turns.pop(next(iter(turns)))


def _process(rows, state, source: str, emit, diagnostics: list[Diagnostic]) -> None:
    if any(row.get("type") == "token_usage_record" for _, row in rows):
        state["has_records"] = True
    for number, row in rows:
        kind = row.get("type")
        payload = row.get("payload")
        if not isinstance(payload, Mapping):
            continue
        if kind == "session_meta":
            _apply_meta(state, payload)
        elif kind == "turn_context":
            _apply_turn(state, payload)
        elif kind == "token_usage_record":
            _record(number, row, payload, state, source, emit, diagnostics)
        elif kind == "event_msg" and payload.get("type") == "token_count":
            if not state["has_records"]:
                _token_count(number, row, payload, state, source, emit, diagnostics)


def _at(row: Mapping[str, object]) -> str | None:
    try:
        return normalize_utc(row.get("timestamp"))  # type: ignore[arg-type]
    except (ValueError, TypeError, OverflowError):
        return None


def _record(number, row, payload, state, source, emit, diagnostics) -> None:
    thread = _str(payload.get("thread_id"))
    response = _str(payload.get("response_id"))
    use = payload.get("usage")
    at = _at(row)
    if thread is None or response is None or not isinstance(use, Mapping) or at is None:
        diagnostics.append(_skipped(source, number, "token_usage_record lacks required fields"))
        return
    turns = state["turns"]
    entry = turns.get(_str(payload.get("turn_id")) or "") if isinstance(turns, dict) else None
    model = (entry[0] if entry else None) or state["model"]
    cwd = (entry[1] if entry else None) or state["cwd"]
    view = dict(state)
    if view["session"] is None:
        view["session"] = thread
    if view["root"] is None:
        view["root"] = _str(payload.get("session_id")) or view["session"]
    emit(_call(f"{thread}:{response}", view, at, model, cwd, use))


def _token_count(number, row, payload, state, source, emit, diagnostics) -> None:
    info = payload.get("info")
    if not isinstance(info, Mapping):
        return  # null info carries no usage
    totals = info.get("total_token_usage")
    total = _count(totals.get("total_tokens")) if isinstance(totals, Mapping) else None
    last = info.get("last_token_usage")
    at = _at(row)
    if total is None or not isinstance(last, Mapping) or at is None or state["session"] is None:
        diagnostics.append(_skipped(source, number, "token_count lacks required fields"))
        return
    if total == state["last_total"]:
        return
    state["last_total"] = total
    emit(_call(f"{state['session']}:tc:{total}", state, at, state["model"], state["cwd"], last))


def read(
    source: Path, checkpoint: Mapping[str, object] | None, *, now: datetime
) -> tuple[list[dict[str, object]], dict[str, object], list[Diagnostic]]:
    del now  # part of the reader contract; Codex files carry their own timestamps
    if not source.is_dir():
        kept = dict(checkpoint) if isinstance(checkpoint, Mapping) else {"files": {}}
        missing = Diagnostic(
            f"{TOOL}_source_missing", "Codex home not found", source=str(source)
        )
        return [], kept, [missing]

    old_files = checkpoint.get("files") if isinstance(checkpoint, Mapping) else None
    if not isinstance(old_files, Mapping):
        old_files = {}
    calls: dict[str, dict[str, object]] = {}
    diagnostics: list[Diagnostic] = []
    files: dict[str, object] = {}

    def emit(call: dict[str, object]) -> None:
        calls.pop(call["key"], None)  # type: ignore[arg-type]
        calls[call["key"]] = call  # type: ignore[index]

    try:
        listed = _list_files(source)
    except OSError as exc:
        bad = Diagnostic(f"{TOOL}_bad_file", type(exc).__name__, source=str(source))
        return [], {"files": dict(old_files)}, [bad]

    for relative, path in listed:
        entry = old_files.get(relative)
        state, tail = _load_state(entry)
        try:
            rows, new_tail, tail_diags, restarted = tail_jsonl(path, tail)
        except OSError as exc:
            diagnostics.append(
                Diagnostic(f"{TOOL}_bad_file", type(exc).__name__, source=str(path))
            )
            if isinstance(entry, Mapping):
                files[relative] = dict(entry)
            continue
        for diag in tail_diags:
            if diag.code == "jsonl_missing":
                diagnostics.append(
                    Diagnostic(f"{TOOL}_bad_file", "file not found", source=diag.source)
                )
            else:
                diagnostics.append(
                    Diagnostic(
                        f"{TOOL}_bad_line", "line is not a JSON object",
                        source=diag.source, line=diag.line,
                    )
                )
        if any(d.code == "jsonl_missing" for d in tail_diags):
            if isinstance(entry, Mapping):
                files[relative] = dict(entry)
            continue
        if restarted or _valid_tail(tail) is None:
            state = _fresh_state()  # reading from byte 0: rebuild per-file state
        _process(rows, state, str(path), emit, diagnostics)
        state["tail"] = new_tail
        files[relative] = state

    return list(calls.values()), {"files": files}, diagnostics

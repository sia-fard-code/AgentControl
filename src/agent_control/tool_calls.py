"""The tool-usage call record (spec §4): validation, revisions and storage.

Validation errors and diagnostics name fields, paths and line numbers only,
never the offending value, so no free text can leak through them.
"""
from __future__ import annotations

import hashlib
import json
import re
import threading
import unicodedata
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Mapping, Sequence

from . import storage
from .models import Diagnostic

TOOLS = ("claude_code", "codex", "cline", "kilo")
READER_KEYS = ("tool", "key", "session", "root_session", "at", "model", "cwd",
               "tokens_sent", "tokens_returned", "cache_read", "cache_write",
               "cache_write_5m", "reasoning", "sent_includes_cache_read", "speed")
COUNTER_FIELDS = ("tokens_sent", "tokens_returned", "cache_read", "cache_write",
                  "cache_write_5m", "reasoning")
SCHEMA = 1

STORED_KEYS = READER_KEYS + ("schema", "revision", "project_id", "recorded_at")

_ID_RE = re.compile(r"[A-Za-z0-9_.:@+\-/]{1,300}")
_MODEL_RE = re.compile(r"[A-Za-z0-9_.:@+\-/<>]{1,200}")
_SPEED_RE = re.compile(r"[a-z_]{1,20}")
_PROJECT_RE = re.compile(r"[A-Za-z0-9-]{1,64}")
_CWD_MAX = 1024
_HEAD_WINDOW = 4096


def _invalid(field: str) -> ValueError:
    return ValueError(f"invalid {field}")


def _check_keys(value: Mapping[str, object], expected: tuple[str, ...]) -> None:
    if not isinstance(value, Mapping):
        raise ValueError("invalid record: not a mapping")
    keys = set(value.keys())
    extra = keys - set(expected)
    if extra:
        raise ValueError(f"invalid record: {len(extra)} unexpected key(s)")
    missing = [name for name in expected if name not in keys]
    if missing:
        raise ValueError(f"invalid record: missing {', '.join(missing)}")


def _pattern(value: object, field: str, pattern: re.Pattern[str]) -> str:
    if not isinstance(value, str) or pattern.fullmatch(value) is None:
        raise _invalid(field)
    return value


def _optional_pattern(value: object, field: str, pattern: re.Pattern[str]) -> str | None:
    return None if value is None else _pattern(value, field, pattern)


def _timestamp(value: object, field: str) -> str:
    if not isinstance(value, str):
        raise _invalid(field)
    try:
        return storage.normalize_utc(value)
    except (ValueError, TypeError, OverflowError):
        raise _invalid(field) from None


def _cwd(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value or len(value) > _CWD_MAX:
        raise _invalid("cwd")
    if any(unicodedata.category(ch) == "Cc" for ch in value):
        raise _invalid("cwd")
    return value


def _counter(value: object, field: str) -> int | None:
    if value is None:
        return None
    if type(value) is not int or value < 0:
        raise _invalid(field)
    return value


def _check_reader_fields(value: Mapping[str, object]) -> dict[str, object]:
    if value["tool"] not in TOOLS or not isinstance(value["tool"], str):
        raise _invalid("tool")
    result: dict[str, object] = {"tool": value["tool"]}
    for name in ("key", "session", "root_session"):
        result[name] = _pattern(value[name], name, _ID_RE)
    result["at"] = _timestamp(value["at"], "at")
    result["model"] = _optional_pattern(value["model"], "model", _MODEL_RE)
    result["cwd"] = _cwd(value["cwd"])
    for name in COUNTER_FIELDS:
        result[name] = _counter(value[name], name)
    if not isinstance(value["sent_includes_cache_read"], bool):
        raise _invalid("sent_includes_cache_read")
    result["sent_includes_cache_read"] = value["sent_includes_cache_read"]
    result["speed"] = _optional_pattern(value["speed"], "speed", _SPEED_RE)
    return result


def validate_reader_call(value: Mapping[str, object]) -> dict[str, object]:
    """Check a reader call (§4.3) and return a copy with `at` normalized."""
    _check_keys(value, READER_KEYS)
    checked = _check_reader_fields(value)
    return {name: checked[name] for name in READER_KEYS}


@dataclass(frozen=True)
class ToolCall:
    schema: int
    tool: str
    key: str
    revision: int
    session: str
    root_session: str
    at: str
    model: str | None
    cwd: str | None
    project_id: str | None
    tokens_sent: int | None
    tokens_returned: int | None
    cache_read: int | None
    cache_write: int | None
    cache_write_5m: int | None
    reasoning: int | None
    sent_includes_cache_read: bool
    speed: str | None
    recorded_at: str

    def as_json(self) -> dict[str, object]:
        return {f.name: getattr(self, f.name) for f in fields(self)}


def _project_id(value: object) -> str | None:
    return _optional_pattern(value, "project_id", _PROJECT_RE)


def validate_tool_call(value: Mapping[str, object]) -> ToolCall:
    """Check a stored record (§4.1, §4.3) and return it as a ToolCall."""
    _check_keys(value, STORED_KEYS)
    if type(value["schema"]) is not int or value["schema"] != SCHEMA:
        raise _invalid("schema")
    if type(value["revision"]) is not int or value["revision"] < 1:
        raise _invalid("revision")
    checked = _check_reader_fields(value)
    return ToolCall(
        schema=SCHEMA,
        revision=value["revision"],
        project_id=_project_id(value["project_id"]),
        recorded_at=_timestamp(value["recorded_at"], "recorded_at"),
        **checked,
    )


def merge_call(current: ToolCall | None, incoming: Mapping[str, object], *,
               project_id: str | None, recorded_at: str) -> ToolCall | None:
    """Apply §4.2: return the revision to append, or None if nothing changed."""
    call = validate_reader_call(incoming)
    project = _project_id(project_id)
    stamp = _timestamp(recorded_at, "recorded_at")
    if current is None:
        return ToolCall(schema=SCHEMA, revision=1, project_id=project,
                        recorded_at=stamp, **call)
    if (current.tool, current.key) != (call["tool"], call["key"]):
        raise ValueError("invalid merge: tool and key differ")
    merged = current.as_json()
    for name in COUNTER_FIELDS:
        known = merged[name]
        new = call[name]
        if new is not None and (known is None or new > known):
            merged[name] = new
    for name, new in (("model", call["model"]), ("cwd", call["cwd"]),
                      ("project_id", project)):
        if merged[name] is None and new is not None:
            merged[name] = new
    if merged == current.as_json():
        return None
    merged["revision"] = current.revision + 1
    merged["recorded_at"] = stamp
    return ToolCall(**merged)


def _parse_records(
    data: bytes, *, source: str, start_line: int
) -> tuple[list[ToolCall], list[Diagnostic]]:
    """Parse JSONL bytes; line numbers continue after `start_line`."""
    calls: list[ToolCall] = []
    diagnostics: list[Diagnostic] = []
    pieces = data.split(b"\n")
    if pieces and pieces[-1] == b"":
        pieces.pop()
    for index, raw in enumerate(pieces, start=start_line + 1):
        text = raw.rstrip(b"\r").decode("utf-8", errors="replace")
        if not text.strip():
            continue
        try:
            calls.append(validate_tool_call(json.loads(text)))
        except (ValueError, TypeError, RecursionError) as exc:
            diagnostics.append(Diagnostic(
                "tool_usage_bad_line",
                f"invalid tool usage line ({type(exc).__name__})",
                source=source,
                line=index,
            ))
    return calls, diagnostics


def _merge_into(target: dict[tuple[str, str], ToolCall],
                calls: Sequence[ToolCall]) -> None:
    for call in calls:
        slot = (call.tool, call.key)
        held = target.get(slot)
        if held is None or call.revision >= held.revision:
            target[slot] = call


def load_tool_calls(
    path: Path,
) -> tuple[dict[tuple[str, str], ToolCall], list[Diagnostic]]:
    try:
        data = path.read_bytes()
    except FileNotFoundError:
        return {}, []
    calls, diagnostics = _parse_records(data, source=str(path), start_line=0)
    result: dict[tuple[str, str], ToolCall] = {}
    _merge_into(result, calls)
    return result, diagnostics


def append_tool_calls(path: Path, calls: Sequence[ToolCall],
                      lock: threading.RLock) -> None:
    records = [validate_tool_call(call.as_json()).as_json() for call in calls]
    if records:
        storage.append_jsonl(path, records, lock)


class ToolCallCache:
    """Keeps the parsed store in memory and re-parses only appended bytes.

    It remembers the file's size, mtime_ns and a sha256 of its first bytes.
    An unchanged file is not read. A file that only grew (same head, larger
    size) is parsed from the previous offset. Anything else is reloaded.
    Only complete lines are consumed, so a line caught mid-write is parsed
    on a later load once its newline lands.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self._calls: dict[tuple[str, str], ToolCall] = {}
        self._diagnostics: list[Diagnostic] = []
        self._size: int | None = None
        self._mtime_ns: int | None = None
        self._offset = 0
        self._lines = 0
        self._head = ""
        self._head_len = 0

    def _reset(self) -> None:
        self._calls = {}
        self._diagnostics = []
        self._size = None
        self._mtime_ns = None
        self._offset = 0
        self._lines = 0
        self._head = ""
        self._head_len = 0

    def _result(self) -> tuple[dict[tuple[str, str], ToolCall], list[Diagnostic]]:
        return dict(self._calls), list(self._diagnostics)

    def load(self) -> tuple[dict[tuple[str, str], ToolCall], list[Diagnostic]]:
        try:
            stat = self.path.stat()
        except FileNotFoundError:
            self._reset()
            return {}, []
        if self._size == stat.st_size and self._mtime_ns == stat.st_mtime_ns:
            return self._result()
        try:
            with self.path.open("rb") as handle:
                grown = (
                    self._size is not None
                    and stat.st_size >= self._offset
                    and self._head_len <= stat.st_size
                    and hashlib.sha256(handle.read(self._head_len)).hexdigest()
                    == self._head
                )
                if not grown:
                    self._reset()
                handle.seek(self._offset)
                data = handle.read(max(0, stat.st_size - self._offset))
                end = data.rfind(b"\n")
                consumed = data[: end + 1] if end >= 0 else b""
                if consumed:
                    calls, diagnostics = _parse_records(
                        consumed, source=str(self.path), start_line=self._lines
                    )
                    _merge_into(self._calls, calls)
                    self._diagnostics.extend(diagnostics)
                    self._lines += consumed.count(b"\n")
                    self._offset += len(consumed)
                self._head_len = min(_HEAD_WINDOW, self._offset)
                handle.seek(0)
                self._head = hashlib.sha256(handle.read(self._head_len)).hexdigest()
        except FileNotFoundError:
            self._reset()
            return {}, []
        self._size = stat.st_size
        self._mtime_ns = stat.st_mtime_ns
        return self._result()

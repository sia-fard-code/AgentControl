from __future__ import annotations

import hashlib
import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from .models import (
    Diagnostic,
    FileIdentity,
    IndexCheckpoint,
    RunStatus,
    StopReason,
    safe_stop_reason,
)
from .storage import normalize_utc, strict_json_loads

_TERMINAL_STATUSES = frozenset({"complete", "failed", "interrupted"})
_PREFIX_LENGTH = 4096


@dataclass(frozen=True)
class StartEvent:
    schema: int
    run_id: str
    ts: str
    harness_version: int
    root: str
    cwd: str
    label: str
    model: str
    effort: str | None
    tool_mode: str | None
    write_mode: str | None
    engine_pid: int | None
    transcript: str
    messages: str
    usage_journal: str
    max_iters: int | None = None

    @property
    def event(self) -> str:
        return "start"


@dataclass(frozen=True)
class EndEvent:
    schema: int
    run_id: str
    ts: str
    status: RunStatus
    tool_calls: int | None
    refused: int | None
    sent: int | None
    recv: int | None
    stop_reason: StopReason | None = None

    @property
    def event(self) -> str:
        return "end"


@dataclass(frozen=True)
class IndexBatch:
    events: tuple[StartEvent | EndEvent, ...]
    checkpoint: IndexCheckpoint
    full_rescan: bool
    diagnostics: tuple[Diagnostic, ...]


def _require_str(value: object, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{field} must be a non-empty string")
    return value


def _optional_str(value: object, field: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise ValueError(f"{field} must be a non-empty string or null")
    return value


def _optional_int(value: object, field: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{field} must be an integer or null")
    return value


def _schema(value: Mapping[str, object]) -> int:
    schema = value.get("schema")
    if schema is None:
        return 1
    if isinstance(schema, bool) or not isinstance(schema, int) or schema not in (1, 2):
        raise ValueError(f"unsupported index schema {schema!r}")
    return schema


def _map_status(schema: int, raw: object) -> RunStatus:
    status = _require_str(raw, "status")
    if schema == 1:
        if status == "ok":
            return "complete"
        if status in _TERMINAL_STATUSES:
            return status  # type: ignore[return-value]
        return "interrupted"
    if status not in _TERMINAL_STATUSES:
        raise ValueError(f"invalid schema-2 status {status!r}")
    return status  # type: ignore[return-value]


def parse_index_record(
    value: Mapping[str, object], line: int
) -> StartEvent | EndEvent:
    if not isinstance(value, Mapping):
        raise ValueError("index record must be an object")
    schema = _schema(value)
    event = value.get("event")
    if event == "start":
        return StartEvent(
            schema=schema,
            run_id=_require_str(value.get("run_id"), "run_id"),
            ts=normalize_utc(_require_str(value.get("ts"), "ts")),
            harness_version=_optional_int(value.get("harness_version"), "harness_version")
            or 0,
            root=_require_str(value.get("root"), "root"),
            cwd=_require_str(value.get("cwd"), "cwd"),
            label=_require_str(value.get("label"), "label"),
            model=_require_str(value.get("model"), "model"),
            effort=_optional_str(value.get("effort"), "effort"),
            tool_mode=_optional_str(value.get("tool_mode"), "tool_mode"),
            write_mode=_optional_str(value.get("write_mode"), "write_mode"),
            engine_pid=_optional_int(value.get("engine_pid"), "engine_pid"),
            transcript=_require_str(value.get("transcript"), "transcript"),
            messages=_require_str(value.get("messages"), "messages"),
            usage_journal=_require_str(value.get("usage_journal"), "usage_journal"),
            max_iters=_optional_int(value.get("max_iters"), "max_iters"),
        )
    if event == "end":
        status = _map_status(schema, value.get("status"))
        return EndEvent(
            schema=schema,
            run_id=_require_str(value.get("run_id"), "run_id"),
            ts=normalize_utc(_require_str(value.get("ts"), "ts")),
            status=status,
            # Unknown reasons are dropped, never stored: the terminal fact still counts.
            stop_reason=safe_stop_reason(value.get("stop_reason"), status),
            tool_calls=_optional_int(value.get("tool_calls"), "tool_calls"),
            refused=_optional_int(value.get("refused"), "refused"),
            sent=_optional_int(value.get("sent"), "sent"),
            recv=_optional_int(value.get("recv"), "recv"),
        )
    raise ValueError(f"unsupported index event {event!r}")


def reduce_index_events(
    events: Sequence[StartEvent | EndEvent],
) -> tuple[dict[str, tuple[StartEvent | None, EndEvent | None]], tuple[Diagnostic, ...]]:
    pairs: dict[str, tuple[StartEvent | None, EndEvent | None]] = {}
    diagnostics: list[Diagnostic] = []
    for event in events:
        slot = 0 if isinstance(event, StartEvent) else 1
        pair = list(pairs.setdefault(event.run_id, (None, None)))
        if pair[slot] is None:
            pair[slot] = event
        elif pair[slot] != event:
            diagnostics.append(
                Diagnostic(
                    "conflicting_duplicate_event",
                    f"conflicting {event.event} ignored",
                    run_id=event.run_id,
                )
            )
        pairs[event.run_id] = (pair[0], pair[1])
    return pairs, tuple(diagnostics)


def validate_artifact_paths(
    start: StartEvent,
) -> tuple[StartEvent, tuple[Diagnostic, ...]]:
    diagnostics: list[Diagnostic] = []
    for field in ("transcript", "messages", "usage_journal"):
        raw = getattr(start, field)
        path = Path(raw)
        if not path.is_absolute():
            diagnostics.append(
                Diagnostic(
                    "invalid_artifact_path",
                    f"{field} must be an absolute path",
                    run_id=start.run_id,
                )
            )
            continue
        if not path.is_file():
            diagnostics.append(
                Diagnostic(
                    "inaccessible_artifact",
                    f"{field} is not currently accessible",
                    source=raw,
                    run_id=start.run_id,
                )
            )
    return start, tuple(diagnostics)


def _file_identity(handle, prefix: bytes) -> FileIdentity:
    stat = os.fstat(handle.fileno())
    return FileIdentity(
        device=stat.st_dev,
        inode=stat.st_ino,
        prefix_length=len(prefix),
        prefix_sha256=hashlib.sha256(prefix).hexdigest(),
    )


def _identity_matches(path: Path, checkpoint: IndexCheckpoint) -> bool:
    if checkpoint.identity is None:
        return checkpoint.offset == 0
    try:
        with path.open("rb") as handle:
            stat = os.fstat(handle.fileno())
            if stat.st_size < checkpoint.offset:
                return False
            prefix = handle.read(checkpoint.identity.prefix_length)
    except OSError:
        return False
    if stat.st_dev != checkpoint.identity.device:
        return False
    if stat.st_ino != checkpoint.identity.inode:
        return False
    if len(prefix) < checkpoint.identity.prefix_length:
        return False
    if hashlib.sha256(prefix).hexdigest() != checkpoint.identity.prefix_sha256:
        return False
    return True


def read_index_batch(path: Path, checkpoint: IndexCheckpoint) -> IndexBatch:
    if not path.exists():
        return IndexBatch((), IndexCheckpoint(0, None), False, ())

    full_rescan = not _identity_matches(path, checkpoint)
    start_offset = 0 if full_rescan else checkpoint.offset

    with path.open("rb") as handle:
        handle.seek(start_offset)
        data = handle.read()

    with path.open("rb") as handle:
        prefix = handle.read(_PREFIX_LENGTH)
        identity = _file_identity(handle, prefix)

    events: list[StartEvent | EndEvent] = []
    diagnostics: list[Diagnostic] = []
    consumed = start_offset
    line_number = 0
    for raw_line in data.splitlines(keepends=True):
        line_number += 1
        if not raw_line.endswith(b"\n"):
            break  # partial trailing line; do not consume
        consumed += len(raw_line)
        stripped = raw_line.strip()
        if not stripped:
            continue
        try:
            value = strict_json_loads(stripped)
        except ValueError:
            diagnostics.append(
                Diagnostic("malformed_json", "Skipped malformed index record", str(path), line_number)
            )
            continue
        if not isinstance(value, Mapping):
            diagnostics.append(
                Diagnostic("non_object_json", "Skipped non-object index record", str(path), line_number)
            )
            continue
        try:
            events.append(parse_index_record(value, line_number))
        except ValueError:
            diagnostics.append(
                Diagnostic("unsupported_index_record", "Skipped unsupported index record", str(path), line_number)
            )
    return IndexBatch(
        tuple(events),
        IndexCheckpoint(consumed, identity),
        full_rescan,
        tuple(diagnostics),
    )

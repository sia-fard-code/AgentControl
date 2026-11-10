from __future__ import annotations

import hashlib
import json
import os
import threading
import uuid
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping, Sequence

from .models import Diagnostic


def normalize_utc(value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("timestamp must be a non-empty ISO-8601 string")
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(f"invalid ISO-8601 timestamp: {value!r}") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"timestamp must be timezone-aware: {value!r}")
    return parsed.astimezone(timezone.utc).isoformat()


def normalize_optional_utc(value: str | None) -> str | None:
    if value is None:
        return None
    return normalize_utc(value)


class StateLocks:
    def __init__(self) -> None:
        self.write = threading.RLock()


def strict_json_loads(raw: str | bytes) -> object:
    try:
        text = raw.decode("utf-8") if isinstance(raw, bytes) else raw
        return json.loads(
            text,
            parse_constant=lambda value: (_ for _ in ()).throw(
                ValueError(f"JSON numbers must be finite: {value}")
            ),
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("invalid strict JSON") from exc


def _jsonable(value: object) -> object:
    if is_dataclass(value):
        return asdict(value)
    return value


def canonical_json(value: object) -> str:
    return json.dumps(
        _jsonable(value),
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def canonical_json_hash(value: object) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def read_jsonl(
    path: Path, *, source: str
) -> tuple[list[dict[str, object]], tuple[Diagnostic, ...]]:
    rows: list[dict[str, object]] = []
    diagnostics: list[Diagnostic] = []
    if not path.exists():
        return rows, ()
    try:
        with path.open("rb") as handle:
            for line_number, raw in enumerate(handle, 1):
                if not raw.strip():
                    continue
                try:
                    item = strict_json_loads(raw)
                except ValueError:
                    diagnostics.append(
                        Diagnostic(
                            "malformed_json",
                            "Skipped malformed JSONL record",
                            source,
                            line_number,
                        )
                    )
                    continue
                if not isinstance(item, dict):
                    diagnostics.append(
                        Diagnostic(
                            "non_object_json",
                            "Skipped non-object JSONL record",
                            source,
                            line_number,
                        )
                    )
                    continue
                rows.append(item)
    except OSError:
        diagnostics.append(
            Diagnostic("unreadable_file", "Accounting source is unreadable", source)
        )
    return rows, tuple(diagnostics)


def append_jsonl(
    path: Path,
    records: Sequence[Mapping[str, object]],
    lock: threading.RLock,
) -> None:
    if not records:
        return
    encoded = "".join(canonical_json(dict(record)) + "\n" for record in records)
    with lock:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())


def atomic_write_json(
    path: Path, value: Mapping[str, object], lock: threading.RLock
) -> None:
    encoded = json.dumps(
        _jsonable(value), indent=2, sort_keys=True, allow_nan=False
    ) + "\n"
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    with lock:
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with temporary.open("w", encoding="utf-8", newline="\n") as handle:
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
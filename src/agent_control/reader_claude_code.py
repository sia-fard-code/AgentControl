"""Reader for Claude Code transcripts (spec §5.2).

Reads every ``*.jsonl`` file under the ``projects`` directory, including
``subagents`` files, incrementally via :func:`jsonl_tail.tail_jsonl`. Only the
usage metadata of assistant API calls is kept; message content never leaves
this module. Diagnostics carry codes, paths and line numbers only.
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Mapping

from .jsonl_tail import tail_jsonl
from .models import Diagnostic
from .storage import normalize_utc

TOOL = "claude_code"

_TAIL_CODES = {
    "jsonl_bad_line": f"{TOOL}_bad_line",
    "jsonl_missing": f"{TOOL}_bad_file",
}


class _BadField(Exception):
    """A call line has a field of the wrong type. Carries only the field name."""


def _text(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _counter(container: object, name: str) -> int | None:
    if not isinstance(container, Mapping):
        return None
    value = container.get(name)
    if value is None:
        return None
    if type(value) is not int or value < 0:
        raise _BadField(name)
    return value


def _optional_str(container: Mapping[str, object], name: str) -> str | None:
    value = container.get(name)
    if value is None:
        return None
    if not isinstance(value, str):
        raise _BadField(name)
    return value


def _is_call(row: Mapping[str, object]) -> bool:
    if row.get("type") != "assistant" or row.get("isApiErrorMessage") is True:
        return False
    message = row.get("message")
    if not isinstance(message, Mapping):
        return False
    if message.get("model") == "<synthetic>":
        return False
    return isinstance(message.get("usage"), Mapping)


def _key(row: Mapping[str, object], message: Mapping[str, object]) -> str | None:
    message_id = _text(message.get("id"))
    request_id = _text(row.get("requestId"))
    if message_id is not None:
        return f"{message_id}:{request_id}" if request_id is not None else message_id
    return _text(row.get("uuid"))


def _sessions(row: Mapping[str, object], stem: str) -> tuple[str, str]:
    session_id = _text(row.get("sessionId"))
    if session_id is None:
        return stem, stem
    agent_id = _text(row.get("agentId"))
    session = f"{session_id}:{agent_id}" if agent_id is not None else session_id
    return session, session_id


def _call(
    row: Mapping[str, object], message: Mapping[str, object], key: str, stem: str
) -> dict[str, object]:
    usage = message["usage"]
    assert isinstance(usage, Mapping)
    timestamp = row.get("timestamp")
    if not isinstance(timestamp, str):
        raise _BadField("timestamp")
    try:
        at = normalize_utc(timestamp)
    except ValueError:
        raise _BadField("timestamp") from None
    session, root = _sessions(row, stem)
    speed = usage.get("speed")
    if speed is not None and not isinstance(speed, str):
        raise _BadField("speed")
    return {
        "tool": TOOL,
        "key": key,
        "session": session,
        "root_session": root,
        "at": at,
        "model": _optional_str(message, "model"),
        "cwd": _optional_str(row, "cwd"),
        "tokens_sent": _counter(usage, "input_tokens"),
        "tokens_returned": _counter(usage, "output_tokens"),
        "cache_read": _counter(usage, "cache_read_input_tokens"),
        "cache_write": _counter(usage, "cache_creation_input_tokens"),
        "cache_write_5m": _counter(usage.get("cache_creation"), "ephemeral_5m_input_tokens"),
        "reasoning": _counter(usage.get("output_tokens_details"), "thinking_tokens"),
        "sent_includes_cache_read": False,
        "speed": speed,
    }


def _file_states(checkpoint: Mapping[str, object] | None) -> dict[str, object]:
    if not isinstance(checkpoint, Mapping):
        return {}
    files = checkpoint.get("files")
    if not isinstance(files, Mapping):
        return {}
    return {str(name): state for name, state in files.items()}


def read(
    source: Path, checkpoint: Mapping[str, object] | None, *, now: datetime
) -> tuple[list[dict[str, object]], dict[str, object], list[Diagnostic]]:
    source = Path(source)
    if not source.is_dir():
        missing = Diagnostic(
            f"{TOOL}_source_missing", "source directory not found", source=str(source)
        )
        unchanged = dict(checkpoint) if isinstance(checkpoint, Mapping) else {"files": {}}
        return [], unchanged, [missing]

    previous = _file_states(checkpoint)
    states: dict[str, object] = {}
    calls: dict[str, dict[str, object]] = {}
    diagnostics: list[Diagnostic] = []

    for path in sorted(p for p in source.rglob("*.jsonl") if p.is_file()):
        relative = path.relative_to(source).as_posix()
        prior = previous.get(relative)
        prior_state = prior if isinstance(prior, Mapping) else None
        try:
            rows, state, tail_diags, _restarted = tail_jsonl(path, prior_state)
        except OSError as exc:
            diagnostics.append(
                Diagnostic(
                    f"{TOOL}_bad_file",
                    f"cannot read file ({type(exc).__name__})",
                    source=str(path),
                )
            )
            if prior_state is not None:
                states[relative] = dict(prior_state)
            continue
        for diag in tail_diags:
            code = _TAIL_CODES.get(diag.code, f"{TOOL}_bad_line")
            diagnostics.append(
                Diagnostic(code, diag.message, source=diag.source, line=diag.line)
            )
        if state:
            states[relative] = state
        for line_no, row in rows:
            if not _is_call(row):
                continue
            message = row["message"]
            assert isinstance(message, Mapping)
            key = _key(row, message)
            if key is None:
                diagnostics.append(
                    Diagnostic(
                        f"{TOOL}_skipped_line",
                        "assistant line has no requestId, message.id or uuid",
                        source=str(path),
                        line=line_no,
                    )
                )
                continue
            try:
                calls[key] = _call(row, message, key, path.stem)
            except _BadField as exc:
                diagnostics.append(
                    Diagnostic(
                        f"{TOOL}_bad_line",
                        f"invalid field: {exc.args[0]}",
                        source=str(path),
                        line=line_no,
                    )
                )

    return list(calls.values()), {"files": states}, diagnostics

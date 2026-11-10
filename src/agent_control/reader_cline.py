"""Reader for Cline task usage (spec §5.4).

Reads ``tasks/<task>/ui_messages.json`` and ``state/taskHistory.json`` under the
extension's globalStorage directory. Prompt text (``request`` and ``task``) is
parsed in memory and dropped; only the §4.1 fields leave this module.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping

from .models import Diagnostic

TOOL = "cline"
REREAD_AFTER_SECONDS = 30
_ANTHROPIC_SHAPED = frozenset({"anthropic", "bedrock", "vertex", "claude-code"})
# checkpoint: {"files": {"<task dir name>": {"size": int, "mtime_ns": int, "read_at": iso}}}

_UI_FILE = "ui_messages.json"


def _counter(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _parse_iso(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _prior_files(checkpoint: Mapping[str, object] | None) -> dict[str, dict[str, Any]]:
    if not checkpoint:
        return {}
    files = checkpoint.get("files")
    if not isinstance(files, Mapping):
        return {}
    return {str(k): dict(v) for k, v in files.items() if isinstance(v, Mapping)}


def _load_cwds(source: Path, diags: list[Diagnostic]) -> dict[str, str]:
    path = source / "state" / "taskHistory.json"
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        diags.append(Diagnostic("cline_bad_file",
                                f"Could not parse task history ({type(exc).__name__}).",
                                source=str(path)))
        return {}
    if not isinstance(data, list):
        diags.append(Diagnostic("cline_bad_file", "Task history is not a JSON array.",
                                source=str(path)))
        return {}
    cwds: dict[str, str] = {}
    for item in data:
        if not isinstance(item, Mapping):
            continue
        task_id = item.get("id")
        cwd = item.get("cwdOnTaskInitialization")
        if isinstance(task_id, str) and isinstance(cwd, str) and cwd:
            cwds[task_id] = cwd
    return cwds
def _call_from_entry(task: str, entry: object, cwd: str | None) -> dict[str, object] | None:
    if not isinstance(entry, Mapping) or entry.get("say") != "api_req_started":
        return None
    ts = entry.get("ts")
    text = entry.get("text")
    if isinstance(ts, bool) or not isinstance(ts, (int, float)) or not isinstance(text, str):
        return None
    try:
        payload = json.loads(text)
    except ValueError:
        return None
    if not isinstance(payload, Mapping) or ("tokensIn" not in payload
                                            and "tokensOut" not in payload):
        return None
    try:
        at = datetime.fromtimestamp(ts / 1000, tz=timezone.utc).isoformat()
    except (OverflowError, OSError, ValueError):
        return None
    info = entry.get("modelInfo")
    model: str | None = None
    provider: object = None
    if isinstance(info, Mapping):
        model_id = info.get("modelId")
        model = model_id if isinstance(model_id, str) and model_id else None
        provider = info.get("providerId")
    ts_text = str(int(ts)) if isinstance(ts, float) and ts.is_integer() else str(ts)
    return {
        "tool": TOOL,
        "key": f"{task}:{ts_text}",
        "session": task,
        "root_session": task,
        "at": at,
        "model": model,
        "cwd": cwd,
        "tokens_sent": _counter(payload.get("tokensIn")),
        "tokens_returned": _counter(payload.get("tokensOut")),
        "cache_read": _counter(payload.get("cacheReads")),
        "cache_write": _counter(payload.get("cacheWrites")),
        "cache_write_5m": None,
        "reasoning": None,
        "sent_includes_cache_read": provider not in _ANTHROPIC_SHAPED,
        "speed": None,
    }


def read(source: Path, checkpoint: Mapping[str, object] | None, *, now: datetime
         ) -> tuple[list[dict[str, object]], dict[str, object], list[Diagnostic]]:
    source = Path(source)
    if not source.is_dir():
        unchanged = dict(checkpoint) if checkpoint else {}
        return [], unchanged, [Diagnostic("cline_source_missing",
                                          "Cline storage directory not found.",
                                          source=str(source))]
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    diags: list[Diagnostic] = []
    prior = _prior_files(checkpoint)
    files: dict[str, dict[str, Any]] = dict(prior)
    calls: dict[str, dict[str, object]] = {}
    cwds = _load_cwds(source, diags)
    tasks_dir = source / "tasks"
    task_dirs = sorted(p for p in tasks_dir.iterdir() if p.is_dir()) if tasks_dir.is_dir() else []
    for tdir in task_dirs:
        task = tdir.name
        ui = tdir / _UI_FILE
        try:
            stat = ui.stat()
        except OSError:
            continue
        old = prior.get(task)
        if old is not None:
            if old.get("size") == stat.st_size and old.get("mtime_ns") == stat.st_mtime_ns:
                continue
            read_at = _parse_iso(old.get("read_at"))
            if read_at is not None and now - read_at < timedelta(seconds=REREAD_AFTER_SECONDS):
                continue
        try:
            data = json.loads(ui.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            diags.append(Diagnostic("cline_bad_file",
                                    f"Could not parse ui_messages.json ({type(exc).__name__}).",
                                    source=str(ui)))
            continue
        if not isinstance(data, list):
            diags.append(Diagnostic("cline_bad_file", "ui_messages.json is not a JSON array.",
                                    source=str(ui)))
            continue
        cwd = cwds.get(task)
        for entry in data:
            call = _call_from_entry(task, entry, cwd)
            if call is not None:
                calls[str(call["key"])] = call
        files[task] = {"size": stat.st_size, "mtime_ns": stat.st_mtime_ns,
                       "read_at": now.astimezone(timezone.utc).isoformat()}
    return list(calls.values()), {"files": files}, diags

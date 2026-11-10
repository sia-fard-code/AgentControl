"""Sanitized activity timeline extracted from a delegated-run transcript.

Security invariant (AGENTS.md + approved decision 14): this module must never
surface prompts, assistant reasoning ("thinking" text), tool result bodies, file
contents, command strings, or credentials. It emits only structural metadata:
the ordered sequence of steps, each step's phase, the tool name, and - when the
tool is a file operation - the workspace-relative path it touched. Everything
else is discarded.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Literal

# ``### [12] read_file({...})`` or ``### [12] thinking``
_STEP = re.compile(r"^###\s+\[(\d+)\]\s+([A-Za-z_][\w-]*)\s*(?:\((.*)\))?\s*$")

# Control markers that are not real tool calls.
_CONTROL = frozenset({"thinking", "finish", "NO_TOOL_ACTION", "PARSE_ERROR", "NO_TOOL"})

# Tools whose single safe, non-sensitive argument is a workspace path. Only the
# path is surfaced; every other argument (content, command, query) is dropped.
_PATH_TOOLS = frozenset(
    {
        "read_file",
        "write_file",
        "replace_in_file",
        "apply_diff",
        "list_dir",
        "list_files",
        "insert_content",
        "search_files",
    }
)

Phase = Literal["thinking", "tool"]


@dataclass(frozen=True)
class ActivityStep:
    iteration: int
    phase: Phase
    tool: str | None
    path: str | None

    def as_dict(self) -> dict[str, object]:
        return {
            "iteration": self.iteration,
            "phase": self.phase,
            "tool": self.tool,
            "path": self.path,
        }


def _safe_path(raw_args: str) -> str | None:
    """Extract only a ``path`` string from the raw argument blob, if present.

    The transcript stores tool arguments as a JSON object. We parse it solely to
    read the ``path`` key and discard everything else. If parsing fails or there
    is no plain string path, we return None rather than risk leaking content.
    """
    raw_args = raw_args.strip()
    if not raw_args:
        return None
    try:
        parsed = json.loads(raw_args)
    except (ValueError, TypeError):
        return None
    if not isinstance(parsed, dict):
        return None
    path = parsed.get("path")
    if isinstance(path, str) and path and "\n" not in path and len(path) <= 260:
        return path
    return None


def parse_activity(transcript_text: str) -> list[ActivityStep]:
    """Return the sanitized, ordered activity timeline for a transcript string."""
    steps: list[ActivityStep] = []
    for line in transcript_text.splitlines():
        match = _STEP.match(line.rstrip())
        if not match:
            continue
        iteration = int(match.group(1))
        name = match.group(2)
        raw_args = match.group(3) or ""
        if name == "thinking":
            steps.append(ActivityStep(iteration, "thinking", None, None))
            continue
        if name in _CONTROL:
            continue
        path = _safe_path(raw_args) if name in _PATH_TOOLS else None
        steps.append(ActivityStep(iteration, "tool", name, path))
    return steps


def _parse_ts(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def _int_or_none(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def iteration_metrics(journal_path) -> dict[int, dict[str, object]]:
    """Return per-iteration timing and token metrics from a usage journal.

    Each entry has tokens_sent/returned/cache_read/cache_write and the
    duration_seconds spent on that iteration (time until the next iteration
    began; the final iteration's duration is None). Never raises: an unreadable
    or missing journal yields an empty mapping, and malformed lines are skipped.
    Only numeric counters and timestamps are read - no prompts or content.
    """
    path = Path(journal_path)
    if not path.is_file():
        return {}
    entries: list[tuple[int, datetime | None, dict[str, object]]] = []
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(row, dict):
                    continue
                iteration = _int_or_none(row.get("iter"))
                if iteration is None:
                    continue
                entries.append(
                    (
                        iteration,
                        _parse_ts(row.get("ts")),
                        _parse_ts(row.get("started")),
                        {
                            "tokens_sent": _int_or_none(row.get("sent")),
                            "tokens_returned": _int_or_none(row.get("recv")),
                            "cache_read": _int_or_none(row.get("cache_read")),
                            "cache_write": _int_or_none(row.get("cache_write")),
                        },
                    )
                )
    except OSError:
        return {}

    metrics: dict[int, dict[str, object]] = {}
    for index, (iteration, ts, started, tokens) in enumerate(entries):
        duration: float | None = None
        tool_seconds: float | None = None
        if ts is not None and index + 1 < len(entries):
            next_ts = entries[index + 1][1]
            if next_ts is not None:
                delta = (next_ts - ts).total_seconds()
                duration = delta if delta >= 0 else None
            next_started = entries[index + 1][2]
            if next_started is not None:
                delta_tool = (next_started - ts).total_seconds()
                tool_seconds = delta_tool if delta_tool >= 0 else None
        model_seconds: float | None = None
        if ts is not None and started is not None:
            delta_model = (ts - started).total_seconds()
            model_seconds = delta_model if delta_model >= 0 else None
        record = {
            "duration_seconds": duration,
            "ts": ts.isoformat() if ts else None,
            "model_started_at": started.isoformat() if started else None,
            "replied_at": ts.isoformat() if ts else None,
            "model_seconds": model_seconds,
            "tool_seconds": tool_seconds,
        }
        record.update(tokens)
        metrics[iteration] = record
    return metrics


def parse_activity_file(path, *, max_bytes: int = 4_000_000) -> list[ActivityStep]:
    """Read a transcript file (bounded) and return its sanitized timeline.

    Reads at most ``max_bytes`` so an enormous transcript cannot exhaust memory.
    Any read error yields an empty timeline rather than raising.
    """
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            text = handle.read(max_bytes)
    except OSError:
        return []
    return parse_activity(text)


_ARCHIVE_SCHEMA = 1


def build_activity_snapshot(transcript_path) -> dict[str, object]:
    """Build a sanitized, pricing-agnostic activity snapshot for archiving.

    Merges the transcript's sanitized step timeline with per-iteration timing
    and token metrics from the sibling usage journal. Contains only structural
    metadata and integer counters - never prompts, reasoning, tool output,
    command strings, or file contents. Cost is intentionally NOT stored so the
    archive stays valid across pricing changes; it is computed at read time.
    """
    transcript = Path(transcript_path)
    steps = [s.as_dict() for s in parse_activity_file(transcript)]
    journal = transcript.with_suffix(".usage.jsonl")
    metrics = iteration_metrics(journal)

    seen: set[int] = set()
    total_seconds = 0.0
    latest_started_at: str | None = None
    model_seconds_total: float | None = None
    tool_seconds_total: float | None = None
    for step in steps:
        it = step["iteration"]
        m = metrics.get(it)
        if m is not None and it not in seen:
            seen.add(it)
            step["started_at"] = m.get("ts")
            step["duration_seconds"] = m.get("duration_seconds")
            step["tokens_sent"] = m.get("tokens_sent")
            step["tokens_returned"] = m.get("tokens_returned")
            step["cache_read"] = m.get("cache_read")
            step["cache_write"] = m.get("cache_write")
            step["model_seconds"] = m.get("model_seconds")
            step["tool_seconds"] = m.get("tool_seconds")
            if isinstance(m.get("ts"), str):
                latest_started_at = m["ts"]
            if isinstance(m.get("duration_seconds"), (int, float)):
                total_seconds += float(m["duration_seconds"])
            if isinstance(m.get("model_seconds"), (int, float)):
                model_seconds_total = (model_seconds_total or 0.0) + float(m["model_seconds"])
            if isinstance(m.get("tool_seconds"), (int, float)):
                tool_seconds_total = (tool_seconds_total or 0.0) + float(m["tool_seconds"])
        else:
            step["started_at"] = None
            step["duration_seconds"] = None
            step["model_seconds"] = None
            step["tool_seconds"] = None

    tool_count = sum(1 for s in steps if s["phase"] == "tool")
    return {
        "schema": _ARCHIVE_SCHEMA,
        "steps": steps,
        "step_count": len(steps),
        "tool_count": tool_count,
        "total_seconds": total_seconds,
        "latest_started_at": latest_started_at,
        "model_seconds_total": model_seconds_total,
        "tool_seconds_total": tool_seconds_total,
    }


def write_activity_archive(path, snapshot: dict[str, object]) -> None:
    """Persist a sanitized activity snapshot atomically. Never raises on OSError."""
    target = Path(path)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(f".{target.name}.tmp")
        tmp.write_text(
            json.dumps(snapshot, separators=(",", ":"), allow_nan=False),
            encoding="utf-8",
        )
        import os

        os.replace(tmp, target)
    except OSError:
        return


def load_activity_archive(path) -> dict[str, object] | None:
    """Load a previously archived activity snapshot, or None if absent/unreadable."""
    target = Path(path)
    if not target.is_file():
        return None
    try:
        value = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(value, dict):
        return None
    return value

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

REPO_INDEX_NAME = "agent-runs-index.jsonl"

_FOOTER_RE = re.compile(
    r"^\[[^\]]*: ([\d,]+) tool calls, ([\d,]+) tok sent / ([\d,]+) tok returned\]$"
)
_MODEL_RE = re.compile(
    r"^- model: `([^`]*)`\s+effort: `([^`]*)`\s+tools: `([^`]*)`"
)
_ROOT_RE = re.compile(r"^- writes: `([^`]*)`\s+root: `([^`]*)`")


@dataclass(frozen=True)
class BackfillPlan:
    records: tuple[dict, ...]
    skipped: tuple[tuple[str, str], ...]


def _indexed_run_ids(path: Path) -> set[str]:
    ids: set[str] = set()
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return ids
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if isinstance(obj, dict):
            run_id = obj.get("run_id")
            if isinstance(run_id, str):
                ids.add(run_id)
    return ids


@dataclass(frozen=True)
class _Parsed:
    model: str
    effort: str | None
    tool_mode: str | None
    write_mode: str
    root_value: str
    final_report: bool
    footer: tuple[int, int, int] | None


def _parse_transcript(path: Path) -> _Parsed | None:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    lines = text.splitlines()
    if not lines or not lines[0].startswith("# "):
        return None

    model: str | None = None
    effort: str | None = None
    tool_mode: str | None = None
    write_mode = "staged"
    root_value = ""
    seen_model_before_task = False
    saw_task = False

    for line in lines:
        if line == "## Task":
            saw_task = True
            break
        m = _MODEL_RE.match(line)
        if m is not None:
            model = m.group(1)
            effort = m.group(2)
            tool_mode = m.group(3)
            seen_model_before_task = True
        r = _ROOT_RE.match(line)
        if r is not None:
            writes = r.group(1)
            write_mode = "disk" if writes.startswith("disk") else "staged"
            root_value = r.group(2)

    if not (saw_task and seen_model_before_task and model is not None):
        return None

    final_report = _has_final_report(lines)
    footer = _find_footer(lines)
    return _Parsed(
        model=model,
        effort=effort or None,
        tool_mode=tool_mode or None,
        write_mode=write_mode,
        root_value=root_value,
        final_report=final_report,
        footer=footer,
    )


def _has_final_report(lines: list[str]) -> bool:
    for i, line in enumerate(lines):
        if line == "---":
            j = i + 1
            while j < len(lines) and lines[j].strip() == "":
                j += 1
            if j < len(lines) and lines[j] == "## Final report":
                return True
    return False


def _find_footer(lines: list[str]) -> tuple[int, int, int] | None:
    for line in reversed(lines):
        m = _FOOTER_RE.match(line)
        if m is not None:
            return (
                int(m.group(1).replace(",", "")),
                int(m.group(2).replace(",", "")),
                int(m.group(3).replace(",", "")),
            )
    return None


def _start_ts(run_id: str) -> str:
    stamp = run_id.split("-", 2)
    local = datetime.strptime("-".join(stamp[:2]), "%Y%m%d-%H%M%S")
    return local.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def plan_backfill(
    repo: Path,
    *,
    machine_index: Path,
    now: float,
    min_age_seconds: int = 900,
) -> BackfillPlan:
    repo = Path(repo)
    runs_dir = repo / ".claude" / "agent-runs"
    repo_index = runs_dir / REPO_INDEX_NAME
    real_repo = os.path.realpath(repo)

    known = _indexed_run_ids(Path(machine_index)) | _indexed_run_ids(repo_index)

    records: list[dict] = []
    skipped: list[tuple[str, str]] = []

    if not runs_dir.is_dir():
        return BackfillPlan(records=(), skipped=())

    for transcript in sorted(runs_dir.glob("*.md")):
        run_id = transcript.stem
        parsed = _parse_transcript(transcript)
        if parsed is None:
            skipped.append((run_id, "not an engine transcript"))
            continue
        if run_id in known:
            skipped.append((run_id, "already indexed"))
            continue
        mtime = os.path.getmtime(transcript)
        if now - mtime < min_age_seconds:
            skipped.append((run_id, "recently modified"))
            continue

        label = run_id.split("-", 2)[2] if len(run_id.split("-", 2)) == 3 else ""
        stem = transcript.with_suffix("")
        messages = str(stem) + ".messages.jsonl"
        usage = str(stem) + ".usage.jsonl"
        start = {
            "schema": 2,
            "event": "start",
            "run_id": run_id,
            "ts": _start_ts(run_id),
            "harness_version": None,
            "label": label,
            "model": parsed.model,
            "effort": parsed.effort,
            "tool_mode": parsed.tool_mode,
            "root": real_repo,
            "cwd": real_repo,
            "engine_pid": None,
            "transcript": str(transcript.resolve()),
            "messages": str(Path(messages).resolve()),
            "usage_journal": str(Path(usage).resolve()),
            "write_mode": parsed.write_mode,
            "max_iters": None,
            "backfilled": True,
        }

        if parsed.final_report:
            status = "complete"
        elif parsed.footer is not None:
            status = "failed"
        else:
            status = "interrupted"

        if parsed.footer is not None:
            tool_calls, sent, recv = parsed.footer
        else:
            tool_calls = sent = recv = None

        end_ts = datetime.fromtimestamp(mtime, timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        end = {
            "schema": 2,
            "event": "end",
            "run_id": run_id,
            "ts": end_ts,
            "status": status,
            "tool_calls": tool_calls,
            "refused": None,
            "sent": sent,
            "recv": recv,
            "backfilled": True,
        }
        records.append(start)
        records.append(end)

    return BackfillPlan(records=tuple(records), skipped=tuple(skipped))


def apply_backfill(repo: Path, plan: BackfillPlan) -> int:
    repo = Path(repo)
    runs_dir = repo / ".claude" / "agent-runs"
    runs_dir.mkdir(parents=True, exist_ok=True)
    index_path = runs_dir / REPO_INDEX_NAME
    written = 0
    with index_path.open("a", encoding="utf-8", newline="\n") as handle:
        for record in plan.records:
            handle.write(
                json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n"
            )
            written += 1
        handle.flush()
        os.fsync(handle.fileno())
    return written

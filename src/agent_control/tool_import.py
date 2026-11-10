"""Import tool usage from the readers (spec §6.1 to §6.4).

Diagnostics name tools, fields, paths and exception type names only, never
reader content, so no free text can reach state through them.
"""
from __future__ import annotations

import importlib
import json
import os
import threading
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Mapping, Sequence

from . import projects, storage, tool_calls
from .models import Diagnostic
from .paths import AppPaths
from .storage import StateLocks

READER_MODULES = {"claude_code": "agent_control.reader_claude_code",
                  "codex": "agent_control.reader_codex",
                  "cline": "agent_control.reader_cline",
                  "kilo": "agent_control.reader_kilo"}

SOURCES_SCHEMA = 1
STATE_SCHEMA = 1

Reader = Callable[..., "tuple[list[dict], dict, list[Diagnostic]]"]


def _load_reader(tool: str) -> Reader:
    """Import the reader module lazily, so a missing reader fails only its tool."""
    return importlib.import_module(READER_MODULES[tool]).read


@dataclass(frozen=True)
class SourceSetting:
    enabled: bool
    path: str | None


def _sources_invalid(path: Path, reason: str) -> list[Diagnostic]:
    return [Diagnostic("usage_sources_invalid",
                       f"usage sources file is invalid ({reason})", source=str(path))]


def load_usage_sources(path: Path) -> tuple[dict[str, SourceSetting], list[Diagnostic]]:
    """Read `usage_sources.json`; a tool absent from the result is disabled."""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}, []
    except (OSError, UnicodeDecodeError) as exc:
        return {}, _sources_invalid(path, type(exc).__name__)
    try:
        raw = json.loads(text)
    except (ValueError, RecursionError) as exc:
        return {}, _sources_invalid(path, type(exc).__name__)
    if not isinstance(raw, dict) or set(raw) != {"schema", "tools"}:
        return {}, _sources_invalid(path, "shape")
    if type(raw["schema"]) is not int or raw["schema"] != SOURCES_SCHEMA:
        return {}, _sources_invalid(path, "schema")
    tools = raw["tools"]
    if not isinstance(tools, dict):
        return {}, _sources_invalid(path, "tools")
    settings: dict[str, SourceSetting] = {}
    for tool, entry in tools.items():
        if tool not in tool_calls.TOOLS:
            return {}, _sources_invalid(path, "unknown tool")
        if not isinstance(entry, dict) or set(entry) != {"enabled", "path"}:
            return {}, _sources_invalid(path, f"{tool} entry")
        enabled, source = entry["enabled"], entry["path"]
        if not isinstance(enabled, bool):
            return {}, _sources_invalid(path, f"{tool} enabled")
        if source is not None and (not isinstance(source, str) or not source):
            return {}, _sources_invalid(path, f"{tool} path")
        settings[tool] = SourceSetting(enabled=enabled, path=source)
    return settings, []


def write_usage_sources(path: Path, settings: Mapping[str, SourceSetting],
                        lock: threading.RLock) -> None:
    tools: dict[str, object] = {}
    for tool, setting in settings.items():
        if tool not in tool_calls.TOOLS:
            raise ValueError("invalid tool")
        tools[tool] = {"enabled": bool(setting.enabled), "path": setting.path}
    storage.atomic_write_json(path, {"schema": SOURCES_SCHEMA, "tools": tools}, lock)


def default_source(tool: str, *, home: Path, env: Mapping[str, str]) -> Path | None:
    """The §6.1 default source for `tool`, or None when it cannot be located."""
    if tool == "claude_code":
        return home / ".claude" / "projects"
    if tool == "codex":
        codex_home = env.get("CODEX_HOME")
        return Path(codex_home) if codex_home else home / ".codex"
    if tool == "cline":
        appdata = env.get("APPDATA")
        if not appdata:
            return None
        return (Path(appdata) / "Code" / "User" / "globalStorage"
                / "saoudrizwan.claude-dev")
    if tool == "kilo":
        return home / ".local" / "share" / "kilo" / "kilo.db"
    return None


def resolve_source(tool: str, setting: SourceSetting | None, *, home: Path,
                   env: Mapping[str, str]) -> Path | None:
    if setting is not None and setting.path:
        return Path(setting.path)
    return default_source(tool, home=home, env=env)


def _exists(path: Path) -> bool:
    try:
        return path.exists()
    except OSError:
        return False


def resolve_project_root(cwd: str, *, home: Path) -> str:
    """Walk up to the nearest `.git` (§6.4), never choosing home or a drive root."""
    start = Path(cwd)
    if not start.is_absolute():
        return cwd
    home_key = os.path.normcase(os.path.abspath(str(home)))
    current = start
    while True:
        if os.path.normcase(os.path.abspath(str(current))) == home_key:
            return cwd
        if current.parent == current:
            return cwd
        if _exists(current / ".git"):
            return str(current)
        current = current.parent


@dataclass(frozen=True)
class ToolImportSummary:
    tool: str
    calls_seen: int
    new_calls: int
    revised_calls: int
    sessions: int
    first_at: str | None
    last_at: str | None
    tokens_sent: int
    tokens_returned: int
    cache_read: int
    cache_write: int
    diagnostics: int


@dataclass(frozen=True)
class ImportReport:
    applied: bool
    tools: tuple[ToolImportSummary, ...]
    new_project_roots: tuple[str, ...]
    diagnostics: tuple[Diagnostic, ...]


def _load_state(path: Path) -> tuple[dict[str, object], list[Diagnostic]]:
    """Read the checkpoints; anything unreadable starts every tool afresh."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}, []
    except (OSError, UnicodeDecodeError, ValueError, RecursionError) as exc:
        return {}, [Diagnostic("tool_usage_state_invalid",
                               f"tool usage state is invalid ({type(exc).__name__})",
                               source=str(path))]
    if (not isinstance(raw, dict) or raw.get("schema") != STATE_SCHEMA
            or not isinstance(raw.get("tools"), dict)):
        return {}, [Diagnostic("tool_usage_state_invalid",
                               "tool usage state is invalid (shape)", source=str(path))]
    return dict(raw["tools"]), []


class _Attribution:
    """Maps a call's cwd to a project id, cached per cwd for one import."""

    def __init__(self, paths: AppPaths, locks: StateLocks, *, apply: bool,
                 now: datetime, home: Path) -> None:
        self.apply = apply
        self.home = home
        self.registry = projects.ProjectRegistry(paths.projects, locks, clock=lambda: now)
        self.known: dict[str, str] = {}
        if not apply:
            loaded, _ = projects.load_projects(paths.projects)
            self.known = {p.root_key: p.project_id for p in loaded.values()}
        self.cache: dict[str, str | None] = {}
        self.new_roots: dict[str, str] = {}

    def project_id(self, cwd: str | None) -> str | None:
        if cwd is None:
            return None
        if cwd not in self.cache:
            self.cache[cwd] = self._resolve(cwd)
        return self.cache[cwd]

    def _resolve(self, cwd: str) -> str | None:
        root = resolve_project_root(cwd, home=self.home)
        if not Path(root).is_absolute():
            # A relative cwd cannot be placed; resolving it would pick up the
            # process's own working directory.
            return None
        if self.apply:
            project, created = self.registry.discover(root)
            if created:
                self.new_roots.setdefault(project.root_key, project.root)
            return project.project_id
        normalized, root_key = projects.canonical_root(root)
        if root_key in self.known:
            return self.known[root_key]
        self.new_roots.setdefault(root_key, normalized)
        return None


class _Tally:
    def __init__(self, tool: str) -> None:
        self.tool = tool
        self.seen = 0
        self.new = 0
        self.revised = 0
        self.sessions: set[str] = set()
        self.ats: list[str] = []
        self.sums = {"tokens_sent": 0, "tokens_returned": 0,
                     "cache_read": 0, "cache_write": 0}
        self.diagnostics = 0

    def add(self, call: Mapping[str, object]) -> None:
        self.sessions.add(str(call["root_session"]))
        self.ats.append(str(call["at"]))
        for name in self.sums:
            value = call[name]
            if isinstance(value, int):
                self.sums[name] += value

    def summary(self) -> ToolImportSummary:
        def key(at: str) -> datetime:
            return datetime.fromisoformat(at)

        return ToolImportSummary(
            tool=self.tool, calls_seen=self.seen, new_calls=self.new,
            revised_calls=self.revised, sessions=len(self.sessions),
            first_at=min(self.ats, key=key) if self.ats else None,
            last_at=max(self.ats, key=key) if self.ats else None,
            diagnostics=self.diagnostics, **self.sums)


def _invalid_field(exc: ValueError) -> str:
    text = str(exc)
    return text[len("invalid "):] if text.startswith("invalid ") else "record"


def _run_reader(read: Reader, source: Path, checkpoint: object, now: datetime
                ) -> tuple[list[object], dict[str, object], list[Diagnostic]]:
    calls, new_checkpoint, diagnostics = read(source, checkpoint, now=now)
    if (not isinstance(calls, list) or not isinstance(new_checkpoint, dict)
            or not isinstance(diagnostics, list)):
        raise TypeError("reader returned an invalid result")
    return calls, new_checkpoint, diagnostics


def import_tool_usage(paths: AppPaths, locks: StateLocks, *, tools: Sequence[str],
                      sources: Mapping[str, Path], apply: bool, now: datetime, home: Path,
                      readers: Mapping[str, Callable] | None = None,
                      store: tool_calls.ToolCallCache | None = None) -> ImportReport:
    recorded_at = storage.normalize_utc(now.isoformat())
    checkpoints, diagnostics = _load_state(paths.tool_usage_state)
    # The existing calls are loaded only when the first valid reader call needs
    # merging, so a sync with nothing new never reads tool_usage.jsonl. With a
    # store, its map is copied: the import adds revisions to `current`, and the
    # cache's own map must never be mutated.
    current: dict[tuple[str, str], tool_calls.ToolCall] | None = None

    def existing() -> dict[tuple[str, str], tool_calls.ToolCall]:
        nonlocal current
        if current is None:
            if store is not None:
                current = dict(store.load()[0])
            else:
                current = tool_calls.load_tool_calls(paths.tool_usage)[0]
        return current

    attribution = _Attribution(paths, locks, apply=apply, now=now, home=home)
    pending: list[tool_calls.ToolCall] = []
    summaries: list[ToolImportSummary] = []
    for tool in tools:
        tally = _Tally(tool)
        try:
            read = readers[tool] if readers is not None else _load_reader(tool)
            calls, checkpoint, reader_diags = _run_reader(
                read, sources[tool], checkpoints.get(tool), now)
        except Exception as exc:  # one failing reader must not stop the others
            diagnostics.append(Diagnostic("tool_reader_failed",
                                          f"{tool}: {type(exc).__name__}"))
            tally.diagnostics += 1
            summaries.append(tally.summary())
            continue
        diagnostics.extend(reader_diags)
        tally.diagnostics += len(reader_diags)
        tally.seen = len(calls)
        for index, raw in enumerate(calls):
            try:
                call = tool_calls.validate_reader_call(raw)
                if call["tool"] != tool:
                    raise ValueError("invalid tool")
            except (ValueError, TypeError, AttributeError) as exc:
                field = _invalid_field(exc) if isinstance(exc, ValueError) else "record"
                diagnostics.append(Diagnostic("tool_call_invalid",
                                              f"{tool}: {field}", line=index + 1))
                tally.diagnostics += 1
                continue
            tally.add(call)
            project_id = attribution.project_id(call["cwd"])
            slot = (tool, str(call["key"]))
            known = existing()
            revision = tool_calls.merge_call(known.get(slot), call,
                                             project_id=project_id,
                                             recorded_at=recorded_at)
            if revision is None:
                continue
            known[slot] = revision
            pending.append(revision)
            if revision.revision == 1:
                tally.new += 1
            else:
                tally.revised += 1
        checkpoints[tool] = checkpoint
        summaries.append(tally.summary())
    if apply:
        with locks.write:
            tool_calls.append_tool_calls(paths.tool_usage, pending, locks.write)
            storage.atomic_write_json(paths.tool_usage_state,
                                      {"schema": STATE_SCHEMA, "tools": checkpoints},
                                      locks.write)
    return ImportReport(applied=apply, tools=tuple(summaries),
                        new_project_roots=tuple(attribution.new_roots.values()),
                        diagnostics=tuple(diagnostics))

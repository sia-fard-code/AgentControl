from __future__ import annotations

import os
import uuid
from collections.abc import Callable, Mapping
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Literal

from .models import Diagnostic, Project, ProjectEvent
from .storage import StateLocks, append_jsonl, normalize_utc, read_jsonl

_EVENTS = frozenset({"discovered", "approved", "rejected", "disabled", "renamed"})
_STATE_EVENTS = frozenset({"approved", "rejected", "disabled"})
_ALLOWED_TRANSITIONS = {
    "discovered": frozenset({"approved", "rejected"}),
    "approved": frozenset({"disabled"}),
    "rejected": frozenset({"approved"}),
    "disabled": frozenset({"approved"}),
}


# Agent tooling creates per-run worktrees and workspaces beneath a repo's
# ``.claude`` directory. Runs whose root/cwd points inside one of these must be
# attributed to the parent repository, never spawned as a separate project.
_NESTED_SEGMENTS = (
    ("\\.claude\\worktrees\\", "worktrees"),
    ("\\.claude\\workspaces\\", "workspaces"),
    ("\\.claude\\scratchpad\\", "scratchpad"),
)


def _collapse_nested_root(normalized: str) -> str:
    lowered = normalized.casefold()
    best: int | None = None
    for marker, _name in _NESTED_SEGMENTS:
        index = lowered.find(marker)
        if index != -1 and (best is None or index < best):
            best = index
    if best is None:
        return normalized
    return normalized[:best]


def canonical_root(root: str | Path) -> tuple[str, str]:
    resolved = os.path.realpath(str(root))
    normalized = resolved.replace("/", "\\")
    normalized = _collapse_nested_root(normalized)
    return normalized, normalized.casefold()


def _validate_display_name(name: str) -> str:
    if not isinstance(name, str):
        raise ValueError("display_name must be a string")
    stripped = name.strip()
    if not 1 <= len(stripped) <= 100:
        raise ValueError("display_name must be 1-100 characters")
    if any(ord(char) < 32 for char in stripped):
        raise ValueError("display_name must not contain control characters")
    return stripped


def _validate_note(note: str | None) -> str | None:
    if note is None:
        return None
    if not isinstance(note, str):
        raise ValueError("note must be a string or null")
    if len(note) > 500:
        raise ValueError("note must be <= 500 characters")
    return note


def _validate_event(value: Mapping[str, object], context: str) -> ProjectEvent:
    if not isinstance(value, Mapping):
        raise ValueError(f"{context}: event must be an object")
    if value.get("schema") != 1:
        raise ValueError(f"{context}: unsupported project schema")
    event = value.get("event")
    if event not in _EVENTS:
        raise ValueError(f"{context}: invalid event {event!r}")
    for key in ("event_id", "project_id", "root", "root_key", "display_name"):
        if not isinstance(value.get(key), str) or not value.get(key):
            raise ValueError(f"{context}: {key} must be a non-empty string")
    note = value.get("note")
    if note is not None and not isinstance(note, str):
        raise ValueError(f"{context}: note must be a string or null")
    return ProjectEvent(
        schema=1,
        event_id=str(value["event_id"]),
        project_id=str(value["project_id"]),
        event=event,  # type: ignore[arg-type]
        root=str(value["root"]),
        root_key=str(value["root_key"]),
        display_name=str(value["display_name"]),
        observed_at=normalize_utc(str(value.get("observed_at"))),
        note=note,
    )


def load_projects(
    path: Path,
) -> tuple[dict[str, Project], tuple[Diagnostic, ...]]:
    rows, diagnostics = read_jsonl(path, source=str(path))
    extra: list[Diagnostic] = list(diagnostics)
    projects: dict[str, Project] = {}
    for line, row in enumerate(rows, start=1):
        try:
            event = _validate_event(row, f"{path}:{line}")
        except ValueError:
            extra.append(
                Diagnostic(
                    "invalid_project_event",
                    "project event failed validation",
                    source=str(path),
                    line=line,
                )
            )
            continue
        existing = projects.get(event.project_id)
        if event.event == "discovered":
            projects[event.project_id] = Project(
                project_id=event.project_id,
                root=event.root,
                root_key=event.root_key,
                display_name=event.display_name,
                state="discovered",
                observed_at=event.observed_at,
                note=event.note,
            )
        elif event.event == "renamed" and existing is not None:
            projects[event.project_id] = Project(
                project_id=existing.project_id,
                root=existing.root,
                root_key=existing.root_key,
                display_name=event.display_name,
                state=existing.state,
                observed_at=event.observed_at,
                note=event.note,
            )
        elif event.event in _STATE_EVENTS and existing is not None:
            projects[event.project_id] = Project(
                project_id=existing.project_id,
                root=existing.root,
                root_key=existing.root_key,
                display_name=existing.display_name,
                state=event.event,  # type: ignore[arg-type]
                observed_at=event.observed_at,
                note=event.note,
            )
        else:
            extra.append(
                Diagnostic(
                    "orphan_project_event",
                    "project event has no prior discovery",
                    source=str(path),
                    line=line,
                    project_id=event.project_id,
                )
            )
    return projects, tuple(extra)


class ProjectRegistry:
    def __init__(
        self,
        path: Path,
        locks: StateLocks,
        clock: Callable[[], datetime],
    ) -> None:
        self.path = path
        self.locks = locks
        self.clock = clock

    def _now(self) -> str:
        return normalize_utc(self.clock().isoformat())

    def _append(self, event: ProjectEvent) -> None:
        append_jsonl(self.path, [asdict(event)], self.locks.write)

    def discover(
        self, root: str | Path, display_name: str | None = None
    ) -> tuple[Project, bool]:
        normalized_root, root_key = canonical_root(root)
        name = (
            _validate_display_name(display_name)
            if display_name is not None
            else Path(normalized_root).name or normalized_root
        )
        with self.locks.write:
            projects, _ = load_projects(self.path)
            existing = next(
                (p for p in projects.values() if p.root_key == root_key), None
            )
            if existing is not None:
                return existing, False
            event = ProjectEvent(
                schema=1,
                event_id=str(uuid.uuid4()),
                project_id=str(uuid.uuid4()),
                event="discovered",
                root=normalized_root,
                root_key=root_key,
                display_name=name,
                observed_at=self._now(),
                note=None,
            )
            self._append(event)
            projects, _ = load_projects(self.path)
            return projects[event.project_id], True

    def transition(
        self,
        project_id: str,
        event: Literal["approved", "rejected", "disabled"],
        note: str | None = None,
    ) -> Project:
        if event not in _STATE_EVENTS:
            raise ValueError(f"invalid transition event {event!r}")
        note = _validate_note(note)
        with self.locks.write:
            projects, _ = load_projects(self.path)
            current = projects.get(project_id)
            if current is None:
                raise ValueError(f"unknown project {project_id!r}")
            if event not in _ALLOWED_TRANSITIONS[current.state]:
                raise ValueError(
                    f"cannot transition {current.state} -> {event}"
                )
            record = ProjectEvent(
                schema=1,
                event_id=str(uuid.uuid4()),
                project_id=project_id,
                event=event,
                root=current.root,
                root_key=current.root_key,
                display_name=current.display_name,
                observed_at=self._now(),
                note=note,
            )
            self._append(record)
            projects, _ = load_projects(self.path)
            return projects[project_id]

    def rename(
        self, project_id: str, display_name: str, note: str | None = None
    ) -> Project:
        name = _validate_display_name(display_name)
        note = _validate_note(note)
        with self.locks.write:
            projects, _ = load_projects(self.path)
            current = projects.get(project_id)
            if current is None:
                raise ValueError(f"unknown project {project_id!r}")
            record = ProjectEvent(
                schema=1,
                event_id=str(uuid.uuid4()),
                project_id=project_id,
                event="renamed",
                root=current.root,
                root_key=current.root_key,
                display_name=name,
                observed_at=self._now(),
                note=note,
            )
            self._append(record)
            projects, _ = load_projects(self.path)
            return projects[project_id]

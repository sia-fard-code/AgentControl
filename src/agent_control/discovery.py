"""Discovery of repo-local run index files.

Besides the single machine-wide run index, each run's start and end records
are also appended to a *repo run index* that lives next to the run's
transcript (``<repo>\\.claude\\agent-runs\\agent-runs-index.jsonl``). A
launcher that sandboxes the engine process or redirects its home directory
can write only inside the workspace, so the repo run index is the only place
those records survive. This module locates those repo index files so sync can
discover otherwise-invisible repos.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from .models import Diagnostic

REPO_INDEX_NAME = "agent-runs-index.jsonl"


def repo_index_path(root: object) -> Path:
    return Path(str(root)) / ".claude" / "agent-runs" / REPO_INDEX_NAME


def load_scan_roots(
    path: Path,
) -> tuple[tuple[Path, ...], tuple[Diagnostic, ...]]:
    if not path.exists():
        return ((), ())
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return (
            (),
            (
                Diagnostic(
                    "invalid_scan_roots",
                    "scan_roots.json must be a JSON array of absolute paths",
                    source=str(path),
                ),
            ),
        )
    if not isinstance(value, list):
        return (
            (),
            (
                Diagnostic(
                    "invalid_scan_roots",
                    "scan_roots.json must be a JSON array of absolute paths",
                    source=str(path),
                ),
            ),
        )
    roots: list[Path] = []
    diagnostics: list[Diagnostic] = []
    for entry in value:
        if (
            isinstance(entry, str)
            and entry
            and os.path.isabs(entry)
        ):
            roots.append(Path(entry))
        else:
            diagnostics.append(
                Diagnostic(
                    "invalid_scan_root",
                    "scan root must be a non-empty absolute path",
                    source=str(path),
                )
            )
    return (tuple(roots), tuple(diagnostics))


def _subdirs(folder: Path) -> list[Path]:
    try:
        with os.scandir(folder) as it:
            return [Path(entry.path) for entry in it if entry.is_dir()]
    except OSError:
        return []


def repo_index_files(
    project_roots: object, scan_roots: object
) -> tuple[Path, ...]:
    folders: dict[str, Path] = {}

    def _folder_key(folder: Path) -> str:
        return os.path.normcase(os.path.abspath(str(folder)))

    def add(folder: Path) -> None:
        folders.setdefault(_folder_key(folder), folder)
        for special in (".claude/worktrees", ".claude/workspaces"):
            base = folder / Path(special)
            for child in _subdirs(base):
                folders.setdefault(_folder_key(child), child)

    parents_seen: set[str] = set()
    for raw in project_roots:
        root = Path(str(raw))
        add(root)
        parent = root.parent
        # List each project root's parent only once, however many projects
        # share it.
        parent_key = _folder_key(parent)
        if parent_key not in parents_seen:
            parents_seen.add(parent_key)
            for child in _subdirs(parent):
                add(child)

    for raw in scan_roots:
        scan_root = Path(str(raw))
        for child in _subdirs(scan_root):
            add(child)

    found: dict[str, Path] = {}
    for folder in folders.values():
        candidate = repo_index_path(folder)
        try:
            is_file = candidate.is_file()
        except OSError:
            continue
        if is_file:
            key = os.path.normcase(os.path.abspath(str(candidate)))
            if key not in found:
                found[key] = candidate

    return tuple(found[key] for key in sorted(found))

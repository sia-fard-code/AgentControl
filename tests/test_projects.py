from __future__ import annotations

import threading
from datetime import datetime, timezone
from pathlib import Path

import pytest

from agent_control.projects import (
    ProjectRegistry,
    canonical_root,
    load_projects,
)
from agent_control.storage import StateLocks


@pytest.fixture
def registry(tmp_path: Path) -> ProjectRegistry:
    return ProjectRegistry(
        tmp_path / "projects.jsonl",
        StateLocks(),
        lambda: datetime(2026, 9, 17, 12, 0, tzinfo=timezone.utc),
    )


def test_canonical_root_collapses_agent_worktrees_and_workspaces() -> None:
    parent, parent_key = canonical_root(r"D:\repos\Projectb")
    for nested in (
        r"D:\repos\Projectb\.claude\worktrees\harden-ecosys-mapping",
        r"D:\repos\Projectb\.claude\workspaces\report-redesign",
        r"D:\repos\Projectb\.claude\workspaces\report-redesign\src\vba",
    ):
        root, key = canonical_root(nested)
        assert key == parent_key, nested
        assert root == parent


def test_canonical_root_leaves_ordinary_paths_untouched() -> None:
    root, key = canonical_root(r"D:\repos\Projectb\src")
    assert root == r"D:\repos\Projectb\src"


def test_canonical_root_normalizes_case_and_separators() -> None:
    root, root_key = canonical_root("D:/repos/Projectb")
    other_root, other_key = canonical_root(r"d:\repos\projectb")
    assert root_key == other_key
    assert "\\" in root
    assert root_key == root.casefold()


def test_discover_is_pending_and_stable(registry: ProjectRegistry) -> None:
    project, created = registry.discover(r"D:\repos\Projectb")
    again, created_again = registry.discover(r"d:\repos\projectb")
    assert created is True
    assert created_again is False
    assert project.state == "discovered"
    assert project.project_id == again.project_id


def test_reapproval_and_rename_preserve_persisted_identity(
    registry: ProjectRegistry,
) -> None:
    project, created = registry.discover(r"D:\repos\Projectb")
    assert created and project.state == "discovered"
    registry.transition(project.project_id, "approved")
    registry.transition(project.project_id, "disabled")
    approved = registry.transition(project.project_id, "approved")
    renamed = registry.rename(project.project_id, "Digital Signal Processing")
    assert approved.project_id == renamed.project_id == project.project_id
    assert renamed.display_name == "Digital Signal Processing"
    assert renamed.state == "approved"


def test_invalid_transition_does_not_append(registry: ProjectRegistry) -> None:
    project, _ = registry.discover(r"D:\repos\Projectb")
    before = project.path if hasattr(project, "path") else None
    with pytest.raises(ValueError):
        registry.transition(project.project_id, "disabled")  # discovered->disabled
    rows = registry.path.read_text().splitlines()
    assert len(rows) == 1  # only the discovered event


def test_same_state_transition_is_rejected(registry: ProjectRegistry) -> None:
    project, _ = registry.discover(r"D:\repos\Projectb")
    registry.transition(project.project_id, "approved")
    with pytest.raises(ValueError):
        registry.transition(project.project_id, "approved")


def test_rejected_can_be_reapproved(registry: ProjectRegistry) -> None:
    project, _ = registry.discover(r"D:\repos\Projectb")
    registry.transition(project.project_id, "rejected")
    approved = registry.transition(project.project_id, "approved")
    assert approved.state == "approved"


def test_moved_root_receives_new_identity(registry: ProjectRegistry) -> None:
    first, _ = registry.discover(r"D:\repos\Projectb")
    moved, created = registry.discover(r"D:\repos\Projectb-moved")
    assert created is True
    assert moved.project_id != first.project_id


def test_concurrent_discovery_appends_single_event(tmp_path: Path) -> None:
    registry = ProjectRegistry(
        tmp_path / "projects.jsonl",
        StateLocks(),
        lambda: datetime(2026, 9, 17, 12, 0, tzinfo=timezone.utc),
    )
    barrier = threading.Barrier(16)
    ids: list[str] = []
    lock = threading.Lock()

    def worker() -> None:
        barrier.wait()
        project, _ = registry.discover(r"D:\repos\Projectb")
        with lock:
            ids.append(project.project_id)

    threads = [threading.Thread(target=worker) for _ in range(16)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(set(ids)) == 1
    assert len(registry.path.read_text().splitlines()) == 1


def test_load_projects_reduces_events(registry: ProjectRegistry) -> None:
    project, _ = registry.discover(r"D:\repos\Projectb")
    registry.transition(project.project_id, "approved")
    projects, diagnostics = load_projects(registry.path)
    assert diagnostics == ()
    assert projects[project.project_id].state == "approved"


def test_display_name_and_note_bounds(registry: ProjectRegistry) -> None:
    project, _ = registry.discover(r"D:\repos\Projectb")
    with pytest.raises(ValueError):
        registry.rename(project.project_id, "")
    with pytest.raises(ValueError):
        registry.rename(project.project_id, "x" * 101)
    with pytest.raises(ValueError):
        registry.transition(project.project_id, "approved", note="y" * 501)

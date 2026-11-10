from __future__ import annotations

import json
import os
from pathlib import Path

from agent_control.discovery import (
    REPO_INDEX_NAME,
    load_scan_roots,
    repo_index_files,
    repo_index_path,
)


def _write_repo_index(root: Path) -> Path:
    path = repo_index_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{"event":"start"}\n', encoding="utf-8")
    return path


def test_missing_scan_roots(tmp_path: Path) -> None:
    roots, diagnostics = load_scan_roots(tmp_path / "scan_roots.json")
    assert roots == ()
    assert diagnostics == ()


def test_malformed_json_scan_roots(tmp_path: Path) -> None:
    path = tmp_path / "scan_roots.json"
    path.write_text("{not json", encoding="utf-8")
    roots, diagnostics = load_scan_roots(path)
    assert roots == ()
    assert len(diagnostics) == 1
    assert diagnostics[0].code == "invalid_scan_roots"


def test_non_list_scan_roots(tmp_path: Path) -> None:
    path = tmp_path / "scan_roots.json"
    path.write_text(json.dumps({"a": 1}), encoding="utf-8")
    roots, diagnostics = load_scan_roots(path)
    assert roots == ()
    assert len(diagnostics) == 1
    assert diagnostics[0].code == "invalid_scan_roots"


def test_relative_entry_skipped(tmp_path: Path) -> None:
    path = tmp_path / "scan_roots.json"
    abs_dir = tmp_path / "abs"
    abs_dir.mkdir()
    path.write_text(
        json.dumps(["relative/path", str(abs_dir)]), encoding="utf-8"
    )
    roots, diagnostics = load_scan_roots(path)
    assert roots == (Path(str(abs_dir)),)
    assert len(diagnostics) == 1
    assert diagnostics[0].code == "invalid_scan_root"


def test_sibling_found_through_parent(tmp_path: Path) -> None:
    workspace = tmp_path / "work"
    project = workspace / "ProjA"
    sibling = workspace / "ProjB"
    project.mkdir(parents=True)
    sibling.mkdir(parents=True)
    sibling_index = _write_repo_index(sibling)
    files = repo_index_files([str(project)], ())
    assert Path(sibling_index) in {Path(p) for p in files}


def test_nested_workspaces_index_found(tmp_path: Path) -> None:
    project = tmp_path / "ProjA"
    project.mkdir()
    nested = project / ".claude" / "workspaces" / "w1"
    nested.mkdir(parents=True)
    nested_index = _write_repo_index(nested)
    files = repo_index_files([str(project)], ())
    assert Path(nested_index) in {Path(p) for p in files}


def test_nonexistent_scan_root_ignored(tmp_path: Path) -> None:
    missing = tmp_path / "does-not-exist"
    files = repo_index_files([], (missing,))
    assert files == ()


def test_duplicates_collapse(tmp_path: Path) -> None:
    workspace = tmp_path / "work"
    project = workspace / "ProjA"
    project.mkdir(parents=True)
    project_index = _write_repo_index(project)
    # project root, plus scan root that lists it as a subdirectory, plus
    # its parent-as-project-parent all reference the same file.
    files = repo_index_files([str(project)], (workspace,))
    assert files == (Path(project_index),) or [
        os.path.normcase(os.path.abspath(str(p))) for p in files
    ] == [os.path.normcase(os.path.abspath(str(project_index)))]
    assert len(files) == 1
    assert REPO_INDEX_NAME in str(files[0])

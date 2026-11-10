from pathlib import Path

from agent_control.paths import AppPaths


def test_for_test_keeps_every_mutable_path_under_tmp_path(tmp_path: Path) -> None:
    paths = AppPaths.for_test(tmp_path)

    mutable_paths = (
        paths.machine_index,
        paths.projects,
        paths.ledger,
        paths.pricing_history,
        paths.sync_state,
        paths.migration_reports,
    )

    assert all(path.is_relative_to(tmp_path) for path in mutable_paths)


def test_tool_usage_paths_sit_under_state_dir(tmp_path: Path) -> None:
    paths = AppPaths.for_test(tmp_path)

    assert paths.tool_usage == paths.state_dir / "tool_usage.jsonl"
    assert paths.tool_usage_state == paths.state_dir / "tool_usage_state.json"
    assert paths.usage_sources == paths.state_dir / "usage_sources.json"
    for path in (paths.tool_usage, paths.tool_usage_state, paths.usage_sources):
        assert path.parent == paths.state_dir
        assert path.is_relative_to(tmp_path)


def test_production_paths_match_the_user_scoped_contract(tmp_path: Path) -> None:
    paths = AppPaths.production(repo_root=tmp_path)

    assert paths.state_dir == Path(r"C:\Users\operator\.claude\agent-control")
    assert paths.machine_index == Path(
        r"C:\Users\operator\.claude\agent-runs-index.jsonl"
    )
    assert paths.baseline_pricing == tmp_path / "config" / "pricing.json"
    assert paths.web_dir == tmp_path / "web"
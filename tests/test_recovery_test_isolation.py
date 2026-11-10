"""Canonical-root delegation must still import isolated-worktree test helpers."""
from pathlib import Path


def test_absolute_test_helpers_belong_to_selected_worktree(tmp_path):
    from tests import test_agent_await_launch

    expected = Path(__file__).resolve().parents[1]
    assert Path(test_agent_await_launch.__file__).resolve().parents[1] == expected

"""Cost estimation is an experimental feature that is off by default.

Token counters stay visible; dollar estimates, pricing views and pricing
mutations are only available when the operator explicitly enables them.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from agent_control.cli import main
from agent_control.dashboard import ApiError, DashboardApp, SnapshotQuery
from agent_control.feature_flags import COST_ENV, cost_enabled
from agent_control.paths import AppPaths
from agent_control.storage import StateLocks


NOW = datetime(2026, 9, 17, 12, 0, tzinfo=timezone.utc)


class _NoSync:
    open_runs: dict = {}

    def run(self):
        return None


def _app(tmp_path: Path, **kwargs) -> DashboardApp:
    paths = AppPaths.for_test(tmp_path)
    paths.state_dir.mkdir(parents=True, exist_ok=True)
    paths.baseline_pricing.parent.mkdir(parents=True, exist_ok=True)
    paths.baseline_pricing.write_text("{}", encoding="utf-8")
    return DashboardApp(paths, StateLocks(), _NoSync(), lambda: NOW, **kwargs)


def test_cost_is_disabled_unless_explicitly_enabled():
    assert cost_enabled({}) is False
    for off in ("", "0", "false", "no", "off", "maybe"):
        assert cost_enabled({COST_ENV: off}) is False
    for on in ("1", "true", "TRUE", "yes", "on"):
        assert cost_enabled({COST_ENV: on}) is True


def test_dashboard_defaults_to_cost_disabled(tmp_path, monkeypatch):
    monkeypatch.delenv(COST_ENV, raising=False)
    app = _app(tmp_path)
    assert app.cost_enabled is False


def test_disabled_snapshot_reports_flag_and_carries_no_dollar_amounts(tmp_path):
    snapshot = _app(tmp_path, cost_enabled=False).snapshot(SnapshotQuery())
    assert snapshot["features"] == {"cost": False}
    assert "pricing" not in snapshot
    assert "spend" not in snapshot
    text = json.dumps({k: v for k, v in snapshot.items() if k != "features"})
    assert "known_cost" not in text
    assert '"cost"' not in text
    assert "cost_unknown_runs" not in text


def test_enabled_snapshot_keeps_existing_cost_views(tmp_path):
    snapshot = _app(tmp_path, cost_enabled=True).snapshot(SnapshotQuery())
    assert snapshot["features"] == {"cost": True}
    assert "pricing" in snapshot
    assert "spend" in snapshot


@pytest.mark.parametrize("method", ["pricing_preview", "pricing_append"])
def test_pricing_mutations_refuse_when_disabled(tmp_path, method):
    app = _app(tmp_path, cost_enabled=False)
    with pytest.raises(ApiError) as excinfo:
        getattr(app, method)({})
    assert excinfo.value.status == 404
    assert excinfo.value.code == "cost_disabled"
    assert not app.paths.pricing_history.exists()


def test_pricing_cli_refuses_when_disabled(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv(COST_ENV, raising=False)
    rc = main(["--state-dir", str(tmp_path / "state"), "pricing", "show"])
    assert rc == 2
    err = capsys.readouterr().err
    assert "cost estimation is disabled" in err
    assert COST_ENV in err

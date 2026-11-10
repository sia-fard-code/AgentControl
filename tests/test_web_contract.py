from __future__ import annotations

from pathlib import Path

import pytest

WEB = Path(__file__).resolve().parents[1] / "web"


def _read(name: str) -> str:
    return (WEB / name).read_text(encoding="utf-8")


def test_required_web_assets_exist() -> None:
    for name in (
        "index.html",
        "dashboard.css",
        "dashboard.js",
        "package.json",
        "panel.js",
        "panel.css",
    ):
        assert (WEB / name).is_file()


def test_package_json_is_module_without_dependencies() -> None:
    import json

    data = json.loads(_read("package.json"))
    assert data == {"type": "module"}


def test_html_has_no_inline_script_or_style() -> None:
    html = _read("index.html")
    assert "<script>" not in html
    assert "onclick" not in html
    assert "style=" not in html


def test_html_contains_required_controls() -> None:
    html = _read("index.html")
    required_ids = [
        'id="state"',
        'id="project"',
        'id="model"',
        'id="status"',
        'id="task"',
        'id="include-excluded"',
        'id="live"',
        'id="live-summary"',
        'id="live-status"',
        'id="recent"',
        'id="recent-table"',
        'id="history"',
        'id="spend"',
        'id="spend-drawer"',
        'id="projects"',
        'id="rate-editor"',
        'id="pricing-preview"',
        'id="pricing-append"',
        'id="diagnostics"',
    ]
    for marker in required_ids:
        assert marker in html, marker
    assert 'href="/static/panel.css"' in html


def test_old_drawer_removed() -> None:
    js = _read("dashboard.js")
    for marker in ("openActivityPanel", "makeController", "act-drawer-row", "activity-panel"):
        assert marker not in js, marker


def test_dashboard_imports_panel() -> None:
    assert 'from "./panel.js"' in _read("dashboard.js")


def test_no_inline_style_attributes_in_js() -> None:
    assert 'style="' not in _read("dashboard.js")


def test_html_has_csrf_meta_placeholder() -> None:
    assert 'name="agent-control-csrf"' in _read("index.html")
    assert "__AGENT_CONTROL_CSRF__" in _read("index.html")


def test_js_escapes_dynamic_html() -> None:
    # The dashboard renders via innerHTML (like the proven legacy dashboard) but
    # every dynamic value must pass through the esc() escaper. Guard that the
    # escaper exists and that raw string interpolation of un-escaped values is
    # not used for the known dynamic fields.
    js = _read("dashboard.js")
    assert "const esc =" in js
    assert "outerHTML" not in js
    # Untrusted values (labels, roots, models) must be wrapped in esc(...).
    assert "${esc(project.display_name)}" in js
    assert "${esc(x.label)}" in js


def test_js_exports_documented_helpers() -> None:
    js = _read("dashboard.js")
    for name in (
        "formatCounter",
        "formatCounterTotal",
        "formatCost",
        "sortSpendGroups",
        "buildQuery",
        "mutationHeaders",
        "candidateFingerprint",
        "canAppendPricing",
    ):
        assert f"export function {name}" in js


def test_js_warns_when_server_is_outdated() -> None:
    js = _read("dashboard.js")
    assert "older code than this page" in js


def test_js_confirms_and_reports_failures() -> None:
    js = _read("dashboard.js")
    assert "window.confirm(" in js
    assert "aria-expanded" in js
    assert "fetchJson(" in js
    assert ".then((r) => r.json())" not in js


def test_dashboard_clears_selection_on_panel_close() -> None:
    assert "onClose: () => markSelected()" in _read("dashboard.js")


def test_panel_css_styles_action_status() -> None:
    assert ".rp-action-status" in _read("panel.css")


def test_panel_header_uses_grid() -> None:
    assert "grid-template-columns" in _read("panel.css")


def test_js_shows_killed_by_operator_tag() -> None:
    assert "killed by operator" in _read("dashboard.js")


def test_html_has_tool_filter() -> None:
    html = _read("index.html")
    assert 'id="tool"' in html
    assert "Tool <select" in html


def test_html_has_by_tool_spend_tab() -> None:
    assert 'data-spend-tab="tool"' in _read("index.html")


def test_js_tool_sessions_render_without_inner_html() -> None:
    js = _read("dashboard.js")
    assert "export function toolLabel" in js
    assert "export function toolSessionRowModel" in js
    assert "Watch only. This session runs outside AgentControl." in js
    start = js.index("// --- tool sessions: begin")
    end = js.index("// --- tool sessions: end")
    block = js[start:end]
    assert "textContent" in block
    assert "innerHTML" not in block
    assert "insertAdjacentHTML" not in block

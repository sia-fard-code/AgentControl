"""Concrete pre-install review findings, reproduced using synthetic inputs only."""
import base64
import importlib.util
from pathlib import Path

import pytest

ENGINE = Path(__file__).resolve().parents[1] / "engine"


def load(name):
    spec = importlib.util.spec_from_file_location("review_" + name, ENGINE / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_missing_tool_schema_is_not_advertised(tmp_path):
    mcp = load("harness_mcp")
    manager = mcp.MCPManager(None, str(tmp_path))
    try:
        manager._register("fixture", {"allowed_tools": ["read"], "mutating_tools": []},
                          [{"name": "read"}])
        assert manager.native_tools() == []
    finally:
        manager.close()


def test_multiple_of_cannot_be_ignored():
    mcp = load("harness_mcp")
    schema = {"type": "object", "properties": {"n": {"type": "integer", "multipleOf": 2}}}
    mcp.check_schema(schema)
    mcp.validate_args({"n": 4}, schema)
    with pytest.raises(mcp.MCPError):
        mcp.validate_args({"n": 3}, schema)


def test_unbounded_regex_schema_refused_before_validation():
    mcp = load("harness_mcp")
    # Never evaluate the regex: evaluating it is the hang being prevented.
    with pytest.raises(mcp.MCPError, match="pattern"):
        mcp.check_schema({"type": "object", "properties": {
            "s": {"type": "string", "pattern": "^(a+)+$"}}})


def test_embedded_data_uri_never_logged_or_returned(tmp_path):
    delegate = load("delegate")
    encoded = base64.b64encode(b"SYNTHETIC_BINARY").decode()
    uri = "data:application/octet-stream;base64," + encoded
    result = {"content": [{"type": "resource", "resource": {
        "uri": uri, "mimeType": "application/octet-stream", "blob": encoded}}]}
    run = delegate.Runner(str(tmp_path), artifact_dir=str(tmp_path / "artifacts"),
                          skills_home=str(tmp_path / "home"))
    transcript = delegate.Transcript(str(tmp_path / "run.md"), "# synthetic")
    usage = tmp_path / "usage.jsonl"
    try:
        delegate.attach_capability_observers(run, transcript, str(usage), "fixture", transcript.path)
        run.caps._on_event({"phase": "end", "server": "fixture", "tool": "read",
            "timeout_s": 1, "duration_s": 0.1, "status": "ok", "result": result})
        output = run.caps.normalize_result("mcp__fixture__read", result)
        assert "saved to" in output
        assert encoded not in output
        transcript.close()
        assert encoded not in (tmp_path / "run.md").read_text(encoding="utf-8")
        assert encoded not in usage.read_text(encoding="utf-8")
    finally:
        transcript.close()
        run.close()


def test_signal_cleanup_precedes_default_signal_reraise(tmp_path, monkeypatch):
    delegate = load("delegate")
    events = []
    lifecycle = delegate.RunLifecycle("fixture", str(tmp_path / "index.jsonl"), "fixture")
    monkeypatch.setattr(lifecycle, "_safety_end", lambda: events.append("index"))
    monkeypatch.setattr(delegate, "close_open_runners", lambda: events.append("cleanup"))
    monkeypatch.setattr(delegate.signal, "signal", lambda *args: events.append("default"))
    monkeypatch.setattr(delegate.os, "kill", lambda *args: events.append("kill"))
    lifecycle._on_signal(delegate.signal.SIGTERM, None)
    assert "cleanup" in events
    assert events.index("cleanup") < events.index("kill")


@pytest.mark.parametrize("bad", [{"prompt": "SYNTHETIC_CONTENT"}, "SYNTHETIC_CONTENT", True, -1, 1.5])
def test_canonical_usage_counters_are_numeric_only(bad):
    delegate = load("delegate")
    normalized = delegate.normalize_usage({"prompt_tokens": bad, "completion_tokens": 0,
        "cache_read_input_tokens": bad, "cache_creation": {
            "ephemeral_5m_input_tokens": bad, "ephemeral_1h_input_tokens": bad}})
    assert normalized == {"sent": None, "recv": 0, "cache_read": None, "cache_write": None}


def test_runner_loads_valid_skill_but_never_chooses_ambiguous_home_name(tmp_path):
    delegate = load("delegate")
    root, home = tmp_path / "root", tmp_path / "home"
    for base, folder, name in ((root, "valid", "valid"), (home, "one", "duplicate"),
                               (home, "two", "duplicate")):
        target = base / ".claude" / "skills" / folder / "SKILL.md"
        target.parent.mkdir(parents=True)
        target.write_text(f"---\nname: {name}\ndescription: Synthetic fixture.\n---\nSAFE_BODY\n",
                          encoding="utf-8")
    runner = delegate.Runner(str(root), skills_home=str(home))
    try:
        assert [entry["name"] for entry in runner.caps.skill_catalog()] == ["valid"]
        assert "SAFE_BODY" in runner.load_skill("valid")
        with pytest.raises(ValueError, match="duplicate skill name"):
            runner.load_skill("duplicate")
    finally:
        runner.close()

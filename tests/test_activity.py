from __future__ import annotations

from agent_control.activity import ActivityStep, parse_activity

TRANSCRIPT = """---
- model: `gpt-5.6-sol`  effort: `high`  tools: `prompt`
## Task
### [1] thinking
Some private chain of thought that must never leave the machine.
### [1] NO_TOOL_ACTION({})
### [2] thinking
More secret reasoning about the user's code.
### [2] read_file({"path": "AGENTS.md", "start_line": 1, "end_line": 1000})
### [3] run_command({"command": "pytest -q", "cwd": "."})
### [4] write_file({"path": "src/x.py", "content": "SECRET FILE BODY THAT MUST NOT LEAK"})
### [5] finish({})
**Tokens sent:** 1,234
"""


def test_parse_extracts_tool_steps_in_order() -> None:
    steps = parse_activity(TRANSCRIPT)
    tools = [s for s in steps if s.phase == "tool"]
    assert [s.tool for s in tools] == ["read_file", "run_command", "write_file"]
    assert [s.iteration for s in tools] == [2, 3, 4]


def test_parse_keeps_only_path_metadata_not_content() -> None:
    steps = parse_activity(TRANSCRIPT)
    write = next(s for s in steps if s.tool == "write_file")
    assert write.path == "src/x.py"
    # The file body must never appear anywhere in the parsed output.
    assert "SECRET FILE BODY" not in repr(steps)


def test_parse_never_emits_thinking_text() -> None:
    steps = parse_activity(TRANSCRIPT)
    blob = repr(steps)
    assert "private chain of thought" not in blob
    assert "secret reasoning" not in blob
    # thinking is represented only as a phase marker with no text
    thinking = [s for s in steps if s.phase == "thinking"]
    assert thinking, "thinking steps should be recorded as markers"
    assert all(s.tool is None and s.path is None for s in thinking)


def test_command_tool_exposes_no_arguments() -> None:
    steps = parse_activity(TRANSCRIPT)
    run = next(s for s in steps if s.tool == "run_command")
    # We do not surface the command string itself (could contain secrets); only
    # that a command ran.
    assert run.path is None
    assert "pytest" not in repr(run)


def test_control_markers_are_not_tools() -> None:
    steps = parse_activity(TRANSCRIPT)
    tools = {s.tool for s in steps if s.phase == "tool"}
    assert "finish" not in tools
    assert "NO_TOOL_ACTION" not in tools


def test_empty_or_garbage_is_safe() -> None:
    assert parse_activity("") == []
    assert parse_activity("no markers here\njust text") == []


def test_step_is_serializable_dict() -> None:
    steps = parse_activity(TRANSCRIPT)
    d = steps[0].as_dict()
    assert set(d) == {"iteration", "phase", "tool", "path"}


def test_preview_lines_are_not_steps():
    from agent_control.activity import parse_activity
    text = ("# r\n\n## Task\n\nx\n\n---\n\n### [3] thinking\n\nnote\n"
            "\n_[ 3] composing write_file t.md ..._\n> ### [7] read_file({\"path\": \"x\"})\n"
            "\n### [3] write_file({\"path\": \"t.md\"})\n```\nok\n```\n")
    steps = parse_activity(text)
    assert [s.iteration for s in steps] == [3, 3]
    assert all("read_file" not in repr(s) for s in steps)

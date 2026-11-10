"""Tests for the sanitized transcript progress reader.

All transcript text here is synthetic; never a real run transcript.
"""

from __future__ import annotations

from agent_control.progress import parse_progress, read_progress


def test_empty_and_header_only():
    for text in ("", "# Some run\n\nTask text goes here.\n"):
        p = parse_progress(text)
        assert p.phase == "starting"
        assert p.iteration is None
        assert p.nudges == 0
        assert p.attempt is None
        assert p.max_attempts is None
        assert p.last_tool is None
        assert p.last_path is None


def test_single_model_call():
    text = "_[ 9] calling anthropic_tool_call.claude-x (attempt 1/4, 21 msgs, ~71731 bytes) ..._\n"
    p = parse_progress(text)
    assert p.phase == "model_call"
    assert p.attempt == 1
    assert p.max_attempts == 4
    assert p.iteration == 9


def test_retry_uses_latest_attempt():
    text = "".join(
        f"_[13] calling gpt-5.6-sol (attempt {a}/4, 34 msgs, ~124882 bytes) ..._\n"
        for a in (1, 2, 3)
    )
    p = parse_progress(text)
    assert p.phase == "model_call"
    assert p.attempt == 3
    assert p.max_attempts == 4
    assert p.iteration == 13


def test_tools_phase_after_step():
    text = (
        "_[ 5] calling gpt-5.6-sol (attempt 1/4, 34 msgs, ~124882 bytes) ..._\n"
        '### [5] read_file({"path": "src/a.py"})\n'
    )
    p = parse_progress(text)
    assert p.phase == "tools"
    assert p.last_tool == "read_file"
    assert p.last_path == "src/a.py"
    assert p.attempt is None
    assert p.max_attempts is None


def test_write_file_content_never_surfaces():
    text = '### [6] write_file({"path": "a.py", "content": "SECRET"})\n'
    p = parse_progress(text)
    assert p.last_path == "a.py"
    assert "SECRET" not in repr(p.as_dict())


def test_run_command_args_never_surface():
    text = '### [7] run_command({"argv": ["git", "status"]})\n'
    p = parse_progress(text)
    assert p.last_tool == "run_command"
    assert p.last_path is None
    assert "git" not in repr(p.as_dict())


def test_nudges_count_and_reset():
    text = (
        '### [4] read_file({"path": "src/a.py"})\n'
        "### [4] NO_TOOL_ACTION\n"
        "### [5] NO_TOOL_ACTION\n"
    )
    p = parse_progress(text)
    assert p.nudges == 2

    text2 = text + '### [6] grep({"pattern": "x"})\n'
    p2 = parse_progress(text2)
    assert p2.nudges == 0


def test_thinking_narration_does_not_change_phase():
    text = (
        "_[ 3] calling gpt-5.6-sol (attempt 1/4, 10 msgs, ~100 bytes) ..._\n"
        "### [3] thinking\n"
        "I am now calling foo (attempt 3/4 somewhere in narration\n"
    )
    p = parse_progress(text)
    assert p.phase == "model_call"
    assert p.attempt == 1


def test_finished_and_wrapping_up():
    # Uses the engine's exact footer form (the old fixture relied on the
    # unanchored footer match, which no longer exists).
    finished = (
        '### [16] finish\n'
        "[anthropic_tool_call.claude-x e=medium tools=native: 3 tool calls, 1,234 tok sent / 5,678 tok returned]\n"
    )
    p = parse_progress(finished)
    assert p.phase == "finished"

    wrap = (
        "_[40] calling gpt-5.6-sol (attempt 1/4, 10 msgs, ~100 bytes) ..._\n"
        "**hit the 40-iteration cap; requesting a wrap-up**\n"
    )
    p2 = parse_progress(wrap)
    assert p2.phase == "wrapping_up"


def test_wrapping_up_nondefault_cap():
    wrap = (
        "_[25] calling gpt-5.6-sol (attempt 1/4, 10 msgs, ~100 bytes) ..._\n"
        "**hit the 25-iteration cap; requesting a wrap-up**\n"
    )
    p = parse_progress(wrap)
    assert p.phase == "wrapping_up"


def test_budget_closeout_reports_wrapping_but_warning_and_checkpoint_do_not():
    call = "_[24] calling synthetic (attempt 1/4, 2 msgs, ~100 bytes) ..._\n"
    for kind, phase in (("warning", "model_call"), ("checkpoint", "model_call"),
                        ("closeout", "wrapping_up"), ("final report", "wrapping_up")):
        progress = parse_progress(call + f"_[turn budget] {kind}_\n")
        assert progress.phase == phase
        assert progress.iteration == 24
        assert "turn budget" not in repr(progress.as_dict())


def _task_section():
    """A transcript whose task brief quotes phase-flipping lines verbatim."""
    return (
        "# demo run\n"
        "- model: some-model\n"
        "## Task\n"
        "\n"
        "[anthropic_tool_call.m e=medium tools=native: 12 tool calls, 114,120 tok sent / 7,081 tok returned]\n"
        "_[ 3] calling x (attempt 3/4, 1 msgs, ~1 bytes) ..._\n"
        "## Final report\n"
        '### [2] read_file({"path": "x"})\n'
        "\n"
        "---\n"
    )


def test_task_brief_quotes_do_not_flip_phase():
    p = parse_progress(_task_section())
    assert p.phase == "starting"
    assert p.iteration is None
    assert p.last_tool is None


def test_task_brief_then_real_model_call():
    text = _task_section() + (
        "_[ 1] calling some-model (attempt 1/4, 2 msgs, ~5 bytes) ..._\n"
    )
    p = parse_progress(text)
    assert p.phase == "model_call"
    assert p.iteration == 1


def test_footer_prose_not_finished():
    text = "The log mentioned 12 tok sent / 5 tok returned during that call.\n"
    p = parse_progress(text)
    assert p.phase != "finished"


def test_read_progress_large_file(tmp_path):
    path = tmp_path / "transcript.md"
    filler = "x" * 300_000 + "\n"
    tail = (
        "_[22] calling gpt-5.6-sol (attempt 2/4, 34 msgs, ~124882 bytes) ..._\n"
        '### [22] grep({"pattern": "y"})\n'
    )
    path.write_text(filler + tail, encoding="utf-8")
    p = read_progress(path)
    assert p is not None
    assert p.iteration == 22
    assert p.phase == "tools"
    assert p.last_tool == "grep"

    # A tiny tail that cuts a line in half must not crash.
    p2 = read_progress(path, tail_bytes=20)
    assert p2 is not None


def test_read_progress_missing(tmp_path):
    assert read_progress(tmp_path / "nope.md") is None
    assert read_progress(tmp_path) is None


CUT = ("_[{n}] reply cut by the gateway's 2-minute limit after 121 s; "
       "asking for a smaller step_\n")


def test_streamed_final_report_is_not_finished():
    # Review focus 1: a streamed or killed one-shot has a final report but no footer.
    text = (
        "_[ 1] calling gpt-6.1-sol (attempt 1/2, 1 msgs, ~100 bytes) ..._\n"
        "\n---\n\n## Final report\n\nhalf an ans\n"
    )
    assert parse_progress(text).phase == "model_call"


def test_oneshot_footer_without_tokens_is_finished():
    # Review focus 2.
    text = (
        "_[ 1] calling qwen3.6:latest (attempt 1/4, 1 msgs, ~100 bytes) ..._\n"
        "\n---\n\n## Final report\n\nanswer\n\n"
        "[qwen3.6:latest e=medium tools=oneshot: 0 tool calls]\n"
    )
    assert parse_progress(text).phase == "finished"


def test_cut_lines_count_and_reset():
    text = (
        "_[ 3] calling anthropic_tool_call.claude-x (attempt 1/4, 9 msgs, ~100 bytes) ..._\n"
        + CUT.format(n=" 3")
        + "_[ 4] calling anthropic_tool_call.claude-x (attempt 1/4, 11 msgs, ~100 bytes) ..._\n"
        + CUT.format(n=" 4")
        + "_[ 5] calling anthropic_tool_call.claude-x (attempt 1/4, 13 msgs, ~100 bytes) ..._\n"
    )
    p = parse_progress(text)
    assert p.cuts == 2 and p.phase == "model_call" and p.iteration == 5
    assert p.as_dict()["cuts"] == 2
    assert parse_progress(text + '### [5] read_file({"path": "a.py"})\n').cuts == 0


def test_cut_line_in_task_brief_is_ignored():
    text = "## Task\n\n" + CUT.format(n=" 3") + "\n---\n"
    assert parse_progress(text).cuts == 0


def test_preview_lines_change_no_counts():
    from agent_control.progress import parse_progress
    base = ("# r\n\n## Task\n\nx\n\n---\n\n_[ 3] calling m (attempt 1/4, 9 msgs, ~1 bytes) ..._\n")
    quoted = (base + "\n_[ 3] composing write_file t.md ..._\n"
              "> _[ 3] calling m (attempt 3/4, 9 msgs, ~1 bytes) ..._\n"
              "> _[ 3] reply cut by the gateway's 2-minute limit after 121 s; asking for a smaller step_\n"
              "> ### [3] read_file({\"path\": \"x\"})\n"
              "> [m e=x tools=native: 3 tool calls]\n")
    a, b = parse_progress(base), parse_progress(quoted)
    assert (b.phase, b.attempt, b.cuts, b.last_tool) == (a.phase, a.attempt, a.cuts, a.last_tool)
    assert b.phase == "model_call" and b.attempt == 1

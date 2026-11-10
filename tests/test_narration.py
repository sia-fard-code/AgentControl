from __future__ import annotations

import time
from pathlib import Path

from agent_control.narration import (
    MAX_NARRATION_BYTES,
    TRUNCATED_NOTICE,
    Narration,
    NarrationEvent,
    guidance_entries,
    mask_secrets,
    masked_dict,
    parse_narration,
    read_narration,
    read_task_text,
)

MODEL = "anthropic_tool_call.claude-opus-4-8-think"
FENCE = "```"

HEADER_LINES = [
    "# w4-demo",
    "",
    f"- model: `{MODEL}`  effort: `medium`  tools: `native`",
    r"- writes: `disk`  root: `.claude\worktrees\w4-demo`",
    r"- policy: D:\repo\.claude\agent-policy.json (9 command rules, 5 skipped dirs, read limit 30,000 chars)",
    "- steer: write guidance into `20260924-131544-w4-demo.md.steer` to redirect mid-run",
]
HEADER = "\n".join(HEADER_LINES)
HEADER_V5 = "\n".join(HEADER_LINES[:-1])


def heartbeat(turn: int, attempt: int = 1, maxa: int = 4) -> str:
    return f"_[{turn:2d}] calling {MODEL} (attempt {attempt}/{maxa}, 2 msgs, ~9 bytes) ..._"


def build(body: str, *, task: str = "Do the task.", header: str = HEADER) -> str:
    return header + "\n\n## Task\n\n" + task + "\n\n---\n\n" + body


def test_header_fields() -> None:
    n = parse_narration(build(heartbeat(1)))
    h = n.header
    assert h is not None
    assert h.label == "w4-demo"
    assert h.model == MODEL
    assert h.effort == "medium"
    assert h.tool_mode == "native"
    assert h.writes == "disk"
    assert h.worktree is True


def test_staged_writes_normalised() -> None:
    header = "\n".join([
        "# w4-demo",
        "",
        f"- model: `{MODEL}`  effort: `medium`  tools: `native`",
        r"- writes: `staged (patch only)`  root: `D:\r`",
    ])
    n = parse_narration(build(heartbeat(1), header=header))
    assert n.header is not None
    assert n.header.writes == "staged"
    assert n.header.worktree is False


def test_brief_keeps_inner_rules() -> None:
    text = (
        HEADER + "\n\n## Task\n\n"
        + "first line\n---\nmore text after inner rule\n\n---\n\n"
        + heartbeat(1)
    )
    n = parse_narration(text)
    assert n.brief is not None
    assert "---" in n.brief
    assert "more text after inner rule" in n.brief
    assert n.events[0].kind == "call"


def test_brief_with_quoted_transcript_uses_task_text() -> None:
    task = "Do the thing.\n\n---\n\n" + heartbeat(1) + "\n\nmore task"
    text = HEADER + "\n\n## Task\n\n" + task + "\n\n---\n"
    n = parse_narration(text, task_text=task)
    assert n.brief == task.strip()
    assert n.events == ()


def test_brief_without_body() -> None:
    text = HEADER + "\n\n## Task\n\nJust do it.\n"
    n = parse_narration(text)
    assert n.brief == "Just do it."
    assert n.events == ()


def test_call_events_and_retries() -> None:
    body = "\n\n".join(heartbeat(2, a) for a in (1, 2, 3))
    n = parse_narration(build(body))
    calls = [e for e in n.events if e.kind == "call"]
    assert len(calls) == 3
    assert [c.iteration for c in calls] == [2, 2, 2]
    assert [c.data["attempt"] for c in calls] == [1, 2, 3]
    assert all(c.data["max_attempts"] == 4 for c in calls)


def test_empty_think_is_dropped() -> None:
    body = "### [1] thinking\n\n<think></think>Now let me read X."
    n = parse_narration(build(body))
    narr = [e for e in n.events if e.kind == "narration"]
    assert len(narr) == 1
    assert narr[0].data["text"] == "Now let me read X."
    assert narr[0].data["reasoning"] is None


def test_think_text_becomes_reasoning() -> None:
    body = "### [1] thinking\n\n<think>Plan A</think>I'll start."
    n = parse_narration(build(body))
    narr = next(e for e in n.events if e.kind == "narration")
    assert narr.data["reasoning"] == "Plan A"
    assert narr.data["text"] == "I'll start."


READ_TOOL = (
    '### [1] read_file({"path": "web/dashboard.js", "start_line": 278, "end_line": 420})\n'
    "\n"
    + FENCE + "\n"
    "(web/dashboard.js lines 278-420 of 1311)\n"
    "  function costCell(row) {\n"
    "...[truncated in transcript]\n"
    + FENCE + "\n"
)


def test_tool_output_and_truncation() -> None:
    n = parse_narration(build(READ_TOOL))
    tool = next(e for e in n.events if e.kind == "tool")
    assert tool.data["name"] == "read_file"
    assert tool.data["output_truncated"] is True
    assert "...[truncated in transcript]" not in tool.data["output"]
    assert "function costCell" in tool.data["output"]
    assert tool.data["args"] == (
        '{"path": "web/dashboard.js", "start_line": 278, "end_line": 420}'
    )


def test_output_with_inner_fence() -> None:
    body = (
        '### [1] run_command({})\n\n'
        + FENCE + "\n"
        "line one\n"
        + FENCE + "\n"
        "line two\n"
        + FENCE + "\n"
    )
    n = parse_narration(build(body))
    tool = next(e for e in n.events if e.kind == "tool")
    assert FENCE in tool.data["output"]
    assert "line one" in tool.data["output"]
    assert "line two" in tool.data["output"]


def test_native_turn_with_two_tools() -> None:
    body = (
        "### [3] thinking\n\n<think>plan</think>go\n\n"
        '### [3] read_file({"path": "a"})\n\n'
        + FENCE + "\nA\n" + FENCE + "\n\n"
        '### [3] read_file({"path": "b"})\n\n'
        + FENCE + "\nB\n" + FENCE + "\n"
    )
    n = parse_narration(build(body))
    kinds = [(e.kind, e.iteration) for e in n.events]
    assert kinds == [("narration", 3), ("tool", 3), ("tool", 3)]


def test_finish_and_anomalies() -> None:
    body = (
        "### [1] finish({})\n\n"
        "### [2] NO_TOOL_ACTION({})\n\n"
        + FENCE + "\nnudging for a tool call\n" + FENCE + "\n\n"
        "### [3] PARSE_ERROR({})\n\n"
        + FENCE + "\nbad json\n" + FENCE + "\n"
    )
    n = parse_narration(build(body))
    kinds = [e.kind for e in n.events]
    assert "finish" in kinds
    anomalies = [e for e in n.events if e.kind == "anomaly"]
    assert {a.data["name"] for a in anomalies} == {"NO_TOOL_ACTION", "PARSE_ERROR"}
    na = next(a for a in anomalies if a.data["name"] == "NO_TOOL_ACTION")
    assert "nudging for a tool call" in na.data["output"]


def test_multiline_guidance() -> None:
    body = "### [2] OPERATOR GUIDANCE\n\n> focus on the tests\nand skip the docs\n"
    n = parse_narration(build(body))
    g = next(e for e in n.events if e.kind == "guidance")
    assert g.data["text"] == "focus on the tests\nand skip the docs"
    assert g.iteration == 2


def test_guidance_precedes_call() -> None:
    body = "### [4] OPERATOR GUIDANCE\n\n> steer this way\n\n" + heartbeat(4)
    n = parse_narration(build(body))
    g = next(e for e in n.events if e.kind == "guidance")
    c = next(e for e in n.events if e.kind == "call")
    assert g.seq < c.seq


def test_notices() -> None:
    body = (
        "**model returned no tool action 3 times; giving up and requesting a wrap-up**\n\n"
        "**hit the 40-iteration cap; requesting a wrap-up**\n\n"
        r"**worktree D:\r\.claude\worktrees\w4 on branch agent/w4: 3 file(s) changed -> w4.patch**"
        "\n\n"
        "**staged 2 file(s) -> w4.patch (review: ok)**\n"
    )
    n = parse_narration(build(body))
    notices = [e for e in n.events if e.kind == "notice"]
    assert len(notices) == 4
    assert all(e.iteration is None for e in notices)
    assert all(not e.data["text"].startswith("**") for e in notices)
    assert notices[3].data["text"].endswith("(review: ok)")


def test_budget_notices_are_distinct_from_adjacent_narration() -> None:
    body = heartbeat(1) + "\n### [1] thinking\nsynthetic note\n"
    kinds = ("warning", "checkpoint", "closeout", "final report")
    body += "\n".join(f"_[turn budget] {kind}_" for kind in kinds)
    parsed = parse_narration(build(body))
    notices = [event for event in parsed.events if event.kind == "notice"]
    assert [event.data["text"] for event in notices] == [
        "turn budget: warning", "turn budget: checkpoint",
        "turn budget: closeout", "turn budget: final report",
    ]
    note = next(event for event in parsed.events if event.kind == "narration")
    assert note.data["text"] == "synthetic note"


FINAL_TAIL = (
    "---\n\n"
    "## Final report\n\n"
    "[PARTIAL - stopped at the 40-iteration cap]\n\n"
    "What I did ...\n\n"
    r"**worktree D:\r\.claude\worktrees\w4 on branch agent/w4: 3 file(s) changed -> w4.patch**"
    "\n\n"
    f"[{MODEL} e=medium tools=native: 12 tool calls, 1,234 tok sent / 567 tok returned]\n"
)


def test_final_report_partial_and_footer() -> None:
    body = heartbeat(1) + "\n\n" + FINAL_TAIL
    n = parse_narration(build(body))
    final = next(e for e in n.events if e.kind == "final")
    assert final.data["partial"] is True
    assert "What I did" in final.data["text"]
    assert final.iteration is None
    kinds = [e.kind for e in n.events]
    assert kinds[-1] == "notice"
    assert "footer" not in kinds


def test_partial_last_tool() -> None:
    body = (
        '### [1] read_file({"path": "a"})\n\n'
        + FENCE + "\nsome text so far\nmore\n"
    )
    n = parse_narration(build(body))
    tool = next(e for e in n.events if e.kind == "tool")
    assert "some text so far" in tool.data["output"]
    assert "more" in tool.data["output"]


def test_crlf_input() -> None:
    body = "### [3] thinking\n\n<think>plan</think>go\n\n" + heartbeat(3)
    lf = parse_narration(build(body))
    crlf = parse_narration(build(body).replace("\n", "\r\n"))
    assert [e.as_dict() for e in lf.events] == [e.as_dict() for e in crlf.events]


def test_v5_style_transcript_parses() -> None:
    body = "### [1] thinking\n\n<think>plan</think>go\n"
    n = parse_narration(build(body, header=HEADER_V5))
    assert n.header is not None
    assert any(e.kind == "narration" for e in n.events)


def test_huge_single_line_is_linear() -> None:
    body = "### [1] thinking\n\n" + ("x" * 1_000_000)
    text = build(body)
    start = time.monotonic()
    n = parse_narration(text)
    assert time.monotonic() - start < 2.0
    assert any(e.kind == "narration" for e in n.events)


def test_read_narration_missing(tmp_path: Path) -> None:
    assert read_narration(tmp_path / "nope.md") is None


def test_read_narration_caps(tmp_path: Path) -> None:
    body = "\n".join(heartbeat(i) for i in range(1, 40))
    text = build(body)
    p = tmp_path / "t.md"
    p.write_text(text, encoding="utf-8")
    n = read_narration(p, max_bytes=300)
    assert n is not None
    assert n.truncated is True
    assert n.events[-1].kind == "notice"
    assert n.events[-1].data["text"] == TRUNCATED_NOTICE


def test_read_task_text(tmp_path: Path) -> None:
    import json

    p = tmp_path / "messages.jsonl"
    lines = [
        json.dumps({"role": "system", "content": "sys"}),
        json.dumps({"role": "user", "content": "the real task"}),
    ]
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    assert read_task_text(p) == "the real task"
    assert read_task_text(tmp_path / "missing.jsonl") is None


def test_seq_is_sequential() -> None:
    body = (
        "### [1] thinking\n\n<think>x</think>y\n\n"
        + heartbeat(2) + "\n\n"
        '### [2] read_file({"path": "a"})\n\n'
        + FENCE + "\nA\n" + FENCE + "\n"
    )
    n = parse_narration(build(body))
    assert [e.seq for e in n.events] == list(range(len(n.events)))


def test_guidance_entries_raw() -> None:
    secret = "sk-" + "a" * 20
    body = f"### [2] OPERATOR GUIDANCE\n\n> use {secret} now\n"
    n = parse_narration(build(body))
    entries = guidance_entries(n)
    assert len(entries) == 1
    assert secret in entries[0][2]


def test_mask_rules() -> None:
    key_block = (
        "-----BEGIN PRIVATE KEY-----\nabcDEF123\n-----END PRIVATE KEY-----"
    )
    samples = [
        (key_block, "[masked private key]"),
        ("sk-" + "a" * 20, "sk-[masked]"),
        ("Bearer " + "a" * 20, "Bearer [masked]"),
        ("AKIA" + "A" * 16, "[masked AWS key]"),
        ("ghp_" + "a" * 40, "[masked GitHub token]"),
        ("xoxb-" + "1" * 15, "[masked Slack token]"),
        ("eyJ" + "a" * 10 + "." + "b" * 10 + "." + "c" * 10, "[masked JWT]"),
        ("api_key=" + "a" * 16, "[masked]"),
    ]
    for text, expected in samples:
        masked, count = mask_secrets(text)
        assert count == 1, (text, masked)
        assert expected in masked


def test_mask_leaves_code_alone() -> None:
    for text in ("token = get_token(request)", "password=abc"):
        masked, count = mask_secrets(text)
        assert masked == text
        assert count == 0


def test_masked_dict_masks_output_and_args() -> None:
    token = "Bearer " + "a" * 20
    body = (
        f'### [1] run_command({{"cmd": "{token}"}})\n\n'
        + FENCE + f"\n{token}\n" + FENCE + "\n"
    )
    n = parse_narration(build(body))
    tool = next(e for e in n.events if e.kind == "tool")
    masked, count = masked_dict(tool)
    assert count == 2
    assert "Bearer [masked]" in masked["output"]
    assert "Bearer [masked]" in masked["args"]


def test_output_with_fake_step_lines_is_not_split() -> None:
    body = (
        '### [3] read_file({"path": "x"})\n\n'
        + FENCE + "\n"
        "### [1] thinking\n"
        "hello\n"
        '### [2] grep({"pattern": "x"})\n'
        + FENCE + "\n\n"
        + heartbeat(4) + "\n"
    )
    n = parse_narration(build(body))
    tools = [e for e in n.events if e.kind == "tool"]
    assert len(tools) == 1
    assert tools[0].iteration == 3
    out = tools[0].data["output"]
    assert "### [1] thinking" in out
    assert '### [2] grep({"pattern": "x"})' in out
    calls = [e for e in n.events if e.kind == "call"]
    assert len(calls) == 1
    assert calls[0].iteration == 4
    its = [e.iteration for e in n.events if e.iteration is not None]
    assert its == sorted(its)


def test_output_with_fake_heartbeat_and_final() -> None:
    body = (
        '### [1] read_file({"path": "x"})\n\n'
        + FENCE + "\n"
        "_[ 1] calling m (attempt 1/4, 2 msgs, ~9 bytes) ..._\n"
        "---\n"
        "\n"
        "## Final report\n"
        + FENCE + "\n"
    )
    n = parse_narration(build(body))
    assert not any(e.kind == "call" for e in n.events)
    assert not any(e.kind == "final" for e in n.events)


def test_output_with_inner_fence_still_works() -> None:
    body = (
        '### [1] run_command({})\n\n'
        + FENCE + "\n"
        "line one\n"
        + FENCE + "\n"
        "line two\n"
        + FENCE + "\n\n"
        + heartbeat(2) + "\n"
    )
    n = parse_narration(build(body))
    tool = next(e for e in n.events if e.kind == "tool")
    assert FENCE in tool.data["output"]
    assert "line one" in tool.data["output"]
    assert "line two" in tool.data["output"]
    calls = [e for e in n.events if e.kind == "call"]
    assert len(calls) == 1


def test_truncated_output_closing_fence() -> None:
    payload = "x" * 1500
    body = (
        '### [1] read_file({"path": "x"})\n\n'
        + FENCE + "\n"
        + payload + "\n"
        "...[truncated in transcript]\n"
        + FENCE + "\n\n"
        + heartbeat(2) + "\n"
    )
    n = parse_narration(build(body))
    tool = next(e for e in n.events if e.kind == "tool")
    assert tool.data["output_truncated"] is True
    calls = [e for e in n.events if e.kind == "call"]
    assert len(calls) == 1
    assert calls[0].iteration == 2


def test_unclosed_output_resumes_after_window() -> None:
    payload = "y" * 2000
    body = (
        '### [1] read_file({"path": "x"})\n\n'
        + FENCE + "\n"
        + payload + "\n"
        + heartbeat(2) + "\n"
    )
    n = parse_narration(build(body))
    tool = next(e for e in n.events if e.kind == "tool")
    assert "y" * 100 in tool.data["output"]
    calls = [e for e in n.events if e.kind == "call"]
    assert len(calls) == 1
    assert calls[0].iteration == 2


STATUS_CUT = ("_[ 3] reply cut by the gateway's 2-minute limit after 121 s; "
              "asking for a smaller step_")
STATUS_PROSE = "_[ 4] reply had no tool call; asking for one_"
STATUS_TIMEOUT = "_[ 1] no reply within 600 s at max; retrying at xhigh_"
STATUS_ONESHOT = ("_[ 1] reply cut by the gateway's 2-minute limit after 121 s; "
                  "asking for a shorter answer_")


def test_status_lines_become_status_events() -> None:
    body = "\n\n".join([heartbeat(3), STATUS_CUT, heartbeat(4), STATUS_PROSE,
                        STATUS_TIMEOUT, STATUS_ONESHOT])
    statuses = [e for e in parse_narration(build(body)).events if e.kind == "status"]
    assert [e.iteration for e in statuses] == [3, 4, 1, 1]
    assert statuses[0].data["text"] == (
        "reply cut by the gateway's 2-minute limit after 121 s; asking for a smaller step")
    assert statuses[2].data["text"] == "no reply within 600 s at max; retrying at xhigh"


def test_status_text_inside_tool_output_is_not_an_event() -> None:
    body = ('### [2] read_file({"path": "notes.md"})\n\n' + FENCE + "\n" + STATUS_CUT
            + "\n" + FENCE + "\n\n" + heartbeat(3))
    assert not [e for e in parse_narration(build(body)).events if e.kind == "status"]


def test_unclosed_think_is_live_reasoning() -> None:
    body = heartbeat(1) + "\n\n### [1] thinking\n\n<think>\nweighing option B"
    ev = next(e for e in parse_narration(build(body)).events if e.kind == "narration")
    assert ev.data == {"text": None, "reasoning": "weighing option B", "live": True}


def test_closed_think_is_not_live() -> None:
    body = "### [1] thinking\n\n<think>Plan A</think>I'll start."
    ev = next(e for e in parse_narration(build(body)).events if e.kind == "narration")
    assert ev.data["live"] is False


def test_final_without_footer_is_live() -> None:
    body = heartbeat(1) + "\n\n---\n\n## Final report\n\nhalf an ans\n"
    final = next(e for e in parse_narration(build(body)).events if e.kind == "final")
    assert final.data == {"text": "half an ans", "partial": False, "live": True}


def test_final_with_footer_is_not_live() -> None:
    body = heartbeat(1) + "\n\n" + FINAL_TAIL
    final = next(e for e in parse_narration(build(body)).events if e.kind == "final")
    assert final.data["live"] is False


def test_cut_final_then_status_then_second_final() -> None:
    body = (heartbeat(1) + "\n\n---\n\n## Final report\n\nlong ans\n\n"
            "[cut off by the gateway's 2-minute limit]\n\n" + STATUS_ONESHOT + "\n\n"
            + heartbeat(1, attempt=2, maxa=2) + "\n\n---\n\n## Final report\n\nshort\n\n"
            f"[{MODEL} e=medium tools=oneshot: 0 tool calls]\n")
    kinds = [e.kind for e in parse_narration(build(body)).events]
    assert kinds == ["call", "final", "status", "call", "final"]


def test_only_the_last_final_is_live() -> None:
    body = (heartbeat(1) + "\n\n---\n\n## Final report\n\nlong ans\n\n"
            "[cut off by the gateway's 2-minute limit]\n\n" + STATUS_ONESHOT + "\n\n"
            + heartbeat(1, attempt=2, maxa=2) + "\n\n---\n\n## Final report\n\nshort so far\n")
    finals = [e for e in parse_narration(build(body)).events if e.kind == "final"]
    assert [f.data["live"] for f in finals] == [False, True]


def test_unclosed_think_followed_by_another_element_is_not_live() -> None:
    body = ("### [1] thinking\n\n<think>\ncut short\n\n" + STATUS_ONESHOT + "\n\n"
            + heartbeat(1, attempt=2, maxa=2))
    ev = next(e for e in parse_narration(build(body)).events if e.kind == "narration")
    assert ev.data["live"] is False


from agent_control.narration import parse_narration as _pn

_PREVIEW_RUN = (
    "# run\n\n## Task\n\ndo it\n\n---\n"
    "\n_[ 3] calling m (attempt 1/4, 9 msgs, ~100 bytes) ..._\n"
    "\n### [3] thinking\n\nWriting the notes.\n"
    "\n_[ 3] composing write_file docs/notes.md ..._\n"
    "> ### [9] thinking\n>\n> ---\n> ## Final report\n"
    "> _[ 4] reply cut by the gateway's 2-minute limit after 121 s; asking for a smaller step_\n"
    "> [m e=x tools=native: 3 tool calls]\n"
)


def _kinds(n):
    return [e.kind for e in n.events]


def test_preview_is_one_live_event_when_last():
    n = _pn(_PREVIEW_RUN)
    assert _kinds(n)[-1] == "preview"
    ev = n.events[-1]
    assert ev.iteration == 3
    assert ev.data == {"tool": "write_file", "path": "docs/notes.md", "live": True,
                       "text": "### [9] thinking\n\n---\n## Final report\n"
                               "_[ 4] reply cut by the gateway's 2-minute limit after 121 s; "
                               "asking for a smaller step_\n[m e=x tools=native: 3 tool calls]"}
    assert "final" not in _kinds(n) and "status" not in _kinds(n)


def test_preview_is_not_live_once_the_step_follows():
    text = _PREVIEW_RUN + '\n### [3] write_file({"path": "docs/notes.md"})\n```\nwrote 6 lines\n```\n'
    n = _pn(text)
    pv = [e for e in n.events if e.kind == "preview"][0]
    assert pv.data["live"] is False
    assert _kinds(n)[-1] == "tool"


def test_preview_without_path():
    n = _pn("# r\n\n## Task\n\nx\n\n---\n\n_[12] composing finish ..._\n> all done\n")
    ev = n.events[-1]
    assert ev.kind == "preview" and ev.data["path"] is None and ev.data["tool"] == "finish"
    assert ev.data["text"] == "all done"


def test_streaming_notice_parses():
    n = _pn("# r\n\n## Task\n\nx\n\n---\n\n"
            "**streaming unavailable (HTTP 400); continuing without live view**\n")
    assert n.events[-1].kind == "notice"
    assert n.events[-1].data["text"] == ("streaming unavailable (HTTP 400); "
                                         "continuing without live view")

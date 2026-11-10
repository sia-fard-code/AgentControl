"""On-demand narration view of a delegated-run transcript.

Unlike ``activity.py`` (which stays metadata-only), this module DOES return
transcript content: assistant reasoning, tool outputs, guidance and final
reports. It is served only by ``GET /api/narration``, read on demand, and must
never be written to disk, logs or AgentControl state. It is deliberately kept
separate from ``activity.py`` for that reason.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from .activity import _STEP

MAX_NARRATION_BYTES = 8_000_000
TRUNCATED_NOTICE = "transcript larger than 8 MB; later steps are not shown"


@dataclass(frozen=True)
class NarrationHeader:
    label: str | None
    model: str | None
    effort: str | None
    tool_mode: str | None
    writes: str | None
    root: str | None
    worktree: bool

    def as_dict(self) -> dict[str, object]:
        return {
            "label": self.label,
            "model": self.model,
            "effort": self.effort,
            "tool_mode": self.tool_mode,
            "writes": self.writes,
            "root": self.root,
            "worktree": self.worktree,
        }


@dataclass(frozen=True)
class NarrationEvent:
    seq: int
    kind: str
    iteration: int | None
    data: dict[str, object]

    def as_dict(self) -> dict[str, object]:
        return {
            "seq": self.seq,
            "kind": self.kind,
            "iteration": self.iteration,
            **self.data,
        }


@dataclass(frozen=True)
class Narration:
    header: NarrationHeader | None
    brief: str | None
    events: tuple[NarrationEvent, ...]
    truncated: bool = False


_BACKTICK = re.compile(r"`([^`]*)`")
_HEARTBEAT = re.compile(
    r"^_\[\s*(\d+)\]\s+calling\s+\S+\s+\(attempt\s+(\d+)/(\d+)"
)
# Engine status lines (reply-limits design, 2026-10-02), one per line in the
# heartbeat's italic style.
_STATUS = re.compile(
    r"^_\[\s*(\d+)\]\s+("
    r"reply cut by the gateway's 2-minute limit after \d+ s; "
    r"asking for a (?:smaller step|shorter answer)"
    r"|reply had no tool call; asking for one"
    r"|no reply within \d+ s at \w+; retrying at \w+"
    r")_$"
)
_BUDGET_NOTICE = re.compile(r"^_\[turn budget\] (warning|checkpoint|closeout|final report)_$")
# Live agent turns (2026-10-03): a tool's text previewed while it streams. The
# engine prefixes every preview line with "> " (or writes a bare ">"), so no
# line inside can start an element.
_COMPOSING = re.compile(
    r"^_\[\s*(\d+)\]\s+composing\s+([A-Za-z_][\w-]*)(?:\s+(.+?))?\s+\.\.\._$"
)
# A <think> with no closing tag yet: a streamed one-shot is still writing it.
_OPEN_THINK = re.compile(r"<think>(?![\s\S]*</think>)([\s\S]*)\Z")
_WORKTREE_MARK = (".claude\\worktrees\\", ".claude/worktrees/")

_NOTICE_PATTERNS = (
    re.compile(
        r"^\*\*model returned no tool action \d+ times; "
        r"giving up and requesting a wrap-up\*\*$"
    ),
    re.compile(r"^\*\*hit the \d+-iteration cap; requesting a wrap-up\*\*$"),
    re.compile(r"^\*\*worktree .+: \d+ file\(s\) changed -> .+\*\*$"),
    re.compile(r"^\*\*staged \d+ file\(s\) -> .+\*\*( \(review: .+\))?$"),
    re.compile(
        r"^\*\*streaming unavailable \(HTTP \d{3}\); "
        r"continuing without live view\*\*$"
    ),
)
_FOOTER = re.compile(r"^\[[^\]]*: [\d,]+ tool calls[^\]]*\]$")


def _normalise(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _first_backtick(line: str) -> str | None:
    m = _BACKTICK.search(line)
    return m.group(1) if m else None


def _parse_header(lines: list[str]) -> NarrationHeader | None:
    if not lines or not lines[0].startswith("# "):
        return None
    label = lines[0][2:].strip() or None
    model = effort = tool_mode = writes = root = None
    for line in lines[1:]:
        if line.startswith("- model:"):
            ticks = _BACKTICK.findall(line)
            if ticks:
                model = ticks[0]
            m = re.search(r"effort:\s*`([^`]*)`", line)
            if m:
                effort = m.group(1)
            m = re.search(r"tools:\s*`([^`]*)`", line)
            if m:
                tool_mode = m.group(1)
        elif line.startswith("- writes:"):
            m = re.search(r"writes:\s*`([^`]*)`", line)
            if m:
                raw = m.group(1)
                writes = "staged" if raw == "staged (patch only)" else raw
            m = re.search(r"root:\s*`([^`]*)`", line)
            if m:
                root = m.group(1)
    worktree = bool(root) and any(mark in root for mark in _WORKTREE_MARK)
    return NarrationHeader(label, model, effort, tool_mode, writes, root, worktree)


_STEP1 = re.compile(r"^###\s+\[1\]\s")


def _is_turn1_element(line: str) -> bool:
    m = _HEARTBEAT.match(line)
    if m and int(m.group(1)) == 1:
        return True
    return bool(_STEP1.match(line))


def _final_here(lines: list[str], idx: int) -> bool:
    """True when lines[idx] is a ``---`` whose next non-blank is ``## Final report``."""
    if lines[idx].strip() != "---":
        return False
    for j in range(idx + 1, len(lines)):
        if lines[j].strip() == "":
            continue
        return lines[j].strip() == "## Final report"
    return False


def _is_any_element_start(lines: list[str], idx: int) -> bool:
    line = lines[idx]
    if _HEARTBEAT.match(line):
        return True
    if _COMPOSING.match(line):
        return True
    if _STATUS.match(line) or _BUDGET_NOTICE.match(line):
        return True
    if line.startswith("### ") and _STEP.match(line):
        return True
    if _GUIDANCE.match(line):
        return True
    if any(p.match(line) for p in _NOTICE_PATTERNS):
        return True
    if _FOOTER.match(line):
        return True
    if _final_here(lines, idx):
        return True
    return False


def _strip_blank_edges(text: str) -> str:
    return text.strip("\n").strip()


def _find_task_line(lines: list[str]) -> int | None:
    for i, line in enumerate(lines):
        if line.strip() == "## Task":
            return i
    return None


def _resolve_brief_body(
    text: str, lines: list[str], task_text: str | None
) -> tuple[str | None, int]:
    """Return (brief, body_start_index). body_start_index == len(lines) means none."""
    n = len(lines)
    task_idx = _find_task_line(lines)

    # 1. Exact match using the real task text.
    if task_text is not None:
        needle = "## Task\n\n" + task_text + "\n\n---\n"
        pos = text.find(needle)
        if pos != -1:
            before = text[: pos + len(needle)]
            body_start = before.count("\n")
            return task_text.strip(), body_start

    # 2. Structural: first turn-1 element right after "---" + one blank line.
    for i in range(2, n):
        if _is_turn1_element(lines[i]) and lines[i - 1] == "" and lines[i - 2].strip() == "---":
            brief = None
            if task_idx is not None:
                brief = _strip_blank_edges("\n".join(lines[task_idx + 1 : i - 2]))
            return brief, i

    # 3. Last resort: first body element of any kind.
    for i in range(n):
        if _is_any_element_start(lines, i):
            brief = None
            if task_idx is not None:
                last_rule = None
                for j in range(i - 1, task_idx, -1):
                    if lines[j].strip() == "---":
                        last_rule = j
                        break
                end = last_rule if last_rule is not None else i
                brief = _strip_blank_edges("\n".join(lines[task_idx + 1 : end]))
            return brief, i

    # No body at all.
    brief = None
    if task_idx is not None:
        last_rule = None
        for j in range(n - 1, task_idx, -1):
            if lines[j].strip() == "---":
                last_rule = j
                break
        end = last_rule if last_rule is not None else n
        brief = _strip_blank_edges("\n".join(lines[task_idx + 1 : end]))
    return brief, n


_GUIDANCE = re.compile(r"^###\s+\[(\d+)\]\s+OPERATOR GUIDANCE\s*$")
_THINK = re.compile(r"<think>(.*?)</think>", re.DOTALL)
_TRUNC_LINE = "...[truncated in transcript]"


def _extract_output(body_lines: list[str]) -> tuple[str | None, bool]:
    """Return (output, output_truncated) from an element's body lines."""
    fence_idxs = [i for i, ln in enumerate(body_lines) if ln == "```"]
    if not fence_idxs:
        return None, False
    open_i = fence_idxs[0]
    if len(fence_idxs) >= 2:
        close_i = fence_idxs[-1]
        inner = body_lines[open_i + 1 : close_i]
    else:
        inner = body_lines[open_i + 1 :]
    truncated = False
    if inner and inner[-1] == _TRUNC_LINE:
        inner = inner[:-1]
        truncated = True
    return "\n".join(inner), truncated


def _parse_narration_body(body: str) -> tuple[str | None, str | None, bool]:
    """Return (text, reasoning, live) for a thinking element body.

    live is True when the body ends inside an unclosed <think>: a streamed
    one-shot is still writing its reasoning.
    """
    reasons = [m.group(1).strip() for m in _THINK.finditer(body)]
    rest = _THINK.sub("", body)
    live = False
    m = _OPEN_THINK.search(rest)
    if m:
        live = True
        reasons.append(m.group(1).strip())
        rest = rest[: m.start()]
    reasons = [r for r in reasons if r]
    reasoning = "\n\n".join(reasons) if reasons else None
    rest = rest.strip()
    return (rest if rest else None), reasoning, live


def _classify_start(lines: list[str], i: int) -> str | None:
    """Return an element-kind tag when lines[i] starts an element, else None."""
    line = lines[i]
    if _HEARTBEAT.match(line):
        return "call"
    if _COMPOSING.match(line):
        return "preview"
    if _STATUS.match(line):
        return "status"
    if _BUDGET_NOTICE.match(line):
        return "budget_notice"
    if _GUIDANCE.match(line):
        return "guidance"
    if line.startswith("### ") and _STEP.match(line):
        return "step"
    if any(p.match(line) for p in _NOTICE_PATTERNS):
        return "notice"
    if _FOOTER.match(line):
        return "footer"
    if _final_here(lines, i):
        return "final"
    return None


_OUTPUT_WINDOW = 1501 + len(_TRUNC_LINE) + 1


def _skip_output_block(lines: list[str], header_idx: int) -> int | None:
    """Return the resume index past a step's fenced output block, or None.

    When the header at ``header_idx`` is followed by an opening fence, scan the
    tool result so its interior lines are never re-classified as elements. The
    returned index is where normal classification resumes; None means there is
    no opening fence (a header without output) and the caller advances by one.
    """
    n = len(lines)
    o = header_idx + 1
    while o < n and lines[o].strip() == "":
        o += 1
    if o >= n or lines[o] != "```":
        return None
    c = 0
    k = o + 1
    while k < n:
        line = lines[k]
        if line == "```":
            prev = lines[k - 1] if k - 1 > o else ""
            if prev == _TRUNC_LINE:
                return k + 1
            if c <= 1501:
                j = k + 1
                while j < n and lines[j].strip() == "":
                    j += 1
                if j >= n or _classify_start(lines, j) is not None:
                    return k + 1
        c += len(line) + 1
        if c > _OUTPUT_WINDOW:
            return k + 1
        k += 1
    return None


def _collect_elements(lines: list[str], start: int) -> list[tuple[int, str]]:
    elements: list[tuple[int, str]] = []
    i = start
    n = len(lines)
    while i < n:
        tag = _classify_start(lines, i)
        if tag is not None:
            elements.append((i, tag))
            if tag == "step":
                m = _STEP.match(lines[i])
                if m is not None and m.group(2) != "thinking":
                    resume = _skip_output_block(lines, i)
                    if resume is not None:
                        i = resume
                        continue
        i += 1
    return elements


def _make_event(seq, kind, iteration, data) -> NarrationEvent:
    return NarrationEvent(seq=seq, kind=kind, iteration=iteration, data=data)


def _build_events(lines: list[str], body_start: int) -> list[NarrationEvent]:
    elements = _collect_elements(lines, body_start)
    starts = [idx for idx, _ in elements]
    events: list[NarrationEvent] = []
    seq = 0
    n = len(lines)
    for pos, (idx, tag) in enumerate(elements):
        next_idx = starts[pos + 1] if pos + 1 < len(elements) else n
        body_lines = lines[idx + 1 : next_idx]
        line = lines[idx]
        if tag == "call":
            m = _HEARTBEAT.match(line)
            it = int(m.group(1))
            events.append(_make_event(seq, "call", it, {
                "attempt": int(m.group(2)),
                "max_attempts": int(m.group(3)),
            }))
            seq += 1
        elif tag == "preview":
            m = _COMPOSING.match(line)
            text_lines: list[str] = []
            for ln in body_lines:
                if ln == ">":
                    text_lines.append("")
                elif ln.startswith("> "):
                    text_lines.append(ln[2:])
                else:
                    break
            events.append(_make_event(seq, "preview", int(m.group(1)), {
                "tool": m.group(2),
                "path": m.group(3),
                "text": "\n".join(text_lines),
                "live": pos == len(elements) - 1,
            }))
            seq += 1
        elif tag == "status":
            m = _STATUS.match(line)
            events.append(_make_event(seq, "status", int(m.group(1)), {
                "text": m.group(2),
            }))
            seq += 1
        elif tag == "guidance":
            m = _GUIDANCE.match(line)
            it = int(m.group(1))
            text = _guidance_text(body_lines)
            events.append(_make_event(seq, "guidance", it, {"text": text}))
            seq += 1
        elif tag == "budget_notice":
            text = "turn budget: " + _BUDGET_NOTICE.match(line).group(1)
            events.append(_make_event(seq, "notice", None, {"text": text}))
            seq += 1
        elif tag == "notice":
            text = line.strip()[2:-2]
            events.append(_make_event(seq, "notice", None, {"text": text}))
            seq += 1
        elif tag == "footer":
            continue
        elif tag == "final":
            live = pos == len(elements) - 1
            ev = _final_event(seq, lines, idx, next_idx, live)
            events.append(ev)
            seq += 1
        elif tag == "step":
            ev = _step_event(seq, line, body_lines)
            if ev is not None and ev.kind == "narration" and ev.data.get("live") \
                    and pos != len(elements) - 1:
                ev = _make_event(ev.seq, ev.kind, ev.iteration, {**ev.data, "live": False})
            if ev is not None:
                events.append(ev)
                seq += 1
    return events


def _guidance_text(body_lines: list[str]) -> str:
    out: list[str] = []
    first_seen = False
    for ln in body_lines:
        if not first_seen:
            if ln.strip() == "":
                continue
            first_seen = True
            if ln.startswith("> "):
                out.append(ln[2:])
            else:
                out.append(ln)
        else:
            out.append(ln)
    return "\n".join(out).strip()


def _final_event(
    seq: int, lines: list[str], idx: int, next_idx: int, live: bool
) -> NarrationEvent:
    # idx is the "---" line; skip it and the "## Final report" header.
    j = idx + 1
    n = len(lines)
    while j < next_idx and lines[j].strip() == "":
        j += 1
    if j < next_idx and lines[j].strip() == "## Final report":
        j += 1
    text = _strip_blank_edges("\n".join(lines[j:next_idx]))
    partial = text.startswith("[PARTIAL")
    return _make_event(seq, "final", None, {
        "text": text, "partial": partial, "live": live,
    })


def _step_event(
    seq: int, line: str, body_lines: list[str]
) -> NarrationEvent | None:
    m = _STEP.match(line)
    name = m.group(2)
    it = int(m.group(1))
    raw_args = m.group(3) or ""
    if name == "thinking":
        body = "\n".join(body_lines)
        text, reasoning, live = _parse_narration_body(body)
        if text is None and reasoning is None:
            return None
        return _make_event(seq, "narration", it, {
            "text": text,
            "reasoning": reasoning,
            "live": live,
        })
    if name == "finish":
        return _make_event(seq, "finish", it, {})
    output, truncated = _extract_output(body_lines)
    if name in ("NO_TOOL_ACTION", "PARSE_ERROR"):
        return _make_event(seq, "anomaly", it, {"name": name, "output": output})
    return _make_event(seq, "tool", it, {
        "name": name,
        "args": raw_args,
        "output": output,
        "output_truncated": truncated,
    })


def parse_narration(text: str, *, task_text: str | None = None) -> Narration:
    text = _normalise(text)
    if task_text is not None:
        task_text = _normalise(task_text)
    lines = text.split("\n")
    header = _parse_header(lines)
    brief, body_start = _resolve_brief_body(text, lines, task_text)
    events = _build_events(lines, body_start)
    return Narration(
        header=header,
        brief=brief,
        events=tuple(events),
        truncated=False,
    )


def read_narration(
    path,
    *,
    max_bytes: int = MAX_NARRATION_BYTES,
    task_text: str | None = None,
) -> Narration | None:
    p = Path(path)
    try:
        raw = p.read_bytes()
    except OSError:
        return None
    truncated = False
    if len(raw) > max_bytes:
        raw = raw[:max_bytes]
        cut = raw.rfind(b"\n")
        if cut != -1:
            raw = raw[:cut]
        truncated = True
    text = raw.decode("utf-8", errors="replace")
    narration = parse_narration(text, task_text=task_text)
    if not truncated:
        return narration
    events = list(narration.events)
    notice = NarrationEvent(
        seq=len(events),
        kind="notice",
        iteration=None,
        data={"text": TRUNCATED_NOTICE},
    )
    events.append(notice)
    return Narration(
        header=narration.header,
        brief=narration.brief,
        events=tuple(events),
        truncated=True,
    )


def read_task_text(messages_path, *, max_bytes: int = 2_000_000) -> str | None:
    p = Path(messages_path)
    try:
        read = 0
        with p.open("r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                read += len(line)
                if read > max_bytes:
                    break
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(obj, dict):
                    continue
                if obj.get("role") == "user":
                    content = obj.get("content")
                    if isinstance(content, str):
                        return content
                    return None
    except OSError:
        return None
    return None


_PRIVATE_KEY = re.compile(
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?"
    r"(?:-----END [A-Z ]*PRIVATE KEY-----|\Z)",
    re.DOTALL,
)
_MASK_SIMPLE = (
    (re.compile(r"\bsk-[A-Za-z0-9_\-]{16,}"), "sk-[masked]"),
    (re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=\-]{16,}"), "Bearer [masked]"),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "[masked AWS key]"),
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}"), "[masked GitHub token]"),
    (re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}"), "[masked Slack token]"),
    (
        re.compile(
            r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"
        ),
        "[masked JWT]",
    ),
)
_KEYVALUE = re.compile(
    r"(?i)\b(api[_-]?key|secret|token|password|passwd|pwd|access[_-]?key)\b"
    r"(\s*[:=]\s*)([\"']?)([A-Za-z0-9_\-+/=.]{12,})\3"
)


def mask_secrets(text: str) -> tuple[str, int]:
    count = 0

    def _pk(_m: re.Match) -> str:
        nonlocal count
        count += 1
        return "[masked private key]"

    text = _PRIVATE_KEY.sub(_pk, text)

    for pattern, replacement in _MASK_SIMPLE:
        text, n = pattern.subn(replacement, text)
        count += n

    def _kv(m: re.Match) -> str:
        nonlocal count
        count += 1
        return m.group(1) + m.group(2) + m.group(3) + "[masked]" + m.group(3)

    text = _KEYVALUE.sub(_kv, text)
    return text, count


def masked_dict(event: NarrationEvent) -> tuple[dict[str, object], int]:
    total = 0
    result: dict[str, object] = {}
    for key, value in event.as_dict().items():
        if key != "kind" and isinstance(value, str):
            masked, n = mask_secrets(value)
            total += n
            result[key] = masked
        else:
            result[key] = value
    return result, total


def guidance_entries(narration: Narration) -> list[tuple[int, int | None, str]]:
    return [
        (e.seq, e.iteration, e.data["text"])
        for e in narration.events
        if e.kind == "guidance"
    ]

"""Sanitized live-progress reader for a delegated-run transcript.

The engine flushes ``### [N] tool(...)`` headers before dispatch and writes each
result after the tool returns. Model-call lines precede every attempt. This
module reads a bounded TAIL of the transcript and reports the current phase,
retry attempt and last tool, so both running tools and stuck retries are visible.

Security invariant (AGENTS.md): as_dict() carries only the eight structural
fields below. Never narration, arguments other than a sanitized path, command
strings, model names or tool output.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from .activity import _CONTROL, _PATH_TOOLS, _STEP, _safe_path

Phase = Literal["starting", "model_call", "tools", "wrapping_up", "finished"]

# Model-call line, written BEFORE every attempt. Anchored at line start so that
# narration merely mentioning "calling x (attempt 3/4" is ignored.
_MODEL_CALL = re.compile(r"^_\[\s*(\d+)\]\s+calling\s+\S+\s+\(attempt\s+(\d+)/(\d+)")

# Run is finished: the engine's footer line, its last write. It matches agent
# runs ("[m e=medium tools=native: 12 tool calls, 114,120 tok sent / 7,081 tok
# returned]") and one-shots with unknown usage ("[m e=high tools=oneshot: 0 tool
# calls]"). "## Final report" alone no longer means finished: a streamed one-shot
# writes it before its answer is complete (live-thinking design).
_FOOTER = re.compile(r"^\[[^\]]*: [\d,]+ tool calls[^\]]*\]$")

# The engine's cut status line (reply-limits design): the gateway discarded a
# reply at its 2-minute limit and the engine asked for a smaller step.
_CUT = re.compile(r"^_\[\s*(\d+)\]\s+reply cut by the gateway's 2-minute limit after \d+ s;")

# Wrap-up markers. The iteration-cap marker embeds the run's actual cap, which
# is not always 40, so it is matched by regex. The no-tool-action marker is
# hard-coded by the engine, so it stays an exact whole-line match.
_WRAP_CAP = re.compile(r"^\*\*hit the \d+-iteration cap; requesting a wrap-up\*\*$")
_BUDGET_CLOSEOUT = re.compile(r"^_\[turn budget\] (?:closeout|final report)_$")
_WRAP_MARKERS = (
    "**model returned no tool action 3 times; giving up and requesting a wrap-up**",
)


@dataclass(frozen=True)
class RunProgress:
    iteration: int | None
    phase: Phase
    attempt: int | None
    max_attempts: int | None
    nudges: int
    last_tool: str | None
    last_path: str | None
    cuts: int = 0

    def as_dict(self) -> dict[str, object]:
        return {
            "iteration": self.iteration,
            "phase": self.phase,
            "attempt": self.attempt,
            "max_attempts": self.max_attempts,
            "nudges": self.nudges,
            "last_tool": self.last_tool,
            "last_path": self.last_path,
            "cuts": self.cuts,
        }


def parse_progress(text: str) -> RunProgress:
    """Return the sanitized progress snapshot for a transcript string."""
    iteration: int | None = None
    attempt: int | None = None
    max_attempts: int | None = None
    nudges = 0
    cuts = 0
    last_tool: str | None = None
    last_path: str | None = None

    finished = False
    wrapping = False
    # Phase decided by the last model-call or step line, in file order.
    last_kind: str | None = None  # "model_call" or "tools"

    # Skip the task brief: lines from an exact "## Task" line through the first
    # following exact "---" line. A brief that quotes a footer, model-call, step
    # or "## Final report" line would otherwise flip the phase. When the tail
    # does not include the "## Task" line (a task longer than the tail), nothing
    # is skipped, which is acceptable.
    in_task = False

    for raw in text.splitlines():
        line = raw.rstrip()

        if in_task:
            if line == "---":
                in_task = False
            continue
        if line == "## Task":
            in_task = True
            continue

        if _FOOTER.match(line):
            finished = True
        if (_WRAP_CAP.match(line) or _BUDGET_CLOSEOUT.match(line)
                or line.strip() in _WRAP_MARKERS):
            wrapping = True

        mc = _MODEL_CALL.match(line)
        if mc:
            it = int(mc.group(1))
            iteration = it if iteration is None else max(iteration, it)
            attempt = int(mc.group(2))
            max_attempts = int(mc.group(3))
            last_kind = "model_call"
            continue

        cut = _CUT.match(line)
        if cut:
            it = int(cut.group(1))
            iteration = it if iteration is None else max(iteration, it)
            cuts += 1
            continue

        step = _STEP.match(line)
        if step:
            it = int(step.group(1))
            iteration = it if iteration is None else max(iteration, it)
            name = step.group(2)
            raw_args = step.group(3) or ""
            if name == "NO_TOOL_ACTION":
                nudges += 1
                continue
            if name in _CONTROL:
                # thinking/finish/etc. are assistant markers, not tool progress.
                continue
            # A real tool step: it drives the phase and resets both retry counts.
            last_kind = "tools"
            nudges = 0
            cuts = 0
            last_tool = name
            last_path = _safe_path(raw_args) if name in _PATH_TOOLS else None

    if finished:
        phase: Phase = "finished"
    elif wrapping:
        phase = "wrapping_up"
    elif last_kind == "model_call":
        phase = "model_call"
    elif last_kind == "tools":
        phase = "tools"
    else:
        phase = "starting"

    if phase != "model_call":
        attempt = None
        max_attempts = None

    return RunProgress(
        iteration=iteration,
        phase=phase,
        attempt=attempt,
        max_attempts=max_attempts,
        nudges=nudges,
        last_tool=last_tool,
        last_path=last_path,
        cuts=cuts,
    )


def read_progress(transcript_path, *, tail_bytes: int = 65_536) -> RunProgress | None:
    """Parse progress from at most the last ``tail_bytes`` of the transcript.

    Returns None when the file cannot be read (missing, directory, permission)
    and never raises.
    """
    path = Path(transcript_path)
    try:
        with path.open("rb") as handle:
            handle.seek(0, 2)
            size = handle.tell()
            start = max(0, size - tail_bytes)
            handle.seek(start)
            data = handle.read()
    except (OSError, ValueError):
        return None
    text = data.decode("utf-8", errors="replace")
    if start > 0:
        # Started mid-file: drop the first, partial line.
        newline = text.find("\n")
        text = text[newline + 1 :] if newline != -1 else ""
    return parse_progress(text)

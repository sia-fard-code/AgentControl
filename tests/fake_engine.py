#!/usr/bin/env python3
"""A standalone fake delegate engine for the intervention end-to-end test.

It mimics just enough of the real engine to be a live process the dashboard can
narrate, steer and kill: it registers a schema-2 start record in the machine
index, streams a transcript turn by turn, honours the steer-file protocol, and
spawns a long-lived child so the kill path has a real process tree to reap.

All content is synthetic; this script never touches live runtime state.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _sibling(transcript: str, suffix: str) -> str:
    assert transcript.endswith(".md")
    return transcript[: -len(".md")] + suffix


class Transcript:
    """A transcript file that flushes and fsyncs on every write."""

    def __init__(self, path: str) -> None:
        self._fh = open(path, "a", encoding="utf-8", newline="\n")

    def write(self, text: str) -> None:
        self._fh.write(text)
        self._fh.flush()
        os.fsync(self._fh.fileno())

    def close(self) -> None:
        self._fh.close()


def _apply_steer(transcript: Transcript, steer_path: str, turn: int) -> None:
    if not os.path.exists(steer_path):
        return
    try:
        with open(steer_path, "r", encoding="utf-8") as fh:
            text = fh.read().strip()
    except OSError:
        return
    if text:
        transcript.write(f"\n### [{turn}] OPERATOR GUIDANCE\n\n> " + text + "\n")
    open(steer_path, "w").close()


def _header(label: str, root: str) -> str:
    name = "<name>"
    return "\n".join([
        f"# {label}",
        "",
        "- model: `fake-model`  effort: `medium`  tools: `native`",
        f"- writes: `disk`  root: `{root}`",
        f"- steer: write guidance into `{name}.md.steer` to redirect mid-run",
        "",
        "## Task",
        "",
        "Synthetic end-to-end task.",
        "",
        "---",
        "",
    ]) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--index", required=True)
    parser.add_argument("--transcript", required=True)
    parser.add_argument("--root", required=True)
    parser.add_argument("--label", required=True)
    parser.add_argument("--turns", type=int, required=True)
    parser.add_argument("--turn-seconds", type=float, required=True)
    parser.add_argument("--child-pid-file", required=True)
    args = parser.parse_args()

    # 1. Start a long-lived child and record its pid.
    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(300)"]
    )
    with open(args.child_pid_file, "w", encoding="utf-8") as fh:
        fh.write(str(child.pid))

    transcript_path = args.transcript
    steer_path = transcript_path + ".steer"
    run_id = f"e2e-{os.getpid()}"

    # 2. Append the schema-2 start record to the machine index.
    start_record = {
        "schema": 2,
        "run_id": run_id,
        "event": "start",
        "ts": _now_iso(),
        "harness_version": 6,
        "label": args.label,
        "model": "fake-model",
        "effort": "medium",
        "tool_mode": "native",
        "root": args.root,
        "cwd": args.root,
        "engine_pid": os.getpid(),
        "transcript": transcript_path,
        "messages": _sibling(transcript_path, ".messages.jsonl"),
        "usage_journal": _sibling(transcript_path, ".usage.jsonl"),
        "write_mode": "disk",
        "max_iters": args.turns,
    }
    with open(args.index, "a", encoding="utf-8", newline="\n") as fh:
        fh.write(json.dumps(start_record) + "\n")

    # 3. Write the transcript header.
    transcript = Transcript(transcript_path)
    transcript.write(_header(args.label, args.root))

    # 4. Run the turns.
    tool_calls = 0
    for turn in range(1, args.turns + 1):
        _apply_steer(transcript, steer_path, turn)
        transcript.write(
            f"_[ {turn}] calling fake-model (attempt 1/4, 2 msgs, ~100 bytes) ..._\n"
        )
        time.sleep(args.turn_seconds)
        transcript.write(
            f"\n### [{turn}] thinking\n\n"
            f"Turn {turn}: reading a synthetic file MARKER-NARRATION-XYZ\n"
        )
        transcript.write(
            f'\n### [{turn}] read_file({{"path": "file_{turn}.txt"}})\n\n'
            f"```\nsynthetic output {turn}\n```\n"
        )
        tool_calls += 1

    # 5. Finish.
    transcript.write("\n---\n\n## Final report\n\nDone.\n")
    transcript.write(
        f"\n[fake-model e=medium tools=native: {tool_calls} tool calls]\n"
    )
    transcript.close()

    end_record = {
        "schema": 2,
        "run_id": run_id,
        "event": "end",
        "ts": _now_iso(),
        "status": "complete",
        "tool_calls": tool_calls,
        "refused": 0,
        "sent": 100,
        "recv": 100,
    }
    with open(args.index, "a", encoding="utf-8", newline="\n") as fh:
        fh.write(json.dumps(end_record) + "\n")

    child.terminate()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

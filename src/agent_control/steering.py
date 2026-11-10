"""The steer-file protocol and delivery tracking.

Pending steer text lives only in process memory, for delivery matching, and
is never logged or persisted by AgentControl.
"""

from __future__ import annotations

import os
import re
import threading
from dataclasses import dataclass
from pathlib import Path

MAX_STEER_CHARS = 4000
WRAP_UP_TEXT = ("Stop working now. Do not call any tool except finish. Call finish with "
                "your report: what you completed, what you verified and how, and what "
                "is still incomplete or unverified.")


class SteerError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def validate_text(text: object) -> str:
    if not isinstance(text, str):
        raise SteerError("invalid_text", "text must be a string")
    trimmed = text.strip()
    if not trimmed:
        raise SteerError("invalid_text", "text must not be empty")
    if len(trimmed) > MAX_STEER_CHARS:
        raise SteerError("invalid_text", f"text must be at most {MAX_STEER_CHARS} characters")
    for ch in trimmed:
        code = ord(ch)
        if code < 0x20 and ch not in ("\n", "\t"):
            raise SteerError("invalid_text", "text contains a control character")
        if code == 0x7F:
            raise SteerError("invalid_text", "text contains a control character")
    return trimmed


def steer_path_for(transcript: str) -> Path:
    if not transcript.endswith(".md") or not Path(transcript).is_file():
        raise SteerError("no_transcript", "transcript must be an existing .md file")
    steer = Path(transcript + ".steer")
    if steer.is_symlink() or steer.is_dir():
        raise SteerError("bad_steer_path", "steer path is a directory or symlink")
    return steer


def read_steer_file(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def append_steer(path: Path, text: str) -> None:
    existing = read_steer_file(path)
    with open(path, "a", encoding="utf-8", newline="\n") as fh:
        if existing.strip():
            fh.write("\n\n")
        fh.write(text + "\n")
        fh.flush()
        os.fsync(fh.fileno())


def cancel_steer(path: Path, text: str) -> bool:
    content = read_steer_file(path)
    idx = content.rfind(text)
    if idx == -1:
        return False
    end = idx + len(text)
    before = content[:idx]
    after = content[end:]
    if before.endswith("\n\n"):
        before = before[:-2]
    elif after.startswith("\n\n"):
        after = after[2:]
    remainder = before + after
    if not remainder.strip():
        remainder = ""
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(remainder)
    return True


def normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


@dataclass(frozen=True)
class PendingSteer:
    id: str
    run_id: str
    text: str
    kind: str
    chars: int
    sent_at: str
    events_at_send: int


def delivery_state(pending: PendingSteer, *, guidance, steer_file_text: str,
                   run_open: bool) -> tuple[str, int | None]:
    target = normalize(pending.text)
    for seq, iteration, text in guidance:
        if seq >= pending.events_at_send and target in normalize(text):
            return ("delivered", iteration)
    if not run_open:
        return ("undeliverable", None)
    if target in normalize(steer_file_text):
        return ("queued", None)
    return ("lost", None)


def status_dict(pending: PendingSteer, state: str, turn: int | None) -> dict[str, object]:
    return {
        "id": pending.id,
        "state": state,
        "kind": pending.kind,
        "chars": pending.chars,
        "sent_at": pending.sent_at,
        "turn": turn,
    }


class SteerBook:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._by_run: dict[str, list[PendingSteer]] = {}
        self._run_locks: dict[str, threading.Lock] = {}

    def add(self, pending: PendingSteer) -> None:
        with self._lock:
            items = self._by_run.setdefault(pending.run_id, [])
            items.append(pending)
            if len(items) > 5:
                del items[:-5]

    def for_run(self, run_id: str) -> list[PendingSteer]:
        with self._lock:
            return list(self._by_run.get(run_id, []))

    def get(self, run_id: str, steer_id: str) -> PendingSteer | None:
        with self._lock:
            for item in self._by_run.get(run_id, []):
                if item.id == steer_id:
                    return item
            return None

    def remove(self, run_id: str, steer_id: str) -> None:
        with self._lock:
            items = self._by_run.get(run_id)
            if items is None:
                return
            self._by_run[run_id] = [i for i in items if i.id != steer_id]

    def lock_for(self, run_id: str) -> threading.Lock:
        with self._lock:
            lock = self._run_locks.get(run_id)
            if lock is None:
                lock = threading.Lock()
                self._run_locks[run_id] = lock
            return lock

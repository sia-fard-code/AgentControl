"""Read-only, retained-handle observations outside the engine's process tree."""
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import sys

from harness_process_identity import ProcessHandle


def _recorded_birth(start):
    try:
        with open(start.transcript + ".engine.diagnostics.jsonl", "rb") as handle:
            offset = max(0, handle.seek(0, 2) - 256 * 1024)
            handle.seek(offset)
            lines = handle.read(256 * 1024).split(b"\n")[:-1]
        if offset:
            lines = lines[1:]
        for line in reversed(lines):
            try:
                row = json.loads(line)
            except (ValueError, UnicodeError):
                continue
            if not isinstance(row, dict):
                continue
            birth = row.get("engine_birth")
            if (type(row.get("schema")) is int and row["schema"] == 1 and row.get("observer") == "engine"
                    and row.get("run_id") == start.run_id
                    and type(row.get("engine_pid")) is int and row["engine_pid"] == start.engine_pid
                    and isinstance(birth, str) and re.fullmatch(r"winft:[0-9]{1,20}", birth)):
                return birth
    except (OSError, ValueError, TypeError, RecursionError):
        pass
    return None


class ExitObserver:
    def __init__(self, capacity=256, open_handle=None):
        self.capacity = capacity
        self.open_handle = open_handle or ProcessHandle.open
        self.handles = {}
        self.unknown = set()
        self.warned = False

    def attach(self, start, snapshot):
        handle = None
        try:
            if (type(start.engine_pid) is not int or start.engine_pid != snapshot.pid
                    or not Path(start.transcript).is_absolute()
                    or not re.fullmatch(r"\w[\w.:-]{0,199}", start.run_id)):
                return False
            expected = _recorded_birth(start)
            if expected is None:
                return False
            key = (start.run_id, start.engine_pid)
            existing = self.handles.get(key)
            if existing and existing[0].birth == expected:
                return True
            self.poll(start.engine_pid)
            handle = self.open_handle(start.engine_pid, expected_creation_time=snapshot.create_time)
            if handle is None or handle.birth != expected:
                return False
            if key in self.handles:
                self.handles.pop(key)[0].close()
                self.unknown.discard(key)
            if len(self.handles) >= self.capacity:
                old_key = next(iter(self.handles))
                old = self.handles.pop(old_key)
                self.unknown.discard(old_key)
                self._write(old, "observer_detached")
                old[0].close()
            item = (handle, start)
            self.handles[key], handle = item, None
            self._write(item, "observer_attached")
            return True
        except Exception:
            return False
        finally:
            if handle is not None:
                handle.close()

    def _write(self, item, event, result=None):
        handle, start = item
        row = {"schema": 1, "observer": "monitor", "event": event,
               "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
               "pid": os.getpid(), "ppid": os.getppid(), "platform": os.name,
               "run_id": start.run_id, "engine_pid": start.engine_pid, "engine_birth": handle.birth}
        if result is not None:
            code = result.get("exit_code")
            row["exit_code"] = code if type(code) is int and 0 <= code <= 0xffffffff else None
            row["windows_status"] = row["exit_code"]
            ticks = result.get("exit_ticks")
            if isinstance(ticks, str) and re.fullmatch(r"[0-9]{1,20}", ticks):
                row["exit_ticks"] = ticks
        try:
            with open(start.transcript + ".observer.diagnostics.jsonl", "a", encoding="utf-8", newline="\n") as out:
                out.write(json.dumps(row, separators=(",", ":"), allow_nan=False) + "\n")
                out.flush()
                os.fsync(out.fileno())
        except (OSError, ValueError):
            if not self.warned:
                self.warned = True
                try:
                    print("WARNING: independent exit diagnostics unavailable", file=sys.stderr)
                except Exception:
                    pass

    def poll(self, pid=None):
        for key, item in list(self.handles.items()):
            if pid is not None and key[1] != pid:
                continue
            result = None
            try:
                result = item[0].poll()
                if result["state"] == "running":
                    continue
                if result["state"] == "exited":
                    self._write(item, "observer_exit", result)
                elif key not in self.unknown:
                    self._write(item, "observer_unknown", result)
                    self.unknown.add(key)
            except Exception:
                if key not in self.unknown:
                    self._write(item, "observer_unknown")
                    self.unknown.add(key)
            finally:
                if result and result.get("state") == "exited":
                    item[0].close()
                    self.handles.pop(key, None)
                    self.unknown.discard(key)

    def close(self):
        self.poll()
        for item in self.handles.values():
            try:
                self._write(item, "observer_detached")
            finally:
                item[0].close()
        self.handles.clear()
        self.unknown.clear()

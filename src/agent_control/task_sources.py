"""Operator-chosen task-status source registry (startup-only, closed schema).

Precondition (caller/ROOT, not enforced here): the registry file and every
status path's parent chain are operator-protected and outside worker write
authority. This module is not an OS sandbox against the same SID.
"""

from __future__ import annotations

import json
import ntpath
import os
import re
import stat
from pathlib import Path

from .models import TaskStatusSource

MAX_BYTES = 64 * 1024
MAX_ENTRIES = 64
_KEYS = frozenset({"project_id", "project_root", "envelope_sha256", "path"})
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
_SHA = re.compile(r"[0-9a-f]{64}")
_REPARSE = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)


class TaskSourcesError(Exception):
    """Fixed code only; never carries registry content, paths or causes."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _fail(code: str = "task_sources_invalid") -> TaskSourcesError:
    return TaskSourcesError(code)


def _local_abs(value: object) -> str:
    """Lexically canonical absolute local path string, or raise."""
    if type(value) is not str or not value or "\x00" in value:
        raise _fail() from None
    if value.startswith(("\\\\", "//")) or not os.path.isabs(value):
        raise _fail() from None
    if os.name == "nt":
        drive, rest = ntpath.splitdrive(value)
        if not re.fullmatch(r"[A-Za-z]:", drive) or ":" in rest or "/" in value:
            raise _fail() from None
        parts = rest.split("\\")[1:]
        if any(p != p.rstrip(". ") for p in parts if p):
            raise _fail() from None
    if os.path.normpath(value) != value:
        raise _fail() from None
    if any(p in (".", "..") for p in re.split(r"[\\/]", value)):
        raise _fail() from None
    return value


def _key(path: str) -> str:
    return os.path.normcase(path)


def _is_link(st: os.stat_result) -> bool:
    return stat.S_ISLNK(st.st_mode) or bool(
        getattr(st, "st_file_attributes", 0) & _REPARSE)


def _shared(st: os.stat_result) -> tuple:
    # Path vs handle APIs differ (e.g. Windows ctime); compare only shared fields.
    return (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_mode, st.st_nlink)


def _full(st: os.stat_result) -> tuple:
    return _shared(st) + (st.st_ctime_ns, getattr(st, "st_file_attributes", 0))


def _read_bounded(path: Path) -> bytes:
    try:
        before = os.lstat(path)
    except FileNotFoundError:
        raise _fail("task_sources_missing") from None
    except OSError:
        raise _fail("task_sources_unreadable") from None
    if _is_link(before) or not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
        raise _fail("task_sources_unprotected") from None
    if before.st_size > MAX_BYTES:
        raise _fail("task_sources_too_large") from None
    try:
        for parent in path.parents:
            if _is_link(os.lstat(parent)):
                raise _fail("task_sources_unprotected") from None
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(path, flags)
        try:
            opened = os.fstat(fd)
            data = os.read(fd, MAX_BYTES + 1)
            closed = os.fstat(fd)
        finally:
            os.close(fd)
        after = os.lstat(path)
    except TaskSourcesError:
        raise
    except OSError:
        raise _fail("task_sources_unreadable") from None
    if (_shared(opened) != _shared(before) or _full(closed) != _full(opened)
            or _full(after) != _full(before)):
        raise _fail("task_sources_unstable") from None
    if len(data) > MAX_BYTES:
        raise _fail("task_sources_too_large") from None
    if len(data) != before.st_size:
        raise _fail("task_sources_unstable") from None
    return data


def _inside(child: str, root: str) -> bool:
    c, r = _key(child), _key(root)
    try:
        return os.path.commonpath([c, r]) == r
    except ValueError:  # different drives
        return False


def _no_dupes(pairs: list) -> dict:
    keys = [k for k, _ in pairs]
    if len(set(keys)) != len(keys):
        raise _fail() from None
    return dict(pairs)


def _no_const(_: str):
    raise _fail() from None


def _entry(raw: object) -> TaskStatusSource:
    if type(raw) is not dict or set(raw) != _KEYS:
        raise _fail() from None
    pid, pin = raw["project_id"], raw["envelope_sha256"]
    if type(pid) is not str or not _ID.fullmatch(pid):
        raise _fail() from None
    if type(pin) is not str or not _SHA.fullmatch(pin):
        raise _fail() from None
    root = _local_abs(raw["project_root"])
    path = _local_abs(raw["path"])
    # Status must not live under (or contain) the candidate write root.
    if _inside(path, root) or _inside(root, path):
        raise _fail() from None
    return TaskStatusSource(pid, root, pin, Path(path))


def load_task_sources(path: str | os.PathLike) -> tuple[TaskStatusSource, ...]:
    """Load the explicit operator registry; raise TaskSourcesError on any problem."""
    data = _read_bounded(Path(path))
    try:
        text = data.decode("utf-8")
        doc = json.loads(text, object_pairs_hook=_no_dupes, parse_constant=_no_const)
    except TaskSourcesError:
        raise _fail() from None
    except (UnicodeDecodeError, ValueError, RecursionError):
        raise _fail() from None
    if type(doc) is not dict or set(doc) != {"schema", "sources"}:
        raise _fail() from None
    if type(doc["schema"]) is not int or doc["schema"] != 1:
        raise _fail() from None
    raw_sources = doc["sources"]
    if type(raw_sources) is not list or len(raw_sources) > MAX_ENTRIES:
        raise _fail() from None
    sources = tuple(_entry(item) for item in raw_sources)
    paths: set[str] = set()
    pins: set[tuple[str, str]] = set()
    for src in sources:
        pkey, pin = _key(str(src.path)), (src.project_id, src.envelope_sha256)
        if pkey in paths or pin in pins:
            raise _fail("task_sources_ambiguous") from None
        paths.add(pkey)
        pins.add(pin)
    roots = {_key(s.project_root) for s in sources}
    for src in sources:  # a status path may not sit in any project's write root
        if any(_inside(_key(str(src.path)), r) for r in roots):
            raise _fail("task_sources_ambiguous") from None
    return sources

"""Bounded candidate snapshot transport (pure, stdlib only).

Capture is observational transport, NOT a trust certificate or acceptance: the parent must
hold ownership/quiescence guards and anchor receipt_sha256 in its journal. Assumes a local
filesystem and an exclusive writer; this is not an OS sandbox.
"""
import dataclasses
import hashlib
import json
import math
import os
import re
import secrets
import shutil
import stat
import sys
import time
from typing import Callable, Optional

from supervisor_journal import _fsync_dir

SCHEMA_MANIFEST = "candidate-manifest-1"
SCHEMA_RECEIPT = "candidate-receipt-1"
SCHEMA_RECEIPT_V2 = "candidate-receipt-2"
PLATFORM = "win32" if sys.platform == "win32" else "posix"
CHUNK = 65536
_SHA = re.compile(r"[0-9a-f]{64}")
_RESERVED = {"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$"} | {
    f"{p}{i}" for p in ("COM", "LPT") for i in range(1, 10)}
_REPARSE = 0x400


class CandidateError(Exception):
    """Safe typed failure; carries only a code, never raw OS text or content."""

    def __init__(self, code):
        super().__init__(code)
        self.code = code


@dataclasses.dataclass(frozen=True)
class SnapshotBounds:
    """Technical per-operation bounds (not goal lifetime quotas)."""
    max_entries: int = 100000
    max_file_bytes: int = 256 * 1024 * 1024
    max_total_bytes: int = 256 * 1024 * 1024
    max_manifest_bytes: int = 64 * 1024 * 1024
    max_path_bytes: int = 4096
    max_seconds: float = 10

    def __post_init__(self):
        for f in dataclasses.fields(self):
            v = getattr(self, f.name)
            ok_type = (isinstance(v, (int, float)) if f.name == "max_seconds" else isinstance(v, int))
            if isinstance(v, bool) or not ok_type or not math.isfinite(v) or v <= 0:
                raise CandidateError("invalid_bounds")


@dataclasses.dataclass(frozen=True)
class GitAdmin:
    """Separately protected root .git pointer; the parent must authenticate it (not a permission)."""
    pointer_sha256: str
    guard: Callable[[], bool]


@dataclasses.dataclass(frozen=True)
class Snapshot:
    """Closed metadata references only; a claim, not approval."""
    directory: str
    manifest_sha256: str
    receipt_sha256: str
    scope_sha256: str
    file_count: int
    total_file_bytes: int
    seed_sha256: Optional[str]
    context_sha256: Optional[str] = None


def _canon(obj):
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def _sha(b):
    return hashlib.sha256(b).hexdigest()


def _strict_json(b):
    def pairs(items):
        d = dict(items)
        if len(d) != len(items):
            raise ValueError
        return d
    try:
        return json.loads(b.decode("utf-8"), object_pairs_hook=pairs,
                          parse_float=lambda c: (_ for _ in ()).throw(ValueError()),
                          parse_constant=lambda c: (_ for _ in ()).throw(ValueError()))
    except (ValueError, UnicodeDecodeError, RecursionError):
        raise CandidateError("tampered") from None


def _check_rel(rel, bounds, allow_dot=True):
    """Validate a normalized relative POSIX path; return its components."""
    if not isinstance(rel, str):
        raise CandidateError("unsafe_path")
    if rel == "." and allow_dot:
        return ()
    if len(rel.encode("utf-8", "surrogatepass")) > bounds.max_path_bytes:
        raise CandidateError("bound_path")
    parts = rel.split("/")
    for p in parts:
        if (not p or p in (".", "..") or any(c in p for c in '\\:*?"<>|') or p[-1] in ". "
                or any(ord(c) < 32 or 0xD800 <= ord(c) <= 0xDFFF for c in p)
                or p.split(".")[0].upper() in _RESERVED):
            raise CandidateError("unsafe_path")
    return tuple(parts)


def _guard(fn, code="owner_guard"):
    try:
        ok = fn() is True
    except Exception:
        ok = False
    if not ok:
        raise CandidateError(code)


def _tick(t0, bounds):
    if time.monotonic() - t0 > bounds.max_seconds:
        raise CandidateError("bound_time")


def _ident(st):
    return (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, stat.S_IFMT(st.st_mode))


def _is_plain(st, kind):
    if st is None:
        return False
    if getattr(st, "st_file_attributes", 0) & _REPARSE:
        return False
    if kind == "dir":
        return stat.S_ISDIR(st.st_mode)
    return stat.S_ISREG(st.st_mode) and st.st_nlink == 1


def _norm(p):
    return os.path.normcase(os.path.abspath(os.fspath(p)))


def _inside(child, parent):
    return child == parent or child.startswith(parent.rstrip(os.sep) + os.sep)


def _write_all(fd, data):
    view = memoryview(data)
    while view:
        n = os.write(fd, view)  # os.write may be short
        if n <= 0:
            raise OSError("short write")
        view = view[n:]


_BIN = getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)


def _hash_file(path, st, bounds, out=None, limit=None, *, t0):
    """Stream a no-follow regular file (optionally into fresh file `out`); return (sha, size).

    Checks the operation deadline `t0` before each chunk; cannot abort an already-blocked read."""
    limit = bounds.max_file_bytes if limit is None else limit
    fd = os.open(path, os.O_RDONLY | _BIN)
    try:
        if _ident(os.fstat(fd)) != _ident(st):
            raise CandidateError("changed_during_capture")
        h, size = hashlib.sha256(), 0
        wfd = os.open(out, os.O_WRONLY | os.O_CREAT | os.O_EXCL | _BIN, 0o600) if out else None
        try:
            while True:
                _tick(t0, bounds)
                chunk = os.read(fd, CHUNK)
                if not chunk:
                    break
                size += len(chunk)
                if size > limit:
                    raise CandidateError("bound_file")
                h.update(chunk)
                if wfd is not None:
                    _write_all(wfd, chunk)
            if wfd is not None:
                os.fsync(wfd)
        finally:
            if wfd is not None:
                os.close(wfd)
        if _ident(os.fstat(fd)) != _ident(st) or size != st.st_size:
            raise CandidateError("changed_during_capture")
    finally:
        os.close(fd)
    return h.hexdigest(), size


def _inputs(inputs, bounds, t0=None):
    if not isinstance(bounds, SnapshotBounds):
        raise CandidateError("invalid_bounds")
    if isinstance(inputs, (str, bytes, dict)):
        raise CandidateError("unsafe_path")
    try:
        iterator = iter(inputs)
    except TypeError:
        raise CandidateError("unsafe_path") from None
    t0 = time.monotonic() if t0 is None else t0
    sels, folded, size = [], [], 1
    for s in iterator:
        _tick(t0, bounds)
        if len(sels) >= bounds.max_entries:
            raise CandidateError("bound_entries")
        parts = _check_rel(s, bounds)
        if any(p.casefold() == ".git" for p in parts):
            raise CandidateError("git_admin_required")
        size += len(_canon(s)) + 1
        if size > bounds.max_manifest_bytes:
            raise CandidateError("bound_manifest")
        sels.append(s)
        folded.append(tuple(p.casefold() for p in parts))
    if not sels:
        raise CandidateError("unsafe_path")
    folded.sort()
    _tick(t0, bounds)
    for a, b in zip(folded, folded[1:]):
        _tick(t0, bounds)
        if b[:len(a)] == a:
            raise CandidateError("unsafe_path")
    sels.sort()
    _tick(t0, bounds)
    return tuple(sels)


def _physical(path):
    """Trusted root/operational/destination ancestry only, never manifest paths."""
    try:
        return _norm(os.path.realpath(os.fspath(path)))
    except (OSError, ValueError, TypeError):
        raise CandidateError("unsafe_path") from None


def _operational(root, sels, roots, bounds, t0):
    nroot, ops = _physical(root), []
    for op in roots:
        _tick(t0, bounds)
        if len(ops) >= bounds.max_entries:
            raise CandidateError("bound_entries")
        o = _physical(op)
        for s in sels:
            _tick(t0, bounds)
            a = nroot if s == "." else _norm(os.path.join(nroot, *s.split("/")))
            if _inside(o, a) or _inside(a, o):
                raise CandidateError("operational_overlap")
        ops.append(o)
    ordered = sorted(ops, key=lambda p: tuple(p.split(os.sep)))
    for a, b in zip(ordered, ordered[1:]):
        _tick(t0, bounds)
        if _inside(b, a):
            raise CandidateError("operational_overlap")
    return nroot, tuple(ops)


def _git_guard(git_admin):
    if git_admin is not None:
        if (not isinstance(git_admin, GitAdmin)
                or not isinstance(git_admin.pointer_sha256, str)
                or not _SHA.fullmatch(git_admin.pointer_sha256)):
            raise CandidateError("git_admin_mismatch")
        _guard(git_admin.guard, "git_admin_guard")


def _lstat(path):
    try:
        return os.lstat(path)
    except FileNotFoundError:
        return None
    except OSError:
        raise CandidateError("io_error") from None


def _listdir(path, bounds, t0):
    try:
        names = []
        with os.scandir(path) as it:
            for entry in it:
                _tick(t0, bounds)
                if len(names) >= bounds.max_entries + 1:
                    raise CandidateError("bound_entries")
                names.append(entry.name)
        names.sort()
        _tick(t0, bounds)
        return names
    except OSError:
        raise CandidateError("io_error") from None


def _walk(root, sels, bounds, t0, git_admin, strict_layout=False, operational_roots=()):
    """No-follow bounded walk: rel -> (kind, st, abspath); plus git pointer and identity checks."""
    entries, checks, git, listings = {}, [], [], {}
    root_st = _lstat(root)
    if root_st is None or not _is_plain(root_st, "dir"):
        raise CandidateError("unsupported_entry")
    checks.append((root, root_st))
    nroot = _physical(root) if strict_layout else None

    def names(ap):
        if ap not in listings:
            ns = _listdir(ap, bounds, t0)
            listings[ap] = (ns, set(ns), {n.casefold() for n in ns})
        return listings[ap]

    def add(rel, kind, st, ap):
        if len(entries) + 1 > bounds.max_entries:
            raise CandidateError("bound_entries")
        entries[rel] = (kind, st, ap)

    def visit(rel, ap, st):
        if _is_plain(st, "dir"):
            add(rel, "dir", st, ap)
            walkdir(rel, ap)
        elif _is_plain(st, "file"):
            add(rel, "file", st, ap)
        else:
            raise CandidateError("unsupported_entry")

    def walkdir(rel, ap):
        # explicit stack: deep trees must not hit the Python recursion limit
        # a stack of lazy scans keeps the recursive version's exact DFS/error order
        stack = [scan(rel, ap)]
        while stack:
            item = next(stack[-1], None)
            if item is None:
                stack.pop()
                continue
            child, cp, st, structural = item
            if not structural:
                if _is_plain(st, "file"):
                    add(child, "file", st, cp)
                    continue
                if not _is_plain(st, "dir"):
                    raise CandidateError("unsupported_entry")
                add(child, "dir", st, cp)
            stack.append(scan(child, cp))

    def scan(rel, ap):
        _tick(t0, bounds)
        seen = set()
        for name in names(ap)[0]:
            _tick(t0, bounds)
            cp = os.path.join(ap, name)
            if name.casefold() == ".git":
                if rel or git_admin is None:
                    raise CandidateError("git_admin_required")
                if name != ".git":
                    raise CandidateError("git_admin_mismatch")
                st = _lstat(cp)
                if st is None or not _is_plain(st, "file"):
                    raise CandidateError("git_admin_mismatch")
                git.append((cp, st))
                continue
            child = f"{rel}/{name}" if rel else name
            _check_rel(child, bounds, allow_dot=False)
            if name.casefold() in seen:
                raise CandidateError("unsafe_path")
            seen.add(name.casefold())
            st = _lstat(cp)
            if strict_layout and not _in_scope(child, sels):
                physical_child = _norm(os.path.join(nroot, *child.split("/")))
                if physical_child in operational_roots:
                    continue
                structural = any(s.startswith(child + "/") for s in sels)
                structural = structural or any(
                    _inside(o, physical_child) for o in operational_roots)
                if not structural:
                    raise CandidateError("undeclared_entry")
                if not _is_plain(st, "dir"):
                    raise CandidateError("unsupported_entry")
                checks.append((cp, st))
                yield child, cp, st, True
            else:
                yield child, cp, st, False

    if strict_layout:
        walkdir("", root)
        return entries, checks, git
    for sel in sels:
        _tick(t0, bounds)
        parts = _check_rel(sel, bounds)
        if not parts:
            walkdir("", root)
            continue
        ap, st = root, root_st
        for i, p in enumerate(parts):
            _tick(t0, bounds)
            _, exact, folded = names(ap)
            if p not in exact:  # absent, or a case alias of a different real name
                if p.casefold() in folded:
                    raise CandidateError("unsafe_path")
                st = None
                break
            ap = os.path.join(ap, p)
            st = _lstat(ap)
            if st is not None and i < len(parts) - 1:
                if not _is_plain(st, "dir"):
                    raise CandidateError("unsupported_entry")
                checks.append((ap, st))
        if st is not None:  # missing selector = recorded absence via the bound inputs list
            visit(sel, ap, st)
    return entries, checks, git


def _build(root, sels, bounds, t0, git_admin, stage=None, *,
           strict_layout=False, operational_roots=()):
    """Return (manifest_dict, manifest_bytes, git_pointer_sha, file_count, total)."""
    root = os.path.abspath(os.fspath(root))
    entries, checks, git = _walk(root, sels, bounds, t0, git_admin,
                                 strict_layout, operational_roots)
    out, total, files = [], 0, 0
    for rel in sorted(entries):
        kind, st, ap = entries[rel]
        _tick(t0, bounds)
        if kind == "dir":
            out.append({"path": rel, "type": "dir"})
            continue
        if st.st_size > bounds.max_file_bytes:
            raise CandidateError("bound_file")
        total += st.st_size
        if total > bounds.max_total_bytes:
            raise CandidateError("bound_total")
        sha, size = stage.add(ap, st, t0) if stage else _hash_file(ap, st, bounds, t0=t0)
        files += 1
        out.append({"path": rel, "type": "file", "size": size, "sha256": sha,
                    "exec": None if PLATFORM == "win32" else bool(st.st_mode & 0o100)})
    for ap, st in checks + [(e[2], e[1]) for e in entries.values()]:
        now = _lstat(ap)
        if now is None or _ident(now) != _ident(st):
            raise CandidateError("changed_during_capture")
    git_sha = None
    if git:
        git_sha, _ = _hash_file(git[0][0], git[0][1], bounds, limit=4096, t0=t0)
        if git_sha != git_admin.pointer_sha256:
            raise CandidateError("git_admin_mismatch")
    manifest = {"schema": SCHEMA_MANIFEST, "platform": PLATFORM, "inputs": list(sels),
                "entries": out}
    _validate_manifest(manifest, bounds, t0)
    mb = _canon(manifest)
    if len(mb) > bounds.max_manifest_bytes:
        raise CandidateError("bound_manifest")
    return manifest, mb, git_sha, files, total


class _Stage:
    """Fresh private staging dir beside `destination`; published by one atomic rename."""

    def __init__(self, destination, owner_guard, bounds):
        self.dest = os.path.abspath(os.fspath(destination))
        self.guard, self.bounds = owner_guard, bounds
        parent = os.path.dirname(self.dest)
        pst = _lstat(parent)
        if pst is None or not _is_plain(pst, "dir"):
            raise CandidateError("destination_unsafe")
        if os.path.lexists(self.dest):
            raise CandidateError("destination_exists")
        _guard(owner_guard)
        self.path = os.path.join(parent, f".{os.path.basename(self.dest)}.stage-{secrets.token_hex(4)}")
        os.mkdir(self.path, 0o700)

    def add(self, src, st, t0):
        blobs = os.path.join(self.path, "blobs")
        if not os.path.isdir(blobs):
            os.mkdir(blobs, 0o700)
        tmp = os.path.join(blobs, ".tmp-" + secrets.token_hex(4))
        sha, size = _hash_file(src, st, self.bounds, out=tmp, t0=t0)
        final = os.path.join(blobs, sha)
        if os.path.lexists(final):  # identical content already staged
            os.unlink(tmp)
        else:
            os.rename(tmp, final)
        return sha, size

    def write(self, name, data):
        fd = os.open(os.path.join(self.path, name), os.O_WRONLY | os.O_CREAT | os.O_EXCL | _BIN, 0o600)
        try:
            _write_all(fd, data)
            os.fsync(fd)
        finally:
            os.close(fd)

    def publish(self):
        _guard(self.guard)
        for d, _, _ in os.walk(self.path):
            _fsync_dir(d)
        if os.path.lexists(self.dest):
            raise CandidateError("destination_exists")
        os.rename(self.path, self.dest)  # shortcut: exclusive-writer assumption covers the check/rename gap
        _fsync_dir(os.path.dirname(self.dest))
        self.path = None

    def cleanup(self):
        if self.path:
            shutil.rmtree(self.path, ignore_errors=True)


def _read_small(path, limit):
    st = _lstat(path)
    if st is None or not _is_plain(st, "file") or st.st_size > limit:
        raise CandidateError("tampered")
    try:
        with open(path, "rb") as f:
            data = f.read(limit + 1)
    except OSError:
        raise CandidateError("tampered") from None
    if len(data) > limit:
        raise CandidateError("tampered")
    return data


def _digest(value):
    return isinstance(value, str) and _SHA.fullmatch(value) is not None


def _counter(value):
    return type(value) is int and value >= 0


def _validate_manifest(m, bounds, t0):
    """Validate logical identity independently of transport hashes and blob locations."""
    if (type(m) is not dict
            or set(m) != {"schema", "platform", "inputs", "entries"}
            or m["schema"] != SCHEMA_MANIFEST or m["platform"] != PLATFORM
            or type(m["inputs"]) is not list or type(m["entries"]) is not list):
        raise CandidateError("tampered")
    try:
        sels = _inputs(m["inputs"], bounds, t0)
    except CandidateError as e:
        if e.code.startswith("bound_"):
            raise
        raise CandidateError("tampered") from None
    if list(sels) != m["inputs"]:
        raise CandidateError("tampered")
    metadata_bytes = len(_canon({"schema": SCHEMA_MANIFEST, "platform": PLATFORM,
                                 "inputs": list(sels), "entries": []}))
    if metadata_bytes > bounds.max_manifest_bytes:
        raise CandidateError("bound_manifest")
    entries, tree, files, total, prev = {}, {}, 0, 0, None
    for e in m["entries"]:
        _tick(t0, bounds)
        if len(entries) >= bounds.max_entries:
            raise CandidateError("bound_entries")
        if type(e) is not dict:
            raise CandidateError("tampered")
        kind = e.get("type")
        if kind == "dir":
            keys = {"path", "type"}
        elif kind == "file":
            keys = {"path", "type", "size", "sha256", "exec"}
        else:
            raise CandidateError("tampered")
        if set(e) != keys:
            raise CandidateError("tampered")
        rel = e["path"]
        try:
            parts = _check_rel(rel, bounds, allow_dot=False)
        except CandidateError as error:
            if error.code.startswith("bound_"):
                raise
            raise CandidateError("tampered") from None
        if (any(p.casefold() == ".git" for p in parts)
                or not _in_scope(rel, sels) or (prev is not None and rel <= prev)):
            raise CandidateError("tampered")
        prev = rel
        if kind == "file":
            if (not _counter(e["size"]) or not _digest(e["sha256"])
                    or (e["exec"] is not None if PLATFORM == "win32"
                        else type(e["exec"]) is not bool)):
                raise CandidateError("tampered")
            if e["size"] > bounds.max_file_bytes:
                raise CandidateError("bound_file")
            files += 1
            total += e["size"]
            if total > bounds.max_total_bytes:
                raise CandidateError("bound_total")
        for i in range(1, len(parts) + 1):
            spelling = parts[:i]
            folded = tuple(p.casefold() for p in spelling)
            node = (spelling, kind if i == len(parts) else "dir")
            if folded in tree and tree[folded] != node:
                raise CandidateError("tampered")
            tree[folded] = node
            if i < len(parts):
                parent = "/".join(spelling)
                if (_in_scope(parent, sels)
                        and entries.get(parent, {}).get("type") != "dir"):
                    raise CandidateError("tampered")
        metadata_bytes += len(_canon(e)) + (1 if entries else 0)
        if metadata_bytes > bounds.max_manifest_bytes:
            raise CandidateError("bound_manifest")
        entries[rel] = e
    return entries, files, total


def _load(snapshot, bounds, t0, verify_blobs=True):
    """Strictly verify a sealed snapshot against its claimed metadata; return manifest entries."""
    if not isinstance(snapshot, Snapshot) or not isinstance(bounds, SnapshotBounds):
        raise CandidateError("invalid_snapshot")
    if (not isinstance(snapshot.directory, str) or not snapshot.directory
            or "\x00" in snapshot.directory
            or not all(_digest(v) for v in (snapshot.manifest_sha256,
                                            snapshot.receipt_sha256,
                                            snapshot.scope_sha256))
            or (snapshot.seed_sha256 is not None and not _digest(snapshot.seed_sha256))
            or (snapshot.context_sha256 is not None and not _digest(snapshot.context_sha256))
            or not _counter(snapshot.file_count) or not _counter(snapshot.total_file_bytes)):
        raise CandidateError("tampered")
    _tick(t0, bounds)
    d = snapshot.directory
    dst = _lstat(d)
    if dst is None or not _is_plain(dst, "dir"):
        raise CandidateError("tampered")
    mb = _read_small(os.path.join(d, "manifest.json"), bounds.max_manifest_bytes)
    rb = _read_small(os.path.join(d, "receipt.json"), 4096)
    if _sha(mb) != snapshot.manifest_sha256 or _sha(rb) != snapshot.receipt_sha256:
        raise CandidateError("tampered")
    m, r = _strict_json(mb), _strict_json(rb)
    keys = {"schema", "manifest_sha256", "scope_sha256", "seed_sha256",
            "git_pointer_sha256", "file_count", "total_file_bytes", "platform"}
    schema = SCHEMA_RECEIPT
    if snapshot.context_sha256 is not None:
        keys.add("context_sha256")
        schema = SCHEMA_RECEIPT_V2
    if (type(r) is not dict or set(r) != keys
            or r["schema"] != schema or r["platform"] != PLATFORM
            or r.get("context_sha256") != snapshot.context_sha256
            or r["manifest_sha256"] != snapshot.manifest_sha256
            or r["seed_sha256"] != snapshot.seed_sha256
            or r["scope_sha256"] != snapshot.scope_sha256
            or (r["git_pointer_sha256"] is not None and not _digest(r["git_pointer_sha256"]))
            or not _counter(r["file_count"]) or not _counter(r["total_file_bytes"])):
        raise CandidateError("tampered")
    try:
        canonical = _canon(m) == mb and _canon(r) == rb
    except (ValueError, TypeError, UnicodeError, RecursionError, OverflowError):
        raise CandidateError("tampered") from None
    if not canonical:
        raise CandidateError("tampered")
    entries, files, total = _validate_manifest(m, bounds, t0)
    if _sha(_canon(m["inputs"])) != snapshot.scope_sha256:
        raise CandidateError("tampered")
    got = (files, total)
    if got != (snapshot.file_count, snapshot.total_file_bytes) or got != (
            r.get("file_count"), r.get("total_file_bytes")):
        raise CandidateError("tampered")
    if files and not _is_plain(_lstat(os.path.join(d, "blobs")), "dir"):
        raise CandidateError("tampered")
    if verify_blobs:
        for e in entries.values():
            _tick(t0, bounds)
            if e["type"] != "file":
                continue
            bp = os.path.join(d, "blobs", e["sha256"])
            st = _lstat(bp)
            if not _is_plain(st, "file") or st.st_size != e["size"]:
                raise CandidateError("tampered")
            try:
                sha, size = _hash_file(bp, st, bounds, t0=t0)
            except (CandidateError, OSError):
                raise CandidateError("tampered") from None
            if (sha, size) != (e["sha256"], e["size"]):
                raise CandidateError("tampered")
            _tick(t0, bounds)
    return m, r, entries


def _seal(stage, mb, scope, seed, git_sha, files, total, context_sha256):
    receipt = {"schema": SCHEMA_RECEIPT, "manifest_sha256": _sha(mb), "scope_sha256": scope,
               "seed_sha256": seed, "git_pointer_sha256": git_sha, "file_count": files,
               "total_file_bytes": total, "platform": PLATFORM}
    if context_sha256 is not None:
        receipt["schema"] = SCHEMA_RECEIPT_V2
        receipt["context_sha256"] = context_sha256
    rb = _canon(receipt)
    stage.write("manifest.json", mb)
    stage.write("receipt.json", rb)
    stage.publish()
    return Snapshot(stage.dest, _sha(mb), _sha(rb), scope, files, total, seed, context_sha256)


def identity(root, *, bounds, inputs, owner_guard, operational_roots=(),
             git_admin=None, strict_layout=True):
    """Bounded read-only data identity; no transport publication or authority."""
    if type(strict_layout) is not bool:
        raise CandidateError("invalid_layout")
    t0 = time.monotonic()
    sels = _inputs(inputs, bounds, t0)
    _, ops = _operational(root, sels, operational_roots, bounds, t0)
    _guard(owner_guard)
    _git_guard(git_admin)
    try:
        _, manifest, _, _, _ = _build(root, sels, bounds, t0, git_admin,
                                     strict_layout=strict_layout, operational_roots=ops)
    except OSError:
        raise CandidateError("io_error") from None
    _git_guard(git_admin)
    _guard(owner_guard)
    _tick(t0, bounds)
    return _sha(manifest)


def open_snapshot(directory, *, bounds, manifest_sha256, receipt_sha256,
                  scope_sha256, seed_sha256, context_sha256=None):
    """Open a transport using protected caller pins, not self-hash authority."""
    t0 = time.monotonic()
    if not isinstance(bounds, SnapshotBounds):
        raise CandidateError("invalid_bounds")
    directory = os.fspath(directory)
    if not _is_plain(_lstat(directory), "dir"):
        raise CandidateError("tampered")
    raw = _read_small(os.path.join(directory, "receipt.json"), 4096)
    if not _digest(receipt_sha256) or _sha(raw) != receipt_sha256:
        raise CandidateError("tampered")
    receipt = _strict_json(raw)
    if type(receipt) is not dict:
        raise CandidateError("tampered")
    snapshot = Snapshot(directory, manifest_sha256, receipt_sha256, scope_sha256,
                        receipt.get("file_count"), receipt.get("total_file_bytes"), seed_sha256,
                        context_sha256)
    _load(snapshot, bounds, t0)
    return snapshot


def validate_owned_layout(root, inputs, *, bounds, owner_guard,
                          operational_roots=(), git_admin=None):
    """Owned-layout observation, not authority or an OS filesystem sandbox."""
    identity(root, bounds=bounds, inputs=inputs, owner_guard=owner_guard,
             operational_roots=operational_roots, git_admin=git_admin, strict_layout=True)


def capture(root, destination, *, bounds, inputs, owner_guard, seed_sha256=None,
            operational_roots=(), git_admin=None, strict_layout=False, context_sha256=None):
    if not isinstance(bounds, SnapshotBounds):
        raise CandidateError("invalid_bounds")
    if type(strict_layout) is not bool:
        raise CandidateError("invalid_layout")
    if seed_sha256 is not None and not (isinstance(seed_sha256, str) and _SHA.fullmatch(seed_sha256)):
        raise CandidateError("invalid_seed")
    if context_sha256 is not None and not _digest(context_sha256):
        raise CandidateError("invalid_args")
    t0 = time.monotonic()
    sels = _inputs(inputs, bounds, t0)
    nroot, ops = _operational(root, sels, operational_roots, bounds, t0)
    nd = _physical(destination)
    if _inside(nd, nroot) or _inside(nroot, nd):
        raise CandidateError("destination_unsafe")
    _git_guard(git_admin)
    stage = None
    try:
        stage = _Stage(destination, owner_guard, bounds)
        _, mb, git_sha, files, total = _build(
            root, sels, bounds, t0, git_admin, stage,
            strict_layout=strict_layout, operational_roots=ops)
        _git_guard(git_admin)
        _tick(t0, bounds)
        return _seal(stage, mb, _sha(_canon(list(sels))), seed_sha256, git_sha, files, total,
                     context_sha256)
    except OSError:
        raise CandidateError("io_error") from None
    finally:
        if stage is not None:
            stage.cleanup()


def _build_tree(snapshot_dir, entries, destination, owner_guard, bounds, t0, inputs, expected_sha):
    """Construct a fresh tree byte-for-byte from verified blobs; publish atomically."""
    stage = None
    try:
        stage = _Stage(destination, owner_guard, bounds)
        for rel in sorted(entries):  # sorted => parents before children
            e = entries[rel]
            target = os.path.join(stage.path, *rel.split("/"))
            os.makedirs(os.path.dirname(target), exist_ok=True)  # structural parents
            if e["type"] == "dir":
                os.makedirs(target, exist_ok=True)
                continue
            bp = os.path.join(snapshot_dir, "blobs", e["sha256"])
            st = _lstat(bp)
            if st is None or not _is_plain(st, "file"):
                raise CandidateError("tampered")
            sha, size = _hash_file(bp, st, bounds, out=target, t0=t0)
            if (sha, size) != (e["sha256"], e["size"]):
                raise CandidateError("tampered")
            if e.get("exec") is not None:
                os.chmod(target, 0o755 if e["exec"] else 0o644)
            _tick(t0, bounds)
        _guard(owner_guard)
        if _sha(_build(stage.path, tuple(inputs), bounds, t0, None,
                       strict_layout=True)[1]) != expected_sha:
            raise CandidateError("tampered")  # verify staged identity BEFORE publication
        stage.publish()
    except OSError:
        raise CandidateError("io_error") from None
    finally:
        if stage is not None:
            stage.cleanup()


def materialize(snapshot, destination, *, bounds, owner_guard):
    """Verify the sealed snapshot, build a fresh tree, re-verify its identity, publish."""
    t0 = time.monotonic()
    m, _, entries = _load(snapshot, bounds, t0)
    _build_tree(snapshot.directory, entries, destination, owner_guard, bounds, t0,
                m["inputs"], snapshot.manifest_sha256)
    return dataclasses.replace(snapshot, directory=os.path.abspath(os.fspath(destination)))


def _key(e):
    return None if e is None else tuple(sorted(e.items()))


def delta(seed, result, *, bounds):
    """Closed changes (path, old_entry, new_entry) from seed to result, sorted by path."""
    t0 = time.monotonic()
    _, _, a = _load(seed, bounds, t0, verify_blobs=False)
    _, _, b = _load(result, bounds, t0, verify_blobs=False)
    return tuple((p, a.get(p), b.get(p)) for p in sorted(set(a) | set(b))
                 if _key(a.get(p)) != _key(b.get(p)))


def _in_scope(path, scopes):
    return any(s == "." or path == s or path.startswith(s + "/") for s in scopes)


def _related(p, q):
    return p == q or q.startswith(p + "/") or p.startswith(q + "/")


def merge(seed, results, destination, *, bounds, allowed_write_paths, owner_guard,
          context_sha256=None):
    """Merge seed->result deltas; reject scope violations, stale seeds and conflicts.

    Publishes a full merged snapshot; carries no acceptance bit and never advances the seed.
    """
    if context_sha256 is not None and not _digest(context_sha256):
        raise CandidateError("invalid_args")
    t0 = time.monotonic()
    scopes = _inputs(allowed_write_paths, bounds, t0)
    _guard(owner_guard)
    sm, _, base = _load(seed, bounds, t0)
    changes = {}  # path -> (branch index, new entry, blob dir)
    additions = 0
    for i, res in enumerate(results):
        _tick(t0, bounds)
        if not isinstance(res, Snapshot) or res.seed_sha256 != seed.manifest_sha256:
            raise CandidateError("stale_seed")
        rm, _, branch = _load(res, bounds, t0)
        if rm["inputs"] != sm["inputs"]:
            raise CandidateError("scope_violation")
        for path in sorted(set(base) | set(branch)):
            _tick(t0, bounds)
            new = branch.get(path)
            if _key(base.get(path)) == _key(new):
                continue
            if not _in_scope(path, scopes):
                raise CandidateError("scope_violation")
            if path not in base and path not in changes and new is not None:
                additions += 1
                if additions > bounds.max_entries:
                    raise CandidateError("bound_entries")
            for q, (j, other, _d) in changes.items():
                _tick(t0, bounds)
                if j == i or not _related(path, q):
                    continue
                if path == q:
                    compatible = _key(other) == _key(new)
                elif path.startswith(q + "/"):
                    # A shared directory creation permits independent child changes.
                    # A deletion/type replacement must agree on the ancestor itself.
                    compatible = _key(branch.get(q)) == _key(other)
                else:
                    previous = changes[path][1] if path in changes else base.get(path)
                    compatible = _key(previous) == _key(new)
                if not compatible:
                    raise CandidateError("merge_conflict")
            changes.setdefault(path, (i, new, res.directory))
    merged, blob_src = dict(base), {}
    for path, (_i, new, d) in changes.items():
        _tick(t0, bounds)
        if new is None:
            merged.pop(path, None)
        else:
            merged[path] = new
            if new["type"] == "file":
                blob_src[new["sha256"]] = d
    if len(merged) > bounds.max_entries:
        raise CandidateError("bound_entries")
    manifest = {"schema": SCHEMA_MANIFEST, "platform": PLATFORM, "inputs": sm["inputs"],
                "entries": [merged[p] for p in sorted(merged)]}
    try:
        _, files, total = _validate_manifest(manifest, bounds, t0)
    except CandidateError as e:
        if e.code == "tampered":
            raise CandidateError("merge_conflict") from None
        raise
    for e in merged.values():
        _tick(t0, bounds)
        if e["type"] == "file":
            blob_src.setdefault(e["sha256"], seed.directory)
    mb = _canon(manifest)
    if len(mb) > bounds.max_manifest_bytes:
        raise CandidateError("bound_manifest")
    stage = None
    try:
        stage = _Stage(destination, owner_guard, bounds)
        for sha, d in sorted(blob_src.items()):
            bp = os.path.join(d, "blobs", sha)
            st = _lstat(bp)
            if st is None or not _is_plain(st, "file") or stage.add(bp, st, t0)[0] != sha:
                raise CandidateError("tampered")
            _tick(t0, bounds)
        snapshot = _seal(stage, mb, seed.scope_sha256, seed.manifest_sha256, None,
                         files, total, context_sha256)
        # Unacknowledged publication is not a journal anchor or worker authority.
        _guard(owner_guard)
        _load(snapshot, bounds, t0)
        _guard(owner_guard)
        _tick(t0, bounds)
        return snapshot
    except OSError:
        raise CandidateError("io_error") from None
    finally:
        if stage is not None:
            stage.cleanup()

"""Trusted protocol-2 DATA identity seam; no automatic routing or acceptance.

Caller supplies a pinned, validated Manifest2 and a protected ownership guard.
This is not an OS filesystem sandbox: approved programs/Git and exclusive owned
administration remain preconditions. A digest or a role string grants nothing.
"""
import contextlib
import hashlib
import hmac
import os
import re
import stat
import subprocess
import threading

import candidate
from supervisor_manifest_v2 import PROFILE, SCHEMA
from supervisor_policy import ManifestError, git_environment

_FAILURE = "candidate identity verification failed"
_MANIFEST_FAILURE = "identity provider requires a validated worker manifest"
_METADATA_BYTES = 4096
_COMMIT = re.compile(r"[0-9a-f]{40}")
_QUERIES = {"common": ("rev-parse", "--path-format=absolute", "--git-common-dir"),
            "directory": ("rev-parse", "--path-format=absolute", "--git-dir"),
            "head": ("rev-parse", "--verify", "--quiet", "HEAD^{commit}")}


class _Refused(Exception):
    pass


def _require(condition):
    if not condition:
        raise _Refused()


def _canonical(path):
    path = os.fspath(path)
    _require(isinstance(path, str) and "\x00" not in path and os.path.isabs(path))
    return os.path.normcase(os.path.normpath(path))


def _inside(path, parent):
    return path == parent or path.startswith(parent.rstrip(os.sep) + os.sep)


def _owner(guard):
    _require(guard() is True)


def _plain(st, directory=False):
    return not getattr(st, "st_file_attributes", 0) & 0x400 and (
        stat.S_ISDIR(st.st_mode) if directory else stat.S_ISREG(st.st_mode) and st.st_nlink == 1)


def _plain_directories(path):
    path = _canonical(path)
    _require(_canonical(os.path.realpath(path)) == path)
    current = path
    while True:
        _require(_plain(os.lstat(current), directory=True))
        parent = os.path.dirname(current)
        if parent == current:
            return
        current = parent


def _stamp(st):
    return (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns,
            st.st_mode, st.st_nlink)


def _small(path):
    _plain_directories(os.path.dirname(path))
    before = os.lstat(path)
    _require(_plain(before) and before.st_size <= _METADATA_BYTES)
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0))
    try:
        handle_before = os.fstat(fd)
        a, b = _stamp(handle_before), _stamp(before)
        # Windows path-stat and handle-stat expose different ctime representations.
        # Compare all common identity fields across APIs, then each full stamp to
        # itself after the read so neither change-time observation is discarded.
        _require(_plain(handle_before) and a[:4] + a[5:] == b[:4] + b[5:])
        chunks, size = [], 0
        while size <= _METADATA_BYTES:
            chunk = os.read(fd, _METADATA_BYTES + 1 - size)
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
        _require(size <= _METADATA_BYTES and size == before.st_size)
        _require(_stamp(os.fstat(fd)) == _stamp(handle_before))
    finally:
        os.close(fd)
    _require(_stamp(os.lstat(path)) == _stamp(before))
    return b"".join(chunks)


def _line(data):
    text = data.decode("utf-8", errors="strict")
    _require(text.endswith("\n") and text.count("\n") == 1)
    value = text[:-1]
    _require(bool(value) and value == value.strip())
    _require(not any(ord(c) < 32 or ord(c) == 127 for c in value))
    return value


def _resolved(base, value):
    return _canonical(value if os.path.isabs(value) else os.path.join(base, value))


def _git(root, query):
    argv = ["git", "-c", "core.hooksPath=", "-c", "core.fsmonitor=false", *_QUERIES[query]]
    proc = subprocess.Popen(argv, cwd=root, env=git_environment({"GIT_OPTIONAL_LOCKS": "0"}),
                            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL, bufsize=0)
    output = []

    def collect():
        try:
            chunks, size = [], 0
            while size <= _METADATA_BYTES:
                block = os.read(proc.stdout.fileno(), _METADATA_BYTES + 1 - size)
                if not block:
                    output.append(b"".join(chunks))
                    return
                chunks.append(block)
                size += len(block)
            output.append(None)
            with contextlib.suppress(OSError):
                proc.kill()
        except (OSError, ValueError):
            output.append(None)

    reader = threading.Thread(target=collect, daemon=True)
    try:
        reader.start()
        code = proc.wait(timeout=10)
        reader.join(timeout=1)
        _require(not reader.is_alive() and code == 0)
        _require(len(output) == 1 and isinstance(output[0], bytes))
        return _line(output[0])
    finally:
        if proc.poll() is None:
            with contextlib.suppress(OSError):
                proc.kill()
        with contextlib.suppress(subprocess.TimeoutExpired, OSError):
            proc.wait(timeout=1)
        if reader.ident is not None:
            reader.join(timeout=1)
        proc.stdout.close()


def _layout(run, root):
    _plain_directories(root)
    _require(os.path.dirname(root) == run)
    run_st, root_st = os.lstat(run), os.lstat(root)
    return run_st.st_dev, run_st.st_ino, root_st.st_dev, root_st.st_ino


def _administration(policy, root, baseline, owner_guard):
    _owner(owner_guard)
    # Direct protected-parent calls use native path spelling, while callbacks
    # already use canonical spelling. Compare both sides in one lexical form.
    policy, root = _canonical(policy), _canonical(root)
    _plain_directories(policy)
    _plain_directories(root)
    pointer_path = os.path.join(root, ".git")
    pointer = _small(pointer_path)
    pointer_line = _line(pointer)
    _require(pointer_line.startswith("gitdir: "))
    value = pointer_line[len("gitdir: "):]
    _require(bool(value) and value == value.strip())
    gitdir = _resolved(root, value)
    common = _canonical(_git(policy, "common"))
    _plain_directories(common)
    worktrees = os.path.join(common, "worktrees")
    _require(gitdir != worktrees and _inside(gitdir, worktrees))
    _plain_directories(gitdir)
    paths = tuple(os.path.join(gitdir, name) for name in ("HEAD", "commondir", "gitdir"))
    metadata = tuple(_small(path) for path in paths)
    head_text, common_text, backlink = tuple(_line(data) for data in metadata)
    _require(_resolved(gitdir, common_text) == common)
    _require(os.path.isabs(backlink) and _canonical(backlink) == pointer_path)
    _require(_COMMIT.fullmatch(head_text) is not None
             or re.fullmatch(r"ref: refs/[^\s]+", head_text) is not None)

    def unchanged():
        _owner(owner_guard)
        _plain_directories(root)
        _plain_directories(gitdir)
        _require(_small(pointer_path) == pointer)
        _require(tuple(_small(path) for path in paths) == metadata)

    unchanged()
    actual_directory = _canonical(_git(root, "directory"))
    unchanged()
    _require(actual_directory == gitdir)
    actual_common = _canonical(_git(root, "common"))
    unchanged()
    _require(actual_common == common)
    actual_head = _git(root, "head")
    unchanged()
    _require(actual_head == baseline)
    _owner(owner_guard)
    return (hashlib.sha256(pointer).hexdigest(), gitdir, common, actual_head,
            tuple(hashlib.sha256(data).hexdigest() for data in metadata))


def identity_provider2(manifest, *, owner_guard):
    """Internal caller must provide a pinned launch-validated immutable Manifest2.

    Structural/alias-safe protocol checks cannot authenticate provenance. Factory
    does no I/O; callback refuses foreign path arguments before filesystem probes.
    """
    try:
        _require(not isinstance(manifest, dict) and callable(owner_guard))
        _require(manifest.schema == SCHEMA and manifest.profile == PROFILE
                 and manifest.role == "worker" and manifest.mcp == "none")
        policy, run = _canonical(manifest.root), _canonical(manifest.run_dir)
        root = _canonical(manifest.candidate_root)
        _require(root == os.path.join(run, "candidate"))
        _require(not _inside(run, policy) and not _inside(policy, run))
        desc, kind = manifest.candidate, manifest.candidate.kind
        _require(kind in ("artifact-manifest", "git"))
        bounds, inputs = candidate.SnapshotBounds(**dict(desc.bounds)), desc.inputs
        baseline = None
        if kind == "git":
            baseline = desc.baseline_commit
            _require(isinstance(baseline, str) and _COMMIT.fullmatch(baseline) is not None)
    except Exception:
        raise ManifestError(_MANIFEST_FAILURE) from None
    pinned_layout = None
    pinned_admin = None

    def observe(requested_root):
        nonlocal pinned_layout, pinned_admin
        try:
            _require(_canonical(requested_root) == root)
            _owner(owner_guard)
            actual_layout = _layout(run, root)
            if pinned_layout is None:
                pinned_layout = actual_layout
            _require(actual_layout == pinned_layout)
            git_admin = None
            if kind == "git":
                state = _administration(policy, root, baseline, owner_guard)
                if pinned_admin is None:
                    pinned_admin = state
                _require(state == pinned_admin)

                def administration_guard():
                    _owner(owner_guard)
                    _require(_layout(run, root) == pinned_layout)
                    _require(_administration(policy, root, baseline, owner_guard) == pinned_admin)
                    _owner(owner_guard)
                    return True

                git_admin = candidate.GitAdmin(state[0], administration_guard)
            result = candidate.identity(root, bounds=bounds, inputs=inputs,
                                         owner_guard=owner_guard, strict_layout=True,
                                         git_admin=git_admin)
            _owner(owner_guard)
            _require(_layout(run, root) == pinned_layout)
            _owner(owner_guard)
            return result
        except Exception:
            raise ManifestError(_FAILURE) from None

    return observe


def verify_seed2(manifest, root, *, owner_guard):
    observed = identity_provider2(manifest, owner_guard=owner_guard)(root)
    try:
        seed = manifest.seed_candidate
        _require(isinstance(seed, str) and re.fullmatch(r"[0-9a-f]{64}", seed) is not None)
        return hmac.compare_digest(observed, seed)
    except Exception:
        raise ManifestError(_FAILURE) from None

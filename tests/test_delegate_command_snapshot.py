"""Command-time candidate snapshots: zero scans by default, bounded when verifying.

Regression: unbounded Runner._tree_digest read 134-208 GB per run around every
disk-mode command. Synthetic files in tmp_path only; no gateway calls.
"""
from __future__ import annotations

import builtins
import os
import subprocess
from types import SimpleNamespace

import pytest

from test_engine_reasoning import engine  # noqa: F401

CHECK = ["python", "-m", "py_compile", "x.py"]
OTHER = ["git", "status"]


@pytest.fixture
def fg(engine):
    return engine.capability_module("finish_gate")


def _command(engine, monkeypatch, rc=0, effect=None):
    calls = []

    def run(argv, **kwargs):
        calls.append(argv)
        if effect is not None:
            effect()
        return subprocess.CompletedProcess(argv, rc, "synthetic out", "synthetic err")

    monkeypatch.setattr(engine, "ALLOWED_CMDS", [CHECK, OTHER])
    monkeypatch.setattr(engine.shutil, "which", lambda name: name)
    monkeypatch.setattr(engine.subprocess, "run", run)
    return calls


def _forbid_scans(engine, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("command-time tree scan or content read")

    monkeypatch.setattr(engine.os, "walk", boom)
    monkeypatch.setattr(engine.os, "scandir", boom)
    monkeypatch.setattr(engine, "open", boom, raising=False)


def _runner(engine, fg, tmp_path, write_mode="disk", plan=True):
    vp = fg.VerificationPlan((tuple(CHECK),)) if plan else None
    return engine.Runner(str(tmp_path), write_mode=write_mode, verification_plan=vp)


def _gate(fg, runner):
    return fg.evaluate_finish(runner.evidence.snapshot(), runner.evidence.plan)


@pytest.mark.parametrize("rc", [0, 3])
@pytest.mark.parametrize("write_mode,plan", [
    ("disk", False), ("stage", False), ("stage", True)])
def test_commands_without_disk_plan_never_scan(
        engine, fg, tmp_path, monkeypatch, rc, write_mode, plan):
    # Production regression: 15-25 min _tree_digest around every --write command.
    with open(tmp_path / "huge.bin", "wb") as fh:
        fh.truncate(64 * 1024 * 1024)  # sparse-ish; must never be opened
    calls = _command(engine, monkeypatch, rc=rc)
    runner = _runner(engine, fg, tmp_path, write_mode, plan)
    try:
        with monkeypatch.context() as m:
            _forbid_scans(engine, m)
            out = runner.run_command(CHECK)
        assert out.startswith(f"exit={rc}\n") and "synthetic out" in out
        assert len(calls) == 1
        assert runner.outcome_unknown is False and runner.caps.outcome_unknown is False
        checks = runner.evidence.snapshot().checks
        assert [(c.outcome, c.exit_code) for c in checks] == [("exited", rc)] * plan
        gate = _gate(fg, runner)
        assert gate.verification_status != "verified_by_command"
        assert "command_outcome_unknown" not in gate.reason_codes
        if write_mode == "disk":
            # unseen disk effects: candidate/review generations are invalidated
            assert runner.candidate_seq == 1
            assert gate.reason_codes == ("candidate_state_unavailable",)
        else:
            # staged candidate is guarded by staged-base validation instead
            assert runner.candidate_seq == 0 and gate.verification_status == "no_mutations"
    finally:
        runner.close()


def test_command_without_finish_gate_module_never_scans(engine, tmp_path, monkeypatch):
    available = engine.capability_module

    def capability_module(name):
        if name == "finish_gate":
            raise engine.CapabilityUnavailable("synthetic missing module")
        return available(name)

    monkeypatch.setattr(engine, "capability_module", capability_module)
    calls = _command(engine, monkeypatch, rc=0)
    runner = engine.Runner(str(tmp_path), write_mode="disk")
    try:
        assert runner.evidence is None
        with monkeypatch.context() as m:
            _forbid_scans(engine, m)
            assert runner.run_command(CHECK).startswith("exit=0\n")
        assert len(calls) == 1 and runner.outcome_unknown is False
        assert runner.candidate_seq == 1
    finally:
        runner.close()


def _small_tree(tmp_path):
    (tmp_path / "x.py").write_text("value = 0\n", encoding="utf-8")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "data.txt").write_text("synthetic data\n", encoding="utf-8")


def test_explicit_check_in_small_tree_still_verifies(engine, fg, tmp_path, monkeypatch):
    _small_tree(tmp_path)
    calls = _command(engine, monkeypatch, rc=0)
    runner = _runner(engine, fg, tmp_path)
    try:
        runner.write_file("x.py", "value = 1\n")
        seq = runner.candidate_seq
        assert runner.run_command(CHECK).startswith("exit=0\n")
        assert runner.run_command(OTHER).startswith("exit=0\n")
        assert len(calls) == 2 and runner.candidate_seq == seq
        assert _gate(fg, runner).verification_status == "verified_by_command"
    finally:
        runner.close()


def _create(p):
    (p / "made.txt").write_text("synthetic new\n", encoding="utf-8")


def _edit(p):
    (p / "sub" / "data.txt").write_text("synthetic DATA\n", encoding="utf-8")


def _delete(p):
    os.remove(p / "sub" / "data.txt")


def _rename(p):
    os.replace(p / "sub" / "data.txt", p / "sub" / "moved.txt")


def _mkdir(p):
    (p / "emptydir").mkdir()


@pytest.mark.parametrize("effect", [_create, _edit, _delete, _rename, _mkdir])
def test_unseen_command_change_invalidates(engine, fg, tmp_path, monkeypatch, effect):
    _small_tree(tmp_path)
    _command(engine, monkeypatch, rc=0, effect=lambda: effect(tmp_path))
    runner = _runner(engine, fg, tmp_path)
    try:
        runner.write_file("x.py", "value = 1\n")
        seq = runner.candidate_seq
        assert runner.run_command(CHECK).startswith("exit=0\n")
        assert runner.candidate_seq == seq + 1 and runner.outcome_unknown is False
        gate = _gate(fg, runner)
        assert gate.verification_status == "unverified_post_edit_mutation"
        assert gate.reason_codes == ("post_check_mutation",)
    finally:
        runner.close()


def test_incidental_change_after_check_invalidates(engine, fg, tmp_path, monkeypatch):
    _small_tree(tmp_path)
    state = {"n": 0}

    def effect():
        state["n"] += 1
        if state["n"] == 2:
            _edit(tmp_path)

    _command(engine, monkeypatch, rc=0, effect=effect)
    runner = _runner(engine, fg, tmp_path)
    try:
        runner.write_file("x.py", "value = 1\n")
        runner.run_command(CHECK)
        runner.run_command(OTHER)
        gate = _gate(fg, runner)
        assert gate.verification_status == "unverified_post_edit_mutation"
        assert gate.reason_codes == ("post_check_mutation",)
    finally:
        runner.close()


def test_snapshot_entry_cap_counts_empty_dirs(engine, tmp_path, monkeypatch):
    for i in range(6):
        (tmp_path / f"d{i}").mkdir()
    monkeypatch.setattr(engine, "SNAPSHOT_MAX_ENTRIES", 5)
    assert engine._candidate_snapshot(str(tmp_path)) is None
    monkeypatch.setattr(engine, "SNAPSHOT_MAX_ENTRIES", 6)
    assert engine._candidate_snapshot(str(tmp_path)) is None
    monkeypatch.setattr(engine, "SNAPSHOT_MAX_ENTRIES", 11)
    assert engine._candidate_snapshot(str(tmp_path)) is None
    # Six initial entries plus six validation visits share one budget.
    monkeypatch.setattr(engine, "SNAPSHOT_MAX_ENTRIES", 12)
    assert isinstance(engine._candidate_snapshot(str(tmp_path)), str)


def test_snapshot_oversized_file_short_circuits_before_open(engine, tmp_path, monkeypatch):
    (tmp_path / "big.bin").write_bytes(b"x" * 100)
    monkeypatch.setattr(engine, "SNAPSHOT_MAX_BYTES", 99)

    def boom(*a, **k):
        raise AssertionError("opened an oversized file")

    monkeypatch.setattr(engine, "open", boom, raising=False)
    assert engine._candidate_snapshot(str(tmp_path)) is None


def test_snapshot_reads_are_chunk_bounded(engine, tmp_path, monkeypatch):
    (tmp_path / "a.bin").write_bytes(b"y" * 1000)
    monkeypatch.setattr(engine, "SNAPSHOT_CHUNK", 64)
    sizes = []
    real_open = builtins.open

    class Spy:
        def __init__(self, fh):
            self.fh = fh

        def read(self, n=-1):
            sizes.append(n)
            return self.fh.read(n)

        def fileno(self):
            return self.fh.fileno()

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            self.fh.close()

    monkeypatch.setattr(engine, "open", lambda *a, **k: Spy(real_open(*a, **k)),
                        raising=False)
    first = engine._candidate_snapshot(str(tmp_path))
    assert isinstance(first, str)
    assert sizes and all(0 < n <= 64 for n in sizes)


@pytest.mark.parametrize("race", ["edit_before_open", "edit_during_read", "replace_after_read"])
def test_snapshot_file_races_are_unavailable(engine, tmp_path, monkeypatch, race):
    path = tmp_path / "a.bin"
    path.write_bytes(b"original")
    initial = path.stat()
    real_open = builtins.open

    def edit():
        path.write_bytes(b"modified")  # same length: byte count alone cannot detect it
        os.utime(path, ns=(initial.st_atime_ns, initial.st_mtime_ns + 2_000_000_000))

    class RacingFile:
        def __init__(self, fh):
            self.fh = fh
            self.changed = False

        def fileno(self):
            return self.fh.fileno()

        def read(self, n):
            data = self.fh.read(n)
            if race == "edit_during_read" and not self.changed:
                self.changed = True
                edit()
            return data

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            self.fh.close()
            if race == "replace_after_read":
                path.unlink()
                path.write_bytes(b"replaced")

    def racing_open(*args, **kwargs):
        if race == "edit_before_open":
            edit()
        return RacingFile(real_open(*args, **kwargs))

    monkeypatch.setattr(engine, "open", racing_open, raising=False)
    assert engine._candidate_snapshot(str(tmp_path)) is None


@pytest.mark.parametrize("where", ["root", "directory", "file"])
def test_snapshot_reparse_attributes_fail_closed(engine, tmp_path, monkeypatch, where):
    _small_tree(tmp_path)
    target = {"root": tmp_path, "directory": tmp_path / "sub",
              "file": tmp_path / "x.py"}[where]
    real_lstat, real_scandir = engine.os.lstat, engine.os.scandir

    def marked(st):
        names = ("st_dev", "st_ino", "st_mode", "st_size", "st_mtime_ns", "st_ctime_ns")
        return SimpleNamespace(**{key: getattr(st, key) for key in names},
                               st_file_attributes=0x400)

    def lstat(path, *args, **kwargs):
        st = real_lstat(path, *args, **kwargs)
        return marked(st) if os.fspath(path) == str(target) else st

    class Entry:
        def __init__(self, entry):
            self.entry = entry

        def __getattr__(self, name):
            return getattr(self.entry, name)

        def stat(self, **kwargs):
            st = self.entry.stat(**kwargs)
            return marked(st) if self.entry.path == str(target) else st

    class Scan:
        def __init__(self, path):
            self.it = real_scandir(path)

        def __enter__(self):
            return (Entry(e) for e in self.it)

        def __exit__(self, *exc):
            self.it.close()

    monkeypatch.setattr(engine.os, "lstat", lstat)
    monkeypatch.setattr(engine.os, "scandir", Scan)
    assert engine._candidate_snapshot(str(tmp_path)) is None


@pytest.mark.parametrize("replace", [False, True])
def test_snapshot_checks_root_and_deadline_after_empty_scandir(
        engine, tmp_path, monkeypatch, replace):
    root = tmp_path / "root"
    root.mkdir()
    real_scandir = engine.os.scandir
    clock = {"t": 0.0}
    monkeypatch.setattr(engine, "time", SimpleNamespace(monotonic=lambda: clock["t"]))

    class Scan:
        def __enter__(self):
            self.it = real_scandir(root)
            return self.it

        def __exit__(self, *exc):
            self.it.close()
            if replace:
                root.rename(tmp_path / "old-root")
                root.mkdir()
            else:
                clock["t"] = 2.0

    monkeypatch.setattr(engine.os, "scandir", lambda path: Scan())
    assert engine._candidate_snapshot(str(root)) is None


@pytest.mark.parametrize("root_link", [False, True])
def test_snapshot_windows_junction_is_unavailable(engine, tmp_path, root_link):
    if os.name != "nt":
        pytest.skip("Windows junction test")
    import _winapi

    target = tmp_path / "target"
    target.mkdir()
    root = tmp_path / "root"
    root.mkdir()
    junction = tmp_path / "junction" if root_link else root / "junction"
    _winapi.CreateJunction(str(target), str(junction))
    try:
        assert engine._candidate_snapshot(str(junction if root_link else root)) is None
    finally:
        os.rmdir(junction)  # remove only the temporary junction, never its target


@pytest.mark.parametrize("race", ["replace_before_scan", "add_during_scan",
                                 "replace_after_scan", "add_after_scan",
                                 "remove_during_scan"])
def test_snapshot_directory_races_are_unavailable(engine, tmp_path, monkeypatch, race):
    root = tmp_path / "root"
    sub = root / "sub"
    leaf = sub / "leaf"
    leaf.mkdir(parents=True)
    real_scandir = engine.os.scandir

    class Scan:
        def __init__(self, path):
            self.path = os.path.normpath(path)
            self.it = real_scandir(path)

        def __enter__(self):
            return self.it

        def __exit__(self, *exc):
            self.it.close()
            if ((race == "replace_before_scan" and self.path == str(root))
                    or (race == "replace_after_scan" and self.path == str(sub))):
                sub.rename(tmp_path / "old-sub")
                sub.mkdir()
            elif race == "add_during_scan" and self.path == str(leaf):
                (leaf / "late.txt").write_bytes(b"unseen")
            elif race == "add_after_scan" and self.path == str(leaf):
                initial = sub.stat()
                (sub / "late.txt").write_bytes(b"unseen")
                os.utime(sub, ns=(initial.st_atime_ns, initial.st_mtime_ns + 2_000_000_000))
            elif race == "remove_during_scan" and self.path == str(leaf):
                leaf.rmdir()

    monkeypatch.setattr(engine.os, "scandir", Scan)
    assert engine._candidate_snapshot(str(root)) is None


def test_snapshot_detects_member_added_with_frozen_dir_metadata(
        engine, tmp_path, monkeypatch):
    root = tmp_path / "root"
    leaf = root / "sub" / "leaf"
    leaf.mkdir(parents=True)
    (leaf / "a.txt").write_bytes(b"a")
    real_scandir, real_lstat = engine.os.scandir, engine.os.lstat
    frozen, injected = {}, []

    def lstat(path, *a, **k):
        key = os.path.normpath(os.fspath(path))
        if key == str(leaf):  # directory identity metadata never changes
            return frozen.setdefault(key, real_lstat(path, *a, **k))
        return real_lstat(path, *a, **k)

    class Scan:
        def __init__(self, path):
            self.path = os.path.normpath(os.fspath(path))
            self.it = real_scandir(path)

        def __enter__(self):
            return self.it

        def __exit__(self, *exc):
            self.it.close()
            if self.path == str(leaf) and not injected:
                injected.append(True)
                (leaf / "late.txt").write_bytes(b"unseen")

    monkeypatch.setattr(engine.os, "lstat", lstat)
    monkeypatch.setattr(engine.os, "scandir", Scan)
    assert engine._candidate_snapshot(str(root)) is None
    assert injected
    # Unchanged trees still produce a stable digest under the same wrappers.
    first = engine._candidate_snapshot(str(root))
    assert first is not None and first == engine._candidate_snapshot(str(root))


@pytest.mark.parametrize("fault", ["error", "deadline"])
def test_snapshot_validation_pass_faults_are_unavailable(
        engine, tmp_path, monkeypatch, fault):
    root = tmp_path / "root"
    (root / "sub").mkdir(parents=True)
    (root / "sub" / "a.txt").write_bytes(b"a")
    real_scandir = engine.os.scandir
    clock, calls = {"t": 0.0}, []
    monkeypatch.setattr(engine, "time", SimpleNamespace(monotonic=lambda: clock["t"]))

    def scandir(path):
        calls.append(os.path.normpath(os.fspath(path)))
        if calls.count(str(root / "sub")) == 2:  # validation pass, not the scan
            if fault == "error":
                raise PermissionError("denied")
            clock["t"] = 2.0
        return real_scandir(path)

    monkeypatch.setattr(engine.os, "scandir", scandir)
    assert engine._candidate_snapshot(str(root)) is None
    assert calls.count(str(root / "sub")) == 2


def test_snapshot_uses_fresh_path_metadata(engine, tmp_path, monkeypatch):
    _small_tree(tmp_path)
    real_scandir = engine.os.scandir

    class Entry:
        def __init__(self, entry):
            self.entry = entry

        def __getattr__(self, name):
            return getattr(self.entry, name)

        def stat(self, **kwargs):
            cached = self.entry.stat(**kwargs)
            # Windows directory entries do not provide stable inode/device IDs.
            return SimpleNamespace(st_dev=0, st_ino=0, st_mode=cached.st_mode,
                                   st_size=cached.st_size, st_mtime_ns=cached.st_mtime_ns,
                                   st_ctime_ns=cached.st_ctime_ns,
                                   st_file_attributes=getattr(cached, "st_file_attributes", 0))

    class Scan:
        def __init__(self, path):
            self.it = real_scandir(path)

        def __enter__(self):
            return (Entry(e) for e in self.it)

        def __exit__(self, *exc):
            self.it.close()

    monkeypatch.setattr(engine.os, "scandir", Scan)
    assert isinstance(engine._candidate_snapshot(str(tmp_path)), str)


def test_snapshot_time_cap(engine, tmp_path, monkeypatch):
    _small_tree(tmp_path)
    ticks = iter(range(0, 10_000, 5))

    class Clock:
        @staticmethod
        def monotonic():
            return float(next(ticks))

    monkeypatch.setattr(engine, "time", Clock)
    assert engine._candidate_snapshot(str(tmp_path)) is None


def test_snapshot_unreadable_or_vanished_file_is_unavailable(engine, tmp_path, monkeypatch):
    _small_tree(tmp_path)

    def gone(*a, **k):
        raise FileNotFoundError("synthetic vanished")

    monkeypatch.setattr(engine, "open", gone, raising=False)
    assert engine._candidate_snapshot(str(tmp_path)) is None


def test_snapshot_link_is_unavailable(engine, tmp_path):
    _small_tree(tmp_path)
    try:
        os.symlink(tmp_path / "sub", tmp_path / "link", target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable on this host")
    assert engine._candidate_snapshot(str(tmp_path)) is None


def test_snapshot_skips_only_vcs_and_engine_state(engine, tmp_path):
    _small_tree(tmp_path)
    base = engine._candidate_snapshot(str(tmp_path))
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "HEAD").write_text("synthetic\n", encoding="utf-8")
    (tmp_path / ".claude").mkdir()
    assert engine._candidate_snapshot(str(tmp_path)) == base
    (tmp_path / "node_modules").mkdir()  # policy skip dir is still candidate state
    assert engine._candidate_snapshot(str(tmp_path)) != base


@pytest.mark.parametrize("rc", [0, 4])
@pytest.mark.parametrize("fail_at", [1, 2])
def test_unavailable_snapshot_never_verifies(engine, fg, tmp_path, monkeypatch, rc, fail_at):
    _small_tree(tmp_path)
    calls = _command(engine, monkeypatch, rc=rc)
    real = engine._candidate_snapshot
    seen = []

    def snap(root):
        seen.append(root)
        return None if len(seen) == fail_at else real(root)

    runner = _runner(engine, fg, tmp_path)
    try:
        runner.write_file("x.py", "value = 1\n")
        monkeypatch.setattr(engine, "_candidate_snapshot", snap)
        out = runner.run_command(CHECK)
        assert out.startswith(f"exit={rc}\n") and len(calls) == 1
        assert len(seen) == fail_at  # no wasted post-scan after a failed pre-scan
        assert runner.outcome_unknown is False
        check = runner.evidence.snapshot().checks[-1]
        assert (check.outcome, check.exit_code) == ("exited", rc)
        monkeypatch.setattr(engine, "_candidate_snapshot", real)
        runner.run_command(CHECK)  # a later clean check cannot resurrect evidence
        gate = _gate(fg, runner)
        assert gate.verification_status == "unverified_post_edit_mutation"
        assert "candidate_state_unavailable" in gate.reason_codes
        assert "command_outcome_unknown" not in gate.reason_codes
    finally:
        runner.close()

"""Protocol-2 atomic Job containment and exact birth codec; synthetic, tmp_path only."""
import os
import sys
import uuid

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from engine import supervisor_process as sp  # noqa: E402

PY = sys.executable
fakeable = pytest.mark.skipif(not hasattr(sp, "_SIEX"), reason="ctypes Windows structures absent")


def _name():
    return "ACv3-" + uuid.uuid4().hex[:12]


def test_birth3_roundtrips_exact_64bit_above_float_precision():
    for value in (1, 2**53 + 1, 133_000_000_000_000_001, 2**64 - 1):
        text = sp.encode_birth3(value)
        assert text == "win:%d" % value and sp.decode_birth3(text) == value


@pytest.mark.parametrize("bad", [True, False, 1.0, -1, 0, 2**64, "1", None])
def test_encode_birth3_rejects_non_canonical_values(bad):
    with pytest.raises(sp.ProcessError) as err:
        sp.encode_birth3(bad)
    assert err.value.reason == "invalid_birth"


@pytest.mark.parametrize("bad", [
    "win:", "win:01", "win:-1", "win:+1", "win: 1", "win:1 ", "x win:1", "WIN:1",
    "win:0", "win:%d" % 2**64, "win:1.0", "win:\u0661", "SECRET-user-data", 5, b"win:1", None])
def test_decode_birth3_rejects_without_echoing_input(bad):
    with pytest.raises(sp.ProcessError) as err:
        sp.decode_birth3(bad)
    assert err.value.reason == "invalid_birth" and str(err.value) == "invalid_birth"


class FakeK:
    """Records kernel calls; `fail` names the one call that should fail."""

    def __init__(self, fail=None, in_job=True):
        self.fail, self.in_job, self.calls = fail, in_job, []

    def _rec(self, name, *args):
        self.calls.append((name,) + args)
        return name != self.fail

    def names(self):
        return [c[0] for c in self.calls]

    def InitializeProcThreadAttributeList(self, buf, count, flags, size):
        ok = self._rec("Init", count, buf is not None)
        return ok if buf is not None else False  # sizing probe always "fails"

    def UpdateProcThreadAttribute(self, attrs, flags, attr, value, size, prev, ret):
        self.calls.append(("Update", attr))
        return not (self.fail == ("Update", attr))

    def DeleteProcThreadAttributeList(self, attrs):
        self._rec("Delete")

    def CreateProcessW(self, app, line, pa, ta, inherit, flags, env, cwd, si, pi):
        if not self._rec("Create", flags):
            return False
        pi._obj.hProcess, pi._obj.hThread, pi._obj.dwProcessId = 501, 502, 4242
        return True

    def AssignProcessToJobObject(self, job, h):
        return self._rec("Assign", h)

    def IsProcessInJob(self, h, job, res):
        if not self._rec("InJob", h, job):
            return False
        res._obj.value = 1 if self.in_job else 0
        return True

    def GetCurrentProcess(self):
        return -1

    def DuplicateHandle(self, src, h, dst, out, access, inherit, opts):
        return self._rec("Dup", dst, access)

    def GetProcessTimes(self, h, c, e, k, u):
        if not self._rec("Times", h):
            return False
        c._obj.dwHighDateTime, c._obj.dwLowDateTime = 0x1D9_0000, 0xFFFF_FFFF
        return True

    def TerminateProcess(self, h, code):
        return self._rec("TermProc", h)

    def TerminateJobObject(self, job, code):
        return self._rec("TermJob", job)

    def WaitForSingleObject(self, h, ms):
        self._rec("Wait", h)
        return 0

    def ResumeThread(self, h):
        self._rec("Resume", h)
        return 1

    def CloseHandle(self, h):
        return self._rec("Close", h)


JOB_LIST, HANDLE_LIST = 0x2000D, 0x20002


def _fake_job(monkeypatch, fake, created=True):
    monkeypatch.setattr(sp, "_api", lambda: (fake, None))
    job = sp.WindowsJob(77, "fake-job-name")
    if created:
        job._created_here = True
    return job


def _spawn(job, fake, atomic):
    return job._spawn(fake, 77, "x", "A=b\0\0", None, [11, 12, 13], atomic=atomic)


@fakeable
def test_atomic_uses_two_attributes_and_never_assigns_or_resumes(monkeypatch):
    fake = FakeK()
    child = _spawn(_fake_job(monkeypatch, fake), fake, atomic=True)
    names = fake.names()
    assert ("Init", 2, True) in fake.calls
    assert ("Update", HANDLE_LIST) in fake.calls and ("Update", JOB_LIST) in fake.calls
    create = [c for c in fake.calls if c[0] == "Create"][0]
    assert create[1] & 0x4 and not create[1] & 0x01000000  # SUSPENDED, no BREAKAWAY
    assert names.index("Create") < names.index("InJob") < names.index("Times")
    assert "Assign" not in names and "Resume" not in names
    assert (child.pid, child.birth) == (4242, (0x1D9_0000 << 32) | 0xFFFF_FFFF)


@fakeable
def test_legacy_spawn_keeps_one_attribute_and_assign(monkeypatch):
    fake = FakeK()
    _spawn(_fake_job(monkeypatch, fake), fake, atomic=False)
    assert ("Init", 1, True) in fake.calls and ("Update", JOB_LIST) not in fake.calls
    assert "Assign" in fake.names() and "InJob" not in fake.names()


@fakeable
def test_unsupported_job_list_fails_before_create_without_fallback(monkeypatch):
    fake = FakeK(fail=("Update", JOB_LIST))
    with pytest.raises(sp.ProcessError) as err:
        _spawn(_fake_job(monkeypatch, fake), fake, atomic=True)
    assert err.value.reason == "atomic_job_unavailable"
    names = fake.names()
    assert "Create" not in names and "Assign" not in names and "Resume" not in names
    assert "Delete" in names


@fakeable
@pytest.mark.parametrize("fail,in_job,reason,created", [
    ("Init", True, "atomic_job_unavailable", False),
    (("Update", HANDLE_LIST), True, "spawn_failed", False),
    ("Create", True, "spawn_failed", False),
    ("InJob", True, "identity_unverified", True),
    (None, False, "identity_unverified", True),
    ("Dup", True, "assign_failed", True),
    ("Times", True, "identity_unverified", True),
])
def test_atomic_failure_matrix_cleans_up_and_never_resumes(monkeypatch, fail, in_job,
                                                           reason, created):
    fake = FakeK(fail=fail, in_job=in_job)
    with pytest.raises(sp.ProcessError) as err:
        _spawn(_fake_job(monkeypatch, fake), fake, atomic=True)
    assert err.value.reason == reason
    names = fake.names()
    assert "Resume" not in names and "Assign" not in names and "TermJob" not in names
    if "Init" != fail:
        assert "Delete" in names
    if created:  # only the new suspended child is killed, then both handles closed
        assert ("TermProc", 501) in fake.calls
        assert ("Close", 501) in fake.calls and ("Close", 502) in fake.calls
    else:
        assert "TermProc" not in names


@fakeable
def test_atomic_requires_job_created_by_this_process(monkeypatch):
    fake = FakeK()
    job = _fake_job(monkeypatch, fake, created=False)
    with pytest.raises(sp.ProcessError) as err:
        job.create_suspended_atomic([PY], cwd=None, env={}, stdout_path=os.devnull,
                                    stderr_path=os.devnull)
    assert err.value.reason == "not_owner" and fake.calls == []


def test_atomic_supported_is_a_bool_and_launches_nothing():
    assert sp.WindowsJob.atomic_supported() in (True, False)
    if sys.platform != "win32":
        assert sp.WindowsJob.atomic_supported() is False


native = pytest.mark.skipif(
    sys.platform != "win32" or not sp.WindowsJob.atomic_supported(),
    reason="native atomic Job association unavailable on this host")
MARK = "import sys;open(sys.argv[1],'w').write('syn-ok')"
# Leader starts an owned sleeper (self-ends after 60 s), records its pid, exits at once.
LEADER = ("import subprocess,sys;p=subprocess.Popen([sys.executable,'-c',"
          "'import time;time.sleep(60)']);open(sys.argv[1],'w').write(str(p.pid))")


def _env(tmp_path):
    keep = {k: os.environ[k] for k in ("SYSTEMROOT", "SystemRoot") if k in os.environ}
    return dict(keep, TEMP=str(tmp_path), TMP=str(tmp_path))


@native
def test_real_atomic_child_is_member_before_resume_and_runs_only_after(tmp_path):
    marker = tmp_path / "m.txt"
    job = sp.WindowsJob.create(_name())
    child = None
    try:
        child = job.create_suspended_atomic(
            [PY, "-I", "-c", MARK, str(marker)], cwd=str(tmp_path), env=_env(tmp_path),
            stdout_path=str(tmp_path / "o"), stderr_path=str(tmp_path / "e"))
        pid, birth = child.pid, sp.decode_birth3(sp.encode_birth3(child.birth))
        assert job.contains(pid, birth) and not marker.exists()
        assert not job.contains(pid, birth + 1)  # wrong birth never grants identity
        assert child.wait(0.3) is None and not marker.exists()
        child.resume()
        assert child.wait(30) == 0 and marker.read_text() == "syn-ok"
        assert job.wait_empty(10)
    finally:
        if child is not None:
            child.close()
        job.terminate(lambda: True, timeout=10)
        job.close()


@native
def test_real_group_survives_leader_exit_by_kernel_count(tmp_path):
    out = tmp_path / "pid.txt"
    name = _name()
    job = sp.WindowsJob.create(name)
    child = again = None
    try:
        child = job.create_suspended_atomic(
            [PY, "-I", "-c", LEADER, str(out)], cwd=str(tmp_path), env=_env(tmp_path),
            stdout_path=str(tmp_path / "o"), stderr_path=str(tmp_path / "e"))
        pid, birth = child.pid, child.birth
        child.resume()
        assert child.wait(30) == 0 and out.exists()
        again = sp.WindowsJob.open_existing(name)
        assert again.active_count() > 0 and not again.is_empty()
        assert not again.contains(pid, birth)  # dead leader is not required for the query
    finally:
        if child is not None:
            child.close()
        job.terminate(lambda: True, timeout=10)
        assert job.wait_empty(10)
        if again is not None:
            again.close()
        job.close()


def test_missing_job_is_never_reported_empty():
    if sys.platform != "win32":
        pytest.skip("Windows Job Objects absent")
    with pytest.raises(sp.ProcessError) as err:
        sp.WindowsJob.open_existing(_name())
    assert err.value.reason == "job_missing"

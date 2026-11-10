"""Real Windows Job containment tests; synthetic children under tmp_path only."""
import json
import os
import sys
import uuid

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from engine import supervisor_process as sp  # noqa: E402

win = pytest.mark.skipif(sys.platform != "win32", reason="Windows Job Objects")
PY = sys.executable
WRITE = ("import json,os,sys;open(sys.argv[1],'w',encoding='utf-8').write("
         "json.dumps([sys.argv[2:],os.environ.get('SYN_VAR')]));print('out-ok');"
         "print('err-ok',file=sys.stderr)")


def _name():
    return "AgentControlTest-" + uuid.uuid4().hex


@win
def test_suspended_child_runs_only_after_resume(tmp_path):
    work = tmp_path / "d ü space"
    work.mkdir()
    marker = work / "mark ü.json"
    args = ["a b", 'q"x', "back\\\\", "", "ü"]
    env = dict(os.environ, SYN_VAR="syn-ü")
    job = sp.WindowsJob.create(_name())
    child = job.create_suspended([PY, "-c", WRITE, str(marker), *args], cwd=str(work), env=env,
                                 stdout_path=str(work / "o.txt"), stderr_path=str(work / "e.txt"))
    try:
        assert child.pid > 0 and child.birth
        assert child.wait(0.3) is None and not marker.exists()
        child.resume()
        assert child.wait(30) == 0
        assert json.loads(marker.read_text(encoding="utf-8")) == [args, "syn-ü"]
        assert (work / "o.txt").read_text().strip() == "out-ok"
        assert (work / "e.txt").read_text().strip() == "err-ok"
        assert job.wait_empty(10)
    finally:
        child.close()
        job.close()
    child.close()  # idempotent


# Waits for a marker file (bounded to 60 s so a leaked process self-ends).
WAITER = ("import os,sys,time;e=time.time()+60\n"
          "while not os.path.exists(sys.argv[1]) and time.time()<e: time.sleep(0.05)")
# Root starts a grandchild waiter, records its pid, and exits immediately.
ROOT_SPAWN = ("import subprocess,sys;p=subprocess.Popen([sys.executable,'-c',sys.argv[1],"
              "sys.argv[2]]);open(sys.argv[3],'w').write(str(p.pid))")


def _count_settles(job, want, timeout=10):
    import time
    end = time.monotonic() + timeout
    while job.active_count() != want and time.monotonic() < end:
        time.sleep(0.02)
    return job.active_count() == want


def _name_gone(name, timeout=10):
    import time
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        try:
            sp.WindowsJob.open_existing(name).close()
        except sp.ProcessError as exc:
            return exc.reason == "job_missing"
        time.sleep(0.02)
    return False


def _spawn_tree(job, tmp_path, release):
    out = tmp_path / "gpid.txt"
    child = job.create_suspended(
        [PY, "-c", ROOT_SPAWN, WAITER, str(release), str(out)], cwd=str(tmp_path),
        env=dict(os.environ), stdout_path=str(tmp_path / "o"), stderr_path=str(tmp_path / "e"))
    child.resume()
    assert child.wait(30) == 0
    return child, int(out.read_text())


@win
def test_root_exit_with_grandchild_is_not_quiescent_and_cancel_is_scoped(tmp_path):
    import subprocess
    release = tmp_path / "never"
    unrelated = subprocess.Popen([PY, "-c", WAITER, str(release)])
    job = sp.WindowsJob.create(_name())
    try:
        child, _ = _spawn_tree(job, tmp_path, release)
        child.close()
        before = job.active_count()  # grandchild (+ possibly its console host)
        assert before >= 1 and not job.is_empty()
        with pytest.raises(sp.ProcessError) as err:
            job.terminate(lambda: False, timeout=5)
        assert err.value.reason == "not_owner" and job.active_count() == before
        result = job.terminate(lambda: True, timeout=10)
        assert result == {"outcome": "exited", "active_processes": 0}
        assert job.is_empty()
        assert unrelated.poll() is None
    finally:
        job.close()
        unrelated.kill()
        unrelated.wait(10)


# Synthetic owner: creates the Job, starts a waiter, records identity, then crashes.
OWNER = ("import json,os,sys;sys.path.insert(0,sys.argv[1]);"
         "from engine import supervisor_process as sp;"
         "j=sp.WindowsJob.create(sys.argv[2]);"
         "c=j.create_suspended([sys.executable,'-c',sys.argv[3],sys.argv[4]],cwd=sys.argv[5],"
         "env=dict(os.environ),stdout_path=os.devnull,stderr_path=os.devnull);"
         "open(sys.argv[6],'w').write(json.dumps([c.pid,c.birth]));c.resume();os._exit(7)")


@win
def test_owner_crash_then_reopen_reattaches_and_missing_is_never_recreated(tmp_path):
    import subprocess
    name, ident, release = _name(), tmp_path / "id.json", tmp_path / "never"
    owner = subprocess.run([PY, "-c", OWNER, ROOT, name, WAITER, str(release), str(tmp_path),
                            str(ident)], timeout=60)
    assert owner.returncode == 7
    pid, birth = json.loads(ident.read_text())
    assert sp.process_birth(pid) == birth  # contained waiter outlived its owner
    job = sp.WindowsJob.open_existing(name)
    try:
        assert job.active_count() >= 1 and job.contains(pid, birth)
        assert not job.contains(pid, birth + 1)  # PID alone is not identity
        assert sp.process_birth(pid) == birth
        assert job.terminate(lambda: True, timeout=10)["outcome"] == "exited"
    finally:
        job.close()
    assert _name_gone(name)  # last handle (held by the dead child) released
    with pytest.raises(sp.ProcessError) as err:
        sp.WindowsJob.open_existing(name)  # still missing: nothing was recreated
    assert err.value.reason == "job_missing"


@win
def test_collision_invalid_input_and_failed_assignment(tmp_path):
    name = _name()
    job = sp.WindowsJob.create(name)
    try:
        with pytest.raises(sp.ProcessError) as err:
            sp.WindowsJob.create(name)
        assert err.value.reason == "job_exists"
        kw = dict(cwd=str(tmp_path), stdout_path=os.devnull, stderr_path=os.devnull)
        for bad in ([], "python", [PY, 3], [PY, "a\0b"], [""]):
            with pytest.raises(sp.ProcessError) as err:
                job.create_suspended(bad, env=dict(os.environ), **kw)
            assert err.value.reason == "invalid_argv"
        with pytest.raises(sp.ProcessError) as err:
            job.create_suspended([PY, "-c", "0"], env={"A=B": "1"}, **kw)
        assert err.value.reason == "invalid_env"
        # Real kernel refusal: a query-only handle lacks JOB_OBJECT_ASSIGN_PROCESS.
        query_only = sp.WindowsJob(sp._api()[0].OpenJobObjectW(0x0004, False, name), name)
        marker = tmp_path / "m"
        with pytest.raises(sp.ProcessError) as err:
            query_only.create_suspended([PY, "-c", WRITE, str(marker)], env=dict(os.environ), **kw)
        assert err.value.reason == "assign_failed"
        query_only.close()
        assert not marker.exists() and job.is_empty()
    finally:
        job.close()
    with pytest.raises(sp.ProcessError) as err:
        sp.WindowsJob.open_existing("AgentControlTest-missing-" + uuid.uuid4().hex)
    assert err.value.reason == "job_missing"


@win
def test_job_dacl_is_owner_only_and_no_handles_leak(tmp_path):
    import ctypes
    from ctypes import wintypes as W
    job = sp.WindowsJob.create(_name())
    try:
        a = ctypes.WinDLL("advapi32", use_last_error=True)
        need, buf = W.DWORD(), ctypes.create_string_buffer(4096)
        assert a.GetKernelObjectSecurity(W.HANDLE(job._h), 4, buf, 4096, ctypes.byref(need))
        text = W.LPWSTR()
        assert a.ConvertSecurityDescriptorToStringSecurityDescriptorW(
            buf, 1, 4, ctypes.byref(text), None)
        dacl = text.value
        assert dacl.startswith("D:P") and "WD" not in dacl and "BU" not in dacl and "AU" not in dacl
        # An unrelated inheritable parent handle must not reach the child.
        leak = open(tmp_path / "leak", "wb")
        import msvcrt
        h = msvcrt.get_osfhandle(leak.fileno())
        os.set_handle_inheritable(h, True)
        probe = ("import ctypes,sys;f=ctypes.c_ulong();"
                 "sys.exit(3 if ctypes.windll.kernel32.GetHandleInformation(int(sys.argv[1]),"
                 "ctypes.byref(f)) else 0)")
        child = job.create_suspended([PY, "-c", probe, str(h)], cwd=str(tmp_path),
                                     env=dict(os.environ), stdout_path=os.devnull,
                                     stderr_path=os.devnull)
        child.resume()
        assert child.wait(30) == 0
        child.close()
        leak.close()
    finally:
        job.close()


def test_non_windows_fails_closed(monkeypatch):
    monkeypatch.setattr(sp.sys, "platform", "linux")
    for call in (lambda: sp.WindowsJob.create(_name()),
                 lambda: sp.WindowsJob.open_existing(_name()),
                 lambda: sp.process_birth(1)):
        with pytest.raises(sp.ProcessError) as err:
            call()
        assert err.value.reason == "unavailable"


@win
def test_close_without_kill_keeps_job_reopenable(tmp_path):
    release = tmp_path / "go"
    name = _name()
    job = sp.WindowsJob.create(name)
    child = job.create_suspended([PY, "-c", WAITER, str(release)], cwd=str(tmp_path),
                                 env=dict(os.environ), stdout_path=os.devnull,
                                 stderr_path=os.devnull)
    child.resume()
    again = sp.WindowsJob.open_existing(name)
    job.close()  # simulated owner exit
    try:
        assert child.poll() is None and again.contains(child.pid, child.birth)
        release.write_text("x")
        assert child.wait(30) == 0 and again.wait_empty(10)
    finally:
        child.close()
        again.close()

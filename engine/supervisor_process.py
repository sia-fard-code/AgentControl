"""Windows named Job Object containment for supervised children (stdlib only).

Children start suspended, join a non-breakaway named Job before any child code
runs, and are resumed only by an explicit caller step. Closing a Job handle
never kills its processes, so a restarted owner can reopen the name and
reattach. Emptiness is the kernel's ActiveProcesses count. Non-Windows hosts
import safely but every containment operation raises ProcessError(unavailable).
"""
import ctypes
import os
import re
import subprocess
import sys
import time

_REASONS = frozenset({
    "unavailable", "job_exists", "job_missing", "job_open_failed", "job_create_failed",
    "invalid_argv", "invalid_env", "spawn_failed", "assign_failed", "resume_failed",
    "not_owner", "identity_unverified", "terminate_failed", "query_failed", "closed",
    "atomic_job_unavailable", "invalid_birth",
})


class ProcessError(Exception):
    """Failure with a fixed reason code; never carries OS or caller text."""

    def __init__(self, reason):
        self.reason = reason if reason in _REASONS else "query_failed"
        super().__init__(self.reason)


_API = None

if sys.platform == "win32":
    from ctypes import wintypes as W

    class _SA(ctypes.Structure):
        _fields_ = [("nLength", W.DWORD), ("lpSecurityDescriptor", ctypes.c_void_p),
                    ("bInheritHandle", W.BOOL)]

    class _SI(ctypes.Structure):
        _fields_ = [("cb", W.DWORD), ("lpReserved", W.LPWSTR), ("lpDesktop", W.LPWSTR),
                    ("lpTitle", W.LPWSTR)] + [(n, W.DWORD) for n in (
                        "dwX", "dwY", "dwXSize", "dwYSize", "dwXCountChars",
                        "dwYCountChars", "dwFillAttribute", "dwFlags")] + [
                    ("wShowWindow", W.WORD), ("cbReserved2", W.WORD),
                    ("lpReserved2", ctypes.c_void_p), ("hStdInput", W.HANDLE),
                    ("hStdOutput", W.HANDLE), ("hStdError", W.HANDLE)]

    class _SIEX(ctypes.Structure):
        _fields_ = [("StartupInfo", _SI), ("lpAttributeList", ctypes.c_void_p)]

    class _PI(ctypes.Structure):
        _fields_ = [("hProcess", W.HANDLE), ("hThread", W.HANDLE),
                    ("dwProcessId", W.DWORD), ("dwThreadId", W.DWORD)]

    class _ACCT(ctypes.Structure):  # JOBOBJECT_BASIC_ACCOUNTING_INFORMATION
        _fields_ = [(n, ctypes.c_longlong) for n in ("u", "k", "pu", "pk")] + [
            (n, W.DWORD) for n in ("faults", "total", "active", "terminated")]


def _api():
    """Lazily bind kernel32/advapi32; unavailable off Windows."""
    global _API
    if sys.platform != "win32":
        raise ProcessError("unavailable")
    if _API is None:
        k = ctypes.WinDLL("kernel32", use_last_error=True)
        a = ctypes.WinDLL("advapi32", use_last_error=True)
        H, D, B, P = W.HANDLE, W.DWORD, W.BOOL, ctypes.c_void_p
        for dll, name, res, args in (
            (k, "CreateJobObjectW", H, (P, W.LPCWSTR)),
            (k, "OpenJobObjectW", H, (D, B, W.LPCWSTR)),
            (k, "AssignProcessToJobObject", B, (H, H)),
            (k, "TerminateJobObject", B, (H, W.UINT)),
            (k, "QueryInformationJobObject", B, (H, ctypes.c_int, P, D, P)),
            (k, "IsProcessInJob", B, (H, H, ctypes.POINTER(B))),
            (k, "CreateProcessW", B, (W.LPCWSTR, W.LPWSTR, P, P, B, D, P, W.LPCWSTR, P, P)),
            (k, "InitializeProcThreadAttributeList", B, (P, D, D, ctypes.POINTER(ctypes.c_size_t))),
            (k, "UpdateProcThreadAttribute", B, (P, D, ctypes.c_size_t, P, ctypes.c_size_t, P, P)),
            (k, "DeleteProcThreadAttributeList", None, (P,)),
            (k, "ResumeThread", D, (H,)),
            (k, "TerminateProcess", B, (H, W.UINT)),
            (k, "WaitForSingleObject", D, (H, D)),
            (k, "GetExitCodeProcess", B, (H, ctypes.POINTER(D))),
            (k, "GetProcessTimes", B, (H,) + (ctypes.POINTER(W.FILETIME),) * 4),
            (k, "GetCurrentProcess", H, ()),
            (k, "OpenProcess", H, (D, B, D)),
            (k, "CloseHandle", B, (H,)),
            (k, "DuplicateHandle", B, (H, H, H, ctypes.POINTER(H), D, B, D)),
            (k, "LocalFree", P, (P,)),
            (a, "OpenProcessToken", B, (H, D, ctypes.POINTER(H))),
            (a, "GetTokenInformation", B, (H, ctypes.c_int, P, D, ctypes.POINTER(D))),
            (a, "ConvertSidToStringSidW", B, (P, ctypes.POINTER(W.LPWSTR))),
            (a, "ConvertStringSecurityDescriptorToSecurityDescriptorW", B,
             (W.LPCWSTR, D, ctypes.POINTER(P), P)),
        ):
            fn = getattr(dll, name)
            fn.restype, fn.argtypes = res, args
        _API = (k, a)
    return _API


_JOB_ALL = 0x1F001F
_QUERY_LIMITED, _SYNCHRONIZE = 0x1000, 0x00100000
_STILL_ACTIVE, _WAIT_TIMEOUT = 259, 0x102
_ALREADY_EXISTS, _NOT_FOUND = 183, 2


def _close(h):
    if h:
        _api()[0].CloseHandle(h)


def _birth(h):
    """Exact kernel creation FILETIME (100 ns ticks) of a process handle."""
    t = [W.FILETIME() for _ in range(4)]
    if not _api()[0].GetProcessTimes(h, *[ctypes.byref(x) for x in t]):
        raise ProcessError("query_failed")
    return (t[0].dwHighDateTime << 32) | t[0].dwLowDateTime


def process_birth(pid):
    """Exact kernel birth of a live pid, or None if it cannot be opened."""
    h = _api()[0].OpenProcess(_QUERY_LIMITED, False, int(pid))
    if not h:
        return None
    try:
        return _birth(h)
    finally:
        _close(h)


_BIRTH3 = re.compile(r"win:([1-9][0-9]{0,19})")


def encode_birth3(value):
    """Exact FILETIME birth as canonical 'win:<decimal>'; ticks exceed journal 2**53 ints."""
    if type(value) is not int or not 0 < value < 2**64:
        raise ProcessError("invalid_birth")
    return "win:%d" % value


def decode_birth3(text):
    """Inverse of encode_birth3; anything non-canonical is rejected without echo."""
    m = _BIRTH3.fullmatch(text) if type(text) is str else None
    if m is None or int(m.group(1)) >= 2**64:
        raise ProcessError("invalid_birth")
    return int(m.group(1))


def _owner_sa():
    """SECURITY_ATTRIBUTES granting Job access only to the current user and SYSTEM."""
    k, a = _api()
    tok, size = W.HANDLE(), W.DWORD()
    if not a.OpenProcessToken(k.GetCurrentProcess(), 0x0008, ctypes.byref(tok)):  # TOKEN_QUERY
        raise ProcessError("job_create_failed")
    try:
        buf = ctypes.create_string_buffer(256)
        if not a.GetTokenInformation(tok, 1, buf, 256, ctypes.byref(size)):  # TokenUser
            raise ProcessError("job_create_failed")
        sid_text = W.LPWSTR()
        if not a.ConvertSidToStringSidW(ctypes.c_void_p.from_buffer(buf).value,
                                        ctypes.byref(sid_text)):
            raise ProcessError("job_create_failed")
        sddl = "D:P(A;;GA;;;%s)(A;;GA;;;SY)" % sid_text.value
        k.LocalFree(sid_text)
    finally:
        _close(tok)
    sd = ctypes.c_void_p()
    if not a.ConvertStringSecurityDescriptorToSecurityDescriptorW(sddl, 1, ctypes.byref(sd), None):
        raise ProcessError("job_create_failed")
    return _SA(ctypes.sizeof(_SA), sd, False), sd


def _cmdline(argv):
    if (not isinstance(argv, (list, tuple)) or not argv or not argv[0]
            or not all(isinstance(x, str) and "\0" not in x for x in argv)):
        raise ProcessError("invalid_argv")
    return subprocess.list2cmdline(argv)  # MS C runtime quoting rules


def _env_block(env):
    if not isinstance(env, dict):
        raise ProcessError("invalid_env")
    items = []
    for key, val in env.items():
        if (not isinstance(key, str) or not isinstance(val, str) or not key
                or "=" in key[1:] or "\0" in key + val):
            raise ProcessError("invalid_env")
        items.append("%s=%s" % (key, val))
    return "\0".join(sorted(items, key=str.upper)) + "\0\0"


class WindowsChild:
    """One contained process; identity is the retained handle plus pid and birth."""

    def __init__(self, process_handle, thread_handle, pid, birth):
        self._h, self._thread = process_handle, thread_handle
        self.pid, self.birth = pid, birth

    def _handle(self):
        if not self._h:
            raise ProcessError("closed")
        return self._h

    def resume(self):
        """Start the main thread exactly once; the caller persists identity first."""
        h = self._handle()
        if not self._thread:
            raise ProcessError("resume_failed")
        if _api()[0].ResumeThread(self._thread) == 0xFFFFFFFF:
            raise ProcessError("resume_failed")
        _close(self._thread)
        self._thread = None
        return h is not None

    def poll(self):
        """Exit code, or None while the exact process object is still running."""
        k = _api()[0]
        code = W.DWORD()
        if k.WaitForSingleObject(self._handle(), 0) == _WAIT_TIMEOUT:
            return None
        if not k.GetExitCodeProcess(self._h, ctypes.byref(code)):
            raise ProcessError("query_failed")
        return code.value

    def wait(self, timeout):
        """Bounded kernel wait; returns exit code or None on timeout."""
        ms = max(0, int(timeout * 1000))
        if _api()[0].WaitForSingleObject(self._handle(), ms) == _WAIT_TIMEOUT:
            return None
        return self.poll()

    def close(self):
        """Release handles without affecting the process (idempotent)."""
        _close(self._thread)
        _close(self._h)
        self._h = self._thread = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


class WindowsJob:
    """A nonce-named Job; closing the handle never kills its processes."""

    def __init__(self, handle, name):
        self._h, self.name = handle, name
        self._created_here = False  # set only by create(); open_existing never sets it

    @staticmethod
    def atomic_supported():
        """Read-only host check for PROC_THREAD_ATTRIBUTE_JOB_LIST (Windows 10+); launches nothing."""
        if sys.platform != "win32" or sys.getwindowsversion().major < 10:
            return False
        try:
            _api()
        except (ProcessError, OSError, AttributeError):
            return False
        return True

    @staticmethod
    def _valid_name(name):
        if not isinstance(name, str) or not 8 <= len(name) <= 200 or "\\" in name or "\0" in name:
            raise ProcessError("job_create_failed")
        return name

    @classmethod
    def create(cls, unique_nonce_name):
        """Create a new owner-only Job; fail if the name already exists."""
        k = _api()[0]
        name = cls._valid_name(unique_nonce_name)
        sa, sd = _owner_sa()
        try:
            h = k.CreateJobObjectW(ctypes.byref(sa), name)
            err = ctypes.get_last_error()
        finally:
            k.LocalFree(sd)
        if not h:
            raise ProcessError("job_create_failed")
        if err == _ALREADY_EXISTS:  # opened someone's Job: never adopt it as new
            _close(h)
            raise ProcessError("job_exists")
        job = cls(h, name)
        job._created_here = True
        return job

    @classmethod
    def open_existing(cls, name):
        """Reattach by name; a missing Job is reported, never recreated."""
        k = _api()[0]
        h = k.OpenJobObjectW(_JOB_ALL, False, cls._valid_name(name))
        if not h:
            raise ProcessError("job_missing" if ctypes.get_last_error() == _NOT_FOUND
                               else "job_open_failed")
        return cls(h, name)

    def _handle(self):
        if not self._h:
            raise ProcessError("closed")
        return self._h

    def active_count(self):
        """Kernel ActiveProcesses for this Job, including all descendants."""
        info = _ACCT()
        if not _api()[0].QueryInformationJobObject(self._handle(), 1, ctypes.byref(info),
                                                   ctypes.sizeof(info), None):
            raise ProcessError("query_failed")
        return info.active

    def is_empty(self):
        return self.active_count() == 0

    def wait_empty(self, timeout):
        """Poll the kernel count until zero or timeout; the count lags handle signal briefly."""
        end = time.monotonic() + max(0.0, timeout)
        while not self.is_empty():
            if time.monotonic() >= end:
                return False
            time.sleep(0.02)
        return True

    def terminate(self, owner_guard, *, timeout):
        """Kill every process in this owned Job if owner_guard() is exactly True.

        Returns observed facts: outcome is "exited" (kernel count reached zero
        in time) or "active" (survivors remain); never inferred from intent.
        """
        job = self._handle()
        if owner_guard() is not True:
            raise ProcessError("not_owner")
        if not _api()[0].TerminateJobObject(job, 1):
            raise ProcessError("terminate_failed")
        empty = self.wait_empty(timeout)
        return {"outcome": "exited" if empty else "active",
                "active_processes": self.active_count()}

    def create_suspended(self, argv, *, cwd, env, stdout_path, stderr_path, stdin_path=None):
        """Create a suspended child already inside this Job; caller must resume()."""
        return self._create(argv, cwd, env, stdout_path, stderr_path, stdin_path, False)

    def create_suspended_atomic(self, argv, *, cwd, env, stdout_path, stderr_path,
                                stdin_path=None):
        """Protocol-2: child joins this Job at CreateProcess (JOB_LIST); caller must resume().

        Only for a Job created by this process. Unsupported association fails closed with
        no legacy Assign fallback. Grants no journal/epoch/approval authority by itself.
        """
        if not self._created_here:
            raise ProcessError("not_owner")
        return self._create(argv, cwd, env, stdout_path, stderr_path, stdin_path, True)

    def _create(self, argv, cwd, env, stdout_path, stderr_path, stdin_path, atomic):
        import msvcrt
        k = _api()[0]
        job, line, block = self._handle(), _cmdline(argv), _env_block(env)
        files = []
        try:
            for path, mode in ((stdin_path or os.devnull, "rb"), (stdout_path, "wb"),
                               (stderr_path, "wb")):
                files.append(open(path, mode))
        except (OSError, TypeError, ValueError):
            for f in files:
                f.close()
            raise ProcessError("spawn_failed") from None
        try:
            handles = [msvcrt.get_osfhandle(f.fileno()) for f in files]
            for h in handles:
                os.set_handle_inheritable(h, True)
            child = self._spawn(k, job, line, block, cwd, handles, atomic=atomic)
        finally:
            for f in files:
                f.close()
        return child

    def _spawn(self, k, job, line, block, cwd, handles, atomic=False):
        count = 2 if atomic else 1
        size = ctypes.c_size_t()
        k.InitializeProcThreadAttributeList(None, count, 0, ctypes.byref(size))
        attrs = ctypes.create_string_buffer(size.value)
        if not k.InitializeProcThreadAttributeList(attrs, count, 0, ctypes.byref(size)):
            raise ProcessError("atomic_job_unavailable" if atomic else "spawn_failed")
        try:
            hlist = (W.HANDLE * len(handles))(*handles)
            if not k.UpdateProcThreadAttribute(attrs, 0, 0x20002, hlist,  # HANDLE_LIST
                                               ctypes.sizeof(hlist), None, None):
                raise ProcessError("spawn_failed")
            jlist = (W.HANDLE * 1)(job)
            if atomic and not k.UpdateProcThreadAttribute(attrs, 0, 0x2000D, jlist,  # JOB_LIST
                                                          ctypes.sizeof(jlist), None, None):
                raise ProcessError("atomic_job_unavailable")  # never fall back to Assign
            si = _SIEX()
            si.StartupInfo.cb = ctypes.sizeof(_SIEX)
            si.StartupInfo.dwFlags = 0x100  # STARTF_USESTDHANDLES
            (si.StartupInfo.hStdInput, si.StartupInfo.hStdOutput,
             si.StartupInfo.hStdError) = handles
            si.lpAttributeList = ctypes.cast(attrs, ctypes.c_void_p)
            pi = _PI()
            flags = 0x4 | 0x400 | 0x80000 | 0x08000000  # SUSPENDED|UNICODE_ENV|EXT_SI|NO_WINDOW
            buf = ctypes.create_unicode_buffer(line)
            envbuf = ctypes.create_unicode_buffer(block, len(block))
            if not k.CreateProcessW(None, buf, None, None, True, flags, envbuf, cwd,
                                    ctypes.byref(si), ctypes.byref(pi)):
                raise ProcessError("spawn_failed")
        finally:
            k.DeleteProcThreadAttributeList(attrs)
        if atomic:
            return self._finish_atomic(k, job, pi)
        if not k.AssignProcessToJobObject(job, pi.hProcess):
            # Never leave an uncontained executable child: kill it while still suspended.
            k.TerminateProcess(pi.hProcess, 1)
            k.WaitForSingleObject(pi.hProcess, 5000)
            _close(pi.hThread)
            _close(pi.hProcess)
            raise ProcessError("assign_failed")
        # A Job's name dies with its last handle even while processes remain, so the
        # child holds a SYNCHRONIZE-only Job handle to keep the name reopenable.
        held = W.HANDLE()
        if not k.DuplicateHandle(k.GetCurrentProcess(), job, pi.hProcess, ctypes.byref(held),
                                 _SYNCHRONIZE, False, 0):
            k.TerminateJobObject(job, 1)
            _close(pi.hThread)
            _close(pi.hProcess)
            raise ProcessError("assign_failed")
        try:
            birth = _birth(pi.hProcess)
        except ProcessError:
            k.TerminateJobObject(job, 1)  # unverifiable identity: contain, then fail
            _close(pi.hThread)
            _close(pi.hProcess)
            raise ProcessError("identity_unverified") from None
        return WindowsChild(pi.hProcess, pi.hThread, pi.dwProcessId, birth)

    @staticmethod
    def _finish_atomic(k, job, pi):
        """Verify membership, pin and exact birth of a still-suspended child, or kill it."""
        def abort(reason):
            # Kill only this never-resumed child (other Job members are untouched), reap, close.
            k.TerminateProcess(pi.hProcess, 1)
            k.WaitForSingleObject(pi.hProcess, 5000)
            _close(pi.hThread)
            _close(pi.hProcess)
            raise ProcessError(reason)
        res = W.BOOL()
        if not k.IsProcessInJob(pi.hProcess, job, ctypes.byref(res)) or not res.value:
            abort("identity_unverified")
        held = W.HANDLE()  # same name-pin as legacy; persistence after all handles is not promised
        if not k.DuplicateHandle(k.GetCurrentProcess(), job, pi.hProcess, ctypes.byref(held),
                                 _SYNCHRONIZE, False, 0):
            abort("assign_failed")
        try:
            birth = _birth(pi.hProcess)
        except ProcessError:
            abort("identity_unverified")
        return WindowsChild(pi.hProcess, pi.hThread, pi.dwProcessId, birth)

    def contains(self, pid, birth):
        """True only if pid with this exact birth is alive and in this Job."""
        k = _api()[0]
        job = self._handle()
        h = k.OpenProcess(_QUERY_LIMITED | _SYNCHRONIZE, False, int(pid))
        if not h:
            return False
        try:
            if _birth(h) != birth or k.WaitForSingleObject(h, 0) != _WAIT_TIMEOUT:
                return False
            res = W.BOOL()
            if not k.IsProcessInJob(h, job, ctypes.byref(res)):
                raise ProcessError("query_failed")
            return bool(res.value)
        finally:
            _close(h)

    def close(self):
        """Release the Job handle only; contained processes keep running."""
        _close(self._h)
        self._h = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

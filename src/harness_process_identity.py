"""Read-only Windows process handles and exact platform-scoped birth tokens."""
import math
import os
import sys

_EPOCH_DELTA = 116444736000000000
_ACCESS = 0x1000 | 0x00100000  # QUERY_LIMITED_INFORMATION | SYNCHRONIZE
_kernel = None


def _valid_pid(pid):
    return type(pid) is int and 0 < pid <= 0xffffffff


def _kernel32():
    global _kernel
    if _kernel is None:
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        pft = ctypes.POINTER(wintypes.FILETIME)
        kernel.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.GetProcessTimes.argtypes = (wintypes.HANDLE, pft, pft, pft, pft)
        kernel.GetProcessTimes.restype = wintypes.BOOL
        kernel.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
        kernel.WaitForSingleObject.restype = wintypes.DWORD
        kernel.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
        kernel.GetExitCodeProcess.restype = wintypes.BOOL
        kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
        kernel.CloseHandle.restype = wintypes.BOOL
        _kernel = kernel
    return _kernel


def _times(handle):
    import ctypes
    from ctypes import wintypes
    times = [wintypes.FILETIME() for _ in range(4)]
    if not _kernel32().GetProcessTimes(handle, *[ctypes.byref(t) for t in times]):
        return None
    return [(t.dwHighDateTime << 32) | t.dwLowDateTime for t in times]


class ProcessHandle:
    def __init__(self, handle, pid, creation):
        self._handle, self.pid, self._creation = handle, pid, creation

    @classmethod
    def open(cls, pid, expected_creation_time=None):
        if os.name != "nt" or not _valid_pid(pid):
            return None
        handle = None
        try:
            expected = expected_creation_time
            if expected is not None and (type(expected) not in (int, float) or not math.isfinite(expected)):
                return None
            handle = _kernel32().OpenProcess(_ACCESS, False, pid)
            if not handle:
                return None
            times = _times(handle)
            if times is None or times[0] <= 0:
                raise OSError("process identity unavailable")
            obj, handle = cls(handle, pid, times[0]), None
            if expected is not None and abs(obj.created_epoch - expected) > 0.00001:
                obj.close()
                return None
            return obj
        except Exception:
            if handle:
                try:
                    _kernel32().CloseHandle(handle)
                except Exception:
                    pass
            return None

    @property
    def birth(self):
        return f"winft:{self._creation}"

    @property
    def created_epoch(self):
        return (self._creation - _EPOCH_DELTA) / 1e7

    def poll(self):
        unknown = {"state": "unknown", "exit_code": None, "exit_ticks": None}
        if self._handle is None:
            return unknown
        try:
            import ctypes
            from ctypes import wintypes
            kernel = _kernel32()
            state = kernel.WaitForSingleObject(self._handle, 0)
            if state == 258:
                return {"state": "running", "exit_code": None, "exit_ticks": None}
            if state != 0:
                return unknown
            code = wintypes.DWORD()
            if not kernel.GetExitCodeProcess(self._handle, ctypes.byref(code)):
                return unknown
            times = _times(self._handle)
            ticks = str(times[1]) if times is not None and times[1] > 0 else None
            return {"state": "exited", "exit_code": int(code.value), "exit_ticks": ticks}
        except Exception:
            return unknown

    def close(self):
        handle, self._handle = self._handle, None
        if handle:
            try:
                _kernel32().CloseHandle(handle)
            except Exception:
                pass

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def __del__(self):
        self.close()


def process_birth(pid):
    if not _valid_pid(pid):
        return None
    if os.name == "nt":
        handle = ProcessHandle.open(pid)
        if handle is None:
            return None
        try:
            return handle.birth
        finally:
            handle.close()
    if sys.platform.startswith("linux"):
        try:
            from pathlib import Path
            boot = Path("/proc/sys/kernel/random/boot_id").read_text(encoding="ascii").strip()
            stat = Path(f"/proc/{pid}/stat").read_text(encoding="ascii", errors="replace")
            fields = stat[stat.rindex(")") + 1:].split()
            return f"linux:{boot}:{fields[19]}" if fields[19].isdigit() else None
        except (OSError, ValueError, IndexError):
            return None
    return None


def live_process_birth(pid):
    """A retained Windows process object can exist after exit; birth is not liveness."""
    if not _valid_pid(pid):
        return None
    if os.name == "nt":
        handle = ProcessHandle.open(pid)
        if handle is None:
            return None
        try:
            return handle.birth if handle.poll()["state"] == "running" else None
        finally:
            handle.close()
    if sys.platform.startswith("linux"):
        try:
            from pathlib import Path
            stat = Path(f"/proc/{pid}/stat").read_text(encoding="ascii", errors="replace")
            if stat[stat.rindex(")") + 1:].split()[0] in {"Z", "X"}:
                return None
        except (OSError, ValueError, IndexError):
            return None
        return process_birth(pid)
    return None


def birth_for_child(child):
    """Use the spawned process's held native handle, never a recycled PID lookup."""
    try:
        if not _valid_pid(child.pid):
            return None
        if os.name == "nt":
            handle = getattr(child, "_handle", None)
            if not isinstance(handle, int) or isinstance(handle, bool) or handle <= 0:
                return None
            times = _times(handle)
            return f"winft:{times[0]}" if times and times[0] > 0 else None
        if child.poll() is None:
            value = process_birth(child.pid)
            return value if child.poll() is None else None
    except Exception:
        pass
    return None

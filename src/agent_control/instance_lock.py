from __future__ import annotations

import os
from pathlib import Path
from typing import Optional


class InstanceLockError(RuntimeError):
    """Raised when another process already holds the instance lock."""


class InstanceLock:
    """A non-blocking exclusive lock over ``<state_dir>/serve.lock``.

    The lock is held for the life of the file handle. ``release`` unlocks and
    closes; the object is also a context manager.
    """

    def __init__(self, fd: int, path: Path) -> None:
        self._fd: Optional[int] = fd
        self._path = path

    def release(self) -> None:
        if self._fd is None:
            return
        fd, self._fd = self._fd, None
        try:
            if os.name == "nt":
                import msvcrt

                os.lseek(fd, 0, os.SEEK_SET)
                try:
                    msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
                except OSError:
                    pass
            else:
                import fcntl

                fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)

    def __enter__(self) -> "InstanceLock":
        return self

    def __exit__(self, *exc) -> None:
        self.release()


def acquire_instance_lock(state_dir: Path) -> InstanceLock:
    state_dir = Path(state_dir)
    state_dir.mkdir(parents=True, exist_ok=True)
    lock_path = state_dir / "serve.lock"
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        _take_lock(fd)
    except OSError as exc:
        os.close(fd)
        raise InstanceLockError(
            f"another process holds {lock_path}"
        ) from exc
    try:
        os.lseek(fd, 0, os.SEEK_SET)
        os.ftruncate(fd, 0)
        os.write(fd, f"{os.getpid()}\n".encode("ascii"))
    except OSError:
        pass
    return InstanceLock(fd, lock_path)


def _take_lock(fd: int) -> None:
    if os.name == "nt":
        import msvcrt

        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
    else:
        import fcntl

        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)

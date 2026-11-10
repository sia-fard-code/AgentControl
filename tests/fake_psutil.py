"""A minimal in-memory stand-in for psutil, for process-control tests.

Later agents reuse this exact API, so keep it stable.
"""

from __future__ import annotations


class NoSuchProcess(Exception):
    ...


class AccessDenied(Exception):
    ...


STATUS_ZOMBIE = "zombie"


class FakeProcess:
    def __init__(
        self,
        pid: int,
        cmdline: list[str],
        create_time: float,
        *,
        child_pids: tuple[int, ...] = (),
        survive_terminate: bool = False,
        survive_kill: bool = False,
        deny: bool = False,
        deny_cmdline: bool = False,
        deny_create_time: bool = False,
        deny_children: bool = False,
    ) -> None:
        self.pid = pid
        self._cmdline = list(cmdline)
        self._create_time = create_time
        self._child_pids = tuple(child_pids)
        self._survive_terminate = survive_terminate
        self._survive_kill = survive_kill
        self._deny = deny
        self._deny_cmdline = deny_cmdline
        self._deny_create_time = deny_create_time
        self._deny_children = deny_children
        self.alive = True
        self.owner: "FakePsutil | None" = None

    def cmdline(self) -> list[str]:
        if self._deny_cmdline:
            raise AccessDenied(f"cmdline denied for {self.pid}")
        return list(self._cmdline)

    def create_time(self) -> float:
        if self._deny_create_time:
            raise AccessDenied(f"create_time denied for {self.pid}")
        return self._create_time

    def is_running(self) -> bool:
        return self.alive

    def status(self) -> str:
        return "running"

    def children(self, recursive: bool = False) -> list["FakeProcess"]:
        if self._deny_children:
            raise AccessDenied(f"children denied for {self.pid}")
        assert self.owner is not None
        self.owner.calls.append(f"children:{self.pid}")
        result: list[FakeProcess] = []
        for child_pid in self._child_pids:
            child = self.owner.table.get(child_pid)
            if child is None:
                continue
            result.append(child)
            if recursive:
                result.extend(child.children(recursive=True))
        return result

    def terminate(self) -> None:
        if self._deny:
            raise AccessDenied(f"terminate denied for {self.pid}")
        assert self.owner is not None
        self.owner.calls.append(f"terminate:{self.pid}")
        if not self.alive:
            raise NoSuchProcess(f"no process {self.pid}")
        self.alive = self._survive_terminate

    def kill(self) -> None:
        if self._deny:
            raise AccessDenied(f"kill denied for {self.pid}")
        assert self.owner is not None
        self.owner.calls.append(f"kill:{self.pid}")
        if not self.alive:
            raise NoSuchProcess(f"no process {self.pid}")
        self.alive = self._survive_kill


class FakePsutil:
    NoSuchProcess = NoSuchProcess
    AccessDenied = AccessDenied
    STATUS_ZOMBIE = STATUS_ZOMBIE

    def __init__(self, processes: list[FakeProcess]) -> None:
        self.table: dict[int, FakeProcess] = {}
        for process in processes:
            process.owner = self
            self.table[process.pid] = process
        self.calls: list[str] = []

    def Process(self, pid: int) -> FakeProcess:
        process = self.table.get(pid)
        if process is None or not process.alive:
            raise NoSuchProcess(f"no process {pid}")
        return process

    def wait_procs(self, procs, timeout=None):
        gone: list[FakeProcess] = []
        alive: list[FakeProcess] = []
        for process in procs:
            (alive if process.alive else gone).append(process)
        return gone, alive

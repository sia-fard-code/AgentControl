"""Content-free diagnostics. Every artifact is synthetic and temporary."""
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

MODULE = Path(__file__).resolve().parents[1] / "engine/harness_diagnostics.py"


def load_module():
    assert MODULE.exists(), "shared lifecycle diagnostic module is missing"
    spec = importlib.util.spec_from_file_location("diagnostics_test", MODULE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def rows(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_allowlisted_durable_metadata_only(tmp_path, monkeypatch):
    d = load_module()
    synced = []
    monkeypatch.setattr(d.os, "fsync", lambda fd: synced.append(fd))
    path = tmp_path / "run.diagnostics.jsonl"
    journal = d.Journal(path, "engine", run_id="synthetic-run", launch_id="synthetic-launch")
    assert journal.emit("request_start", attempt=1, timeout_s=300, route="responses",
                        prompt="SYNTHETIC_PRIVATE", stderr="SYNTHETIC_PRIVATE",
                        exception_message="SYNTHETIC_PRIVATE", url="SYNTHETIC_PRIVATE")
    row = rows(path)[0]
    assert row["observer"] == "engine" and row["pid"] == os.getpid()
    assert row["event"] == "request_start" and row["attempt"] == 1
    assert row["timeout_s"] == 300 and synced
    assert "SYNTHETIC_PRIVATE" not in path.read_text(encoding="utf-8")


def test_unknown_enums_and_invalid_numbers_are_not_logged(tmp_path):
    d = load_module()
    path = tmp_path / "run.jsonl"
    journal = d.Journal(path, "engine", run_id="synthetic-run")
    assert not journal.emit("SYNTHETIC_PRIVATE")
    journal.emit("request_error", reason="SYNTHETIC_PRIVATE", status="SYNTHETIC_PRIVATE",
                 elapsed_s=float("nan"), http_status=True, timeout_s=-1,
                 source="SYNTHETIC_PRIVATE", exception_class="bad class / private")
    row = rows(path)[0]
    for field in ("reason", "status", "elapsed_s", "http_status", "timeout_s", "source",
                  "exception_class"):
        assert field not in row


@pytest.mark.parametrize("code", [0, None, -9, 3221225786])
def test_exit_codes_preserve_unknown_zero_and_windows_status(tmp_path, code):
    d = load_module()
    path = tmp_path / "run.jsonl"
    d.Journal(path, "ask", launch_id="synthetic-launch").emit("child_exit", exit_code=code)
    row = rows(path)[0]
    assert row["exit_code"] == code
    assert row.get("windows_status") == (None if code is None else code & 0xffffffff)


def test_failed_write_does_not_raise_or_emit_exception_text(tmp_path, capsys):
    d = load_module()
    journal = d.Journal(tmp_path, "engine")
    assert journal.emit("registered") is False
    assert journal.emit("registered") is False
    error = capsys.readouterr().err
    assert error.count("lifecycle diagnostics unavailable") == 1
    assert "SYNTHETIC_PRIVATE" not in error


def test_reader_is_bounded_filters_identity_and_ignores_partial_append(tmp_path):
    d = load_module()
    path = tmp_path / "run.jsonl"
    d.Journal(path, "engine", run_id="other", launch_id="other", engine_pid=1).emit("registered")
    journal = d.Journal(path, "engine", run_id="synthetic-run", launch_id="synthetic-launch",
                        engine_pid=2)
    journal.emit("request_start", route="responses")
    journal.emit("request_error", reason="disconnect", content="SYNTHETIC_PRIVATE")
    with path.open("a", encoding="utf-8") as handle:
        handle.write('{"event":"child_exit"')
    found = d.read_events(path, run_id="synthetic-run", launch_id="synthetic-launch", engine_pid=2)
    assert [row["event"] for row in found] == ["request_start", "request_error"]
    assert "SYNTHETIC_PRIVATE" not in json.dumps(found)
    assert d.read_events(path, run_id="synthetic-run", engine_pid=999) == []


def test_reader_revalidates_untrusted_journal_values(tmp_path):
    d = load_module()
    path = tmp_path / "run.jsonl"
    path.write_text(json.dumps({"schema": 1, "observer": "engine", "event": "request_error",
                               "run_id": "synthetic-run", "engine_pid": 2,
                               "reason": "SYNTHETIC_PRIVATE", "stderr": "SYNTHETIC_PRIVATE"}) + "\n",
                    encoding="utf-8")
    found = d.read_events(path, run_id="synthetic-run", engine_pid=2)
    assert len(found) == 1
    assert "SYNTHETIC_PRIVATE" not in json.dumps(found)


def test_valid_unicode_run_identity_is_not_silently_dropped(tmp_path):
    d = load_module()
    path = tmp_path / "run.jsonl"
    run_id = "synthetic-نیرو"
    d.Journal(path, "engine", run_id=run_id).emit("registered")
    assert d.read_events(path, run_id=run_id)[0]["run_id"] == run_id


def test_engine_journal_inherits_validated_container_identity_and_native_birth(tmp_path, monkeypatch):
    d = load_module()
    monkeypatch.setenv("KILO_BACKGROUND_PROCESS_ID", "bgp_synthetic")
    monkeypatch.setenv("KILO_LIFECYCLE_OWNER_ID", "owner-synthetic")
    monkeypatch.setenv("KILO_LIFECYCLE_OWNER_PID", "123")
    monkeypatch.setenv("KILO_LIFECYCLE_OWNER_BIRTH", "winft:123")
    path = tmp_path / "engine.jsonl"
    d.Journal(path, "engine", engine_pid=os.getpid()).emit("registered")
    row = rows(path)[0]
    assert row["processID"] == "bgp_synthetic" and row["owner_id"] == "owner-synthetic"
    assert row["owner_pid"] == 123 and row["owner_birth"] == "winft:123"
    assert row["engine_birth"].startswith(("winft:", "linux:"))


def test_runtime_coverage_requires_fresh_matching_process_birth_and_explicit_owner_support(tmp_path, monkeypatch):
    d = load_module()
    monkeypatch.setattr(d, "process_birth", lambda pid: "winft:123")
    patch_id = "sha256:" + "a" * 64
    monkeypatch.setattr(d, "known_patch_id", lambda: patch_id)
    path = tmp_path / "runtime.jsonl"
    journal = d.Journal(path, "kilo_owner")
    journal.emit("owner_started", diagnostics_version=2, owner_id="owner-synthetic", owner_pid=os.getpid(),
                 owner_birth="winft:123", birth="winft:123", runtime_version="7.8.18", pre_kill=True,
                 rollback_hook=True, actual_exit_hook=True, runner_hook=False, patch_id=patch_id)
    result = d.read_coverage(tmp_path, owner_pid=os.getpid(), owner_id="owner-synthetic",
                             probe_birth=lambda pid: "winft:123")
    assert result["owner"] == "active" and result["pre_kill"] is True
    result = d.read_coverage(tmp_path, owner_pid=os.getpid(), owner_id="owner-synthetic",
                             probe_birth=lambda pid: "winft:456")
    assert result["owner"] == "unverified" and result["pre_kill"] is False
    future = d.datetime.now(d.timezone.utc).timestamp() + 100
    result = d.read_coverage(tmp_path, owner_pid=os.getpid(), owner_id="owner-synthetic", now=future,
                             probe_birth=lambda pid: "winft:123")
    assert result["owner"] == "stale" and result["pre_kill"] is False


def test_plugin_presence_is_not_owner_pre_kill_coverage(tmp_path, monkeypatch):
    d = load_module()
    monkeypatch.setattr(d, "process_birth", lambda pid: "winft:123")
    path = tmp_path / "plugin.jsonl"
    d.Journal(path, "kilo").emit("plugin_started", diagnostics_version=2, instanceID="synthetic",
                                birth="winft:123", pre_kill=False)
    result = d.read_coverage(tmp_path, owner_pid=os.getpid(), probe_birth=lambda pid: "winft:123")
    assert result["plugin"] == "active"
    assert result["owner"] == "unavailable" and result["pre_kill"] is False


@pytest.mark.skipif(os.name != "nt", reason="Windows retained process object activation check")
def test_fresh_marker_from_exited_runtime_is_not_active(tmp_path):
    from test_process_identity import identity_module
    d = load_module()
    identity = identity_module()
    child = subprocess.Popen([sys.executable, "-c", "print('ready',flush=True); input()"],
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    handle = None
    try:
        assert child.stdout.readline().strip() == "ready"
        handle = identity.ProcessHandle.open(child.pid)
        assert handle
        row = {"schema": 1, "observer": "kilo_owner", "event": "owner_started", "pid": child.pid,
               "owner_pid": child.pid, "owner_id": "synthetic-owner", "birth": handle.birth,
               "owner_birth": handle.birth, "ts": d.datetime.now(d.timezone.utc).isoformat(),
               "diagnostics_version": 2, "runtime_version": "7.8.18", "pre_kill": True,
               "rollback_hook": True, "actual_exit_hook": True}
        row["patch_id"] = d.known_patch_id()
        (tmp_path / "runtime.jsonl").write_text(json.dumps(row) + "\n", encoding="utf-8")
        assert d.read_coverage(tmp_path, owner_pid=child.pid)["owner"] == "active"
        child.kill()
        child.communicate(timeout=10)
        assert d.read_coverage(tmp_path, owner_pid=child.pid)["owner"] != "active"
    finally:
        if handle:
            handle.close()
        if child.poll() is None:
            child.kill()
        child.communicate(timeout=10)


def test_unrecognized_patch_cannot_claim_owner_coverage(tmp_path, monkeypatch):
    d = load_module()
    monkeypatch.setattr(d, "process_birth", lambda pid: "winft:123")
    monkeypatch.setattr(d, "known_patch_id", lambda: "sha256:" + "a" * 64)
    d.Journal(tmp_path / "owner.jsonl", "kilo_owner").emit("owner_started", diagnostics_version=2,
        owner_id="synthetic", owner_pid=os.getpid(), owner_birth="winft:123", runtime_version="7.8.18",
        pre_kill=True, rollback_hook=True, actual_exit_hook=True, patch_id="sha256:" + "b" * 64)
    result = d.read_coverage(tmp_path, owner_pid=os.getpid(), probe_birth=lambda pid: "winft:123")
    assert result["owner"] == "unsupported" and result["pre_kill"] is False


def test_child_pid_alone_is_not_a_native_spawn_identity(tmp_path):
    d = load_module()
    path = tmp_path / "wrapper.jsonl"
    d.Journal(path, "ask").emit("child_start", child_pid=os.getpid())
    assert rows(path)[0]["child_birth"] is None

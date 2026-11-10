"""An old label match is not proof that a new worker launched or completed."""
import importlib.util
import json
import os
import sys
from pathlib import Path

import pytest

WATCHER = Path(__file__).resolve().parents[1] / "engine/agent_await.py"
LAUNCH_ID = "c356dcf3-66ce-4f96-8952-89584b92db99"


@pytest.fixture
def watcher():
    assert WATCHER.is_file(), "the deployed watcher needs maintained source and regression coverage"
    spec = importlib.util.spec_from_file_location("test_launch_watcher", WATCHER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def watch(watcher, monkeypatch, tmp_path, receipt=None, launch_id=LAUNCH_ID, pid=None, ready_only=False):
    argv = [str(WATCHER), "--repo", str(tmp_path), "--label", "synthetic-worker",
            "--poll-secs", "0", "--appear-secs", "0.01", "--max-mins", "0"]
    if receipt is not None:
        argv += ["--launch-receipt", str(receipt), "--launch-id", launch_id,
                 "--engine-pid", str(pid or os.getpid())]
    if ready_only:
        argv.append("--ready-only")
    monkeypatch.setattr(sys, "argv", argv)
    return watcher.main()


@pytest.fixture
def attempt(tmp_path):
    runs = tmp_path / ".claude/agent-runs"
    runs.mkdir(parents=True)
    transcript = runs / "synthetic-new-run.md"
    transcript.write_text("# synthetic\n[synthetic-model: 0 tool calls]\n", encoding="utf-8")
    index = runs / "agent-runs-index.jsonl"
    start = {"schema": 2, "event": "start", "run_id": "synthetic-new-run",
             "root": str(tmp_path), "engine_pid": os.getpid(), "label": "synthetic-worker",
             "transcript": str(transcript)}
    end = {"schema": 2, "event": "end", "run_id": start["run_id"], "status": "complete",
           "sent": None, "recv": None}
    index.write_text(json.dumps(start) + "\n" + json.dumps(end) + "\n", encoding="utf-8")
    receipt = tmp_path / "attempt.launch.jsonl"
    initial = {"schema": 1, "launch_id": LAUNCH_ID, "engine_pid": os.getpid(),
               "state": "starting", "run_id": None}
    ready = {**initial, "state": "ready", "run_id": start["run_id"],
             "root": str(tmp_path), "label": "synthetic-worker", "transcript": str(transcript),
             "index_paths": [str(index)]}
    receipt.write_text(json.dumps(initial) + "\n" + json.dumps(ready) + "\n", encoding="utf-8")
    return receipt, index, transcript, initial, ready


def test_label_only_old_completion_is_not_verified_success(watcher, monkeypatch, tmp_path, capsys):
    runs = tmp_path / ".claude/agent-runs"
    runs.mkdir(parents=True)
    (runs / "20000101-000000-synthetic-worker.md").write_text(
        "# synthetic old run\n[synthetic-model: 0 tool calls]\n", encoding="utf-8")
    assert watch(watcher, monkeypatch, tmp_path) == 6
    output = capsys.readouterr().out
    assert "UNVERIFIED" in output
    assert "FINISHED -" not in output


def test_dead_run_displays_durable_evidence_without_inventing_cause(
    watcher, monkeypatch, tmp_path, attempt, capsys,
):
    from test_lifecycle_diagnostics import load_module
    receipt, index, transcript, initial, ready = attempt
    index.write_text(index.read_text(encoding="utf-8").splitlines()[0] + "\n", encoding="utf-8")
    before = index.read_bytes()
    journal = load_module().Journal(str(transcript) + ".engine.diagnostics.jsonl", "engine",
                                   run_id=ready["run_id"], launch_id=LAUNCH_ID,
                                   engine_pid=os.getpid())
    journal.emit("request_start", route="responses", timeout_s=300)
    journal.emit("stream_progress", activity="reasoning", events_seen=25)
    monkeypatch.setattr(watcher, "process_alive", lambda pid: False)
    assert watch(watcher, monkeypatch, tmp_path, receipt) == 7
    output = capsys.readouterr().out
    assert "stream_progress" in output and "reasoning" in output
    assert "initiator unknown" in output
    assert index.read_bytes() == before


def test_diagnostics_with_wrong_identity_are_not_reported(
    watcher, monkeypatch, tmp_path, attempt, capsys,
):
    from test_lifecycle_diagnostics import load_module
    receipt, index, transcript, *_ = attempt
    index.write_text(index.read_text(encoding="utf-8").splitlines()[0] + "\n", encoding="utf-8")
    load_module().Journal(str(transcript) + ".engine.diagnostics.jsonl", "engine",
                          run_id="wrong", launch_id=LAUNCH_ID, engine_pid=os.getpid()).emit(
                              "stop_requested", reason="operator_kill")
    monkeypatch.setattr(watcher, "process_alive", lambda pid: False)
    assert watch(watcher, monkeypatch, tmp_path, receipt) == 7
    assert "operator_kill" not in capsys.readouterr().out


def test_matching_kilo_stop_intent_is_correlated_by_launch_pid_and_time(
    watcher, monkeypatch, tmp_path, attempt, capsys,
):
    from test_lifecycle_diagnostics import load_module
    receipt, index, transcript, initial, ready = attempt
    index.write_text(index.read_text(encoding="utf-8").splitlines()[0] + "\n", encoding="utf-8")
    module = load_module()
    module.Journal(str(transcript) + ".engine.diagnostics.jsonl", "engine",
                   run_id=ready["run_id"], launch_id=LAUNCH_ID, engine_pid=os.getpid()).emit("registered")
    directory = tmp_path / ".local/share/kilo/lifecycle"
    directory.mkdir(parents=True)
    journal = module.Journal(directory / "synthetic.jsonl", "kilo")
    journal.emit("background_observed", action="start", child_pid=os.getppid(),
                 processID="bgp_synthetic", instanceID="instance-synthetic", sessionID="ses_synthetic")
    journal.emit("stop_requested", action="stop", reason="explicit_stop",
                 processID="bgp_synthetic", instanceID="instance-synthetic", sessionID="ses_synthetic")
    monkeypatch.setattr(watcher.Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setattr(watcher, "process_alive", lambda pid: False)
    assert watch(watcher, monkeypatch, tmp_path, receipt) == 7
    output = capsys.readouterr().out
    assert "Kilo diagnostic" in output and "explicit_stop" in output


def test_unrelated_kilo_process_is_not_attributed_to_run(
    watcher, monkeypatch, tmp_path, attempt, capsys,
):
    from test_lifecycle_diagnostics import load_module
    receipt, index, transcript, initial, ready = attempt
    index.write_text(index.read_text(encoding="utf-8").splitlines()[0] + "\n", encoding="utf-8")
    module = load_module()
    module.Journal(str(transcript) + ".engine.diagnostics.jsonl", "engine",
                   run_id=ready["run_id"], launch_id=LAUNCH_ID, engine_pid=os.getpid()).emit("registered")
    directory = tmp_path / ".local/share/kilo/lifecycle"
    directory.mkdir(parents=True)
    journal = module.Journal(directory / "synthetic.jsonl", "kilo")
    journal.emit("background_observed", action="start", child_pid=4294967294,
                 processID="bgp_other", instanceID="instance-synthetic")
    journal.emit("stop_requested", action="stop", reason="explicit_stop",
                 processID="bgp_other", instanceID="instance-synthetic")
    monkeypatch.setattr(watcher.Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setattr(watcher, "process_alive", lambda pid: False)
    assert watch(watcher, monkeypatch, tmp_path, receipt) == 7
    assert "explicit_stop" not in capsys.readouterr().out


def test_wrapper_failure_before_receipt_creation_remains_visible(
    watcher, monkeypatch, tmp_path, capsys,
):
    from test_lifecycle_diagnostics import load_module
    receipt = tmp_path / "absent.launch.jsonl"
    load_module().Journal(str(receipt) + ".ask.diagnostics.jsonl", "ask", launch_id=LAUNCH_ID).emit(
        "exception", reason="wrapper_exception", exception_class="OSError")
    monkeypatch.setattr(watcher, "process_alive", lambda pid: False)
    assert watch(watcher, monkeypatch, tmp_path, receipt) == 7
    assert "wrapper_exception" in capsys.readouterr().out


def test_ready_reports_missing_owner_coverage_without_blocking_launch(
    watcher, monkeypatch, tmp_path, attempt, capsys,
):
    receipt, index, transcript, *_ = attempt
    index.write_text(index.read_text(encoding="utf-8").splitlines()[0] + "\n", encoding="utf-8")
    monkeypatch.setattr(watcher, "process_alive", lambda pid: True)
    assert watch(watcher, monkeypatch, tmp_path, receipt, ready_only=True) == 0
    output = capsys.readouterr().out
    assert "owner=unavailable" in output and "pre-kill=unavailable" in output
    assert "READY -" in output


def test_owner_evidence_requires_matching_actor_target_and_patch(watcher, tmp_path, capsys):
    from test_lifecycle_diagnostics import load_module
    d = load_module()
    patch = "sha256:" + "a" * 64
    context = {"observer": "engine", "owner_id": "owner-synthetic", "owner_pid": 321,
               "owner_birth": "winft:123", "processID": "bgp_synthetic", "patch_id": patch,
               "birth": "winft:789", "parent_birth": "winft:456"}
    record = {"schema": 1, "observer": "kilo_owner", "event": "stop_requested",
              "owner_id": "owner-synthetic", "owner_pid": 321, "pid": 321,
              "owner_birth": "winft:123", "birth": "winft:123", "processID": "bgp_synthetic",
              "patch_id": patch, "target_birth": "winft:999", "reason": "explicit_stop"}
    path = tmp_path / "owner.jsonl"
    path.write_text(json.dumps(record) + "\n", encoding="utf-8")
    module = {"diagnostic_directory": lambda: tmp_path, "read_events": d.read_events,
              "known_patch_id": lambda: patch}
    watcher.report_owner_diagnostics([context], module)
    assert "explicit_stop" not in capsys.readouterr().out
    record["target_birth"] = "winft:456"
    path.write_text(json.dumps(record) + "\n", encoding="utf-8")
    watcher.report_owner_diagnostics([context], module)
    assert "explicit_stop" in capsys.readouterr().out


def test_exact_receipt_and_matching_index_completion_are_verified(
    watcher, monkeypatch, tmp_path, attempt,
):
    assert watch(watcher, monkeypatch, tmp_path, attempt[0]) == 0


@pytest.mark.parametrize("reason", ["response_incomplete", "context_admission"])
def test_known_partial_reasons_are_incomplete_not_generic_failure(
    watcher, monkeypatch, tmp_path, attempt, capsys, reason,
):
    receipt, index, _, _, _ = attempt
    rows = [json.loads(line) for line in index.read_text(encoding="utf-8").splitlines()]
    rows[-1].update(status="failed", stop_reason=reason)
    index.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    assert watch(watcher, monkeypatch, tmp_path, receipt) == watcher.INCOMPLETE_EXIT
    text = capsys.readouterr().out
    assert "INCOMPLETE -" in text and reason in text and "Not relaunched" in text


def test_startup_rejection_is_an_immediate_nonzero_result(
    watcher, monkeypatch, tmp_path, capsys,
):
    receipt = tmp_path / "attempt.launch.jsonl"
    initial = {"schema": 1, "launch_id": LAUNCH_ID, "engine_pid": os.getpid(),
               "state": "starting", "run_id": None}
    rejected = {**initial, "state": "rejected", "reason_code": "native_tools_required", "exit_code": 2}
    receipt.write_text(json.dumps(initial) + "\n" + json.dumps(rejected) + "\n", encoding="utf-8")
    assert watch(watcher, monkeypatch, tmp_path, receipt) == 7
    assert "native_tools_required" in capsys.readouterr().out
    assert not (tmp_path / ".claude/agent-runs").exists()


@pytest.mark.parametrize("mismatch", ["launch_id", "engine_pid", "run_id"])
def test_mismatched_identity_cannot_be_reported_finished(
    watcher, monkeypatch, tmp_path, attempt, mismatch,
):
    receipt, index, transcript, initial, ready = attempt
    ready[mismatch] = 999999 if mismatch == "engine_pid" else "wrong-identity"
    receipt.write_text(json.dumps(initial) + "\n" + json.dumps(ready) + "\n", encoding="utf-8")
    assert watch(watcher, monkeypatch, tmp_path, receipt) != 0


def test_matching_footer_without_terminal_index_is_not_success(
    watcher, monkeypatch, tmp_path, attempt,
):
    receipt, index, *_ = attempt
    index.write_text(index.read_text(encoding="utf-8").splitlines()[0] + "\n", encoding="utf-8")
    assert watch(watcher, monkeypatch, tmp_path, receipt) != 0


@pytest.mark.parametrize("status", ["failed", "interrupted"])
def test_terminal_failure_does_not_become_footer_success(
    watcher, monkeypatch, tmp_path, attempt, status,
):
    receipt, index, *_ = attempt
    records = [json.loads(line) for line in index.read_text(encoding="utf-8").splitlines()]
    records[-1]["status"] = status
    index.write_text("".join(json.dumps(record) + "\n" for record in records), encoding="utf-8")
    assert watch(watcher, monkeypatch, tmp_path, receipt) == 7


def test_partial_ready_record_cannot_verify_completion(watcher, monkeypatch, tmp_path, attempt):
    receipt, index, transcript, initial, ready = attempt
    receipt.write_text(json.dumps(initial) + '\n{"state":"ready"', encoding="utf-8")
    assert watch(watcher, monkeypatch, tmp_path, receipt) != 0


def test_dead_unregistered_child_is_not_a_running_worker(
    watcher, monkeypatch, tmp_path, attempt,
):
    receipt, index, transcript, initial, ready = attempt
    receipt.write_text(json.dumps(initial) + "\n", encoding="utf-8")
    monkeypatch.setattr(watcher, "process_alive", lambda pid: False)
    assert watch(watcher, monkeypatch, tmp_path, receipt) == 7


def test_ready_only_verifies_start_registration_and_live_process(
    watcher, monkeypatch, tmp_path, attempt, capsys,
):
    receipt, index, *_ = attempt
    index.write_text(index.read_text(encoding="utf-8").splitlines()[0] + "\n", encoding="utf-8")
    assert watch(watcher, monkeypatch, tmp_path, receipt, ready_only=True) == 0
    assert "READY" in capsys.readouterr().out


def test_ready_only_never_accepts_dead_registered_worker_without_terminal_end(
    watcher, monkeypatch, tmp_path, attempt,
):
    receipt, index, *_ = attempt
    index.write_text(index.read_text(encoding="utf-8").splitlines()[0] + "\n", encoding="utf-8")
    monkeypatch.setattr(watcher, "process_alive", lambda pid: False)
    assert watch(watcher, monkeypatch, tmp_path, receipt, ready_only=True) == 7


def test_ready_only_requires_exact_identity(watcher, monkeypatch, tmp_path):
    with pytest.raises(SystemExit) as error:
        watch(watcher, monkeypatch, tmp_path, ready_only=True)
    assert error.value.code == 2


def test_old_terminal_record_from_other_mirror_cannot_complete_new_run(
    watcher, monkeypatch, tmp_path, attempt,
):
    receipt, index, transcript, initial, ready = attempt
    records = [json.loads(line) for line in index.read_text(encoding="utf-8").splitlines()]
    index.write_text(json.dumps(records[0]) + "\n", encoding="utf-8")
    mirror = tmp_path / "other-index.jsonl"
    old_start = {**records[0], "engine_pid": 999999}
    mirror.write_text(json.dumps(old_start) + "\n" + json.dumps(records[1]) + "\n", encoding="utf-8")
    ready["index_paths"].append(str(mirror))
    receipt.write_text(json.dumps(initial) + "\n" + json.dumps(ready) + "\n", encoding="utf-8")
    assert watch(watcher, monkeypatch, tmp_path, receipt) != 0


def test_conflicting_terminal_records_cannot_be_reported_as_success(
    watcher, monkeypatch, tmp_path, attempt,
):
    receipt, index, *_ = attempt
    records = [json.loads(line) for line in index.read_text(encoding="utf-8").splitlines()]
    failed = {**records[-1], "status": "failed"}
    index.write_text(json.dumps(records[0]) + "\n" + json.dumps(failed) + "\n"
                     + json.dumps(records[-1]) + "\n", encoding="utf-8")
    assert watch(watcher, monkeypatch, tmp_path, receipt) != 0


def test_duplicate_receipt_json_keys_do_not_validate_success(watcher, monkeypatch, tmp_path, attempt):
    receipt, index, transcript, initial, ready = attempt
    ambiguous = json.dumps(ready)[:-1] + ', "state":"failed", "state":"ready"}'
    receipt.write_text(json.dumps(initial) + "\n" + ambiguous + "\n", encoding="utf-8")
    assert watch(watcher, monkeypatch, tmp_path, receipt) != 0


def test_ready_receipt_must_match_requested_project(watcher, monkeypatch, tmp_path, attempt):
    receipt, index, transcript, initial, ready = attempt
    ready["root"] = str(tmp_path / "wrong-project")
    receipt.write_text(json.dumps(initial) + "\n" + json.dumps(ready) + "\n", encoding="utf-8")
    assert watch(watcher, monkeypatch, tmp_path, receipt) != 0


def test_process_liveness_check_is_read_only_on_the_real_host(watcher):
    assert watcher.process_alive(os.getpid()) is True
    assert watcher.process_alive(-1) is False


def test_dead_child_with_no_receipt_is_an_explicit_launch_failure(watcher, monkeypatch, tmp_path):
    monkeypatch.setattr(watcher, "process_alive", lambda pid: False)
    assert watch(watcher, monkeypatch, tmp_path, tmp_path / "missing.launch.jsonl") == 7

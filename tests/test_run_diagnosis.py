"""Synthetic, metadata-only evidence; no live run artifacts or accounting."""
from dataclasses import replace
from datetime import datetime, timezone
import json
from pathlib import Path
import pytest
import runpy

from agent_control.ledger import sync_candidate
from agent_control.storage import StateLocks
from tests.test_ledger import run_candidate


PRIVATE = "SYNTHETIC_PRIVATE_BODY"
BUILD = "sha256:" + "1" * 64


def sample_run(tmp_path, *, run_id="sample", status="interrupted", timestamp=None):
    transcript = tmp_path / (run_id + ".md")
    candidate = replace(run_candidate(run_id=run_id, status=status),
                        transcript_artifact=str(transcript),
                        ended_at=None if status in {"running", "interrupted"} else
                        "2026-11-01T10:01:00Z",
                        timestamp=timestamp or "2026-11-01T10:00:00Z")
    return sync_candidate(tmp_path / "ledger.jsonl", candidate,
                          observed_at="2026-11-01T11:00:00Z", locks=StateLocks()).record


def event(name, *, run_id="sample", **fields):
    return {"schema": 1, "run_id": run_id, "event": name,
            "engine_pid": 123, "engine_birth": "winft:133700000000000001",
            "launch_id": "e8618875-bf83-4cd6-9888-239ef2c58c1b", **fields}


def write_events(record, rows, *, observer=False):
    suffix = ".observer.diagnostics.jsonl" if observer else ".engine.diagnostics.jsonl"
    path = Path(record.transcript_artifact + suffix)
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    return path


def diagnose(record, **kwargs):
    from agent_control.run_diagnosis import diagnose_run
    return diagnose_run(record, **kwargs)


def test_fatal_engine_exception_is_sanitized_and_separate_from_child_exit(tmp_path):
    record = sample_run(tmp_path)
    write_events(record, [event("registered"), event("engine_build", bundle_id=BUILD),
        event("command_exit", turn=11, call_index=2, exit_code=1),
        event("exception", reason="unhandled_exception", exception_class="TypeError",
              source="delegate.py", line=5200, message=PRIVATE, traceback=PRIVATE)])
    write_events(record, [event("engine_exit_observed", exit_code=1,
                                pid=123, birth="winft:133700000000000001")], observer=True)
    result = diagnose(record)
    assert result["category"] == "engine_exception"
    assert result["engine_exit_code"] == 1
    assert result["last_command_exit_code"] == 1
    assert result["bundle_id"] == BUILD
    assert result["exception"] == {"class": "TypeError", "source": "delegate.py", "line": 5200}
    assert PRIVATE not in json.dumps(result)


def test_child_failure_does_not_imply_engine_failure(tmp_path):
    record = sample_run(tmp_path, status="complete")
    write_events(record, [event("registered"), event("command_exit", exit_code=1),
                          event("terminal_persisted", indexes_written=2),
                          event("process_exit", exit_code=0)])
    result = diagnose(record)
    assert result["category"] == "complete"
    assert result["last_command_exit_code"] == 1
    assert result["engine_exit_code"] == 0


def test_exit_one_does_not_invent_a_killer_or_exception(tmp_path):
    record = sample_run(tmp_path)
    write_events(record, [event("registered"), event("tool_start", turn=11,
                         call_index=2, tool_name="run_command")])
    write_events(record, [event("engine_exit_observed", exit_code=1,
                                pid=123, birth="winft:133700000000000001")], observer=True)
    result = diagnose(record)
    assert result["category"] == "process_exit_unknown"
    assert result["evidence"] == "observed"
    assert result["exception"] is None
    assert result["unfinished"]["stage"] == "tool"
    assert result["unfinished"]["turn"] == 11


def test_completed_tool_without_recorded_result_exposes_result_boundary(tmp_path):
    record = sample_run(tmp_path)
    write_events(record, [event("registered"),
        event("tool_start", turn=11, call_index=1, tool_name="replace_in_file"),
        event("tool_end", turn=11, call_index=1, tool_name="replace_in_file"),
        event("result_recorded", turn=11, call_index=1),
        event("tool_start", turn=11, call_index=2, tool_name="run_command"),
        event("command_exit", turn=11, call_index=2, exit_code=1),
        event("tool_end", turn=11, call_index=2, tool_name="run_command")])
    result = diagnose(record)
    assert result["last_completed"]["stage"] == "tool"
    assert result["unfinished"] == {"stage": "result", "turn": 11, "call_index": 2,
                                     "tool_name": "run_command"}


def test_raised_checkpoint_end_is_not_a_successful_checkpoint(tmp_path):
    record = sample_run(tmp_path)
    write_events(record, [event("registered"), event("checkpoint_start", turn=2),
                         event("checkpoint_end", turn=2, outcome="raised", exception_class="OSError")])
    result = diagnose(record)
    assert result["unfinished"] == {"stage": "checkpoint", "turn": 2}
    assert result["last_completed"] is None


def test_terminal_append_failure_is_not_reported_as_a_persisted_terminal(tmp_path):
    record = sample_run(tmp_path)
    write_events(record, [event("registered"), event("terminal_requested", status="failed"),
                         event("index_append_result", indexes_attempted=2, indexes_written=1)])
    result = diagnose(record)
    assert result["category"] == "terminal_write_failed"
    assert result["terminal"]["indexes_written"] == 1
    assert result["terminal"]["persisted"] is False


def test_eventual_terminal_persistence_clears_a_transient_append_failure(tmp_path):
    record = sample_run(tmp_path, status="complete")
    write_events(record, [event("registered"), event("terminal_requested", status="complete"),
        event("index_append_result", indexes_attempted=2, indexes_written=1),
        event("terminal_persisted", indexes_attempted=2, indexes_written=2)])
    assert diagnose(record)["category"] == "complete"


def test_terminal_write_failure_does_not_hide_the_original_engine_exception(tmp_path):
    record = sample_run(tmp_path)
    write_events(record, [event("registered"), event("exception", reason="unhandled_exception",
        exception_class="TypeError"), event("terminal_requested", status="interrupted"),
        event("index_append_result", indexes_attempted=2, indexes_written=0)])
    result = diagnose(record)
    assert result["category"] == "engine_exception"
    assert result["terminal"]["persisted"] is False


def test_later_cleanup_exception_cannot_replace_primary_reason_or_exception(tmp_path):
    record = sample_run(tmp_path)
    write_events(record, [event("registered"), event("exception", reason="unhandled_exception",
        exception_class="TypeError", source="delegate.py", line=123), event("cleanup_start"),
        event("exception", reason="broken_output", exception_class="BrokenPipeError")])
    result = diagnose(record)
    assert result["category"] == "engine_exception"
    assert result["exception"] == {"class": "TypeError", "source": "delegate.py", "line": 123}


def test_row_cap_preserves_startup_anchor_and_recent_failure_tail(tmp_path, monkeypatch):
    import agent_control.run_diagnosis as module
    monkeypatch.setattr(module, "MAX_DIAGNOSTIC_ROWS", 8)
    record = sample_run(tmp_path)
    write_events(record, [event("registered"), event("engine_build", bundle_id=BUILD),
        *[event("stream_progress") for _ in range(12)],
        event("exception", reason="unhandled_exception", exception_class="TypeError"),
        event("process_exit", exit_code=1)])
    result = diagnose(record)
    assert result["coverage"]["partial"] is True
    assert result["category"] == "engine_exception"
    assert result["bundle_id"] == BUILD
    assert result["engine_exit_code"] == 1


def test_zero_attempted_writes_are_not_evidence_of_persistence(tmp_path):
    record = sample_run(tmp_path)
    write_events(record, [event("registered"), event("terminal_requested"),
                          event("index_append_result", indexes_attempted=0, indexes_written=0)])
    result = diagnose(record)
    assert result["terminal"]["indexes_attempted"] == 0
    assert result["terminal"]["indexes_written"] == 0
    assert result["terminal"]["persisted"] is None


def test_one_successful_index_is_not_full_persistence_when_two_are_expected(tmp_path):
    record = sample_run(tmp_path)
    write_events(record, [event("registered"), event("terminal_requested", indexes_expected=2),
        event("index_append_result", indexes_expected=2, indexes_attempted=1, indexes_written=1)])
    result = diagnose(record)
    assert result["terminal"]["persisted"] is False
    assert result["terminal"]["indexes_expected"] == 2
    assert result["category"] == "terminal_incomplete"


def test_post_completion_engine_exception_is_visible_without_rewriting_task_status(tmp_path):
    record = sample_run(tmp_path, status="complete")
    write_events(record, [event("registered"), event("terminal_persisted", indexes_written=2),
        event("exception", reason="unhandled_exception", exception_class="RuntimeError")])
    result = diagnose(record)
    assert result["status"] == "complete"
    assert result["category"] == "post_completion_exception"


def test_cleanup_failure_is_visible_without_overwriting_primary_failure(tmp_path):
    record = sample_run(tmp_path)
    write_events(record, [event("registered"), event("exception", reason="unhandled_exception",
        exception_class="TypeError"), event("cleanup_error", outcome="raised",
        exception_class="OSError", source="delegate.py", line=100, message=PRIVATE)])
    result = diagnose(record)
    assert result["category"] == "engine_exception"
    assert result["exception"]["class"] == "TypeError"
    assert result["cleanup_exception"] == {"class": "OSError", "source": "delegate.py", "line": 100}
    assert PRIVATE not in json.dumps(result)


def test_completed_task_with_cleanup_failure_stays_complete_but_is_diagnosed(tmp_path):
    record = sample_run(tmp_path, status="complete")
    write_events(record, [event("registered"), event("cleanup_error", outcome="raised", exception_class="OSError")])
    result = diagnose(record)
    assert result["status"] == "complete"
    assert result["category"] == "cleanup_failed"


@pytest.mark.parametrize("reason", ["mutation_outcome_unknown", "recovery_state"])
def test_nonlimit_stop_reasons_are_not_misclassified_as_budget_exits(tmp_path, reason):
    record = replace(sample_run(tmp_path, status="failed"), stop_reason=reason)
    assert diagnose(record)["category"] == reason


@pytest.mark.parametrize("reason", ["response_incomplete", "context_admission"])
def test_partial_protocol_reasons_survive_allowlist_and_keep_their_category(tmp_path, reason):
    from agent_control.models import safe_stop_reason
    assert safe_stop_reason(reason, "failed") == reason
    assert safe_stop_reason(reason, "complete") is None
    record = replace(sample_run(tmp_path, status="failed"), stop_reason=reason)
    result = diagnose(record)
    assert result["stop_reason"] == reason and result["category"] == reason


def test_recorded_limit_reason_is_not_lost_in_a_generic_category(tmp_path):
    record = replace(sample_run(tmp_path, status="failed"), stop_reason="repair_limit")
    assert diagnose(record)["stop_reason"] == "repair_limit"


def test_wrong_run_and_observer_process_identity_are_not_joined(tmp_path):
    record = sample_run(tmp_path)
    write_events(record, [event("registered"),
                          event("exception", run_id="other", exception_class="TypeError")])
    write_events(record, [event("engine_exit_observed", exit_code=99,
                                pid=123, birth="winft:133700000000009999",
                                engine_birth="winft:133700000000009999")], observer=True)
    result = diagnose(record)
    assert result["engine_exit_code"] is None
    assert result["exception"] is None
    assert result["coverage"]["identity_mismatch"] is True


def test_existing_monitor_protocol_uses_target_identity_not_the_monitor_pid(tmp_path):
    record = sample_run(tmp_path)
    write_events(record, [event("registered"), event("request_start"), event("request_end")])
    write_events(record, [{"schema": 1, "run_id": "sample", "observer": "monitor",
        "event": "observer_exit", "pid": 999, "birth": "winft:133700000000009999",
        "engine_pid": 123, "engine_birth": "winft:133700000000000001", "exit_code": 1}], observer=True)
    result = diagnose(record)
    assert result["engine_exit_code"] == 1
    assert result["evidence"] == "observed"
    assert result["coverage"]["identity_mismatch"] is False
    assert result["last_completed"]["stage"] == "request"


def test_observed_exit_takes_precedence_over_engine_exit_intent(tmp_path):
    record = sample_run(tmp_path)
    write_events(record, [event("registered"), event("process_exit", exit_code=0)])
    write_events(record, [event("observer_exit", exit_code=1)], observer=True)
    result = diagnose(record)
    assert result["engine_exit_code"] == 1
    assert result["reported_engine_exit_code"] == 0
    assert result["engine_exit_source"] == "observer"
    assert result["coverage"]["exit_code_conflict"] is True
    assert result["coverage"]["identity_mismatch"] is False


def test_recorded_system_exit_is_a_mechanism_not_an_unknown_observation(tmp_path):
    record = sample_run(tmp_path)
    write_events(record, [event("registered"), event("process_exit", reason="system_exit", exit_code=2)])
    result = diagnose(record)
    assert result["category"] == "system_exit"
    assert result["exit_mechanism"] == "system_exit"
    assert result["evidence"] == "recorded"
    assert result["exception"] is None


def test_signal_mechanism_is_not_replaced_by_its_system_exit_wrapper(tmp_path):
    record = sample_run(tmp_path)
    write_events(record, [event("registered"), event("signal", reason="signal", signal=15),
                         event("process_exit", reason="system_exit", exit_code=143)])
    assert diagnose(record)["category"] == "signal_exit"
    assert diagnose(record)["exit_mechanism"] == "signal"


def test_exit_mechanism_does_not_replace_a_primary_engine_exception(tmp_path):
    record = sample_run(tmp_path)
    write_events(record, [event("registered"), event("exception", reason="unhandled_exception",
        exception_class="TypeError"), event("process_exit", reason="system_exit", exit_code=1)])
    result = diagnose(record)
    assert result["category"] == "engine_exception"
    assert result["exception"]["class"] == "TypeError"


def test_parser_rejects_duplicate_keys_nonfinite_and_untrusted_metadata(tmp_path):
    record = sample_run(tmp_path)
    path = write_events(record, [event("registered"), event("exception",
        reason="unhandled_exception", exception_class=PRIVATE, source=PRIVATE + ".py",
        line=True, output=PRIVATE, argv=[PRIVATE])])
    with path.open("a", encoding="utf-8") as stream:
        stream.write('{"schema":1,"schema":1,"event":"exception"}\n')
        stream.write('{"schema":1,"elapsed_ms":NaN}\n')
        stream.write('{"schema":1,"event":"exception"')
    result = diagnose(record)
    assert PRIVATE not in json.dumps(result)
    assert result["coverage"]["invalid_rows"] >= 2
    assert result["coverage"]["partial"] is True


def test_read_is_bounded_and_reports_a_gap_instead_of_claiming_full_coverage(tmp_path):
    record = sample_run(tmp_path)
    path = write_events(record, [event("registered"), event("engine_build", bundle_id=BUILD)])
    with path.open("a", encoding="utf-8") as stream:
        stream.write("x" * (400 * 1024) + "\n")
        stream.write(json.dumps(event("process_exit", exit_code=0)) + "\n")
    result = diagnose(record)
    assert result["coverage"]["partial"] is True
    assert result["coverage"]["bytes_read"] <= 256 * 1024
    assert result["bundle_id"] == BUILD


def test_missing_journals_do_not_read_the_transcript_or_invent_evidence(tmp_path):
    record = sample_run(tmp_path)
    Path(record.transcript_artifact).write_text(PRIVATE, encoding="utf-8")
    result = diagnose(record)
    assert result["category"] == "process_exit_unknown"
    assert result["evidence"] == "unknown"
    assert result["engine_exit_code"] is None
    assert result["coverage"]["engine"] == "missing"
    assert PRIVATE not in json.dumps(result)


def test_known_producer_source_identifiers_survive_the_consumer_boundary(tmp_path):
    producer = runpy.run_path(str(Path(__file__).parents[1] / "engine/harness_diagnostics.py"))
    record = sample_run(tmp_path)
    for source in producer["SOURCES"]:
        write_events(record, [event("registered"), event("exception", reason="unhandled_exception",
            exception_class="TypeError", source=source, line=10)])
        assert diagnose(record)["exception"]["source"] == source


@pytest.mark.parametrize("source", ["evil.py", "C:/Users/x/context_tokens.py", "../context_engine.py",
                                    "context_tokens.py\n", "CONTEXT_TOKENS.PY", "context_", PRIVATE,
                                    "context_storage", "context_storage.py.bak",
                                    "engine/context_storage.py", "context_unknown.py"])
def test_unknown_or_path_like_sources_are_dropped_with_their_line(tmp_path, source):
    record = sample_run(tmp_path)
    write_events(record, [event("registered"), event("exception", reason="unhandled_exception",
        exception_class="TypeError", source=source, line=10)])
    result = diagnose(record)
    assert result["exception"] == {"class": "TypeError"}
    assert source not in json.dumps(result)


@pytest.mark.parametrize("line", [0, None])
def test_known_source_never_invents_a_line(tmp_path, line):
    record = sample_run(tmp_path)
    write_events(record, [event("registered"), event("exception", reason="unhandled_exception",
        exception_class="TypeError", source="context_engine.py", line=line)])
    assert diagnose(record)["exception"] == {"class": "TypeError", "source": "context_engine.py"}


def test_build_reports_execution_profile_and_numeric_runtime_without_extra_fields(tmp_path):
    record = sample_run(tmp_path)
    write_events(record, [event("registered"), event("engine_build", bundle_id=BUILD,
        execution_profile="resilient-v1", runtime_version="3.13.9", architecture="64bit", argv=PRIVATE)])
    result = diagnose(record)
    assert result["execution_profile"] == "resilient-v1"
    assert result["runtime_version"] == "3.13.9"
    assert result["architecture"] == "64bit"
    assert PRIVATE not in json.dumps(result)


@pytest.mark.parametrize("value", [[], {}, True, 1.5])
def test_wrong_metadata_types_cannot_crash_the_diagnosis(tmp_path, value):
    record = sample_run(tmp_path)
    write_events(record, [event("registered"), event("exception", tool_name=value,
        exception_class=value, source=value, reason=value, outcome=value),
        event("tool_end", tool_name=value, outcome=value)])
    result = diagnose(record)
    assert result["category"] == "process_exit_unknown"
    assert result["exception"]["class"] == "OtherError"


def test_incident_report_counts_population_before_sampling_and_preserves_revisions(tmp_path):
    from agent_control.run_diagnosis import build_incident_report
    a = sample_run(tmp_path, run_id="a", status="interrupted")
    b = sample_run(tmp_path, run_id="b", status="complete")
    old = sample_run(tmp_path, run_id="old", timestamp="2026-10-01T10:00:00Z")
    result = build_incident_report([a, b, old], now=datetime(2026, 11, 1, 11, tzinfo=timezone.utc),
                                   days=7, max_runs=1)
    assert result["eligible_runs"] == 2
    assert result["interrupted_runs"] == 1
    assert result["terminal_runs"] == 2
    assert result["interruption_rate"] == 0.5
    assert result["scanned_runs"] == 1
    assert result["unscanned_runs"] == 1
    assert sum(group["count"] for group in result["groups"]) == 1


def test_incident_sampling_orders_instants_not_iso_string_spelling(tmp_path):
    from agent_control.run_diagnosis import build_incident_report
    earlier = replace(sample_run(tmp_path, run_id="early"), model="earlier", timestamp="2026-11-01T10:00:00Z")
    later = replace(sample_run(tmp_path, run_id="late"), model="later", timestamp="2026-11-01T10:00:00.100Z")
    result = build_incident_report([earlier, later], now=datetime(2026, 11, 1, 11, tzinfo=timezone.utc), max_runs=1)
    assert result["groups"][0]["model"] == "later"

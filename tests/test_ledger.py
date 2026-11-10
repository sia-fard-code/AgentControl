from __future__ import annotations

from pathlib import Path
from dataclasses import replace

import pytest

from agent_control.ledger import (
    highest_valid_revisions,
    load_ledger,
    semantic_run_payload,
    sync_candidate,
    validate_run_record,
)
from agent_control.models import RunCandidate, UsageEvidence, UsageTotals
from agent_control.storage import StateLocks


def usage_evidence(
    *,
    tokens_sent: int | None = 10,
    tokens_returned: int | None = 2,
    cache_read: int | None = 4,
    cache_write: int | None = 0,
    sent_includes_cache_read: bool | None = False,
    source: str = "usage_journal",
    artifact: str | None = "artifact.usage.jsonl",
) -> UsageEvidence:
    totals = UsageTotals(
        tokens_sent=tokens_sent,
        tokens_returned=tokens_returned,
        cache_read=cache_read,
        cache_write=cache_write,
        sent_includes_cache_read=sent_includes_cache_read,
        response_count=1,
        tokens_sent_unknown_responses=0 if tokens_sent is not None else 1,
        tokens_returned_unknown_responses=0 if tokens_returned is not None else 1,
        cache_read_unknown_responses=0 if cache_read is not None else 1,
        cache_write_unknown_responses=0 if cache_write is not None else 1,
    )
    return UsageEvidence(totals=totals, source=source, artifact=artifact, diagnostics=())


def run_candidate(
    *,
    run_id: str = "r",
    project_id: str = "p",
    status: str = "complete",
    provenance: tuple[str, ...] = ("machine_index",),
    model: str = "sample-model",
    **usage_kwargs: object,
) -> RunCandidate:
    return RunCandidate(
        run_id=run_id,
        project_id=project_id,
        timestamp="2026-09-17T12:00:00+00:00",
        ended_at="2026-09-17T12:05:00+00:00",
        status=status,  # type: ignore[arg-type]
        label="demo",
        model=model,
        effort=None,
        tool_mode=None,
        write_mode=None,
        tool_calls=3,
        refused=0,
        usage=usage_evidence(**usage_kwargs),  # type: ignore[arg-type]
        transcript_artifact="artifact.md",
        provenance=provenance,
        lifecycle_inferred=False,
    )


@pytest.fixture
def locks() -> StateLocks:
    return StateLocks()


def test_failed_reason_survives_weaker_same_status_observation(tmp_path, locks):
    path = tmp_path / "ledger.jsonl"
    current = replace(run_candidate(status="failed"), stop_reason="max_iters")
    sync_candidate(path, current, observed_at="2026-09-17T12:05:00Z", locks=locks)
    sync_candidate(path, replace(current, stop_reason=None),
                   observed_at="2026-09-17T12:06:00Z", locks=locks)
    latest = highest_valid_revisions(load_ledger(path)[0])["r"]
    assert latest.stop_reason == "max_iters"
    assert len(path.read_text().splitlines()) == 1


def test_new_failed_reason_merges_without_losing_known_usage(tmp_path, locks):
    path = tmp_path / "ledger.jsonl"
    sync_candidate(path, run_candidate(status="failed"),
                   observed_at="2026-09-17T12:05:00Z", locks=locks)
    weaker = replace(run_candidate(status="failed", tokens_sent=None),
                     stop_reason="repair_limit")
    sync_candidate(path, weaker, observed_at="2026-09-17T12:06:00Z", locks=locks)
    latest = highest_valid_revisions(load_ledger(path)[0])["r"]
    assert latest.stop_reason == "repair_limit"
    assert latest.tokens_sent == 10
    assert len(path.read_text().splitlines()) == 2


def test_unchanged_candidate_does_not_append_revision(
    tmp_path: Path, locks: StateLocks
) -> None:
    candidate = run_candidate(run_id="r", tokens_sent=10)
    first = sync_candidate(
        tmp_path / "ledger.jsonl",
        candidate,
        observed_at="2026-09-17T12:00:00+00:00",
        locks=locks,
    )
    second = sync_candidate(
        tmp_path / "ledger.jsonl",
        candidate,
        observed_at="2026-09-17T12:01:00+00:00",
        locks=locks,
    )
    assert first.appended is True
    assert second.appended is False
    assert len((tmp_path / "ledger.jsonl").read_text().splitlines()) == 1


def test_changed_evidence_appends_higher_revision(
    tmp_path: Path, locks: StateLocks
) -> None:
    path = tmp_path / "ledger.jsonl"
    first = sync_candidate(
        path,
        run_candidate(run_id="r", tokens_sent=10),
        observed_at="2026-09-17T12:00:00+00:00",
        locks=locks,
    )
    second = sync_candidate(
        path,
        run_candidate(run_id="r", tokens_sent=25),
        observed_at="2026-09-17T12:02:00+00:00",
        locks=locks,
    )
    assert first.record.revision == 1
    assert second.appended is True
    assert second.record.revision == 2
    current = highest_valid_revisions(load_ledger(path)[0])["r"]
    assert current.revision == 2
    assert current.tokens_sent == 25


def test_weaker_candidate_does_not_downgrade_known_model(
    tmp_path: Path, locks: StateLocks
) -> None:
    # A later transcript-only candidate that cannot see the model must never
    # overwrite a known model with "unknown" and zero out its cost.
    path = tmp_path / "ledger.jsonl"
    sync_candidate(
        path,
        run_candidate(run_id="r", model="gpt-5.6-sol", tokens_sent=1000),
        observed_at="2026-09-17T12:00:00+00:00",
        locks=locks,
    )
    downgrade = sync_candidate(
        path,
        run_candidate(
            run_id="r",
            model="unknown",
            provenance=("projecta_transcript",),
            tokens_sent=1000,
        ),
        observed_at="2026-09-17T12:05:00+00:00",
        locks=locks,
    )
    assert downgrade.appended is False
    current = highest_valid_revisions(load_ledger(path)[0])["r"]
    assert current.model == "gpt-5.6-sol"


def test_weaker_candidate_does_not_null_known_usage(
    tmp_path: Path, locks: StateLocks
) -> None:
    path = tmp_path / "ledger.jsonl"
    sync_candidate(
        path,
        run_candidate(run_id="r", model="gpt-5.6-sol", tokens_sent=1000, tokens_returned=50),
        observed_at="2026-09-17T12:00:00+00:00",
        locks=locks,
    )
    weaker = sync_candidate(
        path,
        run_candidate(
            run_id="r", model="gpt-5.6-sol", tokens_sent=None, tokens_returned=None
        ),
        observed_at="2026-09-17T12:05:00+00:00",
        locks=locks,
    )
    assert weaker.appended is False
    current = highest_valid_revisions(load_ledger(path)[0])["r"]
    assert current.tokens_sent == 1000


def test_status_change_is_never_blocked_by_downgrade_guard(
    tmp_path: Path, locks: StateLocks
) -> None:
    # A running run whose artifact later disappears (counters -> None) must still
    # transition to interrupted, carrying its last-known tokens forward.
    path = tmp_path / "ledger.jsonl"
    sync_candidate(
        path,
        run_candidate(run_id="r", status="running", model="gpt-5.6-sol", tokens_sent=38230, tokens_returned=4508),
        observed_at="2026-09-17T12:00:00+00:00",
        locks=locks,
    )
    result = sync_candidate(
        path,
        run_candidate(
            run_id="r",
            status="interrupted",
            model="unknown",
            provenance=("machine_index",),
            tokens_sent=None,
            tokens_returned=None,
        ),
        observed_at="2026-09-17T14:30:00+00:00",
        locks=locks,
    )
    assert result.appended is True
    current = highest_valid_revisions(load_ledger(path)[0])["r"]
    assert current.status == "interrupted"
    # last-known usage and model preserved
    assert current.tokens_sent == 38230
    assert current.model == "gpt-5.6-sol"


def test_terminal_status_is_absorbing(tmp_path: Path, locks: StateLocks) -> None:
    # Once a run is terminal, a later "running" observation (e.g. a late artifact
    # write or a re-read) must never resurrect it.
    path = tmp_path / "ledger.jsonl"
    sync_candidate(
        path,
        run_candidate(run_id="r", status="complete", tokens_sent=10),
        observed_at="2026-09-17T12:00:00+00:00",
        locks=locks,
    )
    revert = sync_candidate(
        path,
        run_candidate(run_id="r", status="running", tokens_sent=10),
        observed_at="2026-09-17T12:05:00+00:00",
        locks=locks,
    )
    assert revert.appended is False
    current = highest_valid_revisions(load_ledger(path)[0])["r"]
    assert current.status == "complete"


def test_stronger_candidate_still_upgrades(tmp_path: Path, locks: StateLocks) -> None:
    # A later candidate that fills in a previously-unknown model must win.
    path = tmp_path / "ledger.jsonl"
    sync_candidate(
        path,
        run_candidate(run_id="r", model="unknown", tokens_sent=None),
        observed_at="2026-09-17T12:00:00+00:00",
        locks=locks,
    )
    upgrade = sync_candidate(
        path,
        run_candidate(run_id="r", model="gpt-5.6-sol", tokens_sent=1000),
        observed_at="2026-09-17T12:05:00+00:00",
        locks=locks,
    )
    assert upgrade.appended is True
    current = highest_valid_revisions(load_ledger(path)[0])["r"]
    assert current.model == "gpt-5.6-sol"
    assert current.tokens_sent == 1000


def test_provenance_order_does_not_trigger_new_revision(
    tmp_path: Path, locks: StateLocks
) -> None:
    path = tmp_path / "ledger.jsonl"
    sync_candidate(
        path,
        run_candidate(run_id="r", provenance=("machine_index", "projecta_ledger")),
        observed_at="2026-09-17T12:00:00+00:00",
        locks=locks,
    )
    second = sync_candidate(
        path,
        run_candidate(
            run_id="r", provenance=("projecta_ledger", "machine_index", "machine_index")
        ),
        observed_at="2026-09-17T12:02:00+00:00",
        locks=locks,
    )
    assert second.appended is False


def test_semantic_payload_excludes_bookkeeping_fields(
    tmp_path: Path, locks: StateLocks
) -> None:
    result = sync_candidate(
        tmp_path / "ledger.jsonl",
        run_candidate(),
        observed_at="2026-09-17T12:00:00+00:00",
        locks=locks,
    )
    payload = semantic_run_payload(result.record)
    assert "schema" not in payload
    assert "revision" not in payload
    assert "observed_at" not in payload
    assert payload["tokens_sent"] == 10


def test_sync_candidate_with_current_skips_ledger_load(
    tmp_path: Path, locks: StateLocks, monkeypatch
) -> None:
    import agent_control.ledger as ledger

    path = tmp_path / "ledger.jsonl"
    # Seed a real revision 1 so a supplied current forces revision 2.
    first = sync_candidate(
        path,
        run_candidate(run_id="r", tokens_sent=10),
        observed_at="2026-09-17T12:00:00+00:00",
        locks=locks,
    )
    calls = [0]
    original = ledger.load_ledger

    def counting(p):
        calls[0] += 1
        return original(p)

    monkeypatch.setattr(ledger, "load_ledger", counting)
    result = sync_candidate(
        path,
        run_candidate(run_id="r", tokens_sent=25),
        observed_at="2026-09-17T12:02:00+00:00",
        locks=locks,
        current=first.record,
    )
    assert calls[0] == 0
    assert result.appended is True
    assert result.record.revision == 2
    current = highest_valid_revisions(load_ledger(path)[0])["r"]
    assert current.tokens_sent == 25


def test_sync_candidate_with_current_none_treats_run_as_new(
    tmp_path: Path, locks: StateLocks
) -> None:
    path = tmp_path / "ledger.jsonl"
    result = sync_candidate(
        path,
        run_candidate(run_id="r", tokens_sent=10),
        observed_at="2026-09-17T12:00:00+00:00",
        locks=locks,
        current=None,
    )
    assert result.appended is True
    assert result.record.revision == 1


def test_highest_valid_revision_selects_current_row() -> None:
    rows = [
        validate_run_record(row, "test")
        for row in _ledger_rows_with_revisions()
    ]
    current = highest_valid_revisions(rows)
    assert current["r"].revision == 3
    assert current["r"].status == "failed"


def test_load_ledger_reports_malformed_and_invalid_records(
    tmp_path: Path, locks: StateLocks
) -> None:
    path = tmp_path / "ledger.jsonl"
    sync_candidate(
        path, run_candidate(), observed_at="2026-09-17T12:00:00+00:00", locks=locks
    )
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write("{malformed\n")
        handle.write('{"schema":1,"run_id":"bad","revision":0}\n')
    rows, diagnostics = load_ledger(path)
    assert len(rows) == 1
    codes = {item.code for item in diagnostics}
    assert "malformed_json" in codes
    assert "invalid_run_record" in codes


def test_negative_or_boolean_counters_are_rejected_not_zeroed() -> None:
    base = _valid_row()
    for bad in (-1, True):
        broken = {**base, "tokens_sent": bad}
        with pytest.raises(ValueError):
            validate_run_record(broken, "test")


def _valid_row() -> dict[str, object]:
    return {
        "schema": 1,
        "run_id": "r",
        "revision": 1,
        "project_id": "p",
        "timestamp": "2026-09-17T12:00:00+00:00",
        "ended_at": None,
        "status": "complete",
        "label": "demo",
        "model": "sample-model",
        "effort": None,
        "tool_mode": None,
        "write_mode": None,
        "tool_calls": 3,
        "refused": 0,
        "tokens_sent": 10,
        "tokens_returned": 2,
        "cache_read": 4,
        "cache_write": 0,
        "sent_includes_cache_read": False,
        "tokens_sent_unknown_responses": 0,
        "tokens_returned_unknown_responses": 0,
        "cache_read_unknown_responses": 0,
        "cache_write_unknown_responses": 0,
        "usage_source": "usage_journal",
        "usage_artifact": "artifact.usage.jsonl",
        "transcript_artifact": "artifact.md",
        "provenance": ["machine_index"],
        "lifecycle_inferred": False,
        "observed_at": "2026-09-17T12:00:00+00:00",
    }


def _ledger_rows_with_revisions() -> list[dict[str, object]]:
    first = _valid_row()
    second = {**_valid_row(), "revision": 3, "status": "failed"}
    third = {**_valid_row(), "revision": 2, "status": "interrupted"}
    return [first, second, third]

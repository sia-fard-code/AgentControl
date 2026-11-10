from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import asdict
from pathlib import Path

from .models import AppendResult, Diagnostic, RunCandidate, RunRecord, safe_stop_reason
from .storage import (
    StateLocks,
    append_jsonl,
    normalize_optional_utc,
    normalize_utc,
    read_jsonl,
)
from .usage import validate_counter

_VALID_STATUS = frozenset({"running", "complete", "failed", "interrupted"})
_TERMINAL_STATUSES = frozenset({"complete", "failed", "interrupted"})
_VALID_USAGE_SOURCE = frozenset({"usage_journal", "messages", "footer", "missing"})

# Fields excluded from semantic equality: append-only bookkeeping only.
_NON_SEMANTIC_FIELDS = frozenset({"schema", "revision", "observed_at"})

# Sentinel distinguishing "caller supplied the current record (including None)"
# from "caller wants sync_candidate to load the ledger itself".
_UNSET = object()


def _require_str(value: object, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{field} must be a non-empty string")
    return value


def _optional_str(value: object, field: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise ValueError(f"{field} must be a non-empty string or null")
    return value


def _require_int(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{field} must be an integer")
    return value


def _optional_bool(value: object, field: str) -> bool | None:
    if value is None:
        return None
    if not isinstance(value, bool):
        raise ValueError(f"{field} must be a boolean or null")
    return value


def _provenance(value: object) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        raise ValueError("provenance must be a list of strings")
    items = []
    for item in value:
        if not isinstance(item, str) or not item:
            raise ValueError("provenance entries must be non-empty strings")
        items.append(item)
    return tuple(items)


def _normalize_provenance(provenance: Sequence[str]) -> tuple[str, ...]:
    seen: dict[str, None] = {}
    for item in provenance:
        seen.setdefault(item, None)
    return tuple(sorted(seen))


def validate_run_record(value: Mapping[str, object], context: str) -> RunRecord:
    if not isinstance(value, Mapping):
        raise ValueError(f"{context}: run record must be an object")
    schema = _require_int(value.get("schema"), "schema")
    if schema != 1:
        raise ValueError(f"{context}: unsupported ledger schema {schema!r}")
    revision = _require_int(value.get("revision"), "revision")
    if revision < 1:
        raise ValueError(f"{context}: revision must be >= 1")
    status = _require_str(value.get("status"), "status")
    if status not in _VALID_STATUS:
        raise ValueError(f"{context}: invalid status {status!r}")
    usage_source = _require_str(value.get("usage_source"), "usage_source")
    if usage_source not in _VALID_USAGE_SOURCE:
        raise ValueError(f"{context}: invalid usage_source {usage_source!r}")
    return RunRecord(
        schema=schema,
        run_id=_require_str(value.get("run_id"), "run_id"),
        revision=revision,
        project_id=_require_str(value.get("project_id"), "project_id"),
        timestamp=normalize_utc(_require_str(value.get("timestamp"), "timestamp")),
        ended_at=normalize_optional_utc(_optional_str(value.get("ended_at"), "ended_at")),
        status=status,  # type: ignore[arg-type]
        label=_require_str(value.get("label"), "label"),
        model=_require_str(value.get("model"), "model"),
        effort=_optional_str(value.get("effort"), "effort"),
        tool_mode=_optional_str(value.get("tool_mode"), "tool_mode"),
        write_mode=_optional_str(value.get("write_mode"), "write_mode"),
        tool_calls=validate_counter(value.get("tool_calls"), "tool_calls"),
        refused=validate_counter(value.get("refused"), "refused"),
        tokens_sent=validate_counter(value.get("tokens_sent"), "tokens_sent"),
        tokens_returned=validate_counter(
            value.get("tokens_returned"), "tokens_returned"
        ),
        cache_read=validate_counter(value.get("cache_read"), "cache_read"),
        cache_write=validate_counter(value.get("cache_write"), "cache_write"),
        sent_includes_cache_read=_optional_bool(
            value.get("sent_includes_cache_read"), "sent_includes_cache_read"
        ),
        tokens_sent_unknown_responses=_require_int(
            value.get("tokens_sent_unknown_responses"), "tokens_sent_unknown_responses"
        ),
        tokens_returned_unknown_responses=_require_int(
            value.get("tokens_returned_unknown_responses"),
            "tokens_returned_unknown_responses",
        ),
        cache_read_unknown_responses=_require_int(
            value.get("cache_read_unknown_responses"), "cache_read_unknown_responses"
        ),
        cache_write_unknown_responses=_require_int(
            value.get("cache_write_unknown_responses"), "cache_write_unknown_responses"
        ),
        usage_source=usage_source,  # type: ignore[arg-type]
        usage_artifact=_optional_str(value.get("usage_artifact"), "usage_artifact"),
        transcript_artifact=_optional_str(
            value.get("transcript_artifact"), "transcript_artifact"
        ),
        provenance=_provenance(value.get("provenance")),
        lifecycle_inferred=bool(value.get("lifecycle_inferred")),
        observed_at=normalize_utc(_require_str(value.get("observed_at"), "observed_at")),
        stop_reason=safe_stop_reason(value.get("stop_reason"), status),
    )


def load_ledger(path: Path) -> tuple[list[RunRecord], tuple[Diagnostic, ...]]:
    rows, diagnostics = read_jsonl(path, source=str(path))
    records: list[RunRecord] = []
    extra: list[Diagnostic] = list(diagnostics)
    for line, row in enumerate(rows, start=1):
        try:
            records.append(validate_run_record(row, f"{path}:{line}"))
        except ValueError as exc:
            extra.append(
                Diagnostic(
                    "invalid_run_record",
                    "ledger record failed validation",
                    source=str(path),
                    run_id=row.get("run_id") if isinstance(row.get("run_id"), str) else None,
                )
            )
    return records, tuple(extra)


def highest_valid_revisions(rows: Sequence[RunRecord]) -> dict[str, RunRecord]:
    current: dict[str, RunRecord] = {}
    for record in rows:
        existing = current.get(record.run_id)
        if existing is None or record.revision > existing.revision:
            current[record.run_id] = record
    return current


def semantic_run_payload(record: RunRecord) -> dict[str, object]:
    payload = {
        key: value
        for key, value in asdict(record).items()
        if key not in _NON_SEMANTIC_FIELDS
    }
    payload["provenance"] = list(_normalize_provenance(record.provenance))
    return payload


def _candidate_payload(candidate: RunCandidate, observed_at: str) -> dict[str, object]:
    totals = candidate.usage.totals
    return {
        "run_id": candidate.run_id,
        "project_id": candidate.project_id,
        "timestamp": normalize_utc(candidate.timestamp),
        "ended_at": normalize_optional_utc(candidate.ended_at),
        "status": candidate.status,
        "label": candidate.label,
        "model": candidate.model,
        "effort": candidate.effort,
        "tool_mode": candidate.tool_mode,
        "write_mode": candidate.write_mode,
        "tool_calls": candidate.tool_calls,
        "refused": candidate.refused,
        "tokens_sent": totals.tokens_sent,
        "tokens_returned": totals.tokens_returned,
        "cache_read": totals.cache_read,
        "cache_write": totals.cache_write,
        "sent_includes_cache_read": totals.sent_includes_cache_read,
        "tokens_sent_unknown_responses": totals.tokens_sent_unknown_responses,
        "tokens_returned_unknown_responses": totals.tokens_returned_unknown_responses,
        "cache_read_unknown_responses": totals.cache_read_unknown_responses,
        "cache_write_unknown_responses": totals.cache_write_unknown_responses,
        "usage_source": candidate.usage.source,
        "usage_artifact": candidate.usage.artifact,
        "transcript_artifact": candidate.transcript_artifact,
        "provenance": list(_normalize_provenance(candidate.provenance)),
        "lifecycle_inferred": candidate.lifecycle_inferred,
        "observed_at": normalize_utc(observed_at),
        "stop_reason": candidate.stop_reason,
    }


def _build_record(payload: Mapping[str, object], revision: int) -> RunRecord:
    return validate_run_record({"schema": 1, "revision": revision, **payload}, "candidate")


# Counters whose known value must never be replaced with null by a later,
# weaker record. A transcript-only rescan that cannot see usage or the model
# must not zero out data an earlier machine-index/ledger record already proved.
_PROTECTED_COUNTERS = (
    "tokens_sent",
    "tokens_returned",
    "cache_read",
    "cache_write",
)


def _is_downgrade(current: RunRecord, candidate: RunRecord) -> bool:
    """True when the candidate would replace known data with weaker unknowns.

    A downgrade is a change that only loses information: turning a real model
    into "unknown", or turning a known counter into null, without adding any new
    known field. Such records must not win, because highest-revision selection
    would otherwise surface the weaker data.

    A lifecycle status change (e.g. running -> interrupted or complete) always
    counts as new information and is never treated as a downgrade, so a finished
    or abandoned run can never be frozen in "running" just because its transcript
    became unreadable and its counters dropped to null.
    """
    if current.status != candidate.status:
        return False

    loses = False
    gains = False

    current_known_model = current.model not in ("", "unknown")
    candidate_known_model = candidate.model not in ("", "unknown")
    if current_known_model and not candidate_known_model:
        loses = True
    elif candidate_known_model and current.model != candidate.model:
        gains = True

    for field in _PROTECTED_COUNTERS:
        current_value = getattr(current, field)
        candidate_value = getattr(candidate, field)
        if current_value is not None and candidate_value is None:
            loses = True
        elif candidate_value is not None and current_value != candidate_value:
            gains = True

    return loses and not gains


def _merge_preserving_known(
    current: RunRecord, payload: dict[str, object]
) -> dict[str, object]:
    """Carry forward known model/counters the candidate would otherwise null.

    When a later observation changes the status (e.g. running -> interrupted)
    but can no longer read the transcript, its counters and model come back as
    null/unknown. We keep the run's proven values so a finishing run does not
    lose the token totals it had already recorded.
    """
    merged = dict(payload)
    if payload.get("model") in ("", "unknown", None) and current.model not in (
        "",
        "unknown",
    ):
        merged["model"] = current.model
    for field in _PROTECTED_COUNTERS:
        if payload.get(field) is None and getattr(current, field) is not None:
            merged[field] = getattr(current, field)
    if current.status == payload.get("status") == "failed":
        merged["stop_reason"] = (
            safe_stop_reason(payload.get("stop_reason"), "failed") or current.stop_reason
        )
    if payload.get("sent_includes_cache_read") is None and (
        current.sent_includes_cache_read is not None
    ):
        merged["sent_includes_cache_read"] = current.sent_includes_cache_read
    return merged


def sync_candidate(
    path: Path,
    candidate: RunCandidate,
    *,
    observed_at: str,
    locks: StateLocks,
    current: RunRecord | None | object = _UNSET,
) -> AppendResult:
    payload = _candidate_payload(candidate, observed_at)
    with locks.write:
        if current is _UNSET:
            rows, _ = load_ledger(path)
            current = highest_valid_revisions(rows).get(candidate.run_id)
        # Terminal states are absorbing: once a run is complete/failed/
        # interrupted it must never be revised back to running. This prevents a
        # late artifact write or a re-read from resurrecting a finished run.
        if (
            current is not None
            and current.status in _TERMINAL_STATUSES
            and candidate.status == "running"
        ):
            return AppendResult(appended=False, record=current)
        # A status change must never lose already-proven usage: carry forward the
        # last-known counters/model when the new observation can't see them.
        if current is not None and (current.status != candidate.status or (
            current.status == candidate.status == "failed" and (
                current.stop_reason or safe_stop_reason(candidate.stop_reason, "failed")
            )
        )):
            payload = _merge_preserving_known(current, payload)
        candidate_record = _build_record(payload, current.revision + 1 if current else 1)
        if current is not None and semantic_run_payload(current) == semantic_run_payload(
            candidate_record
        ):
            return AppendResult(appended=False, record=current)
        if current is not None and _is_downgrade(current, candidate_record):
            return AppendResult(appended=False, record=current)
        append_jsonl(path, [asdict(candidate_record)], locks.write)
        return AppendResult(appended=True, record=candidate_record)

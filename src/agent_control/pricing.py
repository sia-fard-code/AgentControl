from __future__ import annotations

import math
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

from .models import (
    CostResult,
    Diagnostic,
    PricingPreview,
    PricingVersion,
    RateSet,
    RunRecord,
)
from .storage import (
    StateLocks,
    append_jsonl,
    normalize_utc,
    read_jsonl,
    strict_json_loads,
)
from .tool_calls import ToolCall

_RATE_KEYS = ("input", "output", "cache_read", "cache_write")
_UNIT_TOKENS = 1_000_000


def _finite_nonnegative(value: object, context: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{context} must be a number")
    number = float(value)
    if not math.isfinite(number) or number < 0:
        raise ValueError(f"{context} must be finite and non-negative")
    return number


def validate_rate_set(value: object, context: str) -> RateSet:
    if not isinstance(value, Mapping):
        raise ValueError(f"{context} must be an object")
    if set(value) != set(_RATE_KEYS):
        raise ValueError(f"{context} must define exactly {sorted(_RATE_KEYS)}")
    return RateSet(
        input=_finite_nonnegative(value["input"], f"{context}.input"),
        output=_finite_nonnegative(value["output"], f"{context}.output"),
        cache_read=_finite_nonnegative(value["cache_read"], f"{context}.cache_read"),
        cache_write=_finite_nonnegative(value["cache_write"], f"{context}.cache_write"),
    )


def load_baseline(path: Path) -> dict[str, RateSet | None]:
    if not path.exists():
        return {}
    envelope = strict_json_loads(path.read_bytes())
    if not isinstance(envelope, Mapping):
        raise ValueError("pricing baseline must be an object")
    if not envelope.get("initialized"):
        return {}
    models = envelope.get("models")
    if not isinstance(models, Mapping):
        raise ValueError("pricing baseline models must be an object")
    result: dict[str, RateSet | None] = {}
    for model, rates in models.items():
        if rates is None:
            result[str(model)] = None
        else:
            result[str(model)] = validate_rate_set(rates, f"models.{model}")
    return result


def validate_pricing_version(value: Mapping[str, object], context: str) -> PricingVersion:
    if not isinstance(value, Mapping):
        raise ValueError(f"{context}: pricing version must be an object")
    schema = value.get("schema")
    if schema != 1:
        raise ValueError(f"{context}: unsupported pricing schema {schema!r}")
    model = value.get("model")
    if not isinstance(model, str) or not model:
        raise ValueError(f"{context}: model must be a non-empty string")
    note = value.get("note", "")
    if not isinstance(note, str):
        raise ValueError(f"{context}: note must be a string")
    version_id = value.get("version_id")
    if not isinstance(version_id, str) or not version_id:
        raise ValueError(f"{context}: version_id must be a non-empty string")
    return PricingVersion(
        schema=1,
        version_id=version_id,
        model=model,
        effective_at=normalize_utc(str(value.get("effective_at"))),
        created_at=normalize_utc(str(value.get("created_at"))),
        rates=validate_rate_set(value.get("rates"), f"{context}.rates"),
        note=note,
    )


def load_pricing_history(
    path: Path,
) -> tuple[list[PricingVersion], tuple[Diagnostic, ...]]:
    rows, diagnostics = read_jsonl(path, source=str(path))
    versions: list[PricingVersion] = []
    extra: list[Diagnostic] = list(diagnostics)
    for line, row in enumerate(rows, start=1):
        try:
            versions.append(validate_pricing_version(row, f"{path}:{line}"))
        except ValueError:
            extra.append(
                Diagnostic(
                    "invalid_pricing_version",
                    "pricing history record failed validation",
                    source=str(path),
                    line=line,
                )
            )
    return versions, tuple(extra)


def resolve_rates(
    model: str,
    started_at: str,
    baseline: Mapping[str, RateSet | None],
    history: Sequence[PricingVersion],
) -> tuple[RateSet | None, str | None]:
    run_start = datetime.fromisoformat(normalize_utc(started_at))
    selected: PricingVersion | None = None
    selected_effective: datetime | None = None
    for index, version in enumerate(history):
        if version.model != model:
            continue
        effective = datetime.fromisoformat(version.effective_at)
        if effective > run_start:
            continue
        if selected_effective is None or effective >= selected_effective:
            selected = version
            selected_effective = effective
    if selected is not None:
        return selected.rates, selected.version_id
    baseline_rate = baseline.get(model)
    if baseline_rate is not None:
        return baseline_rate, None
    return None, None


def _ordinary_input(
    sent: int, includes_cache: bool | None, cache_read: int | None, cache_write: int | None
) -> int:
    """Inclusive input contains both cache reads and cache writes; strip the known ones."""
    if includes_cache is not True:
        return sent
    return max(sent - (cache_read or 0) - (cache_write or 0), 0)


def calculate_cost(
    record: RunRecord,
    baseline: Mapping[str, RateSet | None],
    history: Sequence[PricingVersion],
) -> CostResult:
    if record.tokens_sent is None or record.tokens_returned is None:
        return CostResult(None, "missing_usage", None)
    rates, version = resolve_rates(record.model, record.timestamp, baseline, history)
    if rates is None:
        return CostResult(None, "missing_rate", None)
    input_tokens = _ordinary_input(
        record.tokens_sent, record.sent_includes_cache_read, record.cache_read, record.cache_write
    )
    cache_read = 0 if record.cache_read is None else record.cache_read
    cache_write = 0 if record.cache_write is None else record.cache_write
    amount = (
        input_tokens * rates.input
        + record.tokens_returned * rates.output
        + cache_read * rates.cache_read
        + cache_write * rates.cache_write
    ) / _UNIT_TOKENS
    complete = (
        record.cache_read is not None
        and record.cache_write is not None
        and record.sent_includes_cache_read is not None
        and record.cache_read_unknown_responses == 0
        and record.cache_write_unknown_responses == 0
    )
    return CostResult(amount, "exact" if complete else "upper_bound", version)


def calculate_call_cost(
    call: ToolCall,
    baseline: Mapping[str, RateSet | None],
    history: Sequence[PricingVersion],
) -> CostResult:
    """Price one tool-usage call (spec §7)."""
    if call.tokens_sent is None or call.tokens_returned is None:
        return CostResult(None, "missing_usage", None)
    if call.model is None or call.speed not in (None, "standard"):
        return CostResult(None, "missing_rate", None)
    rates, version = resolve_rates(call.model, call.at, baseline, history)
    if rates is None:
        return CostResult(None, "missing_rate", None)
    input_tokens = _ordinary_input(
        call.tokens_sent, call.sent_includes_cache_read, call.cache_read, call.cache_write
    )
    cache_read = 0 if call.cache_read is None else call.cache_read
    cache_write = 0 if call.cache_write is None else call.cache_write
    amount = (
        input_tokens * rates.input
        + call.tokens_returned * rates.output
        + cache_read * rates.cache_read
        + cache_write * rates.cache_write
    ) / _UNIT_TOKENS
    exact = (
        call.cache_read is not None
        and call.cache_write is not None
        and not call.cache_write_5m
    )
    return CostResult(amount, "exact" if exact else "upper_bound", version)


def _known_total(
    runs: Sequence[RunRecord],
    baseline: Mapping[str, RateSet | None],
    history: Sequence[PricingVersion],
) -> float:
    total = 0.0
    for run in runs:
        cost = calculate_cost(run, baseline, history)
        if cost.amount is not None:
            total += cost.amount
    return total


def preview_pricing(
    candidate: Mapping[str, object],
    runs: Sequence[RunRecord],
    baseline: Mapping[str, RateSet | None],
    history: Sequence[PricingVersion],
    *,
    now: datetime,
) -> PricingPreview:
    model = candidate.get("model")
    if not isinstance(model, str) or not model:
        raise ValueError("candidate.model must be a non-empty string")
    effective_at = normalize_utc(str(candidate.get("effective_at")))
    rates = validate_rate_set(candidate.get("rates"), "candidate.rates")

    projected_version = PricingVersion(
        schema=1,
        version_id="__preview__",
        model=model,
        effective_at=effective_at,
        created_at=now.isoformat(),
        rates=rates,
        note="",
    )
    projected_history = [*history, projected_version]

    matching = [run for run in runs if run.model == model]
    warnings: list[str] = []
    affected = 0
    newly_priced = 0
    superseded = 0
    for run in matching:
        old = calculate_cost(run, baseline, history)
        _, new_version = resolve_rates(
            run.model, run.timestamp, baseline, projected_history
        )
        if new_version != "__preview__":
            # An existing later-effective version still wins for this run.
            continue
        affected += 1
        if old.amount is None:
            newly_priced += 1
        else:
            superseded += 1

    old_total = _known_total(runs, baseline, history)
    projected_total = _known_total(runs, baseline, projected_history)
    return PricingPreview(
        model=model,
        effective_at=effective_at,
        matching_runs=len(matching),
        affected_runs=affected,
        newly_priced_runs=newly_priced,
        superseded_runs=superseded,
        old_known_total=old_total,
        projected_known_total=projected_total,
        delta=projected_total - old_total,
        warnings=tuple(warnings),
    )


def append_pricing_version(
    path: Path,
    candidate: Mapping[str, object],
    runs: Sequence[RunRecord],
    baseline: Mapping[str, RateSet | None],
    *,
    now: datetime,
    locks: StateLocks,
) -> tuple[PricingVersion, PricingPreview]:
    with locks.write:
        history, _ = load_pricing_history(path)
        preview = preview_pricing(candidate, runs, baseline, history, now=now)
        version = validate_pricing_version(
            {
                "schema": 1,
                "version_id": str(uuid.uuid4()),
                "model": candidate.get("model"),
                "effective_at": candidate.get("effective_at"),
                "created_at": now.isoformat(),
                "rates": candidate.get("rates"),
                "note": candidate.get("note", ""),
            },
            "candidate",
        )
        append_jsonl(path, [asdict(version)], locks.write)
        return version, preview

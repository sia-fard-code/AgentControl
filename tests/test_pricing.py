from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from agent_control.models import PricingVersion, RateSet, RunRecord
from agent_control.pricing import (
    append_pricing_version,
    calculate_call_cost,
    calculate_cost,
    load_baseline,
    load_pricing_history,
    preview_pricing,
    resolve_rates,
    validate_rate_set,
)
from agent_control.storage import StateLocks
from agent_control.tool_calls import ToolCall

NOW = datetime(2026, 9, 17, 12, 0, tzinfo=timezone.utc)


def rate_set(
    *, input: float = 0.0, output: float = 0.0, cache_read: float = 0.0, cache_write: float = 0.0
) -> RateSet:
    return RateSet(
        input=input, output=output, cache_read=cache_read, cache_write=cache_write
    )


def run_record(
    *,
    run_id: str = "r",
    model: str = "model",
    timestamp: str = "2026-09-16T12:00:00+00:00",
    tokens_sent: int | None = 100,
    tokens_returned: int | None = 20,
    cache_read: int | None = 0,
    cache_write: int | None = 0,
    sent_includes_cache_read: bool | None = False,
    cache_read_unknown_responses: int = 0,
    cache_write_unknown_responses: int = 0,
) -> RunRecord:
    return RunRecord(
        schema=1,
        run_id=run_id,
        revision=1,
        project_id="p",
        timestamp=timestamp,
        ended_at=None,
        status="complete",
        label="demo",
        model=model,
        effort=None,
        tool_mode=None,
        write_mode=None,
        tool_calls=1,
        refused=0,
        tokens_sent=tokens_sent,
        tokens_returned=tokens_returned,
        cache_read=cache_read,
        cache_write=cache_write,
        sent_includes_cache_read=sent_includes_cache_read,
        tokens_sent_unknown_responses=0,
        tokens_returned_unknown_responses=0,
        cache_read_unknown_responses=cache_read_unknown_responses,
        cache_write_unknown_responses=cache_write_unknown_responses,
        usage_source="usage_journal",
        usage_artifact="artifact.usage.jsonl",
        transcript_artifact="artifact.md",
        provenance=("machine_index",),
        lifecycle_inferred=False,
        observed_at="2026-09-16T12:10:00+00:00",
    )


def _write_baseline(path: Path, models: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "schema": 1,
                "currency": "USD",
                "unit_tokens": 1000000,
                "initialized": True,
                "models": models,
            }
        ),
        encoding="utf-8",
    )


def test_validate_rate_set_requires_four_finite_nonnegative_rates() -> None:
    good = validate_rate_set(
        {"input": 5.0, "output": 15.0, "cache_read": 0.5, "cache_write": 6.0}, "ctx"
    )
    assert good == rate_set(input=5.0, output=15.0, cache_read=0.5, cache_write=6.0)
    for bad in (
        {"input": 5.0, "output": 15.0, "cache_read": 0.5},
        {"input": -1.0, "output": 1.0, "cache_read": 0.0, "cache_write": 0.0},
        {"input": float("inf"), "output": 1.0, "cache_read": 0.0, "cache_write": 0.0},
    ):
        with pytest.raises(ValueError):
            validate_rate_set(bad, "ctx")


def test_openai_prompt_tokens_do_not_double_bill_cached_input() -> None:
    row = run_record(
        tokens_sent=100,
        cache_read=60,
        cache_write=0,
        tokens_returned=0,
        sent_includes_cache_read=True,
    )
    result = calculate_cost(row, {"model": rate_set(input=5, cache_read=0.5)}, [])
    assert result.amount == pytest.approx((40 * 5 + 60 * 0.5) / 1_000_000)
    assert result.classification == "exact"


def test_unknown_cache_inclusion_keeps_all_sent_at_input_rate_upper_bound() -> None:
    row = run_record(
        tokens_sent=100,
        cache_read=None,
        cache_write=None,
        sent_includes_cache_read=None,
        cache_read_unknown_responses=1,
        cache_write_unknown_responses=1,
    )
    result = calculate_cost(row, {"model": rate_set(input=5, output=15)}, [])
    assert result.amount == pytest.approx((100 * 5 + 20 * 15) / 1_000_000)
    assert result.classification == "upper_bound"


def test_missing_usage_and_missing_rate_classifications() -> None:
    missing_usage = calculate_cost(
        run_record(tokens_sent=None), {"model": rate_set(input=5)}, []
    )
    assert missing_usage.amount is None
    assert missing_usage.classification == "missing_usage"
    missing_rate = calculate_cost(run_record(model="other"), {"model": None}, [])
    assert missing_rate.amount is None
    assert missing_rate.classification == "missing_rate"


def test_resolve_rates_uses_newest_effective_before_start() -> None:
    history = [
        PricingVersion(
            schema=1,
            version_id="v1",
            model="model",
            effective_at="2026-09-01T00:00:00+00:00",
            created_at="2026-09-01T00:00:00+00:00",
            rates=rate_set(input=4),
            note="",
        ),
        PricingVersion(
            schema=1,
            version_id="v2",
            model="model",
            effective_at="2026-09-16T00:00:00+00:00",
            created_at="2026-09-16T00:00:00+00:00",
            rates=rate_set(input=6),
            note="",
        ),
        PricingVersion(
            schema=1,
            version_id="v3",
            model="model",
            effective_at="2026-12-01T00:00:00+00:00",
            created_at="2026-12-01T00:00:00+00:00",
            rates=rate_set(input=9),
            note="",
        ),
    ]
    rates, version = resolve_rates(
        "model", "2026-09-16T12:00:00+00:00", {"model": rate_set(input=1)}, history
    )
    assert rates is not None and rates.input == 6
    assert version == "v2"


def test_resolve_rates_tie_favors_later_appended_version() -> None:
    history = [
        PricingVersion(
            schema=1,
            version_id="early",
            model="model",
            effective_at="2026-09-16T00:00:00+00:00",
            created_at="2026-09-16T00:00:00+00:00",
            rates=rate_set(input=6),
            note="",
        ),
        PricingVersion(
            schema=1,
            version_id="late",
            model="model",
            effective_at="2026-09-16T00:00:00+00:00",
            created_at="2026-09-16T01:00:00+00:00",
            rates=rate_set(input=7),
            note="",
        ),
    ]
    rates, version = resolve_rates("model", "2026-09-17T00:00:00+00:00", {}, history)
    assert rates is not None and rates.input == 7
    assert version == "late"


def test_load_baseline_reads_models_and_nulls(tmp_path: Path) -> None:
    path = tmp_path / "pricing.json"
    _write_baseline(
        path,
        {
            "model": {
                "input": 5.0,
                "output": 15.0,
                "cache_read": 0.5,
                "cache_write": 6.0,
            },
            "unpriced": None,
        },
    )
    baseline = load_baseline(path)
    assert baseline["model"] == rate_set(
        input=5.0, output=15.0, cache_read=0.5, cache_write=6.0
    )
    assert baseline["unpriced"] is None


def test_uninitialized_baseline_is_empty(tmp_path: Path) -> None:
    path = tmp_path / "pricing.json"
    path.write_text(
        json.dumps(
            {
                "schema": 1,
                "currency": "USD",
                "unit_tokens": 1000000,
                "initialized": False,
                "models": {},
            }
        ),
        encoding="utf-8",
    )
    assert load_baseline(path) == {}


def test_preview_reports_impact_without_writing_history(tmp_path: Path) -> None:
    history_path = tmp_path / "pricing_history.jsonl"
    runs = [run_record(run_id="a", model="model"), run_record(run_id="b", model="other")]
    preview = preview_pricing(
        {
            "model": "model",
            "effective_at": "2026-01-01T00:00:00+00:00",
            "rates": {"input": 5, "output": 15, "cache_read": 0.5, "cache_write": 6},
        },
        runs,
        {"model": None},
        [],
        now=NOW,
    )
    assert preview.matching_runs == 1
    assert preview.newly_priced_runs == 1
    assert preview.projected_known_total > preview.old_known_total
    assert not history_path.exists()


def test_append_pricing_version_writes_history_and_reprices(
    tmp_path: Path,
) -> None:
    history_path = tmp_path / "pricing_history.jsonl"
    runs = [run_record(model="model")]
    version, preview = append_pricing_version(
        history_path,
        {
            "model": "model",
            "effective_at": "2026-01-01T00:00:00+00:00",
            "rates": {"input": 5, "output": 15, "cache_read": 0.5, "cache_write": 6},
            "note": "genuine edit",
        },
        runs,
        {"model": None},
        now=NOW,
        locks=StateLocks(),
    )
    assert history_path.exists()
    assert version.version_id
    assert version.created_at == NOW.isoformat()
    assert preview.matching_runs == 1
    stored, diagnostics = load_pricing_history(history_path)
    assert len(stored) == 1
    assert diagnostics == ()


def test_invalid_pricing_append_does_not_create_history(tmp_path: Path) -> None:
    history_path = tmp_path / "pricing_history.jsonl"
    with pytest.raises(ValueError):
        append_pricing_version(
            history_path,
            {
                "model": "model",
                "effective_at": "not-a-timestamp",
                "rates": {"input": 5, "output": 15, "cache_read": 0.5, "cache_write": 6},
            },
            [run_record()],
            {"model": None},
            now=NOW,
            locks=StateLocks(),
        )
    assert not history_path.exists()


def test_preview_does_not_mutate_baseline(tmp_path: Path) -> None:
    baseline = {"model": None}
    preview_pricing(
        {
            "model": "model",
            "effective_at": "2026-01-01T00:00:00+00:00",
            "rates": {"input": 5, "output": 15, "cache_read": 0.5, "cache_write": 6},
        },
        [run_record()],
        baseline,
        [],
        now=NOW,
    )
    assert baseline == {"model": None}


def tool_call(**overrides: object) -> ToolCall:
    values: dict[str, object] = {
        "schema": 1,
        "tool": "claude_code",
        "key": "k1",
        "revision": 1,
        "session": "s1",
        "root_session": "s1",
        "at": "2026-09-17T11:00:00+00:00",
        "model": "model",
        "cwd": None,
        "project_id": None,
        "tokens_sent": 1000,
        "tokens_returned": 100,
        "cache_read": 2000,
        "cache_write": 300,
        "cache_write_5m": None,
        "reasoning": None,
        "sent_includes_cache_read": False,
        "speed": None,
        "recorded_at": "2026-09-17T11:00:01+00:00",
    }
    values.update(overrides)
    return ToolCall(**values)  # type: ignore[arg-type]


CALL_RATES = {"model": rate_set(input=1, output=10, cache_read=0.1, cache_write=2)}


def test_call_cost_exact_anthropic_shape() -> None:
    result = calculate_call_cost(tool_call(), CALL_RATES, [])
    assert result.classification == "exact"
    expected = (1000 * 1 + 100 * 10 + 2000 * 0.1 + 300 * 2) / 1_000_000
    assert result.amount == pytest.approx(expected)
    assert result.rate_version_id is None


def test_call_cost_openai_shape_subtracts_cache_read() -> None:
    call = tool_call(tokens_sent=5000, cache_read=2000, cache_write=0,
                     sent_includes_cache_read=True, tool="codex")
    result = calculate_call_cost(call, CALL_RATES, [])
    assert result.classification == "exact"
    expected = (3000 * 1 + 100 * 10 + 2000 * 0.1) / 1_000_000
    assert result.amount == pytest.approx(expected)


# (sent, read, write, includes, expected ordinary input). Inclusive input
# contains both cache reads and cache writes (OpenAI prompt-caching docs).
INCLUSION_CASES = [
    pytest.param(12000, 4000, 7000, True, 1000, id="inclusive_read_and_write"),
    pytest.param(12000, None, 7000, True, 5000, id="read_unknown_write_known"),
    pytest.param(12000, 4000, 7000, False, 12000, id="explicit_exclusive"),
    pytest.param(12000, 4000, 7000, None, 12000, id="unknown_inclusion"),
    pytest.param(5000, 4000, 7000, True, 0, id="clamped_at_zero"),
]


def _inclusion_expected(ordinary: int, read: int | None, write: int) -> float:
    return (ordinary * 1 + (read or 0) * 0.1 + write * 2) / 1_000_000


@pytest.mark.parametrize("sent,read,write,includes,ordinary", INCLUSION_CASES)
def test_run_cost_inclusive_input_subtracts_reads_and_writes(
    sent: int, read: int | None, write: int, includes: bool | None, ordinary: int
) -> None:
    row = run_record(tokens_sent=sent, tokens_returned=0, cache_read=read,
                     cache_write=write, sent_includes_cache_read=includes)
    result = calculate_cost(row, CALL_RATES, [])
    assert result.amount == pytest.approx(_inclusion_expected(ordinary, read, write))


@pytest.mark.parametrize("sent,read,write,includes,ordinary", INCLUSION_CASES)
def test_call_cost_inclusive_input_subtracts_reads_and_writes(
    sent: int, read: int | None, write: int, includes: bool | None, ordinary: int
) -> None:
    call = tool_call(tokens_sent=sent, tokens_returned=0, cache_read=read,
                     cache_write=write, sent_includes_cache_read=includes)
    result = calculate_call_cost(call, CALL_RATES, [])
    assert result.amount == pytest.approx(_inclusion_expected(ordinary, read, write))


def test_call_cost_five_minute_writes_are_upper_bound() -> None:
    result = calculate_call_cost(tool_call(cache_write_5m=50), CALL_RATES, [])
    assert result.classification == "upper_bound"
    assert result.amount is not None
    zero = calculate_call_cost(tool_call(cache_write_5m=0), CALL_RATES, [])
    assert zero.classification == "exact"


def test_call_cost_null_cache_write_is_upper_bound() -> None:
    result = calculate_call_cost(tool_call(cache_write=None), CALL_RATES, [])
    assert result.classification == "upper_bound"
    expected = (1000 * 1 + 100 * 10 + 2000 * 0.1) / 1_000_000
    assert result.amount == pytest.approx(expected)


def test_call_cost_fast_speed_is_missing_rate() -> None:
    result = calculate_call_cost(tool_call(speed="fast"), CALL_RATES, [])
    assert result.classification == "missing_rate"
    assert result.amount is None
    standard = calculate_call_cost(tool_call(speed="standard"), CALL_RATES, [])
    assert standard.classification == "exact"


def test_call_cost_null_model_is_missing_rate() -> None:
    result = calculate_call_cost(tool_call(model=None), CALL_RATES, [])
    assert result.classification == "missing_rate"
    assert result.amount is None
    unknown = calculate_call_cost(tool_call(model="other"), CALL_RATES, [])
    assert unknown.classification == "missing_rate"


def test_call_cost_null_tokens_returned_is_missing_usage() -> None:
    result = calculate_call_cost(tool_call(tokens_returned=None, model=None), CALL_RATES, [])
    assert result.classification == "missing_usage"
    assert result.amount is None


def test_call_cost_history_version_before_at_wins() -> None:
    history = [
        PricingVersion(
            schema=1,
            version_id="v1",
            model="model",
            effective_at="2026-09-01T00:00:00+00:00",
            created_at="2026-09-01T00:00:00+00:00",
            rates=rate_set(input=5),
            note="",
        ),
        PricingVersion(
            schema=1,
            version_id="v2",
            model="model",
            effective_at="2026-12-01T00:00:00+00:00",
            created_at="2026-12-01T00:00:00+00:00",
            rates=rate_set(input=50),
            note="",
        ),
    ]
    result = calculate_call_cost(tool_call(), CALL_RATES, history)
    assert result.rate_version_id == "v1"
    assert result.amount == pytest.approx(1000 * 5 / 1_000_000)


# Source: https://platform.claude.com/docs/en/models/sonnet-5-5/overview (verified 2026-10-28):
# input 2, output 10, cache read 0.10, cache write 1h 4 USD per million tokens.
@pytest.mark.parametrize(
    "model_id", ["anthropic_tool_call.claude-sonnet-5-5-think", "claude-sonnet-5-5"]
)
def test_sonnet_5_5_baseline_matches_published_rates(tmp_path: Path, model_id: str) -> None:
    import shutil

    path = tmp_path / "pricing.json"
    shutil.copy(Path(__file__).resolve().parents[1] / "config" / "pricing.json", path)
    baseline = load_baseline(path)
    assert baseline[model_id] == rate_set(input=2.0, output=10.0, cache_read=0.1, cache_write=4.0)
    result = calculate_call_cost(tool_call(model=model_id), baseline, [])
    assert result.amount == pytest.approx((1000 * 2 + 100 * 10 + 2000 * 0.1 + 300 * 4) / 1_000_000)

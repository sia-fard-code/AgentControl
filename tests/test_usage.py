import json
import math
from pathlib import Path

import pytest

from agent_control.usage import (
    aggregate_usage_records,
    normalize_usage_mapping,
    read_footer_usage,
    read_messages_usage,
    read_usage_journal,
    select_usage_evidence,
    validate_counter,
)


def test_openai_nested_cache_details_preserve_zero_and_shape() -> None:
    totals = normalize_usage_mapping(
        {
            "prompt_tokens": 100,
            "completion_tokens": 20,
            "prompt_tokens_details": {
                "cached_tokens": 60,
                "cache_write_tokens": 0,
            },
        }
    )

    assert (totals.tokens_sent, totals.tokens_returned) == (100, 20)
    assert (totals.cache_read, totals.cache_write) == (60, 0)
    assert totals.sent_includes_cache_read is True
    assert totals.response_count == 1
    assert totals.cache_write_unknown_responses == 0


def test_anthropic_cache_fields_are_mapped_without_fabricating_values() -> None:
    totals = normalize_usage_mapping(
        {
            "input_tokens": 40,
            "output_tokens": 0,
            "cache_read_input_tokens": 5,
            "cache_creation_input_tokens": None,
        }
    )

    assert (totals.tokens_sent, totals.tokens_returned) == (40, 0)
    assert totals.cache_read == 5
    assert totals.cache_write is None
    assert totals.sent_includes_cache_read is False
    assert totals.cache_write_unknown_responses == 1


def test_explicit_zero_is_distinct_from_unknown() -> None:
    zero = normalize_usage_mapping({"input_tokens": 0, "output_tokens": 0})
    unknown = normalize_usage_mapping({"input_tokens": None, "output_tokens": None})

    assert zero.tokens_sent == 0
    assert zero.tokens_sent_unknown_responses == 0
    assert unknown.tokens_sent is None
    assert unknown.tokens_sent_unknown_responses == 1


@pytest.mark.parametrize("bad", [-1, True, 1.5, "4", math.inf])
def test_invalid_counter_is_rejected(bad: object) -> None:
    with pytest.raises(ValueError, match="input_tokens"):
        normalize_usage_mapping({"input_tokens": bad, "output_tokens": 0})


def test_required_counter_rejects_null() -> None:
    with pytest.raises(ValueError, match="required"):
        validate_counter(None, "counter", required=True)


def test_mixed_provider_shapes_are_rejected_as_ambiguous() -> None:
    with pytest.raises(ValueError, match="ambiguous"):
        normalize_usage_mapping(
            {
                "prompt_tokens": 1,
                "completion_tokens": 2,
                "input_tokens": 1,
                "output_tokens": 2,
            }
        )


def test_present_prompt_details_must_be_an_object() -> None:
    with pytest.raises(ValueError, match="prompt_tokens_details"):
        normalize_usage_mapping(
            {
                "prompt_tokens": 1,
                "completion_tokens": 2,
                "prompt_tokens_details": [],
            }
        )


def test_responses_usage_counts_cache_inside_input_tokens() -> None:
    totals = normalize_usage_mapping(
        {
            "input_tokens": 100,
            "input_tokens_details": {
                "cached_tokens": 60,
                "cache_write_tokens": 0,
            },
            "output_tokens": 20,
            "output_tokens_details": {"reasoning_tokens": 7},
            "total_tokens": 120,
        }
    )

    assert (totals.tokens_sent, totals.tokens_returned) == (100, 20)
    assert (totals.cache_read, totals.cache_write) == (60, 0)
    assert totals.sent_includes_cache_read is True
    assert totals.response_count == 1
    assert totals.cache_read_unknown_responses == 0
    assert totals.cache_write_unknown_responses == 0


def test_responses_usage_missing_details_are_unknown_not_zero() -> None:
    no_cache_keys = normalize_usage_mapping(
        {
            "input_tokens": 10,
            "input_tokens_details": {},
            "output_tokens": 3,
        }
    )
    output_details_only = normalize_usage_mapping(
        {
            "input_tokens": 10,
            "output_tokens": 3,
            "output_tokens_details": {"reasoning_tokens": None},
        }
    )

    for totals in (no_cache_keys, output_details_only):
        assert totals.sent_includes_cache_read is True
        assert (totals.tokens_sent, totals.tokens_returned) == (10, 3)
        assert totals.cache_read is None
        assert totals.cache_write is None
        assert totals.cache_read_unknown_responses == 1
        assert totals.cache_write_unknown_responses == 1


@pytest.mark.parametrize(
    "mapping",
    [
        {"input_tokens": 40, "output_tokens": 2},
        {
            "input_tokens": 40,
            "output_tokens": 2,
            "cache_read_input_tokens": 5,
            "cache_creation_input_tokens": 1,
        },
    ],
)
def test_anthropic_usage_is_still_classified_as_anthropic(
    mapping: dict[str, object],
) -> None:
    totals = normalize_usage_mapping(mapping)

    assert totals.sent_includes_cache_read is False
    assert totals.tokens_sent == 40
    assert totals.cache_read == mapping.get("cache_read_input_tokens")
    assert totals.cache_write == mapping.get("cache_creation_input_tokens")


def test_openai_usage_is_unchanged_by_responses_shape() -> None:
    totals = normalize_usage_mapping(
        {
            "prompt_tokens": 50,
            "completion_tokens": 5,
            "prompt_tokens_details": {"cached_tokens": 30},
        }
    )

    assert (totals.tokens_sent, totals.tokens_returned) == (50, 5)
    assert (totals.cache_read, totals.cache_write) == (30, None)
    assert totals.sent_includes_cache_read is True


@pytest.mark.parametrize("bad", [[], None, 3, "x"])
@pytest.mark.parametrize("key", ["input_tokens_details", "output_tokens_details"])
def test_present_responses_details_must_be_an_object(key: str, bad: object) -> None:
    with pytest.raises(ValueError, match=key):
        normalize_usage_mapping({"input_tokens": 1, "output_tokens": 2, key: bad})


@pytest.mark.parametrize(
    ("key", "field"),
    [
        ("input_tokens_details", "cached_tokens"),
        ("input_tokens_details", "cache_write_tokens"),
        ("output_tokens_details", "reasoning_tokens"),
    ],
)
def test_responses_detail_counters_are_validated(key: str, field: str) -> None:
    with pytest.raises(ValueError, match=f"{key}.{field}"):
        normalize_usage_mapping(
            {"input_tokens": 1, "output_tokens": 2, key: {field: -1}}
        )


def test_responses_details_with_anthropic_cache_keys_are_ambiguous() -> None:
    with pytest.raises(ValueError, match="ambiguous"):
        normalize_usage_mapping(
            {
                "input_tokens": 10,
                "output_tokens": 2,
                "input_tokens_details": {"cached_tokens": 4},
                "cache_read_input_tokens": 4,
            }
        )


def test_unrecognized_usage_shape_is_rejected() -> None:
    with pytest.raises(ValueError, match="recognized"):
        normalize_usage_mapping({"unrelated": 1})


def test_aggregate_usage_records_sums_known_values_and_counts_unknowns() -> None:
    totals = aggregate_usage_records(
        [
            {
                "input_tokens": 10,
                "output_tokens": 2,
                "cache_read_input_tokens": 0,
            },
            {
                "input_tokens": None,
                "output_tokens": 3,
                "cache_read_input_tokens": 5,
                "cache_creation_input_tokens": 1,
            },
        ]
    )

    assert totals.tokens_sent == 10
    assert totals.tokens_sent_unknown_responses == 1
    assert totals.tokens_returned == 5
    assert totals.cache_read == 5
    assert totals.cache_write == 1
    assert totals.cache_write_unknown_responses == 1
    assert totals.response_count == 2
    assert totals.sent_includes_cache_read is False


def test_aggregate_usage_records_rejects_provider_shape_conflict() -> None:
    with pytest.raises(ValueError, match="shape"):
        aggregate_usage_records(
            [
                {"input_tokens": 1, "output_tokens": 1},
                {"prompt_tokens": 1, "completion_tokens": 1},
            ]
        )


def test_aggregate_known_and_unknown_inclusion_keeps_shape_unknown() -> None:
    known = normalize_usage_mapping(
        {
            "prompt_tokens": 10,
            "completion_tokens": 2,
            "prompt_tokens_details": {"cached_tokens": 4},
        }
    )
    unknown = known.__class__(
        tokens_sent=3,
        tokens_returned=1,
        cache_read=None,
        cache_write=None,
        sent_includes_cache_read=None,
        response_count=1,
        tokens_sent_unknown_responses=0,
        tokens_returned_unknown_responses=0,
        cache_read_unknown_responses=1,
        cache_write_unknown_responses=1,
    )

    from agent_control.usage import _aggregate_normalized

    assert _aggregate_normalized([known, unknown]).sent_includes_cache_read is None


def _write_jsonl(path: Path, rows: list[object]) -> None:
    path.write_text(
        "".join(json.dumps(row, separators=(",", ":")) + "\n" for row in rows),
        encoding="utf-8",
    )


def test_usage_journal_wins_and_partial_cache_stays_partial(tmp_path: Path) -> None:
    transcript = tmp_path / "sample.md"
    messages = tmp_path / "sample.messages.jsonl"
    journal = tmp_path / "sample.usage.jsonl"
    transcript.write_text("Tokens sent: 99\nTokens returned: 88\n", encoding="utf-8")
    _write_jsonl(
        messages,
        [{"usage": {"input_tokens": 40, "output_tokens": 9}}],
    )
    journal.write_text(
        json.dumps(
            {
                "iter": 1,
                "phase": "loop",
                "raw": {
                    "input_tokens": 10,
                    "output_tokens": 2,
                    "cache_read_input_tokens": 5,
                },
            }
        )
        + "\n"
        + "{malformed\n"
        + json.dumps(
            {
                "iter": 2,
                "phase": "wrapup",
                "raw": {"input_tokens": 0, "output_tokens": 1},
            }
        )
        + "\n",
        encoding="utf-8",
    )

    evidence = select_usage_evidence(transcript, messages, journal)

    assert evidence.source == "usage_journal"
    assert evidence.artifact == str(journal)
    assert evidence.totals.tokens_sent == 10
    assert evidence.totals.tokens_returned == 3
    assert evidence.totals.cache_read == 5
    assert evidence.totals.cache_read_unknown_responses == 1
    assert evidence.diagnostics


def test_wholly_invalid_journal_falls_through_to_messages(tmp_path: Path) -> None:
    messages = tmp_path / "sample.messages.jsonl"
    journal = tmp_path / "sample.usage.jsonl"
    _write_jsonl(
        messages,
        [
            {"kind": "metadata", "usage": {"prompt_tokens": 12, "completion_tokens": 4}},
            {"kind": "metadata-without-accounting"},
        ],
    )
    journal.write_text('{"raw":{"input_tokens":true,"output_tokens":2}}\n', encoding="utf-8")

    evidence = select_usage_evidence(None, messages, journal)

    assert evidence.source == "messages"
    assert evidence.totals.tokens_sent == 12
    assert evidence.totals.tokens_returned == 4


def test_messages_reader_traverses_usage_metadata_only(tmp_path: Path) -> None:
    messages = tmp_path / "sample.messages.jsonl"
    _write_jsonl(
        messages,
        [
            {
                "kind": "response_metadata",
                "metadata": {
                    "usage": {
                        "input_tokens": 7,
                        "output_tokens": 2,
                        "cache_read_input_tokens": 0,
                    }
                },
            },
            {
                "kind": "user",
                "content": {"input_tokens": 900, "output_tokens": 80},
            },
            {"kind": "status", "counts": {"unrelated": 9}},
        ],
    )

    evidence = read_messages_usage(messages)

    assert evidence.source == "messages"
    assert evidence.totals.tokens_sent == 7
    assert evidence.totals.tokens_returned == 2
    assert evidence.totals.cache_read == 0


def test_footer_does_not_fabricate_cache_zero(tmp_path: Path) -> None:
    transcript = tmp_path / "sample.md"
    transcript.write_text(
        "## Accounting\n- Tokens sent: 1,234\n- Tokens returned: 56\n",
        encoding="utf-8",
    )

    evidence = read_footer_usage(transcript)

    assert evidence.source == "footer"
    assert evidence.totals.tokens_sent == 1234
    assert evidence.totals.tokens_returned == 56
    assert evidence.totals.cache_read is None
    assert evidence.totals.cache_write is None
    assert evidence.totals.response_count == 1


@pytest.mark.parametrize(
    "sent",
    ["-5", "+5", "1.5", "5 trailing", " 5 6"],
)
def test_footer_rejects_malformed_counters(tmp_path: Path, sent: str) -> None:
    transcript = tmp_path / "sample.md"
    transcript.write_text(
        f"## Accounting\n- Tokens sent: {sent}\n- Tokens returned: 2\n",
        encoding="utf-8",
    )

    assert read_footer_usage(transcript).source == "missing"


def test_footer_uses_accounting_section_not_body_text(tmp_path: Path) -> None:
    transcript = tmp_path / "sample.md"
    transcript.write_text(
        "Body example: Tokens sent: 900\nTokens returned: 80\n"
        "\n## Accounting\n- Tokens sent: 7\n- Tokens returned: 2\n",
        encoding="utf-8",
    )

    evidence = read_footer_usage(transcript)

    assert evidence.source == "footer"
    assert evidence.totals.tokens_sent == 7
    assert evidence.totals.tokens_returned == 2


def test_footer_does_not_cross_into_later_markdown_section(tmp_path: Path) -> None:
    transcript = tmp_path / "sample.md"
    transcript.write_text(
        "## Accounting\n- Tokens sent: 7\n"
        "\n## Unrelated example\n- Tokens returned: 99\n",
        encoding="utf-8",
    )

    assert read_footer_usage(transcript).source == "missing"


def test_missing_or_unrecognized_sources_return_missing_evidence(tmp_path: Path) -> None:
    transcript = tmp_path / "sample.md"
    transcript.write_text("No accounting footer is present.\n", encoding="utf-8")

    footer = read_footer_usage(transcript)
    selected = select_usage_evidence(transcript, None, None)

    assert footer.source == "missing"
    assert selected.source == "missing"
    assert selected.totals.tokens_sent is None
    assert selected.artifact is None


def test_direct_usage_journal_fields_preserve_reported_zero(tmp_path: Path) -> None:
    journal = tmp_path / "sample.usage.jsonl"
    _write_jsonl(
        journal,
        [
            {
                "iter": 1,
                "phase": "loop",
                "sent": 0,
                "recv": 0,
                "cache_read": None,
                "cache_write": 0,
            }
        ],
    )

    evidence = read_usage_journal(journal)

    assert evidence.source == "usage_journal"
    assert evidence.totals.tokens_sent == 0
    assert evidence.totals.cache_read is None
    assert evidence.totals.cache_read_unknown_responses == 1
    assert evidence.totals.cache_write == 0
    assert evidence.totals.sent_includes_cache_read is None


def test_usage_journal_preserves_openai_raw_shape_for_pricing(tmp_path: Path) -> None:
    journal = tmp_path / "sample.usage.jsonl"
    _write_jsonl(
        journal,
        [
            {
                "iter": 1,
                "phase": "loop",
                "sent": 100,
                "recv": 20,
                "cache_read": 60,
                "cache_write": 0,
                "raw": {
                    "prompt_tokens": 100,
                    "completion_tokens": 20,
                    "prompt_tokens_details": {
                        "cached_tokens": 60,
                        "cache_write_tokens": 0,
                    },
                },
            }
        ],
    )

    evidence = read_usage_journal(journal)

    assert evidence.source == "usage_journal"
    assert evidence.totals.sent_includes_cache_read is True


def _responses_raw(sent: int, recv: int, cached: int, written: int) -> dict:
    return {
        "input_tokens": sent,
        "input_tokens_details": {
            "cached_tokens": cached,
            "cache_write_tokens": written,
        },
        "output_tokens": recv,
        "output_tokens_details": {"reasoning_tokens": 1},
        "total_tokens": sent + recv,
    }


def test_select_usage_evidence_reads_responses_journal_and_messages(
    tmp_path: Path,
) -> None:
    messages = tmp_path / "sample.messages.jsonl"
    journal = tmp_path / "sample.usage.jsonl"
    _write_jsonl(
        messages,
        [
            {"kind": "metadata", "usage": _responses_raw(30, 6, 20, 0)},
            {"kind": "metadata", "usage": _responses_raw(40, 4, 25, 3)},
        ],
    )
    _write_jsonl(
        journal,
        [
            {
                "iter": 1,
                "phase": "loop",
                "sent": 100,
                "recv": 20,
                "cache_read": 60,
                "cache_write": 0,
                "raw": _responses_raw(100, 20, 60, 0),
            },
            {
                "iter": 2,
                "phase": "wrapup",
                "sent": 50,
                "recv": 5,
                "cache_read": 40,
                "cache_write": 2,
                "raw": _responses_raw(50, 5, 40, 2),
            },
        ],
    )

    evidence = select_usage_evidence(None, messages, journal)

    assert evidence.source == "usage_journal"
    assert evidence.diagnostics == ()
    assert (evidence.totals.tokens_sent, evidence.totals.tokens_returned) == (150, 25)
    assert (evidence.totals.cache_read, evidence.totals.cache_write) == (100, 2)
    assert evidence.totals.sent_includes_cache_read is True
    assert evidence.totals.response_count == 2

    journal.write_text("", encoding="utf-8")
    fallback = select_usage_evidence(None, messages, journal)

    assert fallback.source == "messages"
    assert (fallback.totals.tokens_sent, fallback.totals.tokens_returned) == (70, 10)
    assert (fallback.totals.cache_read, fallback.totals.cache_write) == (45, 3)
    assert fallback.totals.sent_includes_cache_read is True


@pytest.mark.parametrize(
    "row",
    [
        {
            "sent": -1,
            "recv": 2,
            "cache_read": 0,
            "cache_write": 0,
            "raw": {"input_tokens": 1, "output_tokens": 2},
        },
        {
            "sent": 1,
            "recv": 2,
            "cache_read": 0,
            "cache_write": 0,
            "raw": "not-an-object",
        },
        {
            "sent": 99,
            "recv": 2,
            "cache_read": 0,
            "cache_write": 0,
            "raw": {"input_tokens": 1, "output_tokens": 2},
        },
    ],
)
def test_journal_rejects_invalid_or_contradictory_dual_representation(
    tmp_path: Path, row: dict[str, object]
) -> None:
    journal = tmp_path / "sample.usage.jsonl"
    _write_jsonl(journal, [row])

    evidence = read_usage_journal(journal)

    assert evidence.source == "missing"
    assert any(item.code == "invalid_usage_record" for item in evidence.diagnostics)


def test_usage_diagnostic_keeps_physical_line_after_malformed_json(
    tmp_path: Path,
) -> None:
    journal = tmp_path / "sample.usage.jsonl"
    journal.write_text(
        '{"sent":1,"recv":1}\n'
        "{malformed\n"
        '{"sent":true,"recv":2}\n',
        encoding="utf-8",
    )

    evidence = read_usage_journal(journal)

    assert evidence.source == "usage_journal"
    invalid = [
        item for item in evidence.diagnostics if item.code == "invalid_usage_record"
    ]
    assert invalid and invalid[0].line == 3

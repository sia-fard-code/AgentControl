from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from pathlib import Path

from .models import Diagnostic, UsageEvidence, UsageSource, UsageTotals
from .storage import strict_json_loads

_OPENAI_KEYS = frozenset(
    {
        "prompt_tokens",
        "completion_tokens",
        "prompt_tokens_details",
    }
)
_ANTHROPIC_KEYS = frozenset(
    {
        "input_tokens",
        "output_tokens",
        "cache_read_input_tokens",
        "cache_creation_input_tokens",
    }
)
_ANTHROPIC_CACHE_KEYS = frozenset(
    {"cache_read_input_tokens", "cache_creation_input_tokens"}
)
_RESPONSES_DETAIL_KEYS = frozenset({"input_tokens_details", "output_tokens_details"})
_USAGE_KEYS = _OPENAI_KEYS | _ANTHROPIC_KEYS | _RESPONSES_DETAIL_KEYS
_JOURNAL_KEYS = frozenset({"sent", "recv", "cache_read", "cache_write"})
_ACCOUNTING_HEADING = re.compile(
    r"^#{1,6}\s+(?:token\s+)?(?:accounting|usage)\s*$",
    re.IGNORECASE | re.MULTILINE,
)
_NEXT_HEADING = re.compile(r"^#{1,6}\s+\S", re.MULTILINE)
_SENT_FOOTER = re.compile(
    r"^\s*[-*]?\s*\**tokens?\s+sent\s*:\**\s*"
    r"([0-9]+(?:,[0-9]{3})*)\s*$",
    re.IGNORECASE | re.MULTILINE,
)
_RETURNED_FOOTER = re.compile(
    r"^\s*[-*]?\s*\**tokens?\s+(?:returned|received)\s*:\**\s*"
    r"([0-9]+(?:,[0-9]{3})*)\s*$",
    re.IGNORECASE | re.MULTILINE,
)


def validate_counter(
    value: object, field: str, *, required: bool = False
) -> int | None:
    if value is None:
        if required:
            raise ValueError(f"{field} is required")
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{field} must be a non-negative integer or null")
    return value


def _unknown(value: int | None) -> int:
    return int(value is None)


def _details_mapping(mapping: Mapping[str, object], key: str) -> Mapping[str, object]:
    if key not in mapping:
        return {}
    details = mapping[key]
    if not isinstance(details, Mapping):
        raise ValueError(f"{key} must be an object")
    return details


def normalize_usage_mapping(mapping: Mapping[str, object]) -> UsageTotals:
    has_openai = any(key in mapping for key in _OPENAI_KEYS)
    has_anthropic = any(key in mapping for key in _ANTHROPIC_KEYS)

    if has_openai and has_anthropic:
        raise ValueError("usage mapping has ambiguous mixed provider shapes")
    if not has_openai and not has_anthropic:
        raise ValueError("usage mapping has no recognized usage shape")
    has_responses_details = any(key in mapping for key in _RESPONSES_DETAIL_KEYS)
    has_responses = (
        not has_openai
        and has_responses_details
        and any(key in mapping for key in ("input_tokens", "output_tokens"))
    )
    if has_responses and any(key in mapping for key in _ANTHROPIC_CACHE_KEYS):
        raise ValueError("usage mapping has ambiguous mixed provider shapes")

    if has_openai:
        tokens_sent = validate_counter(mapping.get("prompt_tokens"), "prompt_tokens")
        tokens_returned = validate_counter(
            mapping.get("completion_tokens"), "completion_tokens"
        )
        cache_read: int | None = None
        cache_write: int | None = None
        if "prompt_tokens_details" in mapping:
            details = mapping["prompt_tokens_details"]
            if not isinstance(details, Mapping):
                raise ValueError("prompt_tokens_details must be an object")
            cache_read = validate_counter(
                details.get("cached_tokens"), "prompt_tokens_details.cached_tokens"
            )
            cache_write = validate_counter(
                details.get("cache_write_tokens"),
                "prompt_tokens_details.cache_write_tokens",
            )
        sent_includes_cache_read = True
    elif has_responses:
        tokens_sent = validate_counter(mapping.get("input_tokens"), "input_tokens")
        tokens_returned = validate_counter(
            mapping.get("output_tokens"), "output_tokens"
        )
        input_details = _details_mapping(mapping, "input_tokens_details")
        output_details = _details_mapping(mapping, "output_tokens_details")
        cache_read = validate_counter(
            input_details.get("cached_tokens"), "input_tokens_details.cached_tokens"
        )
        cache_write = validate_counter(
            input_details.get("cache_write_tokens"),
            "input_tokens_details.cache_write_tokens",
        )
        validate_counter(
            output_details.get("reasoning_tokens"),
            "output_tokens_details.reasoning_tokens",
        )
        sent_includes_cache_read = True
    else:
        tokens_sent = validate_counter(mapping.get("input_tokens"), "input_tokens")
        tokens_returned = validate_counter(
            mapping.get("output_tokens"), "output_tokens"
        )
        cache_read = validate_counter(
            mapping.get("cache_read_input_tokens"), "cache_read_input_tokens"
        )
        cache_write = validate_counter(
            mapping.get("cache_creation_input_tokens"),
            "cache_creation_input_tokens",
        )
        sent_includes_cache_read = False

    return UsageTotals(
        tokens_sent=tokens_sent,
        tokens_returned=tokens_returned,
        cache_read=cache_read,
        cache_write=cache_write,
        sent_includes_cache_read=sent_includes_cache_read,
        response_count=1,
        tokens_sent_unknown_responses=_unknown(tokens_sent),
        tokens_returned_unknown_responses=_unknown(tokens_returned),
        cache_read_unknown_responses=_unknown(cache_read),
        cache_write_unknown_responses=_unknown(cache_write),
    )


def _sum_known(totals: Sequence[UsageTotals], field: str) -> int | None:
    values = [value for item in totals if (value := getattr(item, field)) is not None]
    return sum(values) if values else None


def _aggregate_normalized(totals: Sequence[UsageTotals]) -> UsageTotals:
    known_shapes = {
        item.sent_includes_cache_read
        for item in totals
        if item.sent_includes_cache_read is not None
    }
    if len(known_shapes) > 1:
        raise ValueError("usage records have conflicting provider shapes")
    shape = (
        None
        if any(item.sent_includes_cache_read is None for item in totals)
        else next(iter(known_shapes), None)
    )
    return UsageTotals(
        tokens_sent=_sum_known(totals, "tokens_sent"),
        tokens_returned=_sum_known(totals, "tokens_returned"),
        cache_read=_sum_known(totals, "cache_read"),
        cache_write=_sum_known(totals, "cache_write"),
        sent_includes_cache_read=shape,
        response_count=sum(item.response_count for item in totals),
        tokens_sent_unknown_responses=sum(
            item.tokens_sent_unknown_responses for item in totals
        ),
        tokens_returned_unknown_responses=sum(
            item.tokens_returned_unknown_responses for item in totals
        ),
        cache_read_unknown_responses=sum(
            item.cache_read_unknown_responses for item in totals
        ),
        cache_write_unknown_responses=sum(
            item.cache_write_unknown_responses for item in totals
        ),
    )


def aggregate_usage_records(
    records: Sequence[Mapping[str, object]],
) -> UsageTotals:
    return _aggregate_normalized(
        [normalize_usage_mapping(record) for record in records]
    )


def _missing_totals() -> UsageTotals:
    return UsageTotals(
        tokens_sent=None,
        tokens_returned=None,
        cache_read=None,
        cache_write=None,
        sent_includes_cache_read=None,
        response_count=0,
        tokens_sent_unknown_responses=0,
        tokens_returned_unknown_responses=0,
        cache_read_unknown_responses=0,
        cache_write_unknown_responses=0,
    )


def _missing_evidence(
    diagnostics: Sequence[Diagnostic] = (),
) -> UsageEvidence:
    return UsageEvidence(
        totals=_missing_totals(),
        source="missing",
        artifact=None,
        diagnostics=tuple(diagnostics),
    )


def _safe_diagnostic(
    code: str,
    source: Path,
    *,
    line: int | None = None,
) -> Diagnostic:
    return Diagnostic(
        code=code,
        message="usage accounting metadata is invalid",
        source=str(source),
        line=line,
    )


def _read_usage_rows(
    path: Path,
) -> tuple[list[tuple[int, Mapping[str, object]]], tuple[Diagnostic, ...]]:
    rows: list[tuple[int, Mapping[str, object]]] = []
    diagnostics: list[Diagnostic] = []
    try:
        with path.open("rb") as stream:
            for line_number, encoded in enumerate(stream, start=1):
                if not encoded.strip():
                    continue
                try:
                    value = strict_json_loads(encoded)
                except (UnicodeError, ValueError):
                    diagnostics.append(
                        _safe_diagnostic(
                            "malformed_usage_jsonl", path, line=line_number
                        )
                    )
                    continue
                if not isinstance(value, Mapping):
                    diagnostics.append(
                        _safe_diagnostic(
                            "non_object_usage_jsonl", path, line=line_number
                        )
                    )
                    continue
                rows.append((line_number, value))
    except OSError:
        diagnostics.append(_safe_diagnostic("unreadable_usage_jsonl", path))
    return rows, tuple(diagnostics)


def _journal_raw_mapping(row: Mapping[str, object]) -> Mapping[str, object] | None:
    if "raw" not in row:
        return None
    raw = row["raw"]
    if not isinstance(raw, Mapping) or not any(key in raw for key in _USAGE_KEYS):
        raise ValueError("raw must contain a recognized usage mapping")
    return raw


def _normalize_direct_journal_row(row: Mapping[str, object]) -> UsageTotals:
    tokens_sent = validate_counter(row.get("sent"), "sent")
    tokens_returned = validate_counter(row.get("recv"), "recv")
    cache_read = validate_counter(row.get("cache_read"), "cache_read")
    cache_write = validate_counter(row.get("cache_write"), "cache_write")
    return UsageTotals(
        tokens_sent=tokens_sent,
        tokens_returned=tokens_returned,
        cache_read=cache_read,
        cache_write=cache_write,
        sent_includes_cache_read=None,
        response_count=1,
        tokens_sent_unknown_responses=_unknown(tokens_sent),
        tokens_returned_unknown_responses=_unknown(tokens_returned),
        cache_read_unknown_responses=_unknown(cache_read),
        cache_write_unknown_responses=_unknown(cache_write),
    )


def _read_normalized_evidence(
    path: Path,
    source: UsageSource,
    mappings: Sequence[tuple[int, Mapping[str, object]]],
    diagnostics: Sequence[Diagnostic],
) -> UsageEvidence:
    accepted: list[UsageTotals] = []
    all_diagnostics = list(diagnostics)
    expected_shape: bool | None = None

    for line, mapping in mappings:
        try:
            normalized = normalize_usage_mapping(mapping)
        except ValueError:
            all_diagnostics.append(
                _safe_diagnostic("invalid_usage_record", path, line=line)
            )
            continue
        shape = normalized.sent_includes_cache_read
        if expected_shape is not None and shape != expected_shape:
            all_diagnostics.append(
                _safe_diagnostic("conflicting_usage_shape", path, line=line)
            )
            continue
        expected_shape = shape
        accepted.append(normalized)

    if not accepted:
        return _missing_evidence(all_diagnostics)
    return UsageEvidence(
        totals=_aggregate_normalized(accepted),
        source=source,
        artifact=str(path),
        diagnostics=tuple(all_diagnostics),
    )


def read_usage_journal(path: Path) -> UsageEvidence:
    rows, diagnostics = _read_usage_rows(path)
    accepted: list[UsageTotals] = []
    extra_diagnostics: list[Diagnostic] = list(diagnostics)
    expected_shape: bool | None = None

    for line, row in rows:
        # MCP observation rows share the engine journal but are not gateway
        # responses. Do not inflate unknown-response counts or erase provider
        # shape with their intentionally null counters. Content stays unimported.
        if row.get("phase") in ("mcp_start", "mcp_end", "mcp_wait"):
            continue
        try:
            has_direct = any(key in row for key in _JOURNAL_KEYS)
            direct = _normalize_direct_journal_row(row) if has_direct else None
            raw_mapping = _journal_raw_mapping(row)
            raw = (
                normalize_usage_mapping(raw_mapping)
                if raw_mapping is not None
                else None
            )
            if direct is None and raw is None:
                extra_diagnostics.append(
                    _safe_diagnostic("unrecognized_usage_record", path, line=line)
                )
                continue
            if direct is not None and raw is not None:
                comparable = (
                    ("tokens_sent", "sent"),
                    ("tokens_returned", "recv"),
                    ("cache_read", "cache_read"),
                    ("cache_write", "cache_write"),
                )
                if any(
                    key in row and getattr(direct, field) != getattr(raw, field)
                    for field, key in comparable
                ):
                    raise ValueError("journal counters contradict raw usage")
            normalized = raw if raw is not None else direct
            assert normalized is not None
        except ValueError:
            extra_diagnostics.append(
                _safe_diagnostic("invalid_usage_record", path, line=line)
            )
            continue

        shape = normalized.sent_includes_cache_read
        if shape is not None:
            if expected_shape is not None and shape != expected_shape:
                extra_diagnostics.append(
                    _safe_diagnostic("conflicting_usage_shape", path, line=line)
                )
                continue
            expected_shape = shape
        accepted.append(normalized)

    if not accepted:
        return _missing_evidence(extra_diagnostics)
    return UsageEvidence(
        totals=_aggregate_normalized(accepted),
        source="usage_journal",
        artifact=str(path),
        diagnostics=tuple(extra_diagnostics),
    )


def _iter_usage_mappings(value: object) -> list[Mapping[str, object]]:
    found: list[Mapping[str, object]] = []
    excluded = frozenset(
        {"content", "prompt", "assistant", "tool", "tool_result", "arguments"}
    )

    def collect_usage(item: object) -> None:
        if isinstance(item, Mapping):
            if any(key in item for key in _USAGE_KEYS):
                found.append(item)
                return
            for child in item.values():
                collect_usage(child)
        elif isinstance(item, list):
            for child in item:
                collect_usage(child)

    def visit(item: object) -> None:
        if isinstance(item, Mapping):
            for key, child in item.items():
                normalized_key = str(key).casefold()
                if normalized_key in {"usage", "usage_metadata"}:
                    collect_usage(child)
                elif normalized_key not in excluded:
                    visit(child)
        elif isinstance(item, list):
            for child in item:
                visit(child)

    visit(value)
    return found


def read_messages_usage(path: Path) -> UsageEvidence:
    rows, diagnostics = _read_usage_rows(path)
    mappings: list[tuple[int, Mapping[str, object]]] = []
    for line, row in rows:
        mappings.extend((line, mapping) for mapping in _iter_usage_mappings(row))
    return _read_normalized_evidence(path, "messages", mappings, diagnostics)


def read_footer_usage(path: Path) -> UsageEvidence:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return _missing_evidence((_safe_diagnostic("unreadable_usage_footer", path),))

    headings = list(_ACCOUNTING_HEADING.finditer(text))
    if not headings:
        return _missing_evidence()
    footer = text[headings[-1].end() :]
    next_heading = _NEXT_HEADING.search(footer)
    if next_heading is not None:
        footer = footer[: next_heading.start()]
    sent_match = _SENT_FOOTER.search(footer)
    returned_match = _RETURNED_FOOTER.search(footer)
    if sent_match is None or returned_match is None:
        return _missing_evidence()

    sent = int(sent_match.group(1).replace(",", ""))
    returned = int(returned_match.group(1).replace(",", ""))
    return UsageEvidence(
        totals=UsageTotals(
            tokens_sent=sent,
            tokens_returned=returned,
            cache_read=None,
            cache_write=None,
            sent_includes_cache_read=None,
            response_count=1,
            tokens_sent_unknown_responses=0,
            tokens_returned_unknown_responses=0,
            cache_read_unknown_responses=1,
            cache_write_unknown_responses=1,
        ),
        source="footer",
        artifact=str(path),
        diagnostics=(),
    )


def select_usage_evidence(
    transcript: Path | None,
    messages: Path | None,
    journal: Path | None,
) -> UsageEvidence:
    diagnostics: list[Diagnostic] = []
    for path, reader in (
        (journal, read_usage_journal),
        (messages, read_messages_usage),
        (transcript, read_footer_usage),
    ):
        if path is None or not path.is_file():
            continue
        evidence = reader(path)
        diagnostics.extend(evidence.diagnostics)
        if evidence.source != "missing":
            return UsageEvidence(
                totals=evidence.totals,
                source=evidence.source,
                artifact=evidence.artifact,
                diagnostics=tuple(diagnostics),
            )
    return _missing_evidence(diagnostics)

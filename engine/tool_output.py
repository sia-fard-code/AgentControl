"""Bounded, paginated tool output helpers (stdlib only, no global state)."""
from __future__ import annotations

from dataclasses import dataclass
from itertools import islice

PAGE_BYTES = 16 * 1024  # quarter of the 64 KiB batch cap; not a provider limit


@dataclass(frozen=True)
class Page:
    text: str
    truncated: bool
    next_offset: int | None
    notice: str


def _pos_int(name, v):
    if isinstance(v, bool) or not isinstance(v, int) or v <= 0:
        raise ValueError(f"{name} must be a positive integer")


def _bytes_upto(s: str, cap: int) -> int:
    # more than cap+1 chars is already > cap bytes; avoids encoding a giant record
    return len(s[:cap + 1].encode("utf-8"))


def _prefix(s: str, n: int) -> str:
    return s[:n].encode("utf-8")[:n].decode("utf-8", "ignore")


def _more(nxt: int) -> str:
    return f"[truncated; more records; continue with offset={nxt}]"


def _clipped(i: int) -> str:
    return f"[record {i} clipped to fit page; continue with offset={i + 1}]"


def render(page: Page) -> str:
    if not page.notice:
        return page.text
    return f"{page.text}\n{page.notice}" if page.text else page.notice


def bounded_lines(iterable, max_bytes, start=0, limit=None) -> Page:
    """One page of records; consumes at most one record past the page."""
    _pos_int("max_bytes", max_bytes)
    if isinstance(start, bool) or not isinstance(start, int) or start < 0:
        raise ValueError("start must be a non-negative integer")
    if limit is not None:
        _pos_int("limit", limit)
    it = iter(iterable)
    for _ in islice(it, start):
        pass
    lines: list[str] = []
    used = 0
    for i, rec in enumerate(it, start):
        rec = str(rec)
        if limit is not None and len(lines) >= limit:
            return Page("\n".join(lines), True, i, _more(i))
        cost = _bytes_upto(rec, max_bytes) + (1 if lines else 0)
        # reserve room for the notice we would need if we stopped right after this record
        if used + cost + 1 + len(_more(i + 1)) <= max_bytes:
            lines.append(rec)
            used += cost
            continue
        if lines:
            return Page("\n".join(lines), True, i, _more(i))
        notice = _clipped(i)
        if len(notice) > max_bytes:
            raise ValueError("max_bytes too small for a truncation notice")
        room = max_bytes - len(notice) - 1
        return Page(_prefix(rec, room) if room > 0 else "", True, i + 1, notice)
    return Page("\n".join(lines), False, None, "")


def clip_text(text: str, max_bytes: int, notice: str = "...[truncated]", measure=None) -> str:
    """Longest prefix + notice whose measure fits max_bytes (UTF-8 bytes by default)."""
    _pos_int("max_bytes", max_bytes)
    m = measure or (lambda s: _bytes_upto(s, max_bytes))
    if m(text) <= max_bytes:
        return text
    if m(notice) > max_bytes:
        raise ValueError("max_bytes too small for the truncation notice")
    # shortcut: assumes measure(s) >= len(s) (true for UTF-8 and JSON); widen hi for other measures
    lo, hi = 0, min(len(text), max_bytes)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if m(text[:mid] + notice) <= max_bytes:
            lo = mid
        else:
            hi = mid - 1
    return text[:lo] + notice

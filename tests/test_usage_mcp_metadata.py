"""MCP observation rows are not gateway responses and must not affect accounting."""
import json

from agent_control.usage import read_usage_journal


def test_mcp_metadata_does_not_add_unknown_model_responses(tmp_path):
    path = tmp_path / "synthetic.usage.jsonl"
    rows = [
        {"phase": "loop", "sent": 12, "recv": 4, "cache_read": 0, "cache_write": 0,
         "raw": {"input_tokens": 12, "output_tokens": 4,
                 "input_tokens_details": {"cached_tokens": 0, "cache_write_tokens": 0}}},
        {"phase": "mcp_start", "server": "fixture", "tool": "read_status",
         "sent": None, "recv": None, "cache_read": None, "cache_write": None},
        {"phase": "mcp_end", "server": "fixture", "tool": "read_status", "status": "ok",
         "duration_s": 0.1, "sent": None, "recv": None, "cache_read": None,
         "cache_write": None},
    ]
    path.write_text("\n".join(json.dumps(row) for row in rows)+"\n", encoding="utf-8")
    evidence = read_usage_journal(path)
    assert evidence.totals.response_count == 1
    assert evidence.totals.tokens_sent_unknown_responses == 0
    assert evidence.totals.tokens_returned_unknown_responses == 0
    assert evidence.totals.sent_includes_cache_read is True
    assert evidence.totals.tokens_sent == 12
    assert evidence.totals.cache_read == 0
    assert evidence.diagnostics == ()


def test_mcp_only_journal_is_not_measured_zero_usage(tmp_path):
    path = tmp_path / "synthetic.usage.jsonl"
    path.write_text(json.dumps({"phase": "mcp_end", "server": "fixture", "tool": "read",
        "sent": None, "recv": None, "cache_read": None, "cache_write": None})+"\n",
        encoding="utf-8")
    evidence = read_usage_journal(path)
    assert evidence.totals.response_count == 0
    assert evidence.totals.tokens_sent is None
    assert evidence.diagnostics == ()

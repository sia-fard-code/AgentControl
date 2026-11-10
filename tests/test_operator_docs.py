from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_cutover_document_covers_live_gates() -> None:
    text = (REPO_ROOT / "docs" / "cutover-checklist.md").read_text(
        encoding="utf-8"
    ).casefold()
    for phrase in (
        "dry-run migration",
        "input backups",
        "projecta approval",
        "projectb approval",
        "direct run",
        "wrapper run",
        "pricing_history.jsonl",
        "repo-local dashboard removal is deferred",
        "rollback",
        "127.0.0.1",
    ):
        assert phrase.casefold() in text, phrase


def test_operator_guide_covers_safety_and_commands() -> None:
    text = (REPO_ROOT / "docs" / "operator-guide.md").read_text(
        encoding="utf-8"
    ).casefold()
    for phrase in (
        "127.0.0.1",
        "pricing_history.jsonl",
        "agent-control sync",
        "agent-control serve",
        "deferred",
        "backup",
        "Ask to wrap up",
        "killed by operator",
        "operator-actions.jsonl",
        "next turn",
        "Masking is a courtesy",
    ):
        assert phrase.casefold() in text, phrase


def test_operator_guide_covers_repo_run_index() -> None:
    text = (REPO_ROOT / "docs" / "operator-guide.md").read_text(
        encoding="utf-8"
    ).casefold()
    for phrase in (
        "scan_roots.json",
        "--task-file",
        "backfill-index",
        "agent-runs-index.jsonl",
        "--oneshot",
        "ask-model.py",
        "2-minute limit",
        "asking for a smaller step",
        "writing…",
        "/openai/responses",
    ):
        assert phrase.casefold() in text, phrase


def test_operator_guide_covers_tool_usage() -> None:
    text = (REPO_ROOT / "docs" / "operator-guide.md").read_text(
        encoding="utf-8"
    ).casefold()
    for phrase in (
        "## Usage from Claude Code, Codex, Cline and Kilo",
        "import-usage --apply",
        "usage_sources.json",
        "Watch only",
        "no rate",
    ):
        assert phrase.casefold() in text, phrase

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class AppPaths:
    repo_root: Path
    state_dir: Path
    machine_index: Path
    baseline_pricing: Path
    web_dir: Path

    @property
    def projects(self) -> Path:
        return self.state_dir / "projects.jsonl"

    @property
    def ledger(self) -> Path:
        return self.state_dir / "ledger.jsonl"

    @property
    def pricing_history(self) -> Path:
        return self.state_dir / "pricing_history.jsonl"

    @property
    def sync_state(self) -> Path:
        return self.state_dir / "sync_state.json"

    @property
    def tool_usage(self) -> Path:
        return self.state_dir / "tool_usage.jsonl"

    @property
    def tool_usage_state(self) -> Path:
        return self.state_dir / "tool_usage_state.json"

    @property
    def usage_sources(self) -> Path:
        return self.state_dir / "usage_sources.json"

    @property
    def scan_roots_file(self) -> Path:
        return self.state_dir / "scan_roots.json"

    @property
    def migration_reports(self) -> Path:
        return self.state_dir / "migration_reports"

    @property
    def activity_dir(self) -> Path:
        # Sanitized per-run activity timelines (no prompts/content), captured
        # while transcripts are still readable so detail survives cleanup of
        # ephemeral workspaces. Runtime state only; never committed to git.
        return self.state_dir / "activity"

    def activity_archive(self, run_id: str) -> Path:
        safe = "".join(c if (c.isalnum() or c in "-_.") else "_" for c in run_id)
        return self.activity_dir / f"{safe}.json"

    @classmethod
    def production(cls, repo_root: Path | None = None) -> "AppPaths":
        checkout = (repo_root or Path(__file__).resolve().parents[2]).resolve()
        user_root = Path(r"C:\Users\operator")
        return cls(
            repo_root=checkout,
            state_dir=user_root / ".claude" / "agent-control",
            machine_index=user_root / ".claude" / "agent-runs-index.jsonl",
            baseline_pricing=checkout / "config" / "pricing.json",
            web_dir=checkout / "web",
        )

    @classmethod
    def for_test(cls, root: Path) -> "AppPaths":
        base = root.resolve()
        repo = base / "repo"
        return cls(
            repo_root=repo,
            state_dir=base / "state",
            machine_index=base / "machine-index.jsonl",
            baseline_pricing=repo / "config" / "pricing.json",
            web_dir=repo / "web",
        )
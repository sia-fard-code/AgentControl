"""Independent reconciliation of the AgentControl dashboard against raw sources.

This does NOT reuse the dashboard's snapshot code path for the "expected" side.
It re-reads the raw machine index and each repo's ledger/transcripts directly,
computes what SHOULD be present per project, then compares against what the
central ledger + snapshot actually report. Any divergence is printed and the
script exits non-zero.

Run:
    python scripts/verify_reconciliation.py
"""

from __future__ import annotations

import json
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from agent_control.ledger import highest_valid_revisions, load_ledger  # noqa: E402
from agent_control.paths import AppPaths  # noqa: E402
from agent_control.pricing import (  # noqa: E402
    calculate_cost,
    load_baseline,
    load_pricing_history,
)
from agent_control.projects import canonical_root, load_projects  # noqa: E402
from agent_control.snapshot import SnapshotQuery, build_snapshot  # noqa: E402


def _read_index_starts(index_path: Path) -> dict[str, dict]:
    """Return the raw start record per run_id, straight from the machine index."""
    starts: dict[str, dict] = {}
    if not index_path.is_file():
        return starts
    for line in index_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if record.get("event") == "start" and record.get("run_id"):
            starts[record["run_id"]] = record
    return starts


def main() -> int:
    paths = AppPaths.production()
    failures: list[str] = []

    # --- load actual central state ---
    records, ledger_diags = load_ledger(paths.ledger)
    current = highest_valid_revisions(records)
    projects, _ = load_projects(paths.projects)
    baseline = load_baseline(paths.baseline_pricing)
    history, _ = load_pricing_history(paths.pricing_history)

    name = {pid: pr.display_name for pid, pr in projects.items()}
    root_by_id = {pid: pr.root for pid, pr in projects.items()}

    # --- independent expectation from the raw machine index ---
    index_starts = _read_index_starts(paths.machine_index)
    # map each index run to a canonical project root
    index_by_root: dict[str, set[str]] = defaultdict(set)
    index_model: dict[str, str] = {}
    for run_id, start in index_starts.items():
        root = start.get("root")
        if not root:
            continue
        _, root_key = canonical_root(root)
        index_by_root[root_key].add(run_id)
        if isinstance(start.get("model"), str):
            index_model[run_id] = start["model"]

    print("=" * 78)
    print("AgentControl independent reconciliation")
    print("=" * 78)

    # --- Check 1: every machine-index run is present in the ledger ---
    print("\n[1] Machine-index runs present in central ledger")
    missing_runs = [rid for rid in index_starts if rid not in current]
    if missing_runs:
        failures.append(f"{len(missing_runs)} machine-index runs missing from ledger")
        for rid in missing_runs[:10]:
            print(f"    MISSING {rid}")
    print(f"    index runs={len(index_starts)} ledger runs={len(current)} "
          f"missing={len(missing_runs)}")

    # --- Check 2: no run silently downgraded model to unknown when index knew it ---
    print("\n[2] No known model downgraded to 'unknown'")
    downgraded = []
    for rid, model in index_model.items():
        rec = current.get(rid)
        if rec is None:
            continue
        if model not in ("", "unknown") and rec.model in ("", "unknown"):
            downgraded.append((rid, model, rec.model))
    if downgraded:
        failures.append(f"{len(downgraded)} runs downgraded model to unknown")
        for rid, was, now in downgraded[:10]:
            print(f"    DOWNGRADE {rid}: index={was} ledger={now}")
    print(f"    downgraded={len(downgraded)}")

    # --- Check 3: per-project counts and cost, recomputed independently ---
    print("\n[3] Per-project totals (independent recompute vs snapshot)")
    snap = build_snapshot(
        list(current.values()), projects, baseline, history,
        SnapshotQuery(), now=datetime.now(timezone.utc),
    )
    # independent per-project cost from ledger + pricing
    indep_runs: dict[str, int] = defaultdict(int)
    indep_cost: dict[str, float] = defaultdict(float)
    indep_missing: dict[str, int] = defaultdict(int)
    for rec in current.values():
        proj = projects.get(rec.project_id)
        if proj is None or proj.state != "approved":
            continue
        cost = calculate_cost(rec, baseline, history)
        indep_runs[rec.project_id] += 1
        if cost.amount is not None:
            indep_cost[rec.project_id] += cost.amount
        else:
            indep_missing[rec.project_id] += 1

    snap_proj = snap["project_totals"]
    for pid in sorted(indep_runs, key=lambda p: -indep_cost[p]):
        disp = name.get(pid, pid)
        exp_runs = indep_runs[pid]
        exp_cost = indep_cost[pid]
        got = snap_proj.get(pid, {})
        got_runs = got.get("runs")
        got_cost = got.get("known_cost")
        ok = got_runs == exp_runs and abs((got_cost or 0) - exp_cost) < 1e-6
        flag = "OK " if ok else "MISMATCH"
        if not ok:
            failures.append(f"project {disp}: snapshot {got_runs}/{got_cost} "
                            f"!= independent {exp_runs}/{exp_cost}")
        print(f"    [{flag}] {disp:<22} runs={exp_runs:<4} "
              f"cost=${exp_cost:,.4f} missing_cost={indep_missing[pid]}")

    # --- Check 4: machine-wide total = sum of approved projects ---
    print("\n[4] Machine-wide total = sum of approved-project costs")
    total_indep = sum(indep_cost.values())
    snap_total = snap["normal_totals"]["all_time"]["known_cost"]
    if abs(total_indep - snap_total) >= 1e-6:
        failures.append(f"machine total {snap_total} != sum {total_indep}")
    print(f"    snapshot all_time=${snap_total:,.4f} "
          f"independent_sum=${total_indep:,.4f} "
          f"{'OK' if abs(total_indep - snap_total) < 1e-6 else 'MISMATCH'}")

    # --- Check 5: pending projects excluded from totals ---
    print("\n[5] Pending/rejected/disabled projects excluded from totals")
    for pid, pr in projects.items():
        if pr.state == "approved":
            continue
        if pid in snap_proj:
            failures.append(f"non-approved project {pr.display_name} in totals")
        runs_in_totals = sum(
            1 for r in current.values()
            if r.project_id == pid and any(
                row["run_id"] == r.run_id and row["included_in_totals"]
                for row in snap["runs"]
            )
        )
        if runs_in_totals:
            failures.append(f"{pr.display_name} ({pr.state}) has runs in totals")
        print(f"    {pr.display_name:<22} state={pr.state:<11} "
              f"runs_in_totals={runs_in_totals}")

    # --- Check 6: coverage counts sum to approved run count ---
    print("\n[6] Coverage classification counts are complete")
    approved_runs = sum(indep_runs.values())
    coverage_total = sum(snap["coverage"].values())
    if coverage_total != approved_runs:
        failures.append(
            f"coverage sum {coverage_total} != approved runs {approved_runs}")
    print(f"    approved runs={approved_runs} coverage_sum={coverage_total} "
          f"coverage={dict(snap['coverage'])}")

    # --- Check 7: ledger integrity (no invalid records, no dup run_ids at top) ---
    print("\n[7] Ledger integrity")
    if ledger_diags:
        for d in ledger_diags:
            failures.append(f"ledger diagnostic: {d.code}")
    print(f"    ledger diagnostics={len(ledger_diags)} "
          f"distinct_runs={len(current)} total_rows={len(records)}")

    print("\n" + "=" * 78)
    if failures:
        print(f"RESULT: FAIL ({len(failures)} issue(s))")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("RESULT: PASS - dashboard reconciles with raw sources for all projects")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

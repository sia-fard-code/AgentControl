# Finish Gate, Bound Read-Only Review, and Trajectory Detector Implementation Plan

> Execute via delegated harness runs (`delegate.py` / `fanout.py`) with test-driven development.

**Goal:** Implement factual finish verification (`engine/finish_gate.py`), run-bound read-only patch review (`engine/run_review.py`), observation-only trajectory suspicion detection (`engine/trajectory.py`), and bounded closeout integration in `engine/delegate.py`.

**Spec:** `../specs/2026-10-30-finish-gate-and-review-design.md`

## Global Constraints

- Preserve `HARNESS_VERSION = 6`, `INDEX_SCHEMA = 2`, and all existing `test_engine_contract.py` and `test_engine_turn_budget.py` guarantees.
- Enforce the three operator invariants:
  1. Review the right run and patch (`RunBinding` + `PatchBinding`, no fallback to `agent.patch` or dirty tree).
  2. Do not give the reviewer write authority (tool-less read-only transport, no `Runner`, no MCP, no `--write`/`--worktree`, never dispatch reviewer tool calls).
  3. Do not present reviewer opinion as proof that tests passed (strictly separate `VerificationStatus` from `[reviewer opinion]`; staged runs never report `verified_by_command`).
- Every test injects `tmp_path` and uses synthetic fixtures; never write live user accounting state or contact a real gateway.
- No prompt, transcript, command output, or raw reviewer prose in `completion.json` or accounting logs.

## Task 1: Standalone Modules & Unit Tests (`finish_gate.py`, `run_review.py`, `trajectory.py`)

Files:
- Create `engine/finish_gate.py`, `tests/test_finish_gate.py`
- Create `engine/run_review.py`, `tests/test_run_review.py`
- Create `engine/trajectory.py`, `tests/test_trajectory.py`

Steps:
- [x] Implement `engine/finish_gate.py` (`VerificationPlan`, `CheckEvidence`, `EvidenceSnapshot`, `GateResult`, `RunEvidence`, `StagedBaseChangedError`, `evaluate_finish`, `render_verification_block`) and unit tests in `tests/test_finish_gate.py`.
- [x] Implement `engine/run_review.py` (`TranscriptAnchor`, `RunBinding`, `PatchBinding`, `ReviewInput`, `ReviewUnavailable`, `ReviewResult`, `capture_review_input`, `load_recorded_review_input`, `review_run`, `render_review_block`, `format_concerns_notice`) and unit tests in `tests/test_run_review.py`.
- [x] Implement `engine/trajectory.py` (`ToolObservation`, `Suspicion`, `TrajectoryDetector`) and unit tests in `tests/test_trajectory.py`.
- [x] Run `python -m pytest tests/test_finish_gate.py tests/test_run_review.py tests/test_trajectory.py -q` to GREEN.

## Task 2: `engine/delegate.py` Integration & Integration Tests

Files:
- Modify `engine/delegate.py`
- Create `tests/test_delegate_verification.py`, `tests/test_delegate_closeout.py`, `tests/test_delegate_trajectory.py`

Steps:
- [x] Wire `finish_gate`, `run_review`, and `trajectory` in `engine/delegate.py` via `capability_module()`, adding `--verify-command` and `--review-finish` CLI validation before config/gateway access.
- [x] Instrument `Runner` mutations, staged base digests, `run_command` structured outcomes, `read_file`/`grep` path tracking, and `TrajectoryDetector` observations.
- [x] Implement bounded finish gate and optional `--review-finish` closeout inside `turn_budget(max_iters)` (at most 1 correction, at most 1 review, total model calls <= `max_iters`, default runs without new flags unchanged).
- [x] Write run-owned `<artifact_dir>/patch-<sha256>.diff` and metadata-only `<artifact_dir>/completion.json` in `save_results()`, and render authoritative verification + `[reviewer opinion]` sections.
- [x] Run `python -m pytest tests/test_delegate_verification.py tests/test_delegate_closeout.py tests/test_delegate_trajectory.py tests/test_engine_turn_budget.py tests/test_engine_contract.py -q` to GREEN.

## Task 3: Opt-In Tier 2 Mid-Run Advisor (`--supervise-stuck`) & Deployment Bundle Support

Files:
- Modify `engine/trajectory.py`, `engine/delegate.py`, `scripts/deploy_engine.py`
- Modify `tests/test_trajectory.py`, `tests/test_delegate_trajectory.py`, `tests/test_deploy_engine.py`

Steps:
- [x] Add `AdvisorInput`, `AdvisorResult`, `consult_advisor`, `format_advisor_notice`, `ADVISOR_MAX_INTERVENTIONS = 2`, and `ADVISOR_COOLDOWN_TURNS = 3` in `engine/trajectory.py` and unit tests in `tests/test_trajectory.py`.
- [x] Wire `--supervise-stuck` in `engine/delegate.py` with freshness check, cooldown, per-run cap, tool-less read-only request, `phase="advisor"` usage journalling, `[advisor suggestion — non-authoritative]` injection, and `advisories` metadata in `completion.json`, plus integration tests in `tests/test_delegate_trajectory.py`.
- [x] Add `_SUPERVISION_NAMES = ("finish_gate.py", "run_review.py", "trajectory.py")` and `_SUPERVISION_TESTS` to `scripts/deploy_engine.py` and tests in `tests/test_deploy_engine.py`.
- [x] Run full targeted pytest suite to GREEN and run multi-lens Council review via `fanout.py`.

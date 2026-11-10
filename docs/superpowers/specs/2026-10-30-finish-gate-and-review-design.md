# Evidence-Based Finish Gate, Bound Read-Only Review, and Observation-Only Trajectory Detector

Date: 2026-10-30
Status: Operator approved direction and three core invariants in this session.
Engine version: `HARNESS_VERSION = 6`, `INDEX_SCHEMA = 2` (unchanged).

## 1. Goal and Operator Invariants

Prevent delegated runs from claiming verified completion without factual evidence while allowing optional, bounded, read-only patch review and observation-only loop detection.

Three strict invariants govern every code path:

1. **Review the right run and patch.** Every review input is cryptographically and structurally bound to the specific run (`run_id`, `task_sha256`, transcript prefix anchor, `root_identity`, `write_mode`) and that run's exact candidate patch (`run.patch()` in staged mode, `worktree_diff(wt_path)` in a validated run-owned worktree, or a recorded run-owned patch artifact). Never read or fall back to an unassociated `agent.patch`, `args.patch`, "latest run", or dirty main-tree diff.
2. **Do not give the reviewer write authority.** Reviewer invocations run over a read-only, tool-less model transport with no `Runner`, no `run_command`, no `write_file` / `replace_in_file`, no MCP servers or tools, no executable skills, and no `--write` or `--worktree`. Any unexpected tool call returned by a backend is ignored and treated as `abstain` / unavailable, never dispatched. Reviewer feedback is never written to `.steer` and never injected as `[operator guidance]`.
3. **Do not present its opinion as proof that tests passed.** Factual verification status (`verified_by_command`, `unverified_post_edit_mutation`, `staged_unverified_on_disk`, `no_mutations`) is computed strictly from structured command-execution and mutation facts in `finish_gate.py`. In `stage` mode, `run_command` executes against the unmodified disk tree and can never verify `run.pending`. A reviewer's `pass` or `concerns` verdict is recorded and rendered separately as `[reviewer opinion]` and has no path into factual verification state.

## 2. Modules and Responsibilities

| File | Responsibility |
|---|---|
| `engine/finish_gate.py` | Pure factual verification ledger (`RunEvidence`, `VerificationPlan`, `CheckEvidence`, `EvidenceSnapshot`, `GateResult`), staged-base digest tracking, and `evaluate_finish()`. Zero model calls. |
| `engine/run_review.py` | Run and patch binding structures (`TranscriptAnchor`, `RunBinding`, `PatchBinding`, `ReviewInput`, `ReviewUnavailable`, `ReviewResult`), mode-specific candidate capture, post-run artifact loader (`load_recorded_review_input`), and tool-less `review_run()`. |
| `engine/trajectory.py` | Observation-only suspicion detector (`ToolObservation`, `Suspicion`, `TrajectoryDetector`) for repeated action–observation pairs, repeated `replace_in_file` failures without intervening read/grep, and repeated command refusals. |
| `engine/delegate.py` | CLI flags (`--verify-command`, `--review-finish`), runner instrumentation, bounded closeout orchestration inside `turn_budget(max_iters)`, authoritative completion metadata (`completion.json`), and non-authoritative reviewer opinion rendering. |

## 3. Factual Verification and the Finish Gate (`engine/finish_gate.py`)

### 3.1 CLI Options

- `--verify-command '<JSON argv array>'` (repeatable): designates required verification commands (e.g. `["python", "-m", "pytest", "-q"]`). Must parse as a non-empty JSON list of non-empty strings. Does not widen `ALLOWED_CMDS`. When omitted, no verification command is designated (`no_verification_specified`), and incidental command successes (`git status`, etc.) never produce `verified_by_command`.
- `--review-finish`: requests at most one read-only patch review during closeout. Incompatible with `--oneshot`.

### 3.2 Evidence Rules

- `mutation_seq` increments monotonically on each byte-changing `write_file` or `replace_in_file` commit. No-op writes (where new content equals current content) and refused actions do not advance `mutation_seq`. Reversions back to earlier content still advance `mutation_seq`.
- `verified_by_command` requires all of:
  1. `write_mode == "disk"`.
  2. Non-empty `VerificationPlan.commands`.
  3. Every designated command executed in this run against this run's `root_identity` after the latest `mutation_seq`, exited with `0`, did not time out or lose its outcome, and did not itself mutate the candidate file state.
- Status mapping:
  - `no_mutations`: `mutation_seq == 0` and no uncertain mutation occurred.
  - `staged_unverified_on_disk`: `write_mode == "stage"` and `mutation_seq > 0` (even if designated commands ran and exited `0` against the disk tree).
  - `verified_by_command`: `write_mode == "disk"`, `mutation_seq > 0`, and all designated commands satisfied the rules above after the latest mutation.
  - `unverified_post_edit_mutation`: `write_mode == "disk"` with `mutation_seq > 0` (or uncertain mutation) lacking current passing evidence for all designated commands.

### 3.3 Staged Base Validation

When `Runner` first stages a path in `write_mode == "stage"`, it records the on-disk base state (`absent` or `sha256` of the on-disk bytes). When `run.patch()` or candidate capture builds the diff, if the on-disk file changed since staging began, it raises `StagedBaseChangedError` rather than diffing staged content against a mutated disk base.

## 4. Exact Run & Patch Binding and Read-Only Review (`engine/run_review.py`)

### 4.1 Candidate Capture (`capture_review_input`)

- **Stage mode (`write_mode == "stage"`):** captures `run.patch()` directly from the active `Runner` after validating staged base digests.
- **Worktree mode (`wt_path` owned by this run):** validates that `wt_path` is the worktree created for this run and that its `HEAD` matches the baseline `HEAD` recorded at worktree creation, then captures `worktree_diff(wt_path)`.
- **Plain `--write` without worktree:** unless an explicit run-bound recorded artifact for the current `candidate_seq` and `mutation_seq` is supplied, returns `ReviewUnavailable(reason="unbounded_disk_write")`—never falling back to `args.patch`, `agent.patch`, or `git diff` on the shared root.

### 4.2 Zero Write Authority (`review_run`)

- `review_run(candidate, *, generate_readonly)` calls `generate_readonly(messages)` with no `tools`, no `Runner`, no MCP, and no skills.
- If the backend response carries `tool_calls` or a prompt-mode ` ```tool ` block, nothing is executed and `ReviewResult` records `verdict="abstain"`, `unavailable_reason="unexpected_tool_call"`.
- Structured JSON response schema accepts only:
  - `verdict`: `"pass" | "concerns" | "abstain"`
  - `finding_codes`: subset of `("task_coverage", "possible_regression", "verification_gap", "other")`
- Raw reviewer prose is never persisted to disk or injected into prompts. When `verdict == "concerns"` triggers the single closeout correction, the engine injects a fixed template from the allowlisted `finding_codes` prefixed with `[reviewer opinion — non-authoritative]`.
- If a subsequent correction mutates the candidate (`candidate_seq` / `patch_sha256` changes), the review's `applicability` becomes `"superseded"`.

## 5. Turn Budget and Bounded Closeout (`engine/delegate.py`)

- `turn_budget(max_iters)` remains unchanged. Total model generations (acting turns, protocol repairs, reviewer one-shot, closeout correction, reserved final report) never exceed `max_iters`.
- Default runs without `--verify-command` and without `--review-finish` preserve existing early-finish behavior (`finish()` completes immediately in the turn it is called).
- When `--verify-command` or `--review-finish` is active and a valid `finish` proposal arrives:
  - At most **one** correction turn (`correction_attempted`) and at most **one** review call (`review_attempted`) may occur per run, strictly within the available work + closeout turns before the reserved report turn.
  - Factual verification correction (when `write_mode == "disk"`, `--verify-command` is set, and `correction_useful` is true) takes priority over review when only one closeout turn remains.
- Authoritative artifacts written under `<artifact_dir>/`:
  - `patch-<sha256>.diff` when a bound candidate patch is captured.
  - `completion.json` containing metadata only (run/binding IDs and digests, `verification_status`, check outcomes, `review` verdict/finding_codes/applicability, correction/review counts, and `suspicions`).

## 6. Trajectory Detector and Opt-In Tier 2 Mid-Run Advisor (`engine/trajectory.py`)

- Tracks three deterministic tripwires (threshold = `3`) over structured tool observations:
  1. `repeated_action_observation`: identical `(tool, canonical_args_digest, outcome_digest)` 3 times within the same `state_epoch` (broken by any effective mutation).
  2. `replace_failures_without_read`: 3 executed `replace_in_file` failures (`old_string not found` or `appears N times`) on the same canonical path without an intervening successful `read_file` of that path or `grep` covering that path.
  3. `repeated_command_refusal`: 3 policy-refused `run_command` calls without an intervening started command.
- Protocol failures (`NO_TOOL_ACTION`, `PARSE_ERROR`, `reply_cut`) are excluded.
- Default mode is observation-only: emits metadata-only `Suspicion(code, count, first_action_ordinal, last_action_ordinal, state_epoch)` records in `completion.json`.
- **Opt-In Tier 2 Mid-Run Advisor (`--supervise-stuck`):**
  - Requires explicit `--supervise-stuck` CLI flag (incompatible with `--oneshot`).
  - Only wakes when a Tier 1 tripwire fires and remains fresh at the end of the turn (`suspicion.state_epoch == trajectory.state_epoch`), never on `--soft-iters` checkpoints or protocol failures.
  - Enforces a per-run cap (`ADVISOR_MAX_INTERVENTIONS = 2`), a turn cooldown (`ADVISOR_COOLDOWN_TURNS = 3`), and the shared `turn_budget(max_iters)` (`model_calls_used < work + closeout`).
  - Executes over a tool-less, read-only model request (`consult_advisor`) bound to `run_id`, `task_sha256`, `turn`, `state_epoch`, `candidate_seq`, `mutation_seq`, `suspicion`, and bound `patch_sha256`. Any unexpected tool call in the response is ignored and yields `status="abstain"`, `unavailable_reason="unexpected_tool_call"`.
  - Injects only a fixed template for an allowlisted `action_code` prefixed with `[advisor suggestion — non-authoritative]`, never writes `.steer`, never uses `[operator guidance]`, and never alters `verification_status`.

## 7. Shared Engine Deployment Bundle (`scripts/deploy_engine.py`)

- Adds optional atomic supervision bundle `_SUPERVISION_NAMES = ("finish_gate.py", "run_review.py", "trajectory.py")` and `_SUPERVISION_TESTS` to `scripts/deploy_engine.py` with backup, `py_compile` verification, and rollback-all guarantees.

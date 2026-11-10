# Recoverable Worker Execution — Implementation Plan

Spec: `docs/superpowers/specs/2026-10-31-recoverable-worker-execution-design.md`

- **Goal:** workers stop losing completed work to oversized output, valid Claude batches,
  protocol-repair lifetime limits and missing handoff, under an opt-in `resilient-v1` profile.
- **Architecture:** small new modules `engine/tool_output.py`, `engine/recovery_handoff.py`,
  `engine/candidate.py`, `engine/continuation.py`, called from existing `engine/delegate.py`
  (`Runner`, work loop) and the existing policy-v2 supervisor (`engine/supervise.py`,
  `engine/supervisor_state.py`, `engine/supervisor_harness.py`, `engine/supervisor_policy.py`,
  `engine/supervisor_contract.py`, `engine/supervisor_evidence.py`). Metadata projection in
  `src/agent_control/{snapshot,models,dashboard}.py`, `web/{dashboard,panel}.js`.
- **Tech stack:** Python stdlib, pytest (`pythonpath=src`), `node --test` for web JS. No new deps.
- **Approval / scope:** operator approved in chat 2026-10-31 17:10 UTC for SOURCE only.
  No deploy, migration, default switch, real-cost trial, commit policy change.
- **Global constraints:** legacy defaults/approvals/digests unchanged; context_guard caps keep
  values; no command replay; no global quota; no prompt-tool protocol; no root/exclusion
  change; central state holds typed metadata + digests only; synthetic fixtures in `tmp_path`
  only, never live state; assume every test command can mutate the tree.

Each task: write failing test -> run it (fails) -> minimal implementation -> run it (passes)
-> run baseline `python -m pytest -q tests/test_engine_turn_budget.py
tests/test_engine_context_guard.py tests/test_turn_budget_consumers.py` (155 pass).

## Phase 1 — Profile and bounded tool output

### Task 1.1 Profile flag
- Files: `engine/delegate.py` (main argparser ~4692), test `tests/test_resilient_profile.py` (new).
- Produces: `--profile {legacy,resilient-v1}` default `legacy`; `Runner.profile: str`.
- Tests: default parses to `legacy`; unknown value rejected; legacy run args/digests unchanged.
- Cmd: `python -m pytest -q tests/test_resilient_profile.py`

### Task 1.2 Streaming bounded listing/grep
- Files: `engine/tool_output.py` (new), test `tests/test_tool_output.py` (new).
- Produces: `bounded_lines(iterable, max_bytes, start=0) -> Page`;
  `Page(text, truncated: bool, next_offset: int|None, notice: str)`.
- Tests: 5000-entry synthetic dir in `tmp_path` -> page <= cap, `next_offset` set, iterator
  not fully consumed past cap (generator spy); continuation from `next_offset` yields next page.
- Cmd: `python -m pytest -q tests/test_tool_output.py`

### Task 1.3 Wire list_dir/grep under resilient-v1
- Files: `engine/delegate.py` `Runner.list_dir` (~2138), `Runner.grep` (~2149);
  test `tests/test_tool_output.py`.
- Consumes: `bounded_lines`, `Runner.profile`. Legacy branch untouched.
- Cmd: `python -m pytest -q tests/test_tool_output.py tests/test_engine_context_guard.py`

### Task 1.4 Pending read honors ranges
- Files: `engine/delegate.py` `Runner.read_file` (~2099 pending branch); test
  `tests/test_tool_output.py`.
- Tests: pending (unsaved) content with `start_line/end_line` returns same slice as disk;
  max-read cap applies to pending content.

### Task 1.5 Oversized result -> typed page, not exhaustion latch
- Files: `engine/context_guard.py` `Guard.admit` (~91); test `tests/test_engine_context_guard.py`.
- Produces: under resilient-v1, `admit` returns bounded rendered text with a continuation
  notice for a single oversized read instead of latching exhaustion; constants unchanged.
  `Page` is an internal producer value; public tool results and `Guard.admit` remain text.
- Tests: 65537-byte output with 514-byte request -> bounded page, no latch; cumulative
  read/output limit still enforced when current request is only 102 bytes; real
  exhaustion -> existing `context_budget` worker stop plus a local-allowance handoff reason,
  never a fabricated provider-limit message.

### Task 1.6 Withheld output keeps exit/effect, never replays
- Files: `engine/delegate.py` `Runner.run_command` (~2201); test `tests/test_tool_output.py`.
- Tests: oversized stdout after an append command -> result carries real exit code and
  `effect=applied`, stdout withheld notice; command spy called exactly once.

## Phase 2 — Claude native batches and protocol health

### Task 2.1 Batch validator (pure)
- Files: `engine/native_batch.py` (new, separate from output formatting); test
  `tests/test_claude_batch.py` (new).
- Produces: `validate_batch(calls, *, batch_cap) -> BatchVerdict(ok, reason, calls)`.
- Tests: malformed tail -> `ok=False`, nothing executed; duplicate IDs -> rejected;
  empty ID rejected; over-`BATCH` admission rejected.

### Task 2.2 Sequential dispatch with paired results
- Files: `engine/delegate.py` Claude batch refusal (~5310) gated by profile; test
  `tests/test_claude_batch.py`.
- Tests: write/check/finish/write -> write, check run; finish deferred until settled;
  trailing write gets result; every ID has a paired result or typed skip; call with
  `effect=unknown` -> later calls get explicit skips; per-call root/command/MCP policy denial
  still applies; legacy profile keeps refusal.

### Task 2.3 Valid batches are not incidents
- Files: `engine/delegate.py` `take_repair` (~5134), `REPAIR_LIMIT` (~373); test
  `tests/test_claude_batch.py`.
- Produces: `RepairHealth(consecutive, total_history)`; a valid known-result action resets
  consecutive wire failures, not task-progress history. Total incident metadata is durable.
  No-op reads cannot establish task progress or silently renew a logical-task budget.
- Tests: three spaced valid Claude batches with useful work between -> zero incidents, no
  timeout classification, run continues; genuine repeated repairs without progress -> route
  to existing diagnostic strategy; valid no-op does not count as progress; legacy profile
  keeps `REPAIR_LIMIT = 2`.

## Phase 3 — Candidate-bound verification and handoff

### Task 3.1 Check definitions and candidate binding
- Files: `engine/recovery_handoff.py` (new), consumes `engine/finish_gate.py`
  `VerificationPlan`, `CheckEvidence`, `EvidenceSnapshot`; test `tests/test_recovery_handoff.py` (new).
- Produces: closed metadata-only observations bound to approved definition/configuration
  digests, verifier owner, run binding and before/after candidate digest plus mutation
  sequence. Worker-written receipts never confer trusted acceptance. Reuse existing
  `supervisor_evidence.candidate_identity` unchanged at bounded explicit checkpoints/checks;
  do not hash the repository for every ordinary command or create a second acceptance gate.
- Tests: exit 0 with before==after is usable evidence ONLY for an approved check definition,
  matching owner/capability, root, candidate and configuration; without approval it is
  rejected. Complete task acceptance additionally requires every protected predicate.
  Check mutates candidate (after != before) -> stale; result for Y presented for X ->
  rejected; definition/configuration/owner mismatch -> rejected. Legitimate declared
  non-candidate cache writes do not automatically certify or invalidate candidate contents.

### Task 3.2 Deterministic machine handoff
- Files: `engine/recovery_handoff.py`; `engine/delegate.py` final report/save patch (~5490+);
  test `tests/test_recovery_handoff.py`.
- Produces: durable `begin(owned_dir, binding=..., spec=..., evidence=...,
  before_sha256=...)`, `settle(owned_dir, pending, evidence=..., after_sha256=...)` and
  strict bounded reading of a fixed owned handoff file. One inflight generation at a time;
  atomic replacement, file fsync and existing journal directory-durability barrier.
  Unsupported durability blocks dispatch; published but unacknowledged intent cannot
  cause replay. Handoff includes candidate digest, `rerun_owed`, evidence without raw argv,
  phase and generation; completion binds its digest even if model reporting fails.
  Collection also requires protected expected candidate-root identity because the canonical
  policy project root differs from an owned worker worktree. Known-digest candidates cannot
  contain their own changing handoff artifacts. Unavailable identity remains unknown.
- Tests: model report call raises -> handoff file still written and valid; all acceptance
  checks already trusted -> no extra model call (call counter spy == cap); late edit after
  last check -> saved with `rerun_owed=true`; `finish` without trusted acceptance ->
  lifecycle complete, task success false; no out-of-cap model call in any path.

### Task 3.3 Finish gate separation
- Files: `engine/delegate.py` `evaluate_finish_proposal` (~5093), `engine/finish_gate.py`
  `evaluate_finish` (~300); test `tests/test_recovery_handoff.py`.
- Tests: `GateResult` lifecycle-accepted unverified is not mapped to task success;
  legacy behavior of `evaluate_finish` unchanged (existing finish-gate tests pass).

### Task 3.4 Bound runtime activation
- Files: `engine/recovery_runtime.py` (new), `engine/delegate.py` startup/dispatch/closeout;
  test `tests/test_recovery_runtime.py`. Core schema tests remain in
  `tests/test_recovery_handoff_core.py`.
- Runtime flag: `--recovery-contract PATH`, absent by default and rejected with a legacy
  profile. Its v1 closed keys are `schema_version`, `profile`, `launch_id`, `binding`,
  `spec`, `candidate_root_identity`, `owned_dir`; binding/spec use core schemas.
  This records observations only: it cannot widen policy, extend a worker cap, launch
  another worker, certify task acceptance or replace protected parent approvals.
- Freeze/validate contract task/run/launch, canonical policy root, actual owned worktree
  and transcript artifact directory before model dispatch. Missing bindings are explicitly
  `unbound`. An inflight restart never redispatches; failed intent acknowledgement means
  no action. Ordinary-command identity may remain unknown; explicit verification and
  checkpoint hashing stay bounded and cannot alter the command's actual exit result.
- Final machine checkpoint precedes report fallback and survives model-report failure.
  Metadata durability errors end the worker with `recovery_state` and a local report;
  source consumer labels for this opt-in reason are integrated in Phase 6.

## Phase 4 — Durable continuation (policy-v2 supervisor)

### Task 4.0 Explicit approval codec
- Files: `engine/supervisor_contract_v3.py` (new), `tests/test_supervisor_contract_v3.py`.
- Parse schema 3 independently with frozen common schema-2 fields and null-quota
  semantics, plus tagged candidate descriptor and approved-task continuation profile.
  Reuse existing pure common validators; never fabricate a Git commit to invoke a v2 parser.
- Git descriptor fields: `kind`, `baseline_commit`, `initial_sha256`, `inputs`, `bounds`.
  Artifact descriptor omits `baseline_commit`. Snapshot bounds match the candidate port's
  six finite technical fields. Continuation has only profile/admission, no new quotas.
- Integrate version selection only after the independent codec is tested. Old v1/v2
  parsing, serialization/digests/defaults stay unchanged; artifact access to Git-only
  operations fails closed rather than returning a magic commit.

### Task 4.1 Continuation record
- Files: `engine/continuation.py` (new); test `tests/test_continuation.py` (new).
- Produces: `Continuation` dataclass with spec section 4 fields; `save(path, rec)` atomic
  write, `load(path)` with schema version check; `next_action` enum.
- Tests: round-trip; unknown usage stays `None` (not 0) after save/load; operator caps and
  `cancelled=true` survive reload; unknown schema version rejected.

### Task 4.2 Intent before dispatch, UNKNOWN on crash
- Files: `engine/continuation.py`, `engine/supervisor_state.py` (`admit_worker` ~603),
  `engine/supervisor_journal.py`; test `tests/test_continuation.py`.
- Tests: intent persisted before dispatch spy fires; simulated crash after append, no
  receipt -> state `UNKNOWN`, never `NOT_RUN`; restart does not redispatch until reconciled.

### Task 4.3 Reconcile before redispatch/collection
- Files: `engine/supervise.py`, `engine/supervisor_process.py`; test `tests/test_continuation.py`.
- Tests: surviving PID with matching birth time -> no new launch; stale/duplicate collection
  of same candidate -> rejected; patch never reapplied blindly; cancellation blocks slice
  launch; cumulative caps reached -> blocked; legacy approval digests byte-identical
  (existing `tests/test_supervisor_*_v2.py` pass).

### Task 4.4 Optional atomic native containment
- Files: `engine/supervisor_process.py`; test `tests/test_supervisor_process_v3.py`.
- Produce `WindowsJob.atomic_supported()` and `create_suspended_atomic` with the existing
  suspended-creation signature. Combine HANDLE_LIST and JOB_LIST at creation; verify
  membership and exact birth before the caller can resume. Unsupported capability means
  no protocol-2 launch, never a legacy fallback. Existing methods/defaults stay unchanged.
- `encode_birth3`/`decode_birth3` preserve exact FILETIME as canonical `win:<decimal>`
  strings rather than exceeding the journal's integer precision bound.
- Tests cover unsupported attributes before process creation, cleanup at every boundary,
  real temporary native-Python suspended membership, leader exit with surviving child,
  exact-birth reuse rejection and missing-Job ambiguity. No actual applications/models.

## Phase 5 — Candidate contracts

### Task 5.0 Bounded snapshot transport primitives
- Files: `engine/candidate.py` (new), `tests/test_candidate_snapshots.py` (new).
- Define strict `SnapshotBounds`, canonical per-file/directory manifest entries and sealed
  transport receipts. Ports are `capture`, `materialize`, `delta` and `merge`.
- Data selection is explicit and digest-bound; include all selected/new/ignored data,
  binary bytes, deletions and applicable executable modes. Only separately authenticated
  Git administration may be treated as protected non-data. No arbitrary ignore list.
- Validate paths/aliases/reparse/hardlink boundaries, count/byte/time limits, immutable
  blob digests, expected seed and segment-boundary write scopes. Partial snapshots/trees
  never become selectable. Publication uses file fsync and the existing directory barrier.
- Pure transport receipts never grant acceptance or mutation authority; callers must
  hold protected ownership/quiescence guards and anchor receipt digests in the journal.

### Task 5.1 Git and artifact-manifest contracts
- Files: `engine/candidate.py` (new), `engine/supervisor_policy.py` (manifest FIELDS
  version negotiation), `engine/supervisor_harness.py` (collection); test `tests/test_candidate.py` (new).
- Produces: `GitCandidate` (existing `patch_binding`), `ManifestCandidate(entries: path, size, sha256)`;
  `contract_version` negotiated explicitly; legacy v1 manifest stays strict.
- Tests: non-git root never routed to git contract; manifest path traversal/symlink outside
  root rejected; baseline digest mismatch rejected; root widening rejected; permission
  fields from model summary/task label/checkpoint ignored; unknown contract version
  rejected; existing git/worktree preflight tests pass unchanged.

## Phase 6 — Task-level metadata visibility

### Task 6.1 Typed task state projection
- Files: `src/agent_control/models.py`, `snapshot.py`, `dashboard.py`; `web/dashboard.js`,
  `web/panel.js`; tests `tests/test_task_state_metadata.py` (new), `tests/js/` (node --test).
- Tests: states `continuing|awaiting-verification|reconciliation|blocked|verified-complete`
  projected; snapshot contains no file content, prompts, stdout, or paths outside run dir
  (sentinel string in synthetic run never appears in snapshot/API); historical worker
  records and accounting unchanged (append-only); `verified-complete` only with trusted
  current-candidate acceptance.
- Cmd: `python -m pytest -q tests/test_task_state_metadata.py` and `node --test tests/js/`

## Fault matrix (all synthetic, `tmp_path`, no live state)

| Fault | Task |
|---|---|
| 5000-entry listing | 1.2, 1.3 |
| 65537-byte output, 514-byte request | 1.5 |
| pending read ranges | 1.4 |
| cumulative limit despite 102-byte current request | 1.5 |
| three spaced valid Claude batches | 2.3 |
| malformed tail executes nothing; duplicate IDs | 2.1 |
| write/check/finish/write | 2.2 |
| oversized stdout after append (no replay) | 1.6 |
| crash after append, no receipt -> UNKNOWN | 4.2 |
| stale/duplicate collection | 4.3 |
| coordinator X-vs-Y | 3.1 |
| report failure -> handoff survives | 3.2 |
| cancellation / cumulative caps / legacy approvals | 4.1, 4.3 |
| Git and non-Git manifest security | 5.1 |
| null vs zero usage | 4.1 |
| no central content | 6.1 |

## Mandatory source acceptance gates (part of this implementation)

Run the complete isolated Python and Node regression suites and independent adversarial
review before source completion. Deployment remains outside this implementation and needs
separate explicit approval.
Controlled real-cost trials need separate approval; no savings claimed from records/baseline.

## Integration decisions and compatibility

- The direct CLI flag is `--profile {legacy,resilient-v1}`, default `legacy`. Passing it to
  `Runner` and `Guard` is explicit; omitted constructor keywords retain legacy behavior.
- Supervisor continuation uses explicitly version-negotiated approval/manifest fields;
  it must not silently add the profile to a legacy-v1 approval or change its digest.
- Batch structural validation and repair-health helpers live in `engine/native_batch.py`;
  output producers and formatting live in `engine/tool_output.py`.
- Existing supervisor state and journal modules are `supervisor_state.py` and
  `supervisor_journal.py`, not a new parallel supervisor/store.
- Existing finish evidence uses `CheckEvidence`, `EvidenceSnapshot` and `RunEvidence`.
- Explicit schema-3 lineage uses opt-in `candidate-receipt-2` context binding;
  omitted context preserves exact receipt-v1 bytes and DATA identity. Extend
  `Snapshot`, capture/merge/seal/load/open only, with a validated optional context
  SHA. Materialize/delta retain strict receipt verification. Add red-first sibling,
  singleton-merge, repeated-no-op/idempotent-reference and strict-reopen tests.
- Keep source IDs/kind/seed receipt/envelope in the existing protected snapshot3
  journal record; hash that closed context into durable transport bytes. Reopening
  authenticates raw receipt pin before parsing and matches producing context.
  Merges must authenticate seed receipts, not only equal seed DATA.
- Schema-3 evaluation replay validation permits completed sequential evaluations
  in one revision, rejects pending/duplicate/stale/reused facts and does not count
  evaluations as planner calls. Preserve all v1/v2 restrictions. Tests exercise
  both the public API and journal replay.
- Metadata visibility accepts only explicitly injected trusted parent sources,
  expected project/root/envelope pins and a bounded closed metadata schema. No
  automatic registry/discovery or central content storage. Default snapshots omit
  the new goal-task view unless configured; task rows never alter run accounting.

## Source acceptance results — 2026-11-01

- Completed the approved output, native-batch, verification/handoff, durable
  continuation, and task-observation phases in the isolated
  `.claude/worktrees/recovery-7d0ef113` workspace. Source remains uncommitted.
- Independent adversarial reviews covered bounded output/guards, snapshot resource
  bounds, identity/administration, continuation/recovery, and parent/evaluator
  boundaries. Reproduced and fixed the reported defects with focused regressions.
  The final recovery fixes retain unresolved prior-owner evaluations and reach a
  bounded blocked state without replay, and reschedule work reopened by selection.
- Additional real temporary Git worktrees verify approved origin administration
  and binary/deletion/current-seed materialization. Real contained Windows Python
  validators verify the driver-to-evaluator path. No model requests, external
  applications, production state or live accounting are used by these tests.
- Full isolated Python command:
  `python -m pytest -q --tb=short --show-capture=no`
  — **3503 passed, 17 skipped**, 540.53 seconds.
- Full Node command:
  `node --test --test-reporter=tap tests/js/dashboard.test.mjs tests/js/panel.test.mjs tests/js/task-state.test.mjs`
  — **83 passed**, no failures or skips.
- Python skips are explicit opt-in live/browser/rebuilt-runtime checks and
  platform-specific cases (POSIX execution mode and the 600-level path fixture).
  Portable depth, chunk deadline, DOM-only panel, metadata-content rejection,
  exact-True ownership, raw-findings non-publication and UTF-8 scope regressions ran.
- `git diff --check` is clean. The canonical checkout stays at `da50e92`; the
  existing production listener is untouched. No deployment, source commit, push,
  live migration, accounting correction or production registration was performed.
- Activation remains a separate operator decision: deploy the complete pinned
  engine bundle, opt into `resilient-v1`/approval-3/manifest-2 as applicable, prepare
  the explicitly approved initial snapshot, and explicitly register trusted task
  metadata sources. Legacy defaults and existing approvals are not migrated.
- These source tests do not establish gateway reliability, measured cost savings,
  or an OS filesystem sandbox. Exclusive protected administration and approved
  program/validator authority remain documented preconditions.

## Operator-approved deployment and activation — 2026-11-01

The operator subsequently approved deployment, activation and pushing the release.
This approval does not authorize a new project goal, snapshot, application action,
accounting migration or change to existing goal approvals.

Release gates:

1. Extend the rollback-capable installer to include the complete recovery source
   family and its dependencies. Reject partial bundles, include new modules in
   verification/rollback, and exercise the recovery tests in the deployment gate.
2. Provide a small explicit operator activation setting for new direct agent runs.
   Absence of activation retains legacy behavior; explicit profile choices,
   one-shot behavior and existing supervisor approvals remain unchanged. Validate
   settings before gateway access and expose a cold metadata-only profile probe.
3. Expose an explicitly selected trusted task-source registry at dashboard startup.
   An empty registry activates the view without approving or discovering goals.
4. Run source and installer tests, inspect the intended Git diff, and prepare a
   complete rollback set. Install only after delegated release workers finish.
5. Verify installed checksums, compilation, cold capabilities/profile, and the
   restarted loopback dashboard. Preserve accounting and existing goal histories.
6. Prepare a source commit containing only intended changes and integrate only by
   a clean fast-forward, so the editable dashboard and installer share the same
   release source. Push only after deployment/activation verification. Keep the recovery
   worktree; do not rewrite remote history or create an unsolicited pull request.

Activation release verification:

- Full Python suite after activation, temporary-home isolation and embedded-caller
  fixes: **3608 passed, 17 skipped**, 600.12 seconds. Full Node suite: **83 passed**.
- Deployment regression tests cover out-of-tree selected-source dependencies:
  a required `execution_profile.py` is checked before any backup or write, including
  fixed-sibling loading. Pre-activation engines retain their original bundle shape.
- Operator settings and approved-worker fences preserve explicit choices, legacy
  approvals and one-shot behavior. Cold inspection reads no secrets or run indexes.
- Temporary-home isolation does not create files in candidate roots. Request-budget
  coverage uses synthetic overhead rather than a live installed skill catalog.
- Real HTTP checks now cover the required `/static/task-state.js` route and retain
  the closed static-file allowlist, traversal rejection and JavaScript MIME headers.
- Local activation files, rollout backups and trusted task registrations are
  machine state, not source-controlled project or goal approvals.

## Phase 7 — Model-aware context and optional same-run compaction

Source scope was approved in chat on 2026-11-01. Follow design section 8. Existing
activation approval above does not authorize deploying this new extension.

1. Add pure frozen policy/selection records and cold option resolution in
   `engine/context_policy.py`. Test default model-v1, explicit legacy, frozen
   precedence, one-shot compaction rejection, numeric bounds and provenance first.
2. Add the token meter with explicit estimation and packet-bound usage calibration.
   Test cached input, Unicode, escaping, schemas, media and drift without downloads
   or provider calls. Do not claim heuristic counts are exact capacity guarantees.
3. Extend Guard with optional model policy while retaining the legacy branch exactly.
   Audit every admission/control/replacement path. Keep complete packet reservations
   and finite producer limits. Validate final token candidates instead of assuming
   token counts are monotonic during a byte-prefix binary search. First provide the
   run-local result-receipt reservation/reader hook. Keep a measured all-fallback packet
   for the current batch, address replacement by slot ID and restore all revisable
   fallbacks if a local replacement cannot fit. Do not claim generic safe dispatch
   until receipt publication, outcome recording and the owning-run reader are wired.
4. Integrate direct CLI, one-shot and helper options, all request paths, steers and
   actual usage. Context safety and pairing cannot depend on the execution profile.
   Frozen workers reject new behavior-changing flags unless explicitly authorized.
5. Add a focused compaction transaction module. Bind the snapshot and protocol,
   request a tool-free untrusted summary, validate and publish it, then atomically
   replace only the active context. Test interrupted commit and all failure paths.
   Retain real charges, failures, unknown usage and execution history on rollback.
6. Integrate closed metadata/phase validators, installer dependencies and existing
   operator documentation. Run focused fault tests, full isolated Python/Node suites
   and independent adversarial review before claiming source completion.

Each executor owns a small tested artifact. Observe a failing test before behavior
changes. Read current AGENTS.md and carry its dispatch-anomaly rule in every brief.
Use isolated worktrees or disjoint owned files to preserve concurrent diagnosis work.
Review and reconcile overlapping edits before integration. Do not reset or silently
attribute another session's changes, or replay an interrupted worker's effects.

### Phase 7 review closure — references and repeated compaction

The 2026-11-01 independent review found that dropped groups could lose discoverable
references, while earlier memory rows remained permanently outside compaction units.
The reviewed source-only correction uses the existing result reader, not a new tool
or a larger permission boundary. No deployment or commit is authorized by this plan.

- [x] **Frozen, paged reference index.** Add these `ReceiptStore` methods in
  `engine/context_receipts.py` without changing `record` or `read` signatures:

  - `ReceiptStore.index_ref(self) -> str`
  - `ReceiptStore.capture_complete(self, ref) -> bool`

  `result://<scope>/index/<H>` identifies the immutable reservation prefix `1..H`.
  Generate fixed-width, padded reference lines directly from byte offsets and
  sequence numbers, using at most one page of memory. Do not build a full manifest
  or allocate an index receipt/blob. Enforce owning scope, canonical high-water,
  current receipt bounds, and the existing 4096-byte page limit. Add RED/GREEN tests
  in `tests/test_context_reference_index.py` for cross-line/9-to-10 pagination,
  reservations between pages, malformed/foreign/future handles and capture faults.
  Run `python -m pytest -q tests/test_context_reference_index.py`.

- [x] **Discovery frame and consolidatable memory.** Update
  `engine/context_runtime.py` and `engine/context_compaction.py`. Bind snapshots to
  the frozen `reference_index_ref`, and bind summary source identity to snapshot,
  bindings and selected groups. Mechanically include the index/reader frame outside
  untrusted notes and measure it before publication. Register replacement memory as
  an unpinned singleton group by engine row identity, including identical-note and
  reused-content cases. Preserve contiguous complete groups and the newest real
  batch. Keep groups with incomplete receipts as retention pins. Add three-success
  and identical-note regressions in `tests/test_context_compaction_history.py`.
  Verify old handles/artifacts remain discoverable and memory does not accumulate.

- [x] **Rollback and boundary matrix.** Add
  `tests/test_context_history_rollback.py`. Inject state changes during summary and
  persistence, publisher failure and CAS rejection. Check unchanged payload, pins,
  groups and discovery frame on failure, with real attempt charges retained. Exhaust
  receipt capacity during paging and verify old frozen index handles remain readable.
  Run all three new test files, then the full context suites and independent review.

Publisher failure keeps the documented bounded-prefix receipt fallback: preserve
the actual outcome/exit, seal the consumed slot and report incomplete capture. It
does not itself prove a model-window failure or authorize replay. Otherwise-admissible
dispatch may continue. Compaction must retain the group when complete capture is
unavailable, so omitted references in an uncaptured suffix cannot silently disappear.

#### Identity follow-up from final review

- [x] Carry deterministic snapshot-position lineage alongside candidate messages.
  `_candidate` returns `(messages, lineage)` and the internal new-mode commit callback
  receives a fourth lineage argument. `ContextRuntime.commit_view` requires keyword
  `lineage`; `AutomaticCompaction.commit` forwards it unchanged while restoring only
  tool IDs. Bind lineage in summary source identity. Validate exact tuple shape,
  order/range/uniqueness, one replacement position, retained content, whole omitted
  groups, pins, incomplete groups and newest real batch before allocating row IDs.
  Reuse retained engine IDs directly; never reconstruct them from global content hashes.
  Update every internal callback fixture deliberately, without TypeError retry.
- [x] Add trusted `group_kinds` to snapshots. Missing metadata keeps pure fixtures'
  real-batch default. Validate kind/count/digest and protect the last native/prompt
  group independently of group-list position; other unpinned memory remains eligible.
  Runtime commit independently revalidates its own newest real group.
- [x] Add `tests/test_context_history_identity.py`: retained incomplete A with an
  assistant envelope identical to selected B (both A/B/C and B/A/C orders), trailing
  standalone text after newest real batch, malformed/stale lineage rejection with no
  allocator/state mutation, and three successes with duplicate content/capture faults.

### Operator-approved Phase 7 rollout — 2026-11-02

The operator approved live validation, deployment, commit and push after passing checks.
The earlier source-only approval statements above remain the history of their original
scope, not a prohibition on this separately approved rollout. Follow design section 8.4.
Independent bounded rollout plan: `20261102-015327-ctx-rollout-plan-1ebd` (GPT6.1 max).

- [x] Review current rules, source contracts, installer selection and tracking branch;
  fetch `origin/master` and confirm no divergence from `33d0c66` before new release work.
- [x] Add a small test-first live-canary driver with offline isolation/privacy/protocol
  checks. Live execution is an explicit operator action, not part of normal pytest.
- [x] Run source Claude Chat and GPT Chat no-auto smoke tests, then GPT Responses
  compaction/continuation. Use original policy root and fresh exact launch identities,
  with all canary accounting and run sidecars in temporary storage. Start with explicit
  window 65,536, reply 8,192, safety 4,096 and a 0.50 canary-only trigger fraction;
  pre-meter the reducible fixture geometry rather than padding the pinned task.
- [x] Independently review new instrumentation/repairs and run final isolated Python,
  JavaScript, compilation, whitespace and complete installer gates. Pin all selected
  bundle members, not merely the entrypoint or current HEAD.
- [x] Quiesce owned release workers, retain a pre-install inventory and complete rollback
  set, apply the installer, then verify every installed member and repeat the same-run
  compaction smoke test using the installed launcher and watcher.
- [x] Inspect/stage only intended source, preserve the pending `AGENTS.md` anomaly rule,
  commit and push normally to the existing remote/tracking branch after installed
  verification. Verify remote commit identity and retain rollback references.

Historical attribution remains open for `20261101-132134-context-policy-plan-1101`.
Its original `*.md.engine.diagnostics.jsonl` and `*.md.observer.diagnostics.jsonl`
under `.claude/agent-runs/` establish request start and Windows exit 1, not the cause
or initiator. Do not claim fresh validation repairs missing historical evidence. No
new unexplained canary/dispatch/accounting signal may be ignored during promotion.

Source live-canary evidence (metadata only; private run artifacts remain in temporary
storage, and synthetic fixtures remain under the ignored original-root fixture folder):

- Claude Chat, launch `9f3b86a4-13a0-4cb2-8d58-c970ae2f9369`: complete, three checked
  requests, two complete reads delivered, paired finish, no compaction, exact accounting
  coverage/totals. GPT Chat, launch `11b2047c-7766-4f89-8ce3-a85440ff67c4`: same gates pass
  on its nested high-effort Chat route.
- GPT Responses, launch `0a37fb18-af3e-4f61-bb8b-7bd2611753c0`: complete, nine checked
  requests (eight ordinary generations and one maintenance generation), all seven reads
  complete and delivered. One committed compaction removed four older groups, reducing
  estimated context from 33,929 to 13,211 tokens with pinned rows/newest batch retained.
  Subsequent fresh reads and actual memory-bearing requests used the same route/model/
  effort. The 27-row original journal remained paired and append-only; usage reconciled
  by call order, phase, count and totals (150,665 input / 1,945 output tokens reported).
- Earlier source launch `f913a39e-3c60-474f-b4fc-1a8107526ddf` failed in the diagnostic
  host's late sibling import after two successful reads. Original engine diagnostics
  identify `ModuleNotFoundError` at `recovery_runtime.py:14`; the driver now reproduces
  the selected CLI script-directory import path and restores it afterward. A real cold
  sibling-import regression and the successful fresh Claude run close that defect.
- Earlier Responses launch `2518fd73-ce1a-457a-84ea-6a81c84d9e72` was safely stopped by
  a driver guard before any file action: the model requested a complete known file with
  `start_line=1` and an end beyond EOF. The driver now accepts only full-covering bounds
  on the same authorized file, verifies the real reader's complete rendered result and
  still rejects partial/outside/repeated reads. A regression uses the real paged reader;
  the successful fresh Responses run closes that mismatch. No command was replayed.
- Driver reviews found real CLI/receipt/index/usage, paired-finish, coverage and late
  evidence-retention gaps. Repairs were RED/GREEN tested; 32 offline canary tests now
  pass. Corrected watcher invocation uses exact receipt-owned index/transcript paths,
  not an unsupported `--runs-dir` argument. No monitoring failure changed worker effects.

These tests establish requested wire caps and successful small-window execution, not
provider enforcement of output caps, exact unseen tokenization or full 1M-token capacity.
No new engine-source changes were needed by the canary repairs.

Final scoped independent confirmation `20261102-031837-ctx-final-delta-e815` reports no
blockers after the phase-ordered accounting, late report-write preservation and complete
range-read repairs. The complete 45-member installer selection is pinned as SHA-256
`1d79dd64f7dc3c04da23df435b7f304399c72360593618727eef710e34c7b888` (ordered member names
and payload digests). Pre-install inventory records 37 existing members and eight new
context members. A complete rollback must restore the former and remove the latter;
the entrypoint digest alone is not the release identity.

Deployment and installed verification — 2026-11-02:

- Applied the original installer CLI's complete contract gate and apply path, with an
  additional operator-hosted pin/quiescence check after tests and before mutation.
  An earlier busy-client refusal made no changes; no excluded project was interrupted.
- All 45 installed members match the pinned payloads. All 37 existing-member backups
  match their pre-install SHA-256 values, timestamp `20261102T025452Z`; the eight new
  context members are recorded for removal during a complete rollback.
- Installed launch `6f8b1f34-aa17-4d6c-8027-d1c56de8d7a3` passed the same-run Responses
  smoke: nine checked calls, one committed estimated reduction 33,982 → 13,207 tokens,
  four removed groups, pinned/newest retained, fresh reads and memory-bearing requests,
  all seven reads complete/delivered, normal paired finish and intact 27-row journal.
  Reported 150,898 input / 2,098 output tokens reconcile by phase/order/count/totals.
  The checkpoint is referenced by the actual publisher-owned artifact URI, not a guessed
  file path. Provider enforcement remains unverified.
- Initial installed launch `58cfb665-2218-4f68-9c46-0dbc05d06ebb` was stopped by a
  diagnostic-only ownership guard that accepted only the latest index and active group
  refs. The engine's receipt store already accepts earlier frozen prefixes and sealed
  compacted receipts. A real-store RED/GREEN regression repaired the guard to reuse that
  owning store's closed validation; foreign/future/file refs still fail before paging.
  Installed engine bytes did not change and no command was replayed. Final regression
  and independent confirmation cover this canary-only repair before commit/push.

Scoped pager review `20261102-040854-ctx-pager-review-ecde` reports no blockers. The
owning-store regression is included in the final full run; installed smoke and backup
verification remain successful without another shared-engine replacement. Only the
intended source/docs/tests and the pending `AGENTS.md` rule belong in the release commit;
no temporary run content, manifests, sidecars or synthetic fixture directories are staged.

Final candidate validation: 4,588 Python tests passed and 17 opt-in/platform tests skipped;
91 JavaScript tests passed. Compilation and the complete staged whitespace check passed.
The common credential-signature scan found only an unchanged synthetic narration fixture
outside this changeset. The absent shell `rg` command was not treated as a scan success; the dedicated
content-search tool performed the checks. The temporary deployment safety host also passed
its three offline tests without weakening or skipping the original installer contract gate.

Publication: verified feature commit `4a3a63a` was pushed normally to `origin/master`.
The pending `AGENTS.md` rule is included. No private runtime artifacts or global settings
were committed. Historical planner attribution and provider-capacity/enforcement limits
remain explicitly open; this publication does not claim to resolve or verify them.

## Phase 8 — user-approved provider compatibility repair (2026-11-02)

**Goal:** close the directly observed cap, terminal/accounting and capacity-assumption
defects without replacing token admission or silently changing provider routing.
**Architecture:** retain the current pure policy and installed-harness boundaries;
repair explicit terminal handling, select a small model/route operating profile locally,
and validate its wire-cap mapping with the existing isolated one-case probe host.
**Approval:** the operator approved this approach end to end at 10:21:59Z. Follow design
section 8.5. Independent concrete plan: `20261102-112607-provider-compat-plan-bbbc`.

### Task 8A — explicit Responses partial terminals and paid usage

**Owner/files:** first executor owns `engine/delegate.py` terminal/stream/one-shot paths,
`tests/test_engine_oneshot_stream.py`, and a focused `tests/test_provider_terminals.py`.
Use the existing temporary `harness` fixture from `test_engine_reasoning`; inspect native
stream callers before changing shared event handling. No other writer owns delegate
until this task finishes and the root verifies its effects.

- [x] Inventory callers of `gpt_stream_event`, `stream_gpt_oneshot`, native Responses
  turn assembly, one-shot completion and usage/terminal consumers. Read bounded ranges.
- [ ] Write synthetic failing cases for text + `response.incomplete`, reasoning-only
  zero-text incomplete, failed/error usage, duplicate cumulative usage snapshots and
  absent terminals. Example terminal data:
  `{"type":"response.incomplete","response":{"status":"incomplete","incomplete_details":{"reason":"max_output_tokens"},"usage":{"input_tokens":100,"output_tokens":20}}}`.
  Assert actual usage 100/20 exactly once, stop code `response_incomplete`, exit 1,
  partial text retained, one HTTP/model attempt, no fallback and no native dispatch.
- [x] Run `python -m pytest -q tests/test_provider_terminals.py --tb=short` and observe
  the intended RED failures before implementation.
- [x] Implement the smallest shared terminal classification: capture actual usage first,
  distinguish incomplete/failed/missing terminals, preserve partial output once, and
  propagate known partial state without accepting incomplete function arguments.
- [x] Run the new cases plus `tests/test_engine_oneshot_stream.py`,
  `tests/test_context_usage.py`, `tests/test_context_attempt_boundaries.py` and
  `tests/test_engine_live_turns_gaps.py`. Root independently verifies code and tests.

The first executor delivered RED-only cases before its turn reserve; no source change
or live call occurred. The bounded follow-up implemented the repair and passed 88
focused tests. Root then rejected an in-place heading rewrite, wrote an append-only
failure regression, and replaced that behavior with the existing streamed heading plus
an appended partial marker. The corrected focused set passes 89 tests. Heading text is
not terminal authority, and already-emitted bytes are never rewritten.

### Task 8B — real cap mapping and measured operating profiles

**Owner/files:** second executor, after 8A, owns the remaining narrow delegate preflight/
route/policy integration, `engine/context_policy.py` only if its pure interface needs
extension, and focused `tests/test_provider_profiles.py` / existing reply-limit tests.
No new runtime module or settings-schema expansion is planned.

- [x] Run isolated high-effort GPT Chat and Responses retention probes at 128k, 256k,
  then 512k units. All five markers survived on both routes. Largest reported inputs
  were 513,757 (Chat) and 513,746 (Responses). Six single-POST calls consumed less than
  1.81M reported input tokens; exact backend maximum remains unknown.
- [x] Regress cold/frozen/legacy precedence, actual-model/route/effective-effort profile
  choice, explicit override provenance and unknown-route unverified metadata. Configure
  the known GPT route operating window as 512,000, not a claimed backend maximum; retain
  existing reply/safety formulas and valid auto-compaction thresholds.
- [x] Regress one bound per packet and correct preflight/serializer mapping. Prototype
  known Claude `max_tokens` in source; leave legacy/frozen payload bytes unchanged.
  Reject wrong/multiple/malformed bounds, and do not claim enforcement by client clipping.
- [x] Root runs finite source Claude 128/512 cutoff tests before adopting that mapping.
  If it does not enforce the bound, retain the explicit blocker rather than editing the
  gateway or selecting another model. No raw provider text enters source/tests/reports.
- [ ] Regress the observed Claude error-as-text carrier and Chat cap-exhaustion semantics.
  Ambiguous text cannot count as completed proof; preserve usage and no-replay behavior.

The flat `max_tokens` source prototype was tested, not adopted: launches
`74f9ac08-752b-4b1d-9c2d-a6526a706def` and `616d2f51-fc5e-4a8c-8ddc-87d5b596f641`
requested 128/512 but reported 14,521/14,512 output tokens with the same visible-output
digest. Root reverted the unsupported field selection and retained strict single-bound
validation. Existing read-only model-catalog observation supplied no parameter/context
metadata for these two aliases. Contract discovery is required before another mapping;
no remote gateway edit or replacement model has been authorized as a workaround.

Forwarding-contract escalation `20261102-121817-claude-cap-unblock-e3ae` requires a
deployment-matched handler-to-pipe artifact covering extraction, merge/override
precedence, integer/reasoning constraints and the outgoing provider field. Generic
OpenAPI acceptance or public conventions cannot prove this custom deployment forwards
`params.max_tokens`; no further guessed POST was performed. No such authorized artifact
was supplied or found in project memory, and the existing model-catalog GET has no
forwarding information. This is a contract/access blocker, not proof that every possible
client bound is unsupported. Release and gateway changes remain held.

### Task 8C — diagnostics and acceptance integration

**Owner/files:** an independent probe executor may own only the ignored runtime host
`.claude/agent-runs/provider-probes/1e793d41/probe.py` and its synthetic tests, in parallel
with 8A. Root/consumer executor owns `src/agent_control/run_diagnosis.py`, watcher/CLI
consumer integration only where required, corresponding tests and existing docs.

- [x] Extend the existing host only for actual source/installed entrypoint and expected
  cap field selection. Observers never rewrite requests, results, counters or model IDs.
  Preserve one-POST guard, exact launch receipt/readiness/watcher and private Temp indices.
- [x] Cover known partial codes in metadata consumers without inventing success or
  storing reply text. Test field allowlists and synthetic privacy markers.

The source diagnostic extension is GREEN (46 offline tests) after the initial executor
delivered RED-only cases. Root corrected a test that compared independently randomized
launch identities rather than the engine-selection effect. Consumer regressions exposed
missing `response_incomplete` and existing `context_admission` allowlists; both now
survive model decoding, watcher incomplete classification and non-budget diagnosis.
The complete watcher/diagnosis set passes 79 tests. No source/installed live request was
replayed by these corrections.
- [ ] Verify source/installed default-policy metadata without forcing diagnostic 8M
  overrides. Demonstrate source repaired usage on a real capped Responses request and
  actual Claude cap enforcement. Discovery stays within 12 POSTs / 2.5M projected input;
  review any extra acceptance budget explicitly at the root before additional calls.

Source live launch `37dce8d5-1d15-455b-919d-542d4d35c0a8` used the actual default GPT
operating window 512,000 (no diagnostic window/safety override). The server stopped at
128 output tokens, including 112 reasoning tokens. The repaired normal usage journal
retained 1,608 input / 128 output exactly once; the source watcher reported known
`response_incomplete` instead of a generic failure. The policy-only completion artifact
records the high-effort Responses evidence scope, 513,746 retained-input lower bound,
unknown declared maximum and unchanged opt-in compaction semantics. No installed bytes
changed. Root independently reran the integrated eight-file policy/adapter set: 247 pass.

### Task 8D — review and controlled publication

- [x] Independent source/spec review; close each finding with original effects plus
  RED/GREEN evidence. Run the full isolated Python and JavaScript suites, compile and
  staged whitespace/privacy checks; old passing counts are not acceptance evidence.
- [x] Pin the complete installer-selected bundle and before-images. Use the original
  installer contract gate with quiescence checks before/after tests; do not kill or
  steer unrelated project workers. Verify every installed payload and rollback backup.
- [x] Run bounded installed repaired-cap/partial-accounting and same-run compaction
  acceptance. Record reported measurements and unresolved attribution limits truthfully.
- [x] Stage only intended source/tests/docs, preserve the existing `AGENTS.md` rule,
  commit and push normally to the existing tracking branch after installed acceptance;
  verify remote SHA, clean checkout and deployed hashes. Do not force/amend/create a PR.

### GPT-first scope approval — 2026-11-02T13:31:24Z

Follow design section 8.6 for the new reduced-scope release. The prior Claude contract
blocker is retained for that deferred capability, not a requirement to pretend its
ignored fields work or to withhold otherwise verified GPT fixes indefinitely.

- [x] Independently review the held candidate for GPT terminal/accounting/profile safety
  and Claude non-regression, including malformed terminals, duplicate paid snapshots,
  no replay/native partial dispatch, cold validation and explicit evidence provenance.
- [x] Close release findings test-first and run fresh full Python/JavaScript suites,
  compilation and whitespace/privacy checks. Known Claude forwarding and ambiguous
  error-carrier work remains deferred; no replacement gateway behavior is invented.
- [x] Source acceptance: normal GPT compaction/continuation, explicit capped Responses
  with real usage preserved and fixed partial stop reason, ordinary native Claude
  compatibility, and unchanged Claude/unknown/legacy policy defaults.
- [x] Capture a new complete-bundle payload/before-image inventory; deploy through the
  unmodified installer contract gate plus pre/post-test quiescence; verify all installed
  members and all old backups. Do not use the earlier release's hash as this candidate.
- [x] Repeat the decisive installed acceptance, then publish intended source/tests/docs
  normally to the existing branch. Record actual counts, hashes, commit identities and
  remaining limitations without staging private runtime or probe content.

The live release budget is 30 generation POSTs / 650,000 reported-or-projected input
tokens, separate from completed discovery. There are no more large boundary searches.

Independent GPT-first review `20261102-143612-gpt-first-review-ee06` found three
source release blockers: native partial termination skipped prior staged-patch export;
context-rejection conversion bypassed current/earlier paid usage; malformed terminal
objects bypassed typed partial closure. Root checked the actual paths before assigning
the exact synthetic regressions and smallest repairs. The pre-repair full run was
explicitly stopped as stale and is not counted as validation. No deployment occurred.

The first three blockers are covered by 41 terminal regressions and 277 integrated
focused tests. Rereview then found two additional boundaries: a native paid-idle
usage extra could enter the anomaly artifact, and non-streaming one-shot context
conversion bypassed earlier paid usage. Root reproduced both, made anomaly logging
metadata-only at its canonical boundary, and added the non-streaming paid-context
closeout. The expanded related set passes 356 tests. Fresh full regression and narrow
independent confirmation are required after these final functional changes; earlier
stopped runs remain excluded. Existing original artifacts are never rewritten.

The next full run completed with 4,667 passes, 17 skips and two failures. Root verified
one real local/provider regression: a local pre-dispatch one-shot context refusal lost
its existing local PARTIAL report. The other failure was an obsolete raw-body anomaly
golden. A new local-after-paid regression was RED before a shared closeout helper
restored the local marker while retaining prior usage for both exception origins.
The privacy golden now requires metadata only. The expanded 12-file set passes 437
tests; a fresh full run is required for this final amendment. The earlier `647de6a1...`
candidate pin is superseded and must not be deployed.

The local/provider amendment review found another streamed boundary: a paid cut/idle
reply's pending usage could be discarded by the next preflight outside the try block.
Root expanded the regression to actual legacy-byte overflow after Chat cut/idle and
Responses idle, with known/unknown earlier usage, plus local/provider in-stream
conversion with present/absent current usage. A shared preflight/carry boundary makes
all 14 new cases GREEN without adding phantom rows. The related 12-file set passes
451 tests; scoped review `20261102-154527-gpt-first-stream-carry-ba26` reports no blockers.
The final candidate pin is
`18ea72d7e2e65d7d4d506aa6b6c274cc557d49efe6f7ce1a4d3b333f20311dd4`, covering all 45
installer members; all 45 pre-install hashes still match the prior `1d79dd64...` bundle.
Only the fresh final full-run result may clear the remaining apply gate.

### GPT-first deployment evidence — 2026-11-02

The final full Python run passed **4,684 tests**, with **17 optional/platform skips**, in
550.18 seconds. JavaScript passed **91 tests**; compilation and whitespace checks passed.
All independent release findings were closed with RED/GREEN evidence; the latest scoped
confirmation reports no blockers. Failed/stopped earlier full runs remain historical,
not acceptance evidence.

The original complete installer contract gate passed. A first attempt was refused by
the post-test quiescence guard before any backup or copy because unrelated clients had
started; their tasks were not interrupted. The next quiescent attempt succeeded at
`20261102T152421Z`. All **45 installed hashes** match final bundle `18ea72d7...`, and all
**45 fresh rollback backups** match the immediately preceding `1d79dd64...` before-images.
No earlier candidate pin was substituted. No gateway/application settings were changed.

Installed acceptance, with private run artifacts/accounting kept only in temporary
storage and no command replay:

- GPT Responses launch `e2deb1a3-c592-4e16-a256-2948ec581e33`: all nine generation calls
  validated, one committed compaction **34,091 → 13,911 estimated tokens**, four older
  groups removed, pinned/newest rows retained, fresh reads and memory-bearing requests,
  all seven reads complete/delivered and a normal paired finish. Original 27-row journal
  stayed append-only and paired; reported usage **153,162 input / 2,315 output** reconciled
  by phase, order, count and terminal totals.
- Claude Chat launch `db1b2d5e-667b-4efd-92f1-769017863e26`: ordinary native compatibility
  passed on the original model, Chat route and medium effort, two reads complete/delivered,
  three validated requests and normal paired finish, **32,304 input / 596 output** reported.
  This is not output-cap enforcement proof and does not change the existing 1M policy.
- Capped Responses launch `78fe4784-45ad-4d7b-96bc-507f963506dc`: exactly one POST,
  actual default operating window **512,000**, explicit reply 128 and default safety
  25,600, no diagnostic-window override. The server ended `incomplete/max_output_tokens`;
  the normal journal retained **1,608 input / 128 output** exactly once. Exit 1 and the
  watcher `INCOMPLETE/response_incomplete` are the expected partial outcome, not success
  masquerading as a complete answer. No retry/fallback followed.

The release checks used **26 generation POSTs** and **374,548 reported input tokens**,
within the approved 30 / 650,000 bound. These counts exclude earlier discovery and
normal engineering/reviewer work. The private Claude forwarding/access limitation and
historical planner attribution remain explicitly unresolved under the approved reduced
scope; only verified GPT fixes and Claude-compatible common changes are released.

### GPT-first publication evidence — 2026-11-02

Release commit `98c4acbce5f6c4ccdae2a3428dfa6450c671bf56` was pushed normally to
`origin/master`. A direct remote lookup matched local HEAD, the checkout was clean and
the tracking comparison was 0 ahead / 0 behind. Only the 16 reviewed source/test/doc
paths were included; no private runtime artifacts, probe content or temporary manifests
were committed. No force push, amend, branch change or PR was used.

The final JavaScript result was confirmed as 91 passed / 0 failed. A fresh committed-code
recheck of provider profiles and terminal accounting passed all 87 tests. The full
candidate result remains 4,684 Python passes / 17 skips; no functional source changed
after that run. The pre-publication verifier again confirmed all 45 installed payloads
against bundle `18ea72d7...` and all 45 fresh rollback before-images.

Publication does not establish GPT's exact backend maximum or full-1M capacity, turn
estimated token admission into exact measurement, verify Claude's ignored output caps,
resolve the denied private forwarding contract, or close the historical planner's
exit-1 attribution gap. Those documented limitations remain unchanged.

## Phase 9 — Opt-in bounded multi-file reads Implementation Plan

> For agentic workers: use the existing installed harness, not built-in Task or new
> Agent Manager sessions. Follow task-by-task TDD; the coordinator owns independent
> verification and dispatch-anomaly closure. Read AGENTS.md, this plan and spec §9.

**Goal:** reduce independent file-read model round trips without changing default or
frozen behavior, authority, provider routing or existing upstream cache semantics.
**Architecture:** one explicitly enabled `read_files` native tool, backed by the existing
`engine/paged_tools.py` producer and confined Runner readers. Existing modules remain
the deployment family; no new framework, service, runtime module or setting schema.
**Tech stack:** Python stdlib, pytest synthetic fixtures and existing scripted-model
harness; Node regressions for unchanged dashboard behavior.
**Spec:** `docs/superpowers/specs/2026-10-31-recoverable-worker-execution-design.md` §9,
source-only design approved through the operator question on 2026-11-02.

### Global constraints and ownership

- Explicit `--read-files --profile resilient-v1 --context-policy model-v1` is required.
  Reject one-shot, supervised, approved-worker and recovery-bound use before settings,
  gateway credentials, run registration or model access. No global defaults change.
- Maximum 8 entries. Bound the entire serialized result to `MAX_READ_CHARS`, including
  statuses and continuations; preserve existing finite I/O/resource caps.
- Only synthetic fixtures and temporary paths; no live accounting, private run bodies,
  source/gateway canaries, provider fields, model/route/effort changes or deployment.
- No source commit, push or application action. Root owns spec/plan/operator-guide;
  one harness executor owns the scoped source/tests below, with no overlapping writer.
- Claude executor uses medium effort, ponytail, native tools, one tool per reply and
  short writes. It edits the actual approved source with `--write`, not invisible staged
  changes; all test fixtures and accounting are temporary. This is disjoint ownership,
  not a claim that the canonical tree is a filesystem sandbox.

### Review focus

- A malformed last entry must not permit an earlier read or any preceding native action.
- JSON escaping, Unicode, very long paths/lines and error envelopes must fit the whole
  result cap; no post-hoc clipping that hides statuses or loses a continuation.
- A rejected/unserved/partially delivered read must not become a duplicate-read pointer
  falsely claiming the complete content was already delivered.
- A second call must recheck current data/permissions, including staged content and URI
  grants; this is not a local file-content or response cache.
- A flag omitted from old/frozen requests must not change tool schemas, ordering, system
  messages, payloads, supervisor capabilities or approval digests.

### Task 9A — Bounded producer and single-tool interface

**Files:** modify `engine/paged_tools.py`, narrow integration in `engine/delegate.py`;
create synthetic `tests/test_read_files.py`.
**Interfaces:** `validate_read_files(requests: object) -> tuple[dict, ...]` validates
1–8 closed entries with nonempty string `path` and optional positive integer
`start_line`/`end_line` (reject bool, null, reversed bounds and unknown fields).
`paged_tools.read_files(run, requests, max_read: int) -> str` returns bounded JSON text;
`Runner.read_files(requests) -> str` delegates only when `read_files_enabled` is true.
The optional Runner constructor keyword defaults false. The explicit native schema is
`paged_tools.READ_FILES_SCHEMA`, closed at both object levels, requiring requests/path
and setting minItems=1/maxItems=8. Render `{"results":[...]}` in request order, with
zero-based `index` and `status` = `served|error|unserved`. Served rows include `content`
and `complete`; partial rows additionally include `next_range` with `start_line` and
optional `end_line`. Error rows have a bounded `error`; unserved rows have a bounded
`reason` and no content. Structural/budget preflight failures are bounded whole-call
errors before reads. Indices bind to the original entries without repeating long paths.

- [x] Write RED tests for two small reads in order, 0/9 entries, malformed tail with a
  read spy == 0, unknown fields/bools/nulls/reversed ranges, and disabled dispatch.
- [x] Write RED cases for pending/disk/authorized URI equality, boundary and symlink/URI
  denials, mixed errors, duplicate reads, current-file edits between calls, Unicode/JSON
  escaping, exact envelope cap, long lines, unserved tails and readable continuation.
- [x] Run `python -m pytest -q tests/test_read_files.py --tb=short` and record the intended
  failures before source changes.
- [x] Implement through current confinement/paged-reading primitives, with budget
  passed into production. Reserve ordered entry statuses before bodies; complete
  serialized results must fit. Never trim a completed result to fit after reading.
  Expose continuation at a line boundary without skipping an undelivered partial line;
  explicitly report an unservable line instead of claiming complete evidence.
- [x] Keep ordinary `read_file` defaults/output unchanged. Only fully rendered complete
  whole-file delivery may add suppression state; failed/unserved attempts cannot add it.
  Every executed entry still receives current permission/data checks.
- [x] Run the new file plus `tests/test_tool_output.py`, `tests/test_resilient_profile.py`
  and `tests/test_resilient_guard_edges.py`; all must pass before the next task.

### Task 9B — Cold opt-in and native request integration

**Files:** scoped changes in `engine/delegate.py` and read classification in
`engine/context_guard.py`; create `tests/test_read_files_cli.py`.
**Interfaces:** CLI `--read-files`, internal `args.read_files` and
`Runner(..., read_files_enabled=False)`. `tool_docs`/`native_tools`/dispatch advertise,
validate and accept the tool together only for enabled direct resilient/model-v1 runs.
Use the explicit object-array schema instead of the generic list-of-strings inference.
No supervisor, context-policy, settings or provider wire schema is expanded.

- [x] RED-test cold rejection for omitted/legacy profile or context, one-shot,
  supervisor manifest/pin, `_HARNESS_APPROVED_WORKER=1` and recovery contract. Stub settings,
  load_cfg, registration and reads so any access before rejection fails the test.
- [x] RED-test valid opt-in, constructor/registry refusal without opt-in, malformed tail
  rejected by whole-native-batch preflight, and Chat/Responses schema conversion.
- [x] Characterize the pre-change baseline for canonical no-opt-in equality and old/frozen approval
  behavior; identical opted-in inputs give deterministic system/tool serialization.
- [x] Implement the cold guard immediately after parsing and before profile settings;
  thread only the explicitly validated boolean into the actual agent Runner. Extend
  only relevant existing tool classification/diagnostic handling, not write authority.
- [x] Run `python -m pytest -q tests/test_read_files.py tests/test_read_files_cli.py
  tests/test_resilient_profile.py tests/test_resilient_native_batch.py
  tests/test_context_agent.py tests/test_context_engine.py tests/test_provider_profiles.py --tb=short`.
  Confirm all commands target existing tests before invoking them; correct a filename
  reference rather than silently treating an empty selection as a pass.

### Task 9C — Equivalent-evidence request-count proof and source acceptance

**Files:** create `tests/test_read_files_performance.py`; root updates existing
`docs/operator-guide.md` and this plan with actual source-only acceptance evidence.
**Interfaces:** reuse `test_engine_reasoning.harness`, `test_engine_turn_budget._call`
and existing fake-clock/payload recording, not a direct-API or alternate engine copy.

- [x] RED-test a scripted baseline with read A/read B/finish (3 requests), then an
  opted-in read_files A+B/identical finish (2). Assert both actual delivered file bodies,
  ordered results, valid tool/result pairing, exit 0 and the same finish evidence.
- [x] Cover eight entries (9 versus 2 scripted requests), partial/error results, unknown
  versus zero cache usage, and unchanged provider-bound/context admission semantics.
  Log only test-local numeric counts/timings; no real prompt or response snapshots.
- [x] Run `python -m pytest -q tests/test_read_files.py tests/test_read_files_cli.py
  tests/test_read_files_performance.py --tb=short` and independently inspect the diff.
- [x] Obtain an independent GPT-high review through the installed harness with exact
  UUID/receipt/readiness/watcher. Reproduce and repair actual findings test-first, then
  rerun the feature and affected baseline suites. Do not relaunch unknown-effect work.
- [x] Run full isolated `python -m pytest -q --tb=short --show-capture=no`, full
  `node --test --test-reporter=tap tests/js/dashboard.test.mjs tests/js/diagnosis.test.mjs
  tests/js/panel.test.mjs tests/js/task-state.test.mjs`, compilation and `git diff --check`
  on the final candidate. Include all four current `tests/js/*.test.mjs` files, not a
  historically incomplete three-file selection.
- [x] Report tested request-count reduction and isolated timing observations without
  claiming a live-provider latency improvement, cache-sharing guarantee or invoice
  savings. Preserve the installed bundle unchanged; request rollout approval separately.

Self-review: tasks 9A–9C cover spec §§9.1–9.4 and all five review-focus classes. The
implementation method is the operator-selected current engine, not built-in subagents.
Only source behavior behind this new explicit opt-in is approved; rollout remains gated.

### Phase 9 execution ledger

Both the design and this implementation plan were approved through operator questions
in this session. Baseline is `45c8c0c`; before implementation only the two root-owned
spec/plan files differ. Installed entrypoint blob is `4ca3af1b...` and installed
paged-tools blob is `3e122ec1...`; shared installation is not a development target.

| Preflight pair | Producer / consumer check | Result |
|---|---|---|
| 9A self | Bounded JSON/read state against exact output and current-data tests | Fresh reads outrank optional suppression; do not introduce a content cache. |
| 9B self | Cold explicit activation against old/frozen request equality | Guard must run before profile settings; no supervisor schema extension. |
| 9C self | Scripted equivalent evidence against real latency claims | Request-count proof only; no provider timing or billing inference. |
| 9A / 9B | requests validator, closed schema and Runner enable keyword | Names and bounds agree; native preflight must reject malformed tails before actions. |
| 9A / 9C | Served bodies, complete/partial status and next ranges | Assert delivered evidence, not only a mocked call count. |
| 9B / 9C | Explicit flags, model-v1 fake transport and paired finish | Reuse actual CLI/serializer and temporary accounting fixtures. |

Ruling: execute 9A–9C as one tightly coupled artifact with one Claude owner and a fresh
independent spec/quality review before acceptance — this is the approved execution
method, avoids overlapping edits to delegate.py and avoids three redundant context loads.
Ruling: work in the explicitly approved canonical source checkout with disjoint root-doc
and executor-code ownership, not an unapproved new worktree or branch. Only source and
synthetic tests may change; no automatic commit, install, global setting or service action.
Ruling: include `read_files` in cumulative read classification, but not the legacy text
pager's arbitrary clipping family, which could corrupt its structured JSON statuses.
Ruling: a batch may conservatively leave complete reads unsuppressed to keep current-file
checks correct without a digest/content cache. Partial/error/unserved reads must never
gain whole-file suppression; original single-file behavior remains unchanged.
Ruling: use structured producer metadata for complete/continuation decisions, never parse
untrusted file content or a filename-bearing rendered header as completion authority.
Ruling: this existing plan is the persistent recovery map; temporary helper files are
not authoritative state. Reports stay in the original engine transcript. Do not create a second raw-content report
store in AgentControl state or a new `.superpowers` workspace for this historical plan.

Initial launch `45775488-34f8-460b-ac95-ce54e485aa7f` was rejected before registration,
model access or source mutation: the controller's Windows `os.execv` split the JSON
verification argument. The engine's argument guard behaved correctly. A temporary
synthetic argv-child regression was RED under that launcher and GREEN (1 passed) after
using `subprocess.call` with a list argv. External scratch-test collection is explicitly
confined to its scratch root; no unrelated temporary application tree is collected.
The original failed receipt is preserved, and source blob checks still match the
baseline. Fresh corrected launch: `5eef7747-de4f-48cd-83d6-d23906ce456b`. No shared-engine
or permission change was needed.

Candidate `20261102-231544-fast-dispatch-implement-5eef` stopped max_iters after its last
work-turn prose rather than a usable finish. Its original transcript and frozen diff
are retained, not accepted as task completion. It reported 192 focused/affected passes
and identified missing compatibility coverage and tests written after source. No command
with an unknown outcome is being replayed.

Ruling: preserve the synthetic tests, restore only the three unaccepted prototype source
files to the exact baseline, then rebuild from the already failing tests with missing
cases added first. Restored blob IDs: delegate `4ca3af1b...`, paged tools `3e122ec1...`,
guard `b75a375f...`. Root observed the full retained feature suite RED: 60 failed / 4
passed in 1.97s, for missing APIs/flag/classification, not broken test collection. The
frozen prototype is review evidence only, not an implementation reference.

The worker's candidate_state_unavailable was traced separately: the existing bounded
snapshot refused this canonical tree at its 16,777,216-byte cap. A numeric-only bounded
probe observed 21,735 bytes remaining and the next file at 33,113 bytes, 713 entries,
0.193 seconds, with no deadline or entry-cap overrun. The guard correctly leaves
verification unconfirmed without changing test exit codes. Do not widen the budgets,
delete workspace data or weaken the gate. Root-owned final source/test hashes, independent
review and full isolated regressions remain required for source acceptance.

Ruling: a fresh GPT-high executor may complete the tightly coupled unit after the first
Claude run ended, following the standing escalation/execution rule. This does not switch
models inside an existing run or change any engine default, model route or effort policy.

Rebuild `20261102-234623-fast-dispatch-rebuild-db90` ended with a valid finish. Root
independently confirmed 105 feature passes in 4.93s; the worker reported 421 affected
and frozen-baseline passes. Root compilation and whitespace checks passed. The six
source/test identities were frozen for review; installed delegate/paged blobs still
match the release. Four Chat payload fingerprints were captured against the restored
baseline before rebuilding; the independent Responses-wire golden remains an explicit
verification limitation, not a demonstrated compatibility regression.

Fresh review `20261103-000118-fast-dispatch-final-review-bb82` found one new Important
range regression: the batch logical-line adapter treated the search producer's 8,192
character chunk limit as an unconditional logical-line limit. Root confirmed six RED
cases (disk/staged, long skipped prefixes, served long lines, and EOF without newline)
in 1.01s, while the existing single-reader ranges returned the expected data. Source is
not accepted until this is repaired and scoped re-review clears it.

Phase 9 fix round 2/5: prototype review findings were addressed by the TDD rebuild;
the bb82 logical-line finding is now open. Fresh same-model execution is permitted
because this harness cannot resume a completed native run; only paged_tools.py and its
feature tests are writable in this round. Preserve bounded chunk/scan/output limits and
skip unwanted long lines without retaining them; do not just remove the check while
enumerating physical chunks as logical lines. No interface, CLI, routing or default change.

JavaScript gate selection corrected after discovery: the three named files passed
84 tests, but `diagnosis.test.mjs` is also part of the full suite. All four files must be
run for source acceptance; do not report that partial selection as the full 91-test gate.

### Phase 9 source acceptance — 2026-11-02

Source-only completion is accepted by the coordinator after the following independent
gates on the same frozen six-file code/test candidate. No installation, source commit,
push, gateway edit or global-setting change was performed. HEAD remains `45c8c0c`.

- Fix round 2/5: `20261103-001220-fast-dispatch-long-lines-2049` reproduced the six
  root RED cases, added boundary/scan/continuation RED coverage (21 failed / 4 passed),
  and repaired only paged_tools.py and its tests. Physical chunks and scan budgets were
  not increased. The executor finished normally and reported 128 feature / 56 affected
  baseline passes; it did not claim its bounded snapshot became available.
- Scoped independent re-review `20261103-002102-fast-dispatch-rereview-cba5` marked the
  bb82 finding ADDRESSED, spec/quality PASS for the repair, and identified no new
  Important/Critical breakage. Together with the prior whole-candidate review's
  unaffected requirement assessment, there are no open Important/Critical findings.
- Root's exact three-feature-file gate passed **128 tests in 5.94s**.
- Root's full `python -m pytest -q --tb=short --show-capture=no` passed **4,812 tests,
  17 skipped, in 619.85s** (tracked process `bgp_19e91d417001g4b3fyq8FVZBWo`, exit0).
- Root's full four-file JavaScript gate passed **91 / 0 failed** on the final candidate
  (`bgp_19e91d432001Zp4McSqGVP03Rj`). Compilation and whitespace checks passed; only
  Git's LF/CRLF advisory was emitted.
- All six code/test Git blob identities below matched before and after the full suite.
  Only the approved three source files, three synthetic test files and existing three
  documentation files are changed. No derived test cache is counted as source evidence.

| Candidate path | Git blob identity |
|---|---|
| engine/delegate.py | `5fd945f21ae04be5272baac4cc2b00b1cf7df59a` |
| engine/paged_tools.py | `608dda739cc08870be46e5e76804ca9796197472` |
| engine/context_guard.py | `2e20a19037943c67645c32fffb8fe0b5ddfd7e06` |
| tests/test_read_files.py | `2a52b7b41c1035ff573f6b0dd247a42a5861d2e2` |
| tests/test_read_files_cli.py | `47555b51d613763e94c8b994e298cd1fbaf2a5db` |
| tests/test_read_files_performance.py | `011a71d119e1618f4c45f214fc76d4e38f71be04` |

The installed entrypoint/paged-tools/guard identities remained `4ca3af1b...`,
`3e122ec1...`, `b75a375f...` respectively. The new option is available only through the
development source until a separately approved complete-bundle rollout and live check.

Equivalent scripted read evidence, paired results and normal finish establish **3→2**
model requests for two reads plus finish (one third fewer) and **9→2** for eight reads
plus finish (seven ninths fewer). They do not establish real-provider elapsed-time,
cache-sharing, retention or invoice gains. Existing upstream caching is not replaced.

Retained limits: worker full-tree snapshot verification stays unconfirmed at its
unchanged 16MiB bound; independent root checks above are the source acceptance evidence.
No historical Responses-wire golden was separately captured: the four pre-change Chat
fingerprints, unchanged conversion path, enabled Responses tests and existing frozen
approval fixtures support compatibility without inventing that missing measurement.
An intrinsically oversized logical line may still require an explicit bounded error;
the implementation must not drop its suffix or claim complete delivery. All temporary
diagnostic artifacts and original incomplete runs remain evidence, not accepted releases.

## Phase 10 — Approved rollout, live measurements and publication

Approval: on 2026-11-03 the operator answered **Approve complete rollout (Recommended)**
to the explicit paid-trial/shared-install/normal-publication question. Binding spec §10.
Preflight confirmed unchanged accepted six-file candidate, unchanged old installation,
HEAD/remote `45c8c0c` and exactly the nine intended unstaged paths from Phase 9.

### 10A — Focused validation helper (TDD, then independent review)

Files: `tools/agent-harness/read_files_live_canary.py` and
`tests/test_read_files_live_canary.py`; existing core/installer/canary stays unchanged.
One scoped harness executor owns these two files; root owns docs and operator-only Temp
pin/guard/verifier scripts. Use the installed engine, ponytail and exact launch identity.

- [ ] Write and observe RED synthetic tests for explicit-live/fresh-Temp gates, no
  live accounting, budget boundaries/locking/corruption/unknown reservations/retry and
  duplicate-usage accounting, denied tools, full actual delivery/finish, default argv,
  cache null/zero and pass-through timing observation. No paid calls in these tests.
- [ ] Implement pure `BudgetLedger` reserve/reconcile, `Observer`, `run_case` and CLI
  around existing safe Temp/entrypoint/wire/journal primitives; no compaction geometry.
  Three modes: Claude baseline, Claude batch, GPT Responses batch; source/installed.
- [ ] Use one cumulative budget ledger: 30 generation POSTs / 650,000 input projections
  including retries/report/fallback. Reserve before network; no unknown-state refund.
  Emit only metadata. Normal model routes/efforts/windows/cap fields remain unchanged.
- [ ] Observe GREEN and affected canary regression gates. Independent review covers
  the helper and fresh temporary deployment guard/verifier, not another installer.

### 10B — Fresh source acceptance and bounded live trials

- [ ] Run final full Python, all four JS files, compilation and whitespace on the
  unchanged core plus reviewed helper/tests; preserve code/test identities.
- [ ] Initialize the fresh cumulative Temp ledger and execute sequential exact-identity
  source cases: Claude baseline, Claude batch, fresh Claude batch, GPT Responses batch.
  Start with two fixture files to conserve projected input; expand only if affordable.
  Nominal live baseline is 4 calls versus 3 batch calls with AGENTS bootstrap, not 3→2.
- [ ] Verify readonly effects, actual random-content delivery, normal finish/pairing,
  default wire policy and same original project identity. Log observed counts and timing,
  known cache read/write plus nulls, not fabricated causal/cross-agent/billing claims.

### 10C — New complete pin, original gate and fresh backups

- [ ] Select the complete original `_plan`, determine actual count (expected 45), hash
  every source payload and actual old destination. Create new immutable Temp manifest.
- [ ] TDD-check the temporary guard/verifier with fake roots/process metadata and
  original gate callbacks. Parse actual scripts; include ask-model/fanout/read-files
  canary as well as delegate/agent_await/context canary. No permission broadening.
- [ ] Verify parent directories; require source/target pins and no active shared clients
  before the full original gate and again before any backup/copy. If busy, verify no
  mutation and schedule one-shot/recurring goal waits; never kill/steer other projects.
- [ ] Invoke original `scripts/deploy_engine.py` main `--apply` unchanged, with its full
  gate, all-before-write backups, helpers-first/entrypoint-last checks and restoration.
- [ ] Verify every selected payload and fresh backup by full SHA/time/identity; retain
  actual rollback entrypoint. A refusal/failure requires effect checks before retry.

### 10D — Installed acceptance and publication

- [ ] Within the SAME remaining ledger budget, run installed Claude and GPT batch
  cases, verify actual read_files delivery/normal finish and unchanged ordinary policy.
- [ ] Record actual observed timings/cache usage and remaining uncertainties in existing
  docs. Budget reservations/known usage are metadata, never raw task/provider content.
- [ ] Inspect status/diff/log10, audit secrets/scope and stage only intended release
  code/tests/docs. Do not stage ignored runtime fixtures or any Temp artifacts.
- [ ] Normal commit/push to origin/master; no force/amend/PR/branch/config changes.
- [ ] Verify remote HEAD == local HEAD, clean checkout and installed complete pin.

Ruling: budget admission is a conservative reported-or-projected ceiling, as approved,
not an impossible promise about undocumented backend additions. Reserve at least UTF-8
wire bytes plus framing and twice the active estimate, retain unknown charges, and stop
on overrun — the cost if the projection is wrong is a halted trial, not silent retries.
Ruling: use two small live fixtures initially rather than seven so the conservative
byte-based projection leaves room for mandatory installed acceptance; live evidence has
lower breadth than the eight-entry scripted tests, which remain required and green.
Ruling: reuse only the old canary's safe pure utilities; its actual window is 65,536
(input budget 53,248), so its compaction argv/wire gates cannot represent normal-window
fast-read latency. Ordinary defaults and frozen provider fields must remain intact.

Planning consultation `20261103-091126-read-files-rollout-plan-68f8` completed tool-less.
It correctly warned that 2x an estimate is not a hard backend bound; the projection
definition, byte floor, no-refund accounting and smaller sample above resolve that
without claiming unsupported exact-provider knowledge or changing the approved cap.

### Phase 10 execution ledger

Validation implementation `20261103-092634-read-files-live-helper-c83b` finished normally,
with 38 RED cases before implementation and 38 focused / 70 combined GREEN cases.
Root independently confirmed the combined 70 passes in 5.02s. It wrote only the two
approved validation files; the accepted runtime hashes remain unchanged. No paid trial
or real trial-budget ledger has yet been created.

Temporary operator-host tests were observed 17 RED before implementation and 19 GREEN
afterward, including real quoted Windows argv parsing, original-gate identity/order,
pre/post-test busy/pin refusal, target drift and fresh/ambiguous backup checks.
Prospective metadata capture selected 45 payloads with source digest `43d9aaf3...` and
before digest `18ea72d7...`; this was NOT an installation. A readonly check correctly
refused active supported clients. Only PID/script metadata was observed; no other
project was inspected, killed or steered.

Independent safety review `20261103-094507-read-files-rollout-review-7cf7` found seven
Important issues before any paid call: unauthorized ledger ceilings/identities; timing
proxy usage/exception interference; incomplete wire/journal/usage acceptance; AGENTS
execution mistaken for delivery; lost original network anomalies; manifest-selected
verifier code; and same-second stale backup freshness. Paid trials/application remain
gated while this single safety-fix wave is completed.

Root independently reproduced verifier findings 6–7 with four RED tests (19 existing
passes): an untrusted synthetic installer was executed, destination mismatch reached
selection, a preexisting same-second backup passed, and missing before-inventory passed.
Root repaired only the temporary hosts, then observed **23 GREEN** in 0.89s. The verifier
now authenticates a fresh Temp manifest and fixed approved source/destination before
loading/selecting code. Manifest schema 2 records complete preexisting backup basenames
and before sizes; a qualifying rollback file must be new to that inventory, non-symlink,
correct size/full SHA and within the fresh timestamp boundary. The schema-1 prospective
manifest above is obsolete and cannot authorize application; capture a fresh schema-2
final manifest after source acceptance. Bundle payload digests themselves are unchanged.

Ruling: source findings 1–5 plus a real-loader offline integration are assigned as one
scoped TDD repair to `363292a9-1518-4289-b81d-6b50c25e9ec3`, installed-engine GPT-high,
actual engine PID 56636, receipt in the private Temp directory. The original worker
has ended; only helper/test files are writable. Root owns temporary-host fixes, so there
is no overlapping writer. A fresh independent safety re-review must clear the wave
before any paid trial. The core engine, old canary and installer remain unchanged.

The source safety-repair run `20261103-095758-read-files-helper-fix-3632` reached its
finite turn limit after final GREEN commands (115 combined / 83 helper). Root inspected
the original effects rather than relaunching it and independently confirmed 115 passes.
It included an actual selected source-loader/capability/receipt/index/journal integration
with fake urllib, three synthetic POSTs and valid proof; no live trial occurred.

Re-review `20261103-101102-read-files-safety-rereview-7f00` cleared six findings but kept
parser recursion transparency open and identified a cumulative output-snapshot regression.
Root took only helper/test ownership after the worker ended, added six observed RED
regressions, then made localized repairs. Decoder depth varies by Python build, so the
recursion tests inject that exact stdlib failure only for the synthetic payload, without
changing normal ledger decoding or persisting recursion settings. Tests verify original
exception identity/bytes, independent output-only/null counters and no duplicate input
charge. Root observed 121 combined GREEN cases after the last edit.

Final scoped closure `20261103-103516-read-files-observer-close-0621` marked both open
observer findings ADDRESSED, scoped spec/quality PASS and no new Important/Critical.
Root's final release gates passed **4,901 Python / 17 skips in 591.74s**, all **91 JS**,
compilation and whitespace (LF/CRLF advisory only). The superseded 4,895-case run is not
the final candidate. Runtime Phase9 identities remain unchanged; final helper/test blobs
are `ab52d4fb...` / `38412756...`. The cumulative trial ledger was initialized at
`read-files-live-budget-20261103-68f8.jsonl`, budget ID
`22f0d5a5-d4bb-40b7-b55f-c262feafcc68`, with only the 30/650000 header and no reservations
before the first paid source case. Development/review usage remains separate.

Root's final observer fix had six specific RED cases then **121 combined GREEN**;
closure review `20261103-103516-read-files-observer-close-0621` marked both remaining
observer issues ADDRESSED/spec-quality PASS. The fresh full gate passed **4,901 / 17
skips in 591.74s** plus all91JS before the subsequent canary text-contract clarification.

### Initial paid source observations and contract correction

All trials use the same cumulative budget; no refund or reset:

| Source case | Launch prefix | Generation POSTs | Engine-main elapsed [s] | Outcome |
|---|---|---:|---:|---|
| Claude baseline, two fixtures | `68bf0c34` | 4 | 18.270076 | Complete, paired, all3reads delivered/proved |
| Claude batch, two fixtures | `533c1856` | 3 | 13.810697 | Complete, paired, all3reads delivered/proved |
| Fresh Claude batch | `c3a7549e` | 3 | 13.934670 | Complete; firstrequest reported5,776cached /8,579input |
| GPT Responses, old path-only oracle | `0dfe3bd1` | 1 | 9.376858 | Refused BEFORE any original read |
| GPT Responses, explicit path-only wording | `058f61b6` | 1 | 8.414582 | Same pre-effect refusal |

The two failed GPT calls returned the existing legal native form
`read_file(path=AGENTS.md,start_line=1,end_line=200)`, not a prohibited path/operation.
Runtime optional-range schema and conversion were unchanged. The old observer enforced
a stricter literal path-only oracle; both original results remain failures under that
contract. Known effects: zero reads/deliveries, normal HTTP response usage captured,
interrupted receipt/index persisted. Each attempt remains charged (24,320 /24,432 input
projections; 4,795 /4,817 reported input), with no replay of an uncertain command.

Narrow max consultation `20261103-110448-read-files-gpt-contract-4f21` concluded this is
a full-read compatibility false-negative, NOT an end-to-end pass or proof of backend
normalization. It recommended versioning the task/oracle together rather than modifying
engine schemas or broadening authorized effects.

Ruling: `full-file-v2` permits only already-advertised native optional range keys on the
SAME pinned paths/order: strict positive integer bounds, effective start1, end/default
covering the complete current native line count. No null/bool/glob/unknown/partial forms.
No argument or payload rewriting. Single-reader output must equal the computed native
header plus exact complete body; header text is not parsed as authority. Batch records,
AGENTS outgoing delivery, journal/pairing/finish/nonce and all caps stay strict. This
changes the syntax oracle, not filesystem, command, write, URI or data permissions.

Root observed six compatibility REDs against the ACTUAL source loader (43-line synthetic
AGENTS, whole/over-cover/start-default/end-only), then caught and repaired native newline
counting versus Unicode splitlines with three more actual-reader REDs. The new+old
canary gate passed **136 tests in 6.61s**, including unchanged zero-effect denial cases
and exact-header/truncated-body failures. Scoped review `dbabe5db-716e-4a7f-b944-c864c44c8c86`
is pending; further paid calls remain gated on that verdict.

Budget after these original trials: **12 POSTs /329,103 charged projections**, all12
with known captured input, no missing/refunded reservations. Reported known input totals
105,242; projections deliberately remain charged. Remaining quota is 18 POSTs /320,897
input projections. Original raw artifacts remain only in private Temp. Earlier Claude
latency/cache observations are small old-contract samples, not matched V2 measurements,
statistical/causal claims, guaranteed cross-agent sharing/TTL or invoice savings.

Ruling: the next fresh full-regression refresh may run concurrently with the V2 scoped
review and later readonly GPT COMPATIBILITY case on the same frozen helper/runtime
inputs; installation/publication still wait for every gate. Do not use that case's
timing as a controlled performance comparison while the test workload is active.

Scoped review `20261103-111706-read-files-full-contract-review-dbab` passed V2 spec/quality
with no Important issue for the planned nonempty fixtures. New fresh GPT source trial
`cffb5f20-1b36-4c04-bead-12c69dd4fd3e` then passed full V2 acceptance: 3 generation POSTs,
15.888960s engine-main elapsed, all3read bodies delivered, valid nonce/finish/pairing,
default Responses/high/max_output_tokens32000 policy. First request reported3,082cached
of4,841input. This is a compatibility observation made while regression work was active,
not a controlled speed comparison. The two old failures are not retroactively accepted.

All paid source work is settled: 15 POSTs /412,578 charged projections, all15 with
known captured input and zero excess/missing/refunds. Remaining input quota237,422 and
POST quota15 are retained for mandatory installed checks and possible safe recovery.
The final frozen helper/test identities are `dc7c91e9...` /`7d08c950...`; all six Phase9
runtime/test blobs remain unchanged. The current full-refresh process is
`bgp_1a0ebd748001ebna7O3dKPZKsk`; no source is to change while it validates the V2 inputs.

Final schema-2 capture (still NOT applied):
`C:/Users/operator/AppData/Local/Temp/kilo/read-files-release-20261103-final-manifest.json`.
It binds all45members, source bundle
`43d9aaf3bf558189900f60fed3e3924b9657e221e32e075084d7a234e1a4538f`, before bundle
`18ea72d7e2e65d7d4d506aa6b6c274cc557d49efe6f7ce1a4d3b333f20311dd4`, and the complete
preexisting-backup inventory/sizes. The obsolete schema-1 pre-manifest is not usable.
Next action is the existing final V2 regression result, then a fresh pin/quiescence
check and original full-gate application only if quiet. Shared clients must not be
interrupted to satisfy this wait.

Final V2 regression refresh completed: **4,916 passed /17 skipped in636.49s**
(`bgp_1a0ebd748001ebna7O3dKPZKsk`). A fresh source/target pin and supported-client
quiescence check passed, followed by an actual original-main application attempt
`bgp_1a0f8e768001KuNjQSVjWfENuh` (`APPLY_NOT_BEFORE=20261103T103349Z`). The original
default contract gate returned successfully; SOURCE/TARGET pins still matched afterward.
The post-test process check then found active delegate/watcher clients and refused
before the installer reached any backup or copy. No project/task contents were inspected.

Root's separate readonly effect check confirmed all **45 target payloads and the complete
preexisting-backup inventory unchanged**, still source43d9aaf3... /before18ea72d7....
This refusal is an expected safe barrier, not a partial installation. The timestamp of
this refused attempt must NOT be used as the successful-apply backup boundary. A later
attempt must run the original full gate again, with a new actual timestamp; no cached
gate, shortened command, pin relaxation or client termination is allowed.

Ruling: wait on supported-client quiescence through scheduled goal checks; do not infer
which project owns the clients or kill/steer them. Runtime/helper inputs, schema2 manifest
and live ledger remain frozen. No installed canary or commit/push is permitted yet.

### Evening contract-gate anomaly — active investigation

After the known clients exited, the fresh gate at 21:26Z reported pin-matched quiet.
All eight frozen code/test identities still matched and the original installer was
unchanged. Apply attempt `bgp_1a350b981001nXDzZZd7R7oxTb`
(`APPLY_NOT_BEFORE=20261103T212859Z`) then stopped in the ORIGINAL contract suite with
exit1, before backup/copy. Root separately verified all45oldpayloads and all before-
backup names unchanged. This timestamp is not a successful-apply boundary.

The original `run_contract_tests` captures child stdout/stderr but discards them on
failure, emitting only exitcode. No original failing node is available from that
process. Do not label this transient, repair it by a blind retry or count a later pass
as explanation. A diagnostic-only host captured the exact command via the original
main's selection path while replacing deployment with a no-effect capture. It then
executed only that exact test command, never actual deploy/copy. Command identity:
123arguments, SHA256 `e1a0a22bbcf5f151d8328328e9a92539f5cb33cd174b8fb4f636affd220b6ecb`.

That diagnostic passed **3,261 /2 skips in421.47s** and retained its original private
stdout/stderr in Temp. It is evidence of non-reproduction, not a verified repair.
To identify an evidenced intermittent failure without guessing or weakening anything,
one bounded maximum-three-run reproduction is active as
`bgp_1a366c9be001Oj8FWoZcCzY6Fq`; it stops at the first failed exact command and retains
each private result. Iteration1 returned0; remaining iterations may still be running.
No code, installed payload, backup inventory or trial-ledger reservation was changed;
trial spending remains15POSTs/412578charged. Publication/installed acceptance remain gated
until the contract failure is explained or a concrete unresolved-evidence blocker is
reported. Read original artifacts in place; do not copy test output into source/docs.

Bounded reproduction completed: all3iterations passed **3261/2skips**, respectively
415.45s/426.96s/399.32s. Together with the first421.47s diagnostic, this is four current
passes, not a root-cause explanation. Independent effects remain45unchanged/before-
backup inventory unchanged. No test or gate was changed or skipped.

Narrow tool-less planning `20261103-232152-read-files-contract-fc85-plan`
(exact launch fc85cc1e-6814-4181-846d-f63f685f93b2, verified watcher FINISHED) identified
one remaining structural discriminator: the command-capture diagnostics bypass actual
deploy's pre-test planning and retained payload-plan lifetime. It is not evidence that
planning caused the historical failure. Exactly one context-faithful diagnostic is
prepared, retaining ORIGINAL guard/main/deploy/plan/full gate while unconditionally
stopping at the gate boundary before any backup/copy, even on success. A later pass
would still leave historical attribution unavailable; it is not installation approval.

The operator-only Temp observer had9RED safety cases before implementation, then14GREEN
cases including real original guard/main/installer wiring on synthetic temporary roots.
Independent tool-less review `20261103-233348-read-files-observer-d643` identified two
observation-fidelity defects: capture-write errors interrupted the original gate flow,
and Windows output newline translation changed returned text bytes. Root reproduced
both (3RED cases), repaired them without changing the original subprocess result, and
verified **17observer+23existing guard=40GREEN**. Incomplete capture remains diagnostic
failure but no longer suppresses original postchecks/error classification. These changes
are only private Temp operator controls; all8frozen release identities match unchanged.
Original installer remains unchanged. Temp ACL inspection found operator, system/admin
and sandbox principals, not public/Everyone access; no ACL or permission was modified.

Development calls above are separately registered (fc85:8650input/5778output;
d643:13218input/4727output; monetary spend unknown). They do not consume or reset the
live trial ledger, still15POSTs/412578charged with15POSTs/237422input remaining.

Before the single context-faithful run could start, a fresh pin/quiescence precheck
found8supported delegate/watcher clients and returned17. No client content/project was
inspected or affected. The diagnostic itself has NOT run and its fresh output directory
has NOT been created. Root again confirmed45unchangedpayloads and unchanged backup
inventory. Continue through a scheduled wait for quiet, not by bypassing the barrier;
the unresolved original contract failure remains active and publication is not ready.

Scheduled22:50Z precheck `bgp_1a39b3123001onDqo5cLCN6YAl` still refused safely with3
supported clients (wrapper/engine/watcher metadata only). Root independently reconfirmed
45oldpayloads and the before-backup inventory unchanged. No context-faithful test run
or install was started. Quiescence checking now recurs through a single harness schedule;
cancel it when quiet before starting the one diagnostic. This is not an OS schedule.

### Original-context result and concrete evidence blocker

The operator reported quiet at22:51Z. A fresh actual precheck passed
(`bgp_1a39c4985001khR4c45iSzhqux`); the recurring quiescence schedule was cancelled before
the single diagnostic. No client was killed or steered and no safety gate was bypassed.

Original-context observation `bgp_1a39cf2580013FabzOksCKh28c` ran the actual original
guard/main/deploy/retained-plan/full-gate path. Its diagnostic timestamp225214Z is NOT
an installation or successful-backup boundary. The ORIGINAL test subprocess returned0:
**3261passed/2skipped in454.11s**, with complete private stdout/stderr and no observation
error. Original post-test pins matched; two newly active supported clients then caused
the expected quiescence refusal. The observer unconditionally stopped before the first
backup/copy regardless. Root verified all45oldpayloads and the entire before-backup
inventory unchanged afterward. Original installer remains unchanged; local HEAD remains
45c8c0c and exactly the11intended release paths are uncommitted.

This completes the last bounded discriminator: four bare exact-command passes plus one
full original-context pass do NOT reproduce or explain the21:28Z original test exit1.
The original failing node/trace/stdout/stderr was discarded by the original installer
and is not present in the retained original process artifact. Later cache state and
successful reruns cannot reconstruct it. No evidenced failing defect has been identified
from which to write a valid regression repair. Do not call this repaired or transient.

Concrete blocker: the required original-failure attribution is unavailable, not a
time-based quiescence wait. A timer cannot recover discarded output from an exited
process; additional blind full-suite passes would not close the anomaly. Dependent
installation, installed acceptance, commit and push remain unperformed. No schedule is
kept running. Source/live acceptance evidence and remaining trial quota15POSTs/237422input
are preserved, with total trial spending still15POSTs/412578charged. Overall rollout is
NOT complete; the historical test-failure evidence gap is explicitly unresolved.

### Operator-directed continuation — 2026-11-03T23:07:43Z

After the concrete missing-original-output blocker and five current passing contract
runs were reported, the operator explicitly directed continuing deployment. This is
approval to proceed with that historical attribution gap acknowledged, NOT evidence
that the original exit1 was repaired or expected. The original trace remains unavailable.
All current release gates, frozen inputs, pre/post-test pins/quiescence, full original
test command, fresh rollback backups and remaining live-trial budget still apply.

The resumed Goal's fresh23:08Z precheck `bgp_1a3abdc4b001WLoYn4U6ng2PpN` confirmed
pin-matched quiet. Actual deployment will use a separately TDD-checked thin operator
retainer around the original guard/gate, preserving subprocess results and recording
private original stdout/stderr. Unlike the completed diagnostic, this retainer permits
the unchanged original installer to proceed only after its real gate and checks pass;
it refuses before backup/copy if required evidence cannot be retained. No release
source, installer, policy or default is changed by this operator-only observation.

The retained-apply wrapper had **8 RED cases before implementation**, then **48 GREEN**
combined temporary-path cases (retainer8 + observer17 + existing guard23). Tests exercise
the actual original installer: successful backup/copy/verification after postchecks,
unchanged original test failure and command arguments, capture failure before mutation,
postcheck refusal, and complete restoration after a synthetic entrypoint-copy fault.
All eight frozen release identities were rechecked and still match; no repository code
or original installer was edited for output retention.

Actual retained apply attempt1 `bgp_1a3b01aaf001qupteVnyIe3u87`, diagnostic boundary
`20261103T231310Z`, was refused BEFORE TESTS by one newly active supported client.
The original test subprocess was never called (calls0), and the original installer
returned17. This is an expected busy refusal, not a test failure or partial install.
Root independently confirmed all45oldpayloads and the entire before-backup inventory
unchanged. The attempted timestamp must not be used as a successful backup boundary.
The private output directory is consumed evidence and must not be reused; a later
attempt needs a fresh directory and the original full gate again after a scheduled wait.

### Successful shared installation — 2026-11-03T23:33:25Z

The scheduled23:25Z check found pin-matched quiet. Its recurring wait was cancelled,
then actual retained attempt2 `bgp_1a3bb7a40001BdfE26v8mZ89ol` invoked the unchanged
original installer and full default gate. Successful boundary:
**APPLY_NOT_BEFORE=20261103T232535Z**. Both pre- and post-test pin/quiescence checks passed.
The original test subprocess returned0: **3261 passed /2 skipped in446.29s**. Complete
private stdout/stderr were retained; observation and installer error fields were null.
The original CLI returned0, completed its all-before-write backups, helper-first/
entrypoint-last replacement, and normal per-member verification. No rollback was needed.

The independent final-manifest verifier then confirmed **45 installed payloads and
45 fresh before-image backups**, using full SHA/size, a new basename outside the before
inventory, and timestamp233325Z >= successful boundary. Verified source bundle:
`43d9aaf3bf558189900f60fed3e3924b9657e221e32e075084d7a234e1a4538f`.
Verified before bundle:
`18ea72d7e2e65d7d4d506aa6b6c274cc557d49efe6f7ce1a4d3b333f20311dd4`.
Rollback entrypoint retained:
`C:/Users/operator/.claude/bin/delegate.py.backup-20261103T233325Z-79565a1bc533`.
All helpers passed the original compilation checks; the actual installed entrypoint
also compiled in memory and reported harness version6. No private engine copy was used.

The before-only effect checker is now obsolete for success-state verification and
must NOT be used as though the old installation were expected. Reuse the final
installed/backup verifier with the successful232535Z boundary instead. Installed live
acceptance and publication are still pending; the historical opaque failed-test run
remains unattributed, explicitly disclosed and operator-approved for continuation.

### Installed acceptance complete — pre-publication checkpoint

Both fresh installed cases used the actual shared entrypoint, the original policy
root and ordinary model defaults, with explicit opt-in read_files. Each read AGENTS
first, delivered both random-sentinel fixtures completely, paired all results and
finished with exact sentinel proofs. No provider or prompt bodies are copied here.
Exact-receipt readiness and terminal watchers verified both launch identities:

- Claude Chat/medium: launch `802db829-4033-44ed-8996-1e7e172a0ce9`, engine38448,
  case prefix `read-files-installed-claude-802db829`. Canary ok=true/engine_exit0;
  3 POSTs, 3 reads/3 delivered, normal finish/pairing/proof true. Engine-main elapsed
  12.552292s. Gross inputs per POST:8705/10064/10464 tokens. Normalized cache
  read/write pairs:0/8701,8701/1361,10062/398 tokens (measured zeros, not missing).
- GPT Responses/high: launch `3e93b5ae-3867-44c3-b25c-6d92027ad5d1`, engine37296,
  case prefix `read-files-installed-gpt-3e93b5ae`. Canary ok=true/engine_exit0;
  3 POSTs, 3 reads/3 delivered, normal finish/pairing/proof true. Engine-main elapsed
  15.982691s. Gross inputs:4879/5728/6045 tokens. Normalized cache read/write
  pairs:0/4876,4876/849,5725/317 tokens. No fallback, retries or unknown outcomes.

Selected first-POST timing samples, all seconds from the urlopen call: Claude response
headers0.553261, first complete consumed SSE JSON event1.881616, first consumed nonempty
assistant-text delta1.881616; GPT headers1.060403/event1.063104/text4.033640. Event/header
time is not TTFT. Whole-case time is monotonic elapsed around engine main, not provider
latency alone. Original complete per-POST metadata remains in each private result.
Observed wire forms remained Chat flat medium/max_completion_tokens32768 and Responses
nested high/max_output_tokens32000; these are observed normal fields, not new defaults
or evidence of Claude cap enforcement or a measured maximum context window.

Independent ledger validation after both cases: **21 POSTs /582123 projected and charged,
excess0, reported_known21, reported_missing0**. Remaining:9POSTs/67877input. The same
original ledger/budget identity is retained; no reset/refund or cache discount occurred.
All source/installed acceptance is complete. No additional paid trial is necessary.
Tiny samples support the observed request-count difference (4 baseline versus3 batch
with bootstrap), not a causal/statistical speedup, cross-agent cache guarantee or billing.
Actual dollar spend remains unknown. Full source/JavaScript evidence remains unchanged
at4916/17skips and91tests, with all eight reviewed release inputs frozen.

This is the pre-publication evidence checkpoint: normal intended-file audit, master
commit/push and independent remote/clean-tree/installed-pin verification still follow.
Their actual commit identity and final status belong in the terminal Goal report, not
an invented self-referential commit SHA in this pre-commit document.

Post-acceptance integrity recheck again verified all45installed payloads and all45fresh
rollback backups against the same final manifest/boundary. All8source/test identities
still match their frozen values, and git diff --check passed. Existing Git CRLF advisory
warnings were not treated as test errors and no configuration or line-ending policy was
changed. Only the intended11release paths remain eligible for staging; all private run,
budget, fixture, manifest and operator-controller artifacts stay outside the commit.

Pre-stage audit: local master and remote master both point to45c8c0c. Status contains
exactly the approved11paths; the complete runtime diff matches the reviewed opt-in
reader/context-accounting changes. Credential-pattern checks found only an explicitly
synthetic test marker used by privacy tests, not a real credential. No extra policy,
configuration, installer, runtime state or other-project change is included. Recent
commit style is conventional engine/docs messages; normal direct-master publication
remains the approved path, without a PR, branch, amend, force or version/default change.

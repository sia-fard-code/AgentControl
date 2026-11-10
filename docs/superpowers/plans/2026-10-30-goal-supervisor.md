# Goal Supervisor Implementation Plan

> **For agentic workers:** Use the executing-plans and test-driven-development workflows task by task. Delegation uses the installed harness, not built-in subagents. Do not commit changes; the operator approved implementation, not Git publication or shared deployment.

**Goal:** Implement the approved goal supervisor contract with a real bounded orchestration loop and isolated executable coverage of T01-T25.

**Architecture:** A pure stdlib contract/journal/state/evidence core authorizes effects through a narrow installed-harness adapter and recoverable Windows process containment. An opt-in source CLI drives tool-less planning, capability-limited worker batches, owned-candidate integration and trusted acceptance. Source and synthetic integration tests can advance now; live dispatch requires a separately approved deployment of the new engine protocol.

**Tech Stack:** Python >=3.10, stdlib (including ctypes/msvcrt on Windows), existing psutil only where an existing helper requires it, pytest with temporary paths. No framework, new dependency, database, service endpoint or change to dashboard defaults.

## Global constraints

- Read `docs/superpowers/specs/2026-10-30-goal-supervisor-contract-design.md`; approved 2026-10-30 11:18 UTC.
- Hard ceilings: `max_replans = 2`, `max_total_attempts = 6`, `max_concurrent_workers = 2`, `max_planner_calls = 3`, `max_final_report_calls = 1`, `stop_grace_seconds = 60`.
- Unknown values remain null; UNKNOWN retains reservations/occupancy; failed attempts and lost calls remain consumed; resumption resets nothing.
- No prompts/model bodies/tool output/patches/findings prose in central journals, logs, APIs, source reports or non-synthetic fixtures. Run-owned content is separate and bounded.
- Operator approval/envelope and protected validator bindings are not planner output. No permission/acceptance enlargement through a model.
- Preserve canonical `-C`, old index schema readers and append-only accounting. Relation metadata must not double count usage.
- Preserve pre-existing `AGENTS.md` and `docs/rtk-grep.md` changes. No historical accounting rewrites, commits, deployment, migration, production proof run, listener restart, scheduled task, Projecta/Projectb changes or policy widening.
- Every runtime feature is test-first. Watch RED for the missing behavior, implement the minimum, then watch GREEN. Tests use synthetic text, fake model transports, temporary Git repositories and explicit temporary accounting/config paths. No live network/model calls in tests.
- Installed advisory/executor runs use fresh UUID/receipt and globally unique transcript basename, `--skill ponytail`, exact readiness and completion watcher. Claude executor replies use one tool and writes/replacements under 120 lines.

## File map and interfaces

| File | Responsibility / exported interface |
|---|---|
| `engine/supervisor_contract.py` | Strict envelope/proposal/observation metadata schemas, canonical serialization/digests, owned-reference validation. Export `ContractError`, `GoalEnvelope`, `load_envelope`, `validate_proposal`. |
| `engine/supervisor_journal.py` | OS-held exclusive owner epoch, framed/versioned append-only fsynced transactions, replay, bounded reads and metadata allowlisting. Export `Journal`, `JournalError`. |
| `engine/supervisor_state.py` | Durable admissions, tasks/attempts, evidence watermark, usage/reservations, batch/cap decisions, stopping. Export `Supervisor`, whose effects are returned as typed metadata, never executed internally. |
| `engine/supervisor_evidence.py` | Owned artifact binding/collection, canonical materialized candidate identity, safe Git patch composition, protected validator execution and acceptance facts. Export `candidate_identity`, `collect_result`, `integrate`, `evaluate_acceptance`. |
| `engine/supervisor_process.py` | Windows named Job containment, exact birth identity and bounded identity-safe cancellation/reopen. Export `WindowsJob`, `ProcessError`; unsupported/uncertain containment fails closed. |
| `engine/supervisor_harness.py` | Installed engine protocol preflight, planner/worker command construction, narrowed capabilities, structured launch/result observations and bounded run-local content. Export `HarnessAdapter`, `HarnessError`. |
| `engine/supervise.py` | `build_parser`, `main`, `run_goal`; opt-in preflight/run/resume and metadata-only status with explicit operator envelope/digest and separate state/artifact directories. No dashboard integration. |
| `engine/delegate.py` plus a focused helper if needed | Opt-in machine-readable supervisor protocol, enforced capability narrowing, and exclusive safe worktree creation. Existing non-supervised defaults remain compatible. |
| `scripts/deploy_engine.py`, `tests/test_deploy_engine.py` | Package the complete optional supervisor source bundle atomically with rollback and its test gate. Tests apply only to synthetic temporary destinations; shared deployment remains unapproved. |
| `tests/test_supervisor_contract.py`, `test_supervisor_journal.py`, `test_supervisor_state.py` | Pure core, durable recovery, limits and ownership tests. |
| `tests/test_supervisor_evidence.py`, `test_supervisor_process.py`, `test_supervisor_harness.py` | Real temporary Git/Windows synthetic process cases and engine protocol integrations with fake model transport. |
| `tests/test_supervisor_traces.py`, `test_supervisor_cli.py` | Driver-level T01-T25, hard crash/restart, independent ceilings, privacy canaries and opt-in entry-point end-to-end coverage. |

Data interfaces must remain focused: immutable approved envelope, strict planner proposal containing bounded acyclic task IDs/references, metadata observations from trusted adapters, and decisions such as plan/launch/wait/evaluate/stop. If a listed signature needs a small adjustment, update this plan with the actual interface before a dependent task uses it. Do not widen scope or replace real execution with an event-script interpreter.

## Task 1: Contract, durable ownership and replay

**Files:** Create `supervisor_contract.py`, `supervisor_journal.py` and their focused tests.

**Consumes:** The approved envelope/content boundary and explicit caller-supplied temporary directories/clock.

**Produces:** Strict `GoalEnvelope`, proposal validator, exclusive `Journal` context manager, durable atomic metadata transactions and replay.

- [x] Write RED cases for invalid/flexible limits, extra content fields, malformed/duplicate JSON keys, non-finite counters, missing approval digest, root/policy/validator mismatch, escaping owned paths, cyclic task dependencies, already-owned state and corrupt/torn journal.
- [x] Run `python -m pytest -q tests/test_supervisor_contract.py tests/test_supervisor_journal.py`; confirm missing-behavior failures, not leaked production paths.
- [x] Implement strict records and an OS-lock-held, epoch/sequence-checked fsynced transaction journal. Serialize only schema-allowlisted primitives; persist the original envelope digest. Replay must reconstruct counters without silently dropping a corrupt admission or accepting a new envelope.
- [x] Add a real competing-owner subprocess test, stale-handle test and hard-crash replay test using temporary paths. Assert that releasing/restarting never resets counters or permits two owners.
- [x] Run both focused files GREEN. No commit.

Example behavior oracle (adapt constructor spelling to the final focused API, not the semantics):

```python
with Journal(tmp_path / "state", envelope) as journal:
    journal.transact("admit_worker", {"attempt_id": "a1", "launch_id": launch_uuid})
with Journal(tmp_path / "state", envelope) as resumed:
    assert resumed.replay().attempts_used == 1
```

## Task 2: Deterministic supervisor state and progress

**Files:** Create `supervisor_state.py`, `tests/test_supervisor_state.py`; extend contract/journal tests when required by real callers.

**Consumes:** Approved `GoalEnvelope` and owner-checked `Journal` transactions. No subprocess or model transport in the reducer.

**Produces:** `Supervisor` admissions/observations/decisions, with task plan revisions, usage/reservations and protected terminal transitions.

- [x] Write RED cases for the seventh worker admission, third replan/fourth planning call, third occupied worker, second report, stale epoch/plan response, null usage, double-counted lineage, and restarted counters.
- [x] Add RED progress cases: duplicate evidence, whole-snapshot watermark consumption, response-lost replan, no-progress while another batch worker/descendant is active, acceptance before ceiling rejection, and replan-limit/attempt-limit/no-progress precedence.
- [x] Run `python -m pytest -q tests/test_supervisor_state.py` and observe the missing-behavior failure.
- [x] Implement admissions and evidence consumption in one durable transaction before effects. Only trusted observation schemas can advance state; planner fields cannot fabricate verification/acceptance or approval. Unknown/active survivors retain occupancy.
- [x] Implement durable STOPPING start/deadline, fixed 60-second grace, no late success, no report/model admission after stopping/deadline, and partial/blocked/cancelled distinctions. Add crash-at-t+50 evidence that the deadline stays t+60.
- [x] Run state/core tests GREEN. No commit.

```python
assert supervisor.decide().kind == "wait"  # one batch member is still active
# After the entire batch is quiescent and acceptance is unmet, repeated
# previously supplied evidence yields a fixed no-progress stop, not a call.
assert finished_batch.decide().reason == "no_progress"
```

## Task 3: Bound artifacts, composition and trusted acceptance

**Files:** Create `supervisor_evidence.py`, `tests/test_supervisor_evidence.py`.

**Consumes:** Run-local artifact references, trusted envelope/validator registry, specific canonical baseline and collected attempt metadata.

**Produces:** Run/patch-bound collection, canonical candidate manifests, metadata-only evidence transitions, owned integrated candidate and trusted acceptance facts.

- [x] Write RED tests using temporary Git repositories for same final bytes from different patch serialization, no-op edits, A-B-A identity, conflicting patches, modified candidates after verification and untracked additions/deletions/modes.
- [x] Write RED tests for missing/torn/wrong-run completion metadata, escaped links/references, oversized findings, reworded/unregistered hypotheses and arbitrary output strings pretending to be failure codes.
- [x] Write RED validator-tampering tests: candidate-owned tests made unconditional pass cannot satisfy the protected approved assertions. Include manual criterion pending and protected validator config mutation.
- [x] Run `python -m pytest -q tests/test_supervisor_evidence.py` RED.
- [x] Implement safe owned materialization without touching the operator's main tree. Bind source patches and the resulting full candidate identity; execute only approved commands with bounded timeouts and protected validator identities. Reuse existing finish-gate/run-binding structures where they genuinely fit.
- [x] Implement bounded transient findings and canonical evidence based on materialized content/approved predicates/trusted failure classes. Do not hash arbitrary prose to establish novelty.
- [x] Run evidence/core suites GREEN. No commit.

```python
assert candidate_identity(tree_a) == candidate_identity(tree_b)
assert acceptance_for_worker_modified_assertions.accepted is False
assert acceptance_for_final_candidate.candidate_sha256 == candidate_identity(final_tree)
```

## Task 4: Recoverable Windows containment and narrow engine protocol

**Files:** Create `supervisor_process.py`, process/protocol tests; make narrowly scoped opt-in changes to `delegate.py` (and focused helper if needed).

**Consumes:** Durable admission/identity, exact approved capabilities and temporary source-harness fixtures. Do not deploy or invoke a source worker as a live fallback.

**Produces:** Protocol-v1 feature probe and enforcement, non-destructive worktree creation under supervisor mode, recoverable Windows owned-descendant containment.

The standalone `supervisor_process.py` and its dedicated tests have no core/journal imports and may be implemented concurrently with Tasks 1-3 on disjoint files. Engine protocol work still waits for the contract interfaces; no overlapping executor writes are allowed.

- [x] Write RED engine tests for a capability probe that neither loads gateway configuration nor registers fake usage, attempted capability widening, denied MCP mutation, out-of-scope writes, and existing worktree/branch collisions preserved byte-for-byte.
- [x] Run protocol tests RED using `tests/test_engine_reasoning.py`'s fake transport and explicit temporary HOME/index/config.
- [x] Add opt-in `--supervisor-capabilities` machine-readable probe and a strictly validated supervisor capability input. Reject absent/mismatched policy digest, widened commands/MCP/write paths, incompatible flags or mutated capabilities before model dispatch. Do not change `HARNESS_VERSION` or `INDEX_SCHEMA` unnecessarily.
- [x] Existing non-supervised behavior remains unchanged. Supervisor worktree creation must never delete/reuse a pre-existing directory/branch; avoid check-then-destructive-create races. Enforce the approved worker boundary inside Runner/MCP/command dispatch, not just in model instructions.
- [x] Write RED real Windows cases: suspended child assigned before execution; child/grandchild remains after engine exit; Job reopen after owner crash; cancellation and unrelated-process survival; job assignment/reopen/identity failures hold UNKNOWN. Use synthetic local subprocesses, no network.
- [x] Implement nonce-named non-breakaway Windows Job ownership with retained kernel identities. Confirm emptiness through the kernel, not a sampled descendant list. Do not automatically kill on supervisor handle closure if that would defeat safe live reattachment; uncertain recovery must remain UNKNOWN.
- [x] Run focused process/engine tests GREEN on Windows, then existing launch/verification/reasoning regressions. Unsupported platform containment must explicitly fail closed, not pass skipped checks as proof. No commit or deployment.

## Task 5: Installed harness adapter and actual orchestration entry point

**Files:** Create `supervisor_harness.py`, `supervise.py`, corresponding tests.

**Consumes:** Actual implemented core/evidence/process interfaces; installed protocol-v1 engine; explicit operator envelope and separate owned state/artifact directories.

**Produces:** `HarnessAdapter` structured preflight/dispatch/observe methods and a real `run_goal` loop with `preflight`, `run`, `resume` source commands.

- [x] Write RED tests that an older installed engine fails preflight before planner admission, index writes or model calls; a missing safe containment feature also fails closed. There is no source-worker bypass CLI switch.
- [x] Write RED command/proposal tests: unique transcript basenames/launch UUIDs, canonical root, tool-less bounded planner, existing required ponytail/native worker instructions, narrowed authority, exact structured receipt/index/PID-birth binding, no accidental `--write` MCP grant.
- [x] Implement explicit local operator approval/digest confirmation, not a worker-supplied approval field. Keep all credentials/response bodies out of diagnostics and central metadata. Record lineage separately without changing existing consumers.
- [x] Implement the driver: persist/admit initial planner, validate bounded DAG proposal, execute dependency waves, collect/prove quiescence, compose candidate, run protected acceptance, decide bounded replan or stop, and local closeout. Resume first reconciles durable intents; it never reruns an ambiguous side effect.
- [x] Test synthetic planner results through the real driver and temporary Git artifacts, not only a reducer with manually fabricated success. T02 must actually consume E1, invoke the fake planner once more, launch a correction and reach integrated acceptance.
- [x] Run `python -m pytest -q tests/test_supervisor_harness.py tests/test_supervisor_cli.py` GREEN, and `python engine/supervise.py --help` without any live state.

## Task 6: All trace oracles, crash coverage and independent review

**Files:** Create `tests/test_supervisor_traces.py`; extend focused tests. Update the approved spec only with implementation status/limits, not raw reviewer prose.

- [x] Enumerate T01-T25 explicitly, with a real core/journal/driver and synthetic transport, asserting exact outcomes/counters/bindings/forbidden calls. Do not write a test that merely checks an expected string against itself.
- [x] Hard-crash local supervisor fixture processes around intent/spawn/READY/collection/STOPPING, reopen owner/job identities and prove no duplicated dispatch. Cover independent attempt cap, stale owner/results, missing completion and whole-snapshot consumption.
- [x] Inject a synthetic privacy canary and check every new central persistence/diagnostic/status sink; also test arbitrary forbidden metadata keys and exception text. Run-local fallback is not central persistence.
- [x] Run `python -m pytest -q tests/test_supervisor_contract.py tests/test_supervisor_journal.py tests/test_supervisor_state.py tests/test_supervisor_evidence.py tests/test_supervisor_process.py tests/test_supervisor_harness.py tests/test_supervisor_traces.py tests/test_supervisor_cli.py` and inspect actual results.
- [x] Independently review implementation versus all approved clauses, especially capabilities, validator protection, quiescence and unknown recovery. Fix demonstrated defects test-first. Do not call simulation coverage or a permanently unavailable real loop a complete orchestrator.
- [x] Run `python -m pytest -q` (baseline: 2046 passed, 15 skipped) and relevant JS/lifecycle tests if touched. Run `git diff --check` and check untracked new source files too. Preserve unrelated changes.
- [x] Report implemented capability, executable trace/process evidence, measured checks/skips, installed-engine prerequisite/deployment status and honest residual risks. Do not deploy or start a production goal to prove completion.

## Acceptance / handoff

The design and implementation are operator approved; this plan executes in this session with bounded installed-harness workers and coordinator review. Check off steps only after evidence exists. Any unavoidable missing implementation is partial, not "done." A fully tested adapter refusing today's unsupported installed engine is an honest activation prerequisite, not permission to deploy it.

## Verified implementation checkpoints

- Core contract/journal/state: 138 tests passed after independent integration repairs, including durable artifact-layout/launch/process binding, ownership effect guards, strict control-character rejection, A-B-A selection without fresh novelty, empty acceptance invalidation and fixed trusted failure classes. The concrete state API is `Supervisor(state_dir, envelope, clock, artifact_dir=...)`, `owner_current(epoch=None)`, `effect_identity(effect_id)`, `record_process_identity(effect_id, launch_id, pid, birth)`, `plan_view()`, `select_candidate(candidate)`, `report_done(call_id, usage, success)` plus the admission/observation methods described in the executor handoff.
- Source protocol/default engine regressions: 124 tests passed at the wiring checkpoint; additional planner-write refusal tests then passed with the state suite (102 total). Source changes are not installed or activated.
- Optional atomic packaging/rollback: 59 temporary-destination tests passed. No shared destination was modified.
- Windows backend: seven real local synthetic-process tests passed, including owner crash/reopen and unrelated-process survival. A lost Job identity remains UNKNOWN; detached-descendant recovery is not certified by sampling.
- The early component checkpoints above preceded the finished driver and are retained as history, not the final result.

### Final source verification

- `python -m pytest -q --tb=short`: **2410 passed, 16 skipped**. Fifteen skips are existing operator-opt-in browser/runtime checks; the additional skip is the POSIX executable-mode test on Windows.
- `node --test tests/test_kilo_lifecycle.mjs tests/js/dashboard.test.mjs tests/js/panel.test.mjs`: **82 passed**.
- T01-T25: **26 executable cases passed**, spanning driver, evidence, kernel and core boundaries rather than 25 live-provider runs. Four additional controller subprocess tests hard-exit at intent, READY, collected and stopping cut points and verify exact recovery/counters without duplicate dispatch.
- Fresh-context control-flow review identified four lifecycle/authority defects. Failing-then-passing regressions now cover pre-final optional report quiescence, objective-read failure cleanup, collection-to-integration owner/deadline fencing, and remaining-grace limits on worker and validator cancellation. Normal CLI closeout stays local.
- Shared installation, production listener, live provider goals, migration and historical accounting were not changed. No commit or push was made. The installed engine remains a separately gated activation prerequisite; unsupported installations fail preflight before admission.

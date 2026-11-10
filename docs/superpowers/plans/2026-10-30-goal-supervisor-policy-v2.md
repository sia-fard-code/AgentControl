# Goal-Driven Supervisor Policy Version 2 Implementation Plan

> **For agentic workers:** Use executing-plans and test-driven-development. Delegate through the installed harness, not built-in subagents. No Git commits or deployment are authorized. The operator approved this policy revision at 2026-10-30 16:46 UTC.

**Goal:** New goals continue without default lifetime worker/planner/replan quotas, have eight-worker capacity configurable higher, and use only explicitly chosen usage/deadline limits.

**Architecture:** Add strict envelope-version-selected semantics around the existing supervisor. Keep version 1 unchanged; version 2 uses nullable quotas, diagnostic replanning, request-bounded 64-task proposals and append-only segmented persistence. Existing ownership, unknown-effect and protected verification boundaries remain mandatory.

**Tech stack:** Python >=3.10 stdlib, existing Windows Job backend, existing pytest/Node tests. No new dependencies, database, service endpoint or model transport.

## Global constraints and compatibility

- Read section 10 of `docs/superpowers/specs/2026-10-30-goal-supervisor-contract-design.md` and the relevant existing implementation before changing behavior.
- Retain version 1's exact fixed limits, finite required budget/deadline, stop semantics, digest and existing synthetic fixture defaults. Never silently upgrade old authority or rewrite historical data.
- New `prepare` defaults: schema version 2; lifetime attempt/replan/planner caps null; capacity 8; deadline and max_usage null. Optional report cap 1 and cleanup grace 60 stay fixed. Explicit caps/usage/deadline are strictly validated and frozen.
- Success, unknown effects, ownership loss and permission/manual blockers retain precedence. Diagnostic continuation does not create evidence or grant replay permission. Every invocation/admission remains counted before effects.
- Native worker models and tool-less GPT planning retain current profiles and single-request planner enforcement. No policy widening, source-worker fallback, real model/network test dispatch or production activation.
- Keep raw content only in owned artifacts; central journals/status contain allowlisted metadata. Preserve canonical project identity, append-only history and null versus measured zero.
- Preserve all prior supervisor implementation and unrelated `AGENTS.md`/`docs/rtk-grep.md` changes. No commit/push, shared deployment, live migration, listener, scheduled task or Projecta/Projectb modifications.
- Baseline is 2410 Python tests passed, 16 skipped; 82 Node tests passed. All new tests inject temporary paths and synthetic inputs/processes.

## Task 1: Versioned contract and request bounds

**Files:** Modify `engine/supervisor_contract.py`; extend `tests/test_supervisor_contract.py`; add `tests/supervisor_v2_helpers.py` for synthetic v2 approvals without changing v1 helper defaults.

**Produces:** `GoalEnvelope.schema_version` defaulting to 1 for direct legacy construction; strict version 2 loader; frozen `DEFAULT_LIMITS_V2`; `proposal_task_limit(envelope)`; nullable-policy helpers `limit_reached(envelope,key,used)`, `deadline_passed(envelope,now)`, `usage_available(envelope,charged,reserved,reserve)`. `strict_json` adds an explicit bounded `max_bytes` keyword while retaining its 64KiB default. Proposal version/shape remains 1, constraints selected by the approved envelope version.

- [x] Write/run RED cases for v2 defaults, null and explicit limits, eight/sixteen-task DAGs, >2 revision, optional deadlines/usage, booleans/NaN/zero/unknown-field errors, immutable mappings, and unchanged v1 boundaries.
- [x] Implement schema-version-selected defaults/validation and 64-task/128KiB v2 proposal bounds. Positive concurrency remains explicit, not a silent fallback to two. Validate finite caps when supplied; zero replans is valid, worker/planner ceilings positive.
- [x] Keep current v1 fixture bytes and tests unchanged; ensure altered approvals fail the original SHA check. Publish exact helper signatures for dependent tasks.
- [x] Run contract and relevant v1 regressions GREEN. No commit.

Example actual behavior oracle:

```python
env = load_envelope(v2_raw, approved_sha, project_root)
assert env.schema_version == 2
assert env.limits["max_total_attempts"] is None
assert env.limits["max_concurrent_workers"] == 8
assert not limit_reached(env, "max_replans", 20)
assert usage_available(env, 1000, 500, 100)  # no ceiling, accounting still retained
```

## Task 2: Append-only segmented journals without a disguised lifetime quota

**Files:** Modify `engine/supervisor_journal.py`; extend `tests/test_supervisor_journal.py` or a focused `tests/test_supervisor_journal_v2.py`.

**Consumes:** `getattr(envelope,"schema_version",1)` and the contract bounded-JSON keyword. May run independently of Task 1 with source tests parameterized once the real v2 loader is available.

**Produces:** Same `Journal` constructor, `Record` and `.records` list API; v1 exact paths/limits unchanged; v2 contiguous 16MiB `journal.log` then numbered segment files with no lifetime segment limit. V2 record bound 256KiB; global hashes/sequence/epoch/clock continuous. Journal history is never rewritten/deleted. Following review, a strict atomically published commit-head witness anchors acknowledged sequence/hash/segment/offset so complete suffix loss cannot silently erase admissions; this small metadata head is not historical accounting and is not a migration.

- [x] Write/run RED tests past the old total-byte ceiling with small injected segment thresholds, then actual >16MiB history; restart must recover all admissions/counters. Keep v1 cutoff test.
- [x] Replay bounded lines incrementally, reject oversized/torn records, missing/reordered segments and hash/epoch regressions. A single empty newest segment after interrupted rotation is allowed, not a reset or dropped record.
- [x] Rotate by exclusive creation under the existing owner lock; fsync publication and file before acknowledgment, including initial log/state directory publication. Guard the held lock and actual active tail inode/size/tail selection; replacement, in-place truncation or concurrent tail creation holds. Publish the commit witness before acknowledgment; missing/corrupt/unsatisfied witnesses hold. Durable unacknowledged extra records after a valid older witness are conservatively consumed on replay, never refunded.
- [x] Test hard-exit/rotation boundaries, active-file replacement, short write/fsync failure and unchanged old bytes. Retain committed admissions on replay after a bookkeeping failure.
- [x] Run legacy journal and v2 persistence tests GREEN. Document real RAM/disk/replay growth rather than claiming physically infinite storage. No commit.

```python
with Journal(state, env_v2, schemas, clock) as journal:
    for i in range(100):
        journal.transact("admit", synthetic_metadata(i))
with Journal(state, env_v2, schemas, clock) as recovered:
    assert len([r for r in recovered.records if r.kind == "admit"]) == 100
```

## Task 3: Version-selected core and runtime integration

**Files:** Modify `engine/supervisor_state.py`, `engine/supervisor_harness.py`, `engine/supervise.py`; extend focused tests and add `tests/test_supervisor_v2.py`. Update deployment test gate only for new test paths, never apply it.

**Consumes:** Task 1 helpers and Task 2 journal. **Produces:** A real goal loop supporting optional limits/deadline/budget and eight/sixteen-way waves, not just permissive JSON.

- [x] Write/run RED core admissions beyond six and replans beyond two; include version-selected replay checks, evaluation-group counts and full task metadata for eight/sixteen tasks.
- [x] Replace every live/replay arithmetic comparison with the versioned helpers; schema-select v2 call admission reason (`initial`, `progress`, `diagnostic`, `closeout`) without modifying v1 record shape. A no-novelty v2 round consumes the same watermark and a fresh counted call, not fake evidence. Preserve explicit finite caps and all unknown-state holds.
- [x] Make `deadline=None` safe before arithmetic/comparisons across open/report/decide/stop timestamps, adapter launch pre/post callback, integration, step/idle/exception handling and CLI. No deadline means stop timestamp now and fixed60s cleanup, not JSON infinity.
- [x] Make `max_usage=None` disable only the ceiling, not measurement/reservation/null accounting. Keep positive per-call reservation and actual reported lower bounds; explicit caps still gate admissions.
- [x] Remove adapter's hardcoded six-task wrapper check and metadata schema bound. Keep finite request/record/task-graph bounds adequate for eight/sixteen tasks. Distinguish source request safety from lifetime quotas.
- [x] Update new CLI `prepare` to emit v2/null defaults and capacity8; expose `--max-total-attempts`, `--max-replans`, `--max-planner-calls`, `--max-concurrent-workers`, optional `--deadline-seconds` and `--max-usage`. Explicit overrides are prepared/approved, never applied to resumed authority.
- [x] Prompt diagnostic rounds honestly; retain bounded latest findings/provenance. Find the latest successful/committed batch rather than assuming every prior planner revision produced a batch. Do not lose useful selected patches after an invalid/lost plan revision.
- [x] Add real driver tests with synthetic transport/barriers proving peak8 and16, >6 workers, >2 replans then protected verified success, no-novelty diagnosis then useful correction, and safe context-limited worker continuation. Unknown effects still block; exhausted context alone does not terminate the goal.
- [x] Test explicit finite quotas, zero replans, budget/deadline expiry, report freeze, owner loss, cancellation grace, serialization/recovery, approval immutability and privacy. Retain all original v1 trace oracles as legacy regressions.

```python
result = run_goal(env_v2, adapter=barrier_transport, **temporary_inputs)
assert result["outcome"] == "SUCCEEDED"
assert barrier_transport.peak_workers == 8
assert result["counters"]["attempts"] > 6
```

## Task 4: Review, regression and operator documentation

**Files:** Update `README.md`, approved spec implementation status and this plan. No new central content reports.

- [x] Independently inspect changed authority/replay/stop boundaries; use one bounded tool-less review if helpful, evaluate actual findings and fix demonstrated defects test-first.
- [x] Run all focused v2 and original supervisor/trace/crash tests, then `python -m pytest -q --tb=short` and `node --test tests/test_kilo_lifecycle.mjs tests/js/dashboard.test.mjs tests/js/panel.test.mjs`.
- [x] Check tracked and new files for whitespace; preserve unrelated work and legacy data. No commits or deploy.
- [x] Document new defaults, optional controls, technical per-request/context/storage limits, legacy semantics, genuine blockers and indefinite-spend risk. Report actual peak concurrency/continuation evidence, review scope, regression results and unactivated deployment status.

## Approved handoff

Execute in this session without another policy approval gate: the user approved the revision. Installed-harness workers receive focused disjoint write sets; the coordinator verifies actual effects and integrates. Do not ask questions during the active goal. A partial executor closeout is partial, not completion. The source remains uncommitted and production activation separately gated.

## Final verification and limits

- Complete Python regression: **2566 passed, 16 expected skips**; Node lifecycle/dashboard/panel: **82 passed**.
- Actual scheduler peak8 and configured peak16 passed through synthetic provider transport with real state/Git/validators. A separate Windows test held8actual synthetic worker roots concurrently in owned Jobs and then collected them. No live provider goal was run.
- A protected goal completed after10workeradmissions/4replans; stalled/diagnostic and context-limited continuation cases passed. Explicit quota/budget limits and the original v1 trace/crash checks remain enforced.
- Segmented journals passed19MiB/restart and fault tests. Review defects were fixed with executable counterexamples: uncommitted checkpoint acceptance, persistent-error cleanup loops, lost acknowledged suffixes, in-place truncation/live-witness rollback and initial directory publication. A commit-head witness replaced the earlier no-manifest implementation choice; journal history stays append-only and no live state was migrated.
- Defaults have no call/replan/deadline/usage ceiling. Per-request64tasks/128KiB, individual context/command bounds, cooperative snapshots, per-record/segment bounds and real RAM/disk remain finite. Unknown effects and missing authority still hold; local trusted code is not a filesystem/network sandbox.
- No shared deployment, service change, production proof, historical accounting rewrite, commit or push. Earlier supervisor work and unrelated `AGENTS.md`/`docs/rtk-grep.md` changes were preserved.

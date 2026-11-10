# Goal Supervisor Contract and Trace Table

Date: 2026-10-30

Status: Operator approved this revised contract and implementation on 2026-10-30 at 11:18 UTC. Shared deployment and production activation are not approved.

Policy revision approved at 16:46 UTC: **new goals use the goal-driven version 2
policy in section 10**. The fixed lifetime ceilings, required budgets and automatic
no-progress stop in sections 4-7 describe legacy version 1 approvals and remain
unchanged for their histories. They are not the defaults for new goals.

Baseline: `8dc33f8`; stdlib supervisor around the installed harness, not a replacement worker engine.

## 1. Scope and authority

Define one goal-owning supervisor, one level of workers, and bounded adaptive delegation. This is an approved development extension beyond Release 1's monitoring/accounting scope. The operator's approval authorizes implementation and isolated temporary-path testing after an implementation plan is written. It does not authorize dependency installation, production dispatch, live migration, shared deployment, commits, or a service restart.

The planner proposes; deterministic supervision authorizes and reconciles; `delegate.py` executes workers. Worker findings, reviewer opinions, planner output, and framework callbacks never grant operator authority. Execution must not depend on the dashboard remaining open.

An operator-approved envelope must bind `orchestration_id`, canonical policy-owning project root, goal/acceptance-specification digests, baseline commit, permitted models/efforts, write/command/MCP capabilities, process cancellation authority, and finite limits. Workers inherit an equal or narrower capability set. Any enlargement or acceptance-criterion change requires a new approval, never a planner edit or an automatic counter reset.

The acceptance contract also binds trusted validator implementation/configuration digests, check IDs, failure-classification rules and allowed validator changes. Authoritative assertions run from operator-approved definitions outside worker write authority; commands importing worker-modified assertions do not establish authoritative acceptance merely because they pass. Candidate-owned tests may provide supplementary evidence but cannot redefine the approved validators. Any required validator change needs renewed operator approval and invalidates earlier acceptance evidence.

The first design uses stdlib records and explicit transitions. `pydantic-graph` and LangGraph remain alternatives evaluated against the same traces. Dependency count alone is not a safety argument; neither a graph checkpointer nor a small state machine provides exactly-once external effects.

## 2. Records and content boundary

| Record | Required metadata and meaning |
|---|---|
| `GoalEnvelope` | Identity, policy/approval and acceptance digests, baseline identity, limits, deadline, permitted capabilities, cancellation policy. The supervisor obtains approval from the trusted operator path, not a worker-supplied token. |
| `TaskLedger` | Stable task IDs, dependency IDs, objective and acceptance references/digests, current plan revision, bound worker-attempt IDs. Plan/fact prose stays in permitted run-local artifacts. |
| `WorkerAttempt` | Task ID, unique attempt/launch IDs, receipt reference, run ID when registered, attempt-owned worktree/artifact references, baseline identity, owner epoch, lifecycle, terminal outcome, result/patch/evidence bindings, usage observations and reservations. Unknown values remain `null`. |
| `ProgressLedger` | Plan revision, durable planner-call/replan/attempt counters, evidence watermark, consumed evidence keys, accepted-criterion IDs, unresolved-attempt IDs, stop reason and original stopping timestamps/deadline. Model judgments of progress are advisory only. |
| `EvidenceTransition` | Kind, canonical evidence key, originating run/check IDs, acceptance/hypothesis ID, candidate/baseline digests, prior/new observed states, source-artifact digest/reference, and durable sequence. |

Central state, relation journals, checkpoints, diagnostics, and framework tracing contain only allowlisted metadata, never briefs, conversation text, tool output, patches, findings prose, credentials, or complete sidecars. References must resolve only within the relevant owned artifact directory; reject escaping links, arbitrary paths, malformed schemas, stale bindings, and changed digests.

Bounded findings may be read transiently from permitted run-owned artifacts for planning, with provenance and explicit untrusted-data treatment. This is not permission to copy them into central state. `completion.json` supplies verification metadata, not substantive task findings; it is best-effort and may be absent or incomplete. A planner one-shot need not have worker completion metadata.

For this design exercise, each planner request admits at most 24,000 characters of task findings, at most 4,000 per attempt, excluding instructions and fixed schemas. Reject excess material or select a deterministic provenance-preserving subset; never silently truncate a required acceptance fact or repeatedly reload content to evade the cap. Existing engine request-budget checks also apply.

## 3. Durable ownership and launch protocol

1. Acquire an exclusive per-orchestration OS-held owner lock and validate process birth identity. All journal appends and dispatch actions require the current owner epoch. No timestamp-only lease takeover is allowed. If exclusive ownership or old-owner death cannot be established, hold for reconciliation; never start a competing supervisor.
2. Before any worker spawn, durably append and flush one admission record that consumes an attempt slot and reserves capacity, binding task/attempt/launch IDs, unique receipt/worktree/artifact paths, baseline and owner epoch. Failure to persist means no spawn. Consumed slots are never refunded or reset after crashes, rejection, cancellation, failure, or resume.
3. Spawn only the installed harness with the canonical `-C` root and the recorded launch UUID/receipt. Never use task labels as process identity. The engine derives `run_id` from the transcript basename: every planner, worker and final-report transcript must therefore have a globally unique basename, not a repeated name such as `run.md` in different directories. A conflicting run ID holds admission; it must not merge accounting identities. Worktree labels include unique attempt identity; existing same-name paths cause a hold, not reuse or removal. The current destructive same-label `make_worktree()` behavior is not safe for this adapter without an ownership-safe creation boundary.
4. Bind readiness to the exact receipt, matching registration, engine PID and process birth identity. Use the exact-identity watcher, not label-only output. Preserve READY, terminal lifecycle, and evidence collection as different facts; watcher text is not the future supervisor protocol.
5. On restart, reconcile every durable intent before admitting new work. A registered live run is reattached, not respawned. A terminal run is collected and validated, not declared accepted automatically. A missing receipt or a dead PID without sufficient outcome evidence becomes `UNKNOWN`, not "never launched."
6. The intent-to-spawn crash window is deliberately fail-closed: without a durable not-spawned proof or sufficient launch/effect reconciliation, do not replay. Terminal failure can still have effects. An attempt may be replaced only after explicit safe-to-retry evidence, remaining limits, and the existing approval permit it. The replacement consumes a fresh slot and uses fresh identities.

Use append-only metadata to relate parent, planner, worker and final-report runs. Preserve old index readers: do not bump `INDEX_SCHEMA` as a shortcut; a separately versioned relation journal can carry lineage without changing existing records. Usage totals count each actual model call once; a parent rollup is a view, not another charge.

## 4. State transitions and evidence

Goal states: `APPROVED -> PLANNING -> EXECUTING -> EVALUATING`, with at most two `EVALUATING -> PLANNING` replan transitions. Uncertain effects/identity enter `RECONCILIATION_REQUIRED`. Stop decisions enter `STOPPING`; terminal goal outcomes are `SUCCEEDED`, `PARTIAL`, `BLOCKED`, or `CANCELLED`, with a fixed reason and unresolved child/survivor counts. Reconciliation is not acceptance or permission to dispatch.

Attempt lifecycle is `ADMITTED -> SPAWN_PENDING -> RUNNING -> TERMINAL -> COLLECTED`; any ambiguous step can enter `UNKNOWN`. Keep terminal outcome, worker verification, reviewer opinion, goal-criterion acceptance and integrated-candidate verification in separate fields. `COLLECTED` means validated evidence was obtained, not successful completion of the goal.

Every transition names its required authoritative observation. Re-reading an observation is idempotent. Journal corruption, missing durable counters, an unbound result, or conflicting ownership prevents dispatch and success. Recovery reconstructs counters from admitted events; it never guesses lower values. A lost model response consumes its already admitted planner call and cannot be invisibly retried.

An unknown attempt blocks new dispatch, success, and unsafe cleanup until resolved. Evidence-safe read-only reconciliation may continue within the deadline and the bounded stopping interval in section 6. Lack of resolution yields `BLOCKED` with unresolved effects recorded; it does not certify that children stopped.

One plan revision defines one finite batch, possibly executed in waves at occupancy two. Evaluate acceptance/non-progress and request adaptive planning only at its barrier: all batch tasks are collected or explicitly skipped under that plan, all launched workers and owned descendants are proven quiescent, and no outcome is unknown. One unhelpful result while another worker is active remains `EXECUTING`, not `PARTIAL/no_progress`. Cancellation, revoked approval, ownership failure and the deadline interrupt the batch through the stopping protocol instead of waiting indefinitely for the barrier.

## 5. Deterministic non-progress rule

The initial envelope fixes these ceilings; planners cannot edit them:

| Limit | Definition |
|---|---|
| `max_replans = 2` | At most two admitted adaptive planning calls after the initial plan. An invalid, failed, or response-lost call still consumes its slot. No separate uncounted "repair", "reflection", "retry", or renamed planning call is permitted. |
| `max_total_attempts = 6` | At most six durable worker/integrator agent launch admissions, including rejected, failed and replacement attempts. Planner and final-report model calls have separate caps below, but all usage rolls up to the goal. |
| `max_concurrent_workers = 2` | Admitted attempts with unproven worker/owned-descendant quiescence, including `UNKNOWN`, retain occupancy; a timeout or engine exit alone does not release a slot. |
| `max_planner_calls = 3` | One initial plan plus at most two replans. Each is tool-less and separately bound/journaled. There is no model-call retry outside this cap. |
| `max_final_report_calls = 1` | Optional tool-less closeout after acceptance but before terminal success, with no dispatch authority and reserved usage allowance. An unresolved report prevents success and counts during stopping. Terminal outcomes admit no new model calls. A deterministic local partial report is the fallback. |

The envelope also requires a finite wall-clock deadline, finite per-worker turn limit, bounded designated verification commands, and finite verification timeouts. Acceptance runs at most once per candidate for each initial/replanned batch (three batches maximum); integration and verification do not create an uncounted repair loop. Transport and worker-level repair/review/advisor calls stay inside their worker ceilings and goal usage accounting.

Before admitting a replan, the supervisor must record at least one **new, relevant, unconsumed** `EvidenceTransition` after the previous planner's evidence watermark:

- **Changed candidate:** new materialized candidate content, against the approved baseline and allowed paths, linked to an unmet criterion. Its identity is the digest of a versioned, canonically sorted manifest of normalized relative paths, entry types/modes and exact file-byte digests; only approved entry types are allowed, and link escapes are rejected. A patch hash binds transport bytes but is not a novelty key. Different patch serialization producing the same materialized tree, no-op writes, timestamp changes, artifact filenames and A-to-B-to-A oscillation do not qualify.
- **Falsified hypothesis:** an already registered hypothesis/predicate changes from unresolved to falsified by a bound executed check. Version 1 permits only operator-approved hypothesis IDs bound to fixed validator/predicate/configuration digests in the acceptance registry; plans select IDs rather than invent or normalize free-form predicates. Registration precedes the producing attempt. Planner assertions, rewording, and prose hashes cannot establish a new hypothesis or its falsification.
- **New failing check:** a preauthorized acceptance check produces a previously unseen normalized failure against a bound candidate. The key uses approved validator/check identity and version, materialized candidate identity and an allowlisted failure code emitted by the trusted classifier. Timestamps, run labels and arbitrary output text cannot manufacture novelty. Repeating the same failure against the same candidate does not qualify.

The supervisor validates provenance, schema, baseline/candidate binding and the fixed criterion linkage. A model does not decide whether an event is novel. Novelty is permission to consider replanning, **not proof of useful progress or semantic correctness**.

Replan admission atomically consumes the triggering evidence keys, advances the watermark to the supplied evidence snapshot, increments the durable replan/planner counters, and binds the request to that plan revision. Consumption occurs before the model call and survives invalid output or a lost response. Previously supplied observations cannot fuel later replans merely because they were not selected as the trigger. A stale response cannot alter a newer plan or dispatch work.

When caps leave already admitted work in flight, allow that batch to reach its barrier within the original deadline, without new admissions beyond those caps. At the barrier, evaluate existing acceptance evidence first. If criteria remain unmet and more work is required, choose the reason deterministically: exhausted replan capacity -> `PARTIAL/replan_limit`; otherwise exhausted attempt capacity -> `PARTIAL/attempt_limit`; otherwise no qualifying transition -> `PARTIAL/no_progress`; otherwise admit the bounded replan. There is no extra planner call or renamed task on a stop branch. A valid sixth attempt may still establish success; the limit forbids a seventh, not collection of the sixth. T19 exercises the attempt ceiling independently.

At each decision boundary, cancellation/revoked approval, unresolved effects, and invalid ownership take precedence over success or dispatch. Otherwise evaluate existing acceptance evidence before deciding whether more work is permitted. The deadline stops new goal work and all model admissions; only bounded safety controls/reconciliation in section 6 remain permitted. Success requires its acceptance decision to have been durably recorded before that deadline. Resume, model changes, new task names, changed labels, and new process IDs never reset limits or evidence history.

## 6. Completion, reservations and cancellation

For code-writing goals, workers use unique owned worktrees. Default staged edits remain `staged_unverified_on_disk`; there is no `--stage` flag and no path from staged checks to `verified_by_command`. A worktree is not a security sandbox and does not isolate a shared MCP-connected application.

Integration uses a separately owned candidate based on the approved baseline, with a manifest binding the exact worker patches and resulting candidate digest. No automatic merge into the operator's main tree is allowed. Conflicts yield unmet acceptance or a bound failing-check event; they are not silently resolved by cleanup. Changes after verification invalidate that evidence.

`SUCCEEDED` requires all operator-defined criteria to be satisfied by their trusted, approved validators on the exact current integrated candidate, required checks passing after its final mutation, and no unknown attempt, unresolved conflict, pending cancellation, surviving worker or surviving owned descendant, whether accounted for or not. Engine exit is insufficient: prove attempt quiescence using the approved containment/ownership mechanism; where detached descendants or lost ownership make that proof unavailable, retain uncertainty and occupancy instead of certifying success. An explicitly manual criterion requires operator evidence; a reviewer pass or planner statement never substitutes. For non-code goals, criteria identify the relevant artifact/check binding instead of pretending a code-patch check establishes acceptance.

Worker completion files are schema/digest/run-bound inputs. Their absence or corruption cannot be treated as zero changes or success; use authoritative reconciliation or hold. A terminal run marked complete is a lifecycle fact, not goal acceptance. Bounded task findings are necessary in addition to completion metadata.

Admission reserves attempt, concurrency, time and estimated usage capacity before work starts, including planner calls and closeout. Unknown measured usage stays `null` and retains the unresolved reservation; it is never charged as zero or silently refunded. Hard launch/concurrency limits are enforceable. Provider token/dollar ceilings are estimates unless the transport supplies enforceable limits; report that distinction rather than claiming a hard monetary guarantee.

Every stop reason uses one protocol: durably enter `STOPPING` with its original start time and fixed stopping deadline, freeze dispatch and model admissions, then drain/collect, perform identity-safe read-only reconciliation and (only if expressly authorized) cancel within `stop_grace_seconds = 60`. Resume never resets or extends this interval; an unresolvable time/counter record fails closed. This interval is a cleanup allowance, not an extension of goal work or the success deadline. Once stopping begins, do not promote the goal to success. Lost supervisor ownership permits no controls by that stale owner.

Cancel only within explicitly approved supervisor authority, after exact process identity checks; record supervisor-specific reason, intent, action result and observed exit/survivors. Existing `operator_kill` semantics cannot be reused unchanged. A journal failure does not justify an unverified PID kill; record uncertainty where possible. Partial termination preserves unresolved children/occupancy. No automatic artifact/worktree deletion occurs while ownership, outcome or surviving processes are uncertain.

After the stopping interval, quiescent and fully reconciled cancellation is `CANCELLED`; an unmet but otherwise reconciled goal is `PARTIAL` with its no-progress/cap/deadline reason. Any survivor or unknown outcome is `BLOCKED`, with `cancel_incomplete` for cancellation or `unresolved_effects` otherwise. These are bounded supervisor outcomes, not claims that unresolved OS processes stopped. After deadline, use a deterministic local closeout; admit no final-report model call. A lost final-report response consumes its one slot and retains unknown usage/reservation; fallback prose stays run-local, with only fixed metadata/reason codes centrally.

## 7. Fixed two-worker trace exercise

Synthetic goal only: criteria `C1` and `C2` require distinct worker changes; integration criterion `CI` requires both changes to coexist and pass designated checks on candidate `K`. `W1` and `W2` are the initial independent tasks. `P0` is initial planning, `R1/R2` are replans; `A1..A6` are worker admissions; `E1..` are validated evidence transitions. All observations and result bodies in future fixtures must be synthetic. These are **specified expected traces, not executed tests**.

| ID | Fixed trace/input | Required transition/outcome and rejection oracle |
|---|---|---|
| T01 | `P0; A1(W1), A2(W2); both collected; integrate K; C1/C2/CI pass` | `SUCCEEDED`, attempts 2, replans 0, no active/unknown child. Bind every check to K; isolated worker passes alone are insufficient. |
| T02 | Initial integrated check newly fails `CI` with `E1`; `R1(E1)` proposes one bounded correction; `A3` produces K2; criteria pass | One useful adaptive replan reaches `SUCCEEDED`; attempts 3, replans 1. Planner cannot widen paths, checks or capabilities. |
| T03 | Two workers finish and pass local checks; CI fails once (`E1`); R1 consumes E1; replacements produce no new patch/hypothesis/check state | `PARTIAL/no_progress`, no R2 and no extra launch after evaluation. Rewording the task or re-reading E1 does not qualify. |
| T04 | E1 permits R1; new relevant E2 permits R2; A1..A6 complete but goal remains unmet; E3 then arrives | `PARTIAL/replan_limit`; no fourth planner call and no A7. Fresh hashes cannot extend the cap. If A6 already satisfies all criteria, evaluate it and succeed instead. |
| T05 | Duplicate E1; random labels/timestamps/output strings; two patches serialize differently but produce the same tree; no-op patch; return to an earlier candidate; reworded hypothesis | No new evidence key; no replan admitted. Candidate-content manifests and approved hypothesis IDs define identity; free-form predicates are rejected. |
| T06 | Crash after intent but before durable readiness; receipt absent, effect unknown | Attempt slot remains consumed; `UNKNOWN -> RECONCILIATION_REQUIRED`; no replacement spawn. Evidence of a genuinely not-spawned attempt can resolve it, but never refunds its slot. |
| T07 | Crash after exact readiness while engine is live; restart supervisor | Reattach exact launch and retain reservations; no duplicate worker. Old supervisor cannot append or dispatch after owner replacement. |
| T08 | Terminal lifecycle recorded; crash before collection; completion metadata missing or torn | Revalidate run/patch/check bindings or hold; no fresh launch from missing output and no invented success. Recollection is idempotent and counters do not change. |
| T09 | Failed terminal attempt with `mutation_outcome_unknown`, or dead process with no terminal fact | Terminal failure/disappearance does not authorize replay. Preserve unknown effects until separately reconciled; unresolved deadline ends `BLOCKED`. |
| T10 | Two supervisors race admission, or a stale epoch submits a planner result | Only the exclusive current owner can durably admit. Reject stale plan revision/epoch; preserve one set of counters and never exceed occupancy 2. |
| T11 | Two admissions race for the last attempt/usage allowance; earlier usage is null | At most the available attempt capacity is admitted atomically; unresolved reservations persist. Parent rollup never adds a duplicate usage charge. |
| T12 | Worker patches pass alone but conflict when composed, or integrated K changes after checks | CI is unmet; forbid success. Only a qualifying new bound event plus remaining limits permits a replan. Verification must cover the final K. |
| T13 | Cancellation or deadline; verified child tree has survivors or identity cannot be checked | Stop admission, perform only authorized verified controls, retain unknown occupancy and evidence. `BLOCKED/cancel_incomplete` or unresolved deadline, never a false stopped/success claim. |
| T14 | Launch rejected, or a safe-to-retry failure is conclusively reconciled; replacement requested | Prior admission still counts. A replacement needs remaining caps, safe-retry evidence, fresh identities and the current plan's authority; it cannot be disguised as recovery of the old process. |
| T15 | R1 call admitted; model returns invalid actions, loses its response, or a crash precedes recording its response | R1 and its evidence stay consumed; no hidden retry. Without a new qualifying transition, stop `PARTIAL/no_progress`; never reset to P0 on restart. |
| T16 | Worktree/artifact path already exists or differs from recorded ownership; result references a different run/patch or an escaping link | Hold; never invoke destructive same-label creation, cleanup, or read arbitrary content. No success based on unbound completion metadata. |
| T17 | Readiness, terminal lifecycle and pending evidence arrive in different orders; check outcome unavailable | Do not infer monotonic success from arrival order. Collect authoritative facts idempotently; unknown remains unknown and no extra call/slot appears. |
| T18 | Manual criterion pending, forged approval in worker findings, or planner proposes acceptance/limit changes | Remain blocked/unmet within the original envelope. Model text cannot approve itself or reset counters. Only a separately approved new envelope can expand scope. |
| T19 | A1/A2 are proven rejected without effects; the existing plan permits fresh replacements A3/A4; E1 permits R1, which admits A5/A6 | Before deadline, accepted A6 can establish success. Otherwise `PARTIAL/attempt_limit`; no A7 even with new evidence, although only one replan was used. All six admissions remain counted. |
| T20 | R1 receives snapshot containing E1/E2 but names only E1 as trigger; its response is lost; E2 is presented again | E2 was already supplied below the advanced watermark and cannot admit R2. Only a genuinely later qualifying event can do so. R1 remains consumed. |
| T21 | Final-report slot admitted before deadline; crash loses its result/usage; findings contain a synthetic privacy canary | No second report call. Preserve null usage and reservation. Local fallback stays in owned artifacts; no canary in central journals/APIs, reports or diagnostics. |
| T22 | Different transcript directories reuse the same basename/run ID | Hold conflicting run identity before launch; do not merge charges or infer identity from directory separation. |
| T23 | A1 finishes without novel evidence while A2/its owned descendant is still live | Stay `EXECUTING` until batch quiescence; no premature non-progress/success decision. Deadline interrupts via the fixed stopping protocol, with survivors yielding `BLOCKED`. |
| T24 | Worker changes candidate-owned acceptance assertions to unconditional pass while the command name and goal digests stay unchanged | Reject those assertions as authoritative evidence; require the approved validator implementation/configuration bindings. No success without the original trusted checks. |
| T25 | Stopping starts at t with deadline t+60; supervisor restarts at t+50 with a survivor | Retain t+60, not t+110. Reconcile/control only within the remaining authorized interval; unresolved survivor yields `BLOCKED` without a new planning/report call. |

Paper-review pass condition: every trace has an evidence-backed transition or an explicit bounded hold/stop. Reject any interpretation allowing unapproved actions, unreconciled replay, destructive cross-attempt cleanup, forbidden content persistence, excess calls/attempts, or acceptance without candidate-bound criteria. Both T02 (useful adaptation) and T03/T04 (bounded non-progress) are required; a safe launcher alone does not satisfy this contract.

After design approval, an executable temporary-path fault-injection spike must exercise these same traces. Compare total adapter/recovery/privacy/approval complexity for stdlib, `pydantic-graph`, and LangGraph; no framework wins merely by replacing a fixed number of features. A paper pass or model opinion is not operational crash-safety proof.

## 8. Review and implementation gate

One fresh-context, tool-less `gpt-6.1-sol -e max` review is requested for the draft and traces. It reviews supplied evidence, not the live repository, and grants no authority. The earlier council used the same model family: this is not a cross-model review. Review prose stays in the review run's permitted temporary artifacts, not in central state or repository reports; record only disposition and contract changes here.

The operator subsequently approved the revised design and implementation, including the temporary-path fault-injection spike, on 2026-10-30 at 11:18 UTC. Write and read an implementation plan before runtime changes. Dependency additions, shared deployment, historical accounting rewrites and production activation remain separately gated.

Contract-review disposition, before implementation: the single requested review completed with `NEEDS_REVISION`. The approved revision tightened descendant quiescence, trusted-validator authority, canonical novelty and the common stopping boundary, and added independent attempt-cap, whole-snapshot consumption and lost-closeout/privacy oracles. A local audit also made globally unique transcript/run identity explicit. At that design stage, no executable traces had run; implementation evidence follows below.

## 9. Source implementation and activation boundary

The source now contains a real planner/worker/acceptance loop and the opt-in
`engine/supervise.py` prepare/preflight/run/resume entry point. Core metadata is split
across focused contract, journal, state, evidence, Windows process, capability-policy
and installed-harness adapter modules. The operator's main tree is never automatically
merged. Runtime model profiles are fixed; MCP is disabled and normal closeout is local.

Integration and verification groups are durably admitted before subprocess effects,
with the candidate hash bound after materialization and quiescent batch facts committed
before acceptance. An uncommitted or uncertain group cannot be replayed to invent a
check result. Candidate snapshots use byte/entry/cooperative time limits; incomplete
snapshots leave verification unconfirmed. Reported usage lower bounds retain unknown
aggregate usage and its reservation rather than treating it as zero.

All 25 trace IDs have executable coverage (26 tests, including both T04 cap variants).
The tests span the actual driver with synthetic transport and real temporary Git and
protected validators, real synthetic Windows Job processes, and the core/journal.
They are not 25 live-provider executions. Focused implementation review identified
report finalization, objective-read cleanup, collection-to-integration fencing and
remaining-grace cancellation gaps; each was addressed with failing-then-passing
regression tests. The review was advisory and limited to the supplied control modules.

Shared-engine deployment, production activation, live provider proof runs and
historical accounting changes remain unapproved and were not performed. The source
deploy manifest packages the complete optional supervisor bundle atomically with a
test gate and rollback; synthetic deployment tests use only temporary destinations.

Final verification after the review fixes: the complete Python suite passed with
**2410 passed, 16 skipped**, and the dashboard/panel/lifecycle Node suite passed
**82 tests**. The explicit trace suite passed all 26 cases covering T01-T25, and four
additional hard-exit controller tests exercised intent, READY, collection and stopping
recovery. Existing operator-opt-in live/runtime checks were not enabled. No live
provider goal was used as proof, and no shared installation was activated.

## 10. Approved goal-driven policy, envelope version 2

The operator approved removing the arbitrary lifetime worker/planner/replan quotas,
with capacity eight concurrent workers by default and operator-selected higher
capacity. Goal success still requires the same protected, candidate-bound evidence.
The supervisor continues investigation, diagnosis, strategy changes and fresh worker
contexts until verified success, operator stop, an explicitly chosen limit or a real
permission/identity/effect blocker. It does not stop merely because two replans or six
admissions have elapsed. Capacity is not a requirement to launch eight unnecessary tasks.

Version 2 retains the strict envelope field names, explicit approval SHA and frozen
capability/acceptance bindings. It has these policy defaults:

| Field | New-goal default / meaning |
|---|---|
| `limits.max_total_attempts` | `null`: no lifetime admission quota. All admissions remain durably counted. |
| `limits.max_replans` | `null`: no lifetime replan quota. Explicit finite values, including zero replans, are honored. |
| `limits.max_planner_calls` | `null`: no lifetime planning-invocation quota. There is no hidden generation retry. |
| `limits.max_concurrent_workers` | `8`: finite, positive, explicitly configurable above eight. Unknown/unproven workers retain occupancy. |
| `deadline` | `null`: no goal wall-clock deadline unless chosen by the operator. A supplied timestamp is absolute and is not rebased on resume. |
| `max_usage` | `null`: no aggregate admission-budget ceiling unless chosen by the operator. Usage/reservations still remain visible and unknown remains null. |
| `limits.max_final_report_calls` | `1`: optional pre-final report only, no new call after a terminal outcome. Normal CLI closeout remains local. |
| `limits.stop_grace_seconds` | `60`: bounded cleanup after any stop, even without a goal deadline. |

An explicit limit is authority, not a hint: planners cannot raise it. Version 1 bytes,
validation, limits and replay retain their original meaning. The loader never upgrades
an existing approval or changes its digest; deployment and live state migration remain
separately gated.

No-novelty becomes a **diagnostic replan reason**, not fabricated progress and not an
automatic terminal failure in version 2. Each initial/progress/diagnostic planner
admission is counted and recorded before the call, binds a fresh revision/identity,
and durably consumes the entire evidence watermark even if it did not advance. The
prompt must describe the stalled facts honestly and request a distinct investigation
or approach. Diagnostic continuation cannot authorize replay of unknown effects,
invent acceptance, forge manual permission or relaunch the same OS attempt.

Per-worker context/turn/request bounds, per-command timeouts, findings bounds,
snapshot limits and bounded request parsing remain technical execution boundaries.
Workers can checkpoint and hand back partial results; the parent may safely continue
with fresh identities and contexts rather than terminating the whole goal. A proposal
has a technical maximum of 64 tasks and 128 KiB metadata; this is not a lifetime goal
quota. The adapter and journal metadata must support at least 8 and 16 planned tasks,
not retain a hidden six-task restriction. Very large pools still depend on useful
independent work and request/resource limits rather than a promise to fill capacity.

Long-running version 2 journals use append-only bounded segments without a segment
count or total-history byte ceiling. Global sequence/hash/epoch/time continuity and
active-file ownership fencing span rotation; missing segments, torn records, changed
identity and uncertain publication hold. Existing logs are neither rewritten nor
deleted. Replay reads bounded records incrementally. Retained metadata still consumes
real memory, disk and replay time: actual resource exhaustion fails closed rather than
pretending storage is infinite. A strict atomically published commit-head witness
anchors the acknowledged tail to detect complete suffix/segment loss; journal history
itself remains append-only. Missing or inconsistent witnesses hold, and durable but
unacknowledged extra admissions are consumed rather than refunded. No database,
framework, coordinated whole-directory rollback guarantee or automatic migration is introduced.

Required executable evidence adds: legacy compatibility; null/default and explicit
limits; actual eight-way and configured sixteen-way scheduling; more than six worker
admissions and more than two replans ending in verified success; unchanged-watermark
diagnosis followed by useful work; safe context continuation; segmentation/restart
beyond the former 16 MiB ceiling and rotation fault cases; optional deadline/budget
stopping; unchanged permission, unknown-effect, report, privacy and cancellation guards.
All inputs/processes remain synthetic and temporary in tests. Production goals and
shared engine activation are not authorized by this revision approval.

### Version 2 implementation verification

The revised source is implemented. New `prepare` approvals are version 2 with null
lifetime quotas, capacity 8, and null deadline/usage ceiling unless explicitly chosen.
Version 1 validation, limits and approved history remain unchanged. No state migration,
historical accounting rewrite, shared deployment or live provider goal was performed.

The actual driver passed peak-8 and configured peak-16 scheduler tests, a verified
goal requiring 10 worker admissions and 4 replans, diagnostic continuation without
invented evidence, context-limited safe continuation, explicit limit/budget cases and
an invalid planning revision retaining earlier committed work. Eight real synthetic
Windows worker roots were simultaneously live in owned Jobs, then released and
collected. These are isolated tests, not eight live-provider agents.

Review counterexamples were reproduced and fixed test-first: uncommitted batch files
cannot create acceptance; inconsistent reconciliation cannot bypass bounded cleanup;
commit witnesses detect acknowledged suffix/segment loss; in-place truncation and
changed witnesses revoke effect authority; initial log/state publication is flushed
before acknowledgement. Coordinated rollback of the whole trusted state directory
and its witness is not claimed to be detectable without an external monotonic anchor.

Final full regression: **2566 Python tests passed, 16 expected skips**. The Node
dashboard/panel/lifecycle suite remains part of the verification gate. Fixed per-request,
context, command, snapshot and record bounds remain technical limits; storage and RAM
are not physically infinite. Unbounded policy can continue spending until verification,
a real blocker or operator stop, so explicit controls remain available when desired.

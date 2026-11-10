# Recoverable Worker Execution — Design (resilient-v1)

Status: approved by operator in chat 2026-10-31 17:10 UTC for SOURCE implementation only.
Not approved: deployment, migration, default-profile switch, controlled real-cost trials.

## 0. Scope and invariants

- New behavior lives behind an opt-in execution profile `resilient-v1`. Legacy defaults,
  legacy approvals and their digests keep exact original semantics.
- Existing policy-v2 supervisor stays the only task supervisor. No new supervisor.
- No global quota additions, no switch to prompt-encoded tools, no signed-history rewrite,
  no root/exclusion changes, no new framework dependencies, no weakening of verification.
- Central state/API carries typed safe metadata and digest references only; content stays
  run-local. Historical runs are not re-enacted or reclassified.
- Rollback must not reset caps, reinterpret old approvals or replay effects.

## 1. Bounded tool output (engine/tool_output.py, new)

- `list_dir` and `grep` are bounded and paginated *while generating* output, never by
  building the full result first and truncating after.
- `read_file` honors `start_line`/`end_line`/max-read for pending (unsaved) content exactly
  as for disk content (today the pending branch in `Runner.read_file` returns the whole file).
- One oversized read is recovered as a bounded typed page plus a truncation notice with a
  continuation position, instead of latching context exhaustion.
- Finite caps are preserved: per-request, cumulative read/output, and slice caps
  (`engine/context_guard.py` constants keep their values).
- Real slice exhaustion yields a deterministic handoff (section 3), never a fabricated
  provider-limit claim.
- A command whose output was withheld keeps its actual exit status and effect status.
  Commands are NEVER replayed to recover stdout.

## 2. Native multi-tool batches (Claude) and protocol health

Under `resilient-v1` only (legacy keeps the current batch refusal):
- Before executing anything, validate the complete batch: structural arguments, unique
  non-empty tool-use IDs, and admission bounds (`BATCH` budget). A malformed entry anywhere,
  including the tail, means nothing in the batch executes.
- Per-call policy still applies: root confinement, command allowlist, MCP policy.
- Dispatch is sequential. Every call gets a paired result or an explicit typed skip.
- `finish` in a batch is deferred until all admitted actions settle.
- An action with unknown effect stops dispatch of the later calls (they get explicit skips).
- A valid completed batch is never classified as a gateway timeout or protocol incident.
- Genuine protocol repairs are tracked as local consecutive wire health (reset by a valid,
  known-result action, independently of task-progress classification)
  plus a durable total incident history; the fixed "two lifetime incidents" rule
  (`REPAIR_LIMIT`) no longer applies regardless of intervening useful work.
- A valid no-op is not task progress. Recurring ineffective recovery routes to the existing
  diagnostic strategy, not to infinite same-action loops or hidden lifetime quotas.

## 3. Candidate-bound verification and handoff (engine/recovery_handoff.py, new)

- Schema-2 binding keys are exactly `run_id`, `task_sha256`, `root_identity`,
  `envelope_sha256`, `origin`, `seed_sha256`, `scope_sha256`. Git origin has a real
  `baseline_commit`; artifact origin has only its `initial_sha256`. A validated protocol-2
  boundary and trusted snapshot-identity callback are required before runtime preparation.
  Coordinator debt cannot be cleared by worker checks. Schema-1 defaults stay unchanged.
- Protocol-2 workers use explicit recovery-contract and handoff schema 2. Their tagged
  binding fixes run/task/policy-root/approval, current seed and scope plus a Git-real-commit
  or artifact-initial-hash origin. No artifact path passes a fake commit to v1 logic.
  The frozen validated boundary supplies the descriptor and a trusted identity callback;
  schema-2 points use the canonical snapshot DATA identity, not the legacy flat-tree hash.
  Schema-1 Git contract/record parsing remains strict and unchanged.
- Runtime activation is explicit: `--profile resilient-v1` enables safer tools/protocols;
  `--recovery-contract PATH` also binds durable handoff to an operator-prepared closed
  contract frozen before model dispatch. A profile name or worker-written configuration
  never grants continuation or acceptance authority. Without a binding, completion says
  `unbound`, not a fictitious baseline or checkpoint. Contract data fixes task/run/launch,
  check spec, policy root, actual owned candidate root and transcript artifact directory.
  Accounting and artifacts cannot change the attested candidate: an owned worktree or
  equivalent separately approved candidate boundary is required. Ordinary commands are
  not whole-repository hashed; bounded capture occurs only at verification/checkpoints.
- Coding jobs declare approved acceptance checks: definition, exact argv, verifier owner
  (`worker` or `coordinator`), root identity and current candidate identity.
- Lifecycle is WORK -> VERIFY -> HANDOFF. No blanket verify-only final turn is imposed.
- A late useful edit may be saved unverified with `rerun_owed=true`.
- No model calls beyond the slice cap are made to verify or report.
- The machine handoff record is written deterministically and survives model-report failure.
- When every approved deterministic acceptance check already holds for the current
  candidate, no fresh model prose is required.
- Exit 0 alone, or an unverified `finish`, is not trusted task success. Lifecycle completion
  (`finish_gate.evaluate_finish`) stays separate from task success.
- Check results bind to the check definition digest and the candidate digest. Checks may
  mutate the tree: observed post-check changes invalidate stale evidence. Evidence from a
  later tree Y never certifies an earlier candidate X.

## 4. Durable continuation (engine/continuation.py, new; driven by policy-v2 supervisor)

- Protocol-2 manifests use `agentcontrol.supervisor-manifest/v2`, with the same canonical
  policy root and exact command/write narrowing, plus an approval digest, tagged candidate,
  selected seed/receipt and recovery-contract pin. A per-attempt `run_dir` fixes worker
  `candidate_root` to `run_dir/candidate`; metadata/configuration stay outside that root.
  Workers use an already materialized owned root with explicit `--write`, never recreate
  it with `--worktree`. Readonly planner roles carry no writable/candidate/config paths.
  A separate readonly capabilities-v2 flag preserves the exact original v1 advertisement.
- Schema-3 state uses the existing supervisor/reducer and v2 journal framing. Parent-only
  snapshot records bind current sealed content to seed, receipt, scope and source attempts.
  A collected bounded worker may be `yielded`, not functionally failed or task-complete.
  Successors journal their selected current seed/predecessor before dispatch. Unknown
  effects, missing quiescence, stale seeds and duplicate collection cannot authorize them.
  No-op yields route to diagnostic planning, not hidden lifetime/plateau quotas. Acceptance
  also binds a completed protected evaluation to the current candidate/facts digest;
  worker lifecycle success alone cannot satisfy functional task dependencies.
- Explicit approval schema 3 retains schema-2 common authority and limit fields, replaces
  the top-level Git-only baseline with a tagged `candidate` descriptor, and adds
  `continuation={profile: resilient-v1, admission: approved-task}`. Git descriptors contain
  a real baseline commit; artifact descriptors contain no Git-baseline placeholder.
  Both bind `initial_sha256`, approved project-data `inputs` and finite snapshot `bounds`.
  No unmeasured token/handoff quota is added. Versions 1/2 remain strictly unchanged.
- Finite worker slices (turn/read/output caps per launch) are distinct from task-level
  lifetime limits, which stay `null` (unlimited) unless the operator set them explicitly.
- A continuation record binds: objective, approval, policy and check digests; owned root,
  baseline and current candidate manifest/digests; launch id and PID birth time; last
  mutation, last check, effect uncertainty; ownership; cumulative usage and operator caps;
  next allowed action; confined, digest-bound run-local artifact references.
- Durable intent is written BEFORE dispatch. A crash after an external effect but before
  its receipt is recorded as `UNKNOWN`, never `NOT_RUN`.
- New protocol-2 launches may require optional atomic Job association through
  `PROC_THREAD_ATTRIBUTE_JOB_LIST` while creating the suspended process. Legacy creation
  remains unchanged; unsupported atomic association never falls back silently. Group
  observations use the authenticated named Job's kernel count, not a live-leader guess.
  Missing names/handles and unresolved creation windows remain unknown. This does not
  introduce a Job keeper/service or promise recovery after all identifying handles vanish.
- Before any redispatch or candidate collection the supervisor reconciles surviving
  processes and lineage. Patches are never blindly reapplied.
- Cancellation and explicit limits survive restarts. Unknown usage stays `null` (not 0).
- Legacy approvals keep their exact original semantics and digests.

## 5. Candidate contracts (engine/candidate.py, new)

- New schema-3 task approvals and manifest-2 workers are explicit extensions, never
  reinterpretations of schema-1/2 approvals. New goals retain policy-v2 null lifetime
  quotas and existing technical worker bounds; no guessed token/handoff quota is added.
- A snapshot defines approved project-data inputs explicitly. Operational accounting,
  transcript and state roots are outside that data definition and outside owned workers.
  Snapshot identity includes every defined data entry, binary byte, creation and deletion.
  Git administration is separately protected and bound, not an arbitrary exclusion list.
- A successor materializes the exact sealed CURRENT snapshot, not the original commit
  plus replayed patches. Transport completeness is not worker verification or acceptance.
- Supervisor git/worktree/patch preflight (`supervisor_policy`, `supervisor_harness`) stays strict.
- Two explicitly approved candidate contracts: `git` (legacy worktree + `patch_binding`) and
  `artifact-manifest` (non-git: per-file path/size/sha256 manifest against a baseline).
  Non-git is never silently treated as git; baseline identity is not weakened; roots are
  not widened. Example: Cranes is non-git, Virastyar is git.
- A worktree does not isolate external applications (Word, STAAD, COM). No external-app
  operation and no automatic side-effect retry is authorized by this design.
- New contracts require explicit schema/version negotiation. Manifest `legacy v1` stays strict.
- No permission can come from a model summary, a changed task label or a checkpoint.
- Direct CLI hard caps stay hard. Only independently authorized task continuation may
  launch another slice.

### 5.2 Explicit continuation transport lineage

- Legacy `candidate-receipt-1` bytes and DATA manifests stay unchanged. Explicit
  approval-3/manifest-2 worker and merge captures use `candidate-receipt-2`, adding
  one non-null lowercase SHA256 `context_sha256` to the otherwise closed receipt.
  `None` selects v1 and must not silently accept v2 on reopening.
- The protected parent computes context from exactly `envelope_sha256`,
  `seed_receipt_sha256`, deterministic `source_attempts`, and `kind`
  (`initial|worker|merge`). Full source bindings remain in the existing journal.
  There is no arbitrary nonce, timestamp, owner epoch or acceptance claim.
- Receipt identity hashes actual durable receipt bytes. DATA identity does not
  change. Siblings with equal DATA, singleton merges and repeated equal-DATA seeds
  must not alias distinct journal lineage. Idempotent replay reuses the original
  immutable reference; it does not recapture at a different reference.
- Consumers match the protected raw receipt digest before parsing, reconstruct
  context from its producing journal record, then verify DATA, scope, seed DATA and
  context. Merges authenticate each input's seed RECEIPT as well as seed DATA.
  Context integrity grants no ownership, mutation or acceptance authority.
- Explicit v2 Git workers materialize the current sealed DATA snapshot into a
  separately authenticated, prepared Git worktree. The approved origin commit,
  common directory, pointer and backlink remain bound. They do not fabricate a
  legacy patch/run binding. Artifact candidates never invoke Git.
- Approval-3 permits sequential completed evaluations within one planner revision;
  evaluation IDs remain unique and pending evaluations block another evaluation.
  Current candidate, seed receipt, owner epoch and unused protected facts must all
  bind. Evaluation count is not a planner-call quota. Legacy approvals retain the
  original evaluation restrictions and all actual planner ceilings remain intact.

## 6. Task-level visibility (metadata only)

- Task states: `continuing`, `awaiting-verification`, `reconciliation`, `blocked`,
  `verified-complete`. Projected via `src/agent_control/snapshot.py`/`models.py`/`dashboard.py`
  and `web/dashboard.js`/`panel.js` as typed safe metadata and digest references only.
- Original worker histories and append-only accounting are preserved.
- Task-level success requires trusted acceptance of the current candidate.
- Dashboard input is an explicit trusted startup registration of a fixed
  parent-owned metadata source with expected project and envelope pins. Project
  discovery, worker files and HTTP parameters cannot register sources. Rows are
  last parent observations, not independent proof of current lease or liveness.
  Unconfigured legacy snapshots and accounting remain unchanged.

## 7. Opt-in, release and rollback

- Profiles, flags and envelopes are opt-in and source-only until deployment is approved.
- Release prerequisites: synthetic fault fixtures, full regression suite, independent
  adversarial review. Controlled real-cost trials need separate approval.
- No cost saving may be claimed from historical records or baseline tests.
- Rollback cannot reset caps, reinterpret old approvals or replay effects.

### 7.1 Operator activation approved 2026-11-01

- A closed operator-owned `~/.claude/engine-settings.json` may set
  `{"schema":1,"default_profile":"resilient-v1"}`. No file retains legacy.
  Only new direct agent runs consult it; explicit profile arguments win, one-shot
  behavior stays unchanged, and approved-worker launches without an explicit
  profile remain legacy. The setting grants no goal or application authority.
- A child-only approved-worker environment fence complements the existing
  manifest fence without changing approved argv, payloads or digests.
- A cold `--show-profile` reports effective profile and provenance without prompt,
  gateway, credentials or run-index access. Invalid relevant settings fail closed.
- `serve --task-sources PATH` selects an operator-protected closed registry
  `{"schema":1,"sources":[]}`. Entries contain only `project_id`, `project_root`,
  `envelope_sha256`, `path`; validate bounds, local paths, pins and duplicates.
  An absent flag preserves the legacy snapshot shape; an explicitly empty registry
  enables an empty task view, not discovery, dispatch or snapshot approval.
- Configuration readers reject duplicate/unknown fields, invalid schemas,
  oversized data and link/reparse/hardlink aliases. Operator-protected storage and
  trusted programs remain preconditions, not an OS sandbox against the same user.
- Deploy the complete pinned recovery/activation bundle with rollback verification;
  keep existing workers quiescent during replacement. Activation does not rewrite
  old approvals, accounting or worker histories. Future goals still require their
  separately approved initial snapshots and trusted metadata-source registrations.

## 8. Model-aware context and same-run compaction — 2026-11-01

The operator approved this extension for source implementation in chat. Deployment,
committing and pushing remain separate approvals. This section supersedes section 1's
fixed context byte ceilings only when the new context policy is selected.

### 8.1 Policy and capacity

- Context policy is independent of the tool execution profile. Ordinary direct agent
  and one-shot launches default to `model-v1`; explicit `legacy` retains the original
  byte policy. Frozen approved, supervised and recovery launches retain their original
  policy and digests. Unsupported explicit context overrides fail before gateway access.
- The configured model window for this installation is 1,000,000 tokens. Operators can
  override the window and reply/safety headroom explicitly. Admission uses
  `estimated_input + reply_headroom + safety_headroom <= configured_window`.
- The 1M-window defaults reserve 32,768 reply tokens and 50,000 safety tokens. Smaller
  configured windows scale default headroom; explicit reserves take precedence. A known
  enforced output bound is distinct from an advertised output capability or an estimate.
- Reject booleans as integers, non-finite fractions and impossible allowances. Preserve
  explicit-option provenance. Do not change the closed operator-settings schema or
  widen policy roots, commands, tools or application permissions.
- Active context includes current instructions, task, steers, skills, schemas, results
  and media. Cached input occupies context once. Lifetime billed inputs are not active
  occupancy. Lifetime read/output statistics remain measured, not independent model
  context abort thresholds in `model-v1`.
- Context measurements distinguish inclusive Chat/Responses input totals from native
  Anthropic disjoint input/cache components. A missing or null disjoint component leaves
  the aggregate unknown unless a documented adapter rule establishes its value. A known
  partial sum is not a complete measurement. Historical billing normalization is unchanged.
- Counts are labelled estimates unless a matching tokenizer or provider measurement
  establishes otherwise. Retained measurements bind to the measured packet and route;
  they do not prove future counts under server-side history or model revision drift.
  No vocabulary downloads, guessed native endpoints or silent model changes are allowed.
- Finite output production, pages, valid paired-result slots, memory and artifact quotas
  remain. Verified image/transport restrictions retain their separate semantics and
  failure classification. New token admission must not retain hidden legacy byte caps.
- Before dispatch, reserve a stable run-owned result receipt and reader for every paired
  slot. A guessed 512-byte placeholder does not prove that unknown future outputs fit.
  Fixed receipt-reference envelopes form an actually measured safe packet for the whole
  current batch. Receipts expose recorded outcomes, not approval or verification claims.
- Measure complete candidate packets after each slot selection or deferred-finish
  replacement. Do not add local token deltas or assume prefix counts are monotonic.
  If no richer selection fits, restore the whole measured safe packet for all still-
  revisable slots. Already-sent exchanges remain unchanged until validated compaction.
  Canonical journals retain actual results; projected replacements use slot identity.
- Receipt storage and readers are confined to the owning run and bounded by producer
  resources. A preflight resource failure executes nothing. Later capture faults remain
  explicit and do not falsify action effects or authorize replay. No new filesystem,
  command, application or approval authority follows from a receipt reference.

### 8.2 Compaction transaction

- Automatic compaction is opt-in. Its configurable default trigger is 70% of the model
  window. Trigger earlier if the summarizer, its reply and mandatory retained input need
  more space. One-shot mode rejects compaction to preserve its single-request contract.
- Compact only between complete tool batches. Keep authoritative instructions, task,
  skills and steers, recent complete exchanges, and an engine-owned state capsule.
  Candidate/verification/approval state, unknown effects, artifacts and open anomalies
  remain authoritative outside the model's summary.
- Keep historical receipt discovery in an engine-owned, bounded, paged index, not in
  summary prose. Frozen high-water handles remain stable as new receipts are reserved.
  Include the current discovery handle mechanically in each adopted memory frame.
  Groups with incomplete receipt capture remain retention pins, not new authority.
- Previously adopted descriptive memory remains eligible for later consolidation.
  Track it by engine row/group identity, never by matching a model-written label.
  Repeated compaction must not accumulate permanently uncompactable memory rows.
- Summaries are bounded untrusted descriptive memory, not instructions, approval or
  proof. Mechanical validation checks schema, references, bindings, protocol and size;
  it cannot establish natural-language truth or complete resistance to prompt injection.
- Use the same approved route/model/effort with no tools. Charge attempts and unknown
  usage truthfully as compaction work. Do not reset accounting, turn, repair or execution
  history. No new worker, command replay or durable-successor authority is introduced.
- Preserve opaque/signed dependencies as whole protocol units. Decline unsupported
  history modes rather than assume local truncation removes provider-retained history.
- Validate required pins, full result pairing, meaningful reduction and current state
  again before one atomic active-view assignment. Publish immutable run-local content
  first. Original transcript and message journals remain append-only. Central state and
  APIs contain only allowlisted metadata and references, never summary content.
- Failure, missing usage, stale bindings, cancellation or storage faults retain the
  original active view. Real attempted-call/accounting records still append. Bound
  repeated no-progress attempts. Never discard authoritative pins to force a reduction.

### 8.3 Acceptance

Use temporary synthetic fixtures and small injected windows. Cover default/frozen/
one-shot precedence, Unicode/calibration/media uncertainty, every admission path,
non-monotonic token counts, paired results, preserved authority/evidence/unknown effects,
atomic rollback, malformed or malicious summaries, privacy, wrapper/installer cold gates
and unchanged old approval digests. Full Python/Node regressions and independent review
are required. Tests do not establish exact unseen provider tokenization or gateway limits.

### 8.4 Operator-approved controlled rollout — 2026-11-02

The operator subsequently approved synthetic live canaries, shared-engine deployment,
commit and push, conditional on passing release checks. Automatic compaction remains
opt-in; this approval does not change the default fraction, model window, old approvals
or repository/application permissions.

- First validate development source with isolated run-local artifacts and accounting,
  using the original policy root. Exercise Claude and GPT Chat without compaction and
  GPT Responses with successful same-run compaction followed by continuation. Small
  explicit canary windows/fractions do not change installation defaults or prove a
  provider's full capacity or enforcement of requested output caps.
- Pin the complete installer-selected candidate, verify the final regressions and
  independent review, then install the complete bundle with rollback backups while
  owned release workers are quiescent. Verify installed bytes and repeat the compaction
  smoke test before committing/pushing the intended source. Do not force-push, amend
  history, create an unsolicited pull request or start a production service.
- Keep the historical planner run `20261101-132134-context-policy-plan-1101` explicitly
  unresolved: retained engine diagnostics record startup/request start and observer
  diagnostics record Windows exit 1, but no initiator/owner/pre-kill evidence exists.
  Fresh canaries cannot reconstruct that evidence or establish the old exit's cause.
  This known historical limitation is separate from measured rollout checks; it is not
  classified as a timeout, repaired incident or successful planner completion.
- No production listener/restart, migration, OS scheduling, cancelled-supervisor
  activation, business application action or unrelated global-settings change is
  authorized by this rollout.

### 8.5 Provider compatibility and accounting repair approved 2026-11-02

The operator approved the proposed targeted repair end to end at 10:21:59Z, after
direct provider-boundary experiments disproved uniform capacity/cap assumptions.
The approval covers development-source repairs, bounded synthetic live verification,
independent review, a gated complete-bundle deployment and normal publication. It
does not authorize remote gateway settings edits, application actions, permission
expansion or silent model/route/effort changes. The prior historical attribution gap
remains separate and unresolved.

#### Terminal and accounting contract

- Capture a terminal Responses event's actual usage before classifying its status.
  `response.incomplete` is a known partial outcome, not an unclassified transport cut.
  Preserve its allowlisted reason, received text and actual usage exactly once,
  including reasoning-only/zero-visible-text cases. Unknown remains `null`, not zero.
- A known incomplete outcome is `PARTIAL` with fixed code `response_incomplete` and
  CLI exit 1. Persist an explicit stop reason using existing terminal-status semantics
  rather than reporting a complete answer. Do not replay, retry, fall back to Chat or
  execute native tool calls whose generation/arguments were incomplete.
- Original streamed transcript bytes are append-only, including an eagerly emitted
  heading. Append an explicit partial marker and rely on the failed terminal/stop
  record; do not seek, replace a heading in place or copy the partial answer twice.
- Failed/error events also retain any actual usage they supplied. Repeated cumulative
  snapshots are not separate charges. A stream with no terminal remains unknown
  `stream_incomplete`; do not manufacture a server terminal from a local disconnect.
- Exception conversion to a provider context rejection must retain both its terminal
  usage and earlier paid attempts. Validate terminal object/detail shapes before field
  access, retaining top-level usage when nested objects are malformed.
- A native known partial exports already accepted staged work and local completion
  evidence before exit. It never executes the incomplete response's calls or requests
  a remote wrap-up. Patch preservation is not a claim that unlisted work was verified.
- One-shot retry admission preserves pending earlier usage before the next dispatch,
  including unknown prior entries. A refused next request is not a phantom generation;
  in-stream context conversion attaches a current usage snapshot only when supplied.
- On an observed enforced Chat route, exhaustion of its requested cap may be reported
  conservatively as `PARTIAL/output_cap_reached` even when a gateway labels it `stop`.
  Do not infer provider enforcement from an arbitrary short reply or a local clip.
- Inspect known gateway error carriers before treating them as ordinary answers.
  A missing-usage, error-shaped text carrier cannot prove successful completion or
  authorize replay. Distinguishable provider/context rejections fail known; ambiguous
  prose remains unconfirmed rather than being broadly reinterpreted as authority.

#### Model/route operating profiles and wire bounds

- Select the actual requested model, route and effective effort locally before gateway
  credentials are read. Preserve cold invalid/frozen override rejection and leave pure
  `context_policy.py` resolution free of filesystem/network/settings access.
- Keep a small closed profile record beside existing route selection. Separate its
  configured operating window, observed retained inclusive-input lower bound, declared
  maximum/unknown and evidence scope/provenance. A demonstrated point is not a measured
  maximum, and advertised metadata is not an observed guarantee.
- Claude's tested route reports a 1,000,000-token ceiling and retained 994,948 reported
  input tokens. GPT 6.1 high on both Chat and Responses retained all five synthetic
  markers at 129,755/129,757, 257,754/257,760 and 513,746/513,757 reported input tokens.
  The near-million GPT cases failed. Use a conservative GPT configured operating
  window of 512,000, with the existing reply/safety formulas and requested packet
  admission; do not call this the backend's exact context maximum. Other efforts
  retain explicit evidence-scope labels rather than inheriting an untested guarantee.
- Existing configured or explicit windows for unprofiled routes stay explicitly
  unverified. Explicit operator overrides preserve their provenance and do not turn
  observations into a provider-capacity guarantee. Legacy/frozen policy and payload
  bytes, old approval digests and the closed operator-settings schema remain unchanged.
- Keep compaction opt-in and the default fraction 0.70. Compute valid trigger/target
  and reply/safety headroom against the effective operating policy, not a stale 1M
  assumption. Keep command/output storage limits distinct from model capacity.
- Prototype the known Claude route's `max_tokens` mapping in development source only;
  adopt it only after finite live probes establish actual cutoff. Share field selection
  with preflight, emit only one bound, and reject malformed/nonpositive/over-reserve
  bounds. GPT Chat uses `max_completion_tokens`; Responses uses `max_output_tokens`.
  Unknown mappings stay explicitly unverified, not silently guessed.
- `verified_output_limit` refers to an enforced bound for the request, never an
  advertised maximum. Otherwise retain `null` and evidence/provenance separately.

#### Acceptance and release

- Use synthetic temporary-path regressions for terminal semantics, paid usage,
  no-replay/no-tool execution, semantic error carriers, profile precedence/provenance,
  cap fields, cold/frozen/legacy compatibility and privacy. Central state/APIs retain
  allowlisted metadata/references only, never prompt/response/summary contents.
- The discovery phase is bounded to 12 actual generation POSTs and 2.5M calibrated
  input tokens (including projected rejected input); six GPT envelope probes are
  complete, with no retries and all marker checks passing. Reuse the existing ignored
  probe host; extend only source selection/expected cap mapping, never rewrite wire
  payloads in an observer or use a private engine copy/direct API bypass.
- Reproduce each known failure before its smallest repair. Require independent review,
  full isolated Python/JavaScript regressions and decisive live source acceptance, then
  complete-bundle hash pinning, original installer contract gate, pre/post-test
  quiescence, verified backups and installed smoke before commit/push. No unresolved
  new anomaly is excused by a successful primary test. A genuine backend/approval
  blocker remains explicit rather than being hidden by a settings change.

### 8.6 GPT-first reduced-scope release approved 2026-11-02T13:31:24Z

The operator subsequently approved deployment of the verified GPT work and shared
changes for Claude only where they do not introduce a compatibility problem. This
changes the release gate, not the recorded evidence about Claude output enforcement.

- Release the measured GPT operating profiles, known Responses partial/usage handling,
  append-only reporting and additive metadata-consumer repairs after independent review,
  isolated full regressions and decisive live acceptance. Do not describe this as a
  verified repair of every provider boundary.
- Compatible common admission, storage, history and metadata code may be installed for
  Claude. Keep its existing model IDs, Chat route, effort behavior, configured 1M window,
  cap field behavior and legacy/frozen/pinned wire contract. In particular, do not copy
  GPT's 512,000 policy or Responses wire fields to Claude, and do not substitute models.
- Claude's tested flat cap fields remain unsupported by the available evidence;
  `verified_output_limit` stays `null`. Neither an ignored prototype, local clip nor
  passing ordinary-Claude smoke authorizes an enforcement claim. Unknown models and
  efforts retain their existing configuration with explicit evidence-scope limitations.
- Defer further Claude forwarding repair and error-carrier interpretation that requires
  its private deployment contract. Read-only discovery found deployed version 0.11.4,
  application HTML rather than an OpenAPI document, and HTTP 401 on the selected
  function-source read. Do not bypass that denial or silently change gateway settings.
- Run GPT partial/accounting plus normal same-run continuation acceptance and ordinary
  native Claude compatibility acceptance against source and installed code. Keep all
  synthetic run state/accounting temporary. Bound release live checks to 30 generation
  POSTs / 650,000 reported-or-projected input tokens; review any extension explicitly.
- Preserve the complete-bundle installer gate, pre/post-test quiescence, before-image
  inventory and rollback verification. Publish only intended changes after installed
  acceptance. No business applications, production services, OS tasks, cancelled
  supervisors, permission expansion or unrelated global settings are authorized.
- The private Claude-cap limitation and historical planner-attribution gap are retained
  as documented concerns, not repaired incidents or reasons to claim all providers pass.

## 9. Opt-in fast-dispatch phase 1 — 2026-11-02

Status: approved by the operator through the design-approval question in this session
on 2026-11-02, following the Python fast-dispatch implementation request at 21:33:29Z.
Approval covers source implementation, TDD, independent review and isolated comparisons.
Prior publication approvals do not authorize deploying this new feature, changing the
gateway, starting services or changing global settings. Independent bounded design
consultation: `20261102-223735-fast-dispatch-design-29a0` (completed, tool-less).

Implementation status: source-only acceptance is complete, with the evidence and retained
verification limits recorded in the corresponding plan's Phase 9 acceptance section.
This does not authorize installation or establish live-provider latency/cache gains.

### 9.1 Intent and selected approach

Keep Python and reduce model round trips with one bounded multi-file read tool. Reuse
the upstream prompt caching already reported on the current Claude route; do not claim
the engine owns provider KV storage or guarantees cross-dispatch cache sharing. Prefer
this small request-count reduction to a Rust rewrite or an unmeasured transport change.
Transport pooling, response memoization, arbitrary parallel tools and guessed provider
cache keys, TTLs or explicit Claude cache fields are outside phase 1.

### 9.2 Explicit activation boundary

- Introduce per-dispatch `--read-files`, requiring explicit `--profile resilient-v1`
  and explicit `--context-policy model-v1` for a new ordinary direct agent run.
- Reject the option with one-shot, legacy/frozen context, supervisor manifests,
  approved-worker fences or recovery-bound launches. Phase 1 does not extend approval,
  supervisor, continuation or operator-settings schemas. Reject incompatible options
  before operator settings, gateway credentials, run registration or model access.
- Omission retains the exact existing tool registry/advertisement, Chat/Responses
  request bytes, profile/context defaults and old approval digests. No new default,
  route, model, effort or provider cap behavior is introduced.

### 9.3 Bounded read tool

- Advertise and permit `read_files` only when explicitly enabled. Accept an ordered
  array of 1–8 entries, each with `path` and optional `start_line`/`end_line`, through
  an explicit closed object-array schema. Validate the complete request before reads.
- Read sequentially through the existing confined/paged reader. Preserve policy roots,
  symlink/URI grants, staged-content visibility, ordinary range/error behavior and all
  existing finite producer/resource limits. No commands, writes or MCP batching.
- Bound the complete serialized result, including labels and continuation/status
  metadata, to `MAX_READ_CHARS`. Budget inside the reader, not by clipping a completed
  result afterward. Reserve status space before producing bodies.
- Return ordered per-entry served/error/unserved status and an exact next range when
  partial. Unserved means not executed. A partial or unserved item is not proof of a
  complete read; only actually delivered content may suppress a later duplicate read.
- Do not cache file contents or answers across calls. Executed reads still check current
  permissions and data. Preserve real outcome, paired results, partial/finish semantics
  and no-replay behavior when evidence or output cannot be delivered.

### 9.4 Cache compatibility and truthful acceptance

- Keep instruction authority, history, steers, skill bodies and system construction
  unchanged. Declare the opt-in tool in a deterministic position; identical opted-in
  inputs must serialize identically. Adding a tool can initially invalidate an upstream
  cache prefix and is not proof of a cache-speed improvement.
- Use synthetic temporary-path TDD for malformed tails, schema bounds, root/symlink/URI
  denial, staged reads, mixed errors, exact serialized output limits, continuations and
  suppression state. Retain legacy/frozen/pinned request and approval golden equality.
- Compare equivalent scripted model tasks: two individual reads plus finish require
  three requests; one batched read plus the same finish requires two. Verify equivalent
  delivered evidence, actual request counts, paired results and normal finish. Include
  the bounded eight-entry case and partial/error cases without pretending they finished.
- Reuse existing numeric timing/usage metadata and preserve unknown versus zero. Local
  scripted elapsed time is not a measured provider speedup, cache-hit guarantee or bill.
  Independent review and full isolated Python/JavaScript regressions precede source
  completion. Shared installation, new real-cost trials, commit and push remain gated.

## 10. Approved Phase 9 rollout — 2026-11-03

The operator selected **Approve complete rollout** after requesting the remaining work.
This supersedes section 9's source-only restriction solely for bounded paid source and
installed acceptance, complete-bundle quiescent installation with verified rollback
backups, and normal commit/push of intended release files to `origin/master`.

The paid trial budget is cumulative across source and installed cases, including every
retry/report/fallback generation POST: at most **30 POSTs / 650,000 reported-or-projected
input tokens**. Development/review harness calls are separately logged, not charged to
this trial ledger. This is not a promised monetary limit or a provider token maximum.
No default, model, route, effort, gateway, permission or unrelated application change
is authorized; neither are a new service, migration, OS schedule, force push, amend,
new branch, PR or supervisor activation. The feature remains explicit opt-in.

### 10.1 Trial safety and accounting

- Add only a focused live-canary helper and synthetic temporary-path tests as needed;
  core Phase 9 code is already accepted. Default/import/help must remain no-live.
- Load the actual selected source/installed entrypoint with normal sibling imports;
  use the original policy root and unchanged per-model default windows/fields/streaming.
  No private engine copy, direct API client or wire rewriting is permitted.
- Require actual AGENTS read/delivery before fixture reads, with current AGENTS also
  pinned in the brief. Use random synthetic sentinels never supplied in the prompt;
  normal finish must follow real complete tool-result delivery and valid identity/pairing.
  The versioned `full-file-v2` oracle accepts existing native whole-file-equivalent
  ranges only on those same pinned paths, with start1/full coverage proven before
  execution and exact body/header verification afterward; no permission is broadened.
- A single fresh append-only Temp budget ledger is used throughout. Reserve under an
  exclusive lock and durably append before each generation POST. Missing, corrupt,
  ambiguous, locked or exhausted state refuses before network. No refunds on unknown
  outcomes, errors or retries. Charge confirmed excess monotonically before further calls.
- Admission uses the larger of serialized UTF-8 request bytes plus conservative message
  framing and twice the active engine estimate. This is a conservative **projection**,
  not a claim to bound undocumented upstream-added content. Cache receives no input
  budget discount. Preserve unknown versus measured zero; stop on an actual overrun.
- Smaller live fixtures are permitted to retain budget for mandatory installed checks.
  Compare actual baseline/batch counts; AGENTS bootstrap changes live counts, and the
  source-only scripted 9→2 figure is not assumed. Repeated fresh launches may report
  cache hits but do not prove cache ownership/sharing/TTL or causal latency improvement.
- Record monotonic case/HTTP-header/first-consumed-SSE/first-text-delta intervals with
  exact definitions. Missing values remain null; first byte/event is not labeled TTFT.
  Persist only whitelisted metadata; private original transcripts/sidecars stay in Temp.

### 10.2 Installation and publication safety

Capture a new complete selected payload pin and target before-inventory. Do not reuse
the old `18ea72d7...` manifest. Apply the unchanged original installer and full default
contract gate, augmented only with pre/post-test source/target pin and quiescence checks.
Quiescence inspects bounded process metadata and actual known launcher script arguments,
including wrappers and this canary; it never inspects, kills or steers another project.
Busy means refusal before mutation followed by unchanged-state verification and a
scheduled wait. Verify every fresh existing-member backup, every installed payload,
version/compilation and installed acceptance before publication. Respect automatic
restore on failure; inspect effects before any retry.

Publish only intended source/tests/docs/helper code, never runtime artifacts, fixtures,
private sidecars or manifests. Use a normal commit/push on existing master after scope,
secret and whitespace audits. Verify remote HEAD, clean checkout and installed pin.

### 10.3 Verified installation and acceptance — 2026-11-03

The complete 45-member bundle was installed through the unchanged original full gate,
with source/target pins and quiescence verified both before and after tests. The gate
passed 3,261 tests / 2 skipped; independent verification matched all 45 installed
payloads and all 45 fresh before-images by full SHA, size, new basename and timestamp.
The installed entrypoint compiled in memory and reported harness version6; helper
compilation also passed. Release bundle SHA256:
`43d9aaf3bf558189900f60fed3e3924b9657e221e32e075084d7a234e1a4538f`.
Successful admission boundary: `20261103T232535Z`; backups: `20261103T233325Z`.

Installed Claude/medium Chat and GPT/high Responses batch cases each passed the
full-file-v2 delivery, pairing, exact-sentinel finish and launch-identity checks with
three generation POSTs. Engine-main elapsed samples were 12.552292s and 15.982691s.
The shared source-plus-installed ledger finished at 21 POSTs / 582,123 charged input
tokens, with 21 known inputs, zero missing inputs and zero excess over reservations;
caps remain 30 / 650,000 and were not reset. No further trial is needed for acceptance.
These samples do not establish causal speedup, shared-cache TTL/ownership or billing.
Claude cap enforcement, backend maximum windows and private forwarding behavior are
not newly established. Activation remains explicit opt-in with unchanged defaults.

Historical limit: one earlier gate exit1 lost its original trace. Controlled later
passes did not identify a root cause; the operator explicitly directed continuing at
23:07:43Z after that evidence gap was reported. It is retained as an unresolved
attribution limit, not a repaired failure or a weakened current gate.

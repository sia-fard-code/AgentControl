# AgentControl: Machine-Wide Delegated-Agent Accounting Design

**Date:** 2026-09-17  
**Status:** Approved design, pending implementation plan  
**Project:** `D:\repos\AgentControl`  
**Runtime scope:** Delegated-agent runs launched under the current Windows user account

## 1. Purpose

AgentControl is an independent local project that provides one control plane for delegated-agent token usage and cost accounting across projects on this machine. It replaces repository-local dashboards as the authoritative monitoring and accounting interface while preserving project identity, project-local security policies, and append-only audit history.

The first release is limited to monitoring, accounting, and operator intervention on runs it did not start. It shows a run's own narrated transcript on demand, and lets the operator steer a verified-alive run through the engine's `.steer` file or kill its verified engine process tree. It does not dispatch agents, enforce budgets, or block work. (Amended 2026-09-24; see `2026-09-24-narration-and-intervention-design.md`.)

## 2. Goals

AgentControl must:

1. Discover every run recorded by the shared delegated-agent engine, whether invoked directly or through a repository wrapper.
2. Discover previously unseen project roots as pending projects.
3. Require operator approval before a project contributes to normal token and cost totals.
4. Retain project identity on every run and provide machine-wide and per-project views.
5. Import all recoverable Projecta and Projectb history without duplicating logical runs.
6. Preserve sent, returned, cache-read, and cache-write token counters when reported.
7. Distinguish an unavailable counter (`null`) from a measured zero (`0`).
8. Use one machine-wide effective-dated pricing catalog.
9. Remain available only on `127.0.0.1`.
10. Continue accounting after dashboard restarts and without requiring the dashboard to be running when agents execute.
11. Avoid centrally storing prompts, assistant messages, tool results, credentials, or complete gateway responses.
12. Show a run's narrated transcript on demand, read through from the project's own transcript, and let the operator steer or kill a verified-alive run (added 2026-09-24).

## 3. Non-goals for release 1

Release 1 will not:

- Start or pause delegated agents, or stop them any way other than the operator's verified kill of a run's engine process tree (amended 2026-09-24).
- Enforce daily, monthly, project, or model budgets.
- Serve remote users or bind to a non-loopback address.
- Monitor delegation performed by other Windows user accounts.
- Replace repository `.claude/agent-policy.json` files or weaken repository security boundaries.
- Rewrite, delete, or relocate historical project transcripts.
- Introduce a database server, frontend framework, or network event broker.
- Treat worktrees as separate projects when their index record identifies the canonical parent project root.

## 4. Existing system and evidence

The current Projecta and Projectb dashboards are repository-scoped because each derives its data roots from the dashboard file location:

```python
ROOT = Path(__file__).resolve().parents[2]
RUNS = ROOT / ".claude" / "agent-runs"
LEDGER = ROOT / "costs" / "ledger.jsonl"
REGISTRY = ROOT / "runs" / "agent-registry.jsonl"
```

Consequently, Projecta cannot see Projectb artifacts. Projectb currently has two run transcripts and associated message and usage journals that are absent from the Projecta API.

The shared engine at `C:\Users\operator\.claude\bin\delegate.py` already provides the correct machine-wide discovery boundary (amended 2026-09-24, see 2026-09-24-repo-run-index-design.md: the engine also writes a copy of each record to a repo run index beside the transcript, and AgentControl discovers runs from those repo run indexes too):

```text
C:\Users\operator\.claude\agent-runs-index.jsonl
```

Its append-only start records identify the canonical root, run ID, artifact paths, model, label, effort, tool mode, and write mode. End records identify run status and aggregate sent/returned counters. The current index contains roots for Projecta, Projectb, and one temporary scratch location.

Current `.usage.jsonl` records preserve per-response prompt, completion, cache-read, and cache-write usage without conversation content. Existing Projecta accounting code already handles:

- OpenAI- and Anthropic-shaped usage.
- Nested cache details.
- Structured zero versus unavailable values.
- The fact that OpenAI-style `prompt_tokens` can already include cached input.
- Append-only ledger revisions.
- Effective-dated pricing.
- Machine-index reconciliation.

These proven semantics are the migration baseline.

## 5. Considered architectures

### 5.1 Central normalized control plane — selected

AgentControl consumes the machine-wide index, normalizes referenced usage into a central append-only ledger, owns project approval and pricing, and serves the dashboard. (Amended 2026-10-01, see 2026-10-01-tool-usage-design.md: AgentControl also imports Claude Code, Codex, Cline and Kilo model calls, numbers only, from their local logs into a second append-only tool-call ledger, and counts them in totals by tool.)

Benefits:

- One authoritative machine total.
- Direct and wrapped invocations use the same path.
- Stable project identity and filters.
- Dashboard downtime cannot interrupt agent execution.
- Historical sources can be reconciled and deduplicated once.
- Project-specific harness versions do not affect reporting after normalization.

### 5.2 Federated repository scanner — rejected

A central page could scan every approved project's ledger and transcript tree during each refresh. This avoids a central ledger but is slow and fragile. Moved or unavailable roots break reports, repository harness versions can disagree, and filename collisions and pricing conflicts remain unresolved.

### 5.3 Live HTTP ingestion — rejected

The engine could POST events directly to a dashboard service. This provides immediate updates but makes accounting dependent on service availability and requires buffering and replay. A durable append-only file already exists and is sufficient for a single-user machine.

## 6. System architecture

```text
Project wrapper or direct delegate.py invocation
                    |
                    v
Shared delegate.py under C:\Users\operator\.claude\bin
   |                                  |
   | append lifecycle                 | append sanitized usage
   v                                  v
~\.claude\agent-runs-index.jsonl     project-local *.usage.jsonl
                    \                 /
                     \ referenced by /
                          v
                  AgentControl sync
                          |
          +---------------+----------------+
          |               |                |
          v               v                v
  project events      run ledger      pricing catalog
          \               |                /
           +--------------+---------------+
                          v
             snapshot/API/dashboard
                          |
                  127.0.0.1:8787
```

The machine index and usage journals are durable producer artifacts. AgentControl is a consumer. Failure or shutdown of AgentControl must not prevent delegation.

## 7. Project layout

`D:\repos\AgentControl` will be a standalone Git repository:

```text
AgentControl/
├── README.md
├── AGENTS.md
├── pyproject.toml
├── config/
│   └── pricing.json
├── docs/
│   ├── operator-guide.md
│   └── superpowers/
│       ├── specs/
│       └── plans/
├── engine/
│   └── delegate.py           # version-controlled canonical engine source
├── scripts/
│   ├── deploy_engine.py
│   └── migrate_existing.py
├── src/
│   └── agent_control/
│       ├── __init__.py
│       ├── cli.py
│       ├── paths.py
│       ├── run_index.py
│       ├── projects.py
│       ├── usage.py
│       ├── ledger.py
│       ├── pricing.py
│       ├── migration.py
│       ├── snapshot.py
│       └── dashboard.py
├── web/
│   ├── index.html
│   ├── dashboard.js
│   └── dashboard.css
└── tests/
    ├── fixtures/
    ├── test_run_index.py
    ├── test_projects.py
    ├── test_usage.py
    ├── test_ledger.py
    ├── test_pricing.py
    ├── test_migration.py
    ├── test_snapshot.py
    ├── test_dashboard_api.py
    └── test_engine_contract.py
```

The service remains a lightweight Python 3.10+ application using the standard library, `psutil`, and vanilla JavaScript. No backend/frontend package hierarchy or external database is introduced.

## 8. Runtime state ownership

Generated accounting state is separate from the source checkout:

```text
C:\Users\operator\.claude\agent-control\
├── projects.jsonl
├── ledger.jsonl
├── pricing_history.jsonl       # absent until a real pricing edit
├── sync_state.json
└── migration_reports\
```

The source repository owns the baseline catalog at `config/pricing.json`. Runtime state belongs to the current user because the installed shared engine and machine index are also user-scoped.

Append-only files use one compact JSON object per line, append under an in-process lock, flush, and `fsync`. Mutable `sync_state.json` is written to a temporary sibling and atomically replaced.

## 9. Project registration model

### 9.1 Identity

A project has:

- `project_id`: generated UUID, immutable.
- `root`: resolved absolute path as first observed.
- `root_key`: Windows-normalized identity key based on `realpath`, separator normalization, and case folding.
- `display_name`: operator-editable label.
- `state`: current state reduced from append-only events.

The same normalized root resolves to the same project. A moved project appears as a new pending root until the operator explicitly approves it. Release 1 will not provide automatic move/link inference.

### 9.2 States

```text
discovered (pending) -> approved -> disabled
                     -> rejected
```

- **Pending:** Visible in diagnostics and project review. Runs are synchronized but excluded from normal token and cost totals.
- **Approved:** Historical and current runs contribute to normal totals.
- **Rejected:** Excluded from normal totals and hidden from the default project list, but retained for audit.
- **Disabled:** Previously approved project excluded from current totals without deleting its history.

Approval, rejection, disablement, re-approval, and rename are append-only events. Re-approval appends another `approved` event rather than introducing a separate event type. The latest valid event determines current state. Rename changes metadata, not identity.

### 9.3 Project event schema

```json
{
  "schema": 1,
  "event_id": "uuid",
  "project_id": "uuid",
  "event": "discovered|approved|rejected|disabled|renamed",
  "root": "D:\\repos\\Projectb",
  "root_key": "d:\\work\\tools\\projectb",
  "display_name": "Projectb",
  "observed_at": "2026-09-17T11:12:00+00:00",
  "note": "optional operator note"
}
```

Unknown roots from the machine index are persisted as pending. Temporary roots, scratch directories, and test fixtures cannot silently affect totals.

## 10. Machine-index contract

The shared engine is the only component that reliably observes every delegated run. AgentControl will version and test the installed engine's event contract. (Amended 2026-09-24, see 2026-09-24-repo-run-index-design.md: the engine writes the same records to a repo run index beside the transcript, and AgentControl discovers runs from those repo run indexes as well as from the machine-wide index, so a run whose machine-wide append failed under a sandbox user is still found.)

### 10.1 Start event

The next contract version adds process identity while retaining current fields:

```json
{
  "schema": 2,
  "run_id": "stable-id",
  "event": "start",
  "ts": "UTC timestamp",
  "harness_version": 6,
  "root": "canonical project root",
  "cwd": "actual execution root or worktree",
  "label": "task label",
  "model": "model name",
  "effort": "effort",
  "tool_mode": "mode",
  "write_mode": "stage|disk",
  "engine_pid": 12345,
  "transcript": "absolute artifact path",
  "messages": "absolute artifact path",
  "usage_journal": "absolute artifact path"
}
```

`root` identifies the canonical project. `cwd` may identify a disposable worktree and must not create a separate project.

### 10.2 End event

```json
{
  "schema": 2,
  "run_id": "stable-id",
  "event": "end",
  "ts": "UTC timestamp",
  "status": "complete|failed|interrupted",
  "tool_calls": 10,
  "refused": 0,
  "sent": 1234,
  "recv": 567
}
```

Start and end records join only by `run_id`. Replaying either record is safe.

### 10.3 Liveness

A start without an end is considered live only when:

- `engine_pid` identifies a running process;
- its command line identifies the shared engine; and
- its process creation time is compatible with the start timestamp.

Otherwise it becomes interrupted/orphaned according to explicit age and evidence rules. PID reuse alone cannot mark a historical run as live. Older schema-1 records without `engine_pid` use conservative artifact-activity heuristics and are labelled as inferred.

## 11. Usage normalization

A `.usage.jsonl` record contains only response accounting metadata:

```json
{
  "iter": 1,
  "phase": "loop|wrapup",
  "model": "model name",
  "ts": "UTC timestamp",
  "sent": 100,
  "recv": 20,
  "cache_read": 60,
  "cache_write": 10,
  "raw": {"sanitized usage mapping only": true}
}
```

The central normalizer supports current OpenAI and Anthropic shapes, including nested prompt token details. It rejects negative, Boolean, non-integral, and malformed counters.

Source precedence is:

1. Valid sanitized `.usage.jsonl` aggregation.
2. Valid structured usage from legacy `.messages.jsonl`.
3. Markdown footer sent/returned totals with cache counters set to `null`.
4. Missing usage.

Reported zero remains `0`. Unreported values remain `null`. The normalizer never infers authoritative cache usage.

A successful gateway response is journalled once, after the response succeeds and before its accounting leaves memory. Failed attempts are not journalled as billable successful responses. Loop and wrap-up responses use the same function and contract.

## 12. Central ledger

The central ledger is append-only and revisioned. A logical run may receive later revisions when stronger evidence arrives, but records are never edited in place.

### 12.1 Required run fields

```json
{
  "schema": 1,
  "run_id": "machine or deterministic legacy ID",
  "revision": 1,
  "project_id": "uuid",
  "timestamp": "run start in UTC",
  "ended_at": "UTC timestamp or null",
  "status": "complete|failed|interrupted|running",
  "label": "task label",
  "model": "model name",
  "effort": "effort or null",
  "tool_mode": "mode or null",
  "tool_calls": 10,
  "refused": 0,
  "tokens_sent": 100,
  "tokens_returned": 20,
  "cache_read": 60,
  "cache_write": null,
  "usage_source": "usage_journal|messages|footer|missing",
  "usage_artifact": "source reference or null",
  "transcript_artifact": "source reference or null",
  "provenance": ["machine_index", "projecta_ledger"],
  "observed_at": "UTC timestamp"
}
```

Costs are calculated at read/snapshot time from token fields and the machine-wide effective-dated catalog. Historical ledger rows do not embed mutable derived costs.

### 12.2 Deduplication

- Machine-index `run_id` is authoritative when available.
- A run with no machine ID receives a deterministic legacy ID derived from `project_id` and its canonical relative artifact path.
- Filename or label alone is never an identity key.
- Highest valid revision is the current logical row.
- Repeated synchronization or migration must not append a revision unless normalized evidence changes.
- Conflicting evidence creates a diagnostic. A stronger source may create a higher revision according to the documented precedence rule; it never silently mutates an old row.

## 13. Pricing

AgentControl owns one catalog for all approved projects:

- `config/pricing.json`: tracked baseline.
- `C:\Users\operator\.claude\agent-control\pricing_history.jsonl`: append-only effective-dated versions.

Rates remain USD per one million tokens for:

- input
- output
- cache read
- cache write

The rate active at the run start timestamp applies. Pricing preview is authoritative and shows affected runs, newly priced runs, superseded runs, old total, projected total, and delta before append.

The migration copies the verified baseline only after comparing available Projecta and Projectb baselines. Any conflict stops migration for operator review. Migration does not manufacture pricing-history records. `pricing_history.jsonl` stays absent until the operator confirms a genuine pricing edit.

When cache counters are unavailable, cost coverage explicitly records whether a value is exact, an upper bound, missing usage, or missing rates. Unknown cached input is not displayed as measured zero.

## 14. Synchronization

AgentControl incrementally consumes the machine index using byte offset plus file identity metadata in `sync_state.json`.

For each synchronization:

1. Validate new JSONL records without stopping on one malformed line.
2. Reduce duplicate start/end events by `run_id`.
3. Resolve `root` to an existing project or append a pending discovery event.
4. Validate referenced artifact paths.
5. Normalize lifecycle and usage evidence.
6. Append a central ledger revision only when evidence changed.
7. Persist the new checkpoint atomically after durable ledger/project appends.

If the index is truncated, rotated, or replaced, AgentControl performs a full rescan and deduplicates by `run_id`. If an artifact is temporarily inaccessible, the run remains visible with a diagnostic and is retried during later synchronization.

The dashboard triggers a lightweight sync before snapshots and may also run a background polling loop. Correctness does not depend on polling frequency.

## 15. Historical migration

Migration reads existing sources without modifying them.

### 15.1 Initial roots

- `D:\repos\Combinations\Projecta`
- `D:\repos\Projectb`

Other roots remain pending through normal machine-index discovery.

### 15.2 Evidence sources

The importer reads:

- Machine-index start/end records.
- Repository ledgers and highest valid revisions.
- Repository dispatch registries.
- Transcript metadata and footers.
- Sanitized `.usage.jsonl` journals.
- Legacy `.messages.jsonl` usage only.
- Anomaly metadata needed for state classification.

It does not copy conversation or tool-result content.

### 15.3 Procedure

1. Back up the installed engine and machine index.
2. Compare pricing baselines and stop on conflicts.
3. Run migration in dry-run mode.
4. Produce a report with input counts, logical run counts, duplicates, conflicts, inaccessible artifacts, usage coverage, source precedence, and projected totals.
5. Review the report.
6. Apply only with an explicit confirmation flag.
7. Append project and ledger records durably.
8. Run the same import again and prove that no new logical revisions are produced.
9. Compare central recoverable totals with repository totals.

Imported pending runs remain excluded from default totals until the operator approves their projects. Approving a project includes all its imported historical runs immediately; no re-import or service restart is required.

## 16. Dashboard and API

### 16.1 Views

The dashboard provides:

- Machine totals for today, current month, and all time.
- Live and historical runs.
- Filters for project, model, task label, state, and date.
- Spend breakdown by day, project, model, and task.
- Sent, returned, cache-read, and cache-write totals.
- Exact, upper-bound, missing-rate, and missing-usage indicators.
- Pending, approved, rejected, and disabled project views.
- Pending-project approval, rejection, rename, disable, and re-approval.
- Machine-wide pricing preview, append, current rates, and history.
- Diagnostics for malformed records, duplicate IDs, inaccessible artifacts, conflicting evidence, unsupported schemas, and excluded roots.

Date groups sort newest-first. Model, task, and project groups sort by descending known cost. Unknown values render as an em dash with an explanatory tooltip; partial totals render as known-only lower bounds.

### 16.2 API categories

Read endpoints include:

- `GET /api/data`
- `GET /api/projects`
- `GET /api/diagnostics`

Mutation endpoints include:

- `POST /api/projects/{project_id}/approve`
- `POST /api/projects/{project_id}/reject`
- `POST /api/projects/{project_id}/disable`
- `POST /api/projects/{project_id}/rename`
- `POST /api/pricing/preview`
- `POST /api/pricing/history`

Narration and intervention endpoints (`GET /api/narration`, and `POST /api/runs/{run_id}/steer`, `.../steer/cancel` and `.../kill`) are specified in `2026-09-24-narration-and-intervention-design.md`.

Exact routing may be implemented with the standard-library HTTP server, but invalid methods return `405`, unknown paths return `404`, and responses use strict JSON.

### 16.3 Local mutation security

The server:

- Binds only to `127.0.0.1`.
- Validates `Host` and same-origin `Origin` for mutation requests, and `Host` on every GET.
- Requires the CSRF token on `GET /api/narration`, the one endpoint that returns transcript content.
- Serves the page with a Content-Security-Policy that forbids inline script and framing.
- Requires `application/json` and a valid content length.
- Limits request body size.
- Rejects non-finite JSON numbers.
- Uses a random per-process CSRF token embedded in the locally served page and required by mutation requests.
- Validates all state transitions and pricing data server-side.
- Serializes append operations under locks.

The service is not an authentication boundary and must not be exposed through raw port forwarding or a non-loopback bind in release 1.

Operator-approved amendment, 2026-10-28 (image 0.2.0): an authenticated HTTPS
Nginx virtual host may proxy to the backend in a shared loopback network
namespace. The backend still binds only `127.0.0.1`, publishes no port, and has
no application-provided authentication. Opt-in `--public-origin` accepts one
strict bare HTTPS origin, validates the preserved Host and browser Origin,
rejects duplicate headers, and retains all CSRF and narration protections.
Default local behavior remains unchanged. Never fabricate Origin/CSRF headers
or trust forwarded headers for authorization. The proxy authenticates every
HTTPS route, strips Authorization upstream, uses external credential/TLS files,
and disables request error/access logging, body spooling, response buffering,
caching, and storage. This exception does not authorize host data/PID/socket
mounts or a Linux accounting migration. See the operator guide's authenticated
HTTPS deployment procedure.

## 17. Shared engine ownership and deployment

AgentControl becomes the version-controlled source for the shared `delegate.py` and its contract tests. The installed copy remains:

```text
C:\Users\operator\.claude\bin\delegate.py
```

`scripts/deploy_engine.py` will:

1. Require a passing engine contract test suite.
2. Compare source and installed versions.
3. Back up the installed file.
4. Write a temporary sibling and atomically replace the installed file.
5. Verify checksum and harness version after deployment.
6. Avoid reading or copying gateway credentials.

The engine remains nonblocking with respect to AgentControl. It appends directly to the user-scoped index and project-local usage journal. Index append errors are reported clearly; accounting cannot depend on a live HTTP request.

Repositories retain:

- `.claude/agent-policy.json`.
- Their policy self-test.
- An optional small wrapper that invokes the installed engine with an explicit canonical root.
- Maintainer documentation pointing to AgentControl.

Repo-local dashboard, ledger, pricing, and duplicated harness code are deprecated only after migration and parity verification. Their removal is a later cleanup, not part of initial cutover.

## 18. Error handling and resilience

- Malformed JSONL lines increment diagnostics and do not prevent other records from loading.
- Unsupported schema versions are retained as diagnostics and excluded from accounting until supported.
- Artifact paths are normalized before access. Paths referenced by unapproved roots may be inspected only for sanitized accounting metadata needed to populate pending diagnostics; they cannot contribute to normal totals.
- Missing files leave explicit missing-evidence coverage and are retried.
- Negative, Boolean, non-integral, or non-finite counters and rates are rejected.
- Duplicate events are idempotent.
- Concurrent sync and UI mutation operations cannot interleave partial appends.
- A failed checkpoint write may cause replay but not duplication.
- A failed ledger append prevents checkpoint advancement.
- A dashboard crash cannot corrupt previous append-only records.

## 19. Testing strategy

Implementation follows test-driven development. Existing Projecta accounting/dashboard tests are ported before implementation behavior is moved.

### 19.1 Accounting core

Tests cover:

- OpenAI and Anthropic usage mappings.
- Nested cache-read and cache-write counters.
- Prompt/cache double-count prevention.
- Structured zero versus `null`.
- Malformed counter rejection.
- Usage source precedence.
- Highest valid ledger revision.
- Effective-dated pricing and immutable append.
- Exact, upper-bound, missing-rate, and missing-usage coverage.

### 19.2 Project registry and index

Tests cover:

- Unknown root becomes pending.
- Pending runs are excluded from normal totals.
- Approval includes all historical/current project runs.
- Rejection and disablement exclude runs.
- Rename preserves identity.
- Worktree `cwd` does not create a separate project.
- Index replay is idempotent.
- Truncated or replaced index safely rescans.
- Start/end events reconcile only by `run_id`.
- Direct and wrapped invocation produce equivalent accounting.
- PID reuse cannot create false liveness.

### 19.3 Migration

Tests use fixture copies and cover:

- Cross-project filename collisions.
- Joining machine IDs to repository evidence.
- Stable deterministic legacy IDs.
- `.usage.jsonl` precedence.
- Footer-only unknown cache values.
- Conflicting evidence diagnostics.
- Dry-run write prohibition.
- Apply-mode append behavior.
- Second-run idempotence.
- Pricing-baseline conflict refusal.

### 19.4 Dashboard and API

Tests cover:

- Project/model/task/date/state filters.
- Global and project summaries.
- Newest-first dates and cost-ranked categorical groups.
- Unknown and partial counter rendering.
- Pending approval workflow.
- Invalid project transitions.
- Strict JSON, host/origin/CSRF, and body limits.
- Pricing preview and append.
- Concurrent append serialization.

Browser checks verify the major workflows against a temporary state directory and port.

### 19.5 Engine contract

Tests run the engine with a fake gateway and temporary roots and prove:

- Start and end index records are valid.
- Canonical root and worktree `cwd` are distinct.
- Engine PID is present.
- Successful loop and wrap-up responses each produce exactly one usage record.
- Failed attempts do not double-count successful usage.
- Usage journals contain no conversation content or credentials.
- Index write failure does not prevent delegation and produces a visible warning.

## 20. Implementation phases

### Phase 1: Bootstrap

Create the standalone Git repository, packaging, CLI skeleton, source/test layout, operator documentation, and a clean baseline commit.

### Phase 2: Accounting core

Port tests and extract the proven usage, ledger, pricing, and coverage behavior from Projecta into focused modules.

### Phase 3: Projects and ingestion

Implement append-only project events, hybrid pending approval, schema-aware machine-index ingestion, liveness, synchronization, and central run revisions.

### Phase 4: Historical importer

Implement dry-run reporting, deterministic identities, evidence precedence, idempotent application, and initial Projecta/Projectb reconciliation.

### Phase 5: Dashboard

Port existing views, add project-aware filtering and spend, implement project review and machine-wide pricing APIs, and harden mutation endpoints.

### Phase 6: Engine contract and deployment

Bring the current shared engine source under AgentControl, add schema-2 process identity, test usage journalling, and deploy atomically.

### Phase 7: Cutover

Run dry-run migration, back up inputs, apply and verify migration, approve Projecta and Projectb, run safe dispatches from both, prove replay idempotence, switch the single production listener to port 8787, and optionally add a current-user Windows scheduled task after manual stability is demonstrated.

### Phase 8: Later repository cleanup

After parity is verified and the central service is stable, replace repo-local dashboard/accounting copies with lightweight forwarding documentation or wrappers. This is a separate change in each repository and must preserve their unrelated work.

## 21. Cutover verification

Cutover must prove:

1. AgentControl is the only production listener on `127.0.0.1:8787`.
2. Projecta and Projectb runs appear with correct project identity.
3. Historical logical run counts match the migration report.
4. A second migration and index rescan create no duplicate revisions.
5. Cache counters survive from usage journal to API and UI.
6. Footer-only cache values remain unknown.
7. A temporary new root appears pending and does not change normal totals.
8. Approval includes that project's runs without restarting the server.
9. One safe direct run and one wrapper run are both captured.
10. Agent execution succeeds while AgentControl is stopped and appears after restart.
11. Machine-wide pricing resolves by run timestamp.
12. No pricing-history file exists unless an actual pricing change was confirmed.
13. No prompts, credentials, response bodies, or tool results exist in central state.
14. Full AgentControl tests and affected Projecta/Projectb tests pass.

## 22. Acceptance criteria

The feature is accepted when:

- AgentControl is an independent Git project at `D:\repos\AgentControl`.
- The dashboard reports approved delegated-agent usage across Projecta and Projectb in one interface.
- Every run retains a stable project identity.
- Newly observed roots appear pending.
- Pending, rejected, and disabled projects do not affect normal totals.
- Approval immediately includes all recoverable project history.
- Direct and wrapped engine invocations are captured.
- Historical import is idempotent and collision-safe.
- One effective-dated pricing catalog calculates approved-project costs.
- Unknown usage remains unknown and measured zero remains zero.
- Dashboard availability is not required for delegation.
- Central state contains accounting metadata only.
- The production service is loopback-only and uses one listener on port 8787.

## 23. Risks and mitigations

| Risk | Mitigation |
|---|---|
| Historical source conflicts | Deterministic precedence, dry-run conflict report, no silent overwrite |
| Duplicate imports | Machine run IDs, deterministic legacy IDs, revision comparison, idempotence tests |
| Temporary roots pollute totals | Hybrid pending approval and default exclusion |
| PID reuse mislabels old runs as live | PID, process command, creation-time, and start-time validation |
| Missing artifact after project move | Preserve ledger evidence, report inaccessible source, require explicit new-root approval |
| Dashboard mutation from another local page | Loopback binding, Host/Origin validation, CSRF token, strict JSON |
| Centralization loses repo security boundaries | Keep policy ownership and enforcement in each project/shared engine |
| Engine deployment breaks delegation | Contract tests, backup, atomic replace, checksum verification, rollback procedure |
| Index replay duplicates accounting | Run-ID reduction and append-only change detection |
| Unknown cache data underprices or misleads | Preserve `null`, expose coverage, render upper-bound/unknown semantics |

## 24. Final design decisions

- Use the central normalized control-plane architecture.
- Name and locate the project at `D:\repos\AgentControl`.
- Use hybrid project onboarding: auto-discover as pending, operator approves.
- Use one machine-wide effective-dated pricing catalog.
- Import all recoverable Projecta and Projectb history.
- Keep release 1 to monitoring, accounting, on-demand narration, and the operator's verified steer and kill (amended 2026-09-24).
- Keep runtime state under the current user's `.claude\agent-control` directory.
- Keep the shared engine and machine index as a nonblocking durable producer boundary.
- Preserve project-local security policies and project-local transcript content.
- Do not remove repo-local implementations until central parity is verified.

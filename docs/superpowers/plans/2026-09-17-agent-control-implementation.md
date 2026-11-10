# AgentControl Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a machine-wide, local-only delegated-agent monitoring and accounting control plane that aggregates approved projects while preserving project identity, unknown usage, immutable history, and nonblocking execution.

**Architecture:** AgentControl incrementally consumes the user-scoped append-only run index, reduces canonical project roots through an append-only registry, and normalizes referenced accounting evidence into a central revisioned ledger. Snapshot-time costs use one effective-dated catalog; a hardened `ThreadingHTTPServer` serves separate vanilla web assets only on `127.0.0.1`.

**Tech Stack:** Python 3.10+, standard library, `psutil`, `pytest`, `ThreadingHTTPServer`, vanilla JavaScript, HTML, CSS, and Node's built-in test runner.

**Spec:** `docs/superpowers/specs/2026-09-17-agent-control-design.md`

## Global Constraints

- The approved specification is authoritative; do not redesign it without contradictory implementation evidence.
- Release 1 is monitoring/accounting-only: no dispatch, stop/kill, budgets, database, framework, broker, or remote service.
- Bind only to `127.0.0.1`; agent execution must never depend on AgentControl being online.
- Runtime dependencies are only Python 3.10+ standard library and `psutil`.
- Never centrally store prompts, assistant content, tool results, credentials, complete gateway responses, or complete conversation sidecars.
- Keep repository `.claude/agent-policy.json` files and security boundaries.
- Preserve unavailable counters as `null` and genuine structured zero as `0`; reject Boolean, negative, non-integral, malformed, and non-finite values.
- Deduplicate by machine `run_id`; otherwise use project identity plus canonical relative artifact path. Never use label or filename alone.
- Every run retains project identity. Index `root`, not worktree `cwd`, identifies its project.
- Newly observed roots are pending and diagnostic; only approved projects enter normal totals.
- Use one effective-dated catalog. `pricing_history.jsonl` remains absent until a genuine confirmed pricing edit.
- Runtime state is under `C:\Users\operator\.claude\agent-control\`, separate from source.
- All tests inject temporary paths and must never touch live machine, Projecta, or Projectb state.
- JSONL appends share an in-process lock, append one compact object, flush, and `fsync`; mutable JSON uses a sibling temporary plus `os.replace`.
- Migration is read-only, dry-run-first, conflict/drift refusing, explicitly confirmed, and never rewrites or deletes source artifacts.
- Engine deployment is separately approved, backed up, atomic, checksum-verified, and rollback-capable.
- Do not remove repo-local implementations during migration/cutover; cleanup is a later review.
- Use Windows-compatible commands from `D:\repos\AgentControl`.
- Every task finishes with focused tests and one focused commit.
- For every red command, “expected failure” means nonzero exit with the named missing symbol/assertion and no writes outside that task's temporary fixture. For every green/refactor command, “expected pass” means exit code `0`, no `FAILED`/`ERROR` entries, and no live-state writes.
- Test helpers shown in snippets (`run_record`, `start_event`, `migration_case`, and similar) are created in the exact test file or `tests/conftest.py` named by that task; they are sanitized factories, not production interfaces.

Operator-approved amendment, 2026-10-28: image 0.2.0 adds opt-in authenticated
Nginx HTTPS access without changing the backend's loopback binding or default
local mode. Use a strict explicit `--public-origin`, retain real Host/Origin and
CSRF checks, and share only Nginx's network namespace. Keep password/TLS files
outside source and the image; disable proxy request logging and body spooling.
The implementation must pass temporary-path origin tests and disposable,
unpublished-port HTTPS/Basic Auth/CSRF/logging integration checks before the
updated multi-platform image is published. Host accounting/PID imports and
production activation remain separately operator-gated.

---

## Planning Safety Baseline

```text
git status --short
# no output
git rev-parse HEAD
e5ecd97e81638e7b325b8a9ecee35c0ad2a14cd9
git log -1 --oneline
e5ecd97 docs: define machine-wide AgentControl architecture
```

Projecta is the primary proven source. Projectb accounting code is effectively equivalent but has fewer persisted files. Historical trees changed during read-only inspection, so migration freezes a content manifest at dry run and refuses apply after drift.

## Complete File Map and Responsibility Lock

| File | Responsibility |
|---|---|
| `.gitignore`, `pyproject.toml` | Caches/build exclusions; Python floor, dependencies, entry point, pytest config. |
| `README.md`, `AGENTS.md` | Scope/setup; TDD, sensitive-data, live-action, and external-repository safety rules. |
| `config/pricing.json` | Tracked schema-1 uninitialized catalog envelope; receives compared rates only through confirmed conflict-free migration. |
| `docs/operator-guide.md` | Created in Task 1, then expanded with operations, backups, approvals, parity, rollback, listener, and deferred cleanup. |
| `docs/cutover-checklist.md` | Created in Task 20 with automated and explicitly approved live gates. |
| `src/agent_control/__init__.py` | Package version. |
| `src/agent_control/cli.py` | Parser/composition root; no accounting logic. |
| `src/agent_control/paths.py` | Immutable injectable `AppPaths`. |
| `src/agent_control/models.py` | Frozen dataclasses, literals, protocols; no I/O. |
| `src/agent_control/storage.py` | Strict/tolerant JSON, durable append, atomic JSON, shared locks. |
| `src/agent_control/usage.py` | OpenAI/Anthropic normalization and source precedence. |
| `src/agent_control/ledger.py` | Validation, revision reduction, semantic no-op, durable append. |
| `src/agent_control/pricing.py` | Baseline/history, effective rates, costs, preview, immutable append. |
| `src/agent_control/projects.py` | Canonical roots, stable UUIDs, event reduction/transitions. |
| `src/agent_control/run_index.py` | Schema parsing, path checks, run-ID reduction, checkpoints/rescans. |
| `src/agent_control/liveness.py` | `psutil` probe and schema-aware liveness. |
| `src/agent_control/sync.py` | Ordered index→project→usage→ledger→checkpoint transaction. |
| `src/agent_control/migration.py` | Read-only inventory, manifest, reconciliation, report, confirmed apply. |
| `src/agent_control/snapshot.py` | Approval-aware totals, filters, coverage, grouping, ordering. |
| `src/agent_control/dashboard.py` | Loopback HTTP, routes/security, static assets, locked mutations. |
| `engine/delegate.py` | Canonical schema-2 producer and sanitized exactly-once usage journal. |
| `scripts/migrate_existing.py` | Thin migration CLI wrapper. |
| `scripts/deploy_engine.py` | Test gate, backup, atomic install, verification, rollback. |
| `web/index.html`, `web/dashboard.css`, `web/dashboard.js` | Separate semantic page, styling, rendering/workflows. |
| `tests/conftest.py` | Temporary paths, clocks, process probe, sanitized factories/server. |
| `tests/test_paths.py`, `test_storage.py`, `test_usage.py`, `test_ledger.py`, `test_pricing.py` | Bootstrap/accounting unit contracts. |
| `tests/test_projects.py`, `test_run_index.py`, `test_liveness.py`, `test_sync.py` | Registry and ingestion contracts. |
| `tests/test_migration.py`, `fixtures/migration/{projecta,projectb}`, `fixtures/migration/machine-index.jsonl` | Sanitized history compatibility and idempotence. |
| `tests/test_snapshot.py`, `test_dashboard_api.py`, `test_web_contract.py`, `tests/js/dashboard.test.mjs` | Snapshot/API/security/UI contracts. |
| `tests/test_engine_contract.py`, `test_deploy_engine.py` | Producer and deployment contracts. |
| `tests/test_cli.py`, `test_operator_docs.py` | CLI and operator/cutover documentation contracts. |

Do not copy the old dashboard monolith. Python, HTML, CSS, and JavaScript remain separate.

## Locked Shared Contracts

`models.py` defines these exact frozen types before consumers:

```python
ProjectState = Literal["discovered", "approved", "rejected", "disabled"]
RunStatus = Literal["running", "complete", "failed", "interrupted"]
UsageSource = Literal["usage_journal", "messages", "footer", "missing"]
CostClassification = Literal["exact", "upper_bound", "missing_rate", "missing_usage"]

@dataclass(frozen=True)
class Diagnostic:
    code: str; message: str; source: str | None = None
    line: int | None = None; run_id: str | None = None
    project_id: str | None = None

@dataclass(frozen=True)
class UsageTotals:
    tokens_sent: int | None; tokens_returned: int | None
    cache_read: int | None; cache_write: int | None
    sent_includes_cache_read: bool | None; response_count: int
    tokens_sent_unknown_responses: int; tokens_returned_unknown_responses: int
    cache_read_unknown_responses: int; cache_write_unknown_responses: int

@dataclass(frozen=True)
class UsageEvidence:
    totals: UsageTotals; source: UsageSource; artifact: str | None
    diagnostics: tuple[Diagnostic, ...]

@dataclass(frozen=True)
class RunCandidate:
    run_id: str; project_id: str; timestamp: str; ended_at: str | None
    status: RunStatus; label: str; model: str; effort: str | None
    tool_mode: str | None; write_mode: str | None; tool_calls: int | None
    refused: int | None; usage: UsageEvidence; transcript_artifact: str | None
    provenance: tuple[str, ...]; lifecycle_inferred: bool

@dataclass(frozen=True)
class RunRecord:
    schema: int; run_id: str; revision: int; project_id: str
    timestamp: str; ended_at: str | None; status: RunStatus; label: str
    model: str; effort: str | None; tool_mode: str | None
    write_mode: str | None; tool_calls: int | None; refused: int | None
    tokens_sent: int | None; tokens_returned: int | None
    cache_read: int | None; cache_write: int | None
    sent_includes_cache_read: bool | None
    tokens_sent_unknown_responses: int; tokens_returned_unknown_responses: int
    cache_read_unknown_responses: int; cache_write_unknown_responses: int
    usage_source: UsageSource; usage_artifact: str | None
    transcript_artifact: str | None; provenance: tuple[str, ...]
    lifecycle_inferred: bool; observed_at: str

@dataclass(frozen=True)
class AppendResult:
    appended: bool; record: RunRecord

@dataclass(frozen=True)
class RateSet:
    input: float; output: float; cache_read: float; cache_write: float

@dataclass(frozen=True)
class PricingVersion:
    schema: int; version_id: str; model: str; effective_at: str
    created_at: str; rates: RateSet; note: str

@dataclass(frozen=True)
class CostResult:
    amount: float | None; classification: CostClassification
    rate_version_id: str | None

@dataclass(frozen=True)
class PricingPreview:
    model: str; effective_at: str; matching_runs: int; affected_runs: int
    newly_priced_runs: int; superseded_runs: int; old_known_total: float
    projected_known_total: float; delta: float; warnings: tuple[str, ...]

@dataclass(frozen=True)
class Project:
    project_id: str; root: str; root_key: str; display_name: str
    state: ProjectState; observed_at: str; note: str | None

@dataclass(frozen=True)
class ProjectEvent:
    schema: int; event_id: str; project_id: str
    event: Literal["discovered", "approved", "rejected", "disabled", "renamed"]
    root: str; root_key: str; display_name: str; observed_at: str
    note: str | None

@dataclass(frozen=True)
class FileIdentity:
    device: int; inode: int; prefix_length: int; prefix_sha256: str

@dataclass(frozen=True)
class IndexCheckpoint:
    offset: int; identity: FileIdentity | None

@dataclass(frozen=True)
class ProcessSnapshot:
    pid: int; command_line: tuple[str, ...]; create_time: float; running: bool

class ProcessProbe(Protocol):
    def inspect(self, pid: int) -> ProcessSnapshot | None: ...

@dataclass(frozen=True)
class Liveness:
    status: RunStatus; inferred: bool; code: str

@dataclass(frozen=True)
class SyncReport:
    scanned_records: int; discovered_projects: int; appended_revisions: int
    unchanged_runs: int; retry_runs: int; full_rescan: bool
    diagnostics: tuple[Diagnostic, ...]
```

`run_index.py` additionally defines typed `StartEvent`, `EndEvent`, and `IndexBatch`; `migration.py` defines typed `MigrationSource`, `SourceManifestEntry`, `MigrationEvidence`, `MigrationReport`, and `MigrationApplyResult`; `snapshot.py` defines `SnapshotQuery`; `dashboard.py` defines `DashboardApp`. Their complete fields and signatures are locked in their tasks before another task consumes them.

Persisted timestamps are timezone-aware ISO-8601 normalized to `+00:00`. A project receives `str(uuid4())` exactly once in the durable `discovered` event. Later observations look up the existing project by `root_key` and reuse that persisted immutable ID; a moved root therefore receives a new pending UUID. Legacy IDs use the persisted project ID:

```python
legacy_id = "legacy:" + sha256(
    f"{project_id}\x1f{canonical_relative_path}".encode("utf-8")
).hexdigest()
```

Relative paths resolve beneath the project root, use backslashes, and case-fold. Machine IDs remain unchanged.

## Phase 1 — Bootstrap and Storage

### Task 1: Bootstrap Packaging and Injectable Paths

**Files:** Create `.gitignore`, `pyproject.toml`, `README.md`, `AGENTS.md`, `config/pricing.json`, `docs/operator-guide.md`, `src/agent_control/__init__.py`, `src/agent_control/cli.py`, `src/agent_control/paths.py`, `tests/conftest.py`, `tests/test_paths.py`, and `tests/test_cli.py`.

**Interfaces:** `AppPaths.production(repo_root: Path | None=None) -> AppPaths`; `AppPaths.for_test(root: Path) -> AppPaths`; `main(argv: Sequence[str] | None=None) -> int`.

- [ ] **Red:** Add:

```python
def test_mutable_paths_are_temporary(tmp_path):
    p = AppPaths.for_test(tmp_path)
    assert all(x.is_relative_to(tmp_path) for x in
               (p.machine_index, p.projects, p.ledger, p.sync_state))
```

Run `python -m pytest tests/test_paths.py tests/test_cli.py -v`. Expected: `ModuleNotFoundError`.

- [ ] **Green:** Add `AppPaths` properties for `projects.jsonl`, `ledger.jsonl`, `pricing_history.jsonl`, `sync_state.json`, `migration_reports`; production defaults use the approved user paths. Add Python `>=3.10`, `psutil>=5.9,<8`, pytest extra, and `agent-control=agent_control.cli:main`. Baseline:

```json
{"schema":1,"currency":"USD","unit_tokens":1000000,"initialized":false,"models":{}}
```

Install with `python -m pip install -e ".[test]"`; rerun tests. Expected: all selected tests pass and `--help` creates no state.

- [ ] **Refactor/commit:** Run `python -m pytest tests/test_paths.py tests/test_cli.py -q`; commit:

```bat
git add .gitignore pyproject.toml README.md AGENTS.md config src tests docs/operator-guide.md
git commit -m "chore: bootstrap AgentControl package"
```

### Task 2: Add Shared Types and Crash-Safe Storage

**Files:** Create `models.py`, `storage.py`, `test_storage.py`; modify `conftest.py`.

**Interfaces:** locked types above; `StateLocks.write: threading.RLock`; `strict_json_loads(raw: str | bytes) -> object`; `read_jsonl(path: Path, *, source: str) -> tuple[list[dict[str, object]], tuple[Diagnostic, ...]]`; `append_jsonl(path: Path, records: Sequence[Mapping[str, object]], lock: threading.RLock) -> None`; `atomic_write_json(path: Path, value: Mapping[str, object], lock: threading.RLock) -> None`; `canonical_json_hash(value: object) -> str`.

- [ ] **Red:** Test strict rejection of `NaN/Infinity`, tolerant malformed/non-object JSONL diagnostics, and monkeypatched `os.fsync` invocation.

```python
@pytest.mark.parametrize("raw", ['{"n":NaN}', '{"n":Infinity}', '{"n":-Infinity}'])
def test_strict_json_rejects_non_finite(raw):
    with pytest.raises(ValueError, match="finite"):
        strict_json_loads(raw)

def test_append_flushes_and_fsyncs(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr("agent_control.storage.os.fsync", calls.append)
    append_jsonl(tmp_path / "rows.jsonl", [{"run_id": "r"}], threading.RLock())
    assert len(calls) == 1
```

Run `python -m pytest tests/test_storage.py -v`; expected: collection fails with `ModuleNotFoundError: agent_control.storage`.
- [ ] **Green:** Implement:

```python
def append_jsonl(path, records, lock):
    encoded = "".join(json.dumps(dict(r), sort_keys=True, separators=(",", ":"),
                                 allow_nan=False) + "\n" for r in records)
    if not encoded: return
    with lock:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8", newline="\n") as f:
            f.write(encoded); f.flush(); os.fsync(f.fileno())
```

`atomic_write_json` writes/fsyncs a unique sibling then `os.replace`; serialization failure leaves destination unchanged. Run tests; expected pass.
- [ ] **Refactor/commit:** Share fake UTC clock/lock fixtures; run selected tests; commit `feat: add durable state primitives`.

## Phase 2 — Accounting Core

### Task 3: Normalize Usage Shapes

**Files:** Create `usage.py`, `test_usage.py`.

**Interfaces:** `validate_counter(value: object, field: str, *, required: bool = False) -> int | None`; `normalize_usage_mapping(mapping: Mapping[str, object]) -> UsageTotals`.

- [ ] **Red:** Test OpenAI nested `prompt_tokens_details.cached_tokens/cache_write_tokens`, Anthropic cache fields, zero/null, ambiguous mixed shapes, and `[-1, True, 1.5, "4", inf]` rejection.

```python
def test_openai_nested_cache_details_preserve_zero_and_shape():
    totals = normalize_usage_mapping({
        "prompt_tokens": 100, "completion_tokens": 20,
        "prompt_tokens_details": {"cached_tokens": 60, "cache_write_tokens": 0},
    })
    assert (totals.tokens_sent, totals.cache_read, totals.cache_write) == (100, 60, 0)
    assert totals.sent_includes_cache_read is True

@pytest.mark.parametrize("bad", [-1, True, 1.5, "4", float("inf")])
def test_invalid_counter_is_rejected(bad):
    with pytest.raises(ValueError):
        normalize_usage_mapping({"input_tokens": bad, "output_tokens": 0})
```

Run `python -m pytest tests/test_usage.py -v`; expected: collection fails because `normalize_usage_mapping` is missing.
- [ ] **Green:** OpenAI maps prompt/completion and marks `sent_includes_cache_read=True`; Anthropic maps input/output/cache fields and marks false. Present malformed nested details raise `ValueError`. Minimal validator:

```python
if value is None and not required: return None
if isinstance(value, bool) or not isinstance(value, int) or value < 0:
    raise ValueError(f"{field} must be a non-negative integer or null")
```

Run `python -m pytest tests/test_usage.py -v`; expected pass.
- [ ] **Refactor/commit:** Centralize recognized keys; commit `feat: normalize gateway usage shapes`.

### Task 4: Aggregate Journals and Apply Source Precedence

**Files:** Modify `src/agent_control/usage.py` and `tests/test_usage.py`; create `tests/fixtures/migration/projecta/.claude/agent-runs/20260916-120000-sample.md`, `20260916-120000-sample.messages.jsonl`, and `20260916-120000-sample.usage.jsonl`.

**Interfaces:** `aggregate_usage_records(records: Sequence[Mapping[str, object]]) -> UsageTotals`; `read_usage_journal(path: Path) -> UsageEvidence`; `read_messages_usage(path: Path) -> UsageEvidence`; `read_footer_usage(path: Path) -> UsageEvidence`; `select_usage_evidence(transcript: Path | None, messages: Path | None, journal: Path | None) -> UsageEvidence`.

- [ ] **Red:** Test per-response sums, unknown-response counts, reported zero, malformed-line diagnostics, shape conflicts, and journal→messages→footer→missing order. Footer cache values must be `None`.

```python
def test_usage_journal_wins_and_partial_cache_stays_partial(three_sources):
    transcript, messages, journal = three_sources
    evidence = select_usage_evidence(transcript, messages, journal)
    assert evidence.source == "usage_journal"
    assert evidence.totals.cache_read == 5
    assert evidence.totals.cache_read_unknown_responses == 1

def test_footer_does_not_fabricate_cache_zero(footer_transcript):
    evidence = read_footer_usage(footer_transcript)
    assert evidence.totals.cache_read is None
    assert evidence.totals.cache_write is None
```

Run `python -m pytest tests/test_usage.py -k "journal or footer or precedence" -v`; expected: failures name missing evidence reader/selector functions.
- [ ] **Green:** Each counter sums reported values and remains `None` if none report it. Partly valid journal wins with diagnostics; wholly invalid source falls through. Messages traversal returns recognized usage only. Footer regex returns sent/returned and unknown cache. Selector:

```python
for path, reader in ((journal, read_usage_journal),
                     (messages, read_messages_usage),
                     (transcript, read_footer_usage)):
    if path and path.is_file():
        evidence = reader(path)
        if evidence.source != "missing": return evidence
return missing_usage_evidence()
```

Run `python -m pytest tests/test_usage.py -v`; expected: exit `0` with no failed/error tests.
- [ ] **Refactor/commit:** Scan fixture keys for forbidden content fields; commit `feat: add usage evidence precedence`.

### Task 5: Implement Revisioned Central Ledger

**Files:** Create `ledger.py`, `test_ledger.py`; modify `models.py`.

**Interfaces:** `validate_run_record(value: Mapping[str, object], context: str) -> RunRecord`; `load_ledger(path: Path) -> tuple[list[RunRecord], tuple[Diagnostic, ...]]`; `highest_valid_revisions(rows: Sequence[RunRecord]) -> dict[str, RunRecord]`; `semantic_run_payload(record: RunRecord) -> dict[str, object]`; `sync_candidate(path: Path, candidate: RunCandidate, *, observed_at: str, locks: StateLocks) -> AppendResult`.

- [ ] **Red:** Test highest valid revision, malformed/descending duplicate diagnostics, durable append, changed evidence revision, and observation-time-only no-op.

```python
def test_unchanged_candidate_does_not_append_revision(tmp_path, locks):
    candidate = run_candidate(run_id="r", tokens_sent=10)
    first = sync_candidate(tmp_path / "ledger.jsonl", candidate,
                           observed_at="2026-09-17T12:00:00+00:00", locks=locks)
    second = sync_candidate(tmp_path / "ledger.jsonl", candidate,
                            observed_at="2026-09-17T12:01:00+00:00", locks=locks)
    assert first.appended is True
    assert second.appended is False
    assert len((tmp_path / "ledger.jsonl").read_text().splitlines()) == 1
```

Run `python -m pytest tests/test_ledger.py -v`; expected: collection fails with `ModuleNotFoundError: agent_control.ledger`.
- [ ] **Green:** Central rows require schema/revision. Semantic equality excludes only schema, revision, observed_at; normalized unique provenance remains evidence. Under lock reload current, compare, then append revision 1 or current+1:

```python
if current and semantic_run_payload(current) == candidate_payload(candidate):
    return AppendResult(False, current)
record = RunRecord(schema=1, revision=1 if not current else current.revision+1,
                   observed_at=normalize_utc(observed_at), **candidate_payload(candidate))
append_jsonl(path, [asdict(record)], locks.write)
```

Run ledger tests; expected pass.
- [ ] **Refactor/commit:** Ensure invalid values never become zero; commit `feat: add revisioned central ledger`.

### Task 6: Add Effective-Dated Pricing, Coverage, Preview, and Append

**Files:** Create `pricing.py`, `test_pricing.py`; modify `models.py`.

**Interfaces:** `load_baseline(path: Path) -> dict[str, RateSet | None]`; `load_pricing_history(path: Path) -> tuple[list[PricingVersion], tuple[Diagnostic, ...]]`; `validate_rate_set(value: object, context: str) -> RateSet`; `resolve_rates(model: str, started_at: str, baseline: Mapping[str, RateSet | None], history: Sequence[PricingVersion]) -> tuple[RateSet | None, str | None]`; `calculate_cost(record: RunRecord, baseline: Mapping[str, RateSet | None], history: Sequence[PricingVersion]) -> CostResult`; `preview_pricing(candidate: Mapping[str, object], runs: Sequence[RunRecord], baseline: Mapping[str, RateSet | None], history: Sequence[PricingVersion], *, now: datetime) -> PricingPreview`; `append_pricing_version(path: Path, candidate: Mapping[str, object], runs: Sequence[RunRecord], baseline: Mapping[str, RateSet | None], *, now: datetime, locks: StateLocks) -> tuple[PricingVersion, PricingPreview]`.

- [ ] **Red:** Port Projecta cases: exact effective boundary, future exclusion, append-order tie, earlier complete fallback, OpenAI de-dup arithmetic, four classifications, preview impact, no-write preview, invalid append absence.

```python
def test_openai_prompt_tokens_do_not_double_bill_cached_input():
    row = run_record(tokens_sent=100, cache_read=60, cache_write=0,
                     tokens_returned=0, sent_includes_cache_read=True)
    result = calculate_cost(row, {"model": rate_set(input=5, cache_read=.5)}, [])
    assert result.amount == pytest.approx((40 * 5 + 60 * .5) / 1_000_000)
    assert result.classification == "exact"

def test_preview_does_not_create_pricing_history(tmp_path):
    preview_pricing(candidate(), [run_record()], {"model": None}, [], now=NOW)
    assert not (tmp_path / "pricing_history.jsonl").exists()
```

Run `python -m pytest tests/test_pricing.py -v`; expected: collection fails with `ModuleNotFoundError: agent_control.pricing`.
- [ ] **Green:** Rates are complete four-key finite non-negative sets. Newest effective time ≤ start wins; equal time uses later append. Cost:

```python
if row.tokens_sent is None or row.tokens_returned is None:
    return CostResult(None, "missing_usage", None)
rates, source = resolve_rates(row.model, row.timestamp, baseline, history)
if rates is None:
    return CostResult(None, "missing_rate", None)
input_tokens = row.tokens_sent
if row.sent_includes_cache_read is True and row.cache_read is not None:
    input_tokens = max(input_tokens - row.cache_read, 0)
cache_read = 0 if row.cache_read is None else row.cache_read
cache_write = 0 if row.cache_write is None else row.cache_write
amount = (input_tokens*rates.input + row.tokens_returned*rates.output
          + cache_read*rates.cache_read + cache_write*rates.cache_write) / 1_000_000
complete = (row.cache_read is not None and row.cache_write is not None
            and row.sent_includes_cache_read is not None
            and row.cache_read_unknown_responses == 0
            and row.cache_write_unknown_responses == 0)
return CostResult(amount, "exact" if complete else "upper_bound", source)
```

This preserves the proven conservative arithmetic: when cache-read inclusion is unknown, all sent tokens remain at the full input rate; unknown cache counters are not exposed as measured zero in the record or snapshot, and the estimate is explicitly classified/displayed as an upper bound. Missing sent/returned means `missing_usage` with no amount; missing rates means `missing_rate` with no amount. Tests separately cover all branches, including unknown cache-write and prompt-shape evidence. Preview counts matching/affected/newly-priced/superseded and totals. Append reloads/re-previews under lock, adds server UUID/created time, then durable-appends. Run pricing tests; expected pass and history absent except successful append.
- [ ] **Refactor/commit:** Verify baseline is never mutated by preview; commit `feat: add effective dated pricing`.

## Phase 3 — Projects and Ingestion

### Task 7: Implement Append-Only Project Registry

**Files:** Create `projects.py`, `test_projects.py`; modify `models.py`.

**Interfaces:** `canonical_root(root: str | Path) -> tuple[str, str]`; `load_projects(path: Path) -> tuple[dict[str, Project], tuple[Diagnostic, ...]]`; `ProjectRegistry(path: Path, locks: StateLocks, clock: Callable[[], datetime])`; `ProjectRegistry.discover(root: str | Path, display_name: str | None = None) -> tuple[Project, bool]`; `ProjectRegistry.transition(project_id: str, event: Literal["approved", "rejected", "disabled"], note: str | None = None) -> Project`; `ProjectRegistry.rename(project_id: str, display_name: str, note: str | None = None) -> Project`.

- [ ] **Red:** Test Windows case/separator normalization, stable UUID, discovered/approved/rejected/disabled, later approved reapproval, rename identity, invalid transitions, and 16-thread discovery writing one event.

```python
def test_reapproval_and_rename_preserve_persisted_identity(registry):
    project, created = registry.discover(r"D:\repos\Projectb")
    assert created and project.state == "discovered"
    registry.transition(project.project_id, "approved")
    registry.transition(project.project_id, "disabled")
    approved = registry.transition(project.project_id, "approved")
    renamed = registry.rename(project.project_id, "Digital Signal Processing")
    assert approved.project_id == renamed.project_id == project.project_id
    assert renamed.display_name == "Digital Signal Processing"
```

Run `python -m pytest tests/test_projects.py -v`. Expected: collection fails because `agent_control.projects` is absent.
- [ ] **Green:** Use `realpath`, normalized backslashes, and case-folding. The first locked discovery creates `project_id=str(uuid4())`; later discoveries find the persisted ID by `root_key`. Events exactly match the approved schema. Allowed state changes are discovered→approved/rejected, approved→disabled, and rejected/disabled→approved; same-state and all other changes fail without append. Rename keeps identity/state. Lock spans reload/check/append. Names are stripped 1–100 non-control characters; notes ≤500.

```python
with self.locks.write:
    projects, diagnostics = load_projects(self.path)
    existing = next((p for p in projects.values() if p.root_key == root_key), None)
    if existing is not None:
        return existing, False
    event = ProjectEvent(schema=1, event_id=str(uuid4()), project_id=str(uuid4()),
                         event="discovered", root=root, root_key=root_key,
                         display_name=name, observed_at=utc_now, note=None)
    append_jsonl(self.path, [asdict(event)], self.locks.write)
```
- [ ] **Refactor/commit:** Run `python -m pytest tests/test_projects.py -v`; expected pass. Commit `feat: add append only project registry`.

### Task 8: Parse Machine Index and Checkpoint Safely

**Files:** Create `run_index.py`, `test_run_index.py`; modify `models.py`.

**Interfaces:** `StartEvent(schema: int, run_id: str, ts: str, harness_version: int, root: str, cwd: str, label: str, model: str, effort: str | None, tool_mode: str | None, write_mode: str | None, engine_pid: int | None, transcript: str, messages: str, usage_journal: str)`; `EndEvent(schema: int, run_id: str, ts: str, status: RunStatus, tool_calls: int | None, refused: int | None, sent: int | None, recv: int | None)`; `IndexBatch(events: tuple[StartEvent | EndEvent, ...], checkpoint: IndexCheckpoint, full_rescan: bool, diagnostics: tuple[Diagnostic, ...])`; `parse_index_record(value: Mapping[str, object], line: int) -> StartEvent | EndEvent`; `read_index_batch(path: Path, checkpoint: IndexCheckpoint) -> IndexBatch`; `reduce_index_events(events: Sequence[StartEvent | EndEvent]) -> tuple[dict[str, tuple[StartEvent | None, EndEvent | None]], tuple[Diagnostic, ...]]`; `validate_artifact_paths(start: StartEvent) -> tuple[StartEvent, tuple[Diagnostic, ...]]`.

- [ ] **Red:** Test schema 1/2, start/end join only by exact `run_id`, duplicate replay/conflict, unmatched end, worktree cwd, malformed/unsupported records, absolute/suffix/sibling path checks, partial trailing line, append offset, truncation/replacement/prefix mismatch rescans.

```python
def test_start_and_end_never_join_by_label_or_filename():
    pairs, diagnostics = reduce_index_events([
        start_event(run_id="start-id", label="same"),
        end_event(run_id="end-id"),
    ])
    assert pairs["start-id"][1] is None
    assert pairs["end-id"][0] is None

def test_truncation_forces_safe_full_rescan(index_with_checkpoint):
    path, checkpoint = index_with_checkpoint
    path.write_text(json.dumps(start_json(run_id="replacement")) + "\n")
    batch = read_index_batch(path, checkpoint)
    assert batch.full_rescan is True
    assert batch.events[0].run_id == "replacement"
```

Run `python -m pytest tests/test_run_index.py -v`. Expected: collection fails because `agent_control.run_index` is absent.
- [ ] **Green:** Schema 1 maps producer `ok` to complete; schema 2 accepts complete/failed/interrupted. Exact duplicates no-op. A conflicting duplicate emits `conflicting_duplicate_event`; the first valid event remains authoritative until an explicit evidence-precedence rule selects a stronger source, so replay order cannot silently rewrite lifecycle evidence. Checkpoint records offset, device, inode, prefix length/hash and stops at last newline. Size below offset, identity change, or prefix mismatch scans from zero. Never join by label/path/filename.

```python
slot = 0 if isinstance(event, StartEvent) else 1
pair = list(pairs.setdefault(event.run_id, (None, None)))
if pair[slot] is None:
    pair[slot] = event
elif pair[slot] != event:
    diagnostics.append(Diagnostic("conflicting_duplicate_event",
                                  f"conflicting {event.event} ignored",
                                  run_id=event.run_id))
pairs[event.run_id] = tuple(pair)
```
- [ ] **Refactor/commit:** Run index tests; expected pass. Commit `feat: parse incremental machine run index`.

### Task 9: Classify Liveness

**Files:** Create `liveness.py`, `test_liveness.py`; modify fixtures.

**Interfaces:** `PsutilProcessProbe.inspect(pid: int) -> ProcessSnapshot | None`; `classify_liveness(start: StartEvent, end: EndEvent | None, *, now: datetime, latest_artifact_mtime: float | None, probe: ProcessProbe) -> Liveness`.

- [ ] **Red:** Test end mapping; PID missing/gone; engine command mismatch; creation-time mismatch/PID reuse; schema-1 recent/stale/missing activity. Representative regression:

```python
def test_pid_reuse_does_not_mark_historical_run_live(fake_probe):
    fake_probe.result = ProcessSnapshot(
        42, ("python", r"C:\Users\operator\.claude\bin\delegate.py"),
        utc("2026-09-17T13:00:00+00:00").timestamp(), True,
    )
    result = classify_liveness(
        start_event(engine_pid=42, ts="2026-09-17T12:00:00+00:00"), None,
        now=utc("2026-09-17T13:01:00+00:00"),
        latest_artifact_mtime=None, probe=fake_probe,
    )
    assert result == Liveness("interrupted", False, "creation_time_mismatch")
```

Run `python -m pytest tests/test_liveness.py -v`. Expected: collection fails because `agent_control.liveness` is absent.
- [ ] **Green:** Schema 2 is running only if process runs, command contains normalized `\.claude\bin\delegate.py`, and creation is between start−300s and start+5s. Identity failures are authoritative interrupted codes. Schema 1 is inferred running only when artifact activity is 0–300s old; otherwise inferred interrupted.

```python
snapshot = probe.inspect(start.engine_pid) if start.engine_pid else None
if snapshot is None or not snapshot.running:
    return Liveness("interrupted", False, "process_gone")
command = " ".join(snapshot.command_line).replace("/", "\\").casefold()
if "\\.claude\\bin\\delegate.py" not in command:
    return Liveness("interrupted", False, "command_mismatch")
if not start_epoch - 300 <= snapshot.create_time <= start_epoch + 5:
    return Liveness("interrupted", False, "creation_time_mismatch")
return Liveness("running", False, "process_identity_valid")
```
- [ ] **Refactor/commit:** Test exact time boundaries; run liveness tests; commit `feat: validate delegated run liveness`.

### Task 10: Synchronize Index into Registry and Ledger

**Files:** Create `sync.py`, `test_sync.py`; modify `cli.py`, `test_cli.py`.

**Interfaces:** `Synchronizer(paths: AppPaths, locks: StateLocks, clock: Callable[[], datetime], process_probe: ProcessProbe)`; `Synchronizer.run() -> SyncReport`. Sync state is exactly `{"schema":1,"checkpoint":IndexCheckpoint JSON,"open_starts":{run_id:StartEvent JSON},"unmatched_ends":{run_id:EndEvent JSON},"retry_run_ids":[str],"diagnostics":[Diagnostic JSON]}`.

- [ ] **Red:** Test unknown root pending + ledgered run; approval later affects snapshot input without reimport; direct/wrapper metadata equivalence; inaccessible artifact retry; stronger evidence revision; duplicate replay; failed checkpoint replay no duplicate; failed ledger append no checkpoint; and a start in batch one joining an end in batch two. Representative cross-batch test:

```python
def test_start_is_persisted_until_later_end(temp_paths, synchronizer):
    append_index(temp_paths.machine_index, [start_json(run_id="r")])
    assert synchronizer.run().appended_revisions == 1
    append_index(temp_paths.machine_index, [end_json(run_id="r")])
    assert synchronizer.run().appended_revisions == 1
    current = highest_valid_revisions(load_ledger(temp_paths.ledger)[0])["r"]
    assert current.status == "complete"
    assert current.revision == 2
```

Run `python -m pytest tests/test_sync.py tests/test_cli.py -v`. Expected: collection fails because `agent_control.sync` is absent.
- [ ] **Green:** Under shared lock: load state, merge persisted `open_starts`/`unmatched_ends` with the new batch by run ID, discover only `start.root`, validate paths, classify, select usage, sync candidates, retry missing evidence, and atomically checkpoint after durable appends. Completed pairs leave both pending maps; unmatched starts/ends remain for later batches. Index end sent/recv is lifecycle reconciliation evidence only and may fill primary counters only when all artifact sources are missing; cache remains unknown and `usage_source` remains `missing` with diagnostic `index_end_usage_fallback`, never falsely labelled footer. CLI `sync` emits strict JSON.

```python
state = load_sync_state(self.paths.sync_state)
events = [*state.open_starts.values(), *state.unmatched_ends.values(),
          *read_index_batch(self.paths.machine_index, state.checkpoint).events]
pairs, diagnostics = reduce_index_events(events)
# Durable project/ledger appends happen before this atomic checkpoint/state write.
atomic_write_json(self.paths.sync_state, asdict(next_state), self.locks.write)
```
- [ ] **Refactor/commit:** Run `python -m pytest tests/test_sync.py tests/test_cli.py -v`; expected pass. Commit `feat: synchronize machine run evidence`.

## Phase 4 — Historical Migration

### Task 11: Inventory Historical Evidence and Collision-Safe IDs

**Files:** Create `src/agent_control/migration.py`, `tests/test_migration.py`, `tests/fixtures/migration/projecta/costs/ledger.jsonl`, `tests/fixtures/migration/projecta/costs/pricing.json`, `tests/fixtures/migration/projecta/runs/agent-registry.jsonl`, sanitized transcript/message/usage/anomaly siblings under `tests/fixtures/migration/projecta/.claude/agent-runs/`, colliding sanitized siblings under `tests/fixtures/migration/projectb/.claude/agent-runs/`, and `tests/fixtures/migration/machine-index.jsonl`.

**Interfaces:** `MigrationSource(name: str, root: Path, session_dir: Path | None)`; `SourceManifestEntry(path: str, size: int, mtime_ns: int, sha256: str)`; `MigrationEvidence(run_id: str, project_id: str, artifact_relative_path: str, candidate: RunCandidate, source_rank: int, artifact_match: bool)`; `build_source_manifest(sources: Sequence[MigrationSource], index: Path) -> tuple[SourceManifestEntry, ...]`; `legacy_run_id(project_id: str, project_root: Path, artifact: Path) -> str`; `collect_migration_evidence(sources: Sequence[MigrationSource], index: Path) -> tuple[list[MigrationEvidence], tuple[Diagnostic, ...]]`.

- [ ] **Red:** Test machine index, highest-valid repo ledger (missing revision=1 compatibility), registry, transcript footer, usage, messages usage only, anomaly allowlist, cross-project same filename, canonical artifact machine link, and forbidden-key absence.

```python
def test_same_relative_filename_in_two_projects_has_distinct_legacy_ids(sources):
    relative = Path(".claude/agent-runs/20260916-120000-sample.md")
    projecta, projectb = sources
    assert legacy_run_id(projecta.project_id, projecta.root, projecta.root / relative) != (
        legacy_run_id(projectb.project_id, projectb.root, projectb.root / relative)
    )

def test_machine_id_is_joined_only_by_canonical_artifact_path(migration_fixture):
    evidence, diagnostics = collect_migration_evidence(
        migration_fixture.sources, migration_fixture.index
    )
    linked = next(item for item in evidence if item.artifact_match)
    assert linked.run_id == "machine-a-run"
```

Run `python -m pytest tests/test_migration.py -k "legacy or artifact or sensitive" -v`; expected: collection fails with `ModuleNotFoundError: agent_control.migration`.
- [ ] **Green:** Machine ID replaces legacy only on canonical artifact match. Implement:

```python
relative = artifact.resolve(strict=False).relative_to(root.resolve(strict=False))
canonical = str(relative).replace("/", "\\").casefold()
return "legacy:" + sha256(f"{project_id}\x1f{canonical}".encode()).hexdigest()
```

Readers retain metadata/usage only; anomaly allowlist is turn, attempt, mode, model, kind, cause, type, transient. Manifest records path, size, `mtime_ns`, SHA-256 for every input.
- [ ] **Refactor/commit:** Run migration reader tests and forbidden-key scan; commit `feat: inventory historical accounting evidence`.

### Task 12: Plan Dry-Run Migration and Refuse Conflicts

**Files:** Modify `migration.py`, `test_migration.py`, `cli.py`, `test_cli.py`; create `scripts/migrate_existing.py`.

**Interfaces:** `MigrationReport(schema: int, report_id: str, generated_at: str, manifest: tuple[SourceManifestEntry, ...], input_counts: Mapping[str, int], logical_runs: int, machine_ids: int, legacy_ids: int, duplicates: int, conflicts: int, inaccessible_artifacts: int, malformed_records: int, unsupported_records: int, usage_sources: Mapping[UsageSource, int], coverage: Mapping[CostClassification, int], project_roots: tuple[str, ...], planned_candidates: tuple[RunCandidate, ...], pricing_decision: Literal["initialize","unchanged","conflict"], baseline_catalog: Mapping[str, object] | None, projected_normal_total: float, projected_excluded_total: float, diagnostics: tuple[Diagnostic, ...], applicable: bool)`; `plan_migration(paths: AppPaths, sources: Sequence[MigrationSource], *, clock: Callable[[], datetime]) -> MigrationReport`; `write_migration_report(path: Path, report: MigrationReport) -> str`, returning the SHA-256 of exact report bytes.

- [ ] **Red:** Test dry run writes only explicit report; counts and precedence; differing available Projecta/Projectb baselines refuse; missing Projectb baseline is absence; nonempty tracked mismatch refuses.

```python
def test_pricing_conflict_makes_report_inapplicable(conflicting_sources, temp_paths):
    report = plan_migration(temp_paths, conflicting_sources, clock=fixed_clock)
    assert report.pricing_decision == "conflict"
    assert report.applicable is False

def test_dry_run_report_does_not_create_runtime_accounting(temp_paths, sources):
    report = plan_migration(temp_paths, sources, clock=fixed_clock)
    write_migration_report(temp_paths.migration_reports / "review.json", report)
    assert not temp_paths.projects.exists()
    assert not temp_paths.ledger.exists()
    assert not temp_paths.pricing_history.exists()
```

Run `python -m pytest tests/test_migration.py tests/test_cli.py -k "report or pricing or dry_run" -v`; expected: failures name missing `plan_migration` and migrate CLI behavior.
- [ ] **Green:** Pricing decisions are initialize/unchanged/conflict. Report ID hashes canonical content excluding ID/generated time. Source precedence is journal→messages→footer→missing while machine lifecycle merges by exact artifact. Wrapper calls `main(["migrate", *sys.argv[1:]])`.

```python
unsigned = {key: value for key, value in asdict(report).items()
            if key not in {"report_id", "generated_at"}}
report_id = canonical_json_hash(unsigned)
applicable = pricing_decision != "conflict" and not blocking_diagnostics
```
- [ ] **Refactor/commit:** Run `python -m pytest tests/test_migration.py tests/test_cli.py -v`; expected: exit `0` with no failed/error tests. Demonstration command:

```bat
python scripts/migrate_existing.py --dry-run --projecta "D:\repos\Combinations\Projecta" --projectb "D:\repos\Projectb" --report "scratch\migration.json"
```

Expected: report only, no accounting state. Commit `feat: plan dry run historical migration`.

### Task 13: Apply Confirmed Migration Idempotently

**Files:** Modify `migration.py`, `cli.py`, migration/CLI tests.

**Interfaces:** `MigrationApplyResult(report_id: str, project_events: int, appended_revisions: int, unchanged_runs: int, baseline_action: Literal["initialized","unchanged"], diagnostics: tuple[Diagnostic, ...])`; `verify_source_manifest(report: MigrationReport, sources: Sequence[MigrationSource], index: Path) -> None`; `apply_migration(paths: AppPaths, report_path: Path, sources: Sequence[MigrationSource], *, confirmation_sha256: str, locks: StateLocks, clock: Callable[[], datetime]) -> MigrationApplyResult`.

- [ ] **Red:** Test wrong report hash, changed source, pricing conflict, source immutability, successful apply, second-run zero revisions, and no pricing-history creation. Representative:

```python
def test_second_apply_is_noop_and_never_creates_pricing_history(migration_case):
    first = migration_case.apply()
    second = migration_case.apply()
    assert first.appended_revisions > 0
    assert second.appended_revisions == 0
    assert not migration_case.paths.pricing_history.exists()
```

Run `python -m pytest tests/test_migration.py -k "apply or drift or idempotent" -v`. Expected: tests fail because `apply_migration` is absent.
- [ ] **Green:** Under lock require applicable report and exact report-file SHA-256, recompute manifest/report ID, then atomically initialize baseline if planned, append project discoveries, append ledger revisions. Never open source for write or touch pricing history.

```python
with locks.write:
    report = load_migration_report(report_path)
    if sha256(report_path.read_bytes()).hexdigest() != confirmation_sha256:
        raise ValueError("migration report confirmation mismatch")
    verify_source_manifest(report, sources, paths.machine_index)
    fresh = plan_migration(paths, sources, clock=clock)
    if fresh.report_id != report.report_id or not fresh.applicable:
        raise ValueError("migration inputs or plan changed")
    return append_planned_migration(paths, fresh, locks=locks, clock=clock)
```
- [ ] **Refactor/commit:** Run `python -m pytest tests/test_migration.py tests/test_cli.py -v`; expected: exit `0`, including second-run no-op and source-immutability cases. Document that rollback stops service and restores backed-up runtime/baseline; source needs no rollback. Commit `feat: apply confirmed migration idempotently`.

## Phase 5 — Snapshot and Dashboard

### Task 14: Build Approval-Aware Snapshots

**Files:** Create `src/agent_control/snapshot.py` and `tests/test_snapshot.py`.

**Interfaces:** `SnapshotQuery(project_ids: tuple[str, ...]=(), models: tuple[str, ...]=(), tasks: tuple[str, ...]=(), states: tuple[RunStatus, ...]=(), date_from: date | None=None, date_to: date | None=None, include_excluded: bool=False)`; `build_snapshot(runs: Sequence[RunRecord], projects: Mapping[str, Project], baseline: Mapping[str, RateSet | None], history: Sequence[PricingVersion], query: SnapshotQuery, *, now: datetime) -> dict[str, object]`.

- [ ] **Red:** Test machine/per-project today/month/all-time, approval immediate inclusion, pending/rejected/disabled exclusion, live/history, all filters, inclusive UTC dates, invalid range, coverage, all-unknown/mixed counters, day newest-first, category descending known cost with name tie.

```python
def test_pending_is_diagnostic_but_excluded_until_approval(runs, pending_projects):
    before = build_snapshot(runs, pending_projects, {}, [], SnapshotQuery(), now=NOW)
    diagnostic = build_snapshot(
        runs, pending_projects, {}, [],
        SnapshotQuery(include_excluded=True), now=NOW,
    )
    assert before["normal_totals"]["all_time"]["runs"] == 0
    assert diagnostic["runs"][0]["included_in_totals"] is False
    pending_projects[runs[0].project_id] = replace(
        pending_projects[runs[0].project_id], state="approved"
    )
    after = build_snapshot(runs, pending_projects, {}, [], SnapshotQuery(), now=NOW)
    assert after["normal_totals"]["all_time"]["runs"] == len(runs)
```

Run `python -m pytest tests/test_snapshot.py -v`. Expected: collection fails because `agent_control.snapshot` is absent.
- [ ] **Green:** Default runs/totals include approved only; diagnostic query includes excluded rows marked `included_in_totals=False`. Counter shape:

```python
{"known_total": sum(known) if known else None,
 "unknown_rows": len(values)-len(known),
 "partial": bool(known) and len(known) != len(values)}
```

Return generated time, normal totals, project totals, runs, live, spend day/project/model/task, and coverage. Hide raw absolute artifacts.
- [ ] **Refactor/commit:** Run snapshot tests; commit `feat: build project aware accounting snapshots`.

### Task 15: Add Hardened Loopback API and Mutations

**Files:** Create `dashboard.py`, `test_dashboard_api.py`; modify CLI tests.

**Interfaces:** `DashboardApp(paths: AppPaths, locks: StateLocks, synchronizer: Synchronizer, clock: Callable[[], datetime])`; `DashboardApp.snapshot(query: SnapshotQuery) -> dict[str, object]`; `DashboardApp.projects() -> list[dict[str, object]]`; `DashboardApp.diagnostics() -> list[dict[str, object]]`; `create_server(app: DashboardApp, *, port: int = 8787) -> ThreadingHTTPServer` hard-codes host; `make_handler(app: DashboardApp, csrf_token: str) -> type[BaseHTTPRequestHandler]`. GET data/projects/diagnostics/static; POST project approve/reject/disable/rename and pricing preview/history.

- [ ] **Red:** Test bind address; exact Host; same-origin mutation Origin; per-process CSRF; JSON media type; valid Content-Length; 16 KiB cap; strict finite object JSON; path normalization; 404/405+Allow; transition validation; preview absence; append; concurrent serialization; and GET `/api/data` invoking sync before snapshot. Representative:

```python
def test_data_endpoint_synchronizes_before_snapshot(api_server, synchronizer):
    status, payload = get_json(api_server, "/api/data")
    assert status == 200
    synchronizer.run.assert_called_once_with()
    assert "normal_totals" in payload
```

Run `python -m pytest tests/test_dashboard_api.py tests/test_cli.py -v`. Expected: collection fails because `agent_control.dashboard` is absent.
- [ ] **Green:** Generate `secrets.token_urlsafe(32)` once. Reject backslashes, encoded separators, dot/duplicate segments, unknown query keys, invalid dates. Errors are strict `{"error":{"code":"invalid_json","message":"Request body must be strict JSON"}}`. Mutation checks Origin `http://127.0.0.1:<port>`, CSRF header, exact allowed keys. Shared lock spans fresh load, validation, append. Every data snapshot first calls `synchronizer.run()`; a sync error is included as a sanitized diagnostic with the last readable snapshot when available, otherwise returns 500. Headers include no-store, nosniff, same-self CSP, no-referrer.

```python
def snapshot(self, query):
    sync_report = self.synchronizer.run()
    runs = highest_valid_revisions(load_ledger(self.paths.ledger)[0]).values()
    projects = load_projects(self.paths.projects)[0]
    return build_snapshot(tuple(runs), projects, load_baseline(self.paths.baseline_pricing),
                          load_pricing_history(self.paths.pricing_history)[0],
                          query, now=self.clock())
```
- [ ] **Refactor/commit:** `serve` accepts `--port`, no host. Run API/CLI tests; commit `feat: serve secure loopback accounting API`.

### Task 16: Build Separate Vanilla Dashboard

**Files:** Create `web/index.html`, `web/dashboard.css`, `web/dashboard.js`, `web/package.json`, `tests/js/dashboard.test.mjs`, and `tests/test_web_contract.py`; modify `src/agent_control/dashboard.py` and `tests/test_dashboard_api.py`.

**Interfaces:** JS exports `formatCounter(value: number | null)`, `formatCounterTotal(value)`, `formatCost(value, classification)`, `sortSpendGroups(kind, groups)`, `buildQuery(filters)`, `mutationHeaders(token)`, `candidateFingerprint(candidate)`, and `canAppendPricing(state)`.

- [ ] **Red:** Test no inline style/script; required summary/live/history/filter/spend/coverage/project/pricing controls; CSRF meta placeholder; zero vs em dash; partial ≥; upper-bound ≤; date/category order; preview invalidation; no unsafe `innerHTML` with API data.

```javascript
test("unknown and measured zero stay distinct", () => {
  assert.equal(formatCounter(null).text, "—");
  assert.equal(formatCounter(0).text, "0");
});
test("changed pricing candidate invalidates append", () => {
  assert.equal(canAppendPricing({
    previewFingerprint: "reviewed", candidateFingerprint: "changed"
  }), false);
});
```

Run `python -m pytest tests/test_web_contract.py -v` and `node --test tests/js/dashboard.test.mjs`. Expected: Python reports missing web files and Node reports `ERR_MODULE_NOT_FOUND`.
- [ ] **Green:** HTML has today/month/all-time, project/model/task/state/date filters, day/project/model/task spend, coverage, four project-state tabs, rename/state actions, active/history rates, four-rate editor, preview and separately enabled append. JS uses `textContent`/`createElement`, five-second refresh preserving last data on error, and sends JSON+CSRF. `web/package.json` is exactly `{"type":"module"}` and has no dependencies. Server substitutes only `__AGENT_CONTROL_CSRF__` in HTML and maps exactly three static paths.

```javascript
export function formatCounter(value) {
  return value === null
    ? {text: "—", title: "Not reported by the gateway"}
    : {text: Number(value).toLocaleString(), title: ""};
}
export function mutationHeaders(token) {
  return {"Content-Type": "application/json", "X-AgentControl-CSRF": token};
}
```
- [ ] **Refactor/commit:** Run:

```bat
python -m pytest tests/test_web_contract.py tests/test_dashboard_api.py -v
node --test tests/js/dashboard.test.mjs
```

Expected all pass. Commit `feat: add AgentControl dashboard workflows`.

## Phase 6 — Shared Engine

### Task 17: Own Schema-2 Engine Lifecycle

**Files:** Create `engine/delegate.py` and `tests/test_engine_contract.py`.

**Interfaces:** Copy installed engine read-only; set `HARNESS_VERSION=6`; `append_run_event(path: str, record: Mapping[str, object]) -> bool`; start/end builders return the exact schema-2 objects in specification §10.

- [ ] **Red:** Copy only into checkout:

```bat
copy /Y "C:\Users\operator\.claude\bin\delegate.py" "engine\delegate.py"
```

Add fake-gateway/temp-index tests for canonical root vs worktree cwd, direct/wrapper equivalence, stable run ID, PID, valid terminal statuses, and index failure warning/nonblocking work:

```python
def test_schema2_lifecycle_preserves_root_and_cwd(engine_run):
    start, end = engine_run.index_records
    assert start["schema"] == end["schema"] == 2
    assert start["run_id"] == end["run_id"]
    assert start["root"] == str(engine_run.canonical_root.resolve())
    assert start["cwd"] == str(engine_run.worktree.resolve())
    assert start["engine_pid"] > 0
    assert end["status"] == "complete"
```

Run `python -m pytest tests/test_engine_contract.py -k "lifecycle or root or nonblocking" -v`. Expected: copied engine fails schema/process-identity assertions.
- [ ] **Green:** Start contains every specified field and `engine_pid=os.getpid()`; end joins same ID and maps complete/failed/interrupted. Append directly to env-overridden/default index, flush/fsync; catch `OSError`, print exception type only, continue without HTTP.

```python
def append_run_event(path, record):
    try:
        with open(path, "a", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(record, sort_keys=True, separators=(",", ":"),
                                    allow_nan=False) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        return True
    except OSError as exc:
        print(f"WARNING: run index append failed: {type(exc).__name__}",
              file=sys.stderr)
        return False
```
- [ ] **Refactor/commit:** Run focused engine tests; verify installed checksum unchanged; commit `feat: own schema two delegate lifecycle`.

### Task 18: Guarantee Exactly-Once Sanitized Usage

**Files:** Modify `engine/delegate.py`, `src/agent_control/usage.py`, `tests/test_engine_contract.py`, and `tests/test_usage.py`.

**Interfaces:** Engine `sanitize_usage(raw: object) -> dict[str, object] | None`; `append_usage_record(path: str, *, iteration: int, phase: Literal["loop","wrapup"], model: str, timestamp: str, raw_usage: object) -> bool`; one shared successful-response journaling point. Row fields exactly iter, phase, model, ts, sent, recv, cache_read, cache_write, raw.

- [ ] **Red:** Fake gateway sequence tests one loop + wrap-up yields exactly two rows; failed attempts then success still two; journal-write error does not retry response; recursive forbidden-key scan; source has no AgentControl HTTP.

```python
def test_failed_attempt_is_not_journalled(engine_with_replies):
    run = engine_with_replies([
        transport_failure(), successful_loop(), successful_wrapup()
    ])
    rows = read_objects(run.usage_journal)
    assert [row["phase"] for row in rows] == ["loop", "wrapup"]
    assert len(rows) == 2
```

Run `python -m pytest tests/test_engine_contract.py tests/test_usage.py -k "usage or retry or offline" -v`. Expected: at least the exactly-once or sanitized-shape assertion fails.
- [ ] **Green:** Allowlist OpenAI/Anthropic usage keys and nested prompt details only. Journal after successful validated response and before returning it. Both loop/wrap-up call the same function; failed attempts never call it. Write failure warns and returns successful response.

```python
response = call_gateway_with_retries(request)
validated = validate_gateway_response(response)
append_usage_record(usage_path, iteration=iteration, phase=phase,
                    model=model, timestamp=utc_now(),
                    raw_usage=validated.get("usage"))
return validated
```
- [ ] **Refactor/commit:** Run engine+usage suites; expected pass and no leakage. Commit `feat: journal successful response usage once`.

### Task 19: Add Atomic Engine Deployment and Rollback

**Files:** Create `scripts/deploy_engine.py` and `tests/test_deploy_engine.py`; modify `docs/operator-guide.md`.

**Interfaces:** `deploy(source: Path, destination: Path, *, apply: bool, test_command: Sequence[str]) -> DeploymentResult`; `rollback(destination: Path, backup: Path) -> DeploymentResult`; CLI default dry run, `--apply`, `--rollback`; backup sibling includes UTC/current hash.

- [ ] **Red:** Test dry-run immutability, failed contract gate no backup, backup/install, exact checksum+harness verification, forced verification rollback, explicit rollback validation.

```python
def test_post_replace_verification_failure_restores_original(deployment_case):
    original = deployment_case.destination.read_bytes()
    deployment_case.verify.side_effect = DeploymentError("bad checksum")
    with pytest.raises(DeploymentError):
        deployment_case.apply()
    assert deployment_case.destination.read_bytes() == original
```

Run `python -m pytest tests/test_deploy_engine.py -v`. Expected: import fails because `scripts.deploy_engine` is absent.
- [ ] **Green:** Apply runs `python -m pytest tests/test_engine_contract.py -q`, writes/fsyncs unique sibling, `os.replace`, verifies SHA-256 and harness 6. Failure atomically restores backup. Rollback accepts only regular sibling `delegate.py.backup-*`, backs up current, restores and verifies. Never read credentials or print source.

```python
with temporary.open("wb") as handle:
    handle.write(payload)
    handle.flush()
    os.fsync(handle.fileno())
os.replace(temporary, destination)
if sha256(destination.read_bytes()).digest() != sha256(payload).digest():
    atomic_restore(backup, destination)
    raise DeploymentError("installed checksum mismatch; backup restored")
```
- [ ] **Refactor/commit:** Run deployment+engine tests against temp paths; expected pass. Commit `feat: deploy shared engine atomically`.

## Phase 7 — Operations and Cutover

### Task 20: Complete CLI, Operator Guide, and Gated Cutover

**Files:** Modify `src/agent_control/cli.py`, `tests/test_cli.py`, `README.md`, `AGENTS.md`, and `docs/operator-guide.md`; create `docs/cutover-checklist.md` and `tests/test_operator_docs.py`; modify `config/pricing.json` only after confirmed conflict-free migration initialization.

**Interfaces:** Final commands: `sync`; `serve --port`; `projects list|approve|reject|disable|rename`; `pricing show|preview|append`; `migrate --dry-run|--apply`. Pricing append requires preview hash; migration apply requires report path/hash.

- [ ] **Red:** Test injected CLI paths, strict JSON/errors, mutation confirmations, and documentation phrases covering every cutover item, rollback, loopback, pricing-history absence, sensitive scan, and deferred cleanup.

```python
def test_cutover_document_covers_live_gates(repo_root):
    text = (repo_root / "docs/cutover-checklist.md").read_text().casefold()
    for phrase in ("dry-run migration", "input backups", "projecta approval",
                   "projectb approval", "direct run", "wrapper run",
                   "pricing_history.jsonl", "repo-local dashboard removal is deferred"):
        assert phrase in text
```

Run `python -m pytest tests/test_cli.py tests/test_operator_docs.py -v`; expected: failure because final CLI handlers/checklist are absent.
- [ ] **Green:** CLI handlers call package services directly. Documentation explicitly separates temporary tests from approved live actions and includes the sequence below.

```python
def _run_sync(args, paths, locks):
    report = Synchronizer(paths, locks, utc_now, PsutilProcessProbe()).run()
    print(json.dumps(asdict(report), sort_keys=True, allow_nan=False))
    return 0
```
- [ ] **Automated pre-cutover:** Run:

```bat
git status --short
python -m pytest -v
node --test tests/js/dashboard.test.mjs
python scripts/deploy_engine.py
python scripts/migrate_existing.py --dry-run --projecta "D:\repos\Combinations\Projecta" --projectb "D:\repos\Projectb" --report "scratch\migration.json"
certutil -hashfile "scratch\migration.json" SHA256
git diff --check
```

Expected: tests pass; deploy/migration are nonmutating; migration applicable/conflict-free; diff clean.

- [ ] **Approved backup/apply:** Stop AgentControl. Back up installed engine, machine index, existing runtime directory, reviewed report, Projecta/Projectb ledgers/registries/pricing, and tracked baseline to a timestamped user backup. Apply with exact report hash, rerun dry-run, and require zero planned revisions. Verify:

```bat
if exist "C:\Users\operator\.claude\agent-control\pricing_history.jsonl" (exit /b 1) else (echo pricing history correctly absent)
```

Rollback: stop listener, preserve failed runtime for diagnosis, restore runtime/baseline backups; source artifacts were read-only.

- [ ] **Approved project/parity checks:** Approve Projecta and Projectb UUIDs, sync, and compare logical counts plus recoverable sent/returned/cache totals with highest-valid source revisions. Every difference must be a report diagnostic, never a filename/label merge. Verify approval changes totals without restart.

- [ ] **Approved engine/run checks:** Run engine/deployment tests, dry-run deploy, apply deploy, record backup/checksums. Perform one harmless direct run and one existing-wrapper run across the two approved projects. Verify schema 2, canonical root/cwd, process identity, one journal row per success, project identity, and two syncs append no duplicate. Roll back with:

```bat
python scripts/deploy_engine.py --rollback "<exact backup path printed by apply>"
```

- [ ] **Approved pending/offline/restart checks:** With service stopped, run one harmless temporary-root delegation; restart/sync; verify pending diagnostic and unchanged totals; approve to prove immediate inclusion, then reject. Restart and run two syncs; second appends zero.

- [ ] **Approved production/security checks:** Run full AgentControl and affected Projecta tests plus Projectb self-tests using absolute paths, recording external `git status --short` before/after. Start exactly one production listener:

```bat
agent-control serve --port 8787
netstat -ano | findstr "127.0.0.1:8787"
```

Require one intended LISTENING PID. Scan central JSON/JSONL key names only for prompt, messages, assistant, tool_result, credential, authorization, response_body, content; fail without printing values. Confirm no pricing history unless a real edit was separately confirmed.

- [ ] **Optional scheduled-task gate:** Only after documented manual stability and separate approval, create one current-user task for `python -m agent_control.cli serve --port 8787` with this checkout as working directory. Check no listener/task conflict before and exactly one listener after.

- [ ] **Refactor/commit:** Run full Python/JS/docs tests. Commit CLI/docs/checklist and baseline only if migration initialized it:

```bat
git add src/agent_control/cli.py tests README.md AGENTS.md docs
git commit -m "docs: complete AgentControl operator cutover"
```

Do not commit runtime files, backups, live reports, or external changes. Repo-local dashboard removal remains a later separately reviewed cleanup.

## Unit/Live Boundary

| Temporary and safe | Explicit approval required |
|---|---|
| Tests under `tmp_path`; fake gateway/processes; ephemeral loopback ports | Writes under live `~\.claude\agent-control` |
| Fixture migration and read-only live dry run | Historical apply and live project state changes |
| Deployment tests against temporary destinations | Installed engine replace/rollback |
| Read-only schema/key/type/count/checksum inspection | Direct/wrapper delegated proof runs |
| Existing Projecta/Projectb tests | Production listener or scheduled task |
| AgentControl source edits | Any Projecta/Projectb modification/removal |

## Requirements-to-Task Coverage Matrix

| Requirement | Tasks |
|---|---|
| Bootstrap, Python/dependency limits, injectable paths | 1–2 |
| OpenAI/Anthropic, nested cache, zero/null, invalid rejection, precedence | 3–4 |
| Revision ledger, durable append, highest-valid, no-op | 2, 5, 10 |
| Effective pricing, four classifications, preview/history absence | 6, 15–16 |
| Canonical roots, stable UUID, event states/reapproval/rename/concurrency | 7, 15–16 |
| Schema index, run-ID join, replay/checkpoint/rescan/path/inaccessible | 8, 10 |
| PID/command/create-time and conservative legacy liveness | 9 |
| Pending discovery/exclusion and approval without restart | 7, 10, 14–16 |
| Historical sources, collision IDs, precedence/conflicts/reports | 11–12 |
| Dry-run, explicit apply, source drift, idempotence, migration rollback | 12–13, 20 |
| Machine/project periods, live/history, filters, spend/order/coverage | 14, 16 |
| Project/pricing UI and separate HTML/CSS/JS | 15–16 |
| Loopback, Host/Origin/CSRF/JSON/body/method/path/locking | 2, 7, 15 |
| Engine schema 2, root/cwd/PID, direct/wrapper | 17 |
| Exactly-once success usage, retry safety, no leakage/offline dependency | 18 |
| Engine backup/atomic/checksum/rollback | 19–20 |
| Full tests, parity, one listener, temporary pending root | 20 |
| No accidental pricing history or sensitive central state | 4, 6, 11, 14–16, 18, 20 |
| Preserve repo policy and defer local cleanup | Global constraints, 20 |
| YAGNI release-1 exclusions | Global constraints, 1, 15, 20 |

## Final Verification Before Completion

```bat
git status --short
python -m pytest -v
node --test tests/js/dashboard.test.mjs
python scripts/deploy_engine.py
python scripts/migrate_existing.py --dry-run --projecta "D:\repos\Combinations\Projecta" --projectb "D:\repos\Projectb" --report "scratch\final-review.json"
git diff --check
```

Expected: only understood ignored scratch evidence, all tests pass, deployment/migration remain dry-run, and no whitespace errors. Live migration, engine deployment, delegated runs, production listener, and scheduled task remain operator-gated.

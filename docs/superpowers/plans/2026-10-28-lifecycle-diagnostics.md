# Lifecycle Diagnostics Implementation Plan

> Execute inline with test-driven development. Built-in subagents and model-tool
> dispatch are not used. The operator approved this design, matching spec/plan,
> implementation, and deployment after passing tests.

**Goal:** Retain content-free evidence for failed and interrupted harness runs.

**Architecture:** A focused shared diagnostic module writes bounded, allowlisted,
per-observer journals. Existing engine, wrapper, watcher, and termination paths
emit observations without changing accounting or process-control semantics.

**Tech Stack:** Python standard library, pytest, JavaScript plugin with Node tests.

## Global constraints

- Spec: `../specs/2026-10-28-lifecycle-diagnostics-design.md`.
- `HARNESS_VERSION = 6`, `INDEX_SCHEMA = 2`; startup receipts stay two rows.
- No content, raw exceptions/stderr, credentials, or command-line logging.
- Tests inject temporary paths. No writes to live accounting state.
- No automatic commits; the operator subsequently authorized the final commit.
  No permanent service, scheduled task, or production listener restart.
- A missing terminal record remains missing; an observed exit never invents cause.

## Task 1: Journal and engine evidence

Files: new `engine/harness_diagnostics.py`, `tests/test_lifecycle_diagnostics.py`;
modify `engine/delegate.py` and deployment bundle handling/tests.

- [x] Write tests for `Journal(path, observer, **identity).emit(event, **fields)`:
  allowlist rejection, zero/null preservation, exception metadata without text,
  durable append and failed writes, bounded identity-matched reads.
- [x] Run `python -m pytest tests/test_lifecycle_diagnostics.py -q`; confirm RED.
- [x] Implement the module, then add failing engine tests for request milestones,
  timeout/HTTP/disconnect classification, exception, signal, and final status.
- [x] Integrate without changing payloads, retries, index or receipt records.
- [x] Run focused journal, engine contract, streaming and receipt tests to GREEN.

## Task 2: Wrapper evidence and exact watcher diagnosis

Files: `engine/ask-model.py`, `engine/fanout.py`, `engine/agent_await.py`;
new wrapper/watcher regression tests and synthetic subprocess cases.

- [x] Assert child-start/exit events preserve zero and Windows status values;
  timeout/deadline intent precedes the existing kill operation.
- [x] Assert watcher prints only bounded matching diagnostic fields when failed,
  dead or unverified, with no fabricated terminal row or usage.
- [x] Observe RED, implement the narrow wrapper and watcher integrations, then
  run all launch/oneshot/fanout tests to GREEN.

## Task 3: Termination-owner and Kilo observations

Files: `src/agent_control/process_control.py`, process-control tests;
new `tools/kilo/lifecycle-diagnostics.js` and Node plugin tests.

- [x] Assert a durable operator stop-intent event precedes terminate/kill and
  remains distinct from its outcome. Run RED, implement, run GREEN.
- [x] Test Kilo hook launch/stop/status/disposal allowlists with synthetic inputs;
  unknown events and all content-bearing fields must be discarded.
- [x] Implement plugin using documented hooks. Do not claim a pre-kill hook for
  automatic teardown. Copy the verified plugin for next startup only.

## Task 4: Forced exits and deployment

- [x] Run synthetic subprocess tests for exception, broken pipe, hard exit, and
  forced tree termination. Last-phase evidence must survive; no cause invented.
- [x] Run `python -m pytest -q` and Node plugin tests; inspect `git diff --check`.
- [x] Run `python scripts/deploy_engine.py` preview, then the approved
  `python scripts/deploy_engine.py --apply` with expanded regression gates.
- [x] Check installed checksums and execute installed synthetic subprocess tests
  using temporary indexes/receipts and a synthetic loopback gateway.
- [x] Report tested coverage and the remaining external/all-observer kill limit.

## Execution and verification

- Final full Python suite: 1,421 passed. Kilo hook/privacy tests: 6 passed.
- Installed engine synthetic loopback/exit tests: 6 passed. Installed Kilo
  entry/helper tests: 6 passed. All 9 engine and both plugin artifact hashes match
  their repository sources.
- Shared-engine deployment completed using the existing rollback-backup procedure;
  backup set timestamp: `20261028T135419Z`.
- Independent review corrections: fail-open bootstrap/write handling, separate
  child versus engine PID, pre-spawn visibility, denied stop results, restricted
  Kilo launch ancestry, and non-spoofable runtime-header parsing.
- Kilo plugin files were newly installed for the next normal Kilo startup.
  No active session or production listener was restarted. AgentControl's new
  process-control code takes effect on its next ordinary application start.
- Real Windows engine and process-tree kills retained flushed evidence. An
  all-observer hard kill still cannot name an unrecorded initiator. Automatic
  Kilo pre-kill attribution is not claimed: exposed disposal hooks provide only
  observations, and no installed-runtime teardown test proved a kill cause.
- The operator requested the final source commit after verification. No OS audit
  change, daemon, or scheduled task was created.

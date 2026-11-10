# Owner Attribution Implementation Plan

> Execute inline with test-driven development. No built-in subagents, live
> runtime replacement, active-job restart, automatic commit or model-tool loops.

**Goal:** Preserve termination-owner evidence and make missing activation visible.

**Architecture:** A pinned source patch instruments the Kilo owner; bounded
capability readers expose actual coverage. A retained Windows handle in the
existing AgentControl observer independently preserves actual exit status.

**Tech Stack:** Standard-library Python, Node/Bun ESM, pytest, node:test.

## Global constraints

- Read `../specs/2026-10-28-owner-attribution-design.md` before behavior changes.
- Target active backend 7.8.18 at the exact recorded commit, not CLI 7.4.20.
- Preserve schema-2 accounting, two-row startup receipts, and stop semantics.
- No prompts, outputs, credentials, raw errors, command lines or complete sidecars.
- All tests inject temporary paths; no live accounting writes.
- No installed-runtime replacement or live activation without separate approval.

## 1. Process identity and independent observation

Files: `src/harness_process_identity.py`,
`src/agent_control/exit_observer.py`, `src/agent_control/liveness.py`,
`pyproject.toml`, and synthetic regression tests.

API: `ProcessHandle.open(pid, expected_creation_time=None)` returns a verified
handle or unavailable; `birth` is an exact platform token, `poll()` distinguishes
running/exited/unknown with nullable status, and `close()` releases the handle.

- [x] Write failing real-Windows synthetic-child tests and mismatch/failure tests.
- [x] Implement identity and bounded retained handles, then run focused tests.
- [x] Integrate only after existing `classify_liveness` identity verification;
  diagnostic failure must not alter liveness or operator-control decisions.

## 2. Activation evidence and exact container correlation

Files: existing Kilo plugin/core, new shared process-identity ESM helper,
`engine/harness_diagnostics.py`, `engine/delegate.py`, `engine/agent_await.py`,
deployment dependency handling and tests.

APIs: versioned `plugin_started/plugin_heartbeat/plugin_disposed` and
`owner_started/owner_heartbeat` metadata. Coverage reader returns
active/stale/unavailable/unverified; never infer pre-kill support from plugin
presence. Engine evidence includes validated inherited owner/container IDs.

- [x] Write RED privacy, stale-marker, PID/birth mismatch and inheritance tests.
- [x] Implement allowlisted records, bounded unref'ed heartbeats and readers.
- [x] Show distinct coverage in the watcher, including missing owner support.
- [x] Extend deployment checks but do not apply a shared installation unapproved.

## 3. Pinned Kilo owner patchkit

Files: `tools/kilo/owner-patchkit/` source module, declarations, manifest and
preparation script; Python/Node tests with synthetic inputs.

APIs: async-local `withCause(reason, fn)`; journal `stop(info, action)` preserves
intent/result independently; `exited(info, {code, signal})` records actual child
callbacks only. Preparation validates all upstream hashes/anchors before writing
a fresh staging output; source and installed paths are never overwritten.

- [x] Write RED concurrency, intent-before-kill, unknown/zero, fail-open and
  source-drift tests. Run Node/Python tests to confirm the missing behavior.
- [x] Implement fixed-schema journaling and exact-version patch transformations.
- [x] Cover stop/restart/stopSession, instance finalizer, runtime shutdown and
  rollback separately; never fabricate an exit from persistent polling.
- [x] Verify against pinned sources in temporary storage. Clearly distinguish
  dependency-light patch/helper validation from a full-runtime execution test.

## 4. Integration, review and operator handoff

- [x] Run independent read-only review and fix concrete findings with tests.
- [x] Run full Python, Node and Windows-gated synthetic-process regressions.
- [x] Recheck worktree, whitespace, privacy and staging-only behavior.
- [x] Report exact tested coverage and the separate build/activation approval
  needed before any production owner-attribution claim.

## Verification record

- Full Python regression including the real isolated-backend plugin canary:
  **1,455 passed**, no skips in that explicit canary run.
- Node journal/plugin tests: **19 passed**. Bun native/pinned-function tests:
  **10 passed**. The native kill targets were only self-created synthetic children.
- The real backend canary was the existing unmodified 7.8.18 executable with an
  isolated temporary home, databases, workspace, authenticated loopback port and
  the source plugin. It verified plugin initialization/disposal and rejected an
  owner-pre-kill claim. It did not load a rebuilt owner-instrumented backend.
- Independent review corrections: non-blocking stops, bounded optional identity,
  explicit finalizer causes, retained unknown handles, live-versus-retained-dead
  activation checks, held spawn handles, native actor/target matching, source
  fingerprint registration and staging-root allowlisting. Non-process APIs keep
  null exit status instead of inventing zero.
- Exact source overlay: temporary `owner-attribution-7.8.18-20261028-r3`, prepared
  from verified pinned originals. Source fingerprint:
  `sha256:28d0f872d419f9f8ebccb60d50c9f8c95151fbc05a01be51d4181ad78fcad2d7`.
- At the initial source-only handoff, the full compiled-backend build was still
  outstanding. The operator subsequently authorized and completed the isolated
  rebuild/integration stage recorded below. Production activation remains a
  separate approval gate. Autonomous runner cleanup and UI-gesture attribution
  are not advertised as supported.
- Shared-engine deployment was previewed only. No installed files, running jobs,
  production listener, OS audit policy or Git commit was changed.

## Full isolated rebuild and integration: completed

Authorized on 2026-10-28 at 20:45 UTC. No production rollout was authorized.

### Reproducible build inputs

- Full upstream checkout at `b81761530650f8ec7b071441f4f54d2514f1a244`, tag
  `v7.8.18`, under temporary `kilo/kilo-owner-full-7.8.18-20261028`.
- Checksum-verified upstream Bun fork **1.4.4**, revision
  `38afee6c84fcf48c0edb2466aa4b27d287a59c30`; no global compiler replacement.
- Applied the registered source patch with `git apply --check` before applying.
  Prepared helper bytes match the maintained repository sources.
- Dependencies installed with `--frozen-lockfile --ignore-scripts` into the
  isolated checkout. `isolated.py` redirects HOME, XDG state/config/data/cache,
  TEMP and Bun caches, strips inherited credentials and release flags, and
  supplies an explicit non-release build channel/version.
- Upstream build: `run script/build.ts --single --skip-install` from
  `packages/opencode`. Console bundling, native executable compilation, model
  provider smoke tests and packaged sandbox-worker smoke test completed.
- Upstream `run typecheck` completed without errors.

### Executable and verification

- Version: `7.8.18-owner-attribution`.
- Executable:
  `C:/Users/operator/AppData/Local/Temp/kilo/kilo-owner-full-7.8.18-20261028/packages/opencode/dist/@kilocode/cli-windows-x64/bin/kilo.exe`.
- Size: **316,294,144 bytes**.
- Executable SHA-256:
  `005c583ad80075397f9b3afae2a1bdb998d114b5ebeeeae4c90e59e5f81dcfc0`.
- Preserve the adjacent `kilo-sandbox-mutation-worker.js` when evaluating this
  artifact; this record does not authorize copying either file into production.

`tests/test_kilo_rebuilt_runtime.py` runs the **real executable and runtime graph**
with authenticated loopback HTTP, private temporary data/config/workspaces and a
deterministic local model-protocol fixture. It does not call a remote model or
create user-visible Agent Manager sessions. Nine executable-level cases passed:

1. Stop/restart, exact inherited owner/container/fingerprint and native birth
   matching, independent exit observation, and the final watcher attribution.
2. API session-stop cleanup.
3. Session deletion cleanup.
4. Instance disposal cleanup.
5. Readiness deadline: the worker remains running; no kill intent is invented.
6. Natural CLI exit: instance disposal stops the worker before the later global
   runtime-shutdown boundary; the actual ordering and reasons are asserted.
7. Natural measured exit status zero, without an earlier stop request.
8. External synthetic-worker termination: measured status with no fabricated
   owner kill intent.
9. Persistent-worker stop: null control-file API exit status remains distinct
   from actual runner/worker process exit statuses.

The actual full-Effect upstream Windows rollback regression also passed with
fault injection: **1 test, 5 assertions**; it was not the earlier extracted-function
test. Its other test cases were filtered, not claimed as executed coverage.

Final regression: **1,465 Python tests passed** with both the stock-plugin control
and rebuilt-executable suites enabled; **19 Node tests** and **10 lower-level Bun
tests** passed. Upstream dependency-patch/platform and frontend chunk-size warnings
were non-fatal; no integrity or verification check was disabled.

No installed backend/helper/plugin was replaced, no production listener or active
job was restarted, and no commit/push, OS audit change or persistent service was
performed. Only a separately approved live activation/rollout remains deferred.

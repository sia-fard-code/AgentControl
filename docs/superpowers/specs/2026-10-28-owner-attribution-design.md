# Owner-side termination attribution

Status: implementation approved by the operator after the five-point follow-up
recommendation. This approval does not authorize replacing an installed Kilo
binary, restarting active jobs/listeners, committing, or changing OS audit policy.

## Boundaries established from source

The active VS Code backend is Kilo 7.8.18, upstream commit
`b81761530650f8ec7b071441f4f54d2514f1a244`. The standalone CLI is 7.4.20 and is
not the target. The 7.8.18 background-process manager terminates session processes
from its instance finalizer and forgets their handles. On Windows it invokes
`taskkill /F /T`. Rollback has separate kill paths. Shutdown disposes the manager.
SDK plugin hooks cannot prove these pre-kill decisions.

## Implementation

1. Maintain a version/hash-checked, source-only Kilo patchkit in `tools/kilo/`.
   It prepares a separate staging directory and never replaces installed files.
   The patch targets manager stop/restart/session cleanup, silent instance
   disposal, shutdown and launch rollback. It appends fsynced stop intent before
   the existing action, then a separate returned/failed result. Actual child-exit
   callbacks retain real numeric status; disappearance-only probes retain null.
   No command, output, environment/token, or raw exception enters the journal.
   Cause context is async-local, not mutable global state shared by stops.
2. The runtime journal advertises versioned capabilities and heartbeats. The
   existing plugin independently advertises its narrower observation-only hooks.
   Installation alone never counts as activation. Readers verify live owner PID
   and process birth identity and reject stale, missing or mismatched markers.
3. Inherit the existing `KILO_BACKGROUND_PROCESS_ID` and the patched runtime's
   owner UUID into engine/wrapper diagnostics. Correlate by owner UUID/container
   ID and exact birth identity. PID/time-only correlation remains explicitly
   heuristic and cannot be reported as verified ownership.
4. A small standard-library process-identity module exposes a retained Windows
   query/synchronize handle. AgentControl's existing read-only process probe
   attaches only after its current engine verification succeeds. It retains
   handles across polls and records actual observed exit status outside the
   worker's kill tree. No daemon or additional production listener is introduced.
   Reliable later exit-status collection requires retaining a handle before
   disappearance. Unknown queries retain the bounded handle for later retries.
5. Failure output explicitly distinguishes engine evidence, plugin activation,
   owner pre-kill coverage, a recorded intent, an action result, and an observed
   exit. No history, accounting status, model selection or termination semantics
   are changed. Persistence remains an operator choice, not a blanket default.

## Validation and activation gate

Tests use synthetic text and temporary paths. Exercise privacy, I/O failure,
stale activation, PID reuse, null versus zero, concurrent stop causes, and real
synthetic child exits/hard kills. Check the patch against exact pinned upstream
sources in temporary storage, never live user files. Report separately which
tests exercise helpers, pinned patch integration, and a full Kilo runtime.

Do not claim production owner attribution until a separately approved build and
safe activation pass a real launch/stop/disposal canary. Autonomous runner/job
cleanup not covered by the patch is advertised as unsupported. An arbitrary OS
or power kill may remain unattributed even when all available evidence survives.

## Implemented safety details

- No stop or rollback waits for diagnostic initialization or a birth probe.
  Optional identity probes are bounded to 250 ms and are not on the kill path.
- Finalizers explicitly select instance disposal unless executing a real shutdown;
  they do not inherit an old resource creator's stop reason. OS-result callbacks
  retain the original operation context across later asynchronous callbacks.
- Spawned Python child identity comes from its held process handle, not a PID
  lookup. Kilo spawn identity additionally checks spawn-time consistency and a
  live ChildProcess guard. Adopted-process native birth remains unsupported/null.
- The maintained source implementation has a SHA-256 fingerprint registered in
  the manifest, embedded in the prepared source, and checked by coverage readers.
  This is a source fingerprint, **not a binary attestation or full-runtime canary**.
- Historical attribution requires exact owner/container IDs, matching actor and
  target birth identities, and a recognized patch fingerprint. A current live
  owner is required for activation, not for retaining legitimate historical facts.
- Source preparation only writes a fresh directory beneath the designated
  temporary `kilo` staging root. Hash or anchor drift rejects the entire overlay
  before writes. Windows checkout line endings are normalized before validation.

## Verification and handoff

- 1,455 Python tests passed, including an isolated real 7.8.18 backend plugin
  startup/disposal canary with private temporary home, databases and loopback auth.
- 19 Node tests passed for plugin/journal privacy, activation, concurrent causes,
  unavailable storage and non-blocking termination.
- 10 Bun tests passed: native birth identity and nine tests exercising the exact
  pinned/transformed source functions against disposable Windows children. The
  event bus/framework context was stubbed; this was not a full rebuilt backend.
- No installed runtime, shared engine, plugin installation, production listener,
  active job, OS policy or Git commit was changed by this follow-up.

Prepared overlay and unified patch for this verification:
`C:/Users/operator/AppData/Local/Temp/kilo/owner-attribution-7.8.18-20261028-r3/`.
The authoritative portable inputs remain in `tools/kilo/owner-patchkit/`.

The operator subsequently authorized the full isolated rebuild and integration
test. It is complete: the pinned backend was compiled with the upstream Bun fork,
type-checked, and nine real-executable lifecycle cases passed. The implementation
plan contains the executable checksum and detailed verification record. The final
Python suite passed 1,465 tests with the rebuilt backend enabled.

Before production use, separately approve a safe activation window. Deploy the
matching shared helper/registry and all three plugin files together. Restart
AgentControl only in an approved window to enable its retained observer. The
existing running backend is not retroactively instrumented by source changes;
this isolated build did not replace it.

Readonly live check:
`python tools/kilo/owner-patchkit/status.py --owner-pid <backend-pid>`.
Exit code 2 means owner pre-kill coverage is not verified active; it does not
authorize a restart or imply the model request failed.

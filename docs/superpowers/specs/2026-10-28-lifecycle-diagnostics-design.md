# Durable lifecycle diagnostics

Date: 2026-10-28. Status: operator approved in this session, including deployment
after tests pass. The operator subsequently authorized committing the completed
changes and leaving a clean worktree. No production-listener restart, OS audit
change, or patch to Kilo's installed executable is authorized.

## Goal and evidence

A registered consultation disappeared while streaming. Its session-scoped
launcher and watcher were removed without a terminal index row. No retained
evidence establishes timeout, prompt size, or the termination initiator.
Future inspections must report durable observations instead of reconstructing
causes from transcript silence or a successful retry.

## Design

Keep harness version 6, index schema 2, two-row startup receipts, usage semantics,
and append-only accounting unchanged. Add per-observer append-only diagnostic
JSONL beside the transcript or launch receipt. This is diagnostic evidence, not
an authoritative completion or usage ledger.

The engine records registration, request attempts, configured timeout, HTTP
status, stream opening and throttled progress, transport failures, catchable
signals, unexpected exceptions, broken output pipes, and terminal status.
Record exception class and engine-owned source basename/line, never exception
messages or source excerpts. Critical events are flushed and fsynced.

Wrappers record child creation and observed numeric return codes. Preserve zero
and unknown separately and retain Windows unsigned exit status. A spawned child
PID is not labelled as the engine PID: Windows shims can make them different.
Exact launch IDs retain pre-spawn exceptions even without any engine receipt.
Diagnostics loading and writing fail open, with fixed content-free warnings.
Fanout and
AgentControl record stop intent before their existing termination operation;
intent is not proof the target exited. Do not add new termination behavior.

The exact-launch watcher displays matching diagnostic evidence for a failed or
missing terminal record. Validate run/launch/PID identities; reject unrelated,
oversized, malformed, or partially written rows. Never synthesize terminal
index records, usage, death times, or causal explanations.

Kilo integration is a small separately testable local plugin using documented
tool-before/tool-after and event hooks. Persist only allowlisted metadata for
background-process launch, explicit stop/restart intent, observed status, and
session/instance disposal notifications. The installed SDK does not expose a
documented automatic pre-kill reason hook. Disposal notifications are observations,
not proof that disposal killed a particular worker. Do not claim automatic
teardown attribution without a test showing the relevant evidence survives.
Load the plugin on subsequent Kilo startup; do not restart active sessions.
The watcher reads at most 32 recent Kilo journals, each bounded to 256 KiB and
128 valid rows. Correlation requires a start/restart header PID and a launch
timestamp within 60 seconds, using engine/wrapper launch ancestry only, not
dashboard-control PIDs. It labels results as container observations/intent, never
as proven engine-exit causes. Older journals remain available for explicit reads.

## Artifact locations

- Engine: `<transcript>.engine.diagnostics.jsonl`.
- Startup: `<receipt>.engine-startup.diagnostics.jsonl`.
- Wrappers: `<receipt>.ask.diagnostics.jsonl`,
  `<transcript>.fanout.diagnostics.jsonl`, or `<receipt>.fanout.diagnostics.jsonl`.
- Operator kill: `<transcript>.control.diagnostics.jsonl`.
- Kilo: `~/.local/share/kilo/lifecycle/<date>-<pid>-<instance>.jsonl`.
- Plugin entry/helper: `~/.config/kilo/plugin/lifecycle-diagnostics.js` and
  `lifecycle-diagnostics-core.mjs`; only the `.js` entry is auto-discovered.

`agent_await.py` automatically displays matching evidence on failed/dead/early
launches, without adding raw sidecar content to any API or accounting record.

## Privacy and failure handling

Use fixed event, observer, phase, reason, status and route enums; UUID/process/run
identities; timestamps; numeric counts, deadlines, HTTP/OS statuses. No prompts,
assistant content, tool results, request/response bodies, URLs, credentials,
environment dumps, command lines, exception text, raw stderr, or tracebacks.
Diagnostics must not replace the original outcome when logging fails. Emit at
most a fixed, content-free warning. Readers bound their reads and ignore an
incomplete trailing append. Every test uses temporary paths and synthetic text.

## Observable boundary

Python exit handlers do not run after Windows TerminateProcess or POSIX SIGKILL.
If all observers die together, retain the last flushed phase and explicitly say
the initiator is unknown. No new daemon, detached supervisor, scheduled task,
automatic restart, native/prompt model-tool dispatch, or OS policy change.

## Acceptance

Exercise real synthetic subprocesses and fake transports for normal completion,
unexpected exception, supported signal, broken output, HTTP timeout, client
timeout, disconnect, wrapper-observed hard exit, deadline intent, and tree kill.
Check receipt/index compatibility, PID/launch mismatch, truncated journal, null
versus zero, privacy sentinels, and failure of the diagnostic storage itself.
Run the full Python suite and plugin tests, then the operator-approved deployment
with rollback backups and an installed synthetic smoke test. No live model or
structural-model mutation is required for verification.

# AgentControl

AgentControl is a local, machine-wide monitoring and accounting control plane for delegated-agent runs. It consumes the current Windows user's append-only run index, preserves canonical project identity, and includes only operator-approved projects in normal token totals.

Release 1 does not dispatch or budget agents. From the dashboard the operator can read a run's narrated transcript, and steer or kill a run whose engine process is verified alive. The service binds only to `127.0.0.1`; agent execution does not depend on the service.

Ubuntu deployments can optionally use the authenticated HTTPS Nginx overlay in
`server/compose.agent-control.nginx.yml`. It shares Nginx's loopback namespace,
requires an external password file and TLS certificate, and preserves Origin/CSRF
checks. See [the deployment instructions](docs/operator-guide.md#authenticated-https-on-ubuntu).
This adds protected web access, not Linux host-agent import or process monitoring.

## Features

- **Accounting.** Token use for today, the month and all time, broken down by project and by model. Only operator-approved projects count.
- **Cost estimation (experimental, disabled).** List-price cost estimates are not activated in this build. The dashboard shows token counts only, the `pricing` command and pricing API refuse requests, and no dollar amounts appear in the data API. Setting `AGENTCONTROL_ENABLE_COST=1` turns the experimental views on for development and testing only.
- **Live now.** Running and recently ended runs, with their phase, retries and cut replies. Runs that need attention are flagged.
- **Narration and control.** Read a run's narrated transcript while it runs, including streamed reasoning and answers. Steer or kill a run whose engine process is verified alive.
- **Other tools.** Claude Code, Codex, Cline and Kilo sessions show in Live now, and their token usage counts in the totals.
- **The engine.** The shared harness engine lives in `engine/` and is deployed with `scripts/deploy_engine.py`. It answers a Claude reply cut at the gateway's 2-minute limit with a request for a smaller step, instead of resending it. Tool-mode runs have hard turn ceilings, early budget warnings, reserved closeout turns, and optional preauthorized continuation inside the ceiling. Incomplete runs retain their evidence without being counted as successful completion. See the operator guide for budget options and exit codes. One-shot calls and agent turns stream, so the dashboard shows answers, notes, GPT's reasoning summary, and the text of files being written, while they run.

## Development

Requires Python 3.10 or newer.

```bat
python -m pip install -e ".[test]"
python -m pytest
```

All tests use temporary paths. Do not point test commands at `C:\Users\operator\.claude`, Projecta, or Projectb runtime artifacts.

The vanilla dashboard and run-panel helpers have their own tests. Node 22 needs the files listed explicitly:

```bat
node --test tests/js/dashboard.test.mjs tests/js/panel.test.mjs
```

## Commands

```bat
agent-control sync
agent-control serve --port 8787
agent-control projects list|approve|reject|disable|rename
agent-control pricing show|preview|append
python scripts\migrate_existing.py --dry-run --projecta "<path>" --projectb "<path>" --report "<path>"
python scripts\deploy_engine.py
```

Live migration, engine deployment, delegated proof runs, the production
listener, and any scheduled task are operator-gated. See the cutover checklist.

The engine deployment is a dry run unless you pass `--apply`. It deploys
`engine/delegate.py`, `engine/ask-model.py` and `engine/fanout.py` together,
plus complete optional capability/supervision bundles when present. Each planned
file gets its own backup. If a file or gate fails, the whole planned set is restored.

## Goal supervisor (opt-in source extension)

`python engine/supervise.py --help` exposes a separate Windows-first goal supervisor.
It does not add dashboard dispatch or change Release 1 defaults. The supervisor uses
the installed shared harness, not a source-worker fallback. An older installation is
refused before model admission or creation of goal state. Shared deployment and a
production goal execution still require their separate operator approvals.

The operator supplies a Git project, goal text, an audited capability file and a
protected validator registry. Capability files have this shape; selected command argv
must also be allowed by the project's existing `.claude/agent-policy.json`:

```json
{"write_paths":["src/"],"commands":[["python","-m","pytest","-q"]],"mcp":"none"}
```

The registry is UTF-8 JSON with `schema_version: 1`, an absolute `validators_dir`,
`checks`, and `manual_criteria`. Each check declares `check_id`, `criteria`, a relative
Python `script`, its exact `script_sha256`, `timeout_seconds`, allowed `failure_codes`,
and `falsifies` (an approved hypothesis ID or null). The trusted, pinned scripts must
define the actual acceptance conditions. They run outside the candidate and worker
write areas; candidate-owned assertions cannot replace them. Keep credentials out
of goals, capabilities and registry arguments.

Keep the validator directory, state directory and raw artifact directory separate
from one another and from the project. Use private local storage. Preparation writes
only an **unapproved metadata envelope** and prints its SHA256; it starts no agents:

```bat
python engine/supervise.py prepare --project "<project>" --goal "<goal.txt>" --registry "<registry.json>" --capabilities "<capabilities.json>" --out "<envelope.json>"
```

After the necessary operator approvals, use the printed digest explicitly:

```bat
python engine/supervise.py preflight --project "<project>" --envelope "<envelope.json>" --approve-sha256 "<digest>" --goal "<goal.txt>" --registry "<registry.json>" --capabilities "<capabilities.json>" --state "<private-state>" --artifacts "<private-artifacts>"
```

New preparations emit **version 2** approvals: no default lifetime worker, planner or
replan quota; capacity **8 concurrent workers**; no implicit deadline or usage ceiling.
Use `prepare --max-concurrent-workers 16` for higher capacity. Optional
`--max-total-attempts`, `--max-replans`, `--max-planner-calls`, `--deadline-seconds` and
`--max-usage` add explicit frozen controls before approval. Zero replans is supported.
Existing version 1 approvals retain their original fixed 2/6/2 profile and required
budget/deadline; they are never silently upgraded.

`run` and `resume` accept the same approved inputs plus `--reserve`, the estimated token
reservation per model call (default 1,000,000). `resume` reconciles the original
envelope and layout; it never resets counters, expands approval or blindly launches another attempt.
Use `prepare --allow-cancel` only when identity-verified cancellation of owned Jobs is
authorized. Ctrl-C enters bounded stopping, not an unverified PID kill.

Version 2 continues through useful work, diagnosis and fresh worker contexts rather
than failing at two replans or six admissions. A stalled round is labeled diagnostic,
not fabricated new evidence; no-op/A-B-A content still does not count as progress or
verification. Capacity is not a requirement to manufacture eight assignments. Manual
criteria, permissions and unknown effects still block unsafe progress. V2 history
rotates into append-only segments without a lifetime-byte cutoff; a durable commit
witness prevents missing acknowledged suffixes from resetting counters. Real storage,
RAM and replay time remain finite. Uncommitted verification frames are not acceptance.

**With no chosen quota, budget or deadline, a goal can keep spending and running until
verified success, a real blocker or operator stop.** Select explicit controls when that
is not appropriate. Usage lower bounds and unresolved reservations are distinct from a
complete measurement; provider spending is not guaranteed by token estimates.

Integrated candidates and raw findings stay in owned artifacts. Successful
verification is bound to that candidate's bytes, not to the operator's main working
tree; no automatic merge occurs. Snapshot hashing is bounded (256 MiB, 100,000
entries and a cooperative 10-second budget); an incomplete snapshot is not verified.
The runtime is one worker level, MCP disabled, fixed planner/worker profiles
and local closeout. Proposals are technically bounded to 64 tasks and 128 KiB of
metadata; individual contexts/commands and record sizes also stay bounded. Those are
request/resource boundaries, not lifetime worker/replan quotas. Windows Job Objects contain process trees, **not** filesystem or
network access by hostile local code. Lost identity or uncertain effects may require
operator reconciliation instead of automatic recovery.

The approved contract and executable T01-T25 oracles are in:

- `docs/superpowers/specs/2026-10-30-goal-supervisor-contract-design.md`
- `docs/superpowers/plans/2026-10-30-goal-supervisor.md`
- `tests/test_supervisor_traces.py` (26 tests for 25 trace IDs, not 25 live-provider runs)

See:

- `docs/superpowers/specs/2026-09-17-agent-control-design.md`
- `docs/superpowers/specs/2026-09-24-narration-and-intervention-design.md`
- `docs/superpowers/plans/2026-09-17-agent-control-implementation.md`
- `docs/superpowers/plans/2026-09-24-narration-and-intervention-implementation.md`
- `docs/operator-guide.md`
- `docs/cutover-checklist.md`

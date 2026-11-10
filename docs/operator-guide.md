# AgentControl Operator Guide

AgentControl is a machine-wide, local-only monitoring, accounting and
intervention control plane for delegated agent runs. All mutable paths are
injectable so development and tests remain isolated from the current user's live
accounting state. From the dashboard the operator can read a run's narration,
and steer or kill a run whose engine process is verified alive.


> **Cost estimation is disabled in this build.** It is experimental and has not
> been activated. AgentControl records and shows token counts only: the
> dashboard hides cost columns and spend panels, `agent-control pricing` exits
> with an error, `/api/pricing/*` returns `cost_disabled`, and `/api/data`
> carries no dollar amounts. Sections below that describe pricing apply only
> when the experimental feature is enabled with `AGENTCONTROL_ENABLE_COST=1`.

## Safety boundary

Do not apply a historical migration, deploy the shared engine, start the
production listener, change project state, or create a scheduled task without
explicit operator approval. AgentControl is not an authentication boundary and
must not be exposed through raw port forwarding or non-loopback binding; the
server binds only to `127.0.0.1`. Since 0.2.0, the operator-approved authenticated
HTTPS proxy mode described below may provide external access. Nginx, not
AgentControl, authenticates every request; `--public-origin` alone is not
authentication and must never be used with an unprotected proxy.

`pricing_history.jsonl` must remain absent until a genuine, user-confirmed
pricing edit. Repo-local accounting removal is deferred until migration and
parity verification are complete.

## Runtime layout

- Runtime state directory: `C:\Users\operator\.claude\agent-control\`
- Machine-wide run index: `C:\Users\operator\.claude\agent-runs-index.jsonl`
- Installed shared engine: `C:\Users\operator\.claude\bin\delegate.py`
- Tracked baseline pricing: `config/pricing.json`
- Operator actions (metadata only): `C:\Users\operator\.claude\agent-control\operator-actions.jsonl`

## Commands

All commands accept injected paths (`--state-dir`, `--machine-index`,
`--pricing`, `--web-dir`) so they never touch live state during tests.

- `agent-control sync` — incrementally ingest the machine index into the
  registry and ledger; prints a strict-JSON sync report.
- `agent-control serve --port 8787` — start the loopback dashboard.
- `agent-control projects list|approve|reject|disable|rename` — inspect and
  transition project state. Pending projects stay excluded from normal totals
  until approved.
- `agent-control pricing show|preview|append` — inspect rates, preview the
  impact of a rate, and append an immutable effective-dated version. Append is a
  genuine pricing edit and is the only action that may create
  `pricing_history.jsonl`.
- `python scripts/migrate_existing.py --dry-run ...` — read-only migration
  planning; never writes accounting state.
- `python scripts/deploy_engine.py` — dry-run engine deployment by default;
  `--apply` and `--rollback` are operator-gated. It deploys
  `engine/delegate.py`, `engine/ask-model.py` and `engine/fanout.py` together.

## Authenticated HTTPS on Ubuntu

The public `example/agent-control:0.2.0` image supports AMD64 and ARM64.
The Compose files pin its immutable digest. The standalone
`server/compose.agent-control.yml` remains localhost-only. For the existing
server stack, merge `server/compose.agent-control.nginx.yml` instead; it extends
the standalone service without changing the existing stack's project name.
Use the same Compose project name and working directory as the running server
stack. If it was started with `-p`, retain that exact project name in all commands.

The installed server uses project `stack` and the live base file
`/srv/stack/docker-compose.yml`. Its deployment files are co-located there;
do not replace that live base file with a reference snapshot. Run the commands
below on the Ubuntu host. This helper retains the installed project identity
and resolves bind mounts from its working directory:

```sh
ac_compose() {
  docker compose -p stack --project-directory /srv/stack \
    -f /srv/stack/docker-compose.yml \
    -f /srv/stack/compose.agent-control.nginx.yml "$@"
}
ac_compose ps
```

For a new installation, first place `compose.agent-control.yml` and
`compose.agent-control.nginx.yml` beside the existing base Compose file, and
adapt the helper's paths and project name. Merge the two server blocks in
`server/nginx.agent-control.conf` into a copy of the existing Nginx configuration's
`http` block, saving the complete result as `nginx-config` beside the base Compose
file. Preserve every existing virtual host and include; the template is not a
complete `nginx.conf`. On the installed server this merge is already present;
do not append duplicate server blocks. The original `nginx/nginx.conf` remains
a fallback without the AgentControl route.

Full server snapshots (`server/docker-compose-server` and `server/nginx-config`),
certificate material, and deployment environment files are private local inputs
and are ignored by Git. Only the AgentControl-specific template is versioned.

The editable hostname default is `agent-control.example.com`. Before deployment,
confirm or change it in the merged Nginx configuration and the `x-public-origin`
setting in `compose.agent-control.nginx.yml`. Set its DNS records to this Ubuntu
server. No DNS records or live server configuration are changed by this
repository's tests.

The backend shares only Nginx's network namespace and still binds to
`127.0.0.1:8787`; no backend port is published. Basic Auth applies to all HTTPS
routes, including the initial page, static assets, narration, and mutations.
Nginx preserves the browser's real Host and Origin, strips Authorization before
proxying, and does not fabricate CSRF headers. In proxy mode the backend accepts
only the configured HTTPS authority/origin; forwarded headers are not trusted.
The health check sends that authority to the private loopback listener.

### Provision credentials and TLS first

Keep the current Nginx running until the certificate exists. Its existing HTTP
ACME webroot can obtain the new hostname's certificate before the updated HTTPS
virtual host is activated:

```sh
docker compose -p stack --project-directory /srv/stack \
  -f /srv/stack/docker-compose.yml run --rm --entrypoint certbot certbot \
  certonly --webroot -w /var/www/certbot -d agent-control.example.com
```

Complete any Certbot account/email/terms prompts interactively. The required
certificate files must then exist under
`/srv/stack/nginx/certbot/conf/live/agent-control.example.com/`. If this is a fresh
server, start the existing HTTP challenge-serving configuration first; do not
start the new TLS virtual host with missing certificates.

Create the password file outside the repository on the Ubuntu host. These
first-time setup commands prompt for a strong password without putting it in
shell arguments, source, image layers, logs, or chat:

```sh
sudo install -d -m 0700 /etc/agent-control
docker run --rm -it --mount type=bind,src=/etc/agent-control,dst=/auth \
  httpd:2.4-alpine htpasswd -cB -C 10 /auth/htpasswd operator
sudo chmod 0644 /etc/agent-control/htpasswd
```

Use `-c` only when creating a new file; omit it when updating an existing user so
other users are not erased. The host directory stays mode `0700`; the file is
readable by the Nginx worker through its read-only bind mount. Set
`AGENT_CONTROL_HTPASSWD_FILE` if the external file lives elsewhere. Never commit
the file or print its contents. A missing file fails container creation rather
than creating a directory or allowing unauthenticated access.

Keep any server-generated password in a root-only (`0600`) file outside the
deployment directory, separate from AgentControl's accounting state and never
mounted into either container. After changing the external htpasswd
file, recreate Nginx and AgentControl together: an atomic password-file update
can otherwise leave an existing file bind mount attached to the old inode.

### Validate and activate

The overlay mounts the private, complete `nginx-config` directly, replacing the
base stack's `/etc/nginx/nginx.conf` mount. Validate that all unrelated virtual
hosts remain in that file. Once credentials, DNS, and the certificate are ready:

```sh
ac_compose config --quiet
ac_compose pull agent-control
ac_compose run --rm --no-deps --entrypoint nginx nginx -t
ac_compose up -d --no-deps --force-recreate nginx agent-control
```

Only the selected Nginx and AgentControl services are recreated. Recreate them
together when replacing the Nginx container so the shared namespace stays
current. Open `https://agent-control.example.com` and authenticate with the user
created above. Reverting to the base stack's original Nginx configuration and
removing the AgentControl service rolls back the proxy deployment; preserve the
named state volume, and do not use `down -v`.

After Certbot renews a certificate, reload Nginx to load the new files:

```sh
ac_compose exec nginx nginx -s reload
```

The existing renewal loop does not reload Nginx itself. This setup does not add
a scheduled reload task; the operator must arrange reloads or run the command
after renewal. A reload keeps the container's network namespace intact.

Access and request error logging are disabled for these virtual hosts, including
inherited debug logging. Request/response buffering, proxy caching, and proxy
storage are disabled, and request bodies are capped/buffered in memory at 16 KiB
to prevent narration or guidance from being spooled into proxy files. Use
`nginx -t`, HTTP status codes, and container health for deployment diagnostics;
do not enable body/header debug logging on this route.

This deployment starts with isolated empty state and no host agent sources.
Container PID isolation and Windows-oriented project normalization mean it is
not a Linux host-monitoring collector. Do not mount host indexes, repositories,
user homes, host PIDs, or the Docker socket into it. No historical import,
project approval, migration, shared-engine deployment, or scheduled task is
performed by this setup.

The opt-in Docker integration test uses only disposable containers, synthetic
credentials/certificates, temporary paths, and no published ports:

```sh
docker build -f server/Dockerfile.agent-control -t agent-control:proxy-test .
AGENTCONTROL_RUN_DOCKER_TESTS=1 python -m pytest tests/test_proxy_docker.py -q
```

## Reading Live now

Live now lists every run whose engine has not finished. It ignores the Run
history filters and project approval, so it always shows the full set of open
runs. Runs in projects that are not approved carry a tag: pending project,
rejected project or disabled project. Rows that need attention come first, then
the newest.

Each row has a state. Hovering a state shows the reason and the last signal.

| State | Meaning |
| --- | --- |
| running | The engine process was verified alive by the process check. |
| retrying | The current model call is on attempt 2 or later, or the model replied without calling a tool. It enters Needs attention at attempt 3, or after 2 such replies. |
| quiet | Nothing has been written to the run's transcript or journals for 12 minutes. The engine writes at least every 10 minutes while healthy (each model request and each command is capped at 600 s), so this means it is not advancing. If the process is verified alive, the engine is hung: check its terminal. |
| unverified | There is no process identity. This covers runs from older engines (schema 1), and runs where the probe could not tell. `agent-control serve` warns at startup when psutil cannot be imported. |

The columns are: Doing (waiting on model, retrying model call with attempt N/M,
tools with the last tool, or wrapping up), Elapsed, Iteration (N / M), Sent,
Returned and Cost so far. The summary line under the heading shows the live run
count, the cost so far and a burn rate per hour.

Needs attention lists new projects awaiting approval and the live runs flagged
above, each with its reason.

Dismiss is offered only for quiet runs whose process is not verified. It asks
for confirmation and marks the run interrupted. If the agent was in fact still
running, its real end record later replaces the dismissal. The server refuses
(HTTP 409, `engine_alive`) to dismiss a run whose engine process is verified
alive. AgentControl cannot stop processes; stop the process instead, and the run
then ends on its own.

Recently ended shows runs that ended in the last 15 minutes. Failed runs, and
interrupted runs that were not dismissed, are highlighted. Clicking a row there,
or in Run history, opens its activity.

The activity drawer shows what the agent is doing now and for how long, for
example "waiting on model 2m 10s (attempt 2/4)". For each step, it shows the
model time and the tool time separately. Runs from an engine older than the one
deployed from this checkout show only "until next reply" timings, and no "of M"
in the iteration.

If a refresh fails, the live table greys out and says how old the data is. It
never silently keeps showing stale rows as current.

"Iteration N of M" and the model/tool time split need the engine deployed from
this checkout. The deploy is operator-approved:
`python scripts/deploy_engine.py --apply`. It runs the contract tests, then
deploys `engine/delegate.py`, `engine/ask-model.py` and `engine/fanout.py` to
`~/.claude/bin` together. Each file gets its own backup, named
`<file>.backup-<stamp>-<digest>`, an atomic replace and a verification. If any
file fails, all three are restored, so the deploy is all or nothing. Without
`--apply` it is a dry run.

### Engine-first interruption diagnosis

The run panel's **Engine diagnosis** section keeps the engine exit separate from
the last command exit. A command returning `1` does not prove that the engine
crashed. The panel shows recorded engine exceptions, the last completed boundary,
the last unmatched start, the source bundle fingerprint, and evidence gaps. An
unmatched start is not proof that an operation failed, especially when coverage
is partial. A process exit alone does not identify who or what stopped it.

Two read-only commands are available without starting the dashboard:

```text
agent-control diagnose --run-id <run-id>
agent-control incidents --days 7 --limit 200
```

Both accept the normal injected path options before the subcommand. They do not
sync, migrate, revise accounting, register projects, or replay commands. The HTTP
equivalent for one run is `GET /api/diagnosis?run_id=<run-id>`; the activity response
also contains the same `diagnosis` object.

The incident report uses the latest ledger revision of each run and a cohort
selected by **run start time**. `interruption_rate` counts interrupted runs over
all terminal runs in that cohort; it is not a count of confirmed engine defects.
`--limit` bounds detailed sidecar reads, not that denominator. `unscanned_runs`
must be considered when interpreting the grouped causes. Unknown fingerprints
and causes stay unknown rather than being assigned to an engine build or owner.

Diagnosis reads only the two known diagnostic siblings, never the transcript or
messages. Each sidecar read is bounded to 256 KiB and 2048 accepted rows, keeping
startup evidence and a recent tail. Oversized, incomplete, malformed, unavailable,
and mismatched process-identity evidence is explicit. No full sidecars, exception
messages, command arguments, prompts, tool output, or raw traceback are returned.

The extended boundary events require a separately approved shared-engine
deployment and apply to future launches only. Existing runs and historical
missing events are not repaired by deploying newer code. Deployment and service
restart are separate operator decisions; running either diagnosis command does
neither. Synthetic subprocess and boundary regressions join the engine deploy
gate when diagnostic support is included.

### One-shot calls appear as runs

`delegate.py --oneshot` sends one request with no tools. It is registered as a
run like any agent run. It gets start and end records in the machine index and
the repo run index, and a transcript with the brief and a "## Final report"
section that holds the answer. It also gets a usage journal and the token
footer.

Two callers use it:

- `ask-model.py` is now a thin wrapper around `delegate.py --oneshot`. It takes
  the same command line as before, plus `--label` and `-C`. The label defaults
  to `ask-<model>`, and `-C` defaults to the current directory.
- The fanout synthesis step uses it with the label `fanout-synthesis`.

These calls now appear in Live now and Recently ended, with tokens and cost.
Before this change they were not shown at all. They need the engine deployed
from this checkout.

### Turn budgets and incomplete results

These rules apply to tool-mode runs after the engine from this checkout is
deployed. They do not change tool-less one-shot calls.

`--max-iters` is a positive hard ceiling on model turns, including repairs and
the final report. The default is 30. The engine reserves two tool-enabled
closeout turns for verification, fixes and rechecks, followed by one tool-less
report turn. A default run therefore has 27 ordinary work/repair turns, two
closeout turns and one report turn. A genuine `finish` can end the run earlier.
For small ceilings, 1 means report-only; 2 means one work turn and one report;
3 means one work turn, one closeout turn and one report.

The agent receives a remaining-budget warning and a closeout instruction before
tool access ends. Optional `--soft-iters` sets an earlier work checkpoint and
preauthorizes one continuation inside the same hard ceiling. For example:

```bat
python engine/delegate.py -C "<project-root>" --max-iters 40 --soft-iters 27 -t "<task>"
```

This allows 27 ordinary turns, one continuation of up to 10 turns, two closeout
turns and one report. Without an explicit earlier checkpoint, there is no extra
continuation phase. A model cannot extend its own ceiling, and file changes or
claims of being nearly done never authorize more turns. `ask-model.py` accepts
these options only with `--tools`; `fanout.py` forwards them to each child.

At most two protocol-repair requests are allowed across a run, within the same
hard ceiling. Nested HTTP retries are separate: a model-turn limit is not a
global HTTP-request limit or a wall-clock deadline. Existing request and command
timeouts still apply. A command or mutating tool whose outcome is unknown is
not replayed automatically; inspect its effects before deciding what to do next.

An incomplete run retains its partial report and edits, but ends with status
`failed`, an allowlisted `stop_reason`, and exit code 3. Reasons distinguish
`max_iters`, `repair_limit`, `no_action_limit` and `mutation_outcome_unknown`.
The dashboard labels these runs Incomplete, and the exact-launch watcher never
reports them as successful completion. Fanout preserves their partial evidence
but excludes it from successful-report counts and synthesis inputs. No caller
automatically restarts the task. Review the partial result and verification
state before launching any follow-up work.

Exit codes remain 0 for a usable explicit finish, 1 for an operational failure,
and 2 for invalid command-line arguments. Old records without `stop_reason`
remain readable; no historical migration is needed. Implementing this change
does not install it: shared-engine deployment still requires operator approval.

Default commands never scan the candidate tree. Only disk mode with an explicit
verification plan takes bounded snapshots: 16 MiB, 2048 files plus directories,
1 second per snapshot, and 64 KiB reads; only root `.git` and `.claude` are
excluded. An incomplete snapshot, link/reparse point, read error, detected race,
or exceeded limit means `candidate_state_unavailable`, never
`verified_by_command`. The command's `exited` outcome and exact exit code
(including zero) remain intact; work continues without replaying the command.
Staged writes remain `staged_unverified_on_disk`. The deadline is cooperative:
it cannot preempt one blocked OS call, and link/open checks are not a security
sandbox. Existing processes do not hot-load a deployment; they keep their loaded
engine. The existing `### [turn] tool(...)` transcript header is now flushed
before dispatch, so the dashboard shows the correct phase without a dashboard
server restart once a run uses the updated engine.

### Recoverable command output (`resilient-v1`)

After deployment of this source, `resilient-v1` captures command stdout and stderr
in bounded chunks. Small complete output keeps the existing text format. Larger
streams spill to exclusive files in the owning run's artifact directory; the
tool returns previews, the actual exit code, capture status and `cmd://` references.
If previews do not fit the remaining batch allowance, compact metadata preserves
the references instead. The command is never rerun to recover its output.

Use `read_command_output(ref, offset=0, limit=4096)` to retrieve saved bytes. Follow
`next_offset` until `eof`; offsets count bytes, not characters or lines. A page is
UTF-8 text when valid, otherwise explicitly base64 encoded. A single long JSON
line can therefore be recovered without losing its suffix. References are valid
only in their originating run. Artifact identity and the read blocks are checked.

The shared artifact limits remain 200 files and 256 MiB per run. On storage failure
or quota exhaustion, capture is marked incomplete and excess bytes are drained
and discarded without terminating the command merely for output volume. Stored
EOF does not imply complete capture: inspect `capture_complete` and
`discarded_bytes`. If storage I/O outlives cleanup, the caller receives an
incomplete capture; the drain retains ownership of its sink until that I/O
returns. Filesystem operations remain cooperative, not forcibly preempted.
The selected context policy and turn limits still apply. Under `legacy`, request
and cumulative-read/output byte limits remain unchanged. Command policy, capture
quotas and verification requirements do not change with the context policy.

### Model-aware context and optional compaction

The complete shared-engine bundle was deployed and checksum-verified on 2026-11-02.
Editing this checkout alone does not update that installation; subsequent deployments
still require separate operator approval.

Ordinary direct and one-shot launches default to `--context-policy model-v1`.
The GPT-first release selects a local operating profile for the requested model and
route before reading gateway credentials. For `gpt-6.1-sol`, Chat and Responses use a
512,000-token configured operating window, 32,000 reply tokens and 25,600 safety tokens.
High-effort live retention passed above 513k reported inclusive input on both routes;
512k is a conservative operating choice, not a claimed backend maximum. Other efforts
retain the configured policy without inheriting an untested evidence guarantee.

The tested Claude Opus 5.5 Chat profile keeps its configured 1,000,000-token window,
32,768 reply reserve and 50,000 safety reserve. Unknown model/route configurations stay
explicitly unverified; the pure resolver's 1M fallback is not proof of capacity. Profile
metadata separates configured window, observed lower bound, declared maximum/unknown,
requested effort and evidence scope. Explicit overrides preserve operator provenance
but do not certify a provider limit. Smaller windows scale the default reserves. Flags:

- `--context-window-tokens N`
- `--context-reply-tokens N`
- `--context-safety-tokens N`

For `model-v1`, preflight puts a requested output bound on the actual wire packet
before measuring it: `max_completion_tokens` for Chat or `max_output_tokens` for
Responses. A stricter explicit positive bound is preserved; an invalid or wider
bound is refused before dispatch. This is a request to the gateway, not proof that
every provider enforces it. Provider-verified output-limit metadata remains unknown
unless established separately. Legacy payloads are not modified by this step. The
tested Claude gateway ignores both observed flat cap variants; no unverified field
prototype or gateway setting is substituted in this release. Its bound enforcement
remains unsupported, and local output clipping is not a provider billing limit.

A known Responses `response.incomplete` is recorded as partial with its supplied usage,
including reasoning-only output. The CLI exits 1 with `response_incomplete`, retains
received text once, and does not retry/fall back or execute the incomplete response's
tool calls. A native partial also exports earlier accepted staged edits locally without
another model wrap-up. Terminal context rejections retain earlier paid attempts and any
actual terminal usage. Missing terminals remain unknown rather than fabricated success;
already emitted transcript bytes are never rewritten to relabel an outcome.

Admission measures the complete prepared request. Estimated input plus reply and
safety reserves must fit the configured window. Counts remain labelled estimates
unless a matching measurement establishes otherwise. A configured window is not
proof of exact provider tokenization or transport capacity. Cached input occupies
context once; lifetime billed input is not current context occupancy.

`model-v1` replaces the old 256 KiB request, 128 KiB lifetime-read and 192 KiB
lifetime-output context stops. Finite producer, page, memory and artifact limits
remain. Image count, pixel, byte and text restrictions remain separate. HTTP 413
is a transport rejection, not evidence that the configured token window is full.

Use `--context-policy legacy` alone to select the original byte policy. Do not
combine it with token or compaction options. Frozen approved, supervised and
recovery launches retain their original policy and reject new context overrides.
The context policy is independent of `--profile legacy|resilient-v1`.

Compaction is disabled by default. Enable it explicitly for a tool-mode run:

```powershell
python "$HOME/.claude/bin/delegate.py" -C "<project-root>" --skill ponytail --context-policy model-v1 --auto-compact --compact-fraction 0.70 -f "<task-file>"
```

The default trigger is 70% of the effective configured window: 358,400 estimated input
tokens for the 512k GPT profile or 700,000 for 1M. The engine can trigger earlier to
preserve summarizer headroom. The target is 80% of that trigger: 286,720 or 560,000
tokens respectively. Explicit
reserves and fractions must leave a valid input allowance. `--no-auto-compact`
keeps compaction disabled. One-shot calls reject `--auto-compact` to avoid an extra
maintenance generation. Permitted request retries still retain their actual or
unknown usage. `ask-model.py --tools` and fanout children forward these
options; fanout's one-shot synthesis does not receive compaction options.

Compaction runs only between complete tool batches. It uses the approved
model/route/effort without tools or model fallback. The engine retains authoritative
instructions, task, skills, steers, recent exchanges and its own state capsule.
The summary is untrusted descriptive memory, not permission or verification.
After validation, the engine publishes a run-owned artifact before replacing
only the active context view. The original transcript and journals remain intact.
Failures, unknown usage, stale bindings, unsupported history or storage faults
retain the original view. Attempts still consume actual calls and charges.
Compaction does not reset turn limits, execution history or unknown effects.
Prior descriptive memory can be consolidated again; it is not permanently pinned.
Authoritative instructions and incomplete-capture groups remain retained instead.

Each paired result has a reserved `result://` reference before tool execution.
If a rich result does not fit, use `read_context_result(ref, offset=0, limit=4096)`
instead of rerunning the action. Offsets count bytes. Follow `next_offset` until
`eof`, and inspect `encoding`, `content_complete`, `outcome` and `exit_code`.
Stored EOF is not proof that all producer content was captured. References work
only in their owning run and grant no additional path or command access. A
pending or unknown outcome is never evidence that an action is safe to repeat.
After compaction, the engine-owned frame supplies a frozen
`result://<scope>/index/<H>` handle. Read it with the same `read_context_result`
arguments to discover earlier own-run result handles independently of the summary.
The index lists the reservation prefix through `H`; later reservations do not change
its bytes or EOF. It is bounded and paged, and grants no additional access rights.
Its `content_complete` describes the list, not the bodies listed in it. Read each
ordinary handle's own capture and outcome metadata before drawing conclusions.

A failed result-body publication can retain an explicitly incomplete inline prefix
while preserving the real effect and exit code. This is not permission to replay,
and it is not evidence that the model window is full. Otherwise-safe dispatch may
continue, but compaction must retain that incomplete-capture group rather than
silently remove the only visible uncaptured content.
Central accounting and metadata APIs contain allowlisted metadata and references,
not summaries. The existing on-demand narration exception remains unchanged.

For an explicitly approved live release check, `tools/agent-harness/context_live_canary.py`
requires `--run-live`, `--case`, `--engine`, a fresh temporary `--out`, and a fresh
`--launch-id`/`--launch-receipt` pair. It makes real configured-gateway calls using only
synthetic fixtures, confines actions to read-only own-run evidence, and keeps accounting
and sidecars in the supplied temporary area. Its small-window probes do not change
installation defaults or verify full 1M-token provider capacity. The metadata report
checks real committed reduction/retention/continuation, complete read delivery,
append-only paired journals and phase-ordered usage against terminal totals. A frozen
earlier own-run index is valid even when a newer prefix or a compacted view exists.

### Opt-in bounded multi-file reads

The operator-approved Phase 9 rollout was installed and verified on 2026-11-03. All
45 selected shared-engine payloads and 45 fresh rollback backups match the complete
release manifest. The feature is available to new installed-harness runs; it does not
change defaults or enable the feature in existing runs.

The explicit source or installed-engine options are:

```text
--read-files --profile resilient-v1 --context-policy model-v1
```

They enable `read_files` for an ordinary direct native-tool run only. One-shot,
legacy/frozen, supervised, approved-worker and recovery-bound launches are excluded.
Omitting the new option preserves the existing advertised tools and request behavior.

`read_files` takes 1–8 ordered `requests`, each with `path` and optional positive
`start_line`/`end_line`. Its JSON result labels every entry as served, error or unserved.
Read `complete` and any `next_range`; do not infer a complete read from a partial body.
Unserved entries have not been read. Re-request only the needed read/range under current
permissions; this tool grants no command, write, URI or path access beyond existing reads.

The goal is fewer model round trips for independent reads. Scripted request-count
comparisons are not real-provider latency measurements. Existing upstream cache counters
remain observations, not proof of cache sharing, retention or invoice savings; no cache
key, TTL, explicit Claude cache field or local response cache is added.

Source acceptance passed 128 focused feature tests; the final complete Python suite
with the rollout helper passed 4,916 tests / 17 skipped, and all 91 JavaScript tests
passed. Independent source review and the scoped long-line repair re-review cleared
the Important source findings. The unchanged original deployment gate passed 3,261
tests / 2 skipped before the complete installation. Installed payloads, version,
compilation and fresh before-image backups were independently verified.

Equivalent scripted tasks reduced model requests from 3 to 2 for two file reads plus
finish, and from 9 to 2 for eight reads plus finish. Actual small live cases include
AGENTS bootstrap: the source baseline used 4 generation POSTs, while source and
installed batch cases used 3. Installed Claude/medium and GPT Responses/high cases
each delivered AGENTS plus both synthetic fixtures and finished normally with paired
results and exact sentinel proofs. Observed engine-call elapsed times were 12.552292s
and 15.982691s respectively. These are tiny samples, not a causal or statistical
provider speedup. Normal provider routes, efforts, windows and global settings remain
unchanged; Claude output-cap enforcement is still unsupported/unverified.

The cumulative paid source/installed trial ledger used 21 of 30 POSTs and charged
582,123 of 650,000 conservative reported-or-projected input tokens, including retries
and refused trial attempts. All 21 usage inputs are known; cache gets no budget
discount. Remaining allowance is 9 POSTs / 67,877 input tokens, not a monetary budget
or a promised backend bound. Observed cache reads/writes do not establish cross-agent
sharing, retention or invoice savings. Development/review calls are separately logged.

A prior deployment-gate exit1 discarded its original test trace and remains
unattributed; later passing suites do not constitute a repair. After disclosure, the
operator explicitly approved proceeding with a fresh full gate and retained output.
Fresh backups are retained under the 20261103T233325Z prefix; the delegate entrypoint
before-image is `delegate.py.backup-20261103T233325Z-79565a1bc533`.

For controller scripts on Windows, pass a list argv through `subprocess` when a flag
contains JSON. The controller's `os.execv` path split such an argument in an isolated
regression. Keep external diagnostic pytest collection confined to its scratch directory.

### Reply limits

The gateway cuts every Claude reply (`anthropic_tool_call.*`) that takes longer than about 121 seconds, streamed or not. The engine recognises each kind of failed reply and answers it:

- **A cut reply**, which arrives as a bare `Error:` after 100 seconds or more. The engine tells the model its reply was cut and asks for a smaller step, within the run's finite repair allowance. The narration shows "reply cut by the gateway's 2-minute limit after 121 s; asking for a smaller step".
- **A reply with text but no tool call.** The text is kept, and the model is asked for a tool call straight away.
- **No reply within 10 minutes.** The engine makes one more attempt, at xhigh when the effort was max.

Claude agents are told the limit in their instructions: one tool call per reply, and writes under about 120 lines. Live now shows such a run as retrying, and flags it for attention after a second cut in a row.

### One-shot calls stream

GPT and Claude one-shot calls stream:
- **GPT** goes through `/openai/responses`. The narration shows its reasoning summary when the gateway sends one, and its answer under "Final report (writing…)" while the answer is still arriving.
- **Claude's** thinking is not available from the gateway, but its answer streams the same way.
- **If streaming fails** before the first event, the engine repeats the call without streaming.
- **A Claude one-shot that runs past 2 minutes was cut.** It is asked once for a shorter answer. A second cut keeps that answer, marked "[cut off by the gateway's 2-minute limit]".

### Agent turns stream

Since 2026-10-03, each turn of an agent run streams too, so the timeline fills in while the model is still replying:
- **GPT** (`gpt-*`, native tools) goes through `/openai/responses`. Its reasoning summary shows under "Reasoning (writing…)" when the gateway sends one. GPT does not summarise every step, and quick steps often have none.
- **Claude** (`anthropic_tool_call.*`) streams through chat completions. Its thinking arrives empty from the gateway, so only the step's note shows.
- **File text and the final report** show while they are written: "Writing src/app.py", "Editing src/app.py" or "Final report", with the text growing beneath. The preview disappears when the step completes, because the step itself then shows the result. In the transcript, each preview line starts with `> `.
- **A Claude reply cut at the 2-minute limit** is recognised by its unfinished tool call. A complete Claude tool call reports a normal finish too, so time alone can't tell. It gets the usual smaller-step correction.
- **If the gateway rejects the streamed request** (an HTTP 4xx other than a stale replica), the run continues without streaming. The transcript says `**streaming unavailable (HTTP <code>); continuing without live view**`.
- **`delegate.py --no-stream`** turns streaming off for agent turns and one-shot calls.

## Watching, steering and killing a run

### Opening a run

Every row in Live now, Recently ended and Run history opens a side panel on the
right. Live rows open with the › button or a click on the row; other rows open
with a click, Enter or Space. The rest of the page stays live beside the panel.
The address `#run=<id>` reopens the same run after a reload. `Esc` or the ×
button closes it.

### The timeline

For each turn, the timeline shows:

- the agent's own narration of what it is doing and why, which the engine asks
  for before every tool call;
- each tool call, in one line;
- the tool's output, folded: the engine's 1,500-character excerpt, marked
  "truncated" when cut;
- retries ("Retry: attempt 2/4");
- your guidance ("Guidance, turn N");
- notices, and the final report.

The task brief sits folded at the top. The line at the bottom shows the current
phase, for example "waiting on model 1m 20s".

### What "thinking" cannot show

Claude models (`anthropic_tool_call.*`) return empty reasoning blocks through
this gateway, so for Claude the panel shows the model's narration, not a hidden
chain of thought. GPT models show a reasoning summary, when they write one. Both
stream as the reply arrives (see "Agent turns stream").

### Masking

Obvious credentials are masked on display: API keys starting `sk-`, bearer
tokens, AWS keys, GitHub and Slack tokens, JWTs, private-key blocks, and
`password=` / `token=` style values. Masking is a courtesy for screen sharing,
not a security boundary: the transcript file on disk is unmasked.

### Steering

The text box sends guidance that the engine reads at the agent's next turn. It
is not read during a model call or its retries, which can take several minutes.
It reaches the agent as `[operator guidance] <text>`.

Guidance is appended to the run's `.steer` file, and never overwrites guidance
the orchestrator wrote there.

A steer has one of four statuses:

- "Queued: arrives at the agent's next turn";
- "Delivered at turn N";
- "Not delivered: the steer file was overwritten or consumed without it; send
  again";
- "Not delivered: the run ended".

A queued steer can be cancelled. Delivery tracking lives in the dashboard's
memory. After a restart, delivered guidance still shows in the timeline.

**Ask to wrap up** sends a canned steer asking the agent to stop and call finish
with its report. It is cooperative, so the agent acts on it at its next turn.

### Kill

Kill is available only when the engine process is verified alive: the PID is a
running Python interpreter whose creation time matches the run's start. The
engine may be launched directly or through a wrapper script, so identity rests
on the live pid, the create-time window, and the Python executable rather than
any particular script path.

It terminates the engine and every child process it started, such as a running
test command, immediately. There is no final report.

The confirmation states what happens to the run's edits:

- **staged runs** lose their unapplied edits;
- **`--write` runs'** edits are already in the repository;
- **`--worktree` runs'** edits stay in `.claude\worktrees\<label>` on branch
  `agent/<label>`.

"N process(es) did not exit" means some processes survived even a forced kill.
Check them in Task Manager.

The run then appears in Recently ended as interrupted, with the reason
"killed by operator".

### Where actions are logged

Steer, cancel and kill actions are appended to
`%USERPROFILE%\.claude\agent-control\operator-actions.jsonl`. It holds metadata
only: time, run, action, outcome, character count and process counts. It never
holds the guidance text.

### Security

The server is still loopback-only. Every request must carry the right `Host`
header. The narration endpoint requires the page's CSRF token. The page cannot
be framed by another site, so a hostile page cannot trick a click on Kill.

## Runs launched from Codex, Kilo Code or Cline

### Why such runs were missing

AgentControl discovers runs from the run index the engine appends to for every
start and end. Until now the only index was the machine-wide file at
`C:\Users\operator\.claude\agent-runs-index.jsonl`. OpenAI Codex runs its commands
inside a Windows sandbox, as a separate local user
(`CodexSandboxOnline` or `CodexSandboxOffline`) that may write only inside the
workspace (the repo). Appending to the machine-wide index therefore failed with
a `PermissionError`, which the engine reported only as a stderr warning. The run
completed and left a transcript, but AgentControl never learned the run or its
repo existed, so it did not even appear as a project awaiting approval. Kilo
Code and Cline run as your own user today and were unaffected, but any launcher
that sandboxes the process or redirects the home directory breaks the same way.

### Where the repo run index lives

The deployed engine now writes each record twice: to the machine-wide index and
to a *repo run index* named `agent-runs-index.jsonl` in the same folder as the
transcript, normally `<repo>\.claude\agent-runs\agent-runs-index.jsonl`. The two
files hold the same records. Because the repo run index is inside the workspace,
a sandbox user can always write it, so the run is discoverable even when the
machine-wide append fails.

### Discovery rules

On every sync, AgentControl reads the machine-wide index plus every repo run
index it can find at:

- known project roots;
- every immediate subfolder of each project root's parent, so a sibling of a
  known repo is found with no configuration;
- every immediate subfolder of each folder listed in
  `C:\Users\operator\.claude\agent-control\scan_roots.json`, an optional JSON array
  of absolute folder paths, re-read on every sync (no restart needed);
- the `.claude\worktrees\*` and `.claude\workspaces\*` subfolders of any of the
  above.

Example `scan_roots.json`:

    [
      "D:\\Work\\Tools",
      "D:\\repos\\Projectb\\runs"
    ]

A malformed file reports `invalid_scan_roots`; a bad entry reports
`invalid_scan_root`. An unknown repo found this way becomes a pending
("discovered") project awaiting your approval, like any other.

### Kill refusal for sandboxed runs

You cannot kill a run whose engine process belongs to another Windows user or
runs elevated (for example inside the Codex sandbox). The kill is refused with
HTTP 409 `access_denied` and the message "access denied: the engine process
belongs to another Windows user or runs elevated (for example inside the Codex
sandbox); stop it from the tool that launched it". Stop such a run from the tool
that launched it. Steering and watching still work, because the `.steer` file
and the transcript are inside the workspace.

### Backfilling runs that have no index record

For transcripts already on disk with no index record, use:

    agent-control backfill-index --repo D:\repos\Khattati

This is a dry run: it lists what it would write and changes nothing. Review it,
then apply:

    agent-control backfill-index --repo D:\repos\Khattati --apply

It rebuilds start and end records (marked `backfilled`) from each transcript's
header, filename and modification time, classifies status as complete, failed or
interrupted, skips fanout files, already-indexed runs and transcripts modified
in the last `--min-age-minutes` minutes (default 15), and appends to the repo run
index only. Do not use `agent-control migrate` for this; it invents ids and
timestamps.

### Launching with a task file

To avoid PowerShell joining a multi-line brief onto one line, pass the brief as
a file with `-f/--task-file` instead of `-t`:

    python -u "$HOME\.claude\bin\delegate.py" -C D:\repos\Khattati --worktree -m anthropic_tool_call.claude-opus-5-5-think -e medium --label my-task -f .\brief.md

`-f` and `-t` are mutually exclusive, `-` reads standard input, and the file may
be UTF-8, UTF-8 with BOM or UTF-16.

Runs start writing the repo run index only after the engine is deployed with
`python scripts/deploy_engine.py --apply`.

## Usage from Claude Code, Codex, Cline and Kilo

AgentControl can also count the model calls you make directly in Claude Code,
Codex, Cline and Kilo, outside the engine. It reads each tool's local logs,
keeps only the numbers, and adds the calls to the totals, broken down by tool.
These calls go into a second append-only ledger, separate from the engine runs.

### What is read

| Tool | Default source |
|---|---|
| Claude Code | `%USERPROFILE%\.claude\projects\<slug>\<session>.jsonl`, plus `<session>\subagents\agent-*.jsonl` |
| Codex | `%USERPROFILE%\.codex\sessions\YYYY\MM\DD\rollout-*.jsonl` (`CODEX_HOME` replaces `%USERPROFILE%\.codex`) |
| Cline | `%APPDATA%\Code\User\globalStorage\saoudrizwan.claude-dev\tasks\<task>\ui_messages.json` and `state\taskHistory.json` |
| Kilo | `%USERPROFILE%\.local\share\kilo\kilo.db` |

AgentControl only reads these files. It never changes or deletes them.

Claude Code deletes its own logs after 30 days unless `cleanupPeriodDays` is
set. Calls in deleted logs can no longer be imported.

### What is stored

Each call becomes one line in `tool_usage.jsonl` in the runtime state
directory. A line holds numbers and ids only:

- the tool, the call's id, the session and its top-level session;
- when the call completed;
- the model id, the working folder and the project it belongs to;
- tokens sent, tokens returned, cache reads, cache writes and reasoning tokens;
- Claude Code's speed setting.

A missing number means the tool did not report it. A 0 means it reported zero.

Prompts, replies, titles and file contents are never stored. Every line is
checked before it is written and again when it is loaded, and a field that
could carry free text cannot pass.

A tool can rewrite a call after it was imported, for example while a reply is
still streaming. AgentControl then appends a new revision of the call. The
highest revision counts. A known number is never replaced by a smaller one or
by a missing one.

Import checkpoints are kept in `tool_usage_state.json`, and the tool settings in
`usage_sources.json`, both in the runtime state directory.

### Preview first

Run the import as a dry run:

    agent-control import-usage

It prints one line per tool and ends with `Dry run: nothing written.` For each
tool it shows the calls seen, new calls and revised calls, the number of
sessions, the first and last call times, the token sums and the number of
problems found. It also lists the project folders the import would discover.

A dry run writes nothing at all: no calls, no projects, no checkpoints and no
settings.

By default it covers every tool whose default source exists. To limit it to
some tools, repeat `--tool`. The ids are `claude_code`, `codex`, `cline` and
`kilo`:

    agent-control import-usage --tool claude_code --tool kilo

If one tool's reader fails, the report names the tool and the error type, and
the other tools still run.

### Applying the import

The apply needs the dashboard stopped. Stop it, then run:

    agent-control import-usage --apply

If the dashboard is still running, the command exits with code 2 and prints
`The dashboard is running. Stop it, run import-usage --apply, then start it again.`

The apply writes the calls, records the new project folders and saves the
checkpoints. It also enables the imported tools in `usage_sources.json`. Start
the dashboard again afterwards. From then on, every sync imports new calls from
the enabled tools.

### Approving the new folders

Each call is attributed to a project by its working folder. AgentControl walks
up from that folder to the nearest repository. It never stops at your home
folder or a drive root. If there is no repository, the folder itself is used.

A folder that is not yet a known project becomes a pending ("discovered")
project, like any other. Calls count in the totals only once their project is
approved, the same rule as for runs. A call with no working folder never counts.
Review the folders the import listed and approve the ones that should count.

### Cost and "no rate"

The cost of a call is worked out from list prices in the pricing catalog, the
same as for runs. It is not what you were billed, and it ignores what the tools
record themselves.

A call shows "no rate", with no cost, when:

- the catalog has no price for its model on that date;
- the tool did not report the model;
- the call used Claude Code's fast mode, which the catalog has no prices for.

A call whose tokens sent or returned are missing has no cost either.

A cost is an upper bound when some cache numbers are missing, or when Claude
Code wrote 5-minute cache entries. Those are priced at the 1-hour cache-write
rate, which overstates them.

Prices are added only from the vendor's published pricing page. A model with no
published price stays "no rate".

### Watch-only rows

Sessions in these tools appear under Live now and Recently ended, after the
engine runs. A session is live when its last call is at most 10 minutes old,
and recently ended for 15 minutes after that. Each row shows the tool, the
project (or "not in totals"), the models, the time since the last call, the
calls, tokens and cost, the burn per hour and the subagents.

Each row says `Watch only. This session runs outside AgentControl.` These rows
have no Steer, Kill, Dismiss or narration controls, and they do not open the
panel. Stop or steer such a session from the tool itself.

Totals and Spend gain a `By tool` breakdown, and the filter bar gains `Tool`.
Engine runs are shown as "Harness agents".

### Turning a tool off

To stop importing a tool, set `"enabled": false` for it in
`usage_sources.json`:

    {"schema": 1, "tools": {"claude_code": {"enabled": true, "path": null}, "codex": {"enabled": false, "path": null}}}

A tool missing from the file is off. Records already imported remain in
`tool_usage.jsonl` and still count in the totals.

### Other source folders

Set `path` for a tool in `usage_sources.json` to read from somewhere other than
the default. Use this to point Cline at the storage of a VS Code fork instead of
`%APPDATA%\Code`.

Codex follows `CODEX_HOME`. When it is set, AgentControl reads Codex sessions
from that folder instead of `%USERPROFILE%\.codex`.

### Known limits

- Live is measured from the last call. A session that works for more than 10
  minutes without calling its model shows as recently ended until its next call.
- Claude Code 5-minute cache writes are priced at the 1-hour rate and marked as
  an upper bound.
- Codex cache-write tokens were 0 in every record seen so far. How they should
  be priced is unverified.
- Cline with a direct Anthropic provider, rather than the gateway, is
  unverified on this machine.
- Old Codex files have no per-response records. They are counted from their
  `token_count` events instead: one call each time the running total changes,
  using that event's own per-call usage. Where both exist, this comes within
  about 1% of the per-response records.

## Backups

Before any approved apply, back up the installed engine, the machine index, the
runtime directory, the reviewed migration report, and the Projecta and Projectb
ledgers, registries, and pricing files to a timestamped user backup directory.

## Migration

Migration is read-only, dry-run first, conflict-refusing, and explicitly
confirmed. It never rewrites or deletes source artifacts. A second apply after a
successful apply must plan zero further revisions. Approving a project includes
all of its imported historical runs immediately, with no re-import or restart.

## Parity verification

After approval and sync, compare central recoverable totals (logical run counts
and recoverable sent/returned/cache totals from the highest valid source
revisions) with the repository totals. Every difference must be a report
diagnostic, never a filename or label merge.

## Engine rollback

Engine deployment is backed up, atomic, checksum-verified, and rollback-capable.
It covers `engine/delegate.py`, `engine/ask-model.py` and `engine/fanout.py`.
Each file gets its own backup, named `<file>.backup-<stamp>-<digest>`. If any
of the three fails to deploy or verify, all three are restored.
To roll back, stop the listener and run
`python scripts/deploy_engine.py --rollback "<exact backup path printed by apply>"`.

## Production listener

Start exactly one production listener on `127.0.0.1:8787` and confirm a single
intended LISTENING PID with `netstat`.

## Deferred cleanup

Repo-local dashboard removal is deferred to a later, separately reviewed cleanup
task. See `docs/cutover-checklist.md` for the full gated cutover sequence.

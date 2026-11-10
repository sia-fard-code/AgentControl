# Live now: critical review

2026-09-24. Scope: the Live now table, its activity drawer, and the Needs-attention
banner (`web/dashboard.js`, `src/agent_control/snapshot.py`, `liveness.py`, `sync.py`,
`dashboard.py`, `activity.py`). Four delegated review agents (two on state accuracy,
one on frontend robustness, one on operator usefulness) produced the first pass. Every
finding below was then checked against the code; claims that did not survive are
listed under "Rejected during verification".

## Verdict

Live now currently answers one question: which runs have no end record, and how long
ago was their transcript last written. An operator watching several agents needs four
answers:

1. Is the agent alive?
2. Is it making progress?
3. What is it doing, and what is it costing?
4. Is what I am looking at current?

The section gets the first one wrong in both directions. It flags runs whose engine
process has just been verified alive as `stalled` and then `abandoned`, and offers a
one-click Dismiss. Meanwhile a run stuck in a gateway retry loop shows a healthy
`running`. The second and third answers need a row to be expanded, and the drawer's
timings charge model latency to whichever tool ran last. The fourth is not answered
at all: when a refresh fails, the table keeps showing the old rows.

Almost everything needed to fix this is already computed and then thrown away. The
process verdict is computed on every request. The retry state is written to the
transcript. Cost is already in the payload. No P0 or P1 fix below needs an engine or
machine-index change.

## P0: shows a wrong state that an operator would act on

### 1. The display ignores the process check that sync has just run

- Evidence: every `GET /api/data` runs a full sync first (`dashboard.py:66-73`). The
  sync calls `classify_liveness` with the psutil probe for every open run
  (`sync.py:251-270`), then persists only `persist_status` and `inferred`. Then
  `snapshot.py:26-49` (`_live_display`) recomputes `display_state` from transcript
  silence alone: over 300 s it reads `stalled`, over 3600 s `abandoned`.
- Scenario: a model call at xhigh effort takes 6 minutes, or a `run_command` runs close
  to the engine's 600 s limit. The run shows `stalled`, is listed in Needs attention
  with a Dismiss button, and after an hour reads `abandoned`, all while `liveness.py`
  knows its engine process is alive and valid.
- Fix: have `Synchronizer.run` return the `Liveness` it computed for each open run, and
  let `build_snapshot` use it. A run with valid process identity is never `stalled` or
  `abandoned`; it can be `quiet` (see the state model below). This is internal to
  AgentControl: no engine change, no index-schema change.
- Test: in `test_snapshot`, a schema-2 run with valid identity that has been silent for
  20 minutes stays live, is not `abandoned`, and is not offered for Dismiss.

### 2. A run stuck in a gateway retry loop looks healthy

- Evidence: the engine writes a transcript line before every attempt
  (`calling <model> (attempt N/4, …)`), so transcript silence stays low throughout a
  retry storm. `activity.py:21` parses only `### [N] tool` lines, and
  `NO_TOOL_ACTION` is filtered out as a control marker (`activity.py:24`).
- Observed on the day of this review: three of the review agents themselves each spent
  6 to 10 minutes on attempts 2, 3 and 4 of 4, receiving bare `Error:` replies. Live now
  would have shown `running`, with a last signal under two minutes old, the whole time.
  The harness guide calls this the most common failure.
- Fix: parse the transcript tail for the last `calling … (attempt N/M` line (the same
  pattern `~/.claude/bin/agent_await.py` uses) and the last completed step. Expose
  `phase` (`model_call` or `tool`), `attempt`, `iteration` and `since`. Show
  `retrying 3/4` in the State cell, and list a run in Needs attention once it reaches
  attempt 3.
- Constraint: the sibling `.anomalies.jsonl` holds `turn`, `attempt` and `cause`, but it
  also holds response bodies. If it is used at all, read only those three fields, never
  `response` or `content` (AGENTS.md).

### 3. A failed refresh leaves the table stale, with no warning

- Evidence: `load()` (`dashboard.js:1011-1028`) only rewrites the `#state` line in its
  catch block. The tables keep their old rows, and open drawers keep polling.
  `do_GET` catches only `ApiError` (`dashboard.py:494-495`), so any unexpected exception
  in the sync drops the connection instead of returning an error.
- Scenario: the server restarts, the laptop wakes from sleep, or the ledger is locked.
  Live now keeps showing `running` for runs that finished minutes ago.
- Fix: record the time of the last successful refresh. On failure, grey out `#live` and
  show "Live data is 2 min old; refresh failing". Check `response.ok` on all three
  fetches. Today a JSON error body is assigned to `dash.data`, and the table would then
  read "No live runs".
- Test: stub `fetch` to reject, and assert that the live table gets a staleness marker.

### 4. Live now is filtered by the Run history filters and by approval

- Evidence: `live` is built from `visible` (`snapshot.py:219-221, 250-254`), which
  applies the project, model, task, state and date filters, and drops unapproved
  projects unless `include_excluded` is set.
- Scenario: setting State to `complete` to browse history shows "No live runs" while
  three agents are running. A running agent in a newly discovered, still-pending
  project is invisible. New projects are exactly the ones an operator has not approved
  yet.
- Fix: build `live` from every open run. Approval governs totals, not visibility, so
  tag unapproved rows `pending project`. If a project filter is kept, show
  "N more live runs hidden by filters".
- Test: `SnapshotQuery(states=("complete",))` still returns the open runs in `live`.

## P1: erodes trust or usefulness

### 5. A probe error counts as the process being gone (latent)

`PsutilProcessProbe.inspect` catches every exception and returns `None`
(`liveness.py:25-40`). `classify_liveness` treats `None` as `process_gone` and persists
`interrupted` (`liveness.py:103-123`). Terminal is absorbing against `running`, so the
live run leaves Live now for good. A failed `import psutil` is swallowed too
(`liveness.py:19-22`), which would end every schema-2 run on the first sync.

This is latent today: psutil 7.2.2 is installed, and it can read even PID 4's command
line.

- Fix: return "gone" only for `NoSuchProcess`. For `AccessDenied` and other errors
  return "unknown", keep the run open, and display it as `unverified`. `serve` should
  fail loudly at startup if psutil cannot be imported.
- Tests: add cases for access denied and for psutil missing. Neither exists today.

### 6. Dismiss is one click and hides a live process

- Evidence: the attention-banner handler posts immediately, with no confirmation
  (`dashboard.js:775-786`). `dismiss_run` persists `interrupted`
  (`dashboard.py:250-310`).
- What it does not do: corrupt history. `ledger.py:297-302` blocks only terminal to
  `running`, so a later real `complete` end record does replace the dismissal.
- What it does do: until that end record arrives, a still-running agent vanishes from
  Live now and keeps spending out of sight.
- A related bug: `renderAttention` rebuilds the banner every 5 s
  (`dashboard.js:983`). That re-enables a button the operator has already clicked while
  its request is still in flight.
- Fix: add a confirmation that says what Dismiss does. Do not offer it for
  identity-verified runs (after #1). Keep a dismissed-but-alive run visible with an
  `operator-dismissed` tag until its real end record arrives.

### 7. Drawer timings charge model latency to tool steps

- Evidence: the engine stamps a usage-journal row when a reply **arrives**, to the
  second (`~/.claude/bin/delegate.py:183-189`). `activity.py:211-225` uses that stamp as
  the step's `started_at`, and the gap to the next row as the step's `duration`. So a
  step's duration is its own tool time plus the whole of the next model call, and the
  live timer on the newest step counts time spent waiting on the model.
- Scenario: the drawer shows "Reading snapshot.py 3m 12s". The read took milliseconds;
  the model has been thinking for three minutes.
- Fix now: label the running timer as the model call, "Waiting on model 3m 12s", using
  the transcript tail from #2. Relabel past durations "until next reply".
- GATED: an exact per-step split needs the engine to journal when each call starts.

### 8. "Last signal" uses a different clock from the liveness logic

`snapshot.py:17-23` uses the transcript mtime only. `sync.py:137-147` uses the newest of
the transcript, the messages journal and the usage journal.

- Fix: reuse the silence that sync has already computed, or derive the sibling paths
  from the transcript path, as `activity.py:205` already does. Not gated.

### 9. No progress, cost or elapsed time in the row

- The row shows: start time, model, effort, label, sent and returned tokens
  (`dashboard.js:343-356`).
- It is missing:
  - elapsed time (`generated_at` minus `timestamp`);
  - cost so far (already in the payload, `snapshot.py:188`);
  - what the agent is doing right now (from #2);
  - the iteration number (from the transcript tail).
- Correction to one agent's proposal: `RunRecord.tool_calls` is always null for a live
  run, because sync sets it only from the end record (`sync.py:275`). A live step count
  has to come from the transcript.
- GATED: "iteration N of M" needs the engine to write `max_iters` into its start record.

### 10. Every page poll re-parses every open transcript under the global lock

- Evidence:
  - Each `GET /api/data` runs a full sync (`dashboard.py:66-73`).
  - Each sync calls `_archive_activity` for every open run (`sync.py:297`).
  - `_archive_activity` re-parses the transcript (up to 4 MB) and the usage journal,
    then rewrites the archive file.
  - All of this runs under the one process-wide write lock (`storage.py:36-38`).
  - That repeats every 5 s for each open tab, and each open drawer re-parses the
    transcript again every 2 s (`dashboard.py:145`).
- Fixes:
  - Throttle sync in `DashboardApp.snapshot` so that several tabs share one sync per few
    seconds.
  - Re-archive only when the transcript's size or mtime has changed.
  - Add an in-flight guard to `load()` and `ctl.poll`. Today a slow response can land
    after a newer one and overwrite it.

### 11. Unsorted, unstable row order

`live` is not sorted (`snapshot.py:250-254`), and `updateLiveTable` appends new rows at
the end and never reorders (`dashboard.js:316-336`).

- Fix: sort runs that need attention first, then by start time, newest first. Move each
  row together with its open drawer.

### 12. One word, two meanings

`abandoned` is both a reversible display guess for an open run (`snapshot.py:43-44`)
and the display name for a persisted terminal `interrupted` (`liveness.py:115-140`).

- Fix: use separate vocabularies, per the state model below.

## P2: polish

13. **The activity poll never checks `response.ok`** (`dashboard.js:485-496`). A 404 or
    500 body renders as "No transcript recorded for this run." (`dashboard.js:388-390`).
14. **Rows cannot be used from the keyboard.** Rows respond to `onclick` only, and have
    no `tabindex`, `role` or `aria-expanded` (`dashboard.js:322-333`).
15. **Finished runs vanish instantly.** Keep a "Recently ended" strip for about 15
    minutes, with each run's terminal status, so a crash gets noticed. History rows
    cannot open the activity drawer at all.
16. **One stall threshold for everything.** The same 300 s applies to every model and
    effort level. An xhigh reasoning call can legitimately run longer than that.
17. **"Today" and "this month" use UTC dates** (`snapshot.py:213-230`), while times are
    shown in local time. Runs started between 00:00 and 02:00 local count toward the
    previous day.
18. **The `#state` line shows the raw UTC `generated_at`** (`dashboard.js:979-981`),
    while the tables show local time.
19. **The live column count is repeated as literals.** It appears in three places
    (`liveHead`, `dashboard.js:307`, `dashboard.js:516`); derive it once.
20. **The Live now header holds the wrong number.** It shows all-time machine spend.
    The figure that belongs there is the live burn: summed cost of the live runs, and
    dollars per minute.

## Target: what a live row should say

| State | Condition | Needs attention |
|---|---|---|
| `running` | No end record, process identity valid (schema 1: recent activity), recent step | No |
| `waiting on model` | Transcript tail is `calling … attempt 1/M`; shows how long | Only past an effort-scaled threshold |
| `retrying N/M` | Attempt 2 or later on the current turn | From attempt 3 |
| `quiet` | Identity valid, nothing written past the threshold, not mid-call | Yes, but no Dismiss |
| `unverified` | Schema 1, or the probe returned "unknown" | Only if silent past the threshold |
| leaves Live now | End record, or process gone (terminal) | An `interrupted` run with no end record, in "Recently ended" |

Suggested columns:

- **State**, with the reason and its evidence in a tooltip (for example "process
  verified 3 s ago; last reply 4 m ago; attempt 1/4").
- **Project**
- **Task**
- **Elapsed**
- **Doing**, for example "reading src/x.py", "waiting on model 2 m" or "retry 3/4"
- **Iteration**
- **Tokens**
- **Cost so far**
- **Model and effort**
- **Last signal**

## Suggested order of work

1. **Accuracy:** #1, #4, #5, #8, #3, #6. These are backend changes plus small
   frontend ones, all testable with temporary paths.
2. **Signals:** #2, #7, #9, #12. One transcript-tail parser feeds the new state model
   and the new columns.
3. **Robustness and polish:** #10, #11, and #13 to #20.
4. **GATED (engine, needs operator approval):** write `max_iters` into the start record,
   and journal when each model call starts. These give "iteration N of M" and exact
   per-step timings.

## Rejected during verification

- **"A reused PID makes a dead run look alive" (reported as P0).** The create-time
  window allows only 5 s after the run's start (`liveness.py:72-77`). A reused PID would
  need a new engine process that started within 5 s of the dead one's start. Not a
  realistic path.
- **"The create-time window rejects a slow engine startup" (reported as P0).** The start
  record is written after the process starts, so its creation time always comes before
  the start timestamp. Only a startup longer than 300 s would fail.
- **"Fixing #1 and #8 is gated."** The verdict and the artifact paths are internal to
  AgentControl. Neither the engine nor the index schema has to change.
- **"The drawer polls forever after the run ends."** It stops when the row leaves the
  live list, within 5 s (`dashboard.js:290-301`).
- **"Every sync reads the whole transcript through `read_footer_usage`."** That happens
  only for runs with no usage journal (`usage.py:443-465`). The real per-sync cost is
  #10.
- **"Show `tool_calls` as the step count."** It is always null for a live run (#9).

## Not verified

- No browser or DOM test was run. The JS suite covers only the pure helpers
  (`tests/js/dashboard.test.mjs`).
- `AccessDenied` for an elevated engine process was not reproduced (#5).
- Sync latency was not measured against transcript size (#10).

## Implementation status (2026-09-24)

Branch: `feat/live-now-accuracy`.

| # | Finding (short) | Status | Commits |
| --- | --- | --- | --- |
| 1 | Display ignores the process check sync just ran | Done | d88553e (sync keeps the verdict), e14cb8f (live state model) |
| 2 | Gateway retry loop looks healthy | Done | 1c06301, 0a38c21 (progress reader), e14cb8f |
| 3 | Failed refresh leaves the table stale | Done | 5256373 |
| 4 | Live now filtered by history filters and approval | Done | e14cb8f |
| 5 | A probe error counts as the process being gone | Done | bb240a6 |
| 6 | Dismiss is one click and hides a live process | Done | 5256373 (confirm, pending actions), e14cb8f (dismissed-but-alive stays visible), f6a0beb (409 for verified-alive) |
| 7 | Drawer timings charge model latency to tool steps | Done | e907c2d (engine journals call start), 17a5de6 (API split), 64a7a1d (drawer UI) |
| 8 | "Last signal" uses a different clock | Done | d88553e, e14cb8f |
| 9 | No progress, cost or elapsed time in the row | Done | e14cb8f, 843a61c |
| 10 | Every poll re-parses every transcript under the lock | Done | 6b3a473 (sync throttle, archive on change), 17a5de6 (activity cache), f6a0beb (cache lock), 5256373 (no overlapping requests) |
| 11 | Unsorted, unstable row order | Done | e14cb8f, 843a61c, 64a7a1d (rows move only when out of place) |
| 12 | One word, two meanings | Done | e14cb8f, 843a61c |
| 13 | The activity poll never checks `response.ok` | Done | 5256373 |
| 14 | Rows cannot be used from the keyboard | Done | 5256373, 843a61c (focus kept across re-render) |
| 15 | Finished runs vanish instantly | Done | d6574f9, 64a7a1d (Recently ended strip, activity panel for history rows), 2495664 (end time from last activity for runs with no end record) |
| 16 | One stall threshold for everything | Done | e14cb8f |
| 17 | "Today" and "this month" use UTC dates | Done | d6574f9 |
| 18 | `#state` line shows raw UTC `generated_at` | Done | 5256373 |
| 19 | Live column count repeated as literals | Done | 5256373 |
| 20 | Live now header holds the wrong number | Done | d6574f9, 843a61c |

### Deviations and open items

- #16 was implemented as a single 12-minute quiet threshold, derived from the
  engine's own 600 s request and command limits, instead of per-effort scaling.
- History and recently-ended rows open activity in a side panel rather than an
  inline drawer.
- The GATED engine changes are committed: the Windows path-guard fix restored,
  `max_iters` in start records, and `started` in usage rows (e907c2d,
  HARNESS_VERSION unchanged at 6). The deploy is pending: it is the operator's
  action.
- The harness policy for this repo was added in 219d523.

### Follow-up fixes after operator feedback

- A second `agent-control serve` on the same port bound successfully on Windows
  (SO_REUSEADDR), so the browser talked to an old server running old code. The
  server now binds exclusively and holds a state-directory lock (69a1f2f).
- The page now detects a server older than itself and says to restart it,
  instead of rendering blank cells, a false "No live runs" and "pending project"
  tags (f95a4dc).
- Refresh took ~4.6 s on the real state (453 runs, ~2,900 ledger rows) because
  every sync re-parsed the ledger once per open run (34 parses). A sync now reads
  it once: ~165 ms per refresh, 510 ms cold (ac26769).

# AgentControl: Narration and Operator Intervention Design

**Date:** 2026-09-24
**Status:** Approved direction. The operator chose the scope, stopping model, content level and layout on 2026-09-24, then asked for implementation without section-by-section review of the rest.
**Amends:** `2026-09-17-agent-control-design.md` (Release 1 scope; see section 3)
**Engine change:** none. Everything here works with the installed v6 engine and with every transcript already on disk.

## 1. Purpose

The operator wants to see what a delegated agent is doing and why while it runs, and to act on what they see. Acting means one of two things:
- **steer**: send guidance that the agent receives at its next turn;
- **kill**: stop the agent now.

An agent may be stalled, retrying, or working in the wrong direction.

## 2. Decisions

| # | Question | Decision |
|---|---|---|
| D1 | Release boundary | Amend Release 1. Release 1 has not shipped, so the work stays in it. |
| D2 | Stopping | **Kill** terminates the verified engine process and its child processes immediately. **Ask to wrap up** queues a canned steer asking the agent to call `finish` with its report. No engine stop signal. |
| D3 | Content shown | Narration, tool calls, tool output (the engine's 1,500-character excerpt), operator guidance, notices and the final report. Tool output and the task brief are folded by default. Obvious credentials are masked on display. |
| D4 | Layout | A non-modal side panel on the right. The rest of the page stays live beside it. It replaces the live-row step drawer and the modal `#activity-panel`. |
| D5 | Source of narration | Parse the Markdown transcript on demand. Rejected: the `.messages.jsonl` journal, which has no heartbeats and untruncated output, and an engine-written narration journal, which would need an engine change. |
| D6 | Delivery to the page | Polling every 2 s while the panel is open. A streaming connection is rejected: one thread per stream in a stdlib server is fragile, and it would save only about 2 s. |

## 3. Amendments to Release 1

These amendments land before any implementation, in one commit.

1. **`AGENTS.md` content rule.** Replace the content bullet with:
   > Never place prompts, assistant content, tool results, credentials, response bodies, or complete sidecars in source, fixtures, logs, reports, AgentControl state, or APIs. The one exception is `GET /api/narration`, which reads a run's own transcript on demand for the loopback operator and never stores, logs, or writes it to disk. Test fixtures use synthetic text only.
2. **`AGENTS.md` scope rule.** Replace the Release 1 bullet with:
   > Release 1 has no dispatch, budget enforcement, database, frontend framework, or remote binding. Its only process control is the operator's verified kill of a run's engine process tree, and its only input to an agent is the operator's steer through the engine's own `.steer` file.
3. **Base spec §1, §2, §3, §16 and §24.** Amend them to match the two rules above, and add goal 12: show a run's narrated transcript on demand, and let the operator steer or kill a verified-alive run.
4. **`README.md` line 5.** Amend it to match.

`activity.py` and its security invariant do not change. It stays metadata-only because it feeds the on-disk activity archive. Narration is a separate module with a separate endpoint.

## 4. What the engine already provides (v6, verified in `engine/delegate.py`)

- **The system prompt asks for narration.** Its words: "Narrate briefly what you are doing and why before each tool call: a human is watching this run live and may redirect you."
- **The transcript is written live.** It is flushed and fsynced on every write, and holds:
  - the header;
  - the task;
  - a heartbeat line before every model-call attempt;
  - each step's narration, tool call and output;
  - operator guidance;
  - notices;
  - the final report;
  - a footer.
- **Steering is built in.** Before each turn the engine reads `<transcript>.steer`. When the file is non-empty, the engine:
  1. injects `[operator guidance] <text>` as a user message;
  2. writes `### [N] OPERATOR GUIDANCE` to the transcript;
  3. then truncates the file.

  The file is not read between retry attempts inside one turn.
- **Process identity.** The index start record carries `engine_pid` (schema 2). `liveness._process_identity_valid(start, snapshot)` accepts a PID only when all hold:
  - the process exists and is running (not a zombie);
  - the process creation time falls between 300 s before and 5 s after the start record (a pid cannot be reused while its live owner holds it);
  - the executable (`os.path.basename(command_line[0])`) is a Python interpreter such as `python.exe` or `pythonw.exe`. The engine may be loaded by a wrapper script, so no particular script path is required.
- **Abrupt termination leaves no end record.** `RunLifecycle` writes an `interrupted` end record from atexit and signal handlers. On Windows, `TerminateProcess` runs neither, so a killed run has no end record. The next sync classifies it as `process_gone`, which is displayed as abandoned and persisted as `interrupted`.

**What is not available**, and the page must not pretend it is:
- **Hidden reasoning.** `claude-opus-4-8-think` returns empty `<think></think>` blocks through this gateway: 1,524 of them in 314 transcripts, and only 40 older `claude-opus-4-6-think` blocks carry text. GPT models return none. So "thinking" means the agent's own narration, plus the `<think>` text when a model supplies it.
- **Streaming.** The engine uses `stream: false`. Narration appears once per step, when the reply arrives.
- **A graceful stop.** Ask to wrap up is cooperative. Kill is abrupt, so its consequences depend on the run's write mode:
  - staged runs lose their unapplied edits;
  - `--write` edits are already in the repository;
  - `--worktree` edits stay in `.claude\worktrees\<label>` on branch `agent/<label>`.

## 5. Components

| Unit | Responsibility | Depends on |
|---|---|---|
| `src/agent_control/narration.py` (new, pure) | Parse transcript text into a header, the brief and typed events. Mask credentials. Read a file with a size cap. | nothing |
| `src/agent_control/steering.py` (new) | Append to and cancel from `<transcript>.steer`. Compute delivery state. | nothing (pure functions plus file I/O on the steer path) |
| `src/agent_control/process_control.py` (new) | Re-check the process identity, then terminate the engine process tree. psutil is injected. | `liveness._process_identity_valid`, `models.ProcessSnapshot` |
| `src/agent_control/operator_log.py` (new) | Append-only `operator-actions.jsonl` in the state directory, with metadata only, and read it back. | `storage.append_jsonl`, `storage.read_jsonl` |
| `src/agent_control/sync.py` | `OpenRunObservation` gains `start: StartEvent \| None`. | `run_index.StartEvent` |
| `src/agent_control/snapshot.py` | Live rows gain `can_steer`, `can_kill` and `control_reason`. Ended rows gain "killed by operator". | `operator_log` |
| `src/agent_control/dashboard.py` | New routes and `DashboardApp` methods, a Host check on every GET, the page CSP, and static files for `panel.js` and `panel.css`. | all of the above |
| `web/panel.js`, `web/panel.css` (new) | The side panel: timeline, controls and polling. Pure helpers are exported for `node --test`. | the two GET endpoints and three POST endpoints |
| `web/dashboard.js`, `web/index.html`, `web/dashboard.css` | Open the panel from any run row. Remove the live-row drawer and `#activity-panel`. | `panel.js` |

## 6. Narration

### 6.1 Transcript grammar (engine v6)

Read the file in text mode with universal newlines. Engine transcripts on Windows are CRLF. Strip any stray `\r` at line ends.

**The header** is written in a single write:

```text
# <label>

- model: `<model>`  effort: `<effort>`  tools: `<native|prompt>`
- writes: `<disk|staged (patch only)>`  root: `<path>`
- <policy note>
- steer: write guidance into `<name>.steer` to redirect mid-run

## Task

<task brief: any number of lines; it may itself contain --- lines>

---
```

`header.worktree` is true when the `root` value contains `.claude\worktrees\` or `.claude/worktrees/`.

**Body elements** each begin at the start of a line. An element runs until the next element begins.

| Element | First line | Body |
|---|---|---|
| model call | `_[ N] calling <model> (attempt a/b, m msgs, ~k bytes) ..._`<br>regex `^_\[\s*(\d+)\]\s+calling\s+\S+\s+\(attempt\s+(\d+)/(\d+)` | none |
| narration | `### [N] thinking` | Lines up to the next element. `<think>…</think>` spans are *reasoning*; the rest is *prose*. |
| guidance | `### [N] OPERATOR GUIDANCE` | The first non-blank line starts with `> ` (strip it). Later lines are verbatim. |
| tool | `### [N] <name>(<args>)`, parsed with `activity._STEP`. `args` is the JSON cut at 300 characters, so it may be invalid JSON. | A fenced block. The output is the text between the first ` ``` ` line and the **last** ` ``` ` line of the element, because output can itself contain fences. It is cut when it ends with `...[truncated in transcript]`. |
| anomaly | a tool header whose name is `NO_TOOL_ACTION` or `PARSE_ERROR` | a fenced short message |
| finish | a tool header whose name is `finish` | fenced `ok` |
| notice | one whole line matching an engine notice, listed below | none |
| final report | a `---` line followed by `## Final report` | text up to a notice, the footer, or the end of the file |
| footer | `^\[[^\]]*: [\d,]+ tool calls[^\]]*\]$` | none; parsed and dropped |

**Notices** are recognised only by these exact patterns:
- `^\*\*model returned no tool action \d+ times; giving up and requesting a wrap-up\*\*$`
- `^\*\*hit the \d+-iteration cap; requesting a wrap-up\*\*$`
- `^\*\*worktree .+: \d+ file\(s\) changed -> .+\*\*$`
- `^\*\*staged \d+ file\(s\) -> .+\*\*( \(review: .+\))?$`

**The brief and where the body starts.** A task brief can itself contain lines that look like body elements. Briefs for transcript tooling quote transcripts, for example. So the boundary is found in this order:

1. **Exact.** When the run's `.messages.jsonl` supplies the task text (its first `user` message) and the transcript contains `## Task\n\n<task>\n\n---\n`, the brief is that task text, and the body starts right after it.
2. **Structural fallback.** The body starts at the first turn-1 element that directly follows a `---` line and one blank line. A turn-1 element is a heartbeat for turn 1, or a `### [1] …` header. The brief is the text between `## Task` and that `---`.
3. **Last resort.** If neither applies, the body starts at the first body element, and the brief runs to the last `---` line before it, or to the end of the file.

Strip leading and trailing blank lines from the brief.

### 6.2 Events

Every event has these fields:
- `seq`: its 0-based index in the parse;
- `kind`;
- `iteration`: an int, or null for a notice or the final report.

| kind | extra fields |
|---|---|
| `call` | `attempt`, `max_attempts` |
| `narration` | `text` (prose, stripped), `reasoning` (str, or null when empty). Skip the event when both are empty. |
| `tool` | `name`, `args` (raw string), `output` (str or null), `output_truncated` (bool) |
| `anomaly` | `name` (`NO_TOOL_ACTION` or `PARSE_ERROR`), `output` |
| `guidance` | `text` |
| `finish` | none |
| `notice` | `text` (without the `**` markers) |
| `final` | `text`, `partial` (true when the text starts with `[PARTIAL`) |

### 6.3 Masking

`mask_secrets(text) -> tuple[str, int]` applies these rules in order and returns the masked text and the number of spans it replaced:

1. **Private-key blocks.** `-----BEGIN [A-Z ]*PRIVATE KEY-----` through the matching `-----END … PRIVATE KEY-----`, or to the end of the text when the END line was cut off, becomes `[masked private key]`.
2. **`sk-` keys.** `\bsk-[A-Za-z0-9_\-]{16,}` becomes `sk-[masked]`. This covers Open WebUI, OpenAI and Anthropic keys.
3. **Bearer tokens.** `(?i)\bbearer\s+[A-Za-z0-9._~+/=\-]{16,}` becomes `Bearer [masked]`.
4. **AWS access keys.** `\bAKIA[0-9A-Z]{16}\b` becomes `[masked AWS key]`.
5. **GitHub tokens.** `\bgh[pousr]_[A-Za-z0-9]{36,}` becomes `[masked GitHub token]`.
6. **Slack tokens.** `\bxox[abprs]-[A-Za-z0-9-]{10,}` becomes `[masked Slack token]`.
7. **JWTs.** `\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}` becomes `[masked JWT]`.
8. **Named secrets.**
   - Pattern: `(?i)\b(api[_-]?key|secret|token|password|passwd|pwd|access[_-]?key)\b(\s*[:=]\s*)(["']?)([A-Za-z0-9_\-+/=.]{12,})\3`
   - The key name, separator and quotes are kept, and only the value becomes `[masked]`.

Masking applies to every text field returned by the API: the brief, narration text and reasoning, tool args and output, anomaly output, guidance, notices and the final report. It is a courtesy for screen sharing, not a security boundary: the transcript on disk is unmasked.

### 6.4 API

`GET /api/narration?run_id=<id>&after=<n>`

- Requires a valid `Host` header (section 9) and `X-AgentControl-CSRF: <token>`. Otherwise it returns 400 `bad_host` or 403 `bad_csrf`.
- The run comes from the memoised ledger state (`_load_state`); an unknown run returns 404 `not_found`. The transcript path is the record's `transcript_artifact`.
- `after` is an integer ≥ 0 and defaults to 0; anything else returns 400 `invalid_query`.

A 200 response looks like this:

```json
{
  "run_id": "…",
  "status": "running",
  "available": true,
  "header": {"label": "…", "model": "…", "effort": "…", "tool_mode": "native",
             "writes": "disk", "root": "…", "worktree": true},
  "brief": "…",
  "events": [ … ],
  "total": 57,
  "masked": 0,
  "truncated": false,
  "steer": [ … ]
}
```

- `brief` is non-null only when `after == 0`.
- `events` is `events[after:]`.
- `total` is the number of events parsed so far.
- `masked` counts the masked spans across the brief and ALL events of the transcript, whatever `after` is, so the panel's note stays stable while it polls. Each transcript version is masked once, in memory.
- `truncated` is true when the file exceeded the read cap.
- `steer` lists steer statuses, described in section 7.4.
- When the transcript is missing or unreadable, `available` is false, `header` and `brief` are null, `events` is empty and `total` is 0.

**The cursor protocol:**
- The last event may still be growing; for example, a tool's output is written after its header.
- The client therefore polls with `after = max(0, total - 1)`, and replaces every event whose `seq` is at or above `after`.
- The server holds no per-client state.

**Limits.**
- Read at most 8 MB. If the file is larger, parse the first 8 MB, set `truncated`, and end with a notice: "transcript larger than 8 MB; later steps are not shown".
- Keep parses in an in-memory LRU of 8, keyed by path and `(st_size, st_mtime_ns)`.
- Nothing is written to disk, and nothing reaches the ledger, the activity archive, the operator log or any server log.

## 7. Steering

### 7.1 Endpoint

`POST /api/runs/{run_id}/steer`, with body `{"text": "<guidance>"}` or `{"kind": "wrap_up"}`.

**Preconditions, checked in this order:**
1. **The body shape.** A body must carry exactly one of `text` or `kind`, and `kind` must be `"wrap_up"`. Otherwise it returns 400 `invalid_request`.
2. **The text.** Trimmed, it must be 1 to 4,000 characters, and the only control characters allowed are `\n` and `\t`. Otherwise it returns 400 `invalid_text`.
3. **The run is open.** It must be in `synchronizer.open_runs` (no end record). Otherwise it returns 409 `not_open`.
4. **The engine is verified alive.** The observation's `liveness.code` must be `process_identity_valid`. Otherwise it returns 409 `not_verified`.
5. **The transcript.** `start.transcript` must end in `.md` and exist as a file. Otherwise it returns 409 `no_transcript`.
6. **The steer path.** `<transcript>.steer` must be a regular file or absent: not a directory and not a symlink. Otherwise it returns 409 `bad_steer_path`.

**The wrap-up text:** "Stop working now. Do not call any tool except finish. Call finish with your report: what you completed, what you verified and how, and what is still incomplete or unverified."

### 7.2 File protocol

- The path is the transcript path with `.steer` appended, as the engine forms it.
- Writes are serialised per run with an in-process lock.
- Existing content is never overwritten, because the orchestrator may also have written guidance:
  1. read the existing content;
  2. append `"\n\n"` when that content is non-blank;
  3. append the trimmed text and `"\n"`, as UTF-8 with no newline translation;
  4. fsync.
- A 200 response carries the run id, `state: "queued"`, `id`, `chars`, `kind` and `sent_at`.

### 7.3 Cancel

`POST /api/runs/{run_id}/steer/cancel`, with body `{"id": "<steer id>"}`.
- Under the run's lock, if the file still contains the pending text exactly, rewrite the file without that segment and its separator. The response is `{"state": "cancelled"}`.
- Otherwise the response is 409 `not_pending`.

### 7.4 Delivery tracking

`DashboardApp` keeps a pending-steer record for each accepted steer, in memory only:
- a random `id`;
- `run_id`;
- the trimmed text;
- `kind`;
- `chars`;
- `sent_at`;
- `events_at_send`, the narration `total` at send time.

It keeps at most the five newest per run. `/api/narration` reports each record as one of these states, checked in this order:

1. **`delivered`, with `turn = N`.** A `guidance` event with `seq >= events_at_send` contains the text, after both are whitespace-normalised. The match uses the raw parse, not the masked one.
2. **`undeliverable`.** The run is no longer open.
3. **`queued`.** The steer file still contains the text.
4. **`lost`.** None of the above. The file was overwritten, or the engine truncated it while the append was in flight.

Each entry is `{id, state, kind, chars, sent_at, turn}`. The text is never echoed back. Pending records do not survive a dashboard restart; after one, delivered guidance still shows in the timeline as a `guidance` event.

## 8. Kill

`POST /api/runs/{run_id}/kill`, with body `{"confirm": true}`.

**Preconditions:**
1. `confirm` must be `true`, or it returns 400 `confirm_required`.
2. The run must be in `open_runs`, or it returns 409 `not_open`.
3. The observation needs a `start` with a non-null `engine_pid`, or it returns 409 `no_process_identity`.
4. The PID must not be the dashboard's own, or it returns 409 `refused_self`.

`process_control.terminate_engine(start, *, psutil_module, grace_seconds=5.0) -> KillResult`:
1. **Open the process.** `p = psutil.Process(start.engine_pid)`. A `NoSuchProcess` gives outcome `already_gone`.
2. **Check identity.** Build a `ProcessSnapshot` from `p` (its cmdline, `create_time()` and running flag) and call `_process_identity_valid(start, snapshot)`. False raises `NotVerified`, which the API returns as 409 `not_verified`, and nothing is touched. An `AccessDenied` here or later returns 409 `access_denied`.
3. **List children first.** `children = p.children(recursive=True)`, before anything is terminated: on Windows, orphans can no longer be found through the parent.
4. **Terminate the parent.** `p.terminate()`. psutil re-checks PID and creation time, so a recycled PID raises `NoSuchProcess`.
5. **Terminate each child,** ignoring `NoSuchProcess`.
6. **Wait, then force.** `psutil.wait_procs([p, *children], timeout=grace_seconds)`. Call `kill()` on survivors, then wait 2 s more.
7. **Return** `KillResult(outcome="terminated"|"partial"|"already_gone", engine_pid, children, survivors)`. The outcome is `partial` when anything survives.

A 200 response carries these fields plus `run_id`. The next sync sees `process_gone` and persists `interrupted` with no further work.

The UI states the consequences from the narration header before asking for confirmation:
- **staged:** its edits exist only in the engine's memory and are lost, and there will be no final report;
- **worktree:** its edits stay in the worktree and on its branch;
- **disk:** its edits are already in the repository.

## 9. HTTP security

- **The Host check on every GET.** Every GET (static files and API) requires `Host` to be `127.0.0.1:<port>` or `localhost:<port>`, and otherwise returns 400 `bad_host`. This blocks DNS rebinding, which could otherwise read the page's CSRF token and then the narration.
- **POST checks are unchanged:** Host `127.0.0.1:<port>`, same-origin `Origin`, the CSRF header and a JSON body.
- **The `index.html` response headers.** It gains:
  - `Content-Security-Policy: default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; object-src 'none'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'`;
  - `X-Frame-Options: DENY`;
  - `Referrer-Policy: no-referrer`.

  Framing is forbidden so a hostile page cannot trick a click on Kill.
- **Content hygiene.**
  - The page renders transcript text with `textContent` or through `esc()`, and never inserts it as HTML.
  - Error responses and server logs never contain request bodies or transcript text.

## 10. Operator log

This is an append-only file at `<state_dir>/operator-actions.jsonl`, written with `storage.append_jsonl` under `locks.write`. Each record is metadata only:

```json
{"schema": 1, "ts": "<UTC ISO>", "action": "steer|steer_cancel|kill", "run_id": "…",
 "outcome": "queued|cancelled|terminated|partial|already_gone|not_verified|access_denied",
 "kind": "steer|wrap_up|null", "chars": 123, "engine_pid": 4321, "children": 2, "survivors": 0}
```

Fields that do not apply are `null`. The record holds no text and no hash of the text. `read_actions(state_dir)` returns the valid records and ignores malformed lines. It memoises the parse on the file's `(st_size, st_mtime_ns)`. `killed_run_ids(actions)` returns the runs with a `terminated` or `partial` kill.

## 11. Snapshot changes

- **Live rows.**
  - `can_kill`: the engine is verified alive and `engine_pid` is known.
  - `can_steer`: the engine is verified alive, the row is not dismissed, and a transcript is recorded.
  - `control_reason`: null when both are true. Otherwise, the first that applies of:
    - "no process identity (schema-1 run)";
    - "engine process not verified alive";
    - "no transcript recorded";
    - "dismissed by operator".
- **Recently ended rows.** A run in `killed_run_ids` gets `end_reason` "killed by operator", which takes precedence over the other reasons.
- **History rows.** They gain `killed_by_operator` (bool).
- **Plumbing.** `build_snapshot(..., operator_actions=None)` takes the parsed actions; `DashboardApp.snapshot` passes `operator_log.read_actions(state_dir)`.

## 12. The side panel (`web/panel.js`)

**Structure.**
- A non-modal `<aside id="run-panel">`, fixed to the right at `min(46vw, 720px)`.
- While it is open, `body.run-panel-open` gives `main` a matching right margin, so every table stays visible and live. Below 900 px the panel covers the page instead.
- `Esc` and a close button close it. Focus moves to the panel title on open and returns to the opener on close.
- The URL hash `#run=<id>` restores the panel on reload.

**Header:**
- label and project;
- the state badge, using the Live now badge classes;
- elapsed time;
- iteration "x of N";
- cost so far;
- model and effort;
- write mode: staged, disk or worktree.

The panel refreshes these from each `/api/data` render.

**Timeline** (`role="log"`, `aria-live="polite"`):
- **Task brief:** folded.
- **One group per iteration.** The group header shows the turn number, the reply time and the model and tool seconds from `/api/activity`. Under it, in order:
  - the narration prose;
  - the reasoning, muted and folded when longer than 3 lines;
  - each tool call on one compact line, with its output folded and labelled with its size and "truncated" when cut;
  - anomalies, in the warning style.
- **Operator guidance:** highlighted as "Guidance, turn N".
- **Notices,** then **the final report,** open.
- **Tool-call summaries** (the `summarizeToolCall` helper):

  | Tool | Shown as |
  |---|---|
  | `read_file` | path and line range |
  | `grep` | pattern and glob |
  | `run_command` | argv joined |
  | `write_file`, `replace_in_file` | path |
  | `list_files` | path |
  | anything else, or invalid JSON | the name and the first 120 characters of `args` |

- **Current-phase line** at the bottom, for example "Model call running 36s (attempt 1/4)". It uses `phaseText` from `dashboard.js`, passed in, and a 100 ms ticker.
- **Scrolling.** The view auto-scrolls when within 40 px of the bottom. Otherwise a "N new steps" button jumps to the latest.

**Controls** (live runs only):
- **Steer.**
  - A textarea with a 4,000-character maximum; Ctrl or Cmd+Enter sends.
  - Each pending steer gets a status line:
    - "Queued: arrives at the agent's next turn"
    - "Delivered at turn N"
    - "Not delivered: the steer file was overwritten or consumed without it; send again"
    - "Not delivered: the run ended"
  - While a steer is queued, it can be cancelled.
- **Ask to wrap up** asks for confirmation first.
- **Kill…** asks for confirmation with the consequences for the run's write mode, then reports the outcome, for example "Engine and 2 child processes terminated".
- **Disabled state.** When `can_steer` or `can_kill` is false, the control is disabled and `control_reason` is shown.

**Polling:**
- For an open run, `/api/narration` and `/api/activity` every 2 s. For an ended run, one fetch.
- Paused while `document.hidden`. Stopped on close.

**Module shape.**
- `panel.js` is an ES module.
- Its pure helpers are exported for `node --test`.
- Its DOM code runs only when `document` exists.
- It exports `createRunPanel(deps)`, which returns `{open(runId, opener), close(), update(data), isOpen(), currentRunId()}`. `deps` holds:
  - `fetchJson`;
  - `csrfToken`;
  - the formatters `phaseText`, `stepTimingText`, `formatElapsed`, `formatCost`, `iterationText` and `liveStateClass`;
  - `onChange`, called after a steer, cancel or kill so the dashboard reloads.

`dashboard.js` imports it with `import { createRunPanel } from "./panel.js";`. This works from `/static/` in the browser and from `web/` under node.

## 13. Error handling

| Situation | Behaviour |
|---|---|
| Transcript missing or unreadable | `available: false`. The panel says the transcript was not found and shows `/api/activity` steps, from the live transcript or the archive. |
| Unknown run | 404 on every endpoint |
| Steer or kill on a run that is not verified | 409 with a code. The panel shows the reason and disables the control. |
| Identity mismatch at kill time | 409 `not_verified`. Nothing is terminated. |
| Termination denied (for example, an elevated process) | 409 `access_denied` |
| Processes survive the kill | 200 with `outcome: "partial"` and `survivors > 0`. The panel warns. |
| Dashboard restarted with a steer pending | The status is lost. Delivered guidance still appears in the timeline. |

## 14. Testing

**Every test injects temporary paths and uses synthetic transcript text.**

- **`narration`.**
  - Native mode with several tool calls in one turn.
  - Prompt mode.
  - Empty and non-empty `<think>`.
  - Multi-line guidance.
  - Retries (several heartbeats in one turn).
  - `NO_TOOL_ACTION`, `PARSE_ERROR` and `finish`.
  - The cap and give-up notices.
  - A worktree note and a staged note.
  - The final report, full and `[PARTIAL`.
  - The footer.
  - A brief containing `---`.
  - Output containing a fence.
  - A truncated output.
  - A partial last element.
  - CRLF input.
  - The 8 MB cap.
  - Every masking rule, plus non-matches.
- **`steering`.**
  - Append to an absent, empty or non-empty file.
  - Refuse a directory or symlink steer path.
  - Cancel only an exact pending segment.
  - All four delivery states.
  - The five-per-run limit.
- **`process_control`.**
  - With a fake psutil: identity mismatch leaves every process untouched; children are listed before the parent is terminated; survivors are force-killed; `already_gone`; `access_denied`.
  - One real-process test: a temporary `<tmp>/.claude/bin/delegate.py` that starts a sleeping child. It is killed through `terminate_engine`, and both processes must be gone.
- **`operator_log`.** Append and read back, malformed lines ignored, the memo, `killed_run_ids`, and no text fields in any record.
- **API.**
  - The Host check on GET.
  - The CSRF requirement on narration.
  - The cursor and brief rules.
  - `available: false`.
  - Each steer and kill precondition and code.
  - The CSP and frame headers on `index.html`.
  - Nothing new is written under the state directory by narration requests.
- **Snapshot.** `can_steer`, `can_kill`, `control_reason`, the "killed by operator" reason, and `killed_by_operator`.
- **Web.**
  - Node tests for every exported panel helper.
  - Contract checks: `panel.js` never assigns transcript text to `innerHTML`; `index.html` loads `panel.css`; the old drawer and `#activity-panel` are gone.
- **End to end.** A fake engine script on a temporary machine index writes a transcript turn by turn and honours `.steer` the way the real engine does. Against a real `create_server` it proves narration growth, steer delivery at turn N, and kill ending in `interrupted` with "killed by operator".
- **Manual.** A headless-Chrome smoke of the panel against a synthetic running engine.

## 15. Out of scope

- An engine stop signal (`.stop` checked between turns and retries).
- Steering between retry attempts.
- Streamed replies.
- A structured narration journal.
- Better narration prompts.
- Kill for schema-1 runs.
- Remote access.

Each would need an engine change or a remote-access decision, and belongs to a later, separately approved design.

## 16. Acceptance criteria

1. Opening any run shows its narrated timeline, and new steps appear within 3 s of the engine writing them.
2. The brief and tool output are folded, and credentials matching section 6.3 are masked.
3. A steer is shown as queued, then "delivered at turn N", and appears in the timeline as guidance.
4. Ask to wrap up queues the canned steer.
5. Kill terminates only a verified engine and its descendants. The run is recorded as `interrupted` with "killed by operator" within one refresh. The operator log holds metadata only.
6. The ledger, activity archive, operator log, sync state and server logs contain no transcript text.
7. A GET with a bad Host returns 400, narration without the CSRF header returns 403, and the page cannot be framed.
8. `python -m pytest -q` and `node --test tests/js/` pass, and the headless smoke passes.

## 17. Implementation status (2026-09-24)

All eight acceptance criteria in §16 are met. Delegated agents on the OpenWebUI harness built the work, and the orchestrator reviewed every patch before applying it.

| Part | Commits |
|---|---|
| Spec, plan and Release 1 amendments | `efc5070`, `1778efa` |
| Steer-file protocol and delivery tracking | `b7d24f9` |
| Verified kill and metadata-only operator log | `c33b83f` |
| Live-row control flags; "killed by operator" | `ab55749` |
| Narration parser; fence-safe tool output | `38d4fd0`, `f63b475` |
| Endpoints, the Host check on GET, the page CSP | `dfe1f10` |
| Side panel, its DOM fixes, dashboard wiring, polish | `92f780c`, `e955b81`, `ed84488`, `637c3a2`, `5512b02` |
| End-to-end test against a real process | `af7f64e` |
| Operator guide | `691cf69` |

### Deviations from the text above

1. **§6.1, the brief boundary.** The exact task text from `.messages.jsonl` is tried first. A brief that quotes transcript lines otherwise ended the brief early.
2. **§6.1, tool output.** Lines inside a tool's fenced output, which is bounded by the engine's 1,500-character excerpt, are never classified as elements. Before this fix, four of 326 real transcripts had steps running backwards, because tool output quoted transcript lines. None do now.
3. **§6.4, `masked`.** It counts the whole transcript, not the returned slice. The per-slice count made the panel's note vanish after the first poll.
4. **§12, the panel API.** `createRunPanel` accepts an optional `deps.onClose`, which the dashboard uses to clear the row selection. The panel also has an always-visible action-status line for kill outcomes and action errors.
5. **§8, a second kill.** Killing a run that has already ended, but whose index still has no end record, returns 200 `already_gone`. That is Review Focus item 3.

### Verification

- **Automated tests.** `python -m pytest -q` passes 421 tests. The node tests pass too: 25 for the panel, including fake-DOM tests of its polling and rendering, and 20 for the dashboard.
- **Real transcripts.** Every transcript on this machine parses, across AgentControl, Projectb and Projecta (326 files): no errors, at most 10 ms each.
- **Browser smoke.** A fake engine process and a real `agent-control serve` ran under headless Chrome. The panel rendered the timeline, the folded brief, masked output and delivered guidance. A kill ended the run as "killed by operator", and no transcript, steer or secret text reached the state directory.
- **A real engine run.** The installed shared engine was run isolated, via `CLAUDE_AGENT_RUN_INDEX` and temp folders. A steer was delivered at turn 3, and the agent acted on it. The kill terminated the verified engine, and the run ended as "killed by operator" with no end record.

### Not verified

- A kill while a real engine is inside `run_command`. Descendant termination is proven with the fake engine's child process, and the real run had no child at kill time.
- `access_denied` against an elevated process. It is unit-tested only.
- Losing a staged run's edits on kill. This happens by design and is stated in the confirmation.

### Found outside this repository

`~/.claude/bin/agent_await.py` can report a run as finished too early. It happened when a tool output contained a footer-like line, which a test fixture quoted. That is the same class of defect as deviation 2. It belongs to the shared harness, and this work did not change it.

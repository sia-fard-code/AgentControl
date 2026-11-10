# Usage from Claude Code, Codex, Cline and Kilo: design

- **Date:** 2026-10-01.
- **Status:** approved. The operator chose the scope (live sessions, and their usage counted in totals) and delegated every other decision ("do what is the correct and the best").
- **Extends:**
  - `2026-09-17-agent-control-design.md`, for accounting, projects and pricing;
  - `2026-09-24-repo-run-index-design.md`, for the per-file checkpoint pattern;
  - `2026-09-24-narration-and-intervention-design.md`, for Live now rows. Controls stay engine-only.

## 1. Goal

AgentControl today counts tokens only for runs launched through the engine. This feature adds four tools that do not go through the engine: Claude Code, Codex, Cline and Kilo.

- **Live sessions.** While a session is active it appears in **Live now**. When it goes quiet it moves to **Recently ended**. These rows are watch-only.
- **Totals.** Their tokens and list-price cost count in the today, month and all-time totals, the project totals and the spend tables. Each of these is split by tool.

## 2. Decisions

1. **A second append-only ledger.** It holds one record per model call, kept separate from the run ledger. Two alternatives were rejected:
   - *A pseudo-run per session in the run ledger.* Sessions keep growing, so every sync would append another revision. A run has one model and one day, but sessions switch models and span days. Kill, steer and narration would need an engine-only guard everywhere.
   - *Reading the logs on every refresh and storing nothing.* History would shrink as Claude Code deletes transcripts after 30 days, and every refresh would re-parse about 500 MB.
2. **Numbers only.** Readers parse the tools' files in memory. They keep only the tool, call and session ids, time, model id, working folder, token counts and a speed tier. Titles, prompts, replies, tool inputs and tool outputs are never stored. Claude Code's and Kilo's session titles, and Cline's task text, are all derived from prompts.
3. **List-price cost.** Cost comes from `config/pricing.json` and the pricing history, exactly as for engine runs. The tools' own cost fields are ignored: Cline and Kilo record $0 for gateway calls. A model without a price shows "no rate" (null), never $0.
4. **Same project rules as runs.** A call's working folder is resolved to its repository root and registered through `ProjectRegistry.discover`. The call counts only once that project is approved.
5. **Off until the operator turns it on.** `agent-control import-usage` previews the import and writes nothing. `agent-control import-usage --apply`, run while the dashboard is stopped, imports the history and enables those tools. After that, the dashboard's sync imports new calls.
6. **Watch-only.** Tool sessions have no kill, steer, narration or dismiss. They have no run id, so those endpoints cannot address them.
7. **Out of scope:**
   - Limit views. Codex logs the percentage of its limits used; Claude Code logs only the moments a limit was hit.
   - The old Kilo Code extension store, `globalStorage\kilocode.kilo-code`: 242 requests in April 2026 that may overlap Kilo's migrated "legacy" sessions.
   - VS Code forks. They can be reached with a path override (§6.1).
   - Session titles.

## 3. What is on this machine (measured 2026-10-01)

| Tool | Store | Contents |
|---|---|---|
| Claude Code | `%USERPROFILE%\.claude\projects\<slug>\<session>.jsonl`, plus `<session>\subagents\agent-*.jsonl` | 36 files (25 of them subagents), 120 MB, calls from 2026-08-09, 5,631 calls. Models: `claude-opus-5`, `claude-opus-5-5`, `claude-sonnet-5`. `cleanupPeriodDays` is unset, so files older than 30 days are deleted. |
| Codex | `%USERPROFILE%\.codex\sessions\YYYY\MM\DD\rollout-*.jsonl` (`CODEX_HOME` overrides the home) | 51 files (19 of them guardian subagents), 116 MB, 2025-10-03 to today. 2,329 per-response `token_usage_record`s in 31 files; 20 older files have only `token_count` events. |
| Cline | `%APPDATA%\Code\User\globalStorage\saoudrizwan.claude-dev\tasks\<task>\ui_messages.json` and `state\taskHistory.json` | 89 tasks, 288 MB, 2026-07-23 to 2026-09-25. 11,650 requests, all provider `openai` (the gateway), on `gpt-5.6-terra`, `gpt-5.6-sol` and `gpt-5.6-luna`. Cost is recorded as 0. |
| Kilo | `%USERPROFILE%\.local\share\kilo\kilo.db` (SQLite, WAL) | 386 sessions (175 of them subagents), 19,221 assistant messages, 2026-03-28 to today, 81% on `anthropic_tool_call.claude-opus-4-8-think`. Cost is recorded on only 39. |

Measured facts behind the reader rules in §5:

- **Claude Code**
  - `usage.iterations` always has one entry, and it equals the top-level usage in 13,559 of 13,563 cases. It is a breakdown, not extra usage.
  - 3,835 of the 5,631 calls are written on more than one line. In 258 of those the numbers differ, and in 257 the last line has the largest output.
  - No call id appears in two files.
  - Subagent lines carry the parent's `sessionId`, an `agentId`, and `isSidechain: true`.
- **Codex**
  - In files that have records, the per-response record usage adds up to the thread's own final total in 30 of 31 files.
  - In one compacted thread the cumulative counters jumped by 17M tokens with no per-response usage behind them. The cumulative counters are therefore not trusted.
  - Where both exist, summing `last_token_usage` on each change of the cumulative total comes within 1% of the records overall.
  - A guardian child's usage is never included in its parent's records.
  - `token_usage_record.thread_id` always equals `session_meta.id`. `session_id` is shared by a thread and its subagent threads.
  - The record's `turn_id` matches an earlier `turn_context` in 2,328 of 2,329 cases.
- **Cline:** `cacheReads` never exceeds `tokensIn` (0 of 11,645 requests) and is 95% of it in total. So `tokensIn` includes cache reads, as OpenAI-compatible usage does.
- **Kilo**
  - `reasoning` is larger than `output` in 203 of the 292 messages that have reasoning, so `output` excludes reasoning.
  - `total` equals input + output + cache + reasoning in 258 of those messages.

## 4. The call record

### 4.1 Fields

The record is stored in `state/tool_usage.jsonl`. It is append-only, with one JSON object per line.

| Field | Type | Meaning |
|---|---|---|
| `schema` | int | `1` |
| `tool` | str | `claude_code`, `codex`, `cline` or `kilo` |
| `key` | str | The call's unique id within the tool (§5) |
| `revision` | int | 1 or more; the highest valid revision per `(tool, key)` wins |
| `session` | str | The session (or subagent session) that made the call |
| `root_session` | str | The top-level session it belongs to; equals `session` for top-level calls |
| `at` | str | When the call completed (or started, for Cline), as normalized ISO UTC |
| `model` | str or null | The model id exactly as the tool reports it |
| `cwd` | str or null | The working folder as the tool recorded it |
| `project_id` | str or null | From §6.4; null when `cwd` is null |
| `tokens_sent` | int or null | Input tokens, in the tool's own shape (see the next field) |
| `sent_includes_cache_read` | bool | True when `tokens_sent` already includes `cache_read` (OpenAI shape) |
| `tokens_returned` | int or null | Output tokens, including reasoning or thinking |
| `cache_read` | int or null | Cache-read input tokens |
| `cache_write` | int or null | Cache-write input tokens, all lifetimes |
| `cache_write_5m` | int or null | The part of `cache_write` written with the 5-minute lifetime (Claude Code only) |
| `reasoning` | int or null | Reasoning or thinking tokens. For display only; already inside `tokens_returned` |
| `speed` | str or null | Claude Code's `usage.speed` (`standard` or `fast`); null elsewhere |
| `recorded_at` | str | When AgentControl wrote this revision, as normalized ISO UTC |

A null counter means the tool did not report that number. A 0 means it reported zero.

### 4.2 Revisions

A tool can rewrite a call after AgentControl has imported it:
- Claude Code writes a call on several lines.
- Cline fills in a request's tokens when the request completes.
- Kilo updates a message as it streams.

The import compares each incoming call with the current highest revision of the same `(tool, key)`:

- **No current record:** append revision 1.
- **Counters:** each counter takes the larger of the two known values. A known value is never replaced by null.
- **Other fields:** `model`, `cwd` and `project_id` keep the current value unless it is null. `session`, `root_session`, `at` and `speed` keep the current value.
- **Result:** if nothing changed, nothing is appended. Otherwise revision + 1 is appended with the merged values.

### 4.3 Validation and the content boundary

Every call passes a strict validator before it is written, and every line is validated again when it is loaded:
- **Keys:** exactly the fields above, no others.
- **Counters:** non-negative ints, or null. Booleans are rejected.
- **Ids:** `key`, `session` and `root_session` must match `^[A-Za-z0-9_.:@+\-/]{1,300}$`.
- **Model:** must match `^[A-Za-z0-9_.:@+\-/<>]{1,200}$`.
- **Folder:** `cwd` is at most 1,024 characters, with no control characters.
- **Speed:** must match `^[a-z_]{1,20}$`.

A string that could carry free text, such as a title, a sentence or a newline, cannot pass. The same rule applies to the checkpoint file (§6.2) and to diagnostics: they hold paths, ids, counts and exception type names, never file content.

## 5. Readers

### 5.1 Contract

There is one module per tool. Each module exposes:

```python
def read(source: Path, checkpoint: Mapping[str, object] | None, *, now: datetime
         ) -> tuple[list[dict[str, object]], dict[str, object], list[Diagnostic]]
```

- **Input:** `source` is the tool's store (§6.1). `checkpoint` is what the previous call returned, or None on the first read.
- **Return value:** reader calls (§4.1 without `schema`, `revision`, `project_id` and `recorded_at`), a new JSON-serializable checkpoint, and diagnostics.
- **Purity:** a reader reads only under `source` and never writes.
- **Errors:**
  - A malformed line, file or row becomes a diagnostic carrying the path and line number but no content, and the reader moves on.
  - A missing `source` returns no calls, the checkpoint unchanged, and one `<tool>_source_missing` diagnostic.
- **Re-reading:** reading the same data twice yields the same keys, so a lost checkpoint costs time but never accuracy.

Append-only JSONL files (Claude Code, Codex) are read incrementally:
- **Per-file checkpoint:** `size`, `mtime_ns`, `offset`, and `head`, the sha256 of the first 4,096 bytes.
- **Unchanged file:** if size and mtime are unchanged, the file is skipped.
- **Replaced file:** if `head` differs, or the size is below `offset`, the file is read again from 0.
- **Otherwise:** the reader reads from `offset`, takes only complete lines (those ending in `\n`), and advances `offset` past the last complete line.

Within one `read`, a key that appears more than once keeps its last occurrence.

### 5.2 Claude Code (`reader_claude_code.py`)

- **Source:** the `projects` directory. The reader takes every `*.jsonl` file at any depth, which includes `subagents\`.
- **Which lines are calls:** `type == "assistant"`, with `message.usage` an object, `message.model` not equal to `"<synthetic>"`, and `isApiErrorMessage` not true.
- **Key:** `message.id` + `":"` + `requestId`. When `requestId` is missing, the key is `message.id` alone; when that is missing too, it is the line's `uuid`. A line with none of the three is skipped with a diagnostic.
- **Session:** `sessionId`, plus `":" + agentId` when the line has an `agentId`. When `sessionId` is missing, the file's stem is used.
- **Root session:** `sessionId`.
- **Other fields:** `at` = `timestamp`; `model` = `message.model`; `cwd` = `cwd`.
- **Tokens** (from `usage`):

| Field | Taken from |
|---|---|
| `tokens_sent` | `input_tokens`, which excludes cache, so `sent_includes_cache_read` is False |
| `tokens_returned` | `output_tokens` |
| `cache_read` | `cache_read_input_tokens` |
| `cache_write` | `cache_creation_input_tokens` |
| `cache_write_5m` | `cache_creation.ephemeral_5m_input_tokens` |
| `reasoning` | `output_tokens_details.thinking_tokens` |
| `speed` | `speed` |

`usage.iterations` is ignored (§3).

### 5.3 Codex (`reader_codex.py`)

- **Source:** the Codex home. The reader takes `sessions\**\rollout-*.jsonl` and `archived_sessions\**\rollout-*.jsonl`.
- **Per-file state** (kept in the checkpoint beside the offset):
  - `session` = `session_meta.payload.id`;
  - `root` = `session_meta.payload.session_id`, falling back to `parent_thread_id` and then to `id`;
  - `cwd` from `session_meta`;
  - `turns`, the model and cwd of the last 64 `turn_context` entries, keyed by `turn_id`;
  - `model`, the latest `turn_context` model;
  - `has_records`;
  - `last_total`.
- **When a file has records** (`has_records` is true, or the chunk being read contains a `token_usage_record`), each `token_usage_record` is one call and every `token_count` event in the file is ignored:
  - `key` = `thread_id + ":" + response_id`;
  - `at` = the line's `timestamp`;
  - `model` and `cwd` come from `turns[turn_id]`, falling back to the latest seen;
  - the token counts come from `payload.usage`.
- **Otherwise,** each `event_msg` of `type == "token_count"` whose `info` is an object and whose `info.total_token_usage.total_tokens` differs from `last_total` is one call:
  - `key` = `session + ":tc:" + str(total_tokens)`;
  - `at` = the line's `timestamp`;
  - `model` and `cwd` are the latest seen;
  - the token counts come from `info.last_token_usage`.

  Events whose `info` is null, and events that repeat the total, are skipped.
- **Tokens:**

| Field | Taken from |
|---|---|
| `tokens_sent` | `input_tokens`, which includes cached tokens, so `sent_includes_cache_read` is True |
| `cache_read` | `cached_input_tokens` |
| `cache_write` | `cache_write_input_tokens` |
| `tokens_returned` | `output_tokens`, which includes reasoning |
| `reasoning` | `reasoning_output_tokens` |

`cache_write_5m` and `speed` are null.

- **Known edge:** an old file resumed by a newer Codex switches to records from its first record onward. A response whose `token_count` event was read in an earlier sync, before its record was written, may be counted once more. This is rare, and it is documented in the operator guide.

### 5.4 Cline (`reader_cline.py`)

- **Source:** the extension's globalStorage directory. The reader reads `tasks\<task>\ui_messages.json` and `state\taskHistory.json`.
- **When files are re-read:** Cline rewrites each file whole.
  - The checkpoint stores `size`, `mtime_ns` and `read_at` for each file.
  - A changed file is parsed again in full.
  - A file read less than 30 seconds before `now` is skipped until the next read, so an active task is not re-parsed on every sync.
- **Which entries are calls:** entries with `say == "api_req_started"` whose `text` parses as a JSON object that has `tokensIn` or `tokensOut`. Requests still streaming have neither and are picked up once they complete. Cancelled requests that do have tokens are counted.
- **Fields:**
  - `key` = `<task> + ":" + str(ts)`;
  - `session` and `root_session` = the task directory name;
  - `at` = `ts`, epoch milliseconds converted to UTC;
  - `model` = `modelInfo.modelId`;
  - `cwd` = `cwdOnTaskInitialization` of the `taskHistory.json` item whose `id` is the task, or null.
- **Tokens:** `tokens_sent` = `tokensIn`; `tokens_returned` = `tokensOut`; `cache_read` = `cacheReads`; `cache_write` = `cacheWrites`. `reasoning`, `cache_write_5m` and `speed` are null.
- **`sent_includes_cache_read`:** False when `modelInfo.providerId` is `anthropic`, `bedrock`, `vertex` or `claude-code` (Anthropic-shaped usage), and True otherwise. Only `openai` occurs on this machine.
- **Content boundary:** the `request` field in `text`, and the `task` field in `taskHistory.json`, carry prompt text. They are parsed in memory and dropped.

### 5.5 Kilo (`reader_kilo.py`)

- **Source:** the `kilo.db` file, opened with the URI `<Path.as_uri()>?mode=ro`, `uri=True` and `timeout=5`.
- **Checkpoint:** `{"updated_after": <max time_updated seen, in ms>, "ids_at": [ids of the messages with that time_updated]}`.
- **Query:** `select id, session_id, time_created, time_updated, data from message where time_updated >= ?`.
  - The query uses `>=` so that rows sharing the boundary timestamp are never missed.
  - A row at the boundary whose id is in `ids_at` is skipped, so a quiet database yields nothing.
- **Sessions:** `session` = `message.session_id`. `root_session` comes from following `session.parent_id` to the top. The walk is cycle-safe and goes at most 32 deep.
- **Which messages are calls:** `data.role == "assistant"` with `data.tokens` an object.
- **Fields:**
  - `key` = the message `id`;
  - `at` = `data.time.completed`, falling back to `data.time.created` (ms);
  - `model` = `data.modelID`;
  - `cwd` = `data.path.cwd`, falling back to `session.directory`.
- **Tokens:** `tokens_sent` = `tokens.input`, which excludes cache, so `sent_includes_cache_read` is False.
  - `tokens_returned` = `tokens.output + tokens.reasoning`; reasoning is billed as output and reported separately. When `output` is null, the result is null; when `reasoning` is null, the result is `output`.
  - `reasoning` = `tokens.reasoning`.
  - `cache_read` = `tokens.cache.read`; `cache_write` = `tokens.cache.write`.
  - `cache_write_5m` and `speed` are null.
- **A busy or locked database:** an `sqlite3.OperationalError` becomes a `kilo_db_unavailable` diagnostic. The reader returns no calls and the checkpoint unchanged.

## 6. Import and sync

### 6.1 Sources: `state/usage_sources.json`

```json
{"schema": 1, "tools": {"claude_code": {"enabled": true, "path": null}, "codex": {"enabled": false, "path": null}}}
```

- **A missing file or tool** means that tool is disabled.
- **`path`** overrides the default source. This is how an operator points Cline at a VS Code fork's storage.
- **Defaults**, given `home` and `env`:

| Tool | Default source |
|---|---|
| `claude_code` | `home/.claude/projects` |
| `codex` | `Path(env["CODEX_HOME"])` if set, else `home/.codex` |
| `cline` | `Path(env["APPDATA"])/Code/User/globalStorage/saoudrizwan.claude-dev` |
| `kilo` | `home/.local/share/kilo/kilo.db` |

### 6.2 Checkpoints: `state/tool_usage_state.json`

`{"schema": 1, "tools": {"<tool>": <reader checkpoint>}}` is replaced atomically after every applied import. A reader that failed keeps its previous checkpoint.

### 6.3 `import_tool_usage`

For each requested tool, `import_tool_usage`:
1. runs the reader with its checkpoint;
2. validates each call (§4.3), turning an invalid one into a diagnostic;
3. attributes each call's `cwd` to a project (§6.4);
4. merges it with the current record (§4.2).

With `apply=True` it then:
- appends the new revisions;
- discovers new projects;
- writes the checkpoints.

With `apply=False` it writes nothing at all: no calls, no project events, no checkpoints and no sources file.

It returns a report. For each tool the report gives:
- calls seen, new calls and revised calls;
- distinct root sessions;
- the first and last `at`;
- the known token sums;
- the diagnostic count.

It also lists the project roots that were, or would be, newly discovered. An exception in one reader becomes a `tool_reader_failed` diagnostic naming the tool and the exception type, and the other tools still run.

### 6.4 Project attribution

`resolve_project_root(cwd, home=...)` walks up from `cwd` to the nearest directory that contains `.git`, which may be a directory or, in a worktree, a file:
- **Home and drive root:** the walk stops before reaching the user's home directory or the drive root; neither is ever chosen through the walk. If it finds no repository, `cwd` itself is the root.
- **Missing folders:** a path that no longer exists is walked up until an existing ancestor is found.
- **Caching:** results are cached for each `cwd` within one import.

The root then goes through `ProjectRegistry.discover`:
- `canonical_root` handles case, slashes, and `.claude\worktrees`, `workspaces` and `scratchpad` folders.
- A new root becomes a `discovered` project, which is pending approval.

A dry run looks the root up in `projects.jsonl` without writing to it.

### 6.5 The command and the sync loop

`agent-control import-usage [--apply] [--tool T ...]` takes one or more tools. By default it uses every tool whose default source exists.

- **Without `--apply`:** it prints the report and writes nothing.
- **With `--apply`:**
  1. It first takes the dashboard's instance lock. If the dashboard is running it exits with code 2 and prints `The dashboard is running. Stop it, run import-usage --apply, then start it again.`
  2. It imports.
  3. It enables those tools in `usage_sources.json`.

`Synchronizer` takes an optional `tool_importer`.
- **Wiring:** `serve` builds one for the enabled tools and passes it in. When no tool is enabled, behaviour is unchanged.
- **Each sync:** after the run-index work, the sync calls the importer with `apply=True`.
- **Errors:** a failure becomes a `tool_import_failed` diagnostic. The report's diagnostics join the sync report.

## 7. Pricing

`calculate_call_cost(call, baseline, history)` mirrors `calculate_cost`:

- **Missing usage:** `tokens_sent` or `tokens_returned` null → `missing_usage`, cost null.
- **Missing rate:** `model` null, no rate for that model at `at`, or `speed` neither null nor `standard` → `missing_rate`, cost null. The catalog has no fast-mode rates.
- **The calculation:**
  - Input is `tokens_sent`, minus `cache_read` when `sent_includes_cache_read` is True.
  - The cost is input × input rate, plus `tokens_returned` × output rate, plus `cache_read` × cache-read rate, plus `cache_write` × cache-write rate, per million tokens.
- **Classification:**
  - `exact` when `cache_read` and `cache_write` are both known and `cache_write_5m` is null or 0;
  - `upper_bound` otherwise. The catalog's single `cache_write` rate is the 1-hour rate, so pricing 5-minute writes at it overstates the cost.

Coverage on 2026-10-01:
- **Already priced:** every Claude Code model, every Cline model except a one-off `gpt-5.6-s`, and 87% of Kilo's messages.
- **Unpriced, with traffic:**
  - Kilo: `anthropic_tool_call.claude-opus-4-6-think` (2,208 messages), `gpt-5.2`, `anthropic_pipe.claude-sonnet-4-6-think`, `gpt-5.4`, `google/gemini-3.1-pro-preview-customtools`, `legacy`, `kilo-auto/free`;
  - Codex: `gpt-5-codex`, `gpt-5`, `gpt-6-astra`, `gpt-6-sol`, `gpt-6.1-sol`, `codex-auto-review`.

Prices are added only from the vendor's published pricing page, with the URL and date in the commit message. Anything without a published price stays "no rate".

## 8. Snapshot and API

- **Inclusion:** a call counts toward totals when its project is approved, the same rule as runs. A call with a null `project_id` never counts. Its day is the date of `at` in the dashboard's timezone.
- **Filters:**
  - `SnapshotQuery.tools`: empty means all. The values are `engine` (runs) and the four tool ids.
  - `project_ids`, `models` and the date range apply to calls.
  - A non-empty `tasks` or `states` filter matches runs only.
- **Period totals** (`normal_totals.today`, `month`, `all_time`):
  - The existing `known_cost` and per-counter summaries now include calls.
  - A new `calls` count is added.
  - A new `by_tool` maps `engine`, `claude_code`, `codex`, `cline` and `kilo`, each present only when it has rows, to `{"count", "sessions", "known_cost", "tokens_sent", "tokens_returned", "cache_read", "cache_write"}`. `count` is runs for `engine` and calls for a tool. `sessions` is null for `engine`. The token fields are known sums, or null when none is known.
- **Project totals:** `project_totals` rows include calls and gain the same `by_tool` mapping.
- **Spend tables:**
  - `spend.by_day`, `by_project` and `by_model` include calls.
  - `spend.by_task` groups calls under the key `tool:<tool>`, so every spend table sums to the same total.
  - A new `spend.by_tool` uses the same row shape as `by_model`.
- **Coverage:** `coverage` counts calls as well as runs.
- **Tool sessions:** calls are grouped by `(tool, root_session)`.
  - `tool_sessions_live`: sessions whose latest call is at most `TOOL_LIVE_SECONDS = 600` old.
  - `tool_sessions_recent`: sessions whose latest call is between 600 and 600 + `RECENT_WINDOW_SECONDS` (900) seconds old.
  - Both are sorted newest call first. Each row is:

    ```
    {"tool", "session", "project_id", "project_name", "included_in_totals",
     "models": [ids, most calls first], "first_at", "last_at", "calls",
     "subagents": distinct non-root sessions, "tokens_sent", "tokens_returned",
     "cache_read", "cache_write", "reasoning": known sums or null,
     "tokens_in": total input, summed per call as tokens_sent when it already
       includes cache reads, else tokens_sent + cache_read + cache_write; null
       when no call has tokens_sent,
     "cost": known sum or null, "cost_classification": the worst of missing_usage >
     missing_rate > upper_bound > exact, "burn_per_hour": {"cost": known cost of
     calls in the last 60 minutes or null, "tokens_returned": int}}
    ```

  - The session's project is the project of its latest call.
- **Live totals:** `live_totals` gains `tool_sessions` (a count) and `tool_burn_cost_per_hour` (float or null).
- **API:**
  - `GET /api/data` accepts `tool=<id>`, repeated or comma-separated. An unknown id returns 400.
  - The dashboard loads `tool_usage.jsonl` through a cache keyed on the file's identity. When the file only grew, only the appended bytes are parsed.

## 9. Dashboard page

- **Live now and Recently ended.** Tool session rows follow the run rows. Each row shows:
  - a tool badge;
  - the project (or "not in totals");
  - the models;
  - how long ago the last call was;
  - calls, tokens and cost;
  - burn per hour;
  - subagents.

  Each row carries the line `Watch only. This session runs outside AgentControl.` Rows have no Steer, Kill, Dismiss or narration controls and do not open the panel.
- **Totals.** Each period gains a `By tool` breakdown.
- **Spend.** A `By tool` table is added.
- **Filter.** The filter bar gains `Tool`.
- **Tool names:**

| Id | Shown as |
|---|---|
| `engine` | Harness agents |
| `claude_code` | Claude Code |
| `codex` | Codex |
| `cline` | Cline |
| `kilo` | Kilo |

- **Rendering:** all data is rendered with `textContent` or `esc()`.

## 10. Testing

- **Fixtures are synthetic,** built in `tmp_path`.
- **The content boundary.** Every free-text field in a fixture carries the sentinel `SENTINEL-TEXT-DO-NOT-STORE`. Tests assert it never appears in:
  - a reader's calls, checkpoint or diagnostics;
  - `tool_usage.jsonl`, `tool_usage_state.json` or `projects.jsonl`;
  - the `/api/data` payload.
- **No real stores.** An autouse guard in `tests/conftest.py` fails any test that runs a registered reader on a source outside `tmp_path`.
- **Each reader** gets tests for:
  - its key, session and token rules;
  - null versus 0;
  - incremental reads, replaced files and partial last lines;
  - its skip rules.
- **The store** gets tests for revision merging, no downgrades and unchanged-skip.
- **The import** gets tests for:
  - dry runs writing nothing;
  - failure isolation;
  - project resolution (repository walk-up, worktrees, the home and drive-root stops).
- **The snapshot** gets tests for:
  - inclusion, the `by_tool` split and day boundaries;
  - spend table sums;
  - the live and recent windows;
  - filters.
- **The page** gets JS tests for the rows, the missing controls and the filter.

## 11. Rollout

1. Implement and test (the plan is `docs/superpowers/plans/2026-10-01-tool-usage-implementation.md`).
2. Add the published prices that §7 lists as missing.
3. Run `agent-control import-usage` as a dry run against the live stores, and report the per-tool figures and the new folders to the operator.
4. Only with the operator's approval: stop the dashboard, run `agent-control import-usage --apply`, start the dashboard, and approve the projects that should count.

## 12. Known limits

- **Live is measured from the last call.** A session busy in one tool for more than 10 minutes without calling its model shows as recently ended until its next call.
- **Claude Code 5-minute cache writes** are priced at the 1-hour rate and marked as an upper bound.
- **Codex's `cache_write_input_tokens`** were 0 in the records seen. Their pricing semantics are unverified.
- **Cline with a non-gateway Anthropic provider:** the rule in §5.4 follows Cline's provider shapes and is unverified on this machine.
- **Old Codex files** are counted from `token_count` events, which come within about 1% of the per-response records where both exist.

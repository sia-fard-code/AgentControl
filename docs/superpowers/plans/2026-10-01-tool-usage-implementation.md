# Usage from Claude Code, Codex, Cline and Kilo: implementation plan

> **For agentic workers:** delegated agents on the OpenWebUI harness (`~/.claude/bin/delegate.py --worktree`) carry out this plan.
> - Each agent takes one task. An orchestrator reviews, applies and commits each patch.
> - Do not use superpowers:subagent-driven-development or the built-in Agent or Workflow tools. `~/.claude/DELEGATION.md` forbids them on this machine.
> - Each task gives exact contracts and a numbered list of test cases rather than finished code. Write the tests first.

**Goal:** the dashboard shows Claude Code, Codex, Cline and Kilo sessions in Live now and Recently ended, and counts their tokens and list-price cost in totals and spend, split by tool.

**Architecture:**
1. Four pure readers turn each tool's local logs into number-only call dicts.
2. An importer validates the calls, attributes them to projects, and appends them to a separate append-only `tool_usage.jsonl` with revisions.
3. The snapshot adds those calls to totals and spend, and groups them into watch-only sessions.

**Tech stack:** Python 3.10+ standard library (`sqlite3` included), vanilla ES modules, `node --test`. No new dependencies.

**Spec:** `docs/superpowers/specs/2026-10-01-tool-usage-design.md`, cited as §n.

## Global constraints

- **Engine and versions.** No change to `engine/`, `HARNESS_VERSION`, `INDEX_SCHEMA` or any version number.
- **Tests.**
  - Every test builds synthetic fixtures under `tmp_path`.
  - Nothing reads `%USERPROFILE%\.claude`, `.codex`, `%APPDATA%`, `.local\share\kilo` or live AgentControl state.
  - Run the suite with `python -m pytest -q` and `node --test tests/js/dashboard.test.mjs tests/js/panel.test.mjs` (Node 22 rejects a directory argument).
- **The content boundary** (§4.3).
  - Readers keep only the fields in §4.1.
  - Every fixture puts the sentinel `SENTINEL-TEXT-DO-NOT-STORE` into each free-text field its format has: message text, titles, prompts, tool input and tool output.
  - Each task's tests assert that the sentinel never reaches that task's outputs.
  - Diagnostics carry codes, paths, line numbers, ids and exception type names, never file content or offending values.
- **Style.**
  - Match the surrounding modules: `from __future__ import annotations`, frozen dataclasses, type hints, `models.Diagnostic(code, message, source=..., line=...)`.
  - Use `storage.normalize_utc` for every timestamp.
- **Writing files** (harness limits).
  - Keep each tool call under about 150 lines.
  - Write a new file in parts: create it with the first part ending in the line `# --- continue ---` (`// --- continue ---` in JS). Then replace that marker with the next part, and remove the marker at the end.
- **Copy.** UI text is plain sentences in sentence case. The strings in §9 are used verbatim.

## Shared contracts

These are referenced by several tasks.

```python
# agent_control/tool_calls.py (Task 1)
TOOLS = ("claude_code", "codex", "cline", "kilo")
READER_KEYS = ("tool", "key", "session", "root_session", "at", "model", "cwd",
               "tokens_sent", "tokens_returned", "cache_read", "cache_write",
               "cache_write_5m", "reasoning", "sent_includes_cache_read", "speed")
COUNTER_FIELDS = ("tokens_sent", "tokens_returned", "cache_read", "cache_write",
                  "cache_write_5m", "reasoning")

# Every reader module (Tasks 2-5): agent_control/reader_<tool>.py
TOOL = "<tool id>"
def read(source: Path, checkpoint: Mapping[str, object] | None, *, now: datetime
         ) -> tuple[list[dict[str, object]], dict[str, object], list[Diagnostic]]: ...
```

Each reader call is a dict with exactly the keys in `READER_KEYS`:
- `at` is an ISO-8601 UTC string;
- counters are `int` or `None`;
- `sent_includes_cache_read` is a `bool`.

Readers do not import `tool_calls.py`. The importer validates their dicts with `validate_reader_call`.

Diagnostic codes are prefixed with the tool id:
- `<tool>_source_missing`
- `<tool>_bad_line`
- `<tool>_bad_file`
- `<tool>_skipped_line`
- `kilo_db_unavailable`

## File structure

| File | Task | Responsibility |
|---|---|---|
| `src/agent_control/tool_calls.py` (new) | 1 | `ToolCall`, validation, revision merge, load, append, `ToolCallCache` |
| `src/agent_control/jsonl_tail.py` (new) | 1 | Incremental reads of append-only JSONL with replacement detection |
| `src/agent_control/paths.py`, `tests/test_paths.py` | 1 | `tool_usage`, `tool_usage_state` and `usage_sources` paths |
| `tests/test_tool_calls.py`, `tests/test_jsonl_tail.py` (new) | 1 | Tests for Task 1 |
| `src/agent_control/reader_cline.py`, `tests/test_reader_cline.py` (new) | 2 | §5.4 |
| `src/agent_control/reader_kilo.py`, `tests/test_reader_kilo.py` (new) | 3 | §5.5 |
| `src/agent_control/reader_claude_code.py`, `tests/test_reader_claude_code.py` (new) | 4 | §5.2 |
| `src/agent_control/reader_codex.py`, `tests/test_reader_codex.py` (new) | 5 | §5.3 |
| `src/agent_control/tool_import.py`, `tests/test_tool_import.py` (new), `tests/conftest.py` | 6 | §6.1 to §6.4 and the reader guard |
| `src/agent_control/pricing.py`, `src/agent_control/tool_sessions.py` (new), `src/agent_control/snapshot.py`, `tests/test_pricing.py`, `tests/test_tool_sessions.py` (new), `tests/test_snapshot_tool_usage.py` (new) | 7 | §7 and §8 |
| `src/agent_control/cli.py`, `src/agent_control/sync.py`, `tests/test_cli.py`, `tests/test_sync_tool_usage.py` (new) | 8 | §6.5 |
| `src/agent_control/dashboard.py`, `web/dashboard.js`, `web/dashboard.css`, `web/index.html`, `tests/test_dashboard_api.py`, `tests/js/dashboard.test.mjs`, `tests/test_web_contract.py` | 9 | §8 API and §9 page |
| `docs/operator-guide.md`, `tests/test_operator_docs.py`, `docs/superpowers/specs/2026-09-17-agent-control-design.md` | 10 | The operator guide and a base-spec cross-reference |

## Waves and integration protocol

| Wave | Tasks, in parallel | Starts after |
|---|---|---|
| 1 | 1, 2, 3 | spec and plan committed |
| 2 | 4, 5, 6, 7 | wave 1 applied and committed |
| 3 | 8, 9, 10 | wave 2 applied and committed |

**Per agent:**
- **Launch:**
  ```
  timeout 5400 python -u ~/.claude/bin/delegate.py -C "D:/repos/AgentControl" --worktree -m anthropic_tool_call.claude-opus-5-5-think -e medium --label <label> --patch <scratchpad>/<label>.patch --max-iters <n> -f <brief>
  ```
- **Watcher:** `python -u ~/.claude/bin/agent_await.py --repo "D:/repos/AgentControl" --label <label>`, run beside it.
- **After it ends, the orchestrator:**
  1. reviews the patch;
  2. runs `git apply --check`, then `git apply`;
  3. runs `rtk proxy python -m pytest -q` and `node --test tests/js/dashboard.test.mjs tests/js/panel.test.mjs` (Node 22 rejects a directory argument);
  4. commits one commit per task;
  5. removes the worktree.

**Labels:** `t1-store`, `t2-cline`, `t3-kilo`, `t4-claude`, `t5-codex`, `t6-import`, `t7-snapshot`, `t8-cli-sync`, `t9-web`, `t10-docs`.

## Review focus

These are failure modes the spec implies but the obvious tests would miss. Each has a pinned test in its owning task.

1. **A JSONL file growing past 4,096 bytes** must not look "replaced" just because its head hash now covers more bytes. *(Task 1, `test_growth_past_head_window_is_not_a_restart`.)*
2. **A Cline request that is still streaming** has no tokens yet. It is skipped now and counted once it completes, and never counted twice. *(Task 2, `test_streaming_request_counted_once_after_completion`.)*
3. **Kilo rows that share the checkpoint's `time_updated`** are neither lost nor re-emitted on every read. *(Task 3, `test_boundary_rows_not_lost_or_repeated`.)*
4. **A Codex file that gains records after being read in `token_count` mode** switches to records and ignores later `token_count` events. *(Task 5, `test_switch_to_records_mid_file`.)*
5. **A user home that is itself a git repository** must not swallow every folder under it. *(Task 6, `test_home_repo_does_not_capture_subfolders`.)*

---

### Task 1: Store, incremental tail and paths (`t1-store`, max-iters 45)

**Files:**
- Create: `src/agent_control/tool_calls.py`, `src/agent_control/jsonl_tail.py`, `tests/test_tool_calls.py`, `tests/test_jsonl_tail.py`
- Modify: `src/agent_control/paths.py`, `tests/test_paths.py`

**Interfaces:**
- **Consumes:**
  - `storage.normalize_utc`, `storage.append_jsonl(path, records, lock)` and `storage.read_jsonl(path, *, source)`;
  - `storage.canonical_json` (if it suits the store's format);
  - `models.Diagnostic`.
- **Produces:**

```python
# paths.py: three properties on AppPaths, following the existing ones
tool_usage -> state_dir / "tool_usage.jsonl"
tool_usage_state -> state_dir / "tool_usage_state.json"
usage_sources -> state_dir / "usage_sources.json"

# tool_calls.py
TOOLS, READER_KEYS, COUNTER_FIELDS   # exactly as in Shared contracts
SCHEMA = 1

@dataclass(frozen=True)
class ToolCall:
    schema: int
    tool: str
    key: str
    revision: int
    session: str
    root_session: str
    at: str
    model: str | None
    cwd: str | None
    project_id: str | None
    tokens_sent: int | None
    tokens_returned: int | None
    cache_read: int | None
    cache_write: int | None
    cache_write_5m: int | None
    reasoning: int | None
    sent_includes_cache_read: bool
    speed: str | None
    recorded_at: str
    def as_json(self) -> dict[str, object]: ...

def validate_reader_call(value: Mapping[str, object]) -> dict[str, object]: ...
def validate_tool_call(value: Mapping[str, object]) -> ToolCall: ...
def merge_call(current: ToolCall | None, incoming: Mapping[str, object], *,
               project_id: str | None, recorded_at: str) -> ToolCall | None: ...
def load_tool_calls(path: Path) -> tuple[dict[tuple[str, str], ToolCall], list[Diagnostic]]: ...
def append_tool_calls(path: Path, calls: Sequence[ToolCall], lock: threading.RLock) -> None: ...

class ToolCallCache:
    def __init__(self, path: Path) -> None: ...
    def load(self) -> tuple[dict[tuple[str, str], ToolCall], list[Diagnostic]]: ...

# jsonl_tail.py
def tail_jsonl(path: Path, state: Mapping[str, object] | None
               ) -> tuple[list[tuple[int, dict[str, object]]], dict[str, object], list[Diagnostic], bool]: ...
```

**Behaviour:**
- **`validate_reader_call`** implements §4.3 for `READER_KEYS`. It returns a new dict with `at` normalized. A `ValueError` names the field only, never the value.
- **`validate_tool_call`** applies the same checks to all of the §4.1 fields:
  - `schema == 1`;
  - `revision >= 1`;
  - `project_id` is None or a string of at most 64 characters matching `^[A-Za-z0-9-]+$`;
  - `recorded_at` is normalized.
- **`merge_call`** implements §4.2. It returns the revision to append, or None when nothing changed.
- **`load_tool_calls`:**
  - keeps the highest valid revision for each `(tool, key)`;
  - for two lines with the same revision, the later line wins;
  - turns an invalid line into a `tool_usage_bad_line` diagnostic with `source` and `line`;
  - returns `({}, [])` for a missing file.
- **`append_tool_calls`** writes `ToolCall.as_json()` lines through `storage.append_jsonl`.
- **`ToolCallCache.load`:**
  - keeps the parsed map together with the file's size, mtime_ns and a head hash;
  - when the file only grew, it parses only the bytes after the previous size and merges them;
  - otherwise it reloads in full.
- **`tail_jsonl`:**
  - **State:** a JSON dict with `size`, `mtime_ns`, `offset`, `lines`, `head` and `head_len`. `head` is the sha256 hex of the first `head_len = min(4096, offset)` bytes.
  - **Unchanged file** (same size and mtime_ns): returns `([], state, [], False)`.
  - **Restart:** the file is read again from 0 when its size is below `offset`, or when the hash of its first `head_len` bytes differs. The returned flag (4th element) is then True and the line count restarts.
  - **Lines:** only complete lines (ending in `\n`) are consumed. `\r` is stripped and lines are decoded as UTF-8 with `errors="replace"`. Blank lines are skipped.
  - **Rows** are `(absolute 1-based line number, dict)`. A line that is not a JSON object becomes a `jsonl_bad_line` diagnostic with that line number.
  - **A file that vanished:** returns `([], dict(state or {}), [diag jsonl_missing], False)`.

**Test cases** (`tests/test_tool_calls.py`):
1. A valid reader call passes, and `at` is normalized.
2. Each of these is rejected: an extra key, a missing key, a bool counter, a negative counter, a key containing a space, a model containing a space, a `cwd` containing a newline, a `speed` of `"Fast mode!"`, a tool `"other"`.
3. A `ValueError` raised for a sentinel-bearing value never contains the sentinel.
4. `merge_call(None, ...)` gives revision 1, and `recorded_at` and `project_id` are set.
5. Identical incoming data gives None.
6. A larger counter gives revision 2 holding the larger value. A smaller counter never lowers it, and an incoming None never erases a known value.
7. A null `model`, `cwd` or `project_id` is filled from incoming. Known values are kept. `session`, `root_session`, `at` and `speed` are kept.
8. `load_tool_calls`:
   - the highest revision wins;
   - for the same revision, the later line wins;
   - an invalid line yields a diagnostic with the line number but without its content, and the other lines still load.
9. A round trip of `append_tool_calls` followed by `load_tool_calls`.
10. `ToolCallCache`:
    - an unchanged file is not re-parsed (monkeypatch the parser and count calls);
    - after an append, only the tail is parsed and the new revision is visible;
    - a rewritten file is fully reloaded.

**Test cases** (`tests/test_jsonl_tail.py`):
1. The first read returns every row, with line numbers.
2. After an append, the next read returns only the new rows, with absolute line numbers.
3. A partial last line is not consumed until it is completed.
4. An unchanged file returns nothing.
5. A truncated file restarts, and the flag is True.
6. A same-size file with different first bytes restarts.
7. `test_growth_past_head_window_is_not_a_restart`: a file that grows from 100 bytes to 10 KB across reads never restarts.
8. CRLF lines are handled.
9. A bad JSON line gives `jsonl_bad_line` with its line number, and the sentinel is not in the diagnostic.
10. A vanished file gives `jsonl_missing`.

`tests/test_paths.py`: the three new paths sit under `state_dir`.

### Task 2: Cline reader (`t2-cline`, max-iters 40)

**Files:**
- Create: `src/agent_control/reader_cline.py`, `tests/test_reader_cline.py`

**Interfaces:** implements the reader contract with `TOOL = "cline"`, plus:

```python
REREAD_AFTER_SECONDS = 30
_ANTHROPIC_SHAPED = frozenset({"anthropic", "bedrock", "vertex", "claude-code"})
# checkpoint: {"files": {"<task dir name>": {"size": int, "mtime_ns": int, "read_at": iso}}}
```

**Behaviour.** This is §5.4 exactly.

- **Files read:** `source/tasks/<task>/ui_messages.json` (a JSON array) and `source/state/taskHistory.json` (a JSON array of objects; `id` is the task and `cwdOnTaskInitialization` its folder). `taskHistory.json` is read once per `read` call.
- **When a task file is re-parsed:**
  - only when its size or mtime_ns differs from the checkpoint;
  - not when its checkpointed `read_at` is less than `REREAD_AFTER_SECONDS` before `now`. Such a file is skipped and its checkpoint kept.
- **A file that fails to parse** gives `cline_bad_file` and keeps its previous checkpoint, so it is retried.
- **Calls:** the reader emits one call for each `api_req_started` entry whose `text` parses as a JSON object containing `tokensIn` or `tokensOut`.
  - `key` = `f"{task}:{ts}"`.
  - `at` comes from the entry's `ts`, in epoch milliseconds.
  - `model` = `entry["modelInfo"]["modelId"]` when present, else None.
  - `sent_includes_cache_read` is False when `modelInfo.providerId` is in `_ANTHROPIC_SHAPED`, and True otherwise (a missing `modelInfo` also counts as True).
- **Counters:** `tokensIn`, `tokensOut`, `cacheReads` and `cacheWrites` map to `tokens_sent`, `tokens_returned`, `cache_read` and `cache_write`. A missing key gives None; 0 stays 0. `reasoning`, `cache_write_5m` and `speed` are None.
- **Missing source** gives `cline_source_missing`, no calls, and the checkpoint unchanged.

**Test cases:**
1. One completed request gives one call with every field exactly as above. The key format and the UTC `at` are checked.
2. `test_streaming_request_counted_once_after_completion`:
   - the first read sees a request with `{"request": "SENTINEL..."}` only, and emits nothing;
   - the file is rewritten with tokens, and the next read (more than 30 s later) emits it once;
   - a further unchanged read emits nothing.
3. A cancelled request (`cancelReason` present) that has tokens is counted.
4. Providers: `openai` gives True, `anthropic` gives False, and a missing `modelInfo` gives True.
5. `cwd` comes from `taskHistory`, and is None when the task is missing from it.
6. The sentinel, placed in `request`, `task`, the `say: "text"` entries and `api_conversation_history.json`, appears in no call, checkpoint or diagnostic.
7. Re-reading:
   - a file changed within 30 s of `read_at` is skipped and its checkpoint kept;
   - after 30 s it is read.
8. Malformed `ui_messages.json` gives `cline_bad_file`, and the previous checkpoint for that task is kept.
9. Null versus 0: `tokensOut: 0` gives 0, and a missing `cacheWrites` gives None.
10. A missing source gives `cline_source_missing`.

### Task 3: Kilo reader (`t3-kilo`, max-iters 40)

**Files:**
- Create: `src/agent_control/reader_kilo.py`, `tests/test_reader_kilo.py`

**Interfaces:** implements the reader contract with `TOOL = "kilo"`, plus:

```python
_BUSY_TIMEOUT_SECONDS = 5.0
_MAX_DEPTH = 32
# checkpoint: {"updated_after": int_ms, "ids_at": [message ids whose time_updated == updated_after]}
```

**Behaviour.** This is §5.5, with the boundary handling made exact.

- **Opening the database:** `sqlite3.connect(source.as_uri() + "?mode=ro", uri=True, timeout=_BUSY_TIMEOUT_SECONDS)`. Any `sqlite3.DatabaseError` (connect or query) gives `kilo_db_unavailable`, no calls, and the checkpoint unchanged.
- **Query:** `select id, session_id, time_created, time_updated, data from message where time_updated >= ? order by time_updated, id`.
  - A row at exactly `updated_after` is skipped when its id is in `ids_at`.
  - The new checkpoint is the largest `time_updated` seen, with the ids at that value. When no row is new, the checkpoint stays the same.
- **Root session:** `root_session` follows `session.parent_id` (from `select id, parent_id, directory from session`) up to the top.
  - A cycle, or more than `_MAX_DEPTH` steps, stops at the last session reached.
  - A missing session row makes the message's own `session_id` the root.
- **Field mapping:** exactly as in §5.5, including `tokens_returned = output + reasoning`, with the null rules.
- **Bad data:** an unparseable `data` gives `kilo_bad_line`, carrying the message id and not the content.

**Test cases.** Each builds a temp SQLite db with tables `session(id, parent_id, directory, title)` and `message(id, session_id, time_created, time_updated, data)`.
1. An assistant message gives a call with every field mapped.
2. The `tokens_returned` rules:
   - `output: 10, reasoning: 5` gives 15, with reasoning 5;
   - a missing reasoning gives `tokens_returned` 10 and reasoning None;
   - a missing output gives None.
3. User messages, and assistant messages without `tokens`, are skipped.
4. The root walks up 3 levels. A cycle (A→B→A) terminates.
5. `cwd` falls back from `path.cwd` to `session.directory`; `at` falls back from `completed` to `created`.
6. `test_boundary_rows_not_lost_or_repeated`:
   - two rows share `time_updated` T, and the first read returns both;
   - a third row at T is inserted later, and the next read returns only it;
   - a read with no changes returns nothing.
7. A message updated with new tokens and a later `time_updated` is re-emitted.
8. A garbage file in place of the db gives `kilo_db_unavailable`, and the checkpoint is unchanged.
9. The db file's bytes are identical before and after `read`.
10. The sentinel, placed in `session.title`, message `summary` and `text`, and the `part` rows, appears in no call, checkpoint or diagnostic.
11. A missing source gives `kilo_source_missing`.

### Task 4: Claude Code reader (`t4-claude`, max-iters 40)

**Files:**
- Create: `src/agent_control/reader_claude_code.py`, `tests/test_reader_claude_code.py`

**Interfaces:**
- Consumes `jsonl_tail.tail_jsonl` (Task 1).
- Implements the reader contract with `TOOL = "claude_code"`.
- Checkpoint: `{"files": {"<path relative to source, forward slashes>": <tail_jsonl state>}}`.

**Behaviour.** This is §5.2 exactly.
- **Files:** every `*.jsonl` file at any depth under `source`, sorted.
- **Calls:** only the lines §5.2 accepts are calls. Inside one `read`, a repeated key keeps its last line.
- **Key fallbacks:**
  - no `requestId`: the key is `message.id`;
  - no `message.id`: the key is the line's `uuid`;
  - none of the three: the line is skipped with `claude_code_skipped_line`.
- **Session:** a subagent line, one that has `agentId`, gets session `f"{sessionId}:{agentId}"` and root `sessionId`. A line without `sessionId` uses the file's stem for both.

**Test cases:**
1. One assistant line gives a call with all 15 fields mapped. `cache_write_5m` comes from `cache_creation`, `reasoning` from `output_tokens_details.thinking_tokens`, and `speed` from `usage.speed`.
2. Three lines of one call within one read give one call, with the last line's numbers.
3. Across two reads, the second read re-emits the call with its newer numbers.
4. These lines give no calls: a model of `<synthetic>`, `isApiErrorMessage: true`, a `type` of `user`, an assistant line without usage.
5. Each key fallback works, and a line with none of the three ids gives a diagnostic without content.
6. A subagent file under `<session>/subagents/` gives the `sessionId:agentId` session and the `sessionId` root.
7. Incremental reads:
   - an append yields only the new calls;
   - a partial line waits;
   - a replaced file is read again in full, keeping the same keys.
8. Null versus 0: `cache_read_input_tokens: 0` gives 0, and a missing `output_tokens_details` gives reasoning None.
9. The sentinel in `message.content` text, thinking and `tool_use` input, and in user lines, appears in no call, checkpoint or diagnostic.
10. A missing source gives `claude_code_source_missing`.

### Task 5: Codex reader (`t5-codex`, max-iters 45)

**Files:**
- Create: `src/agent_control/reader_codex.py`, `tests/test_reader_codex.py`

**Interfaces:**
- Consumes `jsonl_tail.tail_jsonl` (Task 1).
- Implements the reader contract with `TOOL = "codex"`, plus `_TURN_MEMORY = 64`.
- Checkpoint: `{"files": {"<relative path>": {"tail": <state>, "session", "root", "cwd", "model", "turns": {turn_id: [model, cwd]}, "has_records": bool, "last_total": int|None}}}`.

**Behaviour.** This is §5.3 exactly.
- **Files:** `source/sessions/**/rollout-*.jsonl` and `source/archived_sessions/**/rollout-*.jsonl`.
- **Lines used:**
  - `session_meta` sets `session`, `root` and `cwd` (§5.3).
  - `turn_context` records `turns[turn_id] = [model, cwd]`, keeping the last `_TURN_MEMORY` entries, and updates the latest model and cwd.
- **Choosing records or `token_count`:** a chunk that contains any `token_usage_record` sets `has_records`. From then on that file uses records only.
- **Calls:**

| Mode | Key | Model and cwd | Tokens |
|---|---|---|---|
| Records | `f"{thread_id}:{response_id}"` | `turns[turn_id]`, else the latest | `payload.usage` |
| `token_count` | `f"{session}:tc:{total}"`, emitted only when `total != last_total`; skips null `info` | the latest | `info.last_token_usage` |

- **Token mapping:** as §5.3. `sent_includes_cache_read` is True.
- **Restart:** when `tail_jsonl` reports a restart, the per-file state is reset.

**Test cases:**
1. In a file with records, each record gives one call: the key, the model from its `turn_context`, the cwd from that turn, and all counters. `token_count` events in the same file give nothing.
2. In a file with only `token_count`, each change of total gives one call. A repeated total and a null `info` are skipped. The key is `session:tc:<total>` and the model is the latest.
3. `test_switch_to_records_mid_file`:
   - the first read covers 2 `token_count` changes and gives 2 calls;
   - after an append of `turn_context`, a record and a `token_count`, only the record is emitted;
   - `has_records` persists in the checkpoint.
4. A child session (`session_id` differs from `id`) gives root `session_id` and session `id`.
5. A record whose `turn_context` was read in an earlier `read` call still gets that turn's model, from the checkpoint.
6. `turns` keeps at most 64 entries.
7. Files in `archived_sessions` are read.
8. Mapping: `sent_includes_cache_read` is True; `cached_input_tokens` gives `cache_read`; `reasoning_output_tokens` gives `reasoning`; `cache_write_5m` and `speed` are None.
9. The sentinel in `response_item` content, `base_instructions` and the user messages in `event_msg` appears in no call, checkpoint or diagnostic.
10. A replaced file resets its per-file state.
11. A missing source gives `codex_source_missing`.

### Task 6: Import, sources and the test guard (`t6-import`, max-iters 50)

**Files:**
- Create: `src/agent_control/tool_import.py`, `tests/test_tool_import.py`
- Modify: `tests/conftest.py`

**Interfaces:**
- **Consumes:**
  - from Task 1: `tool_calls.*`, `AppPaths.tool_usage`, `tool_usage_state` and `usage_sources`;
  - from storage: `storage.atomic_write_json(path, value, lock)` and `storage.StateLocks`;
  - from projects: `projects.ProjectRegistry(path, locks, clock)`, its `.discover(root)` (which returns `(Project, created)`), `projects.load_projects(path)` and `projects.canonical_root(root)` (which returns `(normalized, root_key)`).
- **Produces:**

```python
READER_MODULES = {"claude_code": "agent_control.reader_claude_code",
                  "codex": "agent_control.reader_codex",
                  "cline": "agent_control.reader_cline",
                  "kilo": "agent_control.reader_kilo"}

def _load_reader(tool: str) -> Callable[..., tuple[list[dict], dict, list[Diagnostic]]]: ...
    # importlib.import_module(READER_MODULES[tool]).read

@dataclass(frozen=True)
class SourceSetting:
    enabled: bool
    path: str | None

def load_usage_sources(path: Path) -> tuple[dict[str, SourceSetting], list[Diagnostic]]: ...
def write_usage_sources(path: Path, settings: Mapping[str, SourceSetting], lock: threading.RLock) -> None: ...
def default_source(tool: str, *, home: Path, env: Mapping[str, str]) -> Path | None: ...
def resolve_source(tool: str, setting: SourceSetting | None, *, home: Path, env: Mapping[str, str]) -> Path | None: ...
def resolve_project_root(cwd: str, *, home: Path) -> str: ...

@dataclass(frozen=True)
class ToolImportSummary:
    tool: str
    calls_seen: int
    new_calls: int
    revised_calls: int
    sessions: int
    first_at: str | None
    last_at: str | None
    tokens_sent: int
    tokens_returned: int
    cache_read: int
    cache_write: int
    diagnostics: int

@dataclass(frozen=True)
class ImportReport:
    applied: bool
    tools: tuple[ToolImportSummary, ...]
    new_project_roots: tuple[str, ...]
    diagnostics: tuple[Diagnostic, ...]

def import_tool_usage(paths: AppPaths, locks: StateLocks, *, tools: Sequence[str],
                      sources: Mapping[str, Path], apply: bool, now: datetime, home: Path,
                      readers: Mapping[str, Callable] | None = None) -> ImportReport: ...
```

**Behaviour.** This covers §6.1 to §6.4.

- **Sources:**
  - **`default_source`:** the §6.1 table. It returns None for `cline` when `APPDATA` is missing.
  - **`resolve_source`:** returns `Path(setting.path)` when one is set, and the default otherwise.
  - **`load_usage_sources`:** a missing file gives `{}`. Any malformed content gives `usage_sources_invalid` and `{}`. A tool missing from the file counts as disabled.
- **Readers:** `readers`, when given, maps tool to `read`; otherwise `_load_reader(tool)` loads it.
- **The import loop.** For each tool in `tools`, in the given order:
  1. Run the reader with `checkpoint = state["tools"].get(tool)`, from `paths.tool_usage_state`, and `now`.
  2. If the reader raises, record `tool_reader_failed` with the message `f"{tool}: {type(exc).__name__}"`, keep that tool's old checkpoint, and continue.
  3. Pass each call through `validate_reader_call`. An invalid call gives `tool_call_invalid` with the tool and the field name.
  4. Resolve the project:
     - `cwd` None gives `project_id` None;
     - otherwise the root is `resolve_project_root(cwd, home=home)`, cached per `cwd`;
     - with `apply=True` the project is `ProjectRegistry(paths.projects, locks, clock=lambda: now).discover(root)`;
     - with `apply=False`, `load_projects` is matched by `canonical_root(root)[1]`, and an unknown root is added to `new_project_roots` (deduplicated by root_key) with `project_id` None.
  5. Merge with `merge_call`, against `load_tool_calls(paths.tool_usage)` loaded once per import and updated as calls are accepted.
- **Writing.** Only with `apply=True`, and under `locks.write`:
  - `append_tool_calls` for all new revisions;
  - `atomic_write_json(paths.tool_usage_state, {"schema": 1, "tools": {...}}, lock)`.
- **`new_project_roots`** lists the roots that discover created (when applying) or would create (in a dry run).
- **`resolve_project_root`:**
  1. Normalize the path with `Path(cwd)`. A relative path returns `cwd` unchanged.
  2. Walk from `cwd` upward. At each step:
     - if the directory is `home` (compared case-insensitively on Windows via `os.path.normcase`) or a drive root, stop and return `cwd`;
     - if `<dir>/.git` exists, as a file or a directory, return that directory as a string.
  3. Directories that do not exist are walked through.

**`tests/conftest.py`:** an autouse fixture wraps `agent_control.tool_import._load_reader`. Any reader it returns fails the test when its `source` is not under `tmp_path`. The fixture follows the style of `guard_repo_index_files`.

**Test cases.** Tests use fake readers unless a case says otherwise.
1. A dry run writes nothing: the file names and sha256 of every file under `state_dir` are identical before and after. The report counts are correct.
2. An apply:
   - writes the calls, the checkpoint and the project events;
   - a second apply with the readers returning the same calls appends no new lines;
   - its `new_calls` and `revised_calls` are 0.
3. A reader that re-emits a call with larger counters produces revision 2, and `revised_calls` is 1.
4. A reader that raises leaves that tool's checkpoint unchanged and still imports the other tool. The diagnostic message is `"<tool>: RuntimeError"`, and the exception's message (the sentinel) is absent.
5. An invalid call (extra key) gives `tool_call_invalid`, and the other calls are imported.
6. A nested folder of a repository (one that has `.git/`) resolves to the repository root.
7. A worktree with a `.git` file under `<repo>/.claude/worktrees/x` resolves through `discover` to the same project as `<repo>`.
8. A folder with no repository resolves to itself.
9. `test_home_repo_does_not_capture_subfolders`: with `home/.git` present, `home/a/b` resolves to `home/a/b`.
10. A deleted subfolder of a repository resolves to the repository.
11. An approved existing project receives its `project_id`.
12. A dry run lists a new root once and creates no project events.
13. `cwd` None gives `project_id` None.
14. `usage_sources` round trip: a missing file gives `{}`, malformed content gives a diagnostic, and a `path` override is honoured by `resolve_source`.
15. `default_source`: each tool under a fake home and env, `CODEX_HOME` honoured, and None for a missing `APPDATA`.
16. An end-to-end import with the real Cline and Kilo readers (Tasks 2 and 3) on sentinel-bearing synthetic sources: the sentinel is absent from every file under `state_dir`.
17. The guard: a test-local reader called with a source outside `tmp_path` fails. Use `pytest.raises` around the guarded call.

### Task 7: Pricing, tool sessions and the snapshot (`t7-snapshot`, max-iters 55)

**Files:**
- Create: `src/agent_control/tool_sessions.py`, `tests/test_tool_sessions.py`, `tests/test_snapshot_tool_usage.py`
- Modify: `src/agent_control/pricing.py`, `src/agent_control/snapshot.py`, `tests/test_pricing.py`

**Interfaces:**
- **Consumes:**
  - `tool_calls.ToolCall` and `TOOLS` (Task 1);
  - `pricing.resolve_rates(model, started_at, baseline, history)`;
  - `models.CostResult`;
  - the existing `snapshot` helpers.
- **Produces:**

```python
# pricing.py
def calculate_call_cost(call: ToolCall, baseline: Mapping[str, RateSet | None],
                        history: Sequence[PricingVersion]) -> CostResult: ...

# tool_sessions.py
TOOL_LIVE_SECONDS = 600
TOOL_LABELS = {"engine": "Harness agents", "claude_code": "Claude Code", "codex": "Codex",
               "cline": "Cline", "kilo": "Kilo"}

@dataclass(frozen=True)
class ToolSession:
    tool: str
    root_session: str
    calls: tuple[ToolCall, ...]   # sorted by at
    first_at: str
    last_at: str
    project_id: str | None        # the latest call's
    sessions: frozenset[str]

def group_sessions(calls: Iterable[ToolCall]) -> list[ToolSession]: ...
def session_row(session: ToolSession, projects: Mapping[str, Project], baseline, history,
                included: bool, now: datetime) -> dict[str, object]: ...

# snapshot.py
SnapshotQuery.tools: tuple[str, ...] = ()        # new field, last, with a default
build_snapshot(..., tool_calls: Sequence[ToolCall] = ())   # new keyword-only parameter
```

**Behaviour.** This covers §7 and §8 exactly.
- **`calculate_call_cost`:** classification follows the §7 order. The amount uses the same formula as `calculate_cost` (input minus `cache_read` when `sent_includes_cache_read`), with `resolve_rates(call.model, call.at, ...)`.
- **`build_snapshot`:**
  - **Filtering:** `tool_calls` is filtered by `query`, with these rules:
    - `tools` applies, with `engine` meaning runs;
    - `project_ids`, `models` and the date range (by the date of `at` in `tz`) apply;
    - a non-empty `tasks` or `states` filter excludes every call;
    - a call is approved exactly when its `project_id` is an approved project (the `is_approved` rule).
  - **Totals and spend:**
    - approved calls feed the period totals and `project_totals`, together with the new `calls` count and the `by_tool` mappings;
    - they also feed `spend.by_day`, `by_project`, `by_model` and `by_task` (key `tool:<tool>`), plus the new `spend.by_tool`;
    - `coverage` counts them too.
  - **Tool sessions:**
    - `tool_sessions_live` and `tool_sessions_recent` use `group_sessions` and `session_row`, over all filtered calls, approved or not, with `included_in_totals` set;
    - sessions are live at `TOOL_LIVE_SECONDS` and recent at `TOOL_LIVE_SECONDS + RECENT_WINDOW_SECONDS`, both sorted newest call first.
  - **Live totals:** `live_totals` gains `tool_sessions` and `tool_burn_cost_per_hour`.
  - **Engine runs** contribute `by_tool["engine"]`, with `count` = runs and `sessions` None.

**Test cases** (`tests/test_pricing.py`, additions):
1. An exact cost with the Anthropic shape (no subtraction).
2. The OpenAI shape subtracts `cache_read` from input.
3. `cache_write_5m > 0` gives `upper_bound`.
4. A null `cache_write` gives `upper_bound`.
5. `speed == "fast"` gives `missing_rate` with cost None.
6. A null model gives `missing_rate`.
7. A null `tokens_returned` gives `missing_usage`.
8. A pricing-history version effective before `at` wins over the baseline.

**Test cases** (`tests/test_tool_sessions.py`):
1. Grouping is by `(tool, root_session)`, and `sessions` collects the subagent sessions.
2. `session_row` fields:
   - `models` ordered by call count;
   - `subagents` = the number of distinct sessions minus 1;
   - `cost_classification` is the worst across calls;
   - `burn_per_hour` covers only calls in the last 60 minutes;
   - `project_id` comes from the latest call.

**Test cases** (`tests/test_snapshot_tool_usage.py`):
1. Totals include approved calls only. Pending projects and a null `project_id` are excluded.
2. `by_tool` has keys only for tools that have rows; `count` and `sessions` are right; `engine` counts runs.
3. Day boundaries: calls at 23:30 and 00:30 local fall on different days in `by_day`, and in `today` versus not today.
4. `spend.by_tool`, and the `tool:claude_code` key in `by_task`. With runs and calls together, every spend table sums to `all_time.known_cost`.
5. `coverage` counts calls.
6. Filters:
   - `tools=("engine",)` gives no calls in totals or sessions;
   - `tools=("kilo",)` gives no runs;
   - project, model and date filters apply to calls;
   - a `tasks` filter excludes calls.
7. Windows: a last call 599 s ago is live, 601 s is recent, and 1,501 s is neither.
8. `live_totals` has `tool_sessions` and `tool_burn_cost_per_hour`.
9. Without `tool_calls`, the payload equals the existing payload plus the new keys with empty values, and every existing snapshot test still passes.
10. A session in a pending project appears in `tool_sessions_live` with `included_in_totals: False`, and is absent from totals.

### Task 8: CLI and sync (`t8-cli-sync`, max-iters 45)

**Files:**
- Create: `tests/test_sync_tool_usage.py`
- Modify: `src/agent_control/cli.py`, `src/agent_control/sync.py`, `tests/test_cli.py`

**Interfaces:**
- **Consumes:**
  - from Task 6: `tool_import.import_tool_usage`, `load_usage_sources`, `write_usage_sources`, `resolve_source`, `default_source`, `SourceSetting`, `ImportReport` and `TOOLS`;
  - `instance_lock.acquire_instance_lock(state_dir)` and `InstanceLockError`.
- **Produces:**
  - `agent-control import-usage [--apply] [--tool {claude_code,codex,cline,kilo}]...`, with `--tool` repeatable;
  - `Synchronizer(..., tool_importer: Callable[[], ImportReport] | None = None)`;
  - `cli._tool_importer_for(paths, locks, clock, *, home, env) -> Callable[[], ImportReport] | None`.

**Behaviour.** This is §6.5.

**Tool selection:** the selected tools are the `--tool` values, or else every tool whose `default_source` exists. When none is selected, the command prints `No tool sources found.` and exits 1.

**Dry run** (without `--apply`):
- The command calls `import_tool_usage(apply=False)` and prints, for each tool:
  ```
  <tool>: <calls_seen> calls (<new_calls> new, <revised_calls> revised), <sessions> sessions, <first_at or -> to <last_at or ->, sent <n>, returned <n>, cache read <n>, cache write <n>
  ```
- It then prints `New projects pending approval: <n>`, followed by each root on its own line, indented two spaces.
- When there are diagnostics, it prints `<n> diagnostics:`, then one line each with `code` and `source`.
- Finally it prints `Dry run: nothing written.` and exits 0.

**With `--apply`:**
- It takes `acquire_instance_lock(paths.state_dir)` first. On `InstanceLockError` it prints `The dashboard is running. Stop it, run import-usage --apply, then start it again.` and exits 2.
- It runs the import with `apply=True` and releases the lock.
- It enables the selected tools in `usage_sources.json`, keeping any existing `path`.
- It prints the same report, ending with `Imported. Enabled: <comma-separated tools>.`

**`_tool_importer_for`:**
- It loads `usage_sources`, takes the enabled tools whose resolved source exists, and returns a closure that calls `import_tool_usage(paths, locks, tools=..., sources=..., apply=True, now=clock(), home=home)`.
- It returns None when no tool qualifies.
- `_run_serve` passes it to `Synchronizer` with `home=Path.home()` and `env=os.environ`.

**`Synchronizer._run_locked`:**
- After the existing work it calls `tool_importer()` when one is set.
- An exception becomes `Diagnostic("tool_import_failed", type(exc).__name__)`.
- The report's diagnostics are added to the `SyncReport` diagnostics. Follow how `SyncReport` already carries diagnostics.

**Test cases:**
1. A dry run prints the per-tool lines and `Dry run: nothing written.`, and the state dir is unchanged. Use fake sources under `tmp_path` and pass `--tool`.
2. `--apply` writes `tool_usage.jsonl` and sets `enabled: true` in `usage_sources.json`.
3. `--apply` while the instance lock is held exits 2 with the message.
4. `--tool kilo` imports only kilo.
5. With no sources, the command exits 1 with `No tool sources found.`
6. The sync calls the importer once per run.
7. An importer that raises gives a `tool_import_failed` diagnostic, and the sync report is still returned.
8. Without an importer, the existing behaviour is unchanged (the existing `tests/test_sync.py` passes).
9. `_tool_importer_for` returns None when nothing is enabled, and a callable when kilo is enabled with an existing temp db path.

### Task 9: Dashboard API and page (`t9-web`, max-iters 60)

**Files:**
- Modify: `src/agent_control/dashboard.py`, `web/dashboard.js`, `web/dashboard.css`, `web/index.html`, `tests/test_dashboard_api.py`, `tests/js/dashboard.test.mjs`, `tests/test_web_contract.py`

**Interfaces:**
- **Consumes:**
  - from Task 1: `tool_calls.ToolCallCache` and `TOOLS`;
  - the new `build_snapshot(tool_calls=...)` and `SnapshotQuery.tools` from Task 7, plus the payload keys of §8.
- **Produces:**
  - `GET /api/data?tool=...` (§8);
  - exported pure JS helpers, `toolLabel(id)` and `toolSessionRowModel(row, nowMs)`, which return plain objects for rendering. The JS tests target them.

**Behaviour:**
- **The dashboard:**
  - keeps one `ToolCallCache(paths.tool_usage)` and passes `tool_calls=list(map.values())` to `build_snapshot`;
  - parses `tool` (repeated or comma-separated) into `SnapshotQuery.tools`. An unknown id gives `ApiError(400, ...)`, following the existing bad-parameter style.
- **The page** implements §9:
  - **Live now and Recently ended:** tool session rows render after the run rows. Each shows:
    - the `TOOL_LABELS` name as a badge;
    - the project name, or `not in totals` when `included_in_totals` is false;
    - the models;
    - the last call as `endedAgoText`;
    - calls, tokens and cost (with `formatCost`);
    - burn per hour;
    - subagents, as `N subagents` when N > 0.

    Each row carries the line `Watch only. This session runs outside AgentControl.` It has no Steer, Kill, Dismiss or panel opening.
  - **Totals:** each period shows a `By tool` breakdown.
  - **Spend:** a `By tool` table is added.
  - **Filter:** the filter bar gets a `Tool` select with the five labels; `buildQuery` sends `tool`.
  - **Rendering:** data is rendered with `textContent` or `esc()` only.

**Test cases:**
1. **API:**
   - `tool=kilo,codex` and the repeated form both parse;
   - `tool=bogus` gives 400;
   - with a synthetic `tool_usage.jsonl` in the temp state dir, `/api/data` has `tool_sessions_live`, `by_tool` and `spend.by_tool`;
   - the sentinel placed in a fixture's `cwd` folder name appears only as that path, and the sentinel placed elsewhere (for example a junk extra file in the state dir) never appears.
2. **JS:**
   - `toolLabel` maps every id;
   - `toolSessionRowModel` gives the watch-only line, no control flags, `not in totals` for excluded rows, and the subagents text;
   - `buildQuery` includes `tool`.
3. **`test_web_contract.py`:** the Tool filter control exists in `index.html`, and `dashboard.js` uses no `innerHTML` with tool session data.

### Task 10: Operator guide and base-spec cross-reference (`t10-docs`, max-iters 25)

**Files:**
- Modify: `docs/operator-guide.md`, `tests/test_operator_docs.py`, `docs/superpowers/specs/2026-09-17-agent-control-design.md`

**Behaviour:**
- **Operator guide.** Add a section `## Usage from Claude Code, Codex, Cline and Kilo` covering:
  - what is read and where (the §3 table paths), and what is stored (§4.1, numbers only);
  - the preview, `agent-control import-usage`, then `agent-control import-usage --apply` with the dashboard stopped;
  - approving the new folders it lists;
  - list-price cost and "no rate";
  - watch-only rows;
  - turning a tool off by setting `"enabled": false` in `usage_sources.json`. Records already imported remain;
  - path overrides for VS Code forks and `CODEX_HOME`;
  - the §12 limits.
- **Base spec.** In the base spec, find the sentence or sentences saying accounting covers only engine runs (`grep -n` for "engine" near "accounting" or "ledger"). Add one sentence pointing to `2026-10-01-tool-usage-design.md` for the second, tool-call ledger.

**Test cases** (`tests/test_operator_docs.py`): the guide contains the section heading, `import-usage --apply`, `usage_sources.json` and `Watch only`.

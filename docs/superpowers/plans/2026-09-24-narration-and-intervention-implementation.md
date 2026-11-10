# Narration and Operator Intervention Implementation Plan

> **For agentic workers:** delegated agents on the OpenWebUI harness (`~/.claude/bin/delegate.py --worktree`) carry out this plan, one task per agent, with an orchestrator who reviews, applies and commits each patch.
> - Do not use superpowers:subagent-driven-development or the built-in Agent/Workflow tools. `~/.claude/DELEGATION.md` forbids them on this machine.
> - Each task gives exact contracts and enumerated test cases rather than finished code. The operator asked for the agents to write the code, test-first.

**Goal:** The operator can read any run's narrated transcript in a side panel, then steer it or kill it.

**Architecture:**
- A pure transcript parser serves one content endpoint, which reads on demand and never stores anything.
- Steering appends to the engine's own `.steer` file, and delivery is tracked in memory against the parsed transcript.
- Kill re-checks the process identity, then terminates the engine process tree. An append-only log records metadata only.
- A new ES module, `web/panel.js`, renders the panel and replaces the old step drawer.

**Tech stack:** Python 3.10+ standard library, optional `psutil` (already used by `liveness.py`), vanilla ES modules and `node --test`. No new dependencies.

**Spec:** `docs/superpowers/specs/2026-09-24-narration-and-intervention-design.md`. Sections are cited as §n.

## Global Constraints

- **Engine and versions.** No change to `engine/`, `HARNESS_VERSION`, `INDEX_SCHEMA` or any version number.
- **Tests.** Every test uses `tmp_path` and synthetic text. Nothing reads or writes `C:\Users\operator\.claude` or live state.
- **The content boundary.** Transcript text leaves the transcript only through `GET /api/narration`. It never reaches the ledger, the activity archive, `operator-actions.jsonl`, sync state, server logs or error messages.
- **Rendering.** The page renders transcript text with `textContent` or `esc()`. `web/panel.js` contains no `innerHTML` at all.
- **Binding.** Loopback only. POST keeps its Host, Origin, CSRF and JSON checks. Every GET checks Host.
- **Kill.** Kill acts only on `start.engine_pid`, after `liveness._process_identity_valid` passes on a fresh `psutil.Process`, and only on that process and its descendants.
- **Steering.** A steer never overwrites existing `.steer` content; it appends, with a `"\n\n"` separator.
- **Copy.** UI copy uses plain sentences in sentence case. The strings in §7.4 and §12 are used verbatim.

## Review Focus

These are failure modes the spec implies but the obvious tests would miss. Each has a pinned test in its owning task.

1. **Transcripts from older engines**: no heartbeats, no `- steer:` header line. They must still parse, without crashing. *(Task 1, test `test_v5_style_transcript_parses`.)*
2. **A steer queued behind a long model call** stays `queued` however many events arrive, as long as its text is still in the file. *(Task 3, test `test_queued_survives_new_events`.)*
3. **Kill clicked twice, or from two tabs.** The second call returns 200 `already_gone`, not an error. *(Task 5, test `test_second_kill_reports_already_gone`.)*
4. **A PID recycled between the last sync and the click** makes identity fail. Nothing is terminated and the response is 409 `not_verified`. *(Task 2, test `test_identity_mismatch_touches_nothing`; Task 5, test `test_kill_refuses_recycled_pid`.)*
5. **A 1 MB single-line narration or output** parses in linear time and wraps on screen. *(Task 1, test `test_huge_single_line_is_linear`; Task 4, test `panel.css wraps long lines`.)*

## File Structure

| File | Task | Responsibility |
|---|---|---|
| `src/agent_control/narration.py` (new) | 1 | Transcript grammar to header, brief and events; masking; capped reads |
| `tests/test_narration.py` (new) | 1 | Parser and masking tests |
| `src/agent_control/process_control.py` (new) | 2 | Verified termination of the engine process tree |
| `src/agent_control/operator_log.py` (new) | 2 | Append-only, metadata-only operator actions |
| `tests/fake_psutil.py` (new) | 2 | The fake psutil shared with Task 5 |
| `tests/test_process_control.py`, `tests/test_operator_log.py` (new) | 2 | Tests for Task 2 |
| `src/agent_control/steering.py` (new) | 3 | Steer validation, file protocol, delivery state, `SteerBook` |
| `tests/test_steering.py` (new) | 3 | Tests for Task 3 |
| `web/panel.js`, `web/panel.css`, `tests/js/panel.test.mjs` (new) | 4 | The side panel |
| `src/agent_control/snapshot.py`, `tests/test_snapshot.py` | 6 | Control flags and the killed-by-operator reason |
| `src/agent_control/dashboard.py`, `src/agent_control/sync.py`, `tests/test_narration_api.py` (new), `tests/test_intervention_api.py` (new), `tests/test_dashboard_api.py`, `tests/test_sync.py` | 5 | Endpoints, GET Host check, CSP, `OpenRunObservation.start`, snapshot plumbing |
| `web/dashboard.js`, `web/dashboard.css`, `web/index.html`, `tests/js/dashboard.test.mjs`, `tests/test_web_contract.py` | 7 | Rows open the panel; the old drawer is removed |
| `tests/fake_engine.py`, `tests/test_intervention_e2e.py` (new) | 8 | A real-process end-to-end test |
| `docs/operator-guide.md`, `tests/test_operator_docs.py` | 9 | The operator guide section |

## Waves and integration protocol

| Wave | Tasks, in parallel | Starts after |
|---|---|---|
| 1 | 1, 2, 3, 4, 6 | spec, amendments and plan committed |
| 2 | 5, 7 | wave 1 applied and committed |
| 3 | 8, 9 | wave 2 applied and committed |

**Per agent:**
- **Launch:**
  ```
  timeout 5400 python -u ~/.claude/bin/delegate.py -C "D:/repos/AgentControl" --worktree -m anthropic_tool_call.claude-opus-4-8-think -e medium --label <label> --patch <label>.patch --max-iters <n> -t "$(cat <brief>)"
  ```
- **Watcher:** `python -u ~/.claude/bin/agent_await.py --repo "D:/repos/AgentControl" --label <label>`, run beside it.
- **After it ends, the orchestrator:**
  1. reviews the patch;
  2. runs `git apply --check` and then `git apply`;
  3. runs `python -m pytest -q` and `node --test tests/js/`;
  4. commits one commit per task;
  5. removes the worktree.

**Labels:** `n1-narration`, `n2-control`, `n3-steering`, `n4-panel`, `n5-api`, `n6-snapshot`, `n7-integrate`, `n8-e2e`, `n9-docs`.

## Task 0 (done in the spec commit)

- The spec was written.
- `AGENTS.md` (the content and scope rules), `README.md` and the base spec §1, §2, §3, §16 and §24 were amended as the spec's §3 lists.

---

### Task 1: Transcript narration parser (`n1-narration`, max-iters 45)

**Files:**
- Create: `src/agent_control/narration.py`
- Create: `tests/test_narration.py`

**Interfaces:**
- Consumes: `agent_control.activity._STEP` (the regex for `### [N] name(args)`).
- Produces:

```python
MAX_NARRATION_BYTES = 8_000_000
TRUNCATED_NOTICE = "transcript larger than 8 MB; later steps are not shown"

@dataclass(frozen=True)
class NarrationHeader:
    label: str | None
    model: str | None
    effort: str | None
    tool_mode: str | None
    writes: str | None      # "disk", "staged" (from "staged (patch only)"), else the raw value
    root: str | None
    worktree: bool          # root contains ".claude\worktrees\" or ".claude/worktrees/"
    def as_dict(self) -> dict[str, object]: ...

@dataclass(frozen=True)
class NarrationEvent:
    seq: int                 # 0-based position in the parse
    kind: str                # call|narration|tool|anomaly|guidance|finish|notice|final
    iteration: int | None    # None for notice and final
    data: dict[str, object]  # kind-specific fields, spec §6.2
    def as_dict(self) -> dict[str, object]: ...  # {"seq","kind","iteration", **data}

@dataclass(frozen=True)
class Narration:
    header: NarrationHeader | None
    brief: str | None
    events: tuple[NarrationEvent, ...]
    truncated: bool = False

def parse_narration(text: str, *, task_text: str | None = None) -> Narration: ...  # raw, unmasked
def read_narration(path, *, max_bytes: int = MAX_NARRATION_BYTES,
                   task_text: str | None = None) -> Narration | None: ...
def read_task_text(messages_path, *, max_bytes: int = 2_000_000) -> str | None: ...  # first "user" message content
def mask_secrets(text: str) -> tuple[str, int]: ...
def masked_dict(event: NarrationEvent) -> tuple[dict[str, object], int]: ...
def guidance_entries(narration: Narration) -> list[tuple[int, int | None, str]]: ...  # (seq, iteration, raw text)
```

**Behaviour:**
- **Grammar.** Exactly as in §6.1, with the element table, notice patterns and brief rule applied verbatim.
- **Kind-specific fields**, §6.2:

  | kind | `data` fields |
  |---|---|
  | `call` | `attempt`, `max_attempts` |
  | `narration` | `text`, `reasoning` |
  | `tool` | `name`, `args`, `output`, `output_truncated` |
  | `anomaly` | `name`, `output` |
  | `guidance` | `text` |
  | `finish` | none |
  | `notice` | `text` |
  | `final` | `text`, `partial` |

- **Tool output.** It excludes the fence lines. When the last content line is `...[truncated in transcript]`, drop that line and set `output_truncated: true`. A header with no fenced block after it gets `output: None`.
- **Reading.**
  - `read_narration` returns None for a missing or unreadable file.
  - It reads bytes and decodes UTF-8 with `errors="replace"`.
  - It converts `\r\n` and lone `\r` to `\n`.
  - When the file is larger than `max_bytes`, it parses the first `max_bytes` bytes, cut back to the last newline, and appends a `notice` event with `text = TRUNCATED_NOTICE`, then sets `truncated=True`.
- **Masking.** `mask_secrets` applies the eight rules of §6.3, in order. `masked_dict` masks every `str` value in `as_dict()` except `kind`, and returns the total count.
- **Speed.** Parsing is a single forward pass over lines, and no regex may backtrack across the whole text.

**Tests** (`tests/test_narration.py`, synthetic text only; build transcripts with small helper functions):

| Test | Input | Expect |
|---|---|---|
| `test_header_fields` | header with `writes: \`disk\`  root: \`.claude\worktrees\w1\`` | label, model, effort, tool_mode parsed; `writes == "disk"`; `worktree is True` |
| `test_staged_writes_normalised` | `writes: \`staged (patch only)\`` | `writes == "staged"`, `worktree is False` |
| `test_brief_keeps_inner_rules` | brief containing a `---` line, then `---`, then `_[ 1] calling …` | brief includes the inner `---` and the text after it |
| `test_brief_without_body` | header and task only | brief is the task text |
| `test_call_events_and_retries` | three heartbeats for turn 2 (attempts 1/4, 2/4, 3/4) | three `call` events, iteration 2, attempts 1, 2 and 3, `max_attempts` 4 |
| `test_empty_think_is_dropped` | `<think></think>Now let me read X.` | `narration`, text "Now let me read X.", reasoning None |
| `test_think_text_becomes_reasoning` | `<think>Plan A</think>I'll start.` | reasoning "Plan A", text "I'll start." |
| `test_tool_output_and_truncation` | tool whose output ends with `...[truncated in transcript]` | output without the marker, `output_truncated` True, args raw string |
| `test_output_with_inner_fence` | output containing a line with three backticks | output keeps that line |
| `test_native_turn_with_two_tools` | one thinking section, then two tool headers for turn 3 | narration then two `tool` events, all iteration 3 |
| `test_finish_and_anomalies` | `finish({})`, `NO_TOOL_ACTION({})`, `PARSE_ERROR({})` | `finish`; `anomaly` with name and output |
| `test_multiline_guidance` | `### [4] OPERATOR GUIDANCE`, then `> first` and `second` | guidance text "first\nsecond", iteration 4 |
| `test_guidance_precedes_call` | guidance for turn 4, then turn 4's heartbeat | the guidance seq is lower than the call seq |
| `test_notices` | give-up, cap, worktree note, staged note with a review suffix | four `notice` events, iteration None, no `**` |
| `test_final_report_partial_and_footer` | `---`, `## Final report`, `[PARTIAL - stopped …]` text, notice, footer | a `final` event with `partial` True; the notice comes after `final`; no footer event |
| `test_partial_last_tool` | tool header and opening fence with no closing fence | output is the text so far |
| `test_crlf_input` | the same transcript with `\r\n` | identical events to the LF version |
| `test_v5_style_transcript_parses` (Review Focus 1) | no heartbeats and no `- steer:` header line | header and events parse; no exception |
| `test_huge_single_line_is_linear` (Review Focus 5) | one narration line of 1,000,000 characters | parses in under 2 s |
| `test_read_narration_missing` | a path that does not exist | None |
| `test_read_narration_caps` | a file larger than `max_bytes=300` | `truncated` True; the last event is a notice with `TRUNCATED_NOTICE` |
| `test_seq_is_sequential` | any transcript | seq values are 0 to n-1 |
| `test_guidance_entries_raw` | guidance containing `sk-` plus 20 letters | the entry text is unmasked |
| `test_mask_rules` | one string per rule 1 to 8 | each masked; the count matches |
| `test_mask_leaves_code_alone` | `token = get_token(request)`, `password=abc` | unchanged, count 0 |
| `test_masked_dict_masks_output_and_args` | a tool event with a bearer token in its output and args | both masked; count 2 |

**Verify:** `python -m pytest tests/test_narration.py -q`, then `python -m pytest -q`.
**Commit:** `feat: parse delegated-run transcripts into narration events`

---

### Task 2: Verified kill and operator log (`n2-control`, max-iters 40)

**Files:**
- Create: `src/agent_control/process_control.py`
- Create: `src/agent_control/operator_log.py`
- Create: `tests/fake_psutil.py`
- Create: `tests/test_process_control.py`
- Create: `tests/test_operator_log.py`

**Interfaces:**
- Consumes:
  - `agent_control.liveness._process_identity_valid(start, snapshot) -> bool`
  - `agent_control.models.ProcessSnapshot(pid, command_line: tuple[str, ...], create_time: float, running: bool)`
  - `agent_control.run_index.StartEvent`
  - `agent_control.storage.append_jsonl(path, records, lock)`
  - `agent_control.storage.read_jsonl(path, *, source) -> (rows, diagnostics)`
- Produces:

```python
# process_control.py
class NotVerified(Exception): ...   # PID is not this run's engine, or cannot be checked
class KillDenied(Exception): ...    # the OS refused access

@dataclass(frozen=True)
class KillResult:
    outcome: str      # "terminated" | "partial" | "already_gone"
    engine_pid: int
    children: int     # descendants found before termination
    survivors: int
    def as_dict(self) -> dict[str, object]: ...

def terminate_engine(start: StartEvent, *, psutil_module=None,
                     grace_seconds: float = 5.0) -> KillResult: ...

# operator_log.py
LOG_NAME = "operator-actions.jsonl"
def log_path(state_dir: Path) -> Path: ...
def append_action(state_dir: Path, lock, *, ts: str, action: str, run_id: str,
                  outcome: str, kind: str | None = None, chars: int | None = None,
                  engine_pid: int | None = None, children: int | None = None,
                  survivors: int | None = None) -> dict[str, object]: ...
def read_actions(state_dir: Path) -> list[dict[str, object]]: ...
def killed_run_ids(actions) -> frozenset[str]: ...

# tests/fake_psutil.py  (Task 5 reuses it)
class NoSuchProcess(Exception): ...
class AccessDenied(Exception): ...
STATUS_ZOMBIE = "zombie"
class FakeProcess:
    def __init__(self, pid: int, cmdline: list[str], create_time: float, *,
                 child_pids: tuple[int, ...] = (), survive_terminate: bool = False,
                 survive_kill: bool = False, deny: bool = False): ...
    # pid; cmdline(); create_time(); is_running(); status(); children(recursive=False);
    # terminate(); kill(); alive: bool
class FakePsutil:
    NoSuchProcess = NoSuchProcess; AccessDenied = AccessDenied; STATUS_ZOMBIE = STATUS_ZOMBIE
    def __init__(self, processes: list[FakeProcess]): ...
    calls: list[str]   # ordered: "children:<pid>", "terminate:<pid>", "kill:<pid>"
    def Process(self, pid: int) -> FakeProcess: ...   # NoSuchProcess when absent or dead
    def wait_procs(self, procs, timeout=None) -> tuple[list, list]: ...  # (gone, alive)
```

**Behaviour:**
- **`terminate_engine`** follows §8 steps 1 to 7. psutil is imported lazily when `psutil_module` is None; when it is unavailable, raise `NotVerified("psutil_unavailable")`.
  - `children(recursive=True)` must be called before any `terminate()`.
  - `AccessDenied` from psutil becomes `KillDenied`.
  - When `start.engine_pid` is None, raise `NotVerified("no_process_identity")`.
- **`append_action`** builds exactly these keys: `schema` (1), `ts`, `action`, `run_id`, `outcome`, `kind`, `chars`, `engine_pid`, `children`, `survivors`. It appends one line with `append_jsonl` under `lock` and returns the record.
  - Raise `ValueError` when `action` is not in `{"steer", "steer_cancel", "kill"}`, or `run_id` or `outcome` is empty.
- **`read_actions`** returns only records with `schema == 1`, a known action and non-empty string `run_id`, `ts` and `outcome`, each reduced to the known keys.
  - It ignores malformed lines and never raises.
  - It memoises on `(st_size, st_mtime_ns)` per path in a module-level dict guarded by a lock.
- **`killed_run_ids`** returns the run ids with `action == "kill"` and `outcome` of `terminated` or `partial`.

**Tests:**

| Test | Setup | Expect |
|---|---|---|
| `test_identity_mismatch_touches_nothing` (Review Focus 4) | cmdline without `.claude\bin\delegate.py` | `NotVerified`; `calls` has no terminate or kill |
| `test_create_time_outside_window` | create_time 400 s before `start.ts` | `NotVerified` |
| `test_children_listed_before_parent_terminated` | engine with 2 children | `calls` order is children, then terminate engine, then terminate each child; outcome `terminated`, children 2, survivors 0 |
| `test_survivor_is_killed` | a child with `survive_terminate=True` | `kill:<child>` in calls; outcome `terminated` |
| `test_unkillable_is_partial` | a child with `survive_terminate` and `survive_kill` | outcome `partial`, survivors 1 |
| `test_already_gone` | PID not in the fake | outcome `already_gone`, children 0 |
| `test_access_denied` | engine with `deny=True` | `KillDenied` |
| `test_no_pid` | `start.engine_pid=None` | `NotVerified` |
| `test_real_process_tree_is_killed` | skip without psutil. Write `tmp/.claude/bin/delegate.py` that starts `[sys.executable, "-c", "import time; time.sleep(120)"]`, prints the child pid, then sleeps. Start it with `subprocess.Popen`, read the child pid, build a `StartEvent` with `ts` = now in UTC ISO and `engine_pid` = the Popen pid. | outcome `terminated`, children ≥ 1; both PIDs gone. `finally` kills any leftovers. |
| `test_append_and_read_back` | two appends | two records with exactly the known keys |
| `test_record_has_no_text_fields` | an append with `kind="steer"`, `chars=12` | no key named text, message or content; the file line has no free text |
| `test_malformed_lines_ignored` | the file has garbage, a wrong schema and an unknown action | only valid records returned |
| `test_read_is_memoised` | monkeypatch `read_jsonl` with a counter | two reads of an unchanged file give one call; after an append, two calls |
| `test_killed_run_ids` | kills with `terminated`, `partial`, `already_gone` and `not_verified`, plus a steer | only the first two run ids |
| `test_append_rejects_unknown_action` | `action="dispatch"` | `ValueError` |

**Verify:** `python -m pytest tests/test_process_control.py tests/test_operator_log.py -q`, then `python -m pytest -q`.
**Commit:** `feat: verified engine kill and metadata-only operator log`

---

### Task 3: Steering protocol and delivery tracking (`n3-steering`, max-iters 35)

**Files:**
- Create: `src/agent_control/steering.py`
- Create: `tests/test_steering.py`

**Interfaces:**
- Consumes: nothing from other tasks. Guidance entries arrive as `(seq, iteration, raw_text)` tuples, the shape Task 1's `guidance_entries` returns.
- Produces:

```python
MAX_STEER_CHARS = 4000
WRAP_UP_TEXT = ("Stop working now. Do not call any tool except finish. Call finish with "
                "your report: what you completed, what you verified and how, and what "
                "is still incomplete or unverified.")

class SteerError(Exception):
    def __init__(self, code: str, message: str): ...   # .code: invalid_text|no_transcript|bad_steer_path

def validate_text(text: object) -> str: ...           # trimmed text or SteerError("invalid_text")
def steer_path_for(transcript: str) -> Path: ...      # Path(transcript + ".steer") after checks
def read_steer_file(path: Path) -> str: ...           # "" when absent or unreadable
def append_steer(path: Path, text: str) -> None: ...
def cancel_steer(path: Path, text: str) -> bool: ...
def normalize(text: str) -> str: ...                  # whitespace runs -> one space, stripped

@dataclass(frozen=True)
class PendingSteer:
    id: str
    run_id: str
    text: str
    kind: str            # "steer" | "wrap_up"
    chars: int
    sent_at: str
    events_at_send: int

def delivery_state(pending: PendingSteer, *, guidance, steer_file_text: str,
                   run_open: bool) -> tuple[str, int | None]: ...
def status_dict(pending: PendingSteer, state: str, turn: int | None) -> dict[str, object]: ...

class SteerBook:
    def add(self, pending: PendingSteer) -> None: ...        # keeps the 5 newest per run
    def for_run(self, run_id: str) -> list[PendingSteer]: ...  # oldest first
    def get(self, run_id: str, steer_id: str) -> PendingSteer | None: ...
    def remove(self, run_id: str, steer_id: str) -> None: ...
    def lock_for(self, run_id: str) -> threading.Lock: ...   # one lock per run
```

**Behaviour:**
- **`validate_text`**
  - Accepts only a `str`.
  - Trims it; the result must be 1 to 4,000 characters.
  - Rejects any character below U+0020 other than `\n` and `\t`, and U+007F.
- **`steer_path_for`**
  - `no_transcript` unless the transcript ends with `.md` and `Path(transcript).is_file()`.
  - `bad_steer_path` if the steer path exists and is a directory or a symlink.
- **`append_steer`**
  - Reads the existing content.
  - Opens the file with `open(path, "a", encoding="utf-8", newline="\n")`.
  - Writes `"\n\n"` first when the existing content is not blank, then `text + "\n"`.
  - Flushes and fsyncs.
- **`cancel_steer`**
  - If the file contains `text`, removes its last occurrence together with one adjacent `"\n\n"` separator, and writes the remainder back with `open(path, "w", encoding="utf-8", newline="\n")`. A blank remainder becomes the empty string.
  - Returns whether it removed anything.
- **`delivery_state`** follows the order in §7.4:
  1. `("delivered", iteration)` for the first guidance entry with `seq >= events_at_send` where `normalize(pending.text) in normalize(entry_text)`;
  2. else `("undeliverable", None)` if not `run_open`;
  3. else `("queued", None)` if `normalize(pending.text) in normalize(steer_file_text)`;
  4. else `("lost", None)`.
- **`status_dict`** returns `{"id", "state", "kind", "chars", "sent_at", "turn"}` and never the text.

**Tests:**

| Test | Expect |
|---|---|
| `test_validate_trims_and_accepts_newlines_tabs` | `"  a\n\tb  "` becomes `"a\n\tb"` |
| `test_validate_rejects` | `""`, `"   "`, 4,001 characters, `"a\x00b"`, `"a\x07b"`, `5` (not a str) each raise `SteerError` with code `invalid_text` |
| `test_steer_path_requires_md_file` | a missing transcript, and a `.txt` file, give `no_transcript` |
| `test_steer_path_rejects_directory` | a directory at `<t>.md.steer` gives `bad_steer_path` |
| `test_steer_path_rejects_symlink` | a symlink there gives `bad_steer_path` (skip on `OSError` when symlinks are not permitted) |
| `test_append_to_absent_file` | bytes are `b"hello\n"` |
| `test_append_after_existing_guidance` | existing `b"orchestrator says\n"`; append `"mine"`; the content ends with `b"\n\nmine\n"` and still starts with `b"orchestrator says"`; no `b"\r"` |
| `test_cancel_removes_only_our_segment` | after the previous test, cancelling `"mine"` gives True and the content is `"orchestrator says\n"` or has that as its stripped text |
| `test_cancel_absent_returns_false` | False; the file is unchanged |
| `test_delivered` | a guidance entry `(seq=10, iteration=4, "[x] please focus on tests")` with `events_at_send=8` and text `"please  focus on tests"` gives `("delivered", 4)` |
| `test_old_guidance_does_not_count` | the same entry with `events_at_send=11` falls through to queued or lost |
| `test_queued_survives_new_events` (Review Focus 2) | the file still contains the text, with no guidance and `events_at_send=3`, gives `queued` |
| `test_lost` | the file lacks the text, there is no guidance and the run is open: `lost` |
| `test_undeliverable` | the run is not open and the text is not delivered: `undeliverable` |
| `test_status_dict_has_no_text` | the dict keys are exactly id, state, kind, chars, sent_at and turn |
| `test_book_keeps_five_newest` | after 7 adds, `for_run` returns the last 5; `get` and `remove` work |
| `test_book_lock_is_per_run` | `lock_for("a") is lock_for("a")` and `is not lock_for("b")` |

**Verify:** `python -m pytest tests/test_steering.py -q`, then `python -m pytest -q`.
**Commit:** `feat: steer file protocol with delivery tracking`

---

### Task 4: The run side panel (`n4-panel`, max-iters 50)

**Files:**
- Create: `web/panel.js`
- Create: `web/panel.css`
- Create: `tests/js/panel.test.mjs`

**Interfaces:**
- Consumes (HTTP, from Task 5):
  - `GET /api/narration?run_id&after`, spec §6.4;
  - the existing `GET /api/activity?run_id`, whose response has:
    - top-level keys `steps[]`, `current_phase`, `current_phase_since`, `progress`, `server_now` and `status`;
    - on each step: `iteration`, `phase`, `tool`, `path`, `started_at`, `model_seconds`, `tool_seconds`, `tokens_sent`, `tokens_returned` and `cost`;
  - `POST /api/runs/{id}/steer` with `{text}` or `{kind: "wrap_up"}`;
  - `POST /api/runs/{id}/steer/cancel` with `{id}`;
  - `POST /api/runs/{id}/kill` with `{confirm: true}`.

  POST headers are `{"Content-Type": "application/json", "X-AgentControl-CSRF": token}`, and GET narration sends the same CSRF header.
- Consumes (the row objects in `/api/data`):
  - **`live[]` rows** have `run_id`, `project_id`, `label`, `model`, `effort`, `timestamp`, `status`, `display_state`, `state_reason`, `attention`, `verified`, `dismissed`, `elapsed_seconds`, `max_iters`, `iteration`, `phase`, `attempt`, `max_attempts`, `cost`, `cost_classification`, `tokens_sent` and `tokens_returned`. From Task 6 they add `can_steer`, `can_kill` and `control_reason`.
  - **`recently_ended[]` rows** add `end_reason` and `ended_at_effective`.
  - **`runs[]` (history) rows** add `killed_by_operator` from Task 6.
- Produces (ES module exports, used by Task 7):

```js
export function summarizeToolCall(name, args) {}      // string, per §12 table
export function mergeEvents(existing, after, response) {}  // {events, reset}
export function nextAfter(events) {}                  // Math.max(0, events.length - 1)
export function groupTimeline(events) {}              // [{type:"turn", iteration, events}, {type:"notice"|"final", event}]
export function steerStatusText(status) {}            // §12 strings
export function killConfirmText(label, header) {}     // §8 consequences
export function killOutcomeText(result) {}
export function controlsState(row) {}                 // {steer:{enabled, reason}, kill:{enabled, reason}}
export function nearBottom(scrollTop, scrollHeight, clientHeight, slack = 40) {}
export function outputLabel(event) {}                 // "output · 1,234 chars · truncated"
export function reasoningFolded(text) {}              // true when more than 3 lines
export function runIdFromHash(hash) {}                // "#run=<id>" -> id or null
export function hashForRun(runId) {}
export function createRunPanel(deps) {}               // {open(runId, opener), close(), update(data), isOpen(), currentRunId()}
```

- `deps` holds:
  - `fetchJson(url, options)`, which rejects with an `Error("HTTP 409: code (message)")`;
  - `csrfToken()`;
  - `projectName(projectId)`;
  - the formatters `phaseText`, `stepTimingText`, `formatElapsed`, `formatCost`, `iterationText` and `liveStateClass`;
  - `onChange()`.

**Behaviour:**
- **Helpers.**
  - **`summarizeToolCall`** parses `args` as JSON when it can:

    | Tool | Returns |
    |---|---|
    | `read_file` | `path` plus `:start-end` (or `:start-` / `:-end`) |
    | `grep` | `"<pattern>"` plus ` in <glob>` when a glob is given |
    | `run_command` | `argv.join(" ")` |
    | `write_file`, `replace_in_file`, `list_dir` | `path` |
    | `finish` | `"finish"` |
    | anything else, or unparseable args | `` `${name} ${args.slice(0, 120)}` ``, trimmed |

  - **`mergeEvents`** returns `{events: existing.slice(0, after).concat(response.events), reset: false}`, or `{events: [], reset: true}` when `response.total < after`.
  - **`groupTimeline`** groups consecutive events with the same non-null iteration into one turn. Notice and final events become their own items, in order.
  - **`steerStatusText(status)`** returns a prefix, then ` · ${chars} chars · `, then the state text:
    - the prefix is `"Wrap-up request"` for kind `wrap_up`, otherwise `"Steer"`;
    - the state text is `queued` "Queued: arrives at the agent's next turn", `delivered` `Delivered at turn ${turn}`, `lost` "Not delivered: the steer file was overwritten or consumed without it; send again", or `undeliverable` "Not delivered: the run ended".
  - **`killConfirmText(label, header)`** starts `Kill ${label}? Its engine and child processes stop immediately and it writes no final report.` and adds one sentence by write mode:
    - staged: " Its staged edits exist only in the engine's memory and will be lost."
    - worktree: ` Edits made so far stay in ${header.root}.`
    - disk: " Edits made so far are already in the repository."
    - header null: " Its write mode is unknown."
  - **`killOutcomeText`** returns:
    - terminated with 0 children: "Engine terminated.";
    - with 1 child: "Engine and 1 child process terminated.";
    - with n children: `Engine and ${n} child processes terminated.`;
    - partial: `${survivors} process(es) did not exit (engine PID ${engine_pid}).`;
    - already gone: "The engine had already exited.".
  - **`controlsState(row)`**:
    - no row: both disabled, reason "The run has ended.";
    - otherwise `enabled` is `!!row.can_steer` / `!!row.can_kill`, and `reason` is `row.control_reason || "Not available for this run."` when disabled, or null.
  - **`outputLabel`** uses `toLocaleString("en-US")`.
- **DOM** (inside `createRunPanel`, which is only called in a browser). It follows §12:
  - It creates `<aside id="run-panel" class="run-panel" aria-labelledby="run-panel-title">` on first open, appended to `document.body`.
  - It toggles `document.body.classList` `run-panel-open`.
  - `Esc` closes. Focus moves to the title on open and returns to the opener on close.
  - It sets `location.hash` with `hashForRun`, and clears it on close.
- **Timeline.**
  - The task brief sits in a `<details>`.
  - Each turn gets a header with its reply time and model and tool seconds. They are matched by iteration from the `/api/activity` steps, using `deps.stepTimingText`.
  - Narration prose is a `<p>` with `white-space: pre-wrap`. Reasoning uses a `<details>` when `reasoningFolded`.
  - Each tool line uses `summarizeToolCall`, and its output is in a `<details>` labelled with `outputLabel`. Anomalies use the warning style.
  - Guidance is marked "Guidance, turn N". The final report is open.
  - When a narration response has `masked > 0`, a note says "N values masked".
  - When `available` is false, a note says "Transcript not found; showing timing only", followed by the activity steps.
  - All text goes through `textContent` or `document.createTextNode`. The file contains no `innerHTML`.
- **Controls** (live row only):
  - a textarea (`maxlength=4000`) where Ctrl or Cmd+Enter sends;
  - buttons Send, "Ask to wrap up" (after `confirm("Ask the agent to stop and call finish with its report?")`) and "Kill…" (after `confirm(killConfirmText(...))`);
  - one status line per `narration.steer` entry, with Cancel while it is `queued`;
  - disabled states from `controlsState`, with the reason shown;
  - after any POST, call `deps.onChange()`, and show errors inline.
- **Polling.**
  - For a live row, narration and activity every 2 s, using `after = nextAfter(events)` and `mergeEvents`. When `reset` is set, refetch with `after=0`.
  - For an ended row, a single fetch.
  - Polling pauses while `document.hidden`. A 100 ms ticker updates the current-phase line with `deps.phaseText`.
  - The view auto-scrolls when `nearBottom`. Otherwise a "N new steps" button appears.
- **`panel.css`.**
  - `.run-panel` is fixed right, full height, `width: min(46vw, 720px)`.
  - `body.run-panel-open main { margin-right: min(46vw, 720px); }`.
  - Below 900 px the panel is `width: 100vw` and `main`'s margin is 0.
  - Timeline text uses `white-space: pre-wrap; overflow-wrap: anywhere`.
  - The kill button uses a danger style.
  - It uses the existing colour variables from `dashboard.css` where they exist, and defines its own fallbacks.

**Tests** (`tests/js/panel.test.mjs`, importing `../../web/panel.js` the way `dashboard.test.mjs` imports `dashboard.js`):

| Test | Expect |
|---|---|
| `summarizeToolCall read_file range` | `("read_file", '{"path": "a.py", "start_line": 3, "end_line": 9}')` gives `"a.py:3-9"` |
| `summarizeToolCall grep with glob` | `"\"def x\" in src/**/*.py"` |
| `summarizeToolCall run_command` | argv joined with spaces |
| `summarizeToolCall truncated args` | invalid JSON gives the name plus up to 120 characters |
| `mergeEvents replaces the tail` | existing seq 0 to 4, after 4, response seq 4 and 5: result has 6 events and seq 4 replaced |
| `mergeEvents resets when shrunk` | `response.total < after` gives `reset` true |
| `groupTimeline groups consecutive turns` | turn 1, turn 1, turn 2, notice, final: four items in order |
| `steerStatusText all states` | the four exact strings, plus the wrap-up prefix |
| `killConfirmText by write mode` | staged, worktree, disk and null sentences |
| `killOutcomeText plurals` | 0, 1 and 2 children, partial, already gone |
| `controlsState` | null row, a disabled row with a reason, an enabled row |
| `nearBottom` | within 40 px gives true; 41 px gives false |
| `outputLabel` | `"output · 1,234 chars · truncated"` |
| `reasoningFolded` | 3 lines false; 4 lines true |
| `hash round trip` | `runIdFromHash(hashForRun("a b/c"))` equals `"a b/c"`; `runIdFromHash("#x")` is null |
| `panel.js never uses innerHTML` | reading the file source finds no `innerHTML` |
| `panel.css wraps long lines` (Review Focus 5) | the source contains `overflow-wrap: anywhere` and `pre-wrap` |

**Verify:** `node --test tests/js/panel.test.mjs`, then `node --test tests/js/dashboard.test.mjs`, then `python -m pytest -q`.
**Commit:** `feat(web): run side panel with narration timeline and controls`

---

### Task 6: Snapshot control flags and kill reason (`n6-snapshot`, max-iters 30)

**Files:**
- Modify: `src/agent_control/snapshot.py`, in `_build_live`, `_build_recently_ended`, `build_snapshot` and its history-row construction only.
- Modify: `tests/test_snapshot.py`

**Interfaces:**
- Consumes: operator actions as `Sequence[Mapping[str, object]]` of Task 2's record shape. Compute the killed set locally, with the same rule as Task 2's `killed_run_ids`: `action == "kill"` and `outcome` in `{"terminated", "partial"}`. Do not import `operator_log`; it is built in parallel.
- Produces:
  - `build_snapshot(runs, projects, baseline, history, query, *, now, observations=None, operator_actions=None, ...)`, keeping every existing parameter;
  - live-row keys `can_steer`, `can_kill` and `control_reason`;
  - recently-ended `end_reason = "killed by operator"`;
  - history-row key `killed_by_operator`.

**Behaviour:**
- **In `_build_live`,** with `obs` as the observation and `verified` as today:
  - `can_kill = verified and obs is not None and obs.engine_pid is not None`.
  - `can_steer = verified and not dismissed and bool(record.transcript_artifact)`.
  - `control_reason` is None when both are true. Otherwise it is the first of these that applies:
    - `"no process identity (schema-1 run)"` when `obs is None or obs.engine_pid is None`;
    - `"engine process not verified alive"` when not verified;
    - `"no transcript recorded"`;
    - `"dismissed by operator"`.
- **In `_build_recently_ended`,** a run in the killed set gets reason `"killed by operator"`. This comes before the dismissed and complete branches and takes no " (inferred)" suffix.
- **History rows** gain `killed_by_operator`, a bool.
- **Absence.** When `operator_actions` is None or empty, nothing changes apart from the new keys: `killed_by_operator` False, and the flags as above.

**Tests** (reuse the existing builders and fixtures in `tests/test_snapshot.py`; read its first 80 lines for them):

| Test | Expect |
|---|---|
| `test_verified_live_row_can_steer_and_kill` | `process_identity_valid` observation with `engine_pid`: both True, reason None |
| `test_schema1_row_reason` | `engine_pid` None: both False, reason "no process identity (schema-1 run)" |
| `test_unverified_row_reason` | probe-unknown observation: "engine process not verified alive" |
| `test_no_transcript_blocks_steer_only` | verified with an empty `transcript_artifact`: `can_kill` True, `can_steer` False, "no transcript recorded" |
| `test_dismissed_verified_row` | dismissed and verified: `can_steer` False, `can_kill` True, "dismissed by operator" |
| `test_recently_ended_killed_reason` | an interrupted run plus a kill action with `terminated`: `end_reason` "killed by operator" |
| `test_kill_already_gone_is_not_a_kill` | outcome `already_gone`: normal reason |
| `test_history_row_flag` | the killed run's history row has `killed_by_operator` True, others False |

**Verify:** `python -m pytest tests/test_snapshot.py -q`, then `python -m pytest -q`.
**Commit:** `feat: live rows say whether a run can be steered or killed`

---

### Task 5: Narration and intervention API (`n5-api`, wave 2, max-iters 50)

**Files:**
- Modify: `src/agent_control/dashboard.py`:
  - `DashboardApp.__init__` (a new keyword argument and new attributes);
  - `_load_state` (extract `_ledger_records()`);
  - `snapshot` (pass `operator_actions`);
  - the new methods `narration`, `steer`, `cancel_steer` and `kill`;
  - `make_handler`: the GET Host check, the narration route, POST routes and the `index.html` headers;
  - `_STATIC_FILES`, which gains `panel.js` and `panel.css`.
- Modify: `src/agent_control/sync.py` (`OpenRunObservation.start`)
- Create: `tests/test_narration_api.py`
- Create: `tests/test_intervention_api.py`
- Modify: `tests/test_dashboard_api.py` (the Host and CSP tests)
- Modify: `tests/test_sync.py` (a `start` test)

**Interfaces:**
- Consumes:
  - Task 1: `read_narration`, `masked_dict`, `mask_secrets`, `guidance_entries`, `Narration`;
  - Task 2: `terminate_engine`, `NotVerified`, `KillDenied`, `KillResult`, `append_action` and `read_actions`, plus `tests/fake_psutil.py`;
  - Task 3: `validate_text`, `steer_path_for`, `append_steer`, `cancel_steer`, `read_steer_file`, `SteerError`, `PendingSteer`, `SteerBook`, `delivery_state`, `status_dict` and `WRAP_UP_TEXT`;
  - Task 6: `build_snapshot(..., operator_actions=...)`.
- Produces:

```python
class DashboardApp:
    def __init__(self, paths, locks, synchronizer, clock, *, psutil_module=None): ...
    def narration(self, run_id: str, after: int) -> dict[str, object]: ...
    def steer(self, run_id: str, body: dict) -> dict[str, object]: ...
    def cancel_steer(self, run_id: str, body: dict) -> dict[str, object]: ...
    def kill(self, run_id: str, body: dict) -> dict[str, object]: ...

@dataclass(frozen=True)
class OpenRunObservation:  # sync.py; new LAST field with a default
    ...
    start: StartEvent | None = None
```

**Behaviour:**
- **`sync.py`.** Both `OpenRunObservation(...)` constructions pass `start=start`.
- **`narration()`.**
  - The record comes from `_ledger_records()`, the memoised list that `_load_state` now uses; an unknown run returns 404.
  - The parse goes through an in-memory LRU of 8, keyed by transcript path and `(st_size, st_mtime_ns)`, under a lock.
  - It calls `read_narration(transcript, task_text=read_task_text(<transcript without .md> + ".messages.jsonl"))`. The messages journal is read only up to its first user message, and never cached or returned.
  - The response is shaped as §6.4:
    - `brief` is set only when `after == 0`, and is masked;
    - events are `masked_dict` of `events[after:]`;
    - `masked` is the sum;
    - `steer` is `[status_dict(p, *delivery_state(p, guidance=guidance_entries(raw), steer_file_text=read_steer_file(Path(transcript + ".steer")), run_open=run_id in open_runs)) for p in book.for_run(run_id)]`.
  - A missing transcript gives `available: false`, with `header` and `brief` None, `events` empty and `total` 0.
- **`steer()`** follows the §7.1 preconditions in order, with `open_runs` from `getattr(self.synchronizer, "open_runs", {})`. Then, under `book.lock_for(run_id)`:
  1. `append_steer`;
  2. `events_at_send` is the `total` of a fresh parse;
  3. `PendingSteer(id=secrets.token_hex(8), ...)`, then `book.add`;
  4. `append_action(..., action="steer", outcome="queued", kind, chars)`.

  It returns `{"run_id", "id", "state": "queued", "kind", "chars", "sent_at"}`. `SteerError` codes map to 400 (`invalid_text`) or 409 (the rest).
- **`cancel_steer()`.** The body needs a string `id`. An unknown id returns 409 `not_pending`. Under the lock, `cancel_steer` returning True means `book.remove`, a log entry `steer_cancel`/`cancelled`, and `{"run_id", "id", "state": "cancelled"}`. Otherwise it logs `not_pending` and returns 409 `not_pending`.
- **`kill()`** follows the §8 preconditions: `confirm is True`, `open_runs`, `start` with `engine_pid`, and not `os.getpid()`. Then `terminate_engine(obs.start, psutil_module=self._psutil_module)`:
  - `NotVerified` is logged as `not_verified` and returns 409 `not_verified`;
  - `KillDenied` is logged as `access_denied` and returns 409 `access_denied`;
  - otherwise the outcome is logged with `engine_pid`, `children` and `survivors`, and the response is `result.as_dict() | {"run_id": run_id}`.
- **`snapshot()`** passes `operator_actions=operator_log.read_actions(self.paths.state_dir)`.
- **The handler.**
  - **Host check.** `_get_host_ok()` accepts `127.0.0.1:<port>` and `localhost:<port>`. It runs first in `do_GET` for every path, and a failure returns a JSON 400 `bad_host`.
  - **The narration route.** `/api/narration` needs `X-AgentControl-CSRF == token`, or it returns 403 `bad_csrf`. `run_id` must be exactly one non-empty value, and `after` an optional integer ≥ 0; otherwise 400 `invalid_query`.
  - **POST routes** in `_dispatch_post`, splitting `path[len("/api/runs/"):]` on `/`:
    - `[id, "steer"]` goes to `steer`;
    - `[id, "steer", "cancel"]` goes to `cancel_steer`;
    - `[id, "kill"]` goes to `kill`;
    - `[id, "dismiss"]` is unchanged.
  - **`index.html` headers.** Its response adds the §9 CSP, `X-Frame-Options: DENY` and `Referrer-Policy: no-referrer`.
  - **Static files.** `_STATIC_FILES` adds `"panel.js": "text/javascript; charset=utf-8"` and `"panel.css": "text/css; charset=utf-8"`.

**Tests.** Build the app like `tests/test_activity_api.py` does (`AppPaths.for_test(tmp_path)`, a MagicMock synchronizer, `StateLocks()`, a fixed clock). Seed the ledger with `sync_candidate` and `tests.test_ledger.run_candidate`. Set `synchronizer.open_runs = {run_id: OpenRunObservation(..., liveness=Liveness(display="running", persist_status="running", terminal=False, inferred=False, code="process_identity_valid"), start=StartEvent(...))}`. For kill, pass `psutil_module=FakePsutil([...])` from `tests/fake_psutil.py`.

| Test | Expect |
|---|---|
| `test_get_rejects_bad_host` | `GET /api/data` and `GET /` with `Host: evil.example:1` give 400 |
| `test_get_accepts_localhost_host` | `Host: localhost:<port>` gives 200 |
| `test_index_has_csp_and_frame_headers` | the CSP contains `frame-ancestors 'none'` and `script-src 'self'`; `X-Frame-Options` is DENY |
| `test_panel_assets_served` | `/static/panel.js` and `/static/panel.css` give 200 |
| `test_narration_requires_csrf` | 403 without the header |
| `test_narration_returns_events_and_brief` | after=0 has the brief and events; after=2 has no brief and events from seq 2 |
| `test_narration_masks_and_counts` | a transcript containing `sk-` plus 20 letters in a tool output: masked, `masked >= 1` |
| `test_narration_missing_transcript` | `available` False |
| `test_narration_unknown_run_404` | 404 |
| `test_narration_writes_nothing_to_state` | the set of files under `state_dir` is unchanged after 3 calls |
| `test_steer_appends_and_logs_metadata` | the file ends with the text; the operator log has one steer record without the text |
| `test_steer_preconditions` | `not_open`, `not_verified`, `invalid_text`, `no_transcript`, `invalid_request` (both text and kind) |
| `test_wrap_up_uses_canned_text` | the file contains `WRAP_UP_TEXT` |
| `test_steer_delivered_status` | after a steer, append `### [5] OPERATOR GUIDANCE\n\n> <text>` to the transcript and truncate the `.steer` file: `steer[0].state == "delivered"` and turn is 5 |
| `test_cancel_steer` | cancelled; the file no longer has the text; a second cancel gives 409 `not_pending` |
| `test_kill_requires_confirm` | 400 `confirm_required` |
| `test_kill_terminates_fake_tree` | outcome `terminated`, children 1; the log record has `engine_pid` and `children` |
| `test_kill_refuses_recycled_pid` (Review Focus 4) | a fake process whose cmdline lacks the engine marker: 409 `not_verified`; FakePsutil `calls` has no terminate |
| `test_second_kill_reports_already_gone` (Review Focus 3) | a second kill after the first gives 200 `already_gone` |
| `test_kill_not_open_and_no_identity` | 409 `not_open`; 409 `no_process_identity` |
| `test_snapshot_passes_operator_actions` | after a logged kill of a run that is now interrupted, `recently_ended` has "killed by operator" |
| `test_open_run_observation_has_start` (in `tests/test_sync.py`) | a sync over an open run sets `open_runs[id].start.engine_pid` |

**Verify:** `python -m pytest tests/test_narration_api.py tests/test_intervention_api.py tests/test_dashboard_api.py tests/test_sync.py -q`, then `python -m pytest -q`.
**Commit:** `feat: narration, steer and kill endpoints`

---

### Task 7: Open runs in the panel; remove the old drawer (`n7-integrate`, wave 2, max-iters 50)

**Files:**
- Modify: `web/dashboard.js`, `web/dashboard.css`, `web/index.html`
- Modify: `tests/js/dashboard.test.mjs`, `tests/test_web_contract.py`

**Interfaces:**
- Consumes: `createRunPanel` and `runIdFromHash` from `./panel.js` (Task 4).
- Produces: every live, recently-ended and history row can open the panel.

**Behaviour:**
- **Wiring.**
  - Add `import { createRunPanel, runIdFromHash } from "./panel.js";` at the top of `dashboard.js`.
  - In the DOM block, build `const panel = createRunPanel({...})` from:
    - the existing `fetchJson` and `csrfToken`;
    - `projectName: (id) => dash.names[id] || id`;
    - the existing exported `phaseText`, `stepTimingText`, `formatElapsed`, `formatCost`, `iterationText` and `liveStateClass`;
    - `onChange: load`.
  - `render()` calls `panel.update(dash.data)` after the tables render.
- **Opening the panel.**
  - The live table's first column becomes an Open button:
    - `aria-controls="run-panel"`;
    - `aria-expanded` true only for the open run;
    - label "Open run details".

    Recently ended and history rows get the same button in a new first column, with the header updated and `countColumns` used for colspans.
  - A click calls `panel.open(runId, button)`. The row of the open run gets the class `selected`.
  - After the first successful load, `runIdFromHash(location.hash)` opens that run.
- **Removals.**
  - Delete the live-row drawer: the `controllers` map, `makeController`, `makeDrawerRow`, `openRow`, `closeRow`, `toggleRow`, and the drawer-only step helpers `describeStep`, `fmtDur`, `fmtCost`, `stepKey`, `metaHtml` and `stepInnerHtml` when they are unused.
  - Delete the `#activity-panel` and `#activity-backdrop` markup, `openActivityPanel`, `closeActivityPanel` and their CSS.
  - Keep every exported pure helper.
- **CSP compatibility.** The rank bar at `dashboard.js` around line 944 builds `style="width:…%"` inside `innerHTML`, which the new CSP blocks. Render it with a `data-width` attribute, then set `el.style.width` from JS after insertion.
- **Styles.** `index.html` adds `<link rel="stylesheet" href="/static/panel.css" />` after `dashboard.css`.
- **The attention banner** and its Dismiss buttons keep working.

**Tests:**

| File | Change |
|---|---|
| `tests/test_web_contract.py` | Replace `'id="activity-panel"'` in the required controls with a check that `index.html` links `/static/panel.css`. Add `test_old_drawer_removed`: `dashboard.js` has no `openActivityPanel` or `makeController`. Add `test_dashboard_imports_panel`: `dashboard.js` contains `from "./panel.js"`. Add `test_no_inline_style_attributes_in_js`: `dashboard.js` has no `style="`. Keep `aria-expanded` asserted, since it is now on the Open buttons. |
| `tests/js/dashboard.test.mjs` | Keep it green. Remove only tests of deleted non-exported code, if there are any. |

**Verify:** `node --test tests/js/dashboard.test.mjs`, `node --test tests/js/panel.test.mjs`, then `python -m pytest -q`.
**Commit:** `feat(web): open any run in the side panel; remove the step drawer`

---

### Task 8: End-to-end proof with a real process (`n8-e2e`, wave 3, max-iters 40)

**Files:**
- Create: `tests/fake_engine.py`
- Create: `tests/test_intervention_e2e.py`

**Interfaces:** it consumes everything above over HTTP.

**Behaviour:**
- **`tests/fake_engine.py`** is a script. The test copies it to `tmp/.claude/bin/delegate.py` so it passes the identity check. It takes `--index`, `--transcript`, `--root`, `--label`, `--turns`, `--turn-seconds` and `--child-pid-file`, and it:
  1. starts a sleeping child (`[sys.executable, "-c", "import time; time.sleep(300)"]`) and writes its PID to `--child-pid-file`;
  2. appends a schema-2 start record to `--index`, with keys `schema, run_id, event="start", ts, harness_version=6, label, model="fake-model", effort="medium", tool_mode="native", root, cwd, engine_pid=os.getpid(), transcript, messages, usage_journal, write_mode="disk", max_iters=turns`, and `ts` in UTC ISO format with `Z`;
  3. writes the §6.1 header;
  4. for each turn: honours `<transcript>.steer` exactly like the engine, then writes the heartbeat, sleeps, and writes a thinking section containing `MARKER-NARRATION-XYZ` and a `read_file` tool with its fenced output;
  5. at the end, writes the final report, the footer and an end record.
- **`test_steer_then_kill_end_to_end`** is skipped when psutil is missing. It:
  1. uses `AppPaths.for_test(tmp)`, with the fake engine writing that `machine_index`;
  2. builds a real `Synchronizer(paths, locks, clock=lambda: datetime.now(timezone.utc), process_probe=PsutilProcessProbe())`, a real `DashboardApp` and `create_server(port=0)`;
  3. waits for `/api/data` to list the run as live with `can_steer` and `can_kill`;
  4. checks that `/api/narration` returns growing events;
  5. POSTs a steer, then polls narration until `steer[0].state == "delivered"`, with a 15 s limit;
  6. POSTs a kill, expecting `terminated` and children ≥ 1;
  7. polls `/api/data` until the run is in `recently_ended` with `end_reason == "killed by operator"`, with a 15 s limit;
  8. checks that the child PID is gone;
  9. checks that no file under `state_dir` contains `MARKER-NARRATION-XYZ` or the steer text;
  10. checks that `operator-actions.jsonl` holds two metadata records.

  A `finally` kills any survivors.
- **Defects.** If the test exposes a defect in `src/agent_control/*.py`, fix it minimally and name it in the report.

**Verify:** `python -m pytest tests/test_intervention_e2e.py -q`, then `python -m pytest -q`.
**Commit:** `test: end-to-end narration, steer and kill against a real process`

---

### Task 9: Operator guide (`n9-docs`, wave 3, max-iters 20)

**Files:**
- Modify: `docs/operator-guide.md` (a new section "## Watching, steering and killing an agent", after "## Reading Live now")
- Modify: `tests/test_operator_docs.py` (add the new phrases to the operator-guide list)

**Content:**
- **Opening a run.** Use the Open button, or `#run=<id>`.
- **What "thinking" shows**, and what it cannot show: `claude-opus-4-8-think` returns empty reasoning; GPT hides it; narration appears per step.
- **Folded output and masking.** Masking is a courtesy, not a boundary.
- **Steer semantics:**
  - it is delivered at the next turn, and not during a model call or its retries;
  - the four statuses;
  - text written by the orchestrator is never overwritten.
- **Ask to wrap up** is cooperative.
- **Kill:**
  - it applies only to verified engines;
  - the consequences by write mode;
  - what "partial" means;
  - the run appears as interrupted, "killed by operator".
- **Where actions are logged:** `%USERPROFILE%\.claude\agent-control\operator-actions.jsonl`, metadata only.
- **Security:** loopback only, the page cannot be framed, and narration needs the page's token.

**Tests:** extend the phrases in `test_operator_guide_covers_safety_and_commands` with "Ask to wrap up", "killed by operator", "operator-actions.jsonl" and "next turn".

**Verify:** `python -m pytest tests/test_operator_docs.py -q`, then `python -m pytest -q`.
**Commit:** `docs: watching, steering and killing an agent`

---

## Final verification (orchestrator)

1. Run `python -m pytest -q` and `node --test tests/js/`, and confirm both are green.
2. Run a headless-Chrome smoke:
   - start a temporary server with the fake engine running;
   - open `/#run=<id>`;
   - check that the panel renders the timeline;
   - send a steer, and watch it become delivered;
   - kill the run, and watch it move to Recently ended as "killed by operator".
3. Time `/api/narration` on the largest real transcript. It is read-only and parsed in memory.
4. Update the implementation-status table in the spec and report to the operator.

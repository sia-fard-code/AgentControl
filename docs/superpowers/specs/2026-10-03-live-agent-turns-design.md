# Live agent turns: design

- **Date:** 2026-10-03.
- **Status:** draft, for the operator's review. The operator watched agent runs and saw each step only after its turn ended. Live view existed for one-shot calls only (`2026-10-02-live-oneshot-thinking-design.md`, §3.4 deferred agent runs). The operator chose to stream both GPT and Claude agent turns.
- **Changes:** `engine/delegate.py` (agent loop), `src/agent_control/narration.py`, `web/panel.js`, and `~/.claude/bin/agent_await.py`.
- **Related:** `2026-10-02-reply-limits-design.md` (cut handling, timeouts, causes) and `2026-10-02-live-oneshot-thinking-design.md` (streaming, transcript format, narration live events).

## 1. Goal

While an agent turn is in flight, the dashboard shows what the model is producing:
- **GPT:** its reasoning summary, as it arrives;
- **both models:** the step's note, which is the short text the model writes before its tool call;
- **both models:** the text of a file being written (`write_file`, `replace_in_file`) and of the final report (`finish`), as it is produced.

Today each agent turn is one non-streamed request (`delegate.py`, agent loop). The dashboard gets nothing until the whole reply is back. On 2026-10-03, 10 of the last 12 runs were agent runs.

## 2. What the gateway provides (measured 2026-10-03)

Probes with the engine's own tool list; scripts in the session scratchpad.

| Route | Reasoning | Note and tool calls |
|---|---|---|
| Claude, `/api/chat/completions`, not streamed (today) | `<think></think>`, always empty. All 29 blocks in iptv's `lane-complete-1` were empty. | whole reply at the end |
| Claude, same route, `"stream": true` | `<think></think>`, empty | `delta.content` pieces. Tool calls arrive in standard chunks: the first carries `index`, `id` and `function.name`, and the rest carry argument pieces. `finish_reason` is `stop` even for a complete tool call. `usage` arrives, with or without `stream_options`. |
| GPT, `/api/chat/completions` (today) | none | whole reply at the end |
| GPT, `/openai/responses`, `"stream": true`, function tools, `reasoning.summary: "auto"` | `response.reasoning_summary_text.delta` from about 2 s (367 characters at high effort). It is not on every call: a quick medium-effort turn sent none. | `response.output_item.added` names the function call (with `call_id`) before its arguments. `response.function_call_arguments.delta` follows, then `response.output_item.done` with the complete item, then `response.completed` with usage. |

A second GPT turn whose `input` replayed the first turn's `function_call` and its `function_call_output`, after a `system` message, worked: the model went on to call `finish`. So the Responses route can carry the engine's whole conversation, statelessly.

## 3. Decisions

### 3.1 Agent turns stream

Each turn of the agent loop streams, by model:

| Model, tool mode | Request |
|---|---|
| `anthropic_tool_call.*`, native | `POST /api/chat/completions`: today's payload plus `"stream": true` |
| `anthropic_tool_call.*`, prompt | the same; the note streams, and the ```` ```tool ```` block is held back (§3.2) |
| `gpt-*`, native | `POST /openai/responses`, converted as below |
| anything else, and `gpt-*` in prompt mode | today's non-streamed request |

**The Responses request** for a GPT turn is `{"model", "input", "tools", "tool_choice": "auto", "stream": true, "reasoning": {"effort": <effort>, "summary": "auto"}}`, with `temperature` when given. It is built from the engine's chat-format history:

| History entry | Responses `input` item |
|---|---|
| `{"role": "system" \| "user", "content": C}` | the same message |
| `{"role": "assistant", "content": C}` | `{"role": "assistant", "content": C}`, omitted when C is empty |
| an assistant's `tool_calls[k]` | `{"type": "function_call", "call_id": id, "name": name, "arguments": args}` |
| `{"role": "tool", "tool_call_id": id, "content": R}` | `{"type": "function_call_output", "call_id": id, "output": R}` |

`native_tools()` entries become `{"type": "function", "name", "description", "parameters"}`.

**One reply shape.** Each route assembles a chat-shaped reply: `{"choices": [{"message": {"content", "tool_calls"}, "finish_reason"}], "usage"}`. Everything after the request stays as it is: the cut and prose corrections, nudges, dispatch, history and usage journaling.
- A GPT `function_call` item becomes `{"id": call_id, "type": "function", "function": {"name", "arguments"}}`.
- GPT's `content` is its `output_text` only. The reasoning summary goes to the transcript and never into history.
- Claude's tool calls are assembled by `index`, with argument pieces joined in order.

### 3.2 The transcript grows as the reply arrives

Within a turn, after the heartbeat line, the engine appends as text arrives. Flushing follows the one-shot rule: written at once, fsynced at most once a second.

```
_[ 3] calling gpt-6.1-sol (attempt 1/4, 9 msgs, ~41000 bytes) ..._

### [3] thinking

<think>
…reasoning summary, as it streams (GPT only)…
</think>

…the note, as it streams…

_[ 3] composing write_file src/app.py ..._
> first line of the file, as it streams
> second line
>
> (an empty line of the file is a bare ">")

### [3] write_file({"path": "src/app.py", "content": "…"})
…the result, written after the tool runs, as today
```

- **`### [i] thinking`** opens with the first reasoning or note text. A turn with neither has none, as today. `tr.step` does not repeat a note that was already streamed.
- **`<think>` holds only reasoning.** Claude's empty `<think></think>` is dropped by the existing `ThinkSplitter`.
- **The preview** is written for three fields: `write_file.content`, `replace_in_file.new_string` and `finish.summary`.
  - Its first line is `_[ i] composing <tool>[ <path>] ..._`, written when that field's text starts. The path is shown when the arguments named it first, which is the usual order.
  - The field's text follows, decoded from the streamed JSON. An escape sequence split across pieces, such as `\n`, `\"`, `\\` or `é`, is held until it is complete.
  - Every preview line starts with `> `, or is a bare `>` when empty. So no line of a file can start a step, a final report, a status line or the footer. This matters for Markdown files, transcripts and fixtures.
  - Other tools (`read_file`, `grep`, `list_dir`, `run_command`) compose in about a second and get no preview.
- **Prompt mode:** the note streams until the ```` ```tool ```` fence. The block itself is not written live; the step appears as today.
- **Unchanged:** the heartbeat, the tool step, status lines, notices, the final report, the footer, the messages journal, the usage rows and the end record. Standard output and standard error are unchanged.

### 3.3 Failures keep their causes

A streamed reply is classified with the reply-limits causes, and each cause keeps its handling:

| What happened | Cause | Handling (unchanged) |
|---|---|---|
| Claude: stream ended 100 s or more after it started (`REPLY_CUT_SECONDS`), with no tool call, or with arguments that are not valid JSON (in prompt mode, no complete tool block) | `reply_cut` | "smaller step" correction, at most 2 in a row |
| text but no tool call, under 100 s | `prose_only` (native) or `no_tool_block` (prompt) | nudge |
| no event for `REQUEST_TIMEOUT` (600 s, the same patience as today) | `timeout` | one more attempt, at xhigh after max |
| broken after its first event: connection lost, GPT `response.failed` or `error`, a Claude stream with no `[DONE]`, a GPT stream with no `response.completed` | `transport` | retried with backoff, up to 4 attempts |

- **A streamed cut's usage** travels in the reply, so the turn's usage row counts it. This is the same rule as the cut-usage fix for one-shots.
- **Partial live text stays in the transcript.** An open `<think>` or preview is closed, and the next status line or heartbeat follows. A retry starts its own `thinking` section.

Failures before the first event:
- **HTTP 400 "Model not found"** (a stale replica): refresh the models and retry, streaming, as `chat_with_model_retry` does today.
- **401 or 403:** exit, as today.
- **Any other 4xx:** the gateway rejects the streamed form. The turn is sent again through today's non-streamed path, and the rest of the run stays non-streamed. One notice is written: `**streaming unavailable (HTTP <code>); continuing without live view**`.
- **5xx, or a connection failure:** `transport`, retried with streaming.

### 3.4 An opt-out

`delegate.py --no-stream` sends every agent turn and one-shot through today's non-streamed path. It is an escape hatch if the gateway's streaming misbehaves. Nothing else changes, and `HARNESS_VERSION` stays as it is.

### 3.5 The dashboard shows the preview while it is written

- **`narration.py`:**
  - A `composing` line and the `> ` lines after it form one event of kind `preview`, with data `{tool, path, text, live}`. The text has the `> ` prefixes removed.
  - `live` is true only while the preview is the transcript's last element and there is no footer.
  - Lines starting with `>` never start an element, whatever follows, so hostile file text stays inside the preview.
  - The streaming notice joins the notice patterns.
- **`panel.js`:**
  - A live preview renders as "Writing src/app.py", "Editing src/app.py" or "Final report", with the existing "(writing…)" label, above the text. The text box has a capped height and keeps its end in view.
  - Once the preview is no longer live it renders nothing, because the step or final report that follows shows the result.
  - A streaming note needs no change. The panel always re-fetches its last event (`nextAfter`), so a growing note or preview gets its newest text, including its final text after a later element appears.
- **`activity.py`, `progress.py` and `snapshot.py`:** their patterns are anchored at the start of a line. So a `composing` line and preview lines are never steps, tool calls, attempts or cuts, even when a previewed file quotes a heartbeat or a cut line. Tests pin this, and also that the sanitized activity archive holds no preview text.
- **Content rule:** unchanged. The text stays in the run's own transcript, is read on demand by `GET /api/narration` and masked by `mask_secrets` there, and is never stored, logged or written by AgentControl.

### 3.6 The watchers

- **`agent_await.py`** needs two changes:
  - It looks for the last heartbeat only in the last 6,000 bytes, so a long preview could hide it. It will search the last 64 KB, while the footer check stays on the last line.
  - Its heartbeat pattern is unanchored, so a preview of a file that quotes a heartbeat line, such as a transcript fixture, could fake a retry alarm. The pattern will be anchored at the start of the line (`^_\[`), as AgentControl's is.

  It is deployed in `~/.claude/bin` with a backup and checked end to end, as in the 2026-10-03 watcher fix.
- **`agent-dash.ps1` and `agent-watch.ps1`:** no change. They show the newest `thinking` block, now including partial GPT reasoning, and the composing and preview lines match none of their patterns. A run on a synthetic transcript confirms it.

### 3.7 Out of scope

- **Claude's thinking.** The gateway sends it empty. Point 3 of the gateway admin note asks for it to be passed through, and nothing on our side can recover it.
- **Streaming the wrap-up request** made when the iteration cap is reached.
- **Carrying GPT reasoning items between turns** (`store` or encrypted reasoning). Today's chat route does not carry them either.
- **GPT in prompt mode,** which is only used when forced.

## 4. Testing

Synthetic text only, temporary paths, no network. Built on the engine harness from the reply-limits work: fake replies, `stream_replies`, and a fake clock through `engine._now`.

1. **GPT turn:**
   - from fake Responses events, the transcript holds an open `<think>` with summary text before the stream ends;
   - the note and then the tool step follow;
   - the reply has `tool_calls` with `call_id`, name and arguments;
   - the usage row comes from `response.completed`.
2. **Conversion:** a history with system, user, assistant-with-tool-calls, tool results, the cut placeholder and the correction converts to the table in §3.1, and call ids round-trip.
3. **Claude turn:**
   - from fake chunks with `<think></think>`, a note and indexed tool-call pieces, the note streams;
   - no empty `thinking` section is written;
   - the tool calls are assembled;
   - `finish_reason: "stop"` is accepted.
4. **Preview:**
   - `write_file` content streams with `> ` prefixes, including empty lines;
   - escapes split across pieces decode correctly (`\n`, `\"`, `\\`, `é` split mid-sequence);
   - `replace_in_file.new_string` and `finish.summary` preview;
   - `read_file` and `run_command` write nothing while composing;
   - the path appears when given first.
5. **Cut:**
   - a Claude stream ending at 121 s on the fake clock with truncated arguments is a `reply_cut` and gets the correction;
   - one ending at 121 s with valid arguments is a normal step;
   - one ending at 60 s with valid arguments is a normal step.
6. **Prose:** a streamed reply with no tool call gets the nudge, and the note is not written twice.
7. **Failures:**
   - a 400 before the first event re-sends the turn non-streamed, later turns stay non-streamed, and exactly one notice is written;
   - "Model not found" refreshes and retries streamed;
   - a 502 or a connection error is retried streamed;
   - 600 s with no event takes the timeout path, xhigh after max;
   - a failure mid-stream closes the open `<think>` or preview and retries.
8. **Opt-out:** with `--no-stream`, no streaming request is made, for agent turns or one-shots.
9. **Prompt mode, Claude:** the note streams, the ```` ```tool ```` block is held back, and the step is as today.
10. **Narration:**
    - a preview parses to one `preview` event, live only as the last element;
    - preview text containing `### [9] thinking`, `---` plus `## Final report`, a status line or a footer stays inside the preview;
    - the notice parses.
11. **Activity, progress and snapshot:**
    - a transcript with a preview counts the same steps, tool calls, attempts and cuts as without it, including a preview that quotes a heartbeat and a cut line;
    - the activity archive holds no preview text.
12. **Panel:** a live preview renders with its label and is removed once a later event arrives.
13. **`agent_await.py`,** with the end-to-end script:
    - a heartbeat behind a 20 KB preview still reads as mid-call;
    - a preview quoting `_[ 3] calling m (attempt 3/4, …)` raises no retry alarm.
14. **The existing suites stay green.**

## 5. Rollout

1. Engine and AgentControl changes go on a feature branch, implemented by harness agents. Engine tasks run one after another; the final review covers the whole branch.
2. The engine deploys with `python scripts/deploy_engine.py --apply` after the operator approves. AgentControl's changes take effect when the dashboard restarts. `agent_await.py` is replaced in `~/.claude/bin` with a backup. Runs already in flight keep the code they started with.
3. Live checks:
   - a `gpt-6.1-sol -e high` agent run on a small task shows reasoning on the dashboard before its first tool step;
   - a Claude agent run writing a 100-line file shows the file text growing under "Writing …";
   - `agent_await.py` stays quiet through both, then reports them finished.

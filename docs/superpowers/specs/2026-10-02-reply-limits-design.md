# How the engine handles the gateway's reply limits: design

- **Date:** 2026-10-02.
- **Status:** draft, for the operator's review. The operator chose the scope: treat a late `Error:` as the 2-minute cut and ask for a smaller step; keep prose replies and ask for a tool call; state the limit to every Claude agent; fall back to xhigh after a GPT timeout.
- **Changes:** `engine/delegate.py` (the shared engine) and the transcript lines AgentControl parses.
- **Related:** `2026-10-02-live-oneshot-thinking-design.md`, which streams one-shot calls and reuses the cut detection defined here.

## 1. Goal

Agents lose minutes per turn to failed replies that the engine resends unchanged. This design makes the engine recognise each kind of failure and respond to its cause, so a turn that failed for a known reason costs one short correction instead of up to four identical attempts.

## 2. What was measured (2026-10-02)

| Finding | Evidence |
|---|---|
| The gateway cuts every `anthropic_tool_call.*` reply at about 121 s | 401 logged cuts, median 121 s; 81 first attempts all 121–123 s; live tests at 121.5 s (think) and 121.0 s (non-think). No successful Claude reply in 1,747 turns took longer than 122 s. |
| Not streamed, a cut reply is HTTP 200 with content exactly `Error: ` | finish_reason `stop`, usage null, no tool calls. |
| Streaming does not escape the cut | Two long streams ended at 121.3 s and 121.1 s with `[DONE]` and finish_reason `stop`, after different amounts of text. A streamed tool call arrived with truncated, invalid arguments. |
| Resending the same request often repeats the cut | iptv `chacha-fast`, turn 8: four cuts in a row, about 8.5 minutes. |
| Prose-only replies are the other common failure | iptv `chacha-fast-2`, turns 24 and 26: replies of 2,782–5,759 characters with no tool call, each resent unchanged. The agent was blocked from running its check. |
| GPT has no 2-minute cut, but can go silent at max | GPT replies of up to 299 s succeeded. gpt-6.1-sol at max gave no reply within the 600 s client timeout on two open-ended planning calls; xhigh answered the same iptv question in 241 s. |

## 3. Decisions

### 3.1 Classify each failed attempt

`call_model` measures each attempt's wall time and classifies a reply that does not advance the loop:

| Cause | Rule |
|---|---|
| `reply_cut` | Content is a bare `Error:` (the existing `\s*Error:?\s*` test) and the attempt took at least 100 s. |
| `prose_only` | Native mode, no tool calls, and non-empty content that is not a bare `Error:`. |
| `empty` | No tool calls and empty content, or a bare `Error:` that took less than 100 s. |
| `timeout` | The client read timeout (`REQUEST_TIMEOUT`, 600 s). |
| `transport` | Any other transport failure, as today (HTTP 5xx, connection errors). |

The threshold is 100 s, not 120 s: every observed cut took at least 116 s, and a bare `Error:` that slow is never a quick gateway fault.

Each anomaly record gains `elapsed_s`, the attempt's wall time, and `cause` takes the values above. AgentControl does not read anomaly records, so it needs no change for them; old records keep their old values.

### 3.2 Respond to the cause

- **`reply_cut`: no identical resend.** `call_model` returns at once, and the loop appends one user message:

  > Your last reply took longer than the gateway's 2-minute limit, so it was discarded and nothing ran. Do the same work in a smaller step: call one tool, and keep any write_file or replace_in_file under about 120 lines. Write a big file in parts.

  Before it, the loop adds an assistant message `[reply discarded by the gateway's 2-minute limit]`, so the roles still alternate as they do on the existing `NO_TOOL_ACTION` path. At most two such corrections in a row. A third consecutive cut is handled like any other failed reply: it goes down the existing nudge path, which asks for a tool call and gives up with a wrap-up after three nudges in a row.
- **`prose_only`: no resend.** `call_model` returns at once, and the loop's existing `NO_TOOL_ACTION` path keeps the text as an assistant message and asks for one tool call or `finish`. Today that path runs only after four identical attempts.
- **`timeout`: one more attempt.**
  - At effort `max`, that attempt is sent at `xhigh`; at any other effort it is unchanged.
  - A second timeout fails the turn as today.

  One silent request then costs at most 20 minutes instead of 40.
- **`empty` and `transport`:** unchanged, with four attempts and backoffs of 2, 8 and 20 s.

The model-not-found refresh and the fatal-400 stop in `chat_with_model_retry` are unchanged.

### 3.3 Tell Claude agents the limit up front

When the model id starts with `anthropic_tool_call.`, the system prompt (`BASE_RULES_TEMPLATE`, rendered by `base_rules()`) gains one sentence:

> Each reply must arrive within about 2 minutes or the gateway discards it: call one tool per reply, and keep each write_file or replace_in_file under about 120 lines, writing large files in parts.

GPT prompts are unchanged, because GPT has no such limit.

### 3.4 One-shot calls

`run_oneshot` uses the same classification:
- **A `reply_cut`** is retried once, with this line added to the prompt: "Answer in under about 1,500 words; the gateway discards replies that take longer than 2 minutes."
- **A `timeout` at max** is retried once at xhigh.

### 3.5 What the transcript shows

Each case writes one line in the transcript's existing italic heartbeat style, so the narration names the cause instead of showing a bare retry:

- `_[ 8] reply cut by the gateway's 2-minute limit after 121 s; asking for a smaller step_`
- `_[24] reply had no tool call; asking for one_`
- `_[ 1] no reply within 600 s at max; retrying at xhigh_`

AgentControl's transcript parsing recognises these lines as status, with a test for each:
- `progress.py` reads the heartbeat lines;
- `snapshot.py` flags a run for attention from attempt 3 (`RETRY_ATTENTION_ATTEMPT`), and also flags a second cut in one turn;
- `narration.py` shows the lines in the narration.

## 4. What does not change

- The tools, repo policies, `CMD_TIMEOUT` and `REQUEST_TIMEOUT`.
- Non-streaming requests for agent runs. Streaming cannot get a reply past the cut (§2), so agent runs keep the clearer `Error:` signal.
- `DELEGATION.md` already tells briefs to keep Claude replies small; it gets one line saying the engine now corrects a cut reply itself.

## 5. Testing

All tests use a fake `chat` with scripted responses and a fake clock. No network, and only temporary paths.

1. A bare `Error:` after 121 s causes one call, then the correction message, with no identical resend.
2. A bare `Error:` after 5 s is retried with backoff, as today.
3. A third consecutive cut takes the existing nudge path, not another correction.
4. A prose-only reply causes one call, then the existing nudge; the prose is kept as an assistant message.
5. A timeout at max sends the next attempt with `reasoning.effort` set to xhigh. At high it is retried once unchanged. A second timeout fails the turn.
6. The system prompt carries the 2-minute sentence for `anthropic_tool_call.*` models and not for `gpt-*`.
7. A one-shot `reply_cut` retries once with the shorter-answer line.
8. Anomaly records carry `elapsed_s` and the new `cause` values.
9. AgentControl parses the three status lines.

The engine contract tests gate the deploy, as today.

## 6. Rollout

1. Engine change and tests on a feature branch.
2. Deploy with `python scripts/deploy_engine.py --apply`, after the operator approves. It installs all three harness scripts with backups and all-or-nothing rollback.
3. One live check: a Claude agent asked to write a 600-line file in one go should get the correction after the first cut and finish in parts.

## 7. Out of scope

- Raising the gateway's limit: that is the gateway admin's change (note drafted 2026-10-02).
- Streaming agent runs and showing their thinking (see the related design).

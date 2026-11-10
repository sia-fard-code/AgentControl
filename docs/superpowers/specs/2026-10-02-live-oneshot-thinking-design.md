# Live thinking for one-shot calls: design

- **Date:** 2026-10-02.
- **Status:** draft, for the operator's review. The operator asked to see what a model is thinking while it works, as Kilo's "Reasoning" panel shows. One-shot calls come first, because they are a black box for minutes.
- **Changes:** `engine/delegate.py` (one-shot mode only), `src/agent_control/narration.py`, and `web/panel.js`.
- **Related:** `2026-10-02-reply-limits-design.md` (cut detection, timeouts), and `2026-09-24-narration-and-intervention-design.md` (the narration view this extends).

## 1. Goal

A one-shot call should show on the dashboard while it runs:
- the model's reasoning summary, where the gateway provides one;
- its answer, as the text is produced.

One-shot calls are `ask-model.py`, fanout's synthesis and planner calls. Today the engine sends one non-streaming request and writes nothing until the whole reply is back, which takes minutes at xhigh or max.

## 2. What the gateway provides (measured 2026-10-02)

| Route | Reasoning text | Answer streaming |
|---|---|---|
| GPT, `POST /openai/responses`, `reasoning.summary: "auto"`, not streamed | Yes: an `output` item of type `reasoning`, with `summary[].text` (361 characters in the spike) | not applicable |
| GPT, `/openai/responses`, streamed | None in our three short tests (11–153 reasoning tokens). Kilo's `openwebui-responses` provider, which uses this route, did receive streamed summaries: 11 of its 34 reasoning parts in the last 3 days had text. | Yes: `response.output_text.delta` events |
| GPT, `/api/chat/completions`, with `reasoning.summary` | None; the parameter is accepted but nothing is returned | Raw Responses events, not chat chunks |
| Claude, `anthropic_tool_call.*`, either mode | None: the gateway strips thinking | Yes, but the stream is cut at about 121 s and still ends with `[DONE]` |
| Claude, `anthropic_pipe.*` (older Sonnet models only) | Yes: `<think>…</think>` inside `delta.content` | Yes |

So reasoning text is available for GPT only. Claude can show its answer live but not its thinking, until the gateway admin passes thinking through.

## 3. Decisions

### 3.1 One-shot calls stream

- **GPT (`gpt-*`)** sends `POST /openai/responses` with `{"model", "input": <prompt>, "reasoning": {"effort": <effort>, "summary": "auto"}, "stream": true}`, plus `temperature` when given. The engine reads these Server-Sent Events:

  | Event | Use |
  |---|---|
  | `response.reasoning_summary_text.delta` | reasoning text |
  | `response.output_item.done` for a `reasoning` item | its `summary[].text`, if no deltas came |
  | `response.output_text.delta` | answer text |
  | `response.completed` | usage, in the Responses shape the engine already normalises |
  | `response.failed` or `error` | an anomaly with cause `transport` |

- **Claude (`anthropic_tool_call.*`)** sends `POST /api/chat/completions` with `"stream": true`. The engine reads:
  - `choices[0].delta.content` for answer text;
  - any `<think>…</think>` inside it as reasoning, should the gateway ever pass it;
  - the final chunk's `usage`, if present.

  If the stream ends 100 s or more after it started, it counts as a `reply_cut` and is retried once with the shorter-answer line, as the reply-limits design says. A cut stream looks finished, so elapsed time is the only signal.
- **Other models** keep today's non-streaming request.
- **Fallback:** if a streaming request fails before its first event (an HTTP error, or a gateway that rejects `stream`), the engine sends the same call once without streaming, through today's path.
- **Idle timeout:** a stream that sends no event for 300 s is abandoned as a `timeout`, which the reply-limits design handles (max falls back to xhigh). A silent GPT call then stops after 5 minutes instead of 10.

### 3.2 The transcript grows as text arrives

The engine appends to the transcript as text arrives, flushing at most once per second:

```
### [1] thinking

<think>
…reasoning summary, as it streams…
</think>

---

## Final report

…answer, as it streams…
```

- The `thinking` section appears only if reasoning text arrives.
- `</think>` is written when the first answer text arrives, or at the end.
- The footer line (`[model e=… tools=oneshot: …]`), the messages journal, the usage row and the end record are written when the call ends, as today.
- Standard output still prints only the answer at the end, so `ask-model.py` and fanout keep working unchanged.

### 3.3 The narration shows the step while it is still being written

- **`narration.py`:**
  - it accepts a trailing `<think>` with no closing tag as reasoning;
  - it accepts a `## Final report` with no footer yet as the answer;
  - it marks both events `live: true`.
- **`/api/narration?after=`** re-sends a live event on every poll until it is complete, so a client always gets its newest text.
- **`panel.js`:**
  - it already polls every 2 s for live runs;
  - it replaces a live event when a longer version arrives;
  - while no text has come, its existing phase timer shows "thinking, 2m 10s".
- **Content rule:** unchanged. The text stays in the run's own transcript, is read on demand by `GET /api/narration` for the loopback operator, is masked by `mask_secrets`, and is never stored, logged or written by AgentControl (AGENTS.md's one exception).

### 3.4 Out of scope for this phase

- **GPT agent runs.** Per-step reasoning summaries need the tool loop moved to the Responses API (function_call items and their outputs). That needs its own design.
- **Claude agent runs.** The gateway provides no thinking. The existing phase timer already shows each step's elapsed time.
- **Asking the gateway admin to pass Claude thinking through:** that is in the admin note.

## 4. Open question, settled by the first task

Do streamed GPT summaries arrive for our requests? Kilo receives them and our short tests did not, so the first implementation task sends one streamed request with a long reasoning task, at least 1,000 reasoning tokens. It records which events arrive, trying Kilo's request options if plain `summary: "auto"` gives none. Kilo's `@ai-sdk/openai` provider is the working reference.

If streamed summaries never arrive, the design still holds without them: the answer streams live, the summary is written when its `reasoning` item completes, and the phase timer covers the gap before that.

**Result (2026-10-02, Task 0).** Streamed summaries do arrive. All three request variants streamed `response.reasoning_summary_text.delta` events, 91–102 of them (386–443 characters), starting about 1 s before the first answer text:
- (A) plain `summary: "auto"`;
- (B) `summary: "detailed"`;
- (C) with `store: false` and `include: ["reasoning.encrypted_content"]`.

The task was a gpt-6.1-sol rook-placement question at high effort, with 93–149 reasoning tokens. Plain `"auto"` is enough, so the payload needs no extra options. Earlier short tests (11–153 reasoning tokens) streamed none, so summaries are not guaranteed for every call.

## 5. Testing

Synthetic text only, temporary paths, no network.

1. **Engine, GPT:** a fake event stream with summary deltas, answer deltas and `response.completed`. The transcript has reasoning before the stream ends, the answer then follows, and the usage row comes from `response.completed`.
2. **Engine, Claude:** a fake chunk stream. The answer appears incrementally. A stream ending at 121 s on the fake clock is retried once as a `reply_cut`.
3. **Engine, fallbacks:**
   - an HTTP 400 on the streaming request falls back to one non-streaming call;
   - 300 s with no event is a `timeout`, and at max the retry goes at xhigh.
4. **Engine, contract:** standard output is still only the final answer, and the start and end records are unchanged.
5. **Narration:**
   - an unterminated `<think>` yields a live reasoning event;
   - a final report with no footer yields a live answer event;
   - once complete, both are ordinary events;
   - `after=` re-sends live events.
6. **Panel:** a live event is replaced by its longer version, not duplicated.

## 6. Rollout

1. The first task (§4).
2. Engine and AgentControl changes, on the same feature branch as the reply-limits work.
3. The engine deploys with `python scripts/deploy_engine.py --apply` after the operator approves. AgentControl's changes take effect when the dashboard restarts.
4. One live check: a gpt-6.1-sol xhigh one-shot should show reasoning or answer text on the dashboard before it finishes.

# Reply limits and live one-shot thinking: implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. In this repo, tasks are dispatched one per agent on the OpenWebUI harness (`~/.claude/bin/delegate.py --worktree`), never through built-in subagent tools; see "Execution" at the end.

**Goal:** The engine responds to each kind of failed reply instead of resending it, and one-shot calls stream, so the dashboard shows their reasoning and answer while they run.

**Architecture:**
- **Engine.** All engine changes stay in the single file `engine/delegate.py`:
  - a failure classifier, and cause-specific responses in `call_model` and the agent loop;
  - a streaming path for one-shots: GPT through `/openai/responses`, Claude through chat streaming;
  - a Server-Sent Events reader and a live transcript writer for that path;
  - a fallback to today's non-streaming path when a stream fails before its first event.
- **AgentControl.** Its transcript parsers learn the new status lines and the live (still-open) thinking and final-report sections, and the panel marks text that is still being written.

**Tech stack:** Python 3.13 standard library (`urllib`, `json`, `re`) and pytest; vanilla JS ES modules with `node:test` (Node 22).

**Specs:**
- `docs/superpowers/specs/2026-10-02-reply-limits-design.md`, cited below as RL;
- `docs/superpowers/specs/2026-10-02-live-oneshot-thinking-design.md`, cited below as LT.

## Global constraints

- **Test isolation:** every test injects temporary paths and never writes live user accounting state (AGENTS.md).
- **No content outside transcripts:** never place prompts, assistant content, tool results, credentials, response bodies or complete sidecars in source, fixtures, logs, reports, AgentControl state or APIs. The one exception is `GET /api/narration`. Test fixtures use synthetic text only (AGENTS.md).
- **Deploying:** do not deploy the shared engine without explicit operator approval. Only Task 9 deploys, and only after that approval.
- **Engine file:**
  - the engine stays one file, standard library only;
  - tests and every repo's `tools/agent-harness/selftest_policy.py` load it by path, so it has no sibling imports;
  - do not change `HARNESS_VERSION`.
- **Unchanged:** `REQUEST_TIMEOUT = 600` and `CMD_TIMEOUT = 600`. Agent runs (non-one-shot) keep non-streaming requests (RL §4).
- **Constants, exact:** `REPLY_CUT_SECONDS = 100`, `CUT_CORRECTIONS_PER_TURN = 2`, `STREAM_IDLE_TIMEOUT = 300`, `LIVE_FLUSH_SECONDS = 1.0`.
- **Engine strings, exact:**
  - `CUT_PLACEHOLDER = "[reply discarded by the gateway's 2-minute limit]"`
  - `CUT_CORRECTION = "Your last reply took longer than the gateway's 2-minute limit, so it was discarded and nothing ran. Do the same work in a smaller step: call one tool, and keep any write_file or replace_in_file under about 120 lines. Write a big file in parts."`
  - `CLAUDE_REPLY_RULE = "\n\nEach reply must arrive within about 2 minutes or the gateway discards it: call one tool per reply, and keep each write_file or replace_in_file under about 120 lines, writing large files in parts."`
  - `ONESHOT_SHORTER = "\n\nAnswer in under about 1,500 words; the gateway discards replies that take longer than 2 minutes."`
  - `ONESHOT_CUT_MARK = "[cut off by the gateway's 2-minute limit]"`
- **Transcript status lines, exact:** each is written with `tr.write(f"\n_{note}_")`, with the turn as `{turn:>2}`:
  - `[ 8] reply cut by the gateway's 2-minute limit after 121 s; asking for a smaller step`
  - `[ 1] reply cut by the gateway's 2-minute limit after 121 s; asking for a shorter answer` (one-shots)
  - `[24] reply had no tool call; asking for one`
  - `[ 1] no reply within 600 s at max; retrying at xhigh` (the seconds are `REQUEST_TIMEOUT`, or `STREAM_IDLE_TIMEOUT` for streams)
- **Heartbeat lines** keep the format `_[ n] calling <model> (attempt a/b, <m> msgs, ~<bytes> bytes) ..._`, because `progress.py` and `narration.py` parse it.
- **Narration event contract** (shared by Tasks 6 and 7):
  - a new kind `"status"`, with `iteration` and `text`, where `text` is the status line without the underscores and the `[ n] ` prefix;
  - `"narration"` events gain `live` (a bool);
  - `"final"` events gain `live` (a bool).
- **Where RL leaves a gap:** RL lists five failure causes but says nothing about prompt mode. A prompt-mode reply that has text but no tool block gets the cause `no_tool_block` and keeps today's retries.
- **Test commands:**
  - `python -m pytest -q` (the whole suite);
  - `node --test tests/js/dashboard.test.mjs tests/js/panel.test.mjs`. Node 22 needs explicit file paths here; a directory argument fails.

## Review focus

These are the five failure modes most likely to bite that the specs' tests don't cover. Each has a test in the task that owns the code.

1. **A streamed one-shot killed mid-answer** leaves a final report with no footer. Once the run has ended, the panel must not show "(writing…)", and `progress.py` must not report "finished". Tests are in Task 7 (panel) and Task 5 (progress).
2. **A one-shot footer without token counts** (`[m e=x tools=oneshot: 0 tool calls]`) must still read as finished, now that `## Final report` alone no longer does. Test in Task 5.
3. **A `<think>` tag split across stream chunks** (`"<thi"` + `"nk>"`) must not leak tag fragments into the answer. Test in Task 4.
4. **Malformed or undecodable SSE lines** must be skipped without crashing the run. Test in Task 3.
5. **A GPT stream that ends without `response.completed`** (a dropped connection) must fail the run loudly, keeping the received text in the transcript, rather than report a truncated answer as complete. Test in Task 3.

---

## Task 0: Do streamed GPT summaries arrive? (measurement, no repo change)

Done by the orchestrator, not an agent. It is three gateway requests and changes no repo file. LT §4.

- [ ] **Step 1: Run the check.** Copy `C:\Users\operator\AppData\Local\Temp\claude\d--repo-AgentControl\13c6e9b2-f3ad-4d4b-8c2f-bb030f581b0e\scratchpad\gpt_summary_stream.py`. In the copy:
  - **Model:** `gpt-6.1-sol`, effort `high`.
  - **Prompt:** one that needs long reasoning: "How many ways can 10 non-attacking rooks be placed on a 10x10 board so that none is on the main diagonal? Give the number and one sentence of justification."
  - **Body variants, one request each:**
    - (A) `"reasoning": {"effort": "high", "summary": "auto"}`;
    - (B) the same with `"summary": "detailed"`;
    - (C) (A) plus `"store": false` and `"include": ["reasoning.encrypted_content"]`.

  The script prints only event types, counts, character counts and timings.
- [ ] **Step 2: Record the result** in LT §4 as one sentence: which variant, if any, streamed `response.reasoning_summary_text.delta` events, and how many seconds before the first answer text they arrived.
- [ ] **Step 3: If only (B) or (C) streams summaries,** add the same fields to the payload in Task 3 Step 3 (`stream_gpt_oneshot`) before dispatching Task 3, and change the expected payload in Task 3's tests to match.

---

## Task 1: Classify failed replies; stop resending cut or prose replies

**Files:**
- Modify: `engine/delegate.py`:
  - constants next to `REQUEST_TIMEOUT` (around line 490);
  - `advances()` (around line 1186), `log_anomaly()` (around line 1215) and `call_model()` (around line 1253);
  - the agent loop in `main()` (around lines 1663–1719).
- Modify: `tests/test_engine_reasoning.py` (the `harness` fixture, around lines 70–103).
- Create: `tests/test_engine_reply_limits.py`.

**Interfaces:**
- Consumes: nothing new.
- Produces:
  - `_now() -> float`, the monotonic clock that tests replace;
  - `BARE_ERROR`, a compiled regex;
  - `REPLY_CUT_SECONDS`, `CUT_CORRECTIONS_PER_TURN`, `CUT_PLACEHOLDER`, `CUT_CORRECTION`;
  - `classify_failure(resp, mode, elapsed) -> str`, one of `"reply_cut"`, `"prose_only"`, `"no_tool_block"`, `"timeout"`, `"transport"`, `"empty"`;
  - `call_model(url, key, payload, mode, tr, anomaly_path, turn) -> (resp, ok, cause, elapsed)`, where `cause` is None when `ok` and `elapsed` is the last attempt's wall time in seconds;
  - `log_anomaly(..., cause=None, elapsed=None)`, whose records gain `"elapsed_s"`;
  - a harness `run(..., durations=None)`, which advances the fake clock by `durations[i]` on the i-th chat call.

- [ ] **Step 1: Give the test harness a fake clock.** In `tests/test_engine_reasoning.py`, inside the `harness` fixture:
  - add, after the `time.sleep` patch:
    ```python
    clock = {"t": 1000.0}
    monkeypatch.setattr(engine, "_now", lambda: clock["t"])
    ```
  - replace `run` with:

```python
        def run(self, *argv, replies, durations=None):
            replies = list(replies)
            durations = list(durations or [])

            def fake_chat(url, key, payload):
                self.payloads.append(json.loads(json.dumps(payload)))
                if durations:
                    clock["t"] += durations.pop(0)
                return replies.pop(0)

            monkeypatch.setattr(engine, "chat", fake_chat)
            monkeypatch.setattr(sys, "argv", [
                "delegate.py", "-t", "do the thing", "-C", str(tmp_path),
                "--transcript", str(transcript), *argv])
            engine.main()
            return transcript.read_text(encoding="utf-8")
```

- [ ] **Step 2: Write the failing tests** in `tests/test_engine_reply_limits.py`:

```python
"""Reply-limit handling: cut replies and prose-only replies (reply-limits design).

Synthetic replies only; the harness fakes the gateway and the clock.
"""
from __future__ import annotations

import json

import pytest

from test_engine_reasoning import _finish_reply, _list_reply, engine, harness  # noqa: F401

CLAUDE = "anthropic_tool_call.claude-x"


def _content_reply(content):
    return {"choices": [{"message": {"role": "assistant", "content": content},
                         "finish_reason": "stop"}]}


def _anomalies(harness):
    path = harness.transcript_path.with_suffix(".anomalies.jsonl")
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()]


@pytest.mark.parametrize("resp, mode, elapsed, cause", [
    (_content_reply("Error: "), "native", 121.0, "reply_cut"),
    (_content_reply("Error:"), "oneshot", 100.0, "reply_cut"),
    (_content_reply("Error: "), "native", 5.0, "empty"),
    (_content_reply(""), "native", 130.0, "empty"),
    (_content_reply("I will read the file."), "native", 20.0, "prose_only"),
    (_content_reply("I will read the file."), "prompt", 20.0, "no_tool_block"),
    ({"_transient": "read timeout after 600s"}, "native", 600.0, "timeout"),
    ({"_transient": "HTTP 502"}, "native", 1.0, "transport"),
    ({"_transient": ""}, "native", 1.0, "transport"),
])
def test_classify_failure(engine, resp, mode, elapsed, cause):
    assert engine.classify_failure(resp, mode, elapsed) == cause


def test_cut_reply_gets_one_correction_not_a_resend(engine, harness):
    text = harness.run("-m", CLAUDE,
                       replies=[_content_reply("Error: "), _finish_reply("native")],
                       durations=[121, 3])
    assert len(harness.payloads) == 2
    msgs = harness.payloads[1]["messages"]
    assert msgs[-2] == {"role": "assistant", "content": engine.CUT_PLACEHOLDER}
    assert msgs[-1] == {"role": "user", "content": engine.CUT_CORRECTION}
    assert ("_[ 1] reply cut by the gateway's 2-minute limit after 121 s; "
            "asking for a smaller step_") in text


def test_quick_bare_error_is_resent_unchanged(harness):
    harness.run("-m", CLAUDE,
                replies=[_content_reply("Error: "), _finish_reply("native")],
                durations=[5, 3])
    assert len(harness.payloads) == 2
    assert harness.payloads[0] == harness.payloads[1]


def test_third_consecutive_cut_takes_the_nudge_path(harness):
    text = harness.run("-m", CLAUDE,
                       replies=[_content_reply("Error: ")] * 3 + [_finish_reply("native")],
                       durations=[121, 121, 121, 3])
    assert len(harness.payloads) == 4
    assert text.count("reply cut by the gateway's 2-minute limit") == 2
    assert "NO_TOOL_ACTION" in text


def test_cut_count_resets_after_progress(harness):
    text = harness.run("-m", CLAUDE,
                       replies=[_content_reply("Error: "), _content_reply("Error: "),
                                _list_reply("native"),
                                _content_reply("Error: "), _content_reply("Error: "),
                                _finish_reply("native")],
                       durations=[121, 121, 3, 121, 121, 3])
    assert text.count("reply cut by the gateway's 2-minute limit") == 4
    assert "NO_TOOL_ACTION" not in text


def test_prose_reply_is_kept_and_nudged_without_resend(harness):
    prose = "I will now look at the file."
    text = harness.run("-m", CLAUDE,
                       replies=[_content_reply(prose), _finish_reply("native")],
                       durations=[20, 3])
    assert len(harness.payloads) == 2
    msgs = harness.payloads[1]["messages"]
    assert msgs[-2] == {"role": "assistant", "content": prose}
    assert msgs[-1]["role"] == "user"
    assert msgs[-1]["content"].startswith("You called no tool")
    assert "_[ 1] reply had no tool call; asking for one_" in text


def test_anomaly_records_cause_and_elapsed(harness):
    harness.run("-m", CLAUDE,
                replies=[_content_reply("Error: "), _finish_reply("native")],
                durations=[121.04, 3])
    rec = _anomalies(harness)[0]
    assert rec["cause"] == "reply_cut"
    assert rec["elapsed_s"] == 121.0
```

- [ ] **Step 3: Run the tests to see them fail.**
  - Run: `python -m pytest -q tests/test_engine_reply_limits.py`
  - Expected: FAIL, with `AttributeError` on `_now`, `classify_failure` or `CUT_PLACEHOLDER`.

- [ ] **Step 4: Add the constants, the clock and the classifier** to `engine/delegate.py`. Put the constants right after `REQUEST_TIMEOUT = 600`:

```python
# A bare "Error:" reply that took this long is the gateway's cut of a Claude
# reply at its ~121 s limit (measured 2026-10-02: 401 logged cuts, all >= 116 s;
# no successful Claude reply over 122 s). A quick bare "Error:" is some other
# gateway fault and keeps the ordinary retry.
REPLY_CUT_SECONDS = 100
CUT_CORRECTIONS_PER_TURN = 2
CUT_PLACEHOLDER = "[reply discarded by the gateway's 2-minute limit]"
CUT_CORRECTION = (
    "Your last reply took longer than the gateway's 2-minute limit, so it was "
    "discarded and nothing ran. Do the same work in a smaller step: call one "
    "tool, and keep any write_file or replace_in_file under about 120 lines. "
    "Write a big file in parts.")
BARE_ERROR = re.compile(r"\s*Error:?\s*")


def _now():
    """Monotonic clock for attempt timing; tests replace it."""
    return time.monotonic()
```

  Then put this directly above `def advances(resp, mode):`:

```python
def classify_failure(resp, mode, elapsed):
    """Name why a reply did not advance the loop (see the reply-limits design).

    reply_cut      a bare "Error:" that took REPLY_CUT_SECONDS or longer: the
                   gateway cut a Claude reply at its ~121 s limit
    prose_only     native mode: text but no tool call
    no_tool_block  prompt mode: text but no tool block (keeps today's retries)
    timeout        the client read timeout (REQUEST_TIMEOUT)
    transport      any other transport failure
    empty          anything else: empty content, or a quick bare "Error:"
    """
    if isinstance(resp, dict) and "_transient" in resp:
        detail = str(resp.get("_transient") or "")
        return "timeout" if detail.startswith("read timeout") else "transport"
    choices = resp.get("choices") if isinstance(resp, dict) else None
    msg = ((choices[0] or {}).get("message") or {}) if choices else {}
    content = msg.get("content") or ""
    if BARE_ERROR.fullmatch(content):
        return "reply_cut" if elapsed >= REPLY_CUT_SECONDS else "empty"
    if content.strip() and not msg.get("tool_calls"):
        if mode == "native":
            return "prose_only"
        if mode == "prompt":
            return "no_tool_block"
    return "empty"
```

  In `advances()`, replace `re.fullmatch(r"\s*Error:?\s*", content)` with `BARE_ERROR.fullmatch(content)`.

- [ ] **Step 5: Record the cause and the elapsed time in `log_anomaly`.**
  - Change the signature to `def log_anomaly(anomaly_path, turn, attempt, mode, payload, resp, cause=None, elapsed=None):`.
  - Replace the `"cause": ...` entry of `rec` with `"cause": cause or ("transport" if is_transient else "no_advance"),`.
  - Add `"elapsed_s": round(elapsed, 1) if elapsed is not None else None,` right after `"transient": transient,`.
  - Update the comment above `"cause"`: it now names the `classify_failure()` causes.

- [ ] **Step 6: Return after one attempt for cut and prose replies in `call_model`.** Replace the function body, keeping the existing comments about jitter and the heartbeat:

```python
def call_model(url, key, payload, mode, tr, anomaly_path, turn):
    """Send one turn's request, retrying only where a retry can help.

    Returns (resp, ok, cause, elapsed): cause is None when ok, else the
    classify_failure() name of the last failed attempt; elapsed is that
    attempt's wall time in seconds. "reply_cut" and "prose_only" return after
    ONE attempt: resending the identical request repeats them, so the caller
    responds to the cause instead (reply-limits design).
    """
    backoffs = [2, 8, 20]      # seconds before attempts 2, 3, 4
    resp, cause, elapsed = None, None, 0.0
    for attempt in range(1, 5):
        req_bytes = len(json.dumps(payload).encode())
        n_msgs = len(payload.get("messages") or [])
        hb = (f"[{turn:>2}] calling {payload.get('model')} "
              f"(attempt {attempt}/4, {n_msgs} msgs, ~{req_bytes} bytes) ...")
        if tr is not None:
            tr.write(f"\n_{hb}_")
        print("  " + hb, file=sys.stderr, flush=True)
        started = _now()
        resp = chat_with_model_retry(url, key, payload, turn)
        elapsed = _now() - started
        if advances(resp, mode):
            return resp, True, None, elapsed
        cause = classify_failure(resp, mode, elapsed)
        log_anomaly(anomaly_path, turn, attempt, mode, payload, resp,
                    cause=cause, elapsed=elapsed)
        is_transient = isinstance(resp, dict) and "_transient" in resp
        transient = resp.get("_transient") if isinstance(resp, dict) else None
        reason = (f"transport failure ({transient})" if is_transient
                  else "reply carried no tool action")
        print(f"  [{turn:>2}] anomaly: {reason} "
              f"(attempt {attempt}/4)", file=sys.stderr, flush=True)
        if cause in ("reply_cut", "prose_only"):
            return resp, False, cause, elapsed
        if attempt < 4:
            delay = backoffs[attempt - 1] * random.uniform(0.75, 1.25)
            time.sleep(delay)
    return resp, False, cause, elapsed
```

- [ ] **Step 7: Respond to the cause in the agent loop** in `main()`.
  - Next to `nudges = 0` (around line 1664), add `cuts = 0          # consecutive cut replies, each answered with a correction`.
  - Change the loop's call to `resp, ok, cause, elapsed = call_model(url, key, payload, mode, tr, anomaly_path, i)`.
  - Directly before `if not ok:` (around line 1698), insert:

```python
        if not ok and cause == "reply_cut":
            cuts += 1
            if cuts <= CUT_CORRECTIONS_PER_TURN:
                note = (f"[{i:>2}] reply cut by the gateway's 2-minute limit after "
                        f"{elapsed:.0f} s; asking for a smaller step")
                tr.write(f"\n_{note}_")
                print("  " + note, file=sys.stderr, flush=True)
                add_msg({"role": "assistant", "content": CUT_PLACEHOLDER})
                add_msg({"role": "user", "content": CUT_CORRECTION})
                continue
        if not ok and cause == "prose_only":
            note = f"[{i:>2}] reply had no tool call; asking for one"
            tr.write(f"\n_{note}_")
            print("  " + note, file=sys.stderr, flush=True)
```

  - After the existing `nudges = 0` that follows the `if not ok:` block, add `cuts = 0`.
  - `run_oneshot` also calls `call_model`: change its line to `resp, ok, cause, elapsed = call_model(url, key, payload, "oneshot", tr, anomaly_path, 1)`. Task 2 uses `cause`.

- [ ] **Step 8: Run the tests to see them pass.**
  - Run: `python -m pytest -q tests/test_engine_reply_limits.py tests/test_engine_reasoning.py tests/test_engine_oneshot.py tests/test_engine_contract.py tests/test_engine_model_retry.py`
  - Expected: PASS.
  - If an existing test scripted repeated prose-only replies to exercise retries, update it to the new behaviour, and name it in your report.

- [ ] **Step 9: Run the whole suite.**
  - Run: `python -m pytest -q`
  - Expected: PASS.

- [ ] **Step 10: Commit** (done by the orchestrator after review):

```bash
git add engine/delegate.py tests/test_engine_reasoning.py tests/test_engine_reply_limits.py
git commit -m "feat(engine): classify failed replies; answer a cut or prose reply instead of resending it"
```

---

## Task 2: Timeout fallback, the Claude reply rule, and one-shot cut retry

Runs after Task 1.

**Files:**
- Modify: `engine/delegate.py`:
  - `call_model()`;
  - `BASE_RULES_TEMPLATE` (around line 977), `base_rules()` and `prompt_system()`;
  - `apply_effort` and its neighbours (around line 1346);
  - `ONESHOT_MAX_CHARS` and its neighbours;
  - `run_oneshot()`;
  - the `system = ...` line in `main()`.
- Modify: `tests/test_engine_reply_limits.py` (append).

**Interfaces:**
- Consumes (from Task 1): `call_model(...) -> (resp, ok, cause, elapsed)`, `_now`, the harness's `durations=`, `_content_reply`.
- Produces:
  - `lower_effort_after_timeout(payload) -> dict | None`;
  - `CLAUDE_REPLY_RULE`;
  - `base_rules(model=None)`, `prompt_system(model=None)`;
  - `ONESHOT_SHORTER`.

- [ ] **Step 1: Write the failing tests.** Append to `tests/test_engine_reply_limits.py`:

```python
TIMEOUT = {"_transient": "read timeout after 600s"}


def test_timeout_at_max_retries_once_at_xhigh(harness):
    text = harness.run("-m", "gpt-6.1-sol", "-e", "max",
                       replies=[TIMEOUT, _finish_reply("native")], durations=[600, 5])
    assert len(harness.payloads) == 2
    assert harness.payloads[0]["reasoning"] == {"effort": "max"}
    assert harness.payloads[1]["reasoning"] == {"effort": "xhigh"}
    assert "_[ 1] no reply within 600 s at max; retrying at xhigh_" in text


def test_timeout_at_high_retries_once_unchanged_then_fails_the_turn(harness):
    text = harness.run("-m", "gpt-6-sol", "-e", "high",
                       replies=[TIMEOUT, TIMEOUT, _finish_reply("native")],
                       durations=[600, 600, 5])
    assert len(harness.payloads) == 3
    assert harness.payloads[0] == harness.payloads[1]
    assert "NO_TOOL_ACTION" in text
    assert "retrying at xhigh" not in text


def test_claude_system_prompt_states_the_two_minute_rule(engine, harness):
    harness.run("-m", CLAUDE, replies=[_finish_reply("native")])
    system = harness.payloads[0]["messages"][0]["content"]
    assert system.endswith(engine.CLAUDE_REPLY_RULE)


def test_gpt_system_prompt_has_no_two_minute_rule(engine, harness):
    harness.run("-m", "gpt-6-sol", replies=[_finish_reply("native")])
    system = harness.payloads[0]["messages"][0]["content"]
    assert engine.CLAUDE_REPLY_RULE not in system


def test_oneshot_cut_retries_once_with_shorter_answer_line(engine, harness, capsys):
    # qwen3.6:latest takes the non-streaming one-shot path before and after
    # Tasks 3-4, so this test keeps covering that path.
    text = harness.run("--oneshot", "-m", "qwen3.6:latest",
                       replies=[_content_reply("Error: "), _content_reply("short answer")],
                       durations=[121, 30])
    assert len(harness.payloads) == 2
    assert harness.payloads[1]["messages"][0]["content"] == (
        "do the thing" + engine.ONESHOT_SHORTER)
    assert capsys.readouterr().out == "short answer"
    assert ("_[ 1] reply cut by the gateway's 2-minute limit after 121 s; "
            "asking for a shorter answer_") in text
```

- [ ] **Step 2: Run the tests to see them fail.**
  - Run: `python -m pytest -q tests/test_engine_reply_limits.py`
  - Expected: the five new tests FAIL.

- [ ] **Step 3: Give a timeout one more attempt, at xhigh after max.** Put this directly below `apply_effort`:

```python
def lower_effort_after_timeout(payload):
    """A copy of payload at xhigh when it asked for max, else None.

    On 2026-10-02 gpt-6.1-sol at max went silent past the 600 s client timeout
    on open-ended questions, while xhigh answered in 4 minutes.
    """
    reasoning = payload.get("reasoning")
    if isinstance(reasoning, dict) and reasoning.get("effort") == "max":
        return {**payload, "reasoning": {**reasoning, "effort": "xhigh"}}
    if payload.get("reasoning_effort") == "max":
        return {**payload, "reasoning_effort": "xhigh"}
    return None
```

  In `call_model`, add `timeouts = 0` next to `resp, cause, elapsed = ...`. Directly after the `if cause in ("reply_cut", "prose_only"):` return, insert:

```python
        if cause == "timeout":
            timeouts += 1
            if timeouts >= 2:
                return resp, False, cause, elapsed
            lowered = lower_effort_after_timeout(payload)
            if lowered is not None:
                payload = lowered
                note = (f"[{turn:>2}] no reply within {REQUEST_TIMEOUT} s at max; "
                        "retrying at xhigh")
                if tr is not None:
                    tr.write(f"\n_{note}_")
                print("  " + note, file=sys.stderr, flush=True)
```

- [ ] **Step 4: State the 2-minute rule to Claude agents.** Below `BASE_RULES_TEMPLATE` add:

```python
CLAUDE_REPLY_RULE = (
    "\n\nEach reply must arrive within about 2 minutes or the gateway discards "
    "it: call one tool per reply, and keep each write_file or replace_in_file "
    "under about 120 lines, writing large files in parts.")
```

  Replace `base_rules()` and `prompt_system()` with:

```python
def base_rules(model=None):
    """Rendered per run so the stated read limit is the one actually enforced.

    Telling the model "30,000" while the policy enforces something else teaches it
    to trust a number that will refuse it mid-run. Claude models also get the
    gateway's 2-minute reply rule (reply-limits design).
    """
    rules = BASE_RULES_TEMPLATE.format(limit=f"{MAX_READ_CHARS:,}")
    if model and model.startswith(CLAUDE_PREFIX):
        rules += CLAUDE_REPLY_RULE
    return rules


def prompt_system(model=None):
    lines = [base_rules(model), PROMPT_PROTOCOL]
    for name, args, desc in tool_docs():
        lines.append(f"- `{name}` args={json.dumps(args)}\n    {desc}")
    return "\n".join(lines)
```

  In `main()`, change the line to `system = prompt_system(args.model) if mode == "prompt" else base_rules(args.model)`.

- [ ] **Step 5: Retry a cut one-shot once with a shorter-answer line.** Below `ONESHOT_MAX_CHARS = 600_000` add:

```python
ONESHOT_SHORTER = ("\n\nAnswer in under about 1,500 words; the gateway discards "
                   "replies that take longer than 2 minutes.")
```

  In `run_oneshot`, replace the `try:` block around `call_model` with:

```python
    try:
        resp, ok, cause, elapsed = call_model(url, key, payload, "oneshot", tr,
                                              anomaly_path, 1)
        if not ok and cause == "reply_cut":
            note = (f"[ 1] reply cut by the gateway's 2-minute limit after "
                    f"{elapsed:.0f} s; asking for a shorter answer")
            tr.write(f"\n_{note}_")
            print("  " + note, file=sys.stderr, flush=True)
            shorter = {"role": "user", "content": prompt + ONESHOT_SHORTER}
            journal(shorter)
            payload = {**payload, "messages": [shorter]}
            resp, ok, cause, elapsed = call_model(url, key, payload, "oneshot", tr,
                                                  anomaly_path, 1)
    except SystemExit as exc:
        # chat_with_model_retry / chat stop with a message on a fatal 400, an
        # auth error or exhausted stale-replica retries: record the run failed.
        if exc.code not in (None, 0, 1):
            print(exc.code, file=sys.stderr)
        close_failed("gateway request failed")
```

- [ ] **Step 6: Run the tests to see them pass.**
  - Run: `python -m pytest -q tests/test_engine_reply_limits.py tests/test_engine_oneshot.py tests/test_engine_reasoning.py tests/test_engine_contract.py`
  - Expected: PASS.

- [ ] **Step 7: Run the whole suite.**
  - Run: `python -m pytest -q`
  - Expected: PASS.

- [ ] **Step 8: Commit** (done by the orchestrator):

```bash
git add engine/delegate.py tests/test_engine_reply_limits.py
git commit -m "feat(engine): retry a timeout once at xhigh, state the 2-minute rule to Claude, shorten a cut one-shot"
```

---

## Task 3: Stream GPT one-shots through /openai/responses

Runs after Task 2, and after Task 0 if Task 0 changed the payload.

**Files:**
- Modify: `engine/delegate.py`:
  - imports (add `import http.client`);
  - constants next to `REQUEST_TIMEOUT`;
  - `class Transcript` (around line 947);
  - new code after `chat()` (around line 1131);
  - new code and a rewritten `run_oneshot()` (around line 1365).
- Modify: `tests/test_engine_reasoning.py` (the harness gains a fake stream).
- Modify: `tests/test_engine_oneshot.py` (GPT one-shot tests now stream).
- Create: `tests/test_engine_oneshot_stream.py`.

**Interfaces:**
- Consumes: `_now`, `call_model -> 4-tuple`, `ONESHOT_SHORTER`, `log_anomaly(..., cause=, elapsed=)`, `apply_effort`, `error_detail`, `normalize_usage`.
- Produces:
  - `STREAM_IDLE_TIMEOUT`, `LIVE_FLUSH_SECONDS`, `STREAM_DONE = {"_done": True}`;
  - `class StreamError(Exception)`, with attributes `kind`, `detail` and `events_seen`;
  - `stream_events(url, key, path, payload, idle_timeout=None)`, a generator of dicts;
  - `Transcript.append(text)` and `Transcript.flush()`;
  - `class LiveAnswer(tr)`, with methods `reasoning(text)`, `answer_text(text)` and `close() -> str`;
  - `gpt_stream_event(ev, live, state)`;
  - `log_stream_failure(anomaly_path, attempt, payload, err, elapsed)`;
  - `oneshot_stream_route(model) -> "responses" | None`;
  - `stream_gpt_oneshot(args, url, key, prompt, tr, anomaly_path) -> (answer, usage_resp) | (None, None)`;
  - harness `run(..., stream_replies=None)` and `harness.stream_calls`, a list of `(path, payload)`.

- [ ] **Step 1: Give the harness a fake stream.** In `tests/test_engine_reasoning.py`:
  - add `stream_calls: list = []` to class `H`, and `h.stream_calls = []` next to `h.payloads = []`;
  - extend `run` to take `stream_replies=None`, and before `engine.main()` install:

```python
            stream_scripts = list(stream_replies or [])

            def fake_stream(url, key, path, payload, idle_timeout=None):
                # Each script is a list of: event dicts (yielded), numbers (advance
                # the fake clock), callables (called, e.g. to snapshot the
                # transcript), or an exception (raised). A bare exception instead of
                # a list raises before the first event.
                self.stream_calls.append((path, json.loads(json.dumps(payload))))
                script = stream_scripts.pop(0)
                if isinstance(script, BaseException):
                    raise script
                for item in script:
                    if isinstance(item, BaseException):
                        raise item
                    if callable(item):
                        item()
                    elif isinstance(item, (int, float)) and not isinstance(item, bool):
                        clock["t"] += item
                    else:
                        yield item

            monkeypatch.setattr(engine, "stream_events", fake_stream)
```

- [ ] **Step 2: Write the failing tests** in `tests/test_engine_oneshot_stream.py`:

```python
"""Streaming one-shots (live-thinking design). Synthetic text only; the harness
fakes the stream, the gateway and the clock."""
from __future__ import annotations

import io
import json
import socket
import urllib.error

import pytest

from test_engine_reasoning import RESPONSES_USAGE, engine, harness  # noqa: F401


def _gpt_events(answer, *, summary=None, usage=RESPONSES_USAGE, summary_in_item=None,
                completed=True):
    evs = [{"type": "response.created"}, {"type": "response.in_progress"}]
    if summary is not None:
        evs.append({"type": "response.reasoning_summary_part.added"})
        evs += [{"type": "response.reasoning_summary_text.delta", "delta": p} for p in summary]
        evs.append({"type": "response.reasoning_summary_part.done"})
    item = {"type": "reasoning", "summary": []}
    if summary_in_item is not None:
        item["summary"] = [{"type": "summary_text", "text": summary_in_item}]
    evs.append({"type": "response.output_item.done", "item": item})
    evs += [{"type": "response.output_text.delta", "delta": p} for p in answer]
    if completed:
        evs.append({"type": "response.completed", "response": {"usage": usage}})
    return evs


def _answer(content):
    return {"choices": [{"message": {"role": "assistant", "content": content},
                         "finish_reason": "stop"}]}


def _records(path):
    return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]


def test_gpt_oneshot_streams_through_responses(harness, capsys):
    harness.run("--oneshot", "-m", "gpt-6.1-sol", "-e", "max", replies=[],
                stream_replies=[_gpt_events(["Hello", " world"])])
    assert harness.payloads == []
    path, payload = harness.stream_calls[0]
    assert path == "/openai/responses"
    assert payload == {"model": "gpt-6.1-sol", "input": "do the thing", "stream": True,
                       "reasoning": {"effort": "max", "summary": "auto"}}
    out, err = capsys.readouterr()
    assert out == "Hello world"
    assert err.endswith("\n[sent 100 tok to gpt-6.1-sol; returned 20 tok]\n")


def test_reasoning_reaches_the_transcript_before_the_answer(engine, harness, monkeypatch):
    from agent_control.narration import parse_narration

    monkeypatch.setattr(engine, "LIVE_FLUSH_SECONDS", 0.0)
    seen = []
    script = _gpt_events(["Answer."], summary=["**Plan**\n\nFirst ", "check X."])
    first_answer = next(i for i, e in enumerate(script)
                        if e["type"] == "response.output_text.delta")
    script.insert(first_answer,
                  lambda: seen.append(harness.transcript_path.read_text(encoding="utf-8")))
    text = harness.run("--oneshot", "-m", "gpt-6.1-sol", replies=[], stream_replies=[script])
    assert "<think>\n**Plan**\n\nFirst check X." in seen[0]
    assert "## Final report" not in seen[0]
    events = parse_narration(text).events
    assert [e.kind for e in events if e.kind in ("narration", "final")] == ["narration", "final"]
    assert next(e for e in events if e.kind == "final").data["text"] == "Answer."


def test_summary_from_the_reasoning_item_when_no_deltas(harness):
    text = harness.run("--oneshot", "-m", "gpt-6-sol", replies=[],
                       stream_replies=[_gpt_events(["ok"], summary_in_item="Checked the cases.")])
    assert "<think>\nChecked the cases.\n</think>" in text


def test_no_reasoning_section_without_summary(harness):
    text = harness.run("--oneshot", "-m", "gpt-6-sol", replies=[],
                       stream_replies=[_gpt_events(["ok"])])
    assert "### [1] thinking" not in text
    assert "## Final report\n\nok\n" in text


def test_http_error_before_first_event_falls_back_to_non_streaming(engine, harness, capsys):
    err = engine.StreamError("http", "HTTP 400: unsupported", 0)
    harness.run("--oneshot", "-m", "gpt-6-sol", replies=[_answer("fallback answer")],
                stream_replies=[err])
    assert len(harness.payloads) == 1 and harness.payloads[0]["stream"] is False
    assert capsys.readouterr().out == "fallback answer"


def test_idle_at_max_retries_once_at_xhigh(engine, harness, capsys):
    idle = engine.StreamError("idle", "no event for 300s", 0)
    text = harness.run("--oneshot", "-m", "gpt-6.1-sol", "-e", "max", replies=[],
                       stream_replies=[[{"type": "response.created"}, idle],
                                       _gpt_events(["late answer"])])
    assert [p["reasoning"]["effort"] for _, p in harness.stream_calls] == ["max", "xhigh"]
    assert "_[ 1] no reply within 300 s at max; retrying at xhigh_" in text
    assert capsys.readouterr().out == "late answer"


def test_second_idle_fails_the_run(engine, harness, tmp_path):
    idle = engine.StreamError("idle", "no event for 300s", 1)
    with pytest.raises(SystemExit) as ei:
        harness.run("--oneshot", "-m", "gpt-6-sol", "-e", "high", replies=[],
                    stream_replies=[[idle], [idle]])
    assert ei.value.code == 1
    assert len(harness.stream_calls) == 2
    assert _records(tmp_path / "index.jsonl")[-1]["status"] == "failed"


def test_stream_without_completed_event_fails_and_keeps_the_text(harness, tmp_path):
    # Review focus 5: a dropped connection must not pass as a complete answer.
    with pytest.raises(SystemExit) as ei:
        harness.run("--oneshot", "-m", "gpt-6-sol", replies=[],
                    stream_replies=[_gpt_events(["half an ans"], completed=False)])
    assert ei.value.code == 1
    assert "half an ans" in harness.transcript_path.read_text(encoding="utf-8")
    assert _records(tmp_path / "index.jsonl")[-1]["status"] == "failed"


class _FakeStream:
    def __init__(self, lines):
        self.lines = lines

    def __iter__(self):
        return iter(self.lines)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_stream_events_skips_bad_lines(engine, monkeypatch):
    # Review focus 4: keep-alives, event lines, broken JSON and undecodable bytes.
    lines = [b": keep-alive\n", b"event: response.created\n", b'data: {"type": "a"}\n',
             b"data: {not json\n", b"data: \xff\xfe\n", b"\n", b'data: {"type": "b"}\n',
             b"data: [DONE]\n", b'data: {"type": "after-done"}\n']
    seen = {}

    def fake_urlopen(req, timeout=None):
        seen["timeout"] = timeout
        seen["auth"] = req.get_header("Authorization")
        return _FakeStream(lines)

    monkeypatch.setattr(engine.urllib.request, "urlopen", fake_urlopen)
    got = list(engine.stream_events("http://gw", "k", "/openai/responses", {"stream": True}))
    assert got == [{"type": "a"}, {"type": "b"}, engine.STREAM_DONE]
    assert seen == {"timeout": engine.STREAM_IDLE_TIMEOUT, "auth": "Bearer k"}


def test_stream_events_http_error_is_a_stream_error_without_the_key(engine, monkeypatch):
    def boom(req, timeout=None):
        raise urllib.error.HTTPError("http://gw/x", 400, "bad", None,
                                     io.BytesIO(b'{"detail": "bad key sekret-key"}'))

    monkeypatch.setattr(engine.urllib.request, "urlopen", boom)
    with pytest.raises(engine.StreamError) as ei:
        list(engine.stream_events("http://gw", "sekret-key", "/openai/responses", {}))
    assert ei.value.kind == "http" and ei.value.events_seen == 0
    assert "sekret-key" not in str(ei.value)


def test_stream_events_read_timeout_is_idle(engine, monkeypatch):
    class Slow(_FakeStream):
        def __iter__(self):
            yield b'data: {"type": "a"}\n'
            raise socket.timeout("timed out")

    monkeypatch.setattr(engine.urllib.request, "urlopen", lambda req, timeout=None: Slow([]))
    gen = engine.stream_events("http://gw", "k", "/p", {})
    assert next(gen) == {"type": "a"}
    with pytest.raises(engine.StreamError) as ei:
        next(gen)
    assert ei.value.kind == "idle" and ei.value.events_seen == 1
```

- [ ] **Step 3: Run the tests to see them fail.**
  - Run: `python -m pytest -q tests/test_engine_oneshot_stream.py`
  - Expected: FAIL, with `AttributeError` on `stream_events`, `StreamError` or `STREAM_DONE`.

- [ ] **Step 4: Add the stream reader.**
  - Add `import http.client` to the imports.
  - Next to `REQUEST_TIMEOUT`, add:

```python
# Streaming one-shots (live-thinking design): a stream with no line for this long
# counts as a timeout, and live transcript text is fsynced at most this often.
STREAM_IDLE_TIMEOUT = 300
LIVE_FLUSH_SECONDS = 1.0
```

  Directly after `chat()` add:

```python
STREAM_DONE = {"_done": True}


class StreamError(Exception):
    """A streaming request that failed.

    kind is "http" (the gateway answered with an HTTP error), "idle" (no line
    for STREAM_IDLE_TIMEOUT seconds) or "transport" (any other failure).
    events_seen counts the events received before it, so a caller can tell a
    stream that never started (and may fall back) from one that broke midway.
    """

    def __init__(self, kind, detail, events_seen=0):
        super().__init__(f"{kind}: {detail}")
        self.kind = kind
        self.detail = detail
        self.events_seen = events_seen


def stream_events(url, key, path, payload, idle_timeout=None):
    """POST payload to url + path and yield each Server-Sent Event's JSON data.

    Lines other than "data: ..." are skipped, as are data lines that are not a
    JSON object. "data: [DONE]" yields STREAM_DONE and ends the stream. Raises
    StreamError; an auth failure exits like chat(). The key goes only into the
    Authorization header and is masked out of any error detail.
    """
    timeout = STREAM_IDLE_TIMEOUT if idle_timeout is None else idle_timeout
    req = urllib.request.Request(url + path, data=json.dumps(payload).encode(),
                                 method="POST")
    req.add_header("Authorization", "Bearer " + key)
    req.add_header("Content-Type", "application/json")
    req.add_header("Accept", "text/event-stream")
    seen = 0
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            for raw in r:
                line = raw.decode("utf-8", errors="replace").strip()
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    yield STREAM_DONE
                    return
                try:
                    obj = json.loads(data)
                except ValueError:
                    continue
                if isinstance(obj, dict):
                    seen += 1
                    yield obj
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            sys.exit(f"error: HTTP {e.code}\n{e.read()[:600].decode(errors='replace')}")
        body = e.read()[:8000].decode(errors="replace")
        raise StreamError("http", f"HTTP {e.code}: {error_detail(body, key)}", seen)
    except (TimeoutError, socket.timeout):
        raise StreamError("idle", f"no event for {timeout}s", seen)
    except urllib.error.URLError as e:
        raise StreamError("transport", f"cannot reach gateway: {e.reason}", seen)
    except (OSError, http.client.HTTPException) as e:
        raise StreamError("transport", type(e).__name__, seen)
```

  Check that `error_detail(body, key)` masks the key. `test_stream_events_http_error_is_a_stream_error_without_the_key` proves it.

- [ ] **Step 5: Teach the transcript to grow while streaming.** In `class Transcript`:
  - in `__init__`, set `self._last_flush = float("-inf")` before `self.write(header)`;
  - replace `write` with the version below, and add `append` and `flush`:

```python
    def write(self, text):
        self.fh.write(text + "\n")
        self.flush()

    def append(self, text):
        """Write streamed text with no newline; fsync at most every
        LIVE_FLUSH_SECONDS so a fast stream does not fsync per token."""
        self.fh.write(text)
        if _now() - self._last_flush >= LIVE_FLUSH_SECONDS:
            self.flush()

    def flush(self):
        self.fh.flush()
        os.fsync(self.fh.fileno())
        self._last_flush = _now()
```

  Directly after the class add:

```python
class LiveAnswer:
    """Writes a streamed one-shot into the transcript as it arrives.

    Reasoning goes into a "### [1] thinking" section inside <think>...</think>,
    which the narration view shows as reasoning; the answer goes under
    "## Final report". Each stays open while text arrives, so the dashboard
    shows it live; close() ends them and returns the answer.
    """

    def __init__(self, tr):
        self.tr = tr
        self.state = "idle"          # idle -> reasoning -> answer -> closed
        self.parts = []

    def reasoning(self, text):
        if not text or self.state not in ("idle", "reasoning"):
            return
        if self.state == "idle":
            self.tr.write("\n### [1] thinking\n\n<think>")
            self.state = "reasoning"
        self.tr.append(text)

    def answer_text(self, text):
        if not text or self.state == "closed":
            return
        if self.state == "reasoning":
            self.tr.write("\n</think>")
        if self.state != "answer":
            self.tr.write("\n---\n\n## Final report\n")
            self.state = "answer"
        self.tr.append(text)
        self.parts.append(text)

    def close(self):
        if self.state == "reasoning":
            self.tr.write("\n</think>")
        elif self.state == "answer":
            self.tr.write("")        # end the answer's last line
        self.tr.flush()
        self.state = "closed"
        return "".join(self.parts)
```

- [ ] **Step 6: Add the GPT streaming path.** Put this directly above `run_oneshot`:

```python
def oneshot_stream_route(model):
    """The streaming route a one-shot takes, or None for the non-streaming path."""
    if model.startswith("gpt-"):
        return "responses"
    return None


def log_stream_failure(anomaly_path, attempt, payload, err, elapsed):
    cause = "timeout" if err.kind == "idle" else "transport"
    log_anomaly(anomaly_path, 1, attempt, "oneshot", payload,
                {"_transient": f"stream {err.kind}: {err.detail}"},
                cause=cause, elapsed=elapsed)


def gpt_stream_event(ev, live, state):
    """Apply one Responses API stream event to the live transcript."""
    kind = ev.get("type") or ""
    if kind == "response.reasoning_summary_text.delta":
        state["summary_deltas"] = True
        live.reasoning(ev.get("delta") or "")
    elif kind == "response.reasoning_summary_part.done" and state["summary_deltas"]:
        live.reasoning("\n\n")
    elif kind == "response.output_item.done":
        item = ev.get("item") or {}
        if item.get("type") == "reasoning" and not state["summary_deltas"]:
            texts = [s.get("text") for s in item.get("summary") or []
                     if isinstance(s, dict) and s.get("text")]
            live.reasoning("\n\n".join(texts))
    elif kind == "response.output_text.delta":
        live.answer_text(ev.get("delta") or "")
    elif kind == "response.completed":
        state["completed"] = True
        state["usage"] = (ev.get("response") or {}).get("usage")
    elif kind in ("response.failed", "error"):
        raise StreamError("transport", "the gateway reported a failed response", 1)


def stream_gpt_oneshot(args, url, key, prompt, tr, anomaly_path):
    """Stream a GPT one-shot through /openai/responses into the transcript.

    Returns (answer, usage_resp), or (None, None) when the stream failed before
    its first event, so the caller falls back to the non-streaming path. A
    stream silent for STREAM_IDLE_TIMEOUT gets one more attempt, at xhigh when
    it was max (reply-limits design). Raises StreamError otherwise.
    """
    effort = args.effort
    for attempt in (1, 2):
        payload = apply_effort({"model": args.model, "input": prompt, "stream": True},
                               args.model, effort)
        payload.setdefault("reasoning", {})["summary"] = "auto"
        if args.temperature is not None:
            payload["temperature"] = args.temperature
        hb = (f"[ 1] calling {args.model} (attempt {attempt}/2, 1 msgs, "
              f"~{len(json.dumps(payload).encode())} bytes) ...")
        tr.write(f"\n_{hb}_")
        print("  " + hb, file=sys.stderr, flush=True)
        live = LiveAnswer(tr)
        state = {"summary_deltas": False, "completed": False, "usage": None}
        started = _now()
        try:
            for ev in stream_events(url, key, "/openai/responses", payload):
                gpt_stream_event(ev, live, state)
            if not state["completed"]:
                raise StreamError("transport",
                                  "the stream ended before the response completed", 1)
        except StreamError as exc:
            live.close()
            log_stream_failure(anomaly_path, attempt, payload, exc, _now() - started)
            if exc.events_seen == 0 and exc.kind in ("http", "transport"):
                return None, None
            if exc.kind == "idle" and attempt == 1:
                if effort == "max":
                    effort = "xhigh"
                    note = (f"[ 1] no reply within {STREAM_IDLE_TIMEOUT} s at max; "
                            "retrying at xhigh")
                    tr.write(f"\n_{note}_")
                    print("  " + note, file=sys.stderr, flush=True)
                continue
            raise
        answer = live.close()
        if not answer.strip():
            raise StreamError("transport", "the stream ended without an answer", 1)
        return answer, {"usage": state["usage"]}
    raise StreamError("idle", f"no event for {STREAM_IDLE_TIMEOUT}s", 1)
```

- [ ] **Step 7: Rewrite `run_oneshot`** to stream first and fall back. Replace the whole function. This version already includes Task 2's cut retry:

```python
def run_oneshot(args, url, key, prompt, tr, tpath, anomaly_path, lifecycle,
                journal, journal_usage):
    """Send one tool-less request and record it like a run.

    GPT (and, from the next change, Claude) one-shots stream, so the transcript
    and the dashboard's narration show reasoning and answer text as they arrive;
    a stream that fails before its first event falls back to the non-streaming
    path, which other models use directly. Registration (start record,
    transcript header) is done by main(); this writes the answer, the usage
    row, the footer and the end record.
    """
    msg = {"role": "user", "content": prompt}
    journal(msg)

    def close_failed(reason):
        tail = f"[{args.model} e={args.effort} tools=oneshot: 0 tool calls]"
        tr.write("\n" + tail)
        tr.close()
        lifecycle.finish("failed", tool_calls=0, refused=0, sent=0, recv=0)
        print(f"error: {reason}. Transcript: {tpath}", file=sys.stderr)
        sys.exit(1)

    call_started = datetime.datetime.now(datetime.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ")
    content = usage_resp = None
    route = oneshot_stream_route(args.model)
    if route is not None:
        try:
            if route == "responses":
                content, usage_resp = stream_gpt_oneshot(args, url, key, prompt, tr,
                                                         anomaly_path)
        except StreamError as exc:
            journal_usage(1, "loop", {}, started=call_started)
            close_failed(f"streaming request failed ({exc.kind}: {exc.detail})")
        except SystemExit as exc:
            if exc.code not in (None, 0, 1):
                print(exc.code, file=sys.stderr)
            close_failed("gateway request failed")

    if content is None:
        payload = apply_effort({"model": args.model, "messages": [msg], "stream": False},
                               args.model, args.effort)
        if args.temperature is not None:
            payload["temperature"] = args.temperature
        try:
            resp, ok, cause, elapsed = call_model(url, key, payload, "oneshot", tr,
                                                  anomaly_path, 1)
            if not ok and cause == "reply_cut":
                note = (f"[ 1] reply cut by the gateway's 2-minute limit after "
                        f"{elapsed:.0f} s; asking for a shorter answer")
                tr.write(f"\n_{note}_")
                print("  " + note, file=sys.stderr, flush=True)
                shorter = {"role": "user", "content": prompt + ONESHOT_SHORTER}
                journal(shorter)
                payload = {**payload, "messages": [shorter]}
                resp, ok, cause, elapsed = call_model(url, key, payload, "oneshot", tr,
                                                      anomaly_path, 1)
        except SystemExit as exc:
            # chat_with_model_retry / chat stop with a message on a fatal 400, an
            # auth error or exhausted stale-replica retries: record the run failed.
            if exc.code not in (None, 0, 1):
                print(exc.code, file=sys.stderr)
            close_failed("gateway request failed")
        journal_usage(1, "loop", resp, started=call_started)
        if not ok:
            close_failed("model returned no usable answer -- likely a context-limit "
                         "or gateway failure")
        content = resp["choices"][0]["message"].get("content") or ""
        usage_resp = resp
        tr.write("\n---\n\n## Final report\n\n" + content)
    else:
        journal_usage(1, "loop", usage_resp, started=call_started)

    journal({"role": "assistant", "content": content})
    n = normalize_usage(usage_resp.get("usage"))

    def count(v):
        return v if isinstance(v, int) and not isinstance(v, bool) and v > 0 else 0

    sent, recv = count(n["sent"]), count(n["recv"])
    tail = (f"[{args.model} e={args.effort} tools=oneshot: 0 tool calls"
            f"{f', {sent:,} tok sent / {recv:,} tok returned' if sent or recv else ''}]")
    tr.write("\n" + tail)
    tr.close()
    lifecycle.finish("complete", tool_calls=0, refused=0, sent=sent, recv=recv)

    sys.stdout.write(content)
    sys.stdout.flush()
    shown = ["?" if v is None else v for v in (n["sent"], n["recv"])]
    print(f"\n[sent {shown[0]} tok to {args.model}; returned {shown[1]} tok]",
          file=sys.stderr, flush=True)
```

- [ ] **Step 8: Move the GPT one-shot tests in `tests/test_engine_oneshot.py` onto the stream.** Add a helper at the top:

```python
def _stream(answer, usage=None):
    evs = [{"type": "response.output_text.delta", "delta": answer}]
    evs.append({"type": "response.completed", "response": {"usage": usage}})
    return [evs]
```

  Then change these tests:
  - **`test_gpt_payload_has_no_tools_and_nested_effort`:** rename it `test_non_streaming_payload_has_no_tools_and_nested_effort`. Use `-m qwen3.6:latest -e max` and expect `{"model": "qwen3.6:latest", "messages": [{"role": "user", "content": "do the thing"}], "stream": False, "reasoning": {"effort": "max"}}`.
  - **`test_effort_substitution_applies`:** pass `replies=[], stream_replies=_stream("hi")`. Assert `harness.stream_calls[0][1]["reasoning"] == {"effort": "low", "summary": "auto"}` and `"temperature" not in harness.stream_calls[0][1]`.
  - **`test_attachments_appended_in_order`:** pass `replies=[], stream_replies=_stream("ok")`. Assert on `harness.stream_calls[0][1]["input"]` instead of `harness.payloads[0]["messages"][0]["content"]`.
  - **`test_stdout_is_exact_content_and_token_line`:** pass `replies=[], stream_replies=_stream(answer, RESPONSES_USAGE)`.
  - **`test_token_line_unknown_usage`:** pass `replies=[], stream_replies=_stream("x")`.
  - **`test_registered_like_a_normal_run`:** pass `replies=[], stream_replies=_stream(answer, RESPONSES_USAGE)`. Every assertion stays the same.
  - **`test_gateway_400_fails_and_records_end`, `test_transient_error_is_retried`, `test_model_not_found_is_retried`, `test_tool_mode_auto_is_accepted`:** change `-m gpt-6.1-sol` to `-m qwen3.6:latest`. These cover the non-streaming path.

- [ ] **Step 9: Run the tests to see them pass.**
  - Run: `python -m pytest -q tests/test_engine_oneshot_stream.py tests/test_engine_oneshot.py tests/test_engine_reply_limits.py tests/test_engine_reasoning.py tests/test_engine_contract.py`
  - Expected: PASS.

- [ ] **Step 10: Run the whole suite.**
  - Run: `python -m pytest -q`
  - Expected: PASS.

- [ ] **Step 11: Commit** (done by the orchestrator):

```bash
git add engine/delegate.py tests/test_engine_reasoning.py tests/test_engine_oneshot.py tests/test_engine_oneshot_stream.py
git commit -m "feat(engine): stream GPT one-shots through /openai/responses with live reasoning and answer"
```

---

## Task 4: Stream Claude one-shots through chat streaming

Runs after Task 3.

**Files:**
- Modify: `engine/delegate.py` (`oneshot_stream_route`, new code next to `stream_gpt_oneshot`, and `run_oneshot`'s route dispatch).
- Modify: `tests/test_engine_oneshot.py` (`test_claude_payload_flat_effort_and_temperature`).
- Modify: `tests/test_engine_oneshot_stream.py` (append).

**Interfaces:**
- Consumes (from Task 3): `stream_events`, `STREAM_DONE`, `StreamError`, `LiveAnswer`, `log_stream_failure`, `log_anomaly`, `REPLY_CUT_SECONDS`, `ONESHOT_SHORTER`, and the harness's `stream_replies=` and `stream_calls`.
- Produces:
  - `class ThinkSplitter` with `feed(text)` and `flush()`, each returning a list of `(kind, text)` where kind is `"reasoning"` or `"answer"`;
  - `ONESHOT_CUT_MARK`;
  - `stream_chat_oneshot(args, url, key, prompt, tr, anomaly_path) -> (answer, usage_resp) | (None, None)`;
  - `oneshot_stream_route` returns `"chat"` for `anthropic_tool_call.*`.

- [ ] **Step 1: Write the failing tests.** Append to `tests/test_engine_oneshot_stream.py`:

```python
CLAUDE = "anthropic_tool_call.claude-x"


def _chat_events(pieces, *, usage=None, done=True, clock=None):
    evs = [{"object": "chat.completion.chunk",
            "choices": [{"index": 0, "delta": {"content": p}}]} for p in pieces]
    last = {"object": "chat.completion.chunk",
            "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}
    if usage is not None:
        last["usage"] = usage
    evs.append(last)
    if clock is not None:
        evs.append(clock)                 # a number advances the fake clock
    if done:
        evs.append({"_done": True})
    return evs


def test_claude_oneshot_streams_through_chat(harness, capsys):
    harness.run("--oneshot", "-m", CLAUDE, "-e", "high", replies=[],
                stream_replies=[_chat_events(["The ", "answer."], clock=8)])
    path, payload = harness.stream_calls[0]
    assert path == "/api/chat/completions"
    assert payload == {"model": CLAUDE, "stream": True, "reasoning_effort": "high",
                       "messages": [{"role": "user", "content": "do the thing"}]}
    assert capsys.readouterr().out == "The answer."


def test_claude_stream_cut_retries_once_with_shorter_answer(engine, harness, capsys):
    text = harness.run("--oneshot", "-m", CLAUDE, replies=[],
                       stream_replies=[_chat_events(["long ans"], clock=121),
                                       _chat_events(["short answer"], clock=20)])
    assert len(harness.stream_calls) == 2
    assert harness.stream_calls[1][1]["messages"][0]["content"] == (
        "do the thing" + engine.ONESHOT_SHORTER)
    assert ("_[ 1] reply cut by the gateway's 2-minute limit after 121 s; "
            "asking for a shorter answer_") in text
    assert "long ans\n\n" + engine.ONESHOT_CUT_MARK in text
    assert capsys.readouterr().out == "short answer"


def test_second_cut_keeps_the_answer_marked(engine, harness, capsys, tmp_path):
    harness.run("--oneshot", "-m", CLAUDE, replies=[],
                stream_replies=[_chat_events(["first"], clock=121),
                                _chat_events(["second"], clock=121)])
    assert capsys.readouterr().out == "second\n\n" + engine.ONESHOT_CUT_MARK
    assert _records(tmp_path / "index.jsonl")[-1]["status"] == "complete"


def test_claude_stream_without_done_fails(harness, tmp_path):
    with pytest.raises(SystemExit) as ei:
        harness.run("--oneshot", "-m", CLAUDE, replies=[],
                    stream_replies=[_chat_events(["partial"], done=False)])
    assert ei.value.code == 1
    assert _records(tmp_path / "index.jsonl")[-1]["status"] == "failed"


def test_claude_http_error_falls_back_to_non_streaming(engine, harness, capsys):
    harness.run("--oneshot", "-m", CLAUDE, replies=[_answer("plain")],
                stream_replies=[engine.StreamError("http", "HTTP 400", 0)])
    assert harness.payloads[0]["stream"] is False
    assert capsys.readouterr().out == "plain"


def test_claude_think_tags_become_reasoning(harness):
    text = harness.run("--oneshot", "-m", CLAUDE, replies=[],
                       stream_replies=[_chat_events(["<think>pla", "n</think>The answer."])])
    assert "<think>\nplan\n</think>" in text
    assert "## Final report\n\nThe answer.\n" in text


def _merged(pieces):
    out = []
    for kind, text in pieces:
        if out and out[-1][0] == kind:
            out[-1] = (kind, out[-1][1] + text)
        else:
            out.append((kind, text))
    return out


@pytest.mark.parametrize("chunks, expected", [
    (["plain answer"], [("answer", "plain answer")]),
    (["<think>plan</think>answer"], [("reasoning", "plan"), ("answer", "answer")]),
    # Review focus 3: tags split across chunks never leak into the answer.
    (["<thi", "nk>plan</th", "ink>answer"], [("reasoning", "plan"), ("answer", "answer")]),
    (["a <", "b"], [("answer", "a <b")]),
    (["<think>still thinking"], [("reasoning", "still thinking")]),
])
def test_think_splitter(engine, chunks, expected):
    splitter = engine.ThinkSplitter()
    pieces = []
    for chunk in chunks:
        pieces += splitter.feed(chunk)
    pieces += splitter.flush()
    assert _merged(pieces) == expected
```

- [ ] **Step 2: Run the tests to see them fail.**
  - Run: `python -m pytest -q tests/test_engine_oneshot_stream.py`
  - Expected: the new tests FAIL. Claude one-shots don't stream yet, so `stream_calls` is empty, and `ThinkSplitter` is missing.

- [ ] **Step 3: Add the splitter and the Claude streaming path.** Put this directly below `stream_gpt_oneshot`:

```python
ONESHOT_CUT_MARK = "[cut off by the gateway's 2-minute limit]"


class ThinkSplitter:
    """Split streamed text into reasoning (inside <think>...</think>) and answer.

    A tag can be split across chunks, so a possible partial tag at the end of
    a chunk is held back until the next one decides it.
    """

    OPEN, CLOSE = "<think>", "</think>"

    def __init__(self):
        self.inside = False
        self.carry = ""

    def _kind(self):
        return "reasoning" if self.inside else "answer"

    def feed(self, text):
        buf, out = self.carry + text, []
        self.carry = ""
        while buf:
            tag = self.CLOSE if self.inside else self.OPEN
            i = buf.find(tag)
            if i == -1:
                keep = max((k for k in range(1, len(tag)) if buf.endswith(tag[:k])),
                           default=0)
                piece = buf[:-keep] if keep else buf
                self.carry = buf[-keep:] if keep else ""
                if piece:
                    out.append((self._kind(), piece))
                break
            if i:
                out.append((self._kind(), buf[:i]))
            buf = buf[i + len(tag):]
            self.inside = not self.inside
        return out

    def flush(self):
        rest, self.carry = self.carry, ""
        return [(self._kind(), rest)] if rest else []


def _feed_live(live, pieces):
    for kind, text in pieces:
        if kind == "reasoning":
            live.reasoning(text)
        else:
            live.answer_text(text)


def stream_chat_oneshot(args, url, key, prompt, tr, anomaly_path):
    """Stream a Claude one-shot through chat completions into the transcript.

    Returns (answer, usage_resp), or (None, None) when the stream failed before
    its first event, so the caller falls back to the non-streaming path. The
    gateway ends a cut stream normally (2026-10-02 probes), so elapsed time is
    the only signal: a stream that ran REPLY_CUT_SECONDS or longer is retried
    once with ONESHOT_SHORTER, and a second cut keeps that answer marked with
    ONESHOT_CUT_MARK. A stream silent for STREAM_IDLE_TIMEOUT is not retried:
    the gateway ends every Claude stream by about 121 s, so a silent one is
    broken. Raises StreamError otherwise.
    """
    text = prompt
    for attempt in (1, 2):
        payload = apply_effort({"model": args.model,
                                "messages": [{"role": "user", "content": text}],
                                "stream": True}, args.model, args.effort)
        if args.temperature is not None:
            payload["temperature"] = args.temperature
        hb = (f"[ 1] calling {args.model} (attempt {attempt}/2, 1 msgs, "
              f"~{len(json.dumps(payload).encode())} bytes) ...")
        tr.write(f"\n_{hb}_")
        print("  " + hb, file=sys.stderr, flush=True)
        live, split = LiveAnswer(tr), ThinkSplitter()
        usage, done = None, False
        started = _now()
        try:
            for ev in stream_events(url, key, "/api/chat/completions", payload):
                if ev.get("_done"):
                    done = True
                    continue
                for choice in (ev.get("choices") or [])[:1]:
                    piece = (choice.get("delta") or {}).get("content")
                    if isinstance(piece, str):
                        _feed_live(live, split.feed(piece))
                if isinstance(ev.get("usage"), dict):
                    usage = ev["usage"]
            _feed_live(live, split.flush())
            if not done:
                raise StreamError("transport", "the stream ended before [DONE]", 1)
        except StreamError as exc:
            live.close()
            log_stream_failure(anomaly_path, attempt, payload, exc, _now() - started)
            if exc.events_seen == 0 and exc.kind in ("http", "transport"):
                return None, None
            raise
        elapsed = _now() - started
        answer = live.close()
        if elapsed < REPLY_CUT_SECONDS:
            if not answer.strip():
                raise StreamError("transport", "the stream ended without an answer", 1)
            return answer, {"usage": usage}
        log_anomaly(anomaly_path, 1, attempt, "oneshot", payload,
                    {"choices": [{"message": {"content": ""}, "finish_reason": "stop"}]},
                    cause="reply_cut", elapsed=elapsed)
        tr.write("\n" + ONESHOT_CUT_MARK)
        if attempt == 2:
            return answer + "\n\n" + ONESHOT_CUT_MARK, {"usage": usage}
        note = (f"[ 1] reply cut by the gateway's 2-minute limit after {elapsed:.0f} s; "
                "asking for a shorter answer")
        tr.write(f"\n_{note}_")
        print("  " + note, file=sys.stderr, flush=True)
        text = prompt + ONESHOT_SHORTER
    raise StreamError("transport", "the stream ended without an answer", 1)
```

  - In `oneshot_stream_route`, add `if model.startswith(CLAUDE_PREFIX): return "chat"` before `return None`.
  - In `run_oneshot`, below the `if route == "responses":` branch inside the `try:`, add:
    ```python
            elif route == "chat":
                content, usage_resp = stream_chat_oneshot(args, url, key, prompt, tr,
                                                          anomaly_path)
    ```
  - Update `run_oneshot`'s docstring: "GPT and Claude one-shots stream".

- [ ] **Step 4: Move the Claude one-shot payload test onto the stream.** In `tests/test_engine_oneshot.py`, rewrite `test_claude_payload_flat_effort_and_temperature`:

```python
def test_claude_payload_flat_effort_and_temperature(harness) -> None:
    stream = [[{"choices": [{"delta": {"content": "hi"}}]}, {"_done": True}]]
    _oneshot(harness, "-m", "anthropic_tool_call.claude-x", "-e", "high",
             "--temperature", "0.3", replies=[], stream_replies=stream)
    path, p = harness.stream_calls[0]
    assert path == "/api/chat/completions"
    assert p["reasoning_effort"] == "high"
    assert "reasoning" not in p
    assert p["temperature"] == 0.3 and p["stream"] is True
    assert "tools" not in p and "tool_choice" not in p
    assert [m["role"] for m in p["messages"]] == ["user"]
```

  Also extend the helper `_oneshot(harness, *argv, replies)` to pass on `stream_replies=None`.

- [ ] **Step 5: Run the tests to see them pass.**
  - Run: `python -m pytest -q tests/test_engine_oneshot_stream.py tests/test_engine_oneshot.py tests/test_engine_reply_limits.py`
  - Expected: PASS.

- [ ] **Step 6: Run the whole suite.**
  - Run: `python -m pytest -q`
  - Expected: PASS.

- [ ] **Step 7: Commit** (done by the orchestrator):

```bash
git add engine/delegate.py tests/test_engine_oneshot.py tests/test_engine_oneshot_stream.py
git commit -m "feat(engine): stream Claude one-shots; retry a cut stream once with a shorter answer"
```

---

## Task 5: Progress and Live-now state for the new status lines

Runs in parallel with Task 1. Its files don't overlap with any engine task.

**Files:**
- Modify: `src/agent_control/progress.py`.
- Modify: `src/agent_control/snapshot.py` (constants around line 32, `_classify` around line 95).
- Modify: `tests/test_progress.py`, `tests/test_snapshot.py`, and any test that pins the keys of `RunProgress.as_dict()` (search the tests for `"last_path"`).

**Interfaces:**
- Consumes: the engine status-line formats and the footer formats in Global constraints.
- Produces:
  - `RunProgress.cuts: int = 0`, added as the last field;
  - `as_dict()` gains `"cuts"`;
  - `snapshot.CUT_ATTENTION = 2`;
  - `_classify` reports cuts.

- [ ] **Step 1: Write the failing tests.** Append to `tests/test_progress.py`:

```python
CUT = ("_[{n}] reply cut by the gateway's 2-minute limit after 121 s; "
       "asking for a smaller step_\n")


def test_streamed_final_report_is_not_finished():
    # Review focus 1: a streamed or killed one-shot has a final report but no footer.
    text = (
        "_[ 1] calling gpt-6.1-sol (attempt 1/2, 1 msgs, ~100 bytes) ..._\n"
        "\n---\n\n## Final report\n\nhalf an ans\n"
    )
    assert parse_progress(text).phase == "model_call"


def test_oneshot_footer_without_tokens_is_finished():
    # Review focus 2.
    text = (
        "_[ 1] calling qwen3.6:latest (attempt 1/4, 1 msgs, ~100 bytes) ..._\n"
        "\n---\n\n## Final report\n\nanswer\n\n"
        "[qwen3.6:latest e=medium tools=oneshot: 0 tool calls]\n"
    )
    assert parse_progress(text).phase == "finished"


def test_cut_lines_count_and_reset():
    text = (
        "_[ 3] calling anthropic_tool_call.claude-x (attempt 1/4, 9 msgs, ~100 bytes) ..._\n"
        + CUT.format(n=" 3")
        + "_[ 4] calling anthropic_tool_call.claude-x (attempt 1/4, 11 msgs, ~100 bytes) ..._\n"
        + CUT.format(n=" 4")
        + "_[ 5] calling anthropic_tool_call.claude-x (attempt 1/4, 13 msgs, ~100 bytes) ..._\n"
    )
    p = parse_progress(text)
    assert p.cuts == 2 and p.phase == "model_call" and p.iteration == 5
    assert p.as_dict()["cuts"] == 2
    assert parse_progress(text + '### [5] read_file({"path": "a.py"})\n').cuts == 0


def test_cut_line_in_task_brief_is_ignored():
    text = "## Task\n\n" + CUT.format(n=" 3") + "\n---\n"
    assert parse_progress(text).cuts == 0
```

  Append to `tests/test_snapshot.py`:

```python
def test_cut_progress_is_retrying_with_attention_from_the_second():
    from agent_control.progress import RunProgress
    from agent_control.snapshot import _classify

    def progress(cuts):
        return RunProgress(iteration=4, phase="model_call", attempt=1, max_attempts=4,
                           nudges=0, last_tool=None, last_path=None, cuts=cuts)

    state, reason, attention = _classify(None, progress(1), False, 5.0)
    assert (state, reason, attention) == (
        "retrying", "reply cut at the gateway's 2-minute limit", False)
    state, reason, attention = _classify(None, progress(2), False, 5.0)
    assert (state, reason, attention) == (
        "retrying", "2 replies cut at the gateway's 2-minute limit", True)
```

- [ ] **Step 2: Run the tests to see them fail.**
  - Run: `python -m pytest -q tests/test_progress.py tests/test_snapshot.py`
  - Expected: FAIL. There is no `cuts` field yet, and a streamed final report reads as finished.

- [ ] **Step 3: Update `progress.py`.**
  - **Footer:** replace `_FOOTER` and its comment with:

```python
# Run is finished: the engine's footer line, its last write. It matches agent
# runs ("[m e=medium tools=native: 12 tool calls, 114,120 tok sent / 7,081 tok
# returned]") and one-shots with unknown usage ("[m e=high tools=oneshot: 0 tool
# calls]"). "## Final report" alone no longer means finished: a streamed one-shot
# writes it before its answer is complete (live-thinking design).
_FOOTER = re.compile(r"^\[[^\]]*: [\d,]+ tool calls[^\]]*\]$")

# The engine's cut status line (reply-limits design): the gateway discarded a
# reply at its 2-minute limit and the engine asked for a smaller step.
_CUT = re.compile(r"^_\[\s*(\d+)\]\s+reply cut by the gateway's 2-minute limit after \d+ s;")
```

  - **Unused constant:** delete `_FINAL_REPORT`.
  - **Finished check:** change `if _FOOTER.match(line) or line.strip() == _FINAL_REPORT:` to `if _FOOTER.match(line):`.
  - **`RunProgress`:** add the last field `cuts: int = 0`, and `"cuts": self.cuts,` to `as_dict`. In the module docstring, change "the seven structural fields below" to "the eight structural fields below".
  - **`parse_progress`:**
    - add `cuts = 0` next to `nudges = 0`;
    - directly after the `_MODEL_CALL` block, add:

```python
        cut = _CUT.match(line)
        if cut:
            it = int(cut.group(1))
            iteration = it if iteration is None else max(iteration, it)
            cuts += 1
            continue
```

    - where a real tool step sets `nudges = 0`, also set `cuts = 0`;
    - pass `cuts=cuts` to `RunProgress(...)`.

- [ ] **Step 4: Update `snapshot.py`.**
  - Next to `NUDGE_ATTENTION = 2`, add `CUT_ATTENTION = 2`.
  - In `_classify`, directly after the `progress.attempt >= 2` block, insert:

```python
    if progress is not None and progress.phase == "model_call" and progress.cuts >= 1:
        n = progress.cuts
        reason = ("reply cut at the gateway's 2-minute limit" if n == 1
                  else f"{n} replies cut at the gateway's 2-minute limit")
        return "retrying", reason, n >= CUT_ATTENTION
```

- [ ] **Step 5: Run the tests to see them pass.**
  - Run: `python -m pytest -q tests/test_progress.py tests/test_snapshot.py tests/test_activity.py tests/test_activity_api.py`
  - Expected: PASS. Update any key-set assertion for `as_dict()` so it includes `"cuts"`.

- [ ] **Step 6: Run the whole suite.**
  - Run: `python -m pytest -q`
  - Expected: PASS.

- [ ] **Step 7: Commit** (done by the orchestrator):

```bash
git add src/agent_control/progress.py src/agent_control/snapshot.py tests/test_progress.py tests/test_snapshot.py
git commit -m "feat(progress): count 2-minute cuts; only the footer means a run is finished"
```

---

## Task 6: Narration shows status lines and text that is still being written

Runs in parallel with Task 1.

**Files:**
- Modify: `src/agent_control/narration.py`:
  - patterns near `_HEARTBEAT` (around line 70);
  - `_is_any_element_start`, `_classify_start`, `_build_events`, `_final_event`, `_parse_narration_body` and `_step_event`.
- Modify: `tests/test_narration.py`, plus any exact-dict assertion in `tests/test_narration.py` or `tests/test_narration_api.py` that now also gets `live`.

**Interfaces:**
- Consumes: the status-line formats in Global constraints.
- Produces:
  - events of kind `"status"` with `iteration` and `text`;
  - `"narration"` and `"final"` events carry `live: bool`;
  - `_parse_narration_body(body) -> (text, reasoning, live)`.
- `/api/narration` needs no change for LT §3.3's "re-sends a live event". The panel's `nextAfter()` already returns `events.length - 1`, so every poll re-fetches the last event, and only the last event can still be growing. The thinking section closes before the final report starts.

- [ ] **Step 1: Write the failing tests.** Append to `tests/test_narration.py`:

```python
STATUS_CUT = ("_[ 3] reply cut by the gateway's 2-minute limit after 121 s; "
              "asking for a smaller step_")
STATUS_PROSE = "_[ 4] reply had no tool call; asking for one_"
STATUS_TIMEOUT = "_[ 1] no reply within 600 s at max; retrying at xhigh_"
STATUS_ONESHOT = ("_[ 1] reply cut by the gateway's 2-minute limit after 121 s; "
                  "asking for a shorter answer_")


def test_status_lines_become_status_events() -> None:
    body = "\n\n".join([heartbeat(3), STATUS_CUT, heartbeat(4), STATUS_PROSE,
                        STATUS_TIMEOUT, STATUS_ONESHOT])
    statuses = [e for e in parse_narration(build(body)).events if e.kind == "status"]
    assert [e.iteration for e in statuses] == [3, 4, 1, 1]
    assert statuses[0].data["text"] == (
        "reply cut by the gateway's 2-minute limit after 121 s; asking for a smaller step")
    assert statuses[2].data["text"] == "no reply within 600 s at max; retrying at xhigh"


def test_status_text_inside_tool_output_is_not_an_event() -> None:
    body = ('### [2] read_file({"path": "notes.md"})\n\n' + FENCE + "\n" + STATUS_CUT
            + "\n" + FENCE + "\n\n" + heartbeat(3))
    assert not [e for e in parse_narration(build(body)).events if e.kind == "status"]


def test_unclosed_think_is_live_reasoning() -> None:
    body = heartbeat(1) + "\n\n### [1] thinking\n\n<think>\nweighing option B"
    ev = next(e for e in parse_narration(build(body)).events if e.kind == "narration")
    assert ev.data == {"text": None, "reasoning": "weighing option B", "live": True}


def test_closed_think_is_not_live() -> None:
    body = "### [1] thinking\n\n<think>Plan A</think>I'll start."
    ev = next(e for e in parse_narration(build(body)).events if e.kind == "narration")
    assert ev.data["live"] is False


def test_final_without_footer_is_live() -> None:
    body = heartbeat(1) + "\n\n---\n\n## Final report\n\nhalf an ans\n"
    final = next(e for e in parse_narration(build(body)).events if e.kind == "final")
    assert final.data == {"text": "half an ans", "partial": False, "live": True}


def test_final_with_footer_is_not_live() -> None:
    body = heartbeat(1) + "\n\n" + FINAL_TAIL
    final = next(e for e in parse_narration(build(body)).events if e.kind == "final")
    assert final.data["live"] is False


def test_cut_final_then_status_then_second_final() -> None:
    body = (heartbeat(1) + "\n\n---\n\n## Final report\n\nlong ans\n\n"
            "[cut off by the gateway's 2-minute limit]\n\n" + STATUS_ONESHOT + "\n\n"
            + heartbeat(1, attempt=2, maxa=2) + "\n\n---\n\n## Final report\n\nshort\n\n"
            f"[{MODEL} e=medium tools=oneshot: 0 tool calls]\n")
    kinds = [e.kind for e in parse_narration(build(body)).events]
    assert kinds == ["call", "final", "status", "call", "final"]
```

- [ ] **Step 2: Run the tests to see them fail.**
  - Run: `python -m pytest -q tests/test_narration.py`
  - Expected: FAIL. There is no `status` kind and no `live` key yet.

- [ ] **Step 3: Implement it in `narration.py`.**
  - **Patterns.** Below `_HEARTBEAT` add:

```python
# Engine status lines (reply-limits design, 2026-10-02), one per line in the
# heartbeat's italic style.
_STATUS = re.compile(
    r"^_\[\s*(\d+)\]\s+("
    r"reply cut by the gateway's 2-minute limit after \d+ s; "
    r"asking for a (?:smaller step|shorter answer)"
    r"|reply had no tool call; asking for one"
    r"|no reply within \d+ s at \w+; retrying at \w+"
    r")_$"
)
# A <think> with no closing tag yet: a streamed one-shot is still writing it.
_OPEN_THINK = re.compile(r"<think>(?![\s\S]*</think>)([\s\S]*)\Z")
```

  - **Element starts.** In `_is_any_element_start`, add `if _STATUS.match(line): return True` after the heartbeat check. In `_classify_start`, add `if _STATUS.match(line): return "status"` after the `"call"` check.
  - **Building events.** In `_build_events`, add a branch after `"call"`:

```python
        elif tag == "status":
            m = _STATUS.match(line)
            events.append(_make_event(seq, "status", int(m.group(1)), {
                "text": m.group(2),
            }))
            seq += 1
```

    In the `"final"` branch, compute `live = all(t != "footer" for _, t in elements[pos + 1:])` and call `_final_event(seq, lines, idx, next_idx, live)`.
  - **Final event.** Give `_final_event` a `live: bool` parameter, and return `{"text": text, "partial": partial, "live": live}`.
  - **Narration body.** Replace `_parse_narration_body` with:

```python
def _parse_narration_body(body: str) -> tuple[str | None, str | None, bool]:
    """Return (text, reasoning, live) for a thinking element body.

    live is True when the body ends inside an unclosed <think>: a streamed
    one-shot is still writing its reasoning.
    """
    reasons = [m.group(1).strip() for m in _THINK.finditer(body)]
    rest = _THINK.sub("", body)
    live = False
    m = _OPEN_THINK.search(rest)
    if m:
        live = True
        reasons.append(m.group(1).strip())
        rest = rest[: m.start()]
    reasons = [r for r in reasons if r]
    reasoning = "\n\n".join(reasons) if reasons else None
    rest = rest.strip()
    return (rest if rest else None), reasoning, live
```

  - **Step event.** In `_step_event`'s thinking branch, unpack `text, reasoning, live = _parse_narration_body(body)` and add `"live": live` to the event data. An event with neither text nor reasoning is still dropped.

- [ ] **Step 4: Run the tests to see them pass.**
  - Run: `python -m pytest -q tests/test_narration.py tests/test_narration_api.py tests/test_dashboard_api.py`
  - Expected: PASS. Where an existing test compares a whole narration or final event dict, add `"live": False`.

- [ ] **Step 5: Run the whole suite.**
  - Run: `python -m pytest -q`
  - Expected: PASS.

- [ ] **Step 6: Commit** (done by the orchestrator):

```bash
git add src/agent_control/narration.py tests/test_narration.py tests/test_narration_api.py
git commit -m "feat(narration): status events, and live reasoning and final report while a one-shot streams"
```

---

## Task 7: The panel shows status lines and marks text that is still being written

Runs in parallel with Task 1.

**Files:**
- Modify: `web/panel.js` (pure helpers near the top, and the `renderEvent` cases `narration`, `final` and a new `status`).
- Modify: `web/panel.css`.
- Modify: `tests/js/panel.test.mjs`.

**Interfaces:**
- Consumes: the narration event contract in Global constraints.
- Produces:
  - exported `statusText(event) -> string` and `liveLabel(base, event, runLive) -> string`;
  - the CSS class `rp-status`.

- [ ] **Step 1: Write the failing tests.**
  - In `tests/js/panel.test.mjs`, add `statusText` and `liveLabel` to the destructured import from `panel.js`.
  - Append these after the existing pure-helper tests:

```js
test("statusText capitalises the engine's status line", () => {
  assert.equal(
    statusText({ text: "reply had no tool call; asking for one" }),
    "Reply had no tool call; asking for one",
  );
  assert.equal(statusText({}), "");
});

test("liveLabel marks live text only while the run is live", () => {
  assert.equal(liveLabel("Final report", { live: true }, true), "Final report (writing…)");
  assert.equal(liveLabel("Final report", { live: true }, false), "Final report");
  assert.equal(liveLabel("Final report", { live: false }, true), "Final report");
});
```

  Append these after the existing DOM tests:

```js
test("status events render in their turn", async () => {
  setupGlobals();
  try {
    const deps = makeDeps();
    deps._queues["/api/narration"].push(
      narration(
        [
          { seq: 0, kind: "call", iteration: 3, attempt: 1 },
          {
            seq: 1,
            kind: "status",
            iteration: 3,
            text: "reply cut by the gateway's 2-minute limit after 121 s; asking for a smaller step",
          },
        ],
        2,
      ),
    );
    deps._queues["/api/activity"].push({ steps: [] });
    const panel = createRunPanel(deps);
    panel.open("r", null);
    await flush();
    const turn = findAll(globalThis.document.body, "rp-turn").find(
      (t) => t.getAttribute("data-iteration") === "3",
    );
    assert.equal(
      find(turn, "rp-status")._text,
      "Reply cut by the gateway's 2-minute limit after 121 s; asking for a smaller step",
    );
  } finally {
    teardownGlobals();
  }
});

test("a live final report shows writing only while the run is live", async () => {
  // Review focus 1: after the run ends (or is killed), the marker goes away.
  setupGlobals();
  try {
    const deps = makeDeps();
    const final = { seq: 1, kind: "final", iteration: null, text: "half", partial: false, live: true };
    deps._queues["/api/narration"].push(
      narration([{ seq: 0, kind: "call", iteration: 1, attempt: 1 }, final], 2),
    );
    deps._queues["/api/activity"].push({ steps: [] });
    const panel = createRunPanel(deps);
    panel.open("r", null);
    await flush();
    const heading = () =>
      find(globalThis.document.body, "rp-final").children.find((c) => c.tagName === "H3");
    assert.equal(heading()._text, "Final report (writing…)");
    deps._queues["/api/narration"].push(narration([final], 2));
    deps._queues["/api/activity"].push({ steps: [] });
    panel.update({ live: [], recently_ended: [{ run_id: "r" }] });
    await flush();
    assert.equal(heading()._text, "Final report");
  } finally {
    teardownGlobals();
  }
});
```

  If `renderHeader` needs more fields on the row passed to `panel.update`, copy the row shape from the existing test "kill outcome survives the next poll".

- [ ] **Step 2: Run the tests to see them fail.**
  - Run: `node --test tests/js/panel.test.mjs`
  - Expected: FAIL. `statusText` and `liveLabel` are not exported yet.

- [ ] **Step 3: Implement it in `web/panel.js`.** Add these after `reasoningFolded`:

```js
export function statusText(event) {
  const text = String((event && event.text) || "");
  return text ? text.charAt(0).toUpperCase() + text.slice(1) : "";
}

export function liveLabel(base, event, runLive) {
  return event && event.live && runLive ? `${base} (writing…)` : base;
}
```

  In `renderEvent`:
  - add a case:
    ```js
          case "status": {
            const div = el("div", { class: "rp-status warn" });
            div.textContent = statusText(event);
            return div;
          }
    ```
  - in `"narration"`, change the summary to `el("summary", { text: liveLabel("Reasoning", event, live) })`;
  - in `"final"`, change the heading to `h3.textContent = liveLabel(event.partial ? "Final report (partial)" : "Final report", event, live);`.

  Status events have an iteration, so `groupTimeline` already places them in their turn.

  In `web/panel.css`, add:
  ```css
  .rp-status { font-style: italic; font-size: 0.9em; margin: 0.25rem 0; }
  ```

- [ ] **Step 4: Run the tests to see them pass.**
  - Run: `node --test tests/js/dashboard.test.mjs tests/js/panel.test.mjs`
  - Expected: PASS.

- [ ] **Step 5: Commit** (done by the orchestrator):

```bash
git add web/panel.js web/panel.css tests/js/panel.test.mjs
git commit -m "feat(panel): show engine status lines and mark text still being written"
```

---

## Task 8: Operator documentation

Runs after Tasks 1–7.

**Files:**
- Modify: `docs/operator-guide.md`, `README.md`, `tests/test_operator_docs.py`.

**Interfaces:** none.

- [ ] **Step 1: Write the failing test.**
  - In `tests/test_operator_docs.py`, extend the existing phrase checks for the operator guide, following the file's style, with:
    - `"2-minute limit"`
    - `"asking for a smaller step"`
    - `"writing…"`
    - `"/openai/responses"`
  - Run: `python -m pytest -q tests/test_operator_docs.py`
  - Expected: FAIL.

- [ ] **Step 2: Add the section to `docs/operator-guide.md`.** Put it next to the existing "One-shot calls appear as runs" section, with this text:

```markdown
### Reply limits

The gateway cuts every Claude reply (`anthropic_tool_call.*`) that takes longer than about 121 seconds, streamed or not. The engine recognises each kind of failed reply and answers it:

- **A cut reply**, which arrives as a bare `Error:` after 100 seconds or more. The engine tells the model its reply was cut and asks for a smaller step, at most twice in a row. The narration shows "reply cut by the gateway's 2-minute limit after 121 s; asking for a smaller step".
- **A reply with text but no tool call.** The text is kept, and the model is asked for a tool call straight away.
- **No reply within 10 minutes.** The engine makes one more attempt, at xhigh when the effort was max.

Claude agents are told the limit in their instructions: one tool call per reply, and writes under about 120 lines. Live now shows such a run as retrying, and flags it for attention after a second cut in a row.

### One-shot calls stream

GPT and Claude one-shot calls stream:
- **GPT** goes through `/openai/responses`. The narration shows its reasoning summary when the gateway sends one, and its answer under "Final report (writing…)" while the answer is still arriving.
- **Claude's** thinking is not available from the gateway, but its answer streams the same way.
- **If streaming fails** before the first event, the engine repeats the call without streaming.
- **A Claude one-shot that runs past 2 minutes was cut.** It is asked once for a shorter answer. A second cut keeps that answer, marked "[cut off by the gateway's 2-minute limit]".
```

- [ ] **Step 3: Add to `README.md`'s feature list** these two bullets:
  - "Reply limits: the engine answers a Claude reply cut at the gateway's 2-minute limit with a request for a smaller step, instead of resending it."
  - "One-shot calls stream: the dashboard shows their answer, and GPT's reasoning summary, while they run."

- [ ] **Step 4: Run the tests to see them pass.**
  - Run: `python -m pytest -q tests/test_operator_docs.py`, then `python -m pytest -q`
  - Expected: PASS.

- [ ] **Step 5: Commit** (done by the orchestrator):

```bash
git add docs/operator-guide.md README.md tests/test_operator_docs.py
git commit -m "docs: reply limits and streaming one-shot calls"
```

---

## Task 9: Deploy and live checks (operator approval required)

Done by the orchestrator, after Tasks 1–8 are committed and the operator approves the deploy.

- [ ] **Step 1: Run the whole suite.**
  - Run: `python -m pytest -q` and `node --test tests/js/dashboard.test.mjs tests/js/panel.test.mjs`
  - Expected: PASS.
- [ ] **Step 2: Preview the deploy.** Run `python scripts/deploy_engine.py` (a dry run), show the operator the planned changes, and ask for approval.
- [ ] **Step 3: Apply it, once approved.** Run `python scripts/deploy_engine.py --apply`. Then check:
  - the installed files equal the repo (as in the 2026-10-02 one-shot deploy);
  - the Projectb, iptv, Planter Box and AgentControl selftests pass.
- [ ] **Step 4: Restart the dashboard** the way `docs/operator-guide.md` describes, so the narration and progress changes take effect.
- [ ] **Step 5: Live check, cuts.** In a lab directory with its own policy, run a Claude agent: `-m anthropic_tool_call.claude-opus-5-5-think -e medium`, briefed to write a 600-line file in one `write_file`. Expect:
  - at most two "reply cut" status lines;
  - no four-attempt retry loop;
  - the file written in parts.
- [ ] **Step 6: Live check, streaming.** Run `python -u ~/.claude/bin/ask-model.py -m gpt-6.1-sol -e xhigh --label live-check -p "<a planning question>"`. While it runs, read `GET /api/narration` for the run. Expect, before the run ends, a `final` event with `live: true`, or a `narration` event with reasoning.
- [ ] **Step 7: Update `~/.claude/DELEGATION.md`** (outside the repo). Under "Keep every Claude reply under 2 minutes", add one sentence: "Since <deploy date> the engine answers a cut reply with a request for a smaller step instead of resending it."

---

## Execution

The operator chose the method: each task is one agent on the OpenWebUI harness, and the orchestrator reviews, applies and commits.

- **Launch** each task in its own worktree, with a watcher beside it:
  ```
  timeout 2700 python -u ~/.claude/bin/delegate.py -C "D:/repos/AgentControl" --worktree -m gpt-6.1-sol -e high --label <task-label> --max-iters 30 -f <brief>
  python -u ~/.claude/bin/agent_await.py --repo "D:/repos/AgentControl" --label <task-label>
  ```
- **The brief** is the task's section from this plan plus the Global constraints. The first action is the task's Step 1.
- **The executor model is GPT at `-e high`.** It has no 2-minute reply cut, and Claude executors would hit the cut this plan fixes.
- **Waves:**

  | Wave | Tasks |
  |---|---|
  | 1, in parallel | Tasks 1, 5, 6, 7 (disjoint files), plus Task 0 by the orchestrator |
  | 2 | Task 2 |
  | 3 | Task 3 |
  | 4 | Task 4 |
  | 5 | Task 8 |
  | 6 | Task 9, after operator approval |

- **After each task:**
  1. apply the worktree patch (`git apply --check`, then `git apply`);
  2. run the whole Python and JS suites;
  3. read the diff against the task's steps;
  4. commit with the task's message, ending with the repo's Co-Authored-By line.

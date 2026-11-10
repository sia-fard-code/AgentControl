# Live agent turns: implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. In this repo, tasks are dispatched one per agent on the OpenWebUI harness (`~/.claude/bin/delegate.py --worktree`), never through built-in subagent tools; see "Execution" at the end.

**Goal:** Agent turns stream, so the dashboard shows GPT's reasoning, each step's note, and the text of files and final reports while they are written.

**Architecture:**
- **Engine** (`engine/delegate.py`, one file, standard library only):
  - Task 1 adds pure building blocks: Responses conversion, an incremental JSON-field decoder, a fence holder, and the live transcript writer `LiveTurn`.
  - Task 2 streams GPT turns through `/openai/responses` inside `call_model`. It also adds the failure handling and `--no-stream`.
  - Task 3 adds Claude turns through chat streaming, the streamed-cut rule, and prompt mode.
- **AgentControl:** narration learns the `preview` event and the streaming notice (Task 4), and the panel renders a live preview (Task 5).
- **Shared tools:** `~/.claude/bin/agent_await.py` gets an anchored heartbeat pattern and a wider search (Task 6, done by the orchestrator).

**Tech stack:** Python 3.13 standard library and pytest; vanilla JS ES modules with `node:test` (Node 22).

**Spec:** `docs/superpowers/specs/2026-10-03-live-agent-turns-design.md`, cited below as LA.

## Global constraints

- **Test isolation:** every test injects temporary paths and never writes live user accounting state (AGENTS.md).
- **No content outside transcripts:** never place prompts, assistant content, tool results, credentials, response bodies or complete sidecars in source, fixtures, logs, reports, AgentControl state or APIs. The one exception is `GET /api/narration`. Test fixtures use synthetic text only.
- **Deploying:** do not deploy the shared engine without explicit operator approval. Only Task 8 deploys.
- **Engine file:**
  - it stays one file, standard library only, with no sibling imports;
  - `HARNESS_VERSION` does not change;
  - `REQUEST_TIMEOUT = 600`, `REPLY_CUT_SECONDS = 100`, `CUT_CORRECTIONS_PER_TURN = 2` and `STREAM_IDLE_TIMEOUT = 300` do not change.
- **Transcript strings, exact:**
  - **composing line:** `tr.write(f"\n_[{turn:>2}] composing {tool}{' ' + path if path else ''} ..._")`, for example `_[ 3] composing write_file src/app.py ..._`;
  - **preview lines:** `> ` plus the text, or a bare `>` for an empty line;
  - **notice:** `**streaming unavailable (HTTP 400); continuing without live view**`, with the real status code;
  - **section heading:** `### [3] thinking`, unchanged;
  - **reasoning:** inside `<think>` and `</think>`, each on its own line.
- **Previewed fields:** `PREVIEW_FIELDS = {"write_file": "content", "replace_in_file": "new_string", "finish": "summary"}`.
- **Narration event contract** (Tasks 4 and 5): kind `"preview"`, with `iteration` (int) and data `{"tool": str, "path": str | None, "text": str, "live": bool}`.
- **Test commands:**
  - `python -m pytest -q` (run it through `rtk proxy python -m pytest -q` in the orchestrator's shell);
  - `node --test tests/js/dashboard.test.mjs tests/js/panel.test.mjs`.
- **Never** chain a check through `| tail` before `&& git commit`.

## Review focus

1. **A lone surrogate escape** (`\ud83d` with no pair) in previewed text must not crash the transcript write. Test in Task 1.
2. **`\r\n` line endings** in a previewed file must not leave a stray `\r` in preview lines. Test in Task 1.
3. **Two function calls in one GPT turn** must both reach `tool_calls`, in order, with their own `call_id`s. Test in Task 2.
4. **A Claude tool-call delta without `index`** must be treated as index 0, not crash. Test in Task 3.
5. **A run killed mid-preview** (no footer, preview last) must keep its preview text visible, without "(writing…)". Test in Task 5.

---

## Task 1: Engine building blocks (pure, no network)

**Files:**
- Modify: `engine/delegate.py`. Add the code below after `class LiveAnswer` (around line 1063), and `_PATH_ARG` and `PREVIEW_FIELDS` beside `THINK_BLOCK` (around line 1153).
- Create: `tests/test_engine_live_turns.py`

**Interfaces:**
- Produces:
  - `PREVIEW_FIELDS: dict[str, str]`;
  - `responses_input(msgs: list[dict]) -> list[dict]`;
  - `responses_tools(tools: list[dict]) -> list[dict]`;
  - `class ArgFieldStream(field: str)`: `.feed(piece: str) -> str`, `.started: bool`, `.done: bool`, `.path: str | None`;
  - `class FenceHold()`: `.feed(text) -> str`, `.flush() -> str`;
  - `class LiveTurn(tr, turn: int)`: `.reasoning(text)`, `.note(text)`, `.preview_start(tool, path)`, `.preview_text(text)`, `.close()`, `.wrote_note: bool`.

- [ ] **Step 1: Write the failing tests** in `tests/test_engine_live_turns.py`:

```python
"""Live agent turns: pure building blocks (LA §3.1, §3.2)."""
from __future__ import annotations

import json

import pytest

from test_engine_reasoning import engine  # noqa: F401


def _tr(engine, tmp_path):
    return engine.Transcript(str(tmp_path / "t.md"), "# t")


def _text(tmp_path):
    return (tmp_path / "t.md").read_text(encoding="utf-8")


def test_responses_input_converts_the_whole_history(engine):
    msgs = [
        {"role": "system", "content": "S"},
        {"role": "user", "content": "U"},
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "call_1", "type": "function",
             "function": {"name": "list_dir", "arguments": '{"path": "."}'}}]},
        {"role": "tool", "tool_call_id": "call_1", "content": "a.py"},
        {"role": "assistant", "content": engine.CUT_PLACEHOLDER},
        {"role": "user", "content": engine.CUT_CORRECTION},
    ]
    assert engine.responses_input(msgs) == [
        {"role": "system", "content": "S"},
        {"role": "user", "content": "U"},
        {"type": "function_call", "call_id": "call_1", "name": "list_dir",
         "arguments": '{"path": "."}'},
        {"type": "function_call_output", "call_id": "call_1", "output": "a.py"},
        {"role": "assistant", "content": engine.CUT_PLACEHOLDER},
        {"role": "user", "content": engine.CUT_CORRECTION},
    ]


def test_responses_tools_flattens_function_tools(engine):
    out = engine.responses_tools(engine.native_tools())
    names = [t["name"] for t in out]
    assert "write_file" in names and "finish" in names
    assert all(t["type"] == "function" and "parameters" in t and "function" not in t
               for t in out)


def _feed_all(fs, raw, step):
    return "".join(fs.feed(raw[k:k + step]) for k in range(0, len(raw), step))


@pytest.mark.parametrize("step", [1, 2, 3, 5, 64])
def test_arg_field_stream_decodes_split_escapes(engine, step):
    value = 'line "one"\n\\path\ttab é 😀 end'
    raw = json.dumps({"path": "src/a.py", "content": value})
    fs = engine.ArgFieldStream("content")
    assert _feed_all(fs, raw, step) == value
    assert fs.done and fs.started and fs.path == "src/a.py"


def test_arg_field_stream_path_after_the_field_is_found_late(engine):
    fs = engine.ArgFieldStream("content")
    first = fs.feed('{"content": "abc')
    assert first == "abc" and fs.started and fs.path is None
    fs.feed('", "path": "x.py"}')
    assert fs.done and fs.path == "x.py"


def test_arg_field_stream_lone_surrogate_does_not_break_writing(engine, tmp_path):
    fs = engine.ArgFieldStream("content")
    text = fs.feed('{"content": "a\\ud83db"}')
    tr = _tr(engine, tmp_path)
    live = engine.LiveTurn(tr, 1)
    live.preview_start("write_file", None)
    live.preview_text(text)          # must not raise UnicodeEncodeError
    live.close()
    assert "> a" in _text(tmp_path)


def test_fence_hold_stops_at_the_tool_fence_even_when_split(engine):
    h = engine.FenceHold()
    out = h.feed("I will list it.\n``") + h.feed("`to") + h.feed('ol\n{"name": "x"}')
    assert out == "I will list it.\n" and h.flush() == ""


def test_live_turn_reasoning_then_note(engine, tmp_path):
    tr = _tr(engine, tmp_path)
    live = engine.LiveTurn(tr, 3)
    live.reasoning("Checking ")
    live.reasoning("the file.")
    assert "### [3] thinking\n\n<think>\nChecking the file." in _text(tmp_path)
    live.note("\n\n")                 # whitespace alone opens nothing
    live.note("I will read it.")
    live.close()
    assert _text(tmp_path).endswith(
        "### [3] thinking\n\n<think>\nChecking the file.\n</think>\n\nI will read it.\n")
    assert live.wrote_note


def test_live_turn_whitespace_only_writes_nothing(engine, tmp_path):
    tr = _tr(engine, tmp_path)
    live = engine.LiveTurn(tr, 2)
    live.note("\n \n")
    live.close()
    assert "thinking" not in _text(tmp_path) and not live.wrote_note


def test_live_turn_preview_prefixes_every_line(engine, tmp_path):
    tr = _tr(engine, tmp_path)
    live = engine.LiveTurn(tr, 3)
    live.note("Writing it.")
    live.preview_start("write_file", "src/app.py")
    live.preview_text("### [9] thinking\n\n---\r\n## Fin")
    live.preview_text("al report\n[m: 3 tool calls]")
    live.close()
    tail = _text(tmp_path).split("Writing it.\n", 1)[1]
    assert tail == ("\n_[ 3] composing write_file src/app.py ..._\n"
                    "> ### [9] thinking\n>\n> ---\n> ## Final report\n> [m: 3 tool calls]\n")


def test_live_turn_preview_without_path_and_newlines_in_path(engine, tmp_path):
    tr = _tr(engine, tmp_path)
    live = engine.LiveTurn(tr, 12)
    live.preview_start("finish", None)
    live.preview_text("done")
    live.close()
    live2 = engine.LiveTurn(tr, 13)
    live2.preview_start("write_file", "a\nb.py")
    live2.close()
    text = _text(tmp_path)
    assert "_[12] composing finish ..._\n> done\n" in text
    assert "_[13] composing write_file a b.py ..._" in text


def test_live_turn_note_after_preview_opens_a_new_section(engine, tmp_path):
    tr = _tr(engine, tmp_path)
    live = engine.LiveTurn(tr, 4)
    live.preview_start("write_file", "a.py")
    live.preview_text("x")
    live.note("and more")
    live.close()
    assert "> x\n\n### [4] thinking\n\nand more\n" in _text(tmp_path)
```

- [ ] **Step 2: Run them.** `python -m pytest -q tests/test_engine_live_turns.py`. Expected: FAIL with `AttributeError: module ... has no attribute 'responses_input'`.

- [ ] **Step 3: Implement.** Beside `THINK_BLOCK`:

```python
# Tool-call fields whose text is previewed while it streams (live agent turns, LA §3.2).
PREVIEW_FIELDS = {"write_file": "content", "replace_in_file": "new_string",
                  "finish": "summary"}
_PATH_ARG = re.compile(r'"path"\s*:\s*"((?:[^"\\]|\\.)*)"')
_HEX = set("0123456789abcdefABCDEF")
```

After `class LiveAnswer`:

```python
def responses_input(msgs):
    """The engine's chat-format history as Responses API input items (LA §3.1)."""
    items = []
    for m in msgs:
        role, content = m.get("role"), m.get("content") or ""
        if role == "tool":
            items.append({"type": "function_call_output", "call_id": m.get("tool_call_id"),
                          "output": content})
        elif role == "assistant":
            if content:
                items.append({"role": "assistant", "content": content})
            for c in m.get("tool_calls") or []:
                fn = c.get("function") or {}
                items.append({"type": "function_call", "call_id": c.get("id"),
                              "name": fn.get("name"),
                              "arguments": fn.get("arguments") or "{}"})
        else:
            items.append({"role": role, "content": content})
    return items


def responses_tools(tools):
    """native_tools() entries in the Responses API's flat function form."""
    return [{"type": "function", "name": t["function"]["name"],
             "description": t["function"]["description"],
             "parameters": t["function"]["parameters"]} for t in tools]


def _printable(text):
    # A lone surrogate from a broken \\uXXXX escape cannot be written as UTF-8.
    return text.encode("utf-8", "replace").decode("utf-8")


class ArgFieldStream:
    """Decode one string field of a tool call's JSON arguments as they stream.

    feed(piece) returns the field's newly decoded text. An escape split across
    pieces (a lone backslash, a partial \\uXXXX, or a surrogate pair's first
    half) is held until complete. path holds the "path" argument once its
    string has closed. The key search is textual: the first "<field>": "
    in the arguments, which is the argument key in practice.
    """

    def __init__(self, field):
        self.key = re.compile(r'"%s"\s*:\s*"' % re.escape(field))
        self.buf = ""
        self.pos = None
        self.started = False
        self.done = False
        self.path = None

    def feed(self, piece):
        self.buf += piece or ""
        if self.path is None:
            m = _PATH_ARG.search(self.buf)
            if m:
                try:
                    self.path = json.loads('"' + m.group(1) + '"')
                except ValueError:
                    self.path = m.group(1)
        if self.done:
            return ""
        if self.pos is None:
            m = self.key.search(self.buf)
            if not m:
                return ""
            self.pos, self.started = m.end(), True
        buf, i, n = self.buf, self.pos, len(self.buf)
        end = None
        while i < n:
            ch = buf[i]
            if ch == '"':
                end = i
                break
            if ch != "\\":
                i += 1
                continue
            if i + 1 >= n:
                break
            if buf[i + 1] != "u":
                i += 2
                continue
            if i + 6 > n:
                break
            code = buf[i + 2:i + 6]
            if all(c in _HEX for c in code) and 0xD800 <= int(code, 16) <= 0xDBFF:
                if i + 7 >= n:
                    break                  # wait to see whether a pair follows
                if buf[i + 6:i + 8] == "\\u":
                    if i + 12 > n:
                        break
                    i += 12
                    continue
            i += 6
        segment = buf[self.pos:i]
        if end is not None:
            self.done, self.pos = True, end + 1
        else:
            self.pos = i
        try:
            text = json.loads('"' + segment + '"')
        except ValueError:
            text = segment
        return _printable(text)


class FenceHold:
    """Pass prompt-mode text through until the ```tool fence; hold the rest."""

    FENCE = "```tool"

    def __init__(self):
        self.carry = ""
        self.closed = False

    def feed(self, text):
        if self.closed:
            return ""
        buf = self.carry + (text or "")
        i = buf.find(self.FENCE)
        if i != -1:
            self.closed, self.carry = True, ""
            return buf[:i]
        keep = max((k for k in range(1, len(self.FENCE)) if buf.endswith(self.FENCE[:k])),
                   default=0)
        self.carry = buf[-keep:] if keep else ""
        return buf[:-keep] if keep else buf

    def flush(self):
        rest = "" if self.closed else self.carry
        self.carry = ""
        return rest


class LiveTurn:
    """Writes one streamed agent turn into the transcript as it arrives (LA §3.2).

    Reasoning goes inside <think>...</think> under "### [i] thinking", the note
    follows as plain text, and previewed tool text follows a composing line with
    each line prefixed "> " (a bare ">" when empty), so no line of a file can
    start a step, a final report, a status line or the footer. wrote_note tells
    the loop not to write the note again.
    """

    def __init__(self, tr, turn):
        self.tr = tr
        self.turn = turn
        self.state = "idle"        # idle | reasoning | note | preview | closed
        self.section = False       # a "### [i] thinking" section is open
        self.wrote_note = False
        self.pending = ""          # whitespace held until the note has real text
        self.line_open = False     # the current preview line has text

    def _end(self):
        if self.state == "reasoning":
            self.tr.write("\n</think>")
        elif self.state == "note" or (self.state == "preview" and self.line_open):
            self.tr.write("")
        self.line_open = False
        self.state = "idle"

    def _open(self, first):
        if self.section:
            self.tr.write(first)
        else:
            self.tr.write(f"\n### [{self.turn}] thinking\n" + first)
            self.section = True

    def reasoning(self, text):
        if not text or self.state == "closed":
            return
        if self.state != "reasoning":
            self._end()
            self._open("\n<think>")
            self.state = "reasoning"
        self.tr.append(_printable(text))

    def note(self, text):
        if not text or self.state == "closed":
            return
        if self.state == "note":
            self.tr.append(_printable(text))
            return
        self.pending += text
        if not self.pending.strip():
            return
        text, self.pending = self.pending.lstrip(), ""
        self._end()
        self._open("")
        self.state = "note"
        self.wrote_note = True
        self.tr.append(_printable(text))

    def preview_start(self, tool, path):
        if self.state == "closed":
            return
        self._end()
        self.pending = ""
        where = " " + " ".join(str(path).split()) if path else ""
        self.tr.write(f"\n_[{self.turn:>2}] composing {tool}{where} ..._")
        self.section = False
        self.state = "preview"

    def preview_text(self, text):
        if self.state != "preview" or not text:
            return
        out = []
        for k, part in enumerate(_printable(text).replace("\r", "").split("\n")):
            if k:
                out.append("\n" if self.line_open else ">\n")
                self.line_open = False
            if part:
                if not self.line_open:
                    out.append("> ")
                    self.line_open = True
                out.append(part)
        self.tr.append("".join(out))

    def close(self):
        if self.state == "closed":
            return
        self._end()
        self.pending = ""
        self.tr.flush()
        self.state = "closed"
```

  Check `_open` against the tests: with no section, `note` writes `"\n### [3] thinking\n"` plus `""`, so a blank line separates the heading from the note. With a section already open, it writes `""`, a blank line after `</think>`. For reasoning, the first text after `<think>` starts on the next line. If a test's expected text differs only in blank lines, fix the code to match the test, never the reverse: the tests encode LA §3.2's layout, which `narration.py` parses.

- [ ] **Step 4: Run them.** `python -m pytest -q tests/test_engine_live_turns.py`. Expected: PASS. Then `python -m pytest -q`. Expected: all pass.

- [ ] **Step 5: Commit.** `git add engine/delegate.py tests/test_engine_live_turns.py`, then `git commit -m "feat(engine): building blocks for live agent turns"`.

---

## Task 2: Stream GPT agent turns through /openai/responses

**Files:**
- Modify: `engine/delegate.py`:
  - the argument parser (around line 1990): add `--no-stream`;
  - `run_oneshot` (around line 1854): honour it;
  - `call_model` (around line 1449): add a streaming branch;
  - `main()`'s agent loop (around lines 2178–2300).
- Modify: `tests/test_engine_reasoning.py`. In the harness's `run()`, add `--no-stream` when `stream_replies is None`.
- Create: `tests/test_engine_live_turns_gpt.py`

**Interfaces:**
- Consumes (Task 1): `responses_input`, `responses_tools`, `ArgFieldStream`, `LiveTurn`, `PREVIEW_FIELDS`.
- Produces (Task 3 uses these):
  - `class TurnStreaming(route)` with an attribute `route: str | None`;
  - `agent_stream_route(model, mode, no_stream) -> str | None` (Task 2 returns `"responses"` or `None`);
  - `responses_payload(payload) -> dict`;
  - `_preview_feed(live, entry, piece)`, where `entry` is `(tool, ArgFieldStream)`;
  - `stream_gpt_turn(url, key, payload, live) -> dict` (chat-shaped);
  - `stream_turn(url, key, payload, route, live, mode, turn) -> dict` (chat-shaped, with `"_live": True`);
  - `_http_status(exc) -> int | None`;
  - `call_model(url, key, payload, mode, tr, anomaly_path, turn, streaming=None)`.

- [ ] **Step 1: Keep existing tests on today's path.** In `tests/test_engine_reasoning.py`, `harness.run`, before building `sys.argv`:

```python
            if stream_replies is None:
                argv = ("--no-stream", *argv)   # tests without scripts keep today's path
```

  Run `python -m pytest -q`. Expected: the existing tests now FAIL with `unrecognized arguments: --no-stream` (the flag does not exist yet).

- [ ] **Step 2: Write the failing tests** in `tests/test_engine_live_turns_gpt.py`:

```python
"""Live agent turns: GPT through /openai/responses (LA §3.1, §3.3)."""
from __future__ import annotations

import json

import pytest

from test_engine_reasoning import RESPONSES_USAGE, engine, harness  # noqa: F401


def gpt_turn(name, args, call_id="call_1", summary=("Checking ", "the file."), note="",
             usage=RESPONSES_USAGE, extra_calls=()):
    events = [{"type": "response.created"}]
    for d in summary:
        events.append({"type": "response.reasoning_summary_text.delta", "delta": d})
    if summary:
        events.append({"type": "response.reasoning_summary_part.done"})
    if note:
        events.append({"type": "response.output_text.delta", "delta": note})
    for nm, ar, cid in ((name, args, call_id), *extra_calls):
        raw = json.dumps(ar)
        item = {"type": "function_call", "id": "fc_" + cid, "call_id": cid,
                "name": nm, "arguments": ""}
        events.append({"type": "response.output_item.added", "item": item})
        for k in range(0, len(raw), 7):
            events.append({"type": "response.function_call_arguments.delta",
                           "item_id": item["id"], "delta": raw[k:k + 7]})
        events.append({"type": "response.output_item.done",
                       "item": {**item, "arguments": raw}})
    events.append({"type": "response.completed", "response": {"usage": usage}})
    return events


def finish_turn(call_id="call_9"):
    return gpt_turn("finish", {"summary": "all done"}, call_id=call_id, summary=())


def _records(path):
    return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]


def test_reasoning_is_in_the_transcript_before_the_turn_ends(harness):
    seen = []
    turn = gpt_turn("list_dir", {"path": "."})
    turn.insert(4, lambda: seen.append(harness.transcript_path.read_text(encoding="utf-8")))
    text = harness.run("-m", "gpt-6-sol", "-e", "high", replies=[],
                       stream_replies=[turn, finish_turn()])
    assert "### [1] thinking\n\n<think>\nChecking the file." in seen[0]
    assert "### [1] list_dir" not in seen[0]
    assert "<think>\nChecking the file.\n\n\n</think>" in text or \
           "<think>\nChecking the file.\n\n</think>" in text
    assert "### [1] list_dir(" in text


def test_request_shape_and_history_round_trip(harness):
    harness.run("-m", "gpt-6-sol", "-e", "high", replies=[],
                stream_replies=[gpt_turn("list_dir", {"path": "."}), finish_turn()])
    path, first = harness.stream_calls[0]
    assert path == "/openai/responses"
    assert first["stream"] is True and first["tool_choice"] == "auto"
    assert first["reasoning"] == {"effort": "high", "summary": "auto"}
    assert first["input"][0]["role"] == "system"
    assert all(t["type"] == "function" and "name" in t for t in first["tools"])
    assert "messages" not in first
    second = harness.stream_calls[1][1]["input"]
    assert {"type": "function_call", "call_id": "call_1", "name": "list_dir",
            "arguments": json.dumps({"path": "."})} in second
    outputs = [i for i in second if i.get("type") == "function_call_output"]
    assert outputs and outputs[0]["call_id"] == "call_1"


def test_usage_row_comes_from_response_completed(harness):
    harness.run("-m", "gpt-6-sol", replies=[],
                stream_replies=[gpt_turn("list_dir", {"path": "."}), finish_turn()])
    rows = _records(harness.transcript_path.with_suffix(".usage.jsonl"))
    assert rows[0]["sent"] == 100 and rows[0]["recv"] == 20


def test_note_is_streamed_once(harness):
    text = harness.run("-m", "gpt-6-sol", replies=[],
                       stream_replies=[gpt_turn("list_dir", {"path": "."}, summary=(),
                                                note="Listing the root."),
                                       finish_turn()])
    assert text.count("Listing the root.") == 1


def test_two_function_calls_in_one_turn(harness):
    turn = gpt_turn("list_dir", {"path": "."},
                    extra_calls=(("list_dir", {"path": ".claude"}, "call_2"),))
    text = harness.run("-m", "gpt-6-sol", replies=[], stream_replies=[turn, finish_turn()])
    second = harness.stream_calls[1][1]["input"]
    ids = [i["call_id"] for i in second if i.get("type") == "function_call_output"]
    assert ids == ["call_1", "call_2"]
    assert text.count("### [1] list_dir(") == 2


def test_write_file_text_is_previewed_while_it_streams(harness, tmp_path):
    seen = []
    turn = gpt_turn("write_file", {"path": "notes.md", "content": "# Title\n\nbody"}, summary=())
    turn.insert(-2, lambda: seen.append(harness.transcript_path.read_text(encoding="utf-8")))
    harness.run("-m", "gpt-6-sol", "--write", replies=[], stream_replies=[turn, finish_turn()])
    assert "_[ 1] composing write_file notes.md ..._\n> # Title\n>\n> body" in seen[0]


def test_finish_summary_is_previewed(harness):
    text = harness.run("-m", "gpt-6-sol", replies=[], stream_replies=[finish_turn()])
    assert "_[ 1] composing finish ..._\n> all done" in text


def test_rejected_stream_falls_back_for_the_rest_of_the_run(harness, engine, capsys):
    err = engine.StreamError("http", "HTTP 400: unsupported parameter", 0)
    text = harness.run("-m", "gpt-6-sol",
                       replies=[{"choices": [{"message": {"content": "", "tool_calls": [
                           {"id": "c1", "type": "function", "function": {
                               "name": "list_dir", "arguments": '{"path": "."}'}}]}}]},
                                {"choices": [{"message": {"content": "", "tool_calls": [
                                    {"id": "c2", "type": "function", "function": {
                                        "name": "finish", "arguments": '{"summary": "ok"}'}}]}}]}],
                       stream_replies=[err])
    assert text.count("**streaming unavailable (HTTP 400); continuing without live view**") == 1
    assert len(harness.stream_calls) == 1 and len(harness.payloads) == 2


def test_model_not_found_is_refreshed_and_retried_streaming(harness, engine):
    stale = engine.StreamError("http", "HTTP 400: Model not found", 0)
    harness.run("-m", "gpt-6-sol", replies=[], stream_replies=[stale, finish_turn()])
    assert len(harness.stream_calls) == 2 and harness.payloads == []
    assert "streaming unavailable" not in harness.transcript_path.read_text(encoding="utf-8")


@pytest.mark.parametrize("err", [
    ("http", "HTTP 502: bad gateway", 0),
    ("transport", "cannot reach gateway: ConnectionResetError", 0),
    ("transport", "the stream ended before the response completed", 3),
])
def test_transport_failures_retry_streaming(harness, engine, err):
    harness.run("-m", "gpt-6-sol", replies=[],
                stream_replies=[engine.StreamError(*err), finish_turn()])
    assert len(harness.stream_calls) == 2 and harness.payloads == []


def test_mid_stream_failure_closes_open_reasoning(harness, engine):
    broken = gpt_turn("list_dir", {"path": "."})[:3] + [
        engine.StreamError("transport", "connection reset", 3)]
    text = harness.run("-m", "gpt-6-sol", replies=[], stream_replies=[broken, finish_turn()])
    first = text.split("attempt 2/4", 1)[0]
    assert first.count("<think>") == first.count("</think>") == 1


def test_idle_stream_takes_the_timeout_path_at_xhigh_after_max(harness, engine):
    idle = engine.StreamError("idle", "no event for 600s", 0)
    harness.run("-m", "gpt-6.1-sol", "-e", "max", replies=[],
                stream_replies=[idle, finish_turn()])
    assert harness.stream_calls[1][1]["reasoning"]["effort"] == "xhigh"


def test_no_stream_flag_makes_no_streaming_request(harness):
    from test_engine_reasoning import _finish_reply
    harness.run("-m", "gpt-6-sol", replies=[_finish_reply("native")])   # adds --no-stream
    assert harness.stream_calls == []


def test_no_stream_also_turns_off_oneshot_streaming(harness):
    harness.run("--oneshot", "-m", "gpt-6-sol",
                replies=[{"choices": [{"message": {"content": "an answer"}}]}])
    assert harness.stream_calls == []


def test_other_models_and_prompt_mode_gpt_stay_non_streamed(engine):
    assert engine.agent_stream_route("gpt-6-sol", "native", False) == "responses"
    assert engine.agent_stream_route("gpt-6-sol", "prompt", False) is None
    assert engine.agent_stream_route("qwen3.6:latest", "prompt", False) is None
    assert engine.agent_stream_route("gpt-6-sol", "native", True) is None
```

- [ ] **Step 3: Run them.** `python -m pytest -q tests/test_engine_live_turns_gpt.py`. Expected: FAIL (`unrecognized arguments: --no-stream`, then missing names).

- [ ] **Step 4: Implement.**

  4a. **The flag**, beside `--temperature`:

```python
    ap.add_argument("--no-stream", action="store_true",
                    help="send agent turns and one-shots without streaming (no live view)")
```

  In `run_oneshot`: `route = None if getattr(args, "no_stream", False) else oneshot_stream_route(args.model)`.

  4b. **Routing and the payload**, after `oneshot_stream_route`:

```python
class TurnStreaming:
    """Per-run streaming state for agent turns. route is None once the gateway
    rejected the streamed form (LA §3.3), or when streaming is off."""

    def __init__(self, route):
        self.route = route


def agent_stream_route(model, mode, no_stream):
    """The streaming route an agent turn takes, or None for today's request."""
    if no_stream:
        return None
    if model.startswith("gpt-") and mode == "native":
        return "responses"
    return None


def responses_payload(payload):
    """A chat-completions agent payload as a streamed Responses request."""
    body = {"model": payload["model"], "input": responses_input(payload.get("messages") or []),
            "stream": True,
            "reasoning": {**(payload.get("reasoning") or {}), "summary": "auto"}}
    if payload.get("tools"):
        body["tools"] = responses_tools(payload["tools"])
        body["tool_choice"] = "auto"
    if payload.get("temperature") is not None:
        body["temperature"] = payload["temperature"]
    return body


def _http_status(exc):
    m = re.match(r"HTTP (\d{3})", getattr(exc, "detail", "") or "")
    return int(m.group(1)) if m else None


def _preview_feed(live, entry, piece):
    tool, fs = entry
    was = fs.started
    text = fs.feed(piece)
    if fs.started and not was:
        live.preview_start(tool, fs.path)
    if text:
        live.preview_text(text)
```

  4c. **The GPT turn:**

```python
def stream_gpt_turn(url, key, payload, live):
    """One GPT agent turn through /openai/responses, written live (LA §3.1).

    Returns a chat-shaped reply: content is the output text, tool_calls come
    from function_call items in order, usage from response.completed. Raises
    StreamError when the gateway reports a failure or the stream ends before
    response.completed.
    """
    text, calls, previews = [], [], {}
    usage, completed, summary_deltas, seen = None, False, False, 0
    for ev in stream_events(url, key, "/openai/responses", payload,
                            idle_timeout=REQUEST_TIMEOUT):
        if ev.get("_done"):
            continue
        seen += 1
        kind = ev.get("type") or ""
        if kind == "response.reasoning_summary_text.delta":
            summary_deltas = True
            live.reasoning(ev.get("delta") or "")
        elif kind == "response.reasoning_summary_part.done" and summary_deltas:
            live.reasoning("\n\n")
        elif kind == "response.output_text.delta":
            piece = ev.get("delta") or ""
            text.append(piece)
            live.note(piece)
        elif kind == "response.output_item.added":
            item = ev.get("item") or {}
            if item.get("type") == "function_call" and item.get("name") in PREVIEW_FIELDS:
                previews[item.get("id")] = (item["name"],
                                            ArgFieldStream(PREVIEW_FIELDS[item["name"]]))
        elif kind == "response.function_call_arguments.delta":
            entry = previews.get(ev.get("item_id"))
            if entry:
                _preview_feed(live, entry, ev.get("delta") or "")
        elif kind == "response.output_item.done":
            item = ev.get("item") or {}
            if item.get("type") == "function_call":
                calls.append(item)
            elif item.get("type") == "reasoning" and not summary_deltas:
                live.reasoning("\n\n".join(s.get("text") for s in item.get("summary") or []
                                           if isinstance(s, dict) and s.get("text")))
        elif kind == "response.completed":
            completed = True
            usage = (ev.get("response") or {}).get("usage")
        elif kind in ("response.failed", "error"):
            raise StreamError("transport", "the gateway reported a failed response", seen)
    if not completed:
        raise StreamError("transport", "the stream ended before the response completed", seen)
    msg = {"role": "assistant", "content": "".join(text)}
    if calls:
        msg["tool_calls"] = [{"id": it.get("call_id"), "type": "function",
                              "function": {"name": it.get("name"),
                                           "arguments": it.get("arguments") or "{}"}}
                             for it in calls]
    return {"choices": [{"message": msg, "finish_reason": "stop"}], "usage": usage}


def stream_turn(url, key, payload, route, live, mode, turn, tries=6):
    """Send one agent turn as a stream; returns a chat-shaped reply marked _live.

    A stale replica's 400 "Model not found" before the first event is
    refreshed and retried, as chat_with_model_retry does. Any other
    StreamError propagates to call_model.
    """
    for k in range(tries + 1):
        try:
            if route == "responses":
                resp = stream_gpt_turn(url, key, responses_payload(payload), live)
            else:
                raise StreamError("transport", f"unknown stream route {route!r}", 0)
        except StreamError as exc:
            if (exc.kind == "http" and exc.events_seen == 0 and _http_status(exc) == 400
                    and "model not found" in (exc.detail or "").lower() and k < tries):
                refresh_models(url, key)
                print(f"  [{turn:>2}] model not found on this gateway replica "
                      f"(stale model cache); refreshed, retrying {k + 1}/{tries}",
                      file=sys.stderr, flush=True)
                time.sleep(random.uniform(0.5, 2.0))
                continue
            raise
        resp["_live"] = True
        return resp
```

  4d. **`call_model`.** Add the parameter `streaming=None`. Replace the single line `resp = chat_with_model_retry(url, key, payload, turn)` with:

```python
        route = streaming.route if streaming is not None else None
        if route:
            live = LiveTurn(tr, turn)
            try:
                resp = stream_turn(url, key, payload, route, live, mode, turn)
            except StreamError as exc:
                status = _http_status(exc) if exc.kind == "http" else None
                if exc.events_seen == 0 and status is not None and 400 <= status < 500:
                    # The gateway rejects the streamed form: finish this run the old way.
                    streaming.route = None
                    route = None
                    note = (f"**streaming unavailable (HTTP {status}); "
                            "continuing without live view**")
                    tr.write("\n" + note)
                    print(f"  [{turn:>2}] {note.strip('*')}", file=sys.stderr, flush=True)
                    resp = chat_with_model_retry(url, key, payload, turn)
                elif exc.kind == "idle":
                    resp = {"_transient": f"read timeout after {REQUEST_TIMEOUT}s (stream idle)"}
                else:
                    resp = {"_transient": f"stream {exc.kind}: {exc.detail}"}
            finally:
                live.close()
        else:
            resp = chat_with_model_retry(url, key, payload, turn)
```

  Leave everything after it unchanged (`elapsed`, `advances`, `classify_failure`, `log_anomaly`, the timeout lowering and the backoff). `classify_failure` already maps a `_transient` starting with "read timeout" to `timeout` and anything else to `transport`.

  4e. **The agent loop.** Before `for i in range(1, args.max_iters + 1):`, add:

```python
    streaming = TurnStreaming(agent_stream_route(args.model, mode, args.no_stream))
```

  Pass it: `call_model(url, key, payload, mode, tr, anomaly_path, i, streaming)`. Right after `content = msg.get("content") or ""`, add:

```python
        # A streamed turn already wrote its note live (LA §3.2): don't repeat it.
        streamed = isinstance(resp, dict) and bool(resp.get("_live"))
        shown = None if streamed else content
```

  Then use it in each `tr.step`:
  - `tr.step(i, content, "NO_TOOL_ACTION", ...)` becomes `tr.step(i, shown, "NO_TOOL_ACTION", ...)`;
  - native mode: `tr.step(i, shown if c is calls[0] else None, name, cargs, result)`;
  - prompt mode: pass `None if streamed else narration` in its three `tr.step` calls (PARSE_ERROR, finish and the tool step).

- [ ] **Step 5: Run them.** `python -m pytest -q tests/test_engine_live_turns_gpt.py`. Expected: PASS.
  - If `test_reasoning_is_in_the_transcript_before_the_turn_ends` fails only on the blank lines between the summary and `</think>`, keep the code. Correct the test's final assertion to the actual text, which is the summary, `"\n\n"` from `reasoning_summary_part.done`, then `"\n</think>"`.
  - Then run `python -m pytest -q`. Expected: all pass.

- [ ] **Step 6: Commit.** `git add engine/delegate.py tests/test_engine_reasoning.py tests/test_engine_live_turns_gpt.py`, then `git commit -m "feat(engine): stream GPT agent turns through /openai/responses"`.

---

## Task 3: Stream Claude agent turns, with the streamed-cut rule and prompt mode

**Files:**
- Modify: `engine/delegate.py`: `agent_stream_route`, `stream_turn` and `call_model` from Task 2; add `stream_claude_turn` and `complete_tool_action`.
- Create: `tests/test_engine_live_turns_claude.py`

**Interfaces:**
- Consumes (Tasks 1 and 2): `LiveTurn`, `ThinkSplitter`, `FenceHold`, `ArgFieldStream`, `PREVIEW_FIELDS`, `_preview_feed`, `stream_turn`, `TurnStreaming`, `agent_stream_route`.
- Produces:
  - `stream_claude_turn(url, key, payload, live, mode) -> dict`;
  - `complete_tool_action(resp, mode) -> bool`;
  - `agent_stream_route` returns `"chat"` for `anthropic_tool_call.*` in native or prompt mode.

- [ ] **Step 1: Write the failing tests** in `tests/test_engine_live_turns_claude.py`:

```python
"""Live agent turns: Claude through chat streaming (LA §3.1-§3.3)."""
from __future__ import annotations

import json

from test_engine_reasoning import engine, harness  # noqa: F401

CLAUDE = "anthropic_tool_call.claude-x"
USAGE = {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120}


def chunk(content=None, tool_calls=None, finish=None):
    delta = {}
    if content is not None:
        delta["content"] = content
    if tool_calls is not None:
        delta["tool_calls"] = tool_calls
    return {"choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}


def claude_turn(name, args, note="I will look.", call_id="toolu_1", secs=0, index=True,
                raw=None):
    raw = json.dumps(args) if raw is None else raw
    first = {"id": call_id, "type": "function", "function": {"name": name, "arguments": ""}}
    if index:
        first["index"] = 0
    events = [chunk("<think>"), chunk("</think>"), chunk("\n\n"), chunk(note),
              chunk(tool_calls=[first])]
    for k in range(0, len(raw), 7):
        piece = {"function": {"arguments": raw[k:k + 7]}}
        if index:
            piece["index"] = 0
        events.append(chunk(tool_calls=[piece]))
    if secs:
        events.append(secs)
    events += [chunk(finish="stop"), {"choices": [], "usage": USAGE}, engine_done()]
    return events


def engine_done():
    return {"_done": True}


def finish_turn():
    return claude_turn("finish", {"summary": "all done"}, note="")


def test_note_streams_and_no_empty_thinking_section(harness):
    seen = []
    turn = claude_turn("list_dir", {"path": "."})
    turn.insert(4, lambda: seen.append(harness.transcript_path.read_text(encoding="utf-8")))
    text = harness.run("-m", CLAUDE, replies=[], stream_replies=[turn, finish_turn()])
    assert "### [1] thinking\n\nI will look." in seen[0]
    assert text.count("I will look.") == 1
    assert "<think>" not in text
    assert "### [2] thinking" not in text          # the finish turn had no note
    assert harness.stream_calls[0][0] == "/api/chat/completions"
    assert harness.stream_calls[0][1]["stream"] is True


def test_tool_calls_are_assembled_from_pieces(harness):
    text = harness.run("-m", CLAUDE, replies=[],
                       stream_replies=[claude_turn("list_dir", {"path": "."}), finish_turn()])
    assert '### [1] list_dir({"path": "."})' in text
    second = harness.stream_calls[1][1]["messages"]
    assert any(m.get("tool_call_id") == "toolu_1" for m in second)


def test_missing_index_is_treated_as_zero(harness):
    text = harness.run("-m", CLAUDE, replies=[],
                       stream_replies=[claude_turn("list_dir", {"path": "."}, index=False),
                                       finish_turn()])
    assert '### [1] list_dir({"path": "."})' in text


def test_cut_stream_gets_the_smaller_step_correction(harness, engine):
    cut = claude_turn("write_file", {"path": "a.py", "content": "x" * 200}, secs=121,
                      raw='{"path": "a.py", "content": "xxxxxxxx')
    text = harness.run("-m", CLAUDE, "--write", replies=[], stream_replies=[cut, finish_turn()])
    assert "reply cut by the gateway's 2-minute limit after 121 s; asking for a smaller step" in text
    assert harness.stream_calls[1][1]["messages"][-1]["content"] == engine.CUT_CORRECTION
    assert "_[ 1] composing write_file a.py ..._\n> xxxxxxxx" in text


def test_slow_but_complete_reply_is_a_normal_step(harness):
    slow = claude_turn("list_dir", {"path": "."}, secs=121)
    text = harness.run("-m", CLAUDE, replies=[], stream_replies=[slow, finish_turn()])
    assert "reply cut" not in text and '### [1] list_dir({"path": "."})' in text


def test_quick_complete_reply_is_a_normal_step(harness):
    quick = claude_turn("list_dir", {"path": "."}, secs=60)
    text = harness.run("-m", CLAUDE, replies=[], stream_replies=[quick, finish_turn()])
    assert "reply cut" not in text


def test_prose_only_is_nudged_without_writing_the_note_twice(harness):
    prose = [chunk("I think the answer is 4."), chunk(finish="stop"), engine_done()]
    text = harness.run("-m", CLAUDE, replies=[], stream_replies=[prose, finish_turn()])
    assert "reply had no tool call; asking for one" in text
    assert text.count("I think the answer is 4.") == 1


def test_stream_without_done_is_retried(harness):
    broken = claude_turn("list_dir", {"path": "."})[:-1]          # no [DONE]
    harness.run("-m", CLAUDE, replies=[], stream_replies=[broken, finish_turn()])
    assert len(harness.stream_calls) == 2


def test_prompt_mode_streams_the_note_and_holds_the_tool_block(harness):
    block = '```tool\n{"name": "list_dir", "args": {"path": "."}}\n```'
    turn = [chunk("Listing it.\n"), chunk(block[:5]), chunk(block[5:]),
            chunk(finish="stop"), engine_done()]
    fin = [chunk('```tool\n{"name": "finish", "args": {"summary": "ok"}}\n```'),
           chunk(finish="stop"), engine_done()]
    text = harness.run("-m", CLAUDE, "--tool-mode", "prompt", replies=[],
                       stream_replies=[turn, fin])
    assert "### [1] thinking\n\nListing it." in text
    assert '"name": "list_dir"' not in text.split('### [1] list_dir(')[0].split("## Task")[-1]
    assert text.count("Listing it.") == 1


def test_complete_tool_action(engine):
    ok = {"choices": [{"message": {"tool_calls": [
        {"function": {"name": "x", "arguments": '{"a": 1}'}}]}}]}
    bad = {"choices": [{"message": {"tool_calls": [
        {"function": {"name": "x", "arguments": '{"a": '}}]}}]}
    none = {"choices": [{"message": {"content": "hi"}}]}
    assert engine.complete_tool_action(ok, "native")
    assert not engine.complete_tool_action(bad, "native")
    assert not engine.complete_tool_action(none, "native")
    blk = {"choices": [{"message": {"content": '```tool\n{"name": "x", "args": {}}\n```'}}]}
    assert engine.complete_tool_action(blk, "prompt")
    assert not engine.complete_tool_action(none, "prompt")


def test_claude_routes(engine):
    assert engine.agent_stream_route(CLAUDE, "native", False) == "chat"
    assert engine.agent_stream_route(CLAUDE, "prompt", False) == "chat"
    assert engine.agent_stream_route(CLAUDE, "native", True) is None
```

  Note: in `test_prompt_mode_streams_the_note_and_holds_the_tool_block`, the tool block is always written as the step's header, so the second assertion only checks the live section before the step. If that split proves brittle, assert instead that the text between `### [1] thinking` and `### [1] list_dir(` contains no "```tool".

- [ ] **Step 2: Run them.** `python -m pytest -q tests/test_engine_live_turns_claude.py`. Expected: FAIL (`agent_stream_route(CLAUDE, ...)` returns None).

- [ ] **Step 3: Implement.**

  3a. In `agent_stream_route`, before `return None`:

```python
    if model.startswith(CLAUDE_PREFIX) and mode in ("native", "prompt"):
        return "chat"
```

  3b. After `stream_gpt_turn`:

```python
def stream_claude_turn(url, key, payload, live, mode):
    """One Claude agent turn through chat streaming, written live (LA §3.1).

    Content goes through ThinkSplitter (Claude's <think></think> arrives
    empty) and, in prompt mode, through FenceHold so the tool block is not
    written live. Tool calls are assembled by index (missing index = 0).
    finish_reason is "stop" even for a complete tool call, so the caller
    judges a cut by complete_tool_action and elapsed time.
    """
    split, hold = ThinkSplitter(), (FenceHold() if mode == "prompt" else None)
    content, slots, previews = [], {}, {}
    usage, done, seen, finish = None, False, 0, None

    def show(pieces):
        for kind, text in pieces:
            if kind == "reasoning":
                live.reasoning(text)
            else:
                live.note(hold.feed(text) if hold else text)

    for ev in stream_events(url, key, "/api/chat/completions", payload,
                            idle_timeout=REQUEST_TIMEOUT):
        if ev.get("_done"):
            done = True
            continue
        seen += 1
        if isinstance(ev.get("usage"), dict):
            usage = ev["usage"]
        for ch in (ev.get("choices") or [])[:1]:
            finish = ch.get("finish_reason") or finish
            delta = ch.get("delta") or {}
            piece = delta.get("content")
            if isinstance(piece, str) and piece:
                content.append(piece)
                show(split.feed(piece))
            for tc in delta.get("tool_calls") or []:
                idx = tc.get("index") if isinstance(tc.get("index"), int) else 0
                slot = slots.setdefault(idx, {"id": None, "name": None, "args": []})
                fn = tc.get("function") or {}
                if tc.get("id"):
                    slot["id"] = tc["id"]
                if fn.get("name") and not slot["name"]:
                    slot["name"] = fn["name"]
                    if fn["name"] in PREVIEW_FIELDS:
                        previews[idx] = (fn["name"], ArgFieldStream(PREVIEW_FIELDS[fn["name"]]))
                args = fn.get("arguments")
                if args:
                    slot["args"].append(args)
                    if idx in previews:
                        _preview_feed(live, previews[idx], args)
    show(split.flush())
    if hold:
        live.note(hold.flush())
    if not done:
        raise StreamError("transport", "the stream ended before [DONE]", seen)
    msg = {"role": "assistant", "content": "".join(content)}
    if slots:
        msg["tool_calls"] = [{"id": s["id"] or f"call_{k}", "type": "function",
                              "function": {"name": s["name"], "arguments": "".join(s["args"])}}
                             for k, s in sorted(slots.items())]
    return {"choices": [{"message": msg, "finish_reason": finish or "stop"}], "usage": usage}


def complete_tool_action(resp, mode):
    """True when a reply carries a whole tool action: native tool calls whose
    arguments parse to objects, or a prompt-mode tool block that parses."""
    choices = resp.get("choices") if isinstance(resp, dict) else None
    msg = ((choices[0] or {}).get("message") or {}) if choices else {}
    if mode == "native":
        calls = msg.get("tool_calls") or []
        if not calls:
            return False
        for c in calls:
            try:
                if not isinstance(json.loads((c.get("function") or {}).get("arguments") or ""),
                                  dict):
                    return False
            except ValueError:
                return False
        return True
    m = TOOL_BLOCK.search(msg.get("content") or "")
    if not m:
        return False
    try:
        return isinstance(json.loads(m.group(1)), dict)
    except ValueError:
        return False
```

  3c. In `stream_turn`, replace the `raise StreamError("transport", f"unknown stream route ...")` branch with:

```python
            elif route == "chat":
                resp = stream_claude_turn(url, key, {**payload, "stream": True}, live, mode)
            else:
                raise StreamError("transport", f"unknown stream route {route!r}", 0)
```

  (The `if route == "responses":` line stays first.)

  3d. **The cut rule** in `call_model`. Immediately after `elapsed = _now() - started`, and before `if advances(resp, mode):`, add:

```python
        if (route == "chat" and elapsed >= REPLY_CUT_SECONDS and isinstance(resp, dict)
                and "_transient" not in resp and not complete_tool_action(resp, mode)):
            # A streamed Claude reply ends like a complete one when the gateway
            # cuts it at ~121 s; only the unfinished tool action shows it (LA §3.3).
            cause = "reply_cut"
            log_anomaly(anomaly_path, turn, attempt, mode, payload, resp,
                        cause=cause, elapsed=elapsed)
            print(f"  [{turn:>2}] anomaly: reply carried no tool action "
                  f"(attempt {attempt}/4)", file=sys.stderr, flush=True)
            return resp, False, cause, elapsed
```

  `route` here is the local from Task 2's branch; it is `None` after a fallback, so a non-streamed reply keeps today's bare-"Error:" rule.

- [ ] **Step 4: Run them.** `python -m pytest -q tests/test_engine_live_turns_claude.py`. Expected: PASS. Then `python -m pytest -q`. Expected: all pass.

- [ ] **Step 5: Commit.** `git add engine/delegate.py tests/test_engine_live_turns_claude.py`, then `git commit -m "feat(engine): stream Claude agent turns; detect streamed cuts by the unfinished tool call"`.

---

## Task 4: Narration learns the preview event and the streaming notice

**Files:**
- Modify: `src/agent_control/narration.py`:
  - patterns near line 70: add `_COMPOSING`;
  - `_NOTICE_PATTERNS` near line 87: add the notice;
  - `_classify_start` near line 281;
  - `_build_events` near line 362.
- Test: `tests/test_narration.py`, `tests/test_progress.py` and `tests/test_activity.py` (append tests).

**Interfaces:**
- Produces: narration events of kind `"preview"`, with `iteration` and data `{"tool", "path", "text", "live"}` (Global constraints).

- [ ] **Step 1: Write the failing tests.** Append to `tests/test_narration.py`:

```python
from agent_control.narration import parse_narration as _pn

_PREVIEW_RUN = (
    "# run\n\n## Task\n\ndo it\n\n---\n"
    "\n_[ 3] calling m (attempt 1/4, 9 msgs, ~100 bytes) ..._\n"
    "\n### [3] thinking\n\nWriting the notes.\n"
    "\n_[ 3] composing write_file docs/notes.md ..._\n"
    "> ### [9] thinking\n>\n> ---\n> ## Final report\n"
    "> _[ 4] reply cut by the gateway's 2-minute limit after 121 s; asking for a smaller step_\n"
    "> [m e=x tools=native: 3 tool calls]\n"
)


def _kinds(n):
    return [e.kind for e in n.events]


def test_preview_is_one_live_event_when_last():
    n = _pn(_PREVIEW_RUN)
    assert _kinds(n)[-1] == "preview"
    ev = n.events[-1]
    assert ev.iteration == 3
    assert ev.data == {"tool": "write_file", "path": "docs/notes.md", "live": True,
                       "text": "### [9] thinking\n\n---\n## Final report\n"
                               "_[ 4] reply cut by the gateway's 2-minute limit after 121 s; "
                               "asking for a smaller step_\n[m e=x tools=native: 3 tool calls]"}
    assert "final" not in _kinds(n) and "status" not in _kinds(n)


def test_preview_is_not_live_once_the_step_follows():
    text = _PREVIEW_RUN + '\n### [3] write_file({"path": "docs/notes.md"})\n```\nwrote 6 lines\n```\n'
    n = _pn(text)
    pv = [e for e in n.events if e.kind == "preview"][0]
    assert pv.data["live"] is False
    assert _kinds(n)[-1] == "tool"


def test_preview_without_path():
    n = _pn("# r\n\n## Task\n\nx\n\n---\n\n_[12] composing finish ..._\n> all done\n")
    ev = n.events[-1]
    assert ev.kind == "preview" and ev.data["path"] is None and ev.data["tool"] == "finish"
    assert ev.data["text"] == "all done"


def test_streaming_notice_parses():
    n = _pn("# r\n\n## Task\n\nx\n\n---\n\n"
            "**streaming unavailable (HTTP 400); continuing without live view**\n")
    assert n.events[-1].kind == "notice"
    assert n.events[-1].data["text"] == ("streaming unavailable (HTTP 400); "
                                         "continuing without live view")
```

  Append to `tests/test_progress.py`:

```python
def test_preview_lines_change_no_counts():
    from agent_control.progress import parse_progress
    base = ("# r\n\n## Task\n\nx\n\n---\n\n_[ 3] calling m (attempt 1/4, 9 msgs, ~1 bytes) ..._\n")
    quoted = (base + "\n_[ 3] composing write_file t.md ..._\n"
              "> _[ 3] calling m (attempt 3/4, 9 msgs, ~1 bytes) ..._\n"
              "> _[ 3] reply cut by the gateway's 2-minute limit after 121 s; asking for a smaller step_\n"
              "> ### [3] read_file({\"path\": \"x\"})\n"
              "> [m e=x tools=native: 3 tool calls]\n")
    a, b = parse_progress(base), parse_progress(quoted)
    assert (b.phase, b.attempt, b.cuts, b.last_tool) == (a.phase, a.attempt, a.cuts, a.last_tool)
    assert b.phase == "model_call" and b.attempt == 1
```

  Append to `tests/test_activity.py`:

```python
def test_preview_lines_are_not_steps():
    from agent_control.activity import parse_activity
    text = ("# r\n\n## Task\n\nx\n\n---\n\n### [3] thinking\n\nnote\n"
            "\n_[ 3] composing write_file t.md ..._\n> ### [7] read_file({\"path\": \"x\"})\n"
            "\n### [3] write_file({\"path\": \"t.md\"})\n```\nok\n```\n")
    steps = parse_activity(text)
    assert [s.iteration for s in steps] == [3, 3]
    assert all("read_file" not in repr(s) for s in steps)
```

  If `parse_narration` or `parse_progress` needs the transcript header shape used by the existing fixtures in these files, reuse that header. Keep the body lines exactly as written here. If `ActivityStep`'s fields differ from `iteration`, read `src/agent_control/activity.py` lines 41–118 once and use its field names. The intent is fixed: the preview contributes no step.

- [ ] **Step 2: Run them.** `python -m pytest -q tests/test_narration.py tests/test_progress.py tests/test_activity.py`. Expected: the narration tests FAIL; the progress and activity tests already PASS, because those patterns are anchored. Keep the passing tests: they pin the behaviour.

- [ ] **Step 3: Implement** in `narration.py`. After `_STATUS`:

```python
# Live agent turns (2026-10-03): a tool's text previewed while it streams. The
# engine prefixes every preview line with "> " (or writes a bare ">"), so no
# line inside can start an element.
_COMPOSING = re.compile(
    r"^_\[\s*(\d+)\]\s+composing\s+([A-Za-z_][\w-]*)(?:\s+(.+?))?\s+\.\.\._$"
)
```

  In `_NOTICE_PATTERNS`, add:

```python
    re.compile(
        r"^\*\*streaming unavailable \(HTTP \d{3}\); "
        r"continuing without live view\*\*$"
    ),
```

  In `_classify_start`, before `if _STATUS.match(line):`:

```python
    if _COMPOSING.match(line):
        return "preview"
```

  In `_build_events`, add a branch beside the others:

```python
        elif tag == "preview":
            m = _COMPOSING.match(line)
            text_lines: list[str] = []
            for ln in body_lines:
                if ln == ">":
                    text_lines.append("")
                elif ln.startswith("> "):
                    text_lines.append(ln[2:])
                else:
                    break
            events.append(_make_event(seq, "preview", int(m.group(1)), {
                "tool": m.group(2),
                "path": m.group(3),
                "text": "\n".join(text_lines),
                "live": pos == len(elements) - 1,
            }))
            seq += 1
```

  The footer is itself an element, so a finished run's preview is never last, and never live.

- [ ] **Step 4: Run them.** `python -m pytest -q tests/test_narration.py tests/test_progress.py tests/test_activity.py tests/test_narration_api.py`. Expected: PASS. Then `python -m pytest -q`. Expected: all pass.

- [ ] **Step 5: Commit.** `git add src/agent_control/narration.py tests/test_narration.py tests/test_progress.py tests/test_activity.py`, then `git commit -m "feat(narration): preview events for text streamed into a tool call"`.

---

## Task 5: The panel shows a live preview

**Files:**
- Modify: `web/panel.js`:
  - the exported helpers near `statusText` (line 159);
  - `renderEvent`'s switch (around line 436).
- Modify: `web/panel.css`.
- Test: `tests/js/panel.test.mjs`.

**Interfaces:**
- Consumes (Task 4): `preview` events.
- Produces: the exported `previewHeading(event) -> string` and `previewVisible(event) -> boolean`.

- [ ] **Step 1: Write the failing tests.** Append to `tests/js/panel.test.mjs`, importing the two names from `../../web/panel.js` beside the existing imports:

```js
test("previewHeading names the file or the final report", () => {
  assert.equal(previewHeading({ tool: "write_file", path: "src/app.py" }), "Writing src/app.py");
  assert.equal(previewHeading({ tool: "replace_in_file", path: "a.js" }), "Editing a.js");
  assert.equal(previewHeading({ tool: "finish", path: null }), "Final report");
  assert.equal(previewHeading({ tool: "write_file", path: null }), "Writing a file");
});

test("previewVisible only while the preview is the live element", () => {
  assert.equal(previewVisible({ kind: "preview", live: true }), true);
  assert.equal(previewVisible({ kind: "preview", live: false }), false);
});
```

  Then add one DOM-level test in the style of the file's existing `createRunPanel` tests, which use a fake `fetchJson` and a fake document. Read the first existing test that renders events, and copy its setup.
  1. Feed one poll whose events end with `{seq: 5, kind: "preview", iteration: 3, tool: "write_file", path: "a.py", text: "line 1\nline 2", live: true}` while the run is in `data.live`. Assert:
     - the timeline contains an element with class `rp-preview`;
     - its heading text is `Writing a.py (writing…)`;
     - its `pre` text is `line 1\nline 2`.
  2. A second poll returns the same preview with `live: false`, followed by a `tool` event. Assert that no `rp-preview` element remains.
  3. **Review focus 5:** a run that leaves `data.live` while its preview is still `live: true` keeps the `rp-preview` element. Its heading becomes `Writing a.py`, without "(writing…)".

- [ ] **Step 2: Run them.** `node --test tests/js/dashboard.test.mjs tests/js/panel.test.mjs`. Expected: FAIL (the exports are missing).

- [ ] **Step 3: Implement.** Near `statusText`:

```js
export function previewHeading(event) {
  const path = event && event.path ? String(event.path) : "";
  switch (event && event.tool) {
    case "finish":
      return "Final report";
    case "replace_in_file":
      return path ? `Editing ${path}` : "Editing a file";
    default:
      return path ? `Writing ${path}` : "Writing a file";
  }
}

export function previewVisible(event) {
  return Boolean(event && event.kind === "preview" && event.live);
}
```

  In `renderEvent`'s switch:

```js
      case "preview": {
        if (!previewVisible(event)) return null;
        const div = el("div", { class: "rp-preview" });
        const h = el("div", { class: "rp-preview-heading" });
        h.textContent = liveLabel(previewHeading(event), event, live);
        const pre = el("pre", { class: "rp-text rp-preview-text" });
        pre.textContent = event.text || "";
        div.append(h, pre);
        // Keep the newest lines in view once the node is in the document.
        queueMicrotask(() => { pre.scrollTop = pre.scrollHeight; });
        return div;
      }
```

  If the test's fake DOM lacks `queueMicrotask`, guard it with `if (typeof queueMicrotask === "function")`.

  In `web/panel.css`:

```css
.rp-preview { margin: 0.4em 0; }
.rp-preview-heading { font-weight: 600; }
.rp-preview-text { max-height: 18em; overflow: auto; }
```

- [ ] **Step 4: Run them.** `node --test tests/js/dashboard.test.mjs tests/js/panel.test.mjs`. Expected: PASS.

- [ ] **Step 5: Commit.** `git add web/panel.js web/panel.css tests/js/panel.test.mjs`, then `git commit -m "feat(panel): show a tool's text while it is being written"`.

---

## Task 6: agent_await.py: anchored heartbeat, wider search (orchestrator)

`~/.claude/bin/agent_await.py` is not in the repo. The orchestrator edits it, with a backup `agent_await.py.backup-<UTC stamp>`. LA §3.6.

- [ ] **Step 1: Extend the end-to-end check.** In `scratchpad/check_await_e2e.py`, add two synthetic cases. Both are expected to exit 5 (overran with `--max-mins 0.01`), not 4, and neither may crash:
  - **"heartbeat behind a 20 KB preview":** a heartbeat line, then `_[ 3] composing write_file a.md ..._` and 400 lines of `> ` text, with a fresh mtime;
  - **"preview quoting a retry":** a heartbeat at attempt 1, then a preview containing `> _[ 3] calling m (attempt 3/4, 9 msgs, ~1 bytes) ..._`.

  Run it. Expected: the second case reports exit 4 (BAD) before the fix.
- [ ] **Step 2: Fix.**
  - Set `CALLING = re.compile(r"^_\[\s*\d+\]\s+calling\s+(\S+)\s+\(attempt\s+(\d+)/(\d+)")`.
  - Use `CALLING.match(line)` in the main loop instead of `.search`.
  - Read the heartbeat search from `tail(path, 65536)`, while `is_done` keeps using `tail(path)`.
  - Grep the file for every other use of `CALLING` before saving.
- [ ] **Step 3: Rerun the end-to-end check.** Expected: all six cases `ok`. Run `python -m py_compile` on the file too.

---

## Task 7: Operator documentation (orchestrator)

- [ ] **Step 1:** In `docs/operator-guide.md`, extend the section "One-shot calls stream":
  - agent turns stream too: GPT through Responses with reasoning, and Claude through chat;
  - the preview of file text;
  - the notice and the fallback;
  - `--no-stream`.
- [ ] **Step 2:** In `README.md`, add one line to Features.
- [ ] **Step 3:** Commit: `docs: live agent turns`.

---

## Task 8: Final review, deploy and live checks (operator approval required)

- [ ] **Step 1: Review.** Run one review agent over the whole branch diff (`git diff master...feat/live-agent-turns`) against LA and this plan's Review focus. Fix Critical and Important findings with a failing test first.
- [ ] **Step 2: Suites.** Run both suites and read their exit codes.
- [ ] **Step 3: Ask the operator to approve the deploy.**
- [ ] **Step 4: Deploy.**
  - Merge into master (fast-forward).
  - Run `python scripts/deploy_engine.py --apply`.
  - Restart the dashboard.
  - Update the rule files: `DELEGATION.md`, the agent-harness skill, and the Codex, Kilo and Cline copies (memory `delegation-rule-copies`). The line "One-shot calls now stream" gains "and so do agent turns".
- [ ] **Step 5: Live checks** (LA §5):
  - a `gpt-6.1-sol -e high` agent run on a small task shows reasoning before its first step;
  - a Claude agent run writing a 100-line file shows "Writing …" growing;
  - `agent_await.py` watches both and reports them finished.

---

## Execution

The operator's standing method: each task is one agent on the OpenWebUI harness, and the orchestrator reviews, applies and commits.

- **Launch** each agent task in its own worktree, from the feature branch's worktree, with a watcher beside it:
  ```
  timeout 2700 python -u ~/.claude/bin/delegate.py -C "D:/repos/AgentControl/.claude/worktrees/live-agent-turns" --worktree -m gpt-6.1-sol -e high --label <label> --max-iters 40 -f <brief>
  python -u ~/.claude/bin/agent_await.py --repo "D:/repos/AgentControl/.claude/worktrees/live-agent-turns" --label <label>
  ```
- **The brief** is the task's section, the Global constraints and the Review focus lines for that task. The first action is the task's Step 1.
- **The executor** is `gpt-6.1-sol -e high`. It has no 2-minute reply cut.
- **Waves:**

  | Wave | Tasks |
  |---|---|
  | 1, in parallel | Tasks 1, 4 and 5 (disjoint files), plus Task 6 by the orchestrator |
  | 2 | Task 2 |
  | 3 | Task 3 |
  | 4 | Task 7, then Task 8 |

- **After each task:**
  1. apply the agent's patch to the feature worktree (`git apply --check`, then `git apply`);
  2. run both suites, reading each exit code;
  3. read the diff against the task's steps;
  4. commit with the task's message, ending with the repo's Co-Authored-By line.

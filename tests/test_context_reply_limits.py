"""Reply headroom vs requested output bound on the ACTUAL wire body; synthetic, offline."""
import json
from pathlib import Path

import pytest

from test_engine_reasoning import engine, harness, _finish_reply  # noqa: F401


@pytest.fixture()
def main_wire(engine, harness, tmp_path, monkeypatch):
    import io
    import sys

    monkeypatch.syspath_prepend(str(Path(engine.__file__).parent))
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.delenv("_HARNESS_APPROVED_WORKER", raising=False)
    # Reuse setup only: harness.run replaces transports, so call main directly.
    assert engine.chat.__name__ == "chat" and engine.stream_events.__name__ == "stream_events"
    ct = engine.capability_module("context_tokens")
    measured, mutations, sent = [], [], []
    estimate, preflight = ct.ContextMeter.estimate, engine.context_preflight
    explicit = [None]

    def meter(self, payload):
        measured.append((payload, json.loads(json.dumps(payload))))
        return estimate(self, payload)

    def check(payload):
        field = FIELD["responses" if "input" in payload else "chat"]
        if explicit[0] is not None:
            payload.setdefault(field, explicit[0])
        before = dict(payload)
        preflight(payload)
        mutations.append((payload, before, dict(payload)))

    def urlopen(req, timeout=None):
        body = json.loads(req.data)
        sent.append((req.full_url, req.data, body))
        assert any(p is mutations[-1][0] and snap == body for p, snap in measured)
        assert mutations[-1][2] == body
        oneshot, streamed = not body.get("tools"), body.get("stream", False)
        usage = {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120}
        reply = {"choices": [{"message": {"content": "synthetic done"},
                                "finish_reason": "stop"}], "usage": usage}
        if not oneshot:
            reply = {**_finish_reply("native"), "usage": usage}
        if not streamed:
            return io.BytesIO(json.dumps(reply).encode())
        if "input" in body:
            item = {"type": "function_call", "id": "fc_s", "call_id": "call_s",
                    "name": "finish", "arguments": json.dumps({"summary": "done"})}
            events = ([{"type": "response.output_text.delta", "delta": "synthetic done"}]
                      if oneshot else [{"type": "response.output_item.done", "item": item}])
            events += [{"type": "response.completed", "response": {
                "usage": {"input_tokens": 100, "output_tokens": 20, "total_tokens": 120}}}]
        else:
            msg = reply["choices"][0]["message"]
            if not oneshot:
                msg["tool_calls"][0]["index"] = 0
            events = [{"choices": [{"index": 0, "delta": msg, "finish_reason": None}]},
                      {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
                      {"choices": [], "usage": usage}]
        return io.BytesIO(("".join("data: " + json.dumps(e) + "\n\n" for e in events)
                           + "data: [DONE]\n\n").encode())

    monkeypatch.setattr(ct.ContextMeter, "estimate", meter)
    monkeypatch.setattr(engine, "context_preflight", check)
    monkeypatch.setattr(engine.urllib.request, "urlopen", urlopen)

    def run(model, oneshot, stream, bound=None, *extra):
        explicit[0] = bound
        argv = ["delegate.py", "-t", "synthetic task", "-C", str(tmp_path),
                "--transcript", str(harness.transcript_path), "-m", model, "-e", "high",
                "--context-window-tokens", "100000", "--context-reply-tokens", "4096",
                "--context-safety-tokens", "1000", *extra]
        argv += ["--oneshot"] if oneshot else []
        argv += [] if stream else ["--no-stream"]
        monkeypatch.setattr(sys, "argv", argv)
        try:
            engine.main()
        except SystemExit as exc:
            assert exc.code == 0
        return sent, measured, mutations
    return run


@pytest.mark.parametrize("oneshot", [False, True], ids=["worker", "oneshot"])
@pytest.mark.parametrize("model,stream,route", [
    ("anthropic_tool_call.claude-x", True, "chat"), ("gpt-6-sol", True, "responses"),
    ("anthropic_tool_call.claude-x", False, "chat")])
@pytest.mark.parametrize("bound", [None, 137], ids=["configured", "stricter"])
def test_main_reply_bound_on_real_wire(main_wire, model, stream, route, oneshot, bound):
    sent, measured, mutations = main_wire(model, oneshot, stream, bound)
    assert len(sent) == 1
    url, raw, body = sent[0]
    assert url == "http://gw" + ("/openai/responses" if route == "responses" else "/api/chat/completions")
    assert body[FIELD[route]] == (4096 if bound is None else bound)
    assert body["model"] == model and body["stream"] is stream
    assert body.get("reasoning", {}).get("effort", body.get("reasoning_effort")) == "high"
    assert any(p is mutations[-1][0] and snapshot == body for p, snapshot in measured)
    assert any(all(field not in before for field in FIELD.values())
               and after.get(FIELD["responses" if "input" in after else "chat"]) == 4096
               for _, before, after in mutations) if bound is None else all(
                   after[FIELD["responses" if "input" in after else "chat"]] == bound
                   for _, _, after in mutations)


@pytest.mark.parametrize("reply", [4096, 16384])
@pytest.mark.parametrize("model,stream,route", [
    ("anthropic_tool_call.claude-x", True, "chat"), ("gpt-6-sol", True, "responses"),
    ("anthropic_tool_call.claude-x", False, "chat")])
def test_main_maintenance_bound_and_identity(main_wire, engine, monkeypatch,
                                             reply, model, stream, route):
    from types import SimpleNamespace

    ce = engine.capability_module("context_engine")
    original = ce.AutomaticCompaction.__init__
    trials = []

    def initialize(self, *args, **kwargs):
        original(self, *args, **kwargs)
        prepare = self.prepare

        def capture(messages, bound):
            packet = prepare(messages, bound)
            trials.append(packet)
            return packet

        self.prepare = capture
        messages = [{"role": "user", "content": "synthetic maintenance"}]
        with ce.runtime_scope(self.rt):
            self.measure({"messages": messages})
            packet = trials[-1]
            assert self.rt.policy.verified_output_limit is None
            response = self.call(SimpleNamespace(
                model=packet["model"], route=route, effort="high", messages=messages,
                reply_tokens=packet[FIELD[route]]))
            assert response["choices"][0]["message"]["content"] == "synthetic done"

    monkeypatch.setattr(ce.AutomaticCompaction, "__init__", initialize)
    sent, measured, mutations = main_wire(
        model, False, stream, None, "--auto-compact", "--context-reply-tokens", str(reply))
    assert len(sent) == 2 and len(trials) == 1
    trial = trials[0]
    assert trial[FIELD[route]] == min(reply, 8192)
    for index, cap in enumerate((min(reply, 8192), reply)):
        url, raw, body = sent[index]
        assert url == "http://gw" + ("/openai/responses" if route == "responses"
                                   else "/api/chat/completions")
        assert body["model"] == model and body["stream"] is stream
        assert body[FIELD[route]] == cap
        assert body.get("reasoning", {}).get("effort", body.get("reasoning_effort")) == "high"
        assert any(snapshot == body for _, snapshot in measured)
    assert sent[0][2] == trial
    assert "tools" not in trial and "tool_choice" not in trial

from test_engine_turn_budget import GPT


class _Stop(Exception):
    pass


@pytest.fixture()
def wire(engine, monkeypatch):
    monkeypatch.syspath_prepend(str(Path(engine.__file__).parent))
    sent = []

    def urlopen(req, timeout=None):
        sent.append(req.data)
        raise _Stop()
    monkeypatch.setattr(engine.urllib.request, "urlopen", urlopen)
    return sent


def _send(engine, payload, route, policy=None):
    ce = engine.capability_module("context_engine")
    ct = engine.capability_module("context_tokens")
    ctx = None if policy is None else ce.Scope(policy, ct.ContextMeter(policy, model=GPT, route=route))
    try:
        if ctx is None:
            engine.chat("http://synthetic", "fake", payload)
        else:
            with ce.scope(ctx):
                if route == "responses":
                    list(engine.stream_events("http://synthetic", "fake", "/openai/responses", payload))
                else:
                    engine.chat("http://synthetic", "fake", payload)
    except Exception:  # _Stop or a refusal; the captured wire list is the evidence
        pass
    return ctx


def _policy(engine):
    return engine.capability_module("context_policy").Policy(100000, 4096, 1000)


CHAT = {"model": GPT, "messages": [{"role": "user", "content": "synthetic"}]}
RESP = {"model": GPT, "input": [{"role": "user", "content": "synthetic"}], "stream": True}
FIELD = {"chat": "max_completion_tokens", "responses": "max_output_tokens"}


@pytest.mark.parametrize("route,base", [("chat", CHAT), ("responses", RESP)])
def test_wire_bound_matches_reply_headroom_and_is_measured(engine, wire, route, base):
    policy = _policy(engine)
    ctx = _send(engine, dict(base), route, policy)
    assert len(wire) == 1
    assert json.loads(wire[0])[FIELD[route]] == policy.reply_tokens
    assert ctx.last_estimate is not None  # preflight measured before dispatch


@pytest.mark.parametrize("route,base", [("chat", CHAT), ("responses", RESP)])
def test_stricter_explicit_bound_survives(engine, wire, route, base):
    _send(engine, {**base, FIELD[route]: 100}, route, _policy(engine))
    assert json.loads(wire[0])[FIELD[route]] == 100


@pytest.mark.parametrize("bad", [0, -1, True, "100", 4097])
@pytest.mark.parametrize("route,base", [("chat", CHAT), ("responses", RESP)])
def test_invalid_or_wider_explicit_bound_zero_rpc(engine, wire, route, base, bad):
    _send(engine, {**base, FIELD[route]: bad}, route, _policy(engine))
    assert wire == []


def test_legacy_body_byte_exact(engine, wire):
    payload = dict(CHAT)
    before = json.dumps(payload)
    _send(engine, payload, "chat")
    assert wire[0] == before.encode() and json.dumps(payload) == before


CLAUDE_KNOWN = "anthropic_tool_call.claude-opus-5-5-think"


def test_unverified_claude_mapping_is_not_adopted_after_failed_live_prototype(engine, wire):
    payload = {"model": CLAUDE_KNOWN, "messages": [{"role": "user", "content": "synthetic"}]}
    _send(engine, payload, "chat", _policy(engine))
    assert len(wire) == 1
    body = json.loads(wire[0])
    assert body["max_completion_tokens"] == 4096
    assert "max_tokens" not in body and "max_output_tokens" not in body


@pytest.mark.parametrize("extra", [
    {"max_tokens": 128}, {"max_output_tokens": 128},
    {"max_tokens": 128, "max_completion_tokens": 128},
])
def test_wrong_or_multiple_known_claude_bounds_execute_nothing(engine, wire, extra):
    payload = {"model": CLAUDE_KNOWN, "messages": [], **extra}
    _send(engine, payload, "chat", _policy(engine))
    assert wire == []


def test_known_claude_legacy_payload_still_has_no_injected_cap(engine, wire):
    payload = {"model": CLAUDE_KNOWN, "messages": []}
    before = json.dumps(payload).encode()
    _send(engine, payload, "chat")
    assert wire == [before]

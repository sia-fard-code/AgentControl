"""Automatic compaction through the source loop, fake gateway and small injected window."""
import json
from pathlib import Path

import pytest

from test_engine_reasoning import engine, harness  # noqa: F401
from test_engine_turn_budget import GPT, _call, _run, _usage


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch, engine):
    monkeypatch.syspath_prepend(str(Path(engine.__file__).parent))
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.delenv("_HARNESS_APPROVED_WORKER", raising=False)


def inject_counter(engine, monkeypatch):
    tokens = engine.capability_module("context_tokens")
    original = tokens.ContextMeter.__init__

    def count(packet):
        messages = packet.get("messages", packet.get("input", []))
        text = json.dumps(messages, default=engine._wire_default)
        if "UNTRUSTED COMPACTED MEMORY" in text:
            return 250
        if "memory formatter" in text:
            return 200
        results = sum(m.get("role") == "tool" or m.get("type") == "function_call_output"
                      or str(m.get("content", "")).startswith("Result of ") for m in messages)
        return 700 if results >= 2 else 200

    def init(self, *args, **kwargs):
        kwargs["tokenizer"] = count
        original(self, *args, **kwargs)

    monkeypatch.setattr(tokens, "INITIAL_MARGIN", 1.0)
    monkeypatch.setattr(tokens, "CALIBRATED_MARGIN", 1.0)
    monkeypatch.setattr(tokens.ContextMeter, "__init__", init)
    return count


def test_auto_compacts_at_complete_boundary_and_continues(harness, engine, monkeypatch):
    inject_counter(engine, monkeypatch)
    cc = engine.capability_module("context_compaction")
    old_request = cc._request

    def request(*args):
        req = old_request(*args)
        summary["choices"][0]["message"]["content"] = json.dumps({
            "source_digest": req.source_digest, "notes": "synthetic earlier listing"})
        return req

    monkeypatch.setattr(cc, "_request", request)
    summary = {"choices": [{"message": {"content": "pending"}, "finish_reason": "stop"}],
               "usage": {"prompt_tokens": 200, "completion_tokens": 20}}
    assert _run(harness, "-m", GPT, "-e", "max", "--auto-compact",
                "--context-window-tokens", "1000", "--context-reply-tokens", "100",
                "--context-safety-tokens", "50", replies=[
                    _call("list_dir", {"path": "."}, cid="a"),
                    _call("list_dir", {"path": "."}, cid="b"), summary,
                    _call("finish", {"summary": "done"})]) == 0
    assert len(harness.payloads) == 4
    maintenance = harness.payloads[2]
    assert maintenance["model"] == GPT
    assert maintenance["reasoning"]["effort"] == "max"
    assert "tools" not in maintenance and "tool_choice" not in maintenance
    assert all(m["role"] in ("system", "user") for m in maintenance["messages"])
    assert "UNTRUSTED COMPACTED MEMORY" in json.dumps(harness.payloads[3])
    assert [u["phase"] for u in _usage(harness)] == ["loop", "loop", "compaction", "loop"]


@pytest.mark.parametrize("fault,expected", [
    ("schema", "bad_schema"), ("usage", "missing_usage"), ("partial_usage", "missing_usage"),
    ("tools", "tool_calls_rejected"), ("persist", "storage_error"),
    ("stale", "stale_bindings"), ("timeout", "gateway_error"),
])
def test_bad_summary_keeps_original_view_and_charges(
        harness, engine, monkeypatch, fault, expected):
    inject_counter(engine, monkeypatch)
    cc = engine.capability_module("context_compaction")
    old_request = cc._request
    old_attempt = cc.Compactor.attempt
    outcomes = []
    summary = {"choices": [{"message": {"content": "pending"}, "finish_reason": "stop"}],
               "usage": {"prompt_tokens": 200, "completion_tokens": 20}}

    def request(*args):
        req = old_request(*args)
        summary["choices"][0]["message"]["content"] = json.dumps({
            "source_digest": req.source_digest, "notes": "synthetic notes"})
        if fault == "schema":
            summary["choices"][0]["message"]["content"] = "synthetic invalid schema"
        if fault == "usage":
            summary.pop("usage", None)
        if fault == "partial_usage":   # native-shaped Chat usage, creation component absent
            summary["usage"] = {"input_tokens": 200, "cache_read_input_tokens": 80,
                                "completion_tokens": 20}
        if fault == "tools":
            summary["choices"][0]["message"]["tool_calls"] = [{"id": "unexpected"}]
        if fault == "timeout":
            summary["_transient"] = "read timeout"
        return req

    def attempt(self, *args, **kwargs):
        result = old_attempt(self, *args, **kwargs)
        outcomes.append(result.code)
        return result

    monkeypatch.setattr(cc, "_request", request)
    monkeypatch.setattr(cc.Compactor, "attempt", attempt)
    old_save = engine.Capabilities.save_artifact

    def save(self, data, ext):
        if ext == ".ctxjson" and fault == "persist":
            raise ValueError("synthetic private storage error")
        return old_save(self, data, ext)

    monkeypatch.setattr(engine.Capabilities, "save_artifact", save)
    old_call = engine.call_model

    def call(*args, **kwargs):
        result = old_call(*args, **kwargs)
        if kwargs.get("_maintenance") and fault == "stale":
            Path(str(harness.transcript_path) + ".steer").write_text(
                "synthetic new guidance", encoding="utf-8")
        return result

    monkeypatch.setattr(engine, "call_model", call)
    assert _run(harness, "-m", GPT, "--auto-compact", "--context-window-tokens", "1000",
                "--context-reply-tokens", "100", "--context-safety-tokens", "50", replies=[
                    _call("list_dir", {"path": "."}, cid="a"),
                    _call("list_dir", {"path": "."}, cid="b"), summary,
                    _call("finish", {"summary": "done"})]) == 0
    assert expected in outcomes
    assert len(harness.payloads) == 4
    final = harness.payloads[-1]["messages"]
    assert sum(m["role"] == "tool" for m in final) == 2
    assert "UNTRUSTED COMPACTED MEMORY" not in json.dumps(final)
    if fault == "stale":
        assert final[-1]["content"] == "[operator guidance] synthetic new guidance"
    records = _usage(harness)
    assert [u["phase"] for u in records] == ["loop", "loop", "compaction", "loop"]
    assert records[2]["sent"] == (None if fault == "usage" else 200)
    ce = engine.capability_module("context_engine")
    assert ce.current() is None and ce.current_runtime() is None


def test_two_failed_summaries_bound_attempts_but_main_continues(harness, engine, monkeypatch):
    inject_counter(engine, monkeypatch)
    bad = {"choices": [{"message": {"content": "synthetic bad schema"}}],
           "usage": {"prompt_tokens": 200, "completion_tokens": 20}}
    assert _run(harness, "-m", GPT, "--auto-compact", "--context-window-tokens", "1000",
                "--context-reply-tokens", "100", "--context-safety-tokens", "50", replies=[
                    _call("list_dir", {"path": "."}, cid="a"),
                    _call("list_dir", {"path": "."}, cid="b"), bad,
                    _call("list_dir", {"path": "."}, cid="c"), bad,
                    _call("list_dir", {"path": "."}, cid="d"),
                    _call("finish", {"summary": "done"})]) == 0
    assert len(harness.payloads) == 7
    assert sum(u["phase"] == "compaction" for u in _usage(harness)) == 2


def test_summary_auth_failure_is_private_rollback_not_worker_exit(
        harness, engine, monkeypatch, capsys):
    inject_counter(engine, monkeypatch)
    original = engine.chat

    def call(*args):
        if "memory formatter" in json.dumps(args[2]):
            raise SystemExit("synthetic-private-provider-body")
        return original(*args)

    old_model = engine.call_model

    def model(*args, **kwargs):
        gateway = engine.chat
        if kwargs.get("_maintenance"):
            monkeypatch.setattr(engine, "chat", call)
        try:
            return old_model(*args, **kwargs)
        finally:
            monkeypatch.setattr(engine, "chat", gateway)

    monkeypatch.setattr(engine, "call_model", model)
    assert _run(harness, "-m", GPT, "--auto-compact", "--context-window-tokens", "1000",
                "--context-reply-tokens", "100", "--context-safety-tokens", "50", replies=[
                    _call("list_dir", {"path": "."}, cid="a"),
                    _call("list_dir", {"path": "."}, cid="b"),
                    _call("finish", {"summary": "done"})]) == 0
    assert "synthetic-private-provider-body" not in harness.transcript_path.read_text() + str(
        capsys.readouterr())
    assert _usage(harness)[2]["phase"] == "compaction"
    assert _usage(harness)[2]["sent"] is None


def test_compactor_module_missing_refuses_before_config(harness, engine, monkeypatch, capsys):
    original = engine.capability_module

    def load(name):
        if name == "context_compaction":
            raise engine.CapabilityUnavailable("synthetic missing")
        return original(name)

    monkeypatch.setattr(engine, "capability_module", load)
    monkeypatch.setattr(engine, "load_cfg", lambda: pytest.fail("config read"))
    assert _run(harness, "--auto-compact", replies=[]) == 2
    assert "module_unavailable" in capsys.readouterr().err
    assert not harness.payloads


def test_reused_native_ids_compact_without_changing_retained_pair(harness, engine, monkeypatch):
    inject_counter(engine, monkeypatch)
    cc = engine.capability_module("context_compaction")
    old = cc._request
    summary = {"choices": [{"message": {"content": "pending"}}],
               "usage": {"prompt_tokens": 200, "completion_tokens": 20}}

    def request(*args):
        req = old(*args)
        summary["choices"][0]["message"]["content"] = json.dumps({
            "source_digest": req.source_digest, "notes": "synthetic repeated IDs"})
        return req

    monkeypatch.setattr(cc, "_request", request)
    assert _run(harness, "-m", GPT, "--auto-compact", "--context-window-tokens", "1000",
                "--context-reply-tokens", "100", "--context-safety-tokens", "50", replies=[
                    _call("list_dir", {"path": "."}), _call("list_dir", {"path": "."}),
                    summary, _call("finish", {"summary": "done"})]) == 0
    assert len(harness.payloads) == 4
    assert [u["phase"] for u in _usage(harness)] == ["loop", "loop", "compaction", "loop"]
    kept = harness.payloads[-1]["messages"]
    assert "UNTRUSTED COMPACTED MEMORY" in json.dumps(kept)
    assert [m["tool_call_id"] for m in kept if m["role"] == "tool"] == ["c1"]
    assert [c["id"] for m in kept for c in m.get("tool_calls", [])] == ["c1"]


def test_prompt_compaction_preserves_single_user_results(harness, engine, monkeypatch):
    from test_context_prompt import QWEN, prompt_call
    inject_counter(engine, monkeypatch)
    cc = engine.capability_module("context_compaction")
    old = cc._request
    summary = {"choices": [{"message": {"content": "pending"}}],
               "usage": {"prompt_tokens": 200, "completion_tokens": 20}}

    def request(*args):
        req = old(*args)
        summary["choices"][0]["message"]["content"] = json.dumps({
            "source_digest": req.source_digest, "notes": "synthetic prompt memory"})
        return req

    monkeypatch.setattr(cc, "_request", request)
    assert _run(harness, "-m", QWEN, "--auto-compact", "--context-window-tokens", "1000",
                "--context-reply-tokens", "100", "--context-safety-tokens", "50", replies=[
                    prompt_call("list_dir", {"path": "."}),
                    prompt_call("list_dir", {"path": "."}), summary,
                    prompt_call("finish", {"summary": "done"})]) == 0
    assert len(harness.payloads) == 4
    assert all("tools" not in p for p in harness.payloads)
    last = harness.payloads[-1]["messages"]
    assert sum(str(m.get("content", "")).startswith("Result of ") for m in last) == 1
    assert not any(m["role"] == "tool" for m in last)
    assert "UNTRUSTED COMPACTED MEMORY" in json.dumps(last)


def test_responses_maintenance_uses_same_prepared_wire_and_scope(
        harness, engine, monkeypatch, capsys):
    from test_engine_live_turns_gpt import gpt_turn, finish_turn
    inject_counter(engine, monkeypatch)
    cc = engine.capability_module("context_compaction")
    old = cc._request
    summary_event = {"type": "response.output_text.delta", "delta": "pending"}

    def request(*args):
        req = old(*args)
        summary_event["delta"] = json.dumps({"source_digest": req.source_digest,
                                             "notes": "synthetic private maintenance notes"})
        return req

    monkeypatch.setattr(cc, "_request", request)
    ce = engine.capability_module("context_engine")
    observed = []
    runtime = engine.capability_module("context_runtime").ContextRuntime
    old_observe = runtime.observe_response

    def observe(self, estimate, usage, **kwargs):
        observed.append((kwargs["phase"], kwargs["route"]))
        return old_observe(self, estimate, usage, **kwargs)

    monkeypatch.setattr(runtime, "observe_response", observe)
    text = harness.run("-m", GPT, "-e", "max", "--auto-compact",
                       "--context-window-tokens", "1000", "--context-reply-tokens", "100",
                       "--context-safety-tokens", "50", replies=[], stream_replies=[
        gpt_turn("list_dir", {"path": "."}, call_id="a", summary=(), usage=None),
        gpt_turn("list_dir", {"path": "."}, call_id="b", summary=(), usage=None),
        [summary_event, {"type": "response.completed", "response": {
            "usage": {"input_tokens": 200, "output_tokens": 20}}}], finish_turn()])
    assert len(harness.stream_calls) == 4 and not harness.payloads
    assert all(path == "/openai/responses" for path, _ in harness.stream_calls)
    packet = harness.stream_calls[2][1]
    assert "input" in packet and "messages" not in packet
    assert "tools" not in packet and "tool_choice" not in packet
    assert packet["model"] == GPT and packet["reasoning"]["effort"] == "max"
    assert packet["max_output_tokens"] == 100
    assert ("compaction", "responses") in observed
    # Trusted Responses route makes bare inclusive input_tokens a measurement: view commits.
    assert "UNTRUSTED COMPACTED MEMORY" in json.dumps(harness.stream_calls[3][1])
    assert "synthetic private maintenance notes" not in text + capsys.readouterr().out
    assert ce.current() is None and ce.current_runtime() is None


def test_real_image_asset_survives_component_compaction(harness, engine, monkeypatch, tmp_path):
    from test_engine_images import make_png
    inject_counter(engine, monkeypatch)
    image = make_png(2, 2)
    (tmp_path / "synthetic.png").write_bytes(image)
    cc = engine.capability_module("context_compaction")
    original = cc._request
    summary = {"choices": [{"message": {"content": "pending"}}],
               "usage": {"prompt_tokens": 200, "completion_tokens": 20}}
    captured = []
    runtime = engine.capability_module("context_runtime").ContextRuntime
    old_commit = runtime.commit_view

    def commit(self, *args, lineage):
        newest_ids = list(self._groups[-1]["ids"])
        old_ids = {r["id"] for r in self._rows}
        result = old_commit(self, *args, lineage=lineage)
        captured.append((self.snapshot_data(), result, self._rows[:], self._groups[:],
                         newest_ids, old_ids))
        return result

    def request(*args):
        req = original(*args)
        summary["choices"][0]["message"]["content"] = json.dumps({
            "source_digest": req.source_digest, "notes": "synthetic viewed image"})
        return req

    monkeypatch.setattr(cc, "_request", request)
    monkeypatch.setattr(runtime, "commit_view", commit)
    old_call = engine.call_model

    def call(*args, **kwargs):
        # The generic fake gateway lacks production's ImageAsset JSON default.
        gateway = engine.chat

        def wire(url, key, payload):
            return gateway(url, key, json.loads(json.dumps(payload, default=engine._wire_default)))

        monkeypatch.setattr(engine, "chat", wire)
        try:
            return old_call(*args, **kwargs)
        finally:
            monkeypatch.setattr(engine, "chat", gateway)

    monkeypatch.setattr(engine, "call_model", call)
    assert _run(harness, "-m", GPT, "--auto-compact", "--context-window-tokens", "1000",
                "--context-reply-tokens", "100", "--context-safety-tokens", "50", replies=[
                    _call("view_image", {"path": "synthetic.png"}, cid="image"),
                    _call("list_dir", {"path": "."}, cid="list"), summary,
                    _call("finish", {"summary": "done"})]) == 0
    assert captured and captured[0][1]
    snap = captured[0][0]
    assets = [b["image_url"]["url"] for i in snap["pinned"] for b in (
        snap["messages"][i].get("content") if isinstance(
            snap["messages"][i].get("content"), list) else [])
        if isinstance(b, dict) and b.get("type") == "image_url"]
    assert len(assets) == 1 and assets[0].data == image
    assert snap["messages"][1]["content"] == "do the thing"
    assert snap["groups"] == [(2,), (4, 5)]
    rows, groups, newest_ids, old_ids = captured[0][2:]
    memory_id = rows[2]["id"]
    assert memory_id not in old_ids and not rows[2]["pinned"]
    assert groups[0] == {"kind": "memory", "ids": [memory_id], "refs": []}
    assert groups[1]["kind"] == "native" and groups[1]["ids"] == newest_ids
    assert len(groups[1]["refs"]) == 1
    assert [r["id"] for r in rows[4:6]] == newest_ids
    assert [snap["messages"][i]["role"] for i in (2, 4, 5)] == ["assistant", "assistant", "tool"]
    assert snap["messages"][4]["tool_calls"][0]["id"] == "list"
    assert snap["messages"][5]["tool_call_id"] == "list"


def test_command_not_replayed_across_compaction(harness, engine, monkeypatch, tmp_path):
    import sys
    inject_counter(engine, monkeypatch)
    (tmp_path / "emit.py").write_text("open('count.txt', 'a').write('x')\nprint('synthetic')\n")
    policy = tmp_path / ".claude"
    policy.mkdir(exist_ok=True)
    (policy / "agent-policy.json").write_text(json.dumps({
        "allowed_cmds": [["python", "emit.py"]]}))
    monkeypatch.setattr(engine.shutil, "which", lambda name: sys.executable)
    cc = engine.capability_module("context_compaction")
    original = cc._request
    summary = {"choices": [{"message": {"content": "pending"}}],
               "usage": {"prompt_tokens": 200, "completion_tokens": 20}}

    def request(*args):
        req = original(*args)
        summary["choices"][0]["message"]["content"] = json.dumps({
            "source_digest": req.source_digest, "notes": "synthetic command completed"})
        return req

    monkeypatch.setattr(cc, "_request", request)
    assert _run(harness, "-m", GPT, "--auto-compact", "--context-window-tokens", "1000",
                "--context-reply-tokens", "100", "--context-safety-tokens", "50", replies=[
                    _call("run_command", {"argv": ["python", "emit.py"]}, cid="cmd"),
                    _call("list_dir", {"path": "."}, cid="list"), summary,
                    _call("finish", {"summary": "done"})]) == 0
    assert (tmp_path / "count.txt").read_text() == "x"
    assert len(harness.payloads) == 4
    assert _usage(harness)[2]["phase"] == "compaction"


def test_loaded_skill_instructions_remain_authoritative(harness, engine, monkeypatch):
    from types import SimpleNamespace
    inject_counter(engine, monkeypatch)
    original = engine.Capabilities.load_skill

    def load(self, name):
        self._skills = SimpleNamespace(loaded=[name], catalog=lambda: [])
        return "[synthetic loaded skill] preserve synthetic instruction"

    monkeypatch.setattr(engine.Capabilities, "load_skill", load)
    assert _run(harness, "-m", GPT, "--auto-compact", "--context-window-tokens", "1000",
                "--context-reply-tokens", "100", "--context-safety-tokens", "50", replies=[
                    _call("load_skill", {"name": "synthetic"}, cid="skill"),
                    _call("finish", {"summary": "done"})]) == 0
    assert "preserve synthetic instruction" in harness.payloads[-1]["messages"][0]["content"]


def test_unknown_estimate_declines_unmeasurable_without_rpc(harness, engine, monkeypatch):
    import dataclasses
    inject_counter(engine, monkeypatch)
    ce = engine.capability_module("context_engine")
    measure, attempt, seen = ce.AutomaticCompaction.measure, ce.AutomaticCompaction.attempt, []

    def unknown(self, packet):
        return dataclasses.replace(measure(self, packet), tokens=None)  # unknown, not zero

    def watch(self):
        view = ce.current_runtime().payload_messages()
        out = attempt(self)
        seen.append((out.code, out.calls, self._ids, view == ce.current_runtime().payload_messages()))
        return out

    monkeypatch.setattr(ce.AutomaticCompaction, "measure", unknown)
    monkeypatch.setattr(ce.AutomaticCompaction, "attempt", watch)
    assert _run(harness, "-m", GPT, "--auto-compact", "--context-window-tokens", "1000",
                "--context-reply-tokens", "100", "--context-safety-tokens", "50", replies=[
                    _call("list_dir", {"path": "."}, cid="a"),
                    _call("list_dir", {"path": "."}, cid="b"),
                    _call("finish", {"summary": "done"})]) == 0
    assert seen and all(s == ("unmeasurable", 0, {}, True) for s in seen)
    assert [u["phase"] for u in _usage(harness)] == ["loop", "loop", "loop"]
    assert "memory formatter" not in json.dumps(harness.payloads)


def test_storage_attempt_ledger_is_bounded_after_failures(engine, monkeypatch):
    cs = engine.capability_module("context_storage")
    calls = []

    class Caps:  # synthetic backend that always fails
        def save_artifact(self, data, suffix):
            calls.append(len(data))
            raise OSError("synthetic")

    monkeypatch.setattr(cs, "_MAX_KEYS", 3, raising=False)
    store = cs.OwnedContextStorage(Caps(), lambda *a: None)
    for i in range(3):
        with pytest.raises(cs.ContextStorageError) as err:
            store.publish(f"k{i}", b"x")
        assert err.value.code == "publish_failed"
    for key, code in (("k0", "duplicate_key"), ("k9", "publish_failed"),
                      ("k10", "publish_failed"), ("k1", "duplicate_key")):
        with pytest.raises(cs.ContextStorageError) as err:
            store.publish(key, b"x")
        assert err.value.code == code
    assert len(calls) == 3 and store._used == {"k0", "k1", "k2"}


def test_cancelled_summary_keeps_checkpoint_and_charges(harness, engine, monkeypatch):
    inject_counter(engine, monkeypatch)
    original = engine.call_model
    ce = engine.capability_module("context_engine")
    captured = []

    def call(*args, **kwargs):
        if kwargs.get("_maintenance"):
            rt = ce.current_runtime()
            captured.append((rt, rt.payload_messages()))
            exc = KeyboardInterrupt()
            exc.usage = {"prompt_tokens": 200, "completion_tokens": 20}
            raise exc
        return original(*args, **kwargs)

    monkeypatch.setattr(engine, "call_model", call)
    with pytest.raises(KeyboardInterrupt):
        _run(harness, "-m", GPT, "--auto-compact", "--context-window-tokens", "1000",
             "--context-reply-tokens", "100", "--context-safety-tokens", "50", replies=[
                 _call("list_dir", {"path": "."}, cid="a"),
                 _call("list_dir", {"path": "."}, cid="b")])
    rt, original_view = captured[0]
    assert rt.payload_messages() == original_view
    assert rt.metadata()["compactions"] == 0
    usage = _usage(harness)
    assert usage[-1]["phase"] == "compaction" and usage[-1]["sent"] == 200
    assert ce.current() is None and ce.current_runtime() is None

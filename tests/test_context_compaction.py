import copy
import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1] / "engine"


def _load(name):
    spec = importlib.util.spec_from_file_location(f"_ac_{name}", ROOT / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


cp = _load("context_policy")
cc = _load("context_compaction") if (ROOT / "context_compaction.py").exists() else None
BIND = {"run": "r1", "model": "m1", "route": "chat", "effort": "high", "history_mode": "stateless",
        "task_hash": "t" * 8}


def POL(auto=True):  # window 1000 -> trigger 700, target 560, input limit 800
    return cp.Policy(1000, 100, 100, auto_compact=auto)


def msgs(n_groups, size=40):
    out = [{"role": "system", "content": "SYS"}, {"role": "user", "content": "TASK"}]
    groups = []
    for i in range(n_groups):
        groups.append((len(out), len(out) + 1))
        out += [{"role": "user", "content": f"q{i}" + "x" * size},
                {"role": "assistant", "content": f"a{i}" + "y" * size}]
    return out, tuple(groups)


def toks(packet):  # synthetic estimator: 1 token per 4 JSON chars of messages
    return len(json.dumps(packet["messages"], default=lambda o: "<img>")) // 4


class H:
    """Private fixture state standing in for trusted runtime callbacks."""

    def __init__(self, policy=None, reply=None, commit_ok=True):
        self.policy = policy or POL()
        self.bind = dict(BIND)
        self.calls, self.persisted, self.commits, self.charged = [], [], [], 0
        self.reply = reply
        self.commit_ok = commit_ok
        self.view = None

    def measure(self, packet):
        return cc.Estimate(toks(packet), ())

    def render_worker(self, messages):
        return {"messages": [{"role": "system", "content": "CAPSULE"}] + list(messages)}

    def call_summary(self, request):
        self.calls.append(request)
        self.charged += 1  # charged before any validation, never undone
        r = self.reply
        if callable(r):
            return r(request)
        if r is None:
            data = json.loads(request.messages[1]["content"])
            r = json.dumps({"source_digest": data["source_digest"], "notes": "summary"})
        if isinstance(r, str):
            return cc.SummaryReply(r, 50, 10, "m1", "chat", "high")
        return r

    def persist(self, record):
        self.persisted.append(record)
        return f"artifact-{len(self.persisted)}"

    def commit(self, expected, candidate, ref, *, lineage):
        self.commits.append(ref)
        self.lineage = lineage
        if expected != self.bind or not self.commit_ok:
            return False
        self.view = candidate
        return True

    def compactor(self, **kw):
        return cc.Compactor(self.policy, measure=self.measure, render_worker=self.render_worker,
                            call_summary=self.call_summary, persist=self.persist,
                            commit=self.commit, get_bindings=lambda: dict(self.bind), **kw)

    def snap(self, messages, groups, pinned=(0, 1), **kw):
        return cc.snapshot(messages, pinned=pinned, groups=groups, bindings=dict(self.bind), **kw)


def fixed(h, original_len, tokens):
    """Original packet (system capsule + original_len rows) measures `tokens`; others real."""
    h.measure = lambda p: cc.Estimate(
        tokens if len(p["messages"]) == original_len + 1 else toks(p), ())


@pytest.fixture(autouse=True)
def _need_module():
    if cc is None:
        pytest.fail("engine/context_compaction.py missing")


def test_disabled_is_noop_even_forced():
    h = H(policy=POL(auto=False))
    m, g = msgs(30)
    out = h.compactor().attempt(h.snap(m, g), force=True)
    assert out.code == "disabled" and not out.committed and h.calls == []


def test_below_trigger_makes_no_model_call():
    h = H()
    m, g = msgs(6)
    fixed(h, len(m), 699)
    out = h.compactor().attempt(h.snap(m, g))
    assert out.code == "below_trigger" and h.calls == [] and h.persisted == []


def test_exact_trigger_compacts_and_force_bypasses_only_trigger():
    h = H()
    m, g = msgs(6)
    fixed(h, len(m), 700)
    out = h.compactor().attempt(h.snap(m, g))
    assert out.committed and out.code == "committed" and len(h.calls) == 1
    h2 = H()  # real estimate is far below 700: force bypasses only the trigger
    assert h2.compactor().attempt(h2.snap(m, g)).code == "below_trigger"
    assert H().compactor().attempt(h2.snap(m, g), force=True).committed


def native(n):
    """Native tool exchanges: user, assistant tool_calls, tool results."""
    out, groups = [{"role": "system", "content": "SYS"}, {"role": "user", "content": "TASK"}], []
    for i in range(n):
        s = len(out)
        out += [{"role": "user", "content": f"q{i}" + "x" * 20},
                {"role": "assistant", "content": None, "tool_calls": [
                    {"id": f"c{i}a", "type": "function", "function": {"name": "f", "arguments": "{}"}},
                    {"id": f"c{i}b", "type": "function", "function": {"name": "g", "arguments": "{}"}}]},
                {"role": "tool", "tool_call_id": f"c{i}a", "content": "r" * 20},
                {"role": "tool", "tool_call_id": f"c{i}b", "content": "s" * 20}]
        groups.append(tuple(range(s, len(out))))
    return out, tuple(groups)


def test_request_same_route_tool_free_json_quotation():
    h = H()
    m, g = native(6)
    assert h.compactor().attempt(h.snap(m, g), force=True).committed
    req = h.calls[0]
    assert (req.model, req.route, req.effort) == ("m1", "chat", "high")
    assert req.reply_tokens == 100 and [r["role"] for r in req.messages] == ["system", "user"]
    assert all(set(r) == {"role", "content"} for r in req.messages)  # no tools/tool_calls actions
    data = json.loads(req.messages[1]["content"])
    assert len(data["source_digest"]) == 64 and data["source_digest"] == req.source_digest
    assert data["history"][1]["tool_calls"][0]["id"] == "c0a"  # quoted as DATA only
    assert all(r["role"] != "system" for r in data["history"])  # pins never sent as data
    assert "SYS" not in req.messages[1]["content"]


def test_pins_complete_groups_recent_and_order_preserved():
    h = H()
    m, g = native(6)
    m.insert(6, {"role": "user", "content": "STEER"})  # ungrouped steer between groups
    g = tuple(tuple(i + (i >= 6) for i in grp) for grp in g)
    pinned = (0, 1, 6)
    out = h.compactor().attempt(h.snap(m, g, pinned=pinned), force=True)
    assert out.committed
    view = h.view
    kept = [r for r in view if not str(r.get("content", "")).startswith("[UNTRUSTED")]
    assert [r for r in m if r in kept] == kept  # relative order of retained originals
    for i in (0, 1, 6) + g[-1]:
        assert m[i] in view
    note = [r for r in view if str(r.get("content", "")).startswith("[UNTRUSTED COMPACTED")]
    assert len(note) == 1 and note[0]["role"] == "assistant"
    ids = {c["id"] for r in view for c in r.get("tool_calls") or ()}
    assert ids == {r["tool_call_id"] for r in view if r["role"] == "tool"}  # no orphans


@pytest.mark.parametrize("mutate,code", [
    (lambda m, b: m[3].update(reasoning_details=[{"signature": "zz"}]), "opaque_state"),
    (lambda m, b: m[3].update(content=[{"type": "text", "text": "a", "encrypted_content": "q"}]),
     "opaque_state"),
    (lambda m, b: b.update(history_mode="server_side"), "unsupported_history_mode"),
    (lambda m, b: b.update(route="anthropic"), "unsupported_history_mode"),
])
def test_signed_opaque_or_stateful_history_declines_without_call(mutate, code):
    h = H()
    m, g = msgs(6)
    mutate(m, h.bind)
    out = h.compactor().attempt(h.snap(m, g), force=True)
    assert out.code == code and h.calls == [] and not out.committed


def test_known_media_kept_whole_and_never_sent():
    h = H()
    m, g = msgs(6)
    img = {"type": "image_url", "image_url": {"url": "data:image/png;base64,QUFB"}}
    m[4] = {"role": "user", "content": [{"type": "text", "text": "see"}, img]}
    out = h.compactor().attempt(h.snap(m, g), force=True)
    assert out.committed and m[4] in h.view and m[5] in h.view  # media group untouched
    assert "base64" not in h.calls[0].messages[1]["content"]


def _reply(text=None, **kw):
    def make(req):
        t = text if text is not None else json.dumps({"source_digest": req.source_digest,
                                                      "notes": "n"})
        args = dict(text=t.replace("DIG", req.source_digest), input_tokens=5, output_tokens=5,
                    model="m1", route="chat", effort="high")
        args.update(kw)
        return cc.SummaryReply(**args)
    return make


def _raise(req):
    raise RuntimeError("gateway down")


@pytest.mark.parametrize("reply,code", [
    (_raise, "gateway_error"),
    (lambda r: None, "bad_reply"),
    (_reply('{"source_digest":"DIG","notes":""}'), "bad_notes"),
    (_reply('{"source_digest":"DIG","notes":"a","notes":"b"}'), "bad_schema"),
    (_reply('{"source_digest":"DIG","notes":"a","approved":true}'), "bad_schema"),
    (_reply('{"source_digest":"DIG","notes":"' + "z" * 17000 + '"}'), "bad_notes"),
    (_reply('{"source_digest":"DIG","notes":"' + "z" * 21000 + '"}'), "bad_schema"),
    (_reply('{"source_digest":"DIG","notes":["a"]}'), "bad_notes"),
    (_reply("not json"), "bad_schema"),
    (_reply('{"source_digest":"' + "0" * 64 + '","notes":"a"}'), "source_mismatch"),
    (_reply(model="m2"), "route_mismatch"),
    (_reply(effort="low"), "route_mismatch"),
    (_reply(tool_calls=({"id": "x"},)), "tool_calls_rejected"),
    (_reply(input_tokens=None), "missing_usage"),
    (_reply(output_tokens=True), "missing_usage"),
])
def test_bad_replies_keep_old_view_and_stay_charged(reply, code):
    h = H(reply=reply)
    m, g = msgs(6)
    before = copy.deepcopy(m)
    c = h.compactor()
    out = c.attempt(h.snap(m, g), force=True)
    assert out.code == code and not out.committed and out.calls == 1
    assert h.charged == 1 and c.calls == 1 and c.failures == 1  # cost never rolled back
    assert h.persisted == [] and h.commits == [] and h.view is None and m == before


def _stale_in_call(h):
    inner = h.call_summary
    def call(req):
        h.bind["candidate_hash"] = "new"
        return inner(req)
    h.call_summary = call


def _stale_in_persist(h):
    inner = h.persist
    def persist(rec):
        h.bind["steer_hash"] = "new"
        return inner(rec)
    h.persist = persist


def _commit_raises(h):
    def commit(*a, lineage):
        h.commits.append("raised")
        raise RuntimeError("cancelled")
    h.commit = commit


def _persist_raises(h):
    def persist(rec):
        raise OSError("quota")
    h.persist = persist


@pytest.mark.parametrize("fault,code,persisted", [
    (_stale_in_call, "stale_bindings", 0),
    (_stale_in_persist, "stale_bindings", 1),
    (lambda h: setattr(h, "commit_ok", False), "commit_rejected", 1),
    (_commit_raises, "commit_rejected", 1),  # persisted before commit; never activated
    (_persist_raises, "storage_error", 0),
    (lambda h: setattr(h, "persist", lambda rec: ""), "storage_error", 0),
])
def test_faults_before_commit_keep_old_view_and_charges(fault, code, persisted):
    h = H()
    fault(h)
    m, g = msgs(6)
    c = h.compactor()
    out = c.attempt(h.snap(m, g), force=True)
    assert out.code == code and not out.committed and h.view is None
    assert h.charged == 1 and c.calls == 1 and len(h.persisted) == persisted


def _faulty(h, name, at):
    cbs = dict(measure=h.measure, render_worker=h.render_worker,
               get_bindings=lambda: dict(h.bind))
    real, seen = cbs[name], [0]
    def f(*a):
        seen[0] += 1
        if seen[0] == at:
            raise RuntimeError("SECRET-body")
        return real(*a)
    cbs[name] = f
    return cc.Compactor(h.policy, call_summary=h.call_summary, persist=h.persist,
                        commit=h.commit, **cbs), seen


@pytest.mark.parametrize("name", ["measure", "render_worker", "get_bindings"])
def test_callback_fault_at_every_position_is_fixed_code_and_charged(name):
    h = H()
    m, g = msgs(6)
    c, seen = _faulty(h, name, 0)
    assert c.attempt(h.snap(m, g), force=True).committed
    for at in range(1, seen[0] + 1):
        h = H()
        c, _ = _faulty(h, name, at)
        out = c.attempt(h.snap(m, g), force=True)
        assert out.code == "callback_error" and not out.committed and h.view is None
        assert h.commits == [] and "SECRET" not in repr(out) + repr(c)
        assert out.calls == c.calls == len(h.calls) == h.charged
        assert c.failures == (1 if h.calls else 0)


def test_can_call_fault_is_fixed_code_without_call():
    h = H()
    c = h.compactor(can_call=lambda: (_ for _ in ()).throw(RuntimeError("SECRET")))
    out = c.attempt(h.snap(*msgs(6)), force=True)
    assert out.code == "callback_error" and out.calls == 0 == c.calls == c.failures
    assert h.calls == [] and "SECRET" not in repr(out)


def test_interrupt_before_commit_propagates_without_adoption_or_retry():
    h = H(reply=lambda req: (_ for _ in ()).throw(KeyboardInterrupt()))
    c = h.compactor()
    s = h.snap(*msgs(6))
    with pytest.raises(KeyboardInterrupt):
        c.attempt(s, force=True)
    assert c.calls == 1 and h.view is None
    assert c.attempt(s, force=True).code == "already_attempted" and len(h.calls) == 1


def test_attempt_ledger_is_bounded_and_calls_stay_monotonic():
    h = H(reply=_raise)
    c = h.compactor(max_failures=10 ** 6)
    for i in range(cc.Compactor.LEDGER_MAX + 5):
        h.bind["steer_hash"] = f"s{i}"
        assert c.attempt(h.snap(*msgs(6)), force=True).code == "gateway_error"
    assert len(c._attempted) == cc.Compactor.LEDGER_MAX
    assert c.calls == len(h.calls) == cc.Compactor.LEDGER_MAX + 5
    c.notify_progress()
    assert c.calls == len(h.calls) and len(c._attempted) == 0


def test_changed_bindings_same_history_is_a_new_transaction():
    h = H(reply=_raise)
    c = h.compactor()
    m, g = msgs(6)
    assert c.attempt(h.snap(m, g), force=True).code == "gateway_error"
    assert c.attempt(h.snap(m, g), force=True).code == "already_attempted"
    h.bind["steer_hash"] = "new guidance"
    assert c.attempt(h.snap(m, g), force=True).code == "gateway_error"
    assert c.attempt(h.snap(*msgs(7)), force=True).code == "failure_limit"
    assert len(h.calls) == 2


def test_cancel_or_stale_before_call_makes_no_call():
    h = H()
    m, g = msgs(6)
    assert h.compactor(can_call=lambda: False).attempt(h.snap(m, g), force=True).code \
        == "call_not_permitted"
    s = h.snap(m, g)
    h.bind["receipt_hash"] = "moved"
    assert h.compactor().attempt(s, force=True).code == "stale_bindings"
    assert h.calls == []


def test_repeated_failures_are_bounded_until_progress():
    h = H(reply=_raise)
    c = h.compactor(max_failures=2)
    s1 = h.snap(*msgs(6))
    assert c.attempt(s1, force=True).code == "gateway_error"
    assert c.attempt(s1, force=True).code == "already_attempted"  # one per unchanged snapshot
    assert c.attempt(h.snap(*msgs(7)), force=True).code == "gateway_error"
    assert c.attempt(h.snap(*msgs(8)), force=True).code == "failure_limit"
    assert len(h.calls) == 2
    c.notify_progress()
    h.reply = None
    assert c.attempt(h.snap(*msgs(8)), force=True).committed and len(h.calls) == 3


def test_candidate_over_target_or_not_smaller_is_refused():
    h = H(reply=_reply(text=json.dumps({"source_digest": "DIG", "notes": "w" * 3000})))
    m, g = msgs(6)
    out = h.compactor().attempt(h.snap(m, g), force=True)
    assert out.code == "candidate_too_large" and h.persisted == [] and h.view is None
    h2 = H()
    fixed(h2, len(m), 50)  # original "measures" smaller than any candidate
    assert h2.compactor().attempt(h2.snap(m, g), force=True).code == "no_reduction"


def test_nothing_reducible_or_unobservable_is_noop_without_call():
    h = H()
    m, g = msgs(1)  # only the recent group
    assert h.compactor().attempt(h.snap(m, g), force=True).code == "nothing_reducible"
    m, g = msgs(6)
    assert h.compactor().attempt(h.snap(m, g, pinned=(0, 1) + tuple(range(2, 12))),
                                 force=True).code == "nothing_reducible"
    h.measure = lambda p: cc.Estimate(900, ("estimated", "history_unobservable"))
    assert h.compactor().attempt(h.snap(m, g), force=True).code == "unmeasurable"
    assert h.calls == []


@pytest.mark.parametrize("bad", [((2, 3), (3, 4)), ((4, 5), (2, 3)), ((2, 99),)])
def test_invalid_groups_rejected(bad):
    with pytest.raises(cc.CompactionError):
        H().snap(*msgs(3)[:1], bad)
    m, g = native(2)
    with pytest.raises(cc.CompactionError, match="orphan_tool_pair"):
        H().snap(m, ((2, 3, 4),) + g[1:])  # result c0b split from its call


def test_privacy_of_repr_record_and_source_input_unchanged():
    h = H()
    m, g = msgs(6)
    before = copy.deepcopy(m)
    s = h.snap(m, g)
    m[2]["content"] = "mutated after snapshot"
    c = h.compactor()
    out = c.attempt(s, force=True)
    assert out.committed and h.commits == [out.artifact_ref]
    assert m[2]["content"] == "mutated after snapshot" and s.messages[2] == before[2]
    text = repr(s) + repr(out) + repr(c) + repr(h.calls[0])
    for secret in ("SYS", "TASK", "xxxx", "summary", "m1", "artifact-"):
        assert secret not in text
    rec = h.persisted[0]
    assert set(rec) == {"schema", "source_digest", "notes", "binding_digest", "snapshot_digest",
                        "removed_groups", "before_tokens", "after_tokens"}
    assert "xxxx" not in json.dumps(rec) and "CAPSULE" not in json.dumps(rec)
    assert "CAPSULE" not in json.dumps(h.view)  # runtime renders its own capsule separately


@pytest.mark.parametrize("tamper", [
    lambda s: s.messages[2].update(content="TAMPERED"),
    lambda s: s.messages[0].update(content="SYS2"),
    lambda s: s.bindings.update(model="m2"),
])
def test_snapshot_mutation_after_creation_rejected_before_any_callback(tamper):
    h = H()
    m, g = msgs(6)
    s = h.snap(m, g)
    tamper(s)
    out = h.compactor().attempt(s, force=True)
    assert out.code == "snapshot_tampered" and h.calls == [] and not out.committed
    assert "TAMPER" not in repr(out) and "m2" not in repr(out)


def test_callback_mutating_public_snapshot_cannot_change_candidate():
    h = H()
    m, g = msgs(6)
    s = h.snap(m, g)
    inner = h.call_summary

    def call(req):
        s.messages[-1]["content"] = "EVIL"
        s.bindings["model"] = "m2"
        return inner(req)
    h.call_summary = call
    assert h.compactor().attempt(s, force=True).committed and "EVIL" not in json.dumps(h.view)


class Img:  # synthetic slotted stand-in with the engine ImageAsset attribute shape
    __slots__ = ("path", "mime", "data", "width", "height", "sha256", "source")

    def __init__(self, data=b"\x89PNGsynthetic"):
        self.path, self.mime, self.data, self.width, self.height = "Z:/absent.png", "image/png", data, 2, 3
        self.sha256, self.source = "0" * 64, None  # stale on purpose: bytes are fingerprinted

    def data_url(self):
        raise AssertionError("data_url must not be used")


def test_image_asset_parts_fingerprinted_pinned_and_kept_whole():
    h = H()
    m, g = msgs(6)
    m[4] = {"role": "user", "content": [{"type": "text", "text": "see"}, Img()]}
    s = h.snap(m, g, image_type=Img)
    m2 = copy.deepcopy(m)
    m2[4]["content"][1].data = b"other"
    assert h.snap(m2, g, image_type=Img).digest != s.digest
    out = h.compactor().attempt(s, force=True)
    assert out.committed and m[5] in h.view
    imgs = [p for r in h.view if isinstance(r.get("content"), list) for p in r["content"]
            if isinstance(p, Img)]
    assert [(p.data, p.width) for p in imgs] == [(b"\x89PNGsynthetic", 2)]
    body = h.calls[0].messages[1]["content"]
    assert "PNG" not in body and "absent" not in body and "base64" not in body


@pytest.mark.parametrize("part", [Img(), [], {"type": []}, {"type": {}}, object()])
def test_unregistered_objects_and_malformed_kinds_decline_fixed(part):
    h = H()
    m, g = msgs(6)
    m[4] = {"role": "user", "content": [part]}
    out = h.compactor().attempt(h.snap(m, g), force=True)
    assert out.code == "unsupported_content" and h.calls == []


def test_parallel_results_in_any_id_order_are_valid():
    h = H()
    m, g = native(6)
    m[4], m[5] = m[5], m[4]  # results c0b then c0a: legal by ID
    assert h.compactor().attempt(h.snap(m, g), force=True).committed


@pytest.mark.parametrize("edit", [
    lambda m: m.__setitem__(5, dict(m[5], tool_call_id="c0a")),  # duplicate result
    lambda m: m[3]["tool_calls"].append(dict(m[3]["tool_calls"][0])),  # duplicate call
    lambda m: m.__setitem__(slice(3, 5), [m[4], m[3]]),  # result before its call
    lambda m: m.__setitem__(4, {"role": "user", "content": "x"}),  # interrupted exchange
    lambda m: m[3].__setitem__("tool_calls", "bad"),
    lambda m: m[3].__setitem__("tool_calls", [None]),
    lambda m: m[7].__setitem__("tool_calls", [dict(m[7]["tool_calls"][0], id="c0a"),
                                              m[7]["tool_calls"][1]]),  # id reused later
])
def test_malformed_tool_order_raises_fixed_error(edit):
    m, g = native(3)
    edit(m)
    with pytest.raises(cc.CompactionError) as e:
        H().snap(m, g)
    assert e.value.code in ("orphan_tool_pair", "invalid_message")


@pytest.mark.parametrize("edit,code", [
    (lambda m: m[3]["content"][0].update(meta={"x": [{"signature": "s"}]}), "opaque_state"),
    (lambda m: m[3].update(extra=[{"thinking": "t"}]), "opaque_state"),
    (lambda m: m[3]["tool_calls"][0].update(ext={"encrypted_content": "q"}), "opaque_state"),
    (lambda m: m[3]["tool_calls"][0]["function"].update(arguments='{"signature":"x"}'), None),
    (lambda m: m[2].update(content="please add a signature and reasoning"), None),
])
def test_nested_opaque_keys_scanned_structurally_only(edit, code):
    h = H()
    m, g = native(6)
    m[3]["content"] = [{"type": "text", "text": "t"}]
    edit(m)
    out = h.compactor().attempt(h.snap(m, g), force=True)
    assert out.code == code if code else out.committed
    assert len(h.calls) == (0 if code else 1)


def test_real_context_meter_estimate_accepted():
    ct = _load("context_tokens")
    h = H()
    h.measure = ct.ContextMeter(POL(), route="chat", model="m1").estimate
    m, g = msgs(8, size=4)
    out = h.compactor().attempt(h.snap(m, g), force=True)
    assert out.committed and isinstance(out.before_tokens, int)


def test_none_effort_is_unspecified_and_checked_exactly():
    h = H(reply=_reply(effort=None))
    h.bind["effort"] = None
    m, g = msgs(6)
    assert h.compactor().attempt(h.snap(m, g), force=True).committed
    assert h.calls[0].effort is None
    h2 = H(reply=_reply(effort="high"))
    h2.bind["effort"] = None
    assert h2.compactor().attempt(h2.snap(m, g), force=True).code == "route_mismatch"


def test_deep_json_raises_fixed_error_not_recursion():
    deep = []
    for _ in range(5000):
        deep = [deep]
    m, g = msgs(6)
    m[3]["extra"] = deep
    with pytest.raises(cc.CompactionError) as e:
        H().snap(m, g)
    assert e.value.code == "invalid_message"


def test_lone_surrogates_decline_but_ordinary_unicode_works():
    h = H()
    m, g = msgs(6)
    m[2]["content"] = "bad \ud800"
    assert h.compactor().attempt(h.snap(m, g), force=True).code == "invalid_unicode"
    assert h.calls == []
    h2 = H(reply=_reply('{"source_digest":"DIG","notes":"x \\ud800"}'))
    m, g = msgs(6)
    m[2]["content"] = 'é 漢字 "quoted" {"json": 1}'
    assert h2.compactor().attempt(h2.snap(m, g), force=True).code == "invalid_unicode"
    h3 = H()
    assert h3.compactor().attempt(h3.snap(m, g), force=True).committed
    history = json.loads(h3.calls[0].messages[1]["content"])["history"]
    assert history[0] == m[2]  # ordinary Unicode / quoted JSON kept as exact data

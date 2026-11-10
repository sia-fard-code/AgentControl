import importlib
import json
import threading

import pytest

from engine import context_runtime as cr
from engine.context_policy import Policy

ALLOWED = ("context_tokens", "context_receipts", "context_guard")


def loader(calls=None):
    def load(name):
        assert name in ALLOWED  # fixed selected source only
        if calls is not None:
            calls.append(name)
        return importlib.import_module("engine." + name)
    return load


def blobs():
    store = {}
    def publish(key, data):
        store[key] = bytes(data)
    def read(key, start, n):
        return store[key][start:start + n]
    return store, publish, read


def runtime(window=1_000_000, journal=None, external=None):
    _, pub, rd = blobs()
    return cr.ContextRuntime(Policy(window, 32_000, 8_000), load_component=loader(),
                             model="m-synth", route="chat", journal=journal,
                             publisher=pub, reader=rd, external_bindings=external)


def test_constructor_validates_before_loading():
    calls = []
    for bad in ({"model": ""}, {"route": "bogus"}, {"effort": 3}, {"journal": 5}):
        kw = {"model": "m", "route": "chat", **bad}
        with pytest.raises(ValueError):
            cr.ContextRuntime(Policy(1_000_000, 32_000, 8_000), load_component=loader(calls), **kw)
    assert calls == []


def call(i):
    return {"id": f"c{i}", "type": "function", "function": {"name": "grep", "arguments": "{}"}}


def batch(rt, n, tag="a", kind="native"):
    ids = [f"{tag}{i}" for i in range(n)]
    assistant = {"role": "assistant", "content": "", "tool_calls": [call(i) for i in ids]}
    envs = [{"role": "tool", "tool_call_id": f"c{i}"} for i in ids]
    ok = rt.begin_batch({"model": "m-synth", "messages": rt.payload_messages()},
                        assistant, envs, ids, kind=kind)
    return ok, ids


def drop_first_group(snap, text="synthetic summary"):
    """Valid direct commit: replace the oldest group with one assistant memory row."""
    g, m = snap["groups"][0], snap["messages"]
    lin = tuple(range(g[0])) + (None,) + tuple(range(g[-1] + 1, len(m)))
    return [{"role": "assistant", "content": text} if i is None else m[i] for i in lin], lin


def full_batch(rt, tag):
    ok, ids = batch(rt, 1, tag)
    rt.admit_result("grep", "body " + tag, slot_id=ids[0])
    rt.end_batch()


def test_large_request_native_batch_journal_and_receipt_paging():
    journal = []
    rt = runtime(journal=journal.append)
    rt.append({"role": "system", "content": "synthetic system"})
    rt.append({"role": "user", "content": "t" * (300 * 1024)}, pinned=True)  # >256KiB request
    ok, ids = batch(rt, 2)
    assert ok
    big = "line of synthetic output\n" * 4000  # ~100KB: projected, not inline
    shown = rt.admit_result("grep", big, slot_id=ids[0])
    assert shown != big and len(shown) < len(big)
    rt.admit_result("grep", "small synthetic", slot_id=ids[1])
    rt.end_batch()
    msgs = rt.payload_messages()
    assert sum(m["role"] == "assistant" for m in msgs) == 1
    assert [m.get("tool_call_id") for m in msgs[3:]] == ["ca0", "ca1"]
    assert msgs[4]["content"] == "small synthetic"
    # canonical journal: system, task, assistant once, FULL outputs in API order
    assert [m["role"] for m in journal] == ["system", "user", "assistant", "tool", "tool"]
    assert journal[3]["content"] == big
    ref = rt.group_refs()[-1][0]
    page = rt.read_result(ref, 0, 4096)
    assert page["data"] == big.encode()[:4096].decode() and page["outcome"] == "returned"
    meta = rt.metadata()
    assert meta["observed"]["produced_bytes"] == len(big) + len("small synthetic")
    assert "synthetic" not in json.dumps(meta) + repr(rt) and "result://" not in json.dumps(meta)


def test_phase_meters_isolated_and_cache_counted_once():
    rt = runtime()
    p = {"messages": [{"role": "user", "content": "synthetic " * 50}]}
    est = rt.prepare_request(p, phase="helper")
    assert rt.observe_response(est, {"prompt_tokens": 900,
                                     "prompt_tokens_details": {"cached_tokens": 800}},
                               phase="helper") == 900
    assert rt.meter_for("helper").metadata()["calibration_samples"] == 1
    assert rt.meter_for("worker").metadata()["calibration_samples"] == 0   # worker R untouched
    w = rt.prepare_request(p)
    assert rt.observe_response(w, {"input_tokens": 10, "cache_read_input_tokens": 5,
                                   "cache_creation_input_tokens": 0}) == 15
    assert rt.observe_response(w, None) is None   # unknown stays None, not 0
    assert rt.meter_for("worker").metadata()["unknown_usage"] == 1
    samples = rt.meter_for("worker").metadata()["calibration_samples"]
    for partial in ({"input_tokens": 100}, {"input_tokens": 100, "cache_read_input_tokens": 80}):
        assert rt.observe_response(w, partial) is None   # partial total is no sample
    assert rt.meter_for("worker").metadata()["calibration_samples"] == samples
    assert rt.meter_for("worker").metadata()["unknown_usage"] == 3
    r = rt.prepare_request(p, route="responses")
    assert rt.observe_response(r, {"input_tokens": 100, "input_tokens_details": {
        "cached_tokens": 80}}, route="responses") == 100   # trusted route reaches helper


def test_provisional_unknown_outcome_and_pinned_additions():
    journal = []
    rt = runtime(journal=journal.append)
    rt.append({"role": "user", "content": "synthetic task"}, pinned=True)
    ok, ids = batch(rt, 2)
    rt.admit_result("finish", "ok", slot_id=ids[0], provisional=True)
    assert [m["role"] for m in journal] == ["user", "assistant"]   # not executed yet
    media = {"role": "user", "content": [{"type": "image_url", "image_url": {"url": "data:x"}}]}
    rt.admit_result("grep", "partial", slot_id=ids[1], outcome="unknown", additions=[media])
    with pytest.raises(ValueError):
        rt.end_batch()   # provisional still open
    with pytest.raises(ValueError):
        rt.append({"role": "user", "content": "x"})   # no duplicated projected rows
    rt.replace_result("finish", "ok", "final synthetic", slot_id=ids[0])
    rt.end_batch()
    msgs = rt.payload_messages()
    assert msgs[-1] == media and msgs[2]["content"] == "final synthetic"
    assert rt.snapshot_data()["pinned"] == [0, 4]
    refs = rt.group_refs()[0]
    assert rt.read_result(refs[1])["outcome"] == "unknown"
    assert rt.read_result(refs[1])["exit_code"] is None
    assert [m["role"] for m in journal] == ["user", "assistant", "tool", "user", "tool"]


def test_cancel_skips_known_not_executed():
    rt = runtime()
    ok, ids = batch(rt, 3)
    rt.admit_result("grep", "one", slot_id=ids[0])
    assert rt.before_action(ids[1]) is True
    rt.cancel_remaining()
    rt.end_batch()
    refs = rt.group_refs()[0]
    assert [rt.read_result(r)["outcome"] for r in refs] == ["returned", "skipped", "skipped"]


def test_commit_view_cas_stale_pins_success_and_retained_state():
    ext = {"steer": "h1"}
    rt = runtime(external=lambda: dict(ext))
    rt.append({"role": "system", "content": "synthetic system"})
    rt.append({"role": "user", "content": "synthetic task"}, pinned=True)
    ok, ids = batch(rt, 1)
    rt.admit_result("grep", "result body", slot_id=ids[0])
    rt.end_batch()
    ok, ids = batch(rt, 1, "b")                       # newest real batch stays active
    rt.admit_result("grep", "newest body", slot_id=ids[0])
    rt.end_batch()
    snap = rt.snapshot_data()
    m = snap["messages"]
    good, lin = drop_first_group(snap)
    assert lin == (0, 1, None, 4, 5)
    assert not rt.commit_view(snap["bindings"], [m[0], good[2]] + m[4:],
                              lineage=(0, None, 4, 5))   # pin dropped
    assert not rt.commit_view(snap["bindings"], [m[1], m[0]] + good[2:],
                              lineage=(1, 0, None, 4, 5))   # order changed
    ext["steer"] = "h2"
    assert not rt.commit_view(snap["bindings"], good, lineage=lin)   # stale external state
    snap, refs = rt.snapshot_data(), rt.group_refs()
    before = rt.metadata()
    good, lin = drop_first_group(snap)
    assert rt.commit_view(snap["bindings"], good, "artifact", lineage=lin)
    assert rt.payload_messages() == good
    after = rt.metadata()
    assert after["observed"] == before["observed"] and after["meters"] == before["meters"]
    assert after["receipts"] == before["receipts"] and after["epoch"] > before["epoch"]
    assert rt.read_result(refs[0][0])["data"] == "result body"
    assert rt.snapshot_data()["pinned"] == [0, 1] and rt.snapshot_data()["groups"] == [(2,), (3, 4)]
    assert not rt.commit_view(snap["bindings"], good, lineage=lin)   # old bindings now stale


class TrackLock:   # records whether the runtime lock is held; never blocks the test
    held = False
    def __enter__(self):
        self.held = True
    def __exit__(self, *exc):
        self.held = False


def test_commit_view_external_read_outside_lock_and_reentrant_change_rejected():
    lock, seen, hook = TrackLock(), [], {"go": False}
    def ext():
        seen.append(lock.held)
        if hook["go"]:
            hook["go"] = False
            rt.append({"role": "user", "content": "synthetic late"})   # reentrant mutation
        return {"steer": "h1"}
    rt = runtime(external=ext)
    rt._lock = lock
    rt.append({"role": "system", "content": "synthetic system"})
    rt.append({"role": "user", "content": "synthetic task"})
    full_batch(rt, "a")
    full_batch(rt, "b")
    snap = rt.snapshot_data()
    hook["go"] = True
    assert not rt.commit_view(snap["bindings"], *drop_first_group(snap)[:1],
                              lineage=drop_first_group(snap)[1])
    assert rt.payload_messages()[-1]["content"] == "synthetic late"   # append never lost
    snap = rt.snapshot_data()
    good, lin = drop_first_group(snap)
    assert rt.commit_view(snap["bindings"], good, lineage=lin)
    assert rt.payload_messages()[-1]["content"] == "synthetic late"
    assert seen and not any(seen)   # no callback ever ran while the lock was held


@pytest.mark.parametrize("op", ["append_pin", "replace_system", "begin_batch"])
def test_commit_view_cas_not_lost_to_concurrent_mutation(op):
    rt = runtime(external=lambda: {"steer": "h1"})
    rt.append({"role": "system", "content": "synthetic system"})
    rt.append({"role": "user", "content": "synthetic task"})
    full_batch(rt, "a")
    full_batch(rt, "b")
    snap = rt.snapshot_data()
    good, lin = drop_first_group(snap)
    late = ({"role": "user", "content": "synthetic late pin"} if op == "append_pin"
            else {"role": "system", "content": "synthetic system v2"})
    paused, resume, out = threading.Event(), threading.Event(), {}
    orig = rt._retained
    def retained(g):   # pause commit inside the lock, after binding/lineage checks
        if threading.current_thread() is committer and not paused.is_set():
            paused.set()
            resume.wait(5)
        return orig(g)
    rt._retained = retained
    committer = threading.Thread(target=lambda: out.__setitem__(
        "ok", rt.commit_view(snap["bindings"], good, lineage=lin)))
    def mutate():
        try:
            if op == "begin_batch":
                out["batch"] = batch(rt, 1)[0]
            else:
                rt.append(late, pinned=True) if op == "append_pin" else rt.replace_system(late)
        except ValueError as e:
            out["err"] = str(e)
    mutator = threading.Thread(target=mutate)
    committer.start()
    assert paused.wait(5)
    mutator.start()
    mutator.join(1)   # bounded: done if unsynchronized, still blocked if it waits for the lock
    resume.set()
    committer.join(5)
    mutator.join(5)
    assert not committer.is_alive() and not mutator.is_alive()
    final = rt.payload_messages()
    if op == "begin_batch":   # batch base must be the view the CAS left, or the batch rejected
        assert out["ok"] is True and (out.get("batch") is False or "err" in out
                                      or final[:len(good)] == good)
        return
    assert "err" not in out and out["ok"] in (True, False) and late in final   # never lost
    assert rt.snapshot_data()["pinned"][-1 if op == "append_pin" else 0] == final.index(late)


def check_snapshot_shape(snap):
    """Shape the compactor's snapshot(...) consumes; checked without importing it."""
    assert set(snap) == {"messages", "pinned", "groups", "group_kinds", "bindings", "capsule"}
    assert len(snap["group_kinds"]) == len(snap["groups"])
    b, n = snap["bindings"], len(snap["messages"])
    assert b["history_mode"] == "stateless" and "history" not in b
    assert b["effort"] is None or type(b["effort"]) is str
    assert all(type(i) is int and 0 <= i < n for i in snap["pinned"])
    for g in snap["groups"]:
        assert type(g) is tuple and g and all(type(i) is int for i in g)
        assert list(g) == list(range(g[0], g[-1] + 1)) and g[-1] < n   # ordered, contiguous
    assert isinstance(snap["capsule"], dict) and "summary" not in snap["capsule"]
    json.dumps(snap["capsule"])


def test_snapshot_consumable_prompt_groups_survive_cas_and_rejects_open_batch():
    journal = []
    rt = runtime(journal=journal.append, external=lambda: {"steer": "h1"})
    rt.append({"role": "system", "content": "synthetic system"})
    rt.append({"role": "user", "content": "synthetic task"})
    for tag in ("p", "q"):
        ok, ids = batch(rt, 1, tag, kind="prompt" if tag == "q" else "native")
        if tag == "q":   # prompt-kind workflow: snapshot refused while the batch is open
            with pytest.raises(ValueError):
                rt.snapshot_data()
        rt.admit_result("grep", "body " + tag, slot_id=ids[0])
        rt.end_batch()
    snap = rt.snapshot_data()
    check_snapshot_shape(snap)
    assert snap["groups"] == [(2, 3), (4, 5)]
    text = json.dumps(snap["capsule"])
    assert "body p" not in text and "result://" not in text   # no raw body / refs
    good, lin = drop_first_group(snap)
    assert rt.commit_view(snap["bindings"], good, lineage=lin)
    after = rt.snapshot_data()
    check_snapshot_shape(after)
    assert after["groups"] == [(2,), (3, 4)]   # memory unit + kept suffix group, re-indexed
    assert journal[3]["content"] == "body p"   # canonical full outputs untouched


class Img:   # synthetic stand-in shaped like delegate.ImageAsset
    __slots__ = ("path", "mime", "data", "width", "height", "sha256", "source")
    def __init__(self, data, mime="image/png", path="p1"):
        self.path, self.mime, self.data, self.width, self.height = path, mime, data, 2, 2
        self.sha256, self.source = None, "synthetic"


def test_media_canonical_digest_and_pin_cas():
    _, pub, rd = blobs()
    rt = cr.ContextRuntime(Policy(1_000_000, 32_000, 8_000), load_component=loader(),
                           model="m-synth", route="chat", image_type=Img, publisher=pub, reader=rd)
    a = {"role": "user", "content": [Img(b"px1")]}
    b = {"role": "user", "content": [Img(b"px2")]}
    rt.append(a)
    rt.append(b)
    rt.append({"role": "user", "content": "synthetic"})
    moved = {"role": "user", "content": [Img(b"px1", path="elsewhere")]}
    assert rt._digest(a) == rt._digest(moved)   # identity = data sha + mime/dims, not path
    with pytest.raises(ValueError):
        rt._digest({"x": object()})   # unknown objects never repr'd
    cyc = []
    cyc.append(cyc)
    with pytest.raises(ValueError):
        rt._digest(cyc)
    full_batch(rt, "a")
    full_batch(rt, "b")
    snap = rt.snapshot_data()
    good, lin = drop_first_group(snap)
    for bad in ([b, a], [{"role": "user", "content": [Img(b"pxX")]}, b],
                [{"role": "user", "content": [Img(b"px1", "image/jpeg")]}, b]):
        assert not rt.commit_view(snap["bindings"], bad + good[2:], lineage=lin)   # reorder/pixel/mime
    copies = [{"role": "user", "content": [Img(b"px1")]}, {"role": "user", "content": [Img(b"px2")]}]
    assert rt.commit_view(snap["bindings"], copies + good[2:], lineage=lin)   # slotted copies commit
    assert "px1" not in json.dumps(rt.metadata()) + json.dumps(snap["bindings"])


def test_phase_route_meter_keys_and_bad_phase():
    rt = runtime()
    p = {"messages": [{"role": "user", "content": "synthetic " * 20}]}
    for ph in ("worker", "helper", "advisor", "reviewer", "report", "compaction"):
        assert rt.meter_for(ph) is rt.meter_for(ph)
    est = rt.prepare_request(p, phase="compaction")
    rt.observe_response(est, {"prompt_tokens": 50}, phase="compaction")
    assert rt.meter_for("worker").metadata()["calibration_samples"] == 0
    with pytest.raises(ValueError) as e:
        rt.meter_for("secret-phase-x")
    assert "secret-phase-x" not in str(e.value)
    assert rt.metadata()["meters"] <= cr.MAX_METERS


def test_worker_meter_survives_lru_pressure():
    rt = runtime()
    p = {"messages": [{"role": "user", "content": "synthetic " * 20}]}
    rt.observe_response(rt.prepare_request(p), {"prompt_tokens": 40})
    for i in range(cr.MAX_METERS + 3):
        rt.meter_for("helper", model=f"m{i}")
    assert rt.metadata()["meters"] == cr.MAX_METERS
    assert rt.meter_for("worker").metadata()["calibration_samples"] == 1


def test_cancel_finalizes_provisional_finish_after_unknown():
    journal = []
    rt = runtime(journal=journal.append)
    rt.append({"role": "system", "content": "synthetic system"})
    ok, ids = batch(rt, 3)
    rt.admit_result("finish", "ok", slot_id=ids[0], provisional=True)
    rt.admit_result("grep", "partial", slot_id=ids[1], outcome="unknown")
    assert rt.batch_open and rt.guard is not None
    rt.cancel_remaining()
    assert rt.batch_open and rt.guard.pending == 0   # all pending closed; end_batch closes
    rt.end_batch()
    refs = rt.group_refs()[0]
    outs = [(rt.read_result(r)["outcome"], rt.read_result(r)["exit_code"]) for r in refs]
    assert outs == [("skipped", None), ("unknown", None), ("skipped", None)]
    tools = [m["tool_call_id"] for m in journal if m["role"] == "tool"]
    assert tools == ["ca1", "ca0", "ca2"]   # each control/result recorded once
    msgs = rt.payload_messages()
    assert [m.get("tool_call_id") for m in msgs[2:]] == ["ca0", "ca1", "ca2"]
    assert rt.stopped in (True, False) and (rt.reason is None or type(rt.reason) is str)


def test_replace_system_updates_first_system_row_only():
    journal = []
    rt = runtime(journal=journal.append)
    with pytest.raises(ValueError):
        rt.replace_system({"role": "system", "content": "x"})   # no system row yet
    rt.append({"role": "system", "content": "synthetic v1"})
    rt.append({"role": "user", "content": "synthetic task"})
    with pytest.raises(ValueError):
        rt.replace_system({"role": "user", "content": "x"})
    rt.replace_system({"role": "system", "content": "synthetic v2"})
    assert rt.payload_messages()[0]["content"] == "synthetic v2"
    assert rt.snapshot_data()["pinned"][0] == 0 and journal[-1]["content"] == "synthetic v2"
    assert not rt.batch_open and rt.guard is None and rt.stopped is False and rt.reason is None

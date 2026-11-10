"""Compaction commit identity uses snapshot-position lineage, never global content matching.
Real engine modules loaded by file path; synthetic text and tmp_path only."""
import importlib.util
import json
import sys
from pathlib import Path

import pytest

ENGINE = Path(__file__).resolve().parents[1] / "engine"


def _load(name):
    spec = importlib.util.spec_from_file_location(f"_ctxid_{name}", ENGINE / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


MODS = {n: _load(n) for n in ("context_tokens", "context_receipts", "context_guard")}
cp, cc, cr, ce = (_load(n) for n in ("context_policy", "context_compaction",
                                    "context_runtime", "context_engine"))
NOTES = "synthetic descriptive notes"
LONG = "synthetic long line\n" * 1500                 # above inline bytes -> publisher path


def make(tmp_path, pol=None, journal=None):
    pol = pol or cp.Policy(20000, 300, 300, auto_compact=True, compact_fraction=0.7)
    blobs, state = tmp_path / "blobs", {"fail": False}
    blobs.mkdir(exist_ok=True)

    def publish(key, data):
        if state["fail"]:
            raise OSError("synthetic publisher fault")
        (blobs / key).write_bytes(data)

    def read(key, start, n):
        with open(blobs / key, "rb") as f:
            f.seek(start)
            return f.read(n)
    rt = cr.ContextRuntime(pol, load_component=MODS.__getitem__, model="fixture", route="chat",
                           publisher=publish, reader=read, journal=journal)
    rt.append({"role": "system", "content": "synthetic system"})
    rt.append({"role": "user", "content": "synthetic task"}, pinned=True)
    return rt, state


def batch(rt, cid, text):
    """One native batch; identical envelope whenever cid matches (same name/args)."""
    asst = {"role": "assistant", "content": None, "tool_calls": [
        {"id": cid, "type": "function", "function": {"name": "read_file", "arguments": "{}"}}]}
    env = {"role": "tool", "tool_call_id": cid, "content": ""}
    slot = f"slot_{len(rt._store._receipts)}"   # monotonic: group count shrinks on compaction
    assert rt.begin_batch({"model": rt.model, "messages": rt.payload_messages()}, asst, [env], [slot])
    assert rt.before_action(slot) is True
    rt.admit_result("read_file", text, slot_id=slot, outcome="returned", exit_code=0)
    rt.end_batch()
    return list(rt._groups[-1]["ids"])


def auto(rt, charges, seen=None):
    def call(req):
        body = json.loads(req.messages[1]["content"])
        src = body["source_digest"]
        if seen is not None:
            seen.append(body["history"])
        return {"choices": [{"message": {"content": json.dumps(
            {"source_digest": src, "notes": NOTES})}}],
            "usage": {"prompt_tokens": 50, "completion_tokens": 12}}
    return ce.AutomaticCompaction(
        rt, cc, prepare=lambda m, n: {"model": rt.model, "messages": m},
        render=lambda m: {"model": rt.model, "messages": m}, call=call,
        persist=lambda record: "synthetic-artifact",
        charge=lambda response, error: charges.append(response is not None),
        can_call=lambda: True, sync_steers=lambda: None, image_type=None)


@pytest.mark.parametrize("order", ["ABC", "BAC"])
def test_retained_incomplete_group_keeps_its_own_ids_beside_identical_removed_envelope(
        tmp_path, order):
    rt, state = make(tmp_path)
    ids = {}
    for name in order:
        if name == "A":                               # incomplete capture: retention pin
            state["fail"] = True
            ids["A"] = batch(rt, "x", "synthetic partial " + LONG)
            state["fail"] = False
        elif name == "B":                             # same envelope/call ID, full long result
            ids["B"] = batch(rt, "x", "synthetic complete " + LONG)
        else:
            ids["C"] = batch(rt, "y", "synthetic newest")
    rows = {r["id"]: r["msg"] for r in rt._rows}
    a_pair = [rows[i] for i in ids["A"]]
    charges = []
    out = auto(rt, charges).attempt()
    assert out.code == "committed", (out.code, out.before_tokens, out.after_tokens)
    assert charges == [True]
    active = {r["id"]: r["msg"] for r in rt._rows}
    assert [active.get(i) for i in ids["A"]] == a_pair   # A's own engine IDs and full pair
    assert not set(ids["B"]) & set(active)
    assert ids["A"] in [g["ids"] for g in rt._groups if g["kind"] == "native"]
    assert [g["ids"] for g in rt._groups if g["kind"] != "memory"][-1] == ids["C"]


def _memories(rt):
    return [r for r in rt._rows if cc.MEMORY_LABEL in str(r["msg"].get("content"))]


def test_retrigger_keeps_newest_real_batch_and_consolidates_trailing_text(tmp_path):
    rt, _ = make(tmp_path)
    a = batch(rt, "x", "synthetic a " + LONG)
    b = batch(rt, "y", "synthetic b newest real result")
    t_text = "synthetic standalone T " + "t" * 18000
    rt.append({"role": "assistant", "content": t_text})
    rt.append({"role": "user", "content": "synthetic steer " + "s" * 16000}, pinned=True)
    charges, seen = [], []
    ac = auto(rt, charges, seen)
    out = ac.attempt()
    assert out.code == "committed", (out.code, out.before_tokens, out.after_tokens)
    assert [g["kind"] for g in rt._groups] == ["memory", "native", "memory"]
    rows = {r["id"]: r["msg"] for r in rt._rows}
    b_pair = [rows[i] for i in b]
    old_memory = _memories(rt)[0]["msg"]["content"]
    rt.append({"role": "user", "content": "synthetic steer more " + "s" * 17000}, pinned=True)
    out = ac.attempt()
    assert out.code == "committed", (out.code, out.before_tokens, out.after_tokens)
    assert charges == [True, True]
    summarized = [m.get("content") for m in seen[1]]
    assert old_memory in summarized and t_text in summarized
    assert not any(m.get("tool_calls") or m.get("role") == "tool" for m in seen[1])
    active = {r["id"]: r["msg"] for r in rt._rows}
    assert [active.get(i) for i in b] == b_pair and not set(a) & set(active)
    assert [g["ids"] for g in rt._groups if g["kind"] == "native"] == [b]
    assert len(_memories(rt)) == 1 and [g["kind"] for g in rt._groups] == ["memory", "native"]
    assert t_text not in [m.get("content") for m in active.values()]


def _index_bytes(store, ref):
    out, off = [], 0
    while True:
        page = store.read(ref, off, 4096)
        out.append(page["data"])
        if page["eof"]:
            return "".join(out), page["total_bytes"]
        off = page["next_offset"]


def test_three_successes_keep_incomplete_a_and_newest_whole_append_only(tmp_path):
    # Forced transaction mechanics, not a 0.70 threshold claim (threshold tests are separate).
    journal = []
    pol = cp.Policy(200000, 300, 300, auto_compact=True, compact_fraction=0.7)
    rt, state = make(tmp_path, pol, journal.append)
    state["fail"] = True
    a = batch(rt, "x", "synthetic partial " + LONG)         # incomplete: permanently retained
    state["fail"] = False
    b = batch(rt, "x", "synthetic complete " + LONG)        # same envelope, full capture
    c = batch(rt, "y", "synthetic newest C " + "c" * 12000)
    a_refs = rt.group_refs()[0]
    a_rec = [rt._store.read(r, 0, 4096) for r in a_refs]
    a_pair = [{r["id"]: r["msg"] for r in rt._rows}[i] for i in a]
    assert not any(rt._store.capture_complete(r) for r in a_refs)
    h = rt._store.index_ref()
    h_bytes = _index_bytes(rt._store, h)
    charges = []
    ac = auto(rt, charges)
    real = ac.compactor.attempt
    ac.compactor.attempt = lambda snap, force=False: real(snap, force=True)

    def succeed(n, newest, gone):
        before_j = json.dumps(journal)
        receipts = len(rt._store._receipts)
        out = ac.attempt()
        assert out.code == "committed", (out.code, out.before_tokens, out.after_tokens)
        assert out.after_tokens < out.before_tokens <= pol.input_limit_tokens
        assert out.after_tokens <= pol.compact_target_tokens
        assert charges == [True] * n and ac.compactor.calls == n
        assert ac.compactor.failures == 0 and ac._ids == {}
        assert json.dumps(journal[:len(json.loads(before_j))]) == before_j  # never rewritten
        assert len(rt._store._receipts) == receipts                       # no command replay
        active = {r["id"]: r["msg"] for r in rt._rows}
        assert [active.get(i) for i in a] == a_pair
        assert [rt._store.read(r, 0, 4096) for r in a_refs] == a_rec
        assert len(_memories(rt)) == 1
        # memory takes the removed source position, after retained A
        assert [g["kind"] for g in rt._groups] == ["native", "memory", "native"]
        assert [rt._groups[0]["ids"], rt._groups[2]["ids"]] == [a, newest]
        assert all(i in active for i in newest) and not set(gone) & set(active)
        calls = [t["id"] for m in active.values() for t in m.get("tool_calls") or []]
        results = [m["tool_call_id"] for m in active.values() if m.get("role") == "tool"]
        assert sorted(calls) == sorted(results)                           # no orphan pairs
        assert _index_bytes(rt._store, h) == h_bytes                      # frozen H prefix
        return _memories(rt)[0]["id"]

    m1 = succeed(1, c, b)
    n = len(journal)
    d = batch(rt, "x", "synthetic complete " + LONG)        # reuses B envelope and result
    assert len(journal) > n and rt._store.index_ref() != h
    m2 = succeed(2, d, c + [m1])
    e = batch(rt, "z", "synthetic newest E distinct")
    succeed(3, e, d + [m2])


BIND = {"run": "r1", "model": "m1", "route": "chat", "effort": "high",
        "history_mode": "stateless"}


def _kernel(n=4):
    m = [{"role": "system", "content": "SYS"}, {"role": "user", "content": "TASK"}]
    groups = []
    for i in range(n):
        groups.append((len(m), len(m) + 1))
        m += [{"role": "user", "content": f"q{i}" + "x" * 400},
              {"role": "assistant", "content": f"a{i}" + "y" * 400}]
    calls, commits = [], []

    def call(req):
        calls.append(req)
        src = json.loads(req.messages[1]["content"])["source_digest"]
        return cc.SummaryReply(json.dumps({"source_digest": src, "notes": "s"}),
                               50, 10, "m1", "chat", "high")
    comp = cc.Compactor(
        cp.Policy(1000, 100, 100, auto_compact=True),
        measure=lambda p: cc.Estimate(len(json.dumps(p["messages"])) // 4, ()),
        render_worker=lambda ms: {"messages": list(ms)}, call_summary=call,
        persist=lambda r: "artifact",
        commit=lambda e, c, ref, *, lineage: commits.append(lineage) or True,
        get_bindings=lambda: dict(BIND))
    return m, tuple(groups), comp, calls, commits


@pytest.mark.parametrize("kinds", [["native"] * 3, ["native"] * 5, ["native"] * 3 + ["batch"],
                                   ["native"] * 3 + [None], "nnnn", {"native": 1}, 7])
def test_invalid_group_kinds_rejected_before_any_call(kinds):
    m, g, comp, calls, commits = _kernel()
    with pytest.raises(cc.CompactionError) as e:
        cc.snapshot(m, pinned=(0, 1), groups=g, bindings=BIND, group_kinds=kinds)
    assert e.value.code == "invalid_group_kinds" and calls == [] and commits == []


def test_tampered_group_kinds_rejected_without_call():
    m, g, comp, calls, commits = _kernel()
    snap = cc.snapshot(m, pinned=(0, 1), groups=g, bindings=BIND,
                       group_kinds=["native", "native", "native", "memory"])
    bad = cc.replace(snap, group_kinds=("native",) * 4) if hasattr(cc, "replace") else None
    import dataclasses
    bad = dataclasses.replace(snap, group_kinds=("native",) * 4)
    assert comp.attempt(bad, force=True).code == "snapshot_tampered"
    assert calls == [] and commits == []


def test_kinds_protect_last_real_group_and_omitted_kinds_keep_final_group():
    m, g, comp, calls, commits = _kernel()
    kinds = ["native", "native", "native", "memory"]
    snap = cc.snapshot(m, pinned=(0, 1), groups=g, bindings=BIND, group_kinds=kinds)
    assert comp.attempt(snap, force=True).committed
    assert set(g[2]) <= set(commits[-1]) and not set(g[3]) & set(commits[-1])
    m, g, comp, calls, commits = _kernel()
    plain = cc.snapshot(m, pinned=(0, 1), groups=g, bindings=BIND)
    assert plain.group_kinds is None
    assert comp.attempt(plain, force=True).committed
    assert set(g[3]) <= set(commits[-1])

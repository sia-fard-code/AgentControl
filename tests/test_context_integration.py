"""Integration: ContextRuntime.snapshot_data -> context_compaction.snapshot -> Compactor -> persist -> commit_view.
Real engine modules loaded by file path; synthetic fixtures and tmp_path only."""
import importlib.util
import json
import sys
from pathlib import Path

import pytest

ENGINE = Path(__file__).resolve().parents[1] / "engine"


def _load(name):
    spec = importlib.util.spec_from_file_location(f"_ctxint_{name}", ENGINE / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod   # dataclasses resolve their module via sys.modules
    spec.loader.exec_module(mod)
    return mod


MODS = {n: _load(n) for n in ("context_tokens", "context_receipts", "context_guard")}
cp, cc, cr = _load("context_policy"), _load("context_compaction"), _load("context_runtime")
ct = MODS["context_tokens"]
SECRET = "SYNTH-SECRET-7f3a"
NOTES = "synthetic descriptive notes"


class ImageAsset:   # slotted stand-in with the engine image shape (data/mime/width/height)
    __slots__ = ("data", "mime", "width", "height")

    def __init__(self, data, mime, width, height):
        self.data, self.mime, self.width, self.height = data, mime, width, height


def result_text(n):
    return f"{SECRET} https://example.invalid/{n}\n" + (f"line{n} " + "z" * 60 + "\n") * 36


def test_repro_image_asset_in_view_blocks_every_batch(tmp_path):
    # Finding: begin_batch compares payload with a fresh deepcopy of the view; ImageAsset (like the
    # real delegate.ImageAsset) has no __eq__, so any image object in the active view -> projection_error.
    rt, _ = make(tmp_path, media=ImageAsset(b"\x89PNG-synthetic", "image/png", 64, 64))
    batch(rt, 0)


def make(tmp_path, route="chat", effort=None, ext=None, media=None):
    media = media or {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA", "detail": "low"}}
    pol = cp.Policy(6000, 300, 300, auto_compact=True, compact_fraction=0.5)
    blobs = tmp_path / "blobs"
    blobs.mkdir(exist_ok=True)

    def publish(key, data):
        (blobs / key).write_bytes(data)   # None ack = published

    def read(key, start, n):
        with open(blobs / key, "rb") as f:
            f.seek(start)
            return f.read(n)

    journal = []
    rt = cr.ContextRuntime(pol, load_component=MODS.__getitem__, model="fixture", route=route,
                           effort=effort, image_type=ImageAsset, journal=journal.append,
                           publisher=publish, reader=read,
                           external_bindings=None if ext is None else (lambda: dict(ext)))
    rt.append({"role": "system", "content": "synthetic system"})
    rt.append({"role": "user", "content": "synthetic task"}, pinned=True)
    rt.append({"role": "developer", "content": "synthetic skill"})
    rt.append({"role": "user", "content": [{"type": "text", "text": "see"}, media]})
    return rt, journal


def batch(rt, n, outcome="returned"):
    cid, sid = f"call_{n}", f"slot_{n}"
    asst = {"role": "assistant", "content": None, "tool_calls": [
        {"id": cid, "type": "function", "function": {"name": "read_file", "arguments": "{}"}}]}
    env = {"role": "tool", "tool_call_id": cid, "content": ""}
    assert rt.begin_batch({"model": rt.model, "messages": rt.payload_messages()}, asst, [env], [sid])
    assert rt.before_action(sid) is True
    rt.admit_result("read_file", result_text(n), slot_id=sid, outcome=outcome)
    rt.end_batch()


def snap(rt):
    return cc.snapshot(**rt.snapshot_data(), image_type=ImageAsset)


def echo(requests, **over):
    def call(req):
        requests.append(req)
        src = json.loads(req.messages[1]["content"])["source_digest"]
        kw = dict(text=json.dumps({"source_digest": src, "notes": NOTES + " " + SECRET}),
                  input_tokens=50, output_tokens=12, model=req.model, route=req.route, effort=req.effort)
        kw.update(over)
        return cc.SummaryReply(**kw)
    return call


def compactor(rt, tmp_path, call, persist=None):
    out = tmp_path / "compaction"
    out.mkdir(exist_ok=True)
    stored = []

    def _persist(record):
        p = out / f"{len(stored)}.json"
        p.write_text(json.dumps(record))
        stored.append(p)
        return f"cmp-{len(stored)}"
    c = cc.Compactor(rt.policy, measure=rt.prepare_request,
                     render_worker=lambda m: {"model": rt.model, "messages": m},
                     call_summary=call, persist=persist or _persist, commit=rt.commit_view,
                     get_bindings=rt.bindings)
    return c, stored


def frozen(rt, journal):
    return (rt.payload_messages(), rt.bindings(), len(journal), rt.metadata())


def pairs_ok(msgs):
    open_ids = set()
    for m in msgs:
        if m["role"] == "tool":
            assert m["tool_call_id"] in open_ids
            open_ids.discard(m["tool_call_id"])
        else:
            assert not open_ids
            open_ids = {c["id"] for c in m.get("tool_calls") or ()}
    assert not open_ids


def test_threshold_compaction_commits_only_active_view(tmp_path):
    rt, journal = make(tmp_path)
    for n in range(4):
        batch(rt, n, outcome="unknown" if n == 1 else "returned")
    est = rt.prepare_request({"model": rt.model, "messages": rt.payload_messages()})
    assert type(est) is ct.Estimate
    pol = rt.policy
    assert est.tokens >= pol.compact_trigger_tokens, (est.tokens, pol.compact_trigger_tokens)
    old_msgs, refs = rt.payload_messages(), rt.group_refs()
    newest_ids = list(rt._groups[-1]["ids"])
    old_ids = {r["id"] for r in rt._rows}
    pairs_ok(old_msgs)
    meta0, jlen = rt.metadata(), len(journal)
    reqs = []
    c, stored = compactor(rt, tmp_path, echo(reqs))
    out = c.attempt(snap(rt))
    assert out.code == "committed" and out.committed and out.removed_groups == 3, out
    assert out.after_tokens <= pol.compact_target_tokens < pol.compact_trigger_tokens <= out.before_tokens
    req = reqs[0]
    assert (req.model, req.route, req.effort) == ("fixture", "chat", None)
    assert [m["role"] for m in req.messages] == ["system", "user"]
    assert not any("tool_calls" in m for m in req.messages)
    new = rt.payload_messages()
    pairs_ok(new)
    assert new[:4] == old_msgs[:4]   # system/task/skill/media pins kept by value and order
    frame = f"[ENGINE REFERENCE INDEX: {refs[-1][0].rsplit('/', 1)[0]}/index/4 "
    assert new[4]["role"] == "assistant" and new[4]["content"].startswith(frame)
    assert cc.MEMORY_LABEL + NOTES in new[4]["content"]   # untrusted notes stay after the frame
    assert new[5:] == old_msgs[-2:]   # last complete group kept
    meta1 = rt.metadata()
    assert meta1["compactions"] == 1 and meta1["groups"] == 2 and meta1["pinned"] == 4
    assert rt.snapshot_data()["groups"] == [(4,), (5, 6)]
    memory_id = rt._rows[4]["id"]
    assert memory_id not in old_ids and not rt._rows[4]["pinned"]
    assert rt._groups == [
        {"kind": "memory", "ids": [memory_id], "refs": []},
        {"kind": "native", "ids": newest_ids, "refs": refs[-1]},
    ]
    assert [r["id"] for r in rt._rows[5:]] == newest_ids
    for k in ("observed", "receipts", "meters"):
        assert meta1[k] == meta0[k], k
    assert meta1["receipts"]["outcomes"]["unknown"] == 1
    assert len(journal) == jlen   # canonical journal unchanged by compaction
    for n, group in enumerate(refs):   # lossless own-run pages for every removed result
        page = rt.read_result(group[0], 0, 4096)
        assert page["content_complete"] and page["data"] == result_text(n)
    assert [m["content"] for m in journal if m["role"] == "tool"] == [result_text(n) for n in range(4)]
    assert json.loads(stored[0].read_text())["removed_groups"] == 3


@pytest.mark.parametrize("over,code", [({"tool_calls": ({"id": "x"},)}, "tool_calls_rejected"),
                                       ({"route": "responses"}, "route_mismatch"),
                                       ({"text": "{not json"}, "bad_schema")])
def test_bad_reply_keeps_view_charges_once_no_replay(tmp_path, over, code):
    rt, journal = make(tmp_path)
    for n in range(4):
        batch(rt, n)
    before, reqs = frozen(rt, journal), []
    c, stored = compactor(rt, tmp_path, echo(reqs, **over))
    s = snap(rt)
    assert c.attempt(s).code == code and c.calls == 1 and c.failures == 1
    assert c.attempt(s, force=True).code == "already_attempted" and len(reqs) == 1
    assert frozen(rt, journal) == before and not stored


def test_stale_operator_binding_during_call_or_persist_rejects(tmp_path):
    for stage in ("call", "persist"):
        ext = {"operator": "v1"}
        rt, journal = make(tmp_path / stage if (tmp_path / stage).mkdir() is None else None, ext=ext)
        for n in range(4):
            batch(rt, n)
        before, reqs = frozen(rt, journal), []
        inner = echo(reqs)

        def call(req):
            if stage == "call":
                ext["operator"] = "v2"
            return inner(req)

        def persist(record):
            ext["operator"] = "v2"
            return "cmp-1"
        c, _ = compactor(rt, tmp_path / stage, call, persist if stage == "persist" else None)
        out = c.attempt(snap(rt))
        assert out.code == "stale_bindings" and not out.committed and c.calls == 1, (stage, out)
        assert rt.payload_messages() == before[0] and len(journal) == before[2]
        assert rt.commit_view(before[1], rt.payload_messages()[:4],
                              lineage=(0, 1, 2, None)) is False   # CAS refuses old bindings


def test_metadata_and_bindings_carry_no_content(tmp_path):
    rt, journal = make(tmp_path)
    for n in range(4):
        batch(rt, n)
    c, _ = compactor(rt, tmp_path, echo([]))
    out = c.attempt(snap(rt))
    assert out.committed, out
    data = rt.snapshot_data()
    blob = json.dumps([rt.metadata(), rt.bindings(), data["capsule"], repr(rt), repr(out), repr(c)])
    for bad in (SECRET, "example.invalid", "synthetic", NOTES, "line0"):
        assert bad not in blob, bad
    assert {"route", "model", "effort", "history_mode", "epoch"} <= set(data["bindings"])
    assert data["bindings"]["effort"] is None

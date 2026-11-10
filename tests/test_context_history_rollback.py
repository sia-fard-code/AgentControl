"""Synthetic history rollback and receipt boundaries; no provider or live state."""
import copy
import json

import pytest

from test_context_compaction_history import MODS, batch, body, cc, compactor, make, memories, snap

ReceiptError = MODS["context_receipts"].ReceiptError  # the runtime's own isolated module copy


def frozen(rt):
    data = rt.snapshot_data()
    return (data["messages"], data["pinned"], data["groups"], copy.deepcopy(rt._groups),
            copy.deepcopy(rt._rows), [m["content"] for m in memories(data["messages"])])


@pytest.mark.parametrize("fault,code", [
    ("summary_state", "stale_bindings"), ("persist_state", "stale_bindings"),
    ("persist_fail", "storage_error"), ("cas", "commit_rejected"),
    ("malformed", "bad_schema"), ("usage", "missing_usage"),
    ("binding", "source_mismatch"), ("nonreducing", "no_reduction"),
])
def test_failed_attempt_preserves_history_and_real_charges(tmp_path, fault, code):
    rt, _ = make(tmp_path)
    ext = {"evidence": {"candidate_seq": 1}, "unknown": True}
    rt._external = lambda: copy.deepcopy(ext)
    journal = []
    rt._journal = journal.append
    for n in range(3):
        batch(rt, n)
    seed, _ = compactor(rt, tmp_path)
    assert seed.attempt(snap(rt), force=True).committed
    batch(rt, 3)
    before, transcript = frozen(rt), copy.deepcopy(journal)
    charges, published, commits = [], [], []

    def call(req):
        charges.append({"input": 50, "output": None if fault == "usage" else 12})
        if fault == "summary_state":
            ext["evidence"]["candidate_seq"] += 1
        text = json.dumps({"source_digest": "wrong" if fault == "binding" else req.source_digest,
                           "notes": "synthetic rollback notes"})
        return cc.SummaryReply("{" if fault == "malformed" else text,
                               None if fault == "usage" else 50, 12,
                               req.model, req.route, req.effort)

    def persist(record):
        if fault == "persist_fail":
            raise OSError("synthetic publication fault")
        path = tmp_path / "published.json"
        path.write_text(json.dumps(record))
        published.append(path)
        if fault == "persist_state":
            ext["unknown"] = False
        return path.name

    def commit(*args, lineage):
        commits.append(args)
        return False if fault == "cas" else rt.commit_view(*args, lineage=lineage)

    measure = (lambda packet: cc.Estimate(1000)) if fault == "nonreducing" else rt.prepare_request
    c = cc.Compactor(rt.policy, measure=measure, render_worker=lambda m: {"messages": m},
                     call_summary=call, persist=persist, commit=commit, get_bindings=rt.bindings)
    out = c.attempt(snap(rt), force=True)
    assert out.code == code and not out.committed and out.calls == c.calls == 1
    assert frozen(rt) == before and journal == transcript
    assert charges == [{"input": 50, "output": None if fault == "usage" else 12}]
    assert len(published) == (1 if fault in ("persist_state", "cas") else 0)
    assert len(commits) == (1 if fault == "cas" else 0)
    assert all(p.exists() for p in published)


def test_two_slot_capture_fault_seals_outcome_retains_group_and_capacity_keeps_reader(tmp_path):
    rt, state = make(tmp_path, window=20000)
    state["fail"] = True
    batch(rt, 0, width=2, text=lambda n, k: body(n, k) + "q" * 20000, exit_code=3)
    state["fail"] = False
    refs = rt.group_refs()[0]
    for ref in refs:
        page = rt.read_result(ref)
        assert page["state"] == "recorded" and not page["content_complete"]
        assert not rt._store.capture_complete(ref)
        assert (page["outcome"], page["exit_code"]) == ("returned", 3)
        with pytest.raises(ReceiptError):
            rt._store.record(ref, "again", outcome="returned", exit_code=0)
        assert rt.read_result(ref) == page
    assert set(rt.snapshot_data()["groups"][0]) <= set(rt.snapshot_data()["pinned"])
    frozen_index = rt.bindings()["reference_index_ref"]
    first = rt.read_result(frozen_index, 0, 17)
    # Synthetic unexecuted reservations fill the real configured capacity; no actions run.
    meta = rt._store.metadata()
    limit = meta["limits"]["max_receipts"]
    remaining = limit - meta["receipts"]
    width = meta["limits"]["max_batch"]
    for start in range(0, remaining, width):
        rt._store.reserve_batch([f"capacity-only-{i}" for i in range(start, min(start + width, remaining))])
    assert rt._store.metadata()["receipts"] == limit
    before = frozen(rt)
    asst = {"role": "assistant", "content": "synthetic preflight"}
    assert not rt.begin_batch({"messages": rt.payload_messages()}, asst,
                              [{"role": "tool", "tool_call_id": "late", "content": ""}], ["late"])
    assert frozen(rt) == before and not rt.batch_open
    assert rt._store.metadata()["receipts"] == limit
    assert rt.read_result(frozen_index, 0, 17) == first
    assert all(rt.read_result(r)["exit_code"] == 3 for r in refs)

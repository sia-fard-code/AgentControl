import json
from types import SimpleNamespace

import pytest

from engine import context_guard as cg


class Ctx:
    def __init__(self, limit=1_000_000, flags=("estimated",)):
        self.policy = SimpleNamespace(input_limit_tokens=limit)
        self.flags, self.epoch, self.scale = flags, 0, 1

    def estimate(self, packet):
        return SimpleNamespace(tokens=len(json.dumps(packet)) * self.scale // 4, flags=self.flags)


class Receipts:
    def __init__(self):
        self.records = []

    def reserve_batch(self, slot_ids):
        return [SimpleNamespace(slot_id=s, ref=f"r-{s}", fallback_text=f"[result receipt r-{s}]") for s in slot_ids]

    def record(self, ref, text, *, outcome, exit_code=None, final=True):
        self.records.append((ref, len(text), outcome, final))


def req(n):
    calls = [{"id": f"c{i}", "type": "function", "function": {"name": "read_file", "arguments": "{}"}}
             for i in range(n)]
    return {"model": "m", "messages": [{"role": "user", "content": "synthetic task"},
                                        {"role": "assistant", "content": None, "tool_calls": calls}]}


def envs(n):
    return [{"role": "tool", "tool_call_id": f"c{i}"} for i in range(n)]


def guard(ctx=None):
    rec, pub = Receipts(), []
    return cg.Guard(context=ctx or Ctx(), receipts=rec, publish=pub.append, reader_registered=True), rec, pub


def test_legacy_guard_unchanged_and_never_closing_before_action():
    g = cg.Guard()
    assert "context_policy" not in g.metadata() and g.metadata()["limits"]["request"] == cg.REQUEST_LIMIT
    assert g.before_action("s0") is True
    assert cg.fits("x" * 10, 10) and not cg.fits("x" * 11, 10)


def test_model_fits_measures_whole_obj_in_tokens_ignoring_byte_limit():
    big = {"messages": [{"role": "user", "content": "s" * 400_000}]}   # > 256 KiB wire
    assert cg.fits(big, 1, context=Ctx())
    assert not cg.fits(big, 10**9, context=Ctx(limit=1000))


@pytest.mark.parametrize("ctx,reason", [
    (Ctx(flags=("history_unobservable",)), "history_unobservable"),
    (SimpleNamespace(policy=SimpleNamespace(input_limit_tokens=10), estimate=lambda p: 1 / 0), "counter_error")])
def test_model_fits_classifies_faults_without_byte_fallback(ctx, reason):
    with pytest.raises(cg.ContextBudgetError) as e:
        cg.fits({"messages": []}, 10**9, context=ctx)
    assert e.value.reason == reason


def test_model_guard_continues_past_legacy_byte_ceilings_and_metadata_is_private():
    g, rec, pub = guard()
    src, ids = req(3), ["slot-a", "slot-b", "slot-c"]
    before, big = json.dumps(src), "synthetic line\n" * 20_000
    assert g.start_batch(src, 3, result_envelopes=envs(3), slot_ids=ids) and g.pending == 3
    for s in ids:
        assert g.before_action(s)
        assert len(g.admit("read_file", big, slot_id=s).encode()) <= 16384
    assert not g.closing() and g.reason is None and not g.exhausted and g.pending == 0
    assert g.read == g.output == 3 * len(big) > cg.OUTPUT_BUDGET
    assert json.dumps(src) == before and len(rec.records) == 3 and pub
    m = g.metadata()
    assert m["context_policy"] == "model-v1" and m["input_limit_tokens"] == 1_000_000
    assert m["current_tokens"] == g.base and g.batch is None and m["units"]["current_tokens"] == "estimated_tokens"
    assert str(cg.REQUEST_LIMIT) not in json.dumps(m) and not any(x in json.dumps(m) for x in ("synthetic", "slot-", "r-"))


def test_slots_need_explicit_ids_except_empty_batch():
    g, _, _ = guard()
    with pytest.raises(Exception) as e:
        g.start_batch(req(1), 1, result_envelopes=envs(1))
    assert e.value.reason == "projection_error"
    assert g.start_batch(req(0), 0)


def test_epoch_drift_stop_reflects_on_guard_and_skip_is_explicit():
    ctx = Ctx(limit=100_000)
    g, rec, _ = guard(ctx)
    assert g.start_batch(req(2), 2, result_envelopes=envs(2), slot_ids=["slot-a", "slot-b"])
    ctx.epoch, ctx.scale = 1, 10**6   # new counting epoch: nothing fits any more
    assert g.before_action("slot-a") is False
    assert g.closing() and g.exhausted and g.reason == "model_window_full"
    assert g.skip(slot_id="slot-a", reason_code="admission_closing").startswith("[result receipt")
    assert g.pending == 1 and rec.records[-1][2] == "skipped"


def test_provisional_replace_and_provider_stop():
    g, _, _ = guard()
    assert g.start_batch(req(1), 1, result_envelopes=envs(1), slot_ids=["slot-a"])
    assert g.admit("finish", "ok", slot_id="slot-a", provisional=True) == "ok" and g.pending == 0
    assert g.replace_result("finish", "ok", "final decision", slot_id="slot-a") == "final decision"
    assert g.output == len("ok") + len("final decision")
    g.stop("provider", provider=True)
    assert g.closing() and g.reason == "provider" and g.provider_rejected and g.metadata()["reason"] == "provider"


def test_model_mode_rejects_missing_hooks_before_actions():
    with pytest.raises(Exception) as e:
        cg.Guard(context=Ctx(), receipts=None, publish=[].append, reader_registered=True)
    assert e.value.reason == "invalid_state"
    with pytest.raises(Exception) as e:
        cg.Guard(context=Ctx(), receipts=Receipts(), publish=[].append)
    assert e.value.reason == "invalid_state"

import json
from types import SimpleNamespace

from engine.context_admission import ModelAdmission


class FakeContext:
    """Counter fake: tokens = f(whole packet); the admission logic under test is real."""

    def __init__(self, limit, count=lambda s: len(s) // 4, flags=("estimated",)):
        self.policy = SimpleNamespace(input_limit_tokens=limit)
        self.count, self.flags, self.calls = count, flags, 0

    def estimate(self, packet):
        self.calls += 1
        return SimpleNamespace(tokens=self.count(json.dumps(packet)), flags=self.flags)


class FakeReceipts:
    def __init__(self):
        self.reserved, self.records = [], []

    def reserve_batch(self, slot_ids):
        self.reserved.append(tuple(slot_ids))
        return [SimpleNamespace(slot_id=s, ref=f"r-{s}", fallback_text=f"[result receipt r-{s}: use reader]")
                for s in slot_ids]

    def record(self, ref, text, *, outcome, exit_code=None, final=True):
        self.records.append((ref, len(text), outcome, exit_code, final))


def request(n=1):
    calls = [{"id": f"c{i}", "type": "function", "function": {"name": "read_file", "arguments": "{}"}}
             for i in range(n)]
    return {"model": "m", "messages": [{"role": "user", "content": "synthetic task"},
                                        {"role": "assistant", "content": None, "tool_calls": calls}]}


def make(limit=1_000_000 - 32_768 - 50_000, **kw):
    ctx, rec, pub = FakeContext(limit, **kw), FakeReceipts(), []
    adm = ModelAdmission(ctx, receipts=rec, publish=pub.append, reader_registered=True)
    return adm, ctx, rec, pub


def test_large_result_kept_whole_in_receipt_and_inline_bounded_by_declared_preview():
    adm, _, rec, pub = make()
    big = "synthetic line\n" * 20_000   # ~300 KB, above every legacy byte ceiling
    assert adm.start_batch(request(), 1, result_envelopes=[{"role": "tool", "tool_call_id": "c0"}],
                           slot_ids=["s0"])
    out = adm.admit("read_file", big, slot_id="s0")
    assert len(out.encode("utf-8")) <= 16384 and big.startswith(out.split("\n[context]")[0])
    assert not adm.closing()
    assert pub[-1]["messages"][-1] == {"role": "tool", "tool_call_id": "c0", "content": out}
    assert rec.records == [("r-s0", len(big), "returned", None, True)]   # whole result preserved


def envs(n):
    return [{"role": "tool", "tool_call_id": f"c{i}"} for i in range(n)]


def start(adm, n=1):
    return adm.start_batch(request(n), n, result_envelopes=envs(n), slot_ids=[f"s{i}" for i in range(n)])


def test_cumulative_read_and_output_above_old_lifetime_caps_do_not_abort():
    adm, *_ = make()
    chunk = "y" * (100 * 1024)
    for _ in range(4):   # 400 KiB read/output total > 128 KiB READ and 192 KiB OUTPUT
        assert start(adm) and adm.admit("read_file", chunk, slot_id="s0")   # observed stats only
    assert not adm.closing() and adm.metadata()["observed"]["read_bytes"] == 4 * len(chunk)


def test_all_slot_R_forecast_fails_before_publication():
    adm, _, rec, pub = make(limit=50)
    assert start(adm, 3) is False
    assert pub == [] and adm.reason == "model_window_full" and rec.records == []


def test_128_slots_valid_and_129_rejected():
    adm, _, rec, pub = make()
    assert start(adm, 128) and len(rec.reserved[-1]) == 128 and adm.pending == 128
    adm2, *_ = make()
    try:
        start(adm2, 129)
        assert False
    except Exception as exc:
        assert exc.reason == "projection_error"


def test_duplicate_or_mismatched_slot_ids_rejected():
    adm, *_ = make()
    for ids in (["a", "a"], ["a"]):
        try:
            adm.start_batch(request(2), 2, result_envelopes=envs(2), slot_ids=ids)
            assert False
        except Exception as exc:
            assert exc.reason == "projection_error"


def test_restore_whole_R_when_local_fallback_fails_under_nonmonotone_counter():
    # nonlinear cross-message counter: any slot holding "x" content while another holds a
    # fallback is penalised, so a local fallback can fail while the all-fallback R fits.
    def count(s):
        return 10_000 if ("first-done" in s and "use reader" in s) else 10
    adm, _, rec, pub = make(limit=1000, count=count)
    assert start(adm, 2)
    out = adm.admit("grep", "first-done", slot_id="s0")
    assert out == "[result receipt r-s0: use reader]"   # whole R restored
    assert pub[-1] == pub[0] and not adm.closing()
    assert rec.records[0][2] == "returned"               # outcome kept despite fallback


def test_compact_is_lazy_and_used_whole():
    adm, *_ = make(limit=1000, count=lambda s: 5000 if "BIG" in s else 10)
    called = []
    assert start(adm)
    assert adm.admit("run_command", "short", slot_id="s0", compact=lambda: called.append(1)) == "short"
    assert called == []
    assert start(adm)
    assert adm.admit("run_command", "BIG" * 3000, slot_id="s0", compact=lambda: "exit=0 ref") == "exit=0 ref"


def test_json_reader_results_never_prefix_sliced():
    adm, *_ = make(limit=1000, count=lambda s: 5000 if "BIG" in s else 10)
    assert start(adm)
    payload = json.dumps({"text": "BIG" * 20000, "next_offset": 9})
    out = adm.admit("read_command_output", payload, slot_id="s0")
    assert out == "[result receipt r-s0: use reader]"


def test_clip_candidates_checked_whole_packet_not_monotone():
    # 16k clip fails, 8k clip fits, though an even shorter clip would also fit
    adm, *_ = make(limit=1000, count=lambda s: 10 if len(s) < 600 or 6000 < len(s) < 12000 else 5000)
    assert start(adm)
    out = adm.admit("read_file", "z" * 40000, slot_id="s0")
    assert len(out.encode("utf-8")) == 8192 and out.startswith("z" * 8000)   # notice inside budget
    assert out.endswith("do not rerun to recover it.")


def test_replace_result_targets_slot_identity_despite_duplicate_previous_text():
    adm, _, rec, pub = make()
    assert start(adm, 2)
    adm.admit("finish", "ok", slot_id="s0", provisional=True)
    adm.admit("finish", "ok", slot_id="s1")
    assert adm.replace_result("finish", "ok", "decided", slot_id="s0") == "decided"
    tools = [m["content"] for m in pub[-1]["messages"] if m.get("role") == "tool"]
    assert tools == ["decided", "ok"]
    assert [r[2:] for r in rec.records] == [(None, None, False), ("returned", None, True), ("returned", None, True)]
    try:   # a finalized slot can neither be re-admitted nor replaced again
        adm.replace_result("finish", "ok", "again", slot_id="s1")
        assert False
    except Exception as exc:
        assert exc.reason == "invalid_state"


def test_live_base_appends_not_counted_twice_and_caller_packet_unchanged():
    adm, *_ = make()
    req = request()
    before = json.dumps(req)
    assert adm.start_batch(req, 1, result_envelopes=envs(1), slot_ids=["s0"])
    req["messages"].append({"role": "tool", "tool_call_id": "c0", "content": "live append"})
    adm.admit("grep", "hit", slot_id="s0")
    assert sum(1 for m in adm.packet["messages"] if m.get("role") == "tool") == 1
    req["messages"].pop()
    assert json.dumps(req) == before
    adm.packet["messages"].clear()   # defensive copy
    assert len(adm.packet["messages"]) == 3


def test_wire_plus_native_assistant_envelope_and_non_ascii_escaped_size():
    adm, ctx, _, pub = make(limit=200, count=lambda s: len(s) // 10)
    wire = {"model": "m", "messages": [{"role": "user", "content": "t"}]}
    asst = {"role": "assistant", "content": None, "tool_calls": [{"id": "c0"}]}
    env = {"role": "tool", "tool_call_id": "c0", "name": "grep"}
    assert adm.start_batch([wire, asst], 1, result_envelopes=[env], slot_ids=["s0"])
    text = "\u00e9" * 400   # 800 UTF-8 bytes, but json.dumps escapes to 2400 chars (> limit)
    out = adm.admit("grep", text, slot_id="s0", envelope=env)
    assert out != text and pub[-1]["messages"][1] == asst
    assert {k: v for k, v in pub[-1]["messages"][-1].items() if k != "content"} == env
    try:
        adm.start_batch([wire, asst, asst], 0, result_envelopes=[], slot_ids=[])
        assert False
    except Exception as exc:
        assert exc.reason == "projection_error"


def test_real_meter_and_policy_admit_result_above_256kib():
    from engine.context_policy import Policy
    from engine.context_tokens import ContextMeter
    meter = ContextMeter(Policy(1_000_000, 32_768, 50_000), route="synthetic", model="synthetic-model")
    adm = ModelAdmission(meter, receipts=FakeReceipts(), publish=[].append, reader_registered=True)
    req = request()
    req["messages"][0]["content"] = "synthetic words here\n" * 15_000   # ~315 KB request
    assert adm.start_batch(req, 1, result_envelopes=envs(1), slot_ids=["s0"])
    assert adm.admit("read_file", "hit", slot_id="s0") == "hit"
    assert 0 < adm.current_tokens <= meter.policy.input_limit_tokens


def test_unobservable_history_is_labelled_not_window_full():
    adm, _, _, pub = make(flags=("estimated", "history_unobservable"))
    assert start(adm) is False and adm.reason == "history_unobservable" and pub == []


def test_publisher_failure_keeps_receipt_outcome_and_active_view():
    rec, sent = FakeReceipts(), []
    def publish(p):
        if sent:
            raise RuntimeError("synthetic")
        sent.append(p)
    adm = ModelAdmission(FakeContext(10**6), receipts=rec, publish=publish, reader_registered=True)
    assert start(adm)
    active = adm.packet
    assert adm.admit("grep", "hit", slot_id="s0") == "[result receipt r-s0: use reader]"
    assert adm.reason == "publisher_failed" and adm.packet == active
    assert rec.records[0][2] == "returned"


def test_invalid_callbacks_and_unregistered_reader_rejected():
    for kw in ({"reader_registered": False}, {"reader_registered": True, "publish": None}):
        args = {"receipts": FakeReceipts(), "publish": [].append, **kw}
        try:
            ModelAdmission(FakeContext(10), **args)
            assert False
        except Exception as exc:
            assert exc.reason == "invalid_state"


def test_metadata_and_repr_carry_no_content_refs_or_call_ids():
    adm, *_ = make()
    assert start(adm)
    adm.admit("grep", "SECRET-SYNTHETIC", slot_id="s0")
    blob = json.dumps(adm.metadata()) + repr(adm)
    assert all(x not in blob for x in ("SECRET", "r-s0", "c0", "s0"))


def test_mismatched_envelope_rejected():
    adm, *_ = make()
    assert start(adm)
    try:
        adm.admit("grep", "x", slot_id="s0", envelope={"role": "tool", "tool_call_id": "OTHER"})
        assert False
    except Exception as exc:
        assert exc.reason == "projection_error"


def test_mandatory_additions_after_slots_and_never_published_over_limit():
    image_msg = {"role": "user", "content": [{"type": "text", "text": "IMG" * 50}]}
    adm, _, _, pub = make(limit=1000, count=lambda s: 5000 if "IMG" in s else 10)
    assert start(adm, 2)
    n = len(pub)
    adm.admit("view_image", "shown", slot_id="s0", additions=[image_msg])
    assert len(pub) == n and adm.reason == "model_window_full"   # nothing published over limit
    cp = adm.checkpoint_payload()
    assert cp["messages"][-1] == image_msg and cp["messages"][-2]["role"] == "tool"
    adm.skip("s1", "admission_closing")   # unfinished slots must be finalized before a new batch
    assert adm.start_batch(request(), 1, result_envelopes=envs(1), slot_ids=["z"]) is False


def real_make(limit=10**6, count=lambda s: len(s) // 4, **kw):
    from engine.context_receipts import ReceiptStore
    ctx, rec, pub = FakeContext(limit, count=count), ReceiptStore(**kw), []
    return ModelAdmission(ctx, receipts=rec, publish=pub.append, reader_registered=True), ctx, rec, pub


def reason_of(fn):
    try:
        fn()
    except Exception as exc:
        return exc.reason
    return None


def test_skip_uses_real_store_skipped_and_closed_reason_codes():
    adm, _, rec, pub = real_make()
    assert start(adm, 2)
    ran = []   # actual action callback: must never run once admission is closing
    adm.reason = "model_window_full"
    assert adm.closing()
    out = adm.skip("s0", "admission_closing")
    assert out == adm._slots["s0"]["fallback"] and ran == []
    adm.skip("s1", "SECRET https://x.example/?token=abc")   # arbitrary text never echoed
    for s, code in (("s0", "admission_closing"), ("s1", "not_run")):
        got = rec.read(adm._slots[s]["ref"])
        assert got["outcome"] == "skipped" and code in got["data"] and "SECRET" not in got["data"]
    meta = adm.metadata()
    assert adm.pending == 0 and meta["slots"]["skipped"] == 2 and meta["reason"] == "model_window_full"
    assert reason_of(lambda: adm.skip("s0", "not_run")) == "invalid_state"


def epoch_setup(limit=1000):
    adm, ctx, rec, pub = make(limit=limit, count=lambda s: len(s) // 4)
    ctx.epoch = 1
    assert start(adm, 2)
    adm.admit("grep", "h" * 400, slot_id="s0")
    return adm, ctx, rec, pub


def test_before_action_without_epoch_change_is_cheap_and_true():
    adm, ctx, _, pub = epoch_setup()
    n, calls = len(pub), ctx.calls
    assert adm.before_action("s1") is True and ctx.calls == calls and len(pub) == n


def test_epoch_change_P_still_fits_keeps_P_and_reports_its_count():
    adm, ctx, rec, pub = epoch_setup()
    ctx.epoch, n = 2, len(pub)
    ctx.count = lambda s: len(s) // 5
    assert adm.before_action("s1") is True and len(pub) == n
    assert adm.current_tokens == len(json.dumps(adm.packet)) // 5
    assert "h" * 400 in json.dumps(adm.packet)


def test_epoch_lower_limit_only_R_fits_publishes_whole_R():
    adm, ctx, rec, pub = epoch_setup()
    r_len = len(json.dumps(pub[0])) // 4
    ctx.policy.input_limit_tokens = r_len + 5   # P (with 400 h) no longer fits
    assert adm.before_action("s1") is True and not adm.closing()
    assert pub[-1] == pub[0] and adm.current_tokens == r_len
    assert adm.pending == 1 and [r[2] for r in rec.records] == ["returned"]   # no replay


def test_epoch_change_R_fails_stops_unpublished_with_checkpoint():
    adm, ctx, rec, pub = epoch_setup()
    ctx.epoch, n = 3, len(pub)
    ctx.count = lambda s: 10**6
    assert adm.before_action("s1") is False and adm.reason == "model_window_full"
    assert len(pub) == n and adm.checkpoint_payload() is not None
    assert adm.metadata()["slots"]["executed"] == 1 and adm.pending == 1
    assert adm.rebind(FakeContext(10**6)) is False and adm.reason == "model_window_full"


def test_epoch_change_counter_error_and_history_unobservable():
    for flags, code in ((("estimated", "history_unobservable"), "history_unobservable"), (None, "counter_error")):
        adm, ctx, _, pub = epoch_setup()
        ctx.epoch, n = 9, len(pub)
        ctx.flags = flags
        assert adm.before_action("s1") is False and adm.reason == code and len(pub) == n


def test_real_meter_metadata_epoch_ignores_estimates():
    from engine.context_policy import Policy
    from engine.context_tokens import ContextMeter
    meter = ContextMeter(Policy(1_000_000, 32_768, 50_000), route="synthetic", model="synthetic-model")
    adm = ModelAdmission(meter, receipts=FakeReceipts(), publish=[].append, reader_registered=True)
    assert start(adm, 2) and adm.admit("grep", "hit", slot_id="s0")
    assert adm.before_action("s1") is True   # counting itself bumps estimates: not an epoch change


def test_zero_call_batch_with_real_store_measures_packet_without_reserve():
    adm, ctx, rec, pub = real_make()
    req = {"model": "m", "messages": [{"role": "user", "content": "synthetic"}]}
    assert adm.start_batch(req, 0, result_envelopes=[], slot_ids=[]) is True
    assert pub[-1]["messages"] == req["messages"] and adm.pending == 0
    adm2, *_ = real_make(limit=1)
    assert adm2.start_batch(req, 0, result_envelopes=[], slot_ids=[]) is False
    assert adm2.reason == "model_window_full"


def test_new_batch_rejected_while_slots_pending_or_provisional_before_state_change():
    adm, _, rec, pub = make()
    assert start(adm, 2)
    adm.admit("finish", "ok", slot_id="s0", provisional=True)
    adm.skip("s1", "not_run")
    n, before = len(rec.reserved), adm.packet
    assert reason_of(lambda: start(adm, 1)) == "invalid_state"
    assert len(rec.reserved) == n and adm.packet == before and adm.reason is None


def test_invalid_or_unhashable_slot_and_envelope_inputs_fixed_code():
    adm, *_ = make()
    for ids, es in (([["x"]], envs(1)), ([None], envs(1)), (["s0"], ["bad"]), (["s0"], None), (5, envs(1))):
        assert reason_of(lambda: adm.start_batch(request(), 1, result_envelopes=es, slot_ids=ids)) \
            == "projection_error"
    assert reason_of(lambda: adm.start_batch(request(), 1, result_envelopes=[{"role": "tool",
                     "tool_call_id": "c0", "content": "x"}], slot_ids=["s0"])) == "projection_error"


def test_empty_preflight_content_normalized_and_changes_rejected():
    adm, _, _, pub = make()
    env = {"role": "tool", "tool_call_id": "c0", "content": ""}
    assert adm.start_batch(request(), 1, result_envelopes=[env], slot_ids=["s0"])
    assert adm.admit("grep", "hit", slot_id="s0", envelope=env) == "hit"
    assert sum(1 for m in pub[-1]["messages"] if m.get("role") == "tool") == 1
    assert start(adm)
    for bad in ({"role": "tool", "tool_call_id": "c0", "content": "x"},
                {"role": "user", "tool_call_id": "c0", "content": ""},
                {"role": "tool", "tool_call_id": "c9"}):
        assert reason_of(lambda: adm.admit("grep", "h", slot_id="s0", envelope=bad)) == "projection_error"


def test_large_request_above_256kib_with_small_result_fits_1m():
    adm, _, rec, pub = make()
    req = request()
    req["messages"][0]["content"] = "synthetic history\n" * 20_000   # ~360 KB request
    assert adm.start_batch(req, 1, result_envelopes=envs(1), slot_ids=["s0"])
    assert adm.admit("grep", "hit", slot_id="s0") == "hit" and not adm.closing()


def test_large_result_preserved_in_real_receipt_with_bounded_preview():
    from engine.context_receipts import ReceiptStore
    rec, pub = ReceiptStore(), []
    adm = ModelAdmission(FakeContext(10**6), receipts=rec, publish=pub.append,
                         reader_registered=True, preview_bytes=4096)
    assert start(adm)
    big = "synthetic line\n" * 20_000
    out = adm.admit("read_file", big, slot_id="s0")
    assert len(out.encode("utf-8")) <= 4096 and out.endswith("do not rerun to recover it.")
    got = rec.read(adm._slots["s0"]["ref"])
    assert got["outcome"] == "returned" and got["state"] == "recorded" and big.startswith(got["data"])


def test_tiny_preview_uses_whole_fallback_never_truncated():
    adm = ModelAdmission(FakeContext(10**6), receipts=FakeReceipts(), publish=[].append,
                         reader_registered=True, preview_bytes=8)
    assert start(adm)
    out = adm.admit("read_command_output", json.dumps({"data": "x" * 100}), slot_id="s0")
    assert out == "[result receipt r-s0: use reader]"


def test_read_bytes_counts_reader_tools_and_str_subclass_sent_as_str():
    class CompactText(str):
        pass
    from engine.context_receipts import ReceiptStore
    rec = ReceiptStore()
    adm = ModelAdmission(FakeContext(10**6), receipts=rec, publish=[].append, reader_registered=True)
    assert start(adm, 3)
    adm.admit("read_command_output", CompactText("abc"), slot_id="s0")
    adm.admit("run_command", "boom", slot_id="s1", outcome="returned", exit_code=2)
    adm.admit("finish", "pending", slot_id="s2", provisional=True)
    adm.replace_result("finish", "pending", "decided", slot_id="s2")
    assert adm.metadata()["observed"]["read_bytes"] == 3
    r1 = rec.read(adm._slots["s1"]["ref"])
    assert r1["exit_code"] == 2 and r1["outcome"] == "returned"
    assert rec.read(adm._slots["s2"]["ref"])["data"] == "decided"


def test_metadata_describes_published_view_after_failed_start():
    adm, _, _, pub = make(limit=200)
    assert start(adm)
    adm.admit("grep", "hit", slot_id="s0")
    meta = adm.metadata()
    assert start(adm, 8) is False
    after = adm.metadata()
    assert after["current_tokens"] == meta["current_tokens"]
    assert after["selected_active_bytes"] == meta["selected_active_bytes"]


def test_before_action_rejects_unknown_or_finished_slot():
    adm, *_ = epoch_setup()
    assert reason_of(lambda: adm.before_action("s0")) == "invalid_state"
    assert reason_of(lambda: adm.before_action(["x"])) == "invalid_state"


def test_lone_surrogate_result_after_action_seals_slot_and_keeps_original_recoverable():
    import base64
    adm, _, rec, pub = real_make()
    assert start(adm, 1) and adm.before_action("s0")
    adm.admit("read_file", "\udc80", slot_id="s0", exit_code=None)   # the action already ran
    assert adm.pending == 0 and adm.metadata()["slots"]["executed"] == 1
    assert reason_of(lambda: adm.before_action("s0")) == "invalid_state"   # never runnable again
    got = rec.read(adm._slots["s0"]["ref"])
    assert (got["state"], got["outcome"], got["exit_code"]) == ("recorded", "returned", None)
    assert got["content_complete"] and base64.b64decode(got["data"]) == "\udc80".encode("utf-8", "surrogateescape")
    assert adm.metadata()["observed"]["produced_bytes"] == got["total_bytes"]
    assert pub[-1]["messages"][-1]["tool_call_id"] == "c0"   # pairing stays complete


def test_lone_surrogate_replace_result_on_provisional_slot_finalizes_once():
    adm, _, rec, pub = real_make()
    assert start(adm, 1)
    adm.admit("read_file", "partial", slot_id="s0", provisional=True)
    adm.replace_result("read_file", "partial", "\ud800x", slot_id="s0", exit_code=None)
    got = rec.read(adm._slots["s0"]["ref"])
    assert adm.metadata()["slots"]["executed"] == 1 and got["state"] == "recorded"
    assert got["exit_code"] is None and got["content_complete"]
    assert reason_of(lambda: adm.replace_result("read_file", "x", "y", slot_id="s0")) == "invalid_state"


def test_capture_encoding_failure_seals_slot_and_closes_explicitly():
    class Hostile(str):   # synthetic: any post-action encoding fault must not leave the slot pending
        def encode(self, *a, **k):
            raise RuntimeError("synthetic")
    adm, _, rec, pub = make()
    assert start(adm, 1)
    adm.admit("read_file", Hostile("ok"), slot_id="s0")
    assert adm.pending == 0 and reason_of(lambda: adm.before_action("s0")) == "invalid_state"
    assert rec.records == [("r-s0", 2, "returned", None, True)] and adm.reason is None
    adm2, _, rec2, _ = make()
    assert start(adm2, 1)
    adm2.observed = {}   # synthetic unavoidable post-action capture fault
    adm2.admit("read_file", "ok", slot_id="s0", exit_code=3)
    assert adm2.pending == 0 and adm2.reason == "resource_error"   # sealed and closed explicitly
    assert rec2.records == [("r-s0", 2, "returned", 3, True)]   # outcome still recorded once
    assert reason_of(lambda: adm2.before_action("s0")) == "invalid_state"


def test_clip_notice_never_promises_uncaptured_bytes_complete_or_not():
    from engine.context_receipts import ReceiptStore
    def failing(key, data):
        raise OSError("synthetic publisher fault")
    blobs = {}
    def working(key, data):
        blobs[key] = data   # synthetic in-memory publisher; None ack = published
    big = "synthetic row\n" * 1500   # 21000 bytes > 16384 inline and > 4096 preview
    for publisher, complete in ((failing, False), (working, True)):
        rec = ReceiptStore(publisher=publisher, reader=lambda k, s, n: blobs[k][s:s + n])
        adm = ModelAdmission(FakeContext(10**6), receipts=rec, publish=[].append,
                             reader_registered=True, preview_bytes=4096)
        assert start(adm)
        out = adm.admit("read_file", big, slot_id="s0", exit_code=7)
        assert len(out.encode("utf-8")) <= 4096 and big.startswith(out[:100])   # clipped candidate
        assert "full result" not in out and "content_complete" in out
        got = rec.read(adm._slots["s0"]["ref"])
        assert got["content_complete"] is complete and got["exit_code"] == 7
        assert got["state"] == "recorded" and big.startswith(got["data"])
        if not complete:
            assert got["stored_bytes"] == 16384 < got["total_bytes"] == len(big)
        assert reason_of(lambda: adm.before_action("s0")) == "invalid_state"
        assert reason_of(lambda: adm.replace_result("read_file", out, "x", slot_id="s0")) == "invalid_state"


class Counting:
    """Endless synthetic iterable; raises once a preflight pulls past `cap` items."""

    def __init__(self, make_item, cap=1000):
        self.make_item, self.cap, self.pulled = make_item, cap, 0

    def __iter__(self):
        while True:
            self.pulled += 1
            assert self.pulled <= self.cap, "unbounded consumption"
            yield self.make_item(self.pulled - 1)


def test_invalid_n_calls_consumes_no_iterable_and_has_no_effect():
    for n in (-1, 129, True, 2.0, None):
        adm, ctx, rec, pub = make()
        ids, es = Counting(lambda i: f"s{i}"), Counting(lambda i: {"role": "tool"})
        assert reason_of(lambda: adm.start_batch(request(), n, result_envelopes=es, slot_ids=ids)) \
            == "projection_error"
        assert (ids.pulled, es.pulled, rec.reserved, pub, ctx.calls) == (0, 0, [], [], 0)


def test_oversized_iterables_terminate_boundedly_without_effects():
    adm, ctx, rec, pub = make()
    ids, es = Counting(lambda i: f"s{i}"), Counting(lambda i: {"role": "tool", "tool_call_id": f"c{i}"})
    assert reason_of(lambda: adm.start_batch(request(2), 2, result_envelopes=es, slot_ids=ids)) \
        == "projection_error"
    assert ids.pulled <= 3 and es.pulled <= 3 and (rec.reserved, pub, ctx.calls) == ([], [], 0)


def test_valid_generator_batch_preserves_exact_ids_and_envelopes():
    adm, _, rec, pub = make()
    ids = (f"id-{i}" for i in range(3))
    es = ({"role": "tool", "tool_call_id": f"c{i}"} for i in range(3))
    assert adm.start_batch(request(3), 3, result_envelopes=es, slot_ids=ids)
    assert rec.reserved == [("id-0", "id-1", "id-2")] and adm._order == ["id-0", "id-1", "id-2"]
    assert [m.get("tool_call_id") for m in pub[-1]["messages"][-3:]] == ["c0", "c1", "c2"]

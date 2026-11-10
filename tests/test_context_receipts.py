import json

import pytest

from engine.context_receipts import ReceiptError, ReceiptStore, Reservation


def test_reserve_batch_assigns_unique_refs_and_stable_fallback():
    store = ReceiptStore()
    got = store.reserve_batch(["s1", "s2"])
    assert [r.slot_id for r in got] == ["s1", "s2"]
    assert all(isinstance(r, Reservation) for r in got)
    assert got[0].ref != got[1].ref
    for r in got:
        assert r.ref.startswith("result://")
        assert len(r.fallback_text.encode("utf-8")) <= 512
        body = json.loads(r.fallback_text)
        assert r.ref in r.fallback_text
        assert "read_context_result" in r.fallback_text
        assert isinstance(body, dict)
    with pytest.raises(ReceiptError):
        store.reserve_batch(["s3", "s1"])  # reuse rejects whole batch
    assert store.metadata()["receipts"] == 2


def test_pending_provisional_final_transitions_and_zero_exit():
    store = ReceiptStore()
    a, b, c = store.reserve_batch(["a", "b", "c"])
    page = store.read(a.ref)
    assert page["state"] == "pending" and page["eof"] is False
    assert page["total_bytes"] is None and page["stored_bytes"] is None
    store.record(a.ref, "partial", outcome=None, final=False)
    assert store.read(a.ref)["state"] == "provisional"
    store.record(a.ref, "done", outcome="returned", exit_code=0)
    page = store.read(a.ref)
    assert (page["state"], page["outcome"], page["exit_code"]) == ("recorded", "returned", 0)
    assert page["data"] == "done" and page["eof"] and page["content_complete"]
    with pytest.raises(ReceiptError):
        store.record(a.ref, "again", outcome="returned")
    with pytest.raises(ReceiptError):
        store.record(b.ref, "x", outcome="skipped", exit_code=1)
    with pytest.raises(ReceiptError):
        store.record(b.ref, "x", outcome="success")
    assert store.read(b.ref)["state"] == "pending"
    store.record(c.ref, "", outcome="unknown")
    page = store.read(c.ref)
    assert page["total_bytes"] == 0 and page["eof"] is True and page["exit_code"] is None
    assert "verified" not in page and "approved" not in page


def _file_backend(root):
    def publish(key, data):
        (root / key).write_bytes(data)

    def read(key, offset, length):
        with open(root / key, "rb") as fh:
            fh.seek(offset)
            return fh.read(length)
    return publish, read


def _collect(store, ref):
    out, off = b"", 0
    while True:
        page = store.read(ref, off)
        raw = page["data"].encode() if page["encoding"] == "utf-8" else __import__(
            "base64").b64decode(page["data"])
        out, off = out + raw, page["next_offset"]
        if page["eof"]:
            return out, page


def test_file_backend_lossless_paging_and_tamper(tmp_path):
    publish, read = _file_backend(tmp_path)
    store = ReceiptStore(publisher=publish, reader=read, inline_bytes=1024)
    (r,) = store.reserve_batch(["x"])
    text = ("é€😀" * 3000) + "\udcff" + "z" * 5
    store.record(r.ref, text, outcome="returned", exit_code=0)
    expected = text.encode("utf-8", "surrogateescape")
    data, last = _collect(store, r.ref)
    assert data == expected and last["content_complete"] is True
    assert store.read(r.ref, 4095, 2)["next_offset"] == 4097  # spans two blocks
    with pytest.raises(ReceiptError):
        store.read(r.ref, len(expected) + 1)
    with pytest.raises(ReceiptError):
        store.read(r.ref, True)
    (blob,) = list(tmp_path.iterdir())
    raw = bytearray(blob.read_bytes())
    raw[5000] ^= 1
    blob.write_bytes(bytes(raw))
    with pytest.raises(ReceiptError) as err:
        store.read(r.ref, 4096)
    assert str(err.value) == "read_failed"
    assert store.read(r.ref, 0)["data"]  # untouched block still verifies
    blob.write_bytes(bytes(raw[:100]))
    with pytest.raises(ReceiptError):
        store.read(r.ref, 0)


@pytest.mark.parametrize("bad", [lambda *a: "str", lambda *a: 1 / 0])
def test_reader_wrong_type_or_exception_is_fixed_error(bad):
    store = ReceiptStore(publisher=lambda k, d: None, reader=bad, inline_bytes=16)
    (r,) = store.reserve_batch(["x"])
    store.record(r.ref, "SECRET-BODY" * 10, outcome="returned")
    with pytest.raises(ReceiptError) as err:
        store.read(r.ref)
    assert str(err.value) == "read_failed" and "SECRET" not in repr(err.value)


def test_publisher_failure_and_absence_keep_outcome_and_truncation_truth():
    def boom(key, data):
        raise OSError("disk /secret/path")
    for store in (ReceiptStore(publisher=boom, reader=lambda *a: b"", inline_bytes=8),
                  ReceiptStore(inline_bytes=8)):
        (r,) = store.reserve_batch(["x"])
        store.record(r.ref, "0123456789abc", outcome="unknown")
        page = store.read(r.ref)
        assert page["outcome"] == "unknown" and page["data"] == "01234567"
        assert page["eof"] is True and page["content_complete"] is False
        assert page["total_bytes"] == 13 and page["stored_bytes"] == 8
    capped = ReceiptStore(inline_bytes=8, max_body_bytes=8)
    (r,) = capped.reserve_batch(["x"])
    capped.record(r.ref, "y" * 100, outcome="returned", exit_code=2)
    page = capped.read(r.ref)
    assert page["total_bytes"] is None and page["content_complete"] is False
    assert page["exit_code"] == 2


def test_foreign_refs_rejected_before_callbacks():
    calls = []
    store = ReceiptStore(publisher=lambda *a: calls.append(a),
                         reader=lambda *a: calls.append(a), inline_bytes=4)
    other = ReceiptStore()
    (theirs,) = other.reserve_batch(["x"])
    (mine,) = store.reserve_batch(["x"])
    bad = [theirs.ref, mine.ref + "/../1", "file:///etc/passwd", "https://e.x/1",
           "C:/tmp/a", ["list"], None, mine.ref.upper()]
    for ref in bad:
        with pytest.raises(ReceiptError):
            store.read(ref)
        with pytest.raises(ReceiptError):
            store.record(ref, "t", outcome="returned")
    assert calls == []


def test_batch_validation_is_atomic_and_never_echoes_input():
    store = ReceiptStore(max_receipts=3)
    for batch in ([], ["ok", 7], ["ok", "a/b"], ["ok", "ok"], "ab",
                  ["s"] * 129, ["k1", "x" * 129], ["SECRET-SLOT", "SECRET-SLOT"]):
        with pytest.raises(ReceiptError) as err:
            store.reserve_batch(batch)
        assert "SECRET" not in str(err.value)
    assert store.metadata()["receipts"] == 0
    store.reserve_batch(["ok", "k1"])
    with pytest.raises(ReceiptError) as err:
        store.reserve_batch(["a", "b"])
    assert str(err.value) == "capacity" and store.metadata()["receipts"] == 2
    (last,) = store.reserve_batch(["a"])
    assert last.ref.endswith("/3")
    for cfg in ({"publisher": lambda *a: None}, {"max_receipts": 0},
                {"inline_bytes": True}, {"max_receipts": 65536, "inline_bytes": 65536}):
        with pytest.raises(ReceiptError):
            ReceiptStore(**cfg)


def test_fallback_stable_and_metadata_content_free(tmp_path):
    publish, read = _file_backend(tmp_path)
    store = ReceiptStore(publisher=publish, reader=read, inline_bytes=8)
    (r, s) = store.reserve_batch(["slot-SECRET", "s2"])
    before = r.fallback_text
    assert "pending" not in before and "success" not in before
    secret = "SECRET https://evil.example/x C:/Users/me/key " * 4
    store.record(r.ref, secret, outcome="returned", exit_code=0)
    store.record(s.ref, "short", outcome="skipped")
    assert r.fallback_text == before
    meta, text = store.metadata(), repr(store)
    blob = json.dumps(meta) + text
    for needle in ("SECRET", "https", "C:/", "result://", tmp_path.name, "slot"):
        assert needle not in blob
    assert meta["outcomes"]["returned"] == 1 and meta["states"]["recorded"] == 2


def _codes(fn):
    try:
        fn()
    except ReceiptError as exc:
        return exc.code
    return None


def test_publisher_reentry_same_ref_rejected_and_guard_cleared():
    seen = []

    def publish(key, data):
        seen.append(_codes(lambda: store.record(r.ref, "x", outcome="skipped")))
        seen.append(_codes(lambda: store.read(r.ref)))
        raise OSError("SECRET")
    store = ReceiptStore(publisher=publish, reader=lambda *a: b"", inline_bytes=4)
    (r,) = store.reserve_batch(["x"])
    store.record(r.ref, "0123456789", outcome="returned", exit_code=3)
    assert seen == ["invalid_transition", "read_failed"]
    page = store.read(r.ref)  # guard cleared after callback failure
    assert (page["outcome"], page["exit_code"], page["data"]) == ("returned", 3, "0123")
    assert page["content_complete"] is False


def test_reader_reentry_cannot_finalize_provisional_mid_read(tmp_path):
    publish, base_read = _file_backend(tmp_path)
    seen = []

    def read(key, offset, length):
        seen.append(_codes(lambda: store.record(r.ref, "y", outcome="returned")))
        return base_read(key, offset, length)
    store = ReceiptStore(publisher=publish, reader=read, inline_bytes=4)
    (r,) = store.reserve_batch(["x"])
    store.record(r.ref, "z" * 5000, outcome=None, final=False)
    page = store.read(r.ref)
    assert seen == ["invalid_transition"] and page["data"] == "z" * 4096
    store.record(r.ref, "y", outcome="returned")  # guard cleared after read
    assert store.read(r.ref)["data"] == "y"


class _HintBomb:
    def __init__(self):
        self._it = iter(["a", "b"])

    def __iter__(self):
        return self

    def __next__(self):
        return next(self._it)

    def __length_hint__(self):
        return 2**62


def test_reserve_batch_bounded_consumption_and_fixed_errors():
    store, pulled = ReceiptStore(), []

    def endless():  # stands in for an infinite iterator without risking a hang
        while True:
            pulled.append(1)
            assert len(pulled) < 200, "consumed past cap"
            yield f"s{len(pulled)}"

    def secret():
        yield "ok"
        raise RuntimeError("SECRET-ITER")
    assert _codes(lambda: store.reserve_batch(endless())) == "invalid_batch"
    assert len(pulled) <= 129
    with pytest.raises(ReceiptError) as err:
        store.reserve_batch(secret())
    assert err.value.code == "invalid_batch" and err.value.__cause__ is None
    assert "SECRET" not in repr(err.value) and err.value.__suppress_context__
    assert _codes(lambda: store.reserve_batch(5)) == "invalid_batch"
    assert store.metadata()["receipts"] == 0
    assert [r.slot_id for r in store.reserve_batch(_HintBomb())] == ["a", "b"]


def test_publisher_must_ack_none_synchronously():
    async def apub(key, data):
        return None
    with pytest.raises(ReceiptError):
        ReceiptStore(publisher=apub, reader=lambda *a: b"")
    coros, reads = [], []

    def coro_pub(key, data):
        coros.append(apub(key, data))
        return coros[-1]
    for ack in (lambda k, d: False, lambda k, d: True, coro_pub):
        store = ReceiptStore(publisher=ack, reader=lambda *a: reads.append(a),
                             inline_bytes=4)
        (r,) = store.reserve_batch(["x"])
        store.record(r.ref, "0123456789", outcome="returned", exit_code=0)
        page = store.read(r.ref)
        assert page["content_complete"] is False and page["data"] == "0123"
        assert (page["outcome"], page["exit_code"]) == ("returned", 0)
    assert reads == [] and coros[0].cr_frame is None  # coroutine closed


def test_reservation_repr_is_content_free():
    store = ReceiptStore()
    (r,) = store.reserve_batch(["slot-SECRET"])
    for needle in ("SECRET", "result://", "read_context_result"):
        assert needle not in repr(r)


def test_outcome_exact_type_checked_before_membership():
    calls = []

    class Evil(str):
        def __eq__(self, other):
            calls.append(other)
            raise RuntimeError("SECRET")
        __hash__ = str.__hash__
    store = ReceiptStore(publisher=lambda *a: calls.append(a),
                         reader=lambda *a: b"", inline_bytes=1)
    (r,) = store.reserve_batch(["x"])
    assert _codes(lambda: store.record(r.ref, "ab", outcome=Evil("returned"))) \
        == "invalid_argument"
    assert calls == [] and store.read(r.ref)["state"] == "pending"


def test_per_receipt_inline_cap_16384():
    with pytest.raises(ReceiptError):
        ReceiptStore(max_receipts=1, inline_bytes=16385)
    assert ReceiptStore(max_receipts=1, inline_bytes=16384).metadata()
    store = ReceiptStore(inline_bytes=100)
    assert store.metadata()["limits"]["inline_bytes"] == 100


def test_utf8_truncation_boundary_and_mixed_surrogates():
    store = ReceiptStore(inline_bytes=5, max_body_bytes=5)
    a, b = store.reserve_batch(["a", "b"])
    store.record(a.ref, "a\u00e9\u20ac", outcome="returned")  # 6 bytes, cut mid-char
    page = store.read(a.ref)
    assert page["encoding"] == "base64" and page["stored_bytes"] == 5
    assert page["total_bytes"] == 6 and page["content_complete"] is False
    # Unescapable high surrogate switches the whole text to surrogatepass.
    wide = ReceiptStore(inline_bytes=64, max_body_bytes=64)
    (m,) = wide.reserve_batch(["m"])
    text = "x\udcff\ud800y"
    wide.record(m.ref, text, outcome="returned")
    raw, page = _collect(wide, m.ref)
    assert raw == text.encode("utf-8", "surrogatepass") and page["content_complete"]
    assert raw.decode("utf-8", "surrogatepass") == text

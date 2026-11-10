import pytest

from engine.context_receipts import ReceiptError, ReceiptStore


def _slots(prefix, n):
    return [f"{prefix}{i}" for i in range(n)]


def _read_all(store, ref, limit):
    out, offset = [], 0
    while True:
        page = store.read(ref, offset, limit)
        out.append(page["data"])
        if page["eof"]:
            return "".join(out), page
        assert page["next_offset"] > offset
        offset = page["next_offset"]


def test_index_pages_cross_lines_and_nine_to_ten():
    store = ReceiptStore(max_receipts=20)
    refs = [r.ref for r in store.reserve_batch(_slots("s", 12))]
    handle = store.index_ref()
    scope = refs[0].split("/")[2]
    assert handle == f"result://{scope}/index/12"
    width = len(f"result://{scope}/20")
    expected = "".join(r.ljust(width) + "\n" for r in refs)
    data, last = _read_all(store, handle, 7)  # 7 never aligns to a line
    assert data == expected
    lines = data.split("\n")[:-1]
    assert len({len(x) for x in lines}) == 1
    assert lines[8].rstrip() == refs[8] and lines[9].rstrip() == refs[9]
    assert refs[8].endswith("/9") and refs[9].endswith("/10")
    assert last["kind"] == "reference_index" and last["high_water"] == 12
    assert last["outcome"] is None and last["exit_code"] is None
    assert last["content_complete"] is True and last["encoding"] == "utf-8"
    assert last["total_bytes"] == last["stored_bytes"] == len(expected)
    assert last["next_offset"] == len(expected)
    # A mid-line page computed directly from offsets.
    mid = store.read(handle, width - 3, 9)
    assert mid["data"] == expected[width - 3:width + 6]


def test_empty_index_is_readable():
    store = ReceiptStore(max_receipts=5)
    handle = store.index_ref()
    assert handle.endswith("/index/0")
    page = store.read(handle)
    assert page["data"] == "" and page["eof"] is True
    assert page["total_bytes"] == 0 and page["high_water"] == 0
    store.reserve_batch(["a"])
    assert store.read(handle) == page


def test_new_reservations_never_change_issued_handle():
    store = ReceiptStore(max_receipts=50)
    store.reserve_batch(_slots("a", 3))
    handle = store.index_ref()
    first = store.read(handle, 0, 10)
    full, end = _read_all(store, handle, 4096)
    store.reserve_batch(_slots("b", 9))
    reader = store.reserve_batch(["reader-call"])[0]  # simulated reader receipt
    store.record(reader.ref, "synthetic reader page", outcome="returned", exit_code=0)
    assert store.read(handle, 0, 10) == first
    again, end2 = _read_all(store, handle, 13)
    assert again == full and end2["total_bytes"] == end["total_bytes"]
    assert end2["next_offset"] == end["next_offset"] and end2["high_water"] == 3
    assert reader.ref not in again
    assert store.index_ref().endswith("/index/13")


def test_rejects_bad_handles_and_arguments_without_mutation():
    store = ReceiptStore(max_receipts=20)
    store.reserve_batch(_slots("s", 3))
    handle = store.index_ref()
    base = handle.rsplit("/", 1)[0]
    foreign = ReceiptStore(max_receipts=20)
    foreign.reserve_batch(_slots("s", 3))
    before = (store.metadata(), store.index_ref(), store.read(handle))
    bad_refs = [foreign.index_ref(), base + "/4", base + "/03", base + "/00",
                base + "/-1", base + "/+1", base + "/", base + "/1x", base + "/ 1",
                base + "/\u0661", base + "/1/", handle + "\n", handle.upper(),
                base, base + "/1" + "0" * 400, None, 3]
    for ref in bad_refs:
        with pytest.raises(ReceiptError) as err:
            store.read(ref)
        assert err.value.code == "invalid_ref"
    total = before[2]["total_bytes"]
    for offset, limit in [(-1, 10), (total + 1, 10), (True, 10), (0, True),
                          (0, 0), (0, 4097), (0.0, 10), (0, 10.0), (False, 1)]:
        with pytest.raises(ReceiptError) as err:
            store.read(handle, offset, limit)
        assert err.value.code == "invalid_argument"
    assert (store.metadata(), store.index_ref(), store.read(handle)) == before


def _fail_publisher(key, data):
    raise OSError("synthetic publisher fault")


def _never_read(key, start, size):
    raise AssertionError("backend read not expected")


def test_publisher_fault_keeps_outcome_prefix_and_immutability():
    store = ReceiptStore(publisher=_fail_publisher, reader=_never_read,
                         max_receipts=4, inline_bytes=16, max_body_bytes=4096)
    (res,) = store.reserve_batch(["p"])
    body = "synthetic-output-" * 10
    store.record(res.ref, body, outcome="returned", exit_code=7)
    page = store.read(res.ref)
    assert (page["state"], page["outcome"], page["exit_code"]) == ("recorded", "returned", 7)
    assert page["data"] == body[:16] and page["eof"] is True
    assert page["content_complete"] is False
    assert page["total_bytes"] == len(body) and page["stored_bytes"] == 16
    assert store.capture_complete(res.ref) is False
    with pytest.raises(ReceiptError):
        store.record(res.ref, body, outcome="returned", exit_code=0)
    assert store.read(res.ref) == page


def test_complete_bodies_expose_refs_transitively_incomplete_do_not():
    store = ReceiptStore(max_receipts=4, inline_bytes=64, max_body_bytes=4096)
    full, cut = store.reserve_batch(["full", "cut"])
    store.record(full.ref, "artifact://synthetic/a1 command://synthetic/c1",
                 outcome="returned", exit_code=0)
    store.record(cut.ref, "x" * 70 + " artifact://synthetic/missing",
                 outcome="returned", exit_code=0)
    listed, _ = _read_all(store, store.index_ref(), 4096)
    found = [line.rstrip() for line in listed.split("\n") if line]
    assert found == [full.ref, cut.ref]
    first = store.read(found[0])
    assert "artifact://synthetic/a1" in first["data"]
    assert "command://synthetic/c1" in first["data"] and first["content_complete"]
    second = store.read(found[1])
    assert "artifact://" not in second["data"] and second["content_complete"] is False
    assert store.capture_complete(found[0]) is True
    assert store.capture_complete(found[1]) is False


def test_capture_complete_only_for_complete_final_receipts():
    published = {}
    store = ReceiptStore(publisher=lambda k, d: published.__setitem__(k, d),
                         reader=_never_read, max_receipts=8, inline_bytes=8,
                         max_body_bytes=4096)
    pend, prov, final, blob = (r.ref for r in store.reserve_batch(_slots("c", 4)))
    store.record(prov, "ok", outcome=None, final=False)
    store.record(final, "ok", outcome="skipped")
    store.record(blob, "y" * 100, outcome="returned", exit_code=0)
    assert store.capture_complete(pend) is False
    assert store.capture_complete(prov) is False
    assert store.capture_complete(final) is True
    assert store.capture_complete(blob) is True and len(published) == 1
    for ref in (store.index_ref(), "result://nope/1", None, 1):
        assert store.capture_complete(ref) is False

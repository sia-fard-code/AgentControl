"""OwnedContextStorage over real Capabilities artifacts (tmp paths only)."""
from __future__ import annotations

import os

import pytest

from engine import command_output
from engine.context_storage import ContextStorageError, OwnedContextStorage
from tests.test_engine_reasoning import engine  # noqa: F401 - shared fixture


@pytest.fixture
def caps(engine, tmp_path, monkeypatch):  # noqa: F811
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "home"))
    root = tmp_path / "repo"
    root.mkdir()
    c = engine.Capabilities(str(root), artifact_dir=str(tmp_path / "run" / "art"))
    yield c
    c.close()


@pytest.fixture
def store(caps):
    return OwnedContextStorage(caps, command_output.read_page)


BODY = bytes(range(256)) * 300 + "é✓".encode() + b"\xff\xfe"  # >64KiB, invalid UTF-8


def test_publish_then_exact_range_reads(store):
    assert store.publish("k-1-r2", BODY) is None
    for off, n in ((0, 8192), (65530, 12), (len(BODY) - 5, 5), (76800, 3)):
        assert store.read("k-1-r2", off, n) == BODY[off:off + n]


def _receipts(store):
    from engine.context_receipts import ReceiptStore
    return ReceiptStore(publisher=store.publish, reader=store.read)


def _drain(rs, ref):
    import base64
    out, off = b"", 0
    while True:
        p = rs.read(ref, off, 4096)
        out += p["data"].encode() if p["encoding"] == "utf-8" else base64.b64decode(p["data"])
        if p["eof"]:
            return out, p
        off = p["next_offset"]


def test_receipt_body_over_16k_recoverable_losslessly(store):
    rs = _receipts(store)
    (res,) = rs.reserve_batch(["s1"])
    text = ("a✓é" * 3000) + "\udcff" + "z" * 9001  # multibyte splits + invalid byte
    rs.record(res.ref, text, outcome="returned", exit_code=3)
    body, last = _drain(rs, res.ref)
    assert body == text.encode("utf-8", "surrogateescape")  # ReceiptStore's encoding
    assert last["content_complete"] is True and last["exit_code"] == 3
    assert last["outcome"] == "returned"


def _codes(fn):
    with pytest.raises(ContextStorageError) as e:
        fn()
    return e.value.code


def test_duplicate_key_rejected_before_write_and_args_strict(store, caps):
    store.publish("k", b"x" * 100)
    seq = caps._seq
    assert _codes(lambda: store.publish("k", b"y")) == "duplicate_key"
    assert caps._seq == seq  # no artifact written for the duplicate
    for off, n in ((-1, 1), (True, 1), (0, 0), (0, 8193), (0, 101), (0.0, 1)):
        assert _codes(lambda: store.read("k", off, n)) == "invalid_argument"
    assert _codes(lambda: store.read("other", 0, 1)) == "read_failed"
    assert _codes(lambda: store.publish("big", b"x" * (1048577))) == "invalid_argument"


def _only_file(caps):
    d = caps._artifact_root()
    (name,) = os.listdir(d)
    return os.path.join(d, name)


def test_tamper_and_hardlink_detected(store, caps, tmp_path):
    store.publish("k", BODY)
    p = _only_file(caps)
    st = os.stat(p)
    with open(p, "r+b") as fh:
        fh.seek(70000)
        fh.write(b"\x00")
    os.utime(p, ns=(st.st_atime_ns, st.st_mtime_ns))  # restored mtime still caught
    assert _codes(lambda: store.read("k", 69990, 20)) == "read_failed"
    store.publish("h", b"q" * 50)
    hp = [os.path.join(os.path.dirname(p), n) for n in os.listdir(os.path.dirname(p))
          if os.path.join(os.path.dirname(p), n) != p][0]
    os.link(hp, str(tmp_path / "extra"))
    assert _codes(lambda: store.read("h", 0, 5)) == "read_failed"


def test_failed_publish_keeps_prefix_and_incomplete(store, caps, monkeypatch):
    monkeypatch.setattr(caps, "save_artifact",
                        lambda d, e: (_ for _ in ()).throw(OSError("disk /secret")))
    rs = _receipts(store)
    (res,) = rs.reserve_batch(["s1"])
    rs.record(res.ref, "b" * 20000, outcome="returned", exit_code=1)
    body, last = _drain(rs, res.ref)
    assert body == b"b" * 16384 and last["content_complete"] is False
    assert last["exit_code"] == 1
    meta = repr(store) + repr(store.metadata())
    assert "secret" not in meta and "art" not in meta and "result://" not in meta
    assert store.metadata()["failed_publishes"] == 1

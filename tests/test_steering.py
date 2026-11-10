from __future__ import annotations

import threading
from pathlib import Path

import pytest

from agent_control.steering import (
    MAX_STEER_CHARS,
    WRAP_UP_TEXT,
    PendingSteer,
    SteerBook,
    SteerError,
    append_steer,
    cancel_steer,
    delivery_state,
    normalize,
    read_steer_file,
    status_dict,
    steer_path_for,
    validate_text,
)


def test_validate_trims_and_accepts_newlines_tabs() -> None:
    assert validate_text("  a\n\tb  ") == "a\n\tb"


@pytest.mark.parametrize("bad", ["", "   ", "x" * 4001, "a\x00b", "a\x07b", "a\x7fb", 5])
def test_validate_rejects(bad: object) -> None:
    with pytest.raises(SteerError) as exc:
        validate_text(bad)
    assert exc.value.code == "invalid_text"


def test_validate_accepts_4000() -> None:
    assert validate_text("x" * 4000) == "x" * 4000


def test_steer_path_requires_md_file(tmp_path: Path) -> None:
    missing_md = tmp_path / "a.md"
    with pytest.raises(SteerError) as exc:
        steer_path_for(str(missing_md))
    assert exc.value.code == "no_transcript"

    txt = tmp_path / "a.txt"
    txt.write_text("x", encoding="utf-8")
    with pytest.raises(SteerError) as exc2:
        steer_path_for(str(txt))
    assert exc2.value.code == "no_transcript"


def test_steer_path_ok(tmp_path: Path) -> None:
    md = tmp_path / "a.md"
    md.write_text("x", encoding="utf-8")
    assert steer_path_for(str(md)) == Path(str(md) + ".steer")


def test_steer_path_rejects_directory(tmp_path: Path) -> None:
    md = tmp_path / "a.md"
    md.write_text("x", encoding="utf-8")
    (tmp_path / "a.md.steer").mkdir()
    with pytest.raises(SteerError) as exc:
        steer_path_for(str(md))
    assert exc.value.code == "bad_steer_path"


def test_steer_path_rejects_symlink(tmp_path: Path) -> None:
    md = tmp_path / "a.md"
    md.write_text("x", encoding="utf-8")
    target = tmp_path / "target"
    target.write_text("y", encoding="utf-8")
    try:
        (tmp_path / "a.md.steer").symlink_to(target)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks not supported")
    with pytest.raises(SteerError) as exc:
        steer_path_for(str(md))
    assert exc.value.code == "bad_steer_path"


def test_append_to_absent_file(tmp_path: Path) -> None:
    p = tmp_path / "a.md.steer"
    append_steer(p, "hello")
    assert p.read_bytes() == b"hello\n"


def test_append_after_existing_guidance(tmp_path: Path) -> None:
    p = tmp_path / "a.md.steer"
    p.write_bytes(b"orchestrator says\n")
    append_steer(p, "mine")
    data = p.read_bytes()
    assert data.startswith(b"orchestrator says")
    assert data.endswith(b"\n\nmine\n")
    assert b"\r" not in data


def test_cancel_removes_only_our_segment(tmp_path: Path) -> None:
    p = tmp_path / "a.md.steer"
    p.write_bytes(b"orchestrator says\n")
    append_steer(p, "mine")
    assert cancel_steer(p, "mine") is True
    assert read_steer_file(p).strip() == "orchestrator says"


def test_cancel_absent_returns_false(tmp_path: Path) -> None:
    p = tmp_path / "a.md.steer"
    p.write_bytes(b"orchestrator says\n")
    before = p.read_bytes()
    assert cancel_steer(p, "mine") is False
    assert p.read_bytes() == before


def test_cancel_last_segment_empties_file(tmp_path: Path) -> None:
    p = tmp_path / "a.md.steer"
    p.write_text("mine\n", encoding="utf-8")
    assert cancel_steer(p, "mine") is True
    assert read_steer_file(p) == ""


def _pending(text: str, events_at_send: int) -> PendingSteer:
    return PendingSteer(
        id="s1",
        run_id="r1",
        text=text,
        kind="steer",
        chars=len(text),
        sent_at="2026-09-24T00:00:00+00:00",
        events_at_send=events_at_send,
    )


def test_delivered() -> None:
    p = _pending("please focus on tests", 8)
    guidance = [(10, 4, "please  focus\non tests")]
    assert delivery_state(p, guidance=guidance, steer_file_text="", run_open=True) == (
        "delivered",
        4,
    )


def test_old_guidance_does_not_count() -> None:
    p = _pending("please focus on tests", 11)
    guidance = [(10, 4, "please  focus\non tests")]
    assert delivery_state(
        p, guidance=guidance, steer_file_text="please focus on tests", run_open=True
    ) == ("queued", None)


def test_queued_survives_new_events() -> None:
    p = _pending("please focus on tests", 3)
    assert delivery_state(
        p, guidance=[], steer_file_text="please focus on tests", run_open=True
    ) == ("queued", None)


def test_lost() -> None:
    p = _pending("please focus on tests", 3)
    assert delivery_state(
        p, guidance=[], steer_file_text="something else", run_open=True
    ) == ("lost", None)


def test_undeliverable() -> None:
    p = _pending("please focus on tests", 3)
    assert delivery_state(
        p, guidance=[], steer_file_text="please focus on tests", run_open=False
    ) == ("undeliverable", None)
    assert delivery_state(
        p, guidance=[], steer_file_text="", run_open=False
    ) == ("undeliverable", None)


def test_delivered_wins_after_run_end() -> None:
    p = _pending("please focus on tests", 8)
    guidance = [(10, 4, "please  focus\non tests")]
    assert delivery_state(
        p, guidance=guidance, steer_file_text="", run_open=False
    ) == ("delivered", 4)


def test_status_dict_has_no_text() -> None:
    p = _pending("secret text", 3)
    d = status_dict(p, "queued", 4)
    assert set(d.keys()) == {"id", "state", "kind", "chars", "sent_at", "turn"}


def test_book_keeps_five_newest() -> None:
    book = SteerBook()
    for i in range(7):
        book.add(
            PendingSteer(
                id=f"s{i}",
                run_id="r1",
                text="t",
                kind="steer",
                chars=1,
                sent_at="2026-09-24T00:00:00+00:00",
                events_at_send=0,
            )
        )
    got = book.for_run("r1")
    assert [p.id for p in got] == ["s2", "s3", "s4", "s5", "s6"]
    assert book.get("r1", "s4") is not None
    book.remove("r1", "s4")
    assert book.get("r1", "s4") is None


def test_book_lock_is_per_run() -> None:
    book = SteerBook()
    assert book.lock_for("a") is book.lock_for("a")
    assert book.lock_for("a") is not book.lock_for("b")


def test_wrap_up_and_max_constants() -> None:
    assert MAX_STEER_CHARS == 4000
    assert isinstance(WRAP_UP_TEXT, str)
    assert isinstance(threading.Lock(), type(threading.Lock()))

"""Turn budget: an image-bearing forced report (synthetic PNG, temp paths)."""
import json

import pytest

from test_engine_images import (  # noqa: F401
    _answer, _calls, _tmp_index, engine, harness, make_png, no_pillow, write)


def _end(tmp_path):
    rows = [json.loads(x) for x in (tmp_path / "index.jsonl").read_text().splitlines()]
    return [r for r in rows if r["event"] == "end"][-1]


def test_image_bearing_report_keeps_pixels_inside_cap(harness, tmp_path, no_pillow):
    write(tmp_path, "a.png", make_png(2, 2))
    with pytest.raises(SystemExit) as caught:
        harness.run("-t", "x", "-m", "gpt-6-sol", "--max-iters", "2", replies=[
            _calls(("c1", "view_image", {"path": "a.png"})), _answer("synthetic report")])
    assert caught.value.code == 3
    report = harness.payloads[-1]
    assert len(harness.payloads) == 2 and "tools" not in report
    assert any(isinstance(m["content"], list) and any(
        p.get("type") == "image_url" for p in m["content"]) for m in report["messages"])
    assert _end(tmp_path)["stop_reason"] == "max_iters"
    assert "base64," not in harness.artifacts()


def test_image_report_rejection_still_records_reason(harness, tmp_path, no_pillow, capsys):
    write(tmp_path, "a.png", make_png(2, 2))
    fatal = {"_fatal": True, "_status": 400, "_error_detail": "synthetic image rejection"}
    with pytest.raises(SystemExit) as caught:
        harness.run("-t", "x", "-m", "gpt-6-sol", "--max-iters", "2", replies=[
            _calls(("c1", "view_image", {"path": "a.png"})), fatal])
    assert caught.value.code == 3
    end = _end(tmp_path)
    assert end["status"] == "failed" and end["stop_reason"] == "max_iters"
    assert "images were not dropped" in capsys.readouterr().err
    assert "base64," not in harness.artifacts()

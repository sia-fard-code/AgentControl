"""Image input for delegate one-shots and the view_image agent tool.

Synthetic fixtures only (generated PNG/JPEG/WebP headers), tmp_path and a
temporary run index in every test; nothing reaches a real gateway.
"""
from __future__ import annotations

import base64
import hashlib
import importlib.util
import json
import struct
import sys
import zlib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
ENGINE_PATH = ROOT / "engine" / "delegate.py"
WRAPPER_PATH = ROOT / "engine" / "ask-model.py"


def _load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def engine():
    return _load(ENGINE_PATH, "agentcontrol_engine_images")


@pytest.fixture(scope="module")
def wrapper():
    return _load(WRAPPER_PATH, "agentcontrol_ask_model_images")


@pytest.fixture(autouse=True)
def _tmp_index(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_AGENT_RUN_INDEX", str(tmp_path / "index.jsonl"))


@pytest.fixture()
def no_pillow(engine, monkeypatch):
    monkeypatch.setattr(engine, "_load_pillow", lambda: None)


# ---- synthetic image bytes ---------------------------------------------------

def _chunk(kind, data):
    body = kind + data
    return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body))


def make_png(w=4, h=3, alpha=False, extra_chunks=()):
    color = 6 if alpha else 2
    bpp = 4 if alpha else 3
    raw = b"".join(b"\x00" + bytes([(x * 40 + y) % 256 for x in range(w * bpp)])
                   for y in range(h))
    out = b"\x89PNG\r\n\x1a\n" + _chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, color, 0, 0, 0))
    for kind, data in extra_chunks:
        out += _chunk(kind, data)
    return out + _chunk(b"IDAT", zlib.compress(raw)) + _chunk(b"IEND", b"")


def make_jpeg_header(w=5, h=7):
    """SOI + APP0 + SOF0 header only: enough for bounded dimension parsing."""
    app0 = b"\xff\xe0" + struct.pack(">H", 16) + b"JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"
    sof = b"\xff\xc0" + struct.pack(">HBHHB", 17, 8, h, w, 3) + b"\x01\x11\x00\x02\x11\x01\x03\x11\x01"
    return b"\xff\xd8" + app0 + sof + b"\xff\xd9"


def make_webp_vp8x(w=6, h=9, animated=False):
    flags = 0x02 if animated else 0
    payload = bytes([flags, 0, 0, 0]) + (w - 1).to_bytes(3, "little") + (h - 1).to_bytes(3, "little")
    body = b"WEBP" + b"VP8X" + struct.pack("<I", len(payload)) + payload
    return b"RIFF" + struct.pack("<I", len(body)) + body


def write(tmp_path, name, data):
    p = tmp_path / name
    p.write_bytes(data)
    return p


# ---- wrapper -------------------------------------------------------------------

def test_wrapper_image_option_repeats_and_forwards(wrapper, tmp_path) -> None:
    args = wrapper.parse_args(["-p", "q", "-m", "gpt-6-sol", "--image", "a.png",
                               "--image", "b.jpg", "ctx.txt"])
    argv = wrapper.build_argv(args, "prompt.txt")
    assert argv[argv.index("--attach") + 1] == "ctx.txt"
    images = [argv[i + 1] for i, a in enumerate(argv) if a == "--attach-image"]
    assert images == ["a.png", "b.jpg"]
    assert argv[-2:] == ["-f", "prompt.txt"]


def test_wrapper_text_only_argv_unchanged(wrapper) -> None:
    args = wrapper.parse_args(["-p", "q", "-m", "m1", "ctx.txt", "-e", "high"])
    argv = wrapper.build_argv(args, "p.txt")
    assert argv[3:] == ["--oneshot", "-C", args.root, "-m", "m1", "--label", "ask-m1",
                        "-e", "high", "--attach", "ctx.txt", "-f", "p.txt"]


# ---- validation without Pillow -------------------------------------------------

def test_png_without_pillow_keeps_bytes(engine, tmp_path, no_pillow) -> None:
    data = make_png(4, 3)
    asset = engine.prepare_image(str(write(tmp_path, "a.png", data)), "a.png")
    assert (asset.mime, asset.width, asset.height) == ("image/png", 4, 3)
    assert asset.data == data and asset.size == len(data)
    assert asset.sha256 == hashlib.sha256(data).hexdigest()
    assert asset.source is None
    assert base64.b64encode(data).decode() not in repr(asset)


def test_jpeg_and_webp_dimensions_without_pillow(engine, tmp_path, no_pillow) -> None:
    j = engine.prepare_image(str(write(tmp_path, "a.jpg", make_jpeg_header(5, 7))), "a.jpg")
    assert (j.mime, j.width, j.height) == ("image/jpeg", 5, 7)
    w = engine.prepare_image(str(write(tmp_path, "a.webp", make_webp_vp8x(6, 9))), "a.webp")
    assert (w.mime, w.width, w.height) == ("image/webp", 6, 9)


@pytest.mark.parametrize("data", [b"GIF89a" + b"\x00" * 20, b"not an image", b"",
                                  b"\x89PNG\r\n\x1a\n\x00\x00"])
def test_unknown_or_truncated_refused(engine, tmp_path, no_pillow, data) -> None:
    with pytest.raises(engine.ImageError):
        engine.prepare_image(str(write(tmp_path, "x.bin", data)), "x.bin")


def test_animated_refused_without_pillow(engine, tmp_path, no_pillow) -> None:
    apng = make_png(2, 2, extra_chunks=[(b"acTL", struct.pack(">II", 2, 0))])
    with pytest.raises(engine.ImageError, match="animated"):
        engine.prepare_image(str(write(tmp_path, "a.png", apng)), "a.png")
    with pytest.raises(engine.ImageError, match="animated"):
        engine.prepare_image(str(write(tmp_path, "a.webp", make_webp_vp8x(animated=True))), "a.webp")


def test_resize_needed_without_pillow_names_pillow(engine, tmp_path, no_pillow) -> None:
    big = make_png(engine.IMAGE_MAX_SIDE + 1, 1)
    with pytest.raises(engine.ImageError, match="Pillow"):
        engine.prepare_image(str(write(tmp_path, "big.png", big)), "big.png")
    edge = make_png(engine.IMAGE_MAX_SIDE, 1)
    assert engine.prepare_image(str(write(tmp_path, "e.png", edge)), "e.png").width == 2048


# ---- offline run harness -------------------------------------------------------

@pytest.fixture()
def harness(engine, tmp_path, monkeypatch):
    monkeypatch.setattr(engine, "load_cfg", lambda: ("http://gw", "sekret-key"))
    monkeypatch.setattr(engine, "refresh_models", lambda url, key: None)
    monkeypatch.setattr(engine.atexit, "register", lambda *a, **k: None)
    monkeypatch.setattr(engine.signal, "signal", lambda *a, **k: None)
    monkeypatch.setattr(engine.time, "sleep", lambda s: None)
    clock = {"t": 1000.0}
    monkeypatch.setattr(engine, "_now", lambda: clock["t"])
    monkeypatch.chdir(tmp_path)
    transcript = tmp_path / ".claude" / "agent-runs" / "run.md"

    class H:
        def __init__(self):
            self.payloads, self.stream_calls, self.wire = [], [], []
            self.transcript_path = transcript

        def wire_json(self, payload):
            text = json.dumps(payload, default=engine._wire_default)
            self.wire.append(text)
            return json.loads(text)

        def run(self, *argv, replies=(), stream_replies=None, durations=None):
            replies = list(replies)
            durations = list(durations or [])

            def fake_chat(url, key, payload):
                self.payloads.append(self.wire_json(payload))
                if durations:
                    clock["t"] += durations.pop(0)
                return replies.pop(0)

            def fake_stream(url, key, path, payload, idle_timeout=None):
                self.stream_calls.append((path, self.wire_json(payload)))
                script = scripts.pop(0)
                if isinstance(script, BaseException):
                    raise script
                for item in script:
                    if isinstance(item, (int, float)):
                        clock["t"] += item
                    else:
                        yield item

            scripts = list(stream_replies or [])
            monkeypatch.setattr(engine, "chat", fake_chat)
            monkeypatch.setattr(engine, "stream_events", fake_stream)
            if stream_replies is None:
                argv = ("--no-stream", *argv)
            monkeypatch.setattr(sys, "argv", ["delegate.py", "-C", str(tmp_path),
                                              "--transcript", str(transcript), *argv])
            engine.main()
            return transcript.read_text(encoding="utf-8")

        def artifacts(self):
            out = ""
            for p in transcript.parent.rglob("*"):
                if p.is_file():
                    out += p.read_text(encoding="utf-8")
            return out

    return H()


def _answer(text="the answer", usage=None):
    resp = {"choices": [{"message": {"content": text}, "finish_reason": "stop"}]}
    if usage is not None:
        resp["usage"] = usage
    return resp


def _b64(data):
    return base64.b64encode(data).decode()


def _chat_parts(payload):
    return payload["messages"][0]["content"]


CLAUDE = "anthropic_tool_call.claude-x"


def test_attach_image_requires_oneshot(harness, tmp_path, capsys) -> None:
    write(tmp_path, "a.png", make_png())
    with pytest.raises(SystemExit) as ei:
        harness.run("-t", "x", "--attach-image", "a.png")
    assert ei.value.code == 2
    assert "--attach-image needs --oneshot" in capsys.readouterr().err
    assert not (tmp_path / "index.jsonl").exists()


def test_bad_image_refused_before_network_or_index(harness, engine, tmp_path,
                                                   monkeypatch, capsys) -> None:
    write(tmp_path, "bad.png", b"\x89PNG\r\n\x1a\n" + b"\x00" * 10)
    monkeypatch.setattr(engine, "load_cfg", lambda: pytest.fail("config touched"))
    with pytest.raises(SystemExit) as ei:
        harness.run("--oneshot", "-t", "x", "--attach-image", "bad.png")
    assert ei.value.code == 2
    assert "bad.png" in capsys.readouterr().err
    assert not (tmp_path / "index.jsonl").exists()
    assert harness.payloads == [] and harness.stream_calls == []


def test_too_many_images_refused_before_network(harness, engine, tmp_path, capsys,
                                                 no_pillow) -> None:
    names = []
    for k in range(engine.IMAGE_MAX_PARTS + 1):
        write(tmp_path, f"i{k}.png", make_png(2, 2))
        names += ["--attach-image", f"i{k}.png"]
    with pytest.raises(SystemExit) as ei:
        harness.run("--oneshot", "-t", "x", *names)
    assert ei.value.code == 2
    assert "8-image limit" in capsys.readouterr().err
    assert harness.payloads == []


def test_nonstream_chat_multipart_and_metadata_only_artifacts(harness, tmp_path,
                                                              no_pillow, capsys) -> None:
    a, b = make_png(4, 3), make_jpeg_header(5, 7)
    write(tmp_path, "a.png", a)
    write(tmp_path, "b.jpg", b)
    harness.run("--oneshot", "-m", CLAUDE, "-t", "describe",
                "--attach-image", "a.png", "--attach-image", "b.jpg",
                replies=[_answer()])
    parts = _chat_parts(harness.payloads[0])
    assert parts[0] == {"type": "text", "text": "describe"}
    assert parts[1] == {"type": "image_url",
                        "image_url": {"url": "data:image/png;base64," + _b64(a)}}
    assert parts[2]["image_url"]["url"] == "data:image/jpeg;base64," + _b64(b)
    out = capsys.readouterr().out
    assert out == "the answer"
    art = harness.artifacts()
    assert _b64(a) not in art and _b64(b) not in art and "base64," not in art
    assert hashlib.sha256(a).hexdigest() in art
    msgs = [json.loads(l) for l in (tmp_path / ".claude" / "agent-runs" /
                                    "run.messages.jsonl").read_text().splitlines()]
    img = msgs[0]["content"][1]
    assert img["type"] == "image" and img["image"]["mime"] == "image/png"
    assert (img["image"]["width"], img["image"]["height"], img["image"]["bytes"]) == (4, 3, len(a))


def _gpt_events(text="seen", usage=None):
    return [{"type": "response.output_text.delta", "delta": text},
            {"type": "response.completed", "response": {"usage": usage}},
            {"_done": True}]


def _chat_events(text="seen", usage=None):
    evs = [{"choices": [{"delta": {"content": text}}]}]
    if usage is not None:
        evs.append({"choices": [], "usage": usage})
    return evs + [{"_done": True}]


def test_gpt_stream_uses_input_image_parts(harness, tmp_path, no_pillow, capsys) -> None:
    a = make_png(3, 3)
    write(tmp_path, "a.png", a)
    harness.run("--oneshot", "-m", "gpt-6-sol", "-t", "look", "--attach-image", "a.png",
                stream_replies=[_gpt_events()])
    path, payload = harness.stream_calls[0]
    assert path == "/openai/responses"
    assert payload["input"] == [{"role": "user", "content": [
        {"type": "input_text", "text": "look"},
        {"type": "input_image", "image_url": "data:image/png;base64," + _b64(a)}]}]
    assert capsys.readouterr().out == "seen"
    assert _b64(a) not in harness.artifacts()


def test_gpt_text_only_stream_keeps_string_input(harness) -> None:
    harness.run("--oneshot", "-m", "gpt-6-sol", "-t", "plain",
                stream_replies=[_gpt_events()])
    assert harness.stream_calls[0][1]["input"] == "plain"


def test_claude_stream_multipart_and_shorter_retry_keeps_pixels(harness, engine, tmp_path,
                                                                no_pillow) -> None:
    a = make_png(2, 5)
    write(tmp_path, "a.png", a)
    slow = [engine.REPLY_CUT_SECONDS + 1] + _chat_events("cut")
    harness.run("--oneshot", "-m", CLAUDE, "-t", "look", "--attach-image", "a.png",
                stream_replies=[slow, _chat_events("short")])
    assert [c[0] for c in harness.stream_calls] == ["/api/chat/completions"] * 2
    first, second = (_chat_parts(c[1]) for c in harness.stream_calls)
    url = "data:image/png;base64," + _b64(a)
    assert first[1]["image_url"]["url"] == url and second[1]["image_url"]["url"] == url
    assert first[0]["text"] == "look"
    assert second[0]["text"] == "look" + engine.ONESHOT_SHORTER
    assert _b64(a) not in harness.artifacts()


def test_stream_rejected_falls_back_with_images(harness, engine, tmp_path, no_pillow) -> None:
    a = make_png(2, 2)
    write(tmp_path, "a.png", a)
    harness.run("--oneshot", "-m", CLAUDE, "-t", "look", "--attach-image", "a.png",
                stream_replies=[engine.StreamError("http", "HTTP 404: no", 0)],
                replies=[_answer()])
    parts = _chat_parts(harness.payloads[0])
    assert parts[1]["image_url"]["url"] == "data:image/png;base64," + _b64(a)


def test_nonstream_cut_retry_keeps_pixels(harness, engine, tmp_path, no_pillow) -> None:
    a = make_png(2, 2)
    write(tmp_path, "a.png", a)
    harness.run("--oneshot", "-m", "qwen-x", "-t", "look", "--attach-image", "a.png",
                replies=[_answer("Error:"), _answer()],
                durations=[engine.REPLY_CUT_SECONDS + 5, 1])
    assert len(harness.payloads) == 2
    second = _chat_parts(harness.payloads[1])
    assert second[0]["text"] == "look" + engine.ONESHOT_SHORTER
    assert second[1]["image_url"]["url"].endswith(_b64(a))


def test_image_usage_subtotal_shown_not_added(harness, tmp_path, no_pillow, capsys) -> None:
    write(tmp_path, "a.png", make_png())
    usage = {"prompt_tokens": 900, "completion_tokens": 10,
             "prompt_tokens_details": {"image_tokens": 700, "junk": 1}}
    harness.run("--oneshot", "-m", CLAUDE, "-t", "x", "--attach-image", "a.png",
                replies=[_answer(usage=usage)])
    err = capsys.readouterr().err
    assert "[sent 900 tok to anthropic_tool_call.claude-x, incl. 700 image-input tok; " \
           "returned 10 tok]" in err
    assert "900 tok sent / 10 tok returned" in harness.transcript_path.read_text()
    rows = (tmp_path / ".claude" / "agent-runs" / "run.usage.jsonl").read_text()
    assert '"image_tokens":700' in rows and "junk" not in rows


@pytest.mark.parametrize("usage, shown", [
    ({"prompt_tokens": 5, "completion_tokens": 1}, "[sent 5 tok to anthropic_tool_call.claude-x; returned 1 tok]"),
    (None, "[sent ? tok to anthropic_tool_call.claude-x; returned ? tok]"),
    ({"prompt_tokens": 5, "completion_tokens": 1, "prompt_tokens_details": {"image_tokens": 0}},
     "[sent 5 tok to anthropic_tool_call.claude-x, incl. 0 image-input tok; returned 1 tok]"),
])
def test_image_usage_zero_and_unknown(harness, tmp_path, no_pillow, capsys, usage, shown) -> None:
    write(tmp_path, "a.png", make_png())
    harness.run("--oneshot", "-m", CLAUDE, "-t", "x", "--attach-image", "a.png",
                replies=[_answer(usage=usage)])
    assert shown in capsys.readouterr().err


def test_image_usage_partial(engine) -> None:
    usages = [{"input_tokens": 9, "input_tokens_details": {"image_tokens": 4}},
              {"input_tokens": 3}]
    assert engine.reported_image_tokens(usages) == (4, True)
    assert engine.reported_image_tokens([{"input_tokens": 3}, None]) is None
    clean = engine.sanitize_usage({"input_tokens_details": {"image_tokens": 4}})
    assert clean == {"input_tokens_details": {"image_tokens": 4}}
    assert engine.normalize_usage({"input_tokens_details": {"image_tokens": 4}})["sent"] is None


def test_fatal_rejection_names_model_and_does_not_drop_images(harness, engine, tmp_path,
                                                              no_pillow, capsys) -> None:
    a = make_png()
    write(tmp_path, "a.png", a)
    detail = "image input unsupported data:image/png;base64," + _b64(a)
    with pytest.raises(SystemExit) as ei:
        harness.run("--oneshot", "-m", CLAUDE, "-t", "x", "--attach-image", "a.png",
                    replies=[{"_transient": "HTTP 400", "_fatal": True, "_status": 400,
                              "_error_detail": engine.error_detail(
                                  json.dumps({"detail": detail}))}])
    assert ei.value.code == 1
    err = capsys.readouterr().err
    assert CLAUDE in err and "image(s)" in err and "Not retrying without them" in err
    assert _b64(a) not in err and "[image data omitted]" in err
    assert len(harness.payloads) == 1
    ends = [json.loads(l) for l in (tmp_path / "index.jsonl").read_text().splitlines()]
    assert ends[-1]["event"] == "end" and ends[-1]["status"] == "failed"
    assert "tools=oneshot: 0 tool calls]" in harness.transcript_path.read_text()


def test_error_detail_scrubs_before_truncation(engine) -> None:
    body = json.dumps({"detail": "bad data:image/png;base64," + "A" * 5000 + " tail"})
    out = engine.error_detail(body)
    assert "AAAA" not in out and out.endswith("tail")


# ---- view_image tool ---------------------------------------------------------

@pytest.mark.parametrize("bad", ["/etc/x.png", "\\x.png", "C:/x.png", "C:x.png",
                                 "\\\\server\\share\\x.png", "//server/share/x.png",
                                 "../x.png", "a/../a.png", "a\\..\\a.png"])
def test_view_image_refuses_unsafe_paths(engine, tmp_path, no_pillow, bad) -> None:
    (tmp_path / "a").mkdir()
    write(tmp_path / "a", "a.png", make_png())
    write(tmp_path, "a.png", make_png())
    out = engine.Runner(str(tmp_path)).dispatch("view_image", {"path": bad})
    assert isinstance(out, str) and out.startswith("error:")


def test_view_image_refuses_symlink_escape(engine, tmp_path, no_pillow) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    write(outside, "o.png", make_png())
    repo = tmp_path / "repo"
    repo.mkdir()
    try:
        (repo / "link").symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("symlinks unavailable on this machine")
    out = engine.Runner(str(repo)).dispatch("view_image", {"path": "link/o.png"})
    assert isinstance(out, str) and "escapes the repo root" in out


def test_view_image_reads_known_file_under_skip_dirs(engine, tmp_path, no_pillow) -> None:
    (tmp_path / "node_modules").mkdir()
    data = make_png()
    write(tmp_path / "node_modules", "x.png", data)
    assert "node_modules" in engine.SKIP_DIRS
    out = engine.Runner(str(tmp_path)).dispatch("view_image", {"path": "node_modules/x.png"})
    assert isinstance(out, engine.ImageAsset) and out.data == data
    assert out.path == "node_modules/x.png"


def test_read_file_semantics_unchanged(engine, tmp_path) -> None:
    (tmp_path / "d").mkdir()
    (tmp_path / "d" / "t.txt").write_text("hi", encoding="utf-8")
    r = engine.Runner(str(tmp_path))
    assert r.dispatch("read_file", {"path": "d/../d/t.txt"}) == "hi"


def test_view_image_tool_schema(engine) -> None:
    tool = [t for t in engine.native_tools() if t["function"]["name"] == "view_image"][0]
    params = tool["function"]["parameters"]
    assert params["properties"] == {"path": {"type": "string"}}
    assert params["required"] == ["path"]


def _calls(*calls):
    return {"choices": [{"message": {"content": "", "tool_calls": [
        {"id": cid, "type": "function",
         "function": {"name": name, "arguments": json.dumps(args)}}
        for cid, name, args in calls]}, "finish_reason": "stop"}]}


def _finish(cid="f2"):
    return _calls((cid, "finish", {"summary": "done"}))


def test_native_ordering_and_finish_deferral(harness, tmp_path, no_pillow, capsys) -> None:
    a = make_png(4, 4)
    write(tmp_path, "a.png", a)
    text = harness.run("-t", "inspect", "-m", "gpt-6-sol", replies=[
        _calls(("c1", "list_dir", {"path": "."}), ("c2", "view_image", {"path": "a.png"}),
               ("c3", "finish", {"summary": "too early"})),
        _finish()])
    msgs = harness.payloads[1]["messages"]
    tail = msgs[-5:]
    assert tail[0]["role"] == "assistant" and len(tail[0]["tool_calls"]) == 3
    assert [m.get("tool_call_id") for m in tail[1:4]] == ["c1", "c2", "c3"]
    assert "refused: finish" in tail[3]["content"]
    assert tail[4]["role"] == "user"
    assert tail[4]["content"][0] == {"type": "text",
                                     "text": "Image from view_image call c2: a.png"}
    assert tail[4]["content"][1]["image_url"]["url"].endswith(_b64(a))
    assert "## Final report\n\ndone" in text and "too early" not in capsys.readouterr().out
    # finish is never dispatched, so it does not count (unchanged footer grammar)
    assert text.rstrip().endswith("[gpt-6-sol e=high tools=native: 2 tool calls]")
    assert _b64(a) not in harness.artifacts()


def test_text_only_finish_unchanged(harness, tmp_path) -> None:
    text = harness.run("-t", "x", "-m", "gpt-6-sol", replies=[
        _calls(("c1", "list_dir", {"path": "."}), ("c2", "finish", {"summary": "ok!"}))])
    assert len(harness.payloads) == 1 and "## Final report\n\nok!" in text


def test_failed_view_image_does_not_defer_finish(harness, tmp_path) -> None:
    text = harness.run("-t", "x", "-m", "gpt-6-sol", replies=[
        _calls(("c1", "view_image", {"path": "missing.png"}),
               ("c2", "finish", {"summary": "ok!"}))])
    assert len(harness.payloads) == 1 and "## Final report\n\nok!" in text


@pytest.mark.parametrize("policy", ["legacy", "model-v1"])
def test_prompt_mode_extends_result_message(harness, tmp_path, no_pillow, policy) -> None:
    a = make_png(2, 2)
    write(tmp_path, "a.png", a)
    block = '```tool {"name": "view_image", "args": {"path": "a.png"}}```'
    fin = '```tool {"name": "finish", "args": {"summary": "seen"}}```'
    harness.run("--context-policy", policy, "-t", "x", "-m", "qwen3.6:latest",
                replies=[_answer(block), _answer(fin)])
    msgs = harness.payloads[1]["messages"]
    if policy == "legacy":   # legacy: text result and pixels share one user message
        last = msgs[-1]
        assert last["role"] == "user" and last["content"][0]["text"].startswith(
            "Result of view_image:")
        assert last["content"][1]["image_url"]["url"].endswith(_b64(a))
        return
    # model-v1: canonical result envelope, then the pixels in a following user message
    texts = [m["content"] if isinstance(m["content"], str) else m["content"][0].get("text", "")
             for m in msgs]
    result = max(i for i, t in enumerate(texts) if t.startswith("Result of view_image:"))
    last = msgs[-1]
    assert result < len(msgs) - 1 and last["role"] == "user"
    assert last["content"][-1]["image_url"]["url"].endswith(_b64(a))
    assert "None" not in json.dumps([p for p in last["content"] if p["type"] == "text"])


def test_view_image_cap_refused_visibly_without_eviction(harness, engine, tmp_path,
                                                         no_pillow, monkeypatch) -> None:
    monkeypatch.setattr(engine, "IMAGE_MAX_PARTS", 1)
    write(tmp_path, "a.png", make_png(2, 2))
    write(tmp_path, "b.png", make_png(3, 3))
    harness.run("-t", "x", "-m", "gpt-6-sol", replies=[
        _calls(("c1", "view_image", {"path": "a.png"})),
        _calls(("c2", "view_image", {"path": "b.png"})),
        _finish()])
    msgs = harness.payloads[2]["messages"]
    assert "refused: b.png was not attached" in [m for m in msgs
                                                 if m.get("tool_call_id") == "c2"][0]["content"]
    urls = [p["image_url"]["url"] for m in msgs if isinstance(m["content"], list)
            for p in m["content"] if p["type"] == "image_url"]
    assert len(urls) == 1 and urls[0].endswith(_b64(make_png(2, 2)))


def test_wrapup_keeps_images_and_anomaly_is_metadata_only(harness, tmp_path,
                                                          no_pillow) -> None:
    a = make_png(2, 2)
    write(tmp_path, "a.png", a)
    echo = {"choices": [{"message": {"content": "echo data:image/png;base64," + _b64(a)}}],
            "id": "r1", "echo": {"messages": "data:image/png;base64," + _b64(a)}}
    # Hard total of 3: view_image (work), a prose echo in the closeout turn
    # (repaired), then the reserved tool-less report turn.
    with pytest.raises(SystemExit) as caught:
        harness.run("-t", "x", "-m", "gpt-6-sol", "--max-iters", "3", replies=[
            _calls(("c1", "view_image", {"path": "a.png"})), echo,
            _answer("partial report")])
    assert caught.value.code == 3
    wrap = harness.payloads[-1]
    assert "tools" not in wrap
    urls = [p["image_url"]["url"] for m in wrap["messages"] if isinstance(m["content"], list)
            for p in m["content"] if p["type"] == "image_url"]
    assert urls == ["data:image/png;base64," + _b64(a)]
    anomalies = (tmp_path / ".claude" / "agent-runs" / "run.anomalies.jsonl").read_text()
    assert anomalies and _b64(a) not in anomalies and "echo" not in anomalies
    rec = json.loads(anomalies.splitlines()[0])
    assert rec["images"][0]["sha256"] == hashlib.sha256(a).hexdigest()
    assert _b64(a) not in harness.artifacts()


def test_native_agent_streams_carry_images(harness, tmp_path, no_pillow) -> None:
    a = make_png(2, 2)
    write(tmp_path, "a.png", a)
    call = {"type": "response.output_item.done", "item": {
        "type": "function_call", "call_id": "c1", "name": "view_image",
        "arguments": json.dumps({"path": "a.png"})}}
    fin = {"type": "response.output_item.done", "item": {
        "type": "function_call", "call_id": "c2", "name": "finish",
        "arguments": json.dumps({"summary": "done"})}}
    done = {"type": "response.completed", "response": {"usage": None}}
    text = harness.run("-t", "x", "-m", "gpt-6-sol",
                       stream_replies=[[call, done, {"_done": True}],
                                       [fin, done, {"_done": True}]])
    path, payload = harness.stream_calls[1]
    assert path == "/openai/responses"
    user = payload["input"][-1]
    assert user["role"] == "user"
    assert user["content"][0] == {"type": "input_text",
                                  "text": "Image from view_image call c1: a.png"}
    assert user["content"][1] == {"type": "input_image",
                                  "image_url": "data:image/png;base64," + _b64(a)}
    assert payload["input"][-2]["type"] == "function_call_output"
    assert "## Final report\n\ndone" in text
    assert _b64(a) not in harness.artifacts()


def test_text_only_attachment_refused_before_prepare(harness, engine, monkeypatch):
    monkeypatch.setattr(engine, "prepare_image", lambda *a: pytest.fail("loaded forbidden image"))
    with pytest.raises(SystemExit) as exc:
        harness.run("--oneshot", "-m", CLAUDE, "-t", "q", "--text-only",
                    "--attach-image", "missing.png")
    assert exc.value.code == 2 and not harness.payloads


def test_cumulative_image_admission_no_eviction_or_leak(harness, engine, tmp_path, monkeypatch, no_pillow):
    write(tmp_path, "a.png", make_png())
    write(tmp_path, "b.png", make_png(5, 4))
    cg = engine.capability_module("context_guard")
    monkeypatch.setattr(cg, "IMAGE_COST", 100000)
    with pytest.raises(SystemExit) as exc:
        # legacy: IMAGE_COST is the historical byte charge against the 256KiB request cap
        harness.run("--context-policy", "legacy", "-m", "gpt-6-sol", "-t", "visual evidence", replies=[
            _calls(("a", "view_image", {"path": "a.png"})),
            _calls(("b", "view_image", {"path": "b.png"})), _answer("partial")])
    assert exc.value.code == 3
    wrap = harness.payloads[-1]
    urls = [p["image_url"]["url"] for m in wrap["messages"] if isinstance(m["content"], list)
            for p in m["content"] if p["type"] == "image_url"]
    assert len(urls) == 1 and urls[0].endswith(_b64(make_png()))
    assert _b64(make_png(5, 4)) not in harness.artifacts()


def test_aggregate_byte_cap_checked_before_request(engine, monkeypatch, tmp_path,
                                                   no_pillow) -> None:
    asset = engine.prepare_image(str(write(tmp_path, "a.png", make_png())), "a.png")
    monkeypatch.setattr(engine, "IMAGE_MAX_TOTAL_BYTES", asset.size * 2 - 1)
    payload = {"model": "m-x", "messages": [
        {"role": "user", "content": engine.user_content("t", [asset, asset])}]}
    with pytest.raises(SystemExit) as ei:
        engine.check_payload_images(payload)
    assert "m-x" in str(ei.value.code) and "limit per request" in str(ei.value.code)


def test_content_helpers(engine, tmp_path, no_pillow) -> None:
    asset = engine.prepare_image(str(write(tmp_path, "a.png", make_png())), "a.png")
    assert engine.user_content("t", []) == "t"
    assert engine.append_content_text("t", "!") == "t!"
    multi = engine.user_content("t", [asset])
    longer = engine.append_content_text(multi, "!")
    assert multi[0]["text"] == "t" and longer[0]["text"] == "t!"
    assert longer[1]["image_url"]["url"] is asset


# ---- Pillow preprocessing ------------------------------------------------------

@pytest.fixture()
def PIL():
    return pytest.importorskip("PIL.Image")


def _pil_bytes(PIL, size, fmt, mode="RGB", **kw):
    import io
    import random
    rnd = random.Random(7)
    im = PIL.new(mode, size)
    im.putdata([tuple(rnd.randrange(256) for _ in mode) for _ in range(size[0] * size[1])])
    buf = io.BytesIO()
    im.save(buf, fmt, **kw)
    return buf.getvalue()


def test_pillow_in_limit_keeps_bytes(engine, tmp_path, PIL) -> None:
    for fmt, ext in (("PNG", "png"), ("JPEG", "jpg"), ("WEBP", "webp")):
        data = _pil_bytes(PIL, (20, 10), fmt)
        asset = engine.prepare_image(str(write(tmp_path, "a." + ext, data)), "a." + ext)
        assert asset.data == data and asset.source is None
        assert (asset.width, asset.height) == (20, 10)


def test_pillow_resizes_oversized_opaque_to_jpeg(engine, tmp_path, PIL) -> None:
    import io
    data = _pil_bytes(PIL, (3000, 40), "PNG")
    asset = engine.prepare_image(str(write(tmp_path, "w.png", data)), "w.png")
    assert asset.mime == "image/jpeg" and (asset.width, asset.height) == (2048, 27)
    assert asset.source["sha256"] == hashlib.sha256(data).hexdigest()
    assert asset.source["width"] == 3000 and "transformed" in asset.source["note"]
    assert asset.sha256 == hashlib.sha256(asset.data).hexdigest()
    assert PIL.open(io.BytesIO(asset.data)).size == (2048, 27)


def test_pillow_resizes_alpha_to_png(engine, tmp_path, PIL) -> None:
    data = _pil_bytes(PIL, (2100, 10), "PNG", mode="RGBA")
    asset = engine.prepare_image(str(write(tmp_path, "t.png", data)), "t.png")
    assert asset.mime == "image/png" and asset.width == 2048


def test_pillow_refuses_animated_and_truncated(engine, tmp_path, PIL) -> None:
    import io
    frames = [PIL.new("RGB", (4, 4), c) for c in ((255, 0, 0), (0, 255, 0))]
    buf = io.BytesIO()
    frames[0].save(buf, "WEBP", save_all=True, append_images=frames[1:])
    with pytest.raises(engine.ImageError, match="animated"):
        engine.prepare_image(str(write(tmp_path, "an.webp", buf.getvalue())), "an.webp")
    data = _pil_bytes(PIL, (64, 64), "JPEG")
    with pytest.raises(engine.ImageError):
        engine.prepare_image(str(write(tmp_path, "cut.jpg", data[: len(data) // 2])), "cut.jpg")
    png = make_png(16, 16)
    idat = png.index(b"IDAT")
    broken = png[:idat + 4] + bytes(b ^ 0x5A for b in png[idat + 4:idat + 20]) + png[idat + 20:]
    with pytest.raises(engine.ImageError):
        engine.prepare_image(str(write(tmp_path, "bad.png", broken)), "bad.png")


def test_pixel_bomb_refused_from_header(engine, tmp_path) -> None:
    head = make_png(1, 1)
    ihdr = _chunk(b"IHDR", struct.pack(">IIBBBBB", 8000, 8000, 8, 2, 0, 0, 0))
    bomb = head[:8] + ihdr + head[8 + len(ihdr):]   # 64M pixels declared, tiny file
    with pytest.raises(engine.ImageError, match="pixel"):
        engine.prepare_image(str(write(tmp_path, "bomb.png", bomb)), "bomb.png")


def test_source_size_limit(engine, tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(engine, "IMAGE_MAX_SOURCE_BYTES", 10)
    with pytest.raises(engine.ImageError, match="source limit"):
        engine.prepare_image(str(write(tmp_path, "a.png", make_png())), "a.png")


def test_text_only_oneshot_payload_unchanged(harness, tmp_path, capsys) -> None:
    harness.run("--oneshot", "-m", CLAUDE, "-t", "plain", replies=[_answer()])
    assert harness.payloads[0]["messages"] == [{"role": "user", "content": "plain"}]
    err = capsys.readouterr().err
    assert "image" not in err


# ---- streamed data-URL redaction (gap 1) -----------------------------------------

SYN_BODY = _b64(b"\x89PNG-synthetic-body-" * 4)
SYN = "data:image/png;base64," + SYN_BODY


def _tr(engine, tmp_path, scrub=True):
    path = tmp_path / "t.md"
    tr = engine.Transcript(str(path), "head")
    tr.scrub_images = scrub
    return tr, path


def _no_leak(text):
    assert SYN_BODY[:4] not in text and "base64," not in text and "base64" not in text


def test_append_redacts_data_url_at_every_split(engine, tmp_path) -> None:
    full = "before " + SYN + " after text"
    for i in range(1, len(full)):
        tr, path = _tr(engine, tmp_path)
        tr.append(full[:i])
        _no_leak(path.read_text(encoding="utf-8"))
        tr.append(full[i:])
        _no_leak(path.read_text(encoding="utf-8"))
        tr.write("")
        tr.close()
        out = path.read_text(encoding="utf-8")
        _no_leak(out)
        assert out == "head\nbefore [image data omitted] after text\n", i


def test_append_prefix_then_body_then_text_deltas(engine, tmp_path) -> None:
    tr, path = _tr(engine, tmp_path)
    for piece in ["x ", "data:image/png;base64,", SYN_BODY[:10], SYN_BODY[10:20],
                  SYN_BODY[20:], " readable", " more"]:
        tr.append(piece)
        _no_leak(path.read_text(encoding="utf-8"))
    tr.close()
    out = path.read_text(encoding="utf-8")
    assert out == "head\nx [image data omitted] readable more"


def test_append_carry_flushed_at_write_and_close(engine, tmp_path) -> None:
    tr, path = _tr(engine, tmp_path)
    tr.append("metadata and d")
    tr.write("\n## section")
    tr.append("ends with da")
    tr.close()
    assert path.read_text(encoding="utf-8") == \
        "head\nmetadata and d\n## section\nends with da"
    tr, path = _tr(engine, tmp_path)
    tr.append("cut data:image/pn")          # recognized partial header at close
    tr.flush()
    _no_leak(path.read_text(encoding="utf-8"))
    tr.close()
    assert path.read_text(encoding="utf-8") == "head\ncut [image data omitted]"
    tr, path = _tr(engine, tmp_path)
    tr.append("cut data:ima")               # header continues in a write
    tr.write("ge/png;base64," + SYN_BODY + " ok")
    tr.close()
    assert path.read_text(encoding="utf-8") == "head\ncut [image data omitted] ok\n"
    tr, path = _tr(engine, tmp_path)
    tr.append("a " + SYN[:30])              # body open when a section starts
    tr.write("ABC plain")                   # base64-alphabet text must survive
    tr.close()
    assert path.read_text(encoding="utf-8") == "head\na [image data omitted]ABC plain\n"


def test_append_text_only_unchanged(engine, tmp_path) -> None:
    tr, path = _tr(engine, tmp_path, scrub=False)
    for piece in ["plain d", "ata:image/png;base64,", "QUJD"]:
        tr.append(piece)
    tr.close()
    assert path.read_text(encoding="utf-8") == "head\nplain data:image/png;base64,QUJD"


def _syn_deltas(prefix="echo "):
    cuts = [5, 16, 24, 30, 40]
    pieces, last = [], 0
    for c in cuts:
        pieces.append(SYN[last:c])
        last = c
    pieces.append(SYN[last:] + " readable tail")
    pieces[0] = prefix + pieces[0]
    return pieces


def test_native_gpt_stream_echo_redacted(harness, tmp_path, no_pillow) -> None:
    write(tmp_path, "a.png", make_png(2, 2))
    call = {"type": "response.output_item.done", "item": {
        "type": "function_call", "call_id": "c1", "name": "view_image",
        "arguments": json.dumps({"path": "a.png"})}}
    done = {"type": "response.completed", "response": {"usage": None}}
    fin = {"type": "response.output_item.done", "item": {
        "type": "function_call", "call_id": "c2", "name": "finish",
        "arguments": json.dumps({"summary": "done"})}}
    turn2 = [{"type": "response.reasoning_summary_text.delta", "delta": p}
             for p in _syn_deltas("think ")]
    turn2 += [{"type": "response.output_text.delta", "delta": p}
              for p in _syn_deltas("note ")]
    turn2.append({"type": "response.output_item.added", "output_index": 1, "item": {
        "type": "function_call", "id": "w1", "name": "write_file"}})
    args = '{"path": "x.txt", "content": "' + "".join(_syn_deltas("file ")) + '"}'
    turn2 += [{"type": "response.function_call_arguments.delta", "item_id": "w1",
               "delta": args[k:k + 7]} for k in range(0, len(args), 7)]
    turn2 += [fin, done, {"_done": True}]
    text = harness.run("-t", "x", "-m", "gpt-6-sol",
                       stream_replies=[[call, done, {"_done": True}], turn2])
    _no_leak(harness.artifacts())
    assert text.count("readable tail") == 3 and "[image data omitted]" in text


def test_oneshot_streams_echo_redacted(harness, engine, tmp_path, no_pillow) -> None:
    write(tmp_path, "a.png", make_png(2, 2))
    gpt = [{"type": "response.reasoning_summary_text.delta", "delta": p}
           for p in _syn_deltas("think ")]
    gpt += [{"type": "response.output_text.delta", "delta": p} for p in _syn_deltas()]
    gpt += [{"type": "response.completed", "response": {"usage": None}}, {"_done": True}]
    text = harness.run("--oneshot", "-m", "gpt-6-sol", "-t", "look",
                       "--attach-image", "a.png", stream_replies=[gpt])
    _no_leak(harness.artifacts())
    assert text.count("readable tail") == 2
    chat = [{"choices": [{"delta": {"content": p}}]} for p in _syn_deltas()]
    text = harness.run("--oneshot", "-m", CLAUDE, "-t", "look", "--attach-image", "a.png",
                       stream_replies=[chat + [{"_done": True}]])
    _no_leak(harness.artifacts())
    assert "echo [image data omitted] readable tail" in text


# ---- text cap for image-bearing requests (gap 2) ---------------------------------

def _asset(engine, tmp_path):
    return engine.prepare_image(str(write(tmp_path, "cap.png", make_png(2, 2))), "cap.png")


def _chat_history(engine, asset, extra):
    # 4 + 2 + 7 + 6 + len(user) chars of text; the image itself is not text
    return {"model": "m-x", "messages": [
        {"role": "system", "content": "sys!"},
        {"role": "assistant", "content": "hi", "tool_calls": [
            {"id": "c1", "type": "function",
             "function": {"name": "list_dir", "arguments": '{"a":1}'}}]},
        {"role": "tool", "tool_call_id": "c1", "content": "result"},
        {"role": "user", "content": engine.user_content("u" * extra, [asset])}]}


def test_image_text_cap_exact_boundary_chat_history(engine, tmp_path, no_pillow) -> None:
    asset = _asset(engine, tmp_path)
    cap = engine.IMAGE_MAX_TEXT_CHARS
    assert cap == 600_000
    engine.check_payload_images(_chat_history(engine, asset, cap - 19))
    with pytest.raises(SystemExit) as ei:
        engine.check_payload_images(_chat_history(engine, asset, cap - 18))
    msg = str(ei.value.code)
    assert "m-x" in msg and "600,000" in msg and "600,001" in msg


def test_image_text_cap_ignores_text_only_and_image_bytes(engine, monkeypatch) -> None:
    engine.check_payload_images({"model": "m", "messages": [
        {"role": "user", "content": "x" * 700_000}]})
    engine.check_payload_images({"model": "m", "input": "x" * 700_000})
    big = engine.ImageAsset("big.png", "image/png", b"\0" * 900_000, 2, 2)
    monkeypatch.setattr(engine, "_wire_default",
                        lambda o: pytest.fail("image materialized to count text"))
    engine.check_payload_images({"model": "m", "messages": [
        {"role": "user", "content": engine.user_content("t" * 600_000, [big])}]})


def test_image_text_cap_responses_input(engine, tmp_path, no_pillow) -> None:
    asset = _asset(engine, tmp_path)
    cap = engine.IMAGE_MAX_TEXT_CHARS

    def body(n):
        return {"model": "gpt-x", "input": [
            {"role": "user", "content": [{"type": "input_text", "text": "q" * n},
                                         {"type": "input_image", "image_url": asset}]},
            {"type": "function_call", "call_id": "c", "name": "f", "arguments": "{}"},
            {"type": "function_call_output", "call_id": "c", "output": "out"}]}
    engine.check_payload_images(body(cap - 5))
    with pytest.raises(SystemExit) as ei:
        engine.check_payload_images(body(cap - 4))
    assert "gpt-x" in str(ei.value.code)


def test_image_oneshot_overlong_task_refused_before_config(harness, engine, tmp_path,
                                                           monkeypatch, capsys,
                                                           no_pillow) -> None:
    write(tmp_path, "a.png", make_png())
    monkeypatch.setattr(engine, "load_cfg", lambda: pytest.fail("config touched"))
    with pytest.raises(SystemExit) as ei:
        harness.run("--oneshot", "-m", CLAUDE, "-t", "x" * 600_001,
                    "--attach-image", "a.png", replies=[_answer()])
    assert ei.value.code == 2
    assert "600,000" in capsys.readouterr().err
    assert not (tmp_path / "index.jsonl").exists() and harness.payloads == []


def test_image_oneshot_exact_validator_cap_still_hits_context_budget(harness, tmp_path, no_pillow) -> None:
    write(tmp_path, "a.png", make_png())
    with pytest.raises(SystemExit) as exc:
        harness.run("--oneshot", "--context-policy", "legacy", "-m", "gpt-6-sol",
                    "-t", "x" * 600_000, "--attach-image", "a.png",
                    stream_replies=[_gpt_events()])
    assert exc.value.code == 3 and harness.stream_calls == []


def test_direct_gpt_stream_oneshot_checks_text_cap(engine, tmp_path, monkeypatch,
                                                   no_pillow) -> None:
    import types
    asset = _asset(engine, tmp_path)
    monkeypatch.setattr(engine, "stream_events",
                        lambda *a, **k: pytest.fail("request sent"))
    tr = engine.Transcript(str(tmp_path / "d.md"), "h")
    args = types.SimpleNamespace(model="gpt-6-sol", effort="high", temperature=None)
    with pytest.raises(SystemExit) as ei:
        engine.stream_gpt_oneshot(args, "u", "k", "p" * 600_001, tr, None, images=[asset])
    assert "gpt-6-sol" in str(ei.value.code)
    tr.close()


def test_claude_shorter_retry_overrun_refused(harness, engine, tmp_path, capsys,
                                              no_pillow, monkeypatch) -> None:
    # Isolate the unchanged 600,000-character image validator from the smaller
    # deterministic context budget exercised by the context-guard tests.
    monkeypatch.setattr(engine.capability_module("context_guard"), "REQUEST_LIMIT", 1000000)
    write(tmp_path, "a.png", make_png())
    task = "x" * (600_000 - len(engine.ONESHOT_SHORTER) + 1)
    slow = [engine.REPLY_CUT_SECONDS + 1] + _chat_events("cut")
    with pytest.raises(SystemExit) as ei:
        harness.run("--oneshot", "-m", CLAUDE, "-t", task, "--attach-image", "a.png",
                    stream_replies=[slow, _chat_events("short")])
    assert ei.value.code not in (0, None)
    assert len(harness.stream_calls) == 1
    assert "600,000" in str(ei.value.code) + capsys.readouterr().err

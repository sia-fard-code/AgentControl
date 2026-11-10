"""Context admission and reserved closeout (context-guard design).

Synthetic replies and temporary paths only; no network.
"""
from __future__ import annotations

import io
import json
import subprocess
import urllib.error
from types import SimpleNamespace

import pytest

from test_engine_reasoning import engine, harness  # noqa: F401
from test_engine_turn_budget import GPT, _call, _end, _prose, _run


def _cg(engine):
    return engine.capability_module("context_guard")


def _legacy(harness, *argv, **kw):
    # scope: historical byte-policy contract (256KiB request, 128KiB read, 192KiB output);
    # ordinary runs default to model-v1, so these cases select legacy explicitly.
    return _run(harness, "--context-policy", "legacy", *argv, **kw)


def test_request_bytes_utf8_escaping_and_images(engine):
    cg = _cg(engine)
    assert cg.request_bytes("é") == 2
    assert cg.request_bytes({"a": "é"}) == len(json.dumps({"a": "é"}).encode("utf-8"))
    url = "data:image/png;base64," + "A" * 100000
    assert cg.request_bytes(url) == len(url.encode("utf-8"))
    with pytest.raises(TypeError):
        cg.request_bytes([object()])
    asset = engine.ImageAsset("a.png", "image/png", b"PRIVATE", 1, 1)
    assert cg.request_bytes([asset], image_type=engine.ImageAsset) == len('[""]') + cg.IMAGE_COST
    assert cg.request_bytes({"a": "\n\"\\é"}) == len(json.dumps(
        {"a": "\n\"\\é"}).encode("utf-8"))


def test_data_url_looking_text_payload_exceeds_request_limit(engine):
    cg = _cg(engine)
    payload = {"messages": [{"role": "user", "content":
                            "data:image/png;base64," + "A" * (1024 * 1024)}]}
    size = len(json.dumps(payload).encode("utf-8"))
    assert size > cg.REQUEST_LIMIT
    assert cg.request_bytes(payload, image_type=engine.ImageAsset) == size
    assert not cg.fits(payload, cg.REQUEST_LIMIT, image_type=engine.ImageAsset)


def test_admit_equality_allowed_plus_one_refused(engine):
    cg = _cg(engine)
    g = cg.Guard()
    assert g.start_batch("", 1)
    ok = "x" * cg.BATCH_BUDGET
    assert g.admit("grep", ok) == ok
    refused = g.admit("grep", "y")
    assert "[context budget]" in refused and "per-batch" in refused
    assert g.closing()
    g = cg.Guard()
    g.start_batch("", 1)
    g.read = cg.READ_BUDGET - 10
    assert g.admit("load_skill", "z" * 10) == "z" * 10
    assert "cumulative read" in g.admit("load_skill", "z")
    assert g.closing()


def test_first_exhaustion_closes_and_charges_refusal_without_echo(engine):
    cg = _cg(engine)
    g = cg.Guard()
    g.start_batch({}, 1)
    g.read = cg.READ_BUDGET
    before = g.base
    out = g.admit("load_skill", "PRIVATE skill text")
    assert g.closing(), "the first exhausted budget must stop further tools"
    assert "PRIVATE" not in out
    assert g.output == len(out.encode("utf-8"))
    assert g.batch == g.output
    assert g.base > before - cg.COMPACT_RESULT


def test_refused_command_keeps_exit_line_not_output(engine):
    g = _cg(engine).Guard()
    g.start_batch("", 1)
    g.output = _cg(engine).OUTPUT_BUDGET
    out = g.admit("run_command", "exit=1\n--- stdout ---\nSECRET")
    assert out.startswith("exit=1\n") and "SECRET" not in out and "not re-run" in out


def test_envelope_over_limit_is_not_dispatched(engine):
    cg = _cg(engine)
    g = cg.Guard()
    assert not g.start_batch("x" * (cg.REQUEST_LIMIT - cg.CLOSEOUT_RESERVE), 1)


def test_oversized_initial_task_fails_before_any_model_call(harness, tmp_path, capsys):
    code = _legacy(harness, "-m", GPT, "-t", "q" * (300 * 1024), replies=[])
    assert code == 3 and harness.payloads == []
    assert _end(tmp_path)["stop_reason"] == "context_budget"
    assert capsys.readouterr().out.startswith("[PARTIAL - context_budget")


@pytest.mark.parametrize("oneshot", [False, True], ids=["worker", "oneshot"])
def test_oversized_data_url_looking_task_is_text_only_local_partial(
        harness, tmp_path, capsys, oneshot):
    task = "data:image/png;base64," + "A" * (1024 * 1024)
    # legacy: asserts the frozen byte cap; oneshot now defaults to the 1M-token model-v1 window
    mode = ["--oneshot"] if oneshot else ["--max-iters", "2"]
    code = _legacy(harness, *mode, "-m", GPT, "--text-only", "-t", task,
                replies=[_prose(), _prose()])
    assert harness.payloads == [] and harness.stream_calls == []
    assert code == 3
    assert _end(tmp_path)["stop_reason"] == "context_budget"
    assert capsys.readouterr().out.startswith("[PARTIAL - context_budget")


def test_native_batch_pairs_every_call_and_closes(harness, tmp_path):
    for i in range(4):
        (tmp_path / f"f{i}.txt").write_text("x" * 25000, encoding="utf-8")
    calls = [{"id": f"b{i}", "type": "function", "function": {
        "name": "read_file", "arguments": json.dumps({"path": f"f{i}.txt"})}}
        for i in range(4)] + [{"id": "fin", "type": "function", "function": {
            "name": "finish", "arguments": json.dumps({"summary": "done"})}}]
    batch = {"choices": [{"message": {"content": "", "tool_calls": calls}}]}
    code = _legacy(harness, "-m", GPT, replies=[batch, _prose("closeout")])
    assert code == 3 and _end(tmp_path)["status"] == "failed"
    assert _end(tmp_path)["stop_reason"] == "context_budget"
    report = harness.payloads[-1]
    answered = [m["tool_call_id"] for m in report["messages"] if m["role"] == "tool"]
    assert answered == [c["id"] for c in calls]       # every call paired, in order
    assert _tool_text(report) <= 64 * 1024 + 4096    # per-batch budget + refusals


@pytest.mark.parametrize("kind", ["parse", "no_tool", "finish"])
def test_prompt_large_envelope_stops_before_parse_repair_or_finish(harness, tmp_path, kind):
    tail = {"parse": '```tool {bad} ```', "no_tool": "", "finish":
            '```tool {"name":"finish","args":{"summary":"done"}}```'}[kind]
    reply = _prose("x" * 200000 + tail)
    code = _legacy(harness, "-m", "anthropic_tool_call.claude-x", "--tool-mode", "prompt",
                "--allow-prompt-tools", replies=[reply, _prose("partial")])
    assert code == 3 and _end(tmp_path)["stop_reason"] == "context_budget"
    assert all("x" * 1000 not in json.dumps(p) for p in harness.payloads[1:])


def test_exhaustion_skips_mutation_finish_review_and_advisor(harness, engine, tmp_path, monkeypatch):
    cg = _cg(engine)
    monkeypatch.setattr(cg, "BATCH_BUDGET", 20)
    dispatched = []
    original = engine.Runner.dispatch
    def dispatch(self, name, args):
        dispatched.append(name)
        return original(self, name, args)
    monkeypatch.setattr(engine.Runner, "dispatch", dispatch)
    calls = [_call("list_dir", {"path": "."}, "a"),
             _call("write_file", {"path": "never.txt", "content": "bad"}, "b"),
             _call("finish", {"summary": "done"}, "c")]
    batch = {"choices": [{"message": {"content": "", "tool_calls": [
        x["choices"][0]["message"]["tool_calls"][0] for x in calls]}}]}
    code = _legacy(harness, "-m", GPT, "--review-finish", "--supervise-stuck",
                   replies=[batch, _prose("partial")])
    assert code == 3 and dispatched == ["list_dir"]
    assert not (tmp_path / "never.txt").exists()
    assert not (tmp_path / "agent.patch").exists()
    assert len(harness.payloads) == 2
    assert [m["tool_call_id"] for m in harness.payloads[-1]["messages"]
            if m["role"] == "tool"] == ["a", "b", "c"]


@pytest.mark.parametrize("stream", [False, True])
def test_oneshot_oversize_is_local_partial_without_transport(harness, tmp_path, stream):
    kwargs = {"stream_replies": []} if stream else {}
    try:
        harness.run("--oneshot", "--context-policy", "legacy", "-m", GPT, "-t", "q" * 270000,
                    replies=[], **kwargs)
    except SystemExit as exc:
        assert exc.code == 3
    else:
        pytest.fail("oversized one-shot must fail incomplete")
    assert harness.payloads == [] and harness.stream_calls == []
    assert _end(tmp_path)["stop_reason"] == "context_budget"


def test_oneshot_default_model_v1_admits_past_legacy_byte_cap(harness, tmp_path):
    # pair of the legacy test above: default policy uses the 1M-token window
    _run(harness, "--oneshot", "-m", GPT, "-t", "q" * 270000, replies=[_prose()])
    assert len(harness.payloads) == 1
    assert _end(tmp_path).get("stop_reason") != "context_budget"


def test_auxiliary_request_preflight_before_chat(engine, monkeypatch):
    monkeypatch.setattr(engine, "chat", lambda *a: pytest.fail("oversized auxiliary request sent"))
    with pytest.raises(_cg(engine).ContextBudgetError):
        engine.chat_with_model_retry("u", "k", {"messages": [{"role": "user", "content": "x" * 270000}]}, 1)


def test_retry_growth_rechecked(harness, engine, tmp_path, monkeypatch):
    cg = _cg(engine)
    monkeypatch.setattr(cg, "REQUEST_LIMIT", 2000)
    monkeypatch.setattr(engine, "ONESHOT_SHORTER", "x" * 2000)
    code = _run(harness, "--oneshot", "--context-policy", "legacy", "-m", "qwen-x",
                replies=[_prose("Error: ")], durations=[121])
    assert code == 3 and len(harness.payloads) == 1
    assert _end(tmp_path)["stop_reason"] == "context_budget"


@pytest.mark.parametrize("write", [False, True])
def test_provider_context_rejection_saves_patch_before_local_closeout(harness, engine, tmp_path, monkeypatch, write):
    fatal = {"_fatal": True, "_status": 400, "_error_detail": "maximum context length exceeded PRIVATE"}
    code = _run(harness, "-m", GPT, *(["--write"] if write else []), replies=[
        _call("write_file", {"path": "edited.txt", "content": "saved"}), fatal])
    assert code == 3 and len(harness.payloads) == 2
    assert _end(tmp_path)["stop_reason"] == "context_budget"
    if write:
        assert (tmp_path / "edited.txt").read_text() == "saved"
    else:
        assert "+saved" in (tmp_path / "agent.patch").read_text()


def test_patch_saved_before_optional_report(harness, engine, tmp_path, monkeypatch):
    original = engine.call_model
    def request(url, key, payload, mode, *a, **kw):
        if mode == "oneshot":
            assert "+saved" in (tmp_path / "agent.patch").read_text()
        return original(url, key, payload, mode, *a, **kw)
    monkeypatch.setattr(engine, "call_model", request)
    code = _run(harness, "-m", GPT, "--max-iters", "2", replies=[
        _call("write_file", {"path": "edited.txt", "content": "saved"}), _prose("partial")])
    assert code == 3


def test_text_only_refuses_before_image_prepare(harness, engine, tmp_path, monkeypatch):
    monkeypatch.setattr(engine, "prepare_image", lambda *a: pytest.fail("image loaded in text-only mode"))
    code = _run(harness, "-m", GPT, "--text-only", replies=[
        _call("view_image", {"path": "never.png"}), _call("finish", {"summary": "no image"})])
    assert code == 0
    assert "text-only" in harness.payloads[1]["messages"][-1]["content"]


def test_review_context_exhaustion_cannot_complete(harness, engine, tmp_path, monkeypatch):
    original = engine.chat_with_model_retry
    def request(url, key, payload, turn, **kw):
        if "tools" not in payload:
            raise _cg(engine).ContextBudgetError("provider context", provider=True)
        return original(url, key, payload, turn, **kw)
    monkeypatch.setattr(engine, "chat_with_model_retry", request)
    code = _run(harness, "-m", GPT, "--review-finish", replies=[_call("finish", {"summary": "done"})])
    assert code == 3 and _end(tmp_path)["stop_reason"] == "context_budget"


def test_transformed_responses_request_preflight(engine, monkeypatch):
    monkeypatch.setattr(engine, "stream_events", lambda *a, **kw: pytest.fail("oversized transformed request sent"))
    with pytest.raises(_cg(engine).ContextBudgetError):
        engine.stream_gpt_turn("u", "k", {"model": GPT, "input": "x" * 270000}, None)


def test_context_metadata_contains_only_bounded_accounting(harness, tmp_path):
    secret = "PRIVATE_TASK_TEXT"
    assert _legacy(harness, "-m", GPT, "-t", secret + "x" * 270000, replies=[]) == 3
    path = harness.transcript_path.with_name("run.artifacts") / "completion.json"
    meta = json.loads(path.read_text())["context_budget"]
    assert set(meta) == {"reason", "usage", "limits", "report_source"}
    assert meta["reason"] == "request context" and meta["report_source"] == "local"
    assert secret not in json.dumps(meta)


@pytest.mark.parametrize("tool", ["grep", "list_dir", "mcp__test__large", "load_skill", "read_file"])
def test_arbitrary_refused_output_is_never_echoed(engine, tool):
    cg = _cg(engine)
    g = cg.Guard()
    g.start_batch({}, 1)
    g.output = cg.OUTPUT_BUDGET
    result = g.admit(tool, "PRIVATE_HEADER\nPRIVATE_BODY")
    assert "PRIVATE" not in result and g.closing()
    assert g.rejected == len("PRIVATE_HEADER\nPRIVATE_BODY")
    assert g.output > cg.OUTPUT_BUDGET


def test_result_json_escaping_is_prospectively_charged(engine, monkeypatch):
    cg = _cg(engine)
    monkeypatch.setattr(cg, "REQUEST_LIMIT", cg.CLOSEOUT_RESERVE + 1024)
    g = cg.Guard()
    assert g.start_batch({}, 1)
    assert "context budget" in g.admit("grep", "\\" * 600)
    assert g.closing()


def test_large_engine_notice_not_appended_or_followed_by_tools(harness, engine, tmp_path, monkeypatch):
    monkeypatch.setattr(engine, "CLAUDE_MULTI_REFUSED", "PRIVATE" * 40000)
    code = _legacy(harness, "-m", "anthropic_tool_call.claude-x", replies=[
        {"choices": [{"message": {"content": "", "tool_calls": [
            _call("list_dir", {"path": "."}, "a")["choices"][0]["message"]["tool_calls"][0],
            _call("finish", {"summary": "done"}, "b")["choices"][0]["message"]["tool_calls"][0]]}}]},
        _prose("partial")])
    assert code == 3 and _end(tmp_path)["stop_reason"] == "context_budget"
    assert "PRIVATE" not in json.dumps(harness.payloads[-1])


def test_overlarge_operator_guidance_is_refused_before_history(harness, engine, tmp_path, monkeypatch):
    original = engine.Runner.dispatch
    def dispatch(self, name, args):
        result = original(self, name, args)
        (harness.transcript_path.parent / "run.md.steer").write_text("PRIVATE" * 40000)
        return result
    monkeypatch.setattr(engine.Runner, "dispatch", dispatch)
    code = _legacy(harness, "-m", GPT, replies=[_call("list_dir", {"path": "."}), _prose("partial")])
    assert code == 3 and len(harness.payloads) == 2
    rows = harness.transcript_path.with_name("run.messages.jsonl").read_text()
    assert "PRIVATE" not in rows


def test_stream_provider_context_error_never_falls_back(harness, engine, tmp_path):
    try:
        harness.run("--oneshot", "-m", GPT, replies=[], stream_replies=[
            engine.StreamError("http", "HTTP 400: context_length_exceeded")])
    except SystemExit as exc:
        assert exc.code == 3
    assert len(harness.stream_calls) == 1 and harness.payloads == []
    assert _end(tmp_path)["stop_reason"] == "context_budget"


def test_worker_responses_context_error_never_retried(harness, tmp_path):
    try:
        harness.run("-m", GPT, replies=[], stream_replies=[[
            {"type": "response.failed", "response": {"error": {"code": "context_length_exceeded"}}}]])
    except SystemExit as exc:
        assert exc.code == 3
    assert len(harness.stream_calls) == 1 and harness.payloads == []
    assert _end(tmp_path)["stop_reason"] == "context_budget"


@pytest.mark.parametrize("source", ["skill", "artifact"])
def test_uri_reads_share_cumulative_read_budget(engine, tmp_path, source):
    skill = tmp_path / ".claude" / "skills" / "synthetic"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("---\nname: synthetic\ndescription: test\n---\nbody")
    (skill / "large.txt").write_text("x" * 20000)
    runner = engine.Runner(str(tmp_path), skills_home=str(tmp_path / "nohome"),
                           artifact_dir=str(tmp_path.parent / (tmp_path.name + "-artifacts")))
    try:
        if source == "skill":
            runner.load_skill("synthetic")
            uri = "skill://synthetic/large.txt"
        else:
            uri = runner.caps.save_artifact(b"x" * 20000, ".txt")
            assert uri.startswith("artifact://")
        out = runner.dispatch("read_file", {"path": uri})
        assert "x" * 1000 in out
        cg = _cg(engine)
        g = cg.Guard()
        g.start_batch({}, 1)
        g.read = cg.READ_BUDGET - 100
        assert "cumulative read" in g.admit("read_file", out) and g.closing()
    finally:
        runner.close()


@pytest.mark.parametrize("part", ["task", "schema", "skill"])
def test_whole_request_overhead_prevents_model_call(harness, engine, tmp_path, monkeypatch, part):
    argv = []
    if part == "task":
        argv = ["-t", "x" * 190000]
        # Supply deterministic overhead rather than depending on the real user's
        # installed skill catalog (operator HOME is intentionally isolated).
        monkeypatch.setattr(engine, "preload_skills",
                            lambda *a: [("synthetic", "x" * 8000)])
    elif part == "schema":
        original = engine.native_tools
        monkeypatch.setattr(engine, "native_tools", lambda *a: original(*a) + [
            {"type": "function", "function": {"name": "large", "description": "x" * 200000}}])
    else:
        monkeypatch.setattr(engine, "preload_skills", lambda *a: [("synthetic", "x" * 200000)])
    code = _legacy(harness, "-m", GPT, *argv, replies=[_prose("partial")])
    assert code == 3 and _end(tmp_path)["stop_reason"] == "context_budget"
    assert all("tools" not in p for p in harness.payloads)


def test_finish_correction_result_is_budgeted_before_append(harness, engine, tmp_path, monkeypatch):
    monkeypatch.setattr(_cg(engine), "BATCH_BUDGET", 100)
    code = _legacy(harness, "-m", GPT, "--write", "--verify-command", json.dumps(["python", "-m", "py_compile", "x.py"]),
                replies=[_call("write_file", {"path": "x.py", "content": "x=1"}),
                         _call("finish", {"summary": "done"}), _prose("partial")])
    assert code == 3 and len(harness.payloads) == 3
    assert _end(tmp_path)["stop_reason"] == "context_budget"
    tools = [m["content"] for m in harness.payloads[-1]["messages"] if m["role"] == "tool"]
    assert "context budget" in tools[-1] and "Run the required" not in tools[-1]


@pytest.mark.parametrize("unknown", [False, True])
def test_command_exhaustion_preserves_structured_evidence_no_replay(harness, engine, tmp_path, monkeypatch, unknown):
    cg = _cg(engine)
    monkeypatch.setattr(cg, "BATCH_BUDGET", 10)
    monkeypatch.setattr(engine.shutil, "which", lambda name: name)
    calls = []
    def command(argv, **kw):
        calls.append(argv)
        (tmp_path / "command-edit.txt").write_text("changed")
        if unknown:
            raise subprocess.TimeoutExpired(argv, 1)
        return subprocess.CompletedProcess(argv, 7, "PRIVATE" * 1000, "")
    monkeypatch.setattr(engine.subprocess, "run", command)
    actions = [_call("run_command", {"argv": ["python", "-m", "py_compile", "x.py"]}, "a"),
               _call("write_file", {"path": "never.txt", "content": "bad"}, "b"),
               _call("finish", {"summary": "done"}, "c")]
    batch = {"choices": [{"message": {"content": "", "tool_calls": [
        x["choices"][0]["message"]["tool_calls"][0] for x in actions]}}]}
    code = _legacy(harness, "-m", GPT, "--write", replies=[batch, _prose("partial")])
    assert code == 3 and len(calls) == 1
    assert _end(tmp_path)["stop_reason"] == ("mutation_outcome_unknown" if unknown else "context_budget")
    assert (tmp_path / "command-edit.txt").read_text() == "changed" and not (tmp_path / "never.txt").exists()
    result = next(m["content"] for m in harness.payloads[-1]["messages"] if m["role"] == "tool")
    assert "PRIVATE" not in result
    if not unknown:
        assert result.startswith("exit=7\n")
    completion = json.loads((harness.transcript_path.with_suffix(".artifacts") / "completion.json").read_text())
    assert completion["gate"]


def test_report_must_leave_full_reserve_no_doomed_request(harness, engine, tmp_path, monkeypatch, capsys):
    cg = _cg(engine)
    original = engine.call_model
    def request(*a, **kw):
        result = original(*a, **kw)
        monkeypatch.setattr(cg, "CLOSEOUT_RESERVE", cg.REQUEST_LIMIT - 1)
        return result
    monkeypatch.setattr(engine, "call_model", request)
    code = _legacy(harness, "-m", GPT, "--max-iters", "2", replies=[
        _call("write_file", {"path": "saved.txt", "content": "saved"})])
    assert code == 3 and len(harness.payloads) == 1
    assert "LOCAL factual report" in capsys.readouterr().out


def test_request_and_output_exact_equality_plus_one(engine, monkeypatch):
    cg = _cg(engine)
    g = cg.Guard()
    work = cg.REQUEST_LIMIT - cg.CLOSEOUT_RESERVE
    assert g.start_batch("x" * (work - cg.COMPACT_RESULT), 1)
    assert not cg.Guard().start_batch("x" * (work - cg.COMPACT_RESULT + 1), 1)
    g = cg.Guard()
    g.start_batch({}, 1)
    g.output = cg.OUTPUT_BUDGET - 1
    assert g.admit("grep", "x") == "x"
    g.start_batch({}, 1)
    assert "cumulative tool output" in g.admit("grep", "x") and g.closing()


def test_http_422_context_rejection_is_not_transient(engine, monkeypatch):
    def reject(*a, **kw):
        raise urllib.error.HTTPError("http://gw", 422, "bad", {},
                                    io.BytesIO(b'{"error":{"message":"prompt too long"}}'))
    monkeypatch.setattr(engine.urllib.request, "urlopen", reject)
    with pytest.raises(_cg(engine).ContextBudgetError):
        engine.chat("http://gw", "k", {"model": GPT, "messages": []})


def test_long_call_id_result_envelope_reserved_before_dispatch(harness, engine, tmp_path, monkeypatch):
    cg = _cg(engine)
    cg_limit = 100000 + cg.CLOSEOUT_RESERVE
    monkeypatch.setattr(cg, "REQUEST_LIMIT", cg_limit)
    dispatched = []
    monkeypatch.setattr(engine.Runner, "dispatch", lambda *a: dispatched.append(a) or "ran")
    code = _legacy(harness, "-m", GPT, replies=[_call("list_dir", {"path": "."}, "c" * 60000), _prose("partial")])
    assert code == 3 and dispatched == []
    assert _end(tmp_path)["stop_reason"] == "context_budget"


@pytest.mark.parametrize("policy", ["legacy", "model-v1"])
def test_stream_http_413_never_falls_back(harness, engine, tmp_path, policy):
    # legacy: 413 is a context rejection; model-v1: 413 is transport, never a token-window
    # signal -- but resending the same body non-streamed is doomed under either policy.
    try:
        harness.run("--oneshot", "--context-policy", policy, "-m", GPT, replies=[], stream_replies=[
            engine.StreamError("http", "HTTP 413: request too large")])
    except SystemExit as exc:
        assert exc.code == 3
    assert len(harness.stream_calls) == 1 and not harness.payloads
    reason = _end(tmp_path).get("stop_reason")
    assert (reason == "context_budget") if policy == "legacy" else (reason != "context_budget")


def test_advisor_context_limit_stops_without_more_tools(harness, engine, tmp_path, monkeypatch):
    original = engine.chat_with_model_retry
    advisory_requests = []
    def request(url, key, payload, turn, **kw):
        if "tools" not in payload:
            advisory_requests.append(payload)
            raise _cg(engine).ContextBudgetError("provider context", provider=True)
        return original(url, key, payload, turn, **kw)
    monkeypatch.setattr(engine, "chat_with_model_retry", request)
    reads = [_call("read_file", {"path": "small.txt"}, f"r{i}") for i in range(8)]
    (tmp_path / "small.txt").write_text("synthetic text")
    code = _run(harness, "-m", GPT, "--supervise-stuck", replies=reads)
    assert code == 3 and len(advisory_requests) == 1
    assert _end(tmp_path)["stop_reason"] == "context_budget"
    assert len(harness.payloads) < 8


def test_mcp_text_exhaustion_pairs_skips_without_echo(harness, engine, tmp_path, monkeypatch):
    monkeypatch.setattr(engine.Capabilities, "call_mcp", lambda *a: "PRIVATE_MCP" * 3000)
    monkeypatch.setattr(_cg(engine), "BATCH_BUDGET", 100)
    actions = [_call("mcp__synthetic__read", {}, "m"),
               _call("write_file", {"path": "never.txt", "content": "bad"}, "w"),
               _call("finish", {"summary": "done"}, "f")]
    batch = {"choices": [{"message": {"content": "", "tool_calls": [
        x["choices"][0]["message"]["tool_calls"][0] for x in actions]}}]}
    code = _legacy(harness, "-m", GPT, replies=[batch, _prose("partial")])
    assert code == 3 and not (tmp_path / "never.txt").exists()
    tools = [m for m in harness.payloads[-1]["messages"] if m["role"] == "tool"]
    assert [m["tool_call_id"] for m in tools] == ["m", "w", "f"]
    assert "PRIVATE_MCP" not in json.dumps(tools)


def _tool_text(payload):
    return sum(len(m["content"]) for m in payload["messages"]
               if m["role"] == "tool" and isinstance(m["content"], str))


def test_cumulative_reads_close_out_as_context_budget(harness, tmp_path, capsys):
    for i in range(12):
        (tmp_path / f"big{i}.txt").write_text("x" * 25000, encoding="utf-8")
    reads = [_call("read_file", {"path": f"big{i}.txt"}, cid=f"r{i}") for i in range(12)]
    code = _legacy(harness, "-m", GPT, "--max-iters", "30",
                replies=reads + [_prose("closeout report")] * 3)
    assert code == 3
    end = _end(tmp_path)
    assert end["status"] == "failed" and end["stop_reason"] == "context_budget"
    # 128KiB cumulative read budget: no request ever carries more read text.
    assert max(_tool_text(p) for p in harness.payloads) <= 128 * 1024 + 4096
    assert "tools" not in harness.payloads[-1]
    out = capsys.readouterr().out
    assert out.startswith("[PARTIAL") and "context_budget" in out

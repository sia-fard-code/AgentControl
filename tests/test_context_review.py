"""Regressions for the ctx-final-review-1b47 findings. Synthetic text, temp paths only."""
import importlib.util, json, os, sys
from pathlib import Path
import pytest

from test_engine_reasoning import engine, harness  # noqa: F401

ENGINE_DIR = Path(__file__).resolve().parents[1] / "engine"
ORIGINAL = b"SYNTHETIC original\r\nbytes\n"


@pytest.fixture
def eng():
    sys.path.insert(0, str(ENGINE_DIR))
    spec = importlib.util.spec_from_file_location("delegate_ctx_review", ENGINE_DIR / "delegate.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    yield mod
    sys.path.remove(str(ENGINE_DIR))


def _disk(eng, tmp_path):
    (tmp_path / "a.txt").write_bytes(ORIGINAL)
    return eng.Runner(str(tmp_path), write_mode="disk")


@pytest.mark.parametrize("content", [5, "bad \ud800 surrogate"])
def test_invalid_write_content_never_truncates(eng, tmp_path, content):
    with pytest.raises(ValueError):
        eng.checked_tool_arguments("write_file", {"path": "a.txt", "content": content})
    out = _disk(eng, tmp_path).dispatch("write_file", {"path": "a.txt", "content": content})
    assert out.startswith("error:")
    assert (tmp_path / "a.txt").read_bytes() == ORIGINAL


def test_direct_writes_validate_before_destructive_open(eng, tmp_path):
    run = _disk(eng, tmp_path)
    with pytest.raises((TypeError, ValueError)):
        run.write_file("a.txt", 5)
    with pytest.raises((TypeError, ValueError, UnicodeError)):
        run.write_file("a.txt", "x\udfff")
    out = run.dispatch("replace_in_file", {"path": "a.txt", "old_string": "bytes",
                                           "new_string": "\ud800"})
    assert out.startswith("error:")
    assert (tmp_path / "a.txt").read_bytes() == ORIGINAL
    assert run.candidate_seq == 0


def test_injected_io_failure_keeps_existing_bytes(eng, tmp_path, monkeypatch):
    run = _disk(eng, tmp_path)

    def boom(*a, **k):
        raise OSError("synthetic disk full")
    monkeypatch.setattr(eng.os, "replace", boom)
    out = run.dispatch("write_file", {"path": "a.txt", "content": "new synthetic"})
    assert out.startswith("error:")
    assert (tmp_path / "a.txt").read_bytes() == ORIGINAL
    assert [p.name for p in tmp_path.iterdir()] == ["a.txt"]   # no temp left behind
    assert run.candidate_seq == 1          # uncertain mutation still advances the candidate


def test_grep_fallback_skips_symlinked_file_outside_root(eng, tmp_path, monkeypatch):
    root, outside = tmp_path / "repo", tmp_path / "outside"
    root.mkdir(), outside.mkdir()
    (outside / "secret.txt").write_text("SYNTHETIC_EXTERNAL_TOKEN\n")
    (root / "inside.txt").write_text("SYNTHETIC_EXTERNAL_TOKEN inside\n")
    try:
        os.symlink(outside / "secret.txt", root / "link.txt")
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable on this host")

    def no_rg(*a, **k):
        raise FileNotFoundError("rg")
    monkeypatch.setattr(eng.subprocess, "run", no_rg)
    out = eng.Runner(str(root), profile="legacy").grep("SYNTHETIC_EXTERNAL_TOKEN")
    assert "inside.txt" in out
    assert "link.txt" not in out


@pytest.mark.parametrize("protocol", ["native", "prompt"])
def test_timed_out_command_receipt_is_unknown(harness, engine, monkeypatch, tmp_path, protocol):
    from test_engine_turn_budget import GPT, _run
    monkeypatch.syspath_prepend(str(ENGINE_DIR))
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.delenv("_HARNESS_APPROVED_WORKER", raising=False)
    runtime = engine.capability_module("context_runtime").ContextRuntime
    original_admit, admitted, dispatched = runtime.admit_result, [], []

    def admit(self, name, *a, **k):
        admitted.append((name, k.get("outcome"), k.get("exit_code")))
        return original_admit(self, name, *a, **k)

    def dispatch(self, name, args):
        dispatched.append(name)
        self._last_cmd_info = ("timed_out", True, None, False)
        self.command_timed_out = True
        return "synthetic timeout"

    monkeypatch.setattr(runtime, "admit_result", admit)
    monkeypatch.setattr(engine.Runner, "dispatch", dispatch)
    call = {"argv": ["python", "x.py"]}
    if protocol == "native":
        first = {"choices": [{"message": {"content": "", "tool_calls": [
            {"id": "c1", "type": "function", "function": {
                "name": "run_command", "arguments": json.dumps(call)}}]},
            "finish_reason": "stop"}]}
        args = ("-m", GPT, "--write")
    else:
        first = {"choices": [{"message": {"content": "```tool " + json.dumps(
            {"name": "run_command", "args": call}) + "```"}, "finish_reason": "stop"}]}
        args = ("-m", "qwen3.6:latest", "--write")
    partial = {"choices": [{"message": {"content": "synthetic partial"},
                            "finish_reason": "stop"}]}
    _run(harness, *args, replies=[first, partial])
    assert dispatched == ["run_command"]
    assert admitted == [("run_command", "unknown", None)]


BAD_USAGE = {"prompt_tokens": 4321, "completion_tokens": 7}


def _rows(harness, suffix):
    p = harness.transcript_path.with_suffix(suffix)
    return [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines()] if p.exists() else []


@pytest.mark.parametrize("mode", ["native", "prompt"])
@pytest.mark.parametrize("bad", [
    {"choices": [], "usage": BAD_USAGE, "x": "SYNTHETIC_RAW_BODY"},
    {"usage": BAD_USAGE, "x": "SYNTHETIC_RAW_BODY"},
    {"choices": [None], "usage": BAD_USAGE, "x": "SYNTHETIC_RAW_BODY"},
    {"choices": "SYNTHETIC_RAW_BODY", "usage": BAD_USAGE},
    ["SYNTHETIC_RAW_BODY"],
], ids=["empty", "missing", "null-choice", "str-choices", "list"])
def test_malformed_envelope_journals_usage_and_runs_no_tool(harness, monkeypatch, tmp_path,
                                                             mode, bad):
    from test_engine_reasoning import _finish_reply
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.delenv("_HARNESS_APPROVED_WORKER", raising=False)
    model = "gpt-6-sol" if mode == "native" else "qwen3.6:latest"
    harness.run("-m", model, replies=[bad, _finish_reply(mode)])
    assert len(harness.payloads) == 2      # rejected envelope retried, nothing executed
    sent = [r.get("sent") for r in _rows(harness, ".usage.jsonl")]
    if isinstance(bad, dict):
        assert sent.count(4321) == 1       # valid usage from the bad envelope, once
    for suffix in (".anomalies.jsonl", ".usage.jsonl"):
        assert "SYNTHETIC_RAW_BODY" not in json.dumps(_rows(harness, suffix))


def test_responses_disconnect_after_completed_keeps_usage_once(harness, engine, monkeypatch,
                                                               tmp_path):
    from test_engine_live_turns_gpt import finish_turn
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.delenv("_HARNESS_APPROVED_WORKER", raising=False)
    usage = {"input_tokens": 9876, "output_tokens": 5}
    broken = [{"type": "response.created"},
              {"type": "response.completed", "response": {"usage": usage}},
              engine.StreamError("transport", "synthetic disconnect", 2)]
    harness.run("-m", "gpt-6-sol", replies=[], stream_replies=[broken, finish_turn()])
    sent = [r.get("sent") for r in _rows(harness, ".usage.jsonl")]
    assert sent.count(9876) == 1


@pytest.mark.parametrize("route", ["responses", "chat"])
def test_oneshot_disconnect_after_usage_keeps_usage_once(harness, engine, monkeypatch,
                                                         tmp_path, route):
    from test_engine_oneshot_stream import CLAUDE, _chat_events
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    if route == "responses":
        model, usage = "gpt-6-sol", {"input_tokens": 9876, "output_tokens": 5}
        script = [{"type": "response.output_text.delta", "delta": "synthetic answer"},
                  {"type": "response.completed", "response": {"usage": usage}}]
    else:
        model, usage = CLAUDE, {"prompt_tokens": 9876, "completion_tokens": 5}
        script = _chat_events(["synthetic answer"], usage=usage, done=False)
    script.append(engine.StreamError("transport", "synthetic disconnect", len(script)))
    with pytest.raises(SystemExit):
        harness.run("--oneshot", "-m", model, replies=[], stream_replies=[script])
    sent = [r.get("sent") for r in _rows(harness, ".usage.jsonl")]
    assert sent.count(9876) == 1


@pytest.mark.parametrize("model,path", [
    ("gpt-6-sol", "/openai/responses"),
    ("anthropic_tool_call.claude-x", "/api/chat/completions")])
@pytest.mark.parametrize("oneshot", [False, True])
@pytest.mark.parametrize("usage", [None, {"prompt_tokens": 123, "completion_tokens": 4}])
def test_stream_413_stops_without_replay_and_preserves_usage(
        harness, engine, tmp_path, monkeypatch, model, path, oneshot, usage):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.delenv("_HARNESS_APPROVED_WORKER", raising=False)
    args = ("--oneshot",) if oneshot else ()
    with pytest.raises(SystemExit) as exc:
        harness.run(*args, "--context-policy", "model-v1", "-m", model, replies=[],
                    stream_replies=[engine.StreamError(
                        "http", "HTTP 413: synthetic transport limit", usage=usage)])
    assert exc.value.code == 3
    assert [p for p, _ in harness.stream_calls] == [path]
    assert harness.payloads == []
    assert _end(tmp_path).get("stop_reason") != "context_budget"
    assert _end(tmp_path)["sent"] == (123 if usage else None)
    rows = _rows(harness, ".usage.jsonl")
    assert len(rows) == 1 and rows[0]["sent"] == (123 if usage else None)


@pytest.mark.parametrize("model", ["gpt-6-sol", "anthropic_tool_call.claude-x"])
@pytest.mark.parametrize("oneshot", [False, True])
def test_nonstream_413_stops_without_replay(harness, tmp_path, monkeypatch, model, oneshot):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.delenv("_HARNESS_APPROVED_WORKER", raising=False)
    args = ("--oneshot",) if oneshot else ()
    # chat() maps a model-v1 HTTP 413 to this sentinel (test_context_engine).
    with pytest.raises(SystemExit) as exc:
        harness.run(*args, "--context-policy", "model-v1", "-m", model,
                    replies=[{"_transient": "HTTP 413"} for _ in range(8)])
    assert exc.value.code == 3
    assert len(harness.payloads) == 1 and harness.stream_calls == []
    assert _end(tmp_path).get("stop_reason") != "context_budget"
    assert _end(tmp_path)["sent"] is None
    rows = _rows(harness, ".usage.jsonl")
    assert len(rows) == 1 and rows[0]["sent"] is None


def _done(usage, text="synthetic answer"):
    resp = {"usage": usage} if usage else {}
    return [{"type": "response.output_text.delta", "delta": text},
            {"type": "response.completed", "response": resp}]


@pytest.mark.parametrize("first,sent,recv", [
    ({"input_tokens": 10, "output_tokens": 2}, 30, 5), (None, None, None)],
    ids=["known-known", "unknown-known"])
def test_oneshot_idle_retry_keeps_every_attempt(harness, engine, monkeypatch, tmp_path,
                                                first, sent, recv):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    idle = engine.StreamError("idle", "synthetic idle", 1, usage=first)
    harness.run("--oneshot", "-m", "gpt-6-sol", replies=[], stream_replies=[
        [idle], _done({"input_tokens": 20, "output_tokens": 3})])
    assert len(harness.stream_calls) == 2 and harness.payloads == []
    assert [r.get("sent") for r in _rows(harness, ".usage.jsonl")] == [
        first and 10, 20]
    assert (_end(tmp_path)["sent"], _end(tmp_path)["recv"]) == (sent, recv)


@pytest.mark.parametrize("idle_first", [False, True])
def test_oneshot_fallback_keeps_unknown_stream_attempt(harness, engine, monkeypatch, tmp_path,
                                                       idle_first):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    scripts = [_done(None, "")]
    if idle_first:
        scripts.insert(0, [engine.StreamError("idle", "synthetic idle", 1)])
    harness.run("--oneshot", "-m", "gpt-6-sol", replies=[_answer(SOME)],
                stream_replies=scripts)
    assert len(harness.payloads) == 1
    rows = [r.get("sent") for r in _rows(harness, ".usage.jsonl")]
    assert rows == [None] * len(scripts) + [100]
    assert _end(tmp_path)["sent"] is None


@pytest.mark.parametrize("final", ["list", "none"])
def test_runtime_wrapper_survives_malformed_final_response(harness, engine, monkeypatch,
                                                          tmp_path, final):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.delenv("_HARNESS_APPROVED_WORKER", raising=False)
    if final == "none":
        monkeypatch.setattr(engine, "_call_model_impl",
                            lambda *a, **k: (None, False, "empty", 0.0))
    try:
        harness.run("--context-policy", "model-v1", "-m", "gpt-6-sol", "--max-iters", "1",
                    replies=[["SYNTHETIC_RAW_BODY"]] * 4)
    except SystemExit:
        pass
    assert len(harness.payloads) == (4 if final == "list" else 0)
    end = _end(tmp_path)
    assert end["status"] == "failed" and end["sent"] is None
    assert len(_rows(harness, ".usage.jsonl")) == (4 if final == "list" else 1)


def _stub_impl(engine, monkeypatch, results):
    calls = []

    def impl(*a, **k):
        calls.append(1)
        result = results[len(calls) - 1]
        if isinstance(result, BaseException):
            raise result
        return result
    monkeypatch.setattr(engine, "_call_model_impl", impl)
    return calls


@pytest.mark.parametrize("first", [["SYNTHETIC_RAW_BODY"], None])
def test_declared_malformed_loop_then_measured_zero_wrapup(harness, engine, monkeypatch,
                                                           tmp_path, first):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.delenv("_HARNESS_APPROVED_WORKER", raising=False)
    zero = {"usage": {"input_tokens": 0, "output_tokens": 0},
            "choices": [{"message": {"content": "synthetic report"}}]}
    calls = _stub_impl(engine, monkeypatch, [(first, False, "empty", 0.0),
                                             (zero, True, None, 0.0)])
    try:
        harness.run("--context-policy", "model-v1", "-m", "gpt-6-sol", "--max-iters", "2",
                    replies=[])
    except SystemExit:
        pass
    assert len(calls) == 2 and harness.payloads == [] and harness.stream_calls == []
    rows = _rows(harness, ".usage.jsonl")
    assert [(r["phase"], r["sent"], r["recv"]) for r in rows] == [
        ("loop", None, None), ("wrapup", 0, 0)]
    end = _end(tmp_path)
    assert end["sent"] is None and end["recv"] is None
    assert "SYNTHETIC_RAW_BODY" not in (tmp_path / "index.jsonl").read_text()


@pytest.mark.parametrize("wrap", [
    {"choices": [None], "usage": None},
    {"choices": "SYNTHETIC_RAW_BODY", "usage": None},
    {"choices": [{"message": None}], "usage": None},
    {"choices": [{"message": {"content": 42}}], "usage": None},
    {"choices": [{"message": {"content": {"synthetic": "SYNTHETIC_RAW_BODY"}}}], "usage": None},
])
def test_malformed_wrapup_dict_uses_local_fallback(harness, engine, monkeypatch, tmp_path,
                                                   capsys, wrap):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.delenv("_HARNESS_APPROVED_WORKER", raising=False)
    calls = _stub_impl(engine, monkeypatch, [(None, False, "empty", 0.0),
                                             (wrap, True, None, 0.0)])
    try:
        harness.run("--context-policy", "model-v1", "-m", "gpt-6-sol", "--max-iters", "2",
                    replies=[])
    except SystemExit:
        pass
    assert len(calls) == 2 and harness.payloads == [] and harness.stream_calls == []
    rows = _rows(harness, ".usage.jsonl")
    assert [(r["phase"], r["sent"], r["recv"]) for r in rows] == [
        ("loop", None, None), ("wrapup", None, None)]
    end = _end(tmp_path)
    assert end["status"] in ("failed", "incomplete")
    assert end["sent"] is None and end["recv"] is None
    out = capsys.readouterr().out + harness.transcript_path.read_text(encoding="utf-8")
    assert "LOCAL factual report" in out
    assert "SYNTHETIC_RAW_BODY" not in (tmp_path / "index.jsonl").read_text()


def test_wrapup_rejected_before_dispatch_adds_no_row(harness, engine, monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.delenv("_HARNESS_APPROVED_WORKER", raising=False)
    calls = _stub_impl(engine, monkeypatch, [(None, False, "empty", 0.0),
                                             engine.ContextBudgetError("synthetic")])
    try:
        harness.run("--context-policy", "model-v1", "-m", "gpt-6-sol", "--max-iters", "2",
                    replies=[])
    except SystemExit:
        pass
    assert len(calls) == 2 and harness.payloads == []
    assert [r["phase"] for r in _rows(harness, ".usage.jsonl")] == ["loop"]
    assert _end(tmp_path)["sent"] is None


def test_responses_rejection_then_chat_never_certifies_responses_compaction(
        harness, engine, monkeypatch, tmp_path):
    from test_context_auto import inject_counter
    from test_engine_turn_budget import GPT, _call
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.delenv("_HARNESS_APPROVED_WORKER", raising=False)
    inject_counter(engine, monkeypatch)
    cc = engine.capability_module("context_compaction")
    old_request, old_check = cc._request, cc.Compactor.__dict__.get("_check_reply")
    labels, commits = [], []
    summary = {"choices": [{"message": {"content": "pending"}, "finish_reason": "stop"}],
               "usage": {"prompt_tokens": 200, "completion_tokens": 20}}

    def request(*args):
        req = old_request(*args)
        summary["choices"][0]["message"]["content"] = json.dumps({
            "source_digest": req.source_digest, "notes": "synthetic notes"})
        return req

    rt_cls = engine.capability_module("context_runtime").ContextRuntime
    old_commit = rt_cls.commit_view

    def commit(self, *a, **k):
        commits.append(self.route)
        return old_commit(self, *a, **k)

    ce = engine.capability_module("context_engine")
    old_summary = ce.AutomaticCompaction.summary

    def summ(self, req):
        reply = old_summary(self, req)
        labels.append(reply.route if hasattr(reply, "route") else None)
        return reply

    monkeypatch.setattr(cc, "_request", request)
    monkeypatch.setattr(rt_cls, "commit_view", commit)
    monkeypatch.setattr(ce.AutomaticCompaction, "summary", summ)
    reject = engine.StreamError("http", "HTTP 400: synthetic rejection", 0)
    harness.run("-m", GPT, "-e", "max", "--auto-compact", "--context-window-tokens", "1000",
                "--context-reply-tokens", "100", "--context-safety-tokens", "50",
                replies=[_call("list_dir", {"path": "."}, cid="a"),
                         _call("list_dir", {"path": "."}, cid="b"), summary,
                         _call("finish", {"summary": "done"}),
                         _call("finish", {"summary": "done"})],
                stream_replies=[reject])
    assert "responses" not in labels       # actual identity is Chat or maintenance declined
    assert labels == [] and commits == []  # declined before the RPC: nothing to mislabel
    assert harness.stream_calls and harness.stream_calls[0][0] == "/openai/responses"
    for p in harness.payloads:             # tool-free maintenance keeps model/effort
        if "tools" not in p:
            assert p["model"] == GPT and p["reasoning"]["effort"] == "max"


def _end(tmp_path):
    rows = [json.loads(l) for l in (tmp_path / "index.jsonl").read_text().splitlines()]
    return [r for r in rows if r.get("event") == "end"][-1]


ZERO = {"prompt_tokens": 0, "completion_tokens": 0}
SOME = {"prompt_tokens": 100, "completion_tokens": 20}


def _answer(usage):
    r = {"choices": [{"message": {"content": "synthetic answer"}, "finish_reason": "stop"}]}
    return {**r, "usage": usage} if usage is not None else r


@pytest.mark.parametrize("usages,expect", [
    ([None], None), ([ZERO], 0), ([None, SOME], None), ([SOME, SOME], 200)],
    ids=["absent", "measured-zero", "mixed", "known"])
def test_oneshot_terminal_totals_keep_unknown_null(harness, tmp_path, usages, expect):
    replies = [_answer(u) if i == len(usages) - 1 else {**_answer(u), "choices": [
        {"message": {"content": ""}, "finish_reason": "stop"}]} for i, u in enumerate(usages)]
    harness.run("--oneshot", "--no-stream", "-m", "gpt-6-sol", replies=replies)
    assert _end(tmp_path)["sent"] == expect


@pytest.mark.parametrize("ending", ["success", "failure", "safety"])
@pytest.mark.parametrize("usages,expect", [
    ([None, None], None), ([ZERO, ZERO], 0), ([SOME, None], None), ([SOME, SOME], 200)],
    ids=["absent", "measured-zero", "mixed", "known"])
def test_agent_terminal_totals_keep_unknown_null(harness, engine, monkeypatch, tmp_path,
                                                 usages, expect, ending):
    from test_engine_reasoning import _finish_reply, _list_reply
    callbacks = []
    monkeypatch.setattr(engine.atexit, "register", callbacks.append)
    last = (_finish_reply("native", usages[1]) if ending == "success" else
            {"_fatal": True, "_status": 400, "_error_detail": "synthetic rejection"}
            if ending == "safety" else {"choices": [{"message": {"content": "partial"},
                                                     "finish_reason": "stop"}],
                                        **({"usage": usages[1]} if usages[1] else {})})
    replies = [_list_reply("native", usages[0]), last]
    if ending == "safety":
        replies[0] = _list_reply("native", usages[0])
        usages = usages[:1]
        expect = None if usages[0] is None else usages[0]["prompt_tokens"]
    argv = ("-m", "gpt-6-sol") + (("--max-iters", "2") if ending == "failure" else ())
    try:
        harness.run(*argv, replies=replies)
    except SystemExit:
        pass
    for callback in callbacks:
        callback()
    assert _end(tmp_path)["sent"] == expect


def test_write_keeps_newlines_verbatim_and_new_dirs(eng, tmp_path):
    run = _disk(eng, tmp_path)
    assert run.write_file("a.txt", "one\r\ntwo\n").startswith("wrote")
    assert (tmp_path / "a.txt").read_bytes() == b"one\r\ntwo\n"
    run.write_file("sub/b.txt", "z")
    assert (tmp_path / "sub" / "b.txt").read_bytes() == b"z"

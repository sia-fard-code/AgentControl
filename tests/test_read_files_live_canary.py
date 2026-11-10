import importlib.util
import json
import os
import sys
import urllib.request
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "read_files_live_canary", ROOT / "tools" / "agent-harness" / "read_files_live_canary.py")
canary = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(canary)


def ledger(tmp_path, **limits):
    path = tmp_path / "budget.jsonl"
    canary.BudgetLedger.init(path, **limits)
    return canary.BudgetLedger(path)


def code_of(fn, *args):
    with pytest.raises(canary.CanaryFailure) as exc:
        fn(*args)
    return exc.value.code


def test_brief_versions_semantically_complete_read_contract(tmp_path):
    files = {tmp_path / "fixture.txt": "synthetic-secret-not-in-brief"}
    text = canary._brief("Synthetic current rules", files, "batch")
    assert 'read_file({"path":"AGENTS.md"})' in text
    assert "full-file-v2" in text
    assert "start_line=1" in text and "complete file" in text
    assert "synthetic-secret-not-in-brief" not in text


def test_init_is_exclusive_and_bound_to_root_and_limits(tmp_path):
    book = ledger(tmp_path)
    header = json.loads((tmp_path / "budget.jsonl").read_text().splitlines()[0])
    assert header["root"] == str(ROOT) and header["max_posts"] == 30
    assert header["max_input"] == 650000 and header["budget_id"]
    assert code_of(canary.BudgetLedger.init, tmp_path / "budget.jsonl") == "budget_exists"
    assert book.state() == {"posts": 0, "projected": 0, "excess": 0, "charged": 0,
                            "reported_known": 0, "reported_missing": 0}


def test_post_and_input_boundaries_refuse_before_append(tmp_path):
    book = ledger(tmp_path, max_posts=2, max_input=100)
    assert book.reserve(60) == 1
    assert code_of(book.reserve, 41) == "budget_exhausted"
    assert book.reserve(40) == 2
    assert code_of(book.reserve, 0) == "budget_exhausted"
    assert book.state()["posts"] == 2 and book.state()["charged"] == 100


def test_lock_is_exclusive_never_stolen(tmp_path):
    book = ledger(tmp_path)
    lock = Path(str(tmp_path / "budget.jsonl") + ".lock")
    lock.write_text("")
    assert code_of(book.reserve, 1) == "budget_locked"
    assert lock.exists() and book.state()["posts"] == 0
    lock.unlink()
    assert book.reserve(1) == 1 and not lock.exists()


@pytest.mark.parametrize("tail", ["{\"kind\":\"reserve\"", "{\"kind\":\"other\"}\n", "[]\n",
                                  "{\"kind\":\"reserve\",\"id\":5,\"projection\":1}\n"])
def test_corrupt_torn_or_unknown_ledger_fails_closed(tmp_path, tail):
    book = ledger(tmp_path)
    with (tmp_path / "budget.jsonl").open("a") as f:
        f.write(tail)
    assert code_of(book.reserve, 1) == "budget_corrupt"


def test_missing_ledger_and_foreign_root_fail_closed(tmp_path):
    assert code_of(canary.BudgetLedger(tmp_path / "none.jsonl").reserve, 1) == "budget_missing"
    path = tmp_path / "budget.jsonl"
    canary.BudgetLedger.init(path)
    header = json.loads(path.read_text())
    path.write_text(json.dumps(dict(header, root="X")) + "\n")
    assert code_of(canary.BudgetLedger(path).reserve, 1) == "budget_corrupt"


def test_reconcile_monotonic_duplicates_and_overrun(tmp_path):
    book = ledger(tmp_path, max_input=100)
    first = book.reserve(30)
    book.reconcile(first, 20)       # lower than projection: no refund
    book.reconcile(first, 50)
    book.reconcile(first, 50)       # duplicate snapshot: no double count
    book.reconcile(first, 40)       # lower later snapshot never reduces
    assert book.state() == {"posts": 1, "projected": 30, "excess": 20, "charged": 50,
                            "reported_known": 1, "reported_missing": 0}
    second = book.reserve(10)
    assert book.state()["reported_missing"] == 1
    book.reconcile(second, 60)      # recorded, never raised into the engine's stream
    assert book.state()["charged"] == 110 and book.overrun()
    assert code_of(book.reserve, 0) == "budget_exhausted"
    assert code_of(book.reconcile, 99, 1) == "budget_corrupt"


def test_projection_is_conservative_and_requires_estimate():
    data = b'{"messages":[1,2]}'
    assert canary.projection(data, 3, 2) == len(data) + 2 * canary.FRAMING
    assert canary.projection(data, 10**6, 2) == 2 * 10**6
    for bad in (None, -1, True, 1.5):
        assert code_of(canary.projection, data, bad, 2) == "estimate_missing"
def test_concurrent_reservations_never_exceed_cap(tmp_path):
    import threading
    path = tmp_path / "budget.jsonl"
    canary.BudgetLedger.init(path, max_posts=5)
    outcomes = []

    def worker():
        try:
            outcomes.append(canary.BudgetLedger(path).reserve(1))
        except canary.CanaryFailure as exc:
            outcomes.append(exc.code)
    threads = [threading.Thread(target=worker) for _ in range(12)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    ids = sorted(o for o in outcomes if type(o) is int)
    assert ids == [1, 2, 3, 4, 5]
    assert set(outcomes) - set(ids) <= {"budget_exhausted", "budget_locked"}
    assert canary.BudgetLedger(path).state()["posts"] == 5


def temp_path(name):
    import tempfile
    import uuid
    return Path(tempfile.gettempdir()).resolve() / ("rfc-test-%s-%s" % (name, uuid.uuid4()))


def live_args(**over):
    args = {"--case": "claude-chat", "--mode": "batch", "--engine": "source",
            "--out": str(temp_path("out")), "--launch-id": "12345678-1234-5678-1234-567812345678",
            "--launch-receipt": str(temp_path("receipt")), "--budget": str(temp_path("budget"))}
    args.update(over)
    return ["--run-live"] + [x for k, v in args.items() if v is not None for x in (k, v)]


def test_default_and_help_are_offline(capsys, monkeypatch):
    monkeypatch.setattr(canary.urllib.request, "urlopen", lambda *a, **k: pytest.fail("network"))
    assert canary.main([]) == 1
    assert json.loads(capsys.readouterr().out) == {"ok": False, "error": "run_live_required"}
    with pytest.raises(SystemExit) as exc:
        canary.main(["--help"])
    assert exc.value.code == 0 and "--init-budget" in capsys.readouterr().out


@pytest.mark.parametrize("over,error", [
    ({"--case": "gpt-chat"}, "invalid_case"),
    ({"--case": "gpt-responses", "--mode": "baseline"}, "invalid_mode"),
    ({"--engine": "copy"}, "invalid_engine"),
    ({"--fixture-count": "1"}, "invalid_fixture_count"),
    ({"--fixture-count": "9"}, "invalid_fixture_count"),
    ({"--launch-id": "nope"}, "invalid_launch_id"),
    ({"--out": "relative"}, "invalid_out"),
    ({"--budget": None}, "invalid_budget"),
])
def test_malformed_args_refused_before_effects(over, error, capsys):
    argv = live_args(**over)
    assert canary.main(argv) == 1
    assert json.loads(capsys.readouterr().out)["error"] == error
    assert not Path(argv[argv.index("--out") + 1]).exists()


def test_budget_must_be_existing_temp_ledger(capsys):
    assert canary.main(live_args()) == 1   # fresh path: never implicitly created
    assert json.loads(capsys.readouterr().out)["error"] == "invalid_budget"


def test_init_budget_is_explicit_metadata_only(capsys):
    path = temp_path("init")
    try:
        assert canary.main(["--init-budget", "--budget", str(path)]) == 0
        out = json.loads(capsys.readouterr().out)
        assert out["ok"] and out["max_posts"] == 30 and out["max_input"] == 650000
        assert canary.main(["--init-budget", "--budget", str(path)]) == 1
        assert json.loads(capsys.readouterr().out)["error"] == "invalid_budget"
        assert len(path.read_text().splitlines()) == 1
    finally:
        path.unlink(missing_ok=True)
from types import SimpleNamespace

LID = "12345678-1234-5678-1234-567812345678"
AGENTS = (ROOT / "AGENTS.md").read_text(encoding="utf-8")


class FakeCE:
    def __init__(self, tokens=100):
        self.scope = SimpleNamespace(phase="worker", policy=SimpleNamespace(reply_tokens=32768), last_estimate=SimpleNamespace(
            tokens=tokens, measurable=tokens is not None, calibrated=False, over_limit=False))

    def current(self):
        return self.scope


def observer(tmp_path, mode="batch", tokens=100, **limits):
    files = {}
    for i in range(2):
        path = tmp_path / ("f%d.txt" % i)
        path.write_text("synthetic %d nonce-%d\n" % (i, i), encoding="utf-8")
        files[path] = path.read_text(encoding="utf-8")
    ns = SimpleNamespace(case="claude-chat", mode=mode, launch_id=LID)
    return canary.Observer(ns, None, FakeCE(tokens), files, ledger(tmp_path, **limits)), files


def batch_result(files, complete=True):
    return json.dumps({"results": [{"index": i, "status": "served", "content": text,
                                    "complete": complete} for i, text in enumerate(files.values())]})


def test_observer_accepts_agents_then_complete_batch(tmp_path):
    obs, files = observer(tmp_path)
    calls = []
    original = lambda runner, name, argv: calls.append(name) or (
        AGENTS if name == "read_file" else batch_result(files))
    obs.dispatch(original, None, "read_file", {"path": "AGENTS.md"})
    obs.observe_delivery({"messages": [{"role": "tool", "content": AGENTS}]})
    obs.dispatch(original, None, "read_files", {"requests": [{"path": str(p)} for p in files]})
    assert calls == ["read_file", "read_files"] and len(obs.reads) == 3
    assert [p["bytes"] for p in obs.read_proofs[1:]] == [len(t) for t in files.values()]


@pytest.mark.parametrize("mode,name,argv,code", [
    ("batch", "run_command", {"argv": ["x"]}, "tool_forbidden"),
    ("batch", "write_file", {"path": "x", "content": ""}, "tool_forbidden"),
    ("baseline", "read_files", {"requests": [{"path": "AGENTS.md"}]}, "tool_forbidden"),
    ("batch", "read_file", {"path": "f0.txt"}, "read_sequence"),
    ("batch", "read_file", {"path": "AGENTS.md", "x": 1}, "read_forbidden"),
])
def test_observer_denies_before_effects(tmp_path, mode, name, argv, code):
    obs, _ = observer(tmp_path, mode)
    original = lambda *a: pytest.fail("effect")
    assert code_of(obs.dispatch, original, None, name, argv) == code


@pytest.mark.parametrize("extra", [
    {"start_line": 2, "end_line": 200}, {"start_line": 1, "end_line": 1},
    {"start_line": True}, {"start_line": None}, {"start_line": 1.0},
    {"end_line": None}, {"end_line": 0}, {"end_line": -1}, {"glob": "*"},
])
def test_full_read_contract_refuses_partial_or_invalid_ranges_before_reader(tmp_path, extra):
    obs, _ = observer(tmp_path)
    assert code_of(obs.dispatch, lambda *a: pytest.fail("original reader effect"), None,
                   "read_file", {"path": "AGENTS.md", **extra}) == "read_forbidden"
    assert not obs.reads


@pytest.mark.parametrize("bad", ["header", "body"])
def test_full_read_contract_does_not_trust_header_or_truncated_body(tmp_path, bad):
    obs, _ = observer(tmp_path)
    count = len(AGENTS.splitlines())
    header = "(AGENTS.md lines 1-%d)\n" % (count + (bad == "header"))
    value = header + (AGENTS[:-1] if bad == "body" else AGENTS)
    assert code_of(obs.dispatch, lambda *a: value, None, "read_file",
                   {"path": "AGENTS.md", "start_line": 1, "end_line": 200}) == "read_incomplete"
    assert not obs.reads


def test_observer_rejects_truncated_or_reordered_batch(tmp_path):
    obs, files = observer(tmp_path)
    obs.dispatch(lambda *a: AGENTS, None, "read_file", {"path": "AGENTS.md"})
    obs.observe_delivery({"messages": [{"role": "tool", "content": AGENTS}]})
    reqs = [{"path": str(p)} for p in files]
    assert code_of(obs.dispatch, lambda *a: pytest.fail("effect"), None, "read_files",
                   {"requests": reqs[::-1]}) == "read_sequence"
    (tmp_path / "b").mkdir()
    obs2, files2 = observer(tmp_path / "b")
    obs2.dispatch(lambda *a: AGENTS, None, "read_file", {"path": "AGENTS.md"})
    obs2.observe_delivery({"messages": [{"role": "tool", "content": AGENTS}]})
    assert code_of(obs2.dispatch, lambda *a: batch_result(files2, False), None, "read_files",
                   {"requests": [{"path": str(p)} for p in files2]}) == "read_incomplete"


def wire(content="hi"):
    body = {"model": canary.MODELS["claude-chat"], "reasoning_effort": "medium",
            "max_completion_tokens": 32768, "tools": [{}],
            "messages": [{"role": "user", "content": content}]}
    return SimpleNamespace(full_url="https://gw.invalid/api/chat/completions",
                           data=json.dumps(body).encode())


def test_urlopen_reserves_durably_before_unchanged_forward(tmp_path):
    obs, _ = observer(tmp_path, tokens=5000)
    request, seen = wire(), []

    def original(req, *a, **k):
        seen.append((req, obs.book.state()["posts"], a, k))
        return "RESPONSE"
    response = obs.urlopen(original, request, timeout=7)
    assert seen == [(request, 1, (), {"timeout": 7})]
    assert response.inner == "RESPONSE" or response == "RESPONSE"
    assert obs.posts[0]["projection"] == 10000 and obs.posts[0]["reservation"] == 1


@pytest.mark.parametrize("tokens,limits,code", [
    (None, {}, "estimate_missing"), (100, {"max_posts": 0}, "budget_exhausted")])
def test_urlopen_refuses_without_network(tmp_path, tokens, limits, code):
    obs, _ = observer(tmp_path, tokens=tokens, **limits)
    assert code_of(obs.urlopen, lambda *a, **k: pytest.fail("network"), wire()) == code
    assert obs.book.state()["posts"] == 0
class FakeResponse:
    def __init__(self, lines=(), body=b""):
        self.lines, self.body, self.status, self.closed = list(lines), body, 200, False

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.closed = True
        return False

    def __iter__(self):
        for line in self.lines:
            if isinstance(line, Exception):
                raise line
            yield line

    def read(self, *a):
        return self.body


def sse(obj):
    return ("data: " + json.dumps(obj) + "\n").encode()


def test_stream_pass_through_timing_and_usage(tmp_path):
    obs, _ = observer(tmp_path, tokens=10)
    lines = [b": ping\n", sse({"choices": [{"delta": {"tool_calls": [{}]}}]}),
             sse({"choices": [{"delta": {"content": "x"}}]}),
             sse({"choices": [], "usage": {"prompt_tokens": 900}}),
             sse({"choices": [], "usage": {"prompt_tokens": 900}}), b"data: [DONE]\n"]
    inner = FakeResponse(lines)
    with obs.urlopen(lambda *a, **k: inner, wire()) as r:
        assert list(r) == lines and r.status == 200
    assert inner.closed
    post = obs.posts[0]
    assert post["usage_input"] == 900 and obs.book.state()["charged"] == 900
    assert 0 <= post["headers_s"] and 0 <= post["first_event_s"] <= post["first_text_s"]
    assert obs.book.state()["reported_known"] == 1


def _force_payload_parser_limit(monkeypatch, payload):
    original = canary.json.loads
    def loads(data, *args, **kwargs):
        if isinstance(data, (bytes, bytearray)) and data.strip() == payload:
            raise RecursionError("synthetic JSON decoder depth bound")
        return original(data, *args, **kwargs)
    monkeypatch.setattr(canary.json, "loads", loads)


def test_deep_json_sse_is_passed_through_without_parser_interference(tmp_path, monkeypatch):
    obs, _ = observer(tmp_path, tokens=10)
    body = b'{"x":' + b'[' * 2000 + b'0' + b']' * 2000 + b'}'
    lines = [b"data: " + body + b"\n"]
    inner = FakeResponse(lines)
    # Decoder stack bounds vary by Python build; fault only this payload, not ledger JSON.
    _force_payload_parser_limit(monkeypatch, body)
    with obs.urlopen(lambda *a, **k: inner, wire()) as response:
        assert list(response) == lines
    assert inner.closed and obs.error == "observation_parse"
    assert obs.posts[0]["outcome"] == "parse_error"
    assert obs.book.state()["reported_missing"] == 1


def test_exit_error_not_replaced_by_deep_json_observation(tmp_path, monkeypatch):
    obs, _ = observer(tmp_path, tokens=10)
    body = b'{"x":' + b'[' * 2000 + b'0' + b']' * 2000 + b'}'
    boom = ConnectionResetError("synthetic close failure")
    class BrokenExit(FakeResponse):
        def __exit__(self, *exc):
            self.closed = True
            raise boom
    inner = BrokenExit(body=body)
    _force_payload_parser_limit(monkeypatch, body)
    with pytest.raises(ConnectionResetError) as caught:
        with obs.urlopen(lambda *a, **k: inner, wire()) as response:
            assert response.read(len(body)) == body
    assert caught.value is boom and inner.closed
    assert obs.error == "observation_parse"
    assert obs.posts[0]["outcome"] == "network_error"
    assert obs.posts[0]["error_class"] == "ConnectionResetError"


@pytest.mark.parametrize("repeat_input", [True, False])
def test_cumulative_snapshots_refresh_output_without_recharging_input(tmp_path, repeat_input):
    obs, _ = observer(tmp_path, tokens=10)
    final = {"completion_tokens": 5}
    if repeat_input:
        final["prompt_tokens"] = 900
    lines = [sse({"usage": {"prompt_tokens": 900, "completion_tokens": 0}}),
             sse({"usage": final})]
    with obs.urlopen(lambda *a, **k: FakeResponse(lines), wire()) as response:
        assert list(response) == lines
    assert obs.posts[0]["usage_input"] == 900
    assert obs.posts[0]["usage_output"] == 5
    assert obs.book.state()["charged"] == 900
    assert obs.book.state()["reported_known"] == 1
    rows = [{"phase": "loop", "sent": 900, "recv": 5,
             "raw": {"prompt_tokens": 900, "completion_tokens": 5}}]
    assert canary._usage_coverage(rows, obs.posts) is None


def test_output_known_with_unknown_input_keeps_independent_counter(tmp_path):
    obs, _ = observer(tmp_path, tokens=10)
    lines = [sse({"usage": {"completion_tokens": 5}})]
    with obs.urlopen(lambda *a, **k: FakeResponse(lines), wire()) as response:
        assert list(response) == lines
    assert obs.posts[0]["usage_input"] is None
    assert obs.posts[0]["usage_output"] == 5
    assert obs.book.state()["reported_missing"] == 1


def test_budget_parser_failure_cannot_replace_original_stream_error(tmp_path, monkeypatch):
    obs, _ = observer(tmp_path, tokens=10)
    boom = ConnectionResetError("synthetic transport failure")
    def broken_reconcile(*args):
        raise RecursionError("synthetic ledger decoder bound")
    monkeypatch.setattr(obs.book, "reconcile", broken_reconcile)
    inner = FakeResponse([sse({"usage": {"prompt_tokens": 900}}), boom])
    with pytest.raises(ConnectionResetError) as caught:
        with obs.urlopen(lambda *a, **k: inner, wire()) as response:
            list(response)
    assert caught.value is boom and inner.closed
    assert obs.error == "budget_observation"
    assert obs.posts[0]["outcome"] == "network_error"


def test_stream_exception_and_no_text_delta_kept_honest(tmp_path):
    obs, _ = observer(tmp_path, tokens=10)
    boom = ConnectionResetError()
    inner = FakeResponse([sse({"choices": [{"delta": {"tool_calls": [{}]}}]}), boom])
    with pytest.raises(ConnectionResetError) as exc:
        with obs.urlopen(lambda *a, **k: inner, wire()) as r:
            list(r)
    assert exc.value is boom and inner.closed
    post = obs.posts[0]
    assert post["first_text_s"] is None and post["usage_input"] is None
    assert obs.book.state()["reported_missing"] == 1


def test_non_stream_body_unchanged_and_overrun_stops_next_post(tmp_path):
    obs, _ = observer(tmp_path, tokens=10, max_input=1000)
    body = json.dumps({"choices": [], "usage": {"prompt_tokens": 5000}}).encode()
    with obs.urlopen(lambda *a, **k: FakeResponse(body=body), wire()) as r:
        assert r.read() == body
    assert obs.posts[0]["first_event_s"] is None and obs.book.overrun()
    assert code_of(obs.urlopen, lambda *a, **k: pytest.fail("network"), wire()) == "budget_overrun"


def test_urlopen_error_keeps_reservation(tmp_path):
    obs, _ = observer(tmp_path)
    boom = TimeoutError()

    def original(*a, **k):
        raise boom
    with pytest.raises(TimeoutError):
        obs.urlopen(original, wire())
    assert obs.book.state()["posts"] == 1 and obs.posts[0]["headers_s"] is None
SECRET = "sk-SYNTHETIC-SECRET-0000"


def fake_engine(files, brief_seen, prove=True):
    """Synthetic engine: real dispatch/urlopen hook points, faked network and journals."""
    def flag(name):
        return sys.argv[sys.argv.index(name) + 1]

    class Runner:
        def dispatch(self, name, argv):
            if name == "read_file":
                return AGENTS
            return batch_result(files)

    def post(rows, reply):
        req = wire()
        body = json.loads(req.data)
        body["messages"] = rows
        req.data = json.dumps(body).encode()
        transcript = Path(flag("--transcript"))
        journal = Path(str(transcript.with_suffix("")) + ".messages.jsonl")
        journal.write_text("".join(json.dumps(r) + "\n" for r in rows))
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                for _ in r:
                    pass
        except TimeoutError:
            with urllib.request.urlopen(req, timeout=5) as r:
                list(r)
        return reply

    def main():
        brief_seen.append(list(sys.argv))
        transcript, pid = Path(flag("--transcript")), os.getpid()
        run_id, lid, stem = flag("--label"), flag("--launch-id"), transcript.with_suffix("")
        call = lambda i, n, a: {"role": "assistant", "tool_calls": [
            {"id": "c%d" % i, "function": {"name": n, "arguments": json.dumps(a)}}]}
        rows = [{"role": "user", "content": flag("-t")}]
        post(rows, None)
        rows += [call(1, "read_file", {"path": "AGENTS.md"}), {"role": "tool", "tool_call_id": "c1",
                 "content": Runner().dispatch("read_file", {"path": "AGENTS.md"})}]
        post(rows, None)
        reqs = {"requests": [{"path": str(p)} for p in files]}
        rows += [call(2, "read_files", reqs), {"role": "tool", "tool_call_id": "c2",
                 "content": Runner().dispatch("read_files", reqs)}]
        post(rows, None)
        nonces = " ".join(t.split()[-1] for t in files.values()) if prove else "none"
        rows += [call(3, "finish", {"summary": "nonces " + nonces}),
                 {"role": "tool", "tool_call_id": "c3", "content": "ok"}]
        write = lambda p, items: p.write_text("".join(json.dumps(i) + "\n" for i in items))
        messages, usage = str(stem) + ".messages.jsonl", str(stem) + ".usage.jsonl"
        write(Path(messages), rows)
        write(Path(usage), [
            {"phase": "worker", "sent": 900, "recv": 5, "cache_read": 0, "cache_write": None, "iter": i}
            for i in range(3)])
        index = transcript.parent / "index.jsonl"
        write(Path(flag("--launch-receipt")), [
            {"state": "starting", "engine_pid": pid, "launch_id": lid, "run_id": None},
            {"state": "ready", "engine_pid": pid, "launch_id": lid, "run_id": run_id,
             "transcript": str(transcript), "root": str(ROOT), "label": run_id,
             "index_paths": [str(index)]}])
        write(index, [{"event": "start", "run_id": run_id, "engine_pid": pid, "root": str(ROOT),
                       "model": canary.MODELS["claude-chat"], "effort": "medium",
                       "transcript": str(transcript), "messages": messages,
                       "usage_journal": usage},
                      {"event": "end", "run_id": run_id, "status": "complete", "refused": 0}])
        return 0
    return SimpleNamespace(Runner=Runner, main=main, capability_module=lambda name: FakeCE(100))


@pytest.mark.parametrize("prove,error", [(True, None), (False, "finish_proof")])
def test_execute_loaded_end_to_end_honest_and_private(tmp_path, monkeypatch, prove, error):
    files = {}
    for i in range(2):
        path = tmp_path / ("fixture-%d.txt" % i)
        path.write_text("%s synthetic %d\nnonce-%s\n" % (SECRET, i, "ab%d" % i), encoding="utf-8")
        files[path] = path.read_text(encoding="utf-8")
    out = tmp_path / "out"
    out.mkdir()
    receipt = tmp_path / "receipt.jsonl"
    ns = SimpleNamespace(case="claude-chat", mode="batch", engine="source", launch_id=LID,
                         launch_receipt=str(receipt), fixture_count=2)
    ids = canary.base._run_paths(ns, out)
    monkeypatch.setattr(canary.urllib.request, "urlopen", lambda req, *a, **k: FakeResponse(
        [sse({"choices": [], "usage": {"prompt_tokens": 900, "completion_tokens": 5}}), b"data: [DONE]\n"]))
    seen = []
    result, code = canary._execute_loaded(ns, ids, files, canary._brief(AGENTS, files, "batch"),
                                          fake_engine(files, seen, prove), ledger(tmp_path))
    argv = seen[0]
    assert argv[:9] == ["delegate.py", "-C", str(ROOT), "--profile", "resilient-v1",
                        "--context-policy", "model-v1", "--skill", "ponytail"]
    assert "--read-files" in argv and not [a for a in argv if a.startswith("--context-window")
                                           or a in ("--context-reply-tokens", "--no-stream",
                                                    "--context-safety-tokens", "--auto-compact")]
    assert all(str(p.name) in argv[-1] and "nonce-ab" not in argv[-1] for p in files)
    assert result["error"] == error and result["ok"] is (error is None) and code == (0 if prove else 1)
    assert result["delivered_count"] == 3 and len(result["posts"]) == 3
    assert result["budget"]["posts"] == 3 and result["budget"]["reported_known"] == 3
    text = json.dumps(result)
    assert SECRET not in text and "nonce-ab" not in text and "synthetic" not in text


def test_stream_error_without_context_manager_retains_attempt(tmp_path):
    obs, _ = observer(tmp_path)
    boom = ConnectionResetError("private")
    response = obs.urlopen(lambda *a: FakeResponse([boom]), wire())
    with pytest.raises(ConnectionResetError) as exc:
        list(response)
    assert exc.value is boom and obs.posts[0]["outcome"] == "network_error"
    assert obs.posts[0]["error_class"] == "ConnectionResetError"


def test_cumulative_usage_snapshots_use_max_and_reconcile_before_next_post(tmp_path):
    import io
    obs, _ = observer(tmp_path)
    body = b"".join(sse({"usage": {"prompt_tokens": n, "completion_tokens": 5}}) for n in (900, 1200, 1200, 1000))
    with obs.urlopen(lambda *a: io.BytesIO(body), wire()) as r:
        assert b"".join(iter(lambda: r.read(7), b"")) == body
    assert obs.posts[0]["usage_input"] == 1200
    assert obs.book.state()["charged"] == 1200
    assert obs.book.state()["reported_known"] == 1


def test_usage_phase_totals_allow_merged_rows_not_double_snapshots():
    posts = [{"phase": "worker", "route": "chat", "usage_input": 900, "usage_output": 5}] * 2
    merged = [{"phase": "loop", "sent": 1800, "recv": 10}]
    assert canary._usage_coverage(merged, posts) is None
    assert canary._usage_coverage(merged * 2, posts) == "usage_mismatch"
    native = [{"phase": "loop", "sent": 100, "recv": 5, "raw": {
        "input_tokens": 100, "cache_read_input_tokens": 600, "cache_creation_input_tokens": 200}}]
    assert canary._usage_coverage(native, posts[:1]) is None
    inclusive = [{"phase": "loop", "sent": 900, "recv": 5, "raw": {
        "prompt_tokens": 900, "prompt_tokens_details": {"cached_tokens": 600}}}]
    assert canary._usage_coverage(inclusive, posts[:1]) is None
    inclusive[0]["sent"] = 901
    assert canary._usage_coverage(inclusive, posts[:1]) == "usage_mismatch"


def test_missing_marked_journal_refuses_before_network(tmp_path):
    obs, _ = observer(tmp_path)
    obs.journal_path = tmp_path / "messages.jsonl"
    obs.journal_path.write_text("")
    obs.urlopen(lambda *a: FakeResponse(), wire())
    obs.journal_path.unlink()
    assert code_of(obs.urlopen, lambda *a: pytest.fail("network"), wire()) == "journal_missing"


def test_failed_budget_admission_cannot_be_erased_by_repair(tmp_path):
    obs, _ = observer(tmp_path)
    original = obs.book.path.read_text()
    obs.book.path.write_text("broken\n")
    assert code_of(obs.urlopen, lambda *a: pytest.fail("network"), wire()) == "budget_corrupt"
    obs.book.path.write_text(original)
    assert code_of(obs.urlopen, lambda *a: pytest.fail("network"), wire()) == "budget_corrupt"
    assert obs.book.state()["posts"] == 0


@pytest.mark.parametrize("range_form", ["whole", "over_cover", "start_default", "end_only"])
def test_actual_source_loader_offline_readonly_finish(tmp_path, monkeypatch, range_form):
    import io
    root, home = tmp_path / "project", tmp_path / "home"
    root.mkdir()
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("OPENWEBUI_URL", "https://synthetic.invalid")
    monkeypatch.setenv("OPENWEBUI_API_KEY", "synthetic-key")
    monkeypatch.chdir(root)
    monkeypatch.setattr(canary, "ROOT", root)
    monkeypatch.setattr(canary.base, "ROOT", root)
    (root / "AGENTS.md").write_text("".join("Synthetic\vread-only rule %d\n" % i for i in range(43)))
    skill = root / ".claude" / "skills" / "ponytail"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("---\nname: ponytail\ndescription: Synthetic offline skill\n---\nRead only.\n")
    (root / ".claude" / "agent-policy.json").write_text(json.dumps({"allowed_cmds": [["git", "status"]]}))
    files = {root / ("fixture-%d.txt" % i): "synthetic fixture\nsentinel nonce-%d\n" % i for i in range(2)}
    for path, text in files.items():
        path.write_text(text)
    before = {p: p.read_bytes() for p in [root / "AGENTS.md", *files]}
    out = tmp_path / "out"
    out.mkdir()
    monkeypatch.setenv("CLAUDE_AGENT_RUN_INDEX", str(out / "index.jsonl"))
    ns = SimpleNamespace(case="claude-chat", mode="batch", engine="source", launch_id=LID,
                         launch_receipt=str(tmp_path / "receipt.jsonl"))
    ids = canary.base._run_paths(ns, out)
    book = ledger(tmp_path)
    payloads = []
    with canary.base._loaded_engine(ROOT / "engine" / "delegate.py") as engine:
        monkeypatch.setattr(engine.atexit, "register", lambda *a, **k: None)
        monkeypatch.setattr(engine.signal, "signal", lambda *a, **k: None)
        ce = engine.capability_module("context_engine")
        def transport(req, *a, **k):
            assert req.data is not None, "unexpected offline GET"
            body = json.loads(req.data)
            payloads.append(body)
            assert ce.current().last_estimate.tokens is not None
            assert book.state()["posts"] == len(payloads)
            assert body["model"] == canary.MODELS["claude-chat"]
            assert body["reasoning_effort"] == "medium" and body["stream"] is True
            assert body["max_completion_tokens"] == ce.current().policy.reply_tokens == 32768
            tool_results = [r["content"] for r in body["messages"] if r.get("role") == "tool"]
            ranges = ({"start_line": 1, "end_line": 200} if range_form == "over_cover" else
                      {"start_line": 1} if range_form == "start_default" else
                      {"end_line": 200} if range_form == "end_only" else {})
            if not tool_results:
                name, args = "read_file", {"path": "AGENTS.md", **ranges}
            elif len(tool_results) == 1:
                # Paths come from the actual outgoing brief, never an independent fixture read.
                brief = next(r["content"] for r in body["messages"] if r.get("role") == "user" and "Fixture paths" in r.get("content", ""))
                paths = brief.split("Fixture paths (in order, relative to the root):\n")[1].split("\nThe complete CURRENT")[0].splitlines()
                name, args = "read_files", {"requests": [{"path": p, **ranges} for p in paths]}
            else:
                delivered = json.loads(tool_results[-1])["results"]
                assert all(r["complete"] and r["status"] == "served" for r in delivered)
                name, args = "finish", {"summary": " ".join(r["content"].split()[-1] for r in delivered)}
            call = {"index": 0, "id": "c%d" % len(payloads), "type": "function",
                    "function": {"name": name, "arguments": json.dumps(args)}}
            data = sse({"choices": [{"index": 0, "delta": {"tool_calls": [call]}, "finish_reason": None}]})
            data += sse({"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}],
                         "usage": {"prompt_tokens": 900, "completion_tokens": 5}}) + b"data: [DONE]\n\n"
            return io.BytesIO(data)
        monkeypatch.setattr(canary.urllib.request, "urlopen", transport)
        result, code = canary._execute_loaded(ns, ids, files, canary._brief((root / "AGENTS.md").read_text(), files, "batch"), engine, book)
    assert result["ok"], result
    assert code == 0 and result["normal_finish"] and result["paired"] and result["finish_proof"]
    assert result["delivered_count"] == 3 and len(payloads) == 3
    assert len(canary.base._jsonl(Path(ns.launch_receipt))) == 2
    assert result["usage"] and result["budget"]["reported_known"] == 3
    assert all(p.read_bytes() == data for p, data in before.items())
    assert all(str(home) not in str(p) for p in files)


@pytest.mark.parametrize("change", ["cap", "effort", "form", "route", "unmatched"])
def test_outgoing_contract_refuses_before_reservation_and_network(tmp_path, change):
    obs, _ = observer(tmp_path)
    req = wire()
    body = json.loads(req.data)
    if change == "cap":
        body["max_completion_tokens"] = 8192
    elif change == "effort":
        body["reasoning_effort"] = "high"
    elif change == "form":
        body["reasoning"] = {"effort": body.pop("reasoning_effort")}
    elif change == "route":
        obs.ns.case = "gpt-responses"
        body["model"] = canary.MODELS["gpt-responses"]
        body["reasoning"] = {"effort": "high"}
        body.pop("reasoning_effort")
    else:
        body["messages"].append({"role": "tool", "tool_call_id": "unmatched", "content": "x"})
    req.data = json.dumps(body).encode()
    assert code_of(obs.urlopen, lambda *a: pytest.fail("network"), req) == "wire_invalid"
    assert obs.book.state()["posts"] == 0


def test_outgoing_boundaries_require_append_only_journal(tmp_path):
    obs, _ = observer(tmp_path)
    obs.journal_path = tmp_path / "messages.jsonl"
    obs.journal_path.write_text('{"role":"user","content":"original"}\n')
    obs.urlopen(lambda *a: FakeResponse(), wire())
    obs.journal_path.write_text('{"role":"user","content":"replaced"}\n')
    assert code_of(obs.urlopen, lambda *a: pytest.fail("network"), wire()) == "journal_rewritten"
    assert obs.book.state()["posts"] == 1


def test_network_failure_recovery_retains_fixed_attempt_metadata(tmp_path):
    import io
    import urllib.error
    obs, _ = observer(tmp_path)
    boom = urllib.error.HTTPError("https://private.invalid/secret", 503, "private", {}, None)
    def fail(*a):
        raise boom
    with pytest.raises(urllib.error.HTTPError) as exc:
        obs.urlopen(fail, wire())
    assert exc.value is boom
    with obs.urlopen(lambda *a: io.BytesIO(sse({"usage": {"prompt_tokens": 900}})), wire()) as r:
        list(r)
    assert obs.book.state()["posts"] == 2 and obs.book.state()["reported_missing"] == 1
    assert [p["outcome"] for p in obs.posts] == ["network_error", "complete"]
    assert obs.posts[0]["error_class"] == "HTTPError" and obs.posts[0]["http_status"] == 503
    assert obs.posts[1]["error_class"] is None
    assert "private" not in json.dumps(obs.posts)


@pytest.mark.parametrize("mutation,error", [("empty_usage", "usage_unknown"),
    ("usage_mismatch", "usage_mismatch"), ("false_terminal", "terminal_failed"),
    ("rewrite", "journal_rewritten"), ("network", "network_anomaly"),
    ("observer_exit", "read_forbidden")])
def test_collection_rejects_false_success_and_keeps_primary_error(tmp_path, monkeypatch, mutation, error):
    files = {tmp_path / "fixture.txt": "synthetic sentinel nonce-xyz\n"}
    next(iter(files)).write_text(next(iter(files.values())))
    out = tmp_path / "out"
    out.mkdir()
    ns = SimpleNamespace(case="claude-chat", mode="batch", engine="source", launch_id=LID,
                         launch_receipt=str(tmp_path / "receipt.jsonl"))
    ids = canary.base._run_paths(ns, out)
    engine = fake_engine(files, [])
    original = engine.main
    def main():
        code = original()
        if mutation == "empty_usage":
            ids["usage"].write_text("")
        elif mutation == "usage_mismatch":
            rows = canary.base._jsonl(ids["usage"])
            rows[0]["sent"] += 1
            ids["usage"].write_text("".join(json.dumps(r) + "\n" for r in rows))
        elif mutation == "false_terminal":
            text = ids["index"].read_text().replace('"status": "complete"', '"status": "complete", "stop_reason": "max_iters"')
            ids["index"].write_text(text)
        elif mutation == "rewrite":
            ids["messages"].write_text(ids["messages"].read_text().replace("AGENTS.md", "REPLACED.md"))
        elif mutation == "observer_exit":
            try:
                engine.Runner().dispatch("read_file", {"path": "forbidden", "extra": 1})
            except canary.CanaryFailure:
                return 9
        return code
    engine.main = main
    attempts = []
    def transport(*a, **k):
        attempts.append(1)
        if mutation == "network" and len(attempts) == 1:
            raise TimeoutError("private")
        return FakeResponse([sse({"usage": {"prompt_tokens": 900, "completion_tokens": 5}})])
    monkeypatch.setattr(canary.urllib.request, "urlopen", transport)
    result, code = canary._execute_loaded(ns, ids, files, canary._brief(AGENTS, files, "batch"), engine, ledger(tmp_path))
    assert not result["ok"] and result["error"] == error and code != 0
    if mutation == "observer_exit":
        assert result["observer_error"] == error and result["engine_exit"] == 9


@pytest.mark.parametrize("consume", ["iter", "readline", "chunks", "read"])
@pytest.mark.parametrize("stream", [True, False])
def test_consumption_preserves_bytes_and_observes_usage(tmp_path, consume, stream):
    import io
    obs, _ = observer(tmp_path, tokens=10)
    obj = {"choices": [], "usage": {"prompt_tokens": 900}}
    body = (sse(obj) + sse(obj) + b"data: [DONE]\n") if stream else json.dumps(obj).encode()
    inner = io.BytesIO(body)
    with obs.urlopen(lambda *a: inner, wire()) as response:
        if consume == "iter":
            actual = b"".join(response)
        elif consume == "readline":
            actual = b"".join(iter(response.readline, b""))
        elif consume == "chunks":
            actual = b"".join(iter(lambda: response.read(7), b""))
        else:
            actual = response.read()
        assert actual == body
    assert inner.closed and obs.posts[0]["usage_input"] == 900
    assert obs.book.state()["reported_known"] == 1
    assert obs.posts[0]["first_text_s"] is None
    if not stream:
        assert obs.posts[0]["first_event_s"] is None


@pytest.mark.parametrize("body", [b'data: {"choices":1}\n', b'data: broken\n',
                                      b'data: ' + b'x' * (2**20) + b'\n', b'{broken'],
                         ids=["bad-shape", "bad-sse", "oversize", "bad-json"])
def test_malformed_observation_never_changes_bytes_and_blocks_next_post(tmp_path, body):
    import io
    obs, _ = observer(tmp_path)
    with obs.urlopen(lambda *a: io.BytesIO(body), wire()) as response:
        assert b"".join(response) == body
    assert obs.error == "observation_parse"
    assert code_of(obs.urlopen, lambda *a: pytest.fail("network"), wire()) == "observation_parse"
    assert obs.posts[0]["usage_input"] is None


def test_reconciliation_oserror_cannot_replace_original_stream_error(tmp_path, monkeypatch):
    obs, _ = observer(tmp_path)
    boom = ConnectionResetError("private transport detail")
    def bad_reconcile(*a):
        raise OSError("private accounting detail")
    monkeypatch.setattr(obs.book, "reconcile", bad_reconcile)
    inner = FakeResponse([sse({"usage": {"prompt_tokens": 900}}), boom])
    with pytest.raises(ConnectionResetError) as exc:
        with obs.urlopen(lambda *a: inner, wire()) as response:
            list(response)
    assert exc.value is boom and inner.closed
    assert obs.error == "budget_io"
    assert code_of(obs.urlopen, lambda *a: pytest.fail("network"), wire()) == "budget_io"
    assert "private" not in json.dumps(obs.posts)


@pytest.mark.parametrize("change", [
    {"max_posts": 31}, {"max_input": 650001}, {"budget_id": "not-a-uuid"},
    {"budget_id": "12345678123456781234567812345678"}, {"root": "foreign"}])
def test_loaded_budget_header_refuses_before_network(tmp_path, change):
    obs, _ = observer(tmp_path)
    header = json.loads(obs.book.path.read_text())
    obs.book.path.write_text(json.dumps(dict(header, **change)) + "\n")
    assert code_of(obs.urlopen, lambda *a: pytest.fail("network"), wire()) == "budget_corrupt"


@pytest.mark.parametrize("tail", [
    '{"kind":"reserve","id":true,"projection":1}\n',
    '{"kind":"reserve","id":1,"id":1,"projection":1}\n',
    '{"kind":"usage","id":1,"input":1}\n',
    '{"kind":"reserve","id":1,"projection":1}\n{"kind":"usage","id":true,"input":1}\n',
    '{"kind":"reserve","id":1,"projection":1}\n{"kind":"usage","id":1,"input":2}\n{"kind":"usage","id":1,"input":1}\n'])
def test_ledger_ambiguous_identities_refuse_network(tmp_path, tail):
    obs, _ = observer(tmp_path)
    with obs.book.path.open("a") as f:
        f.write(tail)
    assert code_of(obs.urlopen, lambda *a: pytest.fail("network"), wire()) == "budget_corrupt"


def test_ledger_duplicate_header_key_refuses_network(tmp_path):
    obs, _ = observer(tmp_path)
    text = obs.book.path.read_text().replace('"max_posts": 30', '"max_posts": 30, "max_posts": 30')
    obs.book.path.write_text(text)
    assert code_of(obs.urlopen, lambda *a: pytest.fail("network"), wire()) == "budget_corrupt"


@pytest.mark.parametrize("mode", ["baseline", "batch"])
def test_agents_execution_is_not_delivery_before_fixture_effects(tmp_path, mode):
    obs, files = observer(tmp_path, mode)
    obs.dispatch(lambda *a: AGENTS, None, "read_file", {"path": "AGENTS.md"})
    name, args = ("read_file", {"path": str(next(iter(files)))}) if mode == "baseline" else (
        "read_files", {"requests": [{"path": str(p)} for p in files]})
    assert code_of(obs.dispatch, lambda *a: pytest.fail("fixture effect"), None, name, args) == "agents_not_delivered"
    obs.observe_delivery({"messages": [{"role": "tool", "content": AGENTS}]})
    value = next(iter(files.values())) if mode == "baseline" else batch_result(files)
    assert obs.dispatch(lambda *a: value, None, name, args) == value

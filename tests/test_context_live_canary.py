"""Offline tests for tools/agent-harness/context_live_canary.py (no live calls)."""
import importlib.util, json, subprocess, sys, tempfile, uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "tools" / "agent-harness" / "context_live_canary.py"


def load():
    spec = importlib.util.spec_from_file_location("context_live_canary", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_help_and_no_opt_in_do_nothing(tmp_path):
    r = subprocess.run([sys.executable, str(SCRIPT), "--help"], capture_output=True, text=True)
    assert r.returncode == 0
    r = subprocess.run([sys.executable, str(SCRIPT)], capture_output=True, text=True)
    assert r.returncode != 0 and json.loads(r.stdout)["error"] == "run_live_required"


def test_rejects_bad_case_engine_uuid_and_non_temp_out(tmp_path):
    m = load()
    tmp = Path(tempfile.gettempdir())
    ok = ["--run-live", "--case", "gpt-chat", "--engine", "source",
          "--out", str(tmp / f"canary-{uuid.uuid4()}"), "--launch-id", str(uuid.uuid4()),
          "--launch-receipt", str(tmp / f"receipt-{uuid.uuid4()}.json")]
    assert m.validate(m.parse(ok)) is None
    for i, bad in ((2, "other"), (4, "remote"), (8, "not-a-uuid"), (6, str(ROOT / "x"))):
        argv = list(ok); argv[i] = bad
        assert m.validate(m.parse(argv)) is not None


def test_wire_metadata_redacts_text_and_checks_pairing():
    m = load()
    body = {"model": "m", "max_completion_tokens": 8192, "tools": [{}], "messages": [
        {"role": "user", "content": "SECRET-MARKER"},
        {"role": "assistant", "tool_calls": [{"id": "a", "function": {"name": "read_file"}}]},
        {"role": "tool", "tool_call_id": "a", "content": "SECRET-MARKER"}]}
    meta = m.wire_metadata("/v1/chat/completions", json.dumps(body).encode())
    assert meta["cap_field"] == "max_completion_tokens" and meta["paired"] is True
    assert "SECRET-MARKER" not in json.dumps(meta)
    body["messages"].pop()
    assert m.wire_metadata("/v1/chat/completions", json.dumps(body).encode())["paired"] is False


def test_responses_pairing_and_reused_closed_ids():
    m = load()
    rows = [{"type": "function_call", "call_id": "a", "name": "read_file", "arguments": "{}"},
            {"type": "function_call_output", "call_id": "a", "output": "SECRET-MARKER"}]
    body = {"model": "gpt-6.1-sol", "max_output_tokens": 8192,
            "reasoning": {"effort": "high"}, "input": rows + rows}
    meta = m.wire_metadata("SECRET-HOST", json.dumps(body).encode())
    assert meta["paired"] and meta["route"] == "responses" and meta["effort"] == "high"
    assert "SECRET" not in json.dumps(meta)
    for broken in (rows[:1], rows[1:], [rows[0], rows[0], rows[1]]):
        body["input"] = broken
        assert not m.wire_metadata(None, json.dumps(body).encode())["paired"]
    body["input"] = [rows[0], {**rows[0], "call_id": "b"}, rows[1], {**rows[1], "call_id": "b"}]
    multi = m.wire_metadata(None, json.dumps(body).encode())
    assert multi["paired"] and not multi["single_tool_batches"]
    chat = {"messages": [{"role": "assistant", "tool_calls": [{"id": "a"}]},
                         {"role": "tool", "tool_call_id": "a"}] * 2}
    assert m.wire_metadata(None, json.dumps(chat).encode())["paired"]


def good_state(case="gpt-responses"):
    return {"case": case, "engine_exit": 0, "identities": True, "normal_finish": True,
            "ends": [{"status": "complete", "refused": 0, "sent": 300, "recv": 30}],
            "reads": ["AGENTS.md", "fixture-1", "fixture-2"],
            "delivered_reads": ["AGENTS.md", "fixture-1", "fixture-2"],
            "expected_reads": ["AGENTS.md", "fixture-1", "fixture-2"],
            "usage": [{"phase": "worker", "sent": 100, "recv": 10},
                      {"phase": "compaction", "sent": 200, "recv": 20}],
            "wires": [{"checked": True, "phase": "worker", "after_commit": False, "memory_present": False},
                      {"checked": True, "phase": "compaction", "after_commit": True, "memory_present": True}],
            "compactions": [{"committed": True, "verified": True}],
            "new_final_read_after_commit": True}


def test_strict_final_gates_do_not_confuse_artifacts_with_commit():
    import copy
    m = load()
    assert m.final_gate(good_state()) is None
    state = good_state(); state["usage"] = state["usage"][1:]
    assert m.final_gate(state) == "usage_unknown"
    state = good_state(); state["normal_finish"] = False
    assert m.final_gate(state) == "finish_missing"
    state = good_state(); state["observer_error"] = "tool_forbidden"
    assert m.final_gate(state) == "tool_forbidden"
    changes = [({"engine_exit": 9}, "engine_failed"),
               ({"ends": []}, "terminal_identity"),
               ({"identities": False}, "terminal_identity"),
               ({"ends": [{"status": "failed", "refused": 0}]}, "terminal_failed"),
               ({"compactions": [], "ctxjson_exists": True}, "compaction_not_committed"),
               ({"compactions": [{"committed": False, "verified": True}]}, "compaction_not_committed"),
               ({"new_final_read_after_commit": False}, "postcommit_read_missing"),
               ({"usage": [{"phase": "compaction", "sent": None, "recv": 1}]}, "usage_unknown"),
               ({"usage": [{"phase": "worker", "sent": 1, "recv": 1}]}, "compaction_usage_unknown"),
               ({"reads": ["AGENTS.md", "fixture-1", "fixture-1"]}, "read_sequence"),
               ({"wires": [{"checked": False}]}, "wire_invalid"),
               ({"wires": [{"checked": True, "after_commit": True, "memory_present": False}]}, "postcommit_memory_missing")]
    for updates, expected in changes:
        state = copy.deepcopy(good_state()); state.update(updates)
        assert m.final_gate(state) == expected
    state = good_state(); state["ends"] *= 2
    assert m.final_gate(state) == "terminal_identity"
    for case in ("claude-chat", "gpt-chat"):
        state = good_state(case)
        assert m.final_gate(state) == "unexpected_compaction"
        state["compactions"] = []
        state["usage"] = state["usage"][:1]
        state["wires"] = state["wires"][:1]
        state["ends"][0].update(sent=100, recv=10)
        assert m.final_gate(state) is None


def test_usage_preserves_unknown_and_measured_zero():
    m = load()
    meta = m.usage_metadata([{"phase": "worker", "sent": 0, "recv": 0, "raw": {"secret": "SECRET"}},
                             {"phase": "compaction", "sent": None, "recv": None, "raw": {}}])
    assert meta[0]["sent"] == 0 and meta[0]["recv"] == 0
    assert meta[1]["sent"] is None and meta[1]["recv"] is None
    assert "SECRET" not in json.dumps(meta)


def test_snapshot_uses_runtime_dict_rows_and_preserves_bindings():
    from types import SimpleNamespace
    m = load()
    rows = [{"id": 1, "msg": {"role": "system", "content": "synthetic"}, "pinned": True},
            {"id": 2, "msg": {"role": "tool", "content": "synthetic"}, "pinned": False}]
    rt = SimpleNamespace(_rows=rows, _groups=[{"kind": "native", "ids": [2], "refs": []}])
    snap = m._snapshot(rt)
    assert m._bindings(snap, snap) == (True, True)
    rows[0]["msg"]["content"] = "changed"
    assert m._bindings(snap, m._snapshot(rt)) == (False, True)


def test_invalid_invocations_never_load_engine_or_write(tmp_path, monkeypatch, capsys):
    m = load()
    def forbidden(*args, **kwargs):
        raise AssertionError("unexpected engine/config/network/write")
    monkeypatch.setattr(m, "_execute", forbidden)
    monkeypatch.setattr(m.urllib.request, "urlopen", forbidden)
    monkeypatch.setattr(m.Path, "mkdir", forbidden)
    monkeypatch.setattr(m.Path, "open", forbidden)
    assert m.main([]) == 1
    assert json.loads(capsys.readouterr().out)["error"] == "run_live_required"
    assert m.main(["--bad-SECRET"]) == 1
    text = capsys.readouterr().out
    assert json.loads(text)["error"] == "invalid_arguments" and "SECRET" not in text


def test_geometry_is_measured_before_network(tmp_path, monkeypatch):
    from types import SimpleNamespace
    m = load()
    monkeypatch.setattr(m, "ROOT", tmp_path)
    (tmp_path / "AGENTS.md").write_text("synthetic instructions", encoding="utf-8")
    paths = []
    for i in range(6):
        path = tmp_path / (str(i) + ".txt")
        path.write_text("0" * 10000, encoding="utf-8"); paths.append(path)
    class Meter:
        def estimate(self, payload):
            return SimpleNamespace(tokens=len(json.dumps(payload)) * 2 // 3, measurable=True)
    scope = SimpleNamespace(meter=Meter(), phase="worker", last_estimate=None)
    ns = SimpleNamespace(case="gpt-responses")
    ce = SimpleNamespace(current=lambda: scope, current_runtime=lambda: None)
    o = m.Observer(ns, None, ce, paths)
    body = {"model": "gpt-6.1-sol", "input": [{"role": "user", "content": "synthetic"}],
            "max_output_tokens": 8192, "reasoning": {"effort": "high"}}
    o.geometry_check(body)
    assert o.geometry["safe"] and o.geometry["crossing_fixture"] < 6
    assert o.geometry["retained_bound"] < 26214
    scope.meter = SimpleNamespace(estimate=lambda payload: SimpleNamespace(tokens=None))
    try:
        o.geometry_check(body)
    except m.CanaryFailure as exc:
        assert exc.code == "canary_geometry"
    else:
        assert False, "unknown geometry must fail before sending"
    o = m.Observer(ns, None, ce, paths)
    sent = []
    request = SimpleNamespace(full_url="https://h/openai/responses", data=json.dumps(body).encode())
    try:
        o.urlopen(lambda *a, **k: sent.append(True), request)
    except m.CanaryFailure as exc:
        assert exc.code == "canary_geometry"
    assert not sent and o.error == "canary_geometry"


def test_narrow_guard_and_duplicates_are_sticky(tmp_path, monkeypatch):
    from types import SimpleNamespace
    m = load(); monkeypatch.setattr(m, "ROOT", tmp_path)
    agents, fixture = tmp_path / "AGENTS.md", tmp_path / "fixture.txt"
    agents.write_text("synthetic", encoding="utf-8")
    fixture.write_text("synthetic", encoding="utf-8")
    calls = []
    store = load_engine().capability_module("context_receipts").ReceiptStore()
    own = store.reserve_batch(["synthetic-slot"])[0]
    store.record(own.ref, "synthetic data", outcome="returned", exit_code=None)
    runner = SimpleNamespace(context_runtime=SimpleNamespace(group_refs=lambda: [[own.ref]], _store=store))
    def original(*args):
        calls.append(True)
        return Path(args[-1]["path"]).read_text(encoding="utf-8") if args[-2] == "read_file" else "synthetic page"
    o = m.Observer(SimpleNamespace(case="gpt-chat"), None, None, [fixture])
    o.dispatch(original, runner, "read_file", {"path": str(agents)})
    o.dispatch(original, runner, "read_context_result", {"ref": own.ref})
    for name, args in [("read_file", {"path": str(agents)}), ("run_command", {}),
                       ("read_file", {"path": str(tmp_path / "other")}),
                       ("read_context_result", {"ref": "other-run"})]:
        before = len(calls)
        try:
            o.dispatch(original, runner, name, args)
        except m.CanaryFailure:
            pass
        else:
            assert False, "forbidden execution"
        assert len(calls) == before and o.error


def test_real_attempt_result_and_retention_gate(tmp_path, monkeypatch):
    from types import SimpleNamespace
    m = load(); monkeypatch.setattr(m, "ROOT", tmp_path)
    rows = [{"id": 1, "msg": {"role": "system", "content": "synthetic"}, "pinned": True},
            {"id": 2, "msg": {"role": "tool", "content": "old synthetic"}, "pinned": False},
            {"id": 3, "msg": {"role": "tool", "content": "new synthetic"}, "pinned": False}]
    rt = SimpleNamespace(_rows=rows, _groups=[{"kind": "native", "ids": [2], "refs": []},
                                             {"kind": "native", "ids": [3], "refs": []}], count=0)
    rt.metadata = lambda: {"compactions": rt.count}
    result = SimpleNamespace(code="committed", committed=True, before_tokens=34000,
                             after_tokens=20000, removed_groups=1, calls=1,
                             artifact_ref="artifact://synthetic/checkpoint.ctxjson")
    def attempt(adapter):
        rt._rows = [rows[0], {"id": 4, "msg": {"role": "assistant", "content": "PRIVATE-SUMMARY"},
                                "pinned": False}, rows[2]]
        rt._groups = [{"kind": "memory", "ids": [4], "refs": []},
                      {"kind": "native", "ids": [3], "refs": []}]
        rt.count += 1
        return result
    o = m.Observer(SimpleNamespace(case="gpt-responses"), None, None, [])
    assert o.attempt(attempt, SimpleNamespace(rt=rt)) is result
    assert o.committed and o.compactions[0]["verified"]
    assert o.compactions[0]["checkpoint_ref"] == "artifact://synthetic/checkpoint.ctxjson"
    assert "PRIVATE-SUMMARY" not in json.dumps(o.compactions)
    o = m.Observer(SimpleNamespace(case="gpt-responses"), None, None, [])
    result.after_tokens = 34000
    try:
        o.attempt(attempt, SimpleNamespace(rt=rt))
    except m.CanaryFailure as exc:
        assert exc.code == "compaction_invalid"
    else:
        assert False, "non-reducing persisted result is not a commit"


def test_run_paths_do_not_invent_a_context_checkpoint_file(tmp_path):
    from types import SimpleNamespace
    m = load()
    ns = SimpleNamespace(case="gpt-responses", launch_id=str(uuid.uuid4()),
                         launch_receipt=str(tmp_path / "receipt.jsonl"))
    assert "context" not in m._run_paths(ns, tmp_path)


def test_own_frozen_indices_and_compacted_receipts_remain_readable(tmp_path):
    from types import SimpleNamespace
    m = load()
    receipts = load_engine().capability_module("context_receipts")
    store = receipts.ReceiptStore()
    first = store.reserve_batch(["old-slot"])[0]
    store.record(first.ref, "synthetic old data", outcome="returned", exit_code=None)
    frozen = store.index_ref()
    newest = store.reserve_batch(["new-slot"])[0]
    store.record(newest.ref, "synthetic new data", outcome="returned", exit_code=None)
    rt = SimpleNamespace(group_refs=lambda: [[newest.ref]], _store=store)
    calls = []
    def original(runner, name, args):
        calls.append(args["ref"])
        return store.read(args["ref"], args.get("offset", 0), args.get("limit", 4096))
    o = m.Observer(SimpleNamespace(case="gpt-responses"), None, None, [])
    o.rt = rt
    runner = SimpleNamespace(context_runtime=rt)
    page = o.dispatch(original, runner, "read_context_result", {"ref": frozen, "offset": 0, "limit": 4096})
    assert page["kind"] == "reference_index" and page["high_water"] == 1
    assert calls == [frozen]
    data = o.dispatch(original, runner, "read_context_result", {"ref": first.ref})
    assert data["data"] == "synthetic old data" and calls == [frozen, first.ref]
    for ref in (store.index_ref().rsplit("/", 1)[0] + "/9999", "result://foreign/index/1",
                "result://foreign/1", "file:///private"):
        denied = m.Observer(SimpleNamespace(case="gpt-responses"), None, None, [])
        denied.rt = rt
        expect("context_result_unverified", denied.dispatch, original, runner, "read_context_result", {"ref": ref})
    assert calls == [frozen, first.ref]


def load_engine():
    spec = importlib.util.spec_from_file_location("_canary_test_engine", ROOT / "engine" / "delegate.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def make_ns(tmp, case="gpt-chat"):
    from types import SimpleNamespace
    return SimpleNamespace(case=case, engine="source", launch_id=str(uuid.uuid4()),
                           launch_receipt=str(tmp / "receipt.jsonl"))


def test_engine_argv_task_flag_unique_identity_and_cold_cli(tmp_path):
    import os
    m, eng = load(), load_engine()
    for case in m.MODELS:
        ns = make_ns(tmp_path, case)
        ids = m._run_paths(ns, tmp_path)
        t = ids["transcript"]
        assert t.suffix == ".md" and case in t.name and ns.launch_id in t.name
        assert ids["run_id"] == eng.run_id_for(str(t)) == t.name[:-3]
        assert ids["messages"].name == ids["run_id"] + ".messages.jsonl"
        assert ids["usage"].name == ids["run_id"] + ".usage.jsonl"
        argv = m._engine_argv(ns, ids, "synthetic brief")
        assert argv[argv.index("-t") + 1] == "synthetic brief" and argv.count("synthetic brief") == 1
        assert argv[argv.index("--label") + 1] == ids["run_id"]
        index = tmp_path / ("index-" + case + ".jsonl")
        env = dict(os.environ, CLAUDE_AGENT_RUN_INDEX=str(index))
        r = subprocess.run([sys.executable, str(ROOT / "engine" / "delegate.py")] + argv[1:] +
                           ["--show-profile"], capture_output=True, text=True, env=env, timeout=120)
        assert r.returncode == 0, case
        assert not Path(ns.launch_receipt).exists() and not index.exists() and not t.exists()


def identity_fixture(tmp_path, case="gpt-chat"):
    m, eng = load(), load_engine()
    ns = make_ns(tmp_path, case)
    ids = m._run_paths(ns, tmp_path)
    pid, model = 4242, m.MODELS[case]
    effort = "medium" if case == "claude-chat" else "high"
    start = {"schema": 1, "state": "starting", "engine_pid": pid, "launch_id": ns.launch_id,
             "run_id": None, "timestamp": "t"}
    ready = {"schema": 1, "state": "ready", "engine_pid": pid, "launch_id": ns.launch_id,
             "run_id": ids["run_id"], "root": str(m.ROOT), "label": ids["run_id"],
             "transcript": str(ids["transcript"]), "index_paths": [str(ids["index"])], "timestamp": "t"}
    s = eng.index_record("start", run_id=ids["run_id"], model=model, effort=effort, root=str(m.ROOT),
                         engine_pid=pid, transcript=str(ids["transcript"]),
                         messages=str(ids["messages"]), usage_journal=str(ids["usage"]))
    e = eng.index_record("end", run_id=ids["run_id"], tool_calls=2, refused=0, sent=1, recv=1,
                         status="complete")
    return m, ns, ids, pid, [start, ready], [s, e]


def test_identity_join_uses_real_schema_and_rejects_foreign_or_duplicate(tmp_path):
    import copy
    m, ns, ids, pid, receipt, index = identity_fixture(tmp_path)
    assert m.identity_check(ns, ids, pid, receipt, index)["status"] == "complete"
    foreign = dict(index[0], run_id="other")
    bad = [(receipt[:1], index), (receipt + receipt[1:], index), (receipt[::-1], index),
           ([receipt[0], dict(receipt[1], engine_pid=1)], index),
           ([receipt[0], dict(receipt[1], launch_id=str(uuid.uuid4()))], index),
           ([receipt[0], dict(receipt[1], run_id="x")], index),
           ([receipt[0], dict(receipt[1], transcript="x")], index),
           ([receipt[0], dict(receipt[1], index_paths=[])], index),
           (receipt, index[:1]), (receipt, index + index[1:]), (receipt, index[:1] + index),
           (receipt, [dict(index[0], engine_pid=1), index[1]]),
           (receipt, [dict(index[0], model="x"), index[1]]),
           (receipt, [dict(index[0], usage_journal="x"), index[1]])]
    for r, i in bad:
        try:
            m.identity_check(ns, ids, pid, copy.deepcopy(r), copy.deepcopy(i))
        except m.CanaryFailure as exc:
            assert exc.code in ("receipt_identity", "index_identity")
        else:
            assert False, "identity defect accepted"
    # Foreign runs sharing the index are ignored, not joined.
    assert m.identity_check(ns, ids, pid, receipt, [foreign] + index)["status"] == "complete"


def test_usage_uses_real_journal_schema_zero_null_cache_and_duplicates(tmp_path):
    m, eng = load(), load_engine()
    path = tmp_path / "u.usage.jsonl"
    eng.append_usage_record(str(path), iteration=1, phase="worker", model="m", timestamp="t1",
                            raw_usage={"prompt_tokens": 0, "completion_tokens": 0, "secret": "SECRET"})
    eng.append_usage_record(str(path), iteration=1, phase="compaction", model="m", timestamp="t2",
                            raw_usage={"input_tokens": 50, "output_tokens": 7,
                                       "cache_read_input_tokens": 40, "cache_creation_input_tokens": 3})
    eng.append_usage_record(str(path), iteration=2, phase="worker", model="m", timestamp="t3",
                            raw_usage=None)
    rows = m._jsonl(path)
    assert "raw_usage" not in rows[0]
    meta = m.usage_metadata(rows)
    assert meta[0]["sent"] == 0 and meta[0]["recv"] == 0
    assert meta[1]["sent"] == rows[1]["sent"] and meta[1]["cache_read"] == rows[1]["cache_read"]
    assert meta[1]["sent"] is not None and meta[1]["sent"] >= 50
    assert meta[2]["sent"] is None and meta[2]["recv"] is None
    assert "SECRET" not in json.dumps(meta)
    try:
        m.usage_metadata(rows + rows[1:2])
    except m.CanaryFailure as exc:
        assert exc.code == "usage_duplicate"
    else:
        assert False, "duplicate usage row inflated totals"


CHAT, RESP = "/api/chat/completions", "/openai/responses"


def wire(case, path, **change):
    m = load()
    model = m.MODELS[case]
    if case == "gpt-responses":
        body = {"model": model, "input": [{"role": "user", "content": "SECRET"}],
                "max_output_tokens": 8192, "reasoning": {"effort": "high"}}
    else:
        body = {"model": model, "messages": [{"role": "user", "content": "SECRET"}],
                "max_completion_tokens": 8192}
        body.update({"reasoning_effort": "medium"} if case == "claude-chat"
                    else {"reasoning": {"effort": "high"}})
    for key, value in change.items():
        if value is None:
            body.pop(key, None)
        else:
            body[key] = value
    meta = m.wire_metadata(path, json.dumps(body).encode())
    assert "SECRET" not in json.dumps(meta)
    return m.wire_ok(case, meta)


def test_wire_checks_real_endpoint_cap_and_effort_form():
    assert wire("gpt-chat", CHAT) and wire("claude-chat", CHAT) and wire("gpt-responses", RESP)
    assert wire("gpt-chat", "/base" + CHAT) is False  # no guessed prefixes
    negatives = [("gpt-chat", RESP, {}), ("gpt-responses", CHAT, {}), ("gpt-chat", "/v1/chat", {}),
                 ("gpt-chat", CHAT, {"max_completion_tokens": None, "max_tokens": 8192}),
                 ("gpt-chat", CHAT, {"max_tokens": 8192}),
                 ("gpt-chat", CHAT, {"max_completion_tokens": 4096}),
                 ("gpt-responses", RESP, {"max_output_tokens": None, "max_completion_tokens": 8192}),
                 ("gpt-chat", CHAT, {"reasoning": None}),
                 ("gpt-chat", CHAT, {"reasoning": None, "reasoning_effort": "high"}),
                 ("gpt-responses", RESP, {"reasoning": {"effort": "medium"}}),
                 ("claude-chat", CHAT, {"reasoning_effort": None}),
                 ("claude-chat", CHAT, {"reasoning_effort": None, "reasoning": {"effort": "medium"}}),
                 ("gpt-chat", CHAT, {"model": "other"})]
    for case, path, change in negatives:
        assert wire(case, path, **change) is False, (case, path, change)


def test_observer_parses_full_url_path_only_and_maintenance_has_no_tools(tmp_path):
    from types import SimpleNamespace
    m = load()
    scope = SimpleNamespace(phase="compaction", last_estimate=None)
    o = m.Observer(SimpleNamespace(case="gpt-chat"), None, SimpleNamespace(current=lambda: scope), [])
    o.geometry = {"safe": True}
    body = {"model": "gpt-6.1-sol", "messages": [], "max_completion_tokens": 8192,
            "reasoning": {"effort": "high"}}
    sent = []
    req = SimpleNamespace(full_url="https://SECRET-HOST" + CHAT + "?SECRET=1", data=json.dumps(body).encode())
    o.urlopen(lambda *a, **k: sent.append(1), req)
    assert sent and o.wires[0]["checked"] and "SECRET" not in json.dumps(o.wires)
    body["tools"] = [{"type": "function"}]
    req = SimpleNamespace(full_url="https://h" + CHAT, data=json.dumps(body).encode())
    try:
        o.urlopen(lambda *a, **k: sent.append(1), req)
    except m.CanaryFailure as exc:
        assert exc.code == "wire_identity"
    else:
        assert False, "maintenance request with tools accepted"
    assert len(sent) == 1


def call(i, name="read_file", args='{"path": "SECRET"}'):
    return {"role": "assistant", "content": None, "tool_calls": [
        {"id": i, "type": "function", "function": {"name": name, "arguments": args}}]}


def result(i):
    return {"role": "tool", "tool_call_id": i, "content": "SECRET"}


def write_rows(path, rows):
    path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")


def expect(code, fn, *args):
    try:
        fn(*args)
    except Exception as exc:  # each load() has its own CanaryFailure class; match the code
        assert getattr(exc, "code", None) == code, type(exc).__name__
    else:
        assert False, code


def test_journal_append_only_pairs_finish_and_duplicates(tmp_path):
    m = load()
    path = tmp_path / "j.messages.jsonl"
    base = [{"role": "system", "content": "SECRET"}, {"role": "user", "content": "SECRET"}]
    write_rows(path, base + [call("a"), result("a")])
    first = m.journal_mark(path, None)
    assert set(first) == {"bytes", "sha256_prefix"} and "SECRET" not in json.dumps(first)
    finish = call("f", "finish", '{"summary": "SECRET"}')
    rows = base + [call("a"), result("a"), {"role": "assistant", "content": "prose"},
                   {"role": "assistant", "content": "prose"}, call("a", args='{"path": "other"}'),
                   result("a"), finish, result("f")]
    write_rows(path, rows)
    second = m.journal_mark(path, first)
    assert second["bytes"] > first["bytes"]
    meta = m.journal_metadata(m._jsonl(path))
    assert meta["paired"] and meta["normal_finish"] and "SECRET" not in json.dumps(meta)
    write_rows(path, [base[1]] + rows)  # prefix rewritten
    expect("journal_rewritten", m.journal_mark, path, second)
    write_rows(path, base)  # truncated
    expect("journal_rewritten", m.journal_mark, path, second)
    for broken in ([call("a")] + [result("a")] * 2, [call("a"), call("b"), result("b")],
                   [result("z")], [call("a"), {"role": "user", "content": "x"}, result("a")]):
        expect("journal_pairs", m.journal_metadata, base + broken + [finish, result("f")])
    expect("journal_duplicate", m.journal_metadata,
           base + [call("a"), result("a"), call("a"), result("a"), finish, result("f")])
    assert not m.journal_metadata(base + [call("f", "finish", '{"summary": " "}'), result("f")])["normal_finish"]
    assert not m.journal_metadata(base + [call("f", "finish", "{bad"), result("f")])["normal_finish"]


def test_collect_retains_metadata_when_artifacts_fail(tmp_path):
    m, ns, ids, pid, receipt, index = identity_fixture(tmp_path)
    o = m.Observer(ns, None, None, [])
    o.wires.append({"checked": True, "route": "chat"})
    write_rows(Path(ns.launch_receipt), receipt)
    write_rows(ids["index"], index)
    # usage and journal missing entirely
    result, code = m._collect(ns, ids, o, 0, pid)
    assert code == 1 and not result["ok"] and result["error"] == "usage_missing"
    assert result["launch_id"] == ns.launch_id and result["wires"] == o.wires
    assert result["run_id"] == ids["run_id"] and result["artifacts"]["usage"] == str(ids["usage"])
    # malformed finish args, engine failed: actual exit preserved, no text leaked
    write_rows(ids["usage"], [{"iter": 1, "phase": "worker", "sent": 1, "recv": 1,
                               "cache_read": None, "cache_write": None, "raw": {}}])
    write_rows(ids["messages"], [{"role": "user", "content": "SECRET"},
                                 call("f", "finish", "{SECRET")])
    result, code = m._collect(ns, ids, o, 7, pid)
    assert code == 7 and result["error"] == "engine_failed" and result["engine_exit"] == 7
    assert result["normal_finish"] is False and "SECRET" not in json.dumps(result)
    Path(ns.launch_receipt).write_text("{SECRET", encoding="utf-8")
    result, code = m._collect(ns, ids, o, 0, pid)
    assert result["error"] == "artifact_invalid" and "SECRET" not in json.dumps(result)
    assert result["wires"] == o.wires


def test_brief_lists_root_relative_forward_slash_paths():
    m = load()
    paths = [m.ROOT / ".claude" / "agent-runs" / "canary-fixtures" / "u" / "files" / "fixture-01.txt"]
    brief = m._brief("PINNED-AGENTS", paths)
    assert ".claude/agent-runs/canary-fixtures/u/files/fixture-01.txt" in brief
    assert str(m.ROOT) not in brief and "PINNED-AGENTS" in brief


def test_journal_accepts_the_engine_paired_finish_result(tmp_path):
    # delegate.py 6725..6761 finalizes and journals a result for finish as for other calls.
    m = load()
    path = tmp_path / "paired.messages.jsonl"
    rows = [{"role": "system", "content": "synthetic"},
            call("read", args='{"path":"synthetic.txt"}'), result("read"),
            call("done", "finish", '{"summary":"synthetic complete"}'), result("done")]
    write_rows(path, rows)
    meta = m.journal_metadata(m._jsonl(path))
    assert meta["paired"] is True and meta["normal_finish"] is True
    assert meta["actions"] == 2 and "synthetic complete" not in json.dumps(meta)


def test_journal_never_accepts_an_orphan_finish(tmp_path):
    m = load()
    path = tmp_path / "orphan.messages.jsonl"
    write_rows(path, [call("done", "finish", '{"summary":"synthetic complete"}')])
    expect("journal_pairs", m.journal_metadata, m._jsonl(path))


def test_finish_requires_summary_not_an_unrelated_nonempty_argument(tmp_path):
    m = load()
    path = tmp_path / "blank-summary.messages.jsonl"
    write_rows(path, [call("done", "finish", '{"summary":" ","other":"synthetic"}'), result("done")])
    assert m.journal_metadata(m._jsonl(path))["normal_finish"] is False


def test_selected_engine_bootstrap_supports_real_late_sibling_imports(tmp_path):
    # The live importlib host lacked delegate.py's script-directory sys.path entry.
    code = ("import importlib.util,pathlib,sys; "
            "p=pathlib.Path(sys.argv[1]); "
            "s=importlib.util.spec_from_file_location('canary_import_probe',p); "
            "m=importlib.util.module_from_spec(s);s.loader.exec_module(m); "
            "before=list(sys.path); "
            "ctx=m._loaded_engine(pathlib.Path(sys.argv[2])); "
            "e=ctx.__enter__(); r=e.capability_module('recovery_runtime'); "
            "assert pathlib.Path(r.rh.__file__).resolve().parent==pathlib.Path(sys.argv[2]).resolve().parent; "
            "ctx.__exit__(None,None,None);assert sys.path==before")
    r = subprocess.run([sys.executable, "-c", code, str(SCRIPT), str(ROOT / "engine" / "delegate.py")],
                       capture_output=True, text=True, cwd=tmp_path, timeout=30)
    assert r.returncode == 0, "late sibling import bootstrap failed"


def test_real_loop_usage_is_kept_and_every_wire_is_accounted(tmp_path):
    m, eng = load(), load_engine()
    path = tmp_path / "loop.usage.jsonl"
    eng.append_usage_record(str(path), iteration=1, phase="loop", model="synthetic", timestamp="t",
                            raw_usage={"prompt_tokens": 100, "completion_tokens": 10})
    rows = m.usage_metadata(m._jsonl(path))
    assert rows[0]["phase"] == "loop"
    state = good_state("gpt-chat")
    state.update(usage=rows, compactions=[], wires=[{"checked": True, "phase": "worker"}],
                 ends=[{"status": "complete", "refused": 0, "sent": 100, "recv": 10}])
    assert m.final_gate(state) is None
    state["wires"].append({"checked": True, "phase": "worker"})
    assert m.final_gate(state) == "usage_coverage"
    state["wires"].pop()
    state["ends"][0]["sent"] = 99
    assert m.final_gate(state) == "usage_total_mismatch"


def test_reads_are_recorded_only_after_complete_result_and_actual_wire_delivery(tmp_path, monkeypatch):
    from types import SimpleNamespace
    m = load()
    monkeypatch.setattr(m, "ROOT", tmp_path)
    text = "SYNTHETIC-first-line\nSYNTHETIC-final-line\n"
    (tmp_path / "AGENTS.md").write_text(text, encoding="utf-8")
    ns = SimpleNamespace(case="gpt-chat")
    ce = SimpleNamespace(current=lambda: SimpleNamespace(phase="worker", last_estimate=None))
    failed = m.Observer(ns, None, ce, [])
    expect("read_incomplete", failed.dispatch, lambda *_: "error: synthetic", None,
           "read_file", {"path": "AGENTS.md"})
    assert failed.reads == []
    o = m.Observer(ns, None, ce, [])
    o.geometry = {"safe": True}
    assert o.dispatch(lambda *_: text, None, "read_file", {"path": "AGENTS.md"}) == text
    assert o.reads == [str(tmp_path / "AGENTS.md")] and o.delivered_reads == []
    body = {"model": "gpt-6.1-sol", "max_completion_tokens": 8192,
            "reasoning": {"effort": "high"}, "messages": [call("read"), result("read")]}
    for content, expected in (("SYNTHETIC-first-line\n", []),
                              (json.dumps({"preview": text}), o.reads)):
        body["messages"][-1]["content"] = content
        req = SimpleNamespace(full_url="https://synthetic/api/chat/completions",
                              data=json.dumps(body).encode())
        o.urlopen(lambda *_: None, req)
        assert o.delivered_reads == expected
    state = good_state(); state["delivered_reads"] = state["expected_reads"][:-1]
    assert m.final_gate(state) == "read_not_delivered"


def test_artifact_stat_failure_keeps_late_collection_metadata(tmp_path, monkeypatch):
    m, ns, ids, pid, receipt, index = identity_fixture(tmp_path)
    o = m.Observer(ns, None, None, [])
    o.wires.append({"checked": True, "route": "chat"})
    write_rows(Path(ns.launch_receipt), receipt)
    write_rows(ids["index"], index)
    original = Path.is_file
    def denied(path):
        if path == ids["usage"]:
            raise PermissionError("SYNTHETIC-PRIVATE-ERROR")
        return original(path)
    monkeypatch.setattr(Path, "is_file", denied)
    try:
        report, code = m._collect(ns, ids, o, 0, pid)
    except Exception:
        assert False, "late filesystem exception escaped collection"
    assert code == 1 and report["error"] == "artifact_invalid"
    assert report["launch_id"] == ns.launch_id and report["wires"] == o.wires
    assert "SYNTHETIC-PRIVATE-ERROR" not in json.dumps(report)


def test_jsonl_stat_failure_has_fixed_code(tmp_path, monkeypatch):
    m = load()
    path = tmp_path / "private.jsonl"
    path.write_text("{}\n", encoding="utf-8")
    original = Path.is_file
    def denied(value):
        if value == path:
            raise PermissionError("SYNTHETIC-PRIVATE-ERROR")
        return original(value)
    monkeypatch.setattr(Path, "is_file", denied)
    expect("artifact_invalid", m._jsonl, path)


def test_usage_phase_sequence_cannot_hide_missing_maintenance(tmp_path):
    m = load()
    state = good_state()
    state["wires"].append({"checked": True, "phase": "compaction"})
    state["usage"].append({"phase": "loop", "sent": 300, "recv": 30})
    state["ends"][0].update(sent=600, recv=60)
    # Total count and totals match, but a maintenance generation's record is missing.
    assert m.final_gate(state) == "usage_phase_coverage"
    state["usage"][-1]["phase"] = None
    assert m.final_gate(state) == "usage_phase_coverage"
    state["usage"][-1]["phase"] = "compaction"
    assert m.final_gate(state) is None
    state["usage"][0], state["usage"][1] = state["usage"][1], state["usage"][0]
    assert m.final_gate(state) == "usage_phase_coverage"


def test_report_write_failure_preserves_collected_stdout_metadata(tmp_path, monkeypatch, capsys):
    m = load()
    def execute(ns, out):
        return {"ok": True, "error": None, "launch_id": ns.launch_id, "run_id": "synthetic-run",
                "engine_exit": 0, "wires": [{"checked": True}],
                "artifacts": {"index": str(out / "index.jsonl")}}, 0
    monkeypatch.setattr(m, "_execute", execute)
    for i, error in enumerate((PermissionError, TypeError)):
        def fail_write(*args, **kwargs):
            raise error("SYNTHETIC-PRIVATE-ERROR")
        monkeypatch.setattr(m.json, "dump", fail_write)
        launch = str(uuid.uuid4())
        argv = ["--run-live", "--case", "gpt-chat", "--engine", "source", "--out",
                str(tmp_path / ("out-%d" % i)), "--launch-id", launch,
                "--launch-receipt", str(tmp_path / ("receipt-%d.jsonl" % i))]
        assert m.main(argv) == 1
        text = capsys.readouterr().out
        report = json.loads(text)
        assert report["error"] == "result_write_failed" and report["ok"] is False
        assert report["launch_id"] == launch and report["run_id"] == "synthetic-run"
        assert report["engine_exit"] == 0 and report["wires"] == [{"checked": True}]
        assert report["artifacts"]["index"].endswith("index.jsonl")
        assert "SYNTHETIC-PRIVATE-ERROR" not in text


def test_full_range_from_real_responses_tool_is_a_complete_allowed_read(tmp_path, monkeypatch):
    from types import SimpleNamespace
    m, eng = load(), load_engine()
    monkeypatch.setattr(m, "ROOT", tmp_path)
    text = "synthetic first\nsynthetic last\n"
    (tmp_path / "AGENTS.md").write_text(text, encoding="utf-8")
    reader = eng.capability_module("paged_tools")
    runner = SimpleNamespace(pending={}, read_whole=set(), _resolve=lambda p: str(tmp_path / p))
    calls = []
    def original(r, name, args):
        calls.append(name)
        return reader.read_file(r, args["path"], args.get("start_line"), args.get("end_line"), 30000)
    o = m.Observer(SimpleNamespace(case="gpt-responses"), None, None, [])
    value = o.dispatch(original, runner, "read_file", {"path": "AGENTS.md", "start_line": 1, "end_line": 100000})
    assert value == "(AGENTS.md lines 1-2)\n" + text
    assert o.reads == [str(tmp_path / "AGENTS.md")] and calls == ["read_file"]
    for args in ({"path": "AGENTS.md", "start_line": 2, "end_line": 100000},
                 {"path": "AGENTS.md", "start_line": 1, "end_line": 1},
                 {"path": "AGENTS.md", "start_line": True}):
        denied = m.Observer(SimpleNamespace(case="gpt-responses"), None, None, [])
        expect("read_forbidden", denied.dispatch, original, runner, "read_file", args)
    assert calls == ["read_file"]

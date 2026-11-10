"""Focused tests for MCP/skills capability wiring in engine/delegate.py,
engine/ask-model.py and engine/fanout.py. No live state: temp dirs only."""
import base64
import importlib.util
import json
import pathlib
import sys
import types

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
ENGINE = ROOT / "engine"


def load_engine(name, filename):
    spec = importlib.util.spec_from_file_location(name, ENGINE / filename)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def delegate(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    return load_engine("delegate_caps_under_test", "delegate.py")


def test_policy_keys_include_capabilities(delegate):
    assert "mcp" in delegate.POLICY_KEYS
    assert "skills_dirs" in delegate.POLICY_KEYS


# ---- stub runtime modules (the real ones are owned by other executors) -------

class MCPError(ValueError):
    pass


class StubMCP:
    instances = []

    def __init__(self, policy, root, allow_mutation=False, event=None):
        self.policy, self.root, self.allow_mutation, self.event = policy, root, allow_mutation, event
        self.discovered, self.closed, self.calls, self.result = {}, 0, [], None
        StubMCP.instances.append(self)

    def list_tools(self, server=None):
        if server is None:
            return {"servers": sorted(self.policy["servers"])}
        if server not in self.policy["servers"]:
            raise MCPError(f"server {server} not allowed")
        self.discovered[f"mcp__{server}__echo"] = {
            "type": "object", "properties": {"msg": {"type": "string", "maxLength": 9}},
            "required": ["msg"]}
        return {"echo": "echoes"}

    def native_tools(self):
        return [{"type": "function", "function": {"name": n, "description": "echoes",
                                                   "parameters": s}}
                for n, s in self.discovered.items()]

    def owns(self, name):
        return name.startswith("mcp__")

    def call(self, name, args):
        self.calls.append((name, args))
        if self.event:
            self.event({"phase": "start", "server": "s", "tool": "echo", "args": args,
                        "timeout_s": 30})
            self.event({"phase": "end", "server": "s", "tool": "echo", "timeout_s": 30,
                        "duration_s": 0.1, "status": "ok", "result": {"secret": "RESULT"}})
        return self.result or {"content": [{"type": "text", "text": "echo " + args["msg"]}]}

    def close(self):
        self.closed += 1


def stub_mcp_module():
    real = load_engine("harness_mcp_schema_for_stub", "harness_mcp.py")
    # Native preflight validates against the real pure schema checks; only I/O is stubbed.
    m = types.SimpleNamespace(MCPManager=StubMCP, MCPError=MCPError,
                              check_schema=real.check_schema, validate_args=real.validate_args)

    def validate(value):
        if not isinstance(value, dict) or not isinstance(value.get("servers"), dict):
            raise MCPError("mcp must be an object with servers")
        return {"allowed_commands": value.get("allowed_commands", []),
                "servers": value["servers"]}
    m.validate_mcp_policy = validate
    return m


class StubSkills:
    def __init__(self, root, skills_dirs=(), home=None, strict_duplicates=True):
        self.root, self.loaded = root, []
        self.dir = pathlib.Path(root) / "skills"

    def catalog(self):
        return [{"name": "tidy", "description": "Keep code tidy.\nsecond line"}]

    def load(self, name, max_chars=None):
        if name != "tidy":
            raise ValueError(f"unknown skill {name}")
        if name not in self.loaded:
            self.loaded.append(name)
        return "TIDY-BODY: always tidy"

    def resolve_read(self, uri):
        name, _, rel = uri[len("skill://"):].partition("/")
        if name not in self.loaded or ".." in rel.split("/"):
            raise ValueError("refused")
        return str(self.dir / rel)


def stub_skills_module():
    def validate(value):
        if not isinstance(value, list):
            raise ValueError("skills_dirs must be a list")
        return list(value)
    return types.SimpleNamespace(SkillRegistry=StubSkills, validate_skills_dirs=validate)


@pytest.fixture()
def caps(delegate):
    StubMCP.instances.clear()
    delegate._CAP_MODULES["harness_mcp"] = stub_mcp_module()
    delegate._CAP_MODULES["harness_skills"] = stub_skills_module()
    return delegate


def write_policy(root, pol):
    (root / ".claude").mkdir(parents=True, exist_ok=True)
    (root / ".claude" / "agent-policy.json").write_text(json.dumps(pol), encoding="utf-8")


POL = {"mcp": {"servers": {"s": {"command": ["node", "srv.js"]}}}}


def test_policy_reset_and_bad_nested_policy_fails_closed(caps, tmp_path):
    write_policy(tmp_path, POL)
    caps.load_policy(str(tmp_path))
    assert caps.MCP_POLICY["servers"] == POL["mcp"]["servers"]
    other = tmp_path / "other"
    other.mkdir()
    caps.load_policy(str(other))
    assert caps.MCP_POLICY is None and caps.SKILLS_DIRS == []
    write_policy(other, {"mcp": {"servers": "nope"}})
    with pytest.raises(SystemExit, match="invalid mcp policy"):
        caps.load_policy(str(other))
    assert caps.MCP_POLICY is None


def test_bad_policy_denied_before_network(caps, tmp_path, monkeypatch):
    write_policy(tmp_path, {"skills_dirs": "notalist"})
    monkeypatch.setattr(caps, "load_cfg", lambda: pytest.fail("gateway contacted"))
    monkeypatch.setattr(caps, "make_worktree", lambda *a: pytest.fail("worktree made"))
    monkeypatch.setattr(sys, "argv", ["delegate.py", "-t", "x", "-C", str(tmp_path),
                                      "--worktree"])
    with pytest.raises(SystemExit, match="invalid skills_dirs"):
        caps.main()


def test_missing_runtime_module_fails_closed(delegate, tmp_path, monkeypatch):
    monkeypatch.setattr(delegate, "ENGINE_DIR", str(tmp_path / "nowhere"))
    delegate._CAP_MODULES.clear()
    write_policy(tmp_path, POL)
    with pytest.raises(SystemExit, match="not installed"):
        delegate.load_policy(str(tmp_path))


def make_runner(caps, tmp_path, **kw):
    caps.load_policy(str(tmp_path))
    return caps.Runner(str(tmp_path), "stage", mcp_policy=POL["mcp"],
                       artifact_dir=str(tmp_path / ".claude" / "agent-runs" / "r.artifacts"),
                       **kw)


def names(tools):
    return [t["function"]["name"] for t in tools]


def test_old_runner_signature_and_no_policy_no_mcp(caps, tmp_path):
    run = caps.Runner(str(tmp_path), "stage")
    tools = names(caps.native_tools(run))
    assert "list_mcp_tools" in tools and "load_skill" in tools
    assert "no MCP servers" in run.dispatch("list_mcp_tools", {})
    assert StubMCP.instances == []
    assert names(caps.native_tools())[:1] == ["read_file"]   # old callers work
    run.close()
    run.close()


def test_native_and_prompt_discovery_next_turn_and_unadvertised_refused(caps, tmp_path):
    run = make_runner(caps, tmp_path)
    assert run.dispatch("list_mcp_tools", {}).count("servers")
    assert StubMCP.instances == [] or not StubMCP.instances[0].discovered
    schema = {t["function"]["name"]: t["function"]["parameters"] for t in caps.native_tools(run)}
    assert schema["load_skill"]["required"] == ["name"]
    assert schema["list_mcp_tools"]["required"] == []
    assert "mcp__s__echo" not in schema
    assert "not been discovered" in run.dispatch("mcp__s__echo", {"msg": "hi"})
    out = run.dispatch("list_mcp_tools", {"server": "s"})
    assert "NEXT reply" in out
    # still not advertised until the next turn's catalog is built
    assert "not been discovered" in run.dispatch("mcp__s__echo", {"msg": "hi"})
    tools = {t["function"]["name"]: t for t in caps.native_tools(run)}
    assert tools["mcp__s__echo"]["function"]["parameters"]["properties"]["msg"]["maxLength"] == 9
    assert run.dispatch("mcp__s__echo", {"msg": "hi"}) == "echo hi"
    assert "mcp__s__echo" in caps.prompt_system("gpt-x", run)
    assert "mcp__s__echo" in run.caps.advertised
    assert "not allowed" in run.dispatch("list_mcp_tools", {"server": "evil"})
    assert "no such tool" in run.dispatch("__init__", {})
    run.close()
    assert StubMCP.instances[0].closed == 1


def test_mutation_only_with_explicit_flag(caps, tmp_path):
    run = make_runner(caps, tmp_path)
    run.dispatch("list_mcp_tools", {"server": "s"})
    assert StubMCP.instances[0].allow_mutation is False
    run2 = make_runner(caps, tmp_path, allow_mcp_mutation=True)
    run2.dispatch("list_mcp_tools", {"server": "s"})
    assert StubMCP.instances[1].allow_mutation is True
    run.close(), run2.close()


def test_skill_catalog_load_and_uri_read_only(caps, tmp_path):
    (tmp_path / "skills").mkdir()
    (tmp_path / "skills" / "ref.md").write_text("REFERENCE", encoding="utf-8")
    run = make_runner(caps, tmp_path)
    loaded = []
    run.caps.on_skill_load = loaded.append
    sysmsg = caps.base_rules("m", run)
    assert "tidy: Keep code tidy. second line" in sysmsg and "TIDY-BODY" not in sysmsg
    assert "refused" in run.dispatch("read_file", {"path": "skill://tidy/ref.md"})
    body = run.dispatch("load_skill", {"name": "tidy"})
    assert "TIDY-BODY" in body and "subordinate" in body and loaded == ["tidy"]
    assert run.dispatch("read_file", {"path": "skill://tidy/ref.md"}) == "REFERENCE"
    assert "Loaded skills: tidy" in caps.base_rules("m", run)
    for tool, a in [("write_file", {"path": "skill://tidy/x", "content": "x"}),
                    ("list_dir", {"path": "skill://tidy"}),
                    ("grep", {"pattern": "R", "glob": None}),
                    ("replace_in_file", {"path": "skill://tidy/ref.md", "old_string": "R",
                                         "new_string": "W"}),
                    ("view_image", {"path": "skill://tidy/a.png"})]:
        if tool == "grep":
            continue
        assert run.dispatch(tool, a).startswith("error"), tool
    assert run.pending == {}
    assert (tmp_path / "skills" / "ref.md").read_text() == "REFERENCE"
    run.close()


def test_oversized_result_and_binary_artifacts(caps, tmp_path):
    run = make_runner(caps, tmp_path)
    run.dispatch("list_mcp_tools", {"server": "s"})
    caps.native_tools(run)
    png = b"\x89PNG\r\n\x1a\n" + b"0" * 64
    b64 = base64.b64encode(png).decode()
    StubMCP.instances[0].result = {"isError": True, "content": [
        {"type": "text", "text": "x" * (caps.MAX_READ_CHARS * 2)},
        {"type": "image", "mimeType": "image/png", "data": b64},
        {"type": "image", "mimeType": "image/png", "data": "!!notb64"}]}
    out = run.dispatch("mcp__s__echo", {"msg": "hi"})
    assert len(out) <= caps.MAX_READ_CHARS
    assert "isError: true" in out and b64 not in out and "refused" in out
    refs = [w for w in out.replace("]", " ").split() if w.startswith(".claude/")]
    img = [r for r in refs if r.endswith(".png")][0]
    js = [r for r in refs if r.endswith(".json")][0]
    assert (tmp_path / img).read_bytes() == png
    assert b64 not in (tmp_path / js).read_text()
    assert "lines 1-1" in run.dispatch("read_file", {"path": js, "start_line": 1,
                                                    "end_line": 1})
    run.close()


def test_artifact_uri_when_outside_root(caps, tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    caps.load_policy(str(root))
    run = caps.Runner(str(root), "stage", mcp_policy=POL["mcp"],
                      artifact_dir=str(tmp_path / "out" / "r.artifacts"))
    ref = run.caps.save_artifact(b'{"a": 1}', ".json")
    assert ref.startswith("artifact://")
    assert run.dispatch("read_file", {"path": ref}) == '{"a": 1}'
    assert "unknown artifact" in run.dispatch("read_file", {"path": "artifact://../x"})
    assert "error" in run.dispatch("read_file", {"path": "https://example.com/x"})
    run.close()


def test_close_survives_manager_exception(caps, tmp_path):
    run = make_runner(caps, tmp_path)
    run.dispatch("list_mcp_tools", {"server": "s"})

    def boom():
        raise RuntimeError("close failed")
    StubMCP.instances[0].close = boom
    with pytest.raises(RuntimeError):
        run.close()
    assert run not in caps._OPEN_RUNNERS
    run.close()   # idempotent after failure


# ---- end-to-end main() with a synthetic gateway (no live state) --------------

def tc(i, name, args):
    return {"id": f"c{i}", "type": "function",
            "function": {"name": name, "arguments": json.dumps(args)}}


def reply(*calls, content="step"):
    return {"choices": [{"message": {"content": content, "tool_calls": list(calls)}}],
            "usage": {"prompt_tokens": 5, "completion_tokens": 1}}


@pytest.fixture()
def e2e(caps, tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_AGENT_RUN_INDEX", str(tmp_path / "home" / "index.jsonl"))
    monkeypatch.setattr(caps, "CONF", str(tmp_path / "home" / "conf.env"))
    monkeypatch.setattr(caps, "load_cfg", lambda: ("http://gw", "k"))
    monkeypatch.setattr(caps, "refresh_models", lambda u, k: None)
    monkeypatch.setattr(caps.atexit, "register", lambda *a, **k: None)
    monkeypatch.setattr(caps.signal, "signal", lambda *a, **k: None)
    monkeypatch.chdir(tmp_path)
    write_policy(tmp_path, POL)
    tpath = tmp_path / ".claude" / "agent-runs" / "run.md"

    class E:
        payloads = []
        transcript = tpath

        def run(self, *argv, replies=(), model="anthropic_tool_call.claude-x"):
            replies = list(replies)

            def fake_call(url, key, payload, mode, tr, anomaly, turn, streaming=None):
                self.payloads.append(json.loads(json.dumps(payload)))
                r = replies.pop(0)
                if isinstance(r, BaseException):
                    raise r
                return r, True, None, 1.0
            monkeypatch.setattr(caps, "call_model", fake_call)
            mode = [] if "--oneshot" in argv else ["--tool-mode", "native"]
            monkeypatch.setattr(sys, "argv", ["delegate.py", "-t", "do it", "-m", model,
                                              *mode, "-C", str(tmp_path),
                                              "--transcript", str(tpath), *argv])
            caps.main()
            return tpath.read_text(encoding="utf-8")
    return E()


def test_e2e_discovery_events_usage_and_cleanup(e2e, caps, tmp_path):
    text = e2e.run(replies=[
        reply(tc(1, "list_mcp_tools", {"server": "s"})),
        reply(tc(2, "mcp__s__echo", {"msg": "hi"})),
        reply(tc(3, "finish", {"summary": "done"}))])
    first, second = (names(p["tools"]) for p in e2e.payloads[:2])
    assert "mcp__s__echo" not in first and "mcp__s__echo" in second
    assert "- loaded skills: (none)" in text
    assert '_MCP_CALL {"phase":"start","server":"s","tool":"echo","timeout_s":30,' \
           '"duration_s":null}_' in text
    assert '_MCP_CALL {"phase":"end","server":"s","tool":"echo","timeout_s":30,' \
           '"duration_s":0.1,"status":"ok"}_' in text
    assert "MCP result:" in text
    rows = [json.loads(l) for l in (tmp_path / ".claude" / "agent-runs" / "run.usage.jsonl")
            .read_text().splitlines()]
    mcp_rows = [r for r in rows if r["phase"].startswith("mcp_")]
    assert [r["phase"] for r in mcp_rows] == ["mcp_start", "mcp_end"]
    for r in mcp_rows:
        assert r["sent"] is None and r["recv"] is None and r["cache_read"] is None
        assert "RESULT" not in json.dumps(r) and "hi" not in json.dumps(r.get("args", ""))
        assert "args" not in r and "result" not in r
    index = (tmp_path / ".claude" / "agent-runs" / "agent-runs-index.jsonl").read_text()
    assert "RESULT" not in index and "echo hi" not in index
    assert StubMCP.instances[0].closed == 1
    assert StubMCP.instances[0].allow_mutation is False


def test_e2e_claude_multicall_refused_before_execution(e2e, caps):
    # Inline refusal text is the legacy context wire contract.
    e2e.run("--context-policy", "legacy", replies=[
        reply(tc(1, "list_mcp_tools", {"server": "s"}), tc(2, "load_skill", {"name": "tidy"})),
        reply(tc(3, "finish", {"summary": "done"}))])
    assert StubMCP.instances == []         # nothing ran
    tool_msgs = [m for m in e2e.payloads[1]["messages"] if m["role"] == "tool"]
    assert len(tool_msgs) == 2 and all("ONE tool" in m["content"] for m in tool_msgs)


def test_e2e_claude_multicall_refused_before_execution_model_v1(e2e, caps):
    e2e.run(replies=[
        reply(tc(1, "list_mcp_tools", {"server": "s"}), tc(2, "load_skill", {"name": "tidy"})),
        reply(tc(3, "finish", {"summary": "done"}))])
    assert StubMCP.instances == []         # nothing ran
    tool_msgs = [m for m in e2e.payloads[1]["messages"] if m["role"] == "tool"]
    assert [m["tool_call_id"] for m in tool_msgs] == ["c1", "c2"]
    assert all('"reader_tool":"read_context_result"' in m["content"] for m in tool_msgs)
    assert [(m["role"], m.get("tool_call_id")) for m in e2e.payloads[1]["messages"]
            if "TIDY-BODY" in json.dumps(m)] == []


def test_e2e_gpt_multicall_still_runs_in_order(e2e, caps):
    e2e.run(model="gpt-x", replies=[
        reply(tc(1, "list_mcp_tools", {}), tc(2, "load_skill", {"name": "tidy"})),
        reply(tc(3, "finish", {"summary": "done"}))])
    tool_msgs = [m for m in e2e.payloads[1]["messages"] if m["role"] == "tool"]
    assert "servers" in tool_msgs[0]["content"] and "TIDY-BODY" in tool_msgs[1]["content"]


def test_e2e_skill_preload_and_runtime_load(e2e, caps):
    text = e2e.run("--skill", "tidy", replies=[
        reply(tc(1, "finish", {"summary": "done"}))])
    assert "- loaded skills: tidy" in text
    msgs = e2e.payloads[0]["messages"]
    assert "TIDY-BODY" not in msgs[0]["content"] and "tidy: Keep code tidy" in msgs[0]["content"]
    assert "TIDY-BODY" in msgs[1]["content"] and "subordinate" in msgs[1]["content"]


def test_e2e_runtime_skill_load_marker(e2e, caps):
    text = e2e.run(model="gpt-x", replies=[
        reply(tc(1, "load_skill", {"name": "tidy"})),
        reply(tc(2, "finish", {"summary": "done"}))])
    assert "_SKILL_LOADED tidy_" in text


def test_e2e_oneshot_skill_body_in_prompt(e2e, caps, monkeypatch):
    seen = {}

    def fake_oneshot(args, url, key, prompt, tr, *rest):
        seen["prompt"] = prompt
        tr.close()
    monkeypatch.setattr(caps, "run_oneshot", fake_oneshot)
    text = e2e.run("--oneshot", "--skill", "tidy")
    assert "TIDY-BODY" in seen["prompt"] and seen["prompt"].endswith("do it")
    assert "- loaded skills: tidy" in text and "TIDY-BODY" not in text
    assert StubMCP.instances == []


def test_e2e_unknown_skill_fails_before_network(e2e, caps, monkeypatch):
    monkeypatch.setattr(caps, "load_cfg", lambda: pytest.fail("gateway contacted"))
    with pytest.raises(SystemExit):
        e2e.run("--skill", "nope")


def test_e2e_gateway_failure_still_closes_mcp(e2e, caps):
    with pytest.raises(SystemExit):
        e2e.run(replies=[reply(tc(1, "list_mcp_tools", {"server": "s"})),
                         SystemExit(1)])
    assert StubMCP.instances[0].closed == 1


@pytest.mark.skipif(not (ENGINE / "harness_skills.py").exists()
                    or not (ENGINE / "harness_mcp.py").exists(),
                    reason="runtime capability modules not installed yet")
def test_real_modules_skills_and_no_mcp_policy(delegate, tmp_path):
    delegate._CAP_MODULES.clear()
    home = tmp_path / "home"
    home.mkdir()
    sk = tmp_path / ".claude" / "skills" / "tidy"
    sk.mkdir(parents=True)
    (sk / "SKILL.md").write_text("---\nname: tidy\ndescription: Keep it tidy.\n---\n"
                                 "TIDY-BODY\n", encoding="utf-8")
    (sk / "ref.md").write_text("REFERENCE", encoding="utf-8")
    note = delegate.load_policy(str(tmp_path))
    assert delegate.MCP_POLICY is None and "built-in" in note
    run = delegate.Runner(str(tmp_path), "stage", skills_home=str(home))
    assert "tidy" in delegate.base_rules("m", run)
    assert "TIDY-BODY" not in delegate.base_rules("m", run)
    assert "TIDY-BODY" in run.dispatch("load_skill", {"name": "tidy"})
    assert run.dispatch("read_file", {"path": "skill://tidy/ref.md"}) == "REFERENCE"
    assert "error" in run.dispatch("read_file", {"path": "skill://tidy/../../x"})
    assert "error" in run.dispatch("write_file", {"path": "skill://tidy/ref.md",
                                                  "content": "x"})
    assert "no MCP servers" in run.dispatch("list_mcp_tools", {})
    write_policy(tmp_path, {"mcp": {"servers": 5}})
    with pytest.raises(SystemExit):
        delegate.load_policy(str(tmp_path))
    run.close()


def test_ask_model_default_toolless_and_tools_mode(tmp_path):
    ask = load_engine("ask_model_caps", "ask-model.py")
    a = ask.parse_args(["-p", "x", "-m", "m", "f.txt"])
    argv = ask.build_argv(a, "P")
    assert "--oneshot" in argv and "--write" not in argv and "--skill" not in argv
    a = ask.parse_args(["-p", "x", "-m", "m", "--tools", "--max-iters", "4",
                        "--skill", "a", "--skill", "b"])
    argv = ask.build_argv(a, "P")
    assert "--oneshot" not in argv and argv[argv.index("--max-iters") + 1] == "4"
    assert "--write" not in argv
    assert [argv[i + 1] for i, v in enumerate(argv) if v == "--skill"] == ["a", "b"]
    assert "--write" in ask.build_argv(
        ask.parse_args(["-p", "x", "-m", "m", "--tools", "--write"]), "P")
    for bad in (["--tools", "f.txt"], ["--tools", "--image", "a.png"], ["--write"],
                ["--max-iters", "3"]):
        with pytest.raises(SystemExit):
            ask.parse_args(["-p", "x", "-m", "m", *bad])


def test_fanout_forwards_skills_and_explicit_write_only(tmp_path, monkeypatch):
    fan = load_engine("fanout_caps", "fanout.py")
    seen = []

    class P:
        returncode, stdout, stderr = 0, "ok", ""

        def __init__(self, argv, **kw):
            seen.append(argv)

        def communicate(self, timeout=None):
            return "report", ""

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass
    monkeypatch.setattr(fan.subprocess, "Popen", P)
    monkeypatch.setattr(fan.subprocess, "run", lambda argv, **kw: (seen.append(argv), P(argv))[1])
    fan.run_agent(1, "m", "high", "lens", "t", str(tmp_path), 3, "s", 5)
    assert "--skill" not in seen[-1] and "--write" not in seen[-1]
    fan.run_agent(1, "m", "high", "lens", "t", str(tmp_path), 3, "s", 5, skills=["a"])
    assert "--skill" in seen[-1] and "--write" not in seen[-1]
    fan.run_agent(1, "m", "high", "lens", "t", str(tmp_path), 3, "s", 5, write=True)
    assert "--write" in seen[-1]
    fan.synthesize("t", [(1, "m", "l", "r")], "m", str(tmp_path), skills=["a"])
    assert "--oneshot" in seen[-1] and "--skill" in seen[-1] and "--write" not in seen[-1]


@pytest.mark.parametrize("model", [
    "anthropic_tool_call.claude-opus-5-5-think", "gpt-6.1-sol",
])
def test_tool_wrappers_inherit_native_mode(caps, tmp_path, monkeypatch, model):
    ask = load_engine("ask_native_default", "ask-model.py")
    args = ask.parse_args(["-p", "synthetic task", "-m", model, "--tools",
                           "-C", str(tmp_path)])
    children = [ask.build_argv(args, str(tmp_path / "brief.txt"))]
    fan = load_engine("fan_native_default", "fanout.py")

    class Child:
        returncode = 0

        def __init__(self, argv, **kwargs):
            children.append(argv)

        def communicate(self, timeout=None):
            return "synthetic report", ""

    monkeypatch.setattr(fan.subprocess, "Popen", Child)
    fan.run_agent(1, model, "high", "synthetic lens", "synthetic task",
                  str(tmp_path), 3, "stamp", 5)
    assert len(children) == 2
    for argv in children:
        assert "--oneshot" not in argv
        assert "--tool-mode" not in argv
        assert "--allow-prompt-tools" not in argv
        assert caps.auto_tool_mode(argv[argv.index("-m") + 1]) == "native"


def test_e2e_worktree_does_not_grant_mutation(e2e, caps, monkeypatch, tmp_path):
    wt = tmp_path / "wt"
    wt.mkdir()
    monkeypatch.setattr(caps, "make_worktree", lambda root, label: (str(wt), "b"))
    monkeypatch.setattr(caps, "worktree_diff", lambda w: "")
    e2e.run("--worktree", replies=[reply(tc(1, "list_mcp_tools", {"server": "s"})),
                                   reply(tc(2, "finish", {"summary": "done"}))])
    assert StubMCP.instances[0].allow_mutation is False


# ---- post-review regressions -------------------------------------------------

def tiny_png():
    import struct
    import zlib

    def chunk(t, d):
        return (struct.pack(">I", len(d)) + t + d
                + struct.pack(">I", zlib.crc32(t + d) & 0xffffffff))
    ihdr = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", zlib.compress(b"\x00\xff\x00\x00")) + chunk(b"IEND", b""))


def outside_runner(caps, tmp_path):
    root = tmp_path / "repo"
    root.mkdir(exist_ok=True)
    caps.load_policy(str(root))
    return caps.Runner(str(root), "stage", mcp_policy=POL["mcp"],
                       artifact_dir=str(tmp_path / "out" / "r.artifacts")), root


def big_text(caps):
    n = caps.MAX_READ_CHARS
    lines = [f"line {i} " + "y" * 50 + "\n" for i in range(1, (4 * n) // 50 + 300)]
    lines.append("FINAL-SENTINEL\n")
    blob = "".join(lines)
    assert len(blob) > 4 * n + 1
    return blob, len(lines)


def test_uri_line_ranges_beyond_read_prefix(caps, tmp_path):
    run, root = outside_runner(caps, tmp_path)
    blob, last = big_text(caps)
    ref = run.caps.save_artifact(blob.encode(), ".txt")
    assert ref.startswith("artifact://")
    out = run.dispatch("read_file", {"path": ref, "start_line": last, "end_line": last})
    assert "FINAL-SENTINEL" in out and f"lines {last}-{last}" in out
    whole = run.dispatch("read_file", {"path": ref})
    assert whole.startswith("refused") and "start_line" in whole and len(whole) < 500
    wide = run.dispatch("read_file", {"path": ref, "start_line": 1, "end_line": last})
    assert len(wide) <= caps.MAX_READ_CHARS + 300 and "narrow" in wide
    past = run.dispatch("read_file", {"path": ref, "start_line": last + 5})
    assert "FINAL-SENTINEL" not in past and "past the end" in past
    (root / "skills").mkdir()
    (root / "skills" / "big.md").write_text(blob, encoding="utf-8")
    run.dispatch("load_skill", {"name": "tidy"})
    out = run.dispatch("read_file", {"path": "skill://tidy/big.md", "start_line": last - 1,
                                     "end_line": last})
    assert "FINAL-SENTINEL" in out and f"line {last - 1} " in out
    run.close()


def test_rpc_error_preserved_without_binary(caps, tmp_path):
    run = make_runner(caps, tmp_path)
    run.dispatch("list_mcp_tools", {"server": "s"})
    caps.native_tools(run)
    b64 = base64.b64encode(b"z" * 300).decode()
    StubMCP.instances[0].result = {"isError": True, "rpcError": {
        "code": -32602, "message": "bad params", "data": {"blob": b64}}}
    out = run.dispatch("mcp__s__echo", {"msg": "hi"})
    assert "isError: true" in out and "-32602" in out and "bad params" in out
    assert b64 not in out and len(out) <= caps.MAX_READ_CHARS
    run.close()


def test_embedded_resources_saved_and_no_base64_anywhere(caps, tmp_path, monkeypatch):
    monkeypatch.setattr(caps.urllib.request, "urlopen",
                        lambda *a, **k: pytest.fail("remote resource fetched"))
    run, root = outside_runner(caps, tmp_path)
    run.dispatch("list_mcp_tools", {"server": "s"})
    caps.native_tools(run)
    png = tiny_png()
    b64 = base64.b64encode(png).decode()
    bin64 = base64.b64encode(b"\x00\x01binary").decode()
    StubMCP.instances[0].result = {"content": [
        {"type": "resource", "resource": {"uri": "https://evil.example/x.png",
                                          "mimeType": "image/png", "blob": b64}},
        {"type": "resource", "resource": {"uri": "mem://b", "blob": bin64}},
        {"type": "resource", "resource": {"uri": "mem://t", "mimeType": "text/plain",
                                          "text": "RES-TEXT"}},
        {"type": "text", "text": "z" * (caps.MAX_READ_CHARS * 2)}]}
    out = run.dispatch("mcp__s__echo", {"msg": "hi"})
    assert b64 not in out and bin64 not in out and "RES-TEXT" in out
    refs = [w for w in out.replace("]", " ").split() if w.startswith("artifact://")]
    pngref = [r for r in refs if r.endswith(".png")][0]
    binref = [r for r in refs if r.endswith(".bin")][0]
    jsref = [r for r in refs if r.endswith(".json")][0]
    assert "view_image" in out
    names_ = {r: r[len("artifact://"):] for r in refs}
    assert open(run.caps.artifacts[names_[pngref]], "rb").read() == png
    assert open(run.caps.artifacts[names_[binref]], "rb").read() == b"\x00\x01binary"
    full = open(run.caps.artifacts[names_[jsref]], encoding="utf-8").read()
    assert b64 not in full and bin64 not in full and pngref in full
    assert isinstance(run.dispatch("view_image", {"path": pngref}), caps.ImageAsset)
    run.close()


def _symlink(target, link):
    import os
    try:
        os.symlink(str(target), str(link), target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable (e.g. Windows without privilege)")


def test_artifact_parent_symlink_escape_refused(caps, tmp_path):
    root = tmp_path / "repo"
    (root / ".claude").mkdir(parents=True)
    evil = tmp_path / "evil"
    evil.mkdir()
    _symlink(evil, root / ".claude" / "agent-runs")
    caps.load_policy(str(root))
    run = caps.Runner(str(root), "stage", mcp_policy=POL["mcp"],
                      artifact_dir=str(root / ".claude" / "agent-runs" / "r.artifacts"))
    with pytest.raises(ValueError, match="symlink|escape|plain"):
        run.caps.save_artifact(b"x", ".json")
    assert list(evil.iterdir()) == []
    run.close()


def test_artifact_dir_swap_after_first_write_refused(caps, tmp_path):
    import os
    run, root = outside_runner(caps, tmp_path)
    ref = run.caps.save_artifact(b"first", ".json")
    art = tmp_path / "out" / "r.artifacts"
    os.rename(str(art), str(tmp_path / "moved"))
    other = tmp_path / "other"
    other.mkdir()
    _symlink(other, art)
    with pytest.raises(ValueError):
        run.caps.save_artifact(b"second", ".json")
    assert list(other.iterdir()) == []
    assert "error" in run.dispatch("read_file", {"path": ref})
    run.close()


def test_artifact_count_bounded(caps, tmp_path, monkeypatch):
    monkeypatch.setattr(caps, "MCP_MAX_ARTIFACTS", 2)
    run, _ = outside_runner(caps, tmp_path)
    run.caps.save_artifact(b"a", ".bin")
    run.caps.save_artifact(b"b", ".bin")
    with pytest.raises(ValueError, match="too many artifacts"):
        run.caps.save_artifact(b"c", ".bin")
    run.close()


def test_view_image_only_registered_artifact_images(caps, tmp_path):
    run, root = outside_runner(caps, tmp_path)
    (root / "skills").mkdir()
    (root / "skills" / "a.png").write_bytes(tiny_png())
    run.dispatch("load_skill", {"name": "tidy"})
    txt = run.caps.save_artifact(b"{}", ".json")
    (tmp_path / "out" / "r.artifacts" / "stray.png").write_bytes(tiny_png())
    for p in ("skill://tidy/a.png", "artifact://stray.png", "artifact://../x.png", txt,
              "https://example.com/a.png"):
        assert str(run.dispatch("view_image", {"path": p})).startswith("error"), p
    run.close()


def test_skill_body_over_cap_refused_not_truncated(caps, tmp_path, monkeypatch):
    run = make_runner(caps, tmp_path)
    monkeypatch.setattr(StubSkills, "load", lambda self, name, **kw:
                        "B" * (caps.MAX_READ_CHARS + 10) + "TAIL")
    out = run.dispatch("load_skill", {"name": "tidy"})
    assert out.startswith("error") and "read_file" in out and "TAIL" not in out
    assert "truncated" not in out
    monkeypatch.setattr(sys, "exit", lambda code: (_ for _ in ()).throw(SystemExit(code)))
    with pytest.raises(SystemExit):
        caps.preload_skills(str(tmp_path), ["tidy"])
    run.close()

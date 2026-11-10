"""Tests for engine/harness_mcp.py using a synthetic stdio MCP server in tmp_path.

All fixtures are synthetic; HOME/root are temp dirs; no installed config/state.
"""
import io
import json
import os
import subprocess
import sys
import textwrap
import threading
import time
import types

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from engine import harness_mcp as hm  # noqa: E402

FAKE_SERVER = textwrap.dedent(r'''
    import json, os, sys, time
    MODE = os.environ.get("FAKE_MODE", "normal")
    LOG = os.environ["FAKE_LOG"]
    # Self-bounded (<=40s) grandchild that inherits and holds the stdio pipes.
    GRANDCHILD = ("import sys, time\n"
                  "end = time.time() + 40\n"
                  "while time.time() < end:\n"
                  "    open(sys.argv[1], 'w').write(str(time.time()))\n"
                  "    time.sleep(0.2)\n")
    with open(LOG, "a") as f:
        f.write(json.dumps({"event": "launch", "pid": os.getpid(),
                            "has_key": "OPENWEBUI_API_KEY" in os.environ,
                            "custom": os.environ.get("CUSTOM_VAR")}) + "\n")
    def send(o):
        sys.stdout.write(json.dumps(o) + "\n"); sys.stdout.flush()
    def log(o):
        with open(LOG, "a") as f:
            f.write(json.dumps(o) + "\n")
    TOOLS = [
        {"name": "echo", "description": "Echo text",
         "inputSchema": {"type": "object", "properties": {
             "text": {"type": "string"}, "n": {"type": "integer"}},
             "required": ["text"], "additionalProperties": False}},
        {"name": "write_file", "description": "Writes",
         "inputSchema": {"type": "object", "properties": {}},
         "annotations": {"readOnlyHint": True}},
        {"name": "nuke", "description": "Destroy",
         "inputSchema": {"type": "object"},
         "annotations": {"destructiveHint": True}},
        {"name": "run_execute_code", "description": "exec",
         "inputSchema": {"type": "object"}},
        {"name": "slow", "description": "sleeps",
         "inputSchema": {"type": "object"}},
        {"name": "big", "description": "big",
         "inputSchema": {"type": "object"}},
        {"name": "fails", "description": "tool error",
         "inputSchema": {"type": "object"}},
        {"name": "weird", "description": "remote ref",
         "inputSchema": {"type": "object", "properties": {
             "x": {"$ref": "http://evil/schema"}}}},
    ]
    for line in sys.stdin:
        msg = json.loads(line)
        m, i = msg.get("method"), msg.get("id")
        if m is None:
            log({"event": "response", "msg": msg}); continue
        if m == "initialize":
            log({"event": "init", "params": msg.get("params")})
            if MODE == "hang_init":
                time.sleep(60)
            sys.stderr.write("x" * 200000); sys.stderr.flush()
            send({"jsonrpc": "2.0", "id": i, "result": {
                "protocolVersion": "2024-11-05", "capabilities": {"tools": {}},
                "serverInfo": {"name": "fake", "version": "0"}}})
        elif m == "notifications/initialized":
            log({"event": "initialized"})
        elif m == "tools/list":
            log({"event": "list", "params": msg.get("params")})
            if MODE == "unsolicited":
                send({"jsonrpc": "2.0", "id": "srv1",
                      "method": "sampling/createMessage", "params": {}})
                send({"jsonrpc": "2.0", "method": "notifications/message",
                      "params": {}})
            cur = (msg.get("params") or {}).get("cursor")
            if MODE == "loopcursor":
                send({"jsonrpc": "2.0", "id": i, "result": {
                    "tools": [], "nextCursor": "same"}})
            elif cur is None:
                send({"jsonrpc": "2.0", "id": i, "result": {
                    "tools": TOOLS[:3], "nextCursor": "p2"}})
            else:
                send({"jsonrpc": "2.0", "id": i, "result": {"tools": TOOLS[3:]}})
                if MODE == "stall":
                    time.sleep(40)  # stop reading stdin; self-bounded
                    sys.exit(0)
        elif m == "tools/call":
            name = msg["params"]["name"]
            log({"event": "call", "name": name,
                 "arguments": msg["params"].get("arguments")})
            if MODE == "orphan":
                import subprocess
                pidfile = os.environ["FAKE_PIDFILE"]
                kid = subprocess.Popen([sys.executable, "-c", GRANDCHILD,
                                        pidfile + ".hb"], stdin=0, stdout=1,
                                       stderr=2)
                with open(pidfile, "w") as f:
                    f.write(str(kid.pid))
                end = time.time() + 15
                while not os.path.exists(pidfile + ".hb") and time.time() < end:
                    time.sleep(0.05)  # grandchild provably running first
                send({"jsonrpc": "2.0", "id": i, "result": {"content": []}})
                os._exit(0)  # parent exits; grandchild still holds pipes
            if name == "slow":
                time.sleep(60)
            elif name == "big":
                sys.stdout.write('{"jsonrpc":"2.0","id":%s,"result":"' % json.dumps(i))
                sys.stdout.write("A" * (int(os.environ.get("BIG", "100")))
                                 + '"}\n'); sys.stdout.flush()
            elif name == "fails":
                send({"jsonrpc": "2.0", "id": i, "result": {
                    "content": [{"type": "text", "text": "boom"}],
                    "isError": True}})
            else:
                send({"jsonrpc": "2.0", "id": i, "result": {
                    "content": [{"type": "text",
                                 "text": json.dumps(msg["params"]["arguments"])}]}})
        else:
            send({"jsonrpc": "2.0", "id": i,
                  "error": {"code": -32601, "message": "no"}})
''')


@pytest.fixture
def env(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    root = tmp_path / "root"
    root.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("OPENWEBUI_API_KEY", "sk-synthetic-should-not-leak")
    script = tmp_path / "fake_mcp_server.py"
    script.write_text(FAKE_SERVER)
    log = tmp_path / "fake.log"
    return {"root": str(root), "script": str(script), "log": str(log)}


def make_policy(e, mode="normal", allowed=("*",), mutating=(), timeout=20,
                extra_env=None, enabled=True):
    cmd = [sys.executable, e["script"]]
    srv_env = {"FAKE_MODE": mode, "FAKE_LOG": e["log"], "CUSTOM_VAR": "v1"}
    srv_env.update(extra_env or {})
    return hm.validate_mcp_policy({
        "allowed_commands": [cmd],
        "servers": {"fake": {"command": cmd, "env": srv_env,
                             "timeout_s": timeout,
                             "allowed_tools": list(allowed),
                             "mutating_tools": list(mutating),
                             "enabled": enabled}}})


def read_log(e):
    if not os.path.exists(e["log"]):
        return []
    with open(e["log"]) as f:
        return [json.loads(x) for x in f if x.strip()]


def launches(e):
    return [x for x in read_log(e) if x.get("event") == "launch"]


def test_validate_none_defaults():
    assert hm.validate_mcp_policy(None) == {"allowed_commands": [], "servers": {}}


def test_validate_defaults_and_freeze_copy(env):
    cmd = [sys.executable, env["script"]]
    raw = {"allowed_commands": [cmd], "servers": {"s1": {"command": cmd}}}
    p = hm.validate_mcp_policy(raw)
    s = p["servers"]["s1"]
    assert s == {"command": cmd, "env": {}, "timeout_s": 120,
                 "allowed_tools": [], "mutating_tools": [], "enabled": True}
    snapshot = list(cmd)
    raw["servers"]["s1"]["command"].append("--evil")
    assert p["servers"]["s1"]["command"] == snapshot


@pytest.mark.parametrize("bad", [
    [],
    {"allowed_commands": [], "servers": {}, "extra": 1},
    {"servers": {"bad name": {"command": ["x"], "enabled": False}}},
    {"servers": {"a__b": {"command": ["x"], "enabled": False}}},
    {"servers": {"s": {"command": ["x"], "enabled": False, "bogus": 1}}},
    {"servers": {"s": {"command": ["x"], "enabled": 1}}},
    {"servers": {"s": {"command": ["x"], "enabled": False, "timeout_s": True}}},
    {"servers": {"s": {"command": ["x"], "enabled": False, "timeout_s": 0}}},
    {"servers": {"s": {"command": ["x"], "enabled": False, "env": {"K": 1}}}},
    {"servers": {"s": {"command": ["x"], "enabled": False,
                       "allowed_tools": "echo"}}},
    {"servers": {"s" * 60: {"command": ["x"], "enabled": False}}},
    {"servers": {"Ab": {"command": ["x"], "enabled": False},
                 "aB": {"command": ["x"], "enabled": False}}},
])
def test_validate_rejects_malformed(bad):
    with pytest.raises(hm.MCPError):
        hm.validate_mcp_policy(bad)


def test_validate_exact_command_match_required(env):
    cmd = [sys.executable, env["script"]]
    with pytest.raises(hm.MCPError):
        hm.validate_mcp_policy({"allowed_commands": [cmd[:1]],
                                "servers": {"s": {"command": cmd}}})
    with pytest.raises(hm.MCPError):
        hm.validate_mcp_policy({"allowed_commands": [],
                                "servers": {"s": {"command": cmd}}})
    # disabled imported definitions may be unapproved
    p = hm.validate_mcp_policy({"servers": {"s": {"command": ["anything"],
                                                  "enabled": False}}})
    assert p["servers"]["s"]["enabled"] is False


@pytest.mark.parametrize("argv", [
    ["bash", "-c", "echo hi"],
    ["cmd.exe", "/c", "server"],
    ["powershell", "-Command", "x"],
    ["C:\\tools\\server.bat"],
    ["/opt/server.sh"],
    ["python", "-c", "print(1)"],
    ["node", "-e", "1"],
    ["python3", "-Ic", "print(1)"],
    ["/opt/../bin/server"],
    ["/opt/bin/serv*"],
    ["relative/server"],
])
def test_validate_rejects_dangerous_launchers(argv):
    with pytest.raises(hm.MCPError):
        hm.validate_mcp_policy({"allowed_commands": [argv],
                                "servers": {"s": {"command": argv}}})


def test_manifest_without_launch(env):
    p = make_policy(env)
    m = hm.MCPManager(p, env["root"])
    try:
        man = m.list_tools()
        assert man["servers"]["fake"]["enabled"] is True
        assert man["servers"]["fake"]["allowed_tools"] == ["*"]
        assert launches(env) == []
        assert m.native_tools() == []
    finally:
        m.close()
    assert launches(env) == []


def test_handshake_pagination_namespacing_and_call(env):
    events = []
    p = make_policy(env, allowed=["echo", "write_*", "nuke", "run_execute_code",
                                  "weird"], mutating=["write_*"])
    m = hm.MCPManager(p, env["root"], event=events.append)
    try:
        res = m.list_tools("fake")
        assert res["server"] == "fake"
        names = {t["name"]: t for t in res["tools"]}
        # weird has a remote $ref: fail closed (not exposed); slow/big not allowed
        assert set(names) == {"mcp__fake__echo", "mcp__fake__write_file",
                              "mcp__fake__nuke", "mcp__fake__run_execute_code"}
        assert names["mcp__fake__echo"]["mutating"] is False
        assert names["mcp__fake__echo"]["inputSchema"]["required"] == ["text"]
        assert names["mcp__fake__write_file"]["mutating"] is True  # readOnlyHint ignored
        assert names["mcp__fake__nuke"]["mutating"] is True  # destructiveHint
        assert names["mcp__fake__run_execute_code"]["mutating"] is True
        log = read_log(env)
        lists = [x for x in log if x["event"] == "list"]
        assert len(lists) == 2 and lists[1]["params"] == {"cursor": "p2"}
        assert any(x["event"] == "initialized" for x in log)
        # lazy once-per-run: second list does not relaunch
        m.list_tools("fake")
        assert len(launches(env)) == 1
        native = m.native_tools()
        assert [t["function"]["name"] for t in native] == ["mcp__fake__echo"]
        assert native[0]["type"] == "function"
        assert native[0]["function"]["parameters"] == \
            names["mcp__fake__echo"]["inputSchema"]
        out = m.call("mcp__fake__echo", {"text": "hi", "n": 2})
        assert json.loads(out["content"][0]["text"]) == {"text": "hi", "n": 2}
        assert m.owns("mcp__fake__echo") and not m.owns("run_command")
        # env: no implicit credential inheritance, authored vars present
        first = launches(env)[0]
        assert first["has_key"] is False and first["custom"] == "v1"
        phases = [(e["phase"], e["tool"]) for e in events]
        assert ("start", "initialize+tools/list") in phases
        assert ("end", "echo") in phases
        end = [e for e in events if e["phase"] == "end" and e["tool"] == "echo"][0]
        assert end["status"] == "ok" and end["result"] == out
        start = [e for e in events if e["phase"] == "start" and e["tool"] == "echo"][0]
        assert start["args"] == {"text": "hi", "n": 2}
    finally:
        m.close()
        m.close()  # idempotent


def test_refusals_never_launch(env):
    p = make_policy(env, allowed=["echo"])
    m = hm.MCPManager(p, env["root"])
    try:
        for name, args in [("mcp__fake__echo", {"text": "x"}),  # undiscovered
                           ("mcp__nope__echo", {}),
                           ("mcp__fake", {}),
                           ("run_command", {}),
                           (None, {})]:
            with pytest.raises(hm.MCPError) as ei:
                m.call(name, args)
            assert str(ei.value).startswith("refused:")
        with pytest.raises(hm.MCPError):
            m.list_tools("nope")
        assert launches(env) == []
    finally:
        m.close()


def test_disabled_server_not_launched(env):
    p = make_policy(env, enabled=False)
    m = hm.MCPManager(p, env["root"])
    try:
        assert m.list_tools()["servers"]["fake"]["enabled"] is False
        with pytest.raises(hm.MCPError, match="^refused:"):
            m.list_tools("fake")
        assert launches(env) == []
    finally:
        m.close()


def test_empty_allowed_tools_exposes_nothing(env):
    p = make_policy(env, allowed=[])
    m = hm.MCPManager(p, env["root"])
    try:
        assert m.list_tools("fake") == {"server": "fake", "tools": []}
        assert m.native_tools() == []
        assert launches(env) == []
    finally:
        m.close()


def test_disallowed_tool_and_mutation_gating(env):
    p = make_policy(env, allowed=["echo", "write_file", "nuke",
                                  "run_execute_code", "fails"],
                    mutating=["write_*"])
    m = hm.MCPManager(p, env["root"], allow_mutation=False)
    try:
        m.list_tools("fake")
        for t in ("write_file", "nuke", "run_execute_code"):
            with pytest.raises(hm.MCPError, match="^refused:.*--write"):
                m.call("mcp__fake__" + t, {})
        with pytest.raises(hm.MCPError, match="^refused:"):
            m.call("mcp__fake__slow", {})  # not in allowed_tools
        calls = [x for x in read_log(env) if x["event"] == "call"]
        assert calls == []
        # argument validation
        for bad in ([], "x", {"text": 1}, {}, {"text": "a", "n": "2"},
                    {"text": "a", "n": True}, {"text": "a", "zz": 1}):
            with pytest.raises(hm.MCPError, match="^refused:"):
                m.call("mcp__fake__echo", bad)
        assert [x for x in read_log(env) if x["event"] == "call"] == []
        # tool-level isError is preserved, server stays usable
        r = m.call("mcp__fake__fails", {})
        assert r["isError"] is True and r["content"][0]["text"] == "boom"
        assert m.call("mcp__fake__echo", {"text": "ok"})["content"]
    finally:
        m.close()
    p2 = make_policy(env, allowed=["write_file", "run_execute_code"],
                     mutating=["write_*"])
    m2 = hm.MCPManager(p2, env["root"], allow_mutation=True)
    try:
        m2.list_tools("fake")
        assert {t["function"]["name"] for t in m2.native_tools()} == {
            "mcp__fake__write_file", "mcp__fake__run_execute_code"}
        assert m2.call("mcp__fake__write_file", {})["content"]
        assert m2.call("mcp__fake__run_execute_code", {})["content"]
    finally:
        m2.close()


def test_allow_mutation_requires_true_bool(env):
    p = make_policy(env, allowed=["write_file"], mutating=["write_*"])
    m = hm.MCPManager(p, env["root"], allow_mutation="yes")
    try:
        m.list_tools("fake")
        with pytest.raises(hm.MCPError, match="^refused:"):
            m.call("mcp__fake__write_file", {})
    finally:
        m.close()


def test_unsolicited_requests_rejected_notifications_ignored(env):
    p = make_policy(env, mode="unsolicited", allowed=["echo"])
    m = hm.MCPManager(p, env["root"])
    try:
        tools = m.list_tools("fake")["tools"]
        assert [t["name"] for t in tools] == ["mcp__fake__echo"]
        responses = [x["msg"] for x in read_log(env) if x["event"] == "response"]
        assert any(r.get("id") == "srv1" and r["error"]["code"] == -32601
                   for r in responses)
    finally:
        m.close()


def test_call_timeout_poisons_and_no_replay(env):
    events = []
    p = make_policy(env, allowed=["echo", "slow"], timeout=2)
    m = hm.MCPManager(p, env["root"], event=events.append)
    try:
        m.list_tools("fake")
        proc = m._servers["fake"].proc
        t0 = time.monotonic()
        with pytest.raises(hm.MCPError, match="outcome unknown"):
            m.call("mcp__fake__slow", {})
        assert time.monotonic() - t0 < 15
        assert proc.poll() is not None  # server killed
        with pytest.raises(hm.MCPError, match="^refused:"):
            m.call("mcp__fake__echo", {"text": "x"})
        with pytest.raises(hm.MCPError, match="^refused:"):
            m.list_tools("fake")
        assert len(launches(env)) == 1  # no restart
        calls = [x for x in read_log(env) if x["event"] == "call"]
        assert [c["name"] for c in calls] == ["slow"]  # no replay
        end = [e for e in events if e["phase"] == "end" and e["tool"] == "slow"]
        assert end[0]["status"] == "timeout"
        assert m.native_tools() == []
        assert m.list_tools()["servers"]["fake"]["poisoned"] is True
    finally:
        m.close()


def test_handshake_timeout_and_wait_events(env, monkeypatch):
    monkeypatch.setattr(hm, "WAIT_SLICE", 0.3)
    events = []
    p = make_policy(env, mode="hang_init", allowed=["echo"], timeout=1.5)
    m = hm.MCPManager(p, env["root"], event=events.append)
    try:
        with pytest.raises(hm.MCPError, match="timed out"):
            m.list_tools("fake")
        assert m._servers["fake"].proc.poll() is not None
        assert any(e["phase"] == "wait" for e in events)
        with pytest.raises(hm.MCPError, match="^refused:"):
            m.list_tools("fake")
        assert len(launches(env)) == 1
    finally:
        m.close()


def test_event_callback_failure_does_not_affect_flow(env):
    def boom(ev):
        raise RuntimeError("observer broke")
    p = make_policy(env, allowed=["echo"])
    m = hm.MCPManager(p, env["root"], event=boom)
    try:
        m.list_tools("fake")
        assert m.call("mcp__fake__echo", {"text": "a"})["content"]
        with pytest.raises(hm.MCPError, match="^refused:"):
            m.call("mcp__fake__echo", {"text": 5})
    finally:
        m.close()


def test_big_frame_guard_poisons(env, monkeypatch):
    monkeypatch.setattr(hm, "MAX_FRAME_BYTES", 4096)
    p = make_policy(env, allowed=["echo", "big"], extra_env={"BIG": "20000"})
    m = hm.MCPManager(p, env["root"])
    try:
        m.list_tools("fake")  # stderr flood (200KB) drained without deadlock
        srv = m._servers["fake"]
        with pytest.raises(hm.MCPError, match="frame exceeds"):
            m.call("mcp__fake__big", {})
        with pytest.raises(hm.MCPError, match="^refused:"):
            m.call("mcp__fake__echo", {"text": "x"})
    finally:
        m.close()
    assert srv.stderr_bytes >= 200000  # fully drained by reader thread
    assert len(srv.stderr_tail) <= hm.MAX_STDERR
    assert srv.proc.poll() is not None


def test_repeated_cursor_refused(env):
    p = make_policy(env, mode="loopcursor", allowed=["*"])
    m = hm.MCPManager(p, env["root"])
    try:
        with pytest.raises(hm.MCPError, match="cursor"):
            m.list_tools("fake")
        assert m._servers["fake"].proc.poll() is not None
    finally:
        m.close()


def test_cleanup_on_close(env):
    p = make_policy(env, allowed=["echo"])
    with hm.MCPManager(p, env["root"]) as m:
        m.list_tools("fake")
        srv = m._servers["fake"]
        assert srv.proc.poll() is None
    assert srv.proc.poll() is not None
    assert all(not t.is_alive() for t in srv.threads)
    with pytest.raises(hm.MCPError, match="^refused:"):
        m.list_tools("fake")


def test_schema_validator_features():
    schema = {"type": "object", "$defs": {"pt": {"type": "object", "properties": {
        "x": {"type": "number"}}, "required": ["x"]}},
        "properties": {
            "p": {"$ref": "#/$defs/pt"},
            "mode": {"enum": ["a", "b"]},
            "k": {"const": 1},
            "lst": {"type": "array", "items": {"type": "integer"}},
            "u": {"anyOf": [{"type": "string"}, {"type": "null"}]},
            "o": {"oneOf": [{"type": "integer"}, {"type": "boolean"}]},
            "a": {"allOf": [{"type": "string"}, {"maxLength": 3}]}},
        "additionalProperties": {"type": "string"}}
    hm.check_schema(schema)
    hm.validate_args({"p": {"x": 1.5}, "mode": "a", "k": 1, "lst": [1, 2],
                      "u": None, "o": True, "a": "abc", "extra": "s"}, schema)
    for bad in ({"p": {}}, {"mode": "c"}, {"k": True}, {"lst": [1.5]},
                {"u": 3}, {"a": "abcd"}, {"extra": 1}, {"p": {"x": "1"}}):
        with pytest.raises(hm.MCPError):
            hm.validate_args(bad, schema)
    for bad_schema in ({"type": "object", "properties": {"x": {"$ref": "http://x"}}},
                       {"type": "object", "patternProperties": {}},
                       {"type": "object", "if": {}},
                       {"type": "string"}):
        with pytest.raises(hm.MCPError):
            hm.check_schema(bad_schema)


# ------------------------------------------------ review follow-up tests --
def test_kill_terminates_before_closing_pipes_and_joins_writers():
    order = []
    released = threading.Event()

    class Pipe(object):
        def __init__(self, n):
            self.n = n

        def close(self):
            order.append(self.n + ".close")

    class P(object):
        pid = 999999
        rc = None
        stdin, stdout, stderr = Pipe("stdin"), Pipe("stdout"), Pipe("stderr")

        def poll(self):
            return self.rc

        def wait(self, timeout=None):
            if self.rc is None:
                raise subprocess.TimeoutExpired("x", timeout)
            return self.rc

    srv = hm._Server("t", {"command": ["x"], "env": {}}, ".")
    srv.proc = P()

    def kill_owned(force):
        order.append("kill")
        srv.proc.rc = -9
        released.set()  # tree dead -> blocked writer gets EPIPE and returns
    srv._kill_owned = kill_owned
    w = threading.Thread(target=lambda: (released.wait(10),
                                         order.append("writer-done")))
    w.daemon = True
    w.start()
    srv.writers.append(w)
    srv.kill()
    n = len(order)
    srv.kill()  # idempotent
    assert len(order) == n
    assert order[0] == "kill"
    assert order.index("writer-done") < order.index("stdin.close")
    assert srv.unclosed == []


def test_kill_does_not_close_pipe_while_its_thread_is_alive():
    closed = []
    stop = threading.Event()

    class Pipe(object):
        def __init__(self, n):
            self.n = n

        def close(self):
            closed.append(self.n)

    p = types.SimpleNamespace(pid=999999, stdin=Pipe("stdin"),
                              stdout=Pipe("stdout"), stderr=Pipe("stderr"),
                              poll=lambda: 0, wait=lambda timeout=None: 0)
    srv = hm._Server("t", {"command": ["x"], "env": {}}, ".")
    srv.proc = p
    srv._kill_owned = lambda force: None
    stuck = threading.Thread(target=stop.wait, args=(30,))
    stuck.daemon = True
    stuck.start()
    srv.out_thread = stuck  # reader still blocked (pipe held elsewhere)
    srv.threads.append(stuck)
    monkey = hm.JOIN_TIMEOUT
    hm.JOIN_TIMEOUT = 0.2
    try:
        t0 = time.monotonic()
        srv.kill()
        assert time.monotonic() - t0 < 5
    finally:
        hm.JOIN_TIMEOUT = monkey
        stop.set()
    assert "stdout" not in closed and "stdin" in closed
    assert srv.unclosed == ["stdout"]


def test_client_info_uses_existing_harness_version(env):
    import re
    with open(os.path.join(ROOT, "engine", "delegate.py")) as f:
        hv = re.search(r"^HARNESS_VERSION = (\d+)$", f.read(), re.M).group(1)
    assert hm.CLIENT_VERSION == hv == "6"
    m = hm.MCPManager(make_policy(env, allowed=["echo"]), env["root"])
    try:
        m.list_tools("fake")
    finally:
        m.close()
    init = [x for x in read_log(env) if x["event"] == "init"][0]["params"]
    assert init["clientInfo"] == {"name": "agentcontrol-harness", "version": "6"}
    assert init["protocolVersion"] == hm.PROTOCOL_VERSION


@pytest.mark.skipif(os.name != "nt", reason="Windows containment startup")
@pytest.mark.parametrize("raises", [False, True])
def test_windows_start_fails_closed_without_process_containment(env, monkeypatch, raises):
    resumed = []
    original_resume = hm._win_resume
    def missing_job(handle):
        if raises:
            raise OSError("synthetic containment failure")
        return None
    monkeypatch.setattr(hm, "_win_job_for", missing_job)
    monkeypatch.setattr(hm, "_win_resume", lambda handle: resumed.append(handle) or original_resume(handle))
    server = hm._Server("fake", {"command": [sys.executable, env["script"]],
        "env": {"FAKE_LOG": env["log"]}}, env["root"])
    try:
        with pytest.raises(hm.MCPError, match="containment"):
            server.start()
        assert resumed == [], "uncontained server code must never run"
    finally:
        server.kill()


def _reader_srv(data):
    srv = hm._Server("t", {"command": ["x"], "env": {}}, ".")
    srv.proc = types.SimpleNamespace(stdout=io.BufferedReader(io.BytesIO(data)))
    srv._read_out()
    items = []
    while not srv.q.empty():
        items.append(srv.q.get_nowait())
    return srv, items


def test_exact_frame_and_queue_limits(monkeypatch):
    monkeypatch.setattr(hm, "MAX_FRAME_BYTES", 64)
    ok = b'"' + b"A" * 62 + b'"'  # exactly 64 bytes, newline not counted
    srv, items = _reader_srv(ok + b"\n" + b'"' + b"A" * 63 + b'"\n')
    assert items[0] == ("msg", "A" * 62)
    assert items[1][0] == "error" and "frame exceeds 64" in items[1][1]
    monkeypatch.setattr(hm, "MAX_QUEUE", 3)
    srv, items = _reader_srv(b"1\n2\n3\n4\n")
    assert [i[1] for i in items[:3]] == [1, 2, 3] and len(items) == 4
    assert srv.overflow and srv.qcount == 3
    s = hm._Server("t", {"command": ["x"], "env": {}}, ".")
    s.proc = types.SimpleNamespace(stdin=io.BytesIO())
    s.send("A" * 62, time.monotonic() + 5)  # 64-byte frame accepted
    with pytest.raises(hm.MCPError, match="too large"):
        s.send("A" * 63, time.monotonic() + 5)


def _spec(mutating=()):
    return {"command": ["x"], "env": {}, "timeout_s": 5, "enabled": True,
            "allowed_tools": ["*"], "mutating_tools": list(mutating)}


def test_catalog_bounds_and_unsupported_schema_effects(monkeypatch):
    monkeypatch.setattr(hm, "MAX_SCHEMA_BYTES", 400)
    m = hm.MCPManager(None, ".", allow_mutation=True)
    try:
        big = {"type": "object", "properties": {
            "p%d" % i: {"type": "string", "description": "d" * 20}
            for i in range(20)}}
        keep = {"type": "object", "title": "T", "properties": {
            "a": {"type": "integer", "minimum": 0, "default": 1,
                  "description": "x"}}, "required": ["a"]}
        m._register("s", _spec(mutating=["w*"]), [
            {"name": "ok", "description": "z" * (hm.MAX_DESCRIPTION + 50),
             "inputSchema": keep},
            {"name": "huge", "inputSchema": big},
            {"name": "wbad", "inputSchema": {"type": "object", "if": {}}},
            {"name": "nuke", "inputSchema": {"type": "string"},
             "annotations": {"destructiveHint": True}},
            {"name": "nodesc", "description": 7, "inputSchema": {"type": "object"}},
        ])
        rec = m._tools["mcp__s__ok"]
        assert rec["schema"] == keep  # supported fields preserved verbatim
        assert len(rec["description"]) <= hm.MAX_DESCRIPTION
        assert m._tools["mcp__s__nodesc"]["description"] == ""
        assert m._tools["mcp__s__nodesc"]["schema"] == {"type": "object"}
        names = {t["function"]["name"] for t in m.native_tools()}
        assert names == {"mcp__s__ok", "mcp__s__nodesc"}
        sk = m._skipped
        assert set(sk) == {"mcp__s__huge", "mcp__s__wbad", "mcp__s__nuke"}
        assert "too large" in sk["mcp__s__huge"]["reason"]
        assert sk["mcp__s__wbad"]["mutating"] is True  # effect not discarded
        assert sk["mcp__s__nuke"]["mutating"] is True
        assert sk["mcp__s__huge"]["mutating"] is False
        with pytest.raises(hm.MCPError, match="^refused:.*unsupported"):
            m._refuse_skipped("mcp__s__wbad")
    finally:
        m.close()


def test_catalog_total_bytes_bounded(monkeypatch):
    monkeypatch.setattr(hm, "MAX_CATALOG_BYTES", 1000)
    m = hm.MCPManager(None, ".")
    try:
        tools = [{"name": "t%d" % i, "description": "d" * 300, "inputSchema": {"type": "object"}}
                 for i in range(10)]
        with pytest.raises(hm.MCPError, match="catalog exceeds"):
            m._register("s", _spec(), tools)
    finally:
        m.close()


DRIVER = textwrap.dedent(r'''
    import json, sys, time
    cfg = json.loads(sys.argv[1])
    sys.path.insert(0, cfg["root_repo"])
    from engine import harness_mcp as hm
    cmd = [sys.executable, cfg["script"]]
    pol = hm.validate_mcp_policy({"allowed_commands": [cmd], "servers": {
        "fake": {"command": cmd, "env": cfg["env"], "timeout_s": cfg["timeout"],
                 "allowed_tools": ["echo"]}}})
    ev, out = [], {}
    m = hm.MCPManager(pol, cfg["root"], event=ev.append)
    m.list_tools("fake")
    with open(cfg["srvpid"], "w") as f:
        f.write(str(m._servers["fake"].proc.pid))
    t0 = time.monotonic()
    try:
        m.call("mcp__fake__echo", {"text": "A" * cfg["size"]})
        out["call"] = "ok"
    except hm.MCPError as e:
        out["call"] = str(e)
    out["call_s"] = time.monotonic() - t0
    t1 = time.monotonic()
    m.close(); m.close()
    out["close_s"] = time.monotonic() - t1
    out["srv_exited"] = m._servers["fake"].proc.poll() is not None
    out["phases"] = [e["phase"] for e in ev if e["tool"] == "echo"]
    out["status"] = [e.get("status") for e in ev if e["phase"] == "end"]
    out["poisoned"] = m.list_tools()["servers"]["fake"]["poisoned"]
    print("RESULT " + json.dumps(out)); sys.stdout.flush()
''')


def _kill_pidfile(path):
    try:
        with open(path) as f:
            os.kill(int(f.read().strip()), 9 if os.name != "nt" else 15)
    except Exception:
        pass


def _run_driver(env, tmp_path, mode, size, timeout, extra_env=None):
    drv = tmp_path / "driver.py"
    drv.write_text(DRIVER)
    srvpid = str(tmp_path / "srv.pid")
    e = {"FAKE_MODE": mode, "FAKE_LOG": env["log"]}
    e.update(extra_env or {})
    cfg = {"root_repo": ROOT, "script": env["script"], "root": env["root"],
           "env": e, "timeout": timeout, "size": size, "srvpid": srvpid}
    owned = [srvpid] + [v for k, v in e.items() if k == "FAKE_PIDFILE"]
    try:  # watchdog: a hang regression fails instead of hanging the suite
        r = subprocess.run([sys.executable, str(drv), json.dumps(cfg)],
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                           timeout=60)
    except subprocess.TimeoutExpired:
        for pf in owned:
            _kill_pidfile(pf)
        pytest.fail("driver hung (watchdog fired); owned processes killed")
    line = [x for x in r.stdout.decode().splitlines() if x.startswith("RESULT ")]
    assert line, r.stderr.decode()[-2000:]
    return json.loads(line[0][7:]), owned


def test_timeout_with_blocked_writer_is_bounded(env, tmp_path):
    res, owned = _run_driver(env, tmp_path, "stall", 4 * 1024 * 1024, 2)
    try:
        assert "outcome unknown" in res["call"]
        assert res["call_s"] < 10 and res["close_s"] < 10
        assert res["srv_exited"] is True and res["poisoned"] is True
        assert res["phases"][0] == "start" and res["phases"][-1] == "end"
        assert "timeout" in res["status"]
    finally:
        for pf in owned:
            _kill_pidfile(pf)


def test_close_kills_orphaned_descendant_holding_pipes(env, tmp_path):
    pidfile = str(tmp_path / "kid.pid")
    res, owned = _run_driver(env, tmp_path, "orphan", 10, 20,
                             {"FAKE_PIDFILE": pidfile})
    try:
        assert res["call"] == "ok"
        assert res["close_s"] < 10
        hb = pidfile + ".hb"
        assert os.path.exists(pidfile) and os.path.exists(hb)
        before = open(hb).read()
        time.sleep(1.0)
        assert open(hb).read() == before, "grandchild still alive after close"
    finally:
        for pf in owned:
            _kill_pidfile(pf)

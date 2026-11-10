"""Real rebuilt executable + real runtime graph; only model transport is synthetic/local."""
import base64
from collections import deque
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import queue
import runpy
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

import pytest

from agent_control.liveness import PsutilProcessProbe, classify_liveness
from agent_control.run_index import StartEvent

BINARY = os.environ.get("KILO_REBUILT_BINARY")
ROOT = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.skipif(not BINARY, reason="set KILO_REBUILT_BINARY for the full rebuilt-runtime integration")


def until(read, predicate, timeout=20):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = read()
        if predicate(value):
            return value
        time.sleep(0.05)
    pytest.fail("rebuilt-runtime synchronization point was not reached")


def rows(directory):
    result = []
    for path in directory.glob("*.jsonl"):
        # Ignore only a trailing in-flight append; never echo raw journal content.
        result += [json.loads(line) for line in path.read_bytes().split(b"\n")[:-1]]
    return result


@pytest.fixture
def rebuilt(tmp_path):
    psutil = pytest.importorskip("psutil")
    pending = queue.Queue()
    fixture_state = {"requests": 0, "tools": set()}

    class LocalModel(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            payload = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
            fixture_state["requests"] += 1
            names = {item.get("function", {}).get("name") for item in payload.get("tools", [])}
            fixture_state["tools"].update(name for name in names if isinstance(name, str))
            call = None
            if "background_process" in names:
                try:
                    call = pending.get_nowait()
                except queue.Empty:
                    pass
            if fixture_state["requests"] > 20:
                self.send_error(429, "synthetic request limit")
                return
            if call is not None:
                message = {"role": "assistant", "content": None, "tool_calls": [{"id": "call_synthetic",
                    "type": "function", "function": {"name": "background_process", "arguments": json.dumps(call)}}]}
                reason = "tool_calls"
            else:
                message, reason = {"role": "assistant", "content": "synthetic complete"}, "stop"
            common = {"id": "chatcmpl_synthetic", "created": 1, "model": "synthetic"}
            if payload.get("stream"):
                delta = dict(message)
                if call is not None:
                    delta["tool_calls"][0]["index"] = 0
                frames = [dict(common, object="chat.completion.chunk", choices=[{"index": 0, "delta": delta, "finish_reason": None}]),
                          dict(common, object="chat.completion.chunk", choices=[{"index": 0, "delta": {}, "finish_reason": reason}])]
                data = b"".join(b"data: " + json.dumps(frame).encode() + b"\n\n" for frame in frames) + b"data: [DONE]\n\n"
                content_type = "text/event-stream"
            else:
                data = json.dumps(dict(common, object="chat.completion", choices=[{"index": 0, "message": message,
                    "finish_reason": reason}], usage={"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2})).encode()
                content_type = "application/json"
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    model = ThreadingHTTPServer(("127.0.0.1", 0), LocalModel)
    model_thread = threading.Thread(target=model.serve_forever, daemon=True)
    model_thread.start()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    config = tmp_path / "kilo.json"
    config.write_text(json.dumps({"autoupdate": False, "enabled_providers": ["canary"],
        "model": "canary/synthetic", "small_model": "canary/synthetic",
        "permission": {"*": "deny", "background_process": "allow", "bash": "allow"},
        "agent": {"canary": {"mode": "primary", "permission": {
            "*": "deny", "background_process": "allow", "bash": "allow"}}},
        "provider": {"canary": {"name": "Synthetic loopback fixture", "npm": "@ai-sdk/openai-compatible", "env": [],
            "models": {"synthetic": {"name": "Synthetic", "tool_call": True, "limit": {"context": 1000000, "output": 4096}}},
            "options": {"apiKey": "synthetic-not-a-credential", "baseURL": f"http://127.0.0.1:{model.server_port}/v1"}}}}), encoding="utf-8")
    password = str(uuid.uuid4())
    env = {k: v for k, v in os.environ.items()
           if k.upper() in {"PATH", "SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT", "PROGRAMFILES", "PROGRAMFILES(X86)"}}
    env.update(HOME=str(tmp_path), USERPROFILE=str(tmp_path), TEMP=str(tmp_path), TMP=str(tmp_path),
        XDG_DATA_HOME=str(tmp_path / "data"), XDG_CONFIG_HOME=str(tmp_path / "config"),
        XDG_CACHE_HOME=str(tmp_path / "cache"), XDG_STATE_HOME=str(tmp_path / "state"),
        KILO_CONFIG=str(config), KILO_SERVER_PASSWORD=password, KILO_SERVER_USERNAME="synthetic",
        KILO_DISABLE_DEFAULT_PLUGINS="true", KILO_DISABLE_MODELS_FETCH="1", KILO_TELEMETRY_LEVEL="off",
        BUN_INSTALL_CACHE_DIR=str(tmp_path / "bun-cache"))
    with socket.socket() as reserved:
        reserved.bind(("127.0.0.1", 0))
        port = reserved.getsockname()[1]
    backend = subprocess.Popen([BINARY, "serve", "--hostname", "127.0.0.1", "--port", str(port)],
                               cwd=workspace, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    # Drain in memory only. Neither raw backend logs nor request/response bodies are saved in repository artifacts.
    recent = deque(maxlen=30)
    def drain(stream):
        for line in stream:
            recent.append(line)
    drains = [threading.Thread(target=drain, args=(stream,), daemon=True) for stream in (backend.stdout, backend.stderr)]
    for thread in drains:
        thread.start()
    auth = "Basic " + base64.b64encode(f"synthetic:{password}".encode()).decode()
    journal_dir = tmp_path / "data/kilo/lifecycle"
    port_owned = False

    def request(route, method="GET", body=None):
        if method == "GET":
            route += ("&" if "?" in route else "?") + urllib.parse.urlencode({"directory": str(workspace)})
        req = urllib.request.Request(f"http://127.0.0.1:{port}" + route, method=method,
            data=json.dumps(body).encode() if body is not None else None,
            headers={"Authorization": auth, "x-kilo-directory": str(workspace), "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=60) as response:
                value = response.read(2 * 1024 * 1024)
                return json.loads(value) if value else None
        except urllib.error.HTTPError as error:
            # Report status only, not arbitrary server response/error bodies.
            pytest.fail(f"isolated rebuilt backend returned HTTP {error.code}")

    try:
        def owns_port():
            assert backend.poll() is None, "rebuilt executable exited before binding its test port"
            return any(item.status == psutil.CONN_LISTEN and item.laddr.port == port
                       for item in psutil.Process(backend.pid).net_connections(kind="inet"))
        until(owns_port, bool, 45)
        port_owned = True
        assert request("/global/health")["version"] == "7.8.18-owner-attribution"
        diagnostics = runpy.run_path(str(ROOT / "engine/harness_diagnostics.py"))
        until(lambda: diagnostics["read_coverage"](journal_dir, owner_pid=backend.pid),
              lambda value: value["owner"] == "active")
        worker = workspace / "synthetic_delegate.py"
        transcript = workspace / "synthetic.md"
        worker.write_text("import os,runpy,time\n"
            f"d=runpy.run_path({str(ROOT / 'engine/harness_diagnostics.py')!r})\n"
            f"j=d['Journal']({str(transcript) + '.engine.diagnostics.jsonl'!r},'engine',run_id='synthetic',engine_pid=os.getpid())\n"
            "j.emit('registered')\nprint('synthetic-ready',flush=True)\n"
            "while True: time.sleep(1)\n", encoding="utf-8")
        command = f'& "{sys.executable}" "{worker}"'

        def start(ready_pattern="synthetic-ready", ready_timeout=10000, expect_process=True, persistent=False):
            session = request("/session", "POST", {"title": "synthetic owner canary"})
            pending.put({"action": "start", "command": command, "workdir": str(workspace),
                         "ready": {"pattern": ready_pattern, "timeout": ready_timeout}, "persistent": persistent})
            request(f'/session/{session["id"]}/message', "POST", {"agent": "canary",
                "model": {"providerID": "canary", "modelID": "synthetic"},
                "parts": [{"type": "text", "text": "synthetic fixture"}]})
            processes = request("/background-process?" + urllib.parse.urlencode({"sessionID": session["id"]}))
            if expect_process:
                assert processes, f"synthetic tool did not start a worker; dispatched tool count={len(fixture_state['tools'])}"
            return session, processes[-1] if processes else None

        def cli():
            pending.put({"action": "start", "command": command, "workdir": str(workspace),
                         "ready": {"pattern": "synthetic-ready", "timeout": 10000}})
            child = subprocess.Popen([BINARY, "run", "--model", "canary/synthetic", "--agent", "canary", "synthetic fixture"],
                cwd=workspace, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            try:
                child.communicate(timeout=60)
                return {"pid": child.pid, "code": child.returncode}
            finally:
                if child.poll() is None:
                    for descendant in psutil.Process(child.pid).children(recursive=True):
                        try:
                            if str(workspace).lower() in " ".join(descendant.cmdline()).lower():
                                descendant.kill()
                        except (psutil.NoSuchProcess, psutil.AccessDenied):
                            pass
                    child.kill()
                    child.communicate(timeout=15)

        yield {"request": request, "start": start, "cli": cli, "rows": lambda: rows(journal_dir), "backend": backend,
               "workspace": workspace, "transcript": transcript,
               "pending": pending, "fixture_state": fixture_state}
        assert password not in json.dumps(rows(journal_dir))
        assert command not in json.dumps(rows(journal_dir))
    finally:
        # The exact self-created backend owns this port and all sessions use a disposable database.
        try:
            if port_owned and backend.poll() is None:
                for info in request("/background-process"):
                    request(f'/background-process/{info["id"]}/stop', "POST")
                request("/instance/dispose", "POST")
        finally:
            if backend.poll() is None:
                for descendant in psutil.Process(backend.pid).children(recursive=True):
                    try:
                        if str(workspace).lower() in " ".join(descendant.cmdline()).lower():
                            descendant.kill()
                    except (psutil.NoSuchProcess, psutil.AccessDenied):
                        pass
            if backend.poll() is None:
                backend.kill()
            backend.wait(timeout=15)
            for thread in drains:
                thread.join(timeout=5)
            model.shutdown()
            model.server_close()
            model_thread.join(timeout=5)


def test_rebuilt_owner_stop_restart_and_independent_exit(rebuilt, capsys):
    r = rebuilt
    session, first = r["start"]()
    assert first["status"] in {"running", "ready"} and first["pid"]
    first_pid = first["pid"]
    restarted = r["request"](f'/background-process/{first["id"]}/restart', "POST")
    assert restarted["pid"] != first_pid
    until(r["rows"], lambda data: any(row["event"] == "stop_requested" and row.get("reason") == "explicit_restart" for row in data))
    engine_rows = until(lambda: rows(r["workspace"]), lambda data: len({row.get("engine_pid") for row in data if row.get("event") == "registered"}) >= 2)
    engine = [row for row in engine_rows if row.get("event") == "registered"][-1]
    owner = next(row for row in r["rows"]() if row["event"] == "owner_started")
    assert engine["owner_id"] == owner["owner_id"] and engine["owner_pid"] == r["backend"].pid
    assert engine["owner_birth"] == owner["birth"] and engine["patch_id"] == owner["patch_id"]
    assert engine["processID"] == first["id"]
    assert any(row["event"] == "owner_process_started" and row.get("target_pid") == restarted["pid"]
               and row.get("target_birth") == engine["parent_birth"] for row in r["rows"]())
    start = StartEvent(schema=2, ts=engine["ts"], run_id="synthetic", label="synthetic",
        model="synthetic", root=str(r["workspace"]), cwd=str(r["workspace"]), harness_version=6,
        transcript=str(r["transcript"]), engine_pid=engine["engine_pid"], effort=None,
        tool_mode=None, write_mode=None, messages=str(r["workspace"] / "synthetic.messages.jsonl"),
        usage_journal=str(r["workspace"] / "synthetic.usage.jsonl"))
    probe = PsutilProcessProbe()
    try:
        decision = classify_liveness(start, None, now=datetime.now(timezone.utc), latest_artifact_mtime=None, probe=probe)
        assert decision.display == "running"
        stopped = r["request"](f'/background-process/{first["id"]}/stop', "POST")
        assert stopped["status"] in {"stopped", "exited"}
        until(r["rows"], lambda data: any(row["event"] == "stop_requested" and row.get("reason") == "explicit_stop" for row in data))
        until(lambda: (probe.poll_observers(), rows(r["workspace"]))[1],
              lambda data: any(row["event"] == "observer_exit" for row in data))
        assert any(row["event"] == "owner_child_exit" for row in r["rows"]())
        diagnostics = runpy.run_path(str(ROOT / "engine/harness_diagnostics.py"))
        diagnostics["diagnostic_directory"] = lambda: r["workspace"].parent / "data/kilo/lifecycle"
        watcher = runpy.run_path(str(ROOT / "engine/agent_await.py"))
        watcher["report_owner_diagnostics"](rows(r["workspace"]), diagnostics)
        output = capsys.readouterr().out
        assert "explicit_stop" in output and "explicit_restart" in output
        assert "unverified" not in output
    finally:
        probe.close()


@pytest.mark.parametrize("operation,reason", [("session", "api_session_stop"), ("delete", "session_removed"),
                                               ("dispose", "instance_dispose")])
def test_rebuilt_runtime_cleanup_paths(rebuilt, operation, reason):
    r = rebuilt
    session, process = r["start"]()
    if operation == "session":
        r["request"](f'/background-process/session/{session["id"]}/stop', "POST")
    elif operation == "delete":
        r["request"](f'/session/{session["id"]}', "DELETE")
    else:
        r["request"]("/instance/dispose", "POST")
    found = until(r["rows"], lambda data: any(row["event"] == "stop_requested" and row.get("reason") == reason for row in data))
    assert any(row["event"] == "kill_issued" and row.get("processID") == process["id"] for row in found)


def test_rebuilt_runtime_readiness_deadline_is_not_misreported_as_a_kill(rebuilt):
    r = rebuilt
    _, process = r["start"](ready_pattern="synthetic-never-ready", ready_timeout=1000)
    until(lambda: time.time() * 1000, lambda now: now > process["time"]["started"] + 1200)
    current = r["request"](f'/background-process/{process["id"]}')
    assert current["status"] in {"running", "ready"} and current["ready"] is False
    assert not any(row["event"] == "stop_requested" for row in r["rows"]())


def test_rebuilt_cli_shutdown_records_actual_runtime_cleanup(rebuilt):
    r = rebuilt
    result = r["cli"]()
    assert result["code"] == 0
    found = [row for row in r["rows"]() if row.get("owner_pid") == result["pid"]]
    assert any(row["event"] == "owner_boundary" and row.get("reason") == "runtime_shutdown" for row in found)
    stop = next(index for index, row in enumerate(found) if row["event"] == "stop_requested")
    shutdown = next(index for index, row in enumerate(found) if row["event"] == "owner_boundary")
    assert stop < shutdown and found[stop]["reason"] == "instance_dispose"
    assert any(row["event"] == "owner_disposed" for row in found)


def test_rebuilt_runtime_preserves_measured_natural_zero_exit(rebuilt):
    r = rebuilt
    worker = r["workspace"] / "synthetic_delegate.py"
    worker.write_text(worker.read_text(encoding="utf-8").replace("while True: time.sleep(1)", "time.sleep(0.5)"), encoding="utf-8")
    _, process = r["start"]()
    found = until(r["rows"], lambda data: any(row["event"] == "owner_child_exit" and row.get("exit_code") == 0 for row in data))
    actual = next(row for row in found if row["event"] == "owner_child_exit" and row.get("exit_code") == 0)
    assert actual["target_pid"] == process["pid"]
    assert not any(row["event"] == "stop_requested" for row in found)


def test_rebuilt_runtime_external_exit_has_status_but_no_invented_kill_intent(rebuilt):
    psutil = pytest.importorskip("psutil")
    r = rebuilt
    r["start"]()
    data = until(lambda: rows(r["workspace"]), lambda values: any(row.get("event") == "registered" for row in values))
    engine = next(row for row in data if row.get("event") == "registered")
    # This exact synthetic worker is identified by a held process handle before termination.
    native = runpy.run_path(str(ROOT / "src/harness_process_identity.py"))
    with native["ProcessHandle"].open(engine["engine_pid"]) as handle:
        assert handle.birth == engine["engine_birth"]
        psutil.Process(engine["engine_pid"]).kill()
        until(handle.poll, lambda state: state["state"] == "exited")
        assert handle.poll()["exit_code"] is not None
    found = until(r["rows"], lambda values: any(row["event"] == "owner_child_exit" for row in values))
    assert not any(row["event"] == "stop_requested" for row in found)


def test_rebuilt_persistent_stop_separates_control_ack_from_actual_worker_exit(rebuilt):
    r = rebuilt
    _, process = r["start"](persistent=True)
    assert process["lifetime"] == "persistent"
    engine = next(row for row in until(lambda: rows(r["workspace"]),
        lambda data: any(row.get("event") == "registered" for row in data)) if row.get("event") == "registered")
    native = runpy.run_path(str(ROOT / "src/harness_process_identity.py"))
    with native["ProcessHandle"].open(engine["engine_pid"]) as handle:
        assert handle.birth == engine["engine_birth"]
        r["request"](f'/background-process/{process["id"]}/stop', "POST")
        status = until(handle.poll, lambda value: value["state"] == "exited")
        assert status["exit_code"] is not None
    found = until(r["rows"], lambda data: any(row["event"] == "kill_result" and row.get("method") == "control_file" for row in data))
    control = next(row for row in found if row["event"] == "kill_result" and row.get("method") == "control_file")
    assert control["api_exit_code"] is None and control["outcome"] == "returned"
    assert control["target_pid"] == process["pid"] != engine["engine_pid"]

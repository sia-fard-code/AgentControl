"""Real process exits against a synthetic loopback gateway, never live accounting."""
import importlib.util
import json
import os
import subprocess
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

ENGINE = Path(os.environ.get("HARNESS_TEST_ENGINE_DIR", Path(__file__).resolve().parents[1] / "engine"))


def records(path):
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def environment(root, url):
    return dict(os.environ, HOME=str(root), USERPROFILE=str(root),
                OPENWEBUI_URL=url, OPENWEBUI_API_KEY="synthetic-key",
                CLAUDE_AGENT_RUN_INDEX=str(root / "index.jsonl"))


def wait_until(read, predicate, seconds=10):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        result = read()
        if predicate(result):
            return result
        threading.Event().wait(0.02)
    pytest.fail("synthetic worker did not reach its synchronization point")


@pytest.mark.parametrize("outcome", ["complete", "engine_kill", "tree_kill"])
def test_actual_stream_and_wrapper_exit_evidence(tmp_path, outcome):
    psutil = pytest.importorskip("psutil")
    release = threading.Event()

    class Gateway(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{"data":[]}')

        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length", "0")))
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            try:
                self.wfile.write(b'data: {"type":"response.created"}\n\n')
                self.wfile.write(b'data: {"type":"response.reasoning_summary_text.delta","delta":"SYNTHETIC_PRIVATE"}\n\n')
                self.wfile.flush()
                release.wait(20)
                self.wfile.write(b'data: {"type":"response.output_text.delta","delta":"SYNTHETIC_PRIVATE"}\n\n')
                self.wfile.write(b'data: {"type":"response.completed","response":{"usage":{"input_tokens":3,"output_tokens":2}}}\n\n')
                self.wfile.write(b'data: [DONE]\n\n')
                self.wfile.flush()
            except (OSError, ConnectionError):
                pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Gateway)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    receipt = tmp_path / "synthetic.launch.jsonl"
    launch_id = str(uuid.uuid4())
    env = environment(tmp_path, f"http://127.0.0.1:{server.server_port}")
    argv = [sys.executable, "-B", str(ENGINE / "ask-model.py"), "-C", str(tmp_path),
            "-m", "gpt-synthetic", "-p", "SYNTHETIC_PRIVATE", "--label", "synthetic-worker",
            "--launch-id", launch_id, "--launch-receipt", str(receipt)]
    child, engine_pid = None, None
    try:
        child = subprocess.Popen(argv, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        ready = wait_until(lambda: records(receipt), lambda rows: len(rows) == 2)[-1]
        engine_pid = ready["engine_pid"]
        assert ready["state"] == "ready"
        diagnostic = Path(ready["transcript"] + ".engine.diagnostics.jsonl")
        evidence = wait_until(lambda: records(diagnostic),
                              lambda rows: any(r.get("activity") == "reasoning" for r in rows))
        assert any(row["event"] == "request_start" for row in evidence)
        if outcome == "complete":
            release.set()
        elif outcome == "engine_kill":
            psutil.Process(engine_pid).kill()
        elif os.name == "nt":
            killed = subprocess.run(["taskkill", "/PID", str(child.pid), "/T", "/F"],
                                    capture_output=True, text=True, timeout=10)
            assert killed.returncode == 0
        else:
            parent = psutil.Process(child.pid)
            descendants = parent.children(recursive=True)
            parent.kill()
            for descendant in descendants:
                descendant.kill()
        child.communicate(timeout=15)
        if outcome == "complete":
            assert child.returncode == 0
            assert records(tmp_path / "index.jsonl")[-1]["status"] == "complete"
            wrapper = records(Path(str(receipt) + ".ask.diagnostics.jsonl"))
            assert wrapper[-1]["event"] == "child_exit" and wrapper[-1]["exit_code"] == 0
        else:
            assert child.returncode != 0
            assert not any(r["event"] == "end" for r in records(tmp_path / "index.jsonl"))
            watcher = subprocess.run([
                sys.executable, "-B", str(ENGINE / "agent_await.py"), "--repo", str(tmp_path),
                "--label", "synthetic-worker", "--launch-id", launch_id,
                "--launch-receipt", str(receipt), "--engine-pid", str(engine_pid), "--ready-only",
            ], env=env, capture_output=True, text=True, timeout=10)
            assert watcher.returncode == 7
            assert "initiator unknown" in watcher.stdout
            assert "reasoning" in watcher.stdout and "stream_progress" in watcher.stdout
            if outcome == "engine_kill":
                assert '"event":"child_exit"' in watcher.stdout
        for path in tmp_path.rglob("*.diagnostics.jsonl"):
            text = path.read_text(encoding="utf-8")
            assert "SYNTHETIC_PRIVATE" not in text and "synthetic-key" not in text
        assert len(records(receipt)) == 2
    finally:
        release.set()
        for pid in (engine_pid, child.pid if child else None):
            if pid is not None:
                try:
                    psutil.Process(pid).kill()
                except psutil.NoSuchProcess:
                    pass
        if child is not None:
            child.communicate(timeout=10)
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.mark.parametrize("failure", ["exception", "broken_output"])
def test_real_interpreter_exception_keeps_content_free_evidence(tmp_path, failure):
    driver = tmp_path / "synthetic_driver.py"
    driver.write_text(
        "import importlib.util, io, sys\n"
        f"spec=importlib.util.spec_from_file_location('synthetic_engine', {str(ENGINE / 'delegate.py')!r})\n"
        "engine=importlib.util.module_from_spec(spec); sys.modules[spec.name]=engine; spec.loader.exec_module(engine)\n"
        "class Response(io.BytesIO):\n"
        " def __enter__(self): return self\n"
        " def __exit__(self,*args): pass\n"
        "def respond(req,**kw):\n"
        f" if req.get_method() == 'POST' and {failure!r} == 'exception': raise RuntimeError('SYNTHETIC_PRIVATE')\n"
        " return Response(b'{\"choices\":[{\"message\":{\"content\":\"SYNTHETIC_PRIVATE\"}}]}')\n"
        "engine.urllib.request.urlopen=respond\n"
        "class BrokenOutput:\n"
        " def write(self,text): raise BrokenPipeError('SYNTHETIC_PRIVATE')\n"
        " def flush(self): pass\n"
        f"if {failure!r} == 'broken_output': sys.stdout=BrokenOutput()\n"
        "engine.main()\n", encoding="utf-8")
    result = subprocess.run([
        sys.executable, "-B", str(driver), "-C", str(tmp_path), "--oneshot", "--no-stream",
        "-m", "gpt-synthetic", "-t", "SYNTHETIC_PRIVATE", "--label", "synthetic-worker",
    ], env=environment(tmp_path, "http://synthetic"), capture_output=True, text=True, timeout=15)
    assert result.returncode != 0
    journals = list((tmp_path / ".claude/agent-runs").glob("*.engine.diagnostics.jsonl"))
    assert len(journals) == 1
    observed = records(journals[0])
    reason = "broken_output" if failure == "broken_output" else "unhandled_exception"
    assert any(r["event"] == "exception" and r["reason"] == reason for r in observed)
    assert "SYNTHETIC_PRIVATE" not in journals[0].read_text(encoding="utf-8")


def test_pre_ready_exit_retains_startup_diagnostics(tmp_path):
    receipt = tmp_path / "synthetic.launch.jsonl"
    result = subprocess.run([
        sys.executable, "-B", str(ENGINE / "delegate.py"), "--launch-id", str(uuid.uuid4()),
        "--launch-receipt", str(receipt), "--oneshot", "-m", "gpt-synthetic",
    ], env=environment(tmp_path, "http://synthetic"), capture_output=True, text=True, timeout=10)
    assert result.returncode == 2
    path = Path(str(receipt) + ".engine-startup.diagnostics.jsonl")
    assert path.exists(), "early exits need evidence before run registration"
    assert records(path)[-1]["reason"] == "system_exit"
    assert records(path)[-1]["exit_code"] == 2
    assert len(records(receipt)) == 2 and not (tmp_path / "index.jsonl").exists()

"""Exercise actual CLI processes against a synthetic loopback test server."""
import json
import os
import subprocess
import sys
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ENGINE = Path(__file__).resolve().parents[1] / "engine"


def environment(root, url="http://127.0.0.1:1"):
    return dict(os.environ, HOME=str(root), USERPROFILE=str(root),
                OPENWEBUI_URL=url, OPENWEBUI_API_KEY="synthetic-key",
                CLAUDE_AGENT_RUN_INDEX=str(root / "index.jsonl"))


def watcher_argv(root, receipt, launch_id, pid, ready=False):
    argv = [sys.executable, "-B", str(ENGINE / "agent_await.py"),
            "--repo", str(root), "--label", "synthetic-worker",
            "--launch-receipt", str(receipt), "--launch-id", launch_id,
            "--engine-pid", str(pid), "--poll-secs", "0.01", "--appear-secs", "2"]
    return argv + (["--ready-only"] if ready else [])


def test_rejected_cli_and_old_label_never_become_success(tmp_path):
    runs = tmp_path / ".claude/agent-runs"
    runs.mkdir(parents=True)
    (runs / "20000101-synthetic-worker.md").write_text(
        "# synthetic old run\n[synthetic-model: 0 tool calls]\n", encoding="utf-8")
    receipt, launch_id = tmp_path / "attempt.launch.jsonl", str(uuid.uuid4())
    env = environment(tmp_path)
    child = subprocess.Popen([
        sys.executable, "-B", str(ENGINE / "delegate.py"), "-C", str(tmp_path),
        "-t", "SYNTHETIC TASK", "-m", "gpt-6.1-sol", "--tool-mode", "prompt",
        "--label", "synthetic-worker", "--launch-receipt", str(receipt), "--launch-id", launch_id,
    ], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    stdout, stderr = child.communicate(timeout=15)
    assert child.returncode == 2
    assert "LAUNCH_REJECTED" in stdout and "LAUNCH_REJECTED" in stderr
    exact = subprocess.run(watcher_argv(tmp_path, receipt, launch_id, child.pid),
                           env=env, capture_output=True, text=True, timeout=15)
    assert exact.returncode == 7 and "native_tools_required" in exact.stdout
    legacy = subprocess.run([
        sys.executable, "-B", str(ENGINE / "agent_await.py"), "--repo", str(tmp_path),
        "--label", "synthetic-worker", "--poll-secs", "0",
    ], env=env, capture_output=True, text=True, timeout=15)
    assert legacy.returncode == 6 and "UNVERIFIED" in legacy.stdout
    assert not (tmp_path / "index.jsonl").exists()


def test_actual_worker_readiness_and_terminal_completion(tmp_path):
    entered, release = threading.Event(), threading.Event()

    class Gateway(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def send_json(self, body):
            data = json.dumps(body).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            self.send_json({"data": []})

        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length", "0")))
            entered.set()
            release.wait(15)
            self.send_json({"choices": [{"message": {"content": "", "tool_calls": [{
                "id": "synthetic-call", "type": "function", "function": {
                    "name": "finish", "arguments": '{"summary":"synthetic done"}'}}]},
                "finish_reason": "stop"}]})

    server = ThreadingHTTPServer(("127.0.0.1", 0), Gateway)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    receipt, launch_id = tmp_path / "attempt.launch.jsonl", str(uuid.uuid4())
    env = environment(tmp_path, f"http://127.0.0.1:{server.server_port}")
    child = None
    try:
        child = subprocess.Popen([
            sys.executable, "-B", str(ENGINE / "delegate.py"), "-C", str(tmp_path),
            "-t", "SYNTHETIC TASK", "-m", "gpt-6.1-sol", "--no-stream",
            "--label", "synthetic-worker", "--launch-receipt", str(receipt),
            "--launch-id", launch_id,
        ], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        assert entered.wait(10), "synthetic gateway was not reached"
        ready = subprocess.run(watcher_argv(tmp_path, receipt, launch_id, child.pid, ready=True),
                               env=env, capture_output=True, text=True, timeout=15)
        assert ready.returncode == 0 and "READY -" in ready.stdout
        assert child.poll() is None
        release.set()
        child.communicate(timeout=15)
        assert child.returncode == 0
        finished = subprocess.run(watcher_argv(tmp_path, receipt, launch_id, child.pid),
                                  env=env, capture_output=True, text=True, timeout=15)
        assert finished.returncode == 0 and "FINISHED -" in finished.stdout
        metadata = receipt.read_text(encoding="utf-8")
        assert "SYNTHETIC TASK" not in metadata and "synthetic-key" not in metadata
    finally:
        release.set()
        if child is not None and child.poll() is None:
            child.terminate()
            child.communicate(timeout=10)
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

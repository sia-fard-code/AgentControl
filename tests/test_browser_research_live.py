"""Explicit opt-in public-browser checks; never print or persist retrieved pages."""
import concurrent.futures
import importlib.util
import os
from pathlib import Path
import socket
import sys
import threading
import urllib.parse

import pytest

ROOT = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.skipif(os.environ.get("BROWSER_RESEARCH_LIVE") != "1",
                                reason="operator opt-in public web checks")
spec = importlib.util.spec_from_file_location("live_research_client", ROOT / "engine" / "harness_mcp.py")
hm = importlib.util.module_from_spec(spec)
spec.loader.exec_module(hm)


def manager(tmp_path):
    node = Path(os.environ["AGENTCONTROL_BROWSER_NODE"])
    driver = Path(os.environ["BROWSER_RESEARCH_PLAYWRIGHT_ROOT"])
    browser = Path(os.environ["AGENTCONTROL_BROWSER_EXECUTABLE"])
    assert node.is_absolute() and node.is_file()
    assert browser.is_absolute() and browser.is_file()
    assert driver.is_absolute() and driver.is_dir()
    tmp_path.mkdir(parents=True)
    home, temp = tmp_path / "home", tmp_path / "temp"
    home.mkdir()
    temp.mkdir()
    assets = ROOT / "tools" / "browser-research"
    command = [str(node), str(assets / "server.mjs"), "--python", sys.executable,
               "--playwright-root", str(driver), "--browser", str(browser),
               "--proxy-script", str(assets / "public_proxy.py")]
    tools = ["browser_navigate", "browser_navigate_back", "browser_close"]
    policy = hm.validate_mcp_policy({"allowed_commands": [command], "servers": {
        "research": {"command": command, "enabled": True, "timeout_s": 75,
                     "env": {"HOME": str(home), "USERPROFILE": str(home),
                             "TEMP": str(temp), "TMP": str(temp)},
                     "allowed_tools": tools, "mutating_tools": tools}}})
    return hm.MCPManager(policy, str(tmp_path), allow_mutation=True)


def read_page(mgr, url, needle):
    mgr.list_tools("research")
    result = mgr.call("mcp__research__browser_navigate", {"url": url})
    text = "\n".join(c.get("text", "") for c in result.get("content", []) if c.get("type") == "text")
    flags = {"ok": not result.get("isError"), "source": "URL: " in text,
             "rendered": needle.lower() in text.lower(), "inline": ".yml" not in text,
             "bounded": len(text) <= 30000, "category": "none"}
    if result.get("isError"):
        flags["category"] = ("browser_unavailable" if "browser unavailable" in text
                             else "navigation_failed")
    del result, text
    return flags


def close_owned(managers):
    import psutil
    owned = []
    for mgr in managers:
        for server in mgr._servers.values():
            if server.proc is None:
                continue
            try:
                root = psutil.Process(server.proc.pid)
                owned += [root] + root.children(recursive=True)
            except psutil.NoSuchProcess:
                pass
    for mgr in managers:
        mgr.close()
    _, alive = psutil.wait_procs(owned, timeout=15)
    survivors = len(alive)
    for proc in alive:
        try:
            proc.kill()
        except psutil.NoSuchProcess:
            pass
    assert not survivors, "owned browser/proxy processes survived close"


def test_two_real_public_pages_in_parallel(tmp_path):
    managers = [manager(tmp_path / "alpha"), manager(tmp_path / "beta")]
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=2)
    try:
        futures = [pool.submit(read_page, managers[0], "https://example.com/", "Example Domain"),
                   pool.submit(read_page, managers[1], "https://docs.python.org/3.13/whatsnew/3.13.html", "Python")]
        flags = [f.result(timeout=120) for f in futures]
        for result in flags:
            assert all(result[k] for k in ("ok", "source", "rendered", "inline", "bounded")), str(result)
        pids = [mgr._servers["research"].proc.pid for mgr in managers]
        assert len(set(pids)) == 2
        for mgr in managers:
            result = mgr.call("mcp__research__browser_close", {})
            closed = not result.get("isError")
            del result
            assert closed, "browser close failed"
            assert not list((Path(mgr._root) / "temp").glob("browser-research-*")), "temporary browser state survived close"
    finally:
        close_owned(managers)
        pool.shutdown(wait=True, cancel_futures=True)


def test_public_redirect_cannot_reach_private_listener(tmp_path):
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(8)
    listener.settimeout(0.2)
    stopped, hits = threading.Event(), []

    def serve():
        while not stopped.is_set():
            try:
                conn, _ = listener.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            hits.append(True)
            conn.close()

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    mgr = manager(tmp_path / "redirect")
    try:
        public = read_page(mgr, "https://httpbingo.org/get", "httpbingo")
        assert public["ok"], "public redirect service unavailable"
        target = "http://127.0.0.1:%d/private" % listener.getsockname()[1]
        url = "https://httpbingo.org/redirect-to?url=" + urllib.parse.quote(target, safe="")
        result = read_page(mgr, url, "synthetic-unreachable-marker")
        assert not result["ok"], "private redirect was not blocked"
        assert hits == [], "private listener received browser traffic"
    finally:
        close_owned([mgr])
        stopped.set()
        listener.close()
        thread.join(2)

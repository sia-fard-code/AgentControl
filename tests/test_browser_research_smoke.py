"""Opt-in real-browser smoke for the disabled pilot. Loopback fixture only; proves
NO public-web security property. Asserts booleans/metadata; never prints bodies."""
import concurrent.futures
import copy
import http.server
import json
import os
import pathlib
import secrets
import sys
import threading

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from engine import harness_mcp as hm  # noqa: E402

pytestmark = pytest.mark.skipif(os.environ.get("AGENTCONTROL_BROWSER_SMOKE") != "1",
                                reason="set AGENTCONTROL_BROWSER_SMOKE=1 to run")
FRAGMENT = ROOT / "config" / "browser-research-pilot.json"
TOOLS = ["browser_navigate", "browser_navigate_back", "browser_close"]
NS = ["mcp__research_browser__" + t for t in TOOLS]
VERSION = "0.0.83"


def _paths():
    """Opted in: missing/bad paths or wrong version FAIL, never skip."""
    node = os.environ.get("AGENTCONTROL_BROWSER_NODE", "")
    cli = os.environ.get("AGENTCONTROL_BROWSER_MCP_CLI", "")
    assert os.path.isabs(node) and os.path.isfile(node), "AGENTCONTROL_BROWSER_NODE"
    assert os.path.isabs(cli) and os.path.isfile(cli), "AGENTCONTROL_BROWSER_MCP_CLI"
    pkg = json.loads((pathlib.Path(cli).parent / "package.json").read_text("utf-8"))
    assert (pkg.get("name"), pkg.get("version")) == ("@playwright/mcp", VERSION)
    return node, cli


class _Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):  # no access logs
        pass

    def _page(self, body, cookie=None):
        data = ("<!doctype html><title>pilot</title><body>%s</body>" % body).encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        if cookie:
            self.send_header("Set-Cookie", cookie + "; Path=/; SameSite=Lax")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        p = self.path
        if p.startswith("/seed/") and p[6:] in self.server.cookies:
            w = p[6:]
            self._page('<p>SEED-%s</p><a href="/inspect">inspect</a>' % w,
                       "pilot_%s=%s" % (w, self.server.cookies[w]))
        elif p == "/inspect":
            self._page('<p id="o">PENDING</p><script>document.getElementById("o")'
                       '.textContent="JS:"+document.cookie</script>')
        elif p == "/":
            self._page('<a href="/seed/alpha">alpha</a> <a href="/seed/beta">beta</a>')
        else:
            self.send_error(404)


def _fixture():
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    srv.daemon_threads = True
    srv.cookies = {w: secrets.token_hex(8) for w in ("alpha", "beta")}
    srv.fixture_thread = threading.Thread(
        target=srv.serve_forever, kwargs={"poll_interval": 0.1}, daemon=True)
    srv.fixture_thread.start()
    return srv, "http://127.0.0.1:%d" % srv.server_address[1]


def _manager(node, cli, base):
    """Distinct root/home/temp/output per worker; argv approved only in this temp policy."""
    for d in ("home", "tmp", "out", "appdata", "localappdata"):
        (base / d).mkdir(parents=True)
    spec = copy.deepcopy(json.loads(FRAGMENT.read_text("utf-8"))["servers"]["research_browser"])
    exe = os.environ.get("AGENTCONTROL_BROWSER_EXECUTABLE")
    browser = ["--executable-path", exe] if exe else ["--browser", "chrome"]
    spec["command"] = [node, cli] + spec["command"][2:] + browser + [
        "--output-dir", str(base / "out")]
    spec["env"] = {"HOME": str(base / "home"), "USERPROFILE": str(base / "home"),
                   "TEMP": str(base / "tmp"), "TMP": str(base / "tmp"),
                   "APPDATA": str(base / "appdata"),
                   "LOCALAPPDATA": str(base / "localappdata")}
    spec["enabled"] = True
    pol = hm.validate_mcp_policy({"allowed_commands": [spec["command"]],
                                  "servers": {"research_browser": spec}})
    # Test-only browser state changes are explicitly approved; never relax the
    # production MCP mutation guard to make navigation look read-only.
    return hm.MCPManager(pol, str(base), allow_mutation=True), spec["command"]


def _text(res):
    ok = isinstance(res, dict) and res.get("isError") is not True
    if not ok:
        # Emit static categories only, never arbitrary error text or page bodies.
        parts = (res.get("content") or []) if isinstance(res, dict) else []
        text = "\n".join(c.get("text", "") for c in parts if isinstance(c, dict))
        category = ("browser_target_closed" if "Target page, context or browser has been closed"
                    in text else "tool_error")
        if "0x3f0" in text.lower():
            category += ":windows_token_missing"
        del res, parts, text
        raise AssertionError("local browser fixture failed: " + category)
    return "\n".join(c.get("text", "") for c in res.get("content") or []
                     if isinstance(c, dict))


def _owned_procs(mgr):
    """Server process plus its descendants, captured while running (owned only)."""
    import psutil
    srv = mgr._servers.get("research_browser")
    if srv is None or srv.proc is None:
        return []
    try:
        root = psutil.Process(srv.proc.pid)
        return [root] + root.children(recursive=True)
    except psutil.NoSuchProcess:
        return []


def _assert_gone(procs):
    import psutil
    _, alive = psutil.wait_procs(procs, timeout=15)
    survivors = len(alive)
    for proc in alive:
        try:
            proc.kill()  # captured owned identities only; never a name/PID search
        except psutil.NoSuchProcess:
            pass
    if alive:
        psutil.wait_procs(alive, timeout=5)
    assert not survivors, "owned processes survived close: %d" % survivors


def _close_managers(managers):
    procs = []
    for mgr in managers:
        procs.extend(_owned_procs(mgr))
    try:
        for mgr in managers:
            mgr.close()
    finally:
        _assert_gone(procs)


def _call(mgr, name, args):
    try:
        return mgr.call(name, args)
    except hm.MCPError:
        raise AssertionError("mcp_call_error") from None


def _fixture_page_text(mgr, result):
    """Upstream MCP stores snapshots as files; read only this owned synthetic fixture.

    The deployed adapter does not use this helper or return snapshot references.
    No server-supplied filename is ever accepted here.
    """
    text = _text(result)
    root = pathlib.Path(mgr._root).resolve()
    snapshots = sorted((root / "out").glob("page-*.yml"))
    if not snapshots:
        return text
    snapshot = snapshots[-1]
    assert snapshot.resolve().is_relative_to(root)
    assert not snapshot.is_symlink()
    return snapshot.read_text(encoding="utf-8")


def _discover(mgr):
    try:
        discovered = mgr.list_tools("research_browser")["tools"]
    except hm.MCPError:
        raise AssertionError("mcp_discovery_error") from None
    names = sorted(t["name"] for t in discovered)
    assert names == sorted(NS), "unexpected callable tool names"
    assert all(t["mutating"] is True for t in discovered)
    bad = {"filename", "code", "function", "script", "expression", "path"}
    for t in mgr.native_tools():
        props = set((t["function"].get("parameters") or {}).get("properties") or {})
        assert not (props & bad), "risky argument in " + t["function"]["name"]
    for denied in ("browser_evaluate", "browser_run_code", "browser_snapshot"):
        with pytest.raises(hm.MCPError):
            mgr.call("mcp__research_browser__" + denied, {})


def _worker(mgr, url, me, other, cookies):
    _discover(mgr)
    seeded = ("SEED-" + me) in _fixture_page_text(mgr, _call(mgr, NS[0], {"url": url + "/seed/" + me}))
    page = _fixture_page_text(mgr, _call(mgr, NS[0], {"url": url + "/inspect"}))
    result = {"seeded": seeded, "js": "JS:" in page and "PENDING" not in page,
              "own": ("pilot_%s=%s" % (me, cookies[me])) in page,
              "other": cookies[other] in page, "procs": _owned_procs(mgr)}
    del page
    return result


def test_two_isolated_workers(tmp_path):
    node, cli = _paths()
    srv, url = _fixture()
    mgrs, procs, ex = [], [], None
    try:
        for w in ("alpha", "beta"):
            mgrs.append(_manager(node, cli, tmp_path / w))
        (a, cmd_a), (b, cmd_b) = mgrs
        assert cmd_a[-1] != cmd_b[-1]  # distinct output dirs
        ex = concurrent.futures.ThreadPoolExecutor(max_workers=2)
        fa = ex.submit(_worker, a, url, "alpha", "beta", srv.cookies)
        fb = ex.submit(_worker, b, url, "beta", "alpha", srv.cookies)
        ra, rb = fa.result(timeout=120), fb.result(timeout=120)
        procs = ra["procs"] + rb["procs"]
        for r in (ra, rb):
            flags = (r["seeded"], r["js"], r["own"], r["other"])
            assert flags == (True, True, True, False), "seed/js/own/other flags: %r" % (flags,)
        assert not {p.pid for p in ra["procs"]} & {p.pid for p in rb["procs"]}
        _text(_call(a, NS[1], {}))          # back on A
        _text(_call(a, NS[2], {}))          # close A's page
        page = _fixture_page_text(b, _call(b, NS[0], {"url": url + "/inspect"}))
        unchanged = ("pilot_beta=%s" % srv.cookies["beta"]) in page
        isolated = srv.cookies["alpha"] not in page
        del page
        assert unchanged, "B altered by A"
        assert isolated, "A state leaked into B"
    finally:
        try:
            _close_managers([m[0] for m in mgrs])
        finally:
            if ex is not None:
                ex.shutdown(wait=True, cancel_futures=True)
            srv.shutdown()
            srv.server_close()
            srv.fixture_thread.join(timeout=2)
            assert not srv.fixture_thread.is_alive(), "fixture thread survived shutdown"
    _assert_gone(procs)


def test_close_after_local_fixture_failure(tmp_path):
    node, cli = _paths()
    srv, url = _fixture()
    mgr, procs = None, []
    try:
        mgr, _ = _manager(node, cli, tmp_path / "fail")
        _discover(mgr)
        _text(_call(mgr, NS[0], {"url": url + "/"}))
        procs = _owned_procs(mgr)
        srv.shutdown()
        srv.server_close()  # intentional failure: fixture gone
        try:
            res = mgr.call(NS[0], {"url": url + "/seed/alpha"})
            failed = isinstance(res, dict) and res.get("isError") is True
        except hm.MCPError:
            failed = True
        assert failed, "navigation to a closed fixture should fail"
    finally:
        try:
            _close_managers([mgr] if mgr is not None else [])
        finally:
            srv.shutdown()
            srv.server_close()
            srv.fixture_thread.join(timeout=2)
            assert not srv.fixture_thread.is_alive(), "fixture thread survived shutdown"
    assert procs, "no owned processes captured"
    _assert_gone(procs)

"""Synthetic unit tests and opt-in real MCP transport check for research."""
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SERVER = ROOT / "tools" / "browser-research" / "server.mjs"
NODE = os.environ.get("BROWSER_RESEARCH_NODE") or shutil.which("node")

pytestmark = pytest.mark.skipif(not NODE, reason="node not available")


def run_js(tmp_path, body, timeout=60):
    """Run an ES module that imports the server as `S`; return its last JSON stdout line."""
    script = tmp_path / "case.mjs"
    script.write_text(
        f"import * as S from {json.dumps(SERVER.as_uri())};\n"
        "const out = (v) => process.stdout.write(JSON.stringify(v) + '\\n');\n"
        f"{body}\n",
        encoding="utf-8",
    )
    proc = subprocess.run(
        [NODE, str(script)], capture_output=True, text=True, timeout=timeout, cwd=tmp_path,
        env={**os.environ, "HOME": str(tmp_path), "USERPROFILE": str(tmp_path),
             "APPDATA": str(tmp_path), "LOCALAPPDATA": str(tmp_path),
             "TEMP": str(tmp_path), "TMP": str(tmp_path)},
    )
    assert proc.returncode == 0, proc.stderr
    lines = [l for l in proc.stdout.splitlines() if l.strip()]
    assert lines, "no output; stderr=" + proc.stderr
    return json.loads(lines[-1])


BAD_URLS = [
    "file:///C:/Windows/win.ini", "javascript:alert(1)", "data:text/html,hi",
    "chrome://settings", "ftp://example.com/", "http://user:pw@example.com/",
    "https://user@example.com/", "http://example.com:8080/", "https://example.com:8443/",
    "http:///nohost", "not a url", "", "https://" + "a" * 2050 + ".com/",
]
GOOD_URLS = ["https://example.com/a?b=1", "http://example.com:80/", "https://example.com:443/x"]


def test_validate_url(tmp_path):
    res = run_js(tmp_path, f"""
const bad = {json.dumps(BAD_URLS)}.map(u => S.validateUrl(u));
const good = {json.dumps(GOOD_URLS)}.map(u => S.validateUrl(u));
out({{bad, good, nonstr: S.validateUrl(42)}});
""")
    assert all(r["ok"] is False and isinstance(r["error"], str) for r in res["bad"])
    assert res["nonstr"]["ok"] is False
    assert all(r["ok"] is True for r in res["good"])
    # error categories are static: never echo the input
    for r, u in zip(res["bad"], BAD_URLS):
        assert not u or u not in r["error"]


def test_format_result_small_and_capped(tmp_path):
    res = run_js(tmp_path, r"""
const links = [];
for (let i = 0; i < 2000; i++) links.push({text: 'link"\\' + i, href: 'https://ex.com/p/' + i});
links.push({text: 'bad', href: 'javascript:alert(1)'}, {text: 'f', href: 'file:///etc/passwd'});
const big = S.formatResult({url: 'https://ex.com/', title: 'T', retrievedAt: '2026-01-01T00:00:00Z',
  text: '\u00e9"\\\n'.repeat(40000), links});
const small = S.formatResult({url: 'https://ex.com/f', title: 'Small', retrievedAt: 'now',
  text: 'hello body', links: [{text: 'a', href: 'https://ex.com/a'}]});
out({big, small, bigJson: JSON.stringify(S.toolResult(big)).length,
     again: S.formatResult({url: 'https://ex.com/', title: 'T', retrievedAt: '2026-01-01T00:00:00Z',
       text: '\u00e9"\\\n'.repeat(40000), links}) === big});
""")
    assert res["bigJson"] <= 30000
    assert res["again"] is True
    big = res["big"]
    assert "truncated" in big.lower()
    assert "javascript:" not in big and "file:" not in big
    assert "snapshot" not in big.lower() and ".md" not in big and ".yml" not in big
    small = res["small"]
    for frag in ("https://ex.com/f", "Small", "now", "hello body", "https://ex.com/a"):
        assert frag in small
    assert "truncated" not in small.lower()


def test_tools_exact_schema(tmp_path):
    tools = run_js(tmp_path, "out(S.TOOLS);")
    assert [t["name"] for t in tools] == ["browser_navigate", "browser_navigate_back", "browser_close"]
    for t in tools:
        assert t["annotations"]["destructiveHint"] is True
        assert t["annotations"]["readOnlyHint"] is False
        assert t["inputSchema"]["type"] == "object"
        assert t["inputSchema"]["additionalProperties"] is False
    nav = tools[0]["inputSchema"]
    assert nav["required"] == ["url"]
    assert list(nav["properties"]) == ["url"]
    assert nav["properties"]["url"]["maxLength"] == 2048
    assert tools[1]["inputSchema"]["properties"] == {}
    assert tools[2]["inputSchema"]["properties"] == {}


def test_launch_options(tmp_path):
    opts = run_js(tmp_path, "out(S.launchOptions({browserPath: 'C:/x/hs.exe', port: 4321}));")
    assert opts["executablePath"] == "C:/x/hs.exe"
    assert opts["headless"] is True and opts["chromiumSandbox"] is True
    assert opts["proxy"] == {"server": "http://127.0.0.1:4321", "bypass": "<-loopback>"}
    args = " ".join(opts["args"])
    assert "--disable-quic" in args
    assert "--disable-background-networking" in args
    assert "--force-webrtc-ip-handling-policy=disable_non_proxied_udp" in args
    for bad in ("--no-sandbox", "remote-debugging", "load-extension", "ignore-certificate"):
        assert bad not in args
    assert "ignoreHTTPSErrors" not in opts
    ctx = run_js(tmp_path, "out(S.CONTEXT_OPTIONS);")
    assert ctx == {"serviceWorkers": "block", "acceptDownloads": False, "permissions": []}


FAKE_CTX = r"""
function fakeContext(withWs = true) {
  const c = {routes: [], wsRoutes: [], events: {}, order: [],
    route: async (p, h) => { c.order.push('route'); c.routes.push([p, h]); },
    on: (e, h) => { c.events[e] = h; },
    pages: () => [], newPage: async () => { c.order.push('page'); }};
  if (withWs) c.routeWebSocket = async (p, h) => { c.order.push('ws'); c.wsRoutes.push([p, h]); };
  return c;
}
async function decide(c, url, method) {
  let r = null;
  const route = {request: () => ({url: () => url, method: () => method}),
    abort: async () => { r = 'abort'; }, continue: async () => { r = 'continue'; }};
  await c.routes[0][1](route);
  return r;
}
"""


def test_configure_context(tmp_path):
    res = run_js(tmp_path, FAKE_CTX + r"""
const c = fakeContext();
await S.configureContext(c);
let wsClosed = false;
await c.wsRoutes[0][1]({close: async () => { wsClosed = true; }});
const d = {};
for (const [u, m] of [['https://ex.com/', 'GET'], ['https://ex.com/', 'HEAD'], ['https://ex.com/', 'POST'],
    ['file:///c:/x', 'GET'], ['https://u:p@ex.com/', 'GET'], ['data:text/plain,x', 'GET'],
    ['blob:https://ex.com/1', 'GET'], ['ws://ex.com/', 'GET']]) d[m + ' ' + u] = await decide(c, u, m);
let missing = null;
try { await S.configureContext(fakeContext(false)); } catch (e) { missing = 'threw'; }
out({d, wsClosed, order: c.order, routePat: c.routes[0][0], wsPat: c.wsRoutes[0][0], missing,
     hasPageHandler: typeof c.events.page === 'function'});
""")
    d = res["d"]
    assert d["GET https://ex.com/"] == "continue" and d["HEAD https://ex.com/"] == "continue"
    assert all(v == "abort" for k, v in d.items() if k not in ("GET https://ex.com/", "HEAD https://ex.com/"))
    assert res["wsClosed"] is True and res["order"][0] == "ws"
    assert res["routePat"] == "**/*" and res["wsPat"] == "**/*"
    assert res["missing"] == "threw" and res["hasPageHandler"] is True


FAKE_SESSION = FAKE_CTX + r"""
import fs from 'node:fs';
const log = [];
function deps({failGoto = false, failLaunch = false} = {}) {
  return {browserPath: 'C:/x/hs.exe',
    startProxy: async () => { log.push('proxy'); return {port: 5555, stop: async () => log.push('proxystop')}; },
    launch: async (opts) => {
      log.push('launch'); if (failLaunch) throw new Error('SECRET C:/Users/x stack');
      log.push({home: opts.env.USERPROFILE, exists: fs.existsSync(opts.env.USERPROFILE)});
      return {newContext: async (o) => { log.push({ctx: o}); const c = fakeContext();
          c.close = async () => log.push('ctxclose');
          c.newPage = async () => ({url: () => 'https://ex.com/final', title: async () => 'Fake',
            goto: async () => { if (failGoto) throw new Error('net::ERR https://secret/?q=1'); },
            goBack: async () => null, on: () => {}, setDefaultTimeout: () => {},
            setDefaultNavigationTimeout: () => {},
            evaluate: async () => ({text: 'Body text', links: [{text: 'L', href: 'https://ex.com/l'}]})});
          return c; },
        close: async () => { log.push({bclose: fs.existsSync(opts.env.USERPROFILE)}); }};
    }};
}
"""


def test_session_flow_and_cleanup(tmp_path):
    res = run_js(tmp_path, FAKE_SESSION + r"""
const s = S.createResearchSession(deps());
const empty = [await s.back(), await s.close()];
const bad = await s.navigate('file:///c:/x');
const launchedEarly = log.includes('launch');
const ok = await s.navigate('https://ex.com/');
const back = await s.back();
const closed = await s.close();
const ok2 = await s.navigate('https://ex.com/');
await s.shutdown();
const home = log.find(x => x && x.home).home;
out({empty, bad, launchedEarly, ok, back, closed, ok2, log, homeGone: !fs.existsSync(home)});
""")
    assert all(isinstance(x, str) for x in res["empty"])
    assert res["launchedEarly"] is False and "file:" not in res["bad"]
    for frag in ("https://ex.com/final", "Fake", "Body text", "https://ex.com/l"):
        assert frag in res["ok"] and frag in res["ok2"]
    log = res["log"]
    assert log.count("launch") == 2 and "ctxclose" in log and "proxystop" in log
    assert {"ctx": {"serviceWorkers": "block", "acceptDownloads": False, "permissions": []}} in log
    assert all(e["bclose"] is True for e in log if isinstance(e, dict) and "bclose" in e)
    assert res["homeGone"] is True


def test_session_static_errors(tmp_path):
    res = run_js(tmp_path, FAKE_SESSION + r"""
const a = S.createResearchSession(deps({failLaunch: true}));
const r1 = await a.navigate('https://ex.com/'); await a.shutdown();
const b = S.createResearchSession(deps({failGoto: true}));
const r2 = await b.navigate('https://ex.com/'); await b.shutdown();
out({r1, r2, log});
""")
    for r in (res["r1"], res["r2"]):
        for leak in ("SECRET", "C:/Users", "stack", "secret", "net::", "?q=1"):
            assert leak not in r
    assert "proxystop" in res["log"]


def test_hung_page_extraction_is_bounded_and_discards_browser(tmp_path):
    result = run_js(tmp_path, FAKE_SESSION + r"""
const d = deps();
const launch = d.launch;
d.launch = async (opts) => {
  const b = await launch(opts), nc = b.newContext;
  b.newContext = async (o) => {
    const c = await nc(o), np = c.newPage;
    c.newPage = async () => { const p = await np(); p.evaluate = () => new Promise(() => {}); return p; };
    return c;
  };
  return b;
};
const s = S.createResearchSession({...d, readTimeoutMs: 30, closeTimeoutMs: 30});
const answer = await Promise.race([s.navigate('https://ex.com/'),
  new Promise(r => setTimeout(() => r('hung'), 500))]);
await s.shutdown();
out({bounded: answer !== 'hung', staticError: answer === S.ERR.nav, closed: log.includes('ctxclose')});
""")
    assert result == {"bounded": True, "staticError": True, "closed": True}


def test_http_denial_is_not_returned_as_successful_page(tmp_path):
    result = run_js(tmp_path, FAKE_SESSION + r"""
const d = deps(), launch = d.launch;
d.launch = async opts => {
  const b = await launch(opts), nc = b.newContext;
  b.newContext = async o => {
    const c = await nc(o), np = c.newPage;
    c.newPage = async () => { const p = await np(); p.goto = async () => ({status: () => 403}); return p; };
    return c;
  };
  return b;
};
const s = S.createResearchSession(d);
const answer = await s.navigate('https://ex.com/');
await s.shutdown();
out({denied: answer === S.ERR.nav, closed: log.includes('ctxclose')});
""")
    assert result == {"denied": True, "closed": True}


def test_failed_browser_cleanup_aborts_owned_server_instead_of_hanging(tmp_path):
    result = run_js(tmp_path, FAKE_SESSION + r"""
import path from 'node:path';
let aborted = false;
const d = deps(), launch = d.launch;
d.launch = async opts => {
  const b = await launch(opts);
  b.close = () => new Promise(() => {});
  return b;
};
const s = S.createResearchSession({...d, closeTimeoutMs: 20, abortOwner: () => { aborted = true; }});
await s.navigate('https://ex.com/');
await s.close();
// The production abortOwner exits; this fake removes its synthetic temp state.
const home = log.find(x => x && x.home).home;
fs.rmSync(path.dirname(home), {recursive: true, force: true});
out({aborted});
""")
    assert result == {"aborted": True}


def test_parse_args(tmp_path):
    files = {k: tmp_path / n for k, n in
             [("python", "python.exe"), ("browser", "hs.exe"), ("proxy", "public_proxy.py")]}
    for f in files.values():
        f.write_text("x")
    root = tmp_path / "node_modules"
    root.mkdir()
    good = ["--python", str(files["python"]), "--playwright-root", str(root),
            "--browser", str(files["browser"]), "--proxy-script", str(files["proxy"])]
    cases = {"good": good, "unknown": good + ["--x", "1"], "missing": good[:6],
             "relative": ["--python", "python.exe"] + good[2:],
             "nonexist": good[:7] + [str(tmp_path / "nope.py")], "dup": good + good[:2]}
    res = run_js(tmp_path, f"""
const cases = {json.dumps(cases)}; const r = {{}};
for (const [k, v] of Object.entries(cases)) {{
  try {{ r[k] = S.parseArgs(v); }} catch (e) {{ r[k] = 'error'; }}
}}
out(r);
""")
    assert res["good"] == {"python": str(files["python"]), "playwrightRoot": str(root),
                           "browser": str(files["browser"]), "proxyScript": str(files["proxy"])}
    for k in ("unknown", "missing", "relative", "nonexist", "dup"):
        assert res[k] == "error", k


def test_call_tool_dispatch(tmp_path):
    res = run_js(tmp_path, r"""
const calls = [];
const s = {navigate: async (u) => { calls.push('nav ' + u); return 'N'; },
  back: async () => { calls.push('back'); return 'B'; }, close: async () => { calls.push('close'); return 'C'; }};
const r = [await S.callTool(s, 'browser_navigate', {url: 'https://ex.com/'}),
  await S.callTool(s, 'browser_navigate_back', {}), await S.callTool(s, 'browser_close', undefined),
  await S.callTool(s, 'browser_evaluate', {code: 'x'}),
  await S.callTool(s, 'browser_navigate', {url: 'https://ex.com/', code: '1'}),
  await S.callTool(s, 'browser_navigate', {url: 5})];
out({r, calls});
""")
    r = res["r"]
    assert [x["content"][0]["text"] for x in r[:3]] == ["N", "B", "C"]
    assert all(x["content"][0]["type"] == "text" for x in r)
    assert all(x.get("isError") is True for x in r[3:])
    assert res["calls"] == ["nav https://ex.com/", "back", "close"]


def test_cli_rejects_bad_args_without_stdout(tmp_path):
    proc = subprocess.run([NODE, str(SERVER), "--bogus", "x"], capture_output=True, text=True,
                          timeout=60, cwd=tmp_path)
    assert proc.returncode == 2 and proc.stdout == ""


def test_start_proxy_handshake_and_stop(tmp_path):
    import sys
    good = tmp_path / "good_proxy.py"
    good.write_text('import sys\nprint(\'{"port": 4567}\', flush=True)\nsys.stdin.read()\n')
    bad = tmp_path / "bad_proxy.py"
    bad.write_text("print('nope', flush=True)\n")
    py = json.dumps(sys.executable)
    res = run_js(tmp_path, f"""
const p = await S.startProxy({py}, {json.dumps(str(good))}, 20000);
await p.stop();
let badRes = 'resolved';
try {{ await S.startProxy({py}, {json.dumps(str(bad))}, 20000); }} catch (e) {{ badRes = 'rejected'; }}
out({{port: p.port, badRes}});
""")
    assert res == {"port": 4567, "badRes": "rejected"}


@pytest.mark.skipif(not os.environ.get("BROWSER_RESEARCH_PLAYWRIGHT_ROOT"),
                    reason="opt-in: set BROWSER_RESEARCH_PLAYWRIGHT_ROOT to the cached node_modules")
def test_stdio_initialize_and_list_tools(tmp_path):
    """Real engine/client, no browser launch: tool discovery must remain lazy."""
    import sys
    dummy = tmp_path / "hs.exe"
    dummy.write_text("x")
    proxy = tmp_path / "proxy.py"
    proxy.write_text("raise SystemExit(1)\n")
    import importlib.util
    spec = importlib.util.spec_from_file_location("research_sdk_client", ROOT / "engine" / "harness_mcp.py")
    hm = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(hm)
    command = [NODE, str(SERVER), "--python", sys.executable, "--playwright-root",
               os.environ["BROWSER_RESEARCH_PLAYWRIGHT_ROOT"], "--browser", str(dummy), "--proxy-script", str(proxy)]
    tools = ["browser_navigate", "browser_navigate_back", "browser_close"]
    policy = hm.validate_mcp_policy({"allowed_commands": [command], "servers": {
        "research": {"command": command, "allowed_tools": tools, "enabled": True, "timeout_s": 30}}})
    manager = hm.MCPManager(policy, str(tmp_path))
    try:
        discovered = manager.list_tools("research")["tools"]
        assert [t["name"] for t in discovered] == ["mcp__research__" + t for t in tools]
        assert all(t["mutating"] for t in discovered)
    finally:
        manager.close()

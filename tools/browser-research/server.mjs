// Public research MCP: isolated browser, guarded proxy, page text and links inline.
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { spawn } from 'node:child_process';
import { createRequire } from 'node:module';
import { fileURLToPath } from 'node:url';
import readline from 'node:readline';

export const RESULT_CAP = 30000; // complete JSON tool result
const TEXT_BUDGET = 28500;
const MAX_LINKS = 150;
const NAV_TIMEOUT_MS = 30000;
const ACTION_TIMEOUT_MS = 15000;
const PROXY_START_TIMEOUT_MS = 15000;

function deadline(promise, milliseconds) {
  let timer;
  const expired = new Promise((_, reject) => {
    timer = setTimeout(() => reject(new Error('operation deadline')), milliseconds);
  });
  return Promise.race([promise, expired]).finally(() => clearTimeout(timer));
}

export const ERR = {
  url: 'Error: URL rejected (only public http/https on ports 80/443, no credentials).',
  nav: 'Error: navigation failed (blocked, unreachable, or timed out).',
  browser: 'Error: browser unavailable.',
  args: 'Error: invalid tool arguments.',
  tool: 'Error: unknown tool.',
};

export function validateUrl(raw) {
  const bad = { ok: false, error: ERR.url };
  if (typeof raw !== 'string' || raw.length === 0 || raw.length > 2048) return bad;
  // Raw shape check: WHATWG parsing would silently repair "http:///host" or backslashes.
  if (!/^https?:\/\/[^\/\\?#\s]/i.test(raw)) return bad;
  let u;
  try { u = new URL(raw); } catch { return bad; }
  if (u.protocol !== 'http:' && u.protocol !== 'https:') return bad;
  if (u.username || u.password || !u.hostname) return bad;
  if (!['', '80', '443'].includes(u.port)) return bad;
  return { ok: true, url: u.href };
}

const ANNOTATIONS = { readOnlyHint: false, destructiveHint: true, idempotentHint: false, openWorldHint: true };
const noArgs = { type: 'object', properties: {}, additionalProperties: false };
export const TOOLS = [
  { name: 'browser_navigate', description: 'Open a public http(s) URL and return its rendered text, title, final URL and links.',
    inputSchema: { type: 'object', properties: { url: { type: 'string', maxLength: 2048 } },
      required: ['url'], additionalProperties: false }, annotations: ANNOTATIONS },
  { name: 'browser_navigate_back', description: 'Go back one page and return its text.',
    inputSchema: noArgs, annotations: ANNOTATIONS },
  { name: 'browser_close', description: 'Close the browser session (it restarts on next navigate).',
    inputSchema: noArgs, annotations: ANNOTATIONS },
];
export function toolResult(text, isError = false) {
  const r = { content: [{ type: 'text', text }] };
  if (isError) r.isError = true;
  return r;
}

const jsonLen = (s) => JSON.stringify(toolResult(s)).length;
const clip = (s, n) => {
  let out = String(s ?? '').replace(/\s+/g, ' ').trim().slice(0, n);
  if (/[\ud800-\udbff]$/.test(out)) out = out.slice(0, -1); // never end on a lone surrogate
  return out;
};
const NOTICE = '\n[truncated: page content exceeded the response limit]';

export function formatResult({ url, title, retrievedAt, text, links }) {
  const head = `URL: ${clip(url, 2048)}\nTitle: ${clip(title, 300)}\nRetrieved: ${clip(retrievedAt, 64)}\n\n`;
  const seen = new Set();
  const lines = [];
  for (const l of Array.isArray(links) ? links : []) {
    const href = typeof l?.href === 'string' ? l.href : '';
    if (!/^https?:\/\//i.test(href) || href.length > 1000 || seen.has(href)) continue;
    seen.add(href);
    lines.push(`- ${clip(l.text, 120) || '(no text)'} <${href}>`);
    if (lines.length >= MAX_LINKS) break;
  }
  // Links get at most ~6000 JSON chars; the body takes the rest.
  let linkBlock = '';
  for (const ln of lines) {
    const next = linkBlock + ln + '\n';
    if (jsonLen(next) > 6000) break;
    linkBlock = next;
  }
  linkBlock = linkBlock ? `\n\nLinks:\n${linkBlock}` : '';
  let body = String(text ?? '');
  if (/[\ud800-\udbff]$/.test(body)) body = body.slice(0, -1);
  const full = head + body + linkBlock;
  if (jsonLen(full) <= TEXT_BUDGET) return full;
  let lo = 0, hi = body.length; // largest body prefix that fits, deterministic
  while (lo < hi) {
    const mid = Math.ceil((lo + hi) / 2);
    if (jsonLen(head + body.slice(0, mid) + NOTICE + linkBlock) <= TEXT_BUDGET) lo = mid; else hi = mid - 1;
  }
  let cut = body.slice(0, lo);
  if (/[\ud800-\udbff]$/.test(cut)) cut = cut.slice(0, -1);
  return head + cut + NOTICE + linkBlock;
}

export function launchOptions({ browserPath, port, env }) {
  const o = {
    executablePath: browserPath, headless: true, chromiumSandbox: true,
    proxy: { server: `http://127.0.0.1:${port}`, bypass: '<-loopback>' },
    args: ['--disable-quic', '--disable-background-networking', '--disable-component-update',
      '--disable-sync', '--no-first-run', '--no-default-browser-check',
      '--force-webrtc-ip-handling-policy=disable_non_proxied_udp',
      '--webrtc-ip-handling-policy=disable_non_proxied_udp'],
    timeout: NAV_TIMEOUT_MS, handleSIGINT: false, handleSIGTERM: false, handleSIGHUP: false,
  };
  if (env) o.env = env;
  return o;
}

export const CONTEXT_OPTIONS = { serviceWorkers: 'block', acceptDownloads: false, permissions: [] };

function allowedRequest(url, method) {
  if (method !== 'GET' && method !== 'HEAD') return false;
  let u;
  try { u = new URL(url); } catch { return false; }
  return (u.protocol === 'http:' || u.protocol === 'https:') && !u.username && !u.password;
}

const guardPage = (page) => {
  page.on?.('download', (d) => { d.cancel?.().catch?.(() => {}); });
  page.setDefaultTimeout?.(ACTION_TIMEOUT_MS);
  page.setDefaultNavigationTimeout?.(NAV_TIMEOUT_MS);
};

// Must run before any page exists. Throws if WebSocket routing is unavailable.
export async function configureContext(context) {
  if (typeof context.routeWebSocket !== 'function') throw new Error('routeWebSocket unsupported');
  await context.routeWebSocket('**/*', async (ws) => { await ws.close?.().catch?.(() => {}); });
  await context.route('**/*', async (route) => {
    const req = route.request();
    if (allowedRequest(req.url(), req.method())) await route.continue();
    else await route.abort();
  });
  context.on('page', async (page) => {
    guardPage(page);
    try { if (await page.opener?.()) await page.close(); } catch { /* popup already gone */ }
  });
}

// Fixed, trusted extraction script. Never model-supplied.
const EXTRACT = () => {
  const text = (document.body && document.body.innerText) || '';
  const links = [];
  for (const a of Array.from(document.querySelectorAll('a[href]')).slice(0, 2000)) {
    links.push({ text: (a.innerText || a.textContent || '').slice(0, 200), href: a.href });
  }
  return { text: text.slice(0, 200000), links };
};

function makeTempEnv() {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'browser-research-'));
  const d = (n) => { const p = path.join(root, n); fs.mkdirSync(p); return p; };
  const home = d('home'), temp = d('temp');
  const env = { ...process.env, HOME: home, USERPROFILE: home, APPDATA: d('appdata'),
    LOCALAPPDATA: d('localappdata'), TEMP: temp, TMP: temp };
  return { root, env, output: d('output') };
}

// deps: { browserPath, startProxy(): {port, stop()}, launch(launchOptions): Browser }
export function createResearchSession(deps) {
  let st = null; // { tmp, proxy, browser, context, page }
  let queue = Promise.resolve();
  const serial = (fn) => { const p = queue.then(fn, fn); queue = p.catch(() => {}); return p; };

  async function teardown() {
    const s = st; st = null;
    if (!s) return;
    const milliseconds = deps.closeTimeoutMs ?? 3000;
    try { await deadline(s.context?.close(), milliseconds); } catch { /* close browser next */ }
    let stopped = !s.browser;
    try { await deadline(s.browser?.close(), milliseconds); stopped = true; } catch { /* owner abort below */ }
    try { await deadline(s.proxy?.stop(), milliseconds); } catch { stopped = false; }
    if (!stopped) {
      // A page must not trap shutdown behind its JS thread. Exiting this owned
      // MCP server makes the existing engine kill its entire Windows Job.
      deps.abortOwner?.();
      return;
    }
    try { fs.rmSync(s.tmp.root, { recursive: true, force: true, maxRetries: 5 }); } catch { /* ignore */ }
  }

  async function start() {
    if (st) return st;
    st = { tmp: makeTempEnv() };
    st.proxy = await deps.startProxy();
    st.browser = await deps.launch(launchOptions({ browserPath: deps.browserPath, port: st.proxy.port, env: st.tmp.env }));
    st.context = await st.browser.newContext({ ...CONTEXT_OPTIONS });
    await configureContext(st.context);
    st.page = await st.context.newPage();
    guardPage(st.page);
    return st;
  }

  async function read(page) {
    const [data, title] = await deadline(Promise.all([page.evaluate(EXTRACT), page.title()]),
      deps.readTimeoutMs ?? 5000);
    return formatResult({ url: page.url(), title, retrievedAt: new Date().toISOString(),
      text: data?.text, links: data?.links });
  }

  return {
    navigate: (raw) => serial(async () => {
      const v = validateUrl(raw);
      if (!v.ok) return v.error;
      try { await start(); } catch { await teardown(); return ERR.browser; }
      try {
        const response = await st.page.goto(v.url, { waitUntil: 'domcontentloaded', timeout: NAV_TIMEOUT_MS });
        if (response?.status?.() >= 400) throw new Error('HTTP response denied');
        await st.page.waitForLoadState?.('load', { timeout: 5000 }).catch?.(() => {});
        return await read(st.page);
      } catch { await teardown(); return ERR.nav; }
    }),
    back: () => serial(async () => {
      if (!st?.page) return 'No page is open.';
      try {
        const resp = await st.page.goBack({ waitUntil: 'domcontentloaded', timeout: NAV_TIMEOUT_MS });
        if (resp?.status?.() >= 400) throw new Error('HTTP response denied');
        if (resp === null && st.page.url() === 'about:blank') return 'No previous page.';
        return await read(st.page);
      } catch { await teardown(); return ERR.nav; }
    }),
    close: () => serial(async () => { await teardown(); return 'Browser closed.'; }),
    shutdown: () => serial(teardown),
  };
}

const ARG_KEYS = { '--python': ['python', 'file'], '--playwright-root': ['playwrightRoot', 'dir'],
  '--browser': ['browser', 'file'], '--proxy-script': ['proxyScript', 'file'] };

export function parseArgs(argv) {
  const out = {};
  for (let i = 0; i < argv.length; i += 2) {
    const spec = ARG_KEYS[argv[i]];
    const val = argv[i + 1];
    if (!spec || typeof val !== 'string' || spec[0] in out) throw new Error(`bad argument ${argv[i]}`);
    if (!path.isAbsolute(val)) throw new Error(`${argv[i]} must be absolute`);
    let ok = false;
    try { const s = fs.statSync(val); ok = spec[1] === 'dir' ? s.isDirectory() : s.isFile(); } catch { /* missing */ }
    if (!ok) throw new Error(`${argv[i]} does not exist`);
    out[spec[0]] = val;
  }
  for (const [flag, [key]] of Object.entries(ARG_KEYS)) if (!(key in out)) throw new Error(`missing ${flag}`);
  return out;
}

export async function callTool(session, name, args) {
  const a = args ?? {};
  if (typeof a !== 'object' || Array.isArray(a)) return toolResult(ERR.args, true);
  const keys = Object.keys(a);
  const isErr = (t) => t.startsWith('Error:');
  const wrap = (t) => toolResult(t, isErr(t));
  if (name === 'browser_navigate') {
    if (keys.length !== 1 || typeof a.url !== 'string') return toolResult(ERR.args, true);
    return wrap(await session.navigate(a.url));
  }
  if (name === 'browser_navigate_back' || name === 'browser_close') {
    if (keys.length) return toolResult(ERR.args, true);
    return wrap(await (name === 'browser_close' ? session.close() : session.back()));
  }
  return toolResult(ERR.tool, true);
}

// Spawns the public-only proxy; resolves on its first stdout line {"port":int}.
export function startProxy(python, script, timeoutMs = PROXY_START_TIMEOUT_MS) {
  return new Promise((resolve, reject) => {
    const child = spawn(python, [script], { stdio: ['pipe', 'pipe', 'ignore'], windowsHide: true });
    const stop = async () => {
      if (child.exitCode !== null) return;
      const done = new Promise((r) => child.once('exit', r));
      try { child.stdin.end(); } catch { /* ignore */ }
      child.kill();
      await Promise.race([done, new Promise((r) => setTimeout(r, 3000))]);
    };
    const fail = () => { clearTimeout(t); stop(); reject(new Error('proxy start failed')); };
    const t = setTimeout(fail, timeoutMs);
    let buf = '';
    child.on('error', fail);
    child.on('exit', () => { if (buf !== null) fail(); });
    child.stdout.on('data', (chunk) => {
      if (buf === null) return;
      buf += chunk.toString('utf8');
      const nl = buf.indexOf('\n');
      if (nl < 0) { if (buf.length > 4096) fail(); return; }
      const line = buf.slice(0, nl); buf = null; clearTimeout(t);
      let port;
      try { port = JSON.parse(line).port; } catch { /* invalid */ }
      if (!Number.isInteger(port) || port < 1 || port > 65535) { stop(); reject(new Error('proxy start failed')); return; }
      child.stdout.resume();
      resolve({ port, stop });
    });
  });
}

export async function main(argv = process.argv.slice(2)) {
  let cfg;
  try { cfg = parseArgs(argv); } catch { process.stderr.write('browser-research: invalid arguments\n'); process.exitCode = 2; return; }
  const req = createRequire(path.join(cfg.playwrightRoot, 'noop.js'));
  const pw = req('playwright-core');

  const session = createResearchSession({
    browserPath: cfg.browser,
    startProxy: () => startProxy(cfg.python, cfg.proxyScript),
    launch: (opts) => pw.chromium.launch(opts),
    abortOwner: () => process.exit(75),
  });
  // The cached Playwright package bundles its SDK; it does not install an
  // importable SDK package. This server needs only the four stdio MCP methods.
  let exiting = false;
  const exit = async () => {
    if (exiting) return; exiting = true;
    try { await session.shutdown(); } finally { process.exit(0); }
  };
  for (const sig of ['SIGINT', 'SIGTERM', 'SIGHUP', 'SIGBREAK']) { try { process.on(sig, exit); } catch { /* unsupported */ } }
  const input = readline.createInterface({ input: process.stdin, crlfDelay: Infinity });
  const write = (value) => new Promise((resolve, reject) => {
    process.stdout.write(JSON.stringify(value) + '\n', (error) => error ? reject(error) : resolve());
  });
  try {
    for await (const line of input) {
      if (line.length > 65536) break;
      let message;
      try { message = JSON.parse(line); } catch {
        await write({ jsonrpc: '2.0', id: null, error: { code: -32700, message: 'Parse error' } });
        continue;
      }
      if (!message || typeof message !== 'object' || message.jsonrpc !== '2.0') continue;
      if (!Object.hasOwn(message, 'id')) continue; // notifications carry no reply
      const id = message.id;
      if (typeof id !== 'string' && typeof id !== 'number') continue;
      let result;
      if (message.method === 'initialize') {
        result = { protocolVersion: '2024-11-05', capabilities: { tools: {} },
          serverInfo: { name: 'browser-research', version: '0.1.0' } };
      } else if (message.method === 'ping') result = {};
      else if (message.method === 'tools/list') result = { tools: TOOLS };
      else if (message.method === 'tools/call') {
        try { result = await callTool(session, message.params?.name, message.params?.arguments); }
        catch { result = toolResult(ERR.browser, true); }
      } else {
        await write({ jsonrpc: '2.0', id, error: { code: -32601, message: 'Method not found' } });
        continue;
      }
      await write({ jsonrpc: '2.0', id, result });
    }
  } finally {
    input.close();
    await session.shutdown();
  }
}

const isMain = () => {
  try { return fs.realpathSync.native(process.argv[1]) === fs.realpathSync.native(fileURLToPath(import.meta.url)); }
  catch { return false; }
};
if (process.argv[1] && isMain()) {
  main().catch(() => { process.stderr.write('browser-research: startup failed\n'); process.exit(1); });
}

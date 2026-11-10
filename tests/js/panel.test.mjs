import test from "node:test";
import assert from "node:assert/strict";
import { fileURLToPath, pathToFileURL } from "node:url";
import { dirname, resolve } from "node:path";
import { readFileSync } from "node:fs";

const here = dirname(fileURLToPath(import.meta.url));
const { runStatusText } = await import(pathToFileURL(resolve(here, "../../web/dashboard.js")).href);
const modulePath = pathToFileURL(resolve(here, "../../web/panel.js")).href;
const {
  WRAP_UP_CONFIRM,
  summarizeToolCall,
  mergeEvents,
  nextAfter,
  groupTimeline,
  steerStatusText,
  killConfirmText,
  killOutcomeText,
  controlsState,
  nearBottom,
  outputLabel,
  reasoningFolded,
  statusText,
  previewHeading,
  previewVisible,
  liveLabel,
  runIdFromHash,
  hashForRun,
  createRunPanel,
} = await import(modulePath);

test("summarizeToolCall read_file range", () => {
  assert.equal(
    summarizeToolCall("read_file", '{"path": "a.py", "start_line": 3, "end_line": 9}'),
    "a.py:3-9",
  );
});

test("summarizeToolCall grep with glob", () => {
  assert.equal(
    summarizeToolCall("grep", '{"pattern": "def x", "glob": "src/**/*.py"}'),
    '"def x" in src/**/*.py',
  );
});

test("summarizeToolCall run_command", () => {
  assert.equal(
    summarizeToolCall("run_command", '{"argv": ["python", "-m", "pytest", "-q"]}'),
    "python -m pytest -q",
  );
});

test("summarizeToolCall truncated args", () => {
  const bad = '{"path": "a.py", "start_';
  assert.equal(summarizeToolCall("read_file", bad), ("read_file " + bad).trim());
});

test("mergeEvents replaces the tail", () => {
  const existing = [0, 1, 2, 3, 4].map((seq) => ({ seq }));
  const response = { total: 6, events: [{ seq: 4, v: "new" }, { seq: 5 }] };
  const out = mergeEvents(existing, 4, response);
  assert.equal(out.reset, false);
  assert.equal(out.events.length, 6);
  assert.equal(out.events[4].v, "new");
});

test("mergeEvents resets when shrunk", () => {
  const out = mergeEvents([{ seq: 0 }], 4, { total: 2, events: [] });
  assert.equal(out.reset, true);
  assert.deepEqual(out.events, []);
});

test("nextAfter", () => {
  assert.equal(nextAfter([]), 0);
  assert.equal(nextAfter([1, 2, 3, 4, 5]), 4);
});

test("groupTimeline groups consecutive turns", () => {
  const events = [
    { iteration: 1, kind: "narration" },
    { iteration: 1, kind: "tool" },
    { iteration: 2, kind: "narration" },
    { iteration: null, kind: "notice" },
    { iteration: null, kind: "final" },
  ];
  const items = groupTimeline(events);
  assert.equal(items.length, 4);
  assert.deepEqual(items.map((i) => i.type), ["turn", "turn", "notice", "final"]);
});

test("steerStatusText all states", () => {
  const base = { chars: 5, kind: "steer" };
  assert.equal(
    steerStatusText({ ...base, state: "queued" }),
    "Steer · 5 chars · Queued: arrives at the agent's next turn",
  );
  assert.equal(
    steerStatusText({ ...base, state: "delivered", turn: 7 }),
    "Steer · 5 chars · Delivered at turn 7",
  );
  assert.equal(
    steerStatusText({ ...base, state: "lost" }),
    "Steer · 5 chars · Not delivered: the steer file was overwritten or consumed without it; send again",
  );
  assert.equal(
    steerStatusText({ ...base, state: "undeliverable" }),
    "Steer · 5 chars · Not delivered: the run ended",
  );
  assert.equal(
    steerStatusText({ chars: 5, kind: "wrap_up", state: "queued" }),
    "Wrap-up request · 5 chars · Queued: arrives at the agent's next turn",
  );
});

test("killConfirmText by write mode", () => {
  const start = "Kill R? Its engine and child processes stop immediately and it writes no final report.";
  assert.equal(killConfirmText("R", { writes: "staged" }), start + " Its staged edits exist only in the engine's memory and will be lost.");
  assert.equal(killConfirmText("R", { worktree: true, root: "/w" }), start + " Edits made so far stay in /w.");
  assert.equal(killConfirmText("R", { writes: "disk" }), start + " Edits made so far are already in the repository.");
  assert.equal(killConfirmText("R", null), start + " Its write mode is unknown.");
});

test("killOutcomeText plurals", () => {
  assert.equal(killOutcomeText({ outcome: "terminated", children: 0 }), "Engine terminated.");
  assert.equal(killOutcomeText({ outcome: "terminated", children: 1 }), "Engine and 1 child process terminated.");
  assert.equal(killOutcomeText({ outcome: "terminated", children: 2 }), "Engine and 2 child processes terminated.");
  assert.equal(killOutcomeText({ outcome: "partial", survivors: 3, engine_pid: 42 }), "3 process(es) did not exit (engine PID 42).");
  assert.equal(killOutcomeText({ outcome: "already_gone" }), "The engine had already exited.");
});

test("controlsState", () => {
  const none = controlsState(null);
  assert.equal(none.steer.enabled, false);
  assert.equal(none.kill.enabled, false);
  assert.equal(none.steer.reason, "The run has ended.");
  const mixed = controlsState({ can_steer: false, can_kill: true, control_reason: "dismissed by operator" });
  assert.equal(mixed.steer.enabled, false);
  assert.equal(mixed.steer.reason, "dismissed by operator");
  assert.equal(mixed.kill.enabled, true);
  assert.equal(mixed.kill.reason, null);
  const both = controlsState({ can_steer: true, can_kill: true });
  assert.equal(both.steer.enabled, true);
  assert.equal(both.kill.enabled, true);
});

test("nearBottom", () => {
  assert.equal(nearBottom(0, 140, 100), true);
  assert.equal(nearBottom(0, 141, 100), false);
});

test("outputLabel", () => {
  assert.equal(
    outputLabel({ output: "x".repeat(1234), output_truncated: true }),
    "output · 1,234 chars · truncated",
  );
});

test("reasoningFolded", () => {
  assert.equal(reasoningFolded("a\nb\nc"), false);
  assert.equal(reasoningFolded("a\nb\nc\nd"), true);
});

test("hash round trip", () => {
  assert.equal(runIdFromHash(hashForRun("a b/c")), "a b/c");
  assert.equal(runIdFromHash("#x"), null);
});

test("panel.js never uses innerHTML", () => {
  const src = readFileSync(resolve(here, "../../web/panel.js"), "utf8");
  assert.equal(src.includes("innerHTML"), false);
});

test("panel.css wraps long lines", () => {
  const src = readFileSync(resolve(here, "../../web/panel.css"), "utf8");
  assert.ok(src.includes("overflow-wrap: anywhere"));
  assert.ok(src.includes("pre-wrap"));
});

test("statusText capitalises the engine's status line", () => {
  assert.equal(
    statusText({ text: "reply had no tool call; asking for one" }),
    "Reply had no tool call; asking for one",
  );
  assert.equal(statusText({}), "");
});

test("liveLabel marks live text only while the run is live", () => {
  assert.equal(liveLabel("Final report", { live: true }, true), "Final report (writing…)");
  assert.equal(liveLabel("Final report", { live: true }, false), "Final report");
  assert.equal(liveLabel("Final report", { live: false }, true), "Final report");
});

// --- fake DOM harness --------------------------------------------------

class FakeElement {
  constructor(tagName) {
    this.tagName = String(tagName || "").toUpperCase();
    this.children = [];
    this.parentNode = null;
    this.attributes = {};
    this.className = "";
    this._text = "";
    this.hidden = false;
    this.disabled = false;
    this.value = "";
    this.scrollTop = 0;
    this.scrollHeight = 0;
    this.clientHeight = 0;
    this._listeners = {};
    const self = this;
    this.classList = {
      add(c) {
        const set = new Set(self.className.split(" ").filter(Boolean));
        set.add(c);
        self.className = [...set].join(" ");
      },
      remove(c) {
        const set = new Set(self.className.split(" ").filter(Boolean));
        set.delete(c);
        self.className = [...set].join(" ");
      },
      contains(c) {
        return self.className.split(" ").filter(Boolean).includes(c);
      },
    };
  }
  get textContent() {
    return this._text;
  }
  set textContent(v) {
    this._text = v == null ? "" : String(v);
  }
  setAttribute(k, v) {
    this.attributes[k] = String(v);
    if (k === "class") this.className = String(v);
  }
  getAttribute(k) {
    return k in this.attributes ? this.attributes[k] : null;
  }
  hasAttribute(k) {
    return k in this.attributes;
  }
  removeAttribute(k) {
    delete this.attributes[k];
    if (k === "class") this.className = "";
  }
  // Native-like reflected boolean attribute (as on <details>).
  get open() {
    return "open" in this.attributes;
  }
  set open(v) {
    if (v) this.attributes.open = "";
    else delete this.attributes.open;
  }
  appendChild(c) {
    if (c.parentNode) c.parentNode.removeChild(c);
    c.parentNode = this;
    this.children.push(c);
    return c;
  }
  append(...cs) {
    for (const c of cs) this.appendChild(c);
  }
  insertBefore(c, ref) {
    if (c.parentNode) c.parentNode.removeChild(c);
    c.parentNode = this;
    const i = ref ? this.children.indexOf(ref) : -1;
    if (i < 0) this.children.push(c);
    else this.children.splice(i, 0, c);
    return c;
  }
  removeChild(c) {
    const i = this.children.indexOf(c);
    if (i >= 0) this.children.splice(i, 1);
    c.parentNode = null;
    return c;
  }
  remove() {
    if (this.parentNode) this.parentNode.removeChild(this);
  }
  get firstChild() {
    return this.children.length ? this.children[0] : null;
  }
  addEventListener(type, fn) {
    (this._listeners[type] || (this._listeners[type] = [])).push(fn);
  }
  focus() {}
  dispatch(type, event) {
    for (const fn of this._listeners[type] || []) fn(event || {});
  }
}

class FakeDocument {
  constructor() {
    this.body = new FakeElement("body");
    this.hidden = false;
  }
  createElement(tag) {
    return new FakeElement(tag);
  }
  createTextNode(t) {
    const node = new FakeElement("#text");
    node.textContent = t;
    return node;
  }
}
function find(root, cls) {
  if (root.className && root.className.split(" ").includes(cls)) return root;
  for (const c of root.children) {
    const hit = find(c, cls);
    if (hit) return hit;
  }
  return null;
}

function findAll(root, cls) {
  const out = [];
  const walk = (node) => {
    if (node.className && node.className.split(" ").includes(cls)) out.push(node);
    for (const c of node.children) walk(c);
  };
  walk(root);
  return out;
}

const realSetTimeout = globalThis.setTimeout;
const flush = () => new Promise((r) => realSetTimeout(r, 0));

// Interval stubs: record callbacks by id, tests fire ticks by hand.
let intervals;
function installIntervals() {
  intervals = new Map();
  let nextId = 1;
  globalThis.setInterval = (fn) => {
    const id = nextId++;
    intervals.set(id, fn);
    return id;
  };
  globalThis.clearInterval = (id) => {
    intervals.delete(id);
  };
}

function makeDeps() {
  const queues = { "/api/narration": [], "/api/activity": [], "/api/runs/": [] };
  const calls = [];
  const prefixFor = (url) => {
    if (url.startsWith("/api/narration")) return "/api/narration";
    if (url.startsWith("/api/activity")) return "/api/activity";
    if (url.startsWith("/api/runs/")) return "/api/runs/";
    return url;
  };
  const deps = {
    _queues: queues,
    _calls: calls,
    fetchJson(url, opts) {
      calls.push({ url, opts });
      const q = queues[prefixFor(url)];
      const next = q && q.length ? q.shift() : {};
      if (next instanceof Error) return Promise.reject(next);
      return Promise.resolve(next);
    },
    csrfToken: () => "t",
    projectName: (id) => id,
    liveStateClass: () => "state",
    runStatusText,
    formatElapsed: (s) => `elapsed ${s}`,
    iterationText: (i, m) => `iter ${i}/${m}`,
    formatCost: () => ({ text: "$1.00" }),
    phaseText: () => "phase",
    stepTimingText: (step) => (step && step.model_seconds != null ? `model ${step.model_seconds}s` : ""),
    onChange() {},
  };
  return deps;
}
const realSetInterval = globalThis.setInterval;
const realClearInterval = globalThis.clearInterval;

function setupGlobals() {
  globalThis.document = new FakeDocument();
  globalThis.history = { replaceState() {} };
  globalThis.location = { pathname: "/", search: "", hash: "" };
  globalThis.confirm = () => true;
  installIntervals();
}

function teardownGlobals() {
  globalThis.setInterval = realSetInterval;
  globalThis.clearInterval = realClearInterval;
  delete globalThis.document;
  delete globalThis.history;
  delete globalThis.location;
  delete globalThis.confirm;
}

function narration(events, total, extra) {
  return { available: true, total, events, masked: 0, steer: [], ...(extra || {}) };
}

test("diagnosis is rendered from metadata and cleared before switching runs", async () => {
  setupGlobals();
  try {
    const deps = makeDeps();
    deps._queues["/api/narration"].push(narration([], 0));
    deps._queues["/api/activity"].push({ steps: [], diagnosis: {
      category: "engine_exception", evidence: "recorded",
      exception: { class: "TypeError", source: "delegate.py", line: 42 }, coverage: {},
    } });
    const panel = createRunPanel(deps);
    panel.open("first", null);
    await flush();
    const box = find(globalThis.document.body, "rp-diagnosis");
    assert.equal(box.hidden, false);
    assert.ok(box.children.some(child => child._text.includes("TypeError")));
    deps._queues["/api/narration"].push(narration([], 0));
    deps._queues["/api/activity"].push(new Error("synthetic unavailable"));
    panel.open("second", null);
    assert.equal(box.hidden, true);
    assert.equal(box.children.length, 0);
    await flush();
    assert.equal(box.hidden, true);
    panel.close();
  } finally {
    teardownGlobals();
  }
});

test("revising the last event keeps its turn", async () => {
  setupGlobals();
  try {
    const deps = makeDeps();
    deps._queues["/api/narration"].push(
      narration(
        [
          { seq: 0, kind: "call", iteration: 1, attempt: 1 },
          { seq: 1, kind: "narration", iteration: 1, text: "n1" },
          { seq: 2, kind: "tool", iteration: 1, name: "t", args: "{}", output: "old" },
        ],
        3,
      ),
    );
    deps._queues["/api/activity"].push({ steps: [] });
    const panel = createRunPanel(deps);
    panel.open("r", null);
    await flush();
    deps._queues["/api/narration"].push(
      narration(
        [
          { seq: 2, kind: "tool", iteration: 1, name: "t", args: "{}", output: "new" },
          { seq: 3, kind: "narration", iteration: 2, text: "n2" },
        ],
        4,
      ),
    );
    deps._queues["/api/activity"].push({ steps: [] });
    for (const fn of intervals.values()) fn();
    await flush();
    const turns = findAll(globalThis.document.body, "rp-turn");
    assert.equal(turns.length, 2);
    const t1 = turns.find((t) => t.getAttribute("data-iteration") === "1");
    const t2 = turns.find((t) => t.getAttribute("data-iteration") === "2");
    assert.ok(find(t1, "rp-narration"));
    const tool = find(t1, "rp-tool");
    assert.ok(tool);
    assert.ok(find(tool, "rp-output").children.some((c) => c._text === "new"));
    assert.ok(find(t2, "rp-narration"));
  } finally {
    teardownGlobals();
  }
});

// --- disclosure state survives refreshes -------------------------------

const lines = (n, tag) => Array.from({ length: n }, (_, i) => `${tag} ${i}`).join("\n");
const body = () => globalThis.document.body;
const summaryText = (details) => details.children[0].textContent;
const preText = (details) => details.children[1].textContent;

async function openWith(deps, runId, events, total, extra) {
  deps._queues["/api/narration"].push(narration(events, total, extra));
  deps._queues["/api/activity"].push({ steps: [] });
  const panel = createRunPanel(deps);
  panel.open(runId, null);
  await flush();
  return panel;
}

test("panel header identifies incomplete runs and keeps unknown reasons private", async () => {
  setupGlobals();
  try {
    const panel = await openWith(makeDeps(), "r", [], 0);
    const row = { run_id: "r", label: "synthetic", model: "m", status: "failed",
      display_state: "failed", stop_reason: "max_iters" };
    panel.update({ runs: [row] });
    assert.equal(find(body(), "rp-meta").children[0].textContent, "incomplete: turn limit");
    panel.update({ runs: [{ ...row, stop_reason: "context_budget" }] });
    assert.equal(find(body(), "rp-meta").children[0].textContent, "incomplete: context budget");
    panel.update({ runs: [{ ...row, stop_reason: "recovery_state" }] });
    assert.equal(find(body(), "rp-meta").children[0].textContent, "incomplete: recovery state");
    panel.update({ runs: [{ ...row, stop_reason: "SYNTHETIC_PRIVATE" }] });
    assert.equal(find(body(), "rp-meta").children[0].textContent, "failed");
    panel.update({ live: [{ ...row, status: "running", display_state: "quiet", stop_reason: null }] });
    assert.equal(find(body(), "rp-meta").children[0].textContent, "quiet");
  } finally {
    teardownGlobals();
  }
});

async function poll(deps, response) {
  deps._queues["/api/narration"].push(response);
  deps._queues["/api/activity"].push({ steps: [] });
  for (const fn of [...intervals.values()]) fn();
  await flush();
  await flush();
}

const reasoningEvent = (seq, iteration, reasoning, live) => ({
  seq, kind: "narration", iteration, text: `synthetic narration ${seq}`, reasoning, live,
});

test("short open reasoning stays open as it grows; new long reasoning starts folded", async () => {
  setupGlobals();
  try {
    const deps = makeDeps();
    await openWith(deps, "r", [reasoningEvent(0, 1, lines(2, "think"), true)], 1);
    let r = findAll(body(), "rp-reasoning");
    assert.equal(r.length, 1);
    assert.equal(r[0].open, true, "short reasoning starts open");
    await poll(deps, narration([
      reasoningEvent(0, 1, lines(6, "think"), true),
      reasoningEvent(1, 2, lines(6, "other"), true),
    ], 2));
    r = findAll(body(), "rp-reasoning");
    assert.equal(r.length, 2);
    assert.equal(preText(r[0]), lines(6, "think"));
    assert.equal(r[0].open, true, "open reasoning must not fold while growing");
    assert.equal(r[1].open, false, "new long reasoning keeps folded default");
  } finally {
    teardownGlobals();
  }
});

test("manually expanded long reasoning persists through growth and polls", async () => {
  setupGlobals();
  try {
    const deps = makeDeps();
    const call = { seq: 0, kind: "call", iteration: 1, attempt: 1 };
    await openWith(deps, "r", [call, reasoningEvent(1, 1, lines(5, "a"), true)], 2);
    let r = find(body(), "rp-reasoning");
    assert.equal(r.open, false, "long reasoning starts folded");
    r.open = true; // user expands
    await poll(deps, narration([reasoningEvent(1, 1, lines(8, "a"), true)], 2));
    r = find(body(), "rp-reasoning");
    assert.equal(r.open, true);
    assert.equal(preText(r), lines(8, "a"));
    assert.equal(summaryText(r), "Reasoning (writing…)");
    await poll(deps, narration([reasoningEvent(1, 1, lines(8, "a"), true)], 2));
    await poll(deps, narration([reasoningEvent(1, 1, lines(9, "a"), false)], 2));
    r = find(body(), "rp-reasoning");
    assert.equal(r.open, true, "expanded reasoning survives repeated polls");
    assert.equal(preText(r), lines(9, "a"));
    assert.equal(summaryText(r), "Reasoning");
    assert.equal(findAll(body(), "rp-reasoning").length, 1);
  } finally {
    teardownGlobals();
  }
});

test("manually closed short reasoning stays closed through polls", async () => {
  setupGlobals();
  try {
    const deps = makeDeps();
    await openWith(deps, "r", [reasoningEvent(0, 1, lines(2, "s"), true)], 1);
    let r = find(body(), "rp-reasoning");
    assert.equal(r.open, true);
    r.open = false; // user collapses
    await poll(deps, narration([reasoningEvent(0, 1, lines(2, "s"), true)], 1));
    await poll(deps, narration([reasoningEvent(0, 1, lines(3, "s"), true)], 1));
    r = find(body(), "rp-reasoning");
    assert.equal(r.open, false, "closed reasoning must not reopen");
    assert.equal(preText(r), lines(3, "s"));
  } finally {
    teardownGlobals();
  }
});

const toolEvent = (seq, output, truncated) => ({
  seq, kind: "tool", iteration: 1, name: "read_file", args: "{}", output, output_truncated: truncated,
});

test("expanded tool output stays open while it updates; new output starts collapsed", async () => {
  setupGlobals();
  try {
    const deps = makeDeps();
    await openWith(deps, "r", [toolEvent(0, "abc", false)], 1);
    let o = find(body(), "rp-output");
    assert.equal(o.open, false, "output starts collapsed");
    o.open = true;
    await poll(deps, narration([toolEvent(0, "abcdef", true), toolEvent(1, "xyz", false)], 2));
    const outs = findAll(body(), "rp-output");
    assert.equal(outs.length, 2);
    assert.equal(outs[0].open, true, "expanded output must stay open");
    assert.equal(preText(outs[0]), "abcdef");
    assert.equal(summaryText(outs[0]), "output · 6 chars · truncated");
    assert.equal(outs[1].open, false, "new output keeps collapsed default");
  } finally {
    teardownGlobals();
  }
});

test("run-end label refresh preserves reasoning disclosure even if final fetch fails", async () => {
  setupGlobals();
  try {
    const deps = makeDeps();
    const panel = await openWith(deps, "r", [
      reasoningEvent(0, 1, lines(6, "long"), true),
      reasoningEvent(1, 1, lines(2, "short"), true),
    ], 2);
    let r = findAll(body(), "rp-reasoning");
    assert.deepEqual(r.map((d) => d.open), [false, true]);
    r[0].open = true;
    r[1].open = false;
    deps._queues["/api/narration"].push(new Error("synthetic failure"));
    deps._queues["/api/activity"].push({ steps: [] });
    panel.update({ live: [], runs: [{ run_id: "r" }] });
    // Before any await: only the synchronous label refresh has run.
    r = findAll(body(), "rp-reasoning");
    assert.deepEqual(r.map(summaryText), ["Reasoning", "Reasoning"]);
    assert.deepEqual(r.map((d) => d.open), [true, false]);
    await flush();
    await flush();
    r = findAll(body(), "rp-reasoning");
    assert.deepEqual(r.map((d) => d.open), [true, false]);
  } finally {
    teardownGlobals();
  }
});

test("task brief keeps its disclosure state across after=0 refreshes", async () => {
  setupGlobals();
  try {
    const deps = makeDeps();
    const ev = [reasoningEvent(0, 1, lines(2, "b"), true)];
    await openWith(deps, "r", ev, 1, { brief: "synthetic brief v1" });
    let b = find(body(), "rp-brief");
    assert.equal(b.open, false, "brief starts collapsed");
    b.open = true;
    await poll(deps, narration(ev, 1, { brief: "synthetic brief v2" }));
    await poll(deps, narration(ev, 1, { brief: "synthetic brief v2" }));
    b = find(body(), "rp-brief");
    assert.equal(b.open, true, "expanded brief must stay open");
    assert.equal(preText(b), "synthetic brief v2");
    assert.equal(findAll(body(), "rp-brief").length, 1);
    b.open = false;
    await poll(deps, narration(ev, 1, { brief: "synthetic brief v3" }));
    b = find(body(), "rp-brief");
    assert.equal(b.open, false, "closed brief stays closed");
    assert.equal(preText(b), "synthetic brief v3");
  } finally {
    teardownGlobals();
  }
});

test("transcript reset, run switch and reopen restore disclosure defaults", async () => {
  setupGlobals();
  try {
    const deps = makeDeps();
    const long = reasoningEvent(0, 1, lines(6, "x"), true);
    const panel = await openWith(deps, "r", [
      long, reasoningEvent(1, 1, lines(1, "y"), true), reasoningEvent(2, 1, lines(1, "z"), true),
    ], 3, { brief: "synthetic brief" });
    find(body(), "rp-reasoning").open = true;
    find(body(), "rp-brief").open = true;
    // total < after triggers a true reset, then an automatic after=0 refetch.
    deps._queues["/api/narration"].push(narration([], 1));
    deps._queues["/api/narration"].push(narration([long], 1, { brief: "synthetic brief" }));
    deps._queues["/api/activity"].push({ steps: [] });
    for (const fn of [...intervals.values()]) fn();
    await flush();
    await flush();
    await flush();
    assert.ok(
      deps._calls.some((c) => c.url.includes("after=2")) &&
        deps._calls.filter((c) => c.url.includes("after=0")).length >= 2,
      "reset path refetched from after=0",
    );
    assert.equal(findAll(body(), "rp-reasoning").length, 1);
    assert.equal(find(body(), "rp-reasoning").open, false, "reset restores folded default");
    assert.equal(find(body(), "rp-brief").open, false, "reset restores brief default");
    // Switching runs with identical seq must not carry state over.
    find(body(), "rp-reasoning").open = true;
    deps._queues["/api/narration"].push(narration([long], 1));
    deps._queues["/api/activity"].push({ steps: [] });
    panel.open("r2", null);
    await flush();
    assert.equal(find(body(), "rp-reasoning").open, false, "switch restores default");
    // Close and reopen the same run.
    find(body(), "rp-reasoning").open = true;
    panel.close();
    deps._queues["/api/narration"].push(narration([long], 1));
    deps._queues["/api/activity"].push({ steps: [] });
    panel.open("r2", null);
    await flush();
    assert.equal(findAll(body(), "rp-reasoning").length, 1);
    assert.equal(find(body(), "rp-reasoning").open, false, "reopen restores default");
  } finally {
    teardownGlobals();
  }
});
test("new events join their turn", async () => {
  setupGlobals();
  try {
    const deps = makeDeps();
    deps._queues["/api/narration"].push(
      narration(
        [
          { seq: 0, kind: "call", iteration: 1, attempt: 1 },
          { seq: 1, kind: "narration", iteration: 1, text: "n1" },
          { seq: 2, kind: "tool", iteration: 1, name: "t", args: "{}", output: "a" },
        ],
        3,
      ),
    );
    deps._queues["/api/activity"].push({ steps: [] });
    const panel = createRunPanel(deps);
    panel.open("r", null);
    await flush();
    deps._queues["/api/narration"].push(
      narration(
        [
          { seq: 2, kind: "tool", iteration: 1, name: "t", args: "{}", output: "a" },
          { seq: 3, kind: "tool", iteration: 1, name: "u", args: "{}", output: "b" },
        ],
        4,
      ),
    );
    deps._queues["/api/activity"].push({ steps: [] });
    for (const fn of intervals.values()) fn();
    await flush();
    const turns = findAll(globalThis.document.body, "rp-turn");
    assert.equal(turns.length, 1);
    assert.equal(turns[0].getAttribute("data-iteration"), "1");
    assert.equal(findAll(turns[0], "rp-tool").length, 2);
  } finally {
    teardownGlobals();
  }
});

test("reset clears and refetches from 0", async () => {
  setupGlobals();
  try {
    const deps = makeDeps();
    deps._queues["/api/narration"].push(
      narration(
        [
          { seq: 0, kind: "narration", iteration: 1, text: "n1" },
          { seq: 1, kind: "tool", iteration: 1, name: "t", args: "{}", output: "a" },
          { seq: 2, kind: "narration", iteration: 2, text: "n2" },
          { seq: 3, kind: "tool", iteration: 2, name: "u", args: "{}", output: "b" },
        ],
        4,
      ),
    );
    deps._queues["/api/activity"].push({ steps: [] });
    const panel = createRunPanel(deps);
    panel.open("r", null);
    await flush();
    // shrink -> reset, then refetch answers total 1
    deps._queues["/api/narration"].push(narration([], 1));
    deps._queues["/api/narration"].push(
      narration([{ seq: 0, kind: "narration", iteration: 1, text: "only" }], 1),
    );
    deps._queues["/api/activity"].push({ steps: [] });
    for (const fn of intervals.values()) fn();
    await flush();
    await flush();
    const narrs = deps._calls.filter((c) => c.url.startsWith("/api/narration"));
    assert.ok(narrs.some((c) => c.url.includes("after=0") && narrs.indexOf(c) === narrs.length - 1));
    const turns = findAll(globalThis.document.body, "rp-turn");
    assert.equal(turns.length, 1);
    assert.equal(findAll(globalThis.document.body, "rp-narration").length, 1);
  } finally {
    teardownGlobals();
  }
});
test("open applies the latest data", async () => {
  setupGlobals();
  try {
    const deps = makeDeps();
    deps._queues["/api/narration"].push(narration([], 0));
    deps._queues["/api/activity"].push({ steps: [] });
    const panel = createRunPanel(deps);
    panel.update({
      runs: [{ run_id: "h", label: "hist", project_id: "p", display_state: "done", model: "m" }],
    });
    panel.open("h", null);
    await flush();
    const title = find(globalThis.document.body, "run-panel")
      ? globalThis.document.body
      : globalThis.document.body;
    const titleEl = findById(globalThis.document.body, "run-panel-title");
    assert.equal(titleEl.textContent, "hist");
    const footer = find(globalThis.document.body, "rp-controls");
    assert.equal(footer.hidden, true);
    // no 2s poll interval registered (history run is not live)
    assert.equal(intervals.size, 1); // only phase timer
  } finally {
    teardownGlobals();
  }
});

function findById(root, id) {
  if (root.attributes && root.attributes.id === id) return root;
  for (const c of root.children) {
    const hit = findById(c, id);
    if (hit) return hit;
  }
  return null;
}

test("kill outcome survives the next poll", async () => {
  setupGlobals();
  try {
    const deps = makeDeps();
    deps._queues["/api/narration"].push(narration([], 0));
    deps._queues["/api/activity"].push({ steps: [] });
    const panel = createRunPanel(deps);
    panel.update({
      live: [{ run_id: "r", label: "R", project_id: "p", display_state: "running", model: "m", can_kill: true, can_steer: true }],
    });
    panel.open("r", null);
    await flush();
    deps._queues["/api/runs/"].push({ outcome: "terminated", children: 1 });
    const killBtn = find(globalThis.document.body, "rp-kill");
    killBtn.dispatch("click", {});
    await flush();
    // a poll tick
    deps._queues["/api/narration"].push(narration([], 0));
    deps._queues["/api/activity"].push({ steps: [] });
    for (const fn of intervals.values()) fn();
    await flush();
    const status = find(globalThis.document.body, "rp-action-status");
    assert.equal(status.textContent, "Engine and 1 child process terminated.");
  } finally {
    teardownGlobals();
  }
});
test("turn head gains timing", async () => {
  setupGlobals();
  try {
    const deps = makeDeps();
    deps._queues["/api/narration"].push(
      narration([{ seq: 0, kind: "narration", iteration: 1, text: "n1" }], 1),
    );
    deps._queues["/api/activity"].push({
      steps: [{ iteration: 1, started_at: "2026-01-01T00:00:00Z", model_seconds: 3 }],
    });
    const panel = createRunPanel(deps);
    panel.open("r", null);
    await flush();
    const head = find(globalThis.document.body, "rp-turn-head");
    assert.ok(head.textContent.includes("model 3s"));
  } finally {
    teardownGlobals();
  }
});

test("close stops polling", async () => {
  setupGlobals();
  try {
    const deps = makeDeps();
    deps._queues["/api/narration"].push(narration([], 0));
    deps._queues["/api/activity"].push({ steps: [] });
    const panel = createRunPanel(deps);
    panel.update({
      live: [{ run_id: "r", label: "R", project_id: "p", display_state: "running", model: "m", can_kill: true, can_steer: true }],
    });
    panel.open("r", null);
    await flush();
    assert.ok(intervals.size > 0);
    panel.close();
    assert.equal(intervals.size, 0);
  } finally {
    teardownGlobals();
  }
});

test("status events render in their turn", async () => {
  setupGlobals();
  try {
    const deps = makeDeps();
    deps._queues["/api/narration"].push(
      narration(
        [
          { seq: 0, kind: "call", iteration: 3, attempt: 1 },
          {
            seq: 1,
            kind: "status",
            iteration: 3,
            text: "reply cut by the gateway's 2-minute limit after 121 s; asking for a smaller step",
          },
        ],
        2,
      ),
    );
    deps._queues["/api/activity"].push({ steps: [] });
    const panel = createRunPanel(deps);
    panel.open("r", null);
    await flush();
    const turn = findAll(globalThis.document.body, "rp-turn").find(
      (t) => t.getAttribute("data-iteration") === "3",
    );
    assert.equal(
      find(turn, "rp-status")._text,
      "Reply cut by the gateway's 2-minute limit after 121 s; asking for a smaller step",
    );
  } finally {
    teardownGlobals();
  }
});

test("a live final report shows writing only while the run is live", async () => {
  // Review focus 1: after the run ends (or is killed), the marker goes away.
  setupGlobals();
  try {
    const deps = makeDeps();
    const final = { seq: 1, kind: "final", iteration: null, text: "half", partial: false, live: true };
    deps._queues["/api/narration"].push(
      narration([{ seq: 0, kind: "call", iteration: 1, attempt: 1 }, final], 2),
    );
    deps._queues["/api/activity"].push({ steps: [] });
    const panel = createRunPanel(deps);
    panel.open("r", null);
    await flush();
    const heading = () =>
      find(globalThis.document.body, "rp-final").children.find((c) => c.tagName === "H3");
    assert.equal(heading()._text, "Final report (writing…)");
    deps._queues["/api/narration"].push(narration([final], 2));
    deps._queues["/api/activity"].push({ steps: [] });
    panel.update({ live: [], recently_ended: [{ run_id: "r" }] });
    await flush();
    assert.equal(heading()._text, "Final report");
  } finally {
    teardownGlobals();
  }
});

test("previewHeading names the file or the final report", () => {
  assert.equal(previewHeading({ tool: "write_file", path: "src/app.py" }), "Writing src/app.py");
  assert.equal(previewHeading({ tool: "replace_in_file", path: "a.js" }), "Editing a.js");
  assert.equal(previewHeading({ tool: "finish", path: null }), "Final report");
  assert.equal(previewHeading({ tool: "write_file", path: null }), "Writing a file");
});

test("previewVisible only while the preview is the live element", () => {
  assert.equal(previewVisible({ kind: "preview", live: true }), true);
  assert.equal(previewVisible({ kind: "preview", live: false }), false);
});

test("a live preview renders text and disappears after its tool event", async () => {
  setupGlobals();
  try {
    const deps = makeDeps();
    const preview = {
      seq: 5, kind: "preview", iteration: 3,
      tool: "write_file", path: "a.py", text: "line 1\nline 2", live: true,
    };
    deps._queues["/api/narration"].push(narration([preview], 6));
    deps._queues["/api/activity"].push({ steps: [] });
    const panel = createRunPanel(deps);
    panel.update({ live: [{ run_id: "r" }] });
    panel.open("r", null);
    await flush();
    const timeline = find(globalThis.document.body, "rp-turn");
    const rendered = find(timeline, "rp-preview");
    assert.ok(rendered);
    assert.equal(find(rendered, "rp-preview-heading").textContent, "Writing a.py (writing…)");
    const pre = find(rendered, "rp-preview-text");
    assert.equal(pre.tagName, "PRE");
    assert.equal(pre.textContent, "line 1\nline 2");

    deps._queues["/api/narration"].push(narration([
      { ...preview, live: false },
      { seq: 6, kind: "tool", iteration: 3, name: "write_file", args: "{}", output: "ok" },
    ], 7));
    deps._queues["/api/activity"].push({ steps: [] });
    for (const fn of intervals.values()) fn();
    await flush();
    assert.equal(findAll(globalThis.document.body, "rp-preview").length, 0);
    assert.equal(findAll(globalThis.document.body, "rp-tool").length, 1);
  } finally {
    teardownGlobals();
  }
});

test("a run killed mid-preview keeps the text without the writing marker", async () => {
  setupGlobals();
  try {
    const deps = makeDeps();
    const preview = {
      seq: 5, kind: "preview", iteration: 3,
      tool: "write_file", path: "a.py", text: "line 1\nline 2", live: true,
    };
    deps._queues["/api/narration"].push(narration([preview], 6));
    deps._queues["/api/activity"].push({ steps: [] });
    const panel = createRunPanel(deps);
    panel.update({ live: [{ run_id: "r" }] });
    panel.open("r", null);
    await flush();
    assert.equal(find(globalThis.document.body, "rp-preview-heading").textContent, "Writing a.py (writing…)");

    deps._queues["/api/narration"].push(narration([preview], 6));
    deps._queues["/api/activity"].push({ steps: [] });
    panel.update({ live: [], recently_ended: [{ run_id: "r" }] });
    await flush();
    const rendered = find(globalThis.document.body, "rp-preview");
    assert.ok(rendered);
    assert.equal(find(rendered, "rp-preview-heading").textContent, "Writing a.py");
    assert.equal(find(rendered, "rp-preview-text").textContent, "line 1\nline 2");
  } finally {
    teardownGlobals();
  }
});

test("the writing marker clears when the run ends even if the last fetch fails", async () => {
  setupGlobals();
  try {
    const deps = makeDeps();
    const final = { seq: 1, kind: "final", iteration: null, text: "half", partial: false, live: true };
    deps._queues["/api/narration"].push(
      narration([{ seq: 0, kind: "call", iteration: 1, attempt: 1 }, final], 2),
    );
    deps._queues["/api/activity"].push({ steps: [] });
    const panel = createRunPanel(deps);
    panel.open("r", null);
    await flush();
    const heading = () =>
      find(globalThis.document.body, "rp-final").children.find((c) => c.tagName === "H3");
    assert.equal(heading()._text, "Final report (writing…)");
    deps._queues["/api/narration"].push(new Error("network down"));
    deps._queues["/api/activity"].push({ steps: [] });
    panel.update({ live: [], recently_ended: [{ run_id: "r" }] });
    await flush();
    assert.equal(heading()._text, "Final report");
  } finally {
    teardownGlobals();
  }
});

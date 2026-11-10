import test from "node:test";
import assert from "node:assert/strict";
import { fileURLToPath, pathToFileURL } from "node:url";
import { dirname, resolve } from "node:path";

const here = dirname(fileURLToPath(import.meta.url));
const modulePath = pathToFileURL(resolve(here, "../../web/dashboard.js")).href;
const {
  formatCounter,
  formatCounterTotal,
  formatCost,
  sortSpendGroups,
  buildQuery,
  mutationHeaders,
  candidateFingerprint,
  canAppendPricing,
  staleNote,
  countColumns,
  formatElapsed,
  iterationText,
  describeDoing,
  burnText,
  liveStateClass,
  phaseText,
  stepTimingText,
  endedAgoText,
  serverIsOutdated,
  elapsedOf,
  toolLabel,
  toolSessionRowModel,
  TOOL_IDS,
  createReloadQueue,
  runStatusText,
} = await import(modulePath);

test("runStatusText names known incomplete causes and hides unknown text", () => {
  assert.equal(runStatusText({ status: "failed", stop_reason: "max_iters" }),
    "incomplete: turn limit");
  assert.equal(runStatusText({ status: "failed", stop_reason: "repair_limit" }),
    "incomplete: repair limit");
  assert.equal(runStatusText({ status: "failed", stop_reason: "no_action_limit" }),
    "incomplete: no tool action");
  assert.equal(runStatusText({ status: "failed", stop_reason: "mutation_outcome_unknown" }),
    "incomplete: write outcome unknown");
  assert.equal(runStatusText({ status: "failed", stop_reason: "context_budget" }),
    "incomplete: context budget");
  assert.equal(runStatusText({ status: "failed", stop_reason: "recovery_state" }),
    "incomplete: recovery state");
  assert.equal(runStatusText({ status: "complete", stop_reason: "recovery_state" }), "complete");
  assert.equal(runStatusText({ status: "interrupted", stop_reason: "recovery_state" }),
    "interrupted");
  assert.equal(runStatusText({ status: "complete", stop_reason: "context_budget" }), "complete");
  assert.equal(runStatusText({ status: "interrupted", stop_reason: "context_budget" }),
    "interrupted");
  assert.equal(runStatusText({ status: "failed", stop_reason: "<img src=x>" }), "failed");
  assert.equal(runStatusText({ status: "failed", stop_reason: "toString" }), "failed");
  assert.equal(runStatusText({ status: "failed" }), "failed");
  assert.equal(runStatusText({ status: "complete", stop_reason: "max_iters" }), "complete");
  assert.equal(runStatusText({ status: "interrupted", stop_reason: null }), "interrupted");
});

test("toolLabel maps every tool id", () => {
  assert.equal(toolLabel("engine"), "Harness agents");
  assert.equal(toolLabel("claude_code"), "Claude Code");
  assert.equal(toolLabel("codex"), "Codex");
  assert.equal(toolLabel("cline"), "Cline");
  assert.equal(toolLabel("kilo"), "Kilo");
  assert.deepEqual(TOOL_IDS, ["engine", "claude_code", "codex", "cline", "kilo"]);
  assert.equal(toolLabel("other"), "other");
});

const NOW = Date.parse("2026-09-17T12:00:00Z");

function toolRow(extra) {
  return {
    tool: "codex",
    session: "root-1",
    project_id: "p1",
    project_name: "Demo",
    included_in_totals: true,
    models: ["gpt-a", "gpt-b"],
    first_at: "2026-09-17T11:50:00+00:00",
    last_at: "2026-09-17T11:58:00+00:00",
    calls: 3,
    subagents: 2,
    tokens_sent: 1200,
    tokens_in: 1200,
    tokens_returned: 300,
    cache_read: null,
    cache_write: null,
    reasoning: null,
    cost: 0.25,
    cost_classification: "exact",
    burn_per_hour: { cost: 0.5, tokens_returned: 300 },
    ...extra,
  };
}

test("tool session row model is watch only", () => {
  const m = toolSessionRowModel(toolRow({}), NOW);
  assert.equal(m.badge, "Codex");
  assert.equal(m.project, "Demo");
  assert.equal(m.models, "gpt-a, gpt-b");
  assert.equal(m.lastCall, "2m ago");
  assert.equal(m.calls, "3 calls");
  assert.equal(m.tokens, "1,200 in \u00b7 300 out");
  assert.equal(m.cost.text, "$0.2500");
  assert.equal(m.burn, "~$0.50/h");
  assert.equal(m.subagents, "2 subagents");
  assert.equal(m.note, "Watch only. This session runs outside AgentControl.");
  assert.equal(m.excluded, false);
  for (const flag of ["steer", "kill", "dismiss", "opensPanel"]) {
    assert.equal(m[flag], false, flag);
  }
});

test("tool session row model marks excluded rows and hides zero subagents", () => {
  const m = toolSessionRowModel(
    toolRow({ included_in_totals: false, subagents: 0, cost: null,
      cost_classification: "missing_rate", burn_per_hour: { cost: null, tokens_returned: 0 },
      models: [], calls: 1, tokens_sent: null, tokens_in: null }),
    NOW,
  );
  assert.equal(m.project, "not in totals");
  assert.equal(m.excluded, true);
  assert.equal(m.subagents, "");
  assert.equal(m.cost.text, "\u2014");
  assert.equal(m.burn, "\u2014");
  assert.equal(m.models, "\u2014");
  assert.equal(m.calls, "1 call");
  assert.equal(m.tokens, "\u2014 in \u00b7 300 out");
  assert.equal(toolSessionRowModel(toolRow({ subagents: 1 }), NOW).subagents, "1 subagent");
});

test("tool session row tokens show total input, cached and output", () => {
  const m = toolSessionRowModel(
    toolRow({ tokens_sent: 1580, tokens_in: 1770001580, cache_read: 1770000000,
      tokens_returned: 42000 }),
    NOW,
  );
  assert.equal(m.tokens, "1,770,001,580 in \u00b7 1,770,000,000 cached \u00b7 42,000 out");
});

test("tool session row tokens omit cached part when cache_read is null", () => {
  const m = toolSessionRowModel(
    toolRow({ tokens_in: 1200, cache_read: null, tokens_returned: null }),
    NOW,
  );
  assert.equal(m.tokens, "1,200 in \u00b7 \u2014 out");
  const zero = toolSessionRowModel(toolRow({ tokens_in: 5, cache_read: 0 }), NOW);
  assert.equal(zero.tokens, "5 in \u00b7 0 cached \u00b7 300 out");
});

test("query builder sends the tool filter", () => {
  const query = buildQuery({ tool: ["kilo", "codex"] });
  assert.match(query, /tool=kilo/);
  assert.match(query, /tool=codex/);
  assert.equal(buildQuery({}), "");
});

test("unknown and measured zero stay distinct", () => {
  assert.equal(formatCounter(null).text, "\u2014");
  assert.equal(formatCounter(0).text, "0");
});

test("partial totals render as a lower bound", () => {
  const partial = formatCounterTotal({ known_total: 10, unknown_rows: 1, partial: true });
  assert.match(partial.text, /^\u2265 /);
  const complete = formatCounterTotal({ known_total: 10, unknown_rows: 0, partial: false });
  assert.equal(complete.text, "10");
});

test("upper-bound cost is marked with less-or-equal", () => {
  assert.match(formatCost(0.5, "upper_bound").text, /^\u2264 /);
  assert.equal(formatCost(null, "missing_rate").text, "\u2014");
  assert.equal(formatCost(0.5, "exact").text, "$0.5000");
});

test("day groups sort newest first, categories by cost", () => {
  const days = sortSpendGroups("by_day", [
    { day: "2026-09-15", known_cost: 9 },
    { day: "2026-09-17", known_cost: 1 },
  ]);
  assert.deepEqual(days.map((d) => d.day), ["2026-09-17", "2026-09-15"]);

  const models = sortSpendGroups("by_model", [
    { key: "b", known_cost: 1 },
    { key: "a", known_cost: 5 },
  ]);
  assert.deepEqual(models.map((m) => m.key), ["a", "b"]);
});

test("category ties break by name ascending", () => {
  const models = sortSpendGroups("by_model", [
    { key: "b", known_cost: 5 },
    { key: "a", known_cost: 5 },
  ]);
  assert.deepEqual(models.map((m) => m.key), ["a", "b"]);
});

test("query builder encodes multi-value filters", () => {
  const query = buildQuery({ project: ["P1", "P2"], model: ["m"], include_excluded: true });
  assert.match(query, /project=P1/);
  assert.match(query, /project=P2/);
  assert.match(query, /include_excluded=true/);
});

test("mutation headers carry CSRF and JSON type", () => {
  const headers = mutationHeaders("token");
  assert.equal(headers["Content-Type"], "application/json");
  assert.equal(headers["X-AgentControl-CSRF"], "token");
});

test("changed pricing candidate invalidates append", () => {
  const fingerprint = candidateFingerprint({
    model: "m",
    effective_at: "2026-01-01T00:00:00+00:00",
    rates: { input: 1, output: 1, cache_read: 0, cache_write: 0 },
  });
  assert.equal(
    canAppendPricing({ previewFingerprint: fingerprint, candidateFingerprint: fingerprint }),
    true,
  );
  assert.equal(
    canAppendPricing({ previewFingerprint: fingerprint, candidateFingerprint: "changed" }),
    false,
  );
});

test("staleNote handles never-loaded, seconds and minutes", () => {
  assert.match(staleNote(null, 1000, "HTTP 500: sync_failed"), /^Live data never loaded/);
  const secs = staleNote(1000, 1000 + 42 * 1000, "HTTP 500: sync_failed");
  assert.equal(secs, "Live data is 42 sec old; refresh failing (HTTP 500: sync_failed)");
  const mins = staleNote(1000, 1000 + 2 * 60 * 1000, "HTTP 500: sync_failed");
  assert.equal(mins, "Live data is 2 min old; refresh failing (HTTP 500: sync_failed)");
});

test("countColumns counts th cells", () => {
  assert.equal(countColumns("<tr><th></th><th>a</th><th>b</th></tr>"), 3);
  assert.equal(countColumns("<tr></tr>"), 0);
});

test("formatElapsed renders seconds, minutes, hours and null", () => {
  assert.equal(formatElapsed(null), "\u2014");
  assert.equal(formatElapsed(45), "45s");
  assert.equal(formatElapsed(12 * 60), "12m");
  assert.equal(formatElapsed(2 * 3600 + 5 * 60), "2h 05m");
});

test("iterationText handles both, one and neither", () => {
  assert.equal(iterationText(13, 40), "13 / 40");
  assert.equal(iterationText(13, null), "13");
  assert.equal(iterationText(null, null), "\u2014");
});

test("describeDoing follows the documented priority", () => {
  assert.equal(describeDoing({ dismissed: true, phase: null }), "dismissed; still running");
  assert.equal(
    describeDoing({ phase: "model_call", attempt: 3, max_attempts: 4 }),
    "retrying model call (attempt 3/4)",
  );
  assert.equal(describeDoing({ nudges: 2 }), "no tool call \u00d72");
  assert.equal(describeDoing({ nudges: 1 }), "no tool call");
  assert.equal(describeDoing({ phase: "model_call" }), "waiting on model");
  assert.equal(
    describeDoing({ phase: "tools", last_tool: "read_file", last_path: "src/a.py" }),
    "tools \u00b7 last: Reading src/a.py",
  );
  assert.equal(describeDoing({ phase: "tools" }), "tools");
  assert.equal(describeDoing({ phase: "wrapping_up" }), "wrapping up");
  assert.equal(describeDoing({ phase: "starting" }), "starting");
  assert.equal(describeDoing({ phase: "finished" }), "finishing");
  assert.equal(describeDoing({ phase: null }), "\u2014");
});

test("burnText summarises live totals", () => {
  assert.equal(burnText({ runs: 0 }), "No live runs");
  assert.equal(
    burnText({ runs: 3, known_cost: 0.42, burn_per_hour: 1.1 }),
    "3 live \u00b7 $0.42 so far \u00b7 ~$1.10/h",
  );
  assert.equal(
    burnText({ runs: 3, known_cost: 0.42, burn_per_hour: null }),
    "3 live \u00b7 $0.42 so far",
  );
  assert.equal(burnText({ runs: 2 }), "2 live");
});

test("serverIsOutdated detects payloads missing live keys", () => {
  assert.equal(serverIsOutdated({ live_totals: {}, recently_ended: [] }), false);
  assert.equal(serverIsOutdated({ recently_ended: [] }), true);
  assert.equal(serverIsOutdated({ live_totals: {} }), true);
  assert.equal(serverIsOutdated(null), false);
});

test("elapsedOf prefers the number, falls back to the timestamp", () => {
  assert.equal(elapsedOf({ elapsed_seconds: 12 }, 0), 12);
  assert.equal(
    elapsedOf({ timestamp: "2024-01-01T00:00:00Z" }, Date.parse("2024-01-01T00:00:30Z")),
    30,
  );
  assert.equal(elapsedOf({ timestamp: "not a date" }, 0), null);
  assert.equal(elapsedOf({}, 0), null);
});

test("liveStateClass maps states to badge classes", () => {
  assert.equal(liveStateClass("running"), "ok");
  assert.equal(liveStateClass("retrying"), "warn");
  assert.equal(liveStateClass("quiet"), "warn");
  assert.equal(liveStateClass("unverified"), "muted");
});

test("phaseText describes each phase honestly", () => {
  assert.equal(phaseText(null, 0, null, null), "");
  assert.equal(phaseText("model_call", 130, null, null), "waiting on model 2m 10s");
  assert.equal(phaseText("model_call", 130, 3, 4), "waiting on model 2m 10s (attempt 3/4)");
  assert.equal(phaseText("model_call", 130, 1, 4), "waiting on model 2m 10s");
  assert.equal(phaseText("tools", 3.2, null, null), "running tools 3.2s");
  assert.equal(phaseText("wrapping_up", 5, null, null), "wrapping up");
  assert.equal(phaseText("starting", 5, null, null), "starting");
  assert.equal(phaseText("finished", 5, null, null), "finished");
});

test("stepTimingText prefers model/tool split, falls back to duration", () => {
  assert.equal(
    stepTimingText({ model_seconds: 42, tool_seconds: 0.3 }),
    "model 42.0s \u00b7 tools 0.3s",
  );
  assert.equal(stepTimingText({ model_seconds: 42, tool_seconds: null }), "model 42.0s");
  assert.equal(stepTimingText({ model_seconds: null, tool_seconds: 0.3 }), "tools 0.3s");
  assert.equal(
    stepTimingText({ model_seconds: null, tool_seconds: null, duration_seconds: 18 }),
    "18.0s until next reply",
  );
  assert.equal(
    stepTimingText({ model_seconds: null, tool_seconds: null, duration_seconds: null }),
    "",
  );
});

test("endedAgoText renders just now, seconds, minutes, hours and unknown", () => {
  const now = 1_000_000_000_000;
  const iso = (msAgo) => new Date(now - msAgo).toISOString();
  assert.equal(endedAgoText(iso(2000), now), "just now");
  assert.equal(endedAgoText(iso(40 * 1000), now), "40s ago");
  assert.equal(endedAgoText(iso(4 * 60 * 1000), now), "4m ago");
  assert.equal(endedAgoText(iso(65 * 60 * 1000), now), "1h 05m ago");
  assert.equal(endedAgoText(null, now), "\u2014");
  assert.equal(endedAgoText("not-a-date", now), "\u2014");
});

// --- createReloadQueue -------------------------------------------------

function deferred() {
  let resolve;
  let reject;
  const promise = new Promise((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

const flush = async () => {
  for (let i = 0; i < 10; i++) await Promise.resolve();
};

// Each run waits on its own deferred, then records whether it would apply.
function fakeLoader() {
  const runs = [];
  const queue = createReloadQueue(async (isCurrent) => {
    const gate = deferred();
    const entry = { gate, applied: null };
    runs.push(entry);
    await gate.promise;
    entry.applied = isCurrent();
  });
  return { runs, queue };
}

const tracked = (promise) => {
  const t = { settled: false };
  promise.then(() => (t.settled = true));
  return t;
};

test("createReloadQueue: idle request runs once and resolves after it settles", async () => {
  const { runs, queue } = fakeLoader();
  const p = tracked(queue.request());
  await flush();
  assert.equal(runs.length, 1);
  assert.equal(p.settled, false);
  runs[0].gate.resolve();
  await flush();
  assert.equal(p.settled, true);
  assert.equal(runs[0].applied, true);
  assert.equal(runs.length, 1);
});

test("createReloadQueue: non-fresh requests during a flight coalesce into one queued run", async () => {
  const { runs, queue } = fakeLoader();
  const first = tracked(queue.request());
  const pa = queue.request();
  const pb = queue.request();
  const pc = queue.request();
  assert.equal(pa, pb);
  assert.equal(pb, pc);
  const queued = tracked(pa);
  await flush();
  assert.equal(runs.length, 1);
  runs[0].gate.resolve();
  await flush();
  assert.equal(runs[0].applied, true);
  assert.equal(first.settled, true);
  assert.equal(runs.length, 2);
  assert.equal(queued.settled, false);
  runs[1].gate.resolve();
  await flush();
  assert.equal(queued.settled, true);
  assert.equal(runs[1].applied, true);
  await flush();
  assert.equal(runs.length, 2);
});

test("createReloadQueue: fresh request supersedes the in-flight run and waits for the queued one", async () => {
  const { runs, queue } = fakeLoader();
  const first = tracked(queue.request());
  await flush();
  const fresh = tracked(queue.request({ fresh: true }));
  runs[0].gate.resolve();
  await flush();
  assert.equal(runs[0].applied, false);
  assert.equal(first.settled, true);
  assert.equal(runs.length, 2);
  assert.equal(fresh.settled, false);
  runs[1].gate.resolve();
  await flush();
  assert.equal(runs[1].applied, true);
  assert.equal(fresh.settled, true);
});

test("createReloadQueue: a throwing run still lets the queued run happen and all promises resolve", async () => {
  const { runs, queue } = fakeLoader();
  const first = queue.request();
  const second = queue.request({ fresh: true });
  await flush();
  runs[0].gate.reject(new Error("boom"));
  await flush();
  assert.equal(runs.length, 2);
  runs[1].gate.reject(new Error("boom again"));
  await assert.doesNotReject(first);
  await assert.doesNotReject(second);
  assert.equal(await first, undefined);
  // The queue is still usable afterwards.
  const third = queue.request();
  await flush();
  assert.equal(runs.length, 3);
  runs[2].gate.resolve();
  await third;
  assert.equal(runs[2].applied, true);
});

test("costFeatureEnabled is off unless the server explicitly enables it", async () => {
  const { costFeatureEnabled, COST_DISABLED_NOTE } = await import(modulePath);
  assert.equal(costFeatureEnabled(undefined), false);
  assert.equal(costFeatureEnabled({}), false);
  assert.equal(costFeatureEnabled({ features: {} }), false);
  assert.equal(costFeatureEnabled({ features: { cost: false } }), false);
  assert.equal(costFeatureEnabled({ features: { cost: "true" } }), false);
  assert.equal(costFeatureEnabled({ features: { cost: true } }), true);
  assert.match(COST_DISABLED_NOTE, /disabled/i);
  assert.match(COST_DISABLED_NOTE, /not activated/i);
});

test("money columns render nothing when cost data is absent", () => {
  assert.equal(formatCost(undefined, undefined).text, "\u2014");
  assert.equal(burnText({ runs: 2 }), "2 live");
});

// AgentControl dashboard. Built on the proven legacy agent-harness dashboard,
// adapted to the central control plane: multi-project totals, project approval,
// CSRF-protected mutations, and the central pricing catalog.
//
// Pure helpers are exported for Node's test runner; DOM wiring runs in browsers.

import { createRunPanel, runIdFromHash } from "./panel.js";
import { renderGoalTasksHtml } from "./task-state.js";

export function formatCounter(value) {
  if (value === null || value === undefined) {
    return { text: "\u2014", title: "Not reported by the gateway" };
  }
  return { text: Number(value).toLocaleString(), title: "" };
}

export function formatCounterTotal(summary) {
  if (!summary || summary.known_total === null || summary.known_total === undefined) {
    return { text: "\u2014", title: "No measured values" };
  }
  const base = Number(summary.known_total).toLocaleString();
  if (summary.partial) {
    return {
      text: `\u2265 ${base}`,
      title: `${summary.unknown_rows} run(s) did not report this counter`,
    };
  }
  return { text: base, title: "" };
}

// Cost estimation is experimental and disabled in this build. The server
// reports features.cost; anything other than an explicit true keeps it off.
export const COST_DISABLED_NOTE =
  "Cost estimation: disabled (experimental, not activated in this build). Token counts are shown instead.";

export function costFeatureEnabled(data) {
  return !!(data && data.features && data.features.cost === true);
}

export function formatCost(value, classification) {
  if (value === null || value === undefined) {
    const reason =
      classification === "missing_rate"
        ? "No rate set for this model"
        : "Usage not reported";
    return { text: "\u2014", title: reason };
  }
  const amount = `$${Number(value).toFixed(4)}`;
  if (classification === "upper_bound") {
    return { text: `\u2264 ${amount}`, title: "Upper bound; cache usage unknown" };
  }
  return { text: amount, title: "" };
}

export function sortSpendGroups(kind, groups) {
  const copy = [...groups];
  if (kind === "by_day") {
    copy.sort((a, b) => (a.day < b.day ? 1 : a.day > b.day ? -1 : 0));
    return copy;
  }
  copy.sort((a, b) => {
    if (b.known_cost !== a.known_cost) {
      return b.known_cost - a.known_cost;
    }
    return a.key < b.key ? -1 : a.key > b.key ? 1 : 0;
  });
  return copy;
}

export function buildQuery(filters) {
  const params = new URLSearchParams();
  for (const project of filters.project || []) params.append("project", project);
  for (const model of filters.model || []) params.append("model", model);
  for (const task of filters.task || []) params.append("task", task);
  for (const state of filters.state || []) params.append("state", state);
  for (const tool of filters.tool || []) params.append("tool", tool);
  if (filters.date_from) params.set("date_from", filters.date_from);
  if (filters.date_to) params.set("date_to", filters.date_to);
  if (filters.include_excluded) params.set("include_excluded", "true");
  return params.toString();
}

export function mutationHeaders(token) {
  return { "Content-Type": "application/json", "X-AgentControl-CSRF": token };
}

export function candidateFingerprint(candidate) {
  return JSON.stringify({
    model: candidate.model,
    effective_at: candidate.effective_at,
    rates: candidate.rates,
  });
}

export function canAppendPricing(state) {
  return (
    Boolean(state.previewFingerprint) &&
    state.previewFingerprint === state.candidateFingerprint
  );
}

export function staleNote(lastOkAtMs, nowMs, message) {
  if (lastOkAtMs === null || lastOkAtMs === undefined) {
    return `Live data never loaded; refresh failing (${message})`;
  }
  const seconds = Math.max(0, Math.floor((nowMs - lastOkAtMs) / 1000));
  const age = seconds < 60 ? `${seconds} sec` : `${Math.floor(seconds / 60)} min`;
  return `Live data is ${age} old; refresh failing (${message})`;
}

export function countColumns(headHtml) {
  return (String(headHtml).match(/<th/g) || []).length;
}

// Serialise loads. `run(isCurrent)` performs one load and must skip applying
// its results when isCurrent() is false after its awaits. request() starts a
// run when idle; during a flight it queues exactly one follow-up run (shared
// by every request made in that flight). `fresh: true` also supersedes the
// in-flight run. Returned promises resolve (never reject) once the run that
// serves the request has settled; a throwing run does not break the queue.
export function createReloadQueue(run) {
  let inFlight = null; // { current: boolean } for the running run
  let queued = null; // { token, promise, resolve } for the follow-up run

  function start(token) {
    inFlight = token;
    let settled;
    try {
      settled = Promise.resolve(run(() => token.current));
    } catch {
      settled = Promise.resolve();
    }
    return settled.then(
      () => undefined,
      () => undefined,
    ).then(() => {
      inFlight = null;
      if (queued) {
        const next = queued;
        queued = null;
        start(next.token).then(next.resolve);
      }
    });
  }

  function request({ fresh = false } = {}) {
    if (!inFlight) return start({ current: true });
    if (fresh) inFlight.current = false;
    if (!queued) {
      let resolve;
      const promise = new Promise((r) => {
        resolve = r;
      });
      queued = { token: { current: true }, promise, resolve };
    }
    return queued.promise;
  }

  return { request };
}

// Verb map shared by the activity drawer and the Live now "Doing" cell.
export const TOOL_VERB = {
  read_file: "Reading",
  write_file: "Writing",
  replace_in_file: "Editing",
  apply_diff: "Editing",
  insert_content: "Editing",
  list_dir: "Listing",
  list_files: "Listing",
  search_files: "Searching",
  run_command: "Running a command",
  grep: "Searching",
  thinking: "Thinking\u2026",
};

export function formatElapsed(seconds) {
  if (seconds == null || !Number.isFinite(seconds)) return "\u2014";
  const total = Math.round(seconds);
  if (total < 60) return total + "s";
  if (total < 3600) return Math.floor(total / 60) + "m";
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  return `${h}h ${String(m).padStart(2, "0")}m`;
}

export function iterationText(iteration, maxIters) {
  if (iteration == null && maxIters == null) return "\u2014";
  if (iteration == null) return "\u2014";
  if (maxIters == null) return String(iteration);
  return `${iteration} / ${maxIters}`;
}

export function describeDoing(row) {
  const nudges = row.nudges || 0;
  if (row.phase === "model_call" && row.attempt != null && row.attempt >= 2) {
    return `retrying model call (attempt ${row.attempt}/${row.max_attempts})`;
  }
  if (nudges >= 1) {
    return nudges > 1 ? `no tool call \u00d7${nudges}` : "no tool call";
  }
  switch (row.phase) {
    case "model_call":
      return "waiting on model";
    case "tools": {
      if (!row.last_tool) return "tools";
      const verb = TOOL_VERB[row.last_tool] || row.last_tool;
      const label = row.last_path ? `${verb} ${row.last_path}` : verb;
      return `tools \u00b7 last: ${label}`;
    }
    case "wrapping_up":
      return "wrapping up";
    case "starting":
      return "starting";
    case "finished":
      return "finishing";
    default:
      return row.dismissed ? "dismissed; still running" : "\u2014";
  }
}

export function burnText(liveTotals) {
  const t = liveTotals || {};
  if (!t.runs) return "No live runs";
  // A skewed server sends only the row count (via render's fallback); show the
  // count alone rather than inventing a $0.00 total we do not actually know.
  if (t.known_cost == null) return `${t.runs} live`;
  const cost = `$${Number(t.known_cost || 0).toFixed(2)}`;
  let text = `${t.runs} live \u00b7 ${cost} so far`;
  if (t.burn_per_hour != null) {
    text += ` \u00b7 ~$${Number(t.burn_per_hour).toFixed(2)}/h`;
  }
  return text;
}

// True when the server is running older Python than this page: current servers
// always send live_totals and recently_ended, so a payload missing either was
// built by stale code and cannot supply live state, elapsed time or activity.
export function serverIsOutdated(data) {
  if (!data || typeof data !== "object") return false;
  return !("live_totals" in data) || !("recently_ended" in data);
}

// Elapsed seconds for a live row: prefer the server's measured value, else
// derive it from the row timestamp, else null (unknown, not zero).
export function elapsedOf(row, nowMs) {
  if (typeof row.elapsed_seconds === "number") return row.elapsed_seconds;
  const started = Date.parse(row.timestamp);
  if (!Number.isNaN(started)) return Math.max(0, (nowMs - started) / 1000);
  return null;
}

export function liveStateClass(state) {
  if (state === "running") return "ok";
  if (state === "retrying" || state === "quiet") return "warn";
  return "muted";
}

// Duration formatting shared with the activity drawer: "3.2s" under a minute,
// "2m 10s" above it.
function _durText(seconds) {
  if (seconds == null || !Number.isFinite(seconds)) return "";
  if (seconds < 60) return seconds.toFixed(1) + "s";
  const total = Math.round(seconds);
  const m = Math.floor(total / 60);
  const s = total % 60;
  return `${m}m ${s}s`;
}

export function phaseText(phase, seconds, attempt, maxAttempts) {
  if (phase == null) return "";
  switch (phase) {
    case "model_call": {
      let text = `waiting on model ${_durText(seconds)}`;
      if (attempt != null && attempt >= 2) {
        text += ` (attempt ${attempt}/${maxAttempts})`;
      }
      return text;
    }
    case "tools":
      return `running tools ${_durText(seconds)}`;
    case "wrapping_up":
      return "wrapping up";
    case "starting":
      return "starting";
    case "finished":
      return "finished";
    default:
      return "";
  }
}

export function stepTimingText(step) {
  const model = step.model_seconds;
  const tool = step.tool_seconds;
  if (model != null || tool != null) {
    const bits = [];
    if (model != null) bits.push(`model ${Number(model).toFixed(1)}s`);
    if (tool != null) bits.push(`tools ${Number(tool).toFixed(1)}s`);
    return bits.join(" \u00b7 ");
  }
  if (step.duration_seconds != null) {
    return `${Number(step.duration_seconds).toFixed(1)}s until next reply`;
  }
  return "";
}

export function endedAgoText(iso, nowMs) {
  if (!iso) return "\u2014";
  const then = Date.parse(iso);
  if (!Number.isFinite(then)) return "\u2014";
  const secs = Math.max(0, (nowMs - then) / 1000);
  if (secs < 5) return "just now";
  if (secs < 60) return `${Math.floor(secs)}s ago`;
  if (secs < 3600) return `${Math.floor(secs / 60)}m ago`;
  const h = Math.floor(secs / 3600);
  const m = Math.floor((secs % 3600) / 60);
  return `${h}h ${String(m).padStart(2, "0")}m ago`;
}

// Tool ids and the names shown for them (spec §9).
// Engine incomplete-terminal causes (status stays "failed"); unknown codes never render.
const STOP_REASON_TEXT = Object.freeze({
  max_iters: "turn limit",
  repair_limit: "repair limit",
  no_action_limit: "no tool action",
  mutation_outcome_unknown: "write outcome unknown",
  context_budget: "context budget",
  recovery_state: "recovery state",
});

export function runStatusText(row) {
  const reason = row.stop_reason;
  if (row.status === "failed" && typeof reason === "string" &&
      Object.prototype.hasOwnProperty.call(STOP_REASON_TEXT, reason)) {
    return "incomplete: " + STOP_REASON_TEXT[reason];
  }
  return row.status;
}

export const TOOL_IDS = ["engine", "claude_code", "codex", "cline", "kilo"];
const TOOL_LABELS = {
  engine: "Harness agents",
  claude_code: "Claude Code",
  codex: "Codex",
  cline: "Cline",
  kilo: "Kilo",
};
export const WATCH_ONLY_NOTE = "Watch only. This session runs outside AgentControl.";

export function toolLabel(id) {
  return Object.prototype.hasOwnProperty.call(TOOL_LABELS, id) ? TOOL_LABELS[id] : String(id ?? "");
}

function _plural(n, one, many) {
  return `${Number(n).toLocaleString()} ${n === 1 ? one : many}`;
}

// Plain-text view of one tool session row. A tool session runs outside
// AgentControl, so it offers no controls and never opens the run panel.
export function toolSessionRowModel(row, nowMs) {
  const r = row || {};
  const included = r.included_in_totals === true;
  const models = Array.isArray(r.models) && r.models.length ? r.models.join(", ") : "\u2014";
  // tokens_in is total input including cache, whatever the provider's shape.
  const input = formatCounter(r.tokens_in).text;
  const cached = r.cache_read == null ? "" : ` \u00b7 ${formatCounter(r.cache_read).text} cached`;
  const returned = formatCounter(r.tokens_returned).text;
  const burnCost = r.burn_per_hour ? r.burn_per_hour.cost : null;
  const subagents = Number(r.subagents) || 0;
  return {
    badge: toolLabel(r.tool),
    project: included ? r.project_name || r.project_id || "\u2014" : "not in totals",
    excluded: !included,
    models,
    lastCall: endedAgoText(r.last_at, nowMs),
    calls: _plural(Number(r.calls) || 0, "call", "calls"),
    tokens: `${input} in${cached} \u00b7 ${returned} out`,
    cost: formatCost(r.cost, r.cost_classification),
    burn: burnCost == null ? "\u2014" : `~$${Number(burnCost).toFixed(2)}/h`,
    subagents: subagents > 0 ? _plural(subagents, "subagent", "subagents") : "",
    note: WATCH_ONLY_NOTE,
    steer: false,
    kill: false,
    dismiss: false,
    opensPanel: false,
  };
}

// Plain-text rows for a period's `by_tool` breakdown, in TOOL_IDS order.
export function byToolRows(byTool) {
  const map = byTool || {};
  return TOOL_IDS.filter((id) => map[id]).map((id) => {
    const entry = map[id];
    const unit = id === "engine" ? ["run", "runs"] : ["call", "calls"];
    let count = _plural(Number(entry.count) || 0, unit[0], unit[1]);
    if (entry.sessions != null) count += ` \u00b7 ${_plural(entry.sessions, "session", "sessions")}`;
    return { name: toolLabel(id), count, cost: `$${Number(entry.known_cost || 0).toFixed(2)}` };
  });
}

// --- browser-only below ------------------------------------------------

if (typeof document === "undefined") {
  // Imported by the Node test runner; skip all DOM wiring.
} else {
  const $ = (id) => document.getElementById(id);
  const esc = (x) =>
    String(x ?? "\u2014").replace(/[&<>"']/g, (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c],
    );
  const money = (x) =>
    x == null ? "rate not set" : "$" + Number(x).toLocaleString(undefined, { maximumFractionDigits: 4 });
  const dur = (isoStart, isoEnd) => {
    if (!isoStart || !isoEnd) return "\u2014";
    const secs = (Date.parse(isoEnd) - Date.parse(isoStart)) / 1000;
    if (!Number.isFinite(secs) || secs < 0) return "\u2014";
    if (secs < 3600) return (secs / 60).toFixed(1) + " min";
    const s = Math.floor(secs);
    const pad = (n) => String(n).padStart(2, "0");
    return pad(Math.floor(s / 3600)) + ":" + pad(Math.floor((s % 3600) / 60)) + ":" + pad(s % 60);
  };
  // Timestamps are stored/served in UTC (ISO with +00:00). Render them in the
  // viewer's local timezone so times match the wall clock the operator sees.
  const _tzShort = (() => {
    try {
      const parts = new Intl.DateTimeFormat(undefined, { timeZoneName: "short" }).formatToParts(new Date());
      const tz = parts.find((p) => p.type === "timeZoneName");
      return tz ? tz.value : "";
    } catch {
      return "";
    }
  })();
  const when = (iso) => {
    if (!iso) return "\u2014";
    const d = new Date(iso);
    if (!Number.isFinite(d.getTime())) return iso.replace("T", " ").replace("+00:00", "");
    const p = (n) => String(n).padStart(2, "0");
    return (
      `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ` +
      `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`
    );
  };

  const dash = {
    data: null,
    projects: [],
    diagnostics: [],
    names: {},
    token: "",
    lastOkAt: null,
    hashRestored: false,
  };
  const pendingActions = new Set();

  // Fetch JSON with an ok-check. On a non-OK response, throw an Error whose
  // message carries the HTTP status and the server error.code when the body is
  // the standard {"error": {"code", "message"}} shape.
  async function fetchJson(url, options) {
    const response = await fetch(url, options);
    if (!response.ok) {
      let code = "";
      let message = "";
      try {
        const body = await response.json();
        if (body && body.error && body.error.code) code = body.error.code;
        if (body && body.error && body.error.message) message = body.error.message;
      } catch {
        // non-JSON body; fall back to the bare status
      }
      let text = `HTTP ${response.status}`;
      if (code) text += `: ${code}`;
      if (message) text += ` (${message})`;
      throw new Error(text);
    }
    return response.json();
  }

  function csrfToken() {
    const meta = document.querySelector('meta[name="agent-control-csrf"]');
    return meta ? meta.getAttribute("content") : "";
  }

  const panel = createRunPanel({
    fetchJson,
    csrfToken,
    projectName: (id) => dash.names[id] || id,
    phaseText,
    stepTimingText,
    formatElapsed,
    formatCost,
    iterationText,
    liveStateClass,
    runStatusText,
    goalTasks: () => (dash.data ? dash.data.goal_tasks : undefined),
    onChange: () => load({ fresh: true }),
    onClose: () => markSelected(),
  });

  function openRun(runId, opener) {
    panel.open(runId, opener);
    if (dash.data) panel.update(dash.data);
    markSelected();
  }

  function markSelected() {
    const runId = panel.currentRunId();
    const isOpen = panel.isOpen();
    for (const tableId of ["live", "recent-table", "history"]) {
      const table = $(tableId);
      if (!table) continue;
      table.querySelectorAll("tr[data-run]").forEach((row) => {
        const sel = isOpen && runId === row.dataset.run;
        row.classList.toggle("selected", sel);
        row.querySelectorAll(".open-run").forEach((b) => {
          b.setAttribute("aria-expanded", sel ? "true" : "false");
        });
      });
    }
  }

  function currentFilters() {
    const one = (id) => ($(id).value ? [$(id).value] : []);
    return {
      project: one("project"),
      model: one("model"),
      task: one("task"),
      state: one("status"),
      tool: one("tool"),
      include_excluded: $("include-excluded").checked,
    };
  }

  function countCell(value) {
    const c = formatCounter(value);
    return `<td class="num" title="${esc(c.title)}">${esc(c.text)}</td>`;
  }

  function costCell(row) {
    const c = formatCost(row.cost, row.cost_classification);
    return `<td class="num cost-only" title="${esc(c.title)}">${esc(c.text)}</td>`;
  }

  function stateClass(status) {
    if (status === "failed" || status === "abandoned") return "bad";
    if (status === "interrupted" || status === "stalled") return "warn";
    if (status === "running") return "ok";
    return "";
  }

  function silenceText(seconds) {
    if (seconds == null || !Number.isFinite(seconds)) return "";
    if (seconds < 60) return Math.round(seconds) + "s ago";
    if (seconds < 3600) return Math.round(seconds / 60) + "m ago";
    return Math.floor(seconds / 3600) + "h " + Math.round((seconds % 3600) / 60) + "m ago";
  }

  function rowsInto(id, headHtml, bodyHtml) {
    $(id).innerHTML = headHtml + bodyHtml;
  }

  const _whenLabel = _tzShort ? `When (${_tzShort})` : "When";
  const liveHead =
    "<tr><th></th><th>State</th><th>Project</th><th>Task</th><th>Elapsed</th>" +
    "<th>Doing</th><th>Iteration</th><th>Sent</th><th>Returned</th>" +
    "<th class=\"cost-only\">Cost so far</th><th>Model \u00b7 effort</th></tr>";
  const historyHead =
    `<tr><th>${_whenLabel}</th><th>Project</th><th>Status</th><th>Model</th><th>Effort</th><th>Duration</th><th>Task</th>` +
    "<th>Sent</th><th>Returned</th><th>Cache read</th><th class=\"cost-only\">Cost</th><th>Source</th></tr>";
  const liveColumns = countColumns(liveHead);
  const historyColumns = countColumns(historyHead);

  function historyRow(x) {
    const flag = x.lifecycle_inferred ? " *" : "";
    return (
      `<tr class="${x.included_in_totals ? "" : "excluded"}" data-run="${esc(x.run_id)}" tabindex="0">` +
      `<td>${esc(when(x.timestamp))}</td>` +
      `<td>${esc(dash.names[x.project_id] || x.project_id)}</td>` +
      `<td class="${stateClass(x.status)}">${esc(runStatusText(x))}${flag}${x.killed_by_operator === true ? ' <span class="tag">killed</span>' : ""}</td>` +
      `<td>${esc(x.model)}</td><td>${esc(x.effort)}</td>` +
      `<td>${esc(dur(x.timestamp, x.ended_at))}</td><td>${esc(x.label)}</td>` +
      countCell(x.tokens_sent) +
      countCell(x.tokens_returned) +
      countCell(x.cache_read) +
      costCell(x) +
      `<td>${esc(x.usage_source)}</td></tr>`
    );
  }

  function recentStateClass(x) {
    if (x.dismissed) return "muted";
    if (x.status === "complete") return "ok";
    if (x.status === "failed") return "bad";
    if (x.status === "interrupted") return x.attention ? "bad" : "warn";
    return "";
  }

  function recentRow(x) {
    return (
      `<tr data-run="${esc(x.run_id)}" tabindex="0">` +
      `<td class="${recentStateClass(x)}" title="${esc(x.end_reason || "")}">${esc(runStatusText(x))}${x.end_reason === "killed by operator" ? ' <span class="tag">killed by operator</span>' : ""}</td>` +
      `<td>${esc(dash.names[x.project_id] || x.project_id)}</td>` +
      `<td>${esc(x.label)}</td>` +
      `<td title="${esc(when(x.ended_at_effective))}">${esc(endedAgoText(x.ended_at_effective, Date.now()))}</td>` +
      `<td>${esc(formatElapsed(x.elapsed_seconds))}</td>` +
      costCell(x) +
      `</tr>`
    );
  }

  const recentHead =
    "<tr><th>State</th><th>Project</th><th>Task</th><th>Ended</th><th>Duration</th><th class=\"cost-only\">Cost</th></tr>";

  function renderRecent() {
    const section = $("recent");
    if (!section) return;
    const rows = (dash.data && dash.data.recently_ended) || [];
    const toolRows = (dash.data && dash.data.tool_sessions_recent) || [];
    renderToolSessions("recent-tools", toolRows);
    if (!rows.length && !toolRows.length) {
      section.hidden = true;
      $("recent-table").innerHTML = "";
      return;
    }
    section.hidden = false;
    $("recent-table").innerHTML = rows.length ? recentHead + rows.map(recentRow).join("") : "";
  }

  // --- in-place live table update --------------------------------------
  function updateLiveTable(liveRuns) {
    const table = $("live");
    // ensure header row exists once
    if (!table.querySelector("tr.live-head")) {
      const thead = document.createElement("tr");
      thead.className = "live-head";
      thead.innerHTML = liveHead.replace(/^<tr>|<\/tr>$/g, "");
      table.innerHTML = "";
      table.appendChild(thead);
    }
    const wanted = new Map(liveRuns.map((r) => [r.run_id, r]));

    // remove rows for runs no longer live
    table.querySelectorAll("tr.live-row").forEach((row) => {
      if (!wanted.has(row.dataset.run)) {
        row.remove();
      }
    });

    if (!liveRuns.length) {
      if (!table.querySelector("tr.live-empty")) {
        const tr = document.createElement("tr");
        tr.className = "live-empty";
        tr.innerHTML = `<td class="muted" colspan="${liveColumns}">No live runs</td>`;
        table.appendChild(tr);
      }
      return;
    }
    const emptyRow = table.querySelector("tr.live-empty");
    if (emptyRow) emptyRow.remove();

    // Preserve keyboard focus: re-rendering a row's innerHTML replaces its
    // open-run button, so remember which run's button is focused.
    const active = document.activeElement;
    let focusedRun = null;
    if (active && active.classList && active.classList.contains("open-run")) {
      const owner = active.closest("tr.live-row");
      if (owner) focusedRun = owner.dataset.run;
    }

    // add/update rows in place, then order the DOM to match liveRuns.
    for (const run of liveRuns) {
      let row = table.querySelector(`tr.live-row[data-run="${cssEscape(run.run_id)}"]`);
      const isOpen = panel.isOpen() && panel.currentRunId() === run.run_id;
      if (!row) {
        row = document.createElement("tr");
        row.className = "live-row";
        row.dataset.run = run.run_id;
        row.innerHTML = liveRowInner(run, isOpen);
        wireLiveRow(row, run.run_id);
        table.appendChild(row);
      } else {
        row.innerHTML = liveRowInner(run, isOpen);
        wireLiveRow(row, run.run_id);
      }
    }

    // Reorder in place with a cursor, moving ONLY rows that are out of order.
    let cursor = table.querySelector("tr.live-head");
    for (const run of liveRuns) {
      const row = table.querySelector(`tr.live-row[data-run="${cssEscape(run.run_id)}"]`);
      if (!row) continue;
      if (cursor.nextElementSibling !== row) {
        cursor.after(row);
      }
      cursor = row;
    }

    // Restore focus onto the fresh open-run button of the previously focused run.
    if (focusedRun) {
      const owner = table.querySelector(`tr.live-row[data-run="${cssEscape(focusedRun)}"]`);
      const button = owner && owner.querySelector(".open-run");
      if (button) button.focus();
    }
    markSelected();
  }

  function wireLiveRow(row, runId) {
    row.onclick = () => {
      openRun(runId, row.querySelector(".open-run"));
    };
    const button = row.querySelector(".open-run");
    if (button) {
      button.onclick = (e) => {
        e.stopPropagation();
        openRun(runId, button);
      };
    }
  }

  function cssEscape(value) {
    return String(value).replace(/["\\]/g, "\\$&");
  }

  function liveRowInner(x, open) {
    const state = x.display_state || x.status;
    let stateTitle = x.state_reason || "";
    const sil = silenceText(x.silence_seconds);
    if (sil) stateTitle = (stateTitle ? stateTitle + " \u00b7 " : "") + "last signal " + sil;
    let tags = "";
    if (x.dismissed) tags += ` <span class="tag">dismissed</span>`;
    const projTag = {
      discovered: "pending project",
      rejected: "rejected project",
      disabled: "disabled project",
    }[x.project_state];
    const projTitle = {
      discovered:
        "Not approved yet: its runs are excluded from spend totals until you " +
        "approve it under Projects.",
      rejected: "Rejected: its runs are excluded from spend totals.",
      disabled: "Disabled: its runs are excluded from spend totals.",
    }[x.project_state];
    if (projTag)
      tags += ` <span class="tag" title="${esc(projTitle)}">${projTag}</span>`;
    const doing = describeDoing(x);
    const model =
      x.model || x.effort
        ? `${esc(x.model)}${x.effort ? " \u00b7 " + esc(x.effort) : ""}`
        : "\u2014";
    return (
      `<td class="open-cell"><button type="button" class="open-run" ` +
      `aria-controls="run-panel" aria-expanded="${open ? "true" : "false"}" ` +
      `aria-label="Open run details">\u203a</button></td>` +
      `<td><span class="badge ${liveStateClass(state)}" title="${esc(stateTitle)}">${esc(state)}</span>${tags}</td>` +
      `<td>${esc(dash.names[x.project_id] || x.project_id)}</td>` +
      `<td>${esc(x.label)}</td>` +
      `<td title="started ${esc(when(x.timestamp))}">${esc(formatElapsed(elapsedOf(x, Date.now())))}</td>` +
      `<td title="${esc(x.state_reason || "")}">${esc(doing)}</td>` +
      `<td class="num">${esc(iterationText(x.iteration, x.max_iters))}</td>` +
      countCell(x.tokens_sent) +
      countCell(x.tokens_returned) +
      costCell(x) +
      `<td>${model}</td>`
    );
  }

  // --- tool sessions: begin (DOM nodes and textContent only) -----------
  // Tool sessions run outside AgentControl: rows carry no data-run, so the
  // delegated row handlers never open the panel, and they offer no controls.
  const toolHeads = [
    "Tool", "Project", "Models", "Last call", "Calls", "Tokens", "Cost", "Burn", "Subagents",
  ];

  function textCell(tag, text, className, title) {
    const cell = document.createElement(tag);
    cell.textContent = text;
    if (className) cell.className = className;
    if (title) cell.title = title;
    return cell;
  }

  function toolSessionRows(row, nowMs) {
    const m = toolSessionRowModel(row, nowMs);
    const tr = document.createElement("tr");
    tr.className = m.excluded ? "tool-row excluded" : "tool-row";
    const badgeCell = document.createElement("td");
    const badge = document.createElement("span");
    badge.className = "badge tool-badge";
    badge.textContent = m.badge;
    badgeCell.appendChild(badge);
    tr.appendChild(badgeCell);
    tr.appendChild(textCell("td", m.project, m.excluded ? "muted" : ""));
    tr.appendChild(textCell("td", m.models));
    tr.appendChild(textCell("td", m.lastCall, "", when(row && row.last_at)));
    tr.appendChild(textCell("td", m.calls, "num"));
    tr.appendChild(textCell("td", m.tokens, "num"));
    tr.appendChild(textCell("td", m.cost.text, "num cost-only", m.cost.title));
    tr.appendChild(textCell("td", m.burn, "num cost-only"));
    tr.appendChild(textCell("td", m.subagents));
    const note = document.createElement("tr");
    note.className = m.excluded ? "tool-note-row excluded" : "tool-note-row";
    const noteCell = textCell("td", m.note, "tool-note");
    noteCell.colSpan = toolHeads.length;
    note.appendChild(noteCell);
    return [tr, note];
  }

  function renderToolSessions(tableId, rows) {
    const table = $(tableId);
    if (!table) return;
    table.replaceChildren();
    table.hidden = !rows.length;
    if (!rows.length) return;
    const head = document.createElement("tr");
    for (const name of toolHeads) {
      head.appendChild(textCell("th", name, name === "Cost" || name === "Burn" ? "cost-only" : ""));
    }
    table.appendChild(head);
    const nowMs = Date.now();
    for (const row of rows) table.append(...toolSessionRows(row, nowMs));
  }

  function renderByTool() {
    const table = $("by-tool-totals");
    if (!table) return;
    const t = (dash.data && dash.data.normal_totals) || {};
    const periods = [
      ["Today", t.today],
      ["This month", t.month],
      ["All time", t.all_time],
    ];
    const perPeriod = periods.map(
      ([, p]) => new Map(byToolRows((p || {}).by_tool).map((r) => [r.name, r])),
    );
    const names = TOOL_IDS.map(toolLabel).filter((name) => perPeriod.some((m) => m.has(name)));
    table.replaceChildren();
    const head = document.createElement("tr");
    head.appendChild(textCell("th", "Tool"));
    for (const [label] of periods) head.appendChild(textCell("th", label));
    table.appendChild(head);
    if (!names.length) {
      const tr = document.createElement("tr");
      const cell = textCell("td", "No usage in this view.", "muted");
      cell.colSpan = periods.length + 1;
      tr.appendChild(cell);
      table.appendChild(tr);
      return;
    }
    for (const name of names) {
      const tr = document.createElement("tr");
      tr.appendChild(textCell("td", name));
      for (const m of perPeriod) {
        const r = m.get(name);
        const costOn = costFeatureEnabled(dash.data);
        const text = r ? (costOn ? `${r.cost} \u00b7 ${r.count}` : r.count) : "\u2014";
        tr.appendChild(textCell("td", text, "num"));
      }
      table.appendChild(tr);
    }
  }

  function toolLiveText(liveTotals) {
    const t = liveTotals || {};
    const n = Number(t.tool_sessions) || 0;
    if (!n) return "";
    let text = `${n} tool ${n === 1 ? "session" : "sessions"}`;
    if (t.tool_burn_cost_per_hour != null) {
      text += ` \u00b7 ~$${Number(t.tool_burn_cost_per_hour).toFixed(2)}/h`;
    }
    return text;
  }
  // --- tool sessions: end ------------------------------------------------

  // --- spend drawer -----------------------------------------------------
  const spendTabKey = "agentControlSpendTab";
  let spendTab = (() => {
    try {
      return localStorage.getItem(spendTabKey) || "day";
    } catch {
      return "day";
    }
  })();

  function setSpendTab(tab) {
    spendTab = ["day", "project", "model", "task", "tool"].includes(tab) ? tab : "day";
    try {
      localStorage.setItem(spendTabKey, spendTab);
    } catch {
      /* ignore */
    }
    renderSpend();
  }

  function spendEntries() {
    if (!dash.data) return [];
    const key = "by_" + spendTab;
    const raw = (dash.data.spend && dash.data.spend[key]) || [];
    const kind = spendTab === "day" ? "by_day" : "by_" + spendTab;
    const nameOf = (row) => {
      if (spendTab === "day") return row.day;
      if (spendTab === "tool") return toolLabel(row.key);
      return dash.names[row.key] || row.key;
    };
    return sortSpendGroups(kind, raw).map((row) => ({
      name: nameOf(row),
      cost: row.known_cost,
      runs: row.runs,
      calls: row.calls,
    }));
  }

  function spendCountText(x) {
    const runs = `${Number(x.runs || 0).toLocaleString()} runs`;
    if (!x.calls) return runs;
    return `${runs} \u00b7 ${Number(x.calls).toLocaleString()} calls`;
  }

  function renderSpend() {
    if (!dash.data) return;
    const data = spendEntries();
    const max = Math.max(...data.map((x) => x.cost || 0), 0);
    $("spend-tabs")
      .querySelectorAll("button")
      .forEach((b) => {
        const active = b.dataset.spendTab === spendTab;
        b.classList.toggle("active", active);
        b.setAttribute("aria-selected", active ? "true" : "false");
      });
    if (!data.length) {
      $("spend").innerHTML = '<div class="drawer-empty">No spend data for this view.</div>';
      return;
    }
    const bars =
      '<div class="rank-list">' +
      data
        .slice(0, 14)
        .map(
          (x) =>
            `<div class="rank-row"><span class="rank-name" title="${esc(x.name)}">${esc(x.name)}</span>` +
            `<span class="rank-value">${esc(money(x.cost))}</span>` +
            `<div class="rank-track"><div class="rank-fill" data-width="${max ? Math.max(2, ((x.cost || 0) / max) * 100) : 0}"></div></div>` +
            `<span class="rank-meta">${esc(spendCountText(x))}</span></div>`,
        )
        .join("") +
      "</div>";
    const table =
      '<div class="scroll drawer-table"><table><tr><th>Group</th><th>Runs</th><th>Calls</th><th>Cost</th></tr>' +
      data
        .map(
          (x) =>
            `<tr><td>${esc(x.name)}</td><td class="num">${esc(Number(x.runs || 0).toLocaleString())}</td>` +
            `<td class="num">${esc(Number(x.calls || 0).toLocaleString())}</td><td class="num">${esc(money(x.cost))}</td></tr>`,
        )
        .join("") +
      "</table></div>";
    $("spend").innerHTML = bars + table;
    $("spend")
      .querySelectorAll(".rank-fill[data-width]")
      .forEach((el) => {
        el.style.width = el.dataset.width + "%";
      });
  }

  function openSpend() {
    const drawer = $("spend-drawer");
    const backdrop = $("spend-backdrop");
    backdrop.hidden = false;
    requestAnimationFrame(() => {
      backdrop.classList.add("open");
      drawer.classList.add("open");
    });
    drawer.setAttribute("aria-hidden", "false");
    document.body.style.overflow = "hidden";
    renderSpend();
    $("spend-close").focus();
  }

  function closeSpend() {
    const drawer = $("spend-drawer");
    const backdrop = $("spend-backdrop");
    drawer.classList.remove("open");
    backdrop.classList.remove("open");
    drawer.setAttribute("aria-hidden", "true");
    document.body.style.overflow = "";
    setTimeout(() => (backdrop.hidden = true), 240);
    $("spend-summary").focus();
  }

  // --- projects ---------------------------------------------------------
  async function mutate(path, body) {
    return fetchJson(path, {
      method: "POST",
      headers: mutationHeaders(dash.token),
      body: JSON.stringify(body || {}),
    });
  }

  const projectActions = {
    discovered: ["approve", "reject"],
    approved: ["disable"],
    rejected: ["approve"],
    disabled: ["approve"],
  };
  const stateLabel = {
    discovered: "pending",
    approved: "approved",
    rejected: "rejected",
    disabled: "disabled",
  };

  function projectRow(project) {
    const buttons = (projectActions[project.state] || [])
      .map(
        (action) =>
          `<button class="mini ${action}" type="button" data-project="${esc(project.project_id)}" data-action="${action}">${action}</button>`,
      )
      .join("");
    const stateCls = project.state === "approved" ? "ok" : project.state === "discovered" ? "warn" : "muted";
    return (
      `<tr><td class="${stateCls}">${esc(stateLabel[project.state] || project.state)}</td>` +
      `<td>${esc(project.display_name)}</td>` +
      `<td class="muted" title="${esc(project.root)}">${esc(project.root)}</td>` +
      `<td>${buttons}</td></tr>`
    );
  }

  function renderAttention() {
    const box = $("attention");
    const items = [];

    // 1. New projects awaiting approval (excluded from totals until approved).
    const pending = dash.projects.filter((p) => p.state === "discovered");
    for (const p of pending) {
      items.push({
        kind: "project",
        text: `New project needs approval: <strong>${esc(p.display_name)}</strong>`,
        sub: esc(p.root),
        actions: [
          { label: "Approve", action: "approve", project: p.project_id, primary: true },
          { label: "Reject", action: "reject", project: p.project_id },
        ],
      });
    }

    // 2. Live runs the server has flagged as needing attention.
    const attentionRuns = (dash.data.live || []).filter((r) => r.attention === true);
    for (const r of attentionRuns) {
      const proj = dash.names[r.project_id] || r.project_id;
      const verifiedQuiet = r.display_state === "quiet" && r.verified === true;
      let sub = esc(r.state_reason || "");
      if (verifiedQuiet) {
        sub =
          (sub ? sub + " " : "") +
          "The engine process is alive: check its terminal before doing anything.";
      }
      const actions =
        r.dismiss_allowed && !verifiedQuiet ? [{ label: "Dismiss", run: r.run_id }] : [];
      items.push({
        kind: "stall",
        text:
          `Run <strong>${esc(r.label)}</strong> in ${esc(proj)} is ` +
          `<span class="${liveStateClass(r.display_state)}">${esc(r.display_state)}</span>`,
        sub,
        actions,
      });
    }

    if (!items.length) {
      box.hidden = true;
      box.innerHTML = "";
      return;
    }

    box.hidden = false;
    box.innerHTML =
      `<div class="attention-title">\u26a0 Needs attention (${items.length})</div>` +
      items
        .map((it) => {
          const buttons = it.actions
            .map((a) => {
              if (a.run) {
                const dis = pendingActions.has(`dismiss:${a.run}`) ? " disabled" : "";
                return (
                  `<button class="mini reject" data-att-run="${esc(a.run)}"${dis}>` +
                  `${esc(a.label)}</button>`
                );
              }
              const dis = pendingActions.has(`project:${a.project}:${a.action}`) ? " disabled" : "";
              return (
                `<button class="mini ${a.primary ? "approve" : "reject"}" ` +
                `data-att-project="${esc(a.project)}" data-att-action="${esc(a.action)}"${dis}>${esc(a.label)}</button>`
              );
            })
            .join("");
          return (
            `<div class="attention-row att-${it.kind}">` +
            `<div class="attention-main"><div>${it.text}</div>` +
            `<div class="attention-sub">${it.sub}</div></div>` +
            `<div class="attention-actions">${buttons}</div></div>`
          );
        })
        .join("");

    box.querySelectorAll("button[data-att-project]").forEach((button) => {
      button.onclick = async () => {
        const key = `project:${button.dataset.attProject}:${button.dataset.attAction}`;
        if (pendingActions.has(key)) return;
        pendingActions.add(key);
        button.disabled = true;
        try {
          await mutate(
            `/api/projects/${button.dataset.attProject}/${button.dataset.attAction}`,
            {},
          );
          await load({ fresh: true });
        } catch (error) {
          button.disabled = false;
          window.alert(`Action failed: ${error.message}`);
        } finally {
          pendingActions.delete(key);
        }
      };
    });

    box.querySelectorAll("button[data-att-run]").forEach((button) => {
      button.onclick = async () => {
        const runId = button.dataset.attRun;
        const key = `dismiss:${runId}`;
        if (pendingActions.has(key)) return;
        const confirmed = window.confirm(
          "Mark this run interrupted?\n\nOnly do this if the agent is really gone. " +
            "If it is still running, it disappears from Live now until it ends, and " +
            "its real end record then replaces this.",
        );
        if (!confirmed) return;
        pendingActions.add(key);
        button.disabled = true;
        try {
          await mutate(`/api/runs/${runId}/dismiss`, {});
          await load({ fresh: true });
        } catch (error) {
          button.disabled = false;
          window.alert(`Dismiss failed: ${error.message}`);
        } finally {
          pendingActions.delete(key);
        }
      };
    });
  }

  function renderProjects() {
    const order = { approved: 0, discovered: 1, disabled: 2, rejected: 3 };
    const sorted = [...dash.projects].sort(
      (a, b) => (order[a.state] ?? 9) - (order[b.state] ?? 9) || a.display_name.localeCompare(b.display_name),
    );
    rowsInto(
      "projects",
      "<tr><th>State</th><th>Name</th><th>Root</th><th>Actions</th></tr>",
      sorted.map(projectRow).join(""),
    );
    $("projects")
      .querySelectorAll("button[data-project]")
      .forEach((button) => {
        button.onclick = async () => {
          const key = `project:${button.dataset.project}:${button.dataset.action}`;
          if (pendingActions.has(key)) return;
          pendingActions.add(key);
          button.disabled = true;
          try {
            await mutate(`/api/projects/${button.dataset.project}/${button.dataset.action}`, {});
            await load({ fresh: true });
          } catch (error) {
            button.disabled = false;
            window.alert(`Action failed: ${error.message}`);
          } finally {
            pendingActions.delete(key);
          }
        };
      });
  }

  function renderDiagnostics() {
    const counts = {};
    for (const d of dash.diagnostics) counts[d.code] = (counts[d.code] || 0) + 1;
    const entries = Object.entries(counts).sort((a, b) => b[1] - a[1]);
    rowsInto(
      "diagnostics",
      "<tr><th>Code</th><th>Count</th></tr>",
      entries.length
        ? entries.map(([code, n]) => `<tr><td>${esc(code)}</td><td class="num">${n}</td></tr>`).join("")
        : '<tr><td colspan="2" class="muted">None</td></tr>',
    );
  }

  // --- pricing editor ---------------------------------------------------
  const rateFields = ["input", "output", "cache_write", "cache_read"];
  const pricingState = { previewFingerprint: null, candidateFingerprint: null, model: null };

  function localToUtc(value) {
    if (!value) throw new Error("Select a local date and time");
    const date = new Date(value);
    if (!Number.isFinite(date.getTime())) throw new Error("Invalid local date and time");
    return date.toISOString();
  }

  function ratePayload() {
    if (!pricingState.model) throw new Error("Select a model");
    const rates = {};
    for (const key of rateFields) {
      const input = $("rate-" + key);
      const value = Number(input.value);
      if (input.value === "" || !Number.isFinite(value) || value < 0) {
        throw new Error("Enter all four non-negative rates");
      }
      rates[key] = value;
    }
    const effective = $("rate-custom").checked
      ? localToUtc($("rate-datetime").value)
      : new Date().toISOString();
    return { model: pricingState.model, effective_at: effective, rates, note: $("rate-note").value };
  }

  function renderRates() {
    const active = (dash.data && dash.data.pricing && dash.data.pricing.active) || {};
    const models = Object.entries(active).filter(([m]) => m.toLowerCase() !== "<synthetic>");
    $("rate-list").innerHTML =
      models
        .map(([model, item]) => {
          const r = item.rates || {};
          const status = item.complete
            ? `<span class="ok">${esc(item.source === "baseline" ? "Baseline" : item.source)}</span>`
            : '<span class="warn">Missing rates</span>';
          return (
            `<div class="card"><strong>${esc(model)}</strong> ${status}` +
            `<div class="muted">Input ${esc(r.input ?? "\u2014")} \u00b7 Cached ${esc(r.cache_read ?? "\u2014")} \u00b7 ` +
            `Cache write ${esc(r.cache_write ?? "\u2014")} \u00b7 Output ${esc(r.output ?? "\u2014")}</div>` +
            `<button class="mini" type="button" data-rate-model="${esc(model)}">Adjust rates</button> ` +
            `<span class="muted">${item.history_count || 0} version(s)</span></div>`
          );
        })
        .join("") || '<p class="muted">No models observed yet.</p>';
    $("rate-list")
      .querySelectorAll("[data-rate-model]")
      .forEach((b) => (b.onclick = () => openRateEditor(b.dataset.rateModel)));

    const history = ((dash.data && dash.data.pricing && dash.data.pricing.history) || []).slice().reverse();
    $("rate-history").innerHTML =
      history
        .map((record) => {
          const r = record.rates || {};
          return (
            `<div class="card"><strong>${esc(record.model)}</strong> \u00b7 ${esc(record.effective_at)}` +
            `<div class="muted">Input ${esc(r.input)} \u00b7 Cached ${esc(r.cache_read)} \u00b7 ` +
            `Cache write ${esc(r.cache_write)} \u00b7 Output ${esc(r.output)}</div>` +
            `<div class="muted">Created ${esc(record.created_at)}${record.note ? " \u00b7 " + esc(record.note) : ""}</div></div>`
          );
        })
        .join("") || '<p class="muted">No rate versions yet.</p>';
  }

  function openRateEditor(model) {
    const active = (dash.data.pricing.active || {})[model] || {};
    const rates = active.rates || {};
    pricingState.model = model;
    pricingState.previewFingerprint = null;
    pricingState.candidateFingerprint = null;
    $("rate-editor-title").textContent = "Adjust rates \u00b7 " + model;
    for (const key of rateFields) $("rate-" + key).value = rates[key] ?? "";
    $("rate-now").checked = true;
    $("rate-custom").checked = false;
    $("rate-datetime").disabled = true;
    $("rate-note").value = "";
    $("rate-result").hidden = true;
    $("pricing-append").disabled = true;
    $("rate-editor").hidden = false;
    $("rates-panel").open = true;
  }

  function invalidatePreview() {
    pricingState.previewFingerprint = null;
    $("pricing-append").disabled = true;
  }

  async function previewRates(event) {
    event.preventDefault();
    try {
      const payload = ratePayload();
      const preview = await mutate("/api/pricing/preview", payload);
      pricingState.previewFingerprint = candidateFingerprint(payload);
      pricingState.candidateFingerprint = pricingState.previewFingerprint;
      $("pricing-append").disabled = !canAppendPricing(pricingState);
      $("rate-result").hidden = false;
      $("rate-result").innerHTML =
        `<strong>Authoritative preview</strong><br>Effective ${esc(preview.effective_at)} UTC<br>` +
        `${preview.affected_runs} affected \u00b7 ${preview.newly_priced_runs} newly priced \u00b7 ${preview.superseded_runs} superseded<br>` +
        `${esc(money(preview.old_known_total))} \u2192 ${esc(money(preview.projected_known_total))}; delta ${esc(money(preview.delta))}<br>` +
        `<em>Appending creates an immutable rate version.</em>`;
    } catch (error) {
      $("rate-result").hidden = false;
      $("rate-result").textContent = error.message;
    }
  }

  async function appendRates() {
    if (!canAppendPricing(pricingState)) return;
    try {
      const saved = await mutate("/api/pricing/history", ratePayload());
      const preview = saved.preview || {};
      $("rate-result").hidden = false;
      $("rate-result").innerHTML =
        `<strong>Rate version appended</strong><br>${preview.newly_priced_runs || 0} newly priced \u00b7 ` +
        `projected ${esc(money(preview.projected_known_total))}`;
      invalidatePreview();
      await load({ fresh: true });
      $("rates-panel").open = true;
    } catch (error) {
      $("rate-result").hidden = false;
      $("rate-result").textContent = error.message;
    }
  }

  // --- filters + render -------------------------------------------------
  function fillSelect(id, key, values) {
    const old = $(id).value;
    $(id).innerHTML = '<option value="">All</option>' + values.map((v) => `<option>${esc(v)}</option>`).join("");
    $(id).value = old;
  }

  function refreshFilterOptions() {
    const runs = dash.data.runs || [];
    fillSelect("model", "model", [...new Set(runs.map((r) => r.model).filter(Boolean))].sort());
    fillSelect("status", "status", [...new Set(runs.map((r) => r.status).filter(Boolean))].sort());
    fillSelect("task", "task", [...new Set(runs.map((r) => r.label).filter(Boolean))].sort());
    const projOptions = dash.projects
      .map((p) => `<option value="${esc(p.project_id)}">${esc(p.display_name)}</option>`)
      .join("");
    const oldProj = $("project").value;
    $("project").innerHTML = '<option value="">All</option>' + projOptions;
    $("project").value = oldProj;
    const toolSelect = $("tool");
    if (toolSelect && toolSelect.options.length <= 1) {
      for (const id of TOOL_IDS) {
        const option = document.createElement("option");
        option.value = id;
        option.textContent = toolLabel(id);
        toolSelect.appendChild(option);
      }
    }
  }

  function applyCostVisibility(on) {
    document.body.classList.toggle("cost-disabled", !on);
    const note = $("cost-disabled-note");
    if (note) {
      note.textContent = COST_DISABLED_NOTE;
      note.hidden = on;
    }
    const summary = $("spend-summary");
    if (summary) summary.hidden = !on;
    if (!on) {
      const drawer = $("spend-drawer");
      if (drawer && !drawer.hidden && typeof closeSpend === "function") closeSpend();
    }
  }

  function render() {
    const d = dash.data;
    const cov = d.coverage || {};
    const bad = dash.diagnostics.length;
    const tzLabel = _tzShort ? ` (${_tzShort})` : "";
    $("state").textContent =
      `Snapshot ${when(d.generated_at)}${tzLabel}; ${(d.runs || []).length} rows, ` +
      `${Object.keys(dash.names).length} projects; ${bad} diagnostic record(s).`;

    renderAttention();
    const liveSummary = $("live-summary");
    if (liveSummary) {
      const runText = burnText(d.live_totals || { runs: (d.live || []).length });
      // burnText already prints just the run count when known_cost is absent.
      const toolText = toolLiveText(d.live_totals);
      liveSummary.textContent = toolText ? `${runText} \u00b7 ${toolText}` : runText;
    }
    markOutdated(serverIsOutdated(d));
    updateLiveTable(d.live || []);
    renderToolSessions("live-tools", d.tool_sessions_live || []);
    renderRecent();
    rowsInto(
      "history",
      historyHead,
      (d.runs || []).filter((r) => r.status !== "running").map(historyRow).join("") ||
        `<tr><td class="muted" colspan="${historyColumns}">No runs. Approve a project to include its runs.</td></tr>`,
    );

    const costOn = costFeatureEnabled(d);
    applyCostVisibility(costOn);
    if (costOn) {
      const t = d.normal_totals || {};
      const all = t.all_time || {};
      $("spend-all").textContent = money(all.known_cost);
      $("spend-today").textContent = money((t.today || {}).known_cost);
      $("spend-month").textContent = money((t.month || {}).known_cost);
      $("drawer-all").textContent = money(all.known_cost);
      $("drawer-today").textContent = money((t.today || {}).known_cost);
      $("drawer-month").textContent = money((t.month || {}).known_cost);
      $("drawer-runs").textContent = (all.runs || 0) + " runs";
      $("spend-confidence").textContent =
        `${cov.exact || 0} exact \u00b7 ${cov.upper_bound || 0} upper-bound \u00b7 ` +
        `${cov.missing_rate || 0} missing rate \u00b7 ${cov.missing_usage || 0} missing usage`;
    }

    renderByTool();
    if (costOn) {
      renderSpend();
      renderRates();
    }
    renderProjects();
    renderDiagnostics();
    renderGoalTasks(d.goal_tasks);
    panel.update(d);
    markSelected();
  }

  // Opt-in: no DOM unless the snapshot carries goal_tasks; HTML is built from
  // allowlisted, escaped fields only (task-state.js).
  function renderGoalTasks(rows) {
    let section = $("goal-tasks");
    const html = renderGoalTasksHtml(rows);
    if (!html) {
      if (section) section.remove();
      return;
    }
    if (!section) {
      const anchor = $("projects");
      if (!anchor) return;
      section = document.createElement("section");
      section.id = "goal-tasks";
      const scroll = anchor.closest(".scroll") || anchor;
      scroll.after(section);
    }
    section.innerHTML = html;
  }

  function liveScroll() {
    const live = $("live");
    return live ? live.closest(".scroll") : null;
  }

  function clearStale() {
    const scroll = liveScroll();
    if (scroll) scroll.classList.remove("stale");
    const status = $("live-status");
    // Only clear a refresh-failure note; an outdated-server note is managed by
    // markOutdated during render and must survive a successful refresh.
    if (status && status.dataset.kind === "stale") {
      status.hidden = true;
      status.textContent = "";
      delete status.dataset.kind;
    }
  }

  function markStale(message) {
    const scroll = liveScroll();
    if (scroll) scroll.classList.add("stale");
    const status = $("live-status");
    if (status) {
      status.textContent = staleNote(dash.lastOkAt, Date.now(), message);
      status.dataset.kind = "stale";
      status.hidden = false;
    }
  }

  // Announce a server running older code than this page. A refresh-failure note
  // always wins, so never overwrite one; and clearing the outdated note must not
  // touch a stale note (tracked via data-kind). Does not grey the table.
  function markOutdated(outdated) {
    const status = $("live-status");
    if (!status) return;
    if (status.dataset.kind === "stale") return;
    if (outdated) {
      status.textContent =
        "The dashboard server is running older code than this page. Restart " +
        "agent-control serve to see live state, elapsed time and activity.";
      status.dataset.kind = "outdated";
      status.hidden = false;
    } else if (status.dataset.kind === "outdated") {
      status.hidden = true;
      status.textContent = "";
      delete status.dataset.kind;
    }
  }

  const reloadQueue = createReloadQueue(async (isCurrent) => {
    try {
      const query = buildQuery(currentFilters());
      const [data, projects, diagnostics] = await Promise.all([
        fetchJson(`/api/data?${query}`),
        fetchJson("/api/projects"),
        fetchJson("/api/diagnostics"),
      ]);
      if (!isCurrent()) return;
      dash.data = data;
      dash.projects = projects;
      dash.diagnostics = diagnostics;
      dash.names = Object.fromEntries(projects.map((p) => [p.project_id, p.display_name]));
      dash.lastOkAt = Date.now();
      clearStale();
      refreshFilterOptions();
      render();
      if (!dash.hashRestored) {
        dash.hashRestored = true;
        const id = runIdFromHash(location.hash);
        if (id && !panel.isOpen()) openRun(id, null);
      }
    } catch (error) {
      $("state").textContent = "Refresh failed: " + error.message;
      markStale(error.message);
    }
  });

  function load(opts) {
    return reloadQueue.request(opts);
  }

  function wire() {
    dash.token = csrfToken();
    for (const id of ["project", "model", "status", "task", "tool"]) $(id).onchange = load;
    $("include-excluded").onchange = load;
    $("spend-summary").onclick = openSpend;
    $("spend-close").onclick = closeSpend;
    $("spend-backdrop").onclick = closeSpend;
    $("spend-tabs")
      .querySelectorAll("button")
      .forEach((b) => (b.onclick = () => setSpendTab(b.dataset.spendTab)));
    $("rate-editor").onsubmit = previewRates;
    $("pricing-append").onclick = appendRates;
    $("rate-cancel").onclick = () => {
      $("rate-editor").hidden = true;
    };
    $("rate-custom").onchange = () => {
      $("rate-datetime").disabled = false;
      invalidatePreview();
    };
    $("rate-now").onchange = () => {
      $("rate-datetime").disabled = true;
      invalidatePreview();
    };
    $("rate-editor")
      .querySelectorAll("input,textarea")
      .forEach((input) => {
        if (!["rate-custom", "rate-now"].includes(input.id)) input.addEventListener("input", invalidatePreview);
      });
    // One delegated handler each on tables whose innerHTML is re-rendered every
    // 5s (rowsInto for #history; renderRecent for #recent-table).
    for (const tableId of ["history", "recent-table"]) {
      const table = $(tableId);
      if (!table) continue;
      const openFromRow = (row) => {
        if (row && row.dataset && row.dataset.run) {
          openRun(row.dataset.run, row);
        }
      };
      table.addEventListener("click", (e) => {
        openFromRow(e.target.closest("tr[data-run]"));
      });
      table.addEventListener("keydown", (e) => {
        if (e.key !== "Enter" && e.key !== " ") return;
        const row = e.target.closest("tr[data-run]");
        if (!row) return;
        e.preventDefault();
        openFromRow(row);
      });
    }
    document.addEventListener("keydown", (e) => {
      if (e.key !== "Escape") return;
      if ($("spend-drawer").classList.contains("open")) closeSpend();
      else if (panel.isOpen()) {
        panel.close();
        markSelected();
      }
    });
  }

  wire();
  load();
  setInterval(load, 5000);
}

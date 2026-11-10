// Run side panel: a non-modal right-hand panel showing one delegated-agent
// run's narrated transcript as a timeline, with controls to steer, wrap up or
// kill the run. Pure helpers first, then createRunPanel for the DOM.

import { projectGoalTasks, appendGoalTasks } from "./task-state.js";
// Metadata only. Keep this in the existing asset so older running servers can
// load the panel before an operator approves their backend restart.
const DIAG_CATEGORY = {
  complete: "Run completed",
  in_progress: "Engine still running",
  engine_exception: "Engine exception recorded",
  post_completion_exception: "Engine exception after task completion",
  cleanup_failed: "Task completed; engine cleanup failed",
  broken_output: "Engine output pipe failed",
  terminal_write_failed: "Terminal record could not be fully persisted",
  terminal_incomplete: "Finalization incomplete; cause unknown",
  system_exit: "Engine exit recorded",
  signal_exit: "Engine signal recorded; initiator unknown",
  limit_stop: "Recorded limit exit",
  mutation_outcome_unknown: "Stopped with a mutation outcome unknown",
  recovery_state: "Stopped because recovery state could not be verified",
  recorded_failure: "Failure recorded; detailed cause unknown",
  process_exit_unknown: "Engine stopped; cause unknown",
};

function diagnosticBoundaryText(boundary) {
  if (!boundary) return "";
  let text = boundary.stage || "unknown";
  if (Number.isInteger(boundary.turn)) text += ` at turn ${boundary.turn}`;
  if (Number.isInteger(boundary.call_index)) text += `, call ${boundary.call_index}`;
  if (boundary.tool_name) text += ` (${boundary.tool_name})`;
  return text;
}

export function diagnosisLines(data) {
  if (!data || typeof data !== "object") return [];
  const lines = [Object.hasOwn(DIAG_CATEGORY, data.category) ? DIAG_CATEGORY[data.category] : "Diagnosis unavailable"];
  const coverage = data.coverage || {};
  if (["max_iters", "repair_limit", "no_action_limit", "context_budget", "mutation_outcome_unknown", "recovery_state"].includes(data.stop_reason)) {
    lines.push(`Recorded stop: ${data.stop_reason}`);
  }
  if (data.last_completed) lines.push(`Last completed: ${diagnosticBoundaryText(data.last_completed)}`);
  if (data.unfinished) lines.push(`Last unmatched start: ${diagnosticBoundaryText(data.unfinished)}`);
  const known = value => Number.isInteger(value) ? String(value) : "unknown";
  if (data.engine_exit_code != null || data.last_command_exit_code != null) {
    lines.push(`Engine exit: ${known(data.engine_exit_code)}; last command exit: ${known(data.last_command_exit_code)}`);
  }
  if (coverage.exit_code_conflict) {
    lines.push(`Engine reported exit: ${known(data.reported_engine_exit_code)}; observed exit: ${known(data.engine_exit_code)}`);
  }
  if (data.exception) {
    let text = `Recorded exception: ${data.exception.class || "unknown"}`;
    if (data.exception.source) text += ` at ${data.exception.source}`;
    if (Number.isInteger(data.exception.line)) text += `:${data.exception.line}`;
    lines.push(text);
  }
  if (data.cleanup_exception) lines.push(`Cleanup failure recorded: ${data.cleanup_exception.class || "unknown"}`);
  if (data.execution_profile === "legacy" || data.execution_profile === "resilient-v1") {
    lines.push(`Profile: ${data.execution_profile}; Python: ${data.runtime_version || "unknown"}; architecture: ${data.architecture || "unknown"}`);
  }
  if (data.bundle_id) lines.push(`Engine build: ${data.bundle_id}`);
  else if (coverage.engine === "recorded") lines.push("Engine build: unknown (older or incomplete diagnostics)");
  if (data.terminal && data.terminal.persisted === false) {
    lines.push(`Terminal index writes: ${known(data.terminal.indexes_written)}/${known(data.terminal.indexes_expected ?? data.terminal.indexes_attempted)} (incomplete)`);
  }
  if (coverage.engine === "missing" || coverage.engine === "unavailable") {
    lines.push("Engine diagnostics unavailable for this run");
  }
  if (coverage.partial || coverage.invalid_rows || coverage.identity_mismatch || coverage.exit_code_conflict) {
    lines.push("Evidence incomplete or conflicting; missing events must not be treated as proof of a cause");
  }
  return lines;
}

export const WRAP_UP_CONFIRM =
  "Ask the agent to stop and call finish with its report?";

// --- pure helpers ------------------------------------------------------

export function summarizeToolCall(name, args) {
  const fallback = () => `${name} ${String(args || "").slice(0, 120)}`.trim();
  let parsed;
  try {
    parsed = JSON.parse(args);
  } catch {
    return fallback();
  }
  if (!parsed || typeof parsed !== "object") return fallback();
  switch (name) {
    case "read_file": {
      let out = String(parsed.path ?? "");
      const start = parsed.start_line;
      const end = parsed.end_line;
      if (start != null || end != null) {
        out += `:${start != null ? start : ""}-${end != null ? end : ""}`;
      }
      return out;
    }
    case "grep": {
      let out = `"${parsed.pattern ?? ""}"`;
      if (parsed.glob) out += ` in ${parsed.glob}`;
      return out;
    }
    case "run_command":
      return Array.isArray(parsed.argv) ? parsed.argv.join(" ") : fallback();
    case "write_file":
    case "replace_in_file":
    case "list_dir":
      return String(parsed.path ?? "");
    case "finish":
      return "finish";
    default:
      return fallback();
  }
}

export function mergeEvents(existing, after, response) {
  if (response.total < after) return { events: [], reset: true };
  return {
    events: existing.slice(0, after).concat(response.events),
    reset: false,
  };
}

export function nextAfter(events) {
  return Math.max(0, events.length - 1);
}

export function groupTimeline(events) {
  const items = [];
  let current = null;
  for (const event of events) {
    if (event.kind === "notice" || event.kind === "final") {
      current = null;
      items.push({ type: event.kind, event });
      continue;
    }
    if (event.iteration == null) {
      current = null;
      continue;
    }
    if (current && current.iteration === event.iteration) {
      current.events.push(event);
    } else {
      current = { type: "turn", iteration: event.iteration, events: [event] };
      items.push(current);
    }
  }
  return items;
}

export function steerStatusText(status) {
  const prefix = status.kind === "wrap_up" ? "Wrap-up request" : "Steer";
  let text;
  switch (status.state) {
    case "queued":
      text = "Queued: arrives at the agent's next turn";
      break;
    case "delivered":
      text = `Delivered at turn ${status.turn}`;
      break;
    case "lost":
      text =
        "Not delivered: the steer file was overwritten or consumed without it; send again";
      break;
    case "undeliverable":
      text = "Not delivered: the run ended";
      break;
    default:
      text = status.state;
  }
  return `${prefix} · ${status.chars} chars · ${text}`;
}

export function killConfirmText(label, header) {
  let text = `Kill ${label}? Its engine and child processes stop immediately and it writes no final report.`;
  if (!header) {
    text += " Its write mode is unknown.";
  } else if (header.writes === "staged") {
    text += " Its staged edits exist only in the engine's memory and will be lost.";
  } else if (header.worktree) {
    text += ` Edits made so far stay in ${header.root}.`;
  } else {
    text += " Edits made so far are already in the repository.";
  }
  return text;
}

export function killOutcomeText(result) {
  if (result.outcome === "already_gone") return "The engine had already exited.";
  if (result.outcome === "partial") {
    return `${result.survivors} process(es) did not exit (engine PID ${result.engine_pid}).`;
  }
  const n = result.children;
  if (n === 0) return "Engine terminated.";
  if (n === 1) return "Engine and 1 child process terminated.";
  return `Engine and ${n} child processes terminated.`;
}

export function controlsState(row) {
  if (!row) {
    const reason = "The run has ended.";
    return { steer: { enabled: false, reason }, kill: { enabled: false, reason } };
  }
  const fallback = row.control_reason || "Not available for this run.";
  const steerEnabled = !!row.can_steer;
  const killEnabled = !!row.can_kill;
  return {
    steer: { enabled: steerEnabled, reason: steerEnabled ? null : fallback },
    kill: { enabled: killEnabled, reason: killEnabled ? null : fallback },
  };
}

export function nearBottom(scrollTop, scrollHeight, clientHeight, slack = 40) {
  return scrollHeight - scrollTop - clientHeight <= slack;
}

export function outputLabel(event) {
  const len = (event.output || "").length;
  let text = `output · ${len.toLocaleString("en-US")} chars`;
  if (event.output_truncated) text += " · truncated";
  return text;
}

export function reasoningFolded(text) {
  return String(text ?? "").split("\n").length > 3;
}

export function statusText(event) {
  const text = String((event && event.text) || "");
  return text ? text.charAt(0).toUpperCase() + text.slice(1) : "";
}

export function previewHeading(event) {
  const path = event && event.path ? String(event.path) : "";
  switch (event && event.tool) {
    case "finish":
      return "Final report";
    case "replace_in_file":
      return path ? `Editing ${path}` : "Editing a file";
    default:
      return path ? `Writing ${path}` : "Writing a file";
  }
}

export function previewVisible(event) {
  return Boolean(event && event.kind === "preview" && event.live);
}

export function liveLabel(base, event, runLive) {
  return event && event.live && runLive ? `${base} (writing…)` : base;
}

export function hashForRun(runId) {
  return "#run=" + encodeURIComponent(runId);
}

export function runIdFromHash(hash) {
  const match = /^#run=(.*)$/.exec(hash || "");
  if (!match) return null;
  try {
    return decodeURIComponent(match[1]);
  } catch {
    return null;
  }
}

// --- createRunPanel (DOM) ---------------------------------------------

function findRow(data, runId) {
  if (!data) return { row: null, live: false };
  for (const row of data.live || []) {
    if (row.run_id === runId) return { row, live: true };
  }
  for (const row of data.recently_ended || []) {
    if (row.run_id === runId) return { row, live: false };
  }
  for (const row of data.runs || []) {
    if (row.run_id === runId) return { row, live: false };
  }
  return { row: null, live: false };
}

export function createRunPanel(deps) {
  if (typeof document === "undefined") {
    throw new Error("createRunPanel requires a document");
  }

  const el = (tag, attrs) => {
    const node = document.createElement(tag);
    if (attrs) {
      for (const [key, value] of Object.entries(attrs)) {
        if (key === "class") node.className = value;
        else if (key === "text") node.textContent = value;
        else node.setAttribute(key, value);
      }
    }
    return node;
  };

  // Per-panel DOM references, created lazily on first open.
  let dom = null;
  // Per-run state.
  let runId = null;
  let opener = null;
  let live = false;
  let events = [];
  let steer = [];
  let header = null;
  let brief = null;
  let masked = 0;
  let truncated = false;
  let available = true;
  let row = null;
  let activity = null;
  let serverSkew = 0;
  let pollTimer = null;
  let phaseTimer = null;
  let fetching = false;
  let refetchOnEnd = false;
  let resetPending = false;
  let lastData = null;
  let pollError = "";
  let actionStatus = "";
  const eventNodes = new Map();
  const turnSections = new Map();

  function build() {
    if (dom) return;
    const aside = el("aside", {
      id: "run-panel",
      class: "run-panel",
      "aria-labelledby": "run-panel-title",
    });

    const head = el("header", { class: "rp-head" });
    const title = el("h2", { id: "run-panel-title", tabindex: "-1" });
    const closeBtn = el("button", {
      class: "icon-button rp-close",
      "aria-label": "Close run details",
      text: "\u00d7",
    });
    closeBtn.addEventListener("click", () => close());
    const meta = el("div", { class: "rp-meta" });
    head.append(title, closeBtn, meta);

    const notes = el("div", { class: "rp-notes", "aria-live": "polite" });
    const diagnosis = el("section", { class: "rp-diagnosis", "aria-label": "Engine diagnosis", hidden: "hidden" });
    const timeline = el("div", {
      class: "rp-timeline",
      role: "log",
      "aria-live": "polite",
    });
    const phase = el("div", { class: "rp-phase muted" });
    const jump = el("button", { class: "rp-jump", hidden: "hidden" });
    jump.addEventListener("click", () => {
      timeline.scrollTop = timeline.scrollHeight;
      jump.hidden = true;
    });

    const actionStatusEl = el("div", {
      class: "rp-action-status",
      "aria-live": "polite",
    });

    const controls = buildControls();

    aside.append(head, notes, diagnosis, timeline, phase, actionStatusEl, jump, controls.footer);
    document.body.appendChild(aside);

    aside.addEventListener("keydown", (e) => {
      if (e.key === "Escape") {
        e.stopPropagation();
        close();
      }
    });

    dom = { aside, title, meta, notes, diagnosis, timeline, phase, actionStatus: actionStatusEl, jump, ...controls };
  }
  function buildControls() {
    const footer = el("footer", { class: "rp-controls" });
    const steerStatus = el("div", { class: "rp-steer-status" });
    const textarea = el("textarea", {
      id: "rp-steer-text",
      maxlength: "4000",
      rows: "3",
      placeholder: "Guidance for the agent's next turn",
    });
    textarea.addEventListener("keydown", (e) => {
      if ((e.ctrlKey || e.metaKey) && e.key === "Enter") {
        e.preventDefault();
        doSend();
      }
    });
    const buttons = el("div", { class: "rp-buttons" });
    const sendBtn = el("button", { class: "rp-send", text: "Send" });
    sendBtn.addEventListener("click", () => doSend());
    const wrapBtn = el("button", { class: "rp-wrap", text: "Ask to wrap up" });
    wrapBtn.addEventListener("click", () => doWrap());
    const killBtn = el("button", { class: "rp-kill danger", text: "Kill\u2026" });
    killBtn.addEventListener("click", () => doKill());
    buttons.append(sendBtn, wrapBtn, killBtn);
    const controlReason = el("div", { class: "rp-control-reason muted" });
    footer.append(steerStatus, textarea, buttons, controlReason);
    return { footer, steerStatus, textarea, sendBtn, wrapBtn, killBtn, controlReason };
  }

  function postOptions(body) {
    return {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-AgentControl-CSRF": deps.csrfToken(),
      },
      body: JSON.stringify(body),
    };
  }

  function getOptions() {
    return { headers: { "X-AgentControl-CSRF": deps.csrfToken() } };
  }

  function setActionStatus(text) {
    actionStatus = text || "";
    if (dom) dom.actionStatus.textContent = actionStatus;
  }

  function errText(err) {
    return err && err.message ? err.message : String(err);
  }

  async function doSend() {
    const text = dom.textarea.value.trim();
    if (!text) return;
    dom.sendBtn.disabled = true;
    try {
      await deps.fetchJson(
        `/api/runs/${encodeURIComponent(runId)}/steer`,
        postOptions({ text }),
      );
      dom.textarea.value = "";
      deps.onChange();
    } catch (err) {
      setActionStatus(errText(err));
    } finally {
      applyControls();
    }
  }

  async function doWrap() {
    if (!confirm(WRAP_UP_CONFIRM)) return;
    dom.wrapBtn.disabled = true;
    try {
      await deps.fetchJson(
        `/api/runs/${encodeURIComponent(runId)}/steer`,
        postOptions({ kind: "wrap_up" }),
      );
      deps.onChange();
    } catch (err) {
      setActionStatus(errText(err));
    } finally {
      applyControls();
    }
  }

  async function doKill() {
    if (!confirm(killConfirmText(row ? row.label : runId, header))) return;
    dom.killBtn.disabled = true;
    try {
      const result = await deps.fetchJson(
        `/api/runs/${encodeURIComponent(runId)}/kill`,
        postOptions({ confirm: true }),
      );
      setActionStatus(killOutcomeText(result));
      deps.onChange();
    } catch (err) {
      setActionStatus(errText(err));
    } finally {
      applyControls();
    }
  }

  async function doCancel(id, button) {
    if (button) button.disabled = true;
    try {
      await deps.fetchJson(
        `/api/runs/${encodeURIComponent(runId)}/steer/cancel`,
        postOptions({ id }),
      );
      deps.onChange();
    } catch (err) {
      setActionStatus(errText(err));
      if (button) button.disabled = false;
    }
  }
  // --- rendering -------------------------------------------------------

  function stepForIteration(iteration) {
    if (!activity || !Array.isArray(activity.steps)) return null;
    for (const step of activity.steps) {
      if (step.iteration === iteration) return step;
    }
    return null;
  }

  // Copy the user's open/closed state from replaced disclosures to their
  // replacements, matched by role (class), so refreshes do not collapse them.
  const DISCLOSURE_ROLES = ["rp-reasoning", "rp-output", "rp-brief"];
  function disclosuresByRole(root) {
    const found = new Map();
    const walk = (node) => {
      if (!node) return;
      if (node.tagName === "DETAILS" && node.classList) {
        for (const role of DISCLOSURE_ROLES) {
          if (node.classList.contains(role) && !found.has(role)) found.set(role, node);
        }
      }
      for (const child of node.children || []) walk(child);
    };
    walk(root);
    return found;
  }
  function keepDisclosureState(oldNode, freshNode) {
    if (!oldNode || !freshNode) return;
    const before = disclosuresByRole(oldNode);
    if (!before.size) return;
    for (const [role, details] of disclosuresByRole(freshNode)) {
      const prior = before.get(role);
      if (prior) details.open = !!prior.open;
    }
  }

  // Labels such as "Final report (writing…)" depend on whether the run is live.
  // When it stops being live, re-render those events at once rather than
  // waiting for a narration fetch that may fail.
  function refreshLiveLabels() {
    for (const event of events) {
      if (!event.live) continue;
      const node = eventNodes.get(event.seq);
      if (!node || !node.parentNode) continue;
      const fresh = renderEvent(event);
      if (!fresh) continue;
      keepDisclosureState(node, fresh);
      node.parentNode.insertBefore(fresh, node);
      node.remove();
      eventNodes.set(event.seq, fresh);
    }
  }

  function renderEvent(event) {
    switch (event.kind) {
      case "call": {
        if (!(event.attempt >= 2)) return null;
        const div = el("div", { class: "rp-retry warn" });
        div.textContent = `Retry: attempt ${event.attempt}/${event.max_attempts}`;
        return div;
      }
      case "status": {
        const div = el("div", { class: "rp-status warn" });
        div.textContent = statusText(event);
        return div;
      }
      case "narration": {
        const block = el("div", { class: "rp-narration-block" });
        const p = el("p", { class: "rp-narration" });
        p.textContent = event.text || "";
        block.appendChild(p);
        if (event.reasoning) {
          const details = el("details", { class: "rp-reasoning" });
          if (!reasoningFolded(event.reasoning)) details.setAttribute("open", "open");
          const summary = el("summary", { text: liveLabel("Reasoning", event, live) });
          const pre = el("pre", { class: "rp-text" });
          pre.textContent = event.reasoning;
          details.append(summary, pre);
          block.appendChild(details);
        }
        return block;
      }
      case "preview": {
        if (!previewVisible(event)) return null;
        const div = el("div", { class: "rp-preview" });
        const h = el("div", { class: "rp-preview-heading" });
        h.textContent = liveLabel(previewHeading(event), event, live);
        const pre = el("pre", { class: "rp-text rp-preview-text" });
        pre.textContent = event.text || "";
        div.append(h, pre);
        // Keep the newest lines in view once the node is in the document.
        if (typeof queueMicrotask === "function") {
          queueMicrotask(() => { pre.scrollTop = pre.scrollHeight; });
        }
        return div;
      }
      case "tool": {
        const div = el("div", { class: "rp-tool" });
        const code = el("code", { class: "rp-call" });
        code.textContent = event.name;
        const args = el("span", { class: "rp-args" });
        args.textContent = summarizeToolCall(event.name, event.args);
        div.append(code, args);
        if (event.output != null) {
          const details = el("details", { class: "rp-output" });
          const summary = el("summary");
          summary.textContent = outputLabel(event);
          const pre = el("pre");
          pre.textContent = event.output;
          details.append(summary, pre);
          div.appendChild(details);
        }
        return div;
      }
      case "anomaly": {
        const div = el("div", { class: "rp-anomaly warn" });
        div.textContent = `${event.name}: ${event.output || ""}`;
        return div;
      }
      case "guidance": {
        const div = el("div", { class: "rp-guidance" });
        div.textContent = `Guidance, turn ${event.iteration}: ${event.text || ""}`;
        return div;
      }
      case "finish":
        return null;
      case "notice": {
        const div = el("div", { class: "rp-notice" });
        div.textContent = event.text || "";
        return div;
      }
      case "final": {
        const section = el("section", { class: "rp-final" });
        const h3 = el("h3");
        h3.textContent = liveLabel(event.partial ? "Final report (partial)" : "Final report", event, live);
        const pre = el("pre", { class: "rp-text" });
        pre.textContent = event.text || "";
        section.append(h3, pre);
        return section;
      }
      default:
        return null;
    }
  }

  function turnHeadText(iteration) {
    let text = `Turn ${iteration}`;
    const step = stepForIteration(iteration);
    if (step) {
      if (step.started_at) {
        const t = Date.parse(step.started_at);
        if (Number.isFinite(t)) text += ` · ${new Date(t).toLocaleTimeString()}`;
      }
      const timing = deps.stepTimingText(step);
      if (timing) text += ` · ${timing}`;
    }
    return text;
  }

  function refreshTurnHeads() {
    for (const [iteration, entry] of turnSections) {
      entry.head.textContent = turnHeadText(iteration);
    }
  }
  function renderBrief() {
    const previous = dom.brief;
    if (dom.brief) dom.brief.remove();
    dom.brief = null;
    if (!brief) return;
    const details = el("details", { class: "rp-brief" });
    const summary = el("summary", { text: "Task brief" });
    const pre = el("pre", { class: "rp-text" });
    pre.textContent = brief;
    details.append(summary, pre);
    // Reset paths clear dom.brief first, so state never crosses a real reset.
    keepDisclosureState(previous, details);
    dom.timeline.insertBefore(details, dom.timeline.firstChild);
    dom.brief = details;
  }

  function ensureTurnSection(iteration) {
    let entry = turnSections.get(iteration);
    if (entry) return entry.section;
    const section = el("section", { class: "rp-turn" });
    section.setAttribute("data-iteration", String(iteration));
    const head = el("div", { class: "rp-turn-head" });
    head.textContent = turnHeadText(iteration);
    section.appendChild(head);
    dom.timeline.appendChild(section);
    turnSections.set(iteration, { section, head });
    return section;
  }

  // Incrementally render the timeline: rebuild only events with seq >= after.
  function renderTimeline(after, isReset) {
    const wasNear = nearBottom(
      dom.timeline.scrollTop,
      dom.timeline.scrollHeight,
      dom.timeline.clientHeight,
    );

    if (isReset) {
      for (const node of eventNodes.values()) if (node) node.remove();
      eventNodes.clear();
      for (const entry of turnSections.values()) entry.section.remove();
      turnSections.clear();
    }
    // Replaced nodes of this render only, so disclosure state can carry over
    // to the replacement of the same seq. Never captured on a true reset.
    const replaced = new Map();
    if (!isReset) {
      for (const [seq, node] of eventNodes) {
        if (seq >= after) {
          if (node) {
            replaced.set(seq, node);
            node.remove();
          }
          eventNodes.delete(seq);
        }
      }
    }

    let newCount = 0;
    for (const event of events) {
      if (eventNodes.has(event.seq)) continue;
      const child = renderEvent(event);
      if (child && replaced.has(event.seq)) keepDisclosureState(replaced.get(event.seq), child);
      const standalone =
        event.kind === "notice" || event.kind === "final" || event.iteration == null;
      if (!standalone) {
        const section = ensureTurnSection(event.iteration);
        if (child) {
          section.appendChild(child);
          newCount += 1;
        }
        eventNodes.set(event.seq, child || null);
      } else {
        if (child) {
          dom.timeline.appendChild(child);
          newCount += 1;
        }
        eventNodes.set(event.seq, child || null);
      }
    }

    // Drop turn sections that no longer have any event.
    const liveIters = new Set(
      events.filter((e) => e.iteration != null).map((e) => e.iteration),
    );
    for (const [iteration, entry] of turnSections) {
      if (!liveIters.has(iteration)) {
        entry.section.remove();
        turnSections.delete(iteration);
      }
    }

    if (wasNear) {
      dom.timeline.scrollTop = dom.timeline.scrollHeight;
      dom.jump.hidden = true;
    } else if (newCount > 0) {
      dom.jump.hidden = false;
      dom.jump.textContent = `${newCount} new steps`;
    }
  }

  function renderNotes() {
    const parts = [];
    if (masked > 0) parts.push(`${masked} values masked`);
    if (!available) parts.push("Transcript not found; showing timing only");
    if (pollError) parts.push(pollError);
    dom.notes.textContent = parts.join(" \u00b7 ");
  }

  function renderSteerStatus() {
    const box = dom.steerStatus;
    while (box.firstChild) box.removeChild(box.firstChild);
    for (const status of steer) {
      const line = el("div", { class: "rp-steer-line" });
      line.appendChild(document.createTextNode(steerStatusText(status)));
      if (status.state === "queued") {
        const cancel = el("button", { class: "mini", text: "Cancel" });
        cancel.addEventListener("click", () => doCancel(status.id, cancel));
        line.appendChild(cancel);
      }
      box.appendChild(line);
    }
  }
  function writeModeText() {
    if (!header) return null;
    if (header.worktree) return "worktree";
    if (header.writes === "staged") return "staged";
    if (header.writes === "disk") return "disk";
    return header.writes || null;
  }

  function renderHeader() {
    if (!dom) return;
    dom.title.textContent = row ? row.label : runId || "";
    const meta = dom.meta;
    while (meta.firstChild) meta.removeChild(meta.firstChild);
    if (!row) return;
    const badge = el("span", {
      class: "badge " + deps.liveStateClass(row.display_state),
    });
    badge.textContent = row.status === "failed" ? deps.runStatusText(row) : row.display_state;
    meta.appendChild(badge);
    const add = (text) => {
      if (text == null || text === "") return;
      const span = el("span");
      span.textContent = text;
      meta.appendChild(span);
    };
    add(deps.projectName(row.project_id));
    add(deps.formatElapsed(row.elapsed_seconds));
    add(deps.iterationText(row.iteration, row.max_iters));
    if (row.cost !== undefined) add(deps.formatCost(row.cost, row.cost_classification).text);
    const model = row.model + (row.effort ? ` \u00b7 ${row.effort}` : "");
    add(model);
    add(writeModeText());
    // Optional trusted getter; only exact project_id matches, never implies this run's outcome.
    const bound = typeof deps.goalTasks === "function"
      ? projectGoalTasks(deps.goalTasks(), row.project_id) : [];
    if (bound.length) {
      const box = el("div", { class: "rp-goal-tasks" });
      appendGoalTasks(box, bound, "Project logical tasks; separate from this run");
      meta.appendChild(box);
    }
  }

  function applyControls() {
    if (!dom) return;
    const state = controlsState(live ? row : null);
    dom.sendBtn.disabled = !state.steer.enabled;
    dom.wrapBtn.disabled = !state.steer.enabled;
    dom.textarea.disabled = !state.steer.enabled;
    dom.killBtn.disabled = !state.kill.enabled;
    const reason = state.steer.reason || state.kill.reason || "";
    dom.controlReason.textContent = reason;
  }

  function secondsSince(iso) {
    if (!iso) return null;
    const t = Date.parse(iso);
    if (!Number.isFinite(t)) return null;
    return (Date.now() + serverSkew - t) / 1000;
  }

  function renderPhase() {
    if (!dom) return;
    if (!live || !activity) {
      dom.phase.textContent = "";
      return;
    }
    const progress = activity.progress || null;
    dom.phase.textContent = deps.phaseText(
      activity.current_phase,
      secondsSince(activity.current_phase_since),
      progress ? progress.attempt : null,
      progress ? progress.max_attempts : null,
    );
  }
  // --- fetching --------------------------------------------------------

  async function fetchNarration(after) {
    if (fetching) return;
    fetching = true;
    const forRun = runId;
    try {
      const data = await deps.fetchJson(
        `/api/narration?run_id=${encodeURIComponent(forRun)}&after=${after}`,
        getOptions(),
      );
      if (forRun !== runId) return;
      available = data.available;
      if (data.header != null) header = data.header;
      if (after === 0 && data.brief != null) brief = data.brief;
      masked = data.masked || 0;
      truncated = data.truncated || false;
      steer = data.steer || [];
      const merged = mergeEvents(events, after, data);
      if (merged.reset) {
        events = [];
        for (const node of eventNodes.values()) if (node) node.remove();
        eventNodes.clear();
        for (const entry of turnSections.values()) entry.section.remove();
        turnSections.clear();
        brief = null;
        if (dom && dom.brief) {
          dom.brief.remove();
          dom.brief = null;
        }
        resetPending = true;
        return;
      }
      events = merged.events;
      pollError = "";
      if (after === 0) renderBrief();
      renderTimeline(after, false);
      renderNotes();
      renderSteerStatus();
    } catch (err) {
      pollError = errText(err);
      renderNotes();
    } finally {
      fetching = false;
      if (resetPending) {
        resetPending = false;
        fetchNarration(0);
      }
    }
  }

  async function fetchActivity() {
    const forRun = runId;
    try {
      const data = await deps.fetchJson(
        `/api/activity?run_id=${encodeURIComponent(forRun)}`,
        getOptions(),
      );
      if (forRun !== runId) return;
      activity = data;
      while (dom.diagnosis.firstChild) dom.diagnosis.removeChild(dom.diagnosis.firstChild);
      const lines = diagnosisLines(data.diagnosis);
      dom.diagnosis.hidden = lines.length === 0;
      for (const line of lines) dom.diagnosis.appendChild(el("div", { text: line }));
      if (data.server_now) {
        const t = Date.parse(data.server_now);
        if (Number.isFinite(t)) serverSkew = t - Date.now();
      }
      refreshTurnHeads();
      renderPhase();
    } catch (err) {
      pollError = errText(err);
      renderNotes();
    }
  }

  function startPolling() {
    stopTimers();
    if (live) {
      pollTimer = setInterval(() => {
        if (typeof document !== "undefined" && document.hidden) return;
        fetchNarration(nextAfter(events));
        fetchActivity();
      }, 2000);
    }
    phaseTimer = setInterval(renderPhase, 100);
  }

  function stopTimers() {
    if (pollTimer) clearInterval(pollTimer);
    if (phaseTimer) clearInterval(phaseTimer);
    pollTimer = null;
    phaseTimer = null;
  }
  // --- public API ------------------------------------------------------

  function resetRunState() {
    events = [];
    steer = [];
    header = null;
    brief = null;
    masked = 0;
    truncated = false;
    available = true;
    activity = null;
    serverSkew = 0;
    refetchOnEnd = false;
    resetPending = false;
    pollError = "";
    actionStatus = "";
    eventNodes.clear();
    turnSections.clear();
    if (dom) {
      dom.notes.textContent = "";
      dom.steerStatus.textContent = "";
      dom.actionStatus.textContent = "";
      dom.phase.textContent = "";
      dom.diagnosis.hidden = true;
      while (dom.diagnosis.firstChild) dom.diagnosis.removeChild(dom.diagnosis.firstChild);
      dom.jump.hidden = true;
      dom.textarea.value = "";
      if (dom.brief) {
        dom.brief.remove();
        dom.brief = null;
      }
      while (dom.timeline.firstChild) dom.timeline.removeChild(dom.timeline.firstChild);
    }
  }

  function open(newRunId, newOpener) {
    build();
    stopTimers();
    runId = newRunId;
    opener = newOpener || null;
    resetRunState();
    dom.aside.hidden = false;
    document.body.classList.add("run-panel-open");
    history.replaceState(null, "", hashForRun(runId));
    dom.title.focus();
    row = null;
    if (lastData) {
      // Apply the latest dashboard data at once so a history run shows history
      // chrome immediately, without waiting for the next update().
      const found = findRow(lastData, runId);
      row = found.row;
      live = found.live;
      renderHeader();
      applyControls();
      dom.footer.hidden = !live;
    } else {
      // No data yet: assume live so controls appear for running runs.
      live = true;
    }
    fetchNarration(0);
    fetchActivity();
    startPolling();
    applyControls();
  }

  function close() {
    if (!dom) return;
    stopTimers();
    dom.aside.hidden = true;
    document.body.classList.remove("run-panel-open");
    history.replaceState(null, "", location.pathname + location.search);
    runId = null;
    if (opener && typeof opener.focus === "function") opener.focus();
    opener = null;
    if (typeof deps.onClose === "function") deps.onClose();
  }

  function update(data) {
    lastData = data;
    if (!dom || !runId) return;
    const found = findRow(data, runId);
    row = found.row;
    const wasLive = live;
    live = found.live;
    renderHeader();
    applyControls();
    // Show/hide the live-only controls footer.
    dom.footer.hidden = !live;
    if (wasLive && !live) {
      refreshLiveLabels();
      // The run just left data.live: fetch once more to catch the final report.
      if (!refetchOnEnd) {
        refetchOnEnd = true;
        stopTimers();
        fetchNarration(nextAfter(events));
        fetchActivity();
        phaseTimer = setInterval(renderPhase, 100);
      }
    }
  }

  function isOpen() {
    return !!dom && dom.aside.hidden === false;
  }

  function currentRunId() {
    return runId;
  }

  return { open, close, update, isOpen, currentRunId };
}

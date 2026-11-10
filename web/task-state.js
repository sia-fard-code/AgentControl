// Logical task observations (snapshot.goal_tasks): view-only, never acceptance
// or control authority, never live lease proof. Shared by dashboard and panel.

export const TASK_STATES = ["continuing", "awaiting-verification", "reconciliation",
  "blocked", "verified-complete"];
// Mirrors src/agent_control/task_state.py _REASONS / _OWNERS.
const REASONS = ["cancelled", "revoked", "deadline", "no_progress", "replan_limit",
  "attempt_limit", "usage_limit", "invalid_evidence", "launch_failed", "operator_required",
  "planner_limit", "worker_active", "planning", "next_slice_ready", "verification_due",
  "effect_uncertain", "ownership_unavailable", "initial_snapshot", "state_inconsistent",
  "verified"];
const OWNERS = ["worker", "coordinator", "none"];
const ID = /^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$/;
const SHA = /^[0-9a-f]{64}$/;

const pick = (value, allowed) => (allowed.includes(value) ? value : "unknown");
const count = (v, nullText = "unknown") =>
  Number.isSafeInteger(v) && v >= 0 ? String(v) : v === null ? nullText : "unknown";
const ids = (v) => (Array.isArray(v) ? v.filter((x) => typeof x === "string" && ID.test(x)) : []);
const safeId = (v) => (typeof v === "string" && ID.test(v) ? v : "unknown");

export function taskStateViewModel(row) {
  const r = row && typeof row === "object" ? row : {};
  const state = pick(r.state, TASK_STATES);
  const usage = r.usage && typeof r.usage === "object" ? r.usage : {};
  const limits = r.limits && typeof r.limits === "object" ? r.limits : null;
  const limit = (key) => {
    if (!limits) return "no cap reported";
    const value = limits[key];
    if ((key === "deadline" || key === "stop_grace_seconds") &&
        typeof value === "number" && Number.isFinite(value) && value >= 0) return String(value);
    return count(value, "no cap");
  };
  const required = ids(r.required_criteria);
  return {
    project_id: safeId(r.project_id),
    orchestration_id: safeId(r.orchestration_id),
    state,
    // Success only from Task state, never from any worker run status.
    verified: state === "verified-complete",
    reason: pick(r.reason, REASONS),
    candidate: r.candidate_sha256 === null ? "none"
      : typeof r.candidate_sha256 === "string" && SHA.test(r.candidate_sha256)
        ? r.candidate_sha256.slice(0, 12) : "unknown",
    verifier_owner: pick(r.verifier_owner, OWNERS),
    rerun_owed: r.rerun_owed === true ? "yes" : r.rerun_owed === false ? "no" : "unknown",
    active_workers: count(r.active_workers),
    unresolved_workers: count(r.unresolved_workers),
    pending_evaluations: count(r.pending_evaluations),
    criteria: `${ids(r.satisfied_criteria).length}/${required.length}`,
    criteria_ids: required,
    usage_charged: count(usage.charged),
    usage_reserved: count(usage.reserved),
    usage_unknown_calls: count(usage.unknown_calls),
    usage_total: count(usage.total_usage),
    limit_replans: limit("max_replans"),
    limit_attempts: limit("max_total_attempts"),
    limit_workers: limit("max_concurrent_workers"),
    limit_planner_calls: limit("max_planner_calls"),
    limit_report_calls: limit("max_final_report_calls"),
    limit_stop_grace: limit("stop_grace_seconds"),
    limit_deadline: limit("deadline"),
    limit_usage: limit("max_usage"),
    limit_worker_turns: limit("worker_turn_limit"),
  };
}

const esc = (s) => String(s).replace(/[&<>"']/g, (c) =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);

// Rows for one exact, known project id; no id means no binding and no fallback.
export function projectGoalTasks(rows, projectId) {
  if (!Array.isArray(rows) || typeof projectId !== "string" || !ID.test(projectId)) return [];
  return rows.filter((r) => r && r.project_id === projectId);
}

const TITLE = "Logical tasks \u2014 last parent observation";
const OBSERVATION = "Last parent observation; not live lease proof, not acceptance evidence.";
const EMPTY = "No registered task observations";
const taskFields = (vm) => [
  ["Project", vm.project_id], ["Orchestration", vm.orchestration_id],
  ["State", vm.verified ? "verified-complete (task verified)" : vm.state],
  ["Reason", vm.reason], ["Candidate", vm.candidate],
  ["Verifier owner", vm.verifier_owner], ["Rerun owed", vm.rerun_owed],
  ["Active workers", vm.active_workers], ["Unresolved workers", vm.unresolved_workers],
  ["Pending evaluations", vm.pending_evaluations],
  ["Criteria", `${vm.criteria} ${vm.criteria_ids.join(", ")}`.trim()],
  ["Usage charged", vm.usage_charged], ["Usage reserved", vm.usage_reserved],
  ["Unknown-usage calls", vm.usage_unknown_calls], ["Total usage", vm.usage_total],
  ["Replan cap", vm.limit_replans], ["Admission cap", vm.limit_attempts],
  ["Concurrent workers", vm.limit_workers], ["Planner-call cap", vm.limit_planner_calls],
  ["Report-call cap", vm.limit_report_calls], ["Stop grace (seconds)", vm.limit_stop_grace],
  ["Deadline", vm.limit_deadline], ["Usage cap", vm.limit_usage],
  ["Worker turn cap", vm.limit_worker_turns],
];

// Absent/non-list goal_tasks -> "" so callers create no DOM.
export function renderGoalTasksHtml(rows, title = TITLE) {
  if (!Array.isArray(rows)) return "";
  const head = `<h3>${esc(title)}</h3><p class="muted">${esc(OBSERVATION)}</p>`;
  if (!rows.length) return head + `<p class="muted">${esc(EMPTY)}</p>`;
  return head + rows.map((row) => {
    const vm = taskStateViewModel(row);
    return `<dl class="goal-task" data-state="${esc(vm.state)}">` +
      taskFields(vm).map(([k, v]) => `<dt>${esc(k)}</dt><dd>${esc(v)}</dd>`).join("") + "</dl>";
  }).join("");
}

export function appendGoalTasks(container, rows, title = TITLE) {
  if (!Array.isArray(rows)) return;
  const add = (parent, tag, text, className) => {
    const node = container.ownerDocument.createElement(tag);
    if (text !== undefined) node.textContent = text;
    if (className) node.setAttribute("class", className);
    parent.appendChild(node);
    return node;
  };
  add(container, "h3", title);
  add(container, "p", OBSERVATION, "muted");
  if (!rows.length) add(container, "p", EMPTY, "muted");
  for (const row of rows) {
    const vm = taskStateViewModel(row);
    const dl = add(container, "dl", undefined, "goal-task");
    dl.setAttribute("data-state", vm.state);
    for (const [label, value] of taskFields(vm)) {
      add(dl, "dt", label);
      add(dl, "dd", value);
    }
  }
}

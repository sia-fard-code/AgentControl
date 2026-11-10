import test from "node:test";
import assert from "node:assert/strict";
import { fileURLToPath, pathToFileURL } from "node:url";
import { dirname, resolve } from "node:path";

const here = dirname(fileURLToPath(import.meta.url));
const url = (p) => pathToFileURL(resolve(here, p)).href;
const { TASK_STATES, taskStateViewModel, renderGoalTasksHtml, projectGoalTasks } =
  await import(url("../../web/task-state.js"));
const taskState = await import(url("../../web/task-state.js"));

const row = (over = {}) => ({
  project_id: "p1", orchestration_id: "orch-1", schema_version: 1,
  envelope_sha256: "e".repeat(64), owner_epoch: 3, state: "continuing", reason: "worker_active",
  candidate_sha256: "abcdef0123456789".repeat(4), seed_receipt_sha256: "f".repeat(64),
  plan_revision: 2, active_workers: 1, unresolved_workers: 0, pending_evaluations: 0,
  verifier_owner: "coordinator", rerun_owed: false, satisfied_criteria: ["c1"],
  required_criteria: ["c1", "c2", "c3"],
  usage: { charged: 0, reserved: 0, unknown_calls: 0, total_usage: null }, limits: null,
  observation: "last-parent-observation", ...over,
});

test("all five states are known and only verified-complete is verified", () => {
  assert.deepEqual(TASK_STATES, ["continuing", "awaiting-verification", "reconciliation",
    "blocked", "verified-complete"]);
  for (const state of TASK_STATES) {
    const vm = taskStateViewModel(row({ state }));
    assert.equal(vm.state, state);
    assert.equal(vm.verified, state === "verified-complete");
    assert.match(renderGoalTasksHtml([row({ state })]), new RegExp(state));
  }
});

test("worker-run style complete/failed or malformed state never becomes verified", () => {
  for (const state of ["complete", "failed", "VERIFIED-COMPLETE", "", null, 7, undefined]) {
    const vm = taskStateViewModel(row({ state, status: "complete" }));
    assert.equal(vm.verified, false);
    assert.equal(vm.state, "unknown");
  }
  assert.equal(taskStateViewModel(null).verified, false);
  assert.equal(taskStateViewModel(row({ state: "continuing", status: "complete" })).verified, false);
});

test("html is escaped, extras ignored, no raw json", () => {
  const evil = "<img src=x onerror=alert(1)>";
  const html = renderGoalTasksHtml([row({
    project_id: evil, orchestration_id: evil, verifier_owner: evil, reason: evil,
    body: "SECRET_BODY", source_path: "C:/secret/path", prompt: "SECRET_PROMPT",
    candidate_sha256: evil,
  })]);
  assert.ok(!html.includes("<img"));
  assert.ok(!html.includes("onerror"));
  assert.ok(renderGoalTasksHtml([], evil).includes("&lt;img src=x onerror=alert(1)&gt;"));
  for (const s of ["SECRET_BODY", "C:/secret/path", "SECRET_PROMPT", "{\"", "[object"]) {
    assert.ok(!html.includes(s), s);
  }
  assert.equal(taskStateViewModel(row({ reason: evil })).reason, "unknown");
  assert.match(html, /Logical tasks — last parent observation/);
  assert.match(html, /not live lease proof/i);
});

test("candidate pin short hex, owner, rerun, counts, null vs 0", () => {
  const vm = taskStateViewModel(row({ rerun_owed: true,
    usage: { charged: 0, reserved: 5, unknown_calls: 0, total_usage: null },
    limits: { max_usage: 0 } }));
  assert.equal(vm.candidate, "abcdef012345");
  assert.equal(vm.verifier_owner, "coordinator");
  assert.equal(taskStateViewModel(row({ verifier_owner: "<b>" })).verifier_owner, "unknown");
  assert.deepEqual(taskStateViewModel(row()).criteria_ids, ["c1", "c2", "c3"]);
  assert.deepEqual(taskStateViewModel(row({ required_criteria: ["ok", "<x>"] })).criteria_ids, ["ok"]);
  assert.equal(vm.rerun_owed, "yes");
  assert.equal(vm.active_workers, "1");
  assert.equal(vm.unresolved_workers, "0");
  assert.equal(vm.criteria, "1/3");
  assert.equal(vm.usage_charged, "0");
  assert.equal(vm.usage_total, "unknown");
  assert.equal(vm.limit_usage, "0");
  assert.equal(taskStateViewModel(row()).limit_usage, "no cap reported");
  assert.equal(taskStateViewModel(row({ candidate_sha256: null })).candidate, "none");
});

test("absent key renders nothing; empty list shows explicit note", () => {
  assert.equal(renderGoalTasksHtml(undefined), "");
  assert.equal(renderGoalTasksHtml(null), "");
  assert.match(renderGoalTasksHtml([]), /No registered task observations/);
});

test("project binding only on exact known project id", () => {
  const rows = [row({ project_id: "p1" }), row({ project_id: "p2" })];
  assert.equal(projectGoalTasks(rows, "p1").length, 1);
  assert.deepEqual(projectGoalTasks(rows, ""), []);
  assert.deepEqual(projectGoalTasks(rows, undefined), []);
  assert.deepEqual(projectGoalTasks(undefined, "p1"), []);
  const html = renderGoalTasksHtml(projectGoalTasks(rows, "p1"),
    "Project logical tasks; separate from this run");
  assert.match(html, /Project logical tasks; separate from this run/);
  assert.ok(!html.includes("p2"));
});

test("dashboard and panel still import (no circular import)", async () => {
  const dash = await import(url("../../web/dashboard.js"));
  assert.equal(typeof dash.formatCounter, "function");
});

test("all optional operator caps are visible without replacing null by zero", () => {
  const html = renderGoalTasksHtml([row({ limits: {
    max_replans: 0, max_total_attempts: null, max_concurrent_workers: 8,
    max_planner_calls: null, max_final_report_calls: null,
    stop_grace_seconds: 60, deadline: null, max_usage: null, worker_turn_limit: 200,
  } })]);
  for (const label of ["Replan cap", "Admission cap", "Concurrent workers", "Planner-call cap",
    "Report-call cap", "Stop grace", "Deadline", "Usage cap", "Worker turn cap"]) {
    assert.ok(html.includes(label), label);
  }
  assert.match(html, /no cap/);
});

test("operator caps preserve null, zero, missing, invalid and fractional times", () => {
  const caps = {
    max_replans: "limit_replans", max_total_attempts: "limit_attempts",
    max_concurrent_workers: "limit_workers", max_planner_calls: "limit_planner_calls",
    max_final_report_calls: "limit_report_calls", stop_grace_seconds: "limit_stop_grace",
    deadline: "limit_deadline", max_usage: "limit_usage", worker_turn_limit: "limit_worker_turns",
  };
  for (const [key, field] of Object.entries(caps)) {
    for (const [value, expected] of [[null, "no cap"], [0, "0"], [7, "7"],
      [undefined, "unknown"], [-1, "unknown"], [Infinity, "unknown"], [NaN, "unknown"],
      ["7", "unknown"], [false, "unknown"], [{ body: "SECRET_BODY" }, "unknown"]]) {
      assert.equal(taskStateViewModel(row({ limits: { [key]: value } }))[field], expected, key);
    }
    assert.equal(taskStateViewModel(row())[field], "no cap reported", key);
    assert.equal(taskStateViewModel(row({ limits: {} }))[field], "unknown", key);
  }
  const vm = taskStateViewModel(row({ limits: {
    deadline: 1700000000.125, stop_grace_seconds: 0.5, max_replans: 0.5,
  } }));
  assert.equal(vm.limit_deadline, "1700000000.125");
  assert.equal(vm.limit_stop_grace, "0.5");
  assert.equal(vm.limit_replans, "unknown");
});

test("panel-compatible renderer uses only DOM text nodes and fixed element names", () => {
  const doc = { createElement: (tag) => {
    assert.ok(["h3", "p", "dl", "dt", "dd"].includes(tag), tag);
    return new Element(tag);
  } };
  class Element {
    constructor(tag) { this.tagName = tag; this.children = []; this.ownerDocument = doc; }
    appendChild(child) { this.children.push(child); return child; }
    setAttribute(name, value) { this[name] = value; }
    replaceChildren(...children) { this.children = children; }
  }
  const target = new Element("section");
  assert.equal(typeof taskState.appendGoalTasks, "function");
  taskState.appendGoalTasks(target, [row({ body: "SECRET_BODY" })], "Project observations");
  const text = (node) => [node.textContent || "", ...node.children.map(text)].join(" ");
  assert.match(text(target), /Project observations/);
  assert.match(text(target), /continuing/);
  assert.match(text(target), /not live lease proof, not acceptance evidence/);
  assert.ok(!text(target).includes("SECRET_BODY"));
  const literalTitle = new Element("section");
  taskState.appendGoalTasks(literalTitle, [], "<img src=x onerror=alert(1)>");
  assert.equal(literalTitle.children[0].textContent, "<img src=x onerror=alert(1)>");
  assert.match(text(literalTitle), /No registered task observations/);
  const absent = new Element("section");
  taskState.appendGoalTasks(absent, undefined);
  assert.equal(absent.children.length, 0);
});

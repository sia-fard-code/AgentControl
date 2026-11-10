import test from "node:test";
import assert from "node:assert/strict";
import { diagnosisLines } from "../../web/panel.js";

test("engine and child exits remain distinct, including measured zero", () => {
  const lines = diagnosisLines({ category: "process_exit_unknown", evidence: "observed",
    engine_exit_code: 0, last_command_exit_code: 1, coverage: {} });
  assert.ok(lines.includes("Engine exit: 0; last command exit: 1"));
  assert.ok(lines[0].includes("cause unknown"));
});

test("recorded exception and unfinished stage have explicit evidence", () => {
  const lines = diagnosisLines({ category: "engine_exception", evidence: "recorded",
    exception: { class: "TypeError", source: "delegate.py", line: 42 },
    unfinished: { stage: "result", turn: 11, call_index: 2 }, coverage: {} });
  assert.ok(lines.includes("Recorded exception: TypeError at delegate.py:42"));
  assert.ok(lines.some(line => line.includes("result") && line.includes("turn 11")));
});

test("missing or partial diagnostics do not claim complete attribution", () => {
  assert.deepEqual(diagnosisLines(null), []);
  const lines = diagnosisLines({ category: "process_exit_unknown", evidence: "unknown",
    coverage: { engine: "missing", partial: true } });
  assert.ok(lines.some(line => line.includes("incomplete")));
  assert.ok(lines.some(line => line.includes("unavailable")));
});

test("unknown categories and extraneous body fields are not rendered", () => {
  const lines = diagnosisLines({ category: "SYNTHETIC_PRIVATE", output: "SYNTHETIC_PRIVATE",
    traceback: "SYNTHETIC_PRIVATE", coverage: {} });
  assert.ok(!lines.join(" ").includes("SYNTHETIC_PRIVATE"));
});

test("prototype property names are not diagnosis categories", () => {
  assert.equal(diagnosisLines({ category: "toString", coverage: {} })[0], "Diagnosis unavailable");
});

test("secondary terminal-write failure and unknown build remain visible", () => {
  const lines = diagnosisLines({ category: "engine_exception", evidence: "recorded",
    bundle_id: null, terminal: { persisted: false, indexes_written: 1, indexes_attempted: 2 },
    coverage: { engine: "recorded" } });
  assert.ok(lines.some(line => line.includes("1/2") && line.includes("incomplete")));
  assert.ok(lines.some(line => line.includes("Engine build: unknown")));
});

test("cleanup and runtime details do not hide the primary engine failure", () => {
  const lines = diagnosisLines({ category: "engine_exception", evidence: "recorded",
    exception: { class: "TypeError" }, cleanup_exception: { class: "OSError" },
    execution_profile: "resilient-v1", runtime_version: "3.13.9", architecture: "64bit", coverage: {} });
  assert.equal(lines[0], "Engine exception recorded");
  assert.ok(lines.some(line => line.includes("Cleanup failure recorded: OSError")));
  assert.ok(lines.some(line => line.includes("resilient-v1") && line.includes("3.13.9")));
});

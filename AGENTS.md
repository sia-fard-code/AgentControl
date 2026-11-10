# AgentControl Engineering Rules

- Read the approved specification and implementation plan before changing behavior.
- Use test-driven development and keep modules focused.
- Every test must inject temporary paths; never write live user accounting state.
- Never place prompts, assistant content, tool results, credentials, response bodies, or complete sidecars in source, fixtures, logs, reports, AgentControl state, or APIs. The one exception is `GET /api/narration`, which reads a run's own transcript on demand for the loopback operator and never stores, logs, or writes it to disk. Test fixtures use synthetic text only.
- Do not deploy the shared engine, apply live migration, start the production listener, create scheduled tasks, or modify Projecta/Projectb without explicit operator approval.
- Preserve `null` versus measured `0`, append-only history, project identity, and repository policy boundaries.
- Shell searches: write plain `grep` or `rg`, never `rtk grep`; rtk's grep silently caps and changes results. See `docs/rtk-grep.md`.
- Release 1 has no dispatch, budget enforcement, database, frontend framework, or remote binding. Its only process control is the operator's verified kill of a run's engine process tree, and its only input to an agent is the operator's steer through the engine's own `.steer` file (amended 2026-09-24; see `docs/superpowers/specs/2026-09-24-narration-and-intervention-design.md`).

## Dispatch anomalies are part of the active scope

This rule applies to every session, coordinator, and delegated worker in this repository.

- Before dispatch, require the worker to read this file. Include this rule in each brief and continuation handoff. Do not assume workers load repository instructions automatically.
- The coordinating session owns investigation and closure. Delegation does not transfer that responsibility.
- Treat unexpected dispatch, tool, monitoring, recovery, completion, or accounting signals as work in the active scope. Examples include unexplained interruptions, stalled workers, missing results, budget refusals, conflicting status, and uncertain command effects.
- Add each anomaly to the active task list and goal when you observe it. Keep the original deliverable visible. Pause dependent work when the anomaly makes its results or command effects unreliable.
- Preserve bounded evidence: run and launch IDs, timestamps, exit codes, failure codes, and references to original artifacts. Inspect original artifacts in place. Follow the privacy rules above.
- Check the original execution state and effects before retrying or replacing a worker. Never replay commands with unknown outcomes.
- Reproduce the anomaly safely. Find its root cause. If evidence is insufficient, run the smallest safe diagnostic that can resolve the uncertainty.
- Write a failing regression test before repairing a defect. Implement the smallest complete repair within the approved design. Check both the repair and the original deliverable in isolation.
- Close each anomaly with evidence of repair, evidence that behavior is expected, or a concrete blocker. A successful primary deliverable does not close an unresolved anomaly. Retries, replacement workers, and labels such as "transient" or "outside scope" are not root causes or verified repairs.
- Update relevant regression tests and existing instructions when the cause reveals a reusable lesson. Avoid speculative refactors and unrelated documentation.
- This rule authorizes in-repository defect investigation and repair, not deployment or other gated actions. Keep existing specification, privacy, repository policy, and approval requirements. Do not broaden command or filesystem permissions to make a failure disappear.
- Request operator approval if a repair requires a gated action. Keep the anomaly open while that approval is pending. Continue independent safe work when useful, but report the blocker. Do not claim complete success while an anomaly remains unexplained.

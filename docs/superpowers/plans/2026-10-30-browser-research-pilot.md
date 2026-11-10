# Browser Research Pilot Implementation Plan

## Deployed outcome and use

The operator subsequently authorized making this usable and deploying it. The
pilot-only restrictions below are historical; the deployment amendment in the
specification supersedes them. The working implementation is:

- `tools/browser-research/server.mjs`: narrow stdio MCP; inline bounded rendered
  text, final source URLs, retrieval time, and links; no model-supplied code.
- `tools/browser-research/public_proxy.py`: public-only, numeric-IP-pinned
  HTTP(S) proxy for the isolated browser's complete network path.
- `scripts/deploy_browser_research.py`: selective browser-asset/policy deployment,
  preview, backups, and automatic asset rollback if the policy swap fails.
- `.claude/skills/public-web-research/SKILL.md`: usage and untrusted-page guidance.

The reviewed assets are installed under `~/.claude/bin/browser-research/`. This
project's live `research_browser` MCP definition is enabled with one exact launch
argv and only navigate/back/close. Shell permissions, the 30k cap, other projects,
and the shared engine deployment are unchanged by this work. The runtime uses the
existing cached Playwright driver and official headless-shell binary; keep those
dependencies available. It does not attach the operator's signed-in browser.

Use a new label each time; the existing harness recreates worktrees by label:

```powershell
$label = "web-study-" + [guid]::NewGuid().ToString("N").Substring(0, 8)
python "$env:USERPROFILE\.claude\bin\delegate.py" -C "D:\repos\AgentControl" --worktree --write --skill ponytail --skill public-web-research --label $label -t "Research the question using public sources and cite the URLs."
```

Parallel studies use separate launches/labels with the same original `-C` root.
The explicit `--write` approves mutating browser state; `--worktree` keeps any
filesystem writes off the original checkout. Native MCP discovery is required
before calling browser tools. The browser starts lazily and is closed by the
research skill. No persistent listener or scheduled service is installed.

Two simultaneous deployed workers read official Python and Playwright sources;
a third worker searched Bing and followed a real official result. All three
closed their browsers and left their disposable worktrees unchanged. The tests
also exercise real JS rendering/cookie isolation, private redirect rejection,
temporary state removal, bounded extraction, exact permission grants, and rollback.

Deploy only these browser assets; do not use the general engine deploy command
for this rollout. Require an exclusive policy-edit window. Before any future
rollback, check for concurrent policy changes: restore only the research server
entry and its exact launch grant, never replace an unrelated newer policy with a
whole backup. The deployment script preserves all existing shell/other-server
permissions and refuses an installation directory containing unowned files.

Limitations: anonymous public HTTP(S), ports 80/443, GET/HEAD only; no login,
forms, uploads, downloads, or interactive-site bypasses. Some sites will fail under
these controls. Long responses are deliberately truncated with a notice. Page
text remains untrusted and is sent to the configured model gateway in normal run
transcripts; no research content is added to AgentControl state or accounting.

> **For agentic workers:** Use the executing-plans workflow task-by-task. Execution
> is through the machine's delegated-agent harness, not built-in subagents.

**Goal:** Produce a disabled, narrowly scoped browser policy and demonstrate two
isolated research workers against synthetic local pages without activating public
web access.

**Architecture:** Reuse `engine/harness_mcp.py` unchanged. A standalone policy
fragment has no authorized launch and remains disabled. Offline regression tests
pin the template; a separately opted-in smoke test supplies temporary paths and
launches a pinned local Playwright MCP only for operator-owned loopback fixtures.

**Tech stack:** Python/pytest, existing stdlib MCP client, installed Node executable,
cached `@playwright/mcp` version `0.0.83`, an existing supported browser, and
synthetic stdlib HTTP fixtures. No dependency installation or shared deployment.

## Global constraints

- Follow `docs/superpowers/specs/2026-10-30-browser-research-pilot-design.md`.
- Do not change `.claude/agent-policy.json`, global config, shared engine files,
  existing command permissions, or the 30,000-character default cap.
- Do not commit, deploy, install browsers/packages, enable public access, change
  network settings, or create persistent/background services.
- All test roots, HOME/USERPROFILE, browser outputs, and state are temporary.
- Fixtures are synthetic. Do not persist or print page bodies, prompts, tool
  results, credentials, cookies, or responses outside temporary test fixtures.
- Only `browser_navigate`, `browser_navigate_back`, and `browser_close` are allowed.
- Mark all three tools mutating; authorize browser state changes only inside the
  temporary test managers via `allow_mutation=True`. Preserve the live guard.
- Public-web network containment is unverified and remains an activation blocker.

## Files and responsibilities

- Create `config/browser-research-pilot.json`: disabled standalone MCP fragment,
  no executable grant or shell-policy changes, explicit narrow tools and safe launch options.
- Create `tests/test_browser_research_pilot.py`: offline template and MCP-boundary
  regression tests using synthetic temporary paths.
- Create `tests/test_browser_research_smoke.py`: explicit opt-in real-browser
  integration test with synthetic localhost pages and independent workers.
- The coordinator owns this plan and its specification; the executor does not edit
  unrelated source or documentation.

## Task 1: Fail-closed template and offline tests

**Consumes:** `harness_mcp.validate_mcp_policy(value)` and its existing client.
**Produces:** a policy template accepted while disabled and rejected if enabled
without exact launch authorization. No production engine behavior is changed.

- [ ] Write the tests before creating the template. Core assertions:

```python
policy = json.loads(template_path.read_text(encoding="utf-8"))
assert set(policy) == {"allowed_commands", "servers"}
assert policy["allowed_commands"] == []
server = policy["servers"]["research_browser"]
assert server["enabled"] is False
assert set(server["allowed_tools"]) == {
    "browser_navigate", "browser_navigate_back", "browser_close"
}
```

- [ ] Run `python -m pytest -q tests/test_browser_research_pilot.py`; establish a
  meaningful failure for the missing template before creating it.
- [ ] Create the minimal MCP JSON fragment with explicit Node/CLI path placeholders,
  no launch grants, and a disabled server. Include `--headless`,
  `--isolated`, `--sandbox`, `--block-service-workers`, `--no-webmcp`, `--codegen
  none`, `--image-responses omit`, `--timeout-action 5000`, `--timeout-navigation
  15000`, and `--timeout-settle 1000`. Output-directory selection is test-injected,
  not a shared fixed path. Describe the unverified activation gate in this spec;
  the MCP fragment has only existing schema keys, not an unsupported comment key.
- [ ] Test required flags and forbidden flags (CDP/extension/endpoint, storage
  state/secrets, persistent user data, extra capabilities, disabled sandbox,
  save-session, remote listener). Keep explicit tool names, no wildcard rules.
- [ ] Test activation without authorization fails using the existing validator:

```python
candidate = copy.deepcopy(policy)
candidate["servers"]["research_browser"]["enabled"] = True
with pytest.raises(hm.MCPError):
    hm.validate_mcp_policy(candidate)
```

- [ ] In temporary roots, load a minimal full-policy fixture with
  `allowed_cmds=[["git", "status"]]`, then the SAME fixture with the MCP fragment
  added, through source `delegate.load_policy()`. Assert shell permissions stay
  identical and the cap stays 30k; execute no commands. This is test-only composition,
  not a production grant. A full delegate policy cannot have an empty command list;
  do not change that validator. Do not write live policy or accounting state.
  Use a synthetic MCP server fixture where useful
  to prove disabled servers never start and disallowed tools cannot be called.
- [ ] Re-run the focused tests; record red and green outcomes in the executor's
  final summary, not a repository report.

## Task 2: Explicit local-only real-browser smoke

**Consumes:** Task 1 template, `hm.MCPManager(policy, root)`;
`list_tools(server)`, `native_tools()`, `call(name, args)`, and `close()`.
**Produces:** an opt-in integration test, not a research launcher.

- [ ] Require explicit environment opt-in `AGENTCONTROL_BROWSER_SMOKE=1` and
  injected absolute `AGENTCONTROL_BROWSER_NODE` / `AGENTCONTROL_BROWSER_MCP_CLI`
  paths. If opted in, missing paths, wrong package identity/version, unavailable
  browser, or missing required tool schemas must FAIL rather than skip. Without
  opt-in, skip without starting a browser or contacting a network.
- [ ] Verify the selected entrypoint belongs to `@playwright/mcp` `0.0.83`, then
  replace template placeholders only in an in-memory copy. Authorize exactly that
  argv only in temporary fixture policy. Give each manager distinct temporary
  root/output/home paths and a finite timeout. Do not install anything.
- [ ] Start a bounded `ThreadingHTTPServer` fixture bound to `127.0.0.1` with an
  OS-assigned port and suppressed access logs. Serve only synthetic routes:
  `/seed?worker=alpha`, `/seed?worker=beta`, `/inspect`, and a link/index route.
  Pages set/read synthetic cookies and render a marker through JavaScript. No
  external assets or requests. Stop/join the server in `finally` cleanup.
- [ ] Drive two managers concurrently using `ThreadPoolExecutor(max_workers=2)`.
  Discover actual tools/schemas; verify the exposed set is exactly the three
  approved names and none accepts arbitrary code or output-filename arguments.
  Navigate A/B to their separate seed pages, then inspect both. Assert each sees
  its own marker/cookie, not the other's, and one worker's navigation/back/close
  does not alter the other's page state.
- [ ] Verify unknown/evaluation/snapshot-output tools are refused by the existing
  client. Avoid assertions that print response content. Keep provenance checks
  about fixture URLs, not a claim of real public citations or search quality.
- [ ] Capture owned process IDs/temporary paths as metadata only, and verify
  close/failure cleanup with finite deadlines. A failed test must still close both
  managers and fixture resources. Do not describe process cleanup as network
  containment. Document any cleanup behavior not actually verified.
- [ ] For startup errors emit static categories such as `browser_target_closed`
  or `windows_token_missing`, never arbitrary error lines/command logs. If both
  installed browsers fail under the required sandbox, leave the pilot blocked;
  do not remove sandboxing/Job containment or attach an operator browser.
- [ ] Run default smoke without opt-in (must skip). The coordinator then runs the
  opted-in test with observed local paths. No public sites are visited.

## Task 3: Integration verification and scope review

- [ ] Run `python -m pytest -q tests/test_browser_research_pilot.py
  tests/test_browser_research_smoke.py tests/test_engine_mcp_runtime.py
  tests/test_engine_capabilities.py tests/test_capability_review_safety.py`.
- [ ] Review the complete diff: only the new template/tests and approved docs;
  live policy/config, engine, dependency files, and source-content storage unchanged.
- [ ] Run `python -m pytest -q` and
  `node --test tests/test_kilo_lifecycle.mjs` after final integration.
- [ ] Report actual browser-smoke results separately from offline results, skipped
  tests, and public-web activation blockers. No commit or activation step follows.

## Plan self-review

The tasks cover configuration, exact permission boundary, safe launch options,
isolation, JS rendering, per-worker state, cleanup, opt-in behavior, and regression
verification. Public egress enforcement, real search-engine behavior, downloads,
and adversarial-page containment remain explicit unimplemented activation gates;
the pilot must not claim them as passing. No engine or production listener changes
are needed for this deliverable.

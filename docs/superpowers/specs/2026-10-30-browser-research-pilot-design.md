# Browser research pilot

**Operational status:** deployed and verified with real parallel research workers.
The deployment amendment below supersedes the original disabled-pilot approach.
The official headless browser preserves sandboxing and owned process containment;
inline page text, guarded public networking, isolated contexts and source URLs are
working. Usage and rollback constraints are recorded at the top of the matching
implementation plan. The historical pilot fixture remains disabled; it is not the
enabled live project policy.

## Approval and scope

The operator selected browser-enabled research and approved a repo-scoped pilot:
write the specification and plan, build and test in isolation, and keep live
access disabled until isolation and network checks pass. Shared-engine deployment
and changes to other repositories are excluded. This approval does not authorize
turning on public browsing or modifying the operator's browser/profile.

**Deployment amendment (operator approved):** make the capability usable, run the
necessary tests, and deploy the browser integration. The cached official Chromium
headless shell starts under the required sandbox and existing Windows Job, unlike
the installed branded browsers. The failure-cleanup smoke passes with that binary.
Upstream MCP writes snapshots to files instead of returning their text; the
synthetic snapshots confirm JavaScript rendering and cookies, but model-facing
page reading needs an adapter. Public-only network filtering is still required.

The approved implementation is therefore a narrow stdio research MCP using the
cached Playwright driver, plus an owned public-only forward proxy. It retains the
three browser tools but returns bounded text/source links directly, never snapshot
files. Fixed extraction code is trusted implementation, not model-supplied code.
The proxy validates all DNS answers and connects a validated numeric public IP;
only HTTP port 80 and TLS CONNECT port 443 are accepted. Redirects/subresources use
that same proxy. Remove Chromium's implicit loopback proxy bypass; disable QUIC
and non-proxied WebRTC, block WebSockets/service workers, non-HTTP(S) URLs,
credentials in URLs and non-GET/HEAD requests, cancel downloads and close popups.

Deploy only reviewed browser assets and this project's explicit MCP launch/tool
grant, with backups and rollback. Do not deploy unrelated concurrent engine work.
Use `--worktree --write` for research workers: it explicitly authorizes browser
state mutation inside a disposable worktree without allowing writes to the main
checkout. Preserve original project accounting identity and the 30k cap. Finish
with real parallel delegated studies returning source URLs, not just skipped tests.

## Goal

Establish the smallest useful path for delegated workers to search public pages,
read JavaScript-rendered evidence, and cite sources independently. Reuse the
existing policy-controlled stdio MCP client rather than add native engine tools,
broaden shell permissions, or build a browser service.

## Approach

Use a pinned, already available Playwright MCP package, launched directly with
absolute Node and package entrypoint paths. The locally cached `0.0.83` package
supports explicit sandboxing, disabling WebMCP, and disabling code generation;
the older configured `0.0.38` package is not the pilot target. This is a version
choice, not a claim of a security audit. No package upgrade or install is needed.

The alternatives are an authenticated ego-browser context (not suitable because
its default inherits operator login state), a custom research adapter (defer until
an essential gap is demonstrated), and new engine tools (unnecessary duplication).

### Disabled template, not live permission changes

Add a standalone `config/browser-research-pilot.json` MCP policy fragment.
It is not merged into `.claude/agent-policy.json`. The fragment changes no shell
permissions, has no authorized MCP launch, and has an explicitly disabled browser server.
Command paths are unmistakable placeholders. Merely enabling the server must fail
the existing exact-command authorization check.

The existing live policy, engine, Kilo configuration, shared installation, and 30k
read/result cap stay unchanged. No general-purpose launcher or activation switch
is introduced. The template explains that changing a boolean is not clearance to
browse publicly.

### Initial tool surface

Allow only `browser_navigate`, `browser_navigate_back`, and `browser_close`.
Navigation results already contain page snapshots. Do not
allow wildcard tool names, clicks, typing, form submission, arbitrary evaluation,
code execution, uploads, download tools, network bodies, or screenshots.

Do not expose `browser_snapshot`: its optional output filename permits writing
retrieved content to the workspace; ordinary navigation snapshots suffice for the
first pilot. Inspect actual pinned schemas and reject unexpected write-capable
arguments. If a proposed tool's schema or behavior is incompatible, omit it and
report the limitation rather than widen the boundary.

Explicitly classify all three tools as mutating browser state. The real package
also marks navigation destructive; the existing harness correctly refuses it
without mutation approval. Grant `allow_mutation=True` only to the temporary
synthetic-test managers. Do not weaken classification or grant a live research
agent broad repository write access merely to enable navigation.

Search is ordinary navigation to an encoded public search URL; result links are
read from rendered snapshots and opened by URL. Search-engine availability,
CAPTCHAs, and usefulness of search without clicks are unverified by local fixtures.

### Worker and process isolation

Each worker starts a distinct stdio MCP process with an isolated in-memory browser
profile. Require headless mode, explicit Chromium sandboxing, blocked service
workers, disabled WebMCP/code generation, omitted image responses, finite action
and navigation deadlines, and its own temporary output directory. Never attach
via extension, CDP, or another browser endpoint; never supply storage state,
secrets, a persistent user-data directory, extra capabilities, or `--no-sandbox`.

Use the existing MCP process ownership and close behavior. A fixture failure must
still close both managers, stop the fixture server, and release temporary files.
No public listener, background service, scheduler, or production process is added.

## Safety and activation gate

An isolated profile is not a network sandbox. Origin flags are defense in depth,
not a sufficient egress boundary; the pinned server's help explicitly warns that
origin restrictions do not cover redirects. Navigation itself can produce remote
side effects or browser-created downloads despite the narrow tool surface.

Public browsing remains disabled until a separately reviewed, independently
enforced network boundary has been demonstrated to block loopback, private,
link-local/metadata destinations, IPv6 equivalents, DNS rebinding, redirects,
subresources, and WebSockets. HTTP(S)-only navigation, bounded download behavior,
and cancellation/timeout cleanup must also be demonstrated. No suitable containment
has been established in this pilot. No firewall, proxy, container platform, or
network bypass is silently installed or configured.

The synthetic browser smoke test deliberately uses an operator-owned loopback
fixture. This test-only authorization must not be described as proving public-only
egress or generalized browser security.

## Data handling

Fixtures contain synthetic text only. Tests inject temporary roots, home, outputs,
and browser state. No real user accounting, prompts, research content, credentials,
cookies, or source pages are written to repository files or AgentControl state,
APIs, accounting, or reports. Test output is assertions/metadata, not page bodies.
Avoid pytest assertion locals or response dumps containing page content.

Future research treats pages as untrusted evidence, not instructions. Queries must
not contain secrets or full repository content. Website requests and model context
leave the machine. Future research answers distinguish source URLs, retrieval time,
quoted evidence, and inference; the pilot does not add report storage.

Launch failures must report only static error categories or allowlisted numeric
metadata, not truncated arbitrary tool errors or browser command lines.

## Acceptance

Default, offline tests must verify the disabled fragment, absent launch
authorization, unchanged shell permissions/cap, exact tool allowlist, safe launch options, and
fail-closed activation. They must use synthetic temporary paths and existing
`harness_mcp` validation/client behavior rather than reimplement the client.
The fragment is not a complete delegate policy: `load_policy()` requires a
nonempty command list. Test composition only in a temporary full-policy fixture,
comparing the same fixture's shell permissions before and after adding the fragment.
Do not introduce a dummy production command grant or change engine validation.

An explicitly opted-in real-browser test takes injected absolute Node and MCP
entrypoint paths, verifies package name/version, and uses only synthetic local
pages. Missing opt-in skips the test; an opted-in missing/incorrect dependency or
failed browser check is a failure, not a silent skip. No package/browser download
is permitted. Two managers operate concurrently against the fixture and must show:

- distinct owned process IDs and output directories;
- JavaScript-rendered text available through allowed tools;
- independent cookies and tabs/page state;
- the narrow callable surface, including refused evaluation and file-output tools;
- finite errors/timeouts and cleanup after success/failure.

Run targeted checks, existing MCP/capability regressions, the full Python suite,
and `node --test tests/test_kilo_lifecycle.mjs`. Local smoke results must not be
reported as a completed public-web study. The final result must state which
activation conditions remain unverified.

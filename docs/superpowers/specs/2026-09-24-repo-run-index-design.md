# Repo run index and cross-user run discovery

Date: 2026-09-24

## Problem and evidence

Delegated runs launched by OpenAI Codex never appear in AgentControl, not even
as a project awaiting approval. The engine appends a start record and an end
record for every run to the machine-wide run index at
`C:\Users\operator\.claude\agent-runs-index.jsonl`. That index is AgentControl's
only discovery channel: sync reads it to learn which runs and which repos exist.

Codex's Windows sandbox (`[windows] sandbox = "elevated"` in
`~/.codex/config.toml`) runs commands as a separate local user,
`CodexSandboxOnline` or `CodexSandboxOffline`, which may write only inside the
workspace (the repo). Appending to the machine-wide index therefore fails with
`PermissionError`. The engine swallows that error, printing only a stderr
warning (`WARNING: run index append failed: PermissionError`), so AgentControl
never learns the run or its repo exists.

Transcripts still land in `<repo>\.claude\agent-runs\`, because that folder is
inside the workspace. On disk right now, 11 Khattati runs (2026-09-24, 17:13 to
17:33) have transcripts but no index record.

Kilo Code and Cline run commands as the operator's own user today and did write
the machine-wide index, so their runs appeared. But any launcher that sandboxes
the process or redirects the home directory breaks in the same way. All three
launch through PowerShell on Windows, where `-t "$(cat brief.md)"` joins the
lines of a brief with spaces, mangling multi-line tasks.

## Design

### Engine: dual index write

`engine/delegate.py` keeps `HARNESS_VERSION` at 6 and `INDEX_SCHEMA` at 2. Every
start and end record is now appended to BOTH the machine-wide index AND a *repo
run index*: a file named `agent-runs-index.jsonl` in the same folder as the
run's transcript, normally `<repo>\.claude\agent-runs\agent-runs-index.jsonl`.

The record is byte-identical in both files: one JSON object per line, written
with `sort_keys` and `separators=(",", ":")`, schema 2. Each append is
independent and never fatal, so a failure to write one file does not stop the
other or the run. If only the repo copy succeeds, the engine prints
`note: run registered only in the repo run index <path>; the machine-wide index
was not writable`. When `CLAUDE_AGENT_RUN_INDEX` points at the same file as the
repo copy, one line is written, not two.

### Engine: --task-file

A new `-f/--task-file PATH` option is an alternative to `-t/--task`; the two are
mutually exclusive. `-` reads standard input. The file is decoded by its byte
order mark: UTF-8, UTF-8 with BOM, or UTF-16 (the default of PowerShell 5.1's
`>` redirection). Invalid bytes are an error. CRLF and lone CR are turned into
LF. An empty or whitespace-only file is an error with exit code 2.

### Sync: repo index discovery

`src/agent_control/sync.py` and `discovery.py` read the machine index plus every
repo run index found at:

- known project roots;
- every immediate subfolder of each project root's parent, so siblings of known
  repos are found with no configuration;
- every immediate subfolder of each folder listed in
  `C:\Users\operator\.claude\agent-control\scan_roots.json`, a JSON array of
  absolute folder paths. It is optional and re-read on every sync, so no restart
  is needed. Malformed input yields the diagnostic `invalid_scan_roots`; a bad
  entry yields `invalid_scan_root`;
- the `.claude\worktrees\*` and `.claude\workspaces\*` subfolders of any of the
  above.

Each file has its own checkpoint in `sync_state.json` under `repo_checkpoints`.
A changed file identity in ANY source triggers a full rescan of all sources. An
unknown repo found this way becomes a pending ("discovered") project awaiting
approval, like any other.

### Duplicates and provenance

Machine-index copies win on conflict. Provenance stays `machine_index`, because
the repo file is a copy of the same engine register. Late duplicates are
dropped: an end for a run the ledger already ended, and a start for a run the
ledger already ended.

### Kill

When `psutil` raises `AccessDenied` (the engine runs as a sandbox user), the
kill is refused with HTTP 409 `access_denied` and the message: `access denied:
the engine process belongs to another Windows user or runs elevated (for
example inside the Codex sandbox); stop it from the tool that launched it`.
Steering still works for such runs, because the `.steer` file is inside the
workspace. Watching works too.

### Backfill

`agent-control backfill-index --repo PATH [--apply] [--min-age-minutes N]`
(default 15) rebuilds start and end records, marked `"backfilled": true`, from
transcripts that have no index record. It is dry-run unless `--apply` is given.
It uses the transcript header for label, model, effort, tools and writes; the
filename for run_id and start time; the file mtime for the end time; the
`## Final report` marker and footer for status and counts. `engine_pid` and
`harness_version` are null. Status is `complete` with a final report, `failed`
with only a footer, and `interrupted` with neither. It skips non-engine files
(fanout), runs already indexed, and recently modified transcripts. It appends to
the repo run index only, and the operator runs it. The existing
`agent-control migrate` is unsuitable: it invents ids and timestamps.

## What is deliberately not done

- No discovery from the process list, because a sandbox user's command line is
  unreadable.
- No automatic import of pre-index legacy runs; the 232 Projecta runs stay with
  the gated `migrate` command.
- No version bumps: `HARNESS_VERSION` stays 6 and `INDEX_SCHEMA` stays 2.

## Verification

- `tests/test_engine_repo_index.py`: the engine appends each start and end
  record to both the machine-wide index and the repo run index, byte-identical;
  a failure to write one does not stop the other; the note is printed when only
  the repo copy succeeds; one line is written when `CLAUDE_AGENT_RUN_INDEX`
  points at the repo copy.
- `tests/test_engine_task_file.py`: `-f/--task-file` reads a file or stdin,
  decodes UTF-8, UTF-8 with BOM and UTF-16, normalises CRLF and lone CR to LF,
  is mutually exclusive with `-t`, and rejects an empty or whitespace-only file
  with exit code 2.
- `tests/test_discovery.py`: the discovery rules enumerate known roots, sibling
  subfolders, `scan_roots.json` subfolders and `worktrees`/`workspaces`
  subfolders, and report `invalid_scan_roots` and `invalid_scan_root`.
- `tests/test_sync_repo_index.py`: sync reads repo run indexes, keeps a
  checkpoint per file, rescans all sources when any file identity changes, and
  turns an unknown repo into a pending discovered project.
- `tests/test_sync_duplicates.py`: machine-index copies win, provenance stays
  `machine_index`, and late duplicate start and end records are dropped.
- `tests/test_backfill.py`: backfill is dry-run without `--apply`, rebuilds
  start and end records marked `backfilled`, classifies status as complete,
  failed or interrupted, skips fanout files, indexed runs and recent
  transcripts, and appends to the repo run index only.
- `tests/test_process_control.py`: kill refuses with HTTP 409 `access_denied`
  and the sandbox message when `psutil` raises `AccessDenied`; steering and
  watching still work.

Every test injects temporary paths through `tmp_path` and uses synthetic text
only; none writes under `C:\Users\operator\.claude` or into a real repo.

## Rollout

All steps are operator-gated:

1. Deploy the engine with `python scripts/deploy_engine.py --apply`, then run
   each repo's `tools/agent-harness/selftest_policy.py`.
2. Restart `agent-control serve`.
3. Run `agent-control backfill-index` for `D:\repos\Khattati` and
   `D:\repos\Projectb\runs\knowledge-recovery` (dry-run first, then `--apply`).

Runs start writing the repo run index only after the engine is deployed.


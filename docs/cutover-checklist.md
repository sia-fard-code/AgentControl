# AgentControl Cutover Checklist

This checklist separates safe, automated steps from live actions that require
explicit operator approval. Do not perform any live action below without that
approval. Every automated gate runs against temporary fixtures or read-only
inspection and never mutates live state.

## Automated pre-cutover gates (safe)

Run from `D:\repos\AgentControl`:

```bat
git status --short
python -m pytest -v
node --test tests/js/dashboard.test.mjs
python scripts/deploy_engine.py
python scripts/migrate_existing.py --dry-run --projecta "D:\repos\Combinations\Projecta" --projectb "D:\repos\Projectb" --report "scratch\migration.json"
certutil -hashfile "scratch\migration.json" SHA256
git diff --check
```

Expected: all tests pass, the deploy script stays in dry-run, the dry-run
migration is applicable and conflict-free, and the working tree is clean.

## Live gates (each requires explicit operator approval)

1. **Dry-run migration** review of the produced report before any apply.
2. **Input backups**: back up the installed engine, the machine index, any
   existing runtime directory, the reviewed report, and the Projecta and Projectb
   ledgers, registries, and pricing files to a timestamped user backup.
3. **Approved migration apply** using the exact report hash, followed by a
   second dry-run that must plan zero further revisions.
4. **Projecta approval** of its project UUID, then a sync, then historical
   parity comparison of logical counts and recoverable token totals.
5. **Projectb approval** of its project UUID, then the same parity comparison.
6. **Direct run**: one harmless direct delegated run through the shared engine.
7. **Wrapper run**: one harmless existing-wrapper delegated run.
   Both must show schema 2, canonical root versus worktree cwd, process
   identity, and exactly one usage journal row per successful response. Two
   syncs must append no duplicate revision.
8. **Pending temporary root**: one harmless temporary-root delegation while the
   service is stopped; after restart it must appear as a pending diagnostic and
   must not change normal totals until approved.
9. **Replay/restart idempotence**: restart and run two syncs; the second must
   append zero revisions.
10. **Full test pass**: full AgentControl tests plus the affected Projecta tests
    and Projectb self-tests, recording external `git status --short` before and after.
11. **One production listener** on `127.0.0.1:8787`:

    ```bat
    agent-control serve --port 8787
    netstat -ano | findstr "127.0.0.1:8787"
    ```

    Require exactly one intended LISTENING PID.
12. **Sensitive-content scan**: scan central JSON/JSONL key names only for
    prompt, messages, assistant, tool_result, credential, authorization,
    response_body, and content; fail without printing values.
13. **pricing_history.jsonl** must remain absent unless a genuine, separately
    confirmed pricing edit created it:

    ```bat
    if exist "C:\Users\operator\.claude\agent-control\pricing_history.jsonl" (exit /b 1) else (echo pricing history correctly absent)
    ```

14. **Optional scheduled task** (only after documented manual stability and a
    separate approval): create one current-user task for
    `python -m agent_control.cli serve --port 8787` with this checkout as the
    working directory. Check for no listener/task conflict before and exactly one
    listener after.

## Rollback

- **Migration rollback**: stop the service, preserve the failed runtime for
  diagnosis, and restore the backed-up runtime and baseline. Source artifacts
  were read-only and need no rollback.
- **Engine deployment rollback**: stop the listener and run

  ```bat
  python scripts/deploy_engine.py --rollback "<exact backup path printed by apply>"
  ```

## Deferred cleanup

Repo-local dashboard removal is deferred: repo-local dashboard, ledger, pricing,
and duplicated harness code are removed only in a later, separately reviewed
cleanup task, after migration and parity verification are complete.

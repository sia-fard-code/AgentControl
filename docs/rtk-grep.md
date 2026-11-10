# rtk and grep: the "Failed to resolve 'rg' via PATH" message

Applies to every agent on this PC that runs shell commands through rtk: Claude Code (the `rtk hook claude` PreToolUse
hook), Gemini/Antigravity (its "prefix every shell command with rtk" rule), Codex (`@RTK.md`), and any portal that
types `rtk grep`. Harness agents (`delegate.py`, `fanout.py`, `ask-model.py`) run commands without a shell and are not
affected.

## Symptom

```
rtk: Failed to resolve 'rg' via PATH, falling back to direct exec: Binary 'rg' not found on PATH
```

## Cause

1. rtk 0.40.0 (`~/.gemini/antigravity/bin/rtk`) rewrote every shell `grep` and `rg` command into `rtk grep`.
2. `rtk grep` runs ripgrep. ripgrep is not installed on PATH. In Claude Code's shell, `rg` is only a shell function
   that calls `claude.exe`; rtk is a separate program and cannot see shell functions. So rtk printed the message and
   ran the system grep instead.
3. The message itself is harmless. The damage is what `rtk grep` does to the output. Seen on 2026-10-30:
   - `grep -h` printed rtk's help screen, because `-h` means help in rtk.
   - `grep -c` printed `0 matches for '...'` instead of a count.
   - A pattern with `\[` failed with "Unmatched [".
   - Lines were truncated and grouped by file. Results were capped at 200 in total and 25 per file
     (`[limits]` in the rtk config). Pipelines did not notice the cap.

   Counts, hit lists and "no match" results from `rtk grep` can therefore be silently wrong.

## Fix applied (2026-10-30, operator approved)

`C:\Users\operator\AppData\Roaming\rtk\config.toml`:

```toml
[hooks]
exclude_commands = ["grep", "rg"]
```

The hook no longer rewrites `grep` or `rg`; they behave as normal grep and ripgrep. Verified the same day: `grep -c`,
`grep -h`, `grep -o` and `\[` patterns give normal output with no rtk message. rtk still compresses every other
command (git, ls, tree, test and build output), so it keeps saving tokens. To undo, set the list back to `[]`.

The hook rewrites a command line before it runs. A command that changes this config therefore still runs under the
old setting; test in a separate command.

## Correct use, for every agent

- Search file contents with the tool's own search first (Claude Code: the Grep tool). It does not go through rtk and
  already returns compact results.
- In the shell, write plain `grep` or `rg`. Do not type `rtk grep`: an explicit `rtk grep` still runs rtk's compact
  grep, with the caps and flag differences above.
- When you need the exact output of any other command that rtk rewrites, run it as `rtk proxy <command>`.
- Do not trust a count, a hit list or a "no match" result that came from `rtk grep`.

#!/usr/bin/env python3
"""Attack one problem with several delegated agents at once, then merge the results.

Each agent is an independent delegate.py run in its own process with its own
transcript, so they explore in parallel and cannot contaminate each other's
reasoning. Diversity is the point: a fleet of identical agents mostly agrees with
itself, so give each one a distinct lens (or a distinct model) and let the
disagreements surface what a single pass would miss.

  fanout.py -t "Why does the Live scan kill the renderer?" \
      --lens "memory and allocation pressure" \
      --lens "IndexedDB transaction lifetime and quota" \
      --lens "main-thread blocking and watchdog timeouts"

Heterogeneous fleets -- different models on the same question:

  fanout.py -t "Should main.ts be split, and how?" \
      --agent "gpt-5.6-sol|xhigh|argue FOR splitting, with a concrete seam list" \
      --agent "gpt-5.6-terra|xhigh|argue AGAINST splitting; what breaks?"

With --synthesize, a final model reads every report and produces one answer,
explicitly noting where the agents disagreed. Disagreement is signal; a synthesis
that hides it is worse than the raw reports. The synthesis runs as a
`delegate.py --oneshot` call labelled "fanout-synthesis", so it is registered as
a run like the agents are.

Live progress: each agent's transcript path is printed as it starts. Tail any of
them, or steer one mid-flight via its .steer file (see delegate.py).
"""
import argparse
import concurrent.futures
import datetime
import os
import runpy
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from types import SimpleNamespace

DELEGATE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "delegate.py")
DEFAULT_MODEL = "gpt-5.6-sol"
SYNTH_MODEL = "gpt-5.6-terra"
INCOMPLETE_EXIT = 3  # engine: stopped at a turn/repair limit; stdout may be partial
SYNTH_EFFORT = "high"
SYNTH_TIMEOUT = 1800  # seconds; the engine has its own gateway timeouts inside this
# The fanout's own label. Lens runs are "agent{idx}-{slug}" and their transcripts
# are "{stamp}-{label}.md"; the combined output follows the same scheme as
# "{stamp}-fanout.md", and the synthesis run is labelled "fanout-synthesis".
FANOUT_LABEL = "fanout"


LIVE = {}          # idx -> Popen, so a deadline can terminate stragglers
LIVE_LOCK = threading.Lock()
MONITORED = {}     # idx -> (lens, tpath), for the stall monitor to inspect
JOURNALS = {}      # idx -> content-free owner evidence, never child stdout/stderr


def stop_agent(idx, proc, reason):
    journal = JOURNALS.get(idx)
    if journal is not None:
        journal.emit("stop_requested", reason=reason, target_pid=getattr(proc, "pid", None))
    proc.kill()


def diagnostic_journal(path, **identity):
    try:
        module = runpy.run_path(os.path.join(os.path.dirname(__file__), "harness_diagnostics.py"))
        return module["Journal"](path, "fanout", **identity), module["exception_fields"]
    except Exception:
        try:
            print("WARNING: lifecycle diagnostics unavailable", file=sys.stderr)
        except Exception:
            pass
        return SimpleNamespace(emit=lambda *a, **k: False, identity={}), lambda exc: {}


def stall_monitor(stop_event, stall_warn_secs):
    """Warn (never kill) when a running agent's transcript goes quiet.

    A working-but-slow agent and a wedged one look identical from outside; the
    only cheap signal is whether its transcript is still being written. Every
    60s we compare each running agent's transcript mtime against a threshold and
    warn if it has been silent too long. Warnings repeat every threshold so a
    persistently stalled agent keeps surfacing, but no more often than that so a
    truly dead agent does not spam. Termination stays exclusively with the
    per-agent timeout / deadline in main(); this thread only reports.
    """
    last_warned = {}   # idx -> monotonic-ish time we last warned about it
    while not stop_event.wait(60):
        now = time.time()
        with LIVE_LOCK:
            snapshot = list(MONITORED.items())
        for idx, (lens, tpath) in snapshot:
            try:
                mtime = os.path.getmtime(tpath)
            except OSError:
                continue   # transcript not created yet; nothing to judge
            silent = now - mtime
            if silent < stall_warn_secs:
                continue
            if now - last_warned.get(idx, 0.0) < stall_warn_secs:
                continue   # at most once per threshold interval per agent
            last_warned[idx] = now
            print(f"  [{idx}] STALL WARNING: transcript silent for "
                  f"{silent / 60:.1f} min (threshold {stall_warn_secs / 60:.0f} "
                  f"min)\n      lens: {lens}\n      still running, NOT killed; "
                  f"log: {tpath}", file=sys.stderr, flush=True)


def capability_argv(skills=(), write=False, text_only=False):
    """Forwarded child flags: skills by name; --write only when explicitly asked."""
    argv = []
    for name in skills or ():
        argv += ["--skill", name]
    if write:
        argv += ["--write"]
    if text_only:
        argv += ["--text-only"]
    return argv


CONTEXT_FLAGS = (("context_policy", "--context-policy"),
                 ("context_window_tokens", "--context-window-tokens"),
                 ("context_reply_tokens", "--context-reply-tokens"),
                 ("context_safety_tokens", "--context-safety-tokens"),
                 ("compact_fraction", "--compact-fraction"))


def context_argv(context=None, oneshot=False):
    """Explicit context options only; one-shot synthesis never gets compaction flags."""
    context = context or {}
    argv = []
    for key, flag in CONTEXT_FLAGS:
        if context.get(key) is not None and not (oneshot and key == "compact_fraction"):
            argv += [flag, str(context[key])]
    auto = context.get("auto_compact")
    if auto is not None and not oneshot:  # explicit False is forwarded, never dropped
        argv += ["--auto-compact" if auto else "--no-auto-compact"]
    return argv


def run_agent(idx, model, effort, lens, task, root, max_iters, stamp, agent_timeout,
              skills=(), write=False, soft_iters=None, text_only=False, context=None):
    """One agent, in its own process.

    Stragglers are the known failure mode of a fleet this size: most agents finish
    quickly and one wanders. Each gets its own timeout, and main() can terminate
    whatever is still running when the overall deadline passes, so a single slow
    agent never holds the other nine hostage.
    """
    slug = "".join(c if c.isalnum() else "-" for c in lens.lower())[:28]
    label = f"agent{idx}-{slug}"
    tpath = os.path.join(root, ".claude", "agent-runs", f"{stamp}-{label}.md")
    full = (f"{task}\n\nYour specific angle on this problem: {lens}\n\n"
            "Investigate from that angle specifically. Other agents are covering "
            "other angles, so go deep on yours rather than broad. Report what you "
            "found with file:line evidence, and say plainly what you could not verify.")
    argv = [sys.executable, DELEGATE, "-t", full, "-m", model, "-e", effort,
            "-C", root, "--max-iters", str(max_iters),
            "--transcript", tpath, "--label", label,
            "--patch", os.path.join(root, f"{label}.patch")]
    if soft_iters is not None:
        argv += ["--soft-iters", str(soft_iters)]
    argv += capability_argv(skills, write, text_only) + context_argv(context)
    try:
        os.makedirs(os.path.dirname(tpath), exist_ok=True)
    except OSError:
        pass  # leave the existing engine launch to report its own startup failure
    launch_id = str(uuid.uuid4())
    argv += ["--launch-id", launch_id, "--launch-receipt", tpath + ".launch.jsonl"]
    journal, exception_fields = diagnostic_journal(tpath + ".fanout.diagnostics.jsonl",
                                                  run_id=os.path.basename(tpath)[:-3], launch_id=launch_id)
    journal.emit("observer_started", phase="startup")
    print(f"  [{idx}] start  {model} e={effort}\n"
          f"      lens: {lens}\n"
          f"      log:  {tpath}", file=sys.stderr, flush=True)
    t0 = time.time()
    try:
        proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    except BaseException as exc:
        journal.emit("exception", reason="unhandled_exception", **exception_fields(exc))
        raise
    journal.emit("child_start", child_pid=getattr(proc, "pid", None), _child=proc)
    with LIVE_LOCK:
        LIVE[idx] = proc
        MONITORED[idx] = (lens, tpath)
        JOURNALS[idx] = journal
    try:
        out, err = proc.communicate(timeout=agent_timeout)
    except subprocess.TimeoutExpired:
        stop_agent(idx, proc, "agent_timeout")
        out, err = proc.communicate()
        print(f"  [{idx}] TIMEOUT after {agent_timeout}s -- partial log at {tpath}",
              file=sys.stderr, flush=True)
        return idx, model, lens, None, f"timed out after {agent_timeout}s", tpath, time.time() - t0
    finally:
        journal.emit("child_exit" if proc.returncode is not None else "child_state_unknown",
                     child_pid=getattr(proc, "pid", None), exit_code=proc.returncode)
        with LIVE_LOCK:
            LIVE.pop(idx, None)
            MONITORED.pop(idx, None)
            JOURNALS.pop(idx, None)
    dt = time.time() - t0
    ok = proc.returncode == 0 and (out or "").strip()
    incomplete = proc.returncode == INCOMPLETE_EXIT
    print(f"  [{idx}] {'done ' if ok else 'INCOMPLETE' if incomplete else 'FAILED'} "
          f"({dt:.0f}s) {lens[:45]}", file=sys.stderr, flush=True)
    if incomplete:
        # Partial stdout is evidence, never success: err stays set so callers exclude it.
        return (idx, model, lens, (out or "").strip() or None,
                "incomplete: " + why_failed(err, proc.returncode), tpath, dt)
    if not ok:
        return idx, model, lens, None, why_failed(err, proc.returncode), tpath, dt
    return idx, model, lens, out.strip(), None, tpath, dt


def why_failed(err, code):
    """Pull the actual reason out of stderr.

    delegate.py streams its per-step progress to stderr, so echoing the tail
    verbatim buries the cause under a wall of tool-call lines.
    """
    lines = [l.strip() for l in (err or "").splitlines() if l.strip()]
    for l in reversed(lines):
        if l.lower().startswith(("error:", "warning:", "traceback")) or "Error" in l:
            return f"exit={code}: {l[:300]}"
    tail = [l for l in lines if not l.lstrip().startswith("[")]
    return f"exit={code}: {(tail[-1] if tail else 'no output')[:300]}"


def synthesize(task, results, model, root, label=FANOUT_LABEL, skills=(), text_only=False,
               context=None):
    """Merge the reports with one engine `--oneshot` call.

    Going through the engine (instead of a direct gateway request) registers the
    synthesis as a run, so it appears in the dashboard and its tokens are counted.
    Returns the answer on success, otherwise "(synthesis failed: <reason>)".
    """
    joined = "\n\n".join(
        f"===== AGENT {r[0]} ({r[1]}) -- lens: {r[2]} =====\n{r[3]}"
        for r in results if r[3] and (len(r) < 5 or r[4] is None))
    prompt = (f"Several independent agents investigated this problem:\n\n{task}\n\n"
              "Their reports follow. Produce ONE synthesis that: states the "
              "conclusions they agree on; calls out explicitly where they DISAGREE "
              "and which reading the evidence better supports; and lists what none "
              "of them established. Do not smooth over disagreement -- it is the "
              f"most useful signal here.\n\n{joined}")
    prompt_file = None
    launch_id = str(uuid.uuid4())
    runs = os.path.join(root, ".claude", "agent-runs")
    receipt = os.path.join(runs, launch_id + ".launch.jsonl")
    journal, exception_fields = diagnostic_journal(receipt + ".fanout.diagnostics.jsonl", launch_id=launch_id)
    try:
        os.makedirs(runs, exist_ok=True)
        journal.emit("observer_started", phase="startup")
        fd, prompt_file = tempfile.mkstemp(prefix="fanout-synthesis-", suffix=".txt")
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as fh:
            fh.write(prompt)
        argv = [sys.executable, "-u", DELEGATE, "--oneshot", "-C", root,
                "-m", model, "-e", SYNTH_EFFORT,
                "--label", f"{label}-synthesis"] + capability_argv(skills, text_only=text_only) + \
            context_argv(context, oneshot=True) + [
                "--launch-id", launch_id, "--launch-receipt", receipt, "-f", prompt_file]
        with subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              text=True, encoding="utf-8", errors="replace") as proc:
            journal.emit("child_start", child_pid=getattr(proc, "pid", None), _child=proc)
            try:
                out, err = proc.communicate(timeout=SYNTH_TIMEOUT)
            except BaseException as exc:
                journal.emit("stop_requested", target_pid=getattr(proc, "pid", None),
                             reason="synthesis_timeout" if isinstance(exc, subprocess.TimeoutExpired)
                             else "wrapper_exception")
                proc.kill()
                proc.communicate()
                journal.emit("child_exit", child_pid=getattr(proc, "pid", None), exit_code=proc.returncode)
                raise
            journal.emit("child_exit", child_pid=getattr(proc, "pid", None), exit_code=proc.returncode)
    except Exception as e:
        journal.emit("exception", reason="synthesis_timeout" if isinstance(e, subprocess.TimeoutExpired)
                     else "unhandled_exception", **exception_fields(e))
        return f"(synthesis failed: {type(e).__name__}: {e})"
    finally:
        try:
            if prompt_file is not None:
                os.remove(prompt_file)
        except OSError:
            pass
    if proc.returncode == 0:
        return out
    lines = [l.strip() for l in (err or "").splitlines() if l.strip()]
    reason = lines[-1][:300] if lines else f"exit {proc.returncode}, no stderr"
    return f"(synthesis failed: {reason})"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("-t", "--task", required=True)
    ap.add_argument("--lens", action="append", default=[],
                    help="an angle; uses the default model. Repeatable.")
    ap.add_argument("--agent", action="append", default=[],
                    help="'MODEL|EFFORT|LENS'. Repeatable. For mixed fleets.")
    ap.add_argument("-m", "--model", default=DEFAULT_MODEL)
    ap.add_argument("-e", "--effort", default="high")
    ap.add_argument("-C", "--root", default=".")
    ap.add_argument("--max-iters", type=int, default=25)
    ap.add_argument("--max-parallel", type=int, default=8)
    ap.add_argument("--agent-timeout", type=int, default=900,
                    help="seconds before an individual agent is killed (default 900)")
    ap.add_argument("--deadline", type=int, default=0,
                    help="seconds before the whole fanout reports with whatever "
                         "finished, terminating stragglers. 0 = wait for all.")
    ap.add_argument("--stall-warn-mins", type=int, default=15,
                    help="warn (never kill) when a running agent's transcript "
                         "has been silent this many minutes (default 15)")
    ap.add_argument("--synthesize", action="store_true")
    ap.add_argument("--text-only", action="store_true",
                    help="refuse images in every agent and synthesis")
    ap.add_argument("--skill", dest="skills", action="append", default=[],
                    metavar="NAME", help="preload a skill in every agent and the "
                                         "synthesis (repeatable)")
    ap.add_argument("--write", action="store_true",
                    help="forward --write to every agent (real writes in -C; also "
                         "allows MCP mutation). Never implied.")
    ap.add_argument("--soft-iters", type=int, default=None,
                    help="forward an earlier work checkpoint to every agent; it "
                         "preauthorizes one continuation inside --max-iters")
    for _, flag in CONTEXT_FLAGS:
        ap.add_argument(flag, default=None,
                        help="forwarded verbatim when given; the synthesis one-shot gets "
                             "policy/window/reply/safety only, never compaction flags")
    ap.add_argument("--auto-compact", default=None, action=argparse.BooleanOptionalAction,
                    help="forward to every agent only; the one-shot synthesis never compacts")
    args = ap.parse_args()
    context = {key: getattr(args, key) for key, _ in CONTEXT_FLAGS}
    context["auto_compact"] = args.auto_compact
    if args.max_iters < 1:
        ap.error("--max-iters must be a positive integer")
    if args.soft_iters is not None:
        # Engine reserves: report 1, closeout min(2, max(0, max_iters - 2)).
        work = args.max_iters - 1 - min(2, max(0, args.max_iters - 2))
        if not 1 <= args.soft_iters <= work:
            ap.error(f"--soft-iters must be 1..{work} for --max-iters {args.max_iters}")

    specs = [(args.model, args.effort, l) for l in args.lens]
    for a in args.agent:
        parts = a.split("|", 2)
        if len(parts) != 3:
            sys.exit(f"error: --agent needs 'MODEL|EFFORT|LENS', got: {a}")
        specs.append((parts[0].strip(), parts[1].strip(), parts[2].strip()))
    if not specs:
        sys.exit("error: give at least one --lens or --agent")

    root = os.path.realpath(args.root)
    stamp = f"{datetime.datetime.now():%Y%m%d-%H%M%S}"
    print(f"fanout: {len(specs)} agents, up to {args.max_parallel} at once\n",
          file=sys.stderr, flush=True)

    results, unfinished = [], []
    stop_monitor = threading.Event()
    monitor = threading.Thread(
        target=stall_monitor,
        args=(stop_monitor, max(1, args.stall_warn_mins) * 60),
        daemon=True)
    monitor.start()
    ex = concurrent.futures.ThreadPoolExecutor(max_workers=args.max_parallel)
    futs = {ex.submit(run_agent, i, m, e, l, args.task, root, args.max_iters,
                      stamp, args.agent_timeout, skills=args.skills, write=args.write,
                      soft_iters=args.soft_iters, text_only=args.text_only,
                      context=context): i
            for i, (m, e, l) in enumerate(specs, 1)}
    try:
        for f in concurrent.futures.as_completed(futs, timeout=args.deadline or None):
            results.append(f.result())
    except concurrent.futures.TimeoutError:
        unfinished = [futs[f] for f in futs if not f.done()]
        print(f"\n  deadline of {args.deadline}s reached; terminating "
              f"{len(unfinished)} straggler(s): {unfinished}", file=sys.stderr, flush=True)
        with LIVE_LOCK:
            for idx, proc in list(LIVE.items()):
                stop_agent(idx, proc, "fanout_deadline")
        for f in futs:
            if f.done():
                try:
                    r = f.result()
                    if r not in results:
                        results.append(r)
                except Exception:
                    pass
    ex.shutdown(wait=False)
    stop_monitor.set()
    results.sort(key=lambda r: r[0])

    # Persist the full combined output ourselves. If a caller pipes stdout
    # through `tail`/`head`, the reports are otherwise destroyed and have to be
    # rebuilt from transcripts; this file is the complete copy of record.
    combined = []

    def emit(text=""):
        # Print to stdout exactly as before AND capture for the persisted file,
        # so the two never drift.
        print(text)
        combined.append(text)

    ok = [r for r in results if r[3] and r[4] is None]
    incomplete = [r for r in results if r[4] and r[4].startswith("incomplete")]
    for idx, model, lens, rep, err, tpath, dt in results:
        emit(f"\n{'='*72}\nAGENT {idx} ({model}, {dt:.0f}s) -- {lens}\n{'='*72}\n")
        if err is None:
            emit(rep)
        elif err.startswith("incomplete"):  # partial evidence: kept, never counted as a report
            emit(f"[INCOMPLETE -- not a finished report: {err}]\n{rep or ''}\n"
                 f"[partial transcript: {tpath}]")
        else:
            emit(f"[no report: {err}]\n[partial transcript: {tpath}]")
    for idx in unfinished:
        m, e, l = specs[idx - 1]
        emit(f"\n{'='*72}\nAGENT {idx} ({m}) -- {l}\n{'='*72}\n"
             f"[killed at the {args.deadline}s deadline; partial transcript kept]")

    if args.synthesize and len(ok) > 1:
        emit(f"\n{'='*72}\nSYNTHESIS\n{'='*72}\n")
        emit(synthesize(args.task, results, SYNTH_MODEL, root, FANOUT_LABEL,
                        skills=args.skills, text_only=args.text_only, context=context))
    elif args.synthesize:
        print("\n[synthesis skipped: fewer than two agents produced a report]",
              file=sys.stderr)

    out_path = os.path.join(root, ".claude", "agent-runs", f"{stamp}-{FANOUT_LABEL}.md")
    try:
        os.makedirs(os.path.dirname(out_path), exist_ok=True)
        with open(out_path, "w", encoding="utf-8") as fh:
            fh.write("\n".join(combined) + "\n")
        saved = out_path
    except OSError as e:
        saved = None
        print(f"\nwarning: could not write combined output to {out_path}: {e}",
              file=sys.stderr, flush=True)

    print(f"\n[{len(ok)}/{len(specs)} agents reported]", file=sys.stderr)
    if saved:
        print(f"[full combined output saved to {saved}]", file=sys.stderr, flush=True)
    if len(ok) == len(specs):
        return 0
    if len(ok) + len(incomplete) == len(specs):
        print(f"[{len(incomplete)} agent(s) incomplete; partial evidence kept, not relaunched]",
              file=sys.stderr)
        return INCOMPLETE_EXIT
    return 1


if __name__ == "__main__":
    sys.exit(main())

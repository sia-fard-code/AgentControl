#!/usr/bin/env python3
"""Watch one delegated run and exit when it finishes, stalls or loops.

Legacy source baseline retained from the installed watcher before adding exact
launch identity checks. A matching label alone is not readiness evidence.
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import re
import runpy
import sys
import time
import uuid
from pathlib import Path

RECEIPT_MAX_BYTES = 256 * 1024
RECEIPT_MAX_LINES = 64
# shortcut: only the index tail is scanned, so a start record older than this many bytes is missed; raise it if indexes grow huge.
INDEX_TAIL_BYTES = 8 * 1024 * 1024
STARTUP_STATES = ("rejected", "failed", "unregistered")
# Engine incomplete-terminal reasons; any other value is treated as a plain failure.
STOP_REASONS = frozenset({"max_iters", "repair_limit", "no_action_limit",
                          "mutation_outcome_unknown", "context_budget", "context_admission",
                          "recovery_state", "response_incomplete"})
INCOMPLETE_EXIT = 3
PARTIAL = re.compile(r"^\[PARTIAL - ", re.M)  # the engine's partial-report marker


def process_alive(pid: int) -> bool | None:
    """Read-only liveness query. Never signals the process."""
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return False
    if os.name == "nt":
        # os.kill(pid, 0) on Windows calls TerminateProcess, so never use it here.
        import ctypes
        from ctypes import wintypes
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.OpenProcess.restype = wintypes.HANDLE
        k32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        k32.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
        k32.CloseHandle.argtypes = (wintypes.HANDLE,)
        handle = k32.OpenProcess(0x1000 | 0x00100000, False, pid)  # QUERY_LIMITED | SYNCHRONIZE
        if not handle:
            return None if ctypes.get_last_error() == 5 else False
        try:
            return k32.WaitForSingleObject(handle, 0) == 0x102  # WAIT_TIMEOUT: still running
        finally:
            k32.CloseHandle(handle)
    try:
        os.kill(pid, 0)  # POSIX signal 0 only checks existence
    except ProcessLookupError:
        return False
    except PermissionError:
        return None
    except OSError:
        return False
    return True


def strict_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def invalid_constant(value):
    raise ValueError("non-finite JSON number")


def strict_json(text):
    return json.loads(text, object_pairs_hook=strict_object, parse_constant=invalid_constant)


def read_receipt(path: Path) -> list | None:
    """Complete receipt rows. None if absent; ValueError if malformed or oversized.
    A trailing line without newline is an in-progress append and is ignored."""
    try:
        with open(path, "rb") as fh:
            data = fh.read(RECEIPT_MAX_BYTES + 1)
    except FileNotFoundError:
        return None
    if len(data) > RECEIPT_MAX_BYTES:
        raise ValueError("receipt is larger than the bounded maximum")
    lines = data.decode("utf-8").split("\n")[:-1]
    if len(lines) > RECEIPT_MAX_LINES:
        raise ValueError("receipt has too many lines")
    rows = [strict_json(line) for line in lines]
    if not all(isinstance(row, dict) for row in rows):
        raise ValueError("receipt row is not an object")
    return rows


def validate_receipt(rows: list, launch_id: str, pid: int | None) -> dict:
    """Return the latest row of a consistent history, else raise ValueError."""
    if not rows:
        raise ValueError("receipt has no complete record yet")
    if rows[0].get("state") != "starting" or rows[0].get("run_id") is not None:
        raise ValueError("receipt does not begin with a starting record")
    if len(rows) > 2:
        raise ValueError("receipt has records after its startup outcome")
    first_pid = rows[0].get("engine_pid")
    if type(first_pid) is not int or first_pid <= 0:
        raise ValueError("receipt engine_pid is not a positive integer")
    for row in rows:
        if type(row.get("schema")) is not int or row.get("schema") != 1 or row.get("launch_id") != launch_id:
            raise ValueError("receipt schema or launch_id does not match")
        if row.get("engine_pid") != first_pid or (pid is not None and first_pid != pid):
            raise ValueError("receipt engine_pid does not match")
    last = rows[-1]
    if len(rows) == 2 and last.get("state") not in ("ready",) + STARTUP_STATES:
        raise ValueError("receipt has an unknown startup state")
    if last.get("state") == "ready" and not (
            isinstance(last.get("run_id"), str) and last["run_id"]
            and isinstance(last.get("transcript"), str)
            and isinstance(last.get("index_paths"), list) and last["index_paths"]
             and all(isinstance(p, str) for p in last["index_paths"])):
        raise ValueError("ready record lacks run_id, transcript or index_paths")
    if last.get("state") == "ready" and not all(
            os.path.isabs(path) for path in [last["transcript"], *last["index_paths"]]):
        raise ValueError("ready paths must be absolute")
    return last


def same_path(a, b) -> bool:
    return isinstance(a, str) and isinstance(b, str) and \
        os.path.normcase(os.path.realpath(a)) == os.path.normcase(os.path.realpath(b))


def index_events(ready: dict, pid: int) -> tuple:
    """(matching start, terminal end) for the registered run_id, read from the
    receipt's own index paths. Unrelated or malformed index lines are skipped."""
    start = end = None
    for index in ready["index_paths"]:
        local_start = None
        try:
            with open(index, "rb") as fh:
                fh.seek(max(0, os.fstat(fh.fileno()).st_size - INDEX_TAIL_BYTES))
                data = fh.read(INDEX_TAIL_BYTES)
        except OSError:
            continue
        for line in data.decode("utf-8", errors="replace").split("\n")[:-1]:
            try:
                rec = strict_json(line)
            except ValueError:
                continue
            if not isinstance(rec, dict) or rec.get("run_id") != ready["run_id"]:
                continue
            if rec.get("event") == "start":
                matches = (rec.get("engine_pid") == pid
                           and same_path(rec.get("transcript"), ready["transcript"])
                           and same_path(rec.get("root"), ready.get("root"))
                           and rec.get("label") == ready.get("label"))
                local_start = rec if matches else None
                if matches:
                    if start is not None and start != rec:
                        raise ValueError("conflicting index start records")
                    start = rec
            elif rec.get("event") == "end" and local_start is not None:
                if end is not None and end != rec:
                    raise ValueError("conflicting terminal index records")
                end = rec
    return start, end


FOOTER = re.compile(r"^\[[^\]]*: [\d,]+ tool calls[^\]]*\]$")
CALLING = re.compile(r"^_\[\s*\d+\]\s+calling\s+(\S+)\s+\(attempt\s+(\d+)/(\d+)")
STEP = re.compile(r"^\s*\[\s*(\d+|--)\]\s*(.+)$")
NO_TOOL = re.compile(r"reply carried no tool action")
TRANSPORT = re.compile(r"transport failure|read timeout|HTTP \d{3}")
MCP_EVENT = re.compile(r"^_MCP_CALL (\{.*\})_$")


def mcp_state(text: str) -> dict | None:
    for line in reversed(text.splitlines()):
        match = MCP_EVENT.match(line)
        if not match:
            continue
        try:
            value = json.loads(match.group(1))
        except (ValueError, TypeError):
            return None
        if (isinstance(value, dict) and value.get("phase") in ("start", "wait")
                and isinstance(value.get("server"), str)
                and isinstance(value.get("tool"), str)
                and isinstance(value.get("timeout_s"), (int, float))
                and not isinstance(value.get("timeout_s"), bool)
                and value["timeout_s"] > 0):
            return value
        return None
    return None


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--repo", required=True)
    p.add_argument("--label", required=True)
    p.add_argument("--stall-mins", type=float, default=12.0)
    p.add_argument("--max-mins", type=float, default=45.0)
    p.add_argument("--retry-alarm", type=int, default=3)
    p.add_argument("--poll-secs", type=float, default=20.0)
    p.add_argument("--appear-secs", type=float, default=120.0)
    p.add_argument("--launch-receipt", help="exact mode: the engine's launch receipt")
    p.add_argument("--launch-id", help="exact mode: the launch UUID (paired)")
    p.add_argument("--engine-pid", type=int, help="exact mode: expected engine PID")
    p.add_argument("--ready-only", action="store_true",
                   help="exact mode: return READY once the run is registered and alive")
    args = p.parse_args()
    if (args.launch_receipt is None) != (args.launch_id is None):
        p.error("--launch-receipt and --launch-id must be given together")
    if args.launch_id is not None:
        if not Path(args.launch_receipt).is_absolute():
            p.error("--launch-receipt must be an absolute path")
        try:
            args.launch_id = str(uuid.UUID(args.launch_id))
        except ValueError:
            p.error("--launch-id must be a UUID")
    elif args.ready_only or args.engine_pid is not None:
        p.error("--ready-only/--engine-pid need --launch-receipt and --launch-id")
    return args


def newest_transcript(runs: Path, label: str) -> Path | None:
    hits = sorted(runs.glob(f"*{label}*.md"), key=lambda f: f.stat().st_mtime)
    return hits[-1] if hits else None


def is_done(text: str) -> bool:
    for line in reversed(text.splitlines()):
        if line.strip():
            return bool(FOOTER.match(line.strip()))
    return False


def tail(path: Path, limit: int = 6000) -> str:
    try:
        with path.open("rb") as handle:
            handle.seek(max(0, os.fstat(handle.fileno()).st_size - limit))
            data = handle.read(limit)
    except OSError:
        return ""
    return data[-limit:].decode("utf-8", errors="replace")


def anomalies(transcript: Path) -> str:
    sidecar = transcript.with_suffix("").with_suffix(".anomalies.jsonl")
    if not sidecar.exists():
        sidecar = transcript.parent / (transcript.stem + ".anomalies.jsonl")
    try:
        return tail(sidecar, 3000)
    except OSError:
        return ""


def last_step(text: str) -> str:
    pending = mcp_state(text)
    if pending:
        return (f"MCP {pending['server']}/{pending['tool']} "
                f"{pending.get('duration_s', 0):.1f}s / {pending['timeout_s']}s")
    for line in reversed(text.splitlines()):
        if STEP.match(line):
            return line.strip()[:200]
    for line in reversed(text.splitlines()):
        stripped = line.strip()
        if stripped and not stripped.startswith(("#", "---", "```")):
            return stripped[:200]
    return "(transcript is empty)"


def report(verdict: str, path: Path, elapsed: float, text: str, code: int) -> int:
    print(f"\n{verdict}")
    print(f"  transcript : {path}")
    print(f"  elapsed    : {elapsed / 60:.1f} min")
    print(f"  last step  : {last_step(text)}")
    loaded = [line for line in text.splitlines()
              if line.startswith(("- loaded skills:", "_SKILL_LOADED "))]
    if loaded:
        print("  skills     : " + " | ".join(loaded)[-300:])
    quiet = (time.time() - path.stat().st_mtime) / 60 if path.exists() else -1
    print(f"  quiet for  : {quiet:.1f} min")
    return code


def main() -> int:
    args = parse_args()
    if args.launch_receipt is not None:
        return watch_exact(args)  # exact identity: never fall back to the label glob
    runs = Path(args.repo) / ".claude" / "agent-runs"
    started = time.time()
    path = None
    while time.time() - started < args.appear_secs:
        if runs.is_dir():
            path = newest_transcript(runs, args.label)
            if path is not None:
                break
        time.sleep(3)
    if path is None:
        print(f"NO TRANSCRIPT for label {args.label!r} under {runs} after "
              f"{args.appear_secs:.0f}s. The run probably never started.")
        return 6
    print(f"watching {path.name}  (stall {args.stall_mins}m, cap {args.max_mins}m)"
          "  [legacy label mode: advisory only]")
    sys.stdout.flush()
    while True:
        time.sleep(args.poll_secs)
        elapsed = time.time() - started
        text = tail(path, 65536)
        if is_done(text) and PARTIAL.search(text):
            return report("UNVERIFIED INCOMPLETE - a label-matched transcript ends with a "
                          "partial report, not a finished one; label mode cannot prove "
                          "which launch it belongs to.", path, elapsed, text, 6)
        if is_done(text):
            return report("UNVERIFIED - a label-matched transcript has a footer, but "
                          "label mode cannot prove this launch ran or completed. Use "
                          "--launch-receipt/--launch-id.", path, elapsed, text, 6)
        code = alarm(args, path, elapsed, text)
        if code is not None:
            return code


def watch_exact(args) -> int:
    """Follow one launch by receipt identity and its registered index entries."""
    receipt, started = Path(args.launch_receipt), time.time()
    announced = False
    while True:
        elapsed = time.time() - started
        try:
            rows = read_receipt(receipt)
            last = validate_receipt(rows, args.launch_id, args.engine_pid) if rows else None
        except (ValueError, UnicodeDecodeError, OSError) as exc:
            print(f"LAUNCH INVALID - {receipt}: {exc if isinstance(exc, ValueError) else type(exc).__name__}")
            return 7
        pid = args.engine_pid if args.engine_pid is not None else (
            rows[0].get("engine_pid") if rows else None)
        state = last.get("state") if last else None
        if state in STARTUP_STATES:
            print(f"LAUNCH {state.upper()} - reason_code={last.get('reason_code')} "
                  f"exit_code={last.get('exit_code')} receipt={receipt}")
            report_diagnostics(last, receipt)
            return 7
        if state in (None, "starting") and pid is not None and not process_alive(pid):
            print(f"LAUNCH DEAD - engine {pid} exited before registering; receipt={receipt}")
            report_diagnostics(last or {"launch_id": args.launch_id, "engine_pid": pid}, receipt)
            return 7
        if state == "ready":
            if not same_path(last.get("root"), args.repo) or last.get("label") != args.label:
                print("LAUNCH INVALID - project root or label does not match the requested launch")
                return 7
            try:
                start, end = index_events(last, pid)
            except ValueError:
                print("INDEX INVALID - conflicting registration history")
                return 7
            if start is not None and end is None and not process_alive(pid):
                try:
                    start, end = index_events(last, pid)  # the end may land just before exit
                except ValueError:
                    print("INDEX INVALID - conflicting registration history")
                    return 7
                if end is None:
                    print(f"NOT LIVE - engine {pid} cannot be verified, with no terminal index record "
                          f"for run {last['run_id']}; initiator unknown")
                    report_diagnostics(last, receipt)
                    return 7
            transcript = Path(last["transcript"])
            text = tail(transcript, 65536)
            if end is not None:
                if end.get("status") == "complete":
                    report_coverage(last)
                    return report("FINISHED - registered run ended complete.",
                                  transcript, elapsed, text, 0)
                report_diagnostics(last, receipt)
                reason = end.get("stop_reason")
                if (end.get("status") == "failed" and isinstance(reason, str)
                        and reason in STOP_REASONS):
                    return report(f"INCOMPLETE - registered run stopped at {reason}; partial "
                                  "evidence only, not a finished report. Not relaunched.",
                                  transcript, elapsed, text, INCOMPLETE_EXIT)
                return report(f"FAILED - registered run ended {end.get('status')!r}.",
                              transcript, elapsed, text, 7)
            if start is not None:
                if args.ready_only:
                    report_coverage(last)
                    print(f"READY - run {last['run_id']} registered, engine {pid} alive; "
                          f"transcript={transcript}")
                    return 0
                if not announced:
                    print(f"watching run {last['run_id']} (exact launch {args.launch_id})")
                    sys.stdout.flush()
                    announced = True
                code = alarm(args, transcript, elapsed, text)
                if code is not None:
                    return code
            elif elapsed >= args.appear_secs:
                print(f"UNREGISTERED - receipt names run {last['run_id']} but no matching "
                      "index start record was found")
                return 7
        elif elapsed >= args.appear_secs:
            print(f"NOT READY - no startup outcome in {receipt} after "
                  f"{args.appear_secs:.0f}s")
            report_diagnostics(last or {"launch_id": args.launch_id, "engine_pid": pid}, receipt)
            return 6
        time.sleep(args.poll_secs)


def report_diagnostics(ready, receipt):
    """Print revalidated metadata, not raw sidecars or inferred causes."""
    try:
        module = runpy.run_path(str(Path(__file__).with_name("harness_diagnostics.py")))
    except Exception:
        print("  diagnostics: unavailable")
        return
    identity = {k: ready.get(k) for k in ("run_id", "launch_id", "engine_pid")}
    launch_identity = {k: identity[k] for k in ("launch_id", "engine_pid")}
    wrapper_identity = {"launch_id": identity["launch_id"]}
    sources = [(str(receipt) + ".engine-startup.diagnostics.jsonl", launch_identity),
               (str(receipt) + ".ask.diagnostics.jsonl", wrapper_identity),
               (str(receipt) + ".fanout.diagnostics.jsonl", wrapper_identity)]
    base = ready.get("transcript")
    if base and identity["run_id"]:
        sources += [(base + ".engine.diagnostics.jsonl", identity),
                    (base + ".fanout.diagnostics.jsonl",
                     {k: identity[k] for k in ("run_id", "launch_id")}),
                    (base + ".control.diagnostics.jsonl",
                     {k: identity[k] for k in ("run_id", "engine_pid")}),
                    (base + ".observer.diagnostics.jsonl",
                     {k: identity[k] for k in ("run_id", "engine_pid")})]
    owned = []
    for path, expected in sources:
        events = module["read_events"](path, **expected)
        owned.extend(events)
        for event in events[-5:]:
            print("  diagnostic: " + json.dumps(event, sort_keys=True, separators=(",", ":")))
    report_coverage(ready, module, owned)
    report_owner_diagnostics(owned, module)
    report_kilo_diagnostics(owned, module["read_events"], module["diagnostic_directory"]())


def report_coverage(ready, module=None, owned=None):
    try:
        if module is None:
            module = runpy.run_path(str(Path(__file__).with_name("harness_diagnostics.py")))
        if owned is None:
            owned = module["read_events"](ready["transcript"] + ".engine.diagnostics.jsonl",
                                          run_id=ready["run_id"], engine_pid=ready["engine_pid"])
        engine = [row for row in owned if row["observer"] == "engine"]
        context = next((row for row in reversed(engine) if row.get("owner_pid")), {})
        coverage = module["read_coverage"](module["diagnostic_directory"](), owner_pid=context.get("owner_pid"),
            owner_id=context.get("owner_id"), process_id=context.get("processID"))
        print(f"  diagnostics coverage: engine={'recorded' if engine else 'unavailable'}; "
              f"plugin={coverage['plugin']}; owner={coverage['owner']}; "
              f"pre-kill={'active' if coverage['pre_kill'] else 'unavailable'}")
    except Exception:
        print("  diagnostics coverage: unavailable (launch verification is unchanged)")


def report_owner_diagnostics(owned, module):
    contexts = [row for row in owned if row["observer"] in {"engine", "ask", "fanout"}
                and row.get("owner_id") and row.get("processID")]
    identities = {(row["owner_id"], row["processID"]) for row in contexts}
    if not identities:
        return
    try:
        paths = sorted(module["diagnostic_directory"]().glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)[:32]
        records = [row for path in paths for row in module["read_events"](path) if row["observer"] == "kilo_owner"]
        candidates = [row for row in records if (row.get("owner_id"), row.get("processID")) in identities]
        expected_patch = module["known_patch_id"]()
        def actor_matches(row, context):
            return (expected_patch is not None and row.get("patch_id") == context.get("patch_id") == expected_patch
                    and row.get("owner_pid") == row.get("pid") == context.get("owner_pid")
                    and row.get("owner_birth") is not None
                    and row.get("owner_birth") == row.get("birth") == context.get("owner_birth"))
        matching = []
        for row in candidates:
            scoped = [ctx for ctx in contexts if (ctx["owner_id"], ctx["processID"]) == (row["owner_id"], row["processID"])]
            births = {ctx.get(key) for ctx in scoped for key in ("birth", "parent_birth", "child_birth")}
            births.discard(None)
            if row.get("target_birth") in births and any(actor_matches(row, ctx) for ctx in scoped):
                matching.append(row)
        if len(matching) < len(candidates):
            print("  Owner metadata exists but actor/target identity or patch fingerprint is unverified; no initiator attributed.")
        owners = {row["owner_id"] for row in matching}
        matching += [row for row in records if row.get("owner_id") in owners
                     and row["event"] in {"owner_boundary", "owner_disposed"}
                     and any(ctx["owner_id"] == row["owner_id"] and actor_matches(row, ctx) for ctx in contexts)]
        for row in sorted(matching, key=lambda r: r.get("ts", ""))[-12:]:
            print("  Owner diagnostic (recorded intent/result/observation, not inferred cause): " +
                  json.dumps(row, sort_keys=True, separators=(",", ":")))
    except Exception:
        print("  owner diagnostics: unavailable")


def event_time(row):
    try:
        return datetime.datetime.fromisoformat(row["ts"].replace("Z", "+00:00")).timestamp()
    except (KeyError, ValueError, TypeError, OverflowError, OSError):
        return None


def report_kilo_diagnostics(owned, read_events, directory=None):
    """Correlate a container's launch, never attribute a kill from PID alone."""
    launches = [row for row in owned if row["observer"] in {"ask", "fanout"}
                and row["event"] == "child_start"]
    if not launches:
        launches = [row for row in owned if row["observer"] == "engine" and row["event"] == "registered"]
    anchors = [event_time(row) for row in launches]
    anchors = [stamp for stamp in anchors if stamp is not None]
    if not anchors:
        return
    pids = {row.get(key) for row in launches for key in ("pid", "ppid", "child_pid", "engine_pid")}
    pids.discard(None)
    try:
        directory = directory or Path.home() / ".local/share/kilo/lifecycle"
        # shortcut: inspect only the latest 32 per-instance journals; older evidence
        # remains on disk for explicit inspection rather than an unbounded scan.
        paths = sorted(directory.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)[:32]
        observations = [row for path in paths for row in read_events(path) if row["observer"] == "kilo"]
    except OSError:
        return
    origin = min(anchors)
    containers = {(row.get("instanceID"), row.get("processID")) for row in observations
                  if row["event"] == "background_observed" and row.get("action") in {"start", "restart"}
                  and row.get("child_pid") in pids and event_time(row) is not None
                  and abs(event_time(row) - origin) <= 60
                  and row.get("instanceID") and row.get("processID")}
    matching = [row for row in observations
                if (row.get("instanceID"), row.get("processID")) in containers]
    instances = {instance for instance, process in containers}
    matching += [row for row in observations
                 if row["event"] == "instance_disposed" and row.get("instanceID") in instances]
    for row in sorted(matching, key=lambda r: r.get("ts", ""))[-8:]:
        print("  Kilo diagnostic (container observation/intent, not exit proof): " +
              json.dumps(row, sort_keys=True, separators=(",", ":")))


def alarm(args, path: Path, elapsed: float, text: str) -> int | None:
    """Retry, overrun and stall alarms shared by legacy and exact modes."""
    if True:
        try:
            quiet_mins = (time.time() - path.stat().st_mtime) / 60
        except OSError:
            quiet_mins = elapsed / 60  # transcript not visible: count from watch start
        call = None
        for line in reversed(text.splitlines()):
            if MCP_EVENT.match(line):
                break
            match = CALLING.match(line)
            if match:
                call = match
                break
        pending_mcp = mcp_state(text)
        mid_call = call is not None and not is_done(text)
        if pending_mcp:
            mid_call = quiet_mins * 60 <= pending_mcp["timeout_s"] + 10
        attempt = int(call.group(2)) if call else 0
        if attempt >= args.retry_alarm:
            text += "\n" + anomalies(path)
            if NO_TOOL.search(text):
                why = "the model keeps replying in prose with no tool call. Check the brief."
            elif TRANSPORT.search(text):
                why = "the endpoint is failing (transport error)."
            else:
                why = "cause unclear from the transcript. Read it before re-dispatching."
            return report(f"RETRYING - attempt {attempt}/{call.group(3)} to "
                          f"{call.group(1)}. {why}", path, elapsed, text, 4)
        if elapsed / 60 >= args.max_mins:
            return report(f"OVERRAN - still going after {args.max_mins:.0f} min.",
                          path, elapsed, text, 5)
        if quiet_mins >= args.stall_mins and not mid_call:
            return report(f"STALLED - nothing written for {quiet_mins:.1f} min.",
                          path, elapsed, text, 3)


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""Explicit, single-process live context canary. Import and --help are offline."""
import argparse
import contextlib
import copy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import urllib.parse
import urllib.request
import uuid

ROOT = Path(__file__).resolve().parents[2]
MODELS = {"claude-chat": "anthropic_tool_call.claude-opus-5-5-think",
          "gpt-chat": "gpt-6.1-sol", "gpt-responses": "gpt-6.1-sol"}
LIMIT = 96
ENDPOINTS = {"/api/chat/completions": "chat", "/openai/responses": "responses"}


class CanaryFailure(Exception):
    def __init__(self, code):
        self.code = code


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise CanaryFailure("invalid_arguments")


def parse(argv=None):
    p = Parser(description=__doc__)
    p.add_argument("--run-live", action="store_true")
    for name in ("case", "engine", "out", "launch-id", "launch-receipt"):
        p.add_argument("--" + name)
    return p.parse_args(argv)


def _inside(path, parent):
    return path != parent and parent in path.parents


def _fresh_temp(value, directory=False):
    if not value:
        return None
    p = Path(value)
    temp = Path(tempfile.gettempdir()).resolve()
    if not p.is_absolute() or p != p.resolve() or not _inside(p, temp):
        return None
    if p.exists() or p.is_symlink() or not p.parent.is_dir():
        return None
    return p


def validate(ns):
    if not ns.run_live:
        return "run_live_required"
    if ns.case not in MODELS:
        return "invalid_case"
    if ns.engine not in ("source", "installed"):
        return "invalid_engine"
    try:
        if str(uuid.UUID(ns.launch_id)) != ns.launch_id:
            return "invalid_launch_id"
    except (ValueError, TypeError, AttributeError):
        return "invalid_launch_id"
    out = _fresh_temp(ns.out, directory=True)
    receipt = _fresh_temp(ns.launch_receipt)
    if out is None:
        return "invalid_out"
    if receipt is None or receipt == out or _inside(out, receipt) or _inside(receipt, out):
        return "invalid_launch_receipt"
    return None


def _number(value):
    return value if type(value) is int and 0 <= value <= 10**9 else None


def _paired(rows, responses):
    pending = set()
    single = True
    for row in rows:
        if not isinstance(row, dict):
            return False, False
        if responses:
            kind = row.get("type")
            if kind == "function_call":
                call_id = row.get("call_id")
                if not isinstance(call_id, str) or not call_id or call_id in pending:
                    return False, False
                single = single and not pending
                pending.add(call_id)
            elif kind == "function_call_output":
                call_id = row.get("call_id")
                if not isinstance(call_id, str) or call_id not in pending:
                    return False, False
                pending.remove(call_id)
            elif pending:
                return False, False
        else:
            calls = row.get("tool_calls", [])
            if row.get("role") == "assistant" and calls:
                if pending or not isinstance(calls, list):
                    return False, False
                single = single and len(calls) == 1
                for call in calls:
                    call_id = call.get("id") if isinstance(call, dict) else None
                    if not isinstance(call_id, str) or not call_id or call_id in pending:
                        return False, False
                    pending.add(call_id)
            elif row.get("role") == "tool":
                call_id = row.get("tool_call_id")
                if not isinstance(call_id, str) or call_id not in pending:
                    return False, False
                pending.remove(call_id)
            elif pending:
                return False, False
    return not pending, single


def wire_metadata(path, data):
    """Parse locally; never return text, URLs, headers, arbitrary models or errors."""
    try:
        body = json.loads(data)
        if not isinstance(body, dict):
            return {"valid": False}
        shapes = [key for key in ("messages", "input") if key in body]
        route = {"messages": "chat", "input": "responses"}[shapes[0]] if len(shapes) == 1 else None
        endpoint = ENDPOINTS.get(path)  # exact path only; no guessed prefixes
        rows = body.get("messages" if route == "chat" else "input", [])
        if not isinstance(rows, list) or len(rows) > 10000:
            return {"valid": False}
        paired, single = _paired(rows, route == "responses")
        caps = [key for key in ("max_tokens", "max_completion_tokens", "max_output_tokens") if key in body]
        cap = caps[0] if len(caps) == 1 else None
        reasoning, flat = body.get("reasoning"), body.get("reasoning_effort")
        nested = reasoning.get("effort") if isinstance(reasoning, dict) else None
        form = ("flat" if flat is not None and reasoning is None else
                "nested" if nested is not None and flat is None else None)
        effort = flat if form == "flat" else nested if form == "nested" else None
        return {"valid": route is not None and endpoint == route, "route": route,
                "endpoint": endpoint, "effort_form": form,
                "model": body.get("model") if body.get("model") in MODELS.values() else None,
                "cap_field": cap, "cap_value": _number(body.get(cap)),
                "message_count": min(len(rows), 10000) if route == "chat" else 0,
                "input_count": min(len(rows), 10000) if route == "responses" else 0,
                "tools_present": bool(body.get("tools")), "paired": paired,
                "single_tool_batches": single,
                "memory_present": False,
                "effort": effort if effort in ("low", "medium", "high") else None}
    except (ValueError, TypeError, UnicodeError):
        return {"valid": False}


def wire_ok(case, meta, phase=None):
    """Exact route/model/cap/effort form per case (apply_effort); maintenance has no tools."""
    route = "responses" if case == "gpt-responses" else "chat"
    cap = "max_output_tokens" if route == "responses" else "max_completion_tokens"
    form = "flat" if case == "claude-chat" else "nested"
    return bool(meta.get("valid") and meta.get("route") == route and meta.get("endpoint") == route and
                meta.get("model") == MODELS[case] and meta.get("cap_field") == cap and
                meta.get("cap_value") == 8192 and meta.get("effort_form") == form and
                meta.get("effort") == _effort(case) and meta.get("paired") is True and
                meta.get("single_tool_batches") is True and
                not (phase == "compaction" and meta.get("tools_present")))


def journal_mark(path, previous):
    """Bounded byte count + prefix hash; the earlier mark must remain an exact prefix."""
    try:
        data = path.read_bytes() if path.stat().st_size <= 4_000_000 else None
    except OSError:
        data = None
    if data is None:
        raise CanaryFailure("journal_missing")
    if previous and (len(data) < previous["bytes"] or
                     hashlib.sha256(data[:previous["bytes"]]).hexdigest() != previous["sha256_prefix"]):
        raise CanaryFailure("journal_rewritten")
    return {"bytes": len(data), "sha256_prefix": hashlib.sha256(data).hexdigest()}


def _finish_ok(row):
    calls = row.get("tool_calls") if row.get("role") == "assistant" else None
    if not isinstance(calls, list) or len(calls) != 1 or not isinstance(calls[0], dict):
        return None
    function = calls[0].get("function")
    if not isinstance(function, dict) or function.get("name") != "finish":
        return None
    try:
        args = json.loads(function.get("arguments", "{}"))
    except (ValueError, TypeError):
        return False
    return (isinstance(args, dict) and isinstance(args.get("summary"), str)
            and bool(args["summary"].strip()))


def journal_metadata(rows):
    """Every native call, including finish, has a journalled result; no replayed actions."""
    paired, _ = _paired(rows, False)
    if not paired:
        raise CanaryFailure("journal_pairs")
    seen, finish = set(), False
    for row in rows:
        if row.get("role") == "assistant" and row.get("tool_calls"):
            finish = _finish_ok(row) is True
            key = hashlib.sha256(json.dumps(row.get("tool_calls"), sort_keys=True).encode()).hexdigest()
            if key in seen:  # identical action replayed; a reused CLOSED id with new args is fine
                raise CanaryFailure("journal_duplicate")
            seen.add(key)
    return {"rows": len(rows), "actions": len(seen), "paired": True, "normal_finish": finish}


def _jsonl(path):
    rows = []
    try:
        if not path.is_file() or path.stat().st_size > 4_000_000:
            raise CanaryFailure("artifact_missing_or_large")
        with path.open(encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    row = json.loads(line)
                    if not isinstance(row, dict) or len(rows) >= 2000:
                        raise CanaryFailure("artifact_invalid")
                    rows.append(row)
    except (OSError, ValueError, UnicodeError):
        raise CanaryFailure("artifact_invalid") from None
    return rows


def _effort(case):
    return "medium" if case == "claude-chat" else "high"


def identity_check(ns, ids, pid, receipt, index):
    """Strict join: receipt starting+ready, then exactly one start/end for our run_id."""
    run_id, transcript = ids["run_id"], str(ids["transcript"])
    same = all(type(r.get("engine_pid")) is int and r.get("engine_pid") == pid and
               r.get("launch_id") == ns.launch_id for r in receipt)
    if (len(receipt) != 2 or not same or [r.get("state") for r in receipt] != ["starting", "ready"] or
            receipt[0].get("run_id") is not None):
        raise CanaryFailure("receipt_identity")
    ready = receipt[1]
    paths = ready.get("index_paths")
    if (ready.get("run_id") != run_id or ready.get("transcript") != transcript or
            ready.get("root") != str(ROOT) or ready.get("label") != run_id or
            not isinstance(paths, list) or str(ids["index"]) not in paths):
        raise CanaryFailure("receipt_identity")
    ours = [r for r in index if r.get("run_id") == run_id]
    if [r.get("event") for r in ours] != ["start", "end"]:
        raise CanaryFailure("index_identity")
    start, end = ours
    expected = {"engine_pid": pid, "root": str(ROOT), "model": MODELS[ns.case],
                "effort": _effort(ns.case), "transcript": transcript,
                "messages": str(ids["messages"]), "usage_journal": str(ids["usage"])}
    if any(start.get(k) != v for k, v in expected.items()):
        raise CanaryFailure("index_identity")
    return end


def usage_metadata(rows):
    """Engine-normalized sent/recv/cache only (append_usage_record); never raw counts."""
    result, seen = [], set()
    for row in rows:
        key = json.dumps(row, sort_keys=True)
        if key in seen:
            raise CanaryFailure("usage_duplicate")
        seen.add(key)
        result.append({"phase": row.get("phase") if row.get("phase") in
                       ("loop", "worker", "report", "compaction") else None,
                       **{k: _number(row.get(k)) for k in ("sent", "recv", "cache_read", "cache_write")}})
    return result


def final_gate(state):
    """Pure offline semantic gate; persisted files alone never prove compaction."""
    if state.get("engine_exit") != 0:
        return "engine_failed"
    if state.get("observer_error"):
        return state["observer_error"]
    if not state.get("normal_finish"):
        return "finish_missing"
    ends = state.get("ends", [])
    if len(ends) != 1 or not state.get("identities"):
        return "terminal_identity"
    end = ends[0]
    if end.get("status") != "complete" or end.get("refused") != 0 or end.get("stop_reason"):
        return "terminal_failed"
    usage = state.get("usage", [])
    if (not usage or not any(row.get("phase") in ("loop", "worker", "report") for row in usage) or
            any(row.get("sent") is None or row.get("recv") is None for row in usage)):
        return "usage_unknown"
    if state.get("reads") != state.get("expected_reads") or not state.get("expected_reads"):
        return "read_sequence"
    if state.get("delivered_reads") != state.get("expected_reads"):
        return "read_not_delivered"
    wires = state.get("wires", [])
    if not wires or any(not row.get("checked") for row in wires):
        return "wire_invalid"
    commits = state.get("compactions", [])
    if state.get("case") != "gpt-responses":
        if commits:
            return "unexpected_compaction"
    else:
        actual = [row for row in commits if row.get("committed") and row.get("verified")]
        if not actual:
            return "compaction_not_committed"
        if not any(row.get("phase") == "compaction" for row in usage):
            return "compaction_usage_unknown"
        if not state.get("new_final_read_after_commit"):
            return "postcommit_read_missing"
        if not any(row.get("after_commit") and row.get("memory_present") for row in wires):
            return "postcommit_memory_missing"
    if len(usage) != len(wires):
        return "usage_coverage"
    def phase(value):
        return "worker" if value in ("loop", "worker") else value if value in ("report", "compaction") else None
    usage_phases = [phase(row.get("phase")) for row in usage]
    wire_phases = [phase(row.get("phase")) for row in wires]
    if None in usage_phases or None in wire_phases or usage_phases != wire_phases:
        return "usage_phase_coverage"
    if any(end.get(key) != sum(row[key] for row in usage) for key in ("sent", "recv")):
        return "usage_total_mismatch"
    return None


def _fixtures(launch_id, count):
    directory = ROOT / ".claude" / "agent-runs" / "canary-fixtures" / launch_id / "files"
    directory.mkdir(parents=True, exist_ok=False)
    paths = []
    for i in range(count):
        path = directory / ("fixture-%02d.txt" % (i + 1))
        text = "".join(hashlib.sha256(("context-canary:%d:%d" % (i, n)).encode()).hexdigest() + "\n"
                       for n in range(182))
        with path.open("x", encoding="utf-8", newline="\n") as f:
            f.write(text)
        paths.append(path)
    return paths


def _brief(agents, paths):
    return ("Read AGENTS.md first, then read each listed synthetic fixture EXACTLY ONCE IN ORDER. "
            "Use one native read_file tool call per batch, with complete file contents. "
            "Do not use any other tools, commands, MCP, nested dispatch, or other reads. "
            "After all reads finish with a short nonempty report. All data is synthetic. "
            "Dispatch anomalies remain active scope; preserve bounded evidence and report blockers.\n"
            "Fixture paths (in order, relative to the root):\n" +
            "\n".join(p.relative_to(ROOT).as_posix() for p in paths) +
            "\nThe complete CURRENT AGENTS.md is pinned here independently of tool reads:\n" + agents)


def _run_paths(ns, out):
    """Unique per-launch identity; every sidecar derives from the transcript stem."""
    run_id = "canary-%s-%s" % (ns.case, ns.launch_id)
    return {"run_id": run_id, "out": out, "transcript": out / (run_id + ".md"),
            "index": out / "index.jsonl", "messages": out / (run_id + ".messages.jsonl"),
            "usage": out / (run_id + ".usage.jsonl")}


def _engine_argv(ns, ids, brief):
    transcript = ids["transcript"]
    argv = ["delegate.py", "-C", str(ROOT), "--profile", "resilient-v1", "--skill", "ponytail",
            "--label", ids["run_id"],
            "--context-policy", "model-v1", "--context-window-tokens", "65536",
            "--context-reply-tokens", "8192", "--context-safety-tokens", "4096",
            "--max-iters", "18", "--launch-id", ns.launch_id,
            "--launch-receipt", ns.launch_receipt, "--transcript", str(transcript),
            "--model", MODELS[ns.case], "--effort",
            "medium" if ns.case == "claude-chat" else "high"]
    argv += ["--auto-compact", "--compact-fraction", "0.50"] if ns.case == "gpt-responses" else ["--no-auto-compact", "--no-stream"]
    return argv + ["-t", brief]


def _snapshot(rt):
    rows = {row["id"]: (copy.deepcopy(row["msg"]), row["pinned"]) for row in rt._rows}
    groups = [(g["kind"], tuple(g["ids"]), tuple(g["refs"])) for g in rt._groups]
    return rows, groups


def _bindings(before, after):
    rows, groups = before
    kept, _ = after
    pinned = [key for key, (_, pin) in rows.items() if pin]
    native = [g for g in groups if g[0] in ("native", "prompt", "tool", "native_tool")]
    newest = native[-1][1] if native else ()
    return (bool(pinned) and all(key in kept and rows[key] == kept[key] for key in pinned),
            bool(newest) and all(key in kept and rows[key] == kept[key] for key in newest))


class Observer:
    def __init__(self, ns, engine, ce, paths):
        self.ns, self.engine, self.ce = ns, engine, ce
        self.paths = [ROOT / "AGENTS.md"] + paths
        self.reads, self.wires, self.compactions = [], [], []
        self.expected, self.delivered, self.read_proofs = {}, set(), []
        self.committed = False
        self.new_final_read_after_commit = False
        self.geometry = None
        self.memory = []
        self.error = None
        self.pager_calls = 0
        self.journal_path, self.journal = None, None

    def fail(self, code):
        self.error = self.error or code
        raise CanaryFailure(code)

    @property
    def delivered_reads(self):
        return [path for path in self.reads if path in self.delivered]

    def observe_delivery(self, body):
        # Only actual tool-result fields count, never task pins or replacement memory.
        texts = []
        for row in body.get("messages", body.get("input", [])):
            value = (row.get("content") if row.get("role") == "tool" else
                     row.get("output") if row.get("type") == "function_call_output" else None)
            if not isinstance(value, str):
                continue
            texts.append(value)
            try:
                stack = [(json.loads(value), 0)]
            except (ValueError, TypeError, RecursionError):
                continue
            nodes = 0
            while stack and nodes < 512:
                item, depth = stack.pop()
                nodes += 1
                if isinstance(item, str):
                    texts.append(item)
                elif depth < 8 and isinstance(item, (dict, list)):
                    children = item.values() if isinstance(item, dict) else item
                    stack.extend((v, depth + 1) for v in children)
        for path, expected in self.expected.items():
            if any(expected in text for text in texts):
                self.delivered.add(path)

    def dispatch(self, original, runner, name, argv):
        if not isinstance(argv, dict):
            self.fail("tool_forbidden")
        if name == "read_context_result":
            rt = runner.context_runtime
            ref = argv.get("ref")
            # Frozen earlier prefixes and compacted groups remain readable in this
            # owning store; active groups/latest index are not an ownership boundary.
            owned = False
            if isinstance(ref, str):
                try:
                    owned = rt._store._index_high(ref) is not None or rt._store._lookup(ref) is not None
                except Exception:
                    pass  # closed lookup failure; never invoke the underlying pager
            offset, limit = argv.get("offset", 0), argv.get("limit", 4096)
            if (not owned or
                    set(argv) - {"ref", "offset", "limit"} or
                    type(offset) is not int or offset < 0 or
                    type(limit) is not int or not 1 <= limit <= 4096):
                self.fail("context_result_unverified")
            self.pager_calls += 1
            if self.pager_calls > LIMIT:
                self.fail("observation_limit")
            return original(runner, name, argv)
        if name != "read_file":
            self.fail("tool_forbidden")
        raw = argv.get("path")
        if not isinstance(raw, str) or set(argv) - {"path", "start_line", "end_line"}:
            self.fail("read_forbidden")
        path = Path(raw)
        path = (ROOT / path).resolve() if not path.is_absolute() else path.resolve()
        n = len(self.reads)
        if n >= len(self.paths) or path != self.paths[n]:
            self.fail("read_sequence")
        expected = path.read_text(encoding="utf-8")
        start, end = argv.get("start_line"), argv.get("end_line")
        if ((start is not None and (type(start) is not int or start != 1)) or
                (end is not None and (type(end) is not int or end < 1))):
            self.fail("read_forbidden")
        rendered = expected
        if start is not None or end is not None:
            count = len(expected.splitlines())
            if (end if end is not None else 201) < count:
                self.fail("read_forbidden")
            rendered = f"({raw} lines 1-{count})\n" + expected
        value = original(runner, name, argv)
        if not isinstance(value, str) or value != rendered:
            self.fail("read_incomplete")
        key = str(path)
        self.reads.append(key)
        self.expected[key] = expected
        self.read_proofs.append({"path": key, "complete": True,
                                 "bytes": len(expected.encode("utf-8")),
                                 "sha256": hashlib.sha256(expected.encode("utf-8")).hexdigest()})
        if self.committed and path == self.paths[-1]:
            self.new_final_read_after_commit = True
        return value

    def attempt(self, original, adapter):
        rt = adapter.rt
        before = _snapshot(rt)
        counter = rt.metadata().get("compactions")
        result = original(adapter)
        after = _snapshot(rt)
        committed = result.committed
        pinned, newest = _bindings(before, after)
        post_counter = rt.metadata().get("compactions")
        verified = (committed is True and type(result.before_tokens) is int and
                    type(result.after_tokens) is int and result.after_tokens < result.before_tokens and
                    result.after_tokens <= 26214 and type(counter) is int and
                    post_counter == counter + 1 and pinned and newest)
        row = {"code": result.code if result.code in ("committed", "ok", "no_progress", "below_trigger", "not_needed") else "other",
               "committed": committed is True, "verified": verified,
               "before_tokens": _number(result.before_tokens), "after_tokens": _number(result.after_tokens),
               "removed_groups": _number(result.removed_groups), "calls": _number(result.calls),
               "counter": _number(post_counter), "pinned_retained": pinned, "newest_retained": newest}
        ref = getattr(result, "artifact_ref", None)
        row["checkpoint_ref"] = ref if isinstance(ref, str) and ref.startswith("artifact://") and len(ref) <= 512 else None
        if len(self.compactions) >= LIMIT:
            self.fail("observation_limit")
        self.compactions.append(row)
        if committed:
            if not verified:
                self.fail("compaction_invalid")
            new_ids = set(after[0]) - set(before[0])
            memory_ids = {key for kind, ids, refs in after[1] if kind == "memory" for key in ids}
            self.memory = [copy.deepcopy(after[0][key][0]) for key in new_ids & memory_ids]
            if len(self.memory) != 1:
                self.fail("compaction_memory_invalid")
            self.committed = True
        return result

    def geometry_check(self, body):
        # Simulate complete batches with the actual post-preflight wire and active meter.
        scope = self.ce.current()
        if scope is None:
            raise CanaryFailure("canary_geometry")
        responses = "input" in body
        key = "input" if responses else "messages"
        base = copy.deepcopy(body)
        meter = scope.meter

        def measure(packet):
            estimate = meter.estimate(packet)
            tokens = getattr(estimate, "tokens", None)
            if type(tokens) is not int or tokens < 0 or getattr(estimate, "measurable", True) is False:
                raise CanaryFailure("canary_geometry")
            return tokens

        def batch(path, index):
            # Include line/page framing overhead without executing a tool or changing runtime.
            text = path.read_text(encoding="utf-8")
            rendered = "\n".join("%04d: %s" % (n + 1, line) for n, line in enumerate(text.splitlines()))
            call_id = "canary_geometry_%d" % index
            args = json.dumps({"path": str(path)})
            if responses:
                return [{"type": "function_call", "call_id": call_id, "name": "read_file", "arguments": args},
                        {"type": "function_call_output", "call_id": call_id, "output": rendered}]
            return [{"role": "assistant", "content": None, "tool_calls": [
                        {"id": call_id, "type": "function", "function": {"name": "read_file", "arguments": args}}]},
                    {"role": "tool", "tool_call_id": call_id, "content": rendered}]

        initial = measure(base)
        simulated = copy.deepcopy(base)
        # AGENTS is also read, despite its independent pinned copy in the task.
        simulated[key] += batch(self.paths[0], 0)
        previous = measure(simulated)
        largest = max(0, previous - initial)
        crossing = None
        retained = initial
        for i, path in enumerate(self.paths[1:], 1):
            group = batch(path, i)
            simulated[key] += group
            total = measure(simulated)
            largest = max(largest, total - previous)
            previous = total
            pins_newest = copy.deepcopy(base)
            pins_newest[key] += group
            # Bound memory at the compactor's allowed summary reply cap (8192).
            retained = max(retained, measure(pins_newest) + 8192)
            if crossing is None and total >= 32768:
                crossing = i
        safe = (32768 + largest + 8192 < 53248 and retained < 26214)
        if self.ns.case == "gpt-responses":
            safe = safe and crossing is not None and crossing < len(self.paths) - 1
        else:
            safe = safe and previous + 8192 < 53248
        if not safe:
            raise CanaryFailure("canary_geometry")
        self.geometry = {"safe": True, "initial_tokens": initial, "largest_batch": largest,
                         "crossing_fixture": crossing, "retained_bound": retained,
                         "input_allowance": 53248, "trigger": 32768, "target": 26214}

    def urlopen(self, original, request, *args, **kwargs):
        if self.error:
            self.fail(self.error)
        if self.journal_path is not None and self.journal_path.exists():
            try:  # model boundary: journal so far must be an append-only extension
                self.journal = journal_mark(self.journal_path, self.journal)
            except CanaryFailure as exc:
                self.fail(exc.code)
        data = getattr(request, "data", None)
        if data is None:
            return original(request, *args, **kwargs)
        url = getattr(request, "full_url", None)
        try:
            path = urllib.parse.urlsplit(url).path if isinstance(url, str) else None
        except ValueError:
            path = None
        meta = wire_metadata(path, data)
        if not meta.get("valid"):
            self.fail("wire_invalid")
        body = json.loads(data)
        if self.geometry is None:
            try:
                self.geometry_check(body)
            except CanaryFailure:
                self.fail("canary_geometry")
        scope = self.ce.current()
        if scope is None:
            self.fail("wire_scope_missing")
        meta["phase"] = scope.phase if scope.phase in ("worker", "report", "compaction") else None
        estimate = scope.last_estimate
        meta["estimate"] = _number(getattr(estimate, "tokens", None))
        for flag in ("measurable", "calibrated", "over_limit"):
            value = getattr(estimate, flag, None)
            meta[flag] = value if type(value) is bool else None
        meta["after_commit"] = self.committed
        rows = body.get("messages", body.get("input", []))
        def text_parts(value):
            if isinstance(value, str):
                return [value]
            if isinstance(value, list):
                return [part.get("text") for part in value if isinstance(part, dict) and isinstance(part.get("text"), str)]
            return []
        meta["memory_present"] = any(row.get("role") == "assistant" and
            memory.get("content") in text_parts(row.get("content"))
            for memory in self.memory for row in rows if isinstance(row, dict))
        meta["checked"] = wire_ok(self.ns.case, meta, meta["phase"])
        if not meta["checked"]:
            self.fail("wire_identity")
        self.observe_delivery(body)
        if len(self.wires) >= LIMIT:
            self.fail("observation_limit")
        self.wires.append(meta)
        return original(request, *args, **kwargs)


@contextlib.contextmanager
def _loaded_engine(engine_path):
    # Match the real CLI's script-directory imports, including late recovery helpers.
    previous = list(sys.path)
    name = "_context_live_canary_engine"
    old_module = sys.modules.get(name)
    try:
        sys.path.insert(0, str(engine_path.resolve().parent))
        spec = importlib.util.spec_from_file_location(name, engine_path)
        engine = importlib.util.module_from_spec(spec)
        sys.modules[name] = engine
        spec.loader.exec_module(engine)
        yield engine
    finally:
        sys.path[:] = previous
        if old_module is None:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = old_module


def _execute(ns, out):
    engine_path = ROOT / "engine" / "delegate.py" if ns.engine == "source" else Path.home() / ".claude" / "bin" / "delegate.py"
    ids = _run_paths(ns, out)
    paths = _fixtures(ns.launch_id, 6 if ns.case == "gpt-responses" else 1)
    agents = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
    with _loaded_engine(engine_path) as engine:
        return _execute_loaded(ns, ids, paths, agents, engine)


def _execute_loaded(ns, ids, paths, agents, engine):
    ce = engine.capability_module("context_engine")
    for module in ("context_runtime", "context_compaction", "context_tokens"):
        engine.capability_module(module)
    observer = Observer(ns, engine, ce, paths)
    observer.journal_path = ids["messages"]
    old_urlopen, old_dispatch, old_attempt = urllib.request.urlopen, engine.Runner.dispatch, ce.AutomaticCompaction.attempt
    old_argv = sys.argv
    try:
        urllib.request.urlopen = lambda request, *a, **kw: observer.urlopen(old_urlopen, request, *a, **kw)
        engine.Runner.dispatch = lambda runner, name, argv: observer.dispatch(old_dispatch, runner, name, argv)
        ce.AutomaticCompaction.attempt = lambda adapter: observer.attempt(old_attempt, adapter)
        sys.argv = _engine_argv(ns, ids, _brief(agents, paths))
        try:
            code = engine.main()
            code = 0 if code is None else code if type(code) is int else 1
        except SystemExit as exc:
            code = 0 if exc.code is None else exc.code if type(exc.code) is int else 1
        except Exception:
            code = None  # engine outcome unknown; metadata is still collected below
        ns.engine_exit = code
    finally:
        urllib.request.urlopen, engine.Runner.dispatch, ce.AutomaticCompaction.attempt = old_urlopen, old_dispatch, old_attempt
        sys.argv = old_argv
    return _collect(ns, ids, observer, code, os.getpid())


def _collect(ns, ids, observer, code, pid):
    """Bounded metadata survives every artifact failure; only fixed error codes, never text."""
    artifacts = {k: str(ids[k]) for k in ("transcript", "index", "messages", "usage")}
    artifacts.update(receipt=ns.launch_receipt, fixtures=[str(p) for p in observer.paths[1:]])
    result = {"ok": False, "error": None, "case": ns.case, "engine": ns.engine,
              "launch_id": ns.launch_id, "run_id": ids["run_id"], "engine_pid": pid,
              "engine_exit": code, "provider_enforcement": None, "geometry": observer.geometry,
              "wires": observer.wires, "compactions": observer.compactions, "usage": None,
               "journal": None, "read_count": len(observer.reads), "pager_calls": observer.pager_calls,
               "delivered_read_count": len(observer.delivered_reads), "read_proofs": observer.read_proofs,
              "normal_finish": False,
              "new_final_read_after_commit": observer.new_final_read_after_commit,
              "artifacts": artifacts}
    try:
        end = identity_check(ns, ids, pid, _jsonl(Path(ns.launch_receipt)), _jsonl(ids["index"]))
        if not ids["usage"].is_file():
            raise CanaryFailure("usage_missing")
        result["usage"] = usage_metadata(_jsonl(ids["usage"]))
        journal = journal_metadata(_jsonl(ids["messages"]))
        journal["mark"] = journal_mark(ids["messages"], observer.journal)
        result["journal"], result["normal_finish"] = journal, journal["normal_finish"]
        error = final_gate({"case": ns.case, "engine_exit": code, "ends": [end], "identities": True,
                            "normal_finish": journal["normal_finish"], "observer_error": observer.error,
                             "usage": result["usage"], "reads": observer.reads,
                             "delivered_reads": observer.delivered_reads,
                            "expected_reads": [str(p) for p in observer.paths],
                            "wires": observer.wires, "compactions": observer.compactions,
                            "new_final_read_after_commit": observer.new_final_read_after_commit})
    except CanaryFailure as exc:
        error = exc.code
    except (OSError, ValueError, TypeError, UnicodeError):
        error = "artifact_invalid"
    if code != 0:
        error = "engine_failed"
    result["ok"], result["error"] = error is None, error
    return result, code if type(code) is int and code != 0 else 1 if error else 0


def main(argv=None):
    out = None
    owns_out = False
    try:
        ns = parse(argv)
        error = validate(ns)
        if error:
            raise CanaryFailure(error)
        out = Path(ns.out)
        out.mkdir(exist_ok=False)
        owns_out = True
        old_index = os.environ.get("CLAUDE_AGENT_RUN_INDEX")
        try:
            os.environ["CLAUDE_AGENT_RUN_INDEX"] = str(out / "index.jsonl")
            with open(os.devnull, "w", encoding="utf-8") as discard:
                with contextlib.redirect_stdout(discard), contextlib.redirect_stderr(discard):
                    result, code = _execute(ns, out)
        finally:
            if old_index is None:
                os.environ.pop("CLAUDE_AGENT_RUN_INDEX", None)
            else:
                os.environ["CLAUDE_AGENT_RUN_INDEX"] = old_index
    except CanaryFailure as exc:
        result, code = {"ok": False, "error": exc.code}, 1
    except Exception:
        result, code = {"ok": False, "error": "canary_internal"}, 1
    engine_exit = getattr(locals().get("ns"), "engine_exit", None)
    if engine_exit is not None and engine_exit != 0:
        code = engine_exit
    if owns_out:
        try:
            with (out / "result.json").open("x", encoding="utf-8") as f:
                json.dump(result, f, separators=(",", ":"))
        except Exception:
            result = dict(result, ok=False, error="result_write_failed")
            code = code or 1
    print(json.dumps(result, separators=(",", ":")))
    return code


if __name__ == "__main__":
    raise SystemExit(main())

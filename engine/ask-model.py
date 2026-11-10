#!/usr/bin/env python3
"""Send a prompt plus file contents to a model through the engine, and print the reply.

Exists so Claude Code can offload big-input/small-output work (summarize, extract,
inventory) without the input ever entering its own context: the files are read
from disk by the engine, and only the model's answer comes back on stdout.

This is a thin wrapper over the engine's one-shot mode:

    <this dir>/delegate.py --oneshot -C ROOT -m MODEL [-e EFFORT] --label LABEL
                           [--temperature T] [--attach FILE]... -f PROMPT_FILE

Going through the engine registers the call as a run, so it shows up in the
AgentControl dashboard and its tokens are counted. The engine owns everything
else: the gateway URL and API key, the effort rules per model, the
600,000-character input cap, and the "[sent N tok to MODEL; returned M tok]"
line on stderr. This script only writes the prompt to a temporary UTF-8 file,
runs the engine with stdout/stderr passed straight through, and exits with the
engine's code (0 success, 1 gateway failure, 2 usage error).

Usage:
    ask-model.py -p "List every function that touches resume state." src/main.ts
    ask-model.py -m gpt-5.6-sol -e high -p "Summarize the conclusions." docs/**/2026-08-04.md
    ask-model.py -m gpt-5.6-sol --label summary-notes -C D:/proj -p "..." notes.md
    ask-model.py --list-models          # print the gateway's model ids, one per line

--list-models loads the engine module in-process and asks the gateway for its
model ids (via the engine's load_cfg/refresh_models); no -p is needed and no
subprocess is started. It exits 1 if the gateway cannot be reached.

-m defaults to $OPENWEBUI_MODEL. --label defaults to "ask-<model>" with every run
of non-alphanumerics replaced by "-", trimmed to 40 characters. -C defaults to the
current directory.
"""
import argparse
import importlib.util
import os
import re
import runpy
import subprocess
import sys
import tempfile
import uuid
from types import SimpleNamespace

DELEGATE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "delegate.py")
EFFORTS = ["none", "low", "medium", "high", "xhigh", "max"]
LABEL_MAX = 40


def load_engine():
    """Import <this dir>/delegate.py as a module (it is not on sys.path)."""
    spec = importlib.util.spec_from_file_location("delegate_engine", DELEGATE)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def list_models():
    mod = load_engine()
    url, key = mod.load_cfg()
    ids = mod.refresh_models(url, key)
    if ids is None:
        print("error: could not list models from the gateway", file=sys.stderr)
        return 1
    for model_id in sorted(ids):
        print(model_id)
    return 0


def default_label(model):
    return ("ask-" + re.sub(r"[^A-Za-z0-9]+", "-", model))[:LABEL_MAX]


DEFAULT_MAX_ITERS = 30  # the engine's --max-iters default


def work_capacity(max_iters):
    """Ordinary work turns: max_iters minus the report (1) and closeout reserves."""
    return max_iters - 1 - min(2, max(0, max_iters - 2))


def build_argv(args, prompt_file):
    tools = getattr(args, "tools", False)
    argv = [sys.executable, "-u", DELEGATE] + ([] if tools else ["--oneshot"]) + [
        "-C", args.root, "-m", args.model, "--label", args.label]
    if args.effort:
        argv += ["-e", args.effort]
    if tools:
        # Bounded agent loop: read-only/staged unless --write is explicit.
        if getattr(args, "max_iters", None) is not None:
            argv += ["--max-iters", str(args.max_iters)]
        if getattr(args, "soft_iters", None) is not None:
            argv += ["--soft-iters", str(args.soft_iters)]
        if getattr(args, "write", False):
            argv += ["--write"]
    if getattr(args, "text_only", False):
        argv += ["--text-only"]
    if args.temperature is not None:
        argv += ["--temperature", str(args.temperature)]
    for path in args.files:
        argv += ["--attach", path]
    for path in getattr(args, "images", None) or []:
        argv += ["--attach-image", path]
    for name in getattr(args, "skills", None) or []:
        argv += ["--skill", name]
    for attr, flag in (("launch_receipt", "--launch-receipt"), ("launch_id", "--launch-id"),
                       ("context_policy", "--context-policy"),
                       ("context_window_tokens", "--context-window-tokens"),
                       ("context_reply_tokens", "--context-reply-tokens"),
                       ("context_safety_tokens", "--context-safety-tokens"),
                       ("compact_fraction", "--compact-fraction")):
        value = getattr(args, attr, None)
        if value is not None:
            argv += [flag, value]
    auto = getattr(args, "auto_compact", None)
    if auto is not None:  # explicit False is forwarded, never dropped
        argv += ["--auto-compact" if auto else "--no-auto-compact"]
    return argv + ["-f", prompt_file]


def parse_args(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("files", nargs="*", help="files to include as context")
    ap.add_argument("-p", "--prompt", help="the instruction")
    ap.add_argument("-m", "--model", default=os.environ.get("OPENWEBUI_MODEL"))
    # Left unset by default: reasoning models (gpt-5.6-*) reject any value but 1.
    ap.add_argument("--temperature", type=float)
    ap.add_argument("-e", "--effort", choices=EFFORTS,
                    help="reasoning effort; the engine maps it per model")
    ap.add_argument("--label", help="run label shown in the dashboard "
                                    "(default: ask-<model>)")
    ap.add_argument("-C", "--root", default=None,
                    help="project root the run is registered under (default: cwd)")
    ap.add_argument("--text-only", action="store_true",
                    help="forward engine-enforced image refusal")
    ap.add_argument("--image", dest="images", action="append", default=[],
                    metavar="FILE",
                    help="attach a PNG/JPEG/WebP image (repeatable)")
    ap.add_argument("--list-models", action="store_true",
                    help="print the gateway's model ids, one per line, and exit")
    ap.add_argument("--skill", dest="skills", action="append", default=[],
                    metavar="NAME", help="preload a skill's instructions (repeatable)")
    ap.add_argument("--tools", action="store_true",
                    help="run the bounded delegate tool loop instead of a tool-less "
                         "one-shot (MCP/skills per the repo policy)")
    ap.add_argument("--max-iters", dest="max_iters", type=int, default=None,
                    help="--tools only: hard total model-turn cap (engine default 30)")
    ap.add_argument("--soft-iters", dest="soft_iters", type=int, default=None,
                    help="--tools only: earlier work checkpoint; preauthorizes one "
                         "continuation inside --max-iters")
    ap.add_argument("--write", action="store_true",
                    help="--tools only: allow real writes in -C (and MCP mutation)")
    ap.add_argument("--launch-receipt", metavar="ABSOLUTE_PATH",
                    help="forward a unique startup receipt path to the engine")
    ap.add_argument("--launch-id", metavar="UUID",
                    help="forward the matching fresh launch identity to the engine")
    # Context options: forwarded verbatim only when given; the engine validates values.
    for flag in ("--context-policy", "--context-window-tokens", "--context-reply-tokens",
                 "--context-safety-tokens", "--compact-fraction"):
        ap.add_argument(flag, default=None)
    ap.add_argument("--auto-compact", default=None, action=argparse.BooleanOptionalAction,
                    help="--tools only: enable/disable automatic compaction (one-shot rejects it)")
    args = ap.parse_args(argv)
    if args.list_models:
        return args
    if bool(args.launch_receipt) != bool(args.launch_id):
        ap.error("--launch-receipt and --launch-id must be supplied together")
    if args.launch_receipt:
        if not os.path.isabs(args.launch_receipt):
            ap.error("--launch-receipt must be an absolute path")
        try:
            args.launch_id = str(uuid.UUID(args.launch_id))
        except ValueError:
            ap.error("--launch-id must be a UUID")
        if os.path.lexists(args.launch_receipt):
            ap.error("--launch-receipt must name a fresh unused path")
    if not args.tools and (args.max_iters is not None or args.soft_iters is not None
                           or args.write):
        ap.error("--max-iters/--soft-iters/--write need --tools")
    if not args.tools and args.auto_compact is True:  # one-shot is a single model request
        ap.error("compaction_unavailable_in_oneshot")
    if args.max_iters is not None and args.max_iters < 1:
        ap.error("--max-iters must be a positive integer")
    if args.soft_iters is not None:
        work = work_capacity(DEFAULT_MAX_ITERS if args.max_iters is None else args.max_iters)
        if not 1 <= args.soft_iters <= work:
            ap.error(f"--soft-iters must be 1..{work} (work turns left after the "
                     "report and closeout reserves)")
    if args.tools and (args.files or args.images or args.temperature is not None):
        ap.error("--tools runs an agent that reads files itself: drop file/--image "
                 "attachments and --temperature, and name the paths in -p instead")
    if not args.prompt:
        ap.error("-p/--prompt is required")
    if not args.model:
        ap.error("no model: pass -m or set OPENWEBUI_MODEL")
    if args.label is None:
        args.label = default_label(args.model)
    if args.root is None:
        args.root = os.getcwd()
    return args


def diagnostic_journal(path, **identity):
    try:
        module = runpy.run_path(os.path.join(os.path.dirname(__file__), "harness_diagnostics.py"))
        return module["Journal"](path, "ask", **identity), module["exception_fields"]
    except Exception:
        try:
            print("WARNING: lifecycle diagnostics unavailable", file=sys.stderr)
        except Exception:
            pass
        return SimpleNamespace(emit=lambda *a, **k: False, identity={}), lambda exc: {}


def main(argv=None):
    args = parse_args(argv)
    if args.list_models:
        return list_models()
    if args.launch_receipt is None:
        args.launch_id = str(uuid.uuid4())
        base = (os.path.join(os.path.realpath(args.root), ".claude", "agent-runs")
                if os.path.isdir(args.root) else tempfile.gettempdir())
        try:
            os.makedirs(base, exist_ok=True)
        except OSError:
            base = tempfile.gettempdir()
        args.launch_receipt = os.path.join(base, args.launch_id + ".launch.jsonl")
    journal, exception_fields = diagnostic_journal(args.launch_receipt + ".ask.diagnostics.jsonl",
                                                   launch_id=args.launch_id)
    journal.emit("observer_started", phase="startup")
    fd, prompt_file = tempfile.mkstemp(prefix="ask-model-", suffix=".txt")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as fh:
            fh.write(args.prompt)
        with subprocess.Popen(build_argv(args, prompt_file)) as child:
            journal.emit("child_start", child_pid=child.pid, _child=child)
            try:
                code = child.wait()
            except BaseException:
                # Match subprocess.run's exception cleanup, with durable intent first.
                journal.emit("stop_requested", reason="wrapper_exception", target_pid=child.pid)
                child.kill()
                child.wait()
                journal.emit("child_exit", child_pid=child.pid, exit_code=child.returncode)
                raise
            journal.emit("child_exit", child_pid=child.pid, exit_code=code)
            return code
    except BaseException as exc:
        journal.emit("exception", reason="wrapper_exception", **exception_fields(exc))
        raise
    finally:
        try:
            os.remove(prompt_file)
        except OSError:
            pass


if __name__ == "__main__":
    sys.exit(main())

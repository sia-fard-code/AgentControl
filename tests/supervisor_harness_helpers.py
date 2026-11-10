"""Synthetic installed-engine fixtures for harness adapter tests. No network, no live paths."""
import hashlib
import json
import subprocess
import sys

from supervisor_helpers import envelope_bytes, load

FAKE_ENGINE = r'''
import hashlib, json, os, subprocess, sys, time
here = os.path.dirname(os.path.abspath(__file__))
b = json.load(open(os.path.join(here, "behavior.json")))
a = sys.argv[1:]
if "--help" in a:
    print("usage: delegate.py" + (" [--supervisor-capabilities]" if b.get("help_flag") else ""))
    sys.exit(0)
if "--supervisor-capabilities" in a:
    print(json.dumps(b.get("caps")))
    sys.exit(0)
with open(os.path.join(here, "calls.jsonl"), "a") as fh:
    fh.write(json.dumps(a) + "\n")
opt = lambda f: a[a.index(f) + 1]
receipt, lid, tr = opt("--launch-receipt"), opt("--launch-id"), opt("--transcript")
run_id = os.path.basename(tr)[:-3]
idx = os.path.join(os.path.dirname(tr), "agent-runs-index.jsonl")
def line(path, rec):
    with open(path, "a") as fh:
        fh.write(json.dumps(rec) + "\n")
def rec(state, **kw):
    line(receipt, {"schema": 1, "launch_id": lid, "engine_pid": os.getpid(), "state": state,
                   "run_id": kw.pop("run_id", None), **kw})
rec("starting")
line(idx, {"schema": 2, "event": "start", "run_id": run_id, "engine_pid": os.getpid(),
           "root": os.path.realpath(opt("-C"))})
rec("ready", run_id=run_id, root=os.path.realpath(opt("-C")), transcript=os.path.realpath(tr),
    index_paths=[os.path.realpath(idx)] + b.get("extra_index", []))
if b.get("grandchild_seconds"):
    grandchild = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(%s)" % b["grandchild_seconds"]])
    open(os.path.splitext(__file__)[0] + ".grand.pid", "w").write(str(grandchild.pid))
time.sleep(b.get("sleep", 0))
if b.get("release_file"):
    until = time.monotonic() + 20
    while not os.path.exists(b["release_file"]) and time.monotonic() < until:
        time.sleep(.01)
if "--oneshot" in a:
    sys.stdout.write(b.get("plan", ""))
elif "worker_patch" in b:
    adir = tr[:-3] + ".artifacts"
    os.mkdir(adir)
    patch = b["worker_patch"].encode("utf-8")
    psha = hashlib.sha256(patch).hexdigest()
    pfile = os.path.join(adir, "patch-" + psha[:16] + ".diff")
    open(pfile, "wb").write(patch)
    text = open(opt("-f"), encoding="utf-8").read()
    binding = {"run_id": run_id, "task_sha256": hashlib.sha256(text.encode()).hexdigest(),
               "root_identity": os.path.normcase(os.path.realpath(opt("-C"))), "write_mode": "disk"}
    json.dump({"run_id": run_id, "run_binding": binding,
               "patch_binding": {"run": binding, "patch_sha256": psha, "patch_size": len(patch),
                                 "artifact_path": pfile},
               "verification_status": "unverified_post_edit_mutation"},
              open(os.path.join(adir, "completion.json"), "w"))
    sys.stdout.write(b.get("findings", ""))
if b.get("end", True):
    line(idx, {"schema": 2, "event": "end", "run_id": run_id, "status": b.get("status", "complete"),
               "tool_calls": 0, "refused": 0, "sent": 11, "recv": 7})
'''

PLANNER_MODEL = "gpt-6.1-sol"
WORKER_MODEL = "anthropic_tool_call.claude-opus-5-5-think"
TEST_ARGV = ["python", "-m", "pytest"]


def new_caps():
    return load("supervisor_policy").capabilities()


def make_engine(tmp_path, **behavior):
    d = tmp_path / "installed"
    d.mkdir(exist_ok=True)
    behavior.setdefault("help_flag", True)
    behavior.setdefault("caps", new_caps())
    (d / "behavior.json").write_text(json.dumps(behavior))
    (d / "delegate.py").write_text(FAKE_ENGINE)
    return d / "delegate.py"


def calls(engine):
    p = engine.parent / "calls.jsonl"
    return [json.loads(x) for x in p.read_text().splitlines()] if p.exists() else []


def make_repo(tmp_path):
    root = tmp_path / "repo"
    (root / ".claude").mkdir(parents=True)
    policy = root / ".claude" / "agent-policy.json"
    policy.write_text(json.dumps({"allowed_cmds": [TEST_ARGV], "skip_dirs": []}))
    git = ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid"]
    subprocess.run(git + ["init", "-q"], cwd=root, check=True)
    (root / "README.txt").write_text("synthetic\n")
    subprocess.run(git + ["add", "README.txt"], cwd=root, check=True)
    subprocess.run(git + ["commit", "-q", "-m", "init"], cwd=root, check=True)
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, check=True,
                          capture_output=True, text=True).stdout.strip()
    return root, head, hashlib.sha256(policy.read_bytes()).hexdigest()


def make_envelope(tmp_path, **overrides):
    """(envelope, repo root) bound to a real temporary repo and policy digest."""
    harness = load("supervisor_harness")
    root, head, policy = make_repo(tmp_path)
    fields = {"models": [PLANNER_MODEL, WORKER_MODEL], "baseline_commit": head,
              "policy_digest": policy,
              "capabilities": {"write": ["src/"], "commands": [harness.command_id(TEST_ARGV)],
                               "mcp": []}}
    fields.update(overrides)
    raw, approved = envelope_bytes(root, **fields)
    return load("supervisor_contract").load_envelope(raw, approved, root), root


def operator_caps():
    return {"write_paths": ["src/"], "commands": [list(TEST_ARGV)], "mcp": "none"}


PYTHON = sys.executable

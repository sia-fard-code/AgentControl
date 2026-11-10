"""Synthetic fixtures + transport-only fake adapter for driver tests. No model/network/live paths."""
import hashlib
import json
import re
from pathlib import Path
from types import SimpleNamespace

from supervisor_helpers import envelope_bytes, load
from supervisor_evidence_helpers import diff_for, git, make_registry, make_repo, write_artifacts

contract = load("supervisor_contract")
evidence = load("supervisor_evidence")

GOAL = "Synthetic goal: create src/a.txt and src/b.txt so the protected checks pass."
CAPS = {"write_paths": ["src/"], "commands": [], "mcp": "none"}
CRITERIA, CHECKS = ["C1", "C2", "CI"], ["chk-a", "chk-b", "chk-ci"]
FILE_SCRIPT = ("import pathlib, sys\n"
               "sys.exit(0 if (pathlib.Path(sys.argv[1]) / 'src' / {name!r}).is_file() else 1)\n")
CI_SCRIPT = ("import pathlib, sys\nt = pathlib.Path(sys.argv[1]) / 'src'\n"
             "if not ((t / 'a.txt').is_file() and (t / 'b.txt').is_file()):\n"
             "    print('missing'); sys.exit(1)\n"
             "if (t / 'b.txt').read_text() != {want!r}:\n    print('b_stale'); sys.exit(1)\n")


def sha(data):
    return hashlib.sha256(data).hexdigest()


def setup(tmp_path, ci_wants="B"):
    """Temp repo, protected validators outside it, and an operator-approved envelope."""
    supervise = load("supervise")
    proj = tmp_path / "proj"
    base = make_repo(proj, {"README.md": b"synthetic\n"})
    reg = make_registry(tmp_path / "validators", [
        ("chk-a", ["C1"], FILE_SCRIPT.format(name="a.txt"), [], None, 60),
        ("chk-b", ["C2"], FILE_SCRIPT.format(name="b.txt"), [], None, 60),
        ("chk-ci", ["CI"], CI_SCRIPT.format(want=ci_wants), ["missing", "b_stale"], None, 60)])
    over = {"baseline_commit": base, "goal_digest": sha(GOAL.encode()), "validator_digest": sha(reg),
            "criteria": CRITERIA, "check_ids": CHECKS, "hypothesis_ids": [],
            "capabilities": {"write": ["src/"], "commands": [], "mcp": []}}
    over["acceptance_digest"] = supervise.acceptance_binding(CRITERIA, CHECKS, [], sha(reg))
    raw, approved = envelope_bytes(proj, **over)
    return SimpleNamespace(project=proj, baseline=base, registry=reg, raw=raw, approved=approved,
                           envelope=contract.load_envelope(raw, approved, proj),
                           state=tmp_path / "state", artifacts=tmp_path / "artifacts")


def task(task_id, objective, criteria, depends_on=()):
    return {"task_id": task_id, "depends_on": list(depends_on), "objective": objective,
            "criteria": list(criteria), "hypothesis_ids": []}


def two_worker_planner(brief, n):
    return json.dumps({"plan_revision": n, "tasks": [
        task("W1", "write src/a.txt A", ["C1"]), task("W2", "write src/b.txt B", ["C2"])]})


def correcting_planner(brief, n):
    """Initial plan; a replan only proposes the correction if the trusted failure fact is visible."""
    if n == 0:
        return two_worker_planner(brief, n)
    if "chk-ci" not in brief or "b_stale" not in brief:
        return "{not json"
    return json.dumps({"plan_revision": n, "tasks": [task("W2b", "write src/b.txt B2", ["C2", "CI"])]})


RUNNING = {"state": "RUNNING", "outcome": None, "quiescent": False, "usage": None, "reason": None}
DONE = {"state": "TERMINAL", "outcome": "succeeded", "quiescent": True, "usage": 10, "reason": None}


class FakeAdapter:
    """Transport stand-in. Planner/worker 'models' are tiny functions; everything else is real."""

    def __init__(self, envelope, artifact_root, planner):
        self.envelope, self.artifact_root, self.planner = envelope, Path(artifact_root), planner
        self.launches, self.briefs, self._polls, self._replies, self._expected = [], [], {}, {}, {}

    def preflight(self):
        return {"protocol": "fake", "version": 1}

    def launch(self, effect, brief, *, on_process, owner_guard):
        assert owner_guard() is True
        d = effect.data
        key = d.get("attempt_id") or d["call_id"]
        on_process(key, d["launch_id"], 4000 + len(self.launches), "synthetic-birth")
        n = sum(k == "call_planner" for k, _ in self.launches)
        self.launches.append((effect.kind, key))
        self.briefs.append((effect.kind, brief))
        if effect.kind == "call_planner":
            self._replies[d["launch_id"]] = self.planner(brief, n)
        else:
            self._work(d, brief)
        return {"launch_id": d["launch_id"]}

    def _work(self, d, brief):
        path, text = re.search(r"^write (\S+) (\S+)$", brief, re.M).groups()
        wt = self.artifact_root / d["worktree_ref"]
        git(self.artifact_root, "clone", "-q", str(self.envelope.project_root), str(wt))
        patch = diff_for(wt, {path: text.encode()})
        exp = {"run_id": d["run_id"], "task_sha256": sha(brief.encode()), "root_identity": str(wt),
               "baseline_commit": self.envelope.baseline_commit, "patch_ref": "agent.patch",
               "findings_ref": "findings.md"}
        write_artifacts(self.artifact_root / d["artifact_ref"], d["run_id"], patch, str(wt),
                        exp["task_sha256"], findings="synthetic finding canary-7f3a")
        self._expected[d["launch_id"]] = exp

    def observe(self, identity):
        n = self._polls[identity["launch_id"]] = self._polls.get(identity["launch_id"], 0) + 1
        return dict(RUNNING if n == 1 else DONE)

    def read_plan(self, identity, revision):
        data = json.loads(self._replies[identity["launch_id"]])
        tasks, objs = [], {}
        for t in data["tasks"]:
            ref = f"runs/{identity['run_id']}/objectives/{t['task_id']}.txt"
            (self.artifact_root / ref).parent.mkdir(parents=True, exist_ok=True)
            (self.artifact_root / ref).write_text(t["objective"], encoding="utf-8")
            objs[t["task_id"]] = {"objective_ref": ref, "objective_digest": sha(t["objective"].encode())}
            tasks.append({**{k: t[k] for k in ("task_id", "depends_on", "criteria", "hypothesis_ids")},
                          "objective_digest": objs[t["task_id"]]["objective_digest"]})
        return contract.canonical({"schema_version": 1, "plan_revision": data["plan_revision"],
                                   "tasks": tasks}), objs

    def objective(self, identity, t):
        text = (self.artifact_root / f"runs/{identity['run_id']}/objectives/{t.task_id}.txt").read_text(
            encoding="utf-8")
        assert sha(text.encode()) == t.objective_digest
        return text

    def read_worker(self, identity):
        return evidence.collect_result(self.artifact_root / identity["artifact_ref"],
                                       self._expected[identity["launch_id"]], self.envelope.baseline_commit)

    def cancel(self, identity, owner_guard, *, timeout=5):
        return {"result": "not_authorized"}

    def close(self):
        pass

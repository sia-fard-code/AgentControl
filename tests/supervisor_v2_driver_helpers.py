"""Synthetic v2 driver fixtures: N-file goals, protected validators, barrier transport. Temp-only."""
import json
from types import SimpleNamespace

from supervisor_helpers import load
from supervisor_v2_helpers import envelope_bytes_v2
from supervisor_evidence_helpers import make_registry, make_repo
from supervisor_driver_helpers import FakeAdapter, sha, task, RUNNING, DONE

contract = load("supervisor_contract")

ALL_SCRIPT = ("import pathlib, sys\nt = pathlib.Path(sys.argv[1]) / 'src'\nwant = {want!r}\n"
              "for name, text in want.items():\n"
              "    p = t / name\n"
              "    if not p.is_file():\n        print('missing'); sys.exit(1)\n"
              "    if p.read_text() != text:\n        print('wrong'); sys.exit(1)\n")


def goal_text(n):
    return f"Synthetic v2 goal: create {n} files src/f0.txt..src/f{n - 1}.txt with exact content."


def setup_n(tmp_path, n, limits=None, want=None, **overrides):
    """Temp repo + one pinned CI validator reading all n outputs + approved explicit v2 envelope."""
    supervise = load("supervise")
    want = want or {f"f{i}.txt": f"V{i}" for i in range(n)}
    proj = tmp_path / "proj"
    base = make_repo(proj, {"README.md": b"synthetic\n"})
    crit = [f"C{i}" for i in range(n)] + ["CI"]
    reg = make_registry(tmp_path / "validators",
                        [("chk-all", crit, ALL_SCRIPT.format(want=want), ["missing", "wrong"], None, 60)])
    goal = goal_text(n)
    over = {"baseline_commit": base, "goal_digest": sha(goal.encode()), "validator_digest": sha(reg),
            "criteria": crit, "check_ids": ["chk-all"], "hypothesis_ids": [],
            "capabilities": {"write": ["src/"], "commands": [], "mcp": []},
            "acceptance_digest": supervise.acceptance_binding(crit, ["chk-all"], [], sha(reg))}
    over.update(overrides)
    raw, approved = envelope_bytes_v2(proj, limits, **over)
    return SimpleNamespace(project=proj, baseline=base, registry=reg, raw=raw, approved=approved,
                           envelope=contract.load_envelope(raw, approved, proj), goal=goal, n=n,
                           want=want, state=tmp_path / "state", artifacts=tmp_path / "artifacts")


def fanout_planner(n, content=None):
    """Planner 'model': one revision with n independent write tasks."""
    def plan(brief, rev):
        return json.dumps({"plan_revision": rev, "tasks": [
            task(f"W{i}", f"write src/f{i}.txt {(content or {}).get(i, f'V{i}')}", [f"C{i}", "CI"])
            for i in range(n)]})
    return plan


def advancing_want(final):
    """Target f0 must reach R<final>; every revision's retained side file n<r> must survive."""
    return {"f0.txt": f"R{final}", **{f"n{r}.txt": f"N{r}" for r in range(final + 1)}}


def advancing_planner(final):
    """Revision r: replace f0 with R<r> and add independent n<r>. Replans only act on the trusted
    protected failure fact being visible in the brief (otherwise an unusable reply)."""
    def plan(brief, rev):
        if rev > final + 2:
            raise AssertionError("advancing synthetic goal lost retained work instead of converging")
        if rev and ("chk-all" not in brief or "wrong" not in brief and "missing" not in brief):
            return "{not json"
        return json.dumps({"plan_revision": rev, "tasks": [
            task(f"T{rev}", f"write src/f0.txt R{min(rev, final)}", ["C0", "CI"]),
            task(f"S{rev}", f"write src/n{rev}.txt N{rev}", ["CI"])]})
    return plan


def journal_records(state_dir, kind=None):
    """Read committed journal lines across segments (journal.log first, then numbered segments)."""
    files = sorted(p for p in state_dir.rglob("journal*") if p.is_file())
    recs = [json.loads(line) for p in files for line in p.read_bytes().splitlines() if line.strip()]
    recs.sort(key=lambda r: r["seq"])
    return [r for r in recs if kind is None or r["kind"] == kind]


def stalled_then_changed_planner():
    """rev0 writes wrong f0; rev1 proposes the SAME wrong content (no novelty possible); rev2+ only
    changes strategy once the brief honestly reports the stall, then writes correct content."""
    def plan(brief, rev):
        text = "WRONG" if rev < 2 else "V0"
        if rev >= 2 and not stalled_brief(brief):
            text = "WRONG"  # without an honest stall report the 'model' keeps repeating itself
        return json.dumps({"plan_revision": rev, "tasks": [
            task(f"T{rev}", f"write src/f0.txt {text}", ["C0", "CI"])]})
    return plan


def stalled_brief(brief):
    low = brief.lower()
    return any(w in low for w in ("no new evidence", "no progress", "stalled", "diagnos"))


CONTEXT_FAIL = {"state": "TERMINAL", "outcome": "failed", "quiescent": True, "usage": 10,
                "reason": "context_budget"}


class BarrierAdapter(FakeAdapter):
    """Workers stay RUNNING until `barrier` workers are simultaneously live (then the barrier opens
    for good). live/peak are measured from real launch/observe traffic, never preset."""

    POLL_CAP = 400  # shortcut: bounded hold so a non-admitting driver fails the peak oracle, not hangs

    def __init__(self, envelope, artifact_root, planner, barrier):
        super().__init__(envelope, artifact_root, planner)
        self.barrier, self.open = barrier, False
        self.live, self.peak, self.worker_launches, self.trace = set(), 0, 0, []

    def launch(self, effect, brief, *, on_process, owner_guard):
        out = super().launch(effect, brief, on_process=on_process, owner_guard=owner_guard)
        if effect.kind == "launch_worker":
            self.worker_launches += 1
            self.live.add(effect.data["launch_id"])
            self.peak = max(self.peak, len(self.live))
            self.trace.append(("launch", len(self.live)))
            if len(self.live) >= self.barrier:
                self.open = True
        return out

    def observe(self, identity):
        lid = identity["launch_id"]
        n = self._polls[lid] = self._polls.get(lid, 0) + 1
        if lid not in self.live:
            return dict(RUNNING if n == 1 else DONE)  # planner calls
        if not self.open and n < self.POLL_CAP:
            return dict(RUNNING)
        self.live.discard(lid)
        self.trace.append(("done", len(self.live)))
        return dict(DONE)


class ContextLimitedAdapter(BarrierAdapter):
    """First worker exhausts its context: terminal failed/context_budget, quiescent, with a safely
    collected bound partial patch. Later (fresh identity) workers complete normally."""

    def __init__(self, *a, **kw):
        super().__init__(*a, barrier=1, **kw)
        self.context_launch = None

    def launch(self, effect, brief, *, on_process, owner_guard):
        out = super().launch(effect, brief, on_process=on_process, owner_guard=owner_guard)
        if effect.kind == "launch_worker" and self.context_launch is None:
            self.context_launch = effect.data["launch_id"]
        return out

    def observe(self, identity):
        status = super().observe(identity)
        if identity["launch_id"] == self.context_launch and status["state"] == "TERMINAL":
            return dict(CONTEXT_FAIL)
        return status


def continuation_planner(brief, rev):
    """rev0 asks for partial work (PART); a replan after the context-limited result asks again."""
    text = "PART" if rev == 0 else "V0"
    return json.dumps({"plan_revision": rev, "tasks": [
        task(f"T{rev}", f"write src/f0.txt {text}", ["C0", "CI"])]})

"""Installed-harness adapter for the goal supervisor (protocol v1).

Preflight is effect-free. Runtime methods consume already durable core admissions,
contain the installed engine before execution, and expose only bound metadata.
"""
import hashlib
import fnmatch
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import supervisor_contract as contract
from supervisor_process import ProcessError, WindowsJob
from supervisor_policy import git_environment

PLANNER_MODEL = "gpt-6.1-sol"
WORKER_MODEL = "anthropic_tool_call.claude-opus-5-5-think"
POLICY_FILE = os.path.join(".claude", "agent-policy.json")
MAX_PROBE_BYTES = 64 * 1024
PROBE_TIMEOUT = 30
REQUIRED_FEATURES = ("manifest_sha256_pin", "policy_sha256_binding", "baseline_commit_binding",
                      "launch_run_identity_binding", "write_path_boundary",
                      "exact_command_argv", "owned_worktree_creation", "single_request_planner")
CODES = frozenset({
    "capabilities_mismatch", "model_not_permitted", "policy_mismatch", "baseline_mismatch",
    "containment_unavailable", "engine_missing", "engine_unsupported", "engine_changed",
    "invalid_identity", "artifact_collision", "launch_failed", "owner_lost", "deadline",
    "result_unavailable", "invalid_plan", "objective_changed", "permission_denied",
})
MAX_META_BYTES = 1 << 20
MAX_PLAN_BYTES = 128 * 1024
MAX_BRIEF_CHARS = 128000
ENGINE_STOP_REASONS = frozenset({"max_iters", "repair_limit", "no_action_limit",
                                 "mutation_outcome_unknown", "context_budget"})


def _bytes(path, limit=MAX_META_BYTES):
    try:
        with open(path, "rb") as stream:
            data = stream.read(limit + 1)
    except OSError:
        raise HarnessError("result_unavailable") from None
    if len(data) > limit:
        raise HarnessError("result_unavailable")
    return data


def _json(path):
    try:
        return contract.strict_json(_bytes(path))
    except contract.ContractError:
        raise HarnessError("result_unavailable") from None


def _rows(path):
    try:
        return [contract.strict_json(line) for line in _bytes(path).splitlines() if line]
    except contract.ContractError:
        raise HarnessError("result_unavailable") from None


def _write_once(path, data):
    path = Path(path)
    try:
        with path.open("xb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
    except FileExistsError:
        if _bytes(path, max(len(data), MAX_META_BYTES)) != data:
            raise HarnessError("artifact_collision") from None


def _role(identity):
    if "attempt_id" in identity and "call_id" not in identity:
        return "worker", identity["attempt_id"]
    if "call_id" in identity and identity.get("role") in ("planner", "report"):
        return identity["role"], identity["call_id"]
    raise HarnessError("invalid_identity")


class HarnessError(Exception):
    """Adapter refusal with a fixed code; never carries engine output or model text."""

    def __init__(self, code):
        self.code = code if code in CODES else "engine_unsupported"
        super().__init__(self.code)


def command_id(argv):
    """Deterministic short identifier for one exact argv (envelope capability item)."""
    return "cmd-" + hashlib.sha256(contract.canonical(list(argv))).hexdigest()[:16]


def _sha256(path):
    try:
        with open(path, "rb") as fh:
            return hashlib.sha256(fh.read()).hexdigest()
    except OSError:
        raise HarnessError("engine_missing") from None


class HarnessAdapter:
    def __init__(self, envelope, artifact_root, *, capabilities, engine_path=None,
                 python_executable=None, clock=None, job_factory=None):
        self.envelope = envelope
        self.artifact_root = os.path.realpath(os.fspath(artifact_root))
        self.engine_path = os.path.realpath(os.fspath(
            engine_path or os.path.join(os.path.expanduser("~"), ".claude", "bin", "delegate.py")))
        self.python = python_executable or sys.executable
        self.clock = clock
        self.job_factory = job_factory
        self.capabilities = self._narrow(capabilities)
        self.engine_sha256 = None
        self._runs = {}

    def _narrow(self, caps):
        """Operator capabilities must lie inside the approved envelope; no other source."""
        approved = self.envelope.capabilities
        try:
            ok = (isinstance(caps, dict) and set(caps) == {"write_paths", "commands", "mcp"}
                  and caps["mcp"] == "none" and not approved["mcp"]
                  and isinstance(caps["write_paths"], list)
                  and set(caps["write_paths"]) <= set(approved["write"])
                  and isinstance(caps["commands"], list)
                  and all(isinstance(c, list) and c and all(isinstance(a, str) and a for a in c)
                          for c in caps["commands"])
                  and {command_id(c) for c in caps["commands"]} <= set(approved["commands"]))
        except (TypeError, KeyError):
            ok = False
        if not ok:
            raise HarnessError("capabilities_mismatch")
        return {"write_paths": tuple(caps["write_paths"]),
                "commands": tuple(tuple(c) for c in caps["commands"]), "mcp": "none"}

    def _probe(self, flag):
        try:
            r = subprocess.run([self.python, self.engine_path, flag], capture_output=True,
                               timeout=PROBE_TIMEOUT, stdin=subprocess.DEVNULL)
        except (OSError, subprocess.TimeoutExpired):
            raise HarnessError("engine_unsupported") from None
        if r.returncode != 0 or len(r.stdout) > MAX_PROBE_BYTES:
            raise HarnessError("engine_unsupported")
        return r.stdout

    def preflight(self):
        env = self.envelope
        if not {PLANNER_MODEL, WORKER_MODEL} <= set(env.models):
            raise HarnessError("model_not_permitted")
        root = os.path.realpath(env.project_root)
        policy = os.path.join(root, POLICY_FILE)
        if not os.path.isfile(policy) or _sha256(policy) != env.policy_digest:
            raise HarnessError("policy_mismatch")
        try:
            rules = _json(policy)["allowed_cmds"]
            if not isinstance(rules, list) or not rules or not all(isinstance(r, list) and r
                    and all(isinstance(a, str) and a for a in r) for r in rules):
                raise ValueError
            if any(not any(len(argv) >= len(r) and all(fnmatch.fnmatch(argv[i], a)
                       for i, a in enumerate(r)) for r in rules)
                   for argv in self.capabilities["commands"]):
                raise HarnessError("capabilities_mismatch")
        except (ValueError, KeyError, TypeError):
            raise HarnessError("policy_mismatch") from None
        try:
            r = subprocess.run(["git", "-c", "core.hooksPath=", "-c", "core.fsmonitor=false",
                                "rev-parse", "--verify", "--quiet", env.baseline_commit + "^{commit}"],
                               cwd=root, env=git_environment(), capture_output=True, text=True,
                               timeout=30, stdin=subprocess.DEVNULL)
        except (OSError, subprocess.TimeoutExpired):
            raise HarnessError("baseline_mismatch") from None
        if r.returncode != 0 or r.stdout.strip() != env.baseline_commit:
            raise HarnessError("baseline_mismatch")
        if sys.platform != "win32":
            raise HarnessError("containment_unavailable")
        digest = _sha256(self.engine_path)
        if b"--supervisor-capabilities" not in self._probe("--help"):
            raise HarnessError("engine_unsupported")
        try:
            caps = contract.strict_json(self._probe("--supervisor-capabilities"))
            feats = caps["features"]
            ok = (caps["protocol"] == "agentcontrol.supervisor" and type(caps["version"]) is int
                  and caps["version"] == 1
                  and caps["manifest_schema"] == "agentcontrol.supervisor-manifest/v1"
                  and all(feats[f] is True for f in REQUIRED_FEATURES) and feats["mcp"] == "none")
        except (contract.ContractError, TypeError, KeyError):
            ok = False
        if not ok:
            raise HarnessError("engine_unsupported")
        if _sha256(self.engine_path) != digest:
            raise HarnessError("engine_changed")
        self.engine_sha256 = digest
        return {"protocol": caps["protocol"], "version": 1, "engine_sha256": digest}

    def check_engine(self):
        """Reject any engine change between preflight and launch."""
        if self.engine_sha256 is None or _sha256(self.engine_path) != self.engine_sha256:
            raise HarnessError("engine_changed")
        return self.engine_sha256

    def _paths(self, identity):
        try:
            run_id, launch_id = identity["run_id"], identity["launch_id"]
            _role(identity)
            import uuid
            if str(uuid.UUID(launch_id)) != launch_id:
                raise ValueError
            prefix = f"runs/{run_id}"
            expected = {"transcript_ref": f"{prefix}/{run_id}.md",
                        "receipt_ref": f"{prefix}/{launch_id}.receipt.json",
                        "artifact_ref": f"{prefix}/artifacts"}
            layout = hashlib.sha256(contract.canonical({
                "artifact_root": os.path.normcase(self.artifact_root)})).hexdigest()
            if identity.get("layout") != layout or identity.get("job_name") != "AgentControl-" + launch_id \
                    or any(identity.get(k) != value for k, value in expected.items()):
                raise ValueError
            owned = {k: contract.resolve_owned(self.artifact_root, ref) for k, ref in expected.items()}
            directory = owned["transcript_ref"].parent
            return {"run": directory, "transcript": owned["transcript_ref"],
                    "receipt": owned["receipt_ref"], "artifacts": owned["artifact_ref"],
                    "engine_artifacts": directory / (run_id + ".artifacts"),
                    "brief": directory / "brief.txt", "manifest": directory / "manifest.json",
                    "packet": directory / "controller.json", "stdout": directory / "stdout.txt",
                    "stderr": directory / "stderr.txt", "patch": directory / "worker.patch",
                    "index": directory / "agent-runs-index.jsonl"}
        except (KeyError, TypeError, ValueError, contract.ContractError):
            raise HarnessError("invalid_identity") from None

    def _packet(self, identity):
        paths = self._paths(identity)
        role, effect_id = _role(identity)
        packet = _json(paths["packet"])
        if not isinstance(packet, dict) or set(packet) != {
                "schema", "envelope_digest", "engine_sha256", "brief_sha256",
                "launch_id", "run_id", "effect_id", "role"} or type(packet.get("schema")) is not int \
                or packet["schema"] != 1 \
                or any(packet.get(k) != v for k, v in {
                    "envelope_digest": self.envelope.digest, "engine_sha256": self.engine_sha256,
                    "launch_id": identity["launch_id"], "run_id": identity["run_id"],
                    "effect_id": effect_id, "role": role}.items()) \
                or packet.get("brief_sha256") != hashlib.sha256(_bytes(paths["brief"])).hexdigest():
            raise HarnessError("invalid_identity")
        return packet, paths

    def launch(self, effect, brief, *, on_process, owner_guard):
        """Consume a durable effect once. Pin the suspended process before any engine code."""
        self.check_engine()
        identity = effect.data
        role, effect_id = _role(identity)
        if not owner_guard():
            raise HarnessError("owner_lost")
        if contract.deadline_passed(self.envelope, (self.clock or time.time)()):
            raise HarnessError("deadline")
        if not isinstance(brief, str) or len(brief) > MAX_BRIEF_CHARS:
            raise HarnessError("invalid_plan")
        paths = self._paths(identity)
        try:
            paths["run"].mkdir(parents=True, exist_ok=False)
        except FileExistsError:
            raise HarnessError("artifact_collision") from None
        brief = brief.replace("\r\n", "\n").replace("\r", "\n")
        raw = brief.encode("utf-8")
        _write_once(paths["brief"], raw)
        worker = role == "worker"
        manifest = {
            "schema": "agentcontrol.supervisor-manifest/v1", "root": self.envelope.project_root,
            "policy_sha256": self.envelope.policy_digest, "baseline_commit": self.envelope.baseline_commit,
            "orchestration_id": self.envelope.orchestration_id, "attempt_id": effect_id,
            "launch_id": identity["launch_id"], "run_id": identity["run_id"],
            "role": "worker" if worker else "planner", "mcp": "none",
            "write_paths": list(self.capabilities["write_paths"]) if worker else [],
            "commands": [list(c) for c in self.capabilities["commands"]] if worker else [],
            "max_iters": min(200, self.envelope.worker_turn_limit) if worker else 1,
        }
        manifest_raw = contract.canonical(manifest)
        _write_once(paths["manifest"], manifest_raw)
        _write_once(paths["packet"], contract.canonical({
            "schema": 1, "envelope_digest": self.envelope.digest, "engine_sha256": self.engine_sha256,
            "brief_sha256": hashlib.sha256(raw).hexdigest(), "launch_id": identity["launch_id"],
            "run_id": identity["run_id"], "effect_id": effect_id, "role": role}))
        argv = [self.python, "-u", self.engine_path, "-C", self.envelope.project_root,
                "-m", WORKER_MODEL if worker else PLANNER_MODEL,
                "-e", "medium" if worker else "high" if role == "report" else "max",
                "--skill", "ponytail", "--text-only", "--label", effect_id,
                "-f", str(paths["brief"]), "--transcript", str(paths["transcript"]),
                "--launch-id", identity["launch_id"], "--launch-receipt", str(paths["receipt"]),
                "--supervisor-manifest", str(paths["manifest"]),
                "--supervisor-manifest-sha256", hashlib.sha256(manifest_raw).hexdigest()]
        argv += (["--worktree", "--max-iters", str(manifest["max_iters"]), "--tool-mode", "native",
                  "--patch", str(paths["patch"])] if worker else ["--oneshot"])
        job, child, resumed = None, None, False
        try:
            job = (self.job_factory or WindowsJob).create(identity["job_name"])
            env = {k: v for k, v in os.environ.items() if not k.upper().startswith("GIT_")}
            env["_HARNESS_APPROVED_WORKER"] = "1"
            child = job.create_suspended(argv, cwd=self.envelope.project_root, env=env,
                                         stdout_path=paths["stdout"], stderr_path=paths["stderr"])
            process = {"pid": child.pid, "birth": "winft:" + str(child.birth)}
            self._runs[identity["launch_id"]] = {"job": job, "child": child, "process": process}
            on_process(effect_id, identity["launch_id"], process["pid"], process["birth"])
            self.check_engine()
            if not owner_guard():
                raise HarnessError("owner_lost")
            if contract.deadline_passed(self.envelope, (self.clock or time.time)()):
                raise HarnessError("deadline")
            child.resume()
            resumed = True
            return {"launch_id": identity["launch_id"], **process}
        except Exception as exc:
            if not resumed and job is not None and owner_guard():
                try:
                    job.terminate(owner_guard, timeout=5)
                except ProcessError:
                    pass
            if isinstance(exc, HarnessError):
                raise
            raise HarnessError("launch_failed") from None

    def _job(self, identity):
        running = self._runs.get(identity["launch_id"])
        if running is not None:
            if identity.get("process") is not None and identity["process"] != running["process"]:
                raise HarnessError("invalid_identity")
            return running["job"], running.get("process")
        process = identity.get("process")
        if not isinstance(process, dict) or type(process.get("pid")) is not int \
                or process["pid"] <= 0 or not isinstance(process.get("birth"), str):
            raise HarnessError("invalid_identity")
        job = (self.job_factory or WindowsJob).open_existing(identity["job_name"])
        try:
            birth = process["birth"]
            if not birth.startswith("winft:") or not job.contains(process["pid"], int(birth[6:])):
                raise HarnessError("invalid_identity")
        except (ValueError, HarnessError, ProcessError):
            job.close()
            raise HarnessError("invalid_identity") from None
        self._runs[identity["launch_id"]] = {"job": job, "child": None, "process": process}
        return job, process

    def observe(self, identity):
        """Read only exact run metadata and the retained/reopened owned Job, never global history."""
        unknown = {"state": "UNKNOWN", "outcome": None, "quiescent": False,
                   "usage": None, "reported_usage": None, "reason": "result_unavailable"}
        try:
            _, paths = self._packet(identity)
            job, process = self._job(identity)
            quiet = job.is_empty()
            receipts = _rows(paths["receipt"])
            if any(not isinstance(r, dict) or type(r.get("schema")) is not int or r["schema"] != 1
                   or r.get("launch_id") != identity["launch_id"]
                   or r.get("engine_pid") != process["pid"] for r in receipts):
                return unknown
            ready = [r for r in receipts if r.get("state") == "ready"]
            if len(ready) != 1:
                return unknown
            rec = ready[0]
            expected_index = os.path.normcase(os.path.realpath(paths["index"]))
            if rec.get("run_id") != identity["run_id"] \
                    or os.path.normcase(os.path.realpath(rec.get("root", ""))) != os.path.normcase(
                        os.path.realpath(self.envelope.project_root)) \
                    or os.path.normcase(os.path.realpath(rec.get("transcript", ""))) != os.path.normcase(
                        os.path.realpath(paths["transcript"])) \
                    or not isinstance(rec.get("index_paths"), list) \
                    or expected_index not in [os.path.normcase(os.path.realpath(p))
                                               for p in rec["index_paths"] if isinstance(p, str)]:
                return unknown
            events = _rows(paths["index"])
            if any(not isinstance(r, dict) or type(r.get("schema")) is not int
                   or r["schema"] not in (1, 2) or r.get("run_id") != identity["run_id"] for r in events):
                return unknown
            starts = [r for r in events if r.get("event") == "start"]
            ends = [r for r in events if r.get("event") == "end"]
            if len(starts) != 1 or starts[0].get("engine_pid") != process["pid"] \
                    or os.path.normcase(os.path.realpath(starts[0].get("root", ""))) != os.path.normcase(
                        os.path.realpath(self.envelope.project_root)) or len(ends) > 1:
                return unknown
            if not quiet:
                return {**unknown, "state": "RUNNING", "reason": None}
            if len(ends) != 1 or ends[0].get("stop_reason") == "mutation_outcome_unknown":
                return unknown
            end = ends[0]
            if end.get("status") not in ("complete", "failed", "incomplete") \
                    or end.get("stop_reason") not in ENGINE_STOP_REASONS | {None}:
                return unknown
            reported = (end["sent"] + end["recv"] if all(type(end.get(k)) is int and end[k] >= 0
                                                          for k in ("sent", "recv")) else None)
            return {"state": "TERMINAL", "outcome": "succeeded" if end.get("status") == "complete"
                    and not end.get("stop_reason") else "failed", "quiescent": True,
                    "usage": None, "reported_usage": reported, "reason": end.get("stop_reason")}
        except (HarnessError, ProcessError, OSError, ValueError, TypeError):
            return unknown

    def read_plan(self, identity, revision):
        """Decode run-local prose once; only computed objective hashes enter the core plan."""
        _, paths = self._packet(identity)
        try:
            plan_bytes = (contract.MAX_PROPOSAL_BYTES_V2 if self.envelope.schema_version == 2
                          else MAX_PLAN_BYTES)
            raw = _bytes(paths["stdout"], plan_bytes)
            body = (contract.strict_json(raw, max_bytes=plan_bytes) if self.envelope.schema_version == 2
                    else contract.strict_json(raw))
            if not isinstance(body, dict) or set(body) != {"plan_revision", "tasks"} \
                    or type(body["plan_revision"]) is not int or body["plan_revision"] != revision \
                    or not isinstance(body["tasks"], list) \
                    or not 1 <= len(body["tasks"]) <= contract.proposal_task_limit(self.envelope):
                raise ValueError
            tasks, objectives = [], {}
            fields = {"task_id", "depends_on", "objective", "criteria", "hypothesis_ids"}
            for task in body["tasks"]:
                if not isinstance(task, dict) or set(task) != fields \
                        or not isinstance(task["objective"], str) or not 1 <= len(task["objective"]) <= 4000:
                    raise ValueError
                text = task["objective"].replace("\r\n", "\n").replace("\r", "\n")
                digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
                tasks.append({k: v for k, v in task.items() if k != "objective"})
                tasks[-1]["objective_digest"] = digest
                objectives[task["task_id"]] = text
            proposal = contract.canonical({"schema_version": 1, "plan_revision": revision, "tasks": tasks})
            plan = contract.validate_proposal(proposal, self.envelope, revision)
            directory = paths["run"] / "objectives"
            directory.mkdir(exist_ok=True)
            bindings = {}
            for task in plan.tasks:
                path = contract.resolve_owned(directory, task.task_id + ".txt")
                _write_once(path, objectives[task.task_id].encode("utf-8"))
                bindings[task.task_id] = {"objective_ref": path.relative_to(
                    Path(self.artifact_root)).as_posix(), "objective_digest": task.objective_digest}
            return proposal, bindings
        except (contract.ContractError, ValueError, TypeError, KeyError, UnicodeError, OSError):
            raise HarnessError("invalid_plan") from None

    def objective(self, identity, task):
        _, paths = self._packet(identity)
        path = contract.resolve_owned(paths["run"] / "objectives", task.task_id + ".txt")
        data = _bytes(path, 16000)
        if hashlib.sha256(data).hexdigest() != task.objective_digest:
            raise HarnessError("objective_changed")
        try:
            text = data.decode("utf-8")
        except UnicodeError:
            raise HarnessError("objective_changed") from None
        if len(text) > 4000:
            raise HarnessError("objective_changed")
        return text

    def read_worker(self, identity):
        """Collect only after the caller proved quiescence; safe for persisted COLLECTED recovery."""
        import supervisor_evidence as evidence
        packet, paths = self._packet(identity)
        artifact_dir = contract.resolve_owned(paths["run"], identity["run_id"] + ".artifacts")
        completion = _json(artifact_dir / "completion.json")
        patch = completion.get("patch_binding") if isinstance(completion, dict) else None
        if not isinstance(patch, dict) or not isinstance(patch.get("patch_sha256"), str) \
                or not contract.SHA256.fullmatch(patch["patch_sha256"]):
            raise HarnessError("result_unavailable")
        patch_ref = "patch-" + patch["patch_sha256"][:16] + ".diff"
        path = contract.resolve_owned(artifact_dir, patch_ref)
        recorded = patch.get("artifact_path")
        if recorded is not None and (not isinstance(recorded, str)
                or os.path.normcase(os.path.realpath(recorded)) != os.path.normcase(str(path))):
            raise HarnessError("invalid_identity")
        # A deterministic bounded selection is advisory data, never acceptance evidence.
        stdout = _bytes(paths["stdout"], MAX_META_BYTES).decode("utf-8", "replace")
        findings = stdout[-4000:] if stdout else None
        findings_ref = None
        if findings is not None:
            findings_ref = "supervisor-findings.txt"
            _write_once(artifact_dir / findings_ref, findings.encode("utf-8"))
        expected = {"run_id": identity["run_id"], "task_sha256": packet["brief_sha256"],
                    "root_identity": os.path.normcase(os.path.realpath(self.envelope.project_root)),
                    "baseline_commit": self.envelope.baseline_commit, "patch_ref": patch_ref,
                    "findings_ref": findings_ref}
        return evidence.collect_result(artifact_dir, expected, self.envelope.baseline_commit)

    def cancel(self, identity, owner_guard, *, timeout=5):
        if not self.envelope.cancel_authority:
            raise HarnessError("permission_denied")
        if not owner_guard():
            raise HarnessError("owner_lost")
        if type(timeout) not in (int, float) or not 0 < timeout <= 5:
            raise HarnessError("invalid_identity")
        try:
            self._packet(identity)
            job, _ = self._job(identity)
            result = job.terminate(owner_guard, timeout=timeout)
            return {"outcome": result["outcome"], "quiescent": job.is_empty()}
        except (ProcessError, OSError):
            raise HarnessError("result_unavailable") from None

    def close(self):
        """Release controller handles, never kill or redispatch a live process on close."""
        for run in self._runs.values():
            if run.get("child") is not None:
                run["child"].close()
            run["job"].close()
        self._runs.clear()

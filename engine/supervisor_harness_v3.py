"""Opt-in parent protocol-2 transport; no driver registration or acceptance.

Requires trusted programs and exclusive ownership of controller metadata, snapshot
stores and Git administration. Python checks and Jobs are not an OS filesystem
sandbox. A lost process/effect identity is never permission to replay a launch.
"""
from dataclasses import dataclass
import fnmatch
import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import time
from types import MappingProxyType

import candidate
import finish_gate as fg
import recovery_handoff as rh
import recovery_runtime as runtime
import supervisor_contract as contract
import supervisor_contract_v3 as codec
import supervisor_manifest_v2 as manifest
import supervisor_process as process
from supervisor_harness import (
    HarnessAdapter, HarnessError, MAX_BRIEF_CHARS, MAX_META_BYTES, PLANNER_MODEL,
    POLICY_FILE, WORKER_MODEL, _role, _write_once,
)
from supervisor_journal import _fsync_dir
from supervisor_policy import ManifestError, git_environment
from supervisor_contract_v3 import GoalEnvelope3 as SourceEnvelope3


# Only program sources, never operator configuration, credentials or user content.
REQUIRED_BUNDLE = (
    "candidate.py", "finish_gate.py", "recovery_handoff.py", "recovery_runtime.py",
    "supervisor_contract.py", "supervisor_contract_v3.py", "supervisor_evidence.py",
    "supervisor_journal.py", "supervisor_launch_v2.py", "supervisor_manifest_v2.py",
    "supervisor_policy.py", "supervisor_process.py",
)
PACKET_KEYS = frozenset({
    "schema_version", "envelope_sha256", "engine_sha256", "bundle_sha256",
    "brief_sha256", "manifest_sha256", "config_sha256", "admission", "role",
    "effect_id", "git_admin",
})
COLLECTION_KEYS = frozenset({
    "schema_version", "controller_sha256", "completion_sha256", "handoff_sha256",
    "attempt_id", "run_id", "seed_candidate", "seed_receipt", "candidate_sha256",
    "receipt_sha256", "scope_sha256", "worker_status", "stop_reason",
    "findings_ref", "findings_sha256",
})
KNOWN_STOPS = frozenset({None, "max_iters", "repair_limit", "context_budget",
                         "no_action_limit"})
UNKNOWN = {
    "state": "UNKNOWN", "outcome": None, "quiescent": False, "usage": None,
    "reported_usage": None, "reason": "result_unavailable",
}


def _sha(raw):
    return hashlib.sha256(raw).hexdigest()


def _canonical(path):
    return os.path.normcase(os.path.realpath(os.fspath(path)))


def _require(ok, code="invalid_identity"):
    if not ok:
        raise HarnessError(code)


def _raw(path, limit=MAX_META_BYTES):
    """Controller metadata must not acquire link or hard-link aliases."""
    try:
        st = os.lstat(path)
        _require(stat.S_ISREG(st.st_mode) and st.st_nlink == 1
                 and not getattr(st, "st_file_attributes", 0) & 0x400
                 and _canonical(path) == os.path.normcase(os.path.abspath(path)),
                 "result_unavailable")
        with open(path, "rb") as stream:
            raw = stream.read(limit + 1)
        _require(len(raw) <= limit, "result_unavailable")
        return raw
    except OSError:
        raise HarnessError("result_unavailable") from None


def _data(path, limit=MAX_META_BYTES):
    try:
        return contract.strict_json(_raw(path, limit), max_bytes=limit)
    except contract.ContractError:
        raise HarnessError("result_unavailable") from None


def _plain_directories(path):
    current = os.path.abspath(path)
    _require(_canonical(current) == os.path.normcase(current))
    while True:
        st = os.lstat(current)
        _require(stat.S_ISDIR(st.st_mode)
                 and not getattr(st, "st_file_attributes", 0) & 0x400)
        parent = os.path.dirname(current)
        if parent == current:
            return
        current = parent


def _terminate_suspended(child):
    """Dispose only the exact locally created, never-resumed native process."""
    kernel = process._api()[0]
    handle = child._handle()
    if not kernel.TerminateProcess(handle, 1):
        raise process.ProcessError("terminate_failed")
    kernel.WaitForSingleObject(handle, 5000)
    child.close()


@dataclass(frozen=True)
class CollectedSnapshot3:
    attempt_id: str
    run_id: str
    seed_candidate: str
    seed_receipt: str
    snapshot: object
    receipt_ref: str
    completion_sha256: str
    handoff_sha256: str
    findings: str | None = None
    findings_ref: str | None = None
    worker_status: str | None = None
    stop_reason: str | None = None


class ResilientHarnessAdapter(HarnessAdapter):
    def __init__(self, envelope, artifact_root, *, capabilities, engine_path=None,
                 python_executable=None, clock=None, job_factory=None):
        if not isinstance(envelope, SourceEnvelope3) or envelope.schema_version != 3:
            raise HarnessError("invalid_identity")
        super().__init__(envelope, artifact_root, capabilities=capabilities,
                         engine_path=engine_path, python_executable=python_executable,
                         clock=clock, job_factory=job_factory)
        self.capabilities = MappingProxyType(self.capabilities)
        self._bundle_pin = None
        self._controller_pins = {}
        self._providers = {}
        self._owners = MappingProxyType({})
        self._snapshot_record_resolver = None

    def set_snapshot_record_resolver(self, resolver):
        """Register the claimed Core's trusted receipt lookup, never worker metadata."""
        _require(callable(resolver))
        self._snapshot_record_resolver = resolver

    def _owner(self, identity, supplied=None):
        """The first trusted caller's guard cannot be replaced by a later caller."""
        launch = identity["launch_id"]
        guard = self._owners.get(launch)
        if supplied is not None:
            _require(callable(supplied) and supplied() is True, "owner_lost")
        if guard is None:
            _require(callable(supplied), "owner_lost")
            guard = supplied
            self._owners = MappingProxyType({**self._owners, launch: guard})
        _require(guard() is True, "owner_lost")
        return guard

    def _approval(self):
        env = self.envelope
        try:
            _require(codec.load_envelope3(contract.strict_json(env.raw),
                                          env.project_root) == env)
            _require({PLANNER_MODEL, WORKER_MODEL} <= set(env.models),
                     "model_not_permitted")
            policy = Path(env.project_root) / POLICY_FILE
            _require(_sha(_raw(policy)) == env.policy_digest, "policy_mismatch")
            rules = _data(policy)["allowed_cmds"]
            _require(isinstance(rules, list) and bool(rules)
                     and all(isinstance(rule, list) and bool(rule)
                             and all(isinstance(a, str) and a for a in rule)
                             for rule in rules), "policy_mismatch")
            for argv in self.capabilities["commands"]:
                _require(not any(a.startswith("/") or os.path.isabs(a)
                                 or ".." in a.replace("\\", "/").split("/")
                                 or any(c in a for c in "*?[]") for a in argv),
                         "capabilities_mismatch")
                _require(any(len(argv) >= len(rule)
                             and all(fnmatch.fnmatch(argv[i], a)
                                     for i, a in enumerate(rule))
                             for rule in rules), "capabilities_mismatch")
        except (contract.ContractError, TypeError, KeyError, ValueError):
            raise HarnessError("policy_mismatch") from None

    def _atomic(self):
        factory = self.job_factory or process.WindowsJob
        _require(callable(getattr(factory, "atomic_supported", None))
                 and factory.atomic_supported() is True
                 and callable(getattr(factory, "create_suspended_atomic", None)),
                 "containment_unavailable")
        return factory

    def _bundle(self):
        directory = Path(self.engine_path).parent
        try:
            return tuple((name, _sha(_raw(path, 16 * 1024 * 1024)))
                         for name, path in [("@engine", Path(self.engine_path))]
                         + [(name, directory / name) for name in REQUIRED_BUNDLE])
        except HarnessError:
            raise HarnessError("engine_changed" if self._bundle_pin is not None
                               else "engine_missing") from None

    def _git(self, root, *args):
        try:
            result = subprocess.run(
                ["git", "-c", "core.hooksPath=", "-c", "core.fsmonitor=false",
                 "-c", "fetch.recurseSubmodules=false", *args], cwd=root,
                env=git_environment({"GIT_NO_LAZY_FETCH": "1", "GIT_OPTIONAL_LOCKS": "0"}),
                stdin=subprocess.DEVNULL, capture_output=True, timeout=30)
            _require(result.returncode == 0 and len(result.stdout) <= 65536,
                     "baseline_mismatch")
            return result.stdout
        except (OSError, subprocess.TimeoutExpired):
            raise HarnessError("baseline_mismatch") from None

    def preflight(self):
        self._approval()
        factory = self._atomic()
        before = self._bundle()
        try:
            caps = contract.strict_json(self._probe("--supervisor-capabilities-v2"))
            expected = manifest.capabilities2()
            _require(caps["protocol"] == expected["protocol"]
                     and type(caps["version"]) is int and caps["version"] == 2
                     and caps["manifest_schema"] == manifest.SCHEMA
                     and tuple(caps["manifest_fields"]) == manifest.FIELDS,
                     "engine_unsupported")
            for key, value in expected["features"].items():
                got = caps["features"][key]
                _require(type(got) is type(value) and got == value, "engine_unsupported")
        except (contract.ContractError, KeyError, TypeError):
            raise HarnessError("engine_unsupported") from None
        _require(before == self._bundle(), "engine_changed")
        if self._bundle_pin is not None:
            _require(before == self._bundle_pin, "engine_changed")
        if self.envelope.candidate.kind == "git":
            commit = self.envelope.candidate.baseline_commit
            _require(self._git(self.envelope.project_root, "rev-parse", "--verify", "--quiet",
                               commit + "^{commit}").strip() == commit.encode("ascii"),
                     "baseline_mismatch")
        _require(factory.atomic_supported() is True, "containment_unavailable")
        self._bundle_pin = before
        self.engine_sha256 = dict(before)["@engine"]
        return {"protocol": caps["protocol"], "version": 2,
                "engine_sha256": self.engine_sha256,
                "bundle_sha256": _sha(contract.canonical(before))}

    def check_engine(self):
        self._approval()
        _require(self._bundle_pin is not None and self._bundle() == self._bundle_pin,
                 "engine_changed")
        return self.engine_sha256

    def _paths(self, identity):
        paths = super()._paths(identity)
        paths.update(candidate=paths["run"] / "candidate",
                     config=paths["run"] / "recovery-contract.json",
                     process=paths["run"] / "process.json",
                     capture=paths["run"] / "collected3",
                     collection=paths["run"] / "collection3.json")
        return paths

    def _bounds(self):
        return candidate.SnapshotBounds(**dict(self.envelope.candidate.bounds))

    def _scope(self):
        # DATA scope uses the transport's canonical UTF-8 representation, not
        # the approval codec's ASCII-escaped representation of the same paths.
        return _sha(json.dumps(list(self.envelope.candidate.inputs), sort_keys=True,
                               separators=(",", ":"), ensure_ascii=False).encode("utf-8"))

    def _snapshot_record(self, pin, guard):
        _require(callable(guard) and guard() is True, "owner_lost")
        resolver = self._snapshot_record_resolver
        _require(callable(resolver))
        record = resolver(pin)
        _require(guard() is True, "owner_lost")
        _require(isinstance(record, (dict, MappingProxyType)) and set(record) == {
            "candidate", "receipt_digest", "snapshot_ref", "seed", "scope_digest",
            "source_attempts", "effects_known", "kind", "revision", "epoch",
        })
        record = dict(record)
        _require(record["receipt_digest"] == pin
                 and record["scope_digest"] == self._scope()
                 and record["effects_known"] is True)
        codec._match(codec.SHA256, record["candidate"], "candidate", True)
        contract.validate_owned_reference(record["snapshot_ref"])
        context = codec.snapshot_context3(
            self.envelope.digest, seed_receipt=record["seed"],
            source_attempts=record["source_attempts"], kind=record["kind"])
        if record["kind"] == "initial":
            _require(record["snapshot_ref"] == "snapshots/initial"
                     and record["candidate"] == self.envelope.candidate.initial_sha256)
        return record, context

    def _snapshot(self, ref, manifest_sha, receipt_sha=None, *, initial=False,
                  owner_guard=None):
        _require(initial or callable(owner_guard), "owner_lost")
        if owner_guard is not None:
            _require(callable(owner_guard) and owner_guard() is True, "owner_lost")
        directory = contract.resolve_owned(self.artifact_root, ref)
        _plain_directories(directory)
        raw = _raw(directory / "receipt.json", 4096)
        pinned = receipt_sha is not None
        if receipt_sha is not None:
            _require(_sha(raw) == receipt_sha, "invalid_identity")
        else:
            _require(initial, "invalid_identity")
            receipt_sha = _sha(raw)
        if owner_guard is not None:
            _require(owner_guard() is True, "owner_lost")
        # The previous seed is data only after the journal's receipt pin matched.
        receipt = contract.strict_json(raw)
        previous = receipt["seed_sha256"]
        _require(not initial or previous is None)
        fixed_initial = (ref == "snapshots/initial"
                         and manifest_sha == self.envelope.candidate.initial_sha256)
        context = None
        if "context_sha256" in receipt:
            _require(pinned)
            record, context = self._snapshot_record(receipt_sha, owner_guard)
            _require(record["candidate"] == manifest_sha and record["snapshot_ref"] == ref
                     and receipt["context_sha256"] == context)
            if record["seed"] is None:
                _require(fixed_initial and previous is None)
            else:
                producing_seed, _ = self._snapshot_record(record["seed"], owner_guard)
                _require(previous == producing_seed["candidate"])
        else:
            _require(fixed_initial and previous is None)
        if owner_guard is not None:
            _require(owner_guard() is True, "owner_lost")
        snapshot = candidate.open_snapshot(
            directory, bounds=self._bounds(), manifest_sha256=manifest_sha,
            receipt_sha256=receipt_sha, scope_sha256=self._scope(), seed_sha256=previous,
            context_sha256=context)
        _require(_data(directory / "manifest.json", self._bounds().max_manifest_bytes)["inputs"]
                 == list(self.envelope.candidate.inputs))
        if owner_guard is not None:
            _require(owner_guard() is True, "owner_lost")
        return snapshot

    def initial_snapshot(self):
        try:
            return self._snapshot("snapshots/initial",
                                  self.envelope.candidate.initial_sha256, initial=True)
        except (candidate.CandidateError, contract.ContractError, OSError,
                KeyError, TypeError, ValueError):
            raise HarnessError("invalid_identity") from None

    def _origin(self):
        desc = self.envelope.candidate
        return ({"kind": "git", "baseline_commit": desc.baseline_commit}
                if desc.kind == "git" else
                {"kind": "artifact-manifest", "initial_sha256": desc.initial_sha256})

    def _execution(self, role):
        worker = role == "worker"
        return {
            "profile": "resilient-v1", "model": WORKER_MODEL if worker else PLANNER_MODEL,
            "effort": "medium" if worker else "high" if role == "report" else "max",
            "tool_mode": "native" if worker else None, "mcp": "none",
            "max_iters": min(200, self.envelope.worker_turn_limit) if worker else 1,
            "write_paths": list(self.capabilities["write_paths"]) if worker else [],
            "commands": [list(c) for c in self.capabilities["commands"]] if worker else [],
        }

    def _config(self, identity, brief_sha, paths):
        return {
            "schema_version": 2, "profile": rh.PROFILE,
            "launch_id": identity["launch_id"],
            "binding": {
                "run_id": identity["run_id"], "task_sha256": brief_sha,
                "root_identity": _canonical(self.envelope.project_root),
                "envelope_sha256": self.envelope.digest, "origin": self._origin(),
                "seed_sha256": identity["seed_candidate"], "scope_sha256": self._scope(),
            },
            "spec": {
                "verifier_owner": "coordinator",
                "definition_sha256": self.envelope.validator_digest,
                "config_sha256": _sha(contract.canonical(self._execution("worker"))),
            },
            "candidate_root_identity": _canonical(paths["candidate"]),
            "owned_dir": _canonical(paths["engine_artifacts"]),
        }

    def _manifest(self, identity, paths, config_sha):
        role, effect_id = _role(identity)
        execution = self._execution(role)
        worker = role == "worker"
        return {
            "schema": manifest.SCHEMA, "root": self.envelope.project_root,
            "policy_sha256": self.envelope.policy_digest,
            "orchestration_id": self.envelope.orchestration_id,
            "attempt_id": effect_id, "launch_id": identity["launch_id"],
            "run_id": identity["run_id"], "role": "worker" if worker else "planner",
            "write_paths": execution["write_paths"], "commands": execution["commands"],
            "mcp": "none", "max_iters": execution["max_iters"],
            "envelope_sha256": self.envelope.digest,
            "candidate": contract.strict_json(self.envelope.raw)["candidate"],
            "seed_candidate": identity["seed_candidate"] if worker else None,
            "seed_ref": identity["seed_ref"] if worker else None,
            "seed_receipt": identity["seed_receipt"] if worker else None,
            "candidate_root": str(paths["candidate"]) if worker else None,
            "run_dir": str(paths["run"]), "profile": manifest.PROFILE,
            "recovery_contract_ref": paths["config"].name if worker else None,
            "recovery_contract_sha256": config_sha if worker else None,
        }

    def _identity_provider(self, m, guard):
        from supervisor_launch_v2 import identity_provider2
        return identity_provider2(m, owner_guard=guard)

    def _administration(self, m, guard):
        if m.candidate.kind != "git":
            return None
        from supervisor_launch_v2 import _administration
        return _administration(m.root, m.candidate_root,
                               m.candidate.baseline_commit, guard)

    def _materialize(self, snapshot, m, guard):
        bounds = self._bounds()
        root = Path(m.candidate_root)
        _require(guard() is True, "owner_lost")
        if m.candidate.kind == "artifact-manifest":
            candidate.materialize(snapshot, root, bounds=bounds, owner_guard=guard)
        else:
            temporary = Path(m.run_dir) / "seed-data"
            candidate.materialize(snapshot, temporary, bounds=bounds, owner_guard=guard)
            _require(not os.path.lexists(root) and guard() is True, "invalid_identity")
            self._git(m.root, "worktree", "add", "--detach", "--no-checkout", "--",
                      str(root), m.candidate.baseline_commit)
            _plain_directories(root)
            _require(sorted(p.name for p in root.iterdir()) == [".git"])
            self._administration(m, guard)
            for entry in temporary.iterdir():
                _require(guard() is True, "owner_lost")
                os.replace(entry, root / entry.name)
            temporary.rmdir()
        provider = self._identity_provider(m, guard)
        _require(provider(str(root)) == snapshot.manifest_sha256)
        self._providers[m.launch_id] = provider

    def _packet(self, identity):
        self.check_engine()
        guard = self._owner(identity)
        role, effect_id = _role(identity)
        paths = self._paths(identity)
        try:
            _plain_directories(self.artifact_root)
            _plain_directories(paths["run"])
            raw = _raw(paths["packet"])
            pin = self._controller_pins.get(identity["launch_id"])
            _require(pin is None or pin == raw)
            packet = contract.strict_json(raw)
            _require(type(packet) is dict and set(packet) == PACKET_KEYS
                     and type(packet["schema_version"]) is int
                     and packet["schema_version"] == 2)
            admission = packet["admission"]
            _require(type(admission) is dict and "process" not in admission
                     and all(k in identity and identity[k] == v
                             for k, v in admission.items()))
            self._paths(admission)
            _require(_role(admission) == (role, effect_id))
            expected = {
                "envelope_sha256": self.envelope.digest,
                "engine_sha256": self.engine_sha256,
                "bundle_sha256": _sha(contract.canonical(self._bundle_pin)),
                "brief_sha256": _sha(_raw(paths["brief"])),
                "role": role, "effect_id": effect_id,
            }
            _require(all(packet[k] == v for k, v in expected.items()))
            worker = role == "worker"
            config_raw = (contract.canonical(self._config(
                identity, expected["brief_sha256"], paths)) if worker else None)
            config_sha = _sha(config_raw) if worker else None
            _require(packet["config_sha256"] == config_sha)
            if worker:
                _require(_raw(paths["config"], manifest.MAX_CONTRACT_BYTES) == config_raw)
                runtime._contract_shape(contract.strict_json(config_raw))
                self._snapshot(identity["seed_ref"], identity["seed_candidate"],
                               identity["seed_receipt"], owner_guard=guard)
            manifest_raw = contract.canonical(self._manifest(identity, paths, config_sha))
            _require(packet["manifest_sha256"] == _sha(manifest_raw)
                     and _raw(paths["manifest"], manifest.MAX_MANIFEST_BYTES) == manifest_raw)
            m = manifest.load_manifest2(str(paths["manifest"]), _sha(manifest_raw))
            _require(contract.canonical(packet["git_admin"])
                     == contract.canonical(self._administration(m, guard)
                                           if worker else None))
            if worker and identity["launch_id"] not in self._providers:
                self._providers[identity["launch_id"]] = self._identity_provider(m, guard)
            self._controller_pins.setdefault(identity["launch_id"], raw)
            return packet, paths
        except (candidate.CandidateError, contract.ContractError, ManifestError,
                runtime.RecoveryRuntimeError, OSError, KeyError, TypeError,
                ValueError, AttributeError):
            raise HarnessError("invalid_identity") from None

    def launch(self, effect, brief, *, on_process, owner_guard):
        self.check_engine()
        factory = self._atomic()
        identity = dict(effect.data)
        role, effect_id = _role(identity)
        paths = self._paths(identity)
        guard = self._owner(identity, owner_guard)
        _require(identity.get("process") is None and callable(on_process))
        _require(not contract.deadline_passed(self.envelope, (self.clock or time.time)()),
                 "deadline")
        _require(isinstance(brief, str) and len(brief) <= MAX_BRIEF_CHARS, "invalid_plan")
        raw_brief = brief.replace("\r\n", "\n").replace("\r", "\n").encode("utf-8")
        worker = role == "worker"
        job = child = None
        resumed = False
        try:
            seed = None
            if worker:
                _require(all(k in identity for k in (
                    "seed_candidate", "seed_ref", "seed_receipt", "predecessor",
                    "continuation_key")))
                seed = self._snapshot(identity["seed_ref"], identity["seed_candidate"],
                                      identity["seed_receipt"], owner_guard=guard)
            _plain_directories(self.artifact_root)
            try:
                paths["run"].mkdir(parents=True, exist_ok=False)
            except FileExistsError:
                raise HarnessError("artifact_collision") from None
            _plain_directories(paths["run"])
            _write_once(paths["brief"], raw_brief)
            config_raw = (contract.canonical(self._config(identity, _sha(raw_brief), paths))
                          if worker else None)
            if worker:
                runtime._contract_shape(contract.strict_json(config_raw))
                _write_once(paths["config"], config_raw)
            manifest_raw = contract.canonical(self._manifest(
                identity, paths, _sha(config_raw) if worker else None))
            _write_once(paths["manifest"], manifest_raw)
            m = manifest.load_manifest2(str(paths["manifest"]), _sha(manifest_raw))
            if worker:
                self._materialize(seed, m, guard)
            packet_raw = contract.canonical({
                "schema_version": 2, "envelope_sha256": self.envelope.digest,
                "engine_sha256": self.engine_sha256,
                "bundle_sha256": _sha(contract.canonical(self._bundle_pin)),
                "brief_sha256": _sha(raw_brief), "manifest_sha256": _sha(manifest_raw),
                "config_sha256": _sha(config_raw) if worker else None,
                "admission": {k: v for k, v in identity.items() if k != "process"},
                "role": role, "effect_id": effect_id,
                "git_admin": self._administration(m, guard) if worker else None,
            })
            _write_once(paths["packet"], packet_raw)
            _fsync_dir(str(paths["run"]))
            self._controller_pins[identity["launch_id"]] = packet_raw
            self._packet(identity)
            execution = self._execution(role)
            argv = [
                self.python, "-u", self.engine_path, "-C", self.envelope.project_root,
                "-m", execution["model"], "-e", execution["effort"], "--profile", "resilient-v1",
                "--skill", "ponytail", "--text-only", "--label", effect_id,
                "-f", str(paths["brief"]), "--transcript", str(paths["transcript"]),
                "--launch-id", identity["launch_id"], "--launch-receipt", str(paths["receipt"]),
                "--supervisor-manifest", str(paths["manifest"]),
                "--supervisor-manifest-sha256", _sha(manifest_raw),
            ]
            argv += (["--write", "--max-iters", str(execution["max_iters"]),
                      "--tool-mode", "native", "--recovery-contract", str(paths["config"])]
                     if worker else ["--oneshot"])
            _require(guard() is True, "owner_lost")
            _require(not contract.deadline_passed(
                self.envelope, (self.clock or time.time)()), "deadline")
            self.check_engine()
            job = factory.create(identity["job_name"])
            env = {k: v for k, v in os.environ.items() if not k.upper().startswith("GIT_")}
            env["_HARNESS_APPROVED_WORKER"] = "1"
            child = job.create_suspended_atomic(
                argv, cwd=self.envelope.project_root,
                env=env,
                stdout_path=paths["stdout"], stderr_path=paths["stderr"])
            _require(type(child.pid) is int and child.pid > 0
                     and type(child.birth) is int and 0 < child.birth < 2**64)
            recorded = {"pid": child.pid, "birth": "winft:" + str(child.birth)}
            self._runs[identity["launch_id"]] = {
                "job": job, "child": child, "process": recorded,
            }
            on_process(effect_id, identity["launch_id"], recorded["pid"], recorded["birth"])
            _write_once(paths["process"], contract.canonical({
                "schema_version": 2, "launch_id": identity["launch_id"],
                "job_name": identity["job_name"], "controller_sha256": _sha(packet_raw),
                "process": recorded,
            }))
            _fsync_dir(str(paths["run"]))
            self.check_engine()
            _require(guard() is True, "owner_lost")
            _require(not contract.deadline_passed(
                self.envelope, (self.clock or time.time)()), "deadline")
            if worker:
                _require(self._providers[identity["launch_id"]](m.candidate_root)
                         == seed.manifest_sha256)
            child.resume()
            resumed = True
            return {"launch_id": identity["launch_id"], **recorded}
        except Exception as exc:
            if child is not None and not resumed:
                try:
                    _terminate_suspended(child)
                except (process.ProcessError, OSError):
                    # A failed disposal leaves the retained Job UNKNOWN, never replayable.
                    child.close()
            elif job is not None and child is None:
                job.close()
            if isinstance(exc, HarnessError):
                raise
            raise HarnessError("launch_failed") from None

    def _job(self, identity):
        self._owner(identity)
        recorded = identity.get("process")
        _require(type(recorded) is dict and set(recorded) == {"pid", "birth"}
                 and type(recorded["pid"]) is int and recorded["pid"] > 0)
        birth = recorded["birth"]
        try:
            if isinstance(birth, str) and birth.startswith("winft:"):
                ticks = int(birth[6:])
                _require(birth == "winft:" + str(ticks) and 0 < ticks < 2**64)
            else:
                ticks = process.decode_birth3(birth)
        except (ValueError, process.ProcessError):
            raise HarnessError("invalid_identity") from None
        paths = self._paths(identity)
        _require(_data(paths["process"]) == {
            "schema_version": 2, "launch_id": identity["launch_id"],
            "job_name": identity["job_name"],
            "controller_sha256": _sha(self._controller_pins[identity["launch_id"]]),
            "process": recorded,
        })
        running = self._runs.get(identity["launch_id"])
        if running is not None:
            _require(running["process"] == recorded)
            return running["job"], recorded
        job = (self.job_factory or process.WindowsJob).open_existing(identity["job_name"])
        try:
            # A name alone cannot authenticate a reopened, leaderless Job.
            _require(job.contains(recorded["pid"], ticks) is True)
            count = job.active_count()
            _require(type(count) is int and count >= 0)
        except Exception:
            job.close()
            raise HarnessError("invalid_identity") from None
        self._runs[identity["launch_id"]] = {
            "job": job, "child": None, "process": dict(recorded),
        }
        return job, recorded

    def _handoff_observation(self, identity, packet, paths, guard):
        """Validate an observation, including null points, without certifying old checks."""
        config = self._config(identity, packet["brief_sha256"], paths)
        base = paths["engine_artifacts"]
        _plain_directories(base)
        completion_raw = _raw(base / rh.COMPLETION_NAME, rh.MAX_COMPLETION_BYTES)
        completion = contract.strict_json(completion_raw, max_bytes=rh.MAX_COMPLETION_BYTES)
        _require(type(completion) is dict
                 and completion.get("recovery_profile") == rh.PROFILE
                 and completion.get("recovery_contract_sha256") == packet["config_sha256"]
                 and completion.get("recovery_handoff_status") == "bound"
                 and completion.get("recovery_error") is None
                 and not completion.get("uncertain_evidence")
                 and completion.get("stop_reason") not in (
                     "recovery_state", "mutation_outcome_unknown"), "result_unavailable")
        handoff_raw = _raw(base / rh.HANDOFF_NAME, rh.MAX_BYTES)
        record = rh._record(contract.strict_json(handoff_raw, max_bytes=rh.MAX_BYTES), str(base))
        _require(type(record["schema_version"]) is int and record["schema_version"] == 2
                 and record["phase"] == "settled"
                 and completion.get("recovery_handoff_sha256") == _sha(handoff_raw)
                 and record["binding"] == rh._binding(config["binding"], str(base), 2)
                 and record["spec"] == rh._spec(config["spec"]), "result_unavailable")
        ev = record["evidence"]
        root = config["candidate_root_identity"]
        plan = fg.VerificationPlan(())
        rh._check_spec_plan(config["spec"], plan)
        _require(_canonical(ev["root_identity"]) == _canonical(root)
                 and ev["write_mode"] == "disk" and not ev["uncertain_mutation"]
                 and all(_canonical(c["root_identity"]) == _canonical(root)
                         and c["outcome"] != "outcome_unknown"
                         for c in ev["checks"])
                 and record["rerun_owed"] == rh._owed(record, plan), "result_unavailable")
        _require(guard() is True, "owner_lost")
        current = self._providers[identity["launch_id"]](root)
        rh._sha(current, "invalid_candidate")
        after = record["after"]["candidate_sha256"]
        _require(after is None or after == current, "result_unavailable")
        rh._no_artifacts_in_candidate(str(base), ev, after)
        # Null remains null in the worker record; a later parent receipt is new lineage.
        return completion, completion_raw, handoff_raw, current

    def observe(self, identity, *, owner_guard=None):
        try:
            guard = self._owner(identity, owner_guard)
            packet, paths = self._packet(identity)
            job, recorded = self._job(identity)
            count = job.active_count()
            _require(type(count) is int and count >= 0, "result_unavailable")
            receipts = [contract.strict_json(line)
                        for line in _raw(paths["receipt"]).splitlines() if line]
            _require(all(type(row) is dict and type(row.get("schema")) is int
                         and row["schema"] == 1
                         and row.get("launch_id") == identity["launch_id"]
                         and row.get("engine_pid") == recorded["pid"] for row in receipts),
                     "result_unavailable")
            ready = [row for row in receipts if row.get("state") == "ready"]
            _require(len(ready) == 1, "result_unavailable")
            root = paths["candidate"] if _role(identity)[0] == "worker" \
                else self.envelope.project_root
            rec = ready[0]
            _require(rec.get("run_id") == identity["run_id"]
                     and _canonical(rec.get("root", "")) == _canonical(root)
                     and _canonical(rec.get("transcript", "")) == _canonical(paths["transcript"])
                     and type(rec.get("index_paths")) is list
                     and _canonical(paths["index"]) in [
                         _canonical(p) for p in rec["index_paths"] if isinstance(p, str)],
                     "result_unavailable")
            rows = [contract.strict_json(line)
                    for line in _raw(paths["index"]).splitlines() if line]
            _require(all(type(row) is dict and type(row.get("schema")) is int
                         and row["schema"] in (1, 2)
                         and row.get("run_id") == identity["run_id"]
                         and row.get("launch_id", identity["launch_id"]) == identity["launch_id"]
                         and row.get("engine_pid", recorded["pid"]) == recorded["pid"]
                         for row in rows), "result_unavailable")
            starts = [row for row in rows if row.get("event") == "start"]
            ends = [row for row in rows if row.get("event") == "end"]
            _require(len(starts) == 1 and len(ends) <= 1
                     and starts[0].get("engine_pid") == recorded["pid"]
                     and _canonical(starts[0].get("root", "")) == _canonical(root),
                     "result_unavailable")
            _require(guard() is True, "owner_lost")
            if count:
                return {**UNKNOWN, "state": "RUNNING", "reason": None}
            _require(len(ends) == 1, "result_unavailable")
            end = ends[0]
            _require(end.get("status") in ("complete", "failed", "incomplete")
                     and end.get("stop_reason") in KNOWN_STOPS
                     and not end.get("uncertain_evidence")
                     and end.get("recovery_error") is None, "result_unavailable")
            if _role(identity)[0] == "worker":
                self._handoff_observation(identity, packet, paths, guard)
            reported = (end["sent"] + end["recv"]
                        if all(type(end.get(k)) is int and end[k] >= 0
                               for k in ("sent", "recv")) else None)
            _require(job.is_empty() is True and guard() is True, "result_unavailable")
            return {
                "state": "TERMINAL",
                "outcome": "succeeded" if end["status"] == "complete"
                and end.get("stop_reason") is None else "failed",
                "quiescent": True, "usage": None, "reported_usage": reported,
                "reason": end.get("stop_reason"), "worker_status": end["status"],
            }
        except (HarnessError, process.ProcessError, contract.ContractError,
                candidate.CandidateError, rh.HandoffError, ManifestError,
                OSError, ValueError, TypeError, KeyError, AttributeError):
            return dict(UNKNOWN)

    def read_plan(self, identity, revision, *, owner_guard=None):
        self._owner(identity, owner_guard)
        _, paths = self._packet(identity)
        try:
            limit = contract.MAX_PROPOSAL_BYTES_V2
            body = contract.strict_json(_raw(paths["stdout"], limit), max_bytes=limit)
            _require(type(body) is dict and set(body) == {"plan_revision", "tasks"}
                     and type(body["plan_revision"]) is int
                     and body["plan_revision"] == revision and type(body["tasks"]) is list
                     and 1 <= len(body["tasks"]) <= contract.proposal_task_limit(self.envelope),
                     "invalid_plan")
            tasks, objectives = [], {}
            fields = {"task_id", "depends_on", "objective", "criteria", "hypothesis_ids"}
            for task in body["tasks"]:
                _require(type(task) is dict and set(task) == fields
                         and isinstance(task["objective"], str)
                         and 1 <= len(task["objective"]) <= 4000, "invalid_plan")
                text = task["objective"].replace("\r\n", "\n").replace("\r", "\n")
                tasks.append({k: v for k, v in task.items() if k != "objective"})
                tasks[-1]["objective_digest"] = _sha(text.encode("utf-8"))
                objectives[task["task_id"]] = text
            proposal = contract.canonical({
                "schema_version": 1, "plan_revision": revision, "tasks": tasks,
            })
            plan = contract.validate_proposal(proposal, self.envelope, revision)
            directory = paths["run"] / "objectives"
            directory.mkdir(exist_ok=True)
            _plain_directories(directory)
            bindings = {}
            for task in plan.tasks:
                path = contract.resolve_owned(directory, task.task_id + ".txt")
                _write_once(path, objectives[task.task_id].encode("utf-8"))
                bindings[task.task_id] = {
                    "objective_ref": path.relative_to(Path(self.artifact_root)).as_posix(),
                    "objective_digest": task.objective_digest,
                }
            _fsync_dir(directory)
            return proposal, bindings
        except (contract.ContractError, ValueError, TypeError, KeyError, UnicodeError, OSError):
            raise HarnessError("invalid_plan") from None

    def _capture_admin(self, m, packet, guard, current):
        if m.candidate.kind != "git":
            return None
        state = self._administration(m, guard)
        _require(contract.canonical(state) == contract.canonical(packet["git_admin"]))
        provider = self._providers[m.launch_id]

        def unchanged():
            return (guard() is True
                    and contract.canonical(self._administration(m, guard))
                    == contract.canonical(packet["git_admin"])
                    and provider(m.candidate_root) == current)

        return candidate.GitAdmin(state[0], unchanged)

    def read_worker(self, identity, *, owner_guard=None):
        guard = self._owner(identity, owner_guard)
        # Preserve exact pin failures before generic lifecycle observation can
        # conservatively classify an unreadable group as UNKNOWN.
        self._packet(identity)
        observation = self.observe(identity)
        _require(observation["state"] == "TERMINAL" and observation["quiescent"] is True,
                 "result_unavailable")
        _require(_role(identity)[0] == "worker")
        packet, paths = self._packet(identity)
        job, _ = self._job(identity)
        try:
            completion, completion_raw, handoff_raw, current = self._handoff_observation(
                identity, packet, paths, guard)
            seed = self._snapshot(identity["seed_ref"], identity["seed_candidate"],
                                  identity["seed_receipt"], owner_guard=guard)
            m = manifest.load_manifest2(str(paths["manifest"]), packet["manifest_sha256"])
            boundary = manifest.Boundary2(m.write_paths, m.commands,
                                          candidate_root=m.candidate_root, manifest=m)

            def quiet_owner():
                return guard() is True and job.is_empty() is True

            _require(quiet_owner(), "owner_lost")
            context = codec.snapshot_context3(
                self.envelope.digest, seed_receipt=identity["seed_receipt"],
                source_attempts=[identity["attempt_id"]], kind="worker")
            # Content hygiene: collection records metadata only; stdout stays for on-demand narration.
            worker_status = completion.get("verification_status", observation["worker_status"])
            _require(isinstance(worker_status, str) and 0 < len(worker_status) <= 128,
                     "result_unavailable")
            expected = {
                "schema_version": 3,
                "controller_sha256": _sha(self._controller_pins[identity["launch_id"]]),
                "completion_sha256": _sha(completion_raw),
                "handoff_sha256": _sha(handoff_raw), "attempt_id": identity["attempt_id"],
                "run_id": identity["run_id"], "seed_candidate": identity["seed_candidate"],
                "seed_receipt": identity["seed_receipt"], "candidate_sha256": current,
                "scope_sha256": self._scope(), "worker_status": worker_status,
                "stop_reason": observation["reason"], "findings_ref": None,
                "findings_sha256": None,
            }
            if os.path.lexists(paths["collection"]):
                metadata = _data(paths["collection"])
                _require(type(metadata) is dict and set(metadata) == COLLECTION_KEYS
                         and type(metadata["schema_version"]) is int
                         and all(metadata[k] == v for k, v in expected.items()),
                         "result_unavailable")
                _require(quiet_owner(), "owner_lost")
                snapshot = candidate.open_snapshot(
                    paths["capture"], bounds=self._bounds(), manifest_sha256=current,
                    receipt_sha256=metadata["receipt_sha256"],
                    scope_sha256=self._scope(), seed_sha256=identity["seed_candidate"],
                    context_sha256=context)
                _require(quiet_owner(), "owner_lost")
            else:
                _require(not os.path.lexists(paths["capture"]), "artifact_collision")
                snapshot = candidate.capture(
                    paths["candidate"], paths["capture"], bounds=self._bounds(),
                    inputs=self.envelope.candidate.inputs, owner_guard=quiet_owner,
                    seed_sha256=identity["seed_candidate"], strict_layout=True,
                    git_admin=self._capture_admin(m, packet, guard, current),
                    context_sha256=context)
            _require(snapshot.manifest_sha256 == current, "result_unavailable")
            for path, old, new in candidate.delta(seed, snapshot, bounds=self._bounds()):
                boundary.check_write(paths["candidate"],
                                     os.path.join(paths["candidate"], *path.split("/")))
            # Re-read every raw result pin and the current DATA after transport publication.
            self._packet(identity)
            _, craw, hraw, live = self._handoff_observation(identity, packet, paths, guard)
            _require(craw == completion_raw and hraw == handoff_raw and live == current
                     and quiet_owner(), "result_unavailable")
            metadata = dict(expected, receipt_sha256=snapshot.receipt_sha256)
            _write_once(paths["collection"], contract.canonical(metadata))
            _fsync_dir(str(paths["run"]))
            _require(quiet_owner(), "owner_lost")
            return CollectedSnapshot3(
                attempt_id=identity["attempt_id"], run_id=identity["run_id"],
                seed_candidate=identity["seed_candidate"], seed_receipt=identity["seed_receipt"],
                snapshot=snapshot,
                receipt_ref=(paths["capture"] / "receipt.json").relative_to(
                    Path(self.artifact_root)).as_posix(),
                completion_sha256=_sha(completion_raw), handoff_sha256=_sha(handoff_raw),
                findings=None, findings_ref=None, worker_status=worker_status,
                stop_reason=observation["reason"])
        except (candidate.CandidateError, contract.ContractError, ManifestError,
                rh.HandoffError, runtime.RecoveryRuntimeError, process.ProcessError,
                OSError, ValueError, TypeError, KeyError, AttributeError):
            raise HarnessError("result_unavailable") from None

    def cancel(self, identity, owner_guard, *, timeout=5):
        guard = self._owner(identity, owner_guard)
        return super().cancel(identity, guard, timeout=timeout)

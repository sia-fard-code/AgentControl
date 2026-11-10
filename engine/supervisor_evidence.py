"""Goal-supervisor evidence: candidate identity, bound collection, owned integration, acceptance.

Pure functions with no Supervisor-state effects; the driver registers returned facts.
Errors are EvidenceError with a fixed `code`; subprocess output/exception text is never exposed.
Schemas are documented beside each public function.
"""
import hashlib
import os
import stat
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass, field

from finish_gate import VERIFICATION_STATUSES
from supervisor_process import ProcessError, WindowsJob
from supervisor_policy import git_environment
from supervisor_contract import (COMMIT, ID, MAX_ENVELOPE_BYTES, SHA256, ContractError, canonical,  # noqa: F401
                                 resolve_owned, strict_json)

IDENTITY_VERSION = 1
RESERVED_TOP = frozenset({".git"})  # Git metadata only; generated caches/outputs ARE content
MAX_CANDIDATE_BYTES = 256 * 1024 * 1024
MAX_CANDIDATE_ENTRIES = 100000
MAX_HASH_SECONDS = 10.0


class EvidenceError(Exception):
    """Evidence cannot be established; `code` is a fixed, central-safe reason."""

    def __init__(self, code):
        super().__init__(code)
        self.code = code


def _inside(base, path):
    base, path = os.path.normcase(base), os.path.normcase(path)
    return os.path.commonpath([base, path]) == base


def _hash_budget(max_bytes, max_entries, max_seconds, code="candidate_limit"):
    if type(max_bytes) is not int or max_bytes <= 0 or type(max_entries) is not int or max_entries <= 0 \
            or type(max_seconds) not in (int, float) or not 0 < max_seconds <= 3600:
        raise EvidenceError(code)
    return {"bytes": max_bytes, "entries": max_entries, "until": time.monotonic() + max_seconds,
            "code": code}


def _hash_tick(budget, *, size=0, entries=0):
    budget["bytes"] -= size
    budget["entries"] -= entries
    if budget["bytes"] < 0 or budget["entries"] < 0 or time.monotonic() >= budget["until"]:
        raise EvidenceError(budget["code"])


def candidate_identity(root, *, max_bytes=MAX_CANDIDATE_BYTES, max_entries=MAX_CANDIDATE_ENTRIES,
                       max_seconds=MAX_HASH_SECONDS):
    """SHA256 of a versioned, sorted manifest of (relpath, type, mode, byte digest).

    Types: "f" regular file (mode "x" executable on POSIX else "-"), "l" in-tree symlink
    (digest of its target text). Directories are implied. Anything else, or any link or
    junction resolving outside `root`, raises EvidenceError("unsafe_entry").
    """
    base = os.path.realpath(os.fspath(root))
    if not os.path.isdir(base):
        raise EvidenceError("candidate_missing")
    try:
        return _identity(base, _hash_budget(max_bytes, max_entries, max_seconds))
    except OSError:  # vanished/locked entry mid-walk: fixed code, never the OS message
        raise EvidenceError("candidate_unreadable") from None


def _raise(err):
    raise err


def _identity(base, budget):
    entries = []
    for dirpath, dirnames, filenames in os.walk(base, onerror=_raise, followlinks=False):
        _hash_tick(budget)
        if dirpath == base:
            dirnames[:] = [d for d in dirnames if d not in RESERVED_TOP]
            filenames = [f for f in filenames if f not in RESERVED_TOP]
        for name in list(dirnames) + filenames:
            _hash_tick(budget, entries=1)
            full = os.path.join(dirpath, name)
            rel = os.path.relpath(full, base).replace(os.sep, "/")
            st = os.lstat(full)
            if not _inside(base, os.path.realpath(full)):
                raise EvidenceError("unsafe_entry")
            if stat.S_ISLNK(st.st_mode):
                if name in dirnames:
                    dirnames.remove(name)  # never descend through links
                target = os.readlink(full).encode("utf-8", "surrogateescape")
                entries.append([rel, "l", "-", hashlib.sha256(target).hexdigest()])
            elif stat.S_ISDIR(st.st_mode):
                continue
            elif stat.S_ISREG(st.st_mode):
                mode = "x" if os.name != "nt" and st.st_mode & 0o111 else "-"
                if st.st_size > budget["bytes"]:
                    raise EvidenceError("candidate_limit")
                entries.append([rel, "f", mode, _file_sha(full, budget=budget)])
            else:
                raise EvidenceError("unsafe_entry")
    entries.sort()
    data = canonical({"v": IDENTITY_VERSION, "entries": entries})
    _hash_tick(budget)
    return hashlib.sha256(data).hexdigest()


MAX_FINDINGS_CHARS = 4000  # per attempt; the driver enforces 24000 per planner request
MAX_PATCH_BYTES = 8 * 1024 * 1024
EXPECTED_KEYS = frozenset({"run_id", "task_sha256", "root_identity", "baseline_commit",
                           "patch_ref", "findings_ref"})


@dataclass(frozen=True)
class CollectedResult:
    """Bound worker result. `patch_bytes`/`findings` are run-owned content: never centralize."""
    run_id: str
    baseline_commit: str
    patch_sha256: str
    patch_size: int
    completion_sha256: str
    transport_sha256: str
    worker_verification_status: object  # worker claim only; acceptance re-verifies
    patch_bytes: bytes = field(repr=False)
    findings: object = field(repr=False)  # str (<= 4000 chars, untrusted) or None

    def metadata(self):
        return {"run_id": self.run_id, "baseline_commit": self.baseline_commit,
                "patch_sha256": self.patch_sha256, "patch_size": self.patch_size,
                "completion_sha256": self.completion_sha256,
                "transport_sha256": self.transport_sha256,
                "findings_chars": None if self.findings is None else len(self.findings)}


def _owned_bytes(base, ref, limit, missing):
    try:
        path = resolve_owned(base, ref)
    except ContractError:
        raise EvidenceError("unsafe_reference") from None
    try:
        with open(path, "rb") as fh:
            raw = fh.read(limit + 1)
    except OSError:
        raise EvidenceError(missing) from None
    return raw


def collect_result(owned_artifact_dir, expected_binding, approved_baseline):
    """Read and bind one attempt's delegate artifacts.

    expected_binding (supervisor-recorded at admission, all str unless noted):
      run_id, task_sha256 (64 hex), root_identity (canonical policy-owning project root),
      baseline_commit (40 hex), patch_ref (owned relative path), findings_ref (owned
      relative path or None). completion.json is delegate's record: run_id, run_binding,
      patch_binding {run, patch_sha256, patch_size, ...}, verification_status.
    """
    exp = expected_binding
    if not isinstance(exp, dict) or set(exp) != EXPECTED_KEYS or not (
            all(isinstance(exp[k], str) and exp[k] for k in ("run_id", "root_identity", "patch_ref"))
            and isinstance(exp["task_sha256"], str) and SHA256.match(exp["task_sha256"])
            and isinstance(exp["baseline_commit"], str) and COMMIT.match(exp["baseline_commit"])
            and (exp["findings_ref"] is None or isinstance(exp["findings_ref"], str))):
        raise EvidenceError("expected_binding_malformed")
    if exp["baseline_commit"] != approved_baseline:
        raise EvidenceError("stale_baseline")
    raw = _owned_bytes(owned_artifact_dir, "completion.json", MAX_ENVELOPE_BYTES, "completion_missing")
    try:
        rec = strict_json(raw)
    except ContractError:
        raise EvidenceError("completion_malformed") from None
    if not isinstance(rec, dict) or not {"run_id", "run_binding", "patch_binding"} <= set(rec):
        raise EvidenceError("completion_malformed")
    if rec["run_id"] != exp["run_id"]:
        raise EvidenceError("wrong_run")
    run, patch = rec["run_binding"], rec["patch_binding"]
    if not isinstance(run, dict) or not isinstance(patch, dict):
        raise EvidenceError("binding_unavailable")  # absent binding is UNKNOWN, never "no change"
    if patch.get("run") != run or any(run.get(k) != exp[k]
                                      for k in ("run_id", "task_sha256", "root_identity")):
        raise EvidenceError("wrong_run")
    if run.get("write_mode") != "disk" and rec.get("verification_status") == "verified_by_command":
        raise EvidenceError("stage_verification_claim")
    if rec.get("verification_status") not in VERIFICATION_STATUSES:
        raise EvidenceError("completion_malformed")  # unknown worker claim: never exposed
    data = _owned_bytes(owned_artifact_dir, exp["patch_ref"], MAX_PATCH_BYTES, "patch_missing")
    if len(data) > MAX_PATCH_BYTES:
        raise EvidenceError("patch_oversized")  # reject before any digest of a truncated prefix
    if hashlib.sha256(data).hexdigest() != patch.get("patch_sha256") or len(data) != patch.get("patch_size"):
        raise EvidenceError("patch_digest_mismatch")
    findings = None
    if exp["findings_ref"] is not None:
        fraw = _owned_bytes(owned_artifact_dir, exp["findings_ref"], 4 * MAX_FINDINGS_CHARS,
                            "findings_missing")
        try:
            findings = fraw.decode("utf-8")
        except UnicodeDecodeError:
            raise EvidenceError("findings_malformed") from None
        if len(fraw) > 4 * MAX_FINDINGS_CHARS or len(findings) > MAX_FINDINGS_CHARS:
            raise EvidenceError("findings_oversized")  # reject, never silently truncate
    comp_sha, patch_sha = hashlib.sha256(raw).hexdigest(), hashlib.sha256(data).hexdigest()
    return CollectedResult(
        run_id=exp["run_id"], baseline_commit=exp["baseline_commit"], patch_sha256=patch_sha,
        patch_size=len(data), completion_sha256=comp_sha,
        transport_sha256=hashlib.sha256(canonical([comp_sha, patch_sha])).hexdigest(),
        worker_verification_status=rec.get("verification_status"), patch_bytes=data,
        findings=findings)


GIT_TIMEOUT = 120
GIT_CFG = ["-c", "core.autocrlf=false", "-c", "core.safecrlf=false", "-c", "core.hooksPath=",
           "-c", "core.fsmonitor=false"]


def _git(args, env_extra=None, cwd=None, data=None):
    """Run git (argv, no shell, bounded). Returns (ok, stdout); stderr is discarded as sensitive."""
    env = git_environment(env_extra)
    try:
        out = subprocess.run(["git", *GIT_CFG, *args], cwd=cwd, env=env, input=data,
                             stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                             timeout=GIT_TIMEOUT, shell=False)
    except (OSError, subprocess.TimeoutExpired):
        return False, b""
    return out.returncode == 0, out.stdout[:1 << 16]


@dataclass(frozen=True)
class IntegratedCandidate:
    """Owned candidate at `<candidate_dir>/tree`, bound by `<candidate_dir>/manifest.json`."""
    candidate_dir: str
    tree: str
    baseline_commit: str
    sources: tuple  # ({"run_id", "patch_sha256", "transport_sha256"}, ...) in apply order
    conflict_run_id: object  # run_id of the first patch that failed to apply, else None
    candidate_sha256: object  # candidate_identity(tree); None when a conflict left it unmet
    manifest_sha256: str


@dataclass(frozen=True)
class EvaluationSnapshot3:
    """Protected DATA tree and pin supplied by the driver; no synthetic Git origin."""
    candidate_dir: str
    tree: str
    candidate_sha256: object
    artifact_ref: str
    conflict_run_id: object = None


RESERVED_WRITE = frozenset({".git", ".claude", ".supervisor", ".gitmodules"})


def _check_write_scope(env, baseline_commit, allowed):
    """Every path the private index changed vs baseline (add/delete/modify/mode) must be in scope."""
    ok, out = _git(["diff-index", "--cached", "--no-renames", "--name-only", "-z", baseline_commit], env)
    if not ok or len(out) >= 1 << 16:  # shortcut: 64KiB path list cap; raise _git's cap if needed
        raise EvidenceError("integration_failed")
    for rel in filter(None, out.decode("utf-8", "surrogateescape").split("\0")):
        if any(part.lower() in RESERVED_WRITE for part in rel.split("/")) or (
                allowed is not None and not any(rel == a or rel.startswith(a.rstrip("/") + "/")
                                                for a in allowed)):
            raise EvidenceError("write_scope_violation")


def patch_paths(project_root, patch):
    """Path-aware supersession uses Git's NUL-delimited parser, not criterion overlap."""
    if not isinstance(patch, bytes) or len(patch) > MAX_PATCH_BYTES:
        raise EvidenceError("patch_size")
    if not patch:
        return frozenset()
    ok, output = _git(["-C", os.fspath(project_root), "apply", "--numstat", "-z"], data=patch)
    if not ok or len(output) > MAX_PATCH_BYTES:
        raise EvidenceError("patch_paths")
    chunks, paths, i = output.split(b"\0"), set(), 0
    try:
        while i < len(chunks) - 1:
            fields = chunks[i].split(b"\t", 2)
            i += 1
            if len(fields) != 3:
                raise ValueError
            names = [fields[2]]
            if not names[0]:
                names = chunks[i:i + 2]
                i += 2
                if len(names) != 2:
                    raise ValueError
            for raw in names:
                name = raw.decode("utf-8")
                if not name or name.startswith("/") or "\\" in name or ":" in name \
                        or any(p in ("", ".", "..") for p in name.split("/")):
                    raise ValueError
                paths.add(name)
    except (ValueError, UnicodeError):
        raise EvidenceError("patch_paths") from None
    return frozenset(paths)


def integrate(project_root, baseline_commit, collected_results, candidate_dir, allowed_write_paths=None):
    """Materialize the approved baseline in a NEW owned dir and apply patches in given order.

    allowed_write_paths: envelope.capabilities.write (driver must pass it); None skips only the
    scope check. Reserved metadata (.git/.claude/.supervisor/.gitmodules) is always refused.

    Never writes the main repo (index, refs, worktrees, objects): a private bare repo in
    `<candidate_dir>/git` borrows main objects read-only via alternates. A conflict is a fact
    (conflict_run_id), never resolved. An existing candidate_dir raises "candidate_exists".
    """
    if not isinstance(baseline_commit, str) or not COMMIT.match(baseline_commit) \
            or any(r.baseline_commit != baseline_commit for r in collected_results):
        raise EvidenceError("stale_baseline")
    ok, common = _git(["-C", os.fspath(project_root), "rev-parse", "--path-format=absolute",
                       "--git-common-dir"])
    objects = os.path.join(common.decode("utf-8", "replace").strip(), "objects")
    if not ok or not os.path.isdir(objects):
        raise EvidenceError("baseline_unavailable")
    dest = os.path.abspath(os.fspath(candidate_dir))
    try:
        os.mkdir(dest)  # exclusive: an existing path is held, never reused or cleaned
    except FileExistsError:
        raise EvidenceError("candidate_exists") from None
    gitdir, tree = os.path.join(dest, "git"), os.path.join(dest, "tree")
    os.mkdir(tree)
    if not _git(["init", "-q", "--bare", gitdir])[0]:
        raise EvidenceError("integration_failed")
    with open(os.path.join(gitdir, "objects", "info", "alternates"), "w", encoding="utf-8",
              newline="\n") as fh:
        fh.write(objects + "\n")
    if not _git(["--git-dir", gitdir, "config", "core.bare", "false"])[0]:
        raise EvidenceError("integration_failed")
    env = {"GIT_DIR": gitdir, "GIT_WORK_TREE": tree, "GIT_INDEX_FILE": os.path.join(gitdir, "index")}
    if not (_git(["cat-file", "-e", baseline_commit + "^{commit}"], env)[0]
            and _git(["read-tree", baseline_commit], env)[0]
            and _git(["checkout-index", "-a", "-f", "-u"], env, cwd=tree)[0]):
        raise EvidenceError("baseline_unavailable")
    sources, conflict = [], None
    for res in collected_results:
        sources.append({"run_id": res.run_id, "patch_sha256": res.patch_sha256,
                        "transport_sha256": res.transport_sha256})
        if conflict is None and res.patch_bytes.strip() and not _git(
                ["apply", "--index", "--binary", "--unidiff-zero", "--whitespace=nowarn", "-"], env, cwd=tree,
                data=res.patch_bytes)[0]:
            conflict = res.run_id
    _check_write_scope(env, baseline_commit, allowed_write_paths)
    cand_sha = None if conflict else candidate_identity(tree)
    manifest = canonical({"v": 1, "baseline_commit": baseline_commit, "sources": sources,
                          "conflict_run_id": conflict, "candidate_sha256": cand_sha})
    with open(os.path.join(dest, "manifest.json"), "xb") as fh:
        fh.write(manifest)
    return IntegratedCandidate(dest, tree, baseline_commit, tuple(sources), conflict, cand_sha,
                               hashlib.sha256(manifest).hexdigest())


REGISTRY_KEYS = frozenset({"schema_version", "validators_dir", "checks", "manual_criteria"})
CHECK_KEYS = frozenset({"check_id", "criteria", "script", "script_sha256", "timeout_seconds",
                        "failure_codes", "falsifies"})
OUTPUT_TAIL = 4096


@dataclass(frozen=True)
class AcceptanceFacts:
    """Metadata-only acceptance facts; raw validator output stays in the owned out_dir."""
    candidate_sha256: object
    registry_sha256: str
    accepted: bool
    satisfied_criteria: tuple  # envelope order
    checks: tuple  # ({"check_id", "status": "pass"|"fail"|"not_run", "failure_code"}, ...)
    hypothesis_transitions: tuple  # ({"hypothesis_id", "check_id", "from", "to"}, ...)
    invalid_reason: object  # None or "candidate_mutated" | "validator_changed" | "conflict"


def _registry(raw, envelope, candidate_root):
    """Registry (operator-owned bytes, sha256 == envelope.validator_digest):
    {"schema_version": 1, "validators_dir": abs path outside the candidate,
     "checks": [{"check_id": envelope check ID, "criteria": [envelope criterion IDs],
                 "script": relative .py in validators_dir, "script_sha256": 64 hex,
                 "timeout_seconds": int 1..envelope.verification_timeout_seconds,
                 "failure_codes": [allowlisted IDs], "falsifies": envelope hypothesis ID|null}],
     "manual_criteria": [envelope criterion IDs needing operator evidence]}
    """
    if not isinstance(raw, (bytes, bytearray)) or hashlib.sha256(raw).hexdigest() != envelope.validator_digest:
        raise EvidenceError("registry_digest_mismatch")
    try:
        reg = strict_json(raw)
    except ContractError:
        raise EvidenceError("registry_malformed") from None
    ok = isinstance(reg, dict) and set(reg) == REGISTRY_KEYS and type(reg["schema_version"]) is int \
        and reg["schema_version"] == 1 and isinstance(reg["validators_dir"], str) \
        and os.path.isabs(reg["validators_dir"]) and isinstance(reg["checks"], list) \
        and _id_list(reg["manual_criteria"], envelope.criteria, empty_ok=True)
    for c in reg["checks"] if ok else ():
        ok = ok and isinstance(c, dict) and set(c) == CHECK_KEYS and isinstance(c["check_id"], str) \
            and c["check_id"] in envelope.check_ids and _id_list(c["criteria"], envelope.criteria) \
            and isinstance(c["script"], str) and c["script"].endswith(".py") \
            and isinstance(c["script_sha256"], str) and bool(SHA256.match(c["script_sha256"])) \
            and type(c["timeout_seconds"]) is int \
            and 1 <= c["timeout_seconds"] <= envelope.verification_timeout_seconds \
            and isinstance(c["failure_codes"], list) and len(set(map(str, c["failure_codes"]))) == len(
                c["failure_codes"]) and all(isinstance(f, str) and ID.match(f) and f not in FIXED_CODES
                                            for f in c["failure_codes"]) \
            and (c["falsifies"] is None or (isinstance(c["falsifies"], str)
                                            and c["falsifies"] in envelope.hypothesis_ids))
    if ok:
        ids = [c["check_id"] for c in reg["checks"]]
        covered = {k for c in reg["checks"] for k in c["criteria"]} | set(reg["manual_criteria"])
        ok = len(set(ids)) == len(ids) and covered >= set(envelope.criteria)
    if not ok:
        raise EvidenceError("registry_malformed")
    vdir = os.path.realpath(reg["validators_dir"])
    for other in (candidate_root, envelope.project_root):  # worker-writable areas
        if _overlap(vdir, os.path.realpath(other)):
            raise EvidenceError("registry_untrusted")
    for c in reg["checks"]:
        try:
            if not os.path.isfile(resolve_owned(vdir, c["script"])):
                raise EvidenceError("registry_malformed")
        except (ContractError, OSError):
            raise EvidenceError("registry_malformed") from None
    return reg, vdir


FIXED_CODES = frozenset({"timeout", "launch_failed", "unclassified_failure", "output_limit",
                         "containment_failed", "survivors", "deadline", "owner_lost"})


def _id_list(value, allowed, empty_ok=False):
    return isinstance(value, list) and (empty_ok or bool(value)) \
        and all(isinstance(v, str) and v in allowed for v in value) and len(set(value)) == len(value)


def _overlap(a, b):
    try:
        return _inside(a, b) or _inside(b, a)
    except ValueError:  # different drives never overlap
        return False


OUTPUT_CAP = 1 << 20  # per stream per check; the retained log is truncated to this bound
ENV_KEEP = ("SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT", "PATH", "NUMBER_OF_PROCESSORS",
            "PROCESSOR_ARCHITECTURE")


def _guard_reason(deadline, clock, owner_guard):
    if owner_guard is not None and not owner_guard():
        return "owner_lost"
    if deadline is not None and clock() >= deadline:
        return "deadline"
    return None


def _run_contained(argv, out, stem, timeout, *, deadline=None, clock=time.time,
                   owner_guard=None, on_deadline=None):
    """Run argv in a fresh owned Job; returns (exit code | fixed code, quiescence proven)."""
    reason = _guard_reason(deadline, clock, owner_guard)
    if reason:
        if reason == "deadline" and on_deadline is not None:
            on_deadline()
        return reason, reason == "deadline"
    job_name = "agentcontrol-validator-" + uuid.uuid4().hex
    control = os.path.join(out, stem + ".control.jsonl")

    def record(state, child=None):
        if owner_guard is not None and not owner_guard():
            return
        data = {"schema": 1, "state": state, "job_name": job_name,
                "pid": None if child is None else child.pid,
                "birth": None if child is None else child.birth}
        with open(control, "ab") as stream:
            stream.write(canonical(data) + b"\n")
            stream.flush()
            os.fsync(stream.fileno())

    try:
        record("admitted")
        job = WindowsJob.create(job_name)
    except (ProcessError, OSError):
        return "containment_failed", None  # unsupported/uncertain: fail closed, never uncontained
    tmp = os.path.join(out, stem + ".tmp")
    os.mkdir(tmp)
    env = {k: os.environ[k] for k in ENV_KEEP if k in os.environ}  # no gateway credentials
    env.update(TEMP=tmp, TMP=tmp)
    logs = [os.path.join(out, stem + ext) for ext in (".out", ".err")]
    child, result = None, "containment_failed"
    try:
        reason = _guard_reason(deadline, clock, owner_guard)
        if reason == "owner_lost":
            return reason, False
        child = job.create_suspended(argv, cwd=out, env=env, stdout_path=logs[0], stderr_path=logs[1])
        record("suspended", child)
        reason = _guard_reason(deadline, clock, owner_guard)
        if reason == "owner_lost":
            return reason, False
        if reason != "deadline":
            child.resume()
        end = time.monotonic() + timeout
        while True:
            reason = _guard_reason(deadline, clock, owner_guard)
            if reason is not None:
                if reason == "owner_lost":
                    return reason, False  # a stale owner cannot control this Job
                if on_deadline is not None:
                    on_deadline()
                result = reason
                break
            rc = child.wait(0.02)
            if any(os.path.getsize(p) > OUTPUT_CAP for p in logs):
                result = "output_limit"
                break
            if rc is not None:
                result = rc
                break
            if time.monotonic() >= end:
                result = "timeout"
                break
        drained = isinstance(result, int) and job.wait_empty(1.0)  # kernel count lags exit briefly
        if not drained and not job.is_empty():
            # The Job was created by this call, so stopping it is within verification authority.
            guard = lambda: ((owner_guard is None or owner_guard())
                             and (deadline is None or clock() < deadline + 60))
            remaining = 5.0 if deadline is None else max(0.0, min(5.0, deadline + 60 - clock()))
            if not guard() or remaining <= 0 or job.terminate(guard, timeout=remaining)["outcome"] != "exited":
                return result, False
            result = result if isinstance(result, str) else "survivors"
        record("quiescent" if job.is_empty() else "unknown", child)
        return result, job.is_empty()
    except (ProcessError, OSError):
        try:
            guard = lambda: ((owner_guard is None or owner_guard())
                             and (deadline is None or clock() < deadline + 60))
            remaining = 5.0 if deadline is None else max(0.0, min(5.0, deadline + 60 - clock()))
            return result, guard() and remaining > 0 and job.terminate(guard, timeout=remaining)["outcome"] == "exited"
        except ProcessError:
            return result, False
    finally:
        if child is not None:
            child.close()
        job.close()
        if owner_guard is None or owner_guard():
            for p in logs:
                if os.path.exists(p) and os.path.getsize(p) > OUTPUT_CAP:
                    os.truncate(p, OUTPUT_CAP)


def _script_digests(reg, vdir):
    out = []
    budget = _hash_budget(16 * 1024 * 1024, 128, MAX_HASH_SECONDS, "validator_limit")
    for c in reg["checks"]:
        try:
            out.append(_file_sha(resolve_owned(vdir, c["script"]), budget=budget))
        except (ContractError, OSError):
            out.append(None)
    return out


def preflight_registry(raw, envelope, artifact_root):
    """Validate protected operator definitions without creating files or running a check."""
    reg, directory = _registry(raw, envelope, os.fspath(artifact_root))
    if any(d != c["script_sha256"] for d, c in zip(_script_digests(reg, directory), reg["checks"])):
        raise EvidenceError("validator_changed")
    return {"registry_sha256": hashlib.sha256(raw).hexdigest(),
            "check_ids": tuple(c["check_id"] for c in reg["checks"])}


def evaluate_acceptance(candidate, registry_raw, envelope, out_dir, manual_evidence=None, *,
                        deadline=None, clock=time.time, owner_guard=None, on_deadline=None,
                        identity_provider=None):
    """Run the fixed approved validators on the exact integrated candidate.

    manual_evidence: {criterion_id: 64-hex operator evidence digest} from the trusted operator
    path only. out_dir is created exclusively and keeps raw validator stdout per check.
    """
    schema3 = getattr(envelope, "schema_version", 1) == 3
    if schema3:
        if not callable(identity_provider):
            raise EvidenceError("identity_provider_required")
        if not callable(owner_guard):
            raise EvidenceError("owner_lost")
        supplied_guard = owner_guard

        def owner_guard():  # schema 3: only a literal True proves ownership
            try:
                return supplied_guard() is True
            except Exception:  # an owner that cannot answer is a lost owner
                return False
        if not isinstance(candidate, EvaluationSnapshot3) or (
                not candidate.conflict_run_id and not (
                    isinstance(candidate.candidate_sha256, str)
                    and SHA256.fullmatch(candidate.candidate_sha256))):
            raise EvidenceError("candidate_malformed")
    elif identity_provider is not None:
        raise EvidenceError("identity_provider_forbidden")

    def protected_reason():
        reason = _guard_reason(deadline, clock, owner_guard)
        if reason is None:
            current = identity_provider(candidate.tree)
            reason = _guard_reason(deadline, clock, owner_guard)
            if reason is None and current != candidate.candidate_sha256:
                reason = "candidate_mutated"
        return reason

    reg, vdir = _registry(registry_raw, envelope, candidate.candidate_dir)
    digests = _script_digests(reg, vdir)
    if any(d != c["script_sha256"] for d, c in zip(digests, reg["checks"])):
        raise EvidenceError("validator_changed")
    reason = _guard_reason(deadline, clock, owner_guard)
    if reason is not None:
        if reason == "deadline" and on_deadline is not None:
            on_deadline()
        return AcceptanceFacts(candidate.candidate_sha256, hashlib.sha256(registry_raw).hexdigest(),
                               False, (), tuple({"check_id": c["check_id"], "status": "not_run",
                                                 "failure_code": None} for c in reg["checks"]), (), reason)
    out = os.path.abspath(os.fspath(out_dir))
    try:
        os.mkdir(out)
    except FileExistsError:
        raise EvidenceError("output_exists") from None
    before = (None if candidate.conflict_run_id else
              candidate.candidate_sha256 if schema3 else candidate_identity(candidate.tree))
    invalid = "conflict" if before is None else (
        "candidate_mutated" if before != candidate.candidate_sha256 else None)
    if schema3 and invalid is None:
        invalid = protected_reason()
    checks, transitions = [], []
    for c in reg["checks"]:
        status, code = "not_run", None
        reason = _guard_reason(deadline, clock, owner_guard)
        if reason is not None:
            invalid = reason
            if reason == "deadline" and on_deadline is not None:
                on_deadline()
        if schema3 and invalid is None:
            invalid = protected_reason()
        if invalid is None:
            log = os.path.join(out, c["check_id"] + ".out")
            rc, quiet = _run_contained([sys.executable, "-I", "-B", os.path.join(vdir, c["script"]),
                                        os.fspath(candidate.tree)], out, c["check_id"], c["timeout_seconds"],
                                        deadline=deadline, clock=clock, owner_guard=owner_guard,
                                        on_deadline=on_deadline)
            if quiet is not True:  # survivors or no containment: nothing here can be accepted
                invalid = "containment_unavailable" if quiet is None else "survivors"
                rc = rc if isinstance(rc, str) else "survivors"
            if rc in ("deadline", "owner_lost"):
                invalid = rc
            status = "pass" if rc == 0 and quiet is True else "fail"
            if isinstance(rc, str):
                code = rc
            elif rc != 0:
                with open(log, "rb") as fh:
                    fh.seek(max(0, os.path.getsize(log) - OUTPUT_TAIL))
                    lines = fh.read().decode("utf-8", "replace").split()
                code = lines[-1] if lines and lines[-1] in c["failure_codes"] else "unclassified_failure"
            if status == "fail" and c["falsifies"] and code in c["failure_codes"]:
                transitions.append({"hypothesis_id": c["falsifies"], "check_id": c["check_id"],
                                    "from": "unresolved", "to": "falsified"})
            if schema3 and invalid is None:
                invalid = protected_reason()
        checks.append({"check_id": c["check_id"], "status": status, "failure_code": code})
    if invalid is None and not schema3 and candidate_identity(candidate.tree) != before:
        invalid = "candidate_mutated"
    if schema3 and invalid is None:
        invalid = protected_reason()
    if invalid is None and _script_digests(reg, vdir) != digests:
        invalid = "validator_changed"
    manual = manual_evidence or {}
    satisfied = []
    for crit in envelope.criteria:
        covering = [k for k, c in zip(checks, reg["checks"]) if crit in c["criteria"]]
        if crit in reg["manual_criteria"]:
            good = (isinstance(manual.get(crit), str) and bool(SHA256.fullmatch(manual[crit]))
                    and all(k["status"] == "pass" for k in covering))
        else:
            good = bool(covering) and all(k["status"] == "pass" for k in covering)
        if good and invalid is None:
            satisfied.append(crit)
    if schema3 and invalid is None:
        invalid = protected_reason()
        if invalid is not None:
            satisfied = []
    if schema3 and invalid == "deadline" and on_deadline is not None:
        on_deadline()
    return AcceptanceFacts(before, hashlib.sha256(registry_raw).hexdigest(),
                           invalid is None and len(satisfied) == len(envelope.criteria),
                           tuple(satisfied), tuple(checks),
                           tuple(transitions) if invalid is None else (), invalid)


def _file_sha(path, *, budget=None):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            if budget is not None:
                _hash_tick(budget, size=len(chunk))
            h.update(chunk)
    return h.hexdigest()

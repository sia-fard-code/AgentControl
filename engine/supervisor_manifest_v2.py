"""Supervisor protocol-2 manifest (opt-in, resilient-v1). NOT routed automatically.

Extends v1 typed narrowing with a tagged candidate, owned run_dir layout, seed and
recovery-contract pins. Not a sandbox: no process containment, mcp is always "none".
"""
import hashlib
import json
import math
import os
import stat
from dataclasses import dataclass
from types import MappingProxyType

from supervisor_contract import ContractError, validate_owned_reference
from supervisor_contract_v3 import _candidate
from supervisor_policy import (_HEX64, _ID, _LABEL, Boundary, ManifestError, _inside,
                               _same_path, normalize_write_path, policy_sha256)

SCHEMA = "agentcontrol.supervisor-manifest/v2"
PROFILE = "resilient-v1"
MAX_MANIFEST_BYTES = 64 * 1024
MAX_CONTRACT_BYTES = 64 * 1024
FIELDS = ("schema", "root", "policy_sha256", "orchestration_id", "attempt_id", "launch_id",
          "run_id", "role", "write_paths", "commands", "mcp", "max_iters",
          "envelope_sha256", "candidate", "seed_candidate", "seed_ref", "seed_receipt",
          "candidate_root", "run_dir", "profile", "recovery_contract_ref",
          "recovery_contract_sha256")
WORKER_ONLY = ("candidate_root", "seed_candidate", "seed_ref", "seed_receipt",
               "recovery_contract_ref", "recovery_contract_sha256")


@dataclass(frozen=True)
class Manifest2:
    schema: str
    root: str
    policy_sha256: str
    orchestration_id: str
    attempt_id: str
    launch_id: str
    run_id: str
    role: str
    write_paths: tuple
    commands: tuple
    mcp: str
    max_iters: int
    envelope_sha256: str
    candidate: object
    seed_candidate: str | None
    seed_ref: str | None
    seed_receipt: str | None
    candidate_root: str | None
    run_dir: str
    profile: str
    recovery_contract_ref: str | None
    recovery_contract_sha256: str | None


def capabilities2():
    """Read-only protocol-2 advertisement; v1 capabilities() is untouched."""
    return MappingProxyType({
        "protocol": "agentcontrol.supervisor", "version": 2, "manifest_schema": SCHEMA,
        "manifest_fields": FIELDS,
        "roles": MappingProxyType({
            "planner": "--oneshot, no write_paths/commands/candidate/seed/contract",
            "worker": "--write on pre-materialized run_dir/candidate (not --worktree), "
                      "native tools, --max-iters <= max_iters, --recovery-contract pin"}),
        "features": MappingProxyType({
            "profile": PROFILE, "tagged_candidate": True, "owned_candidate_root": True,
            "seed_verification_callback": True, "recovery_contract_pin": True,
            "manifest_sha256_pin": True, "policy_sha256_binding": True,
            "launch_run_identity_binding": True, "exact_command_argv": True,
            "write_path_boundary": True, "mcp": "none",
            "process_containment": False, "sandbox": False}),
    })


def _pairs(pairs):
    out = {}
    for k, v in pairs:
        if k in out:
            raise ManifestError("duplicate manifest key")    # no value echo
        out[k] = v
    return out


def _finite(s):
    v = float(s)
    if not math.isfinite(v):
        raise ManifestError("manifest number is not finite")
    return v


def _const(_):
    raise ManifestError("manifest number is not finite")


def read_pinned(path, digest, limit, what):
    """Bounded read of a regular non-reparse file; pin RAW bytes before any parse."""
    if not isinstance(digest, str) or not _HEX64.fullmatch(digest):
        raise ManifestError(f"{what} sha256 must be 64 lowercase hex chars")
    if not isinstance(path, str) or not os.path.isabs(path):
        raise ManifestError(f"{what} must be an absolute path")
    try:
        st = os.lstat(path)
        if not stat.S_ISREG(st.st_mode) or getattr(st, "st_file_attributes", 0) & 0x400:
            raise ManifestError(f"{what} must be a regular file, not a link")
        with open(path, "rb") as fh:
            data = fh.read(limit + 1)
    except OSError as e:
        raise ManifestError(f"cannot read {what}: {type(e).__name__}")
    if len(data) > limit:
        raise ManifestError(f"{what} is too large")
    if hashlib.sha256(data).hexdigest() != digest:
        raise ManifestError(f"{what} sha256 mismatch")
    return data


def _sha(m, k, nullable=False):
    if nullable and m[k] is None:
        return None
    if not isinstance(m[k], str) or not _HEX64.fullmatch(m[k]):
        raise ManifestError(f"manifest {k} must be 64 lowercase hex chars")
    return m[k]


def _ref(m, k):
    v = m[k]
    try:
        if not isinstance(v, str) or "\\" in v:
            raise ContractError("bad")
        validate_owned_reference(v)
    except (ContractError, ValueError, TypeError):
        raise ManifestError(f"manifest {k} must be a normalized relative owned reference")
    return v


def load_manifest2(path, digest):
    """Read, pin and strictly parse a v2 manifest; returns immutable Manifest2."""
    data = read_pinned(path, digest, MAX_MANIFEST_BYTES, "supervisor manifest")
    try:
        m = json.loads(data.decode("utf-8"), object_pairs_hook=_pairs,
                       parse_float=_finite, parse_constant=_const)
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        raise ManifestError(f"supervisor manifest is not strict JSON: {type(e).__name__}")
    if not isinstance(m, dict) or set(m) != set(FIELDS):
        raise ManifestError("supervisor manifest fields must be exactly: " + ", ".join(FIELDS))
    return _shape(m)


def _shape(m):
    if m["schema"] != SCHEMA:
        raise ManifestError(f"unsupported manifest schema; this loader speaks {SCHEMA}")
    if m["profile"] != PROFILE or m["mcp"] != "none":
        raise ManifestError("manifest v2 requires profile resilient-v1 and mcp none")
    for k in ("orchestration_id", "attempt_id", "launch_id", "run_id"):
        if not isinstance(m[k], str) or not _ID.fullmatch(m[k]):
            raise ManifestError(f"manifest {k} must be a short identifier")
    for k in ("policy_sha256", "envelope_sha256"):
        _sha(m, k)
    for k in ("root", "run_dir"):
        if not isinstance(m[k], str) or not os.path.isabs(m[k]):
            raise ManifestError(f"manifest {k} must be an absolute path")
    if _inside(m["run_dir"], m["root"]) or _inside(m["root"], m["run_dir"]):
        raise ManifestError("manifest run_dir must be outside the policy root")
    n = m["max_iters"]
    if type(n) is not int or not 1 <= n <= 200:
        raise ManifestError("manifest max_iters must be an integer from 1 to 200")
    try:
        cand = _candidate(m["candidate"])
    except (ContractError, ValueError, TypeError, AttributeError):
        raise ManifestError("manifest candidate must be an exact tagged descriptor")
    if not isinstance(m["write_paths"], list) or not isinstance(m["commands"], list):
        raise ManifestError("manifest write_paths and commands must be lists")
    wp = tuple(normalize_write_path(p) for p in m["write_paths"])
    for argv in m["commands"]:
        if (not isinstance(argv, list) or not argv
                or not all(isinstance(a, str) and a for a in argv)):
            raise ManifestError("each manifest command must be a non-empty argv list")
        if any(c in a for a in argv for c in "*?[]"):
            raise ManifestError("manifest commands are exact argv; glob characters refused")
    cmds = tuple(tuple(a) for a in m["commands"])
    if m["role"] == "planner":
        if wp or cmds or n != 1 or any(m[k] is not None for k in WORKER_ONLY):
            raise ManifestError("planner role is readonly: no write/command/candidate/seed/"
                                "contract fields and max_iters 1")
    elif m["role"] == "worker":
        if not wp:
            raise ManifestError("worker role requires non-empty write_paths")
        for k in ("seed_candidate", "seed_receipt", "recovery_contract_sha256"):
            _sha(m, k)
        _ref(m, "seed_ref")
        _ref(m, "recovery_contract_ref")
        if not isinstance(m["candidate_root"], str) or not _same_path(
                m["candidate_root"], os.path.join(m["run_dir"], "candidate")) \
                or os.path.normpath(m["candidate_root"]) != os.path.normpath(
                    os.path.join(m["run_dir"], "candidate")):
            raise ManifestError("worker candidate_root must be exactly run_dir/candidate")
        for k in ("seed_ref", "recovery_contract_ref"):
            if m[k].split("/")[0].lower() == "candidate":
                raise ManifestError(f"manifest {k} must be outside the candidate root")
    else:
        raise ManifestError("manifest role must be 'planner' or 'worker'")
    return Manifest2(**dict(m, write_paths=wp, commands=cmds, candidate=cand))


@dataclass(frozen=True)
class Boundary2(Boundary):
    """v1 Boundary narrowing (check_write/check_command) plus owned root and manifest."""
    candidate_root: str | None = None
    manifest: Manifest2 | None = None


def _plain_dir(path):
    try:
        st = os.lstat(path)
    except OSError:
        return False
    return (stat.S_ISDIR(st.st_mode) and not getattr(st, "st_file_attributes", 0) & 0x400
            and os.path.normcase(os.path.realpath(path)) == os.path.normcase(
                os.path.abspath(path)))


def validate_launch2(m, args, *, manifest_path, main_root, policy_file, launch_id, run_id,
                     mode, allowed_cmds, permitted, verify_seed):
    """Bind an immutable v2 manifest to this launch; return Boundary2 or raise ManifestError.

    `verify_seed(m, candidate_root)` is the trusted runtime seed check for workers; only an
    exact True admits. No Git, process or model call happens here.
    """
    if not isinstance(m, Manifest2):
        raise ManifestError("validate_launch2 requires a loaded v2 manifest")
    if not _same_path(m.root, main_root):
        raise ManifestError("manifest root is not this canonical -C root")
    if _inside(manifest_path, main_root) or not _same_path(
            os.path.dirname(manifest_path), m.run_dir):
        raise ManifestError("supervisor manifest must live in run_dir outside the repository")
    if not _plain_dir(m.run_dir) or _inside(m.run_dir, main_root):
        raise ManifestError("manifest run_dir must be a plain directory outside the root")
    if policy_sha256(main_root, policy_file) != m.policy_sha256:
        raise ManifestError("repository policy digest does not match the manifest")
    if launch_id != m.launch_id or run_id != m.run_id:
        raise ManifestError("explicit --launch-id and --transcript run id must match the manifest")
    if args.allow_prompt_tools or args.verify_command:
        raise ManifestError("--allow-prompt-tools and --verify-command are refused in v2")
    t = args.transcript
    if not isinstance(t, str) or not os.path.isabs(t) or not _same_path(
            os.path.dirname(t), m.run_dir):
        raise ManifestError("--transcript must be an absolute path directly in run_dir")
    if m.role == "planner":
        if not args.oneshot:
            raise ManifestError("planner role requires --oneshot")
        if (args.write or args.worktree or args.patch is not None
                or args.max_iters not in (None, 1) or args.recovery_contract is not None):
            raise ManifestError("planner role forbids writable execution")
        return Boundary2(manifest=m)
    if args.oneshot or not args.write or args.worktree or args.patch is not None:
        raise ManifestError("worker role requires --write (not --worktree/--patch/--oneshot)")
    if mode != "native":
        raise ManifestError("worker role requires native tools")
    if type(args.max_iters) is not int or not 1 <= args.max_iters <= m.max_iters:
        raise ManifestError("worker role requires explicit --max-iters <= manifest max_iters")
    if (not isinstance(args.label, str) or not _LABEL.fullmatch(args.label)
            or args.label.endswith(".") or m.attempt_id not in args.label):
        raise ManifestError("worker --label must be an identifier containing attempt_id")
    root = os.path.realpath(m.candidate_root)
    if not _plain_dir(m.candidate_root) or not _same_path(root, os.path.join(m.run_dir,
                                                                              "candidate")):
        raise ManifestError("worker candidate root must be an existing plain run_dir/candidate")
    if _inside(manifest_path, root) or _inside(t, root):
        raise ManifestError("manifest and transcript must be outside the candidate root")
    expected = os.path.join(m.run_dir, *m.recovery_contract_ref.split("/"))
    rc = args.recovery_contract
    if not isinstance(rc, str) or not os.path.isabs(rc) or os.path.normcase(
            os.path.normpath(rc)) != os.path.normcase(os.path.normpath(expected)) \
            or not _inside(rc, m.run_dir) or _inside(rc, root):
        raise ManifestError("--recovery-contract must be the manifest's owned contract ref")
    read_pinned(rc, m.recovery_contract_sha256, MAX_CONTRACT_BYTES, "recovery contract")
    for argv in m.commands:
        if any(a.startswith("/") or os.path.isabs(a) or ".." in a.replace("\\", "/").split("/")
               for a in argv):
            raise ManifestError("manifest command names an absolute or escaping path")
        if not any(permitted(rule, list(argv)) for rule in allowed_cmds):
            raise ManifestError("manifest command is wider than the repository allowlist")
    if not callable(verify_seed) or verify_seed(m, root) is not True:
        raise ManifestError("worker seed verification did not return exact True")
    return Boundary2(m.write_paths, m.commands, candidate_root=root, manifest=m)

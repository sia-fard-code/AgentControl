"""Supervisor protocol v1: pinned capability manifest and per-run boundary.

Opt-in only; delegate.py imports this when a --supervisor-* flag is present.
This is NOT a sandbox: the boundary limits the engine's own file tools and
command argv, but an approved command (e.g. a test runner executing worker
code) can still act outside the checkout. No process containment here.
"""
import hashlib
import json
import os
import re
import subprocess
from dataclasses import dataclass

SCHEMA = "agentcontrol.supervisor-manifest/v1"
MAX_MANIFEST_BYTES = 64 * 1024
FIELDS = ("schema", "root", "policy_sha256", "baseline_commit", "orchestration_id",
          "attempt_id", "launch_id", "run_id", "role", "write_paths", "commands",
          "mcp", "max_iters")
PROTECTED = {".git", ".claude"}
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_LABEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")   # one portable path segment
_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_COMMIT = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")


class ManifestError(ValueError):
    """The manifest or its launch combination is refused; message is safe to show."""


def git_environment(extra=None):
    """Internal metadata/materialization Git must not inherit routing, hooks or lazy fetch."""
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith("GIT_")}
    env.update(GIT_TERMINAL_PROMPT="0", GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull,
               GIT_CONFIG_SYSTEM=os.devnull, GIT_NO_LAZY_FETCH="1")
    if extra:
        env.update(extra)
    return env


def capabilities():
    """Exactly what v1 implements; a supervisor must refuse anything absent here."""
    return {
        "protocol": "agentcontrol.supervisor", "version": 1, "manifest_schema": SCHEMA,
        "flags": ["--supervisor-capabilities", "--supervisor-manifest",
                  "--supervisor-manifest-sha256"],
        "manifest_fields": list(FIELDS),
        "roles": {"planner": "--oneshot, no write_paths/commands",
                  "worker": "--worktree (not --write), native tools, --max-iters <= max_iters"},
        "features": {
            "manifest_sha256_pin": True, "policy_sha256_binding": True,
            "baseline_commit_binding": True, "launch_run_identity_binding": True,
            "single_request_planner": True,
            "write_path_boundary": True, "exact_command_argv": True,
            "owned_worktree_creation": True, "mcp": "none",
            "process_containment": False, "sandbox": False,
        },
    }


def _no_duplicates(pairs):
    out = {}
    for k, v in pairs:
        if k in out:
            raise ManifestError("duplicate manifest key")    # no value echo
        out[k] = v
    return out


def sha256_file(path):
    with open(path, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()


def load_manifest(path, digest):
    """Read, pin and strictly parse the manifest. Returns the validated dict."""
    if not isinstance(digest, str) or not _HEX64.fullmatch(digest):
        raise ManifestError("--supervisor-manifest-sha256 must be 64 lowercase hex chars")
    if not os.path.isabs(path):
        raise ManifestError("--supervisor-manifest must be an absolute path")
    try:
        with open(path, "rb") as fh:
            data = fh.read(MAX_MANIFEST_BYTES + 1)
    except OSError as e:
        raise ManifestError(f"cannot read supervisor manifest: {type(e).__name__}")
    if len(data) > MAX_MANIFEST_BYTES:
        raise ManifestError("supervisor manifest is too large")
    if hashlib.sha256(data).hexdigest() != digest:
        raise ManifestError("supervisor manifest sha256 mismatch")
    try:
        m = json.loads(data.decode("utf-8"), object_pairs_hook=_no_duplicates)
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        raise ManifestError(f"supervisor manifest is not strict JSON: {type(e).__name__}")
    if not isinstance(m, dict) or set(m) != set(FIELDS):
        raise ManifestError("supervisor manifest fields must be exactly: " + ", ".join(FIELDS))
    _check_shape(m)
    return m


def _check_shape(m):
    if m["schema"] != SCHEMA:
        raise ManifestError(f"unsupported manifest schema; this engine speaks {SCHEMA}")
    for k in ("orchestration_id", "attempt_id", "launch_id", "run_id"):
        if not isinstance(m[k], str) or not _ID.fullmatch(m[k]):
            raise ManifestError(f"manifest {k} must be a short identifier")
    if not isinstance(m["root"], str) or not os.path.isabs(m["root"]):
        raise ManifestError("manifest root must be an absolute path")
    if not isinstance(m["policy_sha256"], str) or not _HEX64.fullmatch(m["policy_sha256"]):
        raise ManifestError("manifest policy_sha256 must be 64 lowercase hex chars")
    if (not isinstance(m["baseline_commit"], str)
            or not _COMMIT.fullmatch(m["baseline_commit"])):
        raise ManifestError("manifest baseline_commit must be a full lowercase commit id")
    if m["role"] not in ("planner", "worker"):
        raise ManifestError("manifest role must be 'planner' or 'worker'")
    n = m["max_iters"]
    if not isinstance(n, int) or isinstance(n, bool) or not 1 <= n <= 200:
        raise ManifestError("manifest max_iters must be an integer from 1 to 200")
    if m["mcp"] != "none":
        raise ManifestError("protocol v1 supports only mcp: \"none\"")
    if not isinstance(m["write_paths"], list) or not isinstance(m["commands"], list):
        raise ManifestError("manifest write_paths and commands must be lists")
    m["write_paths"] = [normalize_write_path(p) for p in m["write_paths"]]
    for argv in m["commands"]:
        if (not isinstance(argv, list) or not argv
                or not all(isinstance(a, str) and a for a in argv)):
            raise ManifestError("each manifest command must be a non-empty argv list")
        if any(c in a for a in argv for c in "*?[]"):
            raise ManifestError("manifest commands are exact argv; glob characters refused")
    if m["role"] == "planner" and (m["write_paths"] or m["commands"]):
        raise ManifestError("planner role carries no write_paths or commands")


def normalize_write_path(p):
    """Repo-relative '/'-separated path; a trailing '/' marks a directory prefix."""
    if not isinstance(p, str) or not p or "\x00" in p:
        raise ManifestError("write path must be a non-empty string")
    norm = p.replace("\\", "/")
    if norm.startswith("/") or os.path.isabs(p) or re.match(r"^[A-Za-z]:", p):
        raise ManifestError("manifest write path must be repo-relative")
    is_dir = norm.endswith("/")
    parts = [s for s in norm.split("/") if s not in ("", ".")]
    if not parts or ".." in parts:
        raise ManifestError("manifest write path escapes or names the repo root")
    if parts[0].lower() in PROTECTED or any(s.lower() == ".git" for s in parts):
        raise ManifestError("manifest write path names protected metadata")
    return "/".join(parts) + ("/" if is_dir else "")


@dataclass(frozen=True)
class Boundary:
    """Per-run narrowing applied by Runner; never widens repo policy."""
    write_paths: tuple = ()
    commands: tuple = ()

    def check_write(self, root, full):
        """Raise ValueError unless resolved `full` is an allowed file under `root`."""
        root = os.path.realpath(root)
        full = os.path.realpath(full)          # symlinks resolved before matching
        rel = os.path.relpath(full, root).replace("\\", "/")
        if rel == "." or rel.startswith("../") or os.path.isabs(rel):
            raise ValueError("refused: write outside the supervised checkout")
        if rel.split("/")[0].lower() in PROTECTED or ".git" in rel.lower().split("/"):
            raise ValueError("refused: write to protected metadata")
        cmp = (lambda s: s.lower()) if os.name == "nt" else (lambda s: s)
        for allowed in self.write_paths:
            if (cmp(rel) == cmp(allowed) if not allowed.endswith("/")
                    else cmp(rel).startswith(cmp(allowed))):
                return
        raise ValueError(f"refused: {rel} is outside the supervisor write boundary")

    def check_command(self, argv):
        return [list(c) for c in self.commands].count(list(argv)) > 0


def _same_path(a, b):
    return os.path.normcase(os.path.realpath(a)) == os.path.normcase(os.path.realpath(b))


def _inside(path, root):
    rel = os.path.relpath(os.path.realpath(path), os.path.realpath(root))
    return rel == "." or not (rel.startswith("..") or os.path.isabs(rel))


def policy_sha256(main_root, policy_file):
    path = os.path.join(main_root, policy_file)
    if not os.path.isfile(path):
        raise ManifestError(f"supervisor mode requires an explicit {policy_file}")
    return sha256_file(path)


def validate_launch(m, args, *, manifest_path, main_root, policy_file, launch_id,
                    run_id, mode, allowed_cmds, permitted):
    """Bind the manifest to this launch; return the run Boundary or raise ManifestError.

    `permitted(rule, argv)` is the engine's inherited allowlist test, so each exact
    selected argv is checked against existing repo rules rather than re-implemented.
    """
    if not _same_path(m["root"], main_root):
        raise ManifestError("manifest root is not this canonical -C root")
    if _inside(manifest_path, main_root):
        raise ManifestError("supervisor manifest must live outside the repository")
    if policy_sha256(main_root, policy_file) != m["policy_sha256"]:
        raise ManifestError("repository policy digest does not match the manifest")
    r = subprocess.run(["git", "-c", "core.hooksPath=", "-c", "core.fsmonitor=false",
                        "rev-parse", "--verify", "--quiet",
                         m["baseline_commit"] + "^{commit}"],
                       cwd=main_root, env=git_environment(), capture_output=True, text=True, timeout=30)
    if r.returncode != 0 or r.stdout.strip() != m["baseline_commit"]:
        raise ManifestError("manifest baseline_commit is not a commit in this repository")
    if launch_id != m["launch_id"] or run_id != m["run_id"]:
        raise ManifestError("explicit --launch-id and --transcript run id must match the manifest")
    if args.allow_prompt_tools or args.verify_command:
        raise ManifestError("--allow-prompt-tools and --verify-command are refused in v1")
    if m["role"] == "planner":
        if not args.oneshot:
            raise ManifestError("planner role requires --oneshot")
        if args.write or args.worktree or args.patch is not None or args.max_iters is not None:
            raise ManifestError("planner role forbids writable execution")
        return Boundary()
    if args.oneshot or args.write or not args.worktree:
        raise ManifestError("worker role requires --worktree (not --write, not --oneshot)")
    if mode != "native":
        raise ManifestError("worker role requires native tools")
    if args.max_iters is None or not 1 <= args.max_iters <= m["max_iters"]:
        raise ManifestError("worker role requires explicit --max-iters <= manifest max_iters")
    if (not isinstance(args.label, str) or not _LABEL.fullmatch(args.label)
            or args.label.endswith(".") or m["attempt_id"] not in args.label):
        raise ManifestError("worker --label must be an identifier containing attempt_id")
    for argv in m["commands"]:
        if any(a.startswith("/") or os.path.isabs(a) or ".." in a.replace("\\", "/").split("/")
               for a in argv):
            raise ManifestError("manifest command names an absolute or escaping path")
        if not any(permitted(rule, argv) for rule in allowed_cmds):
            raise ManifestError("manifest command is wider than the repository allowlist")
    return Boundary(tuple(m["write_paths"]), tuple(tuple(c) for c in m["commands"]))


def create_owned_worktree(root, label, baseline):
    """New worktree at `baseline` on new branch agent/<label>; never deletes or reuses.

    The directory is reserved by exclusive mkdir, so an existing path holds the
    launch; `git worktree add -b` refuses an existing branch without touching it.
    """
    if not isinstance(label, str) or not _LABEL.fullmatch(label) or label.endswith("."):
        raise ManifestError("worker --label must be an identifier containing attempt_id")
    parent = os.path.realpath(root)
    for seg in (".claude", "worktrees"):       # refuse links BEFORE creating anything
        parent = os.path.join(parent, seg)
        if os.path.lexists(parent):
            if os.path.normcase(os.path.realpath(parent)) != os.path.normcase(parent):
                raise ManifestError("worktree parent is a link or escapes the repository")
        else:
            os.mkdir(parent)
    wt, branch = os.path.join(parent, label), "agent/" + label
    try:
        os.mkdir(wt)
    except FileExistsError:
        raise ManifestError("worktree path for this label already exists; held")
    r = subprocess.run(["git", "-c", "core.hooksPath=", "-c", "core.fsmonitor=false",
                        "worktree", "add", "-b", branch, wt, baseline],
                       cwd=root, env=git_environment(), capture_output=True, text=True, timeout=120)
    if r.returncode != 0:
        try:
            os.rmdir(wt)                       # only our own still-empty reservation
        except OSError:
            pass
        raise ManifestError("could not create owned worktree (branch exists?); held")
    return wt, branch

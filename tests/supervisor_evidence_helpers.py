"""Synthetic temporary-Git fixtures for supervisor evidence tests. No live paths or content."""
import subprocess

GIT_ENV_ARGS = ["-c", "core.autocrlf=false", "-c", "user.name=t", "-c", "user.email=t@example.invalid",
                "-c", "commit.gpgsign=false"]


def git(cwd, *args):
    out = subprocess.run(["git", *GIT_ENV_ARGS, *args], cwd=str(cwd), capture_output=True,
                         timeout=60, check=True)
    return out.stdout.decode()


def write_artifacts(art, run_id, patch, root_identity, task_sha, write_mode="disk",
                    status="verified_by_command", findings=None, patch_ref="agent.patch"):
    """Write a synthetic delegate-shaped completion.json plus patch (and findings) into `art`."""
    import hashlib
    import json
    art.mkdir(parents=True, exist_ok=True)
    (art / patch_ref).write_bytes(patch)
    run = {"run_id": run_id, "task_sha256": task_sha, "root_identity": root_identity,
           "write_mode": write_mode,
           "transcript": {"path": f"{run_id}.md", "prefix_bytes": 1, "sha256": "0" * 64}}
    record = {"run_id": run_id, "verification_status": status, "gate": {}, "run_binding": run,
              "patch_binding": {"run": run, "candidate_seq": 1, "mutation_seq": 1,
                                "source": "worktree", "patch_sha256": hashlib.sha256(patch).hexdigest(),
                                "patch_size": len(patch), "artifact_path": None},
              "candidate_unavailable": None, "review": None}
    (art / "completion.json").write_bytes(json.dumps(record).encode())
    if findings is not None:
        (art / "findings.md").write_bytes(findings.encode())
    return record


def make_registry(vdir, checks, manual=()):
    """checks: [(check_id, criteria, script_text, failure_codes, falsifies, timeout)] -> raw bytes."""
    import hashlib
    from supervisor_helpers import canonical
    vdir.mkdir(parents=True, exist_ok=True)
    entries = []
    for cid, criteria, script, codes, falsifies, timeout in checks:
        (vdir / f"{cid}.py").write_text(script, encoding="utf-8")
        entries.append({"check_id": cid, "criteria": list(criteria), "script": f"{cid}.py",
                        "script_sha256": hashlib.sha256((vdir / f"{cid}.py").read_bytes()).hexdigest(),
                        "timeout_seconds": timeout, "failure_codes": codes, "falsifies": falsifies})
    return canonical({"schema_version": 1, "validators_dir": str(vdir.resolve()),
                      "checks": entries, "manual_criteria": list(manual)})


def make_envelope(root, registry_raw, criteria, check_ids, hypothesis_ids=("H1",)):
    import hashlib
    from supervisor_helpers import envelope_bytes, load
    contract = load("supervisor_contract")
    data_over = {"validator_digest": hashlib.sha256(registry_raw).hexdigest(),
                 "criteria": list(criteria), "check_ids": list(check_ids),
                 "hypothesis_ids": list(hypothesis_ids)}
    raw, _ = envelope_bytes(root, **data_over)
    data = contract.strict_json(raw)
    if "max_usage" not in contract.ENVELOPE_KEYS:  # tolerate core before/after the usage repair
        data.pop("max_usage", None)
    raw = contract.canonical(data)
    return contract.load_envelope(raw, hashlib.sha256(raw).hexdigest(), root)


def make_repo(path, files):
    """Create a repo whose first commit holds `files` ({relpath: bytes}); return its commit."""
    path.mkdir(parents=True, exist_ok=True)
    git(path, "init", "-q")
    for rel, data in files.items():
        target = path / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    git(path, "add", "-A")
    git(path, "commit", "-q", "-m", "base")
    return git(path, "rev-parse", "HEAD").strip()


def diff_for(repo, edits, *extra):
    """Write edits ({relpath: bytes|None}), return the binary-safe diff, then restore the tree."""
    for rel, data in edits.items():
        target = repo / rel
        if data is None:
            target.unlink()
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
    git(repo, "add", "-A")
    patch = subprocess.run(["git", *GIT_ENV_ARGS, "diff", "--cached", "--binary", *extra],
                           cwd=str(repo), capture_output=True, timeout=60, check=True).stdout
    git(repo, "reset", "-q", "--hard", "HEAD")
    git(repo, "clean", "-qfdx")
    return patch

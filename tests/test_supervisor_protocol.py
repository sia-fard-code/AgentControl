"""Opt-in supervisor protocol v1: probe, pinned manifest, Runner boundary, worktrees.

Offline only: fake gateway (test_engine_reasoning harness), temporary HOME,
index, policy, manifest and git repositories. Synthetic text only.
"""
import hashlib
import json
import os
import subprocess
import sys
import uuid

import pytest

from test_engine_reasoning import engine, harness, _finish_reply  # noqa: F401

LAUNCH_ID = "c356dcf3-66ce-4f96-8952-89584b92db99"
POLICY = {"allowed_cmds": [["git", "status"], ["git", "log"], ["python", "-m", "pytest"]]}


def git(root, *argv):
    r = subprocess.run(["git", *argv], cwd=root, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    return r.stdout.strip()


def sha(data):
    return hashlib.sha256(data).hexdigest()


@pytest.fixture()
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "home"))
    git(tmp_path, "init", "-q")
    git(tmp_path, "config", "user.email", "t@example.invalid")
    git(tmp_path, "config", "user.name", "t")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.txt").write_text("alpha\n", encoding="utf-8")
    (tmp_path / "README").write_text("synthetic\n", encoding="utf-8")
    git(tmp_path, "add", "-A")
    git(tmp_path, "commit", "-q", "-m", "base")
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude" / "agent-policy.json").write_text(json.dumps(POLICY), encoding="utf-8")
    return tmp_path


@pytest.fixture()
def manifest(repo, tmp_path_factory):
    """Writes a pinned manifest OUTSIDE the repo; returns (writer, cli_args)."""
    outside = tmp_path_factory.mktemp("supervisor")
    policy = (repo / ".claude" / "agent-policy.json").read_bytes()
    base = {
        "schema": "agentcontrol.supervisor-manifest/v1",
        "root": os.path.realpath(repo),
        "policy_sha256": sha(policy),
        "baseline_commit": git(repo, "rev-parse", "HEAD"),
        "orchestration_id": "orch-1", "attempt_id": "att-1",
        "launch_id": LAUNCH_ID, "run_id": "run",
        "role": "worker", "write_paths": ["src/"],
        "commands": [["git", "status"]], "mcp": "none", "max_iters": 10,
    }

    def write(raw=None, digest=None, **over):
        data = raw if raw is not None else json.dumps({**base, **over}).encode()
        path = outside / "manifest.json"
        path.write_bytes(data)
        return ["--supervisor-manifest", str(path),
                "--supervisor-manifest-sha256", digest or sha(data)]
    write.base = base
    write.receipt = ["--launch-receipt", str(outside / "a.launch.jsonl"),
                     "--launch-id", LAUNCH_ID]
    return write


def snapshot(root):
    return sorted(str(p.relative_to(root)) for p in root.rglob("*"))


def test_internal_owned_worktree_does_not_execute_repository_hooks(engine, repo, manifest, tmp_path):
    sp = engine.capability_module("supervisor_policy")
    marker = tmp_path / "hook-marker"
    hook = repo / ".git" / "hooks" / "post-checkout"
    hook.write_text("#!/bin/sh\nprintf synthetic > '" + marker.as_posix() + "'\n")
    hook.chmod(0o700)
    reference = tmp_path / "reference-worktree"
    subprocess.run(["git", "-C", str(repo), "-c", "core.hooksPath=" + str(hook.parent),
                     "worktree", "add", "--detach", str(reference), manifest.base["baseline_commit"]],
                    check=True, capture_output=True)
    assert marker.read_text() == "synthetic"
    marker.unlink()
    sp.create_owned_worktree(str(repo), "owned-no-hooks", manifest.base["baseline_commit"])
    assert not marker.exists()


def test_capability_probe_is_pure_json(engine, tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("CLAUDE_AGENT_RUN_INDEX", str(tmp_path / "index.jsonl"))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(engine, "load_cfg", lambda: pytest.fail("gateway config loaded"))
    monkeypatch.setattr(engine, "load_policy", lambda root: pytest.fail("policy loaded"))
    monkeypatch.setattr(sys, "argv", ["delegate.py", "--supervisor-capabilities",
                                      "-C", str(tmp_path)])
    before = snapshot(tmp_path)
    engine.main()
    out = json.loads(capsys.readouterr().out)
    assert snapshot(tmp_path) == before          # no receipt, index, diagnostics
    assert out["protocol"] == "agentcontrol.supervisor" and out["version"] == 1
    assert out["manifest_schema"] == "agentcontrol.supervisor-manifest/v1"
    assert out["features"]["mcp"] == "none"
    assert out["features"]["exact_command_argv"] is True
    assert out["features"]["process_containment"] is False


LABEL = ("--label", "att-1-w")
WORKER = ("-m", "gpt-6.1-sol", "--worktree", "--max-iters", "5", *LABEL)
BAD_MANIFESTS = {   # case -> (override, fixed case-specific refusal reason)
    "unknown_field": (dict(extra=1), "fields must be exactly"),
    "wrong_schema": (dict(schema="v0"), "unsupported manifest schema"),
    "wrong_root": (dict(root=os.path.realpath(os.sep)), "manifest root is not this"),
    "policy_digest": (dict(policy_sha256="0" * 64), "policy digest does not match"),
    "baseline": (dict(baseline_commit="f" * 40), "baseline_commit is not a commit"),
    "launch_id": (dict(launch_id="00000000-0000-4000-8000-000000000000"),
                  "must match the manifest"),
    "run_id": (dict(run_id="other"), "must match the manifest"),
    "id_newline": (dict(orchestration_id="orch-1\n"), "must be a short identifier"),
    "role": (dict(role="admin"), "role must be 'planner' or 'worker'"),
    "wider_command": (dict(commands=[["node", "x.js"]]), "wider than the repository"),
    "glob_command": (dict(commands=[["python", "-m", "*"]]), "glob characters refused"),
    "empty_command": (dict(commands=[[]]), "non-empty argv list"),
    "mcp_servers": (dict(mcp={"research_browser": ["*"]}), 'only mcp: "none"'),
    "git_write": (dict(write_paths=[".git/"]), "names protected metadata"),
    "claude_write": (dict(write_paths=[".claude/agent-policy.json"]),
                     "names protected metadata"),
    "escape_write": (dict(write_paths=["../outside"]), "escapes or names the repo root"),
    "abs_write": (dict(write_paths=[os.path.realpath(os.sep) + "x"]), "must be repo-relative"),
    "drive_write": (dict(write_paths=["C:outside"]), "must be repo-relative"),
    "bool_iters": (dict(max_iters=True), "max_iters must be an integer"),
    "zero_iters": (dict(max_iters=0), "max_iters must be an integer"),
}


def expect_refused(engine, harness, monkeypatch, tmp_path, reason, *argv):
    """Exit 2 with the fixed supervisor reason, before gateway, index or worktree."""
    monkeypatch.setattr(engine, "load_cfg", lambda: pytest.fail("gateway config loaded"))
    import io
    err = io.StringIO()
    monkeypatch.setattr(sys, "stderr", err)
    argv = list(argv)
    if "--launch-receipt" in argv:
        i = argv.index("--launch-receipt") + 1
        # Each independent rejection probe needs an unused receipt, not a collision.
        argv[i] = os.path.join(os.path.dirname(argv[i]), uuid.uuid4().hex + ".launch.jsonl")
    with pytest.raises(SystemExit) as error:
        harness.run(*argv, replies=[])
    text = err.getvalue()
    assert error.value.code == 2, text
    assert "supervisor refused:" in text and reason in text, text
    assert not (tmp_path / "index.jsonl").exists()
    assert not (tmp_path / ".claude" / "worktrees").exists()
    return text


@pytest.mark.parametrize("case", sorted(BAD_MANIFESTS))
def test_bad_manifest_refused_before_gateway(engine, harness, repo, manifest,
                                             monkeypatch, case):
    over, reason = BAD_MANIFESTS[case]
    expect_refused(engine, harness, monkeypatch, repo, reason, *WORKER, *manifest.receipt,
                   *manifest(**over))


def test_manifest_digest_duplicate_keys_and_pairing(engine, harness, repo, manifest,
                                                    monkeypatch):
    args = manifest()
    expect_refused(engine, harness, monkeypatch, repo, "sha256 mismatch", *WORKER,
                   *manifest.receipt, *args[:3], "a" * 64)    # mismatched pin
    expect_refused(engine, harness, monkeypatch, repo, "must be given together", *WORKER,
                   *manifest.receipt, *args[:2])              # unpaired flag
    expect_refused(engine, harness, monkeypatch, repo, "must be given together", *WORKER,
                   *manifest.receipt, *args[2:])              # pin without manifest
    expect_refused(engine, harness, monkeypatch, repo, "64 lowercase hex", *WORKER,
                   *manifest.receipt, *args[:3], "A" * 64)    # malformed pin
    dup = json.dumps(manifest.base)[:-1] + ', "role": "secret-value"}'
    text = expect_refused(engine, harness, monkeypatch, repo, "duplicate manifest key",
                          *WORKER, *manifest.receipt, *manifest(raw=dup.encode()))
    assert "secret-value" not in text and "'role'" not in text   # no value echo


@pytest.mark.parametrize("flags,reason", [
    (("--write", "--max-iters", "5"), "worker role requires --worktree"),
    (("--worktree",), "explicit --max-iters"),               # unbounded
    (("--worktree", "--max-iters", "99"), "explicit --max-iters"),
    (("--worktree", "--max-iters", "5", "--tool-mode", "prompt", "--allow-prompt-tools"),
     "--allow-prompt-tools and --verify-command are refused"),
    (("--worktree", "--max-iters", "5", "--tool-mode", "prompt"), "requires native tools"),
    (("--oneshot",), "worker role requires --worktree"),
])
def test_bad_worker_flag_combinations(engine, harness, repo, manifest, monkeypatch,
                                      flags, reason):
    expect_refused(engine, harness, monkeypatch, repo, reason, "-m", "gpt-6.1-sol",
                   *flags, *LABEL, *manifest.receipt, *manifest())


def test_policy_mutation_and_missing_policy_refused(engine, harness, repo, manifest,
                                                    monkeypatch):
    args = manifest()
    pol = repo / ".claude" / "agent-policy.json"
    pol.write_text(json.dumps({"allowed_cmds": [["node", "*"]]}), encoding="utf-8")
    expect_refused(engine, harness, monkeypatch, repo, "policy digest does not match",
                   *WORKER, *manifest.receipt, *args)
    pol.unlink()
    expect_refused(engine, harness, monkeypatch, repo, "requires an explicit",
                   *WORKER, *manifest.receipt, *args)
    assert not pol.exists()                                  # never recreated


@pytest.mark.parametrize("flag", ["--write", "--worktree"])
def test_planner_cannot_create_writable_execution_state(engine, harness, repo, manifest,
                                                       monkeypatch, flag):
    expect_refused(engine, harness, monkeypatch, repo, "planner role forbids writable execution",
                   "-m", "gpt-6.1-sol", "--oneshot", flag, *LABEL, *manifest.receipt,
                   *manifest(role="planner", write_paths=[], commands=[]))


@pytest.fixture()
def sp():
    import importlib.util
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..",
                        "engine", "supervisor_policy.py")
    spec = importlib.util.spec_from_file_location("sp_under_test", os.path.abspath(path))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_helper_errors_do_not_echo_values(sp, repo, tmp_path_factory):
    out = tmp_path_factory.mktemp("h")
    raw = b'{"schema": "x", "schema": "secret-value"}'
    (out / "m.json").write_bytes(raw)
    with pytest.raises(sp.ManifestError) as e:
        sp.load_manifest(str(out / "m.json"), sha(raw))
    assert "secret-value" not in str(e.value) and "schema" not in str(e.value)
    for bad in ("../secret-path", "C:secret-path", "/secret-path"):
        with pytest.raises(sp.ManifestError) as e:
            sp.normalize_write_path(bad)
        assert "secret-path" not in str(e.value)


def test_helper_label_and_linked_parent(sp, repo, tmp_path_factory):
    base = git(repo, "rev-parse", "HEAD")
    for label in ("att-1\n", "C:att-1", "a/b", "..", "a\\b"):
        with pytest.raises(sp.ManifestError):
            sp.create_owned_worktree(str(repo), label, base)
    assert not (repo / ".claude" / "worktrees").exists()
    elsewhere = tmp_path_factory.mktemp("elsewhere")
    try:
        os.symlink(str(elsewhere), str(repo / ".claude" / "worktrees"), target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable on this platform/account")
    with pytest.raises(sp.ManifestError):
        sp.create_owned_worktree(str(repo), "att-1", base)
    assert list(elsewhere.iterdir()) == []                   # nothing created through link


def bounded_runner(engine, sp, repo):
    engine.load_policy(str(repo))      # inherited allowlist includes git status/log
    b = sp.Boundary(write_paths=("src/", "README"), commands=(("git", "status"),))
    return engine.Runner(str(repo), "disk", boundary=b)


def test_runner_allows_in_scope_writes(engine, sp, repo):
    run = bounded_runner(engine, sp, repo)
    run.write_file("src/new.txt", "fresh\n")
    run.replace_in_file("README", "synthetic", "edited")
    run.write_file("src/deep/er.txt", "x")
    assert (repo / "src" / "new.txt").read_text(encoding="utf-8") == "fresh\n"
    assert (repo / "README").read_text(encoding="utf-8") == "edited\n"
    assert (repo / "src" / "deep" / "er.txt").exists()


@pytest.mark.parametrize("path", ["tests/x.py", "README2", "srcx/a.txt", ".git/config",
                                  ".claude/agent-policy.json", "src/../tests/x.py", "src"])
def test_runner_refuses_out_of_scope_write(engine, sp, repo, path):
    run = bounded_runner(engine, sp, repo)
    before = snapshot(repo)
    with pytest.raises(ValueError):
        run.write_file(path, "nope")
    assert snapshot(repo) == before


def test_runner_refuses_out_of_scope_replace_and_symlink_escape(engine, sp, repo, tmp_path_factory):
    run = bounded_runner(engine, sp, repo)
    pol = repo / ".claude" / "agent-policy.json"
    original = pol.read_bytes()
    with pytest.raises(ValueError):
        run.replace_in_file(".claude/agent-policy.json", "git", "node")
    assert pol.read_bytes() == original
    try:
        os.symlink(str(repo / "tests_dir"), str(repo / "src" / "link"), target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable on this platform/account")
    (repo / "tests_dir").mkdir()
    with pytest.raises(ValueError):
        run.write_file("src/link/x.txt", "nope")
    assert not (repo / "tests_dir" / "x.txt").exists()


def test_runner_requires_exact_selected_argv(engine, sp, repo):
    run = bounded_runner(engine, sp, repo)
    assert run.run_command(["git", "status"]).startswith("exit=0")
    for argv in (["git", "status", "--porcelain"], ["git", "log"], ["git"]):
        assert run.run_command(argv).startswith("refused")
    assert engine.ALLOWED_CMDS == POLICY["allowed_cmds"]     # globals not narrowed per run


def test_default_runner_unchanged(engine, repo):
    run = engine.Runner(str(repo), "disk")
    run.write_file("tests/x.py", "ok")
    assert (repo / "tests" / "x.py").read_text(encoding="utf-8") == "ok"
    assert run.boundary is None


def tool_reply(name, **arguments):
    return {"choices": [{"finish_reason": "stop", "message": {"content": "", "tool_calls": [{
        "id": "c-" + name, "type": "function",
        "function": {"name": name, "arguments": json.dumps(arguments)}}]}}]}


def test_worker_runs_in_owned_worktree_with_enforced_boundary(harness, repo, manifest):
    harness.run(*WORKER, "--label", "w-att-1", *manifest.receipt, *manifest(), replies=[
        tool_reply("write_file", path="src/b.txt", content="in scope\n"),
        tool_reply("write_file", path="tests/x.py", content="out of scope\n"),
        tool_reply("run_command", argv=["git", "status", "--short"]),
        _finish_reply("native"), _finish_reply("native"), _finish_reply("native")])
    wt = repo / ".claude" / "worktrees" / "w-att-1"
    assert (wt / "src" / "b.txt").read_text(encoding="utf-8") == "in scope\n"
    assert not (wt / "tests").exists()
    assert not (repo / "src" / "b.txt").exists()               # main tree untouched
    assert git(repo, "rev-parse", "agent/w-att-1") == manifest.base["baseline_commit"]
    start = json.loads((repo / "index.jsonl").read_text(encoding="utf-8").splitlines()[0])
    assert start["run_id"] == "run"
    assert os.path.realpath(start["root"]) == os.path.realpath(repo)
    sent = json.dumps(harness.payloads)
    assert "refused" in sent and "out of scope" in sent


@pytest.mark.parametrize("label", ["agent", "att-1\n", "C:att-1", "../att-1", "a/att-1"])
def test_worker_label_must_carry_attempt_identity(engine, harness, repo, manifest,
                                                  monkeypatch, label):
    expect_refused(engine, harness, monkeypatch, repo, "--label must be an identifier",
                   *WORKER, "--label", label, *manifest.receipt, *manifest())


def held(engine, harness, monkeypatch, *argv):
    import io
    monkeypatch.setattr(engine, "load_cfg", lambda: pytest.fail("gateway config loaded"))
    err = io.StringIO()
    monkeypatch.setattr(sys, "stderr", err)
    with pytest.raises(SystemExit) as error:
        harness.run(*argv, replies=[])
    assert error.value.code == 2, err.getvalue()
    assert "supervisor refused:" in err.getvalue() and "held" in err.getvalue()
    return err.getvalue()


def test_existing_worktree_path_is_held_not_reused(engine, harness, repo, manifest, monkeypatch):
    wt = repo / ".claude" / "worktrees" / "w-att-1"
    wt.mkdir(parents=True)
    (wt / "keep.txt").write_bytes(b"precious\x00bytes")
    held(engine, harness, monkeypatch, *WORKER, "--label", "w-att-1", *manifest.receipt,
         *manifest())
    assert (wt / "keep.txt").read_bytes() == b"precious\x00bytes"
    assert sorted(p.name for p in wt.iterdir()) == ["keep.txt"]
    assert not (repo / "index.jsonl").exists()


def test_existing_branch_is_held_not_deleted(engine, harness, repo, manifest, monkeypatch):
    (repo / "src" / "a.txt").write_text("beta\n", encoding="utf-8")
    git(repo, "commit", "-qam", "side")
    side = git(repo, "rev-parse", "HEAD")
    git(repo, "branch", "agent/w-att-1", side)
    git(repo, "reset", "-q", "--hard", "HEAD~1")
    text = held(engine, harness, monkeypatch, *WORKER, "--label", "w-att-1",
                *manifest.receipt, *manifest())
    assert "fatal" not in text and "agent/w-att-1" not in text   # no Git stderr echo
    assert git(repo, "rev-parse", "agent/w-att-1") == side
    assert not (repo / ".claude" / "worktrees" / "w-att-1").exists()  # reservation cleaned
    assert not (repo / ".claude" / "worktrees" / "w-att-1" / "src").exists()
    assert not (repo / "index.jsonl").exists()


def test_planner_oneshot_has_empty_capabilities(harness, repo, manifest):
    harness.run("-m", "gpt-6.1-sol", "--oneshot", *manifest.receipt,
                *manifest(role="planner", write_paths=[], commands=[]),
                replies=[{"choices": [{"message": {"content": "plan"}, "finish_reason": "stop"}]}])
    assert not (repo / ".claude" / "worktrees").exists()


@pytest.mark.parametrize("failure", ["transport", "stream_idle", "stream_http"])
def test_one_supervised_planner_admission_cannot_hide_another_request(
        engine, harness, repo, manifest, failure):
    from test_engine_oneshot_stream import _gpt_events
    flags = manifest(role="planner", write_paths=[], commands=[])
    args = ("-m", "gpt-6.1-sol", "-e", "max", "--oneshot", *LABEL, *manifest.receipt, *flags)
    response = {"choices": [{"message": {"content": "synthetic second answer"}}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1}}
    if failure == "transport":
        kw = {"replies": [{"_transient": "read timeout after 600s"}, response]}
    elif failure == "stream_idle":
        idle = engine.StreamError("idle", "synthetic timeout", 1)
        kw = {"replies": [], "stream_replies": [[idle], _gpt_events(["synthetic second answer"])]}
    else:
        http = engine.StreamError("http", "HTTP 400: unsupported", 0)
        kw = {"replies": [response], "stream_replies": [http]}
    with pytest.raises(SystemExit) as exc:
        harness.run(*args, **kw)
    assert exc.value.code == 1
    assert len(harness.payloads) + len(harness.stream_calls) == 1


def test_planner_manifest_cannot_carry_capabilities(engine, harness, repo, manifest, monkeypatch):
    expect_refused(engine, harness, monkeypatch, repo, "planner role carries no",
                   "-m", "gpt-6.1-sol", "--oneshot", *manifest.receipt,
                   *manifest(role="planner"))

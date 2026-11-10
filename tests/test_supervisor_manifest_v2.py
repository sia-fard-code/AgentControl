import hashlib
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "engine"))
import supervisor_manifest_v2 as mv2  # noqa: E402
import supervisor_policy as v1  # noqa: E402

SHA = "a" * 64
BOUNDS = {"max_entries": 10, "max_file_bytes": 10, "max_total_bytes": 100,
          "max_manifest_bytes": 1000, "max_path_bytes": 260, "max_seconds": 5}
GIT = {"kind": "git", "baseline_commit": "b" * 40, "initial_sha256": SHA,
       "inputs": ["."], "bounds": BOUNDS}
ART = {"kind": "artifact-manifest", "initial_sha256": SHA, "inputs": ["."], "bounds": BOUNDS}


def world(tmp_path):
    main = tmp_path / "repo"
    main.mkdir(parents=True)
    (main / "policy.json").write_text("{}")
    run = tmp_path / "runs" / "att1"
    (run / "candidate").mkdir(parents=True)
    (run / "meta").mkdir()
    (run / "meta" / "contract.json").write_bytes(b'{"c":1}')
    return main, run


def manifest(main, run, role="worker", **over):
    m = {"schema": mv2.SCHEMA, "root": str(main),
         "policy_sha256": hashlib.sha256(b"{}").hexdigest(),
         "orchestration_id": "orch1", "attempt_id": "att1", "launch_id": "L1",
         "run_id": "R1", "role": role, "write_paths": ["src/"],
         "commands": [["python", "-m", "pytest"]], "mcp": "none", "max_iters": 50,
         "envelope_sha256": SHA, "candidate": GIT, "seed_candidate": SHA,
         "seed_ref": "meta/seed.json", "seed_receipt": SHA,
         "candidate_root": str(run / "candidate"), "run_dir": str(run),
         "profile": "resilient-v1", "recovery_contract_ref": "meta/contract.json",
         "recovery_contract_sha256": hashlib.sha256(b'{"c":1}').hexdigest()}
    if role == "planner":
        m.update(write_paths=[], commands=[], max_iters=1, candidate_root=None,
                 seed_candidate=None, seed_ref=None, seed_receipt=None,
                 recovery_contract_ref=None, recovery_contract_sha256=None)
    m.update(over)
    return m


def put(run, m, raw=None):
    p = run / "manifest.json"
    data = raw if raw is not None else json.dumps(m).encode()
    p.write_bytes(data)
    return str(p), hashlib.sha256(data).hexdigest()


def args(**over):
    a = dict(oneshot=False, write=True, worktree=False, patch=None, max_iters=10,
             label="w-att1", allow_prompt_tools=False, verify_command=None,
             recovery_contract=None, transcript=None)
    a.update(over)
    return SimpleNamespace(**a)


@pytest.mark.parametrize("cand", [GIT, ART])
def test_load_pins_and_is_immutable(tmp_path, cand):
    main, run = world(tmp_path)
    m = mv2.load_manifest2(*put(run, manifest(main, run, candidate=cand)))
    assert m.schema == mv2.SCHEMA and m.candidate.kind == cand["kind"]
    assert m.write_paths == ("src/",) and m.commands == (("python", "-m", "pytest"),)
    with pytest.raises(Exception):
        m.role = "planner"
    if cand is ART:
        assert not hasattr(m, "baseline_commit")


def test_pin_mismatch_relative_and_size(tmp_path):
    main, run = world(tmp_path)
    p, d = put(run, manifest(main, run))
    with pytest.raises(mv2.ManifestError, match="mismatch"):
        mv2.load_manifest2(p, "0" * 64)
    with pytest.raises(mv2.ManifestError):
        mv2.load_manifest2("manifest.json", d)
    with pytest.raises(mv2.ManifestError, match="large"):
        mv2.load_manifest2(*put(run, None, raw=b" " * (64 * 1024 + 1)))


@pytest.mark.parametrize("raw", [
    b'{"schema": 1, "schema": 2}', b'{"x": NaN}', b'{"x": 1e400}', b'[1]', b'\xff'])
def test_strict_json_rejects_without_echo(tmp_path, raw):
    main, run = world(tmp_path)
    with pytest.raises(mv2.ManifestError) as e:
        mv2.load_manifest2(*put(run, None, raw=raw))
    assert "NaN" not in str(e.value) and "1e400" not in str(e.value)


BAD = [dict(extra=1), dict(baseline_commit="b" * 40), dict(profile="legacy"),
       dict(mcp="all"), dict(max_iters=0), dict(max_iters=201), dict(max_iters=True),
       dict(max_iters=5.0), dict(seed_candidate=None), dict(seed_receipt="X" * 64),
       dict(seed_ref="../x"), dict(seed_ref="/abs"), dict(envelope_sha256=None),
       dict(attempt_id="bad id"), dict(run_dir="rel"), dict(candidate_root=None),
       dict(candidate=dict(GIT, extra=1)), dict(candidate=dict(ART, baseline_commit="b" * 40)),
       dict(candidate=dict(GIT, baseline_commit="0" * 40)),
       dict(recovery_contract_sha256=None), dict(write_paths=[".git/x"]),
       dict(commands=[["ls", "*"]]), dict(role="admin")]


@pytest.mark.parametrize("over", BAD)
def test_shape_rejects(tmp_path, over):
    main, run = world(tmp_path)
    m = manifest(main, run)
    m.update(over)
    with pytest.raises(mv2.ManifestError):
        mv2.load_manifest2(*put(run, m))


def test_missing_field_rejects(tmp_path):
    main, run = world(tmp_path)
    m = manifest(main, run)
    del m["seed_ref"]
    with pytest.raises(mv2.ManifestError, match="fields"):
        mv2.load_manifest2(*put(run, m))


def test_candidate_root_must_be_fixed_layout(tmp_path):
    main, run = world(tmp_path)
    (tmp_path / "other").mkdir()
    m = manifest(main, run, candidate_root=str(tmp_path / "other"))
    with pytest.raises(mv2.ManifestError, match="candidate"):
        mv2.load_manifest2(*put(run, m))


@pytest.mark.parametrize("over", [dict(write_paths=["src/"]), dict(commands=[["ls"]]),
                                  dict(max_iters=2), dict(seed_candidate=SHA),
                                  dict(candidate_root="X"), dict(recovery_contract_ref="a")])
def test_planner_shape_readonly(tmp_path, over):
    main, run = world(tmp_path)
    m = manifest(main, run, role="planner")
    if "candidate_root" in over:
        over = dict(candidate_root=str(run / "candidate"))
    m.update(over)
    with pytest.raises(mv2.ManifestError):
        mv2.load_manifest2(*put(run, m))


def test_v1_v2_cross_reject_and_v1_caps_unchanged(tmp_path):
    main, run = world(tmp_path)
    p, d = put(run, manifest(main, run))
    with pytest.raises(v1.ManifestError):
        v1.load_manifest(p, d)
    old = {f: None for f in v1.FIELDS}
    old["schema"] = v1.SCHEMA
    with pytest.raises(mv2.ManifestError):
        mv2.load_manifest2(*put(run, old))
    caps = v1.capabilities()
    assert caps["version"] == 1 and caps["manifest_schema"] == v1.SCHEMA
    assert "baseline_commit" in caps["manifest_fields"]
    c2 = mv2.capabilities2()
    assert c2["version"] == 2 and c2["manifest_schema"] == mv2.SCHEMA
    assert c2["features"]["mcp"] == "none" and c2["features"]["sandbox"] is False
    assert c2["features"]["process_containment"] is False
    assert "baseline_commit" not in c2["manifest_fields"]
    with pytest.raises(TypeError):
        c2["version"] = 3


def launch(tmp_path, role="worker", m_over=None, a_over=None, verify=None, **kw):
    main, run = world(tmp_path)
    m = mv2.load_manifest2(*put(run, manifest(main, run, role=role, **(m_over or {}))))
    a = dict(recovery_contract=str(run / "meta" / "contract.json"),
             transcript=str(run / "t.jsonl"))
    if role == "planner":
        a.update(oneshot=True, write=False, max_iters=None, recovery_contract=None)
    a.update(a_over or {})
    opts = dict(manifest_path=str(run / "manifest.json"), main_root=str(main),
                policy_file="policy.json", launch_id="L1", run_id="R1", mode="native",
                allowed_cmds=[["python", "-m", "pytest"]],
                permitted=lambda rule, argv: list(rule) == list(argv),
                verify_seed=verify if verify is not None else (lambda mm, root: True))
    opts.update(kw)
    return mv2.validate_launch2(m, args(**a), **opts)


@pytest.fixture(autouse=True)
def no_subprocess(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("no process/Git query allowed")
    monkeypatch.setattr(mv2.subprocess, "run", boom) if hasattr(mv2, "subprocess") else None
    monkeypatch.setattr(v1.subprocess, "run", boom)


def test_worker_admitted_with_narrow_boundary(tmp_path):
    seen = []
    b = launch(tmp_path, verify=lambda m, root: seen.append(root) or True)
    assert isinstance(b, v1.Boundary) and b.check_command(["python", "-m", "pytest"])
    assert not b.check_command(["python"])
    root = b.candidate_root
    assert seen == [root]
    b.check_write(root, os.path.join(root, "src", "a.py"))
    for bad in (os.path.join(root, "x.py"), os.path.join(root, ".git", "c")):
        with pytest.raises(ValueError):
            b.check_write(root, bad)
    assert b.manifest.attempt_id == "att1"


@pytest.mark.parametrize("verify", [lambda m, r: 1, lambda m, r: "true", lambda m, r: False])
def test_seed_verifier_must_return_exact_true(tmp_path, verify):
    with pytest.raises(mv2.ManifestError, match="seed"):
        launch(tmp_path, verify=verify)


def test_seed_verifier_missing(tmp_path):
    with pytest.raises(mv2.ManifestError, match="seed"):
        launch(tmp_path, verify_seed=None)


REFUSE = [
    dict(a_over=dict(worktree=True)), dict(a_over=dict(write=False)),
    dict(a_over=dict(oneshot=True)), dict(a_over=dict(max_iters=None)),
    dict(a_over=dict(max_iters=51)), dict(a_over=dict(label="other")),
    dict(a_over=dict(allow_prompt_tools=True)), dict(a_over=dict(verify_command="x")),
    dict(a_over=dict(recovery_contract=None)), dict(a_over=dict(transcript="t.jsonl")),
    dict(launch_id="L2"), dict(run_id="R2"), dict(mode="prompt"),
    dict(allowed_cmds=[["ls"]]), dict(policy_file="missing.json"),
    dict(m_over=dict(policy_sha256="0" * 64)), dict(m_over=dict(commands=[["cat", "../x"]])),
    dict(m_over=dict(commands=[["cat", "/etc/x"]])),
]


@pytest.mark.parametrize("kw", REFUSE)
def test_worker_refusals(tmp_path, kw):
    with pytest.raises(mv2.ManifestError):
        launch(tmp_path, **kw)


def test_foreign_root_and_paths(tmp_path):
    with pytest.raises(mv2.ManifestError, match="root"):
        launch(tmp_path, main_root=str(tmp_path))
    with pytest.raises(mv2.ManifestError, match="transcript"):
        launch(tmp_path / "b", a_over=dict(transcript=str(tmp_path / "t.jsonl")))
    with pytest.raises(mv2.ManifestError, match="manifest"):
        launch(tmp_path / "c", manifest_path=str(tmp_path / "x" / "manifest.json"))


def test_missing_candidate_dir(tmp_path):
    main, run = world(tmp_path)
    m = mv2.load_manifest2(*put(run, manifest(main, run)))
    (run / "candidate").rmdir()
    with pytest.raises(mv2.ManifestError, match="candidate"):
        mv2.validate_launch2(
            m, args(recovery_contract=str(run / "meta" / "contract.json"),
                    transcript=str(run / "t.jsonl")),
            manifest_path=str(run / "manifest.json"), main_root=str(main),
            policy_file="policy.json", launch_id="L1", run_id="R1", mode="native",
            allowed_cmds=[], permitted=lambda r, a: True, verify_seed=lambda m, r: True)


def test_contract_hash_mismatch_no_echo(tmp_path):
    main, run = world(tmp_path)
    m = mv2.load_manifest2(*put(run, manifest(main, run)))
    (run / "meta" / "contract.json").write_bytes(b"SECRET")
    with pytest.raises(mv2.ManifestError, match="contract") as e:
        mv2.validate_launch2(
            m, args(recovery_contract=str(run / "meta" / "contract.json"),
                    transcript=str(run / "t.jsonl")),
            manifest_path=str(run / "manifest.json"), main_root=str(main),
            policy_file="policy.json", launch_id="L1", run_id="R1", mode="native",
            allowed_cmds=[["python", "-m", "pytest"]], permitted=lambda r, a: r == a,
            verify_seed=lambda m, r: True)
    assert "SECRET" not in str(e.value)


def test_planner_readonly_no_verifier_no_git(tmp_path):
    b = launch(tmp_path, role="planner", verify_seed=None)
    assert b.write_paths == () and b.commands == () and b.candidate_root is None
    for i, bad in enumerate((dict(write=True), dict(oneshot=False), dict(max_iters=2),
                             dict(recovery_contract="x"), dict(worktree=True))):
        with pytest.raises(mv2.ManifestError):
            launch(tmp_path / f"p{i}", role="planner",
                   a_over=bad, verify_seed=None)

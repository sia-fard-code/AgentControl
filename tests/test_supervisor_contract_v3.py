"""Schema-3 approval codec tests: synthetic tmp_path data only, no git or user state."""
import hashlib
import importlib.util
from pathlib import Path

import pytest

from supervisor_helpers import canonical, digest, envelope_bytes, load
from supervisor_v2_helpers import envelope_bytes_v2, envelope_dict_v2

ENGINE = Path(__file__).resolve().parents[1] / "engine"
contract = load("supervisor_contract")
BOUNDS = {"max_entries": 1000, "max_file_bytes": 1 << 20, "max_total_bytes": 1 << 24,
          "max_manifest_bytes": 1 << 20, "max_path_bytes": 1024, "max_seconds": 60}
CONT = {"profile": "resilient-v1", "admission": "approved-task"}


@pytest.fixture
def v3(monkeypatch):
    monkeypatch.syspath_prepend(str(ENGINE))
    spec = importlib.util.spec_from_file_location("supervisor_contract_v3",
                                                  ENGINE / "supervisor_contract_v3.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def git_cand(**kw):
    return {"kind": "git", "baseline_commit": "b" * 40, "initial_sha256": digest("snap"),
            "inputs": ["docs", "src"], "bounds": dict(BOUNDS), **kw}


def art_cand(**kw):
    return {"kind": "artifact-manifest", "initial_sha256": digest("snap"),
            "inputs": ["docs", "src"], "bounds": dict(BOUNDS), **kw}


def data3(root, candidate=None, limits=None, **overrides):
    d = envelope_dict_v2(root, limits)
    del d["baseline_commit"]
    d.update(schema_version=3, candidate=candidate or git_cand(), continuation=dict(CONT))
    d.update(overrides)
    return d
# --- tests below ---


@pytest.mark.parametrize("cand", [git_cand, art_cand])
def test_roundtrip_canonical_and_digest(v3, tmp_path, cand):
    d = data3(tmp_path, cand())
    env = v3.load_envelope3(d)
    assert env.schema_version == 3
    assert v3.dump_envelope3(env) == d
    assert env.digest == hashlib.sha256(canonical(d)).hexdigest()
    assert v3.load_envelope3(v3.dump_envelope3(env)) == env
    assert env.candidate.kind == d["candidate"]["kind"]
    assert env.candidate.inputs == ("docs", "src")
    assert env.continuation.profile == "resilient-v1"
    assert env.continuation.admission == "approved-task"
    assert env.models == ("synthetic-model",) and env.criteria == ("C1", "C2", "CI")
    assert env.capabilities["write"] == ("src/",)


def test_null_quotas_stay_none_and_explicit_preserved(v3, tmp_path):
    env = v3.load_envelope3(data3(tmp_path))
    assert env.limits["max_replans"] is None and env.limits["max_total_attempts"] is None
    assert env.limits["max_planner_calls"] is None
    assert env.deadline is None and env.max_usage is None
    assert env.limits["max_concurrent_workers"] == 8
    assert not contract.limit_reached(env, "max_replans", 0)
    assert not contract.deadline_passed(env, 9e18)
    env = v3.load_envelope3(data3(tmp_path, limits={"max_replans": 0, "max_planner_calls": 3},
                                  deadline=2_000_000_000.0, max_usage=5000))
    assert env.limits["max_replans"] == 0 and env.limits["max_planner_calls"] == 3
    assert contract.limit_reached(env, "max_replans", 0)
    assert env.deadline == 2_000_000_000.0 and env.max_usage == 5000


def test_baseline_only_for_git(v3, tmp_path):
    assert v3.load_envelope3(data3(tmp_path)).baseline_commit == "b" * 40
    env = v3.load_envelope3(data3(tmp_path, art_cand()))
    with pytest.raises(contract.ContractError):
        env.baseline_commit
    with pytest.raises(contract.ContractError):
        env.candidate.baseline_commit
    assert not hasattr(env.candidate, "__dict__") or "baseline_commit" not in vars(env.candidate)


def _rejects(v3, d):
    with pytest.raises(contract.ContractError) as info:
        v3.load_envelope3(d)
    assert "secret-marker" not in str(info.value)


@pytest.mark.parametrize("mutate", [
    lambda d: d["candidate"].update(baseline_commit="0" * 40),
    lambda d: d["candidate"].update(baseline_commit=None),
    lambda d: d.update(baseline_commit="a" * 40),
    lambda d: d.update(future="secret-marker"),
    lambda d: d["candidate"].update(extra=1),
    lambda d: d["continuation"].update(context_tokens=32768),
    lambda d: d["candidate"]["bounds"].update(handoff_bytes=32768),
    lambda d: d["candidate"].update(kind="git"),
])
def test_artifact_rejects_baseline_and_unknown(v3, tmp_path, mutate):
    d = data3(tmp_path, art_cand())
    mutate(d)  # last case: git kind without baseline_commit
    _rejects(v3, d)


@pytest.mark.parametrize("mutate", [
    lambda d: d["candidate"].update(baseline_commit="0" * 40),
    lambda d: d["candidate"].update(baseline_commit="B" * 40),
    lambda d: d["candidate"].update(baseline_commit="b" * 40 + "\n"),
    lambda d: d["candidate"].pop("baseline_commit"),
    lambda d: d["candidate"].update(kind="svn"),
    lambda d: d["candidate"].update(initial_sha256="0" * 63),
    lambda d: d.update(schema_version=True),
    lambda d: d.update(schema_version=4),
    lambda d: d.update(schema_version=2),
    lambda d: d.update(continuation={"profile": "legacy", "admission": "approved-task"}),
    lambda d: d.update(continuation={"profile": "resilient-v1"}),
    lambda d: d.update(candidate=None),
    lambda d: d.update(criteria=[]),
    lambda d: d.update(criteria=["secret-marker\n"]),
    lambda d: d.update(models=[]),
    lambda d: d.update(models=["bad model"]),
    lambda d: d.update(capabilities={"write": ["/abs"], "commands": [], "mcp": []}),
    lambda d: d.update(cancel_authority=1),
    lambda d: d.update(deadline=float("nan")),
    lambda d: d.update(deadline=float("inf")),
    lambda d: d.update(max_usage=0),
    lambda d: d["limits"].update(max_concurrent_workers=None),
    lambda d: d["limits"].update(max_replans=True),
    lambda d: d.update(orchestration_id="../x"),
    lambda d: d.update(worker_turn_limit=True),
])
def test_rejects_malformed(v3, tmp_path, mutate):
    d = data3(tmp_path)
    mutate(d)
    _rejects(v3, d)


@pytest.mark.parametrize("key,value", [
    (k, v) for k in BOUNDS for v in (0, -1, True, None, 1.5, float("nan"), float("inf"),
                                     1e400, "1", 10**30)
] + [("max_file_bytes", 1 << 25)])
def test_rejects_bad_bounds(v3, tmp_path, key, value):
    if key == "max_seconds" and value == 1.5:
        return  # finite positive fractional seconds are valid
    d = data3(tmp_path)
    d["candidate"]["bounds"][key] = value
    _rejects(v3, d)


@pytest.mark.parametrize("inputs", [
    [], ["src", "docs"], ["src", "src"], [".", "src"], ["/abs"], ["C:/x"], ["a\\b"],
    ["../x"], ["a/../b"], ["a/./b"], ["a//b"], ["a/"], ["file:ads"], [""], [1], "src",
    ["CON"], ["dir/nul.txt"], ["x" * 600],
])
def test_rejects_bad_inputs(v3, tmp_path, inputs):
    _rejects(v3, data3(tmp_path, git_cand(inputs=inputs)))


def test_whole_scope_dot_alone_ok(v3, tmp_path):
    assert v3.load_envelope3(data3(tmp_path, art_cand(inputs=["."]))).candidate.inputs == (".",)


@pytest.mark.parametrize("change", [
    lambda d: d.update(candidate=art_cand()),
    lambda d: d["candidate"].update(inputs=["src"]),
    lambda d: d["candidate"].update(initial_sha256=digest("other")),
    lambda d: d["candidate"]["bounds"].update(max_seconds=61),
])
def test_descriptor_changes_change_digest(v3, tmp_path, change):
    d = data3(tmp_path)
    before = v3.load_envelope3(d).digest
    change(d)
    assert v3.load_envelope3(d).digest != before


def test_nested_mutation_cannot_alter_approval(v3, tmp_path):
    d = data3(tmp_path)
    env = v3.load_envelope3(d)
    snap = (env.digest, env.candidate.inputs, dict(env.candidate.bounds), env.models)
    d["candidate"]["inputs"].append("zzz")
    d["candidate"]["bounds"]["max_seconds"] = 1
    d["models"].append("other")
    d["capabilities"]["write"].append("etc/")
    assert snap == (env.digest, env.candidate.inputs, dict(env.candidate.bounds), env.models)
    assert env.capabilities["write"] == ("src/",)
    assert v3.dump_envelope3(env) == data3(tmp_path)
    with pytest.raises(TypeError):
        env.candidate.bounds["max_seconds"] = 1
    with pytest.raises(TypeError):
        env.limits["max_replans"] = 1
    with pytest.raises(AttributeError):
        env.candidate.inputs.append("x")
    with pytest.raises(Exception):
        env.digest = "x"


def test_legacy_v1_v2_unchanged_and_reject_v3(v3, tmp_path):
    raw1, ap1 = envelope_bytes(tmp_path)
    raw2, ap2 = envelope_bytes_v2(tmp_path)
    e1 = contract.load_envelope(raw1, ap1, tmp_path)
    e2 = contract.load_envelope(raw2, ap2, tmp_path)
    assert (e1.schema_version, e1.digest, e1.baseline_commit) == (1, ap1, "a" * 40)
    assert (e2.schema_version, e2.digest, e2.limits["max_replans"]) == (2, ap2, None)
    assert "baseline_commit" in contract.ENVELOPE_KEYS
    raw3 = canonical(data3(tmp_path))
    with pytest.raises(contract.ContractError):
        contract.load_envelope(raw3, hashlib.sha256(raw3).hexdigest(), tmp_path)
    for d in (envelope_dict_v2(tmp_path), {**envelope_dict_v2(tmp_path), "schema_version": 1}):
        _rejects(v3, d)

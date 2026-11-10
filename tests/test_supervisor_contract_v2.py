import json
import math

import pytest

from supervisor_helpers import canonical, digest, envelope_bytes, load
from supervisor_v2_helpers import envelope_bytes_v2, envelope_dict_v2

contract = load("supervisor_contract")


def _load2(root, limits=None, **overrides):
    raw, approved = envelope_bytes_v2(root, limits, **overrides)
    return contract.load_envelope(raw, approved, root)


def test_v2_defaults_are_null_quotas_and_capacity_eight(tmp_path):
    env = _load2(tmp_path)
    assert env.schema_version == 2
    assert dict(env.limits) == dict(contract.DEFAULT_LIMITS_V2)
    assert env.limits["max_total_attempts"] is None
    assert env.limits["max_replans"] is None
    assert env.limits["max_planner_calls"] is None
    assert env.limits["max_concurrent_workers"] == 8
    assert env.deadline is None and env.max_usage is None
    assert not contract.limit_reached(env, "max_replans", 20)
    assert contract.usage_available(env, 1000, 500, 100)
    assert not contract.deadline_passed(env, 9e18)


def test_constants_and_legacy_limits_unchanged():
    assert dict(contract.LIMITS) == {"max_replans": 2, "max_total_attempts": 6,
                                     "max_concurrent_workers": 2, "max_planner_calls": 3,
                                     "max_final_report_calls": 1, "stop_grace_seconds": 60}
    assert contract.MAX_ENVELOPE_BYTES == 64 * 1024
    assert contract.MAX_PLAN_TASKS_V2 == 64
    assert contract.MAX_PROPOSAL_BYTES_V2 == 128 * 1024
    assert contract.MAX_RECORD_BYTES_V2 == 256 * 1024
    with pytest.raises(TypeError):
        contract.DEFAULT_LIMITS_V2["max_replans"] = 2


def test_v1_envelope_defaults_to_schema_one(tmp_path):
    raw, approved = envelope_bytes(tmp_path)
    env = contract.load_envelope(raw, approved, tmp_path)
    assert env.schema_version == 1
    assert env.limits is contract.LIMITS
    assert contract.proposal_task_limit(env) == 6
    assert contract.limit_reached(env, "max_replans", 2)
    assert not contract.limit_reached(env, "max_replans", 1)


@pytest.mark.parametrize("limits", [
    {"max_replans": 0}, {"max_total_attempts": 1}, {"max_planner_calls": 5},
    {"max_concurrent_workers": 16}, {"max_concurrent_workers": 1},
    {"max_replans": 30, "max_total_attempts": 100, "max_planner_calls": 31},
])
def test_v2_explicit_caps_honored_and_frozen(tmp_path, limits):
    env = _load2(tmp_path, limits)
    for key, value in limits.items():
        assert env.limits[key] == value
    with pytest.raises(TypeError):
        env.limits["max_replans"] = None


def test_zero_replans_reached_immediately(tmp_path):
    env = _load2(tmp_path, {"max_replans": 0})
    assert contract.limit_reached(env, "max_replans", 0)


@pytest.mark.parametrize("limits", [
    {"max_replans": -1}, {"max_total_attempts": 0}, {"max_planner_calls": 0},
    {"max_concurrent_workers": 0}, {"max_concurrent_workers": None},
    {"max_replans": True}, {"max_total_attempts": 2.0}, {"max_concurrent_workers": 8.5},
    {"max_final_report_calls": 2}, {"stop_grace_seconds": 30}, {"stop_grace_seconds": None},
    {"max_replans": "1"}, {"extra_cap": 1},
])
def test_v2_malformed_caps_rejected(tmp_path, limits):
    with pytest.raises(contract.ContractError):
        _load2(tmp_path, limits)


def test_v2_nan_cap_rejected_by_strict_json(tmp_path):
    data = envelope_dict_v2(tmp_path)
    raw = json.dumps(data, sort_keys=True).replace('"max_replans": null',
                                                   '"max_replans": NaN').encode()
    with pytest.raises(contract.ContractError):
        contract.load_envelope(raw, contract.hashlib.sha256(raw).hexdigest(), tmp_path)


def test_v2_missing_limit_key_rejected(tmp_path):
    data = envelope_dict_v2(tmp_path)
    del data["limits"]["max_replans"]
    raw = canonical(data)
    with pytest.raises(contract.ContractError):
        contract.load_envelope(raw, contract.hashlib.sha256(raw).hexdigest(), tmp_path)


def test_v2_explicit_deadline_and_usage(tmp_path):
    env = _load2(tmp_path, deadline=1000.0, max_usage=500)
    assert env.deadline == 1000.0 and env.max_usage == 500
    assert not contract.deadline_passed(env, 999.0)
    assert contract.deadline_passed(env, 1000.0)
    assert contract.usage_available(env, 300, 100, 100)
    assert not contract.usage_available(env, 300, 100, 101)


@pytest.mark.parametrize("field,value", [
    ("deadline", 0), ("deadline", -1.0), ("deadline", True), ("deadline", "1"),
    ("max_usage", 0), ("max_usage", 1.5), ("max_usage", True),
    ("max_usage", 10**12 + 1),
])
def test_v2_supplied_deadline_usage_validated(tmp_path, field, value):
    with pytest.raises(contract.ContractError):
        _load2(tmp_path, **{field: value})


@pytest.mark.parametrize("reserve", [0, -1, True, 1.0, None])
def test_usage_available_requires_positive_reserve(tmp_path, reserve):
    with pytest.raises(contract.ContractError):
        contract.usage_available(_load2(tmp_path), 0, 0, reserve)


def test_limit_reached_rejects_unknown_key(tmp_path):
    with pytest.raises(contract.ContractError):
        contract.limit_reached(_load2(tmp_path), "not_a_limit", 0)


@pytest.mark.parametrize("overrides", [
    {"deadline": None}, {"max_usage": None},
    {"limits": {"max_replans": None, "max_total_attempts": 6, "max_concurrent_workers": 2,
                "max_planner_calls": 3, "max_final_report_calls": 1, "stop_grace_seconds": 60}},
    {"limits": {"max_replans": 2, "max_total_attempts": 6, "max_concurrent_workers": 8,
                "max_planner_calls": 3, "max_final_report_calls": 1, "stop_grace_seconds": 60}},
])
def test_v1_still_rejects_null_and_widening(tmp_path, overrides):
    raw, approved = envelope_bytes(tmp_path, **overrides)
    with pytest.raises(contract.ContractError):
        contract.load_envelope(raw, approved, tmp_path)


@pytest.mark.parametrize("version", [0, 3, True, "2", None])
def test_unknown_schema_versions_rejected(tmp_path, version):
    with pytest.raises(contract.ContractError):
        _load2(tmp_path, schema_version=version)


def test_v2_altered_bytes_fail_original_sha(tmp_path):
    raw, approved = envelope_bytes_v2(tmp_path)
    altered, _ = envelope_bytes_v2(tmp_path, {"max_replans": 5})
    with pytest.raises(contract.ContractError):
        contract.load_envelope(altered, approved, tmp_path)
    with pytest.raises(contract.ContractError):
        contract.load_envelope(raw, approved, tmp_path / "other")


def test_null_and_measured_zero_are_distinct_digests(tmp_path):
    a, sha_a = envelope_bytes_v2(tmp_path, {"max_replans": None})
    b, sha_b = envelope_bytes_v2(tmp_path, {"max_replans": 0})
    assert sha_a != sha_b
    assert _load2(tmp_path, {"max_replans": 0}).limits["max_replans"] == 0


def test_strict_json_max_bytes_keyword():
    raw = b'{"a":"' + b"x" * 70_000 + b'"}'
    with pytest.raises(contract.ContractError):
        contract.strict_json(raw)
    assert contract.strict_json(raw, max_bytes=128 * 1024) == {"a": "x" * 70_000}
    with pytest.raises(contract.ContractError):
        contract.strict_json(b'{"a":1}', max_bytes=3)


@pytest.mark.parametrize("bad", [0, -1, True, 1.5, None, "64"])
def test_strict_json_max_bytes_must_be_positive_int(bad):
    with pytest.raises(contract.ContractError) as info:
        contract.strict_json(b"{}", max_bytes=bad)
    assert str(info.value) == "max_bytes must be a positive integer"  # fixed, never echoes input


def test_v2_ids_and_digests_use_fullmatch(tmp_path):
    with pytest.raises(contract.ContractError):
        _load2(tmp_path, goal_digest=digest("goal") + "\n")
    with pytest.raises(contract.ContractError):
        _load2(tmp_path, orchestration_id="orch-1\n")


def _task(task_id, deps=()):
    return {"task_id": task_id, "depends_on": list(deps), "objective_digest": digest(task_id),
            "criteria": ["C1"], "hypothesis_ids": []}


def _proposal(tasks, revision=0):
    return canonical({"schema_version": 1, "plan_revision": revision, "tasks": tasks})


def _chain(n):
    return [_task(f"t{i}", [f"t{i - 1}"] if i else []) for i in range(n)]


@pytest.mark.parametrize("n", [8, 16, 64])
def test_v2_accepts_large_dags(tmp_path, n):
    plan = contract.validate_proposal(_proposal(_chain(n)), _load2(tmp_path), 0)
    assert len(plan.tasks) == n
    assert contract.proposal_task_limit(_load2(tmp_path)) == 64


def test_v2_rejects_65_tasks(tmp_path):
    with pytest.raises(contract.ContractError):
        contract.validate_proposal(_proposal(_chain(65)), _load2(tmp_path), 0)


def test_v2_proposal_byte_bound(tmp_path):
    env = _load2(tmp_path)
    small = _proposal([_task("t0")])
    raw = small + b" " * (128 * 1024 - len(small))  # trailing JSON whitespace pads to the bound
    assert len(contract.validate_proposal(raw, env, 0).tasks) == 1
    with pytest.raises(contract.ContractError):
        contract.validate_proposal(raw + b" ", env, 0)


def test_v2_smaller_attempt_ceiling_caps_tasks(tmp_path):
    env = _load2(tmp_path, {"max_total_attempts": 3})
    assert contract.proposal_task_limit(env) == 3
    with pytest.raises(contract.ContractError):
        contract.validate_proposal(_proposal(_chain(4)), env, 0)


@pytest.mark.parametrize("revision", [3, 4, 20])
def test_v2_revisions_beyond_two(tmp_path, revision):
    env = _load2(tmp_path)
    assert contract.validate_proposal(_proposal(_chain(1), revision), env, revision).revision == revision
    with pytest.raises(contract.ContractError):
        contract.validate_proposal(_proposal(_chain(1), revision + 1), env, revision)
    with pytest.raises(contract.ContractError):
        contract.validate_proposal(_proposal(_chain(1), revision - 1), env, revision)


def test_v2_explicit_replan_cap_bounds_revision(tmp_path):
    env = _load2(tmp_path, {"max_replans": 0})
    with pytest.raises(contract.ContractError):
        contract.validate_proposal(_proposal(_chain(1), 1), env, 1)


@pytest.mark.parametrize("tasks", [
    [_task("a", ["b"]), _task("b", ["a"])], [_task("a", ["zz"])], [_task("a"), _task("a")],
])
def test_v2_bad_graphs_rejected(tmp_path, tasks):
    with pytest.raises(contract.ContractError):
        contract.validate_proposal(_proposal(tasks), _load2(tmp_path), 0)


def test_v1_still_rejects_seven_tasks_and_revision_three(tmp_path):
    raw, approved = envelope_bytes(tmp_path)
    env = contract.load_envelope(raw, approved, tmp_path)
    with pytest.raises(contract.ContractError):
        contract.validate_proposal(_proposal(_chain(7)), env, 0)
    with pytest.raises(contract.ContractError):
        contract.validate_proposal(_proposal(_chain(1), 3), env, 3)
    with pytest.raises(contract.ContractError):
        contract.validate_proposal(_proposal(_chain(1)) + b" " * 65536, env, 0)

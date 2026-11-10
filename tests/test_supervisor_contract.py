import json

import pytest

from supervisor_helpers import canonical, envelope_bytes, envelope_dict, load

contract = load("supervisor_contract")


def _load(root, **overrides):
    raw, approved = envelope_bytes(root, **overrides)
    return contract.load_envelope(raw, approved, root)


def test_valid_envelope_is_frozen_and_digest_bound(tmp_path):
    raw, approved = envelope_bytes(tmp_path)
    env = contract.load_envelope(raw, approved, tmp_path)
    assert env.digest == approved
    assert env.limits["max_total_attempts"] == 6
    with pytest.raises(Exception):
        env.digest = "x"


def test_approval_digest_must_match_exact_bytes(tmp_path):
    raw, approved = envelope_bytes(tmp_path)
    with pytest.raises(contract.ContractError):
        contract.load_envelope(raw, "0" * 64, tmp_path)
    with pytest.raises(contract.ContractError):
        contract.load_envelope(raw, None, tmp_path)


@pytest.mark.parametrize("key,value", [
    ("max_replans", 3), ("max_total_attempts", 7), ("max_concurrent_workers", 3),
    ("max_planner_calls", 4), ("max_final_report_calls", 2), ("stop_grace_seconds", 120),
    ("max_replans", 2.0), ("max_total_attempts", True),
])
def test_ceilings_are_fixed(tmp_path, key, value):
    limits = dict(envelope_dict(tmp_path)["limits"], **{key: value})
    with pytest.raises(contract.ContractError):
        _load(tmp_path, limits=limits)


@pytest.mark.parametrize("field,value", [
    ("deadline", float("inf")), ("deadline", float("nan")), ("worker_turn_limit", 0),
    ("verification_timeout_seconds", None), ("worker_turn_limit", 10**9),
])
def test_finite_bounds_required(tmp_path, field, value):
    with pytest.raises(contract.ContractError):
        _load(tmp_path, **{field: value})


def test_unknown_or_content_fields_rejected(tmp_path):
    with pytest.raises(contract.ContractError):
        _load(tmp_path, brief="synthetic prose that must not be persisted")


def test_duplicate_and_nonfinite_json_rejected(tmp_path):
    raw, _ = envelope_bytes(tmp_path)
    dup = raw[:-1] + b',"orchestration_id":"other"}'
    import hashlib
    with pytest.raises(contract.ContractError):
        contract.load_envelope(dup, hashlib.sha256(dup).hexdigest(), tmp_path)
    nan = raw.replace(b'"deadline":2000000000.0', b'"deadline":NaN')
    assert nan != raw
    with pytest.raises(contract.ContractError):
        contract.load_envelope(nan, hashlib.sha256(nan).hexdigest(), tmp_path)


def test_root_must_match_canonical_project(tmp_path):
    other = tmp_path / "other"
    other.mkdir()
    raw, approved = envelope_bytes(tmp_path)
    with pytest.raises(contract.ContractError):
        contract.load_envelope(raw, approved, other)
    with pytest.raises(contract.ContractError):
        _load(tmp_path, project_root="relative/path")


@pytest.mark.parametrize("field", ["policy_digest", "validator_digest", "goal_digest"])
def test_digest_references_strict(tmp_path, field):
    with pytest.raises(contract.ContractError):
        _load(tmp_path, **{field: "not-a-digest"})
    with pytest.raises(contract.ContractError):
        _load(tmp_path, **{field: None})


def test_baseline_must_be_commit_id(tmp_path):
    with pytest.raises(contract.ContractError):
        _load(tmp_path, baseline_commit="main")


def _task(task_id, deps=(), criteria=("C1",), **extra):
    from supervisor_helpers import digest
    return dict({"task_id": task_id, "depends_on": list(deps), "objective_digest": digest(task_id),
                 "criteria": list(criteria), "hypothesis_ids": []}, **extra)


def _proposal(tasks, revision=0, **extra):
    return canonical(dict({"schema_version": 1, "plan_revision": revision, "tasks": tasks}, **extra))


def test_valid_proposal_returns_bounded_tasks(tmp_path):
    env = _load(tmp_path)
    plan = contract.validate_proposal(_proposal([_task("W1"), _task("W2", ["W1"], ["C2"])]), env, 0)
    assert [t.task_id for t in plan.tasks] == ["W1", "W2"]
    assert plan.tasks[1].depends_on == ("W1",)
    assert plan.revision == 0


@pytest.mark.parametrize("extra", [
    {"acceptance_digest": "0" * 64}, {"limits": {"max_replans": 5}},
    {"capabilities": {"write": ["/"]}}, {"verification": "passed"}, {"attempts_used": 0},
])
def test_planner_cannot_set_authority_fields(tmp_path, extra):
    env = _load(tmp_path)
    with pytest.raises(contract.ContractError):
        contract.validate_proposal(_proposal([_task("W1")], **extra), env, 0)
    with pytest.raises(contract.ContractError):
        contract.validate_proposal(_proposal([_task("W1", **extra)]), env, 0)


@pytest.mark.parametrize("tasks", [
    [_task("W1", ["W2"]), _task("W2", ["W1"])],      # cycle
    [_task("W1", ["W1"])],                            # self-cycle
    [_task("W1", ["W9"])],                            # unknown dependency
    [_task("W1"), _task("W1")],                       # duplicate id
    [_task("W1", criteria=["C9"])],                   # criterion outside the envelope
    [_task("W1", hypothesis_ids=["free form predicate"])],
    [_task("W1", hypothesis_ids=["H2"])],             # unregistered hypothesis
    [_task(f"T{i}") for i in range(7)],               # more tasks than attempt ceiling
    [],
])
def test_proposal_rejects_bad_task_graphs(tmp_path, tasks):
    env = _load(tmp_path)
    with pytest.raises(contract.ContractError):
        contract.validate_proposal(_proposal(tasks), env, 0)


def test_stale_plan_revision_rejected(tmp_path):
    env = _load(tmp_path)
    with pytest.raises(contract.ContractError):
        contract.validate_proposal(_proposal([_task("W1")], revision=0), env, 1)


@pytest.mark.parametrize("field,value", [
    ("models", [["x"]]), ("criteria", [{}]), ("check_ids", [[1]]), ("hypothesis_ids", [["H"]]),
    ("capabilities", {"write": [["src"]], "commands": [], "mcp": []}),
    ("models", []), ("criteria", []), ("check_ids", []),
])
def test_unhashable_or_empty_lists_raise_contract_error(tmp_path, field, value):
    with pytest.raises(contract.ContractError):
        _load(tmp_path, **{field: value})


@pytest.mark.parametrize("task", [
    _task("W1", criteria=[["C1"]]), _task("W1", hypothesis_ids=[{}]), _task("W1", deps=[["W2"]]),
    dict(_task("W1"), task_id=["W1"]),
])
def test_unhashable_proposal_items_raise_contract_error(tmp_path, task):
    env = _load(tmp_path)
    with pytest.raises(contract.ContractError):
        contract.validate_proposal(_proposal([task]), env, 0)


def test_hypothesis_ids_may_be_empty(tmp_path):
    assert _load(tmp_path, hypothesis_ids=[]).hypothesis_ids == ()


def test_duplicate_key_diagnostic_does_not_echo_untrusted_key(tmp_path):
    with pytest.raises(contract.ContractError) as exc:
        contract.strict_json(b'{"synthetic-secret-key":1,"synthetic-secret-key":2}')
    assert "synthetic-secret-key" not in str(exc.value)


@pytest.mark.parametrize("value", [0, -5, True, 1.5, None, 10**13])
def test_max_usage_must_be_positive_bounded_integer(tmp_path, value):
    with pytest.raises(contract.ContractError):
        _load(tmp_path, max_usage=value)


def test_max_usage_is_required_and_exposed(tmp_path):
    assert _load(tmp_path, max_usage=5000).max_usage == 5000
    data = envelope_dict(tmp_path)
    data.pop("max_usage")
    raw = canonical(data)
    import hashlib
    with pytest.raises(contract.ContractError):
        contract.load_envelope(raw, hashlib.sha256(raw).hexdigest(), tmp_path)


@pytest.mark.parametrize("rel", ["../escape.json", "/abs.json", "a/../../x", "C:\\x", ""])
def test_owned_reference_cannot_escape(tmp_path, rel):
    with pytest.raises(contract.ContractError):
        contract.resolve_owned(tmp_path, rel)


def test_owned_reference_rejects_link_escape(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    owned = tmp_path / "owned"
    owned.mkdir()
    try:
        (owned / "link").symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("symlink creation not permitted on this host")
    with pytest.raises(contract.ContractError):
        contract.resolve_owned(owned, "link/file.json")
    assert contract.resolve_owned(owned, "sub/file.json") == (owned / "sub" / "file.json").resolve()

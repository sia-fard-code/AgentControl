import importlib.util
import os
from pathlib import Path
import sys

import pytest


CHECK = ("synthetic-check", "unit")
OTHER_CHECK = ("synthetic-check", "lint")


@pytest.fixture
def finish_gate(tmp_path):
    name = f"finish_gate_test_{id(tmp_path)}"
    path = Path(__file__).resolve().parents[1] / "engine" / "finish_gate.py"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
        yield module
    finally:
        sys.modules.pop(name, None)


def test_disk_required_checks_after_last_mutation_are_verified(tmp_path, finish_gate):
    plan = finish_gate.VerificationPlan((CHECK, OTHER_CHECK))
    run = finish_gate.RunEvidence("disk", str(tmp_path), plan)
    assert run.record_mutation("candidate.txt", "before", "after")
    for command in plan.commands:
        run.record_command(command, turn=1, outcome="exited", exit_code=0)
    gate = finish_gate.evaluate_finish(run.snapshot(), plan)
    assert gate == finish_gate.GateResult("verified_by_command", (), (), False)


def test_later_mutation_invalidates_successful_checks(tmp_path, finish_gate):
    plan = finish_gate.VerificationPlan((CHECK, OTHER_CHECK))
    run = finish_gate.RunEvidence("disk", str(tmp_path), plan)
    run.record_mutation("candidate.txt", "before", "after")
    for command in plan.commands:
        run.record_command(command, turn=1, outcome="exited", exit_code=0)
    run.record_mutation("candidate.txt", "after", "later")
    gate = finish_gate.evaluate_finish(run.snapshot(), plan)
    assert gate == finish_gate.GateResult(
        "unverified_post_edit_mutation", ("post_check_mutation",), (0, 1), True
    )
    run.record_command(CHECK, turn=2, outcome="exited", exit_code=0)
    assert finish_gate.evaluate_finish(run.snapshot(), plan).missing_check_ids == (1,)
    run.record_command(OTHER_CHECK, turn=2, outcome="exited", exit_code=0)
    assert finish_gate.evaluate_finish(run.snapshot(), plan).verification_status == (
        "verified_by_command"
    )


def test_latest_required_check_failure_supersedes_pass(tmp_path, finish_gate):
    plan = finish_gate.VerificationPlan((CHECK,))
    run = finish_gate.RunEvidence("disk", str(tmp_path), plan)
    run.record_mutation("candidate.txt", "before", "after")
    run.record_command(CHECK, turn=2, outcome="exited", exit_code=0)
    run.record_command(CHECK, turn=1, outcome="exited", exit_code=7)
    assert finish_gate.evaluate_finish(run.snapshot(), plan) == finish_gate.GateResult(
        "unverified_post_edit_mutation", ("required_check_nonzero",), (0,), True
    )
    run.record_command(CHECK, turn=3, outcome="exited", exit_code=0)
    assert finish_gate.evaluate_finish(run.snapshot(), plan).verification_status == (
        "verified_by_command"
    )


@pytest.mark.parametrize("write_mode", ["stage", "staged"])
def test_stage_command_success_never_verifies_pending(tmp_path, finish_gate, write_mode):
    plan = finish_gate.VerificationPlan((CHECK,))
    run = finish_gate.RunEvidence(write_mode, str(tmp_path), plan)
    run.record_mutation("candidate.txt", "before", "after")
    run.record_command(CHECK, turn=1, outcome="exited", exit_code=0)
    snapshot = run.snapshot()
    gate = finish_gate.evaluate_finish(snapshot, plan)
    assert snapshot.write_mode == "stage"
    assert gate == finish_gate.GateResult(
        "staged_unverified_on_disk", ("staged_edits_not_on_disk",), (0,), False
    )
    assert finish_gate.render_verification_block(gate, plan, snapshot).splitlines() == [
        "Verification: staged_unverified_on_disk",
        "Reasons: staged_edits_not_on_disk",
        "Note: staged edits were not written to disk; "
        "on-disk commands do not exercise pending edits.",
    ]


def test_no_designated_commands_does_not_accept_incidental_success(tmp_path, finish_gate):
    run = finish_gate.RunEvidence("disk", str(tmp_path))
    run.record_mutation("candidate.txt", "before", "after")
    run.record_command(CHECK, turn=1, outcome="exited", exit_code=0)
    assert run.snapshot().checks == ()
    assert finish_gate.evaluate_finish(run.snapshot()) == finish_gate.GateResult(
        "unverified_post_edit_mutation", ("no_verification_specified",), (), False
    )
    plan = finish_gate.VerificationPlan((OTHER_CHECK,))
    run = finish_gate.RunEvidence("disk", str(tmp_path), plan)
    run.record_mutation("candidate.txt", "before", "after")
    run.record_command(CHECK, turn=1, outcome="exited", exit_code=0)
    assert finish_gate.evaluate_finish(run.snapshot(), plan).reason_codes == (
        "required_check_not_run",
    )


@pytest.mark.parametrize("write_mode", ["disk", "stage"])
def test_noop_write_does_not_advance_mutation_sequence(tmp_path, finish_gate, write_mode):
    run = finish_gate.RunEvidence(write_mode, str(tmp_path))
    assert not run.record_mutation("candidate.txt", "", "")
    assert not run.record_mutation("candidate.txt", "same", "same", staged=True)
    assert run.snapshot().mutation_seq == 0
    assert run.snapshot().staged_paths == ()
    assert finish_gate.evaluate_finish(run.snapshot()).verification_status == "no_mutations"
    assert run.record_mutation("new.txt", None, "")
    assert run.snapshot().mutation_seq == 1


def test_reversion_does_not_erase_mutation_history(tmp_path, finish_gate):
    plan = finish_gate.VerificationPlan((CHECK,))
    run = finish_gate.RunEvidence("disk", str(tmp_path), plan)
    assert run.record_mutation("candidate.txt", "original", "edited")
    run.record_command(CHECK, turn=1, outcome="exited", exit_code=0)
    assert run.record_mutation("candidate.txt", "edited", "original")
    assert run.snapshot().mutation_seq == 2
    assert finish_gate.evaluate_finish(run.snapshot(), plan).reason_codes == (
        "post_check_mutation",
    )


@pytest.mark.parametrize("outcome", ["timed_out", "outcome_unknown"])
def test_unknown_outcome_is_not_exit_zero(tmp_path, finish_gate, outcome):
    plan = finish_gate.VerificationPlan((CHECK,))
    run = finish_gate.RunEvidence("disk", str(tmp_path), plan)
    run.record_mutation("candidate.txt", "before", "after")
    run.record_command(CHECK, turn=1, outcome=outcome, exit_code=0)
    snapshot = run.snapshot()
    assert snapshot.checks[0].exit_code is None
    assert snapshot.uncertain_mutation
    assert finish_gate.evaluate_finish(snapshot, plan) == finish_gate.GateResult(
        "unverified_post_edit_mutation", ("command_outcome_unknown",), (0,), False
    )


@pytest.mark.parametrize("source", ["other_root", "other_run"])
def test_check_from_other_root_or_run_is_rejected(tmp_path, finish_gate, source):
    plan = finish_gate.VerificationPlan((CHECK,))
    run = finish_gate.RunEvidence("disk", str(tmp_path / "current"), plan)
    run.record_mutation("candidate.txt", "before", "after")
    if source == "other_root":
        run.record_command(
            CHECK,
            turn=1,
            outcome="exited",
            exit_code=0,
            root_identity=str(tmp_path / "other"),
        )
    else:
        previous = finish_gate.RunEvidence("disk", str(tmp_path / "current"), plan)
        previous.record_command(CHECK, turn=1, outcome="exited", exit_code=0)
        assert previous.snapshot().checks
        assert not run.snapshot().checks
    assert finish_gate.evaluate_finish(run.snapshot(), plan) == finish_gate.GateResult(
        "unverified_post_edit_mutation", ("required_check_not_run",), (0,), True
    )


@pytest.mark.parametrize(
    "initial,replacement,error",
    [
        (None, None, None),
        (None, "", "on-disk base appeared after staging: candidate.txt"),
        ("", "", None),
        ("", None, "on-disk base disappeared or became symlink: candidate.txt"),
        ("before", "after", "on-disk base changed after staging: candidate.txt"),
        ("unchanged", "unchanged", None),
    ],
)
def test_staged_base_change_rejects_patch_capture(
    tmp_path, finish_gate, initial, replacement, error
):
    path = tmp_path / "candidate.txt"
    if initial is not None:
        path.write_text(initial, encoding="utf-8")
    tracker = finish_gate.StagedBaseTracker()
    tracker.record_if_first(path.name, str(path))
    if replacement is None:
        if path.exists():
            path.unlink()
    else:
        path.write_text(replacement, encoding="utf-8")
    tracker.record_if_first(path.name, str(path))
    if error is None:
        assert tracker.validate_and_get_base(path.name, str(path)) == (initial or "")
    else:
        with pytest.raises(finish_gate.StagedBaseChangedError) as caught:
            tracker.validate_and_get_base(path.name, str(path))
        assert str(caught.value) == error


@pytest.mark.parametrize(
    "outcome,exit_code,mutated_candidate,reason,correction_useful",
    [
        ("refused", 0, False, "required_check_refused", False),
        ("timed_out", 0, False, "command_outcome_unknown", False),
        ("outcome_unknown", 0, False, "command_outcome_unknown", False),
        ("exited", 8, False, "required_check_nonzero", True),
        ("exited", None, False, "required_check_nonzero", True),
        ("exited", 0, True, "post_check_mutation", True),
    ],
)
def test_unsuccessful_required_checks(
    tmp_path, finish_gate, outcome, exit_code, mutated_candidate, reason, correction_useful
):
    plan = finish_gate.VerificationPlan((CHECK,))
    run = finish_gate.RunEvidence("disk", str(tmp_path), plan)
    run.record_mutation("candidate.txt", "before", "after")
    run.record_command(
        CHECK,
        turn=1,
        outcome=outcome,
        exit_code=exit_code,
        mutated_candidate=mutated_candidate,
    )
    snapshot = run.snapshot()
    assert snapshot.mutation_seq == 1 + int(mutated_candidate)
    assert snapshot.checks[0].mutated_candidate is mutated_candidate
    assert snapshot.checks[0].exit_code == (exit_code if outcome == "exited" else None)
    assert finish_gate.evaluate_finish(snapshot, plan) == finish_gate.GateResult(
        "unverified_post_edit_mutation", (reason,), (0,), correction_useful
    )


@pytest.mark.parametrize("exit_code", [False, True])
@pytest.mark.parametrize("outcome", ["exited", "refused", "timed_out", "outcome_unknown"])
def test_bool_exit_codes_are_rejected(tmp_path, finish_gate, exit_code, outcome):
    plan = finish_gate.VerificationPlan((CHECK,))
    run = finish_gate.RunEvidence("disk", str(tmp_path), plan)
    with pytest.raises(ValueError, match="exit_code must be int or None, not bool"):
        run.record_command(CHECK, turn=1, outcome=outcome, exit_code=exit_code)
    assert run.snapshot().checks == ()
    assert run.snapshot().mutation_seq == 0
    assert not run.snapshot().uncertain_mutation
    with pytest.raises(ValueError, match="exit_code"):
        finish_gate.CheckEvidence(0, CHECK, 1, 0, str(tmp_path), outcome, exit_code)


@pytest.mark.parametrize("prior_mutation", [False, True])
def test_candidate_changing_check_requires_clean_rerun(tmp_path, finish_gate, prior_mutation):
    plan = finish_gate.VerificationPlan((CHECK, OTHER_CHECK))
    run = finish_gate.RunEvidence("disk", str(tmp_path), plan)
    if prior_mutation:
        run.record_mutation("candidate.txt", "before", "after")
    run.record_command(CHECK, turn=1, outcome="exited", exit_code=0)
    run.record_command(
        OTHER_CHECK, turn=2, outcome="exited", exit_code=0, mutated_candidate=True
    )
    assert finish_gate.evaluate_finish(run.snapshot(), plan) == finish_gate.GateResult(
        "unverified_post_edit_mutation", ("post_check_mutation",), (0, 1), True
    )
    for command in plan.commands:
        run.record_command(command, turn=3, outcome="exited", exit_code=0)
    assert finish_gate.evaluate_finish(run.snapshot(), plan).verification_status == (
        "verified_by_command"
    )


@pytest.mark.parametrize(
    "write_mode,with_plan,run_check,reasons,missing,correction_useful",
    [
        ("disk", False, False, (), (), False),
        ("disk", True, False, ("required_check_not_run",), (0,), True),
        ("disk", True, True, (), (), False),
        ("stage", False, False, (), (), False),
        ("stage", True, False, ("required_check_not_run",), (0,), False),
        ("stage", True, True, (), (), False),
    ],
)
def test_no_mutations_still_reports_required_checks(
    tmp_path, finish_gate, write_mode, with_plan, run_check, reasons, missing, correction_useful
):
    plan = finish_gate.VerificationPlan((CHECK,) if with_plan else ())
    run = finish_gate.RunEvidence(write_mode, str(tmp_path), plan)
    if run_check:
        run.record_command(CHECK, turn=1, outcome="exited", exit_code=0)
    assert finish_gate.evaluate_finish(run.snapshot(), plan) == finish_gate.GateResult(
        "no_mutations", reasons, missing, correction_useful
    )


@pytest.mark.parametrize("write_mode", ["disk", "stage"])
def test_unknown_mutation_cannot_be_cleared_by_success(tmp_path, finish_gate, write_mode):
    plan = finish_gate.VerificationPlan((CHECK,))
    run = finish_gate.RunEvidence(write_mode, str(tmp_path), plan)
    run.record_unknown_mutation()
    run.record_command(CHECK, turn=1, outcome="exited", exit_code=0)
    snapshot = run.snapshot()
    assert snapshot.mutation_seq == 1
    assert snapshot.uncertain_mutation
    assert finish_gate.evaluate_finish(snapshot, plan) == finish_gate.GateResult(
        "unverified_post_edit_mutation",
        ("command_outcome_unknown",),
        (0,) if write_mode == "disk" else (),
        False,
    )


def test_snapshot_merges_staged_paths_without_mutating_old_snapshot(tmp_path, finish_gate):
    plan = finish_gate.VerificationPlan((CHECK,))
    run = finish_gate.RunEvidence("disk", str(tmp_path), plan)
    run.record_mutation("b.txt", None, "", staged=True)
    snapshot = run.snapshot(["a.txt", "b.txt"])
    assert snapshot.staged_paths == ("a.txt", "b.txt")
    run.record_mutation("c.txt", None, "", staged=True)
    run.record_command(CHECK, turn=1, outcome="exited", exit_code=0)
    assert snapshot.mutation_seq == 1
    assert snapshot.checks == ()
    assert snapshot.staged_paths == ("a.txt", "b.txt")
    assert run.snapshot().staged_paths == ("b.txt", "c.txt")
    assert len(run.snapshot().checks) == 1


def test_reported_staged_paths_block_finish_without_mutations(tmp_path, finish_gate):
    run = finish_gate.RunEvidence("stage", str(tmp_path))
    snapshot = run.snapshot(["pending.txt"])
    gate = finish_gate.evaluate_finish(snapshot)
    assert snapshot.mutation_seq == 0
    assert gate == finish_gate.GateResult(
        "staged_unverified_on_disk", ("staged_edits_not_on_disk",), (), False
    )
    assert "Note: staged edits" in finish_gate.render_verification_block(
        gate, evidence=snapshot
    )


def test_plan_parsing_deduplicates_in_order(tmp_path, finish_gate):
    plan = finish_gate.VerificationPlan.from_cli(
        [
            '["synthetic-check", "unit"]',
            '["synthetic-check", "lint"]',
            '["synthetic-check", "unit"]',
            '["synthetic-check", ""]',
        ]
    )
    assert plan.commands == (CHECK, OTHER_CHECK, ("synthetic-check", ""))
    assert finish_gate.VerificationPlan.from_cli(None).commands == ()
    assert finish_gate.VerificationPlan.from_cli([]).commands == ()


@pytest.mark.parametrize(
    "raw",
    [
        "not json",
        "[]",
        '"synthetic-check"',
        "{}",
        "null",
        "true",
        '["", "arg"]',
        '["  ", "arg"]',
        '["synthetic-check", 0]',
        '["synthetic-check", false]',
        '["synthetic-check", null]',
        '["synthetic-check", []]',
    ],
)
def test_plan_parsing_rejects_invalid_commands(tmp_path, finish_gate, raw):
    with pytest.raises(ValueError) as caught:
        finish_gate.VerificationPlan.from_cli([raw])
    assert str(caught.value) == (
        "each --verify-command must be a JSON array of strings "
        "with a non-empty executable"
    )


@pytest.mark.parametrize(
    "outcome,exit_code",
    [
        ("invalid", None),
        ("exited", "0"),
        ("exited", 0.0),
        ("refused", 0),
        ("timed_out", 0),
        ("outcome_unknown", 0),
    ],
)
def test_check_evidence_rejects_invalid_fields(tmp_path, finish_gate, outcome, exit_code):
    with pytest.raises(ValueError):
        finish_gate.CheckEvidence(0, CHECK, 1, 0, str(tmp_path), outcome, exit_code)


@pytest.mark.parametrize("exit_code", [None, 0, 5])
def test_check_evidence_preserves_unknown_and_measured_exit(tmp_path, finish_gate, exit_code):
    check = finish_gate.CheckEvidence(0, CHECK, 1, 0, str(tmp_path), "exited", exit_code)
    assert check.exit_code is exit_code
    plan = finish_gate.VerificationPlan((CHECK,))
    snapshot = finish_gate.EvidenceSnapshot("disk", str(tmp_path), 1, False, (), (check,))
    gate = finish_gate.evaluate_finish(snapshot, plan)
    assert gate.reason_codes == ("post_check_mutation",)
    fresh = finish_gate.EvidenceSnapshot("disk", str(tmp_path), 0, False, (), (check,))
    expected = () if exit_code == 0 else ("required_check_nonzero",)
    assert finish_gate.evaluate_finish(fresh, plan).reason_codes == expected


def test_root_and_write_mode_normalization(tmp_path, finish_gate):
    plan = finish_gate.VerificationPlan((CHECK,))
    alias = str(tmp_path / "nested" / "..")
    run = finish_gate.RunEvidence("anything_else", alias, plan)
    assert run.write_mode == "disk"
    assert run.root_identity == os.path.normcase(os.path.realpath(str(tmp_path)))
    run.record_mutation("candidate.txt", "before", "after")
    run.record_command(
        CHECK, turn=1, outcome="exited", exit_code=0, root_identity=str(tmp_path)
    )
    assert finish_gate.evaluate_finish(run.snapshot(), plan).verification_status == (
        "verified_by_command"
    )


def test_reason_order_deduplicates_and_refusal_prevents_correction(tmp_path, finish_gate):
    commands = tuple(("synthetic-check", str(index)) for index in range(5))
    plan = finish_gate.VerificationPlan(commands)
    run = finish_gate.RunEvidence("disk", str(tmp_path), plan)
    run.record_mutation("candidate.txt", "before", "after")
    run.record_command(commands[1], turn=1, outcome="exited", exit_code=4)
    for command in commands[3:]:
        run.record_command(command, turn=1, outcome="refused")
    assert finish_gate.evaluate_finish(run.snapshot(), plan) == finish_gate.GateResult(
        "unverified_post_edit_mutation",
        ("required_check_not_run", "required_check_nonzero", "required_check_refused"),
        (0, 1, 2, 3, 4),
        False,
    )


def test_staged_base_auto_records_and_compares_raw_bytes(tmp_path, finish_gate):
    path = tmp_path / "candidate.txt"
    path.write_bytes(b"prefix\xff")
    tracker = finish_gate.StagedBaseTracker()
    assert tracker.validate_and_get_base(path.name, str(path)) == "prefix\ufffd"
    path.write_bytes(b"prefix\xfe")
    with pytest.raises(finish_gate.StagedBaseChangedError, match="base changed"):
        tracker.validate_and_get_base(path.name, str(path))
    missing = tmp_path / "missing.txt"
    assert tracker.validate_and_get_base(missing.name, str(missing)) == ""
    missing.write_bytes(b"")
    with pytest.raises(finish_gate.StagedBaseChangedError, match="base appeared"):
        tracker.validate_and_get_base(missing.name, str(missing))


@pytest.mark.parametrize("replacement", ["directory", "symlink", "dangling_symlink"])
def test_staged_base_rejects_nonregular_replacement(tmp_path, finish_gate, replacement):
    path = tmp_path / "candidate.txt"
    path.write_text("same", encoding="utf-8")
    tracker = finish_gate.StagedBaseTracker()
    tracker.record_if_first(path.name, str(path))
    path.unlink()
    if replacement == "directory":
        path.mkdir()
    else:
        target = tmp_path / "target.txt"
        if replacement == "symlink":
            target.write_text("same", encoding="utf-8")
        path.symlink_to(target)
    with pytest.raises(finish_gate.StagedBaseChangedError) as caught:
        tracker.validate_and_get_base(path.name, str(path))
    assert str(caught.value) == (
        "on-disk base disappeared or became symlink: candidate.txt"
    )


def test_staged_absent_base_rejects_dangling_symlink(tmp_path, finish_gate):
    path = tmp_path / "candidate.txt"
    tracker = finish_gate.StagedBaseTracker()
    tracker.record_if_first(path.name, str(path))
    path.symlink_to(tmp_path / "missing.txt")
    with pytest.raises(finish_gate.StagedBaseChangedError, match="base appeared"):
        tracker.validate_and_get_base(path.name, str(path))


def test_metadata_and_rendering_are_summary_only(tmp_path, finish_gate):
    plan = finish_gate.VerificationPlan((CHECK,))
    run = finish_gate.RunEvidence("disk", str(tmp_path), plan)
    run.record_mutation("candidate.txt", "synthetic private before", "synthetic private after")
    gate = finish_gate.evaluate_finish(run.snapshot(), plan)
    assert gate.to_metadata() == {
        "verification_status": "unverified_post_edit_mutation",
        "reason_codes": ["required_check_not_run"],
        "missing_check_ids": [0],
        "correction_useful": True,
    }
    assert finish_gate.render_verification_block(gate).splitlines() == [
        "Verification: unverified_post_edit_mutation",
        "Reasons: required_check_not_run",
    ]
    clean = finish_gate.GateResult("no_mutations", (), (), False)
    assert finish_gate.render_verification_block(clean) == "Verification: no_mutations"
    assert clean.verification_status in finish_gate.VERIFICATION_STATUSES
    assert set(gate.reason_codes) <= set(finish_gate.REASON_CODES)

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from typing import Sequence


VERIFICATION_STATUSES = (
    "verified_by_command",
    "unverified_post_edit_mutation",
    "staged_unverified_on_disk",
    "no_mutations",
)
REASON_CODES = (
    "no_verification_specified",
    "required_check_not_run",
    "required_check_nonzero",
    "required_check_refused",
    "post_check_mutation",
    "command_outcome_unknown",
    "staged_edits_not_on_disk",
    "candidate_state_unavailable",
)


class StagedBaseChangedError(ValueError):
    pass


@dataclass(frozen=True)
class _StagedBase:
    exists: bool
    sha256: str | None
    content: str


class StagedBaseTracker:
    def __init__(self) -> None:
        self._bases: dict[str, _StagedBase] = {}
        self._by_disk: dict[str, str] = {}

    def record_if_first(self, rel_path: str, disk_path: str) -> None:
        disk_key = os.path.normcase(os.path.realpath(disk_path))
        self._by_disk.setdefault(disk_key, rel_path)
        if rel_path in self._bases:
            return
        if os.path.lexists(disk_path):
            with open(disk_path, "rb") as source:
                raw = source.read()
            self._bases[rel_path] = _StagedBase(
                True, hashlib.sha256(raw).hexdigest(), raw.decode("utf-8", errors="replace")
            )
        else:
            self._bases[rel_path] = _StagedBase(False, None, "")

    def validate(self, path: str, disk_path: str | None = None) -> str:
        if disk_path is not None:
            return self.validate_and_get_base(path, disk_path)
        if path in self._bases:
            return self.validate_and_get_base(path, path)
        disk_key = os.path.normcase(os.path.realpath(path))
        rel_path = self._by_disk.get(disk_key, path)
        return self.validate_and_get_base(rel_path, path)

    def validate_and_get_base(self, rel_path: str, disk_path: str) -> str:
        self.record_if_first(rel_path, disk_path)
        base = self._bases[rel_path]
        if not base.exists and os.path.lexists(disk_path):
            raise StagedBaseChangedError(
                f"on-disk base appeared after staging: {rel_path}"
            )
        if base.exists:
            if not os.path.isfile(disk_path) or os.path.islink(disk_path):
                raise StagedBaseChangedError(
                    f"on-disk base disappeared or became symlink: {rel_path}"
                )
            with open(disk_path, "rb") as source:
                raw = source.read()
            if hashlib.sha256(raw).hexdigest() != base.sha256:
                raise StagedBaseChangedError(
                    f"on-disk base changed after staging: {rel_path}"
                )
        return base.content


@dataclass(frozen=True)
class VerificationPlan:
    commands: tuple[tuple[str, ...], ...] = ()

    @classmethod
    def from_cli(cls, raw_commands: Sequence[str] | None) -> VerificationPlan:
        if not raw_commands:
            return cls(())
        commands: list[tuple[str, ...]] = []
        message = (
            "each --verify-command must be a JSON array of strings "
            "with a non-empty executable"
        )
        for raw in raw_commands:
            try:
                item = json.loads(raw)
            except (ValueError, TypeError) as exc:
                raise ValueError(message) from exc
            if (
                not isinstance(item, list)
                or not item
                or not all(isinstance(arg, str) for arg in item)
                or not item[0].strip()
            ):
                raise ValueError(message)
            command = tuple(item)
            if command not in commands:
                commands.append(command)
        return cls(tuple(commands))


@dataclass(frozen=True)
class CheckEvidence:
    check_id: int
    argv: tuple[str, ...]
    turn: int
    mutation_seq: int
    root_identity: str
    outcome: str
    exit_code: int | None
    mutated_candidate: bool = False

    def __post_init__(self) -> None:
        if self.outcome not in ("exited", "refused", "timed_out", "outcome_unknown"):
            raise ValueError("invalid command outcome")
        if self.exit_code is not None and (
            not isinstance(self.exit_code, int) or isinstance(self.exit_code, bool)
        ):
            raise ValueError("exit_code must be int or None, not bool or another type")
        if self.outcome != "exited" and self.exit_code is not None:
            raise ValueError("exit_code must be None unless outcome is exited")


@dataclass(frozen=True)
class EvidenceSnapshot:
    write_mode: str
    root_identity: str
    mutation_seq: int
    uncertain_mutation: bool
    staged_paths: tuple[str, ...]
    checks: tuple[CheckEvidence, ...]
    # Sticky: an exited command may have changed disk files nobody observed.
    candidate_unavailable: bool = False


@dataclass(frozen=True)
class GateResult:
    verification_status: str
    reason_codes: tuple[str, ...]
    missing_check_ids: tuple[int, ...]
    correction_useful: bool

    def to_metadata(self) -> dict[str, object]:
        return {
            "verification_status": self.verification_status,
            "reason_codes": list(self.reason_codes),
            "missing_check_ids": list(self.missing_check_ids),
            "correction_useful": self.correction_useful,
        }


class RunEvidence:
    def __init__(
        self,
        write_mode: str,
        root_identity: str,
        plan: VerificationPlan | None = None,
    ) -> None:
        self.write_mode = "stage" if write_mode in ("stage", "staged") else "disk"
        self.root_identity = os.path.normcase(os.path.realpath(root_identity))
        self.plan = plan or VerificationPlan()
        self.mutation_seq = 0
        self.uncertain_mutation = False
        self.candidate_unavailable = False
        self._staged_paths: set[str] = set()
        self._checks: list[CheckEvidence] = []

    def record_mutation(
        self, path: str, before: str | None, after: str, *, staged: bool = False
    ) -> bool:
        if before is not None and before == after:
            return False
        self.mutation_seq += 1
        if staged or self.write_mode == "stage":
            self._staged_paths.add(path)
        return True

    def record_unknown_mutation(self) -> None:
        self.uncertain_mutation = True
        self.mutation_seq += 1

    def record_candidate_unavailable(self) -> None:
        """A command exited normally but its disk effects were not observed."""
        self.candidate_unavailable = True

    def record_command(
        self,
        argv: Sequence[str],
        *,
        turn: int,
        outcome: str,
        exit_code: int | None = None,
        root_identity: str | None = None,
        mutated_candidate: bool = False,
    ) -> None:
        if isinstance(exit_code, bool):
            raise ValueError("exit_code must be int or None, not bool")
        if outcome in ("timed_out", "outcome_unknown"):
            exit_code = None
            self.uncertain_mutation = True
        if outcome == "refused":
            exit_code = None
        if mutated_candidate:
            self.mutation_seq += 1
        cmd_tuple = tuple(str(arg) for arg in argv)
        eff_root = (
            os.path.normcase(os.path.realpath(root_identity))
            if root_identity is not None
            else self.root_identity
        )
        for idx, required_cmd in enumerate(self.plan.commands):
            if cmd_tuple == required_cmd:
                self._checks.append(
                    CheckEvidence(
                        check_id=idx,
                        argv=cmd_tuple,
                        turn=turn,
                        mutation_seq=self.mutation_seq,
                        root_identity=eff_root,
                        outcome=outcome,
                        exit_code=exit_code,
                        mutated_candidate=mutated_candidate,
                    )
                )

    def snapshot(self, staged_paths: Sequence[str] | None = None) -> EvidenceSnapshot:
        return EvidenceSnapshot(
            write_mode=self.write_mode,
            root_identity=self.root_identity,
            mutation_seq=self.mutation_seq,
            uncertain_mutation=self.uncertain_mutation,
            staged_paths=tuple(sorted(self._staged_paths.union(staged_paths or ()))),
            checks=tuple(self._checks),
            candidate_unavailable=self.candidate_unavailable,
        )


def _evaluate_plan_checks(
    evidence: EvidenceSnapshot,
    plan: VerificationPlan,
    require_after_mutation: bool,
) -> tuple[tuple[str, ...], tuple[int, ...], bool]:
    reasons: list[str] = []
    missing: list[int] = []
    for check_id in range(len(plan.commands)):
        matching = [
            check
            for check in evidence.checks
            if check.check_id == check_id
            and check.root_identity == evidence.root_identity
        ]
        if not matching:
            reason = "required_check_not_run"
        else:
            latest = matching[-1]
            if latest.mutated_candidate:
                reason = "post_check_mutation"
            elif require_after_mutation and latest.mutation_seq < evidence.mutation_seq:
                reason = "post_check_mutation"
            elif latest.outcome in ("timed_out", "outcome_unknown"):
                reason = "command_outcome_unknown"
            elif latest.outcome == "refused":
                reason = "required_check_refused"
            elif (
                latest.outcome == "exited"
                and latest.exit_code == 0
                and not isinstance(latest.exit_code, bool)
            ):
                continue
            else:
                reason = "required_check_nonzero"
        missing.append(check_id)
        if reason not in reasons:
            reasons.append(reason)
    can_correct = (
        bool(missing)
        and "command_outcome_unknown" not in reasons
        and "required_check_refused" not in reasons
    )
    return tuple(reasons), tuple(missing), can_correct


def evaluate_finish(
    evidence: EvidenceSnapshot, plan: VerificationPlan | None = None
) -> GateResult:
    plan = plan or VerificationPlan()
    if evidence.write_mode == "stage":
        if evidence.uncertain_mutation:
            return GateResult(
                "unverified_post_edit_mutation", ("command_outcome_unknown",), (), False
            )
        if evidence.mutation_seq > 0 or evidence.staged_paths:
            return GateResult(
                "staged_unverified_on_disk",
                ("staged_edits_not_on_disk",),
                tuple(range(len(plan.commands))),
                False,
            )
        reasons, missing, _ = _evaluate_plan_checks(
            evidence, plan, require_after_mutation=False
        )
        return GateResult("no_mutations", reasons, missing, False)

    if evidence.uncertain_mutation:
        return GateResult(
            "unverified_post_edit_mutation",
            ("command_outcome_unknown",),
            tuple(range(len(plan.commands))),
            False,
        )
    if evidence.candidate_unavailable:
        # Sticky: a later clean check cannot prove what unseen files contained.
        return GateResult(
            "unverified_post_edit_mutation",
            ("candidate_state_unavailable",),
            tuple(range(len(plan.commands))),
            False,
        )
    if evidence.mutation_seq == 0:
        reasons, missing, can_correct = _evaluate_plan_checks(
            evidence, plan, require_after_mutation=False
        )
        return GateResult("no_mutations", reasons, missing, can_correct)
    if not plan.commands:
        return GateResult(
            "unverified_post_edit_mutation", ("no_verification_specified",), (), False
        )
    reasons, missing, can_correct = _evaluate_plan_checks(
        evidence, plan, require_after_mutation=True
    )
    if not missing and not reasons:
        return GateResult("verified_by_command", (), (), False)
    return GateResult("unverified_post_edit_mutation", reasons, missing, can_correct)


def render_verification_block(
    gate: GateResult,
    plan: VerificationPlan | None = None,
    evidence: EvidenceSnapshot | None = None,
) -> str:
    lines = [f"Verification: {gate.verification_status}"]
    if gate.reason_codes:
        lines.append(f"Reasons: {', '.join(gate.reason_codes)}")
    if (
        evidence is not None
        and evidence.write_mode == "stage"
        and (evidence.mutation_seq > 0 or evidence.staged_paths)
    ):
        lines.append(
            "Note: staged edits were not written to disk; "
            "on-disk commands do not exercise pending edits."
        )
    return "\n".join(lines)

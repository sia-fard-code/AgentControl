"""Bound resilient-v1 runtime activation (spec section 3, plan Task 3.4).

Reads an operator-prepared closed contract once before any model dispatch, validates it
against caller-supplied actual facts and drives recovery_handoff begin/settle around
potentially mutating tool calls. Observations only: it grants no permission, cap, root,
continuation or task acceptance. Raw argv/content are never stored here.
"""
import copy
import hashlib
import os
import uuid
from types import MappingProxyType

import recovery_handoff as rh
import supervisor_contract as contract
import supervisor_evidence as evidence_mod

PROFILE = rh.PROFILE
CONTRACT_KEYS = frozenset({"schema_version", "profile", "launch_id", "binding", "spec",
                           "candidate_root_identity", "owned_dir"})
# Literal read-only tools need no intent; every other tool (incl. mcp__*) is conservative.
READ_ONLY = frozenset({"read_file", "list_dir", "grep", "view_image", "finish",
                       "list_mcp_tools", "load_skill"})


class RecoveryRuntimeError(Exception):
    """Fixed reason code only; never contract bytes, paths or OS messages."""

    def __init__(self, code):
        super().__init__(code)
        self.code = code


def _canon(path):
    return os.path.normcase(os.path.realpath(path))


def unbound_metadata():
    return {"recovery_profile": PROFILE, "recovery_contract_sha256": None,
            "recovery_handoff_status": "unbound", "recovery_handoff_sha256": None,
            "recovery_error": "missing_contract"}


def _contract_shape(data):
    if type(data) is not dict or set(data) != CONTRACT_KEYS \
            or type(data["schema_version"]) is not int or data["schema_version"] not in (1, 2) \
            or data["profile"] != PROFILE or type(data["launch_id"]) is not str:
        raise RecoveryRuntimeError("invalid_contract")
    try:
        if str(uuid.UUID(data["launch_id"])) != data["launch_id"]:
            raise ValueError
        rh._binding(data["binding"], None, schema_version=data["schema_version"])
        rh._spec(data["spec"])
        for key in ("candidate_root_identity", "owned_dir"):
            if os.path.normcase(os.path.normpath(rh._abs(data[key], "x"))) != data[key]:
                raise ValueError
            if data["schema_version"] == 2 and "\0" in data[key]:
                raise ValueError
    except (rh.HandoffError, ValueError):
        raise RecoveryRuntimeError("invalid_contract") from None


def _freeze(data):
    if type(data) is dict:
        return MappingProxyType({key: _freeze(value) for key, value in data.items()})
    if type(data) is list:
        return tuple(_freeze(value) for value in data)
    return data


def _cached_copy(data):
    if type(data) is MappingProxyType:
        return {key: _cached_copy(value) for key, value in data.items()}
    return copy.deepcopy(data)


def read_contract(path):
    """Strict v1/v2 data parse and raw pin. Neither a version nor a digest is authority."""
    try:
        raw = rh._read_bytes(os.fspath(path), rh.MAX_BYTES)
        data = contract.strict_json(raw, max_bytes=rh.MAX_BYTES)
    except (rh.HandoffError, contract.ContractError, OSError, TypeError, ValueError):
        raise RecoveryRuntimeError("invalid_contract") from None
    _contract_shape(data)
    return (_freeze(data) if data["schema_version"] == 2 else data,
            hashlib.sha256(raw).hexdigest())


def _boundary2_checks(boundary, data, digest, *, path, policy_root, candidate_root,
                      run_id, launch_id, baseline_commit, identity_provider):
    """Internal caller must pass validate_launch2's result, not a worker JSON claim.

    Structural protocol checks are loader-alias safe; they do not prove provenance or
    create permissions. The trusted caller captures identity from the approved scope and
    actual administration/ownership guards. No callback is reconstructed from JSON.
    """
    if not callable(identity_provider):
        raise RecoveryRuntimeError("identity_provider_required")
    try:
        m = boundary.manifest
        if m.schema != "agentcontrol.supervisor-manifest/v2" \
                or m.profile != PROFILE or m.role != "worker" \
                or not callable(boundary.check_write) or not callable(boundary.check_command) \
                or boundary.write_paths != m.write_paths or boundary.commands != m.commands:
            raise RecoveryRuntimeError("invalid_boundary")
        # The trusted launch boundary permits ordinary native path spelling.
        # Normalize it lexically (no metadata-directed filesystem probes); records
        # and contract bindings themselves retain their strict canonical spelling.
        root = os.path.normcase(os.path.normpath(rh._abs(m.root, "invalid_boundary")))
        run_dir = os.path.normcase(os.path.normpath(rh._abs(m.run_dir, "invalid_boundary")))
        manifest_candidate = os.path.normcase(os.path.normpath(
            rh._abs(m.candidate_root, "invalid_boundary")))
        # This is the validated runtime root, not a root claimed by record JSON.
        boundary_candidate = _canon(boundary.candidate_root)
        if type(m.candidate.inputs) is not tuple:
            raise RecoveryRuntimeError("invalid_boundary")
        scope = hashlib.sha256(contract.canonical(list(m.candidate.inputs))).hexdigest()
        if m.candidate.kind == "git":
            origin = rh._origin({"kind": "git", "baseline_commit": m.candidate.baseline_commit})
        elif m.candidate.kind == "artifact-manifest":
            origin = rh._origin({"kind": "artifact-manifest",
                                 "initial_sha256": m.candidate.initial_sha256})
        else:
            raise RecoveryRuntimeError("invalid_boundary")
        envelope = rh._sha(m.envelope_sha256, "invalid_boundary")
        seed = rh._sha(m.seed_candidate, "invalid_boundary")
        pin = rh._sha(m.recovery_contract_sha256, "invalid_boundary")
        contract.validate_owned_reference(m.recovery_contract_ref)
        contract_path = os.path.normcase(os.path.normpath(
            os.path.join(run_dir, *m.recovery_contract_ref.split("/"))))
    except (AttributeError, TypeError, ValueError, rh.HandoffError, contract.ContractError):
        raise RecoveryRuntimeError("invalid_boundary") from None
    b = data["binding"]
    checks = (
        ("root_mismatch", root == _canon(policy_root)),
        ("run_mismatch", m.run_id == run_id),
        ("launch_mismatch", m.launch_id == launch_id),
        ("candidate_mismatch", manifest_candidate == candidate_root == boundary_candidate),
        ("candidate_mismatch", candidate_root == os.path.normcase(
            os.path.normpath(os.path.join(run_dir, "candidate")))),
        ("isolation_required", not rh._inside(root, run_dir) and not rh._inside(run_dir, root)),
        ("contract_mismatch", contract_path == _canon(path)
         and rh._inside(run_dir, contract_path) and not rh._inside(candidate_root, contract_path)),
        ("contract_mismatch", digest == pin),
        ("envelope_mismatch", b["envelope_sha256"] == envelope),
        ("seed_mismatch", b["seed_sha256"] == seed),
        ("scope_mismatch", b["scope_sha256"] == scope),
        ("origin_mismatch", b["origin"] == origin),
        ("verifier_owner_mismatch", data["spec"]["verifier_owner"] == "coordinator"),
    )
    for code, ok in checks:
        if not ok:
            raise RecoveryRuntimeError(code)
    if origin["kind"] == "git" and baseline_commit != origin["baseline_commit"]:
        raise RecoveryRuntimeError("baseline_mismatch")


class RecoveryRuntime:
    def __init__(self, data, digest, plan, identity_provider=None):
        self.schema_version = data["schema_version"]
        self.binding, self.spec = data["binding"], data["spec"]
        self.candidate_root, self.owned_dir = data["candidate_root_identity"], data["owned_dir"]
        self.contract_sha256, self.plan = digest, plan
        self._identity_provider = identity_provider
        self.handoff_sha256 = self.error = None

    @classmethod
    def prepare(cls, path, *, run_id, task_text, policy_root, baseline_commit=None, candidate_root,
                launch_id, artifact_dir, plan, parsed=None, boundary=None, identity_provider=None):
        """Validate the contract against caller-approved ACTUAL facts; claims grant nothing."""
        cached, digest = read_contract(path) if parsed is None else parsed
        data = _cached_copy(cached)
        _contract_shape(data)
        b, cand = data["binding"], _canon(candidate_root)
        if data["schema_version"] == 2:
            _boundary2_checks(boundary, data, digest, path=path, policy_root=policy_root,
                              candidate_root=cand, run_id=run_id, launch_id=launch_id,
                              baseline_commit=baseline_commit, identity_provider=identity_provider)
        checks = (
            ("launch_mismatch", data["launch_id"] == launch_id),
            ("run_mismatch", b["run_id"] == run_id),
            ("task_mismatch", b["task_sha256"] == hashlib.sha256(
                task_text.encode("utf-8")).hexdigest()),
            ("baseline_mismatch", data["schema_version"] == 2
             or b["baseline_commit"] == baseline_commit),
            ("root_mismatch", b["root_identity"] == _canon(policy_root)),
            ("candidate_mismatch", data["candidate_root_identity"] == cand),
            # The policy project itself (or anything containing it) is not an owned candidate.
            ("isolation_required", not rh._inside(cand, b["root_identity"])),
            ("artifact_mismatch", data["owned_dir"] == _canon(artifact_dir)),
            ("artifact_inside_candidate", not rh._inside(cand, data["owned_dir"])),
            ("contract_inside_candidate", not rh._inside(cand, _canon(path))),
        )
        for code, ok in checks:
            if not ok:
                raise RecoveryRuntimeError(code)
        try:
            rh._check_spec_plan(data["spec"], plan)
            missing = []
            directory = os.fspath(artifact_dir)
            while not os.path.lexists(directory):
                missing.append(directory)
                parent = os.path.dirname(directory)
                if parent == directory:
                    raise RecoveryRuntimeError("artifact_mismatch")
                directory = parent
            for directory in reversed(missing):
                os.mkdir(directory)
                rh._reused_dirbarrier(os.path.dirname(directory))
                rh._reused_dirbarrier(directory)
            owned = rh._owned(data["owned_dir"])
            rh._reused_dirbarrier(owned)
            current, _ = rh._load(owned, missing_ok=True)
            if current is not None:
                raise RecoveryRuntimeError("handoff_exists")
        except rh.HandoffError as exc:
            raise RecoveryRuntimeError(exc.code) from None
        except OSError:
            raise RecoveryRuntimeError("durability_unsupported") from None
        return cls(data, digest, plan,
                   identity_provider=identity_provider if data["schema_version"] == 2 else None)

    @property
    def blocked(self):
        return self.error is not None

    def _fail(self, code):
        if self.error is None:
            self.error = code
        self.handoff_sha256 = None

    def _identity(self, evidence):
        if self.schema_version == 2:
            try:
                identity = rh._sha(self._identity_provider(self.candidate_root),
                                   "invalid_candidate", nullable=True)
            except Exception:
                identity = None
        else:
            try:
                identity = evidence_mod.candidate_identity(self.candidate_root)
            except (evidence_mod.EvidenceError, OSError):
                identity = None
        if identity is None:
            evidence.record_candidate_unavailable()
        return identity

    def before_call(self, tool, evidence, *, verification=False):
        """Durable intent BEFORE a potentially mutating dispatch; raises => do not dispatch."""
        if self.blocked:
            raise RecoveryRuntimeError("recovery_blocked")
        if tool in READ_ONLY and not verification:
            return None
        before = self._identity(evidence) if verification else None
        try:
            pending = rh.begin(self.owned_dir, binding=self.binding, spec=self.spec,
                               evidence=evidence, before_sha256=before,
                               schema_version=self.schema_version)
            self.handoff_sha256 = None
            return pending
        except rh.HandoffError as exc:
            self._fail(exc.code)
            raise RecoveryRuntimeError(exc.code) from None
        except OSError:
            self._fail("publish_failed")
            raise RecoveryRuntimeError("publish_failed") from None

    def after_call(self, pending, evidence, *, verification=False):
        """Settle after the action ran; never raises (the effect already happened)."""
        if pending is None:
            return None
        after = self._identity(evidence) if verification else None
        try:
            self.handoff_sha256 = rh.settle(self.owned_dir, pending, evidence=evidence,
                                            after_sha256=after)
        except rh.HandoffError as exc:
            self._fail(exc.code)
        except OSError:
            self._fail("publish_failed")
        return None

    def checkpoint(self, evidence):
        """Final machine checkpoint (call BEFORE any report fallback); returns handoff sha."""
        if self.blocked:
            return None
        try:
            current, raw = rh._load(rh._owned(self.owned_dir), missing_ok=True)
            now = self._identity(evidence)
            if current is not None and current["phase"] == "settled":
                last = current["after"]["candidate_sha256"]
                if (now is not None and now == last
                        and current["evidence"] == rh._meta(evidence)):
                    # Only a known current identity can retain current attestation.
                    self.handoff_sha256 = hashlib.sha256(raw).hexdigest()
                    return self.handoff_sha256
                before = last  # an unrecorded external delta (last != now) stays owed
            else:
                before = None
            pending = rh.begin(self.owned_dir, binding=self.binding, spec=self.spec,
                               evidence=evidence, before_sha256=before,
                               schema_version=self.schema_version)
            self.handoff_sha256 = rh.settle(self.owned_dir, pending, evidence=evidence,
                                            after_sha256=now)
        except rh.HandoffError as exc:
            self._fail(exc.code)
            return None
        except OSError:
            self._fail("publish_failed")
            return None
        return self.handoff_sha256

    def metadata(self):
        meta = {"recovery_profile": PROFILE, "recovery_contract_sha256": self.contract_sha256,
                "recovery_handoff_status": "error" if self.blocked else "bound",
                "recovery_handoff_sha256": self.handoff_sha256}
        if self.blocked:
            meta["recovery_error"] = self.error
        return meta

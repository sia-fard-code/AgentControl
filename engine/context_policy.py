"""Pure context-policy records and cold resolution. No filesystem, network or settings access."""
from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction

MODES = ("legacy", "model-v1")
FIELDS = ("auto_compact", "compact_fraction", "context_policy",
          "context_reply_tokens", "context_safety_tokens", "context_window_tokens")
PROVENANCES = ("default", "explicit", "frozen")
DEFAULT_FRACTION = 0.70
CODES = frozenset({
    "context_overrides_frozen", "context_options_require_model_v1", "invalid_context_policy",
    "invalid_window_tokens", "invalid_reply_tokens", "invalid_safety_tokens",
    "invalid_route_output_limit", "reply_below_route_output_limit", "input_allowance_impossible",
    "invalid_auto_compact", "invalid_compact_fraction", "compaction_trigger_invalid",
    "compaction_unavailable_in_oneshot", "invalid_selection",
})


class ContextPolicyError(ValueError):
    """Carries only a fixed code; never echoes argument values."""

    def __init__(self, code: str):
        if code not in CODES:
            raise ValueError("unknown context policy error code")
        super().__init__(code)
        self.code = code


def _posint(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


@dataclass(frozen=True)
class Policy:
    window_tokens: int
    reply_tokens: int
    safety_tokens: int
    auto_compact: bool = False
    compact_fraction: float = DEFAULT_FRACTION
    verified_output_limit: int | None = None  # verified enforced bound for the request, else None

    def __post_init__(self):
        for name in ("window_tokens", "reply_tokens", "safety_tokens"):
            if not _posint(getattr(self, name)):
                raise ContextPolicyError(f"invalid_{name}")
        limit = self.verified_output_limit
        if limit is not None and not _posint(limit):
            raise ContextPolicyError("invalid_route_output_limit")
        if limit is not None and self.reply_tokens < limit:
            raise ContextPolicyError("reply_below_route_output_limit")
        if self.input_limit_tokens <= 0:
            raise ContextPolicyError("input_allowance_impossible")
        if not isinstance(self.auto_compact, bool):
            raise ContextPolicyError("invalid_auto_compact")
        f = self.compact_fraction
        # range check first: comparisons are exact for huge ints and reject NaN/inf without float()
        if isinstance(f, bool) or not isinstance(f, (int, float)) or not 0 < f < 1:
            raise ContextPolicyError("invalid_compact_fraction")
        object.__setattr__(self, "compact_fraction", float(f))
        if self.auto_compact and not (
                0 < self.compact_target_tokens < self.compact_trigger_tokens < self.input_limit_tokens):
            raise ContextPolicyError("compaction_trigger_invalid")

    @property
    def input_limit_tokens(self) -> int:
        return self.window_tokens - self.reply_tokens - self.safety_tokens

    @property
    def compact_trigger_tokens(self) -> int:
        # exact decimal ratio: no float overflow for huge windows and 0.7 * 1M stays 700000
        return Fraction(str(self.compact_fraction)) * self.window_tokens // 1

    @property
    def compact_target_tokens(self) -> int:
        return self.compact_trigger_tokens * 4 // 5  # floor(0.8 * trigger), exact in ints


@dataclass(frozen=True)
class Selection:
    policy: Policy | None  # None means exact legacy behavior
    provenance: str
    explicit_fields: tuple[str, ...] = ()
    provider_profile: tuple[tuple[str, object], ...] = ()

    def __post_init__(self):
        if not isinstance(self.explicit_fields, (list, tuple)):
            raise ContextPolicyError("invalid_selection")
        fields = tuple(self.explicit_fields)
        p, prov = self.policy, self.provenance
        if p is None:  # frozen: nothing chosen; non-frozen legacy only via explicit --context-policy
            ok = (prov == "frozen" and not fields) or (prov == "explicit" and fields == ("context_policy",))
        else:  # model-v1: explicit exactly when --context-policy was supplied
            ok = (isinstance(p, Policy) and prov in ("default", "explicit")
                  and ("context_policy" in fields) == (prov == "explicit"))
        # membership test before set(): unhashable members are rejected by `in`, never hashed
        if not ok or any(f not in FIELDS for f in fields) or fields != tuple(sorted(set(fields))):
            raise ContextPolicyError("invalid_selection")
        object.__setattr__(self, "explicit_fields", fields)
        profile = self.provider_profile
        if profile:
            enums = {
                "profile_id": ("gpt_6_1_sol", "claude_opus_5_5_think", "unprofiled"),
                "route": ("chat", "responses", "unknown"),
                "effective_effort": ("none", "low", "medium", "high", "xhigh", "max", "unknown"),
                "evidence_effort": ("high", "medium", None),
                "evidence_source": ("gpt_retention_20261102", "claude_retention_20261102", None),
                "capacity_evidence_state": ("observed", "scope_mismatch", "unverified"),
                "cap_support": ("observed", "unsupported", "unverified"),
                "cap_evidence_state": ("numeric_only", "ignored", "unverified"),
                "window_provenance": ("operating_profile", "existing_configuration", "operator_override"),
            }
            numbers = ("configured_operating_window", "observed_retained_input_lower_bound", "declared_max")
            keys = (*enums, *numbers, "evidence_match")
            if (p is None or not isinstance(profile, (list, tuple))
                    or any(not isinstance(item, (list, tuple)) or len(item) != 2
                           or item[0] not in keys for item in profile)):
                raise ContextPolicyError("invalid_selection")
            data = dict(profile)
            if (len(profile) != len(keys) or set(data) != set(keys)
                    or any(data[k] not in allowed for k, allowed in enums.items())
                    or any(not (_posint(data[k]) or data[k] is None) for k in numbers)
                    or not _posint(data["configured_operating_window"])
                    or type(data["evidence_match"]) is not bool):
                raise ContextPolicyError("invalid_selection")
            profile = tuple((k, data[k]) for k in keys)
        elif profile != ():
            raise ContextPolicyError("invalid_selection")
        object.__setattr__(self, "provider_profile", profile)

    def metadata(self) -> dict:
        meta = {"context_policy": "legacy" if self.policy is None else "model-v1",
                "provenance": self.provenance, "explicit_fields": list(self.explicit_fields)}
        if self.policy is not None:
            p = self.policy
            meta.update(window_tokens=p.window_tokens, reply_tokens=p.reply_tokens,
                        safety_tokens=p.safety_tokens, input_limit_tokens=p.input_limit_tokens,
                        auto_compact=p.auto_compact, compact_fraction=p.compact_fraction,
                        compact_trigger_tokens=p.compact_trigger_tokens,
                        compact_target_tokens=p.compact_target_tokens,
                        verified_output_limit=p.verified_output_limit)
        if self.provider_profile:
            meta["provider_profile"] = dict(self.provider_profile)
        return meta


def resolve_policy(args, *, model_window: int = 1_000_000, route_output_limit: int | None = None,
                   frozen: bool = False, oneshot: bool = False) -> Selection:
    """Resolve a Selection from CLI-like args (Namespace or dict) without mutating them."""
    get = args.get if isinstance(args, dict) else (lambda key: getattr(args, key, None))
    supplied = {key: get(key) for key in FIELDS if get(key) is not None}
    explicit = tuple(sorted(supplied))
    if frozen:  # old approvals cannot accept new flags; checked before any other validation
        if supplied:
            raise ContextPolicyError("context_overrides_frozen")
        return Selection(None, "frozen", ())
    mode = supplied.get("context_policy")
    if mode is not None and not (isinstance(mode, str) and mode in MODES):
        raise ContextPolicyError("invalid_context_policy")
    if mode == "legacy":
        if len(supplied) > 1:
            raise ContextPolicyError("context_options_require_model_v1")
        return Selection(None, "explicit", explicit)
    window = supplied.get("context_window_tokens", model_window)
    if not _posint(window):
        raise ContextPolicyError("invalid_window_tokens")
    if route_output_limit is not None and not _posint(route_output_limit):
        raise ContextPolicyError("invalid_route_output_limit")
    reply = supplied.get("context_reply_tokens")
    if reply is None:
        reply = max(min(32768, max(1, window // 16)), route_output_limit or 0)
    safety = supplied.get("context_safety_tokens", max(1, -(-window // 20)))  # ceil(5%) exactly
    auto = supplied.get("auto_compact", False)
    if oneshot and auto is True:  # one-shot promises a single model request
        raise ContextPolicyError("compaction_unavailable_in_oneshot")
    policy = Policy(window, reply, safety, auto, supplied.get("compact_fraction", DEFAULT_FRACTION),
                    route_output_limit)
    return Selection(policy, "explicit" if mode else "default", explicit)

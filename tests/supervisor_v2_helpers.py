"""Synthetic version-2 envelope helpers; v1 defaults in supervisor_helpers stay unchanged."""
import hashlib

from supervisor_helpers import canonical, envelope_dict

LIMITS_V2 = {
    "max_replans": None,
    "max_total_attempts": None,
    "max_concurrent_workers": 8,
    "max_planner_calls": None,
    "max_final_report_calls": 1,
    "stop_grace_seconds": 60,
}


def envelope_dict_v2(root, limits=None, **overrides):
    """V2 envelope: null lifetime quotas, capacity 8, null deadline/max_usage unless given."""
    merged = dict(LIMITS_V2, **(limits or {}))
    base = {"schema_version": 2, "limits": merged, "deadline": None, "max_usage": None}
    base.update(overrides)
    return envelope_dict(root, **base)


def envelope_bytes_v2(root, limits=None, **overrides):
    """Return (canonical raw bytes, approval SHA-256) for a synthetic v2 envelope."""
    raw = canonical(envelope_dict_v2(root, limits, **overrides))
    return raw, hashlib.sha256(raw).hexdigest()

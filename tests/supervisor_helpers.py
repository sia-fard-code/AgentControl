"""Synthetic helpers for supervisor core tests. No live paths or content."""
import hashlib
import importlib
import json
import os
import sys
from pathlib import Path

ENGINE = Path(__file__).resolve().parents[1] / "engine"

LIMITS = {
    "max_replans": 2,
    "max_total_attempts": 6,
    "max_concurrent_workers": 2,
    "max_planner_calls": 3,
    "max_final_report_calls": 1,
    "stop_grace_seconds": 60,
}


def load(name):
    """Import an engine supervisor module (engine/ is not an installed package)."""
    if str(ENGINE) not in sys.path:
        sys.path.insert(0, str(ENGINE))
    return importlib.import_module(name)


def digest(label):
    return hashlib.sha256(label.encode()).hexdigest()


def envelope_dict(root, **overrides):
    data = {
        "schema_version": 1,
        "orchestration_id": "orch-synthetic-1",
        "project_root": os.path.realpath(str(root)),
        "goal_digest": digest("goal"),
        "acceptance_digest": digest("acceptance"),
        "policy_digest": digest("policy"),
        "validator_digest": digest("validator"),
        "baseline_commit": "a" * 40,
        "models": ["synthetic-model"],
        "capabilities": {"write": ["src/"], "commands": ["pytest"], "mcp": []},
        "cancel_authority": True,
        "limits": dict(LIMITS),
        "deadline": 2_000_000_000.0,
        "worker_turn_limit": 40,
        "verification_timeout_seconds": 300,
        "criteria": ["C1", "C2", "CI"],
        "check_ids": ["chk-unit"],
        "hypothesis_ids": ["H1"],
        "max_usage": 100_000,
    }
    data.update(overrides)
    return data


def canonical(data):
    return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()


def envelope_bytes(root, **overrides):
    """Return (raw bytes, operator approval digest) for a synthetic envelope."""
    raw = canonical(envelope_dict(root, **overrides))
    return raw, hashlib.sha256(raw).hexdigest()

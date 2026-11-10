"""Operator feature switches.

Cost estimation is experimental and disabled in this build. When it is off,
AgentControl still records and shows token counters, but it does not compute,
show or accept dollar estimates or pricing changes. Set AGENTCONTROL_ENABLE_COST
to "1", "true", "yes" or "on" to turn the experimental cost views on.
"""
from __future__ import annotations

import os
from collections.abc import Mapping

COST_ENV = "AGENTCONTROL_ENABLE_COST"
COST_DISABLED_MESSAGE = (
    "cost estimation is disabled in this build (experimental, not activated); "
    f"set {COST_ENV}=1 to enable it"
)

_TRUE = frozenset({"1", "true", "yes", "on"})


def cost_enabled(env: Mapping[str, str] | None = None) -> bool:
    """True only when the operator has explicitly enabled cost estimation."""
    source = os.environ if env is None else env
    return str(source.get(COST_ENV, "")).strip().lower() in _TRUE

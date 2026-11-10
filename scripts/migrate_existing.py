"""Thin CLI wrapper for AgentControl historical migration.

This forwards to ``agent_control.cli`` so the real argument parsing,
path injection, and confirmation logic live in one tested place.
"""

from __future__ import annotations

import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from agent_control.cli import main  # noqa: E402


def run(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    return main(["migrate", *args])


if __name__ == "__main__":
    raise SystemExit(run())

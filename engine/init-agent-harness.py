#!/usr/bin/env python3
"""Initialise (or MCP-import into) a repo's delegated-agent harness boundary.

    python init-agent-harness.py [-C ROOT] [--force] [--dry-run]
        [--mcp-from-kilo [PATH]] [--mcp-from-claude [PATH]] [--mcp-server ID ...]

All logic lives in harness_setup.py beside this file (loaded by absolute path).
MCP imports are disabled, tool-less and env-less; env VALUES are never copied
(only omitted variable names are listed) and no launch authorization is added.
"""
import importlib.util
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))


def _load_setup():
    path = os.path.join(_HERE, "harness_setup.py")
    if not os.path.isfile(path):
        sys.exit(f"error: harness_setup.py is not installed beside {_HERE}")
    spec = importlib.util.spec_from_file_location("_agentcontrol_harness_setup", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


if __name__ == "__main__":
    raise SystemExit(_load_setup().main(sys.argv[1:]))

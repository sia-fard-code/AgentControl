"""Parent-only v3 adapter tests. All execution data belongs in tmp_path."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "engine"))


def test_v3_parent_adapter_and_collection_port_exist():
    from supervisor_harness import HarnessAdapter
    from supervisor_harness_v3 import CollectedSnapshot3, ResilientHarnessAdapter

    assert issubclass(ResilientHarnessAdapter, HarnessAdapter)
    assert CollectedSnapshot3.__dataclass_params__.frozen

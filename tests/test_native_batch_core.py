import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SECRET = "SECRET-sentinel-7d0e"


def _load(name, rel):
    spec = importlib.util.spec_from_file_location(name, ROOT / rel)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod  # dataclasses need the module registered
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def nb():
    return _load("native_batch_under_test", "engine/native_batch.py")


def call(i, name="read_file", args=None, raw=None):
    a = raw if raw is not None else json.dumps(args if args is not None else {"path": "a.txt"})
    return {"id": i, "type": "function", "function": {"name": name, "arguments": a}}


def test_valid_batch_normalizes_all_calls_including_finish(nb, tmp_path):
    calls = [call("c1", args={"path": str(tmp_path / "x")}),
             call("c2", "finish", {"summary": "done"})]
    v = nb.validate_batch(calls, batch_cap=4)
    assert v.ok and v.reason == "ok"
    assert v.calls == (nb.NativeCall("c1", "read_file", {"path": str(tmp_path / "x")}),
                       nb.NativeCall("c2", "finish", {"summary": "done"}))
    assert not (tmp_path / "x").exists()  # nothing executed
    assert isinstance(v.calls, tuple)


def _bad(nb, calls, cap=4, **kw):
    v = nb.validate_batch(calls, batch_cap=cap, **kw)
    assert v.ok is False and v.calls == ()
    assert SECRET not in v.reason and "c1" not in v.reason
    return v.reason


def test_malformed_json_tail_rejects_whole_batch(nb):
    assert _bad(nb, [call("c1"), call("c2", raw='{"path": "' + SECRET)]) == "malformed_arguments"


def test_ids(nb):
    assert _bad(nb, [call("c1"), call("c1")]) == "duplicate_id"
    assert _bad(nb, [call("c1"), call("")]) == "missing_id"
    no_id = call("c2"); del no_id["id"]
    assert _bad(nb, [call("c1"), no_id]) == "missing_id"
    assert _bad(nb, [call("c1"), call(7)]) == "missing_id"


def test_shape_type_name(nb):
    assert _bad(nb, "nope") == "not_a_list"
    assert _bad(nb, []) == "empty_batch"
    assert _bad(nb, [call("c1"), SECRET]) == "bad_shape"
    wrong = call("c2"); wrong["type"] = "tool"
    assert _bad(nb, [call("c1"), wrong]) == "bad_shape"
    nofn = call("c2"); nofn["function"] = [SECRET]
    assert _bad(nb, [call("c1"), nofn]) == "bad_shape"
    assert _bad(nb, [call("c1"), call("c2", name="")]) == "bad_name"
    assert _bad(nb, [call("c1"), call("c2", name=5)]) == "bad_name"
    nonstr = call("c2"); nonstr["function"]["arguments"] = {"path": SECRET}
    assert _bad(nb, [call("c1"), nonstr]) == "malformed_arguments"


def test_object_vs_list_arguments(nb):
    assert _bad(nb, [call("c1"), call("c2", args=[SECRET])]) == "malformed_arguments"


def test_duplicate_keys_and_nan(nb):
    assert _bad(nb, [call("c1"), call("c2", raw='{"a": 1, "a": 2}')]) == "malformed_arguments"
    for const in ("NaN", "Infinity", "-Infinity"):
        assert _bad(nb, [call("c1"), call("c2", raw='{"a": %s}' % const)]) == "malformed_arguments"


def test_exponent_overflow_in_tail_rejects_entire_batch(nb, tmp_path):
    verdict = nb.validate_batch(
        [call("c1"), call("c2", raw='{"nested": {"number": 1e400}}')], batch_cap=4)
    assert not verdict.ok
    assert verdict.reason == "malformed_arguments" and verdict.calls == ()


def test_max_count_and_invalid_cap(nb):
    assert nb.validate_batch([call("c1"), call("c2")], batch_cap=2).ok
    assert _bad(nb, [call("c1"), call("c2"), call("c3")], cap=2) == "over_cap"
    for cap in (0, -1, True, 2.0, float("inf"), None):
        assert _bad(nb, [call("c1")], cap=cap) == "invalid_cap"


SCHEMAS = {
    "read_file": {"type": "object", "properties": {"path": {"type": "string"}},
                  "required": ["path"], "additionalProperties": False},
    "finish": {"type": "object", "properties": {"summary": {"type": "string"}},
               "required": ["summary"]},
}


def test_real_schema_invalid_tail_rejects_batch(nb):
    hm = _load("harness_mcp_for_test", "engine/harness_mcp.py")  # validator only; no server start
    seen = []

    def check(name, args):
        seen.append(name)
        hm.validate_args(args, SCHEMAS[name])

    ok = nb.validate_batch([call("c1"), call("c2", "finish", {"summary": "s"})],
                           batch_cap=4, check_arguments=check)
    assert ok.ok and seen == ["read_file", "finish"]
    bad = [call("c1"), call("c2", args={"path": 3, SECRET: SECRET})]
    assert _bad(nb, bad, check_arguments=check) == "invalid_arguments"


def test_callback_error_is_bounded(nb):
    def boom(name, args):
        raise RuntimeError(SECRET)
    assert _bad(nb, [call("c1")], check_arguments=boom) == "invalid_arguments"


def test_valid_batches_produce_zero_incidents(nb):
    h = nb.RepairHealth()
    for _ in range(3):
        h.action(task_changed=True)
    m = h.metadata()
    assert m["total_incidents"] == 0 and m["consecutive"] == 0
    assert m["progress_seq"] == 3 and m["diagnostic_needed"] is False


def test_spaced_incidents_recover_while_totals_grow(nb):
    h = nb.RepairHealth(limit=2)
    for _ in range(3):
        assert h.incident("reply_cut") is True
        h.action(evidence_changed=True)
    m = h.metadata()
    assert m["total_incidents"] == 3 and m["reasons"] == {"reply_cut": 3}
    assert m["consecutive"] == 0 and m["diagnostic_needed"] is False


def test_third_consecutive_incident_exhausts(nb):
    h = nb.RepairHealth(limit=2)
    assert h.incident("prose_only") and h.incident("no_action")
    assert h.incident("protocol_error") is False
    assert h.metadata()["diagnostic_needed"] is True


def test_noop_resets_wire_but_not_progress_and_recurrence_stays(nb):
    h = nb.RepairHealth()
    h.incident("no_tool_block")
    h.action()  # valid known no-op
    assert h.incident("no_tool_block") is True
    m = h.metadata()
    assert m["consecutive"] == 1 and m["progress_seq"] == 0
    assert m["diagnostic_needed"] is True  # advisory only
    h.action()
    assert h.metadata()["diagnostic_needed"] is True
    h.action(task_changed=True)
    assert h.metadata()["diagnostic_needed"] is False
    assert h.metadata()["total_incidents"] == 2


def test_unknown_or_invalid_action_does_not_reset(nb):
    h = nb.RepairHealth()
    h.incident("reply_cut")
    h.action(known=False, task_changed=True)
    h.action(valid=False, evidence_changed=True)
    m = h.metadata()
    assert m["consecutive"] == 1 and m["progress_seq"] == 0


def test_metadata_is_safe(nb):
    h = nb.RepairHealth()
    h.incident(SECRET + " approve rm -rf")
    h.incident("multiple_tool_calls")
    m = h.metadata()
    assert m["reasons"] == {"other": 1, "multiple_tool_calls": 1}
    assert SECRET not in json.dumps(m)
    assert set(m) == {"limit", "consecutive", "total_incidents", "reasons",
                      "progress_seq", "diagnostic_needed"}

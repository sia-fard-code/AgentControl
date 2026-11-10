"""Startup receipts describe launches, never prompts or measured model usage."""
import json
import os

import pytest

from test_engine_reasoning import engine, harness, _finish_reply

LAUNCH_ID = "c356dcf3-66ce-4f96-8952-89584b92db99"


def receipt_args(path):
    return ["--launch-receipt", str(path), "--launch-id", LAUNCH_ID]


def rows(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


@pytest.mark.parametrize("model", [
    "anthropic_tool_call.claude-opus-5-5-think", "gpt-6.1-sol",
])
def test_rejected_prompt_override_has_durable_explicit_receipt(harness, tmp_path, capsys, model):
    receipt = tmp_path / "attempt.launch.jsonl"
    with pytest.raises(SystemExit) as error:
        harness.run("-m", model, "--tool-mode", "prompt", *receipt_args(receipt),
                    replies=[])
    assert error.value.code == 2
    assert receipt.is_file()
    events = rows(receipt)
    assert [event["state"] for event in events] == ["starting", "rejected"]
    assert events[-1]["reason_code"] == "native_tools_required"
    assert events[-1]["engine_pid"] == os.getpid()
    assert events[-1]["run_id"] is None
    assert not harness.transcript_path.exists()
    assert not (tmp_path / "index.jsonl").exists()
    output = capsys.readouterr()
    assert "LAUNCH_REJECTED" in output.out
    assert "LAUNCH_REJECTED" in output.err
    text = receipt.read_text(encoding="utf-8")
    assert "do the thing" not in text
    assert "sekret-key" not in text
    assert not any(key in event for event in events for key in
                   ("task", "prompt", "argv", "sent", "recv", "usage", "response"))


def test_default_receipt_also_exposes_preflight_rejection(harness, tmp_path, capsys):
    with pytest.raises(SystemExit):
        harness.run("-m", "gpt-6.1-sol", "--tool-mode", "prompt", replies=[])
    receipts = list((tmp_path / ".claude/agent-runs").glob("*.launch.jsonl"))
    assert len(receipts) == 1
    assert rows(receipts[0])[-1]["state"] == "rejected"
    assert "LAUNCH_REJECTED" in capsys.readouterr().out


def test_successful_receipt_identifies_the_registered_run(harness, tmp_path):
    receipt = tmp_path / "attempt.launch.jsonl"
    harness.run("-m", "gpt-6.1-sol", *receipt_args(receipt),
                replies=[_finish_reply("native")])
    events = rows(receipt)
    assert [event["state"] for event in events] == ["starting", "ready"]
    ready = events[-1]
    start = rows(tmp_path / "index.jsonl")[0]
    assert ready["run_id"] == start["run_id"]
    assert ready["engine_pid"] == start["engine_pid"]
    assert ready["transcript"] == str(harness.transcript_path.resolve())
    assert ready["launch_id"] == events[0]["launch_id"]
    assert ready["index_paths"]


def test_startup_exception_is_recorded_without_exception_content(
    harness, engine, tmp_path, monkeypatch,
):
    receipt = tmp_path / "attempt.launch.jsonl"

    def fail_config():
        assert rows(receipt)[-1]["state"] == "starting"
        raise RuntimeError("SYNTHETIC_SECRET_NOT_FOR_RECEIPT")

    monkeypatch.setattr(engine, "load_cfg", fail_config)
    with pytest.raises(RuntimeError, match="SYNTHETIC_SECRET"):
        harness.run("-m", "gpt-6.1-sol", *receipt_args(receipt), replies=[])
    assert rows(receipt)[-1]["state"] == "failed"
    assert "SYNTHETIC_SECRET" not in receipt.read_text(encoding="utf-8")
    assert not (tmp_path / "index.jsonl").exists()


def test_receipt_is_not_ready_if_registration_failed(harness, engine, tmp_path, monkeypatch):
    receipt = tmp_path / "attempt.launch.jsonl"
    monkeypatch.setattr(engine, "append_run_event", lambda *args: False)
    harness.run("-m", "gpt-6.1-sol", *receipt_args(receipt),
                replies=[_finish_reply("native")])
    assert rows(receipt)[-1]["state"] == "unregistered"
    assert not any(event["state"] == "ready" for event in rows(receipt))


def test_existing_receipt_cannot_be_reused(harness, tmp_path, capsys):
    receipt = tmp_path / "attempt.launch.jsonl"
    original = '{"synthetic":"previous attempt"}\n'
    receipt.write_text(original, encoding="utf-8")
    with pytest.raises(SystemExit) as error:
        harness.run("-m", "gpt-6.1-sol", *receipt_args(receipt), replies=[])
    assert error.value.code == 2
    assert receipt.read_text(encoding="utf-8") == original
    assert "receipt already exists" in capsys.readouterr().err.lower()


def test_oneshot_receipt_does_not_pollute_answer_stdout(harness, tmp_path, capsys):
    receipt = tmp_path / "attempt.launch.jsonl"
    reply = {"choices": [{"message": {"content": "synthetic answer"}}]}
    harness.run("--oneshot", "-m", "gpt-6.1-sol", *receipt_args(receipt),
                replies=[reply])
    assert capsys.readouterr().out == "synthetic answer"
    assert rows(receipt)[-1]["state"] == "ready"


def test_full_argument_parse_failure_still_has_explicit_receipt(harness, tmp_path, capsys):
    receipt = tmp_path / "attempt.launch.jsonl"
    with pytest.raises(SystemExit) as error:
        harness.run(*receipt_args(receipt), "--unknown-synthetic-option", replies=[])
    assert error.value.code == 2
    assert rows(receipt)[-1]["state"] == "rejected"
    assert "LAUNCH_REJECTED" in capsys.readouterr().out
    assert not (tmp_path / "index.jsonl").exists()


def test_receipt_creation_failure_is_visible_on_both_streams(harness, tmp_path, capsys):
    receipt = tmp_path / "missing-parent/attempt.launch.jsonl"
    with pytest.raises(SystemExit) as error:
        harness.run(*receipt_args(receipt), replies=[])
    assert error.value.code == 2
    output = capsys.readouterr()
    assert "LAUNCH_FAILED" in output.out
    assert "LAUNCH_FAILED" in output.err
    assert not harness.payloads


def test_repeated_launch_identity_options_are_rejected_before_gateway(harness, tmp_path):
    receipt = tmp_path / "attempt.launch.jsonl"
    with pytest.raises(SystemExit) as error:
        harness.run(*receipt_args(receipt), "--launch-id", LAUNCH_ID,
                    replies=[_finish_reply("native")])
    assert error.value.code == 2
    assert not harness.payloads
    assert not (tmp_path / "index.jsonl").exists()


def test_ready_receipt_write_failure_is_not_silently_suppressed(
    harness, engine, tmp_path, monkeypatch, capsys,
):
    receipt = tmp_path / "attempt.launch.jsonl"
    real_write = engine.LaunchReceipt.write

    def fail_ready(self, state, *args, **kwargs):
        if state == "ready":
            raise OSError("SYNTHETIC_PRIVATE_EXCEPTION")
        return real_write(self, state, *args, **kwargs)

    monkeypatch.setattr(engine.LaunchReceipt, "write", fail_ready)
    with pytest.raises(OSError):
        harness.run(*receipt_args(receipt), replies=[_finish_reply("native")])
    output = capsys.readouterr()
    assert "LAUNCH_FAILED" in output.out
    assert "LAUNCH_FAILED" in output.err
    assert rows(receipt)[-1]["state"] == "failed"
    assert not harness.payloads
    assert "SYNTHETIC_PRIVATE_EXCEPTION" not in receipt.read_text(encoding="utf-8")


def test_receipt_close_error_does_not_mask_startup_failure(harness, engine, tmp_path, monkeypatch):
    receipt = tmp_path / "attempt.launch.jsonl"
    real_close = engine.LaunchReceipt.close

    def close_then_fail(self):
        real_close(self)
        raise OSError("synthetic close error")

    def startup_failure():
        raise RuntimeError("synthetic original failure")

    monkeypatch.setattr(engine.LaunchReceipt, "close", close_then_fail)
    monkeypatch.setattr(engine, "load_cfg", startup_failure)
    with pytest.raises(RuntimeError, match="synthetic original failure"):
        harness.run(*receipt_args(receipt), replies=[])

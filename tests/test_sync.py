from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from agent_control.ledger import highest_valid_revisions, load_ledger
from agent_control.models import ProcessSnapshot
from agent_control.paths import AppPaths
from agent_control.projects import load_projects
from agent_control.storage import StateLocks
from agent_control.sync import Synchronizer
from tests.test_run_index import end_json, start_json


class FakeProbe:
    def inspect(self, pid: int) -> ProcessSnapshot | None:
        return None


def append_index(path: Path, records: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        for record in records:
            handle.write(json.dumps(record, separators=(",", ":")) + "\n")


@pytest.fixture
def synchronizer(temp_paths: AppPaths) -> Synchronizer:
    return Synchronizer(
        temp_paths,
        StateLocks(),
        lambda: datetime(2026, 9, 17, 13, 0, tzinfo=timezone.utc),
        FakeProbe(),
    )


def test_open_run_observation_carries_verdict(
    temp_paths: AppPaths, synchronizer: Synchronizer
) -> None:
    append_index(
        temp_paths.machine_index,
        [start_json(run_id="r", engine_pid=4321, max_iters=25)],
    )
    synchronizer.run()
    observation = synchronizer.open_runs["r"]
    assert observation.run_id == "r"
    assert observation.liveness.code == "process_gone"
    assert observation.engine_pid == 4321
    assert observation.max_iters == 25
    assert observation.transcript == start_json()["transcript"]
    assert observation.messages == start_json()["messages"]
    assert observation.usage_journal == start_json()["usage_journal"]


def test_open_run_observation_has_start(
    temp_paths: AppPaths, synchronizer: Synchronizer
) -> None:
    append_index(
        temp_paths.machine_index,
        [start_json(run_id="r", engine_pid=4321, max_iters=25)],
    )
    synchronizer.run()
    observation = synchronizer.open_runs["r"]
    assert observation.start is not None
    assert observation.start.engine_pid == 4321


def test_open_run_drops_out_after_end(
    temp_paths: AppPaths, synchronizer: Synchronizer
) -> None:
    append_index(temp_paths.machine_index, [start_json(run_id="r")])
    synchronizer.run()
    assert "r" in synchronizer.open_runs
    append_index(temp_paths.machine_index, [end_json(run_id="r")])
    synchronizer.run()
    assert "r" not in synchronizer.open_runs


def test_open_run_max_iters_survives_state_round_trip(
    temp_paths: AppPaths, synchronizer: Synchronizer
) -> None:
    append_index(
        temp_paths.machine_index, [start_json(run_id="r", max_iters=17)]
    )
    synchronizer.run()
    synchronizer.run()
    assert synchronizer.open_runs["r"].max_iters == 17


def test_terminal_ledger_run_still_open_in_index(
    temp_paths: AppPaths, synchronizer: Synchronizer
) -> None:
    # engine_pid + FakeProbe reporting the process gone makes the ledger
    # record terminal, but with no end record the index run stays open.
    append_index(
        temp_paths.machine_index, [start_json(run_id="r", engine_pid=4321)]
    )
    synchronizer.run()
    current = highest_valid_revisions(load_ledger(temp_paths.ledger)[0])["r"]
    assert current.status == "interrupted"
    assert "r" in synchronizer.open_runs


def test_start_is_persisted_until_later_end(
    temp_paths: AppPaths, synchronizer: Synchronizer
) -> None:
    append_index(temp_paths.machine_index, [start_json(run_id="r")])
    assert synchronizer.run().appended_revisions == 1
    append_index(temp_paths.machine_index, [end_json(run_id="r")])
    assert synchronizer.run().appended_revisions == 1
    current = highest_valid_revisions(load_ledger(temp_paths.ledger)[0])["r"]
    assert current.status == "complete"
    assert current.revision == 2


def test_unknown_root_becomes_pending_and_run_is_ledgered(
    temp_paths: AppPaths, synchronizer: Synchronizer
) -> None:
    append_index(
        temp_paths.machine_index,
        [start_json(run_id="r", root=r"Q:\agentcontrol-test\Temp"), end_json(run_id="r")],
    )
    report = synchronizer.run()
    assert report.discovered_projects == 1
    projects, _ = load_projects(temp_paths.projects)
    assert all(project.state == "discovered" for project in projects.values())
    ledger = highest_valid_revisions(load_ledger(temp_paths.ledger)[0])
    assert "r" in ledger


def test_repeated_sync_is_idempotent(
    temp_paths: AppPaths, synchronizer: Synchronizer
) -> None:
    append_index(
        temp_paths.machine_index,
        [start_json(run_id="r"), end_json(run_id="r")],
    )
    first = synchronizer.run()
    second = synchronizer.run()
    assert first.appended_revisions == 1
    assert second.appended_revisions == 0
    assert len(load_ledger(temp_paths.ledger)[0]) == 1


def test_index_end_usage_fallback_when_artifacts_absent(
    temp_paths: AppPaths, synchronizer: Synchronizer
) -> None:
    append_index(
        temp_paths.machine_index,
        [start_json(run_id="r"), end_json(run_id="r")],
    )
    synchronizer.run()
    current = highest_valid_revisions(load_ledger(temp_paths.ledger)[0])["r"]
    assert current.tokens_sent == 1234
    assert current.tokens_returned == 567
    assert current.usage_source == "missing"
    assert current.cache_read is None


def test_checkpoint_persisted_so_second_run_reads_nothing_new(
    temp_paths: AppPaths, synchronizer: Synchronizer
) -> None:
    append_index(temp_paths.machine_index, [start_json(run_id="r"), end_json(run_id="r")])
    synchronizer.run()
    report = synchronizer.run()
    assert report.scanned_records == 0
    assert temp_paths.sync_state.exists()


def test_usage_evidence_from_real_artifacts_is_used(
    temp_paths: AppPaths, synchronizer: Synchronizer, tmp_path: Path
) -> None:
    runs_dir = tmp_path / "artifacts"
    runs_dir.mkdir()
    journal = runs_dir / "a.usage.jsonl"
    journal.write_text(
        json.dumps(
            {"iter": 1, "phase": "loop", "raw": {"input_tokens": 40, "output_tokens": 9}}
        )
        + "\n",
        encoding="utf-8",
    )
    transcript = runs_dir / "a.md"
    transcript.write_text("x", encoding="utf-8")
    messages = runs_dir / "a.messages.jsonl"
    messages.write_text("{}\n", encoding="utf-8")
    append_index(
        temp_paths.machine_index,
        [
            start_json(
                run_id="r",
                transcript=str(transcript),
                messages=str(messages),
                usage_journal=str(journal),
            ),
            end_json(run_id="r"),
        ],
    )
    synchronizer.run()
    current = highest_valid_revisions(load_ledger(temp_paths.ledger)[0])["r"]
    assert current.usage_source == "usage_journal"
    assert current.tokens_sent == 40
    assert current.tokens_returned == 9


def _clocked(temp_paths: AppPaths, times: list[float], **kwargs) -> Synchronizer:
    base = datetime(2026, 9, 17, 13, 0, tzinfo=timezone.utc)

    def clock() -> datetime:
        return base.fromtimestamp(times[0], tz=timezone.utc)

    return Synchronizer(
        temp_paths, StateLocks(), clock, FakeProbe(), **kwargs
    )


def test_throttle_skips_sync_within_interval(temp_paths: AppPaths) -> None:
    times = [1000.0]
    sync = _clocked(temp_paths, times, min_interval_seconds=5)
    append_index(temp_paths.machine_index, [start_json(run_id="r")])
    first = sync.run()
    assert "r" in sync.open_runs

    times[0] = 1002.0
    append_index(temp_paths.machine_index, [start_json(run_id="s")])
    second = sync.run()
    assert second is first
    assert "s" not in sync.open_runs

    times[0] = 1010.0
    third = sync.run()
    assert third is not first
    assert "s" in sync.open_runs


def test_no_throttle_by_default(temp_paths: AppPaths) -> None:
    times = [1000.0]
    sync = _clocked(temp_paths, times)
    append_index(temp_paths.machine_index, [start_json(run_id="r")])
    sync.run()
    times[0] = 1000.5
    append_index(temp_paths.machine_index, [start_json(run_id="s")])
    sync.run()
    assert "s" in sync.open_runs


def test_sync_loads_ledger_once_over_many_open_runs(
    temp_paths: AppPaths, synchronizer: Synchronizer, monkeypatch
) -> None:
    import agent_control.sync as sync_module

    calls = [0]
    original = sync_module.load_ledger

    def counting(path):
        calls[0] += 1
        return original(path)

    monkeypatch.setattr(sync_module, "load_ledger", counting)
    append_index(
        temp_paths.machine_index,
        [start_json(run_id=f"r{i}") for i in range(5)],
    )
    synchronizer.run()
    assert calls[0] == 1


def test_terminal_ledger_run_skips_usage_and_sync_candidate(
    temp_paths: AppPaths, synchronizer: Synchronizer, monkeypatch
) -> None:
    import agent_control.sync as sync_module

    # First sync makes the run terminal (interrupted) in the ledger.
    append_index(
        temp_paths.machine_index, [start_json(run_id="r", engine_pid=4321)]
    )
    synchronizer.run()
    assert highest_valid_revisions(load_ledger(temp_paths.ledger)[0])[
        "r"
    ].status == "interrupted"

    usage_calls = [0]
    candidate_calls = [0]
    original_usage = synchronizer._resolve_usage

    def spy_usage(*args, **kwargs):
        usage_calls[0] += 1
        return original_usage(*args, **kwargs)

    original_candidate = sync_module.sync_candidate

    def spy_candidate(*args, **kwargs):
        candidate_calls[0] += 1
        return original_candidate(*args, **kwargs)

    monkeypatch.setattr(synchronizer, "_resolve_usage", spy_usage)
    monkeypatch.setattr(sync_module, "sync_candidate", spy_candidate)
    report = synchronizer.run()
    assert usage_calls[0] == 0
    assert candidate_calls[0] == 0
    assert report.unchanged_runs == 1
    assert "r" in synchronizer.open_runs


def test_load_state_memoizes_ledger_parse(
    temp_paths: AppPaths, monkeypatch
) -> None:
    from agent_control.dashboard import DashboardApp

    class NullSync:
        open_runs: dict = {}

        def run(self):
            return None

    app = DashboardApp(
        temp_paths,
        StateLocks(),
        NullSync(),
        lambda: datetime(2026, 9, 17, 13, 0, tzinfo=timezone.utc),
    )
    append_index(
        temp_paths.machine_index, [start_json(run_id="r"), end_json(run_id="r")]
    )
    Synchronizer(
        temp_paths,
        StateLocks(),
        lambda: datetime(2026, 9, 17, 13, 0, tzinfo=timezone.utc),
        FakeProbe(),
    ).run()

    import agent_control.dashboard as dashboard_module

    calls = [0]
    original = dashboard_module.load_ledger

    def counting(path):
        calls[0] += 1
        return original(path)

    monkeypatch.setattr(dashboard_module, "load_ledger", counting)
    first = app._load_state()[0]
    second = app._load_state()[0]
    assert calls[0] == 1
    assert first is second


def test_archive_only_when_transcript_changes(
    temp_paths: AppPaths, synchronizer: Synchronizer, tmp_path: Path, monkeypatch
) -> None:
    transcript = tmp_path / "a.md"
    transcript.write_text("### [1] thinking\n", encoding="utf-8")
    calls = [0]
    import agent_control.activity as activity

    original = activity.write_activity_archive

    def counting(path, snapshot):
        calls[0] += 1
        return original(path, snapshot)

    monkeypatch.setattr(activity, "write_activity_archive", counting)
    append_index(
        temp_paths.machine_index,
        [start_json(run_id="r", transcript=str(transcript))],
    )
    synchronizer.run()
    synchronizer.run()
    assert calls[0] == 1

    with transcript.open("a", encoding="utf-8") as handle:
        handle.write("### [2] thinking\n")
    synchronizer.run()
    assert calls[0] == 2

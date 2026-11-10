"""Lineage rejection is allocation-free; published compaction commits run once."""
import copy
import json

import pytest

from test_context_history_identity import LONG, NOTES, auto, batch, cc, make


LINEAGE = (0, 1, None, 4, 5)
MEMORY = {"role": "assistant", "content": "synthetic descriptive memory"}


def frozen(rt):
    return copy.deepcopy((rt._next_id, rt._rows, rt._groups, rt._epoch,
                          rt._compactions, rt._observed, rt._store.metadata()))


def candidate(rt, lineage=LINEAGE):
    return [copy.deepcopy(MEMORY if i is None else rt._rows[i]["msg"])
            for i in lineage]


def reject(rt, messages, lineage):
    bindings, before = rt.bindings(), frozen(rt)
    assert rt.commit_view(bindings, messages, "synthetic-artifact", lineage=lineage) is False
    assert frozen(rt) == before


@pytest.fixture
def direct(tmp_path):
    rt, _ = make(tmp_path)
    batch(rt, "a", "synthetic older result")
    batch(rt, "b", "synthetic newest result")
    return rt


def test_valid_direct_candidate_reuses_retained_ids_and_allocates_one_memory(direct):
    rt = direct
    assert [r["msg"]["role"] for r in rt._rows] == [
        "system", "user", "assistant", "tool", "assistant", "tool"]
    ids = [r["id"] for r in rt._rows]
    next_id, epoch = rt._next_id, rt._epoch
    messages = candidate(rt)
    assert rt.commit_view(rt.bindings(), messages, lineage=LINEAGE) is True
    assert rt.payload_messages() == messages
    assert [r["id"] for r in rt._rows] == [ids[0], ids[1], next_id + 1, ids[4], ids[5]]
    assert [(g["kind"], g["ids"]) for g in rt._groups] == [
        ("memory", [next_id + 1]), ("native", ids[4:6])]
    assert (rt._next_id, rt._epoch, rt._compactions) == (next_id + 1, epoch + 1, 1)


@pytest.mark.parametrize("lineage", [
    pytest.param(list(LINEAGE), id="list-not-tuple"),
    pytest.param((0, 1, None, 4), id="wrong-length"),
    pytest.param((0, True, None, 4, 5), id="boolean"),
    pytest.param((0, 1, None, -1, 5), id="negative"),
    pytest.param((0, 1, None, 4, 6), id="out-of-range"),
    pytest.param((0, 1, None, 4, 5.0), id="float"),
    pytest.param((0, 1, None, 4, "5"), id="string"),
    pytest.param((0, 1, None, 4, 4), id="duplicate"),
    pytest.param((1, 0, None, 4, 5), id="reversed"),
    pytest.param((0, 1, 2, 4, 5), id="zero-replacements"),
    pytest.param((0, None, None, 4, 5), id="multiple-replacements"),
    pytest.param((0, None, 1, 4, 5), id="replacement-too-early"),
    pytest.param((0, 1, 4, None, 5), id="replacement-too-late"),
])
def test_malformed_lineage_rejects_without_mutation(direct, lineage):
    reject(direct, candidate(direct), lineage)


@pytest.mark.parametrize("index", [0, 1, 3, 4], ids=["system", "task", "assistant", "result"])
def test_changed_retained_content_rejects_without_mutation(direct, index):
    messages = candidate(direct)
    messages[index]["content"] = "synthetic altered retained content"
    reject(direct, messages, LINEAGE)


def published_attempt(tmp_path, rt, charges, events, commit, after_publish=lambda: None):
    adapter = auto(rt, charges)
    artifact = tmp_path / "synthetic-compaction.json"
    boundary_calls, runtime_calls = [], []
    original_callback = adapter.compactor._commit

    def callback(*args, **kwargs):
        boundary_calls.append((args, kwargs))
        return original_callback(*args, **kwargs)

    def runtime_commit(*args, **kwargs):
        runtime_calls.append((args, kwargs))
        return commit(*args, **kwargs)

    def publish(record):
        events.append("publish")
        assert charges == [True]
        assert record["notes"] == NOTES
        artifact.write_text(json.dumps(record), encoding="utf-8")
        after_publish()
        return str(artifact)

    adapter.compactor._persist = publish
    adapter.compactor._commit = callback
    rt.commit_view = runtime_commit
    snap = cc.snapshot(**rt.snapshot_data())
    outcome = adapter.compactor.attempt(snap, force=True)
    assert artifact.is_file()
    assert json.loads(artifact.read_text(encoding="utf-8"))["notes"] == NOTES
    assert charges == [True]
    assert adapter.compactor.calls == outcome.calls == 1
    assert adapter.compactor.failures == 1
    count = 0 if outcome.code == "stale_bindings" else 1
    assert len(boundary_calls) == len(runtime_calls) == count
    if count:
        assert boundary_calls[0][1] == runtime_calls[0][1] == {"lineage": LINEAGE}
        assert boundary_calls[0][1]["lineage"] is runtime_calls[0][1]["lineage"]
    return outcome


@pytest.mark.parametrize("failure", ["false", "type-error"])
def test_publication_precedes_once_only_rejected_commit(tmp_path, failure):
    rt, _ = make(tmp_path)
    batch(rt, "a", "synthetic older " + LONG)
    batch(rt, "b", "synthetic newest")
    before, charges, events = frozen(rt), [], []
    received = []

    def commit(*args, **kwargs):
        events.append("commit")
        received.append((args, kwargs))
        assert events == ["publish", "commit"]
        if failure == "type-error":
            raise TypeError("synthetic callback implementation fault")
        return False

    outcome = published_attempt(tmp_path, rt, charges, events, commit)
    assert outcome.code == "commit_rejected"
    assert events == ["publish", "commit"]
    assert len(received) == 1
    args, kwargs = received[0]
    assert len(args) == 3
    assert kwargs == {"lineage": LINEAGE}
    assert args[1][3:] == [r["msg"] for r in rt._rows[4:]]
    assert frozen(rt) == before


@pytest.mark.parametrize("mutation", ["external-binding", "append"])
def test_binding_change_after_publication_retains_charge_and_legitimate_state(tmp_path, mutation):
    rt, _ = make(tmp_path)
    revision = {"value": 0}
    rt._external = lambda: dict(revision)
    batch(rt, "a", "synthetic older " + LONG)
    batch(rt, "b", "synthetic newest")
    before, charges, events, commits, published_states = frozen(rt), [], [], [], []
    appended = {"role": "assistant", "content": "synthetic publication-time append"}

    def commit(*args, **kwargs):
        commits.append((args, kwargs))
        return False

    def mutate():
        if mutation == "external-binding":
            revision["value"] = 1
        else:
            rt.append(appended)
        published_states.append(frozen(rt))

    outcome = published_attempt(tmp_path, rt, charges, events, commit, after_publish=mutate)
    assert outcome.code == "stale_bindings"
    assert events == ["publish"]
    assert commits == []
    assert len(published_states) == 1
    assert frozen(rt) == published_states[0]
    if mutation == "external-binding":
        assert rt.bindings()["external"] == {"value": 1}
        assert frozen(rt) == before
    else:
        assert rt._rows == before[1] + [{"id": before[0] + 1, "msg": appended, "pinned": False}]
        assert rt._groups == before[2]
        assert (rt._next_id, rt._epoch, rt._compactions) == (
            before[0] + 1, before[3] + 1, before[4])


def test_reentrant_external_append_rejects_once_and_preserves_appended_state(tmp_path):
    rt, _ = make(tmp_path)
    batch(rt, "a", "synthetic older " + LONG)
    batch(rt, "b", "synthetic newest")
    before, charges, events = frozen(rt), [], []
    gate, appended_states, commits = {"armed": False, "reads": 0}, [], []
    appended = {"role": "assistant", "content": "synthetic legitimate concurrent append"}
    original_commit = rt.commit_view

    def external():
        if gate["armed"]:
            gate["reads"] += 1
            if gate["reads"] == 2:  # post-publication freshness read, then commit's own read
                gate["armed"] = False
                rt.append(appended)
                events.append("external-append")
                appended_states.append(frozen(rt))
        return {"revision": 0}

    def commit(*args, **kwargs):
        commits.append((args, kwargs))
        events.append("commit")
        return original_commit(*args, **kwargs)

    rt._external = external
    outcome = published_attempt(tmp_path, rt, charges, events, commit,
                                after_publish=lambda: gate.update(armed=True))
    assert outcome.code == "commit_rejected"
    assert events == ["publish", "commit", "external-append"]
    assert len(commits) == 1
    assert gate["reads"] == 2
    assert len(appended_states) == 1
    assert frozen(rt) == appended_states[0]
    assert rt._rows == before[1] + [{"id": before[0] + 1, "msg": appended, "pinned": False}]
    assert rt._groups == before[2]
    assert (rt._next_id, rt._epoch, rt._compactions) == (before[0] + 1, before[3] + 1, before[4])


def omit(rt, positions):
    first = min(positions)
    return tuple(value for i in range(len(rt._rows))
                 for value in ([None] if i == first else []) + ([] if i in positions else [i]))


@pytest.mark.parametrize("positions", [{2}, {3}], ids=["assistant-only", "result-only"])
def test_partial_group_omission_rejects_without_mutation(direct, positions):
    lineage = omit(direct, positions)
    reject(direct, candidate(direct, lineage), lineage)


@pytest.mark.parametrize("position", [0, 1], ids=["system", "task"])
def test_authority_row_omission_rejects_without_mutation(direct, position):
    assert direct._rows[position]["pinned"] is True
    lineage = omit(direct, {position, 2, 3})
    reject(direct, candidate(direct, lineage), lineage)


def test_authority_pinned_group_omission_rejects_without_mutation(direct):
    direct._rows[2]["pinned"] = True  # synthetic engine-owned pin on the otherwise eligible batch
    reject(direct, candidate(direct), LINEAGE)


@pytest.mark.parametrize("trailing_memory", [False, True])
def test_newest_real_group_cannot_be_removed_even_before_memory(direct, trailing_memory):
    rt = direct
    if trailing_memory:
        rt.append(MEMORY)
        lineage = (0, 1, None, 4, 5, 6)
        assert rt.commit_view(rt.bindings(), candidate(rt, lineage), lineage=lineage) is True
        assert rt._groups[-1]["kind"] == "memory"
    newest = [g for g in rt._groups if g["kind"] == "native"][-1]
    positions = {i for i, row in enumerate(rt._rows) if row["id"] in newest["ids"]}
    lineage = omit(rt, positions)
    reject(rt, candidate(rt, lineage), lineage)


@pytest.mark.parametrize("omit_older", [False, True])
def test_ungrouped_row_omission_rejects_without_mutation(direct, omit_older):
    direct.append({"role": "assistant", "content": "synthetic standalone text"})
    positions = {6, 2, 3} if omit_older else {6}
    lineage = omit(direct, positions)
    reject(direct, candidate(direct, lineage), lineage)


def test_incomplete_capture_group_omission_rejects_without_mutation(tmp_path):
    rt, state = make(tmp_path)
    state["fail"] = True
    batch(rt, "a", "synthetic incomplete " + LONG)
    state["fail"] = False
    batch(rt, "b", "synthetic newest result")
    assert rt._retained(rt._groups[0]) is True
    assert rt._retained(rt._groups[1]) is False
    reject(rt, candidate(rt), LINEAGE)


@pytest.mark.parametrize("change", ["assistant", "result", "both"])
def test_changed_retained_tool_ids_reject_without_mutation(direct, change):
    messages = candidate(direct)
    if change in ("assistant", "both"):
        messages[3]["tool_calls"][0]["id"] = "synthetic-changed-id"
    if change in ("result", "both"):
        messages[4]["tool_call_id"] = "synthetic-changed-id"
    reject(direct, messages, LINEAGE)

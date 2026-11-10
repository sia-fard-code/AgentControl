import dataclasses
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest


MODULE_PATH = Path(__file__).resolve().parents[1] / "engine" / "run_review.py"
SPEC = importlib.util.spec_from_file_location("_run_review_under_test", MODULE_PATH)
rr = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = rr
SPEC.loader.exec_module(rr)

TASK = "Approved synthetic task: change old to new.\n"
PATCH = "diff --git a/demo.txt b/demo.txt\n--- a/demo.txt\n+++ b/demo.txt\n@@ -1 +1 @@\n-old\n+new\n"


def _forbid(*args, **kwargs):
    raise AssertionError("Unexpected execution or fallback")


def _candidate(tmp_path, *, patch_text=PATCH, **overrides):
    root = tmp_path / "root"
    root.mkdir(exist_ok=True)
    transcript = tmp_path / "transcript.jsonl"
    if not transcript.exists():
        transcript.write_bytes(b'{"run":"synthetic","event":"started"}\n')
    values = {
        "run_id": "run-synthetic",
        "task_text": TASK,
        "transcript_path": str(transcript),
        "root": str(root),
        "write_mode": "stage",
        "candidate_seq": 1,
        "mutation_seq": 2,
        "verification_status": "not_run",
        "runner": SimpleNamespace(patch=lambda: patch_text),
    }
    values.update(overrides)
    return rr.capture_review_input(**values)


def _response(content=None, **message_fields):
    if content is None:
        content = json.dumps({"verdict": "pass", "finding_codes": []})
    return {"choices": [{"message": {"content": content, **message_fields}}]}


def _saved(tmp_path):
    candidate = rr.save_candidate_artifact(str(tmp_path / "artifacts"), _candidate(tmp_path))
    record = {
        "run_id": candidate.binding.run.run_id,
        "run_binding": candidate.binding.run.to_metadata(),
        "patch_binding": candidate.binding.to_metadata(),
        "verification_status": candidate.verification_status,
    }
    return candidate, record


def _artifact_record(candidate):
    binding = candidate.binding
    return {
        "run_id": binding.run.run_id,
        "candidate_seq": binding.candidate_seq,
        "mutation_seq": binding.mutation_seq,
        "path": binding.artifact_path,
        "sha256": binding.patch_sha256,
        "size": binding.patch_size,
    }


def test_stage_review_uses_this_runner_patch_not_agent_patch(tmp_path, monkeypatch):
    root = tmp_path / "root"
    root.mkdir()
    (root / "agent.patch").write_text("STALE OTHER RUN", encoding="utf-8")
    (tmp_path / "args.patch").write_text("UNASSOCIATED ARGUMENT", encoding="utf-8")
    validated = []
    patch_calls = []
    real_open = open

    def guarded_open(path, *args, **kwargs):
        assert not os.fspath(path).endswith(".patch"), "Unassociated patch was read"
        return real_open(path, *args, **kwargs)

    def this_patch():
        patch_calls.append(True)
        return PATCH

    runner = SimpleNamespace(
        patch=this_patch,
        pending={"demo.txt": "new"},
        base_tracker=SimpleNamespace(validate=validated.append),
        _resolve=lambda path: str(root / path),
    )
    monkeypatch.setattr(rr, "open", guarded_open, raising=False)
    monkeypatch.setattr(rr.subprocess, "run", _forbid)
    candidate = _candidate(tmp_path, write_mode="staged", runner=runner)
    assert isinstance(candidate, rr.ReviewInput)
    assert candidate.patch_text == PATCH
    assert candidate.binding.source == "pending"
    assert candidate.binding.run.write_mode == "stage"
    assert candidate.binding.run.root_identity == os.path.normcase(os.path.realpath(root))
    assert candidate.binding.patch_sha256 == hashlib.sha256(PATCH.encode()).hexdigest()
    assert candidate.binding.patch_size == len(PATCH.encode())
    assert validated == [str(root / "demo.txt")]
    assert patch_calls == [True]

    def changed_base(path):
        raise ValueError("Synthetic staged base changed")

    runner.base_tracker.validate = changed_base
    runner.patch = _forbid
    unavailable = _candidate(tmp_path, runner=runner)
    assert unavailable == rr.ReviewUnavailable("run-synthetic", "staged_base_changed", 1, 2)


def test_worktree_review_uses_owned_worktree_not_main_root(tmp_path, monkeypatch):
    root = tmp_path / "root"
    wt = root / ".claude" / "worktrees" / "run-synthetic"
    wt.mkdir(parents=True)
    (root / "dirty-main.txt").write_text("UNRELATED MAIN TREE", encoding="utf-8")
    (root / "agent.patch").write_text("STALE PATCH", encoding="utf-8")
    commands = []

    def fake_run(argv, **kwargs):
        assert kwargs["cwd"] == str(wt)
        assert kwargs["shell"] is False
        assert kwargs["capture_output"] is True
        assert kwargs["text"] is True
        assert kwargs["timeout"] == 30
        commands.append(argv)
        if argv == ["git", "rev-parse", "--verify", "HEAD"]:
            return SimpleNamespace(returncode=0, stdout="baseline-synthetic\n")
        assert kwargs["check"] is True
        if argv == ["git", "add", "-A"]:
            return SimpleNamespace(returncode=0, stdout="")
        assert argv == ["git", "diff", "--cached"]
        return SimpleNamespace(returncode=0, stdout=PATCH)

    monkeypatch.setattr(rr.subprocess, "run", fake_run)
    options = {
        "write_mode": "disk", "runner": None, "wt_path": str(wt),
        "expected_wt_path": str(wt), "baseline_head": "baseline-synthetic",
    }
    candidate = _candidate(tmp_path, **options)
    assert isinstance(candidate, rr.ReviewInput)
    assert candidate.patch_text == PATCH
    assert candidate.binding.source == "worktree"
    assert candidate.binding.run.write_mode == "disk"
    assert commands == [
        ["git", "rev-parse", "--verify", "HEAD"],
        ["git", "add", "-A"],
        ["git", "diff", "--cached"],
    ]
    commands.clear()
    diff_paths = []

    def diff_fn(path):
        diff_paths.append(path)
        return PATCH + "+synthetic followup\n"

    candidate = _candidate(tmp_path, worktree_diff_fn=diff_fn, **options)
    assert candidate.patch_text == PATCH + "+synthetic followup\n"
    assert diff_paths == [str(wt)]
    assert commands == [["git", "rev-parse", "--verify", "HEAD"]]


def test_unowned_or_changed_baseline_worktree_is_rejected(tmp_path, monkeypatch):
    root = tmp_path / "root"
    wt = root / ".claude" / "worktrees" / "owned"
    wt.mkdir(parents=True)
    foreign = tmp_path / "foreign"
    foreign.mkdir()
    sibling = root / ".claude" / "worktrees-other" / "not-owned"
    sibling.mkdir(parents=True)
    symlink = wt.parent / "symlink"
    symlink.symlink_to(wt, target_is_directory=True)
    monkeypatch.setattr(rr, "worktree_head", lambda path: "baseline")
    for path, expected in [
        (root, None), (foreign, None), (sibling, None),
        (wt.parent, None), (wt.parent / "missing", None),
        (symlink, None), (wt, wt.parent / "different-run"),
    ]:
        unavailable = _candidate(
            tmp_path, write_mode="disk", wt_path=str(path),
            expected_wt_path=str(expected) if expected else None,
            baseline_head="baseline", worktree_diff_fn=_forbid,
        )
        assert unavailable.reason == "unowned_worktree"
    missing = _candidate(
        tmp_path, write_mode="disk", wt_path=str(wt), worktree_diff_fn=_forbid,
    )
    assert missing.reason == "missing_worktree_baseline"
    changed = _candidate(
        tmp_path, write_mode="disk", wt_path=str(wt), baseline_head="old-head",
        worktree_diff_fn=_forbid,
    )
    assert changed.reason == "worktree_head_changed"


def test_post_run_review_validates_run_task_transcript_and_patch(tmp_path):
    candidate, record = _saved(tmp_path)
    transcript = Path(candidate.binding.run.transcript.path)
    with transcript.open("ab") as stream:
        stream.write(b'{"event":"completion appended after capture"}\n')
    options = {
        "artifact_dir": str(tmp_path / "artifacts"),
        "task_text": TASK,
        "transcript_path": str(transcript),
    }
    loaded = rr.load_recorded_review_input(record, **options)
    assert isinstance(loaded, rr.ReviewInput)
    assert loaded.binding == candidate.binding
    assert loaded.task_text == TASK
    assert loaded.patch_text == PATCH
    assert loaded.verification_status == "not_run"
    assert loaded.binding.run.transcript.prefix_bytes < transcript.stat().st_size

    mutations = [
        ("run_id", "different-run"),
        ("verification_status", None),
        ("run_binding.task_sha256", "0" * 64),
        ("run_binding.transcript.sha256", "0" * 64),
        ("run_binding.transcript.prefix_bytes", -1),
        ("run_binding.transcript.prefix_bytes", True),
        ("patch_binding.run.run_id", "different-run"),
        ("patch_binding.patch_sha256", "0" * 64),
        ("patch_binding.patch_size", candidate.binding.patch_size + 1),
        ("patch_binding.candidate_seq", "1"),
        ("patch_binding.mutation_seq", -1),
        ("patch_binding.source", "latest_run"),
        ("patch_binding.artifact_path", str(tmp_path / "missing.diff")),
    ]
    artifact_path = Path(candidate.binding.artifact_path)
    outside = tmp_path / "outside.diff"
    outside.write_text(PATCH, encoding="utf-8")
    nested = tmp_path / "artifacts" / "nested" / "candidate.diff"
    nested.parent.mkdir()
    nested.write_text(PATCH, encoding="utf-8")
    link = tmp_path / "artifacts" / "link.diff"
    link.symlink_to(artifact_path)
    mutations.extend(("patch_binding.artifact_path", str(path)) for path in (outside, nested, link))
    for key, value in mutations:
        changed = json.loads(json.dumps(record))
        current = changed
        parts = key.split(".")
        for part in parts[:-1]:
            current = current[part]
        current[parts[-1]] = value
        result = rr.load_recorded_review_input(changed, **options)
        assert isinstance(result, rr.ReviewUnavailable), key
        assert result.reason == "binding_mismatch"

    for key, value in [("task_text", TASK + "different"), ("transcript_path", str(tmp_path / "other.jsonl"))]:
        altered_options = {**options, key: value}
        result = rr.load_recorded_review_input(record, **altered_options)
        assert result.reason == "binding_mismatch"
    for malformed in [None, [], {}, {"run_id": "run-synthetic"}, {**record, "run_binding": None}]:
        assert rr.load_recorded_review_input(malformed, **options).reason == "binding_mismatch"

    original_transcript = transcript.read_bytes()
    transcript.write_bytes(b"X" + original_transcript[1:])
    assert rr.load_recorded_review_input(record, **options).reason == "binding_mismatch"
    transcript.write_bytes(original_transcript)
    artifact_path.write_bytes(PATCH.replace("+new", "+bad").encode("utf-8"))
    assert rr.load_recorded_review_input(record, **options).reason == "binding_mismatch"
    artifact_path.write_bytes(PATCH.encode("utf-8"))
    assert isinstance(rr.load_recorded_review_input(record, **options), rr.ReviewInput)


def test_missing_artifact_never_falls_back_to_dirty_tree(tmp_path, monkeypatch):
    candidate, record = _saved(tmp_path)
    root = tmp_path / "root"
    (root / "dirty-main.txt").write_text("UNRELATED DIRTY MAIN", encoding="utf-8")
    (root / "agent.patch").write_text("STALE OTHER RUN", encoding="utf-8")
    (tmp_path / "args.patch").write_text("UNASSOCIATED PATCH", encoding="utf-8")
    Path(candidate.binding.artifact_path).unlink()
    monkeypatch.setattr(rr.subprocess, "run", _forbid)
    result = _candidate(
        tmp_path, write_mode="disk", runner=None,
        recorded_artifact=_artifact_record(candidate), artifact_dir=str(tmp_path / "artifacts"),
    )
    assert result.reason == "unbounded_disk_write"
    result = rr.load_recorded_review_input(
        record, artifact_dir=str(tmp_path / "artifacts"), task_text=TASK,
        transcript_path=candidate.binding.run.transcript.path,
    )
    assert result.reason == "binding_mismatch"
    assert (root / "dirty-main.txt").read_text(encoding="utf-8") == "UNRELATED DIRTY MAIN"


def test_empty_run_does_not_adopt_stale_patch(tmp_path, monkeypatch):
    root = tmp_path / "root"
    root.mkdir()
    (root / "agent.patch").write_text("STALE OTHER RUN", encoding="utf-8")
    monkeypatch.setattr(rr.subprocess, "run", _forbid)
    candidate = _candidate(tmp_path, patch_text="")
    assert candidate.patch_text == ""
    assert candidate.binding.source == "pending"
    assert candidate.binding.patch_size == 0
    assert candidate.binding.patch_sha256 == hashlib.sha256(b"").hexdigest()
    assert _candidate(tmp_path, runner=None).reason == "staged_base_changed"
    assert _candidate(tmp_path, write_mode="disk", runner=None).reason == "unbounded_disk_write"


def test_reviewer_receives_no_tools_runner_or_mcp(tmp_path, monkeypatch):
    candidate = _candidate(tmp_path, operator_guidance=("Also cover the synthetic edge case.",))
    requests = []
    monkeypatch.setattr(rr.subprocess, "run", _forbid)

    def generate_readonly(req):
        requests.append(req)
        assert set(req) == {"messages", "tools_enabled", "readonly"}
        assert req["tools_enabled"] is False
        assert req["readonly"] is True
        assert len(req["messages"]) == 2
        system, user = req["messages"]
        assert system["role"] == "system"
        assert user["role"] == "user"
        for restriction in ("zero tools", "zero runner", "zero MCP", "zero skills", "ONLY a JSON object"):
            assert restriction in system["content"]
        data = json.loads(user["content"])
        assert data["run_id"] == candidate.binding.run.run_id
        assert data["candidate_seq"] == candidate.binding.candidate_seq
        assert data["mutation_seq"] == candidate.binding.mutation_seq
        assert data["patch_sha256"] == candidate.binding.patch_sha256
        assert data["task_text"] == TASK
        assert data["patch_text"] == PATCH
        assert data["verification_status"] == "not_run"
        assert "cannot alter" in data["verification_status_label"]
        assert data["operator_guidance"] == list(candidate.operator_guidance)
        assert data["operator_guidance_label"] == "Operator amendments to the task."
        assert not {"runner", "tools", "mcp", "skills"}.intersection(data)
        return _response()

    result = rr.review_run(candidate, generate_readonly=generate_readonly)
    assert result.verdict == "pass"
    assert len(requests) == 1


@pytest.mark.parametrize("flags", [{"_implicit_tools": ["write_file"]}, {"tools_enabled": True}])
def test_backend_with_implicit_tools_is_rejected(tmp_path, flags):
    candidate = _candidate(tmp_path)
    requests = []

    def backend(req):
        requests.append(req)
        return {**_response(), **flags}

    result = rr.review_run(candidate, generate_readonly=backend)
    assert result.verdict == "unavailable"
    assert result.applicability == "unavailable"
    assert result.unavailable_reason == "backend_not_readonly"
    assert result.finding_codes == ()
    assert len(requests) == 1


@pytest.mark.parametrize("kind", ["tool_calls", "tool_block", "function_call"])
def test_reviewer_tool_call_is_never_dispatched(tmp_path, monkeypatch, kind):
    candidate = _candidate(tmp_path)
    sentinel = tmp_path / "do-not-edit.txt"
    sentinel.write_text("unchanged", encoding="utf-8")
    arguments = {"path": str(sentinel), "content": "unauthorized change"}
    call = {"name": "write_file", "arguments": json.dumps(arguments)}
    if kind == "tool_calls":
        response = _response(tool_calls=[{"id": "synthetic-call", "type": "function", "function": call}])
    elif kind == "function_call":
        response = _response(function_call=call)
    else:
        response = _response("```tool\n" + json.dumps({"write_file": arguments}) + "\n```")
    monkeypatch.setattr(rr.subprocess, "run", _forbid)
    calls = []

    def backend(req):
        calls.append(req)
        return response

    result = rr.review_run(candidate, generate_readonly=backend)
    assert result.verdict == "abstain"
    assert result.applicability == "current"
    assert result.unavailable_reason == "unexpected_tool_call"
    assert result.finding_codes == ()
    assert sentinel.read_text(encoding="utf-8") == "unchanged"
    assert len(calls) == 1


@pytest.mark.parametrize("status", ["failed", "not_run", "passed (synthetic check)"])
def test_reviewer_pass_cannot_change_verification(tmp_path, status):
    candidate = _candidate(tmp_path, verification_status=status)
    states = []

    def backend(req):
        states.append(json.loads(req["messages"][1]["content"])["verification_status"])
        return _response()

    result = rr.review_run(candidate, generate_readonly=backend)
    assert result.verdict == "pass"
    assert candidate.verification_status == status
    assert states == [status]
    assert "verification_status" not in result.to_metadata()
    assert rr.render_review_block(result) == "[reviewer opinion] pass"
    assert "proof of test results" in rr.format_concerns_notice(result)
    attempt = _response(json.dumps({"verdict": "pass", "finding_codes": [], "verification_status": "passed"}))
    assert rr.review_run(candidate, generate_readonly=lambda req: attempt).unavailable_reason == "malformed_verdict"
    assert candidate.verification_status == status


def test_corrected_candidate_supersedes_review(tmp_path):
    candidate = _candidate(tmp_path)
    result = rr.review_run(candidate, generate_readonly=lambda req: _response())
    assert result.with_current_binding(candidate.binding) is result
    alternatives = [
        None,
        dataclasses.replace(candidate.binding, patch_sha256="0" * 64),
        dataclasses.replace(candidate.binding, candidate_seq=2),
        dataclasses.replace(candidate.binding, mutation_seq=3),
    ]
    for binding in alternatives:
        outdated = result.with_current_binding(binding)
        assert outdated.applicability == "superseded"
        assert outdated.reviewed_binding == candidate.binding
        assert rr.render_review_block(outdated, 2) == (
            "[reviewer opinion] pass — superseded; applies to candidate 1, not candidate 2"
        )
        assert outdated.with_current_binding(candidate.binding).applicability == "superseded"
    corrected = _candidate(tmp_path, patch_text=PATCH + "+correction\n", candidate_seq=2, mutation_seq=3)
    assert result.with_current_binding(corrected.binding).applicability == "superseded"
    unavailable = rr.review_run(rr.ReviewUnavailable("run-synthetic", "binding_mismatch"), generate_readonly=_forbid)
    assert unavailable.with_current_binding(candidate.binding) is unavailable
    assert unavailable.to_metadata()["reviewed_binding"] is None
    assert rr.render_review_block(unavailable) == "[reviewer opinion] unavailable (binding_mismatch)"
    bound_unavailable = dataclasses.replace(result, verdict="unavailable", applicability="unavailable")
    assert bound_unavailable.with_current_binding(None) is bound_unavailable


@pytest.mark.parametrize("kind", ["raise", "not_dict", "transient", "empty_choices", "bad_message"])
def test_review_failure_has_no_unbudgeted_retry(tmp_path, kind):
    candidate = _candidate(tmp_path)
    calls = []

    def backend(req):
        calls.append(req)
        if kind == "raise":
            raise RuntimeError("Synthetic transport failure with private detail")
        if kind == "not_dict":
            return "not a response"
        if kind == "transient":
            return {**_response(), "_transient": False}
        if kind == "empty_choices":
            return {"choices": []}
        return {"choices": [{"message": "bad"}]}

    result = rr.review_run(candidate, generate_readonly=backend)
    assert len(calls) == 1
    assert result.finding_codes == ()
    assert result.reviewed_binding == candidate.binding
    if kind in ("raise", "not_dict", "transient"):
        assert result.verdict == "unavailable"
        assert result.unavailable_reason == "transport_failure"
    else:
        assert result.verdict == "abstain"
        assert result.unavailable_reason == "malformed_verdict"


def test_new_metadata_and_reports_exclude_raw_reviewer_content(tmp_path):
    private_task = "PRIVATE_TASK_CONTENT_9341"
    private_patch = "PRIVATE_PATCH_CONTENT_7285"
    private_reviewer = "PRIVATE_REVIEWER_CONTENT_2947"
    candidate = _candidate(tmp_path, task_text=private_task, patch_text=private_patch)
    codes = ["verification_gap", "task_coverage", "possible_regression", "other", "verification_gap"]
    raw = "```json\n" + json.dumps({"verdict": "concerns", "finding_codes": codes}, indent=2) + "\n```"
    result = rr.review_run(candidate, generate_readonly=lambda req: _response(raw))
    assert result.verdict == "concerns"
    assert result.finding_codes == tuple(codes[:-1])
    metadata = result.to_metadata()
    assert set(metadata) == {"verdict", "finding_codes", "applicability", "unavailable_reason", "reviewed_binding"}
    assert metadata["finding_codes"] == codes[:-1]
    assert metadata["reviewed_binding"] == candidate.binding.to_metadata()
    notice = rr.format_concerns_notice(result)
    expected_lines = [
        "[reviewer opinion — non-authoritative]",
        f"Review verdict: concerns (candidate 1, patch sha256 {candidate.binding.patch_sha256[:12]}).",
        *["- " + rr.FINDING_DESCRIPTIONS[code] for code in codes[:-1]],
        "This is a non-authoritative opinion, not operator guidance and not proof of test results.",
    ]
    assert notice == "\n".join(expected_lines)
    assert rr.render_review_block(result) == (
        "[reviewer opinion] concerns (findings: verification_gap, task_coverage, possible_regression, other)"
    )
    combined = json.dumps(metadata) + notice + rr.render_review_block(result)
    for private in (private_task, private_patch, private_reviewer):
        assert private not in combined
    for raw_content in [
        private_reviewer,
        json.dumps({"verdict": "concerns", "finding_codes": ["other"], "explanation": private_reviewer}),
        json.dumps({"verdict": "concerns", "finding_codes": [private_reviewer]}),
    ]:
        malformed = rr.review_run(candidate, generate_readonly=lambda req: _response(raw_content))
        assert malformed.unavailable_reason == "malformed_verdict"
        report = json.dumps(malformed.to_metadata()) + rr.render_review_block(malformed) + rr.format_concerns_notice(malformed)
        assert private_reviewer not in report
        assert private_task not in report
        assert private_patch not in report


@pytest.mark.parametrize("failure", ["missing", "not_callable", "raises", "not_text", "resolve"])
def test_staged_capture_failures_are_closed(tmp_path, failure):
    def raises(*args):
        raise RuntimeError("Synthetic staged capture failure")

    if failure == "missing":
        runner = None
    elif failure == "not_callable":
        runner = SimpleNamespace(patch="not callable")
    elif failure == "raises":
        runner = SimpleNamespace(patch=raises)
    elif failure == "not_text":
        runner = SimpleNamespace(patch=lambda: b"bytes, not text")
    else:
        runner = SimpleNamespace(
            base_tracker=SimpleNamespace(validate=_forbid), pending={"bad": "new"},
            _resolve=raises, patch=_forbid,
        )
    result = _candidate(tmp_path, runner=runner)
    assert result == rr.ReviewUnavailable("run-synthetic", "staged_base_changed", 1, 2)


def test_transcript_anchor_requires_regular_non_symlink_file(tmp_path):
    transcript = tmp_path / "regular.jsonl"
    raw = "Synthetic transcript with unicode: λ\n".encode("utf-8")
    transcript.write_bytes(raw)
    anchor = rr.capture_transcript_anchor(str(transcript))
    assert anchor.to_metadata() == {
        "path": str(transcript.absolute()), "prefix_bytes": len(raw),
        "sha256": hashlib.sha256(raw).hexdigest(),
    }
    directory = tmp_path / "directory"
    directory.mkdir()
    link = tmp_path / "link.jsonl"
    link.symlink_to(transcript)
    for invalid in (directory, link, tmp_path / "missing.jsonl"):
        assert rr.capture_transcript_anchor(str(invalid)) is None
        result = _candidate(tmp_path, transcript_path=str(invalid))
        assert result == rr.ReviewUnavailable("run-synthetic", "transcript_unavailable", 1, 2)
    assert rr.ReviewUnavailable("run-synthetic", "transcript_unavailable").to_metadata() == {
        "run_id": "run-synthetic", "reason": "transcript_unavailable", "candidate_seq": 0, "mutation_seq": 0,
    }


def test_recorded_artifact_capture_requires_exact_binding(tmp_path, monkeypatch):
    candidate, _ = _saved(tmp_path)
    descriptor = _artifact_record(candidate)
    monkeypatch.setattr(rr.subprocess, "run", _forbid)
    options = {"write_mode": "disk", "runner": None, "artifact_dir": str(tmp_path / "artifacts")}
    loaded = _candidate(tmp_path, recorded_artifact=descriptor, **options)
    assert isinstance(loaded, rr.ReviewInput)
    assert loaded.patch_text == PATCH
    assert loaded.binding.source == "recorded_artifact"
    assert loaded.binding.patch_sha256 == candidate.binding.patch_sha256
    assert loaded.binding.patch_size == candidate.binding.patch_size
    outside = tmp_path / "outside.diff"
    outside.write_text(PATCH, encoding="utf-8")
    nested = tmp_path / "artifacts" / "nested" / "candidate.diff"
    nested.parent.mkdir()
    nested.write_text(PATCH, encoding="utf-8")
    link = tmp_path / "artifacts" / "link.diff"
    link.symlink_to(candidate.binding.artifact_path)
    for key, value in [
        ("run_id", "other-run"), ("candidate_seq", 2), ("mutation_seq", 3),
        ("sha256", "0" * 64), ("size", candidate.binding.patch_size + 1),
        ("path", str(outside)), ("path", str(nested)), ("path", str(link)),
        ("path", str(tmp_path / "missing.diff")), ("path", None),
    ]:
        result = _candidate(tmp_path, recorded_artifact={**descriptor, key: value}, **options)
        assert result.reason == "unbounded_disk_write", key
    for invalid in (None, [], {}):
        assert _candidate(tmp_path, recorded_artifact=invalid, **options).reason == "unbounded_disk_write"


@pytest.mark.parametrize("field,limit", [("patch_text", 120_000), ("task_text", 60_000)])
def test_review_limits_do_not_call_backend(tmp_path, field, limit):
    candidate = _candidate(tmp_path, **{field: "λ" * (limit + 1)})
    result = rr.review_run(candidate, generate_readonly=_forbid)
    assert result.verdict == "unavailable"
    assert result.applicability == "unavailable"
    assert result.unavailable_reason == "input_too_large"
    assert result.reviewed_binding == candidate.binding
    boundary = _candidate(tmp_path, **{field: "λ" * limit})
    assert rr.review_run(boundary, generate_readonly=lambda req: _response()).verdict == "pass"


@pytest.mark.parametrize("content", [
    "not JSON", "null", "[]", "{}",
    '{"verdict":"pass","finding_codes":["other"]}',
    '{"verdict":"approved","finding_codes":[]}',
    '{"verdict":"concerns","finding_codes":["not_allowlisted"]}',
    '{"verdict":"concerns","finding_codes":[1]}',
    '{"verdict":"concerns","finding_codes":"other"}',
    '{"verdict":"pass","finding_codes":[],"extra":"raw opinion"}',
    '{"finding_codes":[]}', '{"verdict":"pass"}',
    '{"verdict":null,"finding_codes":[]}',
])
def test_malformed_reviewer_verdicts_are_abstentions(tmp_path, content):
    result = rr.review_run(_candidate(tmp_path), generate_readonly=lambda req: _response(content))
    assert result.verdict == "abstain"
    assert result.applicability == "current"
    assert result.unavailable_reason == "malformed_verdict"
    assert result.finding_codes == ()


@pytest.mark.parametrize("wrapper", ["{}", "```json\n{}\n```", "```\n{}\n```"])
@pytest.mark.parametrize("verdict", ["pass", "concerns", "abstain"])
def test_allowlisted_verdicts_and_json_wrappers(tmp_path, wrapper, verdict):
    codes = [] if verdict == "pass" else ["other", "task_coverage", "other"]
    content = wrapper.format(json.dumps({"verdict": verdict, "finding_codes": codes}))
    result = rr.review_run(_candidate(tmp_path), generate_readonly=lambda req: _response(content))
    assert result.verdict == verdict
    assert result.finding_codes == tuple(dict.fromkeys(codes))
    assert result.applicability == "current"
    assert result.unavailable_reason is None


def test_artifact_save_is_atomic_and_does_not_follow_destination_symlink(tmp_path, monkeypatch):
    candidate = _candidate(tmp_path, patch_text=PATCH + "+unicode λ\n")
    directory = tmp_path / "artifacts"
    directory.mkdir()
    dest = directory / f"patch-{candidate.binding.patch_sha256[:16]}.diff"
    outside = tmp_path / "untouched.txt"
    outside.write_text("untouched", encoding="utf-8")
    dest.symlink_to(outside)
    events = []
    real_fsync = os.fsync
    real_replace = os.replace

    def fsync(fd):
        events.append("fsync")
        real_fsync(fd)

    def replace(source, target):
        events.append("replace")
        assert Path(source).parent == directory
        assert Path(source).read_bytes() == candidate.patch_text.encode("utf-8")
        assert Path(target) == dest
        real_replace(source, target)

    monkeypatch.setattr(rr.os, "fsync", fsync)
    monkeypatch.setattr(rr.os, "replace", replace)
    saved = rr.save_candidate_artifact(str(directory), candidate)
    assert events == ["fsync", "replace"]
    assert saved.binding.artifact_path == str(dest)
    assert candidate.binding.artifact_path is None
    assert not dest.is_symlink()
    assert dest.read_bytes() == candidate.patch_text.encode("utf-8")
    assert outside.read_text(encoding="utf-8") == "untouched"
    assert list(directory.iterdir()) == [dest]
    assert dataclasses.replace(saved.binding, artifact_path=None) == candidate.binding

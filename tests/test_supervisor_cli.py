"""Driver (run_goal) tests: real core journal, real temp Git integration, real protected validators.
Only the transport is faked (tests/supervisor_driver_helpers.FakeAdapter)."""
import json
import pytest

from supervisor_helpers import load
from supervisor_evidence_helpers import git
from supervisor_driver_helpers import GOAL, CAPS, FakeAdapter, correcting_planner, setup, two_worker_planner

supervise = load("supervise")


def _run(fx, planner, **kw):
    adapter = FakeAdapter(fx.envelope, fx.artifacts, planner)
    out = supervise.run_goal(fx.envelope, goal_text=GOAL, registry_raw=fx.registry, capabilities=CAPS,
                             state_dir=fx.state, artifact_root=fx.artifacts, adapter=adapter,
                             wait=lambda s: None, **kw)
    return out, adapter


def _main_untouched(fx):
    assert git(fx.project, "status", "--porcelain") == ""
    assert git(fx.project, "rev-parse", "HEAD").strip() == fx.baseline
    assert git(fx.project, "worktree", "list", "--porcelain").count("worktree ") == 1


def test_t01_two_workers_integrate_and_pass_protected_checks(tmp_path):
    fx = setup(tmp_path)
    out, adapter = _run(fx, two_worker_planner)
    assert (out["outcome"], out["reason"]) == ("SUCCEEDED", "succeeded"), out
    assert [k for k, _ in adapter.launches] == ["call_planner", "launch_worker", "launch_worker"]
    assert out["counters"]["attempts"] == 2 and out["counters"]["replans"] == 0
    assert out["satisfied_criteria"] == ["C1", "C2", "CI"] and len(out["candidate_sha256"]) == 64
    assert out["unresolved"] == 0
    _main_untouched(fx)
    assert "canary-7f3a" not in json.dumps(out)


def test_t02_failed_integration_drives_one_bounded_correction(tmp_path):
    fx = setup(tmp_path, ci_wants="B2")  # initial B is integrated but CI flags it stale
    out, adapter = _run(fx, correcting_planner)
    assert (out["outcome"], out["reason"]) == ("SUCCEEDED", "succeeded"), out
    kinds = [k for k, _ in adapter.launches]
    assert kinds == ["call_planner", "launch_worker", "launch_worker", "call_planner", "launch_worker"]
    assert out["counters"]["planner_calls"] == 2 and out["counters"]["attempts"] == 3
    replan_brief = adapter.briefs[3][1]
    assert "chk-ci" in replan_brief and "b_stale" in replan_brief  # consumed the trusted E1 fact
    assert "canary-7f3a" in replan_brief and "canary-7f3a" not in json.dumps(out)
    _main_untouched(fx)


def test_cli_help_does_not_initialize_state_or_contact_an_engine(tmp_path, capsys):
    before = list(tmp_path.iterdir())
    with pytest.raises(SystemExit) as exc:
        supervise.main(["--help"])
    assert exc.value.code == 0
    assert "preflight" in capsys.readouterr().out and list(tmp_path.iterdir()) == before


def test_old_engine_refusal_precedes_any_goal_state_or_model_admission(tmp_path):
    fx = setup(tmp_path)

    class Old(FakeAdapter):
        def preflight(self):
            raise supervise.harness.HarnessError("engine_unsupported")

    ad = Old(fx.envelope, fx.artifacts, two_worker_planner)
    with pytest.raises(supervise.harness.HarnessError):
        supervise.run_goal(fx.envelope, goal_text=GOAL, registry_raw=fx.registry,
                           capabilities=CAPS, state_dir=fx.state, artifact_root=fx.artifacts, adapter=ad)
    assert not fx.state.exists() and not fx.artifacts.exists() and ad.launches == []


def test_cli_bad_approval_is_fixed_metadata_without_input_echo(tmp_path, capsys):
    fx = setup(tmp_path)
    envelope, goal, registry, capabilities = [tmp_path / n for n in
                                              ("approval.json", "goal.txt", "registry.json", "caps.json")]
    envelope.write_bytes(fx.raw)
    goal.write_text(GOAL)
    registry.write_bytes(fx.registry)
    capabilities.write_text(json.dumps(CAPS))
    code = supervise.main(["preflight", "--project", str(fx.project), "--envelope", str(envelope),
                           "--approve-sha256", "f" * 64, "--goal", str(goal), "--registry", str(registry),
                           "--capabilities", str(capabilities), "--state", str(fx.state),
                           "--artifacts", str(fx.artifacts)])
    assert code == 2
    output = capsys.readouterr()
    assert json.loads(output.out)["error"] == "envelope_invalid"
    assert not fx.state.exists() and not fx.artifacts.exists() and not output.err


def test_prepare_writes_only_unapproved_metadata_and_never_dispatches(tmp_path, monkeypatch, capsys):
    fx = setup(tmp_path)
    policy = fx.project / ".claude" / "agent-policy.json"
    policy.parent.mkdir()
    policy.write_text('{"allowed_cmds":[["git","status"]]}')
    goal, registry, caps, out = [tmp_path / n for n in
                                ("goal.txt", "registry.json", "caps.json", "new-approval.json")]
    goal.write_text(GOAL)
    registry.write_bytes(fx.registry)
    caps.write_text(json.dumps(CAPS))
    monkeypatch.setattr(supervise.harness.HarnessAdapter, "preflight",
                        lambda *a: pytest.fail("prepare must not probe or dispatch"))
    result = supervise.main(["prepare", "--project", str(fx.project), "--goal", str(goal),
                             "--registry", str(registry), "--capabilities", str(caps), "--out", str(out)])
    assert result == 0
    data = json.loads(capsys.readouterr().out)
    assert data["approved"] is False and not fx.state.exists() and not fx.artifacts.exists()
    env = supervise.contract.load_envelope(out.read_bytes(), data["sha256"], fx.project)
    assert env.goal_digest == supervise._sha(GOAL.encode())
    assert GOAL not in out.read_text() and env.schema_version == 2
    assert env.limits["max_replans"] is None


def test_deadline_arriving_with_collection_cannot_start_verification(tmp_path):
    fx = setup(tmp_path)
    clock = type("Clock", (), {"t": 1000.0, "__call__": lambda self: self.t})()

    class Expires(FakeAdapter):
        def observe(self, identity):
            obs = super().observe(identity)
            if "attempt_id" in identity and obs["state"] == "TERMINAL":
                clock.t = self.envelope.deadline
            return obs

    ad = Expires(fx.envelope, fx.artifacts, two_worker_planner)
    out = supervise.run_goal(fx.envelope, goal_text=GOAL, registry_raw=fx.registry, capabilities=CAPS,
                              state_dir=fx.state, artifact_root=fx.artifacts, adapter=ad, clock=clock,
                              wait=lambda s: setattr(clock, "t", clock.t + s))
    assert (out["outcome"], out["reason"]) == ("PARTIAL", "deadline")
    assert not (fx.artifacts / "batches").exists()


def test_token_capacity_can_serialize_workers_without_refusing_the_goal(tmp_path):
    fx = setup(tmp_path)
    data = supervise.contract.strict_json(fx.raw)
    data["max_usage"] = 1500
    raw = supervise.contract.canonical(data)
    fx.envelope = supervise.contract.load_envelope(raw, supervise._sha(raw), fx.project)
    out, ad = _run(fx, two_worker_planner)
    assert out["outcome"] == "SUCCEEDED" and out["counters"]["attempts"] == 2
    assert len(ad.launches) == 3 and out["usage"]["charged"] == 30


def test_keyboard_interrupt_enters_durable_stopping_not_an_untracked_exit(tmp_path):
    fx = setup(tmp_path)
    clock = type("Clock", (), {"t": 1000.0, "__call__": lambda self: self.t})()

    class Interrupted(FakeAdapter):
        def launch(self, effect, brief, **kw):
            result = super().launch(effect, brief, **kw)
            if effect.kind == "call_planner":
                raise KeyboardInterrupt
            return result

    ad = Interrupted(fx.envelope, fx.artifacts, two_worker_planner)
    try:
        out = supervise.run_goal(fx.envelope, goal_text=GOAL, registry_raw=fx.registry, capabilities=CAPS,
                                  state_dir=fx.state, artifact_root=fx.artifacts, adapter=ad, clock=clock,
                                  wait=lambda s: setattr(clock, "t", clock.t + s))
    except KeyboardInterrupt:
        pytest.fail("interrupt escaped without a durable stopping decision")
    assert out["outcome"] == "CANCELLED" and out["counters"]["attempts"] == 0


def test_unknown_call_stopping_uses_bounded_waits_not_busy_polling(tmp_path):
    fx = setup(tmp_path)
    data = supervise.contract.strict_json(fx.raw)
    data["deadline"] = 1002.0
    raw = supervise.contract.canonical(data)
    fx.envelope = supervise.contract.load_envelope(raw, supervise._sha(raw), fx.project)

    class Clock:
        t, calls = 1000.0, 0

        def __call__(self):
            self.calls += 1
            if self.calls > 5000:
                pytest.fail("stopping loop busy-polled without advancing its wait")
            return self.t

    clock, waits = Clock(), []

    def wait(seconds):
        waits.append(seconds)
        clock.t += seconds

    class Unknown(FakeAdapter):
        def observe(self, identity):
            return {"state": "UNKNOWN", "outcome": None, "quiescent": False,
                    "usage": None, "reason": "result_unavailable"}

    ad = Unknown(fx.envelope, fx.artifacts, two_worker_planner)
    out = supervise.run_goal(fx.envelope, goal_text=GOAL, registry_raw=fx.registry, capabilities=CAPS,
                              state_dir=fx.state, artifact_root=fx.artifacts, adapter=ad,
                              clock=clock, wait=wait)
    assert out["outcome"] == "BLOCKED" and len(ad.launches) == 1
    assert waits and sum(waits) >= 62 and clock.t == 1062.0


def test_objective_failure_after_first_launch_enters_stopping_and_retains_admission(tmp_path):
    fx = setup(tmp_path)
    clock = type("Clock", (), {"t": 1000.0, "__call__": lambda self: self.t})()

    class Broken(FakeAdapter):
        cancelled = []

        def objective(self, identity, task):
            if task.task_id == "W2":
                raise supervise.harness.HarnessError("result_unavailable")
            return super().objective(identity, task)

        def observe(self, identity):
            if "attempt_id" in identity:
                return {"state": "UNKNOWN", "outcome": None, "quiescent": False,
                        "usage": None, "reason": "result_unavailable"}
            return super().observe(identity)

        def cancel(self, identity, owner_guard, **kw):
            self.cancelled.append(identity.get("attempt_id") or identity["call_id"])
            return {"quiescent": False}

    ad = Broken(fx.envelope, fx.artifacts, two_worker_planner)
    out = supervise.run_goal(fx.envelope, goal_text=GOAL, registry_raw=fx.registry, capabilities=CAPS,
                              state_dir=fx.state, artifact_root=fx.artifacts, adapter=ad, clock=clock,
                              wait=lambda s: setattr(clock, "t", clock.t + s))
    assert out["outcome"] == "BLOCKED" and out["counters"]["attempts"] == 2
    assert len(ad.launches) == 2 and ad.cancelled and clock.t <= 1062


@pytest.mark.parametrize("failure", ["deadline", "owner"])
def test_collection_cannot_cross_into_integration_after_authority_expires(tmp_path, monkeypatch, failure):
    fx = setup(tmp_path)
    clock = type("Clock", (), {"t": 1000.0, "__call__": lambda self: self.t})()
    held = [True]
    original_guard = supervise.core.Supervisor.owner_current
    monkeypatch.setattr(supervise.core.Supervisor, "owner_current",
                        lambda self, epoch=None: held[0] and original_guard(self, epoch))

    class Changes(FakeAdapter):
        reads = {}

        def read_worker(self, identity):
            n = self.reads[identity["launch_id"]] = self.reads.get(identity["launch_id"], 0) + 1
            result = super().read_worker(identity)
            if n == 2:  # collection during integration, not the first terminal validation
                if failure == "deadline":
                    clock.t = self.envelope.deadline
                else:
                    held[0] = False
            return result

    entered = []

    def forbidden(*a, **kw):
        entered.append(True)
        pytest.fail("integration started after the collection fence expired")

    monkeypatch.setattr(supervise.evidence, "integrate", forbidden)
    ad = Changes(fx.envelope, fx.artifacts, two_worker_planner)
    call = lambda: supervise.run_goal(fx.envelope, goal_text=GOAL, registry_raw=fx.registry,
                capabilities=CAPS, state_dir=fx.state, artifact_root=fx.artifacts, adapter=ad,
                clock=clock, wait=lambda s: setattr(clock, "t", clock.t + s))
    if failure == "owner":
        with pytest.raises(supervise.SuperviseError) as exc:
            call()
        assert exc.value.code == "ownership_lost"
    else:
        assert call()["outcome"] == "BLOCKED"
    assert entered == [] and len(ad.launches) == 3


def test_each_cancellation_is_capped_by_remaining_original_grace(tmp_path):
    import test_supervisor_state as st
    clock = st.Clock()
    calls = []

    class Slow:
        def cancel(self, identity, owner_guard, *, timeout=5):
            assert owner_guard()
            calls.append((identity["attempt_id"], timeout))
            clock.t += timeout + 0.01
            return {"quiescent": False}

    with st._open(tmp_path, clock) as sup:
        st._planned(sup)
        st._launch(sup, "W1", 1)
        st._launch(sup, "W2", 2)
        sup.request_stop("cancelled")
        clock.t = sup.stop_deadline() - 0.1
        driver = supervise._Driver(sup, sup.envelope, Slow(), "synthetic", b"{}", {},
                                    str(tmp_path / "artifacts"), clock, lambda s: None, 1, None)
        driver.cancel_owned()
        assert len(calls) == 1 and 0 < calls[0][1] <= 0.100001

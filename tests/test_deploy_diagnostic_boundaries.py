"""The deploy gate must exercise boundary and real-interpreter regressions."""
from tests.test_deploy_recovery_bundle import dm


def test_diagnostic_gate_selects_boundary_regressions_only_when_enabled(dm, tmp_path, monkeypatch):
    tests = tmp_path / "tests"
    tests.mkdir()
    wanted = {"tests/test_engine_boundaries.py", "tests/test_engine_failure_reproduction.py"}
    for name in wanted:
        (tmp_path / name).write_text("# synthetic contract fixture\n", encoding="utf-8")
    monkeypatch.setattr(dm, "_REPO_ROOT", tmp_path)
    selected = dm.gate_command(False, include_diagnostics=True)
    assert wanted <= set(selected)
    assert not wanted.intersection(dm.gate_command(False, include_diagnostics=False))


def test_helpers_are_published_before_the_entrypoint_that_uses_them(dm, tmp_path, monkeypatch):
    destination = tmp_path / "installed"
    destination.mkdir()
    engine, helper = destination / "delegate.py", destination / "command_output.py"
    engine.write_bytes(b"# synthetic old engine\n")
    helper.write_bytes(b"# synthetic old helper\n")
    new_engine, new_helper = b"# synthetic new engine\n", b"# synthetic new helper\n"
    plan = [dm._Planned(name="delegate.py", destination=engine, payload=new_engine),
            dm._Planned(name="command_output.py", destination=helper, payload=new_helper)]
    monkeypatch.setattr(dm, "_plan", lambda source, target: plan)
    monkeypatch.setattr(dm, "_verify", lambda item: None)
    real_write = dm._atomic_write
    def publish(path, payload):
        if path == engine and payload == new_engine:
            assert helper.read_bytes() == new_helper, "new entrypoint must not see an old helper"
        return real_write(path, payload)
    monkeypatch.setattr(dm, "_atomic_write", publish)
    dm.deploy(tmp_path / "source.py", engine, apply=True,
              test_command=[dm.sys.executable, "-c", "pass"])
    assert engine.read_bytes() == new_engine

import os
import sys

import pytest

from supervisor_helpers import load
from supervisor_evidence_helpers import (diff_for, git, make_envelope, make_registry, make_repo,
                                         write_artifacts)

ev = load("supervisor_evidence")
contract = load("supervisor_contract")


def _tree(path, files):
    for rel, data in files.items():
        p = path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
    return path


def test_identity_same_bytes_same_id_regardless_of_location(tmp_path):
    a = _tree(tmp_path / "a", {"src/x.py": b"x = 1\n", "README": b"r\n"})
    b = _tree(tmp_path / "b", {"README": b"r\n", "src/x.py": b"x = 1\n"})
    (b / ".git").mkdir()  # reserved metadata is excluded
    (b / ".git" / "HEAD").write_bytes(b"ref\n")
    assert ev.candidate_identity(a) == ev.candidate_identity(b)
    assert contract.SHA256.match(ev.candidate_identity(a))


def test_identity_aba_and_content_change(tmp_path):
    root = _tree(tmp_path / "c", {"f": b"A"})
    first = ev.candidate_identity(root)
    (root / "f").write_bytes(b"B")
    changed = ev.candidate_identity(root)
    (root / "f").write_bytes(b"A")
    assert changed != first and ev.candidate_identity(root) == first


def test_patch_path_set_uses_git_parser_and_handles_spaces_and_empty_content(tmp_path, calc):
    patch = diff_for(calc[0], {"calc.py": b"synthetic\n", "src/one space.txt": b"new\n"})
    assert ev.patch_paths(calc[0], patch) == frozenset({"calc.py", "src/one space.txt"})
    assert ev.patch_paths(calc[0], b"") == frozenset()
    assert git(calc[0], "status", "--porcelain") == ""


def test_candidate_hash_budget_refuses_large_or_slow_snapshots(tmp_path, monkeypatch):
    root = tmp_path / "tree"
    _tree(root, {"large.bin": b"x" * 1024})
    with pytest.raises(ev.EvidenceError) as exc:
        ev.candidate_identity(root, max_bytes=16)
    assert exc.value.code == "candidate_limit"
    ticks = iter([0.0, 2.0])
    monkeypatch.setattr(ev.time, "monotonic", lambda: next(ticks, 2.0))
    with pytest.raises(ev.EvidenceError) as exc:
        ev.candidate_identity(root, max_seconds=1.0)
    assert exc.value.code == "candidate_limit"


def test_identity_counts_new_empty_dir_free_generated_files(tmp_path):
    root = _tree(tmp_path / "c", {"f": b"A"})
    first = ev.candidate_identity(root)
    _tree(root, {"__pycache__/f.cpython.pyc": b"gen"})  # generated output is NOT ignored
    assert ev.candidate_identity(root) != first


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX exec bit")
def test_identity_tracks_exec_mode(tmp_path):
    root = _tree(tmp_path / "c", {"f": b"A"})
    first = ev.candidate_identity(root)
    os.chmod(root / "f", 0o755)
    assert ev.candidate_identity(root) != first


def test_identity_rejects_escaping_link(tmp_path):
    outside = _tree(tmp_path / "out", {"secret": b"s"})
    root = _tree(tmp_path / "c", {"f": b"A"})
    try:
        os.symlink(outside / "secret", root / "link")
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable")
    with pytest.raises(ev.EvidenceError) as err:
        ev.candidate_identity(root)
    assert err.value.code == "unsafe_entry"


BASE = "b" * 40


def _expected(art, **over):
    exp = {"run_id": "run-1", "task_sha256": "1" * 64, "root_identity": "wt-1",
           "baseline_commit": BASE, "patch_ref": "agent.patch", "findings_ref": "findings.md"}
    exp.update(over)
    return exp


def _collect_code(art, expected, approved=BASE):
    with pytest.raises(ev.EvidenceError) as err:
        ev.collect_result(art, expected, approved)
    return err.value.code


def test_collect_bound_result(tmp_path):
    art = tmp_path / "art"
    write_artifacts(art, "run-1", b"diff\n", "wt-1", "1" * 64, findings="synthetic finding")
    res = ev.collect_result(art, _expected(art), BASE)
    assert res.run_id == "run-1" and res.findings == "synthetic finding"
    assert res.patch_bytes == b"diff\n" and contract.SHA256.match(res.transport_sha256)
    meta = res.metadata()
    assert "findings" not in meta and "patch_bytes" not in meta and meta["run_id"] == "run-1"


@pytest.mark.parametrize("mutate,code", [
    (lambda art: (art / "completion.json").unlink(), "completion_missing"),
    (lambda art: (art / "completion.json").write_bytes(b'{"run_id": "run-1", '), "completion_malformed"),
    (lambda art: (art / "agent.patch").write_bytes(b"tampered\n"), "patch_digest_mismatch"),
    (lambda art: (art / "findings.md").write_bytes(b"x" * 4001), "findings_oversized"),
    (lambda art: (art / "findings.md").unlink(), "findings_missing"),
])
def test_collect_rejects_missing_torn_tampered(tmp_path, mutate, code):
    art = tmp_path / "art"
    write_artifacts(art, "run-1", b"diff\n", "wt-1", "1" * 64, findings="f")
    mutate(art)
    assert _collect_code(art, _expected(art)) == code


def test_collect_rejects_wrong_run_stale_baseline_stage_claim(tmp_path):
    art = tmp_path / "art"
    write_artifacts(art, "run-2", b"diff\n", "wt-1", "1" * 64, findings="f")
    assert _collect_code(art, _expected(art)) == "wrong_run"
    write_artifacts(art, "run-1", b"diff\n", "wt-1", "1" * 64, findings="f")
    assert _collect_code(art, _expected(art), approved="c" * 40) == "stale_baseline"
    assert _collect_code(art, _expected(art, task_sha256="2" * 64)) == "wrong_run"
    write_artifacts(art, "run-1", b"diff\n", "wt-1", "1" * 64, write_mode="stage", findings="f")
    assert _collect_code(art, _expected(art)) == "stage_verification_claim"


def test_collect_null_binding_is_unknown_not_empty(tmp_path):
    art = tmp_path / "art"
    rec = write_artifacts(art, "run-1", b"", "wt-1", "1" * 64, findings="f")
    rec["patch_binding"] = None
    (art / "completion.json").write_bytes(contract.canonical(rec))
    assert _collect_code(art, _expected(art)) == "binding_unavailable"


def test_collect_rejects_escaping_reference(tmp_path):
    art = tmp_path / "art"
    write_artifacts(art, "run-1", b"diff\n", "wt-1", "1" * 64, findings="f")
    assert _collect_code(art, _expected(art, patch_ref="../agent.patch")) == "unsafe_reference"
    outside = tmp_path / "outside.md"
    outside.write_bytes(b"f")
    (art / "findings.md").unlink()
    try:
        os.symlink(outside, art / "findings.md")
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable")
    assert _collect_code(art, _expected(art)) == "unsafe_reference"


@pytest.mark.parametrize("over", [{"run_id": 7}, {"run_id": ""}, {"task_sha256": "zz"},
                                  {"task_sha256": ["1" * 64]}, {"baseline_commit": "short"},
                                  {"root_identity": None}, {"patch_ref": None}, {"findings_ref": 3}])
def test_collect_rejects_malformed_expected_binding(tmp_path, over):
    art = tmp_path / "art"
    write_artifacts(art, "run-1", b"diff\n", "wt-1", "1" * 64, findings="f")
    approved = over.get("baseline_commit", BASE)
    assert _collect_code(art, _expected(art, **over), approved) == "expected_binding_malformed"


def test_collect_rejects_unknown_verification_status(tmp_path):
    art = tmp_path / "art"
    for status in ("SECRET-canary text", ["verified_by_command"], None):
        write_artifacts(art, "run-1", b"diff\n", "wt-1", "1" * 64, status=status, findings="f")
        assert _collect_code(art, _expected(art)) == "completion_malformed"


def test_collect_oversized_patch_rejected_even_if_prefix_digest_matches(tmp_path, monkeypatch):
    import hashlib
    monkeypatch.setattr(ev, "MAX_PATCH_BYTES", 16)
    art = tmp_path / "art"
    rec = write_artifacts(art, "run-1", b"p" * 40, "wt-1", "1" * 64, findings="f")
    rec["patch_binding"].update(patch_sha256=hashlib.sha256(b"p" * 17).hexdigest(), patch_size=17)
    (art / "completion.json").write_bytes(contract.canonical(rec))
    assert _collect_code(art, _expected(art)) == "patch_oversized"


def test_identity_read_race_is_fixed_code(tmp_path, monkeypatch):
    root = _tree(tmp_path / "r", {"a.txt": b"a"})
    def vanish(path, **kwargs):
        raise FileNotFoundError("SECRET path detail")
    monkeypatch.setattr(ev, "_file_sha", vanish)
    with pytest.raises(ev.EvidenceError) as err:
        ev.candidate_identity(root)
    assert err.value.code == "candidate_unreadable" and "SECRET" not in str(err.value)


def _results(tmp_path, repo, base, patches):
    out = []
    for i, patch in enumerate(patches):
        art = tmp_path / f"art{i}"
        write_artifacts(art, f"run-{i}", patch, "wt", "1" * 64, findings=None)
        exp = _expected(art, run_id=f"run-{i}", root_identity="wt", baseline_commit=base,
                        findings_ref=None)
        out.append(ev.collect_result(art, exp, base))
    return out


def _main_state(repo):
    return (git(repo, "rev-parse", "HEAD"), git(repo, "show-ref"), git(repo, "status", "--porcelain"),
            git(repo, "worktree", "list", "--porcelain"), (repo / ".git" / "index").read_bytes())


@pytest.fixture
def repo(tmp_path):
    files = {"a.txt": b"".join(b"line%d\n" % i for i in range(20)), "gone.txt": b"bye\n",
             "b.txt": b"b\n"}
    path = tmp_path / "main"
    return path, make_repo(path, files)


def test_integrate_composes_two_patches_without_touching_main(tmp_path, repo):
    path, base = repo
    p1 = diff_for(path, {"a.txt": (path / "a.txt").read_bytes().replace(b"line2\n", b"two\n")})
    p2 = diff_for(path, {"new/n.txt": b"n\n", "gone.txt": None})
    before = _main_state(path)
    res = ev.integrate(path, base, _results(tmp_path, path, base, [p1, p2]), tmp_path / "cand")
    assert res.conflict_run_id is None
    tree = tmp_path / "cand" / "tree"
    assert (tree / "new" / "n.txt").read_bytes() == b"n\n" and not (tree / "gone.txt").exists()
    assert b"two\n" in (tree / "a.txt").read_bytes()
    assert res.candidate_sha256 == ev.candidate_identity(tree)
    manifest = contract.strict_json((tmp_path / "cand" / "manifest.json").read_bytes())
    assert manifest["candidate_sha256"] == res.candidate_sha256
    assert manifest["sources"] == list(res.sources) and len(res.sources) == 2
    assert _main_state(path) == before


def test_integrate_same_tree_from_different_serialization_and_noop(tmp_path, repo):
    path, base = repo
    edit = {"a.txt": (path / "a.txt").read_bytes().replace(b"line9\n", b"nine\n")}
    p_ctx, p_tight = diff_for(path, edit, "-U8"), diff_for(path, edit, "-U0")
    assert p_ctx != p_tight
    a = ev.integrate(path, base, _results(tmp_path / "x", path, base, [p_ctx]), tmp_path / "ca")
    b = ev.integrate(path, base, _results(tmp_path / "y", path, base, [p_tight]), tmp_path / "cb")
    assert a.candidate_sha256 == b.candidate_sha256
    noop = ev.integrate(path, base, _results(tmp_path / "z", path, base, [b""]), tmp_path / "cn")
    empty = ev.integrate(path, base, [], tmp_path / "ce")
    assert noop.candidate_sha256 == empty.candidate_sha256 != a.candidate_sha256


def test_integrate_conflict_is_fact_not_resolution(tmp_path, repo):
    path, base = repo
    orig = (path / "a.txt").read_bytes()
    p1 = diff_for(path, {"a.txt": orig.replace(b"line5\n", b"five-A\n")})
    p2 = diff_for(path, {"a.txt": orig.replace(b"line5\n", b"five-B\n")})
    res = ev.integrate(path, base, _results(tmp_path, path, base, [p1, p2]), tmp_path / "cand")
    assert res.conflict_run_id == "run-1" and res.candidate_sha256 is None


def test_integrate_holds_existing_destination(tmp_path, repo):
    path, base = repo
    dest = tmp_path / "cand"
    dest.mkdir()
    (dest / "keep").write_bytes(b"k")
    with pytest.raises(ev.EvidenceError) as err:
        ev.integrate(path, base, [], dest)
    assert err.value.code == "candidate_exists" and (dest / "keep").read_bytes() == b"k"


def test_integrate_rejects_result_bound_to_other_baseline(tmp_path, repo):
    path, base = repo
    (stale,) = _results(tmp_path, path, base, [b""])
    with pytest.raises(ev.EvidenceError) as err:
        ev.integrate(path, "c" * 40, [stale], tmp_path / "cand")
    assert err.value.code == "stale_baseline" and not (tmp_path / "cand").exists()


def _scope_code(tmp_path, path, base, patch, allowed, name):
    with pytest.raises(ev.EvidenceError) as err:
        ev.integrate(path, base, _results(tmp_path / name, path, base, [patch]), tmp_path / name / "c",
                     allowed_write_paths=allowed)
    return err.value.code


def test_integrate_enforces_allowed_write_paths(tmp_path, repo):
    path, base = repo
    inside = diff_for(path, {"new/n.txt": b"n\n"})
    bad = {"o": (diff_for(path, {"a.txt": b"x\n"}), ("new",)),
           "d": (diff_for(path, {"gone.txt": None}), ("new",)),
           "p": (diff_for(path, {"newer.txt": b"p\n"}), ("new",)),
           "m": (b"diff --git a/b.txt b/b.txt\nold mode 100644\nnew mode 100755\n", ("new",))}
    for reserved in (".claude/settings.json", ".supervisor/state.json", ".gitmodules"):
        top = reserved.split("/")[0]
        bad[top] = (diff_for(path, {reserved: b"{}\n"}), (top,))
    before = _main_state(path)
    ok = ev.integrate(path, base, _results(tmp_path / "ok", path, base, [inside]), tmp_path / "okc",
                      allowed_write_paths=("new",))
    assert ok.conflict_run_id is None and ok.candidate_sha256
    for name, (patch, allowed) in bad.items():
        assert _scope_code(tmp_path, path, base, patch, allowed, name) == "write_scope_violation", name
    assert _main_state(path) == before


LOAD = ("import sys, importlib.util\n"
        "s = importlib.util.spec_from_file_location('m', sys.argv[1] + '/calc.py')\n"
        "m = importlib.util.module_from_spec(s); s.loader.exec_module(m)\n")
ADD = LOAD + "if m.add(2, 3) != 5:\n    print('wrong_sum'); sys.exit(1)\n"
MUL = LOAD + "if m.mul(2, 3) != 6:\n    print('wrong_product'); sys.exit(1)\n"
CHECKS = [("chk-add", ["C1"], ADD, ["wrong_sum"], "H1", 30),
          ("chk-mul", ["C2"], MUL, ["wrong_product"], None, 30)]


@pytest.fixture
def calc(tmp_path):
    path = tmp_path / "main"
    base = make_repo(path, {"calc.py": b"def add(a, b):\n    return a - b\n\n\ndef mul(a, b):\n    return 0\n",
                            "test_calc.py": b"from calc import add\nassert add(2, 3) == 5\n"})
    src = (path / "calc.py").read_bytes()
    fix_add = diff_for(path, {"calc.py": src.replace(b"a - b", b"a + b")})
    fix_mul = diff_for(path, {"calc.py": src.replace(b"return 0", b"return a * b")})
    return path, base, fix_add, fix_mul


def _accept(tmp_path, calc, patches, checks=CHECKS, manual=(), criteria=("C1", "C2"), **kw):
    path, base = calc[0], calc[1]
    reg = make_registry(tmp_path / "validators", checks, manual)
    env = make_envelope(path, reg, criteria, [c[0] for c in checks])
    cand = ev.integrate(path, base, _results(tmp_path / "arts", path, base, patches), tmp_path / "cand")
    return cand, reg, env, ev.evaluate_acceptance(cand, reg, env, tmp_path / "out", **kw)


def test_acceptance_two_patches_pass_protected_checks(tmp_path, calc):
    cand, _, env, facts = _accept(tmp_path, calc, [calc[2], calc[3]])
    assert facts.accepted and facts.satisfied_criteria == ("C1", "C2")
    assert facts.candidate_sha256 == ev.candidate_identity(cand.tree) == cand.candidate_sha256
    assert [c["status"] for c in facts.checks] == ["pass", "pass"]
    assert ev.candidate_identity(cand.tree) == cand.candidate_sha256  # validators did not mutate it


def test_acceptance_failure_code_and_hypothesis_transition(tmp_path, calc):
    _, _, _, facts = _accept(tmp_path, calc, [calc[3]])
    assert not facts.accepted and facts.satisfied_criteria == ("C2",)
    assert facts.checks[0]["failure_code"] == "wrong_sum"
    assert facts.hypothesis_transitions == ({"hypothesis_id": "H1", "check_id": "chk-add",
                                             "from": "unresolved", "to": "falsified"},)


def test_unlisted_output_is_not_a_failure_code(tmp_path, calc):
    noisy = LOAD + "print('arbitrary prose'); sys.exit(1)\n"
    _, _, _, facts = _accept(tmp_path, calc, [], checks=[("chk-add", ["C1"], noisy, ["wrong_sum"], None, 30)],
                             criteria=("C1",))
    assert facts.checks[0]["failure_code"] == "unclassified_failure" and not facts.accepted


def test_worker_modified_candidate_tests_cannot_satisfy(tmp_path, calc):
    path = calc[0]
    cheat = diff_for(path, {"test_calc.py": b"assert True\n"})
    _, _, _, facts = _accept(tmp_path, calc, [cheat, calc[3]])
    assert not facts.accepted and "C1" not in facts.satisfied_criteria


def test_manual_criterion_needs_operator_evidence(tmp_path, calc):
    args = dict(manual=["CM"], criteria=("C1", "C2", "CM"))
    _, _, _, facts = _accept(tmp_path / "a", calc, [calc[2], calc[3]], **args)
    assert not facts.accepted and "CM" not in facts.satisfied_criteria
    _, _, _, ok = _accept(tmp_path / "b", calc, [calc[2], calc[3]],
                          manual_evidence={"CM": "e" * 64}, **args)
    assert ok.accepted and "CM" in ok.satisfied_criteria


def test_manual_evidence_cannot_override_a_failing_designated_check(tmp_path, calc):
    _, _, _, facts = _accept(tmp_path, calc, [calc[3]], manual=["C1"],
                              manual_evidence={"C1": "e" * 64})
    assert facts.accepted is False and "C1" not in facts.satisfied_criteria


def test_registry_preflight_is_pure_and_pins_scripts_before_any_dispatch(tmp_path, calc):
    reg = make_registry(tmp_path / "validators", CHECKS)
    env = make_envelope(calc[0], reg, ("C1", "C2"), [c[0] for c in CHECKS])
    boundary = tmp_path / "artifacts"
    meta = ev.preflight_registry(reg, env, boundary)
    assert meta["registry_sha256"] == env.validator_digest and not boundary.exists()
    (tmp_path / "validators" / "chk-add.py").write_text("raise SystemExit(0)\n")
    with pytest.raises(ev.EvidenceError) as err:
        ev.preflight_registry(reg, env, boundary)
    assert err.value.code == "validator_changed" and not boundary.exists()


def test_expired_goal_cannot_start_any_designated_check(tmp_path, calc, monkeypatch):
    monkeypatch.setattr(ev, "_run_contained", lambda *a, **k: pytest.fail("late validator launch"))
    _, _, _, facts = _accept(tmp_path, calc, [calc[2], calc[3]],
                              deadline=1.0, clock=lambda: 2.0)
    assert facts.invalid_reason == "deadline" and facts.accepted is False
    assert not (tmp_path / "out").exists()


def test_lost_goal_owner_cannot_start_any_designated_check(tmp_path, calc, monkeypatch):
    monkeypatch.setattr(ev, "_run_contained", lambda *a, **k: pytest.fail("stale validator launch"))
    _, _, _, facts = _accept(tmp_path, calc, [calc[2], calc[3]], owner_guard=lambda: False)
    assert facts.invalid_reason == "owner_lost" and not facts.accepted
    assert not (tmp_path / "out").exists()


def test_deadline_between_checks_prevents_the_next_command(tmp_path, calc):
    checks = [("chk-add", ["C1"], "print('synthetic completed', flush=True)\n", [], None, 30),
              CHECKS[1]]
    clock = lambda: 2.0 if ((tmp_path / "out" / "chk-add.out").exists()
                             and (tmp_path / "out" / "chk-add.out").stat().st_size) else 0.0
    _, _, _, facts = _accept(tmp_path, calc, [calc[2], calc[3]], checks=checks,
                              deadline=1.0, clock=clock)
    assert facts.invalid_reason == "deadline" and not facts.accepted
    assert not (tmp_path / "out" / "chk-mul.out").exists()


def test_validator_cleanup_also_caps_termination_to_remaining_grace(tmp_path, monkeypatch):
    clock = type("Clock", (), {"t": 0.0, "__call__": lambda self: self.t})()
    requested = []

    class Child:
        pid, birth = 123, 456
        def close(self): pass

    class Job:
        @classmethod
        def create(cls, name): return cls()
        def create_suspended(self, *a, **kw):
            clock.t = 60.9  # delayed startup leaves 0.1 seconds before deadline+60
            return Child()
        def is_empty(self): return False
        def terminate(self, guard, timeout):
            assert guard()
            requested.append(timeout)
            return {"outcome": "unknown"}
        def close(self): pass

    monkeypatch.setattr(ev, "WindowsJob", Job)
    result, quiet = ev._run_contained(["synthetic"], str(tmp_path), "check", 30,
                                       deadline=1.0, clock=clock, owner_guard=lambda: True)
    assert result == "deadline" and quiet is False
    assert len(requested) == 1 and 0 < requested[0] <= 0.100001


def test_tampered_validator_or_registry_rejected(tmp_path, calc):
    path, base = calc[0], calc[1]
    reg = make_registry(tmp_path / "validators", CHECKS)
    env = make_envelope(path, reg, ("C1", "C2"), ["chk-add", "chk-mul"])
    cand = ev.integrate(path, base, [], tmp_path / "cand")
    with pytest.raises(ev.EvidenceError) as err:
        ev.evaluate_acceptance(cand, reg.replace(b'"timeout_seconds":30', b'"timeout_seconds":31'),
                               env, tmp_path / "o1")
    assert err.value.code == "registry_digest_mismatch"
    (tmp_path / "validators" / "chk-add.py").write_text("print('ok')\n", encoding="utf-8")
    with pytest.raises(ev.EvidenceError) as err:
        ev.evaluate_acceptance(cand, reg, env, tmp_path / "o2")
    assert err.value.code == "validator_changed"


def _reg_code(tmp_path, calc, mutate, criteria=("C1", "C2"), vdir=None):
    path, base = calc[0], calc[1]
    reg = contract.strict_json(make_registry(vdir or tmp_path / "validators", CHECKS))
    mutate(reg)
    raw = contract.canonical(reg)
    env = make_envelope(path, raw, criteria, ["chk-add", "chk-mul"])
    cand = ev.integrate(path, base, [], tmp_path / "cand")
    with pytest.raises(ev.EvidenceError) as err:
        ev.evaluate_acceptance(cand, raw, env, tmp_path / "out")
    assert not (tmp_path / "out").exists()
    return err.value.code


def _set(key, value, i=0):
    return lambda reg: reg["checks"][i].__setitem__(key, value)


@pytest.mark.parametrize("mutate", [
    lambda r: r.__setitem__("schema_version", True),
    lambda r: r["checks"].append(dict(r["checks"][0])),
    lambda r: r.__setitem__("manual_criteria", [["C1"]]),
    _set("criteria", [{"x": 1}]), _set("criteria", []), _set("criteria", ["C1", "C1"]),
    _set("script", 5), _set("script", "../escape.py"), _set("script", "missing.py"),
    _set("script_sha256", "XYZ"), _set("failure_codes", [["a"]]), _set("failure_codes", "wrong_sum"),
    _set("failure_codes", ["timeout"]), _set("failure_codes", ["unclassified_failure"]),
    _set("falsifies", ["H1"]), _set("timeout_seconds", True),
    lambda r: r.__setitem__("checks", r["checks"][:1]),  # C2 left with zero coverage
])
def test_registry_strict_schema_is_fixed_error(tmp_path, calc, mutate):
    assert _reg_code(tmp_path, calc, mutate) == "registry_malformed"


def test_validators_inside_project_rejected(tmp_path, calc):
    assert _reg_code(tmp_path, calc, lambda r: None, vdir=calc[0] / "tools" / "v") == "registry_untrusted"


def test_candidate_mutation_during_verification_invalidates(tmp_path, calc):
    mutating = LOAD + "open(sys.argv[1] + '/calc.py', 'a').write('# x\\n')\n"
    _, _, _, facts = _accept(tmp_path, calc, [calc[2], calc[3]], criteria=("C1",),
                             checks=[("chk-add", ["C1"], mutating, [], None, 30)])
    assert not facts.accepted and facts.invalid_reason == "candidate_mutated"
    assert facts.satisfied_criteria == ()


def test_candidate_changed_after_integration_invalidates(tmp_path, calc):
    path, base = calc[0], calc[1]
    reg = make_registry(tmp_path / "validators", CHECKS)
    env = make_envelope(path, reg, ("C1", "C2"), ["chk-add", "chk-mul"])
    cand = ev.integrate(path, base, _results(tmp_path / "a", path, base, [calc[2], calc[3]]),
                        tmp_path / "cand")
    with open(os.path.join(cand.tree, "calc.py"), "ab") as fh:
        fh.write(b"# late\n")
    facts = ev.evaluate_acceptance(cand, reg, env, tmp_path / "out")
    assert not facts.accepted and facts.invalid_reason == "candidate_mutated"


def test_conflicted_candidate_is_unmet(tmp_path, calc):
    path = calc[0]
    other = diff_for(path, {"calc.py": (path / "calc.py").read_bytes().replace(b"a - b", b"b + a")})
    _, _, _, facts = _accept(tmp_path, calc, [calc[2], other])
    assert not facts.accepted and facts.invalid_reason == "conflict"


WIN = pytest.mark.skipif(os.name != "nt", reason="Windows Job containment only; POSIX fails closed")
# Grandchild appends to a heartbeat file while alive; growth after return proves a survivor.
SPAWN = ("import subprocess, sys, os\nhb = os.path.abspath('hb.txt')\n"
         "subprocess.Popen([sys.executable, '-c', 'import time\\nwhile True:\\n"
         "    open(%r, \"a\").write(\"x\"); time.sleep(0.05)' % hb])\nimport time; time.sleep(0.5)\n")


def _no_heartbeat(out):
    import time
    hb = out / "hb.txt"
    first = hb.stat().st_size if hb.exists() else 0
    time.sleep(0.6)
    return (hb.stat().st_size if hb.exists() else 0) == first


@WIN
@pytest.mark.parametrize("tail,timeout,code", [("", 30, "survivors"),
                                               ("time.sleep(60)\n", 2, "timeout")])
def test_validator_descendants_are_contained(tmp_path, calc, tail, timeout, code):
    import subprocess as sp
    bystander = sp.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        _, _, _, facts = _accept(tmp_path, calc, [calc[2]], criteria=("C1",),
                                 checks=[("chk-add", ["C1"], SPAWN + tail, [], None, timeout)])
        assert facts.checks[0] == {"check_id": "chk-add", "status": "fail", "failure_code": code}
        assert not facts.accepted and _no_heartbeat(tmp_path / "out")
        assert bystander.poll() is None
    finally:
        bystander.kill()


@WIN
def test_validator_output_is_capped(tmp_path, calc, monkeypatch):
    monkeypatch.setattr(ev, "OUTPUT_CAP", 64 * 1024)
    flood = "import sys\nwhile True:\n    sys.stdout.write('y' * 4096); sys.stdout.flush()\n"
    _, _, _, facts = _accept(tmp_path, calc, [calc[2]], criteria=("C1",),
                             checks=[("chk-add", ["C1"], flood, [], None, 30)])
    assert facts.checks[0]["failure_code"] == "output_limit" and not facts.accepted
    assert (tmp_path / "out" / "chk-add.out").stat().st_size <= 64 * 1024 + (1 << 20)


@WIN
def test_validator_env_is_sanitized(tmp_path, calc, monkeypatch):
    monkeypatch.setenv("GATEWAY_SECRET_CANARY", "synthetic-canary")
    probe = "import os, sys\nif 'GATEWAY_SECRET_CANARY' in os.environ:\n    print('leak'); sys.exit(1)\n"
    _, _, _, facts = _accept(tmp_path, calc, [calc[2]], criteria=("C1",),
                             checks=[("chk-add", ["C1"], probe, ["leak"], None, 30)])
    assert facts.checks[0]["status"] == "pass" and facts.accepted


def test_containment_unavailable_fails_closed(tmp_path, calc, monkeypatch):
    def refuse(name):
        raise ev.ProcessError("unsupported")
    monkeypatch.setattr(ev.WindowsJob, "create", classmethod(lambda cls, name: refuse(name)))
    _, _, _, facts = _accept(tmp_path, calc, [calc[2]], criteria=("C1",), checks=CHECKS[:1])
    assert facts.checks[0]["failure_code"] == "containment_failed"
    assert not facts.accepted and facts.invalid_reason == "containment_unavailable"


def test_timeout_is_fixed_classification(tmp_path, calc):
    slow = "import time\ntime.sleep(30)\n"
    _, _, _, facts = _accept(tmp_path, calc, [], checks=[("chk-add", ["C1"], slow, [], None, 1)],
                             criteria=("C1",))
    assert facts.checks[0]["status"] == "fail" and facts.checks[0]["failure_code"] == "timeout"

"""Tests for engine.harness_skills (synthetic tmp_path roots only; never the real home)."""
import os

import pytest

from engine import harness_skills as hs
from engine.harness_skills import SkillRegistry, validate_skills_dirs


def _skill(base, folder, name=None, desc="A skill.", body="Body text\n", raw=None):
    d = base / folder
    d.mkdir(parents=True, exist_ok=True)
    if raw is None:
        raw = "---\nname: %s\ndescription: %s\n---\n%s" % (name or folder, desc, body)
    (d / "SKILL.md").write_text(raw, encoding="utf-8")
    return d


@pytest.fixture
def env(tmp_path):
    root, home, extra = tmp_path / "repo", tmp_path / "home", tmp_path / "extra"
    for p in (root, home, extra):
        p.mkdir()
    return root, home, extra


def rs(root):
    return root / ".claude" / "skills"


def _symlink(target, link, is_dir=False):
    try:
        os.symlink(str(target), str(link), target_is_directory=is_dir)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable (e.g. Windows without privilege)")


# ---------------------------------------------------------------- validate
def test_validate_skills_dirs(tmp_path, monkeypatch):
    assert validate_skills_dirs(None) == []
    assert validate_skills_dirs([str(tmp_path)]) == [os.path.normpath(str(tmp_path))]
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    out = validate_skills_dirs(["~/x"])
    assert os.path.isabs(out[0]) and out[0].endswith("x")
    for bad in ("abc", 5, {"a": 1}, [5], [None], [True]):
        with pytest.raises((TypeError, ValueError)):
            validate_skills_dirs(bad)
    for bad in ([""], ["   "], ["relative/dir"], ["~someoneelse/x"], [str(tmp_path) + "\x00"]):
        with pytest.raises(ValueError):
            validate_skills_dirs(bad)


# ---------------------------------------------------------------- discovery
def test_discovery_repo_home_extra_sorted(env):
    root, home, extra = env
    _skill(rs(root), "zeta", desc="Repo skill")
    _skill(rs(home), "alpha", desc="Home skill")
    _skill(extra, "mid", desc="Extra skill")
    (extra / "nofile").mkdir()  # folder without SKILL.md ignored
    reg = SkillRegistry(root, skills_dirs=[str(extra)], home=home)
    assert reg.catalog() == [
        {"name": "alpha", "description": "Home skill"},
        {"name": "mid", "description": "Extra skill"},
        {"name": "zeta", "description": "Repo skill"},
    ]
    assert reg.loaded == []


def test_missing_roots_no_skills(tmp_path):
    reg = SkillRegistry(tmp_path / "nope", skills_dirs=[str(tmp_path / "gone")], home=tmp_path / "nohome")
    assert reg.catalog() == []


def test_same_root_as_home_not_duplicate(env):
    root, _, _ = env
    _skill(rs(root), "one")
    assert [c["name"] for c in SkillRegistry(root, home=root).catalog()] == ["one"]


def test_discovery_does_not_read_body(env, monkeypatch):
    root, home, _ = env
    _skill(rs(root), "big", body="x" * 300_000 + "\nSECRET_BODY\n")
    calls = []
    real = hs._read_body
    monkeypatch.setattr(hs, "_read_body", lambda p: calls.append(p) or real(p))
    reg = SkillRegistry(root, home=home)
    assert reg.catalog() == [{"name": "big", "description": "A skill."}]
    assert calls == []
    assert "SECRET_BODY" not in repr(reg.__dict__)
    with pytest.raises(ValueError, match="too large|exceeds"):
        reg.load("big")
    assert reg.loaded == []


def test_frontmatter_forms(env):
    root, home, _ = env
    base = rs(root)
    _skill(base, "dq", raw='\ufeff---\nname: "dq"\ndescription: "Double \\"quoted\\" text"\n---\nb')
    _skill(base, "sq", raw="---\r\nname: 'sq'\r\ndescription: 'It''s single'\r\n---\r\nb")
    _skill(base, "fold", raw="---\nname: fold\ndescription: >\n  First line\n  second line\n\nlicense: MIT\n---\nb")
    _skill(base, "lit", raw="---\ndescription: |-\n    Line one\n    Line two\nname: lit\nmetadata:\n  k: v\n---\nb")
    _skill(base, "plain", raw="---\nname: plain\ndescription: Plain value\n  continued here\n---\nb")
    cat = {c["name"]: c["description"] for c in SkillRegistry(root, home=home).catalog()}
    assert cat == {
        "dq": 'Double "quoted" text',
        "sq": "It's single",
        "fold": "First line second line",
        "lit": "Line one Line two",
        "plain": "Plain value continued here",
    }


def test_duplicate_names_fail_deterministically(env):
    root, home, extra = env
    _skill(rs(root), "dup")
    _skill(rs(home), "dup")
    with pytest.raises(ValueError, match="duplicate skill name 'dup'"):
        SkillRegistry(root, home=home)
    # same name declared by two folders in one root
    _skill(extra, "a1", name="same")
    _skill(extra, "a2", name="same")
    with pytest.raises(ValueError, match="duplicate"):
        SkillRegistry(root / "none", skills_dirs=[str(extra)], home=root / "none2")


def test_ambiguous_name_can_be_refused_without_hiding_unrelated_skills(env):
    import inspect
    assert "strict_duplicates" in inspect.signature(SkillRegistry).parameters
    root, home, _ = env
    _skill(rs(root), "valid", body="VALID")
    _skill(rs(root), "duplicate-one", name="ambiguous")
    _skill(rs(home), "duplicate-two", name="ambiguous")
    _skill(rs(home), "duplicate-three", name="ambiguous")
    reg = SkillRegistry(root, home=home, strict_duplicates=False)
    assert [row["name"] for row in reg.catalog()] == ["valid"]
    assert "VALID" in reg.load("valid")
    assert any("duplicate skill name" in warning for warning in reg.warnings)
    with pytest.raises(ValueError, match="refused: duplicate skill name"):
        reg.load("ambiguous")
    with pytest.raises(ValueError):
        reg.resolve_read("skill://ambiguous/SKILL.md")


@pytest.mark.parametrize("raw", [
    "no frontmatter\n",
    "---\nname: x\ndescription: d\n",                      # unclosed
    "---\nname: x\n---\nb",                                 # missing description
    "---\ndescription: d\n---\nb",                          # missing name
    "---\nname: x\nname: x\ndescription: d\n---\nb",        # duplicate field
    "---\nname: -bad\ndescription: d\n---\nb",              # bad name
    "---\nname: a/b\ndescription: d\n---\nb",
    "---\nname: " + "a" * 81 + "\ndescription: d\n---\nb",
    "---\nname: x\ndescription: \"unterminated\n---\nb",
    "---\nname: x\ndescription: ''\n---\nb",                # empty description
    "---\nname: x\ndescription: d\n" + "k: v\n" * 20000 + "---\nb",  # oversize header
    "---\nname: x\ndescription: " + "d" * 2000 + "\n---\nb",  # oversize description
    "---\nname: x\nthis is not a mapping\ndescription: d\n---\nb",
], ids=lambda r: "len%d" % len(r))
def test_malformed_frontmatter_rejected(env, raw):
    root, home, _ = env
    _skill(rs(root), "x", raw=raw)
    with pytest.raises(ValueError):
        SkillRegistry(root, home=home)


def test_load_and_repeat(env):
    root, home, _ = env
    d = _skill(rs(root), "pdf", desc="PDF tools", body="Use scripts.\nSee ref/guide.md\n")
    (d / "ref").mkdir()
    (d / "ref" / "guide.md").write_text("guide", encoding="utf-8")
    reg = SkillRegistry(root, home=home)
    text = reg.load("pdf")
    assert "Use scripts." in text and "name: pdf" not in text
    assert "skill://pdf/" in text and "read_file" in text
    assert reg.loaded == ["pdf"]
    again = reg.load("pdf")
    assert "Use scripts." not in again and "already loaded" in again
    assert reg.loaded == ["pdf"]
    assert all("Use scripts" not in str(c) for c in reg.catalog())
    for bad in ("nope", "", "../pdf", None):
        with pytest.raises(ValueError, match="^refused: "):
            reg.load(bad)


def test_resolve_read_only_loaded(env):
    root, home, _ = env
    d = _skill(rs(root), "pdf")
    _skill(rs(root), "other")
    (d / "ref" / "deep").mkdir(parents=True)
    (d / "ref" / "deep" / "g.md").write_text("g", encoding="utf-8")
    reg = SkillRegistry(root, home=home)
    with pytest.raises(ValueError):
        reg.resolve_read("skill://pdf/SKILL.md")  # not loaded yet
    reg.load("pdf")
    canon = os.path.realpath(str(d))
    assert reg.resolve_read("skill://pdf/SKILL.md") == os.path.join(canon, "SKILL.md")
    assert reg.resolve_read("skill://pdf/ref/deep/g.md") == os.path.join(canon, "ref", "deep", "g.md")
    with pytest.raises(ValueError):
        reg.resolve_read("skill://other/SKILL.md")
    with pytest.raises(ValueError):
        reg.resolve_read("skill://unknown/SKILL.md")


@pytest.mark.parametrize("ref", [
    "skill://pdf/../other/SKILL.md", "skill://pdf/ref/../../other/SKILL.md",
    "skill://pdf/..", "skill://pdf/", "skill://pdf", "skill:///SKILL.md", "skill://",
    "skill://pdf//etc/passwd", "skill://pdf/ref\\..\\..\\x", "skill://pdf/C:/Windows/x",
    "skill://pdf/C:x", "skill://pdf/\\\\server\\share\\x", "skill://pdf//server/share",
    "skill://pdf/./SKILL.md", "skill://pdf/ref/nul\x00", "pdf/SKILL.md", "/abs/SKILL.md",
    "skill://pdf/missing.md", "skill://pdf/ref", 5, None,
])
def test_resolve_read_rejects(env, ref):
    root, home, _ = env
    d = _skill(rs(root), "pdf")
    _skill(rs(root), "other")
    (d / "ref").mkdir()
    reg = SkillRegistry(root, home=home)
    reg.load("pdf")
    with pytest.raises(ValueError):
        reg.resolve_read(ref)


def test_symlinked_resource_escape_refused(env, tmp_path):
    root, home, _ = env
    d = _skill(rs(root), "pdf")
    secret = tmp_path / "secret"
    secret.mkdir()
    (secret / "s.txt").write_text("s", encoding="utf-8")
    _symlink(secret / "s.txt", d / "leak.txt")
    _symlink(secret, d / "leakdir", is_dir=True)
    reg = SkillRegistry(root, home=home)
    reg.load("pdf")
    with pytest.raises(ValueError):
        reg.resolve_read("skill://pdf/leak.txt")
    with pytest.raises(ValueError):
        reg.resolve_read("skill://pdf/leakdir/s.txt")


def test_symlinked_skill_md_outside_folder_rejected(env, tmp_path):
    root, home, _ = env
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "SKILL.md").write_text("---\nname: evil\ndescription: d\n---\nb", encoding="utf-8")
    (rs(root) / "evil").mkdir(parents=True)
    _symlink(outside / "SKILL.md", rs(root) / "evil" / "SKILL.md")
    reg = SkillRegistry(root, home=home)          # skipped, never granted
    assert reg.catalog() == [] and any("escape" in w or "outside" in w for w in reg.warnings)
    with pytest.raises(ValueError, match="unknown"):
        reg.load("evil")


def test_symlinked_skill_folder_escape_rejected(env, tmp_path):
    root, home, _ = env
    elsewhere = tmp_path / "elsewhere"
    _skill(elsewhere, "evil")
    rs(root).mkdir(parents=True)
    _symlink(elsewhere / "evil", rs(root) / "evil", is_dir=True)
    reg = SkillRegistry(root, home=home)
    assert reg.catalog() == [] and any("escape" in w for w in reg.warnings)
    with pytest.raises(ValueError, match="unknown"):
        reg.load("evil")
    with pytest.raises(ValueError):
        reg.resolve_read("skill://evil/SKILL.md")


def test_folder_substitution_detected(env, tmp_path):
    root, home, _ = env
    d = _skill(rs(root), "pdf")
    reg = SkillRegistry(root, home=home)
    moved = tmp_path / "moved"
    os.rename(str(d), str(moved))
    _skill(rs(root), "pdf", desc="impostor")
    with pytest.raises(ValueError, match="changed|substitut"):
        reg.load("pdf")


def test_folder_substitution_after_load_detected(env, tmp_path):
    root, home, _ = env
    d = _skill(rs(root), "pdf")
    reg = SkillRegistry(root, home=home)
    reg.load("pdf")
    os.rename(str(d), str(tmp_path / "moved2"))
    _skill(rs(root), "pdf")
    with pytest.raises(ValueError, match="changed|substitut"):
        reg.resolve_read("skill://pdf/SKILL.md")


def test_no_write_authority_and_limits(env):
    root, home, _ = env
    _skill(rs(root), "pdf")
    reg = SkillRegistry(root, home=home)
    for attr in ("write", "write_file", "resolve_write", "search", "run", "image"):
        assert not hasattr(reg, attr)
    assert hs.MAX_BODY_CHARS <= 100_000
    assert hs.MAX_SKILLS >= 1 and hs.MAX_HEADER_BYTES <= 64 * 1024


def test_escaping_home_link_skipped_valid_repo_skill_loads(env, tmp_path, capsys):
    root, home, _ = env
    _skill(rs(root), "good", body="GOOD-BODY\n")
    (rs(root) / "good" / "ref.md").write_text("REF", encoding="utf-8")
    elsewhere = tmp_path / "elsewhere"
    _skill(elsewhere, "evil", body="EVIL-BODY\n")
    (elsewhere / "evil" / "secret.txt").write_text("SECRET", encoding="utf-8")
    rs(home).mkdir(parents=True)
    _symlink(elsewhere / "evil", rs(home) / "evil", is_dir=True)
    reg = SkillRegistry(root, home=home)
    assert [c["name"] for c in reg.catalog()] == ["good"]
    assert any("evil" in w and "skipped" in w for w in reg.warnings)
    assert all("EVIL-BODY" not in w for w in reg.warnings)
    assert "GOOD-BODY" in reg.load("good")
    assert reg.resolve_read("skill://good/ref.md").endswith("ref.md")
    with pytest.raises(ValueError, match="unknown"):
        reg.load("evil")
    for ref in ("skill://evil/secret.txt", "skill://good/../evil/secret.txt"):
        with pytest.raises(ValueError):
            reg.resolve_read(ref)


def test_escaping_skill_md_link_skipped(env, tmp_path):
    root, home, _ = env
    _skill(rs(root), "good")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "SKILL.md").write_text("---\nname: evil\ndescription: d\n---\nb", encoding="utf-8")
    (rs(root) / "evil").mkdir(parents=True)
    _symlink(outside / "SKILL.md", rs(root) / "evil" / "SKILL.md")
    reg = SkillRegistry(root, home=home)
    assert [c["name"] for c in reg.catalog()] == ["good"] and reg.warnings
    with pytest.raises(ValueError):
        reg.load("evil")


def test_policy_dir_authorizes_link_target_and_aliases_dedup(env, tmp_path):
    root, home, extra = env
    _skill(extra, "shared", body="SHARED\n")
    rs(home).mkdir(parents=True)
    _symlink(extra / "shared", rs(home) / "shared", is_dir=True)
    reg = SkillRegistry(root, skills_dirs=[str(extra)], home=home)
    assert [c["name"] for c in reg.catalog()] == ["shared"]
    assert "SHARED" in reg.load("shared")
    # two different physical folders with the same name still fail clearly
    _skill(rs(root), "dupe", name="shared")
    with pytest.raises(ValueError, match="duplicate skill name 'shared'"):
        SkillRegistry(root, skills_dirs=[str(extra)], home=home)


def test_malformed_still_fails_with_link_present(env, tmp_path):
    root, home, _ = env
    _skill(rs(root), "bad", raw="no frontmatter\n")
    elsewhere = tmp_path / "elsewhere"
    _skill(elsewhere, "evil")
    rs(home).mkdir(parents=True)
    _symlink(elsewhere / "evil", rs(home) / "evil", is_dir=True)
    with pytest.raises(ValueError, match="frontmatter"):
        SkillRegistry(root, home=home)


def test_load_max_chars_refuses_without_marking_loaded(env):
    root, home, _ = env
    _skill(rs(root), "long", body="x" * 500 + "TAIL\n")
    reg = SkillRegistry(root, home=home)
    with pytest.raises(ValueError, match="exceed|too large"):
        reg.load("long", max_chars=100)
    assert reg.loaded == []
    assert "TAIL" in reg.load("long", max_chars=10_000)


def test_too_many_skills_rejected(env, monkeypatch):
    root, home, _ = env
    monkeypatch.setattr(hs, "MAX_SKILLS", 2)
    for n in ("a", "b", "c"):
        _skill(rs(root), n)
    with pytest.raises(ValueError, match="too many"):
        SkillRegistry(root, home=home)

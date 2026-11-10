import hashlib
import importlib.util
from pathlib import Path

import pytest

KIT = Path(__file__).resolve().parents[1] / "tools/kilo/owner-patchkit"


def load_kit():
    source = KIT / "prepare.py"
    assert source.exists(), "version-checked Kilo owner patch preparation is missing"
    spec = importlib.util.spec_from_file_location("synthetic_owner_patch", source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_anchor_drift_is_rejected_instead_of_partially_patching():
    kit = load_kit()
    assert kit.replace_once("synthetic old", "old", "new") == "synthetic new"
    with pytest.raises(ValueError):
        kit.replace_once("synthetic", "old", "new")
    with pytest.raises(ValueError):
        kit.replace_once("old old", "old", "new")


def test_git_blob_identity_is_verified_before_any_write(tmp_path):
    kit = load_kit()
    value = b"synthetic source\n"
    expected = hashlib.sha1(b"blob " + str(len(value)).encode() + b"\0" + value).hexdigest()
    kit.verify_sources({"synthetic.ts": value}, {"synthetic.ts": expected})
    with pytest.raises(ValueError):
        kit.verify_sources({"synthetic.ts": value + b"drift"}, {"synthetic.ts": expected})


def test_bad_local_source_leaves_inputs_and_output_untouched(tmp_path, monkeypatch):
    kit = load_kit()
    monkeypatch.setattr(kit, "STAGING_ROOT", tmp_path)
    source = tmp_path / "synthetic-source"
    source.mkdir()
    sentinel = source / "synthetic.txt"
    sentinel.write_text("synthetic source", encoding="utf-8")
    output = tmp_path / "prepared"
    with pytest.raises((ValueError, OSError)):
        kit.prepare(output, source=source)
    assert not output.exists()
    assert sentinel.read_text(encoding="utf-8") == "synthetic source"


def test_existing_output_is_never_replaced(tmp_path):
    kit = load_kit()
    output = tmp_path / "existing"
    output.mkdir()
    with pytest.raises(ValueError):
        kit.prepare(output, source=tmp_path / "missing")
    assert output.is_dir()


def test_manifest_registers_the_exact_maintained_patch_fingerprint():
    kit = load_kit()
    assert kit.MANIFEST["patch_id"] == kit.patch_id()

import importlib.util
from pathlib import Path


def module():
    path = Path(__file__).resolve().parents[1] / "tools/kilo/owner-patchkit/isolated.py"
    assert path.exists(), "isolated full-build environment is missing"
    spec = importlib.util.spec_from_file_location("synthetic_build_env", path)
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


def test_build_environment_does_not_inherit_credentials_or_release_publishing(tmp_path):
    m = module()
    compiler = tmp_path / "compiler/bun.exe"
    env = m.environment(tmp_path, compiler, {"PATH": "synthetic-path", "SystemRoot": "synthetic-windows",
        "OPENAI_API_KEY": "SYNTHETIC_PRIVATE", "KILO_SERVER_PASSWORD": "SYNTHETIC_PRIVATE",
        "KILO_RELEASE": "1", "KILO_BUMP": "patch", "GH_TOKEN": "SYNTHETIC_PRIVATE"})
    assert "SYNTHETIC_PRIVATE" not in str(env)
    assert "KILO_RELEASE" not in env and "KILO_BUMP" not in env
    assert env["KILO_CHANNEL"] == "owner-attribution"
    assert env["PATH"].startswith(str(compiler.parent))
    for field in ("HOME", "USERPROFILE", "XDG_DATA_HOME", "XDG_CONFIG_HOME", "XDG_CACHE_HOME", "TEMP"):
        assert Path(env[field]).is_relative_to(tmp_path)

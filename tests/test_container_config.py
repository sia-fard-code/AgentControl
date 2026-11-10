from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest


@pytest.fixture
def deployment(tmp_path: Path) -> Path:
    root = Path(__file__).resolve().parents[1]
    for name in (
        ".dockerignore",
        "server/Dockerfile.agent-control",
        "server/compose.agent-control.yml",
    ):
        target = tmp_path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(root / name, target)
    return tmp_path


def test_image_uses_nonroot_and_explicit_runtime_paths(deployment: Path) -> None:
    dockerfile = (deployment / "server/Dockerfile.agent-control").read_text()
    lines = dockerfile.splitlines()
    entrypoint = next(line for line in lines if line.startswith("ENTRYPOINT "))
    assert json.loads(entrypoint.removeprefix("ENTRYPOINT ")) == [
        "agent-control",
        "--state-dir", "/var/lib/agent-control",
        "--machine-index", "/sources/agent-runs-index.jsonl",
        "--pricing", "/app/config/pricing.json",
        "--web-dir", "/app/web",
    ]
    assert 'CMD ["serve", "--port", "8787"]' in lines
    assert "USER 10001:10001" in lines
    assert "chown 10001:10001 /var/lib/agent-control" in dockerfile
    assert "chmod 0700 /var/lib/agent-control" in dockerfile
    assert [line for line in lines if line.startswith("COPY ")] == [
        "COPY pyproject.toml ./",
        "COPY src/agent_control/*.py ./src/agent_control/",
        "COPY src/harness_process_identity.py ./src/",
        "COPY web/ ./web/",
        "COPY config/pricing.json ./config/pricing.json",
    ]


def test_build_context_is_a_narrow_allowlist(deployment: Path) -> None:
    rules = [
        line.strip()
        for line in (deployment / ".dockerignore").read_text().splitlines()
        if line.strip() and not line.startswith("#")
    ]
    assert rules == [
        "**",
        "!pyproject.toml",
        "!src/",
        "src/**",
        "!src/harness_process_identity.py",
        "!src/agent_control/",
        "src/agent_control/**",
        "!src/agent_control/*.py",
        "!web/",
        "web/**",
        "!web/index.html",
        "!web/package.json",
        "!web/*.js",
        "!web/*.css",
        "!config/",
        "config/**",
        "!config/pricing.json",
        "!server/",
        "server/**",
        "!server/Dockerfile.agent-control",
    ]


def test_compose_preserves_loopback_and_state_isolation(deployment: Path) -> None:
    docker = shutil.which("docker")
    if docker is None:
        pytest.skip("Docker Compose CLI is not installed")
    version = subprocess.run(
        [docker, "compose", "version"], capture_output=True, timeout=15,
    )
    if version.returncode:
        pytest.skip("Docker Compose plugin is not installed")
    env_file = deployment / "empty.env"
    env_file.write_text("")
    result = subprocess.run(
        [
            docker, "compose", "--env-file", str(env_file),
            "-f", str(deployment / "server/compose.agent-control.yml"),
            "config", "--format", "json",
        ],
        cwd=deployment, check=True, capture_output=True, text=True, timeout=15,
    )
    model = json.loads(result.stdout)
    assert set(model["services"]) == {"agent-control"}
    service = model["services"]["agent-control"]
    assert service["image"] == (
        "docker.io/example/agent-control:0.2.0"
        "@sha256:7326f24bb74926c319988ba504c3c31b5a628201d59d4d65b71a2be00f0bac1b"
    )
    assert "build" not in service
    assert service["network_mode"] == "host"
    assert service["read_only"] is True
    assert service["cap_drop"] == ["ALL"]
    assert service["security_opt"] == ["no-new-privileges:true"]
    assert service["init"] is True
    assert service["restart"] == "unless-stopped"
    for forbidden in (
        "ports", "networks", "labels", "pid", "privileged", "devices",
        "volumes_from", "entrypoint", "command",
    ):
        assert not service.get(forbidden), forbidden
    assert len(service["volumes"]) == 1
    volume = service["volumes"][0]
    assert volume["type"] == "volume"
    assert volume["source"] == "agent-control-data"
    assert volume["target"] == "/var/lib/agent-control"
    probe = service["healthcheck"]["test"]
    assert probe[:3] == ["CMD", "python", "-c"]
    assert "http://127.0.0.1:8787/" in probe[3]
    assert "/api/" not in probe[3]
    assert "timeout=" in probe[3]
    assert ".close()" in probe[3]

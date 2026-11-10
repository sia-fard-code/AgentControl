from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
ORIGIN = "https://agent-control.example.com"


def test_private_server_snapshots_and_credentials_are_ignored(tmp_path: Path) -> None:
    rules = (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    for pattern in (
        "/server/docker-compose-server", "/server/nginx-config",
        "/server/nginx/", "/server/.env",
    ):
        assert pattern in rules


def test_nginx_authenticates_and_preserves_security_headers(tmp_path: Path) -> None:
    section = (ROOT / "server/nginx.agent-control.conf").read_text(encoding="utf-8")
    for directive in (
        'auth_basic "AgentControl";',
        "auth_basic_user_file /etc/nginx/agent-control.htpasswd;",
        "proxy_pass http://127.0.0.1:8787;",
        "proxy_set_header Host $http_host;",
        'proxy_set_header Authorization "";',
        "proxy_http_version 1.1;",
        "proxy_buffering off;", "proxy_request_buffering off;",
        "proxy_max_temp_file_size 0;", "proxy_cache off;", "proxy_store off;",
        "client_max_body_size 16k;", "client_body_buffer_size 16k;",
        "client_body_in_file_only off;", "access_log off;",
        "ssl_protocols TLSv1.2 TLSv1.3;",
        "server_name agent-control.example.com;",
        "location /.well-known/acme-challenge/",
    ):
        assert directive in section, directive
    assert "auth_basic off" not in section
    assert "proxy_set_header Origin" not in section
    assert "proxy_set_header X-AgentControl-CSRF" not in section
    assert section.count("error_log /dev/null;") == 2


def test_proxy_overlay_shares_only_nginx_network_namespace(tmp_path: Path) -> None:
    docker = shutil.which("docker")
    if docker is None:
        pytest.skip("Docker Compose CLI is not installed")
    if subprocess.run(
        [docker, "compose", "version"], capture_output=True, timeout=15,
    ).returncode:
        pytest.skip("Docker Compose plugin is not installed")
    server = tmp_path / "server"
    server.mkdir()
    for name in ("compose.agent-control.yml", "compose.agent-control.nginx.yml"):
        shutil.copyfile(ROOT / "server" / name, server / name)
    (server / "synthetic-base.yml").write_text(
        "services:\n  nginx:\n    image: nginx:stable\n"
        "    ports: ['80:80', '443:443']\n"
        "    volumes: ['./original.conf:/etc/nginx/nginx.conf:ro']\n",
    )
    (server / "nginx-config").write_text("events {}\nhttp {}\n")
    auth = tmp_path / "synthetic-htpasswd"
    auth.write_text("synthetic-placeholder-only\n")
    env = tmp_path / "test.env"
    env.write_text(f"AGENT_CONTROL_HTPASSWD_FILE={auth}\n")
    result = subprocess.run(
        [docker, "compose", "--env-file", str(env),
         "-f", str(server / "synthetic-base.yml"),
         "-f", str(server / "compose.agent-control.nginx.yml"),
         "config", "--format", "json"],
        cwd=tmp_path, capture_output=True, text=True, check=True, timeout=15,
    )
    model = json.loads(result.stdout)
    assert model["name"] != "agent-control"
    app = model["services"]["agent-control"]
    assert app["network_mode"] == "service:nginx"
    assert app["command"] == ["--public-origin", ORIGIN, "serve", "--port", "8787"]
    assert app["environment"]["AGENT_CONTROL_PUBLIC_ORIGIN"] == ORIGIN
    assert app["read_only"] is True
    assert app["cap_drop"] == ["ALL"]
    assert app["security_opt"] == ["no-new-privileges:true"]
    for key in ("ports", "networks", "pid", "privileged", "build"):
        assert not app.get(key), key
    assert len(app["volumes"]) == 1
    assert app["volumes"][0]["type"] == "volume"
    assert app["volumes"][0]["target"] == "/var/lib/agent-control"
    probe = app["healthcheck"]["test"]
    assert probe[:3] == ["CMD", "python", "-c"]
    assert "Host" in probe[3] and "AGENT_CONTROL_PUBLIC_ORIGIN" in probe[3]
    nginx = model["services"]["nginx"]
    assert {port["target"] for port in nginx["ports"]} == {80, 443}
    mounts = {mount["target"]: mount for mount in nginx["volumes"]}
    config = mounts["/etc/nginx/nginx.conf"]
    assert config["source"] == str(server / "nginx-config")
    assert config["read_only"] is True
    password_file = mounts["/etc/nginx/agent-control.htpasswd"]
    assert password_file["source"] == str(auth)
    assert password_file["read_only"] is True
    assert password_file["bind"].get("create_host_path", False) is False

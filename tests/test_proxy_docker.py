from __future__ import annotations

import json
import os
import secrets
import shutil
import subprocess
from pathlib import Path

import pytest


@pytest.mark.skipif(
    os.environ.get("AGENTCONTROL_RUN_DOCKER_TESTS") != "1",
    reason="set AGENTCONTROL_RUN_DOCKER_TESTS=1 for isolated Docker HTTPS tests",
)
def test_authenticated_https_proxy_round_trip(tmp_path: Path) -> None:
    docker = shutil.which("docker")
    openssl = shutil.which("openssl")
    assert docker and openssl
    root = Path(__file__).resolve().parents[1]
    image = os.environ.get("AGENTCONTROL_TEST_IMAGE", "agent-control:proxy-test")
    section = (root / "server/nginx.agent-control.conf").read_text(encoding="utf-8")
    config = tmp_path / "nginx.conf"
    config.write_text(
        "error_log stderr debug;\nevents {}\nhttp {\n"
        + section + "\n}\n",
        encoding="utf-8",
    )
    certificate, key = tmp_path / "certificate.pem", tmp_path / "key.pem"
    subprocess.run(
        [openssl, "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
         "-subj", "/CN=agent-control.example.com",
         "-addext", "subjectAltName=DNS:agent-control.example.com,IP:127.0.0.1",
         "-keyout", str(key), "-out", str(certificate)],
        check=True, capture_output=True, timeout=30,
    )
    password = secrets.token_urlsafe(24)
    hashed = subprocess.run(
        [openssl, "passwd", "-6", "-stdin"], input=password + "\n",
        capture_output=True, text=True, check=True, timeout=15,
    ).stdout.strip()
    auth = tmp_path / "htpasswd"
    auth.write_text("synthetic-operator:" + hashed + "\n")
    auth.chmod(0o644)

    def run(*args: str, input: str | None = None) -> str:
        result = subprocess.run(
            [docker, *args], input=input, capture_output=True, text=True,
            check=True, timeout=90,
        )
        return result.stdout.strip()

    proxy = backend = None
    try:
        tls = "/etc/letsencrypt/live/agent-control.example.com/"
        proxy = run(
            "create", "--network", "none",
            "--mount", f"type=bind,source={config},target=/etc/nginx/nginx.conf,readonly",
            "--mount", f"type=bind,source={auth},target=/etc/nginx/agent-control.htpasswd,readonly",
            "--mount", f"type=bind,source={certificate},target={tls}fullchain.pem,readonly",
            "--mount", f"type=bind,source={key},target={tls}privkey.pem,readonly",
            "--entrypoint", "nginx", "nginx:latest", "-g", "daemon off;",
        )
        run("start", proxy)
        backend = run(
            "create", "--network", "container:" + proxy,
            "--read-only", "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges:true",
            "--mount", "type=volume,target=/var/lib/agent-control",
            "--mount", f"type=bind,source={certificate},target=/tmp/proxy-ca.pem,readonly",
            image, "--public-origin", "https://agent-control.example.com",
            "serve", "--port", "8787",
        )
        run("start", backend)
        probe = r'''
import base64, json, re, ssl, sys, time
from http.client import HTTPConnection, HTTPSConnection

credentials = json.load(sys.stdin)
origin = 'https://agent-control.example.com'
host = 'agent-control.example.com'
authorization = 'Basic ' + base64.b64encode(
    ('synthetic-operator:' + credentials['password']).encode()
).decode()
context = ssl.create_default_context(cafile='/tmp/proxy-ca.pem')

def request(method, path, *, authenticated=True, headers=None, body=None):
    connection = HTTPSConnection('127.0.0.1', 443, context=context, timeout=3)
    sent = {'Host': host}
    if authenticated:
        sent['Authorization'] = authorization
    sent.update(headers or {})
    connection.request(method, path, body=body, headers=sent)
    response = connection.getresponse()
    result = response.status, response.read(), dict(response.getheaders())
    connection.close()
    return result

for attempt in range(100):
    try:
        if request('GET', '/api/data')[0] == 200:
            break
    except OSError:
        pass
    time.sleep(0.1)
else:
    raise AssertionError('HTTPS proxy/backend did not become ready')

for route in ['/', '/api/data', '/static/dashboard.js', '/api/narration?run_id=synthetic']:
    status, _, headers = request('GET', route, authenticated=False)
    assert status == 401, (route, status)
    assert headers['WWW-Authenticate'].startswith('Basic ')
wrong = 'Basic ' + base64.b64encode(b'synthetic-auth-log-probe:deny').decode()
assert request('GET', '/', headers={'Authorization': wrong})[0] == 401
assert request('GET', '/synthetic-private-uri-marker')[0] == 404
status, page, headers = request('GET', '/')
assert status == 200
assert "frame-ancestors 'none'" in headers['Content-Security-Policy']
csrf = re.search(rb'name="agent-control-csrf"\s+content="([^"]+)"', page).group(1).decode()
assert request('GET', '/api/data')[0] == 200
assert request('GET', '/static/panel.js')[0] == 200
assert request('GET', '/api/narration?run_id=synthetic')[0] == 403
assert request('GET', '/api/narration?run_id=synthetic', headers={'X-AgentControl-CSRF': csrf})[0] == 404

sent = {'Origin': origin, 'X-AgentControl-CSRF': csrf, 'Content-Type': 'application/json'}
body = json.dumps({'synthetic_private_body_marker': 'x' * 15000})
assert request('POST', '/api/no-such-route', headers=sent, body=body)[0] == 404
sent['Origin'] = 'https://wrong.example.test'
status, response, _ = request('POST', '/api/no-such-route', headers=sent, body='{}')
assert status == 403 and json.loads(response)['error']['code'] == 'bad_origin'
sent['Origin'] = origin
del sent['X-AgentControl-CSRF']
status, response, _ = request('POST', '/api/no-such-route', headers=sent, body='{}')
assert status == 403 and json.loads(response)['error']['code'] == 'bad_csrf'
assert request('GET', '/', headers={'Host': 'wrong.example.test', 'X-Forwarded-Host': host})[0] == 400

connection = HTTPConnection('127.0.0.1', 80, timeout=3)
connection.request('GET', '/', headers={'Host': host})
response = connection.getresponse()
assert response.status == 301 and response.getheader('Location') == origin + '/'
response.read()
connection.close()
print('Authenticated HTTPS, CSRF, Origin validation, and redirect checks passed')
'''
        output = run(
            "exec", "--interactive", backend, "python", "-c", probe,
            input=json.dumps({"password": password}),
        )
        assert "checks passed" in output
        run(
            "exec", proxy, "sh", "-c",
            'test -z "$(find /var/cache/nginx/client_temp /var/cache/nginx/proxy_temp -type f -print -quit)"',
        )
        run("stop", "--time", "1", backend)
        failure_probe = r'''
import base64, json, ssl, sys
from http.client import HTTPSConnection
password = json.load(sys.stdin)['password']
authorization = 'Basic ' + base64.b64encode(('synthetic-operator:' + password).encode()).decode()
connection = HTTPSConnection('127.0.0.1', 443, context=ssl.create_default_context(cafile='/tmp/proxy-ca.pem'), timeout=3)
connection.request('GET', '/synthetic-upstream-error-marker', headers={'Host': 'agent-control.example.com', 'Authorization': authorization})
response = connection.getresponse()
assert response.status == 502
response.read()
connection.close()
'''
        run(
            "run", "--rm", "--interactive", "--network", "container:" + proxy,
            "--mount", f"type=bind,source={certificate},target=/tmp/proxy-ca.pem,readonly",
            "--entrypoint", "python", image, "-c", failure_probe,
            input=json.dumps({"password": password}),
        )
        logs = subprocess.run(
            [docker, "logs", proxy], capture_output=True, text=True,
            check=True, timeout=15,
        )
        logged = logs.stdout + logs.stderr
        for marker in (
            "synthetic-auth-log-probe", "synthetic-private-uri-marker",
            "synthetic_private_body_marker", "synthetic-upstream-error-marker",
            "Authorization: Basic",
        ):
            assert bool(marker not in logged), "Proxy persisted synthetic request data"
        for container in (backend, proxy):
            ports = json.loads(run("inspect", "--format", "{{json .NetworkSettings.Ports}}", container))
            assert not any(ports.values())
    finally:
        for container in (backend, proxy):
            if container:
                subprocess.run(
                    [docker, "rm", "--force", "--volumes", container],
                    capture_output=True, timeout=30,
                )
        auth.unlink(missing_ok=True)
        key.unlink(missing_ok=True)

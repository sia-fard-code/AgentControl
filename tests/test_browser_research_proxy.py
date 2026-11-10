"""Synthetic resolver/socket checks; no external network or live state."""
import importlib.util
import pathlib
import socket
import threading

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
MODULE = ROOT / "tools" / "browser-research" / "public_proxy.py"


def _load():
    spec = importlib.util.spec_from_file_location("public_proxy_under_test", MODULE)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


pp = _load()

PUBLIC = "93.184.216.34"


@pytest.mark.parametrize("ip", [
    "127.0.0.1", "10.0.0.1", "172.16.0.1", "192.168.1.1", "169.254.169.254",
    "0.0.0.0", "100.64.0.1", "224.0.0.1", "255.255.255.255", "192.0.2.1",
    "198.18.0.1", "240.0.0.1", "::1", "::", "fe80::1", "fc00::1", "ff02::1",
    "::ffff:127.0.0.1", "::ffff:10.0.0.1", "2002:7f00:1::1", "2001::1", "fec0::1",
    "2001:db8::1", "64:ff9b::a00:1", "::127.0.0.1", "not-an-ip", "fe80::1%eth0",
])
def test_non_public_ips_rejected(ip):
    assert pp.is_public_ip(ip) is False


@pytest.mark.parametrize("ip", [PUBLIC, "8.8.8.8", "2606:4700:4700::1111"])
def test_public_ips_accepted(ip):
    assert pp.is_public_ip(ip) is True


def fake_resolver(table):
    """table: host -> list of IP strings, getaddrinfo-shaped output."""
    def resolve(host, port, *args, **kwargs):
        out = []
        for ip in table.get(host, []):
            fam = socket.AF_INET6 if ":" in ip else socket.AF_INET
            addr = (ip, port, 0, 0) if fam == socket.AF_INET6 else (ip, port)
            out.append((fam, socket.SOCK_STREAM, 6, "", addr))
        return out
    return resolve


@pytest.mark.parametrize("answers", [
    ["10.0.0.5"], [PUBLIC, "127.0.0.1"], [], ["::ffff:192.168.0.1"], ["fec0::1"],
])
def test_resolve_rejects_private_mixed_empty(answers):
    with pytest.raises(pp.ProxyReject):
        pp.resolve_public("x.test", 443, resolver=fake_resolver({"x.test": answers}))


def test_resolve_public_ok():
    fam, addr = pp.resolve_public("x.test", 443, resolver=fake_resolver({"x.test": [PUBLIC]}))
    assert fam == socket.AF_INET and addr[0] == PUBLIC and addr[1] == 443


def test_open_public_connects_pinned_numeric_ip_and_rebinding_rejected():
    answers = iter([[PUBLIC], ["127.0.0.1"]])
    calls = []

    def rebinding(host, port, *a, **k):
        return fake_resolver({host: next(answers)})(host, port)

    def connector(family, addr, timeout):
        calls.append((family, addr))
        return "sock"

    assert pp.open_public("x.test", 80, resolver=rebinding, connector=connector) == "sock"
    assert calls == [(socket.AF_INET, (PUBLIC, 80))]
    with pytest.raises(pp.ProxyReject):
        pp.open_public("x.test", 80, resolver=rebinding, connector=connector)
    assert len(calls) == 1


@pytest.mark.parametrize("authority", [
    "example.com:80", "example.com", "user:pw@example.com:443", "exa mple.com:443",
    "[::1]:443", "127.0.0.1:443", "[fe80::1%25eth0]:443", "example.com:443\r\n",
    "example.com:0443x", ":443", "example.com:44300",
])
def test_bad_connect_authority(authority):
    with pytest.raises(pp.ProxyReject):
        pp.parse_connect(authority)


def test_good_connect_authority():
    assert pp.parse_connect("Example.com:443") == ("example.com", 443)


@pytest.mark.parametrize("url", [
    "https://example.com/", "ftp://example.com/", "http://u:p@example.com/",
    "http://example.com:8080/", "http://example.com/#frag", "/relative",
    "http://127.0.0.1/", "http://[::1]/", "http://0x7f000001/", "http://2130706433/",
    "http://example.com/a\x00b", "http:///nohost",
])
def test_bad_http_url(url):
    with pytest.raises(pp.ProxyReject):
        pp.parse_http(url)


def test_good_http_url():
    assert pp.parse_http("http://Example.com/p?q=1") == ("example.com", 80, "/p?q=1")
    assert pp.parse_http("http://example.com:80") == ("example.com", 80, "/")


@pytest.fixture
def fixture_listener():
    """Synthetic loopback origin counting hits."""
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(8)
    hits = []

    def loop():
        while True:
            try:
                c, _ = srv.accept()
            except OSError:
                return
            hits.append(1)
            c.settimeout(2)
            try:
                c.recv(65536)
                c.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 5\r\nConnection: close\r\n\r\nhello")
            except OSError:
                pass
            c.close()

    t = threading.Thread(target=loop, daemon=True)
    t.start()
    yield srv.getsockname()[1], hits
    srv.close()
    t.join(2)


def start_proxy(resolver=socket.getaddrinfo, connector=None):
    kwargs = {"resolver": resolver}
    if connector:
        kwargs["connector"] = connector
    server = pp.make_server(**kwargs)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    return server, t


def stop_proxy(server, t):
    server.shutdown()
    server.server_close()
    t.join(5)
    assert not t.is_alive()


def send_raw(port, data):
    with socket.create_connection(("127.0.0.1", port), timeout=5) as s:
        s.sendall(data)
        out = b""
        while True:
            try:
                chunk = s.recv(65536)
            except (socket.timeout, ConnectionError):
                break
            if not chunk:
                break
            out += chunk
    return out


def status(resp):
    return int(resp.split(b" ", 2)[1])


SECRET = "s3cretTOKEN"


def test_live_rejections_never_reach_listener(fixture_listener, capsys):
    lport, hits = fixture_listener
    resolver = fake_resolver({"alias.test": ["127.0.0.1"], "mixed.test": [PUBLIC, "127.0.0.1"]})
    server, t = start_proxy(resolver=resolver)
    port = server.server_address[1]
    assert server.server_address[0] == "127.0.0.1"
    reqs = [
        f"GET http://alias.test/?{SECRET} HTTP/1.1\r\nHost: alias.test\r\n\r\n",
        f"GET http://127.0.0.1:{lport}/{SECRET} HTTP/1.1\r\n\r\n",
        f"GET http://[::1]/{SECRET} HTTP/1.1\r\n\r\n",
        f"CONNECT 127.0.0.1:{lport} HTTP/1.1\r\n\r\n",
        "CONNECT alias.test:443 HTTP/1.1\r\n\r\n",
        "CONNECT mixed.test:443 HTTP/1.1\r\n\r\n",
        "CONNECT [::1]:443 HTTP/1.1\r\n\r\n",
        f"POST http://alias.test/{SECRET} HTTP/1.1\r\nContent-Length: 0\r\n\r\n",
        f"GET /{SECRET} HTTP/1.1\r\n\r\n",
        "GET http://alias.test/ HTTP/1.1\r\nProxy-Authorization: Basic eDp5\r\n\r\n",
        "GET http://alias.test/ HTTP/1.1\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n\r\n",
        "GET http://x.test/ HTTP/1.1\r\n" + "X-Pad: " + "a" * 70000 + "\r\n\r\n",
    ]
    try:
        for r in reqs:
            resp = send_raw(port, r.encode())
            assert resp.startswith(b"HTTP/"), r[:30]
            assert status(resp) in (400, 403, 405, 431, 502)
            assert SECRET.encode() not in resp
    finally:
        stop_proxy(server, t)
    assert hits == []
    out = capsys.readouterr()
    assert SECRET not in out.out + out.err
    assert "alias.test" not in out.out + out.err


def loopback_connector(lport, seen):
    def connect(family, addr, timeout):
        seen.append(addr)
        assert addr[0] == PUBLIC  # proxy hands over only the pinned public IP
        return socket.create_connection(("127.0.0.1", lport), timeout=timeout)
    return connect


def test_public_http_forwarding(fixture_listener):
    lport, hits = fixture_listener
    seen = []
    server, t = start_proxy(resolver=fake_resolver({"pub.test": [PUBLIC]}),
                            connector=loopback_connector(lport, seen))
    try:
        resp = send_raw(server.server_address[1],
                        b"GET http://pub.test/x HTTP/1.1\r\nHost: pub.test\r\n\r\n")
    finally:
        stop_proxy(server, t)
    assert status(resp) == 200 and resp.endswith(b"hello")
    assert hits == [1] and seen == [(PUBLIC, 80)]


def test_public_connect_tunnel(fixture_listener):
    lport, hits = fixture_listener
    seen = []
    server, t = start_proxy(resolver=fake_resolver({"pub.test": [PUBLIC]}),
                            connector=loopback_connector(lport, seen))
    try:
        with socket.create_connection(("127.0.0.1", server.server_address[1]), timeout=5) as s:
            s.sendall(b"CONNECT pub.test:443 HTTP/1.1\r\nHost: pub.test:443\r\n\r\n")
            head = b""
            while b"\r\n\r\n" not in head:
                head += s.recv(1)
            assert status(head) == 200
            s.sendall(b"ping")
            body = b""
            while True:
                chunk = s.recv(4096)
                if not chunk:
                    break
                body += chunk
    finally:
        stop_proxy(server, t)
    assert body.endswith(b"hello") and seen == [(PUBLIC, 443)]


def test_cli_readiness_line_and_stdin_eof_shutdown():
    import json
    import subprocess
    import sys
    proc = subprocess.Popen([sys.executable, str(MODULE)], stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        line = proc.stdout.readline()
        port = json.loads(line)["port"]
        assert isinstance(port, int) and port > 0
        resp = send_raw(port, b"POST http://example.invalid/ HTTP/1.1\r\n\r\n")
        assert status(resp) == 405
        proc.stdin.close()
        assert proc.wait(timeout=10) == 0
        assert proc.stdout.read() == b"" and proc.stderr.read() == b""
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()


@pytest.mark.parametrize("method", ["_http", "_connect"])
def test_upstream_closes_when_relay_fails(method, monkeypatch):
    class Upstream:
        closed = False
        def settimeout(self, value):
            pass
        def sendall(self, value):
            pass
        def close(self):
            self.closed = True

    upstream = Upstream()
    monkeypatch.setattr(pp, "open_public", lambda *a: upstream)
    def broken_relay(*a, **k):
        raise OSError("synthetic relay failure")
    monkeypatch.setattr(pp, "relay", broken_relay)
    handler = object.__new__(pp.ProxyHandler)
    handler.headers = {}
    handler.server = type("Server", (), {"resolver": None, "connector": None})()
    handler.path = "http://example.com/" if method == "_http" else "example.com:443"
    handler.command = "GET"
    handler.connection = object()
    handler.send_response_only = lambda *a: None
    handler.end_headers = lambda: None
    handler._guard(getattr(handler, method))
    assert upstream.closed

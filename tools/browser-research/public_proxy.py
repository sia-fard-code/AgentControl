"""Public-only, DNS-pinned HTTP GET/HEAD + CONNECT:443 research proxy.

Binds 127.0.0.1:0, prints {"port": N}, serves until stdin EOF. Every target is
resolved once, every DNS answer must be globally routable, and the upstream
socket connects to the pinned numeric address (never re-resolves the name).
"""
import http.server
import ipaddress
import json
import re
import select
import socket
import socketserver
import sys
import threading
import time
import urllib.parse

CONNECT_CAP, CONNECT_SECONDS = 50 * 1024 * 1024, 45
HTTP_CAP, HTTP_SECONDS = 20 * 1024 * 1024, 30
CONNECT_TIMEOUT, IO_TIMEOUT = 10, 15
_DENY_V6 = [ipaddress.ip_network(n) for n in (
    "fec0::/10",      # deprecated site-local; ipaddress otherwise calls it global
    "2002::/16",      # 6to4
    "2001::/32",      # Teredo
    "64:ff9b::/96", "64:ff9b:1::/48",  # NAT64
    "::/96",          # IPv4-compatible (deprecated)
)]
_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")
_NUMERIC_LABEL = re.compile(r"0x[0-9a-f]*|[0-9]+")
_HOP = {"connection", "proxy-connection", "keep-alive", "proxy-authorization", "te",
        "trailer", "transfer-encoding", "upgrade", "host"}


class ProxyReject(Exception):
    def __init__(self, code=403):
        super().__init__(code)
        self.code = code


def is_public_ip(address):
    """True only for a globally routable unicast address (no scope, no transition encoding)."""
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return False
    if ip.version == 6:
        if ip.ipv4_mapped is not None:
            return is_public_ip(str(ip.ipv4_mapped))
        if any(ip in net for net in _DENY_V6):
            return False
    return ip.is_global and not ip.is_multicast and not ip.is_reserved


def _has_ctl(text):
    return any(ord(c) <= 0x20 or ord(c) >= 0x7f for c in text)


def check_host(host):
    """Validate a hostname or IP literal from a request; returns it lowercased."""
    host = (host or "").lower().rstrip(".")
    if not host or _has_ctl(host) or "%" in host:
        raise ProxyReject(400)
    if ":" in host:  # IPv6 literal
        if not is_public_ip(host):
            raise ProxyReject(403)
        return host
    labels = host.split(".")
    if all(_NUMERIC_LABEL.fullmatch(lbl) for lbl in labels):
        # Numeric forms: only canonical dotted-quad, and only if public (blocks 0x7f000001, 2130706433).
        try:
            canonical = str(ipaddress.IPv4Address(host)) == host
        except ValueError:
            canonical = False
        if not canonical or not is_public_ip(host):
            raise ProxyReject(403)
        return host
    if len(host) > 253 or not all(_LABEL.fullmatch(lbl) for lbl in labels):
        raise ProxyReject(400)
    return host


def _split_authority(authority, default_port):
    if _has_ctl(authority) or "@" in authority or "%" in authority:
        raise ProxyReject(400)
    m = re.fullmatch(r"\[([0-9a-fA-F:.]+)\](?::([0-9]{1,5}))?|([^:\[\]]+)(?::([0-9]{1,5}))?", authority)
    if not m:
        raise ProxyReject(400)
    host = m.group(1) or m.group(3)
    port = m.group(2) or m.group(4)
    return check_host(host), int(port) if port else default_port


def parse_connect(authority):
    """CONNECT target -> (host, 443); anything else is rejected."""
    if not re.search(r":[0-9]+$", authority or ""):
        raise ProxyReject(400)
    host, port = _split_authority(authority, None)
    if port != 443:
        raise ProxyReject(403)
    return host, port


def parse_http(url):
    """Absolute http:// URL -> (host, 80, path?query)."""
    if not url or _has_ctl(url) or "#" in url:
        raise ProxyReject(400)
    parts = urllib.parse.urlsplit(url)
    if parts.scheme != "http" or not parts.netloc:
        raise ProxyReject(400)
    host, port = _split_authority(parts.netloc, 80)
    if port != 80:
        raise ProxyReject(403)
    path = parts.path or "/"
    return host, port, path + ("?" + parts.query if parts.query else "")


def resolve_public(host, port, resolver=socket.getaddrinfo):
    """Resolve once; every answer must be public. Returns (family, sockaddr) of the first."""
    try:
        infos = resolver(host, port, 0, socket.SOCK_STREAM)
    except (OSError, UnicodeError):
        raise ProxyReject(502)
    answers = [(fam, addr) for fam, _t, _p, _c, addr in infos
               if fam in (socket.AF_INET, socket.AF_INET6)]
    if not answers or len(answers) != len(infos):
        raise ProxyReject(403)
    for _fam, addr in answers:
        if not is_public_ip(addr[0]) or "%" in addr[0] or (len(addr) > 3 and addr[3]):
            raise ProxyReject(403)
    fam, addr = answers[0]
    return fam, (addr[0], port) if fam == socket.AF_INET else (addr[0], port, 0, 0)


def _connect(family, addr, timeout):
    sock = socket.socket(family, socket.SOCK_STREAM)
    try:
        sock.settimeout(timeout)
        sock.connect(addr)
    except OSError:
        sock.close()
        raise
    return sock


def open_public(host, port, resolver=socket.getaddrinfo, connector=_connect):
    """Resolve, validate, then connect to the pinned numeric address only."""
    family, addr = resolve_public(host, port, resolver)
    try:
        return connector(family, addr, CONNECT_TIMEOUT)
    except OSError:
        raise ProxyReject(502)


def relay(client, upstream, cap, seconds, both):
    """Pump bytes until EOF, byte cap, or deadline. HTTP mode is upstream->client only."""
    deadline = time.monotonic() + seconds
    total = 0
    peers = {upstream: client, client: upstream} if both else {upstream: client}
    while total < cap:
        left = deadline - time.monotonic()
        if left <= 0:
            return
        readable, _, _ = select.select(list(peers), [], [], min(left, 1.0))
        for src in readable:
            data = src.recv(min(65536, cap - total))
            if not data:
                return
            total += len(data)
            peers[src].sendall(data)


class ProxyHandler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    timeout = IO_TIMEOUT
    rbufsize = 0  # unbuffered: no client bytes hide in a buffer past the checked headers

    def log_message(self, *args):
        pass

    def send_error(self, code, message=None, explain=None):
        # Static body only; never echo the request line or URL.
        self.close_connection = True
        try:
            self.send_response_only(code)
            self.send_header("Content-Length", "0")
            self.send_header("Connection", "close")
            self.end_headers()
        except OSError:
            pass

    def _checked_headers(self):
        h = self.headers
        if h.get("Proxy-Authorization") or h.get("Upgrade") or h.get("Transfer-Encoding"):
            raise ProxyReject(403)
        if h.get("Content-Length") not in (None, "0"):
            raise ProxyReject(400)

    def _guard(self, work):
        upstream = None
        try:
            self._checked_headers()
            upstream = work()
        except ProxyReject as exc:
            self.send_error(exc.code)
        except OSError:
            pass
        finally:
            self.close_connection = True
            if upstream is not None:
                upstream.close()

    def _http(self):
        host, port, path = parse_http(self.path)
        upstream = open_public(host, port, self.server.resolver, self.server.connector)
        try:
            authority = "[" + host + "]" if ":" in host else host
            lines = [f"{self.command} {path} HTTP/1.1", f"Host: {authority}"]
            lines += [f"{k}: {v}" for k, v in self.headers.items() if k.lower() not in _HOP]
            lines += ["Connection: close", "", ""]
            upstream.settimeout(IO_TIMEOUT)
            upstream.sendall("\r\n".join(lines).encode("latin-1"))
            relay(self.connection, upstream, HTTP_CAP, HTTP_SECONDS, both=False)
        finally:
            upstream.close()

    def _connect(self):
        host, port = parse_connect(self.path)
        upstream = open_public(host, port, self.server.resolver, self.server.connector)
        try:
            self.send_response_only(200, "Connection Established")
            self.end_headers()
            relay(self.connection, upstream, CONNECT_CAP, CONNECT_SECONDS, both=True)
        finally:
            upstream.close()

    def do_GET(self):
        self._guard(self._http)

    do_HEAD = do_GET

    def do_CONNECT(self):
        self._guard(self._connect)

    def _method_not_allowed(self):
        self.send_error(405)

    do_POST = do_PUT = do_DELETE = do_PATCH = do_OPTIONS = do_TRACE = _method_not_allowed


class _Server(http.server.ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address):
        pass  # no tracebacks: they could carry request data


def make_server(resolver=socket.getaddrinfo, connector=_connect):
    server = _Server(("127.0.0.1", 0), ProxyHandler)
    server.resolver, server.connector = resolver, connector
    return server


def main():
    server = make_server()
    sys.stdout.write(json.dumps({"port": server.server_address[1]}) + "\n")
    sys.stdout.flush()

    def watch_stdin():
        while sys.stdin.buffer.read(4096):
            pass
        server.shutdown()

    threading.Thread(target=watch_stdin, daemon=True).start()
    try:
        server.serve_forever()
    finally:
        server.server_close()


if __name__ == "__main__":
    main()

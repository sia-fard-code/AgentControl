"""Stdlib MCP (Model Context Protocol) stdio client capability for the harness.

Deny-by-default, lazily started, once-per-run servers. Tools are namespaced as
mcp__<server>__<tool>. Mutating tools require explicit allow_mutation.
"""
import atexit
import copy
import fnmatch
import json
import math
from decimal import Decimal, InvalidOperation, localcontext
import os
import queue
import re
import shutil
import signal
import subprocess
import sys
import threading
import time

PROTOCOL_VERSION = "2024-11-05"
SUPPORTED_VERSIONS = ("2024-11-05", "2025-03-26", "2025-06-18")
NAME_RE = re.compile(r"^[A-Za-z0-9_-]+$")
NATIVE_MAX = 64
PREFIX = "mcp__"
MAX_FRAME_BYTES = 32 * 1024 * 1024
MAX_QUEUE = 1000
MAX_PAGES = 50
MAX_TOOLS = 500
MAX_STDERR = 64 * 1024
MAX_NOTIFICATIONS = 1000
WAIT_SLICE = 5.0
DEFAULT_TIMEOUT = 120
JOIN_TIMEOUT = 2.0              # per-thread bounded join during kill
MAX_DESCRIPTION = 2048          # chars kept of a tool description
MAX_SCHEMA_BYTES = 64 * 1024    # compact-JSON size of one inputSchema
MAX_CATALOG_BYTES = 512 * 1024  # total exposed name+description+schema bytes
# clientInfo.version is the EXISTING harness version (engine/delegate.py
# HARNESS_VERSION = 6), not a new product version; a test enforces equality.
CLIENT_VERSION = "6"

SHELL_LAUNCHERS = {
    "sh", "bash", "zsh", "dash", "ksh", "csh", "tcsh", "fish", "cmd", "cmd.exe",
    "powershell", "powershell.exe", "pwsh", "pwsh.exe", "wscript", "cscript",
    "wscript.exe", "cscript.exe", "env", "sudo", "busybox",
}
SHIM_EXTS = (".bat", ".cmd", ".ps1", ".vbs", ".sh")
INTERPRETERS = {"python", "python3", "pythonw", "py", "node", "deno", "bun",
                "perl", "ruby", "php", "lua", "osascript", "tclsh", "R", "Rscript"}
INLINE_FLAGS = {"-c", "-e", "--eval", "-p", "--print", "-r", "--command",
                "--exec", "-i", "--interactive"}
_CLUSTER_INLINE = re.compile(r"^-[A-Za-z]*[cepr][A-Za-z]*")
_SERVER_KEYS = {"command", "env", "timeout_s", "allowed_tools",
                "mutating_tools", "enabled"}


class MCPError(ValueError):
    """Raised for refusals ('refused: ...') and runtime MCP failures."""


def _refuse(msg):
    return MCPError("refused: " + msg)


def _exe_base(path):
    base = os.path.basename(path).lower()
    for suf in (".exe",):
        if base.endswith(suf):
            base = base[: -len(suf)]
    return base


def _check_argv(argv, where):
    if not isinstance(argv, list) or not argv:
        raise MCPError("%s must be a non-empty list of strings" % where)
    for a in argv:
        if not isinstance(a, str) or not a or "\x00" in a:
            raise MCPError("%s must contain non-empty strings" % where)
    exe = argv[0]
    base = _exe_base(exe)
    low = os.path.basename(exe).lower()
    if base in SHELL_LAUNCHERS or low in SHELL_LAUNCHERS:
        raise MCPError("%s: shell launchers are not permitted" % where)
    if low.endswith(SHIM_EXTS):
        raise MCPError("%s: batch/script shims are not permitted" % where)
    interp = base.rstrip("0123456789.") in INTERPRETERS or base in INTERPRETERS
    for a in argv:
        parts = re.split(r"[\\/]", a)
        if ".." in parts:
            raise MCPError("%s: '..' path segments are not permitted" % where)
        if any(ch in a for ch in "*?[]"):
            raise MCPError("%s: wildcard patterns are not permitted" % where)
    if interp:
        for a in argv[1:]:
            if a in INLINE_FLAGS or a.startswith("--eval=") or (
                    not a.startswith("--") and _CLUSTER_INLINE.match(a)):
                raise MCPError("%s: interpreter inline execution flags are "
                               "not permitted" % where)
    if not os.path.isabs(exe) and (os.sep in exe or "/" in exe):
        raise MCPError("%s: relative executable paths are not permitted" % where)
    return list(argv)


def _str_list(v, where):
    if not isinstance(v, list) or any(not isinstance(x, str) or not x for x in v):
        raise MCPError("%s must be a list of non-empty strings" % where)
    return list(v)


def resolve_executable(exe):
    """Resolve a bare executable once via shutil.which to an absolute path."""
    if os.path.isabs(exe):
        return os.path.normpath(exe)
    found = shutil.which(exe)
    if not found:
        raise MCPError("refused: executable %r not found on PATH" % exe)
    found = os.path.abspath(found)
    if os.path.basename(found).lower().endswith(SHIM_EXTS):
        raise MCPError("refused: %r resolves to a script shim" % exe)
    return found


def validate_mcp_policy(value):
    """Strictly validate an MCP policy object; return a frozen deep copy."""
    if value is None:
        return {"allowed_commands": [], "servers": {}}
    if not isinstance(value, dict):
        raise MCPError("mcp policy must be an object")
    extra = set(value) - {"allowed_commands", "servers"}
    if extra:
        raise MCPError("mcp policy: unknown keys %s" % sorted(extra))
    allowed = value.get("allowed_commands", [])
    if not isinstance(allowed, list):
        raise MCPError("allowed_commands must be a list of argv lists")
    allowed_out = [_check_argv(copy.deepcopy(a), "allowed_commands[%d]" % i)
                   for i, a in enumerate(allowed)]
    servers = value.get("servers", {})
    if not isinstance(servers, dict):
        raise MCPError("servers must be an object")
    out = {}
    for name, spec in servers.items():
        if not isinstance(name, str) or not NAME_RE.match(name):
            raise MCPError("server name %r must match [A-Za-z0-9_-]+" % (name,))
        if "__" in name:
            raise MCPError("server name %r must not contain '__'" % name)
        if len(PREFIX + name + "__") + 1 > NATIVE_MAX:
            raise MCPError("server name %r too long" % name)
        if not isinstance(spec, dict):
            raise MCPError("server %r must be an object" % name)
        extra = set(spec) - _SERVER_KEYS
        if extra:
            raise MCPError("server %r: unknown keys %s" % (name, sorted(extra)))
        enabled = spec.get("enabled", True)
        if not isinstance(enabled, bool):
            raise MCPError("server %r: enabled must be a boolean" % name)
        if "command" not in spec:
            raise MCPError("server %r: command is required" % name)
        where = "server %r command" % name
        if enabled:
            command = _check_argv(copy.deepcopy(spec["command"]), where)
            if command not in allowed_out:
                raise MCPError("server %r: command is not exactly listed in "
                               "allowed_commands" % name)
        else:
            cmd = spec["command"]
            if not isinstance(cmd, list) or any(not isinstance(c, str) for c in cmd):
                raise MCPError("%s must be a list of strings" % where)
            command = list(cmd)
        env = spec.get("env", {})
        if not isinstance(env, dict):
            raise MCPError("server %r: env must be an object" % name)
        for k, v in env.items():
            if not isinstance(k, str) or not k or "=" in k or "\x00" in k:
                raise MCPError("server %r: invalid env key %r" % (name, k))
            if not isinstance(v, str) or "\x00" in v:
                raise MCPError("server %r: env %r must be a string" % (name, k))
        t = spec.get("timeout_s", DEFAULT_TIMEOUT)
        if isinstance(t, bool) or not isinstance(t, (int, float)) \
                or not (t > 0) or t != t or t == float("inf") or t > 86400:
            raise MCPError("server %r: timeout_s must be a finite positive "
                           "number" % name)
        allowed_tools = _str_list(spec.get("allowed_tools", []),
                                  "server %r allowed_tools" % name)
        mutating_tools = _str_list(spec.get("mutating_tools", []),
                                   "server %r mutating_tools" % name)
        out[name] = {
            "command": command, "env": dict(env), "timeout_s": t,
            "allowed_tools": allowed_tools, "mutating_tools": mutating_tools,
            "enabled": enabled,
        }
    lowered = {}
    for name in out:
        if name.lower() in lowered:
            raise MCPError("server names %r and %r collide" %
                           (lowered[name.lower()], name))
        lowered[name.lower()] = name
    return {"allowed_commands": allowed_out, "servers": out}


# --------------------------------------------------------------- schemas --
_SCHEMA_KEYS = {
    "type", "properties", "required", "additionalProperties", "items", "enum",
    "const", "anyOf", "oneOf", "allOf", "$ref", "$defs", "definitions",
    "description", "title", "default", "examples", "$schema", "$id",
    "$comment", "format", "minimum", "maximum", "exclusiveMinimum",
    "exclusiveMaximum", "minLength", "maxLength", "minItems", "maxItems",
    "uniqueItems", "nullable", "readOnly", "writeOnly",
    "deprecated", "multipleOf", "minProperties", "maxProperties",
}
_TYPES = {"object", "array", "string", "number", "integer", "boolean", "null"}
MAX_SCHEMA_DEPTH = 32
MAX_SCHEMA_NODES = 5000


def check_schema(schema):
    """Reject schemas this validator cannot faithfully enforce."""
    count = [0]

    def walk(s, depth):
        count[0] += 1
        if count[0] > MAX_SCHEMA_NODES or depth > MAX_SCHEMA_DEPTH:
            raise MCPError("unsupported schema: too large or deep")
        if isinstance(s, bool):
            return
        if not isinstance(s, dict):
            raise MCPError("unsupported schema: node is not an object")
        bad = set(s) - _SCHEMA_KEYS
        if bad:
            raise MCPError("unsupported schema keywords %s" % sorted(bad))
        t = s.get("type")
        if t is not None:
            ts = t if isinstance(t, list) else [t]
            if not ts or any(x not in _TYPES for x in ts):
                raise MCPError("unsupported schema type %r" % (t,))
        ref = s.get("$ref")
        if ref is not None and (not isinstance(ref, str) or not
                                re.match(r"^#/(\$defs|definitions)/[^/]+$", ref)):
            raise MCPError("unsupported (non-local) $ref %r" % (ref,))
        for k in ("properties", "$defs", "definitions"):
            if k in s:
                if not isinstance(s[k], dict):
                    raise MCPError("schema %s must be an object" % k)
                for v in s[k].values():
                    walk(v, depth + 1)
        for k in ("items", "additionalProperties"):
            if k in s:
                walk(s[k], depth + 1)
        for k in ("anyOf", "oneOf", "allOf"):
            if k in s:
                if not isinstance(s[k], list) or not s[k]:
                    raise MCPError("schema %s must be a non-empty list" % k)
                for v in s[k]:
                    walk(v, depth + 1)
        if "required" in s and (not isinstance(s["required"], list) or any(
                not isinstance(x, str) for x in s["required"])):
            raise MCPError("schema required must be a list of strings")
        if "enum" in s and not isinstance(s["enum"], list):
            raise MCPError("schema enum must be a list")
        if "multipleOf" in s:
            factor = s["multipleOf"]
            if isinstance(factor, bool) or not isinstance(factor, (int, float)) or factor <= 0 or not math.isfinite(factor):
                raise MCPError("unsupported schema: multipleOf must be finite and positive")

    walk(schema, 0)
    if not isinstance(schema, dict) or schema.get("type", "object") != "object":
        raise MCPError("tool inputSchema must be an object schema")


def _type_ok(v, t):
    if t == "null":
        return v is None
    if t == "boolean":
        return isinstance(v, bool)
    if t == "integer":
        return (isinstance(v, int) and not isinstance(v, bool)) or (
            isinstance(v, float) and v.is_integer())
    if t == "number":
        return isinstance(v, (int, float)) and not isinstance(v, bool)
    if t == "string":
        return isinstance(v, str)
    if t == "array":
        return isinstance(v, list)
    if t == "object":
        return isinstance(v, dict)
    return False


def _json_eq(a, b):
    if isinstance(a, bool) != isinstance(b, bool):
        return False
    return a == b


def validate_args(value, schema):
    """Validate value against a (pre-checked) local JSON schema."""
    root = schema
    steps = [0]

    def resolve(ref):
        _, kind, key = ref.split("/", 2)
        defs = root.get(kind) or {}
        if key not in defs:
            raise MCPError("unresolvable $ref %r" % ref)
        return defs[key]

    def v(x, s, path, depth):
        steps[0] += 1
        if steps[0] > 100000 or depth > 64:
            raise MCPError("arguments too complex to validate")
        if s is True:
            return
        if s is False:
            raise MCPError("%s: not allowed by schema" % path)
        if "$ref" in s:
            v(x, resolve(s["$ref"]), path, depth + 1)
        t = s.get("type")
        if t is not None:
            ts = t if isinstance(t, list) else [t]
            if s.get("nullable") is True:
                ts = ts + ["null"]
            if not any(_type_ok(x, tt) for tt in ts):
                raise MCPError("%s: expected type %s" % (path, "/".join(ts)))
        if "enum" in s and not any(_json_eq(x, e) for e in s["enum"]):
            raise MCPError("%s: value not in enum" % path)
        if "const" in s and not _json_eq(x, s["const"]):
            raise MCPError("%s: value does not match const" % path)
        if isinstance(x, str):
            if "minLength" in s and len(x) < s["minLength"]:
                raise MCPError("%s: string too short" % path)
            if "maxLength" in s and len(x) > s["maxLength"]:
                raise MCPError("%s: string too long" % path)
            if "pattern" in s:
                raise MCPError("unsupported schema pattern: unbounded regex execution")
        if isinstance(x, (int, float)) and not isinstance(x, bool):
            if "multipleOf" in s:
                number, factor = Decimal(str(x)), Decimal(str(s["multipleOf"]))
                try:
                    with localcontext() as ctx:
                        ctx.prec = max(34, len(number.as_tuple().digits),
                                       len(factor.as_tuple().digits),
                                       number.adjusted() - factor.adjusted() + 10)
                        if number % factor != 0:
                            raise MCPError("%s: not a multipleOf value" % path)
                except InvalidOperation:
                    raise MCPError("%s: invalid multipleOf arithmetic" % path)
            if "minimum" in s and x < s["minimum"]:
                raise MCPError("%s: below minimum" % path)
            if "maximum" in s and x > s["maximum"]:
                raise MCPError("%s: above maximum" % path)
            em, eM = s.get("exclusiveMinimum"), s.get("exclusiveMaximum")
            if isinstance(em, (int, float)) and not isinstance(em, bool) \
                    and x <= em:
                raise MCPError("%s: not above exclusiveMinimum" % path)
            if isinstance(eM, (int, float)) and not isinstance(eM, bool) \
                    and x >= eM:
                raise MCPError("%s: not below exclusiveMaximum" % path)
        if isinstance(x, dict):
            props = s.get("properties", {})
            for r in s.get("required", []):
                if r not in x:
                    raise MCPError("%s: missing required property %r" % (path, r))
            if "maxProperties" in s and len(x) > s["maxProperties"]:
                raise MCPError("%s: too many properties" % path)
            if "minProperties" in s and len(x) < s["minProperties"]:
                raise MCPError("%s: too few properties" % path)
            addl = s.get("additionalProperties", True)
            for k, val in x.items():
                if not isinstance(k, str):
                    raise MCPError("%s: non-string key" % path)
                if k in props:
                    v(val, props[k], "%s.%s" % (path, k), depth + 1)
                elif addl is False:
                    raise MCPError("%s: unexpected property %r" % (path, k))
                elif isinstance(addl, dict):
                    v(val, addl, "%s.%s" % (path, k), depth + 1)
        if isinstance(x, list):
            if "minItems" in s and len(x) < s["minItems"]:
                raise MCPError("%s: too few items" % path)
            if "maxItems" in s and len(x) > s["maxItems"]:
                raise MCPError("%s: too many items" % path)
            if "items" in s:
                for i, item in enumerate(x):
                    v(item, s["items"], "%s[%d]" % (path, i), depth + 1)
            if s.get("uniqueItems") is True:
                seen = [json.dumps(i, sort_keys=True) for i in x]
                if len(set(seen)) != len(seen):
                    raise MCPError("%s: items not unique" % path)
        for sub in s.get("allOf", []):
            v(x, sub, path, depth + 1)
        if "anyOf" in s:
            if not any(_ok(x, sub, path, depth) for sub in s["anyOf"]):
                raise MCPError("%s: matches no anyOf alternative" % path)
        if "oneOf" in s:
            n = sum(1 for sub in s["oneOf"] if _ok(x, sub, path, depth))
            if n != 1:
                raise MCPError("%s: must match exactly one oneOf (got %d)"
                               % (path, n))

    def _ok(x, sub, path, depth):
        try:
            v(x, sub, path, depth + 1)
            return True
        except MCPError:
            return False

    v(value, schema, "args", 0)


# ------------------------------------------------------------- transport --
_SAFE_ENV = ("SYSTEMROOT", "WINDIR", "TEMP", "TMP", "PATH", "PATHEXT",
             "LANG", "LC_ALL", "TZ")
_EOF = object()


class _Timeout(MCPError):
    pass


def _build_env(policy_env):
    """Minimal non-credential base env plus operator-authored strings only."""
    env = {}
    for k in _SAFE_ENV:
        val = os.environ.get(k)
        if val is not None:
            env[k] = val
    env.update(policy_env)
    return env


class _Server(object):
    def __init__(self, name, spec, root):
        self.name = name
        self.spec = spec
        self.root = root
        self.proc = None
        self.q = queue.Queue()
        self.qcount = 0
        self.overflow = None
        self.stderr_tail = b""
        self.stderr_bytes = 0
        self.threads = []       # reader threads
        self.writers = []       # writer threads (tracked, joined on kill)
        self.out_thread = None
        self.err_thread = None
        self.unclosed = []      # pipes left open because a thread still used them
        self.job = None         # Windows Job Object handle (owned descendants)
        self.contained = False  # True when descendants are job/pgroup-contained
        self.next_id = 0
        self.ignored = 0
        self.wlock = threading.Lock()
        self.closed = False

    def start(self):
        argv = list(self.spec["command"])
        argv[0] = resolve_executable(argv[0])
        kw = dict(stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                  stderr=subprocess.PIPE, cwd=self.root, shell=False,
                  env=_build_env(self.spec["env"]), close_fds=True)
        if os.name == "nt":
            kw["creationflags"] = (getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                                   | getattr(subprocess, "CREATE_NO_WINDOW", 0)
                                   | 0x00000004)  # CREATE_SUSPENDED
        else:
            kw["start_new_session"] = True
        try:
            self.proc = subprocess.Popen(argv, **kw)
        except OSError as e:
            raise MCPError("mcp server %r failed to start: %s"
                           % (self.name, e.__class__.__name__))
        if os.name == "nt":
            # Assign to a kill-on-close job BEFORE the server runs any code,
            # so no descendant can escape containment; then resume it.
            try:
                self.job = _win_job_for(self.proc._handle)
            except Exception:
                self.job = None
            self.contained = self.job is not None
            if not self.contained:
                # Never run server code if descendant cleanup is unavailable.
                # The initial process is still suspended and has no children.
                self.kill()
                raise MCPError("mcp server %r failed to start: process containment unavailable"
                               % self.name)
            try:
                resumed = _win_resume(self.proc._handle)
            except Exception:
                resumed = False
            if not resumed:
                self._kill_owned(force=True)
                raise MCPError("mcp server %r failed to start: resume failed"
                               % self.name)
        else:
            self.contained = True
        for fn, attr in ((self._read_out, "out_thread"),
                         (self._read_err, "err_thread")):
            t = threading.Thread(target=fn, name="mcp-%s" % self.name)
            t.daemon = True
            t.start()
            setattr(self, attr, t)
            self.threads.append(t)

    def _kill_owned(self, force):
        _kill_owned_tree(self.proc, self.job, force)

    def _put(self, item):
        if self.qcount >= MAX_QUEUE:
            self.overflow = "server message queue overflow"
            return
        self.qcount += 1
        self.q.put(item)

    def _read_out(self):
        out = self.proc.stdout
        try:
            while True:
                line = out.readline(MAX_FRAME_BYTES + 1)
                if not line:
                    break
                if len(line) > MAX_FRAME_BYTES and not line.endswith(b"\n"):
                    self._put(("error", "frame exceeds %d bytes" % MAX_FRAME_BYTES))
                    while True:  # keep draining the oversized frame
                        rest = out.readline(1 << 20)
                        if not rest or rest.endswith(b"\n"):
                            break
                    continue
                if not line.strip():
                    continue
                try:
                    self._put(("msg", json.loads(line.decode("utf-8"))))
                except (ValueError, UnicodeDecodeError):
                    self._put(("error", "malformed JSON frame"))
        except (OSError, ValueError):
            pass
        self.q.put((_EOF, None))

    def _read_err(self):
        err = self.proc.stderr
        try:
            while True:
                chunk = err.read1(65536) if hasattr(err, "read1") else err.read(65536)
                if not chunk:
                    break
                self.stderr_bytes += len(chunk)
                self.stderr_tail = (self.stderr_tail + chunk)[-MAX_STDERR:]
        except (OSError, ValueError):
            pass

    def send(self, obj, deadline):
        body = json.dumps(obj, separators=(",", ":")).encode("utf-8")
        if len(body) > MAX_FRAME_BYTES:  # same limit as reader: newline excluded
            raise MCPError("refused: request frame too large")
        if self.closed:
            raise MCPError("mcp server %r is closed" % self.name)
        data = body + b"\n"
        box = {}

        def w():
            try:
                with self.wlock:
                    self.proc.stdin.write(data)
                    self.proc.stdin.flush()
            except Exception as e:  # noqa: BLE001
                box["err"] = e
            box["done"] = True

        t = threading.Thread(target=w, name="mcp-w-%s" % self.name)
        t.daemon = True
        self.writers = [x for x in self.writers if x.is_alive()]
        self.writers.append(t)
        t.start()
        t.join(max(0.0, deadline - time.monotonic()))
        if t.is_alive():
            raise _Timeout("write timed out")
        if "err" in box:
            raise MCPError("mcp server %r: write failed (%s)"
                           % (self.name, box["err"].__class__.__name__))

    def request(self, method, params, deadline, on_wait=None):
        self.next_id += 1
        rid = self.next_id
        msg = {"jsonrpc": "2.0", "id": rid, "method": method}
        if params is not None:
            msg["params"] = params
        self.send(msg, deadline)
        last_wait = time.monotonic()
        while True:
            now = time.monotonic()
            if now >= deadline:
                raise _Timeout("%s timed out" % method)
            try:
                kind, item = self.q.get(timeout=min(WAIT_SLICE, deadline - now,
                                                    max(0.01, last_wait + WAIT_SLICE - now)))
            except queue.Empty:
                if on_wait is not None and time.monotonic() - last_wait >= WAIT_SLICE - 0.05:
                    last_wait = time.monotonic()
                    on_wait()
                continue
            self.qcount -= 1
            if self.overflow:
                raise MCPError("mcp server %r: %s" % (self.name, self.overflow))
            if kind is _EOF:
                self.q.put((_EOF, None))
                raise MCPError("mcp server %r exited unexpectedly" % self.name)
            if kind == "error":
                raise MCPError("mcp server %r protocol error: %s" % (self.name, item))
            if not isinstance(item, dict) or item.get("jsonrpc") != "2.0":
                raise MCPError("mcp server %r sent non JSON-RPC 2.0 frame" % self.name)
            if "method" in item:
                if "id" in item:
                    # Unsolicited server->client request: reject, never act.
                    self.send({"jsonrpc": "2.0", "id": item["id"],
                               "error": {"code": -32601,
                                         "message": "method not supported by client"}},
                              deadline)
                else:
                    self.ignored += 1
                    if self.ignored > MAX_NOTIFICATIONS:
                        raise MCPError("mcp server %r: too many notifications"
                                       % self.name)
                continue
            if item.get("id") != rid:
                continue  # stale/unknown response id; ignore
            if "error" in item:
                return {"__rpc_error__": item["error"]}
            if "result" not in item:
                raise MCPError("mcp server %r: response without result" % self.name)
            return item["result"]

    def notify(self, method, deadline):
        self.send({"jsonrpc": "2.0", "method": method}, deadline)

    def alive(self):
        return self.proc is not None and self.proc.poll() is None

    def kill(self):
        """Terminate then kill only our own server process (tree)."""
        if self.closed:
            return
        self.closed = True
        p = self.proc
        if p is None:
            return
        # 1. Kill the owned tree FIRST. Closing the buffered stdin here could
        #    block forever on the IO lock held by a writer stuck in write().
        #    Always run even if the server already exited: owned descendants
        #    (job / process group) may still hold the pipes.
        try:
            self._kill_owned(force=False)
        except Exception:
            pass
        try:
            p.wait(timeout=JOIN_TIMEOUT)
        except Exception:
            pass
        try:
            self._kill_owned(force=True)  # no-op on Windows job already gone
        except Exception:
            pass
        try:
            p.wait(timeout=5)
        except Exception:
            pass
        # 2. Bounded joins: writers (EPIPE once the tree is dead) and readers.
        for t in list(self.writers) + list(self.threads):
            try:
                t.join(timeout=JOIN_TIMEOUT)
            except Exception:
                pass
        # 3. Close only pipes whose user thread is gone; closing a buffered
        #    pipe in use by a live thread blocks on its lock. A leftover pipe
        #    is recorded and released at interpreter exit (daemon thread).
        writers_alive = any(t.is_alive() for t in self.writers)
        for name, s, busy in (("stdin", p.stdin, writers_alive),
                              ("stdout", p.stdout, self._alive(self.out_thread)),
                              ("stderr", p.stderr, self._alive(self.err_thread))):
            if s is None:
                continue
            if busy:
                self.unclosed.append(name)
                continue
            try:
                s.close()
            except Exception:
                pass
        if self.job is not None and os.name == "nt":
            try:
                _k32().CloseHandle(self.job)  # kill-on-close: final backstop
            except Exception:
                pass
            self.job = None

    @staticmethod
    def _alive(t):
        return t is not None and t.is_alive()


_K32 = None


def _k32():
    """kernel32 via ctypes (stdlib) with explicit signatures; Windows only."""
    global _K32
    if _K32 is None:
        import ctypes
        from ctypes import wintypes as W
        k = ctypes.WinDLL("kernel32", use_last_error=True)
        k.CreateJobObjectW.restype = W.HANDLE
        k.CreateJobObjectW.argtypes = (ctypes.c_void_p, W.LPCWSTR)
        k.SetInformationJobObject.restype = W.BOOL
        k.SetInformationJobObject.argtypes = (W.HANDLE, ctypes.c_int,
                                              ctypes.c_void_p, W.DWORD)
        k.AssignProcessToJobObject.restype = W.BOOL
        k.AssignProcessToJobObject.argtypes = (W.HANDLE, W.HANDLE)
        k.TerminateJobObject.restype = W.BOOL
        k.TerminateJobObject.argtypes = (W.HANDLE, ctypes.c_uint)
        k.CloseHandle.restype = W.BOOL
        k.CloseHandle.argtypes = (W.HANDLE,)
        _K32 = k
    return _K32


def _win_job_for(handle):
    """Create a kill-on-close Job Object and put the (suspended) server in it.

    Every descendant the server spawns afterwards is in the same job, so
    TerminateJobObject reaches owned grandchildren even after the server
    itself exited, and never anything we did not launch (e.g. STAAD).
    Returns the job handle or None.
    """
    import ctypes
    from ctypes import wintypes as W

    class Basic(ctypes.Structure):
        _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64),
                    ("PerJobUserTimeLimit", ctypes.c_int64),
                    ("LimitFlags", W.DWORD),
                    ("MinimumWorkingSetSize", ctypes.c_size_t),
                    ("MaximumWorkingSetSize", ctypes.c_size_t),
                    ("ActiveProcessLimit", W.DWORD),
                    ("Affinity", ctypes.c_size_t),
                    ("PriorityClass", W.DWORD),
                    ("SchedulingClass", W.DWORD)]

    class Ext(ctypes.Structure):
        _fields_ = [("Basic", Basic), ("Io", ctypes.c_uint64 * 6),
                    ("ProcessMemoryLimit", ctypes.c_size_t),
                    ("JobMemoryLimit", ctypes.c_size_t),
                    ("PeakProcessMemoryUsed", ctypes.c_size_t),
                    ("PeakJobMemoryUsed", ctypes.c_size_t)]
    k = _k32()
    job = k.CreateJobObjectW(None, None)
    if not job:
        return None
    info = Ext()
    info.Basic.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    if not (k.SetInformationJobObject(job, 9, ctypes.byref(info),
                                      ctypes.sizeof(info))
            and k.AssignProcessToJobObject(job, int(handle))):
        k.CloseHandle(job)
        return None
    return job


def _win_resume(handle):
    import ctypes
    nt = ctypes.WinDLL("ntdll")
    nt.NtResumeProcess.restype = ctypes.c_long
    nt.NtResumeProcess.argtypes = (ctypes.c_void_p,)
    return nt.NtResumeProcess(ctypes.c_void_p(int(handle))) == 0


def _kill_owned_tree(p, job, force):
    """Kill only the server we launched and its OWNED descendants.

    Windows: terminate the Job Object (works after the server exited); if no
    job exists, fall back to the server pid only while it is still alive (a
    dead pid may be reused by an unrelated process, so it is never targeted).
    POSIX: always signal the server's own session/process group (pgid == the
    server pid, kept reserved while any group member lives), even if the
    server itself has already exited.
    """
    if os.name == "nt":
        if job is not None:
            try:
                _k32().TerminateJobObject(job, 1)
            except Exception:
                pass
        if p.poll() is None:
            try:
                p.kill()
            except Exception:
                pass
        return
    try:
        os.killpg(p.pid, signal.SIGKILL if force else signal.SIGTERM)
    except Exception:
        if p.poll() is None:
            try:
                p.kill() if force else p.terminate()
            except Exception:
                pass


_LIVE = set()
_LIVE_LOCK = threading.Lock()


def _atexit_cleanup():
    with _LIVE_LOCK:
        mgrs = list(_LIVE)
    for m in mgrs:
        try:
            m.close()
        except Exception:
            pass


atexit.register(_atexit_cleanup)


def _matches(name, patterns):
    return any(fnmatch.fnmatchcase(name, p) for p in patterns)


class MCPManager(object):
    """Lazy once-per-run MCP stdio servers with deny-by-default tool policy."""

    def __init__(self, policy, root, allow_mutation=False, event=None):
        self._policy = copy.deepcopy(policy or {"allowed_commands": [],
                                                "servers": {}})
        self._root = os.path.abspath(root)
        self._allow_mutation = allow_mutation is True
        self._event = event
        self._servers = {}      # name -> _Server (started)
        self._poisoned = {}     # name -> reason
        self._tools = {}        # namespaced -> record
        self._skipped = {}      # namespaced -> {reason, mutating}: not exposed
        self._catalog_bytes = 0
        self._discovered = set()
        self._lock = threading.RLock()
        self._closed = False
        self.launch_count = {}
        with _LIVE_LOCK:
            _LIVE.add(self)

    # -- helpers --
    def _emit(self, ev):
        if self._event is None:
            return
        try:
            self._event(dict(ev))
        except Exception:
            pass  # observability must never affect authorization/flow

    def owns(self, name):
        return isinstance(name, str) and name.startswith(PREFIX)

    def _spec(self, server):
        spec = self._policy["servers"].get(server) if isinstance(server, str) else None
        if spec is None:
            raise _refuse("unknown mcp server %r" % (server,))
        if not spec["enabled"]:
            raise _refuse("mcp server %r is disabled" % server)
        if spec["command"] not in self._policy["allowed_commands"]:
            raise _refuse("mcp server %r command not approved" % server)
        return spec

    def _is_mutating(self, spec, tool, annotations):
        if tool == "execute_code" or tool.endswith("_execute_code"):
            return True
        if _matches(tool, spec["mutating_tools"]):
            return True
        if isinstance(annotations, dict) and annotations.get("destructiveHint") is True:
            return True
        return False

    def list_tools(self, server=None):
        if server is None:
            out = {}
            for n, s in self._policy["servers"].items():
                out[n] = {"enabled": s["enabled"],
                          "allowed_tools": list(s["allowed_tools"]),
                          "mutating_tools": list(s["mutating_tools"]),
                          "started": n in self._servers,
                          "poisoned": n in self._poisoned}
            return {"servers": out}
        spec = self._spec(server)
        with self._lock:
            self._ensure(server, spec)
            tools = [dict(name=k, description=r["description"],
                          inputSchema=copy.deepcopy(r["schema"]),
                          mutating=r["mutating"])
                     for k, r in self._tools.items() if r["server"] == server]
        return {"server": server, "tools": tools}

    def _poison(self, server, reason):
        self._poisoned[server] = reason
        srv = self._servers.get(server)
        if srv is not None:
            srv.kill()

    def _ensure(self, server, spec):
        if self._closed:
            raise _refuse("mcp manager is closed")
        if server in self._poisoned:
            raise _refuse("mcp server %r unavailable for this run: %s"
                          % (server, self._poisoned[server]))
        if server in self._discovered:
            return
        if not spec["allowed_tools"]:
            self._discovered.add(server)  # nothing permitted: never launch
            return
        t0 = time.monotonic()
        timeout = spec["timeout_s"]
        tool = "initialize+tools/list"
        self._emit({"phase": "start", "server": server, "tool": tool,
                    "args": {}, "timeout_s": timeout})
        wait = lambda: self._emit({"phase": "wait", "server": server, "tool": tool,  # noqa: E731
                                   "timeout_s": timeout,
                                   "duration_s": round(time.monotonic() - t0, 3)})
        status, err = "ok", None
        try:
            srv = _Server(server, spec, self._root)
            self._servers[server] = srv
            self.launch_count[server] = self.launch_count.get(server, 0) + 1
            srv.start()
            deadline = t0 + timeout
            res = srv.request("initialize", {
                "protocolVersion": PROTOCOL_VERSION, "capabilities": {},
                "clientInfo": {"name": "agentcontrol-harness", "version": CLIENT_VERSION}},
                deadline, wait)
            if not isinstance(res, dict) or "__rpc_error__" in res:
                raise MCPError("initialize failed")
            if res.get("protocolVersion") not in SUPPORTED_VERSIONS:
                raise MCPError("unsupported protocolVersion %r"
                               % (res.get("protocolVersion"),))
            srv.notify("notifications/initialized", deadline)
            found, cursor, seen = [], None, set()
            for _ in range(MAX_PAGES):
                page = srv.request("tools/list",
                                   {"cursor": cursor} if cursor else {},
                                   deadline, wait)
                if not isinstance(page, dict) or "__rpc_error__" in page \
                        or not isinstance(page.get("tools"), list):
                    raise MCPError("malformed tools/list response")
                found.extend(page["tools"])
                if len(found) > MAX_TOOLS:
                    raise MCPError("tool catalog exceeds %d" % MAX_TOOLS)
                cursor = page.get("nextCursor")
                if cursor is None:
                    break
                if not isinstance(cursor, str) or cursor in seen:
                    raise MCPError("repeated or invalid pagination cursor")
                seen.add(cursor)
            else:
                raise MCPError("tools/list pagination exceeded %d pages" % MAX_PAGES)
            self._register(server, spec, found)
            self._discovered.add(server)
        except _Timeout:
            status, err = "timeout", "startup/discovery timed out after %ss" % timeout
            self._poison(server, err)
            raise MCPError("mcp server %r: %s" % (server, err))
        except MCPError as e:
            status, err = "error", str(e)
            self._poison(server, "startup failed")
            raise
        except BaseException as e:
            status, err = "error", e.__class__.__name__
            self._poison(server, "startup aborted")
            raise
        finally:
            self._emit({"phase": "end", "server": server, "tool": tool,
                        "timeout_s": timeout, "status": status,
                        "duration_s": round(time.monotonic() - t0, 3),
                        "result": None, "error": err})

    def _register(self, server, spec, found):
        for t in found:
            if not isinstance(t, dict) or not isinstance(t.get("name"), str):
                raise MCPError("malformed tool entry")
            orig = t["name"]
            if not _matches(orig, spec["allowed_tools"]):
                continue
            if not NAME_RE.match(orig):
                continue  # unsupported native characters: not exposed
            full = "%s%s__%s" % (PREFIX, server, orig)
            if len(full) > NATIVE_MAX:
                continue
            if full in self._tools or any(k.lower() == full.lower()
                                          for k in self._tools):
                raise MCPError("ambiguous/duplicate tool name %r" % orig)
            mutating = self._is_mutating(spec, orig, t.get("annotations"))
            # Missing schemas are malformed discovery, not unrestricted objects.
            schema = t.get("inputSchema")
            try:
                size = len(json.dumps(schema, separators=(",", ":"),
                                      allow_nan=False).encode("utf-8"))
            except (TypeError, ValueError):
                size = None
            try:
                if size is None:
                    raise MCPError("unsupported schema: not JSON")
                if size > MAX_SCHEMA_BYTES:
                    raise MCPError("unsupported schema: too large (%d > %d bytes)"
                                   % (size, MAX_SCHEMA_BYTES))
                check_schema(schema)
            except MCPError as e:
                # Fail closed (not exposed) but keep the classified effect so
                # the refusal still reports a mutating tool as mutating.
                self._skipped[full] = {"server": server, "tool": orig,
                                       "reason": str(e), "mutating": mutating}
                continue
            desc = t.get("description", "")
            desc = desc[:MAX_DESCRIPTION] if isinstance(desc, str) else ""
            cost = size + len(full) + len(desc.encode("utf-8"))
            if self._catalog_bytes + cost > MAX_CATALOG_BYTES:
                raise MCPError("tool catalog exceeds %d bytes" % MAX_CATALOG_BYTES)
            self._catalog_bytes += cost
            self._tools[full] = {
                "server": server, "tool": orig, "schema": copy.deepcopy(schema),
                "description": desc, "mutating": mutating,
            }

    def _refuse_skipped(self, name):
        s = self._skipped.get(name)
        if s is not None:
            raise _refuse("mcp tool %r not exposed (%s; %s): %s" % (
                name, "mutating" if s["mutating"] else "read-only",
                "unsupported schema", s["reason"]))

    def native_tools(self):
        out = []
        for full, r in self._tools.items():
            if r["mutating"] and not self._allow_mutation:
                continue
            if r["server"] in self._poisoned:
                continue
            out.append({"type": "function", "function": {
                "name": full, "description": r["description"],
                "parameters": copy.deepcopy(r["schema"])}})
        return out

    def call(self, name, args):
        if not self.owns(name):
            raise _refuse("not an mcp tool name: %r" % (name,))
        rest = name[len(PREFIX):]
        if rest.count("__") < 1:
            raise _refuse("malformed mcp tool name %r" % name)
        server = rest.split("__", 1)[0]
        spec = self._spec(server)
        if server in self._poisoned:
            raise _refuse("mcp server %r unavailable for this run: %s"
                          % (server, self._poisoned[server]))
        rec = self._tools.get(name)
        if rec is None:
            self._refuse_skipped(name)
        if rec is None or rec["server"] != server:
            raise _refuse("unknown or undiscovered mcp tool %r (call list_tools"
                          " for the server first)" % name)
        if not _matches(rec["tool"], spec["allowed_tools"]):
            raise _refuse("mcp tool %r not allowed by policy" % name)
        if self._is_mutating(spec, rec["tool"], None) or rec["mutating"]:
            if not self._allow_mutation:
                raise _refuse("mcp tool %r is mutating; requires --write" % name)
        if not isinstance(args, dict):
            raise _refuse("mcp tool arguments must be a JSON object")
        try:
            json.dumps(args, allow_nan=False)
        except (TypeError, ValueError):
            raise _refuse("mcp tool arguments are not JSON-compatible")
        try:
            validate_args(args, rec["schema"])
        except MCPError as e:
            raise _refuse("invalid arguments for %s: %s" % (name, e))
        with self._lock:
            srv = self._servers.get(server)
            if srv is None or not srv.alive():
                self._poison(server, "server not running")
                raise _refuse("mcp server %r is not running; no restart" % server)
            return self._dispatch(server, srv, spec, rec, name, args)

    def _dispatch(self, server, srv, spec, rec, name, args):
        timeout = spec["timeout_s"]
        t0 = time.monotonic()
        self._emit({"phase": "start", "server": server, "tool": rec["tool"],
                    "args": copy.deepcopy(args), "timeout_s": timeout})
        wait = lambda: self._emit({"phase": "wait", "server": server,  # noqa: E731
                                   "tool": rec["tool"], "timeout_s": timeout,
                                   "duration_s": round(time.monotonic() - t0, 3)})
        status, err, result = "ok", None, None
        try:
            res = srv.request("tools/call", {"name": rec["tool"],
                                             "arguments": args},
                              t0 + timeout, wait)
            if isinstance(res, dict) and "__rpc_error__" in res:
                e = res["__rpc_error__"]
                status = "rpc_error"
                err = e.get("message") if isinstance(e, dict) else str(e)
                result = {"isError": True, "rpcError": e}
            elif not isinstance(res, dict):
                raise MCPError("malformed tools/call result")
            else:
                result = res
                if res.get("isError") is True:
                    status = "tool_error"
            return result
        except _Timeout:
            status = "timeout"
            err = ("tools/call timed out after %ss; outcome unknown (the server "
                   "may have acted); server disabled for this run, no replay"
                   % timeout)
            self._poison(server, "timed out")
            raise MCPError(err)
        except MCPError as e:
            status, err = "error", str(e)
            self._poison(server, "protocol failure")
            raise
        except BaseException as e:
            status, err = "error", e.__class__.__name__
            self._poison(server, "call aborted; outcome unknown")
            raise
        finally:
            self._emit({"phase": "end", "server": server, "tool": rec["tool"],
                        "timeout_s": timeout, "status": status,
                        "duration_s": round(time.monotonic() - t0, 3),
                        "result": result, "error": err})

    def close(self):
        if self._closed:
            return
        self._closed = True
        for srv in list(self._servers.values()):
            try:
                srv.kill()
            except Exception:
                pass
        with _LIVE_LOCK:
            _LIVE.discard(self)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

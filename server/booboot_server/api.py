"""HTTP API: JSON over HTTP/1.1, standard library only."""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import socket
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlsplit

from . import __version__
from .console import clean_text
from .errors import ApiError, BadRequest, NotFound

log = logging.getLogger(__name__)

PREFIX = "/api/v1"
CHUNK = 1 << 20
MAX_JSON = 1 << 20
MAX_WAIT = 60
MAX_TIMEOUT = 3600

ROUTES = []


def route(method, pattern, session=True, raw=False):
    """Register a handler for a path regex.

    session: the request needs the session token.
    raw: the request body is data, not JSON parameters.
    Handlers get (request, match) and return a dict to send, or None.
    """
    def deco(fn):
        ROUTES.append((method, re.compile(PREFIX + pattern + "$"), fn, session, raw))
        return fn
    return deco


class Body:
    """Request body reader for Content-Length and chunked bodies."""

    def __init__(self, rfile, headers):
        self._rfile = rfile
        self._chunked = "chunked" in headers.get("Transfer-Encoding", "").lower()
        try:
            self._left = 0 if self._chunked else int(headers.get("Content-Length") or 0)
        except ValueError:
            raise BadRequest("bad Content-Length") from None
        self.done = not self._chunked and self._left == 0

    def read(self, n=CHUNK):
        if self.done:
            return b""
        if self._chunked and self._left == 0:
            line = self._rfile.readline(1024)
            try:
                size = int(line.split(b";")[0].strip(), 16)
            except ValueError:
                raise BadRequest("bad chunked body") from None
            if size == 0:
                while self._rfile.readline(1024).strip():
                    pass  # trailer lines
                self.done = True
                return b""
            self._left = size
        data = self._rfile.read(min(n, self._left))
        if not data:
            raise BadRequest("upload interrupted")
        self._left -= len(data)
        if self._left == 0:
            if self._chunked:
                self._rfile.readline(1024)  # CRLF after the chunk
            else:
                self.done = True
        return data


class MethodNotAllowed(ApiError):
    status = 405
    code = "method_not_allowed"


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "booboot/" + __version__
    timeout = 300  # seconds without network activity

    _body = None
    _sent = False

    @property
    def dut(self):
        return self.server.dut

    @property
    def body(self):
        return self._body

    def do_GET(self):
        self._handle()

    def do_PUT(self):
        self._handle()

    def do_POST(self):
        self._handle()

    def do_DELETE(self):
        self._handle()

    def log_request(self, code="-", size="-"):
        quiet = (self.command == "GET" and str(code).startswith("2")
                 and self.path.startswith((PREFIX + "/console", PREFIX + "/status")))
        if not quiet:
            log.info('%s "%s %s" %s', self.client_address[0], self.command, self.path, code)

    def log_message(self, fmt, *args):
        log.info("%s %s", self.client_address[0], fmt % args)

    def token(self):
        auth = self.headers.get("Authorization", "")
        return auth[7:].strip() if auth[:7].lower() == "bearer " else ""

    def _route(self):
        url = urlsplit(self.path)
        found = False
        for method, rx, fn, session, raw in ROUTES:
            m = rx.match(url.path)
            if m:
                if method == self.command:
                    return fn, m, session, raw, url
                found = True
        if found:
            raise MethodNotAllowed("%s not allowed on %s" % (self.command, url.path))
        raise NotFound("no such endpoint: %s" % url.path)

    def handle_expect_100(self):
        """Refuse an upload before its body is sent if the client may not use the DUT."""
        self._body = None
        self._sent = False
        try:
            session = self._route()[2]
            if session:
                self.server.sessions.check(self.token())
        except ApiError as e:
            self._fail(e)
            return False
        return super().handle_expect_100()

    def _handle(self):
        self._body = None
        self._sent = False
        self._json = {}
        token = self.token()
        begun = False
        try:
            self._body = Body(self.rfile, self.headers)
            fn, m, session, raw, url = self._route()
            self._query = parse_qs(url.query)
            if not raw:
                self._json = self._read_params()
            if session:
                self.server.sessions.begin(token)
                begun = True
            result = fn(self, m)
            if result is not None:
                self.send_json(200, result)
        except ApiError as e:
            self._fail(e)
        except (BrokenPipeError, ConnectionResetError, socket.timeout):
            self.close_connection = True
        except Exception as e:
            log.exception("request failed")
            self._fail(ApiError("internal error: %s" % e))
        finally:
            if begun:
                self.server.sessions.end(token)
            if self._body is None or not self._body.done:
                self.close_connection = True

    def _read_params(self):
        raw = bytearray()
        while True:
            chunk = self._body.read(65536)
            if not chunk:
                break
            raw += chunk
            if len(raw) > MAX_JSON:
                raise BadRequest("request body too large")
        text = raw.decode("utf-8", "replace").strip()
        if not text:
            return {}
        try:
            obj = json.loads(text)
        except ValueError:
            if "form-urlencoded" in self.headers.get("Content-Type", ""):
                return {k: v[-1] for k, v in parse_qs(text).items()}
            raise BadRequest("request body is not valid JSON") from None
        if not isinstance(obj, dict):
            raise BadRequest("request body must be a JSON object")
        return obj

    def _fail(self, e):
        if e.status >= 500:
            log.error("%s %s: %s", self.command, self.path, e.message)
        if self._sent:
            self.close_connection = True
            return
        try:
            self.send_json(e.status, e.to_dict())
        except OSError:
            self.close_connection = True

    def _end_headers(self):
        if self._body is None or not self._body.done:
            self.send_header("Connection", "close")
            self.close_connection = True
        self.end_headers()
        self._sent = True

    def send_json(self, status, obj):
        data = (json.dumps(obj, indent=2) + "\n").encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self._end_headers()
        self.wfile.write(data)

    def send_data_headers(self, length, headers=()):
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(length))
        for key, value in headers:
            self.send_header(key, value)
        self._end_headers()

    # Parameters come from the JSON body, or else from the query string.

    def param(self, name, default=None):
        value = self._json.get(name)
        if value is not None:
            return value
        values = self._query.get(name)
        return values[-1] if values else default

    def param_bool(self, name, default=False):
        value = self.param(name)
        if value is None:
            return default
        text = str(value).lower()
        if text in ("1", "true", "yes", "on"):
            return True
        if text in ("0", "false", "no", "off", ""):
            return False
        raise BadRequest("%s must be true or false" % name)

    def param_float(self, name, default=None, low=0.0, high=None):
        value = self.param(name)
        if value is None:
            return default
        try:
            number = float(value)
        except (TypeError, ValueError):
            raise BadRequest("%s must be a number" % name) from None
        if number < low or (high is not None and number > high):
            raise BadRequest("%s must be between %g and %g" % (name, low, high or float("inf")))
        return number


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, dut, sessions, prompt):
        if ":" in address[0]:
            self.address_family = socket.AF_INET6
        super().__init__(address, Handler)
        self.dut = dut
        self.sessions = sessions
        self.prompt = prompt


# Session

@route("GET", "/status", session=False)
def get_status(r, m):
    status = r.dut.status()
    status["session"] = r.server.sessions.status(r.token())
    return status


@route("POST", "/session", session=False)
def open_session(r, m):
    client = str(r.param("client") or r.client_address[0])
    timeout = r.param_float("timeout")
    token, info = r.server.sessions.open(client, timeout, r.param_bool("force"))
    info["session"] = token
    return info


@route("DELETE", "/session")
def close_session(r, m):
    r.server.sessions.close(r.token())
    return {"closed": True}


@route("POST", "/session/keepalive")
def keepalive(r, m):
    return r.server.sessions.status(r.token())


# Power

@route("PUT", "/power")
def put_power(r, m):
    state = r.param("state")
    if state not in ("on", "off"):
        raise BadRequest("state must be on or off")
    return r.dut.set_power(state == "on")


@route("POST", "/power/cycle")
def power_cycle(r, m):
    return r.dut.power_cycle(r.param_float("off_time", high=60))


# SD card

@route("PUT", "/sd")
def put_sd(r, m):
    return r.dut.set_sd(r.param("mode"))


@route("PUT", "/sd/image", raw=True)
def put_image(r, m):
    return r.dut.write_image(r.body, r.param("compression", "auto"), r.param_bool("verify"))


@route("GET", "/sd/partitions")
def get_partitions(r, m):
    return r.dut.card_info()


@route("GET", r"/sd/files/(\d+)(/.*)?")
def get_file(r, m):
    part, path = int(m.group(1)), unquote(m.group(2) or "/")
    with r.dut.sd_open(part, path) as (kind, value):
        if kind == "dir":
            return {"path": path, "entries": value}
        r.send_data_headers(os.fstat(value.fileno()).st_size)
        shutil.copyfileobj(value, r.wfile, CHUNK)
    return None


@route("PUT", r"/sd/files/(\d+)(/.*)", raw=True)
def put_file(r, m):
    return r.dut.sd_put(int(m.group(1)), unquote(m.group(2)), r.body, r.param_bool("dir"))


@route("DELETE", r"/sd/files/(\d+)(/.*)")
def delete_file(r, m):
    return r.dut.sd_delete(int(m.group(1)), unquote(m.group(2)), r.param_bool("recursive"))


# Console

def _since(r, default):
    return r.dut.console.resolve(str(r.param("since", default)))


def _text(r, data, clean=False):
    text = data.decode("utf-8", "replace")
    return clean_text(text) if r.param_bool("clean", clean) else text


def _regex(r, name, default=None):
    pattern = r.param(name, default)
    if not pattern:
        raise BadRequest("%s is required" % name)
    try:
        return re.compile(str(pattern).encode("utf-8"))
    except re.error as e:
        raise BadRequest("bad %s regex: %s" % (name, e)) from None


@route("GET", "/console")
def get_console(r, m):
    c = r.dut.console
    since = _since(r, "boot")
    wait = r.param_float("wait", 0.0, high=MAX_WAIT)
    start, data = c.read(since, int(r.param_float("max", CHUNK, low=1)), wait)
    nxt = start + len(data)
    if r.param("format") == "raw":
        r.send_data_headers(len(data), [("X-Cursor", str(start)), ("X-Next", str(nxt))])
        r.wfile.write(data)
        return None
    return {"cursor": start, "next": nxt, "lost": start > since, "boot": c.boot, "last": c.last,
            "text": _text(r, data)}


@route("POST", "/console/write")
def console_write(r, m):
    c = r.dut.console
    data = str(r.param("text", "")).encode("utf-8")
    if r.param_bool("newline"):
        data += c.eol
    c.write(data)
    return {"written": len(data), "cursor": c.end}


@route("POST", "/console/expect")
def console_expect(r, m):
    pattern = _regex(r, "pattern")
    since = _since(r, "last")
    timeout = r.param_float("timeout", 30.0, high=MAX_TIMEOUT)
    matched, start, data, match, nxt = r.dut.console.expect(pattern, since, timeout)
    return {"matched": matched, "match": match.group(0).decode("utf-8", "replace") if match else None,
            "cursor": start, "next": nxt, "text": _text(r, data)}


@route("POST", "/console/run")
def console_run(r, m):
    command = r.param("command")
    if command is None:
        raise BadRequest("command is required")
    pattern = _regex(r, "prompt", r.server.prompt)
    timeout = r.param_float("timeout", 30.0, high=MAX_TIMEOUT)
    matched, out, nxt = r.dut.console.run(str(command), pattern, timeout)
    return {"matched": matched, "next": nxt, "output": _text(r, out, clean=True)}

"""HTTP API: JSON over HTTP/1.1, standard library only."""

from __future__ import annotations

import codecs
import json
import logging
import os
import re
import select
import shutil
import socket
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlsplit

from . import __version__, netinfo
from .console import clean_text
from .errors import ApiError, BadRequest, NotFound
from .scripts import Scripts

log = logging.getLogger(__name__)

PREFIX = "/api/v1"
CHUNK = 1 << 20
MAX_JSON = 1 << 20
MAX_WAIT = 60
MAX_TIMEOUT = 3600
PING = 10  # seconds between pings on an idle console stream
WEB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")
WEB_TYPES = {"html": "text/html; charset=utf-8", "js": "text/javascript; charset=utf-8",
             "css": "text/css; charset=utf-8", "svg": "image/svg+xml", "ico": "image/x-icon"}

ROUTES = []


def route(method, pattern, session=True, raw=False, prefix=PREFIX):
    """Register a handler for a path regex, under prefix.

    session: the request needs the session token. "optional": a valid token
    keeps the session alive, but none is needed.
    raw: the request body is data, not JSON parameters.
    Handlers get (request, match) and return a dict to send, or None.
    """
    def deco(fn):
        ROUTES.append((method, re.compile(prefix + pattern + "$"), fn, session, raw))
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
        # Reads, the writes of typed keys and heartbeats would fill the log.
        quiet = str(code).startswith("2") and (
            (self.command == "GET" and self.path.startswith((PREFIX + "/console", PREFIX + "/status",
                                                             PREFIX + "/scripts")))
            or (self.command == "POST" and self.path.startswith((PREFIX + "/console/write",
                                                                 PREFIX + "/session/heartbeat"))))
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
            if session is True:
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
            if session == "optional":
                if token:
                    try:
                        self.server.sessions.begin(token)
                        begun = True
                    except ApiError:
                        pass
            elif session:
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

    def send_data_headers(self, length, headers=(), content_type="application/octet-stream"):
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(length))
        for key, value in headers:
            self.send_header(key, value)
        self._end_headers()

    def send_stream_headers(self, content_type):
        """Start an answer that ends when the connection closes."""
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.close_connection = True
        self.end_headers()
        self._sent = True
        self.connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)

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

    def __init__(self, address, dut, sessions, prompt, web=True, scripts=None):
        if ":" in address[0]:
            self.address_family = socket.AF_INET6
        super().__init__(address, Handler)
        self.dut = dut
        self.sessions = sessions
        self.prompt = prompt
        self.web = web
        self.scripts = scripts if scripts is not None else Scripts("", sessions)
        # Scripts reach this server on the board itself.
        host, port = self.server_address[:2]
        host = {"0.0.0.0": "127.0.0.1", "::": "::1"}.get(host, host)
        self.scripts.url = "http://%s:%d" % ("[%s]" % host if ":" in host else host, port)


# Session

@route("GET", "/status", session=False)
def get_status(r, m):
    status = r.dut.status()
    status["session"] = r.server.sessions.status(r.token())
    status["network"] = netinfo.info()
    status["script"] = r.server.scripts.running()
    return status


@route("POST", "/session", session=False)
def open_session(r, m):
    client = str(r.param("client") or r.client_address[0])
    timeout = r.param_float("timeout")
    force = "gone" if r.param("force") == "gone" else r.param_bool("force")
    token, info = r.server.sessions.open(client, timeout, force)
    info["session"] = token
    return info


@route("DELETE", "/session")
def close_session(r, m):
    if r.server.sessions.close(r.token()):
        return {"closed": True}
    # It ends when its script ends.
    return {"closed": False, "script": r.server.scripts.running()}


@route("POST", "/session/keepalive")
def keepalive(r, m):
    return r.server.sessions.status(r.token())


@route("POST", "/session/heartbeat", session=False)
def heartbeat(r, m):
    return r.server.sessions.heartbeat(r.token())


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


def _time(c, cursor):
    t = c.time_of(cursor)
    return None if t is None else round(t, 3)


@route("GET", "/console", session="optional")
def get_console(r, m):
    c = r.dut.console
    since = _since(r, "boot")
    wait = r.param_float("wait", 0.0, high=MAX_WAIT)
    start, data = c.read(since, int(r.param_float("max", CHUNK, low=1)), wait)
    nxt = start + len(data)
    if r.param_bool("timestamps"):
        data = c.stamped(start, data)
    if r.param("format") == "raw":
        r.send_data_headers(len(data), [("X-Cursor", str(start)), ("X-Next", str(nxt))])
        r.wfile.write(data)
        return None
    return {"cursor": start, "next": nxt, "lost": start > since, "boot": c.boot, "last": c.last,
            "text": _text(r, data)}


# Not tied to the session: a stream lasts as long as its client wants.
@route("GET", "/console/stream", session=False)
def console_stream(r, m):
    c = r.dut.console
    raw = r.param("format") == "raw"
    start = max(min(_since(r, "boot"), c.resolve("now")), c.resolve("start"))
    items = c.follow(start)
    r.send_stream_headers("application/octet-stream" if raw else "application/x-ndjson")
    decoder = codecs.getincrementaldecoder("utf-8")("replace")

    def send(obj):
        r.wfile.write(json.dumps(obj, ensure_ascii=False).encode("utf-8") + b"\n")

    if not raw:
        send({"type": "hello", "name": r.dut.name, "version": __version__, "started": round(c.started, 3),
              "time": round(time.time(), 3), "power": r.dut.power_state, "cursor": start, "end": c.resolve("now")})
    pinged = time.monotonic()
    for item in items:
        if item is None:
            if _gone(r.connection):
                return None
            if not raw and time.monotonic() - pinged >= PING:
                send({"type": "ping"})
                pinged = time.monotonic()
        elif raw:
            if item[0] == "output":
                r.wfile.write(item[2])
        elif item[0] == "output":
            send({"type": "output", "cursor": item[1], "next": item[1] + len(item[2]),
                  "text": decoder.decode(item[2])})
            pinged = time.monotonic()
        else:
            send({"type": "power", "state": "on" if item[2] else "off", "cursor": item[1],
                  "time": round(item[3], 3)})
            pinged = time.monotonic()
    return None


def _gone(sock):
    """Return True if the client closed the connection."""
    try:
        if not select.select([sock], [], [], 0)[0]:
            return False
        return not sock.recv(1, socket.MSG_PEEK)
    except OSError:
        return True


# Scripts run on this board. Starting and stopping one needs the session; reading, not.

@route("POST", "/scripts")
def start_script(r, m):
    token = r.token()
    client = r.server.sessions.status(token)["client"]
    return r.server.scripts.start(token, client, r.param("source"), str(r.param("name") or "script.py"),
                                  r.param("args") or [], r.param_float("timeout"))


@route("GET", "/scripts", session=False)
def list_scripts(r, m):
    return {"enabled": r.server.scripts.enabled, "scripts": r.server.scripts.list()}


@route("GET", r"/scripts/(\d+)", session=False)
def get_script(r, m):
    return r.server.scripts.info(int(m.group(1)))


@route("GET", r"/scripts/(\d+)/output", session=False)
def script_output(r, m):
    try:
        since = int(r.param("since", 0))
    except (TypeError, ValueError):
        raise BadRequest("since must be a number") from None
    wait = r.param_float("wait", 0.0, high=MAX_WAIT)
    start, data, info = r.server.scripts.read(int(m.group(1)), since, int(r.param_float("max", CHUNK, low=1)), wait)
    return {"cursor": start, "next": start + len(data), "lost": start > since >= 0, "text": _text(r, data),
            "script": info}


@route("POST", r"/scripts/(\d+)/stop")
def stop_script(r, m):
    client = r.server.sessions.status(r.token())["client"]
    return r.server.scripts.stop(int(m.group(1)), "stopped by %s" % client)


# Log files of the console on this board, one per boot. No session needed.

@route("GET", "/logs", session=False)
def get_logs(r, m):
    files = r.dut.console.log
    return {"files": files.files() if files else [], "current": os.path.basename(files.path) if files else ""}


@route("GET", "/logs/([^/]+)", session=False)
def get_log(r, m):
    files = r.dut.console.log
    path = files.file_path(unquote(m.group(1))) if files else None
    if not path:
        raise NotFound("no such log file: %s" % unquote(m.group(1)))
    with open(path, "rb") as f:
        # The current file grows: send the size announced, no more.
        left = os.fstat(f.fileno()).st_size
        r.send_data_headers(left, content_type="text/plain; charset=utf-8")
        while left > 0:
            data = f.read(min(CHUNK, left))
            if not data:
                break
            r.wfile.write(data)
            left -= len(data)
    return None


# The console web page. Not under /api/v1.

@route("GET", r"/([a-z]+\.(%s))?" % "|".join(WEB_TYPES), session=False, prefix="")
def web_file(r, m):
    if not r.server.web:
        raise NotFound("the web page is turned off ([server] web = no)")
    name = m.group(1) or "index.html"
    try:
        with open(os.path.join(WEB_DIR, name), "rb") as f:
            data = f.read()
    except FileNotFoundError:
        raise NotFound("no such file: /%s" % name) from None
    r.send_data_headers(len(data), [("Cache-Control", "no-cache")], WEB_TYPES[name.rsplit(".", 1)[1]])
    r.wfile.write(data)
    return None


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
    c = r.dut.console
    matched, start, data, match, nxt = c.expect(pattern, since, timeout)
    return {"matched": matched, "match": match.group(0).decode("utf-8", "replace") if match else None,
            "cursor": start, "next": nxt, "time": _time(c, nxt - 1) if matched else None,
            "text": _text(r, data)}


@route("POST", "/console/run")
def console_run(r, m):
    command = r.param("command")
    if command is None:
        raise BadRequest("command is required")
    pattern = _regex(r, "prompt", r.server.prompt)
    timeout = r.param_float("timeout", 30.0, high=MAX_TIMEOUT)
    c = r.dut.console
    matched, out, nxt = c.run(str(command), pattern, timeout)
    return {"matched": matched, "next": nxt, "time": _time(c, nxt - 1) if matched else None,
            "output": _text(r, out, clean=True)}

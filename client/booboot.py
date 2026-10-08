#!/usr/bin/env python3
"""BooBoot client: Python library and command line tool.

Needs Python 3.9 or later and nothing else. Works on Linux, macOS and
Windows. Copy this file anywhere and run it, or import it:

    import booboot

    dut = booboot.Client("http://booboot.local:8080")
    dut.open_session("my-script")
    dut.power_off()
    dut.put_file("BOOT.BIN", "1:/BOOT.BIN")
    dut.power_on()
    print(dut.expect("login: ", since="boot", timeout=120)["text"])
    dut.close_session()
"""

from __future__ import annotations

import argparse
import codecs
import contextlib
import getpass
import json
import os
import posixpath
import queue
import re
import socket
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

__version__ = "dev"  # set by install.sh and in releases

DEFAULT_URL = "http://booboot.local:8080"
CHUNK = 1 << 20

HEARTBEAT = 10  # seconds between heartbeats

EXIT_ERROR = 1
EXIT_TIMEOUT = 3
EXIT_BUSY = 4


class Error(Exception):
    """An error from the server, or a connection problem.

    code is the server error code, for example "busy" or "no_session".
    info has the full error answer of the server.
    """

    def __init__(self, message, status=0, code="error", info=None):
        super().__init__(message)
        self.message = message
        self.status = status
        self.code = code
        self.info = info or {}


def split_path(remote):
    """Split "N:/path" into (N, "/path"). The partition is 1 when not given."""
    m = re.match(r"^(\d+):(.*)$", remote)
    if m:
        return int(m.group(1)), m.group(2) or "/"
    return 1, remote or "/"


def dut_url(url, name):
    """The URL of DUT name of the board at url: URL/duts/NAME."""
    return re.sub(r"/duts/[^/]+/?$", "", url.rstrip("/")) + "/duts/" + urllib.parse.quote(name)


def default_client_name():
    try:
        user = getpass.getuser()
    except Exception:
        user = "user"
    return "%s@%s" % (user, socket.gethostname())


def _http_error(e):
    try:
        info = json.loads(e.read().decode("utf-8"))
    except Exception:
        info = {}
    if not isinstance(info, dict):
        info = {}
    message = info.get("message") or "HTTP %d %s" % (e.code, e.reason)
    return Error(message, e.code, info.get("error", "error"), info)


class _Upload:
    """File wrapper that reads big blocks and reports progress."""

    def __init__(self, f, size, progress):
        self._f = f
        self._size = size
        self._done = 0
        self._progress = progress

    def read(self, n=-1):
        data = self._f.read(CHUNK)
        self._done += len(data)
        if self._progress:
            self._progress(self._done, self._size)
        return data


def _copy(src, dst):
    total = 0
    while True:
        data = src.read(CHUNK)
        if not data:
            return total
        dst.write(data)
        total += len(data)


class Client:
    """Access to one BooBoot server, that is one DUT.

    All calls but status(), open_session(), read(), read_raw(), stream() and
    the script reads (scripts(), script(), script_output(), follow_script())
    need the session.
    Errors raise booboot.Error.

    When the host name of url is not found, like a .local name over a VPN,
    the client tries the name without .local, then the addresses given, and
    goes on with the first one that answers. on_note(text) is told which.
    """

    @classmethod
    def from_env(cls, **kwargs):
        """Client of BOOBOOT_URL with the session BOOBOOT_SESSION, as a script run on the board gets them.

        Without BOOBOOT_SESSION, like on a desktop computer, it opens a session.
        """
        c = cls(os.environ.get("BOOBOOT_URL", DEFAULT_URL), session=os.environ.get("BOOBOOT_SESSION") or None,
                **kwargs)
        if not c.session:
            c.open_session()
        return c

    def __init__(self, url=DEFAULT_URL, session=None, timeout=30.0, addresses=(), on_note=None):
        self.url = url.rstrip("/")
        self.base = self.url  # where the requests go: url, or another address of the server
        self.addresses = list(addresses)
        self.on_note = on_note
        self.session = session
        self.timeout = timeout
        # No proxy: the server is on the local network.
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def request(self, method, path, params=None, json_body=None, data=None, headers=None,
                timeout=None, stream=False):
        """Send a request and return the decoded JSON answer.

        With stream=True, return the open response instead.
        """
        url = self.base + "/api/v1" + path
        params = {k: v for k, v in (params or {}).items() if v is not None}
        if params:
            url += "?" + urllib.parse.urlencode(params)
        hdrs = dict(headers or {})
        if self.session:
            hdrs["Authorization"] = "Bearer " + self.session
        if json_body is not None:
            body = {k: v for k, v in json_body.items() if v is not None}
            data = json.dumps(body).encode("utf-8")
            hdrs["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
        try:
            resp = self._opener.open(req, timeout=timeout or self.timeout)
            if stream:
                return resp
            with resp:
                answer = resp.read()
        except urllib.error.HTTPError as e:
            raise _http_error(e) from None
        except (urllib.error.URLError, OSError) as e:
            # The name was not found: nothing was sent, the request can go to another address.
            if self._other_address(e):
                return self.request(method, path, params, json_body, data, headers, timeout, stream)
            hint = ""
            if (urllib.parse.urlsplit(self.url).hostname or "").endswith(".local"):
                hint = " (.local names work only on the local network: use the name or address of the board)"
            raise Error("cannot reach %s: %s%s" % (self.url, getattr(e, "reason", e), hint)) from None
        return json.loads(answer.decode("utf-8")) if answer else None

    def _other_address(self, error):
        """After a failed name lookup, switch to the first other address of the server that answers."""
        if self.base != self.url or not isinstance(getattr(error, "reason", error), socket.gaierror):
            return False
        parts = urllib.parse.urlsplit(self.url)
        name = parts.hostname or ""
        others = ([name[:-len(".local")]] if name.endswith(".local") else []) + self.addresses
        for other in others:
            host = "[%s]" % other if ":" in other else other
            base = "%s://%s%s%s" % (parts.scheme, host, ":%d" % parts.port if parts.port else "",
                                    parts.path.rstrip("/"))
            try:
                self._opener.open(base + "/api/v1/status", timeout=3).close()
            except OSError:
                continue
            self.base = base
            if self.on_note:
                self.on_note("%s not found, using %s" % (name, other))
            return True
        return False

    # Session

    def status(self):
        """Return the state of power, SD card, console and session."""
        return self.request("GET", "/status")

    def duts(self):
        """The DUTs of the board: {"duts": [{"name", "url", "power", "session", ...}]}.

        "url" is the path of the DUT on the board, like /duts/dut2: see dut_url().
        """
        return self.request("GET", "/duts")

    def set_label(self, label):
        """Set the label of the DUT, free text shown with its name. Empty: none. Needs the session."""
        return self.request("PUT", "/label", json_body={"label": label})

    def open_session(self, client=None, timeout=None, force=False):
        """Open the session needed by all other calls.

        client: name shown to other clients.
        timeout: idle seconds after which the server ends the session.
        force: True takes the session from another client. "gone" takes it only
        from a client that is gone: one that sent heartbeats, and stopped.
        """
        info = self.request("POST", "/session", json_body={
            "client": client or default_client_name(), "timeout": timeout, "force": force or None})
        self.session = info["session"]
        return info

    def close_session(self):
        """Release the session. While a script of the session runs, it ends with the script: "closed" is false."""
        if self.session:
            try:
                return self.request("DELETE", "/session")
            finally:
                self.session = None
        return None

    def keepalive(self):
        return self.request("POST", "/session/keepalive")

    def heartbeat(self):
        """Tell the server that this client is still there. It does not count as activity.

        A client that stays connected sends it every HEARTBEAT seconds while it
        has the session, so that others see when it is gone.
        """
        return self.request("POST", "/session/heartbeat")

    # Power

    def power_on(self):
        """Power on. The SD card goes to the DUT first. Result has the boot cursor."""
        return self.request("PUT", "/power", json_body={"state": "on"})

    def power_off(self):
        return self.request("PUT", "/power", json_body={"state": "off"})

    def power_cycle(self, off_time=None):
        return self.request("POST", "/power/cycle", json_body={"off_time": off_time})

    # SD card

    def sd_mode(self, mode):
        """Connect the card to "host" (the BooBoot board), "dut", or "off"."""
        return self.request("PUT", "/sd", json_body={"mode": mode}, timeout=60)

    def write_image(self, path, verify=False, compression="auto", progress=None):
        """Write a disk image (raw, gz, xz or bz2) to the card. Power must be off."""
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            return self.request(
                "PUT", "/sd/image", params={"compression": compression, "verify": int(verify)},
                data=_Upload(f, size, progress),
                headers={"Content-Type": "application/octet-stream", "Content-Length": str(size)},
                timeout=3600)

    def partitions(self):
        return self.request("GET", "/sd/partitions", timeout=120)

    def _file_path(self, remote):
        part, path = split_path(remote)
        return "/sd/files/%d/%s" % (part, urllib.parse.quote(path.lstrip("/")))

    def list_dir(self, remote="1:/"):
        """Return the entries of a card directory, like "1:/" or "2:/etc"."""
        with self.request("GET", self._file_path(remote), timeout=120, stream=True) as resp:
            body = resp.read()
            if "json" not in resp.headers.get("Content-Type", ""):
                raise Error("not a directory: %s" % remote, code="not_a_directory")
        return json.loads(body.decode("utf-8"))["entries"]

    def get_file(self, remote, local):
        """Copy a card file to a local path or binary file object. Return the size."""
        with self.request("GET", self._file_path(remote), timeout=120, stream=True) as resp:
            if "json" in resp.headers.get("Content-Type", ""):
                raise Error("is a directory: %s" % remote, code="is_a_directory")
            if hasattr(local, "write"):
                return _copy(resp, local)
            with open(local, "wb") as f:
                return _copy(resp, f)

    def put_file(self, local, remote, progress=None):
        """Copy a local file to a card path like "1:/BOOT.BIN". Parent folders are created."""
        size = os.path.getsize(local)
        with open(local, "rb") as f:
            return self.request(
                "PUT", self._file_path(remote), data=_Upload(f, size, progress),
                headers={"Content-Type": "application/octet-stream", "Content-Length": str(size)},
                timeout=600)

    def mkdir(self, remote):
        return self.request("PUT", self._file_path(remote), params={"dir": 1}, data=b"", timeout=120)

    def delete(self, remote, recursive=False):
        params = {"recursive": 1} if recursive else None
        return self.request("DELETE", self._file_path(remote), params=params, timeout=120)

    # Console
    #
    # Each console byte has a cursor. "since" is a cursor, a negative number
    # (bytes before the end), or one of: "start" (oldest byte kept), "boot"
    # (last power on), "last" (end of the last expect or run match), "now".

    def read(self, since="boot", wait=0, clean=False, max_bytes=None, timestamps=False):
        """Return console output: {"text", "cursor", "next", ...}.

        wait: seconds to wait for new output when there is none.
        clean: remove escape codes and carriage returns.
        timestamps: start each line with its time since power on.
        """
        params = {"since": since, "wait": wait or None, "clean": 1 if clean else None, "max": max_bytes,
                  "timestamps": 1 if timestamps else None}
        return self.request("GET", "/console", params=params, timeout=wait + self.timeout)

    def read_raw(self, since="boot", wait=0, timestamps=False):
        """Return (bytes, next cursor)."""
        params = {"since": since, "wait": wait or None, "format": "raw", "timestamps": 1 if timestamps else None}
        try:
            with self.request("GET", "/console", params=params, timeout=wait + self.timeout,
                              stream=True) as resp:
                return resp.read(), int(resp.headers.get("X-Next", "0"))
        except OSError as e:
            raise Error("cannot reach %s: %s" % (self.url, e)) from None

    def stream(self, since="boot"):
        """Yield the console events as they come, as dicts. "type" is:

        hello: first event, with "name", "power" (state), "cursor" (first cursor),
        "end" (cursor at the call), "time" and "started" (Unix time of the
        server start: cursors count from it);
        output: "text" from "cursor" to "next";
        power: "state" (on or off) switched at "cursor", at "time" (Unix time);
        ping: sent after 10 s without other events.
        """
        try:
            with self.request("GET", "/console/stream", params={"since": since}, timeout=30,
                              stream=True) as resp:
                for line in resp:
                    yield json.loads(line)
        except OSError as e:
            raise Error("cannot reach %s: %s" % (self.url, e)) from None

    def write(self, text, newline=False):
        """Send text. newline adds the line ending set on the server."""
        return self.request("POST", "/console/write", json_body={"text": text, "newline": newline})

    def expect(self, pattern, since="last", timeout=30, clean=False, switch=None):
        """Wait for a regex in the console output.

        Result: "matched", "match", "text" (output up to the end of the match),
        "next" (cursor after the match) and "time" (seconds from power on to the match).
        A power switch before the match raises Error "power_switched": the output after it
        is from another boot. switch: the "switch" of the power on of the boot to wait in,
        to also catch a switch made before this call.
        """
        body = {"pattern": pattern, "since": since, "timeout": timeout, "clean": clean, "switch": switch}
        return self.request("POST", "/console/expect", json_body=body, timeout=timeout + self.timeout)

    def run(self, command, prompt=None, timeout=30, clean=True, switch=None):
        """Send a command line and wait for the prompt regex.

        Result: "matched", "output" (without the command echo and the prompt line)
        and "time" (seconds from power on to the prompt). A power switch raises Error
        "power_switched", as for expect().
        """
        body = {"command": command, "prompt": prompt, "timeout": timeout, "clean": clean, "switch": switch}
        return self.request("POST", "/console/run", json_body=body, timeout=timeout + self.timeout)

    def boot_time(self, pattern, timeout=120, off_time=None):
        """Power cycle the DUT and wait for a regex, like a login prompt.

        Return the seconds from power on to the regex, or None on timeout.
        """
        r = self.expect_boot(self.power_cycle(off_time), pattern, timeout)
        return r["time"] if r["matched"] else None

    def expect_boot(self, power_on, pattern, timeout=120, clean=False):
        """expect() in the boot started by power_on, the result of power_on() or power_cycle().

        Another power switch in between raises Error "power_switched", rather than reading another boot.
        """
        return self.expect(pattern, since=power_on.get("boot", "boot"), timeout=timeout, clean=clean,
                           switch=power_on.get("switch"))

    # Scripts

    def run_script(self, source, name="script.py", args=(), timeout=None):
        """Start a Python script on the BooBoot board, in this session. Return its information.

        The script uses the DUT with Client.from_env(), in this session. It goes
        on when this client disconnects: its output is kept on the board.
        source: text of the script. args: its arguments.
        timeout: seconds after which it is stopped.
        """
        return self.request("POST", "/scripts", json_body={
            "source": source, "name": name, "args": list(args), "timeout": timeout})

    def scripts(self):
        """The scripts kept on the board, the newest first: {"enabled", "scripts"}."""
        return self.request("GET", "/scripts")

    def script(self, script_id):
        """Information on a script: "state" (running, exited or stopped), "exit_code", "reason", ..."""
        return self.request("GET", "/scripts/%d" % script_id)

    def script_output(self, script_id, since=0, wait=0, clean=False, max_bytes=None):
        """Output of a script from a cursor: {"text", "cursor", "next", "lost", "script"}.

        since: cursor, or a negative number of bytes before the end.
        wait: seconds to wait for new output while the script runs.
        """
        params = {"since": since, "wait": wait or None, "clean": 1 if clean else None, "max": max_bytes}
        return self.request("GET", "/scripts/%d/output" % script_id, params=params, timeout=wait + self.timeout)

    def follow_script(self, script_id, since=0):
        """Yield the output of a script as (text, next cursor), until the script ends."""
        while True:
            r = self.script_output(script_id, since, wait=10)
            since = r["next"]
            if r["text"]:
                yield r["text"], since
            if r["script"]["state"] != "running" and since >= r["script"]["output"]:
                return

    def stop_script(self, script_id):
        return self.request("POST", "/scripts/%d/stop" % script_id)


# Command line

EPILOG = """\
Card paths are N:/path, N being the partition number. "/path" means "1:/path".
Commands open a session when needed and keep its token in a local file.

examples:
  booboot status
  booboot deploy BOOT.BIN image.ub --expect "login: " --timeout 120
  booboot console run "uname -a"
  booboot boottime "login: " --runs 5
  booboot sd put BOOT.BIN image.ub 1:/
  booboot console attach
  booboot script run soak.py 500

exit codes: 0 ok, 1 error, 2 bad arguments, 3 timeout (pattern not seen),
4 busy (another client has the session)
"""


def _store_path():
    base = (os.environ.get("LOCALAPPDATA") or os.environ.get("XDG_CACHE_HOME")
            or os.path.join(os.path.expanduser("~"), ".cache"))
    return os.path.join(base, "booboot", "sessions.json")


def _load_store():
    try:
        with open(_store_path()) as f:
            store = json.load(f)
        return store if isinstance(store, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_json(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(obj, f, indent=1)
    os.replace(tmp, path)


def _store_key(url, name):
    # Each client name has its own session, like agents that run at the same time.
    url = url.rstrip("/")
    return url + " " + name if name else url


def _save_token(url, token, name=None):
    store = _load_store()
    if token:
        store[_store_key(url, name)] = token
    else:
        store.pop(_store_key(url, name), None)
    _save_json(_store_path(), store)


def _addresses_path():
    return os.path.join(os.path.dirname(_store_path()), "addresses.json")


def _load_addresses():
    try:
        with open(_addresses_path()) as f:
            store = json.load(f)
        return store if isinstance(store, dict) else {}
    except (OSError, ValueError):
        return {}


def known_addresses(url):
    """The addresses that the server at url reported last."""
    found = _load_addresses().get(url.rstrip("/"))
    return [a for a in found if isinstance(a, str)] if isinstance(found, list) else []


def remember_addresses(url, status):
    """Keep the addresses of the server from its status, for when the host name of url is not found."""
    url = url.rstrip("/")
    addresses = (status.get("network") or {}).get("addresses") or []
    host = urllib.parse.urlsplit(url).hostname or ""
    if not addresses or _is_address(host) or known_addresses(url) == addresses:
        return
    store = _load_addresses()
    store[url] = addresses
    try:
        _save_json(_addresses_path(), store)
    except OSError:
        pass


def _is_address(host):
    for family in (socket.AF_INET, socket.AF_INET6):
        try:
            socket.inet_pton(family, host)
            return True
        except OSError:
            pass
    return False


def _saved_token(url, name=None):
    return os.environ.get("BOOBOOT_SESSION") or _load_store().get(_store_key(url, name))


def _warn(text):
    sys.stderr.write("booboot: %s\n" % text)
    sys.stderr.flush()


def _client(args, session=True):
    """Return a client. With session, make sure it has a valid session."""
    c = Client(args.url, addresses=known_addresses(args.url), on_note=_warn)
    if not session:
        return c
    c.session = _saved_token(c.url, args.name)
    if c.session:
        try:
            c.keepalive()
            return c
        except Error as e:
            if e.code != "no_session" or os.environ.get("BOOBOOT_SESSION"):
                raise
            _warn("session expired, opening a new one")
    _open(c, args, args.session_timeout)
    _save_token(c.url, c.session, args.name)
    try:
        remember_addresses(c.url, c.status())
    except Error:
        pass  # only for later, when the name is not found
    return c


def _open(c, args, timeout, force=False):
    """Open the session. With --wait, wait for the DUT to be free, at most that many seconds."""
    deadline = time.monotonic() + (args.wait or 0)
    told = False
    while True:
        try:
            return c.open_session(args.name, timeout, force)
        except Error as e:
            if e.code != "busy" or time.monotonic() >= deadline:
                raise
            if not told:
                _warn("waiting for the DUT, used by " + _holder(e.info.get("session") or {}))
                told = True
        # Status reads are not logged by the server, refused session requests are.
        while time.monotonic() < deadline:
            time.sleep(min(2.0, max(0.0, deadline - time.monotonic())))
            if not (c.status().get("session") or {}).get("active"):
                break


def _print_json(obj):
    print(json.dumps(obj, indent=2))


def _progress(args):
    if args.json or not sys.stderr.isatty():
        return None
    last = [0.0]

    def show(done, total):
        now = time.monotonic()
        if now - last[0] < 0.5 and done < total:
            return
        last[0] = now
        pct = 100 * done // total if total else 100
        sys.stderr.write("\rsent %.1f of %.1f MB (%d%%)" % (done / 1e6, total / 1e6, pct))
        if done >= total:
            sys.stderr.write("\n")
        sys.stderr.flush()

    return show


def _size(n):
    for unit in ("B", "kB", "MB", "GB"):
        if n < 1000 or unit == "GB":
            return ("%d %s" if unit == "B" else "%.1f %s") % (n, unit)
        n /= 1000.0


def _print_text(text):
    sys.stdout.write(text)
    if text and not text.endswith("\n"):
        sys.stdout.write("\n")
    sys.stdout.flush()


def _holder(ses):
    """The client of a session, and its state, for messages."""
    state = "idle %gs, free in %gs at most" % (ses.get("idle", 0), ses.get("expires_in", 0))
    if ses.get("script"):
        state = "running the script %s on the board" % ses["script"]
    elif ses.get("alive") is True:
        state = "connected, " + state
    elif ses.get("alive") is False:
        state = "gone: no heartbeat for %gs; %s" % (ses.get("heartbeat_age", 0), state)
    return "%s (%s)" % (ses.get("client", "?"), state)


def _status_text(s):
    ses = s["session"]
    if not ses["active"]:
        who = "free"
    elif ses.get("yours"):
        who = "yours (expires after %gs idle)" % ses["timeout"]
    else:
        who = "used by " + _holder(ses)
    con = s["console"]
    lines = ["%s, BooBoot %s" % (s["name"], s["version"])]
    if s.get("label"):
        lines.append("label:   " + s["label"])
    lines += [
        "power:   %s" % s["power"]["state"] + (" (%s)" % s["power"]["error"] if s["power"]["error"] else ""),
        "sd card: %s" % s["sd"]["mode"] + (" (%s)" % s["sd"]["error"] if s["sd"]["error"] else "")
        + ", content %s" % s["sd"]["card"]["state"],
        "console: %s at %s baud" % (con["device"], con["baudrate"])
        + ("" if con["connected"] else " (not connected: %s)" % con["error"])
        + (", %d bytes sent" % con["written"] if con.get("written") else ""),
        "session: " + who,
    ]
    net = s.get("network")
    if net:
        lines.append("network: " + ", ".join([net["hostname"]] + net["addresses"]))
    others = [d for d in s.get("duts", []) if d != s["name"]]
    if others:
        lines.append("other DUTs of the board: %s (booboot duts)" % ", ".join(others))
    if s.get("script"):
        lines.append("script:  %(name)s (%(id)d), running for %(time)gs, started by %(client)s" % s["script"])
    if s["operation"]:
        lines.append("busy:    " + s["operation"]["name"])
    return "\n".join(lines)


def cmd_duts(args):
    c = _client(args, session=False)
    try:
        r = c.duts()
    except Error as e:
        if e.status != 404:
            raise
        # A server older than several DUTs per board.
        r = {"duts": [{"name": c.status()["name"], "url": ""}]}
    if args.json:
        return _print_json(r)
    board = re.sub(r"/duts/[^/]+$", "", c.url)
    for d in r["duts"]:
        if d.get("error"):
            state = "not running: " + d["error"]
        else:
            ses = d.get("session") or {}
            state = "power %s, %s" % (d.get("power"), "used by " + _holder(ses) if ses.get("active") else "free")
        print("%-10s %s%-20s %s%s" % (d["name"], board, d["url"], state,
                                      "  label: " + d["label"] if d.get("label") else ""))
    return None


def cmd_label(args):
    if args.text is None:
        r = {"label": _client(args, session=False).status().get("label", "")}
    else:
        r = _client(args).set_label(args.text)
    if args.json:
        return _print_json(r)
    if args.text is None:
        print(r["label"] or "(no label)")
    else:
        print("label set: " + r["label"] if r["label"] else "label removed")
    return None


def cmd_status(args):
    c = _client(args, session=False)
    c.session = _saved_token(c.url, args.name)
    s = c.status()
    remember_addresses(c.url, s)
    if args.json:
        return _print_json(s)
    print(_status_text(s))


def cmd_session_open(args):
    c = _client(args, session=False)
    info = _open(c, args, args.timeout, args.force)
    _save_token(c.url, c.session, args.name)
    if args.json:
        return _print_json(info)
    print("session opened (ends after %gs idle)" % info["timeout"])


def cmd_session_close(args):
    c = _client(args, session=False)
    c.session = _saved_token(c.url, args.name)
    if not c.session:
        print("no session")
        return
    try:
        r = c.close_session()
        if r.get("closed") is False and r.get("script"):
            print("the session ends when script %(id)d (%(name)s) ends" % r["script"])
        else:
            print("session closed")
    except Error as e:
        if e.code not in ("no_session", "busy"):
            raise
        print("session had already expired")
    finally:
        _save_token(c.url, None, args.name)


def cmd_power(args):
    c = _client(args)
    if args.action == "on":
        r = c.power_on()
    elif args.action == "off":
        r = c.power_off()
    else:
        r = c.power_cycle(args.off_time)
    if args.json:
        return _print_json(r)
    print("power " + r["power"])


def cmd_sd_mode(args):
    r = _client(args).sd_mode(args.mode)
    if args.json:
        return _print_json(r)
    print("sd card: " + r["mode"])


def cmd_sd_flash(args):
    c = _client(args)
    r = c.write_image(args.image, args.verify, args.compression, _progress(args))
    if args.json:
        return _print_json(r)
    print("wrote %s in %gs, sha256 %s%s" % (_size(r["bytes"]), r["seconds"], r["sha256"],
                                           ", verified" if r.get("verified") else ""))


def cmd_sd_parts(args):
    r = _client(args).partitions()
    if args.json:
        return _print_json(r)
    print("%s, %s" % (r["device"], _size(r["size"])))
    for p in r["partitions"]:
        print("  %d  %-6s %-12s %10s" % (p["number"], p["type"] or "?", p["label"], _size(p["size"])))


def cmd_sd_ls(args):
    entries = _client(args).list_dir(args.path)
    if args.json:
        return _print_json(entries)
    for e in entries:
        is_dir = e["type"] == "dir"
        print("%s %10s  %s%s" % ("d" if is_dir else "-", "" if is_dir else e["size"], e["name"],
                                 "/" if is_dir else ""))


def cmd_sd_get(args):
    c = _client(args)
    name = posixpath.basename(split_path(args.path)[1].rstrip("/"))
    local = args.local or name
    if local == "-":
        c.get_file(args.path, sys.stdout.buffer)
        return
    if os.path.isdir(local):
        local = os.path.join(local, name)
    n = c.get_file(args.path, local)
    if args.json:
        return _print_json({"path": local, "size": n})
    print("copied %s to %s (%s)" % (args.path, local, _size(n)))


def _put_targets(files, dest, as_dir=False):
    """Return (local, remote) pairs to copy files to dest. A dest ending with / is a directory."""
    part, path = split_path(dest)
    as_dir = as_dir or path.endswith("/") or len(files) > 1
    if as_dir and not path.endswith("/"):
        path += "/"
    return [(f, "%d:%s" % (part, path + os.path.basename(f) if as_dir else path)) for f in files]


def cmd_sd_put(args):
    c = _client(args)
    results = []
    for local, remote in _put_targets(args.files, args.dest):
        r = c.put_file(local, remote, _progress(args))
        results.append(r)
        if not args.json:
            print("copied %s to %s (%s)" % (local, remote, _size(r["size"])))
    if args.json:
        _print_json(results)


def cmd_sd_mkdir(args):
    r = _client(args).mkdir(args.path)
    if args.json:
        return _print_json(r)
    print("created " + args.path)


def cmd_sd_rm(args):
    r = _client(args).delete(args.path, args.recursive)
    if args.json:
        return _print_json(r)
    print("deleted " + args.path)


def cmd_console_read(args):
    # Reading needs no session. A saved one is kept alive.
    c = _client(args, session=False)
    c.session = _saved_token(c.url, args.name)
    if args.json:
        return _print_json(c.read(args.since, clean=args.clean, timestamps=args.timestamps))
    since = args.since
    out = sys.stdout.buffer
    while True:
        wait = 30 if args.follow else 0
        if args.clean:
            r = c.read(since, wait=wait, clean=True, timestamps=args.timestamps)
            data, since = r["text"].encode("utf-8"), r["next"]
        else:
            data, since = c.read_raw(since, wait=wait, timestamps=args.timestamps)
        out.write(data)
        out.flush()
        if not data and not args.follow:
            return


def cmd_console_write(args):
    text = args.text
    if args.escapes:
        text = codecs.decode(text.encode("latin-1", "backslashreplace"), "unicode_escape")
    r = _client(args).write(text, newline=not args.no_newline)
    if args.json:
        _print_json(r)


def cmd_console_expect(args):
    r = _client(args).expect(args.pattern, args.since, args.timeout, clean=not args.raw)
    if args.json:
        _print_json(r)
    elif not args.quiet:
        _print_text(r["text"])
        _print_time(args.pattern, r)
    if not r["matched"]:
        _warn("timeout: %r not seen within %gs" % (args.pattern, args.timeout))
        return EXIT_TIMEOUT


def _print_time(pattern, r):
    if r["matched"] and r.get("time") is not None:
        _warn("%r seen %.3f s after power on" % (pattern, r["time"]))


def cmd_console_run(args):
    r = _client(args).run(args.command, args.prompt, args.timeout, clean=not args.raw)
    if args.json:
        _print_json(r)
    else:
        _print_text(r["output"])
    if not r["matched"]:
        _warn("timeout: prompt not seen within %gs" % args.timeout)
        return EXIT_TIMEOUT


WINDOWS_KEYS = {"H": "\x1b[A", "P": "\x1b[B", "M": "\x1b[C", "K": "\x1b[D",
                "G": "\x1b[H", "O": "\x1b[F", "S": "\x1b[3~"}


def _keys():
    """Yield the keys typed by the user, as text."""
    if os.name == "nt":
        import msvcrt
        while True:
            ch = msvcrt.getwch()
            if ch in ("\x00", "\xe0"):
                ch = WINDOWS_KEYS.get(msvcrt.getwch(), "")
            yield ch
    else:
        fd = sys.stdin.fileno()
        while True:
            data = os.read(fd, 1024)
            if not data:
                return
            yield data.decode("utf-8", "replace")


@contextlib.contextmanager
def _raw_terminal():
    if os.name == "nt":
        os.system("")  # turns on escape codes in the Windows console
        yield
        return
    import termios
    import tty
    fd = sys.stdin.fileno()
    if not os.isatty(fd):
        yield
        return
    old = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        yield
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)


def cmd_console_attach(args):
    c = _client(args)
    stop = threading.Event()
    out = sys.stdout.buffer

    def reader():
        cursor = args.since
        while not stop.is_set():
            try:
                data, cursor = c.read_raw(cursor, wait=10)
            except Error as e:
                if stop.is_set():
                    return
                sys.stderr.write("\r\n[%s]\r\n" % e.message)
                time.sleep(1)
                continue
            out.write(data)
            out.flush()

    threading.Thread(target=reader, daemon=True).start()
    sys.stderr.write("[console of %s, press Ctrl-] to quit]\r\n" % c.url)
    try:
        with _raw_terminal():
            for keys in _keys():
                if "\x1d" in keys:
                    break
                if keys:
                    c.write(keys)
    finally:
        stop.set()
        sys.stderr.write("\r\n[closed]\r\n")


def _script_name(path):
    """A file name the server takes, from the name of a local file."""
    name = re.sub(r"[^A-Za-z0-9_.-]", "_", os.path.basename(path))
    if not name.endswith(".py"):
        name += ".py"
    return name if re.match(r"[A-Za-z0-9_]", name) and name != "booboot.py" else "_" + name


def _script_id(c, given, running=False):
    """The script given, or the latest one, or with running, the running one."""
    if given is not None:
        return given
    for s in c.scripts()["scripts"]:
        if not running or s["state"] == "running":
            return s["id"]
    raise Error("no script is running" if running else "no script on this board")


def _script_end(info):
    """Print how a script ended. Return the exit code for this command."""
    if info["state"] == "exited":
        _warn("script %d exited with code %d" % (info["id"], info["exit_code"]))
        return None if info["exit_code"] == 0 else EXIT_ERROR
    _warn("script %d stopped: %s" % (info["id"], info["reason"]))
    return EXIT_ERROR


def _follow_script(c, sid, since, show=True):
    """Print the output of a script until it ends, through connection losses. Return its information."""
    waiting = False
    try:
        while True:
            try:
                for text, cursor in c.follow_script(sid, since):
                    since = cursor
                    if show:
                        sys.stdout.write(text)
                        sys.stdout.flush()
                return c.script(sid)
            except Error as e:
                if e.status:
                    raise
                if not waiting:
                    _warn("%s; trying again until it answers (Ctrl-C to stop: the script goes on)" % e.message)
                    waiting = True
                time.sleep(5)
    except KeyboardInterrupt:
        _warn("script %d goes on. Follow it: booboot script output %d --since %d -f. Stop it: booboot script stop %d"
              % (sid, sid, since, sid))
        raise


def cmd_script_run(args):
    with open(args.file, encoding="utf-8") as f:
        source = f.read()
    c = _client(args)
    info = c.run_script(source, _script_name(args.file), args.args, args.timeout)
    if args.detach:
        if args.json:
            return _print_json(info)
        print("script %d started" % info["id"])
        return None
    if not args.json:
        _warn("script %d started" % info["id"])
    info = _follow_script(c, info["id"], 0, show=not args.json)
    if args.json:
        _print_json(info)
    return _script_end(info)


def cmd_script_output(args):
    c = _client(args, session=False)
    sid = _script_id(c, args.id)
    if args.json:
        return _print_json(c.script_output(sid, args.since))
    if args.follow:
        return _script_end(_follow_script(c, sid, args.since))
    since = args.since
    while True:
        r = c.script_output(sid, since)
        sys.stdout.write(r["text"])
        since = r["next"]
        if not r["text"] or since >= r["script"]["output"]:
            break
    sys.stdout.flush()
    return None


def cmd_script_list(args):
    c = _client(args, session=False)
    r = c.scripts()
    if args.json:
        return _print_json(r)
    if not r["enabled"]:
        _warn("scripts are off on this unit")
    for s in r["scripts"]:
        state = "exited %d" % s["exit_code"] if s["state"] == "exited" else s["state"]
        line = "%4d  %-9s %-20s %s %8gs  %s" % (s["id"], state, s["name"], s["started"], s["time"], s["client"])
        print(line + ("  (%s)" % s["reason"] if s["reason"] else ""))
    return None


def cmd_script_stop(args):
    c = _client(args)
    sid = _script_id(c, args.id, running=True)
    c.stop_script(sid)
    info = _follow_script(c, sid, -1, show=False)
    if args.json:
        return _print_json(info)
    print("script %d stopped" % sid)
    return None


def cmd_deploy(args):
    c = _client(args)
    result = {"power_off": c.power_off()}
    if args.image:
        _warn("writing %s" % args.image)
        result["image"] = c.write_image(args.image, args.verify, progress=_progress(args))
    result["files"] = []
    for local, remote in _put_targets(args.files, args.dest, as_dir=True):
        _warn("copying %s to %s" % (local, remote))
        result["files"].append(c.put_file(local, remote, _progress(args)))
    result["power_on"] = c.power_on()
    _warn("power on")
    code = None
    if args.expect:
        r = c.expect_boot(result["power_on"], args.expect, args.timeout, clean=True)
        result["expect"] = r
        if not args.json and not args.quiet:
            _print_text(r["text"])
        if not args.json:
            _print_time(args.expect, r)
        if not r["matched"]:
            _warn("timeout: %r not seen within %gs" % (args.expect, args.timeout))
            code = EXIT_TIMEOUT
    if args.json:
        _print_json(result)
    return code


def cmd_boottime(args):
    c = _client(args)
    times = []
    code = None
    for n in range(1, args.runs + 1):
        t = c.boot_time(args.pattern, args.timeout, args.off_time)
        if t is None:
            _warn("run %d: %r not seen within %gs" % (n, args.pattern, args.timeout))
            code = EXIT_TIMEOUT
            break
        times.append(t)
        if not args.json:
            print("run %d/%d: %.3f s" % (n, args.runs, t))
            sys.stdout.flush()
    result = {"pattern": args.pattern, "times": times}
    if times:
        result.update(min=min(times), mean=round(sum(times) / len(times), 3), max=max(times))
    if args.json:
        _print_json(result)
    elif len(times) > 1:
        print("min %.3f s, mean %.3f s, max %.3f s" % (result["min"], result["mean"], result["max"]))
    return code


# MCP server: the DUT as tools for MCP clients, on standard input and output.

MCP_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")
MCP_MAX_TEXT = 16000  # characters of console output in a tool result
_REQUIRED = object()

MCP_INSTRUCTIONS = """\
These tools control one embedded board, the DUT, through a BooBoot server: its \
power, its SD card and its serial console.
Usual work: deploy boot files or an image and wait for the login prompt, log in \
with console_write and console_expect, then run shell commands with console_run.
Card paths are N:/path, N being the partition number ("/path" means "1:/path"). \
Local paths are files on this computer.
Every byte of console output has a cursor. "since" takes a cursor or "boot" (last \
power on), "last" (end of the last expect or run match), "now" or "start".
The SD card can be used only while the power is off. Power on gives it back to the DUT.
Long or unattended work, like a boot loop, can run on the BooBoot board as a Python \
script (script_run): it goes on if this connection drops, and its output stays on \
the board (script_output)."""


class _McpSession:
    """The BooBoot client of an MCP server, with a session opened when needed."""

    def __init__(self, url, name=None, timeout=None):
        self.client = Client(url, addresses=known_addresses(url), on_note=self._address_note)
        self.name = name or "mcp " + default_client_name()
        self.timeout = timeout or 900
        self._note = ""
        self._stop = threading.Event()
        threading.Thread(target=self._beat, daemon=True).start()

    def _beat(self):
        while not self._stop.wait(HEARTBEAT):
            if self.client.session:
                try:
                    self.client.heartbeat()
                except Error as e:
                    if e.code == "not_found":
                        return  # a server older than heartbeats

    def _address_note(self, text):
        self._note += "Note: %s.\n" % text

    def dut(self):
        """Return the client with a valid session."""
        c = self.client
        if c.session:
            try:
                c.keepalive()
                return c
            except Error as e:
                if e.code != "no_session":
                    raise
            self._note = ("Note: the session had expired and was opened again. "
                          "Another client may have used the DUT in between.\n")
        self.open()
        return c

    def open(self, force=False):
        """Open the session. Takes it from a gone client of the same name, like an earlier run that was killed."""
        c = self.client
        try:
            info = c.open_session(self.name, self.timeout, force)
        except Error as e:
            ses = e.info.get("session", {})
            if e.code != "busy" or force or ses.get("client") != self.name or ses.get("alive") is not False:
                raise
            info = c.open_session(self.name, self.timeout, "gone")
            self._note += "Note: took over the session of an earlier client of the same name: %s.\n" % _holder(ses)
        try:
            c.heartbeat()
        except Error:
            pass
        return info

    def take_note(self):
        note, self._note = self._note, ""
        return note

    def close(self):
        self._stop.set()
        try:
            return self.client.close_session()
        except Error:
            return None


def _arg(a, name, default=_REQUIRED):
    if a.get(name) is not None:
        return a[name]
    if default is _REQUIRED:
        raise ValueError("missing argument: %s" % name)
    return default


def _local(path):
    return os.path.expanduser(str(path))


def _tail(text):
    if len(text) <= MCP_MAX_TEXT:
        return text
    return "[%d earlier characters not shown]\n%s" % (len(text) - MCP_MAX_TEXT, text[-MCP_MAX_TEXT:])


class _Total:
    """Progress of several uploads as one growing count."""

    def __init__(self, report, total):
        self.report = report
        self.total = total
        self.base = 0

    def upload(self, size):
        base = self.base
        self.base += size
        if not self.report:
            return None
        return lambda done, _: self.report(base + done, self.total)


def _mcp_status(s, a, report):
    status = s.client.status()
    remember_addresses(s.client.url, status)
    return _status_text(status)


def _mcp_power(s, a, report):
    action = _arg(a, "action")
    c = s.dut()
    if action == "on":
        r = c.power_on()
    elif action == "off":
        r = c.power_off()
    elif action == "cycle":
        r = c.power_cycle(a.get("off_time"))
    else:
        raise ValueError("action must be on, off or cycle")
    if r["power"] == "on":
        return "Power on. The console output of this boot starts at cursor %d (since \"boot\")." % r["boot"]
    return "Power off."


def _mcp_sd_mode(s, a, report):
    return "SD card: %s" % s.dut().sd_mode(_arg(a, "mode"))["mode"]


def _mcp_sd_flash(s, a, report):
    path = _local(_arg(a, "image"))
    progress = _Total(report, os.path.getsize(path)).upload(os.path.getsize(path))
    r = s.dut().write_image(path, bool(a.get("verify")), progress=progress)
    return "Wrote %s in %gs, sha256 %s%s. The card stays on the host side until power on." % (
        _size(r["bytes"]), r["seconds"], r["sha256"], ", verified" if r.get("verified") else "")


def _mcp_sd_list(s, a, report):
    path = a.get("path") or "1:/"
    lines = []
    for e in s.dut().list_dir(path):
        if e["type"] == "dir":
            lines.append("dir   %s/" % e["name"])
        else:
            lines.append("%-5s %s (%s)" % (e["type"], e["name"], _size(e["size"])))
    return "%s\n%s" % (path, "\n".join(lines) or "(empty)")


def _mcp_sd_get(s, a, report):
    remote = _arg(a, "path")
    name = posixpath.basename(split_path(remote)[1].rstrip("/"))
    local = _local(a.get("local_path") or name)
    if os.path.isdir(local):
        local = os.path.join(local, name)
    n = s.dut().get_file(remote, local)
    return "Copied %s to %s (%s)." % (remote, os.path.abspath(local), _size(n))


def _mcp_copy(c, files, dest, report, as_dir=False):
    files = [files] if isinstance(files, str) else list(files)
    targets = _put_targets([_local(f) for f in files], dest, as_dir)
    total = _Total(report, sum(os.path.getsize(f) for f, _ in targets))
    lines = []
    for local, remote in targets:
        r = c.put_file(local, remote, total.upload(os.path.getsize(local)))
        lines.append("Copied %s to %s (%s)." % (local, remote, _size(r["size"])))
    return lines


def _mcp_sd_put(s, a, report):
    return "\n".join(_mcp_copy(s.dut(), _arg(a, "files"), _arg(a, "dest"), report))


def _mcp_sd_delete(s, a, report):
    path = _arg(a, "path")
    s.dut().delete(path, bool(a.get("recursive")))
    return "Deleted %s." % path


def _mcp_console_read(s, a, report):
    c = s.dut()
    max_bytes = max(1, min(int(a.get("max_bytes") or MCP_MAX_TEXT), 1 << 20))
    start = c.read(str(a.get("since", "boot")), max_bytes=1)["cursor"]
    end = int(a["until"]) if a.get("until") is not None else c.status()["console"]["cursor"]
    frm = max(start, end - max_bytes)
    text = ""
    if end > frm:
        text = c.read(frm, max_bytes=end - frm, clean=True, timestamps=bool(a.get("timestamps")))["text"]
    head = "Console output from cursor %d to %d" % (frm, end)
    if frm > start:
        head += " (%d earlier bytes not shown: since=%d, until=%d)" % (frm - start, start, frm)
    return "%s:\n%s" % (head, text or "(none)")


def _mcp_console_write(s, a, report):
    r = s.dut().write(str(_arg(a, "text")), newline=bool(a.get("newline", True)))
    return "Sent %d bytes. Their answer starts at cursor %d." % (r["written"], r["cursor"])


def _mcp_console_expect(s, a, report):
    pattern = _arg(a, "pattern")
    timeout = float(a.get("timeout", 30))
    r = s.dut().expect(pattern, since=str(a.get("since", "last")), timeout=timeout, clean=True)
    if r["matched"]:
        head = "Matched %r" % r["match"]
        if r.get("time") is not None:
            head += " %.3f s after power on" % r["time"]
        head += ". Next cursor %d. Output from cursor %d:" % (r["next"], r["cursor"])
    else:
        head = "Not seen within %gs. Output from cursor %d:" % (timeout, r["cursor"])
    return "%s\n%s" % (head, _tail(r["text"]) or "(none)")


def _mcp_console_run(s, a, report):
    timeout = float(a.get("timeout", 30))
    r = s.dut().run(str(_arg(a, "command")), a.get("prompt"), timeout)
    if r["matched"]:
        return _tail(r["output"]) or "(no output)"
    return "Prompt not seen within %gs. Output so far:\n%s" % (timeout, _tail(r["output"]) or "(none)")


def _mcp_deploy(s, a, report):
    c = s.dut()
    c.power_off()
    lines = ["Power off."]
    image = a.get("image")
    if image:
        size = os.path.getsize(_local(image))
        r = c.write_image(_local(image), bool(a.get("verify")), progress=_Total(report, size).upload(size))
        lines.append("Wrote %s (%s)." % (image, _size(r["bytes"])))
    lines += _mcp_copy(c, a.get("files") or [], a.get("dest") or "1:/", report, as_dir=True)
    on = c.power_on()
    lines.append("Power on.")
    pattern = a.get("expect")
    if pattern:
        timeout = float(a.get("timeout", 120))
        r = c.expect_boot(on, pattern, timeout, clean=True)
        if r["matched"]:
            lines.append("Matched %r %.3f s after power on. Boot output:" % (r["match"], r["time"] or 0))
        else:
            lines.append("%r not seen within %gs. Boot output:" % (pattern, timeout))
        lines.append(_tail(r["text"]) or "(none)")
    return "\n".join(lines)


def _mcp_boot_time(s, a, report):
    pattern = _arg(a, "pattern")
    runs = max(1, min(int(a.get("runs", 1)), 50))
    timeout = float(a.get("timeout", 120))
    c = s.dut()
    times, lines = [], []
    for n in range(1, runs + 1):
        t = c.boot_time(pattern, timeout, a.get("off_time"))
        if t is None:
            lines.append("Run %d: %r not seen within %gs." % (n, pattern, timeout))
            break
        times.append(t)
        lines.append("Run %d: %.3f s" % (n, t))
    if len(times) > 1:
        lines.append("min %.3f s, mean %.3f s, max %.3f s" % (min(times), sum(times) / len(times), max(times)))
    return "\n".join(lines)


def _mcp_session(s, a, report):
    action = _arg(a, "action")
    c = s.client
    if action == "release":
        r = s.close() or {}
        if r.get("closed") is False and r.get("script"):
            return "The session ends when script %(id)d (%(name)s) ends." % r["script"]
        return "Session released: other clients can use the DUT."
    if action != "open":
        raise ValueError("action must be open or release")
    force = a.get("force") or False
    if force not in (True, False, "gone"):
        raise ValueError('force must be true, false or "gone"')
    if c.session and force is not True:
        try:
            c.keepalive()
            return "The session is already open."
        except Error as e:
            if e.code != "no_session":
                raise
    info = s.open(force)
    return "Session opened. It ends after %gs without calls." % info["timeout"]


def _script_state(info):
    if info["state"] == "running":
        return "Script %d is running (%gs)." % (info["id"], info["time"])
    if info["state"] == "exited":
        return "Script %d exited with code %d after %gs." % (info["id"], info["exit_code"], info["time"])
    return "Script %d stopped after %gs: %s." % (info["id"], info["time"], info["reason"])


def _mcp_script_text(c, sid, since, wait):
    """Output of a script from a cursor, waiting up to wait seconds for its end, and its state."""
    deadline = time.monotonic() + min(float(wait), 3600)
    start, text = None, ""
    while True:
        r = c.script_output(sid, since, wait=max(0.0, min(30.0, deadline - time.monotonic())), clean=True)
        start = r["cursor"] if start is None else start
        text += r["text"]
        since, info = r["next"], r["script"]
        if (info["state"] != "running" and since >= info["output"]) or time.monotonic() >= deadline:
            break
    shown = _tail(text)
    head = "Output from cursor %d to %d" % (start, since)
    if shown != text:
        head += " (cut: read the start with script_output, since=%d)" % start
    more = " Read more with script_output, since=%d." % since if info["state"] == "running" else ""
    return "%s:\n%s\n%s%s" % (head, shown or "(none)", _script_state(info), more)


def _mcp_script_run(s, a, report):
    if a.get("path"):
        path = _local(a["path"])
        with open(path, encoding="utf-8") as f:
            source = f.read()
        name = _script_name(path)
    else:
        source = str(_arg(a, "source"))
        name = _script_name(str(a.get("name") or "script.py"))
    c = s.dut()
    info = c.run_script(source, name, [str(x) for x in a.get("args") or []], a.get("timeout"))
    return "Script %d (%s) started.\n%s" % (info["id"], name, _mcp_script_text(c, info["id"], 0, a.get("wait", 10)))


def _mcp_script_output(s, a, report):
    c = s.client
    sid = _script_id(c, a.get("id"))
    return _mcp_script_text(c, sid, int(a.get("since", 0)), a.get("wait", 0))


def _mcp_script_stop(s, a, report):
    c = s.dut()
    sid = _script_id(c, a.get("id"), running=True)
    c.stop_script(sid)
    for _ in c.follow_script(sid, -1):
        pass
    return _script_state(c.script(sid))


def _mcp_script_list(s, a, report):
    r = s.client.scripts()
    lines = [] if r["enabled"] else ["Scripts are off on this unit: its configuration must allow them."]
    for info in r["scripts"]:
        lines.append("%s %s, started %s by %s." % (_script_state(info), info["name"], info["started"], info["client"]))
    return "\n".join(lines) or "No scripts."


def _mcp_error_text(e):
    if e.code == "busy":
        ses = e.info.get("session", {})
        text = "The DUT is used by another client: %s. " % _holder(ses)
        if ses.get("alive") is False:
            return text + ('That client is gone. Tell the user. If they agree, take the DUT with the session '
                           'tool (action open, force "gone").')
        if ses.get("script"):
            return text + "Wait for the end of the script, or tell the user."
        if ses.get("alive") is True:
            return text + "That client is connected: someone may be using the DUT. Wait and try again."
        return text + ("Wait and try again. Only if the user says that this client is gone, take the DUT with "
                       "the session tool (action open, force true).")
    return "Error (%s): %s" % (e.code, e.message)


_SINCE = {"type": ["string", "integer"],
          "description": 'Cursor number, or "boot", "last", "now", "start", or a negative number of bytes'}
_CARD_PATH = {"type": "string", "description": 'Card path N:/path, like "1:/BOOT.BIN"'}
_TIMEOUT = {"type": "number", "description": "Seconds to wait"}


def _tool(name, title, description, props, required, fn, read_only=False, destructive=True):
    tool = {
        "name": name,
        "title": title,
        "description": description,
        "inputSchema": {"type": "object", "properties": props, "required": list(required)},
        "annotations": {"title": title, "readOnlyHint": read_only, "destructiveHint": destructive,
                        "openWorldHint": False},
    }
    return tool, fn


MCP_TOOLS = [
    _tool("status", "DUT status",
          "Show the power, SD card, console and session state of the DUT.",
          {}, [], _mcp_status, read_only=True),
    _tool("power", "Switch the power",
          "Switch the DUT power on, off, or off then on (cycle). Power on first connects the SD card "
          "to the DUT.",
          {"action": {"type": "string", "enum": ["on", "off", "cycle"]},
           "off_time": {"type": "number", "description": "Seconds off during a cycle"}},
          ["action"], _mcp_power),
    _tool("deploy", "Deploy and boot",
          "Power off, write a disk image and/or copy files to the SD card, power on, and wait for a "
          "regex such as a login prompt. Returns the boot output and the boot time.",
          {"files": {"type": "array", "items": {"type": "string"}, "description": "Local files to copy"},
           "dest": {"type": "string", "description": 'Card directory for the files, default "1:/"'},
           "image": {"type": "string", "description": "Local disk image to write first (raw, gz, xz, bz2)"},
           "verify": {"type": "boolean", "description": "Read the image back and compare"},
           "expect": {"type": "string", "description": 'Regex that ends the boot, like "login: "'},
           "timeout": {"type": "number", "description": "Seconds to wait for expect, default 120"}},
          [], _mcp_deploy),
    _tool("sd_mode", "Switch the SD card",
          "Connect the SD card to the BooBoot board (host), to the DUT (dut), or to nothing (off). "
          "host needs the power off. The file tools switch to host by themselves.",
          {"mode": {"type": "string", "enum": ["host", "dut", "off"]}},
          ["mode"], _mcp_sd_mode, destructive=False),
    _tool("sd_flash", "Write a disk image",
          "Write a local disk image (raw, gz, xz or bz2) to the whole SD card. The power must be off.",
          {"image": {"type": "string", "description": "Local path of the image"},
           "verify": {"type": "boolean", "description": "Read the card back and compare"}},
          ["image"], _mcp_sd_flash),
    _tool("sd_list", "List SD card files",
          "List a directory of the SD card. The power must be off.",
          {"path": dict(_CARD_PATH, description='Card directory, default "1:/"')},
          [], _mcp_sd_list, destructive=False),
    _tool("sd_get", "Copy a file from the SD card",
          "Copy a file of the SD card to this computer. The power must be off.",
          {"path": _CARD_PATH,
           "local_path": {"type": "string", "description": "Local file or directory, default the current one"}},
          ["path"], _mcp_sd_get, destructive=False),
    _tool("sd_put", "Copy files to the SD card",
          "Copy local files to the SD card, replacing files of the same name. The power must be off. "
          "A dest ending with / is a directory.",
          {"files": {"type": "array", "items": {"type": "string"}, "description": "Local files"},
           "dest": _CARD_PATH},
          ["files", "dest"], _mcp_sd_put),
    _tool("sd_delete", "Delete on the SD card",
          "Delete a file or directory of the SD card. The power must be off.",
          {"path": _CARD_PATH, "recursive": {"type": "boolean", "description": "Delete a directory and its content"}},
          ["path"], _mcp_sd_delete),
    _tool("console_read", "Read the console",
          "Read the serial console output after a cursor. Long output is cut to its last max_bytes; "
          "the answer gives the cursors to read the rest.",
          {"since": dict(_SINCE, description=_SINCE["description"] + ', default "boot"'),
           "until": {"type": "integer", "description": "Stop at this cursor"},
           "max_bytes": {"type": "integer", "description": "Default %d" % MCP_MAX_TEXT},
           "timestamps": {"type": "boolean", "description": "Start each line with its time since power on"}},
          [], _mcp_console_read, read_only=True, destructive=False),
    _tool("console_write", "Write to the console",
          "Send text to the serial console, followed by Enter unless newline is false. Control "
          "characters work: \\u0003 is Ctrl-C.",
          {"text": {"type": "string"}, "newline": {"type": "boolean", "description": "Default true"}},
          ["text"], _mcp_console_write),
    _tool("console_expect", "Wait for console output",
          "Wait until a regex (Python syntax) appears in the console output after a cursor, and return "
          "the output up to it. $ matches the end of the output received so far, which suits prompts.",
          {"pattern": {"type": "string"},
           "since": dict(_SINCE, description=_SINCE["description"] + ', default "last"'),
           "timeout": dict(_TIMEOUT, description="Seconds to wait, default 30")},
          ["pattern"], _mcp_console_expect, read_only=True, destructive=False),
    _tool("console_run", "Run a console command",
          "Send a command line to the shell of the DUT (or to U-Boot) and return its output, without the "
          "echo and the prompt. Waits for the prompt regex, by default the one set on the server "
          "(ends with #, $ or >). For prompts like login or password, use console_write and console_expect.",
          {"command": {"type": "string"},
           "prompt": {"type": "string", "description": "Prompt regex"},
           "timeout": dict(_TIMEOUT, description="Seconds to wait, default 30")},
          ["command"], _mcp_console_run),
    _tool("boot_time", "Measure the boot time",
          "Power cycle the DUT and measure the time from power on to a regex, like a login prompt, "
          "once or several times.",
          {"pattern": {"type": "string"},
           "runs": {"type": "integer", "description": "Number of boots, default 1"},
           "off_time": {"type": "number", "description": "Seconds off before each boot"},
           "timeout": dict(_TIMEOUT, description="Seconds to wait for each boot, default 120")},
          ["pattern"], _mcp_boot_time),
    _tool("script_run", "Run a script on the BooBoot board",
          "Run a Python script on the BooBoot board, in this session. It goes on if this connection drops; its "
          "output stays on the board. In the script, booboot.Client.from_env() is the DUT, as in the booboot "
          "Python module: power_cycle(), expect(), run(), boot_time(), ... It has no other access to the "
          "hardware. One script runs at a time.",
          {"path": {"type": "string", "description": "Local Python file"},
           "source": {"type": "string", "description": "Text of the script, instead of path"},
           "name": {"type": "string", "description": "File name for source, default script.py"},
           "args": {"type": "array", "items": {"type": "string"}, "description": "Arguments of the script"},
           "timeout": {"type": "number", "description": "Seconds after which the script is stopped"},
           "wait": {"type": "number", "description": "Seconds to wait for its end and output, default 10"}},
          [], _mcp_script_run),
    _tool("script_output", "Read the output of a script",
          "Read the output of a script from a cursor, while it runs or after it ended, and its state.",
          {"id": {"type": "integer", "description": "Script number, default the latest"},
           "since": {"type": "integer", "description": "Cursor, default 0 (the start); negative: bytes before the end"},
           "wait": {"type": "number", "description": "Seconds to wait for the end of the script, default 0"}},
          [], _mcp_script_output, read_only=True, destructive=False),
    _tool("script_stop", "Stop a script",
          "Stop a script running on the BooBoot board.",
          {"id": {"type": "integer", "description": "Script number, default the running one"}},
          [], _mcp_script_stop),
    _tool("script_list", "List the scripts",
          "List the scripts kept on the BooBoot board, the newest first, with their state.",
          {}, [], _mcp_script_list, read_only=True, destructive=False),
    _tool("session", "Open or release the session",
          "Only one client can use the DUT at a time. The other tools open the session when needed. "
          "release lets other clients use the DUT. open with force takes the DUT from another client.",
          {"action": {"type": "string", "enum": ["open", "release"]},
           "force": {"type": ["boolean", "string"],
                     "description": 'Take the session from another client. "gone": only from a client that '
                                    'is gone'}},
          ["action"], _mcp_session),
]
MCP_FUNCTIONS = {tool["name"]: fn for tool, fn in MCP_TOOLS}


class McpServer:
    """MCP server: JSON-RPC 2.0 messages, one per line, on binary streams."""

    def __init__(self, session, inp=None, out=None):
        self.session = session
        self._in = inp or sys.stdin.buffer
        self._out = out or sys.stdout.buffer
        self._lock = threading.Lock()
        self._jobs = queue.Queue()

    def send(self, msg):
        data = json.dumps(msg, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n"
        with self._lock:
            self._out.write(data)
            self._out.flush()

    def _reply(self, mid, result=None, error=None):
        msg = {"jsonrpc": "2.0", "id": mid}
        if error:
            msg["error"] = error
        else:
            msg["result"] = result
        self.send(msg)

    def serve(self):
        """Answer requests until the input ends. Tool calls run one at a time."""
        worker = threading.Thread(target=self._work, daemon=True)
        worker.start()
        while True:
            line = self._in.readline()
            if not line:
                break
            if not line.strip():
                continue
            try:
                msg = json.loads(line.decode("utf-8"))
            except ValueError:
                self._reply(None, error={"code": -32700, "message": "parse error"})
                continue
            for m in msg if isinstance(msg, list) else [msg]:
                self._handle(m)
        self._jobs.put(None)
        worker.join()

    def _handle(self, msg):
        # Notifications (no id) and responses (no method) need no answer.
        if not isinstance(msg, dict) or "method" not in msg or "id" not in msg:
            return
        mid, method = msg["id"], msg["method"]
        params = msg.get("params") or {}
        if method == "initialize":
            asked = params.get("protocolVersion")
            self._reply(mid, {
                "protocolVersion": asked if asked in MCP_VERSIONS else MCP_VERSIONS[0],
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "booboot", "version": __version__},
                "instructions": MCP_INSTRUCTIONS,
            })
        elif method == "ping":
            self._reply(mid, {})
        elif method == "tools/list":
            self._reply(mid, {"tools": [tool for tool, _ in MCP_TOOLS]})
        elif method == "tools/call":
            self._jobs.put((mid, params))
        else:
            self._reply(mid, error={"code": -32601, "message": "method not found: %s" % method})

    def _work(self):
        while True:
            job = self._jobs.get()
            if job is None:
                return
            self._call(*job)

    def _call(self, mid, params):
        fn = MCP_FUNCTIONS.get(params.get("name"))
        if fn is None:
            self._reply(mid, error={"code": -32602, "message": "unknown tool: %s" % params.get("name")})
            return
        token = (params.get("_meta") or {}).get("progressToken")
        try:
            text, failed = fn(self.session, params.get("arguments") or {}, self._progress(token)), False
        except Error as e:
            text, failed = _mcp_error_text(e), True
        except Exception as e:
            text, failed = "Error: %s" % e, True
        text = self.session.take_note() + text
        self._reply(mid, {"content": [{"type": "text", "text": text}], "isError": failed})

    def _progress(self, token):
        """Return report(done, total) sending progress notifications, or None."""
        if token is None:
            return None
        last = [0.0, -1]  # time and value of the last notification

        def report(done, total):
            now = time.monotonic()
            # Progress must go up, and a few notifications are enough.
            if done <= last[1] or (now - last[0] < 0.5 and done < total):
                return
            last[:] = [now, done]
            self.send({"jsonrpc": "2.0", "method": "notifications/progress",
                       "params": {"progressToken": token, "progress": done, "total": total,
                                  "message": "sent %s of %s" % (_size(done), _size(total))}})

        return report


def cmd_mcp(args):
    session = _McpSession(args.url, args.name, args.session_timeout)
    try:
        McpServer(session).serve()
    finally:
        session.close()


def build_parser():
    p = argparse.ArgumentParser(
        prog="booboot", description="Control a device under test through a BooBoot server.",
        epilog=EPILOG, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("-u", "--url", default=os.environ.get("BOOBOOT_URL", DEFAULT_URL),
                   help="server URL (env BOOBOOT_URL, default %(default)s)")
    p.add_argument("--dut", default=os.environ.get("BOOBOOT_DUT") or None,
                   help="DUT of the board, when it has several (env BOOBOOT_DUT): its URL is URL/duts/DUT")
    p.add_argument("--json", action="store_true", help="print results as JSON")
    p.add_argument("--name", help="client name shown to other clients (default user@host); each name has "
                                  "its own session, like agents that run at the same time")
    p.add_argument("--session-timeout", type=float, metavar="S",
                   help="idle seconds before the server ends a session opened by this command")
    p.add_argument("--wait", type=float, metavar="S",
                   help="when another client has the DUT, wait up to S seconds for it to be free")
    p.add_argument("--version", action="version", version=__version__)
    sub = p.add_subparsers(dest="command", metavar="COMMAND")
    sub.required = True

    sp = sub.add_parser("status", help="show power, SD card, console and session state")
    sp.set_defaults(func=cmd_status)
    sp = sub.add_parser("duts", help="list the DUTs of the board, with their state")
    sp.set_defaults(func=cmd_duts)
    sp = sub.add_parser("label", help="show or set the label of the DUT, shown with its name",
                        description="Show the label of the DUT, or set it: free text shown with its name, "
                                    "like \"ZCU102 rev B, bench 3\". \"\" removes it. Setting it needs the session.")
    sp.add_argument("text", nargs="?", help="the new label")
    sp.set_defaults(func=cmd_label)

    ses = sub.add_parser("session", help="open or close the session").add_subparsers(dest="action", metavar="ACTION")
    ses.required = True
    sp = ses.add_parser("open", help="open the session (other commands open one when needed)")
    sp.add_argument("--timeout", type=float, metavar="S", help="idle seconds before the server ends the session")
    sp.add_argument("--force", nargs="?", const=True, default=False, choices=["gone"], metavar="gone",
                    help="take the session from another client; with gone, only from a client that is gone")
    sp.set_defaults(func=cmd_session_open)
    sp = ses.add_parser("close", help="release the session for other clients")
    sp.set_defaults(func=cmd_session_close)

    sp = sub.add_parser("power", help="switch the DUT power")
    sp.add_argument("action", choices=["on", "off", "cycle"])
    sp.add_argument("--off-time", type=float, metavar="S", help="seconds off during a cycle")
    sp.set_defaults(func=cmd_power)

    sd = sub.add_parser("sd", help="SD card: switch, write image, edit files").add_subparsers(
        dest="action", metavar="ACTION")
    sd.required = True
    for mode, text in (("host", "connect the card to the BooBoot board (power must be off)"),
                       ("dut", "connect the card to the DUT"),
                       ("off", "disconnect the card")):
        sp = sd.add_parser(mode, help=text)
        sp.set_defaults(func=cmd_sd_mode, mode=mode)
    sp = sd.add_parser("flash", help="write a disk image (raw, gz, xz, bz2) to the card")
    sp.add_argument("image")
    sp.add_argument("--verify", action="store_true", help="read the card back and compare")
    sp.add_argument("--compression", default="auto", choices=["auto", "none", "gz", "xz", "bz2", "zst"])
    sp.set_defaults(func=cmd_sd_flash)
    sp = sd.add_parser("parts", help="list the partitions")
    sp.set_defaults(func=cmd_sd_parts)
    sp = sd.add_parser("ls", help="list a directory")
    sp.add_argument("path", nargs="?", default="1:/")
    sp.set_defaults(func=cmd_sd_ls)
    sp = sd.add_parser("get", help="copy a file from the card (LOCAL - for stdout)")
    sp.add_argument("path")
    sp.add_argument("local", nargs="?")
    sp.set_defaults(func=cmd_sd_get)
    sp = sd.add_parser("put", help="copy files to the card (DEST ending with / is a directory)")
    sp.add_argument("files", nargs="+", metavar="FILE")
    sp.add_argument("dest", metavar="DEST")
    sp.set_defaults(func=cmd_sd_put)
    sp = sd.add_parser("mkdir", help="create a directory")
    sp.add_argument("path")
    sp.set_defaults(func=cmd_sd_mkdir)
    sp = sd.add_parser("rm", help="delete a file or directory")
    sp.add_argument("path")
    sp.add_argument("-r", "--recursive", action="store_true")
    sp.set_defaults(func=cmd_sd_rm)

    con = sub.add_parser("console", help="serial console").add_subparsers(dest="action", metavar="ACTION")
    con.required = True
    since_help = "cursor, negative byte count, start, boot, last or now (default %(default)s)"
    sp = con.add_parser("read", help="print console output")
    sp.add_argument("--since", default="boot", help=since_help)
    sp.add_argument("-f", "--follow", action="store_true", help="keep printing new output")
    sp.add_argument("--clean", action="store_true", help="remove escape codes and carriage returns")
    sp.add_argument("-t", "--timestamps", action="store_true", help="start each line with its time since power on")
    sp.set_defaults(func=cmd_console_read)
    sp = con.add_parser("write", help="send text, with a line ending unless -n")
    sp.add_argument("text")
    sp.add_argument("-n", "--no-newline", action="store_true")
    sp.add_argument("-e", "--escapes", action="store_true", help=r"interpret escapes like \x03 (Ctrl-C)")
    sp.set_defaults(func=cmd_console_write)
    sp = con.add_parser("expect", help="wait for a regex and print the output up to it")
    sp.add_argument("pattern")
    sp.add_argument("--since", default="last", help=since_help)
    sp.add_argument("--timeout", type=float, default=30.0, metavar="S")
    sp.add_argument("--raw", action="store_true", help="keep escape codes and carriage returns")
    sp.add_argument("-q", "--quiet", action="store_true", help="print nothing")
    sp.set_defaults(func=cmd_console_expect)
    sp = con.add_parser("run", help="send a command line and print its output")
    sp.add_argument("command")
    sp.add_argument("--prompt", help="prompt regex (default set on the server)")
    sp.add_argument("--timeout", type=float, default=30.0, metavar="S")
    sp.add_argument("--raw", action="store_true", help="keep escape codes and carriage returns")
    sp.set_defaults(func=cmd_console_run)
    sp = con.add_parser("attach", help="interactive terminal (Ctrl-] to quit)")
    sp.add_argument("--since", default="now", help=since_help)
    sp.set_defaults(func=cmd_console_attach)

    sp = sub.add_parser("deploy", help="power off, write image and files, power on, wait for a regex")
    sp.add_argument("files", nargs="*", metavar="FILE", help="files to copy to DEST")
    sp.add_argument("--image", help="disk image to write first")
    sp.add_argument("--verify", action="store_true", help="verify the image")
    sp.add_argument("--dest", default="1:/", help="card directory for the files (default %(default)s)")
    sp.add_argument("--expect", metavar="PATTERN", help="regex to wait for after power on")
    sp.add_argument("--timeout", type=float, default=120.0, metavar="S")
    sp.add_argument("-q", "--quiet", action="store_true", help="do not print the boot output")
    sp.set_defaults(func=cmd_deploy)

    sp = sub.add_parser("boottime", help="power cycle and measure the time from power on to a regex")
    sp.add_argument("pattern", help='regex that ends the boot, like "login: "')
    sp.add_argument("--runs", type=int, default=1, metavar="N", help="number of boots (default 1)")
    sp.add_argument("--off-time", type=float, metavar="S", help="seconds off before each boot")
    sp.add_argument("--timeout", type=float, default=120.0, metavar="S")
    sp.set_defaults(func=cmd_boottime)

    scr = sub.add_parser("script", help="run Python scripts on the BooBoot board").add_subparsers(
        dest="action", metavar="ACTION")
    scr.required = True
    sp = scr.add_parser("run", help="send a script to the board and run it there, showing its output; "
                                    "it goes on if this command stops")
    sp.add_argument("--detach", action="store_true", help="only start the script")
    sp.add_argument("--timeout", type=float, metavar="S", help="seconds after which the script is stopped")
    sp.add_argument("file", help="the script, a Python file. Options of this command come before it.")
    sp.add_argument("args", nargs=argparse.REMAINDER, help="arguments of the script")
    sp.set_defaults(func=cmd_script_run)
    sp = scr.add_parser("output", help="print the output of a script, while it runs or after")
    sp.add_argument("id", type=int, nargs="?", help="script number (default: the latest)")
    sp.add_argument("--since", type=int, default=0, metavar="CURSOR",
                    help="from this cursor; negative: bytes before the end")
    sp.add_argument("-f", "--follow", action="store_true", help="go on until the script ends")
    sp.set_defaults(func=cmd_script_output)
    sp = scr.add_parser("list", help="list the scripts kept on the board")
    sp.set_defaults(func=cmd_script_list)
    sp = scr.add_parser("stop", help="stop a script")
    sp.add_argument("id", type=int, nargs="?", help="script number (default: the running one)")
    sp.set_defaults(func=cmd_script_stop)

    sp = sub.add_parser("mcp", help="run as an MCP server on standard input and output")
    sp.set_defaults(func=cmd_mcp)
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    if args.dut:
        args.url = dut_url(args.url, args.dut)
    try:
        return args.func(args) or 0
    except Error as e:
        if args.json:
            _print_json(dict(e.info, error=e.code, message=e.message))
        if e.code == "busy":
            s = e.info.get("session", {})
            _warn("busy: the DUT is used by " + _holder(s))
            if s.get("alive") is False:
                _warn("take it with: booboot session open --force gone")
            return EXIT_BUSY
        _warn(e.message)
        return EXIT_ERROR
    except KeyboardInterrupt:
        return 130
    except OSError as e:
        _warn(str(e))
        return EXIT_ERROR


if __name__ == "__main__":
    sys.exit(main())

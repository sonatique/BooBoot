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
import re
import socket
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

__version__ = "0.1.0"

DEFAULT_URL = "http://booboot.local:8080"
CHUNK = 1 << 20

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

    All calls but status() and open_session() need the session.
    Errors raise booboot.Error.
    """

    def __init__(self, url=DEFAULT_URL, session=None, timeout=30.0):
        self.url = url.rstrip("/")
        self.session = session
        self.timeout = timeout
        # No proxy: the server is on the local network.
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def request(self, method, path, params=None, json_body=None, data=None, headers=None,
                timeout=None, stream=False):
        """Send a request and return the decoded JSON answer.

        With stream=True, return the open response instead.
        """
        url = self.url + "/api/v1" + path
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
            raise Error("cannot reach %s: %s" % (self.url, getattr(e, "reason", e))) from None
        return json.loads(answer.decode("utf-8")) if answer else None

    # Session

    def status(self):
        """Return the state of power, SD card, console and session."""
        return self.request("GET", "/status")

    def open_session(self, client=None, timeout=None, force=False):
        """Open the session needed by all other calls.

        client: name shown to other clients.
        timeout: idle seconds after which the server ends the session.
        force: take the session from another client.
        """
        info = self.request("POST", "/session", json_body={
            "client": client or default_client_name(), "timeout": timeout, "force": force or None})
        self.session = info["session"]
        return info

    def close_session(self):
        if self.session:
            try:
                self.request("DELETE", "/session")
            finally:
                self.session = None

    def keepalive(self):
        return self.request("POST", "/session/keepalive")

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

    def read(self, since="boot", wait=0, clean=False, max_bytes=None):
        """Return console output: {"text", "cursor", "next", ...}.

        wait: seconds to wait for new output when there is none.
        clean: remove escape codes and carriage returns.
        """
        params = {"since": since, "wait": wait or None, "clean": 1 if clean else None, "max": max_bytes}
        return self.request("GET", "/console", params=params, timeout=wait + self.timeout)

    def read_raw(self, since="boot", wait=0):
        """Return (bytes, next cursor)."""
        params = {"since": since, "wait": wait or None, "format": "raw"}
        try:
            with self.request("GET", "/console", params=params, timeout=wait + self.timeout,
                              stream=True) as resp:
                return resp.read(), int(resp.headers.get("X-Next", "0"))
        except OSError as e:
            raise Error("cannot reach %s: %s" % (self.url, e)) from None

    def write(self, text, newline=False):
        """Send text. newline adds the line ending set on the server."""
        return self.request("POST", "/console/write", json_body={"text": text, "newline": newline})

    def expect(self, pattern, since="last", timeout=30, clean=False):
        """Wait for a regex in the console output.

        Result: "matched", "match", "text" (output up to the end of the match)
        and "next" (cursor after the match).
        """
        body = {"pattern": pattern, "since": since, "timeout": timeout, "clean": clean}
        return self.request("POST", "/console/expect", json_body=body, timeout=timeout + self.timeout)

    def run(self, command, prompt=None, timeout=30, clean=True):
        """Send a command line and wait for the prompt regex.

        Result: "matched" and "output" (without the command echo and the prompt line).
        """
        body = {"command": command, "prompt": prompt, "timeout": timeout, "clean": clean}
        return self.request("POST", "/console/run", json_body=body, timeout=timeout + self.timeout)


# Command line

EPILOG = """\
Card paths are N:/path, N being the partition number. "/path" means "1:/path".
Commands open a session when needed and keep its token in a local file.

examples:
  booboot status
  booboot deploy BOOT.BIN image.ub --expect "login: " --timeout 120
  booboot console run "uname -a"
  booboot sd put BOOT.BIN image.ub 1:/
  booboot console attach

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


def _save_token(url, token):
    store = _load_store()
    if token:
        store[url] = token
    else:
        store.pop(url, None)
    path = _store_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(store, f, indent=1)
    os.replace(tmp, path)


def _saved_token(url):
    return os.environ.get("BOOBOOT_SESSION") or _load_store().get(url.rstrip("/"))


def _warn(text):
    sys.stderr.write("booboot: %s\n" % text)
    sys.stderr.flush()


def _client(args, session=True):
    """Return a client. With session, make sure it has a valid session."""
    c = Client(args.url)
    if not session:
        return c
    c.session = _saved_token(c.url)
    if c.session:
        try:
            c.keepalive()
            return c
        except Error as e:
            if e.code != "no_session" or os.environ.get("BOOBOOT_SESSION"):
                raise
            _warn("session expired, opening a new one")
    c.open_session(args.name, args.session_timeout)
    _save_token(c.url, c.session)
    return c


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


def cmd_status(args):
    c = _client(args, session=False)
    c.session = _saved_token(c.url)
    s = c.status()
    if args.json:
        return _print_json(s)
    ses = s["session"]
    if not ses["active"]:
        who = "free"
    elif ses.get("yours"):
        who = "yours (expires after %gs idle)" % ses["timeout"]
    else:
        who = "used by %s (idle %gs, expires in %gs)" % (ses["client"], ses["idle"], ses["expires_in"])
    con = s["console"]
    lines = [
        "%s, BooBoot %s" % (s["name"], s["version"]),
        "power:   %s" % s["power"]["state"] + (" (%s)" % s["power"]["error"] if s["power"]["error"] else ""),
        "sd card: %s" % s["sd"]["mode"] + (" (%s)" % s["sd"]["error"] if s["sd"]["error"] else "")
        + ", content %s" % s["sd"]["card"]["state"],
        "console: %s at %s baud" % (con["device"], con["baudrate"])
        + ("" if con["connected"] else " (not connected: %s)" % con["error"]),
        "session: " + who,
    ]
    if s["operation"]:
        lines.append("busy:    " + s["operation"]["name"])
    print("\n".join(lines))


def cmd_session_open(args):
    c = _client(args, session=False)
    info = c.open_session(args.name, args.timeout, args.force)
    _save_token(c.url, c.session)
    if args.json:
        return _print_json(info)
    print("session opened (ends after %gs idle)" % info["timeout"])


def cmd_session_close(args):
    c = _client(args, session=False)
    c.session = _saved_token(c.url)
    if not c.session:
        print("no session")
        return
    try:
        c.close_session()
        print("session closed")
    except Error as e:
        if e.code not in ("no_session", "busy"):
            raise
        print("session had already expired")
    finally:
        _save_token(c.url, None)


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


def cmd_sd_put(args):
    c = _client(args)
    part, path = split_path(args.dest)
    as_dir = path.endswith("/") or len(args.files) > 1
    if as_dir and not path.endswith("/"):
        path += "/"
    results = []
    for local in args.files:
        remote = "%d:%s" % (part, path + os.path.basename(local) if as_dir else path)
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
    c = _client(args)
    if args.json:
        return _print_json(c.read(args.since, clean=args.clean))
    since = args.since
    out = sys.stdout.buffer
    while True:
        if args.clean:
            r = c.read(since, wait=30 if args.follow else 0, clean=True)
            data, since = r["text"].encode("utf-8"), r["next"]
        else:
            data, since = c.read_raw(since, wait=30 if args.follow else 0)
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
    if not r["matched"]:
        _warn("timeout: %r not seen within %gs" % (args.pattern, args.timeout))
        return EXIT_TIMEOUT


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


def cmd_deploy(args):
    c = _client(args)
    result = {"power_off": c.power_off()}
    if args.image:
        _warn("writing %s" % args.image)
        result["image"] = c.write_image(args.image, args.verify, progress=_progress(args))
    part, path = split_path(args.dest)
    if not path.endswith("/"):
        path += "/"
    result["files"] = []
    for local in args.files:
        remote = "%d:%s%s" % (part, path, os.path.basename(local))
        _warn("copying %s to %s" % (local, remote))
        result["files"].append(c.put_file(local, remote, _progress(args)))
    result["power_on"] = c.power_on()
    _warn("power on")
    code = None
    if args.expect:
        r = c.expect(args.expect, "boot", args.timeout, clean=True)
        result["expect"] = r
        if not args.json and not args.quiet:
            _print_text(r["text"])
        if not r["matched"]:
            _warn("timeout: %r not seen within %gs" % (args.expect, args.timeout))
            code = EXIT_TIMEOUT
    if args.json:
        _print_json(result)
    return code


def build_parser():
    p = argparse.ArgumentParser(
        prog="booboot", description="Control a device under test through a BooBoot server.",
        epilog=EPILOG, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("-u", "--url", default=os.environ.get("BOOBOOT_URL", DEFAULT_URL),
                   help="server URL (env BOOBOOT_URL, default %(default)s)")
    p.add_argument("--json", action="store_true", help="print results as JSON")
    p.add_argument("--name", help="client name shown to other clients (default user@host)")
    p.add_argument("--session-timeout", type=float, metavar="S",
                   help="idle seconds before the server ends a session opened by this command")
    p.add_argument("--version", action="version", version=__version__)
    sub = p.add_subparsers(dest="command", metavar="COMMAND")
    sub.required = True

    sp = sub.add_parser("status", help="show power, SD card, console and session state")
    sp.set_defaults(func=cmd_status)

    ses = sub.add_parser("session", help="open or close the session").add_subparsers(dest="action", metavar="ACTION")
    ses.required = True
    sp = ses.add_parser("open", help="open the session (other commands open one when needed)")
    sp.add_argument("--timeout", type=float, metavar="S", help="idle seconds before the server ends the session")
    sp.add_argument("--force", action="store_true", help="take the session from another client")
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
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        return args.func(args) or 0
    except Error as e:
        if args.json:
            _print_json(dict(e.info, error=e.code, message=e.message))
        if e.code == "busy":
            s = e.info.get("session", {})
            _warn("busy: the DUT is used by %s (idle %gs, free in %gs at most)"
                  % (s.get("client", "?"), s.get("idle", 0), s.get("expires_in", 0)))
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

"""Client library and CLI against a stub HTTP server.

Uses only the client, so it also runs on Windows and macOS.
"""

import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

CLIENT_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "client")
sys.path.insert(0, CLIENT_DIR)
import booboot  # noqa: E402

STATUS = {
    "name": "dut1", "version": "0.1.0",
    "power": {"state": "off", "backend": "gpio", "error": ""},
    "sd": {"mode": "dut", "error": "", "card": {"state": "unknown"}},
    "console": {"connected": True, "device": "/dev/ttyUSB0", "baudrate": 921600,
                "cursor": 0, "boot": 0, "last": 0, "error": ""},
    "operation": None,
    "session": {"active": True, "client": "other@pc", "idle": 3.0, "expires_in": 297.0,
                "timeout": 300, "yours": False},
}


class Stub(BaseHTTPRequestHandler):
    """Answers from server.answers[(method, path)]: (status, body, content type, headers)."""

    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def _answer(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        path = self.path.split("?")[0]
        self.server.requests.append((self.command, self.path, dict(self.headers), body))
        status, data, ctype, headers = self.server.answers.get(
            (self.command, path), (404, {"error": "not_found", "message": "no stub"}, None, {}))
        if not isinstance(data, bytes):
            data, ctype = json.dumps(data).encode(), "application/json"
        self.send_response(status)
        self.send_header("Content-Type", ctype or "application/octet-stream")
        self.send_header("Content-Length", str(len(data)))
        for key, value in headers.items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(data)

    do_GET = do_PUT = do_POST = do_DELETE = _answer


class ClientTest(unittest.TestCase):
    def setUp(self):
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Stub)
        self.server.daemon_threads = True
        self.server.requests = []
        self.server.answers = {}
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.url = "http://127.0.0.1:%d" % self.server.server_address[1]
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp)
        env = {"LOCALAPPDATA": self.tmp, "XDG_CACHE_HOME": self.tmp, "BOOBOOT_SESSION": "",
               "HTTP_PROXY": "http://127.0.0.1:9", "http_proxy": "http://127.0.0.1:9"}
        patcher = mock.patch.dict(os.environ, env)
        patcher.start()
        self.addCleanup(patcher.stop)

    def answer(self, method, path, data, status=200, ctype=None, headers=None):
        self.server.answers[(method, "/api/v1" + path)] = (status, data, ctype, headers or {})

    def cli(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = booboot.main(["--url", self.url] + list(args))
        return code, out.getvalue(), err.getvalue()

    def test_json_request(self):
        # The proxy set in the environment is not used.
        self.answer("PUT", "/power", {"power": "on", "boot": 12})
        c = booboot.Client(self.url, session="tok")
        self.assertEqual(c.power_on(), {"power": "on", "boot": 12})
        method, path, headers, body = self.server.requests[-1]
        self.assertEqual((method, path), ("PUT", "/api/v1/power"))
        self.assertEqual(headers["Authorization"], "Bearer tok")
        self.assertEqual(json.loads(body), {"state": "on"})
        self.answer("POST", "/power/cycle", {"power": "on", "boot": 20})
        c.power_cycle()
        self.assertEqual(json.loads(self.server.requests[-1][3]), {})  # None values are not sent

    def test_errors(self):
        self.answer("POST", "/session", {"error": "busy", "message": "used", "session": {"client": "x"}}, 423)
        with self.assertRaises(booboot.Error) as e:
            booboot.Client(self.url).open_session("me")
        self.assertEqual((e.exception.status, e.exception.code), (423, "busy"))
        self.assertEqual(e.exception.info["session"]["client"], "x")
        self.answer("GET", "/status", b"oops", 500, "text/plain")
        with self.assertRaises(booboot.Error) as e:
            booboot.Client(self.url).status()
        self.assertEqual((e.exception.status, e.exception.message), (500, "HTTP 500 Internal Server Error"))
        with self.assertRaises(booboot.Error) as e:
            booboot.Client("http://127.0.0.1:9", timeout=5).status()
        self.assertIn("cannot reach", e.exception.message)

    def test_files(self):
        local = os.path.join(self.tmp, "BOOT.BIN")
        with open(local, "wb") as f:
            f.write(b"b" * 3000)
        self.answer("PUT", "/sd/files/1/dir%20a/BOOT.BIN", {"path": "/dir a/BOOT.BIN", "size": 3000})
        seen = []
        booboot.Client(self.url).put_file(local, "1:/dir a/BOOT.BIN", lambda done, total: seen.append((done, total)))
        method, path, headers, body = self.server.requests[-1]
        self.assertEqual((headers["Content-Length"], body), ("3000", b"b" * 3000))
        self.assertEqual(seen[-1], (3000, 3000))

        self.answer("GET", "/sd/files/2/etc", {"path": "/etc", "entries": [{"name": "a", "type": "file"}]})
        self.answer("GET", "/sd/files/1/BOOT.BIN", b"content")
        c = booboot.Client(self.url)
        self.assertEqual(c.list_dir("2:/etc")[0]["name"], "a")
        out = io.BytesIO()
        self.assertEqual(c.get_file("/BOOT.BIN", out), 7)
        self.assertEqual(out.getvalue(), b"content")
        with self.assertRaises(booboot.Error) as e:
            c.list_dir("1:/BOOT.BIN")
        self.assertEqual(e.exception.code, "not_a_directory")
        with self.assertRaises(booboot.Error) as e:
            c.get_file("2:/etc", io.BytesIO())
        self.assertEqual(e.exception.code, "is_a_directory")

    def test_boot_time(self):
        self.answer("POST", "/power/cycle", {"power": "on", "boot": 100})
        self.answer("POST", "/console/expect", {"matched": True, "text": "x", "next": 200, "time": 12.345})
        c = booboot.Client(self.url, session="t")
        self.assertEqual(c.boot_time("login: ", timeout=60, off_time=1), 12.345)
        cycle, expect = self.server.requests[-2:]
        self.assertEqual(json.loads(cycle[3]), {"off_time": 1})
        self.assertEqual(json.loads(expect[3])["since"], "boot")
        self.answer("POST", "/console/expect", {"matched": False, "text": "", "next": 200, "time": None})
        self.assertIsNone(c.boot_time("login: "))

    def test_read_raw(self):
        self.answer("GET", "/console", b"\x1b[0mboot\r\n", headers={"X-Next": "42"})
        data, nxt = booboot.Client(self.url).read_raw("boot", wait=1)
        self.assertEqual((data, nxt), (b"\x1b[0mboot\r\n", 42))
        self.assertIn("since=boot", self.server.requests[-1][1])
        self.assertIn("format=raw", self.server.requests[-1][1])

    def test_split_path(self):
        self.assertEqual(booboot.split_path("2:/etc/x"), (2, "/etc/x"))
        self.assertEqual(booboot.split_path("/BOOT.BIN"), (1, "/BOOT.BIN"))
        self.assertEqual(booboot.split_path("3:"), (3, "/"))
        self.assertEqual(booboot.split_path(""), (1, "/"))

    def test_cli_status(self):
        self.answer("GET", "/status", STATUS)
        code, out, err = self.cli("status")
        self.assertEqual(code, 0, err)
        self.assertIn("power:   off", out)
        self.assertIn("used by other@pc (idle 3s, expires in 297s)", out)
        self.assertIn("console: /dev/ttyUSB0 at 921600 baud\n", out)
        code, out, err = self.cli("--json", "status")
        self.assertEqual(json.loads(out)["name"], "dut1")
        written = json.loads(json.dumps(STATUS))
        written["console"]["written"] = 37
        self.answer("GET", "/status", written)
        code, out, err = self.cli("status")
        self.assertIn("921600 baud, 37 bytes sent", out)

    def test_cli_session(self):
        # Busy: exit code 4, and no token is kept.
        self.answer("POST", "/session", {"error": "busy", "message": "used",
                                         "session": {"client": "x", "idle": 1, "expires_in": 5}}, 423)
        code, out, err = self.cli("power", "off")
        self.assertEqual(code, booboot.EXIT_BUSY)
        self.assertIn("used by x", err)
        # Free: the token is kept and used by the next command.
        self.answer("POST", "/session", {"session": "t1", "client": "me", "timeout": 300})
        self.answer("POST", "/session/keepalive", {"active": True})
        self.answer("PUT", "/power", {"power": "off"})
        self.assertEqual(self.cli("power", "off")[:2], (0, "power off\n"))
        self.assertEqual(self.cli("power", "off")[0], 0)
        auth = [r[2].get("Authorization") for r in self.server.requests if r[1] == "/api/v1/power"]
        self.assertEqual(auth, ["Bearer t1", "Bearer t1"])
        self.assertEqual(sum(r[1] == "/api/v1/session" for r in self.server.requests), 2)
        # Expired: a new session is opened, with a warning.
        self.answer("POST", "/session/keepalive", {"error": "no_session", "message": "expired"}, 401)
        code, out, err = self.cli("power", "off")
        self.assertEqual(code, 0)
        self.assertIn("session expired", err)
        self.answer("DELETE", "/session", {"closed": True})
        self.assertEqual(self.cli("session", "close")[1], "session closed\n")
        self.assertEqual(self.cli("session", "close")[1], "no session\n")

    def test_cli_timeout_exit_code(self):
        self.answer("POST", "/session", {"session": "t1", "client": "me", "timeout": 300})
        self.answer("POST", "/console/expect", {"matched": False, "match": None, "text": "abc", "next": 3})
        code, out, err = self.cli("console", "expect", "login:", "--timeout", "1")
        self.assertEqual((code, out), (booboot.EXIT_TIMEOUT, "abc\n"))
        self.assertIn("not seen", err)

    def test_mcp_stdio(self):
        # The MCP server on this system's standard input and output.
        self.answer("GET", "/status", STATUS)
        messages = [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize",
             "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t"}}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "status", "arguments": {}}},
        ]
        data = "".join(json.dumps(m) + "\n" for m in messages).encode()
        p = subprocess.run([sys.executable, os.path.join(CLIENT_DIR, "booboot.py"), "--url", self.url, "mcp"],
                           input=data, capture_output=True, timeout=60)
        self.assertEqual(p.returncode, 0, p.stderr)
        answers = {m["id"]: m for m in map(json.loads, p.stdout.decode().splitlines())}
        self.assertEqual(answers[1]["result"]["protocolVersion"], "2025-06-18")
        self.assertIn("console_run", [t["name"] for t in answers[2]["result"]["tools"]])
        self.assertIn("used by other@pc", answers[3]["result"]["content"][0]["text"])

    def test_cli_help(self):
        with contextlib.redirect_stdout(io.StringIO()) as out, self.assertRaises(SystemExit) as e:
            booboot.main(["--help"])
        self.assertEqual(e.exception.code, 0)
        self.assertIn("exit codes", out.getvalue())


if __name__ == "__main__":
    unittest.main()

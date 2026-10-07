"""Server with fake hardware, used through the client library and the CLI."""

import gzip
import hashlib
import http.client
import io
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

import common
import booboot
from booboot_server import session


class EndToEnd(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        cls.server, cls.dut, cls.url = common.start_fake_server(cls.tmp)
        cls.card = os.path.join(cls.tmp, "card")

    @classmethod
    def tearDownClass(cls):
        common.stop_server(cls.server, cls.dut)
        shutil.rmtree(cls.tmp)

    def setUp(self):
        self.c = booboot.Client(self.url)
        self.c.open_session("test")
        self.addCleanup(self.c.close_session)

    def test_session_busy(self):
        other = booboot.Client(self.url)
        with self.assertRaises(booboot.Error) as e:
            other.open_session("other")
        self.assertEqual((e.exception.status, e.exception.code), (423, "busy"))
        self.assertEqual(e.exception.info["session"]["client"], "test")
        with self.assertRaises(booboot.Error) as e:
            other.power_on()
        self.assertEqual(e.exception.code, "busy")
        status = other.status()
        self.assertTrue(status["session"]["active"])
        self.assertFalse(status["session"]["yours"])
        self.assertTrue(self.c.status()["session"]["yours"])

    @mock.patch.object(session, "GONE_AFTER", 0.5)
    def test_session_gone(self):
        other = booboot.Client(self.url)
        with self.assertRaises(booboot.Error) as e:
            other.open_session("other", force="gone")
        self.assertIsNone(e.exception.info["session"]["alive"])  # no heartbeat yet
        with self.assertLogs("booboot_server.api") as logs:
            self.assertTrue(self.c.heartbeat()["alive"])
            self.c.keepalive()
        self.assertEqual(len(logs.output), 1, logs.output)  # heartbeats are not logged
        time.sleep(0.6)
        self.assertFalse(other.status()["session"]["alive"])
        other.open_session("other", force="gone")
        with self.assertRaises(booboot.Error) as e:
            self.c.heartbeat()
        self.assertEqual(e.exception.code, "busy")
        self.c.session = None
        other.close_session()

    def test_boot_and_shell(self):
        c = self.c
        c.power_off()
        c.put_file(__file__, "1:/BOOT.BIN")
        self.assertIn("BOOT.BIN", [e["name"] for e in c.list_dir("1:/")])
        out = io.BytesIO()
        c.get_file("1:/BOOT.BIN", out)
        with open(__file__, "rb") as f:
            self.assertEqual(out.getvalue(), f.read())

        r = c.power_on()
        self.assertEqual(r["power"], "on")
        self.assertEqual(c.status()["sd"]["mode"], "dut")
        r = c.expect("login: $", since="boot", timeout=10)
        self.assertTrue(r["matched"], r)
        self.assertIn("U-Boot", r["text"])
        # The fake board prints its login prompt about 0.2 s after power on.
        self.assertTrue(0.1 < r["time"] < 2, r["time"])
        stamped = c.read(since="boot", timestamps=True, clean=True)["text"]
        self.assertRegex(stamped, r"\[ +0\.[0-9]{3}\] U-Boot 2024.01")
        self.assertEqual(c.run("root", timeout=5)["output"], "")
        r = c.run("uname -a", timeout=5)
        self.assertEqual(r["output"], "Linux fake 6.6.0-fake #1 SMP armv7l GNU/Linux\n")
        self.assertGreater(r["time"], 0.1)
        self.assertEqual(c.run("echo hello", timeout=5)["output"], "hello\n")
        self.assertIn("Linux fake", c.read(since="boot", clean=True)["text"])

        # The card can not go to the host while the power is on.
        with self.assertRaises(booboot.Error) as e:
            c.sd_mode("host")
        self.assertEqual((e.exception.status, e.exception.code), (409, "power_on"))
        with self.assertRaises(booboot.Error) as e:
            c.put_file(__file__, "1:/x")
        self.assertEqual(e.exception.code, "power_on")

        c.power_off()
        c.delete("1:/BOOT.BIN")
        c.power_on()
        r = c.expect("no boot image", since="boot", timeout=5)
        self.assertTrue(r["matched"])
        c.power_off()

    def test_expect_timeout(self):
        c = self.c
        c.power_off()
        r = c.expect("never", since="now", timeout=0.3)
        self.assertFalse(r["matched"])

    def test_power_switched(self):
        c = self.c
        on = c.power_on()
        self.assertEqual(c.status()["console"]["switches"], on["switch"])
        self.assertEqual(c.power_off()["switch"], on["switch"] + 1)
        # The boot of on ended with the power off: no wait in another boot.
        with self.assertRaises(booboot.Error) as e:
            c.expect_boot(on, "never", timeout=10)
        self.assertEqual((e.exception.status, e.exception.code), (409, "power_switched"))
        self.assertEqual(e.exception.info["switch"]["power"], "off")
        self.assertIn("was switched off at", e.exception.message)
        # A switch by another request during the wait.
        other = booboot.Client(self.url, session=c.session)
        threading.Timer(0.3, other.power_on).start()
        t = time.monotonic()
        with self.assertRaises(booboot.Error) as e:
            c.expect("never", since="now", timeout=10)
        self.assertEqual(e.exception.code, "power_switched")
        self.assertLess(time.monotonic() - t, 5)
        c.power_off()

    def test_write_image(self):
        c = self.c
        c.power_off()
        raw = os.urandom(1 << 20) + b"\0" * (4 << 20)
        path = os.path.join(self.tmp, "image.img.gz")
        with open(path, "wb") as f:
            f.write(gzip.compress(raw))
        r = c.write_image(path, verify=True)
        self.assertEqual(r["bytes"], len(raw))
        self.assertEqual(r["sha256"], hashlib.sha256(raw).hexdigest())
        self.assertTrue(r["verified"])
        with open(os.path.join(self.card, "card.img"), "rb") as f:
            self.assertEqual(f.read(), raw)
        card = c.status()["sd"]["card"]
        self.assertEqual((card["state"], card["sha256"]), ("written", r["sha256"]))

    def test_chunked_upload(self):
        self.c.power_off()
        host, port = self.url[7:].split(":")
        conn = http.client.HTTPConnection(host, int(port), timeout=10)
        conn.request("PUT", "/api/v1/sd/files/2/dir/chunked.bin", body=iter([b"abc", b"defg"]),
                     headers={"Authorization": "Bearer " + self.c.session}, encode_chunked=True)
        resp = conn.getresponse()
        body = resp.read()
        self.assertEqual(resp.status, 200, body)
        self.assertEqual(json.loads(body)["size"], 7)
        conn.close()
        with open(os.path.join(self.card, "p2", "dir", "chunked.bin"), "rb") as f:
            self.assertEqual(f.read(), b"abcdefg")

    def test_errors(self):
        with self.assertRaises(booboot.Error) as e:
            self.c.request("GET", "/nothing")
        self.assertEqual(e.exception.status, 404)
        with self.assertRaises(booboot.Error) as e:
            self.c.request("POST", "/power")
        self.assertEqual(e.exception.status, 405)
        with self.assertRaises(booboot.Error) as e:
            self.c.expect("(", timeout=1)
        self.assertEqual(e.exception.status, 400)
        with self.assertRaises(booboot.Error) as e:
            self.c.list_dir("7:/")
        self.assertEqual(e.exception.status, 404)

    def raw(self, request):
        """Send raw HTTP and return (status line, body)."""
        host, port = self.url[7:].split(":")
        with socket.create_connection((host, int(port)), timeout=10) as s:
            s.sendall(request.replace(b"TOKEN", self.c.session.encode()))
            data = b""
            while True:
                chunk = s.recv(65536)
                if not chunk:
                    break
                data += chunk
        head, _, body = data.partition(b"\r\n\r\n")
        return head.split(b"\r\n")[0].decode(), body

    def test_bad_requests(self):
        auth = b"Authorization: Bearer TOKEN\r\nConnection: close\r\n"
        cases = [
            (b"PUT /api/v1/power HTTP/1.1\r\n" + auth + b"Content-Length: 5\r\n\r\n{bad}", b"not valid JSON"),
            (b"PUT /api/v1/power HTTP/1.1\r\n" + auth + b"Content-Length: 2\r\n\r\n[]", b"JSON object"),
            (b"PUT /api/v1/power HTTP/1.1\r\n" + auth + b"Content-Length: x\r\n\r\n", b"Content-Length"),
            (b"POST /api/v1/console/write?newline=maybe HTTP/1.1\r\n" + auth + b"\r\n", b"true or false"),
            (b"GET /api/v1/console?wait=100 HTTP/1.1\r\n" + auth + b"\r\n", b"between 0 and 60"),
            (b"PUT /api/v1/sd/image?compression=rar HTTP/1.1\r\n" + auth + b"Content-Length: 1\r\n\r\nx",
             b"compression must be"),
        ]
        for request, message in cases:
            status, body = self.raw(request)
            self.assertIn(" 400 ", status + " ", request)
            self.assertIn(message, body, request)

    def test_console_stream(self):
        c = self.c
        c.power_off()
        c.put_file(__file__, "1:/BOOT.BIN")
        viewer = booboot.Client(self.url)  # no session
        events = viewer.stream(since="now")
        hello = next(events)
        self.assertEqual((hello["type"], hello["name"]), ("hello", "dut1"))
        self.assertEqual(hello["cursor"], c.status()["console"]["cursor"])
        self.assertEqual(hello["end"], hello["cursor"])
        self.assertLess(hello["started"], hello["time"])
        got = []

        def collect():
            for e in events:
                if e["type"] == "power" and e["state"] == "on":
                    got.clear()  # drop events from before the power on
                got.append(e)
                if got[0].get("state") == "on" and e.get("state") == "off":
                    return

        t = threading.Thread(target=collect, daemon=True)
        t.start()
        boot = c.power_on()["boot"]
        self.assertTrue(c.expect("login: $", since="boot", timeout=10)["matched"])
        c.power_off()
        t.join(5)
        self.assertEqual([e["type"] for e in got][0], "power")
        self.assertEqual((got[0]["state"], got[0]["cursor"]), ("on", boot))
        self.assertAlmostEqual(got[0]["time"], time.time(), delta=10)
        self.assertEqual((got[-1]["type"], got[-1]["state"]), ("power", "off"))
        output = [e for e in got if e["type"] == "output"]
        self.assertEqual(output[0]["cursor"], boot)
        for i in range(1, len(output)):
            self.assertEqual(output[i - 1]["next"], output[i]["cursor"])
        text = "".join(e["text"] for e in output)
        self.assertIn("U-Boot 2024.01", text)
        self.assertTrue(text.endswith("login: "), text)
        self.assertEqual(got[-1]["cursor"], output[-1]["next"])
        events.close()

        # Output since power on, raw, with the session held by another client.
        host, port = self.url[7:].split(":")
        conn = http.client.HTTPConnection(host, int(port), timeout=10)
        conn.request("GET", "/api/v1/console/stream?format=raw&since=%d" % boot)
        resp = conn.getresponse()
        self.assertEqual(resp.status, 200)
        self.assertTrue(resp.read(len(text)).decode().endswith("login: "))
        conn.close()
        # Reads need no session either.
        self.assertIn("U-Boot", viewer.read(since=boot)["text"])

    def test_stream_ends_with_client(self):
        before = set(threading.enumerate())
        host, port = self.url[7:].split(":")
        sock = socket.create_connection((host, int(port)), timeout=10)
        sock.sendall(b"GET /api/v1/console/stream?since=now HTTP/1.1\r\nHost: x\r\n\r\n")
        self.assertIn(b"200 OK", sock.recv(4096))
        (thread,) = set(threading.enumerate()) - before
        sock.close()
        thread.join(5)
        self.assertFalse(thread.is_alive())

    def test_keep_alive(self):
        # Several requests on one connection, one of them an upload.
        self.c.power_off()
        host, port = self.url[7:].split(":")
        conn = http.client.HTTPConnection(host, int(port), timeout=10)
        headers = {"Authorization": "Bearer " + self.c.session}
        for i in range(3):
            conn.request("PUT", "/api/v1/sd/files/1/k%d" % i, body=b"x" * 1000, headers=headers)
            self.assertEqual(json.loads(conn.getresponse().read())["size"], 1000)
            conn.request("GET", "/api/v1/status", headers=headers)
            self.assertTrue(json.loads(conn.getresponse().read())["session"]["yours"])
        conn.close()
        for i in range(3):
            self.c.delete("1:/k%d" % i)


class Cli(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        cls.server, cls.dut, cls.url = common.start_fake_server(os.path.join(cls.tmp, "fake"))
        cls.env = dict(os.environ, BOOBOOT_URL=cls.url, XDG_CACHE_HOME=cls.tmp, LOCALAPPDATA="")
        cls.env.pop("BOOBOOT_SESSION", None)

    @classmethod
    def tearDownClass(cls):
        common.stop_server(cls.server, cls.dut)
        shutil.rmtree(cls.tmp)

    def cli(self, *args, code=0):
        p = subprocess.run([sys.executable, common.CLIENT] + list(args), env=self.env, cwd=self.tmp,
                           capture_output=True, text=True, timeout=60)
        self.assertEqual(p.returncode, code, p.stdout + p.stderr)
        return p.stdout

    @mock.patch.object(session, "GONE_AFTER", 0.5)
    def test_force_gone(self):
        viewer = booboot.Client(self.url)
        viewer.open_session("viewer", force=True)
        viewer.heartbeat()
        time.sleep(0.6)
        self.assertIn("viewer (gone: no heartbeat for", self.cli("status"))
        self.cli("session", "open", "--force", "gone")
        self.assertIn("yours", self.cli("status"))
        self.cli("session", "close")

    def test_agents(self):
        # Each client name has its own session: agents on one computer do not share one.
        def cmd(name, *args):
            return [sys.executable, common.CLIENT, "--name", name] + list(args)

        for name in ("agent (a)", "agent (b)"):
            self.addCleanup(subprocess.run, cmd(name, "session", "close"), env=self.env, capture_output=True)
        self.cli("--name", "agent (a)", "power", "off")
        self.cli("--name", "agent (b)", "power", "off", code=4)
        status = json.loads(self.cli("--name", "agent (b)", "--json", "status"))
        self.assertEqual((status["session"]["client"], status["session"]["yours"]), ("agent (a)", False))
        self.assertTrue(json.loads(self.cli("--name", "agent (a)", "--json", "status"))["session"]["yours"])
        # With --wait, b goes on when a releases the DUT.
        release = threading.Timer(1.0, subprocess.run, [cmd("agent (a)", "session", "close")],
                                  {"env": self.env, "capture_output": True})
        release.start()
        self.addCleanup(release.join)
        t = time.monotonic()
        p = subprocess.run(cmd("agent (b)", "--wait", "30", "power", "off"), env=self.env, capture_output=True,
                           text=True, timeout=60)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("waiting for the DUT, used by agent (a)", p.stderr)
        self.assertGreater(time.monotonic() - t, 0.9)
        self.cli("--name", "agent (a)", "--wait", "0.5", "power", "off", code=4)

    def test_flow(self):
        boot = os.path.join(self.tmp, "BOOT.BIN")
        with open(boot, "w") as f:
            f.write("boot")
        status = json.loads(self.cli("--json", "status"))
        self.assertEqual(status["power"]["state"], "off")
        self.assertEqual(status["network"]["hostname"], socket.gethostname())

        out = self.cli("deploy", boot, "--expect", "login: ", "--timeout", "10")
        self.assertIn("fake login:", out)
        self.assertIn("yours", self.cli("status"))
        self.assertEqual(self.cli("console", "run", "root"), "")
        self.assertEqual(self.cli("console", "run", "uname -a"), "Linux fake 6.6.0-fake #1 SMP armv7l GNU/Linux\n")
        written = json.loads(self.cli("--json", "status"))["console"]["written"]
        self.cli("console", "write", "echo from write")
        self.assertIn("from write", self.cli("console", "expect", "(?s)write.*# $", "--timeout", "5"))
        self.assertEqual(json.loads(self.cli("--json", "status"))["console"]["written"], written + 16)
        self.cli("console", "expect", "nothing", "--timeout", "0.3", code=3)

        # Another client holds the session: exit code 4.
        other = booboot.Client(self.url)
        other.open_session("other", force=True)
        self.cli("power", "off", code=4)
        self.cli("session", "open", "--force", "gone", code=4)  # only from a client that is gone
        other.close_session()
        self.cli("power", "off")  # opens a new session

        self.cli("sd", "put", boot, "2:/a/")
        self.assertIn("BOOT.BIN", self.cli("sd", "ls", "2:/a"))
        self.assertEqual(self.cli("sd", "get", "2:/a/BOOT.BIN", "-"), "boot")
        self.cli("sd", "rm", "-r", "2:/a")
        self.assertIn("vfat", self.cli("sd", "parts"))
        self.cli("session", "close")
        self.assertIn("free", self.cli("status"))

    def test_boottime(self):
        boot = os.path.join(self.tmp, "BOOT.BIN")
        with open(boot, "w") as f:
            f.write("boot")
        self.cli("session", "open", "--force")
        self.cli("sd", "put", boot, "1:/")
        out = self.cli("boottime", "login: $", "--runs", "3", "--off-time", "0", "--timeout", "10")
        self.assertRegex(out, r"run 3/3: 0\.[0-9]{3} s\nmin 0\.[0-9]{3} s, mean 0\.[0-9]{3} s, max 0\.[0-9]{3} s")
        r = json.loads(self.cli("--json", "boottime", "login: $", "--runs", "2", "--off-time", "0"))
        self.assertEqual(len(r["times"]), 2)
        self.assertTrue(all(0.1 < t < 2 for t in r["times"]), r)
        self.cli("boottime", "never", "--off-time", "0", "--timeout", "0.5", code=3)
        self.cli("power", "off")
        self.cli("sd", "rm", "1:/BOOT.BIN")
        self.cli("session", "close")

    def test_more_commands(self):
        self.cli("session", "open", "--force", "--timeout", "60")
        self.cli("power", "off")
        image = os.path.join(self.tmp, "card.img.gz")
        with open(image, "wb") as f:
            f.write(gzip.compress(b"\1" * 100000))
        self.assertIn("wrote 100.0 kB", self.cli("sd", "flash", image, "--verify"))
        self.assertEqual(self.cli("sd", "host"), "sd card: host\n")
        self.assertEqual(self.cli("sd", "off"), "sd card: off\n")
        self.assertEqual(self.cli("sd", "dut"), "sd card: dut\n")
        self.cli("sd", "mkdir", "1:/newdir")
        self.assertIn("newdir/", self.cli("sd", "ls"))
        self.cli("sd", "rm", "1:/newdir")
        self.assertEqual(self.cli("power", "cycle", "--off-time", "0"), "power on\n")
        # A read does not wait: wait for the first boot line before it.
        self.cli("console", "expect", "Fake FSBL", "--since", "boot", "--timeout", "10", "-q")
        self.assertIn("Fake FSBL", self.cli("console", "read", "--since", "boot"))
        self.assertRegex(self.cli("console", "read", "-t", "--since", "boot"), r"\[ +0\.[0-9]{3}\] ")
        # Errors are also JSON with --json.
        out = self.cli("--json", "sd", "host", code=1)
        self.assertEqual(json.loads(out)["error"], "power_on")
        self.cli("console", "write", "-n", "-e", r"\x03")
        self.cli("power", "off")
        self.cli("session", "close")


if __name__ == "__main__":
    unittest.main()

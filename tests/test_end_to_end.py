"""Server with fake hardware, used through the client library and the CLI."""

import gzip
import hashlib
import http.client
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

import common
import booboot


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
        self.assertEqual(c.run("root", timeout=5)["output"], "")
        r = c.run("uname -a", timeout=5)
        self.assertEqual(r["output"], "Linux fake 6.6.0-fake #1 SMP armv7l GNU/Linux\n")
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

    def test_flow(self):
        boot = os.path.join(self.tmp, "BOOT.BIN")
        with open(boot, "w") as f:
            f.write("boot")
        status = json.loads(self.cli("--json", "status"))
        self.assertEqual(status["power"]["state"], "off")

        out = self.cli("deploy", boot, "--expect", "login: ", "--timeout", "10")
        self.assertIn("fake login:", out)
        self.assertIn("yours", self.cli("status"))
        self.assertEqual(self.cli("console", "run", "root"), "")
        self.assertEqual(self.cli("console", "run", "uname -a"), "Linux fake 6.6.0-fake #1 SMP armv7l GNU/Linux\n")
        self.cli("console", "write", "echo from write")
        self.assertIn("from write", self.cli("console", "expect", "(?s)write.*# $", "--timeout", "5"))
        self.cli("console", "expect", "nothing", "--timeout", "0.3", code=3)

        # Another client holds the session: exit code 4.
        other = booboot.Client(self.url)
        other.open_session("other", force=True)
        self.cli("power", "off", code=4)
        other.close_session()
        self.cli("power", "off")  # opens a new session

        self.cli("sd", "put", boot, "2:/a/")
        self.assertIn("BOOT.BIN", self.cli("sd", "ls", "2:/a"))
        self.assertEqual(self.cli("sd", "get", "2:/a/BOOT.BIN", "-"), "boot")
        self.cli("sd", "rm", "-r", "2:/a")
        self.assertIn("vfat", self.cli("sd", "parts"))
        self.cli("session", "close")
        self.assertIn("free", self.cli("status"))


if __name__ == "__main__":
    unittest.main()

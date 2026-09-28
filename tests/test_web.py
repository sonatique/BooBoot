"""The console web page, its files, and the log files of the console."""

import http.client
import json
import os
import shutil
import subprocess
import tempfile
import unittest

import common
import booboot


def find_chrome():
    """Chrome or Chromium for the browser test: BOOBOOT_CHROME, or one in PATH."""
    path = os.environ.get("BOOBOOT_CHROME")
    if path:
        return path
    for name in ("google-chrome", "chromium", "chromium-browser", "chrome"):
        path = shutil.which(name)
        if path:
            return path
    return None


class WebTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        cls.server, cls.dut, cls.url = common.start_fake_server(os.path.join(cls.tmp, "fake"))
        host, port = cls.url[7:].split(":")
        cls.address = (host, int(port))

    @classmethod
    def tearDownClass(cls):
        common.stop_server(cls.server, cls.dut)
        shutil.rmtree(cls.tmp)

    def get(self, path):
        conn = http.client.HTTPConnection(*self.address, timeout=10)
        conn.request("GET", path)
        resp = conn.getresponse()
        body = resp.read()
        conn.close()
        return resp.status, resp.getheader("Content-Type"), body

    def boot(self):
        """Boot the fake board and log in, so that the console has output."""
        c = booboot.Client(self.url)
        c.open_session("web-test", force=True)
        try:
            c.power_off()
            c.put_file(__file__, "1:/BOOT.BIN")
            c.power_on()
            self.assertTrue(c.expect("login: $", since="boot", timeout=10)["matched"])
            c.run("root", timeout=5)
        finally:
            c.close_session()

    def test_page(self):
        status, kind, body = self.get("/")
        self.assertEqual((status, kind), (200, "text/html; charset=utf-8"))
        self.assertIn(b'<script src="console.js">', body)
        for name in ("terminal.js", "console.js"):
            status, kind, body = self.get("/" + name)
            self.assertEqual((status, kind), (200, "text/javascript; charset=utf-8"), name)
        self.assertEqual(self.get("/nothing.js")[0], 404)
        self.assertEqual(self.get("/../server.py")[0], 404)
        self.server.web = False
        try:
            self.assertEqual(self.get("/")[0], 404)
            self.assertEqual(self.get("/api/v1/status")[0], 200)
        finally:
            self.server.web = True

    def test_logs(self):
        self.boot()
        status, kind, body = self.get("/api/v1/logs")
        self.assertEqual(status, 200)
        logs = json.loads(body)
        names = [f["name"] for f in logs["files"]]
        self.assertEqual(logs["current"], names[0])
        self.assertTrue(all(f["size"] >= 0 and f["mtime"] > 0 for f in logs["files"]))
        status, kind, body = self.get("/api/v1/logs/" + logs["current"])
        self.assertEqual((status, kind), (200, "text/plain; charset=utf-8"))
        self.assertIn(b"] U-Boot 2024.01 (fake)", body)
        for bad in ("latest.log", "..%2F..%2Fetc%2Fpasswd", "console-1.log", "x"):
            self.assertEqual(self.get("/api/v1/logs/" + bad)[0], 404, bad)

    def test_terminal_model(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("node not found")
        p = subprocess.run([node, os.path.join(common.ROOT, "tests", "terminal_check.js")],
                           capture_output=True, text=True, timeout=60)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)

    def test_browser(self):
        chrome = find_chrome()
        if not chrome:
            self.skipTest("Chrome not found (set BOOBOOT_CHROME)")
        self.boot()
        profile = tempfile.mkdtemp()
        try:
            p = subprocess.run([chrome, "--headless", "--no-sandbox", "--disable-gpu", "--no-first-run",
                                "--user-data-dir=" + profile, "--virtual-time-budget=5000", "--dump-dom",
                                self.url + "/"], capture_output=True, text=True, timeout=120)
        finally:
            shutil.rmtree(profile, ignore_errors=True)
        dom = p.stdout
        self.assertIn('<span id="name">dut1</span>', dom, p.stderr[-2000:])
        self.assertIn("Connected", dom)
        self.assertRegex(dom, r'<div class="m">---- power on \d{4}-\d\d-\d\d \d\d:\d\d:\d\d\.\d{3} ----\n</div>')
        self.assertIn("<div>U-Boot 2024.01 (fake)\n</div>", dom)
        self.assertIn('<div>[    1.234567] <span style="color: rgb(13, 188, 121);">OK</span>'
                      ' Reached target Multi-User System.\n</div>', dom)
        self.assertIn("<div>root@fake:~# ", dom)


if __name__ == "__main__":
    unittest.main()

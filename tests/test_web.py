"""The console web page, its files, and the log files of the console."""

import http.client
import json
import os
import select
import shutil
import subprocess
import tempfile
import time
import unittest
from unittest import mock

import common
import booboot
from booboot_server import netinfo, session


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


class Browser:
    """Headless Chrome, driven with the DevTools protocol over pipes."""

    def __init__(self, chrome):
        # The first start of Chrome on a machine can be slow, as it builds its caches, or get no answer: one
        # more start then.
        for attempt in range(2):
            self._start(chrome)
            try:
                target = self.call("Target.createTarget", url="about:blank")["targetId"]
                break
            except AssertionError:
                self.close()
                if attempt:
                    raise
        self.answer_time = 30
        self.session = self.call("Target.attachToTarget", targetId=target, flatten=True)["sessionId"]
        self.call("Page.enable", session=True)

    def _start(self, chrome):
        self.profile = tempfile.mkdtemp()
        cmd_r, self._cmd = os.pipe()
        self._out, out_w = os.pipe()
        # Chrome reads commands on fd 3 and writes answers on fd 4. bash, as dash only takes fds 0 to 9.
        self.proc = subprocess.Popen(
            ["bash", "-c", 'exec "$0" "$@" 3<&%d 4>&%d' % (cmd_r, out_w), chrome, "--headless", "--no-sandbox",
             "--disable-gpu", "--no-first-run", "--user-data-dir=" + self.profile, "--remote-debugging-pipe",
             "about:blank"],
            pass_fds=(cmd_r, out_w), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        os.close(cmd_r)
        os.close(out_w)
        self._buf = b""
        self._id = 0
        self.events = []
        self.answer_time = 60

    def call(self, method, session=False, **params):
        self._id += 1
        msg = {"id": self._id, "method": method, "params": params}
        if session:
            msg["sessionId"] = self.session
        os.write(self._cmd, json.dumps(msg).encode() + b"\0")
        while True:
            m = self._read(self.answer_time)
            if m.get("id") == self._id:
                if "error" in m:
                    raise AssertionError("%s: %s" % (method, m["error"]))
                return m["result"]
            self.events.append(m)

    def _read(self, timeout):
        while b"\0" not in self._buf:
            if not select.select([self._out], [], [], timeout)[0]:
                raise AssertionError("no answer from Chrome")
            data = os.read(self._out, 65536)
            if not data:
                raise AssertionError("Chrome stopped")
            self._buf += data
        msg, _, self._buf = self._buf.partition(b"\0")
        return json.loads(msg)

    def eval(self, expression):
        r = self.call("Runtime.evaluate", session=True, expression=expression, returnByValue=True)
        return r["result"].get("value")

    def wait(self, expression, what, timeout=10):
        deadline = time.monotonic() + timeout
        while not self._true(expression):
            if time.monotonic() > deadline:
                raise AssertionError("timeout: %s; screen: %r" % (what, self.eval("document.body.innerText")[-500:]))
            time.sleep(0.05)

    def _true(self, expression):
        try:
            return self.eval(expression)
        except AssertionError as e:
            # While the page goes to another address, it has no context to evaluate in for a moment.
            if "navigated" in str(e) or "context" in str(e).lower():
                return False
            raise

    def key(self, key, text=None, modifiers=0):
        params = {"key": key, "modifiers": modifiers}
        if text:
            params["text"] = text
        self.call("Input.dispatchKeyEvent", session=True, type="keyDown" if text else "rawKeyDown", **params)
        self.call("Input.dispatchKeyEvent", session=True, type="keyUp", key=key, modifiers=modifiers)

    def type(self, text):
        for ch in text:
            self.key(ch, ch)

    def accept_dialog(self, timeout=10, accept=True, text=None):
        deadline = time.monotonic() + timeout
        while not any(e.get("method") == "Page.javascriptDialogOpening" for e in self.events):
            if time.monotonic() > deadline:
                raise AssertionError("no dialog")
            self.events.append(self._read(timeout))
        message = [e for e in self.events if e.get("method") == "Page.javascriptDialogOpening"][-1]
        self.events.clear()
        answer = {} if text is None else {"promptText": text}
        self.call("Page.handleJavaScriptDialog", session=True, accept=accept, **answer)
        return message["params"]["message"]

    def close(self):
        self.proc.kill()
        self.proc.wait()
        os.close(self._cmd)
        os.close(self._out)
        shutil.rmtree(self.profile, ignore_errors=True)


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
        self.assertEqual(self.get("/favicon.svg")[:2], (200, "image/svg+xml"))
        self.assertEqual(self.get("/favicon.ico")[:2], (200, "image/x-icon"))
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
        if not chrome or not shutil.which("bash"):
            self.skipTest("Chrome or bash not found (set BOOBOOT_CHROME)")
        self.boot()
        b = Browser(chrome)
        self.addCleanup(b.close)
        b.call("Page.navigate", session=True, url=self.url + "/")
        b.wait("document.getElementById('screen').textContent.includes('root@fake:~# ')", "shell prompt")
        dom = b.eval("document.documentElement.outerHTML")
        self.assertIn('<span id="name">dut1</span>', dom)
        self.assertIn("Connected", dom)
        self.assertRegex(dom, r'<div class="m">---- power on \d{4}-\d\d-\d\d \d\d:\d\d:\d\d\.\d{3} ----\n</div>')
        self.assertIn("<div>U-Boot 2024.01 (fake)\n</div>", dom)
        self.assertIn('<div>[    1.234567] <span style="color: rgb(13, 188, 121);">OK</span>'
                      ' Reached target Multi-User System.\n</div>', dom)
        self.assertIn("<div>root@fake:~# ", dom)

    def test_typing(self):
        chrome = find_chrome()
        if not chrome or not shutil.which("bash"):
            self.skipTest("Chrome or bash not found (set BOOBOOT_CHROME)")
        self.boot()
        b = Browser(chrome)
        self.addCleanup(b.close)
        screen = "document.getElementById('screen').textContent"
        info = "document.getElementById('info').textContent"
        b.call("Page.navigate", session=True, url=self.url + "/")
        b.wait(screen + ".endsWith('root@fake:~# \\n')", "shell prompt")
        # The page is at 127.0.0.1: it shows another address of the board.
        address = netinfo.addresses()[:1]
        if address:
            b.wait("%s.includes('also at %s:%d')" % (info, address[0], self.server.server_address[1]), "address")
        b.type("x")
        time.sleep(0.3)
        self.assertNotIn("# x", b.eval(screen), "keys sent without control")

        b.eval("document.getElementById('control').click()")
        b.wait(info + ".includes('in control')", "control taken")
        self.assertEqual(self.status()["client"], "127.0.0.1")
        deadline = time.monotonic() + 5
        while not self.status()["alive"] and time.monotonic() < deadline:
            time.sleep(0.1)
        self.assertTrue(self.status()["alive"], "heartbeat")
        b.type("echo typedd")
        b.key("Backspace")
        b.key("Enter")
        b.wait("/echo typed *\\ntyped\\n/.test(%s)" % screen, "typed command and its output")
        self.assertIn("class=\"cursor", b.eval("document.getElementById('screen').innerHTML"))
        b.eval("const d = new DataTransfer(); d.setData('text/plain', 'echo pasted\\n');"
               "document.dispatchEvent(new ClipboardEvent('paste', {clipboardData: d}))")
        b.wait("/echo pasted *\\npasted\\n/.test(%s)" % screen, "pasted command")
        b.key("c", modifiers=2)
        b.wait(screen + ".includes('^C')", "Ctrl+C")

        # Another client takes the DUT: the next key ends the control.
        other = booboot.Client(self.url)
        other.open_session("colleague", force=True)
        b.type("y")
        b.wait(info + ".includes('control lost: the DUT is used by colleague')", "control lost")
        # Taking it back asks first.
        b.eval("document.getElementById('control').click()")
        self.assertIn("used by colleague, idle for", b.accept_dialog())
        b.wait(info + ".includes('in control')", "control taken over")
        # A client that sends heartbeats is connected, and gone when they stop.
        other.open_session("colleague", force=True)
        other.heartbeat()
        b.wait(info + ".includes('control lost')", "control lost again")
        b.eval("document.getElementById('control').click()")
        self.assertIn("used by colleague, which is connected", b.accept_dialog())
        b.wait(info + ".includes('in control')", "connected client taken over")
        with mock.patch.object(session, "GONE_AFTER", 0.5):
            other.open_session("colleague", force=True)
            other.heartbeat()
            b.wait(info + ".includes('used by colleague (gone)')", "gone client shown")
            b.eval("document.getElementById('control').click()")
            self.assertIn("used by colleague, which is gone", b.accept_dialog())
            b.wait(info + ".includes('in control')", "gone client taken over")

        # The power button works in control. Power off asks first.
        power = "document.getElementById('power')"
        power_state = lambda: self.dut.status()["power"]["state"]  # noqa: E731
        self.assertEqual(b.eval(power + ".disabled"), False)
        self.assertEqual(b.eval(power + ".textContent"), "Power off")
        # A click that asks waits for the answer: it runs after this call returns.
        b.eval("setTimeout(() => %s.click())" % power)
        self.assertEqual(b.accept_dialog(accept=False), "Switch the DUT off?")
        self.assertEqual(power_state(), "on")
        b.eval("setTimeout(() => %s.click())" % power)
        b.accept_dialog()
        b.wait(power + ".textContent === 'Power on' && !%s.disabled" % power, "button after power off")
        self.assertEqual(power_state(), "off")
        b.eval(power + ".click()")
        b.wait(power + ".textContent === 'Power off' && !%s.disabled" % power, "button after power on")
        self.assertEqual(power_state(), "on")

        # The DUT panel: name, address and details, and the label, set in control, shown with the name.
        field = "document.getElementById('dut-%s')"
        name = "document.getElementById('name').textContent"
        b.eval("document.getElementById('dut').click()")
        b.wait("%s && %s.textContent.startsWith('BooBoot ')" % (field % "server", field % "server"), "DUT panel")
        self.assertEqual((b.eval(field % "name" + ".value"), b.eval(field % "address" + ".value")),
                         ("dut1", self.url))
        self.assertIn(" baud", b.eval(field % "console" + ".textContent"))
        self.assertEqual(b.eval(field % "power" + ".textContent"), "fake relay, on")
        self.assertEqual(b.eval("document.activeElement.id"), "dut-label")
        # Keys typed in the panel go to the label, not to the DUT.
        b.type("ZCU102 rev B")
        b.key("Enter")
        b.wait(name + " === 'ZCU102 rev B (dut1)'", "label shown")
        self.assertEqual((booboot.Client(self.url).status()["label"], b.eval("document.title"),
                          b.eval(field % "note" + ".textContent")),
                         ("ZCU102 rev B", "ZCU102 rev B (dut1) - BooBoot", "Label set"))
        self.assertNotIn("ZCU102", b.eval(screen))
        b.eval(field % "label" + ".value = ''; " + field % "set" + ".click()")
        b.wait(name + " === 'dut1'", "label removed")
        b.key("Escape")
        b.wait("document.getElementById('panel').hidden", "panel closed")

        b.eval("document.getElementById('control').click()")
        b.wait(info + ".includes('session free')", "control released")
        self.assertFalse(self.status()["active"])
        self.assertEqual(b.eval(power + ".disabled"), True)
        b.eval("document.getElementById('dut').click()")
        b.wait("%s.disabled && %s.readOnly" % (field % "set", field % "label"), "label locked without control")
        self.assertEqual(b.eval(field % "note" + ".textContent"), "Take control to change it")
        b.eval("document.getElementById('dut').click()")

        # Leaving the page releases the session.
        b.eval("document.getElementById('control').click()")
        b.wait(info + ".includes('in control')", "control taken again")
        b.call("Page.navigate", session=True, url="about:blank")
        deadline = time.monotonic() + 5
        while self.status()["active"] and time.monotonic() < deadline:
            time.sleep(0.1)
        self.assertFalse(self.status()["active"])

    def status(self):
        return json.loads(self.get("/api/v1/status")[2])["session"]


class DutsWebTest(unittest.TestCase):
    """The page of the board, and the list of the DUTs on the page of each DUT."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        cls.server, cls.dut, cls.url = common.start_fake_server(os.path.join(cls.tmp, "fake"), duts=["dut1", "dut2"])

    @classmethod
    def tearDownClass(cls):
        common.stop_server(cls.server)
        shutil.rmtree(cls.tmp)

    def test_browser(self):
        chrome = find_chrome()
        if not chrome or not shutil.which("bash"):
            self.skipTest("Chrome or bash not found (set BOOBOOT_CHROME)")
        dut2 = booboot.Client(self.url + "/duts/dut2")
        dut2.open_session("test")
        self.addCleanup(dut2.close_session)
        dut2.set_label("bench 3")
        self.addCleanup(dut2.set_label, "")
        b = Browser(chrome)
        self.addCleanup(b.close)
        b.call("Page.navigate", session=True, url=self.url + "/")
        b.wait("document.querySelectorAll('#duts tr').length === 2", "list of the DUTs")
        rows = b.eval("[...document.querySelectorAll('#duts tr')].map(r => r.innerText.split('\\t').join(' '))")
        self.assertEqual(rows, ["dut1  off free ", "dut2 bench 3 off used by test "])
        b.call("Page.navigate", session=True, url=self.url + "/duts/dut2/")
        select = "document.getElementById('duts')"
        b.wait("!%s.hidden && %s.value.endsWith('/duts/dut2/')" % (select, select), "list on the page of dut2")
        self.assertEqual(b.eval("[...%s.options].map(o => o.text)" % select), ["dut1", "bench 3 (dut2)"])
        self.assertEqual(b.eval("document.getElementById('name').hidden"), True)
        b.eval("%s.value = '/duts/dut1/'; %s.dispatchEvent(new Event('change'))" % (select, select))
        b.wait("location.pathname === '/duts/dut1/' && %s.value.endsWith('/duts/dut1/')" % select, "page of dut1")
        b.wait("document.getElementById('info').textContent.includes('Connected')", "dut1 connected")


if __name__ == "__main__":
    unittest.main()

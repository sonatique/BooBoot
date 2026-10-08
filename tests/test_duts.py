"""Several DUTs on one server: their URLs, the list of the board, independent sessions, configuration."""

import http.client
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
from booboot_server import config
from booboot_server.__main__ import build_units
from booboot_server.api import Server
from booboot_server.board import Label
from booboot_server.errors import BadRequest


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class DutsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        cls.server, cls.dut, cls.url = common.start_fake_server(os.path.join(cls.tmp, "fake"), duts=["dut1", "dut2"])

    @classmethod
    def tearDownClass(cls):
        common.stop_server(cls.server)
        shutil.rmtree(cls.tmp)

    def get(self, path, port=None):
        conn = http.client.HTTPConnection("127.0.0.1", port or self.server.server_address[1], timeout=10)
        conn.request("GET", path)
        resp = conn.getresponse()
        body = resp.read()
        conn.close()
        return resp.status, resp.getheader("Location"), body

    def test_urls(self):
        self.assertEqual(booboot.Client(self.url).status()["name"], "dut1")  # the first DUT at the root
        status = booboot.Client(self.url + "/duts/dut2").status()
        self.assertEqual((status["name"], status["duts"]), ("dut2", ["dut1", "dut2"]))
        duts = booboot.Client(self.url + "/duts/dut2").request("GET", "/duts")["duts"]
        self.assertEqual([(d["name"], d["url"], d["power"]) for d in duts],
                         [("dut1", "/duts/dut1", "off"), ("dut2", "/duts/dut2", "off")])
        with self.assertRaises(booboot.Error) as e:
            booboot.Client(self.url + "/duts/dut3").status()
        self.assertEqual((e.exception.status, e.exception.message), (404, "no DUT named dut3 on this board"))

    def test_sessions(self):
        a = booboot.Client(self.url + "/duts/dut1")
        b = booboot.Client(self.url + "/duts/dut2")
        a.open_session("a")
        self.addCleanup(a.close_session)
        b.open_session("b")
        self.addCleanup(b.close_session)
        b.power_on()
        self.addCleanup(b.power_off)
        self.assertEqual(a.status()["power"]["state"], "off")
        self.assertEqual(b.status()["power"]["state"], "on")
        with self.assertRaises(booboot.Error) as e:
            booboot.Client(self.url).open_session("c")
        self.assertEqual(e.exception.info["session"]["client"], "a")
        sessions = {d["name"]: d["session"]["client"] for d in a.request("GET", "/duts")["duts"]}
        self.assertEqual(sessions, {"dut1": "a", "dut2": "b"})

    def test_script_url(self):
        c = booboot.Client(self.url + "/duts/dut2")
        c.open_session("s")
        self.addCleanup(c.close_session)
        info = c.run_script("import os, booboot\nprint(os.environ['BOOBOOT_URL'][-10:])\n"
                            "print(booboot.Client.from_env().status()['name'])\n")
        text = "".join(t for t, _ in c.follow_script(info["id"]))
        self.assertEqual(text, "/duts/dut2\ndut2\n")

    def test_pages(self):
        status, _, body = self.get("/")
        self.assertEqual(status, 200)
        self.assertIn(b"api/v1/duts", body)  # the board page
        self.assertIn(b'<script src="console.js">', self.get("/duts/dut2/")[2])
        self.assertEqual(self.get("/duts/dut2")[:2], (301, "/duts/dut2/"))
        self.assertEqual(self.get("/duts/dut2/console.js")[0], 200)
        self.assertEqual(self.get("/duts/nope/")[0], 404)

    def test_command_line(self):
        env = dict(os.environ, BOOBOOT_URL=self.url, XDG_CACHE_HOME=self.tmp, LOCALAPPDATA="")
        env.pop("BOOBOOT_DUT", None)

        def cli(*args, **extra):
            p = subprocess.run([sys.executable, common.CLIENT] + list(args), env=dict(env, **extra),
                               capture_output=True, text=True, timeout=60)
            self.assertEqual(p.returncode, 0, p.stderr)
            return p.stdout

        self.assertEqual(cli("--dut", "dut2", "label", "bench 3"), "label set: bench 3\n")
        self.assertEqual(cli("--dut", "dut2", "label"), "bench 3\n")
        self.assertIn("  label: bench 3", cli("duts"))
        out = cli("--dut", "dut2", "status")
        self.assertIn("\nlabel:   bench 3\n", out)
        self.assertEqual(cli("--dut", "dut2", "label", ""), "label removed\n")
        cli("--dut", "dut2", "session", "close")
        self.assertEqual(cli("--dut", "dut2", "label"), "(no label)\n")
        out = cli("--dut", "dut2", "status")
        self.assertTrue(out.startswith("dut2, BooBoot"), out)
        self.assertIn("other DUTs of the board: dut1 (booboot duts)", out)
        self.assertTrue(cli("status", BOOBOOT_DUT="dut2").startswith("dut2, "))
        lines = cli("duts").splitlines()
        self.assertEqual([line.split()[:3] for line in lines],
                         [["dut1", self.url + "/duts/dut1", "power"], ["dut2", self.url + "/duts/dut2", "power"]])

    def test_label(self):
        c = booboot.Client(self.url + "/duts/dut2")
        with self.assertRaises(booboot.Error) as e:
            c.set_label("bench 3")
        self.assertEqual(e.exception.status, 401)  # it needs the session
        c.open_session("labeller")
        self.addCleanup(c.close_session)
        self.assertEqual(c.set_label(" ZCU102 rev B, bench 3 ")["label"], "ZCU102 rev B, bench 3")
        self.addCleanup(c.set_label, "")
        self.assertEqual(c.status()["label"], "ZCU102 rev B, bench 3")
        self.assertEqual({d["name"]: d["label"] for d in c.duts()["duts"]},
                         {"dut1": "", "dut2": "ZCU102 rev B, bench 3"})
        with self.assertRaises(booboot.Error) as e:
            booboot.Client(self.url + "/duts/dut2").set_label("mine")
        self.assertEqual(e.exception.code, "busy")
        with self.assertRaises(booboot.Error) as e:
            c.set_label("two\nlines")
        self.assertEqual(e.exception.status, 400)

    def test_name_not_found(self):
        # The address the client goes on with keeps the path of the DUT.
        real = socket.getaddrinfo

        def getaddrinfo(host, *args, **kwargs):
            if str(host).endswith(".local"):
                raise socket.gaierror(socket.EAI_NONAME, "Name or service not known")
            return real(host, *args, **kwargs)

        port = self.server.server_address[1]
        with mock.patch("socket.getaddrinfo", getaddrinfo):
            c = booboot.Client("http://localhost.local:%d/duts/dut2" % port)
            self.assertEqual(c.status()["name"], "dut2")
        self.assertEqual(c.base, "http://localhost:%d/duts/dut2" % port)

    def test_own_port(self):
        units = list(self.server.units.values())
        server = Server(("127.0.0.1", 0), units, default=units[1], board=False)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        port = server.server_address[1]
        self.assertEqual(booboot.Client("http://127.0.0.1:%d" % port).status()["name"], "dut2")
        self.assertIn(b'<script src="console.js">', self.get("/", port)[2])  # its page, not the board
        self.assertEqual(booboot.Client("http://127.0.0.1:%d/duts/dut1" % port).status()["name"], "dut1")
        # Scripts still reach their DUT on the port of the board.
        self.assertTrue(units[1].scripts.url.endswith(":%d/duts/dut2" % self.server.server_address[1]))


class ConfigTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp)

    def write(self, name, text):
        with open(os.path.join(self.tmp, name), "w") as f:
            f.write(text)

    def cfg(self, name, sdmux, device):
        cfg = config.load(None)
        cfg["server"].update(name=name, run_dir=self.tmp + "/{name}", log_dir=self.tmp + "/log-{name}")
        cfg["power"]["backend"] = "none"
        cfg["sdmux"]["serial"] = sdmux
        cfg["console"]["device"] = device
        return name, cfg, None, ""

    def test_conflicts(self):
        units = build_units([self.cfg("a", "m1", "/dev/ttyBOOBOOT1"), self.cfg("b", "m2", "/dev/ttyBOOBOOT1"),
                             self.cfg("c", "m1", "none"), self.cfg("a", "m3", "none"),
                             self.cfg("d", "m4", "none"), self.cfg("e", "", "auto"), self.cfg("f", "", "none")])
        for u in units:
            if u.dut:
                self.addCleanup(u.dut.console.stop)
        errors = {u.name: u.error for u in units if u.error}
        self.assertEqual(errors, {
            "b": "uses the same serial adapter /dev/ttyBOOBOOT1 as a: each DUT needs its own",
            "c": "uses the same USB-SD-Mux m1 as a: each DUT needs its own",
            "a": "another DUT has the same name",
            "f": "uses the same USB-SD-Mux (the only one: set serial in [sdmux]) as e: each DUT needs its own"})
        self.assertEqual([u.name for u in units if not u.error], ["a", "d", "e"])

    def test_label_file(self):
        path = os.path.join(self.tmp, "state", "label")
        label = Label(path, "from the configuration")
        self.assertEqual(label.text, "from the configuration")
        label.set("  ZCU102 rev B, bench 3 ")
        self.assertEqual(Label(path, "from the configuration").text, "ZCU102 rev B, bench 3")
        label.set("")
        self.assertEqual(Label(path, "from the configuration").text, "")  # removed for good
        for bad in ("two\nlines", "x" * 101):
            with self.assertRaises(BadRequest):
                label.set(bad)
        self.assertEqual(label.text, "")

    def test_load_dir(self):
        self.write("server.ini", "[server]\nport = 9000\n[scripts]\nenabled = yes\n")
        self.write("a.ini", "[server]\nname = alpha\nport = 9001\n[console]\ndevice = none\n")
        self.write("b.ini", "[scripts]\nenabled = no\n")
        self.write("c.ini", "[server\n")
        board, duts = config.load_dir(self.tmp)
        self.assertEqual(board.getint("server", "port"), 9000)
        self.assertEqual([(n, p, bool(e)) for n, _, p, e in duts], [("alpha", 9001, False), ("b", None, False),
                                                                     ("c", None, True)])
        self.assertTrue(duts[0][1].getboolean("scripts", "enabled"))  # from server.ini
        self.assertFalse(duts[1][1].getboolean("scripts", "enabled"))  # the DUT file has the last word
        self.assertEqual(duts[0][1].get("console", "device"), "none")

    def start(self, port):
        """Start the server on the configuration directory, and return the names of its DUTs."""
        fake = os.path.join(self.tmp, "fake")
        proc = subprocess.Popen([sys.executable, "-m", "booboot_server", "--fake", fake, "--config-dir", self.tmp,
                                 "--host", "127.0.0.1"], cwd=os.path.join(common.ROOT, "server"),
                                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
        self.addCleanup(proc.stderr.close)
        self.addCleanup(proc.wait, 10)
        self.addCleanup(proc.terminate)
        self.proc = proc
        board = booboot.Client("http://127.0.0.1:%d" % port)
        deadline = time.monotonic() + 15
        while True:
            try:
                return [d["name"] for d in board.request("GET", "/duts")["duts"]]
            except booboot.Error:
                self.assertLess(time.monotonic(), deadline, "server not up")
                time.sleep(0.2)

    def test_main(self):
        port, own = free_port(), free_port()
        self.write("server.ini", "[server]\nport = %d\n" % port)
        self.write("dut1.ini", "[server]\nlabel = from the configuration\n")
        self.write("dut2.ini", "[server]\nport = %d\n" % own)
        # A label set before a restart.
        os.makedirs(os.path.join(self.tmp, "fake", "dut2", "state"))
        self.write(os.path.join("fake", "dut2", "state", "label"), "bench 3\n")
        self.assertEqual(self.start(port), ["dut1", "dut2"])
        self.assertEqual(booboot.Client("http://127.0.0.1:%d" % port).status()["label"], "from the configuration")
        self.assertEqual(booboot.Client("http://127.0.0.1:%d" % own).status()["label"], "bench 3")
        self.assertEqual(booboot.Client("http://127.0.0.1:%d" % own).status()["name"], "dut2")
        self.assertEqual(booboot.Client("http://127.0.0.1:%d/duts/dut2" % port).status()["name"], "dut2")

    def test_own_port_taken(self):
        # The DUT still runs, on the port of the board.
        port = free_port()
        taken = socket.socket()
        self.addCleanup(taken.close)
        taken.bind(("127.0.0.1", 0))
        taken.listen()
        self.write("server.ini", "[server]\nport = %d\n" % port)
        self.write("dut1.ini", "[server]\nport = %d\n" % taken.getsockname()[1])
        self.assertEqual(self.start(port), ["dut1"])
        self.assertEqual(booboot.Client("http://127.0.0.1:%d/duts/dut1" % port).status()["power"]["state"], "off")
        self.proc.terminate()
        self.assertIn("dut1: port %d: " % taken.getsockname()[1], self.proc.stderr.read())


if __name__ == "__main__":
    unittest.main()

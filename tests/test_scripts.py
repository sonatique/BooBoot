"""Scripts run on the board: the runner, the client library and the command line."""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

import common
import booboot
from booboot_server.errors import Unavailable
from booboot_server.scripts import Scripts, _complete
from booboot_server.session import Sessions

LOOP = """\
import sys, time, booboot
dut = booboot.Client.from_env()
for i in range(int(sys.argv[1])):
    print("run", i, dut.power_off()["power"])
    time.sleep(float(sys.argv[2]))
print("done")
"""

SLEEP = 'import time\nprint("sleeping")\ntime.sleep(60)\n'


class ScriptTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        cls.server, cls.dut, cls.url = common.start_fake_server(os.path.join(cls.tmp, "fake"), keep=3, output=1)
        cls.dir = os.path.join(cls.tmp, "fake", "scripts")

    @classmethod
    def tearDownClass(cls):
        common.stop_server(cls.server, cls.dut)
        shutil.rmtree(cls.tmp)

    def setUp(self):
        self.c = booboot.Client(self.url)
        self.c.open_session("test", force=True)
        self.addCleanup(self.cleanup)

    def cleanup(self):
        running = self.c.status()["script"]
        if running:
            booboot.Client(self.url).open_session("cleanup", force=True)  # stops the script
            self.wait_end(running["id"])
        try:
            self.c.close_session()
        except booboot.Error:
            pass

    def wait_end(self, sid):
        for _ in self.c.follow_script(sid):
            pass
        return self.c.script(sid)

    def wait_output(self, sid, text):
        deadline = time.monotonic() + 10
        while text not in self.c.script_output(sid, 0, wait=1)["text"]:
            self.assertLess(time.monotonic(), deadline, "no %r in the output" % text)

    def test_run_and_read_later(self):
        info = self.c.run_script(LOOP, "loop.py", ["3", "0.2"])
        self.assertEqual((info["state"], info["name"], info["args"], info["client"]),
                         ("running", "loop.py", ["3", "0.2"], "test"))
        # Another client reads the output while the script runs, and after.
        reader = booboot.Client(self.url)
        first = reader.script_output(info["id"], 0, wait=5)
        self.assertTrue(first["text"].startswith("run 0 off\n"), first)
        text = first["text"] + "".join(t for t, _ in reader.follow_script(info["id"], first["next"]))
        self.assertEqual(text, "run 0 off\nrun 1 off\nrun 2 off\ndone\n")
        end = reader.script(info["id"])
        self.assertEqual((end["state"], end["exit_code"], end["reason"], end["output"]), ("exited", 0, "", len(text)))
        self.assertGreater(end["time"], 0.5)
        self.assertEqual(reader.script_output(info["id"], -5)["text"], "done\n")
        self.assertEqual(reader.scripts()["scripts"][0]["id"], info["id"])

    def test_session(self):
        c = self.c
        info = c.run_script(SLEEP, "sleep.py")
        self.wait_output(info["id"], "sleeping")
        other = booboot.Client(self.url)
        ses = other.status()["session"]
        self.assertEqual((ses["script"], ses["alive"], ses["idle"]), ("sleep.py", True, 0.0))
        self.assertEqual(other.status()["script"]["id"], info["id"])
        with self.assertRaises(booboot.Error) as e:
            c.run_script(SLEEP)
        self.assertEqual((e.exception.status, e.exception.code), (409, "script_running"))
        with self.assertRaises(booboot.Error) as e:
            other.open_session("other", force="gone")  # its client is never gone while it runs
        self.assertEqual(e.exception.code, "busy")
        # Closed while the script runs, the session ends with it.
        r = c.close_session()
        self.assertEqual((r["closed"], r["script"]["id"]), (False, info["id"]))
        self.assertEqual(other.status()["session"]["client"], "test")
        # Taking the session over stops the script.
        other.open_session("other", force=True)
        end = self.wait_end(info["id"])
        self.assertEqual((end["state"], end["reason"], end["exit_code"]),
                         ("stopped", "session taken over by other", -15))
        self.assertEqual(other.status()["session"]["client"], "other")
        other.close_session()

    def test_session_ends_with_script(self):
        info = self.c.run_script("import time\ntime.sleep(0.5)\n")
        self.assertFalse(self.c.close_session()["closed"])
        self.assertEqual(self.wait_end(info["id"])["state"], "exited")
        self.assertFalse(self.c.status()["session"]["active"])

    def test_stop_and_time_limit(self):
        c = self.c
        info = c.run_script(SLEEP, "sleep.py")
        self.wait_output(info["id"], "sleeping")
        c.stop_script(info["id"])
        end = self.wait_end(info["id"])
        self.assertEqual((end["state"], end["reason"], end["exit_code"]), ("stopped", "stopped by test", -15))
        with self.assertRaises(booboot.Error) as e:
            c.stop_script(info["id"])
        self.assertEqual((e.exception.status, e.exception.code), (409, "script_ended"))
        info = c.run_script(SLEEP, "sleep.py", timeout=1)
        self.assertEqual(self.wait_end(info["id"])["reason"], "time limit of 1s")

    def test_processes_left_behind(self):
        t0 = time.monotonic()
        info = self.c.run_script('import subprocess\nsubprocess.Popen(["sleep", "60"])\nprint("left")\n')
        end = self.wait_end(info["id"])
        self.assertLess(time.monotonic() - t0, 10)
        self.assertEqual((end["state"], end["exit_code"]), ("exited", 0))

    def test_output_kept(self):
        # 3 MB of output, 1 MB kept at least.
        line = "%05d " + "é" * 500 + "\n"
        info = self.c.run_script("import sys\nfor i in range(3000):\n    sys.stdout.write(%r %% i)\n" % line)
        end = self.wait_end(info["id"])
        size = len((line % 0).encode("utf-8"))
        self.assertEqual(end["output"], 3000 * size)
        r = self.c.script_output(info["id"], 0)
        self.assertTrue(r["lost"])
        self.assertGreaterEqual(end["output"] - r["cursor"], 1 << 20)
        self.assertEqual(self.c.script_output(info["id"], -size)["text"], line % 2999)

    def test_keep(self):
        ids = []
        for i in range(4):
            ids.append(self.c.run_script("print(%d)\n" % i)["id"])
            self.wait_end(ids[-1])
        # The oldest is deleted, with its files.
        self.assertEqual([s["id"] for s in self.c.scripts()["scripts"]], ids[:0:-1])
        with self.assertRaises(booboot.Error) as e:
            self.c.script(ids[0])
        self.assertEqual(e.exception.status, 404)
        self.assertEqual(sorted(os.listdir(self.dir), key=int), [str(i) for i in ids[1:]])

    def test_bad_requests(self):
        c = self.c
        for name in ("../x.py", "x.txt", "booboot.py", ".x.py"):
            with self.assertRaises(booboot.Error) as e:
                c.run_script("print(1)", name)
            self.assertEqual(e.exception.code, "bad_request", name)
        for body in ({"source": "print(1)", "args": "a"}, {"source": ""}, {"source": "x" * (600 << 10)},
                     {"source": "print(1)", "timeout": 0}, {"source": "print(1)", "timeout": 1e6}):
            with self.assertRaises(booboot.Error) as e:
                c.request("POST", "/scripts", json_body=body)
            self.assertEqual(e.exception.status, 400, str(body)[:80])
        with self.assertRaises(booboot.Error) as e:
            c.script(99999)
        self.assertEqual(e.exception.status, 404)
        with self.assertRaises(booboot.Error) as e:
            booboot.Client(self.url).run_script("print(1)")
        self.assertEqual(e.exception.code, "busy")


class RunnerTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir)

    def test_off(self):
        sessions = Sessions()
        token, _ = sessions.open("a")
        with self.assertRaises(Unavailable) as e:
            Scripts(self.dir, sessions).start(token, "a", "print(1)", "a.py", [])
        self.assertEqual(e.exception.code, "scripts_off")

    def test_server_restart(self):
        for sid, state in ((4, "running"), (7, "exited")):
            os.makedirs(os.path.join(self.dir, str(sid)))
            with open(os.path.join(self.dir, str(sid), "info.json"), "w") as f:
                json.dump({"id": sid, "name": "a.py", "state": state, "reason": ""}, f)
        os.makedirs(os.path.join(self.dir, "9"))  # no information
        scripts = Scripts(self.dir, Sessions(), enabled=True)
        self.assertEqual([(s["id"], s["state"], s["reason"]) for s in scripts.list()],
                         [(7, "exited", ""), (4, "stopped", "server restarted")])
        self.assertEqual(scripts._next, 10)

    def test_complete(self):
        e = "é".encode("utf-8")
        euro = "€".encode("utf-8")
        self.assertEqual(_complete(b"ab" + e), b"ab" + e)
        self.assertEqual(_complete(b"ab" + e[:1]), b"ab")
        self.assertEqual(_complete(b"ab" + euro[:2]), b"ab")
        self.assertEqual(_complete(b""), b"")

    def test_from_env(self):
        with mock.patch.dict(os.environ, {"BOOBOOT_URL": "http://10.1.2.3:8081", "BOOBOOT_SESSION": "t"}):
            c = booboot.Client.from_env()
        self.assertEqual((c.url, c.session), ("http://10.1.2.3:8081", "t"))
        # Without a session, as on a desktop computer, it opens one.
        with mock.patch.dict(os.environ, {"BOOBOOT_URL": "http://10.1.2.3:8081", "BOOBOOT_SESSION": ""}), \
                mock.patch.object(booboot.Client, "open_session") as open_session:
            booboot.Client.from_env()
        open_session.assert_called_once_with()


class CliTest(unittest.TestCase):
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
        return p.stdout, p.stderr

    def script(self, name, text):
        path = os.path.join(self.tmp, name)
        with open(path, "w") as f:
            f.write(text)
        return path

    def test_commands(self):
        loop = self.script("loop.py", LOOP)
        out, err = self.cli("script", "run", loop, "2", "0")
        self.assertEqual(out, "run 0 off\nrun 1 off\ndone\n")
        self.assertRegex(err, r"script (\d+) started\n.*script \1 exited with code 0\n")
        out, err = self.cli("script", "run", self.script("fail now.py", "import sys\nsys.exit(3)\n"), code=1)
        self.assertIn("exited with code 3", err)
        self.assertEqual(self.cli("script", "list")[0].split()[1:4], ["exited", "3", "fail_now.py"])

        out = self.cli("script", "run", "--detach", self.script("sleep.py", SLEEP))[0]
        sid = int(out.split()[1])
        self.assertEqual(out, "script %d started\n" % sid)
        self.assertIn("script:  sleep.py (%d), running for" % sid, self.cli("status")[0])
        deadline = time.monotonic() + 10
        while self.cli("script", "output")[0] != "sleeping\n":
            self.assertLess(time.monotonic(), deadline)
        self.assertEqual(self.cli("session", "close")[0], "the session ends when script %d (sleep.py) ends\n" % sid)
        self.cli("power", "off", code=4)  # the script holds the session
        self.cli("session", "open", "--force")  # stops the script
        out = self.cli("script", "output", str(sid), "-f", code=1)[1]
        self.assertIn("script %d stopped: session taken over by" % sid, out)
        self.cli("session", "close")


if __name__ == "__main__":
    unittest.main()

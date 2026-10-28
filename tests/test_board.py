"""What the board itself does: the messages of the server, and its load."""

import http.client
import json
import logging
import os
import shutil
import tempfile
import time
import unittest

import common
import booboot
from booboot_server.board_state import Activity, BoardLog


class BoardTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        # The messages of the server go to the log of the board only, not to the test output.
        cls.log = BoardLog()
        cls.logger = logging.getLogger("booboot_server")
        cls.saved = (cls.logger.level, cls.logger.propagate)
        cls.logger.setLevel(logging.INFO)
        cls.logger.propagate = False
        cls.logger.addHandler(cls.log)
        cls.activity = Activity(0.1)
        cls.activity.start()
        cls.server, cls.dut, cls.url = common.start_fake_server(
            os.path.join(cls.tmp, "fake"), duts=["dut1", "dut2"], board_log=cls.log, activity=cls.activity)

    @classmethod
    def tearDownClass(cls):
        common.stop_server(cls.server)
        cls.activity.stop()
        cls.logger.removeHandler(cls.log)
        cls.logger.setLevel(cls.saved[0])
        cls.logger.propagate = cls.saved[1]
        shutil.rmtree(cls.tmp)

    def test_log(self):
        c = booboot.Client(self.url + "/duts/dut2")
        c.open_session("board-test")
        self.addCleanup(c.close_session)
        c.power_on()
        c.power_off()
        text = c.request("GET", "/board/log", params={"since": "start"})["text"]
        self.assertRegex(text, r"\n\d{4}-\d\d-\d\d \d\d:\d\d:\d\d\.\d{3} INFO booboot_server\.dut: dut2: power on\n")
        # The same log at the base URL of the board, as a stream.
        conn = http.client.HTTPConnection(*self.server.server_address[:2], timeout=10)
        self.addCleanup(conn.close)
        conn.request("GET", "/api/v1/board/log/stream?since=start")
        resp = conn.getresponse()
        hello = json.loads(resp.readline())
        self.assertEqual((hello["type"], hello["name"], hello["power"]), ("hello", "BooBoot log", ""))
        output = ""
        while "dut2: power off" not in output:
            item = json.loads(resp.readline())
            if item["type"] == "output":
                output += item["text"]

    def test_activity(self):
        deadline = time.monotonic() + 5
        while self.activity.get()["cpu"] is None:
            self.assertLess(time.monotonic(), deadline, "no CPU measure")
            time.sleep(0.05)
        a = booboot.Client(self.url).request("GET", "/board/activity")
        self.assertTrue(0 <= a["cpu"] <= 100, a)
        self.assertTrue(a["traffic"]["in"] >= 0 and a["traffic"]["out"] >= 0, a)
        self.assertTrue(0 <= a["disk"]["free"] <= a["disk"]["total"], a)
        self.assertEqual(a["interval"], 0.1)

    def test_none(self):
        server, _, url = common.start_fake_server(os.path.join(self.tmp, "none"))
        self.addCleanup(common.stop_server, server)
        for path in ("/board/log", "/board/activity"):
            with self.assertRaises(booboot.Error) as e:
                booboot.Client(url).request("GET", path)
            self.assertEqual(e.exception.status, 404)


if __name__ == "__main__":
    unittest.main()

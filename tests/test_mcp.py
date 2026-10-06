"""The MCP server of the client ("booboot mcp") against a server with a fake board."""

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
from booboot_server import session


class Mcp:
    """An MCP client talking to "booboot mcp" in a subprocess."""

    def __init__(self, url, *options):
        self.proc = subprocess.Popen(
            [sys.executable, common.CLIENT, "--url", url] + list(options) + ["mcp"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.next_id = 0
        self.notifications = []

    def send(self, msg):
        self.proc.stdin.write((json.dumps(msg) + "\n").encode())
        self.proc.stdin.flush()

    def request(self, method, params=None):
        self.next_id += 1
        msg = {"jsonrpc": "2.0", "id": self.next_id, "method": method}
        if params is not None:
            msg["params"] = params
        self.send(msg)
        return self.answer(self.next_id)

    def answer(self, mid):
        while True:
            line = self.proc.stdout.readline()
            if not line:
                raise AssertionError("no answer: " + self.proc.stderr.read().decode())
            msg = json.loads(line)
            if "id" in msg and msg["id"] == mid:
                return msg
            self.notifications.append(msg)

    def tool(self, name, progress=None, **arguments):
        params = {"name": name, "arguments": arguments}
        if progress:
            params["_meta"] = {"progressToken": progress}
        result = self.request("tools/call", params)["result"]
        return result["isError"], result["content"][0]["text"]

    def start(self):
        r = self.request("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                        "clientInfo": {"name": "test", "version": "1"}})
        self.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        return r["result"]

    def close(self):
        self.proc.stdin.close()
        code = self.proc.wait(timeout=10)
        self.proc.stdout.close()
        self.proc.stderr.close()
        return code


class McpServerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        cls.server, cls.dut, cls.url = common.start_fake_server(os.path.join(cls.tmp, "fake"))
        cls.boot = os.path.join(cls.tmp, "BOOT.BIN")
        with open(cls.boot, "wb") as f:
            f.write(os.urandom(3 << 20))

    @classmethod
    def tearDownClass(cls):
        common.stop_server(cls.server, cls.dut)
        shutil.rmtree(cls.tmp)

    def setUp(self):
        self.mcp = Mcp(self.url)
        self.addCleanup(self.close)

    def close(self):
        if self.mcp.proc.poll() is None:
            self.assertEqual(self.mcp.close(), 0)

    def test_protocol(self):
        info = self.mcp.start()
        self.assertEqual(info["protocolVersion"], "2025-06-18")
        self.assertEqual(info["serverInfo"]["name"], "booboot")
        self.assertIn("tools", info["capabilities"])
        self.assertIn("Card paths", info["instructions"])
        self.assertEqual(self.mcp.request("ping")["result"], {})

        tools = self.mcp.request("tools/list")["result"]["tools"]
        names = [t["name"] for t in tools]
        for name in ("status", "power", "deploy", "sd_flash", "sd_put", "console_run", "console_expect",
                     "console_read", "boot_time", "session"):
            self.assertIn(name, names)
        for t in tools:
            self.assertEqual(t["inputSchema"]["type"], "object", t["name"])
            self.assertTrue(t["description"], t["name"])
            for arg in t["inputSchema"]["required"]:
                self.assertIn(arg, t["inputSchema"]["properties"], t["name"])
        read_only = {t["name"] for t in tools if t["annotations"]["readOnlyHint"]}
        self.assertEqual(read_only, {"status", "console_read", "console_expect"})

        self.assertEqual(self.mcp.request("resources/list")["error"]["code"], -32601)
        self.assertEqual(self.mcp.request("tools/call", {"name": "nothing"})["error"]["code"], -32602)
        failed, text = self.mcp.tool("console_expect")
        self.assertTrue(failed)
        self.assertIn("missing argument: pattern", text)
        self.mcp.proc.stdin.write(b"{bad json\n")
        self.mcp.proc.stdin.flush()
        self.assertEqual(self.mcp.answer(None)["error"]["code"], -32700)

    def test_version(self):
        r = self.mcp.request("initialize", {"protocolVersion": "1999-01-01", "capabilities": {},
                                            "clientInfo": {"name": "test", "version": "1"}})
        self.assertEqual(r["result"]["protocolVersion"], booboot.MCP_VERSIONS[0])

    def test_board(self):
        m = self.mcp
        m.start()
        failed, text = m.tool("deploy", progress="p1", files=[self.boot], expect="login: $", timeout=10)
        self.assertFalse(failed, text)
        self.assertRegex(text, r"Copied .*BOOT.BIN to 1:/BOOT.BIN \(3.1 MB\)")
        self.assertRegex(text, r"Matched 'login: ' 0\.\d{3} s after power on")
        self.assertIn("U-Boot 2024.01", text)
        done = [n["params"]["progress"] for n in m.notifications if n["method"] == "notifications/progress"]
        self.assertTrue(done and done == sorted(set(done)), done)
        self.assertEqual(done[-1], 3 << 20)

        self.assertFalse(m.tool("console_write", text="root")[0])
        failed, text = m.tool("console_expect", pattern="# $", timeout=5)
        self.assertIn("Matched '# '", text)
        self.assertEqual(m.tool("console_run", command="uname -a"),
                         (False, "Linux fake 6.6.0-fake #1 SMP armv7l GNU/Linux\n"))
        failed, text = m.tool("console_run", command="true", prompt="never", timeout=0.5)
        self.assertIn("Prompt not seen", text)

        failed, text = m.tool("console_read", since="boot", max_bytes=100, timestamps=True)
        self.assertRegex(text, r"^Console output from cursor \d+ to \d+ \(\d+ earlier bytes not shown: since=")
        self.assertRegex(text, r"\[ +0\.\d{3}\] ")
        failed, text = m.tool("boot_time", pattern="login: $", runs=2, off_time=0, timeout=10)
        self.assertRegex(text, r"Run 2: 0\.\d{3} s\nmin 0\.\d{3} s, mean")
        failed, text = m.tool("sd_list")
        self.assertTrue(failed)
        self.assertIn("power off the DUT first", text)

        self.assertEqual(m.tool("power", action="off"), (False, "Power off."))
        failed, text = m.tool("sd_list", path="1:/")
        self.assertIn("BOOT.BIN (3.1 MB)", text)
        copy = os.path.join(self.tmp, "copy")
        os.makedirs(copy, exist_ok=True)
        failed, text = m.tool("sd_get", path="1:/BOOT.BIN", local_path=copy)
        self.assertFalse(failed, text)
        with open(os.path.join(copy, "BOOT.BIN"), "rb") as f, open(self.boot, "rb") as g:
            self.assertEqual(f.read(), g.read())
        self.assertFalse(m.tool("sd_put", files=[self.boot], dest="2:/boot/")[0])
        self.assertIn("boot/", m.tool("sd_list", path="2:/")[1])
        self.assertFalse(m.tool("sd_delete", path="2:/boot", recursive=True)[0])
        self.assertFalse(m.tool("sd_delete", path="1:/BOOT.BIN")[0])
        self.assertEqual(m.tool("sd_mode", mode="dut"), (False, "SD card: dut"))
        self.assertIn("session: yours", m.tool("status")[1])
        failed, text = m.tool("sd_put", files=["/nonexistent/file"], dest="1:/")
        self.assertTrue(failed)
        self.assertIn("No such file", text)

    def test_session(self):
        m = self.mcp
        m.start()
        self.assertIn("free", m.tool("status")[1])
        self.assertEqual(m.tool("power", action="off"), (False, "Power off."))
        other = booboot.Client(self.url)
        other.open_session("colleague", force=True)
        failed, text = m.tool("power", action="off")
        self.assertTrue(failed)
        self.assertIn("used by another client: colleague", text)
        self.assertIn("Session opened", m.tool("session", action="open", force=True)[1])
        self.assertIn("already open", m.tool("session", action="open")[1])
        self.assertIn("released", m.tool("session", action="release")[1])
        self.assertIn("free", m.tool("status")[1])
        m.tool("power", action="off")
        self.assertEqual(m.close(), 0)
        # The session ends with the MCP server.
        self.assertFalse(booboot.Client(self.url).status()["session"]["active"])

    def test_expired_session(self):
        self.mcp.close()
        self.mcp = Mcp(self.url, "--session-timeout", "1")
        m = self.mcp
        m.start()
        self.assertEqual(m.tool("power", action="off"), (False, "Power off."))
        time.sleep(1.5)
        failed, text = m.tool("power", action="off")
        self.assertFalse(failed)
        self.assertTrue(text.startswith("Note: the session had expired and was opened again."), text)
        self.assertEqual(m.tool("power", action="off"), (False, "Power off."))

    @mock.patch.object(session, "GONE_AFTER", 1.0)
    def test_gone_client(self):
        self.mcp.close()
        self.mcp = m = Mcp(self.url, "--name", "agent")
        m.start()
        # A client that sends heartbeats is connected, and gone when they stop.
        viewer = booboot.Client(self.url)
        viewer.open_session("viewer")
        viewer.heartbeat()
        failed, text = m.tool("power", action="off")
        self.assertTrue(failed)
        self.assertIn("viewer (connected,", text)
        viewer.heartbeat()
        self.assertTrue(m.tool("session", action="open", force="gone")[0])
        time.sleep(1.2)
        failed, text = m.tool("power", action="off")
        self.assertIn("viewer (gone: no heartbeat for", text)
        self.assertIn('force "gone"', text)
        self.assertIn("Session opened", m.tool("session", action="open", force="gone")[1])
        self.assertTrue(viewer.status()["session"]["alive"])  # the MCP server sends heartbeats
        # Killed, it leaves its session. The next run of the same name takes it over by itself.
        m.proc.kill()
        m.close()
        time.sleep(1.2)
        self.mcp = m = Mcp(self.url, "--name", "agent")
        m.start()
        failed, text = m.tool("power", action="off")
        self.assertFalse(failed)
        self.assertTrue(text.startswith("Note: took over the session of an earlier client of the same name: "
                                        "agent (gone"), text)
        self.assertTrue(text.endswith("Power off."), text)


if __name__ == "__main__":
    unittest.main()

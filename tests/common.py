"""Test helpers."""

import logging
import os
import sys
import threading
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "server"))
sys.path.insert(0, os.path.join(ROOT, "client"))

from booboot_server import config  # noqa: E402
from booboot_server.__main__ import build  # noqa: E402
from booboot_server.api import Server  # noqa: E402

CLIENT = os.path.join(ROOT, "client", "booboot.py")

# Tests check errors themselves: keep the output clean.
logging.basicConfig(level=logging.CRITICAL)


def start_fake_server(directory, session_timeout=300):
    """Start a server with fake hardware. Return (server, dut, url)."""
    cfg = config.load(None)
    cfg["server"]["session_timeout"] = str(session_timeout)
    dut, sessions, _, prompt = build(cfg, directory)
    server = Server(("127.0.0.1", 0), dut, sessions, prompt)
    dut.start()
    threading.Thread(target=server.serve_forever, daemon=True).start()
    deadline = time.monotonic() + 5
    while not dut.console.connected and time.monotonic() < deadline:
        time.sleep(0.02)
    return server, dut, "http://127.0.0.1:%d" % server.server_address[1]


def stop_server(server, dut):
    server.shutdown()
    server.server_close()
    dut.stop()

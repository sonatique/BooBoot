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
from booboot_server.__main__ import build, build_units  # noqa: E402
from booboot_server.api import Server  # noqa: E402

CLIENT = os.path.join(ROOT, "client", "booboot.py")

# Tests check errors themselves: keep the output clean.
logging.basicConfig(level=logging.CRITICAL)


def start_fake_server(directory, session_timeout=300, duts=None, settings=None, **scripts):
    """Start a server with fake hardware. Return (server, dut, url), dut being the first DUT.

    duts: names of several DUTs, each in its own directory. settings: more settings of some DUTs, like
    {"dut2": {"power": {"backend": "none"}}}. scripts: [scripts] settings.
    """
    def cfg_of(name):
        cfg = config.load(None)
        cfg["server"]["session_timeout"] = str(session_timeout)
        cfg["server"]["name"] = name
        cfg["scripts"].update(enabled="yes", user="")
        cfg["scripts"].update({k: str(v) for k, v in scripts.items()})
        cfg.read_dict((settings or {}).get(name, {}))
        return cfg

    if duts:
        units = build_units([(name, cfg_of(name), None, "") for name in duts], directory)
    else:
        units = [build(cfg_of("dut1"), directory)]
    server = Server(("127.0.0.1", 0), units)
    for u in units:
        u.dut.start()
    threading.Thread(target=server.serve_forever, daemon=True).start()
    deadline = time.monotonic() + 5
    while not all(u.dut.console.connected for u in units) and time.monotonic() < deadline:
        time.sleep(0.02)
    return server, units[0].dut, "http://127.0.0.1:%d" % server.server_address[1]


def stop_server(server, dut=None):
    server.shutdown()
    server.server_close()
    for u in server.units.values():
        u.scripts.close()
        u.dut.stop()

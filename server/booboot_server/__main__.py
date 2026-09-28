"""Run the server: python3 -m booboot_server [-c CONFIG] [--fake [DIR]] [--probe]"""

from __future__ import annotations

import argparse
import configparser
import logging
import os
import signal
import sys
import tempfile
import threading

from . import __version__, config, gpio
from .api import Server
from .console import Console, NoPort, SerialPort, find_serial_ports
from .dut import Dut
from .power import CommandPower, GpioPower, NoPower, SdmuxGpioPower
from .sdmux import UsbSdMux, find_muxes
from .session import Sessions
from .storage import Storage

log = logging.getLogger("booboot")


def make_power(p, mux):
    backend = p.get("backend")
    active_low = p.getboolean("active_low")
    if backend == "gpio":
        return GpioPower(p.get("line"), p.get("chip"), active_low)
    if backend == "sdmux":
        return SdmuxGpioPower(mux, p.getint("sdmux_gpio"), active_low)
    if backend == "command":
        return CommandPower(p.get("on_command"), p.get("off_command"))
    if backend == "none":
        return NoPower()
    raise ValueError("unknown power backend %r" % backend)


def build(cfg, fake_dir=None):
    """Return (dut, sessions, (host, port), prompt) from the configuration."""
    s, p, c = cfg["server"], cfg["power"], cfg["console"]
    name = s.get("name")
    log_dir = config.expand(s.get("log_dir"), name)
    run_dir = config.expand(s.get("run_dir"), name)
    if fake_dir is not None:
        from .fake import FakeBoard
        board = FakeBoard(fake_dir)
        power, mux, storage, port = board.power, board.mux, board.storage, board.port
        log_dir = os.path.join(fake_dir, "log")
    else:
        mux = UsbSdMux(cfg.get("sdmux", "serial"))
        storage = Storage(mux.block_device, os.path.join(run_dir, "mnt"))
        power = make_power(p, mux)
        device = c.get("device")
        port = NoPort() if device == "none" else SerialPort(device, c.getint("baudrate"))
    console = Console(port, c.getint("buffer_size"), log_dir, c.getint("keep_logs"), c.get("line_ending"))
    dut = Dut(name, power, mux, storage, console, p.getfloat("off_time"))
    sessions = Sessions(s.getfloat("session_timeout"), s.getfloat("session_timeout_max"))
    return dut, sessions, (s.get("host"), s.getint("port")), c.get("prompt")


def probe():
    """Print what the board offers, to help writing the configuration."""
    print("GPIO chips:")
    for path in gpio.chips():
        try:
            label, count = gpio.chip_info(path)[1:]
            names = gpio.line_names(path)
        except OSError as e:
            print("  %s: %s" % (path, e))
            continue
        print("  %s  %s, %d lines" % (path, label, count))
        named = ["%d=%s" % (i, n) for i, n in enumerate(names) if n]
        print("    " + (" ".join(named) if named else "(no line names)"))
    print("USB-SD-Mux:")
    for m in find_muxes():
        print("  serial=%(serial)s model=%(model)s control=%(sg)s card=%(block)s" % m)
    print("Serial ports:")
    for port in find_serial_ports():
        print("  " + port)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="booboot-server",
                                 description="Control a device under test: power, SD card and serial console.")
    ap.add_argument("-c", "--config", help="configuration file (INI)")
    ap.add_argument("--fake", nargs="?", const="", metavar="DIR",
                    help="simulate the hardware, with files in DIR (default: a new temporary directory)")
    ap.add_argument("--host", help="listen address")
    ap.add_argument("--port", type=int, help="listen port")
    ap.add_argument("--probe", action="store_true", help="list GPIO lines, USB-SD-Mux and serial ports, then exit")
    ap.add_argument("--version", action="version", version=__version__)
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    if args.probe:
        probe()
        return 0

    try:
        cfg = config.load(args.config)
        if args.host:
            cfg["server"]["host"] = args.host
        if args.port is not None:
            cfg["server"]["port"] = str(args.port)
        fake_dir = None
        if args.fake is not None:
            fake_dir = args.fake or tempfile.mkdtemp(prefix="booboot-fake-")
        dut, sessions, address, prompt = build(cfg, fake_dir)
        server = Server(address, dut, sessions, prompt, cfg["server"].getboolean("web"))
    except (OSError, ValueError, configparser.Error) as e:
        log.error("%s", e)
        return 1

    def stop(signum, frame):
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    dut.start()
    host, port = server.server_address[:2]
    log.info("BooBoot %s: %s on http://%s:%d", __version__, dut.name, host, port)
    if server.web:
        log.info("console web page: http://%s:%d/", host, port)
    if fake_dir is not None:
        log.info("fake hardware in %s", fake_dir)
    try:
        server.serve_forever()
    finally:
        server.server_close()
        dut.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())

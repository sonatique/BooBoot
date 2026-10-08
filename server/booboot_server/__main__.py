"""Run the server: python3 -m booboot_server [-c CONFIG | --config-dir DIR] [--fake [DIR]] [--probe]"""

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
from .board import Label, Unit, claims
from .console import Console, NoPort, SerialPort, find_serial_ports
from .dut import Dut
from .power import CommandPower, GpioPower, NoPower, SdmuxGpioPower
from .scripts import Scripts
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


def build(cfg, fake_dir=None, own_port=None):
    """Return the Unit of a DUT from its configuration. own_port: its own port, if any."""
    s, p, c, sc = cfg["server"], cfg["power"], cfg["console"], cfg["scripts"]
    name = s.get("name")
    log_dir = config.expand(s.get("log_dir"), name)
    run_dir = config.expand(s.get("run_dir"), name)
    state_dir = config.expand(s.get("state_dir"), name)
    scripts_dir = config.expand(sc.get("dir"), name)
    if fake_dir is not None:
        from .fake import FakeBoard
        board = FakeBoard(fake_dir)
        power, mux, storage, port = board.power, board.mux, board.storage, board.port
        log_dir = os.path.join(fake_dir, "log")
        state_dir = os.path.join(fake_dir, "state")
        scripts_dir = os.path.join(fake_dir, "scripts")
    else:
        mux = UsbSdMux(cfg.get("sdmux", "serial"))
        storage = Storage(mux.block_device, os.path.join(run_dir, "mnt"))
        power = make_power(p, mux)
        device = c.get("device")
        port = NoPort() if device == "none" else SerialPort(device, c.getint("baudrate"))
    console = Console(port, c.getint("buffer_size"), log_dir, c.getint("keep_logs"), c.get("line_ending"), name)
    dut = Dut(name, power, mux, storage, console, p.getfloat("off_time"))
    sessions = Sessions(s.getfloat("session_timeout"), s.getfloat("session_timeout_max"))
    scripts = Scripts(scripts_dir, sessions, sc.getboolean("enabled"), sc.get("user"), sc.getfloat("max_time"),
                      sc.getint("memory") << 20, sc.getint("output") << 20, sc.getint("keep"),
                      sc.getint("file_size") << 20, name)
    label = Label(os.path.join(state_dir, "label"), s.get("label").strip())
    return Unit(name, dut, sessions, scripts, c.get("prompt"), own_port, label=label)


def build_units(duts, fake_dir=None):
    """Return the units of the DUTs, given as (name, cfg, port, error).

    A DUT that cannot start, or that would use hardware of an earlier DUT, gets a unit with its error.
    With fake_dir, each DUT simulates its hardware, in its own directory when there are several.
    """
    units, names, used = [], set(), {}
    for name, cfg, port, error in duts:
        if not error and name in names:
            error = "another DUT has the same name"
        if not error and fake_dir is None:
            for claim in sorted(claims(cfg)):
                if claim in used:
                    error = "uses the same %s as %s: each DUT needs its own" % (claim, used[claim])
                    break
        if not error:
            d = None if fake_dir is None else fake_dir if len(duts) == 1 else os.path.join(fake_dir, name)
            try:
                units.append(build(cfg, d, port))
                for claim in claims(cfg):
                    used.setdefault(claim, name)
                names.add(name)
                continue
            except (OSError, ValueError, configparser.Error) as e:
                error = str(e)
        log.error("%s: %s", name, error)
        units.append(Unit(name, port=port, error=error))
        names.add(name)
    return units


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
                                 description="Control devices under test: power, SD card and serial console.")
    ap.add_argument("-c", "--config", help="configuration file (INI) of a single DUT")
    ap.add_argument("--config-dir", metavar="DIR",
                    help="configuration directory: server.ini for the board, and one NAME.ini per DUT")
    ap.add_argument("--fake", nargs="?", const="", metavar="DIR",
                    help="simulate the hardware, with files in DIR (default: a new temporary directory)")
    ap.add_argument("--duts", metavar="NAMES", help="with --fake: several simulated DUTs, like dut1,dut2")
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
        if args.duts and args.fake is None:
            raise ValueError("--duts needs --fake")
        fake_dir = None
        if args.fake is not None:
            fake_dir = args.fake or tempfile.mkdtemp(prefix="booboot-fake-")
        if args.config_dir:
            board, duts = config.load_dir(args.config_dir)
            if not duts:
                raise ValueError("no DUT configuration (NAME.ini) in %s" % args.config_dir)
            units = build_units(duts, fake_dir)
            if all(u.error for u in units):
                raise ValueError("no DUT could start")
        elif args.duts:
            board = config.load(args.config)
            duts = []
            for name in args.duts.split(","):
                cfg = config.load(args.config)
                cfg["server"]["name"] = name
                duts.append((name, cfg, None, ""))
            units = build_units(duts, fake_dir)
        else:
            board = config.load(args.config)
            units = [build(board, fake_dir)]
        s = board["server"]
        host = args.host or s.get("host")
        port = s.getint("port") if args.port is None else args.port
        servers = [Server((host, port), units, web=s.getboolean("web"))]
        # A DUT that names its own port also answers there, at the root, as when each DUT had its server.
        for u in units:
            if u.port and u.port not in [x.server_address[1] for x in servers]:
                try:
                    servers.append(Server((host, u.port), units, default=u, web=s.getboolean("web"), board=False))
                except OSError as e:
                    log.error("%s: port %d: %s", u.name, u.port, e)
    except (OSError, ValueError, configparser.Error) as e:
        log.error("%s", e)
        return 1
    running = [u for u in units if not u.error]

    def stop(signum, frame):
        for server in servers:
            threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    for u in running:
        u.dut.start()
    host, port = servers[0].server_address[:2]
    log.info("BooBoot %s on http://%s:%d: %s", __version__, host, port, ", ".join(u.name for u in units))
    for server in servers[1:]:
        log.info("%s also on port %d", server.default.name, server.server_address[1])
    if servers[0].web:
        log.info("console web page: http://%s:%d/", host, port)
    if fake_dir is not None:
        log.info("fake hardware in %s", fake_dir)
    for u in running:
        if u.scripts.enabled:
            log.info("%s: scripts are on, in %s", u.name, u.scripts.dir)
    for server in servers[1:]:
        threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        servers[0].serve_forever()
    finally:
        for server in servers:
            server.server_close()
        for u in running:
            u.scripts.close()
            u.dut.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())

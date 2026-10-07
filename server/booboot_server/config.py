"""Configuration file (INI format) with defaults."""

from __future__ import annotations

import configparser
import glob
import os

# Settings of the board, in a configuration directory. The other .ini files are the DUTs.
SERVER_FILE = "server.ini"

DEFAULTS = {
    "server": {
        "name": "dut1",
        "host": "0.0.0.0",
        "port": "8080",
        "log_dir": "/var/log/booboot/{name}",
        "run_dir": "/run/booboot/{name}",
        "session_timeout": "300",
        "session_timeout_max": "3600",
        "web": "yes",
    },
    "power": {
        "backend": "gpio",
        "line": "GPIO17",
        "chip": "",
        "active_low": "no",
        "sdmux_gpio": "0",
        "on_command": "",
        "off_command": "",
        "off_time": "2",
    },
    "sdmux": {
        "serial": "",
    },
    "console": {
        "device": "auto",
        "baudrate": "921600",
        "line_ending": "cr",
        "prompt": "[#$>] $",
        "buffer_size": "8388608",
        "keep_logs": "100",
    },
    "scripts": {
        "enabled": "no",
        "user": "booboot-script",
        "dir": "/var/lib/booboot/{name}/scripts",
        "max_time": "86400",
        "memory": "512",
        "output": "10",
        "keep": "20",
        "file_size": "100",
    },
}


def load(*paths):
    """Return a ConfigParser with defaults, updated from the files given, in order. None is skipped."""
    cp = configparser.ConfigParser(interpolation=None)
    cp.read_dict(DEFAULTS)
    for path in paths:
        if path:
            with open(path) as f:
                cp.read_file(f)
    return cp


def load_dir(directory):
    """Return (board, duts) from a configuration directory: server.ini, and one NAME.ini per DUT.

    board: the defaults, then server.ini. duts: (name, cfg, port, error) for each DUT file, in file
    name order. cfg: the defaults, server.ini, then the file of the DUT, which names the DUT (default:
    its file name). port: the port that the file of the DUT names, or None. error: why the file cannot
    be read, or "".
    """
    server = os.path.join(directory, SERVER_FILE)
    server = server if os.path.exists(server) else None
    board = load(server)
    duts = []
    for path in sorted(glob.glob(os.path.join(directory, "*.ini"))):
        if os.path.basename(path) == SERVER_FILE:
            continue
        name = os.path.splitext(os.path.basename(path))[0]
        try:
            own = configparser.ConfigParser(interpolation=None)
            with open(path) as f:
                own.read_file(f)
            cfg = load(server, path)
            if own.has_option("server", "name"):
                name = own.get("server", "name")
            cfg["server"]["name"] = name
            port = own.getint("server", "port") if own.has_option("server", "port") else None
        except (OSError, ValueError, configparser.Error) as e:
            duts.append((name, None, None, "%s: %s" % (path, e)))
            continue
        duts.append((name, cfg, port, ""))
    return board, duts


def expand(value, name):
    return value.replace("{name}", name)

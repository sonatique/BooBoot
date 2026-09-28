"""Configuration file (INI format) with defaults."""

from __future__ import annotations

import configparser

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
}


def load(path=None):
    """Return a ConfigParser with defaults, updated from the file if given."""
    cp = configparser.ConfigParser(interpolation=None)
    cp.read_dict(DEFAULTS)
    if path:
        with open(path) as f:
            cp.read_file(f)
    return cp


def expand(value, name):
    return value.replace("{name}", name)

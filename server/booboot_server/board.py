"""The DUTs of a board, served by one server."""

from __future__ import annotations

import logging
import os

from .errors import ApiError, BadRequest

# Longest label, in characters.
MAX_LABEL = 100


class _Named(logging.LoggerAdapter):
    def process(self, msg, kwargs):
        return "%s: %s" % (self.extra["name"], msg), kwargs


def named_log(logger, name):
    """logger, with the name of the DUT at the start of each message, if any."""
    return _Named(logger, {"name": name}) if name else logger


class Unit:
    """One DUT with its session, its scripts and its prompt.

    A DUT that cannot start has an error instead: the board lists it, and its calls answer that error.
    port: its own port besides /duts/NAME on the port of the board, or None.
    """

    def __init__(self, name, dut=None, sessions=None, scripts=None, prompt="", port=None, error="", label=None):
        self.name = name
        self.dut = dut
        self.sessions = sessions
        self.scripts = scripts
        self.prompt = prompt
        self.port = port
        self.error = error
        self.label = label


class Label:
    """Free text shown with the name of a DUT, like "ZCU102 rev B, bench 3". The name stays its identifier.

    Kept in a file, so that it survives restarts. Until it is set, the label is the one of the configuration.
    """

    def __init__(self, path, default=""):
        self.path = path
        try:
            with open(path, encoding="utf-8") as f:
                self.text = f.read().strip()
        except (OSError, UnicodeDecodeError):
            self.text = default

    def set(self, text):
        text = text.strip()
        if len(text) > MAX_LABEL:
            raise BadRequest("the label has more than %d characters" % MAX_LABEL)
        if any(ord(c) < 32 or ord(c) == 127 for c in text):
            raise BadRequest("the label must be one line of text")
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(text + "\n")
            os.replace(tmp, self.path)
        except OSError as e:
            raise ApiError("cannot keep the label: %s" % e) from e
        self.text = text


def claims(cfg):
    """The hardware that the configuration of a DUT uses, which two DUTs cannot share."""
    serial = cfg.get("sdmux", "serial")
    found = set()
    if serial != "none":
        found.add("USB-SD-Mux " + (serial or "(the only one: set serial in [sdmux])"))
    power, console = cfg["power"], cfg["console"]
    if power.get("backend") == "gpio":
        chip = power.get("chip")
        found.add("relay line " + power.get("line") + (" of " + chip if chip else ""))
    device = console.get("device")
    if device != "none":
        found.add("serial adapter " + (device if device != "auto" else "(the only one: set device in [console])"))
    return found

"""The DUTs of a board, served by one server."""

from __future__ import annotations

import logging


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

    def __init__(self, name, dut=None, sessions=None, scripts=None, prompt="", port=None, error=""):
        self.name = name
        self.dut = dut
        self.sessions = sessions
        self.scripts = scripts
        self.prompt = prompt
        self.port = port
        self.error = error


def claims(cfg):
    """The hardware that the configuration of a DUT uses, which two DUTs cannot share."""
    serial = cfg.get("sdmux", "serial")
    found = {"USB-SD-Mux " + (serial or "(the only one: set serial in [sdmux])")}
    power, console = cfg["power"], cfg["console"]
    if power.get("backend") == "gpio":
        chip = power.get("chip")
        found.add("relay line " + power.get("line") + (" of " + chip if chip else ""))
    device = console.get("device")
    if device != "none":
        found.add("serial adapter " + (device if device != "auto" else "(the only one: set device in [console])"))
    return found

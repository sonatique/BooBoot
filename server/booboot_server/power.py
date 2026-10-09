"""Power switch backends. Each has set(on) and close()."""

from __future__ import annotations

import subprocess

from . import gpio
from .errors import HardwareError, Unavailable


class GpioPower:
    """Relay driven by a GPIO line of the board."""

    name = "gpio"

    def __init__(self, line, chip="", active_low=False):
        self._spec = (line, chip)
        self._active_low = active_low
        self._line = None

    def set(self, on):
        try:
            if self._line is None:
                chip, offset = gpio.find_line(*self._spec)
                self._line = gpio.OutputLine(chip, offset, self._active_low, on)
            else:
                self._line.set(on)
        except (gpio.GpioError, OSError) as e:
            raise HardwareError("GPIO: %s" % e) from e

    def close(self):
        if self._line:
            self._line.close()
            self._line = None


class SdmuxGpioPower:
    """Relay driven by GPIO 0 or 1 of a USB-SD-Mux FAST (open drain)."""

    name = "sdmux"

    def __init__(self, mux, index=0, active_low=False):
        self._mux = mux
        self._index = index
        self._active_low = active_low

    def set(self, on):
        self._mux.set_gpio(self._index, on != self._active_low)

    def close(self):
        pass


class CommandPower:
    """Power switched by shell commands."""

    name = "command"

    def __init__(self, on_command, off_command):
        self._commands = {True: on_command, False: off_command}

    def set(self, on):
        cmd = self._commands[bool(on)]
        if not cmd:
            raise Unavailable("no %s_command set in [power]" % ("on" if on else "off"))
        try:
            p = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=30)
        except subprocess.TimeoutExpired as e:
            raise HardwareError("power command timed out: %s" % cmd) from e
        if p.returncode != 0:
            raise HardwareError("power command failed (%d): %s" % (p.returncode, p.stderr.strip()))

    def close(self):
        pass


class NoPower:
    """No relay: only keeps track of the requested state, like to mark a switch made by hand."""

    name = "none"
    present = False

    def __init__(self, on_change=None):
        self._on_change = on_change

    def set(self, on):
        if self._on_change:
            self._on_change(on)

    def close(self):
        pass

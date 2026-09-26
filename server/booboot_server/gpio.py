"""Linux GPIO character device (uAPI v2) with plain ioctl calls.

Works on any board with /dev/gpiochipN and Linux 5.10 or later.
"""

from __future__ import annotations

import fcntl
import glob
import os
import re
import struct

# ioctl numbers from <linux/gpio.h>
GET_CHIPINFO = 0x8044B401  # struct gpiochip_info
GET_LINEINFO = 0xC100B405  # struct gpio_v2_line_info
GET_LINE = 0xC250B407  # struct gpio_v2_line_request
SET_VALUES = 0xC010B40F  # struct gpio_v2_line_values

CHIPINFO_SIZE = 68
LINEINFO_SIZE = 256
REQUEST_SIZE = 592

FLAG_ACTIVE_LOW = 0x2
FLAG_OUTPUT = 0x8
ATTR_OUTPUT_VALUES = 2

# Offsets in struct gpio_v2_line_request
REQ_CONSUMER = 256
REQ_CONFIG = 288
REQ_ATTR0 = REQ_CONFIG + 32
REQ_NUM_LINES = 560
REQ_FD = 588


class GpioError(Exception):
    pass


def chips():
    """Return GPIO chip device paths, sorted by number."""
    paths = glob.glob("/dev/gpiochip*")
    return sorted(paths, key=lambda p: int(re.sub(r"\D", "", p) or 0))


def _cstr(buf):
    return bytes(buf).split(b"\0", 1)[0].decode("ascii", "replace")


def chip_info(path):
    """Return (name, label, number of lines) of a chip."""
    fd = os.open(path, os.O_RDWR | os.O_CLOEXEC)
    try:
        buf = bytearray(CHIPINFO_SIZE)
        fcntl.ioctl(fd, GET_CHIPINFO, buf)
    finally:
        os.close(fd)
    return _cstr(buf[0:32]), _cstr(buf[32:64]), struct.unpack_from("=I", buf, 64)[0]


def line_names(path):
    """Return the line names of a chip, indexed by offset."""
    count = chip_info(path)[2]
    fd = os.open(path, os.O_RDWR | os.O_CLOEXEC)
    names = []
    try:
        for offset in range(count):
            buf = bytearray(LINEINFO_SIZE)
            struct.pack_into("=I", buf, 64, offset)
            fcntl.ioctl(fd, GET_LINEINFO, buf)
            names.append(_cstr(buf[0:32]))
    finally:
        os.close(fd)
    return names


def find_line(line, chip=""):
    """Return (chip path, offset) of a line given by name or by number."""
    if chip and not chip.startswith("/"):
        chip = "/dev/" + chip
    if line.isdigit():
        return chip or "/dev/gpiochip0", int(line)
    for path in [chip] if chip else chips():
        try:
            names = line_names(path)
        except OSError:
            continue
        if line in names:
            return path, names.index(line)
    raise GpioError("GPIO line %r not found (run with --probe to list lines)" % line)


def build_request(offset, active_low, value):
    """Return a struct gpio_v2_line_request for one output line."""
    req = bytearray(REQUEST_SIZE)
    struct.pack_into("=I", req, 0, offset)
    req[REQ_CONSUMER:REQ_CONSUMER + 7] = b"booboot"
    flags = FLAG_OUTPUT | (FLAG_ACTIVE_LOW if active_low else 0)
    struct.pack_into("=QI", req, REQ_CONFIG, flags, 1)
    # attribute 0: output value of line 0
    struct.pack_into("=IIQQ", req, REQ_ATTR0, ATTR_OUTPUT_VALUES, 0, int(bool(value)), 1)
    struct.pack_into("=I", req, REQ_NUM_LINES, 1)
    return req


class OutputLine:
    """One GPIO line used as output. The line is held until close()."""

    def __init__(self, chip, offset, active_low=False, value=False):
        req = build_request(offset, active_low, value)
        try:
            fd = os.open(chip, os.O_RDWR | os.O_CLOEXEC)
        except OSError as e:
            raise GpioError("cannot open %s: %s" % (chip, e.strerror)) from e
        try:
            fcntl.ioctl(fd, GET_LINE, req)
        except OSError as e:
            raise GpioError("cannot get %s line %d: %s" % (chip, offset, e.strerror)) from e
        finally:
            os.close(fd)
        self._fd = struct.unpack_from("=i", req, REQ_FD)[0]

    def set(self, value):
        buf = bytearray(struct.pack("=QQ", int(bool(value)), 1))
        fcntl.ioctl(self._fd, SET_VALUES, buf)

    def close(self):
        if self._fd >= 0:
            os.close(self._fd)
            self._fd = -1

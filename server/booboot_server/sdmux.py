"""USB-SD-Mux control with the usbsdmux package from Linux Automation GmbH."""

from __future__ import annotations

import glob
import os
import threading

from .errors import HardwareError, Unavailable

# SCSI model strings of the mux card readers
MODELS = {"sdmux HS-SD/MMC": "classic", "sdFST HS-SD/MMC": "fast"}
MODES = ("host", "dut", "off")
SYS_SG = "/sys/class/scsi_generic"


def _read(path):
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return ""


def find_muxes():
    """Return the USB-SD-Mux devices seen by the kernel."""
    found = []
    for sg in sorted(glob.glob(os.path.join(SYS_SG, "sg*"))):
        model = MODELS.get(_read(os.path.join(sg, "device", "model")))
        if not model:
            continue
        # The USB serial number is on the USB device, a parent of the SCSI device.
        serial = ""
        path = os.path.realpath(os.path.join(sg, "device"))
        while path != "/":
            if os.path.exists(os.path.join(path, "idVendor")):
                serial = _read(os.path.join(path, "serial"))
                break
            path = os.path.dirname(path)
        block_dir = os.path.join(sg, "device", "block")
        blocks = sorted(os.listdir(block_dir)) if os.path.isdir(block_dir) else []
        found.append({
            "sg": "/dev/" + os.path.basename(sg),
            "serial": serial,
            "model": model,
            "block": "/dev/" + blocks[0] if blocks else "",
        })
    return found


class UsbSdMux:
    """One USB-SD-Mux, found again on each call so that replugging works."""

    def __init__(self, serial=""):
        self.serial = serial
        self._lock = threading.Lock()

    def _find(self):
        muxes = find_muxes()
        if self.serial:
            muxes = [m for m in muxes if m["serial"] == self.serial]
        if not muxes:
            what = " with serial " + self.serial if self.serial else ""
            raise Unavailable("no USB-SD-Mux found%s" % what)
        if len(muxes) > 1:
            serials = ", ".join(m["serial"] for m in muxes)
            raise Unavailable("several USB-SD-Mux found, set serial in [sdmux]: " + serials)
        return muxes[0]

    def _driver(self):
        try:
            from usbsdmux.usbsdmux import autoselect_driver
        except ImportError as e:
            raise Unavailable("python package usbsdmux is missing (pip install usbsdmux)") from e
        sg = self._find()["sg"]
        try:
            return autoselect_driver(sg)
        except Exception as e:
            raise HardwareError("USB-SD-Mux %s: %s" % (sg, e)) from e

    def get_mode(self):
        with self._lock:
            drv = self._driver()
            try:
                return drv.get_mode()
            except Exception as e:
                raise HardwareError("USB-SD-Mux: %s" % e) from e

    def set_mode(self, mode):
        with self._lock:
            drv = self._driver()
            try:
                if mode == "host":
                    drv.mode_host()
                elif mode == "dut":
                    drv.mode_DUT()
                else:
                    drv.mode_disconnect()
            except Exception as e:
                raise HardwareError("USB-SD-Mux: %s" % e) from e

    def set_gpio(self, index, high):
        with self._lock:
            drv = self._driver()
            try:
                if high:
                    drv.gpio_set_high(index)
                else:
                    drv.gpio_set_low(index)
            except NotImplementedError as e:
                raise Unavailable("this USB-SD-Mux has no GPIO (FAST model needed)") from e
            except Exception as e:
                raise HardwareError("USB-SD-Mux GPIO: %s" % e) from e

    def block_device(self):
        return self._find()["block"]

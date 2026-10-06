"""The device under test: power, SD card and console, with safety rules.

Rules:
- The card can be on the host side only while the power is off.
- Power on moves the card from the host side to the DUT.
- One hardware operation at a time.
"""

from __future__ import annotations

import contextlib
import logging
import threading
import time

from . import __version__
from . import storage as st
from .errors import ApiError, BadRequest, Conflict, HardwareError, Unavailable
from .sdmux import MODES

log = logging.getLogger(__name__)


class Dut:
    def __init__(self, name, power, mux, storage, console, off_time=2.0):
        self.name = name
        self.power = power
        self.mux = mux
        self.storage = storage
        self.console = console
        self.off_time = off_time
        self.power_state = "unknown"
        self.power_error = ""
        self.mux_mode = "unknown"
        self.mux_error = ""
        self.card = {"state": "unknown"}
        self.operation = None
        self._lock = threading.Lock()

    @contextlib.contextmanager
    def _op(self, name):
        if not self._lock.acquire(timeout=2):
            op = self.operation or {}
            raise Conflict("busy with: %s" % op.get("name"), code="operation_in_progress", operation=op)
        self.operation = {"name": name, "started": round(time.time(), 1)}
        try:
            yield self.operation
        finally:
            self.operation = None
            self._lock.release()

    def start(self):
        self.console.start()
        for action in (lambda: self._set_power(False), lambda: self._set_mux("dut")):
            try:
                action()
            except ApiError as e:
                log.error("%s", e.message)

    def stop(self):
        try:
            self._set_power(False)
        except ApiError as e:
            log.error("%s", e.message)
        self.power.close()
        self.console.stop()

    def _set_power(self, on):
        try:
            self.power.set(on)
        except Exception as e:
            self.power_state = "unknown"
            self.power_error = e.message if isinstance(e, ApiError) else str(e)
            if isinstance(e, ApiError):
                raise
            raise HardwareError("power: %s" % e) from e
        if not on and self.power_state != "off":
            self.console.mark_off()
        self.power_state = "on" if on else "off"
        self.power_error = ""
        log.info("power %s", self.power_state)

    def _set_mux(self, mode):
        try:
            self.mux.set_mode(mode)
        except Exception as e:
            self.mux_mode = "unknown"
            self.mux_error = e.message if isinstance(e, ApiError) else str(e)
            if isinstance(e, ApiError):
                raise
            raise HardwareError("sdmux: %s" % e) from e
        self.mux_mode = mode
        self.mux_error = ""
        log.info("sd card to %s", mode)

    def _refresh_mux(self):
        try:
            self.mux_mode = self.mux.get_mode()
            self.mux_error = ""
        except ApiError as e:
            self.mux_mode = "unknown"
            self.mux_error = e.message

    def status(self):
        if not self._lock.locked():
            self._refresh_mux()
        c = self.console
        return {
            "name": self.name,
            "version": __version__,
            "power": {"state": self.power_state, "backend": self.power.name, "error": self.power_error},
            "sd": {"mode": self.mux_mode, "error": self.mux_error, "card": self.card},
            "console": {
                "connected": c.connected,
                "device": c.port.path or c.port.device,
                "baudrate": c.port.baudrate,
                "cursor": c.end,
                "boot": c.boot,
                "last": c.last,
                "written": c.written,
                "error": c.error,
            },
            "operation": self.operation,
        }

    # Power

    def set_power(self, on):
        with self._op("power on" if on else "power off"):
            return self._power(on)

    def power_cycle(self, off_time=None):
        with self._op("power cycle"):
            self._set_power(False)
            time.sleep(self.off_time if off_time is None else off_time)
            return self._power(True)

    def _power(self, on):
        if not on:
            self._set_power(False)
            return {"power": "off"}
        if self.mux_mode in ("host", "unknown"):
            try:
                self._set_mux("dut")
            except Unavailable as e:
                log.warning("power on without SD card switch: %s", e.message)
        # Console times count from the moment the relay is switched on.
        boot = self.console.mark_boot(lambda: self._set_power(True))
        return {"power": "on", "boot": boot}

    # SD card

    def _require_off(self):
        if self.power_state != "off":
            raise Conflict("power is %s: power off the DUT first" % self.power_state, code="power_on")

    def _host(self):
        """Make sure the card is on the host side and ready."""
        self._require_off()
        if self.mux_mode != "host":
            self._set_mux("host")
            self.storage.wait_ready()

    def set_sd(self, mode):
        if mode not in MODES:
            raise BadRequest("mode must be one of: " + ", ".join(MODES))
        with self._op("sd " + mode):
            if mode == "host":
                self._require_off()
                self._set_mux("host")
                self.storage.wait_ready()
            else:
                self._set_mux(mode)
            return {"mode": mode}

    def write_image(self, body, compression="auto", verify=False):
        with self._op("write image") as op:
            self._host()
            self.card = {"state": "writing"}
            op["bytes"] = 0

            def progress(n):
                op["bytes"] = n

            try:
                result = self.storage.write_image(body, compression, verify, progress)
            except Exception:
                self.card = {"state": "incomplete"}
                raise
            self.card = {"state": "written", "sha256": result["sha256"], "bytes": result["bytes"],
                         "time": round(time.time(), 1)}
            return result

    def card_info(self):
        with self._op("sd info"):
            self._host()
            return self.storage.card_info()

    @contextlib.contextmanager
    def sd_open(self, part, path):
        """Yield ("dir", entries) or ("file", open file) for a card path."""
        with self._op("sd read"):
            self._host()
            with self.storage.mounted(part) as root:
                kind, value = st.open_path(root, path)
                if kind == "dir":
                    yield kind, value
                else:
                    with value:
                        yield kind, value

    def sd_put(self, part, path, body, is_dir=False):
        with self._op("sd write"):
            self._host()
            self._modified()
            with self.storage.mounted(part, write=True) as root:
                if is_dir:
                    st.make_dir(root, path)
                    return {"path": path, "type": "dir"}
                return {"path": path, "size": st.put_file(root, path, body)}

    def sd_delete(self, part, path, recursive=False):
        with self._op("sd delete"):
            self._host()
            self._modified()
            with self.storage.mounted(part, write=True) as root:
                st.delete(root, path, recursive)
                return {"deleted": path}

    def _modified(self):
        if self.card.get("state") == "written":
            self.card = dict(self.card, state="modified")

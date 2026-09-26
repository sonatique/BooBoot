"""Simulated hardware, to run the server without a board."""

from __future__ import annotations

import contextlib
import os
import threading
import time

from .console import SerialPort
from .errors import NotFound
from .power import NoPower
from .storage import Storage

BOOT_LOG = [
    "\r\nFake FSBL 2024.1\r\n",
    "\r\nU-Boot 2024.01 (fake)\r\n",
    "Hit any key to stop autoboot:  0 \r\n",
    "Starting kernel ...\r\n",
    "[    0.000000] Booting Linux on physical CPU 0x0\r\n",
    "[    0.000000] Linux version 6.6.0-fake\r\n",
    "[    1.234567] \x1b[0;32mOK\x1b[0m Reached target Multi-User System.\r\n",
]


class FakePower(NoPower):
    name = "fake"


class FakeMux:
    def __init__(self):
        self.mode = "dut"

    def get_mode(self):
        return self.mode

    def set_mode(self, mode):
        self.mode = mode

    def set_gpio(self, index, high):
        pass

    def block_device(self):
        return ""


class FakeStorage(Storage):
    """Card made of an image file and one directory per partition."""

    PARTS = ((1, "vfat", "BOOT"), (2, "ext4", "rootfs"))

    def __init__(self, directory):
        super().__init__(None, "")
        self.dir = directory
        for number, _, _ in self.PARTS:
            os.makedirs(os.path.join(directory, "p%d" % number), exist_ok=True)

    def device(self):
        return os.path.join(self.dir, "card.img")

    def wait_ready(self, timeout=20):
        pass

    def card_info(self):
        parts = [{"number": n, "device": os.path.join(self.dir, "p%d" % n), "start": 0, "size": 0,
                  "type": t, "label": label} for n, t, label in self.PARTS]
        return {"device": self.device(), "size": 0, "partitions": parts}

    @contextlib.contextmanager
    def mounted(self, number, write=False):
        path = os.path.join(self.dir, "p%d" % number)
        if not os.path.isdir(path):
            raise NotFound("no partition %d on the SD card" % number)
        yield path


class FakeBoard:
    """A board on a pseudo terminal.

    It boots to a login prompt when powered with BOOT.BIN on partition 1.
    Login is "root". The shell knows "uname -a", "echo" and "exit".
    """

    def __init__(self, directory):
        os.makedirs(directory, exist_ok=True)
        self.mux = FakeMux()
        self.storage = FakeStorage(os.path.join(directory, "card"))
        self.power = FakePower(self._power)
        self._master, self._slave = os.openpty()
        # A pseudo terminal ignores the speed. 115200 exists on all systems.
        self.port = SerialPort(os.ttyname(self._slave), 115200)
        self._lock = threading.Lock()
        self._gen = 0
        self._state = "off"
        self._line = b""
        self._cr = False
        threading.Thread(target=self._input, name="fake-board", daemon=True).start()

    def _power(self, on):
        with self._lock:
            self._gen += 1
            self._state = "off"
            self._line = b""
            gen = self._gen
        if on:
            threading.Thread(target=self._boot, args=(gen,), daemon=True).start()

    def _out(self, text):
        os.write(self._master, text.encode("utf-8"))

    def _boot(self, gen):
        time.sleep(0.05)
        boot_bin = os.path.join(self.storage.dir, "p1", "BOOT.BIN")
        ok = self.mux.mode == "dut" and os.path.isfile(boot_bin)
        lines = BOOT_LOG if ok else [BOOT_LOG[0], "no boot image on the SD card\r\n"]
        for line in lines:
            with self._lock:
                if gen != self._gen:
                    return
                self._out(line)
            time.sleep(0.02)
        with self._lock:
            if ok and gen == self._gen:
                self._state = "login"
                self._out("\r\nfake login: ")

    def _input(self):
        while True:
            try:
                data = os.read(self._master, 1024)
            except OSError:
                return
            if not data:
                return
            with self._lock:
                for b in data:
                    self._key(bytes([b]))

    def _prompt(self):
        return "root@fake:~# " if self._state == "shell" else "fake login: "

    def _key(self, ch):
        if self._state not in ("login", "shell"):
            return
        if ch == b"\n" and self._cr:
            self._cr = False
            return
        self._cr = ch == b"\r"
        if ch in (b"\r", b"\n"):
            self._out("\r\n")
            line = self._line.decode("utf-8", "replace").strip()
            self._line = b""
            self._command(line)
        elif ch == b"\x03":
            self._line = b""
            self._out("^C\r\n" + self._prompt())
        else:
            self._line += ch
            os.write(self._master, ch)

    def _command(self, cmd):
        if self._state == "login":
            if cmd == "root":
                self._state = "shell"
            elif cmd:
                self._out("Login incorrect\r\n")
        elif cmd == "uname -a":
            self._out("Linux fake 6.6.0-fake #1 SMP armv7l GNU/Linux\r\n")
        elif cmd.startswith("echo "):
            self._out(cmd[5:] + "\r\n")
        elif cmd == "exit":
            self._state = "login"
        elif cmd:
            self._out("-sh: %s: not found\r\n" % cmd.split()[0])
        self._out(self._prompt())

"""What the board itself does: the messages of the server, and its load."""

from __future__ import annotations

import logging
import threading
import time

from .console import Console, NoPort

LOG_FORMAT = "%(asctime)s.%(msecs)03d %(levelname)s %(name)s: %(message)s"
LOG_SIZE = 1 << 20  # bytes of messages kept


class BoardLog(logging.Handler):
    """The messages of the server, kept like the output of a console, to read and to stream."""

    def __init__(self, size=LOG_SIZE):
        super().__init__(logging.INFO)
        self.setFormatter(logging.Formatter(LOG_FORMAT, "%Y-%m-%d %H:%M:%S"))
        self.console = Console(NoPort(), size)

    def emit(self, record):
        try:
            self.console.add((self.format(record) + "\n").encode("utf-8"))
        except Exception:
            self.handleError(record)


def _cpu():
    """(busy, total) CPU time since the board started, or None."""
    try:
        with open("/proc/stat") as f:
            values = [int(v) for v in f.readline().split()[1:]]
    except (OSError, ValueError):
        return None
    idle = values[3] + (values[4] if len(values) > 4 else 0)
    return sum(values) - idle, sum(values)


def _traffic():
    """(received, sent) bytes on the network interfaces since the board started, or None."""
    try:
        with open("/proc/net/dev") as f:
            lines = f.readlines()[2:]
    except OSError:
        return None
    received = sent = 0
    for line in lines:
        name, _, data = line.partition(":")
        fields = data.split()
        if name.strip() == "lo" or len(fields) < 9:
            continue
        try:
            received += int(fields[0])
            sent += int(fields[8])
        except ValueError:
            return None
    return received, sent


def _temperature():
    try:
        with open("/sys/class/thermal/thermal_zone0/temp") as f:
            return round(int(f.read()) / 1000, 1)
    except (OSError, ValueError):
        return None


class Activity:
    """The load of the board, measured every interval seconds: CPU use, network traffic, temperature."""

    def __init__(self, interval=1.0):
        self.interval = interval
        self._now = {"cpu": None, "traffic": {"in": None, "out": None}, "temperature": _temperature()}
        self._stop = threading.Event()
        self._thread = None

    def start(self):
        self._thread = threading.Thread(target=self._run, name="activity", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()

    def get(self):
        return dict(self._now, interval=self.interval)

    def _run(self):
        last = (time.monotonic(), _cpu(), _traffic())
        while not self._stop.wait(self.interval):
            now = (time.monotonic(), _cpu(), _traffic())
            seconds = now[0] - last[0]
            cpu = traffic = None
            if now[1] and last[1] and now[1][1] > last[1][1]:
                cpu = round(100.0 * (now[1][0] - last[1][0]) / (now[1][1] - last[1][1]), 1)
            if now[2] and last[2] and seconds > 0:
                traffic = {"in": round((now[2][0] - last[2][0]) / seconds),
                           "out": round((now[2][1] - last[2][1]) / seconds)}
            self._now = {"cpu": cpu, "traffic": traffic or {"in": None, "out": None}, "temperature": _temperature()}
            last = now

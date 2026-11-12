"""What the board itself does: the messages of the server, and its load."""

from __future__ import annotations

import glob
import logging
import os
import shutil
import threading
import time

from .console import Console, NoPort

log = logging.getLogger(__name__)

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


def _disk():
    """Bytes total and free on the file system of the board, or None."""
    try:
        usage = shutil.disk_usage("/")
    except OSError:
        return None
    return {"total": usage.total, "free": usage.free}


# The conditions of the throttled state of the firmware of a Raspberry Pi, and their bits. The same bits 16 higher
# tell that the condition occurred since they were last cleared, which the kernel may do every few seconds.
PI_FLAGS = (("under-voltage", 0), ("frequency capped", 1), ("throttled", 2), ("soft temperature limit", 3))
PI_HOLD = 10  # seconds a condition stays current after it was last seen, so that one that comes and goes logs little


def _pi_sources():
    """The throttled state file of a Raspberry Pi, and the under-voltage alarm of its kernel, None if not found."""
    state = next(iter(glob.glob("/sys/devices/platform/*/*:firmware/get_throttled")), None)
    alarm = None
    for name in glob.glob("/sys/class/hwmon/hwmon*/name"):
        try:
            with open(name) as f:
                if f.read().strip() == "rpi_volt":
                    alarm = os.path.join(os.path.dirname(name), "in0_lcrit_alarm")
        except OSError:
            pass
    return state, alarm


def _pi_state(state, alarm):
    """(conditions now, conditions that occurred) of a Raspberry Pi, or None."""
    try:
        if state:
            with open(state) as f:
                value = int(f.read().strip(), 16)
            return ({name for name, bit in PI_FLAGS if value & 1 << bit},
                    {name for name, bit in PI_FLAGS if value & 1 << (bit + 16)})
        if alarm:
            with open(alarm) as f:
                return ({"under-voltage"} if f.read().strip() == "1" else set()), set()
    except (OSError, ValueError):
        pass
    return None


def _ordered(names):
    """The conditions of a Raspberry Pi in names, in the order of their bits."""
    return [name for name, _ in PI_FLAGS if name in names]


class Activity:
    """
    The load of the board, measured every interval seconds: CPU use, network traffic, temperature, disk. On a
    Raspberry Pi, also the conditions of its power and temperature, like under-voltage, logged when they come and go.
    pi_state: the throttled state file to read, by default the one of the board, if any.
    """

    def __init__(self, interval=1.0, pi_state=None):
        self.interval = interval
        self._pi = (pi_state, None) if pi_state else _pi_sources()
        # Conditions of the Raspberry Pi: monotonic time last seen, Unix time last seen (None: before the start),
        # those current, and those that occurred, at the last reading.
        self._pi_last = {}
        self._pi_seen = {}
        self._pi_current = set()
        self._pi_occurred = None
        self._now = {"cpu": None, "traffic": {"in": None, "out": None}, "temperature": _temperature(),
                     "disk": _disk(), "pi": self._pi_check()}
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
            self._now = {"cpu": cpu, "traffic": traffic or {"in": None, "out": None}, "temperature": _temperature(),
                         "disk": _disk(), "pi": self._pi_check()}
            last = now

    def _pi_check(self):
        """Reads the conditions of the Raspberry Pi: {"now": [...], "seen": {name: Unix time or None}}, or None."""
        state = _pi_state(*self._pi)
        if state is None:
            return None
        now, occurred = state
        mono, unix = time.monotonic(), time.time()
        if self._pi_occurred is None:
            for name in _ordered(occurred - now):
                self._pi_seen[name] = None
                log.warning("Raspberry Pi: %s since the board started", name)
        else:
            # Occurred since the last reading: it came and went in between.
            now = now | (occurred - self._pi_occurred)
        self._pi_occurred = occurred
        for name in now:
            self._pi_last[name] = mono
            self._pi_seen[name] = unix
        current = {n for n, t in self._pi_last.items() if mono - t <= PI_HOLD}
        for name in _ordered(current - self._pi_current):
            log.warning("Raspberry Pi: %s", name)
        for name in _ordered(self._pi_current - current):
            log.info("Raspberry Pi: no more %s", name)
        self._pi_current = current
        return {"now": _ordered(current), "seen": {n: self._pi_seen[n] for n in _ordered(self._pi_seen)}}

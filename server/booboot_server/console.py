"""Serial console: always-on capture, cursors, log files, expect."""

from __future__ import annotations

import bisect
import fcntl
import glob
import logging
import os
import re
import select
import termios
import threading
import time
from array import array

from .errors import BadRequest, HardwareError, Unavailable

log = logging.getLogger(__name__)

LINE_ENDINGS = {"cr": b"\r", "lf": b"\n", "crlf": b"\r\n"}
ANSI = re.compile(r"\x1b(\[[0-?]*[ -/]*[@-~]|\][^\x07\x1b]*(\x07|\x1b\\)|[@-Z\\-_])")
# expect() scans new data again from this many bytes back, so that
# a match can span two reads.
OVERLAP = 64 * 1024
# Arrival times kept at most, to bound memory when output comes in tiny reads.
MAX_TIMES = 100000
# Power switch events kept at most.
MAX_EVENTS = 1000


def clean_text(text):
    """Remove terminal escape codes and carriage returns."""
    return ANSI.sub("", text).replace("\r\n", "\n").replace("\r", "")


def find_serial_ports():
    ports = sorted(glob.glob("/dev/serial/by-id/*"))
    if ports:
        return ports
    return sorted(glob.glob("/dev/ttyUSB*") + glob.glob("/dev/ttyACM*"))


class SerialPort:
    """Serial port in raw mode 8N1, using termios only."""

    def __init__(self, device="auto", baudrate=921600):
        self._speed = getattr(termios, "B%d" % baudrate, None)
        if self._speed is None:
            raise ValueError("unsupported baud rate %d" % baudrate)
        self.device = device
        self.baudrate = baudrate
        self.path = ""
        self._fd = -1

    def open(self):
        path = self.device
        if path == "auto":
            ports = find_serial_ports()
            if not ports:
                raise OSError("no USB serial adapter found")
            if len(ports) > 1:
                raise OSError("several serial ports found, set device in [console]: " + ", ".join(ports))
            path = ports[0]
        fd = os.open(path, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK | os.O_CLOEXEC)
        try:
            fcntl.ioctl(fd, termios.TIOCEXCL)
            attrs = termios.tcgetattr(fd)
            attrs[0] = 0  # iflag
            attrs[1] = 0  # oflag
            attrs[2] = termios.CS8 | termios.CREAD | termios.CLOCAL  # cflag
            attrs[3] = 0  # lflag
            attrs[4] = attrs[5] = self._speed
            attrs[6][termios.VMIN] = 0
            attrs[6][termios.VTIME] = 0
            termios.tcsetattr(fd, termios.TCSANOW, attrs)
        except Exception:
            os.close(fd)
            raise
        self._fd = fd
        self.path = path

    def read(self, timeout):
        """Return received bytes, or b"" after timeout. Raise OSError when the port is gone."""
        r, _, _ = select.select([self._fd], [], [], timeout)
        if not r:
            return b""
        try:
            data = os.read(self._fd, 65536)
        except BlockingIOError:
            return b""
        if not data:
            raise OSError("serial port closed")
        return data

    def write(self, data, timeout=10):
        view = memoryview(data)
        deadline = time.monotonic() + timeout
        while view:
            if time.monotonic() > deadline:
                raise OSError("serial write timed out")
            select.select([], [self._fd], [], 0.5)
            try:
                n = os.write(self._fd, view)
            except BlockingIOError:
                continue
            view = view[n:]

    def close(self):
        if self._fd >= 0:
            os.close(self._fd)
            self._fd = -1


class NoPort:
    """No serial port configured."""

    device = "none"
    path = ""
    baudrate = 0

    def open(self):
        raise OSError("no console configured")

    def close(self):
        pass


class ConsoleLog:
    """Console output in files, one per boot, each line with a time stamp."""

    NAME = re.compile(r"console-[0-9-]+\.log$")

    def __init__(self, directory, keep=100):
        self.dir = directory
        self.keep = keep
        self.path = ""
        self._lock = threading.Lock()
        self._f = None
        os.makedirs(directory, exist_ok=True)
        self.new_file()

    def new_file(self, t0=None):
        """Start a new file. Line times count from t0 (time.monotonic())."""
        with self._lock:
            if self._f:
                self._f.close()
            base = os.path.join(self.dir, time.strftime("console-%Y%m%d-%H%M%S"))
            path, n = base + ".log", 1
            while os.path.exists(path):
                path, n = "%s-%d.log" % (base, n), n + 1
            self._f = open(path, "ab")
            self.path = path
            self._f.write(time.strftime("# %Y-%m-%d %H:%M:%S\n").encode())
            self._f.flush()
            self._t0 = time.monotonic() if t0 is None else t0
            self._bol = True
            self._link(path)
            self._prune()

    def _link(self, path):
        latest = os.path.join(self.dir, "latest.log")
        try:
            if os.path.lexists(latest):
                os.unlink(latest)
            os.symlink(os.path.basename(path), latest)
        except OSError:
            pass

    def _prune(self):
        files = sorted(glob.glob(os.path.join(self.dir, "console-*.log")), key=os.path.getmtime)
        for path in files[:-self.keep] if self.keep > 0 else []:
            try:
                os.unlink(path)
            except OSError:
                pass

    def files(self):
        """Return the log files, newest first: dicts with name, size and mtime."""
        files = []
        for path in glob.glob(os.path.join(self.dir, "console-*.log")):
            try:
                st = os.stat(path)
            except OSError:
                continue
            files.append({"name": os.path.basename(path), "size": st.st_size, "mtime": round(st.st_mtime, 1)})
        return sorted(files, key=lambda f: (f["mtime"], f["name"]), reverse=True)

    def file_path(self, name):
        """Return the path of a log file, or None if there is no such file."""
        path = os.path.join(self.dir, name)
        return path if self.NAME.match(name) and os.path.isfile(path) else None

    def write(self, data, now=None):
        now = time.monotonic() if now is None else now
        with self._lock:
            out = bytearray()
            parts = data.split(b"\n")
            for i, part in enumerate(parts):
                last = i == len(parts) - 1
                if last and not part:
                    break
                if self._bol:
                    out += b"[%10.3f] " % (now - self._t0)
                out += part
                if last:
                    self._bol = False
                else:
                    out += b"\n"
                    self._bol = True
            self._f.write(out)
            self._f.flush()

    def close(self):
        with self._lock:
            if self._f:
                self._f.close()
                self._f = None


class Console:
    """Owns the serial port and keeps its output.

    Every received byte has a cursor: its position since the service started.
    Clients read, or wait for text, from a cursor.
    """

    def __init__(self, port, buffer_size=8 << 20, log_dir="", keep_logs=100, line_ending="cr"):
        if line_ending not in LINE_ENDINGS:
            raise ValueError("line_ending must be one of: " + ", ".join(LINE_ENDINGS))
        self.port = port
        self.eol = LINE_ENDINGS[line_ending]
        self.connected = False
        self.error = ""
        self.boot = 0  # cursor at the last power on
        self.last = 0  # cursor after the last expect or run match
        self.started = time.time()  # cursors count from here
        self._size = buffer_size
        self._buf = bytearray()
        self._base = 0  # cursor of _buf[0]
        # Arrival time of each read, and time of each power on, by cursor.
        self._chunk_pos = array("q")
        self._chunk_time = array("d")
        self._boot_pos = []
        self._boot_time = []
        # Power switches: (number, cursor, on, time.time()).
        self._events = []
        self._seq = 0
        self._cond = threading.Condition()
        self._write_lock = threading.Lock()
        self.log = ConsoleLog(log_dir, keep_logs) if log_dir else None
        self._stop = threading.Event()
        self._thread = None

    @property
    def end(self):
        return self._base + len(self._buf)

    def start(self):
        self._thread = threading.Thread(target=self._run, name="console", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(2)
        self.port.close()
        if self.log:
            self.log.close()

    def _run(self):
        while not self._stop.is_set():
            if not self.connected:
                try:
                    self.port.open()
                except OSError as e:
                    if self.error != str(e):
                        log.warning("console: %s", e)
                    self.error = str(e)
                    self._stop.wait(1.0)
                    continue
                self.connected = True
                self.error = ""
                log.info("console on %s at %d baud", self.port.path, self.port.baudrate)
            try:
                data = self.port.read(0.2)
            except OSError as e:
                log.warning("console lost: %s", e)
                self.connected = False
                self.error = str(e)
                self.port.close()
                continue
            if data:
                self._append(data)

    def _append(self, data):
        now = time.monotonic()
        with self._cond:
            self._chunk_pos.append(self.end)
            self._chunk_time.append(now)
            if len(self._chunk_pos) > MAX_TIMES:
                # Forget the times of the oldest half.
                del self._chunk_pos[:MAX_TIMES // 2]
                del self._chunk_time[:MAX_TIMES // 2]
            self._buf += data
            extra = len(self._buf) - self._size
            if extra > 0:
                # Drop at least 1/8 of the buffer to limit copies.
                drop = max(extra, self._size // 8)
                del self._buf[:drop]
                self._base += drop
                self._trim_times()
            self._cond.notify_all()
        if self.log:
            self.log.write(data, now)

    def _trim_times(self):
        """Forget the times of bytes that left the buffer."""
        for pos, times in ((self._chunk_pos, self._chunk_time), (self._boot_pos, self._boot_time)):
            i = bisect.bisect_right(pos, self._base) - 1
            if i > 0:
                del pos[:i]
                del times[:i]

    def _time_of(self, cursor):
        c = bisect.bisect_right(self._chunk_pos, cursor) - 1
        b = bisect.bisect_right(self._boot_pos, cursor) - 1
        if c < 0 or b < 0:
            return None
        return max(0.0, self._chunk_time[c] - self._boot_time[b])

    def time_of(self, cursor):
        """Seconds from the power on to the arrival of a byte, or None."""
        with self._cond:
            return self._time_of(cursor)

    def stamped(self, start, data):
        """Return data with the time since power on at the start of each line."""
        out = bytearray()
        with self._cond:
            i = 0
            while i < len(data):
                j = data.find(b"\n", i)
                j = len(data) if j < 0 else j + 1
                t = self._time_of(start + i)
                out += b"[%10.3f] " % t if t is not None else b"[%10s] " % b"?"
                out += data[i:j]
                i = j
        return bytes(out)

    def resolve(self, since):
        """Turn a since value into a cursor.

        Values: a cursor, a negative number (bytes before the end),
        "start", "boot", "last" or "now".
        """
        with self._cond:
            named = {"start": self._base, "boot": self.boot, "last": self.last, "now": self.end}
            if since in named:
                return named[since]
            try:
                n = int(since)
            except (TypeError, ValueError):
                raise BadRequest("since must be a number, start, boot, last or now") from None
            return max(self._base, self.end + n) if n < 0 else n

    def read(self, since, max_bytes=1 << 20, wait=0.0):
        """Return (start, data) from cursor since. Wait up to wait seconds for data."""
        deadline = time.monotonic() + wait
        with self._cond:
            since = min(since, self.end)
            start = max(since, self._base)
            while self.end <= start:
                left = deadline - time.monotonic()
                if left <= 0:
                    break
                self._cond.wait(left)
                start = max(since, self._base)
            i = start - self._base
            return start, bytes(self._buf[i:i + max_bytes])

    def write(self, data):
        if not self.connected:
            raise Unavailable("console not connected: %s" % self.error)
        with self._write_lock:
            try:
                self.port.write(data)
            except OSError as e:
                raise HardwareError("console write failed: %s" % e) from e

    def mark_boot(self, switch=None):
        """Start a new boot: mark the cursor, call switch (the power on), note the time.

        Output times count from the end of switch.
        """
        with self._cond:
            self.boot = self.last = self.end
        if switch:
            switch()
        now = time.monotonic()
        with self._cond:
            self._boot_pos.append(self.boot)
            self._boot_time.append(now)
            self._event(self.boot, True)
        if self.log:
            self.log.new_file(now)
        return self.boot

    def mark_off(self):
        """Note a power off in the output."""
        with self._cond:
            self._event(self.end, False)

    def _event(self, cursor, on):
        self._seq += 1
        self._events.append((self._seq, cursor, on, time.time()))
        if len(self._events) > MAX_EVENTS:
            del self._events[:MAX_EVENTS // 2]
        self._cond.notify_all()

    def follow(self, since, idle=1.0, max_bytes=65536):
        """Yield the output and the power switches from cursor since, as they come.

        Items: ("output", cursor, data), ("power", cursor, on, time), or None
        when nothing came for idle seconds. Output that left the buffer before
        it was read is skipped.
        """
        with self._cond:
            pos = max(min(since, self.end), self._base)
            seq = next((e[0] - 1 for e in self._events if e[1] >= pos), self._seq)
        while True:
            with self._cond:
                item = self._next(pos, seq, max_bytes)
                if item is None:
                    self._cond.wait(idle)
                    item = self._next(pos, seq, max_bytes)
            if item is None:
                yield None
            elif item[0] == "power":
                seq = item[4]
                yield item[:4]
            else:
                pos = item[1] + len(item[2])
                yield item

    def _next(self, pos, seq, max_bytes):
        event = None
        if self._events:
            i = max(0, seq + 1 - self._events[0][0])
            if i < len(self._events):
                event = self._events[i]
        pos = max(pos, self._base)
        if event and event[1] <= pos:
            return ("power", event[1], event[2], event[3], event[0])
        stop = min(self.end, pos + max_bytes, event[1] if event else self.end)
        if stop > pos:
            return ("output", pos, bytes(self._buf[pos - self._base:stop - self._base]))
        return None

    def expect(self, pattern, since, timeout):
        """Wait for a bytes regex in the output after cursor since.

        Return (matched, start, data, match, next): data is the output from
        start up to the end of the match, next is the cursor after it.
        """
        deadline = time.monotonic() + timeout
        with self._cond:
            since = min(since, self.end)
        scanned = since
        while True:
            with self._cond:
                start = max(since, self._base)
                frm = max(start, scanned - OVERLAP)
                # Keep one byte before frm so that "^" works as in the full text.
                lead = 1 if frm > start else 0
                data = bytes(self._buf[frm - lead - self._base:])
                end = self.end
            m = pattern.search(data, lead)
            if m:
                stop = frm - lead + m.end()
                with self._cond:
                    i = max(start, self._base) - self._base
                    out = bytes(self._buf[i:max(i, stop - self._base)])
                    self.last = stop
                return True, start, out, m, stop
            scanned = end
            with self._cond:
                while self.end == end:
                    left = deadline - time.monotonic()
                    if left <= 0:
                        i = max(start, self._base) - self._base
                        return False, start, bytes(self._buf[i:]), None, self.end
                    self._cond.wait(left)

    def run(self, command, pattern, timeout):
        """Send a command line and wait for the prompt.

        Return (matched, output, next). The output has neither the echoed
        command line nor the prompt line.
        """
        deadline = time.monotonic() + timeout
        start = self.end
        self.write(command.encode("utf-8") + self.eol)
        # Look for the prompt only after the first line: it is the echo of
        # the command, which can look like a prompt while it arrives.
        ok, _, first, _, line_end = self.expect(re.compile(b"\n"), start, timeout)
        if not ok:
            return False, first, line_end
        left = max(0.0, deadline - time.monotonic())
        matched, _, out, m, nxt = self.expect(pattern, line_end, left)
        if m:
            out = out[:len(out) - len(m.group(0))]
            out = out[:out.rfind(b"\n") + 1]
        if command.strip().encode("utf-8") not in first:
            out = first + out
        return matched, out, nxt

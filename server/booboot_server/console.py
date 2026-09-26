"""Serial console: always-on capture, cursors, log files, expect."""

from __future__ import annotations

import fcntl
import glob
import logging
import os
import re
import select
import termios
import threading
import time

from .errors import BadRequest, HardwareError, Unavailable

log = logging.getLogger(__name__)

LINE_ENDINGS = {"cr": b"\r", "lf": b"\n", "crlf": b"\r\n"}
ANSI = re.compile(r"\x1b(\[[0-?]*[ -/]*[@-~]|\][^\x07\x1b]*(\x07|\x1b\\)|[@-Z\\-_])")
# expect() scans new data again from this many bytes back, so that
# a match can span two reads.
OVERLAP = 64 * 1024


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

    def __init__(self, directory, keep=100):
        self.dir = directory
        self.keep = keep
        self._lock = threading.Lock()
        self._f = None
        os.makedirs(directory, exist_ok=True)
        self.new_file()

    def new_file(self):
        with self._lock:
            if self._f:
                self._f.close()
            base = os.path.join(self.dir, time.strftime("console-%Y%m%d-%H%M%S"))
            path, n = base + ".log", 1
            while os.path.exists(path):
                path, n = "%s-%d.log" % (base, n), n + 1
            self._f = open(path, "ab")
            self._f.write(time.strftime("# %Y-%m-%d %H:%M:%S\n").encode())
            self._f.flush()
            self._t0 = time.monotonic()
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

    def write(self, data):
        with self._lock:
            out = bytearray()
            parts = data.split(b"\n")
            for i, part in enumerate(parts):
                last = i == len(parts) - 1
                if last and not part:
                    break
                if self._bol:
                    out += b"[%10.3f] " % (time.monotonic() - self._t0)
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
        self._size = buffer_size
        self._buf = bytearray()
        self._base = 0  # cursor of _buf[0]
        self._cond = threading.Condition()
        self._write_lock = threading.Lock()
        self._log = ConsoleLog(log_dir, keep_logs) if log_dir else None
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
        if self._log:
            self._log.close()

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
        with self._cond:
            self._buf += data
            extra = len(self._buf) - self._size
            if extra > 0:
                # Drop at least 1/8 of the buffer to limit copies.
                drop = max(extra, self._size // 8)
                del self._buf[:drop]
                self._base += drop
            self._cond.notify_all()
        if self._log:
            self._log.write(data)

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

    def mark_boot(self):
        with self._cond:
            self.boot = self.last = self.end
        if self._log:
            self._log.new_file()
        return self.boot

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

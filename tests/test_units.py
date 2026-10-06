import bz2
import gzip
import io
import lzma
import os
import queue
import re
import shutil
import struct
import tempfile
import threading
import time
import unittest
from unittest import mock

import common  # noqa: F401  (sets sys.path)
from booboot_server import gpio, netinfo, session, storage
from booboot_server.console import Console, clean_text
from booboot_server.errors import BadRequest, Busy, Conflict, NoSession
from booboot_server.session import Sessions


def ioc_rw(nr, size):
    return (3 << 30) | (size << 16) | (0xB4 << 8) | nr


class GpioLayout(unittest.TestCase):
    def test_ioctl_numbers(self):
        self.assertEqual(gpio.GET_CHIPINFO, (2 << 30) | (68 << 16) | (0xB4 << 8) | 0x01)
        self.assertEqual(gpio.GET_LINEINFO, ioc_rw(0x05, gpio.LINEINFO_SIZE))
        self.assertEqual(gpio.GET_LINE, ioc_rw(0x07, gpio.REQUEST_SIZE))
        self.assertEqual(gpio.SET_VALUES, ioc_rw(0x0F, 16))

    def test_request(self):
        req = gpio.build_request(17, True, True)
        self.assertEqual(len(req), 592)
        self.assertEqual(struct.unpack_from("=I", req, 0)[0], 17)
        self.assertEqual(bytes(req[256:263]), b"booboot")
        flags, num_attrs = struct.unpack_from("=QI", req, 288)
        self.assertEqual(flags, gpio.FLAG_OUTPUT | gpio.FLAG_ACTIVE_LOW)
        self.assertEqual(num_attrs, 1)
        self.assertEqual(struct.unpack_from("=IIQQ", req, 320), (gpio.ATTR_OUTPUT_VALUES, 0, 1, 1))
        self.assertEqual(struct.unpack_from("=I", req, 560)[0], 1)

    def test_find_line_by_number(self):
        self.assertEqual(gpio.find_line("5", "gpiochip2"), ("/dev/gpiochip2", 5))
        self.assertEqual(gpio.find_line("17"), ("/dev/gpiochip0", 17))


class InflaterTest(unittest.TestCase):
    def inflate(self, kind, data, step=4096):
        inf = storage.Inflater(kind)
        out = bytearray()
        for i in range(0, len(data), step):
            for chunk in inf.feed(data[i:i + step]):
                self.assertLessEqual(len(chunk), storage.CHUNK)
                out += chunk
        inf.finish()
        return bytes(out)

    def test_formats(self):
        raw = os.urandom(300000) + b"\0" * 5000000
        for kind, comp in (("gz", gzip.compress), ("xz", lzma.compress), ("bz2", bz2.compress)):
            data = comp(raw)
            self.assertEqual(storage.detect_compression(data), kind)
            self.assertEqual(self.inflate(kind, data), raw, kind)
            # Several streams in one file
            self.assertEqual(self.inflate(kind, data + comp(b"end")), raw + b"end", kind)

    def test_zstd(self):
        try:
            from compression import zstd  # Python 3.14 or later
        except ImportError:
            with self.assertRaises(BadRequest):
                storage.Inflater("zst")
            return
        raw = os.urandom(300000) + bytes(5000000)
        data = zstd.compress(raw) + zstd.compress(b"end")
        self.assertEqual(storage.detect_compression(data), "zst")
        self.assertEqual(self.inflate("zst", data), raw + b"end")

    def test_xz_padding(self):
        data = lzma.compress(b"hello") + b"\0" * 8
        self.assertEqual(self.inflate("xz", data), b"hello")

    def test_truncated(self):
        data = gzip.compress(os.urandom(10000))
        with self.assertRaises(BadRequest):
            self.inflate("gz", data[:-100])

    def test_raw(self):
        self.assertEqual(storage.detect_compression(b"\0" * 512), "none")
        self.assertEqual(self.inflate("none", b"abc" * 10000), b"abc" * 10000)

    def test_write_image_to_file(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp)
        target = os.path.join(tmp, "card.img")
        st = storage.Storage(lambda: target, "")
        raw = os.urandom(3 << 20)
        r = st.write_image(io.BytesIO(lzma.compress(raw)), verify=True)
        self.assertEqual(r["bytes"], len(raw))
        self.assertTrue(r["verified"])
        self.assertEqual(r["compression"], "xz")
        with open(target, "rb") as f:
            self.assertEqual(f.read(), raw)


class PathTest(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.root)

    def test_resolve(self):
        self.assertEqual(storage.resolve(self.root, "/"), self.root)
        self.assertEqual(storage.resolve(self.root, "../../etc/passwd"), os.path.join(self.root, "etc", "passwd"))
        self.assertEqual(storage.resolve(self.root, "a//b/"), os.path.join(self.root, "a", "b"))

    def test_symlink_escape(self):
        os.symlink("/etc", os.path.join(self.root, "link"))
        with self.assertRaises(BadRequest):
            storage.open_path(self.root, "/link/passwd")
        with self.assertRaises(BadRequest):
            storage.open_path(self.root, "/link")
        with self.assertRaises(BadRequest):
            storage.put_file(self.root, "/link/x", io.BytesIO(b""))

    def test_files(self):
        self.assertEqual(storage.put_file(self.root, "/a/b/f.bin", io.BytesIO(b"123")), 3)
        kind, entries = storage.open_path(self.root, "/a/b")
        self.assertEqual((kind, entries[0]["name"], entries[0]["size"]), ("dir", "f.bin", 3))
        kind, f = storage.open_path(self.root, "/a/b/f.bin")
        with f:
            self.assertEqual(f.read(), b"123")
        self.assertEqual(os.listdir(os.path.join(self.root, "a", "b")), ["f.bin"])
        with self.assertRaises(Conflict):
            storage.delete(self.root, "/a")
        storage.delete(self.root, "/a", recursive=True)
        self.assertEqual(os.listdir(self.root), [])


class QueuePort:
    """Serial port stand-in. A write gets an echo, the reply and a prompt."""

    device = path = "test"
    baudrate = 0

    def __init__(self):
        self.q = queue.Queue()
        self.replies = {}

    def open(self):
        pass

    def read(self, timeout):
        try:
            return self.q.get(timeout=timeout)
        except queue.Empty:
            return b""

    def write(self, data):
        line = data.rstrip(b"\r\n")
        if line in self.replies:
            threading.Thread(target=self._answer, args=(line,)).start()

    def _answer(self, line):
        # The echo comes in two parts, like on a slow serial line.
        # The first part ends after "> " when there is one.
        k = line.find(b"> ") + 2 if b"> " in line else len(line) // 2
        self.q.put(line[:k])
        time.sleep(0.3)
        self.q.put(line[k:] + b"\r\n")
        self.q.put(self.replies[line])
        self.q.put(b"root@test:~# ")

    def close(self):
        pass


class ConsoleTest(unittest.TestCase):
    def setUp(self):
        self.port = QueuePort()
        self.log_dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.log_dir)
        self.c = Console(self.port, buffer_size=4096, log_dir=self.log_dir)
        self.c.start()
        self.addCleanup(self.c.stop)
        deadline = time.monotonic() + 2
        while not self.c.connected and time.monotonic() < deadline:
            time.sleep(0.01)

    def feed(self, *chunks):
        for chunk in chunks:
            self.port.q.put(chunk)

    def test_read_and_wait(self):
        self.feed(b"hello\r\n")
        start, data = self.c.read(0, wait=2)
        self.assertEqual((start, data), (0, b"hello\r\n"))
        t = time.monotonic()
        start, data = self.c.read(self.c.end, wait=0.3)
        self.assertEqual(data, b"")
        self.assertGreater(time.monotonic() - t, 0.25)
        self.assertEqual(self.c.resolve("-3"), 4)
        self.assertEqual(self.c.resolve("now"), 7)

    def test_expect_split(self):
        self.feed(b"boot...\r\nfake log", b"in: ")
        matched, start, data, m, nxt = self.c.expect(re.compile(b"login: $"), 0, 2)
        self.assertTrue(matched)
        self.assertEqual(data, b"boot...\r\nfake login: ")
        self.assertEqual(nxt, len(data))
        self.assertEqual(self.c.last, nxt)

    def test_written(self):
        self.assertEqual(self.c.written, 0)
        self.c.write(b"abc")
        self.c.run("", re.compile(b"[#$>] $"), 0.1)
        self.assertEqual(self.c.written, 4)

    def test_expect_timeout(self):
        self.feed(b"nothing here")
        matched, start, data, m, nxt = self.c.expect(re.compile(b"login"), 0, 0.3)
        self.assertFalse(matched)
        self.assertEqual(data, b"nothing here")

    def test_expect_line_start(self):
        # "^" only matches at a line start, also when the search restarts.
        self.feed(b"xOK\r\n")
        time.sleep(0.1)
        self.feed(b"OK\r\n")
        matched, start, data, m, nxt = self.c.expect(re.compile(rb"(?m)^OK"), 0, 2)
        self.assertTrue(matched)
        self.assertEqual(data, b"xOK\r\nOK")

    def test_ring_buffer(self):
        self.feed(b"a" * 3000, b"b" * 3000)
        deadline = time.monotonic() + 2
        while self.c.end < 6000 and time.monotonic() < deadline:
            time.sleep(0.01)
        start, data = self.c.read(0)
        self.assertGreater(start, 0)
        self.assertLessEqual(len(data), 4096)
        self.assertTrue(data.endswith(b"b" * 3000))
        # Times of dropped bytes are dropped too.
        self.assertLessEqual(len(self.c._chunk_pos), 2)
        self.assertLessEqual(self.c._chunk_pos[0], start)

    def test_run(self):
        self.port.replies[b"uname -a"] = b"Linux test\r\n"
        self.port.replies[b""] = b""
        matched, out, nxt = self.c.run("uname -a", re.compile(b"[#$>] $"), 2)
        self.assertTrue(matched)
        self.assertEqual(out, b"Linux test\r\n")
        matched, out, nxt = self.c.run("", re.compile(b"[#$>] $"), 2)
        self.assertEqual(out, b"")
        # The first half of this echo ends with "> ", like a prompt.
        self.port.replies[b"echo abc > /tmp/x"] = b"written\r\n"
        matched, out, nxt = self.c.run("echo abc > /tmp/x", re.compile(b"[#$>] $"), 2)
        self.assertEqual((matched, out), (True, b"written\r\n"))
        self.assertEqual(self.c.last, self.c.end)
        # No echo line: timeout.
        matched, out, nxt = self.c.run("unknown", re.compile(b"[#$>] $"), 0.3)
        self.assertFalse(matched)

    def test_log_file(self):
        self.c.mark_boot()
        self.feed(b"line one\r\nline two\r\n")
        deadline = time.monotonic() + 2
        while self.c.end < 20 and time.monotonic() < deadline:
            time.sleep(0.01)
        with open(os.path.join(self.log_dir, "latest.log"), "rb") as f:
            lines = f.read().split(b"\n")
        self.assertTrue(lines[1].startswith(b"[") and lines[1].endswith(b"] line one\r"), lines)
        self.assertTrue(lines[2].endswith(b"] line two\r"), lines)

    def test_times(self):
        # Times count from the end of the power on switch.
        self.c.mark_boot(lambda: time.sleep(0.2))
        boot = self.c.boot
        time.sleep(0.3)
        self.feed(b"U-Boot\r\nlogin: ")
        matched, start, data, m, nxt = self.c.expect(re.compile(b"login: $"), boot, 2)
        self.assertTrue(matched)
        t = self.c.time_of(nxt - 1)
        self.assertTrue(0.25 < t < 0.45, t)  # 0.3 s, without the 0.2 s of the switch
        self.assertIsNone(Console(QueuePort()).time_of(0))
        stamped = self.c.stamped(start, data).decode()
        self.assertRegex(stamped, r"^\[ +0\.[0-9]{3}\] U-Boot\r\n\[ +0\.[0-9]{3}\] login: $")
        # A new boot: times count from it.
        self.c.mark_boot()
        self.feed(b"again\n")
        matched, start, data, m, nxt = self.c.expect(re.compile(b"again"), self.c.boot, 2)
        self.assertLess(self.c.time_of(nxt - 1), 0.25)
        self.assertGreater(self.c.time_of(boot), 0.25)  # older bytes keep their boot

    def test_follow(self):
        self.feed(b"old\n")
        self.wait_end(4)
        self.c.mark_off()
        items = self.c.follow(self.c.end, idle=0.1)
        self.assertEqual(next(items)[:3], ("power", 4, False))
        self.assertIsNone(next(items))
        self.c.mark_boot()
        self.feed(b"new\n")
        on = next(items)
        self.assertEqual(on[:3], ("power", 4, True))
        self.assertAlmostEqual(on[3], time.time(), delta=5)
        self.assertEqual(next(items), ("output", 4, b"new\n"))
        # Output up to a power switch comes before it.
        self.feed(b"end")
        self.wait_end(11)
        self.c.mark_off()
        self.assertEqual(next(items), ("output", 8, b"end"))
        self.assertEqual(next(items)[:3], ("power", 11, False))
        # A new reader gets the switches from its start cursor on.
        items = self.c.follow(4, idle=0.1)
        self.assertEqual(next(items)[:3], ("power", 4, False))
        self.assertEqual(next(items)[:3], ("power", 4, True))
        self.assertEqual(next(items), ("output", 4, b"new\nend"))
        self.assertEqual(next(items)[:3], ("power", 11, False))
        self.assertIsNone(next(items))

    def test_follow_skips_lost_output(self):
        items = self.c.follow(0, idle=0.1, max_bytes=1000)
        self.feed(b"a" * 3000, b"b" * 3000)
        self.wait_end(6000)
        kind, start, data = next(items)
        self.assertGreater(start, 0)
        self.assertEqual(len(data), 1000)
        rest = b"".join(item[2] for item in iter(lambda: next(items), None))
        self.assertTrue((data + rest).endswith(b"b" * 3000))
        self.assertEqual(start + len(data) + len(rest), 6000)

    def wait_end(self, end):
        deadline = time.monotonic() + 2
        while self.c.end < end and time.monotonic() < deadline:
            time.sleep(0.01)

    def test_times_bounded(self):
        self.c.mark_boot()
        with mock.patch("booboot_server.console.MAX_TIMES", 10):
            for i in range(25):
                self.feed(b"%d\n" % i)
                time.sleep(0.01)
            deadline = time.monotonic() + 2
            while not self.c.read(0)[1].endswith(b"24\n") and time.monotonic() < deadline:
                time.sleep(0.01)
        self.assertLessEqual(len(self.c._chunk_pos), 10)
        self.assertIsNotNone(self.c.time_of(self.c.end - 1))
        self.assertIsNone(self.c.time_of(0))  # oldest times are gone

    def test_clean_text(self):
        self.assertEqual(clean_text("\x1b[0;32mOK\x1b[0m\r\nnext\r\r\n"), "OK\nnext\n")


class SessionTest(unittest.TestCase):
    def test_lease(self):
        s = Sessions(default_timeout=0.3)
        token, info = s.open("a")
        self.assertEqual(info["client"], "a")
        with self.assertRaises(Busy):
            s.open("b")
        with self.assertRaises(Busy):
            s.begin("wrong")
        s.begin(token)
        time.sleep(0.4)
        with self.assertRaises(Busy):
            s.open("b")  # no expiry while a request runs
        s.end(token)
        self.assertTrue(s.status(token)["yours"])
        time.sleep(0.4)
        self.assertFalse(s.status()["active"])
        with self.assertRaises(NoSession):
            s.begin(token)
        token_b, _ = s.open("b")
        token_c, _ = s.open("c", force=True)
        with self.assertRaises(Busy):
            s.close(token_b)
        s.close(token_c)
        self.assertFalse(s.status()["active"])

    def test_timeout_limits(self):
        s = Sessions(max_timeout=10)
        with self.assertRaises(BadRequest):
            s.open("a", timeout=11)

    @mock.patch.object(session, "GONE_AFTER", 0.3)
    def test_gone(self):
        s = Sessions()
        token, info = s.open("cli")
        self.assertIsNone(info["alive"])  # no heartbeat
        with self.assertRaises(Busy):
            s.open("b", force="gone")
        token, _ = s.open("viewer", force=True)
        info = s.heartbeat(token)
        self.assertTrue(info["alive"] and info["yours"])
        s.begin(token)
        s.end(token)
        with self.assertRaises(Busy) as e:
            s.open("b", force="gone")
        self.assertTrue(e.exception.info["session"]["alive"])
        self.assertEqual(s.status()["idle"], 0.0)
        time.sleep(0.4)
        self.assertGreater(s.status()["idle"], 0.3)  # a heartbeat is not activity
        self.assertFalse(s.status()["alive"])
        self.assertGreater(s.status()["heartbeat_age"], 0.3)
        with self.assertRaises(Busy):
            s.open("b")
        token_b, _ = s.open("b", force="gone")
        with self.assertRaises(Busy):
            s.heartbeat(token)
        self.assertEqual(s.status(token_b)["client"], "b")


class NetInfoTest(unittest.TestCase):
    def test_addresses(self):
        found = netinfo.addresses()
        self.assertFalse([a for a in found if a.startswith(("127.", "169.254.")) or a == "::1"])
        info = netinfo.info()
        self.assertEqual(set(info), {"hostname", "addresses"})
        self.assertIs(netinfo.info(), info)  # kept for a while

    def test_ipv6(self):
        path = os.path.join(tempfile.mkdtemp(), "if_inet6")
        self.addCleanup(shutil.rmtree, os.path.dirname(path))
        with open(path, "w") as f:
            f.write("20010db8000000000000000000000005 02 40 00 80 eth0\n"
                    "fe800000000000000000000000000001 02 40 20 80 eth0\n"
                    "00000000000000000000000000000001 01 80 10 80 lo\n"
                    "fd7a115c000000000000000000000001 05 80 00 80 docker0\n")
        self.assertEqual(netinfo._ipv6(path), ["2001:db8::5"])
        self.assertEqual(netinfo._ipv6(path + ".none"), [])


if __name__ == "__main__":
    unittest.main()

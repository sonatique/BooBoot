"""Hardware facing code, with stand-ins for the hardware."""

import contextlib
import fcntl
import io
import os
import shutil
import struct
import sys
import tempfile
import threading
import time
import types
import unittest
from unittest import mock

import common  # noqa: F401  (sets sys.path)
from booboot_server import __main__ as server_main
from booboot_server import config, gpio, power, sdmux
from booboot_server.console import Console, NoPort, SerialPort
from booboot_server.dut import Dut
from booboot_server.errors import BadRequest, Conflict, HardwareError, Unavailable


def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(text)


def make_sysfs(root, devices):
    """Build a fake /sys/class/scsi_generic. devices: (sg name, model, USB serial, block name)."""
    for i, (sg, model, serial, block) in enumerate(devices):
        usb = os.path.join(root, "devices", "usb1", "1-%d" % i)
        scsi = os.path.join(usb, "1-%d:1.0" % i, "host%d" % i, "target%d:0:0" % i, "%d:0:0:0" % i)
        os.makedirs(os.path.join(scsi, "block", block) if block else scsi)
        write(os.path.join(usb, "idVendor"), "0424\n")
        write(os.path.join(usb, "serial"), serial + "\n")
        write(os.path.join(scsi, "model"), model.ljust(16) + "\n")
        sg_dir = os.path.join(root, "class", "scsi_generic", sg)
        os.makedirs(sg_dir)
        os.symlink(scsi, os.path.join(sg_dir, "device"))
    return os.path.join(root, "class", "scsi_generic")


class FakeDriver:
    """Stand-in for a usbsdmux driver."""

    def __init__(self, state, sg):
        self.state = state
        state["sg"] = sg

    def _check(self):
        if self.state.get("fail"):
            raise OSError("I2C transaction failed")

    def get_mode(self):
        self._check()
        return self.state["mode"]

    def mode_host(self):
        self._check()
        self.state["mode"] = "host"

    def mode_DUT(self):
        self._check()
        self.state["mode"] = "dut"

    def mode_disconnect(self):
        self._check()
        self.state["mode"] = "off"

    def gpio_set_high(self, index):
        if self.state.get("classic"):
            raise NotImplementedError()
        self.state["gpio%d" % index] = "high"

    def gpio_set_low(self, index):
        if self.state.get("classic"):
            raise NotImplementedError()
        self.state["gpio%d" % index] = "low"


def fake_usbsdmux(state):
    module = types.ModuleType("usbsdmux.usbsdmux")
    module.autoselect_driver = lambda sg: FakeDriver(state, sg)
    return {"usbsdmux": types.ModuleType("usbsdmux"), "usbsdmux.usbsdmux": module}


class SdMuxTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp)

    def use_sysfs(self, devices):
        path = make_sysfs(self.tmp, devices)
        patcher = mock.patch.object(sdmux, "SYS_SG", path)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_find(self):
        self.use_sysfs([("sg0", "Flash Reader", "111", "sda"),
                        ("sg1", "sdFST HS-SD/MMC", "000000001234", "sdb"),
                        ("sg2", "sdmux HS-SD/MMC", "000000005678", "")])
        self.assertEqual(sdmux.find_muxes(), [
            {"sg": "/dev/sg1", "serial": "000000001234", "model": "fast", "block": "/dev/sdb"},
            {"sg": "/dev/sg2", "serial": "000000005678", "model": "classic", "block": ""},
        ])

    def test_select_by_serial(self):
        self.use_sysfs([("sg1", "sdFST HS-SD/MMC", "A1", "sdb"), ("sg2", "sdFST HS-SD/MMC", "B2", "sdc")])
        with self.assertRaises(Unavailable) as e:
            sdmux.UsbSdMux().block_device()
        self.assertIn("A1, B2", e.exception.message)
        self.assertEqual(sdmux.UsbSdMux("B2").block_device(), "/dev/sdc")
        with self.assertRaises(Unavailable):
            sdmux.UsbSdMux("C3").block_device()

    def test_switch(self):
        self.use_sysfs([("sg3", "sdFST HS-SD/MMC", "A1", "sdd")])
        state = {"mode": "dut"}
        with mock.patch.dict(sys.modules, fake_usbsdmux(state)):
            mux = sdmux.UsbSdMux()
            for mode in ("host", "off", "dut"):
                mux.set_mode(mode)
                self.assertEqual(mux.get_mode(), mode)
            self.assertEqual(state["sg"], "/dev/sg3")
            mux.set_gpio(1, False)
            self.assertEqual(state["gpio1"], "low")
            state["fail"] = True
            with self.assertRaises(HardwareError):
                mux.set_mode("host")
            state["classic"] = True
            with self.assertRaises(Unavailable):
                mux.set_gpio(0, True)

    def test_package_missing(self):
        self.use_sysfs([("sg3", "sdFST HS-SD/MMC", "A1", "sdd")])
        with mock.patch.dict(sys.modules, {"usbsdmux": None, "usbsdmux.usbsdmux": None}):
            with self.assertRaises(Unavailable) as e:
                sdmux.UsbSdMux().get_mode()
        self.assertIn("pip install usbsdmux", e.exception.message)


class FakeLine:
    instances = []

    def __init__(self, chip, offset, active_low, value):
        self.args = (chip, offset, active_low, value)
        self.values = [value]
        self.closed = False
        FakeLine.instances.append(self)

    def set(self, value):
        self.values.append(value)

    def close(self):
        self.closed = True


class PowerTest(unittest.TestCase):
    def test_gpio(self):
        FakeLine.instances = []
        with mock.patch.object(gpio, "find_line", return_value=("/dev/gpiochip3", 5)) as find, \
                mock.patch.object(gpio, "OutputLine", FakeLine):
            p = power.GpioPower("GPIO17", "", active_low=True)
            p.set(False)
            p.set(True)
            p.close()
        find.assert_called_once_with("GPIO17", "")
        [line] = FakeLine.instances
        self.assertEqual(line.args, ("/dev/gpiochip3", 5, True, False))
        self.assertEqual(line.values, [False, True])
        self.assertTrue(line.closed)

    def test_gpio_error(self):
        with mock.patch.object(gpio, "find_line", side_effect=gpio.GpioError("GPIO line 'X' not found")):
            with self.assertRaises(HardwareError) as e:
                power.GpioPower("X").set(False)
        self.assertIn("not found", e.exception.message)

    def test_sdmux_gpio(self):
        calls = []
        mux = types.SimpleNamespace(set_gpio=lambda index, high: calls.append((index, high)))
        power.SdmuxGpioPower(mux, 1, active_low=False).set(True)
        power.SdmuxGpioPower(mux, 0, active_low=True).set(True)
        power.SdmuxGpioPower(mux, 0, active_low=True).set(False)
        self.assertEqual(calls, [(1, True), (0, False), (0, True)])

    def test_command(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp)
        state = os.path.join(tmp, "state")
        p = power.CommandPower("echo on > '%s'" % state, "exit 3")
        p.set(True)
        with open(state) as f:
            self.assertEqual(f.read(), "on\n")
        with self.assertRaises(HardwareError) as e:
            p.set(False)
        self.assertIn("(3)", e.exception.message)
        with self.assertRaises(Unavailable):
            power.CommandPower("true", "").set(False)


class GpioLookupTest(unittest.TestCase):
    def test_find_by_name(self):
        names = {"/dev/gpiochip0": ["ID_SDA", "GPIO17"], "/dev/gpiochip1": ["A", "B", "GPIO27"]}

        def line_names(path):
            if path == "/dev/gpiochip2":
                raise OSError("busy")
            return names[path]

        with mock.patch.object(gpio, "chips", return_value=["/dev/gpiochip2", "/dev/gpiochip0", "/dev/gpiochip1"]), \
                mock.patch.object(gpio, "line_names", side_effect=line_names):
            self.assertEqual(gpio.find_line("GPIO27"), ("/dev/gpiochip1", 2))
            self.assertEqual(gpio.find_line("GPIO17"), ("/dev/gpiochip0", 1))
            with self.assertRaises(gpio.GpioError):
                gpio.find_line("GPIO17", "gpiochip1")
            with self.assertRaises(gpio.GpioError):
                gpio.find_line("GPIO99")


def mockup_chip():
    """Return the path of a gpio-mockup chip, if the kernel has one and we may use it."""
    if not hasattr(os, "geteuid") or os.geteuid() != 0:
        return None
    for path in gpio.chips():
        try:
            if gpio.chip_info(path)[1].startswith("gpio-mockup"):
                return path
        except OSError:
            pass
    return None


MOCKUP = mockup_chip()


@unittest.skipUnless(MOCKUP, "needs root and a gpio-mockup chip (modprobe gpio-mockup)")
class RealGpioTest(unittest.TestCase):
    """The ioctl calls, on a simulated GPIO chip of the kernel."""

    def line_info(self, offset):
        fd = os.open(MOCKUP, os.O_RDWR)
        try:
            buf = bytearray(gpio.LINEINFO_SIZE)
            struct.pack_into("=I", buf, 64, offset)
            fcntl.ioctl(fd, gpio.GET_LINEINFO, buf)
        finally:
            os.close(fd)
        return gpio._cstr(buf[32:64]), struct.unpack_from("=Q", buf, 72)[0]

    def value(self, line):
        buf = bytearray(struct.pack("=QQ", 0, 1))
        fcntl.ioctl(line._fd, 0xC010B40E, buf)  # GPIO_V2_LINE_GET_VALUES_IOCTL
        return struct.unpack("=QQ", buf)[0]

    def test_output_line(self):
        names = gpio.line_names(MOCKUP)
        self.assertEqual(len(names), gpio.chip_info(MOCKUP)[2])
        line = gpio.OutputLine(MOCKUP, 3, active_low=True, value=True)
        try:
            consumer, flags = self.line_info(3)
            self.assertEqual(consumer, "booboot")
            self.assertTrue(flags & gpio.FLAG_OUTPUT)
            self.assertTrue(flags & gpio.FLAG_ACTIVE_LOW)
            self.assertEqual(self.value(line), 1)
            line.set(False)
            self.assertEqual(self.value(line), 0)
            with self.assertRaises(gpio.GpioError):
                gpio.OutputLine(MOCKUP, 3)  # already used
        finally:
            line.close()
        if names[3]:
            self.assertEqual(gpio.find_line(names[3]), (MOCKUP, 3))

    def test_power_backend(self):
        p = power.GpioPower("2", os.path.basename(MOCKUP))
        p.set(True)
        p.set(False)
        p.close()


class StubMux:
    def __init__(self, mode="dut"):
        self.mode = mode
        self.fail = None

    def get_mode(self):
        if self.fail:
            raise self.fail
        return self.mode

    def set_mode(self, mode):
        if self.fail:
            raise self.fail
        self.mode = mode


class StubStorage:
    def __init__(self):
        self.ready = 0
        self.fail = None

    def wait_ready(self):
        self.ready += 1

    def write_image(self, body, compression, verify, progress):
        if self.fail:
            raise self.fail
        progress(10)
        return {"bytes": 10, "sha256": "x"}


class DutRules(unittest.TestCase):
    def setUp(self):
        self.states = []
        self.power = power.NoPower(self.states.append)
        self.mux = StubMux()
        self.storage = StubStorage()
        self.dut = Dut("t", self.power, self.mux, self.storage, Console(NoPort()), off_time=0)
        self.dut.start()
        self.addCleanup(self.dut.stop)

    def test_start_powers_off(self):
        self.assertEqual(self.states, [False])
        self.assertEqual((self.dut.power_state, self.dut.mux_mode), ("off", "dut"))

    def test_power_on_moves_card(self):
        self.dut.set_sd("host")
        self.assertEqual(self.storage.ready, 1)
        self.dut.console.mark_boot()
        r = self.dut.set_power(True)
        self.assertEqual((r["power"], self.mux.mode), ("on", "dut"))
        with self.assertRaises(Conflict) as e:
            self.dut.set_sd("host")
        self.assertEqual(e.exception.code, "power_on")
        with self.assertRaises(Conflict):
            self.dut.write_image(io.BytesIO(b""))
        self.assertEqual(self.dut.power_cycle(0)["power"], "on")
        self.assertEqual(self.states[-3:], [True, False, True])

    def test_power_on_without_mux(self):
        self.mux.fail = Unavailable("no USB-SD-Mux found")
        self.dut.mux_mode = "unknown"
        self.assertEqual(self.dut.set_power(True)["power"], "on")
        self.assertEqual(self.dut.status()["sd"]["error"], "no USB-SD-Mux found")

    def test_power_error_blocks_card(self):
        def broken(on):
            raise OSError("relay board gone")

        self.power._on_change = broken
        with self.assertRaises(HardwareError):
            self.dut.set_power(False)
        status = self.dut.status()["power"]
        self.assertEqual((status["state"], status["error"]), ("unknown", "relay board gone"))
        with self.assertRaises(Conflict):
            self.dut.set_sd("host")

    def test_bad_mode(self):
        with self.assertRaises(BadRequest):
            self.dut.set_sd("maybe")

    def test_card_state(self):
        self.dut.write_image(io.BytesIO(b""))
        self.assertEqual(self.dut.card["state"], "written")
        self.storage.fail = BadRequest("bad xz data")
        with self.assertRaises(BadRequest):
            self.dut.write_image(io.BytesIO(b""))
        self.assertEqual(self.dut.card["state"], "incomplete")

    def test_one_operation_at_a_time(self):
        started, release = threading.Event(), threading.Event()

        def hold():
            with self.dut._op("write image"):
                started.set()
                release.wait(10)

        t = threading.Thread(target=hold)
        t.start()
        started.wait(5)
        try:
            self.assertEqual(self.dut.status()["operation"]["name"], "write image")
            with self.assertRaises(Conflict) as e:
                self.dut.set_power(True)
            self.assertEqual(e.exception.code, "operation_in_progress")
        finally:
            release.set()
            t.join()


class FlakyPort:
    """Fails to open once, then fails to read once, then works."""

    device = path = "flaky"
    baudrate = 0

    def __init__(self):
        self.opens = 0
        self.read_fails = 1

    def open(self):
        self.opens += 1
        if self.opens == 1:
            raise OSError("not there yet")

    def read(self, timeout):
        if self.read_fails:
            self.read_fails -= 1
            raise OSError("unplugged")
        time.sleep(timeout)
        return b""

    def write(self, data):
        pass

    def close(self):
        pass


class ConsolePortTest(unittest.TestCase):
    def test_reconnect(self):
        port = FlakyPort()
        c = Console(port)
        c.start()
        self.addCleanup(c.stop)
        deadline = time.monotonic() + 5
        while not (port.opens >= 3 and c.connected) and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertEqual(port.opens, 3)
        self.assertTrue(c.connected)
        c.write(b"x")

    def test_not_connected(self):
        c = Console(NoPort())
        c.start()
        self.addCleanup(c.stop)
        time.sleep(0.1)
        with self.assertRaises(Unavailable):
            c.write(b"x")
        self.assertIn("no console", c.error)

    def test_bad_settings(self):
        with self.assertRaises(ValueError):
            SerialPort("/dev/ttyUSB0", 12345)
        with self.assertRaises(ValueError):
            Console(NoPort(), line_ending="nl")


class ServerSetupTest(unittest.TestCase):
    def test_power_backends(self):
        cfg = config.load(None)
        p = cfg["power"]
        self.assertIsInstance(server_main.make_power(p, None), power.GpioPower)
        for backend, cls in (("sdmux", power.SdmuxGpioPower), ("command", power.CommandPower),
                             ("none", power.NoPower)):
            p["backend"] = backend
            self.assertIsInstance(server_main.make_power(p, None), cls)
        p["backend"] = "usb"
        with self.assertRaises(ValueError):
            server_main.make_power(p, None)

    def test_build(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp)
        path = os.path.join(tmp, "b.ini")
        write(path, "[server]\nname = bench2\nport = 8081\nrun_dir = {0}/{{name}}\nlog_dir = {0}/log\n"
                    "[console]\ndevice = none\n[power]\nbackend = none\n".format(tmp))
        dut, sessions, address, prompt = server_main.build(config.load(path))
        self.addCleanup(dut.console.stop)
        self.assertEqual((dut.name, address, prompt), ("bench2", ("0.0.0.0", 8081), "[#$>] $"))
        self.assertIsInstance(dut.console.port, NoPort)
        self.assertEqual(dut.storage.mount_dir, os.path.join(tmp, "bench2", "mnt"))
        self.assertEqual(sessions.default_timeout, 300)
        self.assertTrue(os.path.islink(os.path.join(tmp, "log", "latest.log")))

    def test_main_errors_and_probe(self):
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(server_main.main(["--config", "/nonexistent/booboot.ini"]), 1)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(server_main.main(["--probe"]), 0)
        for title in ("GPIO chips:", "USB-SD-Mux:", "Serial ports:"):
            self.assertIn(title, out.getvalue())


if __name__ == "__main__":
    unittest.main()

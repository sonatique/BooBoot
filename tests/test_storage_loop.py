"""Storage code on real block devices (loop devices) and on a fake sysfs.

The loop tests need root, losetup and mkfs.ext4. They are skipped otherwise.
"""

import gzip
import io
import os
import shutil
import struct
import subprocess
import tempfile
import time
import unittest

import common  # noqa: F401  (sets sys.path)
from booboot_server import storage
from booboot_server.errors import BadRequest, HardwareError, NotFound


def mbr(parts):
    """Return an MBR sector with (start sector, sector count, type) partitions."""
    sector = bytearray(512)
    for i, (start, count, ptype) in enumerate(parts):
        off = 446 + 16 * i
        sector[off + 4] = ptype
        struct.pack_into("<II", sector, off + 8, start, count)
    sector[510:512] = b"\x55\xaa"
    return bytes(sector)


class SysfsPartitions(unittest.TestCase):
    """Partition list read from a fake /sys/class/block."""

    def test_card_info(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp)
        disk = os.path.join(tmp, "sdz")
        files = {"size": "62333952"}
        files.update({"sdz1/partition": "1", "sdz1/start": "8192", "sdz1/size": "524288"})
        files.update({"sdz2/partition": "2", "sdz2/start": "532480", "sdz2/size": "61801472"})
        for name, value in files.items():
            path = os.path.join(disk, name)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w") as f:
                f.write(value + "\n")
        os.makedirs(os.path.join(disk, "queue"))
        st = storage.Storage(lambda: "/dev/sdz", "")
        st.sys_block = tmp
        info = st.card_info()
        self.assertEqual(info["size"], 62333952 * 512)
        parts = [(p["number"], p["device"], p["start"], p["size"]) for p in info["partitions"]]
        self.assertEqual(parts, [(1, "/dev/sdz1", 8192 * 512, 524288 * 512),
                                 (2, "/dev/sdz2", 532480 * 512, 61801472 * 512)])
        self.assertEqual(st._partition_device(2), "/dev/sdz2")
        with self.assertRaises(NotFound):
            st._partition_device(3)


class WholeDiskStorage(storage.Storage):
    """Storage whose partition 1 is the whole device (no partition table)."""

    def _partition_device(self, number):
        if number != 1:
            raise NotFound("no partition %d" % number)
        return self.device()


CAN_RUN = (hasattr(os, "geteuid") and os.geteuid() == 0 and shutil.which("losetup")
           and shutil.which("mkfs.ext4") and os.path.exists("/dev/loop-control"))


@unittest.skipUnless(CAN_RUN, "needs root, losetup and mkfs.ext4")
class LoopStorage(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp)
        self.img = os.path.join(self.tmp, "card.img")
        with open(self.img, "wb") as f:
            f.write(mbr([(2048, 30720, 0x83)]))
            f.truncate(32 << 20)
        self.loop = subprocess.check_output(["losetup", "-f", "--show", "-P", self.img], text=True).strip()
        self.addCleanup(subprocess.call, ["losetup", "-d", self.loop])
        self.st = storage.Storage(lambda: self.loop, os.path.join(self.tmp, "mnt"))

    def test_write_image(self):
        image = os.urandom(1 << 20) + bytes(7 << 20)
        r = self.st.write_image(io.BytesIO(gzip.compress(image)), verify=True)
        self.assertTrue(r["verified"])
        self.assertEqual((r["bytes"], r["compression"]), (len(image), "gz"))
        with open(self.img, "rb") as f:
            self.assertEqual(f.read(len(image)), image)
        self.st.wait_ready(timeout=5)

    def test_image_too_large(self):
        with self.assertRaises(BadRequest):
            self.st.write_image(io.BytesIO(b"\1" * (33 << 20)))

    def test_partitions(self):
        # Some kernels (like minimal VM kernels) do not read MBR tables. Where they do, udev can make
        # the kernel read the table again after losetup, which removes the partitions for a moment.
        name = os.path.basename(self.loop)
        entry = os.path.join(self.st.sys_block, name, name + "p1")
        deadline = time.monotonic() + 5
        seen, parts = False, []
        while not parts and time.monotonic() < deadline:
            seen = seen or os.path.exists(entry)
            parts = self.st.card_info()["partitions"]
            if not parts:
                time.sleep(0.05)
        if not seen and not parts:
            self.skipTest("kernel does not show loop partitions")
        [part] = parts
        self.assertEqual((part["number"], part["start"], part["size"]), (1, 2048 * 512, 30720 * 512))

    def test_mount_and_files(self):
        subprocess.check_call(["mkfs.ext4", "-q", "-F", "-L", "rootfs", self.loop])
        st = WholeDiskStorage(lambda: self.loop, os.path.join(self.tmp, "mnt"))
        with st.mounted(1, write=True) as root:
            storage.put_file(root, "/boot/BOOT.BIN", io.BytesIO(b"x" * 5000))
            os.symlink("/etc", os.path.join(root, "etc-link"))
            # The device is mounted: writing an image must fail.
            with self.assertRaises(HardwareError):
                st.write_image(io.BytesIO(b"\1" * 4096))
        self.assertFalse(os.path.ismount(st.mount_dir))
        with st.mounted(1) as root:
            kind, entries = storage.open_path(root, "/boot")
            self.assertEqual([(e["name"], e["size"]) for e in entries], [("BOOT.BIN", 5000)])
            kind, f = storage.open_path(root, "/boot/BOOT.BIN")
            with f:
                self.assertEqual(f.read(), b"x" * 5000)
            with self.assertRaises(BadRequest):
                storage.open_path(root, "/etc-link/passwd")
            with self.assertRaises(OSError):
                storage.put_file(root, "/boot/new", io.BytesIO(b""))  # read only mount
        with st.mounted(1, write=True) as root:
            storage.delete(root, "/boot", recursive=True)
            storage.delete(root, "/etc-link")
            self.assertEqual(sorted(os.listdir(root)), ["lost+found"])
        self.assertFalse(os.path.ismount(st.mount_dir))


if __name__ == "__main__":
    unittest.main()

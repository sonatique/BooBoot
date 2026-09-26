"""SD card access from the board: write images, list partitions, edit files."""

from __future__ import annotations

import bz2
import contextlib
import errno
import fcntl
import hashlib
import logging
import lzma
import os
import posixpath
import shutil
import stat
import subprocess
import time
import zlib

from .errors import BadRequest, Conflict, HardwareError, NotFound

log = logging.getLogger(__name__)

CHUNK = 1 << 20

# ioctl numbers from <linux/fs.h>
BLKRRPART = 0x125F
BLKFLSBUF = 0x1261

MAGIC = (
    (b"\x1f\x8b", "gz"),
    (b"\xfd7zXZ\x00", "xz"),
    (b"BZh", "bz2"),
    (b"\x28\xb5\x2f\xfd", "zst"),
)
COMPRESSIONS = ("auto", "none", "gz", "xz", "bz2", "zst")


def detect_compression(head):
    for magic, kind in MAGIC:
        if head.startswith(magic):
            return kind
    return "none"


class Inflater:
    """Streaming decompression with a bounded output size per step."""

    def __init__(self, kind):
        if kind not in COMPRESSIONS or kind == "auto":
            raise BadRequest("unknown compression %r" % kind)
        self.kind = kind
        self._new()

    def _new(self):
        self._busy = False
        if self.kind == "gz":
            self._d = zlib.decompressobj(wbits=31)
        elif self.kind == "xz":
            self._d = lzma.LZMADecompressor()
        elif self.kind == "bz2":
            self._d = bz2.BZ2Decompressor()
        elif self.kind == "zst":
            try:
                from compression import zstd  # Python 3.14 or later
            except ImportError:
                raise BadRequest("zstd needs Python 3.14 on the server, use xz, gz or bz2") from None
            self._d = zstd.ZstdDecompressor()

    def feed(self, data):
        """Yield the decompressed chunks for this input."""
        if self.kind == "none":
            if data:
                yield data
            return
        while data or self._busy:
            if not self._busy and not data.strip(b"\0"):
                return  # padding between or after streams
            self._busy = True
            try:
                out = self._d.decompress(data, CHUNK)
            except Exception as e:
                raise BadRequest("bad %s data: %s" % (self.kind, e)) from e
            if self.kind == "gz":
                data = self._d.unconsumed_tail
                more = bool(data) or len(out) == CHUNK
            else:
                data = b""
                more = not self._d.needs_input
            if out:
                yield out
            if self._d.eof:
                # The next stream, if any, starts in unused_data.
                data = self._d.unused_data
                self._new()
                continue
            if not more:
                return

    def finish(self):
        """Check that the last stream is complete."""
        if self._busy and not self._d.eof:
            raise BadRequest("%s data is truncated" % self.kind)


def _run(args, timeout=120):
    try:
        p = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError as e:
        raise HardwareError("%s not found" % args[0]) from e
    except subprocess.TimeoutExpired as e:
        raise HardwareError("%s timed out" % args[0]) from e
    if p.returncode != 0:
        raise HardwareError("%s failed: %s" % (args[0], (p.stderr or p.stdout).strip()))
    return p.stdout


def _read(*path):
    try:
        with open(os.path.join(*path)) as f:
            return f.read().strip()
    except OSError:
        return ""


def _blkid(dev):
    try:
        p = subprocess.run(["blkid", "-p", "-o", "export", dev], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return {}
    return dict(line.split("=", 1) for line in p.stdout.splitlines() if "=" in line)


def _is_block(path):
    try:
        return stat.S_ISBLK(os.stat(path).st_mode)
    except OSError:
        return False


def _ioctl(fd, request):
    try:
        fcntl.ioctl(fd, request)
    except OSError:
        pass


def _write_all(fd, data):
    view = memoryview(data)
    while view:
        n = os.write(fd, view)
        view = view[n:]


class Storage:
    """The card as seen by the board when the mux is on the host side."""

    sys_block = "/sys/class/block"

    def __init__(self, device_fn, mount_dir):
        self._device_fn = device_fn
        self.mount_dir = mount_dir

    def device(self):
        dev = self._device_fn()
        if not dev:
            raise HardwareError("no block device found for the SD card")
        return dev

    def wait_ready(self, timeout=20):
        """Wait until the card can be read, after the mux switched to the host."""
        deadline = time.monotonic() + timeout
        last = "no block device"
        while True:
            try:
                dev = self.device()
                # Opening the device also makes the kernel look for a new card.
                fd = os.open(dev, os.O_RDONLY | os.O_CLOEXEC)
                try:
                    size = os.lseek(fd, 0, os.SEEK_END)
                finally:
                    os.close(fd)
                if size > 0:
                    self._wait_nodes(dev)
                    return
                last = "card has size 0"
            except HardwareError as e:
                last = e.message
            except OSError as e:
                last = e.strerror
            if time.monotonic() > deadline:
                raise HardwareError("SD card not ready: %s" % last)
            time.sleep(0.25)

    def _partition_names(self, dev):
        sysdir = os.path.join(self.sys_block, os.path.basename(os.path.realpath(dev)))
        names = []
        if os.path.isdir(sysdir):
            for entry in sorted(os.listdir(sysdir)):
                if os.path.exists(os.path.join(sysdir, entry, "partition")):
                    names.append(entry)
        return sysdir, names

    def _wait_nodes(self, dev, timeout=5):
        """Wait for udev to create the partition device nodes."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            names = self._partition_names(dev)[1]
            if all(os.path.exists("/dev/" + n) for n in names):
                return
            time.sleep(0.1)

    def _partition_device(self, number):
        sysdir, names = self._partition_names(self.device())
        for name in names:
            if _read(sysdir, name, "partition") == str(number):
                return "/dev/" + name
        raise NotFound("no partition %d on the SD card" % number)

    def card_info(self):
        dev = self.device()
        sysdir, names = self._partition_names(dev)
        parts = []
        for name in names:
            info = _blkid("/dev/" + name)
            parts.append({
                "number": int(_read(sysdir, name, "partition") or 0),
                "device": "/dev/" + name,
                "start": int(_read(sysdir, name, "start") or 0) * 512,
                "size": int(_read(sysdir, name, "size") or 0) * 512,
                "type": info.get("TYPE", ""),
                "label": info.get("LABEL", ""),
            })
        parts.sort(key=lambda p: p["number"])
        size = int(_read(sysdir, "size") or 0) * 512
        return {"device": dev, "size": size, "partitions": parts}

    @contextlib.contextmanager
    def mounted(self, number, write=False):
        """Mount a partition for the time of the block. Yield the mount path."""
        dev = self._partition_device(number)
        mnt = self.mount_dir
        os.makedirs(mnt, exist_ok=True)
        if os.path.ismount(mnt):
            _run(["umount", mnt])
        _run(["mount", "-o", "rw,noatime" if write else "ro", dev, mnt])
        try:
            yield mnt
        finally:
            try:
                _run(["umount", mnt])
            except HardwareError as e:
                log.warning("%s, doing a lazy unmount", e.message)
                _run(["umount", "-l", mnt])

    def write_image(self, body, compression="auto", verify=False, progress=None):
        """Write an image read from body (an object with read(n)) to the card."""
        if compression not in COMPRESSIONS:
            raise BadRequest("compression must be one of: " + ", ".join(COMPRESSIONS))
        dev = self.device()
        block = _is_block(dev)
        start = time.monotonic()
        data = body.read(CHUNK)
        kind = detect_compression(data) if compression == "auto" else compression
        inflater = Inflater(kind)
        sha = hashlib.sha256()
        written = 0
        # O_EXCL on a block device fails when it is mounted or in use.
        flags = os.O_WRONLY | os.O_CLOEXEC | (os.O_EXCL if block else os.O_CREAT | os.O_TRUNC)
        try:
            fd = os.open(dev, flags, 0o644)
        except OSError as e:
            raise HardwareError("cannot open %s: %s" % (dev, e.strerror)) from e
        try:
            capacity = os.lseek(fd, 0, os.SEEK_END) if block else None
            os.lseek(fd, 0, os.SEEK_SET)
            while data:
                for out in inflater.feed(data):
                    if capacity is not None and written + len(out) > capacity:
                        raise BadRequest("image is larger than the SD card (%d bytes)" % capacity)
                    _write_all(fd, out)
                    sha.update(out)
                    written += len(out)
                    if progress:
                        progress(written)
                data = body.read(CHUNK)
            inflater.finish()
            os.fsync(fd)
            if block:
                _ioctl(fd, BLKFLSBUF)
        finally:
            os.close(fd)
        result = {"bytes": written, "sha256": sha.hexdigest(), "compression": kind, "verified": False}
        if verify:
            if self._hash(dev, written) != result["sha256"]:
                raise HardwareError("verify failed: the SD card content differs from the image")
            result["verified"] = True
        if block:
            self._reread(dev)
        result["seconds"] = round(time.monotonic() - start, 1)
        return result

    def _hash(self, dev, length):
        """Read back the first length bytes, bypassing the cache, and hash them."""
        fd = os.open(dev, os.O_RDONLY | os.O_CLOEXEC)
        try:
            if _is_block(dev):
                _ioctl(fd, BLKFLSBUF)
            if hasattr(os, "posix_fadvise"):
                os.posix_fadvise(fd, 0, 0, os.POSIX_FADV_DONTNEED)
            sha = hashlib.sha256()
            left = length
            while left:
                data = os.read(fd, min(CHUNK, left))
                if not data:
                    break
                sha.update(data)
                left -= len(data)
            return sha.hexdigest()
        finally:
            os.close(fd)

    def _reread(self, dev):
        """Make the kernel read the new partition table."""
        fd = os.open(dev, os.O_RDONLY | os.O_CLOEXEC)
        try:
            _ioctl(fd, BLKRRPART)
        finally:
            os.close(fd)
        self._wait_nodes(dev)


# File operations on a mounted partition. Paths use "/" and are relative to
# the partition root.

def _inside(root, path):
    return path == root or path.startswith(root + os.sep)


def resolve(root, path):
    """Return the local path of a card path. Refuse paths that leave root."""
    rel = posixpath.normpath("/" + path).lstrip("/")
    full = os.path.join(root, *rel.split("/")) if rel else root
    real_root = os.path.realpath(root)
    parent = os.path.realpath(os.path.dirname(full)) if rel else real_root
    if not _inside(real_root, parent):
        raise BadRequest("path leaves the partition: %s" % path)
    return full


def list_dir(full):
    entries = []
    with os.scandir(full) as it:
        for e in it:
            st = e.stat(follow_symlinks=False)
            if stat.S_ISDIR(st.st_mode):
                kind = "dir"
            elif stat.S_ISREG(st.st_mode):
                kind = "file"
            elif stat.S_ISLNK(st.st_mode):
                kind = "link"
            else:
                kind = "other"
            entries.append({"name": e.name, "type": kind, "size": st.st_size, "mtime": int(st.st_mtime)})
    entries.sort(key=lambda x: x["name"])
    return entries


def open_path(root, path):
    """Return ("dir", entries) or ("file", open binary file)."""
    real = os.path.realpath(resolve(root, path))
    if not _inside(os.path.realpath(root), real):
        raise BadRequest("path leaves the partition: %s" % path)
    if os.path.isdir(real):
        return "dir", list_dir(real)
    if not os.path.isfile(real):
        raise NotFound("not found: %s" % path)
    return "file", open(real, "rb")


def put_file(root, path, body):
    """Write a file from body, through a temporary file. Return its size."""
    full = resolve(root, path)
    if full == root or path.endswith("/"):
        raise BadRequest("a file name is needed")
    if os.path.isdir(full) and not os.path.islink(full):
        raise Conflict("is a directory: %s" % path)
    parent = os.path.dirname(full)
    os.makedirs(parent, exist_ok=True)
    tmp = os.path.join(parent, ".booboot-upload.tmp")
    size = 0
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW | os.O_CLOEXEC, 0o644)
        try:
            while True:
                data = body.read(CHUNK)
                if not data:
                    break
                _write_all(fd, data)
                size += len(data)
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(tmp, full)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise
    return size


def make_dir(root, path):
    os.makedirs(resolve(root, path), exist_ok=True)


def delete(root, path, recursive=False):
    full = resolve(root, path)
    if full == root:
        raise BadRequest("cannot delete the partition root")
    if os.path.islink(full) or not os.path.isdir(full):
        try:
            os.unlink(full)
        except FileNotFoundError:
            raise NotFound("not found: %s" % path) from None
    elif recursive:
        shutil.rmtree(full)
    else:
        try:
            os.rmdir(full)
        except OSError as e:
            if e.errno == errno.ENOTEMPTY:
                raise Conflict("directory not empty: %s (use recursive)" % path) from None
            raise

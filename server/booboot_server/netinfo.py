"""Name and addresses of this board, for clients where its .local name does not work, like over a VPN."""

from __future__ import annotations

import fcntl
import socket
import struct
import threading
import time

SIOCGIFADDR = 0x8915
# Container and bridge interfaces are not reachable from other computers.
SKIP = ("docker", "br-", "veth")
MAX_AGE = 30  # seconds

_lock = threading.Lock()
_cache = (0.0, None)


def _skipped(name):
    return name == "lo" or name.startswith(SKIP)


def _ipv4(name):
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        try:
            data = fcntl.ioctl(s.fileno(), SIOCGIFADDR, struct.pack("256s", name[:15].encode()))
        except OSError:
            return None
    address = socket.inet_ntoa(data[20:24])
    return None if address.startswith(("127.", "169.254.")) else address


def _ipv6(path="/proc/net/if_inet6"):
    """Global IPv6 addresses."""
    found = []
    try:
        with open(path) as f:
            for line in f:
                fields = line.split()
                if len(fields) == 6 and fields[3] == "00" and not _skipped(fields[5]):
                    found.append(socket.inet_ntop(socket.AF_INET6, bytes.fromhex(fields[0])))
    except (OSError, ValueError):
        pass
    return found


def addresses():
    """IPv4 addresses of the network interfaces, then the global IPv6 ones."""
    try:
        names = [name for _, name in socket.if_nameindex() if not _skipped(name)]
    except OSError:
        names = []
    return [a for a in map(_ipv4, names) if a] + _ipv6()


def info():
    """{"hostname", "addresses"}, at most MAX_AGE seconds old."""
    global _cache
    with _lock:
        when, value = _cache
        if value is None or time.monotonic() - when > MAX_AGE:
            value = {"hostname": socket.gethostname(), "addresses": addresses()}
            _cache = (time.monotonic(), value)
        return value

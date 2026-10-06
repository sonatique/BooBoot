"""One client at a time. A session is a lease that ends when its client is idle too long.

Clients that stay connected, like the viewers, send a heartbeat while they hold
the session. When it stops, their client is gone: the session can be taken
over with force "gone", which takes it only from such a client.
"""

from __future__ import annotations

import secrets
import threading
import time

from .errors import BadRequest, Busy, NoSession

GONE_AFTER = 30  # seconds without heartbeat


def _same(a, b):
    return secrets.compare_digest(a.encode("utf-8"), b.encode("utf-8"))


class Sessions:
    def __init__(self, default_timeout=300, max_timeout=3600):
        self.default_timeout = default_timeout
        self.max_timeout = max_timeout
        self._lock = threading.Lock()
        self._s = None

    def _expire(self):
        s = self._s
        if s and s["active"] == 0 and time.monotonic() - s["seen"] > s["timeout"]:
            self._s = None

    def _info(self, s):
        now = time.monotonic()
        idle = 0.0 if s["active"] else now - s["seen"]
        beat = None if s["beat"] is None else now - s["beat"]
        return {
            "client": s["client"],
            "opened": s["opened"],
            "timeout": s["timeout"],
            "idle": round(idle, 1),
            "expires_in": round(max(0.0, s["timeout"] - idle), 1),
            # None: the client sends no heartbeat.
            "alive": None if beat is None else beat <= GONE_AFTER,
            "heartbeat_age": None if beat is None else round(beat, 1),
        }

    def _busy(self):
        return Busy("the DUT is used by another client", session=self._info(self._s))

    def _check(self, token):
        self._expire()
        s = self._s
        if s and token and _same(token, s["token"]):
            return s
        if s:
            raise self._busy()
        raise NoSession("no session, or it expired: open one with POST /api/v1/session")

    def open(self, client, timeout=None, force=False):
        """Start a session. Return (token, info).

        force: True takes the session from any client, "gone" only from a client that is gone.
        """
        if timeout is None:
            timeout = self.default_timeout
        elif not 1 <= timeout <= self.max_timeout:
            raise BadRequest("timeout must be between 1 and %g seconds" % self.max_timeout)
        with self._lock:
            self._expire()
            if self._s and not (force is True or (force == "gone" and self._info(self._s)["alive"] is False)):
                raise self._busy()
            s = {
                "token": secrets.token_hex(16),
                "client": client,
                "opened": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "timeout": timeout,
                "seen": time.monotonic(),
                "beat": None,
                "active": 0,
            }
            self._s = s
            return s["token"], self._info(s)

    def close(self, token):
        with self._lock:
            self._check(token)
            self._s = None

    def check(self, token):
        with self._lock:
            self._check(token)

    def heartbeat(self, token):
        """The client of the session is still there. Does not count as activity."""
        with self._lock:
            s = self._check(token)
            s["beat"] = time.monotonic()
            info = self._info(s)
            info["active"] = True
            info["yours"] = True
            return info

    def begin(self, token):
        """Mark the start of a request. The session does not expire while requests run."""
        with self._lock:
            s = self._check(token)
            s["active"] += 1
            s["seen"] = time.monotonic()

    def end(self, token):
        with self._lock:
            s = self._s
            if s and s["token"] == token:
                s["active"] -= 1
                s["seen"] = time.monotonic()

    def status(self, token=""):
        with self._lock:
            self._expire()
            s = self._s
            if not s:
                return {"active": False}
            info = self._info(s)
            info["active"] = True
            info["yours"] = bool(token) and _same(token, s["token"])
            return info

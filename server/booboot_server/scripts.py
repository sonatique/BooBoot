"""Python scripts sent by clients, run on this board.

A script runs in the session of the client that started it, as a user
without access to the hardware: it uses the DUT through the HTTP API, with
that session. Its output is kept on disk, and read from a cursor like the
console, while it runs or after it ended. One script runs at a time.
"""

from __future__ import annotations

import json
import logging
import os
import pwd
import re
import shutil
import signal
import subprocess
import sys
import threading
import time

from .errors import ApiError, BadRequest, Conflict, NotFound, Unavailable

log = logging.getLogger(__name__)

MAX_SOURCE = 512 << 10
STOP_GRACE = 5  # seconds between SIGTERM and SIGKILL
NAME = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,63}\.py$")

# Runs as the user of the script: sets its limits, then becomes the script.
BOOT = """\
import os, resource, sys
data, fsize = int(sys.argv[1]), int(sys.argv[2])
resource.setrlimit(resource.RLIMIT_DATA, (data, data))
resource.setrlimit(resource.RLIMIT_FSIZE, (fsize, fsize))
os.nice(10)
os.execv(sys.executable, [sys.executable, "-u"] + sys.argv[3:])
"""


def library():
    """Directory of the booboot client module: next to the server package once installed, else in the repository."""
    here = os.path.dirname(os.path.abspath(__file__))
    for d in (os.path.dirname(here), os.path.join(os.path.dirname(os.path.dirname(here)), "client")):
        if os.path.exists(os.path.join(d, "booboot.py")):
            return d
    return ""


def _now():
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def _complete(data):
    """data without a UTF-8 character cut at its end."""
    for n in range(1, min(4, len(data)) + 1):
        b = data[-n]
        if b < 0x80:
            return data
        if b >= 0xC0:
            size = 2 if b < 0xE0 else 3 if b < 0xF0 else 4
            return data[:-n] if size > n else data
    return data


def _kill_user(uid):
    """Kill the processes of the user of the scripts, like those a script left running outside its group."""
    for pid in os.listdir("/proc"):
        if pid.isdigit():
            try:
                if os.stat("/proc/" + pid).st_uid == uid:
                    os.kill(int(pid), signal.SIGKILL)
            except OSError:
                pass


def _signal(proc, sig):
    """Signal the process group of a script that still runs."""
    if proc.returncode is None:
        try:
            os.killpg(proc.pid, sig)
        except OSError:
            pass


class Output:
    """Output of a script in a file, of which the last `size` bytes at least are kept.

    Bytes have cursors from the start of the script, like the console.
    """

    def __init__(self, path, size, base=0):
        self.path = path
        self.size = size
        self.base = base  # cursor of the first byte of the file
        try:
            self.end = base + os.path.getsize(path)
        except OSError:
            self.end = base
        self.done = True
        self._file = None
        self._cond = threading.Condition()

    def open(self):
        self._file = os.fdopen(os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600), "ab")
        self.done = False

    def append(self, data):
        with self._cond:
            if self._file is None:
                return
            self._file.write(data)
            self._file.flush()
            self.end += len(data)
            if self.end - self.base > 2 * self.size:
                self._trim()
            self._cond.notify_all()

    def _trim(self):
        self._file.close()
        with open(self.path, "rb") as f:
            f.seek(-self.size, os.SEEK_END)
            data = f.read()
        tmp = self.path + ".tmp"
        with open(tmp, "wb") as f:
            f.write(data)
        os.chmod(tmp, 0o600)
        os.replace(tmp, self.path)
        self.base = self.end - len(data)
        self._file = open(self.path, "ab")

    def close(self):
        with self._cond:
            if self._file:
                self._file.close()
                self._file = None
            self.done = True
            self._cond.notify_all()

    def read(self, since, max_bytes, wait):
        """Return (start, data) from cursor since, negative: bytes before the end. Waits for data while running."""
        deadline = time.monotonic() + wait
        with self._cond:
            if since < 0:
                since = self.end + since
            since = max(0, since)
            while self.end <= max(since, self.base) and not self.done:
                left = deadline - time.monotonic()
                if left <= 0:
                    break
                self._cond.wait(left)
            start = min(max(since, self.base), self.end)
            with open(self.path, "rb") as f:
                f.seek(start - self.base)
                data = f.read(min(max_bytes, self.end - start))
            return start, data if self.done else _complete(data)


class Job:
    def __init__(self, directory, info, output_size):
        self.dir = directory
        self.info = info
        self.output = Output(os.path.join(directory, "output"), output_size, info.get("output_base", 0))
        self.proc = None
        self.token = None
        self.ids = None
        self.watcher = None
        self.started = time.monotonic()
        self.stop_reason = ""

    def public(self):
        info = dict(self.info, output=self.output.end)
        info.pop("output_base", None)
        if info["state"] == "running":
            info["time"] = round(time.monotonic() - self.started, 1)
        return info

    def save(self):
        self.info["output_base"] = self.output.base
        tmp = os.path.join(self.dir, "info.json.tmp")
        with open(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as f:
            json.dump(self.info, f, indent=1)
        os.replace(tmp, os.path.join(self.dir, "info.json"))


class Scripts:
    def __init__(self, directory, sessions, enabled=False, user="", max_time=86400, memory=512 << 20,
                 output=10 << 20, keep=20, file_size=100 << 20):
        self.dir = directory
        self.sessions = sessions
        self.enabled = enabled
        self.user = user
        self.max_time = max_time
        self.memory = memory
        self.output_size = output
        self.keep = keep
        self.file_size = file_size
        self.url = ""  # of this server, for the scripts; set once it listens
        self._lock = threading.Lock()
        self._jobs = {}
        self._running = None
        self._next = 1
        self._load()
        sessions.on_takeover = self._taken_over

    def _load(self):
        """Scripts of earlier runs of the server. Those that were running stopped with it."""
        try:
            names = [n for n in os.listdir(self.dir) if n.isdigit()]
        except OSError:
            return
        self._next = max(map(int, names), default=0) + 1
        for name in names:
            d = os.path.join(self.dir, name)
            try:
                os.chmod(d, 0o700)
                with open(os.path.join(d, "info.json")) as f:
                    info = json.load(f)
            except (OSError, ValueError):
                continue
            job = Job(d, info, self.output_size)
            if info.get("state") == "running":
                info.update(state="stopped", reason="server restarted")
                job.save()
            self._jobs[int(name)] = job

    def _ids(self):
        """(uid, gid) to run scripts as, or None to run them as the server."""
        if not self.user or os.geteuid() != 0:
            return None
        try:
            pw = pwd.getpwnam(self.user)
        except KeyError:
            raise Unavailable("the user %s for scripts does not exist: run server/install.sh again"
                              % self.user) from None
        return pw.pw_uid, pw.pw_gid

    def start(self, token, client, source, name, args, timeout=None):
        """Start a script in the session of token. Return its information."""
        if not self.enabled:
            raise Unavailable("scripts are off on this unit: set enabled = yes in the [scripts] section of its "
                              "configuration to allow them", code="scripts_off")
        if not isinstance(source, str) or not source:
            raise BadRequest("source is required: the text of the script")
        if len(source.encode("utf-8")) > MAX_SOURCE:
            raise BadRequest("the script is larger than %d KB" % (MAX_SOURCE >> 10))
        if not NAME.match(name) or name == "booboot.py":
            raise BadRequest("name must be a file name ending in .py, other than booboot.py")
        if not isinstance(args, list) or not all(isinstance(a, str) for a in args):
            raise BadRequest("args must be a list of strings")
        if timeout is None:
            timeout = self.max_time
        elif not 1 <= timeout <= self.max_time:
            raise BadRequest("timeout must be between 1 and %g seconds" % self.max_time)
        ids = self._ids()
        with self._lock:
            if self._running:
                job = self._running
                raise Conflict("script %d (%s) is running" % (job.info["id"], job.info["name"]),
                               code="script_running", script=job.public())
            self.sessions.hold(token, name)
            try:
                job = self._spawn(token, client, source, name, args, timeout, ids)
            except Exception:
                self.sessions.release(token)
                raise
            self._jobs[job.info["id"]] = job
            self._running = job
        log.info("script %d (%s) started by %s", job.info["id"], name, client)
        job.watcher = threading.Thread(target=self._watch, args=(job, timeout), daemon=True)
        job.watcher.start()
        return job.public()

    def _spawn(self, token, client, source, name, args, timeout, ids):
        sid = self._next
        self._next += 1
        d = os.path.join(self.dir, str(sid))
        work = os.path.join(d, "work")
        info = {"id": sid, "name": name, "args": args, "client": client, "state": "running", "exit_code": None,
                "reason": "", "started": _now(), "ended": None, "time": 0.0, "timeout": timeout}
        job = Job(d, info, self.output_size)
        # Temporary files go to its own directory, deleted with it.
        env = {"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": work, "TMPDIR": work, "LANG": "C.UTF-8",
               "PYTHONPATH": library(), "BOOBOOT_URL": self.url, "BOOBOOT_SESSION": token,
               "BOOBOOT_SCRIPT": str(sid)}
        user = {"user": ids[0], "group": ids[1], "extra_groups": []} if ids else {}
        path = os.path.join(d, name)
        cmd = [sys.executable, "-c", BOOT, str(self.memory), str(self.file_size), path] + args
        try:
            os.makedirs(work)
            # The script reaches its own files by name, and lists no other.
            os.chmod(d, 0o711)
            with open(path, "w", encoding="utf-8") as f:
                f.write(source)
            os.chmod(path, 0o644)
            if ids:
                os.chown(work, *ids)
            os.chmod(work, 0o700)
            job.output.open()
            job.proc = subprocess.Popen(cmd, cwd=work, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                        stderr=subprocess.STDOUT, start_new_session=True, **user)
        except OSError as e:
            job.output.close()
            shutil.rmtree(d, ignore_errors=True)
            raise ApiError("cannot start the script: %s" % e) from None
        job.token = token
        job.ids = ids
        job.save()
        return job

    def _watch(self, job, timeout):
        timer = threading.Timer(timeout, self._stop, (job, "time limit of %gs" % timeout))
        timer.daemon = True
        timer.start()
        reader = threading.Thread(target=self._read, args=(job,), daemon=True)
        reader.start()
        code = job.proc.wait()
        timer.cancel()
        # Processes that the script left behind end with it.
        try:
            os.killpg(job.proc.pid, signal.SIGKILL)
        except OSError:
            pass
        # Only for a system user: never the processes of a person.
        if job.ids and 0 < job.ids[0] < 1000:
            _kill_user(job.ids[0])
        reader.join(STOP_GRACE)
        # Later scripts cannot read its files.
        try:
            os.chmod(job.dir, 0o700)
        except OSError:
            pass
        with self._lock:
            job.info.update(state="stopped" if job.stop_reason else "exited", exit_code=code,
                            reason=job.stop_reason, ended=_now(), time=round(time.monotonic() - job.started, 1))
            job.output.close()
            job.save()
            self._running = None
            self.sessions.release(job.token)
            self._prune()
        log.info("script %d (%s) %s, exit code %d%s", job.info["id"], job.info["name"], job.info["state"], code,
                 ": " + job.stop_reason if job.stop_reason else "")

    def _read(self, job):
        f = job.proc.stdout
        while True:
            data = f.read1(65536)
            if not data:
                break
            job.output.append(data)
        f.close()

    def _stop(self, job, reason):
        with self._lock:
            if job.info["state"] != "running" or job.stop_reason:
                return
            job.stop_reason = reason
        _signal(job.proc, signal.SIGTERM)
        timer = threading.Timer(STOP_GRACE, _signal, (job.proc, signal.SIGKILL))
        timer.daemon = True
        timer.start()

    def _taken_over(self, token, client):
        job = self._running
        if job and job.token == token:
            self._stop(job, "session taken over by %s" % client)

    def _prune(self):
        """Delete the oldest ended scripts, with their files, beyond keep."""
        ended = sorted(i for i, job in self._jobs.items() if job is not self._running)
        for i in ended[:max(0, len(ended) - self.keep)]:
            shutil.rmtree(self._jobs.pop(i).dir, ignore_errors=True)

    def _job(self, sid):
        job = self._jobs.get(sid)
        if job is None:
            raise NotFound("no script %d" % sid)
        return job

    def stop(self, sid, reason):
        job = self._job(sid)
        if job.info["state"] != "running":
            raise Conflict("script %d is not running" % sid, code="script_ended", script=job.public())
        self._stop(job, reason)
        return job.public()

    def list(self):
        with self._lock:
            return [self._jobs[i].public() for i in sorted(self._jobs, reverse=True)]

    def info(self, sid):
        with self._lock:
            return self._job(sid).public()

    def running(self):
        """Information on the running script, or None."""
        with self._lock:
            return self._running.public() if self._running else None

    def read(self, sid, since, max_bytes, wait):
        """Return (start, data, info) of the output of a script."""
        with self._lock:
            job = self._job(sid)
        try:
            start, data = job.output.read(since, max_bytes, wait)
        except OSError:
            raise NotFound("no script %d" % sid) from None  # deleted meanwhile
        with self._lock:
            return start, data, job.public()

    def close(self):
        """Stop the running script, as the server stops."""
        job = self._running
        if job:
            self._stop(job, "server stopped")
            try:
                job.proc.wait(STOP_GRACE)
            except subprocess.TimeoutExpired:
                _signal(job.proc, signal.SIGKILL)
            job.watcher.join(STOP_GRACE)

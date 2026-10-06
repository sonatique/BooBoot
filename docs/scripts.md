# Scripts on the BooBoot board

A client can send a Python script to the BooBoot board and run it there. The
script goes on when the client disconnects, on purpose or because the
network dropped: its output stays on the board, to read later. Use it for
long or unattended work, like a boot loop over a night, or for work that
needs no delay between the board and the DUT.

## Turning them on

Scripts are off by default. Anyone who reaches the port of the server can
run them, and with them, programs on the BooBoot board. Turn them on only on
a trusted network. In `/etc/booboot/NAME.ini`:

```ini
[scripts]
enabled = yes
```

Then `sudo systemctl restart booboot@NAME`. The other settings of the
section, with their defaults:

| Setting | Default | Meaning |
|---|---|---|
| `user` | `booboot-script` | User that runs the scripts, created by `install.sh` |
| `dir` | `/var/lib/booboot/{name}/scripts` | Where scripts, their output and their files are kept |
| `max_time` | `86400` | Seconds a script may run, at most (24 h) |
| `memory` | `512` | MB of memory for a script |
| `output` | `10` | MB of output kept for a script: the last part |
| `file_size` | `100` | Size limit in MB of each file a script writes |
| `keep` | `20` | Number of ended scripts kept |

## Writing a script

A script uses the DUT through the same `booboot` module as a client on a
desktop computer. `booboot.Client.from_env()` is the DUT, in the session of
the client that started the script:

```python
import sys
import booboot

dut = booboot.Client.from_env()
runs = int(sys.argv[1])
for i in range(runs):
    t = dut.boot_time("login: ", timeout=120)
    print("boot %d: %s" % (i + 1, "%.3f s" % t if t is not None else "timeout"))
```

What the script prints, on its standard output or error, is its output. It
has the Python of the board (3.9 or later) with its standard library and the
`booboot` module, and no other package. It has no input.

The same script runs on a desktop computer: there, `from_env()` opens a
session on the server of `BOOBOOT_URL` (default
`http://booboot.local:8080`).

## Running a script

```sh
booboot script run boots.py 500          # start, then show the output live
booboot script run --detach boots.py 500 # only start
booboot script output --follow           # the output from the start, then live
booboot script output --since -2000      # the last 2000 bytes
booboot script list                      # the scripts kept, the newest first
booboot script stop                      # stop the running script
```

`script run` and `script output --follow` end with the script, and exit
with 0 if it exited with 0. Ctrl-C stops showing the output, not the script:
the command says how to follow it again, from where it stopped. When the
connection drops, they try again until the server answers. Options of
`script run` come before the file; what comes after the file goes to the
script. `script output` and `script stop` take a script number, by default
the latest one and the running one.

In the Python module: `run_script()`, `script_output()`, `follow_script()`,
`script()`, `scripts()` and `stop_script()`. MCP clients have the tools
`script_run`, `script_output`, `script_stop` and `script_list`. The HTTP API
is in [api.md](api.md#scripts).

## Session

A script runs in the session of the client that started it. One script runs
at a time.

- While the script runs, the session does not end after its idle time, and
  counts as connected: others see `used by NAME (running the script
  boots.py on the board)`, and cannot take it as gone.
- The client may disconnect: the script goes on.
- When the client closes the session while the script runs, the session
  ends when the script ends.
- When another client takes the session over (`--force`), the script is
  stopped.
- Stopping the script, or reaching `max_time` or the time limit given at
  start, stops it: SIGTERM, then SIGKILL 5 s later if it still runs.
- Programs started by the script end with it, even those that left its
  process group.
- A restart of the server stops the running script. It is then listed as
  stopped, "server stopped" ("server restarted" after a crash of the
  server).

## What a script can do

It runs as the user `booboot-script`, with low priority, the memory and file
size limits above, and no access to the hardware: the relay, the SD card and
the serial console are used only through the server, so the rules of the
server hold, like the SD card used only with the power off. It can use the
network of the board, for example to reach the DUT over Ethernet.

A script cannot read the files of other scripts: while it runs, it reaches
only its own directory, and once it ended, its directory is closed to all
scripts. Nobody can run a script again: each run sends its own text.

The output and the state of the scripts are open to all clients, as the
console is: anyone who reaches the server can list the scripts and read
their output and arguments, without a session. Do not print secrets, or
give them as arguments.

## Files and cleanup

Each script has a directory on the board, in `dir`, numbered like the
script:

- the script itself, as sent;
- its output;
- `work/`, its working directory, which is also its home and temporary
  directory: files it writes with relative paths, and temporary files, go
  there.

When a script ends, the oldest ended scripts beyond `keep` (20) are deleted,
with their output and all their files. A running script is never deleted.
Files that a script writes elsewhere, in a directory open to all like
`/var/tmp`, are not deleted.

With the default settings, the scripts take on the SD card of the board at
most 21 directories (20 ended scripts and a running one), each with up to
20 MB of output (the last 10 MB at least are kept) and its files, each file
up to 100 MB.

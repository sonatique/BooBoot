# BooBoot architecture

BooBoot lets a program on a desktop computer (a script, a test tool, an
agent) control an embedded board that boots from an SD card, the DUT
(device under test):

- switch the DUT power with a relay,
- change the SD card content with a USB-SD-Mux FAST from Linux Automation GmbH,
- use the DUT serial console.

A small Linux board, typically an old Raspberry Pi, sits next to the DUT and
runs the BooBoot server. Clients use it over Ethernet with plain HTTP and JSON.

```
Desktop                     BooBoot board (Raspberry Pi)                DUT
+-----------------+         +--------------------------------+
| client          |  HTTP   | booboot server (systemd)       |
|  CLI, library,  |---------|   power     GPIO    relay      |------ power input
|  curl, C#, ...  |   LAN   |   SD card   USB     SD-Mux     |------ SD card slot
+-----------------+         |   console   USB     UART       |------ debug UART
                            +--------------------------------+
```

## Hardware

**Relay.** A GPIO line of the board drives a small driver board, which drives
the power relay. At rest the relay must be off, so that the DUT stays off
while the BooBoot board boots or is down. On a Raspberry Pi, GPIO17 is pulled
down at reset: with a driver that switches on with a high input, the DUT is
off by default. For a driver that switches on with a low input, set
`active_low = yes` and use a line that is pulled up at reset.

The server switches the power off when it starts and when it stops.

Other ways to switch the power, set in `[power] backend`:
- `sdmux`: GPIO 0 or 1 of the USB-SD-Mux FAST. The BooBoot board then needs
  no GPIO at all. These outputs are open drain and are released (high, with
  a pull-up) when the mux powers up: use a relay driver that switches on with
  a low input, and set `active_low = yes`.
- `command`: any shell commands (USB relay, network power switch, ...).
- `none`: no relay.

**USB-SD-Mux FAST.** Its micro SD adapter goes into the DUT SD slot, and the
card goes into the mux. It is a USB 2.0 device; any USB port works. The
classic USB-SD-Mux works too.

**Serial console.** A USB UART adapter (FTDI, CP210x, CH340, ...) at the DUT
I/O voltage (3.3 V or 1.8 V). Default speed 921600 baud, set in the
configuration. When the DUT is off, the idle high TX line of the adapter can
feed the DUT through its I/O pins, so a power cycle is not a clean cold boot.
If that happens, add a buffer powered from the DUT side, or at least a series
resistor.

## Supported boards

The server needs:
- Linux with Python 3.9 or later (Raspberry Pi OS Bullseye or later),
- Linux 5.10 or later for the `gpio` power backend,
- USB host ports for the mux and the UART adapter, and a network link.

It uses little CPU and about 20 to 30 MB of memory.

| Board | Notes |
|---|---|
| Raspberry Pi 4, 5 | Best choice: real Gigabit Ethernet. |
| Raspberry Pi 2, 3 | Fine. Ethernet (100 Mbit/s, 3B+ about 300 Mbit/s) shares the USB 2.0 bus with the mux. |
| Raspberry Pi 1 B, B+ | Works with the 32-bit OS. xz decompression is slow on its single core: send gz or raw images, or copy files. The first model B has weak USB power: use a powered hub. |
| Raspberry Pi Zero, Zero 2 W | No Ethernet port: needs a USB hub with Ethernet, or Wi-Fi. |
| Other Linux boards | Work the same way. GPIO line names differ: see `booboot-server --probe`, or give the chip and line number. |

Copying `BOOT.BIN` and `image.ub` (tens of MB) takes seconds on any model.
Writing a full image is limited by the card and the mux (roughly 10 to 20
MB/s), and on older models also by the network.

## Server

Python 3 standard library only, plus the `usbsdmux` package from Linux
Automation GmbH (pure Python, no dependencies) to switch the mux. Also used:
`mount`, `umount` and `blkid` from util-linux.

| Module | Job |
|---|---|
| `api.py` | HTTP server (one thread per request), routes, parameters, errors |
| `session.py` | One client at a time (see Sessions) |
| `dut.py` | State and safety rules; one hardware operation at a time |
| `power.py`, `gpio.py` | Power backends; GPIO through the kernel character device (ioctl) |
| `sdmux.py` | Finds the mux in sysfs, switches it with `usbsdmux` |
| `storage.py` | Image writing, partitions, file operations |
| `console.py` | Serial port (termios), output buffer, log files, expect |
| `fake.py` | Simulated board for development and tests |
| `web/` | Console web page: HTML and JavaScript, served at `/` |
| `config.py` | INI configuration with defaults |

### Sessions

Only one client may use the DUT at a time.

- A client opens a session (`POST /api/v1/session`) and gets a token. It
  sends the token with every request (`Authorization: Bearer TOKEN`).
- Every request keeps the session alive. When the client sends nothing for
  the session timeout (default 300 s, the client may ask for another value),
  the session ends and another client can open one.
- While a session is open, other clients get HTTP 423 `busy`, with the name
  of the client that has the session, its idle time and when it expires.
  `GET /api/v1/status` always works, so a client can see that the server is
  up but busy.
- `force` takes the session from another client, for when it is known to be
  gone.
- Reading the console needs no session: viewers can watch while another
  client uses the DUT.
- A session never expires while one of its requests runs (like a long image
  write or an expect).

### Safety rules

- The SD card can be on the BooBoot board side ("host") only while the DUT
  power is off.
- SD card operations switch the card to the host side when needed, and leave
  it there. Power on switches it back to the DUT.
- One hardware operation at a time. Another one gets HTTP 409 with the name
  of the running operation.
- Only the block device of the mux is ever written. The client never gives a
  device name. The server finds the card reader of the mux in sysfs, and
  opens it with `O_EXCL`, which fails if it is mounted.

### Console

The server opens the serial port at start and reads it all the time, also
when no client is connected, so no boot output is lost. It opens the port
again if the adapter is unplugged.

Each received byte has a cursor: its position since the server started.
Clients read or wait from a cursor, given as a number or as a name:

| Value | Meaning |
|---|---|
| `boot` | Position at the last power on |
| `last` | End of the last `expect` or `run` match (set to `boot` at power on) |
| `now` | Current end of the output |
| `start` | Oldest byte still in memory |
| negative number | That many bytes before the end |

- `read`: output from a cursor, with an optional wait for new output (long
  polling). Each answer gives the `next` cursor.
- `stream`: the output and the power switches as they come, on one
  connection, for viewers.
- `write`: send text, optionally followed by the line ending (`cr` by default,
  like a terminal Enter key).
- `expect`: wait on the server for a regex after a cursor, with a timeout.
  Returns the output up to the end of the match. Waiting on the server means
  no race and no network delay.
- `run`: send a command line, wait for the prompt, and return only the
  command output. This is the most useful call for agents.

The last 8 MB (configurable) stay in memory. Each power on also starts a new
log file in `/var/log/booboot/NAME/`, with a time stamp on each line
(`latest.log` points to the current one). Clients can list and download them.

The server also serves a [web page](web.md) at `/` that shows the console
live in any browser, from the stream, with no session.

**Boot time.** The server notes the time of each power on, just after the
relay is switched, and the arrival time of each read from the serial port.
`expect` and `run` return the time from power on to their match, and `read`
can start each line with its time since power on. The times are measured on
the board, so the network has no effect on them, and they are right even
when `expect` is called after the text arrived. The command
`booboot boottime "login: " --runs 5` power cycles the DUT 5 times and gives
each boot time with the minimum, mean and maximum. Accuracy is about 20 ms
(relay closing time, USB UART adapter delay).

### SD card

- **Image**: the client sends the file as is. The server detects gz, xz or
  bz2 compression (zstd with Python 3.14) and decompresses while writing,
  computes the sha256, and can read the card back to verify it.
- **Files**: the server mounts the partition, does the operation (list, read,
  write, delete, create a directory) and unmounts it. Any file system that
  Linux can mount works: FAT32 for `BOOT.BIN` and `image.ub`, ext4 for a root
  file system, and so on. Files are written to a temporary name, then
  renamed. Paths cannot leave the partition.

## API

HTTP/1.1 with JSON, described in [api.md](api.md). Parameters can be sent as
JSON, as a query string, or as a form, so plain `curl`, PowerShell
`Invoke-RestMethod` and C# `HttpClient` are enough.

## Client

`client/booboot.py` is one file, standard library only, for Linux, macOS and
Windows. It is:
- a Python library (`booboot.Client`), and
- a command line tool (`booboot ...`), built on the library.

The command line tool is made for scripts and agents: it never asks
questions, `--json` gives machine readable output, waits have timeouts, and
exit codes tell errors (1), timeouts (3) and busy (4) apart. It opens a
session when needed and keeps the token in a local file between calls.
`booboot console attach` is an interactive terminal for humans.

`client/csharp/BooBootClient.cs` is the same client as a small C# class
(.NET 6 or later, one file, no package needed). Other languages use the HTTP
API directly.

[BooBoot Console](console.md) (`client/csharp/BooBootConsole`) is a desktop
program that shows the serial console live, with scrollback, copy, save and
log files. It is written in C# with Avalonia (.NET 10), for Windows, Linux
and macOS. It reads the console stream and needs no session.

`booboot mcp` makes the client an MCP (Model Context Protocol) server on
standard input and output: the DUT becomes a set of tools for MCP clients
(see [mcp.md](mcp.md)). It runs next to the MCP client, not on the BooBoot
board, because its tools copy local files to the SD card. It keeps one
BooBoot session for as long as it runs, and cuts long console output to fit
tool results.

## Several DUTs

One board can serve several DUTs: one server instance per DUT, each with its
own configuration file, port, relay line, mux (by serial number) and UART
adapter (by `/dev/serial/by-id/` path). The systemd template unit
`booboot@NAME` runs the instance with `/etc/booboot/NAME.ini`.

## Deployment

`server/install.sh NAME` on the board, as root:
1. creates a Python virtual environment in `/opt/booboot` with `usbsdmux`,
2. copies the server there, and installs `booboot-server` and the `booboot`
   client in `/usr/local/bin`,
3. loads the `sg` kernel module at boot (needed by the mux),
4. adds a udev rule that keeps desktop automounters away from the mux card,
5. creates `/etc/booboot/NAME.ini` if missing, with the next free port
   (8080 for the first DUT, 8081 for the second, ...),
6. installs, enables and starts `booboot@NAME`.

Running it again updates the code and keeps the configuration.
[Installation from zero](install.md) goes step by step from a blank SD card,
and [First bring-up](bringup.md) lists the checks to do on new hardware.

The service runs as root: it switches GPIO lines, writes to the card and
mounts partitions. It is meant for a trusted network: there is no
authentication beyond the session.

## Development and tests

`booboot-server --fake` (or `python3 -m booboot_server --fake` in `server/`)
runs the server with a simulated board: a pseudo terminal that prints a boot
log and a login prompt when powered with `BOOT.BIN` on partition 1, and
directories as card partitions. The whole stack, client included, runs on a
desktop without hardware.

Tests: `python3 -m unittest discover -s tests`. The tests that need loop
devices, mounts or a simulated GPIO chip (`modprobe gpio-mockup`) run only as
root. CI runs them all, on several Python versions, runs the client tests on
Windows and macOS, and checks the C# client (`client/csharp/Check`) and
BooBoot Console (`client/csharp/BooBootConsole.Tests`, with its window drawn
off screen) against a server with a simulated board.

## Limits and ideas for later

- `run` needs the DUT to echo the command line, as shells and U-Boot do. It
  returns the output up to the last line break before the prompt: the end of
  an output without a final line break is lost.
- zstd images need Python 3.14 on the server.
- Ideas: `.bmap` support (write only the used blocks of an image), an image
  cache on the board, boot interrupt sequences run on the server (for tight
  timing like U-Boot with no autoboot delay), typing in BooBoot Console,
  current measurement of the DUT, reading a whole card back.

# Installation from zero

From a blank SD card to the first boot of the DUT. Part A sets up the
Raspberry Pi, part B the desktop computer. Then [First bring-up](bringup.md)
checks each part of the hardware. An agent with a shell on your computer can
also lead you through all of it: give it [agent-setup.md](agent-setup.md).

What you need (which models and why: [hardware.md](hardware.md)):

- A Raspberry Pi with Ethernet (Pi 3 or later recommended), its power supply,
  and a micro SD card of 16 GB or more for its system.
- A relay driver board and a relay for the DUT power.
- A USB-SD-Mux FAST and its USB cable.
- A USB UART adapter at the DUT I/O voltage (3.3 V or 1.8 V).
- An Ethernet network with DHCP and internet access (for the installation and
  the clock).

## A. Raspberry Pi

### 1. Write the system

1. Install Raspberry Pi Imager on a computer (raspberrypi.com/software).
2. Choose the Raspberry Pi model, then the system: "Raspberry Pi OS (other)",
   then "Raspberry Pi OS Lite". 64-bit for Pi 3, 4 and 5; 32-bit for Pi 1, 2
   and Zero.
3. Choose the micro SD card.
4. When Imager offers to customise the system, set:
   - hostname: `booboot` (with several boards: `booboot1`, `booboot2`, ...),
   - user name and password,
   - time zone,
   - SSH on, with password.

   No Wi-Fi: the board uses Ethernet.
5. Write the card, and put it in the Raspberry Pi.

### 2. Wiring

Power off. The pins used, on the pin header of the Raspberry Pi (40 pins, 26
on the first models). Pin 1 is the square pad, at the end of the header away
from the USB ports. Odd pins are on the inner row, even pins on the board
edge (pinout.xyz shows them all).

| Pin | Signal | Use |
|---|---|---|
| 2 | 5 V | relay driver power (5 V driver) |
| 1 | 3.3 V | relay driver power (3.3 V driver) |
| 6 | GND | relay driver ground |
| 11 | GPIO17 | relay driver input (default) |
| 7 | GPIO4 | relay driver input, for a driver that switches on with a low input |

**Relay driver.** Best: a driver that switches on when its input is high, and
works with 3.3 V on its input (a relay module with its trigger jumper on "H",
or a transistor driver). Connect:

- driver power (VCC) to pin 2 (5 V), or to pin 1 (3.3 V) for a 3.3 V driver,
- driver GND to pin 6,
- driver input (IN) to pin 11 (GPIO17).

This is the default configuration: nothing to change. GPIO17 is low while the
Pi boots, so the DUT stays off.

If the driver switches on when its input is low: use pin 7 (GPIO4, high while
the Pi boots) and set `line = GPIO4` and `active_low = yes` in the
configuration (step 5). Some 5 V modules of this kind do not switch fully off
with 3.3 V: [First bring-up](bringup.md) step 3 shows it.

The relay contact goes in series with the DUT power supply. For mains voltage,
use a relay board made and rated for it, in a closed box, and have it wired by
someone qualified.

**USB.** The USB-SD-Mux FAST and the USB UART adapter go into USB ports of the
Pi. Do not connect them to the DUT yet: [First bring-up](bringup.md) does it
step by step.

**Network.** Ethernet cable to the network, then power on the Pi. The first
start takes one or two minutes.

### 3. Log in

From the desktop computer, in a terminal (PowerShell on Windows):

```sh
ssh USER@booboot.local
```

`USER` is the user name set in Imager. If `booboot.local` is not found, find
the address of the Pi in the list of devices of your router (DHCP), and use
`ssh USER@ADDRESS`. Then use this address instead of `booboot.local`
everywhere below.

### 4. Install

On the Pi:

```sh
sudo apt update
sudo apt install -y git
git clone https://github.com/sonatique/BooBoot.git
sudo BooBoot/server/install.sh
booboot --url http://localhost:8080 status
```

`install.sh` installs the server in `/opt/booboot`, the `booboot` command,
and a service that starts at boot. `status` must answer, with the power off.
Errors about the mux or the console are normal while they are not connected.

### 5. Configuration

The configuration is `/etc/booboot/dut1.ini`. Each setting is explained in
the file. The defaults fit the wiring above. The ones most often changed:

- `[power] line` and `active_low`: the relay (step 2),
- `[console] baudrate`: the DUT serial speed (921600 by default),
- `[console] device`: with several USB serial adapters, the path of the right
  one in `/dev/serial/by-id/`.

```sh
sudo nano /etc/booboot/dut1.ini
sudo systemctl restart booboot@dut1
```

Service messages: `journalctl -u booboot@dut1 -f` (Ctrl+C to stop).

### 6. Clock

Most Raspberry Pi models have no clock that runs while they are off (the Pi 5
has one, with a battery): the Pi sets its time from the network. Log file names and the power on and off times shown by the viewers
use it. Check:

```sh
timedatectl
```

It must show the right local time and `System clock synchronized: yes`.
Without internet access, give it a time server of your network: in
`/etc/systemd/timesyncd.conf`, set `NTP=ADDRESS` under `[Time]`, then
`sudo systemctl restart systemd-timesyncd`. Time zone: `sudo raspi-config`,
Localisation Options.

### 7. Hardware checks

Follow [First bring-up](bringup.md) from step 2: it checks the relay, the SD
card and the serial console one by one, then connects the DUT.

## B. Desktop computer

### 1. Web page

Open `http://booboot.local:8080/` in a browser: the DUT console, live, with
nothing to install (see [web.md](web.md)).

### 2. Command line tool

`booboot.py` needs Python 3.9 or later.

- Windows: install Python from python.org, with "Add python.exe to PATH"
  checked, or run `winget install Python.Python.3.12`.
- Linux and macOS: Python 3 is usually there (`python3 --version`).

Get the file, from the repository clone, or directly:
`https://raw.githubusercontent.com/sonatique/BooBoot/main/client/booboot.py`

```sh
python3 booboot.py status          # Windows: python booboot.py status
```

The server address is `http://booboot.local:8080` by default. For another
one, give `--url`, or set it once:

- Windows: `setx BOOBOOT_URL http://ADDRESS:8080` (for new terminals),
- Linux and macOS: `export BOOBOOT_URL=http://ADDRESS:8080` in `~/.bashrc`
  or `~/.zshrc`.

To type `booboot` instead of `python3 booboot.py`:

- Linux and macOS: `chmod +x booboot.py` and copy it as `booboot` into a
  folder of the `PATH`, like `~/.local/bin`.
- Windows: next to `booboot.py`, create `booboot.cmd` with this line, and add
  the folder to the `PATH`:

  ```bat
  @python "%~dp0booboot.py" %*
  ```

Then `booboot --help` lists the commands.

### 3. BooBoot Console (optional)

A desktop program to watch the console, with log files on the desktop (see
[console.md](console.md)). From the latest release,
`https://github.com/sonatique/BooBoot/releases/latest`, download `BooBootConsole-win-x64.exe` (Windows) or
`BooBootConsole-linux-x64` (Linux), and run it. It is one file, with nothing
else to install.

- Windows may warn that the program is not signed: "More info", then "Run
  anyway".
- Linux: `chmod +x BooBootConsole-linux-x64` first.
- macOS: from the sources, with the .NET 10 SDK:
  `dotnet run --project client/csharp/BooBootConsole`.

### 4. MCP clients (optional)

To let an MCP client use the DUT through tools, add the MCP server of
`booboot.py` to its configuration: see [mcp.md](mcp.md).

## Later

**Update.** On the Pi, the configuration is kept:

```sh
cd ~/BooBoot && git pull && sudo server/install.sh
```

**A second DUT on the same Pi.** Connect its relay driver to another GPIO
line, its mux and its UART adapter, then:

```sh
sudo ~/BooBoot/server/install.sh dut2
```

It creates `/etc/booboot/dut2.ini` with the next port (8081). Set another
relay `line` in it. With two muxes and two UART adapters, each configuration
must also name its own: `serial` in `[sdmux]` (from
`sudo booboot-server --probe`) and `device` in `[console]` (a
`/dev/serial/by-id/` path), in both `dut1.ini` and `dut2.ini`. Clients use
`http://booboot.local:8081` for the second DUT.

**Remove.** With several DUTs, also disable `booboot@dut2` and the others.

```sh
sudo systemctl disable --now booboot@dut1
sudo rm -rf /opt/booboot /etc/booboot /var/log/booboot /usr/local/bin/booboot /usr/local/bin/booboot-server \
    /etc/systemd/system/booboot@.service /etc/udev/rules.d/99-booboot.rules /etc/modules-load.d/booboot.conf
sudo systemctl daemon-reload
```

**Security.** The service runs as root and has no password: keep it on a
trusted network, never reachable from the internet.

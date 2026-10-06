# Setup runbook for agents

This file is written for an agent that can run shell commands on the user's
computer and talk with the user. It sets up BooBoot on a Raspberry Pi
together with the user: the user does the manual steps, the agent does the
rest, tests what can be tested, and leaves the system ready to use.

Read the whole file before you start. The human guides it refers to are
[install.md](install.md) (steps A1 to A7 and B1 to B4) and
[bringup.md](bringup.md). If you only have this file, they are at
`https://raw.githubusercontent.com/sonatique/BooBoot/main/docs/`.

## 1. Rules

1. **Report every step.** Use the report format of section 2, before and
   after each step you run. Never run a command without reporting it.
2. **Ask, then verify.** When the user says a step is done, check it with the
   step's check command before you rely on it.
3. **Stop on surprises.** If a check fails or an output differs from what this
   file expects, stop, report what you saw, give the likely causes, and ask
   the user before doing anything not written here.
4. **Hands off the hardware without consent.** Do not switch the DUT power,
   switch the SD card, or write to the card unless the current step says so
   and the user agreed to it in this session. Never run `booboot sd flash`
   or delete card files other than the test file of step T4, unless the user
   asks for it.
5. **Change only what this file lists.** On the Pi: the BooBoot installation,
   `/etc/booboot/*.ini`, and with consent `/etc/systemd/timesyncd.conf`. On
   the user's computer: the report file, and the optional items of phase 4
   with consent. Do not change other system settings, users, passwords,
   firewalls or SSH settings.
6. **No secrets.** Never ask for, type or store passwords. Steps that need a
   password are done by the user in their own terminal.
7. **Mains voltage is the user's business.** If the DUT supply is mains
   voltage, say once that the relay wiring must be done by a qualified
   person, and do not give wiring advice beyond install.md.

## 2. How to report

Before a step:

```
STEP <id>: <title>
Goal: <what this step does>
Will run: <commands, in order>
```

After it:

```
STEP <id>: PASS | FAIL | SKIPPED
Ran: <each command, exactly, with its exit code>
Output: <the lines that matter, verbatim; say when you shortened it>
Changed: <what is now different on the Pi or the computer, or "nothing">
Next: <the next step>
```

For a manual step, give the user the instructions, wait, then report the
check like an agent step. Keep a report file, `booboot-setup-report.md`, in
the current directory of the user's computer: write the answers of phase 0,
then add each report as it is made. If the file exists when you start, read
it, tell the user what was done, and offer to go on from there.

## 3. Values

Ask for them in phase 0, or use the defaults. Use them in all commands.

| Name | Default | Meaning |
|---|---|---|
| `HOST` | `booboot.local` | Pi hostname or IP address |
| `USER` | (ask) | user name on the Pi |
| `NAME` | `dut1` | DUT name, also the service and configuration name |
| `PORT` | `8080` | server port (8081 for a second DUT, and so on) |

Commands on the Pi run through SSH from the user's computer:

```sh
ssh -o BatchMode=yes -o ConnectTimeout=10 -o StrictHostKeyChecking=accept-new USER@HOST '<command>'
```

Below, `pi '<command>'` means that. If you run on the Pi itself, run the
commands directly. On the Pi, always give the client its address:
`booboot --url http://localhost:PORT ...`.

## 4. The steps

| Id | Title | Who | Done when |
|---|---|---|---|
| A1 | Write the system | user | the Pi answers on the network (check of A3) |
| P1 | Network and power | user | same |
| A3 | Log in without password | user, agent | check of A3 passes |
| A4 | Install | agent | check of A4 passes |
| A5 | Configuration | agent | check of A5 passes |
| A6 | Clock | agent | check of A6 passes |
| T1 | Service and network | agent | check of T1 passes |
| T2 | What the board sees | agent | check of T2 passes |
| T3 | Relay | user, agent | user confirms |
| T4 | SD card | user, agent | check of T4 passes |
| T5 | Serial loopback | user, agent | check of T5 passes |
| O | Options (B2 to B4 and others) | agent, with consent | each chosen option checked |
| F1 | Connect the DUT | user | user confirms |
| F2 | First boot | user, agent | check of F2 passes |
| END | Final report | agent | report written |

A2 of install.md (wiring) is done part by part in T3 to T5 and F1: each
part is connected just before its test.

## 5. Phase 0: start

1. Say in two lines what you will do: set up BooBoot on the Pi with them,
   ask them for the manual steps, run and test the rest, and report each
   step in `booboot-setup-report.md`.
2. Ask, in one message:
   - Which of these are done: A1 (system written on the card), network and
     power connected, password-free SSH login, BooBoot installed, any wiring
     (relay, SD mux, UART adapter, DUT)?
   - The Pi user name, and the hostname or address if not `booboot`.
   - The relay driver: switches on with a high input, with a low input, or
     the Waveshare relay HAT? Not known yet is a valid answer.
   - The DUT serial speed (921600 if they do not know) and I/O voltage.
   - The DUT power supply voltage.

   If parts are missing or the user is not sure what to use, point to
   [hardware.md](hardware.md).
3. Write the answers in the report file. Then go to the first step not done.
   Steps said done are checked when reached; a failed check makes the step
   not done.

## 6. Phase 1: manual preparation

### A1: Write the system (user)

If not done, tell the user (details in install.md, A1):

1. Raspberry Pi Imager, device: their Pi model; system: "Raspberry Pi OS
   (other)", "Raspberry Pi OS Lite" (64-bit for Pi 3, 4, 5; 32-bit for Pi 1,
   2, Zero); storage: the micro SD card.
2. Customisation: hostname `booboot`, user name and password, time zone, SSH
   on. For SSH, offer password-free login now: show the user your public key
   (see A3) to paste as an authorised key in the SSH settings.
3. Write the card, put it in the Pi.

### P1: Network and power (user)

Ethernet cable, then power supply (the right one for the model, see
install.md). Nothing else connected yet. The first start takes two minutes.

### A3: Log in without password (user, then agent)

The agent cannot type passwords, so SSH must work with a key.

1. Check (agent): `pi 'echo ok'`.
   - `ok`: done, go to the sudo check below.
   - `Permission denied`: the key is not on the Pi. Go on with 2.
   - `Could not resolve hostname` or a timeout: the Pi is not found. Ask the
     user for its address (router list of DHCP devices), set `HOST`, and
     check again. Still nothing: ask them to check the cable, the power
     LED, and that the card was written with SSH on.
   - `REMOTE HOST IDENTIFICATION HAS CHANGED`: normal after writing a new
     card for the same hostname. With the user's consent, run
     `ssh-keygen -R HOST`, then check again.
2. Key (agent, with consent): if there is no `~/.ssh/id_ed25519.pub`, create
   it with `ssh-keygen -t ed25519 -N "" -f ~/.ssh/id_ed25519`. Show the
   public key.
3. Copy the key (user, in their own terminal, it asks for the Pi password):
   - Linux and macOS: `ssh-copy-id USER@HOST`
   - Windows PowerShell:
     `type $env:USERPROFILE\.ssh\id_ed25519.pub | ssh USER@HOST "mkdir -p ~/.ssh && cat >> ~/.ssh/authorized_keys"`
4. Check again (agent): `pi 'echo ok'` must print `ok`.
5. Sudo check (agent): `pi 'sudo -n true && echo ok'` must print `ok`. The
   first user of Raspberry Pi OS has sudo without password. If not, stop and
   tell the user: the install needs it.
6. Model (agent): `pi 'cat /proc/device-tree/model; echo; uname -m'`. Report
   it. If it is a Pi 1 or Zero, say that it works but is slower
   (architecture.md, Supported boards).

## 7. Phase 2: installation (agent)

### A4: Install

Check first: `pi 'systemctl is-active booboot@NAME'`. `active` means
installed: ask the user whether to update it (same commands, the
configuration is kept) or skip.

```sh
pi 'sudo apt-get update'
pi 'sudo apt-get install -y git'
pi 'test -d BooBoot && git -C BooBoot pull || git clone https://github.com/sonatique/BooBoot.git'
pi 'sudo BooBoot/server/install.sh NAME'
```

`install.sh` prints `Service booboot@NAME started.` It may take a few
minutes on older models. Check:

```sh
pi 'systemctl is-active booboot@NAME; systemctl is-enabled booboot@NAME'
pi 'booboot --url http://localhost:PORT status'
```

Expected: `active`, `enabled`, then a status with `power:   off`. Errors
about the SD card or the console are normal while they are not connected.
If the service is not active: `pi 'journalctl -u booboot@NAME -n 40 --no-pager'`,
report, stop.

### A5: Configuration

The file is `/etc/booboot/NAME.ini`. Show the user the current values:

```sh
pi 'grep -E "^(line|chip|active_low|backend|device|baudrate) *=" /etc/booboot/NAME.ini'
```

Set from the phase 0 answers, after telling the user what you change:

| Relay driver | Settings |
|---|---|
| switches on with a high input | `line = GPIO17`, `active_low = no` (defaults) |
| switches on with a low input | `line = GPIO4`, `active_low = yes` |
| Waveshare relay HAT, relay 1 | `line = GPIO26`, `active_low = yes` |
| not known yet | keep the defaults, settle it in T3 |

DUT serial speed other than 921600: `baudrate = <speed>`.

Edit with `sed` on the exact line, then restart and show the result:

```sh
pi 'sudo sed -i "s/^active_low = .*/active_low = yes/" /etc/booboot/NAME.ini'
pi 'sudo systemctl restart booboot@NAME && sleep 2 && systemctl is-active booboot@NAME'
pi 'grep -E "^(line|active_low|baudrate) *=" /etc/booboot/NAME.ini'
```

### A6: Clock

```sh
pi 'timedatectl'
```

Expected: the right local time and `System clock synchronized: yes`. If not
synchronised, ask whether the network has internet access. Without it, ask
the user for a time server of their network, then with consent:

```sh
pi 'sudo sed -i "s/^#\?NTP=.*/NTP=ADDRESS/" /etc/systemd/timesyncd.conf && sudo systemctl restart systemd-timesyncd'
```

Wrong time zone: tell the user to run `sudo raspi-config` (Localisation
Options) themselves, or with consent `pi 'sudo timedatectl set-timezone ZONE'`.

## 8. Phase 3: tests

### T1: Service and network (agent)

From the user's computer, not through SSH:

```sh
curl -s http://HOST:PORT/api/v1/status
curl -s -o /dev/null -w "%{http_code}\n" http://HOST:PORT/
```

Expected: JSON with `"name": "NAME"`, then `200` (the web page). On Windows
PowerShell, use `curl.exe`. Then a restart check, with the user's consent:

```sh
pi 'sudo systemctl restart booboot@NAME' ; sleep 3
pi 'booboot --url http://localhost:PORT status'
```

### T2: What the board sees (agent)

```sh
pi 'sudo booboot-server --probe'
```

Report the three parts:

- `GPIO chips`: the configured relay line must be listed (like `17=GPIO17`).
- `USB-SD-Mux`: one line `serial=... model=fast ...` when the mux is
  plugged in, with `card=/dev/sdX` when a card is in it.
- `Serial ports`: the UART adapter, when plugged in.

Missing mux or adapter at this point is normal if they are not connected
yet: say so, and check again in T4 and T5.

### T3: Relay (user, then agent)

1. Ask the user to connect the relay driver to the Pi (install.md A2: pins
   and driver types), with **nothing** on the relay contacts yet.
2. With their consent, run each command and ask what they saw:

   ```sh
   pi 'booboot --url http://localhost:PORT power on'
   pi 'booboot --url http://localhost:PORT power off'
   ```

   Expected: relay on (click, LED) after `power on`, off after `power off`.
3. Inverted: change `active_low` (A5), restart, test again. No reaction at
   all: check the wiring and `line`, run T2 again.
4. Boot state, with consent: `pi 'sudo reboot'`. Ask the user to watch the
   relay: it must stay off during the whole boot. Wait about a minute, then
   check `pi 'echo ok'`. If it switched on during the boot, see bringup.md
   step 3 (other GPIO line, or a resistor) and stop for the user's decision.

### T4: SD card (user, then agent)

1. Ask the user to plug the USB-SD-Mux FAST into the Pi, with a micro SD
   card in it, and the mux adapter **not** in the DUT. Tell them: a test
   file is written to the card and deleted, nothing else changes. Get their
   consent.
2. Run:

   ```sh
   pi 'sudo booboot-server --probe | grep -A2 USB-SD-Mux'
   pi 'booboot --url http://localhost:PORT power off'
   pi 'booboot --url http://localhost:PORT sd parts'
   pi 'echo booboot-test > /tmp/booboot-test.txt && booboot --url http://localhost:PORT sd put /tmp/booboot-test.txt 1:/'
   pi 'booboot --url http://localhost:PORT sd ls 1:/'
   pi 'booboot --url http://localhost:PORT sd rm 1:/booboot-test.txt'
   pi 'booboot --url http://localhost:PORT sd dut'
   ```

   Expected: a partition list, `booboot-test.txt` in the listing, then its
   deletion, then the card back on the DUT side. `SD card not ready`: ask to
   check the card, try another one.

### T5: Serial loopback (user, then agent)

1. Ask the user to plug the USB UART adapter into the Pi and to connect its
   TX and RX pins together (loopback), nothing else on it.
2. Run:

   ```sh
   pi 'booboot --url http://localhost:PORT status'
   pi 'booboot --url http://localhost:PORT console write booboot-loopback'
   pi 'booboot --url http://localhost:PORT console expect booboot-loopback --since -200 --timeout 3'
   ```

   Expected: the status shows the adapter (`console: /dev/...`, not "not
   connected"); `expect` exits with 0. Exit code 3 (timeout): check the
   loopback wire and the adapter. Several adapters: set `device` in the
   configuration to the right `/dev/serial/by-id/...` path (A5).
3. Ask the user to remove the loopback wire.

## 9. Phase 4: options (agent, with consent)

Offer each one, do the chosen ones, check and report each. Steps B1 to B4
are in install.md, part B.

| Option | What to do | Check |
|---|---|---|
| B1 Web page | tell the user the address `http://HOST:PORT/` | T1 already got 200 |
| B2 Command line tool on this computer | download `https://raw.githubusercontent.com/sonatique/BooBoot/main/client/booboot.py` to a folder the user chooses; if `HOST` or `PORT` differ from the defaults, tell them how to set `BOOBOOT_URL` (install.md B2) | `python3 booboot.py --url http://HOST:PORT status` (Windows: `python`) |
| B3 BooBoot Console | give the user the download link of install.md B3 | the user confirms it shows the console |
| B4 MCP server | if your host supports MCP servers and the user wants it: register `python3 PATH/booboot.py --url http://HOST:PORT mcp` as a stdio server named `booboot` (mcp.md), after B2 | the server's `status` tool answers |
| Web page off | `web = no` under `[server]` (A5 method) | `curl` of `/` gives 404 |
| Session timeout | `session_timeout` in seconds (A5 method) | `status` shows it after a session is opened |
| Second DUT | install.md, Later: `sudo BooBoot/server/install.sh dut2`, then per DUT config | phases 2 and 3 with `NAME=dut2`, `PORT=8081` |

## 10. Phase 5: final manual steps

### F1: Connect the DUT (user)

Give the user these steps (bringup.md step 6), and wait for each:

1. Power off first: `pi 'booboot --url http://localhost:PORT power off'`.
2. UART: GND to GND, adapter TX to DUT RX, adapter RX to DUT TX, at the DUT
   I/O voltage.
3. The micro SD adapter of the mux into the DUT SD slot.
4. The DUT power supply through the relay contacts: COM and NO (normally
   open), so that the DUT is off when the relay is off.
5. Ask the user to measure, if they can, the DUT supply with the power off:
   close to 0 V. If not, the UART adapter feeds the DUT through its pins
   (bringup.md step 6).

### F2: First boot (user, then agent)

1. Ask the user for: the boot files on this computer (for example
   `BOOT.BIN` and `image.ub`), the card directory (default `1:/`), and the
   text that ends the boot (for example `login: `).
2. Copy the files to the Pi with `scp FILES USER@HOST:/tmp/`, then, with
   consent:

   ```sh
   pi 'cd /tmp && booboot --url http://localhost:PORT deploy BOOT.BIN image.ub --expect "login: " --timeout 120'
   ```

   Report the boot output and the time from power on to the expected text.
   On failure, use the table of bringup.md step 7 (nothing at all, garbage
   characters, ...), report, and ask.
3. Optional, with consent: `pi 'booboot --url http://localhost:PORT boottime "login: " --runs 3'`.

## 11. END: Final report

1. Release the session: `pi 'booboot --url http://localhost:PORT session close'`.
2. Write the final report in the report file and show it to the user:
   - a table of all steps with PASS, FAIL or SKIPPED, and why for the last
     two,
   - the Pi model, the system, the BooBoot version (`status`),
   - the configuration values changed,
   - the addresses: `http://HOST:PORT/` (web page) and the API,
   - what is left to do, if anything,
   - how to update later: `cd ~/BooBoot && git pull && sudo server/install.sh NAME`
     on the Pi,
   - how to use the unit from an agent later: [agent-use.md](agent-use.md).

## 12. Common problems

| Seen | Likely cause | What to do |
|---|---|---|
| `Permission denied (publickey,password)` | key not on the Pi | A3, steps 2 and 3 |
| `sudo: a password is required` | user without password-free sudo | ask the user; the install needs sudo |
| `apt-get` fails to download | no internet, or wrong clock | check the network; A6 |
| `pip` fails in `install.sh` | no access to PyPI | the network needs internet access during the install |
| status: `power: unknown` | GPIO line wrong or busy | T2, then `line` in A5 |
| no mux in `--probe` | cable, power, `sg` module | `pi 'lsusb'`, `pi 'sudo modprobe sg'`, T2 again |
| console `not connected` | adapter missing, or several adapters | T2; set `device` (A5) |
| `busy` error, exit code 4 | another client has the session | ask the user; `session close` on that client, or wait |

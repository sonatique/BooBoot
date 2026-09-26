# BooBoot

Remote control of an embedded board that boots from an SD card: power, SD
card content and serial console, over the network.

A small Linux board (like an old Raspberry Pi) sits next to the board under
test (the DUT). It switches the DUT power with a relay, changes the SD card
content through a USB-SD-Mux FAST (Linux Automation GmbH), and reads and
writes the DUT serial console through a USB UART adapter. Any program on the
network can use it through a simple HTTP and JSON API. A command line tool
and a Python library are included.

- [Architecture](docs/architecture.md)
- [HTTP API](docs/api.md)

## Server setup (on the Raspberry Pi)

1. Install Raspberry Pi OS Lite (any model, 32 or 64 bit) and connect it to
   the network.
2. Connect the relay driver to GPIO17, the USB-SD-Mux FAST and the USB UART
   adapter.
3. Get this repository on the board and run:

   ```sh
   sudo server/install.sh
   ```

4. Check the setup and adapt `/etc/booboot/dut1.ini` if needed:

   ```sh
   sudo booboot-server --probe       # GPIO lines, muxes, serial ports
   sudo systemctl restart booboot@dut1
   booboot --url http://localhost:8080 status
   ```

## Client

`client/booboot.py` needs only Python 3.9 or later, on Linux, macOS or
Windows. Copy it anywhere.

```sh
export BOOBOOT_URL=http://booboot.local:8080

booboot status
booboot deploy BOOT.BIN image.ub --expect "login: " --timeout 120
booboot console run root
booboot console run "uname -a"
booboot console attach              # interactive terminal, Ctrl-] to quit
booboot session close
```

Other commands: `booboot --help`, and `booboot COMMAND --help`.

## Development

Run the server with a simulated board, no hardware needed:

```sh
cd server && python3 -m booboot_server --fake --port 8080
```

Run the tests (as root, the tests with loop devices and GPIO run too):

```sh
python3 -m unittest discover -s tests
```

CI (GitHub Actions) runs a lint check and the tests on Python 3.9, 3.11 and
3.14, once as root, and runs the client tests on Windows and macOS.

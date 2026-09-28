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
- [First bring-up](docs/bringup.md)
- [MCP server](docs/mcp.md)

## Server setup (on the Raspberry Pi)

1. Write Raspberry Pi OS Lite (any model, 32 or 64 bit) with Raspberry Pi
   Imager, with hostname `booboot` and SSH on. Connect it to the network.
2. Connect the relay driver to GPIO17, the USB-SD-Mux FAST and the USB UART
   adapter.
3. Install:

   ```sh
   sudo apt install -y git
   git clone https://github.com/sonatique/BooBoot.git
   sudo BooBoot/server/install.sh
   ```

4. Follow [First bring-up](docs/bringup.md): it checks the relay, the SD card
   and the serial console one by one before the DUT is connected.

## Client

`client/booboot.py` needs only Python 3.9 or later, on Linux, macOS or
Windows. Copy it anywhere.

```sh
export BOOBOOT_URL=http://booboot.local:8080

booboot status
booboot deploy BOOT.BIN image.ub --expect "login: " --timeout 120
booboot console run root
booboot console run "uname -a"
booboot boottime "login: " --runs 5  # power on to login prompt, 5 boots
booboot console attach              # interactive terminal, Ctrl-] to quit
booboot session close
```

Other commands: `booboot --help`, and `booboot COMMAND --help`.

For C#, `client/csharp/BooBootClient.cs` is a small client class (.NET 6 or
later, no package needed), with an example program in `client/csharp/Example`.
Other languages can use the [HTTP API](docs/api.md) directly.

`booboot mcp` runs an [MCP server](docs/mcp.md) on standard input and output,
so that MCP clients can use the DUT through tools.

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
3.14, once as root, runs the client tests on Windows and macOS, checks the C#
client against a server with a simulated board, and runs `server/install.sh`
on a machine with systemd, with the relay on a simulated GPIO chip.

# First bring-up

Do these steps in order. Each one checks one part alone, before the DUT is
connected, so that a problem shows up where it is easy to find. The details of
step 1, with the wiring, are in [Installation from zero](install.md).

## 1. Board and OS

1. Write Raspberry Pi OS Lite (32 or 64 bit) with Raspberry Pi Imager. In its
   settings: hostname `booboot`, SSH on, your user and password.
2. Connect Ethernet and log in: `ssh booboot.local`.
3. Get the code and install:

   ```sh
   sudo apt update
   sudo apt install -y git
   git clone https://github.com/sonatique/BooBoot.git
   sudo BooBoot/server/install.sh
   booboot --url http://localhost:8080 status
   ```

   `status` must answer. Errors about GPIO, mux or console are normal here.

## 2. What the board sees

```sh
sudo booboot-server --probe
```

- **GPIO chips**: the relay line must be listed (`17=GPIO17` on a Raspberry
  Pi). On other boards, write its name in `line` of `/etc/booboot/dut1.ini`,
  or its number with `chip`.
- **USB-SD-Mux**: one line with `model=fast`, and `card=/dev/sdX` when a card
  is in the mux. If it is missing: check the cable, `lsusb`, `sudo modprobe sg`.
- **Serial ports**: the USB UART adapter. With several adapters, write the
  right `/dev/serial/by-id/...` path in `device` of the configuration.

After a change of the configuration: `sudo systemctl restart booboot@dut1`.

## 3. Relay, without the DUT

Connect only the relay driver and the relay, not the DUT power.

```sh
booboot power on      # relay clicks, contact closed (check with a multimeter)
booboot power off     # contact open
```

- Inverted? Set `active_low = yes` (or `no`) in the configuration.
- Check the relay stays off while the board boots: `sudo reboot` and watch it.
  Before the service starts, only the pull-up or pull-down of the GPIO pin
  (and of the driver input) decides. If it clicks on during boot, choose a
  pin with the other default pull, or add a resistor.

## 4. SD card, without the DUT

Put a card in the mux. Leave the mux adapter out of the DUT.

```sh
booboot sd parts                  # partition list, FAT32 first
echo test > test.txt
booboot sd put test.txt 1:/
booboot sd ls 1:/
booboot sd rm 1:/test.txt
booboot sd dut
```

`SD card not ready`: check that the card is in the mux, try another card.

## 5. Serial, without the DUT

Connect TX to RX on the USB UART adapter (loopback).

```sh
booboot console write hello
booboot console read --since -20      # shows hello
```

Nothing? Check `booboot status` (console connected?) and the adapter.

## 6. Connect the DUT

- UART: GND to GND, adapter TX to DUT RX, adapter RX to DUT TX, at the DUT I/O
  voltage (3.3 V or 1.8 V).
- The micro SD adapter of the mux in the DUT SD slot.
- DUT power through the relay.

With the power off (`booboot power off`), measure the DUT 3.3 V rail: it must
be close to 0 V. If not, the idle high TX line of the adapter feeds the DUT
through its I/O pins: power cycles will not be clean. Add a buffer powered from
the DUT side, or a series resistor.

## 7. First boot

```sh
booboot deploy BOOT.BIN image.ub --expect "login: " --timeout 120
```

| What you see | What to check |
|---|---|
| Nothing at all | TX and RX swapped, GND, voltage level, `booboot console read --since boot`, or the web page `http://booboot.local:8080/` |
| Garbage characters | Baud rate (`baudrate` in the configuration, 921600 by default) |
| U-Boot does not start | Card content, card in the DUT slot (`booboot status`: sd card on dut) |
| Keys have no effect | `line_ending` (`cr` by default; try `lf`) |
| `run` times out | Prompt regex (`prompt` in the configuration, or `--prompt`) |

## 8. From your desktop

```sh
booboot --url http://booboot.local:8080 status
```

If `booboot.local` does not resolve, use the IP address of the board.

## Logs

- Service: `journalctl -u booboot@dut1`
- Console, one file per power on: `/var/log/booboot/dut1/` (`latest.log` is
  the current one)

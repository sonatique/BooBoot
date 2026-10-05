# Hardware

What to buy for one DUT, and why. [install.md](install.md) shows how to
connect it, [bringup.md](bringup.md) how to check each part.

## Shopping list

| Part | Recommended | Notes |
|---|---|---|
| Board | Raspberry Pi 3 Model B or later, any RAM size | See [Choosing the Raspberry Pi](#choosing-the-raspberry-pi) |
| Power supply | The official supply for the model: Pi 3: micro-USB 5.1 V 2.5 A; Pi 4: USB-C 5.1 V 3 A; Pi 5: USB-C 5.1 V 5 A | The Pi also powers the mux, the UART adapter and the relay driver. A weak supply causes random USB problems. |
| System card | micro SD card, 16 or 32 GB, class A1, from a known brand | For the Pi system, not for the DUT |
| SD card switch | USB-SD-Mux FAST (Linux Automation GmbH) | The classic USB-SD-Mux works too, slower |
| Mux cable | USB-A to USB-C cable (Pi 3, 4 and 5 have USB-A ports) | USB 2.0 is enough |
| DUT card | micro SD card that the DUT supports, class A1 or better | Goes into the mux |
| Serial adapter | USB UART adapter with an FTDI (FT232R, FT232H) or CP2102N chip, at the DUT I/O voltage | See [USB UART adapter](#usb-uart-adapter) |
| Relay | Relay module with a high-level trigger (or an H/L trigger jumper), 5 V coil, or a relay HAT | See [Relay](#relay) |
| Wires | Female-female jumper wires | Relay driver, UART, and one for the loopback test |
| Network | Ethernet cable to a network with DHCP and internet access | Internet for the installation and the clock |
| Optional | A case with air holes; a heatsink or fan for Pi 4 and 5 | |

Several DUTs on one board: one mux, one UART adapter and one relay per DUT.
A relay board with several channels serves several DUTs.

## Choosing the Raspberry Pi

**Short answer:** a Pi 3 Model B or 3B+ is enough for everything. A Pi 4 or 5
is better if you often write full disk images, or run several DUTs on one
board.

What limits the speed:

- **The mux is a USB 2.0 device.** The USB-SD-Mux FAST reaches about 35 MB/s
  between the card and the board. USB 3 ports do not make it faster.
- **On Pi 1, 2 and 3, Ethernet and the USB ports share one USB 2.0 bus.**
  While a full image is written, the download and the card write compete for
  it. On Pi 4 and 5, Ethernet is separate (Gigabit), and the mux has the USB
  bandwidth for itself.
- **The processor matters little.** The server uses little CPU. It counts
  only on single-core models, when several viewers watch fast console output,
  or when decompressing xz images.
- **Copying boot files** (`BOOT.BIN`, `image.ub`, tens of MB) takes seconds on
  any model. Only full disk images take minutes.

| Model | Verdict | Full image, 1 GB raw (estimate) | Notes |
|---|---|---|---|
| Pi 4, Pi 5 | Best | 30 to 40 s (mux limit) | Needs cooling in a closed case |
| Pi 3 Model B+ | Good | 50 to 70 s | Ethernet about 300 Mbit/s, shared with USB |
| Pi 3 Model B, Pi 2 Model B | Good | 90 to 120 s | Ethernet 100 Mbit/s, shared with USB |
| Pi 1 Model B, B+ | Works, slow | 2 to 3 min | Single core; send gz or raw images rather than xz. The first Model B has weak USB power: use a powered hub. |
| Pi Zero, Zero 2 W | Not recommended | | No Ethernet, one USB port: needs a hub with Ethernet |

The times are estimates from the bus and network limits, not measurements.
Compressed images (gz, xz) cross the network faster. Other Linux boards work
the same way ([architecture.md](architecture.md#supported-boards)).

System: Raspberry Pi OS Lite, Bullseye or later (Python 3.9 or later); 64-bit
on Pi 3 and later.

## USB UART adapter

- **Voltage.** The adapter signals must match the DUT I/O voltage, usually
  3.3 V, sometimes 1.8 V. 5 V adapters can damage the DUT. For 1.8 V, use an
  adapter made for it (like the FTDI TTL-232RG-VREG1V8 cable) or a level
  shifter.
- **Chip.** FTDI (FT232R, FT232H) and CP2102N adapters handle 921600 baud
  well. CH340 adapters work too.
- **Serial number.** With several adapters on one board, each needs its own
  serial number, so that its name in `/dev/serial/by-id/` is unique and
  stays the same. FTDI and CP2102N chips have one. Most CH340 do not: two of
  them cannot be told apart reliably.
- **Wiring.** GND to GND, adapter TX to DUT RX, adapter RX to DUT TX. Do not
  connect the adapter VCC pin to the DUT.
- **Back-powering.** While the DUT is off, the idle TX line of the adapter is
  high and can feed the DUT through its I/O pins, so that a power cycle is not
  a clean cold boot. If bringup.md step 6 shows it, add a buffer powered from
  the DUT side (a part with power-off protection, like a 74LVC1G125), or at
  least a 1 kOhm resistor in series with the adapter TX line.

## Relay

The relay must be off at rest, and stay off while the Pi boots, so that the
DUT is never powered by accident. Before the server starts, only the pull of
the GPIO pin decides: on Raspberry Pi, GPIO 0 to 8 are pulled high at reset,
GPIO 9 to 27 pulled low.

| Choice | Wiring and configuration | Notes |
|---|---|---|
| Module with high-level trigger, or H/L jumper set to H (best) | IN to GPIO17 (pin 11). Default configuration. | GPIO17 is low at reset: relay off |
| Module with low-level trigger only (common optocoupler modules) | IN to GPIO4 (pin 7), `line = GPIO4`, `active_low = yes` | Some 5 V modules do not switch fully off with 3.3 V. On boards with a JD-VCC jumper: remove it, JD-VCC to 5 V, VCC to 3.3 V. |
| Waveshare RPi Relay Board (HAT, 3 relays, 5 A) | Plugs on the header. Relay 1: `line = GPIO26`, `active_low = yes` | Check that it stays off during boot (bringup.md step 3) |
| USB relay or network power switch | `backend = command`, with `on_command` and `off_command` | No GPIO needed |
| Relay driven by the mux GPIO | `backend = sdmux` | No GPIO needed; needs a low-level trigger driver, `active_low = yes` ([architecture.md](architecture.md#hardware)) |

Choosing the relay:

- **Contact rating.** Check the rating for the kind of supply you switch: a
  relay rated 10 A at 250 V AC is usually rated only 10 A at 30 V DC. Keep a
  margin for the inrush current of DUT supplies with large capacitors.
- **Driver.** Use a module with a driver transistor or optocoupler and a
  flyback diode, never a bare relay coil on a GPIO pin. Relay coils take
  about 70 mA each from the 5 V pin, which is fine.
- **Switch the low-voltage side.** Switching the DC output of the DUT power
  supply gives clean, fast power cycles. Switching the mains side of a power
  adapter works too, but its capacitors keep the DUT powered for a moment
  after the switch: raise `off_time` in the configuration if the DUT does not
  fully reset.
- **Mains voltage.** Use a relay board made and rated for it, in a closed box,
  and have it wired by someone qualified. Not a HAT on the Pi.
- **Contacts.** Wire the DUT supply through COM and NO (normally open), so
  that the DUT is off when the relay is off.

## Example lists

**One DUT, small budget:** Pi 3 Model B, official 2.5 A supply, 16 GB A1
card, USB-SD-Mux FAST with a USB-A to USB-C cable, FTDI 3.3 V USB UART
adapter, 1-channel 5 V relay module with H/L trigger jumper, jumper wires,
Ethernet cable.

**Several DUTs, frequent full images:** Pi 4 with official supply and a
cooled case, 32 GB A1 card, Waveshare RPi Relay Board (B) with 8 relays, and
per DUT: one USB-SD-Mux FAST with its cable, one FTDI or CP2102N adapter.

## Links

- [USB-SD-Mux FAST, Linux Automation GmbH](https://linux-automation.com/en/products/usb-sd-mux-fast.html)
- [Waveshare RPi Relay Board](https://www.waveshare.com/rpi-relay-board.htm),
  [8-relay version (B)](https://www.waveshare.com/rpi-relay-board-b.htm)
- [Raspberry Pi pin numbers](https://pinout.xyz)

# ESP32 + EBYTE E32-433T30D — two-node LoRa chat

Two nodes, two different implementations of the same protocol:

- **PC node**: ESP32-WROOM + E32, wired together, flashed from VS Code +
  PlatformIO, connected to the PC over USB.
- **Pi node**: E32 wired **directly into the Raspberry Pi 5's GPIO header**
  (no second ESP32 involved) — a Python script on the Pi talks to it, run
  over SSH.

Type a line on either side, press Enter, it goes out over the E32 and prints
on the other side.

## 1. PC node wiring — ESP32 <-> E32

| E32-433T30D pin | Connect to |
|---|---|
| M0  | ESP32 GPIO25 |
| M1  | ESP32 GPIO33 |
| RXD | ESP32 GPIO27 (TX2) |
| TXD | ESP32 GPIO26 (RX2) |
| AUX | ESP32 GPIO32 |
| VCC | **Separate 3.3V supply, NOT the ESP32 board's 3.3V pin** (see Power below) |
| GND | Common ground with ESP32 AND the separate supply |

These GPIOs are free on WROOM/WROVER boards regardless of variant (no
PSRAM/boot-strapping conflicts).

## 2. Pi node wiring — Raspberry Pi 5 GPIO <-> E32

BCM numbering (the numbers silkscreened as "GPIOxx" on most pinout diagrams,
not the physical pin position):

| E32-433T30D pin | Connect to |
|---|---|
| M0  | Pi GPIO23 (physical pin 16) |
| M1  | Pi GPIO24 (physical pin 18) |
| RXD | Pi GPIO14 / TXD (physical pin 8) |
| TXD | Pi GPIO15 / RXD (physical pin 10) |
| AUX | Pi GPIO17 (physical pin 11) |
| VCC | **Separate 3.3V supply, NOT the Pi's 3V3 pin** (see Power below) |
| GND | Common ground with the Pi AND the separate supply (e.g. physical pin 6) |

Note the crossover again: E32 TXD → Pi's RXD pin (GPIO15), E32 RXD → Pi's
TXD pin (GPIO14).

### Enable the UART on the Pi (one-time)

By default the Pi's console can sit on this same UART, which will fight with
our script for the port. Free it up:

```
sudo raspi-config
```
→ **Interface Options → Serial Port**
→ "Would you like a login shell to be accessible over serial?" → **No**
→ "Would you like the serial port hardware to be enabled?" → **Yes**
→ Finish → reboot.

After reboot this exposes `/dev/serial0` (symlinked to the Pi 5's primary
UART). Unlike older Pi models, the Pi 5's Bluetooth radio has its own
dedicated UART, so there's no need for the classic "disable Bluetooth to
free up GPIO14/15" workaround.

## 3. Power — read this before you power either E32 on

The 433T30D is the **1W (30dBm)** version. During transmit bursts it can pull
several hundred mA, which neither the ESP32 board's onboard regulator nor
the Pi's 3V3 GPIO pin is meant to supply — the classic symptom is random
resets/reboots, or garbled/empty received data.

- Power **each** E32's VCC from its own **dedicated 3.3V source rated ≥1A**
  (a small buck converter, an AMS1117-3.3 with proper caps, etc.) — not from
  the ESP32 board's 3.3V pin, and not from the Pi's 3V3 pin.
- Do **not** power either module at 5V. VCC sets the module's TXD output
  level, and neither the ESP32's nor the Pi's GPIOs are 5V-tolerant. Keep
  everything at 3.3V and skip level shifters entirely.
- Tie grounds together: E32 GND, its host board's GND, and the separate
  supply's GND, on both nodes.
- **Never power a module without its antenna attached** — transmitting into
  an open port can damage the power amplifier.

## 4. Project layout

```
e32-lora-chat/
  platformio.ini      # PC node: ESP32 firmware
  src/main.cpp
  pi/
    chat.py            # Pi node: Python script, run directly on the Pi
```

## 5. PC side (VS Code, local)

1. Install the **PlatformIO IDE** extension in VS Code.
2. Open the `e32-lora-chat` folder in VS Code (`File > Open Folder`).
3. Plug the PC's ESP32 into a USB port.
4. Build/upload/monitor:
   ```
   pio run -t upload
   pio device monitor
   ```
5. You should see: `[PC] E32 LoRa chat ready. Type a message and press Enter...`

## 6. Pi side (Python, over SSH)

1. SSH into the Pi: `ssh pi@<raspberry-pi-ip>`
   (VS Code's **Remote-SSH** extension works too if you want an editor —
   `Ctrl+Shift+P` → "Remote-SSH: Connect to Host..." — but a plain terminal
   is enough for this script.)
2. Copy `pi/chat.py` to the Pi, e.g. from the PC:
   ```
   scp "e32-lora-chat/pi/chat.py" pi@<raspberry-pi-ip>:~/chat.py
   ```
3. **Important — Raspberry Pi 5 GPIO gotcha**: the E32 Python library uses
   the classic `RPi.GPIO` package, but the version of `RPi.GPIO` on PyPI does
   **not** support the Pi 5's new RP1 GPIO chip. Use `rpi-lgpio` instead — it's
   a drop-in replacement that provides the same `RPi.GPIO` import path but
   actually works on a Pi 5.
   ```
   python3 -m venv ~/e32-venv
   source ~/e32-venv/bin/activate
   pip install rpi-lgpio pyserial
   pip install --no-deps ebyte-lora-e32-rpi
   ```
   (The `--no-deps` on the last line matters: that package's own metadata
   asks pip for plain `RPi.GPIO`, which would pull in the Pi-5-incompatible
   version and shadow the `rpi-lgpio` you just installed. Installing it with
   `--no-deps` keeps `rpi-lgpio` as the one that answers `import RPi.GPIO`.)
4. Serial port permission (one-time):
   ```
   sudo usermod -aG dialout $USER
   ```
   then log out and back in (or reboot) for it to apply.
5. Run it:
   ```
   source ~/e32-venv/bin/activate   # if not already active
   python3 ~/chat.py
   ```
6. You should see: `[PI] E32 LoRa chat ready. Type a message and press Enter...`

## 7. Test it

1. Have the PC's Serial Monitor and the Pi's SSH session open side by side.
2. Type `hello from pc` in the PC monitor, hit Enter.
3. Within a second you should see `[PI <- recv] hello from pc` on the Pi side.
4. Type something back on the Pi side, confirm it shows up on the PC.

## Troubleshooting

- **Nothing received, ever**: double check RX/TX aren't swapped on either
  node (E32 TXD → host's RX pin, E32 RXD → host's TX pin), and that both
  modules are still on their factory-default channel/address (matches out of
  the box unless one was reconfigured previously).
- **Garbled data / random resets while sending**: almost always the power
  supply issue above — the E32 is browning out.
- **Pi: `RuntimeError` about GPIO chip / "Cannot determine SOC peripheral
  base address"**: `rpi-lgpio` isn't actually installed/active — check with
  `pip show rpi-lgpio` inside the venv, and make sure plain `RPi.GPIO` never
  got installed on top of it.
- **Pi: permission denied on /dev/serial0**: the `dialout` group change
  hasn't taken effect yet — log out/in or reboot.
- **Pi: port busy / nothing on /dev/serial0**: revisit the `raspi-config`
  serial steps above — the login shell must be disabled while the hardware
  stays enabled.
- **Range is short on the bench**: expected indoors/close-range with the
  stock antenna and default air data rate; not a wiring problem.

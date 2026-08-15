# Rover-side LoRa endpoint — ESP32-S3 + EBYTE E32-433T30D

The rover half of the 433 MHz fallback link. Stands in for the Jetson: it parses
teleop frames, answers each one, and prints the command it *would* have driven.
The mast half is `mast/lora_bridge.py`, running on the Pi 5 with its own E32.

If you only want to prove two radios can talk to each other, the older
`../e32-lora-chat/` project is the simpler text-chat version and its README
documents the Pi wiring in more detail. This project is the framed protocol
that replaces it.

## 1. Wiring — ESP32-S3-DevKitC-1 ↔ E32

| E32 pin | E32 name | ESP32-S3 |
|---|---|---|
| 1 | M0 | GPIO15 |
| 2 | M1 | GPIO16 |
| 3 | RXD | GPIO17 (`TX1`) |
| 4 | TXD | GPIO18 (`RX1`) |
| 5 | AUX | GPIO21 |
| 6 | VCC | **separate 3.3 V supply, ≥1 A — not the dev board's 3V3 pin** |
| 7 | GND | common with the S3 **and** that supply |

Note the crossover: E32 **TXD → S3 RX**, E32 **RXD → S3 TX**. Getting this
backwards is the single most common reason a link never works.

GPIO17/18 are the S3's default `Serial1` pins, so nothing needs remapping. None
of the five collide with the strapping pins (0, 3, 45, 46), the USB pins
(19, 20), flash/PSRAM (26–37), or anything the panel-board firmwares in this
repo already use (1–6 ADC, 8/9 I2C, 43/44 USB UART).

## 2. Power and safety — before you apply power

- **Never power a module without its antenna attached.** A 1 W PA transmitting
  into an open port can destroy itself.
- The 30 dBm part draws several hundred mA in transmit bursts. Neither the dev
  board's regulator nor a Pi's 3V3 pin will hold that up. The symptom is not an
  obvious power fault — it is random resets and garbled receives.
- Dedicated 3.3 V ≥1 A per module, ≥100 µF low-ESR bulk capacitor at the
  module's VCC pin, all grounds tied together.
- The module tolerates up to 5.2 V and only reaches its full rated power at
  ≥5.0 V, so at 3.3 V the output is below 30 dBm. That is the right trade here:
  VCC also sets the module's TXD output level, and neither the S3 nor the Pi is
  5 V-tolerant, so staying at 3.3 V keeps level shifters out of the design.
  **Above 5.2 V destroys the module.**
- **The default is full power, 30 dBm.** Worth knowing what that means: the EU
  limit for the 433.05–434.79 MHz ISM band is roughly 10 mW ERP, and ERC has its
  own frequency rules on top. `CFG!LOW` drops the module to its minimum 21 dBm
  if a bench session or a rules check calls for it.

## 3. Module configuration

Both ends must agree on channel, air data rate, FEC and transmission mode. The
firmware writes this over the module's own command interface — see §5.

Factory default reads back as `C0 00 00 1A 17 44`. Target:

| Byte | Value | Meaning |
|---|---|---|
| HEAD | `C0` | save over power-down (`C2` writes the same fields without saving) |
| ADDH | `00` | transparent mode; our frame header does the addressing |
| ADDL | `00` | |
| SPED | `1B` | `00` 8N1 · `011` UART 9600 · `011` air 4.8 kbps |
| CHAN | `17` | 410 + 23 = **433 MHz** |
| OPTION | `44` | `0` transparent · `1` push-pull · `000` 250 ms wake · `1` FEC on · `00` **30 dBm** |

`CFG!LOW` writes `47` instead — the same settings at the module's minimum
21 dBm, for bench work or wherever §2's regulatory note applies.

Air data rate is the one real trade. 4.8 kbps roughly doubles throughput over
the 2.4 kbps default and costs a few dB of sensitivity, i.e. range. If the range
walk disappoints, set `CFG_SPED` to `0x1A` in `src/main.cpp` (and `CFG_SPED` in
`mast/lora_bridge.py`) and halve the poll rate.

Configuration only works in mode 3 (M0=M1=1) at 9600 8N1 whatever the
configured UART baud is — the firmware handles both.

## 4. Protocol

The channel is half-duplex and shared: if both ends transmit at once the frames
collide and both are lost. So the mast polls and the rover only ever answers,
immediately, inside the same read that consumed the poll. Nothing else
transmits, ever.

```
AA 55 | type | seq | payload[4] | crc16[2]        = 10 bytes
```

| Field | Notes |
|---|---|
| sync | `AA 55`; the resync point after any corruption |
| type | `01` teleop (mast→rover), `02` status (rover→mast) |
| seq | uint8, wraps; the rover echoes it so the mast can measure RTT and loss |
| payload | teleop: `int8 vx, vy, wz` (±100 % of max), `uint8 flags` · status: `uint8 echo_seq, rx_ok, rx_bad, flags` |
| crc16 | CRC-16/CCITT-FALSE over `type..payload`, little-endian. Bad CRC → dropped, never acted on |

Ten bytes stays inside the module's 58-byte single-packet limit, so one frame is
always one air packet, written in a single `write()`.

`src/link_frame.h` and `mast/lora_frame.py` are byte-for-byte mirrors with no
shared source of truth. Change one, change the other, then re-run the check in
§6 step 2.

**Failsafe:** the rover zeroes its command output after 500 ms without a
CRC-valid teleop frame — two to three missed polls at 5 Hz. It fails to
*stopped*, never to last-known-good, and it boots into that state.

## 5. Build and console

```
pio run -t upload -t monitor
```

Console is 115200 on the USB-UART port. Commands (type and press Enter):

| Command | Effect |
|---|---|
| `CFG?` | read the module's six config bytes and print them as hex |
| `CFG!` | write §3 at 30 dBm, then read back |
| `CFG!LOW` | write §3 at 21 dBm, then read back |
| `TEST` | transmit one known teleop frame, and report whether AUX confirmed the module keyed |
| `TESTBAD` | the same frame with a corrupted CRC |
| `STAT` | print frame counters and failsafe state |

Once a second it prints the current command, counters, and `LINKED`/`FAILSAFE`.

## 6. Bring-up

Each step fails for one reason. Do not skip ahead — most "the radio doesn't
work" reports are a swapped TX/RX pair or a browning-out supply.

1. **Wiring, unpowered.** Continuity-check every line in §1 on both nodes.
   Confirm the crossover. Confirm both antennas are screwed on.

2. **Framing, no radio.** Jumper GPIO17 to GPIO18 with the E32 unplugged, then:

   ```
   pio run -t upload -t monitor
   > TEST        # frame comes straight back; rx_ok increments
   > TESTBAD     # rx_bad increments, rx_ok does not
   > STAT
   ```

   Cross-check the two codecs — these must print identical bytes:

   ```
   # firmware
   > TEST
   TEST tx AA 55 01 <seq> 0A EC 1E 00 <crc_lo> <crc_hi>

   # mast, anywhere, no hardware needed
   python3 mast/lora_bridge.py --selftest
   teleop seq=0 vx=10 vy=-20 wz=30 flags=0 -> AA 55 01 00 0A EC 1E 00 37 D6
   ```

   (Only `seq` and therefore the CRC differ; with `seq=0` they match exactly.)

3. **Config read-back.** Power both modules. On the rover `CFG?`, on the mast
   `python3 lora_bridge.py --config read`. Both must answer — `C0 00 00 1A 17 44`
   on a factory module. Then write and verify on both:

   ```
   > CFG!                                        # rover
   python3 lora_bridge.py --config write         # mast
   ```

   Both must now read back **`C0 00 00 1B 17 44`**. A module that will not
   answer at all is a wiring or supply problem, not a radio one.

   With one radio powered you can already confirm the transmit path, because
   AUX drops low while the module is busy and rises once the packet is in the
   RF chip. `TEST` watches that and says so:

   ```
   > TEST
   TEST tx AA 55 01 00 0A EC 1E 00 37 D6
   TEST: AUX low for 16 ms - frame transmitted
   ```

4. **Text at 1 m.** Before the framed protocol, confirm the RF path itself:
   `python3 lora_bridge.py --chat` on the mast against a rover flashed with
   `../e32-lora-chat`, or just watch the rover's `rx_bad` counter climb as the
   mast sends text. Either direction working proves the radios hear each other.

5. **Framed ping at 1 m.** `python3 lora_bridge.py --ping 200`.
   Target: ≥99 % answered, RTT p95 under 150 ms. High loss with the modules
   sitting next to each other means collisions — check that nothing on the rover
   transmits except in reply.

6. **Range walk.** Mast fixed, rover carried out in 25 m steps with `--ping`
   running at each stop. Write down loss and RTT per distance. That table is
   what sets the real failover thresholds; the ones currently in
   `lora_bridge.py` are guesses.

7. **Failure injection.** (a) Power off the rover's E32 mid-run — the mast must
   report `DOWN` within about a second. (b) Confirm the rover console prints
   `FAILSAFE` and zeroes the command; check it, do not assume it. (c) Walk out
   of range and back — the link must recover without restarting either side.

## Troubleshooting

- **Nothing received, ever** — RX/TX swapped on one end, or the two modules are
  on different channels/air rates. `CFG?` on both and compare.
- **Garbled data, random resets while transmitting** — the E32 is browning out.
  §2.
- **`CFG?` times out** — M0/M1 not reaching the module, AUX stuck low, or the
  supply sagging. AUX stuck low with the module unplugged is impossible here:
  the pin is pulled up in firmware.
- **`TEST: AUX never went low`** — the module is not seeing the frame at all.
  Its RXD is not connected to GPIO17, or it is unpowered. Note the pull-up
  means a module that is simply absent looks exactly like this.
- **`TEST: AUX stuck low`** — the module took the frame and never finished.
  Almost always the supply collapsing under the transmit burst.
- **`rx_bad` climbing while `rx_ok` stays flat** — the radios hear each other
  but the bytes are corrupt. Usually marginal signal, sometimes a UART baud
  mismatch between the two `SPED` bytes.
- **Works, but every other poll is missed** — reply timeout too tight for the
  air rate. Raise `--reply-timeout` on the mast, or lower the poll rate.
- **Short range** — check `CFG?` actually reads `44` in the OPTION byte and not
  `47`; a module left on `CFG!LOW` transmits at 21 dBm, roughly 8× less power.

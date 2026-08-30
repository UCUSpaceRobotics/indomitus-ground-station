# Outdoor range and throughput test

Every threshold in `link_monitor.py` and `lora_bridge.py` is a bench guess taken
at about a metre. Both files say so in their own comments. This is the procedure
that replaces those guesses with measurements.

**What you are producing:** a distance table for both links, and from it the
four constants that decide when the rover falls back to LoRa.

## Bench baselines to beat

Everything below is what the same gear does at ~1 m, so you can tell a range
effect from a broken setup.

> **The Wi-Fi column is stale as of 2026-08-23.** It was measured on the
> out-of-tree vendor driver and with a faulty antenna on the mast Alfa, and is
> not reproducible on the current setup. Both radios now run the mainline
> `rtw88` driver; current bench figures are 131–140 down / 93–113 up at 40 MHz,
> or 203–262 / 230 at 80 MHz, with 2.7 ms round trip. See
> [STARTUP.md](STARTUP.md). Re-measure and replace this column. The LoRa column
> is unaffected.

| | Wi-Fi (5 GHz ch36 HT40) | LoRa (433 MHz, 4.8 kbps air, 30 dBm) |
|---|---|---|
| Throughput | 170 Mbit/s down, 208 up | 168 B/s one way |
| Round trip | 4.9 ms | 240 ms (ESP32 rig) / 245 ms (Jetson) |
| Loss | 0 % | 0 % at ≤3 Hz |
| Usable rate | — | 3 Hz polls; 4 Hz fails outright |

The two links are four orders of magnitude apart. That is the point: Wi-Fi
carries the mission, LoRa carries "stop, and crawl somewhere I can see you".

---

## Before you leave the bench

Run all of it. Every one of these has bitten this project at least once.

```bash
# 1. Rover powered, AP actually up, and reachable.
ssh admin@10.44.0.1 'iw dev wlx00c0caba8237 link'      # expect SSID + signal
ssh admin@10.44.0.1 'ping -c3 10.42.0.1'               # expect 0% loss
```

If the Alfa shows `Not connected` / `NO-CARRIER`, the rover's AP is not
running — that is a rover-side problem and no amount of walking will fix it.

```bash
# 2. Both mast services up, both ports bound.
ssh admin@10.44.0.1 'systemctl is-active link-monitor lora-bridge; ss -lntp | grep -E "400[12]"'

# 3. Both radios on identical settings. Must both print C0 00 00 1B 17 44.
ssh admin@10.44.0.1 'sudo journalctl -u lora-bridge -b | grep "module config"'
#    ... and on the rover's ESP32 console: CFG?

# 4. iperf3 present on all three machines.
iperf3 --version                                        # GS PC
ssh admin@10.44.0.1 'iperf3 --version'                  # mast Pi
ssh -J admin@10.44.0.1 indomitus-rover@10.42.0.1 'iperf3 --version'   # rover
```

The mast checks itself now: `lora_bridge.py` reads the module's registers at
startup and logs `module config OK: ...`, or a warning naming the byte that is
wrong. That check exists because a module can quietly revert to the factory
2.4 kbps air rate across a power interruption, and the result — 100 % loss with
zero CRC failures — is indistinguishable from being out of range. It cost an
hour of chasing wiring that was fine.

The rover end cannot self-check: its M0/M1 are strapped low, so mode 3 is
unreachable and the module's registers can only be read from the ESP32 bench rig
with `CFG?`. If the rover has been power-cycled and the link is dead with no CRC
errors, suspect this first.

If the rover lacks iperf3 it has no internet either — fetch the arm64 `.deb`s
here and `dpkg -i` them, the same way the Pi got its copy (see
[README](README.md#dependencies)).

**Antennas on both E32s before anything is powered.** A 1 W PA into an open
port can destroy itself.

---

## Terminals

Four, all on the GS PC. Label them, you will be reading them at distance.

| | What | Command |
|---|---|---|
| T1 | **Both links, one line** | `python3 mast/watch_links.py --csv run1.csv` |
| T2 | mast Pi shell | `ssh admin@10.44.0.1` |
| T3 | rover shell | `ssh -J admin@10.44.0.1 indomitus-rover@10.42.0.1` |
| T4 | ROS container | `docker exec -it indomitus_ground_station bash` |

T1 is the one to photograph at each stop. It prints a line a second and the
`--csv` file is what you plot afterwards.

---

## Part A — Wi-Fi

The Wi-Fi hop is **Pi ↔ rover**. Test it directly so you are measuring the
radio and not the Ethernet in front of it.

```bash
# T3, on the rover - leave running for the whole test
iperf3 -s

# T2, on the Pi, at each distance
iperf3 -c 10.42.0.1 -t 10          # rover -> Pi  (download)
iperf3 -c 10.42.0.1 -t 10 -R       # Pi -> rover  (upload)
ping -c 20 10.42.0.1 | tail -2
iw dev wlx00c0caba8237 link | grep -E "signal|bitrate"
```

Then once, at the furthest distance that still works, check end to end — this
adds the Ethernet hop and is what the operator console actually experiences:

```bash
# T1 or any GS PC shell
iperf3 -c 10.42.0.1 -t 10
```

**Watch for the stall, not just the rate.** `link_monitor.py` has a detector for
a link that is associated, shows good signal, and moves no traffic — the failure
that took out the mast Ethernet on 2026-08-06. If `iw` looks healthy and iperf3
reads zero, that is the case, and T1 will show `WIFI DOWN  rx stalled`.

---

## Part B — LoRa

The manual tools need the serial port, so the service has to be out of the way:

```bash
# T2, on the Pi
sudo systemctl stop lora-bridge

lora_bridge.py --ping 60           # loss and round trip
lora_bridge.py --rate-sweep        # fastest poll rate that still works
lora_bridge.py --throughput 30     # one-way saturation, iperf-style

sudo systemctl start lora-bridge   # ALWAYS restart it before moving on
```

Forgetting the restart leaves the rover with no fallback and nothing on screen
to say so. If in doubt: `systemctl is-active lora-bridge`.

**Reading the three tools:**

- `--ping` is the headline: delivered percentage and round trip at this
  distance. Compare against 0 % / 240 ms.
- `--rate-sweep` finds the usable command rate. The cliff is set by the round
  trip, so it will move outward as range degrades. If 3 Hz stops passing, the
  poll rate in `lora_bridge.py` and the rover's failsafe timeout both need
  lowering together.
- `--throughput` floods one way and asks the rover's own counter what arrived.
  `into the module` vs `over the air` is the module's buffer overflowing; at
  short range they are equal.

`--throughput` sends STATUS frames rather than TELEOP on purpose — the rover
counts them but does not answer, so nothing transmits back into the flood. That
is why the number is capacity rather than contention.

---

## Part C — Failover

Two different things to test, and the order matters.

### C1 — the mechanism, without breaking anything

```bash
# T4, in the container
source /opt/ws/install/setup.bash
ros2 launch gs_comms comms.launch.py     # if not already running

ros2 param set /link_status_node force_path LORA      # relay over the radio
ros2 topic echo --once /link/active_path              # expect LORA
ros2 param set /link_status_node force_path WIFI      # back to Wi-Fi
ros2 param set /link_status_node force_path AUTO      # hand control back
```

Use a parameter, **not** `ros2 topic pub /link/active_path`. `link_status_node`
publishes that topic itself, so a hand-published value fights its publisher and
subscribers see the two alternate.

With `force_path LORA` set and the joystick panel connected, drive the rover on
the radio. Expect roughly 0.3–0.6 s between stick and movement and three updates
a second. Confirm it feels like crawling, not driving — if anyone is planning to
attempt a task on this link, this is the moment they find out.

### C2 — the real thing

Leave `force_path` on `AUTO`, keep LoRa in range, and walk the rover out of
Wi-Fi range. What should happen, in order:

1. T1 shows `WIFI DEGRADED`, then `WIFI DOWN`
2. about 1.5 s later (3 samples at 2 Hz) `/link/active_path` flips to `LORA`
3. commands keep working, slowly
4. walking back, Wi-Fi must hold `OK` for 15 samples before it flips back — that
   asymmetry is deliberate, so do not expect an instant recovery

```bash
# T4, to watch the decision itself
ros2 topic echo /link/active_path
ros2 topic echo /diagnostics --field status[0].message
```

Record **the distance at which the flip happened** and whether commands were
ever lost in the gap. That gap is the whole reason the fallback exists.

---

## Recording

One row per stop, per link. Fill it in on the spot.

| Dist (m) | LoS? | Wi-Fi sig | Wi-Fi down/up | Wi-Fi rtt | LoRa loss | LoRa rtt | LoRa max rate | LoRa B/s | Notes |
|---|---|---|---|---|---|---|---|---|---|
| 0 (bench) | y | −25 | 170 / 208 | 4.9 | 0 % | 240 | 3 Hz | 168 | baseline |
| 25 | | | | | | | | | |
| 50 | | | | | | | | | |
| 100 | | | | | | | | | |
| 200 | | | | | | | | | |
| … | | | | | | | | | |

Also note, because they change the answer more than distance does: line of
sight or not, what is between you (buildings, vehicles, wet ground), antenna
height and orientation at both ends, and the weather.

Keep going until each link fails, not until you get bored. **The interesting
number is where it stops working**, and a LoRa link in particular tends to work
and then stop rather than degrade gently.

---

## What to do with the numbers

The point of the walk is these four edits.

| Where | Constant | Currently | Set from |
|---|---|---|---|
| `mast/link_monitor.py` | `SIGNAL_DEGRADED_DBM` / `SIGNAL_DOWN_DBM` | −75 / −85 | the signal at which Wi-Fi throughput actually collapsed |
| `mast/link_monitor.py` | `LOSS_DEGRADED_PCT` / `RTT_DEGRADED_MS` | 20 % / 150 ms | loss and RTT one stop before failure |
| `mast/lora_bridge.py` | `LOSS_DEGRADED_PCT` / `MISSES_DOWN` | 20 % / 5 | LoRa loss at the last working distance |
| `mast/lora_bridge.py` + firmware | poll rate / `FAILSAFE_TIMEOUT_MS` | 3 Hz / 1000 ms | the sweep's cliff at maximum range |

The failsafe timeout must stay at about three missed polls. Change the rate and
you must change it too — they are one decision, in two files.

Update the `### Measured` tables in [README.md](README.md) and the
[rover firmware README](../microcontrollers_indomitus/esp32s3_lora_rover/README.md#measured--bench-2026-08-15-both-modules-on-a-desk),
and delete the "no range test has been completed" warnings once it has been.

---

## Gotchas

- **Restart `lora-bridge` after every manual LoRa test.** The manual tools and
  the service both want `/dev/ttyAMA0`.
- **The Pi has no internet.** Anything you need installed, fetch here and `scp`
  the `.deb` across.
- **`force_path` survives nothing.** It is a live parameter, not a saved one;
  restarting the node returns it to `AUTO`. That is deliberate — nobody should
  discover a forced path days later.
- **30 dBm is the default.** Confirm the frequency rules before a public field
  test; `lora_bridge.py --config low` and the firmware's `CFG!LOW` drop both
  ends to 21 dBm, and both ends must match.
- **Cold boot takes a moment.** Both services lose the bind race against netplan
  once and restart 5 s later — `NRestarts=1` is expected, not a fault.

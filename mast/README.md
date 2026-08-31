# The comms chain — GS PC ↔ mast Pi ↔ rover

The mast is a Raspberry Pi 5 ("GSRapberry") at the top of the ground-station
mast. It runs **no ROS at all** — it is a dumb appliance exporting raw TCP
services, and everything that needs a ROS graph runs in the Humble container on
the GS PC where it can be debugged on a desk instead of up a mast.

Two jobs: route between the wired ground-station link and the rover's Wi-Fi, and
own the LoRa radio that takes over when that Wi-Fi dies.

This file is the reference for the whole link, including the rover-side network
config, because **none of the rover-side config exists in any repository** — it
lives only on the Jetson's SD card. See [What lives where](#what-lives-where).

> Both Alfa adapters were moved to the mainline `rtw88` driver on 2026-08-23.
> [STARTUP.md](STARTUP.md) covers the current driver/AP setup, measured
> throughput, recovery paths, and — importantly — **which of those settings do
> not survive a reboot.** Read it before a field test.

## Topology

```
GS PC   enp2s0        10.44.0.10/24   route 10.42.0.0/24 via 10.44.0.1
          |             point-to-point through the TP-Link POE160S injector
Pi      eth0          10.44.0.1/24    \  99-mast.yaml
Pi      wlx00c0…8237  10.42.0.2/24    /  Alfa AWUS036ACH, client of the rover's AP
Rover   wlx00c0…86c1  10.42.0.1/24       Alfa AWUS036ACH, AP (hostapd)
                                         route 10.44.0.0/24 via 10.42.0.2

Pi      E32-433T30D   433 MHz  ← LoRa fallback →  rover's E32
```

The Pi is an L3 router between the two /24s and has **no default route** on
either interface — see the comments in `99-mast.yaml`, which are the real
documentation for the network config.

Because the Pi routes rather than bridges, **multicast DDS discovery cannot
cross it**. That is why the ground station needs static discovery peers; see
[ROS 2 and DDS](#ros-2-and-dds).

## Addresses and access

| Host | User | Interface | Address | Notes |
|---|---|---|---|---|
| GS PC (`gs-pc`) | `gs` | `enp2s0` | `10.44.0.10/24` | mast link — **currently the live port** |
| | | `eno1` | `10.44.0.10/24` | second port, same config, whichever is cabled |
| | | `wlp3s0` | `10.20.18.46/24` | home Wi-Fi + internet, **not** the rover path |
| Mast Pi (`GSRapberry`) | `admin` | `eth0` | `10.44.0.1/24` | to GS PC, via PoE injector |
| | | `wlx00c0caba8237` | `10.42.0.2/24` | Alfa, client of the rover AP |
| | | `wlan0` | — | onboard Wi-Fi, down; remove before competition |
| Rover (`indomitus-rover`) | `indomitus-rover` | `wlx00c0caba86c1` | `10.42.0.1/24` | Alfa, **AP** (hostapd) |
| | | `enP1p1s0` | `10.45.0.51/24` | NM `rover-lifeline`, static — **wired recovery path, see below** |
| Other Wi-Fi clients | — | — | `10.42.0.50`–`10.42.0.150` | DHCP from `rover-ap-dhcp.service` |

Anyone joining the AP gets a lease from the rover. That server is
`rover-ap-dhcp.service` (dnsmasq, DHCP only — DNS disabled so it cannot collide
with `systemd-resolved`), bound **exclusively** to the AP radio so it can never
hand addresses out on the ground station's own wired subnet.

MAC addresses (interface names are derived from them, so they are stable):

| Device | MAC |
|---|---|
| Pi `eth0` | `88:a2:9e:c6:55:46` |
| Pi `wlan0` | `88:a2:9e:c6:55:47` |
| Pi Alfa | `00:c0:ca:ba:82:37` |
| Rover Alfa | `00:c0:ca:ba:86:c1` |

### SSH

```
ssh admin@10.44.0.1                    # mast Pi
ssh indomitus-rover@10.42.0.1          # rover, direct (needs the return route)
ssh -J admin@10.44.0.1 indomitus-rover@10.42.0.1
```

Keys are installed GS PC → Pi, Pi → rover, and GS PC → rover.

**Out-of-band recovery** if the Pi's IPv4 config is wrong — this works over the
Ethernet cable with no working IP config at all:

```
ssh admin@fe80::8aa2:9eff:fec6:5546%<gs-iface>
```

The rover now has an equivalent: the wired `rover-lifeline` path described
under [Wired recovery path](#wired-recovery-path). **Bring it up before
touching the radio or the Wi-Fi driver**, and the whole class of "the AP did
not come back and the rover is now a brick on a bench" problems disappears.

Without it the only way in is physical, so any unattended radio change must
still arm a rollback *before* it changes anything; see
`/usr/local/sbin/rover-ap-rollback.sh` and `rover-ap-apply.sh`. Be aware what
that rollback does and does not cover: it stops and reloads services and
modules, so it recovers a *configuration* failure. It cannot recover a driver
whose module index was never written, or an adapter that needs a bus reset —
for those the timer fires, reports success, and the link stays dead.

### Routing

| Host | Route | Set by |
|---|---|---|
| GS PC | `10.42.0.0/24 via 10.44.0.1` | NM profiles `gs-mast` / `gs-mast-p2`, `ipv4.never-default yes` |
| Pi | forwards both ways, no default route | `99-mast.yaml` + `ip_forward=1` |
| Rover | `10.44.0.0/24 via 10.42.0.2` | `rover-ap.service` `ExecStartPre` |

The rover's return route is the single thing that makes GS PC → rover work at
all. Without it the rover receives every packet and its replies leave by a
default gateway that does not exist, which looks exactly like a dead link.

> A stale copy of that route also sits in `Hotspot.nmconnection` as
> `route1=10.44.0.0/24,10.42.0.2`. It is **inert** — NetworkManager no longer
> manages the radio (see below) — but it is harmless and worth leaving as a
> fallback if the AP is ever handed back to NM.

### Wired recovery path

Earlier this Jetson had a second NIC, `enP8p1s0`, which took a DHCP lease on
`10.44.0.0/24` — the same subnet as the mast link — and needed careful metric
ordering to stop the rover replying down a port the ground station could not
reach. **That NIC no longer exists.** The Jetson was reflashed to L4T R36.4.3
(2026-08-21) and only `enP1p1s0` enumerates now; the collision is gone with it.

If you plug a cable into the *other* physical port you will get a negotiated
1000 Mbit/s link that transmits nothing at all — carrier comes from the PHY,
but there is no driver behind it. `rx_packets: 0` on the ground-station side
with a healthy `Link detected: yes` is the signature.

The remaining port is now the rover's **out-of-band recovery path**, which is
worth more than the bench convenience it replaced:

| Host | Profile | Address |
|---|---|---|
| Rover `enP1p1s0` | `rover-lifeline` (autoconnect, `never-default`) | `10.45.0.51/24` |
| GS PC `eno1` | `rover-recovery` (**autoconnect off**, on demand) | `10.45.0.1/24` |

`eno1` normally runs `gs-mast` like the mast port. To use the wired path:

```
nmcli connection up rover-recovery      # on the GS PC
ssh indomitus-rover@10.45.0.51
nmcli connection up gs-mast             # put eno1 back afterwards
```

It is deliberately not autoconnect: two profiles fighting over `eno1` is how
you lose the mast link by accident. Note that `gs-mast` has autoconnect and
priority 100, so it *will* reclaim `eno1` on any carrier flap — if the wired
path goes quiet mid-session, check which profile is actually active before
debugging anything else.

## RF configuration

| Setting | Value |
|---|---|
| SSID | `ERC_UCUSpaceRobotics_A` (`_B` is the spare slot) |
| Security | WPA2-PSK, CCMP |
| Band / channel | 5 GHz, ch **36** (5180 MHz) |
| Width | **40 MHz**, HT40+ (secondary ch40), centre 5190 |
| Mode | 802.11ac **VHT40** — see below |
| TX power | 20 dBm |
| Regulatory domain | `UA` via `wireless-regdom.service` |

**Channel 36 is deliberate.** The kernel boots in regulatory domain `00`
("world"), which flags almost every 5 GHz channel `no IR` — the radio may
associate there but may not *start an AP*. Under domain `00`, channel 36 is the
only one that works. Picking it means a regulatory-domain failure degrades to
"wrong TX power" instead of "AP does not start and the rover is unreachable".

**Before Kraków:** change `UA` to `PL` in
`/etc/systemd/system/wireless-regdom.service`, then
`systemctl restart wireless-regdom`. Both are ETSI and both permit ch36.
ERC assigns the actual channel at the RF Check, and because netplan matches on
SSID alone, **that is a rover-side change only** — the Pi follows automatically.

### VHT40 does work — the note that said otherwise was wrong

This section previously said the `rtl8812au` driver does not implement VHT in
AP mode, so the link could only reach HT40 MCS15 (300 Mbit/s PHY) and never
VHT40 MCS9 (400 Mbit/s). **That is not correct.** On 2026-08-21
`rover-ap-apply.sh` selected its `vht40` variant and the mast Pi associated at
a **400 Mbit/s** PHY rate — the VHT path the config always asked for.

The earlier reading was most likely taken while NetworkManager was still
contending for the radio (see below), which is enough to make hostapd fall back
to the `ht40` variant and look like a driver limitation. If you see 300 rather
than 400, check `cat /run/rover-ap-variant` before blaming the chipset: it
records which config actually came up.

### The rover Nano's Alfa is still in USB 2.0 mode

Measured 2026-08-29 on the Nano that replaced the Orin: **57 Mbit/s down,
54 up** (`iperf3`, cameras idle), with three MJPEG camera streams eating
**22 Mbit/s — about 39% of the downlink**.

The adapter is at **480M**, and `/etc/modprobe.d/` on that Nano has **no options
set at all**: `rtw_switch_usb_mode=0`. So the single documented fix in the
section below has simply never been applied to this host. `rtw_power_mgnt` is
already 0, so power saving is not the problem here — the USB mode is.

`mast/8812au-usb3.conf` is the drop-in, and `mast/alfa-usb3.sh --apply` installs
it. **Do not copy it into place by hand.** Switching USB mode re-enumerates the
adapter, the adapter is the only route into this rover, and unlike the Orin this
Nano has **no wired lifeline** (`10.45.0.51` does not answer). The script
therefore arms a detached self-revert on the Nano *before* it changes anything,
and only `--keep` cancels it.

Two things differ from the old rover and matter for recovery:

- the AP here is **NetworkManager**, connection `Hotspot` (mode=ap,
  autoconnect=yes) — not `hostapd`/`rover-ap.service`. The `10.44.0.0/24` return
  route is a static route *on that connection*, so both come back together when
  NM re-activates it;
- **do not reach for the morrownr driver on this box.** Its repo now requires
  kernels **5.10-7.0** and tests gcc 12-15; this Nano is **4.9.201-tegra** with
  gcc 7.5. The repo `mast/README.md` used to name, `morrownr/8812au-20210629`,
  no longer exists — it was folded into `8812au-20210820`. The 52 → 108 Mbit/s
  figure below was measured on the **Orin** (L4T R36, kernel 5.15) and is not a
  promise about this hardware. DKMS itself is healthy here
  (`realtek-rtl88xxau 5.6.4.2` is built against 4.9.201-tegra), so the toolchain
  is not the obstacle — the source is.

Judge the result by stability, not just by the link coming back: SuperSpeed on
this board is what wrecks the cameras, and an unstable radio link is a failure.

### Two things that quietly cost most of the throughput

**NetworkManager must not manage the AP radio.** Without
`/etc/NetworkManager/conf.d/99-rover-ap-unmanaged.conf`, NM keeps trying to
bring up its own `Hotspot` profile on the same interface and fails with
`supplicant-timeout` every 20 s. The failure mode is nasty: hostapd reports
`active`, `iw dev` shows `type AP` on the right channel, and **nothing
beacons** — a client scan does not see the BSSID at all. Check
`nmcli -t -f DEVICE,STATE device status`; the AP radio must read `unmanaged`.

**Power saving must be off on both ends — including the AP.** Both radios come
up with `power_save: on` by default, which costs roughly 4x throughput and
turns 5 ms RTT into 81 ms average with 236 ms spikes. It is now disabled in two
places, both persistent:

- rover — `ExecStartPost=` in `rover-ap.service` runs `iw … set power_save off`
- Pi — `rtw_power_mgnt=0` in `/etc/modprobe.d/8812au.conf`

`iw dev <iface> get power_save` on both ends is the first thing to check when
the link is up, the signal is good, and it still feels slow.

### The Alfa must be switched into USB3 mode — it is not a port problem

`cat /sys/bus/usb/devices/<dev>/speed` tells you the mode: **480 Mb/s is USB2,
5000 Mb/s is USB3**.

**Do not chase this by moving the adapter between ports.** The Jetson's hub is a
Microchip **USB2744 / USB5744** — *one physical chip* whose USB2 and USB3 halves
enumerate as two separate devices (`1-2` at 480M and `2-1` at 5000M). A device
that appears under `1-2.x` at 480 Mb/s is not in a "USB2-only port"; it is an
adapter that has not been told to switch modes. Moving it to another port just
produces another `1-2.x` path at 480 Mb/s.

The AWUS036ACH enumerates in USB2 mode by default and must be switched
explicitly, via `/etc/modprobe.d/8812au.conf`:

```
options 88XXau rtw_switch_usb_mode=1 rtw_led_ctrl=1
options 8812au rtw_switch_usb_mode=1 rtw_led_ctrl=1
```

Both module names are listed because the rover and the Pi have historically run
different builds of this driver (`88XXau` is the aircrack-ng fork, `8812au` is
morrownr's) and the option only applies to the module actually loaded. Reload
the driver to apply; the adapter re-enumerates and its device path moves from
`1-2.x` to `2-1.x`.

> Note this was previously set to `rtw_switch_usb_mode=0` on the rover, which is
> why it sat on USB2. The Pi has always had `=1`, which is why its adapter has
> always shown 5000 Mb/s — that asymmetry is the quickest way to tell the two
> configurations apart.

### Measured

Bench range (~1 m, −21 to −29 dBm), 2026-08-13, `iperf3 -t 8`:

| Configuration | Down | Up | RTT |
|---|---|---|---|
| 2.4 GHz ch1, HT20 (NM) | 71.6 | 71.5 | 6.1 ms |
| 5 GHz ch36, HT20 (NM) | 82.4 | 103 | 5.1 ms |
| **5 GHz ch36, HT40 (hostapd)** | **170** | **208** | 4.9 ms |

Re-measured 2026-08-21 after the Jetson was reflashed, `iperf3 -t 8`, −28 dBm,
AP on **VHT40** (400 Mbit/s PHY) — but with the rover's Alfa on the **USB2**
hub, which is the cap:

| Configuration | Down | Up | RTT |
|---|---|---|---|
| VHT40, USB2, power save **on** | 43 | 61 | 81 ms avg, 236 ms peak |
| VHT40, USB2, power save **off** | 44 | 78 | 3.9 ms |
| VHT40, **USB3**, aircrack `88XXau`, Pi→rover | 52 | 61 | — |
| VHT40, **USB3**, morrownr `8812au`, Pi→rover | **105–112** | 55 | median 6 ms |
| VHT40, **USB3**, morrownr `8812au`, GS→rover | 57 | 80 | median 6 ms |

Power saving costs about a third of the throughput and all of the latency
stability — fix that first, always.

**The driver build matters more than anything else measured here.** Swapping
the rover from the aircrack-ng `88XXau` v5.6.4.2 (2019) to morrownr `8812au`
v5.13.6 roughly **doubled** the wireless hop, 52 → ~108 Mbit/s, with no other
change. Prefer morrownr on both ends; it is what the original figures were
taken on. Both are kept installed via DKMS so either can be loaded:

```
modprobe -r 8812au && modprobe 88XXau     # fall back
modprobe -r 88XXau && modprobe 8812au     # preferred
```

Do the swap **with the wired recovery path already up** — the driver owns the
only radio, so a failure with no second path is unrecoverable without physical
access.

### Check for a busy CPU before blaming the RF

`nvpmodel_indicator.py`, the GNOME power-mode tray applet, was found spinning at
**110% CPU** (one full core) for an entire uptime, because `nvpmodel` cannot
read `/sys/devices/platform/gpu.0/fbp_pg_mask` on this image and it retries
forever. It inflates latency and suppresses throughput while every RF metric
looks perfect. Killed and disabled via `Hidden=true` in
`/etc/xdg/autostart/nvpmodel_indicator.desktop`.

The rover also boots a full GNOME session it has no use for. Worth stripping.

### Still unexplained: the residual latency spikes

Even with morrownr, USB3, power management off at both the `iw` and driver
levels, and an idle CPU, latency is **bimodal**: median ~6 ms but with a long
tail. Measured 2026-08-21:

| Path | Mean | Median | Over 20 ms | Max |
|---|---|---|---|---|
| GS → Pi (wired only) | 0.25 ms | — | 0% | 0.4 ms |
| Pi → rover (wifi hop) | 22.5 ms | ~3 ms | 8% | 256 ms |
| GS → rover (forwarded) | 32.8 ms | 6 ms | 35% | 156 ms |

The wired hop is perfect, so the tail originates at the wireless hop and the
Pi's forwarding path amplifies it. Note the *reply* direction is clean — the
rover answers the Pi's pings at a flat 2.4 ms — so it is not simply "the radio
is slow". **Do not trust a mean here**; always look at the per-packet
distribution, which is how the CPU-spin above was found after three separate
mean-based readings pointed at the wrong things.

This is the most likely cause of a future "the link is up but teleop feels
laggy" report, and it is unresolved.

## Services

| Port | Program | Serves |
|---|---|---|
| tcp/4001 | `lora_bridge.py` | LoRa link metrics as JSON lines; accepts teleop commands on the same socket |
| tcp/4002 | `link_monitor.py` | Wi-Fi link metrics as JSON lines |

Both bind `10.44.0.1` — the mast link only, never `0.0.0.0`. Both emit one JSON
object per line with the same `state` vocabulary (`OK` / `DEGRADED` / `DOWN`)
plus a `reasons` list, so the consumer can read either stream with one parser.

`link_status_node` on the GS PC consumes 4002 and decides, with asymmetric
hysteresis, whether the command path should be Wi-Fi or LoRa. It publishes that
on the latched `/link/active_path`; `lora_gateway_node` acts on it. Neither the
decision nor the relay can be forced by hand-publishing that topic - use
`ros2 param set /link_status_node force_path LORA|WIFI|AUTO`, which exists for
exactly that and does not fight the node's own publisher.

`watch_links.py` shows both links on one line and is what to run during a range
test; see [FIELD-TEST.md](FIELD-TEST.md).

```
python3 mast/watch_links.py --csv run1.csv
14:40:54  WIFI OK  sig -35 dBm  loss 0%  rtt 15 ms  300 Mb | LORA OK  loss 0%  rtt 241 ms  rover ok
```

> **Port conflict:** `ser2net` was installed and enabled on the Pi, bound to
> `10.44.0.1:4001` — the original raw-serial export that `lora_bridge.py`
> replaces. Both want the same port and only one can hold it, and the symptom
> is `OSError: [Errno 98] Address already in use` at startup. It has now been
> disabled (`sudo systemctl disable --now ser2net`, 2026-08-15); re-enable with
> `sudo systemctl enable --now ser2net` if the raw export is ever wanted back.
> It only opened `/dev/ttyAMA0` on connection, which is why it never blocked
> `--ping`/`--config` — only the listening socket collided.

## ROS 2 and DDS

| Setting | Value |
|---|---|
| `ROS_DOMAIN_ID` | **90**, both ends |
| RMW | `rmw_fastrtps_cpp` (Humble default) |
| GS container | `indomitus_ground_station`, `network_mode: host`, `ipc: host` |
| Rover container | `rover_prod`, `network_mode: host` |

> The rover-core README's table listing `132` for the Jetson container is **stale
> documentation**. The running container reports 42, verified 2026-08-13. A
> domain mismatch fails completely silently — an empty topic list, no error.

Because the Pi routes, multicast SPDP discovery never reaches the rover, and
Fast DDS also advertises locators for every interface it can see — including
this laptop's home Wi-Fi, which the rover cannot route to. Both are fixed by
`docker/fastdds_rover_link.xml`, generated by `docker/gen-dds-profile.sh`:

- `interfaceWhiteList` → `10.44.0.10`, so only the mast link is advertised
- `initialPeersList` → `10.42.0.1` (the rover) **and** `239.255.0.1:17900`

That multicast locator is not redundant. Fast DDS seeds the peer list with the
multicast locator by default, and defining the element *replaces* that default
rather than adding to it — so listing only the rover silently breaks discovery
between nodes on the ground station itself. The symptom is `ros2 node list`
returning nothing while the nodes are plainly running.

The profile bakes in literal addresses (Fast DDS 2.6 accepts no CIDR), so
**re-run `./docker/gen-dds-profile.sh` whenever either end's address changes.**

Both failure modes look like success at first glance: discovery reports correct
types, QoS and publisher counts while `ros2 topic hz` is silent on every topic.

### The Nano's camera is deliberately outside all of this

The Jetson now at `10.42.0.1` is a **Nano 4GB on JetPack 4.5.1** — Ubuntu
18.04.6, kernel `4.9.201-tegra`, L4T R32.5.2, login `jetson`. **ROS 2 Humble has
no binaries for 18.04**, so nothing in this section applies to it: no
`v4l2_camera_node`, no DDS profile, no topic to whitelist. This is the whole
difference from the Orin NX, where the arducams were ordinary ROS topics.

The cameras are **Arducam B0495 (USB3 2.3MP)**, driverless UVC, so `uvcvideo`
handles them. `mast/start-cameras.sh` discovers every capture-capable
`/dev/video*`, deploys `mast/camera_mjpeg_server.py`, and starts **one server process per
camera** — first on **port 8090**, each further one on the next port up. Not
8080: `web_video_server` owns that on the GS. A UI camera row takes each URL
directly; see "Cameras outside ROS" in `ui/README.md`.

One process per camera on purpose: these cameras wedge (see the USB note below),
and a process per feed means a wedged camera takes down only its own stream and
can be restarted with `--dev /dev/videoN` without interrupting the others. It
also puts each encode on its own core. Two cameras at 960x600@10 measured ~22%
and ~21% of a core.

Node numbers are **not stable across replugs** — `/dev/video0` and `/dev/video1`
swap around — so the script rediscovers them every run rather than remembering
them, and reports each camera's USB path and link speed alongside its port.

**USB speed decides what the camera offers**, and the two lists share no frame
rate, which is a trap when re-cabling:

| Speed | Modes |
|---|---|
| 480M (USB 2.0) | 960x600 @ 10 — and nothing else |
| 5000M (USB 3.0) | 1920x1200 @ 50/30/15, 960x600 @ 80/60/30/15 — **no 10** |

Neither offers MJPEG, so the Nano always JPEG-encodes in software. That encode
is single-threaded: 960x600@10 measured **23% of one core**, so 960x600@30 is
~70% and 1920x1200 at any usable rate is out of reach without NVJPEG or a
threaded encoder. `camera_mjpeg_server.py` snaps a requested mode onto one the camera
actually has and logs the substitution, so a stale `--fps` degrades instead of
failing silently.

> **SuperSpeed does not survive an external hub chain here.** Behind two
> external hubs (`2-1.2.3.2`) the camera enumerates at 5000M and then fails
> every transfer with `-71` (EPROTO). Two symptoms, depending on how far it
> gets: `Failed to set UVC probe control` then `can't set config #1`, leaving no
> `/dev/video*` node at all; or, if it does open, `Non-zero status (-71) in
> video completion handler` — which reaches the operator as **flat green frames
> with the occasional real one**, because an unfilled YUYV buffer decodes to
> green. Unbind/rebind and an `authorized` re-enumeration do **not** clear a
> wedged device; only a physical power cycle does.
>
> **Read the sysfs path, not the socket you think you used.** This is an NVIDIA
> Jetson Nano Developer Kit, and its four USB3-A sockets hang off a VIA Labs hub
> soldered to the board (`2109:0817`, at `2-1`). So a camera plugged straight
> into the Nano still reads **`2-1.N` — one dot**, and that is what "direct"
> looks like here. More dots than that are external hubs:
>
> ```
> 2-1              VIA Labs      onboard, unavoidable
>  └─ 2-1.2           Realtek    external hub #1
>      └─ 2-1.2.3        Realtek external hub #2
>          └─ 2-1.2.3.2  Arducam
> ```
>
> `start-cameras.sh` counts the external hubs per camera and warns. Note the same
> chain at 480M was completely stable — the whole rover harness (Alfa, CAN,
> cdc_acm) runs through it on bus 1 — so the fault appears only once SuperSpeed
> is negotiated.
>
> **Removing the hubs is not sufficient.** A camera plugged straight into the
> Nano at `2-1.1`, hub-free at 5000M, still logged 824 `-71` errors in one boot
> with the kernel repeatedly issuing `reset SuperSpeed USB device`. It served
> frames — 19 fps against a requested 30, with visible errors — rather than
> failing outright, which is a worse failure mode because it looks like it
> works. Tegra's xHCI and this Cypress FX3 bridge are simply unreliable together
> at SuperSpeed. After enough resets the device fell back to 480M on bus 1 on
> its own, and has been faultless since: **0 errors, 9.8 fps, and the `-71`
> counter frozen.**
>
> **So: run these cameras at USB 2.0 here.** The cost is real but small —
> 960x600@10 instead of @30 — and 960x600 is what the rest of the rover's
> arducams run anyway. USB 3.0 on this board is not worth the frame rate.
>
> `start-cameras.sh` therefore defaults every camera to the USB 2.0 mode, and
> warns on any camera that came up at 5000M. It cannot *force* the link down:
> kernel 4.9 on this Tegra exposes no per-port SuperSpeed disable, `authorized`
> toggling just re-enumerates at 5000M, and unbinding the hub tree drops the
> Wi-Fi link with it (the hubs are one device presenting on both buses — that
> mistake cost a reboot). **Link speed is decided by the cable and the socket.**

> **A wedged SuperSpeed camera leaves a ghost `/dev/video*` behind.** When the
> device goes dark without a clean disconnect, the kernel never tears its node
> down, so it keeps answering descriptors from cache while `VIDIOC_STREAMON`
> returns EIO. The same physical camera then re-appears on the USB 2.0 wires as
> a *second* node — three cameras, four nodes, the ghost and the real one on the
> same physical socket (`2-1.2.3.2` and `1-2.2.3.2` are bus 2 and bus 1 views of
> one port; compare `devnum`, the ghost is much older).
>
> This is worth more than tidiness: the ghost consumes a port, which shifts
> every camera after it by one and silently breaks the UI tile mapping. So
> `start-cameras.sh` requires one real captured frame from a node before it will
> serve it, rather than trusting enumeration.

> **Enforcing USB 2.0: `mast/99-arducam-no-superspeed.rules`.** Deauthorising a
> camera's SuperSpeed instance (`echo 0 > .../authorized`) removes its node
> cleanly, leaves the 480M feeds untouched and does not disturb the link —
> unlike unbinding the hub tree, which drops the Wi-Fi with it. The udev rule
> does that automatically for `04b4:4950` at `ATTR{speed}=="5000"`.
>
> Proven: it removes the stale SuperSpeed twin a wedged camera leaves behind.
>
> **Also now proven, and it is the negative: a camera whose *only* enumeration
> is SuperSpeed does NOT re-train at High Speed. It just vanishes.** Measured
> 2026-08-30 on `2-1.2.1`, a B0495 behind one external Realtek hub, which had
> been failing with `uvcvideo: Failed to set UVC probe control : -71` and 426
> `-71` errors in that boot. With the rule installed the camera enumerated at
> 5000M, udev set `authorized=0`, the node went away — and no 480M instance ever
> appeared, on either bus. So the rule cannot rescue a camera in this state; it
> only cleans up ghosts.
>
> There is no software fix from here: this kernel has no per-port SuperSpeed
> disable, so the link speed is decided by the cable and the socket. Use a USB
> 2.0 cable, or a socket with no external hub in the path. `echo 1 >
> .../authorized` puts the (still broken) node back if you want the previous
> state, and the rule is one file to delete.
>
> Note the node numbers move across reboots — `/dev/video0` was `1-2.3` before
> one reboot and `1-2.1` after — so read the USB path, never the node number,
> when deciding which camera is which.

`mjpg-streamer` was tried first and rejected. It builds here — `libv4l-dev` is
the non-obvious dependency — but segfaults inside `input_uvc` before it opens
the device. Its one advantage, relaying MJPEG untouched, does not apply to a
camera that offers no MJPEG. `camera_mjpeg_server.py` needs nothing installed: the OpenCV
and numpy that ship with JetPack are enough, so it needs no apt and no sudo.

If that camera ever has to be a real ROS topic, the route is a container, and
the image is `dustynv/ros:humble-ros-base-l4t-r32.7.1` — Humble built from
source on an L4T r32 base. Do **not** reach for a stock Jammy image: on kernel
4.9 it dies with `error adding seccomp filter rule for syscall clone3`, which on
JetPack 4.6 also appears after an `apt upgrade` bumps docker, and is pinned back
with `docker.io=20.10.7-0ubuntu1~18.04.2`.

## What lives where

Not all of this is in version control. The rover-side config is in **no**
repository at all.

### Mast Pi

| Path | Source |
|---|---|
| `/etc/netplan/99-mast.yaml` | `mast/99-mast.yaml` |
| `/usr/local/sbin/link_monitor.py` + `link-monitor.service` | `mast/link_monitor.py` |
| `/usr/local/sbin/eth0-ring-watchdog` + `.service` | — resets `eth0` on an RX-error storm |
| `/etc/ser2net.yaml` | — superseded by `lora_bridge.py`, see conflict above |
| `/boot/firmware/config.txt` | — `usb_max_current_enable=1`, `gpio=23,24=op,dl` |
| `/etc/modprobe.d/8812au.conf` | — `rtw_switch_usb_mode=1 rtw_led_ctrl=1` |
| `/etc/apt/sources.list.d/ubuntu.sources` | — `noble-updates` added; without it apt cannot resolve deps |

`usb_max_current_enable=1` is load-bearing: the Alfa draws ~800 mA in USB3 mode,
above the Pi 5's 600 mA default cap.

### Rover Jetson — **nothing here is in a repo**

| Path | Purpose |
|---|---|
| `/etc/systemd/system/wireless-regdom.service` | sets regdomain before the AP starts |
| `/etc/systemd/system/rover-ap.service` | AP + address + return route |
| `/etc/hostapd/rover-ap.conf` | live config (copy of one variant below) |
| `/etc/hostapd/rover-ap-vht40.conf` | 40 MHz, requests 802.11ac |
| `/etc/hostapd/rover-ap-ht40.conf` | 40 MHz, 802.11n only — fallback |
| `/etc/NetworkManager/conf.d/99-rover-ap-unmanaged.conf` | keeps NM off the radio |
| `/usr/local/sbin/rover-ap-apply.sh` | tries VHT40 → HT40 → rollback |
| `/usr/local/sbin/rover-ap-rollback.sh` | hands the radio back to NetworkManager |
| `/etc/systemd/system/rover-ap-dhcp.service` | DHCP for AP clients (`BindsTo` the AP) |
| `/etc/dnsmasq-rover-ap.conf` | pool `10.42.0.50–150`, DNS disabled, AP interface only |
| `/etc/systemd/system/bluetooth-auto-let-connect.service` | BLE auto-trust/reconnect for the gamepad |
| `/usr/local/bin/bluetooth_let_connect.py` | the script it runs |
| `/usr/local/sbin/wifi-apply5.sh`, `wifi-rollback.sh` | NM-era 5 GHz switch, superseded by `rover-ap-apply.sh` |
| `/etc/udev/rules.d/80-can.rules` | **500 kbit/s, no `restart-ms`** — diverges from the repo copy, see below |
| `/etc/udev/rules.d/99-rplidar-s2.rules` | `/dev/rplidar-s2` symlink, mode 0666 |
| `/etc/udev/rules.d/99-slabs.rules` | 0666 on VID `2b03`/`04b4`, hidraw |
| `/etc/modprobe.d/btusb.conf` | `enable_autosuspend=0` — the gamepad drops without it |
| `/etc/modprobe.d/8812au.conf` | blacklists in-kernel `rtw88_8812au`; sets `rtw_switch_usb_mode`/`rtw_led_ctrl` |
| out-of-tree `8812au` (morrownr, **preferred**) + `88XXau` (aircrack-ng, fallback) | DKMS, both installed; **rebuild after any kernel change** |
| `/etc/xdg/autostart/nvpmodel_indicator.desktop` | `Hidden=true` — the stock applet spins at 110% CPU |
| out-of-tree `gs_usb_adapter` CAN driver | DKMS-style build from a patched `socketcan_gs_usb` clone |
| `/usr/local/zed/settings/SN32888826.conf` | ZED factory calibration, **serial 32888826** |
| `~/mapir_ws/config/mapir_camera.yaml` | MAPIR camera params; records which pixel formats segfault |

> **`80-can.rules` is not the repo's copy.** The Jetson runs **500 kbit/s with
> no `restart-ms`**, deliberately, so a bus-off latches for inspection instead
> of auto-recovering. `indomitus-rover-core/system/rules.d/80-can.rules` still
> says `bitrate 1000000 restart-ms 100`, and `system/setup.sh --can` will
> overwrite the Jetson's version. Decide which is correct and fix the repo.

`hostapd` 2:2.10-6ubuntu2.4 was installed from a hand-carried `.deb` — the
Jetson has no default route, so `apt install` cannot reach an archive. The
package's **own `hostapd.service` is masked**; leaving it enabled makes it race
`rover-ap.service` for the same radio on boot.

NM's `shared` mode used to provide the AP, the IP address *and* the return
route. hostapd only does the first, so `rover-ap.service` does the other two.
Miss that and the AP comes up perfectly with no address on it.

### GS PC

NetworkManager profiles `gs-mast` (`eno1`) and `gs-mast-p2` (`enp2s0`): both
`ipv4.method manual`, `10.44.0.10/24`, `ipv4.never-default yes`, autoconnect
priority 100. No NAT anywhere — NAT would rewrite the addresses DDS advertises
inside its discovery payload and break ROS in a way that is painful to debug.

## Deploying

Both scripts are stdlib + pyserial: nothing to build, nothing to break on a
kernel upgrade. Both run as systemd services, and both unit files live in this
directory so the mast can be rebuilt from the repository rather than from
whatever happens to be on the SD card.

```
scp mast/link_monitor.py mast/lora_bridge.py mast/lora_frame.py \
    mast/link-monitor.service mast/lora-bridge.service admin@10.44.0.1:/tmp/

ssh admin@10.44.0.1
sudo install -m 755 /tmp/link_monitor.py /tmp/lora_bridge.py /usr/local/sbin/
sudo install -m 644 /tmp/lora_frame.py /usr/local/sbin/
sudo install -m 644 /tmp/*.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now link-monitor lora-bridge
```

`lora_frame.py` must sit next to `lora_bridge.py`: Python puts the script's own
directory first on `sys.path`, which is the only reason `import lora_frame`
resolves from `/usr/local/sbin`.

### Dependencies

`link_monitor.py` needs nothing installed. `lora_bridge.py` needs
`python3-serial` (already present) and `python3-lgpio`.

**The Pi has no default route, so `apt install` cannot fetch anything.** The
lgpio packages were downloaded on the GS PC and installed with dpkg:

```
# on the GS PC, which has internet
base=http://ports.ubuntu.com/ubuntu-ports/pool/universe/l/lg-gpio
curl -O $base/liblgpio1_0.2.0.0-0ubuntu3_arm64.deb
curl -O $base/python3-lgpio_0.2.0.0-0ubuntu3_arm64.deb
scp *.deb admin@10.44.0.1:/tmp/ && ssh admin@10.44.0.1 'sudo dpkg -i /tmp/*lgpio*.deb'
```

**lgpio, not RPi.GPIO** — the version of `RPi.GPIO` on PyPI does not support the
Pi 5's RP1 GPIO controller. (`../microcontrollers_indomitus/e32-lora-chat/`
uses the `rpi-lgpio` shim instead because the library it depends on insists on
the `RPi.GPIO` import path; `lora_bridge.py` talks to `lgpio` directly and needs
no shim.)

`~/e32-venv` on the Pi carries its own copy of both, but that is for the
`e32-lora-chat` bench script only. **Do not point the service at it** — a system
service must not depend on a user's home directory surviving.

### Two systemd gotchas worth knowing

- **`lgpio` writes to the current working directory.** It opens a notification
  FIFO called `.lgd-nfy<n>` the moment it is imported. systemd starts services
  in `/`, which the service user cannot write, and the failure surfaces far from
  its cause: `FileNotFoundError: [Errno 2] No such file or directory:
  '.lgd-nfy-3'`. `lora-bridge.service` sets `StateDirectory=lora-bridge` and
  `WorkingDirectory=/var/lib/lora-bridge` to fix it. Running the script by hand
  hides this entirely, because a shell's cwd is writable.
- **`network.target` does not mean an address is assigned.** Both services bind
  `10.44.0.1` specifically, so the first bind after a cold boot loses the race
  against netplan. This is observed, not theoretical: after a reboot both report
  `NRestarts=1` and the journal carries one
  `OSError: [Errno 99] Cannot assign requested address`. `Restart=always` with
  `RestartSec=5` recovers it within seconds, so a fresh boot showing
  `NRestarts=1` is expected and not a fault.

`lora-bridge.service` runs as `admin`, not root: `/dev/ttyAMA0` and
`/dev/gpiochip4` are both `root:dialout` and `admin` is in `dialout`.

## LoRa wiring — Pi 5 GPIO ↔ E32-433T30D

BCM numbering (the numbers silkscreened as "GPIOxx" on pinout diagrams, not the
physical pin positions):

| E32 pin | Pi 5 |
|---|---|
| M0 | GPIO23 (physical 16) |
| M1 | GPIO24 (physical 18) |
| RXD | GPIO14 / TXD (physical 8) |
| TXD | GPIO15 / RXD (physical 10) |
| AUX | GPIO17 (physical 11) |
| VCC | **separate 3.3 V supply ≥1 A — not the Pi's 3V3 pin** |
| GND | common with the Pi and that supply (physical 6) |

Note the crossover: E32 TXD → Pi's RX pin, E32 RXD → Pi's TX pin.

Power, antenna and regulatory warnings are the same on both ends of the link and
are written out once, in
[`../microcontrollers_indomitus/esp32s3_lora_rover/README.md`](../microcontrollers_indomitus/esp32s3_lora_rover/README.md#2-power-and-safety--before-you-apply-power).
Read them before applying power. Short version: never transmit without an
antenna, give the module its own supply, and know that the default configuration
transmits at full 30 dBm — `--config low` drops it to 21 dBm.

### Freeing the UART (one-time, Ubuntu)

The mast Pi runs Ubuntu, not Raspberry Pi OS, so `raspi-config` is not the path
and there is no `/dev/serial0` alias — the port is `/dev/ttyAMA0`.

1. In `/boot/firmware/config.txt`: `enable_uart=1`
2. In `/boot/firmware/cmdline.txt`: remove `console=serial0,115200` (leave the rest)
3. `sudo systemctl disable --now serial-getty@ttyAMA0`
4. `sudo usermod -aG dialout $USER`, then reboot

Verify: `ls -l /dev/ttyAMA0` exists, and `sudo lsof /dev/ttyAMA0` prints nothing.

## Using the bridge

```
python3 lora_bridge.py --selftest        # frame codec, no hardware needed
python3 lora_bridge.py --rate-sweep      # fastest poll rate the link sustains
python3 lora_bridge.py --throughput 30   # iperf-style one-way saturation test
python3 lora_bridge.py --config read     # print the module's six config bytes
python3 lora_bridge.py --config write    # write them at 30 dBm, then read back
python3 lora_bridge.py --config low      # same, but at 21 dBm
python3 lora_bridge.py --chat            # raw transparent text, no framing
python3 lora_bridge.py --ping 200        # measure loss and RTT, print a summary
python3 lora_bridge.py                   # normal: poll at 3 Hz, serve tcp/4001
```

The full bring-up procedure, the wire format and the module's register layout
live with the rover firmware, in
[`../microcontrollers_indomitus/esp32s3_lora_rover/README.md`](../microcontrollers_indomitus/esp32s3_lora_rover/README.md).

Clients on 4001 read one JSON metrics object per line and may write one JSON
command object per line on the same socket:

```json
{"vx": 20, "vy": 0, "wz": -10, "estop": false}
```

A command that stops being refreshed reverts to zero after `--command-timeout`
(0.5 s); the rover independently zeroes its own output after 1 s of silence.

## Diagnosing a dead link

Work outwards; each step tells you which hop to stop trusting.

```
ip -br addr show                          # is the mast port even up? carrier=0 means cable
ping 10.44.0.1                            # GS PC → Pi
ssh admin@10.44.0.1 'iw dev wlx00c0caba8237 link'   # associated? RSSI? bitrate?
ssh admin@10.44.0.1 'ping -c3 10.42.0.1'  # Pi → rover
ping 10.42.0.1                            # GS PC → rover: fails alone = return route
nc 10.44.0.1 4002 | head -1               # what the monitor thinks
```

If the Pi's Alfa reports `No such device (-19)`, the adapter has dropped off
USB — check `lsusb` for a Realtek device and `dmesg | grep -i usb`. A clean
single `USB disconnect` with no over-current warnings and no re-enumeration
attempts means it was physically unplugged, not browned out.

To prove where packets die, watch the wireless leg while pinging from the GS PC:

```
ssh admin@10.44.0.1 'sudo tcpdump -ni wlx00c0caba8237 "icmp and host 10.44.0.10"'
```

Requests present with no replies = the rover is receiving and cannot answer,
i.e. the return route is missing.

## Verified

| Behaviour | Result |
|---|---|
| Rover cold boot → AP returns unattended | **passes** — `rover-ap.service` active ~2 min after boot, ch36/40 MHz, packaged `hostapd.service` stayed masked (2026-08-14) |
| Mast Pi reboot → services return unattended | **passes** — `eth0`, Alfa at USB3, `link-monitor`, watchdog, `/dev/ttyAMA0` (2026-08-13) |
| Alfa unplug → replug | **passes** — netplan reassociates with no intervention |
| `link_monitor.py` with the radio physically gone | **passes** — reports `DOWN` / `not associated`, does not crash when `iw` fails and the sysfs counters return `null` |
| DDS both directions across the router | **passes** — with the static-peer profile |
| GS PC → rover before the return route existed | **failed**, as designed: `tcpdump` showed every request arriving on the wireless leg and not one reply |
| DHCP scoped to the AP only | **passes** — `sockets bound exclusively to interface wlx00c0caba86c1`, pool `10.42.0.50–150` |

> **Lesson from the hostapd migration.** Moving the AP off NetworkManager
> silently removed DHCP for ~25 hours and nobody noticed, because the only
> client we were testing with — the mast Pi — is statically configured and never
> asks for a lease. When replacing something that did several jobs, enumerate
> the jobs; and test with a client that is representative, not just the one that
> is convenient.

## Known gaps

- **No range test has been completed.** All numbers above are bench figures.
  The thresholds in both monitors are guesses; `link_monitor.py` says so in its
  own comments.
- **The rover's root filesystem is corrupt.** `tune2fs` reports `clean with
  errors`, 45 errors, first at 2026-07-29, and it has never been fsck'd; every
  boot logs `mounting fs with errors`. The NVMe itself is healthy (0 media
  errors, 0% wear) — the likely cause is 37 unsafe shutdowns. It has already
  cost one outage: `depmod` could not write `modules.dep`, so both Wi-Fi
  modules were present on disk but invisible to `modprobe`, which looks exactly
  like a missing driver. The 2026-08-21 reflash did **not** recreate the
  filesystem (the error history survived it). Wants a clean flash.
- **The rover's Alfa is on the USB2 hub**, capping throughput at roughly a
  third. Move it to a Bus 02 port — see above.
- `enP8p1s0` no longer enumerates after the R36.4.3 reflash, so the rover has
  one Ethernet port rather than two.
- **The Pi's clock is about a week behind** and has never NTP-synced, because
  its only internet path (`wlan0`) is down. Log timestamps cannot be correlated
  with real time — this nearly caused a recent USB disconnect to be misread as a
  week old. Wants an RTC battery or a boot-time sync from the GS PC.
- **The AP is 5 GHz only**, so every 2.4 GHz-only client — every ESP32, older
  phones and laptops — is locked out. One radio can only be on one band at a
  time; a second band needs a second adapter. See
  [`../HANDOVER.md`](../HANDOVER.md).
- **No range test has been done.** Every threshold in both monitors is a bench
  guess; [FIELD-TEST.md](FIELD-TEST.md) is the procedure that replaces them.
- **Credentials are defaults** (the Wi-Fi PSK is in `99-mast.yaml`; the rover's
  login is trivial). Change both before competition.
- `wlan0` on the Pi logs `brcmf_set_channel … fail` every ~11 s. It is the
  home-Wi-Fi management path and should be removed before competition anyway.
- The RF Form declares an Alfa APA-M25-6E 10 dBi panel that is **not fitted** —
  stock dipoles are — and declares the mast as the AP, which is the opposite of
  what is built.

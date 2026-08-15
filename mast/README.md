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
| Rover (`indomitus-rover-computer`) | `indomitus-rover` | `wlx00c0caba86c1` | `10.42.0.1/24` | Alfa, **AP** (hostapd) |
| | | `enP8p1s0` | `10.44.0.189/24` | NM `Rover-Shared`, DHCP — **subnet collision, see below** |
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

There is **no equivalent for the rover**. Every other Jetson interface is down,
so if its AP fails to start the only way in is physical. Any change to the
rover's radio must therefore arm a rollback *before* it changes anything; see
`/usr/local/sbin/rover-ap-rollback.sh` and the header comment in the deploy
script it came from.

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

### ⚠ Subnet collision on the rover's Ethernet

The rover's `enP8p1s0` (NM connection `Rover-Shared`) takes a DHCP lease on
**`10.44.0.0/24` — the same subnet as the mast link** — and is *not* reachable
from the GS PC. That leaves two routes to the same prefix:

```
10.44.0.0/24 via 10.42.0.2 dev wlx00c0caba86c1              # metric 0  ← wins
10.44.0.0/24 dev enP8p1s0  proto kernel src 10.44.0.189 metric 200
```

Only the metric ordering keeps the link working: the Wi-Fi route is metric 0, so
`ip route get 10.44.0.10` correctly resolves via `10.42.0.2`. If that ordering
ever flips — a DHCP option change, an NM restart picking a lower metric — the
rover's replies leave by an Ethernet port that cannot reach the ground station,
and the link dies silently while every interface still looks up.

It also means the rover advertises DDS locators on an address the GS PC cannot
route to. Discovery still succeeds today, but this is precisely the shape of the
"discovery perfect, `ros2 topic hz` silent" failure described above.

**Fix properly**: either move that Ethernet off `10.44.0.0/24`, or take the
interface down when it is not being used for bench work.

## RF configuration

| Setting | Value |
|---|---|
| SSID | `IndomitusRover` (must become `ERC_UCUSpaceRobotics_A`/`_B`) |
| Security | WPA2-PSK, CCMP |
| Band / channel | 5 GHz, ch **36** (5180 MHz) |
| Width | **40 MHz**, HT40+ (secondary ch40), centre 5190 |
| Mode | 802.11n HT40 — see below |
| TX power | 15 dBm |
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

### Why 300 Mbit/s and not 400

The AP config requests 802.11ac (`ieee80211ac=1`, `vht_capab`, `vht_oper_*`) and
hostapd accepts it without complaint, but the beacon it actually transmits
carries **HT capabilities and HT operation only — no VHT IE**. The `rtl8812au`
out-of-tree driver does not implement VHT in AP mode; it advertises VHT on the
phy but not for the AP interface type.

So the link negotiates HT40 MCS15 (300 Mbit/s PHY) rather than VHT40 MCS9
(400 Mbit/s). This is a chipset limit, not a config gap — swapping which end is
the AP does not help, since both radios are AWUS036ACH on the same driver. The
VHT lines are left in place, harmlessly, so a future driver fix lights up on its
own.

### Measured

Bench range (~1 m, −21 to −29 dBm), 2026-08-13, `iperf3 -t 8`:

| Configuration | Down | Up | RTT |
|---|---|---|---|
| 2.4 GHz ch1, HT20 (NM) | 71.6 | 71.5 | 6.1 ms |
| 5 GHz ch36, HT20 (NM) | 82.4 | 103 | 5.1 ms |
| **5 GHz ch36, HT40 (hostapd)** | **170** | **208** | 4.9 ms |

Mbit/s. These are bench numbers and say nothing about competition range —
**no range test has been completed yet**.

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
on the latched `/link/active_path`. It does not move any traffic itself.

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
| `ROS_DOMAIN_ID` | **42**, both ends |
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
  `10.44.0.1` specifically, so the first bind after a cold boot can lose the
  race against netplan. Both use `Restart=always` with `RestartSec=5`, which
  covers it within seconds.

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
- The rover's Ethernet collides with the mast subnet — see the warning above.
  This is the most likely cause of a future silent link failure.
- **The Pi's clock is about a week behind** and has never NTP-synced, because
  its only internet path (`wlan0`) is down. Log timestamps cannot be correlated
  with real time — this nearly caused a recent USB disconnect to be misread as a
  week old. Wants an RTC battery or a boot-time sync from the GS PC.
- **The AP is 5 GHz only**, so every 2.4 GHz-only client — every ESP32, older
  phones and laptops — is locked out. One radio can only be on one band at a
  time; a second band needs a second adapter. See
  [`../HANDOVER.md`](../HANDOVER.md).
- **Neither service has been tested across an actual reboot.** Both are
  `enabled` and both survive `systemctl kill -s KILL`, but the cold-boot bind
  race against netplan has only been reasoned about, not observed.
- **Credentials are defaults** (the Wi-Fi PSK is in `99-mast.yaml`; the rover's
  login is trivial). Change both before competition.
- `wlan0` on the Pi logs `brcmf_set_channel … fail` every ~11 s. It is the
  home-Wi-Fi management path and should be removed before competition anyway.
- The RF Form declares an Alfa APA-M25-6E 10 dBi panel that is **not fitted** —
  stock dipoles are — and declares the mast as the AP, which is the opposite of
  what is built.

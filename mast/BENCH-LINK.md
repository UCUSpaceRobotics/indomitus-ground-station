# The bench link — running a stand-in rover on the desk

Companion to [README.md](README.md) (the real comms chain) and
[STARTUP.md](STARTUP.md) (the rover link's driver/AP setup and persistence).
This file covers the **bench link**: a laptop hosting an AP that the ground
station connects to, so the GS stack has something to talk to while the actual
rover is being worked on.

Both links are designed to run **at the same time**. The bench has its own SSID
and its own `/24`, and nothing here touches `10.42.0.0/24`, the rover's hostapd
config, or `rover-ap.service`.

Set it up with [restore-bench-link.sh](restore-bench-link.sh); that script is
the authoritative copy of every file described here.

## Topology

```
GS PC   enp2s0        10.44.0.10/24   route 10.42.0.0/24 via 10.44.0.1   (rover)
                                      route 10.43.0.0/24 via 10.44.0.1   (bench)
          |             point-to-point through the TP-Link POE160S injector
Pi      eth0          10.44.0.1/24
Pi      wlx00c0…8237  10.42.0.2/24  +  10.43.0.2/24    client of whichever AP is up
Bench   wlx00c0…eac8  10.43.0.1/24    SSID IndomitusBench, ch44 VHT40, hostapd
                                      route 10.44.0.0/24 via 10.43.0.2
Rover   wlx00c0…86c1  10.42.0.1/24    SSID ERC_UCUSpaceRobotics_A — untouched
```

The bench laptop occupies exactly the rover's position in the chain, including
the return route to `10.44.0.0/24`. The Pi routes between its two `/24`s as
before, and the GS PC needs one extra static route.

### Why two addresses on one Pi interface

`addresses: [10.42.0.2/24, 10.43.0.2/24]` in `99-mast.yaml`. Each is on-link
only on its own network, so whichever AP the Pi associates with, the correct
address applies and the other is inert. That is what lets both links coexist
with **no change to the rover side at all**.

netplan matches on **SSID alone**, so the Pi follows either AP wherever it
moves. There is **no priority field** for `access-points` — with both APs in
range wpa_supplicant chooses by signal. To force one, power the other down.

## What the link is

| | Bench laptop | Mast Pi |
|---|---|---|
| Kernel | 6.17.0-23-generic | 6.8.0-1057-raspi |
| Module | `rtw_8812au` (rtw88/0.6, DKMS) | `rtw_8812au` (rtw88/0.6, DKMS) |
| Role | AP (hostapd), `10.43.0.1` | client, `10.43.0.2` |
| Radio | Alfa AWUS036ACH `00:c0:ca:ba:ea:c8` | Alfa AWUS036ACH `00:c0:ca:ba:82:37` |

The laptop runs the **same lwfinger/rtw88 backport** as the Pi and rover, even
though its 6.17 kernel has RTL8812AU support in-tree since 6.13. That is
deliberate: matching module means bench measurements mean the same thing as
field measurements. The in-tree `rtw88_8812au` works fine if you would rather
not carry the DKMS/Secure Boot burden — see
[Driver choice](#driver-choice-and-secure-boot).

### Observed, 2026-08-28, radios ~1 m apart

| | |
|---|---|
| PHY tx / rx | 324–400 / 324–360 Mbit/s, VHT-MCS 8–9, 40 MHz, NSS 2 |
| Signal | −36 dBm, chains `[-38, -36]` (2 dB spread) |
| RTT bench → Pi | 8.2 ms avg |
| RTT bench → GS PC | 4.9 ms avg, 32 ms max, 0 % loss over 20 |

⚠️ **These are PHY rates and ping times, not throughput.** `STARTUP.md`'s
131–262 Mbit/s figures are iperf3 measurements and are not comparable. Nobody
has run iperf3 across the bench link yet; do that before quoting a number.

The same caution as the rover link applies: do not bench with the radios
touching. Anything hotter than about −15 dBm is receiver compression, not a
good link.

## Setting it up

```bash
./mast/restore-bench-link.sh --dry      # what would change
./mast/restore-bench-link.sh            # do it
./mast/restore-bench-link.sh --help     # all parameters
```

Run it **on the bench laptop** — the opposite of `restore-link.sh`, which runs
from the GS PC. It sudo's locally and reaches the Pi over SSH, falling back
from `10.43.0.2` to the wired `10.44.0.1`.

Everything is parameterised; the useful ones:

```bash
./mast/restore-bench-link.sh --channel 149          # move off ch44
./mast/restore-bench-link.sh --band bg --channel 11 # 2.4 GHz instead
./mast/restore-bench-link.sh --width 20
./mast/restore-bench-link.sh --iface wlx...         # after an adapter swap
GS_SSH=user@10.44.0.10 ./mast/restore-bench-link.sh # also fix the GS route
```

`--iface` matters more than it looks: the interface name is MAC-derived, so it
changes every time the Alfa is swapped, and three different AWUS036ACH units
have been in play (`86:c1` rover, `82:37` Pi, `ea:c8` bench spare).

### Known-good file inventory

| Host | Path | Holds |
|---|---|---|
| Bench | `/etc/hostapd/bench-ap.conf` | live AP config, mode 600 |
| Bench | `/etc/systemd/system/bench-ap.service` | hostapd + address + return route + tx power |
| Bench | `/etc/NetworkManager/conf.d/99-bench-ap-unmanaged.conf` | radio hidden from NM, `+=` form |
| Bench | `/etc/modprobe.d/cfg80211.conf` | `ieee80211_regdom=UA` — without it ch44 is illegal |
| Bench | `/etc/dnsmasq.d/bench-ap.conf` | DHCP, AP interface only |
| Bench | `/etc/systemd/system/dnsmasq.service.d/10-after-bench-ap.conf` | ordering after the AP |
| Pi | `/etc/netplan/99-mast.yaml` | both SSIDs, both addresses |
| GS PC | NM profile on `enp2s0` | `10.43.0.0/24 via 10.44.0.1` |

## Failure signatures

Everything below was hit for real while building this link.

| What you see | Actual cause | Fix |
|---|---|---|
| Alfa enumerates on USB, `Driver=[none]`, no `wlx` interface | Secure Boot rejected the unsigned DKMS modules — `Loading of module with unavailable key is rejected` | enroll the MOK key, see below |
| `bench-ap.service` restart-loops; AP appears then vanishes | an `ExecStartPost` that cannot succeed. `iw set power_save off` returns **EOPNOTSUPP (-95)** on an AP interface — power save is a managed-mode setting only | remove it; power save belongs on the *client* |
| hostapd refuses to start on a valid-looking channel | regdomain is world `00`, where all 5 GHz is `no IR`. ch36 may *look* usable there because cfg80211 beacon hints clear no-IR on channels it has heard a beacon on | set the country properly |
| dnsmasq `active` but nothing on 67/udp | no `dhcp-range` — dnsmasq runs DNS-only and never opens the DHCP port | add the range |
| dnsmasq refuses to start | `/etc/dnsmasq.d/ubuntu-fan` already sets `bind-interfaces`; `bind-dynamic` collides. **`dnsmasq --test` reports "syntax check OK" anyway** — the conflict is only enforced at startup | use `bind-interfaces` and order the unit after the AP |
| Pi associates, good signal, no IP, ARP fails | `netplan generate` + restarting `netplan-wpa-*.service` restarts **wpa_supplicant only** — that is association. Addresses come from systemd-networkd, which neither command touches | `netplan apply` |
| GS PC: `ip route get 10.43.0.1` → `destination net unreachable` | `nmcli device reapply` does not reliably push route changes | `nmcli con up PROFILE` |
| `--txpower 30` has no effect | cfg80211 clamps to the regdomain ceiling (20 dBm on every UA band) | see [Transmit power](#transmit-power) |
| Pi does not come back after an AP restart | same trap as the rover link | `sudo systemctl restart netplan-wpa-wlx00c0caba8237.service` |

Note the recurring shape: **five of these are checks that lied.** `dnsmasq
--test` passing on a config that cannot start, `mokutil --test-key` exiting 1
while reporting success, `modprobe` succeeding on a blacklisted module,
`systemctl is-active`'s `"inactive"` containing `"active"`, and a `grep -q`
pipeline under `set -o pipefail` reporting a successful match as failure.
Prefer matching on output text over trusting an exit status.

## Persistence

### Survives a reboot

| Thing | Where |
|---|---|
| `rtw_8812au` driver | DKMS `rtw88/0.6`, `AUTOINSTALL=yes` — rebuilds and re-signs on kernel updates |
| MOK key trusted | enrolled in shim; survives kernel updates |
| regdomain UA | `/etc/modprobe.d/cfg80211.conf` |
| AP starts at boot | `bench-ap.service`, enabled |
| AP config, address, return route, tx power | `bench-ap.service` + `bench-ap.conf` |
| radio hidden from NM | `99-bench-ap-unmanaged.conf` |
| DHCP | `dnsmasq` enabled + the ordering drop-in |
| Pi's two addresses and two SSIDs | `99-mast.yaml` |

### Does NOT survive a reboot

| Thing | Consequence |
|---|---|
| GS PC route, if added with `ip route add` | bench unreachable from the GS PC; put it on the NM profile |

That is the only one — provided the script was used. A hand-built bench will
also lose the AP's address and return route, which is exactly why they live in
`ExecStartPost` rather than in someone's shell history.

## Driver choice and Secure Boot

The bench laptop has Secure Boot on with `lockdown=integrity`, so DKMS modules
must be signed by a key shim trusts. Enrollment happens in MokManager **before
the kernel starts** — it needs a reboot and a keypress, and cannot be scripted:

```bash
sudo mokutil --import /var/lib/shim-signed/mok/MOK.der
# reboot, then: Enroll MOK -> Continue -> Yes -> password
mokutil --test-key /var/lib/shim-signed/mok/MOK.der   # "is already enrolled"
```

`restore-bench-link.sh` checks this in preflight and stops with these
instructions rather than half-configuring a machine whose radio cannot appear.

Both modules claim the same modalias
(`usb:v0BDAp8812…icFFiscFFipFFin*`), so whichever you want must win explicitly:
the loser is blacklisted and the winner force-loaded, exactly as on the Pi.
Getting the direction backwards is the documented "interface missing entirely
after a reboot" failure.

## Transmit power

The ceiling is the **regulatory domain**, not the adapter:

```
country UA: DFS-ETSI
	(5150 - 5250 @ 80), (N/A, 20), NO-OUTDOOR
	(5250 - 5350 @ 80), (N/A, 20), NO-OUTDOOR, DFS
	(5470 - 5725 @ 160), (N/A, 20), NO-OUTDOOR
	(5725 - 5850 @ 80), (N/A, 20), NO-OUTDOOR
```

Every 5 GHz band under UA is **20 dBm**, and 2.4 GHz likewise. cfg80211 clamps
anything higher, so a config asking for 30 dBm produces a value that silently
never takes effect. `restore-bench-link.sh` refuses such a value rather than
writing a config that lies.

If a higher figure was seen historically, it was the **vendor `8812au`
driver**, which does not enforce cfg80211's regulatory limits. Mainline `rtw88`
does. That is a correctness change, not a regression.

Worth knowing before chasing power at all: raising the **AP's** transmit does
nothing for the **uplink**, which is bounded by the Pi's own radio and its
antennas. A link that is weak in one direction is not fixed by shouting louder
from the other end — check the per-chain signal spread first.

## Relationship to the rover tooling

`verify-link.sh` and `restore-link.sh` are **rover-specific** and have no bench
awareness. Against the bench link:

- `verify-link.sh` hardcodes `ROVER_ALFA=wlx00c0caba86c1` and pings
  `10.42.0.1`, so it fails wholesale. That is the script being rover-scoped,
  not the bench being broken.
- `restore-link.sh` will happily rebuild the rover link alongside; the two
  scripts do not conflict.

Two things in the rover tooling are worth a look given what was learned here:

1. `restore-link.sh` runs `iw dev $ROVER_ALFA set power_save off` on the
   rover's **AP** interface and then prints `AP power_save off` unconditionally.
   On this hardware that call returns EOPNOTSUPP; the `-` prefix on
   `rover-ap.service`'s equivalent hides the same thing. Confirm with
   `ssh indomitus-rover@10.45.0.51 'sudo iw dev wlx00c0caba86c1 set power_save off; echo rc=$?'`.
2. `verify-link.sh` checks `AP power save off` and expects `off`. If the value
   is unsettable on an AP interface, that check can never be satisfied by
   `restore-link.sh` and is testing something it cannot control.

Neither has been changed — they are recorded here so the next person does not
rediscover them mid-field-test.

# Bringing the link up, and what a reboot undoes

Companion to [README.md](README.md) (topology, addressing) and
[FIELD-TEST.md](FIELD-TEST.md) (range procedure). This file covers the state the
Wi-Fi link is in after the 2026-08-23 rework, and — the part that will bite you —
**which of those fixes survive a power cycle and which silently do not.**

Read the persistence table before any field test. Three of the settings that make
the link fast are runtime-only right now.

## What the link is

Both Alfa AWUS036ACH adapters run the **mainline `rtw88` mac80211 driver**, via
the [lwfinger/rtw88](https://github.com/lwfinger/rtw88) DKMS backport, not the
out-of-tree vendor driver they used to run.

| | Mast Pi | Rover |
|---|---|---|
| Kernel | 6.8.0-1057-raspi | 5.15.148-tegra (L4T R36.4.3 / JetPack 6.2) |
| Module | `rtw_8812au` (rtw88/0.6) | `rtw_8812au` (rtw88/0.6) |
| Role | client, `10.42.0.2` | AP (hostapd), `10.42.0.1` |

The backport is needed because RTL8812AU support only reached mainline `rtw88` in
**kernel 6.13**; neither host is close, and JetPack 7 is still on 6.8. The
backport builds against 5.4+, so it works on both without touching the kernel.

### Measured, 2026-08-23, both radios ~1 m apart

| | 40 MHz (current) | 80 MHz |
|---|---|---|
| PHY tx / rx | 400 / 324 Mbit/s | 780 / 585 Mbit/s |
| Pi → rover | 131–140 Mbit/s | **230** Mbit/s |
| rover → Pi | 93–113 Mbit/s | **203–262** Mbit/s |
| RTT avg / max | 2.7 / 7.2 ms | 3.5 / 15.2 ms |

**These supersede the bench baselines in FIELD-TEST.md**, which record 170/208 at
"HT40" and are not reproducible — they predate this rework and were measured on
hardware with a faulty antenna and the vendor driver.

⚠️ **Do not bench with the radios touching.** At ~1 m the signal reads about
−20 dBm, which is where the numbers above were taken. Sat side by side it reads
**−6 to −8 dBm**, which overdrives the receiver front end: rate control oscillates
between MCS 5 and 8 and throughput drops to ~115/76 with no fault present. If
bench numbers look bad, check `signal:` before chasing anything else — anything
hotter than about −15 dBm is measuring compression, not the link.

Currently running **40 MHz**. 80 MHz is roughly 1.7× upload and 2.2× download for
slightly looser latency; 40 MHz holds more link margin at range. To switch:

```bash
sudo cp /etc/hostapd/rover-ap-rtw88-vht80.conf /etc/hostapd/rover-ap.conf
sudo systemctl restart rover-ap.service
ssh admin@10.44.0.1 'sudo systemctl restart netplan-wpa-wlx00c0caba8237.service'
```

That third line is **not optional** — see [Known problems](#known-problems).

## Checking it, and putting it back

Two scripts. Use them in that order; both are idempotent.

```bash
ROVER_PW=... ./mast/verify-link.sh          # what is wrong
ROVER_PW=... ./mast/restore-link.sh --dry   # what would change
ROVER_PW=... ./mast/restore-link.sh         # change it
ROVER_PW=... ./mast/verify-link.sh          # confirm
```

`verify-link.sh` exits with the number of failures. WARNs are the known
non-persistent settings under [Does NOT survive a reboot](#does-not-survive-a-reboot).

`restore-link.sh` reproduces **every config file in full**, so it is the
authoritative copy of this configuration — a stock image plus this script gets
the link back. It only writes files whose content actually differs.

### Known-good file inventory

Everything the link depends on. Anything not in this list is not load-bearing.

| Host | Path | Holds |
|---|---|---|
| Rover | `/etc/hostapd/rover-ap.conf` | live AP config — a copy of `rover-ap-rtw88-vht40.conf` (md5 `f2f5869b…`) or `-vht80` (`463ff27d…`) |
| Rover | `/etc/NetworkManager/conf.d/99-rover-ap-unmanaged.conf` | **both** radio MACs in one `unmanaged-devices` key |
| Rover | `/etc/modprobe.d/99-blacklist-88XXau.conf` | `blacklist 8812au` + `88XXau` |
| Rover | `/etc/modprobe.d/8812au.conf` | vendor options; the `rtw*_8812au` blacklist lines must stay **commented** |
| Rover | `rover-ap.service`, `rover-ap-dhcp.service` | both `enabled` |
| Pi | `/etc/modules-load.d/rtw88-alfa.conf` | forces `rtw_8812au` at boot |
| Pi | `/etc/modprobe.d/99-blacklist-vendor-8812au.conf` | `blacklist 8812au` + `88XXau` |
| Pi | `/etc/modprobe.d/8812au.conf` | same commented-blacklist requirement as the rover |
| Pi | `/etc/udev/rules.d/99-alfa-powersave-off.rules` | MAC-matched power-save-off |
| GS | `~/.local/bin/sync-mast-clock.sh` + crontab | mast clock, every 15 min |
| GS | NM: `gs-mast` autoconnect **no**, `rover-recovery` autoconnect **yes** | stops `eno1` being hijacked |

Both hosts also need DKMS `rtw88/0.6` installed with `AUTOINSTALL=yes`.

### Failure signatures

Symptoms that look like a broken radio but are not.

| What you see | Actual cause | Fix |
|---|---|---|
| `hostapd` active but `iw dev` says `type managed`, 0 stations | An NM `conf.d` rule with `managed=1` took the radio; NM and hostapd fight over it | remove it, restore the unmanaged file, restart both |
| Alfa interface missing entirely after a reboot, `lsmod` empty | `blacklist rtw_8812au` in `8812au.conf`. A manual `modprobe` overrides a blacklist, so it looks fine until you reboot | comment the blacklist line; check with `systemd-analyze cat-config modprobe.d \| grep -E '^blacklist rtw'` |
| One radio unmanaged, the other not, despite two conf files | NM `conf.d` **overrides** a repeated key instead of merging; the alphabetically-last file wins | put both MACs in one key, then `systemctl restart NetworkManager` (reload is not enough) |
| A guest device associates then hangs at "obtaining IP address" | `rover-ap-dhcp` is dead. It is `BindsTo=rover-ap.service`, so it dies with the AP and never returns. The Pi's **static** 10.42.0.2 hides this completely | `systemctl enable --now rover-ap-dhcp.service` |
| Both hosts "unreachable", cables show link, ARP fails on both | The two GS cables are swapped — each device sits on a port configured for the other's subnet | identify with `ping6 ff02::1%<iface>`, which works across mismatched subnets; then swap them back |
| Pi associated, no IP, after any AP restart | The Pi does not reliably reassociate on its own | `ssh admin@10.44.0.1 'sudo systemctl restart netplan-wpa-wlx00c0caba8237.service'` |
| Bench throughput far below the table above | Signal hotter than about −15 dBm — receiver compression from radios sat together, not a link fault | separate them to ~1 m |

Two traps worth stating outright, both of which produced a *false healthy* reading:

- **`"inactive"` contains `"active"`.** A substring test on `systemctl is-active`
  reports a dead service as running. `verify-link.sh` matches exactly for this
  reason; the bug initially hid the stopped DHCP server.
- **A manual `modprobe` succeeds on a blacklisted module.** Only
  `systemd-modules-load` honours the blacklist, so the link works perfectly right
  up until the next reboot.

## Persistence

### Survives a reboot

Verified by an actual reboot of both the Pi and the rover on 2026-08-23, not just
by inspection.

| Host | Thing | Where |
|---|---|---|
| Pi, rover | `rtw88/0.6` driver | DKMS, `AUTOINSTALL=yes` — rebuilds on kernel updates too |
| Pi | **module loads at boot** | `/etc/modules-load.d/rtw88-alfa.conf` — see below, this is required |
| Pi | vendor driver blacklisted | `/etc/modprobe.d/99-blacklist-vendor-8812au.conf` |
| Pi | **client power save off** | `/etc/udev/rules.d/99-alfa-powersave-off.rules` |
| Rover | vendor driver blacklisted | `/etc/modprobe.d/99-blacklist-88XXau.conf` |
| Rover | `rtw88_8812au` un-blacklisted | `/etc/modprobe.d/8812au.conf` (lines commented) |
| Rover | AP starts at boot | `rover-ap.service`, enabled |
| Rover | AP width/config | `/etc/hostapd/rover-ap.conf` |
| Rover | **both radios hidden from NM** | `/etc/NetworkManager/conf.d/99-rover-ap-unmanaged.conf` |
| GS PC | lifeline claims `eno1` | `rover-recovery` autoconnect=yes, `gs-mast` autoconnect=no |
| GS PC | mast clock sync | `crontab -l` + `~/.local/bin/sync-mast-clock.sh` |

The Pi's power-save rule matches on **MAC, not interface name**, so it survives
the `wlan1 → wlx00c0caba8237` rename and re-fires on every driver reload, not
just at boot. Verified by forcing power save on and reloading the module.

### Three traps found by that reboot

**The Pi's Alfa does not auto-load.** `modules.alias` maps `0bda:8812` twice:

```
alias usb:v0BDAp8812...ic*isc*ip*in*      8812au       <- vendor, broad match
alias usb:v0BDAp8812...icFFiscFFipFFin*   rtw_8812au   <- rtw88, needs class FF/FF/FF
```

With the vendor module blacklisted and the rtw88 alias not matching, **nothing
loads and the interface never appears** — no `wlx00c0caba8237` at all. Hence
`/etc/modules-load.d/rtw88-alfa.conf`. If the Alfa is ever missing after a boot,
check `lsmod | grep rtw` first; `sudo modprobe rtw_8812au` brings it straight
back, then restart `netplan-wpa-wlx00c0caba8237.service`.

**NM's `conf.d` overrides, it does not merge.** Two files each setting
`unmanaged-devices` means the alphabetically-last one wins and the other is
silently ignored. Both the Alfa and the Intel 8265 MACs must live on **one
semicolon-separated key** in a single file. `systemctl reload NetworkManager` is
not enough either — it needs a full `restart`.

**`gs-mast` used to hijack `eno1`.** It had `autoconnect=yes` with no bound
device, so when the rover's reboot bounced `eno1`'s carrier NM gave it
`10.44.0.10/24` — a duplicate of `enp2s0`, at a *lower* route metric. Traffic for
the Pi went out the rover's cable and the mast became unreachable, taking the
lifeline with it. `gs-mast` is now `autoconnect=no`. If you ever cable the mast
to `eno1` instead, bring it up by hand.

### Does NOT survive a reboot

Two things left. Both cost real performance.

**1. Mast Pi's onboard `wlan0` comes back up and scans.**
`/etc/netplan/50-cloud-init.yaml` still has it as `dhcp4: true`, so
`wpa_supplicant` starts, finds nothing, and rescans all 42 channels every ~11 s —
again including 5180 MHz. README already says this radio should be down.

```bash
sudo pkill -f 'wpa-wlan0'; sudo ip link set wlan0 down
```

Permanent fix is to remove the `wlan0` stanza from `50-cloud-init.yaml`, or
physically remove the radio before competition as README suggests.

**2. GS PC: EEE off on `enp2s0`.**
Worth 514 → 887 Mbit/s on the mast Ethernet leg.

```bash
sudo ethtool --set-eee enp2s0 eee off
```

⚠️ This renegotiates the link and **can leave it in the "link up, no traffic"
state** — see [Known problems](#known-problems). Needs a NetworkManager
dispatcher script or a systemd unit on `gs-mast-p2` to persist.

Also non-persistent, but harmless: the `iperf3 -s` server on the rover. Start it
by hand before a field test (`nohup iperf3 -s -D`).

### Partially survives

**Pi clock.** It has no default route, so NTP can never sync, and its RTC reads
`1970-01-01` because the Pi 5's coin cell on J5 is not fitted. The GS pushes time
every 15 minutes, so the Pi is **wrong for up to 15 min after boot** and stays
wrong if the GS is off. `timedatectl` will always say
`System clock synchronized: no` — that is expected, since `date -s` does not set
the flag. Fit the coin cell for a real fix.

## Recovery paths

If the mast Ethernet is dead, the Pi is still reachable **over the Wi-Fi via the
rover** — this is what worked on 2026-08-23 when the documented IPv6 route did not:

```bash
ssh -J indomitus-rover@10.45.0.51 admin@10.42.0.2      # Pi, via rover, over Wi-Fi
ssh indomitus-rover@10.45.0.51                          # rover, wired lifeline
ssh admin@fe80::8aa2:9eff:fec6:5546%enp2s0              # Pi, IPv6-LL over the cable
```

The IPv6 link-local path only helps when the cable is sound but IP config is
wrong. When the **cable itself** is the fault it fails too, so do not rely on it
as the only out-of-band route. The wired lifeline to the rover requires the cable
in the rover's `enP1p1s0` (not `enP8p1s0`, whose profile is DHCP-only).

## Known problems

**The Pi does not reliably reassociate after an AP restart.** It sits associated
with no IP. Every AP config change needs:

```bash
ssh admin@10.44.0.1 'sudo systemctl restart netplan-wpa-wlx00c0caba8237.service'
```

This is a field hazard — restart the rover AP and the link may not return on its
own. Related to the "associated, good signal, no traffic" detector already in
`link_monitor.py`.

**Mast Ethernet corrupts one direction.** GS → Pi loses ~0.2 % of frames to CRC
errors (1551 in a 10 s transfer); Pi → GS moves 1.09 GB with **zero**. Disabling
EEE lifted throughput 514 → 887 Mbit/s but did not fix the corruption, so the
fault is physical and specific to the GS-transmit path. The link also flapped
four times in one session. Not currently limiting — the Wi-Fi hop peaks at
262 Mbit/s — but it is a real fault. Untested next steps: bypass the TP-Link
POE160S injector, swap the cable, or run `sudo ethtool --cable-test enp2s0`
(RTL8125B supports pair-level TDR).

**Link up, no traffic.** Both ends can report `1Gbps/Full, Link detected: yes`
while nothing crosses. Seen on 2026-08-06 and again on 2026-08-23 after toggling
EEE. Clear it from the Pi:

```bash
sudo ip link set eth0 down; sleep 3; sudo ip link set eth0 up
```

## Why the config looks like this

Three independent faults were stacked, each masking the others — which is why
partial fixes looked like regressions:

1. **Vendor `rtl8812au` driver.** Never reported station capabilities to hostapd
   (`capability=0x0`, `supported_rates=0c 18 30`), silently ignored width
   requests (asked for 80 MHz, got 40), and never established A-MPDU. Throughput
   matched un-aggregated 802.11 airtime arithmetic to within 3 %: ~50 Mbit/s
   regardless of a 300 Mbit/s PHY. It also made 40 MHz produce 190–230 ms latency
   spikes — on `rtw88`, 40 MHz is clean, so that was the driver, not the width.
2. **Power save.** `iw ... get power_save` read `off` while the vendor driver
   still parked the radio (`rtw_ips_mode` is separate from the cfg80211 flag).
   After the driver swap, mac80211's default re-enabled it, costing 117 ms
   average RTT with 408 ms spikes. Hence the udev rule.
3. **Antenna.** The Pi's Alfa had a **10 dB imbalance between chains**
   (`-22 [-22, -32] dBm`, steady across every sample) — a bad connection on
   chain 1. Two-stream MIMO is bounded by the weaker chain, so the rover backed
   the downlink off to MCS 3–4. Reseating it took download from ~120 to
   ~203–262 Mbit/s and rates to MCS 9.

Diagnostic worth keeping: the bracketed pair in `station dump` is per receive
chain on the **reporting** radio. Want them within ~2–3 dB.

```bash
sudo iw dev wlx00c0caba8237 station dump | grep -E '^\s+signal:'   # on the Pi
sudo iw dev wlx00c0caba86c1 station dump                            # on the rover
```

`hostapd` now has `ctrl_interface=/var/run/hostapd`, which the old config lacked,
so per-station rates are finally readable:

```bash
sudo hostapd_cli -i wlx00c0caba86c1 all_sta
```

## Config inventory

Rover, `/etc/hostapd/` — `rover-ap.conf` is a copy of whichever variant is live:

| File | Width |
|---|---|
| `rover-ap-rtw88-vht80.conf` | 80 MHz, VHT |
| `rover-ap-rtw88-vht40.conf` | 40 MHz, VHT — **currently live** |
| `rover-ap-vht40.conf`, `rover-ap-ht40.conf` | pre-rework, vendor-driver era |
| `rover-ap.conf.prevht80`, `8812au.conf.vendor-bak` | backups |

Helper scripts left in `~indomitus-rover/` on the rover: `ap-switch-to-nm.sh`,
`ap-revert-to-hostapd.sh`, `ap-try-vht80.sh`, `ap-restore-conf.sh`,
`ap-swap-driver.sh`. Each arms a systemd dead-man timer that reverts after a
timeout unless cancelled with `sudo systemctl stop ap-revert.timer`.

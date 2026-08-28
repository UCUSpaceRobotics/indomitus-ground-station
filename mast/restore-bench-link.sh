#!/bin/bash
# Bring up the BENCH link — GS PC ↔ mast Pi ↔ bench laptop — in one shot.
#
#     ./mast/restore-bench-link.sh                      # defaults
#     ./mast/restore-bench-link.sh --dry                # print what would change
#     ./mast/restore-bench-link.sh --channel 149        # different channel
#     ./mast/restore-bench-link.sh --band bg --channel 11
#     GS_SSH=user@10.44.0.10 ./mast/restore-bench-link.sh
#     ./mast/restore-bench-link.sh --help
#
# Run this ON THE BENCH LAPTOP (the machine hosting the AP). It sudo's locally
# for its own config and reaches the Pi over SSH; it is NOT run from the GS PC
# the way restore-link.sh is.
#
# Idempotent: safe to run repeatedly. Every file it writes is reproduced in
# full, so this script *is* the authoritative copy of the bench configuration —
# the same contract restore-link.sh has for the rover link.
#
# See mast/BENCH-LINK.md for the topology, the persistence table, and the
# failure signatures behind most of the comments in here.
#
#
# WHY THIS EXISTS
#
# The bench laptop stands in for the rover so the ground station has something
# to talk to while the real rover is being worked on. Both links run at once
# and must not collide, so the bench gets its own SSID and its own /24. The
# rover's 10.42.0.0/24 link is left entirely alone.
#
#
# WHAT THIS DELIBERATELY DOES NOT DO
#
#   Enroll the Secure Boot MOK key. The lwfinger/rtw88 DKMS modules are signed
#   with a machine-owner key shim must trust, and enrollment happens in
#   MokManager before the kernel starts — it needs a reboot and a keypress at
#   the console. The script CHECKS for it and stops with instructions.
#
#   Touch anything under 10.42.0.0/24, the rover's hostapd config, or
#   rover-ap.service. If the rover link is broken that is restore-link.sh's job.
#
#   Set power save on the bench AP. mac80211 returns EOPNOTSUPP (-95) for AP
#   interfaces, and a failing ExecStartPost tears the whole AP down. Power save
#   is a CLIENT-side concern; the Pi's udev rule is what matters.
#
#   Exceed the regulatory power limit. --txpower is validated against what the
#   active regdomain actually grants and refuses anything higher, because
#   cfg80211 silently clamps over-limit values and the config would then lie.
#
set -uo pipefail

# ================================================================ defaults ==

# Interface name is MAC-derived, so it changes whenever the Alfa is swapped.
BENCH_IFACE=${BENCH_IFACE:-wlx00c0cabaeac8}
BENCH_SSID=${BENCH_SSID:-IndomitusBench}
BENCH_PSK=${BENCH_PSK:-12345678}
BENCH_BAND=${BENCH_BAND:-a}          # a = 5 GHz, bg = 2.4 GHz
BENCH_CHAN=${BENCH_CHAN:-44}
BENCH_TXPOWER=${BENCH_TXPOWER:-max}  # dBm, or "max" for the regdomain ceiling
BENCH_WIDTH=${BENCH_WIDTH:-40}       # 20 or 40
BENCH_ADDR=${BENCH_ADDR:-10.43.0.1}
BENCH_PREFIX=${BENCH_PREFIX:-24}
REGDOM=${REGDOM:-UA}

PI_ALFA=${PI_ALFA:-wlx00c0caba8237}
PI_ALFA_MAC=${PI_ALFA_MAC:-00:c0:ca:ba:82:37}
PI_BENCH_IP=${PI_BENCH_IP:-10.43.0.2}
PI_ROVER_IP=${PI_ROVER_IP:-10.42.0.2}
ROVER_SSID=${ROVER_SSID:-IndomitusRover}
ROVER_PSK=${ROVER_PSK:-12345678}

GS_NET=${GS_NET:-10.44.0.0/24}
GS_VIA=${GS_VIA:-10.44.0.1}
GS_PC_IP=${GS_PC_IP:-10.44.0.10}
GS_SSH=${GS_SSH:-}

DRY=0
DO_PI=1
DO_GS=1

usage() {
    cat <<EOF
Bring up the BENCH link — GS PC <-> mast Pi <-> bench laptop — in one shot.
Run this ON THE BENCH LAPTOP. Idempotent; see mast/BENCH-LINK.md.

  ./mast/restore-bench-link.sh                    # defaults
  ./mast/restore-bench-link.sh --dry              # change nothing
  ./mast/restore-bench-link.sh --channel 149
  ./mast/restore-bench-link.sh --band bg --channel 11
  GS_SSH=user@10.44.0.10 ./mast/restore-bench-link.sh

Options
  --iface NAME       AP interface            (default: $BENCH_IFACE)
  --ssid NAME        AP SSID                 (default: $BENCH_SSID)
  --psk SECRET       WPA2 passphrase, 8-63 chars
  --band a|bg        5 GHz or 2.4 GHz        (default: $BENCH_BAND)
  --channel N        channel                 (default: $BENCH_CHAN)
  --width 20|40      channel width           (default: $BENCH_WIDTH)
  --txpower N|max    dBm, capped by regdomain (default: $BENCH_TXPOWER)
  --regdom CC        regulatory country code (default: $REGDOM)
  --addr IP          AP address              (default: $BENCH_ADDR/$BENCH_PREFIX)
  --pi-ip IP         Pi's bench address      (default: $PI_BENCH_IP)
  --gs-net CIDR      network beyond the Pi   (default: $GS_NET)
  --no-pi            skip the mast Pi section
  --no-gs            skip the GS PC section
  --dry              print what would change, change nothing
  -h, --help         this

Environment: every default above can also be set as an env var of the same
name in caps (BENCH_SSID=..., GS_SSH=user@host, ...). Flags win over env.

Note on --txpower: the ceiling is the active regulatory domain, not the
adapter. Under UA every 5 GHz band is 20 dBm. The script refuses a higher
value rather than writing one cfg80211 will silently clamp.
EOF
    exit 0
}

while [ $# -gt 0 ]; do
    case "$1" in
        --iface)   BENCH_IFACE=$2; shift 2 ;;
        --ssid)    BENCH_SSID=$2; shift 2 ;;
        --psk)     BENCH_PSK=$2; shift 2 ;;
        --band)    BENCH_BAND=$2; shift 2 ;;
        --channel) BENCH_CHAN=$2; shift 2 ;;
        --width)   BENCH_WIDTH=$2; shift 2 ;;
        --txpower) BENCH_TXPOWER=$2; shift 2 ;;
        --regdom)  REGDOM=$2; shift 2 ;;
        --addr)    BENCH_ADDR=$2; shift 2 ;;
        --pi-ip)   PI_BENCH_IP=$2; shift 2 ;;
        --gs-net)  GS_NET=$2; shift 2 ;;
        --no-pi)   DO_PI=0; shift ;;
        --no-gs)   DO_GS=0; shift ;;
        --dry)     DRY=1; shift ;;
        -h|--help) usage ;;
        *) echo "unknown option: $1 (try --help)"; exit 1 ;;
    esac
done

BENCH_CIDR=$BENCH_ADDR/$BENCH_PREFIX
BENCH_NET=$(printf '%s.0/%s' "${BENCH_ADDR%.*}" "$BENCH_PREFIX")

PI_SSH_BENCH=admin@$PI_BENCH_IP
PI_SSH_WIRED=admin@$GS_VIA
PI=""

HOSTAPD_CONF=/etc/hostapd/bench-ap.conf
AP_UNIT=/etc/systemd/system/bench-ap.service
DNSMASQ_CONF=/etc/dnsmasq.d/bench-ap.conf
DNSMASQ_DROPIN=/etc/systemd/system/dnsmasq.service.d/10-after-bench-ap.conf
NM_UNMANAGED=/etc/NetworkManager/conf.d/99-bench-ap-unmanaged.conf
REGDOM_CONF=/etc/modprobe.d/cfg80211.conf

# --------------------------------------------------------------- reporting --

say()   { printf '  %s\n' "$*"; }
head_() { printf '\n\033[1m%s\033[0m\n' "$1"; }
did()   { printf '  \033[32m%s\033[0m %s\n' "changed" "$1"; }
ok()    { printf '  \033[2malready ok\033[0m %s\n' "$1"; }
warn()  { printf '  \033[33mWARN\033[0m  %s\n' "$1"; }
die()   { printf '  \033[31mFAIL\033[0m  %s\n' "$1"; exit 1; }

SSH="ssh -o BatchMode=yes -o ConnectTimeout=8"

loc_root()  { [ "$DRY" = 1 ] && { say "[dry] local: $*"; return 0; }; sudo "$@"; }
pi_read()   { [ -n "$PI" ] && $SSH "$PI" "sudo $*" 2>/dev/null; }
pi_root()   { [ -n "$PI" ] || return 1
              [ "$DRY" = 1 ] && { say "[dry] pi: $*"; return 0; }
              $SSH "$PI" "sudo $*" 2>/dev/null; }

locate_pi() {
    for t in "$PI_SSH_BENCH" "$PI_SSH_WIRED"; do
        $SSH "$t" true 2>/dev/null && { PI=$t; return 0; }
    done
    return 1
}

put_local() {
    local path=$1 content current
    content=$(cat)
    current=$(sudo cat "$path" 2>/dev/null)
    if [ "$current" = "$content" ]; then ok "$path"; return; fi
    [ "$DRY" = 1 ] && { did "$path (dry)"; return; }
    printf '%s\n' "$content" | sudo install -m "${PUT_MODE:-644}" /dev/stdin "$path" \
        || printf '%s\n' "$content" | sudo tee "$path" >/dev/null
    did "$path"
}

put_pi() {
    local path=$1 content current
    content=$(cat)
    current=$(pi_read "cat '$path'")
    if [ "$current" = "$content" ]; then ok "pi:$path"; return; fi
    [ "$DRY" = 1 ] && { did "pi:$path (dry)"; return; }
    printf '%s\n' "$content" | $SSH "$PI" "sudo install -m 644 /dev/stdin '$path'" \
        || printf '%s\n' "$content" | $SSH "$PI" "sudo tee '$path' >/dev/null"
    did "pi:$path"
}

# ============================================================ radio params ==

# Work out hw_mode, the HT40 secondary-channel direction, and the VHT centre
# index from the requested band/channel/width.
#
# 5 GHz 40 MHz channels come in fixed pairs — (36,40) (44,48) (52,56) ... and
# (149,153) (157,161). The LOWER member takes HT40+ (secondary above), the
# UPPER takes HT40-. Getting this backwards makes hostapd refuse to start.
# The VHT centre index is the midpoint: +2 for HT40+, -2 for HT40-.
compute_radio() {
    if [ "$BENCH_BAND" = "bg" ]; then
        HW_MODE=g
        USE_VHT=0
        # HT40+ needs chan+4 to exist; above ch7 there is no room, so go minus.
        [ "$BENCH_CHAN" -le 7 ] && HT_DIR='[HT40+]' || HT_DIR='[HT40-]'
        VHT_SEG0=0
    else
        HW_MODE=a
        USE_VHT=1
        local idx
        if [ "$BENCH_CHAN" -ge 149 ]; then
            idx=$(( (BENCH_CHAN - 149) / 4 ))
        else
            idx=$(( (BENCH_CHAN - 36) / 4 ))
        fi
        if [ $(( idx % 2 )) -eq 0 ]; then
            HT_DIR='[HT40+]'; VHT_SEG0=$(( BENCH_CHAN + 2 ))
        else
            HT_DIR='[HT40-]'; VHT_SEG0=$(( BENCH_CHAN - 2 ))
        fi
    fi
    if [ "$BENCH_WIDTH" = "20" ]; then
        HT_DIR=""
        VHT_SEG0=$BENCH_CHAN
    fi
}

# Highest power the ACTIVE regdomain grants on this channel, in whole dBm.
# cfg80211 silently clamps anything above it, so a config asking for more is a
# config that lies about what the radio is doing.
regdom_max_dbm() {
    local phy=$1 freq_line
    freq_line=$(iw phy "$phy" info 2>/dev/null | grep -E "\[$BENCH_CHAN\]" | head -1)
    sed -n 's/.*(\([0-9.]*\) dBm).*/\1/p' <<<"$freq_line" | cut -d. -f1
}

# =================================================================== BENCH ==

preflight() {
    head_ "Preflight (bench laptop)"

    ip link show "$BENCH_IFACE" >/dev/null 2>&1 \
        || die "$BENCH_IFACE not present. Is the Alfa plugged in? Override with --iface"
    ok "$BENCH_IFACE present"

    case "$BENCH_BAND" in a|bg) ;; *) die "--band must be 'a' or 'bg'" ;; esac
    case "$BENCH_WIDTH" in 20|40) ;; *) die "--width must be 20 or 40" ;; esac
    [ "${#BENCH_PSK}" -ge 8 ] || die "--psk must be at least 8 characters (WPA2 minimum)"

    if [ "$(mokutil --sb-state 2>/dev/null)" = "SecureBoot enabled" ]; then
        # NB: mokutil --test-key exits 1 even on success and prints a keyring
        # warning. Match the TEXT, never the exit status.
        local mok
        mok=$(mokutil --test-key /var/lib/shim-signed/mok/MOK.der 2>&1)
        case "$mok" in
            *"is already enrolled"*) ok "MOK key enrolled (Secure Boot)" ;;
            *) die "Secure Boot is on but no MOK key is enrolled — DKMS modules
        cannot load. Run:
            sudo mokutil --import /var/lib/shim-signed/mok/MOK.der
        then reboot and complete enrollment in MokManager." ;;
        esac
    else
        ok "Secure Boot off — module signing not enforced"
    fi

    # NB: capture, do not test `... | grep -q` directly. grep -q exits on the
    # first match, SIGPIPEs the upstream command, and `set -o pipefail` then
    # reports the whole pipeline as failed even though the match succeeded.
    local dkms_line
    dkms_line=$(dkms status 2>/dev/null | grep '^rtw88/' | head -1)
    if [ -n "$dkms_line" ]; then
        ok "DKMS $dkms_line"
    else
        warn "no rtw88 DKMS module — the in-tree rtw88_8812au may be in use instead.
        That works on kernel >= 6.13 but will NOT match the Pi and rover."
    fi

    command -v hostapd >/dev/null || die "hostapd not installed (sudo apt install hostapd)"
    ok "hostapd present"

    compute_radio
    ok "radio: band $BENCH_BAND ch$BENCH_CHAN ${BENCH_WIDTH}MHz ${HT_DIR:-[HT20]}"
}

setup_regdom() {
    # With no country set the kernel uses world domain 00, where every 5 GHz
    # channel is passive-scan/no-IR and hostapd cannot start. ch36 can *appear*
    # usable there purely because cfg80211 beacon hints clear no-IR on a
    # channel it has heard a beacon on — never rely on that.
    PUT_MODE=644 put_local "$REGDOM_CONF" <<EOF
# Without this the kernel falls back to world domain 00, where the 5 GHz band
# is passive-scan only and hostapd cannot start. Set it to where the hardware
# actually is; this is a compliance setting, not a lock to route around.
options cfg80211 ieee80211_regdom=$REGDOM
EOF
    local reg
    reg=$(iw reg get 2>/dev/null | awk '/^country/{print $2; exit}')
    if [ "${reg%%:*}" = "$REGDOM" ]; then
        ok "regdomain active: ${reg%%:*}"
    else
        loc_root iw reg set "$REGDOM" && did "regdomain -> $REGDOM (runtime; file above persists it)"
        sleep 1
    fi

    # Now that the domain is right, resolve the power ceiling and validate.
    local phy limit
    phy=phy$(iw dev "$BENCH_IFACE" info 2>/dev/null | awk '/wiphy/{print $2}')
    limit=$(regdom_max_dbm "$phy")
    [ -n "$limit" ] || { warn "cannot read a power limit for ch$BENCH_CHAN — is it available in $REGDOM?"; limit=20; }

    if [ "$BENCH_TXPOWER" = "max" ]; then
        BENCH_TXPOWER=$limit
        ok "txpower: ${BENCH_TXPOWER} dBm (regdomain ceiling for ch$BENCH_CHAN)"
    elif [ "$BENCH_TXPOWER" -gt "$limit" ] 2>/dev/null; then
        die "--txpower $BENCH_TXPOWER exceeds what $REGDOM permits on ch$BENCH_CHAN (${limit} dBm).
        cfg80211 would silently clamp it, so the config would not match reality.
        This ceiling is regulatory, not a hardware limit — raising it is a
        licensing question, not a configuration one."
    else
        ok "txpower: ${BENCH_TXPOWER} dBm (ceiling ${limit} dBm)"
    fi
    TXPOWER_MBM=$(( BENCH_TXPOWER * 100 ))
}

restore_bench() {
    head_ "Bench laptop — AP"
    setup_regdom

    # hostapd owns the radio; NetworkManager must not also manage it or the two
    # fight and the AP flaps.
    #   '+=' APPENDS. Plain '=' would override the key already set by
    #   10-ubuntu-fan.conf — conf.d overrides a repeated key, it does not merge.
    #   Same trap as the rover's 99-rover-ap-unmanaged.conf.
    local mac
    mac=$(cat "/sys/class/net/$BENCH_IFACE/address")
    PUT_MODE=644 put_local "$NM_UNMANAGED" <<EOF
# hostapd owns this radio (bench-ap.service). If NetworkManager also manages
# it, both fight over interface state and the AP flaps.
# '+=' appends rather than replacing: 10-ubuntu-fan.conf already sets this key,
# and conf.d overrides a repeated key instead of merging it.
[keyfile]
unmanaged-devices+=mac:$mac
EOF

    # ht_capab/vht_capab are what THIS radio advertises (RTL8812AU: HT 0x196e,
    # VHT 0x03d071a2). hostapd validates them against the driver and refuses to
    # start on a mismatch, so do not add flags the chip does not report —
    # notably NO TX-STBC and NO LDPC for HT on this part.
    local vht_block=""
    if [ "$USE_VHT" = 1 ]; then
        vht_block="ieee80211ac=1
# vht_oper_chwidth 0 = 20/40 MHz (1 would be 80). Centre index is the midpoint
# of the 40 MHz pair. This radio reports \"neither 160 nor 80+80\".
vht_oper_chwidth=0
vht_oper_centr_freq_seg0_idx=$VHT_SEG0
vht_capab=[MAX-MPDU-11454][SHORT-GI-80][TX-STBC-2BY1]"
    fi

    PUT_MODE=600 put_local "$HOSTAPD_CONF" <<EOF
# Generated by mast/restore-bench-link.sh — edit that, not this.
interface=$BENCH_IFACE
driver=nl80211
ssid=$BENCH_SSID
country_code=$REGDOM
ieee80211d=1

hw_mode=$HW_MODE
channel=$BENCH_CHAN
ieee80211n=1
wmm_enabled=1
$vht_block

# ${BENCH_WIDTH} MHz. The HT40 direction is fixed by which half of the channel
# pair the primary sits in; reversing it makes hostapd refuse to start.
ht_capab=${HT_DIR}[SHORT-GI-20][SHORT-GI-40][RX-STBC1][MAX-AMSDU-7935][DSSS_CCK-40]

auth_algs=1
wpa=2
wpa_passphrase=$BENCH_PSK
wpa_key_mgmt=WPA-PSK
rsn_pairwise=CCMP

# Makes per-station rates readable: hostapd_cli -i $BENCH_IFACE all_sta
ctrl_interface=/var/run/hostapd
EOF

    # Address, return route, and tx power all come up with the AP. The rover
    # carries the equivalent $GS_NET via $PI_ROVER_IP; leaving it as a runtime
    # `ip route add` means a reboot silently drops it.
    #
    # tx power is best-effort and logged, NOT fatal: a failing ExecStartPost
    # tears the AP down, which is exactly how the power_save call broke it.
    PUT_MODE=644 put_local "$AP_UNIT" <<EOF
[Unit]
Description=Indomitus bench AP (hostapd, ch$BENCH_CHAN ${BENCH_WIDTH}MHz)
After=network.target
Wants=network.target

[Service]
Type=simple
ExecStart=/usr/sbin/hostapd $HOSTAPD_CONF
# 'replace' not 'add', so a restart with either already present is not an error.
# NO power_save call: mac80211 returns EOPNOTSUPP for AP interfaces and a
# failing ExecStartPost tears the AP down.
ExecStartPost=/bin/sh -c 'sleep 3; ip addr replace $BENCH_CIDR dev $BENCH_IFACE; ip route replace $GS_NET via $PI_BENCH_IP dev $BENCH_IFACE; iw dev $BENCH_IFACE set txpower fixed $TXPOWER_MBM || echo "bench-ap: txpower ${BENCH_TXPOWER}dBm rejected, using driver default" >&2'
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

    [ "$DRY" = 1 ] && return 0

    loc_root systemctl restart NetworkManager >/dev/null; sleep 4
    loc_root systemctl daemon-reload
    loc_root systemctl reset-failed bench-ap.service >/dev/null 2>&1
    loc_root systemctl enable --now bench-ap.service >/dev/null
    loc_root systemctl restart bench-ap.service; sleep 8
    did "bench-ap.service restarted"
}

restore_bench_dhcp() {
    head_ "Bench laptop — DHCP"

    if ! command -v dnsmasq >/dev/null; then
        warn "dnsmasq not installed — no DHCP. The Pi is STATIC so the GS chain
        still works, but any other client hangs at 'obtaining IP address'.
        Install with: sudo apt install dnsmasq"
        return
    fi

    local base pool_lo pool_hi
    base=${BENCH_ADDR%.*}
    pool_lo=$base.50
    pool_hi=$base.150

    # bind-interfaces, NOT bind-dynamic: /etc/dnsmasq.d/ubuntu-fan already sets
    # bind-interfaces and dnsmasq refuses to start if both appear. Note that
    # `dnsmasq --test` reports "syntax check OK" on that combination anyway —
    # the conflict is only enforced at startup.
    PUT_MODE=644 put_local "$DNSMASQ_CONF" <<EOF
# Generated by mast/restore-bench-link.sh. Serves ONLY the Alfa AP interface.
#
# bind-interfaces (NOT bind-dynamic): /etc/dnsmasq.d/ubuntu-fan already sets
# bind-interfaces, and dnsmasq exits if both are present. Because
# bind-interfaces needs the interface to exist at startup, dnsmasq.service
# carries a drop-in ordering it After=bench-ap.service.
interface=$BENCH_IFACE
except-interface=lo
bind-interfaces

# Pool clear of .1 (AP) and .2 (the mast Pi's static address).
dhcp-range=$pool_lo,$pool_hi,12h
dhcp-authoritative

# Hand out NO default route (option 3) and NO DNS (option 6). 99-mast.yaml is
# explicit that a DHCP-supplied default route competing with the Pi's own
# routing is the thing to avoid. An empty value means "do not send it".
dhcp-option=3
dhcp-option=6

# Fixed address for the mast Pi's Alfa if it ever asks via DHCP instead of
# using its netplan static. Harmless if it never does.
dhcp-host=$PI_ALFA_MAC,$PI_BENCH_IP
EOF

    loc_root mkdir -p "$(dirname "$DNSMASQ_DROPIN")"
    PUT_MODE=644 put_local "$DNSMASQ_DROPIN" <<'EOF'
# bind-interfaces requires the AP interface to exist and hold its address
# before dnsmasq starts. bench-ap.service assigns it in ExecStartPost and is
# not "active" until that completes, so After= is sufficient.
[Unit]
After=bench-ap.service
Wants=bench-ap.service

[Service]
Restart=on-failure
RestartSec=5
EOF

    [ "$DRY" = 1 ] && return 0
    loc_root systemctl daemon-reload
    loc_root systemctl reset-failed dnsmasq.service >/dev/null 2>&1
    loc_root systemctl enable --now dnsmasq >/dev/null
    loc_root systemctl restart dnsmasq; sleep 3
    did "dnsmasq restarted"
}

# ==================================================================== PI ====

restore_pi() {
    head_ "Mast Pi"
    [ "$DO_PI" = 1 ] || { say "skipped (--no-pi)"; return; }
    locate_pi || { warn "unreachable over $PI_SSH_BENCH or $PI_SSH_WIRED — skipping.
        Configure it from the GS PC over ethernet; see mast/BENCH-LINK.md."; return 1; }
    say "via ${PI#*@}"

    # The Pi must know BOTH SSIDs and hold BOTH addresses. netplan matches on
    # SSID alone so it follows whichever AP is up; the two addresses are each
    # on-link only on their own network, so it is correct either way and the
    # rover's config needs no change.
    put_pi /etc/netplan/99-mast.yaml <<EOF
# /etc/netplan/99-mast.yaml on the ground-station mast Pi (GSRapberry).
# Written by mast/restore-bench-link.sh — see mast/BENCH-LINK.md.
#
# Replaces the original 01-mast.yaml, which never took effect: it declared
# \`renderer: networked\` (a typo for \`networkd\`, so netplan rejected the whole
# file), and even once corrected it sorted BEFORE 50-cloud-init.yaml, whose
# \`dhcp4: true\` for eth0 would have overridden it. Netplan merges in lexical
# order with last-one-wins, so this must stay 99-.
#
# Topology (see mast/README.md and mast/BENCH-LINK.md):
#   GS PC  eno1/enp2s0  $GS_PC_IP/24   routes to 10.42.0.0/24 AND $BENCH_NET
#            |          point-to-point through the TP-Link POE160S injector
#   Pi     eth0         $GS_VIA/24    <- this file
#   Pi     $PI_ALFA   $PI_ROVER_IP/24    <- rover link
#                    + $PI_BENCH_IP/24    <- bench link
#   Rover  wlan0        10.42.0.1/24    AP, SSID $ROVER_SSID
#   Bench  wlx00c0...   $BENCH_ADDR/24    AP, SSID $BENCH_SSID
#
# Deliberately NO default route on either interface here. Every hop in the rover
# data path is on-link or covered by one static route, so nothing depends on a
# gateway. The Pi's own internet (apt, DKMS rebuilds after a kernel update)
# comes from the onboard wlan0 in 50-cloud-init.yaml; at competition there is no
# internet and none is needed.
#
# Letting the Alfa take a DHCP lease is what we must avoid: the rover's AP runs
# NetworkManager \`ipv4.method shared\`, whose DHCP hands out a default route, and
# that route would compete with wlan0's for the Pi's default. Static, gateway-
# less, and below the shared-mode pool start (~.10) so it cannot collide. The
# bench AP suppresses DHCP options 3 and 6 for the same reason.
#
# BOTH addresses stay assigned permanently. Each is on-link only on its own
# network, so whichever AP the Pi associates with the right one applies and the
# other is inert. That is what lets the rover link and the bench link exist at
# the same time without collision.
#
# netplan has NO priority field for access-points: with both APs in range,
# wpa_supplicant chooses by signal. To force one, power the other down.
#
# eth0 keeps IPv6 link-local: that is the out-of-band recovery path
# (ssh admin@fe80::8aa2:9eff:fec6:5546%<gs-iface>) if the IPv4 config is wrong.
#
# NOTE: ERC requires "ERC_<TeamName>_(A/B)" - team name is UCU Space Robotics,
# not Indomitus, which is the rover - so expect to change access-points: here.
#
# No band or channel appears in this file and none should. netplan matches on
# SSID alone, so the client follows the AP wherever it moves.
network:
  version: 2
  renderer: networkd
  ethernets:
    eth0:
      dhcp4: no
      dhcp6: no
      link-local: [ipv6]
      addresses: [$GS_VIA/24]
      optional: true
  wifis:
    $PI_ALFA:
      dhcp4: no
      dhcp6: no
      addresses: [$PI_ROVER_IP/24, $PI_BENCH_IP/24]
      optional: true
      access-points:
        "$ROVER_SSID":
          password: "$ROVER_PSK"
        "$BENCH_SSID":
          password: "$BENCH_PSK"
EOF

    [ "$DRY" = 1 ] && return 0

    # THE ONE THAT BITES: `netplan generate` + restarting netplan-wpa-*.service
    # restarts wpa_supplicant ONLY, which is association. ADDRESSES are applied
    # by systemd-networkd, which neither command touches — so the Pi associates
    # and then sits there with no IP, looking exactly like a broken AP.
    pi_root "netplan apply" >/dev/null
    did "netplan apply (addresses AND association — generate alone is not enough)"
    sleep 6

    # The Pi does not reliably reassociate after an AP restart.
    pi_root "systemctl restart netplan-wpa-$PI_ALFA.service" >/dev/null; sleep 10
    did "reassociated"

    local ps
    ps=$(pi_read "iw dev $PI_ALFA get power_save" | awk '{print $3}')
    if [ "$ps" = "off" ]; then
        ok "client power save off"
    else
        warn "client power save is '${ps:-?}' — costs ~115ms average latency.
        /etc/udev/rules.d/99-alfa-powersave-off.rules should have fired;
        check it exists and matches $PI_ALFA_MAC."
    fi
}

# ==================================================================== GS ====

restore_gs() {
    head_ "GS PC"
    [ "$DO_GS" = 1 ] || { say "skipped (--no-gs)"; return; }

    local cmds="nmcli -t -f NAME,DEVICE connection show --active   # find the profile on enp2s0
nmcli con modify PROFILE +ipv4.routes \"$BENCH_NET $GS_VIA\"
nmcli con up PROFILE            # a full activation; 'device reapply' does NOT
                                # reliably push route changes
ip route get ${BENCH_ADDR}      # must print: via $GS_VIA dev enp2s0"

    if [ -z "$GS_SSH" ] || ! $SSH "$GS_SSH" true 2>/dev/null; then
        warn "${GS_SSH:-no GS_SSH set} — run these on the GS PC yourself:"
        printf '%s\n' "$cmds" | sed 's/^/      /'
        return
    fi

    local prof
    prof=$($SSH "$GS_SSH" "nmcli -t -f NAME,DEVICE connection show --active" 2>/dev/null \
           | awk -F: '$2=="enp2s0"{print $1; exit}')
    [ -n "$prof" ] || { warn "no active profile on enp2s0 — run the commands above by hand"; return; }
    say "active profile on enp2s0: $prof"

    # Capture rather than testing a `| grep -q` pipeline: grep -q's early exit
    # SIGPIPEs ssh and pipefail would report a successful match as a failure.
    local gs_routes
    gs_routes=$($SSH "$GS_SSH" "nmcli -g ipv4.routes connection show '$prof'" 2>/dev/null)
    if [ "${gs_routes#*"$BENCH_NET"}" != "$gs_routes" ]; then
        ok "$BENCH_NET route present on $prof"
    else
        [ "$DRY" = 1 ] && { did "would add $BENCH_NET via $GS_VIA to $prof (dry)"; return; }
        $SSH "$GS_SSH" "nmcli con modify '$prof' +ipv4.routes '$BENCH_NET $GS_VIA'" \
            && did "added $BENCH_NET via $GS_VIA to $prof"
        # A full activation, not `device reapply` — reapply does not reliably
        # push route changes and leaves 'destination net unreachable'.
        $SSH "$GS_SSH" "nmcli con up '$prof'" >/dev/null 2>&1
        did "reactivated $prof"
    fi
}

# ================================================================= VERIFY ===

verify() {
    head_ "Verify"
    [ "$DRY" = 1 ] && { say "[dry] skipped"; return; }

    printf '  %-26s %s\n' "bench-ap.service" \
        "$(systemctl is-active bench-ap.service) / $(systemctl is-enabled bench-ap.service 2>/dev/null)"
    printf '  %-26s %s\n' "restarts" "$(systemctl show bench-ap.service -p NRestarts --value)"
    printf '  %-26s %s\n' "dnsmasq" "$(systemctl is-active dnsmasq 2>/dev/null)"

    local info w tx
    info=$(iw dev "$BENCH_IFACE" info 2>/dev/null)
    w=$(sed -n 's/.*width: \([0-9]*\) MHz.*/\1/p' <<<"$info")
    tx=$(awk '/txpower/{print $2}' <<<"$info")
    if [ "$w" = "$BENCH_WIDTH" ]; then
        printf '  %-26s %s\n' "AP width" "${w} MHz"
    else
        warn "AP width ${w:-?} MHz, asked for $BENCH_WIDTH"
    fi
    printf '  %-26s %s\n' "txpower" "${tx:-?} dBm (asked ${BENCH_TXPOWER})"
    printf '  %-26s %s\n' "stations" "$(iw dev "$BENCH_IFACE" station dump 2>/dev/null | grep -c '^Station')"

    # Two-stream MIMO is bounded by the weaker chain; a 10 dB gap once halved
    # downlink until the antenna connector was reseated.
    local chains a b spread
    chains=$(iw dev "$BENCH_IFACE" station dump 2>/dev/null \
             | grep -m1 'signal:' | grep -oE '\[-?[0-9]+, *-?[0-9]+\]' | tr -d '[]' | tr ',' ' ')
    if [ -n "$chains" ]; then
        a=$(awk '{print $1}' <<<"$chains"); b=$(awk '{print $2}' <<<"$chains")
        spread=$(( a > b ? a - b : b - a ))
        if [ "$spread" -le 5 ]; then
            printf '  %-26s %s\n' "antenna chains" "${a}/${b} dBm (${spread} dB)"
        else
            warn "antenna chains ${a}/${b} dBm — ${spread} dB gap, reseat the antenna"
        fi
    fi

    echo
    for h in "$PI_BENCH_IP" "$GS_VIA" "$GS_PC_IP"; do
        printf '  %-26s ' "ping $h"
        ping -c2 -W2 "$h" >/dev/null 2>&1 && echo OK || echo FAIL
    done
}

# ============================================================================

printf '\033[1mRestoring Indomitus BENCH link\033[0m  %s%s\n' \
    "$(date -Is)" "$([ "$DRY" = 1 ] && echo '  [DRY RUN]')"
say "$BENCH_SSID  ch$BENCH_CHAN/${BENCH_WIDTH}MHz  $BENCH_CIDR  regdom $REGDOM"
preflight
restore_bench
restore_bench_dhcp
restore_pi
restore_gs
verify
printf '\n  Rover link (10.42.0.0/24) untouched — use mast/restore-link.sh for that.\n'

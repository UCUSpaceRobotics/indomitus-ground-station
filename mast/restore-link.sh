#!/bin/bash
# Put the Wi-Fi link back into its known-good state, on all three hosts.
#
#     ROVER_PW=... ./mast/restore-link.sh          # do it
#     ROVER_PW=... ./mast/restore-link.sh --dry    # print what would change
#
# Idempotent: safe to run repeatedly, and it only touches what is actually
# wrong. Pair with verify-link.sh — verify, restore, verify.
#
# Every file written here is reproduced in full, so this script *is* the
# authoritative copy of the configuration. If the rover's SD card dies, this
# plus a stock image gets the link back.
#
# WHAT THIS DELIBERATELY DOES NOT TOUCH
#   /etc/NetworkManager/conf.d/10-globally-managed-devices.conf on the rover.
#   It was truncated to 0 bytes on 2026-08-24 and the original was never
#   captured. Ubuntu ships it containing `unmanaged-devices=*,except:type:wifi`,
#   which would make NM stop managing ETHERNET — and rover-lifeline on
#   enP1p1s0 is an NM-managed ethernet profile. Restoring the distro default
#   would kill the wired lifeline. Empty is correct here; leave it empty.

set -u

PI=admin@10.44.0.1
ROVER_LIFELINE=indomitus-rover@10.45.0.51
ROVER_WIFI=indomitus-rover@10.42.0.1
ROVER_PW=${ROVER_PW:-}
DRY=0
[ "${1:-}" = "--dry" ] && DRY=1

PI_ALFA=wlx00c0caba8237
ROVER_ALFA=wlx00c0caba86c1
GS_MAST_IF=eno1
SSH="ssh -o BatchMode=yes -o ConnectTimeout=8"
ROVER=""

say()  { printf '  %s\n' "$*"; }
head_(){ printf '\n\033[1m%s\033[0m\n' "$1"; }
did()  { printf '  \033[32m%s\033[0m %s\n' "changed" "$1"; }
ok()   { printf '  \033[2malready ok\033[0m %s\n' "$1"; }

# Reads always execute, even under --dry: inspecting is harmless, and stubbing
# it makes a dry run claim every file needs changing.
pi_read()    { $SSH "$PI" "sudo $*" 2>/dev/null; }
rover_read() {
    [ -n "$ROVER" ] || return 1
    [ -n "$ROVER_PW" ] || return 2
    printf '%s\n' "$ROVER_PW" | $SSH "$ROVER" "sudo -S -p '' $*" 2>/dev/null
}

# Mutations respect --dry.
pi_root() { [ "$DRY" = 1 ] && { say "[dry] pi: $*"; return 0; }; pi_read "$@"; }
rover_root() {
    [ -n "$ROVER" ] || return 1
    [ "$DRY" = 1 ] && { say "[dry] rover: $*"; return 0; }
    [ -n "$ROVER_PW" ] || { say "!! need ROVER_PW"; return 2; }
    rover_read "$@"
}

locate_rover() {
    for t in "$ROVER_LIFELINE" "$ROVER_WIFI"; do
        $SSH "$t" true 2>/dev/null && { ROVER=$t; return 0; }
    done
    return 1
}

# put_file <host-fn> <path> <<<"content"  - write only if content differs.
put_file() {
    local runner=$1 path=$2 content
    content=$(cat)
    local reader="${runner%_root}_read"
    local current
    current=$("$reader" "cat '$path' 2>/dev/null")
    if [ "$current" = "$content" ]; then ok "$path"; return; fi
    [ "$DRY" = 1 ] && { did "$path (dry)"; return; }
    "$runner" "install -m 644 /dev/stdin '$path'" <<<"$content" >/dev/null 2>&1 \
        || printf '%s\n' "$content" | "$runner" "tee '$path' >/dev/null"
    did "$path"
}

# ==================================================================== ROVER ===

restore_rover() {
    head_ "Rover"
    locate_rover || { say "!! unreachable via lifeline or Wi-Fi — skipping"; return 1; }
    say "via ${ROVER#*@}"

    # 1. Both radios hidden from NetworkManager, in ONE key. conf.d OVERRIDES
    #    the same key rather than merging, so two files means the later one
    #    silently wins and the other radio quietly goes back to NM.
    put_file rover_root /etc/NetworkManager/conf.d/99-rover-ap-unmanaged.conf <<'EOF'
# hostapd owns the Alfa radio (see rover-ap.service). If NetworkManager also
# manages it, both fight over the interface state and the AP flaps.
#
# The onboard Intel 8265 is here too: NM scans a disconnected wifi device across
# all 38 channels including 5180 MHz - the Alfa AP channel - from inside the
# Jetson. Both MACs must share ONE unmanaged-devices key: conf.d files override
# the same key rather than merging it.
[keyfile]
unmanaged-devices=mac:00:c0:ca:ba:86:c1;mac:74:04:f1:bc:7f:0f
EOF

    # 2. Anything that hands the AP radio back to NM. A `managed=1` match-device
    #    rule here puts the interface into `type managed` and the AP stops
    #    serving while hostapd is still "active" — a confusing failure.
    local strays
    strays=$(rover_read "ls /etc/NetworkManager/conf.d/ 2>/dev/null" | grep -E '99-manage-wifi\.conf|99-rover-ap-unmanaged\.conf\.disabled')
    if [ -n "$strays" ]; then
        rover_root "rm -f /etc/NetworkManager/conf.d/99-manage-wifi.conf /etc/NetworkManager/conf.d/99-rover-ap-unmanaged.conf.disabled"
        did "removed NM overrides: $(tr '\n' ' ' <<<"$strays")"
    else
        ok "no NM overrides present"
    fi

    # 3. Vendor driver out, mainline rtw88 in.
    put_file rover_root /etc/modprobe.d/99-blacklist-88XXau.conf <<'EOF'
blacklist 8812au
blacklist 88XXau
EOF
    # rtw88 must NOT be blacklisted; the vendor package's own conf ships lines
    # that block it. Comment rather than delete so the file stays recognisable.
    if rover_read "grep -qE '^blacklist rtw(88)?_8812au' /etc/modprobe.d/8812au.conf"; then
        rover_root "sed -i 's/^blacklist rtw88_8812au/#blacklist rtw88_8812au/; s/^blacklist rtw_8812au/#blacklist rtw_8812au/' /etc/modprobe.d/8812au.conf"
        did "un-blacklisted rtw88 in 8812au.conf"
    else
        ok "rtw88 not blacklisted"
    fi

    # 4. AP config: 40 MHz VHT on ch36. Swap to -vht80 for ~2x throughput.
    if rover_read "cmp -s /etc/hostapd/rover-ap.conf /etc/hostapd/rover-ap-rtw88-vht40.conf"; then
        ok "hostapd config = rtw88-vht40"
    else
        rover_root "cp /etc/hostapd/rover-ap-rtw88-vht40.conf /etc/hostapd/rover-ap.conf"
        did "hostapd config -> rtw88-vht40"
    fi

    [ "$DRY" = 1 ] && return 0

    rover_root "systemctl reload-or-restart NetworkManager" >/dev/null; sleep 5
    rover_root "systemctl enable --now rover-ap.service" >/dev/null
    rover_root "systemctl restart rover-ap.service" >/dev/null; sleep 8
    did "restarted NetworkManager + rover-ap"

    # 5. AP-side power save. rover-ap.service has an ExecStartPost for this but
    #    it is prefixed `-`, so a failure is silent.
    rover_root "iw dev $ROVER_ALFA set power_save off" >/dev/null
    did "AP power_save off"

    # 6. Intel 8265 WiFi off, Bluetooth deliberately left up (separate rfkill
    #    entry, separate bus). Resolve phy -> index; NEVER hardcode, and never
    #    use `rfkill block wifi` — that would also block the Alfa AP.
    local iphy idx
    iphy=$(rover_read "iw dev wlP7p1s0 info 2>/dev/null | awk '/wiphy/{print \$2}'")
    if [ -n "$iphy" ]; then
        idx=$(rover_read "rfkill list" | awk -F: -v p="phy${iphy}" '$2 ~ p {gsub(/ /,"",$1); print $1}')
        [ -n "$idx" ] && { rover_root "rfkill block $idx" >/dev/null; rover_root "ip link set wlP7p1s0 down" >/dev/null; did "Intel phy$iphy blocked (rfkill $idx)"; }
    else
        ok "no Intel radio present"
    fi

    # 7. DHCP for guests. BindsTo=rover-ap.service, so it dies with the AP and
    #    does not come back on its own. The mast Pi has a STATIC 10.42.0.2, so a
    #    dead DHCP server is invisible until some other device tries to join.
    rover_root "systemctl enable --now rover-ap-dhcp.service" >/dev/null
    did "rover-ap-dhcp started"
}

# ======================================================================= PI ===

restore_pi() {
    head_ "Mast Pi"
    $SSH "$PI" true 2>/dev/null || { say "!! unreachable — skipping"; return 1; }

    # The vendor modalias for 0bda:8812 matches more broadly than rtw88's, so
    # with the vendor driver blacklisted NOTHING auto-loads and the Alfa never
    # appears at all. Force it.
    put_file pi_root /etc/modules-load.d/rtw88-alfa.conf <<'EOF'
# The vendor 8812au modalias for 0bda:8812 is broader (ic*) than rtw_8812au's
# (icFF/iscFF/ipFF), so with the vendor driver blacklisted nothing auto-loads at
# boot and the Alfa never appears. Load it explicitly.
rtw_8812au
EOF

    put_file pi_root /etc/modprobe.d/99-blacklist-vendor-8812au.conf <<'EOF'
blacklist 8812au
blacklist 88XXau
EOF

    # mac80211 defaults power save ON for managed interfaces; measured cost was
    # 117ms average RTT with 400ms spikes. MAC-matched so it survives the
    # interface rename and fires on every driver reload, not just boot.
    put_file pi_root /etc/udev/rules.d/99-alfa-powersave-off.rules <<'EOF'
# mac80211 defaults power_save ON for managed interfaces. On the Alfa client
# that costs ~115ms of average latency and 400ms spikes at full throughput
# (measured 2026-08-23: avg RTT 117ms -> 2.2ms with it off).
#
# Matched on MAC, not interface name, so it survives the udev rename
# (wlan1 -> wlx00c0caba8237) and fires on every driver reload, not just boot.
ACTION=="add", SUBSYSTEM=="net", ATTR{address}=="00:c0:ca:ba:82:37", RUN+="/usr/sbin/iw dev $name set power_save off"
EOF

    # THE ONE THAT BITES: the vendor package's 8812au.conf blacklists rtw88.
    # A manual `modprobe rtw_8812au` overrides a blacklist, so the link looks
    # fine — but systemd-modules-load RESPECTS it, so the Alfa silently fails to
    # appear on the next reboot. Check with:
    #   systemd-analyze cat-config modprobe.d | grep -E '^blacklist rtw'
    if pi_read "grep -qE '^blacklist rtw(88)?_8812au' /etc/modprobe.d/8812au.conf"; then
        pi_root "cp -n /etc/modprobe.d/8812au.conf /etc/modprobe.d/8812au.conf.prertw88"
        pi_root "sed -i 's/^blacklist rtw88_8812au/#blacklist rtw88_8812au/; s/^blacklist rtw_8812au/#blacklist rtw_8812au/' /etc/modprobe.d/8812au.conf"
        did "un-blacklisted rtw88 in 8812au.conf (would have broken next boot)"
    else
        ok "rtw88 not blacklisted"
    fi

    [ "$DRY" = 1 ] && return 0

    pi_root "udevadm control --reload-rules" >/dev/null
    if ! pi_read "lsmod | grep -q '^rtw_8812au'"; then
        pi_root "modprobe rtw_8812au" >/dev/null; sleep 6
        did "loaded rtw_8812au"
    else
        ok "rtw_8812au loaded"
    fi

    # Onboard radio rescans all 42 channels every ~11s, including the AP's own.
    # netplan re-enables it every boot; see STARTUP.md.
    pi_root "pkill -f 'wpa-wlan0'" >/dev/null 2>&1
    pi_root "ip link set wlan0 down" >/dev/null 2>&1
    did "onboard wlan0 down"

    pi_root "systemctl restart netplan-wpa-${PI_ALFA}.service" >/dev/null; sleep 12
    did "reassociated (AP restarts do not reconnect this on their own)"
}

# ==================================================================== GS PC ===

# Idempotent autoconnect setter. The old version announced "changed" even when
# nmcli failed, which is how a deleted rover-recovery still reported success.
set_autoconnect() {
    local con=$1 want=$2 cur
    cur=$(nmcli -t -f connection.autoconnect con show "$con" 2>/dev/null | cut -d: -f2)
    if [ -z "$cur" ]; then
        say "!! no such connection: $con"
        return 1
    fi
    if [ "$cur" = "$want" ]; then
        ok "$con autoconnect already $want"
        return 0
    fi
    if [ "$DRY" = 1 ]; then
        say "[dry] gs: nmcli con modify $con connection.autoconnect $want"
        return 0
    fi
    if nmcli con modify "$con" connection.autoconnect "$want"; then
        did "$con autoconnect $want"
    else
        say "!! failed to set $con autoconnect $want"
        return 1
    fi
}

restore_gs() {
    head_ "GS PC"

    # Exactly one ethernet profile may autoconnect. Both gs-mast and gs-mast-p2
    # carry 10.44.0.10/24, so if they come up together the Pi is reachable by
    # whichever cable won the metric, which is not always the one you plugged.
    #
    # gs-mast (eno1) is the one that must win. gs-mast-p2 (enp2s0) is a trap: the
    # port links and negotiates 1000Mb/s but its receive path is dead, so ARP for
    # the Pi goes out and nothing ever comes back. It looks healthy and drops
    # everything. The mast cable belongs in eno1.
    set_autoconnect gs-mast    yes
    set_autoconnect gs-mast-p2 no

    # EEE on the GS transmit path costs 514 vs 887 Mbit/s to the Pi. Needs root
    # here, which this script does not assume it has.
    if [ "$(ethtool --show-eee "$GS_MAST_IF" 2>/dev/null | awk '/EEE status/{print $3}')" != "disabled" ]; then
        say "!! run yourself:  sudo ethtool --set-eee $GS_MAST_IF eee off   (514 -> 887 Mbit/s)"
    else
        ok "$GS_MAST_IF EEE disabled"
    fi

    # Video must not go over rosbridge — it base64s each frame into JSON and the
    # browser queue grows unbounded (measured 30-60s of lag).
    if ! ss -lnt 2>/dev/null | grep -q ':8080'; then
        [ "$DRY" = 0 ] && docker exec -d indomitus_ground_station bash -lc \
            'source /opt/ros/humble/setup.bash; exec ros2 run web_video_server web_video_server --ros-args -p port:=8080 -p default_stream_type:=ros_compressed -p default_snapshot_type:=ros_compressed >/tmp/wvs.log 2>&1'
        did "started web_video_server (ros_compressed defaults)"
    else
        ok "web_video_server listening"
    fi
}

# ============================================================================ =

printf '\033[1mRestoring Indomitus link state\033[0m  %s%s\n' "$(date -Is)" "$([ "$DRY" = 1 ] && echo '  [DRY RUN]')"
restore_gs
restore_pi
restore_rover
printf '\n  Now run: \033[1mROVER_PW=... ./mast/verify-link.sh\033[0m\n'

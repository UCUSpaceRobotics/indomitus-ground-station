#!/bin/bash
# Verify every setting the Wi-Fi link depends on, across all three hosts.
#
# Run from the GS PC:   ./mast/verify-link.sh
#
# Each check prints PASS/FAIL/WARN plus what it actually found, so a failure
# tells you what to fix rather than just that something is wrong. Exit code is
# the number of FAILs.
#
# Rover sudo needs a password. Export it, or the rover's root-only checks are
# reported SKIP rather than failing the run:
#     ROVER_PW=... ./mast/verify-link.sh
#
# Companion to STARTUP.md, which explains *why* each of these matters.

set -u

PI=admin@10.44.0.1
ROVER_USER=${ROVER_USER:-jetson}
ROVER_WIFI=$ROVER_USER@10.42.0.1
ROVER_LIFELINE=$ROVER_USER@10.45.0.51
ROVER_PW=${ROVER_PW:-}
GS_MAST_IF=eno1

PI_ALFA=wlx00c0caba8237
ROVER_ALFA=wlx00c0caba86c1
ROVER_ALFA_MAC=00:c0:ca:ba:86:c1
INTEL_MAC=74:04:f1:bc:7f:0f

fails=0
SSH="ssh -o BatchMode=yes -o ConnectTimeout=8"
ROVER=""        # resolved by locate_rover
ROVER_HINT=""   # why locate_rover failed, when it can tell

# ---------------------------------------------------------------- reporting --

pass() { printf '  \033[32mPASS\033[0m  %-42s %s\n' "$1" "${2:-}"; }
fail() { printf '  \033[31mFAIL\033[0m  %-42s %s\n' "$1" "${2:-}"; fails=$((fails + 1)); }
warn() { printf '  \033[33mWARN\033[0m  %-42s %s\n' "$1" "${2:-}"; }
skip() { printf '  ----  %-42s %s\n' "$1" "${2:-}"; }
head_() { printf '\n\033[1m%s\033[0m\n' "$1"; }

# check <label> <expected> <actual>  - PASS only on an exact match.
#
# Deliberately exact, not substring: systemctl's "inactive" contains "active",
# so a substring test reports a dead service as healthy. That false PASS hid a
# stopped rover-ap-dhcp on the first run of this script.
check() {
    local label=$1 expect=$2 actual=$3
    if [ "$actual" = "$expect" ]; then
        pass "$label" "$actual"
    else
        fail "$label" "want '$expect', got '${actual:-<empty>}'"
    fi
}

# --------------------------------------------------------------- remote exec --

pi() { $SSH "$PI" "$*" 2>/dev/null; }
pi_root() { $SSH "$PI" "sudo $*" 2>/dev/null; }   # Pi has passwordless sudo

rover() { [ -n "$ROVER" ] && $SSH "$ROVER" "$*" 2>/dev/null; }
rover_root() {
    [ -n "$ROVER" ] || return 1
    [ -n "$ROVER_PW" ] || return 2
    printf '%s\n' "$ROVER_PW" | $SSH "$ROVER" "sudo -S -p '' $*" 2>/dev/null
}

# The rover drops ICMP, so reachability is probed on tcp/22 rather than by ping.
#
# Note $SSH runs BatchMode, which disables password auth outright: ROVER_PW only
# ever reaches `sudo -S` on the far side, never the login itself. So a rover that
# is up with no installed key fails here, and the fix is ssh-copy-id, not a
# password. Saying which of the two happened saves chasing the network for what
# is really a missing key or a wrong username.
locate_rover() {
    local target host reachable=""
    for target in "$ROVER_LIFELINE" "$ROVER_WIFI"; do
        host=${target#*@}
        timeout 4 bash -c "exec 3<>/dev/tcp/$host/22" 2>/dev/null || continue
        reachable=$target
        if $SSH "$target" true 2>/dev/null; then ROVER=$target; return 0; fi
    done
    [ -n "$reachable" ] && ROVER_HINT="sshd up at ${reachable#*@}, no key for $ROVER_USER - run: ssh-copy-id $reachable"
    return 1
}

# ===================================================================== GS PC ==

check_gs() {
    head_ "GS PC"

    # gs-mast and gs-mast-p2 both carry 10.44.0.10/24, so only one may autoconnect
    # or traffic for the Pi leaves by whichever cable won the metric. This is the
    # check that actually guards that, and it is the one to trust.
    local dupes
    dupes=$(ip -br addr | grep -c '10\.44\.0\.10/24')
    if [ "$dupes" -le 1 ]; then
        pass "no duplicate 10.44.0.10" "$dupes interface(s)"
    else
        fail "no duplicate 10.44.0.10" "$dupes interfaces share it - check gs-mast autoconnect"
    fi
    # gs-mast is bound to eno1 and is the profile that must win. gs-mast-p2 sits on
    # enp2s0, whose receive path is dead: it links and negotiates 1000Mb/s while
    # dropping everything inbound, so it must never come up on its own.
    check "gs-mast autoconnect on" "yes" "$(nmcli -t -f connection.autoconnect con show gs-mast 2>/dev/null | cut -d: -f2)"
    check "gs-mast-p2 autoconnect off" "no" "$(nmcli -t -f connection.autoconnect con show gs-mast-p2 2>/dev/null | cut -d: -f2)"

    # EEE on the GS transmit path costs 514 vs 887 Mbit/s to the Pi. Runtime
    # only - it does not survive a reboot.
    local eee
    eee=$(ethtool --show-eee "$GS_MAST_IF" 2>/dev/null | awk '/EEE status/{print $3}')
    [ "$eee" = "disabled" ] && pass "$GS_MAST_IF EEE disabled" "$eee" \
                            || warn "$GS_MAST_IF EEE disabled" "got '${eee:-?}' - not persistent, re-apply after reboot"

    [ -x "$HOME/.local/bin/sync-mast-clock.sh" ] \
        && pass "mast clock sync script" "present" \
        || fail "mast clock sync script" "missing ~/.local/bin/sync-mast-clock.sh"
    crontab -l 2>/dev/null | grep -q sync-mast-clock \
        && pass "clock sync cron installed" "every 15 min" \
        || fail "clock sync cron installed" "not in crontab"

    # Video must come from web_video_server, not rosbridge. The rosbridge path
    # base64s every frame into JSON and the browser queue grows without bound.
    ss -lnt 2>/dev/null | grep -q ':8080' \
        && pass "web_video_server on :8080" "listening" \
        || fail "web_video_server on :8080" "not listening - UI falls back to rosbridge"
}

# ==================================================================== MAST PI ==

check_pi() {
    head_ "Mast Pi ($PI)"
    if ! pi true; then fail "reachable" "no SSH to 10.44.0.1"; return; fi
    pass "reachable" "$(pi 'uptime -p')"

    # The vendor modalias for 0bda:8812 is broader than rtw88's, so with the
    # vendor driver blacklisted NOTHING auto-loads and the Alfa never appears.
    check "rtw88 driver loaded" "rtw_8812au" "$(pi 'lsmod | grep -oE "^rtw_8812au"')"
    pi 'test -f /etc/modules-load.d/rtw88-alfa.conf' \
        && pass "module forced at boot" "modules-load.d present" \
        || fail "module forced at boot" "missing - Alfa will not appear after reboot"
    check "vendor driver blacklisted" "blacklist 8812au" "$(pi 'grep -h "blacklist 8812au" /etc/modprobe.d/*.conf | head -1')"

    # mac80211 defaults power save ON for managed interfaces; it cost 117ms
    # average RTT with 400ms spikes. The udev rule matches on MAC so it re-fires
    # on every driver reload, not just boot.
    check "client power save off" "off" "$(pi "iw dev $PI_ALFA get power_save | awk '{print \$3}'")"
    pi 'test -f /etc/udev/rules.d/99-alfa-powersave-off.rules' \
        && pass "power save udev rule" "present" \
        || fail "power save udev rule" "missing - power save returns on next reload"

    # Onboard radio rescans all 42 channels every ~11s, including the AP's.
    local wlan0
    wlan0=$(pi 'ip -br link show wlan0 2>/dev/null | awk "{print \$2}"')
    [ "$wlan0" = "DOWN" ] && pass "onboard wlan0 down" "$wlan0" \
                          || warn "onboard wlan0 down" "got '${wlan0:-?}' - netplan brings it back, it scans 5180 MHz"

    local skew
    skew=$(( $(date -u +%s) - $(pi 'date -u +%s') ))
    [ "${skew#-}" -lt 120 ] && pass "clock in step with GS" "${skew}s" \
                            || fail "clock in step with GS" "${skew}s adrift"
}

# ====================================================================== ROVER ==

check_rover() {
    head_ "Rover"
    if ! locate_rover; then fail "reachable" "${ROVER_HINT:-no tcp/22 via lifeline or Wi-Fi}"; return; fi
    pass "reachable" "via ${ROVER#*@} - $(rover 'uptime -p')"

    check "rtw88 driver loaded" "rtw_8812au" "$(rover 'lsmod | grep -oE "^rtw_8812au"')"
    check "AP service active" "active" "$(rover 'systemctl is-active rover-ap.service')"
    check "AP service enabled" "enabled" "$(rover 'systemctl is-enabled rover-ap.service')"

    local info width
    info=$(rover "iw dev $ROVER_ALFA info")
    width=$(sed -n 's/.*width: \([0-9]*\) MHz.*/\1/p' <<<"$info")
    case "$width" in
        80|40) pass "AP channel width" "${width} MHz" ;;
        20)    warn "AP channel width" "20 MHz - half the expected throughput" ;;
        *)     fail "AP channel width" "got '${width:-?}'" ;;
    esac
    check "AP power save off" "off" "$(rover "iw dev $ROVER_ALFA get power_save | awk '{print \$3}'")"

    # Both radios must be hidden from NM in ONE key: conf.d overrides rather
    # than merges, so two files means the later one silently wins.
    local unmanaged
    unmanaged=$(rover 'grep -h unmanaged-devices /etc/NetworkManager/conf.d/*.conf 2>/dev/null | tr -d " "')
    if [[ "$unmanaged" == *"$ROVER_ALFA_MAC"* && "$unmanaged" == *"$INTEL_MAC"* ]]; then
        pass "both radios unmanaged by NM" "one merged key"
    else
        fail "both radios unmanaged by NM" "${unmanaged:-<none>}"
    fi

    # Intel WiFi off (Bluetooth deliberately stays up - separate rfkill entry).
    local intel_phy intel_block
    intel_phy=$(rover "iw dev wlP7p1s0 info 2>/dev/null | awk '/wiphy/{print \$2}'")
    if [ -n "$intel_phy" ]; then
        intel_block=$(rover "rfkill list | grep -A1 'phy${intel_phy}:' | awk '/Soft blocked/{print \$3}'")
        [ "$intel_block" = "yes" ] && pass "Intel WiFi rfkill-blocked" "phy$intel_phy soft blocked" \
                                   || warn "Intel WiFi rfkill-blocked" "phy$intel_phy blocked=${intel_block:-?} - it scans 5180 MHz"
    else
        skip "Intel WiFi rfkill-blocked" "wlP7p1s0 not present"
    fi
    check "Bluetooth still up" "UP RUNNING" "$(rover 'hciconfig hci0 2>/dev/null | grep -o "UP RUNNING"')"
}

# ======================================================== AP guest access ======
# Whether a device other than the mast Pi can actually use this AP. The Pi has a
# static 10.42.0.2 from netplan, so it works with no DHCP at all - which means a
# dead DHCP server is invisible until someone else tries to join.

check_ap_guests() {
    head_ "AP guest access (devices other than the mast Pi)"
    [ -n "$ROVER" ] || { skip "all checks" "rover unreachable"; return; }

    check "DHCP server running" "active" "$(rover 'systemctl is-active rover-ap-dhcp.service')"
    check "DHCP server enabled" "enabled" "$(rover 'systemctl is-enabled rover-ap-dhcp.service')"

    local conf
    conf=$(rover 'cat /etc/hostapd/rover-ap.conf 2>/dev/null')
    if [ -z "$conf" ]; then
        conf=$(rover_root 'cat /etc/hostapd/rover-ap.conf')
        [ -z "$conf" ] && { skip "hostapd config checks" "need ROVER_PW"; return; }
    fi

    grep -q '^ignore_broadcast_ssid=1' <<<"$conf" \
        && warn "SSID broadcast" "hidden - clients must add it manually" \
        || pass "SSID broadcast" "visible"

    local acl
    acl=$(grep '^macaddr_acl=' <<<"$conf" | cut -d= -f2)
    [ "${acl:-0}" = "0" ] && pass "MAC filtering" "open (macaddr_acl=${acl:-0})" \
                          || fail "MAC filtering" "macaddr_acl=$acl - only listed MACs may join"

    local maxsta
    maxsta=$(grep '^max_num_sta=' <<<"$conf" | cut -d= -f2)
    [ -z "$maxsta" ] && pass "client limit" "unset (hostapd default 2007)" \
                     || warn "client limit" "max_num_sta=$maxsta"

    grep -q '^ap_isolate=1' <<<"$conf" \
        && warn "client isolation" "on - guests cannot reach each other" \
        || pass "client isolation" "off"

    local stations
    stations=$(rover_root "iw dev $ROVER_ALFA station dump | grep -c '^Station'")
    [ -n "$stations" ] && pass "associated stations" "$stations" \
                       || skip "associated stations" "need ROVER_PW"
}

# ======================================================== link health =========

check_link() {
    head_ "Link health"
    pi true || { skip "all checks" "Pi unreachable"; return; }

    local sig chains
    sig=$(pi_root "iw dev $PI_ALFA station dump | grep -m1 '^\s*signal:'")
    chains=$(grep -oE '\[-?[0-9]+, *-?[0-9]+\]' <<<"$sig" | tr -d '[]' | tr ',' ' ')
    if [ -n "$chains" ]; then
        local a b spread
        a=$(awk '{print $1}' <<<"$chains"); b=$(awk '{print $2}' <<<"$chains")
        spread=$(( a > b ? a - b : b - a ))
        # A bad antenna on one chain caps 2-stream MIMO; a 10 dB gap halved
        # downlink until the connector was reseated.
        [ "$spread" -le 5 ] && pass "antenna chains balanced" "${a}/${b} dBm (${spread} dB)" \
                            || fail "antenna chains balanced" "${a}/${b} dBm - ${spread} dB gap, reseat the antenna"
        # Anything hotter than about -15 dBm is receiver compression, not a good link.
        [ "$a" -lt -15 ] && pass "signal not overdriven" "${a} dBm" \
                         || warn "signal not overdriven" "${a} dBm - radios too close, readings unreliable"
    else
        skip "antenna chains balanced" "no station dump"
    fi

    local rtt
    rtt=$(pi "ping -c 10 -i 0.2 -W 2 10.42.0.1 2>/dev/null | tail -1 | cut -d= -f2")
    [ -n "$rtt" ] && pass "RTT to rover" "$rtt ms" || fail "RTT to rover" "no reply"
}

# ============================================================================ =

printf '\033[1mIndomitus link verification\033[0m  %s\n' "$(date -Is)"
check_gs
check_pi
check_rover
check_ap_guests
check_link

printf '\n'
if [ "$fails" -eq 0 ]; then
    printf '\033[32mAll checks passed.\033[0m WARNs are non-persistent settings - see mast/STARTUP.md\n'
else
    printf '\033[31m%d check(s) failed.\033[0m See mast/STARTUP.md for the fix for each.\n' "$fails"
fi
exit "$fails"

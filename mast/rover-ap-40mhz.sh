#!/bin/bash
# Run the rover AP at 40 MHz via hostapd, as a TRIAL, with an armed rollback.
#
#     ROVER_PW=... ./mast/rover-ap-40mhz.sh --apply      # arm, swap, verify
#     ROVER_PW=... ./mast/rover-ap-40mhz.sh --keep       # cancel the rollback
#     ROVER_PW=... ./mast/rover-ap-40mhz.sh --rollback   # revert now (reboots)
#     ROVER_PW=... ./mast/rover-ap-40mhz.sh --status
#     ./mast/rover-ap-40mhz.sh --dry
#
# WHY THIS IS NOT A SETTING. The AP is a NetworkManager `Hotspot` profile and NM
# 1.36 has no channel-width property for AP mode (the full 802-11-wireless
# property list has ssid/band/channel/rate/tx-power/powersave/ap-isolation and
# nothing about width). 20 MHz is therefore a hard ceiling for that mechanism.
# Going wider means hostapd, which also means re-creating by hand the two things
# `ipv4.method shared` was providing for free: a DHCP server and the return
# route to the ground station.
#
# THIS IS DELIBERATELY NOT PERSISTENT. Nothing is `systemctl enable`d and no
# file NM reads is modified. The stock 20 MHz Hotspot has autoconnect=yes, so
# **a reboot restores it** — which is exactly what makes the rollback safe: the
# armed rollback simply reboots. Consequence: --keep means "do not revert right
# now", NOT "this survives". After any reboot you are back at 20 MHz on NM.
# Making it permanent is a separate, deliberate change once the numbers justify
# it — see WHAT PERMANENT WOULD MEAN at the bottom of this file.
#
# WHY AN ARMED ROLLBACK. The Alfa is the only way into this rover: the wired
# lifeline on 10.45.0.51 does not answer on this Nano (verified 2026-08-31). If
# hostapd will not start, or starts but no client can associate, nothing on the
# network can fix it. The rollback is armed BEFORE the AP is touched.
#
# EXPECT A MODEST GAIN. 40 MHz roughly doubles the PHY ceiling (144 -> 400
# Mbit/s), but the measured 2026-08-31 link runs ~80 Mbit/s at 20 MHz and the
# USB 2.0 bus does not bind until ~280 Mbit/s, so the realistic target is
# ~150 Mbit/s. If you see much less, the width took but something else is
# capping — measure before concluding, and remember 40 MHz costs link margin at
# range, which is the whole reason STARTUP.md ran 40 rather than 80.

set -u

NANO_HOST=10.42.0.1
NANO_SSH=indomitus-rover@$NANO_HOST
NM_CONN=Hotspot
IFACE=wlx00c0caba86c1
AP_ADDR=10.42.0.1/24
GS_NET=10.44.0.0/24
GS_VIA=10.42.0.2
DHCP_RANGE=10.42.0.50,10.42.0.150,60m
REMOTE_CONF=/tmp/rover-ap-vht40.conf
KEEP=/run/rover-ap-40mhz.keep
STARTED=/run/rover-ap-40mhz.started
ROLLBACK_SECS=${ROLLBACK_SECS:-300}
SSH="ssh -o BatchMode=yes -o ConnectTimeout=8"
REPO=$(cd "$(dirname "$0")/.." && pwd)
ROVER_PW=${ROVER_PW:-}
MODE=""
DRY=0
FORCE=0

for a in "$@"; do
    case "$a" in
        --apply)    MODE=apply;;
        --keep)     MODE=keep;;
        --rollback) MODE=rollback;;
        --status)   MODE=status;;
        --dry)      DRY=1; [ -z "$MODE" ] && MODE=apply;;
        --force)    FORCE=1;;
        -h|--help)  MODE=help;;
        *) echo "unknown argument: $a" >&2; exit 2;;
    esac
done

say()  { printf '  %s\n' "$*"; }
head_(){ printf '\n\033[1m%s\033[0m\n' "$1"; }
did()  { printf '  \033[32m%s\033[0m %s\n' "changed" "$1"; }
ok()   { printf '  \033[2mok\033[0m %s\n' "$1"; }
warn() { printf '  \033[33mwarn\033[0m %s\n' "$1"; }
die()  { printf '  \033[31merror\033[0m %s\n' "$1" >&2; exit 1; }

if [ "$MODE" = help ] || [ -z "$MODE" ]; then
    sed -n '2,45p' "$0" | sed 's/^# \?//'
    exit 0
fi

if [ "$DRY" != 1 ] && [ -z "$ROVER_PW" ]; then
    die "ROVER_PW is unset — the rover needs a sudo password (same as restore-link.sh)"
fi
SUDO="echo '${ROVER_PW:-1}' | sudo -S"

nexec() {
    if [ "$DRY" = 1 ]; then printf '  \033[2mwould run\033[0m %s\n' "$1"; return 0; fi
    $SSH "$NANO_SSH" "$1"
}

reachable() { ping -c1 -W2 "$NANO_HOST" >/dev/null 2>&1 && $SSH "$NANO_SSH" true 2>/dev/null; }

# ------------------------------------------------------------------ status --

show_status() {
    head_ "AP on $NANO_SSH"
    $SSH "$NANO_SSH" "
        echo \"hostapd running:  \$(pgrep -x hostapd >/dev/null && echo YES || echo no)\"
        echo \"NM manages iface: \$(nmcli -t -f GENERAL.STATE dev show $IFACE 2>/dev/null | head -1)\"
        echo \"rollback armed:   \$(pgrep -f '[r]over-ap-40mhz-rollback' >/dev/null && echo YES || echo no)\"
        echo \"keep flag:        \$([ -e $KEEP ] && echo set || echo unset)\"
        iw dev $IFACE info 2>/dev/null | grep -oE 'channel [0-9]+ \(.*\), width: [0-9]+ MHz' | sed 's/^/AP: /'
        echo \"stations:         \$(iw dev $IFACE station dump 2>/dev/null | grep -c '^Station')\"
        echo \"addr:             \$(ip -br addr show $IFACE 2>/dev/null | awk '{print \$3}')\"
        echo \"route to GS:      \$(ip route | grep '$GS_NET' || echo MISSING)\"
        " 2>/dev/null | sed 's/^/  /'
}

[ "$MODE" = status ] && { show_status; exit 0; }

# -------------------------------------------------------------------- keep --

if [ "$MODE" = keep ]; then
    head_ "Cancelling the rollback"
    reachable || die "cannot reach $NANO_SSH — do NOT cancel a rollback you cannot verify"
    nexec "$SUDO touch $KEEP; $SUDO pkill -f '[r]over-ap-40mhz-rollback' 2>/dev/null; true"
    did "rollback cancelled"
    warn "this is still NOT persistent — the next reboot returns the AP to NM at 20 MHz"
    exit 0
fi

# ---------------------------------------------------------------- rollback --

if [ "$MODE" = rollback ]; then
    head_ "Reverting to the NM Hotspot at 20 MHz"
    say "this reboots the rover — NM Hotspot has autoconnect=yes and comes back on its own"
    reachable || die "cannot reach $NANO_SSH (if the link is down the armed rollback should already have fired; otherwise connect a screen to the rover)"
    nexec "$SUDO pkill -f '[r]over-ap-40mhz-rollback' 2>/dev/null
           $SUDO setsid nohup sh -c 'sleep 2; reboot' >/dev/null 2>&1 &
           sleep 1; true"
    did "rebooting — back in ~90s"
    exit 0
fi

# ------------------------------------------------------------------- apply --

head_ "Pre-flight"
if [ "$DRY" != 1 ]; then
    reachable || die "cannot reach $NANO_SSH"
    ok "$NANO_SSH reachable"

    $SSH "$NANO_SSH" "test -x /usr/sbin/hostapd && test -x /usr/sbin/dnsmasq" 2>/dev/null \
        || die "hostapd and/or dnsmasq missing on the rover"
    ok "hostapd and dnsmasq present"

    # Verify sudo BEFORE arming anything. --keep needs sudo too, so arming a
    # rollback with a bad ROVER_PW would be an uncancellable reboot.
    SUDO_OK=$($SSH "$NANO_SSH" "$SUDO true 2>/dev/null && echo yes || echo no" 2>/dev/null)
    [ "$SUDO_OK" = yes ] || die "sudo failed on the rover — check ROVER_PW. Nothing armed, nothing changed."
    ok "sudo works (verified before arming)"

    # The rover is a live machine. Swapping the AP cuts every station for ~30s,
    # which drops teleop mid-drive.
    STATIONS=$($SSH "$NANO_SSH" "iw dev $IFACE station dump | grep -c '^Station'" 2>/dev/null)
    OTHERS=$(( ${STATIONS:-1} - 1 ))
    BRINGUP=$($SSH "$NANO_SSH" "pgrep -f 'rover.launch.py' >/dev/null && echo yes || echo no" 2>/dev/null)

    BLOCK=0
    [ "$OTHERS" -gt 0 ] && { warn "$OTHERS station(s) besides the mast Pi are on the AP — they will be kicked"; BLOCK=1; }
    [ "$BRINGUP" = yes ] && { warn "rover.launch.py is running — motor control is live and teleop will cut"; BLOCK=1; }
    if [ "$BLOCK" = 1 ] && [ "$FORCE" != 1 ]; then
        say ""
        die "refusing on a live rover. Stop the bringup and clear the AP, or pass --force if you know it is safe."
    fi
    [ "$BLOCK" = 1 ] && warn "proceeding anyway (--force)"
    [ "$BLOCK" = 0 ] && ok "rover is idle — no other stations, no bringup"
fi
say "rollback reboots in ${ROLLBACK_SECS}s unless --keep runs first"

head_ "Arming the rollback FIRST"
# setsid + nohup so it outlives this SSH session, which the AP swap kills.
# The rollback is a plain reboot: nothing here is enabled at boot, and the NM
# Hotspot profile has autoconnect=yes, so a reboot IS the revert. $KEEP is in
# /run so a reboot clears it too.
nexec "$SUDO rm -f $KEEP
       cat > /tmp/rover-ap-40mhz-rollback <<'ROLLBACK'
#!/bin/sh
sleep \$1
[ -e $KEEP ] && exit 0
logger -t rover-ap-40mhz 'no --keep within timeout, rebooting to restore the NM Hotspot'
reboot
ROLLBACK
       $SUDO cp /tmp/rover-ap-40mhz-rollback /usr/local/sbin/rover-ap-40mhz-rollback
       $SUDO chmod +x /usr/local/sbin/rover-ap-40mhz-rollback
       $SUDO setsid nohup /usr/local/sbin/rover-ap-40mhz-rollback $ROLLBACK_SECS >/dev/null 2>&1 &
       sleep 1; true"
[ "$DRY" = 1 ] || did "rollback armed (${ROLLBACK_SECS}s, reboots)"

head_ "Staging the hostapd config"
if [ "$DRY" = 1 ]; then
    say "would copy $REPO/mast/rover-ap-hostapd-vht40.conf -> $NANO_SSH:$REMOTE_CONF"
else
    scp -q -o BatchMode=yes "$REPO/mast/rover-ap-hostapd-vht40.conf" "$NANO_SSH:$REMOTE_CONF" \
        || die "scp failed"
    did "staged at $REMOTE_CONF"
fi

head_ "Swapping the AP — THE LINK WILL DROP HERE"
say "NM releases $IFACE; hostapd takes it at 40 MHz; dnsmasq serves $DHCP_RANGE"
# Detached, and written to disk first rather than passed inline.
#
# THE BUG THIS AVOIDS (hit for real 2026-08-31). The obvious form,
#     echo "$PW" | sudo -S setsid nohup sh -c '...' >/dev/null 2>&1 < /dev/null &
# is silently broken: `< /dev/null` replaces stdin, and stdin IS the pipe
# carrying the password into `sudo -S`. sudo gets nothing, fails, and 2>/dev/null
# swallows the error — the step becomes a no-op that reports success. The arm
# step survived the same pattern only because an earlier sudo in the SAME SSH
# session had primed sudo's credential cache; this step is the first sudo in a
# fresh session, so it had nothing to fall back on.
#
# Fix, belt and braces: no stdin redirect on a sudo being fed a password; the
# payload lives in a file so quoting is simple; and the payload touches $STARTED
# as its first act so we can PROVE it ran instead of inferring it.
#
# Order matters. NM must release the interface before hostapd can claim it, and
# the static route must be re-added because it came from the NM profile.
nexec "cat > /tmp/rover-ap-40mhz-apply <<'APPLY'
#!/bin/sh
touch $STARTED
nmcli dev set $IFACE managed no
nmcli con down $NM_CONN >/dev/null 2>&1
sleep 3
ip addr flush dev $IFACE
ip addr add $AP_ADDR dev $IFACE
ip link set $IFACE up
hostapd -B $REMOTE_CONF
sleep 3
dnsmasq --interface=$IFACE --bind-interfaces --except-interface=lo \
        --listen-address=${AP_ADDR%%/*} --dhcp-range=$DHCP_RANGE \
        --port=0 --no-hosts --dhcp-authoritative \
        --pid-file=/run/rover-ap-40mhz-dnsmasq.pid
ip route replace $GS_NET via $GS_VIA dev $IFACE
logger -t rover-ap-40mhz 'AP swapped to hostapd 40 MHz'
APPLY
       $SUDO cp /tmp/rover-ap-40mhz-apply /usr/local/sbin/rover-ap-40mhz-apply
       $SUDO chmod +x /usr/local/sbin/rover-ap-40mhz-apply
       $SUDO rm -f $STARTED
       $SUDO setsid nohup /usr/local/sbin/rover-ap-40mhz-apply >/dev/null 2>&1 &
       sleep 1; true"

if [ "$DRY" = 1 ]; then say "would now wait for the link"; exit 0; fi

head_ "Waiting for the link"
BACK=0
for i in $(seq 1 30); do
    sleep 5
    if reachable; then ok "back after ~$((i * 5))s"; BACK=1; break; fi
done
[ "$BACK" = 1 ] || die "link did not come back — do NOTHING and let the rollback reboot it at ${ROLLBACK_SECS}s"

# Did the payload actually execute? Without this, a sudo that failed silently
# looks identical to a swap that ran and changed nothing.
RAN=$($SSH "$NANO_SSH" "[ -e $STARTED ] && echo yes || echo no" 2>/dev/null)
[ "$RAN" = yes ] || warn "the swap script never ran (no $STARTED) — sudo probably failed; check ROVER_PW"

show_status

WIDTH=$($SSH "$NANO_SSH" "iw dev $IFACE info 2>/dev/null | grep -oE 'width: [0-9]+ MHz'" 2>/dev/null)
printf '\n'
case "$WIDTH" in
    *"40 MHz"*) did "AP is at 40 MHz";;
    *"20 MHz"*) if [ "$RAN" = yes ]; then
                    warn "AP came up but is still 20 MHz — hostapd fell back; check 'journalctl -t hostapd' on the rover"
                else
                    warn "still 20 MHz because the swap never ran — this is NOT an hostapd fallback"
                fi;;
    *)          warn "could not read the AP width ($WIDTH)";;
esac

say ""
say "NOW MEASURE. A link that is up but unstable is a failure here, not a success."
say "Baselines from 2026-08-31, both ends morrownr, NM Hotspot at 20 MHz:"
say "    GS -> rover  79-89 Mbit/s | rover -> GS  67-81 Mbit/s"
say "    loaded RTT   40-50 ms avg, 57-92 ms max, 0% loss"
say "Target at 40 MHz: roughly 150 Mbit/s. Check the loaded RTT too — 40 MHz"
say "on the vendor driver is exactly where STARTUP.md recorded 190-230 ms spikes."
say ""
say "    ssh $NANO_SSH 'nohup iperf3 -s -D'"
say "    iperf3 -c $NANO_HOST -t 10 -f m"
say "    iperf3 -c $NANO_HOST -t 10 -f m -R"
say "    (iperf3 -c $NANO_HOST -t 12 >/dev/null &); sleep 1; ping -c 20 -i 0.5 $NANO_HOST | tail -2"
say ""
say "Happy    ->  ROVER_PW=... $0 --keep      (within ${ROLLBACK_SECS}s)"
say "Not sure ->  do nothing; it reboots itself back to 20 MHz"
say "Revert   ->  ROVER_PW=... $0 --rollback"
say ""
say "WHAT PERMANENT WOULD MEAN, once the numbers justify it: a hostapd unit and a"
say "DHCP unit (the rover-ap.service / rover-ap-dhcp.service pair README.md"
say "describes but which no longer exist on the rover), the config installed to"
say "/etc/hostapd/rover-ap.conf, $IFACE marked unmanaged in NM, and the static"
say "route moved into an ExecStartPre. That is a change to make deliberately,"
say "with the link verified, not as a side effect of a trial."

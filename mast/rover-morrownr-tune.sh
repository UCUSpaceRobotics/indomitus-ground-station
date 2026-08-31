#!/bin/bash
# Tune the rover Alfa's morrownr 8812au options for AP mode, with an armed
# rollback.
#
#     ROVER_PW=... ./mast/rover-morrownr-tune.sh --apply      # arm, install, reload, verify
#     ROVER_PW=... ./mast/rover-morrownr-tune.sh --keep       # cancel the rollback (link is good)
#     ROVER_PW=... ./mast/rover-morrownr-tune.sh --rollback   # revert now
#     ROVER_PW=... ./mast/rover-morrownr-tune.sh --status
#     ./mast/rover-morrownr-tune.sh --dry
#
# WHAT THIS CHANGES. The rover runs morrownr's vendor 8812au with the defaults
# from its own /etc/modprobe.d/8812au.conf, which are managed-mode defaults on a
# host that is an AP. Three of them are wrong for that role:
#
#   rtw_switch_usb_mode  0 -> 1   adapter sits in a USB 3.0 port at 480M
#   rtw_vht_enable       1 -> 2   morrownr: "use only for 5 GHz AP mode"
#   rtw_power_mgnt       2 -> 0   maxPS; morrownr: "not recommended for AP mode"
#
# WHY A DROP-IN AND NOT AN EDIT. Options land in /etc/modprobe.d/99-alfa-tune.conf
# rather than editing 8812au.conf. modprobe concatenates `options` from every
# *.conf in alphabetical order and the last value of a duplicated parameter wins,
# so `99-` beats `8812au.conf` without touching the vendor file. Reverting is
# `rm`, which cannot corrupt a file we did not write.
#
# WHY AN ARMED ROLLBACK. The Alfa is the only way into this rover. The wired
# lifeline on 10.45.0.51 does NOT answer on this Nano (verified 2026-08-31), so
# a bad option that stops the AP coming back means walking to the rover. The
# rollback is armed BEFORE anything is installed and fires unless --keep runs.
#
# WHAT THIS DOES NOT FIX. The AP is a NetworkManager `Hotspot` profile, and NM
# 1.36 exposes no channel-width property for AP mode, so the beacon is 20 MHz.
# That is the throughput cap, not the driver. rtw_vht_enable=2 lets the driver
# advertise VHT and wpa_supplicant's AP may widen on its own, but if it stays at
# 20 MHz the fix is hostapd, not this script. Measure before concluding.

set -u

NANO_HOST=10.42.0.1
NANO_SSH=indomitus-rover@$NANO_HOST
NM_CONN=Hotspot
MODULE=8812au
CONF=/etc/modprobe.d/99-alfa-tune.conf
KEEP=/run/rover-alfa-tune.keep
STARTED=/run/rover-alfa-tune.started
ROLLBACK_SECS=${ROLLBACK_SECS:-300}
SSH="ssh -o BatchMode=yes -o ConnectTimeout=8"
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
    sed -n '2,40p' "$0" | sed 's/^# \?//'
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
    head_ "Alfa on $NANO_SSH"
    $SSH "$NANO_SSH" "
        echo \"drop-in installed: \$([ -e $CONF ] && echo yes || echo no)\"
        echo \"rollback armed:    \$(pgrep -f '[r]over-alfa-tune-rollback' >/dev/null && echo YES || echo no)\"
        echo \"keep flag:         \$([ -e $KEEP ] && echo set || echo unset)\"
        echo \"driver:            \$(basename \$(readlink -f /sys/class/net/wlx00c0caba86c1/device/driver 2>/dev/null) 2>/dev/null)\"
        for p in rtw_switch_usb_mode rtw_vht_enable rtw_power_mgnt; do
            echo \"\$p = \$(cat /sys/module/$MODULE/parameters/\$p 2>/dev/null)\"
        done
        for d in /sys/bus/usb/devices/*/; do
            case \"\$(cat \$d/idVendor 2>/dev/null):\$(cat \$d/idProduct 2>/dev/null)\" in
                0bda:8812) echo \"Alfa \$(basename \$d) at \$(cat \$d/speed)M\";;
            esac
        done
        echo \"AP width: \$(iw dev wlx00c0caba86c1 info 2>/dev/null | grep -o 'width: [0-9]* MHz')\"
        echo \"stations: \$(iw dev wlx00c0caba86c1 station dump 2>/dev/null | grep -c '^Station')\"
        " 2>/dev/null | sed 's/^/  /'
}

[ "$MODE" = status ] && { show_status; exit 0; }

# -------------------------------------------------------------------- keep --

if [ "$MODE" = keep ]; then
    head_ "Cancelling the rollback"
    reachable || die "cannot reach $NANO_SSH — do NOT cancel a rollback you cannot verify"
    nexec "$SUDO touch $KEEP; $SUDO pkill -f '[r]over-alfa-tune-rollback' 2>/dev/null; true"
    did "rollback cancelled; $CONF stays"
    say "to make it survive a reboot nothing more is needed — $CONF is on disk"
    show_status
    exit 0
fi

# ---------------------------------------------------------------- rollback --

if [ "$MODE" = rollback ]; then
    head_ "Reverting to the stock 8812au options"
    reachable || die "cannot reach $NANO_SSH (if the link is down the armed rollback should already have fired; otherwise connect a screen to the rover)"
    nexec "$SUDO rm -f $CONF; $SUDO pkill -f '[r]over-alfa-tune-rollback' 2>/dev/null
           $SUDO setsid nohup sh -c 'nmcli con down $NM_CONN >/dev/null 2>&1
           modprobe -r $MODULE; sleep 3; modprobe $MODULE; sleep 5
           nmcli con up $NM_CONN >/dev/null 2>&1' >/dev/null 2>&1 &
           sleep 1; true"
    did "reverting — link drops now, back in ~30s"
    exit 0
fi

# ------------------------------------------------------------------- apply --

head_ "Pre-flight"
if [ "$DRY" != 1 ]; then
    reachable || die "cannot reach $NANO_SSH"
    ok "$NANO_SSH reachable"

    DRV=$($SSH "$NANO_SSH" "basename \$(readlink -f /sys/class/net/wlx00c0caba86c1/device/driver)" 2>/dev/null)
    [ "$DRV" = "rtl8812au" ] || die "driver is '$DRV', expected the morrownr vendor 'rtl8812au' — wrong driver for these options"
    ok "morrownr rtl8812au is loaded"

    $SSH "$NANO_SSH" "test -e /sys/module/$MODULE/parameters/rtw_vht_enable" 2>/dev/null \
        || die "$MODULE has no rtw_vht_enable parameter"
    ok "$MODULE accepts the options"

    # Verify sudo BEFORE arming — --keep needs sudo too.
    SUDO_OK=$($SSH "$NANO_SSH" "$SUDO true 2>/dev/null && echo yes || echo no" 2>/dev/null)
    [ "$SUDO_OK" = yes ] || die "sudo failed on the rover — check ROVER_PW. Nothing armed, nothing changed."
    ok "sudo works (verified before arming)"

    # The rover is a live machine. Reloading the module cuts the AP for ~30s,
    # which drops teleop mid-drive and kicks every other station off.
    STATIONS=$($SSH "$NANO_SSH" "iw dev wlx00c0caba86c1 station dump | grep -c '^Station'" 2>/dev/null)
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
say "rollback fires in ${ROLLBACK_SECS}s unless --keep runs first"

head_ "Arming the rollback FIRST"
# setsid + nohup so it outlives this SSH session, which the module reload kills.
# $KEEP lives in /run, so a reboot clears it and a rebooted-but-broken rover
# still gets reverted on the next arm.
nexec "$SUDO rm -f $KEEP
       cat > /tmp/rover-alfa-tune-rollback <<'ROLLBACK'
#!/bin/sh
sleep \$1
[ -e $KEEP ] && exit 0
rm -f $CONF
nmcli con down $NM_CONN >/dev/null 2>&1
modprobe -r $MODULE; sleep 3; modprobe $MODULE; sleep 5
nmcli con up $NM_CONN >/dev/null 2>&1
logger -t rover-alfa-tune 'rolled back to stock options (no --keep within timeout)'
ROLLBACK
       $SUDO cp /tmp/rover-alfa-tune-rollback /usr/local/sbin/rover-alfa-tune-rollback
       $SUDO chmod +x /usr/local/sbin/rover-alfa-tune-rollback
       $SUDO setsid nohup /usr/local/sbin/rover-alfa-tune-rollback $ROLLBACK_SECS >/dev/null 2>&1 &
       sleep 1; true"
[ "$DRY" = 1 ] || did "rollback armed (${ROLLBACK_SECS}s)"

head_ "Installing $CONF"
nexec "cat > /tmp/99-alfa-tune.conf <<'EOF'
# Managed by mast/rover-morrownr-tune.sh — remove this file to revert.
#
# Sorts after 8812au.conf, so these override morrownr's managed-mode defaults
# there. AP-mode values, per morrownr's own notes in that file:
#   rtw_switch_usb_mode=1  USB 3.0 (adapter is in a USB 3.0 port)
#   rtw_vht_enable=2       force auto enable — 5 GHz AP mode only
#   rtw_power_mgnt=0       power saving off — maxPS is wrong for an AP
options 8812au rtw_switch_usb_mode=1 rtw_vht_enable=2 rtw_power_mgnt=0 rtw_led_ctrl=1
EOF
       $SUDO cp /tmp/99-alfa-tune.conf $CONF
       true"
[ "$DRY" = 1 ] || did "installed"

head_ "Reloading $MODULE — THE LINK WILL DROP HERE"
say "the adapter re-enumerates into USB 3.0; NM re-activates '$NM_CONN'"
# Detached, payload written to disk. See the long comment in rover-ap-40mhz.sh:
# `sudo -S ... < /dev/null` silently eats its own password pipe and no-ops. No
# stdin redirect here, and $STARTED proves the payload ran.
nexec "cat > /tmp/rover-alfa-tune-apply <<'APPLY'
#!/bin/sh
touch $STARTED
nmcli con down $NM_CONN >/dev/null 2>&1
modprobe -r $MODULE; sleep 3; modprobe $MODULE; sleep 5
nmcli con up $NM_CONN >/dev/null 2>&1
logger -t rover-alfa-tune 'module reloaded with AP-mode options'
APPLY
       $SUDO cp /tmp/rover-alfa-tune-apply /usr/local/sbin/rover-alfa-tune-apply
       $SUDO chmod +x /usr/local/sbin/rover-alfa-tune-apply
       $SUDO rm -f $STARTED
       $SUDO setsid nohup /usr/local/sbin/rover-alfa-tune-apply >/dev/null 2>&1 &
       sleep 1; true"

if [ "$DRY" = 1 ]; then say "would now wait for the link"; exit 0; fi

head_ "Waiting for the link"
BACK=0
for i in $(seq 1 30); do
    sleep 5
    if reachable; then ok "back after ~$((i * 5))s"; BACK=1; break; fi
done
[ "$BACK" = 1 ] || die "link did not come back — do nothing and let the rollback fire at ${ROLLBACK_SECS}s"

RAN=$($SSH "$NANO_SSH" "[ -e $STARTED ] && echo yes || echo no" 2>/dev/null)
[ "$RAN" = yes ] || warn "the reload script never ran (no $STARTED) — sudo probably failed; check ROVER_PW"

show_status

SPEED=$($SSH "$NANO_SSH" "for d in /sys/bus/usb/devices/*/; do case \"\$(cat \$d/idVendor 2>/dev/null):\$(cat \$d/idProduct 2>/dev/null)\" in 0bda:8812) cat \$d/speed;; esac; done" 2>/dev/null | head -1)
printf '\n'
[ "$SPEED" = "5000" ] && did "Alfa is at 5000M (USB 3.0)" || warn "Alfa is at ${SPEED}M — rtw_switch_usb_mode did not take"

say ""
say "NOW MEASURE. A link that is up but unstable is a failure here, not a success."
say "Baselines from 2026-08-31, both ends on morrownr, stock options:"
say "    GS -> rover  89 Mbit/s | rover -> GS  76 Mbit/s"
say "    loaded RTT   37-54 ms avg, 69-123 ms max, 0% loss"
say ""
say "    ssh $NANO_SSH 'nohup iperf3 -s -D'"
say "    iperf3 -c $NANO_HOST -t 10 -f m       # up"
say "    iperf3 -c $NANO_HOST -t 10 -f m -R    # down"
say "    (iperf3 -c $NANO_HOST -t 12 >/dev/null &); sleep 1; ping -c 20 -i 0.5 $NANO_HOST | tail -2"
say ""
say "Happy    ->  ROVER_PW=... $0 --keep      (within ${ROLLBACK_SECS}s)"
say "Not sure ->  do nothing; it reverts itself"
say "Revert   ->  ROVER_PW=... $0 --rollback"

#!/bin/bash
# Switch the rover Nano's Alfa into USB 3.0 mode, with an armed rollback.
#
#     ./mast/alfa-usb3.sh --apply      # arm rollback, install, reload, verify
#     ./mast/alfa-usb3.sh --keep       # cancel the rollback (link is good)
#     ./mast/alfa-usb3.sh --rollback   # revert now
#     ./mast/alfa-usb3.sh --status
#     ./mast/alfa-usb3.sh --dry
#
# WHY AN ARMED ROLLBACK. The Alfa is the only way into this rover: unlike the
# old Orin, this Nano has NO wired lifeline configured (10.45.0.51 does not
# answer). Switching USB mode makes the adapter re-enumerate, so the link
# necessarily drops for a few seconds — and if it does not come back, nothing
# short of physical access recovers it. So the revert is scheduled on the Nano
# BEFORE anything changes, detached from this SSH session, and only cancelled
# once the GS has actually reconnected. This mirrors rover-ap-apply.sh.
#
# WHAT COMES BACK ON ITS OWN. The AP here is NetworkManager, not hostapd: the
# connection is `Hotspot` (mode=ap, ssid ERC_UCUSpaceRobotics_A, autoconnect=yes) and
# the 10.44.0.0/24 return route is a static route ON that connection, so both
# return when NM re-activates it. That is why this reloads the module rather
# than rebooting — the recovery path is shorter and observable.
#
# WHAT THIS DOES NOT FIX. SuperSpeed on this board is what wrecks the cameras
# (see the USB note in mast/README.md). Putting the RADIO on USB 3.0 is a
# different controller path from the camera hubs, but it is the same silicon
# family, so treat a link that comes up unstable — not just absent — as a
# failure and roll back.
set -uo pipefail

NANO_SSH=${NANO_SSH:-indomitus-rover@10.42.0.1}
NANO_HOST=${NANO_HOST:-${NANO_SSH#*@}}
# How long the Nano waits before reverting itself. Long enough for the adapter
# to re-enumerate, NM to re-activate the AP and the GS to reconnect and run
# --keep; short enough that a failed attempt is not a long walk.
ROLLBACK_SECS=${ROLLBACK_SECS:-240}
CONF=/etc/modprobe.d/8812au-usb3.conf
KEEP=/run/alfa-usb3.keep
MODULE=${MODULE:-88XXau}
NM_CONN=${NM_CONN:-Hotspot}

DRY=0
MODE=""

usage() {
    cat <<EOF
Switch the rover Nano's Alfa into USB 3.0 mode, with an armed rollback.
Read the header of this file before running it: this changes the only link
into the rover, and this Nano has no wired lifeline.

  ./mast/alfa-usb3.sh --apply           arm rollback, install, reload, verify
  ./mast/alfa-usb3.sh --apply --dry     print the plan, change nothing
  ./mast/alfa-usb3.sh --keep            cancel the rollback (link verified good)
  ./mast/alfa-usb3.sh --rollback        revert now
  ./mast/alfa-usb3.sh --status

Options
  --secs N        seconds before the armed rollback fires (default: $ROLLBACK_SECS)
  --dry           with --apply, print what would happen

Environment: NANO_SSH (default $NANO_SSH), ROVER_PW (the Nano's sudo password),
MODULE (default $MODULE), NM_CONN (default $NM_CONN), ROLLBACK_SECS.
EOF
    exit 0
}

while [ $# -gt 0 ]; do
    case "$1" in
        --apply)    MODE=apply; shift ;;
        --keep)     MODE=keep; shift ;;
        --rollback) MODE=rollback; shift ;;
        --status)   MODE=status; shift ;;
        --dry)      DRY=1; shift ;;
        --secs)     ROLLBACK_SECS=$2; shift 2 ;;
        -h|--help)  usage ;;
        *) echo "unknown option: $1 (try --help)"; exit 1 ;;
    esac
done
[ -n "$MODE" ] || usage

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
say()   { printf '  %s\n' "$*"; }
head_() { printf '\n\033[1m%s\033[0m\n' "$1"; }
did()   { printf '  \033[32m%s\033[0m %s\n' "changed" "$1"; }
ok()    { printf '  \033[2mok\033[0m %s\n' "$1"; }
warn()  { printf '  \033[33mWARN\033[0m  %s\n' "$1"; }
die()   { printf '  \033[31mFAIL\033[0m  %s\n' "$1"; exit 1; }

SSH="ssh -n -o BatchMode=yes -o StrictHostKeyChecking=accept-new -o ConnectTimeout=8"
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
        echo \"conf installed: \$([ -e $CONF ] && echo yes || echo no)\"
        echo \"rollback armed: \$(pgrep -f '[a]lfa-usb3-rollback' >/dev/null && echo YES || echo no)\"
        echo \"keep flag:      \$([ -e $KEEP ] && echo set || echo unset)\"
        echo \"rtw_switch_usb_mode = \$(cat /sys/module/$MODULE/parameters/rtw_switch_usb_mode 2>/dev/null)\"
        echo \"rtw_power_mgnt      = \$(cat /sys/module/$MODULE/parameters/rtw_power_mgnt 2>/dev/null)\"
        for d in /sys/bus/usb/devices/*/; do
            case \"\$(cat \$d/idVendor 2>/dev/null):\$(cat \$d/idProduct 2>/dev/null)\" in
                0bda:8812) echo \"Alfa \$(basename \$d) at \$(cat \$d/speed)M\";;
            esac
        done" 2>/dev/null | sed 's/^/  /'
}

[ "$MODE" = status ] && { show_status; exit 0; }

# -------------------------------------------------------------------- keep --

if [ "$MODE" = keep ]; then
    head_ "Cancelling the rollback"
    reachable || die "cannot reach $NANO_SSH — do NOT cancel a rollback you cannot verify"
    nexec "$SUDO touch $KEEP; pkill -f '[a]lfa-usb3-rollback' 2>/dev/null; true"
    did "rollback cancelled; $CONF stays"
    show_status
    exit 0
fi

# ---------------------------------------------------------------- rollback --

if [ "$MODE" = rollback ]; then
    head_ "Reverting to USB 2.0"
    reachable || die "cannot reach $NANO_SSH (if the link is down, the armed rollback should already have fired; otherwise connect directly)"
    nexec "$SUDO rm -f $CONF; $SUDO pkill -f '[a]lfa-usb3-rollback' 2>/dev/null
           $SUDO nmcli con down $NM_CONN >/dev/null 2>&1
           $SUDO modprobe -r $MODULE; sleep 2; $SUDO modprobe $MODULE; sleep 3
           $SUDO nmcli con up $NM_CONN >/dev/null 2>&1; true"
    did "reverted"
    exit 0
fi

# ------------------------------------------------------------------- apply --

head_ "Pre-flight"
if [ "$DRY" != 1 ]; then
    reachable || die "cannot reach $NANO_SSH"
    ok "$NANO_SSH reachable"
    $SSH "$NANO_SSH" "test -e /sys/module/$MODULE/parameters/rtw_switch_usb_mode" 2>/dev/null \
        || die "$MODULE has no rtw_switch_usb_mode parameter — wrong driver loaded?"
    ok "$MODULE supports rtw_switch_usb_mode"
fi
say "rollback will fire in ${ROLLBACK_SECS}s unless --keep is run first"

head_ "Arming the rollback FIRST"
# setsid + nohup so it outlives this SSH session, which the module reload kills.
# It reverts unless --keep has dropped the flag file. $KEEP lives in /run, so a
# reboot clears it and a rebooted-but-broken rover still gets reverted.
nexec "$SUDO rm -f $KEEP
       $SUDO sh -c 'cat > /usr/local/sbin/alfa-usb3-rollback' <<'ROLLBACK'
#!/bin/sh
sleep \$1
[ -e $KEEP ] && exit 0
rm -f $CONF
nmcli con down $NM_CONN >/dev/null 2>&1
modprobe -r $MODULE; sleep 2; modprobe $MODULE; sleep 3
nmcli con up $NM_CONN >/dev/null 2>&1
logger -t alfa-usb3 'rolled back to USB 2.0 (no --keep within timeout)'
ROLLBACK
       $SUDO chmod +x /usr/local/sbin/alfa-usb3-rollback
       $SUDO setsid nohup /usr/local/sbin/alfa-usb3-rollback $ROLLBACK_SECS >/dev/null 2>&1 < /dev/null &
       sleep 1; true"
[ "$DRY" = 1 ] || did "rollback armed (${ROLLBACK_SECS}s)"

head_ "Installing $CONF"
if [ "$DRY" = 1 ]; then
    say "would copy $REPO/mast/8812au-usb3.conf -> $NANO_SSH:$CONF"
else
    scp -q -o BatchMode=yes "$REPO/mast/8812au-usb3.conf" "$NANO_SSH:/tmp/8812au-usb3.conf" \
        || die "scp failed"
    nexec "$SUDO cp /tmp/8812au-usb3.conf $CONF"
    did "installed"
fi

head_ "Reloading $MODULE — THE LINK WILL DROP HERE"
say "the adapter re-enumerates; NM re-activates '$NM_CONN' and its static route"
# Detached: this command kills its own transport, so it must not be waited on.
nexec "$SUDO setsid nohup sh -c 'nmcli con down $NM_CONN >/dev/null 2>&1
       modprobe -r $MODULE; sleep 3; modprobe $MODULE; sleep 5
       nmcli con up $NM_CONN >/dev/null 2>&1' >/dev/null 2>&1 < /dev/null &
       sleep 1; true"

if [ "$DRY" = 1 ]; then say "would now wait for the link"; exit 0; fi

head_ "Waiting for the link"
for i in $(seq 1 30); do
    sleep 5
    if reachable; then
        ok "back after ~$((i * 5))s"
        show_status
        SPEED=$($SSH "$NANO_SSH" "for d in /sys/bus/usb/devices/*/; do case \"\$(cat \$d/idVendor 2>/dev/null):\$(cat \$d/idProduct 2>/dev/null)\" in 0bda:8812) cat \$d/speed;; esac; done" 2>/dev/null | head -1)
        printf '\n'
        if [ "$SPEED" = "5000" ]; then
            did "Alfa is at 5000M (USB 3.0)"
        else
            warn "Alfa is still at ${SPEED}M — the option did not take"
        fi
        say ""
        say "NOW: measure before deciding. A link that is up but unstable is a"
        say "failure here, not a success:"
        say "    iperf3 -c $NANO_HOST -t 8 -R -f m      # was 57 Mbit/s at 480M"
        say "    ping -c 50 $NANO_HOST | tail -2         # watch for a long tail"
        say ""
        say "Happy    ->  ./mast/alfa-usb3.sh --keep      (within ${ROLLBACK_SECS}s of applying)"
        say "Not sure ->  do nothing; it reverts itself"
        say "Revert   ->  ./mast/alfa-usb3.sh --rollback"
        exit 0
    fi
done

warn "link did not return within 150s"
say "The armed rollback fires ${ROLLBACK_SECS}s after it was armed and should"
say "bring it back by itself — wait, then re-check with:"
say "    ./mast/alfa-usb3.sh --status"
say "If it is still dead after that, connect to the Nano directly and run:"
say "    sudo rm -f $CONF && sudo reboot"
exit 1

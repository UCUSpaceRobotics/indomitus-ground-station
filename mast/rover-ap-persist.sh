#!/bin/bash
# Make the 40 MHz hostapd AP survive a reboot, with a rollback that also
# survives a reboot.
#
#     ROVER_PW=... ./mast/rover-ap-persist.sh --install       # install + enable, no reboot yet
#     ROVER_PW=... ./mast/rover-ap-persist.sh --reboot-test   # arm probation, reboot, verify
#     ROVER_PW=... ./mast/rover-ap-persist.sh --confirm       # persistence is good, keep it
#     ROVER_PW=... ./mast/rover-ap-persist.sh --uninstall     # back to the NM Hotspot
#     ROVER_PW=... ./mast/rover-ap-persist.sh --status
#
# WHY THIS IS THE DANGEROUS ONE. The trial script was safe because a reboot
# undid it. Persistence removes that property on purpose: after this, a reboot
# brings hostapd back, so a config that fails at boot leaves the rover
# unreachable — and this Nano has NO wired lifeline (10.45.0.51 does not
# answer). Three independent nets guard that:
#
#   1. rover-ap.service OnFailure -> rover-ap-fallback.service   (hostapd exits)
#   2. rover-ap-watchdog.timer at boot+3min                      (hostapd runs but AP unusable)
#   3. rover-ap-probation.timer at boot+5min                     (anything else — un-persists entirely)
#
# Net 3 is why --reboot-test exists and why --confirm is a separate step: the
# reboot is the test, so the confirmation must be a file on DISK, not in /run.
#
# SEQUENCE: --install, then --reboot-test, then verify the link and MEASURE,
# then --confirm. Skipping --confirm is safe by construction: the rover
# un-persists itself and comes back on the 20 MHz Hotspot.

set -u
NANO_HOST=10.42.0.1
NANO_SSH=indomitus-rover@$NANO_HOST
IFACE=wlx00c0caba86c1
SSH="ssh -o BatchMode=yes -o ConnectTimeout=8"
REPO=$(cd "$(dirname "$0")/.." && pwd)
M=$REPO/mast
ROVER_PW=${ROVER_PW:-}
MODE=""; DRY=0
for a in "$@"; do case "$a" in
    --install) MODE=install;; --reboot-test) MODE=reboot;; --confirm) MODE=confirm;;
    --uninstall) MODE=uninstall;; --status) MODE=status;; --dry) DRY=1;;
    -h|--help) MODE=help;; *) echo "unknown: $a" >&2; exit 2;; esac; done

say(){ printf '  %s\n' "$*"; }
head_(){ printf '\n\033[1m%s\033[0m\n' "$1"; }
did(){ printf '  \033[32m%s\033[0m %s\n' "changed" "$1"; }
ok(){ printf '  \033[2mok\033[0m %s\n' "$1"; }
warn(){ printf '  \033[33mwarn\033[0m %s\n' "$1"; }
die(){ printf '  \033[31merror\033[0m %s\n' "$1" >&2; exit 1; }

[ "$MODE" = help ] || [ -z "$MODE" ] && { sed -n '2,40p' "$0" | sed 's/^# \?//'; exit 0; }
[ "$DRY" != 1 ] && [ -z "$ROVER_PW" ] && die "ROVER_PW is unset"
SUDO="echo '${ROVER_PW:-1}' | sudo -S"
nexec(){ if [ "$DRY" = 1 ]; then printf '  \033[2mwould run\033[0m %s\n' "$1"; return 0; fi; $SSH "$NANO_SSH" "$1"; }
reachable(){ ping -c1 -W2 "$NANO_HOST" >/dev/null 2>&1 && $SSH "$NANO_SSH" true 2>/dev/null; }

check_sudo(){
    [ "$DRY" = 1 ] && return 0
    r=$($SSH "$NANO_SSH" "$SUDO true 2>/dev/null && echo yes || echo no" 2>/dev/null)
    [ "$r" = yes ] || die "sudo failed on the rover — check ROVER_PW. Nothing changed."
    ok "sudo verified"
}

if [ "$MODE" = status ]; then
    head_ "Persistence on $NANO_SSH"
    $SSH "$NANO_SSH" "
      for u in rover-ap rover-ap-dhcp; do echo \"\$u: \$(systemctl is-enabled \$u.service 2>&1) / \$(systemctl is-active \$u.service 2>&1)\"; done
      echo \"watchdog timer: \$(systemctl is-enabled rover-ap-watchdog.timer 2>&1)\"
      echo \"probation timer: \$(systemctl is-enabled rover-ap-probation.timer 2>&1)\"
      echo \"confirmed flag: \$([ -e /etc/rover-ap-confirmed ] && echo SET || echo unset)\"
      echo \"hostapd: \$(pgrep -x hostapd >/dev/null && echo running || echo no)\"
      iw dev $IFACE info 2>/dev/null | grep -oE 'channel [0-9]+ .*width: [0-9]+ MHz'
      echo \"NM: \$(nmcli -t -f GENERAL.CONNECTION dev show $IFACE 2>/dev/null | head -1)\"
      echo \"uptime: \$(uptime -p)\"" 2>/dev/null | sed 's/^/  /'
    exit 0
fi

if [ "$MODE" = confirm ]; then
    head_ "Confirming persistence"
    reachable || die "cannot reach the rover — do NOT confirm a state you cannot verify"
    W=$($SSH "$NANO_SSH" "iw dev $IFACE info 2>/dev/null | grep -oE 'width: [0-9]+ MHz'" 2>/dev/null)
    say "AP is currently: ${W:-unknown}"
    case "$W" in *"40 MHz"*) ;; *) warn "not 40 MHz — confirming would lock in a state that is not what you wanted";; esac
    check_sudo
    nexec "$SUDO touch /etc/rover-ap-confirmed
           $SUDO systemctl disable rover-ap-probation.timer >/dev/null 2>&1; true"
    did "confirmed — /etc/rover-ap-confirmed written, probation disabled"
    say "the AP now survives reboots. Undo with --uninstall."
    exit 0
fi

if [ "$MODE" = uninstall ]; then
    head_ "Removing persistence"
    reachable || die "cannot reach the rover"
    check_sudo
    nexec "$SUDO rm -f /etc/rover-ap-confirmed
           $SUDO systemctl disable --now rover-ap-dhcp.service rover-ap.service rover-ap-watchdog.timer rover-ap-probation.timer >/dev/null 2>&1
           $SUDO systemctl start rover-ap-fallback.service; true"
    did "units disabled; NM Hotspot restored at 20 MHz"
    exit 0
fi

if [ "$MODE" = install ]; then
    head_ "Pre-flight"
    reachable || die "cannot reach the rover"
    ok "rover reachable"
    check_sudo

    head_ "Installing config, units and helpers"
    for f in rover-ap-hostapd-vht40.conf rover-ap.service rover-ap-dhcp.service \
             rover-ap-fallback.service rover-ap-watchdog.service rover-ap-watchdog.timer \
             rover-ap-probation.service rover-ap-probation.timer \
             rover-ap-watchdog.sh rover-ap-probation.sh; do
        [ -e "$M/$f" ] || die "missing $M/$f"
    done
    if [ "$DRY" = 1 ]; then say "would scp 10 files and install them"; else
        scp -q -o BatchMode=yes \
            "$M/rover-ap-hostapd-vht40.conf" "$M/rover-ap.service" "$M/rover-ap-dhcp.service" \
            "$M/rover-ap-fallback.service" "$M/rover-ap-watchdog.service" "$M/rover-ap-watchdog.timer" \
            "$M/rover-ap-probation.service" "$M/rover-ap-probation.timer" \
            "$M/rover-ap-watchdog.sh" "$M/rover-ap-probation.sh" \
            "$NANO_SSH:/tmp/" || die "scp failed"
        did "staged in /tmp"
    fi
    nexec "$SUDO install -m 0600 /tmp/rover-ap-hostapd-vht40.conf /etc/hostapd/rover-ap.conf
           $SUDO install -m 0644 /tmp/rover-ap.service /tmp/rover-ap-dhcp.service /tmp/rover-ap-fallback.service /tmp/rover-ap-watchdog.service /tmp/rover-ap-watchdog.timer /tmp/rover-ap-probation.service /tmp/rover-ap-probation.timer /etc/systemd/system/
           $SUDO install -m 0755 /tmp/rover-ap-watchdog.sh /usr/local/sbin/rover-ap-watchdog
           $SUDO install -m 0755 /tmp/rover-ap-probation.sh /usr/local/sbin/rover-ap-probation
           $SUDO systemctl daemon-reload
           $SUDO systemctl enable rover-ap.service rover-ap-dhcp.service rover-ap-watchdog.timer >/dev/null 2>&1
           true"
    [ "$DRY" = 1 ] || did "installed and enabled (NOT started, NOT rebooted)"
    say ""
    say "hostapd config syntax check:"
    nexec "$SUDO hostapd -t -d /etc/hostapd/rover-ap.conf 2>&1 | tail -3" || true
    say ""
    say "Next: ROVER_PW=... $0 --reboot-test"
    exit 0
fi

if [ "$MODE" = reboot ]; then
    head_ "Reboot test"
    reachable || die "cannot reach the rover"
    check_sudo
    say "arming probation: unless --confirm runs within 5 min of boot, the rover"
    say "un-persists itself and comes back on the 20 MHz NM Hotspot"
    nexec "$SUDO rm -f /etc/rover-ap-confirmed
           $SUDO systemctl enable rover-ap-probation.timer >/dev/null 2>&1
           $SUDO setsid nohup sh -c 'sleep 2; reboot' >/dev/null 2>&1 &
           sleep 1; true"
    did "rebooting"

    head_ "Waiting for the rover"
    BACK=0
    for i in $(seq 1 40); do sleep 5; reachable && { ok "back after ~$((i*5))s"; BACK=1; break; }; done
    [ "$BACK" = 1 ] || die "rover did not come back within 200s — probation fires at 5 min and restores the Hotspot; wait, do not panic"

    W=$($SSH "$NANO_SSH" "iw dev $IFACE info 2>/dev/null | grep -oE 'width: [0-9]+ MHz'" 2>/dev/null)
    printf '\n'
    case "$W" in
        *"40 MHz"*) did "AP came back at 40 MHz after a reboot";;
        *) warn "AP is ${W:-unknown} after reboot — a fallback probably fired; check 'journalctl -t rover-ap -t rover-ap-watchdog'";;
    esac
    say ""
    say "MEASURE NOW, then confirm within 5 minutes of the boot:"
    say "    ssh $NANO_SSH 'nohup iperf3 -s -D'"
    say "    iperf3 -c $NANO_HOST -t 10 -f m ; iperf3 -c $NANO_HOST -t 10 -f m -R"
    say "    ROVER_PW=... $0 --confirm"
    say ""
    say "Do nothing and it un-persists itself. That is the safe default."
    exit 0
fi

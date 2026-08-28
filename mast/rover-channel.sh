#!/bin/bash
# Change the ROVER Wi-Fi channel AND recover ROS on the new channel, in one go.
#
#     ROVER_PW=... ./mast/rover-channel.sh 149            # 5 GHz channel 149
#     ROVER_PW=... ./mast/rover-channel.sh 44 --width 20
#     ROVER_PW=... ./mast/rover-channel.sh 11 --band bg   # 2.4 GHz
#     ./mast/rover-channel.sh --dry 149
#
# The rover twin of mast/bench-channel.sh. Run this ON THE GS PC. It:
#   1. rewrites channel + VHT centre index in the rover's live
#      /etc/hostapd/rover-ap.conf and restarts rover-ap.service;
#   2. reassociates the mast Pi (it does not reconnect on its own after an AP
#      restart) and waits for the link to come back;
#   3. restarts the rover-side ROS via rover-up.sh so the feeds re-establish.
#
# CAVEAT: this edits the LIVE rover-ap.conf. mast/restore-link.sh restores that
# file from a canned config (rover-ap-rtw88-vht40.conf, ch36) on its next run,
# so a channel set here is not permanent — to persist it, bake the channel into
# the canned config too. The rover uses prebuilt hostapd configs by design; this
# is the field-expedient override, not a replacement for them.
set -uo pipefail

# ================================================================ defaults ==

ROVER_SSH=${ROVER_SSH:-indomitus-rover@10.42.0.1}
ROVER_LIFELINE=${ROVER_LIFELINE:-indomitus-rover@10.45.0.51}
ROVER_LINK_IP=${ROVER_LINK_IP:-10.42.0.1}
ROVER_PW=${ROVER_PW:-}

PI_SSH=${PI_SSH:-admin@10.44.0.1}
PI_ALFA=${PI_ALFA:-wlx00c0caba8237}
HOSTAPD_CONF=${HOSTAPD_CONF:-/etc/hostapd/rover-ap.conf}

BENCH_CHAN=""          # reused name for the channel; positional or --channel
BAND=${BAND:-a}
WIDTH=${WIDTH:-40}
DRY=0

usage() {
    cat <<EOF
Change the rover Wi-Fi channel and recover ROS on it. Run ON THE GS PC.

  ROVER_PW=... ./mast/rover-channel.sh 149
  ROVER_PW=... ./mast/rover-channel.sh 44 --width 20
  ROVER_PW=... ./mast/rover-channel.sh 11 --band bg
  ./mast/rover-channel.sh --dry 149

Options
  --channel N     channel (or give it as the first positional argument)
  --band a|bg     5 GHz or 2.4 GHz          (default: $BAND)
  --width 20|40   channel width            (default: $WIDTH)
  --rover-ssh U@H rover over Wi-Fi          (default: $ROVER_SSH)
  --conf PATH     rover hostapd conf        (default: $HOSTAPD_CONF)
  --dry           print what would change, change nothing
  -h, --help

Environment: ROVER_PW (rover sudo, required), ROVER_SSH, ROVER_LIFELINE,
PI_SSH, PI_ALFA, HOSTAPD_CONF.
EOF
    exit 0
}

while [ $# -gt 0 ]; do
    case "$1" in
        --channel)   BENCH_CHAN=$2; shift 2 ;;
        --band)      BAND=$2; shift 2 ;;
        --width)     WIDTH=$2; shift 2 ;;
        --rover-ssh) ROVER_SSH=$2; shift 2 ;;
        --conf)      HOSTAPD_CONF=$2; shift 2 ;;
        --dry)       DRY=1; shift ;;
        -h|--help)   usage ;;
        -*)          echo "unknown option: $1 (try --help)"; exit 1 ;;
        *)           BENCH_CHAN=$1; shift ;;
    esac
done
CHAN=$BENCH_CHAN
[ -n "$CHAN" ] || { echo "error: no channel given (try --help)"; exit 1; }

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SSH="ssh -o BatchMode=yes -o StrictHostKeyChecking=accept-new -o ConnectTimeout=8"

say()  { printf '  %s\n' "$*"; }
head_(){ printf '\n\033[1m%s\033[0m\n' "$1"; }
did()  { printf '  \033[32m%s\033[0m %s\n' "changed" "$1"; }
warn() { printf '  \033[33mWARN\033[0m  %s\n' "$1"; }
die()  { printf '  \033[31mFAIL\033[0m  %s\n' "$1"; exit 1; }
ping_ok() { ping -c1 -W2 "$1" >/dev/null 2>&1; }

need_rover_pw() {
    [ -n "$ROVER_PW" ] && return 0
    if [ -t 0 ]; then read -rsp "  rover sudo password (ROVER_PW): " ROVER_PW; echo
    else die "ROVER_PW unset and no TTY to prompt on. Export it or run interactively."; fi
}

ROVER=""
locate_rover() {
    local t
    for t in "$ROVER_SSH" "$ROVER_LIFELINE"; do
        $SSH "$t" true 2>/dev/null && { ROVER=$t; return 0; }
    done
    return 1
}

# 5 GHz 40 MHz channels pair as (36,40)(44,48)... and (149,153)(157,161); the
# lower member is HT40+ (centre +2), the upper HT40- (centre -2). 20 MHz has no
# secondary and the VHT centre is the channel itself. Same math as
# restore-bench-link.sh's compute_radio.
compute_seg0() {
    if [ "$WIDTH" = "20" ] || [ "$BAND" = "bg" ]; then VHT_SEG0=$CHAN; return; fi
    local idx
    if [ "$CHAN" -ge 149 ]; then idx=$(( (CHAN - 149) / 4 )); else idx=$(( (CHAN - 36) / 4 )); fi
    if [ $(( idx % 2 )) -eq 0 ]; then VHT_SEG0=$(( CHAN + 2 )); else VHT_SEG0=$(( CHAN - 2 )); fi
}
compute_seg0

printf '\033[1mRover channel change -> ch%s (%s, %sMHz)\033[0m  %s%s\n' \
    "$CHAN" "$BAND" "$WIDTH" "$(date -Is)" "$([ "$DRY" = 1 ] && echo '  [DRY RUN]')"

# ------------------------------------------------------------------ RF change
head_ "Rover AP -> channel $CHAN (VHT centre $VHT_SEG0)"
if [ "$DRY" = 1 ]; then
    say "[dry] on the rover: sed channel=$CHAN, vht_oper_centr_freq_seg0_idx=$VHT_SEG0 in $HOSTAPD_CONF"
    say "[dry] restart rover-ap.service; reassoc Pi ($PI_ALFA); wait for $ROVER_LINK_IP"
else
    locate_rover || die "rover unreachable over $ROVER_SSH or $ROVER_LIFELINE"
    say "via ${ROVER#*@}"
    need_rover_pw

    # Prime sudo, edit the two lines, restart the AP. sed only rewrites existing
    # keys; if either is absent the file is not the expected hostapd conf.
    $SSH "$ROVER" "printf '%s\n' '$ROVER_PW' | sudo -S -p '' -v && \
        sudo grep -qE '^channel=' '$HOSTAPD_CONF' && sudo grep -qE '^vht_oper_centr_freq_seg0_idx=' '$HOSTAPD_CONF' && \
        sudo sed -i 's/^channel=.*/channel=$CHAN/; s/^vht_oper_centr_freq_seg0_idx=.*/vht_oper_centr_freq_seg0_idx=$VHT_SEG0/' '$HOSTAPD_CONF' && \
        sudo systemctl restart rover-ap.service" \
        || die "failed to edit $HOSTAPD_CONF / restart rover-ap.service (is it the expected hostapd config?)"
    did "channel=$CHAN, VHT centre=$VHT_SEG0 in $HOSTAPD_CONF; rover-ap restarted"

    # The Pi does not reconnect on its own after an AP restart.
    say "reassociating the mast Pi ($PI_ALFA) ..."
    $SSH "$PI_SSH" "sudo systemctl restart netplan-wpa-$PI_ALFA.service" 2>/dev/null \
        || warn "could not restart netplan-wpa-$PI_ALFA on the Pi — reassociate it by hand"
    sleep 8

    say "waiting for $ROVER_LINK_IP on the new channel ..."
    up=0
    for i in $(seq 1 20); do ping_ok "$ROVER_LINK_IP" && { up=1; break; }; sleep 3; done
    [ "$up" = 1 ] || die "rover did not come back at $ROVER_LINK_IP after the channel change"
    did "rover back at $ROVER_LINK_IP"
fi

# ------------------------------------------------------------- restart ROS
head_ "Recover ROS on the new channel"
ROVER_PW="$ROVER_PW" ROVER_SSH="$ROVER_SSH" ROVER_LIFELINE="$ROVER_LIFELINE" \
    exec "$REPO/mast/rover-up.sh" --skip-link --no-gs --no-ui $([ "$DRY" = 1 ] && echo --dry)

#!/bin/bash
# Change the bench Wi-Fi channel AND recover ROS on the new channel, in one go.
#
#     ./mast/bench-channel.sh 149            # move the bench link to ch149
#     ./mast/bench-channel.sh 11 --band bg   # 2.4 GHz, channel 11
#     ./mast/bench-channel.sh 44 --width 20
#     ./mast/bench-channel.sh --dry 149
#
# Run this ON THE GS PC. It:
#   1. re-runs mast/restore-bench-link.sh --channel N on the bench (the RF/IP
#      link is otherwise unchanged — same SSID, same /24, same addresses, so
#      the DDS profiles stay valid and are NOT regenerated);
#   2. waits for the bench to come back on the new channel;
#   3. restarts the rover-side ROS launch via bench-up.sh so the camera feeds
#      re-establish over the link — the GS stack and UI are left running.
#
# Why a wrapper and not just restore-bench-link.sh --channel: a channel change
# bounces hostapd, the link drops for a few seconds, and the rover's DDS
# participants can be left publishing into a link that flapped under them. The
# clean fix is to relaunch the rover side once the link is back — which is
# exactly what bench-up.sh --skip-link does. See mast/BENCH-LINK.md.
set -uo pipefail

# ================================================================ defaults ==

BENCH_SSH=${BENCH_SSH:-starezax@10.43.0.1}
BENCH_SETUP_SSH=${BENCH_SETUP_SSH:-$BENCH_SSH}
BENCH_SUDO_PASS=${BENCH_SUDO_PASS:-}
BENCH_LINK_IP=${BENCH_LINK_IP:-10.43.0.1}

BENCH_CHAN=""
BENCH_BAND=${BENCH_BAND:-a}
BENCH_WIDTH=${BENCH_WIDTH:-40}
DRY=0

usage() {
    cat <<EOF
Change the bench Wi-Fi channel and recover ROS on it. Run ON THE GS PC.

  ./mast/bench-channel.sh 149              # 5 GHz channel 149
  ./mast/bench-channel.sh 11 --band bg     # 2.4 GHz channel 11
  ./mast/bench-channel.sh 44 --width 20
  ./mast/bench-channel.sh --dry 149

Options
  --channel N     channel (or give it as the first positional argument)
  --band a|bg     5 GHz or 2.4 GHz          (default: $BENCH_BAND)
  --width 20|40   channel width            (default: $BENCH_WIDTH)
  --bench-ssh U@H bench over the link       (default: $BENCH_SSH)
  --setup-ssh U@H bench for the RF change   (default: \$BENCH_SSH)
  --dry           print what would change, change nothing
  -h, --help

Environment: BENCH_SSH, BENCH_SETUP_SSH, BENCH_SUDO_PASS, BENCH_LINK_IP.
EOF
    exit 0
}

while [ $# -gt 0 ]; do
    case "$1" in
        --channel)   BENCH_CHAN=$2; shift 2 ;;
        --band)      BENCH_BAND=$2; shift 2 ;;
        --width)     BENCH_WIDTH=$2; shift 2 ;;
        --bench-ssh) BENCH_SSH=$2; shift 2 ;;
        --setup-ssh) BENCH_SETUP_SSH=$2; shift 2 ;;
        --dry)       DRY=1; shift ;;
        -h|--help)   usage ;;
        -*)          echo "unknown option: $1 (try --help)"; exit 1 ;;
        *)           BENCH_CHAN=$1; shift ;;   # positional channel
    esac
done

[ -n "$BENCH_CHAN" ] || { echo "error: no channel given (try --help)"; exit 1; }

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SSH="ssh -o BatchMode=yes -o StrictHostKeyChecking=accept-new -o ConnectTimeout=8"

say()  { printf '  %s\n' "$*"; }
head_(){ printf '\n\033[1m%s\033[0m\n' "$1"; }
did()  { printf '  \033[32m%s\033[0m %s\n' "changed" "$1"; }
warn() { printf '  \033[33mWARN\033[0m  %s\n' "$1"; }
die()  { printf '  \033[31mFAIL\033[0m  %s\n' "$1"; exit 1; }
ping_ok() { ping -c1 -W2 "$1" >/dev/null 2>&1; }

need_sudo_pass() {
    [ -n "$BENCH_SUDO_PASS" ] && return 0
    if [ -t 0 ]; then
        read -rsp "  bench sudo password for ${BENCH_SETUP_SSH#*@}: " BENCH_SUDO_PASS; echo
    else
        die "BENCH_SUDO_PASS unset and no TTY to prompt on. Export it or run interactively."
    fi
}

printf '\033[1mBench channel change -> ch%s (%s, %sMHz)\033[0m  %s%s\n' \
    "$BENCH_CHAN" "$BENCH_BAND" "$BENCH_WIDTH" "$(date -Is)" \
    "$([ "$DRY" = 1 ] && echo '  [DRY RUN]')"

# ------------------------------------------------------------------ RF change
head_ "Bench link -> channel $BENCH_CHAN"
if [ "$DRY" = 1 ]; then
    say "[dry] on $BENCH_SETUP_SSH: restore-bench-link.sh --channel $BENCH_CHAN --band $BENCH_BAND --width $BENCH_WIDTH --no-gs"
else
    $SSH "$BENCH_SETUP_SSH" true 2>/dev/null \
        || die "cannot SSH to $BENCH_SETUP_SSH — set --setup-ssh to a reachable address"
    need_sudo_pass
    scp -o BatchMode=yes -o StrictHostKeyChecking=accept-new -o ConnectTimeout=8 "$REPO/mast/restore-bench-link.sh" \
        "$BENCH_SETUP_SSH:/tmp/restore-bench-link.sh" >/dev/null \
        || die "scp of restore-bench-link.sh failed"
    # --no-gs: the GS route does not change with the channel. Prime sudo once.
    $SSH "$BENCH_SETUP_SSH" "printf '%s\n' '$BENCH_SUDO_PASS' | sudo -S -p '' -v && \
        bash /tmp/restore-bench-link.sh --channel $BENCH_CHAN --band $BENCH_BAND --width $BENCH_WIDTH --no-gs" \
        || warn "restore-bench-link.sh returned non-zero — check output above"
    did "channel set to $BENCH_CHAN on the bench"

    say "waiting for $BENCH_LINK_IP on the new channel ..."
    ok=0
    for i in $(seq 1 20); do ping_ok "$BENCH_LINK_IP" && { ok=1; break; }; sleep 3; done
    [ "$ok" = 1 ] || die "bench did not come back at $BENCH_LINK_IP after the channel change"
    did "bench back at $BENCH_LINK_IP"
fi

# ------------------------------------------------------------- restart ROS
head_ "Recover ROS on the new channel"
# Link is up and unchanged at the IP layer, so skip the link, GS stack and UI —
# only relaunch the rover side so the camera DDS re-establishes.
BENCH_SUDO_PASS="$BENCH_SUDO_PASS" BENCH_SSH="$BENCH_SSH" BENCH_SETUP_SSH="$BENCH_SETUP_SSH" \
    exec "$REPO/mast/bench-up.sh" --skip-link --no-gs --no-ui $([ "$DRY" = 1 ] && echo --dry)

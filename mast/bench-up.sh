#!/bin/bash
# Bring the whole BENCH test up in one command, from the GS PC.
#
#     ./mast/bench-up.sh                       # link + GS stack + rover + UI
#     ./mast/bench-up.sh --dry                 # print what would happen
#     ./mast/bench-up.sh --channel 149         # bootstrap the link on ch149
#     ./mast/bench-up.sh --skip-link           # link already up, do the rest
#     ./mast/bench-up.sh --no-ui               # everything except the UI
#     ./mast/bench-up.sh --ros-only            # only restart ROS (GS + rover)
#     ./mast/bench-up.sh --help
#
# Run this ON THE GS PC. It:
#   1. brings the bench link up (mast/restore-bench-link.sh on the bench) if
#      10.43.0.1 is not reachable, and adds the GS route to 10.43.0.0/24;
#   2. starts the GS console stack (docker-compose.gs.yaml) pointed at the
#      bench as its DDS peer;
#   3. SSHes to the bench, wires the two arducams to /dev/arducam-{mast,rear},
#      generates the bench Fast DDS profile, starts rover_dev and launches JUST
#      the two arducams (full rover.launch.py is not bench-viable);
#   4. serves the UI with `npm run dev` on :5173;
#   5. verifies the camera topics reach the GS and web_video_server serves them.
#
# Companion to mast/restore-bench-link.sh (the RF/IP link) and
# mast/bench-channel.sh (change channel + restart ROS). See mast/BENCH-LINK.md.
#
# The bench keeps the rover's NATIVE camera namespaces (mast_arducam,
# rear_arducam) — the UI's default tiles point at /camera/*, so re-point a tile
# in the settings dialog to /mast_arducam/image_raw to see the feed, or leave
# the UI as-is. This is deliberate; do not "fix" it by renaming here.
set -uo pipefail

# ================================================================ defaults ==

# --- bench reachability ----------------------------------------------------
BENCH_SSH=${BENCH_SSH:-starezax@10.43.0.1}       # over the bench link, once up
BENCH_SETUP_SSH=${BENCH_SETUP_SSH:-$BENCH_SSH}   # used to bootstrap the link
BENCH_ROVER_DIR=${BENCH_ROVER_DIR:-/home/starezax/Desktop/indomitus/indomitus-rover-core}
# Remote sudo password (the bench sudo is NOT passwordless). Prompted on a TTY
# if left empty. Never echoed; primes the sudo timestamp once per SSH session.
BENCH_SUDO_PASS=${BENCH_SUDO_PASS:-}

# --- the link --------------------------------------------------------------
BENCH_LINK_IP=${BENCH_LINK_IP:-10.43.0.1}        # bench AP address
BENCH_LINK_PREFIX=${BENCH_LINK_PREFIX:-10.43.0.} # bench /24, for the DDS whitelist
GS_LINK_PREFIX=${GS_LINK_PREFIX:-10.44.0.}       # GS wired /24
GS_IP=${GS_IP:-10.44.0.10}                       # GS address the rover peers back to
GS_VIA=${GS_VIA:-10.44.0.1}                       # mast Pi, routes between the /24s
BENCH_NET=${BENCH_NET:-10.43.0.0/24}

# restore-bench-link.sh knobs, only used when bootstrapping a down link.
BENCH_CHAN=${BENCH_CHAN:-44}
BENCH_BAND=${BENCH_BAND:-a}
BENCH_WIDTH=${BENCH_WIDTH:-40}

# --- ROS -------------------------------------------------------------------
ROS_DOMAIN_ID=${ROS_DOMAIN_ID:-90}
RMW=${RMW:-rmw_fastrtps_cpp}
ROVER_PEER=${ROVER_PEER:-$BENCH_LINK_IP}         # GS discovers the bench here
ZED_MODE=${ZED_MODE:-}                           # '' = no ZED (none on the bench)
# Where the bench writes its generated Fast DDS profile (inside the mounted
# rover-core repo, so the container at /work sees it). gen-dds-profile.sh always
# writes this exact basename — keep it in sync with the generator.
BENCH_DDS_PROFILE=${BENCH_DDS_PROFILE:-/work/docker/fastdds_rover_link.xml}

# --- UI --------------------------------------------------------------------
UI_PORT=${UI_PORT:-5173}

DRY=0
DO_LINK=1
DO_GS=1
DO_ROVER=1
DO_UI=1

usage() {
    cat <<EOF
Bring the whole BENCH test up from the GS PC: link, GS stack, rover, UI.
Idempotent; see mast/BENCH-LINK.md.

  ./mast/bench-up.sh                 # everything
  ./mast/bench-up.sh --dry           # change nothing, print the plan
  ./mast/bench-up.sh --channel 149   # bootstrap a down link on ch149
  ./mast/bench-up.sh --skip-link     # assume the link is up
  ./mast/bench-up.sh --ros-only      # only (re)start GS + rover ROS
  ./mast/bench-up.sh --no-ui

Options
  --bench-ssh U@H     bench over the link      (default: $BENCH_SSH)
  --setup-ssh U@H     bench for link bootstrap (default: \$BENCH_SSH)
  --rover-dir PATH    rover-core repo on bench (default: $BENCH_ROVER_DIR)
  --channel N         bench Wi-Fi channel      (default: $BENCH_CHAN)
  --band a|bg         5 GHz or 2.4 GHz         (default: $BENCH_BAND)
  --width 20|40       channel width            (default: $BENCH_WIDTH)
  --domain N          ROS_DOMAIN_ID            (default: $ROS_DOMAIN_ID)
  --peer IP           DDS peer (the bench)     (default: $ROVER_PEER)
  --zed-mode ''|rgb|nav  ZED2i mode            (default: '${ZED_MODE}', none on bench)
  --ui-port N         vite dev server port     (default: $UI_PORT)
  --skip-link         do not touch the bench link
  --ros-only          only restart GS + rover ROS (implies --skip-link --no-ui)
  --no-link | --no-gs | --no-rover | --no-ui
  --dry               print what would change, change nothing
  -h, --help

Environment: every default can be set as an env var of the same name
(BENCH_SSH=..., BENCH_SUDO_PASS=..., ROS_DOMAIN_ID=...). Flags win over env.
EOF
    exit 0
}

while [ $# -gt 0 ]; do
    case "$1" in
        --bench-ssh) BENCH_SSH=$2; shift 2 ;;
        --setup-ssh) BENCH_SETUP_SSH=$2; shift 2 ;;
        --rover-dir) BENCH_ROVER_DIR=$2; shift 2 ;;
        --channel)   BENCH_CHAN=$2; shift 2 ;;
        --band)      BENCH_BAND=$2; shift 2 ;;
        --width)     BENCH_WIDTH=$2; shift 2 ;;
        --domain)    ROS_DOMAIN_ID=$2; shift 2 ;;
        --peer)      ROVER_PEER=$2; shift 2 ;;
        --zed-mode)  ZED_MODE=$2; shift 2 ;;
        --ui-port)   UI_PORT=$2; shift 2 ;;
        --skip-link|--no-link) DO_LINK=0; shift ;;
        --no-gs)     DO_GS=0; shift ;;
        --no-rover)  DO_ROVER=0; shift ;;
        --no-ui)     DO_UI=0; shift ;;
        --ros-only)  DO_LINK=0; DO_UI=0; shift ;;
        --dry)       DRY=1; shift ;;
        -h|--help)   usage ;;
        *) echo "unknown option: $1 (try --help)"; exit 1 ;;
    esac
done

# The GS repo is the parent of this script's directory.
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
GS_COMPOSE="$REPO/docker/docker-compose.gs.yaml"

# --------------------------------------------------------------- reporting --

say()   { printf '  %s\n' "$*"; }
head_() { printf '\n\033[1m%s\033[0m\n' "$1"; }
did()   { printf '  \033[32m%s\033[0m %s\n' "changed" "$1"; }
ok()    { printf '  \033[2mok\033[0m %s\n' "$1"; }
warn()  { printf '  \033[33mWARN\033[0m  %s\n' "$1"; }
die()   { printf '  \033[31mFAIL\033[0m  %s\n' "$1"; exit 1; }

SSH="ssh -o BatchMode=yes -o StrictHostKeyChecking=accept-new -o ConnectTimeout=8"

# Run a command on the bench. `bssh <target> <cmd...>`.
bssh() { local t=$1; shift; $SSH "$t" "$@"; }

# Prime the bench sudo timestamp once for this call, then run a privileged
# remote command. sudo caches the credential for the rest of the SSH session,
# so restore-bench-link.sh's own internal `sudo` calls do not re-prompt.
#   bsudo <target> <shell-command-string>
bsudo() {
    local t=$1; shift
    [ "$DRY" = 1 ] && { say "[dry] $t: sudo $*"; return 0; }
    $SSH "$t" "printf '%s\n' '$BENCH_SUDO_PASS' | sudo -S -p '' -v && $*"
}

ping_ok() { ping -c1 -W2 "$1" >/dev/null 2>&1; }

need_sudo_pass() {
    [ -n "$BENCH_SUDO_PASS" ] && return 0
    if [ -t 0 ]; then
        read -rsp "  bench sudo password for ${BENCH_SETUP_SSH#*@}: " BENCH_SUDO_PASS; echo
    else
        die "BENCH_SUDO_PASS is unset and no TTY to prompt on. Export it or run interactively."
    fi
}

# ============================================================= preflight ====

preflight() {
    head_ "Preflight (GS PC)"
    command -v docker >/dev/null || die "docker not installed"
    docker compose version >/dev/null 2>&1 || die "docker compose v2 not available"
    [ -f "$GS_COMPOSE" ] || die "missing $GS_COMPOSE"
    ok "docker + compose + $GS_COMPOSE"
    ok "GS repo: $REPO"
}

# ================================================================== LINK ====

# The GS PC needs a static route to the bench /24 via the Pi. Put it on the NM
# profile of whichever interface holds the GS link address, so it survives a
# reboot (an `ip route add` would not — see BENCH-LINK.md's persistence table).
ensure_gs_route() {
    ping_ok "$BENCH_LINK_IP" && { ok "GS route to $BENCH_NET (bench reachable)"; return 0; }

    local iface prof
    iface=$(ip -o -4 addr show 2>/dev/null | awk -v p="$GS_LINK_PREFIX" '$4 ~ "^"p {print $2; exit}')
    [ -n "$iface" ] || { warn "no interface on $GS_LINK_PREFIX — is the wire to the Pi up?"; return 1; }
    prof=$(nmcli -t -f NAME,DEVICE connection show --active 2>/dev/null | awk -F: -v d="$iface" '$2==d{print $1; exit}')
    [ -n "$prof" ] || { warn "no active NM profile on $iface — add route by hand: nmcli con modify PROFILE +ipv4.routes '$BENCH_NET $GS_VIA'"; return 1; }

    local routes
    routes=$(nmcli -g ipv4.routes connection show "$prof" 2>/dev/null)
    if [ "${routes#*"$BENCH_NET"}" != "$routes" ]; then
        ok "$BENCH_NET route already on $prof"
    else
        [ "$DRY" = 1 ] && { did "would add $BENCH_NET via $GS_VIA to $prof (dry)"; return 0; }
        nmcli con modify "$prof" +ipv4.routes "$BENCH_NET $GS_VIA" \
            || sudo nmcli con modify "$prof" +ipv4.routes "$BENCH_NET $GS_VIA" \
            || { warn "could not add the route to $prof"; return 1; }
        nmcli con up "$prof" >/dev/null 2>&1 || sudo nmcli con up "$prof" >/dev/null 2>&1
        did "added $BENCH_NET via $GS_VIA to $prof (and reactivated)"
    fi
}

ensure_link() {
    head_ "Bench link"
    [ "$DO_LINK" = 1 ] || { say "skipped (--skip-link)"; return 0; }

    ensure_gs_route

    if ping_ok "$BENCH_LINK_IP"; then
        ok "bench reachable at $BENCH_LINK_IP — link already up"
        return 0
    fi

    say "bench $BENCH_LINK_IP unreachable — bootstrapping via restore-bench-link.sh"

    if [ "$DRY" = 1 ]; then
        say "[dry] scp mast/restore-bench-link.sh -> $BENCH_SETUP_SSH:/tmp/ and run it (--no-gs, ch$BENCH_CHAN/$BENCH_WIDTH)"
        say "[dry] then wait for $BENCH_LINK_IP"
        return 0
    fi

    $SSH "$BENCH_SETUP_SSH" true 2>/dev/null \
        || die "cannot SSH to $BENCH_SETUP_SSH to bootstrap the link. Set --setup-ssh to a currently reachable address (e.g. the campus IP)."
    need_sudo_pass

    scp -o BatchMode=yes -o StrictHostKeyChecking=accept-new -o ConnectTimeout=8 "$REPO/mast/restore-bench-link.sh" "$BENCH_SETUP_SSH:/tmp/restore-bench-link.sh" >/dev/null \
        || die "scp of restore-bench-link.sh to the bench failed"
    # Prime sudo, then run the link script (it sudo's internally; --no-gs
    # because the GS route is handled locally above).
    $SSH "$BENCH_SETUP_SSH" "printf '%s\n' '$BENCH_SUDO_PASS' | sudo -S -p '' -v && \
        BENCH_CHAN=$BENCH_CHAN BENCH_BAND=$BENCH_BAND BENCH_WIDTH=$BENCH_WIDTH \
        bash /tmp/restore-bench-link.sh --no-gs" \
        || warn "restore-bench-link.sh returned non-zero — check its output above"
    did "ran restore-bench-link.sh on the bench"

    say "waiting for $BENCH_LINK_IP ..."
    local i
    for i in $(seq 1 20); do
        ping_ok "$BENCH_LINK_IP" && { ok "bench up at $BENCH_LINK_IP"; return 0; }
        sleep 3
    done
    die "bench still unreachable at $BENCH_LINK_IP after link bootstrap"
}

# =================================================================== GS =====

start_gs() {
    head_ "GS console stack"
    [ "$DO_GS" = 1 ] || { say "skipped (--no-gs)"; return 0; }

    say "peer=$ROVER_PEER domain=$ROS_DOMAIN_ID  (docker-compose.gs.yaml)"
    if [ "$DRY" = 1 ]; then
        say "[dry] ROVER_PEER=$ROVER_PEER ROS_DOMAIN_ID=$ROS_DOMAIN_ID docker compose -f $GS_COMPOSE --project-directory $REPO up -d"
        return 0
    fi
    # ROVER_PEER re-points the GS Fast DDS profile at the bench; the entrypoint
    # regenerates the profile at (re)creation, so a peer change needs up -d, not
    # restart. Compose recreates only when the environment actually changed.
    ( cd "$REPO" && ROVER_PEER="$ROVER_PEER" ROS_DOMAIN_ID="$ROS_DOMAIN_ID" \
        docker compose -f "$GS_COMPOSE" --project-directory "$REPO" up -d ) \
        || die "docker compose up failed"
    did "GS stack up (rosbridge :9090, web_video_server :8080)"
}

# ================================================================ ROVER =====

# The rover side runs from a standalone script (mast/bench-rover.sh) scp'd to
# the bench, NOT an inline heredoc — embedding gen-dds-profile.sh in a heredoc
# corrupts its own $-expansions and heredocs. The sudo timestamp is primed once
# so the remote script's sudo calls (udev rules) do not re-prompt.
start_rover() {
    head_ "Bench rover side"
    [ "$DO_ROVER" = 1 ] || { say "skipped (--no-rover)"; return 0; }

    if [ "$DRY" = 1 ]; then
        say "[dry] scp mast/bench-rover.sh + docker/gen-dds-profile.sh -> $BENCH_SSH:/tmp/"
        say "[dry] on $BENCH_SSH: bench-rover.sh (arducam udev, DDS profile, rover_dev, rover.launch.py)"
        return 0
    fi

    $SSH "$BENCH_SSH" true 2>/dev/null \
        || die "cannot SSH to $BENCH_SSH (is the link up? try without --skip-link)"
    need_sudo_pass

    scp -o BatchMode=yes -o StrictHostKeyChecking=accept-new -o ConnectTimeout=8 \
        "$REPO/mast/bench-rover.sh" "$REPO/docker/gen-dds-profile.sh" \
        "$BENCH_SSH:/tmp/" >/dev/null \
        || die "scp of bench-rover.sh / gen-dds-profile.sh to the bench failed"

    # Prime sudo, then run the rover-side script with its environment.
    $SSH "$BENCH_SSH" "printf '%s\n' '$BENCH_SUDO_PASS' | sudo -S -p '' -v && \
        ROVER_DIR='$BENCH_ROVER_DIR' DOMAIN=$ROS_DOMAIN_ID RMW=$RMW \
        PROFILE='$BENCH_DDS_PROFILE' LINK_PREFIX='$BENCH_LINK_PREFIX' GS_IP='$GS_IP' \
        ZED_MODE='$ZED_MODE' GEN_DDS=/tmp/gen-dds-profile.sh \
        bash /tmp/bench-rover.sh" \
        || die "rover-side bring-up failed (see output above / /tmp/rover_launch.log on the bench)"
    did "rover.launch.py started on the bench"
}

# ================================================================== UI ======

start_ui() {
    head_ "UI"
    [ "$DO_UI" = 1 ] || { say "skipped (--no-ui)"; return 0; }
    [ -d "$REPO/ui/node_modules" ] || warn "ui/node_modules missing — run 'npm install' in ui/ first"

    if [ "$DRY" = 1 ]; then
        say "[dry] (cd $REPO/ui && npm run dev -- --port $UI_PORT) &"
        return 0
    fi
    if ss -ltn 2>/dev/null | grep -q ":$UI_PORT "; then
        ok "something already listening on :$UI_PORT — leaving it"
    else
        ( cd "$REPO/ui" && nohup npm run dev -- --port "$UI_PORT" >/tmp/bench-ui.log 2>&1 & )
        sleep 3
        ss -ltn 2>/dev/null | grep -q ":$UI_PORT " && did "UI dev server on :$UI_PORT (log: /tmp/bench-ui.log)" \
            || warn "UI did not come up on :$UI_PORT — see /tmp/bench-ui.log"
    fi
}

# ================================================================ VERIFY ====

verify() {
    head_ "Verify"
    [ "$DRY" = 1 ] && { say "[dry] skipped"; return 0; }

    printf '  %-30s ' "ping bench $BENCH_LINK_IP"
    ping_ok "$BENCH_LINK_IP" && echo OK || echo FAIL

    # Camera topics reaching the GS over DDS.
    local topics
    topics=$(docker exec indomitus_ground_station bash -lc \
        'source /opt/ros/humble/setup.bash; source /opt/ws/install/setup.bash 2>/dev/null; ros2 topic list 2>/dev/null' 2>/dev/null \
        | grep -E 'arducam|/camera/' )
    if [ -n "$topics" ]; then
        say "camera topics visible on GS:"
        printf '%s\n' "$topics" | sed 's/^/      /'
    else
        warn "no arducam/camera topics on the GS yet — give DDS a few seconds, or check /tmp/rover_launch.log on the bench"
    fi

    echo
    say "UI:            http://$GS_IP:$UI_PORT   (and http://localhost:$UI_PORT)"
    say "video server:  http://$GS_IP:8080"
    say "rosbridge:     ws://$GS_IP:9090"
    say "camera feeds are under /mast_arducam and /rear_arducam (native rover names)."
}

# ============================================================================

printf '\033[1mBench bring-up\033[0m  %s%s\n' \
    "$(date -Is)" "$([ "$DRY" = 1 ] && echo '  [DRY RUN]')"
say "bench=$BENCH_SSH  peer=$ROVER_PEER  domain=$ROS_DOMAIN_ID"
preflight
ensure_link
start_gs
start_rover
start_ui
verify

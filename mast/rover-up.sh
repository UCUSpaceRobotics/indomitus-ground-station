#!/bin/bash
# Bring the whole ROVER up in one command, from the GS PC.
#
#     ROVER_PW=... ./mast/rover-up.sh              # link + GS stack + rover + UI
#     ROVER_PW=... ./mast/rover-up.sh --dry        # print what would happen
#     ROVER_PW=... ./mast/rover-up.sh --skip-link  # link already up, do the rest
#     ./mast/rover-up.sh --no-ui
#     ./mast/rover-up.sh --ros-only                # only (re)start ROS
#     ./mast/rover-up.sh --help
#
# The rover twin of mast/bench-up.sh. Run this ON THE GS PC. It:
#   1. brings the rover Wi-Fi link up (mast/restore-link.sh, which runs from the
#      GS PC with ROVER_PW) if 10.42.0.1 is not reachable;
#   2. starts the GS console stack (docker-compose.gs.yaml) pointed at the rover
#      as its DDS peer (10.42.0.1 — the compose default);
#   3. SSHes to the rover, generates the rover Fast DDS profile, ensures the
#      arducams are wired, starts the ROS container and launches
#      rover_bringup rover.launch.py with video crossing the link;
#   4. serves the UI with `npm run dev` on :5173;
#   5. verifies the camera topics reach the GS.
#
# ASSUMPTIONS about the real rover — override with the flags/env if wrong:
#   * ROS runs in a container named by --container (default rover_dev), from a
#     rover-core checkout at --rover-dir (default below). If the rover runs ROS
#     bare-metal or under a different container, set --container '' and adjust.
#   * /dev/arducam-{mast,rear,container} already exist (the rover ships udev
#     rules — see src/rover_sensors/docs/arducam.md). rover-rover.sh only
#     rebuilds them if missing.
#   * The rover sudo user is indomitus-rover with password $ROVER_PW.
#
# Companion to mast/restore-link.sh (the link) and mast/rover-channel.sh
# (change channel + restart ROS). See mast/README.md and mast/STARTUP.md.
set -uo pipefail

# ================================================================ defaults ==

# --- rover reachability ----------------------------------------------------
ROVER_SSH=${ROVER_SSH:-indomitus-rover@10.42.0.1}       # over the Wi-Fi link
ROVER_LIFELINE=${ROVER_LIFELINE:-indomitus-rover@10.45.0.51}  # wired lifeline
ROVER_DIR=${ROVER_DIR:-/home/indomitus-rover/indomitus-rover-core}   # VERIFY on the rover
ROVER_CONTAINER=${ROVER_CONTAINER:-rover_dev}           # '' if ROS runs bare-metal
# Rover sudo password (indomitus-rover). Also consumed by restore-link.sh.
ROVER_PW=${ROVER_PW:-}

# --- the link --------------------------------------------------------------
ROVER_LINK_IP=${ROVER_LINK_IP:-10.42.0.1}               # rover AP address
ROVER_LINK_PREFIX=${ROVER_LINK_PREFIX:-10.42.0.}        # rover /24, DDS whitelist
GS_LINK_PREFIX=${GS_LINK_PREFIX:-10.44.0.}
GS_IP=${GS_IP:-10.44.0.10}                              # GS address the rover peers back to
GS_VIA=${GS_VIA:-10.44.0.1}
ROVER_NET=${ROVER_NET:-10.42.0.0/24}

# --- ROS -------------------------------------------------------------------
ROS_DOMAIN_ID=${ROS_DOMAIN_ID:-90}
RMW=${RMW:-rmw_fastrtps_cpp}
ROVER_PEER=${ROVER_PEER:-$ROVER_LINK_IP}                # GS discovers the rover here
ZED_MODE=${ZED_MODE:-rgb}                               # rover HAS a ZED; 'rgb' by default
ROVER_DDS_PROFILE=${ROVER_DDS_PROFILE:-/work/docker/fastdds_rover_link.xml}

# --- UI --------------------------------------------------------------------
UI_PORT=${UI_PORT:-5173}

DRY=0
DO_LINK=1
DO_GS=1
DO_ROVER=1
DO_UI=1

usage() {
    cat <<EOF
Bring the whole ROVER up from the GS PC: link, GS stack, rover, UI.
Idempotent; see mast/README.md and mast/STARTUP.md.

  ROVER_PW=... ./mast/rover-up.sh          # everything
  ROVER_PW=... ./mast/rover-up.sh --dry    # change nothing, print the plan
  ROVER_PW=... ./mast/rover-up.sh --skip-link
  ./mast/rover-up.sh --ros-only            # only (re)start GS + rover ROS
  ./mast/rover-up.sh --no-ui

Options
  --rover-ssh U@H     rover over Wi-Fi         (default: $ROVER_SSH)
  --lifeline U@H      rover wired lifeline     (default: $ROVER_LIFELINE)
  --rover-dir PATH    rover-core repo on rover (default: $ROVER_DIR)
  --container NAME    ROS container ('' = bare-metal) (default: $ROVER_CONTAINER)
  --domain N          ROS_DOMAIN_ID            (default: $ROS_DOMAIN_ID)
  --peer IP           DDS peer (the rover)     (default: $ROVER_PEER)
  --zed-mode ''|rgb|nav  ZED2i mode            (default: $ZED_MODE)
  --ui-port N         vite dev server port     (default: $UI_PORT)
  --skip-link         do not touch the link
  --ros-only          only restart GS + rover ROS (implies --skip-link --no-ui)
  --no-link | --no-gs | --no-rover | --no-ui
  --dry               print what would change, change nothing
  -h, --help

Environment: ROVER_PW (rover sudo, required to bring the link up), plus every
default above as an env var of the same name. Flags win over env.
EOF
    exit 0
}

while [ $# -gt 0 ]; do
    case "$1" in
        --rover-ssh) ROVER_SSH=$2; shift 2 ;;
        --lifeline)  ROVER_LIFELINE=$2; shift 2 ;;
        --rover-dir) ROVER_DIR=$2; shift 2 ;;
        --container) ROVER_CONTAINER=$2; shift 2 ;;
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

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
GS_COMPOSE="$REPO/docker/docker-compose.gs.yaml"

say()   { printf '  %s\n' "$*"; }
head_() { printf '\n\033[1m%s\033[0m\n' "$1"; }
did()   { printf '  \033[32m%s\033[0m %s\n' "changed" "$1"; }
ok()    { printf '  \033[2mok\033[0m %s\n' "$1"; }
warn()  { printf '  \033[33mWARN\033[0m  %s\n' "$1"; }
die()   { printf '  \033[31mFAIL\033[0m  %s\n' "$1"; exit 1; }

SSH="ssh -o BatchMode=yes -o StrictHostKeyChecking=accept-new -o ConnectTimeout=8"
ping_ok() { ping -c1 -W2 "$1" >/dev/null 2>&1; }

# Reach the rover over Wi-Fi, falling back to the wired lifeline.
ROVER=""
locate_rover() {
    local t
    for t in "$ROVER_SSH" "$ROVER_LIFELINE"; do
        $SSH "$t" true 2>/dev/null && { ROVER=$t; return 0; }
    done
    return 1
}

# Prime the rover sudo timestamp once, then run a privileged remote command.
rsudo_prime() { $SSH "$ROVER" "printf '%s\n' '$ROVER_PW' | sudo -S -p '' -v"; }

need_rover_pw() {
    [ -n "$ROVER_PW" ] && return 0
    if [ -t 0 ]; then
        read -rsp "  rover sudo password (ROVER_PW) for ${ROVER_SSH#*@}: " ROVER_PW; echo
    else
        die "ROVER_PW is unset and no TTY to prompt on. Export it or run interactively."
    fi
}

# ============================================================= preflight ====

preflight() {
    head_ "Preflight (GS PC)"
    command -v docker >/dev/null || die "docker not installed"
    docker compose version >/dev/null 2>&1 || die "docker compose v2 not available"
    [ -f "$GS_COMPOSE" ] || die "missing $GS_COMPOSE"
    [ -x "$REPO/mast/restore-link.sh" ] || die "missing mast/restore-link.sh"
    ok "docker + compose + restore-link.sh"
}

# ================================================================== LINK ====

ensure_link() {
    head_ "Rover link"
    [ "$DO_LINK" = 1 ] || { say "skipped (--skip-link)"; return 0; }

    if ping_ok "$ROVER_LINK_IP"; then
        ok "rover reachable at $ROVER_LINK_IP — link already up"
        return 0
    fi

    say "rover $ROVER_LINK_IP unreachable — restoring the link (mast/restore-link.sh)"
    if [ "$DRY" = 1 ]; then
        say "[dry] ROVER_PW=*** ./mast/restore-link.sh   (rover + Pi + GS route)"
        say "[dry] then wait for $ROVER_LINK_IP"
        return 0
    fi
    need_rover_pw
    # restore-link.sh runs FROM the GS PC and handles rover, Pi, and the GS
    # route itself (unlike the bench, which is configured on the bench).
    ( cd "$REPO" && ROVER_PW="$ROVER_PW" ./mast/restore-link.sh ) \
        || warn "restore-link.sh returned non-zero — check its output above"
    did "ran restore-link.sh"

    say "waiting for $ROVER_LINK_IP ..."
    local i
    for i in $(seq 1 20); do
        ping_ok "$ROVER_LINK_IP" && { ok "rover up at $ROVER_LINK_IP"; return 0; }
        sleep 3
    done
    die "rover still unreachable at $ROVER_LINK_IP after restore-link.sh"
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
    ( cd "$REPO" && ROVER_PEER="$ROVER_PEER" ROS_DOMAIN_ID="$ROS_DOMAIN_ID" \
        docker compose -f "$GS_COMPOSE" --project-directory "$REPO" up -d ) \
        || die "docker compose up failed"
    did "GS stack up (rosbridge :9090, web_video_server :8080)"
}

# ================================================================ ROVER =====

start_rover() {
    head_ "Rover side"
    [ "$DO_ROVER" = 1 ] || { say "skipped (--no-rover)"; return 0; }

    if [ "$DRY" = 1 ]; then
        say "[dry] scp mast/rover-rover.sh + docker/gen-dds-profile.sh -> rover:/tmp/"
        say "[dry] on the rover: rover-rover.sh (DDS profile, arducam check, container, rover.launch.py)"
        return 0
    fi

    locate_rover || die "rover unreachable over $ROVER_SSH or $ROVER_LIFELINE (link up? try without --skip-link)"
    say "via ${ROVER#*@}"
    need_rover_pw

    scp -o BatchMode=yes -o StrictHostKeyChecking=accept-new -o ConnectTimeout=8 \
        "$REPO/mast/rover-rover.sh" "$REPO/docker/gen-dds-profile.sh" \
        "$ROVER:/tmp/" >/dev/null \
        || die "scp of rover-rover.sh / gen-dds-profile.sh to the rover failed"

    $SSH "$ROVER" "printf '%s\n' '$ROVER_PW' | sudo -S -p '' -v && \
        ROVER_DIR='$ROVER_DIR' CONTAINER='$ROVER_CONTAINER' DOMAIN=$ROS_DOMAIN_ID RMW=$RMW \
        PROFILE='$ROVER_DDS_PROFILE' LINK_PREFIX='$ROVER_LINK_PREFIX' GS_IP='$GS_IP' \
        ZED_MODE='$ZED_MODE' GEN_DDS=/tmp/gen-dds-profile.sh \
        bash /tmp/rover-rover.sh" \
        || die "rover-side bring-up failed (see output above / /tmp/rover_launch.log on the rover)"
    did "rover.launch.py started on the rover"
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
        ( cd "$REPO/ui" && nohup npm run dev -- --port "$UI_PORT" >/tmp/rover-ui.log 2>&1 & )
        sleep 3
        ss -ltn 2>/dev/null | grep -q ":$UI_PORT " && did "UI dev server on :$UI_PORT (log: /tmp/rover-ui.log)" \
            || warn "UI did not come up on :$UI_PORT — see /tmp/rover-ui.log"
    fi
}

# ================================================================ VERIFY ====

verify() {
    head_ "Verify"
    [ "$DRY" = 1 ] && { say "[dry] skipped"; return 0; }

    printf '  %-30s ' "ping rover $ROVER_LINK_IP"
    ping_ok "$ROVER_LINK_IP" && echo OK || echo FAIL

    local topics
    topics=$(docker exec indomitus_ground_station bash -lc \
        'source /opt/ros/humble/setup.bash; source /opt/ws/install/setup.bash 2>/dev/null; ros2 topic list 2>/dev/null' 2>/dev/null \
        | grep -E 'arducam|/camera/|zed' )
    if [ -n "$topics" ]; then
        say "camera topics visible on GS:"
        printf '%s\n' "$topics" | sed 's/^/      /'
    else
        warn "no camera topics on the GS yet — give DDS a few seconds, or check /tmp/rover_launch.log on the rover"
    fi

    echo
    say "UI:            http://$GS_IP:$UI_PORT   (and http://localhost:$UI_PORT)"
    say "video server:  http://$GS_IP:8080"
    say "rosbridge:     ws://$GS_IP:9090"
    say "camera feeds are under /mast_arducam, /rear_arducam, /container_arducam (native rover names)."
}

# ============================================================================

printf '\033[1mRover bring-up\033[0m  %s%s\n' \
    "$(date -Is)" "$([ "$DRY" = 1 ] && echo '  [DRY RUN]')"
say "rover=$ROVER_SSH  peer=$ROVER_PEER  domain=$ROS_DOMAIN_ID"
preflight
ensure_link
start_gs
start_rover
start_ui
verify

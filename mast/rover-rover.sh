#!/bin/bash
# Rover-side bring-up, run ON THE ROVER by mast/rover-up.sh.
#
# The rover twin of mast/bench-rover.sh. rover-up.sh scp's this and
# gen-dds-profile.sh to the rover and invokes it with the environment below.
#
# Does, in order:
#   1. generates the rover Fast DDS profile (whitelist the rover link address,
#      peer the GS PC) via gen-dds-profile.sh;
#   2. starts the ROS container (or runs bare-metal if CONTAINER=''), stops a
#      running rover.launch.py, builds the workspace;
#   3. launches rover_bringup rover.launch.py in tmux session $SESSION inside the
#      container — the same session rover-launch-core.service uses, so it can
#      always be reached: indomitus-rover-core scripts/enter_container.sh rover core.
#      That launch also starts rover_cameras (MJPEG for the GS UI).
#
# Needs sudo (profile dir). rover-up.sh primes the timestamp.
set -uo pipefail

# ---- parameters (env, with defaults) --------------------------------------
ROVER_DIR=${ROVER_DIR:-/home/indomitus-rover/indomitus-rover-core}
CONTAINER=${CONTAINER:-rover_prod}                # '' = run ROS on the host
SESSION=${SESSION:-core}                          # tmux session the launch runs in
DOMAIN=${DOMAIN:-90}
RMW=${RMW:-rmw_fastrtps_cpp}
PROFILE=${PROFILE:-/tmp/fastdds_rover_link.xml}   # where the launch reads it (docker cp'd into the container)
LINK_PREFIX=${LINK_PREFIX:-10.42.0.}
GS_IP=${GS_IP:-10.44.0.10}
ZED_MODE=${ZED_MODE:-rgb}
GEN_DDS=${GEN_DDS:-/tmp/gen-dds-profile.sh}

say() { printf '  %s\n' "$*"; }
die() { printf '  FAIL %s\n' "$*"; exit 1; }

# Run a ROS command either inside the container or on the host.
rexec()  { if [ -n "$CONTAINER" ]; then docker exec "$CONTAINER" bash -lc "$1"; else bash -lc "$1"; fi; }

# ============================================================== DDS profile ==
gen_profile() {
    say "Fast DDS profile (domain $DOMAIN, peer $GS_IP, whitelist $LINK_PREFIX*)"
    [ -x "$GEN_DDS" ] || die "generator $GEN_DDS not found (rover-up.sh should have copied it)"
    sudo install -d "$ROVER_DIR/docker"
    # Clear a stale root-owned profile (e.g. from a prior in-container run) so
    # the generation below, running as the login user, can write it fresh.
    sudo rm -f "$ROVER_DIR/docker/fastdds_rover_link.xml" "$ROVER_DIR/docker/.gen-dds-profile.sh"
    cp "$GEN_DDS" "$ROVER_DIR/docker/.gen-dds-profile.sh"
    ( cd "$ROVER_DIR/docker" \
      && ROVER_LINK_PREFIX="$LINK_PREFIX" ROVER_PEER="$GS_IP" ROS_DOMAIN_ID="$DOMAIN" \
         bash .gen-dds-profile.sh ) \
        || die "DDS profile generation failed"
    rm -f "$ROVER_DIR/docker/.gen-dds-profile.sh"
    say "  wrote $ROVER_DIR/docker/fastdds_rover_link.xml"
}

# The container does not mount $ROVER_DIR/docker, so the profile is copied in
# (after the container is up). Bare-metal reads it from PROFILE directly.
install_profile() {
    local src="$ROVER_DIR/docker/fastdds_rover_link.xml"
    if [ -n "$CONTAINER" ]; then
        docker cp "$src" "$CONTAINER:$PROFILE" >/dev/null || die "docker cp of the DDS profile failed"
    else
        [ "$src" = "$PROFILE" ] || cp "$src" "$PROFILE" || die "copy of the DDS profile to $PROFILE failed"
    fi
    say "  DDS profile at $PROFILE"
}

# ================================================================ container ==
start_container() {
    [ -n "$CONTAINER" ] || { say "ROS on host (CONTAINER='')"; return 0; }
    say "ROS container: $CONTAINER"
    cd "$ROVER_DIR"
    if [ "$(docker inspect -f '{{.State.Running}}' "$CONTAINER" 2>/dev/null)" != "true" ]; then
        docker start "$CONTAINER" >/dev/null 2>&1 || docker compose up -d >/dev/null 2>&1
    fi
    docker ps --format '{{.Names}} {{.Status}}' | grep -q "$CONTAINER" \
        || die "$CONTAINER is not running"
    say "  $(docker ps --format '{{.Names}} {{.Status}}' | grep "$CONTAINER")"
}

build_ws() {
    say "build workspace (rover_bringup, rover_sensors)"
    rexec 'source /opt/ros/humble/setup.bash; cd /opt/ws && colcon build --symlink-install --packages-up-to rover_bringup rover_sensors 2>&1 | tail -3' \
        | sed 's/^/  /'
}

# rover.launch.py runs in tmux session $SESSION. Ctrl-C it there (the way an
# operator would); anything still running after that — e.g. a launch started
# outside tmux by an older version of this script — is killed.
# [r]over_bringup: keeps pgrep/pkill from matching the bash -lc running them.
stop_rover() {
    say "stop rover.launch.py (tmux session '$SESSION')"
    rexec "tmux has-session -t '$SESSION' 2>/dev/null && tmux send-keys -t '$SESSION' C-c; true"
    local i
    for i in $(seq 1 15); do
        rexec 'pgrep -f "[r]over_bringup rover.launch.py" >/dev/null' || return 0
        sleep 1
    done
    say "  still running after Ctrl-C — killing it"
    rexec 'pkill -f "[r]over_bringup rover.launch.py"; sleep 2; true'
}

launch_rover() {
    say "launch rover.launch.py in tmux session '$SESSION'  (zed2i_mode='$ZED_MODE')"
    rexec "tmux has-session -t '$SESSION' 2>/dev/null || tmux new-session -d -s '$SESSION'" \
        || die "cannot create tmux session '$SESSION' (is tmux in the image?)"
    # Env on the command, not exported: the session shell stays as it was.
    local cmd="ROS_DOMAIN_ID=$DOMAIN RMW_IMPLEMENTATION=$RMW FASTRTPS_DEFAULT_PROFILES_FILE=$PROFILE ROS_LOCALHOST_ONLY=0 ros2 launch rover_bringup rover.launch.py zed2i_mode:='$ZED_MODE'"
    if [ -n "$CONTAINER" ]; then
        docker exec "$CONTAINER" tmux send-keys -t "$SESSION" "$cmd" Enter
    else
        tmux send-keys -t "$SESSION" "$cmd" Enter
    fi || die "tmux send-keys to '$SESSION' failed"
    sleep 8
    say "  --- tmux session '$SESSION' (tail) ---"
    rexec "tmux capture-pane -p -t '$SESSION' -S -300" \
        | grep -iE "camera|zed|error|started" | tail -14 | sed 's/^/  /'
    say "  attach: scripts/enter_container.sh rover $SESSION  (in indomitus-rover-core)"
}

# ============================================================================
gen_profile
start_container
install_profile
stop_rover
build_ws
launch_rover
say "rover side up."

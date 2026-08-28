#!/bin/bash
# Rover-side bring-up, run ON THE ROVER by mast/rover-up.sh.
#
# The rover twin of mast/bench-rover.sh. rover-up.sh scp's this and
# gen-dds-profile.sh to the rover and invokes it with the environment below.
#
# Does, in order:
#   1. checks the arducam udev symlinks the rover already ships
#      (/dev/arducam-{mast,rear,container}); only tries to rebuild them if they
#      are missing — on the real rover they are persistent, keyed to fixed hub
#      ports (src/rover_sensors/docs/arducam.md), and must not be second-guessed;
#   2. generates the rover Fast DDS profile (whitelist the rover link address,
#      peer the GS PC) via gen-dds-profile.sh;
#   3. starts the ROS container (or runs bare-metal if CONTAINER=''), builds the
#      workspace, and launches rover_bringup rover.launch.py.
#
# Needs sudo (udev, if a rebuild is needed). rover-up.sh primes the timestamp.
set -uo pipefail

# ---- parameters (env, with defaults) --------------------------------------
ROVER_DIR=${ROVER_DIR:-/home/indomitus-rover/indomitus-rover-core}
CONTAINER=${CONTAINER:-rover_dev}                 # '' = run ROS on the host
DOMAIN=${DOMAIN:-90}
RMW=${RMW:-rmw_fastrtps_cpp}
PROFILE=${PROFILE:-/work/docker/fastdds_rover_link.xml}   # container path (or host path if bare-metal)
LINK_PREFIX=${LINK_PREFIX:-10.42.0.}
GS_IP=${GS_IP:-10.44.0.10}
ZED_MODE=${ZED_MODE:-rgb}
GEN_DDS=${GEN_DDS:-/tmp/gen-dds-profile.sh}

say() { printf '  %s\n' "$*"; }
die() { printf '  FAIL %s\n' "$*"; exit 1; }

# Run a ROS command either inside the container or on the host.
rexec()  { if [ -n "$CONTAINER" ]; then docker exec "$CONTAINER" bash -lc "$1"; else bash -lc "$1"; fi; }
rexecd() { if [ -n "$CONTAINER" ]; then docker exec -d "$CONTAINER" bash -lc "$1"; else nohup bash -lc "$1" >/dev/null 2>&1 & fi; }

# ============================================================ arducam check ==
check_arducams() {
    say "arducam devices"
    local missing=0 s
    for s in mast rear; do
        if [ -e "/dev/arducam-$s" ]; then
            say "  /dev/arducam-$s -> $(readlink -f /dev/arducam-$s)"
        else
            say "  /dev/arducam-$s MISSING"; missing=1
        fi
    done
    [ -e /dev/arducam-container ] && say "  /dev/arducam-container -> $(readlink -f /dev/arducam-container)"

    if [ "$missing" = 1 ]; then
        say "  one or more symlinks missing — the rover normally ships"
        say "  /etc/udev/rules.d/99-arducam.rules keyed to fixed hub ports."
        say "  Rebuild it per src/rover_sensors/docs/arducam.md (do NOT guess ports here)."
    fi
}

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

launch_rover() {
    say "launch rover.launch.py  (zed2i_mode='$ZED_MODE')"
    rexec 'pkill -f "rover_bringup rover.launch.py" 2>/dev/null; pkill -f v4l2_camera_node 2>/dev/null; sleep 2; true'
    rexecd "source /opt/ros/humble/setup.bash; source /opt/ws/install/setup.bash; \
        export ROS_DOMAIN_ID=$DOMAIN RMW_IMPLEMENTATION=$RMW FASTRTPS_DEFAULT_PROFILES_FILE=$PROFILE ROS_LOCALHOST_ONLY=0; \
        exec ros2 launch rover_bringup rover.launch.py zed2i_mode:='$ZED_MODE' > /tmp/rover_launch.log 2>&1"
    sleep 8
    say "  --- /tmp/rover_launch.log (tail) ---"
    rexec 'grep -iE "arducam|camera|zed|error|started" /tmp/rover_launch.log 2>/dev/null | tail -14' \
        | sed 's/^/  /'
}

# ============================================================================
check_arducams
gen_profile
start_container
build_ws
launch_rover
say "rover side up."

#!/bin/bash
# Rover-side bring-up, run ON THE BENCH laptop by mast/bench-up.sh.
#
# Not meant to be run by hand normally — bench-up.sh scp's this and
# gen-dds-profile.sh to the bench and invokes it with the environment below.
# It is a standalone script (not an inline heredoc) so its quoting is sane and
# it can be read and debugged on the bench like any other file.
#
# Does, in order:
#   1. wires the two Arducam capture nodes to /dev/arducam-{mast,rear} by USB
#      port (the same paths/names the real rover uses);
#   2. generates the bench Fast DDS profile (whitelist the bench link address,
#      peer the GS PC) via gen-dds-profile.sh;
#   3. starts rover_dev, builds the workspace, and launches JUST the two
#      arducams with the bench DDS environment. Full rover.launch.py is not
#      bench-viable — see launch_cameras() for why.
#
# Needs sudo (udev rules, /dev symlinks). bench-up.sh primes the sudo timestamp
# before calling this, so the sudo calls here do not re-prompt.
set -uo pipefail

# ---- parameters (env, with defaults) --------------------------------------
ROVER_DIR=${ROVER_DIR:-/home/starezax/Desktop/indomitus/indomitus-rover-core}
DOMAIN=${DOMAIN:-90}
RMW=${RMW:-rmw_fastrtps_cpp}
PROFILE=${PROFILE:-/work/docker/fastdds_rover_link.xml}   # path INSIDE the container
LINK_PREFIX=${LINK_PREFIX:-10.43.0.}                       # bench /24, DDS whitelist
GS_IP=${GS_IP:-10.44.0.10}                                 # DDS peer (the GS PC)
ZED_MODE=${ZED_MODE:-}                                     # '' = no ZED on the bench
GEN_DDS=${GEN_DDS:-/tmp/gen-dds-profile.sh}                # copied here by bench-up.sh

say() { printf '  %s\n' "$*"; }
die() { printf '  FAIL %s\n' "$*"; exit 1; }

# ============================================================ arducam udev ==
# All B0495 units share a serial, so we key on the USB port (KERNELS), exactly
# as src/rover_sensors/docs/arducam.md prescribes. The first capture node found
# becomes mast, the second rear. Re-runnable: rewrites the rule file each time.
map_arducams() {
    say "arducam udev symlinks"
    local n base idx port role i=0 rules=""
    for n in /dev/video*; do
        [ -e "$n" ] || continue
        base=$(basename "$n")
        # Card name check: only the Arducam nodes, not the built-in webcam.
        v4l2-ctl -d "$n" --info 2>/dev/null | grep -qi "Arducam" || continue
        # index 0 is the capture node; index 1 is metadata and must not be used.
        idx=$(cat "/sys/class/video4linux/$base/index" 2>/dev/null)
        [ "$idx" = "0" ] || continue
        # sysfs path .../usbX/X-P/X-P:1.0/... — KERNELS is the 'X-P' before :1.0
        port=$(udevadm info -q path -n "$n" 2>/dev/null \
               | grep -oE '[0-9]+-[0-9.]+:1\.0' | head -1 | sed 's/:1\.0//')
        if [ -z "$port" ]; then say "  $n: could not derive USB port, skipping"; continue; fi
        case $i in 0) role=mast ;; 1) role=rear ;; *) role=cam$i ;; esac
        rules+="SUBSYSTEM==\"video4linux\", KERNELS==\"$port\", ATTR{index}==\"0\", MODE:=\"0666\", SYMLINK+=\"arducam-$role\"
"
        say "  $n -> /dev/arducam-$role  (KERNELS=$port)"
        i=$((i+1))
    done
    [ "$i" -ge 1 ] || { say "  no Arducam capture nodes found — is a camera plugged in?"; return 1; }
    printf '%s' "$rules" | sudo tee /etc/udev/rules.d/99-arducam.rules >/dev/null
    sudo udevadm control --reload-rules && sudo udevadm trigger
    sleep 2
    ls -l /dev/arducam-* 2>/dev/null | sed 's/^/  /' || true
}

# ============================================================== DDS profile ==
gen_profile() {
    say "Fast DDS profile (domain $DOMAIN, peer $GS_IP, whitelist $LINK_PREFIX*)"
    [ -x "$GEN_DDS" ] || die "generator $GEN_DDS not found (bench-up.sh should have copied it)"
    sudo install -d "$ROVER_DIR/docker"
    # A previous run inside the container generated this as root; the user then
    # cannot overwrite it. Clear the stale root-owned files so the generation
    # below (running as the login user) can write fresh, user-owned ones.
    sudo rm -f "$ROVER_DIR/docker/fastdds_rover_link.xml" "$ROVER_DIR/docker/.gen-dds-profile.sh"
    # gen-dds-profile.sh writes fastdds_rover_link.xml next to itself, so run a
    # copy placed in the repo docker dir. That path is what PROFILE points at
    # inside the container (repo is mounted at /work).
    cp "$GEN_DDS" "$ROVER_DIR/docker/.gen-dds-profile.sh"
    ( cd "$ROVER_DIR/docker" \
      && ROVER_LINK_PREFIX="$LINK_PREFIX" ROVER_PEER="$GS_IP" ROS_DOMAIN_ID="$DOMAIN" \
         bash .gen-dds-profile.sh ) \
        || die "DDS profile generation failed"
    rm -f "$ROVER_DIR/docker/.gen-dds-profile.sh"
    say "  wrote $ROVER_DIR/docker/fastdds_rover_link.xml"
}

# ================================================================ rover_dev ==
start_container() {
    say "rover_dev container"
    cd "$ROVER_DIR"
    if [ "$(docker inspect -f '{{.State.Running}}' rover_dev 2>/dev/null)" != "true" ]; then
        docker start rover_dev >/dev/null 2>&1 || docker compose up -d >/dev/null 2>&1
    fi
    docker ps --format '{{.Names}} {{.Status}}' | grep -q rover_dev \
        || die "rover_dev is not running"
    say "  $(docker ps --format '{{.Names}} {{.Status}}' | grep rover_dev)"
}

build_ws() {
    say "build workspace (rover_bringup, rover_sensors)"
    docker exec rover_dev bash -lc \
        'source /opt/ros/humble/setup.bash; cd /opt/ws && colcon build --symlink-install --packages-up-to rover_bringup rover_sensors 2>&1 | tail -3' \
        | sed 's/^/  /'
}

launch_cameras() {
    say "launch the two arducams (mast_arducam, rear_arducam)"
    # rover.launch.py is NOT bench-viable: it hard-aborts on missing rover
    # hardware (can.launch.py raises "Interface 'can0' does not exist"), and its
    # ZED cannot be skipped from the CLI — ros2launch rejects an empty value
    # (`zed2i_mode:=`, api.py:132) and zed_wrapper is not built here. So the
    # bench launches just the cameras, with the SAME namespaces/frames the real
    # rover.launch.py gives them (mast_arducam, rear_arducam). The real rover
    # runs the full bringup — see mast/rover-rover.sh.
    docker exec rover_dev bash -lc \
        'pkill -f arducam 2>/dev/null; pkill -f v4l2_camera 2>/dev/null; pkill -f cameras.launch 2>/dev/null; sleep 2; true'
    # A wrapper launch that includes arducam.launch.py once per camera. Passing
    # the args programmatically (not via the CLI) keeps this uniform with how
    # the ZED skip has to be done, and lets one process own both cameras.
    docker exec -i rover_dev bash -c 'cat > /tmp/cameras.launch.py' <<'PYEOF'
import os
from launch import LaunchDescription
from launch.actions import IncludeLaunchDescription
from launch.launch_description_sources import PythonLaunchDescriptionSource
from ament_index_python.packages import get_package_share_directory

def generate_launch_description():
    ad = os.path.join(get_package_share_directory("rover_sensors"), "launch", "arducam.launch.py")
    def cam(n):
        return IncludeLaunchDescription(
            PythonLaunchDescriptionSource(ad),
            launch_arguments={
                "camera_name": n,
                "camera_path": f"/dev/arducam-{n}",
                "namespace": f"{n}_arducam",
                "camera_frame_id": f"{n}_arducam_optical_frame",
            }.items())
    return LaunchDescription([cam("mast"), cam("rear")])
PYEOF
    docker exec -d rover_dev bash -lc "source /opt/ros/humble/setup.bash; source /opt/ws/install/setup.bash; \
        export ROS_DOMAIN_ID=$DOMAIN RMW_IMPLEMENTATION=$RMW FASTRTPS_DEFAULT_PROFILES_FILE=$PROFILE ROS_LOCALHOST_ONLY=0; \
        exec ros2 launch /tmp/cameras.launch.py > /tmp/rover_launch.log 2>&1"
    sleep 12
    say "  --- /tmp/rover_launch.log (tail) ---"
    docker exec rover_dev bash -lc 'grep -iE "arducam|Starting camera|error|Statically|not found" /tmp/rover_launch.log | tail -12' \
        | sed 's/^/  /'
}

# ============================================================================
map_arducams || say "  (arducam mapping incomplete — cameras may not appear)"
gen_profile
start_container
build_ws
launch_cameras
say "rover side up."

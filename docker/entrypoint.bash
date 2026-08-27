#!/usr/bin/bash

set -e

source /opt/ros/${ROS_DISTRO}/setup.bash

# Regenerate the Fast DDS profile on every start, so it can never describe a
# topology that is no longer plugged in. The generator picks 'linked' mode when
# an address in the rover link subnet exists and 'local' mode when it does not;
# a profile left over from the other mode is the difference between a working
# `ros2 topic list` and one that returns nothing but /parameter_events and
# /rosout. Needs the host network namespace to see the real interfaces, which
# is how the ground station container runs.
#
# Only meaningful when the profile this container was pointed at is the
# generated one. Then it is also mandatory: the file is not in git, so if it is
# not written here it does not exist, and a FASTRTPS_DEFAULT_PROFILES_FILE
# pointing at a missing file does not stop Fast DDS - it logs one XMLPARSER
# line and falls back to plain multicast, which cannot reach a rover behind the
# mast Pi. Every symptom then points at the network instead of at this file, so
# fail here where the cause is still visible.
if [ "${FASTRTPS_DEFAULT_PROFILES_FILE:-}" = /work/docker/fastdds_rover_link.xml ]; then
    if [ ! -x /work/docker/gen-dds-profile.sh ]; then
        echo "error: /work/docker/gen-dds-profile.sh is missing or not executable;" >&2
        echo "       is the repository mounted at /work?" >&2
        exit 1
    fi
    if [ ! -w /work/docker ]; then
        echo "error: /work/docker is not writable, so the DDS profile cannot be" >&2
        echo "       generated. Mount the repository read-write." >&2
        exit 1
    fi
    if ! /work/docker/gen-dds-profile.sh; then
        echo "error: could not generate the DDS profile; refusing to start with" >&2
        echo "       discovery that cannot reach the rover." >&2
        exit 1
    fi
fi

if [ -d /opt/ws/src ] && [ "$(ls -A  /opt/ws/src 2> /dev/null)" ]; then
    if [ ! -f /opt/ws/install/setup.bash ] || find /opt/ws/src -type f -newer /opt/ws/install/setup.bash -print -quit | grep -q .; then
        echo "Building workspace..."

        rosdep install --from-paths src  --ignore-src -r -y || true

        colcon build --symlink-install
    fi
fi

if [ -f /opt/ws/install/setup.bash ]; then
    source /opt/ws/install/setup.bash
fi

echo "ROS ${ROS_DISTRO} ready. Workspace: /opt/ws"

exec "$@"

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
# generated one, and only possible when /work is mounted writable.
if [ -x /work/docker/gen-dds-profile.sh ] \
   && [ "${FASTRTPS_DEFAULT_PROFILES_FILE:-}" = /work/docker/fastdds_rover_link.xml ] \
   && [ -w /work/docker ]; then
    /work/docker/gen-dds-profile.sh || \
        echo "warning: could not regenerate the DDS profile; using whatever is on disk" >&2
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

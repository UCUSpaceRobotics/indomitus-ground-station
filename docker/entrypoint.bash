#!/usr/bin/bash

set -e

source /opt/ros/${ROS_DISTRO}/setup.bash

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

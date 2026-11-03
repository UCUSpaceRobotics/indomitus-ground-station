ARG ROS_DISTRO=humble

# Node for the browser UI (ui/). Jammy's own nodejs is v12; vite needs >= 20.19.
# The official binary is glibc-linked and runs on the ROS image as is.
FROM node:22-bookworm-slim AS node

FROM ros:${ROS_DISTRO}-ros-base

RUN apt-get update && apt-get install -y --no-install-recommends \
    locales \
    git curl wget ca-certificates \
    build-essential python3-pip \
    python3-colcon-common-extensions \
    python3-rosdep \
    python3-serial \
    iputils-ping iproute2 pkg-config \
    ros-${ROS_DISTRO}-ros2launch \
    ros-${ROS_DISTRO}-teleop-twist-joy \
    ros-${ROS_DISTRO}-rosbridge-suite \
    ros-${ROS_DISTRO}-web-video-server \
    usbutils tmux \
    g++ make cmake \
    && rm -rf /var/lib/apt/lists/*

COPY --from=node /usr/local/bin/node /usr/local/bin/node
COPY --from=node /usr/local/lib/node_modules /usr/local/lib/node_modules
RUN ln -s ../lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm && \
    ln -s ../lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx

RUN locale-gen en_US.UTF-8

ENV LANG=en_US.UTF-8 LC_ALL=en_US.UTF-8


RUN rosdep init || true && rosdep update


ENV WS=/opt/ws
RUN mkdir -p ${WS}/src
WORKDIR ${WS}

ENV ROS_DISTRO=${ROS_DISTRO}

RUN echo "if [ -f /opt/ros/\${ROS_DISTRO}/setup.bash ]; then source /opt/ros/\${ROS_DISTRO}/setup.bash; fi" >> /root/.bashrc && \
    echo "if [ -f /usr/share/colcon_argcomplete/hook/colcon-argcomplete.bash ]; then source /usr/share/colcon_argcomplete/hook/colcon-argcomplete.bash; fi" >> /root/.bashrc && \
    echo "if [ -f ${WS}/install/setup.bash ]; then source ${WS}/install/setup.bash; fi" >> /root/.bashrc

COPY docker/tmux.conf /root/.tmux.conf
COPY docker/entrypoint.bash /entrypoint.bash
RUN chmod +x /entrypoint.bash
ENTRYPOINT ["/entrypoint.bash"]

CMD ["bash"]

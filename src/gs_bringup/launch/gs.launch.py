"""Everything the ground station needs to be useful, in one launch.

The operator console is four independent things that all have to be up before
the UI shows anything: the two serial boards (joy.launch.py), the mast link
(comms.launch.py), the websocket the browser talks ROS over (rosbridge), and
the MJPEG re-encoder the camera tiles read (web_video_server). Starting them by
hand in four terminals is how they end up half-started, which reads in the UI as
"the rover is not connected" no matter how healthy the rover actually is.

Each block can be switched off, because on a laptop that is only meant to watch
telemetry the serial boards are not plugged in and their nodes would spend the
session logging open() failures:

    ros2 launch gs_bringup gs.launch.py use_joy:=false

Discovery is NOT this file's job. The rover is reached over a routed link and
multicast SPDP does not cross the mast Pi, so the container points Fast DDS at
docker/fastdds_rover_link.xml, which docker/entrypoint.bash regenerates at every
start. Launching this without that profile gives an empty `ros2 node list`.
"""

import os

from ament_index_python.packages import get_package_share_directory
from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument, GroupAction, IncludeLaunchDescription
from launch.conditions import IfCondition
from launch.launch_description_sources import PythonLaunchDescriptionSource
from launch.substitutions import EnvironmentVariable, LaunchConfiguration
from launch_ros.actions import Node, PushRosNamespace


def generate_launch_description():
    use_joy = LaunchConfiguration('use_joy')
    use_comms = LaunchConfiguration('use_comms')
    use_rosbridge = LaunchConfiguration('use_rosbridge')
    use_video = LaunchConfiguration('use_video')
    rosbridge_port = LaunchConfiguration('rosbridge_port')
    video_port = LaunchConfiguration('video_port')

    joy_launch = os.path.join(
        get_package_share_directory('gs_joy'), 'launch', 'joy.launch.py')
    comms_launch = os.path.join(
        get_package_share_directory('gs_comms'), 'launch', 'comms.launch.py')

    return LaunchDescription([
        DeclareLaunchArgument(
            'use_joy', default_value='true',
            description='Serial joystick + button boards and their interpreters'),
        DeclareLaunchArgument(
            'use_comms', default_value='true',
            description='Mast link monitor and LoRa gateway'),
        DeclareLaunchArgument(
            'use_rosbridge', default_value='true',
            description='Websocket + rosapi the browser UI connects to'),
        DeclareLaunchArgument(
            'use_video', default_value='true',
            description='web_video_server, the MJPEG source for the camera tiles'),

        # Defaults are what ui/src/config.js derives when nothing overrides it:
        # ws://<page host>:9090 and http://<page host>:8080. Changing a port
        # here means changing it in the UI settings dialog too.
        DeclareLaunchArgument(
            'rosbridge_port',
            default_value=EnvironmentVariable('ROSBRIDGE_PORT', default_value='9090'),
            description='Websocket port for the UI'),
        DeclareLaunchArgument(
            'video_port',
            default_value=EnvironmentVariable('VIDEO_SERVER_PORT', default_value='8080'),
            description='HTTP port web_video_server serves MJPEG on'),

        # Everything below is ground-station-owned, so it lives under /gs: it
        # keeps `ros2 node list`/`topic list` readable once the rover (and its
        # own un-namespaced nodes) is on the same graph.
        GroupAction([
            PushRosNamespace('gs'),

            IncludeLaunchDescription(
                PythonLaunchDescriptionSource(joy_launch),
                condition=IfCondition(use_joy),
            ),
            IncludeLaunchDescription(
                PythonLaunchDescriptionSource(comms_launch),
                condition=IfCondition(use_comms),
            ),

            # Bound to every interface on purpose: the console laptop serves the UI
            # and a second machine on the same switch opens it, which is how the
            # team actually operates. An empty address is rosbridge's own "all".
            Node(
                package='rosbridge_server',
                executable='rosbridge_websocket',
                name='rosbridge_websocket',
                condition=IfCondition(use_rosbridge),
                parameters=[{
                    'port': rosbridge_port,
                    'address': '',
                    # Camera frames over rosbridge are large; the stock 10 MB cap
                    # is what a full-resolution CompressedImage burst needs.
                    'max_message_size': 10000000,
                    # The UI calls /rover/drive/* and /rover/lights/* while streaming video.
                    # On the single-threaded default a service the rover is slow to
                    # answer stalls every subscription with it.
                    'call_services_in_new_thread': True,
                    # Empty globs are rosbridge's "expose everything"; the console
                    # is on a private link and the UI reads arbitrary topics.
                    'topics_glob': '',
                    'services_glob': '',
                    'params_glob': '',
                }],
                output='screen',
            ),
            # rosapi is a separate process from the websocket but not optional:
            # the UI enumerates topics and services through it, and without it the
            # panels come up blank even though the socket is connected.
            Node(
                package='rosapi',
                executable='rosapi_node',
                name='rosapi',
                condition=IfCondition(use_rosbridge),
                parameters=[{
                    'topics_glob': '',
                    'services_glob': '',
                    'params_glob': '',
                }],
                output='screen',
            ),

            Node(
                package='web_video_server',
                executable='web_video_server',
                name='web_video_server',
                condition=IfCondition(use_video),
                parameters=[{
                    'port': video_port,
                    'address': '0.0.0.0',
                    # Default is 'mjpeg'; 'ros_compressed' hands the browser the
                    # frames the rover already compressed instead of decoding and
                    # re-encoding them on the console CPU. The UI asks for the type
                    # per tile anyway, this only sets the fallback.
                    'default_stream_type': 'ros_compressed',
                }],
                output='screen',
            ),
        ]),
    ])

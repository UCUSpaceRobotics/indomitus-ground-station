from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument
from launch.substitutions import EnvironmentVariable, LaunchConfiguration
from launch_ros.actions import Node


def generate_launch_description():
    # Both mast services live on the Pi at this address; only the ports differ.
    # link_monitor.py serves Wi-Fi metrics on 4002, lora_bridge.py serves LoRa
    # metrics and accepts commands on 4001. See mast/README.md.
    mast_host_arg = DeclareLaunchArgument(
        'mast_host',
        default_value=EnvironmentVariable('MAST_HOST', default_value='10.44.0.1'),
        description='Mast Pi address; both TCP services bind it, not 0.0.0.0'
    )

    # These must match joy_to_cmd_vel_node's linear_scale/angular_scale. The
    # gateway sends percentages of full scale, so if the joy node's scaling
    # changes and these do not, the rover will act on the wrong magnitudes.
    max_linear_arg = DeclareLaunchArgument(
        'max_linear',
        default_value='0.5',
        description='Twist linear.x/y that corresponds to 100% over the radio'
    )

    max_angular_arg = DeclareLaunchArgument(
        'max_angular',
        default_value='1.0',
        description='Twist angular.z that corresponds to 100% over the radio'
    )

    mast_host = LaunchConfiguration('mast_host')

    return LaunchDescription([
        mast_host_arg,
        max_linear_arg,
        max_angular_arg,

        # Decides the path. Publishes /link/active_path, moves no traffic.
        Node(
            package='indomitus_rover_comms',
            executable='link_status_node',
            name='link_status_node',
            output='screen',
            parameters=[{
                'monitor_host': mast_host,
                'monitor_port': 4002,
            }],
        ),

        # Acts on the decision. Relays /cmd_vel_ext over the radio while the
        # path is LORA, and republishes the radio's own metrics.
        Node(
            package='indomitus_rover_comms',
            executable='lora_gateway_node',
            name='lora_gateway_node',
            output='screen',
            parameters=[{
                'bridge_host': mast_host,
                'bridge_port': 4001,
                'max_linear': LaunchConfiguration('max_linear'),
                'max_angular': LaunchConfiguration('max_angular'),
            }],
        ),
    ])

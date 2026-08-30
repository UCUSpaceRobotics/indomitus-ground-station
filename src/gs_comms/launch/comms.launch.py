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

    # The emergency link is a SECOND radio, nothing to do with the mast Pi:
    # this console's ESP32-S3 (e32-e-stop-gs) on USB, talking 430 MHz to the
    # rover's emergency-esp. It carries power on/off and the Jetson reset.
    #
    # gs_joy owns ttyACM0 (joysticks) and ttyACM1 (buttons), so this board
    # lands on ttyACM2 - but all three enumerate as the same anonymous 1a86
    # USB_Single_Serial bridge, so that only holds while they are plugged in
    # the same order. Pin it with a udev rule instead:
    #
    #   udevadm info -q property -n /dev/ttyACM2 | grep ID_SERIAL_SHORT
    #   SUBSYSTEM=="tty", ATTRS{idVendor}=="1a86", \
    #     ATTRS{serial}=="<that value>", SYMLINK+="gs-estop-esp"
    #
    # then launch with estop_port:=/dev/gs-estop-esp.
    estop_port_arg = DeclareLaunchArgument(
        'estop_port',
        default_value=EnvironmentVariable('ESTOP_BOARD_PORT',
                                          default_value='/dev/ttyACM2'),
        description='Serial port of the e-stop board (power cut + Jetson reset)'
    )

    # A console that only watches telemetry has no e-stop board plugged in.
    use_estop_board_arg = DeclareLaunchArgument(
        'use_estop_board',
        default_value='true',
        description='Open the e-stop board; false makes the power services refuse'
    )

    mast_host = LaunchConfiguration('mast_host')

    return LaunchDescription([
        mast_host_arg,
        max_linear_arg,
        max_angular_arg,
        estop_port_arg,
        use_estop_board_arg,

        # Decides the path. Publishes /link/active_path, moves no traffic.
        Node(
            package='gs_comms',
            executable='link_status_node',
            name='link_status_node',
            output='screen',
            parameters=[{
                'monitor_host': mast_host,
                'monitor_port': 4002,
            }],
        ),

        # Acts on the decision. Relays /cmd_vel_gs over the radio while the
        # path is LORA, and republishes the radio's own metrics. Also owns the
        # separate emergency radio: power/set_power and power/reboot_jetson.
        Node(
            package='gs_comms',
            executable='lora_gateway_node',
            name='lora_gateway_node',
            output='screen',
            parameters=[{
                'bridge_host': mast_host,
                'bridge_port': 4001,
                'max_linear': LaunchConfiguration('max_linear'),
                'max_angular': LaunchConfiguration('max_angular'),
                'use_estop_board': LaunchConfiguration('use_estop_board'),
                'estop_port': LaunchConfiguration('estop_port'),
                # Must match RADIO_BAUD in e32-e-stop-gs's firmware.
                'estop_baudrate': 115200,
            }],
        ),
    ])

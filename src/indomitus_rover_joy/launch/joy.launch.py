from launch import LaunchDescription
from launch_ros.actions import Node
from launch.actions import DeclareLaunchArgument
from launch.substitutions import LaunchConfiguration, EnvironmentVariable

def generate_launch_description():
    # esp32_switch+joy board: 3 joysticks *and* 9 switches, streamed at 50 Hz.
    # Its switches ride along in Joy.buttons, they are not on /switches.
    joy_board_port_arg = DeclareLaunchArgument(
        'joy_board_port',
        default_value=EnvironmentVariable('JOY_BOARD_PORT', default_value='/dev/ttyACM0'),
        description='Serial port of the joystick board (joysticks + its own 9 switches)'
    )

    # esp_32_switches+buttons board: 23 buttons, sent only on change.
    button_board_port_arg = DeclareLaunchArgument(
        'button_board_port',
        default_value=EnvironmentVariable('BUTTON_BOARD_PORT', default_value='/dev/ttyACM1'),
        description='Serial port of the button board (feeds /switches)'
    )

    # Survives restarts, so the sticks only get calibrated once.
    calibration_file_arg = DeclareLaunchArgument(
        'calibration_file',
        default_value=EnvironmentVariable(
            'JOY_CALIBRATION_FILE', default_value='/work/config/joy_calibration.yaml'),
        description='YAML file the calibration wizard saves to and the node loads at startup'
    )

    return LaunchDescription([
        joy_board_port_arg,
        button_board_port_arg,
        calibration_file_arg,
        Node(
            package='indomitus_rover_joy',
            executable='serial_joy_node',
            name='serial_joy_node',
            parameters=[{
                'port': LaunchConfiguration('joy_board_port'),
                'baudrate': 115200,
                # Defaults assume a perfectly centred 0..1000 stick; the
                # calibration wizard in the UI overwrites these at runtime and
                # persists them to calibration_file.
                'axis_min': [0.0, 0.0, 0.0, 0.0, 0.0, 0.0],
                'axis_center': [500.0, 500.0, 500.0, 500.0, 500.0, 500.0],
                'axis_max': [1000.0, 1000.0, 1000.0, 1000.0, 1000.0, 1000.0],
                'deadzone': 0.05,
                'axis_scale': [1.0, 1.0, 1.0, 1.0, 1.0, 1.0],
                'invert_switches': False,
                'calibration_file': LaunchConfiguration('calibration_file'),
            }],
            output='screen'
        ),
        Node(
            package='indomitus_rover_joy',
            executable='switch_reader_node',
            name='switch_reader_node',
            parameters=[{
                'port': LaunchConfiguration('button_board_port'),
                'baudrate': 115200,
                'num_switches': 23,
                'publish_rate': 10.0,
            }],
            output='screen'
        ),
        Node(
            package='indomitus_rover_joy',
            executable='joy_to_cmd_vel_node',
            name='joy_to_cmd_vel_node',
            parameters=[{
                # Same mapping as rover_teleop/config/joy.yaml on the rover:
                # J0Y forward/back, J0X strafe, J1X yaw.
                'linear_x_axis': 1,
                'linear_y_axis': 0,
                'angular_z_axis': 2,
                'linear_x_scale': 0.5,
                'linear_y_scale': 0.5,
                'angular_z_scale': 1.0,
                'joy_timeout': 0.2,
                # Sticks drive the rover while switch 0 is 1; the arm gets them
                # when it is 0. Both nodes read the same switch.
                'mode_switch_index': 0,
                'mode_switch_value': 1,
            }],
            remappings=[
                # /cmd_vel is twist_mux's OUTPUT on the rover. External sources
                # feed an input: cmd_vel_ext (priority 10), so the onboard
                # gamepad on cmd_vel_joy (priority 100) always wins.
                ('cmd_vel', '/cmd_vel_ext'),
            ],
            output='screen'
        ),
        Node(
            package='indomitus_rover_joy',
            executable='joy_to_servo_node',
            name='joy_to_servo_node',
            parameters=[{
                # Deliberately the same sticks as the drive node — the mode
                # switch decides who listens, so only one of them is ever
                # publishing. Adjust these once the arm jog mapping is chosen.
                'linear_x_axis': 1,
                'linear_y_axis': 0,
                'linear_z_axis': -1,
                'angular_x_axis': -1,
                'angular_y_axis': 3,
                'angular_z_axis': 2,
                'linear_scale': 1.0,
                'angular_scale': 1.0,
                'frame_id': 'base_link',
                'mode_switch_index': 0,
                'mode_switch_value': 0,
            }],
            output='screen'
        )
    ])

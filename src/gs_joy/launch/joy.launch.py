import os

from ament_index_python.packages import get_package_share_directory
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

    # Survives restarts, so a remapped panel stays remapped. Written by the
    # UI through /arm_gamepad/save_bindings.
    arm_bindings_file_arg = DeclareLaunchArgument(
        'arm_bindings_file',
        default_value=EnvironmentVariable(
            'ARM_BINDINGS_FILE', default_value='/work/config/arm_bindings.yaml'),
        description='YAML file the arm-mapping page saves to and the node loads at startup'
    )

    # Runtime bindings live on /work, not in the package's share directory.
    # Under --symlink-install that share path is a symlink back into the repo
    # source, so save_bindings was rewriting a tracked file: the console's own
    # wiring ended up in git, and the shipped defaults were lost the first time
    # anyone pressed Apply. Same place the arm mapping and the stick
    # calibration persist to.
    gs_bindings_file_arg = DeclareLaunchArgument(
        'gs_bindings_file',
        default_value=EnvironmentVariable(
            'GS_BINDINGS_FILE', default_value='/work/config/gs_bindings.yaml'),
        description='YAML file the settings dialog saves rover-function binds to'
    )

    arm_joy_topic_arg = DeclareLaunchArgument(
        'arm_joy_topic',
        default_value=EnvironmentVariable('ARM_JOY_TOPIC', default_value='/arm/joy'),
        description="Topic the arm's gamepad_servo_node should be remapped onto"
    )

    # Which console control calls which rover service. Shipped defaults; the
    # UI's settings dialog rewrites this file via ~/save_bindings, the same way
    # the arm-mapping page rewrites arm_bindings.yaml below.
    gs_bindings = os.path.join(
        get_package_share_directory('gs_joy'), 'config', 'gs_bindings.yaml')

    # Which console control fills which SDL gamepad slot for the arm. Shipped
    # defaults; the UI's arm-mapping page rewrites this file via save_bindings.
    arm_bindings = os.path.join(
        get_package_share_directory('gs_joy'), 'config', 'arm_bindings.yaml')

    return LaunchDescription([
        joy_board_port_arg,
        button_board_port_arg,
        calibration_file_arg,
        arm_bindings_file_arg,
        gs_bindings_file_arg,
        arm_joy_topic_arg,
        # One node, both boards. They are two USB ports but one panel, and
        # the shared serial reader is what lets either board reconnect on its
        # own instead of needing a relaunch.
        Node(
            package='gs_joy',
            executable='console_boards_node',
            name='console_boards',
            parameters=[{
                'joy_port': LaunchConfiguration('joy_board_port'),
                # Must match UART_BAUD in the joystick board's firmware.
                'joy_baudrate': 921600,
                'switch_port': LaunchConfiguration('button_board_port'),
                'switch_baudrate': 115200,
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
                'num_switches': 23,
                'switch_publish_rate': 10.0,
            }],
            output='screen'
        ),
        Node(
            package='gs_joy',
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
                # Second dead band, on top of console_boards' own. That one is
                # measured around the calibrated centre and so misses a stick
                # that rests off-centre; this one is applied to the axis as it
                # arrives, and also covers a gamepad publishing /joy directly.
                'deadzone': 0.05,
                'joy_timeout': 0.2,
                # The sticks run at 200 Hz; the rover link does not need to.
                'publish_rate': 50.0,
                # Sticks drive the rover while switch 0 is 0; the arm gets them
                # when it is 1. Both nodes read the same switch.
                'mode_switch_index': 0,
                'mode_switch_value': 0,
            }],
            remappings=[
                # /cmd_vel is twist_mux's OUTPUT on the rover. External sources
                # feed an input: cmd_vel_gs (priority 10), so the onboard
                # gamepad on cmd_vel_joy (priority 100) always wins.
                ('cmd_vel', '/cmd_vel_gs'),
            ],
            output='screen'
        ),
        Node(
            package='gs_joy',
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
                # Same cap as the drive node; MoveIt Servo's own
                # incoming_command_timeout is 0.1 s, so 50 Hz has wide margin.
                'publish_rate': 50.0,
                'mode_switch_index': 0,
                'mode_switch_value': 1,
            }],
            output='screen'
        ),
        # The console, dressed as an SDL gamepad, for the arm's
        # gamepad_servo_node. Its own topic rather than /joy: /joy already
        # carries the console's raw frame, which is a different layout
        # entirely. Point the rover's gamepad node at /arm/joy.
        Node(
            package='gs_joy',
            executable='arm_gamepad_node',
            name='arm_gamepad',
            parameters=[arm_bindings, {
                'output_topic': LaunchConfiguration('arm_joy_topic'),
                'bindings_file': LaunchConfiguration('arm_bindings_file'),
                # The arm stops itself after 0.2 s of /joy silence, and the
                # button board only speaks on change, so this publishes on a
                # timer rather than on input.
                'publish_rate': 50.0,
            }],
            output='screen'
        ),
        # Console switches -> rover services. Separate from the drive path on
        # purpose: this one talks to services that may not answer, and must
        # never be able to delay a stop.
        Node(
            package='gs_joy',
            executable='gs_interpreter_node',
            name='gs_interpreter',
            # The file twice over: once as a parameter file for its contents,
            # once as a path so the node knows where to write it back.
            # The shipped file seeds the defaults; bindings_file is where
            # save_bindings writes and where startup restores from, so a
            # console that has been configured keeps its own wiring and the
            # package keeps its defaults.
            parameters=[gs_bindings, {
                'bindings_file': LaunchConfiguration('gs_bindings_file'),
            }],
            output='screen'
        )
    ])

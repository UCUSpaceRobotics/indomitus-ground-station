from launch import LaunchDescription
from launch_ros.actions import Node
from launch.actions import DeclareLaunchArgument
from launch.substitutions import LaunchConfiguration, EnvironmentVariable

def generate_launch_description():
    port_arg = DeclareLaunchArgument(
        'port',
        default_value=EnvironmentVariable('SERIAL_PORT', default_value='/dev/ttyACM0'),
        description='Serial port for the joystick'
    )

    return LaunchDescription([
        port_arg,
        Node(
            package='indomitus_rover_joy',
            executable='serial_joy_node',
            name='serial_joy_node',
            parameters=[{
                'port': LaunchConfiguration('port'),
                'baudrate': 115200,
                'adc_max': 4095,
            }],
            output='screen'
        ),
        Node(
            package='indomitus_rover_joy',
            executable='joy_to_cmd_vel_node',
            name='joy_to_cmd_vel_node',
            parameters=[{
                'linear_axis': 1,
                'angular_axis': 0,
                'linear_scale': 1.0,
                'angular_scale': 1.0,
            }],
            output='screen'
        ),
        Node(
            package='indomitus_rover_joy',
            executable='joy_to_servo_node',
            name='joy_to_servo_node',
            parameters=[{
                'linear_x_axis': 1,
                'linear_y_axis': 0,
                'angular_y_axis': 2,
                'linear_scale': 1.0,
                'angular_scale': 1.0,
                'frame_id': 'base_link',
            }],
            output='screen'
        )
    ])

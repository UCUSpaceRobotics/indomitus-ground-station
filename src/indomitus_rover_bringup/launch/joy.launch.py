from launch import LaunchDescription
from launch_ros.actions import Node

def generate_launch_description():
    return LaunchDescription([
        Node(
            package='indomitus_rover_joy',
            executable='serial_joy_node',
            name='serial_joy_node',
            parameters=[{
                'port': '/dev/ttyACM0',
                'baudrate': 115200,
                'adc_max': 4095,
            }],
            output='screen'
        )
    ])

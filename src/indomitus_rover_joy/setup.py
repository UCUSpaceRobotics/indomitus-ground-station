from setuptools import find_packages, setup
import os
from glob import glob

package_name = 'indomitus_rover_joy'

setup(
    name=package_name,
    version='0.0.0',
    packages=find_packages(exclude=['test']),
    data_files=[
        ('share/ament_index/resource_index/packages',
            ['resource/' + package_name]),
        ('share/' + package_name, ['package.xml']),
        (os.path.join('share', package_name, 'launch'), glob('launch/*.launch.py')),
    ],
    install_requires=['setuptools'],
    zip_safe=True,
    maintainer='yurFitio',
    maintainer_email='fito.pn@ucu.edu.ua',
    description='Serial joystick bridge for Indomitus Rover',
    license='MIT',
    tests_require=['pytest'],
    entry_points={
        'console_scripts': [
            'serial_joy_node = indomitus_rover_joy.serial_joy_node:main',
            'joy_to_cmd_vel_node = indomitus_rover_joy.joy_to_cmd_vel_node:main',
            'joy_to_servo_node = indomitus_rover_joy.joy_to_servo_node:main'
        ],
    },
)

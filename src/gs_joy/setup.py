from setuptools import find_packages, setup
import os
from glob import glob

package_name = 'gs_joy'

setup(
    name=package_name,
    version='0.0.0',
    packages=find_packages(exclude=['test']),
    data_files=[
        ('share/ament_index/resource_index/packages',
            ['resource/' + package_name]),
        ('share/' + package_name, ['package.xml']),
        (os.path.join('share', package_name, 'launch'), glob('launch/*.launch.py')),
        (os.path.join('share', package_name, 'config'), glob('config/*.yaml')),
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
            'console_boards_node = gs_joy.console_boards_node:main',
            'arm_gamepad_node = gs_joy.arm_gamepad_node:main',
            'joy_to_cmd_vel_node = gs_joy.joy_to_cmd_vel_node:main',
            'joy_to_servo_node = gs_joy.joy_to_servo_node:main',
            'gs_interpreter_node = gs_joy.gs_interpreter_node:main'
        ],
    },
)

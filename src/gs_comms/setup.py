import os
from glob import glob

from setuptools import find_packages, setup

package_name = 'gs_comms'

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
    description='Ground-station link monitoring and Wi-Fi/LoRa failover for Indomitus Rover',
    license='MIT',
    tests_require=['pytest'],
    entry_points={
        'console_scripts': [
            'link_status_node = gs_comms.link_status_node:main',
            'lora_gateway_node = gs_comms.lora_gateway_node:main',
        ],
    },
)

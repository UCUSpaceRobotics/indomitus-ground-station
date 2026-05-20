# Indomitus Ground Station

This repository contains the ground station software for the Indomitus Rover, specifically focused on joystick control and serial communication.

## Quick Start

### 1. Build and Start the Container
By default, the system expects a serial device at `/dev/ttyACM0`.

```bash
docker-compose up -d
```

### 2. Enter the Container
```bash
docker-compose exec indomitus_ground_station bash
```

### 3. Launch the Joystick Bridge
Inside the container:
```bash
ros2 launch indomitus_rover_joy joy.launch.py
```

## Configuration

### Serial Port
If your device is on a different port (e.g., `/dev/ttyUSB0`), you can specify it using the `SERIAL_PORT` environment variable:

```bash
SERIAL_PORT=/dev/ttyUSB0 docker-compose up -d
```

To run the container without a hardware device connected (to avoid Docker errors), set the port to `/dev/null`:

```bash
SERIAL_PORT=/dev/null docker-compose up -d
```

### Launch Parameters
You can also override the port directly when launching:

```bash
ros2 launch indomitus_rover_joy joy.launch.py port:=/dev/ttyUSB0
```

## Development Commands

### Building the Workspace
The workspace is automatically built on container start if changes are detected, but you can manualy trigger it inside the container:

```bash
colcon build --symlink-install
source install/setup.bash
```

### Running Individual Nodes
If you want to run nodes separately:

```bash
ros2 run indomitus_rover_joy serial_joy_node
ros2 run indomitus_rover_joy joy_to_cmd_vel_node
ros2 run indomitus_rover_joy joy_to_servo_node
```

## Project Structure
- `src/indomitus_rover_joy`: ROS 2 package for joystick serial bridge and message conversion.
- `src/indomitus_rover_bringup`: Top-level launch configurations.
- `docker/`: Dockerfile and entrypoint scripts.

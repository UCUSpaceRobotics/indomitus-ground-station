# Indomitus Ground Station

This repository contains the ground station software for the Indomitus Rover, specifically focused on joystick control and serial communication.

## Quick Start

### 1. Build and Start the Container
By default the system expects two ESP32s: the **joystick board** at
`/dev/ttyACM0` and the **button board** at `/dev/ttyACM1`.

```bash
docker compose up -d
```

### 2. Enter the Container
```bash
docker compose exec indomitus_ground_station bash
```

### 3. Launch the Joystick Bridge
Inside the container:
```bash
ros2 launch indomitus_rover_joy joy.launch.py
```

## Configuration

### Serial Port
The system is configured to mount `/dev` and has permissions via the `dialout` group. This means:
1. **The container starts even if the device is not connected.**
2. **Hot-plugging is supported** (you can plug the device in after the container is running).

If a board is on a different port, override it with an environment variable:

```bash
JOY_BOARD_PORT=/dev/ttyUSB0 BUTTON_BOARD_PORT=/dev/ttyUSB1 docker compose up -d
```

`/dev/ttyACM*` numbering follows plug-in order, so if the two boards swap,
either swap the variables or add a udev rule keyed on the CP2102 serial number
to get stable `/dev/joy_board` and `/dev/button_board` symlinks.

### Launch Parameters
You can also override the ports directly when launching:

```bash
ros2 launch indomitus_rover_joy joy.launch.py \
    joy_board_port:=/dev/ttyUSB0 button_board_port:=/dev/ttyUSB1
```

### Stick Calibration
Open **`#/calibrate`** in the UI (linked from the home page under *Setup*).

The wizard walks all three sticks, five captures each — X max, X min, Y max,
Y min, then release to centre — confirming between movements. It reads
`/joy/raw` (the uncalibrated 0..1000 values in the firmware's own channel
order) rather than `/joy`, since `/joy` is the output of the very mapping being
calibrated.

**Nothing about the wiring is assumed.** Each capture watches which channel
actually moved and assigns it, so it does not matter which ADC pin a pot is
soldered to, nor which physical stick you decide is #1 — you bind axes the way
a game does. The resulting logical→physical table is the `axis_map` parameter;
`/joy` always publishes in logical order (`stick1 X, stick1 Y, stick2 X, …`),
so `joy_to_cmd_vel_node` keeps working unchanged.

Whichever direction you push when asked for **max** becomes +1.0, so a stick
wired backwards needs no invert flag — it just gets calibrated as held.

**Panel buttons drive the wizard.** Under *Panel buttons*, bind *Confirm/next*
and *Restart* to any switch on either board — press *Bind*, then press the
switch you want. Bindings persist in the browser. This is the point of the
feature: both hands stay on the sticks while calibrating, no mouse needed.

*Apply to node* pushes `axis_min` / `axis_center` / `axis_max` / `deadzone` to
`serial_joy_node` over `rcl_interfaces/srv/SetParameters`; it takes effect
immediately, no restart. *Save on rover* calls
`/serial_joy_node/save_calibration` (`std_srvs/Trigger`), which writes
`calibration_file` (default `/work/config/joy_calibration.yaml`, override with
`JOY_CALIBRATION_FILE`). The node reloads that file on startup, so the sticks
only need calibrating once.

The file is written in `ros2 param` layout, so `ros2 param load /serial_joy_node
joy_calibration.yaml` works too.

The **centre deadzone** slider sets the fraction of travel that reads as zero.
Travel outside it is rescaled to the full range, so there is no jump at the
deadzone edge.

### Hardware Protocol
Both boards talk plain ASCII at 115200 baud.

**Joystick board** (`microcontrollers_indomitus/esp32_switch+joy`) — one line
every 20 ms:

```
110110011|498|501|500|512|499|503
```

9 switch bits (PCF8575 @ 0x24), then 6 axes as `0..1000` in the order
J0X, J0Y, J1X, J1Y, J2X, J2Y. `serial_joy_node` normalizes each axis to
`-1.0 .. 1.0` around 500 and publishes `sensor_msgs/Joy` on `/joy`, with the
9 switches carried in `Joy.buttons`.

**Button board** (`microcontrollers_indomitus/esp_32_switches+buttons`) — 23
bits, `1 = pressed`, sent **only when the debounced state changes**:

```
00000000000000000000000
```

`switch_reader_node` latches that state and republishes it at 10 Hz on
`/switches` (`std_msgs/Int32MultiArray`) so the UI does not mark it stale
between presses.

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
ros2 run indomitus_rover_joy switch_reader_node
ros2 run indomitus_rover_joy joy_to_cmd_vel_node
ros2 run indomitus_rover_joy joy_to_servo_node
```

### Driving the Rover
`joy_to_cmd_vel_node` turns `/joy` into `geometry_msgs/Twist`. It is a swerve
rover, so `linear.y` is real — it can strafe sideways.

**Publish to `/cmd_vel_ext`, not `/cmd_vel`.** On the rover `/cmd_vel` is the
*output* of `twist_mux`, which arbitrates three inputs (`rover_bringup/config/twist_mux.yaml`):

| Input          | Priority | Source                        |
| -------------- | -------- | ----------------------------- |
| `cmd_vel_joy`  | 100      | onboard bluetooth gamepad     |
| `cmd_vel_nav`  | 50       | Nav2                          |
| `cmd_vel_ext`  | 10       | this ground station           |

The launch file remaps accordingly. Lowest priority is deliberate: whoever holds
the onboard gamepad can always override the remote console, and twist_mux drops
any input that goes quiet for 0.5 s.

Stick mapping, kept identical to `rover_teleop/config/joy.yaml` so the panel and
the gamepad behave the same:

| Twist        | Axis        | Stick             | Scale |
| ------------ | ----------- | ----------------- | ----- |
| `linear.x`   | 1 (`J0Y`)   | forward / back    | 0.5   |
| `linear.y`   | 0 (`J0X`)   | strafe            | 0.5   |
| `angular.z`  | 2 (`J1X`)   | yaw               | 1.0   |

`rover_controller/RoverSwerveController` consumes the Twist as a ros2_control
plugin and drives the wheel/steer interfaces directly — there are no
`/steering_controller/commands` topics any more. It clamps to `max_linear_speed`
and `max_angular_speed` (1.0 each) and stops on its own after
`cmd_vel_timeout_s` (0.5 s).

If `/joy` goes quiet for `joy_timeout` (0.2 s) the node publishes a zero Twist,
so an unplugged board stops the rover instead of latching the last command.

**Twist alone will not move the rover.** Both the hardware component and the
controller start inactive (`hardware_components_initial_state: inactive:
RoverHardware`, and `rover.launch.py` passes `inactive_controllers:
swerve_controller`). The onboard gamepad enables them via its motor-toggle
button, which calls `/controller_manager/set_hardware_component_state` and
`/controller_manager/switch_controller`. The ground station does not do this
yet — see below.

## Operator Console (Web UI)

`ui/` holds the browser console the operator drives from: camera wall, live
telemetry, joystick command path and the rover log. It talks to ROS over
`rosbridge_websocket` (port 9090, already published by `docker-compose.yml`) and
takes camera video from `web_video_server`.

```bash
cd ui
npm install
npm run dev     # http://localhost:5173, also reachable from the LAN
```

Inside the container, alongside the joystick launch:

```bash
ros2 launch rosbridge_server rosbridge_websocket_launch.xml
ros2 run web_video_server web_video_server
```

See `ui/README.md` for configuration and `ui/COMMANDS.md` for the full command
list.

## Project Structure
- `src/indomitus_rover_joy`: ROS 2 package for joystick serial bridge and message conversion.
- `src/indomitus_rover_bringup`: Top-level launch configurations.
- `docker/`: Dockerfile and entrypoint scripts.
- `ui/`: React operator console (rosbridge + web_video_server).

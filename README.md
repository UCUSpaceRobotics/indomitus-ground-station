# Indomitus Ground Station

This repository contains the ground station software for the Indomitus Rover, specifically focused on joystick control and serial communication.

## Quick Start

```bash
docker compose -f docker/docker-compose.gs.yaml --project-directory . up -d
```

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
`console_boards` over `rcl_interfaces/srv/SetParameters`; it takes effect
immediately, no restart. *Save on rover* calls
`/console_boards/save_calibration` (`std_srvs/Trigger`), which writes
`calibration_file` (default `/work/config/joy_calibration.yaml`, override with
`JOY_CALIBRATION_FILE`). The node reloads that file on startup, so the sticks
only need calibrating once.

The file is written in `ros2 param` layout, so `ros2 param load /console_boards
joy_calibration.yaml` works too. A file saved before the two board nodes were
merged is keyed `serial_joy_node`; it is still read, and rewritten under the
new name the next time you save.

The **centre deadzone** slider sets the fraction of travel that reads as zero.
Travel outside it is rescaled to the full range, so there is no jump at the
deadzone edge.

### Arm Mapping

The arm is driven by `gamepad_servo_node` on the rover, which reads a **canonical
SDL gamepad**: `axes[0..5]` and `buttons[0..14]`, where an index means the same
physical control on every device. The console is not a gamepad — it is three
sticks and two boards of switches in whatever order they were soldered — so
`arm_gamepad_node` assembles that layout and publishes it on `/arm/joy`.

It is a separate topic from `/joy` on purpose: `/joy` carries the console's own
raw frame, which the drive nodes and the calibration wizard read, and it is a
different layout entirely. Point the rover's gamepad node at this one:

```bash
ros2 launch arm_tasks gamepad.launch.py --ros-args -r joy:=/arm/joy
```

**Bind controls from the UI**, not by editing indices: open *Arm mapping* from
the home page, press **Bind** on a row, then press the switch or move the stick
you want. Each row's dot lights when the arm is seeing that control right now,
which is the only real confirmation a binding took. *Apply to node* takes effect
immediately with no restart; *Save on console* writes `arm_bindings_file`
(default `/work/config/arm_bindings.yaml`, override with `ARM_BINDINGS_FILE`) so
it survives one. Apply first — what gets saved is what was tested.

A binding is one string per slot, `"<source>:<index>"` or
`"<source>:<index>:inv"`, where the source is `joy` (the stick board's own 9
switches), `switches` (the button board's 23) or `joy_axis` (the sticks). The
`:inv` flag is for a switch wired so "on" reads 0, or a stick that pushes the
wrong way. The node validates every binding and refuses a bad one with a reason,
so a typo cannot half-remap the arm — a button source on a stick slot, for
instance, would publish a hard 0 or 1 as an axis value, which is full-speed arm
motion from a toggle.

Shipped defaults live in `src/indomitus_rover_joy/config/arm_bindings.yaml`.
They are **placeholders**, exactly like `gs_bindings.yaml`: a plausible panel
layout, not a measured one. Bind them against the real console before a run.

SDL button 6 (START) is deliberately not offered anywhere — the arm document
marks its index as unverified on real hardware. Buttons 4, 5, 7, 8, 12 and 14 are
absent for the opposite reason: the arm ignores them, so binding one would be a
control that silently does nothing.

With a bridge and the node running, `npm run check:arm` in `ui/` round-trips a
mapping through rosbridge and puts it back as it found it.

### Hardware Protocol
Both boards talk plain ASCII, and both are read by a single node,
`console_boards`, which owns one serial port per board.

**Joystick board** (`microcontrollers_indomitus/esp32_switch+joy`) — 921600
baud, one line every 5 ms (`POLL_INTERVAL_MS` in its firmware):

```
110110011|498|501|500|512|499|503
```

9 switch bits (PCF8575 @ 0x24), then 6 axes as `0..1000` in the order
J0X, J0Y, J1X, J1Y, J2X, J2Y. `console_boards` normalizes each axis to
`-1.0 .. 1.0` around 500 and publishes `sensor_msgs/Joy` on `/joy`, with the
9 switches carried in `Joy.buttons`.

**Button board** (`microcontrollers_indomitus/esp_32_switches+buttons`) — 115200
baud, 23 bits, `1 = pressed`, sent **only when the debounced state changes**:

```
00000000000000000000000
```

`console_boards` latches that state and republishes it at 10 Hz on
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
ros2 run indomitus_rover_joy console_boards_node
ros2 run indomitus_rover_joy arm_gamepad_node
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
`rosbridge_websocket` on port 9090 and takes camera video from
`web_video_server` on port 8080.

Both are started by `gs.launch.py`, together with the serial boards and the mast
link, so on the console there is nothing to start by hand:


The root `docker-compose.yml` is the development counterpart: same image and
same environment, but it launches nothing and leaves you a shell. Start pieces
there yourself, or run the same launch with the parts you do not want switched
off:

```bash
docker compose up -d
docker compose exec indomitus_ground_station bash
ros2 launch indomitus_rover_bringup gs.launch.py use_joy:=false use_comms:=false
```

The UI itself runs on the host, not in the container:

```bash
cd ui
npm install
npm run dev     # http://localhost:5173, also reachable from the LAN
```

See `ui/README.md` for configuration and `ui/COMMANDS.md` for the full command
list.

## Project Structure
- `src/indomitus_rover_joy`: ROS 2 package for joystick serial bridge and message conversion.
- `src/indomitus_rover_bringup`: Top-level launch configurations.
- `docker/`: Dockerfile and entrypoint scripts.
- `ui/`: React operator console (rosbridge + web_video_server).

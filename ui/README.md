# Indomitus Ground Station UI

Operator console for the Indomitus rover. React + Vite in the browser, talking to
ROS 2 over `rosbridge_websocket`, with camera video over `web_video_server`.

It is a **monitoring** console: it subscribes, it never publishes. Drive
authority stays with the physical joystick box and `joy_to_cmd_vel_node`.

## Requirements

On the rover / ground station backend:

```bash
# required — telemetry, log and switch state
ros2 launch rosbridge_server rosbridge_websocket_launch.xml   # port 9090

# recommended — camera video (see "Camera transport" below)
ros2 run web_video_server web_video_server                    # port 8080
```

`rosbridge-suite` and `web-video-server` come from
`ros-<distro>-rosbridge-suite` and `ros-<distro>-web-video-server`.
Port 9090 is already published by the repository's `docker-compose.yml`; add
`"8080:8080"` there if you run `web_video_server` inside the container.

## Running

```bash
cd ui
npm install
npm run dev        # http://localhost:5173 — also served on the LAN
npm run build      # static bundle in ui/dist
npm run preview    # serve the built bundle

npm run check        # lint + render every route headlessly; no rosbridge needed
npm run check:bridge # reconnect/backoff/watchdog against a live bridge
```

The dev server binds all interfaces, so a second laptop on the rover network can
open `http://<ground-station-ip>:5173`.

## Screens

| Route          | Purpose                                                          |
| -------------- | ---------------------------------------------------------------- |
| `#/`           | Monitor picker, keyboard reference, endpoint check before a run   |
| `#/left`       | Aux cameras, telemetry, command path, control box, rover log      |
| `#/right`      | Primary camera wall                                              |
| `#/cam/<id>`   | One camera full-screen, for a third display                       |

Routing is hash-based, so `dist/` works from any static host — or straight off
disk — with no rewrite rules.

### Keyboard

| Key     | Action                        |
| ------- | ----------------------------- |
| `1`–`9` | Select camera                 |
| `←` `→` | Previous / next camera        |
| `G`     | Toggle focus / grid layout    |
| `F`     | Fullscreen the camera pane    |
| `S`     | Bypass the physical switch box|

## Configuration

Endpoints resolve in this order, first match wins:

1. **Query string** — `#/right?ros=ws://10.0.0.5:9090&video=http://10.0.0.5:8080`
   (also `mode=mjpeg|ros`, `theme=dark|light`). Per-window, nothing persisted.
2. **Settings dialog** (gear icon) — persisted in `localStorage`. Camera names,
   image topics, switch indices, monitor assignment and every telemetry topic are
   editable here, so retargeting the UI at a different rover needs no rebuild.
   "Scan topics" pulls the live topic list off `rosapi` for autocomplete.
3. **Build-time env** — see `.env.example`.
4. **Derived from the page host** — `ws://<host>:9090` and `http://<host>:8080`.

Step 4 is why the UI works when it is served from the ground station laptop and
opened somewhere else; a hard-coded `localhost` does not.

### Camera transport

- **`mjpeg` (default)** — `web_video_server` multipart streams. Cheap on CPU and
  bandwidth. The browser exposes no per-frame metadata, so a tile can only report
  whether the connection is up.
- **`ros`** — `sensor_msgs/CompressedImage` over rosbridge. Needs no extra
  rover-side node and every frame carries a timestamp, so tiles report true frame
  rate, frame age and link delay — which is what lets a frozen feed be told apart
  from a live one. Costs noticeably more bandwidth; base64 over the websocket is
  roughly a third larger than the raw JPEG.

In focus layout the thumbnail strip polls `/snapshot` at 0.5 Hz instead of
opening a stream per camera; the grid layout streams every visible tile.

### Temporary placeholder stills

Two cameras have stand-in stills while they are not yet publishing:

| Camera            | Topic                      | File                                  |
| ----------------- | -------------------------- | ------------------------------------- |
| Arm End Effector  | `/camera/arm/image_raw`    | `src/assets/placeholders/gripper_cam.jpg` |
| Mast Pan/Tilt     | `/camera/mast/image_raw`   | `src/assets/placeholders/mast_cam.jpg`    |

A still is drawn **only while the real feed is down**, and the pane is always
marked while one is showing — an amber `PLACEHOLDER` badge in the main pane, an
amber corner dot on thumbnails — with the image dimmed and desaturated. The
moment the topic starts publishing, the live feed takes over. A stand-in must
never be mistakable for live video, particularly on a screen someone drives from.

To remove: delete the `PLACEHOLDER_BY_ID` / `PLACEHOLDER_BY_TOPIC` block in
`src/config.js`, the two files above, and the `.feed-still` rules in
`src/index.css`.

## Topics consumed

| Topic                          | Type                          | Used for                       |
| ------------------------------ | ----------------------------- | ------------------------------ |
| `/switches`                    | `std_msgs/Int32MultiArray`    | Which camera tiles are enabled |
| `/joy`                         | `sensor_msgs/Joy`             | Stick positions, publish rate  |
| `/cmd_vel`                     | `geometry_msgs/Twist`         | Drive command                  |
| `/servo_node/delta_twist_cmds` | `geometry_msgs/TwistStamped`  | Arm cartesian command          |
| `/rosout`                      | `rcl_interfaces/msg/Log`      | Rover log                      |
| `/odom`                        | `nav_msgs/Odometry`           | Speed, heading, position       |
| `/battery_state`               | `sensor_msgs/BatteryState`    | Charge, pack voltage, current  |
| `/imu/data`                    | `sensor_msgs/Imu`             | Roll / pitch                   |
| `/gps/fix`                     | `sensor_msgs/NavSatFix`       | Position and fix quality       |
| `<camera>/compressed`          | `sensor_msgs/CompressedImage` | Video in `ros` transport mode  |

The first five are published by this repository. The rest are the usual rover
topics; where one is absent the matching readout stays empty rather than
inventing a value.

## Behaviour worth knowing

- **Nothing is faked.** A readout with no publisher, or one that has gone quiet,
  shows `—` and dims. Battery percentage in particular is never estimated.
- **The link self-heals.** rosbridge drops are retried with exponential backoff
  and a connect watchdog, and every subscription is re-established afterwards —
  roslib does not replay them. The badge in the top bar shows measured
  round-trip time and flags a rover clock that has drifted.
- **Switch gating fails open.** Until `/switches` publishes, every camera is
  shown. `S` (or the toolbar button) ignores the switch box entirely, for when it
  is unplugged mid-run with feeds left "off".
- **No fonts are fetched.** A system font stack, because a competition site has
  no internet.

## Layout

```
src/
├── config.js              runtime config store (query → localStorage → env → host)
├── ros/
│   ├── context.js         connection context + useRos
│   ├── RosProvider.jsx    single connection, reconnect, latency probe
│   └── useTopic.js        subscriptions, render throttling, staleness, log buffer
├── components/            camera, telemetry, drive, switches, log, settings, chrome
├── pages/                 Home, LeftMonitor, RightMonitor, SingleCamera
└── lib/format.js          units, quaternions, timestamps

scripts/
├── smoke-render.mjs       npm run check:render
└── check-connection.mjs   npm run check:bridge
```

`src/ros/connection.js` is deliberately plain JS with no React in it: the retry
state machine is the part most worth testing on its own, and `check:bridge`
drives it directly.

## Troubleshooting

| Symptom                                | Check                                                                 |
| -------------------------------------- | --------------------------------------------------------------------- |
| Badge stuck on "Reconnecting"          | `rosbridge_websocket` running; port 9090 reachable; URL in settings    |
| Telemetry all `—`, log empty           | Bridge is up but the topics are not being published — check `ros2 topic list` |
| Every camera "No signal"               | `web_video_server` running and port 8080 reachable, or switch to `ros` transport |
| One camera "No signal"                 | Topic name in settings; `ros2 topic hz <topic>`                        |
| Cameras missing with no obvious reason | Control box panel — the switch may be off. Press `S` to bypass         |
| Video works, page is sluggish          | Use focus layout instead of grid, or lower quality/width in settings   |

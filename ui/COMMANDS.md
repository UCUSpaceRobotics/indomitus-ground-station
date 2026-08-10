# UI Commands

Everything here runs from the `ui` directory. See `README.md` for what the
console actually does and how it is configured.

## Node side

```bash
npm install          # first run, and after pulling new dependencies

npm run dev          # dev server on :5173, bound to all interfaces (HMR)
npm run build        # static bundle into ui/dist
npm run preview      # serve the built bundle on :4173

npm run lint         # oxlint
npm run lint:fix     # oxlint with autofix

npm run check        # lint + render every route headlessly (no rosbridge needed)
npm run check:bridge # exercise reconnect/backoff/watchdog against a live bridge
```

`npm run check` is the one to run before committing: it mounts `#/`, `#/left`,
`#/right` and `#/cam/<id>` through Vite's SSR pipeline and fails on any
render-time error.

`npm run check:bridge` needs a running `rosbridge_websocket`. It defaults to
`ws://localhost:9090`; pass another URL as an argument:

```bash
npm run check:bridge -- ws://192.168.1.50:9090
```

Because the dev server binds every interface, a second machine on the rover
network can open `http://<ground-station-ip>:5173` directly — useful for driving
the two-monitor setup from one laptop while someone watches on another.

## ROS side

The UI needs `rosbridge_websocket` for everything, and `web_video_server` for
camera video in the default MJPEG transport.

```bash
# required
ros2 launch rosbridge_server rosbridge_websocket_launch.xml    # :9090

# recommended
ros2 run web_video_server web_video_server                     # :8080
```

Install with `ros-<distro>-rosbridge-suite` and `ros-<distro>-web-video-server`.

`docker-compose.yml` already publishes 9090. If `web_video_server` runs inside
the container, add `"8080:8080"` to its `ports` list as well.

Without `web_video_server` the console still works: switch the camera transport
to *CompressedImage via rosbridge* in the settings dialog (gear icon), which
carries video over the 9090 websocket instead.

## Sanity checks on the rover

```bash
ros2 topic list                 # do the topics the UI reads exist?
ros2 topic hz /switches         # is the control box publishing?
ros2 topic hz /camera/front/image_raw
```

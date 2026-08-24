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

# required for video - note the two type defaults, they are not optional
ros2 run web_video_server web_video_server --ros-args \
  -p port:=8080 \
  -p default_stream_type:=ros_compressed \
  -p default_snapshot_type:=ros_compressed
```

Both packages are in `docker/Dockerfile`. Networking is `network_mode: host`, so
9090 and 8080 bind the host directly and no `ports:` list is involved.

**The two `ros_compressed` defaults matter.** In any other stream type
`web_video_server` subscribes to the *raw* image topic and transcodes it. Raw is
1.73 MB/frame at 960x600 — about 415 Mbit/s at 30 fps, roughly four times the
whole rover Wi-Fi link, and it will take the rover offline. `ros_compressed`
passes the rover's existing JPEG through untouched: no decode, no re-encode, no
subscriber on the raw topic. `mjpegUrl()`/`snapshotUrl()` in `ui/src/config.js`
request it explicitly; these defaults are the backstop.

Do **not** fall back to *CompressedImage via rosbridge* in the settings dialog
except to prove the link works. rosbridge base64s every frame into JSON (a
170 KB frame becomes ~227 KB of text) and the browser builds a fresh `data:` URL
per frame, so the websocket queue grows without bound — measured at 30-60 s of
accumulated lag while frames were still arriving at the GS sub-second fresh.

## Sanity checks on the rover

```bash
ros2 topic list                 # do the topics the UI reads exist?
ros2 topic hz /switches         # is the control box publishing?
ros2 topic hz /camera/front/image_raw
```

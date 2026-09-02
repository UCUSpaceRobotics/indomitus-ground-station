# Rover cameras

Serves the rover's cameras as plain MJPEG over HTTP, bypassing ROS (the
Jetson has no working `v4l2_camera_node`/DDS path — see "The Nano's camera is
deliberately outside all of this" in [`../mast/README.md`](../mast/README.md) for why).

## Files

| File | What it is |
|---|---|
| `start-cameras.sh` | Run this. Deploys and starts one server per camera, from the GS. |
| `camera_mjpeg_server.py` | Runs on the Jetson. Captures one camera, serves it as MJPEG. |
| `cameras.yaml` | The camera list: name → udev symlink, plus optional res/fps/quality/format. |
| `99-arducam-no-superspeed.rules` | udev rule that removes a wedged Arducam's unstable SuperSpeed node. |

## Usage

```
./start-cameras.sh              # probe, deploy, start, verify every configured camera
./start-cameras.sh --probe      # report each camera's real modes/formats, change nothing
./start-cameras.sh --stop
./start-cameras.sh --dry
./start-cameras.sh --help       # every flag, and their env-var equivalents
```

Point a UI camera tile at the URL it prints (`http://<jetson>:<port>/<name>?action=stream`).

## Adding or tuning a camera

Edit `cameras.yaml`. Each camera needs a `device` (its udev symlink, not
`/dev/videoN` — node numbers aren't stable across replugs). `res`, `fps`,
`quality`, `format` are optional per camera, falling back to `start-cameras.sh`'s
`--res`/`--fps`/`--quality`/`--format`.

Don't guess a camera's real modes — check them:

```
./start-cameras.sh --probe
```

This prints every pixel format the camera offers, and every resolution/fps
each format supports, straight from `v4l2-ctl` on the Jetson. Pick `res`/`fps`
from that list, not from a generic "standard resolutions" table — an
unsupported value silently snaps to whatever the fallback logic in
`camera_mjpeg_server.py`'s `pick_mode()` picks instead.

For why the defaults favor small/slow modes on this board (USB 3.0
SuperSpeed instability, CPU budget for the JPEG encode), see
[`../mast/README.md`](../mast/README.md).

## Running `camera_mjpeg_server.py` on the Jetson host, without a venv fallback

JetPack ships no `cv2` for host `python3`, and the rover has no route to
`apt`/`pip`, so `start-cameras.sh` falls back to running inside the
`rover_prod` container — working, but a container restart takes the streams
with it. To avoid that, build a venv with `cv2`+`numpy` **offline** and copy
it onto the rover; `start-cameras.sh` picks it up automatically and prefers
it over both the container and bare host `python3`.

```
# on a machine with internet (NOT the rover) — match the Jetson's arch/python:
pip download opencv-python-headless numpy \
    --platform manylinux2014_aarch64 --python-version 310 \
    --only-binary=:all: -d /tmp/cv-wheels

# transfer and install fully offline, on the rover:
scp -r /tmp/cv-wheels indomitus-rover@10.42.0.1:/tmp/
ssh indomitus-rover@10.42.0.1
mkdir -p ~/cameras && python3 -m venv ~/cameras/.camera-venv
~/cameras/.camera-venv/bin/pip install --no-index --find-links=/tmp/cv-wheels opencv-python-headless numpy
~/cameras/.camera-venv/bin/python3 -c "import cv2, numpy"   # no error = done
```

The venv must live at `<remote $HOME>/cameras/.camera-venv` — `CAM_REMOTE_DIR`
and `CAM_VENV_PY` in `start-cameras.sh` control this path if you need it
elsewhere. `opencv-python-headless` rather than the full package: this script
never touches a display, so the Qt/GTK dependencies aren't needed.

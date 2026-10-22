# Quickstart — bring the console up and see the rover

- Run everything from the repo root on the **GS PC**.
- Link details: [mast/README.md](mast/README.md).

## Real rover

```bash
ROVER_PW=... ./mast/rover-up.sh --dry     # review the plan first
ROVER_PW=... ./mast/rover-up.sh           # do it
```

- Brings the Wi-Fi link up if `10.42.0.1` is down ([restore-link.sh](mast/restore-link.sh)).
- Starts the GS stack, launches `rover_bringup rover.launch.py` on the rover (this includes
  `rover_cameras`), and serves the UI on `:5173`.
- The rover launch runs in tmux session `core` inside `rover_prod`, the same session as
  rover-core's `rover-launch-core.service`. If it is already running, it gets Ctrl-C and is
  restarted. Attach from rover-core: `scripts/enter_container.sh rover core` (`-r` = read-only).
- Ends by querying each camera's `/health` on ports 8090-8097.

| Flag | Default | |
|---|---|---|
| `--rover-ssh` | `indomitus-rover@10.42.0.1` | rover over Wi-Fi |
| `--lifeline` | `indomitus-rover@10.45.0.51` | wired fallback |
| `--rover-dir` | `/home/indomitus-rover/indomitus-rover-core` | rover-core checkout **(verify)** |
| `--container` | `rover_prod` | ROS container; `''` for bare-metal |
| `--session` | `core` | tmux session for `rover.launch.py` |
| `--zed-mode` | `rgb` | `nav`, or `''` to skip the ZED |

Change the rover Wi-Fi channel and recover ROS on it:

```bash
ROVER_PW=... ./mast/rover-channel.sh 149        # 5 GHz ch149
ROVER_PW=... ./mast/rover-channel.sh 11 --band bg
```

Variations:

```bash
./mast/rover-up.sh --skip-link            # link already up; just (re)start the rest
./mast/rover-up.sh --ros-only             # only restart GS + rover ROS
./mast/rover-up.sh --no-ui                # everything except the UI
```

## Enter a container

```bash
scripts/enter_container.sh gs              # GS container
scripts/enter_container.sh rover core      # rover tmux session `core` (-r = read-only)
```

- `rover` = rover-core's `scripts/enter_container.sh rover … -n` (no Wi-Fi switching).

## Open the UI

- Same laptop: <http://localhost:5173>. Another machine: `http://<gs-ip>:5173`.
- Cameras are MJPEG from `rover_cameras`, not ROS topics:
  `http://10.42.0.1:<port>/<name>?action=stream`. The ports are in rover-core
  `src/rover/rover_cameras/config/cameras.yaml`.
- Old camera table saved in the browser: settings → "Restore defaults".

## If something is dark

| Symptom | Look at |
|---|---|
| UI loads, no video | `curl http://10.42.0.1:<port>/health`; `rover_cameras` output in tmux session `core` |
| No topics on the GS at all | the link: `ping 10.42.0.1`; DDS peer matches the target; tmux session `core` on the rover |
| Frozen last frame | the link dropped — the UI holds the last frame; check `ping` and the mast Pi association |
| Link keeps dropping | `mast/BENCH-LINK.md` failure signatures; Wi-Fi power-save on the client; a suspended laptop kills its AP |
| Can't SSH after a reimage | the host key changed — `ssh-keygen -R <ip>` then reconnect |

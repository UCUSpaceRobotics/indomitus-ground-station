# Quickstart — bring the console up and see the rover

- Run everything from the repo root on the **GS PC**.
- Link details: [mast/README.md](mast/README.md).

## Bring it up

```bash
ROVER_PW=... ./mast/restore-link.sh       # 1. Wi-Fi link, only if 10.42.0.1 does not ping (--dry first)
docker compose -f docker/docker-compose.gs.yaml --project-directory . up -d   # 2. GS stack
(cd ui && npm run dev)                    # 3. UI on :5173
scripts/enter_container.sh rover core     # 4. rover: check / start the launch
```

- Rover launch = `ros2 launch rover_bringup rover.launch.py` in tmux session `core` inside
  `rover_prod` (cameras included). It may already be running from `rover-launch-core.service`.
- Cameras answer on `curl http://10.42.0.1:<port>/health` (ports 8090-8097).

Change the rover Wi-Fi channel:

```bash
ROVER_PW=... ./mast/rover-channel.sh 149        # 5 GHz ch149
ROVER_PW=... ./mast/rover-channel.sh 11 --band bg
```

## Enter a container

```bash
scripts/enter_container.sh gs              # GS container
scripts/enter_container.sh gs gs           # GS launch (tmux `gs`): Ctrl-C / Up Enter
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

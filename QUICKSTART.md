# Quickstart — bring the console up and see the rover

One page to get the ground station talking to either the **real rover** or the
**bench** stand-in, with camera video in the UI. Every command runs from the
repo root on the **GS PC** unless it says otherwise.

For the *why* behind the link, see [mast/README.md](mast/README.m

The GS console stack, the UI, and the DDS transport are identical for both; only
the link and the DDS **peer** differ (`10.42.0.1` rover vs `10.43.0.1` bench).

## Real rover

```bash
ROVER_PW=... ./mast/rover-up.sh --dry     # review the plan first
ROVER_PW=... ./mast/rover-up.sh           # do it
```

`rover-up.sh` brings the Wi-Fi link up if `10.42.0.1` is down
([restore-link.sh](mast/restore-link.sh)), starts the GS stack pointed at the
rover, launches `rover_bringup rover.launch.py` on the rover with video crossing
the link, and serves the UI on `:5173`.

Verify the real-rover defaults match your machine (override with flags):

| Flag | Default | |
|---|---|---|
| `--rover-ssh` | `indomitus-rover@10.42.0.1` | rover over Wi-Fi |
| `--lifeline` | `indomitus-rover@10.45.0.51` | wired fallback |
| `--rover-dir` | `/home/indomitus-rover/indomitus-rover-core` | rover-core checkout **(verify)** |
| `--container` | `rover_dev` | ROS container; `''` for bare-metal |
| `--zed-mode` | `rgb` | `nav`, or `''` to skip the ZED |

Change the rover Wi-Fi channel and recover ROS on it:

```bash
ROVER_PW=... ./mast/rover-channel.sh 149        # 5 GHz ch149
ROVER_PW=... ./mast/rover-channel.sh 11 --band bg
```

---

## Bench (stand-in rover)

```bash
./mast/bench-up.sh --dry                  # review the plan
./mast/bench-up.sh                        # do it (prompts once for bench sudo)
```

`bench-up.sh` brings up the `IndomitusBench` AP if `10.43.0.1` is down
([restore-bench-link.sh](mast/restore-bench-link.sh)), adds the GS route, starts
the GS stack pointed at the bench, wires the two arducams to
`/dev/arducam-{mast,rear}`, launches the two arducams (full `rover.launch.py`
is not bench-viable — CAN/ZED hardware), and serves the UI.

Bench defaults (override with flags): `--bench-ssh starezax@10.43.0.1`,
`--rover-dir /home/starezax/Desktop/indomitus/indomitus-rover-core`,
`--zed-mode ''` (no ZED on the bench).

Change the bench channel and recover ROS:

```bash
./mast/bench-channel.sh 149
./mast/bench-channel.sh 11 --band bg
```

---

## Open the UI

- **Same laptop:** <http://localhost:5173>
- **Second machine on the network:** `http://<gs-ip>:5173`

Camera feeds publish under the rover's **native** namespaces —
`/mast_arducam/image_raw`, `/rear_arducam/image_raw` (and
`/container_arducam/…` on the real rover). The UI's default camera tiles point
at `/camera/…`, so open the settings dialog and re-point a tile at the
`*_arducam` topic to watch a feed.

## Handy variations

```bash
./mast/rover-up.sh --skip-link            # link already up; just (re)start the rest
./mast/rover-up.sh --ros-only             # only restart GS + rover ROS
./mast/rover-up.sh --no-ui                # everything except the UI
```

(Same flags on `bench-up.sh`.)

## If something is dark

| Symptom | Look at |
|---|---|
| UI loads, no video | camera topics: `docker exec indomitus_ground_station bash -lc 'ros2 topic list' \| grep arducam`; re-point the UI tile at the `*_arducam` topic |
| No topics on the GS at all | the link: `ping 10.42.0.1` (or `.43`); DDS peer matches the target; `/tmp/rover_launch.log` on the rover/bench |
| Frozen last frame | the link dropped — the UI holds the last frame; check `ping` and the mast Pi association |
| Link keeps dropping | `mast/BENCH-LINK.md` failure signatures; Wi-Fi power-save on the client; a suspended laptop kills its AP |
| Can't SSH after a reimage | the host key changed — `ssh-keygen -R <ip>` then reconnect |

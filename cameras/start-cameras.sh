#!/bin/bash
# Serve the rover's Arducam(s) as plain MJPEG over HTTP, no ROS.
#
#     ./cameras/start-cameras.sh               # probe, deploy, start, verify
#     ./cameras/start-cameras.sh --probe       # only report what the camera offers
#     ./cameras/start-cameras.sh --stop
#     ./cameras/start-cameras.sh --dry
#
# Every other camera on this project publishes over ROS 2 (v4l2/gscam node ->
# Fast DDS -> web_video_server on the GS). This camera bypasses that: its host
# has no working cv2 for a ROS node to relay through, but the Arducam B0495 is
# a driverless UVC device the stock uvcvideo handles fine, so
# cameras/camera_mjpeg_server.py captures it directly and serves MJPEG over HTTP,
# and a UI camera tile points straight at that URL. The trade: this feed is
# outside ROS — no rosbag recording, no per-frame timestamps, and the UI's
# `ros` transport mode cannot carry it (see isDirectUrl() in ui/src/config.js).
#
# Point a camera tile at the URL this prints: open the UI settings dialog and
# type it into the "Image topic / URL" box of any row.
#
# Which cameras get served, and by what name, comes from cameras/cameras.yaml
# (see that file) — it maps each camera's deterministic udev symlink to a
# name, which then appears in its stream URL and log file. With no config
# (or an empty one) every /dev/video* node is auto-discovered instead, named
# after its device.
set -uo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
source "$REPO/cameras/utils.sh"

# ================================================================ defaults ==

# Same names/defaults as enter_container.sh's `rover` command, so the two
# scripts can be pointed at the rover the same way.
JETSON_USER=${JETSON_USER:-indomitus-rover}
JETSON_HOTSPOT_IP=${JETSON_HOTSPOT_IP:-10.42.0.1}
JETSON_ETHERNET_IP=${JETSON_ETHERNET_IP:-indomitus-rover-computer.local}
USE_ETH=${USE_ETH:-false}
WIFI_SSID=${WIFI_SSID:-ERC_UCUSpaceRobotics_A}
WIFI_PASS=${WIFI_PASS:-19283746}
JETSON_IP=${JETSON_IP:-}         # empty = $JETSON_HOTSPOT_IP, or $JETSON_ETHERNET_IP with --eth
JETSON_SSH=${JETSON_SSH:-}       # empty = derived from --user/--ip/--eth below; set to override outright
# First camera's port; each further camera takes the next one up. NOT 8080:
# web_video_server owns that on the GS.
FIRST_PORT=${FIRST_PORT:-8090}
DEV_FILTER=${DEV_FILTER:-}             # empty = serve every configured/found camera
# Concrete camera list: name -> udev symlink (+ optional per-camera res/fps/
# quality). See cameras/cameras.yaml for the format. If its `cameras:` map is
# empty or missing, every /dev/video* node is auto-discovered instead (the
# old behaviour).
CONFIG=${CONFIG:-$REPO/cameras/cameras.yaml}
# Fallback runtime when the host python3 has no cv2 (see the note by CAM_RUNTIME below).
CAM_CONTAINER=${CAM_CONTAINER:-rover_prod}
CAM_CREMOTE=${CAM_CREMOTE:-/tmp/camera_mjpeg_server.py}
# What the B0495 offers depends on the USB speed it enumerated at:
#
#   480M (USB 2.0)  960x600 @ 10 only
#   5000M (USB 3.0) 1920x1200 @ 50/30/15, 960x600 @ 80/60/30/15 — no 10
#
# THE DEFAULT IS THE USB 2.0 MODE, DELIBERATELY, FOR EVERY CAMERA: SuperSpeed
# is not reliable with these cameras on this board (see the USB note in
# mast/README.md), so 960x600@10 is used even for a camera that enumerated at
# 5000M, because it makes camera_mjpeg_server.py pick the slowest, safest rate
# on offer. This does NOT force the USB link speed itself — that is decided by
# the cable and socket; a camera that comes up at 5000M is warned about below.
#
# It is also the CPU budget: 960x600@10 measured ~22% of a core per camera on
# this board, so four cameras fit; @30 would not.
DEFAULT_RES=${DEFAULT_RES:-960x600}
DEFAULT_FPS=${DEFAULT_FPS:-10}
DEFAULT_QUALITY=${DEFAULT_QUALITY:-80}   # JPEG quality; the camera gives no MJPEG to relay
# Capture pixel format (FourCC), e.g. YUYV, MJPG, GREY, UYVY. YUYV is the
# Arducam's only option; a different camera model (thermal, spectral, stereo)
# may need a different one — set per camera in --config.
DEFAULT_FORMAT=${DEFAULT_FORMAT:-YUYV}
# Dedicated remote directory (relative to $HOME) holding camera_mjpeg_server.py
# and, if present, a venv at .camera-venv with cv2+numpy — see CAM_VENV_PY below.
CAM_REMOTE_DIR=${CAM_REMOTE_DIR:-cameras}
SERVER_REMOTE_PATH=${SERVER_REMOTE_PATH:-}       # where the server lands; empty = remote $HOME/$CAM_REMOTE_DIR
# Preferred runtime: a venv on the host with cv2+numpy already installed
# (JetPack ships neither, and the rover has no route to fetch them, so this
# has to be built offline and copied on — see cameras/README.md). Checked
# before bare host python3, which is checked before the container fallback.
CAM_VENV_PY=${CAM_VENV_PY:-$CAM_REMOTE_DIR/.camera-venv/bin/python3}   # relative to remote $HOME

DRY=0
MODE=start

usage() {
    cat <<EOF
Serve the rover's Arducam(s) as MJPEG over HTTP, bypassing ROS. Idempotent.
See the header of this file for why this camera is not a ROS topic.

  ./cameras/start-cameras.sh            # probe, deploy, start, verify
  ./cameras/start-cameras.sh --probe    # report formats/resolutions, change nothing
  ./cameras/start-cameras.sh --stop
  ./cameras/start-cameras.sh --dry

Options
  --eth           use wired Ethernet ($JETSON_ETHERNET_IP) instead of the hotspot
  --user U        Jetson SSH username             (default: $JETSON_USER)
  --ip IP         Jetson IP/host, overrides --eth's default
                                          (default: $JETSON_HOTSPOT_IP, or $JETSON_ETHERNET_IP with --eth)
  --ssid SSID     Wi-Fi SSID of the Jetson hotspot (default: $WIFI_SSID)
  --pass PASS     Wi-Fi password for the hotspot   (default: $WIFI_PASS)
  --ssh U@H       rover's Jetson as user@host, overrides --user/--ip entirely
                                          (default: derived from --user/--ip/--eth)
  --port N        HTTP port of the FIRST camera; the rest take N+1, N+2 …
                                          (default: $FIRST_PORT)
  --config FILE   name -> udev symlink camera list (default: $CONFIG)
                  See cameras/cameras.yaml for the format. Missing file or empty
                  \`cameras:\` falls back to auto-discovering every /dev/video*.
  --dev LIST      comma-separated camera names or devices to serve
                  ('' = every configured/found camera) (default: ${DEV_FILTER:-all})
  --res WxH       capture resolution      (default: $DEFAULT_RES)
  --fps N         capture frame rate      (default: $DEFAULT_FPS)
                  Snapped to the nearest rate the camera offers, never faster
                  than asked. The default is the USB 2.0 mode on purpose, for
                  every camera; see the notes at the top of this file. Per-
                  camera overrides can also be set in --config.
  --quality N     JPEG quality 1-100      (default: $DEFAULT_QUALITY)
  --format FMT    capture pixel format (FourCC), e.g. YUYV, MJPG, GREY
                                          (default: $DEFAULT_FORMAT)
                  Per-camera overrides can also be set in --config.
  --probe | --stop | --dry
  -h, --help

Needs nothing installed: cameras/camera_mjpeg_server.py runs on host python3 if it
has OpenCV, else inside the rover_prod container, which does. No apt, no build.

Environment: every default above as an env var of the same name. Flags win.
EOF
    exit 0
}

while [ $# -gt 0 ]; do
    case "$1" in
        --eth)     USE_ETH=true; shift ;;
        --user)    JETSON_USER=$2; shift 2 ;;
        --ip)      JETSON_IP=$2; shift 2 ;;
        --ssid)    WIFI_SSID=$2; shift 2 ;;
        --pass)    WIFI_PASS=$2; shift 2 ;;
        --ssh)     JETSON_SSH=$2; shift 2 ;;
        --port)    FIRST_PORT=$2; shift 2 ;;
        --config)  CONFIG=$2; shift 2 ;;
        --dev)     DEV_FILTER=$2; shift 2 ;;
        --res)     DEFAULT_RES=$2; shift 2 ;;
        --fps)     DEFAULT_FPS=$2; shift 2 ;;
        --quality) DEFAULT_QUALITY=$2; shift 2 ;;
        --format)  DEFAULT_FORMAT=$2; shift 2 ;;
        --probe)   MODE=probe; shift ;;
        --stop)    MODE=stop; shift ;;
        --dry)     DRY=1; shift ;;
        -h|--help) usage ;;
        *) echo "unknown option: $1 (try --help)"; exit 1 ;;
    esac
done

[ -n "$JETSON_IP" ] || { [ "$USE_ETH" = true ] && JETSON_IP=$JETSON_ETHERNET_IP || JETSON_IP=$JETSON_HOTSPOT_IP; }
[ -n "$JETSON_SSH" ] || JETSON_SSH="${JETSON_USER}@${JETSON_IP}"

SERVER_SRC="$REPO/cameras/camera_mjpeg_server.py"

say()   { printf '  %s\n' "$*"; }
head_() { printf '\n\033[1m%s\033[0m\n' "$1"; }
did()   { printf '  \033[32m%s\033[0m %s\n' "changed" "$1"; }
ok()    { printf '  \033[2mok\033[0m %s\n' "$1"; }
warn()  { printf '  \033[33mWARN\033[0m  %s\n' "$1"; }
die()   { printf '  \033[31mFAIL\033[0m  %s\n' "$1"; exit 1; }

# -n matters: several loops below feed a camera list to `while read` on stdin
# and call ssh in the body. Without -n, ssh inherits that stdin and swallows the
# remaining lines, so only the FIRST camera is ever processed — silently, with
# no error. Nothing here ever pipes input to ssh, so -n costs nothing.
SSH="ssh -n -o BatchMode=yes -o StrictHostKeyChecking=accept-new -o ConnectTimeout=8"
JETSON_HOST=${JETSON_SSH#*@}

nexec() {
    if [ "$DRY" = 1 ]; then printf '  \033[2mwould run\033[0m %s\n' "$1"; return 0; fi
    $SSH "$JETSON_SSH" "$1"
}

# ================================================================== reach ==

head_ "Jetson at $JETSON_SSH"
if [ "$DRY" = 1 ]; then
    say "dry run — nothing will be touched"
else
    ensure_wifi_connection "$WIFI_SSID" "$WIFI_PASS" "$USE_ETH"
    # exits (not returns) on timeout, e.g. no ssh-copy-id done yet — see utils.sh
    wait_for_ssh "$JETSON_SSH" 30 "$USE_ETH"
    ok "$( $SSH "$JETSON_SSH" '. /etc/os-release; echo "$PRETTY_NAME $(uname -r)"' 2>/dev/null )"
    [ -n "$SERVER_REMOTE_PATH" ] || SERVER_REMOTE_PATH=$($SSH "$JETSON_SSH" 'echo $HOME' 2>/dev/null)/$CAM_REMOTE_DIR/camera_mjpeg_server.py
fi
[ -n "$SERVER_REMOTE_PATH" ] || SERVER_REMOTE_PATH="\$HOME/$CAM_REMOTE_DIR/camera_mjpeg_server.py"   # --dry, no remote to ask

# ================================================================== stop ==

if [ "$MODE" = stop ]; then
    nexec "pkill -f '[c]amera_mjpeg_server\.py' 2>/dev/null; \
           docker exec $CAM_CONTAINER pkill -f '[c]amera_mjpeg_server\.py' 2>/dev/null; \
           sleep 1; true"
    ok "stream stopped (host and $CAM_CONTAINER)"
    exit 0
fi

# ==================================================== find the cameras ==
#
# Two ways to find cameras, chosen by whether --config defines any:
#
#   configured  probe exactly the named symlinks in $CONFIG, one ssh call
#               each. Lets each physical camera keep a stable name and
#               resolution/fps/quality across replugs and node renumbering.
#   discovered  probe every /dev/video* node found (the original behaviour),
#               named after its device basename (e.g. "video0"). Used when
#               $CONFIG has no uncommented lines.
#
# Either way the result is CAMS, one line per working camera:
#   name|device|usb-path|usb-speed|product|res|fps|quality

head_ "Cameras"
nexec "pkill -f '[c]amera_mjpeg_server\.py' 2>/dev/null; sleep 1; true"

# Parsed locally with python3+PyYAML (already a repo dependency — see
# src/gs_joy) rather than a hand-rolled YAML reader in bash.
CONF_CAMS=""
if [ -f "$CONFIG" ]; then
    PARSED=$(python3 - "$CONFIG" <<'PY'
import sys
import yaml

path = sys.argv[1]
try:
    with open(path) as f:
        doc = yaml.safe_load(f) or {}
except Exception as exc:                            # noqa: BLE001
    print('ERROR|%s: %s' % (path, exc))
    sys.exit(0)

cams = doc.get('cameras') or {}
if not isinstance(cams, dict):
    print('ERROR|%s: top-level "cameras" must map name -> {device, ...}' % path)
    sys.exit(0)

for name, cfg in cams.items():
    cfg = cfg or {}
    device = cfg.get('device')
    if not device:
        print('ERROR|%s: camera "%s" has no device' % (path, name))
        sys.exit(0)
    print('OK|%s|%s|%s|%s|%s|%s' % (
        name, device, cfg.get('res', ''), cfg.get('fps', ''), cfg.get('quality', ''),
        cfg.get('format', '')))
PY
) || die "cannot parse $CONFIG — is PyYAML installed? (pip install pyyaml)"

    while IFS='|' read -r STATUS NAME DEV RES FPS QUALITY FORMAT; do
        [ -n "$STATUS" ] || continue
        [ "$STATUS" = ERROR ] && die "$NAME"
        [ -n "$RES" ] && [ "$RES" != "-" ] || RES=$DEFAULT_RES
        [ -n "$FPS" ] && [ "$FPS" != "-" ] || FPS=$DEFAULT_FPS
        [ -n "$QUALITY" ] && [ "$QUALITY" != "-" ] || QUALITY=$DEFAULT_QUALITY
        [ -n "$FORMAT" ] && [ "$FORMAT" != "-" ] || FORMAT=$DEFAULT_FORMAT
        CONF_CAMS="$CONF_CAMS$NAME|$DEV|$RES|$FPS|$QUALITY|$FORMAT
"
    done <<EOF
$PARSED
EOF
fi

# One ssh call per configured camera, checking the exact symlink named in
# $CONFIG (v4l2-ctl and the sysfs walk both follow it). Simpler and safer than
# folding a variable-length camera list into one remote script, and cheap: a
# handful of ssh round trips, not the hundreds this project scales to.
probe_camera() {
    NAME=$1 DEV=$2
    $SSH "$JETSON_SSH" "
        d='$DEV'
        if [ ! -e \"\$d\" ]; then
            echo 'SKIP|$NAME|$DEV||||not present (symlink missing — replugged, or udev rule not installed?)'
            exit 0
        fi
        real=\$(readlink -f \"\$d\")
        n=\$(basename \"\$real\")
        sys=\$(readlink -f /sys/class/video4linux/\$n/device/..)
        info=\"\$(basename \$sys)|\$(cat \$sys/speed 2>/dev/null)|\$(cat \$sys/product 2>/dev/null)\"
        if ! v4l2-ctl -d \"\$d\" --list-formats 2>/dev/null | grep -q 'Video Capture'; then
            echo \"SKIP|$NAME|$DEV|\$info|not a capture node\"
            exit 0
        fi
        # A node can enumerate and still refuse to stream (a camera that
        # trained at SuperSpeed and then wedged leaves a stale node behind).
        # Demand one real frame before believing in a camera.
        err=\$(timeout 8 v4l2-ctl -d \"\$d\" --stream-mmap --stream-count=1 2>&1)
        rc=\$?
        if [ \$rc -ne 0 ]; then
            why=\$(echo \"\$err\" | grep -m1 -i 'failed\|error' | tr -d '|')
            [ \$rc = 124 ] && why=\"timed out after 8s\${why:+ — \$why}\"
            echo \"SKIP|$NAME|$DEV|\$info|no frame: \${why:-VIDIOC_STREAMON did not succeed}\"
            exit 0
        fi
        echo \"OK|$NAME|$DEV|\$info\"
    " 2>/dev/null
}

if [ "$DRY" = 1 ]; then
    CAMS="dry-cam|/dev/videoDRY|2-1.1|5000|(dry run)|$DEFAULT_RES|$DEFAULT_FPS|$DEFAULT_QUALITY|$DEFAULT_FORMAT"
elif [ -n "$CONF_CAMS" ]; then
    CAMS="" REJECTED=""
    while IFS='|' read -r NAME DEV RES FPS QUALITY FORMAT; do
        [ -n "$NAME" ] || continue
        RESULT=$(probe_camera "$NAME" "$DEV")
        case "$RESULT" in
            OK\|*)   CAMS="$CAMS${RESULT#OK|}|$RES|$FPS|$QUALITY|$FORMAT
" ;;
            SKIP\|*) REJECTED="$REJECTED${RESULT#SKIP|}
" ;;
        esac
    done <<EOF
$CONF_CAMS
EOF
else
    CAMS=$($SSH "$JETSON_SSH" '
        for d in /dev/video*; do
            [ -e "$d" ] || continue
            n=$(basename "$d")
            sys=$(readlink -f /sys/class/video4linux/$n/device/..)
            info="$d|$(basename $sys)|$(cat $sys/speed 2>/dev/null)|$(cat $sys/product 2>/dev/null)"
            if ! v4l2-ctl -d "$d" --list-formats 2>/dev/null | grep -q "Video Capture"; then
                echo "SKIP|$info|not a capture node"
                continue
            fi
            err=$(timeout 8 v4l2-ctl -d "$d" --stream-mmap --stream-count=1 2>&1)
            rc=$?
            if [ $rc -ne 0 ]; then
                why=$(echo "$err" | grep -m1 -i "failed\|error" | tr -d "|")
                [ $rc = 124 ] && why="timed out after 8s${why:+ — $why}"
                echo "SKIP|$info|no frame: ${why:-VIDIOC_STREAMON did not succeed}"
                continue
            fi
            echo "OK|$info"
        done' 2>/dev/null)

    REJECTED=$(printf '%s\n' "$CAMS" | sed -n 's/^SKIP|//p')
    CAMS=$(printf '%s\n' "$CAMS" | sed -n 's/^OK|//p')
    # No --config: name each camera after its device basename (e.g. "video0"),
    # and give every one the same global res/fps/quality/format.
    CAMS=$(printf '%s\n' "$CAMS" | awk -F'|' -v res="$DEFAULT_RES" -v fps="$DEFAULT_FPS" \
        -v q="$DEFAULT_QUALITY" -v fmt="$DEFAULT_FORMAT" \
        'BEGIN{OFS="|"} NF{n=$1; sub(/^.*\//,"",n); print n,$1,$2,$3,$4,res,fps,q,fmt}')
    REJECTED=$(printf '%s\n' "$REJECTED" | awk -F'|' \
        'BEGIN{OFS="|"} NF{n=$1; sub(/^.*\//,"",n); print n,$1,$2,$3,$4,$5}')
fi

if [ "$DRY" != 1 ]; then
    # Report every rejected camera, not just when ALL of them failed — a
    # configured camera that silently drops out (present but not streaming)
    # is exactly the kind of failure an operator needs called out, even when
    # other cameras on the same run are fine.
    if [ -n "$REJECTED" ]; then
        warn "camera(s) configured/found, but not serving:"
        printf '%s\n' "$REJECTED" | while IFS='|' read -r NAME DEV UPATH SPEED CARD WHY; do
            [ -n "$NAME" ] || continue
            say "  $NAME  $DEV  ${CARD:-unknown}"
            if [ -n "$UPATH" ]; then
                say "    USB $UPATH at ${SPEED}M — $WHY"
            else
                say "    $WHY"
            fi
        done
        # A rejected node at 5000M is not a mystery, it is the known-bad case.
        if printf '%s\n' "$REJECTED" | awk -F'|' '$4 == 5000 { hit = 1 } END { exit !hit }'; then
            say ""
            say "At least one camera is at 5000M, which is the known-bad case on this"
            say "board. SuperSpeed here fails the UVC probe control with EPROTO:"
            say "  uvcvideo: Failed to set UVC probe control : -71"
            say "so the node never streams and looks exactly like an absent camera."
            say "Confirm with:  ssh $JETSON_SSH dmesg | grep -c -- -71"
            say ""
            say "Fixes, best first — software cannot force the link down to USB 2.0:"
            say "  1. re-cable: a USB 2.0 cable, or a socket with no external hub"
            say "  2. install the udev rule that deauthorises the SuperSpeed"
            say "     instance so the camera re-trains at 480M, then replug it:"
            say "       scp cameras/99-arducam-no-superspeed.rules $JETSON_SSH:/tmp/"
            say "       ssh $JETSON_SSH sudo cp /tmp/99-arducam-no-superspeed.rules /etc/udev/rules.d/"
            say "       ssh $JETSON_SSH sudo udevadm control --reload"
        fi
    fi

    if [ -z "$CAMS" ]; then
        if [ -n "$CONF_CAMS" ]; then
            die "none of the cameras in $CONFIG are present on the Jetson — check the symlinks and udev rules"
        else
            die "no /dev/video* on the Jetson at all — is the camera plugged in? (check lsusb)"
        fi
    fi

    # --dev restricts the set (by name or by device), so one camera can be
    # restarted without disturbing the others' streams.
    if [ -n "$DEV_FILTER" ]; then
        CAMS=$(printf '%s\n' "$CAMS" | awk -v want="$DEV_FILTER" -F'|' '
            BEGIN { n = split(want, a, ","); for (i = 1; i <= n; i++) keep[a[i]] = 1 }
            ($1 in keep) || ($2 in keep)')
        [ -n "$CAMS" ] || die "none of --dev '$DEV_FILTER' matches a configured/found camera (by name or device)"
    fi
fi

PORT=$FIRST_PORT
while IFS='|' read -r NAME DEV UPATH SPEED CARD RES FPS QUALITY FORMAT; do
    [ -n "$DEV" ] || continue
    say ""
    say "$NAME  ($DEV)  ->  port $PORT"
    say "  $CARD"
    say "  USB $UPATH at ${SPEED}M (5000 = USB 3.0, 480 = USB 2.0)"
    say "  capture $RES @ ${FPS}fps, $FORMAT, quality $QUALITY"

    # Hub depth is what actually breaks SuperSpeed here: cascaded hubs are fine
    # at 480M and fail at 5000M with EPROTO (-71), which shows up as flat green
    # frames. This board's four USB3-A sockets hang off a hub soldered to the
    # board, so a camera plugged straight in still reads one dot in the path —
    # hence the -1 below to count only EXTERNAL hubs.
    HUBS=$(printf '%s' "$UPATH" | tr -cd '.' | wc -c)
    EXTRA=$(( HUBS > 0 ? HUBS - 1 : 0 ))
    if [ "$SPEED" = "5000" ]; then
        warn "  running at SuperSpeed — the unreliable case on this board"
        if [ "$EXTRA" -gt 0 ]; then
            say "  Worse: behind $EXTRA external hub(s), the configuration that produces"
            say "  flat green frames and floods dmesg with"
            say "  'Non-zero status (-71) in video completion handler'."
        else
            say "  Hub-free, but that is not enough: a camera direct on the board at"
            say "  5000M still logged 824 -71 errors in one boot, serving ~19 fps"
            say "  against 30 with visible drops."
        fi
        say "  Software cannot force the link down to USB 2.0 — this kernel has no"
        say "  per-port SuperSpeed disable. Use a USB 2.0 cable or socket."
        say "  Serving it anyway, at the slowest mode it offers."
    fi

    if [ "$DRY" != 1 ]; then
        # The pixel-format header line looks like "[0]: 'YUYV' (YUYV 4:2:2)",
        # not "Pixel Format" — matching on that literal (as this used to)
        # silently dropped every format line, leaving Size/Interval blocks
        # with no way to tell which pixel format each one belongs to.
        $SSH "$JETSON_SSH" "v4l2-ctl -d $DEV --list-formats-ext 2>/dev/null" 2>/dev/null \
            | grep -E "^[[:space:]]*\[[0-9]+\]|Size: Discrete|Interval: Discrete" | sed 's/^/    /'
        if $SSH "$JETSON_SSH" "v4l2-ctl -d $DEV --list-formats 2>/dev/null" 2>/dev/null | grep -qi MJPG; then
            ok "  offers MJPEG — frames relayed, not re-encoded"
        else
            say "  no MJPEG — the Jetson encodes this one"
        fi
    fi
    PORT=$((PORT + 1))
done <<EOF
$CAMS
EOF

[ "$MODE" = probe ] && exit 0

# ================================================================= deploy ==
#
# One file, copied to the rover's Jetson. Nothing is installed:
# camera_mjpeg_server.py runs on the OpenCV and numpy that ship with JetPack.

head_ "Server"
# Preference order: host venv (built offline, cv2+numpy copied on since
# JetPack ships neither and the rover has no route to fetch them — see
# cameras/README.md) > bare host python3 > the rover_prod container. Host-
# native beats the container either way, because a container restart takes
# the streams with it.
CAM_RUNTIME=host
CAM_PY="python3 $SERVER_REMOTE_PATH"
if [ "$DRY" != 1 ]; then
    $SSH "$JETSON_SSH" "mkdir -p \$HOME/$CAM_REMOTE_DIR" 2>/dev/null
    scp -q -o BatchMode=yes -o StrictHostKeyChecking=accept-new \
        "$SERVER_SRC" "$JETSON_SSH:$SERVER_REMOTE_PATH" || die "scp of camera_mjpeg_server.py failed"

    # Errors are captured, not thrown away, so a failing venv/import shows
    # its real reason (missing .so, wrong path, ...) instead of a bare "no cv2".
    VENV_ERR=$($SSH "$JETSON_SSH" "test -x \$HOME/$CAM_VENV_PY || echo 'not found or not executable: \$HOME/$CAM_VENV_PY'; \$HOME/$CAM_VENV_PY -c 'import cv2, numpy' 2>&1" 2>&1)
    if [ -z "$VENV_ERR" ]; then
        ok "host venv ($CAM_VENV_PY) has cv2 + numpy"
        CAM_PY="\$HOME/$CAM_VENV_PY $SERVER_REMOTE_PATH"
        did "deployed $SERVER_REMOTE_PATH"
    else
        warn "host venv ($CAM_VENV_PY) rejected:"
        printf '%s\n' "$VENV_ERR" | sed 's/^/       /'
        HOST_ERR=$($SSH "$JETSON_SSH" 'python3 -c "import cv2, numpy"' 2>&1)
        if [ -z "$HOST_ERR" ]; then
            ok "host python3 has cv2 + numpy, nothing to install"
            did "deployed $SERVER_REMOTE_PATH"
        elif $SSH "$JETSON_SSH" "docker exec $CAM_CONTAINER python3 -c 'import cv2, numpy'" 2>/dev/null; then
            CAM_RUNTIME=container
            CAM_PY="docker exec -d $CAM_CONTAINER python3 $CAM_CREMOTE"
            warn "no cv2 in bare host python3 either - falling back to the $CAM_CONTAINER container"
            say  "     $HOST_ERR"
            $SSH "$JETSON_SSH" "docker cp $SERVER_REMOTE_PATH $CAM_CONTAINER:$CAM_CREMOTE" >/dev/null 2>&1 \
                || die "docker cp of camera_mjpeg_server.py into $CAM_CONTAINER failed"
            did "deployed $CAM_CONTAINER:$CAM_CREMOTE (via $SERVER_REMOTE_PATH)"
        else
            die "no python3 with cv2+numpy in the host venv, bare host python3, or $CAM_CONTAINER"
        fi
    fi
else
    say "would copy $SERVER_SRC -> $JETSON_SSH:$SERVER_REMOTE_PATH"
fi

# ================================================================== start ==
#
# One server process per camera, each on its own port: a wedged camera then
# takes down only its own feed, and can be restarted with --dev without
# interrupting the other's stream, and each encode gets its own core.

head_ "Streams"

PORT=$FIRST_PORT
STARTED=""
while IFS='|' read -r NAME DEV UPATH SPEED CARD RES FPS QUALITY FORMAT; do
    [ -n "$DEV" ] || continue
    LOG=/tmp/camera_mjpeg_$NAME.log
    # Container mode uses `docker exec -d`, which is already detached and
    # cannot redirect to a host path, so the nohup/redirect wrapper is
    # host-only. Container logs come from `docker logs`/the exec's own stdout.
    if [ "${CAM_RUNTIME:-host}" = container ]; then
        nexec "$CAM_PY \
            --device $DEV --width ${RES%x*} --height ${RES#*x} \
            --fps $FPS --quality $QUALITY --format $FORMAT --port $PORT --name $NAME"
    else
        nexec "nohup $CAM_PY \
            --device $DEV --width ${RES%x*} --height ${RES#*x} \
            --fps $FPS --quality $QUALITY --format $FORMAT --port $PORT --name $NAME \
            >$LOG 2>&1 & true"
    fi
    STARTED="$STARTED$NAME|$DEV|$PORT|$LOG
"
    PORT=$((PORT + 1))
done <<EOF
$CAMS
EOF

if [ "$DRY" = 1 ]; then
    printf '%s' "$STARTED" | while IFS='|' read -r NAME DEV PORT LOG; do
        [ -n "$DEV" ] && say "would serve $NAME ($DEV) on http://$JETSON_HOST:$PORT/$NAME?action=stream"
    done
    exit 0
fi

# Cameras negotiate and the first frames take a moment; wait once for all of
# them rather than sleeping per camera.
sleep 4

# ================================================================= verify ==
#
# From the GS, not the Jetson: a stream that only works on loopback is the
# failure this catches. Headers only — an MJPEG body never ends.

head_ "Verify from the GS"
FAILED=0
OK_URLS=""
printf '%s' "$STARTED" | while IFS='|' read -r NAME DEV PORT LOG; do
    [ -n "$DEV" ] || continue
    URL="http://$JETSON_HOST:$PORT/$NAME?action=stream"
    CT=$(curl -s -m 8 -o /dev/null -D - "$URL" 2>/dev/null | grep -i '^content-type:' | tr -d '\r')
    HEALTH=$(curl -s -m 5 "http://$JETSON_HOST:$PORT/health" 2>/dev/null)
    # Content-Type alone is NOT enough: the HTTP server comes up whether or not
    # the camera ever opened, so a wedged camera answers with a good header and
    # zero frames. A feed serving nothing must never read as healthy.
    FRAMES=$(printf '%s' "$HEALTH" | sed -n 's/.*frames=\([0-9]*\).*/\1/p')
    if printf '%s' "$CT" | grep -qi 'multipart/x-mixed-replace' && [ "${FRAMES:-0}" -gt 0 ]; then
        ok "$NAME  $HEALTH"
        say "     $URL"
    elif printf '%s' "$CT" | grep -qi 'multipart/x-mixed-replace'; then
        warn "$NAME ($DEV) on port $PORT is serving NO FRAMES — $HEALTH"
        say  "     the server is up but the camera never opened; last log lines:"
        $SSH "$JETSON_SSH" "tail -4 $LOG 2>/dev/null" 2>/dev/null | sed 's/^/       /'
    else
        warn "$NAME ($DEV) on port $PORT is not serving MJPEG at all"
        say  "     on the Jetson: tail $LOG"
        $SSH "$JETSON_SSH" "tail -6 $LOG 2>/dev/null" 2>/dev/null | sed 's/^/       /'
    fi
done

printf '\n  \033[1mPoint UI tiles at:\033[0m\n'
printf '%s' "$STARTED" | while IFS='|' read -r NAME DEV PORT LOG; do
    [ -n "$DEV" ] && printf '    %-16s %s\n' "$NAME" "http://$JETSON_HOST:$PORT/$NAME?action=stream"
done
say ""
say "Settings dialog -> Cameras -> paste one into each row's \"Image topic / URL\" box."
say "Single frames, for a quick eyeball: swap action=stream for action=snapshot."

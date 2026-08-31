#!/bin/bash
# Serve the rover's Arducam(s) as plain MJPEG over HTTP, no ROS.
#
#     ./mast/start-cameras.sh               # probe, deploy, start, verify
#     ./mast/start-cameras.sh --probe       # only report what the camera offers
#     ./mast/start-cameras.sh --stop
#     ./mast/start-cameras.sh --dry
#
# Every other camera on this project publishes over ROS 2 (v4l2/gscam node ->
# Fast DDS -> web_video_server on the GS). This camera bypasses that: its host
# has no working cv2 for a ROS node to relay through, but the Arducam B0495 is
# a driverless UVC device the stock uvcvideo handles fine, so
# mast/camera_mjpeg_server.py captures it directly and serves MJPEG over HTTP,
# and a UI camera tile points straight at that URL. The trade: this feed is
# outside ROS — no rosbag recording, no per-frame timestamps, and the UI's
# `ros` transport mode cannot carry it (see isDirectUrl() in ui/src/config.js).
#
# Point a camera tile at the URL this prints: open the UI settings dialog and
# type it into the "Image topic / URL" box of any row.
set -uo pipefail

# ================================================================ defaults ==

NANO_SSH=${NANO_SSH:-indomitus-rover@10.42.0.1}
# First camera's port; each further camera takes the next one up. NOT 8080:
# web_video_server owns that on the GS.
NANO_PORT=${NANO_PORT:-8090}
NANO_DEV=${NANO_DEV:-}             # empty = serve every capture-capable node
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
NANO_RES=${NANO_RES:-960x600}
NANO_FPS=${NANO_FPS:-10}
NANO_QUALITY=${NANO_QUALITY:-80}   # JPEG quality; the camera gives no MJPEG to relay
NANO_REMOTE=${NANO_REMOTE:-}       # where the server lands; empty = remote $HOME

DRY=0
MODE=start

usage() {
    cat <<EOF
Serve the rover's Arducam(s) as MJPEG over HTTP, bypassing ROS. Idempotent.
See the header of this file for why this camera is not a ROS topic.

  ./mast/start-cameras.sh            # probe, deploy, start, verify
  ./mast/start-cameras.sh --probe    # report formats/resolutions, change nothing
  ./mast/start-cameras.sh --stop
  ./mast/start-cameras.sh --dry

Options
  --ssh U@H       rover's Jetson, over the link  (default: $NANO_SSH)
  --port N        HTTP port of the FIRST camera; the rest take N+1, N+2 …
                                          (default: $NANO_PORT)
  --dev LIST      comma-separated /dev/videoN to serve
                  ('' = every capture-capable node) (default: ${NANO_DEV:-all})
  --res WxH       capture resolution      (default: $NANO_RES)
  --fps N         capture frame rate      (default: $NANO_FPS)
                  Snapped to the nearest rate the camera offers, never faster
                  than asked. The default is the USB 2.0 mode on purpose, for
                  every camera; see the notes at the top of this file.
  --quality N     JPEG quality 1-100      (default: $NANO_QUALITY)
  --probe | --stop | --dry
  -h, --help

Needs nothing installed: mast/camera_mjpeg_server.py runs on host python3 if it
has OpenCV, else inside the rover_prod container, which does. No apt, no build.

Environment: every default above as an env var of the same name. Flags win.
EOF
    exit 0
}

while [ $# -gt 0 ]; do
    case "$1" in
        --ssh)     NANO_SSH=$2; shift 2 ;;
        --port)    NANO_PORT=$2; shift 2 ;;
        --dev)     NANO_DEV=$2; shift 2 ;;
        --res)     NANO_RES=$2; shift 2 ;;
        --fps)     NANO_FPS=$2; shift 2 ;;
        --quality) NANO_QUALITY=$2; shift 2 ;;
        --probe)   MODE=probe; shift ;;
        --stop)    MODE=stop; shift ;;
        --dry)     DRY=1; shift ;;
        -h|--help) usage ;;
        *) echo "unknown option: $1 (try --help)"; exit 1 ;;
    esac
done

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SERVER_SRC="$REPO/mast/camera_mjpeg_server.py"

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
NANO_HOST=${NANO_SSH#*@}

nexec() {
    if [ "$DRY" = 1 ]; then printf '  \033[2mwould run\033[0m %s\n' "$1"; return 0; fi
    $SSH "$NANO_SSH" "$1"
}

# ================================================================== reach ==

head_ "Jetson at $NANO_SSH"
if [ "$DRY" = 1 ]; then
    say "dry run — nothing will be touched"
elif ! $SSH "$NANO_SSH" true 2>/dev/null; then
    die "cannot ssh to $NANO_SSH (ssh-copy-id $NANO_SSH, or set --ssh)"
else
    ok "$( $SSH "$NANO_SSH" '. /etc/os-release; echo "$PRETTY_NAME $(uname -r)"' 2>/dev/null )"
    [ -n "$NANO_REMOTE" ] || NANO_REMOTE=$($SSH "$NANO_SSH" 'echo $HOME' 2>/dev/null)/camera_mjpeg_server.py
fi
[ -n "$NANO_REMOTE" ] || NANO_REMOTE='$HOME/camera_mjpeg_server.py'   # --dry, no remote to ask

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
# Every capture-capable node, not just the first: this rover carries two
# arducams and will carry more. No udev rules on this box, so node numbers are
# whatever uvcvideo assigned and are NOT stable across replugs — hence
# rediscovery on every run rather than a remembered /dev/videoN.

head_ "Cameras"
nexec "pkill -f '[c]amera_mjpeg_server\.py' 2>/dev/null; sleep 1; true"
if [ "$DRY" = 1 ]; then
    CAMS="/dev/videoDRY|2-1.1|5000|(dry run)"
else
    CAMS=$($SSH "$NANO_SSH" '
        for d in /dev/video*; do
            [ -e "$d" ] || continue
            n=$(basename "$d")
            sys=$(readlink -f /sys/class/video4linux/$n/device/..)
            info="$d|$(basename $sys)|$(cat $sys/speed 2>/dev/null)|$(cat $sys/product 2>/dev/null)"
            if ! v4l2-ctl -d "$d" --list-formats 2>/dev/null | grep -q "Video Capture"; then
                echo "SKIP|$info|not a capture node"
                continue
            fi
            # A node can enumerate and still refuse to stream (a camera that
            # trained at SuperSpeed and then wedged leaves a stale node
            # behind). Demand one real frame before believing in a camera.
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

    # Split the two answers apart: what will be served, and what was found but
    # rejected. The rejected list is the whole point of the reporting below.
    REJECTED=$(printf '%s\n' "$CAMS" | sed -n 's/^SKIP|//p')
    CAMS=$(printf '%s\n' "$CAMS" | sed -n 's/^OK|//p')

    if [ -z "$CAMS" ]; then
        if [ -z "$REJECTED" ]; then
            die "no /dev/video* on the Jetson at all — is the camera plugged in? (check lsusb)"
        fi
        warn "video nodes exist, but none of them produced a frame:"
        printf '%s\n' "$REJECTED" | while IFS='|' read -r DEV UPATH SPEED CARD WHY; do
            [ -n "$DEV" ] || continue
            say "  $DEV  ${CARD:-unknown}"
            say "    USB $UPATH at ${SPEED}M — $WHY"
        done
        # A rejected node at 5000M is not a mystery, it is the known-bad case.
        if printf '%s\n' "$REJECTED" | awk -F'|' '$3 == 5000 { hit = 1 } END { exit !hit }'; then
            say ""
            say "At least one node is at 5000M, which is the known-bad case on this"
            say "board. SuperSpeed here fails the UVC probe control with EPROTO:"
            say "  uvcvideo: Failed to set UVC probe control : -71"
            say "so the node never streams and looks exactly like an absent camera."
            say "Confirm with:  ssh $NANO_SSH dmesg | grep -c -- -71"
            say ""
            say "Fixes, best first — software cannot force the link down to USB 2.0:"
            say "  1. re-cable: a USB 2.0 cable, or a socket with no external hub"
            say "  2. install the udev rule that deauthorises the SuperSpeed"
            say "     instance so the camera re-trains at 480M, then replug it:"
            say "       scp mast/99-arducam-no-superspeed.rules $NANO_SSH:/tmp/"
            say "       ssh $NANO_SSH sudo cp /tmp/99-arducam-no-superspeed.rules /etc/udev/rules.d/"
            say "       ssh $NANO_SSH sudo udevadm control --reload"
        fi
        die "no camera on the Jetson produced a frame"
    fi

    # --dev restricts the set, so one camera can be restarted without disturbing
    # the other's stream.
    if [ -n "$NANO_DEV" ]; then
        CAMS=$(printf '%s\n' "$CAMS" | awk -v want="$NANO_DEV" -F'|' '
            BEGIN { n = split(want, a, ","); for (i = 1; i <= n; i++) keep[a[i]] = 1 }
            $1 in keep')
        [ -n "$CAMS" ] || die "none of --dev '$NANO_DEV' is a capture-capable node on the Jetson"
    fi
fi

PORT=$NANO_PORT
while IFS='|' read -r DEV UPATH SPEED CARD; do
    [ -n "$DEV" ] || continue
    say ""
    say "$DEV  ->  port $PORT"
    say "  $CARD"
    say "  USB $UPATH at ${SPEED}M (5000 = USB 3.0, 480 = USB 2.0)"

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
        $SSH "$NANO_SSH" "v4l2-ctl -d $DEV --list-formats-ext 2>/dev/null" 2>/dev/null \
            | grep -E 'Pixel Format|Size: Discrete|fps' | sed 's/^/    /'
        if $SSH "$NANO_SSH" "v4l2-ctl -d $DEV --list-formats 2>/dev/null" 2>/dev/null | grep -qi MJPG; then
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
# Where python3 has cv2. Host first; the container is the fallback, not the
# preference, because a container restart takes the streams with it.
CAM_RUNTIME=host
CAM_PY="python3 $NANO_REMOTE"
if [ "$DRY" != 1 ]; then
    if $SSH "$NANO_SSH" 'python3 -c "import cv2, numpy"' 2>/dev/null; then
        ok "host python3 has cv2 + numpy, nothing to install"
        scp -q -o BatchMode=yes -o StrictHostKeyChecking=accept-new \
            "$SERVER_SRC" "$NANO_SSH:$NANO_REMOTE" || die "scp of camera_mjpeg_server.py failed"
        did "deployed $NANO_REMOTE"
    elif $SSH "$NANO_SSH" "docker exec $CAM_CONTAINER python3 -c 'import cv2, numpy'" 2>/dev/null; then
        CAM_RUNTIME=container
        CAM_PY="docker exec -d $CAM_CONTAINER python3 $CAM_CREMOTE"
        warn "host python3 has no cv2 - falling back to the $CAM_CONTAINER container"
        say  "  (JetPack 6 ships no cv2 binding and the rover has no route to an apt archive)"
        scp -q -o BatchMode=yes -o StrictHostKeyChecking=accept-new \
            "$SERVER_SRC" "$NANO_SSH:$NANO_REMOTE" || die "scp of camera_mjpeg_server.py failed"
        $SSH "$NANO_SSH" "docker cp $NANO_REMOTE $CAM_CONTAINER:$CAM_CREMOTE" >/dev/null 2>&1 \
            || die "docker cp of camera_mjpeg_server.py into $CAM_CONTAINER failed"
        did "deployed $CAM_CONTAINER:$CAM_CREMOTE (via $NANO_REMOTE)"
    else
        die "no python3 with cv2+numpy on the host or in $CAM_CONTAINER"
    fi
else
    say "would copy $SERVER_SRC -> $NANO_SSH:$NANO_REMOTE"
fi

# ================================================================== start ==
#
# One server process per camera, each on its own port: a wedged camera then
# takes down only its own feed, and can be restarted with --dev without
# interrupting the other's stream, and each encode gets its own core.

head_ "Streams"

PORT=$NANO_PORT
STARTED=""
while IFS='|' read -r DEV UPATH SPEED CARD; do
    [ -n "$DEV" ] || continue
    LOG=/tmp/camera_mjpeg_$(basename "$DEV").log
    # Container mode uses `docker exec -d`, which is already detached and
    # cannot redirect to a host path, so the nohup/redirect wrapper is
    # host-only. Container logs come from `docker logs`/the exec's own stdout.
    if [ "${CAM_RUNTIME:-host}" = container ]; then
        nexec "$CAM_PY \
            --device $DEV --width ${NANO_RES%x*} --height ${NANO_RES#*x} \
            --fps $NANO_FPS --quality $NANO_QUALITY --port $PORT"
    else
        nexec "nohup $CAM_PY \
            --device $DEV --width ${NANO_RES%x*} --height ${NANO_RES#*x} \
            --fps $NANO_FPS --quality $NANO_QUALITY --port $PORT \
            >$LOG 2>&1 & true"
    fi
    STARTED="$STARTED$DEV|$PORT|$LOG
"
    PORT=$((PORT + 1))
done <<EOF
$CAMS
EOF

if [ "$DRY" = 1 ]; then
    printf '%s' "$STARTED" | while IFS='|' read -r DEV PORT LOG; do
        [ -n "$DEV" ] && say "would serve $DEV on http://$NANO_HOST:$PORT/?action=stream"
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
printf '%s' "$STARTED" | while IFS='|' read -r DEV PORT LOG; do
    [ -n "$DEV" ] || continue
    URL="http://$NANO_HOST:$PORT/?action=stream"
    CT=$(curl -s -m 8 -o /dev/null -D - "$URL" 2>/dev/null | grep -i '^content-type:' | tr -d '\r')
    HEALTH=$(curl -s -m 5 "http://$NANO_HOST:$PORT/health" 2>/dev/null)
    # Content-Type alone is NOT enough: the HTTP server comes up whether or not
    # the camera ever opened, so a wedged camera answers with a good header and
    # zero frames. A feed serving nothing must never read as healthy.
    FRAMES=$(printf '%s' "$HEALTH" | sed -n 's/.*frames=\([0-9]*\).*/\1/p')
    if printf '%s' "$CT" | grep -qi 'multipart/x-mixed-replace' && [ "${FRAMES:-0}" -gt 0 ]; then
        ok "$DEV  $HEALTH"
        say "     $URL"
    elif printf '%s' "$CT" | grep -qi 'multipart/x-mixed-replace'; then
        warn "$DEV on port $PORT is serving NO FRAMES — $HEALTH"
        say  "     the server is up but the camera never opened; last log lines:"
        $SSH "$NANO_SSH" "tail -4 $LOG 2>/dev/null" 2>/dev/null | sed 's/^/       /'
    else
        warn "$DEV on port $PORT is not serving MJPEG at all"
        say  "     on the Jetson: tail $LOG"
        $SSH "$NANO_SSH" "tail -6 $LOG 2>/dev/null" 2>/dev/null | sed 's/^/       /'
    fi
done

printf '\n  \033[1mPoint UI tiles at:\033[0m\n'
printf '%s' "$STARTED" | while IFS='|' read -r DEV PORT LOG; do
    [ -n "$DEV" ] && printf '    %s\n' "http://$NANO_HOST:$PORT/?action=stream"
done
say ""
say "Settings dialog -> Cameras -> paste one into each row's \"Image topic / URL\" box."
say "Single frames, for a quick eyeball: swap action=stream for action=snapshot."

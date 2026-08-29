#!/bin/bash
# Serve the Nano's Arducam as plain MJPEG over HTTP, no ROS.
#
#     ./mast/nano-camera.sh                 # probe, deploy, start, verify
#     ./mast/nano-camera.sh --probe         # only report what the camera offers
#     ./mast/nano-camera.sh --stop
#     ./mast/nano-camera.sh --dry
#
# WHY THIS EXISTS, instead of the rover's ROS path.
#
# Every other camera on this project reaches the console the same way: a
# v4l2/gscam node publishes sensor_msgs/Image, Fast DDS carries it over the
# link, and web_video_server on the GS re-serves it as MJPEG for the UI tiles.
# That needs ROS 2 Humble on the camera's machine.
#
# This Jetson is a Nano 4GB on JetPack 4.5.1 — Ubuntu 18.04, kernel
# 4.9.201-tegra, L4T R32.5.2. **Humble has no binaries for 18.04**, so there is
# no v4l2_camera_node to run. That is the whole difference from the Orin NX,
# where the arducams were ordinary ROS topics.
#
# The camera itself is unaffected: the Arducam B0495 is a driverless UVC device
# the stock uvcvideo handles. So mast/nano_mjpeg.py captures it and serves MJPEG
# over HTTP, and a UI camera tile points straight at that URL.
#
# The trade, stated plainly: this feed is outside ROS. No rosbag recording, no
# per-frame timestamps, and the UI's `ros` transport mode cannot carry it (the
# tile stays on HTTP — see isDirectUrl() in ui/src/config.js). Switch gating
# still works, because that is decided in the browser. If this camera ever has
# to be a real ROS topic, the route is a container, and the image is
# dustynv/ros:humble-ros-base-l4t-r32.7.1 — Humble built from source on an L4T
# r32 base. Do NOT reach for a stock Jammy image: on kernel 4.9 it dies with
# `error adding seccomp filter rule for syscall clone3`.
#
# Point a camera tile at the URL this prints: open the UI settings dialog and
# type it into the "Image topic / URL" box of any row.
set -uo pipefail

# ================================================================ defaults ==

NANO_SSH=${NANO_SSH:-jetson@10.42.0.1}
# First camera's port; each further camera takes the next one up. NOT 8080:
# web_video_server owns that on the GS.
NANO_PORT=${NANO_PORT:-8090}
NANO_DEV=${NANO_DEV:-}             # empty = serve every capture-capable node
# What the B0495 offers depends on the USB speed it enumerated at, and the two
# lists share no frame rate at all:
#
#   480M (USB 2.0)  960x600 @ 10 only
#   5000M (USB 3.0) 1920x1200 @ 50/30/15, 960x600 @ 80/60/30/15 — no 10
#
# so a request carried across a re-cabling selects a rate that does not exist.
# nano_mjpeg.py snaps whatever is asked for onto a mode the camera actually has
# and says so in its log, rather than failing in a way that looks like a broken
# camera. Neither speed offers MJPEG, so the Nano always encodes.
#
# THE DEFAULT IS THE USB 2.0 MODE, DELIBERATELY, FOR EVERY CAMERA.
#
# SuperSpeed is not reliable with these cameras on this board — see the USB note
# in mast/README.md — so the safe mode is the policy even for a camera that
# enumerated at 5000M and advertises better. Asking for 960x600@10 makes
# nano_mjpeg.py pick the slowest rate on offer (10 at 480M, 15 at 5000M), which
# is the most conservative thing the camera will do.
#
# Note what this does NOT do: nothing here can force the USB *link* speed. That
# is decided by the cable and the socket, and kernel 4.9 on this Tegra exposes
# no per-port SuperSpeed disable. A camera that comes up at 5000M is warned
# about below; the fix is a USB 2.0 cable or a different socket.
#
# It is also the CPU budget. Encode is single-threaded and this board has four
# A57s with one already spoken for: 960x600@10 measured ~22% of a core per
# camera, so four cameras fit. @30 is ~70% each and would not.
NANO_RES=${NANO_RES:-960x600}
NANO_FPS=${NANO_FPS:-10}
NANO_QUALITY=${NANO_QUALITY:-80}   # JPEG quality; the camera gives no MJPEG to relay
NANO_REMOTE=${NANO_REMOTE:-}       # where the server lands; empty = remote $HOME

DRY=0
MODE=up

usage() {
    cat <<EOF
Serve the Nano's Arducam as MJPEG over HTTP, bypassing ROS. Idempotent.
See the header of this file for why this camera is not a ROS topic.

  ./mast/nano-camera.sh              # probe, deploy, start, verify
  ./mast/nano-camera.sh --probe      # report formats/resolutions, change nothing
  ./mast/nano-camera.sh --stop
  ./mast/nano-camera.sh --dry

Options
  --ssh U@H       Nano over the link      (default: $NANO_SSH)
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

Needs nothing installed on the Nano: mast/nano_mjpeg.py runs on the OpenCV and
numpy that ship with JetPack. No apt, no build, no sudo.

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
SERVER_SRC="$REPO/mast/nano_mjpeg.py"

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

head_ "Nano at $NANO_SSH"
if [ "$DRY" = 1 ]; then
    say "dry run — nothing will be touched"
elif ! $SSH "$NANO_SSH" true 2>/dev/null; then
    die "cannot ssh to $NANO_SSH (ssh-copy-id $NANO_SSH, or set --ssh)"
else
    ok "$( $SSH "$NANO_SSH" '. /etc/os-release; echo "$PRETTY_NAME $(uname -r)"' 2>/dev/null )"
    # Derive from the remote home rather than hard-coding a user: this box logs
    # in as `jetson`, the rover as `indomitus-rover`.
    [ -n "$NANO_REMOTE" ] || NANO_REMOTE=$($SSH "$NANO_SSH" 'echo $HOME' 2>/dev/null)/nano_mjpeg.py
fi
[ -n "$NANO_REMOTE" ] || NANO_REMOTE='$HOME/nano_mjpeg.py'   # --dry, no remote to ask

# ================================================================== stop ==

if [ "$MODE" = stop ]; then
    nexec "pkill -f '[n]ano_mjpeg\.py' 2>/dev/null; sleep 1; true"
    ok "stream stopped"
    exit 0
fi

# ==================================================== find the cameras ==
#
# Every capture-capable node, not just the first: this rover carries two
# arducams and will carry more. No udev rules on this box, so node numbers are
# whatever uvcvideo assigned and are NOT stable across replugs — hence
# rediscovery on every run rather than a remembered /dev/videoN.
#
# The sysfs walk (video4linux/<n>/device/..) is what ties a node to its USB
# device, which is the only way to see the link speed and hub depth that decide
# whether the camera will actually work here.

# Stop every server before discovery, not just before starting. Two reasons:
# the previous run's device/port assignment is not necessarily this run's
# (node numbers move across replugs), and the streaming test below would
# otherwise find the device busy with our own server and skip a good camera.
head_ "Cameras"
nexec "pkill -f '[n]ano_mjpeg\.py' 2>/dev/null; sleep 1; true"
if [ "$DRY" = 1 ]; then
    CAMS="/dev/videoDRY|2-1.1|5000|(dry run)"
else
    CAMS=$($SSH "$NANO_SSH" '
        for d in /dev/video*; do
            [ -e "$d" ] || continue
            v4l2-ctl -d "$d" --list-formats 2>/dev/null | grep -q "Video Capture" || continue
            # Enumerating is not the same as working. A camera that trained at
            # SuperSpeed and then wedged leaves a STALE node behind: the kernel
            # never tears it down because the device went dark without a clean
            # disconnect, so it still answers descriptors from cache while
            # VIDIOC_STREAMON returns EIO. The same physical camera then
            # re-appears on the USB 2.0 wires as a second node. Serving the
            # ghost wastes a port and — worse — shifts the port of every
            # camera after it, which silently breaks the UI tile mapping.
            # So demand one real frame before believing in a camera.
            # (No apostrophes in here: this whole block is a single-quoted
            # string handed to ssh, and one would end it.)
            timeout 8 v4l2-ctl -d "$d" --stream-mmap --stream-count=1 >/dev/null 2>&1 || continue
            n=$(basename "$d")
            sys=$(readlink -f /sys/class/video4linux/$n/device/..)
            echo "$d|$(basename $sys)|$(cat $sys/speed 2>/dev/null)|$(cat $sys/product 2>/dev/null)"
        done' 2>/dev/null)
    [ -n "$CAMS" ] || die "no capture-capable /dev/video* on the Nano — are the cameras plugged in? (check lsusb)"

    # --dev restricts the set, so one camera can be restarted without disturbing
    # the other's stream.
    if [ -n "$NANO_DEV" ]; then
        CAMS=$(printf '%s\n' "$CAMS" | awk -v want="$NANO_DEV" -F'|' '
            BEGIN { n = split(want, a, ","); for (i = 1; i <= n; i++) keep[a[i]] = 1 }
            $1 in keep')
        [ -n "$CAMS" ] || die "none of --dev '$NANO_DEV' is a capture-capable node on the Nano"
    fi
fi

PORT=$NANO_PORT
while IFS='|' read -r DEV UPATH SPEED CARD; do
    [ -n "$DEV" ] || continue
    say ""
    say "$DEV  ->  port $PORT"
    say "  $CARD"
    say "  USB $UPATH at ${SPEED}M (5000 = USB 3.0, 480 = USB 2.0)"

    # Hub depth is what actually breaks SuperSpeed here. Cascaded hubs are fine
    # at 480M and fail at 5000M with EPROTO (-71) on the isochronous video
    # endpoint, which reaches the operator as flat green frames with the
    # occasional real one — an unfilled YUYV buffer decodes to green.
    #
    # Each dot in the path is one hub, but the FIRST is free: this Jetson Nano
    # devkit's four USB3-A sockets hang off a VIA Labs hub soldered to the board
    # (2109:0817), so a camera plugged straight into the Nano still reads N-M.X.
    # Only dots beyond that are external hubs — hence the -1.
    HUBS=$(printf '%s' "$UPATH" | tr -cd '.' | wc -c)
    EXTRA=$(( HUBS > 0 ? HUBS - 1 : 0 ))
    if [ "$SPEED" = "5000" ]; then
        warn "  running at SuperSpeed — the unreliable case on this board"
        if [ "$EXTRA" -gt 0 ]; then
            say "  Worse: behind $EXTRA external hub(s), the configuration that produces"
            say "  flat green frames and floods dmesg with"
            say "  'Non-zero status (-71) in video completion handler'."
        else
            say "  Hub-free, but that is not enough: a camera direct on the Nano at"
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
        # No MJPEG means the Nano encodes. Worth saying out loud per camera,
        # because it is the whole CPU budget and it differs by USB speed.
        if $SSH "$NANO_SSH" "v4l2-ctl -d $DEV --list-formats 2>/dev/null" 2>/dev/null | grep -qi MJPG; then
            ok "  offers MJPEG — frames relayed, not re-encoded"
        else
            say "  no MJPEG — the Nano encodes this one"
        fi
    fi
    PORT=$((PORT + 1))
done <<EOF
$CAMS
EOF

[ "$MODE" = probe ] && exit 0

# ================================================================= deploy ==
#
# One file, copied to the Nano. Nothing is installed: nano_mjpeg.py runs on the
# OpenCV and numpy that ship with JetPack, which is why this needs no sudo.
#
# mjpg-streamer was the obvious candidate and was tried first. It builds here
# but segfaults inside input_uvc before it opens the device; its advantage would
# have been relaying MJPEG untouched, which this camera does not offer anyway.

head_ "Server"
if [ "$DRY" != 1 ]; then
    MISSING=$($SSH "$NANO_SSH" 'python3 -c "import cv2, numpy" 2>&1 >/dev/null || echo missing' 2>/dev/null)
    [ -z "$MISSING" ] || die "python3 cv2/numpy not importable on the Nano — expected from JetPack: $MISSING"
    ok "python3 + cv2 + numpy present, nothing to install"
    scp -q -o BatchMode=yes -o StrictHostKeyChecking=accept-new \
        "$SERVER_SRC" "$NANO_SSH:$NANO_REMOTE" || die "scp of nano_mjpeg.py failed"
    did "deployed $NANO_REMOTE"
else
    say "would copy $SERVER_SRC -> $NANO_SSH:$NANO_REMOTE"
fi

# ================================================================== start ==
#
# One server process per camera, each on its own port. Separate processes rather
# than one multi-camera server: a camera that wedges — which these do, see the
# hub note above — then takes down only its own feed, and can be restarted with
# --dev without interrupting the other's stream. It also puts each encode on its
# own core, which is the point on a 4-core board.

head_ "Streams"

PORT=$NANO_PORT
STARTED=""
while IFS='|' read -r DEV UPATH SPEED CARD; do
    [ -n "$DEV" ] || continue
    LOG=/tmp/nano_mjpeg_$(basename "$DEV").log
    nexec "nohup python3 $NANO_REMOTE \
        --device $DEV --width ${NANO_RES%x*} --height ${NANO_RES#*x} \
        --fps $NANO_FPS --quality $NANO_QUALITY --port $PORT \
        >$LOG 2>&1 & true"
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
# From the GS, not the Nano: a stream that only works on loopback is the failure
# this catches. Headers only — an MJPEG body never ends.

head_ "Verify from the GS"
FAILED=0
OK_URLS=""
printf '%s' "$STARTED" | while IFS='|' read -r DEV PORT LOG; do
    [ -n "$DEV" ] || continue
    URL="http://$NANO_HOST:$PORT/?action=stream"
    CT=$(curl -s -m 8 -o /dev/null -D - "$URL" 2>/dev/null | grep -i '^content-type:' | tr -d '\r')
    HEALTH=$(curl -s -m 5 "http://$NANO_HOST:$PORT/health" 2>/dev/null)
    # Content-Type alone is NOT enough. The HTTP server comes up whether or not
    # the camera ever opened, so a wedged camera answers with a perfectly good
    # multipart header and zero frames — which reported as "ok" until this
    # checked the frame count. A feed serving nothing must never read as healthy.
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
        say  "     on the Nano: tail $LOG"
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

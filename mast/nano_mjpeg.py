#!/usr/bin/env python3
"""Serve a V4L2 camera as MJPEG over HTTP. Runs on the Jetson Nano.

WHY THIS EXISTS instead of mjpg-streamer, and instead of ROS.

Not ROS: this Nano is JetPack 4.5.1 — Ubuntu 18.04, kernel 4.9.201-tegra. ROS 2
Humble has no binaries for 18.04, so there is no v4l2_camera_node to publish
with. See the header of mast/nano-camera.sh for the full reasoning.

Not mjpg-streamer: it builds cleanly here but segfaults inside input_uvc before
it opens the device, on a camera that offers exactly one mode. Debugging a
third-party C plugin on an EOL platform is not the shortest path to video, and
the usual reason to prefer it — relaying MJPEG frames untouched — does not apply
to this camera anyway. The Arducam B0495 on this box offers only YUYV, so
something has to encode regardless. OpenCV 4.1.1 and numpy ship with JetPack, so
this needs nothing installed.

Cost: 960x600 at 10 fps is ~5.8 MPix/s of JPEG encode, a fraction of one A57
core. That headroom is the whole reason this is affordable — it would not be at
1080p30, and if the camera is ever moved to a real USB 3.0 port (it is currently
on a 480M path, which is why it offers one mode) this should be re-measured.

Each frame is encoded ONCE and handed to every connected client, so a second
viewer costs bandwidth but no extra CPU. Capture runs in its own thread and
never blocks on a slow client: clients are always served the newest frame, and a
viewer that cannot keep up drops frames rather than stalling the camera.

URL paths deliberately match mjpg-streamer's, because the UI derives the
snapshot URL from the stream URL by swapping action=stream for action=snapshot
(see snapshotUrl() in ui/src/config.js):

    /?action=stream     multipart/x-mixed-replace, the camera tiles
    /?action=snapshot   one JPEG, the thumbnail strip
    /health             plain text, for scripts
"""

import argparse
import re
import socketserver
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse

import cv2

BOUNDARY = 'nanomjpegframe'


def enumerate_modes(device):
    """{(w, h): [fps, ...]} the device actually offers, via v4l2-ctl.

    OpenCV will accept any width/height/fps you hand it and then quietly give
    you something else, so ask V4L2 directly instead of trusting the setters.
    """
    try:
        out = subprocess.check_output(
            ['v4l2-ctl', '-d', device, '--list-formats-ext'],
            stderr=subprocess.STDOUT).decode('utf-8', 'replace')
    except Exception as exc:                    # noqa: BLE001
        print('cannot enumerate modes (%s); using the requested one as given'
              % exc, file=sys.stderr, flush=True)
        return {}

    modes = {}
    size = None
    for line in out.splitlines():
        m = re.search(r'Size: Discrete (\d+)x(\d+)', line)
        if m:
            size = (int(m.group(1)), int(m.group(2)))
            modes.setdefault(size, [])
            continue
        m = re.search(r'\(([\d.]+) fps\)', line)
        if m and size:
            modes[size].append(float(m.group(1)))
    return modes


def pick_mode(modes, want_w, want_h, want_fps):
    """Snap a request onto a mode the camera has. Returns (w, h, fps, note).

    This matters because the same camera offers different modes at different
    USB speeds: on a 480M path the B0495 gives 960x600 at 10 fps and nothing
    else, while on a 5000M path it gives 15/30/60/80 and no 10 at all. A request
    carried over from one to the other selects a rate that does not exist, and
    the capture then fails in a way that looks like a broken camera.
    """
    if not modes:
        return want_w, want_h, want_fps, 'unverified (no format list)'

    notes = []
    if (want_w, want_h) in modes:
        size = (want_w, want_h)
    else:
        # Largest on offer: if the requested size is gone, the operator would
        # rather have the best picture available than no picture.
        size = max(modes, key=lambda s: s[0] * s[1])
        notes.append('%dx%d not offered' % (want_w, want_h))

    rates = sorted(modes[size])
    if want_fps in rates:
        fps = want_fps
    else:
        # Highest rate at or below the request, else the slowest on offer.
        # Never silently run the camera faster than was asked for.
        below = [r for r in rates if r <= want_fps]
        fps = below[-1] if below else rates[0]
        notes.append('%g fps not offered (have %s)'
                     % (want_fps, '/'.join('%g' % r for r in rates)))

    return size[0], size[1], fps, '; '.join(notes) or 'exact'


class Camera:
    """Grabs frames in the background, holding only the newest JPEG.

    Deliberately last-frame-wins rather than a queue: for a live view a backlog
    is worse than a gap, and an unbounded queue on a 4GB board is a slow leak.
    """

    def __init__(self, device, width, height, fps, quality):
        self.device = device
        self.width = width
        self.height = height
        self.fps = fps
        self.quality = int(quality)

        self._jpeg = None
        self._seq = 0
        self._cond = threading.Condition()
        self._stop = threading.Event()
        self.mode = 'unopened'
        self.frames = 0
        self.errors = 0
        self.started = time.time()

    def open(self):
        # Snap onto a real mode before touching OpenCV; see pick_mode().
        w, h, fps, note = pick_mode(enumerate_modes(self.device),
                                    self.width, self.height, self.fps)
        if note != 'exact':
            print('requested %dx%d @%g -> using %dx%d @%g (%s)'
                  % (self.width, self.height, self.fps, w, h, fps, note),
                  flush=True)
        self.width, self.height, self.fps = w, h, fps

        cap = cv2.VideoCapture(self.device, cv2.CAP_V4L2)
        if not cap.isOpened():
            raise RuntimeError('cannot open %s' % self.device)
        # The B0495 offers YUYV only. Setting FOURCC explicitly stops OpenCV
        # from negotiating something the camera will refuse.
        cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*'YUYV'))
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, self.width)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self.height)
        cap.set(cv2.CAP_PROP_FPS, self.fps)
        # Report what we actually got, not what we asked for — a camera that
        # silently substitutes a mode is otherwise invisible until the picture
        # looks wrong.
        got = (int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)),
               int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT)),
               cap.get(cv2.CAP_PROP_FPS))
        self.mode = '%dx%d@%.0f' % got
        print('camera open: %dx%d @ %.0f fps, jpeg q%d'
              % (got[0], got[1], got[2], self.quality), flush=True)
        return cap

    def run(self):
        cap = None
        while not self._stop.is_set():
            try:
                if cap is None:
                    cap = self.open()
                ok, frame = cap.read()
                if not ok:
                    raise RuntimeError('read failed')
                ok, buf = cv2.imencode(
                    '.jpg', frame, [int(cv2.IMWRITE_JPEG_QUALITY), self.quality])
                if not ok:
                    raise RuntimeError('encode failed')
                with self._cond:
                    self._jpeg = buf.tobytes()
                    self._seq += 1
                    self.frames += 1
                    self._cond.notify_all()
            except Exception as exc:            # noqa: BLE001 - keep serving
                # A USB camera on a hub chain does drop out. Reopen rather than
                # exit, so the feed comes back on its own instead of needing
                # someone to ssh in.
                self.errors += 1
                print('capture error: %s (reopening)' % exc, file=sys.stderr, flush=True)
                if cap is not None:
                    cap.release()
                    cap = None
                time.sleep(1.0)
        if cap is not None:
            cap.release()

    def latest(self, since, timeout=5.0):
        """Newest frame and its sequence, once it is newer than `since`."""
        with self._cond:
            if self._seq <= since:
                self._cond.wait(timeout)
            return self._jpeg, self._seq

    def stop(self):
        self._stop.set()


class Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.0'
    camera = None

    def log_message(self, fmt, *args):
        pass        # one line per frame is not a log, it is a denial of service

    def do_GET(self):
        parsed = urlparse(self.path)
        action = parse_qs(parsed.query).get('action', [''])[0]

        if parsed.path == '/health':
            self._health()
        elif action == 'snapshot':
            self._snapshot()
        elif action == 'stream' or parsed.path in ('/', '/stream'):
            self._stream()
        else:
            self.send_error(404)

    def _health(self):
        cam = self.camera
        up = time.time() - cam.started
        body = ('ok mode=%s frames=%d errors=%d uptime=%.0fs fps=%.1f\n'
                % (cam.mode, cam.frames, cam.errors, up,
                   cam.frames / up if up else 0)).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'text/plain')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _snapshot(self):
        jpeg, _ = self.camera.latest(-1)
        if jpeg is None:
            self.send_error(503, 'no frame yet')
            return
        self.send_response(200)
        self.send_header('Content-Type', 'image/jpeg')
        self.send_header('Content-Length', str(len(jpeg)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(jpeg)

    def _stream(self):
        self.send_response(200)
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Content-Type',
                         'multipart/x-mixed-replace; boundary=' + BOUNDARY)
        self.end_headers()
        seq = -1
        try:
            while True:
                jpeg, seq = self.camera.latest(seq)
                if jpeg is None:
                    continue
                self.wfile.write(b'--' + BOUNDARY.encode() + b'\r\n')
                self.wfile.write(b'Content-Type: image/jpeg\r\n')
                self.wfile.write(b'Content-Length: %d\r\n\r\n' % len(jpeg))
                self.wfile.write(jpeg)
                self.wfile.write(b'\r\n')
        except (BrokenPipeError, ConnectionResetError):
            pass        # the operator closed a tile; not an error


class Server(socketserver.ThreadingMixIn, HTTPServer):
    # Python 3.6 on this image has no http.server.ThreadingHTTPServer.
    daemon_threads = True
    allow_reuse_address = True


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--device', default='/dev/video0')
    ap.add_argument('--width', type=int, default=960)
    ap.add_argument('--height', type=int, default=600)
    ap.add_argument('--fps', type=int, default=10)
    ap.add_argument('--quality', type=int, default=80, help='JPEG quality 1-100')
    ap.add_argument('--port', type=int, default=8090)
    ap.add_argument('--bind', default='0.0.0.0')
    args = ap.parse_args()

    cam = Camera(args.device, args.width, args.height, args.fps, args.quality)
    threading.Thread(target=cam.run, daemon=True).start()

    Handler.camera = cam
    server = Server((args.bind, args.port), Handler)
    print('serving http://%s:%d/?action=stream' % (args.bind, args.port), flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        cam.stop()


if __name__ == '__main__':
    main()

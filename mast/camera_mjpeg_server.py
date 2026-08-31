#!/usr/bin/env python3
"""Serve a V4L2 camera as MJPEG over HTTP. Runs on the rover's Jetson.

See the header of mast/start-cameras.sh for why this exists instead of ROS.

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

BOUNDARY = 'cameramjpegframe'


def enumerate_modes(device):
    """{(w, h): [fps, ...]} the device actually offers, via v4l2-ctl."""
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
    """Snap a request onto a mode the camera has. Returns (w, h, fps, note)."""
    if not modes:
        return want_w, want_h, want_fps, 'unverified (no format list)'

    notes = []
    if (want_w, want_h) in modes:
        size = (want_w, want_h)
    else:
        # Requested size is gone: fall back to the largest one on offer.
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
    """Grabs frames in the background, holding only the newest JPEG."""

    def __init__(self, device, width, height, fps, quality, name=None):
        self.device = device
        self.width = width
        self.height = height
        self.fps = fps
        self.quality = int(quality)
        self.name = name or device

        self._jpeg = None
        self._seq = 0
        self._cond = threading.Condition()
        self._stop = threading.Event()
        self.mode = 'unopened'
        self.frames = 0
        self.errors = 0
        self.started = time.time()

    def open(self):
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
        cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*'YUYV'))
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, self.width)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self.height)
        cap.set(cv2.CAP_PROP_FPS, self.fps)
        # Report what the camera actually gave us, not what was asked for.
        got = (int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)),
               int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT)),
               cap.get(cv2.CAP_PROP_FPS))
        self.mode = '%dx%d@%.0f' % got
        print('%s: camera open: %dx%d @ %.0f fps, jpeg q%d'
              % (self.name, got[0], got[1], got[2], self.quality), flush=True)
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
                # Reopen rather than exit, so a dropped camera recovers on its
                # own instead of needing someone to ssh in and restart it.
                self.errors += 1
                print('%s: capture error: %s (reopening)' % (self.name, exc),
                      file=sys.stderr, flush=True)
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
        body = ('ok name=%s mode=%s frames=%d errors=%d uptime=%.0fs fps=%.1f\n'
                % (cam.name, cam.mode, cam.frames, cam.errors, up,
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
    ap.add_argument('--name', default=None,
                     help='camera name, for logs and /health (default: --device)')
    args = ap.parse_args()

    cam = Camera(args.device, args.width, args.height, args.fps, args.quality, args.name)
    threading.Thread(target=cam.run, daemon=True).start()

    Handler.camera = cam
    server = Server((args.bind, args.port), Handler)
    print('%s: serving http://%s:%d/%s?action=stream'
          % (cam.name, args.bind, args.port, args.name or ''), flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        cam.stop()


if __name__ == '__main__':
    main()

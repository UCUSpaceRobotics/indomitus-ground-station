#!/usr/bin/env python3
"""Serve a V4L2 camera as MJPEG over HTTP. Runs on the rover's Jetson.

See the header of cameras/start-cameras.sh for why this exists instead of ROS.

Capture is viewer-counted: the camera stays closed and idle until a stream
connects, and closes again once the last one disconnects, so a camera nobody
is watching costs no USB bandwidth or CPU (ui/src/lib/frameMirror.js shows
each camera's last frame in the UI's thumbnail strip without opening a
connection at all).

    /?action=stream     multipart/x-mixed-replace, opens/keeps the camera live
    /?action=snapshot   one JPEG from whatever is currently captured, if any
    /capture            save one full-quality frame to --save-dir on this host
    /record?on=1|0      save one frame every --record-interval seconds, or stop
    /health             plain text, for scripts

/capture and /record are the dataset side: they write the captured frame at
--save-quality, independent of the (often heavily compressed) stream quality,
and they hold the camera open themselves, so they work with nobody watching.
The rover's DualSense (rover_teleop's joystick_interpreter_node) and the UI's
dataset controls both drive them, and both read the state back from /health.
"""

import argparse
import os
import queue
import re
import socketserver
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse

import cv2
import numpy as np

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


def split_stereo(frame, view):
    """Cut a side-by-side stereo frame (left|right eye, concatenated on the
    width) down to one eye. ZED-style cameras report only the combined
    resolution over UVC — there is no separate mode for a single eye, so this
    always runs on the full frame, not something the camera can do itself.
    """
    if view == 'both':
        return frame
    half = frame.shape[1] // 2
    eye = frame[:, :half] if view == 'left' else frame[:, half:]
    # The slice is a non-contiguous view (a row stride, not a copy); cv2.imencode
    # needs contiguous memory, so make one copy of half the data here rather
    # than let imencode do it implicitly (or fail) downstream.
    return np.ascontiguousarray(eye)


class Camera:
    """Grabs frames in the background, holding only the newest JPEG."""

    def __init__(self, device, width, height, fps, quality, fourcc='YUYV', name=None,
                 view='both'):
        self.device = device
        self.width = width
        self.height = height
        self.fps = fps
        self.quality = int(quality)
        self.fourcc = fourcc
        self.name = name or device
        self.view = view

        self._jpeg = None
        self._frame = None
        self._seq = 0
        self._cond = threading.Condition()
        self._stop = threading.Event()
        self._viewers = 0
        self.mode = 'idle'
        self.frames = 0
        self.errors = 0
        self.started = time.time()

    @property
    def viewers(self):
        return self._viewers

    def add_viewer(self):
        with self._cond:
            self._viewers += 1
            self._cond.notify_all()

    def remove_viewer(self):
        with self._cond:
            self._viewers = max(0, self._viewers - 1)
            self._cond.notify_all()

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
        if len(self.fourcc) != 4:
            raise RuntimeError('--format must be a 4-character FourCC, got %r' % self.fourcc)
        cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*self.fourcc))
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, self.width)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self.height)
        cap.set(cv2.CAP_PROP_FPS, self.fps)
        # Report what the camera actually gave us, not what was asked for.
        got = (int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)),
               int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT)),
               cap.get(cv2.CAP_PROP_FPS))
        self.mode = '%dx%d@%.0f' % got
        note = ' (%s eye)' % self.view if self.view != 'both' else ''
        print('%s: camera open: %dx%d @ %.0f fps, %s, jpeg q%d%s'
              % (self.name, got[0], got[1], got[2], self.fourcc, self.quality, note), flush=True)
        return cap

    def run(self):
        cap = None
        while not self._stop.is_set():
            # No viewers: stay closed and idle rather than capturing for
            # nobody. Woken by add_viewer() or stop().
            with self._cond:
                while self._viewers == 0 and not self._stop.is_set():
                    self._cond.wait()
            if self._stop.is_set():
                break

            try:
                if cap is None:
                    cap = self.open()
                ok, frame = cap.read()
                if not ok:
                    raise RuntimeError('read failed')
                frame = split_stereo(frame, self.view)
                ok, buf = cv2.imencode(
                    '.jpg', frame, [int(cv2.IMWRITE_JPEG_QUALITY), self.quality])
                if not ok:
                    raise RuntimeError('encode failed')
                with self._cond:
                    self._jpeg = buf.tobytes()
                    self._frame = frame
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
                continue

            if self._viewers == 0 and cap is not None:
                cap.release()
                cap = None
                self.mode = 'idle'
        if cap is not None:
            cap.release()

    def latest(self, since, timeout=5.0):
        """Newest frame and its sequence, once it is newer than `since`."""
        with self._cond:
            if self._seq <= since:
                self._cond.wait(timeout)
            return self._jpeg, self._seq

    def fresh_frame(self, since, timeout):
        """First raw frame captured after sequence `since`, or None on timeout.

        Unlike latest(), keeps waiting through wake-ups that bring no new frame
        (add_viewer() notifies too), so a closed camera gets the time it needs
        to open.
        """
        deadline = time.time() + timeout
        with self._cond:
            while self._seq <= since:
                left = deadline - time.time()
                if left <= 0:
                    return None
                self._cond.wait(left)
            return self._frame

    @property
    def seq(self):
        return self._seq

    def stop(self):
        self._stop.set()
        with self._cond:
            self._cond.notify_all()


class Dataset:
    """Saves raw frames to disk at full JPEG quality: on request, or on a timer.

    Encoding and disk writes happen on a worker thread fed by a queue, never in
    the capture loop, so saving cannot stall the stream. While recording, and
    for the moment a capture takes, this holds the camera open by counting
    itself as a viewer — the same mechanism a stream uses.
    """

    def __init__(self, camera, save_dir, quality, interval):
        self.camera = camera
        self.save_dir = save_dir
        self.quality = int(quality)
        self.interval = float(interval)
        self.recording = False
        self.saved = 0
        self._since = 0                 # sequence of the last frame recorded
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._queue = queue.Queue(maxsize=64)
        threading.Thread(target=self._writer, daemon=True).start()
        threading.Thread(target=self._recorder, daemon=True).start()

    def capture(self, timeout=3.0):
        """Save the next frame; returns its path, or None if none arrived."""
        cam = self.camera
        since = cam.seq
        cam.add_viewer()
        try:
            frame = cam.fresh_frame(since, timeout)
        finally:
            cam.remove_viewer()
        return None if frame is None else self._enqueue(frame)

    def set_recording(self, on):
        with self._lock:
            if on == self.recording:
                return
            self.recording = on
            if on:
                # Whatever is in the buffer predates this recording (it may be
                # left over from a stream that closed long ago): wait for new.
                self._since = self.camera.seq
                self.camera.add_viewer()
            else:
                self.camera.remove_viewer()
        print('%s: recording %s (every %gs to %s)'
              % (self.camera.name, 'ON' if on else 'OFF', self.interval, self.save_dir),
              flush=True)
        self._wake.set()

    def _recorder(self):
        while True:
            self._wake.wait(self.interval)
            self._wake.clear()
            if not self.recording:
                continue
            # A frame is only worth saving once: skip a tick the camera has not
            # produced anything new for (still opening, or a read error).
            seq = self.camera.seq
            frame = self.camera.fresh_frame(self._since, 0) if seq > self._since else None
            if frame is not None:
                self._since = seq
                self._enqueue(frame)

    def _enqueue(self, frame):
        # Named at capture time, not write time, so a backed-up queue cannot
        # skew the timestamps.
        now = time.time()
        stamp = time.strftime('%Y%m%d-%H%M%S', time.localtime(now))
        name = self.camera.name
        path = os.path.join(self.save_dir, time.strftime('%Y-%m-%d', time.localtime(now)),
                            name, '%s_%s-%03d.jpg' % (name, stamp, int(now * 1000) % 1000))
        try:
            self._queue.put_nowait((path, frame))
        except queue.Full:
            print('%s: dataset write queue full, dropping %s' % (name, path),
                  file=sys.stderr, flush=True)
            return None
        return path

    def _writer(self):
        while True:
            path, frame = self._queue.get()
            try:
                ok, buf = cv2.imencode(
                    '.jpg', frame, [int(cv2.IMWRITE_JPEG_QUALITY), self.quality])
                if not ok:
                    raise RuntimeError('encode failed')
                os.makedirs(os.path.dirname(path), exist_ok=True)
                with open(path, 'wb') as f:
                    f.write(buf.tobytes())
                self.saved += 1
            except Exception as exc:            # noqa: BLE001 - keep saving
                print('%s: cannot save %s: %s' % (self.camera.name, path, exc),
                      file=sys.stderr, flush=True)


class Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.0'
    camera = None
    dataset = None

    def log_message(self, fmt, *args):
        pass        # one line per frame is not a log, it is a denial of service

    def do_GET(self):
        parsed = urlparse(self.path)
        action = parse_qs(parsed.query).get('action', [''])[0]

        if parsed.path == '/health':
            self._health()
        elif parsed.path == '/capture':
            self._capture()
        elif parsed.path == '/record':
            self._record(parse_qs(parsed.query).get('on', [''])[0])
        elif action == 'snapshot':
            self._snapshot()
        elif action == 'stream' or parsed.path in ('/', '/stream'):
            self._stream()
        else:
            self.send_error(404)

    def _health(self):
        # Doesn't call add_viewer(): a health check must not itself keep an
        # otherwise-idle camera capturing.
        cam = self.camera
        ds = self.dataset
        up = time.time() - cam.started
        self._text(200, 'ok name=%s mode=%s viewers=%d frames=%d errors=%d uptime=%.0fs '
                        'fps=%.1f recording=%d saved=%d\n'
                   % (cam.name, cam.mode, cam.viewers, cam.frames, cam.errors, up,
                      cam.frames / up if up else 0, ds.recording, ds.saved))

    def _capture(self):
        path = self.dataset.capture()
        if path is None:
            self._text(503, 'error no frame from %s\n' % self.camera.name)
        else:
            self._text(200, 'ok saved=%s\n' % path)

    def _record(self, on):
        if on not in ('0', '1'):
            self._text(400, 'error on must be 1 or 0\n')
            return
        self.dataset.set_recording(on == '1')
        self._text(200, 'ok recording=%d\n' % self.dataset.recording)

    def _text(self, code, text):
        body = text.encode()
        self.send_response(code)
        self.send_header('Content-Type', 'text/plain')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        # Read cross-origin by the UI's dataset controls; same reasoning as
        # the header on _snapshot().
        self.send_header('Access-Control-Allow-Origin', '*')
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
        # The ground station UI runs on a different origin (its own dev/preview
        # port) and reads this response with fetch() to save a frame to disk
        # (ui/src/lib/saveFrame.js) — without this header the browser's CORS
        # check blocks it from ever seeing the bytes, and the UI has no way to
        # ask for the header only when it is the one asking, so this is always
        # on. A snapshot carries nothing sensitive, so the wildcard is fine.
        self.send_header('Access-Control-Allow-Origin', '*')
        self.end_headers()
        self.wfile.write(jpeg)

    def _stream(self):
        # The connection itself is the "someone is watching" signal.
        self.camera.add_viewer()
        try:
            self.send_response(200)
            self.send_header('Cache-Control', 'no-store')
            self.send_header('Content-Type',
                             'multipart/x-mixed-replace; boundary=' + BOUNDARY)
            self.end_headers()
            seq = -1
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
        finally:
            self.camera.remove_viewer()


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
    ap.add_argument('--format', default='YUYV',
                     help='capture pixel format (FourCC), e.g. YUYV, MJPG, GREY')
    ap.add_argument('--port', type=int, default=8090)
    ap.add_argument('--bind', default='0.0.0.0')
    ap.add_argument('--name', default=None,
                     help='camera name, for logs and /health (default: --device)')
    ap.add_argument('--view', default='both', choices=('left', 'right', 'both'),
                     help='for a side-by-side stereo camera (e.g. ZED2i), which eye '
                          'to serve; the camera itself has no such mode, so this '
                          'crops the joined frame in software (default: both, unsplit)')
    ap.add_argument('--save-dir', default=os.path.expanduser('~/dataset'),
                     help='where /capture and /record write frames, as '
                          '<dir>/<date>/<name>/<name>_<time>.jpg (default: ~/dataset)')
    ap.add_argument('--save-quality', type=int, default=95,
                     help='JPEG quality of saved frames, independent of --quality')
    ap.add_argument('--record-interval', type=float, default=1.0,
                     help='seconds between frames while recording (default: 1)')
    args = ap.parse_args()

    cam = Camera(args.device, args.width, args.height, args.fps, args.quality,
                 fourcc=args.format, name=args.name, view=args.view)
    threading.Thread(target=cam.run, daemon=True).start()

    Handler.camera = cam
    Handler.dataset = Dataset(cam, args.save_dir, args.save_quality, args.record_interval)
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

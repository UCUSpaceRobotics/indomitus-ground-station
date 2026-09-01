/**
 * Per-camera "last frame" store, shared across every mounted CameraFeed.
 *
 * Whenever a camera's live MJPEG stream is open somewhere (the focused pane,
 * or a grid tile), each incoming frame is also painted onto that camera's
 * entry here. A thumbnail elsewhere just displays this — it never opens its
 * own connection, so an unfocused camera costs nothing: no request, no
 * rover-side capture or encode (see the viewer-counted capture in
 * cameras/camera_mjpeg_server.py). It simply shows the last frame the camera
 * happened to produce while something else was actually watching it, frozen
 * until that's true again.
 */

const canvases = new Map();   // cameraId -> offscreen HTMLCanvasElement
const listeners = new Map();  // cameraId -> Set<() => void>

function getMirrorCanvas(cameraId) {
  let canvas = canvases.get(cameraId);
  if (!canvas) {
    canvas = document.createElement('canvas');
    canvases.set(cameraId, canvas);
  }
  return canvas;
}

/** Called by the live stream on every frame it loads. */
export function paintMirror(cameraId, source) {
  const w = source.naturalWidth || source.width;
  const h = source.naturalHeight || source.height;
  if (!w || !h) return;

  const canvas = getMirrorCanvas(cameraId);
  if (canvas.width !== w || canvas.height !== h) {
    canvas.width = w;
    canvas.height = h;
  }
  canvas.getContext('2d').drawImage(source, 0, 0, w, h);
  for (const notify of listeners.get(cameraId) || []) notify();
}

/** Copies the current mirror content onto `target`. Returns true if it drew anything. */
export function drawMirror(cameraId, target) {
  const canvas = canvases.get(cameraId);
  if (!canvas || !canvas.width || !canvas.height) return false;
  if (target.width !== canvas.width || target.height !== canvas.height) {
    target.width = canvas.width;
    target.height = canvas.height;
  }
  target.getContext('2d').drawImage(canvas, 0, 0);
  return true;
}

/** Subscribes to new frames for one camera. Returns an unsubscribe function. */
export function subscribeMirror(cameraId, onFrame) {
  let set = listeners.get(cameraId);
  if (!set) listeners.set(cameraId, (set = new Set()));
  set.add(onFrame);
  return () => set.delete(onFrame);
}

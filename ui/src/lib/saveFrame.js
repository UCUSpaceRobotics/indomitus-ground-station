import { ensureWritePermission, getScreenshotDirHandle, screenshotsSupported } from './screenshotDir';

function pad(n) {
  return String(n).padStart(2, '0');
}

/** `<camera name>_YYYY-MM-DD_HH-MM-SS.<ext>`, safe on every filesystem. */
function frameFilename(cameraName, ext) {
  const d = new Date();
  const date = `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
  const time = `${pad(d.getHours())}-${pad(d.getMinutes())}-${pad(d.getSeconds())}`;
  const safeName = String(cameraName).trim().replace(/[^a-zA-Z0-9_-]+/g, '_') || 'camera';
  return `${safeName}_${date}_${time}.${ext}`;
}

/** Decodes a base64 image payload (as carried by CompressedImage over rosbridge) into a Blob. */
export function blobFromBase64(base64, mime) {
  const bytes = atob(base64);
  const out = new Uint8Array(bytes.length);
  for (let i = 0; i < bytes.length; i += 1) out[i] = bytes.charCodeAt(i);
  return new Blob([out], { type: mime });
}

/**
 * Saves a blob via a plain `<a download>` click on a `blob:` URL. `blob:` is
 * always same-origin to the page that created it, so — unlike a direct link
 * to the (cross-origin, no-CORS) camera server — every browser honours the
 * `download` attribute and saves it straight away. No `window.open`, no
 * picker dialog, no navigation: nothing here can replace the tab or pop
 * anything over the video, which matters with an operator driving off it.
 */
function downloadBlob(blob, filename) {
  const url = URL.createObjectURL(blob);
  const link = document.createElement('a');
  link.href = url;
  link.download = filename;
  link.rel = 'noopener';
  link.style.display = 'none';
  document.body.appendChild(link);
  link.click();
  link.remove();
  setTimeout(() => URL.revokeObjectURL(url), 10_000);
}

async function writeToDir(dirHandle, filename, blob) {
  if (!(await ensureWritePermission(dirHandle))) {
    throw new Error('Permission to write to the screenshot folder was denied.');
  }
  const fileHandle = await dirHandle.getFileHandle(filename, { create: true });
  const writable = await fileHandle.createWritable();
  await writable.write(blob);
  await writable.close();
}

/**
 * Saves a frame the page already holds as a Blob — the ROS transport's frame,
 * decoded straight from its base64 payload with no canvas involved. Writes
 * into the folder configured in Settings when one is set; otherwise falls
 * back to a browser download if `allowDownloadFallback` is on.
 */
export async function saveFrameBlob(blob, cameraName, ext, { allowDownloadFallback = false } = {}) {
  const filename = frameFilename(cameraName, ext);
  const dirHandle = screenshotsSupported() ? await getScreenshotDirHandle() : null;
  if (dirHandle) {
    await writeToDir(dirHandle, filename, blob);
    return { filename, viaFallback: false };
  }
  if (!allowDownloadFallback) {
    throw new Error(
      screenshotsSupported()
        ? 'No screenshot folder set — pick one in Settings.'
        : 'This browser cannot save to a folder — enable the download fallback in Settings.',
    );
  }
  downloadBlob(blob, filename);
  return { filename, viaFallback: true };
}

/**
 * Saves an MJPEG frame by fetching a single still snapshot from `snapshotUrl`
 * (see config.js's `snapshotUrl()`).
 *
 * This needs the camera server's snapshot response to carry
 * `Access-Control-Allow-Origin`, since the UI reads it from a different
 * origin (its own dev/preview port). `cameras/camera_mjpeg_server.py` (the
 * Nano cameras) sends it; a stock `web_video_server` does not, and there is
 * no client-side way around that — every trick that avoids asking the
 * server's permission (drawing the frame through a `<canvas>`, linking
 * straight to the cross-origin URL with `download`) either throws a
 * SecurityError when read back or gets treated as a plain navigation instead
 * of a download, which would replace this tab with the JPEG. Neither is
 * worth risking on a console someone may be driving from, so a blocked fetch
 * is reported as an error instead of attempting either.
 */
export async function saveMjpegFrame(snapshotUrl, cameraName, opts = {}) {
  let blob;
  try {
    const response = await fetch(snapshotUrl, { cache: 'no-store' });
    if (!response.ok) throw new Error(`Camera server returned HTTP ${response.status}.`);
    blob = await response.blob();
  } catch {
    throw new Error(
      "Can't read this camera's frame to save it — its server does not allow this page to read " +
        "it (missing CORS headers). Right-click the video and \"Save Image As\" still works.",
    );
  }
  return saveFrameBlob(blob, cameraName, 'jpg', opts);
}

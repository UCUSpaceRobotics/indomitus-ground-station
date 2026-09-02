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

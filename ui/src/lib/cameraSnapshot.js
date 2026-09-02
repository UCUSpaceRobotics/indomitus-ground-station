/**
 * Fetches one MJPEG snapshot (see config.js's `snapshotUrl()`) as a Blob.
 *
 * Shared by lib/saveFrame.js and lib/qrScan.js — both need the same bytes,
 * and both hit the same wall: the camera servers are cross-origin from the
 * UI, so this needs `Access-Control-Allow-Origin` on the response.
 * `cameras/camera_mjpeg_server.py` (the Nano cameras) sends it; a stock
 * `web_video_server` does not, and there is no client-side way around that.
 */
export async function fetchSnapshotBlob(snapshotUrl) {
  let response;
  try {
    response = await fetch(snapshotUrl, { cache: 'no-store' });
  } catch {
    throw new Error(
      "Can't read this camera's frame — its server does not allow this page to read it " +
        '(missing CORS headers).',
    );
  }
  if (!response.ok) throw new Error(`Camera server returned HTTP ${response.status}.`);
  return response.blob();
}

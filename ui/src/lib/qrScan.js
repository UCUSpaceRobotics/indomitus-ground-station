import jsQR from 'jsqr';

/**
 * Decodes a QR code from an already-in-hand frame.
 *
 * `createImageBitmap` on a `Blob` never taints the canvas it is drawn into —
 * unlike drawing a cross-origin `<img>` — because the bytes are already
 * local data by the time this runs; whatever CORS restriction stood between
 * the page and the camera server was already cleared (or sidestepped, for
 * the ROS transport's base64 payload) to get the blob in the first place.
 * Returns the decoded text, or null if no QR code was found in the frame.
 */
export async function decodeQrFromBlob(blob) {
  const bitmap = await createImageBitmap(blob);
  try {
    const canvas = document.createElement('canvas');
    canvas.width = bitmap.width;
    canvas.height = bitmap.height;
    const ctx = canvas.getContext('2d');
    ctx.drawImage(bitmap, 0, 0);
    const { data, width, height } = ctx.getImageData(0, 0, canvas.width, canvas.height);
    const result = jsQR(data, width, height);
    return result ? result.data : null;
  } finally {
    bitmap.close();
  }
}

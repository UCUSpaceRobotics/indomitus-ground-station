import { useCallback, useEffect, useRef, useState } from 'react';
import { CameraOff, Image as ImageIcon, QrCode, RefreshCw, Radio, RotateCw, Save } from 'lucide-react';
import { VIDEO_MODES, isDirectUrl, mjpegUrl, placeholderFor, snapshotUrl, useConfig } from '../config';
import { useTopic, useTick, isStale } from '../ros/useTopic';
import { fmtAge, fmtNumber, stampToMs } from '../lib/format';
import { rotateCamera, useRotation } from '../lib/rotation';
import { paintMirror, drawMirror, subscribeMirror } from '../lib/frameMirror';
import { fetchSnapshotBlob } from '../lib/cameraSnapshot';
import { blobFromBase64, saveFrameBlob } from '../lib/saveFrame';
import { decodeQrFromBlob } from '../lib/qrScan';

const RETRY_BASE_MS = 1000;
const RETRY_MAX_MS = 15_000;

function retryDelay(attempt) {
  return Math.min(RETRY_BASE_MS * 2 ** Math.max(0, attempt - 1), RETRY_MAX_MS);
}

/** Only http(s) is ever offered as a clickable link — a QR is untrusted input. */
function isHttpUrl(text) {
  try {
    return ['http:', 'https:'].includes(new URL(text).protocol);
  } catch {
    return false;
  }
}

/**
 * Appends the cache-buster. web_video_server URLs always carry a query, but a
 * direct MJPEG source may not (`http://10.42.0.1:8080/stream`), and a bare `&`
 * there produces a path no server answers — which reads in the UI as a camera
 * that never recovers from its first retry.
 */
function bust(src, nonce) {
  return `${src}${src.includes('?') ? '&' : '?'}_r=${nonce}`;
}

/**
 * MJPEG from `web_video_server`.
 *
 * `src` is assigned imperatively so the stream can be torn down properly on
 * unmount — leaving an <img> pointed at a multipart response keeps the HTTP
 * connection (and the rover-side encoder) working for a pane nobody is watching.
 */
function MjpegImage({ src, alt, cameraId, onStatus }) {
  const imgRef = useRef(null);
  const attemptRef = useRef(0);
  const [nonce, setNonce] = useState(0);

  useEffect(() => {
    const img = imgRef.current;
    if (!img || !src) return undefined;

    let closed = false;
    let retryTimer = null;
    onStatus('connecting');

    const handleLoad = () => {
      if (closed) return;
      attemptRef.current = 0;
      onStatus('live');
      paintMirror(cameraId, img);
    };

    const handleError = () => {
      if (closed) return;
      onStatus('error');
      attemptRef.current += 1;
      retryTimer = setTimeout(() => setNonce((n) => n + 1), retryDelay(attemptRef.current));
    };

    img.addEventListener('load', handleLoad);
    img.addEventListener('error', handleError);
    // The cache-buster doubles as the retry trigger: a fresh URL forces a new
    // connection instead of letting the browser replay a dead one.
    img.src = nonce === 0 ? src : bust(src, nonce);

    return () => {
      closed = true;
      if (retryTimer) clearTimeout(retryTimer);
      img.removeEventListener('load', handleLoad);
      img.removeEventListener('error', handleError);
      img.removeAttribute('src');
    };
  }, [src, nonce, cameraId, onStatus]);

  return <img ref={imgRef} alt={alt} className="feed-img" />;
}

/**
 * Thumbnail strip: shows the last frame `frameMirror` has for this camera,
 * with no request of its own — a camera not currently the focused pane opens
 * no connection, so the rover neither captures nor encodes it (see
 * cameras/camera_mjpeg_server.py). Redraws only when a stream open elsewhere
 * (the main pane, or a grid tile) paints a new frame into the mirror.
 */
function MirrorCanvas({ alt, cameraId, onStatus }) {
  const canvasRef = useRef(null);

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return undefined;

    const draw = () => onStatus(drawMirror(cameraId, canvas) ? 'live' : 'connecting');
    draw();
    return subscribeMirror(cameraId, draw);
  }, [cameraId, onStatus]);

  return <canvas className="feed-img" role="img" aria-label={alt} ref={canvasRef} />;
}

/**
 * One camera pane.
 *
 * Two transports, chosen in settings:
 *
 * - `mjpeg` (default) — `web_video_server`. Cheap on CPU and bandwidth, but the
 *   browser exposes no per-frame metadata, so we can only report whether the
 *   connection is up.
 * - `ros` — `sensor_msgs/CompressedImage` over rosbridge. Needs no extra
 *   rover-side service and every frame is timestamped, so this mode reports true
 *   frame rate and frame age and can tell a frozen feed from a live one.
 *
 * Either way the pane reports what it is actually receiving. It never
 * substitutes a placeholder image for a feed that is down.
 */
export default function CameraFeed({ camera, variant = 'main', className = '' }) {
  const config = useConfig();
  const isThumb = variant === 'thumb';
  // A direct MJPEG URL has no ROS topic behind it, so `ros` transport cannot
  // serve it. Fall back to HTTP for that camera rather than showing "No signal"
  // on a feed that is actually up.
  const direct = isDirectUrl(camera.topic);
  const rosMode = config.videoMode === VIDEO_MODES.ros && !direct;

  const [httpStatus, setHttpStatus] = useState('connecting');
  const [reloadKey, setReloadKey] = useState(0);
  const [saveToast, setSaveToast] = useState(null);
  const [qrResult, setQrResult] = useState(null);
  const qrPopupRef = useRef(null);
  const now = useTick(1000);
  // Shared across every pane showing this camera — thumbnail, main and the
  // fullscreen route rotate together.
  const rotation = useRotation(camera.id);

  const handleStatus = useCallback((next) => setHttpStatus(next), []);
  const reload = useCallback(() => {
    setHttpStatus('connecting');
    setReloadKey((k) => k + 1);
  }, []);

  // Auto-dismiss the "saved"/error pop after a few seconds.
  useEffect(() => {
    if (!saveToast) return undefined;
    const timer = setTimeout(() => setSaveToast(null), 3000);
    return () => clearTimeout(timer);
  }, [saveToast]);

  // The QR result stays up until the operator dismisses it, not on a timer —
  // it may hold a link they still need to read or click. Any pointerdown
  // outside the popup itself closes it; a click on the popup (including the
  // link it may contain) does not.
  useEffect(() => {
    if (!qrResult) return undefined;
    const onPointerDown = (event) => {
      if (!qrPopupRef.current?.contains(event.target)) setQrResult(null);
    };
    document.addEventListener('pointerdown', onPointerDown);
    return () => document.removeEventListener('pointerdown', onPointerDown);
  }, [qrResult]);

  // 33 ms, so a 30 fps publisher is not throttled down to 15 in the main pane.
  // rosbridge drops frames to honour this, and the drop happens *after* the
  // rover has already put them on the link — so this trades browser and
  // websocket load, never link bandwidth. Thumbnails stay at 1 fps.
  const frameIntervalMs = isThumb ? 1000 : 33;
  const rosFeed = useTopic(`${camera.topic}/compressed`, 'sensor_msgs/CompressedImage', {
    enabled: rosMode,
    throttleMs: frameIntervalMs,
    queueLength: 1,
    renderMs: 0,
  });

  /**
   * The frame behind both the save and the QR-scan buttons.
   *
   * Deliberately not a canvas capture of what's on screen. The camera
   * servers (web_video_server, the Nano's mjpg-streamer) are cross-origin
   * from the UI and send no CORS headers, so drawing their frames onto a
   * `<canvas>` taints it — reading it back throws a SecurityError ("the
   * operation is insecure") in every browser, not just Firefox. The ROS
   * transport hands the frame over as base64 directly, so it is decoded
   * straight to a Blob; the MJPEG transport re-fetches one still frame
   * instead — see lib/cameraSnapshot.js for what happens when that fetch is
   * itself CORS-blocked.
   */
  const getFrameBlob = useCallback(async () => {
    if (rosMode) {
      if (!rosFeed.message?.data) throw new Error('No frame available yet.');
      const mime = String(rosFeed.message.format || '').includes('png') ? 'image/png' : 'image/jpeg';
      const ext = mime === 'image/png' ? 'png' : 'jpg';
      return { blob: blobFromBase64(rosFeed.message.data, mime), ext };
    }
    return { blob: await fetchSnapshotBlob(snapshotUrl(config, camera.topic)), ext: 'jpg' };
  }, [rosMode, rosFeed.message, camera.topic, config]);

  const handleSave = useCallback(
    async (event) => {
      // Grid tiles are themselves buttons (see CameraGrid); without this the
      // click also bubbles up and switches the view to this camera.
      event.stopPropagation();
      setQrResult(null);
      try {
        const { blob, ext } = await getFrameBlob();
        const result = await saveFrameBlob(blob, camera.name, ext, {
          allowDownloadFallback: config.screenshotDownloadFallback,
        });
        setSaveToast({
          tone: 'ok',
          text: result.viaFallback
            ? `Downloaded ${result.filename} (folder saving unavailable — check your browser's downloads)`
            : `Saved ${result.filename}`,
        });
      } catch (err) {
        setSaveToast({ tone: 'crit', text: String(err.message || err) });
      }
    },
    [getFrameBlob, camera.name, config.screenshotDownloadFallback],
  );

  const handleScanQr = useCallback(
    async (event) => {
      event.stopPropagation();
      setSaveToast(null);
      try {
        const { blob } = await getFrameBlob();
        const text = await decodeQrFromBlob(blob);
        setQrResult(
          text ? { tone: 'ok', text } : { tone: 'warn', text: 'No QR code detected in this frame.' },
        );
      } catch (err) {
        setQrResult({ tone: 'crit', text: String(err.message || err) });
      }
    },
    [getFrameBlob],
  );

  const rosStale = isStale(rosFeed.receivedAt, now, Math.max(3000, frameIntervalMs * 5));
  let status = httpStatus;
  if (rosMode) {
    status = rosFeed.receivedAt === 0 ? 'connecting' : rosStale ? 'error' : 'live';
  }

  const stampMs = stampToMs(rosFeed.message?.header?.stamp);
  const frameAgeMs = rosFeed.receivedAt ? now - rosFeed.receivedAt : null;

  let media = null;
  if (rosMode) {
    if (rosFeed.message?.data) {
      const mime = String(rosFeed.message.format || '').includes('png') ? 'image/png' : 'image/jpeg';
      media = (
        <img className="feed-img" alt={camera.name} src={`data:${mime};base64,${rosFeed.message.data}`} />
      );
    }
  } else if (isThumb) {
    media = <MirrorCanvas cameraId={camera.id} alt={camera.name} onStatus={handleStatus} />;
  } else {
    media = (
      <MjpegImage
        key={reloadKey}
        src={mjpegUrl(config, camera.topic)}
        alt={camera.name}
        cameraId={camera.id}
        onStatus={handleStatus}
      />
    );
  }

  // A stand-in still, shown only while the real feed is down. Always badged, so
  // it cannot be read as live video.
  const placeholder = placeholderFor(camera);
  const showStill = status !== 'live' && Boolean(placeholder);

  return (
    <div
      className={`feed feed-${variant} is-${status} rot-${rotation} ${showStill ? 'has-still' : ''} ${className}`.trim()}
    >
      {media}

      {showStill && (
        <img className="feed-still" src={placeholder} alt={`${camera.name} placeholder still`} />
      )}

      {status !== 'live' && !showStill && (
        <div className="feed-placeholder">
          <CameraOff size={isThumb ? 18 : 34} />
          <span className="feed-placeholder-text">
            {status === 'connecting' ? 'Acquiring…' : 'No signal'}
          </span>
          {!isThumb && <span className="mono feed-placeholder-topic">{camera.topic}</span>}
        </div>
      )}

      {showStill && !isThumb && (
        <div className="feed-badge is-still">
          <ImageIcon size={12} />
          PLACEHOLDER
          <span className="feed-badge-stat">
            {status === 'connecting' ? 'acquiring feed…' : 'feed down'}
          </span>
        </div>
      )}

      {/* Rotates the picture, not the camera — see lib/rotation.js. Bottom
          left, so it never lands on the Retry button above it. Thumbnails are
          too small for a control and follow the main pane's angle instead. */}
      {!isThumb && (
        <button
          type="button"
          className="btn btn-sm feed-rotate"
          onClick={() => rotateCamera(camera.id)}
          title={`Rotate view 90° (now ${rotation}°)`}
          aria-label={`Rotate ${camera.name} view, currently ${rotation} degrees`}
        >
          <RotateCw size={13} />
          {rotation !== 0 && <span className="mono feed-rotate-deg">{rotation}°</span>}
        </button>
      )}

      {/* Only in the main pane: tiles are themselves buttons, and a button
          inside a button is invalid. */}
      {status !== 'live' && variant === 'main' && !rosMode && (
        <button type="button" className="btn btn-sm feed-retry" onClick={reload}>
          <RefreshCw size={13} /> Retry
        </button>
      )}

      {/* Top right: LIVE badge, save-frame and scan-QR buttons share this
          corner. Both buttons only exist while there is an actual frame to
          act on — no feed, no buttons. In the focus pane they are always
          visible; on a grid tile they only appear on hover (see
          .feed-tile .feed-save, .feed-tile .feed-scan-qr in index.css). */}
      {status === 'live' && !isThumb && (
        <div className="feed-topright">
          <button
            type="button"
            className="btn btn-sm feed-scan-qr"
            onClick={handleScanQr}
            title={`Scan QR code from ${camera.name}`}
            aria-label={`Scan QR code in the current frame from ${camera.name}`}
          >
            <QrCode size={13} />
          </button>
          <button
            type="button"
            className="btn btn-sm feed-save"
            onClick={handleSave}
            title={`Save frame from ${camera.name}`}
            aria-label={`Save current frame from ${camera.name}`}
          >
            <Save size={13} />
          </button>
          <div className="feed-badge">
            <Radio size={12} />
            LIVE
            {rosMode && rosFeed.hz > 0 && (
              <span className="feed-badge-stat">{fmtNumber(rosFeed.hz, 1)} fps</span>
            )}
            {rosMode && frameAgeMs !== null && (
              <span className="feed-badge-stat">{fmtAge(frameAgeMs)}</span>
            )}
            {rosMode && stampMs !== null && rosFeed.receivedAt > 0 && (
              <span className="feed-badge-stat" title="Rover timestamp to browser arrival">
                {fmtAge(rosFeed.receivedAt - stampMs)} link
              </span>
            )}
          </div>
        </div>
      )}

      {saveToast && !isThumb && (
        <div className={`feed-save-toast is-${saveToast.tone}`}>{saveToast.text}</div>
      )}

      {/* Stays up until the operator dismisses it (click anywhere outside),
          not on a timer — see the pointerdown listener above. Content
          decoded from a live camera feed is untrusted: rendered as text, and
          only ever offered as a clickable link when it parses as http(s). */}
      {qrResult && !isThumb && (
        <div className={`feed-qr-popup is-${qrResult.tone}`} ref={qrPopupRef}>
          {qrResult.tone === 'ok' ? (
            isHttpUrl(qrResult.text) ? (
              <a href={qrResult.text} target="_blank" rel="noopener noreferrer" className="mono">
                {qrResult.text}
              </a>
            ) : (
              <span className="mono">{qrResult.text}</span>
            )
          ) : (
            <span>{qrResult.text}</span>
          )}
        </div>
      )}
    </div>
  );
}

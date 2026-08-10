import { useCallback, useEffect, useRef, useState } from 'react';
import { CameraOff, Image as ImageIcon, RefreshCw, Radio } from 'lucide-react';
import { VIDEO_MODES, mjpegUrl, placeholderFor, snapshotUrl, useConfig } from '../config';
import { useTopic, useTick, isStale } from '../ros/useTopic';
import { fmtAge, fmtNumber, stampToMs } from '../lib/format';

const RETRY_BASE_MS = 1000;
const RETRY_MAX_MS = 15_000;
const SNAPSHOT_INTERVAL_MS = 2000;

function retryDelay(attempt) {
  return Math.min(RETRY_BASE_MS * 2 ** Math.max(0, attempt - 1), RETRY_MAX_MS);
}

/**
 * MJPEG from `web_video_server`.
 *
 * `src` is assigned imperatively so the stream can be torn down properly on
 * unmount — leaving an <img> pointed at a multipart response keeps the HTTP
 * connection (and the rover-side encoder) working for a pane nobody is watching.
 */
function MjpegImage({ src, alt, onStatus }) {
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
    img.src = nonce === 0 ? src : `${src}&_r=${nonce}`;

    return () => {
      closed = true;
      if (retryTimer) clearTimeout(retryTimer);
      img.removeEventListener('load', handleLoad);
      img.removeEventListener('error', handleError);
      img.removeAttribute('src');
    };
  }, [src, nonce, onStatus]);

  return <img ref={imgRef} alt={alt} className="feed-img" />;
}

/** Still frames polled on an interval, for the thumbnail strip: a six-camera
 *  focus view then opens one stream instead of six. */
function SnapshotImage({ src, alt, onStatus }) {
  const [nonce, setNonce] = useState(0);

  useEffect(() => {
    const timer = setInterval(() => setNonce((n) => n + 1), SNAPSHOT_INTERVAL_MS);
    return () => clearInterval(timer);
  }, []);

  return (
    <img
      className="feed-img"
      alt={alt}
      src={`${src}&_r=${nonce}`}
      onLoad={() => onStatus('live')}
      onError={() => onStatus('error')}
    />
  );
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
  const rosMode = config.videoMode === VIDEO_MODES.ros;

  const [httpStatus, setHttpStatus] = useState('connecting');
  const [reloadKey, setReloadKey] = useState(0);
  const now = useTick(1000);

  const handleStatus = useCallback((next) => setHttpStatus(next), []);
  const reload = useCallback(() => {
    setHttpStatus('connecting');
    setReloadKey((k) => k + 1);
  }, []);

  const frameIntervalMs = isThumb ? 1000 : 66;
  const rosFeed = useTopic(`${camera.topic}/compressed`, 'sensor_msgs/CompressedImage', {
    enabled: rosMode,
    throttleMs: frameIntervalMs,
    queueLength: 1,
    renderMs: 0,
  });

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
    media = (
      <SnapshotImage
        key={reloadKey}
        src={snapshotUrl(config, camera.topic)}
        alt={camera.name}
        onStatus={handleStatus}
      />
    );
  } else {
    media = (
      <MjpegImage
        key={reloadKey}
        src={mjpegUrl(config, camera.topic)}
        alt={camera.name}
        onStatus={handleStatus}
      />
    );
  }

  // A stand-in still, shown only while the real feed is down. Always badged, so
  // it cannot be read as live video.
  const placeholder = placeholderFor(camera);
  const showStill = status !== 'live' && Boolean(placeholder);

  return (
    <div className={`feed feed-${variant} is-${status} ${showStill ? 'has-still' : ''} ${className}`.trim()}>
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

      {/* Only in the main pane: tiles are themselves buttons, and a button
          inside a button is invalid. */}
      {status !== 'live' && variant === 'main' && !rosMode && (
        <button type="button" className="btn btn-sm feed-retry" onClick={reload}>
          <RefreshCw size={13} /> Retry
        </button>
      )}

      {status === 'live' && !isThumb && (
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
      )}
    </div>
  );
}

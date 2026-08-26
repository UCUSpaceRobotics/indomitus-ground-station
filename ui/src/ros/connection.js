import * as ROSLIB from 'roslib';

export const CONNECT_TIMEOUT_MS = 6000;
export const BACKOFF_BASE_MS = 500;
export const BACKOFF_MAX_MS = 8000;

export function backoffDelay(attempt, random = Math.random) {
  const exponential = Math.min(BACKOFF_BASE_MS * 2 ** Math.max(0, attempt - 1), BACKOFF_MAX_MS);
  // Jitter keeps several browser windows from stampeding the bridge together.
  return Math.round(exponential * (0.8 + random() * 0.4));
}

/**
 * Keeps a rosbridge websocket up.
 *
 * Plain JS with no React in it, both because the retry logic is the part most
 * worth testing on its own and because it is genuinely independent of the view.
 *
 * `onStatus({status, attempt, error, generation})` fires on every transition.
 * `generation` increments on each successful connect: roslib re-sends subscribe
 * ops itself after a drop, but callers still want a signal to reset per-topic
 * rate and staleness state, which would otherwise read as live across an outage.
 *
 * @returns {{ros: object, dispose: () => void}}
 */
export function createRosConnection({ url, onStatus = () => {}, timeoutMs = CONNECT_TIMEOUT_MS }) {
  const ros = new ROSLIB.Ros({});

  let cancelled = false;
  let retryTimer = null;
  let watchdog = null;
  let attempt = 0;
  let generation = 0;

  const emit = (status, error = null) => {
    if (!cancelled) onStatus({ status, attempt, error, generation });
  };

  const clearWatchdog = () => {
    if (watchdog) {
      clearTimeout(watchdog);
      watchdog = null;
    }
  };

  const closeQuietly = () => {
    try {
      ros.close();
    } catch {
      // Never opened, or already gone.
    }
  };

  const scheduleRetry = () => {
    if (cancelled || retryTimer) return;
    attempt += 1;
    emit('reconnecting');
    retryTimer = setTimeout(() => {
      retryTimer = null;
      open();
    }, backoffDelay(attempt));
  };

  const open = () => {
    if (cancelled) return;

    // roslib's connect() returns early whenever a transport exists that is not
    // fully CLOSED — including one still winding down from close(), and one hung
    // in CONNECTING. Calling it then would silently do nothing and strand the
    // connection, so retire the old socket first and let its 'close' (or the
    // retry we schedule here) drive the next attempt.
    const transport = ros.transport;
    if (transport && typeof transport.isClosed === 'function' && !transport.isClosed()) {
      closeQuietly();
      scheduleRetry();
      return;
    }

    emit(attempt === 0 ? 'connecting' : 'reconnecting');
    ros.connect(url).catch((err) => emit('reconnecting', String(err?.message || err)));

    clearWatchdog();
    // A host that silently drops packets leaves the socket in CONNECTING
    // indefinitely; force the issue so backoff can take over.
    watchdog = setTimeout(() => {
      watchdog = null;
      if (cancelled || ros.isConnected) return;
      closeQuietly();
      scheduleRetry();
    }, timeoutMs);
  };

  ros.on('connection', () => {
    if (cancelled) return;
    attempt = 0;
    generation += 1;
    clearWatchdog();
    emit('connected');
  });

  ros.on('error', (err) => {
    // roslib emits 'close' straight after, which is what drives the retry.
    if (!cancelled) emit('reconnecting', String(err?.message || 'websocket error'));
  });

  ros.on('close', () => {
    if (cancelled) return;
    clearWatchdog();
    scheduleRetry();
  });

  open();

  return {
    ros,
    dispose() {
      cancelled = true;
      if (retryTimer) clearTimeout(retryTimer);
      clearWatchdog();
      closeQuietly();
    },
  };
}

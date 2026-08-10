import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import * as ROSLIB from 'roslib';
import { RosContext } from './context';
import { createRosConnection } from './connection';

const PING_INTERVAL_MS = 3000;

/** builtin_interfaces/Time (ROS 2) or the ROS 1 spelling -> milliseconds. */
function rosTimeToMs(time) {
  if (!time) return null;
  const sec = time.sec ?? time.secs;
  const nsec = time.nanosec ?? time.nsecs ?? 0;
  if (typeof sec !== 'number') return null;
  return sec * 1000 + nsec / 1e6;
}

/**
 * Owns the single rosbridge connection for the app and keeps it alive.
 *
 * The previous implementation connected once and gave up on the first drop, so
 * any Wi-Fi blip took the ground station down until someone reloaded the page.
 */
export default function RosProvider({ url, children }) {
  const rosRef = useRef(null);
  const [state, setState] = useState({
    status: 'connecting',
    generation: 0,
    attempt: 0,
    error: null,
  });
  const [ping, setPing] = useState({ latencyMs: null, clockSkewMs: null });
  const [manualNonce, setManualNonce] = useState(0);

  useEffect(() => {
    setPing({ latencyMs: null, clockSkewMs: null });
    const connection = createRosConnection({
      url,
      onStatus: ({ status, attempt, error, generation }) => {
        setState({ status, attempt, error, generation });
        if (status !== 'connected') setPing({ latencyMs: null, clockSkewMs: null });
      },
    });
    rosRef.current = connection.ros;

    return () => {
      connection.dispose();
      rosRef.current = null;
    };
  }, [url, manualNonce]);

  const connected = state.status === 'connected';

  // Round-trip time to the bridge, plus rover-vs-operator clock offset. Both are
  // measured; if rosapi is unavailable the badge shows nothing rather than a guess.
  useEffect(() => {
    if (!connected || !rosRef.current) return undefined;
    const service = new ROSLIB.Service({
      ros: rosRef.current,
      name: '/rosapi/get_time',
      serviceType: 'rosapi/GetTime',
    });
    let cancelled = false;
    let supported = true;

    const measure = () => {
      if (cancelled || !supported) return;
      const sentAt = performance.now();
      const wallSentAt = Date.now();
      service.callService(
        {},
        (result) => {
          if (cancelled) return;
          const latencyMs = Math.round(performance.now() - sentAt);
          const roverMs = rosTimeToMs(result?.time);
          const clockSkewMs =
            roverMs === null ? null : Math.round(roverMs - (wallSentAt + latencyMs / 2));
          setPing({ latencyMs, clockSkewMs });
        },
        () => {
          if (cancelled) return;
          // rosapi is optional; stop probing rather than spamming the console.
          supported = false;
          setPing({ latencyMs: null, clockSkewMs: null });
        },
      );
    };

    measure();
    const timer = setInterval(measure, PING_INTERVAL_MS);
    return () => {
      cancelled = true;
      clearInterval(timer);
    };
  }, [connected, state.generation]);

  const reconnect = useCallback(() => setManualNonce((n) => n + 1), []);

  const value = useMemo(
    () => ({
      ros: connected ? rosRef.current : null,
      status: state.status,
      generation: state.generation,
      connected,
      attempt: state.attempt,
      error: state.error,
      latencyMs: ping.latencyMs,
      clockSkewMs: ping.clockSkewMs,
      url,
      reconnect,
    }),
    [connected, state, ping, url, reconnect],
  );

  return <RosContext.Provider value={value}>{children}</RosContext.Provider>;
}

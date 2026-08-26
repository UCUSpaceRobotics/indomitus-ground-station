import { useCallback, useEffect, useRef, useState } from 'react';
import * as ROSLIB from 'roslib';
import { useRos } from './context';

const EMPTY = { message: null, receivedAt: 0, count: 0, hz: 0 };

/**
 * Subscribes to a topic and returns the latest message.
 *
 * Two things this handles that a bare `new ROSLIB.Topic(...)` does not:
 *
 * - **Re-subscribe after a reconnect.** roslib sends the subscribe op once, on
 *   the socket that was live at the time, so subscriptions silently die when the
 *   bridge drops. The effect keys on the connection generation.
 * - **Decouple message rate from render rate.** /joy runs at 50 Hz; re-rendering
 *   React that often for a text readout is wasteful. Messages land in a ref and
 *   are flushed on an interval (`renderMs`). Pass `renderMs: 0` for video, where
 *   every frame matters.
 *
 * @returns {{message: unknown, receivedAt: number, count: number, hz: number}}
 *   `receivedAt` is a `Date.now()` stamp (0 = nothing received yet) so callers
 *   can render an honest "stale"/"no data" state instead of a fabricated value.
 */
export function useTopic(name, messageType, options = {}) {
  const {
    throttleMs = 100,
    queueLength = 1,
    renderMs = 100,
    enabled = true,
    compression = 'none',
  } = options;

  const { ros, generation } = useRos();
  const [snapshot, setSnapshot] = useState(EMPTY);
  const pending = useRef(null);
  const stats = useRef({ count: 0, hz: 0, lastAt: 0 });

  useEffect(() => {
    if (!ros || !enabled || !name || !messageType) {
      setSnapshot(EMPTY);
      return undefined;
    }

    stats.current = { count: 0, hz: 0, lastAt: 0 };
    pending.current = null;

    const topic = new ROSLIB.Topic({
      ros,
      name,
      messageType,
      throttle_rate: throttleMs,
      queue_length: queueLength,
      compression,
    });

    const flush = () => {
      if (!pending.current) return;
      const next = pending.current;
      pending.current = null;
      setSnapshot(next);
    };

    const handle = (message) => {
      const now = Date.now();
      const s = stats.current;
      if (s.lastAt) {
        const dt = now - s.lastAt;
        // Exponential moving average: steady enough to read, quick to react.
        if (dt > 0) s.hz = s.hz ? s.hz * 0.8 + (1000 / dt) * 0.2 : 1000 / dt;
      }
      s.lastAt = now;
      s.count += 1;

      const next = { message, receivedAt: now, count: s.count, hz: s.hz };
      if (renderMs <= 0) setSnapshot(next);
      else pending.current = next;
    };

    topic.subscribe(handle);
    const timer = renderMs > 0 ? setInterval(flush, renderMs) : null;

    return () => {
      if (timer) clearInterval(timer);
      topic.unsubscribe(handle);
    };
  }, [ros, generation, name, messageType, throttleMs, queueLength, renderMs, enabled, compression]);

  return snapshot;
}

/**
 * Like {@link useTopic}, but keeps a bounded history instead of only the latest
 * message — for log-style topics such as /rosout.
 *
 * While `paused` the subscription stays open and messages keep accumulating in
 * the ring buffer; they are just not flushed to React, so un-pausing shows what
 * was missed rather than a gap.
 *
 * @returns {{entries: Array<{seq: number, receivedAt: number, message: unknown}>, clear: () => void}}
 */
export function useTopicBuffer(name, messageType, options = {}) {
  const { limit = 500, throttleMs = 0, renderMs = 250, enabled = true, paused = false } = options;

  const { ros, generation } = useRos();
  const [entries, setEntries] = useState([]);
  const buffer = useRef([]);
  const seq = useRef(0);
  const dirty = useRef(false);
  const pausedRef = useRef(paused);
  pausedRef.current = paused;

  const clear = useCallback(() => {
    buffer.current = [];
    dirty.current = false;
    setEntries([]);
  }, []);

  useEffect(() => {
    if (!ros || !enabled || !name || !messageType) return undefined;

    const topic = new ROSLIB.Topic({
      ros,
      name,
      messageType,
      throttle_rate: throttleMs,
      queue_length: 0,
    });

    const handle = (message) => {
      seq.current += 1;
      buffer.current.push({ seq: seq.current, receivedAt: Date.now(), message });
      if (buffer.current.length > limit) buffer.current.splice(0, buffer.current.length - limit);
      dirty.current = true;
    };

    topic.subscribe(handle);
    const timer = setInterval(() => {
      if (!dirty.current || pausedRef.current) return;
      dirty.current = false;
      setEntries(buffer.current.slice());
    }, renderMs);

    return () => {
      clearInterval(timer);
      topic.unsubscribe(handle);
    };
  }, [ros, generation, name, messageType, throttleMs, limit, renderMs, enabled]);

  return { entries, clear };
}

/**
 * Re-renders on an interval so time-derived UI (staleness, "3 s ago") stays
 * truthful without every topic hook running its own timer.
 */
export function useTick(intervalMs = 1000) {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const timer = setInterval(() => setNow(Date.now()), intervalMs);
    return () => clearInterval(timer);
  }, [intervalMs]);
  return now;
}

/** True when a topic has produced nothing recently (or ever). */
export function isStale(receivedAt, now, maxAgeMs = 3000) {
  if (!receivedAt) return true;
  return now - receivedAt > maxAgeMs;
}

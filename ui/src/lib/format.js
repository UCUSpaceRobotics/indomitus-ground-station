/** Placeholder for "we have no reading", used everywhere instead of a zero. */
export const NO_VALUE = '—';

export function clamp(value, min, max) {
  return Math.min(max, Math.max(min, value));
}

export function fmtNumber(value, digits = 2) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return NO_VALUE;
  return Number(value).toFixed(digits);
}

export function fmtPercent(value, digits = 0) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return NO_VALUE;
  return `${(Number(value) * 100).toFixed(digits)}%`;
}

/** Milliseconds -> a short human age like "820 ms" / "4.2 s" / "3 m". */
export function fmtAge(ms) {
  if (ms === null || ms === undefined || !Number.isFinite(ms)) return NO_VALUE;
  if (ms < 1000) return `${Math.round(ms)} ms`;
  if (ms < 60_000) return `${(ms / 1000).toFixed(1)} s`;
  return `${Math.round(ms / 60_000)} m`;
}

export function radToDeg(rad) {
  return (rad * 180) / Math.PI;
}

/** Yaw (Z rotation) in degrees, 0-360, from a geometry_msgs/Quaternion. */
export function quaternionToYawDeg(q) {
  if (!q) return null;
  const { x = 0, y = 0, z = 0, w = 1 } = q;
  const siny = 2 * (w * z + x * y);
  const cosy = 1 - 2 * (y * y + z * z);
  const deg = radToDeg(Math.atan2(siny, cosy));
  return (deg + 360) % 360;
}

/** Roll and pitch in degrees from a geometry_msgs/Quaternion — a rover's
 *  tip-over margin is worth watching. */
export function quaternionToRollPitchDeg(q) {
  if (!q) return null;
  const { x = 0, y = 0, z = 0, w = 1 } = q;

  const sinr = 2 * (w * x + y * z);
  const cosr = 1 - 2 * (x * x + y * y);
  const roll = Math.atan2(sinr, cosr);

  const sinp = clamp(2 * (w * y - z * x), -1, 1);
  const pitch = Math.asin(sinp);

  return { roll: radToDeg(roll), pitch: radToDeg(pitch) };
}

const COMPASS = ['N', 'NE', 'E', 'SE', 'S', 'SW', 'W', 'NW'];

export function compassPoint(deg) {
  if (deg === null || deg === undefined || Number.isNaN(deg)) return NO_VALUE;
  return COMPASS[Math.round(((deg % 360) + 360) % 360 / 45) % 8];
}

/** Magnitude of the planar velocity component of a geometry_msgs/Twist. */
export function twistSpeed(twist) {
  if (!twist?.linear) return null;
  const { x = 0, y = 0 } = twist.linear;
  return Math.hypot(x, y);
}

/** Rounds a byte count to a readable unit. */
export function fmtBytes(bytes) {
  if (!Number.isFinite(bytes)) return NO_VALUE;
  const units = ['B', 'KB', 'MB', 'GB'];
  let value = bytes;
  let i = 0;
  while (value >= 1024 && i < units.length - 1) {
    value /= 1024;
    i += 1;
  }
  return `${value.toFixed(value >= 10 || i === 0 ? 0 : 1)} ${units[i]}`;
}

/** builtin_interfaces/Time -> epoch milliseconds. */
export function stampToMs(stamp) {
  if (!stamp) return null;
  const sec = stamp.sec ?? stamp.secs;
  const nsec = stamp.nanosec ?? stamp.nsecs ?? 0;
  if (typeof sec !== 'number') return null;
  return sec * 1000 + nsec / 1e6;
}

export function fmtClock(ms) {
  if (!Number.isFinite(ms)) return NO_VALUE;
  const d = new Date(ms);
  const pad = (n, len = 2) => String(n).padStart(len, '0');
  return `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}.${pad(d.getMilliseconds(), 3)}`;
}

/**
 * The arm's controls, as the console is allowed to fill them.
 *
 * Mirrors `arm_bindings.py` on the ROS side, which is the source of truth —
 * the node validates every binding and rejects anything this file gets wrong,
 * so the worst a drift here can do is show a stale label, never mis-drive the
 * arm. Kept in the UI because the operator needs names and descriptions that
 * the parameter strings do not carry.
 *
 * SDL button 6 (START) is deliberately absent: the arm document marks its
 * index as unverified on real hardware. A slot that is not in this list cannot
 * be bound from the UI, which is the point.
 */

export const BUTTON_SLOTS = [
  { key: 'safe_pose', sdl: 'A', index: 0, action: 'Home pose + start servo' },
  { key: 'sampling_home', sdl: 'B', index: 1, action: 'Sampling home (drill tool only)' },
  { key: 'astrobio_home', sdl: 'X', index: 2, action: 'Astrobio home' },
  { key: 'drill_home', sdl: 'Y', index: 3, action: 'Drill home (drill tool only)' },
  { key: 'push_boost', sdl: 'LB / L1', index: 9, action: 'Hold: speed ×3' },
  { key: 'shift', sdl: 'RB / R1', index: 10, action: 'Hold: right stick becomes pitch/roll' },
  { key: 'gripper_open', sdl: 'D-Pad up', index: 11, action: 'Open gripper' },
  { key: 'gripper_close', sdl: 'D-Pad left', index: 13, action: 'Close gripper' },
];

export const AXIS_SLOTS = [
  { key: 'left_x', sdl: 'Left stick X', index: 0, action: 'Move left/right (view Y)' },
  { key: 'left_y', sdl: 'Left stick Y', index: 1, action: 'Move forward/back (view X)' },
  { key: 'right_x', sdl: 'Right stick X', index: 2, action: 'Yaw — roll with shift held' },
  { key: 'right_y', sdl: 'Right stick Y', index: 3, action: 'Up/down — pitch with shift held' },
];

export const ALL_SLOTS = [...BUTTON_SLOTS, ...AXIS_SLOTS];
export const ALL_KEYS = ALL_SLOTS.map((slot) => slot.key);
/** Mirrors SLOTS_BY_KEY in arm_bindings.py, for looking a slot up by binding. */
export const SLOTS_BY_KEY = Object.fromEntries(ALL_SLOTS.map((slot) => [slot.key, slot]));

const SOURCE_LABELS = {
  joy: 'stick board switch',
  switches: 'button board',
  joy_axis: 'stick axis',
};

/** `"switches:4:inv"` -> `{source, index, invert}`, or null when unbound. */
export function parseBind(text) {
  const parts = String(text || '').trim().split(':');
  if (parts.length < 2 || !SOURCE_LABELS[parts[0]]) return null;
  const index = Number(parts[1]);
  if (!Number.isInteger(index) || index < 0) return null;
  return { source: parts[0], index, invert: parts[2] === 'inv' };
}

export function formatBind(bind) {
  if (!bind) return '';
  return `${bind.source}:${bind.index}${bind.invert ? ':inv' : ''}`;
}

/** What the operator reads, e.g. "button board #4 (inverted)". */
export function bindLabel(bind) {
  if (!bind) return 'unbound';
  const where = `${SOURCE_LABELS[bind.source]} #${bind.index}`;
  return bind.invert ? `${where} (inverted)` : where;
}

/**
 * Slots sharing one physical control.
 *
 * Not an error — one switch driving two things is occasionally deliberate —
 * but it is nearly always a control the operator forgot was already spoken
 * for, so the page says so.
 */
export function findConflicts(bindings) {
  const seen = new Map();
  const clashes = [];
  for (const key of ALL_KEYS) {
    const bind = parseBind(bindings[key]);
    if (!bind) continue;
    const signature = `${bind.source}:${bind.index}`;
    if (seen.has(signature)) clashes.push([seen.get(signature), key]);
    else seen.set(signature, key);
  }
  return clashes;
}

/**
 * Rover capabilities the console can be wired to.
 *
 * Mirrors FUNCTIONS in indomitus_rover_joy/switch_bindings.py. Two copies,
 * because this dialog cannot import Python and neither side is worth a
 * code-generation step — keep them in step by hand. The node validates
 * whatever this sends and rejects the whole set if a key is unknown, so a
 * drift here shows up as a refused Apply rather than a switch that silently
 * does nothing.
 */

export const SOURCE_SWITCHES = 'switches';
export const SOURCE_JOY = 'joy';

/** Bind that names its own service instead of picking one from the catalogue. */
export const CUSTOM_KEY = 'custom';

/**
 * `setbool` is the absolute form a latching switch sends its position to;
 * `trigger` is the edge form a momentary button fires. Which one a bind uses
 * is derived from its source, never chosen by hand.
 */
export const ROVER_FUNCTIONS = [
  { key: 'drive_power', label: 'Drive power', group: 'Drive', setbool: '/drive/power', trigger: '/drive/power/toggle' },
  { key: 'drive_compact', label: 'Drive compact', group: 'Drive', setbool: '/drive/compact', trigger: '/drive/compact/toggle' },
  // No absolute form: clearing a fault is an action, not a state to hold.
  { key: 'drive_clear_errors', label: 'Clear drive errors', group: 'Drive', setbool: '', trigger: '/drive/clear_errors' },
  { key: 'spotlight', label: 'Spotlight', group: 'Lights', setbool: '/lights/spotlight', trigger: '/lights/spotlight/toggle' },
  { key: 'beautiful', label: 'Beautiful lights', group: 'Lights', setbool: '/lights/beautiful', trigger: '/lights/beautiful/toggle' },
];

export const FUNCTIONS_BY_KEY = Object.fromEntries(
  ROVER_FUNCTIONS.map((fn) => [fn.key, fn]),
);

/** Groups in catalogue order, for the dropdown's optgroups. */
export const FUNCTION_GROUPS = ROVER_FUNCTIONS.reduce((groups, fn) => {
  const found = groups.find((g) => g.label === fn.group);
  if (found) found.items.push(fn);
  else groups.push({ label: fn.group, items: [fn] });
  return groups;
}, []);

/**
 * What will actually be called, so the row can show it rather than leaving the
 * operator to guess which of the two forms a source picks.
 *
 * @returns {{service: string, kind: 'setbool'|'trigger'}}
 */
export function resolveCall(bind) {
  const fn =
    bind.function === CUSTOM_KEY
      ? { setbool: bind.service || '', trigger: '' }
      : FUNCTIONS_BY_KEY[bind.function];
  if (!fn) return { service: '', kind: '' };

  if (bind.source === SOURCE_SWITCHES && fn.setbool) {
    return { service: fn.setbool, kind: 'setbool' };
  }
  if (fn.trigger) return { service: fn.trigger, kind: 'trigger' };
  if (fn.setbool) return { service: fn.setbool, kind: 'setbool' };
  return { service: '', kind: '' };
}

export function functionLabel(bind) {
  if (bind.function === CUSTOM_KEY) return bind.service || 'Custom service';
  return FUNCTIONS_BY_KEY[bind.function]?.label || bind.function;
}

/** How many switch bits and joy buttons the two boards actually report. */
export const SOURCE_LIMITS = { [SOURCE_SWITCHES]: 23, [SOURCE_JOY]: 9 };

/**
 * The one console control that is not bindable: joy[0] picks who the sticks
 * belong to. joy_to_cmd_vel_node drives while it reads 0, joy_to_servo_node
 * jogs the arm while it reads 1 — both nodes' `mode_switch_index`. It is
 * therefore in no binding table, and the UI has to know it by number.
 */
export const MODE_SWITCH_INDEX = 0;

/** True while the sticks belong to the arm rather than the wheels. */
export function isArmMode(joyMessage) {
  return joyMessage?.buttons?.[MODE_SWITCH_INDEX] === 1;
}

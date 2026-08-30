/**
 * Rover capabilities the console can be wired to.
 *
 * Mirrors FUNCTIONS in gs_joy/switch_bindings.py. Two copies,
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
  // The tower's three lamps, one switch each. traffic_light drives the same
  // three together, which a single switch cannot describe.
  { key: 'light_red', label: 'Red light', group: 'Lights', setbool: '/lights/red', trigger: '/lights/red/toggle' },
  { key: 'light_green', label: 'Green light', group: 'Lights', setbool: '/lights/green', trigger: '/lights/green/toggle' },
  { key: 'light_blue', label: 'Blue light', group: 'Lights', setbool: '/lights/blue', trigger: '/lights/blue/toggle' },
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
 * An index that exists on `source`, for a bind being moved between boards.
 *
 * The boards are different widths — 23 against 9 — so switch 12 has no
 * counterpart on the joystick board. Moving a bind across has to land it
 * somewhere real: past the end, the panel draws no row to label it and the
 * node never sees that index in a frame, so the function goes quiet.
 *
 * Clamps rather than unbinds. Unbinding looked safer — it refuses to guess
 * which control was meant — but the settings row only shows its source and
 * index pickers while the bind *is* bound, so dropping the index took the
 * pickers away with it and the operator's choice of board appeared to be
 * refused outright. Clamping keeps the row editable so they can pick the
 * control they actually want, and nothing is lost by guessing: crossing to
 * another board means a different physical switch whatever we choose.
 */
export function fitIndexToSource(source, index) {
  if (index < 0) return -1;
  const last = SOURCE_LIMITS[source] - 1;
  return index > last ? last : index;
}

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


/**
 * Console modes: behaviours of the ground station's own drive node rather than
 * rover services, so they are not function binds — each is a pair of
 * parameters on joy_to_cmd_vel_node. From the operator's seat it is the same
 * gesture though, pick a switch and get a behaviour, so they are offered
 * beside the functions.
 *
 * Lives here rather than inside the settings dialog because the control-box
 * panel has to label these controls too. While the dialog owned the list, a
 * switch bound to the steering mode was drawn as "unassigned" — which is the
 * one thing an operator opens that panel to find out.
 *
 * `key` is the config field holding the bind, so `config[mode.key]` is the
 * {source, index} it is wired to.
 */
export const CONSOLE_MODES = [
  {
    key: 'driveModeBind',
    name: 'Steering mode',
    calls: 'row / curvature',
    hint: 'Curvature: the yaw stick sets a turn radius, so one arc holds across the '
      + 'speed range. Row: the yaw stick is the yaw rate directly, which is what '
      + 'strafing and precise placement want.',
  },
  {
    key: 'vyBind',
    name: 'Strafe (vy)',
    calls: 'sideways motion',
    hint: 'Off by default, as on the rover. With strafe live a diagonal stick crabs '
      + 'instead of turning. While it is off, row mode also mirrors yaw in reverse so '
      + 'the rover steers like a car.',
  },
  {
    key: 'grannyBind',
    name: 'Granny mode',
    calls: 'speed \u00d70.1',
    hint: 'Scales the whole command — yaw included, so a turn keeps its shape '
      + 'instead of tightening as you slow down.',
  },
  {
    key: 'muteBind',
    name: 'No output',
    calls: 'stop commanding',
    hint: 'Hands the drive to the onboard gamepad or autonomy without killing the '
      + 'node. One zero Twist goes out first, then silence: twist_mux holds the '
      + 'last command it was given, so going quiet alone would leave the rover '
      + 'running on it.',
  },
];

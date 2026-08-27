"""Console panel to a canonical SDL gamepad, for the arm.

The arm's ``gamepad_servo_node`` on the rover reads ``sensor_msgs/Joy`` in the
**SDL GameController layout** — axes[0..5] and buttons[0..14], where an index
means the same physical control on every device. The console is not a gamepad:
it has three sticks and nine switches on one board and twenty-three buttons on
another, in whatever order they were soldered. This module is the translation
between the two, and it is the only place that knows the SDL numbering.

The catalogue below is the contract. Bindings say *which console control* fills
each SDL slot; they never invent new slots, because the meaning of a slot lives
on the rover and this side cannot change it.

No ROS import anywhere in here — arm_bindings is deliberately standalone, so
the mapping can be tested without a running graph.
"""

from dataclasses import dataclass

#: The joystick board's 9 switches, riding in Joy.buttons.
SOURCE_JOY = 'joy'
#: The button board's 23 switches, on /switches.
SOURCE_SWITCHES = 'switches'
#: The joystick board's 6 analogue axes, in Joy.axes, already -1.0..1.0.
SOURCE_JOY_AXIS = 'joy_axis'

BUTTON_SOURCES = (SOURCE_JOY, SOURCE_SWITCHES)
AXIS_SOURCES = (SOURCE_JOY_AXIS,)
SOURCES = BUTTON_SOURCES + AXIS_SOURCES

#: SDL publishes six axes and fifteen buttons. Both arrays are always sent at
#: full length: gamepad_servo_node indexes into them directly, and a short array
#: is an IndexError on the rover rather than an unbound control here.
NUM_AXES = 6
NUM_BUTTONS = 15


@dataclass(frozen=True)
class Slot:
    """One SDL control the arm reads, and what it does there."""

    #: Stable key used in parameters, config and the UI.
    key: str
    #: Index within Joy.axes or Joy.buttons.
    index: int
    #: What the operator sees.
    label: str
    #: What the arm does with it.
    action: str


#: Every SDL button gamepad_servo_node acts on, per arm_gamepad_mapping.md.
#:
#: Button 6 (START / BUTTON_LEVEL) is deliberately absent: the document is
#: explicit that its index is unverified on real hardware and that it must not
#: be configured on the ground station. Leaving it out of the catalogue means
#: the UI cannot offer it, which is the point — an unbindable slot cannot be
#: bound by accident.
#:
#: The remaining free SDL indices (4 BACK, 5 GUIDE, 7 L3, 8 R3, 12 DPAD_DOWN,
#: 14 DPAD_RIGHT) are absent for the same reason: the arm ignores them, so a
#: binding onto one would be a control that silently does nothing.
BUTTON_SLOTS = (
    Slot('safe_pose', 0, 'A', 'Home pose + start servo'),
    Slot('sampling_home', 1, 'B', 'Sampling home (drill_sampling tool only)'),
    Slot('exit', 2, 'X', 'Exit teleop'),
    Slot('drill_home', 3, 'Y', 'Drill home (drill_sampling tool only)'),
    Slot('push_boost', 9, 'LB / L1', 'Hold: speed x3'),
    Slot('shift', 10, 'RB / R1', 'Hold: right stick becomes pitch/roll'),
    Slot('gripper_open', 11, 'D-Pad up', 'Open gripper'),
    Slot('gripper_close', 13, 'D-Pad left', 'Close gripper'),
)

#: The four axes the arm actually steers with. Axes 4 and 5 are the triggers:
#: gamepad_servo_node reads them only to record a rest value at startup and
#: never for control, so they are not bindable and are published as a constant.
AXIS_SLOTS = (
    Slot('left_x', 0, 'Left stick, left/right', 'Move left/right (view Y)'),
    Slot('left_y', 1, 'Left stick, forward/back', 'Move forward/back (view X)'),
    Slot('right_x', 2, 'Right stick, left/right', 'Yaw, or roll with shift held'),
    Slot('right_y', 3, 'Right stick, up/down', 'Move up/down, or pitch with shift held'),
)

SLOTS_BY_KEY = {slot.key: slot for slot in BUTTON_SLOTS + AXIS_SLOTS}
BUTTON_KEYS = tuple(slot.key for slot in BUTTON_SLOTS)
AXIS_KEYS = tuple(slot.key for slot in AXIS_SLOTS)


@dataclass(frozen=True)
class Bind:
    """Which console control fills one SDL slot."""

    source: str
    index: int
    #: Buttons: a switch wired so "on" reads 0. Axes: the stick pushes the
    #: wrong way. Same flag, because both mean "this reads backwards".
    invert: bool = False

    def button_value(self, value) -> int:
        """SDL button state (0/1) for a raw console reading."""
        pressed = bool(value) != self.invert
        return 1 if pressed else 0

    def axis_value(self, value) -> float:
        """SDL axis value for a raw console reading, clamped to -1..1."""
        value = -float(value) if self.invert else float(value)
        return max(-1.0, min(1.0, value))


def parse_bind(text: str):
    """Parse ``"<source>:<index>[:inv]"``, or return None for unbound.

    Bindings cross the wire as one short string per slot rather than a nested
    tree, because the UI edits them over ``rcl_interfaces/SetParameters``,
    which carries flat scalars. One string per slot also means a typo can only
    break the slot it is in.
    """
    text = (text or '').strip()
    if not text:
        return None

    parts = text.split(':')
    if len(parts) not in (2, 3):
        raise ValueError(
            f'{text!r}: expected "<source>:<index>" or "<source>:<index>:inv"')

    source, raw_index = parts[0].strip(), parts[1].strip()
    if source not in SOURCES:
        raise ValueError(f'{text!r}: source must be one of {SOURCES}, got {source!r}')

    try:
        index = int(raw_index)
    except ValueError:
        raise ValueError(f'{text!r}: index must be an integer, got {raw_index!r}') from None
    if index < 0:
        raise ValueError(f'{text!r}: index must not be negative')

    invert = False
    if len(parts) == 3:
        flag = parts[2].strip().lower()
        if flag not in ('inv', 'invert', ''):
            raise ValueError(f'{text!r}: third field must be "inv", got {parts[2]!r}')
        invert = flag.startswith('inv')

    return Bind(source=source, index=index, invert=invert)


def format_bind(bind) -> str:
    """Inverse of parse_bind, so a saved file round-trips."""
    if bind is None:
        return ''
    return f'{bind.source}:{bind.index}' + (':inv' if bind.invert else '')


def build_bindings(specs: dict) -> dict:
    """Validate ``{slot_key: "source:index"}`` into ``{slot_key: Bind|None}``.

    Unknown keys are an error rather than a shrug: silently ignoring one is how
    a mapping ends up looking configured while the arm never sees it.
    """
    bindings = {}
    for key, text in specs.items():
        slot = SLOTS_BY_KEY.get(key)
        if slot is None:
            raise ValueError(
                f'{key}: not an arm control; expected one of {sorted(SLOTS_BY_KEY)}')

        try:
            bind = parse_bind(text)
        except ValueError as exc:
            raise ValueError(f'{key}: {exc}') from None

        if bind is not None:
            wanted = AXIS_SOURCES if key in AXIS_KEYS else BUTTON_SOURCES
            if bind.source not in wanted:
                kind = 'an axis' if key in AXIS_KEYS else 'a button'
                raise ValueError(
                    f'{key}: {kind} slot needs source in {wanted}, got {bind.source!r}')

        bindings[key] = bind
    return bindings


class GamepadFrame:
    """Builds one SDL Joy payload from the latest console readings.

    Holds the last value seen per source, because the two boards report
    independently and at different rates: the sticks arrive at 200 Hz while the
    button board only speaks when something changes. The arm needs a complete
    frame at a steady rate regardless, or its 0.2 s /joy timeout stops it.
    """

    def __init__(self, bindings):
        self._bindings = dict(bindings)
        self._values = {SOURCE_JOY: [], SOURCE_SWITCHES: [], SOURCE_JOY_AXIS: []}

    def set_bindings(self, bindings):
        self._bindings = dict(bindings)

    def update(self, source: str, values):
        if source not in self._values:
            raise ValueError(f'unknown source {source!r}')
        self._values[source] = list(values)

    def _read(self, key, default):
        bind = self._bindings.get(key)
        if bind is None:
            return default
        values = self._values.get(bind.source, [])
        if bind.index >= len(values):
            # The board is present but shorter than the binding expects — a
            # miswired index, or a board that came up with fewer switches.
            # Reporting neutral is the only safe reading: the alternative is
            # an arm that moves because an index ran off the end.
            return default
        return values[bind.index]

    def axes(self) -> list:
        out = [0.0] * NUM_AXES
        for key in AXIS_KEYS:
            slot = SLOTS_BY_KEY[key]
            raw = self._read(key, 0.0)
            bind = self._bindings.get(key)
            out[slot.index] = bind.axis_value(raw) if bind is not None else 0.0
        return out

    def buttons(self) -> list:
        out = [0] * NUM_BUTTONS
        for key in BUTTON_KEYS:
            slot = SLOTS_BY_KEY[key]
            raw = self._read(key, 0)
            bind = self._bindings.get(key)
            out[slot.index] = bind.button_value(raw) if bind is not None else 0
        return out


def conflicts(bindings: dict) -> list:
    """Slots that share one physical control, as (key, key, bind) triples.

    Not fatal — an operator may genuinely want one switch to drive two things —
    but the UI shows it, because the usual cause is binding a slot and
    forgetting the control was already spoken for.
    """
    seen = {}
    found = []
    for key in BUTTON_KEYS + AXIS_KEYS:
        bind = bindings.get(key)
        if bind is None:
            continue
        signature = (bind.source, bind.index)
        if signature in seen:
            found.append((seen[signature], key, bind))
        else:
            seen[signature] = key
    return found

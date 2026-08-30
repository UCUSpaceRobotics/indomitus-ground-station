"""Panel switches to rover service calls.

The console has latching switches; the rover's onboard joystick has momentary
buttons. They ask for the same things in different shapes: a switch knows its
own absolute position, so it calls SetBool with it, while a button can only ask
the owner to invert what it holds. Keeping state on this side would mean two
copies of the same truth drifting apart the moment the other operator touches
something, so this side keeps none — beyond remembering where each switch was
last seen, which is the minimum an edge detector can work with.

No ROS import anywhere in here — switch_bindings is deliberately standalone.
"""

from dataclasses import dataclass

#: The joystick board's own 9 switches ride in Joy.buttons, in the same message
#: as the axes, so a mode and the stick values can never disagree.
SOURCE_JOY = 'joy'
#: The button board's 23 switches, republished on /switches at 10 Hz.
SOURCE_SWITCHES = 'switches'
SOURCES = (SOURCE_JOY, SOURCE_SWITCHES)

#: How many controls each board actually reports: the joystick board has 9
#: switches wired off its PCF8575, the button board 23. An index past the end
#: of its own board is the one wiring mistake nobody can see — the panel draws
#: only the controls a board has, so the row never appears to be labelled, and
#: EdgeTracker skips an index no frame ever reaches, so the function silently
#: does nothing. It is easy to land on by accident: moving a bind from the
#: button board to the joystick one keeps the index it already had.
#: Mirrored in ui/src/lib/roverFunctions.js as SOURCE_LIMITS.
SOURCE_WIDTHS = {SOURCE_JOY: 9, SOURCE_SWITCHES: 23}

#: A latching switch sends its absolute position with SetBool; a momentary
#: button has no position and can only fire an edge, so it calls the Trigger
#: twin. Which one a binding uses is derived from its source, never chosen by
#: hand - picking SetBool for a button is the classic way to wire a console so
#: that releasing it undoes what pressing it did.
KIND_SETBOOL = 'setbool'
KIND_TRIGGER = 'trigger'


@dataclass(frozen=True)
class Function:
    """One rover capability, in both the shapes it is offered in.

    Mirrored in ui/src/lib/roverFunctions.js for the settings dialog. Two
    copies, because the UI cannot import Python and neither side is worth a
    code-generation step; keep them in step by hand.
    """

    key: str
    label: str
    #: Absolute form, for latching switches. Empty when there is not one.
    setbool: str = ''
    #: Edge form, for momentary buttons. Empty when there is not one.
    trigger: str = ''

    def resolve(self, source: str):
        """(service, kind) this function should be called with from `source`.

        Prefers the shape that matches the control, and falls back to whatever
        the rover actually offers: drive/clear_errors has no absolute form, so
        even a latching switch fires it on the rising edge only.
        """
        if source == SOURCE_SWITCHES and self.setbool:
            return self.setbool, KIND_SETBOOL
        if self.trigger:
            return self.trigger, KIND_TRIGGER
        if self.setbool:
            return self.setbool, KIND_SETBOOL
        return '', ''


#: Everything the console can be wired to. drive_* are owned by
#: rover_teleop/drive_power_node, lights by rover_peripherals.
FUNCTIONS = (
    Function('drive_power', 'Drive power', '/drive/power', '/drive/power/toggle'),
    Function('drive_compact', 'Drive compact', '/drive/compact', '/drive/compact/toggle'),
    # No absolute form: clearing a fault is an action, not a state.
    Function('drive_clear_errors', 'Clear drive errors', '', '/drive/clear_errors'),
    # Absolute only. rover_peripherals/rover_lighting_node advertises exactly
    # lights/spotlight, lights/beautiful and lights/traffic_light, all absolute
    # — there are no toggle twins behind the lights the way there are behind
    # drive_power. Naming one here made resolve() prefer it for anything bound
    # to the joystick board, which then called a service nobody offers: the
    # console warned "not available" and the light never came on. The joy
    # board's 9 controls are latching switches anyway, so they have a position
    # to send and lose nothing by going through SetBool.
    Function('spotlight', 'Spotlight', '/lights/spotlight', ''),
    Function('beautiful', 'Beautiful lights', '/lights/beautiful', ''),
    # The tower's three lamps, one switch each. Absolute like the rest of the
    # lights — rover_lighting_node offers no toggle twins. lights/traffic_light
    # drives the same lamps as a bitmask, but a switch holds one lamp and knows
    # nothing about the other two, so it cannot send a mask.
    Function('light_red', 'Red light', '/lights/red', ''),
    Function('light_green', 'Green light', '/lights/green', ''),
    Function('light_blue', 'Blue light', '/lights/blue', ''),
)

FUNCTIONS_BY_KEY = {f.key: f for f in FUNCTIONS}


@dataclass(frozen=True)
class Binding:
    """One panel switch wired to one rover service."""

    name: str
    source: str
    index: int
    service: str
    #: True when the switch is wired so that "up" reads as 0.
    invert: bool = False
    #: How to call `service`. Derived from the source at build time.
    kind: str = KIND_SETBOOL

    def desired(self, value: int) -> bool:
        """What to send for this switch position."""
        return bool(value) != self.invert


def build_bindings(specs: dict) -> list:
    """Validate the parameter tree into Bindings, naming what is wrong.

    Bindings come from config rather than code because only the people holding
    the console know which switch is under which label, and a silent typo here
    is a switch that quietly does nothing.
    """
    bindings = []
    for name, spec in specs.items():
        source = spec.get('source', SOURCE_SWITCHES)
        if source not in SOURCES:
            raise ValueError(
                f'{name}: source must be one of {SOURCES}, got {source!r}')

        index = spec.get('index', -1)
        if not isinstance(index, int) or index < 0:
            raise ValueError(f'{name}: index must be a non-negative int, got {index!r}')

        service = spec.get('service', '')
        if not service:
            raise ValueError(f'{name}: no service given')

        bindings.append(Binding(
            name=name,
            source=source,
            index=index,
            service=service,
            invert=bool(spec.get('invert', False)),
        ))
    return bindings


#: Key used for a binding that names its own service instead of picking one
#: out of FUNCTIONS. The settings dialog offers it as "Custom service".
CUSTOM_KEY = 'custom'


def binds_from_specs(specs, claimed=()) -> list:
    """Build Bindings from the list the settings dialog writes at runtime.

    Each spec is a dict: `function`, `source`, `index`, optional `invert`, and
    for CUSTOM_KEY an explicit `service` plus optional `trigger`.

    Exclusivity is enforced here and not only in the UI. A control wired to two
    things is the one console mistake an operator cannot see by looking at the
    panel — the second thing just happens — and this parameter can be written
    by anything that can reach the node, not only by the dialog. `claimed` is
    the (source, index) pairs already spoken for elsewhere, so a switch bit
    cannot select a camera and energise the drive at the same time.
    """
    taken = {tuple(pair): 'another setting' for pair in claimed}
    bindings = []

    for position, spec in enumerate(specs):
        name = spec.get('function') or ''
        if not name:
            raise ValueError(f'bind {position}: no function given')

        source = spec.get('source', SOURCE_SWITCHES)
        if source not in SOURCES:
            raise ValueError(
                f'{name}: source must be one of {SOURCES}, got {source!r}')

        index = spec.get('index', -1)
        if not isinstance(index, int) or isinstance(index, bool) or index < 0:
            raise ValueError(f'{name}: index must be a non-negative int, got {index!r}')
        width = SOURCE_WIDTHS[source]
        if index >= width:
            raise ValueError(
                f'{name}: the {source} board has {width} controls, '
                f'so {source}[{index}] is not one of them')

        if name == CUSTOM_KEY:
            service = spec.get('service', '')
            if not service:
                raise ValueError(f'{name}: a custom bind needs a service name')
            function = Function(CUSTOM_KEY, service, service, spec.get('trigger', ''))
        else:
            function = FUNCTIONS_BY_KEY.get(name)
            if function is None:
                raise ValueError(f'{name}: not a known function')

        service, kind = function.resolve(source)
        if not service:
            raise ValueError(f'{name}: offers no service to call')

        owner = taken.get((source, index))
        if owner is not None:
            raise ValueError(
                f'{name}: {source}[{index}] is already used by {owner}')
        taken[(source, index)] = name

        bindings.append(Binding(
            name=name,
            source=source,
            index=index,
            service=service,
            invert=bool(spec.get('invert', False)),
            kind=kind,
        ))
    return bindings


def specs_from_binds(bindings) -> list:
    """Inverse of binds_from_specs, for writing the file back out."""
    return [
        {
            'function': b.name,
            'source': b.source,
            'index': b.index,
            'invert': b.invert,
            **({'service': b.service} if b.name == CUSTOM_KEY else {}),
        }
        for b in bindings
    ]


class EdgeTracker:
    """Reports switch movements, never the position a switch was found in.

    The first sample from a source is a baseline and fires nothing. That is the
    safety property: plugging the console in, or restarting this node, must not
    replay every switch position at the rover. A power switch left up would
    otherwise energise the drive the instant the node came up, with nobody
    having touched anything.
    """

    def __init__(self, bindings):
        self._bindings = list(bindings)
        self._previous = {}

    def update(self, source: str, values) -> list:
        """Feed the latest values for one source.

        Returns the (binding, desired) pairs that just changed.
        """
        values = list(values)
        previous = self._previous.get(source)
        self._previous[source] = values

        if previous is None or len(previous) != len(values):
            # No baseline, or the board came back reporting a different number
            # of switches — a length change means the frames are not comparable,
            # so re-baseline rather than inventing edges from the mismatch.
            return []

        changes = []
        for binding in self._bindings:
            if binding.source != source or binding.index >= len(values):
                continue
            if values[binding.index] == previous[binding.index]:
                continue
            changes.append((binding, binding.desired(values[binding.index])))
        return changes

    def forget(self, source: str):
        """Drop a source's baseline, so the next sample fires nothing.

        Used when the stream goes stale: switches may have moved while nobody
        was looking, and the positions they come back in are not edges.
        """
        self._previous.pop(source, None)

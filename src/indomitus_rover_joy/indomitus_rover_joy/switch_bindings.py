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


@dataclass(frozen=True)
class Binding:
    """One panel switch wired to one rover service."""

    name: str
    source: str
    index: int
    service: str
    #: True when the switch is wired so that "up" reads as 0.
    invert: bool = False

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

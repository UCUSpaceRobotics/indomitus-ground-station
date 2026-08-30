"""Console switches: what fires, and — mostly — what must not.

The interesting cases here are all the ones where nothing should happen.
A switch that fires when it shouldn't is a rover that energises its drive
because somebody plugged a USB cable in.

No ROS import anywhere in here — switch_bindings is deliberately standalone.
"""

import pytest

from indomitus_rover_joy.switch_bindings import (
    CUSTOM_KEY,
    FUNCTIONS,
    KIND_SETBOOL,
    KIND_TRIGGER,
    SOURCE_JOY,
    SOURCE_SWITCHES,
    Binding,
    EdgeTracker,
    binds_from_specs,
    build_bindings,
    specs_from_binds,
)


POWER = Binding('drive_power', SOURCE_SWITCHES, 0, '/drive/power')
LIGHT = Binding('spotlight', SOURCE_SWITCHES, 2, '/lights/spotlight')


def tracker(*bindings):
    return EdgeTracker(bindings or (POWER,))


# ── the baseline rule ────────────────────────────────────────────────────────

def test_the_first_sample_never_fires():
    # This is the safety property of the whole module. Plugging the console in,
    # or restarting this node, must not replay every switch position at the
    # rover: a power switch left up would energise the drive with nobody
    # having touched anything.
    assert tracker().update(SOURCE_SWITCHES, [1, 1, 1]) == []


def test_a_switch_that_never_moves_never_fires_again():
    t = tracker()
    t.update(SOURCE_SWITCHES, [1, 0, 0])

    for _ in range(10):
        assert t.update(SOURCE_SWITCHES, [1, 0, 0]) == []


def test_moving_a_switch_fires_its_new_position():
    t = tracker()
    t.update(SOURCE_SWITCHES, [0, 0, 0])

    assert t.update(SOURCE_SWITCHES, [1, 0, 0]) == [(POWER, True)]


def test_moving_it_back_fires_the_other_way():
    t = tracker()
    t.update(SOURCE_SWITCHES, [0, 0, 0])
    t.update(SOURCE_SWITCHES, [1, 0, 0])

    assert t.update(SOURCE_SWITCHES, [0, 0, 0]) == [(POWER, False)]


def test_unbound_switches_are_ignored():
    # The button board reports 23 bits and only a few are wired.
    t = tracker()
    t.update(SOURCE_SWITCHES, [0] * 23)

    changes = t.update(SOURCE_SWITCHES, [0] + [1] * 22)

    assert changes == []


def test_several_switches_moving_at_once_all_fire():
    t = tracker(POWER, LIGHT)
    t.update(SOURCE_SWITCHES, [0, 0, 0])

    changes = t.update(SOURCE_SWITCHES, [1, 0, 1])

    assert changes == [(POWER, True), (LIGHT, True)]


# ── the two sources are independent ──────────────────────────────────────────

def test_the_joystick_board_and_the_button_board_keep_separate_baselines():
    # The 9 switches on the joystick board arrive inside Joy.buttons; the 23 on
    # the button board arrive on /switches. Sharing a baseline between them
    # would make every message from one look like a change on the other.
    mode = Binding('mode', SOURCE_JOY, 1, '/drive/compact')
    t = tracker(POWER, mode)

    t.update(SOURCE_SWITCHES, [0, 0, 0])
    assert t.update(SOURCE_JOY, [0, 0]) == []          # joy's own first sample
    assert t.update(SOURCE_JOY, [0, 1]) == [(mode, True)]
    assert t.update(SOURCE_SWITCHES, [0, 0, 0]) == []  # unaffected


# ── boards coming and going ──────────────────────────────────────────────────

def test_a_frame_of_a_different_length_re_baselines_instead_of_firing():
    # A board that reconnects reporting a different number of switches gives
    # frames that cannot be compared to the old ones. Lining them up by index
    # would invent edges out of the mismatch.
    t = tracker()
    t.update(SOURCE_SWITCHES, [0, 0, 0])

    assert t.update(SOURCE_SWITCHES, [1, 1]) == []
    # ...and the new length is now the baseline.
    assert t.update(SOURCE_SWITCHES, [0, 1]) == [(POWER, False)]


def test_forgetting_a_source_makes_the_next_sample_a_baseline_again():
    # Switches can be moved while the stream is down, and where they turn up
    # afterwards is a position, not an edge.
    t = tracker()
    t.update(SOURCE_SWITCHES, [0, 0, 0])

    t.forget(SOURCE_SWITCHES)

    assert t.update(SOURCE_SWITCHES, [1, 0, 0]) == []
    assert t.update(SOURCE_SWITCHES, [0, 0, 0]) == [(POWER, False)]


def test_a_binding_past_the_end_of_the_frame_is_skipped_not_crashed():
    # A config typo, or a board reporting fewer switches than expected, must
    # not take the console's control path down.
    t = tracker(Binding('typo', SOURCE_SWITCHES, 40, '/drive/power'))
    t.update(SOURCE_SWITCHES, [0, 0, 0])

    assert t.update(SOURCE_SWITCHES, [1, 1, 1]) == []


# ── inverted wiring ──────────────────────────────────────────────────────────

def test_an_inverted_switch_sends_the_opposite_of_its_bit():
    inverted = Binding('power', SOURCE_SWITCHES, 0, '/drive/power', invert=True)
    t = tracker(inverted)
    t.update(SOURCE_SWITCHES, [0])

    assert t.update(SOURCE_SWITCHES, [1]) == [(inverted, False)]


# ── config validation ────────────────────────────────────────────────────────

def test_a_valid_spec_builds():
    bindings = build_bindings({
        'drive_power': {'source': 'switches', 'index': 0, 'service': '/drive/power'},
    })

    assert bindings == [Binding('drive_power', SOURCE_SWITCHES, 0, '/drive/power')]


def test_switches_is_the_default_source():
    binding, = build_bindings({'x': {'index': 1, 'service': '/s'}})

    assert binding.source == SOURCE_SWITCHES


@pytest.mark.parametrize('spec, wrong', [
    ({'source': 'panel', 'index': 0, 'service': '/s'}, 'source'),
    ({'source': 'switches', 'index': -1, 'service': '/s'}, 'index'),
    ({'source': 'switches', 'index': 0, 'service': ''}, 'service'),
])
def test_a_bad_spec_is_rejected_and_names_the_binding(spec, wrong):
    # A typo here is a switch that silently does nothing, which on a console is
    # indistinguishable from a broken rover. Fail loudly at startup instead.
    with pytest.raises(ValueError, match='drive_power'):
        build_bindings({'drive_power': spec})


# ── the runtime binding list the settings dialog writes ──────────────────────

def test_a_latching_switch_gets_the_absolute_service():
    binds = binds_from_specs([
        {'function': 'drive_power', 'source': SOURCE_SWITCHES, 'index': 0}])
    assert binds[0].service == '/drive/power'
    assert binds[0].kind == KIND_SETBOOL


def test_a_momentary_button_gets_the_toggle_twin():
    # A button has no position to send, so SetBool would undo it on release.
    binds = binds_from_specs([
        {'function': 'drive_power', 'source': SOURCE_JOY, 'index': 3}])
    assert binds[0].service == '/drive/power/toggle'
    assert binds[0].kind == KIND_TRIGGER


def test_a_function_with_no_absolute_form_is_edge_fired_from_either_source():
    # Clearing a fault is an action; there is no "off" position to hold.
    for source in (SOURCE_JOY, SOURCE_SWITCHES):
        binds = binds_from_specs([
            {'function': 'drive_clear_errors', 'source': source, 'index': 1}])
        assert binds[0].service == '/drive/clear_errors'
        assert binds[0].kind == KIND_TRIGGER


def test_a_control_cannot_drive_two_functions():
    with pytest.raises(ValueError, match='already used'):
        binds_from_specs([
            {'function': 'drive_power', 'source': SOURCE_SWITCHES, 'index': 0},
            {'function': 'spotlight', 'source': SOURCE_SWITCHES, 'index': 0},
        ])


def test_a_switch_already_driving_a_camera_is_refused():
    with pytest.raises(ValueError, match='already used'):
        binds_from_specs(
            [{'function': 'drive_power', 'source': SOURCE_SWITCHES, 'index': 4}],
            claimed=[(SOURCE_SWITCHES, 4)])


def test_the_same_index_on_two_sources_is_not_a_clash():
    binds = binds_from_specs([
        {'function': 'drive_power', 'source': SOURCE_SWITCHES, 'index': 2},
        {'function': 'spotlight', 'source': SOURCE_JOY, 'index': 2},
    ])
    assert len(binds) == 2


def test_an_unknown_function_is_named_in_the_error():
    with pytest.raises(ValueError, match='nonsense'):
        binds_from_specs([
            {'function': 'nonsense', 'source': SOURCE_SWITCHES, 'index': 0}])


def test_a_custom_bind_needs_its_own_service():
    with pytest.raises(ValueError, match='custom'):
        binds_from_specs([
            {'function': CUSTOM_KEY, 'source': SOURCE_SWITCHES, 'index': 0}])

    binds = binds_from_specs([{
        'function': CUSTOM_KEY, 'source': SOURCE_SWITCHES,
        'index': 0, 'service': '/science/pump'}])
    assert binds[0].service == '/science/pump'


def test_specs_survive_a_round_trip():
    specs = [
        {'function': 'drive_power', 'source': SOURCE_SWITCHES, 'index': 0, 'invert': True},
        {'function': 'drive_clear_errors', 'source': SOURCE_JOY, 'index': 5, 'invert': False},
    ]
    assert specs_from_binds(binds_from_specs(specs)) == specs


def test_invert_still_reaches_the_binding():
    binds = binds_from_specs([{
        'function': 'drive_power', 'source': SOURCE_SWITCHES,
        'index': 0, 'invert': True}])
    assert binds[0].desired(0) is True and binds[0].desired(1) is False


def test_a_bind_past_the_end_of_its_board_is_refused():
    """The failure it prevents: switch 12 exists on the button board and not on
    the joystick one, so moving the bind across leaves it somewhere no frame
    reaches. The old set built cleanly and then did nothing at all.
    """
    with pytest.raises(ValueError, match='9 controls'):
        binds_from_specs([
            {'function': 'drive_compact', 'source': SOURCE_JOY, 'index': 12}])

    # The same index is fine on the wider board.
    binds = binds_from_specs([
        {'function': 'drive_compact', 'source': SOURCE_SWITCHES, 'index': 12}])
    assert binds[0].index == 12


def test_each_board_is_bindable_to_its_last_control():
    for source, last in ((SOURCE_JOY, 8), (SOURCE_SWITCHES, 22)):
        binds = binds_from_specs([
            {'function': 'drive_power', 'source': source, 'index': last}])
        assert binds[0].index == last
        with pytest.raises(ValueError, match='is not one of them'):
            binds_from_specs([
                {'function': 'drive_power', 'source': source, 'index': last + 1}])


@pytest.mark.parametrize('function, service', [
    ('spotlight', '/lights/spotlight'),
    ('beautiful', '/lights/beautiful'),
    ('light_red', '/lights/red'),
    ('light_green', '/lights/green'),
    ('light_blue', '/lights/blue'),
])
def test_every_light_offers_both_forms(function, service):
    """A latching panel switch sends its position; the toggle twin is there
    for anything that can only fire an edge. rover_lighting_node advertises
    both for every light, so neither source is left without one.
    """
    absolute = binds_from_specs([
        {'function': function, 'source': SOURCE_SWITCHES, 'index': 3}])
    assert absolute[0].service == service
    assert absolute[0].kind == KIND_SETBOOL

    edge = binds_from_specs([{'function': function, 'source': SOURCE_JOY, 'index': 3}])
    assert edge[0].service == f'{service}/toggle'
    assert edge[0].kind == KIND_TRIGGER


def test_no_function_offers_a_service_the_rover_does_not():
    """Every service either side of resolve() must be one that exists.

    Kept as an explicit list rather than a rule, because the only way to know
    is to go and read what the rover advertises: rover_teleop/drive_power_node
    for the drive_*, rover_peripherals/rover_lighting_node for the lights.
    """
    advertised = {
        '/drive/power', '/drive/power/toggle',
        '/drive/compact', '/drive/compact/toggle',
        '/drive/clear_errors',
        '/lights/spotlight', '/lights/spotlight/toggle',
        '/lights/beautiful', '/lights/beautiful/toggle',
        '/lights/red', '/lights/red/toggle',
        '/lights/green', '/lights/green/toggle',
        '/lights/blue', '/lights/blue/toggle',
    }
    for function in FUNCTIONS:
        for source in (SOURCE_SWITCHES, SOURCE_JOY):
            service, _ = function.resolve(source)
            assert service in advertised, f'{function.key} from {source} -> {service}'

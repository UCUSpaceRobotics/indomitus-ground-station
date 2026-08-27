"""Console panel to SDL gamepad: what reaches the arm, and what must not.

The dangerous direction here is a frame that says "moving" when nothing is
being touched, or a button that reads pressed because an index ran off the end
of a short board. Those are an arm that moves on its own.

No ROS import anywhere in here — arm_bindings is deliberately standalone.
"""

import pytest

from indomitus_rover_joy.arm_bindings import (
    AXIS_KEYS,
    BUTTON_KEYS,
    NUM_AXES,
    NUM_BUTTONS,
    SLOTS_BY_KEY,
    SOURCE_JOY,
    SOURCE_JOY_AXIS,
    SOURCE_SWITCHES,
    Bind,
    GamepadFrame,
    build_bindings,
    conflicts,
    format_bind,
    parse_bind,
)


def frame(**specs):
    return GamepadFrame(build_bindings(specs))


# ── the catalogue is the contract ────────────────────────────────────────────

def test_the_arm_document_s_button_indices_are_what_we_publish():
    # Straight from arm_gamepad_mapping.md. If the arm renumbers a control,
    # this is the test that should fail.
    expected = {
        'safe_pose': 0,      # A
        'sampling_home': 1,  # B
        'exit': 2,           # X
        'drill_home': 3,     # Y
        'push_boost': 9,     # LB
        'shift': 10,         # RB
        'gripper_open': 11,  # D-Pad up
        'gripper_close': 13,  # D-Pad left
    }
    assert {key: SLOTS_BY_KEY[key].index for key in BUTTON_KEYS} == expected


def test_button_six_is_not_offerable():
    # The arm document is explicit: START/BUTTON_LEVEL is unverified on real
    # hardware and must not be configured. Absent from the catalogue means the
    # UI cannot show it and a config naming it is rejected.
    assert 6 not in {SLOTS_BY_KEY[key].index for key in BUTTON_KEYS}
    with pytest.raises(ValueError):
        build_bindings({'level': 'switches:1'})


def test_the_triggers_are_not_bindable():
    # axes[4]/axes[5] exist in the frame but the arm only reads them to record
    # a rest value at startup, so binding one would be a control that does
    # nothing while looking configured.
    assert {SLOTS_BY_KEY[key].index for key in AXIS_KEYS} == {0, 1, 2, 3}


# ── parsing ──────────────────────────────────────────────────────────────────

def test_parses_the_three_shapes():
    assert parse_bind('switches:4') == Bind(SOURCE_SWITCHES, 4, False)
    assert parse_bind('joy:2:inv') == Bind(SOURCE_JOY, 2, True)
    assert parse_bind('') is None
    assert parse_bind(None) is None


def test_round_trips_through_format():
    for text in ('switches:4', 'joy:2:inv', 'joy_axis:0', 'joy_axis:3:inv', ''):
        assert format_bind(parse_bind(text)) == text


@pytest.mark.parametrize('text', [
    'switches',           # no index
    'switches:4:5:6',     # too many fields
    'nope:1',             # unknown source
    'switches:x',         # non-numeric index
    'switches:-1',        # negative index
    'switches:4:maybe',   # unknown flag
])
def test_rejects_malformed_bindings(text):
    with pytest.raises(ValueError):
        parse_bind(text)


def test_an_axis_slot_refuses_a_button_source():
    # Binding a switch to a stick axis would publish a hard 0 or 1 as an axis
    # value: full-speed arm motion from a toggle.
    with pytest.raises(ValueError):
        build_bindings({'left_x': 'switches:3'})


def test_a_button_slot_refuses_an_axis_source():
    with pytest.raises(ValueError):
        build_bindings({'safe_pose': 'joy_axis:1'})


def test_an_unknown_slot_is_an_error_not_a_shrug():
    with pytest.raises(ValueError):
        build_bindings({'fly_to_the_moon': 'switches:1'})


# ── frame assembly ───────────────────────────────────────────────────────────

def test_an_untouched_console_publishes_a_neutral_frame():
    # The safety property. Every unbound slot, and every board not heard from
    # yet, must read as "nothing is being touched".
    f = frame(left_x='joy_axis:0', safe_pose='switches:4')
    assert f.axes() == [0.0] * NUM_AXES
    assert f.buttons() == [0] * NUM_BUTTONS


def test_the_frame_is_always_full_length():
    # gamepad_servo_node indexes straight into these; a short array is an
    # IndexError on the rover, not an unbound control here.
    f = frame()
    assert len(f.axes()) == NUM_AXES
    assert len(f.buttons()) == NUM_BUTTONS


def test_a_stick_reaches_the_slot_the_arm_reads():
    f = frame(right_y='joy_axis:3')
    f.update(SOURCE_JOY_AXIS, [0.0, 0.0, 0.0, 0.7, 0.0, 0.0])
    assert f.axes()[SLOTS_BY_KEY['right_y'].index] == pytest.approx(0.7)


def test_inverting_an_axis_flips_its_sign():
    f = frame(right_y='joy_axis:3:inv')
    f.update(SOURCE_JOY_AXIS, [0.0, 0.0, 0.0, 0.7, 0.0, 0.0])
    assert f.axes()[SLOTS_BY_KEY['right_y'].index] == pytest.approx(-0.7)


def test_axis_values_are_clamped():
    # A miscalibrated stick can overshoot 1.0; the arm scales by this directly.
    f = frame(left_x='joy_axis:0')
    f.update(SOURCE_JOY_AXIS, [4.2, 0, 0, 0, 0, 0])
    assert f.axes()[0] == pytest.approx(1.0)


def test_a_button_reaches_the_slot_the_arm_reads():
    f = frame(gripper_open='switches:10')
    f.update(SOURCE_SWITCHES, [0] * 11 + [0])
    assert f.buttons()[SLOTS_BY_KEY['gripper_open'].index] == 0

    f.update(SOURCE_SWITCHES, [0] * 10 + [1, 0])
    assert f.buttons()[SLOTS_BY_KEY['gripper_open'].index] == 1


def test_inverting_a_button_flips_it():
    f = frame(shift='joy:2:inv')
    f.update(SOURCE_JOY, [0, 0, 0])
    assert f.buttons()[SLOTS_BY_KEY['shift'].index] == 1


def test_an_index_past_the_end_of_a_board_reads_neutral():
    # A miswired index, or a board that came up with fewer switches than the
    # mapping expects. Reading neutral is the only safe answer: the
    # alternative is an arm that moves because an index ran off the end.
    f = frame(gripper_close='switches:20', left_y='joy_axis:5')
    f.update(SOURCE_SWITCHES, [1, 1, 1])
    f.update(SOURCE_JOY_AXIS, [1.0, 1.0])
    assert f.buttons()[SLOTS_BY_KEY['gripper_close'].index] == 0
    assert f.axes()[SLOTS_BY_KEY['left_y'].index] == 0.0


def test_rebinding_takes_effect_without_losing_the_last_reading():
    # The UI rebinds while the arm is live; the values already in hand still
    # apply, so a rebind does not blank the frame until the next board message.
    f = frame(left_x='joy_axis:0')
    f.update(SOURCE_JOY_AXIS, [0.1, 0.9, 0, 0, 0, 0])
    assert f.axes()[0] == pytest.approx(0.1)

    f.set_bindings(build_bindings({'left_x': 'joy_axis:1'}))
    assert f.axes()[0] == pytest.approx(0.9)


def test_two_boards_are_read_independently():
    # The sticks arrive at 200 Hz and the button board only speaks on change;
    # one going quiet must not blank the other.
    f = frame(left_x='joy_axis:0', safe_pose='switches:4')
    f.update(SOURCE_JOY_AXIS, [0.5, 0, 0, 0, 0, 0])
    f.update(SOURCE_SWITCHES, [0, 0, 0, 0, 1])
    assert f.axes()[0] == pytest.approx(0.5)
    assert f.buttons()[0] == 1


# ── conflicts ────────────────────────────────────────────────────────────────

def test_one_control_on_two_slots_is_reported():
    found = conflicts(build_bindings({
        'safe_pose': 'switches:4',
        'exit': 'switches:4',
    }))
    assert len(found) == 1
    first, second, bind = found[0]
    assert {first, second} == {'safe_pose', 'exit'}
    assert bind == Bind(SOURCE_SWITCHES, 4, False)


def test_the_same_index_on_different_boards_is_not_a_conflict():
    # index 4 means different hardware on each board.
    assert conflicts(build_bindings({
        'safe_pose': 'switches:4',
        'exit': 'joy:4',
    })) == []


def test_unbound_slots_never_conflict():
    assert conflicts(build_bindings({'safe_pose': '', 'exit': ''})) == []

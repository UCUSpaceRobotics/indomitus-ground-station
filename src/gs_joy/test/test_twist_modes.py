"""The two steering modes: what reaches the wheels for a given stick position.

This is the module that decides which way the rover goes, so the cases worth
writing down are the ones where the two modes genuinely disagree, and the ones
where a plausible-looking implementation would put the rover somewhere the
operator did not point.

No ROS import anywhere in here — twist_modes is deliberately standalone.
"""

import math

import pytest

from gs_joy.twist_modes import (
    MODE_CURVATURE,
    MODE_ROW,
    apply_granny,
    build_twist,
    clamp_linear,
    curvature_twist,
    row_twist,
    swerve_wz_correction,
)

CURV = 2.0
PROBE = 1e-5


def curve(vx, vy, steer):
    return curvature_twist(vx, vy, steer, CURV, PROBE)


# ── row ──────────────────────────────────────────────────────────────────────

def test_row_passes_the_sticks_straight_through():
    assert row_twist(0.4, -0.2, 0.7) == (0.4, -0.2, 0.7)


def test_row_yaw_does_not_depend_on_speed():
    # The defining property of the mode, and the reason it is the default:
    # the same yaw stick means the same yaw rate whatever the rover is doing.
    assert row_twist(0.0, 0.0, 0.5)[2] == row_twist(1.0, 0.0, 0.5)[2]


# ── curvature ────────────────────────────────────────────────────────────────

def test_curvature_holds_one_arc_across_the_speed_range():
    # The defining property of this mode: yaw scales with speed so that
    # wz / v — the curvature — stays put. Same stick, same circle, any speed.
    _, _, slow = curve(0.2, 0.0, 0.5)
    _, _, fast = curve(0.8, 0.0, 0.5)
    assert slow / 0.2 == pytest.approx(fast / 0.8)


def test_curvature_is_the_stick_times_max_curvature():
    vx, _, wz = curve(0.5, 0.0, 1.0)
    assert wz / vx == pytest.approx(CURV)


def test_straight_stick_means_no_yaw():
    assert curve(0.7, 0.0, 0.0)[2] == 0.0


def test_reversing_mirrors_the_arc():
    # Backing up with the stick over must curve the other way, or reverse
    # steering fights the operator. This is what v_signed is for.
    _, _, forward = curve(0.5, 0.0, 0.5)
    _, _, back = curve(-0.5, 0.0, 0.5)
    assert back == pytest.approx(-forward)


def test_diagonal_speed_counts_towards_the_arc():
    # Curvature is derived from total speed, not from vx alone: a diagonal is
    # moving, so it must curve like it.
    _, _, wz = curve(0.3, 0.4, 0.5)  # hypot = 0.5
    assert wz == pytest.approx(0.5 * 0.5 * CURV)


def test_standing_still_with_the_stick_over_still_points_the_wheels():
    # Otherwise the wheels snap straight the moment the sticks centre, and the
    # rover cannot be pre-steered before moving off.
    vx, vy, wz = curve(0.0, 0.0, 1.0)
    assert vx == pytest.approx(PROBE)
    assert vy == 0.0
    assert wz == pytest.approx(PROBE * CURV)


def test_standing_still_and_straight_commands_nothing():
    # The probe speed is for holding an angle. With no angle asked for there is
    # nothing to hold, and a rover that creeps while untouched is a bug.
    assert curve(0.0, 0.0, 0.0) == (0.0, 0.0, 0.0)


def test_the_probe_does_not_apply_once_actually_moving():
    vx, _, _ = curve(0.5, 0.0, 1.0)
    assert vx == pytest.approx(0.5)


# ── dispatch ─────────────────────────────────────────────────────────────────

def test_the_modes_disagree_on_the_same_sticks():
    # If these ever match, one of the two branches is not doing its job.
    # Note the speed: at vx = 1/max_curvature the two happen to agree, which
    # is a coincidence of the numbers and not a property worth testing.
    args = (0.3, 0.0, 0.9, 0.9, CURV, PROBE)
    assert build_twist(MODE_ROW, *args) != build_twist(MODE_CURVATURE, *args)


def test_row_is_what_an_unknown_mode_falls_back_to():
    # This runs per message on the path that drives the wheels. A typo in a
    # parameter must not raise: that would stop the Twist stream mid-drive.
    args = (0.4, 0.1, 0.6, 0.6, CURV, PROBE)
    assert build_twist('nonsense', *args) == build_twist(MODE_ROW, *args)


# ── the square-gate clamp ────────────────────────────────────────────────────

def test_a_full_diagonal_is_shrunk_to_the_limit():
    vx, vy = clamp_linear(1.0, 1.0, 1.0)
    assert math.hypot(vx, vy) == pytest.approx(1.0)


def test_clamping_keeps_the_heading():
    # Per-axis clamping would keep the magnitude and bend the heading, sending
    # the rover somewhere the stick was not pointing.
    vx, vy = clamp_linear(1.0, 0.5, 1.0)
    assert vy / vx == pytest.approx(0.5)


def test_inside_the_limit_nothing_moves():
    assert clamp_linear(0.3, 0.4, 1.0) == (0.3, 0.4)


def test_a_zero_limit_disables_the_clamp():
    assert clamp_linear(3.0, 4.0, 0.0) == (3.0, 4.0)


# ── granny ───────────────────────────────────────────────────────────────────

def test_granny_off_changes_nothing():
    assert apply_granny(0.6, -0.2, 0.9, 0.1, False) == (0.6, -0.2, 0.9)


def test_granny_scales_every_component():
    vx, vy, wz = apply_granny(0.6, -0.2, 0.8, 0.5, True)
    assert (vx, vy, wz) == pytest.approx((0.3, -0.1, 0.4))


def test_granny_preserves_the_arc():
    # Yaw is scaled with the linear pair on purpose: scaling only translation
    # would tighten every turn as a side effect of slowing down, so a line the
    # operator had lined up would stop being the line they get.
    vx, vy, wz = curve(0.6, 0.0, 0.5)
    gx, gy, gz = apply_granny(vx, vy, wz, 0.1, True)
    assert gz / gx == pytest.approx(wz / vx)


def test_granny_preserves_heading():
    vx, vy, _ = apply_granny(0.3, 0.4, 0.0, 0.1, True)
    assert vy / vx == pytest.approx(4 / 3)


# ── strafe off: the reverse mirror ───────────────────────────────────────────

def test_vy_on_leaves_yaw_alone_in_reverse():
    # With strafe available the operator can hold a heading independently, so
    # the raw yaw is what they asked for.
    assert row_twist(-0.5, 0.0, 0.7, True)[2] == pytest.approx(0.7)


def test_vy_off_mirrors_yaw_in_reverse():
    # Without strafe the yaw stick is the only way to point the rover, and a
    # stick that turns the same way backwards as forwards fights the operator
    # reversing out of anything.
    assert row_twist(-0.5, 0.0, 0.7, False)[2] == pytest.approx(-0.7)


def test_vy_off_leaves_yaw_alone_going_forward():
    assert row_twist(0.5, 0.0, 0.7, False)[2] == pytest.approx(0.7)


def test_the_mirror_has_a_deadband_around_stationary():
    # Otherwise yaw flips sign on noise while the rover is standing still.
    assert swerve_wz_correction(-1e-4, 0.7) == pytest.approx(0.7)


def test_curvature_needs_no_mirror():
    # It already derives yaw from signed speed, so reversing mirrors the arc on
    # its own; correcting again would cancel that back out.
    args = (-0.5, 0.0, 0.9, 0.9, CURV, PROBE)
    assert build_twist(MODE_CURVATURE, *args, False) == build_twist(MODE_CURVATURE, *args, True)

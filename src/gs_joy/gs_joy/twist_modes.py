"""Two ways to turn three stick axes into a Twist.

Mirrors the two branches of ``_publish_timer_cb`` in the rover's
``rover_teleop/joystick_interpreter_node.py``, so the console and the onboard
gamepad steer the rover the same way rather than two ways that merely look
alike. The rover keeps its copy; this one exists because the console builds its
own Twist on ``/cmd_vel_gs`` instead of sending the rover a Joy.

ROW (the default, and what the console did before this existed)
    The yaw stick *is* the yaw rate. Direct, predictable, and what you want
    for strafing and precise placement — but the turn radius then depends on
    how fast you happen to be going.

CURVATURE
    The yaw stick sets a curvature (1/R) instead, and yaw rate is derived from
    the speed actually commanded. The rover then holds one arc through the
    whole speed range, which is what you want for driving a line.

No ROS import anywhere in here — twist_modes is deliberately standalone, so
the motion math can be tested without a graph. That matters more here than
elsewhere: this is the code that decides which way the wheels point.
"""

import math

MODE_ROW = 'row'
MODE_CURVATURE = 'curvature'
MODES = (MODE_ROW, MODE_CURVATURE)

#: Below this the rover is standing still for the purposes of curvature: there
#: is no speed to derive a yaw rate from, so the arc has to be commanded some
#: other way. Same threshold the rover uses.
STOPPED_SPEED = 1e-3


def swerve_wz_correction(vx, wz):
    """Mirror yaw while reversing, so the rover steers like a car.

    Only meaningful with strafe off. With vy available the operator can hold a
    heading independently and the raw yaw is what they want; without it, the
    yaw stick is the only way to point the rover, and a stick that turns the
    same way going backwards as forwards fights the operator the moment they
    reverse out of anything.
    """
    return -float(wz) if float(vx) < -1e-3 else float(wz)


def row_twist(vx, vy, wz, vy_enabled=True):
    """Yaw straight off the stick, with the reverse mirror when strafe is off."""
    if not vy_enabled:
        wz = swerve_wz_correction(vx, wz)
    return float(vx), float(vy), float(wz)


def curvature_twist(vx, vy, steer, max_curvature, angle_probe_speed):
    """Yaw derived from commanded speed and a curvature the stick sets.

    ``steer`` is the yaw stick *unscaled* — it asks for a radius here, not a
    rate, so the angular scale has no meaning and applying it would silently
    change the tightest available turn.

    Standing still with the stick over is not a no-op: the wheels still have to
    point somewhere, or the rover snaps back to straight the moment the sticks
    centre. A token forward speed commands the angle without meaningfully
    driving, which is what ``angle_probe_speed`` is for.
    """
    vx = float(vx)
    vy = float(vy)
    target_curvature = float(steer) * float(max_curvature)

    v_total = math.hypot(vx, vy)
    # Reversing must mirror the arc, not repeat it: a negative vx flips which
    # way the same stick deflection curves.
    v_signed = -v_total if vx < 0.0 else v_total

    if v_total < STOPPED_SPEED and target_curvature != 0.0:
        vx = float(angle_probe_speed)
        vy = 0.0
        return vx, vy, vx * target_curvature

    return vx, vy, v_signed * target_curvature


def build_twist(mode, vx, vy, wz, steer, max_curvature, angle_probe_speed,
                vy_enabled=True):
    """Dispatch on mode, defaulting to row for anything unrecognised.

    An unknown mode string resolves to row rather than raising: this runs per
    message on the path that drives the wheels, and the safe answer to a typo
    in a parameter is the direct, predictable mode — not an exception that
    stops the Twist stream and coasts the rover.
    """
    if mode == MODE_CURVATURE:
        # No correction here: curvature already derives yaw from signed speed,
        # so reversing mirrors the arc on its own.
        return curvature_twist(vx, vy, steer, max_curvature, angle_probe_speed)
    return row_twist(vx, vy, wz, vy_enabled)


def apply_granny(vx, vy, wz, scale, enabled):
    """Scale the whole command down for fine work.

    Applied after the mode, and to yaw as well as the linear pair, so the arc
    an operator has lined up in curvature mode is the arc they still get —
    scaling only the linear part would tighten every turn as a side effect of
    slowing down.
    """
    if not enabled:
        return float(vx), float(vy), float(wz)
    scale = float(scale)
    return float(vx) * scale, float(vy) * scale, float(wz) * scale


def apply_deadzone(value, deadzone):
    """Kill stick noise around centre, then rescale the rest to full travel.

    console_boards_node already deadzones while it normalises, but that band
    sits around the *calibrated* centre: a stick whose rest position has
    drifted away from axis_center reads non-zero, clears the band, and the
    rover creeps with the sticks untouched. This one is applied to the axis
    value as it actually arrives, so it holds whatever produced the Joy — a
    miscalibrated console, or a gamepad that never went through
    console_boards_node at all.

    The rescale is what keeps it from being a cliff: without it the output
    jumps from 0 to `deadzone` the moment the stick leaves the band.
    """
    deadzone = float(deadzone)
    value = float(value)
    if deadzone <= 0.0:
        return value
    if abs(value) < deadzone:
        return 0.0
    if deadzone >= 1.0:
        return 0.0
    sign = 1.0 if value > 0.0 else -1.0
    return sign * (abs(value) - deadzone) / (1.0 - deadzone)


def clamp_linear(vx, vy, max_linear_speed):
    """Shrink the linear pair along its own direction.

    The console's sticks travel in a square gate, so both axes reach 1.0 at
    once and a full diagonal is sqrt(2) times the per-axis scale. Clamping each
    axis separately would keep the magnitude and bend the heading, so the rover
    would not go where the stick points.
    """
    speed = math.hypot(vx, vy)
    if 0.0 < max_linear_speed < speed:
        shrink = max_linear_speed / speed
        return vx * shrink, vy * shrink
    return vx, vy

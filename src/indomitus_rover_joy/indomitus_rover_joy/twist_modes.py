"""Two ways to turn three stick axes into a Twist.

Mirrors the two branches of ``_publish_timer_cb`` in the rover's
``rover_teleop/joystick_interpreter_node.py``, so the console and the onboard
gamepad steer the rover the same way rather than two ways that merely look
alike. The rover keeps its copy; this one exists because the console builds its
own Twist on ``/cmd_vel_ext`` instead of sending the rover a Joy.

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


def row_twist(vx, vy, wz):
    """Yaw straight off the stick. The identity case, kept for symmetry."""
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


def build_twist(mode, vx, vy, wz, steer, max_curvature, angle_probe_speed):
    """Dispatch on mode, defaulting to row for anything unrecognised.

    An unknown mode string resolves to row rather than raising: this runs per
    message on the path that drives the wheels, and the safe answer to a typo
    in a parameter is the direct, predictable mode — not an exception that
    stops the Twist stream and coasts the rover.
    """
    if mode == MODE_CURVATURE:
        return curvature_twist(vx, vy, steer, max_curvature, angle_probe_speed)
    return row_twist(vx, vy, wz)


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

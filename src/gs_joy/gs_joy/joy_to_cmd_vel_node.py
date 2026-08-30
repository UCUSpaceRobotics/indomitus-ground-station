import rclpy
from rcl_interfaces.msg import SetParametersResult
from rclpy.executors import ExternalShutdownException
from rclpy.node import Node
from sensor_msgs.msg import Joy
from std_msgs.msg import Int32MultiArray
from geometry_msgs.msg import Twist

from gs_joy.twist_modes import (
    MODE_CURVATURE,
    MODE_ROW,
    MODES,
    apply_deadzone,
    apply_granny,
    build_twist,
    clamp_linear,
)

#: Where a mode switch can live. 'joy' is the stick board's own 9 switches,
#: which ride in the same message as the axes; 'switches' is the button board.
SOURCE_JOY = 'joy'
SOURCE_SWITCHES = 'switches'


class JoyToCmdVelNode(Node):
    def __init__(self):
        super().__init__('joy_to_cmd_vel_node')

        # Axis indices into the 6-axis Joy from console_boards:
        #   0 J0X  1 J0Y  2 J1X  3 J1Y  4 J2X  5 J2Y
        # Mirrors rover_teleop/config/joy.yaml on the rover, so the panel and the
        # bluetooth gamepad steer identically: J0 translates (forward/back and
        # strafe), J1X yaws. Set an index to -1 to leave that component at zero.
        self.declare_parameter('linear_x_axis', 1)
        self.declare_parameter('linear_y_axis', 0)
        self.declare_parameter('angular_z_axis', 2)
        # Scales match rover_teleop; the swerve controller clamps to
        # max_linear_speed / max_angular_speed (1.0 each) anyway.
        self.declare_parameter('linear_x_scale', 0.5)
        self.declare_parameter('linear_y_scale', 0.5)
        self.declare_parameter('angular_z_scale', 1.0)
        # Dead band around stick centre, as a fraction of full travel, applied
        # to the axis as it arrives here. console_boards_node has a deadzone of
        # its own, but that one is measured around the *calibrated* centre, so
        # a stick whose rest position has drifted reads non-zero, clears that
        # band, and the rover creeps untouched. This one does not care how the
        # Joy was produced, which is also what makes it work for a gamepad that
        # never passed through console_boards_node. 0.0 disables it.
        #
        # Applied before the scales and before the mode, so the clamp, the
        # curvature and the yaw rate all see a stick that is genuinely centred.
        self.declare_parameter('deadzone', 0.05)
        # The console's sticks travel in a square gate, not a circular one, so
        # both axes can read 1.0 at once. A full diagonal is then sqrt(2) times
        # the per-axis scale, which is how a 1 m/s rover ends up commanded at
        # 1.41. This caps the magnitude of the linear pair; 0 disables it.
        self.declare_parameter('max_linear_speed', 1.0)
        # Stop the rover if Joy messages stop arriving (serial unplugged, board
        # reset, node killed). 0.0 disables the watchdog.
        self.declare_parameter('joy_timeout', 0.5)
        # The 3 sticks drive either the rover or the arm; a panel switch picks
        # which. Index into Joy.buttons (the joystick board's own 9 switches,
        # which arrive in the same message as the axes, so the mode and the
        # stick values can never disagree). -1 disables gating.
        self.declare_parameter('mode_switch_index', 0)
        self.declare_parameter('mode_switch_value', 1)
        # Ceiling on how often a Twist goes out, independent of the /joy rate.
        # The sticks run at 200 Hz so the console feels responsive, but every
        # Twist is a packet over the rover link — and eventually over the LoRa
        # fallback, which cannot carry 200 Hz. 0.0 publishes on every message.
        #
        # This throttles only the steady stream. Both stop paths (mode handover
        # and the watchdog) bypass it: a stop must never wait for a rate limit.
        self.declare_parameter('publish_rate', 50.0)

        # ── steering mode ────────────────────────────────────────────────────
        # 'row'       the yaw stick is the yaw rate — direct, and what strafing
        #             and precise placement want.
        # 'curvature' the yaw stick sets 1/R and yaw is derived from commanded
        #             speed, so one arc holds across the speed range.
        # Same two modes as rover_teleop/joystick_interpreter_node, so the
        # console and the onboard gamepad steer alike.
        self.declare_parameter('twist_mode', MODE_ROW)
        # Tightest turn the yaw stick can ask for at full deflection, as
        # curvature 1/R. 2.0 means R = 0.5 m, an ICR inside the wheelbase.
        self.declare_parameter('max_curvature', 2.0)
        # Token speed that commands a wheel angle while standing still, so the
        # wheels hold the arc instead of snapping straight. See curvature_twist.
        self.declare_parameter('angle_probe_speed', 1e-5)
        # A panel control can flip the mode live. -1 leaves it on whatever
        # `twist_mode` says. While bound, the switch decides and the parameter
        # is only the value it falls back to if the board goes quiet.
        self.declare_parameter('twist_mode_switch_source', SOURCE_SWITCHES)
        self.declare_parameter('twist_mode_switch_index', -1)
        # Which reading means curvature. The other reading means row.
        self.declare_parameter('twist_mode_switch_value', 1)

        # ── strafe ───────────────────────────────────────────────────────────
        # Off by default, matching rover_teleop's vy_enabled_default. A swerve
        # rover can strafe, but most driving does not want it: with vy live,
        # a stick pushed diagonally crabs instead of turning. While it is off
        # the row mode also mirrors yaw in reverse, so the rover steers like a
        # car — see swerve_wz_correction.
        self.declare_parameter('vy_enabled', False)
        self.declare_parameter('vy_switch_source', SOURCE_SWITCHES)
        self.declare_parameter('vy_switch_index', -1)
        self.declare_parameter('vy_switch_value', 1)

        # ── granny mode ──────────────────────────────────────────────────────
        # Everything scaled down for fine work, exactly as rover_teleop does it.
        self.declare_parameter('granny_speed_scale', 0.1)
        self.declare_parameter('granny_mode', False)
        self.declare_parameter('granny_switch_source', SOURCE_SWITCHES)
        self.declare_parameter('granny_switch_index', -1)
        self.declare_parameter('granny_switch_value', 1)

        # ── mute ─────────────────────────────────────────────────────────────
        # Stop commanding the rover from this console without killing the node,
        # so the onboard gamepad or an autonomy stack owns the drive uncontested.
        #
        # Muting is not simply "stop publishing": twist_mux holds the last
        # command it was given, so going quiet mid-drive would leave the rover
        # running on it. One zero Twist goes out first, then silence — the same
        # shape as handing the sticks to the arm.
        self.declare_parameter('mute', False)
        self.declare_parameter('mute_switch_source', SOURCE_SWITCHES)
        self.declare_parameter('mute_switch_index', -1)
        self.declare_parameter('mute_switch_value', 1)

        self.mode_switch_index = self.get_parameter('mode_switch_index').get_parameter_value().integer_value
        self.mode_switch_value = self.get_parameter('mode_switch_value').get_parameter_value().integer_value

        publish_rate = self.get_parameter('publish_rate').get_parameter_value().double_value
        self.min_publish_period = 1.0 / publish_rate if publish_rate > 0.0 else 0.0
        # Deadline for the next Twist, in seconds on the node clock. Advancing a
        # deadline rather than measuring elapsed-since-last-publish is what makes
        # a cap at or above the input rate a true no-op: with a plain
        # "skip if elapsed < period" test, jitter in the arrival interval drops
        # every frame that lands early and the losses compound — a 50 Hz cap on
        # a 47.7 Hz stream measured 31.9 Hz.
        self.next_publish_deadline = None

        # Cached rather than read per callback: get_parameter() on every axis of
        # every message is 6 lookups per Twist, which at 200 Hz is pure
        # overhead. _on_set_parameters keeps runtime retuning working.
        self.axes = {}
        self.scales = {}
        for name in ('linear_x', 'linear_y', 'angular_z'):
            self.axes[name] = self.get_parameter(f'{name}_axis').get_parameter_value().integer_value
            self.scales[name] = self.get_parameter(f'{name}_scale').get_parameter_value().double_value
        self.add_on_set_parameters_callback(self._on_set_parameters)

        self.deadzone = self.get_parameter('deadzone').get_parameter_value().double_value

        self.max_linear_speed = self.get_parameter(
            'max_linear_speed').get_parameter_value().double_value

        self.twist_mode = self._mode_param(
            self.get_parameter('twist_mode').get_parameter_value().string_value)
        self.max_curvature = self.get_parameter(
            'max_curvature').get_parameter_value().double_value
        self.angle_probe_speed = self.get_parameter(
            'angle_probe_speed').get_parameter_value().double_value
        self.twist_mode_switch_source = self.get_parameter(
            'twist_mode_switch_source').get_parameter_value().string_value
        self.twist_mode_switch_index = self.get_parameter(
            'twist_mode_switch_index').get_parameter_value().integer_value
        self.twist_mode_switch_value = self.get_parameter(
            'twist_mode_switch_value').get_parameter_value().integer_value
        self.vy_enabled = self.get_parameter('vy_enabled').get_parameter_value().bool_value
        self.vy_switch_source = self.get_parameter(
            'vy_switch_source').get_parameter_value().string_value
        self.vy_switch_index = self.get_parameter(
            'vy_switch_index').get_parameter_value().integer_value
        self.vy_switch_value = self.get_parameter(
            'vy_switch_value').get_parameter_value().integer_value
        self._logged_vy = None

        self.granny_speed_scale = self.get_parameter(
            'granny_speed_scale').get_parameter_value().double_value
        self.granny_mode = self.get_parameter('granny_mode').get_parameter_value().bool_value
        self.granny_switch_source = self.get_parameter(
            'granny_switch_source').get_parameter_value().string_value
        self.granny_switch_index = self.get_parameter(
            'granny_switch_index').get_parameter_value().integer_value
        self.granny_switch_value = self.get_parameter(
            'granny_switch_value').get_parameter_value().integer_value

        self.mute = self.get_parameter('mute').get_parameter_value().bool_value
        self.mute_switch_source = self.get_parameter(
            'mute_switch_source').get_parameter_value().string_value
        self.mute_switch_index = self.get_parameter(
            'mute_switch_index').get_parameter_value().integer_value
        self.mute_switch_value = self.get_parameter(
            'mute_switch_value').get_parameter_value().integer_value
        self._logged_granny = None
        self._logged_mute = None

        # Last /switches frame, for a mode switch on the button board. That
        # board only speaks on change, so the latest reading has to be kept
        # here rather than expected to arrive with the sticks.
        self.switch_state = []
        # What the switch last resolved to, so a change can be logged once
        # instead of at the stick rate.
        self._logged_mode = None

        self.joy_timeout = self.get_parameter('joy_timeout').get_parameter_value().double_value
        self.last_joy_time = None
        self.stopped = True

        # Subscriber
        self.subscription = self.create_subscription(
            Joy,
            'joy',
            self.joy_callback,
            10)

        # The button board, for a mode switch that lives there. Subscribed
        # unconditionally: the switch can be rebound at runtime from the UI,
        # and a subscription that only exists when it started bound would make
        # that silently not work.
        self.switch_subscription = self.create_subscription(
            Int32MultiArray, 'switches', self._on_switches, 10)

        # Publisher
        self.publisher_ = self.create_publisher(Twist, 'cmd_vel', 10)

        if self.joy_timeout > 0.0:
            self.watchdog = self.create_timer(self.joy_timeout / 2.0, self.check_timeout)

        self.get_logger().info("Joy to CmdVel Node started")

    def _mode_param(self, value):
        if value in MODES:
            return value
        self.get_logger().warn(f"twist_mode {value!r} is not one of {MODES}; using {MODE_ROW}")
        return MODE_ROW

    def _on_switches(self, msg):
        self.switch_state = list(msg.data)

    def _switch_says(self, source, index, on_value, buttons, fallback):
        """Resolve one bound toggle, or fall back when it cannot be read.

        A bound switch decides; the parameter is the fallback for when nothing
        is bound, or when the board carrying the switch has not reported yet.
        Falling back rather than guessing matters because every one of these
        changes what the rover is commanded to do.
        """
        if index < 0:
            return fallback
        values = buttons if source == SOURCE_JOY else self.switch_state
        if index >= len(values):
            return fallback
        return values[index] == on_value

    def active_mode(self, buttons):
        """Which steering mode this frame should use."""
        if self.twist_mode_switch_index < 0:
            return self.twist_mode
        curvature = self._switch_says(
            self.twist_mode_switch_source, self.twist_mode_switch_index,
            self.twist_mode_switch_value, buttons, self.twist_mode == MODE_CURVATURE)
        return MODE_CURVATURE if curvature else MODE_ROW

    def active_vy(self, buttons):
        return self._switch_says(
            self.vy_switch_source, self.vy_switch_index,
            self.vy_switch_value, buttons, self.vy_enabled)

    def active_granny(self, buttons):
        return self._switch_says(
            self.granny_switch_source, self.granny_switch_index,
            self.granny_switch_value, buttons, self.granny_mode)

    def active_mute(self, buttons):
        return self._switch_says(
            self.mute_switch_source, self.mute_switch_index,
            self.mute_switch_value, buttons, self.mute)

    def _on_set_parameters(self, params):
        for param in params:
            if param.name == 'twist_mode':
                if param.value not in MODES:
                    return SetParametersResult(
                        successful=False, reason=f'twist_mode must be one of {MODES}')
            elif param.name == 'deadzone':
                if not 0.0 <= float(param.value) < 1.0:
                    return SetParametersResult(
                        successful=False, reason='deadzone must be in [0.0, 1.0)')
            elif param.name in ('twist_mode_switch_source',
                                'vy_switch_source',
                                'granny_switch_source',
                                'mute_switch_source'):
                if param.value not in (SOURCE_JOY, SOURCE_SWITCHES):
                    return SetParametersResult(
                        successful=False,
                        reason=f"{param.name} must be "
                               f"'{SOURCE_JOY}' or '{SOURCE_SWITCHES}'")

        for param in params:
            for name in ('linear_x', 'linear_y', 'angular_z'):
                if param.name == f'{name}_axis':
                    self.axes[name] = int(param.value)
                elif param.name == f'{name}_scale':
                    self.scales[name] = float(param.value)
            if param.name == 'deadzone':
                self.deadzone = float(param.value)
            if param.name == 'max_linear_speed':
                self.max_linear_speed = float(param.value)
            if param.name == 'publish_rate':
                rate = float(param.value)
                self.min_publish_period = 1.0 / rate if rate > 0.0 else 0.0
            if param.name == 'twist_mode':
                self.twist_mode = str(param.value)
            if param.name == 'max_curvature':
                self.max_curvature = float(param.value)
            if param.name == 'angle_probe_speed':
                self.angle_probe_speed = float(param.value)
            if param.name == 'twist_mode_switch_source':
                self.twist_mode_switch_source = str(param.value)
            if param.name == 'twist_mode_switch_index':
                self.twist_mode_switch_index = int(param.value)
            if param.name == 'twist_mode_switch_value':
                self.twist_mode_switch_value = int(param.value)
            if param.name == 'vy_enabled':
                self.vy_enabled = bool(param.value)
            if param.name == 'vy_switch_source':
                self.vy_switch_source = str(param.value)
            if param.name == 'vy_switch_index':
                self.vy_switch_index = int(param.value)
            if param.name == 'vy_switch_value':
                self.vy_switch_value = int(param.value)
            if param.name == 'granny_speed_scale':
                self.granny_speed_scale = float(param.value)
            if param.name == 'granny_mode':
                self.granny_mode = bool(param.value)
            if param.name == 'granny_switch_source':
                self.granny_switch_source = str(param.value)
            if param.name == 'granny_switch_index':
                self.granny_switch_index = int(param.value)
            if param.name == 'granny_switch_value':
                self.granny_switch_value = int(param.value)
            if param.name == 'mute':
                self.mute = bool(param.value)
            if param.name == 'mute_switch_source':
                self.mute_switch_source = str(param.value)
            if param.name == 'mute_switch_index':
                self.mute_switch_index = int(param.value)
            if param.name == 'mute_switch_value':
                self.mute_switch_value = int(param.value)
        return SetParametersResult(successful=True)

    def mode_selected(self, buttons):
        if self.mode_switch_index < 0:
            return True
        if self.mode_switch_index >= len(buttons):
            return False
        return buttons[self.mode_switch_index] == self.mode_switch_value

    def joy_callback(self, msg):
        self.last_joy_time = self.get_clock().now()

        if not self.mode_selected(msg.buttons):
            # Handing the sticks to the arm: send one zero Twist so the rover
            # doesn't coast on the last command, then stay quiet.
            if not self.stopped:
                self.publisher_.publish(Twist())
                self.stopped = True
                self.next_publish_deadline = None
                self.get_logger().info('Drive mode deselected — rover stopped')
            return

        if self.active_mute(msg.buttons):
            # Same shape as the arm handover: one zero Twist, then silence.
            # twist_mux holds the last command it was given, so simply going
            # quiet would leave the rover running on it.
            if not self.stopped:
                self.publisher_.publish(Twist())
                self.stopped = True
                self.next_publish_deadline = None
            if self._logged_mute is not True:
                self.get_logger().info('Output muted — this console is not commanding the rover')
                self._logged_mute = True
            return
        if self._logged_mute is not False:
            self.get_logger().info('Output live')
            self._logged_mute = False

        # Rate limit before building anything. Checked after the stop path above
        # so a handover always gets through immediately.
        now = self.get_clock().now().nanoseconds / 1e9
        if self.min_publish_period > 0.0:
            if self.next_publish_deadline is None:
                self.next_publish_deadline = now
            if now < self.next_publish_deadline:
                return
            # max() clamps the catch-up burst that would otherwise follow a gap
            # (mode handover, board unplugged) where the deadline fell behind.
            self.next_publish_deadline = max(
                now, self.next_publish_deadline + self.min_publish_period)

        num_axes = len(msg.axes)

        def get_axis_raw(name):
            idx = self.axes[name]
            if not 0 <= idx < num_axes:
                return 0.0
            return apply_deadzone(msg.axes[idx], self.deadzone)

        # Defined in terms of get_axis_raw so the dead band cannot apply to the
        # scaled pair and miss `steer`, which reads the yaw axis unscaled.
        def get_axis_val(name):
            return get_axis_raw(name) * self.scales[name]

        vy_enabled = self.active_vy(msg.buttons)
        if vy_enabled != self._logged_vy:
            self.get_logger().info(f'Strafe (vy): {"ENABLED" if vy_enabled else "DISABLED"}')
            self._logged_vy = vy_enabled

        vx = get_axis_val('linear_x')
        # Zeroed at the source rather than after the mode, so the clamp and the
        # curvature both see the speed the rover is actually being asked for.
        vy = get_axis_val('linear_y') if vy_enabled else 0.0
        wz = get_axis_val('angular_z')
        # Curvature asks the yaw stick for a radius, not a rate, so it reads
        # the axis unscaled — angular_z_scale would otherwise quietly change
        # the tightest turn available.
        steer = get_axis_raw('angular_z')

        # Clamp before the mode runs, not after: curvature derives yaw from the
        # speed actually commanded, so it has to see the clamped pair or a
        # diagonal would curve tighter than the same stick position on an axis.
        vx, vy = clamp_linear(vx, vy, self.max_linear_speed)

        mode = self.active_mode(msg.buttons)
        if mode != self._logged_mode:
            self.get_logger().info(f'Steering mode: {mode}')
            self._logged_mode = mode

        vx, vy, wz = build_twist(
            mode, vx, vy, wz, steer, self.max_curvature, self.angle_probe_speed,
            vy_enabled)

        granny = self.active_granny(msg.buttons)
        if granny != self._logged_granny:
            self.get_logger().info(f'Granny mode: {"ENABLED" if granny else "DISABLED"}')
            self._logged_granny = granny
        vx, vy, wz = apply_granny(vx, vy, wz, self.granny_speed_scale, granny)

        twist = Twist()
        twist.linear.x = vx
        twist.linear.y = vy
        twist.angular.z = wz

        self.stopped = False
        self.publisher_.publish(twist)

    def check_timeout(self):
        if self.last_joy_time is None or self.stopped:
            return

        age = (self.get_clock().now() - self.last_joy_time).nanoseconds / 1e9
        if age < self.joy_timeout:
            return

        self.get_logger().warn(f"No joy message for {age:.2f}s, commanding stop")
        self.publisher_.publish(Twist())
        self.stopped = True
        self.next_publish_deadline = None


def main(args=None):
    rclpy.init(args=args)
    node = JoyToCmdVelNode()
    try:
        rclpy.spin(node)
    except (KeyboardInterrupt, ExternalShutdownException):
        # ExternalShutdownException is the normal path when the launch file or a
        # supervisor stops us, and rclpy's own SIGINT handler has usually shut
        # the context down before the finally block runs — so an unguarded
        # shutdown() raises. Both used to end a plain Ctrl-C in a traceback and
        # a non-zero exit, which reads as a crash in the launch log.
        pass
    finally:
        node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()


if __name__ == '__main__':
    main()

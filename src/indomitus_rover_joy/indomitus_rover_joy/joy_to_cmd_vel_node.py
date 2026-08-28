import math

import rclpy
from rcl_interfaces.msg import SetParametersResult
from rclpy.executors import ExternalShutdownException
from rclpy.node import Node
from sensor_msgs.msg import Joy
from geometry_msgs.msg import Twist


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

        self.max_linear_speed = self.get_parameter(
            'max_linear_speed').get_parameter_value().double_value

        self.joy_timeout = self.get_parameter('joy_timeout').get_parameter_value().double_value
        self.last_joy_time = None
        self.stopped = True

        # Subscriber
        self.subscription = self.create_subscription(
            Joy,
            'joy',
            self.joy_callback,
            10)

        # Publisher
        self.publisher_ = self.create_publisher(Twist, 'cmd_vel', 10)

        if self.joy_timeout > 0.0:
            self.watchdog = self.create_timer(self.joy_timeout / 2.0, self.check_timeout)

        self.get_logger().info("Joy to CmdVel Node started")

    def _on_set_parameters(self, params):
        for param in params:
            for name in ('linear_x', 'linear_y', 'angular_z'):
                if param.name == f'{name}_axis':
                    self.axes[name] = int(param.value)
                elif param.name == f'{name}_scale':
                    self.scales[name] = float(param.value)
            if param.name == 'max_linear_speed':
                self.max_linear_speed = float(param.value)
            if param.name == 'publish_rate':
                rate = float(param.value)
                self.min_publish_period = 1.0 / rate if rate > 0.0 else 0.0
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

        twist = Twist()
        num_axes = len(msg.axes)

        def get_axis_val(name):
            idx = self.axes[name]
            return msg.axes[idx] * self.scales[name] if 0 <= idx < num_axes else 0.0

        twist.linear.x = get_axis_val('linear_x')
        twist.linear.y = get_axis_val('linear_y')
        twist.angular.z = get_axis_val('angular_z')

        # Square gate: a full diagonal is 1.0 on both axes, so the magnitude
        # runs to sqrt(2) even though neither axis is over its own limit.
        # Shrink the pair along its own direction rather than clamping each
        # axis separately - per-axis clamping keeps the magnitude and bends
        # the heading, so the rover would not go where the stick points.
        # Yaw is a separate rate in rad/s and is not part of this.
        speed = math.hypot(twist.linear.x, twist.linear.y)
        if 0.0 < self.max_linear_speed < speed:
            shrink = self.max_linear_speed / speed
            twist.linear.x *= shrink
            twist.linear.y *= shrink

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

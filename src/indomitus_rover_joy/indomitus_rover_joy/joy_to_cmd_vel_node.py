import rclpy
from rclpy.node import Node
from sensor_msgs.msg import Joy
from geometry_msgs.msg import Twist


class JoyToCmdVelNode(Node):
    def __init__(self):
        super().__init__('joy_to_cmd_vel_node')

        # Axis indices into the 6-axis Joy from serial_joy_node:
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
        # Stop the rover if Joy messages stop arriving (serial unplugged, board
        # reset, node killed). 0.0 disables the watchdog.
        self.declare_parameter('joy_timeout', 0.5)
        # The 3 sticks drive either the rover or the arm; a panel switch picks
        # which. Index into Joy.buttons (the joystick board's own 9 switches,
        # which arrive in the same message as the axes, so the mode and the
        # stick values can never disagree). -1 disables gating.
        self.declare_parameter('mode_switch_index', 0)
        self.declare_parameter('mode_switch_value', 1)

        self.mode_switch_index = self.get_parameter('mode_switch_index').get_parameter_value().integer_value
        self.mode_switch_value = self.get_parameter('mode_switch_value').get_parameter_value().integer_value

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
                self.get_logger().info('Drive mode deselected — rover stopped')
            return

        twist = Twist()
        num_axes = len(msg.axes)

        def get_axis_val(param_name, scale_param_name):
            idx = self.get_parameter(param_name).get_parameter_value().integer_value
            scale = self.get_parameter(scale_param_name).get_parameter_value().double_value
            return msg.axes[idx] * scale if 0 <= idx < num_axes else 0.0

        twist.linear.x = get_axis_val('linear_x_axis', 'linear_x_scale')
        twist.linear.y = get_axis_val('linear_y_axis', 'linear_y_scale')
        twist.angular.z = get_axis_val('angular_z_axis', 'angular_z_scale')

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


def main(args=None):
    rclpy.init(args=args)
    node = JoyToCmdVelNode()
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        node.destroy_node()
        rclpy.shutdown()


if __name__ == '__main__':
    main()

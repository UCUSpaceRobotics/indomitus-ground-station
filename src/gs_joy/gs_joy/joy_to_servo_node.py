import rclpy
from rcl_interfaces.msg import SetParametersResult
from rclpy.executors import ExternalShutdownException
from rclpy.node import Node
from sensor_msgs.msg import Joy
from geometry_msgs.msg import TwistStamped

# Logical name -> (axis parameter, which scale it uses).
AXES = (
    ('linear_x', 'linear_x_axis', 'linear_scale'),
    ('linear_y', 'linear_y_axis', 'linear_scale'),
    ('linear_z', 'linear_z_axis', 'linear_scale'),
    ('angular_x', 'angular_x_axis', 'angular_scale'),
    ('angular_y', 'angular_y_axis', 'angular_scale'),
    ('angular_z', 'angular_z_axis', 'angular_scale'),
)


class JoyToServoNode(Node):
    def __init__(self):
        super().__init__('joy_to_servo_node')
        
        # Parameters for Cartesian Control
        self.declare_parameter('linear_x_axis', 1)
        self.declare_parameter('linear_y_axis', 0)
        self.declare_parameter('linear_z_axis', -1)
        self.declare_parameter('angular_x_axis', -1)
        self.declare_parameter('angular_y_axis', 3)
        self.declare_parameter('angular_z_axis', 2)
        
        self.declare_parameter('linear_scale', 1.0)
        self.declare_parameter('angular_scale', 1.0)
        self.declare_parameter('frame_id', 'base_link')

        # Mirror of joy_to_cmd_vel_node's gate: the same panel switch hands the
        # 3 sticks to the rover or to the arm, so this node takes the opposite
        # value. -1 disables gating.
        self.declare_parameter('mode_switch_index', 0)
        self.declare_parameter('mode_switch_value', 0)
        # Ceiling on outgoing jog commands, independent of the /joy rate. Same
        # reasoning as joy_to_cmd_vel_node: the sticks run at 200 Hz, but every
        # TwistStamped is a packet over the rover link. MoveIt Servo halts on
        # incoming_command_timeout (0.1 s by default), so stay well above 10 Hz.
        # 0.0 publishes on every message.
        self.declare_parameter('publish_rate', 50.0)

        self.mode_switch_index = self.get_parameter('mode_switch_index').get_parameter_value().integer_value
        self.mode_switch_value = self.get_parameter('mode_switch_value').get_parameter_value().integer_value

        publish_rate = self.get_parameter('publish_rate').get_parameter_value().double_value
        self.min_publish_period = 1.0 / publish_rate if publish_rate > 0.0 else 0.0
        # Advancing deadline, not elapsed-since-last-publish: the latter drops
        # every jittery early frame, so a cap at or above the input rate would
        # quietly cost throughput instead of being a no-op.
        self.next_publish_deadline = None

        # Cached rather than read per callback — this ran 8 get_parameter()
        # calls per message, which at 200 Hz is 1600 lookups a second for values
        # that almost never change. _on_set_parameters keeps live retuning.
        self.frame_id = self.get_parameter('frame_id').get_parameter_value().string_value
        self.scales = {
            name: self.get_parameter(name).get_parameter_value().double_value
            for name in ('linear_scale', 'angular_scale')
        }
        self.axes = {
            name: self.get_parameter(param).get_parameter_value().integer_value
            for name, param, _ in AXES
        }
        self.add_on_set_parameters_callback(self._on_set_parameters)

        self.stopped = True

        # Subscriber
        self.subscription = self.create_subscription(
            Joy,
            'joy',
            self.joy_callback,
            10)
        
        # Publisher for MoveIt Servo. Absolute: MoveIt Servo runs outside the
        # gs namespace, on the rover/arm side.
        self.publisher_ = self.create_publisher(TwistStamped, '/servo_node/delta_twist_cmds', 10)
        
        self.get_logger().info("Joy to MoveIt Servo Node started")

    def _on_set_parameters(self, params):
        axis_params = {param: name for name, param, _ in AXES}
        for param in params:
            if param.name in axis_params:
                self.axes[axis_params[param.name]] = int(param.value)
            elif param.name in self.scales:
                self.scales[param.name] = float(param.value)
            elif param.name == 'frame_id':
                self.frame_id = str(param.value)
            elif param.name == 'publish_rate':
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
        if not self.mode_selected(msg.buttons):
            # Handing the sticks to the rover: one zero twist so MoveIt Servo
            # halts the arm instead of holding the last jog command. Bypasses
            # the rate limit — a halt must never wait on a throttle.
            if not self.stopped:
                ts = TwistStamped()
                ts.header.stamp = self.get_clock().now().to_msg()
                ts.header.frame_id = self.frame_id
                self.publisher_.publish(ts)
                self.stopped = True
                self.next_publish_deadline = None
                self.get_logger().info('Arm mode deselected — arm stopped')
            return

        now = self.get_clock().now()
        if self.min_publish_period > 0.0:
            now_s = now.nanoseconds / 1e9
            if self.next_publish_deadline is None:
                self.next_publish_deadline = now_s
            if now_s < self.next_publish_deadline:
                return
            # max() clamps the catch-up burst after a gap (mode handover, board
            # unplugged) that left the deadline in the past.
            self.next_publish_deadline = max(
                now_s, self.next_publish_deadline + self.min_publish_period)

        self.stopped = False

        ts = TwistStamped()
        ts.header.stamp = now.to_msg()
        ts.header.frame_id = self.frame_id

        num_axes = len(msg.axes)

        def get_axis_val(name, scale_name):
            idx = self.axes[name]
            return msg.axes[idx] * self.scales[scale_name] if 0 <= idx < num_axes else 0.0

        ts.twist.linear.x = get_axis_val('linear_x', 'linear_scale')
        ts.twist.linear.y = get_axis_val('linear_y', 'linear_scale')
        ts.twist.linear.z = get_axis_val('linear_z', 'linear_scale')

        ts.twist.angular.x = get_axis_val('angular_x', 'angular_scale')
        ts.twist.angular.y = get_axis_val('angular_y', 'angular_scale')
        ts.twist.angular.z = get_axis_val('angular_z', 'angular_scale')

        self.publisher_.publish(ts)

def main(args=None):
    rclpy.init(args=args)
    node = JoyToServoNode()
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

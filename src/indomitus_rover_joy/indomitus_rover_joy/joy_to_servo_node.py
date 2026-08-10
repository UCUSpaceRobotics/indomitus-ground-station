import rclpy
from rclpy.node import Node
from sensor_msgs.msg import Joy
from geometry_msgs.msg import TwistStamped

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

        self.mode_switch_index = self.get_parameter('mode_switch_index').get_parameter_value().integer_value
        self.mode_switch_value = self.get_parameter('mode_switch_value').get_parameter_value().integer_value
        self.stopped = True
        
        # Subscriber
        self.subscription = self.create_subscription(
            Joy,
            'joy',
            self.joy_callback,
            10)
        
        # Publisher for MoveIt Servo
        self.publisher_ = self.create_publisher(TwistStamped, 'servo_node/delta_twist_cmds', 10)
        
        self.get_logger().info("Joy to MoveIt Servo Node started")

    def mode_selected(self, buttons):
        if self.mode_switch_index < 0:
            return True
        if self.mode_switch_index >= len(buttons):
            return False
        return buttons[self.mode_switch_index] == self.mode_switch_value

    def joy_callback(self, msg):
        ts = TwistStamped()
        ts.header.stamp = self.get_clock().now().to_msg()
        ts.header.frame_id = self.get_parameter('frame_id').get_parameter_value().string_value

        if not self.mode_selected(msg.buttons):
            # Handing the sticks to the rover: one zero twist so MoveIt Servo
            # halts the arm instead of holding the last jog command.
            if not self.stopped:
                self.publisher_.publish(ts)
                self.stopped = True
                self.get_logger().info('Arm mode deselected — arm stopped')
            return

        self.stopped = False

        linear_scale = self.get_parameter('linear_scale').get_parameter_value().double_value
        angular_scale = self.get_parameter('angular_scale').get_parameter_value().double_value
        
        # Mapping axes (with safety checks for array length)
        num_axes = len(msg.axes)
        
        def get_axis_val(param_name):
            idx = self.get_parameter(param_name).get_parameter_value().integer_value
            return msg.axes[idx] if 0 <= idx < num_axes else 0.0

        ts.twist.linear.x = get_axis_val('linear_x_axis') * linear_scale
        ts.twist.linear.y = get_axis_val('linear_y_axis') * linear_scale
        ts.twist.linear.z = get_axis_val('linear_z_axis') * linear_scale
        
        ts.twist.angular.x = get_axis_val('angular_x_axis') * angular_scale
        ts.twist.angular.y = get_axis_val('angular_y_axis') * angular_scale
        ts.twist.angular.z = get_axis_val('angular_z_axis') * angular_scale
            
        self.publisher_.publish(ts)

def main(args=None):
    rclpy.init(args=args)
    node = JoyToServoNode()
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        node.destroy_node()
        rclpy.shutdown()

if __name__ == '__main__':
    main()

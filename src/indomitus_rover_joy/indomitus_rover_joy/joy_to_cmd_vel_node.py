import rclpy
from rclpy.node import Node
from sensor_msgs.msg import Joy
from geometry_msgs.msg import Twist

class JoyToCmdVelNode(Node):
    def __init__(self):
        super().__init__('joy_to_cmd_vel_node')
        
        # Parameters
        self.declare_parameter('linear_axis', 1)
        self.declare_parameter('angular_axis', 0)
        self.declare_parameter('linear_scale', 1.0)
        self.declare_parameter('angular_scale', 1.0)
        
        self.linear_axis = self.get_parameter('linear_axis').get_parameter_value().integer_value
        self.angular_axis = self.get_parameter('angular_axis').get_parameter_value().integer_value
        self.linear_scale = self.get_parameter('linear_scale').get_parameter_value().double_value
        self.angular_scale = self.get_parameter('angular_scale').get_parameter_value().double_value
        
        # Subscriber
        self.subscription = self.create_subscription(
            Joy,
            'joy',
            self.joy_callback,
            10)
        
        # Publisher
        self.publisher_ = self.create_publisher(Twist, 'cmd_vel', 10)
        
        self.get_logger().info("Joy to CmdVel Node started")

    def joy_callback(self, msg):
        twist = Twist()
        
        if len(msg.axes) > max(self.linear_axis, self.angular_axis):
            twist.linear.x = msg.axes[self.linear_axis] * self.linear_scale
            twist.angular.z = msg.axes[self.angular_axis] * self.angular_scale
            
        self.publisher_.publish(twist)

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

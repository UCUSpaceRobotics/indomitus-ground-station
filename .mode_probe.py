"""Drive joy_to_cmd_vel_node through both steering modes and print what it sends.

In-process publisher + subscriber so there is no race between `topic pub` and
`topic echo`: the same node feeds the sticks and records the Twist that comes
back, and only reports once a frame has actually arrived for each mode.
"""
import rclpy
from rclpy.node import Node
from sensor_msgs.msg import Joy
from geometry_msgs.msg import Twist

VX_AXIS, WZ_AXIS = 0.6, 0.9
SWITCH = 3


class Probe(Node):
    def __init__(self):
        super().__init__('mode_probe')
        self.pub = self.create_publisher(Joy, '/test/joy', 10)
        self.create_subscription(Twist, '/test/cmd_vel', self._on_twist, 10)
        self.mode_value = 0
        self.last = None
        self.create_timer(0.02, self._tick)

    def _tick(self):
        msg = Joy()
        msg.header.stamp = self.get_clock().now().to_msg()
        msg.axes = [0.0, VX_AXIS, WZ_AXIS, 0.0, 0.0, 0.0]
        buttons = [0] * 9
        buttons[SWITCH] = self.mode_value
        msg.buttons = buttons
        self.pub.publish(msg)

    def _on_twist(self, msg):
        self.last = (msg.linear.x, msg.linear.y, msg.angular.z)


def settle(node, spins=200):
    node.last = None
    for _ in range(spins):
        rclpy.spin_once(node, timeout_sec=0.02)
        if node.last is not None and _ > 60:
            break
    return node.last


def main():
    rclpy.init()
    node = Probe()

    node.mode_value = 0
    row = settle(node)
    node.mode_value = 1
    curvature = settle(node)

    print(f'sticks: linear_x axis={VX_AXIS}  yaw axis={WZ_AXIS}')
    print(f'ROW       switch=0 -> vx={row[0]:.4f} vy={row[1]:.4f} wz={row[2]:.4f}')
    print(f'CURVATURE switch=1 -> vx={curvature[0]:.4f} vy={curvature[1]:.4f} wz={curvature[2]:.4f}')

    # row: wz is the yaw axis times angular_z_scale (1.0).
    # curvature: wz = v_signed * steer * max_curvature = 0.3 * 0.9 * 2.0
    expect_row = WZ_AXIS
    expect_curv = (VX_AXIS * 0.5) * WZ_AXIS * 2.0
    ok = abs(row[2] - expect_row) < 1e-6 and abs(curvature[2] - expect_curv) < 1e-6
    print(f'expected  row wz={expect_row:.4f}  curvature wz={expect_curv:.4f}')
    print('OK' if ok else 'MISMATCH')

    node.destroy_node()
    rclpy.shutdown()
    raise SystemExit(0 if ok else 1)


if __name__ == '__main__':
    main()

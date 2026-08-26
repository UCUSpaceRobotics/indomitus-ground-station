import re

import rclpy
import serial
from rclpy.node import Node
from std_msgs.msg import Int32MultiArray

# Line emitted by the esp_32_switches+buttons board: one '0'/'1' char per wired
# pin, 1 = pressed (the firmware already normalizes both expanders). With the
# default IGNORE_MASKs that is 23 chars, left to right:
#   expander A (0x20): P02 P03 P04 P05 P06 P07 P12 P13 P14
#   expander B (0x22): P00..P07 P10 P11 P12 P13 P14 P17
# The board only sends on a debounced change, never periodically.
LINE_RE = re.compile(r'^[01]+$')


class SwitchReaderNode(Node):
    def __init__(self):
        super().__init__('switch_reader_node')

        # Parameters
        self.declare_parameter('port', '/dev/ttyACM1')
        self.declare_parameter('baudrate', 115200)
        # Expected number of bits per frame; 0 accepts any length.
        self.declare_parameter('num_switches', 23)
        # Republish the latched state at this rate so late subscribers still see
        # it. 0.0 publishes only when the board reports a change.
        self.declare_parameter('publish_rate', 10.0)

        port = self.get_parameter('port').get_parameter_value().string_value
        baud = self.get_parameter('baudrate').get_parameter_value().integer_value
        self.num_switches = self.get_parameter('num_switches').get_parameter_value().integer_value
        publish_rate = self.get_parameter('publish_rate').get_parameter_value().double_value

        # Initialize Serial
        try:
            self.ser = serial.Serial(port, baud, timeout=0.1)
            self.get_logger().info(f"Connected to {port} at {baud}")
        except Exception as e:
            self.get_logger().error(f"Failed to connect to {port}: {e}")
            self.ser = None

        self.buffer = b''
        self.state = None

        # Publisher
        self.publisher_ = self.create_publisher(Int32MultiArray, 'switches', 10)

        # Timer to read serial (100Hz; the board debounces over 4 x 5ms polls)
        self.timer = self.create_timer(0.01, self.read_serial)

        if publish_rate > 0.0:
            self.republish_timer = self.create_timer(1.0 / publish_rate, self.republish)

    def read_serial(self):
        if self.ser is None:
            return

        try:
            waiting = self.ser.in_waiting
            if waiting:
                self.buffer += self.ser.read(waiting)
        except Exception as e:
            self.get_logger().error(f"Serial read error: {e}")
            return

        if b'\n' not in self.buffer:
            return

        *lines, self.buffer = self.buffer.split(b'\n')

        for raw in lines:
            line = raw.decode('utf-8', errors='ignore').strip()
            if not line:
                continue

            if not LINE_RE.match(line):
                # Boot banner and the per-expander I2C warnings.
                self.get_logger().debug(f"Ignoring non-data line: {line}")
                continue

            if self.num_switches > 0 and len(line) != self.num_switches:
                self.get_logger().warn(
                    f"Expected {self.num_switches} switch bits, got {len(line)}: {line}"
                )
                continue

            self.state = [int(bit) for bit in line]
            self.publish()

    def republish(self):
        if self.state is not None:
            self.publish()

    def publish(self):
        msg = Int32MultiArray()
        msg.data = self.state
        self.publisher_.publish(msg)


def main(args=None):
    rclpy.init(args=args)
    node = SwitchReaderNode()
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        if node.ser:
            node.ser.close()
        node.destroy_node()
        rclpy.shutdown()


if __name__ == '__main__':
    main()

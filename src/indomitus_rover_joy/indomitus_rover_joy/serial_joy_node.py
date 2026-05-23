import rclpy
from rclpy.node import Node
from sensor_msgs.msg import Joy
import serial

class SerialJoyNode(Node):
    def __init__(self):
        super().__init__('serial_joy_node')
        
        # Parameters
        self.declare_parameter('port', '/dev/ttyACM0')
        self.declare_parameter('baudrate', 115200)
        self.declare_parameter('axis_max', 100.0) 
        self.declare_parameter('deadzone', 0.05)
        
        port = self.get_parameter('port').get_parameter_value().string_value
        baud = self.get_parameter('baudrate').get_parameter_value().integer_value
        self.axis_max = self.get_parameter('axis_max').get_parameter_value().double_value
        self.deadzone = self.get_parameter('deadzone').get_parameter_value().double_value

        # Initialize Serial
        try:
            self.ser = serial.Serial(port, baud, timeout=0.1)
            self.get_logger().info(f"Connected to {port} at {baud}")
        except Exception as e:
            self.get_logger().error(f"Failed to connect to {port}: {e}")
            self.ser = None

        # Publisher
        self.publisher_ = self.create_publisher(Joy, 'joy', 10)
        
        # Timer to poll serial data (50Hz)
        self.timer = self.create_timer(0.02, self.read_serial)

    def read_serial(self):
        if self.ser is None:
            return

        # Loop until we process the latest packet
        while self.ser.in_waiting >= 4:
            # 1. Look for the magic header byte (0xAA)
            if self.ser.read(1) == b'\xAA':
                
                # 2. Read the next 3 bytes (X, Y, Checksum)
                payload = self.ser.read(3)
                if len(payload) == 3:
                    
                    # In Python, indexing a bytes object returns an unsigned int (0-255)
                    raw_x = payload[0]
                    raw_y = payload[1]
                    rx_checksum = payload[2]
                    
                    # 3. Calculate expected checksum
                    # & 0xFF forces it to overflow at 255, perfectly matching C++ uint8_t math
                    expected_cs = (0xAA + raw_x + raw_y) & 0xFF
                    
                    if rx_checksum == expected_cs:
                        # 4. Convert unsigned bytes back to signed integers (-128 to 127)
                        x = int.from_bytes(payload[0:1], byteorder='little', signed=True)
                        y = int.from_bytes(payload[1:2], byteorder='little', signed=True)
                        
                        self.publish_joy(x, y)
                        
                        # 5. Flush old data. We only care about the absolute newest joystick position.
                        self.ser.reset_input_buffer()
                        break 
                    else:
                        self.get_logger().warn("Checksum mismatch! Dropped corrupted packet.")

    def publish_joy(self, x, y):
        msg = Joy()
        msg.header.stamp = self.get_clock().now().to_msg()
        msg.header.frame_id = "joy_serial"
        
        joy_x = float(x) / self.axis_max
        joy_y = float(y) / self.axis_max

        msg.axes = [
            0.0 if abs(joy_x) < self.deadzone else joy_x,
            0.0 if abs(joy_y) < self.deadzone else joy_y
        ]
        msg.buttons = []
        
        self.publisher_.publish(msg)

def main(args=None):
    rclpy.init(args=args)
    node = SerialJoyNode()
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
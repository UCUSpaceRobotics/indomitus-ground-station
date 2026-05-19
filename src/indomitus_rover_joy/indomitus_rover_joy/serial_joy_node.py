import rclpy
from rclpy.node import Node
from sensor_msgs.msg import Joy
import serial
import re

class SerialJoyNode(Node):
    def __init__(self):
        super().__init__('serial_joy_node')
        
        # Parameters
        self.declare_parameter('port', '/dev/ttyACM0')
        self.declare_parameter('baudrate', 115200)
        self.declare_parameter('adc_max', 4095) # Assuming 12-bit ADC
        
        port = self.get_parameter('port').get_parameter_value().string_value
        baud = self.get_parameter('baudrate').get_parameter_value().integer_value
        self.adc_max = self.get_parameter('adc_max').get_parameter_value().integer_value
        
        # Initialize Serial
        try:
            self.ser = serial.Serial(port, baud, timeout=0.1)
            self.get_logger().info(f"Connected to {port} at {baud}")
        except Exception as e:
            self.get_logger().error(f"Failed to connect to {port}: {e}")
            # We don't exit here to allow the node to stay alive, 
            # but it won't do much without serial.
            self.ser = None

        # Publisher
        self.publisher_ = self.create_publisher(Joy, 'joy', 10)
        
        # Timer to poll serial data (50Hz)
        self.timer = self.create_timer(0.02, self.read_serial)
        
        # Regex to parse the format: POT1: 142 (0.11V)  POT2: 2003 (1.61V)  POT3: 1877 (1.51V)
        self.pattern = re.compile(r"POT1:\s*(\d+).*?POT2:\s*(\d+).*?POT3:\s*(\d+)")

    def normalize(self, value):
        # Map 0 -> adc_max to -1.0 -> 1.0
        # Formula: (val - center) / center
        center = self.adc_max / 2.0
        return (float(value) - center) / center

    def read_serial(self):
        if self.ser is None:
            return

        if self.ser.in_waiting > 0:
            try:
                line = self.ser.readline().decode('utf-8', errors='ignore').strip()
                if not line:
                    return
                
                match = self.pattern.search(line)
                if match:
                    pot1 = int(match.group(1))
                    pot2 = int(match.group(2))
                    pot3 = int(match.group(3))
                    
                    msg = Joy()
                    msg.header.stamp = self.get_clock().now().to_msg()
                    msg.header.frame_id = "joy_serial"
                    
                    # Normalizing to -1.0 to 1.0
                    # Note: You might need to invert some axes depending on your hardware
                    msg.axes = [
                        self.normalize(pot1),
                        self.normalize(pot2),
                        self.normalize(pot3)
                    ]
                    # No buttons in this specific output format, but Joy msg needs it
                    msg.buttons = []
                    
                    self.publisher_.publish(msg)
                    # self.get_logger().debug(f"Published: {msg.axes}")
                    
            except Exception as e:
                self.get_logger().warn(f"Error parsing serial data: {e}")

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

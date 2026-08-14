import os
import re

import rclpy
import serial
import yaml
from rcl_interfaces.msg import SetParametersResult
from rclpy.node import Node
from sensor_msgs.msg import Joy
from std_msgs.msg import Int32MultiArray
from std_srvs.srv import Trigger

# Line emitted by the esp32_switch+joy board at 50 Hz, e.g.
#   110110011|498|501|500|512|499|503
# 9 switch bits from the PCF8575 at 0x24, then one 0..1000 value per joystick
# axis in the order J0X, J0Y, J1X, J1Y, J2X, J2Y.
LINE_RE = re.compile(r'^([01]+)((?:\|-?\d+)+)$')

NUM_AXES = 6
CALIBRATION_KEYS = ('axis_map', 'axis_min', 'axis_center', 'axis_max', 'axis_scale', 'deadzone')


class SerialJoyNode(Node):
    def __init__(self):
        super().__init__('serial_joy_node')

        # Parameters
        self.declare_parameter('port', '/dev/ttyACM0')
        # Must match UART_BAUD in the joystick board's firmware. 115200 cannot
        # carry 200 Hz: a 35-byte frame costs 3.0 ms on the wire there.
        self.declare_parameter('baudrate', 921600)
        # Per-axis calibration, in raw firmware units (0..1000). Captured by the
        # calibration wizard in the UI, which writes them back over
        # /serial_joy_node/set_parameters.
        #
        # `axis_max` is whichever end the operator pushed when asked for the
        # positive direction, so it may be numerically *below* axis_min on a
        # stick that is wired backwards — normalize() reads the orientation off
        # these values rather than needing a separate invert flag.
        # Logical -> physical axis. axis_map[i] is the position in the firmware's
        # frame that logical axis i reads from, so /joy always exposes the same
        # meaning regardless of which ADC pin a pot is actually soldered to.
        # The calibration wizard discovers this by watching which axis moves.
        self.declare_parameter('axis_map', list(range(NUM_AXES)))
        self.declare_parameter('axis_min', [0.0] * NUM_AXES)
        self.declare_parameter('axis_center', [500.0] * NUM_AXES)
        self.declare_parameter('axis_max', [1000.0] * NUM_AXES)
        self.declare_parameter('deadzone', 0.05)
        # Post-normalization multiplier, mostly for trimming one stick's travel.
        self.declare_parameter('axis_scale', [1.0] * NUM_AXES)
        # The firmware reports the raw PCF8575 pin state. Whether a closed
        # switch reads 1 or 0 depends on how the panel is wired, so flip it here
        # rather than reflashing.
        self.declare_parameter('invert_switches', False)
        # Where save_calibration writes, and where calibration is restored from
        # at startup. Empty disables both.
        self.declare_parameter('calibration_file', '')

        port = self.get_parameter('port').get_parameter_value().string_value
        baud = self.get_parameter('baudrate').get_parameter_value().integer_value
        self.invert_switches = self.get_parameter('invert_switches').get_parameter_value().bool_value
        self.calibration_file = self.get_parameter('calibration_file').get_parameter_value().string_value

        self.axis_map = self._axis_map_param()
        self.axis_min = self._axis_param('axis_min', 0.0)
        self.axis_center = self._axis_param('axis_center', 500.0)
        self.axis_max = self._axis_param('axis_max', 1000.0)
        self.axis_scale = self._axis_param('axis_scale', 1.0)
        self.deadzone = self.get_parameter('deadzone').get_parameter_value().double_value

        self._load_calibration()

        # Let the UI push calibration without a restart.
        self.add_on_set_parameters_callback(self._on_set_parameters)

        # Initialize Serial
        try:
            self.ser = serial.Serial(port, baud, timeout=0.1)
            self.get_logger().info(f"Connected to {port} at {baud}")
        except Exception as e:
            self.get_logger().error(f"Failed to connect to {port}: {e}")
            self.ser = None

        self.buffer = b''

        # Publishers. `joy/raw` carries the uncalibrated 0..1000 values, which is
        # what the calibration wizard has to see — it cannot capture endpoints
        # through the very mapping it is trying to produce.
        self.publisher_ = self.create_publisher(Joy, 'joy', 10)
        self.raw_publisher_ = self.create_publisher(Int32MultiArray, 'joy/raw', 10)

        self.save_srv = self.create_service(
            Trigger, '~/save_calibration', self._on_save_calibration)

        # Poll at the firmware's send rate (200 Hz). Polling slower than the
        # board sends does not merely add latency: read_serial keeps only the
        # newest frame in the buffer and drops the rest, so a 50 Hz timer
        # against a 200 Hz board would discard 3 frames in 4.
        self.timer = self.create_timer(0.005, self.read_serial)

    # ── parameters ───────────────────────────────────────────────────────────

    def _axis_map_param(self):
        values = [int(v) for v in self.get_parameter('axis_map').get_parameter_value().integer_array_value]
        if len(values) != NUM_AXES or not all(0 <= v < NUM_AXES for v in values):
            self.get_logger().warn(
                f"axis_map {values} is not {NUM_AXES} indices in [0,{NUM_AXES}); using identity")
            return list(range(NUM_AXES))
        return values

    def _axis_param(self, name, fallback):
        values = list(self.get_parameter(name).get_parameter_value().double_array_value)
        if len(values) < NUM_AXES:
            self.get_logger().warn(
                f"{name} has {len(values)} entries, expected {NUM_AXES}; padding with {fallback}")
            values += [fallback] * (NUM_AXES - len(values))
        return values

    def _on_set_parameters(self, params):
        for param in params:
            if param.name in ('axis_min', 'axis_center', 'axis_max', 'axis_scale'):
                if len(param.value) != NUM_AXES:
                    return SetParametersResult(
                        successful=False,
                        reason=f"{param.name} needs exactly {NUM_AXES} values")
            elif param.name == 'axis_map':
                if len(param.value) != NUM_AXES:
                    return SetParametersResult(
                        successful=False,
                        reason=f"axis_map needs exactly {NUM_AXES} entries")
                if not all(0 <= int(v) < NUM_AXES for v in param.value):
                    return SetParametersResult(
                        successful=False,
                        reason=f"axis_map entries must be in [0,{NUM_AXES})")
            elif param.name == 'deadzone':
                if not 0.0 <= param.value < 1.0:
                    return SetParametersResult(
                        successful=False, reason="deadzone must be in [0.0, 1.0)")

        # Only commit once every parameter in the batch has been validated.
        for param in params:
            if param.name == 'axis_map':
                self.axis_map = [int(v) for v in param.value]
            elif param.name == 'axis_min':
                self.axis_min = list(param.value)
            elif param.name == 'axis_center':
                self.axis_center = list(param.value)
            elif param.name == 'axis_max':
                self.axis_max = list(param.value)
            elif param.name == 'axis_scale':
                self.axis_scale = list(param.value)
            elif param.name == 'deadzone':
                self.deadzone = float(param.value)
            elif param.name == 'invert_switches':
                self.invert_switches = bool(param.value)

        return SetParametersResult(successful=True)

    # ── calibration persistence ──────────────────────────────────────────────

    def _calibration_dict(self):
        return {
            'axis_map': [int(v) for v in self.axis_map],
            'axis_min': [float(v) for v in self.axis_min],
            'axis_center': [float(v) for v in self.axis_center],
            'axis_max': [float(v) for v in self.axis_max],
            'axis_scale': [float(v) for v in self.axis_scale],
            'deadzone': float(self.deadzone),
        }

    def _load_calibration(self):
        path = self.calibration_file
        if not path:
            return
        if not os.path.exists(path):
            self.get_logger().info(f"No calibration at {path}, using defaults")
            return

        try:
            with open(path) as f:
                loaded = yaml.safe_load(f) or {}
            # Stored in ros2 param-file layout so `ros2 param load` also works.
            values = loaded.get(self.get_name(), {}).get('ros__parameters', {})

            for key in CALIBRATION_KEYS:
                if key not in values:
                    continue
                if key == 'deadzone':
                    self.deadzone = float(values[key])
                elif key == 'axis_map':
                    mapping = [int(v) for v in values[key]]
                    if len(mapping) != NUM_AXES or not all(0 <= v < NUM_AXES for v in mapping):
                        self.get_logger().warn('Ignoring axis_map: out of range')
                        continue
                    self.axis_map = mapping
                else:
                    axis_values = [float(v) for v in values[key]]
                    if len(axis_values) != NUM_AXES:
                        self.get_logger().warn(f"Ignoring {key}: expected {NUM_AXES} values")
                        continue
                    setattr(self, key, axis_values)

            self.get_logger().info(f"Loaded calibration from {path}")
        except Exception as e:
            self.get_logger().error(f"Failed to load calibration from {path}: {e}")

    def _on_save_calibration(self, request, response):
        path = self.calibration_file
        if not path:
            response.success = False
            response.message = 'calibration_file parameter is empty'
            return response

        try:
            directory = os.path.dirname(path)
            if directory:
                os.makedirs(directory, exist_ok=True)
            with open(path, 'w') as f:
                yaml.safe_dump(
                    {self.get_name(): {'ros__parameters': self._calibration_dict()}},
                    f,
                    default_flow_style=False,
                )
            response.success = True
            response.message = f'Calibration saved to {path}'
            self.get_logger().info(response.message)
        except Exception as e:
            response.success = False
            response.message = f'Failed to save calibration: {e}'
            self.get_logger().error(response.message)

        return response

    # ── axis mapping ─────────────────────────────────────────────────────────

    def normalize(self, index, value):
        center = self.axis_center[index]
        delta = float(value) - center

        # Pick the endpoint on the same side of centre as the current reading.
        # Multiplying instead of comparing keeps this correct for a stick whose
        # max sits below its centre (wired backwards).
        to_max = self.axis_max[index] - center
        span = to_max if delta * to_max >= 0.0 else -(self.axis_min[index] - center)

        if abs(span) < 1e-6:
            return 0.0

        norm = delta / span

        if abs(norm) < self.deadzone:
            norm = 0.0
        elif self.deadzone < 1.0:
            # Rescale the live region back to full travel so the stick doesn't
            # jump from 0 to `deadzone` the moment it leaves the dead band.
            sign = 1.0 if norm > 0.0 else -1.0
            norm = sign * (abs(norm) - self.deadzone) / (1.0 - self.deadzone)

        norm *= self.axis_scale[index]

        return max(-1.0, min(1.0, norm))

    # ── serial ───────────────────────────────────────────────────────────────

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

        # Only the newest complete frame is worth publishing; anything older is
        # already stale by the time we get here.
        for raw in reversed(lines):
            line = raw.decode('utf-8', errors='ignore').strip()
            if not line:
                continue

            match = LINE_RE.match(line)
            if match is None:
                # Boot banner and I2C warnings share the port with the data.
                self.get_logger().debug(f"Ignoring non-data line: {line}")
                continue

            switch_bits = match.group(1)
            axis_values = [int(v) for v in match.group(2).split('|') if v]
            self.publish_joy(switch_bits, axis_values)
            return

    def publish_joy(self, switch_bits, axis_values):
        # Raw stays in the firmware's own order — the calibration wizard needs
        # to see physical axes to work out which one moved.
        raw_msg = Int32MultiArray()
        raw_msg.data = axis_values
        self.raw_publisher_.publish(raw_msg)

        msg = Joy()
        msg.header.stamp = self.get_clock().now().to_msg()
        msg.header.frame_id = "joy_serial"

        axes = []
        for logical in range(NUM_AXES):
            physical = self.axis_map[logical]
            if physical < len(axis_values):
                axes.append(self.normalize(logical, axis_values[physical]))
            else:
                axes.append(0.0)
        msg.axes = axes
        msg.buttons = [
            (1 - int(bit)) if self.invert_switches else int(bit)
            for bit in switch_bits
        ]

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

"""Both console boards in one node.

The operator console has two ESP32 boards on two USB serial ports, and until now
each had its own node with its own copy of the same read-lines-off-a-port loop.
They are one piece of hardware from the operator's point of view — the panel in
front of them — and splitting them bought nothing but two places to fix every
serial bug. This node owns both ports and publishes what each board says on its
own topic:

    /joy       sensor_msgs/Joy        calibrated sticks + the joy board's switches
    /joy/raw   Int32MultiArray        the same sticks, uncalibrated
    /switches  Int32MultiArray        the button board's 23 toggles

The two boards stay independent inside here. They run at different bauds and
different rates, and one being unplugged must never stop the other publishing —
that asymmetry is the whole reason the console can lose its sticks and keep its
switches, which is exactly how the last outage presented.
"""

import os
import re

import rclpy
import serial
import yaml
from rcl_interfaces.msg import SetParametersResult
from rclpy.executors import ExternalShutdownException
from rclpy.node import Node
from sensor_msgs.msg import Joy
from std_msgs.msg import Int32MultiArray
from std_srvs.srv import Trigger

# Line emitted by the esp32_switch+joy board at 200 Hz, e.g.
#   110110011|498|501|500|512|499|503
# 9 switch bits from the PCF8575 at 0x24, then one 0..1000 value per joystick
# axis in the order J0X, J0Y, J1X, J1Y, J2X, J2Y.
JOY_LINE_RE = re.compile(r'^([01]+)((?:\|-?\d+)+)$')

# Line emitted by the esp_32_switches+buttons board: one '0'/'1' char per wired
# pin, 1 = pressed (the firmware already normalizes both expanders). With the
# default IGNORE_MASKs that is 23 chars, left to right:
#   expander A (0x20): P02 P03 P04 P05 P06 P07 P12 P13 P14
#   expander B (0x22): P00..P07 P10 P11 P12 P13 P14 P17
# The board only sends on a debounced change, never periodically.
SWITCH_LINE_RE = re.compile(r'^[01]+$')

NUM_AXES = 6
CALIBRATION_KEYS = ('axis_map', 'axis_min', 'axis_center', 'axis_max', 'axis_scale', 'deadzone')

# Calibration used to be saved under the joystick node's name. Read it back so
# a console that was calibrated before the two nodes merged keeps its sticks.
LEGACY_CALIBRATION_KEY = 'serial_joy_node'


class BoardPort:
    """One board's serial port, reopened for as long as it takes.

    The old nodes opened the port once in __init__ and set self.ser = None
    forever if that failed, so a board plugged in a second after startup — or
    one that re-enumerated when its USB dropped — stayed dead until someone
    relaunched. Both boards go through this class now, so both recover.
    """

    def __init__(self, node, label, port, baud, retry_period=2.0):
        self.node = node
        self.label = label
        self.port = port
        self.baud = baud
        self.retry_period = retry_period

        self.ser = None
        self.buffer = b''
        self._next_open_attempt = 0.0
        self._open_failure_logged = False

    def _now(self):
        return self.node.get_clock().now().nanoseconds / 1e9

    def _open(self):
        """Try to open the port, at most once every retry_period seconds."""
        now = self._now()
        if now < self._next_open_attempt:
            return False
        self._next_open_attempt = now + self.retry_period

        try:
            self.ser = serial.Serial(self.port, self.baud, timeout=0.1)
        except Exception as e:
            # Once per outage, not once per retry: at a 2 s retry this would
            # otherwise be 30 identical lines a minute for an unplugged board.
            if not self._open_failure_logged:
                self.node.get_logger().error(
                    f"{self.label}: cannot open {self.port} at {self.baud}: {e}")
                self._open_failure_logged = True
            self.ser = None
            return False

        self.node.get_logger().info(f"{self.label}: connected to {self.port} at {self.baud}")
        self._open_failure_logged = False
        self.buffer = b''
        return True

    def _drop(self, reason):
        self.node.get_logger().error(f"{self.label}: {reason}; will reopen {self.port}")
        try:
            if self.ser is not None:
                self.ser.close()
        except Exception:
            pass
        self.ser = None
        self.buffer = b''
        self._next_open_attempt = self._now() + self.retry_period

    def read_lines(self):
        """Return every complete line waiting on the port, oldest first."""
        if self.ser is None and not self._open():
            return []

        try:
            waiting = self.ser.in_waiting
            if waiting:
                self.buffer += self.ser.read(waiting)
        except Exception as e:
            # A yanked USB cable surfaces here, not at open().
            self._drop(f"serial read error: {e}")
            return []

        if b'\n' not in self.buffer:
            return []

        *lines, self.buffer = self.buffer.split(b'\n')

        decoded = []
        for raw in lines:
            line = raw.decode('utf-8', errors='ignore').strip()
            if line:
                decoded.append(line)
        return decoded

    def close(self):
        try:
            if self.ser is not None:
                self.ser.close()
        except Exception:
            pass
        self.ser = None


class ConsoleBoardsNode(Node):
    def __init__(self):
        super().__init__('console_boards')

        # ── ports ────────────────────────────────────────────────────────────
        # Defaults are by-id paths on purpose. Both boards are the same CH340,
        # so /dev/ttyACM* numbering is kernel enumeration order and swaps on a
        # replug; addressing them by ACM number is how the sticks end up silent
        # while the switches still work. Override per machine in the repo .env.
        self.declare_parameter('joy_port', '/dev/ttyACM0')
        # Must match UART_BAUD in the joystick board's firmware. 115200 cannot
        # carry 200 Hz: a 35-byte frame costs 3.0 ms on the wire there.
        self.declare_parameter('joy_baudrate', 921600)
        self.declare_parameter('switch_port', '/dev/ttyACM1')
        self.declare_parameter('switch_baudrate', 115200)

        # ── joystick calibration ─────────────────────────────────────────────
        # Per-axis calibration, in raw firmware units (0..1000). Captured by the
        # calibration wizard in the UI, which writes them back over
        # /console_boards/set_parameters.
        #
        # `axis_max` is whichever end the operator pushed when asked for the
        # positive direction, so it may be numerically *below* axis_min on a
        # stick that is wired backwards — normalize() reads the orientation off
        # these values rather than needing a separate invert flag.
        # Logical -> physical axis. axis_map[i] is the position in the firmware's
        # frame that logical axis i reads from, so /joy always exposes the same
        # meaning regardless of which ADC pin a pot is actually soldered to.
        # The calibration wizard discovers this by watching which axis moved.
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

        # ── button board ─────────────────────────────────────────────────────
        # Expected number of bits per frame; 0 accepts any length.
        self.declare_parameter('num_switches', 23)
        # Republish the latched switch state at this rate so late subscribers
        # still see it. 0.0 publishes only when the board reports a change.
        self.declare_parameter('switch_publish_rate', 10.0)

        self.invert_switches = self.get_parameter('invert_switches').get_parameter_value().bool_value
        self.calibration_file = self.get_parameter('calibration_file').get_parameter_value().string_value
        self.num_switches = self.get_parameter('num_switches').get_parameter_value().integer_value
        switch_publish_rate = self.get_parameter(
            'switch_publish_rate').get_parameter_value().double_value

        self.axis_map = self._axis_map_param()
        self.axis_min = self._axis_param('axis_min', 0.0)
        self.axis_center = self._axis_param('axis_center', 500.0)
        self.axis_max = self._axis_param('axis_max', 1000.0)
        self.axis_scale = self._axis_param('axis_scale', 1.0)
        self.deadzone = self.get_parameter('deadzone').get_parameter_value().double_value

        self._load_calibration()

        # Let the UI push calibration without a restart.
        self.add_on_set_parameters_callback(self._on_set_parameters)

        self.joy_board = BoardPort(
            self, 'joy board',
            self.get_parameter('joy_port').get_parameter_value().string_value,
            self.get_parameter('joy_baudrate').get_parameter_value().integer_value)
        self.switch_board = BoardPort(
            self, 'button board',
            self.get_parameter('switch_port').get_parameter_value().string_value,
            self.get_parameter('switch_baudrate').get_parameter_value().integer_value)

        # Publishers. `joy/raw` carries the uncalibrated 0..1000 values, which is
        # what the calibration wizard has to see — it cannot capture endpoints
        # through the very mapping it is trying to produce.
        self.joy_pub = self.create_publisher(Joy, 'joy', 10)
        self.joy_raw_pub = self.create_publisher(Int32MultiArray, 'joy/raw', 10)
        self.switch_pub = self.create_publisher(Int32MultiArray, 'switches', 10)

        self.switch_state = None

        self.save_srv = self.create_service(
            Trigger, '~/save_calibration', self._on_save_calibration)

        # Poll at the joy board's send rate (200 Hz). Polling slower than the
        # board sends does not merely add latency: read_joy keeps only the
        # newest frame and drops the rest, so a 50 Hz timer against a 200 Hz
        # board would discard 3 frames in 4.
        self.joy_timer = self.create_timer(0.005, self.read_joy)
        # The button board debounces over 4 x 5 ms polls and sends on change.
        self.switch_timer = self.create_timer(0.01, self.read_switches)

        if switch_publish_rate > 0.0:
            self.switch_republish_timer = self.create_timer(
                1.0 / switch_publish_rate, self.republish_switches)

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
            if not values:
                # Pre-merge file. Saving once rewrites it under this node's
                # name, so this path is only taken until the next save.
                values = loaded.get(LEGACY_CALIBRATION_KEY, {}).get('ros__parameters', {})
                if values:
                    self.get_logger().info(
                        f"Loading calibration saved under '{LEGACY_CALIBRATION_KEY}'")

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

    # ── joy board ────────────────────────────────────────────────────────────

    def read_joy(self):
        lines = self.joy_board.read_lines()

        # Only the newest complete frame is worth publishing; anything older is
        # already stale by the time we get here.
        for line in reversed(lines):
            match = JOY_LINE_RE.match(line)
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
        self.joy_raw_pub.publish(raw_msg)

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

        self.joy_pub.publish(msg)

    # ── button board ─────────────────────────────────────────────────────────

    def read_switches(self):
        for line in self.switch_board.read_lines():
            if not SWITCH_LINE_RE.match(line):
                # Boot banner and the per-expander I2C warnings.
                self.get_logger().debug(f"Ignoring non-data line: {line}")
                continue

            if self.num_switches > 0 and len(line) != self.num_switches:
                self.get_logger().warn(
                    f"Expected {self.num_switches} switch bits, got {len(line)}: {line}"
                )
                continue

            self.switch_state = [int(bit) for bit in line]
            self.publish_switches()

    def republish_switches(self):
        if self.switch_state is not None:
            self.publish_switches()

    def publish_switches(self):
        msg = Int32MultiArray()
        msg.data = self.switch_state
        self.switch_pub.publish(msg)


def main(args=None):
    rclpy.init(args=args)
    node = ConsoleBoardsNode()
    try:
        rclpy.spin(node)
    except (KeyboardInterrupt, ExternalShutdownException):
        # ExternalShutdownException is the normal path when the launch file or a
        # supervisor stops us, and rclpy's own SIGINT handler has usually shut
        # the context down before the finally block runs — so an unguarded
        # shutdown() raises. Both read as a crash in the launch log.
        pass
    finally:
        # Closing a serial port is interruptible, and a second Ctrl-C lands
        # here; the ports go away with the process regardless.
        try:
            node.joy_board.close()
            node.switch_board.close()
        except KeyboardInterrupt:
            pass
        node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()


if __name__ == '__main__':
    main()

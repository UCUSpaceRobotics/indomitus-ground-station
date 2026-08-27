"""Publishes the console panel as an SDL gamepad, for the arm.

``gamepad_servo_node`` on the rover drives the arm from ``sensor_msgs/Joy`` in
the canonical SDL GameController layout. The console is not a gamepad, so this
node assembles that layout from the two panel boards and publishes it on its
own topic — by default ``/arm/joy``, not ``/joy``, because ``/joy`` already
carries the console's own raw frame for the drive nodes and the calibration
wizard. Point the rover's gamepad node at this topic:

    ros2 launch arm_tasks gamepad.launch.py --ros-args -r joy:=/arm/joy

Which console control fills which SDL slot is configuration, not code: only the
people holding the console know what is under each label. Bindings are one flat
string parameter per slot (see arm_bindings.parse_bind), so the UI can rewrite
any single one over ``rcl_interfaces/SetParameters`` while the arm is live, and
``~/save_bindings`` persists them.

The frame goes out at a steady rate whether or not anything moved. The arm
stops itself if ``/joy`` is quiet for 0.2 s, and the button board only transmits
on change, so publishing on input alone would look like a dropped controller
every time the operator held still.
"""

import os

import rclpy
import yaml
from rcl_interfaces.msg import SetParametersResult
from rclpy.executors import ExternalShutdownException
from rclpy.node import Node
from sensor_msgs.msg import Joy
from std_msgs.msg import Int32MultiArray
from std_srvs.srv import Trigger

from indomitus_rover_joy.arm_bindings import (
    AXIS_KEYS,
    BUTTON_KEYS,
    SLOTS_BY_KEY,
    SOURCE_JOY,
    SOURCE_JOY_AXIS,
    SOURCE_SWITCHES,
    GamepadFrame,
    build_bindings,
    conflicts,
    format_bind,
)

#: Parameter prefix for the mapping, e.g. `bind.safe_pose`. Flat rather than a
#: nested tree so one slot can be set on its own from the UI.
BIND_PREFIX = 'bind.'

ALL_KEYS = BUTTON_KEYS + AXIS_KEYS


class ArmGamepadNode(Node):
    def __init__(self):
        super().__init__('arm_gamepad')

        self.declare_parameter('output_topic', '/arm/joy')
        # 50 Hz: the arm's /joy timeout is 0.2 s, so this has wide margin while
        # staying well under the 200 Hz the stick board actually produces.
        self.declare_parameter('publish_rate', 50.0)
        # Where save_bindings writes and startup restores from. Empty disables
        # both, which is what a laptop running the UI without a panel wants.
        self.declare_parameter('bindings_file', '')
        # Published straight through as Joy.header.frame_id.
        self.declare_parameter('frame_id', 'arm_gamepad')

        for key in ALL_KEYS:
            self.declare_parameter(BIND_PREFIX + key, '')

        self.frame_id = self.get_parameter('frame_id').get_parameter_value().string_value
        self.bindings_file = self.get_parameter('bindings_file').get_parameter_value().string_value

        self.bindings = self._bindings_from_parameters()
        self._load_bindings()
        self._warn_about_conflicts()

        self.frame = GamepadFrame(self.bindings)

        # Validate before committing, so a bad string from the UI is refused
        # with a reason instead of leaving the arm half-remapped.
        self.add_on_set_parameters_callback(self._on_set_parameters)

        output_topic = self.get_parameter('output_topic').get_parameter_value().string_value
        self.publisher_ = self.create_publisher(Joy, output_topic, 10)

        self.create_subscription(Joy, 'joy', self._on_joy, 10)
        self.create_subscription(Int32MultiArray, 'switches', self._on_switches, 10)

        self.save_srv = self.create_service(Trigger, '~/save_bindings', self._on_save_bindings)

        rate = self.get_parameter('publish_rate').get_parameter_value().double_value
        self.timer = self.create_timer(1.0 / max(rate, 1.0), self._publish)

        self.get_logger().info(
            f'publishing SDL gamepad on {output_topic} at {rate:g} Hz '
            f'({sum(b is not None for b in self.bindings.values())}/{len(ALL_KEYS)} controls bound)')

    # ── bindings ─────────────────────────────────────────────────────────────

    def _bindings_from_parameters(self):
        specs = {
            key: self.get_parameter(BIND_PREFIX + key).get_parameter_value().string_value
            for key in ALL_KEYS
        }
        try:
            return build_bindings(specs)
        except ValueError as exc:
            # A launch-time typo. Come up unbound rather than not at all: the
            # UI is the tool for fixing this, and it needs the node running.
            self.get_logger().error(f'ignoring the configured mapping: {exc}')
            return {key: None for key in ALL_KEYS}

    def _warn_about_conflicts(self):
        for first, second, bind in conflicts(self.bindings):
            self.get_logger().warn(
                f'{first} and {second} are both on {format_bind(bind)}')

    def _on_set_parameters(self, params):
        pending = {}
        for param in params:
            if not param.name.startswith(BIND_PREFIX):
                continue
            key = param.name[len(BIND_PREFIX):]
            if key not in SLOTS_BY_KEY:
                return SetParametersResult(
                    successful=False, reason=f'{param.name}: not an arm control')
            try:
                build_bindings({key: param.value})
            except ValueError as exc:
                return SetParametersResult(successful=False, reason=str(exc))
            pending[key] = param.value

        if not pending:
            return SetParametersResult(successful=True)

        # Every string in the batch parsed, so committing cannot half-fail.
        self.bindings.update(build_bindings(pending))
        self.frame.set_bindings(self.bindings)
        self._warn_about_conflicts()
        for key, text in pending.items():
            self.get_logger().info(f'{key} -> {text or "unbound"}')

        return SetParametersResult(successful=True)

    def _bindings_dict(self):
        return {key: format_bind(self.bindings.get(key)) for key in ALL_KEYS}

    def _load_bindings(self):
        path = self.bindings_file
        if not path:
            return
        if not os.path.exists(path):
            self.get_logger().info(f'No arm mapping at {path}, using the launch defaults')
            return

        try:
            with open(path) as f:
                loaded = yaml.safe_load(f) or {}
            # ros2 param-file layout, so `ros2 param load` works on it too.
            values = loaded.get(self.get_name(), {}).get('ros__parameters', {})
            specs = {
                key: values.get(BIND_PREFIX + key, '')
                for key in ALL_KEYS
                if BIND_PREFIX + key in values
            }
            if not specs:
                self.get_logger().warn(f'{path} has no {BIND_PREFIX}* entries; ignoring it')
                return
            self.bindings.update(build_bindings(specs))
            self.get_logger().info(f'Loaded arm mapping from {path}')
        except (ValueError, OSError, yaml.YAMLError) as exc:
            self.get_logger().error(f'Failed to load arm mapping from {path}: {exc}')

    def _on_save_bindings(self, request, response):
        path = self.bindings_file
        if not path:
            response.success = False
            response.message = 'bindings_file parameter is empty'
            return response

        try:
            directory = os.path.dirname(path)
            if directory:
                os.makedirs(directory, exist_ok=True)
            payload = {
                self.get_name(): {
                    'ros__parameters': {
                        BIND_PREFIX + key: text for key, text in self._bindings_dict().items()
                    }
                }
            }
            with open(path, 'w') as f:
                yaml.safe_dump(payload, f, default_flow_style=False)
            response.success = True
            response.message = f'Arm mapping saved to {path}'
            self.get_logger().info(response.message)
        except OSError as exc:
            response.success = False
            response.message = f'Failed to save arm mapping: {exc}'
            self.get_logger().error(response.message)

        return response

    # ── inputs ───────────────────────────────────────────────────────────────

    def _on_joy(self, msg):
        # One message carries both, and they must not be split: a mode switch
        # and the stick values in the same frame can never disagree.
        self.frame.update(SOURCE_JOY_AXIS, msg.axes)
        self.frame.update(SOURCE_JOY, msg.buttons)

    def _on_switches(self, msg):
        self.frame.update(SOURCE_SWITCHES, msg.data)

    def _publish(self):
        msg = Joy()
        msg.header.stamp = self.get_clock().now().to_msg()
        msg.header.frame_id = self.frame_id
        msg.axes = self.frame.axes()
        msg.buttons = self.frame.buttons()
        self.publisher_.publish(msg)


def main(args=None):
    rclpy.init(args=args)
    node = ArmGamepadNode()
    try:
        rclpy.spin(node)
    except (KeyboardInterrupt, ExternalShutdownException):
        pass
    finally:
        node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()


if __name__ == '__main__':
    main()

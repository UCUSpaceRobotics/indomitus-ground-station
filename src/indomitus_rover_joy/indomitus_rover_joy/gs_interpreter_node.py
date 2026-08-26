"""Console switches to rover services.

Deliberately a separate node from joy_to_cmd_vel_node rather than an extension
of it. That node carries the drive path — an advancing-deadline rate limiter,
two stop paths that bypass it, a 0.2 s watchdog — and it is the thing that
stops the rover. This one is event-driven, idle almost all the time, and talks
to services that may not answer. Mixing them means a fault in the second can
delay the first.

Holds no rover state. A switch knows its own position, so it sends that
absolutely; what the rover ends up in comes back on drive/state and
lights/state, which the console reads directly.

Subscribes:
  joy       sensor_msgs/Joy             the joystick board's own 9 switches
  switches  std_msgs/Int32MultiArray    the button board's 23

Calls (std_srvs/SetBool, on the rover):
  /drive/power  /drive/compact  /lights/spotlight  /lights/beautiful
"""

import rclpy
from rclpy.executors import ExternalShutdownException
from rclpy.node import Node
from sensor_msgs.msg import Joy
from std_msgs.msg import Int32MultiArray
from std_srvs.srv import SetBool

from indomitus_rover_joy.switch_bindings import (
    SOURCE_JOY,
    SOURCE_SWITCHES,
    EdgeTracker,
    build_bindings,
)


class GsInterpreterNode(Node):
    def __init__(self):
        super().__init__('gs_interpreter')

        # Which switch drives which service is config, not code: only the
        # people holding the console know what is under each label, and the
        # wiring changes more often than this node will.
        self.declare_parameter('bindings', [''])
        names = [n for n in self.get_parameter('bindings').value if n]

        specs = {}
        for name in names:
            spec = {}
            for key, default in (('source', SOURCE_SWITCHES), ('index', -1),
                                 ('service', ''), ('invert', False)):
                self.declare_parameter(f'{name}.{key}', default)
                spec[key] = self.get_parameter(f'{name}.{key}').value
            specs[name] = spec

        try:
            bindings = build_bindings(specs)
        except ValueError as exc:
            self.get_logger().error(f'bad binding config: {exc} — no switches are wired')
            bindings = []

        self._tracker = EdgeTracker(bindings)
        self._service_clients = {}
        # One in flight per service. A bounced switch would otherwise queue
        # requests whose replies land in an order nobody controls.
        self._call_pending = {}
        for binding in bindings:
            if binding.service not in self._service_clients:
                self._service_clients[binding.service] = self.create_client(
                    SetBool, binding.service)
                self._call_pending[binding.service] = False

        self.create_subscription(Joy, 'joy', self._on_joy, 10)
        self.create_subscription(Int32MultiArray, 'switches', self._on_switches, 10)

        if bindings:
            self.get_logger().info(
                'GsInterpreter started — switches wired:\n' + '\n'.join(
                    f'  {b.source}[{b.index}] -> {b.service}'
                    f'{" (inverted)" if b.invert else ""}'
                    for b in bindings))
        else:
            self.get_logger().warn('GsInterpreter started with nothing wired')

    def _on_joy(self, msg: Joy):
        self._apply(self._tracker.update(SOURCE_JOY, msg.buttons))

    def _on_switches(self, msg: Int32MultiArray):
        self._apply(self._tracker.update(SOURCE_SWITCHES, msg.data))

    def _apply(self, changes):
        for binding, desired in changes:
            self._call(binding, desired)

    def _call(self, binding, desired: bool):
        client = self._service_clients[binding.service]
        label = 'ON' if desired else 'OFF'

        if self._call_pending[binding.service]:
            self.get_logger().warn(
                f'{binding.name} -> {label}: {binding.service} still busy, dropped')
            return
        if not client.service_is_ready():
            # Worth a warning rather than a silent no-op: from the console this
            # looks like a switch that does nothing, and the operator needs to
            # know the rover is not listening rather than that the switch is dead.
            self.get_logger().warn(
                f'{binding.name} -> {label}: {binding.service} not available')
            return

        request = SetBool.Request()
        request.data = desired
        self._call_pending[binding.service] = True
        client.call_async(request).add_done_callback(
            lambda future: self._on_result(future, binding, label))

    def _on_result(self, future, binding, label: str):
        self._call_pending[binding.service] = False
        try:
            result = future.result()
        except Exception as exc:
            self.get_logger().error(f'{binding.name} -> {label} failed: {exc!r}')
            return

        level = self.get_logger().info if result.success else self.get_logger().warn
        level(f'{binding.name} -> {label}: {result.message}')


def main(args=None):
    rclpy.init(args=args)
    node = GsInterpreterNode()
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

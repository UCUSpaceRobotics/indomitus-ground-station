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

Which control drives which function is set from the ground station's settings
dialog while everything is running, not only from a file at launch. The whole
set crosses as one JSON string in the `binds` parameter — one parameter rather
than a tree of `name.key` ones, because a tree cannot grow a new entry at
runtime: rclpy will not declare a parameter nobody knew about at startup, so a
newly bound function would have nowhere to land. `~/save_bindings` writes the
current set back to `bindings_file` so it survives a restart.

Rebinding drops the edge detector's baselines, so the first sample after a
change fires nothing. That is the same safety property as startup: a switch
left up must never energise anything just because the wiring was edited.

Subscribes:
  joy       sensor_msgs/Joy             the joystick board's own 9 switches
  switches  std_msgs/Int32MultiArray    the button board's 23

Calls, per switch_bindings.FUNCTIONS:
  std_srvs/SetBool   from latching switches, which have a position to send
  std_srvs/Trigger   from momentary buttons, and for actions with no "off"
"""

import json
import os

import rclpy
import yaml
from rcl_interfaces.msg import SetParametersResult
from rclpy.executors import ExternalShutdownException
from rclpy.node import Node
from sensor_msgs.msg import Joy
from std_msgs.msg import Int32MultiArray
from std_srvs.srv import SetBool, Trigger

from gs_joy.switch_bindings import (
    KIND_TRIGGER,
    SOURCE_JOY,
    SOURCE_SWITCHES,
    EdgeTracker,
    binds_from_specs,
    build_bindings,
    specs_from_binds,
)

#: Top-level key in bindings_file, matching the ros2 param-file layout so
#: `ros2 param load` also works on it.
NODE_KEY = 'gs_interpreter'


class GsInterpreterNode(Node):
    def __init__(self):
        super().__init__('gs_interpreter')

        # The live set, as JSON. Empty means "fall back to the legacy tree".
        self.declare_parameter('binds', '')
        # Switch bits the settings dialog has already given to camera
        # selection. Enforced here as well as in the dialog: a bit that both
        # picks a feed and energises the drive is a wiring mistake nobody can
        # see by looking at the panel.
        self.declare_parameter('camera_switches', [-1])
        self.declare_parameter('bindings_file', '')
        # Legacy tree, still read when `binds` is empty so an existing
        # gs_bindings.yaml keeps working until it is saved once from the UI.
        self.declare_parameter('bindings', [''])

        self.bindings_file = self.get_parameter('bindings_file').value

        self._bindings = []
        self._tracker = EdgeTracker([])
        # NOT _clients: rclpy's Node keeps its own list under that name and
        # create_client() appends to it, so shadowing it crashes the node the
        # first time anything is wired.
        self._service_clients = {}
        # One in flight per service. A bounced switch would otherwise queue
        # requests whose replies land in an order nobody controls.
        self._call_pending = {}

        self._install(self._initial_bindings(), 'startup')

        self.add_on_set_parameters_callback(self._on_set_parameters)
        self.create_service(Trigger, '~/save_bindings', self._on_save_bindings)

        self.create_subscription(Joy, 'joy', self._on_joy, 10)
        self.create_subscription(Int32MultiArray, 'switches', self._on_switches, 10)

    # ── configuration ────────────────────────────────────────────────────────

    def _camera_switches(self, values=None):
        if values is None:
            values = self.get_parameter('camera_switches').value
        return [(SOURCE_SWITCHES, int(i)) for i in values if i >= 0]

    def _initial_bindings(self):
        """Prefer the runtime set; fall back to the launch-time tree."""
        raw = self.get_parameter('binds').value
        if raw:
            try:
                return self._parse(raw)
            except ValueError as exc:
                self.get_logger().error(f'bad binds parameter: {exc} — falling back')

        names = [n for n in self.get_parameter('bindings').value if n]
        if not names:
            return []

        specs = {}
        for name in names:
            spec = {}
            for key, default in (('source', SOURCE_SWITCHES), ('index', -1),
                                 ('service', ''), ('invert', False)):
                self.declare_parameter(f'{name}.{key}', default)
                spec[key] = self.get_parameter(f'{name}.{key}').value
            specs[name] = spec

        try:
            return build_bindings(specs)
        except ValueError as exc:
            self.get_logger().error(
                f'bad binding config: {exc} — no switches are wired')
            return []

    def _parse(self, raw, claimed=None):
        try:
            specs = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError(f'not valid JSON: {exc}') from exc
        if not isinstance(specs, list):
            raise ValueError('expected a list of binds')
        if claimed is None:
            claimed = self._camera_switches()
        return binds_from_specs(specs, claimed=claimed)

    def _install(self, bindings, why):
        """Swap in a new set. Baselines are dropped, so nothing replays."""
        self._bindings = list(bindings)
        self._tracker = EdgeTracker(self._bindings)

        self._service_clients = {}
        self._call_pending = {}
        for binding in self._bindings:
            if binding.service in self._service_clients:
                continue
            srv_type = Trigger if binding.kind == KIND_TRIGGER else SetBool
            self._service_clients[binding.service] = self.create_client(
                srv_type, binding.service)
            self._call_pending[binding.service] = False

        if self._bindings:
            self.get_logger().info(
                f'GsInterpreter bindings ({why}):\n' + '\n'.join(
                    f'  {b.source}[{b.index}] -> {b.service} ({b.kind})'
                    f'{" (inverted)" if b.invert else ""}'
                    for b in self._bindings))
        else:
            self.get_logger().warn(f'GsInterpreter has nothing wired ({why})')

    def _on_set_parameters(self, params):
        """Validate and, when it holds up, apply in one step.

        rclpy runs this *before* it commits the value, and the post-set hook is
        not available on every distro this has to run on, so the new set is
        installed from the incoming value rather than read back afterwards. The
        whole batch is validated before anything is installed: a half-applied
        console is worse than the working one the operator already has.
        """
        incoming = {p.name: p.value for p in params}
        if 'binds' not in incoming and 'camera_switches' not in incoming:
            return SetParametersResult(successful=True)

        raw = incoming.get('binds', self.get_parameter('binds').value)
        claimed = self._camera_switches(
            incoming.get('camera_switches',
                         self.get_parameter('camera_switches').value))

        if not raw:
            # Clearing the set is a legitimate request: it unwires everything.
            self._install([], 'cleared')
            return SetParametersResult(successful=True)

        try:
            bindings = self._parse(raw, claimed)
        except ValueError as exc:
            return SetParametersResult(successful=False, reason=str(exc))

        self._install(bindings, 'reconfigured')
        return SetParametersResult(successful=True)

    def _on_save_bindings(self, request, response):
        path = self.bindings_file
        if not path:
            response.success = False
            response.message = 'no bindings_file configured'
            return response

        try:
            directory = os.path.dirname(path)
            if directory:
                os.makedirs(directory, exist_ok=True)
            payload = {NODE_KEY: {'ros__parameters': {
                'binds': json.dumps(specs_from_binds(self._bindings)),
                # -1, not an empty list: rclpy cannot infer the type of an
                # empty array, and [0] would claim switch bit 0 for cameras
                # and refuse whatever bind is saved alongside it.
                'camera_switches': [i for _, i in self._camera_switches()] or [-1],
            }}}
            with open(path, 'w') as handle:
                yaml.safe_dump(payload, handle, default_flow_style=False, sort_keys=True)
            response.success = True
            response.message = f'Saved {len(self._bindings)} binds to {path}'
            self.get_logger().info(response.message)
        except Exception as exc:
            response.success = False
            response.message = f'Failed to save bindings: {exc}'
            self.get_logger().error(response.message)

        return response

    # ── panel ────────────────────────────────────────────────────────────────

    def _on_joy(self, msg: Joy):
        self._apply(self._tracker.update(SOURCE_JOY, msg.buttons))

    def _on_switches(self, msg: Int32MultiArray):
        self._apply(self._tracker.update(SOURCE_SWITCHES, msg.data))

    def _apply(self, changes):
        for binding, desired in changes:
            if binding.kind == KIND_TRIGGER and not desired:
                # An edge-fired service has no "off". Releasing the button, or
                # flipping the switch back down, is not a second request.
                continue
            self._call(binding, desired)

    def _call(self, binding, desired: bool):
        client = self._service_clients[binding.service]
        label = 'FIRE' if binding.kind == KIND_TRIGGER else ('ON' if desired else 'OFF')

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

        if binding.kind == KIND_TRIGGER:
            request = Trigger.Request()
        else:
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

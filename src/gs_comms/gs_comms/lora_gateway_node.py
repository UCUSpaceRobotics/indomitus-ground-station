#!/usr/bin/env python3
"""Carry operator commands over the LoRa fallback while Wi-Fi is not carrying them.

`link_status_node` decides which path is live and publishes it on the latched
/link/active_path. This node is the half that acts on that decision, and until
it existed the decision went nowhere: nothing in the repo subscribed to that
topic at all.

While the path is LORA it takes the operator's Twist from /cmd_vel_ext - the
same command the panel joystick feeds into twist_mux - converts it to the
compact form mast/lora_bridge.py accepts, and writes it to the bridge's TCP
service on the mast Pi. While the path is WIFI it sends nothing, so the rover
never has two sources driving it at once; DDS carries commands as usual and the
bridge's own command timeout keeps the radio quiet.

It also republishes the bridge's link metrics, so the fallback path is
observable the same way the primary one already is.

The radio is slow and this does not hide that. Measured on the bench: ~240 ms
round trip, and the mast polls at 3 Hz because a faster poll cannot have its
reply back before the next one goes out. Roughly 0.3-0.6 s from stick to rover,
three updates a second. That is a "crawl somewhere safe and stop" path, not a
driving path.

Commands are sent slightly faster than the mast polls. The bridge holds the
latest command and reverts it to zero if nobody refreshes it, so overshooting
its rate costs a few bytes of TCP and means a single missed send never lets the
command go stale mid-manoeuvre.
"""

import json
import socket
import threading

import rclpy
from diagnostic_msgs.msg import DiagnosticArray, DiagnosticStatus, KeyValue
from geometry_msgs.msg import Twist
from rclpy.executors import ExternalShutdownException
from rclpy.node import Node
from rclpy.qos import QoSDurabilityPolicy, QoSProfile, QoSReliabilityPolicy
from std_msgs.msg import Bool, Float32, String

PATH_LORA = "LORA"


def _clamp_percent(value):
    return max(-100, min(100, int(round(value))))


class LoraGatewayNode(Node):

    def __init__(self):
        super().__init__("lora_gateway_node")

        self.declare_parameter("bridge_host", "10.44.0.1")
        self.declare_parameter("bridge_port", 4001)
        # Above the mast's 3 Hz poll: the bridge only ever transmits the newest
        # command, so sending faster wastes nothing and keeps it fresh.
        self.declare_parameter("send_rate_hz", 5.0)
        # Full-scale values the percentages are relative to. These match
        # joy_to_cmd_vel_node's linear_scale/angular_scale; if those change,
        # these must change with them or the rover will scale commands wrongly.
        self.declare_parameter("max_linear", 0.5)
        self.declare_parameter("max_angular", 1.0)
        # A Twist this old is not an instruction any more. Stop rather than
        # repeat it - the operator may already have let go of the stick.
        self.declare_parameter("cmd_timeout", 0.5)

        self.host = self.get_parameter("bridge_host").value
        self.port = int(self.get_parameter("bridge_port").value)
        self.max_linear = float(self.get_parameter("max_linear").value)
        self.max_angular = float(self.get_parameter("max_angular").value)
        self.cmd_timeout = float(self.get_parameter("cmd_timeout").value)
        send_rate = float(self.get_parameter("send_rate_hz").value)

        latched = QoSProfile(
            depth=1,
            reliability=QoSReliabilityPolicy.RELIABLE,
            durability=QoSDurabilityPolicy.TRANSIENT_LOCAL,
        )

        self.pub_state = self.create_publisher(String, "lora/state", 10)
        self.pub_loss = self.create_publisher(Float32, "lora/loss_pct", 10)
        self.pub_rtt = self.create_publisher(Float32, "lora/rtt_ms", 10)
        self.pub_failsafe = self.create_publisher(Bool, "lora/rover_failsafe", latched)
        self.pub_relaying = self.create_publisher(Bool, "lora/relaying", latched)
        self.pub_diag = self.create_publisher(DiagnosticArray, "/diagnostics", 10)

        # TRANSIENT_LOCAL to match the publisher: a VOLATILE subscriber would
        # connect happily and then wait for the next transition, which may never
        # come if the path was decided before this node started.
        self.create_subscription(
            String, "link/active_path", self._on_path, latched)
        self.create_subscription(Twist, "cmd_vel_ext", self._on_twist, 10)
        # Nothing publishes this yet. It exists so an operator-console stop
        # button has somewhere to go without inventing new plumbing then.
        self.create_subscription(Bool, "lora/estop", self._on_estop, 10)

        self._lock = threading.Lock()
        self._sock = None
        self._latest = None
        self._connected = False

        self.active_path = None
        self.relaying = False
        self.estop = False
        self.twist = None
        self.twist_stamp = None

        self._publish_relaying()

        threading.Thread(target=self._reader, daemon=True).start()
        self.create_timer(1.0 / send_rate, self._send_tick)
        self.create_timer(0.5, self._publish_metrics)

        self.get_logger().info(
            f"lora_gateway_node: bridge {self.host}:{self.port}, "
            f"sending at {send_rate} Hz while /link/active_path is {PATH_LORA}")

    # -- subscriptions -----------------------------------------------------

    def _on_path(self, msg):
        if msg.data == self.active_path:
            return
        self.active_path = msg.data
        relaying = msg.data == PATH_LORA
        if relaying and not self.relaying:
            self.get_logger().warn(
                "command path switched to LoRa - relaying over the radio at "
                "~3 Hz, expect 0.3-0.6 s of lag")
        elif self.relaying and not relaying:
            self.get_logger().info("command path back on Wi-Fi - stopping the relay")
            # Do not leave the last command sitting in the bridge for its
            # timeout to clear. Say stop explicitly, once.
            self._send({"vx": 0, "vy": 0, "wz": 0, "estop": False})
        self.relaying = relaying
        self._publish_relaying()

    def _on_twist(self, msg):
        self.twist = msg
        self.twist_stamp = self.get_clock().now()

    def _on_estop(self, msg):
        if msg.data != self.estop:
            self.get_logger().warn(f"e-stop {'engaged' if msg.data else 'released'}")
        self.estop = msg.data

    # -- bridge connection -------------------------------------------------

    def _reader(self):
        """Stream JSON metrics from the bridge, reconnecting forever."""
        backoff = 1.0
        while rclpy.ok():
            try:
                with socket.create_connection((self.host, self.port), timeout=10) as sock:
                    sock.settimeout(10)
                    with self._lock:
                        self._sock = sock
                        self._connected = True
                    self.get_logger().info(f"connected to lora bridge at {self.host}")
                    backoff = 1.0
                    for line in sock.makefile("r"):
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            sample = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        with self._lock:
                            self._latest = sample
            except (OSError, socket.timeout) as exc:
                self.get_logger().warn(f"lora bridge unreachable: {exc}")
            with self._lock:
                self._sock = None
                self._connected = False
                self._latest = None
            threading.Event().wait(backoff)
            backoff = min(backoff * 2, 10.0)

    def _send(self, command):
        with self._lock:
            sock = self._sock
        if sock is None:
            return False
        try:
            sock.sendall((json.dumps(command) + "\n").encode())
            return True
        except OSError:
            # The reader thread owns reconnection; just drop this one.
            return False

    # -- relay -------------------------------------------------------------

    def _send_tick(self):
        if not self.relaying:
            return
        self._send(self._current_command())

    def _current_command(self):
        if self.estop:
            return {"vx": 0, "vy": 0, "wz": 0, "estop": True}

        stale = (self.twist is None or self.twist_stamp is None or
                 (self.get_clock().now() - self.twist_stamp).nanoseconds
                 > self.cmd_timeout * 1e9)
        if stale:
            return {"vx": 0, "vy": 0, "wz": 0, "estop": False}

        return {
            "vx": _clamp_percent(100.0 * self.twist.linear.x / self.max_linear),
            "vy": _clamp_percent(100.0 * self.twist.linear.y / self.max_linear),
            "wz": _clamp_percent(100.0 * self.twist.angular.z / self.max_angular),
            "estop": False,
        }

    # -- reporting ---------------------------------------------------------

    def _publish_relaying(self):
        self.pub_relaying.publish(Bool(data=self.relaying))

    def _publish_metrics(self):
        with self._lock:
            sample = dict(self._latest) if self._latest else None
            connected = self._connected

        if sample is None:
            state = "DOWN"
            reasons = ["no bridge connection" if not connected
                       else "no sample from bridge"]
        else:
            state = sample.get("state", "DOWN")
            reasons = sample.get("reasons", [])

        self.pub_state.publish(String(data=state))
        if sample:
            for key, pub in (("loss_pct_avg", self.pub_loss),
                             ("rtt_ms_avg", self.pub_rtt)):
                value = sample.get(key)
                if value is not None:
                    pub.publish(Float32(data=float(value)))
            failsafe = sample.get("rover_failsafe")
            if failsafe is not None:
                self.pub_failsafe.publish(Bool(data=bool(failsafe)))

        status = DiagnosticStatus()
        status.name = "comms: lora fallback link"
        status.hardware_id = f"{self.host}:{self.port}"
        # A dead fallback only matters urgently once it is the live path. When
        # Wi-Fi is carrying commands a bad LoRa link is a warning, not an error.
        if state == "OK":
            status.level = DiagnosticStatus.OK
        elif self.relaying:
            status.level = DiagnosticStatus.ERROR
        else:
            status.level = DiagnosticStatus.WARN
        status.message = f"{state}, {'relaying' if self.relaying else 'standby'}"
        if reasons:
            status.message += f" ({', '.join(reasons)})"
        for key in ("loss_pct_avg", "rtt_ms_avg", "polls", "replies",
                    "consecutive_misses", "rover_failsafe", "rover_rx_bad"):
            status.values.append(
                KeyValue(key=key, value=str((sample or {}).get(key))))
        status.values.append(
            KeyValue(key="active_path", value=str(self.active_path)))

        msg = DiagnosticArray()
        msg.header.stamp = self.get_clock().now().to_msg()
        msg.status.append(status)
        self.pub_diag.publish(msg)


def main():
    rclpy.init()
    node = LoraGatewayNode()
    try:
        rclpy.spin(node)
    except (KeyboardInterrupt, ExternalShutdownException):
        pass
    finally:
        node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()


if __name__ == "__main__":
    main()

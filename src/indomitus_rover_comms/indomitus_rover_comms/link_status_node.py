#!/usr/bin/env python3
"""Consume the mast Pi's link metrics and decide which command path is live.

The mast Pi runs no ROS (see mast/README.md). It exports two raw TCP services -
`link_monitor.py` on 4002 (JSON Wi-Fi metrics) and `lora_bridge.py` on 4001
(JSON LoRa metrics, and command injection into the radio) - and everything
needing a ROS graph runs here, in the Humble container on the ground-station PC,
where it can be debugged on a desk instead of up a mast.

This node does two jobs and deliberately not a third:

  1. Republish the mast's link metrics as ROS topics + /diagnostics.
  2. Decide, with hysteresis, whether the primary Wi-Fi path or the LoRa
     fallback should carry rover commands, and publish that on
     /link/active_path.

It does NOT move any rover traffic itself. `lora_gateway_node` subscribes to
/link/active_path and starts/stops relaying commands into `lora_bridge.py`'s
port on 4001. Keeping the decision separate from the transport means the
failover logic can be tested by publishing fake metrics, with no radio
involved - and equally, the relay can be tested by publishing a fake
/link/active_path with no failure involved.

Hysteresis is asymmetric on purpose. Failing over is cheap and failing over late
is dangerous - the operator is flying blind while the link is dead - so it trips
after a short run of bad samples. Failing back is the opposite: returning to a
marginal Wi-Fi link and immediately dropping again would flap the command path
mid-manoeuvre, so it requires a much longer run of good samples.
"""

import json
import socket
import threading

import rclpy
from diagnostic_msgs.msg import DiagnosticArray, DiagnosticStatus, KeyValue
from rclpy.executors import ExternalShutdownException
from rclpy.node import Node
from rclpy.qos import QoSDurabilityPolicy, QoSProfile, QoSReliabilityPolicy
from std_msgs.msg import Bool, Float32, String

PATH_WIFI = "WIFI"
PATH_LORA = "LORA"


class LinkStatusNode(Node):

    def __init__(self):
        super().__init__("link_status_node")

        self.declare_parameter("monitor_host", "10.44.0.1")
        self.declare_parameter("monitor_port", 4002)
        # 3 bad samples at ~1 Hz - fast enough that the operator notices the
        # console change before they notice the rover stopped responding.
        self.declare_parameter("fail_after", 3)
        # 15 good samples before trusting Wi-Fi again.
        self.declare_parameter("restore_after", 15)
        self.declare_parameter("publish_rate_hz", 2.0)

        self.host = self.get_parameter("monitor_host").value
        self.port = int(self.get_parameter("monitor_port").value)
        self.fail_after = int(self.get_parameter("fail_after").value)
        self.restore_after = int(self.get_parameter("restore_after").value)

        # Latched: a late subscriber (the operator console reconnecting
        # mid-run) must immediately learn which path is live rather than wait
        # for the next transition, which may never come.
        latched = QoSProfile(
            depth=1,
            reliability=QoSReliabilityPolicy.RELIABLE,
            durability=QoSDurabilityPolicy.TRANSIENT_LOCAL,
        )

        self.pub_state = self.create_publisher(String, "link/state", 10)
        self.pub_signal = self.create_publisher(Float32, "link/signal_dbm", 10)
        self.pub_loss = self.create_publisher(Float32, "link/loss_pct", 10)
        self.pub_rtt = self.create_publisher(Float32, "link/rtt_ms", 10)
        self.pub_path = self.create_publisher(String, "link/active_path", latched)
        self.pub_degraded = self.create_publisher(Bool, "link/degraded", latched)
        self.pub_diag = self.create_publisher(DiagnosticArray, "/diagnostics", 10)

        self._lock = threading.Lock()
        self._latest = None
        self._connected = False

        self.bad_run = 0
        self.good_run = 0
        self.active_path = PATH_WIFI
        self._publish_path()

        threading.Thread(target=self._reader, daemon=True).start()
        self.create_timer(
            1.0 / float(self.get_parameter("publish_rate_hz").value), self._tick)

        self.get_logger().info(
            f"link_status_node: reading {self.host}:{self.port}, "
            f"fail_after={self.fail_after} restore_after={self.restore_after}")

    # -- metric ingest -----------------------------------------------------

    def _reader(self):
        """Stream JSON lines from the mast monitor, reconnecting forever."""
        backoff = 1.0
        while rclpy.ok():
            try:
                with socket.create_connection((self.host, self.port), timeout=10) as s:
                    s.settimeout(10)
                    with self._lock:
                        self._connected = True
                    self.get_logger().info(f"connected to link monitor at {self.host}")
                    backoff = 1.0
                    for line in s.makefile("r"):
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
                self.get_logger().warn(f"link monitor unreachable: {exc}")
            # Losing the monitor means the *wired* mast link is down, which is
            # worse than a bad Wi-Fi link - so drop the sample rather than let
            # the state machine coast on stale data.
            with self._lock:
                self._connected = False
                self._latest = None
            threading.Event().wait(backoff)
            backoff = min(backoff * 2, 10.0)

    # -- state machine -----------------------------------------------------

    def _tick(self):
        with self._lock:
            sample = dict(self._latest) if self._latest else None
            connected = self._connected

        if sample is None:
            state = "DOWN"
            reasons = ["no link metrics" if not connected
                       else "no sample from monitor"]
        else:
            state = sample.get("state", "DOWN")
            reasons = sample.get("reasons", [])

        if state == "DOWN":
            self.bad_run += 1
            self.good_run = 0
        elif state == "OK":
            self.good_run += 1
            self.bad_run = 0
        else:                       # DEGRADED holds position: neither trips
            self.bad_run = 0        # a switch nor counts toward restoring.
            self.good_run = 0

        if self.active_path == PATH_WIFI and self.bad_run >= self.fail_after:
            self.get_logger().error(
                f"primary link DOWN for {self.bad_run} samples ({', '.join(reasons)}) "
                f"- switching command path to LoRa")
            self.active_path = PATH_LORA
            self._publish_path()
        elif self.active_path == PATH_LORA and self.good_run >= self.restore_after:
            self.get_logger().info(
                f"primary link OK for {self.good_run} samples - restoring Wi-Fi")
            self.active_path = PATH_WIFI
            self._publish_path()

        self._publish_metrics(sample, state, reasons)

    def _publish_path(self):
        self.pub_path.publish(String(data=self.active_path))
        self.pub_degraded.publish(Bool(data=self.active_path != PATH_WIFI))

    def _publish_metrics(self, sample, state, reasons):
        self.pub_state.publish(String(data=state))
        if sample:
            for key, pub in (("signal_dbm", self.pub_signal),
                             ("loss_pct_avg", self.pub_loss),
                             ("rtt_ms_avg", self.pub_rtt)):
                val = sample.get(key)
                if val is not None:
                    pub.publish(Float32(data=float(val)))

        status = DiagnosticStatus()
        status.name = "comms: primary wifi link"
        status.hardware_id = (sample or {}).get("iface", "unknown")
        status.level = {
            "OK": DiagnosticStatus.OK,
            "DEGRADED": DiagnosticStatus.WARN,
        }.get(state, DiagnosticStatus.ERROR)
        status.message = f"{state} via {self.active_path}"
        if reasons:
            status.message += f" ({', '.join(reasons)})"
        for key in ("ssid", "freq_mhz", "signal_dbm", "tx_bitrate_mbps",
                    "loss_pct_avg", "rtt_ms_avg", "stalled"):
            status.values.append(
                KeyValue(key=key, value=str((sample or {}).get(key))))
        status.values.append(KeyValue(key="active_path", value=self.active_path))

        msg = DiagnosticArray()
        msg.header.stamp = self.get_clock().now().to_msg()
        msg.status.append(status)
        self.pub_diag.publish(msg)


def main():
    rclpy.init()
    node = LinkStatusNode()
    try:
        rclpy.spin(node)
    except (KeyboardInterrupt, ExternalShutdownException):
        # ExternalShutdownException is the normal path when the launch file or a
        # supervisor stops us; letting it escape prints a traceback that looks
        # like a crash in the logs, which matters when the logs are what you are
        # reading to work out why the link dropped mid-run.
        pass
    finally:
        node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Carry operator commands over the LoRa fallback while Wi-Fi is not carrying them.

`link_status_node` decides which path is live and publishes it on the latched
/link/active_path. This node is the half that acts on that decision, and until
it existed the decision went nowhere: nothing in the repo subscribed to that
topic at all.

While the path is LORA it takes the operator's Twist from /cmd_vel_gs - the
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

## Second radio: rover power and the Jetson reset line

This node also carries the emergency commands - cut rover power, restore it,
pulse the Jetson's reset line - and those go over a *different radio* from
everything above. Two independent 433 MHz links exist and it matters not to
confuse them:

    teleop     GS PC --TCP 4001--> mast Pi --E32 ch 0x17, 4.8k air--> rover
    emergency  GS PC --USB CDC--> ESP32-S3 --E32 ch 0x14, 2.4k air--> rover

The emergency link's rover end is `indomitus-embedded-control/emergency-esp`,
an ESP32-C3 owning three transistor gates: rover power, the Jetson's SYS_RESET*
line, and the Jetson's CAN isolation. It already understands three single-byte
radio commands, defined in that project's `RadioProto`:

    0x00 STOP           -> PowerCut::engage()    (rover power off)
    0x01 RUN            -> PowerCut::release()   (rover power on)
    0x02 REBOOT_JETSON  -> JetsonReset::reset()  (one 100 ms SYS_RESET* pulse)

The console end is `e32-e-stop-gs`, the ESP32-S3 on a USB port of this PC.

### Why this talks to the S3 rather than to the radio

The S3 is not a transparent pipe and must not be used as one. It owns the
physical stop button on the console and it retransmits its own idea of the
state - RUN every 2 s, STOP every 250 ms. A command injected around it does not
survive: send a stop over the air directly and the S3's next unsolicited RUN, at
most two seconds later, powers the rover straight back up.

So the host sets the S3's *desired* state and the S3 combines it with the
button, exactly as the rover combines the radio command with its own button:

    transmitted = button_pressed OR host_stop

One owner of the radio, and the physical button stays authoritative - no host
command can talk over someone holding the stop down.

### Host <-> S3 line protocol

ASCII, newline-terminated, at 115200 on the S3's USB console - the same port its
`[GROUND] ...` logs already come out of, so the two interleave and anything
unrecognised is ignored as a log line.

    host -> S3   POWER RUN       desired state: let the rover be powered
                 POWER STOP      desired state: cut rover power
                 JETSON REBOOT   one-shot, edge-triggered: pulse SYS_RESET*
                 STATUS          ask for an immediate [STATE] line

    S3 -> host   [STATE] power=run|stop button=up|down host=run|stop
                         rover=synced|nosync
                 [ACK] <verb>
                 [NAK] <verb> <reason>

`power=` is what the S3 is actually transmitting (button OR host) and is the
only thing reported as the rover's power state. `host=` echoes what the host
asked for, so a desired state that never took effect is visible rather than
assumed.

### What the emergency half deliberately does not do

- It sends nothing until explicitly commanded. At startup the desired state is
  unknown, and silence is correct: the S3 and the rover both hold whatever
  state they were left in, so restarting this node cannot power-cycle a moving
  rover.
- It sends nothing on shutdown, for the same reason.
- It never restores power on its own. The rover firmware is deliberately not
  latched - one 0x01 clears its stop with no rearm step - so the latch lives
  here: once stopped, power returns only on an explicit set_power(true).
"""

import json
import socket
import threading
import time

import rclpy
import serial
from diagnostic_msgs.msg import DiagnosticArray, DiagnosticStatus, KeyValue
from geometry_msgs.msg import Twist
from rclpy.executors import ExternalShutdownException, MultiThreadedExecutor
from rclpy.node import Node
from rclpy.qos import QoSDurabilityPolicy, QoSProfile, QoSReliabilityPolicy
from std_msgs.msg import Bool, Float32, String
from std_srvs.srv import SetBool, Trigger

PATH_LORA = "LORA"

# Verbs sent to the e32-e-stop-gs board over USB. See the protocol section above.
CMD_POWER_RUN = "POWER RUN"
CMD_POWER_STOP = "POWER STOP"
CMD_JETSON_REBOOT = "JETSON REBOOT"
CMD_STATUS = "STATUS"

POWER_RUN = "run"
POWER_STOP = "stop"


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

        # -- emergency board (second radio; see the module docstring) --------
        # Opt-in, and default OFF on purpose. Opening the wrong port is not a
        # harmless mistake here: all three console boards enumerate as the same
        # anonymous 1a86 USB_Single_Serial bridge, every process in the
        # container runs as root, and root ignores TIOCEXCL - so two nodes can
        # hold one port and silently eat each other's bytes. That is exactly
        # what happened when this defaulted to a /dev/ttyACM<n> guess: it
        # landed on the joystick board and /gs/joy went dead with no error
        # anywhere. Turn it on only with a port pinned by serial number.
        self.declare_parameter("use_estop_board", False)
        # Never a bare /dev/ttyACM<n>: that index depends on plug order and is
        # not stable across a replug. The joystick and button boards are
        # already pinned by serial number in .env; this is the third board.
        self.declare_parameter(
            "estop_port",
            "/dev/serial/by-id/usb-1a86_USB_Single_Serial_5C4C051474-if00")
        self.declare_parameter("estop_baudrate", 115200)
        # The S3 holds the desired state, so this is not a heartbeat the rover
        # depends on. It exists so a reconnect re-asserts intent and a board
        # that missed a line converges within a second.
        self.declare_parameter("estop_refresh_hz", 1.0)
        # How long a service call waits for [STATE] to actually show the change
        # before answering. One radio period plus slack.
        self.declare_parameter("estop_ack_timeout", 1.5)
        # No [STATE] line for this long means the board is not talking, even
        # though the port is still open.
        self.declare_parameter("estop_state_timeout", 5.0)
        # A reset pulse is 100 ms and the firmware drops one that overlaps
        # another. Refusing here gives a real answer instead of a silent drop.
        self.declare_parameter("reboot_min_interval", 5.0)

        self.host = self.get_parameter("bridge_host").value
        self.port = int(self.get_parameter("bridge_port").value)
        self.max_linear = float(self.get_parameter("max_linear").value)
        self.max_angular = float(self.get_parameter("max_angular").value)
        self.cmd_timeout = float(self.get_parameter("cmd_timeout").value)
        send_rate = float(self.get_parameter("send_rate_hz").value)

        self.use_estop_board = bool(self.get_parameter("use_estop_board").value)
        self.estop_port = self.get_parameter("estop_port").value
        self.estop_baudrate = int(self.get_parameter("estop_baudrate").value)
        estop_refresh_hz = float(self.get_parameter("estop_refresh_hz").value)
        self.estop_ack_timeout = float(
            self.get_parameter("estop_ack_timeout").value)
        self.estop_state_timeout = float(
            self.get_parameter("estop_state_timeout").value)
        self.reboot_min_interval = float(
            self.get_parameter("reboot_min_interval").value)

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
        # Absolute: this is joy_to_cmd_vel_node's remapped output, which lives
        # outside the gs namespace on purpose so the rover-side twist_mux sees it.
        self.create_subscription(Twist, "/cmd_vel_gs", self._on_twist, 10)
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

        # -- emergency board --------------------------------------------------
        # Latched, because these describe a state rather than an event: a UI
        # panel connecting after the last change still has to render the truth.
        self.pub_powered = self.create_publisher(
            Bool, "power/rover_powered", latched)
        self.pub_board_ok = self.create_publisher(Bool, "power/board_ok", latched)
        self.pub_estop_link = self.create_publisher(
            Bool, "power/rover_link", latched)
        self.pub_console_button = self.create_publisher(
            Bool, "power/console_button", latched)

        # Same shape as lora/estop above, so one console stop control can drive
        # the soft stop and the hard power cut without inventing new plumbing.
        self.create_subscription(Bool, "power/estop", self._on_power_estop, 10)

        self.create_service(SetBool, "power/set_power", self._srv_set_power)
        self.create_service(Trigger, "power/reboot_jetson", self._srv_reboot_jetson)

        # None until an operator says otherwise: transmitting nothing is what
        # leaves the rover as it was found.
        self._desired_power = None
        self._power_estop_held = False

        self._reported_power = None
        self._reported_button = None
        self._reported_host = None
        self._rover_synced = None
        self._last_state_at = None
        self._last_reboot_at = None

        self._estop_serial = None
        self._estop_write_lock = threading.Lock()
        self._estop_cv = threading.Condition()
        self._published_power = {}

        if self.use_estop_board:
            threading.Thread(target=self._estop_reader, daemon=True).start()
            self.create_timer(1.0 / estop_refresh_hz, self._estop_refresh_tick)
            self.create_timer(1.0, self._publish_power_state)
            self.get_logger().info(
                f"emergency board on {self.estop_port} @{self.estop_baudrate}, "
                f"idle until commanded")
        else:
            self.get_logger().info(
                "use_estop_board is false - power/set_power and "
                "power/reboot_jetson will refuse")

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

    # -- emergency board: serial ------------------------------------------

    def _estop_open(self):
        """Open the S3's port without resetting it.

        Opening a CH34x asserts DTR and RTS, which on an ESP32 dev board are
        wired to EN/BOOT and reset the chip. Restarting this node must not
        reboot the board holding the rover's power state, so both lines are
        cleared before open() rather than after it.
        """
        ser = serial.Serial()
        ser.port = self.estop_port
        ser.baudrate = self.estop_baudrate
        ser.timeout = 1.0
        ser.dtr = False
        ser.rts = False
        ser.exclusive = True
        ser.open()
        return ser

    def _estop_reader(self):
        """Read the board's [STATE]/[ACK] lines, reconnecting forever."""
        backoff = 1.0
        while rclpy.ok():
            try:
                ser = self._estop_open()
                self._estop_serial = ser
                self.get_logger().info(f"emergency board open on {self.estop_port}")
                backoff = 1.0
                # A fresh connection knows nothing. Ask, then re-assert whatever
                # the operator last decided - the board may have been replugged
                # while this node held an opinion about the rover.
                self._estop_write(CMD_STATUS)
                self._estop_refresh_tick()
                with ser:
                    while rclpy.ok():
                        raw = ser.readline()
                        if not raw:
                            continue
                        self._on_estop_line(raw.decode("utf-8", "replace").strip())
            except (OSError, serial.SerialException) as exc:
                self.get_logger().warn(f"emergency board unreachable: {exc}")
            self._estop_serial = None
            with self._estop_cv:
                self._reported_power = None
                self._reported_button = None
                self._reported_host = None
                self._rover_synced = None
                self._last_state_at = None
                self._estop_cv.notify_all()
            time.sleep(backoff)
            backoff = min(backoff * 2, 10.0)

    def _estop_write(self, verb):
        ser = self._estop_serial
        if ser is None:
            return False
        try:
            with self._estop_write_lock:
                ser.write((verb + "\n").encode())
            return True
        except (OSError, serial.SerialException):
            # The reader thread owns reconnection; just drop this one.
            return False

    def _on_estop_line(self, line):
        if line.startswith("[STATE]"):
            self._on_estop_state(line)
        elif line.startswith("[NAK]"):
            self.get_logger().error(f"emergency board rejected a command: {line}")
        elif line:
            # [ACK] and the board's own human-readable log.
            self.get_logger().debug(f"emergency board: {line}")

    def _on_estop_state(self, line):
        fields = {}
        for token in line.split()[1:]:
            key, _, value = token.partition("=")
            if value:
                fields[key] = value

        with self._estop_cv:
            power = fields.get("power")
            if power in (POWER_RUN, POWER_STOP):
                if power != self._reported_power:
                    self.get_logger().warn(f"rover power is now {power.upper()}")
                self._reported_power = power
            self._reported_button = fields.get("button")
            self._reported_host = fields.get("host")
            rover = fields.get("rover")
            if rover is not None:
                self._rover_synced = rover == "synced"
            self._last_state_at = time.monotonic()
            self._estop_cv.notify_all()

    def _estop_board_ok(self):
        stamp = self._last_state_at
        return (stamp is not None and
                (time.monotonic() - stamp) < self.estop_state_timeout)

    def _wait_for_power(self, want):
        """Block until the board reports `want`, or the ack timeout elapses."""
        deadline = time.monotonic() + self.estop_ack_timeout
        with self._estop_cv:
            while self._reported_power != want:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._estop_cv.wait(remaining)
            return True

    # -- emergency board: desired state ------------------------------------

    def _estop_refresh_tick(self):
        """Re-assert the desired state so a missed line converges on its own."""
        if self._desired_power is None:
            return
        self._estop_write(
            CMD_POWER_RUN if self._desired_power else CMD_POWER_STOP)

    def _set_desired_power(self, run, reason):
        if self._desired_power != run:
            self.get_logger().warn(
                f"rover power commanded {'RUN' if run else 'STOP'} ({reason})")
        self._desired_power = run
        return self._estop_write(CMD_POWER_RUN if run else CMD_POWER_STOP)

    def _on_power_estop(self, msg):
        if msg.data:
            self._power_estop_held = True
            self._set_desired_power(False, "console e-stop")
        elif self._power_estop_held:
            # Cleared, but power stays off. An e-stop that undid itself when the
            # control sprang back would be an e-stop in name only; the operator
            # has to ask for power back explicitly.
            self._power_estop_held = False
            self.get_logger().warn(
                "console e-stop released - power stays off until "
                "power/set_power is called with data: true")

    # -- emergency board: services -----------------------------------------

    def _estop_unavailable(self):
        """Reason the board cannot be commanded right now, or None."""
        if not self.use_estop_board:
            return "use_estop_board is false; this console has no e-stop board"
        if not self._estop_board_ok():
            return (f"emergency board on {self.estop_port} is not reporting "
                    "state; nothing was sent")
        return None

    def _srv_set_power(self, request, response):
        unavailable = self._estop_unavailable()
        if unavailable:
            response.success = False
            response.message = unavailable
            return response

        if request.data and self._power_estop_held:
            response.success = False
            response.message = (
                "console e-stop is held; release it before restoring power")
            return response

        want = POWER_RUN if request.data else POWER_STOP
        if not self._set_desired_power(request.data, "service call"):
            response.success = False
            response.message = "write to the emergency board failed"
            return response

        if self._wait_for_power(want):
            response.success = True
            response.message = f"rover power {want}"
        else:
            # The command went out, the board just has not confirmed it. Say
            # exactly that rather than claiming either outcome - the refresh
            # timer keeps re-asserting it regardless.
            response.success = False
            response.message = (
                f"sent, but the board did not report power={want} within "
                f"{self.estop_ack_timeout:.1f} s")
        return response

    def _srv_reboot_jetson(self, request, response):
        del request

        unavailable = self._estop_unavailable()
        if unavailable:
            response.success = False
            response.message = unavailable
            return response

        if self._reported_power == POWER_STOP:
            response.success = False
            response.message = (
                "rover power is cut; there is nothing to reboot. Restore power "
                "first")
            return response

        now = time.monotonic()
        if self._last_reboot_at is not None:
            since = now - self._last_reboot_at
            if since < self.reboot_min_interval:
                response.success = False
                response.message = (
                    f"a reboot was sent {since:.1f} s ago; wait "
                    f"{self.reboot_min_interval - since:.1f} s")
                return response

        if not self._estop_write(CMD_JETSON_REBOOT):
            response.success = False
            response.message = "write to the emergency board failed"
            return response

        self._last_reboot_at = now
        self.get_logger().warn("Jetson reboot pulse requested over the radio")
        # Edge-triggered and unacknowledged by design: the rover fires a 100 ms
        # SYS_RESET* pulse and reports nothing back, so there is no state to
        # wait on. The honest answer is "sent".
        response.success = True
        response.message = "reset pulse sent; the Jetson takes ~40 s to come back"
        return response

    # -- emergency board: reporting ----------------------------------------

    def _publish_power_once(self, publisher, key, value):
        """Publish only on change - these are latched state topics."""
        if self._published_power.get(key) == value:
            return
        self._published_power[key] = value
        publisher.publish(Bool(data=value))

    def _publish_power_state(self):
        board_ok = self._estop_board_ok()
        self._publish_power_once(self.pub_board_ok, "board", board_ok)
        self._publish_power_once(
            self.pub_powered, "power", self._reported_power == POWER_RUN)
        self._publish_power_once(
            self.pub_estop_link, "rover", bool(self._rover_synced))
        self._publish_power_once(
            self.pub_console_button, "button", self._reported_button == "down")

        status = DiagnosticStatus()
        status.name = "comms: rover power (emergency radio)"
        status.hardware_id = str(self.estop_port)

        if not board_ok:
            status.level = DiagnosticStatus.ERROR
            status.message = "emergency board not reporting"
        elif not self._rover_synced:
            status.level = DiagnosticStatus.WARN
            status.message = "board up, no rover echo on the radio"
        elif self._reported_power == POWER_STOP:
            status.level = DiagnosticStatus.WARN
            status.message = "rover power is CUT"
        else:
            status.level = DiagnosticStatus.OK
            status.message = "rover powered"

        status.values = [
            KeyValue(key="port", value=str(self.estop_port)),
            KeyValue(key="power", value=str(self._reported_power)),
            KeyValue(key="console_button", value=str(self._reported_button)),
            KeyValue(key="board_desired", value=str(self._reported_host)),
            KeyValue(key="node_desired", value=str(self._desired_power)),
            KeyValue(key="estop_held", value=str(self._power_estop_held)),
            KeyValue(key="rover_synced", value=str(self._rover_synced)),
        ]

        msg = DiagnosticArray()
        msg.header.stamp = self.get_clock().now().to_msg()
        msg.status.append(status)
        self.pub_diag.publish(msg)

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
    # Multi-threaded on purpose: power/set_power blocks for up to
    # estop_ack_timeout waiting for the board to confirm, and on the
    # single-threaded default that would also stall power/estop - the one
    # callback that must never wait behind anything.
    executor = MultiThreadedExecutor()
    try:
        rclpy.spin(node, executor=executor)
    except (KeyboardInterrupt, ExternalShutdownException):
        pass
    finally:
        # Deliberately no parting command to the emergency board: whatever the
        # rover is doing, it keeps doing. See the module docstring.
        node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()


if __name__ == "__main__":
    main()

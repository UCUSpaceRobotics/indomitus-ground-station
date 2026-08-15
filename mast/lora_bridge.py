#!/usr/bin/env python3
"""Mast-side master for the LoRa fallback link.

Owns the E32-433T30D wired into the Pi 5's GPIO header: the UART, the M0/M1
mode pins and the AUX busy line. Polls the rover at a fixed rate, measures what
comes back, and serves the result as line-delimited JSON over TCP - the same
shape link_monitor.py serves on 4002, so the ground station can consume both
with one parser.

    tcp/4001  <- this, JSON LoRa link metrics + command injection
    tcp/4002  <- link_monitor.py, JSON Wi-Fi link metrics

This replaces the ser2net design the older docstrings described. ser2net is a
dumb byte pipe: it cannot drive M0/M1 or watch AUX, so it cannot configure the
module, cannot avoid transmitting while the module is busy, and cannot measure
anything. Owning the port here costs one small daemon and buys all three.

The channel is half-duplex and shared. If both ends transmit at once the frames
collide and both are lost, so the protocol is strict master/slave: the mast
sends one teleop frame per period and the rover answers only after receiving
one. Nothing else ever transmits.

Clients on 4001 may write one JSON object per line to set the command that gets
polled out:

    {"vx": 20, "vy": 0, "wz": -10, "estop": false}

A command that stops being refreshed goes back to zero after --command-timeout;
the rover independently zeroes its own output after 500 ms of silence.

Usage:
    lora_bridge.py                       # serve on tcp/4001
    lora_bridge.py --ping 200            # bring-up: measure loss and RTT
    lora_bridge.py --chat                # bring-up: raw transparent text
    lora_bridge.py --config read         # print the module's registers
    lora_bridge.py --config write        # write them (30 dBm), then read back
    lora_bridge.py --selftest            # codec vectors, no hardware needed
"""

import argparse
import json
import math
import socket
import socketserver
import sys
import threading
import time
from collections import deque

import lora_frame

# --- link state thresholds -------------------------------------------------
#
# Same vocabulary as link_monitor.py so link_status_node can read either
# stream. Also the same caveat, and it matters more here: these are guesses
# until the range walk in the README has actually been done. A LoRa link does
# not degrade the way Wi-Fi does - it tends to work, then stop.
LOSS_DEGRADED_PCT = 20.0
LOSS_DOWN_PCT = 80.0
RTT_DEGRADED_MS = 400.0
MISSES_DOWN = 5               # consecutive unanswered polls = DOWN regardless of the window
WINDOW = 20                   # polls retained for rolling loss/RTT

# --- module configuration -------------------------------------------------
# See README.md for the bit breakdown. Factory default is C0 00 00 1A 17 44.
CFG_HEAD_SAVE = 0xC0
CFG_ADDH = 0x00
CFG_ADDL = 0x00
CFG_SPED = 0x1B               # 8N1, UART 9600, air 4.8 kbps
CFG_CHAN = 0x17               # 410 + 23 = 433 MHz
CFG_OPTION = {
    "write": 0x44,            # transparent, push-pull, FEC on, 30 dBm
    "low": 0x47,              # as above but 21 dBm
}

CONFIG_BAUD = 9600            # the module's command mode is always 9600 8N1

MODE_NORMAL = 0
MODE_SLEEP = 3


def percentile(values, pct):
    """Nearest-rank percentile. Small samples, no numpy on the Pi."""
    if not values:
        return None
    ordered = sorted(values)
    idx = math.ceil(pct / 100.0 * len(ordered)) - 1
    return ordered[max(0, min(len(ordered) - 1, idx))]


# --- GPIO ------------------------------------------------------------------


class NullGpio:
    """For a module whose M0/M1 are strapped low and whose AUX is not wired.

    Mode 0 is all the runtime path needs, so this is a legitimate way to run -
    it just means the module has to be configured elsewhere (the ESP32's CFG!
    command, or a USB-TTL adapter) because mode 3 is unreachable.
    """

    def set_mode(self, mode):
        return mode == MODE_NORMAL

    def wait_aux(self, timeout=1.0):
        return True

    def close(self):
        pass


class Gpio:
    """M0/M1 as outputs, AUX as input, via lgpio.

    lgpio and not RPi.GPIO: the Pi 5's RP1 is not supported by the RPi.GPIO on
    PyPI. Ubuntu packages this as python3-lgpio.
    """

    def __init__(self, m0, m1, aux, chip=None):
        import lgpio  # imported here so --selftest works on a machine with no GPIO

        self._lgpio = lgpio
        self.m0, self.m1, self.aux = m0, m1, aux

        candidates = [chip] if chip is not None else [4, 0]
        last_error = None
        for number in candidates:
            try:
                self._handle = lgpio.gpiochip_open(number)
                self.chip = number
                break
            except Exception as exc:            # lgpio raises bare Exception subclasses
                last_error = exc
        else:
            raise RuntimeError(
                f"could not open any of gpiochip{candidates}: {last_error}. "
                "Pass --gpiochip N; on a Pi 5 the 40-pin header is usually 4.")

        lgpio.gpio_claim_output(self._handle, self.m0, 0)
        lgpio.gpio_claim_output(self._handle, self.m1, 0)
        lgpio.gpio_claim_input(self._handle, self.aux)

    def set_mode(self, mode):
        """Datasheet 6.1: only switch while AUX is high, and let it settle."""
        settled = self.wait_aux()
        self._lgpio.gpio_write(self._handle, self.m0, mode & 0x01)
        self._lgpio.gpio_write(self._handle, self.m1, (mode >> 1) & 0x01)
        time.sleep(0.005)
        return self.wait_aux() and settled

    def wait_aux(self, timeout=1.0):
        """AUX low means busy: self-check, unsent TX, or RX draining to the UART."""
        deadline = time.monotonic() + timeout
        while self._lgpio.gpio_read(self._handle, self.aux) == 0:
            if time.monotonic() > deadline:
                return False
            time.sleep(0.001)
        time.sleep(0.003)
        return True

    def close(self):
        self._lgpio.gpiochip_close(self._handle)


# --- module ----------------------------------------------------------------


class E32:
    """The serial port and the mode pins as one thing, because they are."""

    def __init__(self, port, baud, gpio):
        import serial

        self.gpio = gpio
        self.ser = serial.Serial(port, baudrate=baud, timeout=0)
        self.baud = baud
        self.gpio.set_mode(MODE_NORMAL)

    def write_frame(self, frame):
        if not self.gpio.wait_aux():
            return False
        self.ser.write(frame)
        return True

    def read(self):
        waiting = self.ser.in_waiting
        return self.ser.read(waiting) if waiting else b""

    def transact(self, command, reply_len, timeout=1.0):
        """One sleep-mode command, then straight back to normal mode."""
        if not self.gpio.set_mode(MODE_SLEEP):
            raise RuntimeError(
                "module stayed busy entering config mode - check AUX, M0/M1 and the supply")
        try:
            self.ser.baudrate = CONFIG_BAUD
            self.ser.reset_input_buffer()
            self.ser.write(command)
            self.ser.flush()

            reply = bytearray()
            deadline = time.monotonic() + timeout
            while len(reply) < reply_len and time.monotonic() < deadline:
                reply += self.ser.read(reply_len - len(reply))
                time.sleep(0.005)
            return bytes(reply)
        finally:
            self.ser.baudrate = self.baud
            self.gpio.set_mode(MODE_NORMAL)

    def config_read(self):
        return self.transact(bytes((0xC1, 0xC1, 0xC1)), 6)

    def config_write(self, option):
        command = bytes((CFG_HEAD_SAVE, CFG_ADDH, CFG_ADDL, CFG_SPED, CFG_CHAN, option))
        # Some firmware revisions echo the saved parameters and some stay quiet,
        # so the echo is not the acceptance test - the read-back is.
        self.transact(command, 6)
        time.sleep(0.1)
        return command, self.config_read()

    def close(self):
        self.ser.close()
        self.gpio.close()


def hexdump(data):
    return " ".join(f"{b:02X}" for b in data)


# --- master ----------------------------------------------------------------


class Master(threading.Thread):
    """Polls the rover and holds the latest snapshot for the TCP server."""

    daemon = True

    def __init__(self, e32, rate_hz, reply_timeout, command_timeout):
        super().__init__()
        self.e32 = e32
        self.period = 1.0 / rate_hz
        self.reply_timeout = reply_timeout
        self.command_timeout = command_timeout

        self.parser = lora_frame.Parser()
        self.results = deque(maxlen=WINDOW)   # True/False per poll
        self.rtts = deque(maxlen=WINDOW)
        self.all_rtts = []                    # unbounded, for --ping summaries
        self.misses = 0
        self.polls = 0
        self.replies = 0
        self.seq = 0

        self._command = lora_frame.Teleop()
        self._command_ts = 0.0
        self._lock = threading.Lock()
        # A client can connect before the first poll finishes, so this has to be
        # a valid snapshot rather than a placeholder - `link` in particular is
        # what tells a consumer which of the mast's two streams it is reading.
        self.latest = {"ts": round(time.time(), 3), "link": "lora",
                       "state": "DOWN", "reasons": ["starting up"]}
        self.stop_after = None                # set by --ping

    # -- command injection ------------------------------------------------

    def set_command(self, vx, vy, wz, estop=False, mode=False):
        flags = (lora_frame.FLAG_ESTOP if estop else 0) | (lora_frame.FLAG_MODE if mode else 0)
        with self._lock:
            self._command = lora_frame.Teleop(vx, vy, wz, flags)
            self._command_ts = time.monotonic()

    def _current_command(self):
        with self._lock:
            command, ts = self._command, self._command_ts
        # A command nobody is refreshing is a command from a client that went
        # away. Stop repeating it rather than driving on a stale stick.
        if time.monotonic() - ts > self.command_timeout:
            return lora_frame.Teleop()
        return command

    # -- poll loop ---------------------------------------------------------

    def run(self):
        while self.stop_after is None or self.polls < self.stop_after:
            started = time.monotonic()
            self._poll_once()
            time.sleep(max(0.0, self.period - (time.monotonic() - started)))

    def _poll_once(self):
        command = self._current_command()
        self.seq = (self.seq + 1) & 0xFF
        frame = lora_frame.encode(lora_frame.TYPE_TELEOP, self.seq,
                                  lora_frame.pack_teleop(command))

        sent_at = time.monotonic()
        self.polls += 1
        if not self.e32.write_frame(frame):
            self._record(None, None, "module busy, frame not sent")
            return

        status, rtt = self._await_status(self.seq, sent_at)
        self._record(status, rtt, None)

    def _await_status(self, want_seq, sent_at):
        """Read until the rover's answer to this exact poll arrives, or time out."""
        deadline = sent_at + self.reply_timeout
        while time.monotonic() < deadline:
            chunk = self.e32.read()
            if not chunk:
                time.sleep(0.002)
                continue
            for frame_type, _seq, payload in self.parser.feed(chunk):
                if frame_type != lora_frame.TYPE_STATUS:
                    continue
                status = lora_frame.unpack_status(payload)
                if status.echo_seq != want_seq:
                    continue  # answer to an earlier poll; too late to count
                return status, (time.monotonic() - sent_at) * 1000.0
        return None, None

    def _record(self, status, rtt, note):
        delivered = status is not None
        self.results.append(delivered)
        if delivered:
            self.replies += 1
            self.misses = 0
            self.rtts.append(rtt)
            self.all_rtts.append(rtt)
        else:
            self.misses += 1

        loss = 100.0 * (1.0 - sum(self.results) / len(self.results))
        rtt_avg = (sum(self.rtts) / len(self.rtts)) if self.rtts else None
        state, reasons = self._classify(loss, rtt_avg)
        if note:
            reasons = reasons + [note]

        snap = {
            "ts": round(time.time(), 3),
            "link": "lora",
            "state": state,
            "reasons": reasons,
            "loss_pct_avg": round(loss, 1),
            "rtt_ms": round(rtt, 1) if rtt is not None else None,
            "rtt_ms_avg": round(rtt_avg, 1) if rtt_avg is not None else None,
            "polls": self.polls,
            "replies": self.replies,
            "consecutive_misses": self.misses,
            "rx_bad_frames": self.parser.bad,
            "seq": self.seq,
        }
        if status is not None:
            snap.update({
                "rover_rx_ok": status.rx_ok,
                "rover_rx_bad": status.rx_bad,
                "rover_failsafe": bool(status.flags & lora_frame.STATUS_FAILSAFE),
                "rover_estop": bool(status.flags & lora_frame.STATUS_ESTOP),
            })

        with self._lock:
            self.latest = snap

    def _classify(self, loss, rtt_avg):
        reasons = []
        # The consecutive-miss test exists because the rolling average is slow:
        # five dead polls in a row is a dead link even while the window still
        # remembers a healthy minute.
        if self.misses >= MISSES_DOWN:
            reasons.append(f"{self.misses} unanswered polls")
        if loss >= LOSS_DOWN_PCT:
            reasons.append(f"loss {loss:.0f}%")
        if reasons:
            return "DOWN", reasons

        if loss >= LOSS_DEGRADED_PCT:
            reasons.append(f"loss {loss:.0f}%")
        if rtt_avg is not None and rtt_avg >= RTT_DEGRADED_MS:
            reasons.append(f"rtt {rtt_avg:.0f} ms")
        if reasons:
            return "DEGRADED", reasons
        return "OK", []

    def snapshot(self):
        with self._lock:
            return dict(self.latest)


# --- TCP service -----------------------------------------------------------


class Handler(socketserver.StreamRequestHandler):
    """Stream JSON metrics out; accept JSON commands in, on the same socket."""

    def handle(self):
        master = self.server.master
        threading.Thread(target=self._read_commands, daemon=True).start()
        try:
            while True:
                self.wfile.write((json.dumps(master.snapshot()) + "\n").encode())
                self.wfile.flush()
                time.sleep(self.server.interval)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass  # client went away; nothing to clean up

    def _read_commands(self):
        master = self.server.master
        try:
            for line in self.rfile:
                line = line.strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    continue
                master.set_command(
                    msg.get("vx", 0), msg.get("vy", 0), msg.get("wz", 0),
                    bool(msg.get("estop", False)), bool(msg.get("mode", False)))
        except (ConnectionResetError, OSError, ValueError):
            pass


class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True
    address_family = socket.AF_INET


# --- bring-up modes --------------------------------------------------------


def run_selftest():
    """Codec vectors. No radio, no GPIO, no serial - run this anywhere."""
    frame = lora_frame.encode(lora_frame.TYPE_TELEOP, 0,
                              lora_frame.pack_teleop(lora_frame.Teleop(10, -20, 30, 0)))
    print(f"teleop seq=0 vx=10 vy=-20 wz=30 flags=0 -> {hexdump(frame)}")
    print("  the firmware's TEST command prints the same 10 bytes (with its own seq)")

    parser = lora_frame.Parser()
    got = parser.feed(frame)
    assert got == [(lora_frame.TYPE_TELEOP, 0, frame[4:8])], got

    corrupted = bytearray(frame)
    corrupted[-1] ^= 0xFF
    assert parser.feed(bytes(corrupted)) == []
    assert (parser.ok, parser.bad) == (1, 1), (parser.ok, parser.bad)

    # Garbage in front of a good frame must not cost us the frame.
    parser = lora_frame.Parser()
    assert len(parser.feed(b"\x00\xffrubbish\xaa\xaa" + frame)) == 1

    status = lora_frame.encode(lora_frame.TYPE_STATUS, 7,
                               lora_frame.pack_status(lora_frame.Status(6, 200, 1, 0)))
    print(f"status seq=7 echo=6 rx_ok=200 rx_bad=1 flags=0 -> {hexdump(status)}")
    print("selftest OK")


def run_config(e32, action):
    if action == "read":
        print(f"config: {hexdump(e32.config_read())}")
        return
    wrote, readback = e32.config_write(CFG_OPTION[action])
    print(f"wrote:  {hexdump(wrote)}")
    print(f"config: {hexdump(readback)}")
    if readback != wrote:
        print("MISMATCH - the module did not take the configuration", file=sys.stderr)


def run_chat(e32):
    """Raw transparent text, no framing. Bring-up step before the protocol."""
    def reader():
        while True:
            chunk = e32.read()
            if chunk:
                sys.stdout.write(f"[MAST <- recv] {chunk.decode('utf-8', 'replace')}\n")
                sys.stdout.flush()
            time.sleep(0.02)

    threading.Thread(target=reader, daemon=True).start()
    print("[MAST] transparent chat ready. Type a message and press Enter.")
    for line in sys.stdin:
        line = line.strip()
        if line:
            e32.write_frame(line.encode() + b"\n")


def run_ping(master, count):
    master.stop_after = count
    master.start()
    while master.is_alive():
        time.sleep(0.2)

    delivered = master.replies
    loss = 100.0 * (1.0 - delivered / master.polls) if master.polls else 100.0
    print(f"\n{master.polls} polls, {delivered} answered, {loss:.1f}% loss")
    if master.all_rtts:
        print(f"rtt ms: min {min(master.all_rtts):.0f}  "
              f"p50 {percentile(master.all_rtts, 50):.0f}  "
              f"p95 {percentile(master.all_rtts, 95):.0f}  "
              f"max {max(master.all_rtts):.0f}")
    print(f"frames failing CRC: {master.parser.bad}")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", default="/dev/ttyAMA0",
                    help="Ubuntu does not create the /dev/serial0 alias Raspberry Pi OS does")
    ap.add_argument("--baud", type=int, default=9600,
                    help="must match the UART baud encoded in the module's SPED byte")
    ap.add_argument("--m0", type=int, default=23, help="BCM pin (physical 16)")
    ap.add_argument("--m1", type=int, default=24, help="BCM pin (physical 18)")
    ap.add_argument("--aux", type=int, default=17, help="BCM pin (physical 11)")
    ap.add_argument("--gpiochip", type=int, default=None,
                    help="default: try 4 then 0")
    ap.add_argument("--no-gpio", action="store_true",
                    help="M0/M1 strapped low in hardware; disables config mode")
    ap.add_argument("--bind", default="10.44.0.1",
                    help="bind address; the mast link only, not 0.0.0.0")
    ap.add_argument("--tcp-port", type=int, default=4001)
    ap.add_argument("--rate", type=float, default=5.0, help="polls per second")
    ap.add_argument("--reply-timeout", type=float, default=0.15)
    ap.add_argument("--command-timeout", type=float, default=0.5,
                    help="zero the command if no client refreshes it")
    ap.add_argument("--ping", type=int, metavar="N",
                    help="send N polls, print loss and RTT, exit")
    ap.add_argument("--chat", action="store_true", help="raw transparent text mode")
    ap.add_argument("--config", choices=["read", "write", "low"],
                    help="read the module's registers, or write them at 30 dBm "
                         "('write') or 21 dBm ('low'), then exit")
    ap.add_argument("--selftest", action="store_true",
                    help="check the frame codec; needs no hardware")
    args = ap.parse_args()

    if args.selftest:
        run_selftest()
        return

    gpio = NullGpio() if args.no_gpio else Gpio(args.m0, args.m1, args.aux, args.gpiochip)
    e32 = E32(args.port, args.baud, gpio)
    try:
        if args.config:
            run_config(e32, args.config)
            return
        if args.chat:
            run_chat(e32)
            return

        master = Master(e32, args.rate, args.reply_timeout, args.command_timeout)
        if args.ping:
            run_ping(master, args.ping)
            return

        master.start()
        server = Server((args.bind, args.tcp_port), Handler)
        server.master = master
        server.interval = 1.0 / args.rate
        print(f"lora_bridge: polling {args.port} at {args.rate} Hz, "
              f"serving {args.bind}:{args.tcp_port}")
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        e32.close()


if __name__ == "__main__":
    main()

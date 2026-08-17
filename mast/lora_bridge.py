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
the rover independently zeroes its own output after 1 s of silence.

Usage:
    lora_bridge.py                       # serve on tcp/4001
    lora_bridge.py --ping 200            # bring-up: measure loss and RTT
    lora_bridge.py --rate-sweep          # find the usable poll rate
    lora_bridge.py --throughput 30       # iperf-style saturation test
    lora_bridge.py --chat                # bring-up: raw transparent text
    lora_bridge.py --config read         # print the module's registers
    lora_bridge.py --config write        # write them (30 dBm), then read back
    lora_bridge.py --selftest            # codec vectors, no hardware needed
"""

import argparse
import glob
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
# How long AUX must read high before a mode change is trusted. Measured
# boundary is ~20 ms; see Gpio.set_mode.
MODE_SETTLE_S = 0.030


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


# The chip that owns the 40-pin header, by label. The device number is not
# stable - it is 4 on a Pi 5 and 0 on earlier boards - and on a Pi 5 the other
# four chips are internal brcmstb controllers that must never be driven.
HEADER_CHIP_LABELS = (
    "pinctrl-rp1",        # Pi 5
    "pinctrl-bcm2711",    # Pi 4
    "pinctrl-bcm2835",    # Pi 3 and earlier
)


class Gpio:
    """M0/M1 as outputs, AUX as input, via lgpio.

    lgpio and not RPi.GPIO: the Pi 5's RP1 is not supported by the RPi.GPIO on
    PyPI. Ubuntu packages it as python3-lgpio, and it also arrives as a
    dependency of rpi-lgpio, which is how the mast Pi already has it.
    """

    def __init__(self, m0, m1, aux, chip=None):
        import lgpio  # imported here so --selftest works on a machine with no GPIO

        self._lgpio = lgpio
        self.m0, self.m1, self.aux = m0, m1, aux
        self.chip, self._handle = self._open_chip(chip)

        lgpio.gpio_claim_output(self._handle, self.m0, 0)
        lgpio.gpio_claim_output(self._handle, self.m1, 0)
        # Pulled up, like the firmware does, so a disconnected or unpowered
        # module reads "not busy" instead of floating. The E32 drives AUX
        # push-pull, so the pull-up costs nothing when one is attached.
        lgpio.gpio_claim_input(self._handle, self.aux, lgpio.SET_PULL_UP)

    def _open_chip(self, chip):
        """Find the header's gpiochip by label, or take the one we were given."""
        lgpio = self._lgpio
        if chip is not None:
            return chip, lgpio.gpiochip_open(chip)

        tried = []
        for path in sorted(glob.glob("/dev/gpiochip*")):
            suffix = path[len("/dev/gpiochip"):]
            if not suffix.isdigit():
                continue
            number = int(suffix)
            try:
                handle = lgpio.gpiochip_open(number)
            except Exception as exc:        # lgpio raises bare Exception subclasses
                tried.append(f"{number}: {exc}")
                continue
            label = lgpio.gpio_get_chip_info(handle)[3]
            if label in HEADER_CHIP_LABELS:
                return number, handle
            lgpio.gpiochip_close(handle)
            tried.append(f"{number}: {label!r} is not a header controller")

        raise RuntimeError(
            "no 40-pin header gpiochip found (" + "; ".join(tried) + "). "
            "Pass --gpiochip N to override.")

    def set_mode(self, mode):
        """Change mode and wait until the module will actually answer.

        The datasheet only promises AUX high plus 2 ms, which is not enough.
        Measured on the mast Pi: AUX drops within 5 ms of M0/M1 changing, but
        C1C1C1 is ignored until roughly 20 ms have passed - 0/5 accepted at
        5 ms, 4/5 at 10 ms, 5/5 from 20 ms. Worse, a plain "is AUX high?" check
        run straight after the write samples the level from *before* the module
        reacted and returns true while it is still switching. So require AUX to
        read high continuously instead of merely once.
        """
        settled = self.wait_aux()
        self._lgpio.gpio_write(self._handle, self.m0, mode & 0x01)
        self._lgpio.gpio_write(self._handle, self.m1, (mode >> 1) & 0x01)
        return self.wait_aux_stable() and settled

    def wait_aux(self, timeout=1.0):
        """AUX low means busy: self-check, unsent TX, or RX draining to the UART."""
        deadline = time.monotonic() + timeout
        while self._lgpio.gpio_read(self._handle, self.aux) == 0:
            if time.monotonic() > deadline:
                return False
            time.sleep(0.001)
        time.sleep(0.003)
        return True

    def wait_aux_stable(self, timeout=1.0, stable=MODE_SETTLE_S):
        """Wait until AUX has read high continuously for `stable` seconds."""
        deadline = time.monotonic() + timeout
        high_since = None
        while time.monotonic() < deadline:
            if self._lgpio.gpio_read(self._handle, self.aux):
                if high_since is None:
                    high_since = time.monotonic()
                elif time.monotonic() - high_since >= stable:
                    return True
            else:
                high_since = None
            time.sleep(0.001)
        return False

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
        # Not _stop: threading.Thread defines _stop() as a method on Python 3.12
        # and calls it internally when the thread exits. Shadowing it with an
        # Event makes every shutdown die with "'Event' object is not callable",
        # in a traceback that names none of our own code.
        self._stopping = threading.Event()
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

    def stop(self):
        """Ask the poll loop to finish its current cycle and exit."""
        self._stopping.set()

    def run(self):
        while not self._stopping.is_set() and (
                self.stop_after is None or self.polls < self.stop_after):
            started = time.monotonic()
            self.poll_once()
            self._stopping.wait(max(0.0, self.period - (time.monotonic() - started)))

    def poll_once(self):
        """One poll and its answer. Returns the rover's Status, or None."""
        command = self._current_command()
        self.seq = (self.seq + 1) & 0xFF
        frame = lora_frame.encode(lora_frame.TYPE_TELEOP, self.seq,
                                  lora_frame.pack_teleop(command))

        sent_at = time.monotonic()
        self.polls += 1
        if not self.e32.write_frame(frame):
            self._record(None, None, "module busy, frame not sent")
            return None

        status, rtt = self._await_status(self.seq, sent_at)
        self._record(status, rtt, None)
        return status

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


def expected_config(option="write"):
    return bytes((CFG_HEAD_SAVE, CFG_ADDH, CFG_ADDL, CFG_SPED, CFG_CHAN,
                  CFG_OPTION[option]))


def check_config(e32):
    """Read the module's registers at startup and complain if they are wrong.

    A module that has quietly reverted to factory settings - which is what a
    power glitch can do - produces 100% loss with zero CRC failures, because
    the two ends are on different air data rates and neither can demodulate
    the other. That is indistinguishable from being out of range, and it has
    already cost this project an hour of chasing wiring that was fine.

    Warn rather than refuse to start: a wrong air rate is recoverable and the
    metrics stream is more useful up than down. The config is not rewritten
    automatically either - that would put a flash write in the boot path to
    paper over a fault worth seeing.
    """
    want = expected_config()
    try:
        have = e32.config_read()
    except RuntimeError as exc:
        print(f"WARNING: could not read the module's config ({exc}). "
              f"Radio settings are unverified.", file=sys.stderr)
        return False

    if not have:
        print("WARNING: module did not answer a config read. It may be "
              "unpowered, or its RXD line is not connected.", file=sys.stderr)
        return False
    if have != want:
        print(f"WARNING: module config is {hexdump(have)}, expected "
              f"{hexdump(want)}.", file=sys.stderr)
        if have[3] != want[3]:
            print("         The SPED byte differs - the air data rate does not "
                  "match the rover. Expect 100% loss with no CRC errors.",
                  file=sys.stderr)
        print("         Fix with: lora_bridge.py --config write", file=sys.stderr)
        return False

    print(f"module config OK: {hexdump(have)}")
    return True


def run_config(e32, action):
    if action == "read":
        readback = e32.config_read()
        if not readback:
            print("no reply - check the module's wiring, supply and M0/M1 pins",
                  file=sys.stderr)
            return
        print(f"config: {hexdump(readback)}")
        return

    wrote, readback = e32.config_write(CFG_OPTION[action])
    print(f"wrote:  {hexdump(wrote)}")
    if not readback:
        print("no reply to the read-back - the write is unconfirmed", file=sys.stderr)
        return
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


def run_rate_sweep(e32, rates, polls, command_timeout):
    """Find the fastest poll rate the link actually sustains.

    The reply has to be back before the next poll goes out, so the usable rate
    is bounded by the round trip and nothing else. Sweeping is how you find that
    bound rather than guessing it: this project started at 5 Hz on paper and
    scored 0% delivered, because 200 ms is shorter than the 240 ms round trip.

    Read the table as a property of the pair, not of the radio alone. The reply
    window below is 90% of the period, so a rate fails as soon as
    0.9/rate < round-trip, which is slightly before 1/rate < round-trip. With a
    240 ms round trip that puts the cliff just under 3.75 Hz - which is why 3 Hz
    passes and 4 Hz does not, even though a 250 ms period is nominally longer
    than the round trip.
    """
    print(f"{'rate':>7} {'period':>8} {'sent':>6} {'answered':>9} "
          f"{'loss':>7} {'p50':>8} {'p95':>8}")
    for rate in rates:
        period = 1.0 / rate
        # The reply window must stay inside the period, or every poll lands late
        # and the loop measures its own overrun instead of the link.
        master = Master(e32, rate, min(0.9 * period, 0.5), command_timeout)
        master.stop_after = polls
        master.start()
        master.join()

        loss = 100.0 * (1.0 - master.replies / master.polls) if master.polls else 100.0
        p50 = percentile(master.all_rtts, 50)
        p95 = percentile(master.all_rtts, 95)
        print(f"{rate:>6.1f}H {period * 1000:>7.0f}ms {master.polls:>6} "
              f"{master.replies:>9} {loss:>6.1f}% "
              f"{(f'{p50:.0f} ms' if p50 else '-'):>8} "
              f"{(f'{p95:.0f} ms' if p95 else '-'):>8}")


def run_throughput(e32, duration, burst, reply_timeout, command_timeout):
    """iperf-style saturation test: flood one way and count what arrived.

    --ping measures a polled round trip. This measures how much the radio will
    actually carry. Frames go out back to back and write_frame blocks on AUX, so
    the module paces us to whatever it will accept over UART; the rover's own
    rx_ok counter is the ground truth for what then made it over the air. The
    gap between the two is the module's 512-byte buffer overflowing, which
    nothing on the UART side can see.

    The flood uses STATUS frames rather than TELEOP on purpose. The rover counts
    every CRC-valid frame in rx_ok but only answers TELEOP ones, so this fills
    the air in one direction only. Flooding with TELEOP would have the rover
    transmitting back into our transmission - exactly the collisions the
    master/slave design exists to prevent - and would measure contention rather
    than capacity.

    rx_ok is one byte, so a burst plus its closing poll has to stay under 256
    for the difference to be unambiguous. Longer runs are repeated bursts.
    """
    if burst > 254:
        raise SystemExit("--burst must be 254 or less: the rover reports rx_ok "
                         "as a single byte and the delta would wrap")

    master = Master(e32, 1.0, reply_timeout, command_timeout)
    flood = lora_frame.pack_status(lora_frame.Status(0, 0, 0, 0))

    sent_total = delivered_total = rounds = 0
    flood_seconds = 0.0
    deadline = time.monotonic() + duration
    print(f"flooding for {duration:.0f}s in bursts of {burst} frames "
          f"({lora_frame.FRAME_LEN} bytes each)")

    while time.monotonic() < deadline:
        before = master.poll_once()
        if before is None:
            print("  no answer to the baseline poll - is the rover powered?")
            break

        seq = master.seq
        started = time.monotonic()
        sent = 0
        for _ in range(burst):
            seq = (seq + 1) & 0xFF
            if not e32.write_frame(
                    lora_frame.encode(lora_frame.TYPE_STATUS, seq, flood)):
                break
            sent += 1
        elapsed = time.monotonic() - started

        # Let the air and both UARTs drain before asking what arrived.
        time.sleep(2.0)
        master.seq = seq
        after = master.poll_once()
        if after is None:
            print("  no answer after the burst - link lost?")
            break

        # The closing poll's own frame is counted by the rover too.
        delivered = (after.rx_ok - before.rx_ok - 1) & 0xFF
        rounds += 1
        sent_total += sent
        delivered_total += delivered
        flood_seconds += elapsed
        pct = (100.0 * delivered / sent) if sent else 0.0
        print(f"  burst {rounds}: sent {sent} in {elapsed:.2f}s, "
              f"delivered {delivered} ({pct:.0f}%)")

    if not flood_seconds:
        return
    width = lora_frame.FRAME_LEN
    print()
    print(f"into the module : {sent_total} frames / {flood_seconds:.2f}s = "
          f"{sent_total / flood_seconds:5.1f} frame/s, "
          f"{sent_total * width / flood_seconds:5.0f} B/s")
    print(f"over the air    : {delivered_total} frames / {flood_seconds:.2f}s = "
          f"{delivered_total / flood_seconds:5.1f} frame/s, "
          f"{delivered_total * width / flood_seconds:5.0f} B/s")
    if sent_total:
        drop = 100.0 * (1.0 - delivered_total / sent_total)
        print(f"buffer overflow : {drop:.0f}% - accepted over UART, never transmitted")


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
                    help="default: find the chip labelled pinctrl-rp1 / pinctrl-bcm*")
    ap.add_argument("--no-gpio", action="store_true",
                    help="M0/M1 strapped low in hardware; disables config mode")
    ap.add_argument("--bind", default="10.44.0.1",
                    help="bind address; the mast link only, not 0.0.0.0")
    ap.add_argument("--tcp-port", type=int, default=4001)
    ap.add_argument("--rate", type=float, default=3.0,
                    help="polls per second; the measured round trip is ~240 ms, "
                         "so a period below that can never be answered in time")
    ap.add_argument("--reply-timeout", type=float, default=0.30)
    ap.add_argument("--command-timeout", type=float, default=0.5,
                    help="zero the command if no client refreshes it")
    ap.add_argument("--ping", type=int, metavar="N",
                    help="send N polls, print loss and RTT, exit")
    ap.add_argument("--rate-sweep", nargs="?", const="1,2,3,4,5,6", metavar="HZ",
                    help="sweep poll rates (comma-separated, default 1,2,3,4,5,6) "
                         "and report loss at each; finds the usable command rate")
    ap.add_argument("--sweep-polls", type=int, default=30,
                    help="polls per rate in --rate-sweep")
    ap.add_argument("--throughput", type=float, nargs="?", const=30.0, metavar="SEC",
                    help="iperf-style: flood one way for SEC seconds (default 30) "
                         "and report what the rover actually received")
    ap.add_argument("--burst", type=int, default=200,
                    help="frames per burst in --throughput; 254 max (rx_ok is a byte)")
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
    master = None
    try:
        if args.config:
            run_config(e32, args.config)
            return
        if args.chat:
            run_chat(e32)
            return

        # Everything past here moves traffic, so verify the radio is set up the
        # way both ends assume before any of it is trusted.
        if not args.no_gpio:
            check_config(e32)

        if args.rate_sweep:
            rates = [float(r) for r in args.rate_sweep.split(",") if r.strip()]
            run_rate_sweep(e32, rates, args.sweep_polls, args.command_timeout)
            return
        if args.throughput:
            run_throughput(e32, args.throughput, args.burst,
                           args.reply_timeout, args.command_timeout)
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
        # Stop polling before closing the port. Otherwise the daemon thread
        # keeps writing to a closed serial handle and dumps a traceback that
        # interleaves with - and hides - whatever actually went wrong.
        if master is not None:
            master.stop()
            master.join(timeout=2.0)
        e32.close()


if __name__ == "__main__":
    main()

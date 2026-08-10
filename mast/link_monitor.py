#!/usr/bin/env python3
"""Primary Wi-Fi link quality monitor for the ground-station mast Pi.

Samples the Alfa's radio state and its reachability to the rover, and serves the
result as line-delimited JSON over TCP. The failover decision itself is NOT made
here - this only reports. `link_failover_node` on the ground-station PC consumes
this stream and decides when to switch the command path to LoRa.

That split is deliberate and matches how the E32 is handled (ser2net on 4001):
the mast Pi runs no ROS at all. It is a dumb appliance exporting two TCP
services, and everything that needs a ROS graph runs in the Humble container on
the GS PC where it can be debugged on a desk rather than up a mast.

    tcp/4001  <- ser2net, raw E32 serial
    tcp/4002  <- this, JSON link metrics

Stdlib only: nothing to install on the Pi, nothing to break on a kernel upgrade.

Usage:
    link_monitor.py [--iface IFACE] [--peer IP] [--port N] [--interval SEC]
"""

import argparse
import json
import re
import socket
import socketserver
import subprocess
import threading
import time
from collections import deque

# --- link state thresholds -------------------------------------------------
#
# These drive the reported state only; the consumer applies its own hysteresis
# before acting. Values are starting points measured on 2.4 GHz at close range
# and MUST be re-measured at distance on the competition band before they mean
# anything - see the note in mast/README.md.
SIGNAL_DEGRADED_DBM = -75.0   # 802.11 gets unreliable well before the link drops
SIGNAL_DOWN_DBM = -85.0
LOSS_DEGRADED_PCT = 20.0
LOSS_DOWN_PCT = 80.0
RTT_DEGRADED_MS = 150.0

PING_COUNT = 3
PING_INTERVAL = 0.2
PING_DEADLINE = 2
WINDOW = 10                   # samples retained for rolling loss/RTT
STALL_SAMPLES = 3             # consecutive tx-moving/rx-frozen samples = stalled


def _run(cmd, timeout=5):
    """Run a command, returning stdout or '' - never raise into the sample loop."""
    try:
        return subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout
        ).stdout
    except (subprocess.SubprocessError, OSError):
        return ""


def read_radio(iface):
    """Parse `iw dev <iface> link` into a flat dict.

    Deliberately does NOT use `iw station dump` / `survey dump`: the out-of-tree
    rtl8812au driver implements neither in client mode (both return empty even
    as root), so tx-retry and channel-busy counters are simply unavailable on
    this hardware. Packet counters from sysfs are used instead - see
    read_counters().
    """
    out = {
        "associated": False, "ssid": None, "bssid": None, "freq_mhz": None,
        "signal_dbm": None, "tx_bitrate_mbps": None,
    }

    link = _run(["iw", "dev", iface, "link"])
    if not link or "Not connected" in link:
        return out
    out["associated"] = True

    m = re.search(r"Connected to ([0-9a-f:]{17})", link)
    if m:
        out["bssid"] = m.group(1)
    m = re.search(r"SSID:\s*(.+)", link)
    if m:
        out["ssid"] = m.group(1).strip()
    m = re.search(r"freq:\s*([\d.]+)", link)
    if m:
        out["freq_mhz"] = float(m.group(1))
    m = re.search(r"signal:\s*(-?\d+)", link)
    if m:
        out["signal_dbm"] = float(m.group(1))
    m = re.search(r"tx bitrate:\s*([\d.]+)", link)
    if m:
        out["tx_bitrate_mbps"] = float(m.group(1))
    return out


COUNTERS = ("rx_packets", "tx_packets", "rx_errors", "tx_errors",
            "rx_dropped", "tx_dropped")


def read_counters(iface):
    """Read kernel interface counters from sysfs. Always available."""
    out = {}
    for name in COUNTERS:
        try:
            with open(f"/sys/class/net/{iface}/statistics/{name}") as fh:
                out[name] = int(fh.read().strip())
        except (OSError, ValueError):
            out[name] = None
    return out


def read_reachability(peer):
    """Ping the peer; return (loss_pct, rtt_avg_ms). Loss 100.0 if unreachable."""
    out = _run(
        ["ping", "-c", str(PING_COUNT), "-i", str(PING_INTERVAL),
         "-W", "1", "-w", str(PING_DEADLINE), peer],
        timeout=PING_DEADLINE + 3,
    )
    loss = 100.0
    rtt = None
    m = re.search(r"(\d+(?:\.\d+)?)% packet loss", out)
    if m:
        loss = float(m.group(1))
    m = re.search(r"rtt [^=]*= [\d.]+/([\d.]+)/", out)
    if m:
        rtt = float(m.group(1))
    return loss, rtt


def classify(radio, loss, rtt, stalled):
    """Reduce the sample to OK / DEGRADED / DOWN plus the reasons why.

    Reasons are returned so an operator can see *why* the console dropped to the
    fallback link, rather than just that it did.
    """
    reasons = []
    if not radio["associated"]:
        return "DOWN", ["not associated"]
    # A stalled interface looks perfectly healthy by every other measure. This
    # is exactly how the mast Ethernet failed on 2026-08-06: carrier up, clean
    # autonegotiation, zero CRC errors, and no traffic whatsoever. Anything that
    # only checks RSSI and association would have reported that link as fine.
    if stalled:
        reasons.append("rx stalled (tx moving, rx frozen)")
    if loss >= LOSS_DOWN_PCT:
        reasons.append(f"loss {loss:.0f}%")
    if radio["signal_dbm"] is not None and radio["signal_dbm"] <= SIGNAL_DOWN_DBM:
        reasons.append(f"signal {radio['signal_dbm']:.0f} dBm")
    if reasons:
        return "DOWN", reasons

    if loss >= LOSS_DEGRADED_PCT:
        reasons.append(f"loss {loss:.0f}%")
    if radio["signal_dbm"] is not None and radio["signal_dbm"] <= SIGNAL_DEGRADED_DBM:
        reasons.append(f"signal {radio['signal_dbm']:.0f} dBm")
    if rtt is not None and rtt >= RTT_DEGRADED_MS:
        reasons.append(f"rtt {rtt:.0f} ms")
    if reasons:
        return "DEGRADED", reasons
    return "OK", []


class Sampler(threading.Thread):
    """Owns the sampling loop; holds the latest snapshot for the TCP server."""

    daemon = True

    def __init__(self, iface, peer, interval):
        super().__init__()
        self.iface, self.peer, self.interval = iface, peer, interval
        self.losses, self.rtts = deque(maxlen=WINDOW), deque(maxlen=WINDOW)
        self.prev_counters = None
        self.rx_frozen_for = 0
        self.latest = {"state": "DOWN", "reasons": ["starting up"]}
        self.lock = threading.Lock()

    def run(self):
        while True:
            started = time.time()
            radio = read_radio(self.iface)
            counters = read_counters(self.iface)
            loss, rtt = (100.0, None)
            if radio["associated"]:
                loss, rtt = read_reachability(self.peer)

            self.losses.append(loss)
            if rtt is not None:
                self.rtts.append(rtt)
            loss_avg = sum(self.losses) / len(self.losses)
            rtt_avg = (sum(self.rtts) / len(self.rtts)) if self.rtts else None

            # Counters are cumulative; the rate of change is what matters.
            deltas = {}
            if self.prev_counters:
                for k, v in counters.items():
                    prev = self.prev_counters.get(k)
                    deltas[k] = (max(0, v - prev)
                                 if v is not None and prev is not None else None)
            self.prev_counters = counters

            # We ping every cycle, so a healthy associated link always has rx
            # traffic. tx moving while rx stays frozen means the path is one-way.
            if deltas.get("tx_packets") and not deltas.get("rx_packets"):
                self.rx_frozen_for += 1
            else:
                self.rx_frozen_for = 0
            stalled = self.rx_frozen_for >= STALL_SAMPLES

            state, reasons = classify(radio, loss_avg, rtt_avg, stalled)
            snap = {
                "ts": round(time.time(), 3),
                "iface": self.iface,
                "peer": self.peer,
                "state": state,
                "reasons": reasons,
                "loss_pct_instant": loss,
                "loss_pct_avg": round(loss_avg, 1),
                "rtt_ms": rtt,
                "rtt_ms_avg": round(rtt_avg, 1) if rtt_avg is not None else None,
                "stalled": stalled,
                "deltas": deltas,
            }
            snap.update(radio)
            with self.lock:
                self.latest = snap

            time.sleep(max(0.0, self.interval - (time.time() - started)))

    def snapshot(self):
        with self.lock:
            return dict(self.latest)


class Handler(socketserver.StreamRequestHandler):
    """Stream one JSON object per line for as long as the client stays connected."""

    def handle(self):
        sampler = self.server.sampler
        interval = self.server.interval
        try:
            while True:
                self.wfile.write(
                    (json.dumps(sampler.snapshot()) + "\n").encode())
                self.wfile.flush()
                time.sleep(interval)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass  # client went away; nothing to clean up


class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True
    address_family = socket.AF_INET


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--iface", default="wlx00c0caba8237",
                    help="Alfa interface (systemd names it from the MAC)")
    ap.add_argument("--peer", default="10.42.0.1",
                    help="rover address to measure reachability against")
    ap.add_argument("--bind", default="10.44.0.1",
                    help="bind address; the mast link only, not 0.0.0.0")
    ap.add_argument("--port", type=int, default=4002)
    ap.add_argument("--interval", type=float, default=1.0)
    args = ap.parse_args()

    sampler = Sampler(args.iface, args.peer, args.interval)
    sampler.start()

    server = Server((args.bind, args.port), Handler)
    server.sampler = sampler
    server.interval = args.interval
    server.serve_forever()


if __name__ == "__main__":
    main()

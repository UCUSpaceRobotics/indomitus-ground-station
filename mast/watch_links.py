#!/usr/bin/env python3
"""Watch both mast links on one line. Run on the GS PC during a range test.

Connects to link_monitor.py (tcp/4002, Wi-Fi) and lora_bridge.py (tcp/4001,
LoRa) at once and prints a combined status line, so the person walking away
from the mast has a single thing to read rather than two terminals to compare.

Neither connection is required: whichever service is down simply reads as
"--", which is itself the answer you want when you are trying to work out
which link died first.

Stdlib only, like everything else here.

Usage:
    watch_links.py [--host 10.44.0.1] [--interval 1.0] [--csv run1.csv]
"""

import argparse
import json
import socket
import threading
import time

WIFI_PORT = 4002
LORA_PORT = 4001


class Feed(threading.Thread):
    """Holds the newest JSON object from one of the mast's TCP services."""

    daemon = True

    def __init__(self, host, port, label):
        super().__init__()
        self.host, self.port, self.label = host, port, label
        self.latest = None
        self._lock = threading.Lock()

    def run(self):
        backoff = 1.0
        while True:
            try:
                with socket.create_connection((self.host, self.port), timeout=5) as sock:
                    sock.settimeout(10)
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
                            self.latest = sample
            except OSError:
                pass
            with self._lock:
                self.latest = None
            time.sleep(backoff)
            backoff = min(backoff * 2, 10.0)

    def snapshot(self):
        with self._lock:
            return dict(self.latest) if self.latest else None


def _num(value, fmt, width):
    return format(value, fmt).rjust(width) if isinstance(value, (int, float)) \
        else "--".rjust(width)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default="10.44.0.1")
    ap.add_argument("--interval", type=float, default=1.0)
    ap.add_argument("--csv", help="append every sample to this file for later plotting")
    args = ap.parse_args()

    wifi = Feed(args.host, WIFI_PORT, "wifi")
    lora = Feed(args.host, LORA_PORT, "lora")
    wifi.start()
    lora.start()

    csv = None
    if args.csv:
        csv = open(args.csv, "a", buffering=1)
        if csv.tell() == 0:
            csv.write("ts,wifi_state,signal_dbm,wifi_loss,wifi_rtt,tx_mbps,"
                      "lora_state,lora_loss,lora_rtt,rover_failsafe\n")

    print(f"watching {args.host}: wifi tcp/{WIFI_PORT}, lora tcp/{LORA_PORT}  "
          f"(ctrl-c to stop)")
    try:
        while True:
            w = wifi.snapshot() or {}
            r = lora.snapshot() or {}
            stamp = time.strftime("%H:%M:%S")

            line = (
                f"{stamp}  "
                f"WIFI {str(w.get('state', '--')):8s} "
                f"sig {_num(w.get('signal_dbm'), '.0f', 4)} dBm  "
                f"loss {_num(w.get('loss_pct_avg'), '.0f', 3)}%  "
                f"rtt {_num(w.get('rtt_ms_avg'), '.0f', 4)} ms  "
                f"{_num(w.get('tx_bitrate_mbps'), '.0f', 4)} Mb  "
                f"| LORA {str(r.get('state', '--')):8s} "
                f"loss {_num(r.get('loss_pct_avg'), '.0f', 3)}%  "
                f"rtt {_num(r.get('rtt_ms_avg'), '.0f', 4)} ms  "
                # Never print "ok" for a rover we have heard nothing from - an
                # absent reading and a healthy one must not look the same.
                f"rover {'--' if r.get('rover_failsafe') is None else ('FAILSAFE' if r['rover_failsafe'] else 'ok')}"
            )
            print(line)

            if csv:
                csv.write(",".join(str(v) for v in (
                    round(time.time(), 1),
                    w.get("state"), w.get("signal_dbm"), w.get("loss_pct_avg"),
                    w.get("rtt_ms_avg"), w.get("tx_bitrate_mbps"),
                    r.get("state"), r.get("loss_pct_avg"), r.get("rtt_ms_avg"),
                    r.get("rover_failsafe"))) + "\n")

            time.sleep(args.interval)
    except KeyboardInterrupt:
        pass
    finally:
        if csv:
            csv.close()


if __name__ == "__main__":
    main()

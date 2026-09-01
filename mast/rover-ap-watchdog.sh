#!/bin/sh
# Installed to /usr/local/sbin/rover-ap-watchdog. Falls back to the NM Hotspot
# unless the AP is both running AND addressed. Deliberately does NOT test for
# associated stations: nobody being connected is legitimate, and dropping a
# working 40 MHz AP to 20 MHz for that would be a false positive.
IFACE=wlx00c0caba86c1
if ! pgrep -x hostapd >/dev/null; then
    logger -t rover-ap-watchdog "hostapd not running — falling back"
    systemctl start rover-ap-fallback.service
    exit 0
fi
if ! ip -4 addr show "$IFACE" | grep -q 'inet 10\.42\.0\.1/'; then
    logger -t rover-ap-watchdog "AP has no address — falling back"
    systemctl start rover-ap-fallback.service
    exit 0
fi
logger -t rover-ap-watchdog "AP healthy at $(iw dev $IFACE info | grep -o 'width: [0-9]* MHz')"

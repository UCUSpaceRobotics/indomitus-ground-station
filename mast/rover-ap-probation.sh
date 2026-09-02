#!/bin/sh
# Installed to /usr/local/sbin/rover-ap-probation.
[ -e /etc/rover-ap-confirmed ] && {
    logger -t rover-ap-probation "confirmed — persistence stands"
    systemctl disable rover-ap-probation.timer >/dev/null 2>&1
    exit 0
}
logger -t rover-ap-probation "NOT confirmed within 5 min of boot — un-persisting"
systemctl disable --now rover-ap.service rover-ap-dhcp.service rover-ap-watchdog.timer >/dev/null 2>&1
systemctl start rover-ap-fallback.service
systemctl disable rover-ap-probation.timer >/dev/null 2>&1

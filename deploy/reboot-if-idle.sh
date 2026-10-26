#!/bin/sh
# Reboot after updates that need it, but only while the app is asleep (or not running).
# Run by gymtracker-reboot.timer every 15 minutes from 04:00 to 05:45; installed as
# /usr/local/sbin/gymtracker-reboot-if-idle.
[ -f /run/reboot-required ] || exit 0
state=$(curl -sf -m 5 http://127.0.0.1:8080/state \
  | python3 -c 'import json, sys; print(json.load(sys.stdin)["state"])' 2>/dev/null)
case "$state" in
  ""|idle) ;;
  *) logger -t gymtracker "reboot for updates postponed: app is $state"; exit 0 ;;
esac
logger -t gymtracker "rebooting for updates (app ${state:-not running})"
systemctl reboot

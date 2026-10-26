#!/bin/sh
# Restart the app service after an update, waiting while a workout is in progress
# (checks every 30 s). tools/deploy.sh starts this in the background.
while :; do
  state=$(curl -sf -m 5 http://127.0.0.1:8080/state \
    | python3 -c 'import json, sys; print(json.load(sys.stdin)["state"])' 2>/dev/null)
  case "$state" in
    recording|paused|finished) sleep 30 ;;
    *) break ;;
  esac
done
systemctl --user restart gymtracker
logger -t gymtracker "restarted after an update (was ${state:-not running})"

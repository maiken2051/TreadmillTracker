#!/bin/sh
# Update the Pi from this checkout: copy HEAD over SSH, install it into ~/venv, and
# restart the app service once no workout is running (a workout is never interrupted).
#   sh tools/deploy.sh [ssh-host]        (default host: gymtracker)
# Uncommitted changes are not deployed. First-time setup is deploy/install.sh on the Pi.
set -eu
HOST=${1:-gymtracker}
cd "$(dirname "$0")/.."
git diff --quiet HEAD || echo "note: uncommitted changes are not deployed"
echo "deploying $(git log --oneline -1) to $HOST"
git archive HEAD | ssh "$HOST" '
  set -e
  rm -rf ~/gymtracker/app.new && mkdir -p ~/gymtracker/app.new
  tar -x -C ~/gymtracker/app.new
  rm -rf ~/gymtracker/app && mv ~/gymtracker/app.new ~/gymtracker/app
  ~/venv/bin/pip install -q --upgrade ~/gymtracker/app 2>&1 | grep -v "notice" || true
  install -D -m 755 ~/gymtracker/app/deploy/kiosk.sh ~/.local/bin/gymtracker-kiosk
  if systemctl --user is-enabled -q gymtracker 2>/dev/null; then
    setsid nohup sh ~/gymtracker/app/deploy/restart-when-idle.sh >/dev/null 2>&1 < /dev/null &
    echo "installed; the app restarts as soon as no workout is running"
  else
    echo "installed (no service yet: run sh ~/gymtracker/app/deploy/install.sh)"
  fi
'

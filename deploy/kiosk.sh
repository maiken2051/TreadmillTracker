#!/bin/sh
# The touchscreen page, full screen. Started by the desktop at login
# (~/.config/autostart/gymtracker-kiosk.desktop); restarts Chromium if it exits.
# Stop it with:  pkill -f gymtracker-kiosk; pkill -x chromium
URL="http://127.0.0.1:8080/?kiosk"

# Wait up to 2 minutes for the app; the page reconnects by itself after that anyway.
i=0
while [ $i -lt 120 ] && ! curl -sf -m 2 http://127.0.0.1:8080/healthz >/dev/null; do
  sleep 1
  i=$((i + 1))
done

while true; do
  # --password-store=basic: otherwise an "Unlock Login Keyring" dialog covers the page.
  chromium --kiosk --noerrdialogs --disable-infobars --incognito --password-store=basic \
    --disable-session-crashed-bubble --ozone-platform=wayland "$URL" >/dev/null 2>&1
  sleep 3
done

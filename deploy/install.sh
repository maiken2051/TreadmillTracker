#!/bin/sh
# Set up a Raspberry Pi to run Gym Tracker by itself. Run on the Pi, as the desktop user,
# from an unpacked copy of the repo:   sh ~/gymtracker/app/deploy/install.sh
# Safe to run again: every step only changes what is missing or different.
#
# Assumes Raspberry Pi OS (Debian 13) with the desktop, auto-login, and sudo. The display
# section matches the Waveshare 7" DSI (H) panel mounted upside down; GT_DISPLAY=no skips it.
# GT_NAS_SHARE='//server/share/folder' sets up the copy to a network share (guest access;
# see deploy/fstab.snippet for a share that needs a login).
set -eu

APP=$(cd "$(dirname "$0")/.." && pwd)
DEPLOY="$APP/deploy"
VENV="$HOME/venv"
say() { printf '\n== %s\n' "$*"; }
# put SRC at DEST (root-owned with sudo), only if different; prints when it changes
place() {
  src=$1 dest=$2 mode=$3 how=${4:-}
  if [ -f "$dest" ] && cmp -s "$src" "$dest"; then return 1; fi
  $how install -D -m "$mode" "$src" "$dest"
  echo "  wrote $dest"
}

say "packages"
sudo apt-get install -y -q python3-venv cifs-utils chromium curl unattended-upgrades >/dev/null
echo "  ok"

say "app in $VENV"
[ -x "$VENV/bin/python" ] || python3 -m venv "$VENV"
"$VENV/bin/pip" install -q --upgrade "$APP"
"$VENV/bin/gymtracker" --version

say "screen backlight on/off for the video group"
if place "$DEPLOY/90-gymtracker-backlight.rules" /etc/udev/rules.d/90-gymtracker-backlight.rules 644 sudo; then
  sudo udevadm control --reload
  sudo udevadm trigger --subsystem-match=backlight --action=add
fi
id -nG | grep -qw video || { sudo usermod -aG video "$USER"; echo "  added $USER to video (log in again)"; }

say "automatic updates; reboots only when no workout is running"
place "$DEPLOY/52gymtracker-unattended" /etc/apt/apt.conf.d/52gymtracker-unattended 644 sudo || true
place "$DEPLOY/20auto-upgrades" /etc/apt/apt.conf.d/20auto-upgrades 644 sudo || true
place "$DEPLOY/reboot-if-idle.sh" /usr/local/sbin/gymtracker-reboot-if-idle 755 sudo || true
place "$DEPLOY/gymtracker-reboot.service" /etc/systemd/system/gymtracker-reboot.service 644 sudo || true
place "$DEPLOY/gymtracker-reboot.timer" /etc/systemd/system/gymtracker-reboot.timer 644 sudo || true
sudo systemctl daemon-reload
sudo systemctl enable --now -q gymtracker-reboot.timer
echo "  next window: $(systemctl show gymtracker-reboot.timer -p NextElapseUSecRealtime --value)"

say "NAS share (automount at /mnt/nas-workouts)"
if grep -q "/mnt/nas-workouts" /etc/fstab; then
  echo "  already in /etc/fstab"
elif [ -n "${GT_NAS_SHARE:-}" ]; then
  # fstab writes a space as \040; doubled here because sed's replacement eats one backslash
  share=$(printf '%s' "$GT_NAS_SHARE" | sed 's/ /\\\\040/g')
  sudo cp -p /etc/fstab "/etc/fstab.bak-$(date +%Y%m%d)"
  { echo; sed "s|@SHARE@|$share|" "$DEPLOY/fstab.snippet"; } | sudo tee -a /etc/fstab >/dev/null
  echo "  added $GT_NAS_SHARE to /etc/fstab"
else
  echo "  skipped: set GT_NAS_SHARE='//server/share/folder' to copy workouts to a share"
fi
if grep -q "/mnt/nas-workouts" /etc/fstab; then
  sudo mkdir -p /mnt/nas-workouts
  sudo systemctl daemon-reload
  sudo systemctl start "$(systemd-escape -p --suffix=automount /mnt/nas-workouts)"
  echo "  automount active"
fi

if [ "${GT_DISPLAY:-yes}" != no ]; then
  say "display: Waveshare 7\" DSI (H), rotated 180"
  overlay="dtoverlay=vc4-kms-dsi-waveshare-panel,7_0_inchH,dsi0"
  if ! grep -q "^$overlay" /boot/firmware/config.txt; then
    sudo cp -p /boot/firmware/config.txt "/boot/firmware/config.txt.bak-$(date +%Y%m%d)"
    echo "$overlay" | sudo tee -a /boot/firmware/config.txt >/dev/null
    echo "  added the panel overlay to config.txt (takes effect after a reboot)"
  fi
  if [ ! -f "$HOME/.config/kanshi/config" ]; then
    mkdir -p "$HOME/.config/kanshi"
    printf 'profile {\n\toutput DSI-1 enable transform 180\n}\n' > "$HOME/.config/kanshi/config"
    echo "  wrote ~/.config/kanshi/config"
  fi
  sudo raspi-config nonint do_blanking 1  # 1 = screen blanking off
fi

say "kiosk page at login"
place "$DEPLOY/kiosk.sh" "$HOME/.local/bin/gymtracker-kiosk" 755 || true
sed "s|@HOME@|$HOME|" "$DEPLOY/gymtracker-kiosk.desktop" > /tmp/gymtracker-kiosk.desktop
place /tmp/gymtracker-kiosk.desktop "$HOME/.config/autostart/gymtracker-kiosk.desktop" 644 || true
rm -f /tmp/gymtracker-kiosk.desktop

say "app service (starts at boot)"
place "$DEPLOY/gymtracker.service" "$HOME/.config/systemd/user/gymtracker.service" 644 || true
sudo loginctl enable-linger "$USER"
systemctl --user daemon-reload
systemctl --user enable -q gymtracker
systemctl --user restart gymtracker
sleep 3
curl -sf -m 5 http://127.0.0.1:8080/healthz >/dev/null && echo "  running: http://$(hostname):8080/" \
  || echo "  NOT answering yet; see: journalctl --user-unit gymtracker -n 50"

say "done"
echo "The kiosk page starts at the next login or reboot (or run ~/.local/bin/gymtracker-kiosk &)."

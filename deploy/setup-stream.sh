#!/usr/bin/env bash
# Set up the live stream on a fresh Ubuntu 24.04 machine (e.g. Oracle Cloud's
# free Arm VM). Run it there as the normal user (it uses sudo):
#
#   curl -fsSL https://raw.githubusercontent.com/davidackerman/still-life/main/deploy/setup-stream.sh | bash
#
# It installs ffmpeg and fonts, clones the public repo (no credentials), asks
# for the YouTube stream key (stored root-only in /etc/still-life/stream.env,
# never in the repo), and starts a service that restarts itself on failure, on
# reboot and when new code is pushed. Safe to run again.
set -euo pipefail

sudo apt-get update -qq
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq ffmpeg git python3-numpy python3-pil fonts-dejavu-core

REPO="$HOME/still-life"
[ -d "$REPO/.git" ] || git clone -q https://github.com/davidackerman/still-life "$REPO"
git -C "$REPO" pull -q --ff-only

sudo install -d -m 700 /etc/still-life
if ! sudo test -s /etc/still-life/stream.env; then
  read -rsp "YouTube stream key (Studio → Go live → Stream key; input hidden): " KEY </dev/tty; echo
  [ -n "$KEY" ] || { echo "no key given"; exit 1; }
  printf 'STREAM_KEY=%s\n' "$KEY" | sudo tee /etc/still-life/stream.env >/dev/null
  sudo chmod 600 /etc/still-life/stream.env
  unset KEY
fi

sudo tee /etc/systemd/system/still-life-stream.service >/dev/null <<UNIT
[Unit]
Description=still-life live stream
After=network-online.target
Wants=network-online.target

[Service]
User=$USER
WorkingDirectory=$REPO
EnvironmentFile=/etc/still-life/stream.env
ExecStart=/usr/bin/python3 $REPO/stream.py --repo $REPO
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
UNIT

sudo systemctl daemon-reload
sudo systemctl enable --now still-life-stream
sudo systemctl restart still-life-stream
sleep 5
systemctl --no-pager --lines=5 status still-life-stream || true
echo
echo "Streaming. Logs: journalctl -u still-life-stream -f"

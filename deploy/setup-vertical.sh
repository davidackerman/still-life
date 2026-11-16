#!/usr/bin/env bash
# Add the vertical (9:16) stream for YouTube's Dual stream, on the machine that
# already runs the wide one (setup-stream.sh). Run it there as the normal user:
#
#   bash ~/still-life/deploy/setup-vertical.sh
#
# It is its own service with its own checkout, so it can crash, stall or
# restart without touching the wide stream, and it runs at the lowest CPU
# priority (Nice=19), so the wide stream always wins the processor: 720x1280 at
# 15 fps, x264 ultrafast, 2.5 Mbps (about a quarter of a core on the A1).
#
# It asks for the vertical stream key (Studio, Dual stream set to Encoder: the
# second key), kept root-only in /etc/still-life/vertical.env and never in the
# repo. Safe to run again. To stop it: sudo systemctl disable --now still-life-vertical
set -euo pipefail

REPO="$HOME/still-life-tall"
[ -d "$REPO/.git" ] || git clone -q https://github.com/davidackerman/still-life "$REPO"
git -C "$REPO" pull -q --ff-only

sudo install -d -m 700 /etc/still-life
if ! sudo test -s /etc/still-life/vertical.env; then
  read -rsp "YouTube VERTICAL stream key (Dual stream → Encoder → the second key; input hidden): " KEY </dev/tty; echo
  [ -n "$KEY" ] || { echo "no key given"; exit 1; }
  if sudo grep -q "STREAM_KEY=$KEY\$" /etc/still-life/stream.env 2>/dev/null; then
    echo "that is the wide stream's key: paste the vertical one"; exit 1
  fi
  printf 'STREAM_KEY=%s\n' "$KEY" | sudo tee /etc/still-life/vertical.env >/dev/null
  sudo chmod 600 /etc/still-life/vertical.env
  unset KEY
fi

sudo tee /etc/systemd/system/still-life-vertical.service >/dev/null <<UNIT
[Unit]
Description=still-life live stream, vertical (Dual stream)
After=network-online.target still-life-stream.service
Wants=network-online.target

[Service]
User=$USER
WorkingDirectory=$REPO
EnvironmentFile=/etc/still-life/vertical.env
Environment=STREAM_FPS=15
Environment=X264_PRESET=ultrafast
Nice=19
CPUWeight=20
ExecStart=/usr/bin/python3 $REPO/stream.py --repo $REPO --tall --bitrate 2500
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
UNIT

sudo systemctl daemon-reload
sudo systemctl enable --now still-life-vertical
sudo systemctl restart still-life-vertical
sleep 5
systemctl --no-pager --lines=5 status still-life-vertical || true
echo
echo "Streaming tall. Logs: journalctl -u still-life-vertical -f"

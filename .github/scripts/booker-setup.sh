#!/usr/bin/env bash
# Installs the live-browser stack for dian_booker, then touches $BOOKER_READY_FILE.
# Runs in the background so the monitor starts polling right away.
set -euo pipefail
sudo apt-get update -qq
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq xvfb x11vnc novnc websockify >/dev/null
sudo curl -fsSL -o /usr/local/bin/cloudflared \
  https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64
sudo chmod +x /usr/local/bin/cloudflared
touch "$BOOKER_READY_FILE"

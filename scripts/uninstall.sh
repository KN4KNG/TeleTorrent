#!/usr/bin/env bash
set -Eeuo pipefail
[[ ${EUID:-$(id -u)} -eq 0 ]] || { echo "Run with sudo"; exit 1; }
systemctl disable --now teletorrent qbittorrent-nox telegram-bot-api 2>/dev/null || true
rm -f /etc/systemd/system/{teletorrent,qbittorrent-nox,telegram-bot-api}.service
systemctl daemon-reload
rm -rf /opt/teletorrent
cat <<'EOF'
Application removed. Data and configuration were retained:
  /etc/teletorrent.env
  /srv/teletorrent
  /var/lib/teletorrent
  /var/lib/telegram-bot-api
Remove those manually only when you no longer need them.
EOF

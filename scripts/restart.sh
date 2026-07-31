#!/usr/bin/env bash
set -Eeuo pipefail
sudo systemctl stop teletorrent qbittorrent-nox telegram-bot-api clamav-daemon
sleep 3
sudo systemctl start clamav-daemon telegram-bot-api qbittorrent-nox
sleep 8
sudo systemctl start teletorrent
sudo systemctl --no-pager --full status clamav-daemon telegram-bot-api qbittorrent-nox teletorrent

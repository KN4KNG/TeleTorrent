#!/usr/bin/env bash
set -Eeuo pipefail

[[ ${EUID:-$(id -u)} -eq 0 ]] || { echo "Run with sudo: sudo ./install.sh"; exit 1; }
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR=/opt/teletorrent
ENV_FILE=/etc/teletorrent.env
BOTAPI_SRC=/usr/local/src/telegram-bot-api
BOTAPI_BIN=/usr/local/bin/telegram-bot-api

read_value() {
  local prompt="$1" default="${2:-}" value
  if [[ -n "$default" ]]; then read -rp "$prompt [$default]: " value; else read -rp "$prompt: " value; fi
  printf '%s' "${value:-$default}"
}
read_secret() {
  local prompt="$1" value
  read -rsp "$prompt: " value; echo >&2
  printf '%s' "$value"
}
set_env() {
  local key="$1" value="$2"
  python3 - "$ENV_FILE" "$key" "$value" <<'PY'
from pathlib import Path
import sys
p=Path(sys.argv[1]); key=sys.argv[2]; value=sys.argv[3]
lines=p.read_text().splitlines() if p.exists() else []
out=[]; found=False
for line in lines:
    if line.startswith(key+'='):
        out.append(f'{key}={value}'); found=True
    else: out.append(line)
if not found: out.append(f'{key}={value}')
p.write_text('\n'.join(out)+'\n')
PY
}

ARCH="$(dpkg --print-architecture)"
case "$ARCH" in arm64|armhf) ;; *) echo "Warning: designed for Raspberry Pi OS; detected $ARCH";; esac

echo "[1/10] Installing operating-system packages"
apt-get update
DEBIAN_FRONTEND=noninteractive apt-get install -y \
  qbittorrent-nox clamav clamav-daemon clamav-freshclam \
  python3 python3-venv python3-pip libmagic1 p7zip-full unzip curl ca-certificates \
  git cmake g++ gperf make libssl-dev zlib1g-dev pkg-config

echo "[2/10] Creating service accounts and directories"
id teletorrent &>/dev/null || useradd --system --home /var/lib/teletorrent --create-home --shell /usr/sbin/nologin teletorrent
id telegrambotapi &>/dev/null || useradd --system --home /var/lib/telegram-bot-api --create-home --shell /usr/sbin/nologin telegrambotapi
usermod -aG clamav teletorrent || true
install -d -o teletorrent -g teletorrent -m 2770 /srv/teletorrent/{quarantine,approved,rejected}
install -d -o teletorrent -g teletorrent -m 0750 /var/lib/teletorrent/qbit
install -d -o telegrambotapi -g teletorrent -m 0750 /var/lib/telegram-bot-api /var/lib/telegram-bot-api/temp

echo "[3/10] Installing TeleTorrent"
rm -rf "$APP_DIR"
install -d -o root -g root -m 0755 "$APP_DIR"
cp -a "$ROOT_DIR/teletorrent" "$ROOT_DIR/requirements.txt" "$APP_DIR/"
python3 -m venv "$APP_DIR/.venv"
"$APP_DIR/.venv/bin/pip" install --upgrade pip wheel
"$APP_DIR/.venv/bin/pip" install -r "$APP_DIR/requirements.txt"
chown -R root:root "$APP_DIR"

echo "[4/10] Installing Telegram Local Bot API (can take 20-60 minutes on a Pi 4)"
if [[ ! -x "$BOTAPI_BIN" ]]; then
  rm -rf "$BOTAPI_SRC"
  git clone --depth 1 --recursive https://github.com/tdlib/telegram-bot-api.git "$BOTAPI_SRC"
  cmake -S "$BOTAPI_SRC" -B "$BOTAPI_SRC/build" -DCMAKE_BUILD_TYPE=Release -DCMAKE_INSTALL_PREFIX=/usr/local
  cmake --build "$BOTAPI_SRC/build" --target install --parallel 2
fi

echo "[5/10] Installing cloudflared"
case "$ARCH" in
  arm64) CF_URL='https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-arm64' ;;
  armhf) CF_URL='https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-arm' ;;
  amd64) CF_URL='https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64' ;;
  *) echo "Unsupported architecture for cloudflared: $ARCH"; exit 1 ;;
esac
curl -fL "$CF_URL" -o /usr/local/bin/cloudflared
chmod 0755 /usr/local/bin/cloudflared

echo "[6/10] Configuring secrets"
if [[ ! -f "$ENV_FILE" ]]; then cp "$ROOT_DIR/.env.example" "$ENV_FILE"; fi
BOT_TOKEN="$(read_secret 'Telegram BotFather token')"
USER_IDS="$(read_value 'Allowed numeric Telegram user ID(s), comma separated')"
API_ID="$(read_value 'Telegram API ID from my.telegram.org')"
API_HASH="$(read_secret 'Telegram API hash from my.telegram.org')"
DASH_USER="$(read_value 'Dashboard username' 'admin')"
DASH_PASS="$(read_secret 'Dashboard password')"
QBIT_USER="$(read_value 'qBittorrent username' 'admin')"
QBIT_PASS="$(read_secret 'qBittorrent password (may be left blank and set after first login)')"
[[ "$API_ID" =~ ^[0-9]+$ ]] || { echo "Telegram API ID must be numeric"; exit 1; }
[[ -n "$BOT_TOKEN" && -n "$USER_IDS" && -n "$API_HASH" && -n "$DASH_PASS" ]] || { echo "Required value missing"; exit 1; }
set_env TELEGRAM_BOT_TOKEN "$BOT_TOKEN"
set_env TELEGRAM_ALLOWED_USER_IDS "$USER_IDS"
set_env TELEGRAM_API_ID "$API_ID"
set_env TELEGRAM_API_HASH "$API_HASH"
set_env DASHBOARD_USERNAME "$DASH_USER"
set_env DASHBOARD_PASSWORD "$DASH_PASS"
set_env QBIT_USERNAME "$QBIT_USER"
set_env QBIT_PASSWORD "$QBIT_PASS"
chown root:teletorrent "$ENV_FILE"
chmod 0640 "$ENV_FILE"

echo "[7/10] Installing services"
cp "$ROOT_DIR/systemd/"*.service /etc/systemd/system/
chmod +x "$ROOT_DIR/scripts/"*.sh 2>/dev/null || true
systemctl daemon-reload
systemctl enable clamav-freshclam clamav-daemon qbittorrent-nox telegram-bot-api teletorrent

echo "[8/10] Updating ClamAV"
systemctl stop clamav-freshclam 2>/dev/null || true
freshclam || true
systemctl enable --now clamav-freshclam clamav-daemon

echo "[9/10] Switching the bot to the Local Bot API"
systemctl stop teletorrent telegram-bot-api 2>/dev/null || true
curl -fsS "https://api.telegram.org/bot${BOT_TOKEN}/logOut" >/dev/null || true
systemctl start telegram-bot-api
sleep 5
systemctl restart qbittorrent-nox
sleep 6
systemctl start teletorrent

echo "[10/10] Validation"
"$APP_DIR/.venv/bin/python" -m compileall -q "$APP_DIR/teletorrent"
systemctl --no-pager --full status telegram-bot-api qbittorrent-nox teletorrent | sed -n '1,70p' || true

cat <<'MSG'

TeleTorrent installation finished.

1. Open qBittorrent: http://PI-IP:8080
2. Find its temporary first-run password if needed:
   sudo journalctl -u qbittorrent-nox -n 100 --no-pager
3. Change the qBittorrent Web UI password, then update the same value here:
   sudo nano /etc/teletorrent.env
4. Restart the stack:
   sudo ./scripts/restart.sh   # from the cloned repository
   or: sudo systemctl restart qbittorrent-nox telegram-bot-api teletorrent
5. Dashboard: http://PI-IP:8787
6. Telegram: /start, /files, /recent, /search chess

Do not expose ports 8080, 8787, 8790, or 8081 directly to the internet.
Temporary file links use an on-demand Cloudflare Quick Tunnel and expire after 15 minutes.
MSG

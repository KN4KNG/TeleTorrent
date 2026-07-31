# TeleTorrent

**A secure Telegram-first torrent manager for Raspberry Pi.**

TeleTorrent turns a Raspberry Pi 4 into a private Telegram-controlled qBittorrent appliance with quarantine-first malware scanning, a live dashboard, automatic Telegram status updates, and approved-file delivery.

## What it does

- Accepts magnet links and `.torrent` files from allowlisted Telegram users.
- Downloads everything into a quarantine directory.
- Pauses completed torrents and scans them with ClamAV.
- Sends executable and script-like files to manual Telegram review.
- Moves clean content into approved storage only after verifying the move completed.
- Automatically edits one Telegram status message while torrents are active.
- Provides an authenticated web dashboard on port `8787`.
- Browses approved files through `/files`, `/recent`, and `/search`.
- Sends files below 1.99 GB directly through Telegram's Local Bot API.
- Creates a resumable 15-minute Cloudflare Quick Tunnel link for larger files or upload failures.
- Stores scan and event history in SQLite.

## Architecture

```text
Telegram
   │
   ├── magnet links / .torrent files
   ├── live progress and controls
   └── approved file browser
   │
TeleTorrent
   ├── qBittorrent Web API
   ├── ClamAV scanning
   ├── SQLite audit history
   ├── authenticated dashboard
   └── temporary download server
   │
/srv/teletorrent
   ├── quarantine
   ├── approved
   └── rejected
```

## Hardware and operating system

Recommended:

- Raspberry Pi 4 with 4 GB or more RAM
- Raspberry Pi OS Lite 64-bit
- USB 3 SSD mounted persistently for torrent storage
- Wired Ethernet where possible

Compiling Telegram's Local Bot API can take 20–60 minutes on a Pi 4.

## Prerequisites

Before installing, create:

1. A Telegram bot through `@BotFather` and copy its token.
2. Your numeric Telegram user ID.
3. A Telegram application at `my.telegram.org` and copy its `api_id` and `api_hash`.

The `api_id` and `api_hash` are required for the self-hosted Telegram Local Bot API, which enables direct bot uploads up to 2 GB.

## Fresh installation

```bash
git clone https://github.com/KN4KNG/teletorrent.git
cd teletorrent
sudo ./install.sh
```

The installer will prompt for Telegram, dashboard, and qBittorrent credentials. It installs and configures:

- qBittorrent-nox
- ClamAV and signature updates
- TeleTorrent Python environment
- Telegram Local Bot API
- cloudflared
- systemd services and service accounts

### Complete qBittorrent setup

After installation, open:

```text
http://YOUR-PI-IP:8080
```

Recent qBittorrent releases generate a temporary password on first launch. View it with:

```bash
sudo journalctl -u qbittorrent-nox -n 100 --no-pager
```

Log in, change the Web UI password, and set the same password in:

```bash
sudo nano /etc/teletorrent.env
```

Then restart:

```bash
sudo systemctl restart qbittorrent-nox telegram-bot-api teletorrent
```

### Dashboard

Open:

```text
http://YOUR-PI-IP:8787
```

Use `DASHBOARD_USERNAME` and `DASHBOARD_PASSWORD` from `/etc/teletorrent.env`.

## Telegram commands

```text
/start                 Introduction
/files                 Browse approved files
/recent                Show recently approved files
/search TERM           Search approved file names
/list                  Show torrents with controls
/status                Current service and transfer status
/history               Recent scan history
/storage               Storage usage
/pause HASH            Pause a torrent
/resume HASH           Resume a torrent
/delete HASH           Remove torrent, retain files
/deletefiles HASH      Remove torrent and files
```

Send a magnet URI as a normal message or attach a `.torrent` file.

## Automatic live status

When a torrent is active, TeleTorrent automatically sends one live status message and edits it every ten seconds. It displays:

- overall download and upload rate;
- per-torrent progress;
- ETA;
- seeds and peers;
- current qBittorrent state.

When activity ends, the same message is finalized rather than continuously posting new messages.

## Approved-file delivery

The file browser only exposes files physically located beneath `APPROVED_DIR`.

- Files smaller than `TELEGRAM_DIRECT_LIMIT_BYTES` are sent through Telegram.
- Files above the limit automatically receive a 15-minute HTTPS link.
- A direct-upload timeout or error also falls back to a link.
- Links use random tokens, do not reveal the filesystem path, support range requests, and expire automatically.
- cloudflared starts on demand and stops after all links expire.

The default direct threshold is `1,990,000,000` bytes to stay safely below Telegram's 2 GB limit.

## Storage on an external SSD

Mount the SSD persistently, for example at `/mnt/torrentssd`, then run:

```bash
sudo mkdir -p /mnt/torrentssd/{quarantine,approved,rejected}
sudo chown -R teletorrent:teletorrent /mnt/torrentssd
sudo chmod -R 2770 /mnt/torrentssd
```

Change these values in `/etc/teletorrent.env`:

```dotenv
QUARANTINE_DIR=/mnt/torrentssd/quarantine
APPROVED_DIR=/mnt/torrentssd/approved
REJECTED_DIR=/mnt/torrentssd/rejected
```

The systemd services use filesystem sandboxing. When using paths outside `/srv/teletorrent`, also update `ReadWritePaths=` in both qBittorrent and TeleTorrent service files or add systemd drop-ins.

## Service management

```bash
sudo ./scripts/restart.sh
sudo systemctl status clamav-daemon telegram-bot-api qbittorrent-nox teletorrent --no-pager
sudo journalctl -u teletorrent -f
sudo journalctl -u qbittorrent-nox -f
sudo journalctl -u telegram-bot-api -f
```

Validate the configuration file without displaying secrets:

```bash
sudo -u teletorrent test -r /etc/teletorrent.env && echo readable
```

## Security model and limitations

TeleTorrent treats all downloads as untrusted until scanning and promotion complete. It does not execute downloaded files.

ClamAV and extension policy reduce risk but cannot guarantee safety. New malware, encrypted archives, malformed files, targeted payloads, and unsupported formats can evade detection. Keep suspicious executables paused and analyze them in an isolated disposable virtual machine on another computer.

Do not port-forward ports `8080`, `8081`, `8787`, or `8790`. Use a private VPN such as Tailscale or WireGuard for persistent remote administration. Temporary file links are the only intentionally public endpoints and expire after 15 minutes.

Cloudflare Quick Tunnels are convenient but are a third-party transport. Do not create links for private files you are unwilling to transmit through Cloudflare's network.

## Updating from Git

```bash
cd teletorrent
git pull
sudo ./install.sh
```

The installer preserves an existing `/etc/teletorrent.env`, although it prompts for current values. Back up configuration and data before major upgrades:

```bash
sudo tar -czf teletorrent-backup.tgz \
  /etc/teletorrent.env \
  /var/lib/teletorrent \
  /srv/teletorrent
```

## Uninstall

```bash
sudo ./scripts/uninstall.sh
```

The uninstall script retains configuration and downloaded data by default.

## License

MIT License. See [LICENSE](LICENSE).

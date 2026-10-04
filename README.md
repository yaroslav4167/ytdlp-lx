<p align="center">
  <img src="Designer.png" alt="yt-dlp Telegram Bot" width="420">
</p>

# yt-dlp Telegram Bot

Telegram bot for downloading media and audio from supported `yt-dlp` sites. It supports direct links, text search, audio/video selection, Spotify track lookup, Coub looping, and retrying Telegram requests.

## Features

- Instagram, TikTok, Coub, YouTube, Spotify lookup, and other `yt-dlp` extractors.
- Separate audio/video streams are merged with FFmpeg.
- Coub video is looped until its full audio track ends.
- Output is kept under Telegram's 50 MiB bot upload limit where possible.
- Direct and SOCKS5-proxy extraction race for metadata; the successful route is reused for the download.
- Telegram send/edit operations retry after transient network errors.
- Playlists are rejected; only single media items are processed.

## Requirements

- Linux or another Python 3.10+ environment
- Python 3.10+
- FFmpeg and FFprobe
- Telegram bot token from `@BotFather`
- Optional Spotify Web API credentials
- Optional SOCKS5 proxy, such as Cloudflare WARP

## Installation

```bash
git clone <repository-url> ytdlp-lx
cd ytdlp-lx
chmod +x install.sh
./install.sh
nano .env
.venv/bin/python main.py
```

Set at least:

```dotenv
TELEGRAM_BOT_TOKEN=your_bot_token
```

The bot stores temporary files in `downloads/`. They are removed after sending; stale temporary files are cleaned on startup and before new downloads.

## Cloudflare WARP SOCKS5

Install `warp-cli` using Cloudflare's official WARP package for your distribution. Register and connect it:

```bash
sudo warp-cli registration new
sudo warp-cli mode warp
sudo warp-cli connect
sudo warp-cli status
```

The bot expects a local SOCKS5 endpoint. On systems where WARP exposes SOCKS5 on `127.0.0.1:40000`, set:

```dotenv
DOWNLOAD_PROXY=socks5://127.0.0.1:40000
```

Verify the endpoint before starting the bot:

```bash
ss -ltnp | grep 40000
curl --socks5-hostname 127.0.0.1:40000 https://ifconfig.me
```

If your WARP installation does not expose a SOCKS5 listener, use the endpoint provided by your WARP wrapper or leave `DOWNLOAD_PROXY` empty. Do not commit proxy credentials or tokens.

## systemd

Create a service user and install the project under `/opt/ytdlp-lx`:

```bash
sudo useradd --system --home /opt/ytdlp-lx --shell /usr/sbin/nologin ytbot
sudo cp -a . /opt/ytdlp-lx
sudo chown -R ytbot:ytbot /opt/ytdlp-lx
sudo cp systemd/ytdlp-lx.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now ytdlp-lx
sudo journalctl -u ytdlp-lx -f
```

## Tests

The smoke test checks downloads, valid audio/video streams, Telegram retry behavior, and text-search output.

```bash
.venv/bin/python test_ytdlp_smoke.py
```

TikTok may be skipped when the test server IP is blocked by TikTok. This is an upstream access restriction, not a test failure in the bot.

## Security

Never commit `.env`, bot tokens, Spotify secrets, browser cookies, downloaded media, or server backups. Rotate any credential that was previously stored in source code.

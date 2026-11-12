<p align="center">
  <img src="Designer.png" alt="yt-dlp Telegram Bot" width="420">
</p>

# yt-dlp Telegram Bot

Telegram bot for downloading media and audio from supported `yt-dlp` sites. It supports direct links, text search, audio/video selection, Spotify track lookup, Coub looping, and retrying Telegram requests.

## Features

- Instagram, TikTok, Coub, YouTube, Spotify/Yandex Music lookup, and other `yt-dlp` extractors.
- Separate audio/video streams are merged with FFmpeg.
- Coub video is looped until its full audio track ends.
- Output is kept under Telegram's 50 MiB bot upload limit where possible.
- Metadata remembers the successful direct/SOCKS5 route for 10 minutes per host. Unknown hosts race immediately; a slow cached route starts the alternative after 250 ms. Downloads reuse the preferred route with network-error fallback.
- Telegram send/edit operations retry after transient network errors.
- Playlists are rejected; only single media items are processed.

## Yandex Music links

Send a single-track link such as `https://music.yandex.ru/album/44620083/track/157450762`
or `https://music.yandex.ru/track/157450762`. No Yandex account or token is required.
The bot reads the public track title and artists, searches YouTube, and sends audio
from the first result as MP3, just like Spotify lookup. The recording may differ
from the Yandex version. Albums and playlists are not supported.

## Requirements

- Linux or another Python 3.11+ environment
- Python 3.11+
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

`MAX_PARALLEL_DOWNLOADS=2` in `.env` controls the number of simultaneous media
jobs (download, conversion and upload together; valid range 1–8). Additional jobs
wait in FIFO order and show their current queue position in the existing card,
or in a plain status message. Search and metadata lookup remain independent.
Restart the bot after changing the limit. Two jobs is the default for the shared
single-CPU production server; increase only when CPU, memory and disk allow it.

Stale cleanup includes `thumb_` and `final_` files. Active media identifiers and
files retained for manual resend are protected. Cancelling an active blocking
worker keeps its job slot until the worker stops.

Runtime logs include timestamps and media job IDs, including yt-dlp worker
messages. Bot tokens and URL credentials are redacted. Telegram's global error
handler logs transient API/network errors separately from unexpected failures.

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
.venv/bin/python -m unittest -v test_yandex_music
```

TikTok may be skipped when the test server IP is blocked by TikTok. This is an upstream access restriction, not a test failure in the bot.

## Security

Never commit `.env`, bot tokens, Spotify secrets, browser cookies, downloaded media, or server backups. Rotate any credential that was previously stored in source code.

## Interactive Telegram cards (HTMLBubbles)

Private chats use interactive format, search and progress cards, with separate
error screens and native file delivery. Use `/plain` for ordinary messages,
`/bubbles` to enable cards, and `/language ru|en|auto` to choose the UI language.
Telegram's user language is used automatically; missing/unsupported languages
fall back to English. Commands/preferences apply until the next bot restart.

Designs are editable in `bubbles/designs/default/`; translations are in
`bubbles/locales/`. New cards read templates from disk without restarting.
See [the editing and deployment guide](bubbles/README.md) for limits, fallback
behavior, and the 10-minute retry window for files whose delivery failed.

Widget mode has no extra caption or native inline keyboard. A URL offers direct
“Download audio” / “Download video” actions. Real mini-covers are embedded from
media metadata; after sending, the loading card is removed. Unconfirmed
media-upload outcomes are not automatically retried, preventing timeout-driven
duplicate files; the card asks the user to check the chat before a manual resend.

Search cards show all eight recordings as a flat list without pagination. The selected recording keeps its cover throughout downloading, conversion, and sending. Telegram does not support a scrollbar inside widgets; vertical gestures scroll the chat.

YouTube downloads require `yt-dlp[default]` (including EJS) and Deno >=2.3 or Node >=22. Deno is detected at `.tools/deno`, on PATH, or through `YTDLP_DENO`. Use the application virtual environment so these dependencies do not change other bots.

Search results are shown before optional thumbnail downloads, without fully inspecting every result. An animated processing bubble replaces the initial plain-text wait message. Upload status reads “Отправка…” / “Sending…”.

The bot registers `/start`, `/bubbles`, `/plain` and `/language` in Telegram’s command menu on startup, with Russian and English descriptions and an English fallback. A menu registration failure is logged without preventing polling.

`/language` opens a language picker with Russian, English and automatic buttons. Typed `/language ru`, `/language en` and `/language auto` remain supported. In shared chats, only the user who opened a picker can use its buttons.

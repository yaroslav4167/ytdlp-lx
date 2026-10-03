#!/usr/bin/env bash
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$APP_DIR"

python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -r requirements.txt

command -v ffmpeg >/dev/null || {
    echo "ffmpeg is required. Install it with your distribution package manager." >&2
    exit 1
}

if [ ! -f .env ]; then
    cp .env.example .env
    echo "Created .env. Set TELEGRAM_BOT_TOKEN before starting the bot."
fi
